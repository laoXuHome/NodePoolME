"""P4-03: one fixed scheduled batch per window, no random spread."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from backend.database import db
from backend.services import speed_queue

TZ = ZoneInfo("Asia/Shanghai")


def _node(name: str) -> int:
    airport_id = db.execute(
        "INSERT INTO airports (name, url, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("固定调度机场", "https://example.com/sub", 1,
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    return db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint, params, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (airport_id, name, "ss", "1.2.3.4", 443, f"fp-{name}", "{}",
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )


def _at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def _setup() -> None:
    db.set_setting("speed_enabled", "1")
    db.set_setting("speed_schedule_enabled", "1")
    db.set_setting("speed_schedule_last_date", "")
    db.set_setting("speed_schedule_every_days", 1)
    db.set_setting("speed_window_start", "17:00")
    db.set_setting("speed_window_end", "24:00")
    db.set_setting("speed_parallel_nodes", 2)


def test_schedule_creates_one_fixed_batch_in_window_and_never_repeats():
    _setup()
    nodes = [_node("固定01"), _node("固定02"), _node("固定03")]
    q = speed_queue.SpeedQueue()

    first = q.schedule_once(_at(27, 20, 0))
    assert first["created"] == 3
    assert first["node_ids"] == nodes
    assert first["concurrency"] == 2
    jobs = [q.jobs[jid] for jid in q._order]
    assert [job.node_id for job in jobs] == nodes
    # All share the same fixed start timestamp, preserving FIFO order.
    assert len({job.not_before for job in jobs}) == 1
    assert abs(jobs[0].not_before - datetime.now(TZ).timestamp()) < 10

    second = q.schedule_once(_at(27, 21, 0))
    assert second["created"] == 0
    assert second["skipped"] == "already_ran"
    assert len(q.jobs) == 3


def test_schedule_outside_window_does_nothing():
    _setup()
    _node("窗口01")
    q = speed_queue.SpeedQueue()
    result = q.schedule_once(_at(27, 12, 0))
    assert result["skipped"] == "outside_window"
    assert result["created"] == 0
    assert q.jobs == {}


def test_overnight_window_uses_previous_day_and_does_not_repeat():
    _setup()
    db.set_setting("speed_window_start", "20:00")
    db.set_setting("speed_window_end", "02:00")
    _node("跨日01")
    q = speed_queue.SpeedQueue()

    result = q.schedule_once(_at(28, 0, 30))
    assert result["created"] == 1
    assert result["window_date"] == "2026-11-27"
    again = q.schedule_once(_at(28, 0, 31))
    assert again["skipped"] == "already_ran"
    assert again["created"] == 0


def test_configured_concurrency_is_the_batch_limit():
    _setup()
    db.set_setting("speed_parallel_nodes", 3)
    q = speed_queue.SpeedQueue()
    assert q._parallel_limit() == 3
    db.set_setting("speed_parallel_nodes", 99)
    assert q._parallel_limit() == 8
