"""Schema migration baseline (P-1 计划 §2.2).

本模块是 `schema_migrations` 表的**唯一**创建者。`backend.database.db.SCHEMA`
永远不包含它，`init_db()` 永不创建它：建表、基线登记、行数校验三件事只在
同一个 `BEGIN IMMEDIATE` 事务内完成，避免"启动即建空表、下次迁移被 preflight
拒死"的两段式死锁。

主要接口：
    ensure_schema_table_transactional()  原子建表 + 基线登记 + 行数校验
    record_baseline()                    校验生产结构后登记基线（幂等）
    current_version()                    按 seq 排序取最新成功版本
    preflight()                          只读判定矩阵（不改库）
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from backend import config

# --- 版本注册表（seq 唯一分配，禁止运行时自增猜测） ---------------------------

BASELINE_VERSION = f"{config.VERSION}-baseline"
BASELINE_APP_VERSION = config.VERSION
VERSION_SEQ: dict[str, int] = {
    BASELINE_VERSION: 1,
    "1.10": 2,
}

# --- 基准结构 ----------------------------------------------------------------

BASE_TABLES: tuple[str, ...] = (
    "settings",
    "airports",
    "fetch_logs",
    "nodes",
    "airport_filtered_nodes",
    "test_groups",
    "results",
    "latency_results",
    "share_history",
    "schedule_history",
    "batches",
    "run_events",
    "logs",
    "speed_sources",
    "speed_source_checks",
    "speed_runs",
    "speed_samples",
)

# `tasks` 是历史遗留表：可能不存在，也可能存在且为空。永远保留、不改、不迁移。
LEGACY_OPTIONAL_TABLES: tuple[str, ...] = ("tasks",)

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  migration_id TEXT    NOT NULL,
  seq          INTEGER NOT NULL UNIQUE,
  version      TEXT    NOT NULL,
  status       TEXT    NOT NULL CHECK (status IN ('running','failed','success')),
  started_at   TEXT    NOT NULL,
  finished_at  TEXT,
  detail_json  TEXT,
  UNIQUE(migration_id)
)
"""

# 受支持的结构基准：**逐项登记**，不做"任意列顺序都合法"的宽松比较。
#
# 两组基准的差异仅来自历史 `ALTER TABLE` 追加列导致的列声明顺序不同（语义相同、
# 物理列序不同）。列序会影响 `SELECT *` 与省略列名的 `INSERT`，因此不视为等价，
# 必须区分登记；未登记的任何第三种结构（含其它列序）一律拒绝。
#
# 两值均由 structure_hash() 计算（含列 ordinal），见 tests/test_migration_baseline.py:
#   alter_history = 生产同源的历史 ALTER 库形态（当前开发实例）
#   fresh_init    = 全新 init_db() 建出的库形态
SUPPORTED_STRUCTURE_PROFILES: dict[str, str] = {
    "alter_history": "798c5d4be3db0885cf75421669d2872d9ec67e426f946ec5cc919f84acc3ca15",
    "fresh_init": "e9feb0bb5028a38a082f1e2a75105cd15aa963c5562e15217ea80ff70c4eeb71",
}

# 兼容别名：默认取历史 ALTER 形态（生产端最可能的形态，但**仍待 NAS 只读实测确认**，
# 不得据此宣称生产结构已验证）。
BASELINE_STRUCTURE_HASH = SUPPORTED_STRUCTURE_PROFILES["alter_history"]


class MigrationError(RuntimeError):
    """迁移基线的通用错误基类。"""


class BaselineRefused(MigrationError):
    """基线登记被拒绝；`reason` 是稳定的机器可读码。"""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


class MigrationStateError(MigrationError):
    """`schema_migrations` 已存在但状态不可用于登记。"""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


# --- 连接 --------------------------------------------------------------------


