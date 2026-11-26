"""数据库备份与导入（v1.10 简化版）：手动导出单个 .db + 手动导入 + 完整性校验。

设计边界（本版**不**做的事）：

* 不做自动恢复链：没有 `pre-restore` 多级回退、没有隔离副本编排、没有恢复子进程；
* 不做滚动清理：备份文件由用户保管，程序不自动删除；
* 不自动寻找备份：导入必须由用户显式给出文件路径。

保留的安全能力（不可省）：

1. 导出用 SQLite 在线备份 API。**禁止** `cp`/`copy2` 正在使用的库 —— WAL 下
   文件拷贝会得到缺表丢行的副本。
2. 导出后立刻在独立只读连接上跑 `integrity_check` + 表集合 + 结构摘要；不过就不落名。
3. 导入前从**同一份文件**再校验一次；不过直接拒绝，目标库一动不动。
4. 导入只允许在"维护态 + 写封锁已确认 + 全部连接已关闭 + 无第三方句柄"下进行；
   替换前处理 `-wal`/`-shm`，同卷临时文件 + fsync + `os.replace` + 目录 fsync，
   替换后复核行数与结构。
5. 导入失败保留现场：原库以单个 `*.preimport-<stamp>.db` 副本留存；能复原就复原，
   复原失败只报告 leftover 路径，不做多级自动回退。
"""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from backend import config
from backend.database import migrate

PREFIX = "nodepool-"
BACKUP_EXT = ".db"
META_EXT = ".json"
TMP_PREFIX = ".tmp-"

KEY_TABLES = ("airports", "nodes", "settings", "test_groups")

PROBE_TIMEOUT = 30


class BackupError(RuntimeError):
    """备份/导入错误的基类，`reason` 为稳定机器码。"""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {message}" if message else reason)


# --- 小工具 ------------------------------------------------------------------


def _now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


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


def _sqlite_uri(path: Path, *, ro: bool = True, immutable: bool = False) -> str:
    uri = f"file:{path}?mode={'ro' if ro else 'rw'}"
    if immutable:
        uri += "&immutable=1"
    return uri


def _open_ro(path: Path, *, immutable: bool = False) -> sqlite3.Connection:
    """只读打开。`immutable=True` 用于冻结的备份文件：不创建 `-wal/-shm`。"""
    conn = sqlite3.connect(_sqlite_uri(path, immutable=immutable), uri=True,
                           timeout=PROBE_TIMEOUT)
    conn.row_factory = sqlite3.Row
    return conn


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        " ORDER BY name")]


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {name: int(conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0])
            for name in _table_names(conn)}


def _digest_rows(conn: sqlite3.Connection, table: str) -> dict[str, Any]:
    """键表行摘要：行数 + 整行排序后的 sha256。"""
    if table not in set(_table_names(conn)):
        return {"rows": 0, "sha256": None}
    rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
    payload = "\n".join("|".join("" if v is None else str(v) for v in tuple(row))
                        for row in rows)
    return {"rows": len(rows),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()}


def _meta_path(db_file: Path) -> Path:
    return db_file.with_suffix(META_EXT)


def _read_meta(db_file: Path) -> dict[str, Any] | None:
    path = _meta_path(db_file)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                              # noqa: BLE001
        return None


def latest_backup(backup_dir: str | Path | None = None) -> Path | None:
    """目录内最新的导出文件（按修改时间）；没有返回 None。"""
    root = Path(backup_dir or config.BACKUP_DIR)
    if not root.exists():
        return None
    files = [p for p in root.glob(f"{PREFIX}*{BACKUP_EXT}")
             if p.is_file() and not p.name.startswith(".")]
    return max(files, key=lambda p: p.stat().st_mtime) if files else None


