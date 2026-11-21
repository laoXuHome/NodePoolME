"""Regression: every node-pool filter works and filters combine (AND)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-filters-")
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
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('A','http://x',30,datetime('now'),datetime('now'),1)")
    ap2 = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                     "VALUES ('B','http://x',30,datetime('now'),datetime('now'),1)")
    ids = {}
    for name, fp, air, lat, proto, region in (("hit-all", "f1", ap, 120, "ss", "HK"),
                                              ("hit-one", "f2", ap, 400, "vmess", "JP")):
        node = db.execute(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,region,fail_count,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (air, name, proto, "1.1.1.1", 443, db.dumps({"cipher": "aes-128-gcm", "password": "pw"}),
             fp, "available", lat, region, 0))
        ids[fp] = node
        db.execute("INSERT INTO latency_results (node_id,batch_id,ok,latency_ms,error_type,message,ts) "
                   "VALUES (?,NULL,1,?,NULL,'',?)", (node, lat, db.now()))
    g1 = db.row("SELECT id FROM test_groups WHERE ident='G'")["id"]
    g2 = db.row("SELECT id FROM test_groups WHERE ident='TG'")["id"]
    db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
               "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids["f1"], g1, db.now()))
    db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
               "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids["f1"], g2, db.now()))
    db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
               "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids["f2"], g1, db.now()))
    return ids, g1, g2, ap, ap2


def test_filters():
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        ids, g1, g2, ap, ap2 = _seed()
        db.set_setting("sub_max_latency", 500)
        db.set_setting("sub_required_tags", "G,TG")

        def names(**params):
            r = client.get("/api/nodes", params={k: v for k, v in params.items() if v not in (None, "")})
            assert r.status_code == 200, r.text
            return [i["raw_name"] for i in r.json()["items"]]

        assert names(airport_id=ap) == ["hit-all", "hit-one"]
        assert names(protocol="vmess") == ["hit-one"]
        assert names(region="HK") == ["hit-all"]
        assert names(q="hit-all") == ["hit-all"]
        assert names(group_id=g1, passed="1") == ["hit-all", "hit-one"]
        assert names(group_id=g2, passed="1") == ["hit-all"]
        assert names(group_id=g2, passed="miss") == ["hit-one"]
        assert names(tags="G") == ["hit-all", "hit-one"]
        assert names(tags="G,TG") == ["hit-all"]
        assert names(passed="untagged") == []
        assert names(passed="qualified") == ["hit-all", "hit-one"]
        # combined: publishable + airport + protocol
        assert names(publishable="1", airport_id=ap) == ["hit-all"]
        assert names(publishable="0") == ["hit-one"]
        assert names(publishable="1", protocol="ss") == ["hit-all"]
        # group_id also accepts the ident, and unknown groups must not crash
        assert names(group_id="TG", passed="1") == ["hit-all"]
        assert names(group_id="NOPE") == []
        assert names(tags="G,TG", group_id="TG", passed="1", publishable="1") == ["hit-all"]
        assert names(sort="tags") == ["hit-all", "hit-one"]
        assert names(limit="0") != []


if __name__ == "__main__":
    test_filters()
    print("PASS node filters")
