"""Publish must require ALL selected tags (AND), zero tags keeps latency-only behaviour."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-tags-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def _seed():
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('A','http://x',30,datetime('now'),datetime('now'),1)")
    ids = {}
    for fp in ("one", "two"):
        ids[fp] = db.execute(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,fail_count,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (ap, fp, "ss", "1.1.1.1", 443,
             db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), fp, "available", 100, 0))
        for target in ("G", "W"):
            db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,message,ts) "
                       "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids[fp], target, db.now()))
    g1 = db.row("SELECT id FROM test_groups WHERE ident='G'")["id"]
    g2 = db.row("SELECT id FROM test_groups WHERE ident='TG'")["id"]
    db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
               "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids["one"], g1, db.now()))
    db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
               "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids["one"], g2, db.now()))
    db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
               "VALUES (?,?,NULL,1,100,NULL,'',?)", (ids["two"], g1, db.now()))


def test_required_tags():
    db.init_db()
    _seed()
    db.set_setting("sub_max_latency", 500)
    db.set_setting("sub_max_latency2", 500)
    db.set_setting("sub_required_tags", "")
    names = lambda: sorted(n["raw_name"] for n in builder.collect_nodes()[0])
    assert names() == ["one", "two"]
    db.set_setting("sub_required_tags", "G")
    assert names() == ["one", "two"]
    db.set_setting("sub_required_tags", "G,TG")
    assert names() == ["one"]
    db.set_setting("sub_required_tags", "TG")
    assert names() == ["one"]
    db.set_setting("sub_max_latency", 80)
    db.set_setting("sub_max_latency2", 80)
    assert names() == []


if __name__ == "__main__":
    test_required_tags()
    print("PASS required tags gate")
