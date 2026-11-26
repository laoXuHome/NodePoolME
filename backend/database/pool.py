"""B 类二次封锁：写入闸、连接池存活标记、halt_tail 前置检查与排空判据。

本模块是 v1.10 §2.7「B 类」的唯一实现，遵守 A/B 职责分离：

* 不读取维护状态文件，不 import 维护状态模块，不做任何维护态判定；
* 只消费 A 类入口（HTTP 中间件、调度器、CLI、写入守卫、启动恢复）已经得出的
  结论，通过 :func:`block_writes` / :func:`unblock_writes` 生效。

并发约定（P-1.5 修复项 1）：写连接的 checkout/创建/登记、写事务的提交，与封锁
操作全部共用同一把 ``_blocking_lock``（见 :func:`write_slot` / :func:`commit_slot`），
因此不存在"检查时未封锁 → 封锁关闭连接 → 另一个线程又新建并登记写连接"的窗口，
也不存在"提交许可复核通过 → 封锁置位 → 该事务仍然 commit 成功"的窗口。

锁获取顺序（固定，不得反向，避免死锁）：

1. ``_blocking_lock``（可重入）→ 2. ``_INFLIGHT_LOCK`` / ``_ISSUER_LOCK``。
持有 ``_INFLIGHT_LOCK`` 或 ``_ISSUER_LOCK`` 时不申请 ``_blocking_lock``。

另有一个不取锁的"封锁意图"标记 ``_block_intent``（在取得 ``_blocking_lock`` 之前
置位，见 :func:`block_writes`）：正在提交临界区内的事务若在复核时看到它，就拒绝
提交，而不是"等封锁释放锁之后再把写入落库"。
"""
from __future__ import annotations

