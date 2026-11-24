"""数据成熟度：完整数据日 50% + 合格天数 25% + 覆盖率 25%，五级评级。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-maturity-")
os.environ.setdefault("NODEPOOL_DATA_DIR", _TMP)
os.environ.setdefault("NODEPOOL_CONFIG_DIR", os.path.join(_TMP, "config"))
os.environ.setdefault("NODEPOOL_LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("NODEPOOL_BACKUP_DIR", os.path.join(_TMP, "backup"))
os.environ.setdefault("NODEPOOL_ADMIN_PASSWORD", "test123")
os.environ.setdefault("NODEPOOL_TEST_ENGINE", "tcp")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402
from backend.scoring import eligibility  # noqa: E402


def test_weighted_formula():
    # 完整日 7/7、合格 7/7、覆盖率 100% → 100
    assert eligibility.maturity_score(7, 7, 1.0) == 100.0
    # 完整日 0、合格 7、覆盖率 0 → 25
    assert eligibility.maturity_score(0, 7, 0.0) == 25.0
    # 完整日 7、合格 7、覆盖率 0 → 75
    assert eligibility.maturity_score(7, 7, 0.0) == 75.0
    # 完整日 0、合格 0、覆盖率 100 → 25（但没有合格日会被强制降级）
    assert eligibility.maturity_score(0, 0, 1.0) == 25.0


@pytest.mark.parametrize("full,qualified,cov,expected", [
    (7, 7, 1.00, 100.0),
    (7, 7, 0.90, 97.5),
    (7, 7, 0.75, 93.75),
    (7, 7, 0.00, 75.0),
    (3, 7, 1.00, 71.43),
    (0, 7, 1.00, 50.0),
    (4, 5, 0.90, 68.93),
    (0, 3, 1.00, 35.71),
    (0, 0, 1.00, 25.0),
])
def test_expected_scores(full, qualified, cov, expected):
    score = eligibility.maturity_score(full, qualified, cov)
    assert round(score, 2) == expected


@pytest.mark.parametrize("score,grade", [
    (100, "ample"), (90, "ample"), (89.99, "normal"), (75, "normal"),
    (74.99, "reference"), (60, "reference"), (59.99, "rough"),
    (40, "rough"), (39.99, "insufficient"), (0, "insufficient"),
])
def test_grade_boundaries(score, grade):
    assert eligibility.maturity_grade(score) == grade


def test_day_ratios_count_final_kept_snapshots():
    stats = [
        eligibility.DayStat(date="2026-11-20", eligible=40, kept=12),
        eligibility.DayStat(date="2026-11-21", eligible=40, kept=11),
        eligibility.DayStat(date="2026-11-22", eligible=5, kept=4),
        eligibility.DayStat(date="2026-11-23", eligible=9, kept=12, reason="density"),
    ]
    dates = ["2026-11-19", "2026-11-20", "2026-11-21", "2026-11-22",
             "2026-11-23", "2026-11-24", "2026-11-25"]
    full, qualified = eligibility.day_ratios(stats, dates)
    assert full == 1 and qualified == 3     # 淘汰日与 11 份的日子都不算完整


def _seed(days_ago_to_times, interval=30, nodes=4):
    for table in ("schedule_history", "share_history", "batches", "airports", "run_events"):
        db.execute(f"DELETE FROM {table}")
    db.set_setting("test_interval_minutes", interval)
    created = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO airports (name, url, enabled, interval_minutes, created_at, updated_at)"
               " VALUES ('成熟度机场','http://x',1,60,?,?)", (created, created))
    airport_id = db.row("SELECT id FROM airports")["id"]
    now = datetime.now()
    db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
               " VALUES (?,?, 'complete', 3, 3, 1)", (created, created))
    batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for back, times in days_ago_to_times.items():
        day = base - timedelta(days=back)
        for hour, minute in times:
            stamp = day + timedelta(hours=hour, minutes=minute)
            if stamp > now:
                continue
            db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published,"
                       " total_available, airport_id, pool_count, published_count,"
                       " available_count, available_known) VALUES (?, ?, 120, 100, 100, ?, 10, 8, 8, 1)",
                       (stamp.strftime("%Y-%m-%d %H:%M:%S"), batch_id, airport_id))
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    for index in range(nodes):
        db.execute("INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint,"
                   " params, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (airport_id, f"n{index}", "ss", f"1.2.3.{index}", 443, f"fp{index}",
                    "{}", "available", stamp, stamp))


def _row(client):
    client.post("/api/auth/login", json={"password": "test123"})
    return client.get("/api/dashboard").json()["airport_list"][0]


def _half_hour_day():
    return [(h, m) for h in range(24) for m in (0, 30)]


def test_full_seven_days_is_ample_and_scored():
    _seed({back: _half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        row = _row(client)
    assert row["maturity_score"] >= 90
    assert row["data_status"] == "ample"
    assert row["full_days"] >= 6 and row["qualified_days"] == 7
    assert row["score"] is not None


def test_qualified_but_not_full_days():
    # 180 分钟计划：每天 8 个点位，8 份快照即可全覆盖，但只有 8 份 <12
    _seed({back: [(h, 0) for h in range(0, 24, 3)] for back in range(0, 7)}, interval=180)
    with TestClient(app) as client:
        row = _row(client)
    assert row["full_days"] == 0
    assert row["qualified_days"] >= 6
    assert row["data_status"] == "rough"       # ≈50 分
    assert row["score"] is not None


def test_mixed_days():
    # 6 天各 12 份（完整）+ 1 天 3 份（不合格）
    spec = {back: _half_hour_day() for back in range(0, 6)}
    spec[6] = [(h, 0) for h in range(3)]
    _seed(spec, interval=30)
    with TestClient(app) as client:
        row = _row(client)
    assert row["full_days"] == 6
    assert row["qualified_days"] == 6
    assert row["data_status"] in ("reference", "normal", "ample")


def test_three_qualified_days():
    _seed({0: _half_hour_day(), 1: _half_hour_day(), 2: _half_hour_day()})
    with TestClient(app) as client:
        row = _row(client)
    assert row["qualified_days"] <= 3
    assert row["maturity_score"] < 60
    assert row["data_status"] in ("rough", "insufficient")


def test_no_qualified_day_forces_insufficient_and_blank_score():
    _seed({1: [(9, 0), (15, 0)], 2: [(9, 0), (15, 0)], 3: [(9, 0)]})
    with TestClient(app) as client:
        row = _row(client)
    assert row["qualified_days"] == 0
    assert row["data_status"] == "insufficient"
    assert row["score"] is None
