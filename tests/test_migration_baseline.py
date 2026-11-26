"""P-1.2：schema_migrations 基线的验收测试（PT-06 / PT-06b / PT-06c / PT-06d / PT-06e）。

所有测试都在**副本库**上执行：先经 `sqlite3.backup()` 从开发库复制（WAL 安全），
再在副本上做建表、注入与故障模拟，绝不触碰 `data/nodepool.db` 与生产库。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from backend import config
from backend.database import migrate

REPO = Path(__file__).resolve().parents[1]


# --- helpers ----------------------------------------------------------------------------


def _copy_db(tmp_path: Path, name: str = "probe.db") -> Path:
    """WAL 安全复制：只用 sqlite3.backup()，绝不 shutil.copyfile。"""
    target = tmp_path / name
    src = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    dst = sqlite3.connect(target, isolation_level=None)
    src.backup(dst)
    dst.close()
    src.close()
    return target


def _fresh_db(tmp_path: Path, init_times: int = 1) -> Path:
    """在独立子进程中跑 N 次 init_db()，得到"全新部署"的库。"""
    db_path = tmp_path / "fresh.db"
    env = os.environ.copy()
    env["NODEPOOL_DATA_DIR"] = str(tmp_path)
    env["NODEPOOL_DB"] = str(db_path)
    code = (
        "from backend.database import db\n"
        f"for _ in range({init_times}):\n"
        "    db.init_db()\n"
        "db.reset_connection()\n"
    )
    subprocess.run([sys.executable, "-c", code], cwd=str(REPO), env=env, check=True,
                   capture_output=True)
    return db_path


def _business_fingerprint(path: Path) -> dict:
    """17 张基准业务表的结构摘要 + 行数 + 内容摘要（用于证明零变化）。"""
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        sig = migrate.structure_signature(conn)
        result = {
            "structure_hash": migrate.structure_hash(conn),
            "counts": {},
            "content": {},
        }
        for table in migrate.BASE_TABLES:
            result["counts"][table] = int(
                conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            rows = conn.execute(f'SELECT * FROM "{table}" ORDER BY rowid').fetchall()
            result["content"][table] = hashlib.sha256(
                json.dumps([list(row) for row in rows], default=str).encode()
            ).hexdigest()
        result["objects"] = sorted(sig["objects"])
        return result
    finally:
        conn.close()


def _seed_business_rows(conn: sqlite3.Connection) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES ('pt06_seed', 'keep-me')")
    conn.execute(
        "INSERT INTO logs(ts, level, source, message) VALUES ('2026-11-26 14:00:00','INFO','pt06','seed')"
    )


# --- PT-06 ---------------------------------------------------------------------


def test_pt06_baseline_idempotent_and_business_tables_unchanged(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        _seed_business_rows(conn)
        before = _business_fingerprint(path)
        assert before["structure_hash"] == migrate.BASELINE_STRUCTURE_HASH

        first = migrate.record_baseline(
            conn,
            image_digest="sha256:ca97a56640ad5111449bd09e7d7f81cde570f7ac1ac14df588ea8a6f3dfb76d1",
            healthz_version="1.8.7",
        )
        assert first["status"] == "created"
        assert first["version"] == migrate.BASELINE_VERSION == "1.8.7-baseline"
        assert first["seq"] == 1
        assert first["tasks_present"] is True and first["tasks_rows"] == 0

        second = migrate.record_baseline(conn)
        assert second["status"] == "already_present"
        assert second["seq"] == 1

        assert migrate.schema_table_exists(conn) is True
        assert len(migrate.migration_rows(conn)) == 1
        assert migrate.current_version(conn) == "1.8.7-baseline"

        detail = json.loads(migrate.migration_rows(conn)[0]["detail_json"])
        assert detail["baseline"] is True
        assert detail["structure_hash"] == migrate.BASELINE_STRUCTURE_HASH
        assert detail["app_version"] == "1.8.7"
        assert detail["healthz_version"] == "1.8.7"
        assert detail["image_digest"].startswith("sha256:")
        assert detail["tasks_present"] is True
        assert detail["tables"] == list(migrate.BASE_TABLES)
        assert detail["tables_count"] == 17

        after = _business_fingerprint(path)
        assert after == before, "17 张基准业务表的结构与数据必须零变化"
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()


# --- PT-06b --------------------------------------------------------------------


def test_pt06b_version_ordering_uses_seq_not_string(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        migrate.record_baseline(conn)

        # 模拟后续版本：1.9 的 seq 小于 1.10，但字符串比较恰好相反
        for version, seq in (("1.9", 10), ("1.10", 11)):
            conn.execute("BEGIN IMMEDIATE")
            migrate._insert_row(
                conn,
                migration_id=f"{version}-test",
                seq=seq,
                version=version,
                status="success",
                started_at="2026-11-26 14:00:00",
                finished_at="2026-11-26 14:00:01",
                detail_json=None,
            )
            conn.execute("COMMIT")

        assert migrate.current_version(conn) == "1.10"
        string_max = conn.execute(
            "SELECT MAX(version) FROM schema_migrations WHERE status='success'"
        ).fetchone()[0]
        assert string_max == "1.9", "字符串 MAX 会错序，用于证明 seq 排序的必要性"
        assert migrate.current_version(conn) != string_max
        assert [row["seq"] for row in migrate.migration_rows(conn)] == [1, 10, 11]
        assert migrate.VERSION_SEQ["1.8.7-baseline"] == 1
        assert migrate.VERSION_SEQ["1.10"] == 2
    finally:
        conn.close()


# --- PT-06c --------------------------------------------------------------------


def test_pt06c_create_and_insert_are_atomic_on_insert_failure(tmp_path, monkeypatch):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        before = _business_fingerprint(path)

        def boom(*args, **kwargs):
            raise RuntimeError("injected insert failure")

        monkeypatch.setattr(migrate, "_insert_row", boom)
        with pytest.raises(RuntimeError, match="injected insert failure"):
            migrate.record_baseline(conn)

        assert migrate.schema_table_exists(conn) is False, "回滚后不得留下空表"
        assert _business_fingerprint(path) == before
    finally:
        conn.close()


def test_pt06c_count_verification_failure_rolls_back(tmp_path, monkeypatch):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        real_count = migrate._count_rows
        state = {"calls": 0}

        def flaky(connection):
            state["calls"] += 1
            if state["calls"] == 1:
                return 0  # 让流程进入"插入基线"分支
            raise RuntimeError("injected count verification failure")

        monkeypatch.setattr(migrate, "_count_rows", flaky)
        with pytest.raises(RuntimeError, match="count verification failure"):
            migrate.record_baseline(conn)
        monkeypatch.setattr(migrate, "_count_rows", real_count)

        assert migrate.schema_table_exists(conn) is False
        assert state["calls"] == 2
    finally:
        conn.close()


# --- PT-06d --------------------------------------------------------------------


def test_pt06d_tasks_absent_and_empty_present_share_same_hash(tmp_path):
    # (a) tasks 存在且为空
    path = _copy_db(tmp_path, "with_tasks.db")
    conn = migrate.open_connection(path)
    try:
        assert migrate.tasks_state(conn)["tasks_present"] is True
        result = migrate.record_baseline(conn)
        assert result["tasks_present"] is True
        assert result["tasks_rows"] == 0
        assert result["structure_hash"] == migrate.BASELINE_STRUCTURE_HASH
        with_tasks_hash = result["structure_hash"]
    finally:
        conn.close()

    # (b) tasks 不存在
    path2 = _copy_db(tmp_path, "no_tasks.db")
    conn = migrate.open_connection(path2)
    try:
        conn.execute("DROP TABLE tasks")
        assert migrate.tasks_state(conn)["tasks_present"] is False
        result = migrate.record_baseline(conn)
        assert result["tasks_present"] is False
        assert result["structure_hash"] == with_tasks_hash, \
            "tasks 是否存在于主摘要之外，两种形态必须得到同一 structure_hash"
        detail = json.loads(migrate.migration_rows(conn)[0]["detail_json"])
        assert detail["tasks_present"] is False
        assert detail["tasks_hash"] is None
    finally:
        conn.close()


def test_pt06d_tasks_present_but_not_empty_is_refused(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        conn.execute(
            "INSERT INTO tasks(name, status, started_at) VALUES ('legacy','done','2026-11-01 00:00:00')"
        )
        with pytest.raises(migrate.BaselineRefused) as exc:
            migrate.record_baseline(conn)
        assert exc.value.reason == "tasks_not_empty"
        assert migrate.schema_table_exists(conn) is False
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1, "拒绝登记不得动 tasks"
    finally:
        conn.close()


def test_pt06d_structure_drift_is_refused(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        conn.execute("CREATE TABLE intruder (id INTEGER PRIMARY KEY, v TEXT)")
        with pytest.raises(migrate.BaselineRefused) as exc:
            migrate.record_baseline(conn)
        assert exc.value.reason == "structure_mismatch"
        assert "actual=" in str(exc.value)
        assert migrate.schema_table_exists(conn) is False
    finally:
        conn.close()


def test_pt06d_missing_base_table_is_refused(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        conn.execute("DROP TABLE speed_samples")
        with pytest.raises(migrate.BaselineRefused) as exc:
            migrate.record_baseline(conn)
        assert exc.value.reason == "missing_base_tables"
        assert "speed_samples" in str(exc.value)
    finally:
        conn.close()


def test_pt06d_hash_ignores_alter_history_and_column_order(tmp_path):
    """历史 ALTER 出来的库与全新 init_db 的库，结构摘要必须相同。"""
    fresh = _fresh_db(tmp_path)
    fresh_conn = migrate.open_connection(fresh)
    legacy_conn = migrate.open_connection(_copy_db(tmp_path, "legacy.db"))
    try:
        assert migrate.structure_hash(fresh_conn) == migrate.structure_hash(legacy_conn)
        assert migrate.structure_hash(fresh_conn) == migrate.BASELINE_STRUCTURE_HASH
        assert len(migrate.structure_signature(fresh_conn)["tables"]) == 17
        assert len(migrate.structure_signature(fresh_conn)["objects"]) == 13
    finally:
        fresh_conn.close()
        legacy_conn.close()


# --- PT-06e --------------------------------------------------------------------


def test_pt06e_init_db_never_creates_schema_migrations(tmp_path):
    path = _fresh_db(tmp_path, init_times=3)
    conn = migrate.open_connection(path)
    try:
        assert migrate.schema_table_exists(conn) is False
        assert migrate.current_version(conn) is None
        assert migrate.migration_rows(conn) == []

        verdict = migrate.preflight(conn)
        assert verdict["state"] == "absent"
        assert verdict["allowed"] is True
        assert verdict["action"] == "create_table_and_record_baseline"
        assert migrate.schema_table_exists(conn) is False, "preflight 必须只读"

        before = _business_fingerprint(path)
        result = migrate.record_baseline(conn)
        assert result["status"] == "created"
        assert len(migrate.migration_rows(conn)) == 1
        assert migrate.current_version(conn) == "1.8.7-baseline"
    finally:
        conn.close()

    # 再跑两次 init_db：迁移表必须原样保留，且不被普通初始化触碰
    subprocess.run(
        [sys.executable, "-c",
         "from backend.database import db\nfor _ in range(2):\n    db.init_db()\ndb.reset_connection()\n"],
        cwd=str(REPO),
        env={**os.environ,
             "NODEPOOL_DATA_DIR": str(tmp_path),
             "NODEPOOL_DB": str(path)},
        check=True, capture_output=True,
    )
    conn = migrate.open_connection(path)
    try:
        rows = migrate.migration_rows(conn)
        assert len(rows) == 1 and rows[0]["version"] == "1.8.7-baseline"
        after = _business_fingerprint(path)
        assert after["counts"] == before["counts"]
        assert after["structure_hash"] == before["structure_hash"]
        assert migrate.preflight(conn)["state"] == "ready"
    finally:
        conn.close()


# --- preflight 判定矩阵 ---------------------------------------------------------

def test_preflight_matrix_empty_incomplete_and_mismatch(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        # 表存在但 0 行
        conn.execute(migrate.SCHEMA_MIGRATIONS_DDL)
        verdict = migrate.preflight(conn)
        assert verdict["state"] == "empty" and verdict["allowed"] is False
        assert verdict["reason"] == "migration_table_empty"

        # 存在 running 行 ⇒ 未完成，要求进入恢复流程
        conn.execute("BEGIN IMMEDIATE")
        migrate._insert_row(
            conn, migration_id="1.10", seq=2, version="1.10", status="running",
            started_at="2026-11-26 14:00:00", finished_at=None, detail_json=None,
        )
        conn.execute("COMMIT")
        verdict = migrate.preflight(conn)
        assert verdict["state"] == "incomplete" and verdict["allowed"] is False
        assert verdict["pending"] == ["1.10"]

        # 只有 success 但版本与运行版本不一致 ⇒ version_mismatch
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("BEGIN IMMEDIATE")
        migrate._insert_row(
            conn, migration_id="1.10", seq=2, version="1.10", status="success",
            started_at="2026-11-26 14:00:00", finished_at="2026-11-26 14:00:01", detail_json=None,
        )
        conn.execute("COMMIT")
        verdict = migrate.preflight(conn)
        assert verdict["state"] == "version_mismatch" and verdict["allowed"] is False
        assert verdict["current_version"] == "1.10"
        assert verdict["expected"] == "1.8.7-baseline"

        # 表存在且恰为基线 ⇒ ready
        conn.execute("DELETE FROM schema_migrations")
        conn.execute("BEGIN IMMEDIATE")
        migrate._insert_row(
            conn, migration_id="1.8.7-baseline", seq=1, version="1.8.7-baseline",
            status="success", started_at="2026-11-26 14:00:00",
            finished_at="2026-11-26 14:00:01", detail_json=None,
        )
        conn.execute("COMMIT")
        verdict = migrate.preflight(conn)
        assert verdict["state"] == "ready" and verdict["allowed"] is True
    finally:
        conn.close()


def test_preflight_and_record_baseline_refuse_on_version_mismatch(tmp_path):
    path = _copy_db(tmp_path)
    conn = migrate.open_connection(path)
    try:
        with pytest.raises(migrate.BaselineRefused) as exc:
            migrate.record_baseline(conn, runtime_version="1.9.0", app_version="1.9.0")
        assert exc.value.reason == "unsupported_runtime_version"
        assert migrate.schema_table_exists(conn) is False

        with pytest.raises(migrate.BaselineRefused) as exc:
            migrate.record_baseline(conn, healthz_version="1.10")
        assert exc.value.reason == "version_mismatch"
    finally:
        conn.close()