import sys
import threading
import time
import types
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
    ``untrusted_issuer_module`` / ``untrusted_issuer_identity`` /
    ``issuer_identity_conflict`` / ``issuer_replaced`` /
    ``untrusted_issuer`` / ``unconfirmed_state``。
    """

    def __init__(self, code: str = "unconfirmed", detail: str | None = None) -> None:
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


_ISSUER_TOKEN = object()                 # 模块私有；凭据必须携带它才可能有效
_TRUSTED_ISSUER_MODULE = "backend.database.migrate"
_TRUSTED_ISSUER_FUNC = "halt_migration"
_ISSUER_ID = f"{_TRUSTED_ISSUER_MODULE}:{_TRUSTED_ISSUER_FUNC}"
_ISSUER_LOCK = threading.Lock()


@dataclass(frozen=True)
class _IssuerRecord:
    """已注册发射器的登记项：**同时保存令牌对象与发射器函数对象**。

    * ``token``：签发令牌本体（不是 id 影子）；
    * ``function``：实际发射器函数对象（**强引用**，防止对象被回收后 id 复用）；
    * ``issuer``：发射器身份标识（``模块:函数``，仅作诊断，不单独作为信任依据）。

    三项一律用 ``is`` 做对象身份比对：id 相同但引用不同（对象被替换 / id 复用 /
    同名函数重新定义）全部视为不匹配。
    """

    token: object
    issuer: str
    function: Any
    registered_at: float


_ISSUERS: dict[int, _IssuerRecord] = {}   # id(token) -> 登记项；P-1.5 默认空


@dataclass(frozen=True)
class MaintConfirmation:
    """``halt_migration`` 回读确认后签发的凭据（``halt_tail`` 唯一接受的输入）。

    可信性由四个条件同时成立决定，缺一即无效：

    1. ``_token`` 是模块私有哨兵（直接实例化、复制字段、子类都拿不到）；
    2. ``_issuer_token`` 与注册表中**登记的那个令牌是同一对象**（身份，不是 id/名字）；
    3. ``_issuer`` 等于该登记项记录的身份标识；
    4. 该登记项绑定的**发射器函数对象**仍是模块里当前的定义（``模块.halt_migration``
       就是登记时的那个对象）——同名函数被替换、登记项被改写都会让凭据立即失效。

    有效凭据只能由 ``mint_confirmation()`` 用已注册令牌签发，而注册要求发射器是
    ``backend.database.migrate`` 模块内定义的 ``halt_migration``（模块级
    ``__globals__`` 身份比对，改 ``__module__`` 字符串无法冒充）。
    P-1.5 不注册任何发射器 ⇒ ``halt_tail()`` 失败关闭。
    """

    migration_id: str
    maintenance_ok: bool
    writes_blocked: bool
    confirmed_at: float
    source: str = HALT_SOURCE
    _token: object = field(default=None, repr=False, compare=False)
    _issuer: str | None = field(default=None, repr=False, compare=False)
    _issuer_token: object = field(default=None, repr=False, compare=False)

    def is_confirmed(self) -> bool:
        if self._token is not _ISSUER_TOKEN:
            return False
        record = _lookup_token(self._issuer_token)
        if record is None or record.issuer != self._issuer:
            return False
        if not _record_binding_live(record):
            return False
        return (self.source == HALT_SOURCE
                and isinstance(self.migration_id, str) and self.migration_id.strip() != ""
                and self.maintenance_ok is True
                and self.writes_blocked is True
                and isinstance(self.confirmed_at, (int, float))
                and not isinstance(self.confirmed_at, bool))


def _lookup_token(token: Any) -> _IssuerRecord | None:
    """按**对象身份**查已注册令牌（id 与引用必须同时对应，否则视为未注册）。"""
    if token is None:
        return None
    with _ISSUER_LOCK:
        record = _ISSUERS.get(id(token))
        if record is None or record.token is not token:
            return None
        return record


def _record_binding_live(record: _IssuerRecord) -> bool:
    """登记项绑定的发射器函数对象是否仍是模块里当前的那个对象。

    同名函数被重新定义 / 模块属性被替换 / 登记项被改写成别的函数对象，都会让
    这里返回 ``False``：令牌、登记记录、发射器对象三者必须同时是原配。
    """
    module = sys.modules.get(_TRUSTED_ISSUER_MODULE)
    if module is None:
        return False
    current = getattr(module, _TRUSTED_ISSUER_FUNC, None)
    return isinstance(record.function, types.FunctionType) and current is record.function


def _has_issuer() -> bool:
    with _ISSUER_LOCK:
        return bool(_ISSUERS)


def register_confirmation_issuer(issue: Any) -> object:
    """P-1.6 ``halt_migration`` 的受控注册入口：返回该发射器专属的签发令牌。

    信任依据不是"可被任意赋值的 ``__module__`` 字符串"，而是函数对象与模块的
    真实绑定：``issue.__globals__`` 必须**就是**
    ``sys.modules["backend.database.migrate"].__dict__``（函数的 ``__globals__``
    是只读属性，普通调用方无法把它指到别处），并且函数名必须是 ``halt_migration``。

    * 不是函数 → ``untrusted_issuer_identity``；
    * ``__module__`` 不是该模块（典型：测试/业务代码里的函数）→ ``untrusted_issuer_module``；
    * 不在该模块命名空间内 / 函数名不是 ``halt_migration`` → ``untrusted_issuer_identity``；
    * 同一身份串下传入的是**另一个函数对象**（同名函数被重新定义）→
      ``issuer_identity_conflict``，绝不把原令牌交出去。

    同一**函数对象**重复注册是幂等的：返回**同一个**令牌对象（不会产生第二个可用令牌）。
    P-1.5 不注册任何发射器，因此此刻 ``halt_tail()`` 必然失败关闭。
    """
    if not isinstance(issue, types.FunctionType):
        raise HaltPreconditionFailed(
            "untrusted_issuer_identity",
            "确认发射器必须是函数，got %r" % (type(issue).__name__,))
    module = sys.modules.get(_TRUSTED_ISSUER_MODULE)
    if module is None or getattr(issue, "__module__", "") != _TRUSTED_ISSUER_MODULE:
        raise HaltPreconditionFailed(
            "untrusted_issuer_module",
            f"确认凭据只能由 {_TRUSTED_ISSUER_MODULE} 签发，got %r"
            % (getattr(issue, "__module__", ""),))
    if getattr(issue, "__globals__", None) is not module.__dict__:
        raise HaltPreconditionFailed(
            "untrusted_issuer_identity",
            "发射器不在该模块的全局命名空间内（仅伪造 __module__ 不能冒充可信发射器）")
    if getattr(issue, "__name__", "") != _TRUSTED_ISSUER_FUNC:
        raise HaltPreconditionFailed(
            "untrusted_issuer_identity",
            f"只有 {_TRUSTED_ISSUER_MODULE}.{_TRUSTED_ISSUER_FUNC} 可以注册为确认发射器")
    with _ISSUER_LOCK:
        for record in _ISSUERS.values():
            if record.issuer != _ISSUER_ID:
                continue
            if record.function is issue:
                return record.token          # 幂等：同一函数对象永远拿到同一个令牌
            raise HaltPreconditionFailed(
                "issuer_identity_conflict",
                "同一发射器身份下传入的是另一个函数对象（同名函数已被替换），"
                "拒绝把原令牌交给新对象")
        token = object()
        _ISSUERS[id(token)] = _IssuerRecord(token=token, issuer=_ISSUER_ID,
                                            function=issue,
                                            registered_at=time.time())
        return token


def mint_confirmation(token: Any, migration_id: str, maintenance_ok: bool,
                      writes_blocked: bool, *, now: float | None = None) -> MaintConfirmation:
    """用注册令牌签发凭据；未注册令牌或确认条件不为真时直接拒绝。

    令牌校验是对象身份校验（``record.token is token``）：另一个令牌对象、
    ``id`` 相同但已被替换的登记项、纯字符串/模块名冒充一律 ``untrusted_issuer``。

    另外还要校验登记项绑定的**发射器函数对象**仍是模块里当前的那个对象：
    同名函数被重新定义、登记项被改写成别的函数对象时，令牌即便"原本是真的"、
    也不再对应原发射器 ⇒ ``issuer_replaced``，绝不用它签发凭据。
    """
    record = _lookup_token(token)
    if record is None:
        raise HaltPreconditionFailed("untrusted_issuer",
                                     "签发令牌未注册，或与已注册令牌不是同一对象")
    if not _record_binding_live(record):
        raise HaltPreconditionFailed("issuer_replaced",
                                     "令牌、登记记录与发射器函数对象的绑定已被替换，拒绝签发")
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
                             _issuer=record.issuer,
                             _issuer_token=record.token)


# 封锁与写连接 checkout / 写事务提交的共同同步边界（可重入，便于 block_writes 内部调用关闭）
_blocking_lock = threading.RLock()
_blocked_reason: str | None = None
_block_intent = False                 # 封锁意图：在取得 _blocking_lock 之前置位
_blocked_seq = 0                      # 每次"从未封锁 → 封锁"自增，供测试与诊断


# --------------------------------------------------------------------- 写入闸
@contextmanager
def write_slot() -> Iterator[None]:
    """写连接 checkout 的同步边界。

    在锁内复核写入闸：一旦 :func:`block_writes` 完成（或已置位封锁意图），任何并发
    checkout 都在这里被拒（不会再创建或登记新的写连接）。只读连接不经过这里。
    """
    with _blocking_lock:
        reason = _blocked_reason
        if reason is not None:
            raise PoolClosed(reason)
        if _block_intent:
            raise PoolClosed(REASON_MAINTENANCE)
        yield


@contextmanager
def commit_slot() -> Iterator[None]:
    """写事务"登记 / 提交许可复核 + commit"的同步边界（与 :func:`block_writes` 同锁）。

    语义保证：本上下文内的复核与 ``conn.commit()`` 之间不会插入一次封锁——

    * 封锁先完成 ⇒ 这里在入口就以 ``WriteRefused("writes_blocked")`` 拒绝；
    * 提交先取得临界区 ⇒ 封锁必须等本上下文退出（``commit()`` 已返回）才可能返回；
    * 封锁请求在本上下文执行期间到达 ⇒ 意图标记已置位，复核会拒绝提交，而封锁
      仍要等本上下文退出才返回。

    因此 :func:`block_writes` 返回时，不存在"复核已通过但还没 commit"的事务。
    """
    with _blocking_lock:
        if _blocked_reason is not None or _block_intent:
            raise WriteRefused("writes_blocked")
        yield


def block_writes(reason: str = REASON_MAINTENANCE) -> str:
    """置位全局写入闸，并把连接池标记为 dead，同时关闭全部业务写连接。

    置位与"中止在途事务 + 关闭现有写连接"在同一个锁内完成，与 :func:`write_slot`
    / :func:`commit_slot` 互斥，因此封锁返回后不存在仍然存活的业务写连接，也不存在
    仍可提交的写事务。封锁意图在取锁之前置位，让"已在提交临界区内"的事务在复核时
    直接拒绝提交（而不是等锁再落库）。幂等：保留首次封锁原因。
    """
    global _blocked_reason, _blocked_seq, _block_intent
    _block_intent = True                    # 先声明意图（不取锁），再取锁完成封锁
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
    global _blocked_reason, _block_intent
    with _blocking_lock:
        _blocked_reason = None
        _block_intent = False


def block_pending() -> bool:
    """是否已声明封锁（含"封锁进行中"这一段：意图已置位、闸门可能尚未落下）。"""
    return _block_intent or _blocked_reason is not None


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
    """测试专用：清空封锁标记、封锁意图与已注册的确认发射器（不关闭连接）。"""
    global _blocked_reason, _blocked_seq, _block_intent
    with _blocking_lock:
        _blocked_reason = None
        _blocked_seq = 0
        _block_intent = False
        with _ISSUER_LOCK:             # 锁序：_blocking_lock → _ISSUER_LOCK
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
    * 缺模块私有哨兵 / 令牌对象与登记项不是同一对象 / 发射器身份不符 / 字段不完整
      / 两个确认字段不为严格 ``True`` → ``unconfirmed``。

    本函数不读维护文件、不查库、不推进任何迁移状态。
    """
    if not isinstance(rec, MaintConfirmation):
        raise HaltPreconditionFailed(
            "not_confirmation",
            "halt_tail 拒绝继续：入参不是 halt_migration 回读确认凭据（%r）"
            % (type(rec).__name__,))
    if not _has_issuer():
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
