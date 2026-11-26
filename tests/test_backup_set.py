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
