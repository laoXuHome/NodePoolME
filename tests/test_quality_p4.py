"""P4: seven-day quality, independent maturity and four-level speed grades."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.database import db
from backend.health import store as p2
from backend.nodepool import store as p1
from backend.quality import calculator, service, store

NOW = datetime(2026, 11, 28, 12, 0, 0)
BAND_TIMES = [(2, "night"), (4, "night"), (6, "night"), (7, "night"),
              (9, "day"), (11, "day"), (13, "day"), (15, "day"),
              (18, "peak"), (20, "peak"), (22, "peak"), (23, "peak")]


def _prepare_node(name="P4节点", *, excluded=0):
    p1.ensure_schema()
    p2.ensure_schema()
    airport_id = db.execute(
        "INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled,speed_excluded) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"{name}机场", "http://example.test/sub", 30, "2026-11-28 00:00:00",
         "2026-11-28 00:00:00", 1, excluded),
    )
    node = {"raw_name": name, "protocol": "vless", "server": "1.2.3.4", "port": 443,
            "params": {"uuid": f"uuid-{name}", "network": "ws", "sni": "a.test"},
            "fingerprint": f"fp-{name}", "region": "HK"}
    with db.transaction() as conn:
        p1.ingest_fetch(conn, airport_id, [node])
    return airport_id, p1.list_nodes()["items"][0]["id"]


def _insert_check(node_id, at, status="success", p80=120, reason=None):
    db.execute(
        """INSERT INTO np2_checks
           (node_id,trigger,status,reason,config_version,target_count,success_count,
            failure_count,unknown_count,p80_ms,health_pass,started_at,finished_at,duration_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (node_id, "test", status, reason, 1, 1, 1 if status == "success" else 0,
         1 if status == "failure" else 0, 1 if status == "unknown" else 0,
         p80, 1 if status == "success" else 0, at, at, 100),
    )


def _seven_days(node_id, *, status="success"):
    for offset in range(7, 0, -1):
        day = (NOW - timedelta(days=offset)).date()
        for hour, _band in BAND_TIMES:
            _insert_check(node_id, f"{day.isoformat()} {hour:02d}:00:00", status=status,
                          p80=120 if status == "success" else None)


