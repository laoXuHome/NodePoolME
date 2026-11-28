"""P5 competition acceptance tests.

The data is synthetic, but every transition is driven through the public
engine/store path used by the API and scheduler.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from backend.competition import engine, service, store
from backend.business import store as business_store
from backend.database import db
from backend.quality import store as quality_store


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S")


def seed_node(*, name="节点A", first_seen=None, health="healthy", active=1,
              airport="测试机场", enabled=1) -> int:
    first = _stamp(first_seen or datetime.now())
    with db.transaction() as conn:
        from backend.nodepool import store as p1_store
        p1_store.ensure_schema(conn)
        aid = conn.execute(
            "SELECT id FROM airports WHERE name=?", (airport,)
        ).fetchone()
        if aid is None:
            aid = conn.execute(
                """INSERT INTO airports(name,url,enabled,interval_minutes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (airport, f"https://{airport}.example/sub", enabled, 60, first, first),
            ).lastrowid
        else:
            aid = int(aid["id"])
        nid = conn.execute(
            """INSERT INTO np_nodes
               (airport_id,identity_key,occurrence,current_name,first_name,protocol,server,
                port,params,source_active,management_layer,base_health,data_status,
                first_seen_at,last_seen_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, f"{name}-key", 1, name, name, "ss", "127.0.0.1", 443, "{}",
             active, "observation", health, "ample", first, first, first, first),
        ).lastrowid
        return int(nid)


def seed_quality(node_id: int, *, score=88.0, maturity=85.0, p80=120,
                 speed_grade="good", speed_mb=3.0, severe=0, max_consec=0,
                 at=None, quality_state="pass"):
    moment = _stamp(at or datetime.now())
    detail = {
        "p80_ms": p80,
        "severe_days": severe,
        "max_consecutive_failures": max_consec,
        "stability_score": 90.0,
    }
    with db.transaction() as conn:
        quality_store.ensure_schema(conn)
        conn.execute(
            """INSERT INTO np4_current
               (node_id,evaluated_at,quality_score,maturity_score,mature,quality_pass,
                quality_state,speed_grade,speed_mb_s,speed_kb_s,speed_at,reasons_json,
                detail_json,config_version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(node_id) DO UPDATE SET evaluated_at=excluded.evaluated_at,
                 quality_score=excluded.quality_score,maturity_score=excluded.maturity_score,
                 mature=excluded.mature,quality_pass=excluded.quality_pass,
                 quality_state=excluded.quality_state,speed_grade=excluded.speed_grade,
                 speed_mb_s=excluded.speed_mb_s,speed_kb_s=excluded.speed_kb_s,
                 speed_at=excluded.speed_at,reasons_json=excluded.reasons_json,
                 detail_json=excluded.detail_json""",
            (node_id, moment, score, maturity,
             1 if (maturity is not None and maturity >= 70) else 0,
             1 if (score is not None and score >= 75) else 0,
             quality_state, speed_grade, speed_mb,
             (speed_mb or 0) * 1024, moment, "[]",
             json.dumps(detail), 1),
        )


def seed_business(node_id: int, business: str, *, state="qualified", at=None):
    moment = _stamp(at or datetime.now())
    with db.transaction() as conn:
        business_store.ensure_schema(conn)
        conn.execute(
            """INSERT INTO np3_state
               (node_id,business,state,last_run_at,last_valid_at,updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(node_id,business) DO UPDATE SET state=excluded.state,
                 last_run_at=excluded.last_run_at,last_valid_at=excluded.last_valid_at,
                 updated_at=excluded.updated_at""",
            (node_id, business, state, moment, moment if state == "qualified" else None,
             moment),
        )


def run_at(at: datetime, *, trigger="manual"):
    return engine.run(trigger=trigger, now=at, refresh_p4=False)


def cat_state(node_id: int, category="standard") -> dict:
    item = store.get_state(node_id, category)
    assert item is not None
    return item


def test_new_node_starts_in_observation_and_is_not_preferred():
    now = datetime.now()
    nid = seed_node(first_seen=now)
    seed_quality(nid, score=95, maturity=90)
    result = run_at(now)
    assert result["ok"]
    state = cat_state(nid)
    assert state["state"] == "observation"
    assert state["last_reason"] == "observation_period"


def test_immature_data_stays_observation():
    now = datetime.now()
    nid = seed_node(first_seen=now - timedelta(days=10))
    seed_quality(nid, score=95, maturity=55)
    run_at(now)
    assert cat_state(nid)["state"] == "observation"
    assert cat_state(nid)["admission_ok"] is False


def test_observation_completion_enters_candidate_only():
    now = datetime.now()
    nid = seed_node(first_seen=now - timedelta(days=5))
    seed_quality(nid, score=90, maturity=88)
    run_at(now)
    state = cat_state(nid)
    assert state["state"] == "candidate"
    assert state["consecutive_successes"] == 1
    assert state["preferred_since"] is None


def test_candidate_needs_two_valid_confirmations_for_preference():
    now = datetime.now()
    nid = seed_node(first_seen=now - timedelta(days=5))
    seed_quality(nid, score=90, at=now)
    run_at(now)
    assert cat_state(nid)["state"] == "candidate"
    seed_quality(nid, score=90, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    assert cat_state(nid)["state"] == "preferred"


def test_mature_node_is_not_replaced_without_gap_and_two_confirmations():
    now = datetime.now()
    mature = seed_node(name="成熟节点", first_seen=now - timedelta(days=20))
    newcomer = seed_node(name="新节点", first_seen=now - timedelta(days=8))
    seed_quality(mature, score=80, at=now)
    run_at(now)
    seed_quality(mature, score=80, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    assert cat_state(mature)["state"] == "preferred"
    seed_quality(newcomer, score=84, at=now + timedelta(minutes=2))
    run_at(now + timedelta(minutes=2))
    seed_quality(newcomer, score=84, at=now + timedelta(minutes=3))
    run_at(now + timedelta(minutes=3))
    assert cat_state(newcomer)["state"] == "candidate"
    assert cat_state(mature)["state"] == "preferred"
    assert cat_state(newcomer)["advantage_count"] == 0


def test_new_node_replaces_mature_only_after_gap_and_two_confirmations():
    now = datetime.now()
    mature = seed_node(name="旧优选", first_seen=now - timedelta(days=20))
    newcomer = seed_node(name="高分配置", first_seen=now - timedelta(days=8))
    seed_quality(mature, score=80, at=now)
    run_at(now)
    seed_quality(mature, score=80, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    assert cat_state(mature)["state"] == "preferred"
    seed_quality(newcomer, score=90, at=now + timedelta(minutes=2))
    run_at(now + timedelta(minutes=2))
    assert cat_state(newcomer)["state"] == "candidate"
    assert cat_state(newcomer)["advantage_count"] == 1
    seed_quality(newcomer, score=90, at=now + timedelta(minutes=3))
    run_at(now + timedelta(minutes=3))
    assert cat_state(newcomer)["state"] == "preferred"
    assert cat_state(mature)["state"] == "observation"
    history = store.history(mature, limit=10)
    assert any(h["reason"] == "replaced_by_new_node" for h in history)


def test_mature_single_low_score_does_not_demote():
    now = datetime.now()
    nid = seed_node(name="稳节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    run_at(now)
    seed_quality(nid, score=90, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    seed_quality(nid, score=62, at=now + timedelta(minutes=2))
    run_at(now + timedelta(minutes=2))
    state = cat_state(nid)
    assert state["state"] == "preferred"
    assert state["consecutive_failures"] == 1


def test_three_consecutive_failures_demote_to_observation_and_recover():
    now = datetime.now()
    nid = seed_node(name="恢复节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    run_at(now)
    seed_quality(nid, score=90, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    for i in range(2, 5):
        seed_quality(nid, score=62, at=now + timedelta(minutes=i))
        run_at(now + timedelta(minutes=i))
    state = cat_state(nid)
    assert state["state"] == "observation"
    assert state["recovery_required"] is True
    seed_quality(nid, score=90, at=now + timedelta(minutes=5))
    run_at(now + timedelta(minutes=5))
    assert cat_state(nid)["state"] == "observation"
    seed_quality(nid, score=90, at=now + timedelta(minutes=6))
    run_at(now + timedelta(minutes=6))
    assert cat_state(nid)["state"] == "candidate"


def test_unknown_does_not_increment_failures():
    now = datetime.now()
    nid = seed_node(name="未知节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    run_at(now)
    seed_quality(nid, score=90, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    seed_quality(nid, score=None, maturity=None, quality_state="unknown",
                 at=now + timedelta(minutes=2))
    run_at(now + timedelta(minutes=2))
    seed_quality(nid, score=None, maturity=None, quality_state="unknown",
                 at=now + timedelta(minutes=3))
    run_at(now + timedelta(minutes=3))
    state = cat_state(nid)
    assert state["state"] == "preferred"
    assert state["consecutive_failures"] == 0


def test_business_qualification_is_independent_and_multi_category_can_coexist():
    now = datetime.now()
    nid = seed_node(name="多业务节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=92, at=now)
    seed_business(nid, "gpt", state="suspended", at=now)
    for business in ("youtube", "telegram", "github", "global"):
        seed_business(nid, business, state="qualified", at=now)
    run_at(now)
    seed_quality(nid, score=92, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    assert cat_state(nid, "gpt")["state"] != "preferred"
    for business in ("youtube", "telegram", "github", "global"):
        assert cat_state(nid, business)["state"] == "preferred"
    assert cat_state(nid, "standard")["state"] == "preferred"


def test_eliminated_node_remains_in_p1():
    now = datetime.now()
    nid = seed_node(name="失活节点", first_seen=now - timedelta(days=20), active=1)
    seed_quality(nid, score=90, at=now)
    run_at(now)
    with db.transaction() as conn:
        conn.execute("UPDATE np_nodes SET source_active=0 WHERE id=?", (nid,))
    run_at(now + timedelta(minutes=1))
    assert cat_state(nid)["state"] == "eliminated"
    assert db.row("SELECT COUNT(*) AS c FROM np_nodes WHERE id=?", (nid,))["c"] == 1


def test_duplicate_and_manual_runs_do_not_double_count():
    now = datetime.now()
    nid = seed_node(name="不重复节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    run_at(now)
    assert cat_state(nid)["state"] == "candidate"
    first_history = len(store.history(nid, limit=100))
    again = run_at(now + timedelta(seconds=5))
    assert again["changes"] == 0
    assert len(store.history(nid, limit=100)) == first_history


def test_summary_and_dashboard_counts_are_consistent():
    now = datetime.now()
    nid = seed_node(name="统计节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    run_at(now)
    seed_quality(nid, score=90, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    summary = service.summary()
    dash = service.dashboard_summary()
    assert summary["preferred_standard"] == dash["preferred_standard"]
    assert len(summary["categories"]) == 6
    assert summary["preferred_standard"] >= 1
    assert store.history(nid, limit=100), "history must be queryable"


def test_settings_validate_range_and_restore_defaults():
    now = datetime.now()
    nid = seed_node(name="参数节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    payload = service.settings()
    assert payload["current"]["candidate_score"] == 80
    try:
        service.update_settings({"candidate_score": 99})
    except store.CompetitionError as exc:
        assert exc.code == "invalid_setting"
    else:
        raise AssertionError("out-of-range candidate_score must be rejected")
    try:
        service.update_settings({"retain_score": 85, "candidate_score": 70})
    except store.CompetitionError as exc:
        assert exc.code == "invalid_setting"
    else:
        raise AssertionError("retain_score above candidate_score must be rejected")
    changed = service.update_settings({"observation_days": 5, "replace_gap": 10})
    assert changed["current"]["observation_days"] == 5
    assert changed["current"]["replace_gap"] == 10
    assert changed["current"]["config_version"] > payload["current"]["config_version"]
    restored = service.restore_defaults()
    assert restored["current"]["observation_days"] == 3
    assert restored["current"]["replace_gap"] == 8


def test_same_score_does_not_replace_mature_preferred():
    now = datetime.now()
    mature = seed_node(name="同分成熟", first_seen=now - timedelta(days=20))
    newcomer = seed_node(name="同分新节点", first_seen=now - timedelta(days=8))
    seed_quality(mature, score=90, at=now)
    run_at(now)
    seed_quality(mature, score=90, at=now + timedelta(minutes=1))
    run_at(now + timedelta(minutes=1))
    assert cat_state(mature)["state"] == "preferred"
    for i in (2, 3, 4):
        seed_quality(newcomer, score=90, at=now + timedelta(minutes=i))
        run_at(now + timedelta(minutes=i))
    assert cat_state(newcomer)["state"] == "candidate"
    assert cat_state(mature)["state"] == "preferred"
    assert cat_state(newcomer)["advantage_count"] == 0


def test_speed_danger_blocks_and_no_speed_data_is_flagged_allowed():
    now = datetime.now()
    danger = seed_node(name="危速度", first_seen=now - timedelta(days=20))
    no_speed = seed_node(name="无测速", first_seen=now - timedelta(days=20))
    seed_quality(danger, score=90, speed_grade="danger", speed_mb=0.3, at=now)
    seed_quality(no_speed, score=90, speed_grade="empty", speed_mb=None, at=now)
    run_at(now)
    assert cat_state(danger)["state"] == "observation"
    assert cat_state(danger)["admission_ok"] is False
    assert cat_state(no_speed)["admission_ok"] is True
    assert cat_state(no_speed)["speed_grade"] == "empty"


def test_category_error_does_not_block_other_categories_and_history_is_explainable():
    now = datetime.now()
    nid = seed_node(name="隔离节点", first_seen=now - timedelta(days=20))
    seed_quality(nid, score=90, at=now)
    for business in ("youtube", "telegram", "github", "global"):
        seed_business(nid, business, state="qualified", at=now)
    # GPT is deliberately not qualified; the other categories still run.
    run_at(now)
    for category in ("standard", "youtube", "telegram", "github", "global"):
        assert cat_state(nid, category)["state"] == "candidate"
    assert cat_state(nid, "gpt")["state"] == "observation"
    history = store.history(nid, category="standard", limit=20)
    assert history
    assert {"node_id", "category", "from_state", "to_state", "at", "reason",
            "config_version"} <= set(history[0].keys())
