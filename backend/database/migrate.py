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

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
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

# 由 structure_hash() 在生产实测结构上算出的常量（见 tests/test_migration_baseline.py）。
# 列顺序、注释、空白均不参与摘要，因此"历史 ALTER 出来的库"与"全新 init_db 的库"
# 得到同一个值；出现真实结构漂移才会不匹配。
BASELINE_STRUCTURE_HASH = "eb5fd0ce54a87d6e748a144a35ce97903b71acfb5415eb9fd8e4cba28f88da0b"


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
    cols = sorted(
        ([row[1], row[2], int(row[3]), row[4], int(row[5])]
         for row in conn.execute(f'PRAGMA table_info("{name}")')),
        key=lambda item: item[0],
    )
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
    """结构签名：语义等价于 DDL、但对列顺序/注释/空白不敏感。

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

    - 表不存在：建表、插基线行、校验 `count == 1`、提交。
    - 表存在且恰有本迁移成功行：幂等返回，不重复插入。
    - 其它状态：抛 `MigrationStateError`（不猜测、不补写）。
    任一步异常 → `ROLLBACK`，因此不存在"表在、基线缺"的中间态。
    """
    _require_autocommit(conn)
    seq = VERSION_SEQ[version] if seq is None else seq
    created_at = _utc_now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(SCHEMA_MIGRATIONS_DDL)
        count = _count_rows(conn)
        if count == 0:
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
    expected_structure_hash: str | None = None,
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
        expected_hash = expected_structure_hash or BASELINE_STRUCTURE_HASH
        if expected_hash and actual_hash != expected_hash:
            raise BaselineRefused(
                "structure_mismatch",
                f"actual={actual_hash} expected={expected_hash}",
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
        result["tasks_present"] = tasks["tasks_present"]
        result["tasks_rows"] = tasks["tasks_rows"]
        return result
    finally:
        if own_conn:
            conn.close()


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
    "structure_signature",
    "tasks_state",
]
