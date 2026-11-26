"""B 类二次封锁：写入闸、连接池存活标记、halt_tail 前置检查与排空判据。

本模块是 v1.10 §2.7「B 类」的唯一实现，遵守 A/B 职责分离：

* 不读取维护状态文件，不 import 维护状态模块，不做任何维护态判定；
* 只消费 A 类入口（HTTP 中间件、调度器、CLI、写入守卫、启动恢复）已经得出的
  结论，通过 :func:`block_writes` / :func:`unblock_writes` 生效。

并发约定（P-1.5 修复项 1）：写连接的 checkout/创建/登记与封锁操作共用同一把
``_blocking_lock``（见 :func:`write_slot`），因此不存在"检查时未封锁 → 封锁关闭
连接 → 另一个线程又新建并登记写连接"的窗口。
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

REASON_MAINTENANCE = "maintenance"
REASON_RECOVERY = "recovery_required"
HALT_TAIL_OK = "halt_tail"
HALT_SOURCE = "halt_migration"


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
    """``halt_tail`` 前置条件不满足：凭据不可信或未确认维护写入/写入封锁。

    ``code``：``not_confirmation`` / ``no_trusted_issuer`` / ``unconfirmed`` /
    ``untrusted_issuer_module`` / ``untrusted_issuer`` / ``unconfirmed_state``。
    """

    def __init__(self, code: str = "unconfirmed", detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


_ISSUER_TOKEN = object()                 # 模块私有；凭据必须携带它才可能有效
_TRUSTED_ISSUER_MODULE = "backend.database.migrate"
_ISSUERS: dict[int, str] = {}             # id(mint token) -> 发射器模块名；P-1.5 默认空


@dataclass(frozen=True)
class MaintConfirmation:
    """``halt_migration`` 回读确认后签发的凭据（``halt_tail`` 唯一接受的输入）。

    可信性由 ``_token``（模块私有哨兵）与 ``_issuer``（已注册的可信发射器）共同决定：
    直接实例化、复制字段、临时鸭子类型都不会带上哨兵，因此一律无效。
    有效凭据只能由 ``mint_confirmation()`` 签发，而签发必须先由
    ``backend.database.migrate`` 中的 ``halt_migration`` 完成
    ``register_confirmation_issuer()``（P-1.5 不注册任何发射器 ⇒ 失败关闭）。
    """

    migration_id: str
    maintenance_ok: bool
    writes_blocked: bool
    confirmed_at: float
    source: str = HALT_SOURCE
    _token: object = field(default=None, repr=False, compare=False)
    _issuer: str | None = field(default=None, repr=False, compare=False)

    def is_confirmed(self) -> bool:
        return (self._token is _ISSUER_TOKEN
                and self._issuer in set(_ISSUERS.values())
                and self.source == HALT_SOURCE
                and isinstance(self.migration_id, str) and self.migration_id.strip() != ""
                and self.maintenance_ok is True
                and self.writes_blocked is True
                and isinstance(self.confirmed_at, (int, float))
                and not isinstance(self.confirmed_at, bool))


def register_confirmation_issuer(issue: Any) -> object:
    """P-1.6 ``halt_migration`` 的注册入口：返回该发射器专属的签发令牌。

    只接受定义在 ``backend.database.migrate`` 里的可调用对象；其它模块调用会得到
    ``HaltPreconditionFailed("untrusted_issuer_module")``。P-1.5 不注册任何发射器，
    因此此刻 ``halt_tail()`` 必然失败关闭（没有可信确认来源，就不接受任何凭据）。
    """
    module = getattr(issue, "__module__", "")
    if module != _TRUSTED_ISSUER_MODULE:
        raise HaltPreconditionFailed(
            "untrusted_issuer_module", f"确认凭据只能由 {_TRUSTED_ISSUER_MODULE} 签发，got %r" % (module,))
    token = object()
    _ISSUERS[id(token)] = module
    return token


def mint_confirmation(token: Any, migration_id: str, maintenance_ok: bool,
                      writes_blocked: bool, *, now: float | None = None) -> MaintConfirmation:
    """用注册令牌签发凭据；未注册令牌或确认条件不为真时直接拒绝。"""
    if token is None or id(token) not in _ISSUERS:
        raise HaltPreconditionFailed("untrusted_issuer", "签发令牌未注册")
    if maintenance_ok is not True or writes_blocked is not True:
        raise HaltPreconditionFailed("unconfirmed_state",
                                     "maintenance_ok/writes_blocked 必须回读确认为 True")
    # writes_blocked 必须在签发时刻被真实证实（闸门置位、写连接全关、无在途可提交事务）
    if not confirm_writes_blocked()[0]:
        raise HaltPreconditionFailed("unconfirmed_state",
                                     "写入封锁尚未真实成立，拒绝签发确认凭据")
    return MaintConfirmation(migration_id=str(migration_id),
                             maintenance_ok=True,
                             writes_blocked=True,
                             confirmed_at=time.time() if now is None else float(now),
                             _token=_ISSUER_TOKEN,
                             _issuer=_ISSUERS[id(token)])


# 封锁与写连接 checkout 的共同同步边界（可重入，便于 block_writes 内部调用关闭）
_blocking_lock = threading.RLock()
_blocked_reason: str | None = None
_blocked_seq = 0                      # 每次"从未封锁 → 封锁"自增，供测试与诊断


# --------------------------------------------------------------------- 写入闸
@contextmanager
def write_slot() -> Iterator[None]:
    """写连接 checkout 的同步边界。

    在锁内复核写入闸：一旦 :func:`block_writes` 完成，任何并发 checkout 都在
    这里被拒（不会再创建或登记新的写连接）。只读连接不经过这里。
    """
    with _blocking_lock:
        reason = _blocked_reason
        if reason is not None:
            raise PoolClosed(reason)
        yield


def block_writes(reason: str = REASON_MAINTENANCE) -> str:
    """置位全局写入闸，并把连接池标记为 dead，同时关闭全部业务写连接。

    置位与"关闭现有写连接"在同一个锁内完成，与 :func:`write_slot` 互斥，
    因此封锁返回后不存在仍然存活的业务写连接。幂等：保留首次封锁原因。
    """
    global _blocked_reason, _blocked_seq
    with _blocking_lock:
        if _blocked_reason is None:
            _blocked_reason = reason
            _blocked_seq += 1
        current = _blocked_reason
        abort_inflight_transactions()      # 先标记中止在途事务，再关闭连接
        close_business_connections()
    return current


def ensure_blocked(reason: str = REASON_MAINTENANCE) -> str:
    """确保写入闸处于封锁态（不解除）；用于排空失败等"必须保持封锁"的路径。"""
    return block_writes(reason)


def unblock_writes() -> None:
    """解除写入闸（仅用于维护结束后的受控恢复；不会自动推进迁移状态）。"""
    global _blocked_reason
    with _blocking_lock:
        _blocked_reason = None


def writes_blocked() -> bool:
    return _blocked_reason is not None


def blocked_reason() -> str | None:
    return _blocked_reason


def blocked_sequence() -> int:
    """封锁世代号（用于诊断/测试，不代表任何维护态判定）。"""
    return _blocked_seq


def pool_dead() -> bool:
    """连接池是否 dead。与写入闸同源：封锁即 dead，解除即恢复。"""
    return writes_blocked()


def reset_for_tests() -> None:
    """测试专用：清空封锁标记与已注册的确认发射器（不关闭连接）。"""
    global _blocked_reason, _blocked_seq
    with _blocking_lock:
        _blocked_reason = None
        _blocked_seq = 0
        _ISSUERS.clear()


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


def abort_inflight_transactions() -> int:
    """标记中止全部在途写事务（由封锁流程在关闭连接之前调用）。"""
    from backend.database import db

    return db.abort_inflight_transactions()


def active_write_transactions() -> int:
    from backend.database import db

    return db.active_write_transactions()


def confirm_writes_blocked() -> tuple[bool, dict[str, Any]]:
    """排空判据 1～3：写入闸置位 + 业务写连接全部关闭 + 无仍可提交的在途事务 + 只读 query_only=1。"""
    blocked = writes_blocked()
    open_writes = open_write_connections()
    inflight = active_write_transactions()
    readonly_ok = readonly_query_only_ok()
    ok = blocked and open_writes == 0 and inflight == 0 and readonly_ok
    return ok, {
        "writes_blocked": blocked,
        "open_write_connections": open_writes,
        "active_write_transactions": inflight,
        "readonly_query_only": readonly_ok,
        "reason": blocked_reason(),
    }


# --------------------------------------------------------------------- halt_tail
def halt_tail(rec: Any) -> str:
    """停机收尾前置检查：只接受真实可信的 :class:`MaintConfirmation`。

    拒绝条件（任一命中即 :class:`HaltPreconditionFailed`，``code`` 标明原因）：

    * 不是 :class:`MaintConfirmation` → ``not_confirmation``；
    * 当前没有任何已注册的可信发射器（P-1.5 常态）→ ``no_trusted_issuer``；
    * 缺模块私有哨兵 / 发射器未注册 / 字段不完整 / 两个确认字段不为严格 ``True``
      → ``unconfirmed``。

    本函数不读维护文件、不查库、不推进任何迁移状态。
    """
    if not isinstance(rec, MaintConfirmation):
        raise HaltPreconditionFailed(
            "not_confirmation",
            "halt_tail 拒绝继续：入参不是 halt_migration 回读确认凭据（%r）"
            % (type(rec).__name__,))
    if not _ISSUERS:
        raise HaltPreconditionFailed(
            "no_trusted_issuer",
            "halt_tail 拒绝继续：当前没有可信确认来源（halt_migration 尚未注册发射器）")
    if not rec.is_confirmed():
        raise HaltPreconditionFailed(
            "unconfirmed",
            "halt_tail 前置条件不满足：source=%r issuer=%r migration_id=%r "
            "maintenance_ok=%r writes_blocked=%r"
            % (rec.source, rec._issuer, rec.migration_id, rec.maintenance_ok,
               rec.writes_blocked))
    return HALT_TAIL_OK
