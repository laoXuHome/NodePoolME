"""Node pool API: publishable filter and human-readable publish reasons."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-nodes-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402


def _seed():
    airport = db.execute(
        "INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
        "VALUES ('星链','http://x',30,datetime('now'),datetime('now'),1)")
    ids = {}
    for name, fp in (("快", "fp-fast"), ("慢", "fp-slow"), ("未测", "fp-none")):
        ids[fp] = db.execute(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (airport, name, "ss", "1.1.1.1", 443,
             db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), fp, "available", 0, 0))
    for fp, latency in (("fp-fast", 180), ("fp-slow", 720)):
        for target in ("G", "W"):
            db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,message,ts) "
                       "VALUES (?,?,?,?,?,?,?,?)",
                       (ids[fp], target, None, 1, latency, None, "", db.now()))
    return ids


def test_filters_and_reasons():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        ids = _seed()
        db.set_setting("sub_max_latency", 500)
        db.set_setting("latency_test_enabled", "1")

        yes = client.get("/api/nodes?publishable=1").json()
        no = client.get("/api/nodes?publishable=0").json()
        assert yes["total"] == 1 and yes["items"][0]["publishable"] is True, yes
        assert yes["items"][0]["publish_reason"] == "下发"
        assert no["total"] == 2, no
        reasons = {i["raw_name"]: i["publish_reason"] for i in no["items"]}
        assert "未通过" in reasons["慢"] and "500" in reasons["慢"], reasons
        assert reasons["未测"] == "无可用延迟数据", reasons

        # selecting a tag group alone lists nodes that hit it (regression: str/int key mismatch)
        group_id = db.row("SELECT id FROM test_groups ORDER BY id LIMIT 1")["id"]
        db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
                   "VALUES (?,?,NULL,1,180,NULL,'',?)", (ids["fp-fast"], group_id, db.now()))
        hit = client.get(f"/api/nodes?group_id={group_id}").json()
        assert hit["total"] == 1 and hit["items"][0]["raw_name"] == "快", hit


if __name__ == "__main__":
    test_filters_and_reasons()
    print("PASS test_filters_and_reasons")
