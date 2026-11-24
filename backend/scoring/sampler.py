"""动态采样：按检测计划生成理论采样点，再用全局一对一匹配挑快照。

评分只看"计划要求的样本有没有被真实检测覆盖"，不看检测次数。
检测频率变了就分段生成，历史计划不会被当前配置覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

SCORE_WINDOW_DAYS = 7
MAX_MATCH_MINUTES = 15.0


@dataclass(frozen=True)
class Segment:
    """一段时间里生效的检测计划；known=False 表示没有历史记录，无法还原。"""
    start: datetime
    end: datetime
    interval_minutes: float
    known: bool = True


@dataclass(frozen=True)
class SamplePoint:
    time: datetime
    interval_minutes: float
    known: bool = True

    @property
    def max_distance(self) -> timedelta:
        """容忍窗口：最多 15 分钟，且不超过半个检测周期。

        真实批次是按"距上次检测满间隔才开跑"触发的，批次本身还要跑几分钟，
        实际节奏会比计划晚一点并逐轮累积偏移；用 1/4 周期（30 分钟档只有 7.5 分钟）
        会把"确实测过、只是晚了几分钟"的样本判成缺失，覆盖率被无谓压低。
        半个周期既不会抢邻档的样本，也能吸收这种偏移。
        """
        return timedelta(minutes=min(MAX_MATCH_MINUTES, self.interval_minutes / 2))


def build_segments(plan: list[tuple[datetime, int]], window_start: datetime,
                   window_end: datetime) -> list[Segment]:
    """把计划变更记录切成区间；窗口起点早于第一条记录时，那段标为未知。"""
    rows = sorted((dt, int(iv)) for dt, iv in plan if int(iv) >= 0)
    interval = None
    for changed_at, iv in rows:
        if changed_at <= window_start:
            interval = iv
    segments: list[Segment] = []
    cursor = window_start
    for changed_at, iv in rows:
        if changed_at <= window_start or changed_at >= window_end:
            continue
        segments.append(Segment(cursor, changed_at,
                                interval if interval is not None else iv,
                                known=interval is not None))
        cursor = changed_at
        interval = iv
    if cursor < window_end:
        segments.append(Segment(cursor, window_end,
                                interval if interval is not None else 30,
                                known=interval is not None))
    return [s for s in segments if s.start < s.end]


def generate_points(segments: list[Segment], snapshot_times: list[datetime],
                    window_end: datetime) -> list[SamplePoint]:
    """每个区间按自己的间隔铺采样点，并把相位对齐到该区间最早的真实快照。"""
    points: list[SamplePoint] = []
    ordered_snapshots = sorted(snapshot_times)
    for segment in segments:
        interval = timedelta(minutes=segment.interval_minutes)
        if interval <= timedelta(0):
            continue
        anchor = next((ts for ts in ordered_snapshots
                       if segment.start <= ts < segment.end), None)
        anchor = anchor or segment.start
        cursor = anchor
        earliest = ordered_snapshots[0] if ordered_snapshots else None
        # 相位对齐到真实快照后，最多回退一个间隔把边界点补回来；
        # 但绝不早于项目产生第一份快照的时刻，避免把"尚未运行"算成缺样本。
        while (cursor - interval >= segment.start
               and (earliest is None or cursor - interval >= earliest)):
            cursor -= interval
        while cursor < segment.end and cursor <= window_end:
            points.append(SamplePoint(cursor, segment.interval_minutes, segment.known))
            cursor += interval
    points.sort(key=lambda point: point.time)
    return points


def match_points(points: list[SamplePoint], snapshots: list[datetime]) -> list[tuple[int, int]]:
    """全局一对一匹配：先最大化匹配数量，再让总时间距离最小。

    时间轴上距离满足 Monge 性质，因此"不交叉"的单调匹配就是全局最优；
    用动态规划求解，同一距离优先选更早的快照。
    """
    pts = sorted(range(len(points)), key=lambda i: points[i].time)
    snaps = sorted(range(len(snapshots)), key=lambda j: snapshots[j])
    snap_times = [snapshots[j] for j in snaps]
    n, m = len(pts), len(snaps)
    if not n or not m:
        return []
    missing_penalty = 1_000_000.0
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back = [[0] * (m + 1) for _ in range(n + 1)]   # 0=skip point 1=skip snapshot 2=match
    for j in range(m + 1):
        dp[0][j] = 0.0
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + missing_penalty
        back[i][0] = 0
    for i in range(1, n + 1):
        point = points[pts[i - 1]]
        max_dist = point.max_distance
        for j in range(1, m + 1):
            best = dp[i - 1][j] + missing_penalty
            move = 0
            if dp[i][j - 1] < best:
                best = dp[i][j - 1]
                move = 1
            distance = abs((point.time - snap_times[j - 1]).total_seconds())
            if distance <= max_dist.total_seconds():
                candidate = dp[i - 1][j - 1] + distance
                # 同样最优时优先较早的快照：给后出现的快照加极小权重
                candidate += j * 1e-6
                if candidate < best:
                    best = candidate
                    move = 2
            dp[i][j] = best
            back[i][j] = move
    result: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 and j > 0:
        move = back[i][j]
        if move == 2:
            result.append((pts[i - 1], snaps[j - 1]))
            i -= 1
            j -= 1
        elif move == 1:
            j -= 1
        else:
            i -= 1
    result.reverse()
    return result


def data_status(maturity_days: float, coverage: float | None) -> str:
    """数据状态：采集中 / 初步 / 参考 / 正常 / 数据不足 / 未知。"""
    if maturity_days < 1:
        return "collecting"
    if coverage is None:
        return "unknown"
    if maturity_days < 2:
        return "tentative" if coverage >= 0.6 else "insufficient"
    if maturity_days < 7:
        return "reference" if coverage >= 0.6 else "insufficient"
    if coverage >= 0.8:
        return "normal"
    if coverage >= 0.6:
        return "reference"
    return "insufficient"


def status_rank(status: str) -> int:
    """正式排名 0、参考排名 1、不参与 2。"""
    return {"ample": 0, "normal": 0, "reference": 1, "rough": 1,
            "unknown": 1, "insufficient": 2}.get(status, 2)
