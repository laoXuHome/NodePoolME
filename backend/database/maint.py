"""维护状态文件与 STATE_DIR（P-1.4 / 计划 §2.6、总体设计 §2.1「maintenance 持久化契约」）。

本模块是 maintenance 状态**唯一**的读取与写入实现：

    MAINT_FILE                        <STATE_DIR>/maintenance.json
    MaintRead / MaintReadResult       四态读取结果（ABSENT / PRESENT / CORRUPT / UNREADABLE）
    MaintState                        三态维护状态（ACTIVE / INACTIVE / RECOVERY_REQUIRED）
    read_maint_file()                 四态读取层：缺失、损坏、不可读必须是不同结果
    maintenance_state()               唯一维护态判定入口（内含唯一一份 migration_incomplete 查询）
    migration_incomplete()            唯一允许的「读库判断迁移是否未完成」实现
    confirm_maint_write(rec)          写入确认：只回答「我刚写的这一份是否落盘」
    set_maintenance(rec)              原子写：tmp → flush → 文件 fsync → os.replace → 目录 fsync → 回读确认
    selfcheck_state_dir()             启动自检（每次启动一次、结果缓存；失败只禁用迁移）

红线（v1.10 已批准语义，不得放宽）：
* 只有 ``FileNotFoundError`` 才算「明确不存在」；权限错误与其他 I/O 异常一律
  ``UNREADABLE``，**不得**伪装成缺失。
* ``maintenance`` 只认 JSON 真布尔（``type(v) is bool``）；``"false"``/``0``/``null``/
  键缺失一律非法。
* ``true`` 与 ``false`` **都必须**先过结构自洽校验；结构失败一律
  ``RECOVERY_REQUIRED`` 且**不查库**。
* ``CORRUPT`` / ``UNREADABLE`` 一律直接 ``RECOVERY_REQUIRED``、**不查库**。
* 文件缺失**永远不得**单独推出 ``INACTIVE``。
* 本模块不写业务库、不建表、不 checkpoint；读库一律 ``mode=ro``。
"""
from __future__ import annotations

import errno
import json
import logging
import math
import os
import secrets
import sqlite3
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from typing import Any, NamedTuple

from backend import config

log = logging.getLogger("nodepool.maint")

# --- 路径常量 ----------------------------------------------------------------

MAINT_FILENAME = "maintenance.json"

# 模块级镜像，便于测试在临时目录上构造（运行时一律按模块全局名取值）。
STATE_DIR = config.STATE_DIR
MAINT_FILE = Path(STATE_DIR) / MAINT_FILENAME

# 迁移 phase 名集合。与总体设计 §3.1 的 ``Phase`` 逐项对应；``migrate.py`` 补齐
# ``Phase`` 后，本集合必须与 ``{p.name for p in Phase}`` 完全一致（测试钉死）。
PHASE_NAMES = frozenset(
    {
        "PREPARED",
        "TX_OPEN",
        "TX_ROLLED_BACK",
        "COMMIT_ATTEMPTED",
        "COMMITTED",
        "UNKNOWN",
    }
)

# 稳定错误标识（供日志与验收引用，不随文案变化）
ERR_STATE_DIR_EQUALS_DATA_DIR = "state_dir_equals_data_dir"
ERR_STATE_DIR_CROSS_DEVICE = "state_dir_cross_device"
ERR_MAINT_FS_UNSUPPORTED = "maint_fs_unsupported"

_PROBE_STEPS = (
    "mkdir",
    "write",
    "file_fsync",
    "rename",
    "dir_fsync",
    "readback",
    "unlink",
    "dir_fsync_after_unlink",
)


# --- 状态与异常 --------------------------------------------------------------


class MaintState(Enum):
    """三态维护状态。禁止任何读取者再用裸 True/False 表达维护态。"""

    ACTIVE = auto()             # 文件有效 ∧ 确认处于维护态
    INACTIVE = auto()           # 确认不处于维护态（依据是 DB 佐证，不是「文件不在」）
    RECOVERY_REQUIRED = auto()  # 缺失/损坏/无法确认，或 DB 存在非 success 迁移行 → 保守恢复


class MaintUnverifiable(Exception):
    """无法确认迁移是否未完成（库打不开/损坏/超时）。调用方必须转 RECOVERY_REQUIRED。"""


