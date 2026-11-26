"""A 类维护态入口的公共实现：维护态判定 → 写入闸 / 连接池 / 排空。

v1.10 §2.7 规定五个 A 类入口都要调用 ``maintenance_state()``，不得各自读取
``maintenance.json``、不得复制判定逻辑。本模块把这套判定收敛成三个函数：

* :func:`write_reason` —— 把三态维护状态映射成拒绝原因（``None`` 表示放行）；
* :func:`apply_state` —— 由 A 类入口调用，非 INACTIVE 时置位 B 类写入闸；
* :func:`write_guard` —— 正式数据库写入入口的守卫（含 PRAGMA 检查）。

此外提供 §2.7 的停写排空（PT-16）：:func:`confirm_writes_blocked` 与 :func:`drain`。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from backend import config
from backend.database import backup, db, maint, pool
from backend.database.maint import MaintState

DRAIN_OK = "ok"
DRAIN_FAILED = "drain_failed"
DRAIN_MIN_INTERVAL = 10.0


def write_reason(state: MaintState) -> str | None:
    """三态维护状态 → 写入拒绝原因（INACTIVE 返回 ``None``）。"""
    if state is MaintState.ACTIVE:
        return pool.REASON_MAINTENANCE
    if state is MaintState.RECOVERY_REQUIRED:
        return pool.REASON_RECOVERY
    return None


def judge() -> tuple[MaintState, str | None]:
    """A 类入口统一判定：返回 (维护状态, 拒绝原因)。"""
    state = maint.maintenance_state()
    return state, write_reason(state)


def apply_state(state: MaintState) -> str | None:
    """把 A 类判定结果交给 B 类：非 INACTIVE 置位写入闸并标记连接池 dead。

    只封锁、不解除：解除封锁必须由维护流程显式调用 :func:`release`，
    以免 A 类入口在维护过程中途把闸门悄悄打开。
    """
    reason = write_reason(state)
    if reason is not None:
        pool.block_writes(reason)
    return reason


def release() -> None:
    """受控解除写入闸（仅供维护结束后的显式恢复使用，不推进迁移状态）。"""
    pool.unblock_writes()


def _pragma_ok(conn: sqlite3.Connection) -> tuple[bool, dict[str, int | None]]:
    values: dict[str, int | None] = {}
    for name in ("foreign_keys", "recursive_triggers"):
        try:
            values[name] = int(conn.execute(f"PRAGMA {name}").fetchone()[0])
        except Exception:
            values[name] = None
    return all(values.get(n) == 1 for n in ("foreign_keys", "recursive_triggers")), values


@contextmanager
def write_guard():
    """正式写入入口守卫：维护态拒绝 + 强制 PRAGMA 约束。

    * ACTIVE ⇒ ``WriteRefused("maintenance")``
    * RECOVERY_REQUIRED ⇒ ``WriteRefused("recovery_required")``
    * ``foreign_keys`` / ``recursive_triggers`` 非 ON ⇒ ``WriteRefused("pragmas_not_enabled")``
    """
    state, reason = judge()
    if reason is not None:
        apply_state(state)
        raise pool.WriteRefused(reason)
    conn = db.connect()
    ok, values = _pragma_ok(conn)
    if not ok:
        raise pool.WriteRefused("pragmas_not_enabled")
    yield conn


def guard_write() -> None:
    """无 yield 需要的调用点使用的轻量写法（判定 + 闸门，不建连接）。"""
    state, reason = judge()
    if reason is not None:
        apply_state(state)
        raise pool.WriteRefused(reason)


# --------------------------------------------------------------- PT-16 停写排空
def confirm_writes_blocked() -> tuple[bool, dict[str, Any]]:
    """排空判据 1～3：写入闸置位、业务写连接全部关闭、只读连接 query_only=1。"""
    return pool.confirm_writes_blocked()


def wal_checkpoint_truncate(db_path: str | Path | None = None) -> tuple[int, int, int] | None:
    """执行 ``PRAGMA wal_checkpoint(TRUNCATE)`` 并返回三元组（失败返回 ``None``）。

    这是写操作，调用方必须先确认写入闸已置位、业务写连接已全部关闭。
    """
    path = Path(db_path) if db_path else config.DB_PATH
    try:
        conn = sqlite3.connect(str(path), timeout=30)
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            return (int(row[0]), int(row[1]), int(row[2]))
        finally:
            conn.close()
    except Exception:
        return None


def external_holders(db_path: str | Path | None = None) -> list[str]:
    """仍持有 ``nodepool.db*`` 的外部进程（排除自身 pid）。"""
    path = Path(db_path) if db_path else config.DB_PATH
    return backup.holders_of(path)


@dataclass
class DrainResult:
    ok: bool
    code: str
    rounds: list[dict[str, Any]] = field(default_factory=list)
    detail: str = ""
    interval: float = DRAIN_MIN_INTERVAL

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "code": self.code, "rounds": self.rounds,
                "detail": self.detail, "interval": self.interval}


def _drain_round(db_path: str | Path,
                 checkpoint: Callable[[], tuple[int, int, int] | None],
                 holders: Callable[[], list[str]]) -> dict[str, Any]:
    blocked_ok, blocked_detail = confirm_writes_blocked()
    checkpoint_result = checkpoint() if blocked_ok else None
    checkpoint_ok = checkpoint_result == (0, 0, 0)
    holder_list = holders() if blocked_ok else []
    holders_ok = blocked_ok and not holder_list
    ok = blocked_ok and checkpoint_ok and holders_ok
    return {
        "ok": ok,
        "writes_blocked": blocked_detail,
        "checkpoint": list(checkpoint_result) if checkpoint_result is not None else None,
        "checkpoint_ok": checkpoint_ok,
        "holders": holder_list,
        "holders_ok": holders_ok,
    }


def drain(*, rounds: int = 2, min_interval: float = DRAIN_MIN_INTERVAL,
          clock: Callable[[], float] = time.monotonic,
          sleeper: Callable[[float], None] = time.sleep,
          checkpoint: Callable[[], tuple[int, int, int] | None] | None = None,
          holders: Callable[[], list[str]] | None = None,
          db_path: str | Path | None = None) -> DrainResult:
    """停写排空：四项判据连续 ``rounds`` 次满足且两次间隔 ≥ ``min_interval`` 秒。

    任一轮失败立即判定 ``drain_failed``，调用方据此走 ``halt_migration("drain_failed")``
    的安全拒绝路径（本阶段不实现状态机）。
    """
    path = Path(db_path) if db_path else config.DB_PATH
    checkpoint = checkpoint or (lambda: wal_checkpoint_truncate(path))
    holders = holders or (lambda: external_holders(path))
    results: list[dict[str, Any]] = []
    prev_end: float | None = None
    for index in range(max(1, rounds)):
        gap: float | None = None
        if prev_end is not None:
            gap = clock() - prev_end
            while gap < min_interval:
                sleeper(min_interval - gap)
                gap = clock() - prev_end
        result = _drain_round(path, checkpoint, holders)
        result["index"] = index
        if gap is not None:
            result["gap"] = round(gap, 3)
        results.append(result)
        if not result["ok"]:
            return DrainResult(False, DRAIN_FAILED, results,
                               f"第 {index + 1} 轮排空判据未通过", min_interval)
        prev_end = clock()
    return DrainResult(True, DRAIN_OK, results,
                       f"排空判据连续 {len(results)} 次满足，间隔 ≥ {min_interval:g} 秒",
                       min_interval)