def open_connection(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开一个**自动提交**连接（显式事务由本模块自己发 BEGIN IMMEDIATE）。"""
    path = Path(db_path or config.DB_PATH)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _require_autocommit(conn: sqlite3.Connection) -> None:
    if conn.isolation_level is not None:
        raise ValueError(
            "migrate.py 需要 isolation_level=None 的连接；"
            "请用 open_connection() 或显式传入 autocommit 连接"
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


# --- 结构摘要 ----------------------------------------------------------------

_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_CHECK_START = re.compile(r"\bCHECK\s*\(", re.I)


def _normalize_sql(sql: str) -> str:
    """去掉注释并折叠空白（注释/换行不参与结构语义）。"""
    sql = _COMMENT_BLOCK.sub(" ", sql or "")
    sql = _COMMENT_LINE.sub(" ", sql)
    return " ".join(sql.split())


def _check_expressions(ddl: str) -> list[str]:
    """抽取 DDL 中的 CHECK(...) 表达式（括号配对，天然忽略注释外的空白差异）。"""
    out: list[str] = []
    for match in _CHECK_START.finditer(ddl or ""):
        start = match.end() - 1
        depth = 0
        for idx in range(start, len(ddl)):
            ch = ddl[idx]
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    out.append(" ".join(ddl[start + 1:idx].split()))
                    break
    return sorted(out)


def _is_excluded(name: str, tbl_name: str) -> bool:
    if name == "schema_migrations" or tbl_name == "schema_migrations":
        return True  # 迁移自身的表不进业务结构摘要
    if name in LEGACY_OPTIONAL_TABLES or tbl_name in LEGACY_OPTIONAL_TABLES:
        return True
    return name.startswith("sqlite_") or tbl_name.startswith("sqlite_")


def _table_signature(conn: sqlite3.Connection, name: str, ddl: str) -> dict[str, Any]:
    # 用位置索引读取 PRAGMA 行，兼容 sqlite3.Row 与原生 tuple。
    # 列按 **ordinal（cid）原序**保留：`SELECT *` 与省略列名的 `INSERT` 依赖列序，
    # 列序变化必须被摘要捕获，不能用排序抹平。
    cols = [
        [int(row[0]), row[1], row[2], int(row[3]), row[4], int(row[5])]
        for row in conn.execute(f'PRAGMA table_info("{name}")')
    ]
    indexes = []
    for idx in conn.execute(f'PRAGMA index_list("{name}")'):
        cols_idx = [
            [row[0], row[2]]
            for row in conn.execute(f'PRAGMA index_info("{idx[1]}")')
        ]
        indexes.append([idx[1], int(idx[2]), idx[3], int(idx[4]), cols_idx])
    indexes.sort(key=lambda item: item[0])
    fks = sorted(
        ([row[2], row[3], row[4], row[5], row[6], row[7]]
         for row in conn.execute(f'PRAGMA foreign_key_list("{name}")')),
        key=lambda item: [str(part) for part in item],
    )
    return {
        "columns": cols,
        "indexes": indexes,
        "foreign_keys": fks,
        "checks": _check_expressions(ddl),
    }


def structure_signature(conn: sqlite3.Connection) -> dict[str, Any]:
    """结构签名：语义等价于 DDL、但对注释/空白不敏感，**保留列 ordinal**。

    只收录业务对象；`tasks` 与 `sqlite_%` 内部对象整体排除（前者单独记账，
    后者的存在只取决于 AUTOINCREMENT，不属业务结构）。
    """
    rows = conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master"
    ).fetchall()
    tables: dict[str, Any] = {}
    objects: dict[str, Any] = {}
    for row in rows:
        kind, name, tbl_name, ddl = row[0], row[1], row[2], row[3]
        if not ddl or _is_excluded(name, tbl_name or ""):
            continue
        if kind == "table":
            tables[name] = _table_signature(conn, name, ddl)
        else:
            objects[name] = _normalize_sql(ddl)
    missing = [name for name in BASE_TABLES if name not in tables]
    return {"tables": tables, "objects": objects, "missing_base_tables": missing}


def _hash_signature(signature: dict[str, Any]) -> str:
    payload = {
        "tables": signature["tables"],
        "objects": signature["objects"],
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def structure_hash(conn: sqlite3.Connection) -> str:
    return _hash_signature(structure_signature(conn))


def supported_structure_hashes() -> dict[str, str]:
    """受支持的结构基准（名称 → 摘要），用于报错与审计展示。"""
    return dict(SUPPORTED_STRUCTURE_PROFILES)


def structure_profile_for(actual_hash: str) -> str | None:
    """把实际摘要映射到已登记的基准名；未登记的一律返回 None（拒绝）。"""
    for name, expected in SUPPORTED_STRUCTURE_PROFILES.items():
        if actual_hash == expected:
            return name
    return None


def tasks_state(conn: sqlite3.Connection) -> dict[str, Any]:
    """`tasks` 的独立记账：存在性、行数、定义摘要（不影响主摘要）。"""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='tasks'"
    ).fetchone()
    if row is None:
        return {"tasks_present": False, "tasks_rows": 0, "tasks_hash": None}
    ddl = _normalize_sql(row[0] or "")
    task_hash = hashlib.sha256(ddl.encode("utf-8")).hexdigest()
    count = int(conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0])
    return {"tasks_present": True, "tasks_rows": count, "tasks_hash": task_hash}


# --- 读取 --------------------------------------------------------------------


def schema_table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    ).fetchone()
    return row is not None


def migration_rows(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    if not schema_table_exists(conn):
        return []
    cols = ("id", "migration_id", "seq", "version", "status", "started_at",
            "finished_at", "detail_json")
    return [dict(zip(cols, row)) for row in conn.execute(
        "SELECT id, migration_id, seq, version, status, started_at, finished_at, detail_json"
        " FROM schema_migrations ORDER BY seq"
    )]


def current_version(conn: sqlite3.Connection) -> str | None:
    """按 `seq` 取最新成功版本。禁止 MAX(version)/ORDER BY version（字符串会错序）。"""
    if not schema_table_exists(conn):
        return None
    row = conn.execute(
        "SELECT version FROM schema_migrations WHERE status='success'"
        " ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def preflight(conn: sqlite3.Connection, runtime_version: str | None = None) -> dict[str, Any]:
    """只读判定矩阵（§2.2.5）。不创建表、不写任何行。"""
    runtime_version = runtime_version or config.VERSION
    expected = f"{runtime_version}-baseline"
    if not schema_table_exists(conn):
        return {
            "state": "absent",
            "allowed": True,
            "reason": "migration_absent",
            "current_version": None,
            "expected": expected,
            "action": "create_table_and_record_baseline",
        }
    rows = migration_rows(conn)
    if not rows:
        return {
            "state": "empty",
            "allowed": False,
            "reason": "migration_table_empty",
            "current_version": None,
            "expected": expected,
            "action": "refuse",
        }
    unfinished = [row for row in rows if row["status"] != "success"]
    if unfinished:
        return {
            "state": "incomplete",
            "allowed": False,
            "reason": "migration_incomplete",
            "current_version": current_version(conn),
            "expected": expected,
            "pending": [row["migration_id"] for row in unfinished],
            "action": "recovery_required",
        }
    current = current_version(conn)
    if current not in VERSION_SEQ:
        return {
            "state": "unknown_version",
            "allowed": False,
            "reason": "version_unknown",
            "current_version": current,
            "expected": expected,
            "action": "refuse",
        }
    if current != expected:
        return {
            "state": "version_mismatch",
            "allowed": False,
            "reason": "version_mismatch",
            "current_version": current,
            "expected": expected,
            "action": "refuse",
        }
    return {
        "state": "ready",
        "allowed": True,
        "reason": "ok",
        "current_version": current,
        "expected": expected,
        "action": "proceed",
    }


# --- 写入（只在迁移器入口事务内） ---------------------------------------------


def _count_rows(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0])


def _insert_row(conn: sqlite3.Connection, *, migration_id: str, seq: int, version: str,
                status: str, started_at: str, finished_at: str | None,
                detail_json: str | None) -> None:
    conn.execute(
        "INSERT INTO schema_migrations"
        " (migration_id, seq, version, status, started_at, finished_at, detail_json)"
        " VALUES (?,?,?,?,?,?,?)",
        (migration_id, seq, version, status, started_at, finished_at, detail_json),
    )


def ensure_schema_table_transactional(
    conn: sqlite3.Connection,
    *,
    detail: dict[str, Any],
    migration_id: str = BASELINE_VERSION,
    version: str = BASELINE_VERSION,
    seq: int | None = None,
) -> dict[str, Any]:
    """建表 + 基线登记 + 行数校验，同一 `BEGIN IMMEDIATE` 事务内完成。

    - 表**原本不存在**：建表、插基线行、校验 `count == 1`、提交。
    - 表原本存在且恰有本迁移成功行：幂等返回，不重复插入。
    - 表原本存在但 0 行：`MigrationStateError("migration_table_empty")`，拒绝补写。
    - 表原本存在且内容不符：`baseline_conflict` / `migration_table_not_pristine`。
    任一步异常 → `ROLLBACK`，因此不存在"表在、基线缺"的中间态。
    """
    _require_autocommit(conn)
    seq = VERSION_SEQ[version] if seq is None else seq
    created_at = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 先在同一事务内判断"表原本是否存在"，绝不先建表再判断，避免把
        # "已有空表"顺手补成基线（那会把迁移入口焊死）。
        existed = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone() is not None
        if not existed:
            conn.execute(SCHEMA_MIGRATIONS_DDL)
            count = 0
        else:
            count = _count_rows(conn)
        if not existed and count == 0:
            _insert_row(
                conn,
                migration_id=migration_id,
                seq=seq,
                version=version,
                status="success",
                started_at=created_at,
                finished_at=created_at,
                detail_json=json.dumps(detail, sort_keys=True, separators=(",", ":"), ensure_ascii=False),
            )
            if _count_rows(conn) != 1:
                raise MigrationStateError("baseline_count_mismatch", "基线行数校验失败")
            conn.execute("COMMIT")
            return {"status": "created", "migration_id": migration_id, "seq": seq, "version": version}
        if existed and count == 0:
            raise MigrationStateError(
                "migration_table_empty",
                "schema_migrations 已存在但 0 行：拒绝补写基线（不自动修复、不删除）",
            )
        if count == 1:
            row = conn.execute(
                "SELECT migration_id, seq, version, status FROM schema_migrations"
            ).fetchone()
            if row[0] == migration_id and row[2] == version \
                    and row[3] == "success" and int(row[1]) == int(seq):
                conn.execute("COMMIT")
                return {"status": "already_present", "migration_id": migration_id,
                        "seq": int(row[1]), "version": version}
            raise MigrationStateError(
                "baseline_conflict",
                f"已存在不同迁移行：migration_id={row[0]} version={row[2]}",
            )
        raise MigrationStateError("migration_table_not_pristine", f"表内已有 {count} 行")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def record_baseline(
    conn: sqlite3.Connection | None = None,
    *,
    db_path: str | Path | None = None,
    app_version: str | None = None,
    image_digest: str | None = None,
    healthz_version: str | None = None,
    runtime_version: str | None = None,
    detail_extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """校验实际结构后登记基线；幂等，可重复调用。

    校验发生在**任何写入之前**：结构摘要不匹配、`tasks` 非空、基准表缺失
    都会立刻拒绝，且不会留下空表。
    """
    own_conn = conn is None
    if own_conn:
        conn = open_connection(db_path)
    try:
        _require_autocommit(conn)
        runtime_version = runtime_version or config.VERSION
        app_version = app_version or config.VERSION
        expected_version = f"{runtime_version}-baseline"
        if expected_version not in VERSION_SEQ:
            raise BaselineRefused(
                "unsupported_runtime_version",
                f"运行版本 {runtime_version} 未在 VERSION_SEQ 注册",
            )
        if app_version != runtime_version:
            raise BaselineRefused(
                "version_mismatch",
                f"app_version={app_version} ≠ runtime_version={runtime_version}",
            )
        if healthz_version is not None and healthz_version != runtime_version:
            raise BaselineRefused(
                "version_mismatch",
                f"healthz_version={healthz_version} ≠ runtime_version={runtime_version}",
            )

        signature = structure_signature(conn)
        if signature["missing_base_tables"]:
            raise BaselineRefused(
                "missing_base_tables",
                ",".join(signature["missing_base_tables"]),
            )
        actual_hash = _hash_signature(signature)
        profile = structure_profile_for(actual_hash)
        if profile is None:
            raise BaselineRefused(
                "structure_mismatch",
                f"actual={actual_hash} supported={supported_structure_hashes()}",
            )
        tasks = tasks_state(conn)
        if tasks["tasks_present"] and tasks["tasks_rows"] != 0:
            raise BaselineRefused(
                "tasks_not_empty",
                f"tasks 行数 {tasks['tasks_rows']}（红线：不删除、不修改该表，登记必须停止）",
            )

        detail: dict[str, Any] = {
            "baseline": True,
            "app_version": app_version,
            "runtime_version": runtime_version,
            "image_digest": image_digest,
            "healthz_version": healthz_version,
            "structure_hash": actual_hash,
            "structure_profile": profile,
            "tables": list(BASE_TABLES),
            "tables_count": len(signature["tables"]),
            "tasks_present": tasks["tasks_present"],
            "tasks_rows": tasks["tasks_rows"],
            "tasks_hash": tasks["tasks_hash"],
        }
        if detail_extra:
            detail.update(detail_extra)

        result = ensure_schema_table_transactional(conn, detail=detail)
        result["version"] = BASELINE_VERSION
        result["structure_hash"] = actual_hash
        result["structure_profile"] = profile
        result["tasks_present"] = tasks["tasks_present"]
        result["tasks_rows"] = tasks["tasks_rows"]
        return result
    finally:
        if own_conn:
            conn.close()

# =============================================================================
# P-1.6 迁移状态机（计划 §8：`migrate.py` 最小状态机 + 空迁移 dry-run）
#
# 范围声明（与已批准文档的对应关系，务必与交付材料一起阅读）：
#
# * 设计 v1.10 §5 的 phase 集合与转换表（T1–T8）**在本文件完整实现**，包括
#   `IllegalTransition`、旧值 `PhaseMismatch`、`COMMITTED` 不可回退。
# * 计划 v1.10 §3.2 与 PT-09b 明确规定 P-1 **不建** `system_events` /
#   `migration_tx_notes`、不实现持久化事件链（`seq/prev_sha/token_fp/
#   s5_failed_ever`），`schema_migrations.status` 只有 `running/failed/success`，
#   且 `write_event()` 必须抛 `NotImplementedError`。
#   因此本阶段实现的是"无事件链"的最小状态机：
#       - phase 是**进程内**状态，唯一 durable 痕迹是 `schema_migrations.status`；
#       - `set_phase()`（durable 事件后端）与 `write_event()`/`emit_event()`
#         一律 `NotImplementedError`，绝不用内存或普通日志冒充持久事件；
#       - 非 DB 诊断通道固定为 `<STATE_DIR>/migration-fault.log`（K1 未成立时
#         唯一允许的记录方式）。
# * 设计 v1.10 §1/§2 的 durable 事件链（A/B/C 提交结果确认、哈希链、E1–E3、
#   β/β′ 恢复入口、`s5_failed_ever` 单调）**不在 P-1 实现**：PT-09b 未解除前
#   实现它等于改已批准架构，需负责人裁决（见交付材料的冲突报告）。
# * P-1.5 已验收的维护封锁、写入闸、排空判据与**发射器对象身份绑定**是本阶段
#   唯一的可信凭据来源：`halt_migration` 必须走 `register_confirmation_issuer`
#   → `mint_confirmation` → `halt_tail`，不得自造凭据、不得绕过身份校验。
# * P-1.5 阶段不注册发射器 ⇒ `halt_tail()` 失败关闭；P-1.6 只有在**真实进入
#   `halt_migration`** 时才惰性注册（模块 import 本身不注册，避免改变 P-1.5 语义）。
# =============================================================================

TARGET_VERSION = "1.10"


class Phase(str, Enum):
    """设计 v1.10 §5 的六个 phase。禁止在此之外新增临时状态。"""

    PREPARED = "PREPARED"
    TX_OPEN = "TX_OPEN"
    COMMIT_ATTEMPTED = "COMMIT_ATTEMPTED"
    COMMITTED = "COMMITTED"
    TX_ROLLED_BACK = "TX_ROLLED_BACK"
    UNKNOWN = "UNKNOWN"


# 设计 v1.10 §1.2/§5 的转换表：**唯一**合法来源，任何其他"能走通"的组合都是 bug。
LEGAL_TRANSITIONS: tuple[tuple[Phase, Phase], ...] = (
    (Phase.PREPARED, Phase.TX_OPEN),            # T1 BEGIN 成功
    (Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED),    # T2 COMMIT 前
    (Phase.TX_OPEN, Phase.TX_ROLLED_BACK),      # T3 ROLLBACK 成功
    (Phase.COMMIT_ATTEMPTED, Phase.COMMITTED),  # T4 β 门禁全通过
    (Phase.COMMIT_ATTEMPTED, Phase.TX_ROLLED_BACK),  # T5 β 确认未提交
    (Phase.COMMIT_ATTEMPTED, Phase.UNKNOWN),    # T6 α COMMIT 抛异常且 K1 成立
    (Phase.UNKNOWN, Phase.COMMITTED),           # T7 重查确认已提交
    (Phase.UNKNOWN, Phase.TX_ROLLED_BACK),      # T8 重查确认未提交
)

TRANSITION_NAMES: dict[tuple[Phase, Phase], str] = {
    (Phase.PREPARED, Phase.TX_OPEN): "T1",
    (Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED): "T2",
    (Phase.TX_OPEN, Phase.TX_ROLLED_BACK): "T3",
    (Phase.COMMIT_ATTEMPTED, Phase.COMMITTED): "T4",
    (Phase.COMMIT_ATTEMPTED, Phase.TX_ROLLED_BACK): "T5",
    (Phase.COMMIT_ATTEMPTED, Phase.UNKNOWN): "T6",
    (Phase.UNKNOWN, Phase.COMMITTED): "T7",
    (Phase.UNKNOWN, Phase.TX_ROLLED_BACK): "T8",
}

# 设计 v1.10 §2.1 的分派表：T1/T2 属迁移事务内（tx_local），T3–T8 属 durable。
# P-1 不实现两个后端，但**表本身**必须存在且自检（PT-12）：它规定了"哪种转换
# 需要 durable 证据"，将来接入事件链时不得改口。
TRANSITION_DISPATCH: dict[tuple[Phase, Phase], str] = {
    (Phase.PREPARED, Phase.TX_OPEN): "tx_local",
    (Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED): "tx_local",
    (Phase.TX_OPEN, Phase.TX_ROLLED_BACK): "durable",
    (Phase.COMMIT_ATTEMPTED, Phase.COMMITTED): "durable",
    (Phase.COMMIT_ATTEMPTED, Phase.TX_ROLLED_BACK): "durable",
    (Phase.COMMIT_ATTEMPTED, Phase.UNKNOWN): "durable",
    (Phase.UNKNOWN, Phase.COMMITTED): "durable",
    (Phase.UNKNOWN, Phase.TX_ROLLED_BACK): "durable",
}


class WriteOutcome(Enum):
    """设计 v1.10 §2.1：事件写入结果唯一口径，禁止裸 bool（PT-13）。

    `LOCAL` 只用于 tx_local 与 P-1 的非 DB 诊断通道，**不参与**任何 durable 判断。
    """

    COMMITTED = "committed"     # A：确认已提交
    ABSENT = "absent"           # B：确认未提交
    UNRESOLVED = "unresolved"   # C：无法确认
    LOCAL = "local"             # 仅 tx_local / 非 DB 通道


class PhaseResult(Enum):
    """设计 v1.10 §2.2 的三态返回；任何上层不得用布尔替代。"""

    SUCCESS = "success"
    REJECTED = "rejected"
    HALTED = "halted"


class IllegalTransition(MigrationError):
    """转换不在 `LEGAL_TRANSITIONS` 内（含 `UNKNOWN → UNKNOWN`）。"""

    def __init__(self, old: Phase, new: Phase, message: str = "") -> None:
        self.old = old
        self.new = new
        detail = message or "不在 LEGAL_TRANSITIONS 内"
        super().__init__(f"illegal_transition: {old.value} -> {new.value}（{detail}）")


class PhaseMismatch(MigrationError):
    """`old` 与记录当前 phase 不一致（同一转换被写两次的典型症状）。"""

    def __init__(self, expected: Phase, actual: Phase) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"phase_mismatch: expected {expected.value}, actual {actual.value}")


class MigrationHalted(MigrationError):
    """已 `halt_tail`，此后禁止任何状态推进与事件写入（零数据库写入）。"""

    def __init__(self, message: str = "迁移已停机收尾，禁止继续推进") -> None:
        super().__init__(f"halted: {message}")


class MigrationDisabled(MigrationError):
    """`NODEPOOL_MIGRATION_ENABLED` 未开启：迁移入口整体不可达（PT-11）。"""

    def __init__(self, message: str = "NODEPOOL_MIGRATION_ENABLED=0，迁移入口关闭") -> None:
        super().__init__(f"migration_disabled: {message}")


class MigrationRefused(MigrationError):
    """前置条件不满足（维护态 / 目标库 / 备份集 / 版本状态）。"""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


class AlreadyRunning(MigrationError):
    """另一个迁移会话已持有单实例锁（设计 v1.10 §3.3 的 K1_β 单实例前提）。"""

    def __init__(self, message: str = "另一个迁移会话正在运行") -> None:
        super().__init__(f"already_running: {message}")


class DispatchTableBroken(MigrationError):
    """分派表自检失败：`TRANSITION_DISPATCH` 与 `LEGAL_TRANSITIONS` 不自洽。"""

    def __init__(self, message: str) -> None:
        super().__init__(f"dispatch_table_broken: {message}")


class FatalExit(MigrationError):
    """停机收尾无法确认（维护态或写入封锁未回读成立）：非零退出，零数据库写入。"""

    def __init__(self, reason: str, message: str = "", exit_code: int = 3) -> None:
        self.reason = reason
        self.exit_code = int(exit_code)
        super().__init__(f"{reason}: {message}" if message else reason)


@dataclass
class MigrationRecord:
    """单次迁移会话的进程内记录（P-1 阶段不落事件表，故只在内存与 status 行）。"""

    migration_id: str
    phase: Phase = Phase.PREPARED
    phase_epoch: int = 0                 # 计划 §2.5.1：每次成功转换 +1，拒绝路径不变
    halted: bool = False
    commit_raised: bool = False          # 非 phase 标记（设计 v1.10 §1.1 S1）
    pending_reason: str | None = None
    s5_failed_ever: bool = False
    started_at: str = field(default_factory=_utc_now)
    meta: dict[str, Any] = field(default_factory=dict)


def check_transition_table(legal: tuple[tuple[Phase, Phase], ...] | None = None,
                           dispatch: dict[tuple[Phase, Phase], str] | None = None,
                           names: dict[tuple[Phase, Phase], str] | None = None) -> bool:
    """分派表启动自检（PT-12 / 设计 v1.10 §0.10）：显式 `if` + 抛错，不用 `assert`。

    * 分派表键集合必须**恰好**等于合法转换集合（多一条、少一条都拒启动）；
    * 后端只能是 `tx_local` / `durable`；
    * T1/T2 必须 `tx_local`，T3–T8 必须 `durable`（防止事件层接入时改口）。
    """
    legal = LEGAL_TRANSITIONS if legal is None else legal
    dispatch = TRANSITION_DISPATCH if dispatch is None else dispatch
    names = TRANSITION_NAMES if names is None else names
    legal_set = set(legal)
    keys = set(dispatch)
    if keys != legal_set:
        raise DispatchTableBroken(
            "keys != LEGAL_TRANSITIONS: extra=%r missing=%r"
            % (sorted(map(lambda t: (t[0].value, t[1].value), keys - legal_set)),
               sorted(map(lambda t: (t[0].value, t[1].value), legal_set - keys))))
    for key, backend in dispatch.items():
        if backend not in ("tx_local", "durable"):
            raise DispatchTableBroken(f"unknown backend {backend!r} for {key}")
    for key in ((Phase.PREPARED, Phase.TX_OPEN), (Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED)):
        if dispatch.get(key) != "tx_local":
            raise DispatchTableBroken(f"T1/T2 must be tx_local: {key} -> {dispatch.get(key)}")
    for key in legal_set - {(Phase.PREPARED, Phase.TX_OPEN), (Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED)}:
        if dispatch.get(key) != "durable":
            raise DispatchTableBroken(f"T3-T8 must be durable: {key} -> {dispatch.get(key)}")
    if set(names) != legal_set:
        raise DispatchTableBroken("TRANSITION_NAMES 与 LEGAL_TRANSITIONS 不一致")
    return True


check_transition_table()   # 模块导入即自检：普通模式与 -O 一致拒启动


def set_phase_memory(rec: MigrationRecord, old: Phase, new: Phase) -> PhaseResult:
    """P-1 的 phase 推进：**只改进程内状态**，不写任何数据库。

    这是 `set_phase()` 在"无事件链"阶段的可施工内核：合法性与旧值一致性检查
    与将来接入 durable 后端时完全相同，差别只在于此处没有事件可写。
    """
    if rec.halted:
        raise MigrationHalted("halt_tail 之后不得推进 phase")
    if rec.phase is not old:
        raise PhaseMismatch(old, rec.phase)
    if old is Phase.UNKNOWN and new is Phase.UNKNOWN:
        raise IllegalTransition(old, new, "记录原因请用 append_reason，不得用状态变化表达")
    if (old, new) not in LEGAL_TRANSITIONS:
        raise IllegalTransition(old, new)
    rec.phase = new
    rec.phase_epoch += 1                 # 只在成功转换后递增，且每次只加一
    return PhaseResult.SUCCESS


def set_phase(rec: MigrationRecord, old: Phase, new: Phase) -> PhaseResult:
    """durable 事件后端的 `set_phase`（设计 v1.10 §2.1）：P-1 **未实现**。

    PT-09b（计划 §5）要求本阶段 `write_event()` 抛 `NotImplementedError`，
    `schema_migrations` 也只有 running/failed/success。没有 `system_events` 就
    无法产生"事件先成功、再改内存"的可信顺序，因此本函数不允许退化成
    "直接改内存后假装已持久化"，一律失败关闭。
    """
    raise NotImplementedError(
        "set_phase: P-1 不建 system_events/migration_tx_notes（计划 §3.2、PT-09b），"
        "durable 事件后端未实现；P-1 会话内请使用 set_phase_memory()"
    )


def write_event(rec: MigrationRecord, event: str, detail: dict[str, Any] | None = None) -> WriteOutcome:
    """durable 事件写入通道（设计 v1.10 §2.1）：P-1 **必须** NotImplementedError。"""
    raise NotImplementedError(
        "write_event: P-1 不实现持久化事件链（计划 §3.2 / PT-09b）；"
        "无 system_events 表、无 seq/prev_sha/token_fp/s5_failed_ever 列"
    )


def write_tx_event(rec: MigrationRecord, event: str,
                   detail: dict[str, Any] | None = None) -> WriteOutcome:
    """tx_local 事件通道（`migration_tx_notes`）：P-1 同样未实现。"""
    raise NotImplementedError(
        "write_tx_event: P-1 不建 migration_tx_notes（计划 §3.2 / PT-09b）"
    )


def emit_event(rec: MigrationRecord, event: str, detail: dict[str, Any] | None = None) -> WriteOutcome:
    """唯一事件入口（按 `TRANSITION_DISPATCH` 分派）：P-1 两个后端都不可用。

    保留函数与分派表，是为了让 P-1.6 之后接入事件链只需填后端、不改调用方。
    """
    raise NotImplementedError(
        "emit_event: P-1 无 tx_local/durable 后端（计划 §3.2 / PT-09b）；"
        "分派表见 TRANSITION_DISPATCH"
    )


def append_reason(rec: MigrationRecord, code: str,
                  detail: dict[str, Any] | None = None) -> WriteOutcome:
    """phase 不变地追加上下文；P-1 只走非 DB 通道（`migration-fault.log`）。

    设计 v1.10 §1.1/§5 禁止用 `set_phase(UNKNOWN, UNKNOWN)` 记录原因；P-1 无事件
    表，因此原因只能落到文件日志（`WriteOutcome.LOCAL`），绝不伪造 durable 证据。
    """
    if rec.halted:
        raise MigrationHalted("halt_tail 之后不得追加上下文")
    _write_fault_log({
        "event": "migration_reason",
        "migration_id": rec.migration_id,
        "phase": rec.phase.value,
        "code": str(code),
        "detail": detail or {},
        "ts": _utc_now(),
    })
    rec.meta.setdefault("reasons", []).append({"code": str(code), "ts": _utc_now()})
    return WriteOutcome.LOCAL


# --- 非 DB 诊断通道 -----------------------------------------------------------

def _write_fault_log(record: dict[str, Any], path: str | Path | None = None) -> str:
    """`migration-fault.log`：K1 未成立 / halt / 失败时唯一允许的记录方式。

    追加一行 JSON 并 fsync：不写任何数据库、不进事件链。
    """
    target = Path(path or config.MIGRATION_FAULT_LOG)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return str(target)


# --- 可信发射器（P-1.5 受控注册接口） -----------------------------------------

_ISSUER_TOKEN: object | None = None
_ISSUER_TOKEN_LOCK = threading.Lock()


def _ensure_issuer_token() -> object:
    """惰性注册 `halt_migration` 为可信发射器（同一函数对象 → 同一令牌）。

    惰性是刻意的：模块 import（含 CLI/测试导入）不改变 P-1.5 的"无发射器 ⇒
    `halt_tail()` 失败关闭"语义，只有真正进入停机收尾才注册。
    """
    global _ISSUER_TOKEN
    if _ISSUER_TOKEN is None:
        with _ISSUER_TOKEN_LOCK:
            if _ISSUER_TOKEN is None:
                from backend.database import pool
                _ISSUER_TOKEN = pool.register_confirmation_issuer(_ISSUER_FN)
    return _ISSUER_TOKEN


def reset_issuer_token_for_tests() -> None:
    """仅供测试：丢掉本模块持有的令牌（注册表仍由 `pool.reset_for_tests()` 清理）。"""
    global _ISSUER_TOKEN
    _ISSUER_TOKEN = None


def halt_migration(reason: str, *, rec: MigrationRecord | None = None,
                   migration_id: str | None = None,
                   detail: dict[str, Any] | None = None,
                   fault_log_path: str | Path | None = None) -> dict[str, Any]:
    """停机收尾（设计 v1.10 §0.10 十二次修订 + 计划 §2.5.2）。

    三个确认动作**各自独立异常边界**，任何一个抛错都不得跳过其余仍可执行的确认：

    1. `pool.block_writes()`（先封锁，保证此后无业务写可提交）；
    2. `maint.maintenance_state()`（A 类唯一判定入口）→ `maintenance_ok`；
    3. `pool.confirm_writes_blocked()`（闸置位 + 写连接全关 + 无在途可提交 + 只读 query_only）。

    只有三项都取得**明确肯定**结果，才允许注册可信发射器、签发凭据并 `halt_tail()`。
    任一失败/不可读 → `FatalExit`（非零退出），**不签发凭据、不置 `rec.halted`**。
    故障日志写入失败不中断封锁与确认动作，日志错误本身保留为诊断信息。
    """
    from backend.database import maint, pool

    mid = migration_id or (rec.migration_id if rec is not None else "unknown")
    diagnostics: dict[str, Any] = {}
    log_errors: list[str] = []

    def _log(record: dict[str, Any]) -> None:
        """非 DB 诊断通道：写失败只记录错误，绝不中断封锁与确认。"""
        try:
            _write_fault_log(record, fault_log_path)
        except Exception as exc:      # noqa: BLE001 - 日志不得影响故障安全
            log_errors.append(f"{type(exc).__name__}: {exc}")

    # --- 1) 封锁：独立边界 ---
    block_reason: str | None = None
    block_error: str | None = None
    try:
        block_reason = pool.block_writes(pool.REASON_MAINTENANCE)
    except Exception as exc:          # noqa: BLE001 - 必须继续尝试维护态/回读确认
        block_error = f"{type(exc).__name__}: {exc}"

    # --- 2) 维护态：独立边界 ---
    state: Any = None
    maint_error: str | None = None
    try:
        state = maint.maintenance_state()
    except Exception as exc:          # noqa: BLE001
        maint_error = f"{type(exc).__name__}: {exc}"

    # --- 3) 写入封锁回读：独立边界 ---
    writes_ok = False
    write_detail: dict[str, Any] = {}
    confirm_error: str | None = None
    try:
        writes_ok, write_detail = pool.confirm_writes_blocked()
    except Exception as exc:          # noqa: BLE001
        confirm_error = f"{type(exc).__name__}: {exc}"

    mstate = getattr(maint, "MaintState", None)
    maintenance_ok = bool(mstate is not None and state is mstate.ACTIVE)
    diagnostics = {
        "block_reason": block_reason,
        "block_error": block_error,
        "maintenance": getattr(state, "name", None),
        "maint_error": maint_error,
        "maintenance_ok": maintenance_ok,
        "writes_blocked": bool(writes_ok),
        "confirm_error": confirm_error,
        "block_detail": write_detail,
        "log_errors": log_errors,
    }

    _log({
        "event": "halt_requested",
        "migration_id": mid,
        "reason": str(reason),
        "detail": detail or {},
        "ts": _utc_now(),
        **diagnostics,
    })

    if block_error is not None or maint_error is not None or confirm_error is not None \
            or not maintenance_ok or not writes_ok:
        raise FatalExit(
            "halt_unconfirmed",
            f"维护态={diagnostics['maintenance']} maintenance_ok={maintenance_ok} "
            f"writes_blocked={writes_ok} block_error={block_error} "
            f"maint_error={maint_error} confirm_error={confirm_error}",
        )

    try:
        token = _ensure_issuer_token()
    except Exception as exc:          # noqa: BLE001 - 注册失败同样不得签发凭据
        raise FatalExit("halt_unconfirmed", f"register_confirmation_issuer 失败：{exc}") from exc
    try:
        confirmation = pool.mint_confirmation(token, mid, True, True)
    except pool.HaltPreconditionFailed as exc:
        raise FatalExit("halt_unconfirmed", f"mint_confirmation 拒绝：{exc.code}") from exc
    try:
        tail = pool.halt_tail(confirmation)
    except pool.HaltPreconditionFailed as exc:
        raise FatalExit("halt_unconfirmed", f"halt_tail 拒绝：{exc.code}") from exc

    # 只有 halt_tail 复核成功之后才允许标记已 halt
    if rec is not None:
        rec.halted = True
        rec.pending_reason = str(reason)
        rec.commit_raised = rec.commit_raised or reason == "commit_outcome_unknown"
    _log({
        "event": "halt_tail",
        "migration_id": mid,
        "reason": str(reason),
        "halt_tail": tail,
        "ts": _utc_now(),
    })
    return {"halt_tail": tail, "reason": str(reason), "migration_id": mid,
            "maintenance": diagnostics["maintenance"], "writes_blocked": True,
            "diagnostics": {**diagnostics, "log_errors": log_errors}}


def halt_tail(confirmation: Any) -> str:
    """`migrate.py` 侧的停机收尾复核入口（计划 §3.1 归属）。

    只做两件事：把凭据交给 `pool.halt_tail()` 复核（不可信/未确认即抛），
    复核通过后返回 `halt_tail` 常量。**不读维护文件、不查库、不推进 phase**；
    调用方（`halt_migration`）负责置 `rec.halted`，之后任何 `set_phase_memory`
    / `append_reason` / 事件写入都会被 `MigrationHalted` 拒绝。
    """
    from backend.database import pool
    code = pool.halt_tail(confirmation)
    return code


_ISSUER_FN = halt_migration     # 登记用的函数对象：模块级定义、__globals__ 即本模块


# --- 单实例锁 ----------------------------------------------------------------

class migration_run_lock:
    """单实例锁（`<STATE_DIR>/migration.lock`，`flock` 非阻塞）。

    设计 v1.10 §3.3 的 K1_β 前提是"只有一个迁移会话"：两个进程同时推进 phase
    必然产生互不喜欢的状态。锁文件不承载任何状态，只承载互斥。
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or (Path(config.STATE_DIR) / "migration.lock"))
        self._fd: int | None = None

    def __enter__(self) -> "migration_run_lock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise AlreadyRunning(f"{self.path} 已被持有：{exc}") from exc
        self._fd = fd
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


