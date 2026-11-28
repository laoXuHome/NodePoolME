"""P2: target management, layered probes, nearest-rank P80 and recovery."""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.database import db
from backend.health import prober, service, store
from backend.nodepool import store as p1


@pytest.fixture(autouse=True)
def _reset_p2_runtime():
    service.reset_runtime_for_tests()
    yield
    service.reset_runtime_for_tests()


def _airport():
    return db.execute(
        "INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
        "VALUES ('P2机场','http://example.test/sub',30,datetime('now'),datetime('now'),1)"
    )


def _node(name="节点A", server="1.2.3.4"):
    return {
        "raw_name": name, "protocol": "vless", "server": server, "port": 443,
        "params": {"uuid": "uuid-p2", "network": "ws", "sni": "a.test"},
        "fingerprint": f"fp-{name}-{server}", "region": "HK",
    }


def _prepare_node(name="节点A"):
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    airport_id = _airport()
    with db.transaction() as conn:
        p1.ingest_fetch(conn, airport_id, [_node(name)])
    with db.transaction() as conn:
        store.sync_states(conn)
    node = p1.list_nodes()["items"][0]
    return node["id"]


def _targets():
    return store.list_targets(enabled_only=True)


def _stage(name, status="success", executed=True):
    return {"stage": name, "executed": executed, "ok": status == "success",
            "status": status if executed else "unexecuted", "latency_ms": 10,
            "slow": False, "detail": {"reason": None}}


def _result(target, *, status="success", p80=120, health_pass=True, samples=None,
            reason=None):
    stages = [_stage("dns"), _stage("tcp"), _stage("tls"), _stage("https")]
    if status == "failure":
        stages = [_stage("dns"), _stage("tcp", "failure"), _stage("tcp", "unexecuted", False),
                  _stage("https", "unexecuted", False)]
        reason = reason or "tcp_all_failed"
    elif status == "unknown":
        stages = [_stage("dns", "unknown"), _stage("tcp", "unexecuted", False),
                  _stage("tls", "unexecuted", False), _stage("https", "unexecuted", False)]
        reason = reason or "dns_timeout"
    if samples is None:
        samples = [
            {"index": i, "ok": True, "latency_ms": v, "error": None,
             "sampled_at": "2026-11-28 10:00:00"}
            for i, v in enumerate([100, 110, 120, 130, 140])
        ]
    return {
        "target_id": target["id"], "target_name": target["name"],
        "target_url": target["url"], "status": status, "reason": reason,
        "p80_ms": p80 if status != "unknown" else None, "tcp_valid_count": 5,
        "tls_ms": 20, "https_ms": 30, "https_status": 200,
        "health_pass": health_pass, "available": status != "unknown",
        "stages": stages, "tcp_samples": samples,
        "duration_ms": 500, "started_at": "2026-11-28 10:00:00",
        "finished_at": "2026-11-28 10:00:01", "p80_grade": prober.tcp_grade(p80),
    }


def _round(node_id, *, status="success", p80=120, health_pass=True,
           trigger="normal", unknown_targets=None):
    targets = _targets()
    version = store.config_version()
    check = store.create_check(node_id, trigger, version, len(targets))
    results = []
    for target in targets:
        if unknown_targets and target["id"] in unknown_targets:
            results.append(_result(target, status="unknown", p80=None, health_pass=False))
        else:
            results.append(_result(target, status=status, p80=p80,
                                   health_pass=health_pass, samples=[
                                       {"index": i, "ok": True, "latency_ms": v,
                                        "error": None, "sampled_at": "2026-11-28 10:00:00"}
                                       for i, v in enumerate(
                                           [p80 - 40, p80 - 30, p80 - 20, p80, p80 + 10]
                                       )
                                   ]))
    return service.record_round(check["id"], {"id": node_id, "queue_id": 0}, results)


# ---------------------------------------------------------------- target management
def test_default_google_wikipedia_and_minimum_two_enabled_targets():
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    targets = store.list_targets()
    assert [t["name"] for t in targets] == ["Google", "Wikipedia"]
    assert all(t["enabled"] for t in targets)
    google = targets[0]
    assert google["host"] == "www.google.com" and google["secure"] is True

    with pytest.raises(store.HealthError) as exc:
        store.update_target(google["id"], enabled=False)
    assert exc.value.code == "min_targets"
    with db.transaction() as conn:
        with pytest.raises(store.HealthError):
            store.delete_target(targets[1]["id"], conn=conn)


