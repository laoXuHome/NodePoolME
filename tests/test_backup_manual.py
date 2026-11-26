"""手动备份/导入简化版测试（v1.10 P-1.7 修订）。

覆盖：正常导出、运行期导出一致性、完整性校验、正常导入、无效备份拒绝、
导入期写封锁、导入失败维护态与现场、WAL/SHM 处理、迁移不再自动备份。

全部在临时目录 + 副本库上执行；不接触 NAS/生产库/容器，不执行 P-7。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import config
from backend.database import backup, cli, db, gate, maint, migrate, pool


def _tmp_root() -> Path:
    root = Path.home() / ".p1btest"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=str(root)))


def _set_env(monkeypatch, root: Path) -> SimpleNamespace:
    state = root / "state"
    state.mkdir()
    data = root / "data"
    data.mkdir()
    backups = root / "backup"
    backups.mkdir()
    live = data / "nodepool.db"
    monkeypatch.setattr(config, "DB_PATH", live)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SUB_DIR", data / "sub")
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "BACKUP_DIR", backups)
    monkeypatch.setattr(config, "MIGRATION_ENABLED", True)
    monkeypatch.setattr(config, "MIGRATION_FAULT_LOG", state / "migration-fault.log")
    monkeypatch.setattr(maint, "STATE_DIR", state)
    monkeypatch.setattr(maint, "MAINT_FILE", state / maint.MAINT_FILENAME)
    maint.reset_state_dir_selfcheck()
    migrate.reset_issuer_token_for_tests()
    db.reset_connection()
    pool.reset_for_tests()
    return SimpleNamespace(root=root, state=state, data=data, live=live,
                           backups=backups, fault_log=state / "migration-fault.log")


@pytest.fixture
def env(monkeypatch):
    ns = _set_env(monkeypatch, _tmp_root())
    db.init_db()
    _add_airports(2, prefix="seed")
    db.reset_connection()
    yield ns
    pool.reset_for_tests()
    db.reset_connection()


def _add_airports(n: int, prefix: str = "row") -> int:
    stamp = db.now()
    before = db.row("SELECT COUNT(*) AS n FROM airports")["n"]
    for i in range(n):
        db.execute(
            "INSERT INTO airports (name, url, enabled, interval_minutes, daily_time,"
            " filter_keywords, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (f"{prefix}-{i}-{time.time_ns()}", "https://x.example/sub", 1, 60, None,
             "[]", stamp, stamp))
    return before + n


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {name: conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
                for (name,) in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name NOT LIKE 'sqlite_%'")}
    finally:
        conn.close()


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fast_gate(monkeypatch) -> None:
    """把 10s 排空换成即时判"通过"（判据不放宽：仍要求 ok/continue_allowed）。"""
    def fake_drain(*_a, **_k):
        return SimpleNamespace(ok=True, continue_allowed=True, detail={"stubbed": True},
                               rounds=[], interval=10.0)
    monkeypatch.setattr(gate, "drain", fake_drain)


def _activate(migration_id: str = "m-keep") -> None:
    maint.set_maintenance(
        migrate.MigrationRecord(migration_id=migration_id, phase=migrate.Phase.PREPARED),
        reason="test")


# --- 导出 --------------------------------------------------------------------


def test_export_single_file_with_meta(env):
    info = backup.export_backup(label="manual")
    path = Path(info["path"])
    assert path.is_file() and path.suffix == ".db"
    assert path.parent == env.backups
    assert not any(p.is_dir() for p in env.backups.iterdir())
    assert info["integrity_check"] == "ok"
    meta = json.loads(Path(info["meta_path"]).read_text(encoding="utf-8"))
    assert meta["sha256"] == info["sha256"] == _sha(path)
    assert meta["db_bytes"] == path.stat().st_size
    assert meta["structure_hash"] and meta["structure_profile"]
    assert meta["tables"]["airports"] == 2
    assert "created_at" in meta and "schema_version" in meta
    # 备份文件独立存放，不覆盖生产库
    assert path != env.live and _sha(env.live) != _sha(path)
    assert backup.verify_backup(path)["ok"] is True


def test_export_is_consistent_while_writing(env):
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        conn = sqlite3.connect(str(env.live), timeout=30)
        try:
            stamp = db.now()
            i = 0
            while not stop.is_set():
                conn.execute(
                    "INSERT INTO airports (name, url, enabled, interval_minutes,"
                    " daily_time, filter_keywords, created_at, updated_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (f"live-{i}", "https://live.example/sub", 1, 30, None, "[]",
                     stamp, stamp))
                conn.commit()
                i += 1
        except BaseException as exc:                            # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.15)
    info = backup.export_backup(label="online")
    stop.set()
    thread.join(timeout=5)
    assert not errors
    path = Path(info["path"])
    report = backup.verify_backup(path)
    assert report["ok"] is True
    # 在线备份 = 某个已提交时刻的一致视图：行数介于起始与结束之间，且无半条记录
    assert 2 <= report["tables"]["airports"] <= _counts(env.live)["airports"]
    assert report["integrity_check"] == "ok"


def test_verify_rejects_corrupt_truncated_and_missing_tables(env):
    info = backup.export_backup(label="bad")
    src = Path(info["path"])

    truncated = env.root / "truncated.db"
    truncated.write_bytes(src.read_bytes()[: src.stat().st_size // 2])
    assert backup.verify_backup(truncated)["ok"] is False

    corrupt = env.root / "corrupt.db"
    payload = bytearray(src.read_bytes())
    payload[:64] = b"\x00" * 64                       # 头部损坏 ⇒ 不是有效 SQLite 文件
    corrupt.write_bytes(bytes(payload))
    assert backup.verify_backup(corrupt)["ok"] is False

    no_table = env.root / "no_table.db"
    conn = sqlite3.connect(str(no_table))
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    report = backup.verify_backup(no_table)
    assert report["ok"] is False
    assert any(e.startswith("missing_tables:") for e in report["errors"])


def test_verify_rejects_meta_sha_mismatch(env):
    info = backup.export_backup(label="meta")
    path = Path(info["path"])
    meta_path = Path(info["meta_path"])
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["sha256"] = "0" * 64
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    report = backup.verify_backup(path)
    assert report["ok"] is False
    assert "meta_sha256_mismatch" in report["errors"]


# --- 导入 --------------------------------------------------------------------


def test_import_restores_backup_and_releases_maintenance(env, monkeypatch):
    _fast_gate(monkeypatch)
    info = backup.export_backup(label="restore")
    before = _counts(env.live)
    _add_airports(3, prefix="after-export")
    assert _counts(env.live)["airports"] > before["airports"]

    rc = cli.main(["import", "--file", info["path"], "--json"])
    assert rc == cli.EXIT_OK
    assert _counts(env.live)["airports"] == before["airports"]
    assert not Path(maint.MAINT_FILE).exists()
    assert pool.writes_blocked() is False

    pool.reset_for_tests()
    db.reset_connection()
    _add_airports(1, prefix="post-import")     # 导入后服务可继续写
    assert _counts(env.live)["airports"] == before["airports"] + 1
    assert rc == 0


def test_import_invalid_file_refused_without_maintenance(env, monkeypatch):
    _fast_gate(monkeypatch)
    bad = env.root / "bad.db"
    bad.write_bytes(b"not a database")
    digest = _sha(env.live)
    rc = cli.main(["import", "--file", str(bad), "--json"])
    assert rc == cli.EXIT_REFUSED
    assert not Path(maint.MAINT_FILE).exists()
    assert _sha(env.live) == digest
    assert pool.writes_blocked() is False


def test_import_refused_when_maintenance_not_inactive(env, monkeypatch):
    _fast_gate(monkeypatch)
    info = backup.export_backup(label="blocked")
    _activate("m-in-progress")
    rc = cli.main(["import", "--file", info["path"], "--json"])
    assert rc == cli.EXIT_REFUSED
    assert Path(maint.MAINT_FILE).exists()


def test_import_blocks_writes_before_replace(env):
    """替换前写封锁必须仍在，且业务句柄拿不到写槽。"""
    info = backup.export_backup(label="lock")
    db.close_all_connections()
    pool.block_writes("test-import")
    seen: dict[str, object] = {}

    def hook(_target: Path) -> None:
        seen["blocked"] = pool.writes_blocked()
        with pytest.raises(pool.PoolClosed):
            with pool.write_slot():
                pass

    try:
        backup.import_backup(info["path"], before_replace=hook)
    finally:
        pool.unblock_writes()
    assert seen["blocked"] is True


def test_import_handles_stale_wal_and_shm(env):
    info = backup.export_backup(label="wal")
    backup_rows = _counts(env.live)
    _add_airports(2, prefix="stale-")
    db.reset_connection()
    # 伪造"上次异常退出遗留"的 -wal/-shm（真实内容不可解析也必须被移走）
    Path(str(env.live) + "-wal").write_bytes(b"stale-wal-bytes")
    Path(str(env.live) + "-shm").write_bytes(b"stale-shm-bytes")

    db.close_all_connections()
    pool.block_writes("test-wal")
    try:
        result = backup.import_backup(info["path"])
    finally:
        pool.unblock_writes()
    assert result["ok"] is True
    wal = Path(str(env.live) + "-wal")
    assert (not wal.exists()) or wal.stat().st_size == 0, "不得残留旧 WAL 帧"
    assert _counts(env.live) == backup_rows
    assert Path(result["preimport_copy"]).exists()          # 原库副本留存供人工确认


def test_import_failure_reports_scene_and_keeps_original(env):
    info = backup.export_backup(label="fail")
    _add_airports(2, prefix="keep-")
    db.reset_connection()
    db.close_all_connections()
    digest = _sha(env.live)
    pool.block_writes("test-fail")

    def boom(_target: Path) -> None:
        raise RuntimeError("simulated replace failure")

    try:
        with pytest.raises(backup.BackupError) as exc:
            backup.import_backup(info["path"], before_replace=boom)
    finally:
        pool.unblock_writes()
    assert exc.value.reason == "import_failed"
    assert _sha(env.live) == digest                        # 原库已复原
    leftovers = list(Path(env.live.parent).glob("nodepool.db.preimport-*"))
    assert leftovers and leftovers[0].stat().st_size > 0


def test_import_failure_keeps_maintenance_and_block(env, monkeypatch):
    _fast_gate(monkeypatch)
    info = backup.export_backup(label="halt")

    def broken(*_a, **_k):
        raise backup.BackupError("import_failed", "simulated")

    monkeypatch.setattr(backup, "import_backup", broken)
    digest = _sha(env.live)
    rc = cli.main(["import", "--file", info["path"], "--json"])
    assert rc == cli.EXIT_HALTED
    assert _sha(env.live) == digest
    assert Path(maint.MAINT_FILE).exists()
    assert pool.writes_blocked() is True


# --- 命令与迁移边界 -----------------------------------------------------------


def test_cli_backup_and_verify(env):
    out = env.root / "manual-out"
    rc = cli.main(["backup", "--out", str(out), "--label", "offline", "--json"])
    assert rc == cli.EXIT_OK
    files = sorted(out.glob("nodepool-*-offline.db"))
    assert len(files) == 1
    rc2 = cli.main(["verify", "--file", str(files[0]), "--json"])
    assert rc2 == cli.EXIT_OK
    rc3 = cli.main(["status", "--json"])
    assert rc3 == cli.EXIT_OK


def test_migration_does_not_auto_export(env, monkeypatch):
    """迁移与备份解耦：run 不再调用任何备份函数。"""
    calls = {"n": 0}
    monkeypatch.setattr(backup, "export_backup",
                        lambda *_a, **_k: calls.__setitem__("n", calls["n"] + 1))
    _activate("m-run")
    clock = [0.0]

    def fake_clock() -> float:
        clock[0] += 100.0
        return clock[0]

    base_conn = migrate.open_connection(env.live)
    try:
        migrate.record_baseline(base_conn)
    finally:
        base_conn.close()
    copy_db = env.root / "copy.db"
    shutil_copy = sqlite3.connect(str(env.live))
    try:
        copy_conn = sqlite3.connect(str(copy_db))
        try:
            shutil_copy.backup(copy_conn)
        finally:
            copy_conn.close()
    finally:
        shutil_copy.close()
    migrate.run(db_path=copy_db, dry_run=True, drain_rounds=2, drain_min_interval=10.0,
                clock=fake_clock, sleeper=lambda _s: None,
                checkpoint=lambda: (0, 0, 0), holders=lambda: [])
    assert calls["n"] == 0
