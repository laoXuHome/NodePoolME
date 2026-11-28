"""P6.2.1 final-delivery keyword filter acceptance tests."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-p621-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

import pytest
from fastapi.testclient import TestClient

from backend.main import app
from backend import config as app_config
from backend.competition import store as p5_store
from backend.database import db
from backend.final_filter import service as filter_service
from backend.final_filter import store as filter_store
from backend.final_filter.store import CATEGORIES, FinalFilterError, DEFAULT_RULES
from backend.final_results import service as final_service
from backend.nodepool import store as p1_store


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seed_node(name: str, airport_name: str, server: str = "127.0.0.1", port: int = 443) -> int:
    stamp = _stamp()
    with db.transaction() as conn:
        p1_store.ensure_schema(conn)
        row = conn.execute("SELECT id FROM airports WHERE name=?", (airport_name,)).fetchone()
        if row:
            aid = int(row["id"])
        else:
            aid = conn.execute(
                """INSERT INTO airports(name,url,enabled,interval_minutes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (airport_name, f"https://{abs(hash(airport_name))}.example/sub", 1, 60, stamp, stamp),
            ).lastrowid
        return int(conn.execute(
            """INSERT INTO np_nodes
               (airport_id,identity_key,occurrence,current_name,first_name,protocol,server,
                port,params,source_active,management_layer,base_health,data_status,
                first_seen_at,last_seen_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, f"{name}-{stamp}-key", 1, name, name, "ss", server, port, "{}",
             1, "observation", "healthy", "ample", stamp, stamp, stamp, stamp),
        ).lastrowid)


def prepare_p6(nodes: list[tuple[int, list[str]]], run_status: str = "success"):
    with db.transaction() as conn:
        p5_store.ensure_states(conn)
    stamp = _stamp()
    with db.transaction() as conn:
        for node_id, categories in nodes:
            for category in categories:
                conn.execute(
                    """UPDATE np5_state SET state='preferred', score=88, maturity=88,
                       p80_ms=120, delay_grade='good', speed_grade='good', speed_mb_s=3,
                       preferred_since=?, last_reason='admission_ok', last_evaluated_at=?,
                       last_change_at=?, updated_at=?
                       WHERE node_id=? AND category=?""",
                    (stamp, stamp, stamp, stamp, node_id, category),
                )
        run_id = p5_store.start_run(conn, trigger="manual", categories=list(CATEGORIES))
        p5_store.finish_run(conn, run_id, status=run_status, nodes_scanned=len(nodes), changes=0)
    return final_service.snapshot("manual")


def clear_rules():
    payload = filter_service.summary()
    for rule in payload["rules"]:
        filter_service.delete_rule(rule["id"])


def add_rule(keyword: str, enabled: bool = True) -> dict:
    return filter_service.create_rule(keyword, enabled=enabled)


def table_count(conn, table: str) -> int:
    try:
        return int(conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()["c"] or 0)
    except Exception:
        return 0


def test_defaults_crud_multi_rules_and_restore():
    first = filter_service.summary()
    assert len(first["rules"]) == len(DEFAULT_RULES)
    assert first["rule_stats"]["enabled"] == len(DEFAULT_RULES)
    assert first["rule_stats"]["disabled"] == 0
    assert [item["keyword"] for item in first["rules"]] == list(DEFAULT_RULES)
    revision_before = first["revision"]

    created = add_rule("香港官网")
    assert created["rule"]["keyword"] == "香港官网"
    assert created["rule"]["enabled"] is True
    assert created["summary"]["rule_stats"]["enabled"] == len(DEFAULT_RULES) + 1
    assert created["summary"]["revision"] > revision_before

    updated = filter_service.update_rule(created["rule"]["id"], keyword="HK-Official")
    assert updated["rule"]["keyword"] == "HK-Official"
    assert updated["rule"]["updated_at"]
    disabled = filter_service.update_rule(created["rule"]["id"], enabled=False)
    assert disabled["rule"]["enabled"] is False
    enabled = filter_service.update_rule(created["rule"]["id"], enabled=True)
    assert enabled["rule"]["enabled"] is True

    deleted = filter_service.delete_rule(created["rule"]["id"])
    assert deleted["rule"]["id"] == created["rule"]["id"]
    assert len(deleted["summary"]["rules"]) == len(DEFAULT_RULES)

    restored = filter_service.restore_defaults()
    assert restored["restored"] == len(DEFAULT_RULES)
    assert [item["keyword"] for item in restored["summary"]["rules"]] == list(DEFAULT_RULES)
    assert all(item["enabled"] for item in restored["summary"]["rules"])

    with pytest.raises(FinalFilterError) as err:
        add_rule("traffic")
    assert err.value.code == "keyword_exists"


def test_matching_is_case_insensitive_across_all_three_fields_and_keeps_all_reasons():
    n_name = seed_node("香港官网节点", "普通机场")
    n_airport = seed_node("普通节点", "TRAFFIC机场")
    n_server = seed_node("普通节点2", "普通机场2", server="10.20.30.40")
    n_multi = seed_node("官网-TRAFFIC", "Traffic机场", server="10.20.30.50")
    prepare_p6([(n_name, ["standard"]), (n_airport, ["standard"]),
                (n_server, ["standard"]), (n_multi, ["standard"])])
    clear_rules()
    add_rule("官网")
    add_rule("traffic")
    add_rule("10.20.30")

    payload = filter_service.preview()
    standard = next(item for item in payload["categories"] if item["code"] == "standard")
    assert standard["internal"] == 4
    assert standard["allowed"] + standard["filtered"] == 4
    by_node = {item["node_id"]: item for item in standard["filtered_nodes"]}
    assert set(by_node) == {n_name, n_airport, n_server, n_multi}
    assert [h["field"] for h in by_node[n_name]["hits"]] == ["node_name"]
    assert [h["field_label"] for h in by_node[n_name]["hits"]] == ["节点名称"]
    assert [h["field"] for h in by_node[n_airport]["hits"]] == ["airport_name"]
    # Uppercase TRAFFIC must match the lowercase rule.
    assert by_node[n_airport]["hits"][0]["keyword"] == "traffic"
    assert [h["field"] for h in by_node[n_server]["hits"]] == ["server"]
    assert set(by_node[n_multi]["matched_keywords"]) == {"官网", "traffic", "10.20.30"}
    # Same node matched by 4 keyword/field pairs: name/traffic, airport/traffic,
    # airport/官网 is absent, server/10.20.30. All hits remain.
    assert {(h["keyword"], h["field"]) for h in by_node[n_multi]["hits"]} == {
        ("官网", "node_name"), ("traffic", "node_name"),
        ("traffic", "airport_name"), ("10.20.30", "server"),
    }
    assert all(item["filtered_at"] == payload["computed_at"] for item in standard["filtered_nodes"])

    summary = filter_service.summary()
    hit_counts = {item["keyword"]: item["hit_count"] for item in summary["rules"]}
    assert hit_counts["官网"] == 2
    assert hit_counts["traffic"] == 2
    traffic = next(item for item in summary["rules"] if item["keyword"] == "traffic")
    assert traffic["hit_record_count"] == 3
    assert hit_counts["10.20.30"] == 2  # server node + multi node
    assert summary["rule_stats"]["matched"] == 3


def test_six_categories_filter_independently_and_internal_results_remain():
    node = seed_node("官网多类节点", "普通机场")
    other = seed_node("正常节点", "普通机场")
    prepare_p6([(node, list(CATEGORIES)), (other, list(CATEGORIES))])
    before = final_service.current()
    version_id = before["version"]["id"]
    before_counts = before["version"]["category_counts"]
    before_versions = len(final_service.versions(limit=100))
    clear_rules()
    add_rule("官网")

    payload = filter_service.preview()
    by_code = {item["code"]: item for item in payload["categories"]}
    for code in CATEGORIES:
        item = by_code[code]
        # Two internal nodes: the filtered one remains in P6.1, only the preview
        # marks it as filtered. Category accounts never bleed into another.
        assert item["internal"] == 2
        assert item["filtered"] == 1
        assert item["allowed"] == 1
        assert [n["node_id"] for n in item["filtered_nodes"]] == [node]
        assert [n["node_id"] for n in item["allowed_nodes"]] == [other]
        assert item["filtered_nodes"][0]["filtered_at"]

    current = final_service.current()
    assert current["version"]["id"] == version_id
    assert current["version"]["category_counts"] == before_counts
    assert current["version"]["total_nodes"] == 12
    assert len(final_service.versions(limit=100)) == before_versions
    with db.transaction() as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM np6_entries WHERE version_id=?",
                            (version_id,)).fetchone()["c"]
        state = conn.execute(
            "SELECT state,score FROM np5_state WHERE node_id=? ORDER BY category", (node,)
        ).fetchall()
    assert rows == before["version"]["total_nodes"]
    assert all(row["state"] == "preferred" and row["score"] == 88 for row in state)


def test_no_enabled_keyword_allows_every_node_in_all_categories():
    node = seed_node("官网节点", "10.20.30.机场", server="traffic.example.com")
    prepare_p6([(node, list(CATEGORIES))])
    clear_rules()
    payload = filter_service.preview()
    assert payload["totals"] == {"internal": 6, "filtered": 0, "allowed": 6}
    for item in payload["categories"]:
        assert item["filtered"] == 0
        assert item["allowed"] == item["internal"] == 1
        assert item["filtered_nodes"] == []


def test_rule_lifecycle_recomputes_without_detection_or_competition():
    node = seed_node("关键词测试节点", "普通机场")
    prepare_p6([(node, ["standard", "gpt"])])
    clear_rules()
    with db.transaction() as conn:
        before = {
            "nodes": conn.execute("SELECT COUNT(*) AS c FROM np_nodes").fetchone()["c"],
            "p5_runs": conn.execute("SELECT COUNT(*) AS c FROM np5_runs").fetchone()["c"],
            "p5_state": [dict(row) for row in conn.execute(
                "SELECT node_id,category,state,score FROM np5_state ORDER BY category"
            ).fetchall()],
            "p6_versions": conn.execute("SELECT COUNT(*) AS c FROM np6_versions").fetchone()["c"],
            "p6_entries": [dict(row) for row in conn.execute(
                "SELECT version_id,category,node_id,node_name,server,port FROM np6_entries ORDER BY category"
            ).fetchall()],
            "np2_events": table_count(conn, "np2_events"),
            "np3_events": table_count(conn, "np3_events"),
            "np4_events": table_count(conn, "np4_events"),
        }

    created = add_rule("关键词测试")
    assert created["summary"]["totals"]["filtered"] == 2
    assert created["summary"]["totals"]["allowed"] == 0
    updated = filter_service.update_rule(created["rule"]["id"], keyword="不存在的词")
    assert updated["summary"]["totals"]["filtered"] == 0
    assert updated["summary"]["totals"]["allowed"] == 2
    assert updated["summary"]["revision"] > created["summary"]["revision"]
    disabled = filter_service.update_rule(created["rule"]["id"], enabled=False)
    assert disabled["summary"]["rule_stats"]["enabled"] == 0
    enabled = filter_service.update_rule(created["rule"]["id"], enabled=True)
    assert enabled["summary"]["totals"]["filtered"] == 0  # keyword still unmatchable
    final_filter_service_recalc = filter_service.recompute()
    assert final_filter_service_recalc["totals"]["internal"] == 2

    with db.transaction() as conn:
        after = {
            "nodes": conn.execute("SELECT COUNT(*) AS c FROM np_nodes").fetchone()["c"],
            "p5_runs": conn.execute("SELECT COUNT(*) AS c FROM np5_runs").fetchone()["c"],
            "p5_state": [dict(row) for row in conn.execute(
                "SELECT node_id,category,state,score FROM np5_state ORDER BY category"
            ).fetchall()],
            "p6_versions": conn.execute("SELECT COUNT(*) AS c FROM np6_versions").fetchone()["c"],
            "p6_entries": [dict(row) for row in conn.execute(
                "SELECT version_id,category,node_id,node_name,server,port FROM np6_entries ORDER BY category"
            ).fetchall()],
            "np2_events": table_count(conn, "np2_events"),
            "np3_events": table_count(conn, "np3_events"),
            "np4_events": table_count(conn, "np4_events"),
        }
    assert after == before


def test_dashboard_and_summary_counts_match_current_preview():
    node = seed_node("官网统计节点", "普通机场")
    prepare_p6([(node, list(CATEGORIES))])
    clear_rules()
    add_rule("官网")
    summary = filter_service.summary()
    dash = filter_service.dashboard_summary()
    assert summary["totals"] == {"internal": 6, "filtered": 6, "allowed": 0}
    assert dash["internal"] == 6
    assert dash["filtered"] == 6
    assert dash["allowed"] == 0
    assert dash["enabled_rules"] == 1
    assert dash["version_no"] == summary["p6"]["version_no"]
    assert dash["computed_at"] == summary["computed_at"]



def test_api_routes_and_dashboard_block():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": app_config.ADMIN_PASSWORD}).status_code == 200
        node = seed_node("官网接口节点", "普通机场")
        prepare_p6([(node, list(CATEGORIES))])
        clear_rules()

        created = client.post("/api/final-filter/rules", json={"keyword": "官网", "enabled": True})
        assert created.status_code == 200
        rule_id = created.json()["rule"]["id"]
        assert created.json()["summary"]["totals"]["filtered"] == 6

        summary = client.get("/api/final-filter/summary").json()
        assert summary["totals"] == {"internal": 6, "filtered": 6, "allowed": 0}
        assert summary["rule_stats"]["enabled"] == 1
        assert summary["p6"]["version_no"].startswith("P6.1-")
        assert summary["boundary"]["p5_write"] is False
        assert summary["boundary"]["subscription_publish"] is False

        rules = client.get("/api/final-filter/rules").json()
        assert rules["rules"][0]["keyword"] == "官网"
        preview = client.get("/api/final-filter/preview").json()
        assert [item["code"] for item in preview["categories"]] == list(CATEGORIES)
        one = client.get("/api/final-filter/preview?category=standard").json()
        assert [item["code"] for item in one["categories"]] == ["standard"]
        assert client.get("/api/final-filter/preview?category=bad").status_code == 400

        patched = client.patch(f"/api/final-filter/rules/{rule_id}", json={"enabled": False})
        assert patched.status_code == 200
        assert patched.json()["summary"]["totals"]["filtered"] == 0
        assert client.delete(f"/api/final-filter/rules/{rule_id}").json()["summary"]["totals"]["allowed"] == 6

        restored = client.post("/api/final-filter/defaults")
        assert restored.status_code == 200
        assert len(restored.json()["summary"]["rules"]) == len(DEFAULT_RULES)
        assert client.post("/api/final-filter/recompute").status_code == 200

        dash = client.get("/api/dashboard").json()
        assert "final_result" in dash and "competition" in dash
        assert dash["final_result"]["total"] == 6
        assert dash["final_delivery"]["internal"] == 6
        # The restored default set includes 官网, so this demo node is filtered again.
        assert dash["final_delivery"]["filtered"] == 6
        assert dash["final_delivery"]["allowed"] == 0
        assert dash["final_delivery"]["enabled_rules"] == len(DEFAULT_RULES)
