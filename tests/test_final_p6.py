"""P6.1 final preferred-result and immutable version acceptance tests."""
from __future__ import annotations

from datetime import datetime

import pytest

from backend.competition import store as p5_store
from backend.database import db
from backend.final_results import service as final_service
from backend.final_results import store as final_store
from backend.final_results.store import CATEGORIES, FinalError


def seed_node(name: str) -> int:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.transaction() as conn:
        from backend.nodepool import store as p1_store
        p1_store.ensure_schema(conn)
        aid = conn.execute("SELECT id FROM airports WHERE name=?", (f"{name}机场",)).fetchone()
        if aid is None:
            aid = conn.execute(
                """INSERT INTO airports(name,url,enabled,interval_minutes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (f"{name}机场", f"https://{name}.example/sub", 1, 60, stamp, stamp),
            ).lastrowid
        else:
            aid = int(aid["id"])
        return int(conn.execute(
            """INSERT INTO np_nodes
               (airport_id,identity_key,occurrence,current_name,first_name,protocol,server,
                port,params,source_active,management_layer,base_health,data_status,
                first_seen_at,last_seen_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(aid), f"{name}-key", 1, name, name, "ss", "127.0.0.1", 443, "{}",
             1, "observation", "healthy", "ample", stamp, stamp, stamp, stamp),
        ).lastrowid)


def seed_states():
    with db.transaction() as conn:
        p5_store.ensure_states(conn)


def mark(node_id: int, categories, state="preferred", score=88.0):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.transaction() as conn:
        for category in categories:
            conn.execute(
                """UPDATE np5_state SET state=?, score=?, maturity=?, p80_ms=?,
                   delay_grade=?, speed_grade=?, speed_mb_s=?, preferred_since=?,
                   last_reason=?, last_evaluated_at=?, last_change_at=?, updated_at=?
                   WHERE node_id=? AND category=?""",
                (state, score, 88.0, 120, "good", "good", 3.0,
                 stamp if state == "preferred" else None,
                 "admission_ok" if state == "preferred" else "test_reset",
                 stamp, stamp, stamp, int(node_id), category),
            )


def make_run(status="success"):
    with db.transaction() as conn:
        run_id = p5_store.start_run(conn, trigger="manual", categories=list(CATEGORIES))
        if status != "running":
            p5_store.finish_run(conn, run_id, status=status, nodes_scanned=3, changes=0)
        return run_id


def history_count():
    return len(final_service.versions(limit=100))


def test_six_categories_and_same_node_can_appear_in_all(monkeypatch):
    n1, n2, n3 = seed_node("甲"), seed_node("乙"), seed_node("丙")
    seed_states()
    mark(n1, list(CATEGORIES))
    mark(n2, ["gpt"])
    mark(n3, ["youtube"])
    make_run()

    result = final_service.snapshot("manual")
    assert result["created"] is True
    current = final_service.current()
    version = current["version"]
    assert version["version_no"] == "P6.1-0001"
    assert version["total_nodes"] == 8
    assert version["category_counts"] == {
        "standard": 1, "gpt": 2, "youtube": 2,
        "telegram": 1, "github": 1, "global": 1,
    }
    by_code = {item["code"]: item for item in current["categories"]}
    for code in CATEGORIES:
        assert n1 in [n["node_id"] for n in by_code[code]["nodes"]]
    assert n2 in [n["node_id"] for n in by_code["gpt"]["nodes"]]
    assert n2 not in [n["node_id"] for n in by_code["youtube"]["nodes"]]


def test_versions_record_add_remove_retain_per_category_and_are_immutable():
    n1, n2, n3 = seed_node("丁"), seed_node("戊"), seed_node("己")
    seed_states()
    mark(n1, ["standard", "gpt"])
    mark(n2, ["youtube"])
    make_run()
    first = final_service.snapshot("manual")
    first_id = first["version"]["id"]

    mark(n1, ["gpt"], state="eliminated")
    mark(n2, ["gpt"])
    mark(n3, ["standard"])
    make_run()
    second = final_service.snapshot("auto")
    second_id = second["version"]["id"]
    assert second["version"]["previous_version_id"] == first_id
    assert second_id != first_id

    diff = final_service.diff(second_id)
    by_code = {item["code"]: item for item in diff["categories"]}
    # category isolation: changes in GPT must not be reported in YouTube.
    assert [n["node_id"] for n in by_code["gpt"]["added"]] == [n2]
    assert [n["node_id"] for n in by_code["gpt"]["removed"]] == [n1]
    assert [n["node_id"] for n in by_code["standard"]["added"]] == [n3]
    assert [n["node_id"] for n in by_code["standard"]["retained"]] == [n1]
    assert by_code["youtube"]["retained"] and not by_code["youtube"]["added"]
    assert not by_code["youtube"]["removed"]
    # Every change row carries its own change time and trigger reason.
    for item in by_code["gpt"]["added"] + by_code["gpt"]["removed"]:
        assert item["at"]
        assert item["trigger"] == "auto"

    # An older snapshot is still readable and unchanged after a new version.
    old = final_service.version(first_id)
    assert old["version"]["version_no"] == "P6.1-0001"
    old_gpt = next(item for item in old["categories"] if item["code"] == "gpt")
    assert [n["node_id"] for n in old_gpt["nodes"]] == [n1]
    assert old["version"]["change_counts"]["gpt"] == {
        "added": 1, "removed": 0, "retained": 0,
    }

    # Reading repeatedly never creates a version.
    before = history_count()
    for _ in range(5):
        final_service.current()
        final_service.version(first_id)
    assert history_count() == before

    # Same membership: an explicit snapshot is a no-op, not a new version.
    again = final_service.snapshot("manual")
    assert again["created"] is False
    assert again["reason"] == "unchanged"
    assert history_count() == before