class MaintWriteError(RuntimeError):
    """maintenance 文件写入失败。``reason`` 是稳定的机器可读码。"""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason}: {detail}" if detail else reason)


class MaintRead(Enum):
    ABSENT = auto()      # 明确不存在（FileNotFoundError）——唯一允许进入 DB 判定的读取结果
    PRESENT = auto()     # 存在 ∧ UTF-8 可解码 ∧ JSON 合法 ∧ 顶层是对象
    CORRUPT = auto()     # 存在但不可用：解码/解析失败、顶层非对象
    UNREADABLE = auto()  # 存在性未知：权限错误或其他 I/O 异常（EIO/EBUSY/路径是目录/超时）


class MaintReadResult(NamedTuple):
    status: MaintRead
    blob: dict | None    # 仅 PRESENT 时非 None


# --- 四态读取 ----------------------------------------------------------------


def _read_maint_bytes(target: Path) -> bytes:
    """读文件字节。独立一层便于测试注入 PermissionError / OSError。"""
    return target.read_bytes()


def read_maint_file() -> MaintReadResult:
    """四态读取层：缺失、损坏、不可读必须是不同结果。

    只有 ``ABSENT`` 可以由上层转入 DB 判定；``CORRUPT``/``UNREADABLE`` 一律不得
    伪装成「缺失」。
    """
    target = Path(MAINT_FILE)
    try:
        raw = _read_maint_bytes(target)
    except FileNotFoundError:                      # 目录项不存在：真缺失
        return MaintReadResult(MaintRead.ABSENT, None)
    except PermissionError as e:                   # 存在但无读权限
        log.warning("maint_read_permission_denied path=%s err=%r", target, e)
        return MaintReadResult(MaintRead.UNREADABLE, None)
    except OSError as e:                           # EIO/EBUSY/路径是目录/超时/网络盘断连
        log.warning("maint_read_io_error path=%s errno=%s err=%r", target,
                    getattr(e, "errno", None), e)
        return MaintReadResult(MaintRead.UNREADABLE, None)
    try:                                           # 先判编码再判 JSON，避免混合失败归因
        text = raw.decode("utf-8")
        obj = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        log.warning("maint_parse_failed path=%s err=%r", target, e)
        return MaintReadResult(MaintRead.CORRUPT, None)      # 截断/乱码：损坏，不是缺失
    if type(obj) is not dict:                      # 顶层必须是对象（数组/字符串/数字/null 均非法）
        log.warning("maint_root_not_object path=%s type=%s", target, type(obj).__name__)
        return MaintReadResult(MaintRead.CORRUPT, None)
    return MaintReadResult(MaintRead.PRESENT, obj)


# --- 结构自洽校验 ------------------------------------------------------------

_MISSING = object()  # 区分「键不存在」与「键值为 null」


def since_sane(v: Any) -> bool:
    """``since`` 数值检查：int 与 float 分开处理，绝不抛出。

    * ``type(v) is int``   ：整数天然有限，只判 ``v > 0``（不调 ``math.isfinite``，
      否则 ``10**1000`` 这类任意精度大整数会触发 ``OverflowError``）
    * ``type(v) is float`` ：必须 ``isfinite`` ∧ ``> 0``（排除 NaN/±Inf/0/负数）
    * 其他类型（含 ``bool``）一律非法
    """
    if type(v) is int:
        return v > 0
    if type(v) is float:
        try:
            return math.isfinite(v) and v > 0
        except (OverflowError, ValueError, TypeError):
            return False
    return False


def maint_fields_sane(blob: Any) -> bool:
    """结构自洽校验：``maintenance`` 为 True 或 False 都必须先过这一关。

    - ``migration_id``：非空 str
    - ``since``：正的有限数值（见 ``since_sane``）
    - ``phase``：str 且 ∈ ``PHASE_NAMES``

    本函数只返回 True/False，**不抛出**（含 OverflowError）。
    """
    try:
        if type(blob) is not dict:
            return False
        mid, since, phase = (blob.get("migration_id"), blob.get("since"), blob.get("phase"))
        return (isinstance(mid, str) and mid.strip() != ""
                and since_sane(since)
                and isinstance(phase, str) and phase in PHASE_NAMES)
    except Exception:                              # 最后一道：结构检查永不冒泡
        return False


# --- 唯一查库判定 ------------------------------------------------------------


