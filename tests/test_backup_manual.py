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
import subprocess
import sys
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


def test_preimport_captures_committed_wal_data(env):
    """WAL 里存在已提交但未 checkpoint 的数据时，preimport 必须完整包含它。"""
    info = backup.export_backup(label="wal")
    label = f"wal-proof-{time.time_ns()}"
    code = (
        "import os, sqlite3, sys\n"
        "db = sys.argv[1]\n"
        "conn = sqlite3.connect(db, isolation_level=None)\n"
        "conn.execute('PRAGMA journal_mode=WAL')\n"
        "conn.execute(\"INSERT INTO airports (name, url, enabled, interval_minutes,\"\n"
        "             \" daily_time, filter_keywords, created_at, updated_at)\"\n"
        "             \" VALUES (?,?,?,?,?,?,?,?)\",\n"
        "             (sys.argv[2], 'https://wal.example/sub', 1, 60, None, '[]',\n"
        "              '2026-11-26 20:00:00', '2026-11-26 20:00:00'))\n"
        "os._exit(0)  # 不 close：留下未 checkpoint 的已提交 WAL\n"
    )
    subprocess.run([sys.executable, "-c", code, str(env.live), label], check=True,
                   env={**os.environ, "NODEPOOL_DB": str(env.live)})
    wal = Path(str(env.live) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "前置条件：WAL 必须存在"
    live_counts = _counts(env.live)                    # 只读连接能看到 WAL 内已提交行
    assert live_counts["airports"] == 3

    db.reset_connection()
    db.close_all_connections()
    pool.block_writes("test-wal")
    try:
        result = backup.import_backup(info["path"])
    finally:
        pool.unblock_writes()
    assert result["ok"] is True
    assert result["preimport_tables"] == live_counts      # 副本逐表计数 = 含 WAL 的真实原库
    preimport = Path(result["preimport_copy"])
    conn = sqlite3.connect(f"file:{preimport}?mode=ro", uri=True)
    try:
        names = {row[0] for row in conn.execute("SELECT name FROM airports")}
    finally:
        conn.close()
    assert label in names, "preimport 丢失 WAL 中已提交的数据"
    assert _counts(env.live)["airports"] == 2             # 目标已换成旧备份
    assert not Path(str(env.live) + "-wal").exists() or \
        Path(str(env.live) + "-wal").stat().st_size == 0


def test_import_failure_reports_scene_and_keeps_original(env):
    info = backup.export_backup(label="fail")
    _add_airports(2, prefix="keep-")
    db.reset_connection()
    db.close_all_connections()
    before_counts = _counts(env.live)
    pool.block_writes("test-fail")

    def boom(_target: Path) -> None:
        raise RuntimeError("simulated replace failure")

    try:
        with pytest.raises(backup.BackupError) as exc:
            backup.import_backup(info["path"], before_replace=boom)
    finally:
        pool.unblock_writes()
    assert exc.value.reason == "import_failed"
    assert _counts(env.live) == before_counts               # 原库已复原（内容等价）
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


# --- 验收修正专项 -------------------------------------------------------------


def test_close_report_keeps_failed_connection(monkeypatch):
    """关闭失败的连接不得从登记表移除，也不得被计为成功。"""

    class BadConn:
        def close(self):
            raise sqlite3.OperationalError("simulated close failure")

    key = 987654
    monkeypatch.setattr(db, "_WRITE_CONNS", {key: BadConn()})
    report = db.close_all_connections_report()
    assert report["ok"] is False
    assert report["failed"] and report["failed"][0]["label"] == f"write:{key}"
    assert report["remaining"] == [f"write:{key}"]
    assert key in db._WRITE_CONNS


def test_close_failure_aborts_import(env, monkeypatch):
    """关闭确认失败 ⇒ 中止导入；不得替换库、不得清维护态。"""
    _fast_gate(monkeypatch)
    info = backup.export_backup(label="closefail")
    counts = _counts(env.live)
    monkeypatch.setattr(db, "close_all_connections_report", lambda: {
        "closed": 0, "failed": [{"label": "write:1", "error": "simulated"}],
        "remaining": ["write:1"], "ok": False})
    rc = cli.main(["import", "--file", info["path"], "--json"])
    assert rc == cli.EXIT_HALTED
    assert _counts(env.live) == counts
    assert Path(maint.MAINT_FILE).exists()
    assert pool.writes_blocked() is True
    pool.reset_for_tests()
    db.reset_connection()


def test_self_handles_block_import(env):
    """当前进程自己持有的库文件句柄不能被忽略。"""
    info = backup.export_backup(label="selfhandle")
    digest = _sha(env.live)
    db.close_all_connections()
    pool.block_writes("self-handle-test")
    handle = open(env.live, "rb")
    try:
        with pytest.raises(backup.BackupError) as exc:
            backup.import_backup(info["path"])
        assert exc.value.reason == "import_self_handles_present"
    finally:
        handle.close()
        pool.unblock_writes()
    assert _sha(env.live) == digest


def test_verify_and_import_require_strict_meta(env, monkeypatch):
    _fast_gate(monkeypatch)
    info = backup.export_backup(label="meta-strict")
    path = Path(info["path"])
    meta_path = Path(info["meta_path"])
    good_meta = meta_path.read_text(encoding="utf-8")

    # 1) 缺 .json
    meta_path.unlink()
    report = backup.verify_backup(path)
    assert report["ok"] is False and "meta_missing" in report["errors"]
    rc = cli.main(["import", "--file", str(path), "--json"])
    assert rc == cli.EXIT_REFUSED
    assert not Path(maint.MAINT_FILE).exists()

    # 2) .json 损坏
    meta_path.write_text("{not json", encoding="utf-8")
    report = backup.verify_backup(path)
    assert report["ok"] is False and "meta_unreadable" in report["errors"]

    # 3) 字段缺失
    meta_path.write_text(good_meta, encoding="utf-8")
    meta = json.loads(good_meta)
    meta.pop("tables")
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    report = backup.verify_backup(path)
    assert report["ok"] is False
    assert any(e.startswith("meta_fields_missing:") for e in report["errors"])


def test_same_second_exports_never_overwrite(env, monkeypatch):
    monkeypatch.setattr(backup, "_stamp", lambda: "20260101-000000")
    paths = [backup.export_backup(label="same")["path"] for _ in range(3)]
    assert len(set(paths)) == 3
    for path in paths:
        assert backup.verify_backup(path)["ok"] is True
    assert len(list(env.backups.glob("nodepool-20260101-000000-same-*.db"))) == 3


def test_concurrent_exports_never_overwrite(env, monkeypatch):
    monkeypatch.setattr(backup, "_stamp", lambda: "20260101-000000")
    results: list[str] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(backup.export_backup(label="conc")["path"])
        except BaseException as exc:                            # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    assert len(set(results)) == 6
    for path in results:
        assert backup.verify_backup(path)["ok"] is True


def test_cli_backup_and_verify(env):
    out = env.root / "manual-out"
    rc = cli.main(["backup", "--out", str(out), "--label", "offline", "--json"])
    assert rc == cli.EXIT_OK
    files = sorted(out.glob("nodepool-*-offline-*.db"))
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
