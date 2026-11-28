"""P4 seven-day quality, maturity and download-speed rules.

The calculator is intentionally pure: callers pass P2 checks and P4 settings, so
the same rules can be tested without a database or a live Mihomo node.  P4 never
writes P1/P2/P3 state; it only derives evidence from P2 snapshots.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from collections import Counter
from statistics import pstdev
from typing import Iterable

BANDS = ("peak", "night", "day")
BAND_LABELS = {"peak": "17:00–01:00", "night": "01:00–08:00", "day": "08:00–17:00"}
DEFAULT_WEIGHTS = {"peak": 50.0, "night": 15.0, "day": 35.0}
QUALITY_WEIGHTS = {"available": 50.0, "delay": 30.0, "stability": 20.0}
DEFAULT_SETTINGS = {
    "window_days": 7,
    "weights": DEFAULT_WEIGHTS,
    "min_daily_snapshots": 12,
    "min_quality_days": 5,
    "min_maturity": 70,
    "quality_pass": 75,
    "speed_excellent": 5.0,
    "speed_good": 2.0,
    "speed_minimum": 0.5,
    "speed_ttl_days": 7,
    "speed_limit_mb": 50,
    "speed_window_start": "17:00",
    "speed_window_end": "24:00",
    "speed_schedule_enabled": True,
    "speed_every_days": 1,
    "speed_concurrency": 4,
    "timezone": "Asia/Shanghai",
}

GRADE_DEFS = {
    "excellent": {"key": "excellent", "label": "优", "emoji": "🟢", "color": "#10B981"},
    "good": {"key": "good", "label": "良", "emoji": "🔵", "color": "#4682B4"},
    "poor": {"key": "poor", "label": "差", "emoji": "🟠", "color": "#F59E0B"},
    "danger": {"key": "danger", "label": "危", "emoji": "🔴", "color": "#EF4444"},
    "empty": {"key": "empty", "label": "无数据", "emoji": "⚪", "color": "#CBD5E1"},
    "excluded": {"key": "excluded", "label": "机场已排除", "emoji": "⚪", "color": "#CBD5E1"},
}


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def band_for_hour(hour: int) -> str:
    """17–01 peak, 01–08 night, 08–17 day (end-exclusive)."""
    hour = int(hour) % 24
    if hour >= 17 or hour < 1:
        return "peak"
    if hour < 8:
        return "night"
    return "day"


def parse_stamp(value) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def speed_grade(mb_per_s: float | None, settings: dict | None = None, *,
                excluded: bool = False) -> dict:
    """Map MB/s to P4's four usable grades. Missing data is never 'danger'."""
    if excluded:
        return {**GRADE_DEFS["excluded"], "mb_per_s": None, "kb_per_s": None}
    if mb_per_s is None or float(mb_per_s) < 0:
        return {**GRADE_DEFS["empty"], "mb_per_s": None, "kb_per_s": None}
    th = {**DEFAULT_SETTINGS, **(settings or {})}
    excellent = float(th["speed_excellent"])
    good = float(th["speed_good"])
    minimum = float(th["speed_minimum"])
    speed = float(mb_per_s)
    if speed >= excellent:
        key = "excellent"
    elif speed >= good:
        key = "good"
    elif speed >= minimum:
        key = "poor"
    else:
        key = "danger"
    return {**GRADE_DEFS[key], "mb_per_s": round(speed, 3),
            "kb_per_s": round(speed * 1024, 1)}


def _weighted(values: list[tuple[str, float]], weights: dict) -> float | None:
    """Band-weighted mean; every band contributes its own mean, not each sample."""
    if not values:
        return None
    counts = Counter(band for band, _ in values)
    present = set(counts)
    total = sum(float(weights.get(b, 0)) for b in present)
    if total <= 0:
        return None
    sums: dict[str, float] = {}
    for band, value in values:
        sums[band] = sums.get(band, 0.0) + float(value)
    return sum(float(weights.get(band, 0)) / total * (sums[band] / counts[band])
               for band in present)