def _run(node_id, mb, *, at=None, status="ok", bytes_total=30 * 1024 * 1024):
    stamp = (at or (NOW - timedelta(hours=1))).strftime("%Y-%m-%d %H:%M:%S")
    return db.execute(
        """INSERT INTO speed_runs
           (node_id,started_at,finished_at,status,confidence,airport_name,trigger,
            multi_mbps,mb_per_s,single_mb_per_s,bytes_total,traffic_limit_bytes)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (node_id, stamp, stamp, status, "normal", "P4机场", "manual", mb * 8,
         mb, mb, bytes_total, 50 * 1024 * 1024),
    )


def test_quality_and_maturity_are_separate_100_point_evidence():
    _, node_id = _prepare_node()
    _seven_days(node_id)
    _run(node_id, 6.0)
    result = service.evaluate_node(node_id, settings=store.get_settings(), now=NOW)
    assert result["quality_score"] == 100.0
    assert result["valid_snapshots"] == 78
    assert result["full_days"] == 6
    assert result["maturity_score"] >= 90
    assert result["mature"] is True
    assert result["quality_pass"] is True
    assert result["quality_state"] == "pass"
    assert result["speed_grade"] == "excellent"
    assert result["speed_mb_s"] == 6.0
    current = store.current(node_id)
    assert current["quality_score"] == 100.0
    assert current["maturity_score"] == result["maturity_score"]
    history = store.recent_evaluations(node_id)
    assert history and history[0]["quality_score"] == 100.0


def test_immature_node_is_observed_not_zeroed():
    _, node_id = _prepare_node("新节点")
    _insert_check(node_id, "2026-11-27 18:00:00")
    result = service.evaluate_node(node_id, settings=store.get_settings(), now=NOW)
    assert result["quality_score"] is not None
    assert result["maturity_score"] < 70
    assert result["mature"] is False
    assert result["quality_state"] == "unknown"
    assert result["quality_pass"] is False
    assert "not_enough_qualified_days" in result["reasons"]
    assert result["speed_grade"] == "empty"
    assert result["speed_reason"] == "no_data"


def test_unknown_only_has_no_score_and_never_becomes_failure():
    _, node_id = _prepare_node("Unknown节点")
    for offset in range(7, 0, -1):
        day = (NOW - timedelta(days=offset)).date()
        _insert_check(node_id, f"{day.isoformat()} 18:00:00", status="unknown", p80=None,
                      reason="timeout")
    result = service.evaluate_node(node_id, settings=store.get_settings(), now=NOW)
    assert result["quality_score"] is None
    assert result["valid_snapshots"] == 0
    assert result["quality_state"] == "unknown"
    assert "no_valid_snapshots" in result["reasons"]
    assert result["quality_pass"] is False


def test_severe_day_caps_score_instead_of_being_hidden_by_average():
    _, node_id = _prepare_node("严重日节点")
    _seven_days(node_id)
    # Remove one day's successes and replace them with three valid failures.
    day = (NOW - timedelta(days=3)).date().isoformat()
    for hour, _ in BAND_TIMES:
        db.execute("DELETE FROM np2_checks WHERE node_id=? AND finished_at=?",
                   (node_id, f"{day} {hour:02d}:00:00"))
    for hour in (18, 20, 22):
        _insert_check(node_id, f"{day} {hour:02d}:00:00", status="failure", p80=None)
    result = service.evaluate_node(node_id, settings=store.get_settings(), now=NOW)
    assert result["severe_days"] == 1
    assert result["quality_score"] <= 60.0
    assert "severe_day" in result["reasons"]
    assert result["available_score"] > 85  # average alone would have been high


def test_three_time_band_weights_are_applied_when_one_band_is_missing():
    cfg = {**calculator.DEFAULT_SETTINGS}
    checks = [
        {"status": "success", "p80_ms": 100, "finished_at": "2026-11-27 19:00:00"},
        {"status": "success", "p80_ms": 700, "finished_at": "2026-11-27 10:00:00"},
    ]
    result = calculator.quality_from_checks(checks, cfg, datetime(2026, 11, 28, 12))
    # Only peak (50) and day (35) exist; the missing night band is not a failure.
    assert result["band_stats"]["night"]["valid"] == 0
    assert result["p80_ms"] is not None
    assert 100 < result["p80_ms"] < 700
    assert result["available_score"] == 100.0


def test_speed_grade_boundaries_and_median_not_peak():
    cfg = {**calculator.DEFAULT_SETTINGS}
    assert calculator.speed_grade(5.0, cfg)["key"] == "excellent"
    assert calculator.speed_grade(4.999, cfg)["key"] == "good"
    assert calculator.speed_grade(2.0, cfg)["key"] == "good"
    assert calculator.speed_grade(1.999, cfg)["key"] == "poor"
    assert calculator.speed_grade(0.5, cfg)["key"] == "poor"
    assert calculator.speed_grade(0.499, cfg)["key"] == "danger"
    assert calculator.speed_grade(None, cfg)["key"] == "empty"
    assert calculator.speed_grade(None, cfg, excluded=True)["key"] == "excluded"
    runs = [
        {"id": 3, "status": "ok", "mb_per_s": 10.0, "finished_at": "2026-11-27 18:00:00"},
        {"id": 2, "status": "ok", "mb_per_s": 0.1, "finished_at": "2026-11-26 18:00:00"},
        {"id": 1, "status": "ok", "mb_per_s": 0.1, "finished_at": "2026-11-25 18:00:00"},
    ]
    ev = calculator.speed_evaluation(runs, cfg, now=NOW)
    assert ev["key"] == "danger"
    assert ev["median_mb_s"] == 0.1
    assert ev["valid_runs"] == 3


def test_unknown_speed_never_overrides_previous_valid_and_exclusion_is_grey():
    airport_id, node_id = _prepare_node("测速节点")
    _seven_days(node_id)
    _run(node_id, 6.0)
    _run(node_id, 0.1, status="node_error", at=NOW - timedelta(minutes=10))
    result = service.evaluate_node(node_id, settings=store.get_settings(), now=NOW)
    assert result["speed_grade"] == "excellent"
    assert result["speed_mb_s"] == 6.0
    db.execute("UPDATE airports SET speed_excluded=1 WHERE id=?", (airport_id,))
    result = service.evaluate_node(node_id, settings=store.get_settings(), now=NOW)
    assert result["speed_grade"] == "excluded"
    assert result["speed_mb_s"] is None
    assert result["quality_score"] == 100.0  # exclusion never touches quality


def test_settings_validation_rejects_conflicting_thresholds():
    with pytest.raises(ValueError):
        calculator.validate_settings({"speed_excellent": 2.0, "speed_good": 2.0})
    with pytest.raises(ValueError):
        calculator.validate_settings({"weights": {"peak": 50, "night": 15, "day": 20}})
    with pytest.raises(ValueError):
        calculator.validate_settings({"min_daily_snapshots": 5})
    with pytest.raises(ValueError):
        calculator.validate_settings({"speed_window_end": "25:00"})
    ok = calculator.validate_settings({"speed_window_start": "17:00", "speed_window_end": "24:00"})
    assert ok["speed_window_end"] == "24:00"