def _connect_readonly() -> sqlite3.Connection:
    """只读连接：``mode=rw`` + ``query_only=1``，能看到 WAL 中未 checkpoint 的页。

    不用 ``mode=ro``：WAL 活库里已提交但尚未 checkpoint 的页可能不可见（实测
    会出现漏行）。``mode=rw`` 不会创建不存在的库；写入由 ``query_only`` 拒绝。
    """
    path = Path(config.DB_PATH).resolve()
    conn = sqlite3.connect(f"file:{path}?mode=rw", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _schema_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    return row is not None


def migration_incomplete() -> bool:
    """唯一允许的「读 DB 判断迁移是否未完成」实现（五个读取者不得各写一份）。

    - 表不存在（迁移从未开始）⇒ ``False``
    - 存在任一 ``status <> 'success'`` 行 ⇒ ``True``
    - 无法查询（打不开/损坏/超时）⇒ 抛 ``MaintUnverifiable``（由调用方转保守恢复）
    """
    try:
        conn = _connect_readonly()
        try:
            if not _schema_table_exists(conn):
                return False
            return conn.execute(
                "SELECT 1 FROM schema_migrations WHERE status <> 'success' LIMIT 1"
            ).fetchone() is not None
        finally:
            conn.close()
    except MaintUnverifiable:
        raise
    except Exception as e:
        raise MaintUnverifiable(repr(e)) from e


def judge_by_migration_rows() -> MaintState:
    """查库取结论：唯一封装 ``migration_incomplete()`` 的判定分支。"""
    try:
        return (MaintState.RECOVERY_REQUIRED if migration_incomplete()
                else MaintState.INACTIVE)
    except MaintUnverifiable:
        return MaintState.RECOVERY_REQUIRED          # 无法确认 ≠ 正常


def maintenance_state() -> MaintState:
    """唯一维护态判定入口：A 类五个直接读取者只准调这一个函数。

    查库只有两条入口：① ``ABSENT``（确认不存在）；② ``PRESENT`` ∧
    ``maintenance is False`` ∧ 结构合法。损坏/不可读/结构不自洽一律不查库、
    直接保守恢复。
    """
    rd = read_maint_file()

    if rd.status is MaintRead.UNREADABLE:            # 权限/IO：存在性未知
        return MaintState.RECOVERY_REQUIRED          # 不查库（不伪装成缺失）
    if rd.status is MaintRead.CORRUPT:               # 存在但不可用
        return MaintState.RECOVERY_REQUIRED          # 不查库（不因 DB 全 success 而放行）

    if rd.status is MaintRead.ABSENT:                # 真缺失：必须由 DB 佐证
        return judge_by_migration_rows()

    # PRESENT：先布尔，再结构；两步都过才有资格分支
    blob = rd.blob or {}
    m = blob.get("maintenance", _MISSING)
    if type(m) is not bool:                          # "false"/"true"/0/1/null/缺失 均非法
        return MaintState.RECOVERY_REQUIRED
    if not maint_fields_sane(blob):                  # True 与 False 都要先过结构校验
        return MaintState.RECOVERY_REQUIRED          # 结构失败不查库
    if m is True:
        return MaintState.ACTIVE                     # True 分支不查库
    return judge_by_migration_rows()                 # False 分支：查库一次


# --- 写入 --------------------------------------------------------------------


def _st_dev(path: Any) -> int:
    """取路径的 ``st_dev``。**唯一**的设备号取值点，便于测试精确注入。

    这里不吞异常：调用方必须区分 ``FileNotFoundError``（文件确实不存在）
    与其他 ``OSError``（权限/IO 异常，必须失败关闭）。
    """
    return os.stat(path).st_dev


def _fsync_dir(path: Path) -> None:
    """目录 fsync（``O_RDONLY|O_DIRECTORY`` 是目录 fsync 的合法用法）。"""
    fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_nonce() -> str:
    return secrets.token_hex(8)


def _rec_phase_name(rec: Any) -> str | None:
    phase = getattr(rec, "phase", None)
    name = getattr(phase, "name", None)
    if name is None and isinstance(phase, str):
        name = phase
    return name if isinstance(name, str) else None


def _blob_matches(blob: dict | None, expected: dict) -> bool:
    """幂等判定：只看「这份是不是我写的」，不比 ``since``/``pid`` 等易变字段。"""
    if type(blob) is not dict:
        return False
    return (blob.get("maintenance") is True
            and blob.get("migration_id") == expected["migration_id"]
            and blob.get("phase") == expected["phase"]
            and blob.get("nonce") == expected["nonce"])


def set_maintenance(rec: Any, reason: str | None = None) -> dict:
    """原子写维护文件：``tmp`` → flush → 文件 fsync → ``os.replace`` → 目录 fsync → 回读确认。

    幂等：已存在且内容匹配（maintenance/migration_id/phase/nonce）则跳过写。
    任何一步失败抛 ``MaintWriteError``（调用方按 design 记录 ``maintenance_ok=False``，
    继续执行写入封锁，最终 ``fatal_exit("maintenance_unconfirmed")``）。
    """
    migration_id = getattr(rec, "migration_id", None)
    phase_name = _rec_phase_name(rec)
    if not isinstance(migration_id, str) or migration_id.strip() == "":
        raise MaintWriteError("maint_write_invalid_rec", "migration_id 缺失或非字符串")
    if phase_name is None or phase_name not in PHASE_NAMES:
        raise MaintWriteError("maint_write_invalid_rec", f"phase 非法：{phase_name!r}")

    nonce = getattr(rec, "maint_nonce", None)
    if not isinstance(nonce, str) or nonce == "":
        nonce = _new_nonce()
        try:
            setattr(rec, "maint_nonce", nonce)      # 同一 nonce 贯穿本次 halt
        except Exception:
            pass

    if reason is None:
        for attr in ("halt_reason", "reason", "halt_code", "code"):
            value = getattr(rec, attr, None)
            if isinstance(value, str) and value:
                reason = value
                break
    expected = {
        "maintenance": True,
        "migration_id": migration_id,
        "phase": phase_name,
        "nonce": nonce,
        "reason": reason or "migration_halt",
        "pid": os.getpid(),
    }

    existing = read_maint_file()
    if existing.status is MaintRead.PRESENT and _blob_matches(existing.blob, expected):
        return existing.blob                         # 内容匹配：跳过写（幂等）

    blob = dict(expected, since=int(time.time() * 1000))   # UTC 毫秒
    target = Path(MAINT_FILE)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise MaintWriteError("maint_write_failed", f"STATE_DIR 不可用：{e!r}") from e

    tmp = target.with_name(f"{target.name}.tmp.{nonce}.{secrets.token_hex(4)}")
    payload = json.dumps(blob, ensure_ascii=False, sort_keys=True).encode("utf-8")
    try:
        with open(tmp, "wb") as f:                   # 文件 fd 必须是**可写** fd 才能 fsync
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)                      # 同目录 rename：POSIX 原子
    except OSError as e:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise MaintWriteError("maint_write_failed", repr(e)) from e

    try:
        _fsync_dir(target.parent)                    # 目录项持久化
    except OSError as e:
        raise MaintWriteError("maint_write_dir_fsync_failed", repr(e)) from e

    back = read_maint_file()                         # 回读确认：不认「调用过就算成功」
    if back.status is not MaintRead.PRESENT or not _blob_matches(back.blob, expected):
        raise MaintWriteError(
            "maint_write_unconfirmed",
            f"回读不一致：status={back.status.name}",
        )
    return back.blob


