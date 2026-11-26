"""Stage-1 integration tests: instance lock and startup classification."""
from __future__ import annotations

import fcntl
import os
import sqlite3
from pathlib import Path

import pytest

from backend import config
from backend.database import bootstrap, db, instance_lock, maint, migrate, pool
from backend.database.maint import MaintRead, MaintReadResult, MaintState


@pytest.fixture
def temp_env(tmp_path, monkeypatch):
    state = tmp_path / "state"
    data = tmp_path / "data"
    state.mkdir()
    data.mkdir()
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "DB_PATH", data / "nodepool.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backup")
    monkeypatch.setattr(maint, "STATE_DIR", state)
    monkeypatch.setattr(maint, "MAINT_FILE", state / maint.MAINT_FILENAME)
    pool.reset_for_tests()
    db.reset_connection()
    yield state, data / "nodepool.db"
    pool.reset_for_tests()
    db.reset_connection()


def test_instance_lock_is_exclusive_and_stale_file_is_not_a_lock(temp_env):
    _state, _db = temp_env
    first = instance_lock.acquire()
    try:
        with pytest.raises(instance_lock.AlreadyRunning):
            instance_lock.acquire()
    finally:
        first.release()
    # The lock file is still on disk; acquiring again must succeed.
    assert instance_lock.lock_path().exists()
    second = instance_lock.acquire()
    second.release()


def test_instance_lock_descriptor_is_cloexec(temp_env):
    _state, _db = temp_env
    lock = instance_lock.acquire()
    try:
        flags = fcntl.fcntl(lock.fd, fcntl.F_GETFD)
        assert flags & fcntl.FD_CLOEXEC
    finally:
        lock.release()


def _read(blob=None, status=MaintRead.ABSENT) -> MaintReadResult:
    return MaintReadResult(status, blob)


def _dbstate(kind: str) -> bootstrap.DbState:
    return bootstrap.DbState(kind=kind, exists=kind not in ("absent",))


def test_startup_clean_absent_is_inactive():
    decision = bootstrap.classify(_read(), _dbstate("absent"))
    assert decision.state is MaintState.INACTIVE
    assert decision.code == "first_deploy"


def test_startup_r4a_rolled_back_without_failed_row_is_recovery():
    blob = {"maintenance": True, "migration_id": "m1", "since": 1,
            "phase": "TX_ROLLED_BACK"}
    decision = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_only"))
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "rolled_back_without_failed_row"


def test_startup_unknown_phase_is_F1():
    blob = {"maintenance": True, "migration_id": "m1", "since": 1, "phase": "BOGUS"}
    decision = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_only"))
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "unknown_phase"


def test_startup_mismatched_phase_db_is_F2():
    blob = {"maintenance": True, "migration_id": "m1", "since": 1,
            "phase": "TX_OPEN"}
    decision = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_success"))
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "stale_maintenance"


def test_startup_corrupt_maintenance_is_F3():
    decision = bootstrap.classify(_read(status=MaintRead.CORRUPT), _dbstate("baseline_only"))
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "maintenance_file_invalid"


def test_probe_schema_absent_does_not_create_db(temp_env):
    _state, path = temp_env
    assert not path.exists()
    dbstate = bootstrap.probe_db(path)
    assert dbstate.kind == "absent"
    assert not path.exists()


def test_probe_sees_uncheckpointed_wal_commit(temp_env):
    _state, path = temp_env
    db.init_db()
    conn = migrate.open_connection(path)
    migrate.record_baseline(conn)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    # Write through a normal connection and leave WAL uncheckpointed.
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO schema_migrations(migration_id,version,seq,status,started_at)"
        " VALUES ('wal-probe','1.10',2,'success','2026-11-26')")
    conn.execute("COMMIT")
    conn.close()
    state = bootstrap.probe_db(path)
    assert state.kind == "baseline_success"


def _prepare_real_apply(temp_env, monkeypatch, *, trigger: bool = False):
    _state, path = temp_env
    monkeypatch.setattr(config, "MIGRATION_ENABLED", True)
    monkeypatch.setattr(config, "MIGRATION_FAULT_LOG", config.STATE_DIR / "fault.log")
    monkeypatch.setattr(migrate, "reset_issuer_token_for_tests", migrate.reset_issuer_token_for_tests)
    import types
    monkeypatch.setattr("backend.database.gate.drain",
                        lambda **_kw: types.SimpleNamespace(ok=True, continue_allowed=True,
                                                            detail={}, rounds=[], interval=0))
    db.init_db()
    conn = migrate.open_connection(path)
    migrate.record_baseline(conn)
    if trigger:
        conn.execute("CREATE TRIGGER r4a_fail BEFORE INSERT ON schema_migrations"
                     " WHEN NEW.status='running' BEGIN SELECT RAISE(ABORT,'r4a'); END")
    conn.close()
    rec = migrate.MigrationRecord(migration_id="m-p18", phase=migrate.Phase.PREPARED)
    rec.reason = "test"
    lock = instance_lock.acquire()
    pool.block_writes(pool.REASON_MAINTENANCE)
    maint.set_maintenance(rec, reason="test")
    from backend.database import gate
    gate.apply_state(maint.MaintState.ACTIVE)
    return lock, rec


