"""运维 CLI（P-1 v1.10 简化版）。

用法::

    python -m backend.database.cli status                  # 只读：维护态/迁移摘要
    python -m backend.database.cli verify [--file X.db]    # 只读：校验备份文件
    python -m backend.database.cli backup [--out DIR]      # 手动导出独立备份（可在线执行）
    python -m backend.database.cli import --file X.db      # 手动导入（自动进维护态）
    python -m backend.database.cli run --db <副本库>       # P-1.6 空迁移 dry-run

设计边界：没有 rollback / restore-set 子命令，也不自动寻找备份；导入必须显式给文件。
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
from backend.database import backup, db, gate, maint, migrate, pool

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


def _run(dry_run: bool = True) -> tuple[dict[str, Any], int]:
    """P-1.6：空迁移 dry-run（只允许副本库；开关未开即拒绝）。"""
    db_path = getattr(_RUN_ARGS, "db", None)
    try:
        result = migrate.run(db_path=db_path, dry_run=dry_run)
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


def _clear_maintenance_and_release() -> dict[str, Any]:
    """导入成功后解除维护态：删 maintenance 文件 → 目录 fsync → 复核 → 解除写封锁。"""
    path = Path(maint.MAINT_FILE)
    detail: dict[str, Any] = {}
    if path.exists():
        path.unlink()
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    state = maint.maintenance_state()
    detail["maintenance_after"] = state.name
    if state is not maint.MaintState.INACTIVE:
        # 删文件不足以代表不处于维护态（比如库内仍有非 success 迁移行）⇒ 不解封
        detail["code"] = "maintenance_not_clear"
        return detail
    gate.release()
    detail["writes_blocked_after"] = bool(pool.writes_blocked())
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
    if state is not maint.MaintState.INACTIVE:
        return {"command": "import", "ok": False, "code": "maintenance_not_inactive",
                "detail": f"当前维护态 {state.name}；导入只允许从 INACTIVE 发起"}, EXIT_REFUSED

    # 1) 再校验源：无效就拒绝，绝不进入维护态、绝不动目标库
    report = backup.verify_backup(source)
    if not report.get("ok"):
        return {"command": "import", "ok": False, "code": "backup_invalid",
                "detail": report.get("errors"), "file": source.name}, EXIT_REFUSED

    rec = migrate.MigrationRecord(migration_id=f"import-{time.strftime('%Y%m%d-%H%M%S')}")
    steps: list[str] = ["source_verified"]
    result: dict[str, Any] = {"command": "import", "file": source.name,
                              "target": str(target), "steps": steps}
    try:
        maint.set_maintenance(rec, reason="manual_import")
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

    release = _clear_maintenance_and_release()
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = args.command

    if command == "run":
        global _RUN_ARGS
        _RUN_ARGS = args
        try:
            result, code = _run(True)
        except Exception as exc:          # noqa: BLE001 - CLI 边界统一转非零退出
            print(json.dumps({"command": command, "ok": False, "code": "error",
                              "detail": str(exc)}, ensure_ascii=False, default=str))
            return EXIT_REFUSED
        print(json.dumps(result, ensure_ascii=False, default=str))
        return code

    if command == "backup":
        result, code = _backup(args)
    elif command == "verify":
        result, code = _verify(args)
    elif command == "import":
        result, code = _import(args)
    else:
        result, code = _status(), EXIT_OK
    print(json.dumps(result, ensure_ascii=False, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