def test_empty_result_is_a_valid_version_and_does_not_reuse_old_nodes():
    n1 = seed_node("庚")
    seed_states()
    mark(n1, ["standard", "gpt"])
    make_run()
    first = final_service.snapshot("manual")

    mark(n1, ["standard", "gpt"], state="observation")
    make_run()
    empty = final_service.snapshot("manual")
    assert empty["created"] is True
    current = final_service.current()
    assert current["version"]["is_empty"] is True
    assert current["version"]["total_nodes"] == 0
    assert all(item["count"] == 0 and item["nodes"] == [] for item in current["categories"])
    assert current["version"]["previous_version_id"] == first["version"]["id"]
    old = final_service.version(first["version"]["id"])
    assert old["version"]["total_nodes"] == 2


def test_invalid_runs_are_rejected_and_do_not_overwrite_or_create_versions():
    n1 = seed_node("辛")
    seed_states()
    mark(n1, ["standard"])
    # No P5 run at all.
    with pytest.raises(FinalError) as err:
        final_service.snapshot("manual")
    assert err.value.code == "no_p5_run"
    assert history_count() == 0

    # Successful version 1.
    make_run()
    first = final_service.snapshot("manual")
    first_id = first["version"]["id"]

    # A running run must not overwrite the valid version.
    make_run("running")
    with pytest.raises(FinalError) as err:
        final_service.snapshot("manual")
    assert err.value.code == "run_not_success"
    assert final_service.current()["version"]["id"] == first_id
    assert history_count() == 1

    # A partial run is also invalid.
    with db.transaction() as conn:
        run_id = conn.execute("SELECT id FROM np5_runs ORDER BY id DESC LIMIT 1").fetchone()["id"]
        p5_store.finish_run(conn, int(run_id), status="partial", nodes_scanned=1, changes=0,
                            message="partial")
    with pytest.raises(FinalError) as err:
        final_service.snapshot("manual")
    assert err.value.code == "run_not_success"
    assert final_service.current()["version"]["id"] == first_id

    # Same read-only guarantee for the summary endpoint.
    for _ in range(3):
        final_service.summary()
    assert history_count() == 1


def test_snapshot_does_not_modify_p1_or_p5_state():
    n1 = seed_node("壬")
    seed_states()
    mark(n1, ["standard", "gpt"])
    make_run()
    with db.transaction() as conn:
        before = [dict(row) for row in conn.execute(
            "SELECT node_id,category,state,score,maturity,last_reason FROM np5_state ORDER BY node_id,category"
        ).fetchall()]
    final_service.snapshot("manual")
    final_service.current()
    with db.transaction() as conn:
        after = [dict(row) for row in conn.execute(
            "SELECT node_id,category,state,score,maturity,last_reason FROM np5_state ORDER BY node_id,category"
        ).fetchall()]
        history = conn.execute("SELECT COUNT(*) AS c FROM np5_history").fetchone()["c"]
        runs = conn.execute("SELECT COUNT(*) AS c FROM np5_runs").fetchone()["c"]
    assert before == after
    assert history == 0
    assert runs == 1


def test_summary_and_dashboard_expose_real_storage_only():
    n1 = seed_node("癸")
    seed_states()
    mark(n1, ["global"])
    make_run()
    final_service.snapshot("manual")
    summary = final_service.summary()
    assert summary["history_count"] == 1
    assert summary["source"]["status"] == "success"
    assert summary["pending"] is False
    dash = final_service.dashboard_summary()
    assert dash["version_no"] == summary["current"]["version_no"]
    assert dash["preferred_business"]["global"] == 1
    assert dash["total"] == 1


def _identity_snapshot():
    n1 = seed_node("身份")
    seed_states()
    mark(n1, ["standard"])
    make_run()
    first = final_service.snapshot("manual")
    return n1, first


