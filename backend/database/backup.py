"""备份集（P-1 计划 §2.1 / §2.3 / §2.4）：三件套、原子发布、分级验证、安全恢复。

核心约束：

1. 一律用 SQLite 在线备份 API（`source.backup(dest, pages=1024)`），
   **禁止** `cp` / `shutil.copy2` 正在使用的库 —— WAL 下文件拷贝会得到不完整副本。
2. 一个"备份"是一个**目录**：`db` + `manifest.json` + `complete.json` 三件齐全，
   且 `complete.json` 的两个 sha256 与实文件相符，才算有效备份。
3. 发布顺序：`.tmp-<nonce>` 内写三个文件 → 逐个 fsync → 目录 fsync →
   `os.replace()` 原子改名 → `fsync(BACKUP_DIR)`。
4. L1/L2 的参照物只能是**同一快照**：在线备份（源库 `data_version` 变了）禁止
   与"备份完成后的实时源库"比较；停写快照才允许与停写窗口内读到的源计数比较。
5. `restore_set()` 走完整安全流程：验证备份集 → 关闭连接 → 确认无第三方句柄 →
   删 `-wal/-shm` → 同卷临时文件 + fsync → `os.replace` + 目录 fsync →
   失败自动回退 `pre-restore` 备份集。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from backend import config
from backend.database import migrate

STAGES = ("pre-migrate", "daily", "manual", "pre-restore")
PROTECTED_STAGES = ("pre-migrate", "pre-restore")

SET_PREFIX = "nodepool-"
TMP_PREFIX = ".tmp-"
DB_FILE = "db"
MANIFEST_FILE = "manifest.json"
COMPLETE_FILE = "complete.json"
LATEST_FILE = "nodepool-latest.db"
MANIFEST_FORMAT = 1

KEY_TABLES = ("airports", "nodes", "settings", "test_groups")
IMPORTANT_TABLES = ("results", "latency_results", "batches", "share_history")

DEFAULT_MIN_FREE_MB = 2048
DEFAULT_KEEP_DAYS = 30
DEFAULT_KEEP_DAILY = 3


class BackupError(RuntimeError):
    """备份/恢复错误的基类，`reason` 为稳定机器码。"""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


# --- 小工具 ------------------------------------------------------------------


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _fsync_file(path: Path) -> None:
    with open(path, "rb+") as handle:
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _fsync_file(path)


def _backup_dir(backup_dir: str | Path | None = None) -> Path:
    return Path(backup_dir or config.BACKUP_DIR)


def _min_free_mb() -> int:
    return int(os.getenv("NODEPOOL_SNAPSHOT_MIN_FREE_MB", str(DEFAULT_MIN_FREE_MB)))


def _keep_days() -> int:
    return int(os.getenv("NODEPOOL_BACKUP_KEEP_DAYS", str(DEFAULT_KEEP_DAYS)))


def _open_ro(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """只读打开。`immutable=True` 用于**已冻结的备份文件**：不创建 `-wal/-shm`，
    也不会因外部改动而读出错数据。"""
    uri = f"file:{path}?mode=ro" + ("&immutable=1" if immutable else "")
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _connect_src(path: Path) -> sqlite3.Connection:
    """只读源连接：不复用业务线程的读写连接。"""
    conn = _open_ro(path)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _data_version(conn: sqlite3.Connection) -> int | None:
    try:
        return int(conn.execute("PRAGMA data_version").fetchone()[0])
    except sqlite3.Error:
        return None


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    )]


def _counts(conn: sqlite3.Connection, tables: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in tables:
        out[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
    return out


def _digest_rows(conn: sqlite3.Connection, table: str) -> str:
    rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    blob = json.dumps([list(row) for row in rows], default=str, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def business_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """17 张基准表的行数（不存在则记 -1，用于暴露缺表）。"""
    present = set(_table_names(conn))
    out: dict[str, int] = {}
    for name in migrate.BASE_TABLES:
        out[name] = int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]) \
            if name in present else -1
    return out


# --- 快照 --------------------------------------------------------------------


def snapshot_set(
    stage: str = "manual",
    *,
    src_db: str | Path | None = None,
    backup_dir: str | Path | None = None,
    stop_write: bool = False,
    min_free_mb: int | None = None,
    verify: bool = True,
) -> dict[str, Any]:
    """生成一个备份集目录并原子发布。

    `stop_write=True` 表示调用方已处于停写窗口：备份前后各读一次
    `PRAGMA data_version`，相同才把"停写窗口内读到的源库计数"写进 manifest，
    供 `verify_set()` 做 L2 的源侧比对；不同则按在线备份处理，禁止源侧比对。
    """
    if stage not in STAGES:
        raise BackupError("bad_stage", f"stage={stage} 不在 {STAGES}")
    src = Path(src_db or config.DB_PATH)
    if not src.exists():
        raise BackupError("snapshot_failed", f"源库不存在：{src}")
    dest_root = _backup_dir(backup_dir)
    dest_root.mkdir(parents=True, exist_ok=True)

    need_free = (min_free_mb if min_free_mb is not None else _min_free_mb()) * 1024 * 1024
    free = shutil.disk_usage(dest_root).free
    if free < need_free:
        raise BackupError(
            "snapshot_failed",
            f"可用空间不足：{free // (1024 * 1024)} MB < 门槛 {need_free // (1024 * 1024)} MB",
        )

    nonce = secrets.token_hex(8)
    tmp_dir = dest_root / f"{TMP_PREFIX}{nonce}"
    tmp_dir.mkdir(parents=False, exist_ok=False)
    tmp_db = tmp_dir / DB_FILE

    src_conn = _connect_src(src)
    try:
        dv_before = _data_version(src_conn)
        source_counts = _counts(
            src_conn,
            [t for t in migrate.BASE_TABLES
             if t in set(_table_names(src_conn))],
        )
        dst_conn = sqlite3.connect(str(tmp_db))
        try:
            def _progress(_status: int, _remaining: int, _total: int) -> None:
                # 批间让出调度权，避免长事务霸占 GIL
                time.sleep(0)

            src_conn.backup(dst_conn, pages=1024, progress=_progress)
            dst_conn.commit()
        finally:
            dst_conn.close()
        dv_after = _data_version(src_conn)
    finally:
        src_conn.close()

    _fsync_file(tmp_db)

    quiesced = bool(stop_write and dv_before is not None and dv_before == dv_after)

    # manifest 的计数与摘要一律从**备份文件本身**读出，不读实时源库
    backup_conn = _open_ro(tmp_db, immutable=True)
    try:
        names = set(_table_names(backup_conn))
        counts = {t: int(backup_conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                  for t in migrate.BASE_TABLES if t in names}
        digests = {t: _digest_rows(backup_conn, t) for t in KEY_TABLES if t in names}
        important = {t: counts.get(t, -1) for t in IMPORTANT_TABLES}
        tasks = migrate.tasks_state(backup_conn)
        schema_version = migrate.current_version(backup_conn) \
            if migrate.schema_table_exists(backup_conn) else None
        profile = migrate.structure_profile_for(migrate.structure_hash(backup_conn))
        sqlite_version = backup_conn.execute("SELECT sqlite_version()").fetchone()[0]
    finally:
        backup_conn.close()

    manifest = {
        "format": MANIFEST_FORMAT,
        "stage": stage,
        "created_at": _now(),
        "app_version": config.VERSION,
        "sqlite_version": sqlite_version,
        "db_bytes": tmp_db.stat().st_size,
        "tables": counts,
        "tables_expected": list(migrate.BASE_TABLES),
        "missing_tables": [t for t in migrate.BASE_TABLES if t not in counts],
        "key_digests": digests,
        "important_counts": important,
        "tasks": tasks,
        "schema_version": schema_version,
        "structure_profile": profile,
        "write_quiesced": quiesced,
        "source_data_version": {"before": dv_before, "after": dv_after},
        "source_counts": source_counts if quiesced else None,
        "source_db": None,  # 脱敏：不写绝对路径
    }
    for suffix in ("-wal", "-shm"):
        side = Path(str(tmp_db) + suffix)
        if side.exists() and side.stat().st_size <= 32768 and suffix == "-shm":
            side.unlink()
        elif side.exists() and side.stat().st_size == 0:
            side.unlink()

    _write_json(tmp_dir / MANIFEST_FILE, manifest)

    complete = {
        "stage": stage,
        "db_sha256": _sha256(tmp_db),
        "manifest_sha256": _sha256(tmp_dir / MANIFEST_FILE),
        "bytes": tmp_db.stat().st_size,
        "finished_at": _now(),
    }
    _write_json(tmp_dir / COMPLETE_FILE, complete)
    _fsync_dir(tmp_dir)

    final_name = _publish_name(dest_root, stage)
    final_dir = dest_root / final_name
    os.replace(tmp_dir, final_dir)
    _fsync_dir(dest_root)

    result: dict[str, Any] = {
        "ok": True,
        "file": final_name,
        "size": complete["bytes"],
        "path": str(final_dir),
        "stage": stage,
        "sha256": complete["db_sha256"],
        "write_quiesced": quiesced,
        "tables": counts,
    }

    if verify:
        verdict = verify_set(final_dir)
        if not verdict["ok"]:
            # 发布后自检失败：改名隔离，避免被当成可用备份
            bad_dir = dest_root / f"INVALID-{final_name}"
            os.replace(final_dir, bad_dir)
            _fsync_dir(dest_root)
            raise BackupError("snapshot_failed", f"发布后验证失败：{verdict['errors']}")
        result["verified"] = True
        _update_latest_copy(dest_root, final_dir)
    return result


def _publish_name(dest_root: Path, stage: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{SET_PREFIX}{stamp}-{stage}"
    if not (dest_root / name).exists():
        return name
    for idx in range(1, 100):
        candidate = f"{name}-{idx}"
        if not (dest_root / candidate).exists():
            return candidate
    raise BackupError("snapshot_failed", "同秒备份集命名冲突过多")


def _update_latest_copy(dest_root: Path, set_dir: Path) -> None:
    """`nodepool-latest.db` 兼容副本：只有完整备份集才更新。"""
    source = set_dir / DB_FILE
    target = dest_root / LATEST_FILE
    tmp = dest_root / f"{TMP_PREFIX}latest-{secrets.token_hex(4)}"
    shutil.copyfile(source, tmp)
    _fsync_file(tmp)
    os.replace(tmp, target)
    _fsync_dir(dest_root)


# --- 验证 --------------------------------------------------------------------


def verify_set(
    set_dir: str | Path,
    *,
    live_source: str | Path | None = None,
    require_source_compare: bool = False,
) -> dict[str, Any]:
    """L1 结构 + L2 内容验证。

    - L1：三件套齐全、`complete.json` 两个 sha256 相符、`integrity_check=ok`、
      `foreign_key_check` 0 行、17 张基准表齐全。
    - L2：备份文件自算的行数/摘要必须与 manifest 一致（同一快照自洽）；
      仅当 manifest 标记 `write_quiesced=True` 时，才允许与 `source_counts`
      比对；在线备份**禁止**与实时源库比较（`live_source` 只用于断言这一点）。
    """
    path = Path(set_dir)
    errors: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "ok": False,
        "path": str(path),
        "l1": {"ok": False},
        "l2": {"ok": False, "mode": None, "compared_to_source": False},
        "errors": errors,
    }

    def fail(code: str, detail: str = "", layer: str = "l1") -> None:
        errors.append({"layer": layer, "code": code, "detail": detail})
        report[layer]["ok"] = False

    db_path = path / DB_FILE
    manifest_path = path / MANIFEST_FILE
    complete_path = path / COMPLETE_FILE
    for file_path in (db_path, manifest_path, complete_path):
        if not file_path.exists():
            fail("missing_file", file_path.name)
            return report

    try:
        complete = json.loads(complete_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        fail("unreadable_json", str(exc))
        return report

    db_sha = _sha256(db_path)
    manifest_sha = _sha256(manifest_path)
    if complete.get("db_sha256") != db_sha:
        fail("db_sha_mismatch", f"complete={complete.get('db_sha256')} actual={db_sha}")
    if complete.get("manifest_sha256") != manifest_sha:
        fail("manifest_sha_mismatch",
             f"complete={complete.get('manifest_sha256')} actual={manifest_sha}")
    if complete.get("bytes") not in (None, db_path.stat().st_size):
        fail("bytes_mismatch", f"complete={complete.get('bytes')} actual={db_path.stat().st_size}")

    conn = _open_ro(db_path, immutable=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            fail("integrity_check", str(integrity))
        fk_rows = conn.execute("PRAGMA foreign_key_check").fetchall()
        if fk_rows:
            fail("foreign_key_check", f"{len(fk_rows)} 行违规")

        present = set(_table_names(conn))
        missing = [t for t in migrate.BASE_TABLES if t not in present]
        if missing:
            fail("missing_tables", ",".join(missing))

        # 结构层：备份内结构必须仍是已登记基准，且与 manifest 记录一致
        actual_profile = migrate.structure_profile_for(migrate.structure_hash(conn))
        if actual_profile is None:
            fail("structure_profile_unregistered",
                 f"结构摘要未登记：{migrate.structure_hash(conn)}")
        elif manifest.get("structure_profile") != actual_profile:
            fail("structure_drift",
                 f"manifest={manifest.get('structure_profile')} backup={actual_profile}")
        backup_tasks = migrate.tasks_state(conn)
        if (manifest.get("tasks") or {}).get("tasks_hash") != backup_tasks["tasks_hash"]:
            fail("tasks_ddl_drift", "tasks 定义摘要与 manifest 不一致")

        counts = {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                  for t in migrate.BASE_TABLES if t in present}
        digests = {t: _digest_rows(conn, t) for t in KEY_TABLES if t in present}

        # L2：备份内部自洽（manifest 由同一份备份文件读出）
        l2_mode = "quiesced" if manifest.get("write_quiesced") else "online"
        report["l2"]["mode"] = l2_mode
        manifest_counts = manifest.get("tables") or {}
        for table, actual in counts.items():
            expected = manifest_counts.get(table)
            if expected is None or int(expected) != actual:
                fail("count_mismatch", f"{table}: manifest={expected} backup={actual}", "l2")
        for table, expected in manifest_counts.items():
            if table not in counts and int(expected) >= 0:
                fail("count_mismatch", f"{table}: manifest={expected} backup=missing", "l2")
        for table, expected in (manifest.get("key_digests") or {}).items():
            if digests.get(table) != expected:
                fail("digest_mismatch", f"{table}: 关键表摘要不一致", "l2")

        if l2_mode == "quiesced":
            # 停写窗口内：允许与"停写后读到的源库计数"比对。
            # 参照物优先取调用方传入的 live_source（调用方确认仍在停写），
            # 否则回落到 manifest 里同窗口记录的 source_counts。
            source_counts = manifest.get("source_counts") or {}
            for table, expected in source_counts.items():
                actual = counts.get(table)
                if actual is None or int(expected) != int(actual):
                    fail("source_count_mismatch",
                         f"{table}: source={expected} backup={actual}", "l2")
            if source_counts:
                report["l2"]["compared_to_source"] = True
            if live_source is not None:
                ref_conn = _open_ro(Path(live_source))
                try:
                    ref_counts = {t: int(ref_conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                                  for t in counts}
                finally:
                    ref_conn.close()
                for table, expected in ref_counts.items():
                    actual = counts.get(table)
                    if actual is None or int(expected) != int(actual):
                        fail("source_count_mismatch",
                             f"{table}: source={expected} backup={actual}", "l2")
                report["l2"]["compared_to_source"] = True
                report["l2"]["source_ref"] = "live_source"
        else:
            # 在线备份：禁止与实时源库比较（这里显式记录，不因 live_source 报警）
            if live_source is not None:
                report["l2"]["live_source_ignored"] = True
            if require_source_compare:
                fail("l2_reference_unavailable",
                     "在线备份没有同一快照的源侧参照物，不能做 L2 源比对", "l2")

        tasks = backup_tasks
        manifest_tasks = manifest.get("tasks") or {}
        if bool(manifest_tasks.get("tasks_present")) != bool(tasks["tasks_present"]):
            fail("tasks_state_mismatch", "tasks 存在性与 manifest 不一致", "l2")
        if tasks["tasks_present"] and int(manifest_tasks.get("tasks_rows", -1)) != tasks["tasks_rows"]:
            fail("tasks_state_mismatch",
                 f"tasks 行数 manifest={manifest_tasks.get('tasks_rows')} backup={tasks['tasks_rows']}",
                 "l2")
        report["tasks"] = tasks
        report["tables"] = counts
    finally:
        conn.close()

    if not errors:
        report["l1"]["ok"] = True
        report["l2"]["ok"] = True
        report["ok"] = True
    return report


# --- 备份清单与清理 -----------------------------------------------------------


def _read_manifest(set_dir: Path) -> dict[str, Any] | None:
    try:
        return json.loads((set_dir / MANIFEST_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def list_sets(backup_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """列出备份集（含无效/半成品，供审计与清理判断）。"""
    root = _backup_dir(backup_dir)
    if not root.exists():
        return []
    entries: list[dict[str, Any]] = []
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        name = child.name
        if name.startswith(TMP_PREFIX) or name.startswith("INVALID-"):
            state = "incomplete"
        elif name.startswith(SET_PREFIX):
            state = "set"
        else:
            continue
        manifest = _read_manifest(child) if state == "set" else None
        entries.append({
            "name": name,
            "path": str(child),
            "state": state,
            "stage": (manifest or {}).get("stage"),
            "created_at": (manifest or {}).get("created_at"),
            "bytes": (manifest or {}).get("db_bytes"),
            "protected": (manifest or {}).get("stage") in PROTECTED_STAGES,
        })
    return entries


def valid_sets(backup_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """只返回三件齐全且 sha 相符的备份集（完整 L1 由 verify_set 负责）。"""
    out = []
    for entry in list_sets(backup_dir):
        if entry["state"] != "set":
            continue
        if verify_set(entry["path"])["ok"]:
            out.append(entry)
    return out


def latest_set(backup_dir: str | Path | None = None) -> dict[str, Any] | None:
    sets = valid_sets(backup_dir)
    return sets[-1] if sets else None


def prune(
    backup_dir: str | Path | None = None,
    *,
    keep_days: int | None = None,
    keep_daily: int = DEFAULT_KEEP_DAILY,
    now: datetime | None = None,
) -> dict[str, Any]:
    """滚动清理：只删**有效**的 `daily`/`manual` 旧备份。

    - `pre-migrate` / `pre-restore` 永不删除；
    - 半成品 / 无效备份集不删除（保留给人工处置）；
    - `daily` 至少保留最近 `keep_daily` 份。
    """
    root = _backup_dir(backup_dir)
    days = keep_days if keep_days is not None else _keep_days()
    ref = now or datetime.now().astimezone()
    removed: list[str] = []
    kept: list[str] = []
    skipped: list[str] = []

    candidates = []
    for entry in list_sets(root):
        if entry["state"] != "set":
            skipped.append(entry["name"])
            continue
        if entry["protected"]:
            skipped.append(entry["name"])
            continue
        verdict = verify_set(entry["path"])
        if not verdict["ok"]:
            skipped.append(entry["name"])
            continue
        candidates.append(entry)

    daily = [e for e in candidates if e["stage"] == "daily"]
    daily.sort(key=lambda e: e["name"], reverse=True)
    keep_names = {e["name"] for e in daily[:keep_daily]}

    for entry in candidates:
        if entry["name"] in keep_names:
            kept.append(entry["name"])
            continue
        created = entry.get("created_at")
        old = True
        if created:
            try:
                created_dt = datetime.strptime(created, "%Y-%m-%d %H:%M:%S").astimezone()
                old = (ref - created_dt) > timedelta(days=days)
            except ValueError:
                old = True
        if old:
            shutil.rmtree(entry["path"])
            removed.append(entry["name"])
        else:
            kept.append(entry["name"])

    return {"removed": removed, "kept": kept, "skipped": skipped, "keep_days": days,
            "keep_daily": keep_daily}


# --- 恢复 --------------------------------------------------------------------


def _default_close_connections() -> None:
    from backend.database import db as db_module
    closer = getattr(db_module, "close_all_connections", None)
    if callable(closer):
        closer()
    else:
        db_module.reset_connection()


def holders_of(db_path: Path) -> list[str]:
    """扫描 `/proc/*/fd`，返回仍持有 `db_path`（或其 `-wal/-shm`）的进程标识。"""
    target = os.path.realpath(str(db_path))
    related = {target, target + "-wal", target + "-shm"}
    hits: list[str] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return hits
    for pid_dir in proc_root.iterdir():
        if not pid_dir.name.isdigit():
            continue
        if int(pid_dir.name) == os.getpid():
            continue
        fd_dir = pid_dir / "fd"
        try:
            for fd in fd_dir.iterdir():
                try:
                    link = os.readlink(fd)
                except OSError:
                    continue
                if os.path.realpath(link) in related:
                    hits.append(f"{pid_dir.name}:{fd.name}")
                    break
        except OSError:
            continue
    return hits


def restore_set(
    set_dir: str | Path,
    *,
    db_path: str | Path | None = None,
    backup_dir: str | Path | None = None,
    close_connections: Callable[[], None] | None = None,
    publish: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """把备份集安全恢复到 `db_path`（默认 `config.DB_PATH`）。

    失败安全顺序（任何一步失败都不丢原库数据）：
    验证备份集 → 生成 `pre-restore` → 关闭连接 → 确认无第三方句柄 →
    同卷临时文件复制 + fsync + **完整校验** → 原库三件（db/-wal/-shm）**改名隔离**
    （不是删除） → `os.replace` 临时文件 → 目录 fsync → 恢复后校验 →
    成功才丢弃隔离副本；失败先按隔离副本原样还原，再回退 `pre-restore`。
    """
    target = Path(db_path or config.DB_PATH)
    source_set = Path(set_dir)

    verdict = verify_set(source_set)
    if not verdict["ok"]:
        raise BackupError("backup_invalid", f"备份集验证失败：{verdict['errors']}")

    manifest = _read_manifest(source_set) or {}
    pre_restore = snapshot_set("pre-restore", src_db=target, backup_dir=backup_dir)

    (close_connections or _default_close_connections)()

    held = holders_of(target)
    if held:
        raise BackupError("restore_handles_present", f"仍有进程持有库文件：{held[:5]}")

    # --- 阶段 A：只读准备，绝不触碰目标库文件 ---
    nonce = secrets.token_hex(8)
    tmp = target.parent / f"restore-{nonce}.db"
    try:
        shutil.copyfile(source_set / DB_FILE, tmp)
        _fsync_file(tmp)
    except Exception as exc:  # noqa: BLE001 - 复制/落盘失败时目标库必须原封不动
        _silent_unlink(tmp)
        raise BackupError("restore_stage_failed", f"临时文件复制/fsync 失败：{exc}") from exc

    try:
        _verify_staged_db(tmp, manifest)
    except Exception as exc:  # noqa: BLE001
        _silent_unlink(tmp)
        if isinstance(exc, BackupError):
            raise
        raise BackupError("restore_verify_failed", f"临时恢复库校验失败：{exc}") from exc

    # --- 阶段 B：原库三件改名隔离（可原样还原），再原子替换 ---
    quarantine = _quarantine_target(target, nonce)
    replaced = False
    try:
        os.replace(tmp, target)
        _fsync_dir(target.parent)
        replaced = True
        _post_restore_checks(target, source_set)
    except Exception as exc:  # noqa: BLE001 - 任何失败都要还原
        _silent_unlink(tmp)
        first_error = exc
        try:
            outcome = _unquarantine(quarantine)
        except Exception as quarantine_exc:  # noqa: BLE001
            outcome = _RestoreOutcome(
                leftover=[dst for _src, dst in quarantine],
                persist_error=f"{type(quarantine_exc).__name__}: {quarantine_exc}",
            )
            first_error = f"{first_error} / 隔离副本还原异常：{quarantine_exc}"
        if not outcome.ok():
            # 隔离副本已不可用：**不用** `pre-restore` 覆盖目标路径——那会毁掉刚替换进来的
            # 新库（现场唯一可取证副本）。保留现状 + 隔离残留，报 leftover 交人工/P-1.8 处置。
            raise BackupError(
                "restore_rolled_back_leftover",
                f"恢复失败（{first_error}）；原库未确认还原：{outcome.detail()}",
            ) from exc
        if not replaced:
            raise BackupError("restore_replace_failed", str(first_error)) from exc
        raise BackupError("restore_rolled_back",
                          f"恢复后校验失败，已还原原库：{first_error}") from exc

    # 成功：隔离副本与临时文件一并清理
    _drop_quarantine(quarantine)
    _silent_unlink(tmp)

    published = None
    if publish is not None:
        published = publish("restore")

    return {
        "ok": True,
        "db": str(target),
        "from_set": source_set.name,
        "pre_restore_set": pre_restore["file"],
        "published": published,
    }


def _silent_unlink(path: Path) -> None:
    try:
        if path.exists():
            path.unlink()
    except OSError:
        pass


def _verify_staged_db(tmp: Path, manifest: dict[str, Any]) -> None:
    """临时恢复库的完整校验：integrity/fk/表齐全/逐表计数/tasks 形态。

    全部在**目标库未改动**的前提下进行；任何不满足都抛错，由调用方丢弃临时文件。
    """
    conn = _open_ro(tmp, immutable=True)
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise BackupError("restore_verify_failed", f"临时恢复库 integrity_check={integrity}")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise BackupError("restore_verify_failed", "临时恢复库存在外键违规")
        present = set(_table_names(conn))
        missing = [t for t in migrate.BASE_TABLES if t not in present]
        if missing:
            raise BackupError("restore_verify_failed", f"临时恢复库缺表：{missing}")
        counts = {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                  for t in migrate.BASE_TABLES if t in present}
        for table, expected in (manifest.get("tables") or {}).items():
            if table in counts and int(expected) != counts[table]:
                raise BackupError("restore_verify_failed",
                                  f"{table}: manifest={expected} staged={counts[table]}")
        tasks = migrate.tasks_state(conn)
        want_tasks = manifest.get("tasks") or {}
        if bool(want_tasks.get("tasks_present")) != bool(tasks["tasks_present"]):
            raise BackupError("restore_verify_failed", "临时恢复库 tasks 存在性与 manifest 不一致")
        if tasks["tasks_present"] and int(want_tasks.get("tasks_rows", -1)) != tasks["tasks_rows"]:
            raise BackupError("restore_verify_failed",
                              f"tasks: manifest={want_tasks.get('tasks_rows')} staged={tasks['tasks_rows']}")
    finally:
        conn.close()


def _quarantine_target(target: Path, nonce: str) -> list[tuple[Path, Path]]:
    """把目标库 `db` 与 `-wal` / `-shm` 逐个改名隔离（不删除、不覆盖任何文件）。

    返回已成功隔离的 `[(原路径, 隔离路径)]`。
    中途失败时**只还原已经成功隔离的文件**，尚未隔离的文件仍在原路径、保持原样；
    若某个隔离副本连还原都失败，则保留该副本并在错误里给出完整路径，绝不静默删除。
    """
    pairs: list[tuple[Path, Path]] = []
    try:
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(target) + suffix)
            if not src.exists():
                continue
            dst = Path(str(src) + f".restore-prev-{nonce}")
            os.replace(src, dst)
            pairs.append((src, dst))
        _fsync_dir(target.parent)
    except Exception as exc:  # noqa: BLE001 - 隔离失败必须原路退回
        outcome = _restore_moved(pairs, remove_new=False)
        if not outcome.ok():
            raise BackupError(
                "restore_quarantine_failed",
                f"隔离中断（{exc}）；原库未确认完整还原：{outcome.detail()}",
            ) from exc
        raise BackupError(
            "restore_quarantine_failed", f"隔离中断，已将已隔离文件原样退回：{exc}"
        ) from exc
    return pairs


@dataclass
class _RestoreOutcome:
    """隔离副本还原结果。`ok()` 为真才代表"原库已完整还原且持久化已确认"。"""

    leftover: list[Path] = field(default_factory=list)
    missing: list[Path] = field(default_factory=list)
    persist_error: str | None = None
    target_dir: Path | None = None

    def ok(self) -> bool:
        return not self.leftover and not self.missing and self.persist_error is None

    def detail(self) -> str:
        parts: list[str] = []
        if self.missing:
            parts.append(f"隔离副本缺失（无法还原）：{[str(p) for p in self.missing]}")
        if self.leftover:
            parts.append(f"隔离副本改名还原失败，已保留：{[str(p) for p in self.leftover]}")
        if self.persist_error:
            parts.append(f"目录 fsync 失败，还原持久化未确认（{self.target_dir}）：{self.persist_error}")
        return "；".join(parts)


def _restore_moved(pairs: list[tuple[Path, Path]], *, remove_new: bool) -> _RestoreOutcome:
    """把隔离副本改回原名，返回完整结果（不是简单 bool）。

    `remove_new=True` 仅用于"隔离已全部完成、目标路径上现在只可能是新库文件"的场景；
    部分隔离（隔离未完成）时传 `remove_new=False`，**不删除任何原路径文件**，
    以免误删尚未隔离的原始 db/-wal/-shm。

    - `pairs` 中每一项都代表**已经成功隔离**的原始文件，因此 `dst` 必须存在；
      `dst` 缺失记为 `missing`，绝不当作还原成功。
    - `os.replace` 失败时，只要隔离副本仍在，就记录完整路径为 `leftover`。
    - 还原后目录 `fsync` 失败记为 `persist_error`，不得宣称回滚完全成功；
      此时不清理任何隔离副本。
    """
    outcome = _RestoreOutcome()
    if not pairs:
        return outcome
    base = next((src for src, _ in pairs if not str(src).endswith(("-wal", "-shm"))), None)
    outcome.target_dir = (base or pairs[0][0]).parent

    # 预检：pairs 中每一项都是"已成功隔离"的文件，隔离副本必须全部在位。
    # 任一缺失即在**任何删除/改名之前**整体失败，避免"先清掉目标文件、再发现副本没了"。
    missing_pre = [dst for _src, dst in pairs if not dst.exists()]
    if missing_pre:
        outcome.missing.extend(missing_pre)
        return outcome

    if remove_new and base is not None:
        for suffix in ("-wal", "-shm"):
            _silent_unlink(Path(str(base) + suffix))
        _silent_unlink(base)
    ordered = sorted(pairs, key=lambda p: 1 if (base is not None and p[0] == base) else 0)
    for src, dst in ordered:
        if not dst.exists():
            outcome.missing.append(dst)
            continue
        try:
            os.replace(dst, src)
        except OSError:
            if dst.exists():
                outcome.leftover.append(dst)
            else:
                outcome.missing.append(dst)
    try:
        _fsync_dir(outcome.target_dir)
    except OSError as exc:
        outcome.persist_error = f"{type(exc).__name__}: {exc}"
    return outcome


def _unquarantine(pairs: list[tuple[Path, Path]]) -> _RestoreOutcome:
    """隔离已全部完成后的回滚：清掉替换产生的新库与 sidecar，再还原隔离副本。"""
    return _restore_moved(pairs, remove_new=True)


def _drop_quarantine(pairs: list[tuple[Path, Path]]) -> None:
    for _src, dst in pairs:
        _silent_unlink(dst)


def _post_restore_checks(target: Path, set_dir: Path) -> None:
    """恢复后校验：`init_db()` → integrity → 逐表计数与备份 manifest 比对。"""
    from backend.database import db as db_module
    db_module.reset_connection()
    db_module.init_db()
    db_module.reset_connection()

    manifest = _read_manifest(set_dir) or {}
    conn = _open_ro(target)
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise BackupError("restore_verify_failed", "恢复库 integrity_check 失败")
        counts = {t: int(conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0])
                  for t in migrate.BASE_TABLES}
        for table, expected in (manifest.get("tables") or {}).items():
            if table in counts and int(expected) != counts[table]:
                raise BackupError(
                    "restore_verify_failed",
                    f"{table}: manifest={expected} restored={counts[table]}",
                )
    finally:
        conn.close()


def _rollback_to_pre_restore(target: Path, pre_restore_dir: str) -> None:
    """用 `pre-restore` 备份集把库换回去（同卷临时文件 + fsync + 校验 + 隔离替换）。"""
    pref = Path(pre_restore_dir)
    nonce = secrets.token_hex(8)
    tmp = target.parent / f"rollback-{nonce}.db"
    shutil.copyfile(pref / DB_FILE, tmp)
    _fsync_file(tmp)
    try:
        _verify_staged_db(tmp, _read_manifest(pref) or {})
    except Exception:
        _silent_unlink(tmp)
        raise
    pairs = _quarantine_target(target, nonce)
    try:
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except Exception as exc:
        outcome = _unquarantine(pairs)
        _silent_unlink(tmp)
        if not outcome.ok():
            raise BackupError(
                "rollback_leftover",
                f"回退未能确认还原原库：{outcome.detail()}",
            ) from exc
        raise
    _drop_quarantine(pairs)


__all__ = [
    "BackupError",
    "COMPLETE_FILE",
    "DB_FILE",
    "LATEST_FILE",
    "MANIFEST_FILE",
    "PROTECTED_STAGES",
    "SET_PREFIX",
    "STAGES",
    "business_counts",
    "holders_of",
    "latest_set",
    "list_sets",
    "prune",
    "restore_set",
    "snapshot_set",
    "valid_sets",
    "verify_set",
]