def _weighted_variance(values: list[tuple[str, float]], weights: dict,
                       mean: float) -> float | None:
    """Variance with each band's weight spread evenly across its own samples."""
    if len(values) < 2:
        return None
    counts = Counter(band for band, _ in values)
    present = set(counts)
    total = sum(float(weights.get(b, 0)) for b in present)
    if total <= 0:
        return None
    return sum((float(weights.get(band, 0)) / total / counts[band]) * (float(value) - mean) ** 2
               for band, value in values)


def delay_score(p80_ms: float | None) -> float | None:
    """100 at ≤150 ms, 0 at ≥800 ms, linear between. P80 only, never median."""
    if p80_ms is None:
        return None
    return round(_clamp(100.0 * (800.0 - float(p80_ms)) / 650.0), 1)


def stability_score(stddev_ms: float | None) -> float | None:
    """100 at ≤40 ms standard deviation, 0 at ≥400 ms."""
    if stddev_ms is None:
        return None
    return round(_clamp(100.0 * (400.0 - float(stddev_ms)) / 360.0), 1)


def quality_from_checks(checks: Iterable[dict], settings: dict | None = None,
                        window_end: datetime | None = None) -> dict:
    """Compute the 7-day quality result from P2 checks.

    Definitive Success/Failure snapshots count in the availability denominator.
    Unknown, running, interrupted and invalid snapshots are excluded entirely —
    they never become a zero.  Delay uses only real TCP P80 samples.
    """
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    weights = {**DEFAULT_WEIGHTS, **(cfg.get("weights") or {})}
    days = int(cfg.get("window_days") or 7)
    end = (window_end or datetime.now()).replace(microsecond=0)
    start = end - timedelta(days=days)
    valid: list[dict] = []
    for row in checks:
        stamp = parse_stamp(row.get("finished_at") or row.get("started_at"))
        if stamp is None or not (start <= stamp <= end):
            continue
        status = str(row.get("status") or "").lower()
        if status not in ("success", "failure"):
            continue
        valid.append({"at": stamp, "status": status,
                      "p80_ms": row.get("p80_ms"), "band": band_for_hour(stamp.hour),
                      "date": stamp.date().isoformat()})
    if not valid:
        return {"quality_score": None, "available_score": None, "delay_score": None,
                "stability_score": None, "valid_snapshots": 0, "valid_days": 0,
                "availability": None, "p80_ms": None, "p80_stddev_ms": None,
                "severe_days": 0, "max_consecutive_failures": 0,
                "band_stats": {}, "daily": [], "reasons": ["no_valid_snapshots"]}

    success = [row for row in valid if row["status"] == "success"]
    failures = [row for row in valid if row["status"] == "failure"]
    availability = len(success) / len(valid) * 100 if valid else None

    p80_values = [(row["band"], float(row["p80_ms"])) for row in success
                  if row.get("p80_ms") is not None]
    p80_weighted = _weighted(p80_values, weights)
    stddev = None
    if p80_weighted is not None:
        variance = _weighted_variance(p80_values, weights, p80_weighted)
        stddev = variance ** 0.5 if variance is not None else None

    by_day: dict[str, list[dict]] = {}
    for row in valid:
        by_day.setdefault(row["date"], []).append(row)
    daily = []
    severe_days = 0
    for day in sorted(by_day):
        rows = by_day[day]
        ok = len([r for r in rows if r["status"] == "success"])
        rate = ok / len(rows) if rows else None
        # A severe day needs enough evidence; one isolated failure is not a "day".
        severe = bool(len(rows) >= 3 and rate is not None and rate < 0.5)
        if severe:
            severe_days += 1
        daily.append({"date": day, "valid": len(rows), "success": ok,
                      "failure": len(rows) - ok, "availability": round(rate * 100, 1),
                      "severe": severe, "bands": sorted({r["band"] for r in rows})})

    ordered = sorted(valid, key=lambda row: row["at"])
    run = max_run = 0
    for row in ordered:
        if row["status"] == "failure":
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0

    band_stats = {}
    for band in BANDS:
        rows = [r for r in valid if r["band"] == band]
        band_stats[band] = {
            "label": BAND_LABELS[band], "weight": float(weights.get(band, 0)),
            "valid": len(rows),
            "success": len([r for r in rows if r["status"] == "success"]),
            "availability": round(len([r for r in rows if r["status"] == "success"]) / len(rows) * 100, 1)
            if rows else None,
            "p80_ms": round(_weighted([(band, float(r["p80_ms"])) for r in rows
                                       if r["status"] == "success" and r.get("p80_ms") is not None],
                                      {band: 1}) or 0, 1) if rows and any(
                r["status"] == "success" and r.get("p80_ms") is not None for r in rows) else None,
        }

    components = {
        "available": (availability, QUALITY_WEIGHTS["available"]),
        "delay": (delay_score(p80_weighted), QUALITY_WEIGHTS["delay"]),
        "stability": (stability_score(stddev), QUALITY_WEIGHTS["stability"]),
    }
    present = {name: (value, weight) for name, (value, weight) in components.items()
               if value is not None}
    if present:
        denominator = sum(weight for _, weight in present.values())
        score = sum(value * weight for value, weight in present.values()) / denominator
    else:
        score = None
    if score is not None and (severe_days or max_run >= 3):
        score = min(score, 60.0)

    reasons: list[str] = []
    if severe_days:
        reasons.append("severe_day")
    if max_run >= 3:
        reasons.append("consecutive_failures")
    if p80_weighted is None:
        reasons.append("no_latency_samples")

    return {
        "quality_score": round(score, 1) if score is not None else None,
        "available_score": round(availability, 1) if availability is not None else None,
        "delay_score": delay_score(p80_weighted),
        "stability_score": stability_score(stddev),
        "valid_snapshots": len(valid),
        "valid_days": len(by_day),
        "availability": round(availability, 1) if availability is not None else None,
        "p80_ms": round(p80_weighted, 1) if p80_weighted is not None else None,
        "p80_stddev_ms": round(stddev, 1) if stddev is not None else None,
        "severe_days": severe_days,
        "max_consecutive_failures": max_run,
        "success_snapshots": len(success),
        "failure_snapshots": len(failures),
        "band_stats": band_stats,
        "daily": daily,
        "reasons": reasons,
    }