def test_target_crud_invalidates_old_generation_and_restores_defaults():
    node_id = _prepare_node()
    targets = _targets()
    assert store.config_version() == 1
    store.update_target(targets[0]["id"], url="https://www.google.com/favicon.ico")
    assert store.config_version() == 2
    state = store.get_state(node_id)
    assert state["state"] == "unknown" and int(state["consecutive_failures"]) == 0
    detail = p1.detail(node_id)
    assert detail["management_layer"] == "observation"
    assert detail["base_health"] == "unknown"
    assert detail["observation_reason"] == "probe_config_changed"

    store.restore_defaults()
    assert store.config_version() == 3
    assert [t["name"] for t in store.list_targets()] == ["Google", "Wikipedia"]


# ---------------------------------------------------------------- P80 / stages
def test_p80_is_nearest_rank_fourth_of_five_not_mean_or_max():
    assert prober.p80_nearest_rank([900, 100, 400, 200, 300]) == 400
    assert prober.p80_nearest_rank([100]) == 100
    assert prober.p80_nearest_rank([]) is None
    # mean would be 380, max 900, median 300: only the fourth value is legal.
    assert prober.p80_nearest_rank([900, 100, 400, 200, 300]) != 380
    assert prober.p80_nearest_rank([900, 100, 400, 200, 300]) != 300


def test_tcp_stage_keeps_five_samples_and_uses_fourth(monkeypatch):
    values = iter([100, 900, 200, 400, 300])

    async def fake_connect(host, port, timeout):
        class Writer:
            def close(self):
                pass
        return object(), Writer(), next(values)
    monkeypatch.setattr(prober, "_direct_connect", fake_connect)
    monkeypatch.setattr(prober, "TCP_INTERVAL", 0)

    async def run():
        return await prober._tcp_samples(
            host="1.2.3.4", port=443, timeout=3, deadline=10**9, proxy_port=None
        )
    samples, stage = asyncio.run(run())
    assert len(samples) == 5
    assert [s["latency_ms"] for s in samples] == [100, 900, 200, 400, 300]
    assert stage["latency_ms"] == 400
    assert stage["detail"]["method"] == "nearest_rank"


def test_dns_failure_marks_later_stages_unexecuted(monkeypatch):
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    target = store.list_targets()[0]

    async def fake_dns(host, port, timeout):
        return ({"stage": "dns", "executed": True, "ok": False, "status": "unknown",
                 "latency_ms": 3000, "slow": False,
                 "detail": {"reason": "dns_timeout"}}, [], "dns_timeout")
    called = {"tcp": 0}

    async def boom(*args, **kwargs):
        called["tcp"] += 1
        raise AssertionError("TCP must not run after DNS failure")
    monkeypatch.setattr(prober, "_dns", fake_dns)
    monkeypatch.setattr(prober, "_tcp_samples", boom)
    result = asyncio.run(prober.probe_target(target))
    assert called["tcp"] == 0
    assert result["status"] == "unknown"
    stages = {s["stage"]: s for s in result["stages"]}
    assert stages["dns"]["status"] == "unknown"
    for name in ("tcp", "tls", "https"):
        assert stages[name]["executed"] is False
        assert stages[name]["status"] == "unexecuted"


# ---------------------------------------------------------------- attribution / P1
def test_single_failure_is_observation_then_three_failures_unhealthy_and_preserves_competition():
    node_id = _prepare_node()
    first = _round(node_id, status="success")
    assert first["state"]["state"] == "healthy"
    p1.set_competition(node_id, state="preferred", reason="P1 manual")

    one = _round(node_id, status="failure")
    assert one["state"]["state"] == "exception"
    assert one["state"]["consecutive_failures"] == 1
    detail = p1.detail(node_id)
    assert detail["management_layer"] == "observation"
    assert detail["competition_state"] == "preferred", "P2 must not change competition"
    assert detail["base_health"] != "unhealthy"

    two = _round(node_id, status="failure")
    assert two["state"]["consecutive_failures"] == 2
    assert p1.detail(node_id)["base_health"] != "unhealthy"

    three = _round(node_id, status="failure")
    assert three["state"]["state"] == "unhealthy"
    assert three["state"]["consecutive_failures"] == 3
    detail = p1.detail(node_id)
    assert detail["base_health"] == "unhealthy"
    assert detail["management_layer"] == "observation"
    assert detail["competition_state"] == "preferred"


def test_unknown_never_counts_as_failure_and_never_overwrites_health():
    node_id = _prepare_node()
    _round(node_id, status="success")
    for _ in range(3):
        state = _round(node_id, status="unknown")["state"]
        assert state["consecutive_failures"] == 0
    detail = p1.detail(node_id)
    assert detail["base_health"] == "healthy"
    assert detail["management_layer"] == "healthy"


def test_target_outage_makes_every_target_unknown_without_batch_kill():
    node_id = _prepare_node()
    _round(node_id, status="success")
    unknown_all = {t["id"] for t in _targets()}
    for _ in range(3):
        result = _round(node_id, unknown_targets=unknown_all)
        assert result["summary"]["status"] == "unknown"
        assert result["state"]["state"] == "healthy"
    assert p1.detail(node_id)["management_layer"] == "healthy"


