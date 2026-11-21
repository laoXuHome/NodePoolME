"""Regression: publish() must not shadow the module-level meta() helper."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-publish-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def test_publish_empty_pool_does_not_crash():
    db.init_db()
    first = builder.publish(reason="empty-1")
    assert first["nodes"] == 0 and "stats" in first
    second = builder.publish(reason="empty-2")          # hits the previous-meta branch too
    assert second["reason"] == "empty-2"
    assert isinstance(builder.meta(), dict)


def test_publish_selected_node():
    db.init_db()
    db.execute("UPDATE test_groups SET enabled=1")
    airport = db.execute(
        "INSERT INTO airports (name, url, interval_minutes, created_at, updated_at, enabled) "
        "VALUES ('t','http://x',30,datetime('now'),datetime('now'),1)")
    node_id = db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, params, fingerprint, "
        "status, latency, fail_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (airport, "n1", "ss", "1.1.1.1", 443, db.dumps({"cipher": "aes-128-gcm", "password": "pw"}),
         "fp1", "available", 120, 0))
    # Selection is gated by the latency probe; group rows only add ident segments
    for target in ("G", "W"):
        db.execute("INSERT INTO latency_results (node_id, target, batch_id, ok, latency_ms, error_type, message, ts) "
                   "VALUES (?,?,?,?,?,?,?,?)", (node_id, target, None, 1, 120, None, "", db.now()))
    groups = db.rows("SELECT id FROM test_groups WHERE enabled=1")
    for group in groups:
        db.execute("INSERT INTO results (node_id, group_id, batch_id, ok, latency_ms, error_type, message, ts) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   (node_id, group["id"], None, 1, 120, None, "", db.now()))
    result = builder.publish(reason="selected")
    assert result["nodes"] == 1
    clash = builder.read("clash")[1]
    assert "proxies:" in clash and "n1" in clash
    assert builder.meta()["nodes"] == 1


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
