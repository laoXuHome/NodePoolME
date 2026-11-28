"""P3: five independent business probes, qualification and scheduling."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.business import service, store
from backend.database import db
from backend.nodepool import store as p1


@pytest.fixture(autouse=True)
def _reset_p3_runtime():
    service.reset_runtime_for_tests()
    yield
    service.reset_runtime_for_tests()


def _airport():
    return db.execute(
        "INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
        "VALUES ('P3机场','http://example.test/sub',30,datetime('now'),datetime('now'),1)"
    )


def _node(name="节点A", server="1.2.3.4"):
    return {
        "raw_name": name, "protocol": "vless", "server": server, "port": 443,
        "params": {"uuid": "uuid-p3", "network": "ws", "sni": "a.test"},
        "fingerprint": f"fp-{name}-{server}", "region": "HK",
    }


def _prepare_node(name="节点A"):
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        store.sync_states(conn)
    airport_id = _airport()
    with db.transaction() as conn:
        p1.ingest_fetch(conn, airport_id, [_node(name)])
        store.sync_states(conn)
    return p1.list_nodes()["items"][0]["id"]


def _stage(name, status="success", executed=True, latency=20, detail=None):
    return {"stage": name, "executed": executed, "ok": status == "success",
            "status": status if executed else "unexecuted", "latency_ms": latency,
            "slow": False, "detail": detail or {"reason": None}}


def _result(target, *, status="success", http_status=200):
    if status == "success":
        stages = [_stage("dns"), _stage("tcp", latency=120),
                  _stage("tls", latency=40), _stage("https", latency=80,
                                                    detail={"status": http_status})]
        reason = None
    elif status == "failure":
        stages = [_stage("dns"), _stage("tcp"), _stage("tls"),
                  _stage("https", "failure", detail={"status": http_status})]
        reason = "business_http_mismatch"
    else:
        stages = [_stage("dns", "unknown", detail={"reason": "dns_timeout"}),
                  _stage("tcp", "unexecuted", False), _stage("tls", "unexecuted", False),
                  _stage("https", "unexecuted", False)]
        reason = "dns_timeout"
    return {
        "target_id": target["id"], "target_name": target["name"], "target_url": target["url"],
        "status": status, "reason": reason, "available": status != "unknown",
        "https_status": http_status if status != "unknown" else None,
        "https_ms": 80 if status != "unknown" else None,
        "tls_ms": 40 if status != "unknown" else None,
        "stages": stages, "duration_ms": 600,
        "started_at": "2026-11-28 10:00:00", "finished_at": "2026-11-28 10:00:01",
    }


def _apply(node_id, business, *, status="success", http_status=200):
    with db.transaction() as conn:
        target = store.target(business, conn=conn)
        check_id = store.create_check(conn, node_id, business, target, "manual")
    row = {"node_id": int(node_id), "business": business, "queue_id": 0}
    return service.record_check(check_id, row, _result(target, status=status,
                                                       http_status=http_status), target)


def test_default_five_targets_and_expected_status():
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    targets = store.list_targets()
    assert [t["business"] for t in targets] == ["gpt", "youtube", "telegram", "github", "global"]
    assert all(t["enabled"] for t in targets)
    assert all(t["url"].startswith("https://") for t in targets)
    assert targets[0]["expected_status"] == "200-399"
    assert 200 in targets[0]["expected"] and 403 not in targets[0]["expected"]


def test_two_successes_qualify_and_businesses_are_independent():
    node_id = _prepare_node("资格节点")
    _apply(node_id, "gpt")
    assert store.get_state(node_id, "gpt")["state"] == "pending"
    _apply(node_id, "gpt")
    assert store.get_state(node_id, "gpt")["state"] == "qualified"
    _apply(node_id, "youtube")
    assert store.get_state(node_id, "youtube")["state"] == "pending"
    assert store.get_state(node_id, "gpt")["state"] == "qualified"
    _apply(node_id, "youtube", status="failure", http_status=403)
    assert store.get_state(node_id, "gpt")["state"] == "qualified"
    assert store.get_state(node_id, "youtube")["state"] == "pending"


def test_single_and_double_failure_keep_qualification_third_revokes():
    node_id = _prepare_node("撤销节点")
    _apply(node_id, "github")
    _apply(node_id, "github")
    assert store.get_state(node_id, "github")["state"] == "qualified"
    _apply(node_id, "github", status="failure", http_status=403)
    assert store.get_state(node_id, "github")["state"] == "qualified"
    _apply(node_id, "github", status="failure", http_status=403)
    assert store.get_state(node_id, "github")["state"] == "qualified"
    _apply(node_id, "github", status="failure", http_status=403)
    state = store.get_state(node_id, "github")
    assert state["state"] == "suspended"
    assert state["consecutive_failures"] == 3
    events = [e["event"] for e in store.events_for(node_id, "github")]
    assert "qualification_granted" in events
    assert "qualification_suspended" in events


def test_unknown_does_not_count_or_overwrite_qualified_state():
    node_id = _prepare_node("未知节点")
    _apply(node_id, "telegram")
    _apply(node_id, "telegram")
    before = store.get_state(node_id, "telegram")
    _apply(node_id, "telegram", status="unknown")
    after = store.get_state(node_id, "telegram")
    assert after["state"] == "qualified"
    assert after["consecutive_successes"] == before["consecutive_successes"]
    assert after["consecutive_failures"] == 0
    assert after["last_classification"] == "unknown"


def test_expired_qualification_is_not_active_and_is_queued_again():
    node_id = _prepare_node("过期节点")
    _apply(node_id, "global")
    _apply(node_id, "global")
    with db.transaction() as conn:
        store.update_settings(valid_hours_value=1, conn=conn)
        conn.execute("UPDATE np3_state SET last_valid_at='2020-01-01 00:00:00', next_due_at=? "
                     "WHERE node_id=? AND business='global'", ("2020-01-01 00:05:00", node_id))
        store.enqueue_due(conn, limit=20)
    assert store.states_for_node(node_id)["global"]["expired"] is True
    assert store.states_for_node(node_id)["global"]["qualification_active"] is False
    assert db.row("SELECT COUNT(*) c FROM np3_queue WHERE node_id=? AND business='global'",
                  (node_id,))["c"] == 1


def test_queue_is_unique_per_node_and_business_but_allows_five_businesses():
    node_id = _prepare_node("队列节点")
    first = service.enqueue_manual([node_id], business="gpt")
    second = service.enqueue_manual([node_id], business="gpt")
    all_five = service.enqueue_manual([node_id])
    assert first["queued"] == 1
    assert second["queued"] == 0
    assert all_five["queued"] == 4
    rows = db.rows("SELECT business,status FROM np3_queue WHERE node_id=? ORDER BY business",
                   (node_id,))
    assert len(rows) == 5
    assert len({r["business"] for r in rows}) == 5


def test_target_outage_makes_all_businesses_unknown_without_node_failure(monkeypatch):
    node_id = _prepare_node("目标故障节点")
    service.enqueue_manual([node_id])

    async def fake_target(target, deadline_seconds=20):
        return _result(target, status="unknown")

    async def fake_prepare(nodes):
        return {int(n["id"]): 31001 for n in nodes}

    async def fake_node(target, proxy_port, deadline_seconds=20):
        raise AssertionError("target outage must not probe the node")

    monkeypatch.setattr(service, "target_probe", fake_target)
    monkeypatch.setattr(service, "prepare_ports", fake_prepare)
    monkeypatch.setattr(service, "node_probe", fake_node)

    async def run():
        result = await service.tick()
        assert result["started"] is True
        await service.wait_batch()
    asyncio.run(run())
    for code in store.BUSINESSES:
        state = store.get_state(node_id, code)
        assert state["state"] == "unknown"
        assert state["consecutive_failures"] == 0


def test_batch_runs_through_injected_probe_and_preserves_p1_and_p2(monkeypatch):
    node_id = _prepare_node("业务联动节点")
    before_p1 = p1.detail(node_id)
    before_p2 = db.row("SELECT base_health,management_layer,competition_state FROM np_nodes WHERE id=?",
                       (node_id,))
    service.enqueue_manual([node_id], business="youtube")

    async def fake_target(target, deadline_seconds=20):
        return _result(target)

    async def fake_prepare(nodes):
        return {int(n["id"]): 31002 for n in nodes}

    seen = {}

    async def fake_node(target, proxy_port, deadline_seconds=20):
        seen["port"] = proxy_port
        return _result(target)

    monkeypatch.setattr(service, "target_probe", fake_target)
    monkeypatch.setattr(service, "prepare_ports", fake_prepare)
    monkeypatch.setattr(service, "node_probe", fake_node)

    async def run():
        result = await service.tick()
        assert result["started"] is True
        await service.wait_batch()
    asyncio.run(run())
    state = store.get_state(node_id, "youtube")
    assert state["state"] == "pending" and state["consecutive_successes"] == 1
    assert seen["port"] == 31002, "业务检测必须走节点自己的代理出口端口"
    after_p1 = p1.detail(node_id)
    after_p2 = db.row("SELECT base_health,management_layer,competition_state FROM np_nodes WHERE id=?",
                      (node_id,))
    assert after_p1["base_health"] == before_p1["base_health"]
    assert after_p1["competition_state"] == before_p1["competition_state"]
    assert dict(after_p2) == dict(before_p2)


def test_config_change_only_invalidates_that_business():
    node_id = _prepare_node("配置节点")
    _apply(node_id, "gpt")
    _apply(node_id, "gpt")
    _apply(node_id, "youtube")
    assert store.get_state(node_id, "gpt")["state"] == "qualified"
    target = store.update_target("gpt", url="https://chatgpt.com/robots.txt")
    assert target["config_version"] == 2
    assert store.get_state(node_id, "gpt")["state"] == "untested"
    assert store.get_state(node_id, "youtube")["state"] == "pending"


def test_business_api_summary_nodes_settings():
    from fastapi.testclient import TestClient
    from backend.api import auth
    from backend.main import app

    node_id = _prepare_node("接口业务节点")
    app.dependency_overrides[auth.require_auth] = lambda: {"user": "admin"}
    try:
        with TestClient(app) as client:
            summary = client.get("/api/business/summary").json()
            assert len(summary["businesses"]) == 5
            assert summary["queue"]["total"] == 0
            nodes = client.get("/api/business/nodes").json()
            assert nodes["total"] == 1 and nodes["items"][0]["id"] == node_id
            assert set(nodes["items"][0]["businesses"].keys()) == set(store.BUSINESSES)
            detail = client.get(f"/api/business/nodes/{node_id}").json()
            assert set(detail["businesses"].keys()) == set(store.BUSINESSES)
            assert detail["businesses"]["gpt"]["target_enabled"] is True
            r = client.patch("/api/business/settings", json={"valid_hours": 24, "interval_minutes": 360})
            assert r.status_code == 200 and r.json()["valid_hours"] == 24
            r = client.post(f"/api/business/nodes/{node_id}/check", json={"business": "gpt"})
            assert r.status_code == 200 and r.json()["queued"] == 1
            bad = client.post(f"/api/business/nodes/{node_id}/check", json={"business": "nope"})
            assert bad.status_code == 400
            r = client.patch("/api/business/targets/gpt", json={"expected_status": "200-399"})
            assert r.status_code == 200
            restored = client.post("/api/business/targets/defaults").json()["items"]
            assert [t["business"] for t in restored] == list(store.BUSINESSES)
    finally:
        app.dependency_overrides.pop(auth.require_auth, None)


def test_suspended_business_requalifies_after_two_successes():
    node_id = _prepare_node("恢复节点")
    _apply(node_id, "telegram")
    _apply(node_id, "telegram")
    for _ in range(3):
        _apply(node_id, "telegram", status="failure", http_status=403)
    assert store.get_state(node_id, "telegram")["state"] == "suspended"
    _apply(node_id, "telegram")
    assert store.get_state(node_id, "telegram")["state"] == "pending"
    _apply(node_id, "telegram")
    state = store.get_state(node_id, "telegram")
    assert state["state"] == "qualified"
    assert state["consecutive_successes"] == 2
    assert any(e["event"] == "qualification_recovered"
               for e in store.events_for(node_id, "telegram"))


def test_batch_abort_releases_queue_and_checks(monkeypatch):
    node_id = _prepare_node("中断节点")
    service.enqueue_manual([node_id], business="global")

    def boom(*args, **kwargs):
        raise RuntimeError("injected batch abort")

    monkeypatch.setattr(store, "create_check", boom)

    async def run():
        result = await service.tick()
        assert result["started"] is True
        await service.wait_batch()
    asyncio.run(run())
    rows = db.rows("SELECT status FROM np3_queue WHERE node_id=? AND business='global'",
                   (node_id,))
    assert [r["status"] for r in rows] == ["error"]