def confirm_maint_write(rec: Any) -> bool:
    """写入确认（仅 halt 使用）：只回答「我刚写的这一份是否真的落盘」。

    文件存在 ∧ 可解析 ∧ ``maintenance is True`` ∧ ``migration_id == rec.migration_id``
    ∧ ``phase == rec.phase.name`` ∧ ``since`` 非空 ∧ ``nonce == rec.maint_nonce``。
    任何不一致都返回 False；**不**推导维护态。
    """
    try:
        rd = read_maint_file()
        if rd.status is not MaintRead.PRESENT or type(rd.blob) is not dict:
            return False
        blob = rd.blob
        return (blob.get("maintenance") is True
                and blob.get("migration_id") == getattr(rec, "migration_id", None)
                and blob.get("phase") == _rec_phase_name(rec)
                and bool(blob.get("since"))
                and blob.get("nonce") == getattr(rec, "maint_nonce", None))
    except Exception:
        return False


# --- STATE_DIR 启动自检 ------------------------------------------------------


@dataclass(frozen=True)
class StateDirCheck:
    """自检结果。``ok=False`` 只禁用迁移入口，业务照常启动。"""

    ok: bool
    code: str                                    # ok / state_dir_* / maint_fs_unsupported
    state_dir: str
    errno: int | None = None
    detail: str = ""
    db_anchor: str = ""
    db_dev: int | None = None
    state_dev: int | None = None
    steps: dict[str, bool] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "state_dir": self.state_dir,
            "errno": self.errno,
            "detail": self.detail,
            "db_anchor": self.db_anchor,
            "db_dev": self.db_dev,
            "state_dev": self.state_dev,
            "steps": dict(self.steps),
        }


