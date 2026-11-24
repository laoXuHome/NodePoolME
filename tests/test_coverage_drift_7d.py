"""长期漂移下的覆盖率：7 天连续 36 分钟一轮，覆盖率仍应稳定在 83% 左右。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-drift-")
os.environ.setdefault("NODEPOOL_DATA_DIR", _TMP)
os.environ.setdefault("NODEPOOL_CONFIG_DIR", os.path.join(_TMP, "config"))
os.environ.setdefault("NODEPOOL_LOG_DIR", os.path.join(_TMP, "logs"))
os.environ.setdefault("NODEPOOL_BACKUP_DIR", os.path.join(_TMP, "backup"))
os.environ.setdefault("NODEPOOL_ADMIN_PASSWORD", "test123")
os.environ.setdefault("NODEPOOL_TEST_ENGINE", "tcp")

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402
from backend.subscriptions import builder  # noqa: E402
from backend.scoring import sampler  # noqa: E402


def _ts(dt):
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _seed(days=7, interval=36, plan=30):
    for table in ("schedule_history", "share_history", "batches", "airports"):
        db.execute(f"DELETE FROM {table}")
    db.set_setting("test_interval_minutes", plan)
    created = _ts(datetime.now() - timedelta(days=8))
    db.execute("INSERT INTO airports (name, url, enabled, interval_minutes, created_at, updated_at)"
               " VALUES ('长跑机场','http://x',1,60,?,?)", (created, created))
    airport_id = db.row("SELECT id FROM airports")["id"]
    start = _ts(datetime.now() - timedelta(days=days))
    db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
               " VALUES (?,?, 'complete', 1, 1, 1)", (start, start))
    batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
    stamp = datetime.now() - timedelta(days=days)
    end = datetime.now()
    while stamp < end:
        db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published, total_available,"
                   " airport_id, pool_count, published_count, available_count, available_known)"
                   " VALUES (?, ?, 120, 100, 100, ?, 10, 8, 8, 1)",
                   (_ts(stamp), batch_id, airport_id))
        stamp += timedelta(minutes=interval)
    return airport_id


def _dashboard(client):
    assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
    return client.get("/api/dashboard").json()["airport_list"][0]


def test_seven_days_drift_keeps_coverage():
    _seed(days=7, interval=36)
    with TestClient(app) as client:
        row = _dashboard(client)
    assert row["coverage"] >= 80          # 83.3%，仍属正常覆盖
    assert row["data_status"] == "normal"
    assert row["rank_tier"] == 0
    assert row["sampled_points"] == 336   # 7 天 × 30 分钟
    assert row["score_samples"] == 280    # 336 × 83.3%，一份快照只用一次
    assert row["plan_estimated"] is False


def test_manual_full_batch_does_not_inflate_coverage():
    _seed(days=7, interval=36)
    with TestClient(app) as client:
        before = _dashboard(client)
        airport_id = db.row("SELECT id FROM airports")["id"]
        off_grid = datetime.now() - timedelta(days=1, minutes=18)
        db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
                   " VALUES (?,?, 'complete', 6, 6, 1)", (_ts(off_grid), _ts(off_grid)))
        batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
        db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published, total_available,"
                   " airport_id, pool_count, published_count, available_count, available_known)"
                   " VALUES (?, ?, 130, 110, 110, ?, 11, 9, 9, 1)",
                   (_ts(off_grid), batch_id, airport_id))
        after = _dashboard(client)
    assert after["coverage"] == before["coverage"]
    assert after["score_samples"] == before["score_samples"]


def test_incomplete_batch_snapshots_stay_out():
    _seed(days=7, interval=60)
    with TestClient(app) as client:
        before = _dashboard(client)
        assert 49.0 <= before["coverage"] <= 51.0   # 60 分钟一轮 ≈ 计划的一半
        airport_id = db.row("SELECT id FROM airports")["id"]
        # 中断批次恰好落在计划采样点上：如果算进来，覆盖率会跳到 100%
        start = datetime.now() - timedelta(days=7)
        db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
                   " VALUES (?,?, 'interrupted', 6, 2, 1)", (_ts(start), _ts(start)))
        batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
        for k in range(1, 336, 2):
            stamp = start + timedelta(minutes=30 * k)
            db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published, total_available,"
                       " airport_id, pool_count, published_count, available_count, available_known)"
                       " VALUES (?, ?, 120, 90, 90, ?, 10, 7, 7, 1)",
                       (_ts(stamp), batch_id, airport_id))
        after = _dashboard(client)
        assert after["coverage"] == before["coverage"]
        assert after["score_samples"] == before["score_samples"]
        all_ts = {p["ts"] for p in builder.share_history(7)}
        scored_ts = {p["ts"] for p in builder.share_history(7, complete_only=True)}
    assert len(all_ts) > len(scored_ts)
    assert 167 <= len(scored_ts) <= 169   # 7 天 60 分钟一轮：84 个完整批次时间点，边界±1


def test_matching_is_one_to_one_and_local():
    end = datetime.now()
    segs = sampler.build_segments([(end - timedelta(days=7), 30)], end - timedelta(days=7), end)
    snaps = [end - timedelta(days=7) + timedelta(minutes=6 + 36 * k) for k in range(280)]
    points = sampler.generate_points(segs, snaps, end)
    pairs = sampler.match_points(points, snaps)
    assert len({j for _, j in pairs}) == len(pairs)          # 一份快照只用一次
    assert all(abs((points[i].time - snaps[j]).total_seconds()) <= 15 * 60 for i, j in pairs)
    for (i1, j1), (i2, j2) in zip(pairs, pairs[1:]):
        assert i1 < i2 and j1 < j2                            # 单调、不交叉
        assert (points[i2].time - points[i1].time).total_seconds() < 2 * 30 * 60 + 1


def test_complete_manual_batch_counts():
    """手动完整检测（force 跑完整轮）与自动批次同等参与评分。"""
    _seed(days=7, interval=60)
    with TestClient(app) as client:
        before = _dashboard(client)
        assert 49.0 <= before["coverage"] <= 51.0   # 60 分钟一轮 ≈ 计划的一半
        airport_id = db.row("SELECT id FROM airports")["id"]
        start = datetime.now() - timedelta(days=7)
        db.execute("INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
                   " VALUES (?,?, 'complete', 6, 6, 1)", (_ts(start), _ts(start)))
        batch_id = db.row("SELECT MAX(id) AS id FROM batches")["id"]
        for k in range(1, 336, 2):
            stamp = start + timedelta(minutes=30 * k)
            db.execute("INSERT INTO share_history (ts, batch_id, total_nodes, total_published, total_available,"
                       " airport_id, pool_count, published_count, available_count, available_known)"
                       " VALUES (?, ?, 120, 100, 100, ?, 10, 8, 8, 1)",
                       (_ts(stamp), batch_id, airport_id))
        after = _dashboard(client)
    assert after["coverage"] >= 99.0
    assert after["score_samples"] >= 334
