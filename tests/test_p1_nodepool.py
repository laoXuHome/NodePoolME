"""P1: three logical pools, two state axes, stable identity and audit trail."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.database import db
from backend.nodepool import store
from backend.services import fetcher


def _airport(name="测试机场", url="http://example.test/sub"):
    return db.execute(
        "INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
        "VALUES (?,?,30,datetime('now'),datetime('now'),1)", (name, url))


def _node(name, server="1.2.3.4", port=443, uuid="uuid-1", protocol="vless"):
    return {
        "raw_name": name, "protocol": protocol, "server": server, "port": port,
        "params": {"uuid": uuid, "network": "ws", "path": "/ws", "sni": "a.test"},
        "fingerprint": f"fp-{name}-{server}-{port}",
        "region": "HK",
    }


def _ingest(airport_id, nodes):
    with db.transaction() as conn:
        return store.ingest_fetch(conn, airport_id, nodes)


def test_new_nodes_are_in_raw_and_observation_and_identity_survives_rename():
    airport_id = _airport()
    first = _node("香港 01")
    second = _node("日本 02", server="5.6.7.8", uuid="uuid-2")
    _ingest(airport_id, [first, second])
    s = store.summary()
    assert s["raw_total"] == 2 and s["observation"] == 2 and s["healthy"] == 0
    assert s["raw_total"] == s["healthy"] + s["observation"] + s["raw_only"]
    assert s["airports"][0]["raw_total"] == 2 and s["airports"][0]["observation"] == 2
    items = store.list_nodes()["items"]
    node = next(i for i in items if i["current_name"] == "香港 01")
    old_id = node["id"]

    # Same endpoint, renamed by the airport: identity must not change.
    _ingest(airport_id, [{**first, "raw_name": "香港 01 专线"}, second])
    assert store.summary()["raw_total"] == 2
    renamed = next(i for i in store.list_nodes()["items"] if i["current_name"] == "香港 01 专线")
    assert renamed["id"] == old_id
    d = store.detail(old_id)
    assert [n["name"] for n in d["names"]] == ["香港 01", "香港 01 专线"]
    assert d["first_name"] == "香港 01"
    assert any(e["kind"] == "name_changed" for e in d["events"])

    # Repeated identical fetch is idempotent, not a duplicate.
    _ingest(airport_id, [{**first, "raw_name": "香港 01 专线"}, second])
    assert store.summary()["raw_total"] == 2


def test_health_gate_and_layer_transitions_never_delete():
    airport_id = _airport()
    _ingest(airport_id, [_node("节点A")])
    node_id = store.list_nodes()["items"][0]["id"]

    d = store.set_health(node_id, health="healthy", data_status="sufficient")
    assert d["management_layer"] == "healthy"
    assert d["competition_state"] == "candidate"
    assert d["base_health"] == "healthy" and d["data_status"] == "sufficient"
    assert store.summary()["healthy"] == 1

    # Lost health -> observation, record retained.
    d = store.set_health(node_id, health="unhealthy", data_status="sufficient",
                         reason="TLS 失败")
    assert d["management_layer"] == "observation"
    assert d["competition_state"] == "observation"
    assert d["observation_reason"] == "TLS 失败"
    assert store.summary()["raw_total"] == 1
    assert store.summary()["healthy"] + store.summary()["observation"] + store.summary()["raw_only"] == 1

    # Insufficient data is observation even if the probe said healthy.
    d = store.set_health(node_id, health="healthy", data_status="insufficient")
    assert d["management_layer"] == "observation"
    assert d["observation_reason"] == "insufficient_data"

    # Recovery is a normal state update, not a new node.
    d = store.set_health(node_id, health="healthy", data_status="sufficient")
    assert d["management_layer"] == "healthy"
    assert store.summary()["raw_total"] == 1

    # Manual retirement keeps the row and its source/name history.
    d = store.set_layer(node_id, layer="raw", reason="历史归档")
    assert d["management_layer"] == "raw"
    assert d["base_health"] == "healthy"
    assert store.detail(node_id)["names"][0]["name"] == "节点A"
    assert store.summary()["raw_only"] == 1

    # Cannot force healthy without evidence.
    store.set_health(node_id, health="unknown", data_status="unknown")
    try:
        store.set_layer(node_id, layer="healthy")
    except store.NodePoolError as exc:
        assert exc.code == "not_health_eligible"
    else:
        raise AssertionError("unqualified node was admitted to healthy pool")


def test_source_removal_marks_historical_and_reappearance_recovers_for_observation():
    airport_id = _airport()
    a, b = _node("A"), _node("B", server="9.9.9.9", uuid="uuid-b")
    _ingest(airport_id, [a, b])
    assert store.summary()["healthy"] + store.summary()["observation"] + store.summary()["raw_only"] == 2
    id_a = next(i["id"] for i in store.list_nodes()["items"] if i["current_name"] == "A")
    id_b_original = next(i["id"] for i in store.list_nodes()["items"] if i["current_name"] == "B")
    store.set_health(id_a, health="healthy", data_status="sufficient")
    _ingest(airport_id, [a])  # B disappears from the subscription
    d_b = next(i for i in store.list_nodes()["items"] if i["current_name"] == "B")
    assert d_b["source_active"] is False
    assert d_b["management_layer"] == "observation"
    assert d_b["observation_reason"] == "source_missing"
    assert store.summary()["historical"] == 1
    assert store.summary()["raw_total"] == 2

    # Reappearance keeps the same identity and returns to observation for re-check.
    _ingest(airport_id, [a, b])
    d_b = next(i for i in store.list_nodes()["items"] if i["current_name"] == "B")
    assert d_b["id"] == id_b_original
    assert d_b["source_active"] is True
    assert d_b["management_layer"] == "observation"
    assert any(e["kind"] == "source_returned" for e in store.detail(d_b["id"])["events"])


def test_eliminated_node_keeps_raw_record_and_multiple_qualifications():
    airport_id = _airport()
    _ingest(airport_id, [_node("节点C")])
    node_id = store.list_nodes()["items"][0]["id"]
    store.set_health(node_id, health="healthy", data_status="sufficient")
    d = store.set_competition(node_id, state="eliminated", reason="质量下降")
    assert d["competition_state"] == "eliminated"
    assert d["management_layer"] == "healthy"  # base health stays a separate axis
    assert store.summary()["raw_total"] == 1
    d = store.grant_qualification(node_id, "streaming", reason="流媒体可用")
    d = store.grant_qualification(node_id, "download", reason="下载可用")
    assert {q["qualification"] for q in d["qualifications"] if q["active"]} == {"streaming", "download"}
    d = store.revoke_qualification(node_id, "streaming", reason="失效")
    active = {q["qualification"] for q in d["qualifications"] if q["active"]}
    assert active == {"download"}
    # Elimination did not remove the node; later health loss only moves layer.
    d = store.set_health(node_id, health="unhealthy", data_status="sufficient")
    assert d["management_layer"] == "observation"
    assert d["competition_state"] == "eliminated"
    assert store.summary()["raw_total"] == 1


def test_competition_requires_healthy_layer_and_filters_are_queryable():
    airport_id = _airport()
    _ingest(airport_id, [_node("HK-01"), _node("JP-02", server="8.8.8.8", uuid="u2")])
    try:
        store.set_competition(store.list_nodes()["items"][0]["id"], state="preferred")
    except store.NodePoolError as exc:
        assert exc.code == "not_competition_eligible"
    else:
        raise AssertionError("observation node was marked preferred")

    node_id = next(i["id"] for i in store.list_nodes()["items"] if i["current_name"] == "HK-01")
    store.set_health(node_id, health="healthy", data_status="sufficient")
    store.set_competition(node_id, state="preferred")
    by_layer = store.list_nodes(layer="healthy")
    assert by_layer["total"] == 1 and by_layer["items"][0]["id"] == node_id
    by_q = store.list_nodes(q="HK-01")
    assert by_q["total"] == 1
    by_q2 = store.list_nodes(q="JP-02")
    assert by_q2["total"] == 1
    by_comp = store.list_nodes(competition="preferred")
    assert by_comp["total"] == 1
    assert store.list_nodes(airport_id=airport_id, source_active=1)["total"] == 2


def test_fetcher_ingest_keeps_p1_record_after_cache_replacement():
    airport_id = _airport("采集机场")
    node = _node("采集节点")
    node["fingerprint"] = "fp-fetch"
    fetcher._upsert(airport_id, [node])
    assert db.row("SELECT COUNT(*) c FROM np_nodes")["c"] == 1
    node_id = store.list_nodes()["items"][0]["id"]

    fetcher._upsert(airport_id, [{**node, "raw_name": "采集节点-改名"}])
    assert db.row("SELECT COUNT(*) c FROM np_nodes")["c"] == 1
    d = store.detail(node_id)
    assert d["current_name"] == "采集节点-改名"
    assert len(d["names"]) == 2

    # The legacy cache replaces/deletes its rows on an empty fetch; P1 must not.
    fetcher._upsert(airport_id, [])
    assert db.row("SELECT COUNT(*) c FROM nodes WHERE airport_id=?", (airport_id,))["c"] == 0
    d = store.detail(node_id)
    assert d["source_active"] is False
    assert d["management_layer"] == "observation"
    assert store.summary()["raw_total"] == 1


def test_api_contract_and_error_codes():
    from fastapi.testclient import TestClient
    from backend.api import auth
    from backend.main import app

    airport_id = _airport("API机场")
    _ingest(airport_id, [_node("API节点")])
    node_id = store.list_nodes()["items"][0]["id"]
    app.dependency_overrides[auth.require_auth] = lambda: {"user": "admin"}
    try:
        with TestClient(app) as client:
            s = client.get("/api/nodepool/summary").json()
            assert s["raw_total"] == 1 and s["observation"] == 1
            assert client.get("/api/nodepool/nodes?layer=observation").json()["total"] == 1
            assert client.get(f"/api/nodepool/nodes/{node_id}").json()["id"] == node_id
            r = client.post(f"/api/nodepool/nodes/{node_id}/health",
                            json={"health": "healthy", "data_status": "sufficient"})
            assert r.status_code == 200 and r.json()["management_layer"] == "healthy"
            r = client.post(f"/api/nodepool/nodes/{node_id}/layer", json={"layer": "invalid"})
            assert r.status_code == 400 and r.json()["detail"]["code"] == "invalid_layer"
            r = client.post("/api/nodepool/nodes/999999/health",
                            json={"health": "healthy", "data_status": "sufficient"})
            assert r.status_code == 404
            r = client.post(f"/api/nodepool/nodes/{node_id}/competition",
                            json={"state": "preferred"})
            assert r.status_code == 200
            r = client.post(f"/api/nodepool/nodes/{node_id}/qualifications",
                            json={"qualification": "streaming"})
            assert r.status_code == 200
            r = client.delete(f"/api/nodepool/nodes/{node_id}/qualifications/streaming")
            assert r.status_code == 200
    finally:
        app.dependency_overrides.pop(auth.require_auth, None)
