"""B 类二次封锁：写入闸、连接池存活标记、halt_tail 前置检查与排空判据。

本模块是 v1.10 §2.7「B 类」的唯一实现，遵守 A/B 职责分离：

* 不读取维护状态文件，不 import 维护状态模块，不做任何维护态判定；
* 只消费 A 类入口（HTTP 中间件、调度器、CLI、写入守卫、启动恢复）已经得出的
  结论，通过 :func:`block_writes` / :func:`unblock_writes` 生效。

因此静态扫描下本文件不含任何维护文件读取或维护态判定的标识（PT-23）。
"""
from __future__ import annotations

import threading
from typing import Any

REASON_MAINTENANCE = "maintenance"
REASON_RECOVERY = "recovery_required"
HALT_TAIL_OK = "halt_tail"


class PoolClosed(RuntimeError):
    """连接池已被标记 dead：业务写连接不可再 checkout（维护 / 恢复中）。"""

    def __init__(self, reason: str = REASON_MAINTENANCE) -> None:
        super().__init__(f"connection pool closed: {reason}")
        self.reason = reason


class WriteRefused(RuntimeError):
    """数据库写入闸门拒绝写入。

    ``reason`` 取值：``maintenance`` / ``recovery_required`` /
    ``pragmas_not_enabled`` / ``writes_blocked``。
    """

    def __init__(self, reason: str = REASON_MAINTENANCE) -> None:
        super().__init__(f"write refused: {reason}")
        self.reason = reason


class HaltPreconditionFailed(RuntimeError):
    """``halt_tail`` 前置条件不满足：迁移未确认维护写入或写入封锁。"""


_lock = threading.RLock()
_blocked_reason: str | None = None


# --------------------------------------------------------------------- 写入闸
def block_writes(reason: str = REASON_MAINTENANCE) -> str:
    """置位全局写入闸，并把连接池标记为 dead，同时关闭全部业务写连接。

    由 A 类入口调用（幂等；重复调用不改变既有原因，保留首次封锁原因）。
    """
    global _blocked_reason
    with _lock:
        if _blocked_reason is None:
            _blocked_reason = reason
        current = _blocked_reason
    close_business_connections()
    return current


def unblock_writes() -> None:
    """解除写入闸（仅用于维护结束后的受控恢复；不会自动推进迁移状态）。"""
    global _blocked_reason
    with _lock:
        _blocked_reason = None


def writes_blocked() -> bool:
    with _lock:
        return _blocked_reason is not None


def blocked_reason() -> str | None:
    with _lock:
        return _blocked_reason


def pool_dead() -> bool:
    """连接池是否 dead。与写入闸同源：封锁即 dead，解除即恢复。"""
    return writes_blocked()


def reset_for_tests() -> None:
    """测试专用：清空封锁标记（不关闭连接）。"""
    global _blocked_reason
    with _lock:
        _blocked_reason = None


# ------------------------------------------------------------- 连接协作与排空
def close_business_connections() -> int:
    """关闭全部业务写连接，返回关闭个数（只读连接保持打开）。"""
    from backend.database import db  # 延迟导入，避免模块循环

    return db.close_write_connections()


def open_write_connections() -> int:
    from backend.database import db

    return db.open_write_connections()


def readonly_query_only_ok() -> bool:
    from backend.database import db

    return db.readonly_query_only_ok()


def confirm_writes_blocked() -> tuple[bool, dict[str, Any]]:
    """排空判据 1～3 的前两项：写入闸置位 + 业务写连接全部关闭 + 只读连接 query_only=1。"""
    blocked = writes_blocked()
    open_writes = open_write_connections()
    readonly_ok = readonly_query_only_ok()
    ok = blocked and open_writes == 0 and readonly_ok
    return ok, {
        "writes_blocked": blocked,
        "open_write_connections": open_writes,
        "readonly_query_only": readonly_ok,
        "reason": blocked_reason(),
    }


# --------------------------------------------------------------------- halt_tail
def halt_tail(rec: Any) -> str:
    """停机收尾前置检查：只接受已回读确认的迁移记录，不读维护文件。

    ``rec`` 必须是 ``halt_migration`` 已回读确认的记录对象，需同时具备
    ``maintenance_ok`` 与 ``writes_blocked`` 且均为真，才允许进入后续安全收尾。
    """
    maintenance_ok = getattr(rec, "maintenance_ok", None)
    writes_blocked_flag = getattr(rec, "writes_blocked", None)
    if maintenance_ok is True and writes_blocked_flag is True:
        return HALT_TAIL_OK
    raise HaltPreconditionFailed(
        "halt_tail 前置条件不满足：maintenance_ok=%r writes_blocked=%r"
        % (maintenance_ok, writes_blocked_flag)
    )
