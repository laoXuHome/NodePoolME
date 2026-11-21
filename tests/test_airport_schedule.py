"""Airport fetch schedule: interval, daily slot, and the manual-only combination."""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-airport-sched-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.services import fetcher  # noqa: E402

_IDS = []


def _add(interval: int, daily: str | None, last_attempt: str | None):
    aid = db.execute(
        "INSERT INTO airports (name,url,interval_minutes,daily_time,last_attempt_at,created_at,updated_at,"
        "enabled,last_status) VALUES (?,?,?,?,?,datetime('now'),datetime('now'),1,'ok')",
        (f"a{len(_IDS)}", "http://x", interval, daily, last_attempt))
    _IDS.append((aid, interval, daily))
    return aid


def _due_ids(now: datetime | None = None) -> set[int]:
    return {a["id"] for a in fetcher.due_airports(now)}


def test_manual_only_never_auto_fetches():
    db.init_db()
    now = datetime.now().astimezone()
    old = (now - timedelta(days=30)).isoformat()
    fresh = (now - timedelta(minutes=5)).isoformat()
    manual_old = _add(0, None, old)
    manual_never = _add(0, None, None)
    manual_daily_zero = _add(0, "", old)
    interval = _add(30, None, old)
    interval_recent = _add(30, None, fresh)
    daily = _add(0, "03:30", (now - timedelta(days=1)).isoformat())

    due = _due_ids(now)
    assert manual_old not in due and manual_never not in due and manual_daily_zero not in due
    assert interval in due and interval_recent not in due
    assert daily in due


def test_daily_and_interval_both():
    db.init_db()
    now = datetime.now().astimezone()
    # daily fires once per day even if the interval has not elapsed yet
    aid = _add(120, "03:30", (now - timedelta(days=1, hours=1)).isoformat())
    assert aid in _due_ids(now)


def test_interval_applies_when_daily_slot_passed():
    db.init_db()
    now = datetime.now().astimezone()
    # daily slot already consumed after the last attempt, but interval elapsed since then
    aid = _add(60, "03:30", (now - timedelta(minutes=90)).isoformat())
    assert aid in _due_ids(now)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