def maturity_from_quality(quality: dict, settings: dict | None = None,
                          window_end: datetime | None = None) -> dict:
    """Maturity is independent from quality; immature nodes are observed, not zeroed."""
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    days = int(cfg["window_days"])
    min_daily = int(cfg["min_daily_snapshots"])
    min_days = int(cfg["min_quality_days"])
    min_maturity = float(cfg["min_maturity"])
    daily = {row["date"]: int(row["valid"]) for row in quality.get("daily", [])}
    end = (window_end or datetime.now()).date()
    window_dates = [(end - timedelta(days=i)).isoformat()
                    for i in range(days - 1, -1, -1)]
    daily_completion = sum(min(daily.get(day, 0) / min_daily, 1.0) for day in window_dates) / max(1, len(window_dates))
    full_days = len([day for day, count in daily.items() if count >= min_daily])
    valid_days = len([day for day, count in daily.items() if count > 0])
    covered_cells = 0
    for row in quality.get("daily", []):
        covered_cells += len(set(row.get("bands") or []))
    total_cells = max(1, days * len(BANDS))
    coverage = covered_cells / total_cells
    # 50% daily attainment + 25% qualified days + 25% 7×3 band coverage.
    # valid_days is reported separately; it is not double-counted in the score.
    maturity = (daily_completion * 50.0
                + min(full_days / min_days, 1.0) * 25.0
                + coverage * 25.0)
    mature = bool(full_days >= min_days and maturity >= min_maturity)
    reasons = []
    if quality.get("valid_snapshots", 0) == 0:
        reasons.append("no_valid_snapshots")
    if full_days < min_days:
        reasons.append("not_enough_qualified_days")
    if maturity < min_maturity:
        reasons.append("low_maturity")
    if coverage < 0.5:
        reasons.append("low_time_coverage")
    return {
        "maturity_score": round(maturity, 1),
        "full_days": full_days,
        "valid_days": valid_days,
        "daily_completion": round(daily_completion * 100, 1),
        "coverage": round(coverage * 100, 1),
        "min_maturity": min_maturity,
        "mature": mature,
        "reasons": reasons,
    }