_SELFCHECK_CACHE: StateDirCheck | None = None


def reset_state_dir_selfcheck() -> None:
    """清缓存（仅供测试在切换 STATE_DIR 后重新自检）。"""
    global _SELFCHECK_CACHE
    _SELFCHECK_CACHE = None


def _state_dir_illegal() -> bool:
    try:
        return Path(STATE_DIR).resolve() == Path(config.DATA_DIR).resolve()
    except OSError:
        return str(STATE_DIR) == str(config.DATA_DIR)


def _probe_state_dir(state_dir: Path, steps: dict[str, bool]) -> tuple[int, str]:
    """完整探针序列。返回 ``(errno, 失败步骤)``；``errno=0`` 表示全部通过。

    只写 ``STATE_DIR`` 内的随机探针文件，绝不触碰 ``nodepool.db*``。
    """
    token = f"{os.getpid()}-{secrets.token_hex(6)}"
    src = state_dir / f".maint-selfcheck-{token}.tmp"
    dst = state_dir / f".maint-selfcheck-{token}.renamed"
    payload = f"nodepool-maint-probe {token}".encode("utf-8")

    steps["mkdir"] = True
    try:
        with open(src, "wb") as f:
            f.write(payload)
            f.flush()
            steps["write"] = True
            os.fsync(f.fileno())                     # 可写 fd 上 fsync
            steps["file_fsync"] = True
        os.replace(src, dst)                         # 同目录原子 rename
        steps["rename"] = True
        _fsync_dir(state_dir)
        steps["dir_fsync"] = True
        back = dst.read_bytes()
        steps["readback"] = back == payload
        if not steps["readback"]:
            return errno.EIO, "readback_mismatch"   # 回读内容不一致：I/O 完整性失败
        dst.unlink()
        steps["unlink"] = True
        _fsync_dir(state_dir)
        steps["dir_fsync_after_unlink"] = True
        return 0, ""
    except OSError as e:
        return int(getattr(e, "errno", 0) or 0), _failed_step(steps)
    finally:
        for candidate in (src, dst):
            try:
                if candidate.exists():
                    candidate.unlink()
            except OSError:
                pass


def _failed_step(steps: dict[str, bool]) -> str:
    for name in _PROBE_STEPS:
        if not steps.get(name):
            return name
    return "cleanup"


