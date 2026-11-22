"""Per-node latency rounds must alternate G/W with only one request in flight."""
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-latency-serial-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.scheduler import manager  # noqa: E402


def test_alternating_single_flight():
    db.init_db()
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('A','http://x',30,datetime('now'),datetime('now'),1)")
    node = db.row(
        "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
        "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) RETURNING *",
        (ap, "n1", "ss", "1.1.1.1", 443, db.dumps({"cipher": "aes-128-gcm", "password": "pw"}),
         "fp", "available", 100, 0),
    )
    probes = manager.latency_probes()
    active = {"n": 0, "max": 0, "order": []}

    class Tracker:
        async def test(self, node, probe):
            active["n"] += 1
            active["max"] = max(active["max"], active["n"])
            active["order"].append(probe["ident"])
            await asyncio.sleep(0.01)
            active["n"] -= 1
            return SimpleNamespace(ok=True, latency_ms=100, error_type=None, message="")

    manager.engine_mod.engine_manager = Tracker()
    manager.engine_mod.jitter_ms = lambda: 0
    counters = {"ok": 0, "done": 0, "groups": {}}
    asyncio.run(manager._latency_task([node], probes, None, asyncio.Semaphore(10),
                                      {node["id"]: asyncio.Lock()}, counters))
    assert active["max"] == 1, active
    assert active["order"] == ["G", "W", "G", "W"], active


def test_parallel_nodes_each_stay_single_flight():
    """Different nodes may probe together, but a single node never runs two probes."""
    db.init_db()
    db.execute("DELETE FROM nodes")
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('P','http://x',30,datetime('now'),datetime('now'),1)")
    nodes = []
    for i in range(5):
        nodes.append(db.row(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) RETURNING *",
            (ap, f"n{i}", "ss", f"10.1.0.{i + 1}", 443,
             db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), f"fp{i}", "available", 100, 0)))
    probes = manager.latency_probes()
    total = {"n": 0, "max": 0}
    per_node = {n["id"]: {"n": 0, "max": 0} for n in nodes}

    class Tracker:
        async def test(self, node, probe):
            total["n"] += 1
            total["max"] = max(total["max"], total["n"])
            per_node[node["id"]]["n"] += 1
            per_node[node["id"]]["max"] = max(per_node[node["id"]]["max"], per_node[node["id"]]["n"])
            await asyncio.sleep(0.01)
            total["n"] -= 1
            per_node[node["id"]]["n"] -= 1
            return SimpleNamespace(ok=True, latency_ms=100, error_type=None, message="")

    manager.engine_mod.engine_manager = Tracker()
    manager.engine_mod.jitter_ms = lambda: 0
    counters = {"ok": 0, "done": 0, "groups": {}}
    asyncio.run(manager._latency_task(nodes, probes, None, asyncio.Semaphore(3),
                                      {n["id"]: asyncio.Lock() for n in nodes}, counters))
    assert max(v["max"] for v in per_node.values()) == 1, per_node
    assert 2 <= total["max"] <= 3, total
    assert counters["done"] == len(nodes) * len(probes)


def test_manual_test_refused_during_cycle():
    manager.state.running = True
    try:
        res = asyncio.run(manager.test_single_node(1, mode="latency"))
        assert res["ok"] is False and "运行" in res["error"], res
    finally:
        manager.state.running = False


if __name__ == "__main__":
    test_alternating_single_flight()
    print("PASS alternating single-flight")
    test_manual_test_refused_during_cycle()
    print("PASS manual blocked during cycle")
    test_parallel_nodes_each_stay_single_flight()
    print("PASS parallel nodes, single-flight per node")
