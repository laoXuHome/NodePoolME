"""P-1.3 备份集验收测试：PT-01 / PT-02 / PT-03 / PT-04 / PT-04b / PT-21 + 恢复与清理。

全部在临时目录与一次性库上执行；不接触仓库开发实例、不接触生产库。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from backend import config
from backend.database import backup, migrate

REPO = Path(__file__).resolve().parents[1]


# --- helpers ------------------------------------------------------------------


def _fresh_db(tmp_path: Path, name: str = "src.db") -> Path:
    db_path = tmp_path / name
    env = {**os.environ,
           "NODEPOOL_DATA_DIR": str(tmp_path),
           "NODEPOOL_DB": str(db_path)}
    subprocess.run(
        [sys.executable, "-c",
         "from backend.database import db\n"
         "db.init_db()\n"
         "db.reset_connection()\n"],
        cwd=str(REPO), env=env, check=True, capture_output=True,
    )
    conn = sqlite3.connect(db_path)
    try:
        conn.executemany(
            "INSERT INTO settings(key, value) VALUES (?,?)",
            [("k1", "v1"), ("k2", "v2"), ("k3", "v3")],
        )
        for idx in range(5):
            conn.execute(
                "INSERT INTO logs(ts, level, source, message) VALUES (?,?,?,?)",
                (f"2026-11-26 16:00:0{idx}", "INFO", "pt13", f"seed-{idx}"),
            )
        conn.commit()
        seeded = {"settings": int(conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0]),
                  "logs": int(conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0])}
    finally:
        conn.close()
    _fresh_db.last = seeded  # type: ignore[attr-defined]
    return db_path


def _resign(set_dir: Path) -> None:
    """把 db/manifest/complete 三者签名重算为互相自洽，用于模拟"签名齐全的篡改"。"""
    manifest = json.loads((set_dir / "manifest.json").read_text())
    manifest["db_sha256"] = hashlib.sha256((set_dir / "db").read_bytes()).hexdigest()
    (set_dir / "manifest.json").write_text(json.dumps(manifest))
    complete = json.loads((set_dir / "complete.json").read_text())
    complete["db_sha256"] = manifest["db_sha256"]
    complete["manifest_sha256"] = hashlib.sha256((set_dir / "manifest.json").read_bytes()).hexdigest()
    (set_dir / "complete.json").write_text(json.dumps(complete))


def _open(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])


def _rows(conn: sqlite3.Connection, table: str) -> list[list]:
    return [list(row) for row in conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid')]


def _recomplete(set_dir: Path) -> None:
    """重写 complete.json 的 sha，用于模拟"结构坏但 sha 自洽"的备份集。"""
    complete_path = set_dir / backup.COMPLETE_FILE
    payload = json.loads(complete_path.read_text(encoding="utf-8"))
    payload["db_sha256"] = backup._sha256(set_dir / backup.DB_FILE)
    payload["manifest_sha256"] = backup._sha256(set_dir / backup.MANIFEST_FILE)
    payload["bytes"] = (set_dir / backup.DB_FILE).stat().st_size
    complete_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# --- PT-01 --------------------------------------------------------------------


def test_pt01_online_snapshot_with_concurrent_writes(tmp_path):
    src = _fresh_db(tmp_path)
    bdir = tmp_path / "backup"
    errors: list[Exception] = []
    stop = threading.Event()

    def writer() -> None:
        conn = sqlite3.connect(src, timeout=5)
        try:
            while not stop.is_set():
                try:
                    conn.execute(
                        "INSERT INTO logs(ts, level, source, message) VALUES (?,?,?,?)",
                        ("2026-11-26 16:10:00", "INFO", "pt01", "concurrent"),
                    )
                    conn.commit()
                except sqlite3.Error:
                    pass
                time.sleep(0.003)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            conn.close()

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.05)

    dirty = sqlite3.connect(src, timeout=5, isolation_level=None)
    dirty.execute("BEGIN")
    dirty.execute(
        "INSERT INTO logs(ts, level, source, message) VALUES ('2026-11-26 16:11:00','INFO','pt01','DIRTY-UNCOMMITTED')"
    )

    result = backup.snapshot_set("daily", src_db=src, backup_dir=bdir, min_free_mb=0)
    stop.set()
    thread.join(timeout=3)
    dirty.execute("ROLLBACK")
    dirty.close()
    assert not errors, errors

    assert result["ok"] is True and result["verified"] is True
    set_dir = Path(result["path"])
    conn = _open(set_dir / backup.DB_FILE)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        messages = [row["message"] for row in conn.execute("SELECT message FROM logs")]
        assert "DIRTY-UNCOMMITTED" not in messages, "未提交事务的行不得进入备份"
        assert _count(conn, "logs") >= _fresh_db.last["logs"]
        assert _count(conn, "settings") >= _fresh_db.last["settings"]
    finally:
        conn.close()

    verdict = backup.verify_set(set_dir)
    assert verdict["ok"] is True
    assert verdict["l2"]["mode"] == "online"
    manifest = json.loads((set_dir / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["write_quiesced"] is False
    assert manifest["source_counts"] is None


def test_pt01_space_gate_refuses_snapshot(tmp_path):
    src = _fresh_db(tmp_path)
    with pytest.raises(backup.BackupError) as exc:
        backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b",
                            min_free_mb=10 ** 9)
    assert exc.value.reason == "snapshot_failed"
    assert not (tmp_path / "b").exists() or list((tmp_path / "b").iterdir()) == []


# --- PT-02 --------------------------------------------------------------------


def test_pt02_plain_copy_is_not_a_valid_backup(tmp_path):
    src = _fresh_db(tmp_path)
    # 造出未 checkpoint 的 WAL 帧
    holder = sqlite3.connect(src)
    holder.execute("INSERT INTO logs(ts, level, source, message) VALUES ('2026-11-26 16:20:00','INFO','pt02','wal-only')")
    holder.commit()
    assert Path(str(src) + "-wal").exists()

    cp_path = tmp_path / "cp-copy.db"
    shutil.copyfile(src, cp_path)

    snapshot = backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    api_conn = _open(Path(snapshot["path"]) / backup.DB_FILE)
    assert holder.execute("SELECT COUNT(*) FROM logs").fetchone()[0] == _count(api_conn, "logs")
    holder.close()
    cp_conn = sqlite3.connect(cp_path)
    try:
        api_tables = len([r for r in api_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")])
        cp_tables = len([r for r in cp_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")])
        api_logs = _count(api_conn, "logs")
        cp_logs = cp_conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0] if cp_tables else 0
        assert api_tables == 17
        # 文件拷贝得到的副本：要么缺表，要么丢行 —— 均不可当备份
        assert cp_tables < api_tables or cp_logs < api_logs
    finally:
        api_conn.close()
        cp_conn.close()


# --- PT-03 --------------------------------------------------------------------


def test_pt03_l1_good_set_passes_and_bad_sets_fail(tmp_path):
    src = _fresh_db(tmp_path)
    bdir = tmp_path / "b"
    result = backup.snapshot_set("manual", src_db=src, backup_dir=bdir, min_free_mb=0)
    good = Path(result["path"])
    assert backup.verify_set(good)["ok"] is True

    # ① 截断 1 KB
    bad1 = tmp_path / "bad1"
    shutil.copytree(good, bad1)
    with open(bad1 / backup.DB_FILE, "r+b") as handle:
        handle.truncate((bad1 / backup.DB_FILE).stat().st_size - 1024)
    verdict1 = backup.verify_set(bad1)
    assert verdict1["ok"] is False
    assert any(e["code"] == "db_sha_mismatch" for e in verdict1["errors"])

    # ② 篡改页
    bad2 = tmp_path / "bad2"
    shutil.copytree(good, bad2)
    with open(bad2 / backup.DB_FILE, "r+b") as handle:
        handle.seek(4096 * 3 + 128)
        handle.write(b"\x00\xff\x00\xff")
    verdict2 = backup.verify_set(bad2)
    assert verdict2["ok"] is False
    assert any(e["code"] in ("db_sha_mismatch", "integrity_check") for e in verdict2["errors"])

    # ③ 删除表（并重算 sha，使错误只暴露在结构层）
    bad3 = tmp_path / "bad3"
    shutil.copytree(good, bad3)
    conn = sqlite3.connect(bad3 / backup.DB_FILE)
    conn.execute("DROP TABLE speed_samples")
    conn.commit()
    conn.close()
    _recomplete(bad3)
    verdict3 = backup.verify_set(bad3)
    assert verdict3["ok"] is False
    assert any(e["code"] == "missing_tables" for e in verdict3["errors"])


# --- PT-04 --------------------------------------------------------------------


def test_pt04_quiesced_l2_detects_one_row_loss_and_names_table(tmp_path):
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("pre-migrate", src_db=src, backup_dir=tmp_path / "b",
                                stop_write=True, min_free_mb=0)
    set_dir = Path(result["path"])
    manifest = json.loads((set_dir / backup.MANIFEST_FILE).read_text(encoding="utf-8"))
    assert manifest["write_quiesced"] is True
    assert manifest["source_counts"]["settings"] == _fresh_db.last["settings"]
    assert backup.verify_set(set_dir)["ok"] is True

    conn = sqlite3.connect(set_dir / backup.DB_FILE)
    conn.execute("DELETE FROM settings WHERE key='k1'")
    conn.commit()
    conn.close()
    _recomplete(set_dir)

    verdict = backup.verify_set(set_dir)
    assert verdict["ok"] is False
    errors = [e for e in verdict["errors"] if e["code"] in ("count_mismatch", "source_count_mismatch")]
    assert errors and all("settings" in e["detail"] for e in errors)


# --- PT-04b -------------------------------------------------------------------


def test_pt04b_online_backup_never_compares_with_live_source(tmp_path):
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("daily", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    set_dir = Path(result["path"])

    conn = sqlite3.connect(src)
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES (?,?)",
        [("live1", "x"), ("live2", "y"), ("live3", "z")],
    )
    conn.commit()
    conn.close()

    verdict = backup.verify_set(set_dir, live_source=src)
    assert verdict["ok"] is True, "在线备份不得因实时源库多出 3 行而报警"
    assert verdict["l2"]["mode"] == "online"
    assert verdict["l2"]["compared_to_source"] is False
    assert verdict["l2"]["live_source_ignored"] is True

    strict = backup.verify_set(set_dir, require_source_compare=True)
    assert strict["ok"] is False
    assert any(e["code"] == "l2_reference_unavailable" for e in strict["errors"])


def test_pt04b_quiesced_backup_reports_live_source_divergence(tmp_path):
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("pre-migrate", src_db=src, backup_dir=tmp_path / "b",
                                stop_write=True, min_free_mb=0)
    set_dir = Path(result["path"])

    conn = sqlite3.connect(src)
    conn.executemany(
        "INSERT INTO settings(key, value) VALUES (?,?)",
        [("live1", "x"), ("live2", "y"), ("live3", "z")],
    )
    conn.commit()
    conn.close()

    verdict = backup.verify_set(set_dir, live_source=src)
    assert verdict["ok"] is False, "同一构造在停写场景必须报警"
    errs = [e for e in verdict["errors"] if e["code"] == "source_count_mismatch"]
    assert errs and all("settings" in e["detail"] for e in errs)


# --- PT-21 --------------------------------------------------------------------


def test_pt21_incomplete_sets_are_invalid_and_never_pruned(tmp_path):
    src = _fresh_db(tmp_path)
    bdir = tmp_path / "b"
    good = backup.snapshot_set("daily", src_db=src, backup_dir=bdir, min_free_mb=0)
    good_dir = Path(good["path"])

    # 只留 db
    only_db = bdir / "nodepool-20260101-000000-daily"
    only_db.mkdir()
    shutil.copyfile(good_dir / backup.DB_FILE, only_db / backup.DB_FILE)
    # 留 db + manifest，缺 complete
    no_complete = bdir / "nodepool-20260101-000001-daily"
    no_complete.mkdir()
    shutil.copyfile(good_dir / backup.DB_FILE, no_complete / backup.DB_FILE)
    shutil.copyfile(good_dir / backup.MANIFEST_FILE, no_complete / backup.MANIFEST_FILE)
    # complete.json 的 sha 与实文件不符
    bad_sha = bdir / "nodepool-20260101-000002-daily"
    shutil.copytree(good_dir, bad_sha)
    payload = json.loads((bad_sha / backup.COMPLETE_FILE).read_text(encoding="utf-8"))
    payload["db_sha256"] = "0" * 64
    (bad_sha / backup.COMPLETE_FILE).write_text(json.dumps(payload), encoding="utf-8")

    for path in (only_db, no_complete, bad_sha):
        verdict = backup.verify_set(path)
        assert verdict["ok"] is False, path.name
    assert any(e["code"] == "missing_file" for e in backup.verify_set(only_db)["errors"])
    assert any(e["code"] == "missing_file" for e in backup.verify_set(no_complete)["errors"])
    assert any(e["code"] == "db_sha_mismatch" for e in backup.verify_set(bad_sha)["errors"])

    names = {entry["name"] for entry in backup.valid_sets(bdir)}
    assert names == {good_dir.name}

    result = backup.prune(bdir, keep_days=0, keep_daily=0)
    assert set(result["skipped"]) >= {only_db.name, no_complete.name, bad_sha.name}
    assert all(path.exists() for path in (only_db, no_complete, bad_sha))


# --- latest 兼容副本 ------------------------------------------------------------


def test_latest_copy_only_updated_for_valid_set(tmp_path, monkeypatch):
    src = _fresh_db(tmp_path)
    bdir = tmp_path / "b"
    first = backup.snapshot_set("daily", src_db=src, backup_dir=bdir, min_free_mb=0)
    latest = bdir / backup.LATEST_FILE
    assert latest.exists()
    assert backup._sha256(latest) == backup._sha256(Path(first["path"]) / backup.DB_FILE)

    before_sha = backup._sha256(latest)

    def fake_verify(_path, **_kwargs):
        return {"ok": False, "errors": [{"layer": "l1", "code": "x", "detail": "injected"}]}

    monkeypatch.setattr(backup, "verify_set", fake_verify)
    with pytest.raises(backup.BackupError):
        backup.snapshot_set("daily", src_db=src, backup_dir=bdir, min_free_mb=0)
    monkeypatch.undo()

    assert backup._sha256(latest) == before_sha, "无效备份集不得更新 nodepool-latest.db"
    assert any(entry["state"] == "incomplete" for entry in backup.list_sets(bdir))


# --- 恢复 ---------------------------------------------------------------------


def test_restore_set_restores_content_and_publishes(tmp_path):
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    set_dir = Path(result["path"])
    manifest = json.loads((set_dir / backup.MANIFEST_FILE).read_text(encoding="utf-8"))

    target = tmp_path / "restore-target.db"
    shutil.copyfile(src, target)
    conn = sqlite3.connect(target)
    conn.execute("DELETE FROM settings")
    conn.commit()
    conn.close()

    calls: list[str] = []
    restored = backup.restore_set(
        set_dir, db_path=target, backup_dir=tmp_path / "b",
        publish=lambda kind: calls.append(kind) or {"ok": True},
    )
    assert restored["ok"] is True
    assert calls == ["restore"]
    assert restored["pre_restore_set"].endswith("pre-restore")

    conn = _open(target)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert _count(conn, "settings") == manifest["tables"]["settings"]
    finally:
        conn.close()
    wal = Path(str(target) + "-wal")
    if wal.exists():
        # init_db 以 WAL 模式重新打开库，允许出现新的空 -wal；不得残留旧库帧
        assert wal.stat().st_size == 0, "替换后不得残留旧库的 WAL 帧"
    leftovers = [p for p in target.parent.glob("restore-*.db") if p.name != target.name]
    assert leftovers == [], "临时恢复文件必须已被 os.replace 消费"


def test_restore_aborts_when_third_party_holds_the_file(tmp_path):
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    target = tmp_path / "held.db"
    shutil.copyfile(src, target)
    sha_before = backup._sha256(target)

    child = subprocess.Popen([
        sys.executable, "-c",
        "import sqlite3, time, sys\n"
        "c = sqlite3.connect(sys.argv[1])\n"
        "c.execute('SELECT COUNT(*) FROM settings').fetchone()\n"
        "time.sleep(60)\n",
        str(target),
    ])
    try:
        deadline = time.time() + 5
        while time.time() < deadline and not backup.holders_of(target):
            time.sleep(0.05)
        assert backup.holders_of(target), "测试进程应已持有库句柄"

        with pytest.raises(backup.BackupError) as exc:
            backup.restore_set(Path(result["path"]), db_path=target,
                               backup_dir=tmp_path / "b")
        assert exc.value.reason == "restore_handles_present"
        assert backup._sha256(target) == sha_before, "中止时不得改动任何文件"
    finally:
        child.kill()
        child.wait(timeout=5)


def test_restore_rolls_back_when_post_check_fails(tmp_path, monkeypatch):
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    target = tmp_path / "target.db"
    shutil.copyfile(src, target)

    # 让目标库与备份集内容不同，便于识别"是否真的换回去了"
    conn = sqlite3.connect(target)
    conn.execute("DELETE FROM settings WHERE key='k3'")
    conn.commit()
    conn.close()
    rows_before = _rows(sqlite3.connect(target), "settings")

    def boom(_target, _set_dir):
        raise RuntimeError("injected post-restore failure")

    monkeypatch.setattr(backup, "_post_restore_checks", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(Path(result["path"]), db_path=target,
                           backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_rolled_back"

    monkeypatch.undo()
    restored_rows = _rows(sqlite3.connect(target), "settings")
    assert restored_rows == rows_before, "回退后必须是 pre-restore 的内容"
    check = sqlite3.connect(target)
    try:
        assert check.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        check.close()


# --- 清理策略 ------------------------------------------------------------------


def test_prune_keeps_protected_and_recent_daily(tmp_path):
    src = _fresh_db(tmp_path)
    bdir = tmp_path / "b"
    for _ in range(3):
        backup.snapshot_set("daily", src_db=src, backup_dir=bdir, min_free_mb=0)
    backup.snapshot_set("pre-migrate", src_db=src, backup_dir=bdir, min_free_mb=0)
    backup.snapshot_set("pre-restore", src_db=src, backup_dir=bdir, min_free_mb=0)

    protected = [e for e in backup.list_sets(bdir) if e["protected"]]
    assert len(protected) == 2

    result = backup.prune(bdir, keep_days=0, keep_daily=2)
    assert all(Path(entry["path"]).exists() for entry in protected)
    remaining = backup.list_sets(bdir)
    daily_left = [e["name"] for e in remaining if e["stage"] == "daily"]
    assert len(daily_left) == 2
    assert len(result["removed"]) == 1


def test_structure_drift_in_backup_is_rejected(tmp_path):
    """备份库内业务表结构被改动（与 manifest 记录不一致）必须拒绝。"""
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    set_dir = Path(result["path"])
    manifest = json.loads((set_dir / "manifest.json").read_text())
    assert manifest["structure_profile"] in ("alter_history", "fresh_init")
    conn = sqlite3.connect(set_dir / "db")
    conn.execute('ALTER TABLE "airports" ADD COLUMN evil TEXT')
    conn.commit()
    conn.close()
    _resign(set_dir)
    verdict = backup.verify_set(set_dir)
    assert verdict["ok"] is False
    assert any(e["code"] in ("structure_drift", "structure_profile_unregistered")
               for e in verdict["errors"]), verdict["errors"]


def test_tasks_ddl_drift_in_backup_is_rejected(tmp_path):
    """`tasks` 定义摘要变化必须拒绝（PT-06d 双结构不得被随意改写）。"""
    src = _fresh_db(tmp_path)
    # 全新初始化库本不含 tasks；先建一张合法的 tasks 再快照，避免依赖外部建表
    conn = sqlite3.connect(src)
    conn.execute("CREATE TABLE IF NOT EXISTS tasks (id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()
    result = backup.snapshot_set("manual", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    set_dir = Path(result["path"])
    conn = sqlite3.connect(set_dir / "db")
    conn.execute("ALTER TABLE tasks ADD COLUMN evil TEXT")
    conn.commit()
    conn.close()
    _resign(set_dir)
    verdict = backup.verify_set(set_dir)
    assert verdict["ok"] is False
    assert any(e["code"] == "tasks_ddl_drift" for e in verdict["errors"]), verdict["errors"]


def test_published_set_contains_only_three_files(tmp_path):
    """发布后的集合目录只允许三件套；只读校验不得留下 -wal/-shm 残留。"""
    src = _fresh_db(tmp_path)
    result = backup.snapshot_set("daily", src_db=src, backup_dir=tmp_path / "b", min_free_mb=0)
    set_dir = Path(result["path"])
    assert sorted(p.name for p in set_dir.iterdir()) == ["complete.json", "db", "manifest.json"]
    assert backup.verify_set(set_dir)["ok"] is True
    assert sorted(p.name for p in set_dir.iterdir()) == ["complete.json", "db", "manifest.json"]


# --- P-1.3 修复：恢复失败安全（WAL 未 checkpoint 的原库绝不能被破坏） ----------


def _fresh_db_with_uncheckpointed_wal(tmp_path):
    """造一个"已提交数据只存在于 -wal 里"的库副本，用于验证失败路径不丢数据。"""
    src = _fresh_db(tmp_path)
    keeper = sqlite3.connect(src)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("PRAGMA wal_autocheckpoint=0")
    for i in range(5):
        keeper.execute(
            "INSERT INTO logs(ts, level, source, message) VALUES (?, 'INFO', 'wal', ?)",
            (f"2026-11-26 19:00:0{i}", f"wal-only-{i}"),
        )
    keeper.commit()
    wal = Path(str(src) + "-wal")
    assert wal.exists() and wal.stat().st_size > 0, "需要存在未 checkpoint 的 WAL 帧"

    target = tmp_path / "live.db"
    shutil.copyfile(src, target)
    shutil.copyfile(wal, str(target) + "-wal")
    shm = Path(str(src) + "-shm")
    if shm.exists():
        shutil.copyfile(shm, str(target) + "-shm")
    # 不带 -wal 的纯 db 副本必须看不到这 5 行（证明数据确实只在 WAL 里）；
    # 必须在 keeper 仍打开时复制，否则关闭会触发 checkpoint 把帧写回主库
    bare = tmp_path / "bare.db"
    shutil.copyfile(src, bare)
    keeper.close()
    # 只读打开：读写连接在关闭时会 checkpoint，把 WAL 帧合并回主库，破坏测试前提
    bare_conn = sqlite3.connect(f"file:{bare}?mode=ro", uri=True)
    full_conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    assert _count(bare_conn, "logs") < _count(full_conn, "logs"), "构造失败：数据未停留在 WAL"
    assert _count(full_conn, "logs") == _fresh_db.last["logs"] + 5
    bare_conn.close()
    full_conn.close()
    assert Path(str(target) + "-wal").stat().st_size > 0, "构造失败：WAL 帧已被 checkpoint"
    return target


def _side_state(db_path: Path) -> dict:
    """记录 db/-wal/-shm 的存在性、大小与 sha256，用于"完全不变"断言。"""
    state = {}
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(db_path) + suffix)
        # -shm 是 WAL 索引（可从 WAL 重建），字节不等价；db 与 -wal 必须字节不变
        digest = None
        if p.exists() and suffix != "-shm":
            digest = hashlib.sha256(p.read_bytes()).hexdigest()
        state[suffix or "db"] = (
            p.exists(),
            p.stat().st_size if p.exists() else None,
            digest,
        )
    return state


def test_restore_copy_failure_leaves_original_untouched(tmp_path, monkeypatch):
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    before = _side_state(target)

    real_copy = backup.shutil.copyfile

    def boom(src, dst, *a, **k):
        if Path(dst).name.startswith("restore-"):
            raise OSError("模拟复制失败")
        return real_copy(src, dst, *a, **k)

    monkeypatch.setattr(backup.shutil, "copyfile", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(Path(result["path"]), db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_stage_failed"
    assert _side_state(target) == before, "复制失败不得改动原库任何文件"
    assert _count(sqlite3.connect(target), "logs") == _fresh_db.last["logs"] + 5


def test_restore_fsync_failure_leaves_original_untouched(tmp_path, monkeypatch):
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    before = _side_state(target)

    real_fsync = backup._fsync_file

    def boom(path, *a, **k):
        if Path(path).name.startswith("restore-"):
            raise OSError("模拟 fsync 失败")
        return real_fsync(path, *a, **k)

    monkeypatch.setattr(backup, "_fsync_file", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(Path(result["path"]), db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_stage_failed"
    assert _side_state(target) == before, "fsync 失败不得改动原库任何文件"


def test_restore_staged_verify_failure_leaves_original_untouched(tmp_path, monkeypatch):
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    before = _side_state(target)

    def boom(*a, **k):
        raise backup.BackupError("restore_verify_failed", "模拟临时库完整性校验失败")

    monkeypatch.setattr(backup, "_verify_staged_db", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(Path(result["path"]), db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_verify_failed"
    assert _side_state(target) == before, "临时库校验失败不得改动原库任何文件"
    leftovers = [p.name for p in tmp_path.glob("restore-*.db")]
    assert leftovers == [], "失败的临时文件必须被清理"


def test_restore_replace_failure_preserves_original_wal(tmp_path, monkeypatch):
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    before = _side_state(target)

    real_replace = backup.os.replace

    def guarded(src, dst, *a, **k):
        if Path(src).name.startswith("restore-"):
            raise OSError("模拟 os.replace 失败")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(Path(result["path"]), db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_replace_failed"
    assert _side_state(target) == before, "替换失败必须原样还原 db 与 -wal/-shm"
    assert _count(sqlite3.connect(target), "logs") == _fresh_db.last["logs"] + 5


def test_post_restore_check_failure_restores_original_with_wal(tmp_path, monkeypatch):
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    before = _side_state(target)

    def boom(*a, **k):
        raise RuntimeError("模拟恢复后校验失败")

    monkeypatch.setattr(backup, "_post_restore_checks", boom)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(Path(result["path"]), db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_rolled_back"
    assert _side_state(target) == before, "回退后必须回到原库（含未 checkpoint 的 WAL）"
    assert _count(sqlite3.connect(target), "logs") == _fresh_db.last["logs"] + 5


def test_restore_normal_path_on_wal_database_keeps_wal_rows(tmp_path):
    """补齐：正常恢复同样以"存在未 checkpoint WAL"的库为目标。"""
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    set_dir = Path(result["path"])
    replaced = backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert replaced["ok"] is True
    conn = sqlite3.connect(target)
    assert _count(conn, "logs") == _fresh_db.last["logs"] + 5
    manifest = json.loads((set_dir / "manifest.json").read_text())
    for table, expected in manifest["tables"].items():
        assert _count(conn, table) == expected
    conn.close()


# --- P-1.3 局部修复：隔离中途失败的回滚安全 -----------------------------------


def _count_ro(db_path, table="logs"):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return _count(conn, table)
    finally:
        conn.close()


def _wal_target_and_set(tmp_path):
    target = _fresh_db_with_uncheckpointed_wal(tmp_path)
    result = backup.snapshot_set("manual", src_db=target, backup_dir=tmp_path / "b", min_free_mb=0)
    return target, Path(result["path"])


def _install_replace_failure(monkeypatch, fail_sidecar):
    """让隔离改名在指定 sidecar 上失败；还原方向（隔离副本→原名）保持可用。"""
    real = backup.os.replace

    def guarded(src, dst, *a, **k):
        dst_name = Path(dst).name
        if dst_name.endswith(f"{fail_sidecar}.restore-prev-" + dst_name.rsplit("restore-prev-", 1)[-1]):
            raise OSError(f"inject: quarantine {fail_sidecar} 失败")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded)


def test_quarantine_fails_on_wal_keeps_original_db_and_sidecars(tmp_path, monkeypatch):
    """DB 改名成功、WAL 改名失败：只还原 DB，WAL/SHM 原样未动，不误删。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    before = _side_state(target)
    expected_rows = _fresh_db.last["logs"] + 5

    real = backup.os.replace

    def guarded(src, dst, *a, **k):
        name = Path(dst).name
        if name.endswith("-wal") or "-wal.restore-prev-" in name:
            if ".restore-prev-" in name:
                raise OSError("inject: WAL 改名失败")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_quarantine_failed"

    after = _side_state(target)
    assert after["db"] == before["db"], "DB 必须字节不变（改名后被还原）"
    assert after["-wal"] == before["-wal"], "WAL 必须字节不变（尚未隔离，绝不能删）"
    assert after["-shm"] == before["-shm"], "SHM 必须未被误删"
    assert _count_ro(target) == expected_rows, "WAL 中的已提交数据必须仍可读"
    assert [p.name for p in tmp_path.glob("*.restore-prev-*")] == [], "还原成功的隔离副本不得残留"


