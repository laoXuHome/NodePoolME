"""每日快照准入：整日门槛 → 60 分钟密度 → 复核，再交给原有 DP 匹配。

规则：
- 自然日按服务器本地时区切分；
- 每天先要 ≥4 份有效完整检测快照，否则整日淘汰；
- 任意连续 60 分钟（半开区间 [t, t+60)）最多保留 2 份，超出时按"对计划采样点的贡献"择优删除；
- 密度筛选后再复核 ≥4，仍不足则整日淘汰；
- 暂停/中断区间内的计划点标记为排除，不参与分子分母。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from . import sampler

MIN_DAILY_SNAPSHOTS = 4
DENSITY_LIMIT = 2
DENSITY_WINDOW = timedelta(minutes=60)
_DAY_FMT = "%Y-%m-%d"


@dataclass
class DayStat:
    date: str
    eligible: int = 0
    kept: int = 0
    reason: str | None = None      # sparse / density


@dataclass
class Selection:
    kept: list[datetime]
    day_stats: list[DayStat]
    dropped_days: list[str]
    excluded_ranges: list[tuple[datetime, datetime, str]]
    matched: list[tuple[int, int]] = None   # 由调用方在过滤后的点集上重算

    @property
    def scored_days(self) -> set[str]:
        return {d.date for d in self.day_stats if d.reason is None}


def local_day(stamp: datetime) -> str:
    return stamp.strftime(_DAY_FMT)


def day_bounds(day: str) -> tuple[datetime, datetime]:
    start = datetime.strptime(day, _DAY_FMT)
    return start, start + timedelta(days=1)


def _parse(ts) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


_START_RE = re.compile(r"NodePool\s+[\d.]+\s*启动")
_STOP_RE = re.compile(r"NodePool\s*已停止")


def build_abnormal_ranges(batches, events, logs, window_start: datetime,
                          now: datetime) -> list[tuple[datetime, datetime, str]]:
    """暂停 / 中断 / 停服区间。只有能确认的才排除，普通空档不猜。"""
    ranges: list[tuple[datetime, datetime, str]] = []
    starts = []
    for row in events or []:
        if row.get("kind") == "start":
            stamp = _parse(row.get("ts"))
            if stamp:
                starts.append(stamp)
    for row in logs or []:
        if _START_RE.search(row.get("message") or ""):
            stamp = _parse(row.get("ts"))
            if stamp:
                starts.append(stamp)

    run_rows = []
    for row in batches or []:
        start = _parse(row.get("started_at"))
        if not start:
            continue
        status = (row.get("status") or "").lower()
        finished = _parse(row.get("finished_at"))
        if status in ("stopped", "failed") and finished:
            ranges.append((start, finished, "batch"))
        elif status == "running" and not finished:
            nxt = next((ts for ts in sorted(starts) if ts > start), None)
            end = nxt or (now if now - start > timedelta(hours=6) else None)
            if end:
                ranges.append((start, end, "crash"))

    ordered = []
    for row in events or []:
        stamp = _parse(row.get("ts"))
        if stamp:
            ordered.append((stamp, row.get("kind")))
    for row in logs or []:
        stamp = _parse(row.get("ts"))
        message = row.get("message") or ""
        if not stamp:
            continue
        if _STOP_RE.search(message):
            ordered.append((stamp, "shutdown"))
        elif _START_RE.search(message):
            ordered.append((stamp, "start"))
    ordered.sort()

    open_pause = None
    open_stop = None
    for stamp, kind in ordered:
        if kind == "pause":
            open_pause = stamp
        elif kind in ("resume", "shutdown", "start") and open_pause:
            if stamp > open_pause:
                ranges.append((open_pause, stamp, "pause"))
            open_pause = None
        if kind == "shutdown":
            open_stop = stamp
        elif kind in ("start", "resume") and open_stop:
            if stamp > open_stop:
                ranges.append((open_stop, stamp, "shutdown"))
            open_stop = None
    if open_pause:
        ranges.append((open_pause, now, "pause"))
    if open_stop:
        ranges.append((open_stop, now, "shutdown"))

    merged: list[tuple[datetime, datetime, str]] = []
    for start, end, kind in sorted((max(s, window_start), e, k) for s, e, k in ranges):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            prev = merged[-1]
            priority = {"batch": 0, "crash": 1, "shutdown": 2, "pause": 3}
            kind = max((prev[2], kind), key=lambda item: priority.get(item, 0))
            merged[-1] = (prev[0], max(prev[1], end), kind)
        else:
            merged.append((start, end, kind))
    return merged


def is_excluded(stamp: datetime, ranges) -> bool:
    for start, end, _ in ranges:
        if start <= stamp < end:
            return True
    return False


def _day_points(points: list[sampler.SamplePoint], day: str) -> list[sampler.SamplePoint]:
    start, end = day_bounds(day)
    return [p for p in points if p.known and start <= p.time < end]


def _useful_snapshots(snaps: list[datetime], points: list[sampler.SamplePoint]) -> list[datetime]:
    if not points:
        return []
    useful = []
    for stamp in snaps:
        for point in points:
            if abs((stamp - point.time).total_seconds()) <= point.max_distance.total_seconds():
                useful.append(stamp)
                break
    return useful


def _density_filter(snaps: list[datetime], points: list[sampler.SamplePoint]) -> list[datetime]:
    """滑动 60 分钟窗口最多 2 份；超额时删"最冗余"的那份，而不是删最早的。"""
    kept = sorted(snaps)
    while True:
        violation = None
        for i, stamp in enumerate(kept):
            j = i
            while j + 1 < len(kept) and kept[j + 1] < stamp + DENSITY_WINDOW:
                j += 1
            if j - i + 1 > DENSITY_LIMIT:
                violation = (i, j)
                break
        if violation is None:
            return kept
        i, j = violation
        window = kept[i:j + 1]
        base = len(sampler.match_points(points, kept))
        best = None
        for cand in window:
            trial = [x for x in kept if x != cand]
            loss = base - len(sampler.match_points(points, trial))
            near = min((abs((cand - p.time).total_seconds()) for p in points), default=1e9)
            key = (loss, -near, -cand.timestamp())
            if best is None or key < best[0]:
                best = (key, cand)
        kept.remove(best[1])


def select_snapshots(stamps: list[datetime], points: list[sampler.SamplePoint],
                     excluded_ranges) -> Selection:
    """四阶段筛选：日门槛 → 密度 → 复核；返回保留快照与每日统计。"""
    ordered = sorted(stamps)
    by_day: dict[str, list[datetime]] = {}
    for stamp in ordered:
        by_day.setdefault(local_day(stamp), []).append(stamp)

    kept: list[datetime] = []
    stats: list[DayStat] = []
    for day in sorted(by_day):
        snaps = by_day[day]
        stat = DayStat(date=day, eligible=len(snaps))
        if len(snaps) < MIN_DAILY_SNAPSHOTS:
            stat.reason = "sparse"
            stats.append(stat)
            continue
        day_points = _day_points(points, day)
        usable = _useful_snapshots(snaps, day_points)
        # 与计划点完全无关的快照不可能被匹配，先剔除，不损失覆盖率
        kept_day = _density_filter(usable, day_points) if day_points else []
        stat.kept = len(kept_day)
        if len(kept_day) < MIN_DAILY_SNAPSHOTS:
            stat.reason = "density"
            stats.append(stat)
            continue
        kept.extend(kept_day)
        stats.append(stat)

    return Selection(kept=sorted(kept), day_stats=stats,
                     dropped_days=[d.date for d in stats if d.reason],
                     excluded_ranges=excluded_ranges)