def speed_evaluation(runs: list[dict], settings: dict | None = None, *,
                    excluded: bool = False, now: datetime | None = None) -> dict:
    """Choose up to three latest valid runs; never use a single peak as grade."""
    cfg = {**DEFAULT_SETTINGS, **(settings or {})}
    if excluded:
        return {**speed_grade(None, cfg, excluded=True), "runs": [], "run": None,
                "median_mb_s": None, "valid_runs": 0, "reason": "airport_excluded"}
    cutoff = (now or datetime.now()) - timedelta(days=int(cfg["speed_ttl_days"]))
    valid = []
    for run in runs:
        status = str(run.get("status") or "")
        mb = run.get("mb_per_s")
        stamp = parse_stamp(run.get("finished_at") or run.get("started_at"))
        if status not in ("ok", "partial") or mb is None or stamp is None:
            continue
        if stamp < cutoff:
            continue
        valid.append((stamp, float(mb), run))
    valid.sort(key=lambda item: (item[0], int(item[2].get("id") or 0)), reverse=True)
    selected = valid[:3]
    if not selected:
        latest = runs[0] if runs else None
        return {**speed_grade(None, cfg), "runs": [], "run": None,
                "median_mb_s": None, "valid_runs": 0,
                "reason": (latest or {}).get("status") or "no_data",
                "last_message": (latest or {}).get("message")}
    median = sorted(item[1] for item in selected)[len(selected) // 2]
    return {**speed_grade(median, cfg), "runs": [item[2] for item in selected],
            "run": selected[0][2], "median_mb_s": round(median, 3),
            "valid_runs": len(selected), "latest_mb_s": round(selected[0][1], 3),
            "reason": None}


def validate_settings(data: dict, current: dict | None = None) -> dict:
    """Validate P4 settings and reject conflicting speed thresholds."""
    merged = {**DEFAULT_SETTINGS, **(current or {}), **data}
    weights = {**DEFAULT_WEIGHTS, **(merged.get("weights") or {})}
    for band in BANDS:
        try:
            weights[band] = float(weights[band])
        except (TypeError, ValueError):
            raise ValueError("invalid_weight")
        if not 0 <= weights[band] <= 100:
            raise ValueError("invalid_weight")
    if abs(sum(weights.values()) - 100.0) > 0.01:
        raise ValueError("weight_sum")
    ints = {"min_daily_snapshots": (6, 24), "min_quality_days": (3, 7),
            "min_maturity": (50, 90), "quality_pass": (60, 90),
            "speed_ttl_days": (1, 14), "speed_every_days": (1, 7),
            "speed_concurrency": (1, 16), "speed_limit_mb": (10, 50)}
    for key, (low, high) in ints.items():
        try:
            value = int(merged[key])
        except (TypeError, ValueError):
            raise ValueError(f"invalid_{key}")
        if not low <= value <= high:
            raise ValueError(f"invalid_{key}")
        merged[key] = value
    floats = {"speed_excellent": (0.5, 50.0), "speed_good": (0.25, 20.0),
              "speed_minimum": (0.25, 1.0)}
    for key, (low, high) in floats.items():
        try:
            value = float(merged[key])
        except (TypeError, ValueError):
            raise ValueError(f"invalid_{key}")
        if not low <= value <= high:
            raise ValueError(f"invalid_{key}")
        merged[key] = value
    if not merged["speed_excellent"] > merged["speed_good"] > merged["speed_minimum"]:
        raise ValueError("speed_threshold_order")
    # 512 KB/s ≈ 0.5 MB/s is the default; custom values remain above 0.25.
    if merged["speed_minimum"] < 0.25:
        raise ValueError("speed_minimum")
    for key in ("speed_window_start", "speed_window_end"):
        value = str(merged[key] or "").strip()
        if not value or ":" not in value:
            raise ValueError(f"invalid_{key}")
        parts = value.split(":")
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError(f"invalid_{key}")
        hour, minute = int(parts[0]), int(parts[1])
        if not (0 <= hour <= 24 and 0 <= minute <= 59) or (hour == 24 and minute):
            raise ValueError(f"invalid_{key}")
        merged[key] = f"{hour:02d}:{minute:02d}"
    merged["speed_schedule_enabled"] = bool(merged.get("speed_schedule_enabled"))
    merged["window_days"] = 7
    merged["weights"] = weights
    return merged


def quality_grade(quality_score: float | None, quality_pass: float) -> str:
    if quality_score is None:
        return "unknown"
    return "pass" if quality_score >= quality_pass else "fail"
