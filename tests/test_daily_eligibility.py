"""每日快照准入：日门槛、60 分钟密度、暂停/中断排除。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-elig-")
os.environ.setdefault("NODEPOOL_DATA_DIR", _TMP)
os.environ.setdefault("NODEPOOL_CONFIG_DIR", os.path.join(_TMP, "config"))
os.environ.setdefault("NODEPOOL_LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("NODEPOOL_BACKUP_DIR", os.path.join(_TMP, "backup"))
os.environ.setdefault("NODEPOOL_ADMIN_PASSWORD", "test123")
os.environ.setdefault("NODEPOOL_TEST_ENGINE", "tcp")

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402
from backend.scoring import eligibility, sampler  # noqa: E402

DAY = datetime(2026, 11, 20)
DAY2 = DAY + timedelta(days=1)


def _points(day, step=30, days=1):
    segments = sampler.build_segments([(day, step)], day, day + timedelta(days=days))
    return sampler.generate_points(segments, [day], day + timedelta(days=days))


def _clock(day, *specs):
    return [day + timedelta(hours=h, minutes=m) for h, m in specs]


def _stat(selection, day):
    return next((d for d in selection.day_stats if d.date == day.strftime("%Y-%m-%d")), None)


def test_three_snapshots_day_is_dropped():
    snaps = _clock(DAY, (9, 0), (15, 0), (21, 0))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    stat = _stat(sel, DAY)
    assert stat.reason == "sparse" and stat.kept == 0
    assert sel.kept == []


def test_four_snapshots_density_leaves_two_then_dropped():
    snaps = _clock(DAY, (9, 0), (9, 15), (9, 30), (9, 45))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    stat = _stat(sel, DAY)
    assert stat.reason == "density" and stat.kept == 2
    assert sel.kept == []


def test_five_snapshots_density_leaves_three_then_dropped():
    snaps = _clock(DAY, (9, 0), (9, 10), (9, 20), (9, 30), (13, 0))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    stat = _stat(sel, DAY)
    assert stat.kept == 3 and stat.reason == "density"


def test_six_snapshots_density_leaves_four_kept():
    snaps = _clock(DAY, (9, 0), (9, 10), (9, 20), (9, 30), (13, 0), (21, 0))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    stat = _stat(sel, DAY)
    assert stat.kept == 4 and stat.reason is None
    assert len(sel.kept) == 4


def test_eight_snapshots_density_leaves_six_kept():
    snaps = _clock(DAY, (9, 0), (9, 10), (9, 20), (9, 30),
                   (12, 0), (15, 0), (18, 0), (21, 0))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    stat = _stat(sel, DAY)
    assert stat.kept == 6 and stat.reason is None
    assert len(sel.kept) == 6


def test_rolling_window_crosses_hours():
    snaps = _clock(DAY, (9, 0), (9, 30), (10, 0), (21, 0))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    assert _stat(sel, DAY).kept == 4        # 09:00/09:30/10:00 不在同一半开窗口，无需删
    snaps = _clock(DAY, (9, 50), (10, 10), (10, 30), (13, 0), (21, 0))
    sel = eligibility.select_snapshots(snaps, _points(DAY), [])
    assert _stat(sel, DAY).kept == 4        # 09:50/10:10/10:30 同窗 3 份 → 删 1


def test_pause_range_excludes_plan_points():
    day_s = DAY.strftime("%Y-%m-%d")
    events = [{"ts": f"{day_s} 10:00:00", "kind": "pause"},
              {"ts": f"{day_s} 11:30:00", "kind": "resume"}]
    batches = [{"started_at": f"{day_s} 09:00:00", "finished_at": f"{day_s} 12:00:00",
                "status": "stopped"}]
    ranges = eligibility.build_abnormal_ranges(batches, events, [], DAY, DAY + timedelta(days=1))
    assert any(kind == "pause" for _, _, kind in ranges)
    assert eligibility.is_excluded(DAY + timedelta(hours=10, minutes=30), ranges)
    assert not eligibility.is_excluded(DAY + timedelta(hours=13), ranges)


def test_plain_gap_is_not_treated_as_pause():
    ranges = eligibility.build_abnormal_ranges([], [], [], DAY, DAY2)
    assert ranges == []


def _seed_history(days, per_day_times, step=30):
    for table in ("schedule_history", "share_history", "batches", "airports", "run_events"):
        db.execute(f"DELETE FROM {table}")
    db.set_setting("test_interval_minutes", 30)
    created = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO airports (name, url, enabled, interval_minutes, created_at, updated_at)"
               " VALUES ('准入机场','http://x',1,60,?,?)", (created, created))
    airport_id = db.row("SELECT id FROM airports")["id"]
    now = datetime.now().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=days)
    db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
               " VALUES (?,?, 'complete', 1, 1, 1)", (start.strftime("%Y-%m-%d %H:%M:%S"),) * 2)
    batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
    stamp = start
    while stamp <= now:
        for hour, minute in per_day_times:
            slot = stamp.replace(hour=hour, minute=minute, second=0)
            if start <= slot <= now:
                db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published,"
                           " total_available, airport_id, pool_count, published_count,"
                           " available_count, available_known) VALUES (?, ?, 120, 100, 100, ?, 10, 8, 8, 1)",
                           (slot.strftime("%Y-%m-%d %H:%M:%S"), batch_id, airport_id))
        stamp += timedelta(days=1)
    return airport_id


def _row(client):
    client.post("/api/auth/login", json={"password": "test123"})
    return client.get("/api/dashboard").json()["airport_list"][0]


def test_insufficient_when_few_qualified_days():
    # 只有 3 天、每天 6 份：日门槛过不了 4 天
    _seed_history(3, [(9, 0), (9, 30), (13, 0), (13, 30), (18, 0), (21, 0)])
    with TestClient(app) as client:
        row = _row(client)
    assert row["eligible_days"] <= 3
    assert row["data_status"] == "insufficient"
    assert row["score"] is None


def _seed_days(entries):
    """entries: [(days_ago, [(h, m), ...]), ...]"""
    for table in ("schedule_history", "share_history", "batches", "airports", "run_events"):
        db.execute(f"DELETE FROM {table}")
    db.set_setting("test_interval_minutes", 30)
    created = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO airports (name, url, enabled, interval_minutes, created_at, updated_at)"
               " VALUES ('准入机场','http://x',1,60,?,?)", (created, created))
    airport_id = db.row("SELECT id FROM airports")["id"]
    now = datetime.now()
    db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
               " VALUES (?,?, 'complete', 3, 3, 1)", (created, created))
    batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for back, times in entries:
        day = base - timedelta(days=back)
        for hour, minute in times:
            stamp = day + timedelta(hours=hour, minutes=minute)
            if stamp > now:
                continue
            db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published,"
                       " total_available, airport_id, pool_count, published_count,"
                       " available_count, available_known) VALUES (?, ?, 120, 100, 100, ?, 10, 8, 8, 1)",
                       (stamp.strftime("%Y-%m-%d %H:%M:%S"), batch_id, airport_id))


GOOD = [(9, 0), (9, 30), (13, 0), (13, 30), (18, 0), (21, 0)]


def test_dropped_days_do_not_touch_good_days():
    good = [(back, GOOD) for back in (1, 2, 3, 5, 7)]
    _seed_days(good)
    with TestClient(app) as client:
        control = _row(client)
    # 中间插一天只有 2 份：整日淘汰，且已有日期的覆盖率与分母完全不受影响
    _seed_days(good + [(4, [(10, 0), (20, 0)])])
    with TestClient(app) as client:
        row = _row(client)
    assert row["coverage"] == control["coverage"]
    assert row["sampled_points"] == control["sampled_points"]
    assert row["eligible_days"] == control["eligible_days"]
    assert row["dropped_days"] >= 1
    assert "sparse" in [d["reason"] for d in row["day_stats"] if d["reason"]]


def _reset():
    for table in ("schedule_history", "share_history", "batches", "airports", "run_events"):
        db.execute(f"DELETE FROM {table}")
    db.set_setting("test_interval_minutes", 30)
    created = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO airports (name, url, enabled, interval_minutes, created_at, updated_at)"
               " VALUES ('准入机场','http://x',1,60,?,?)", (created, created))
    return db.row("SELECT id FROM airports")["id"]


def _batch(status, started, processed=3, target=3):
    db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
               " VALUES (?,?,?,?,?, 1)", (started, started, status, target, processed))
    return db.row("SELECT MAX(id) AS id FROM batches")["id"]


def _share(airport_id, batch_id, stamp):
    db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published, total_available,"
               " airport_id, pool_count, published_count, available_count, available_known)"
               " VALUES (?, ?, 120, 100, 100, ?, 10, 8, 8, 1)",
               (stamp.strftime("%Y-%m-%d %H:%M:%S"), batch_id, airport_id))


def test_interrupted_snapshot_is_not_counted():
    airport_id = _reset()
    base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
    auto = _batch("complete", (base + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S"))
    stopped = _batch("stopped", (base + timedelta(hours=20)).strftime("%Y-%m-%d %H:%M:%S"), processed=2)
    for hour, minute in ((9, 0), (9, 30), (13, 0)):
        _share(airport_id, auto, base + timedelta(hours=hour, minutes=minute))
    _share(airport_id, stopped, base + timedelta(hours=21))
    with TestClient(app) as client:
        row = _row(client)
    stat = next(d for d in row["day_stats"] if d["date"] == base.strftime("%Y-%m-%d"))
    assert stat["eligible"] == 3 and stat["reason"] == "sparse"


def test_auto_and_manual_batches_follow_same_rule():
    def run(mixed):
        airport_id = _reset()
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=1)
        auto = _batch("complete", (base + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S"))
        manual = _batch("complete", (base + timedelta(hours=13)).strftime("%Y-%m-%d %H:%M:%S"))
        for index, (hour, minute) in enumerate(((9, 0), (9, 10), (9, 20), (9, 30), (13, 0), (21, 0))):
            batch_id = manual if (mixed and index >= 4) else auto
            _share(airport_id, batch_id, base + timedelta(hours=hour, minutes=minute))
        with TestClient(app) as client:
            return _row(client)

    pure = run(mixed=False)
    mixed = run(mixed=True)
    assert mixed["coverage"] == pure["coverage"]
    assert mixed["score_samples"] == pure["score_samples"]
    assert next(d for d in mixed["day_stats"] if d["reason"] is None)["kept"] == 4
