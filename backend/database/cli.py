"""迁移 CLI：A 类入口 3（写命令受维护态封锁）。

用法（P-1.5 只接线维护态约束，不实现迁移状态机）::

    python -m backend.database.cli status      # 只读
    python -m backend.database.cli verify      # 只读
    python -m backend.database.cli snapshot    # 写：维护态下拒绝
    python -m backend.database.cli run --db <副本库>   # P-1.6：空迁移 dry-run
    python -m backend.database.cli rollback --set <备份集>  # P-1.6：受维护态/排空约束

``--allow-write`` 仅为兼容计划里的命令形态而存在，**不能**绕过维护态封锁。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from backend import config
from backend.database import backup, gate, migrate

READ_COMMANDS = ("status", "verify")
WRITE_COMMANDS = ("snapshot",)
MIGRATE_COMMANDS = ("run", "rollback")

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_HALTED = 4

_RUN_ARGS: argparse.Namespace | None = None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.database.cli",
                                     description="NodePoolME 迁移 CLI（维护态受约束）")
    parser.add_argument("command", choices=READ_COMMANDS + WRITE_COMMANDS + MIGRATE_COMMANDS)
    parser.add_argument("--db", help="run：dry-run 的副本数据库路径（必填，且不得等于主库）")
    parser.add_argument("--set", dest="set_dir", help="rollback：备份集目录（缺省用最新集）")
    parser.add_argument("--allow-write", action="store_true",
                        help="保留参数：不能绕过维护态封锁")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return parser


def _status() -> dict:
    state = gate.judge()[0]
    latest = backup.latest_set()
    return {
        "command": "status",
        "maintenance": state.name,
        "db_path": str(config.DB_PATH),
        "db_exists": config.DB_PATH.exists(),
        "state_dir": str(config.STATE_DIR),
        "latest_set": (latest or {}).get("name"),
        "migration_enabled": bool(config.MIGRATION_ENABLED),
        "migrate_state": migrate.migration_state_summary(),
    }


def _verify() -> dict:
    latest = backup.latest_set()
    if not latest:
        return {"command": "verify", "ok": False, "code": "no_backup_set"}
    report = backup.verify_set(latest["path"])
    return {"command": "verify", "set": latest["name"], "ok": bool(report.get("ok")),
            "detail": report}


def _snapshot() -> dict:
    info = backup.snapshot_set(stage="cli")
    return {"command": "snapshot", "ok": True, "set": info.get("name"), "detail": info}


def _run(dry_run: bool = True) -> tuple[dict, int]:
    """P-1.6：空迁移 dry-run（只允许副本库；开关未开即拒绝）。"""
    db = getattr(_RUN_ARGS, "db", None)
    try:
        result = migrate.run(db_path=db, dry_run=dry_run)
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


def _rollback() -> tuple[dict, int]:
    """P-1.6：从备份集回滚（维护态 + 排空约束由 migrate.rollback_to_snapshot 保证）。"""
    try:
        result = migrate.rollback_to_snapshot(getattr(_RUN_ARGS, "set_dir", None))
    except migrate.MigrationDisabled as exc:
        return {"command": "rollback", "ok": False, "code": "migration_disabled",
                "detail": str(exc)}, EXIT_REFUSED
    except migrate.MigrationRefused as exc:
        return {"command": "rollback", "ok": False, "code": exc.reason,
                "detail": str(exc)}, EXIT_REFUSED
    except migrate.AlreadyRunning as exc:
        return {"command": "rollback", "ok": False, "code": "already_running",
                "detail": str(exc)}, EXIT_REFUSED
    except migrate.FatalExit as exc:
        return {"command": "rollback", "ok": False, "code": exc.reason,
                "detail": str(exc), "halt_tail": "not_confirmed"}, exc.exit_code
    result.setdefault("command", "rollback")
    return result, (EXIT_OK if result.get("ok") else EXIT_HALTED)


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = args.command

    if command in MIGRATE_COMMANDS:
        global _RUN_ARGS
        _RUN_ARGS = args
        try:
            result, code = _run(True) if command == "run" else _rollback()
        except Exception as exc:          # noqa: BLE001 - CLI 边界统一转非零退出
            print(json.dumps({"command": command, "ok": False, "code": "error",
                              "detail": str(exc)}, ensure_ascii=False, default=str))
            return EXIT_REFUSED
        print(json.dumps(result, ensure_ascii=False, default=str))
        return code

    if command in WRITE_COMMANDS:
        state, reason = gate.judge()
        if reason is not None:
            # --allow-write 不得绕过维护态封锁
            print(json.dumps({"ok": False, "command": command, "reason": reason,
                              "maintenance": state.name,
                              "detail": "维护态下拒绝写命令（--allow-write 无效）"},
                             ensure_ascii=False))
            return EXIT_REFUSED
        try:
            with gate.write_guard():
                result = _snapshot()
        except Exception as exc:
            print(json.dumps({"ok": False, "command": command, "code": getattr(exc, "reason", "error"),
                              "detail": str(exc)}, ensure_ascii=False))
            return EXIT_REFUSED
        print(json.dumps(result, ensure_ascii=False, default=str))
        return EXIT_OK

    result = _status() if command == "status" else _verify()
    print(json.dumps(result, ensure_ascii=False, default=str))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