def _mutate_identity(node_id: int, mutation: str):
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with db.transaction() as conn:
        if mutation == "node_name":
            conn.execute("UPDATE np_nodes SET current_name=? WHERE id=?", ("身份新名", node_id))
        elif mutation == "server":
            conn.execute("UPDATE np_nodes SET server=? WHERE id=?", ("10.9.8.7", node_id))
        elif mutation == "port":
            conn.execute("UPDATE np_nodes SET port=? WHERE id=?", (8443, node_id))
        elif mutation == "airport_name":
            aid = conn.execute("SELECT airport_id FROM np_nodes WHERE id=?", (node_id,)).fetchone()[0]
            conn.execute("UPDATE airports SET name=? WHERE id=?", ("身份新机场", aid))
        elif mutation == "airport_id":
            aid = conn.execute(
                """INSERT INTO airports(name,url,enabled,interval_minutes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                ("身份第二机场", "https://identity-second.example/sub", 1, 60, stamp, stamp),
            ).lastrowid
            conn.execute("UPDATE np_nodes SET airport_id=? WHERE id=?", (int(aid), node_id))
        else:
            raise AssertionError(mutation)


@pytest.mark.parametrize(
    ("mutation", "expected_fields"),
    [
        ("node_name", {"node_name"}),
        ("server", {"server"}),
        ("port", {"port"}),
        ("airport_name", {"airport_name"}),
        ("airport_id", {"airport_id", "airport_name"}),
    ],
)
def test_saved_node_identity_change_creates_version_without_add_remove(mutation, expected_fields):
    node_id, first = _identity_snapshot()
    old_id = first["version"]["id"]
    old_version = final_service.version(old_id)
    old_node = next(c for c in old_version["categories"] if c["code"] == "standard")["nodes"][0]
    assert old_node["node_id"] == node_id
    assert old_node["node_name"] == "身份"
    assert old_node["server"] == "127.0.0.1"
    assert old_node["port"] == 443
    assert old_node["airport_name"] == "身份机场"

    _mutate_identity(node_id, mutation)

    # Same P5 run: an identity change must still create a version.
    changed = final_service.auto_snapshot()
    assert changed["created"] is True
    assert changed["version"]["previous_version_id"] == old_id
    assert history_count() == 2

    diff = final_service.diff(changed["version"]["id"])
    standard = next(c for c in diff["categories"] if c["code"] == "standard")
    assert [n["node_id"] for n in standard["added"]] == []
    assert [n["node_id"] for n in standard["removed"]] == []
    assert [n["node_id"] for n in standard["retained"]] == [node_id]
    retained = standard["retained"][0]
    assert {item["field"] for item in retained["changed_fields"]} == expected_fields
    assert retained["before"]["node_id"] == retained["after"]["node_id"] == node_id

    new_node = next(c for c in final_service.current()["categories"] if c["code"] == "standard")["nodes"][0]
    assert new_node["node_id"] == node_id
    if mutation == "node_name":
        assert new_node["node_name"] == "身份新名"
    elif mutation == "server":
        assert new_node["server"] == "10.9.8.7"
    elif mutation == "port":
        assert new_node["port"] == 8443
    elif mutation == "airport_name":
        assert new_node["airport_name"] == "身份新机场"
    else:
        assert new_node["airport_id"] != old_node["airport_id"]
        assert new_node["airport_name"] == "身份第二机场"

    # The old snapshot keeps the original identity; it is not rewritten in place.
    still_old = final_service.version(old_id)
    original = next(c for c in still_old["categories"] if c["code"] == "standard")["nodes"][0]
    assert original == old_node

    # No further identity change: repeated snapshots stay unchanged.
    for _ in range(3):
        again = final_service.snapshot("manual")
        assert again["created"] is False
        assert again["reason"] == "unchanged"
    assert history_count() == 2


def test_legacy_membership_signature_does_not_force_duplicate_version():
    """Upgrading v2.044 membership signatures must not create a duplicate."""
    import hashlib

    n1 = seed_node("旧签")
    seed_states()
    mark(n1, ["standard"])
    make_run()
    first = final_service.snapshot("manual")
    version_id = first["version"]["id"]

    legacy = {code: [] for code in CATEGORIES}
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT category,node_id FROM np6_entries WHERE version_id=?",
            (version_id,),
        ).fetchall()
        for item in row:
            legacy[item["category"]].append(int(item["node_id"]))
        for code in legacy:
            legacy[code].sort()
        import json
        legacy_hash = hashlib.sha256(
            json.dumps(legacy, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        detail = json.loads(conn.execute(
            "SELECT detail_json FROM np6_versions WHERE id=?", (version_id,)
        ).fetchone()[0])
        detail.pop("signature_version", None)
        conn.execute(
            "UPDATE np6_versions SET signature=?, detail_json=? WHERE id=?",
            (legacy_hash, json.dumps(detail, ensure_ascii=False, separators=(",", ":")), version_id),
        )

    unchanged = final_service.snapshot("manual")
    assert unchanged["created"] is False
    assert unchanged["reason"] == "unchanged"
    assert history_count() == 1

    # A real identity change after the legacy signature still creates a version.
    with db.transaction() as conn:
        conn.execute("UPDATE np_nodes SET current_name=? WHERE id=?", ("旧签新名", n1))
    upgraded = final_service.snapshot("manual")
    assert upgraded["created"] is True
    assert history_count() == 2
    retained = next(
        c for c in final_service.diff(upgraded["version"]["id"])["categories"]
        if c["code"] == "standard"
    )["retained"][0]
    assert [item["field"] for item in retained["changed_fields"]] == ["node_name"]