def test_real_apply_persists_committed_phase(temp_env, monkeypatch):
    lock, rec = _prepare_real_apply(temp_env, monkeypatch)
    try:
        result = migrate.run(dry_run=False, lock_token=lock, drain_rounds=1,
                             drain_min_interval=0)
        assert result["ok"] is True and result["phase"] == "COMMITTED"
        blob = maint.read_maint_file().blob
        assert blob["phase"] == "COMMITTED" and blob["maintenance"] is True
        conn = migrate.open_connection(config.DB_PATH)
        try:
            rows = migrate.migration_rows(conn)
        finally:
            conn.close()
        assert [r["status"] for r in rows] == ["success", "success"]
    finally:
        lock.release()


def test_real_apply_failure_persists_tx_rolled_back(temp_env, monkeypatch):
    lock, rec = _prepare_real_apply(temp_env, monkeypatch, trigger=True)
    try:
        result = migrate.run(dry_run=False, lock_token=lock, drain_rounds=1,
                             drain_min_interval=0)
        assert result["code"] == "failed_row_unwritable" or result["code"] == "migration_failed"
        blob = maint.read_maint_file().blob
        assert blob["phase"] == "TX_ROLLED_BACK"
        conn = migrate.open_connection(config.DB_PATH)
        try:
            rows = migrate.migration_rows(conn)
        finally:
            conn.close()
        # T1's running row was rolled back; T2 wrote the failed row independently.
        assert len(rows) == 2
        assert rows[0]["version"].endswith("-baseline")
        assert rows[1]["status"] == "failed"
    finally:
        lock.release()


def test_startup_non_boolean_maintenance_is_F3():
    blob = {"maintenance": "false", "migration_id": "m1", "since": 1,
            "phase": "PREPARED"}
    decision = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_only"))
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "maintenance_file_invalid"


def test_startup_present_active_is_never_active():
    blob = {"maintenance": True, "migration_id": "m1", "since": 1,
            "phase": "PREPARED"}
    decision = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_only"))
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "interrupted_before_tx"


def test_startup_committed_false_requires_success_row():
    blob = {"maintenance": False, "migration_id": "m1", "since": 1,
            "phase": "COMMITTED"}
    ok = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_success"))
    assert ok.state is MaintState.INACTIVE
    assert ok.code == "maint_released_db_success"
    bad = bootstrap.classify(_read(blob, MaintRead.PRESENT), _dbstate("baseline_only"))
    assert bad.state is MaintState.RECOVERY_REQUIRED


def test_cli_apply_reuses_instance_lock_and_clears_on_success(temp_env, monkeypatch):
    _state, path = temp_env
    monkeypatch.setattr(config, "MIGRATION_ENABLED", True)
    monkeypatch.setattr(config, "MIGRATION_FAULT_LOG", config.STATE_DIR / "fault.log")
    monkeypatch.setattr(migrate, "reset_issuer_token_for_tests", migrate.reset_issuer_token_for_tests)
    import types
    monkeypatch.setattr("backend.database.gate.drain",
                        lambda **_kw: types.SimpleNamespace(ok=True, continue_allowed=True,
                                                            detail={}, rounds=[], interval=0))
    db.init_db()
    conn = migrate.open_connection(path)
    migrate.record_baseline(conn)
    conn.close()
    from backend.database import cli
    rc = cli.main(["run", "--apply", "--reason", "p18-cli-test"])
    assert rc == cli.EXIT_OK
    assert not Path(maint.MAINT_FILE).exists()
    conn = migrate.open_connection(path)
    try:
        assert [r["status"] for r in migrate.migration_rows(conn)] == ["success", "success"]
    finally:
        conn.close()
    # The lock file remains, but the kernel lock is free after the CLI exits.
    assert instance_lock.lock_path().exists()
    lock = instance_lock.acquire()
    lock.release()


def test_cli_dry_run_reuses_instance_lock(temp_env, monkeypatch):
    _state, path = temp_env
    monkeypatch.setattr(config, "MIGRATION_ENABLED", True)
    monkeypatch.setattr(config, "MIGRATION_FAULT_LOG", config.STATE_DIR / "fault.log")
    import types
    monkeypatch.setattr("backend.database.gate.drain",
                        lambda **_kw: types.SimpleNamespace(ok=True, continue_allowed=True,
                                                            detail={}, rounds=[], interval=0))
    db.init_db()
    conn = migrate.open_connection(path)
    migrate.record_baseline(conn)
    conn.execute("VACUUM INTO ?", (str(path.with_name("copy.db")),))
    conn.close()
    rec = migrate.MigrationRecord(migration_id="dry-run-lock", phase=migrate.Phase.PREPARED)
    maint.set_maintenance(rec, reason="test")
    from backend.database import cli
    rc = cli.main(["run", "--db", str(path.with_name("copy.db"))])
    assert rc == cli.EXIT_OK


