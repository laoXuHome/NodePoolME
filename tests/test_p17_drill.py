"""P-1.7 全链路故障恢复与迁移演练：PT-05 / PT-07 / PT-08 / PT-17 / PT-18 / PT-20 / PT-22 / PT-26。

全部在**隔离副本库 + 临时目录**上执行：不接触 NAS、生产库、生产容器、生产备份目录，
不执行 P-7，不接 durable 事件链（PT-09b 保持有效）。

PT-05 说明：本环境无 docker（`docker: command not found`），无法按计划"临时容器
`-p 8089:8080`"起生产镜像；改用**独立 uvicorn 进程 + 独立端口 8089 + 独立
`NODEPOOL_DB`/`NODEPOOL_STATE_DIR`** 等价演练，判定项（`/healthz` 200、条数一致、
订阅四格式可重建）不变。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import config
from backend.database import backup, cli, db, gate, maint, migrate, pool

REPO_ROOT = Path(__file__).resolve().parents[1]
SUB_TOKEN = "p17-sub-token-0123456789"


# --- helpers -----------------------------------------------------------------

def _tmp_root() -> Path:
    root = Path.home() / ".p17test"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=str(root)))


def _fast_drain() -> dict:
    clock = [0.0]

    def fake_clock() -> float:
        clock[0] += 100.0
        return clock[0]

    return {"drain_rounds": 2, "drain_min_interval": 10.0, "clock": fake_clock,
            "sleeper": lambda _s: None, "checkpoint": lambda: (0, 0, 0),
            "holders": lambda: []}


def _fast_rollback() -> dict:
    """回滚演练用：排空判据不削弱（2 轮 + ≥10s），时钟/休眠注入使测试快速完成。"""
    params = _fast_drain()
    params.pop("holders")          # rollback 的探针签名是 holders(target)
    return {**params, "holders": lambda _target: []}


def _activate(migration_id: str = "mig-p17") -> dict:
    return maint.set_maintenance(
        migrate.MigrationRecord(migration_id=migration_id, phase=migrate.Phase.PREPARED),
        reason="p17_test")


def _set_env(monkeypatch, root: Path) -> SimpleNamespace:
    state = root / "state"
    state.mkdir()
    data = root / "data"
    data.mkdir()
    live = data / "live.db"
    copy_db = root / "copy.db"
    backups = root / "backup"
    backups.mkdir()
    sub = data / "sub"
    monkeypatch.setattr(config, "DB_PATH", live)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "SUB_DIR", sub)
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
    return SimpleNamespace(root=root, state=state, data=data, live=live, copy=copy_db,
                           backups=backups, sub=sub,
                           fault_log=state / "migration-fault.log")


@pytest.fixture
def env(monkeypatch):
    root = _tmp_root()
    ns = _set_env(monkeypatch, root)
    db.init_db()
    stamp = db.now()
    for name in ("p17-a1", "p17-a2"):
        db.execute(
            "INSERT INTO airports (name, url, enabled, interval_minutes, daily_time,"
            " filter_keywords, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
            (name, f"https://{name}.example/sub", 1, 60, None, "[]", stamp, stamp))
    db.set_setting("sub_token", SUB_TOKEN)      # 固定 token，便于订阅演练取数
    db.reset_connection()
    conn = migrate.open_connection(ns.live)
    try:
        migrate.record_baseline(conn)
        conn.execute("VACUUM INTO ?", (str(ns.copy),))
    finally:
        conn.close()

    yield ns

    migrate.reset_issuer_token_for_tests()
    maint.reset_state_dir_selfcheck()
    db.reset_connection()
    pool.reset_for_tests()
    shutil.rmtree(root, ignore_errors=True)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            h.update(candidate.read_bytes())
    return h.hexdigest()


def _counts(path: Path) -> dict[str, int]:
    conn = sqlite3.connect(f"{Path(path).as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        conn.execute("PRAGMA query_only=1")
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
    finally:
        conn.close()


def _structure_hash(path: Path) -> str:
    conn = migrate.open_connection(path)
    try:
        return migrate.structure_hash(conn)
    finally:
        conn.close()


def _rows(path: Path) -> list[dict]:
    conn = migrate.open_connection(path)
    try:
        return [dict(row) for row in migrate.migration_rows(conn)]
    finally:
        conn.close()


def _pre_set(ns: SimpleNamespace) -> dict:
    return backup.snapshot_set(stage="pre-migrate", src_db=ns.copy,
                               backup_dir=ns.backups, stop_write=True)


def _wal_bytes(path: Path) -> int:
    candidate = Path(str(path) + "-wal")
    return candidate.stat().st_size if candidate.exists() else 0


def _run_copy(ns: SimpleNamespace, **kwargs) -> dict:
    return migrate.run(db_path=ns.copy, statements=kwargs.pop("statements", ()),
                       **_fast_drain(), **kwargs)


# ----------------------------------------------------------------- PT-07
def test_pt07_snapshot_failure_halts_without_running_row(env, monkeypatch):
    """备份失败 ⇒ halt_migration("snapshot_failed")：无 running 行、封锁保持、CLI 非零退出。"""
    _activate()

    def boom(*_a, **_k):
        raise backup.BackupError("snapshot_failed", "注入：磁盘/IO 失败")

    monkeypatch.setattr(backup, "snapshot_set", boom)
    before_rows = [r["status"] for r in _rows(env.copy)]
    res = _run_copy(env, snapshot=True)

    print("[PT-07]", res.get("code"), res.get("halt_code"), "phase=", res.get("phase"))
    assert res["ok"] is False
    assert res.get("code") == "snapshot_failed" or res.get("halt_code") == "snapshot_failed"
    assert [r["status"] for r in _rows(env.copy)] == before_rows       # 无 running 行
    assert "running" not in [r["status"] for r in _rows(env.copy)]
    assert pool.writes_blocked() is True
    with pytest.raises(pool.PoolClosed):
        with pool.write_slot():
            pass
    assert cli.main(["run", "--db", str(env.copy)]) != 0              # 退出码非零
    assert pool.writes_blocked() is True


# ----------------------------------------------------------------- PT-08
def test_pt08_alter_failure_marks_failed_then_snapshot_restore(env):
    """DDL 中途抛错 ⇒ 回滚 + failed 行 ⇒ pre-migrate 恢复 ⇒ 结构/行数复原 ⇒ 人工解封后可写。"""
    _activate()
    info = _pre_set(env)
    hash_before = _structure_hash(env.copy)
    counts_before = _counts(env.copy)

    res = _run_copy(env, statements=[
        "ALTER TABLE nodes ADD COLUMN p17_probe TEXT",
        "ALTER TABLE nodes ADD COLUMN p17_probe TEXT",     # 重复列 ⇒ 事务内抛错
    ])
    assert res["ok"] is False
    assert res.get("code") in ("known_failed", "failed", "migration_failed")
    statuses = [r["status"] for r in _rows(env.copy)]
    assert statuses.count("failed") == 1
    assert pool.writes_blocked() is True

    # 结构复核：事务回滚后不得残留新列
    probe_conn = sqlite3.connect(str(env.copy))
    try:
        cols = probe_conn.execute("PRAGMA table_info(nodes)").fetchall()
    finally:
        probe_conn.close()
    assert "p17_probe" not in [c[1] for c in cols]

    restore = migrate.rollback_to_snapshot(info["path"], db_path=env.copy, **_fast_rollback())
    assert restore["code"] == "restored", restore.get("restore_output")
    assert restore["restored"] is True
    print("[PT-08] run=", res.get("code"), "restore=", json.dumps(
        {k: restore.get(k) for k in ("code", "steps", "restore_exit", "timings")},
        ensure_ascii=False))
    assert restore["steps"] == list(migrate.RESTORE_SEQUENCE)
    assert _structure_hash(env.copy) == hash_before
    assert _counts(env.copy) == counts_before
    assert backup.verify_set(info["path"])["ok"] is True          # 恢复后 L1/L2 仍通过

    # 人工确认解封后才允许写入（恢复本身不得自动开放）
    assert pool.writes_blocked() is True
    maint.MAINT_FILE.unlink()
    pool.unblock_writes()
    db.set_setting("p17_writable", "1")
    assert db.get("p17_writable") == "1"


# ----------------------------------------------------------------- PT-17
def test_pt17_rollback_script_full_checks(env):
    """完整回滚剧本：行数/摘要一致、无 stale WAL、订阅四格式重建、维护文件需人工移除。"""
    _activate()
    info = _pre_set(env)
    hash_before = _structure_hash(env.copy)
    counts_before = _counts(env.copy)

    # 演练改动：数据层（新增一行机场）——结构摘要不变，行数变化可被恢复抹平。
    # 说明：`pre-restore` 快照会做 L1 结构登记校验，注入未登记的 DDL 会让
    # 子进程的 pre-restore 快照自身失败（fail-closed），故结构变更留给 PT-08 的
    # 回滚路径验证，这里只验证"行数/摘要一并复原"的剧本。
    conn = sqlite3.connect(str(env.copy))
    try:
        conn.execute("INSERT INTO airports (name, url, enabled, interval_minutes,"
                     " filter_keywords, created_at, updated_at)"
                     " VALUES ('p17-late','https://late.example/sub',1,60,'[]',"
                     "'2026-11-26 00:00:00','2026-11-26 00:00:00')")
        conn.commit()
    finally:
        conn.close()
    assert _counts(env.copy)["airports"] == counts_before["airports"] + 1
    restore = migrate.rollback_to_snapshot(info["path"], db_path=env.copy, **_fast_rollback())

    assert restore["code"] == "restored", restore.get("restore_output")
    assert _counts(env.copy) == counts_before
    assert _structure_hash(env.copy) == hash_before
    assert _wal_bytes(env.copy) == 0                     # 无 stale WAL 帧（仅新开空的 -wal）
    assert Path(env.state / "maintenance.json").exists()  # 恢复不得自动清维护文件

    maint.MAINT_FILE.unlink()
    pool.unblock_writes()
    from backend.subscriptions import builder
    report = builder.publish("p17-restore")
    assert report.get("updated_at")
    for fmt, marker in (("clash", "proxies:"), ("singbox", "outbounds"), ("loon", "Loon")):
        fmt_name, content = builder.read(fmt)
        assert fmt_name == fmt and marker in content
    (fmt4, content4) = builder.read("base64")
    assert fmt4 == "base64" and isinstance(content4, str)   # 0 节点 ⇒ base64 空载荷属正常
    assert (config.SUB_DIR / "sub_all.base64").exists()


# ----------------------------------------------------------------- PT-18
def test_pt18_conservative_recovery_five_entries(env, monkeypatch):
    """删维护文件 + 非 success 行 ⇒ 五入口一致 RECOVERY_REQUIRED，写入不开放。"""
    conn = migrate.open_connection(env.live)
    try:
        conn.execute("INSERT INTO schema_migrations (migration_id, seq, version, status,"
                     " started_at, finished_at, detail_json) VALUES (?,?,?,?,?,?,?)",
                     ("m-p18", 2, "1.10", "failed", "2026-01-01 00:00:00",
                      "2026-01-01 00:00:01", "{}"))
    finally:
        conn.close()
    monkeypatch.setattr(config, "DB_PATH", env.live)
    assert not maint.MAINT_FILE.exists()

    state = maint.maintenance_state()
    assert state is maint.MaintState.RECOVERY_REQUIRED

    # 入口 1：HTTP 写 503
    from backend.main import app
    from fastapi.testclient import TestClient
    with TestClient(app) as client:
        resp = client.post("/api/does-not-matter", json={})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "recovery_required"
        assert client.get("/healthz").json()["migration"] == "RECOVERY_REQUIRED"
        # 入口 2：调度器本轮不下发/不循环
        from backend.scheduler import manager
        assert manager.maintenance_hold()
        before_cycle = manager.state.last_cycle_at      # 其他模块可能已留下历史值，只比变化
        asyncio.run(manager.job_cycle())
        assert manager.state.last_cycle_at == before_cycle

    # 入口 3：连接池拒绝 checkout
    with pytest.raises(pool.PoolClosed):
        with pool.write_slot():
            pass
    # 入口 4：CLI 写命令非零退出
    assert cli.main(["snapshot"]) == cli.EXIT_REFUSED
    # 入口 5：write_guard 抛错
    with pytest.raises(Exception) as exc:
        with gate.write_guard():
            pass
    assert "recovery" in str(exc.value).lower() or "maint" in str(exc.value).lower()
    assert cli.main(["status"]) == cli.EXIT_OK


# ----------------------------------------------------------------- PT-22
def test_pt22a_third_party_handle_blocks_restore_without_touching_files(env):
    """注入"仍有第三方句柄"⇒ 中止且不改文件（F10）。"""
    _activate()
    info = _pre_set(env)
    digest_before = _digest(env.copy)
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import sqlite3,sys,time;c=sqlite3.connect(sys.argv[1]);"
         "print('held',flush=True);time.sleep(30)", str(env.copy)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        time.sleep(0.3)
        params = _fast_rollback()
        params.pop("holders")        # 用真实 /proc 探针，不注入
        res = migrate.rollback_to_snapshot(info["path"], db_path=env.copy, **params)
        print("[PT-22a]", res["code"], res.get("holders"))
        assert res["code"] == "restore_handles_present"
        assert res["restored"] is False
        assert res["holders"]
        assert _digest(env.copy) == digest_before       # 原库一动不动
        assert pool.writes_blocked() is True
    finally:
        holder.terminate()
        holder.wait(timeout=10)


def test_pt22b_replace_failure_keeps_original_db(env, monkeypatch):
    """替换失败（os.replace 抛错）⇒ 原库按隔离副本还原，抛出 replace_failed。"""
    info = _pre_set(env)
    digest_before = _digest(env.copy)
    real_replace = backup.os.replace

    fired = {"n": 0}

    def boom(src, dst):
        if Path(str(dst)) == env.copy and fired["n"] == 0:
            fired["n"] += 1
            raise OSError("injected replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(info["path"], db_path=env.copy, backup_dir=env.backups,
                           close_connections=lambda: None)
    print("[PT-22b]", exc.value.reason)
    assert exc.value.reason in ("restore_replace_failed", "restore_rolled_back")
    monkeypatch.setattr(backup.os, "replace", real_replace)
    assert _digest(env.copy) == digest_before          # 回到原库
    assert backup.valid_sets(env.backups)


def test_pt22c_post_restore_init_db_failure_rolls_back(env, monkeypatch):
    """注入"替换后 init_db() 失败"⇒ 自动回退 pre-restore 且回到原库。"""
    info = _pre_set(env)
    digest_before = _digest(env.copy)

    real_init = db.init_db

    def boom(*_a, **_k):
        raise RuntimeError("injected init_db failure after replace")

    monkeypatch.setattr(db, "init_db", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(info["path"], db_path=env.copy, backup_dir=env.backups,
                           close_connections=lambda: None)
    monkeypatch.setattr(db, "init_db", real_init)
    print("[PT-22c]", exc.value.reason)
    assert exc.value.reason in ("restore_rolled_back", "restore_replace_failed")
    assert _digest(env.copy) == digest_before
    pre_restore = [s for s in backup.list_sets(env.backups) if s["stage"] == "pre-restore"]
    assert pre_restore and backup.verify_set(pre_restore[-1]["path"])["ok"] is True


def test_pt22d_restore_clears_wal_before_atomic_replace(env, monkeypatch):
    """断言：替换前 `-wal`/`-shm` 已清，替换是 `os.replace`（不是 copy2）。"""
    info = _pre_set(env)
    observed: dict[str, object] = {}
    real_replace = backup.os.replace

    def watching(src, dst):
        if Path(str(dst)) == env.copy:
            observed["wal"] = Path(str(env.copy) + "-wal").exists()
            observed["shm"] = Path(str(env.copy) + "-shm").exists()
            observed["src_is_same_dir"] = Path(str(src)).parent == env.copy.parent
        return real_replace(src, dst)

    monkeypatch.setattr(backup.os, "replace", watching)
    report = backup.restore_set(info["path"], db_path=env.copy, backup_dir=env.backups,
                                close_connections=lambda: None)
    assert report["ok"] is True
    assert observed["wal"] is False and observed["shm"] is False
    assert observed["src_is_same_dir"] is True          # 同卷临时文件 ⇒ 原子替换
    assert backup.verify_set(info["path"])["ok"] is True


# ----------------------------------------------------------------- PT-26
def _age_set(path: Path, created_at: str) -> None:
    manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
    manifest["created_at"] = created_at
    backup._write_json(path / "manifest.json", manifest)
    complete = json.loads((path / "complete.json").read_text(encoding="utf-8"))
    complete["manifest_sha256"] = backup._sha256(path / "manifest.json")
    backup._write_json(path / "complete.json", complete)


def test_pt26_prune_protects_pre_sets_and_partials(env):
    pre_migrate = backup.snapshot_set(stage="pre-migrate", src_db=env.copy,
                                      backup_dir=env.backups, stop_write=True)
    pre_restore = backup.snapshot_set(stage="pre-restore", src_db=env.copy,
                                      backup_dir=env.backups, stop_write=True)
    old_daily = backup.snapshot_set(stage="daily", src_db=env.copy, backup_dir=env.backups)
    new_daily = backup.snapshot_set(stage="daily", src_db=env.copy, backup_dir=env.backups)
    old_manual = backup.snapshot_set(stage="manual", src_db=env.copy, backup_dir=env.backups)
    for item in (pre_migrate, pre_restore, old_daily, old_manual):
        _age_set(Path(item["path"]), "2020-01-01 00:00:00")
    # 半成品：只留 db，无 manifest/complete
    partial = env.backups / f"{backup.SET_PREFIX}20200101-000000-daily"
    partial.mkdir()
    shutil.copyfile(env.copy, partial / backup.DB_FILE)

    report = backup.prune(env.backups, keep_days=30, keep_daily=1)
    print("[PT-26] removed=", report["removed"], "kept=", report["kept"],
          "skipped=", report["skipped"])
    kept = set(report["kept"]) | set(report["skipped"])
    removed = set(report["removed"])
    assert Path(pre_migrate["path"]).exists()
    assert Path(pre_restore["path"]).exists()
    assert partial.exists()                      # 半成品 24h 内不删
    assert Path(new_daily["path"]).exists()      # keep_daily=1 保留最新
    assert Path(old_daily["path"]).name in removed
    assert Path(old_manual["path"]).name in removed
    assert pre_migrate["file"] in kept and pre_restore["file"] in kept


# ----------------------------------------------------------------- PT-20
def test_pt20_performance_budget_real_timings(env):
    """100 MB 库：快照 ≤5s、排空 ≤30s、总停写窗口 ≤60s（真实耗时，不估算）。"""
    bulk_root = env.root / "bulk"
    bulk_root.mkdir()
    bulk_db = bulk_root / "bulk.db"
    shutil.copyfile(env.copy, bulk_db)
    conn = sqlite3.connect(str(bulk_db))
    try:
        # 用业务表 `logs` 的正文撑到 100 MB 量级：不新增表，结构摘要保持已登记
        blob = "P" * (1024 * 1024)
        conn.execute("BEGIN")
        for i in range(100):
            conn.execute("INSERT INTO logs (ts, level, source, message) VALUES (?,?,?,?)",
                         ("2026-11-26 00:00:00", "INFO", "p17-bulk", blob))
        conn.execute("COMMIT")
        conn.execute("PRAGMA journal_mode=WAL")     # 与生产一致的 WAL 库
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    size_mb = bulk_db.stat().st_size / (1024 * 1024)
    assert size_mb >= 95, f"库体量 {size_mb:.1f} MB 未达 100 MB 量级"

    t0 = time.monotonic()
    snap = backup.snapshot_set(stage="pre-migrate", src_db=bulk_db,
                               backup_dir=bulk_root / "backup", stop_write=True)
    t_snap = time.monotonic() - t0
    t_verify_start = time.monotonic()
    verdict = backup.verify_set(snap["path"])
    t_verify = time.monotonic() - t_verify_start
    assert verdict["ok"] is True

    pool.block_writes("p17-pt20")           # 排空判据要求写封锁已生效（与真实窗口一致）
    assert pool.confirm_writes_blocked()[0] is True
    t0 = time.monotonic()
    drained = gate.drain(rounds=2, min_interval=10.0, db_path=bulk_db)
    t_drain = time.monotonic() - t0
    pool.unblock_writes()
    assert drained.ok is True, drained.detail

    window = t_snap + t_verify + t_drain
    print(f"\n[PT-20] db={size_mb:.1f}MB snapshot={t_snap:.3f}s verify={t_verify:.3f}s "
          f"drain={t_drain:.3f}s total={window:.3f}s")
    assert t_snap <= 5.0, f"快照 {t_snap:.2f}s 超出 5s"
    assert t_drain <= 30.0, f"排空 {t_drain:.2f}s 超出 30s"
    assert window <= 60.0, f"总停写窗口 {window:.2f}s 超出 60s"


# ----------------------------------------------------------------- PT-05
def _child_env(ns: SimpleNamespace, db_path: Path, state_dir: Path) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "NODEPOOL_DB": str(db_path),
        "NODEPOOL_DATA_DIR": str(db_path.parent),
        "NODEPOOL_CONFIG_DIR": str(db_path.parent / "config"),
        "NODEPOOL_LOG_DIR": str(db_path.parent / "logs"),
        "NODEPOOL_BACKUP_DIR": str(db_path.parent / "backup"),
        "NODEPOOL_STATE_DIR": str(state_dir),
        "NODEPOOL_MIGRATION_ENABLED": "0",
        "NODEPOOL_ADMIN_PASSWORD": "p17pass",
        "NODEPOOL_TEST_ENGINE": "tcp",
    })
    return env


def _http(url: str, timeout: float = 3.0) -> tuple[int, str]:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def test_pt05_l3_restore_drill_on_port_8089(env):
    """L3 恢复演练：独立端口 8089 起临时服务，`/healthz` 200、条数一致、订阅四格式可重建。"""
    serve_root = env.root / "serve"
    serve_root.mkdir()
    serve_db = serve_root / "restored.db"
    serve_state = serve_root / "state"
    serve_state.mkdir()
    info = _pre_set(env)
    # 用备份集内容落成"恢复副本"（等价于停机窗口恢复后的库）
    shutil.copyfile(Path(info["path"]) / backup.DB_FILE, serve_db)
    counts_source = _counts(env.copy)
    counts_restored = _counts(serve_db)          # 起服务之前：恢复副本必须与源逐表一致
    for table in ("airports", "nodes", "settings"):
        assert counts_restored[table] == counts_source[table], table
    assert counts_restored["airports"] >= 2

    cenv = _child_env(env, serve_db, serve_state)
    pub = subprocess.run([sys.executable, "-c",
                          "import json;from backend.subscriptions import builder;"
                          "print(json.dumps(builder.publish('p17-pt05')))"],
                         cwd=str(REPO_ROOT), env=cenv, capture_output=True, text=True,
                         timeout=180)
    assert pub.returncode == 0, pub.stderr[-800:]

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1",
         "--port", "8089", "--log-level", "warning"],
        cwd=str(REPO_ROOT), env=cenv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True)
    try:
        health = None
        for _ in range(60):
            try:
                health = _http("http://127.0.0.1:8089/healthz")
                break
            except (urllib.error.URLError, ConnectionError):
                time.sleep(0.5)
        assert health is not None, "8089 端口服务未在 30s 内就绪"

        status, body = health
        payload = json.loads(body)
        assert status == 200 and payload["status"] == "ok"

        # 起服务后只比对业务实体条数（服务启动会自行补写 settings 种子项）
        counts_served = _counts(serve_db)
        for table in ("airports", "nodes"):
            assert counts_served[table] == counts_source[table], table

        # 订阅四格式可重建（副本 0 节点 ⇒ base64 空载荷属正常，另三种带格式头）
        for fmt in ("clash", "singbox", "loon"):
            code, text = _http(f"http://127.0.0.1:8089/sub/all/{SUB_TOKEN}/{fmt}")
            assert code == 200, fmt
            assert len(text.strip()) > 0, fmt
        assert "proxies:" in _http(f"http://127.0.0.1:8089/sub/all/{SUB_TOKEN}/clash")[1]
        code, b64 = _http(f"http://127.0.0.1:8089/sub/all/{SUB_TOKEN}/base64")
        assert code == 200
        import base64
        assert base64.b64decode(b64 or "") is not None          # 空载荷也必须是合法 base64
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
