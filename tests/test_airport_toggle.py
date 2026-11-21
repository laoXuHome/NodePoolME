"""Airport enable/disable controls pool membership, and re-enabling restores cache without a fetch."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-toggle-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.services import fetcher  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def _seed_airport(name: str, enabled: int) -> int:
    aid = db.execute(
        "INSERT INTO airports (name,url,enabled,interval_minutes,last_status,created_at,updated_at)"
        " VALUES (?,?,?,?,?,datetime('now'),datetime('now'))",
        (name, "http://x", enabled, 60, "ok"))
    node_id = db.execute(
        "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
        "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (aid, f"{name}-节点", "ss", "1.1.1.1", 443,
         db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), f"fp-{aid}", "available", 100, 0))
    db.execute(
        "INSERT INTO latency_results (node_id,batch_id,ok,latency_ms,error_type,message,ts)"
        " VALUES (?,?,?,?,?,?,?)", (node_id, None, 1, 100, None, "", db.now()))
    return aid


def test_disable_removes_from_pool_and_enable_restores():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        aid = _seed_airport("机场A", 1)

        assert client.get("/api/nodes").json()["total"] == 1
        assert client.get("/api/dashboard").json()["nodes_total"] == 1
        assert len(builder.collect_nodes()[0]) == 1

        # disable -> nodes leave the pool everywhere
        assert client.patch(f"/api/airports/{aid}", json={"enabled": False}).status_code == 200
        assert client.get("/api/nodes").json()["total"] == 0
        dash = client.get("/api/dashboard").json()
        assert dash["nodes_total"] == 0 and dash["nodes_available"] == 0
        assert builder.collect_nodes()[0] == []
        assert db.row("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?", (aid,))["c"] == 1  # cache kept

        # re-enable -> cached nodes come back, no fetch scheduled
        assert client.patch(f"/api/airports/{aid}", json={"enabled": True}).status_code == 200
        assert client.get("/api/nodes").json()["total"] == 1
        assert len(builder.collect_nodes()[0]) == 1
        airport = fetcher.airport_by_id(aid)
        assert airport["last_attempt_at"] is not None, "re-enable should defer the next automatic pull"
        assert aid not in {a["id"] for a in fetcher.due_airports()}


def test_manual_fetch_rejected_when_disabled():
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        aid = _seed_airport("机场B", 0)
        res = client.post(f"/api/airports/{aid}/fetch")
        assert res.status_code == 400
        assert "disabled" in res.json()["detail"]


if __name__ == "__main__":
    test_disable_removes_from_pool_and_enable_restores()
    print("PASS test_disable_removes_from_pool_and_enable_restores")
    test_manual_fetch_rejected_when_disabled()
    print("PASS test_manual_fetch_rejected_when_disabled")
