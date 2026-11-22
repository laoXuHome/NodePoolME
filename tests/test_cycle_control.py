"""Pause/stop a running batch; stopping publishes only the progress achieved."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-cycle-control-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from backend.database import db  # noqa: E402
from backend.scheduler import manager  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


class SlowEngine:
    name = "fake"

    async def prepare(self, block):
        await asyncio.sleep(0)

    async def test(self, node, probe):
        await asyncio.sleep(0.18)
        return SimpleNamespace(ok=True, latency_ms=120, error_type=None, message="ok")


def _seed():
    db.init_db()
    db.execute("DELETE FROM test_groups")
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('控制','http://x',30,datetime('now'),datetime('now'),1)")
    node = db.row(
        "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
        "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now')) RETURNING *",
        (ap, "n1", "ss", "1.1.1.1", 443,
         db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), "fp", "available", 100, 0),
    )
    manager.engine_mod.engine_manager = SlowEngine()
    manager.engine_mod.jitter_ms = lambda: 0
    return node


async def _wait_until(pred, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.02)
    return False


def test_pause_and_stop_publish_partial():
    _seed()

    async def scenario():
        task = asyncio.create_task(manager.run_cycle(force=True, reason="manual"))
        assert await _wait_until(lambda: manager.state.running)
        await asyncio.sleep(0.12)
        assert manager.state.request_pause()["ok"]
        await asyncio.sleep(0.3)
        assert manager.state.paused is True
        rows_paused = db.row("SELECT COUNT(*) AS c FROM latency_results")["c"]
        await asyncio.sleep(0.2)
        assert db.row("SELECT COUNT(*) AS c FROM latency_results")["c"] == rows_paused
        assert manager.state.request_resume()["ok"]
        await asyncio.sleep(0.08)
        assert manager.state.request_stop()["ok"]
        result = await asyncio.wait_for(task, timeout=8)
        assert result["ok"] and result["stopped"], result
        assert result["published"] is True, result
        batch = db.row("SELECT * FROM batches WHERE id=?", (result["batch_id"],))
        assert batch["status"] == "stopped"
        scoped, _ = builder.collect_nodes(result["batch_id"])
        # Only results produced before the stop are eligible.
        assert all(n["id"] == 1 for n in scoped)

    asyncio.run(scenario())
    assert manager.state.running is False
    assert manager.state.paused is False
    assert manager.state.stop_requested is False


def test_stopped_schedule_keeps_interval():
    db.init_db()
    db.set_setting("sub_publish_enabled", "1")
    db.set_setting("sub_publish_mode", "interval")
    db.set_setting("sub_publish_interval_minutes", 60)
    planned = datetime.now() - timedelta(minutes=1)
    db.set_setting("sub_publish_next_at", planned.strftime("%Y-%m-%d %H:%M:%S"))
    db.execute("DELETE FROM settings WHERE key='sub_last_auto_publish'")

    async def fake_cycle(**kwargs):
        return {"ok": True, "published": True, "stopped": True}

    original = manager.run_cycle
    manager.run_cycle = fake_cycle
    try:
        asyncio.run(manager.job_publish())
    finally:
        manager.run_cycle = original

    nxt = manager._parse_ts(db.get("sub_publish_next_at"))
    assert nxt is not None
    # The cadence continues from the originally planned slot (+60m), not from completion.
    assert abs((nxt - (planned + timedelta(minutes=60))).total_seconds()) < 2
    assert db.get("sub_last_auto_publish") == datetime.now().strftime("%Y-%m-%d")




def test_small_global_pool_does_not_deadlock():
    """Groups used to hold every global slot while waiting for a node locked by latency."""
    db.init_db()
    db.execute("DELETE FROM test_groups")
    db.execute("DELETE FROM nodes")
    db.execute("DELETE FROM airports")
    ap = db.execute("INSERT INTO airports (name,url,interval_minutes,created_at,updated_at,enabled) "
                    "VALUES ('并发','http://x',30,datetime('now'),datetime('now'),1)")
    for i in range(8):
        db.execute(
            "INSERT INTO nodes (airport_id,raw_name,protocol,server,port,params,fingerprint,status,"
            "latency,fail_count,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
            (ap, f"n{i}", "ss", f"10.0.0.{i + 1}", 443,
             db.dumps({"cipher": "aes-128-gcm", "password": "pw"}), f"fp{i}", "available", 100, 0))
    for i in range(5):
        db.execute(
            "INSERT INTO test_groups (name,ident,url,method,timeout,expect_status,expect_body,"
            "max_latency,interval_minutes,enabled,sort_order,is_default,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))",
            (f"G{i}", f"T{i}", "http://example.com", "GET", 8, "200-399", "", 1000, 30, 1, i, 0))
    db.set_setting("global_concurrency", 3)
    db.set_setting("group_concurrency", 5)
    db.set_setting("ip_concurrency", 1)
    db.set_setting("subnet_concurrency", 2)

    class FastEngine:
        name = "fake"

        async def prepare(self, block):
            await asyncio.sleep(0)

        async def test(self, node, probe):
            await asyncio.sleep(0.005)
            return SimpleNamespace(ok=True, latency_ms=50, error_type=None, message="ok")

    manager.engine_mod.engine_manager = FastEngine()
    manager.engine_mod.jitter_ms = lambda: 0

    result = asyncio.run(asyncio.wait_for(manager.run_cycle(force=True, reason="manual"), timeout=6))
    assert result["ok"] and not result["stopped"], result
    assert result["processed"] == result["total"], result


if __name__ == "__main__":
    test_pause_and_stop_publish_partial()
    print("PASS pause/stop partial publish")
    test_stopped_schedule_keeps_interval()
    print("PASS stopped schedule keeps interval")
    test_small_global_pool_does_not_deadlock()
    print("PASS small global pool completes")
