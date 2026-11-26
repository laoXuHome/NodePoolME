"""运维 CLI（P-1 v1.10 简化版）。

用法::

    python -m backend.database.cli status                  # 只读：维护态/迁移摘要
    python -m backend.database.cli verify [--file X.db]    # 只读：校验备份文件
    python -m backend.database.cli backup [--out DIR]      # 手动导出独立备份（可在线执行）
    python -m backend.database.cli import --file X.db      # 手动导入（自动进维护态）
    python -m backend.database.cli run --db <副本库>       # 迁移 dry-run（只对副本）
    python -m backend.database.cli run --apply             # 停机后对主库执行迁移
    python -m backend.database.cli import --file X.db --recover --reason "..."  # 人工恢复导入

设计边界：没有 rollback / restore-set 子命令，也不自动寻找备份；导入必须显式给文件。
`run`/`import` 会先获取 ``<STATE_DIR>/migration.lock`` 进程生命周期锁；应用运行期间
CLI 无法取得该锁，必须先停应用（设计 v1.10 D2 选择 B）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from backend import config
from backend.database import (backup, bootstrap, db, gate, instance_lock, maint,
                              migrate, pool)

READ_COMMANDS = ("status", "verify", "backup")
WRITE_COMMANDS = ("import",)
MIGRATE_COMMANDS = ("run",)

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_HALTED = 4

_RUN_ARGS: argparse.Namespace | None = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backend.database.cli",
        description="NodePoolME 数据库运维 CLI：状态 / 备份 / 校验 / 导入 / 迁移演练")
    parser.add_argument("command",
                        choices=READ_COMMANDS + WRITE_COMMANDS + MIGRATE_COMMANDS)
    parser.add_argument("--db", help="run：dry-run 的副本库（必填，且不得等于主库）；"
                                     "import：目标库（缺省为主库）")
    parser.add_argument("--file", dest="file", help="import/verify：备份文件路径")
    parser.add_argument("--out", dest="out", help="backup：导出目录（缺省 BACKUP_DIR）")
    parser.add_argument("--label", default="manual", help="backup：文件名标签（默认 manual）")
    parser.add_argument("--apply", action="store_true",
                        help="run：对主库执行真实迁移（不带此参数为副本 dry-run）")
    parser.add_argument("--recover", action="store_true",
                        help="import：从 RECOVERY_REQUIRED 发起人工恢复导入")
    parser.add_argument("--reason", default="manual_migration", help="维护记录原因")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def _status() -> dict[str, Any]:
    state = gate.judge()[0]
    latest = backup.latest_backup()
    return {
        "command": "status",
        "maintenance": state.name,
        "db_path": str(config.DB_PATH),
        "db_exists": config.DB_PATH.exists(),
        "state_dir": str(config.STATE_DIR),
        "latest_backup": str(latest) if latest else None,
        "migration_enabled": bool(config.MIGRATION_ENABLED),
        "writes_blocked": bool(pool.writes_blocked()),
        "migrate_state": migrate.migration_state_summary(),
    }


def _verify(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    target = Path(args.file) if args.file else backup.latest_backup()
    if target is None:
        return {"command": "verify", "ok": False, "code": "no_backup_file"}, EXIT_REFUSED
    report = backup.verify_backup(target)
    report["command"] = "verify"
    report["file"] = target.name
    return report, (EXIT_OK if report.get("ok") else EXIT_REFUSED)


def _backup(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """在线导出：只读源库，不参与写入闸；维护态下也可导出（用于停机前留档）。"""
    try:
        info = backup.export_backup(args.out or None, label=args.label)
    except backup.BackupError as exc:
        return {"command": "backup", "ok": False, "code": exc.reason,
                "detail": str(exc)}, EXIT_REFUSED
    info["command"] = "backup"
    return info, EXIT_OK


def _migration_preflight() -> tuple[dict[str, Any] | None, int]:
    """Read-only gate before the CLI writes a PREPARED maintenance record."""
    if not config.MIGRATION_ENABLED:
        return ({"command": "run", "ok": False, "code": "migration_disabled",
                 "detail": "NODEPOOL_MIGRATION_ENABLED=0，迁移入口关闭"},
                EXIT_REFUSED)
    read = maint.read_maint_file()
    try:
        dbstate = bootstrap.probe_db()
    except bootstrap.StartupProbeError as exc:
        return ({"command": "run", "ok": False, "code": "probe_unverifiable",
                 "detail": str(exc)}, EXIT_HALTED)
    decision = bootstrap.classify(read, dbstate)
    if not decision.inactive:
        return ({"command": "run", "ok": False, "code": decision.code,
                 "detail": "当前数据库/维护文件不允许启动迁移",
                 "state": decision.state.name, "db_kind": decision.db_kind},
                EXIT_REFUSED)
    if dbstate.kind != "baseline_only":
        return ({"command": "run", "ok": False, "code": "migration_target_not_baseline",
                 "detail": "run --apply 只允许对‘仅基线’数据库执行",
                 "db_kind": dbstate.kind}, EXIT_REFUSED)
    return None, EXIT_OK


def _run(dry_run: bool = True, *,
         lock_token: instance_lock.InstanceLock | None = None) -> tuple[dict[str, Any], int]:
    """Run dry-run on a copy, or a real apply with PREPARED + blocked + token."""
    db_path = getattr(_RUN_ARGS, "db", None)
    if dry_run:
        try:
            result = migrate.run(db_path=db_path, dry_run=True, lock_token=lock_token)
        except migrate.MigrationDisabled as exc:
            return {"command": "run", "ok": False, "code": "migration_disabled",
                    "detail": str(exc)}, EXIT_REFUSED
        except migrate.MigrationRefused as exc:
            return {"command": "run", "ok": False, "code": exc.reason,
                    "detail": str(exc)}, EXIT_REFUSED
        except migrate.AlreadyRunning as exc:
            return {"command": "run", "ok": False, "code": "already_running",
                    "detail": str(exc)}, EXIT_REFUSED
        except migrate.FatalExit as exc:
            return {"command": "run", "ok": False, "code": exc.reason,
                    "detail": str(exc), "halt_tail": "not_confirmed"}, exc.exit_code
        result.setdefault("command", "run")
        return result, (EXIT_OK if result.get("ok") else EXIT_HALTED)

    # Real apply: the CLI acquired the process-lifetime lock in main().
    if db_path is not None:
        return {"command": "run", "ok": False, "code": "apply_uses_main_db",
                "detail": "--apply 不允许指定副本库"}, EXIT_REFUSED
    refusal, code = _migration_preflight()
    if refusal is not None:
        return refusal, code

    rec = migrate.MigrationRecord(
        migration_id=f"mig-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}")
    rec.reason = getattr(_RUN_ARGS, "reason", None) or "manual_migration"
    result: dict[str, Any] = {"command": "run", "dry_run": False,
                              "migration_id": rec.migration_id}
    # The PREPARED record and the write gate precede any migration transaction.
    pool.block_writes(pool.REASON_MAINTENANCE)
    try:
        maint.set_maintenance(rec, reason=rec.reason)
    except Exception as exc:                                      # noqa: BLE001
        return {**result, "ok": False, "code": "maintenance_write_failed",
                "detail": str(exc), "writes_blocked": bool(pool.writes_blocked())}, EXIT_HALTED
    result["prepared"] = True
    gate.apply_state(maint.MaintState.ACTIVE)
    ok, detail = pool.confirm_writes_blocked()
    result["block_confirm"] = detail
    if not ok:
        return {**result, "ok": False, "code": "writes_block_unconfirmed",
                "maintenance_kept": True}, EXIT_HALTED

    try:
        outcome = migrate.run(dry_run=False, lock_token=lock_token, record=rec)
    except migrate.MigrationDisabled as exc:
        return {**result, "ok": False, "code": "migration_disabled",
                "detail": str(exc), "maintenance_kept": True}, EXIT_REFUSED
    except migrate.MigrationRefused as exc:
        return {**result, "ok": False, "code": exc.reason,
                "detail": str(exc), "maintenance_kept": True}, EXIT_REFUSED
    except migrate.AlreadyRunning as exc:
        return {**result, "ok": False, "code": "already_running",
                "detail": str(exc), "maintenance_kept": True}, EXIT_HALTED
    except migrate.FatalExit as exc:
        return {**result, "ok": False, "code": exc.reason, "detail": str(exc),
                "halt_tail": "not_confirmed", "maintenance_kept": True}, exc.exit_code
    except Exception as exc:                                      # noqa: BLE001
        return {**result, "ok": False, "code": "migration_error",
                "detail": f"{type(exc).__name__}: {exc}",
                "maintenance_kept": True}, EXIT_HALTED
    result.update(outcome)
    result["command"] = "run"
    if outcome.get("ok"):
        release = _clear_maintenance_and_release()
        result["release"] = release
        if release.get("code") != "ok":
            result.update({"ok": False, "code": "maintenance_not_clear",
                           "maintenance_kept": True})
            return result, EXIT_HALTED
    else:
        result["maintenance_kept"] = True
    return result, (EXIT_OK if result.get("ok") else EXIT_HALTED)


def _clear_maintenance_and_release(db_path: Path | None = None) -> dict[str, Any]:
    """成功收尾：删维护文件 → 目录 fsync → 用真实 DB 复核 → 解除写封锁。"""
    path = Path(maint.MAINT_FILE)
    detail: dict[str, Any] = {}
    if path.exists():
        path.unlink()
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    read = maint.read_maint_file()
    try:
        dbstate = bootstrap.probe_db(db_path)
    except bootstrap.StartupProbeError as exc:
        detail.update({"code": "maintenance_not_clear", "detail": str(exc),
                       "maintenance_after": "RECOVERY_UNVERIFIED"})
        return detail
    decision = bootstrap.classify(read, dbstate)
    detail["maintenance_after"] = decision.state.name
    detail["code_after"] = decision.code
    detail["db_kind"] = decision.db_kind
    if not decision.inactive:
        # 删文件不足以代表不处于维护态（DB 仍有 running/failed 或 state 不可信）⇒ 不解封
        detail["code"] = "maintenance_not_clear"
        return detail
    gate.release()
    detail["writes_blocked_after"] = bool(pool.writes_blocked())
    if detail["writes_blocked_after"]:
        detail["code"] = "maintenance_not_clear"
        return detail
    detail["code"] = "ok"
    return detail


def _import(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """手动导入：校验 → 维护态 → 停写/排空/关连接 → 文件级安全替换 → 解除维护。"""
    if not args.file:
        return {"command": "import", "ok": False, "code": "file_required",
                "detail": "必须用 --file 指定备份文件"}, EXIT_REFUSED
    source = Path(args.file)
    target = Path(args.db) if args.db else config.DB_PATH

    # 0) 维护态先行：已在维护中（可能有迁移未收尾）绝不写第二个维护记录
    state, _reason = gate.judge()
    if state is maint.MaintState.ACTIVE:
        return {"command": "import", "ok": False, "code": "maintenance_not_inactive",
                "detail": "当前维护态 ACTIVE；导入不能从进行中的维护态发起"}, EXIT_REFUSED
    if state is maint.MaintState.RECOVERY_REQUIRED and not getattr(args, "recover", False):
        return {"command": "import", "ok": False, "code": "recovery_requires_flag",
                "detail": "当前为 RECOVERY_REQUIRED；人工恢复导入必须显式加 --recover"}, EXIT_REFUSED

    # 1) 再校验源：无效就拒绝，绝不进入维护态、绝不动目标库
    report = backup.verify_backup(source)
    if not report.get("ok"):
        return {"command": "import", "ok": False, "code": "backup_invalid",
                "detail": report.get("errors"), "file": source.name}, EXIT_REFUSED

    rec = migrate.MigrationRecord(migration_id=f"import-{time.strftime('%Y%m%d-%H%M%S')}")
    rec.reason = getattr(args, "reason", None) or "manual_import"
    steps: list[str] = ["source_verified"]
    result: dict[str, Any] = {"command": "import", "file": source.name,
                              "target": str(target), "steps": steps}
    try:
        maint.set_maintenance(rec, reason=rec.reason)
    except Exception as exc:                                      # noqa: BLE001
        return {**result, "ok": False, "code": "maintenance_write_failed",
                "detail": str(exc)}, EXIT_REFUSED
    steps.append("maintenance")

    gate.apply_state(maint.MaintState.ACTIVE)
    pool.block_writes("手动导入备份")
    ok, detail = pool.confirm_writes_blocked()
    result["block_confirm"] = detail
    if not ok:
        return {**result, "ok": False, "code": "writes_block_unconfirmed"}, EXIT_HALTED
    steps.append("stop_writes")

    drained = gate.drain(rounds=2, min_interval=10.0)
    result["drain"] = {"ok": bool(drained.ok), "detail": drained.detail,
                       "rounds": len(drained.rounds), "interval": drained.interval}
    if not drained.continue_allowed:
        return {**result, "ok": False, "code": "drain_failed",
                "detail": "仍有写入活动，导入中止；维护态与写封锁保持"}, EXIT_HALTED
    steps.append("drain")

    close_report = db.close_all_connections_report()
    result["connections"] = close_report
    if not close_report["ok"]:
        # 任一连接关闭失败 ⇒ 不得替换文件；维护态与写封锁保持
        result.update({"ok": False, "code": "connections_not_closed",
                       "detail": {"failed": close_report["failed"],
                                  "remaining": close_report["remaining"]},
                       "maintenance_kept": True,
                       "writes_blocked": bool(pool.writes_blocked())})
        steps.append("close_connections_failed")
        return result, EXIT_HALTED
    steps.append("close_connections")

    try:
        info = backup.import_backup(source, db_path=target)
    except backup.BackupError as exc:
        result["code"] = exc.reason
        result["detail"] = str(exc)
        result["ok"] = False
        steps.append("import_failed")
        result["maintenance_kept"] = True
        result["writes_blocked"] = bool(pool.writes_blocked())
        return result, EXIT_HALTED
    result.update({"ok": True, "code": "imported", "preimport_copy": info.get("preimport_copy"),
                   "preimport_tables": info.get("preimport_tables"),
                   "tables": info.get("tables"),
                   "structure_profile": info.get("structure_profile")})
    steps.append("import_atomic")

    # 导入后复核一次（独立只读连接，不与业务连接复用）
    after = backup.inspect_db(target)
    result["post_check"] = {"ok": after.get("ok"), "errors": after.get("errors")}
    if not after.get("ok"):
        result["ok"] = False
        result["code"] = "post_check_failed"
        result["maintenance_kept"] = True
        return result, EXIT_HALTED
    steps.append("verify_imported")

    release = _clear_maintenance_and_release(db_path=target)
    result["release"] = release
    if release.get("code") != "ok":
        result["ok"] = False
        result["code"] = "maintenance_not_clear"
        result["maintenance_kept"] = True
        return result, EXIT_HALTED
    steps.append("release")
    result["ok"] = True
    result["code"] = "imported"
    return result, EXIT_OK


def _dispatch(args: argparse.Namespace, lock_token: instance_lock.InstanceLock | None) -> tuple[dict[str, Any], int]:
    global _RUN_ARGS
    command = args.command
    if command == "run":
        _RUN_ARGS = args
        return _run(not bool(getattr(args, "apply", False)), lock_token=lock_token)
    if command == "backup":
        return _backup(args)
    if command == "verify":
        return _verify(args)
    if command == "import":
        return _import(args)
    return _status(), EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = args.command

    if command in ("run", "import"):
        try:
            with instance_lock.acquire() as lock:
                result, code = _dispatch(args, lock)
        except instance_lock.AlreadyRunning as exc:
            result, code = {"command": command, "ok": False, "code": "already_running",
                            "detail": str(exc)}, EXIT_REFUSED
        except instance_lock.LockUnsupported as exc:
            result, code = {"command": command, "ok": False, "code": "lock_unsupported",
                            "detail": str(exc)}, EXIT_REFUSED
        except Exception as exc:          # noqa: BLE001 - CLI 边界统一转非零退出
            result, code = {"command": command, "ok": False, "code": "error",
                            "detail": str(exc)}, EXIT_REFUSED
    else:
        try:
            result, code = _dispatch(args, None)
        except Exception as exc:          # noqa: BLE001
            result, code = {"command": command, "ok": False, "code": "error",
                            "detail": str(exc)}, EXIT_REFUSED
    print(json.dumps(result, ensure_ascii=False, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
