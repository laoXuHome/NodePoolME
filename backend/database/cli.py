"""迁移 CLI：A 类入口 3（写命令受维护态封锁）。

用法（P-1.5 只接线维护态约束，不实现迁移状态机）::

    python -m backend.database.cli status      # 只读
    python -m backend.database.cli verify      # 只读
    python -m backend.database.cli snapshot    # 写：维护态下拒绝
    python -m backend.database.cli run         # P-1.6 未实现
    python -m backend.database.cli rollback    # P-1.6 未实现

``--allow-write`` 仅为兼容计划里的命令形态而存在，**不能**绕过维护态封锁。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from backend import config
from backend.database import backup, gate

READ_COMMANDS = ("status", "verify")
WRITE_COMMANDS = ("snapshot",)
PENDING_COMMANDS = ("run", "rollback")   # P-1.6 才实现

EXIT_OK = 0
EXIT_REFUSED = 2
EXIT_PENDING = 3


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="backend.database.cli",
                                     description="NodePoolME 迁移 CLI（维护态受约束）")
    parser.add_argument("command", choices=READ_COMMANDS + WRITE_COMMANDS + PENDING_COMMANDS)
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    command = args.command

    if command in PENDING_COMMANDS:
        print(json.dumps({"command": command, "ok": False, "code": "not_implemented",
                          "stage": "P-1.6"}, ensure_ascii=False))
        return EXIT_PENDING

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
