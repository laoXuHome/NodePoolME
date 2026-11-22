"""Per-airport keyword filtering: no pool entry, no test, no publish; manual admission sticks."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-filter-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend import config  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.services import fetcher  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def _node(name: str, fp: str) -> dict:
    return {"raw_name": name, "protocol": "ss", "server": "1.1.1.1", "port": 443,
            "fingerprint": fp, "params": {"cipher": "aes-128-gcm", "password": "pw"},
            "region": "HK"}


def _seed_latency(aid: int) -> None:
    for node in db.rows("SELECT id FROM nodes WHERE airport_id=?", (aid,)):
        for target in ("G", "W"):
            db.execute(
                "INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,"
                "error_type,message,ts) VALUES (?,?,?,?,?,?,?,?)",
                (node["id"], target, None, 1, 100, None, "", db.now()))


def _upsert_three(aid: int, keywords: list[str]):
    return fetcher._upsert(aid, [
        _node("正常香港 01", f"good-{aid}"),
        _node("官网公告 请勿购买", f"official-{aid}"),
        _node("剩余流量 10G", f"traffic-{aid}"),
    ], keywords)


def test_airport_filter_api_flow():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        res = client.post("/api/airports", json={"name": "过滤机场", "url": "http://example.com/sub"})
        assert res.status_code == 200, res.text
        aid = res.json()["id"]
        assert res.json()["filter_keywords"] == config.DEFAULT_FILTER_KEYWORDS

        stats = _upsert_three(aid, config.DEFAULT_FILTER_KEYWORDS)
        assert stats["total"] == 1 and stats["filtered"] == 2
        _seed_latency(aid)
        assert [r["raw_name"] for r in fetcher.filtered_nodes(aid)] == [
            "官网公告 请勿购买", "剩余流量 10G"]
        assert client.get("/api/nodes").json()["total"] == 1
        assert len(builder.collect_nodes()[0]) == 1

        detail = client.get(f"/api/airports/{aid}/filter").json()
        assert len(detail["nodes"]) == 2
        assert detail["keywords"] == config.DEFAULT_FILTER_KEYWORDS
        assert detail["default_keywords"] == config.DEFAULT_FILTER_KEYWORDS

        # Narrowing keywords reclassifies cached nodes without a re-fetch.
        assert client.patch(f"/api/airports/{aid}", json={"filter_keywords": ["官网"]}).status_code == 200
        assert client.get("/api/nodes").json()["total"] == 2
        assert [r["raw_name"] for r in fetcher.filtered_nodes(aid)] == ["官网公告 请勿购买"]
        _seed_latency(aid)
        published = [n["raw_name"] for n in builder.collect_nodes()[0]]
        assert "官网公告 请勿购买" not in published and len(published) == 2

        # Manually admit one filtered node; it enters the pool and stays there.
        fp = fetcher.filtered_nodes(aid)[0]["fingerprint"]
        admitted = client.post(f"/api/airports/{aid}/filter/admit", json={"fingerprint": fp})
        assert admitted.status_code == 200, admitted.text
        assert admitted.json()["node_count"] == 3
        assert fetcher.filtered_nodes(aid) == []
        assert client.get("/api/nodes").json()["total"] == 3
        _seed_latency(aid)

        # Adding 流量 filters the traffic node again; the admitted official node stays.
        client.patch(f"/api/airports/{aid}", json={"filter_keywords": ["官网", "流量"]})
        assert client.get("/api/nodes").json()["total"] == 2
        assert [r["raw_name"] for r in fetcher.filtered_nodes(aid)] == ["剩余流量 10G"]

        # Back to 官网 only: the traffic node returns, admitted node still stays.
        client.patch(f"/api/airports/{aid}", json={"filter_keywords": ["官网"]})
        assert client.get("/api/nodes").json()["total"] == 3
        assert fetcher.filtered_nodes(aid) == []

        # Next fetch keeps the admission even though the name still matches.
        stats2 = _upsert_three(aid, ["官网"])
        assert stats2["total"] == 3 and stats2["filtered"] == 0
        _seed_latency(aid)
        assert len(builder.collect_nodes()[0]) == 3
        assert db.row("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=? AND raw_name LIKE '%官网%'",
                      (aid,))["c"] == 1

        # Clearing keywords releases every filtered node.
        client.patch(f"/api/airports/{aid}", json={"filter_keywords": []})
        assert client.get("/api/nodes").json()["total"] == 3
        assert fetcher.filtered_nodes(aid) == []


if __name__ == "__main__":
    test_airport_filter_api_flow()
    print("PASS test_airport_filter_api_flow")
