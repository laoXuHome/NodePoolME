"""P-1.4 维护状态文件与 STATE_DIR 验收测试：PT-11 / PT-14 / PT-14b / PT-24。

全部在临时目录与一次性库上执行；不接触仓库开发实例的 STATE_DIR、不接触生产库、
不在 NAS 上跑自检。跨设备用例使用 /dev/shm（tmpfs）与临时目录（overlay）两套
真实文件系统，拿不到第二个文件系统时 skip 并注明原因。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import config
from backend.database import maint, migrate

REPO = Path(__file__).resolve().parents[1]

OTHER_FS = Path("/dev/shm")


# --- fixtures / helpers -------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    """把 maint 的 STATE_DIR / MAINT_FILE 与 config.DB_PATH 指到临时目录。"""
    state = tmp_path / "state"
    state.mkdir()
    db_path = tmp_path / "nodepool.db"
    monkeypatch.setattr(maint, "STATE_DIR", state)
    monkeypatch.setattr(maint, "MAINT_FILE", state / maint.MAINT_FILENAME)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    maint.reset_state_dir_selfcheck()
    yield SimpleNamespace(state=state, db=db_path, maint_file=state / maint.MAINT_FILENAME)
    maint.reset_state_dir_selfcheck()


@pytest.fixture
def query_counter(monkeypatch):
    """统计 migration_incomplete() 的调用次数（= 查库次数）。"""
    calls = {"n": 0}
    real = maint.migration_incomplete

    def wrapped():
        calls["n"] += 1
        return real()

    monkeypatch.setattr(maint, "migration_incomplete", wrapped)
    return calls


def _make_db(path: Path, statuses: tuple[str, ...] = ()) -> Path:
    """建一个只含 schema_migrations 的库；statuses 为空则不建表。"""
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            candidate.unlink()
    conn = sqlite3.connect(path)
    try:
        if statuses:
            conn.executescript(migrate.SCHEMA_MIGRATIONS_DDL)
            for idx, status in enumerate(statuses, start=1):
                conn.execute(
                    "INSERT INTO schema_migrations (migration_id, seq, version, status,"
                    " started_at) VALUES (?,?,?,?,?)",
                    (f"m{idx}", idx, "1.8.7-baseline", status, "2026-11-26T00:00:00Z"),
                )
            conn.commit()
    finally:
        conn.close()
    return path


def _valid_blob(maintenance: bool = True, **over):
    blob = {
        "maintenance": maintenance,
        "migration_id": "mig-1",
        "phase": "PREPARED",
        "since": 1700000000000,
        "nonce": "nonce-1",
        "reason": "test",
        "pid": 1,
    }
    blob.update(over)
    return blob


def _write_raw(maint_file: Path, text: str) -> None:
    maint_file.write_text(text, encoding="utf-8")


def _write_blob(maint_file: Path, blob) -> None:
    maint_file.write_text(json.dumps(blob, ensure_ascii=False), encoding="utf-8")


def _fill(state: Path, maintenance: bool = True) -> dict:
    """真实写入一份 maintenance 文件（用于目录/探针类用例）。"""
    blob = _valid_blob(maintenance=maintenance,
                       since=int(time.time() * 1000), nonce="fill-nonce")
    (state / maint.MAINT_FILENAME).write_text(
        json.dumps(blob, ensure_ascii=False), encoding="utf-8")
    return blob


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- PT-11：四态读取 + 三态判定 + 查库次数 ------------------------------------


def test_pt11_present_true_is_active_without_db_query(env, query_counter):
    _make_db(env.db, ("success",))
    _write_blob(env.maint_file, _valid_blob(True))
    assert maint.read_maint_file().status is maint.MaintRead.PRESENT
    assert maint.maintenance_state() is maint.MaintState.ACTIVE
    assert query_counter["n"] == 0


def test_pt11_present_false_defers_to_db(env, query_counter):
    _make_db(env.db, ("success",))
    _write_blob(env.maint_file, _valid_blob(False))
    assert maint.maintenance_state() is maint.MaintState.INACTIVE
    assert query_counter["n"] == 1

    _make_db(env.db, ("success", "running"))
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 2        # 第二次判定再查一次，累计 2


def test_pt11_present_false_with_failed_row_is_recovery(env, query_counter):
    _make_db(env.db, ("success", "failed"))
    _write_blob(env.maint_file, _valid_blob(False))
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 1


@pytest.mark.parametrize("bad_value", ["false", "true", 0, 1, None, "0", "1"])
def test_pt11_non_boolean_maintenance_is_recovery_without_query(env, query_counter, bad_value):
    _make_db(env.db, ("success",))
    blob = _valid_blob(True)
    if bad_value is None:
        blob.pop("maintenance")            # 键缺失同样是非法
    else:
        blob["maintenance"] = bad_value
    _write_blob(env.maint_file, blob)
    assert maint.read_maint_file().status is maint.MaintRead.PRESENT
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 0


@pytest.mark.parametrize("flag", [True, False])
@pytest.mark.parametrize(
    "field,value",
    [
        ("migration_id", ""),
        ("migration_id", 42),
        ("since", "1700000000000"),
        ("since", True),
        ("since", None),
        ("since", 0),
        ("since", -1),
        ("since", -10 ** 1000),
        ("phase", 7),
        ("phase", "STAGE"),
    ],
)
def test_pt11_structure_violation_blocks_both_branches(env, query_counter, flag, field, value):
    """true 与 false 两套各跑一遍：结构失败必须 RECOVERY_REQUIRED 且不查库。"""
    _make_db(env.db, ("success",))
    blob = _valid_blob(flag)
    blob[field] = value
    _write_blob(env.maint_file, blob)
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 0


@pytest.mark.parametrize("raw_since", ["NaN", "Infinity", "-Infinity"])
@pytest.mark.parametrize("flag", [True, False])
def test_pt11_non_finite_since_is_recovery(env, query_counter, raw_since, flag):
    _make_db(env.db, ("success",))
    text = json.dumps(_valid_blob(flag)).replace("1700000000000", raw_since)
    _write_raw(env.maint_file, text)
    assert maint.read_maint_file().status is maint.MaintRead.PRESENT
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 0


def test_pt11_huge_int_since_is_structurally_valid(env, query_counter):
    """10**1000：int 只判 >0，不调 isfinite，不得 OverflowError。"""
    _make_db(env.db, ("success",))
    big = 10 ** 1000

    _write_blob(env.maint_file, _valid_blob(True, since=big))
    assert maint.since_sane(big) is True
    assert maint.maintenance_state() is maint.MaintState.ACTIVE
    assert query_counter["n"] == 0

    _write_blob(env.maint_file, _valid_blob(False, since=big))
    assert maint.maintenance_state() is maint.MaintState.INACTIVE
    assert query_counter["n"] == 1

    _write_blob(env.maint_file, _valid_blob(False, since=big, migration_id=""))
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 1        # 结构失败后不再查库（计数未增加）


@pytest.mark.parametrize("payload", [
    b'{"maintenance":tr',                                  # 截断
    b"\xff\xfe\x00not-utf8",                               # 非 UTF-8
    b"[]",                                                 # 顶层数组
    b'"x"',                                                # 顶层字符串
    b"42",                                                 # 顶层数字
    b"null",                                               # 顶层 null
])
def test_pt11_corrupt_is_recovery_without_query(env, query_counter, payload):
    _make_db(env.db, ("success",))
    env.maint_file.write_bytes(payload)
    rd = maint.read_maint_file()
    assert rd.status is maint.MaintRead.CORRUPT
    assert rd.status is not maint.MaintRead.ABSENT
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 0


def test_pt11_permission_error_is_unreadable_not_absent(env, query_counter, monkeypatch):
    _make_db(env.db, ("success",))
    _write_blob(env.maint_file, _valid_blob(True))

    def boom(_target):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(maint, "_read_maint_bytes", boom)
    rd = maint.read_maint_file()
    assert rd.status is maint.MaintRead.UNREADABLE
    assert rd.status is not maint.MaintRead.ABSENT
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 0


def test_pt11_is_a_directory_is_unreadable(env, query_counter):
    """路径是目录 ⇒ UNREADABLE（存在性未知，不得当缺失）。"""
    _make_db(env.db, ("success",))
    env.maint_file.mkdir()
    rd = maint.read_maint_file()
    assert rd.status is maint.MaintRead.UNREADABLE
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 0


def test_pt11_absent_requires_db_evidence(env, query_counter):
    """真缺失：INACTIVE 只能来自 DB 查询；DB 有非 success 行或查询失败 ⇒ 恢复。"""
    _make_db(env.db, ("success",))
    assert maint.read_maint_file().status is maint.MaintRead.ABSENT
    assert maint.maintenance_state() is maint.MaintState.INACTIVE
    assert query_counter["n"] == 1

    _make_db(env.db, ("success", "running"))
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 2

    env.db.unlink()                        # 库打不开 ⇒ 无法确认 ≠ 正常
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    assert query_counter["n"] == 3
    with pytest.raises(maint.MaintUnverifiable):
        maint.migration_incomplete()


def test_pt11_absent_with_table_missing_is_inactive(env):
    """迁移从未开始（表不存在）⇒ migration_incomplete()=False ⇒ INACTIVE。"""
    _make_db(env.db, ())                   # 有库无表
    assert maint.migration_incomplete() is False
    assert maint.maintenance_state() is maint.MaintState.INACTIVE


def test_pt11_phase_names_match_design():
    assert maint.PHASE_NAMES == {
        "PREPARED", "TX_OPEN", "TX_ROLLED_BACK",
        "COMMIT_ATTEMPTED", "COMMITTED", "UNKNOWN",
    }


# --- PT-14：STATE_DIR 文件系统能力 -------------------------------------------


def test_pt14_selfcheck_full_chain(env):
    _make_db(env.db, ("success",))
    chk = maint.selfcheck_state_dir()
    assert chk.ok is True and chk.code == "ok"
    assert chk.db_dev == chk.state_dev
    assert all(chk.steps[name] for name in maint._PROBE_STEPS), chk.steps
    # 探针文件必须清理干净
    leftovers = [p.name for p in env.state.iterdir() if p.name.startswith(".maint-selfcheck")]
    assert leftovers == []


def test_pt14_selfcheck_cached_once(env):
    _make_db(env.db, ("success",))
    first = maint.selfcheck_state_dir()
    second = maint.selfcheck_state_dir()
    assert first is second                     # 结果缓存：每次启动只跑一次


def test_pt14_cross_device_rename_reports_exdev(tmp_path):
    """跨设备 rename 必须报 EXDEV（实测 errno 18）；同一目录 rename 必须成功。"""
    left = tmp_path / "left"
    left.mkdir()
    if not OTHER_FS.exists():
        pytest.skip("缺少第二个文件系统（/dev/shm）")
    if os.stat(left).st_dev == os.stat(OTHER_FS).st_dev:
        pytest.skip("临时目录与 /dev/shm 同设备，无法构造跨设备场景")

    probe = left / "probe.bin"
    probe.write_bytes(b"x")
    target_dir = OTHER_FS / f"nodepool-p14-{os.getpid()}"
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        with pytest.raises(OSError) as caught:
            os.replace(probe, target_dir / "probe.bin")
        assert caught.value.errno == 18, caught.value
        assert probe.exists()
        # 同目录 rename 仍然成功（自检依赖这一点）
        os.replace(probe, left / "probe.renamed")
        assert (left / "probe.renamed").exists()
    finally:
        for p in (probe, left / "probe.renamed", target_dir / "probe.bin"):
            try:
                p.unlink()
            except OSError:
                pass
        try:
            target_dir.rmdir()
        except OSError:
            pass


def test_pt14_selfcheck_rename_failure_reports_errno(env, monkeypatch):
    _make_db(env.db, ("success",))

    def boom(src, dst):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(maint.os, "replace", boom)
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False and chk.code == maint.ERR_MAINT_FS_UNSUPPORTED
    assert chk.errno == 22
    assert chk.steps["write"] and chk.steps["file_fsync"]
    assert chk.steps["rename"] is False
    assert [p.name for p in env.state.iterdir()] == []     # 探针不残留


def test_pt14_selfcheck_dir_fsync_failure_reports_errno(env, monkeypatch):
    _make_db(env.db, ("success",))

    def boom(_path):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(maint, "_fsync_dir", boom)
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False and chk.code == maint.ERR_MAINT_FS_UNSUPPORTED
    assert chk.errno == 22
    assert chk.steps["dir_fsync"] is False
    assert [p.name for p in env.state.iterdir()] == []


def test_pt14_selfcheck_readback_mismatch(env, monkeypatch):
    _make_db(env.db, ("success",))
    real = pathlib.Path.read_bytes

    def tampered(self):
        data = real(self)
        if ".maint-selfcheck" in self.name:
            return data + b"-tampered"
        return data

    monkeypatch.setattr(pathlib.Path, "read_bytes", tampered)
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False and chk.code == maint.ERR_MAINT_FS_UNSUPPORTED
    assert "readback" in chk.detail


def test_pt14_selfcheck_mkdir_failure_reports_errno(env, monkeypatch):
    _make_db(env.db, ("success",))
    blocker = env.state.parent / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setattr(maint, "STATE_DIR", blocker / "state")
    monkeypatch.setattr(maint, "MAINT_FILE", blocker / "state" / maint.MAINT_FILENAME)
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False and chk.code == maint.ERR_MAINT_FS_UNSUPPORTED
    assert chk.errno is not None                 # ENOTDIR/EEXIST 之一，必须带 errno
    assert chk.steps["mkdir"] is False


def test_pt14_selfcheck_on_real_state_dir():
    """PT-14 原文：在**真实配置的 STATE_DIR** 上跑完整链路（用仓库开发实例，
    不碰生产；探针文件执行完即自删）。"""
    real_state = Path(config.STATE_DIR)
    if not real_state.is_dir():
        pytest.skip("真实 STATE_DIR 不存在")
    if not os.access(real_state, os.W_OK):
        pytest.skip("真实 STATE_DIR 不可写")
    maint.reset_state_dir_selfcheck()
    chk = maint.selfcheck_state_dir()
    assert chk.ok is True, chk.as_dict()
    assert str(real_state) == chk.state_dir
    assert chk.db_dev == chk.state_dev
    assert all(chk.steps[name] for name in maint._PROBE_STEPS), chk.steps
    assert [p.name for p in real_state.iterdir()
            if p.name.startswith(".maint-selfcheck")] == []


def test_pt14_same_device_check_uses_db_path_st_dev(env):
    """同卷判定按计划原文比较 `os.stat(DB_PATH).st_dev`；库文件存在时结果必须与它一致。"""
    _make_db(env.db, ("success",))
    maint.reset_state_dir_selfcheck()
    chk = maint.selfcheck_state_dir()
    assert chk.ok is True, chk.as_dict()
    db_file_dev = os.stat(env.db).st_dev
    db_dir_dev = os.stat(env.db.parent).st_dev
    if db_file_dev == db_dir_dev:      # 正常文件系统：比较对象就是库文件本身
        assert chk.db_dev == db_file_dev
    else:                              # overlay/沙箱：退回库所在目录锚点
        assert chk.db_dev == db_dir_dev
    assert chk.state_dev == os.stat(env.state).st_dev


# --- PT-14b：跨卷拒绝、非法配置、业务不受影响 --------------------------------


@pytest.fixture
def cross_device_state(tmp_path, monkeypatch):
    if not OTHER_FS.exists():
        pytest.skip("缺少第二个文件系统（/dev/shm）")
    if os.stat(tmp_path).st_dev == os.stat(OTHER_FS).st_dev:
        pytest.skip("临时目录与 /dev/shm 同设备")
    state = OTHER_FS / f"nodepool-p14b-{os.getpid()}"
    state.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(maint, "STATE_DIR", state)
    monkeypatch.setattr(maint, "MAINT_FILE", state / maint.MAINT_FILENAME)
    maint.reset_state_dir_selfcheck()
    yield state
    maint.reset_state_dir_selfcheck()
    for p in list(state.iterdir()):
        try:
            p.unlink()
        except OSError:
            pass
    try:
        state.rmdir()
    except OSError:
        pass


def test_pt14b_cross_device_state_dir_is_refused(env, cross_device_state):
    _make_db(env.db, ("success",))
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False
    assert chk.code == maint.ERR_STATE_DIR_CROSS_DEVICE
    assert chk.db_dev != chk.state_dev
    assert chk.steps["write"] is False           # 跨卷在探针之前就被拦下
    assert [p.name for p in cross_device_state.iterdir()] == []


def test_pt14b_cross_device_keeps_business_ok(env, cross_device_state):
    """自检失败只禁用迁移：业务接口仍 200，且不 503。"""
    from fastapi.testclient import TestClient

    from backend.main import app

    from backend.database import db as db_mod

    _make_db(env.db, ("success",))
    db_mod.reset_connection()
    with TestClient(app) as client:
        chk = app.state.state_dir_check
        assert chk is not None and chk.ok is False
        assert chk.code == maint.ERR_STATE_DIR_CROSS_DEVICE
        assert client.get("/healthz").status_code == 200
        login = client.post("/api/auth/login", json={"password": config.ADMIN_PASSWORD})
        assert login.status_code == 200
        assert client.get("/api/nodes").status_code == 200


def test_pt14b_state_dir_equal_data_dir_is_refused(env, monkeypatch):
    monkeypatch.setattr(maint, "STATE_DIR", Path(config.DATA_DIR).resolve())
    monkeypatch.setattr(maint, "MAINT_FILE",
                        Path(config.DATA_DIR).resolve() / maint.MAINT_FILENAME)
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False
    assert chk.code == maint.ERR_STATE_DIR_EQUALS_DATA_DIR
    assert chk.steps["mkdir"] is False
    assert not (Path(config.DATA_DIR) / maint.MAINT_FILENAME).exists()


def test_pt14b_config_creates_state_dir_when_override_missing(tmp_path):
    """NODEPOOL_STATE_DIR 覆盖路径不存在 ⇒ 创建空目录。"""
    target = tmp_path / "nested" / "state"
    env = {**os.environ, "NODEPOOL_DATA_DIR": str(tmp_path / "data"),
           "NODEPOOL_STATE_DIR": str(target)}
    out = subprocess.run(
        [sys.executable, "-c",
         "from backend import config\n"
         "print(config.STATE_DIR)\n"
         "print(config.STATE_DIR_CONFIG_ERROR)\n"],
        cwd=str(REPO), env=env, check=True, capture_output=True, text=True,
    )
    lines = out.stdout.strip().splitlines()
    assert lines[0] == str(target.resolve())
    assert lines[1] == "None"
    assert target.is_dir() and list(target.iterdir()) == []


def test_pt14b_config_default_state_dir_is_data_state(tmp_path):
    env = {**os.environ, "NODEPOOL_DATA_DIR": str(tmp_path / "data")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from backend import config\n"
         "print(config.STATE_DIR)\n"],
        cwd=str(REPO), env=env, check=True, capture_output=True, text=True,
    )
    assert out.stdout.strip() == str((tmp_path / "data" / "state").resolve())


def test_pt14b_config_flags_state_dir_equal_data_dir(tmp_path):
    env = {**os.environ, "NODEPOOL_DATA_DIR": str(tmp_path / "data"),
           "NODEPOOL_STATE_DIR": str(tmp_path / "data")}
    out = subprocess.run(
        [sys.executable, "-c",
         "from backend import config\n"
         "print(config.STATE_DIR_CONFIG_ERROR)\n"],
        cwd=str(REPO), env=env, check=True, capture_output=True, text=True,
    )
    assert out.stdout.strip() == "state_dir_equals_data_dir"


# --- PT-24：探针副作用审计 ----------------------------------------------------


def test_pt24_probe_never_touches_database(env, capsys):
    """PT-24：自检 + 判定 + 写维护文件，全程数据库三件不变。"""
    conn = sqlite3.connect(env.db)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.executescript(migrate.SCHEMA_MIGRATIONS_DDL)
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, seq, version, status, started_at)"
            " VALUES ('w1', 1, '1.8.7-baseline', 'success', '2026-11-26T00:00:00Z')"
        )
        conn.execute("CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO audit (v) VALUES ('keep-me')")
        conn.commit()

        wal = env.db.parent / (env.db.name + "-wal")
        shm = env.db.parent / (env.db.name + "-shm")
        assert wal.exists(), "需要未 checkpoint 的 WAL 文件"
        before = {
            "db_sha": _sha(env.db),
            "wal_size": wal.stat().st_size,
            "wal_mtime": wal.stat().st_mtime_ns,
            "shm_exists": shm.exists(),
            "entries": sorted(p.name for p in env.db.parent.iterdir()),
        }

        chk = maint.selfcheck_state_dir()
        assert chk.ok is True
        assert maint.maintenance_state() is maint.MaintState.INACTIVE   # 只读查库
        rec = SimpleNamespace(migration_id="mig-24", phase="PREPARED", maint_nonce="n24")
        blob = maint.set_maintenance(rec)
        assert blob["maintenance"] is True
        assert maint.confirm_maint_write(rec) is True

        after = {
            "db_sha": _sha(env.db),
            "wal_size": wal.stat().st_size,
            "wal_mtime": wal.stat().st_mtime_ns,
            "shm_exists": shm.exists(),
            "entries": sorted(p.name for p in env.db.parent.iterdir()),
        }
    finally:
        conn.close()

    assert after == before, after
    # STATE_DIR 内只有维护文件，没有任何数据库文件或探针残留
    names = sorted(p.name for p in env.state.iterdir())
    assert names == [maint.MAINT_FILENAME]
    assert not [n for n in names if n.startswith(".maint-selfcheck")]
    assert not [n for n in names if "nodepool.db" in n]


def test_pt24_selfcheck_failure_creates_no_database_files(env, monkeypatch):
    """自检失败路径也不得建库/建表。"""
    def boom(src, dst):
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(maint.os, "replace", boom)
    chk = maint.selfcheck_state_dir()
    assert chk.ok is False
    assert not env.db.exists()
    assert [p.name for p in env.db.parent.iterdir() if p.is_file()] == []
    assert [p.name for p in env.state.iterdir()] == []