# --- 写后置：失败行 / 成功行 --------------------------------------------------

def _next_seq(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM schema_migrations").fetchone()
    return int(row[0]) + 1


def _mark_failed(db_path: str | Path, migration_id: str, seq: int, version: str,
                 started_at: str, failure: str, detail_extra: dict[str, Any] | None = None) -> bool:
    """事务回滚后写 `failed` 行（独立短事务）。写不进去就返回 False，由调用方重封锁。"""
    conn = open_connection(db_path)
    try:
        _require_autocommit(conn)
        conn.execute("BEGIN IMMEDIATE")
        rowcount = conn.execute(
            "UPDATE schema_migrations SET status='failed', finished_at=?, detail_json=?"
            " WHERE migration_id=? AND status='running'",
            (_utc_now(),
             json.dumps({"failure": failure, "dry_run": True, "statements": 0,
                         **(detail_extra or {})}, sort_keys=True, ensure_ascii=False),
             migration_id),
        ).rowcount
        if int(rowcount) == 0:
            _insert_row(conn, migration_id=migration_id, seq=seq, version=version,
                        status="failed", started_at=started_at, finished_at=_utc_now(),
                        detail_json=json.dumps({"failure": failure, "dry_run": True,
                                                "statements": 0, **(detail_extra or {})},
                                               sort_keys=True, ensure_ascii=False))
        conn.execute("COMMIT")
        return True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        return False
    finally:
        conn.close()


def migration_state_summary(db_path: str | Path | None = None) -> dict[str, Any]:
    """只读状态摘要（CLI `status` 用）：preflight 判定 + 当前版本 + 未完成迁移。"""
    conn = open_connection(db_path)
    try:
        pre = preflight(conn)
        return {
            "preflight": pre["state"],
            "allowed": bool(pre.get("allowed")),
            "reason": pre.get("reason"),
            "current_version": pre.get("current_version"),
            "expected": pre.get("expected"),
            "pending": pre.get("pending", []),
            "migration_incomplete": pre["state"] == "incomplete",
        }
    finally:
        conn.close()


# --- 空迁移 dry-run（计划 §8：P-1.6 = 最小状态机 + 空迁移 dry-run） -------------

def _safe_log(record: dict[str, Any], path: str | Path | None = None) -> str | None:
    """非 DB 诊断写入：失败只返回错误串，绝不影响封锁与确认动作。"""
    try:
        _write_fault_log(record, path)
        return None
    except Exception as exc:      # noqa: BLE001 - 日志不得影响故障安全
        return f"{type(exc).__name__}: {exc}"


def _recheck_structure_unchanged(db_path: str | Path,
                                 expected_hash: str | None) -> tuple[bool, str | None]:
    """只读复核：事务回滚后结构摘要是否仍与迁移前一致。

    用独立连接 + `PRAGMA query_only=1`：只读语义由 SQLite 强制，不产生任何写入。
    复核本身失败（打不开/查询错）一律视为"无法确认"，调用方必须走 halt，不得
    认定 `KNOWN_FAILED`。
    """
    try:
        conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA query_only=1")
            actual = structure_hash(conn)
        finally:
            conn.close()
    except Exception as exc:          # noqa: BLE001 - 复核不可用即无法确认
        return False, f"recheck_unavailable: {type(exc).__name__}: {exc}"
    if expected_hash is None:
        return False, "recheck_no_baseline_hash"
    if actual != expected_hash:
        return False, "recheck_structure_changed"
    return True, None


def _close_migration_connection(conn: sqlite3.Connection) -> tuple[bool, str | None]:
    """关闭本次迁移的独立连接，并**确认**它真的不可再执行 SQL。

    `migrate.open_connection()` 建的是直接 SQLite 连接，不在 P-1.5 业务连接池里：
    `pool.confirm_writes_blocked()` 无法证明它已关闭。因此在 halt 确认前必须显式
    关闭并复核（关闭后任何 `execute` 都必须抛 `ProgrammingError`）。
    """
    try:
        conn.close()
    except Exception as exc:          # noqa: BLE001
        return False, f"close_error: {type(exc).__name__}: {exc}"
    try:
        conn.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return True, None
    except Exception as exc:          # noqa: BLE001 - 非 ProgrammingError 属异常路径
        return False, f"close_unverified: {type(exc).__name__}: {exc}"
    return False, "close_unverified: connection still operable"


def run(*, db_path: str | Path | None = None, dry_run: bool = True,
        snapshot: bool = False, snapshot_stage: str = "pre-migrate",
        drain_rounds: int = 2, drain_min_interval: float = 10.0,
        clock: Any = None, sleeper: Any = None, checkpoint: Any = None,
        holders: Any = None, lock_path: str | Path | None = None,
        fault_log_path: str | Path | None = None) -> dict[str, Any]:
    """执行一次**空迁移**（P-1.6 只允许 dry-run，且必须指向副本）。

    前置边界（任一不满足即拒绝，不动任何数据）：开关开启；`dry_run=True` 且目标
    不是主库；维护态 `ACTIVE`；单实例锁；写入封锁；排空判据；`preflight == ready`。

    阶段结果判定口径（计划 §2.5.1 的"失败二分"，全部由**本地事实**决定）：

    * `BEGIN IMMEDIATE` 抛错 ⇒ **没有事务**：phase 保持 `PREPARED`，不伪造
      `TX_OPEN`，不写任何行；保持封锁。
    * 事务内异常 + `ROLLBACK` 明确成功 + 只读复核结构未变 ⇒ `KNOWN_FAILED`：
      推进 `TX_ROLLED_BACK`（T3/T5）并写 `failed` 行（`migration_incomplete` ⇒
      RECOVERY_REQUIRED，写入保持封锁）。
    * `ROLLBACK` 失败 / 只读复核失败 / 复核不可用 ⇒ `OUTCOME_UNKNOWN`：**不伪造
      `TX_ROLLED_BACK`**，**不写误导性的 failed 行**；关闭迁移连接并确认后
      `halt_migration("rollback_unverified")`。
    * `COMMIT` 抛错 ⇒ `OUTCOME_UNKNOWN`：phase 停 `COMMIT_ATTEMPTED`，不写 success
      / failed 猜结果；**先关闭并确认迁移连接**，再 `halt_migration("commit_outcome_unknown")`；
      连接关闭失败 ⇒ `FatalExit`（不把连接池确认冒充为全部连接已关闭）。
    * `running → success` 失败 ⇒ 停 `COMMIT_ATTEMPTED` + halt；`A` 之后内存落地
      失败 ⇒ halt（`HALTED` 语义）。
    """
    from backend.database import backup, gate, maint, pool

    if not config.MIGRATION_ENABLED:
        raise MigrationDisabled()
    target = Path(db_path or config.DB_PATH).resolve()
    live = Path(config.DB_PATH).resolve()
    if dry_run and target == live:
        raise MigrationRefused(
            "dry_run_requires_copy",
            "P-1.6 空迁移 dry-run 只允许对副本执行；生产/开发主库要等 P-1.8",
        )
    if not dry_run:
        raise MigrationRefused("p17_required", "P-1.6 只实现空迁移 dry-run，真实迁移在 P-1.7/P-1.8")

    ready_state, _reason = gate.judge()
    if ready_state is not maint.MaintState.ACTIVE:
        raise MigrationRefused(
            "maintenance_required",
            f"迁移只能在维护态 ACTIVE 下执行，当前 {ready_state.name}",
        )

    with migration_run_lock(lock_path):
        gate.apply_state(ready_state)          # 封锁写入（含关闭业务写连接）
        rec = MigrationRecord(migration_id=f"mig-{_utc_now().replace(' ', 'T')}-{os.getpid()}")

        drain_kwargs: dict[str, Any] = {"rounds": drain_rounds,
                                        "min_interval": drain_min_interval,
                                        "db_path": target}
        if clock is not None:
            drain_kwargs["clock"] = clock
        if sleeper is not None:
            drain_kwargs["sleeper"] = sleeper
        if checkpoint is not None:
            drain_kwargs["checkpoint"] = checkpoint
        if holders is not None:
            drain_kwargs["holders"] = holders
        drain_result = gate.drain(**drain_kwargs)
        if not drain_result.continue_allowed:
            halt = halt_migration("drain_failed", rec=rec, detail={"detail": drain_result.detail},
                                  fault_log_path=fault_log_path)
            return {"ok": False, "code": "drain_failed", "status": "halted",
                    "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                    "migration_id": rec.migration_id,
                    "halt_tail": halt["halt_tail"], "requirement": "recovery_required"}

        if snapshot:
            try:
                info = backup.snapshot_set(stage=snapshot_stage, src_db=target, stop_write=True)
                backup.verify_set(info["path"])
            except Exception as exc:      # noqa: BLE001 - 任何备份失败都必须停机
                halt_migration("snapshot_failed", rec=rec, detail={"error": str(exc)},
                               fault_log_path=fault_log_path)
                return {"ok": False, "code": "snapshot_failed", "status": "halted",
                        "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                        "migration_id": rec.migration_id,
                        "requirement": "recovery_required"}

        conn = open_connection(target)
        try:
            pre = preflight(conn)
            if pre["state"] != "ready":
                raise MigrationRefused(
                    "migration_not_ready",
                    f"preflight={pre['state']} current={pre.get('current_version')}",
                )
            hash_before = structure_hash(conn)
            seq = _next_seq(conn)
            started_at = _utc_now()
            detail = {"dry_run": True, "statements": 0, "target_version": TARGET_VERSION}

            begin_ok = False
            try:
                conn.execute("BEGIN IMMEDIATE")
                begin_ok = True
                set_phase_memory(rec, Phase.PREPARED, Phase.TX_OPEN)          # T1
                _insert_row(conn, migration_id=rec.migration_id, seq=seq,
                            version=TARGET_VERSION, status="running",
                            started_at=started_at, finished_at=None,
                            detail_json=json.dumps(detail, sort_keys=True, ensure_ascii=False))
                set_phase_memory(rec, Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED)  # T2
                # 空迁移：P-1.6 的 dry-run 不含任何 DDL，这里就是全部"迁移内容"
            except Exception as exc:      # noqa: BLE001 - 事务内任何异常都要判定能否确认失败
                rollback_ok = False
                rollback_error: str | None = None
                if begin_ok:
                    try:
                        conn.execute("ROLLBACK")
                        rollback_ok = True
                    except Exception as rb:      # noqa: BLE001
                        rollback_error = f"{type(rb).__name__}: {rb}"
                recheck_ok = False
                recheck_detail: str | None = None
                if begin_ok and rollback_ok:
                    recheck_ok, recheck_detail = _recheck_structure_unchanged(target, hash_before)

                closed, close_error = _close_migration_connection(conn)
                pool.ensure_blocked(pool.REASON_MAINTENANCE)
                _safe_log({
                    "event": "migration_transaction_failed",
                    "migration_id": rec.migration_id,
                    "phase": rec.phase.value,
                    "phase_epoch": rec.phase_epoch,
                    "begin_ok": begin_ok,
                    "rollback_ok": rollback_ok,
                    "rollback_error": rollback_error,
                    "recheck_ok": recheck_ok,
                    "recheck_detail": recheck_detail,
                    "connection_closed": closed,
                    "close_error": close_error,
                    "error": str(exc),
                    "ts": _utc_now(),
                }, fault_log_path)

                if not begin_ok:
                    # BEGIN 失败：没有事务被打开，绝不伪造 TX_OPEN/回滚状态
                    return {"ok": False, "code": "begin_failed", "result": "no_transaction",
                            "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                            "migration_id": rec.migration_id,
                            "rows_written": 0, "connection_closed": closed,
                            "error": str(exc)}

                if rollback_ok and recheck_ok:
                    old_phase = rec.phase
                    if old_phase in (Phase.TX_OPEN, Phase.COMMIT_ATTEMPTED):
                        set_phase_memory(rec, old_phase, Phase.TX_ROLLED_BACK)   # T3/T5
                    failed_ok = _mark_failed(target, rec.migration_id, seq, TARGET_VERSION,
                                             started_at, str(exc),
                                             {"known_failed": True,
                                              "recheck": recheck_detail})
                    if not failed_ok:
                        halt_migration("failed_row_unwritable", rec=rec,
                                       detail={"error": str(exc)},
                                       fault_log_path=fault_log_path)
                        return {"ok": False, "code": "failed_row_unwritable",
                                "result": "outcome_unknown",
                                "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                                "migration_id": rec.migration_id,
                                "requirement": "recovery_required"}
                    return {"ok": False, "code": "migration_failed", "result": "known_failed",
                            "status": "rolled_back", "phase": rec.phase.value,
                            "phase_epoch": rec.phase_epoch, "migration_id": rec.migration_id,
                            "failed_row": True, "error": str(exc),
                            "requirement": "recovery_required"}

                # OUTCOME_UNKNOWN：回滚/复核不可确认 ⇒ 不伪造任何持久痕迹，直接 halt
                if not closed:
                    raise FatalExit(
                        "migration_connection_open",
                        f"迁移连接未能确认关闭：{close_error}",
                    )
                halt_migration("rollback_unverified", rec=rec,
                               detail={"error": str(exc), "rollback_ok": rollback_ok,
                                       "rollback_error": rollback_error,
                                       "recheck_ok": recheck_ok,
                                       "recheck_detail": recheck_detail},
                               fault_log_path=fault_log_path)
                return {"ok": False, "code": "rollback_unverified",
                        "result": "outcome_unknown", "status": "halted",
                        "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                        "migration_id": rec.migration_id,
                        "requirement": "recovery_required"}

            try:
                conn.execute("COMMIT")
            except Exception as exc:      # noqa: BLE001 - COMMIT 结果不可知必须就地判定
                closed, close_error = _close_migration_connection(conn)
                pool.ensure_blocked(pool.REASON_MAINTENANCE)
                _safe_log({
                    "event": "migration_commit_outcome_unknown",
                    "migration_id": rec.migration_id,
                    "phase": rec.phase.value,
                    "phase_epoch": rec.phase_epoch,
                    "connection_closed": closed,
                    "close_error": close_error,
                    "error": str(exc),
                    "ts": _utc_now(),
                }, fault_log_path)
                if not closed:
                    # 不能把"连接池已封锁"冒充为"本次迁移连接已关闭"
                    raise FatalExit("migration_connection_open",
                                    f"COMMIT 异常后迁移连接未能确认关闭：{close_error}")
                halt_migration("commit_outcome_unknown", rec=rec,
                               detail={"error": str(exc)}, fault_log_path=fault_log_path)
                return {"ok": False, "code": "commit_outcome_unknown", "status": "halted",
                        "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                        "migration_id": rec.migration_id, "rows_written": 0,
                        "connection_closed": True,
                        "requirement": "recovery_required"}

            try:
                conn.execute("BEGIN IMMEDIATE")
                updated = conn.execute(
                    "UPDATE schema_migrations SET status='success', finished_at=?"
                    " WHERE migration_id=? AND status='running'",
                    (_utc_now(), rec.migration_id),
                ).rowcount
                conn.execute("COMMIT")
                if int(updated) != 1:
                    raise MigrationStateError("phase_row_missing",
                                              "running 行未找到，无法确认迁移已落地")
            except Exception as exc:      # noqa: BLE001 - 无持久痕迹不得推进 phase
                try:
                    conn.execute("ROLLBACK")
                except Exception:
                    pass
                closed, close_error = _close_migration_connection(conn)
                pool.ensure_blocked(pool.REASON_MAINTENANCE)
                if not closed:
                    raise FatalExit("migration_connection_open",
                                    f"状态行落盘失败后迁移连接未能确认关闭：{close_error}")
                halt_migration("phase_commit_failed", rec=rec,
                               detail={"error": str(exc)}, fault_log_path=fault_log_path)
                return {"ok": False, "code": "phase_commit_failed", "status": "halted",
                        "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                        "migration_id": rec.migration_id,
                        "requirement": "recovery_required"}

            try:
                set_phase_memory(rec, Phase.COMMIT_ATTEMPTED, Phase.COMMITTED)   # T4
            except Exception as exc:      # noqa: BLE001 - A 之后落地失败 ⇒ HALTED
                pool.ensure_blocked(pool.REASON_MAINTENANCE)
                halt_migration("phase_apply_failed", rec=rec,
                               detail={"error": str(exc)}, fault_log_path=fault_log_path)
                return {"ok": False, "code": "phase_apply_failed", "status": "halted",
                        "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                        "migration_id": rec.migration_id,
                        "requirement": "recovery_required"}

            return {"ok": True, "code": "migration_committed", "status": "committed",
                    "phase": rec.phase.value, "phase_epoch": rec.phase_epoch,
                    "migration_id": rec.migration_id, "seq": seq,
                    "version": TARGET_VERSION, "dry_run": True, "statements": 0}
        finally:
            try:
                conn.close()
            except Exception:             # noqa: BLE001 - 清理不得掩盖上面的判定结果
                pass


def rollback_to_snapshot(set_path: str | Path | None = None, *,
                         db_path: str | Path | None = None,
                         drain_rounds: int = 2, drain_min_interval: float = 10.0,
                         clock: Any = None, sleeper: Any = None,
                         checkpoint: Any = None, holders: Any = None,
                         fault_log_path: str | Path | None = None) -> dict[str, Any]:
    """P-1.6 的回滚入口：**只做安全前置检查与只读验证**，不执行真实恢复。

    计划 §2.5.2-3 把"恢复"放在停机窗口：停写 → 排空 → 关闭全部连接 → 用
    `pre-migrate` 备份集原子替换 → L1/L2 校验 → 人工确认后清维护文件 → 重启。
    该流程属 P-1.7 演练内容，**P-1.6 不得触碰目标库**。

    因此本函数：

    1. 要求迁移开关开启、维护态 `ACTIVE`；
    2. 定位备份集（显式传入或最新集），用 `backup.verify_set()` 做**纯只读**验证；
    3. 一律以 `MigrationRefused("p17_required")` 拒绝执行恢复。

    **绝不调用** `backup.restore_set()`，不替换/覆盖/隔离目标数据库，也不因为
    "失败后会还原"就提前开放真实恢复能力。
    """
    from backend.database import backup, gate, maint

    if not config.MIGRATION_ENABLED:
        raise MigrationDisabled()
    state, _reason = gate.judge()
    if state is not maint.MaintState.ACTIVE:
        raise MigrationRefused("maintenance_required",
                               f"回滚只能在维护态 ACTIVE 下执行，当前 {state.name}")
    target = Path(db_path or config.DB_PATH).resolve()
    set_dir = Path(set_path) if set_path else None
    if set_dir is None:
        latest = backup.latest_set()
        if not latest:
            raise MigrationRefused("no_backup_set", "没有任何可用备份集")
        set_dir = Path(latest["path"])
    verdict = backup.verify_set(set_dir)          # 只读：不复制、不替换、不算 L3
    if not verdict.get("ok"):
        raise MigrationRefused("backup_invalid", f"备份集验证未通过：{verdict.get('errors')}")
    raise MigrationRefused(
        "p17_required",
        f"P-1.6 不执行真实恢复（目标 {target}，备份集 {set_dir.name}）；"
        "停机窗口恢复属 P-1.7，本阶段只完成前置检查与只读验证",
    )


__all__ = [
    "BASELINE_APP_VERSION",
    "BASELINE_STRUCTURE_HASH",
    "BASELINE_VERSION",
    "BASE_TABLES",
    "BaselineRefused",
    "MigrationError",
    "MigrationStateError",
    "SCHEMA_MIGRATIONS_DDL",
    "VERSION_SEQ",
    "current_version",
    "ensure_schema_table_transactional",
    "migration_rows",
    "open_connection",
    "preflight",
    "record_baseline",
    "schema_table_exists",
    "structure_hash",
    "structure_profile_for",
    "structure_signature",
    "supported_structure_hashes",
    "tasks_state",
    # P-1.6 迁移状态机
    "AlreadyRunning",
    "DispatchTableBroken",
    "FatalExit",
    "IllegalTransition",
    "LEGAL_TRANSITIONS",
    "MigrationDisabled",
    "MigrationHalted",
    "MigrationRecord",
    "MigrationRefused",
    "Phase",
    "PhaseMismatch",
    "PhaseResult",
    "TARGET_VERSION",
    "TRANSITION_DISPATCH",
    "TRANSITION_NAMES",
    "WriteOutcome",
    "append_reason",
    "check_transition_table",
    "emit_event",
    "halt_migration",
    "halt_tail",
    "migration_run_lock",
    "migration_state_summary",
    "reset_issuer_token_for_tests",
    "rollback_to_snapshot",
    "run",
    "set_phase",
    "set_phase_memory",
    "write_event",
    "write_tx_event",
]
