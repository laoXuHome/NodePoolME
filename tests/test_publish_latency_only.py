"""Regression: the publish gate is the latency number, not node status."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-publish-status-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def test_unavailable_node_with_good_latency_is_published():
    db.init_db()
    airport = db.execute(
        "INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
        "VALUES ('A','http://x',30,datetime('now'),datetime('now'),1)")
    node = db.execute(
        "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
        "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (airport, "n1", "ss", "1.1.1.1", 443,
         db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), "fp1", "unavailable", 120, 9))
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id,target,batch_id,ok,latency_ms,error_type,message,ts) "
                   "VALUES (?,?,?,?,?,?,?,?)", (node, target, None, 1, 120, None, "", db.now()))
    db.set_setting("sub_max_latency", 500)
    db.set_setting("sub_max_latency2", 500)
    selected, stats = builder.collect_nodes()
    assert len(selected) == 1, (selected, stats)
    assert selected[0]["display_name"] == "A⁃n1" or "n1" in selected[0]["display_name"]


if __name__ == "__main__":
    test_unavailable_node_with_good_latency_is_published()
    print("PASS publish gate uses latency only")
