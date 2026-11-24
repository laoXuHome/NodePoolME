"""动态采样：按计划生成采样点 + 全时间轴一对一匹配 + 覆盖率/成熟度状态。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-sample-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from backend.scoring import sampler  # noqa: E402

T0 = datetime(2026, 11, 17, 0, 0, 0)


def _plan_points(interval, days=1):
    segs = sampler.build_segments([(T0, interval)], T0, T0 + timedelta(days=days))
    return sampler.generate_points(segs, [], T0 + timedelta(days=days))


def test_points_per_interval():
    assert len(_plan_points(30)) == 48
    assert len(_plan_points(60)) == 24
    assert len(_plan_points(120)) == 12
    assert len(_plan_points(180)) == 8
    assert len(_plan_points(360)) == 4


def test_points_seven_day_totals():
    assert len(_plan_points(30, 7)) == 336
    assert len(_plan_points(60, 7)) == 168
    assert len(_plan_points(120, 7)) == 84
    assert len(_plan_points(180, 7)) == 56
    assert len(_plan_points(360, 7)) == 28


def test_interval_change_splits_segments():
    change = T0 + timedelta(days=3)
    segs = sampler.build_segments([(T0, 60), (change, 30)], T0, T0 + timedelta(days=7))
    assert [round(s.interval_minutes) for s in segs] == [60, 30]
    points = sampler.generate_points(segs, [], T0 + timedelta(days=7))
    hourly = [p for p in points if p.time < change]
    half = [p for p in points if p.time >= change]
    assert len(hourly) == 72
    assert len(half) == 192
    gaps = {(b.time - a.time) for a, b in zip(points, points[1:]) if a.time < change}
    assert gaps == {timedelta(minutes=60)}


def test_pause_segment_generates_no_points():
    pause = T0 + timedelta(days=1)
    resume = T0 + timedelta(days=2)
    segs = sampler.build_segments([(T0, 60), (pause, 0), (resume, 60)], T0, T0 + timedelta(days=3))
    points = sampler.generate_points(segs, [], T0 + timedelta(days=3))
    assert not [p for p in points if pause <= p.time < resume]
    assert [p for p in points if p.time < pause]
    assert [p for p in points if p.time >= resume]


def test_snapshot_used_only_once():
    snaps = [T0 + timedelta(minutes=30)]
    points = _plan_points(30)
    matched = sampler.match_points(points, snaps)
    assert len(matched) == 1
    assert len({pair[1] for pair in matched}) == len(matched)


def test_global_nearest_matching_prefers_closest_point():
    points = [sampler.SamplePoint(T0 + timedelta(hours=10), 60),
              sampler.SamplePoint(T0 + timedelta(hours=10, minutes=30), 60)]
    snaps = [T0 + timedelta(hours=10, minutes=12)]
    matched = sampler.match_points(points, snaps)
    assert len(matched) == 1
    assert points[matched[0][0]].time == T0 + timedelta(hours=10)


def test_cross_hour_matching():
    segs = sampler.build_segments([(T0 + timedelta(hours=10), 30)], T0 + timedelta(hours=10),
                                  T0 + timedelta(hours=12))
    points = sampler.generate_points(segs, [], T0 + timedelta(hours=12))
    snaps = [T0 + timedelta(hours=11, minutes=5)]
    matched = sampler.match_points(points, snaps)
    assert [points[pair[0]].time for pair in matched] == [T0 + timedelta(hours=11, minutes=0)]


def test_match_distance_boundary():
    segs = sampler.build_segments([(T0 + timedelta(hours=8), 60)], T0 + timedelta(hours=8),
                                  T0 + timedelta(hours=9))
    points = sampler.generate_points(segs, [], T0 + timedelta(hours=9))
    assert points[0].max_distance == timedelta(minutes=15)
    inside = [points[0].time + timedelta(minutes=15)]
    outside = [points[0].time + timedelta(minutes=15, seconds=1)]
    assert sampler.match_points(points, inside)
    assert not sampler.match_points(points, outside)


def test_short_interval_uses_half_period():
    segs = sampler.build_segments([(T0 + timedelta(hours=8), 30)], T0 + timedelta(hours=8),
                                  T0 + timedelta(hours=9))
    points = sampler.generate_points(segs, [], T0 + timedelta(hours=9))
    assert points[0].max_distance == timedelta(minutes=15)


def test_missing_hours_stay_missing():
    span_start, span_end = T0 + timedelta(hours=6), T0 + timedelta(hours=12)
    segs = sampler.build_segments([(span_start, 60)], span_start, span_end)
    points = sampler.generate_points(segs, [], span_end)
    snaps = [span_start + timedelta(hours=1), span_start + timedelta(hours=5)]
    matched = sampler.match_points(points, snaps)
    known = [p for p in points if p.known]
    assert len(known) == 6
    assert len(matched) == 2


def test_intermittent_snapshots_keep_valid_matches():
    span_start, span_end = T0, T0 + timedelta(hours=6)
    segs = sampler.build_segments([(span_start, 60)], span_start, span_end)
    points = sampler.generate_points(segs, [], span_end)
    snaps = [T0 + timedelta(minutes=m) for m in (5, 62, 190, 241, 305)]
    matched = sampler.match_points(points, snaps)
    assert len(matched) == 5
    assert len({pair[1] for pair in matched}) == 5


def test_manual_snapshot_enters_pool():
    span_start, span_end = T0 + timedelta(hours=8), T0 + timedelta(hours=10)
    segs = sampler.build_segments([(span_start, 60)], span_start, span_end)
    points = sampler.generate_points(segs, [], span_end)
    manual = [span_start + timedelta(minutes=3)]   # 手动检测，不在计划节奏上
    assert len(sampler.match_points(points, manual)) == 1


def test_plan_history_not_overwritten_by_current_config():
    from backend.database import db

    db.execute("DELETE FROM schedule_history")
    db.record_schedule_plan(60, "settings")
    db.record_schedule_plan(60, "boot")
    assert len(db.rows("SELECT * FROM schedule_history")) == 1
    db.record_schedule_plan(30, "settings")
    rows = db.rows("SELECT interval_minutes FROM schedule_history ORDER BY id")
    assert [int(r["interval_minutes"]) for r in rows] == [60, 30]


def test_status_thresholds():
    assert sampler.data_status(7, 0.80) == "normal"
    assert sampler.data_status(7, 0.7999) == "reference"
    assert sampler.data_status(7, 0.60) == "reference"
    assert sampler.data_status(7, 0.5999) == "insufficient"
    assert sampler.data_status(7, 0.50) == "insufficient"
    assert sampler.data_status(7, None) == "unknown"


def test_new_project_states():
    assert sampler.data_status(0.5, 0.99) == "collecting"
    assert sampler.data_status(1.5, 0.60) == "tentative"
    assert sampler.data_status(1.5, 0.59) == "insufficient"
    assert sampler.data_status(2.0, 0.60) == "reference"
    assert sampler.data_status(6.9, 0.90) == "reference"
    assert sampler.data_status(7.0, 0.90) == "normal"
    assert sampler.data_status(7.0, 0.30) == "insufficient"


def test_rank_tiers():
    assert sampler.status_rank("normal") == 0
    assert sampler.status_rank("reference") == 1
    assert sampler.status_rank("unknown") == 1
    assert sampler.status_rank("insufficient") == 2
    assert sampler.status_rank("collecting") == 2


def test_unknown_history_marks_uncoverable():
    # 窗口起点早于第一条计划记录：那段标为未知，不伪造采样点
    segs = sampler.build_segments([(T0 + timedelta(days=3), 60)], T0, T0 + timedelta(days=7))
    assert segs[0].known is False
    assert segs[-1].known is True


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("PASS dynamic sampling")


def test_frequency_does_not_change_score():
    """检测更频繁只增加采样点，不加权：同一条时间曲线，30 分钟与 60 分钟评分一致。"""
    from backend.api.routes import _weighted_mean

    def score_for(interval):
        end = T0 + timedelta(days=1)
        segs = sampler.build_segments([(T0, interval)], T0, end)
        points = sampler.generate_points(segs, [], end)
        snaps = [p.time for p in points]           # 每个采样点都恰好命中
        matched = sampler.match_points(points, snaps)
        samples = []
        for pidx, _ in matched:
            hour = points[pidx].time.hour
            samples.append({"ts": points[pidx].time.strftime("%Y-%m-%d %H:%M:%S"),
                            "available": 0.9 if 17 <= hour or hour < 1 else
                                         0.2 if 1 <= hour < 8 else 0.5})
        return _weighted_mean(samples, "available"), len(points)

    half_hour, n_half = score_for(30)
    hourly, n_hour = score_for(60)
    assert n_half > n_hour
    assert abs(half_hour - hourly) < 1e-9


def test_practical_drift_still_covers():
    """批次"满间隔才开跑 + 自身耗时"会让真实节奏比计划晚几分钟，不能判成缺失。"""
    end = T0 + timedelta(days=2)
    segs = sampler.build_segments([(T0, 30)], T0, end)

    def coverage(step_minutes):
        snaps = [T0 + timedelta(minutes=5 + step_minutes * k)
                 for k in range(int(2 * 1440 / step_minutes))]
        points = sampler.generate_points(segs, snaps, end)
        matched = sampler.match_points(points, snaps)
        known = [p for p in points if p.known]
        return len([m for m in matched if points[m[0]].known]) / len(known) * 100

    assert coverage(30) == 100
    assert coverage(36) >= 80      # 30 分钟计划、实际 36 分钟一轮：仍算正常覆盖
    assert coverage(40) >= 70      # 更慢一点：掉到参考，但不至于判成数据不足
    assert coverage(120) < 30      # 频率掉到 2 小时一次：覆盖率如实反映