def holders_of(db_path: Path) -> list[str]:
    """扫描 `/proc/*/fd`，列出除自身之外仍持有该库（含 -wal/-shm）的进程。"""
    target = Path(db_path).name
    self_pid = os.getpid()
    held: list[str] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return held
    for entry in proc_root.iterdir():
        if not entry.name.isdigit() or int(entry.name) == self_pid:
            continue
        try:
            for fd in (entry / "fd").iterdir():
                try:
                    link = Path(os.readlink(fd)).name
                except OSError:
                    continue
                if link == target or link in (target + "-wal", target + "-shm"):
                    held.append(f"{entry.name}:{fd.name}")
                    break
        except (PermissionError, FileNotFoundError, OSError):
            continue
    return held


# --- 校验 --------------------------------------------------------------------


def _probe_file(path: Path, *, immutable: bool = True) -> dict[str, Any]:
    """在冻结文件上做 L1（完整性/表集合/结构）+ 逐表计数。"""
    errors: list[str] = []
    report: dict[str, Any] = {"file": str(path), "errors": errors}
    if not path.exists():
        errors.append("file_missing")
        return report
    if path.stat().st_size <= 0:
        errors.append("file_empty")
        return report
    try:
        conn = _open_ro(path, immutable=immutable)
        try:
            conn.execute("PRAGMA query_only=1")
            report["integrity_check"] = conn.execute("PRAGMA integrity_check").fetchone()[0]
            names = set(_table_names(conn))
            missing = [t for t in migrate.BASE_TABLES if t not in names]
            if missing:
                errors.append("missing_tables:" + ",".join(missing))
            report["tables"] = _counts(conn)
            report["structure_hash"] = migrate.structure_hash(conn)
            report["structure_profile"] = migrate.structure_profile_for(
                report["structure_hash"])
            if report["structure_profile"] is None:
                errors.append("structure_profile_unregistered")
            report["schema_version"] = (migrate.current_version(conn)
                                        if migrate.schema_table_exists(conn) else None)
            report["tasks"] = migrate.tasks_state(conn)
            report["key_digests"] = {t: _digest_rows(conn, t) for t in KEY_TABLES}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        errors.append(f"sqlite_error:{exc}")
        return report
    if report.get("integrity_check") != "ok":
        errors.append(f"integrity_check={report.get('integrity_check')}")
    report["ok"] = not errors
    return report


def verify_backup(path: str | Path) -> dict[str, Any]:
    """校验备份文件：完整性 + 表集合 + 结构摘要 + 元信息 sha（若有元信息）。"""
    db_file = Path(path)
    report = _probe_file(db_file)
    errors: list[str] = report["errors"]
    meta = _read_meta(db_file)
    report["meta"] = meta
    if meta and db_file.exists():
        if meta.get("db_bytes") and int(meta["db_bytes"]) != db_file.stat().st_size:
            errors.append("meta_size_mismatch")
        if meta.get("sha256") and _sha256(db_file) != meta["sha256"]:
            errors.append("meta_sha256_mismatch")
        if meta.get("structure_hash") and report.get("structure_hash") \
                and meta["structure_hash"] != report["structure_hash"]:
            errors.append("meta_structure_mismatch")
    report["sha256"] = _sha256(db_file) if db_file.exists() else None
    report["db_bytes"] = db_file.stat().st_size if db_file.exists() else 0
    report["ok"] = not errors
    return report


# --- 导出 --------------------------------------------------------------------


def _check_free_space(target_dir: Path, need_bytes: int) -> None:
    usage = shutil.disk_usage(target_dir)
    if usage.free < need_bytes:
        raise BackupError(
            "insufficient_space",
            f"可用 {usage.free // (1 << 20)} MB，需要约 {need_bytes // (1 << 20)} MB")


