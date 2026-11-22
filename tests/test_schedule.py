"""Scheduled publish: fires once inside the window, and never while a cycle is running."""
import asyncio
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-schedule-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from backend.database import db  # noqa: E402
from backend.scheduler import manager  # noqa: E402


def test_startup_anchor_delays_first_cycle_when_disabled():
    db.init_db()
    calls = []

    async def fake_cycle(**kwargs):
        calls.append(kwargs)
        return {"ok": True}

    original = manager.run_cycle
    manager.run_cycle = fake_cycle
    manager.state.last_cycle_at = None
    manager.state.auto_cycle_anchor = datetime.now()
    try:
        asyncio.run(manager.job_cycle())
        assert not calls, "recent anchor must suppress the first automatic cycle"
        manager.state.auto_cycle_anchor = datetime.now() - timedelta(minutes=60)
        asyncio.run(manager.job_cycle())
        assert len(calls) == 1, "an expired anchor must allow the automatic cycle"
    finally:
        manager.run_cycle = original
        manager.state.auto_cycle_anchor = None


def test_disabled_does_nothing():
    db.init_db()
    db.set_setting("sub_publish_enabled", "0")
    asyncio.run(manager.job_publish())
    assert db.get("sub_last_auto_publish") is None


def test_runs_once_in_window():
    db.init_db()
    now = datetime.now().strftime("%H:%M")
    db.set_setting("sub_publish_enabled", "1")
    db.set_setting("sub_publish_time", now)
    asyncio.run(manager.job_publish())
    assert db.get("sub_last_auto_publish") == datetime.now().strftime("%Y-%m-%d")
    # second call within the same day must be a no-op
    before = db.get("sub_last_auto_publish")
    asyncio.run(manager.job_publish())
    assert db.get("sub_last_auto_publish") == before


def test_skips_while_cycle_running():
    db.init_db()
    db.set_setting("sub_publish_enabled", "1")
    db.set_setting("sub_publish_time", datetime.now().strftime("%H:%M"))
    db.execute("DELETE FROM settings WHERE key='sub_last_auto_publish'")
    manager.state.running = True
    try:
        asyncio.run(manager.job_publish())
    finally:
        manager.state.running = False
    assert db.get("sub_last_auto_publish") is None


def test_interval_mode():
    db.init_db()
    db.set_setting("sub_publish_enabled", "1")
    db.set_setting("sub_publish_mode", "interval")
    db.set_setting("sub_publish_interval_minutes", 60)
    db.execute("DELETE FROM settings WHERE key='sub_publish_next_at'")
    asyncio.run(manager.job_publish())
    next_at = manager._parse_ts(db.get("sub_publish_next_at"))
    assert next_at is not None and next_at > datetime.now()
    # still inside the countdown: nothing runs
    asyncio.run(manager.job_publish())
    assert db.get("sub_last_auto_publish") is None

    # pretend the countdown expired; a successful cycle re-arms it one interval ahead
    db.set_setting("sub_publish_next_at", "2000-01-01 00:00:00")
    calls = []

    async def fake_cycle(**kwargs):
        calls.append(kwargs)
        return {"ok": True, "published": True}

    original = manager.run_cycle
    manager.run_cycle = fake_cycle
    try:
        asyncio.run(manager.job_publish())
    finally:
        manager.run_cycle = original
    assert calls, "interval publish never triggered"
    assert db.get("sub_last_auto_publish") == datetime.now().strftime("%Y-%m-%d")
    next_at = manager._parse_ts(db.get("sub_publish_next_at"))
    assert next_at > datetime.now() + manager.timedelta(minutes=50)


def test_fetch_blocked_during_cycle():
    db.init_db()
    db.set_setting("sub_publish_enabled", "0")
    manager.state.running = True
    try:
        result = asyncio.run(manager.fetch_now(999))
    finally:
        manager.state.running = False
    assert result["ok"] is False and "运行" in result["error"]


def test_bad_time_format_rejected():
    from backend.models.schemas import SettingsIn
    try:
        SettingsIn(sub_publish_time="25:99")
    except Exception:
        return
    raise AssertionError("25:99 should be rejected")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