def test_quarantine_fails_on_shm_keeps_wal_data_readable(tmp_path, monkeypatch):
    """DB、WAL 改名成功、SHM 改名失败：db/wal 还原，shm 原样未动。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    before = _side_state(target)
    expected_rows = _fresh_db.last["logs"] + 5
    real = backup.os.replace

    def guarded(src, dst, *a, **k):
        name = Path(dst).name
        if "-shm.restore-prev-" in name:
            raise OSError("inject: SHM 改名失败")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_quarantine_failed"

    after = _side_state(target)
    assert after["db"] == before["db"] and after["-wal"] == before["-wal"]
    assert after["-shm"] == before["-shm"], "尚未隔离的 SHM 不得被删除"
    assert _count_ro(target) == expected_rows
    assert [p.name for p in tmp_path.glob("*.restore-prev-*")] == []


def test_quarantine_rollback_failure_keeps_quarantine_copy(tmp_path, monkeypatch):
    """部分隔离后回滚改名再次失败：保留隔离副本，不静默删除，错误里能定位文件。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    before = _side_state(target)
    real = backup.os.replace

    def guarded(src, dst, *a, **k):
        name = Path(dst).name
        src_name = Path(src).name
        if "-wal.restore-prev-" in name:
            raise OSError("inject: WAL 改名失败")
        # 还原方向：隔离副本 → 原名，对 db 的还原再次失败
        if ".restore-prev-" in src_name and "-wal.restore-prev-" not in src_name \
                and "-shm.restore-prev-" not in src_name:
            raise OSError("inject: DB 还原失败")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_quarantine_failed"
    message = str(exc.value)
    assert ".restore-prev-" in message, "错误信息必须列出残留隔离副本路径"

    leftovers = sorted(p.name for p in tmp_path.glob("*.restore-prev-*"))
    assert len(leftovers) == 1
    assert leftovers[0].startswith("live.db.restore-prev-"), "残留的应是原 DB 的隔离副本"
    qpath = tmp_path / leftovers[0]
    assert qpath.exists(), "未能还原的隔离副本必须保留，不得静默删除"
    assert hashlib.sha256(qpath.read_bytes()).hexdigest() == before["db"][2], "保留的隔离副本必须是原始 DB 内容"
    # 尚未隔离的 sidecar 仍是原文件，内容不变
    assert _side_state(target)["-wal"] == before["-wal"]
    assert _side_state(target)["-shm"] == before["-shm"]
    # 不产生无法识别的半库：原路径上不得出现新的 db 文件（还原失败，未做替换）
    assert not Path(target).exists(), "未替换前原路径不应出现新库文件"