def export_backup(dest_dir: str | Path | None = None, *, db_path: str | Path | None = None,
                  label: str = "manual") -> dict[str, Any]:
    """手动导出独立 `.db` 备份（可在服务运行中执行）。"""
    src = Path(db_path or config.DB_PATH)
    if not src.exists():
        raise BackupError("source_missing", f"源库不存在：{src}")
    out = Path(dest_dir or config.BACKUP_DIR)
    out.mkdir(parents=True, exist_ok=True)
    label = "".join(ch for ch in (label or "manual") if ch.isalnum() or ch in "-_") or "manual"
    _check_free_space(out, src.stat().st_size * 2 + (8 << 20))

    tmp = out / f"{TMP_PREFIX}{secrets.token_hex(6)}.db"
    started = time.monotonic()
    try:
        source = _open_ro(src)
        dest = sqlite3.connect(str(tmp), timeout=PROBE_TIMEOUT)
        try:
            source.backup(dest, pages=1024)
            dest.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            dest.close()
            source.close()
        for suffix in ("-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
        _fsync_file(tmp)
        report = _probe_file(tmp)
        if not report["ok"]:
            raise BackupError("export_verify_failed", str(report["errors"]))
    except Exception:
        for suffix in ("", "-wal", "-shm"):
            Path(str(tmp) + suffix).unlink(missing_ok=True)
        raise

    final = out / f"{PREFIX}{_stamp()}-{label}{BACKUP_EXT}"
    os.replace(tmp, final)
    _fsync_dir(out)

    meta = {
        "format": 2,
        "created_at": _now(),
        "app_version": getattr(config, "VERSION", None),
        "sqlite_version": None,
        "label": label,
        "db_bytes": final.stat().st_size,
        "sha256": _sha256(final),
        "integrity_check": report["integrity_check"],
        "structure_hash": report["structure_hash"],
        "structure_profile": report["structure_profile"],
        "schema_version": report["schema_version"],
        "tasks": report["tasks"],
        "tables": report["tables"],
        "key_digests": report["key_digests"],
    }
    try:
        conn = _open_ro(final, immutable=True)
        try:
            meta["sqlite_version"] = conn.execute("SELECT sqlite_version()").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    _write_json(_meta_path(final), meta)
    _fsync_dir(out)
    return {
        "ok": True,
        "file": final.name,
        "path": str(final),
        "meta_path": str(_meta_path(final)),
        "size": meta["db_bytes"],
        "sha256": meta["sha256"],
        "integrity_check": meta["integrity_check"],
        "structure_hash": meta["structure_hash"],
        "structure_profile": meta["structure_profile"],
        "schema_version": meta["schema_version"],
        "tables": meta["tables"],
        "takes_ms": round((time.monotonic() - started) * 1000, 1),
    }


# --- 导入 --------------------------------------------------------------------


def _sidecar(target: Path, suffix: str, stamp: str) -> Path:
    return Path(str(target) + f".preimport-{stamp}{suffix}")


def _restore_scene(target: Path, stamp: str) -> str:
    """用 `.preimport-<stamp>.db` 还原目标库（副本保留，供事后取证）。"""
    backup_copy = _sidecar(target, "", stamp)
    if not backup_copy.exists():
        return "preimport_copy_missing"
    tmp = target.parent / f".revert-{secrets.token_hex(6)}.db"
    try:
        shutil.copyfile(backup_copy, tmp)
        _fsync_file(tmp)
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        return f"{type(exc).__name__}: {exc}"
    return ""


def import_backup(source: str | Path, *, db_path: str | Path | None = None,
                  before_replace: Callable[[Path], None] | None = None) -> dict[str, Any]:
    """把指定备份文件安全导入目标库。

    调用方必须先完成：写入封锁、排空、关闭全部业务连接。本函数只负责文件级安全。
    `before_replace(target)` 是测试注入点（断言替换前封锁仍生效）。
    成功后原库副本保留在 `*.preimport-<stamp>.db`，由人工确认后删除。
    """
    src = Path(source)
    target = Path(db_path or config.DB_PATH)
    if not src.exists():
        raise BackupError("backup_missing", f"备份文件不存在：{src}")
    verdict = verify_backup(src)
    if not verdict["ok"]:
        raise BackupError("backup_invalid", str(verdict["errors"]))

    held = holders_of(target)
    if held:
        raise BackupError("import_handles_present", f"仍有进程持有库文件：{held[:5]}")

    target.parent.mkdir(parents=True, exist_ok=True)
    stamp = _stamp()
    preimport: Path | None = None
    if target.exists():
        preimport = _sidecar(target, "", stamp)
        shutil.copyfile(target, preimport)
        _fsync_file(preimport)

    moved: list[tuple[Path, Path]] = []
    for suffix in ("-wal", "-shm"):
        side = Path(str(target) + suffix)
        if side.exists():
            dst = _sidecar(target, suffix, stamp)
            os.replace(side, dst)
            moved.append((side, dst))

    tmp = target.parent / f".import-{secrets.token_hex(6)}.db"
    try:
        shutil.copyfile(src, tmp)
        _fsync_file(tmp)
        staged = _probe_file(tmp, immutable=True)
        if not staged["ok"]:
            raise BackupError("import_stage_failed", str(staged["errors"]))
        if before_replace is not None:
            before_replace(target)
        os.replace(tmp, target)
        _fsync_dir(target.parent)
    except Exception as exc:                                       # noqa: BLE001
        Path(str(tmp)).unlink(missing_ok=True)
        for side, dst in moved:
            try:
                os.replace(dst, side)
            except OSError:
                pass
        if preimport is not None:
            restored = _restore_scene(target, stamp)
            if restored:
                raise BackupError(
                    "import_failed_leftover",
                    f"导入失败（{exc}）；原库未复原（{restored}），留存：{preimport}") from exc
            raise BackupError("import_failed", f"导入失败，原库已复原：{exc}") from exc
        raise BackupError("import_failed", f"导入失败（目标库原本不存在）：{exc}") from exc

    try:
        after = _probe_file(target, immutable=False)
        src_report = _probe_file(src, immutable=True)
        problems = list(after["errors"])
        mismatched = [t for t, n in (src_report.get("tables") or {}).items()
                      if after.get("tables", {}).get(t) != n]
        if mismatched:
            problems.append("count_mismatch:" + ",".join(mismatched))
        if after.get("structure_hash") != src_report.get("structure_hash"):
            problems.append("structure_mismatch")
        if problems:
            raise BackupError("import_verify_failed", str(problems))
    except Exception as exc:                                       # noqa: BLE001
        for _side, dst in moved:
            Path(str(dst)).unlink(missing_ok=True)
        restored = _restore_scene(target, stamp) if preimport is not None else "target_missing"
        if restored:
            raise BackupError(
                "import_failed_leftover",
                f"导入后校验失败（{exc}）；原库未复原（{restored}），留存：{preimport}") from exc
        raise BackupError("import_rolled_back",
                          f"导入后校验失败（{exc}）；原库已复原") from exc

    for _side, dst in moved:
        Path(str(dst)).unlink(missing_ok=True)
    for suffix in ("-wal", "-shm"):
        side = Path(str(target) + suffix)
        if side.exists() and side.stat().st_size == 0:
            side.unlink(missing_ok=True)
    return {
        "ok": True,
        "source": str(src),
        "target": str(target),
        "preimport_copy": str(preimport) if preimport and preimport.exists() else None,
        "tables": after.get("tables"),
        "structure_hash": after.get("structure_hash"),
        "structure_profile": after.get("structure_profile"),
        "meta": _read_meta(src),
    }


def drop_preimport_copy(path: str | Path) -> bool:
    """人工确认后删除导入前的原库副本（成功后不自动删）。"""
    target = Path(path)
    if not target.exists():
        return False
    target.unlink()
    _fsync_dir(target.parent)
    return True


__all__ = [
    "BACKUP_EXT",
    "BackupError",
    "KEY_TABLES",
    "PREFIX",
    "drop_preimport_copy",
    "export_backup",
    "holders_of",
    "import_backup",
    "latest_backup",
    "verify_backup",
]
