"""Node detail can test latency and tags separately."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-node-mode-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.scheduler import manager  # noqa: E402


def _seed():
    db.init_db()
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('A','http://x',30,datetime('now'),datetime('now'),1)")
    return db.execute(
        "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
        "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
        (ap, "n1", "ss", "1.1.1.1", 443,
         db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), "fp", "available", 100, 0))


class FakeEngine:
    async def prepare(self, nodes):
        return None

    async def test(self, node, probe):
        return SimpleNamespace(ok=True, latency_ms=100, error_type=None, message="")


def test_modes():
    node_id = _seed()
    fake = FakeEngine()
    manager.engine_mod.engine_manager = fake
    manager.engine_mod.jitter_ms = lambda: 0

    lat = asyncio.run(manager.test_single_node(node_id, mode="latency"))
    assert lat["ok"] and lat["mode"] == "latency", lat
    assert lat["results"] == [], lat
    assert lat["latency"]["ok"] is True
    assert set(lat["latency"]["targets"]) == {"G", "W"}

    tags = asyncio.run(manager.test_single_node(node_id, mode="tags"))
    assert tags["ok"] and tags["mode"] == "tags", tags
    assert tags["latency"] is None
    assert len(tags["results"]) == len(db.rows("SELECT id FROM test_groups WHERE enabled=1"))


if __name__ == "__main__":
    test_modes()
    print("PASS node test modes")