def test_slow_observe_band_then_two_success_recovery():
    node_id = _prepare_node()
    slow = _round(node_id, p80=650, health_pass=False)
    assert slow["state"]["state"] == "delay"
    assert p1.detail(node_id)["management_layer"] == "observation"
    assert p1.detail(node_id)["base_health"] == "unknown"

    _round(node_id, p80=120, health_pass=True)  # a normal round leaves the delay state
    assert p1.detail(node_id)["management_layer"] == "healthy"

    # Three failures to unhealthy, then two valid successes to recover.
    for _ in range(3):
        _round(node_id, status="failure")
    assert store.get_state(node_id)["state"] == "unhealthy"
    first = _round(node_id, status="success")
    assert first["state"]["state"] == "recovering"
    assert first["state"]["consecutive_successes"] == 1
    assert p1.detail(node_id)["management_layer"] == "observation"
    second = _round(node_id, status="success")
    assert second["state"]["state"] == "healthy"
    assert p1.detail(node_id)["competition_state"] == "observation"
    assert p1.detail(node_id)["management_layer"] == "healthy"


def test_three_failure_threshold_exact_and_unknown_in_between(monkeypatch):
    node_id = _prepare_node()
    _round(node_id, status="failure")
    _round(node_id, status="unknown")
    second = _round(node_id, status="failure")
    assert second["state"]["consecutive_failures"] == 2
    _round(node_id, status="unknown")
    third = _round(node_id, status="failure")
    assert third["state"]["state"] == "unhealthy"
    assert third["state"]["consecutive_failures"] == 3


# ---------------------------------------------------------------- queue
def test_same_node_is_never_queued_twice():
    node_id = _prepare_node()
    with db.transaction() as conn:
        first = store.enqueue_nodes(conn, [node_id], trigger="manual", priority=0)
        second = store.enqueue_nodes(conn, [node_id], trigger="manual", priority=0)
    assert first == 1 and second == 0
    rows = db.rows("SELECT COUNT(*) AS n FROM np2_queue WHERE node_id=? AND status IN ('pending','running')",
                   (node_id,))
    assert rows[0]["n"] == 1


def test_check_round_persists_all_targets_and_five_raw_samples():
    node_id = _prepare_node()
    result = _round(node_id)
    check = store.check(result["check_id"])
    assert check["status"] == "success"
    detail = store.node_detail(node_id)
    assert len(detail["latest"]["targets"]) == 2
    for target in detail["latest"]["targets"]:
        assert len(target["tcp_samples"]) == 5
        assert target["p80_ms"] == 120
        stages = {s["stage"]: s for s in target["stages"]}
        assert stages["dns"]["executed"] and stages["https"]["executed"]
    assert detail["state"]["next_due_at"]


def test_config_change_discards_inflight_round(monkeypatch):
    node_id = _prepare_node()
    targets = _targets()
    check = store.create_check(node_id, "normal", store.config_version(), len(targets))
    store.update_target(targets[0]["id"], url="https://www.wikipedia.org/favicon.ico")
    with db.transaction() as conn:
        conn.execute("UPDATE np2_checks SET status='running' WHERE id=?", (check["id"],))
    result = service.record_round(
        check["id"], {"id": node_id, "queue_id": 0},
        [_result(t, status="success") for t in targets],
    )
    assert result["applied"] is False
    assert store.check(check["id"])["status"] == "unknown"
    assert store.get_state(node_id)["state"] == "unknown"


def test_p2_api_targets_nodes_and_settings():
    from fastapi.testclient import TestClient
    from backend.api import auth
    from backend.main import app

    node_id = _prepare_node("接口节点")
    app.dependency_overrides[auth.require_auth] = lambda: {"user": "admin"}
    try:
        with TestClient(app) as client:
            summary = client.get("/api/health/summary").json()
            assert len(summary["targets"]) == 2
            assert summary["enabled_targets"] == 2
            targets = client.get("/api/health/targets").json()["items"]
            assert [t["name"] for t in targets] == ["Google", "Wikipedia"]
            r = client.post("/api/health/targets", json={"name": "Backup", "url": "https://example.com/"})
            assert r.status_code == 200 and r.json()["id"]
            r = client.patch(f"/api/health/targets/{targets[0]['id']}", json={"enabled": False})
            assert r.status_code == 200  # still two enabled: Wikipedia + Backup
            r = client.patch(f"/api/health/targets/{targets[1]['id']}", json={"enabled": False})
            assert r.status_code == 400
            nodes = client.get("/api/health/nodes").json()
            assert nodes["total"] == 1 and nodes["items"][0]["id"] == node_id
            detail = client.get(f"/api/health/nodes/{node_id}").json()
            assert detail["node"]["id"] == node_id
            settings = client.get("/api/health/settings").json()
            assert settings["interval_minutes"] == 30
            assert client.patch("/api/health/settings", json={"interval_minutes": 60}).json()["interval_minutes"] == 60
            assert client.post(f"/api/health/nodes/{node_id}/check").json()["queued"] == 1
            restored = client.post("/api/health/targets/defaults").json()["items"]
            assert [t["name"] for t in restored] == ["Google", "Wikipedia"]
    finally:
        app.dependency_overrides.pop(auth.require_auth, None)