# --- P-1.3 最后局部修复：隔离副本缺失与目录持久化 ------------------------------


def test_missing_quarantine_copy_reports_leftover_not_success(tmp_path, monkeypatch):
    """隔离副本在回滚前丢失：必须报告缺失，绝不当成还原成功。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    before = _side_state(target)
    real = backup.os.replace

    def guarded(src, dst, *a, **k):
        name = Path(dst).name
        if "-wal.restore-prev-" in name:
            raise OSError("inject: WAL 改名失败")
        real(src, dst, *a, **k)
        # DB 改名成功后立刻删掉隔离副本，模拟副本丢失
        if name.startswith("live.db.restore-prev-") and "-wal.restore-prev-" not in name:
            Path(dst).unlink()

    monkeypatch.setattr(backup.os, "replace", guarded)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_quarantine_failed"
    message = str(exc.value)
    assert "隔离副本缺失" in message, f"必须明确报告缺失：{message}"
    assert ".restore-prev-" in message, "错误信息必须给出缺失副本路径"
    # 原路径没有 db（未被误报为已还原），WAL/SHM 原样未动
    assert not Path(target).exists(), "不得宣称已还原（原路径不应出现 db）"
    assert _side_state(target)["-wal"] == before["-wal"]
    assert _side_state(target)["-shm"] == before["-shm"]


def test_restore_rename_failure_reports_full_path(tmp_path, monkeypatch):
    """隔离副本改名还原失败：保留副本并报告完整路径。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    before = _side_state(target)
    real = backup.os.replace

    def guarded(src, dst, *a, **k):
        dn, sn = Path(dst).name, Path(src).name
        if "-wal.restore-prev-" in dn:
            raise OSError("inject: WAL 改名失败")
        if ".restore-prev-" in sn and "-wal.restore-prev-" not in sn and "-shm.restore-prev-" not in sn:
            raise OSError("inject: DB 还原失败")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_quarantine_failed"
    message = str(exc.value)
    assert "改名还原失败" in message and str(tmp_path) in message, message
    kept = [p for p in tmp_path.glob("*.restore-prev-*")]
    assert kept, "隔离副本必须保留"
    assert hashlib.sha256(kept[0].read_bytes()).hexdigest() == before["db"][2]
    assert _side_state(target)["-wal"] == before["-wal"]


