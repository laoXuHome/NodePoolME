"""Node names use ⁃ (U+2043) outside and ᐧ (U+1427) between tag idents."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-name-sep-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def test_separators():
    db.init_db()
    assert builder.NAME_SEP == "\u2043" and builder.TAG_SEP == "\u1427"
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('星链','http://x',30,datetime('now'),datetime('now'),1)")
    node = db.execute(
        "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
        "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (ap, "香港01", "ss", "1.1.1.1", 443,
         db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), "fp", "available", 100, 0))
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,"
                   "message,ts) VALUES (?,?,NULL,1,100,NULL,'',?)", (node, target, db.now()))
    for ident in ("G", "TG", "Y"):
        gid = db.row("SELECT id FROM test_groups WHERE ident=?", (ident,))["id"]
        db.execute("INSERT INTO results (node_id,group_id,batch_id,ok,latency_ms,error_type,message,ts) "
                   "VALUES (?,?,NULL,1,100,NULL,'',?)", (node, gid, db.now()))

    nodes, _ = builder.collect_nodes()
    assert len(nodes) == 1
    assert nodes[0]["display_name"] == "星链⁃GᐧTGᐧY⁃香港01", nodes[0]["display_name"]


if __name__ == "__main__":
    test_separators()
    print("PASS node name separators")