def test_tick_runs_one_batch_through_injected_probe_and_marks_healthy(monkeypatch):
    node_id = _prepare_node("调度节点")

    async def fake_target(target, deadline_seconds=20):
        return _result(target)

    async def fake_prepare(nodes):
        return {int(n["id"]): 31000 for n in nodes}

    async def fake_node(target, proxy_port, deadline_seconds=20):
        return _result(target, p80=130, health_pass=True)

    service.reset_runtime_for_tests()
    monkeypatch.setattr(service, "target_probe", fake_target)
    monkeypatch.setattr(service, "prepare_ports", fake_prepare)
    monkeypatch.setattr(service, "node_probe", fake_node)

    async def run():
        result = await service.tick()
        assert result["started"] is True
        await service.wait_batch()
        return result
    asyncio.run(run())
    state = store.get_state(node_id)
    assert state["state"] == "healthy"
    assert p1.detail(node_id)["management_layer"] == "healthy"
    assert store.node_detail(node_id)["latest"]["targets"][0]["p80_ms"] == 130


def _dns_ok():
    return {**_stage("dns"), "latency_ms": 18,
            "detail": {"reason": None, "address": "127.0.0.1"}}


def _tcp_ok(p80=112):
    return {"stage": "tcp", "executed": True, "ok": True, "status": "success",
            "latency_ms": p80, "slow": False,
            "detail": {"p80_ms": p80, "valid_count": 5, "method": "nearest_rank"}}


def test_probe_target_full_chain_wires_p80_into_result(monkeypatch):
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    target = store.list_targets()[0]

    async def fake_dns(host, port, timeout):
        return _dns_ok(), ["127.0.0.1"], None

    async def fake_tcp(**kwargs):
        return [{"index": i, "ok": True, "latency_ms": 100 + i * 3}
                for i in range(5)], _tcp_ok(112)

    async def fake_tls_https(**kwargs):
        return ({**_stage("tls"), "latency_ms": 42,
                 "detail": {"reason": None, "certificate": "valid"}},
                {**_stage("https"), "latency_ms": 88,
                 "detail": {"reason": None, "status": 204}})

    monkeypatch.setattr(prober, "_dns", fake_dns)
    monkeypatch.setattr(prober, "_tcp_samples", fake_tcp)
    monkeypatch.setattr(prober, "_tls_https", fake_tls_https)
    result = asyncio.run(prober.probe_target(target))
    assert result["status"] == "success"
    assert result["p80_ms"] == 112
    assert result["p80_grade"]["label"] == "优秀"
    assert result["tcp_samples"][3]["latency_ms"] == 109
    assert result["tls_ms"] == 42 and result["https_status"] == 204


def test_probe_target_dns_failure_short_circuits_without_combine_error(monkeypatch):
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    target = store.list_targets()[0]

    async def fake_dns(host, port, timeout):
        return ({**_stage("dns", "unknown"), "detail": {"reason": "dns_timeout"}},
                [], "dns_timeout")

    monkeypatch.setattr(prober, "_dns", fake_dns)
    result = asyncio.run(prober.probe_target(target))
    assert result["status"] == "unknown"
    assert [s["status"] for s in result["stages"]] == [
        "unknown", "unexecuted", "unexecuted", "unexecuted"
    ]
    assert result["p80_ms"] is None


def test_probe_target_tcp_failure_short_circuits_without_combine_error(monkeypatch):
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    target = store.list_targets()[0]

    async def fake_dns(host, port, timeout):
        return _dns_ok(), ["127.0.0.1"], None

    async def fake_tcp(**kwargs):
        return ([{"index": i, "ok": False, "latency_ms": None, "error": "refused"}
                 for i in range(5)],
                {**_stage("tcp", "failure"), "latency_ms": None,
                 "detail": {"reason": "tcp_all_failed", "p80_ms": None}})

    monkeypatch.setattr(prober, "_dns", fake_dns)
    monkeypatch.setattr(prober, "_tcp_samples", fake_tcp)
    result = asyncio.run(prober.probe_target(target))
    assert result["status"] == "failure"
    assert [s["status"] for s in result["stages"]] == [
        "success", "failure", "unexecuted", "unexecuted"
    ]
    assert result["p80_ms"] is None
