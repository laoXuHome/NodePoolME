"""Latency probe is the publish gate; test groups only contribute name idents."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-latency-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402
from backend.scheduler import manager  # noqa: E402


def _airport() -> int:
    return db.execute(
        "INSERT INTO airports (name, url, interval_minutes, created_at, updated_at, enabled) "
        "VALUES ('星链','http://x',30,datetime('now'),datetime('now'),1)")


def _node(airport: int, name: str, fingerprint: str) -> int:
    return db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, params, fingerprint, "
        "status, latency, fail_count, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (airport, name, "ss", "1.1.1.1", 443, db.dumps({"cipher": "aes-128-gcm", "password": "pw"}),
         fingerprint, "available", 0, 0))


def _probe(node_id: int, ok: bool, latency: int) -> None:
    db.execute("INSERT INTO latency_results (node_id, batch_id, ok, latency_ms, error_type, message, ts) "
               "VALUES (?,?,?,?,?,?,?)",
               (node_id, None, 1 if ok else 0, latency, None if ok else "slow", "", db.now()))


def _group_pass(node_id: int, group_id: int) -> None:
    db.execute("INSERT INTO results (node_id, group_id, batch_id, ok, latency_ms, error_type, message, ts) "
               "VALUES (?,?,?,?,?,?,?,?)", (node_id, group_id, None, 1, 300, None, "", db.now()))


def test_publish_gated_by_latency_ceiling():
    db.init_db()
    db.set_setting("sub_max_latency", 500)
    airport = _airport()
    a = _node(airport, "快速-有标识", "fp-a")
    b = _node(airport, "慢速", "fp-b")
    c = _node(airport, "快速-无标识", "fp-c")
    d = _node(airport, "未检测", "fp-d")
    _probe(a, True, 300)
    _probe(b, True, 700)
    _probe(c, True, 320)
    group = db.rows("SELECT id FROM test_groups WHERE enabled=1 ORDER BY sort_order")[0]
    _group_pass(a, group["id"])

    nodes, stats = builder.collect_nodes()
    names = [n["display_name"] for n in nodes]
    assert len(nodes) == 2, names
    assert any("星链.G " in n for n in names), names          # ident from the group
    assert any("星链.快速-无标识" == n for n in names), names   # published without any ident
    assert stats["skipped"]["slow"] == 1 and stats["skipped"]["no_result"] == 1, stats


def test_probe_config_and_toggle():
    db.init_db()
    probe = manager.latency_probe()
    assert probe and probe["max_latency"] == 500
    assert probe["url"] == "http://www.google.com/generate_204"
    db.set_setting("latency_test_url", "https://www.gstatic.com/generate_204")
    db.set_setting("latency_test_enabled", "0")   # legacy value is ignored
    assert manager.latency_probe() is not None
    assert manager.latency_probe()["url"].startswith("https://")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