def test_instance_lock_is_visible_across_processes(temp_env):
    import subprocess
    import sys

    repo = Path(__file__).resolve().parents[1]
    script = (
        "import sys\n"
        "from backend.database import instance_lock\n"
        "try:\n"
        "    lock = instance_lock.acquire()\n"
        "except instance_lock.AlreadyRunning:\n"
        "    sys.exit(2)\n"
        "lock.release()\n"
        "sys.exit(0)\n"
    )
    env = os.environ.copy()
    env["NODEPOOL_STATE_DIR"] = str(config.STATE_DIR)
    with instance_lock.acquire():
        refused = subprocess.run([sys.executable, "-c", script], cwd=repo, env=env,
                                 capture_output=True, text=True, timeout=20)
        assert refused.returncode == 2, refused.stderr
    accepted = subprocess.run([sys.executable, "-c", script], cwd=repo, env=env,
                              capture_output=True, text=True, timeout=20)
    assert accepted.returncode == 0, accepted.stderr


def test_r4a_sigkill_leaves_rolled_back_phase_and_recovery(temp_env):
    """End-to-end barrier test: kill -9 between TX_ROLLED_BACK and failed row."""
    import json
    import signal
    import subprocess
    import sys
    import time

    repo = Path(__file__).resolve().parents[1]
    _state, path = temp_env
    db.init_db()
    conn = migrate.open_connection(path)
    migrate.record_baseline(conn)
    conn.execute(
        "CREATE TRIGGER r4a_abort BEFORE INSERT ON schema_migrations "
        "WHEN NEW.status = 'running' BEGIN SELECT RAISE(ABORT, 'r4a_injected'); END"
    )
    conn.close()

    script = r'''
import os, sys, types
from backend import config
from backend.database import db, gate, instance_lock, maint, migrate
gate.drain = lambda **kw: types.SimpleNamespace(ok=True, continue_allowed=True, detail={}, rounds=[], interval=0)
db.init_db()
rec = migrate.MigrationRecord(migration_id="r4a-e2e")
rec.reason = "r4a-e2e"
with instance_lock.acquire() as lock:
    maint.set_maintenance(rec, reason=rec.reason)
    gate.apply_state(maint.MaintState.ACTIVE)
    migrate.run(dry_run=False, record=rec, lock_token=lock,
                statements=["INSERT INTO schema_migrations(version, seq, status, started_at, finished_at) "
                            "VALUES ('1.10', 2, 'success', 'x', 'x')"],
                drain_rounds=1, drain_min_interval=0)
'''
    env = os.environ.copy()
    env.update({
        "PYTHONPATH": str(repo),
        "NODEPOOL_STATE_DIR": str(config.STATE_DIR),
        "NODEPOOL_DATA_DIR": str(config.DATA_DIR),
        "NODEPOOL_CONFIG_DIR": str(config.CONFIG_DIR),
        "NODEPOOL_LOG_DIR": str(config.LOG_DIR),
        "NODEPOOL_BACKUP_DIR": str(config.BACKUP_DIR),
        "NODEPOOL_MIGRATION_ENABLED": "1",
        "NODEPOOL_TEST_MODE": "1",
        "NODEPOOL_TEST_BARRIER": "r4a_after_tx_rolled_back",
        "NODEPOOL_TEST_BARRIER_TIMEOUT": "5",
    })
    proc = subprocess.Popen([sys.executable, "-c", script], cwd=repo, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    marker = Path(config.STATE_DIR) / "p18-r4a.reached"
    deadline = time.monotonic() + 10
    while not marker.exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        if not marker.exists():
            out, err = proc.communicate(timeout=2) if proc.poll() is not None else ("", "barrier timeout")
            raise AssertionError(f"marker missing rc={proc.poll()} out={out!r} err={err!r}")
        payload = json.loads(marker.read_text())
        assert payload["phase"] == "TX_ROLLED_BACK"
        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)
        assert proc.returncode == -signal.SIGKILL
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    # Database: baseline only; no running/failed row. Maintenance phase is durable.
    conn = migrate.open_connection(path)
    try:
        rows = migrate.migration_rows(conn)
    finally:
        conn.close()
    assert [r["status"] for r in rows] == ["success"]
    assert maint.read_maint_file().blob["phase"] == "TX_ROLLED_BACK"
    decision = bootstrap.inspect_startup()
    assert decision.state is MaintState.RECOVERY_REQUIRED
    assert decision.code == "rolled_back_without_failed_row"


def test_app_startup_records_baseline_only_after_inactive(temp_env):
    from fastapi.testclient import TestClient
    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").json()["migration"] == "INACTIVE"
    conn = migrate.open_connection()
    try:
        rows = migrate.migration_rows(conn)
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "success"
    assert rows[0]["version"] == migrate.BASELINE_VERSION