def test_fsync_failure_reports_persistence_unconfirmed(tmp_path, monkeypatch):
    """还原后目录 fsync 失败：必须报告持久化未确认，不得静默成功。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    before = _side_state(target)
    real_replace = backup.os.replace
    real_fsync_dir = backup._fsync_dir

    def guarded_replace(src, dst, *a, **k):
        if "-wal.restore-prev-" in Path(dst).name:
            raise OSError("inject: WAL 改名失败")
        return real_replace(src, dst, *a, **k)

    def failing_fsync_dir(path, *a, **k):
        # 只在目标库所在目录注入失败，避免影响 pre-restore 快照发布
        if Path(path) == tmp_path:
            raise OSError("inject: fsync dir 失败")
        return real_fsync_dir(path, *a, **k)

    monkeypatch.setattr(backup.os, "replace", guarded_replace)
    monkeypatch.setattr(backup, "_fsync_dir", failing_fsync_dir)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")
    assert exc.value.reason == "restore_quarantine_failed"
    message = str(exc.value)
    assert "fsync" in message and "持久化未确认" in message, message
    assert str(tmp_path) in message, "错误信息必须包含相关目录"
    # DB 内容已改名还原（字节与原始一致），但整体不得被称为成功
    assert _side_state(target)["db"] == before["db"]
    assert _side_state(target)["-wal"] == before["-wal"]


# --- P-1.3 局部修复：隔离副本预检必须在清理目标文件之前 ------------------------


def test_preflight_missing_copy_blocks_target_cleanup(tmp_path):
    """remove_new=True 场景：任一隔离副本缺失时，目标路径上的新库文件一个字节都不许动。"""
    target = tmp_path / "live.db"
    target.write_bytes(b"NEW-DB")
    Path(str(target) + "-wal").write_bytes(b"NEW-WAL")
    Path(str(target) + "-shm").write_bytes(b"NEW-SHM")

    q_db = Path(str(target) + ".restore-prev-t1")
    q_wal = Path(str(target) + "-wal.restore-prev-t1")
    q_db.write_bytes(b"OLD-DB")
    q_wal.write_bytes(b"OLD-WAL")
    q_shm_missing = Path(str(target) + "-shm.restore-prev-t1")  # 故意不创建

    pairs = [(target, q_db), (Path(str(target) + "-wal"), q_wal), (Path(str(target) + "-shm"), q_shm_missing)]
    outcome = backup._restore_moved(pairs, remove_new=True)

    assert outcome.ok() is False
    assert [p.name for p in outcome.missing] == [q_shm_missing.name]
    # 目标文件完全不变（未被删除或覆盖）
    assert target.read_bytes() == b"NEW-DB"
    assert Path(str(target) + "-wal").read_bytes() == b"NEW-WAL"
    assert Path(str(target) + "-shm").read_bytes() == b"NEW-SHM"
    # 已存在的隔离副本也未被提前清理
    assert q_db.exists() and q_wal.exists()


def test_preflight_missing_db_copy_keeps_replaced_library_readable(tmp_path, monkeypatch):
    """集成：替换成功、恢复后校验失败，进入 _unquarantine 前删掉 DB 隔离副本。"""
    target, set_dir = _wal_target_and_set(tmp_path)
    manifest = json.loads((set_dir / "manifest.json").read_text())
    new_db_sha = hashlib.sha256((set_dir / "db").read_bytes()).hexdigest()

    def boom(*a, **k):
        raise RuntimeError("inject: post-check 失败")

    monkeypatch.setattr(backup, "_post_restore_checks", boom)

    real_unquarantine = backup._unquarantine
    seen: dict[str, Path] = {}

    def wrapper(pairs):
        db_dst = next(dst for src, dst in pairs if not str(src).endswith(("-wal", "-shm")))
        wal_dst = next(dst for src, dst in pairs if str(src).endswith("-wal"))
        shm_dst = next(dst for src, dst in pairs if str(src).endswith("-shm"))
        seen["db"], seen["wal"], seen["shm"] = db_dst, wal_dst, shm_dst
        db_dst.unlink()          # 在 _unquarantine 开始前丢失 DB 隔离副本
        return real_unquarantine(pairs)

    monkeypatch.setattr(backup, "_unquarantine", wrapper)
    with pytest.raises(backup.BackupError) as exc:
        backup.restore_set(set_dir, db_path=target, backup_dir=tmp_path / "b")

    assert exc.value.reason == "restore_rolled_back_leftover"
    message = str(exc.value)
    assert "隔离副本缺失" in message, message
    assert str(seen["db"]) in message, "错误信息必须包含缺失隔离副本的完整路径"

    # 目标路径上当前的新库 DB 未被删除或覆盖，仍等于备份集内容且可读
    assert Path(target).exists(), "预检失败不得删除目标路径的新库 DB"
    assert hashlib.sha256(Path(target).read_bytes()).hexdigest() == new_db_sha
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _count(conn, "logs") == manifest["tables"]["logs"]
    conn.close()

    # 其他目标文件（WAL/SHM 的隔离副本）未被提前清理
    assert seen["wal"].exists() and seen["shm"].exists()