def selfcheck_state_dir() -> StateDirCheck:
    """启动自检（每次启动执行一次、结果缓存；失败只禁用迁移）。

    序列：目录合法性 → 建目录（若缺）→ 写探针 → 文件 fsync → 原子 rename →
    目录 fsync → 回读比对 → 删除 → 再目录 fsync。任一步失败记
    ``maint_fs_unsupported`` 并附 ``errno``；跨设备记 ``state_dir_cross_device``；
    ``STATE_DIR == DATA_DIR`` 记 ``state_dir_equals_data_dir``。
    """
    global _SELFCHECK_CACHE
    if _SELFCHECK_CACHE is not None:
        return _SELFCHECK_CACHE

    state_dir = Path(STATE_DIR)
    steps: dict[str, bool] = {name: False for name in _PROBE_STEPS}

    def finish(ok: bool, code: str, errno: int | None = None, detail: str = "",
               db_anchor: str = "", db_dev: int | None = None,
               state_dev: int | None = None) -> StateDirCheck:
        global _SELFCHECK_CACHE
        result = StateDirCheck(ok=ok, code=code, state_dir=str(state_dir),
                               errno=errno, detail=detail, db_anchor=db_anchor,
                               db_dev=db_dev, state_dev=state_dev, steps=dict(steps))
        _SELFCHECK_CACHE = result
        if not ok:
            log.warning("%s state_dir=%s errno=%s detail=%s", code, state_dir, errno, detail)
        return result

    if _state_dir_illegal():
        return finish(False, ERR_STATE_DIR_EQUALS_DATA_DIR,
                      detail="STATE_DIR 与 DATA_DIR 相同：维护文件必须与业务数据分离")

    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        steps["mkdir"] = True
    except OSError as e:
        return finish(False, ERR_MAINT_FS_UNSUPPORTED, getattr(e, "errno", None),
                      f"建目录失败：{e!r}")

    # 「同一文件系统」判定（计划 §2.6 原文）：
    #     os.stat(STATE_DIR).st_dev == os.stat(DB_PATH).st_dev
    #
    # 规则（严格，不放宽）：
    # * DB_PATH 已存在 ⇒ **必须**用库文件自身的 st_dev 判定；即使它与父目录
    #   的 st_dev 不一致，也不得退回父目录设备号。
    # * DB_PATH 不存在 ⇒ **仅当**明确捕获 FileNotFoundError 时，才允许用库所在
    #   目录的 st_dev 作为同一挂载的锚点（首次部署，文件尚未创建）。
    # * 其它 stat 异常（PermissionError / EIO / 其它 OSError）⇒ 失败关闭：
    #   返回 maint_fs_unsupported 并附 errno，**不得**伪装成文件不存在。
    db_path = Path(config.DB_PATH)
    db_dir = db_path.parent
    if not db_dir.exists():
        try:
            db_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            return finish(False, ERR_MAINT_FS_UNSUPPORTED, getattr(e, "errno", None),
                          f"数据库目录不可用：{e!r}", str(db_dir))
    anchor_str = str(db_dir)

    try:
        state_dev = _st_dev(state_dir)
    except OSError as e:
        return finish(False, ERR_MAINT_FS_UNSUPPORTED, getattr(e, "errno", None),
                      f"取 STATE_DIR st_dev 失败（失败关闭）：{e!r}", anchor_str)

    db_dev: int | None = None
    db_dev_source = ""
    try:
        db_dev = _st_dev(db_path)                  # 库文件存在：以文件本身为准
        db_dev_source = "db_file"
    except FileNotFoundError:                      # 唯一允许退回目录锚点的情形
        try:
            db_dev = _st_dev(db_dir)
            db_dev_source = "db_dir(anchor:db_file_absent)"
        except OSError as e:
            return finish(False, ERR_MAINT_FS_UNSUPPORTED, getattr(e, "errno", None),
                          f"取数据库目录 st_dev 失败（失败关闭）：{e!r}", anchor_str)
    except OSError as e:                           # PermissionError / EIO / 其它
        return finish(False, ERR_MAINT_FS_UNSUPPORTED, getattr(e, "errno", None),
                      f"取 DB_PATH st_dev 失败（失败关闭，不退回目录锚点）：{e!r}",
                      anchor_str)

    if db_dev != state_dev:
        return finish(False, ERR_STATE_DIR_CROSS_DEVICE, None,
                      "STATE_DIR 与数据库不在同一文件系统：拒绝迁移（业务照常运行）"
                      f"（db st_dev={db_dev}[{db_dev_source}],"
                      f" state_dir st_dev={state_dev})",
                      anchor_str, db_dev, state_dev)

    if not state_dir.is_dir():
        return finish(False, ERR_MAINT_FS_UNSUPPORTED, errno.ENOTDIR,
                      "STATE_DIR 存在但不是目录", anchor_str, db_dev, state_dev)

    err, failed = _probe_state_dir(state_dir, steps)
    if err or failed:
        return finish(False, ERR_MAINT_FS_UNSUPPORTED, err or None,
                      f"原子写链路不通过：{failed}",
                      anchor_str, db_dev, state_dev)

    return finish(True, "ok", None, "", anchor_str, db_dev, state_dev)


__all__ = [
    "MAINT_FILE",
    "MAINT_FILENAME",
    "STATE_DIR",
    "PHASE_NAMES",
    "MaintState",
    "MaintRead",
    "MaintReadResult",
    "MaintUnverifiable",
    "MaintWriteError",
    "StateDirCheck",
    "read_maint_file",
    "maintenance_state",
    "migration_incomplete",
    "judge_by_migration_rows",
    "set_maintenance",
    "confirm_maint_write",
    "since_sane",
    "maint_fields_sane",
    "selfcheck_state_dir",
    "reset_state_dir_selfcheck",
]
