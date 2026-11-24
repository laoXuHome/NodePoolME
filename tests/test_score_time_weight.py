"""时段加权：傍晚–凌晨样本占主导，凌晨减重；采样代表性由动态采样负责。"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-weight-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from backend.api.routes import _time_band, _weighted_mean  # noqa: E402


def _samples(values_by_hour):
    return [
        {"ts": f"2026-11-23 {hour:02d}:00:00", "available": value, "published": value, "share": value}
        for hour, value in values_by_hour
    ]


def test_time_band_mapping():
    assert _time_band("2026-11-23 18:00:00")[0] == 0.50
    assert _time_band("2026-11-23 00:30:00")[0] == 0.50
    assert _time_band("2026-11-23 03:00:00")[0] == 0.15
    assert _time_band("2026-11-23 09:00:00")[0] == 0.35
    assert _time_band("")[0] == 0.35


def test_band_shares_are_fixed_regardless_of_sample_count():
    # 半夜 6 个 1.0、傍晚 1 个 0.0：傍晚占 50%，半夜占 15%
    half = _weighted_mean(_samples([(3, 1.0)] * 6 + [(20, 0.0)]), "available")
    assert half is not None and abs(half - (0.15 * 1.0) / 0.65) < 1e-9
    # 反过来：傍晚 6 个 1.0、半夜 1 个 0.0，结果应被傍晚主导
    other = _weighted_mean(_samples([(20, 1.0)] * 6 + [(3, 0.0)]), "available")
    assert other is not None and abs(other - (0.50 * 1.0) / 0.65) < 1e-9
    assert other > half


def test_missing_band_is_dropped_not_zeroed():
    # 只有白天档（35%）的快照：权重归一化后仍按自身均值返回，不因缺档被压成 0
    daytime = _weighted_mean(_samples([(9, 0.9), (13, 0.5)]), "share")
    assert daytime is not None and abs(daytime - 0.7) < 1e-9


def test_hours_within_band_share_weight_equally():
    # 晚高峰只有 19 点一份 1.0，凌晨档 03/05/07 三份 0.0
    rows = _samples([(19, 1.0), (3, 0.0), (5, 0.0), (7, 0.0)])
    mean = _weighted_mean(rows, "share")
    assert mean is not None and abs(mean - 0.50 / 0.65) < 1e-9


def test_unknown_availability_is_ignored():
    rows = _samples([(20, None), (20, 0.8)])
    assert abs(_weighted_mean(rows, "available") - 0.8) < 1e-9


if __name__ == "__main__":
    test_time_band_mapping()
    test_band_shares_are_fixed_regardless_of_sample_count()
    test_missing_band_is_dropped_not_zeroed()
    test_hours_within_band_share_weight_equally()
    test_unknown_availability_is_ignored()
    print("PASS score time weight")
