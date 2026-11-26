"""P-1.6 迁移状态机验收测试：PT-09b / PT-12 / PT-13 / PT-19 / PT-25 + 状态机与 halt 边界。

全部在临时目录与一次性库上执行；**不接触**仓库开发实例的 STATE_DIR、不接触生产库、
不执行任何真实迁移（P-1.6 只允许空迁移 dry-run，且只对副本）。

覆盖：
* 无事件链边界（PT-09b）：不建 `system_events`/`migration_tx_notes`，
  `write_event()/write_tx_event()/emit_event()/set_phase()` 一律 NotImplementedError，
  `schema_migrations.status` 只有 running/failed/success；
* 分派表自检（PT-12）与三态/`WriteOutcome` 返回（PT-13）；
* 重启不推进 phase、不补写事件（PT-19）、`migration_incomplete` 语义（PT-25）；
* 状态机：合法转换全集、非法转换、旧值不匹配、halt 后拒绝推进；
* run()：维护态/排空/备份/事务/COMMIT 异常/重复执行/进程中断；
* halt_migration：必须走 P-1.5 可信发射器对象身份绑定，未确认即 FatalExit。
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import config
from backend.database import backup, db, maint, migrate, pool

REPO_ROOT = Path(__file__).resolve().parents[1]
STATUSES_ALLOWED = ("running", "failed", "success")


# --- helpers -----------------------------------------------------------------

def _tmp_root() -> Path:
    """临时根目录建在 `$HOME` 下：`/tmp` 的文件/目录 `st_dev` 不同，会被同卷校验误判。"""
    root = Path.home() / ".p16test"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-", dir=str(root)))


def _fast_drain() -> dict:
    """让排空判据（2 轮 + 间隔 ≥10s）在测试里瞬间完成，但判据本身不被削弱。"""
    clock = [0.0]

    def fake_clock() -> float:
        clock[0] += 100.0
        return clock[0]

    return {
        "drain_rounds": 2,
        "drain_min_interval": 10.0,
        "clock": fake_clock,
        "sleeper": lambda _s: None,
        "checkpoint": lambda: (0, 0, 0),
        "holders": lambda: [],
    }


def _activate(migration_id: str = "mig-test") -> dict:
    """把维护文件写成 maintenance=True（ACTIVE），phase 用状态机自己的枚举。"""
    rec = migrate.MigrationRecord(migration_id=migration_id, phase=migrate.Phase.PREPARED)
    return maint.set_maintenance(rec, reason="p16_test")


@pytest.fixture
def env(monkeypatch):
    """维护态目录、主库、dry-run 副本、开关与 fault log 全部指向临时目录。"""
    root = _tmp_root()
    state = root / "state"
    state.mkdir()
    live = root / "live.db"
    copy_db = root / "copy.db"
    backups = root / "backup"
    backups.mkdir()

    monkeypatch.setattr(config, "DB_PATH", live)
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "BACKUP_DIR", backups)
    monkeypatch.setattr(config, "MIGRATION_ENABLED", True)
    monkeypatch.setattr(config, "MIGRATION_FAULT_LOG", state / "migration-fault.log")
    monkeypatch.setattr(maint, "STATE_DIR", state)
    monkeypatch.setattr(maint, "MAINT_FILE", state / maint.MAINT_FILENAME)
    maint.reset_state_dir_selfcheck()
    migrate.reset_issuer_token_for_tests()

    db.reset_connection()
    db.init_db()
    conn = migrate.open_connection(live)
    try:
        migrate.record_baseline(conn)
        conn.execute("VACUUM INTO ?", (str(copy_db),))
    finally:
        conn.close()

    yield SimpleNamespace(root=root, state=state, live=live, copy=copy_db,
                          backups=backups, fault_log=state / "migration-fault.log")

    migrate.reset_issuer_token_for_tests()
    maint.reset_state_dir_selfcheck()
    db.reset_connection()
    pool.reset_for_tests()
    import shutil
    shutil.rmtree(root, ignore_errors=True)


def _rows(path: Path) -> list[dict]:
    conn = migrate.open_connection(path)
    try:
        return [dict(row) for row in migrate.migration_rows(conn)]
    finally:
        conn.close()


def _row_counts(path: Path) -> int:
    return len(_rows(path))


def _fault_records(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line]


# --- PT-09b：无事件链边界 ------------------------------------------------------

def test_pt09b_no_event_tables_and_event_api_not_implemented(env):
    conn = migrate.open_connection(env.copy)
    try:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert "system_events" not in names
        assert "migration_tx_notes" not in names
        cols = {row[1] for row in conn.execute("PRAGMA table_info(schema_migrations)").fetchall()}
        for forbidden in ("prev_sha", "token_fp", "s5_failed_ever", "event", "seq_no"):
            assert forbidden not in cols
    finally:
        conn.close()

    rec = migrate.MigrationRecord(migration_id="m-x")
    with pytest.raises(NotImplementedError):
        migrate.write_event(rec, "migration_phase", {})
    with pytest.raises(NotImplementedError):
        migrate.write_tx_event(rec, "migration_phase", {})
    with pytest.raises(NotImplementedError):
        migrate.emit_event(rec, "migration_phase", {})
    with pytest.raises(NotImplementedError):
        migrate.set_phase(rec, migrate.Phase.PREPARED, migrate.Phase.TX_OPEN)


def test_pt09b_schema_migrations_status_only_three_values(env):
    conn = migrate.open_connection(env.copy)
    try:
        for bad in ("pending", "committed", "unknown", ""):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO schema_migrations (migration_id, seq, version, status,"
                    " started_at, finished_at, detail_json) VALUES (?,?,?,?,?,?,?)",
                    (f"bad-{bad or 'empty'}", 90, "1.10", bad, "2026-01-01 00:00:00",
                     None, None))
        conn.execute("ROLLBACK") if conn.in_transaction else None
        assert {row["status"] for row in migrate.migration_rows(conn)} <= set(STATUSES_ALLOWED)
    finally:
        conn.close()


def test_pt09b_migration_entry_closed_when_disabled(env, monkeypatch):
    monkeypatch.setattr(config, "MIGRATION_ENABLED", False)
    before = _row_counts(env.copy)
    with pytest.raises(migrate.MigrationDisabled):
        migrate.run(db_path=env.copy, **_fast_drain())
    with pytest.raises(migrate.MigrationDisabled):
        migrate.rollback_to_snapshot(db_path=env.copy)
    assert _row_counts(env.copy) == before


# --- PT-12：分派表自检 --------------------------------------------------------

def test_pt12_dispatch_table_is_exactly_legal_transitions(env):
    assert migrate.check_transition_table() is True
    assert set(migrate.TRANSITION_DISPATCH) == set(migrate.LEGAL_TRANSITIONS)
    assert migrate.TRANSITION_DISPATCH[(migrate.Phase.PREPARED, migrate.Phase.TX_OPEN)] == "tx_local"
    assert (migrate.TRANSITION_DISPATCH[(migrate.Phase.COMMIT_ATTEMPTED,
                                         migrate.Phase.UNKNOWN)] == "durable")


def test_pt12_broken_dispatch_table_is_refused(env):
    legal = migrate.LEGAL_TRANSITIONS
    # 多一条
    extra = dict(migrate.TRANSITION_DISPATCH)
    extra[(migrate.Phase.COMMITTED, migrate.Phase.UNKNOWN)] = "durable"
    with pytest.raises(migrate.DispatchTableBroken):
        migrate.check_transition_table(dispatch=extra)
    # 少一条
    missing = dict(migrate.TRANSITION_DISPATCH)
    missing.pop((migrate.Phase.UNKNOWN, migrate.Phase.TX_ROLLED_BACK))
    with pytest.raises(migrate.DispatchTableBroken):
        migrate.check_transition_table(dispatch=missing)
    # 后端写反：T1 必须是 tx_local
    wrong = dict(migrate.TRANSITION_DISPATCH)
    wrong[(migrate.Phase.PREPARED, migrate.Phase.TX_OPEN)] = "durable"
    with pytest.raises(migrate.DispatchTableBroken):
        migrate.check_transition_table(dispatch=wrong)
    # T3–T8 必须 durable
    wrong2 = dict(migrate.TRANSITION_DISPATCH)
    wrong2[(migrate.Phase.COMMIT_ATTEMPTED, migrate.Phase.COMMITTED)] = "tx_local"
    with pytest.raises(migrate.DispatchTableBroken):
        migrate.check_transition_table(dispatch=wrong2)
    # 名称表同样参与自检
    with pytest.raises(migrate.DispatchTableBroken):
        migrate.check_transition_table(names={})
    assert len(legal) == 8


# --- PT-13：三态 / WriteOutcome ------------------------------------------------

def test_pt13_reason_and_phase_return_typed_results(env):
    rec = migrate.MigrationRecord(migration_id="m-typed")
    out = migrate.append_reason(rec, "commit_raised", {"phase": rec.phase.value})
    assert isinstance(out, migrate.WriteOutcome)
    assert out is migrate.WriteOutcome.LOCAL
    assert rec.phase is migrate.Phase.PREPARED          # 记录原因不改 phase
    assert _fault_records(config.MIGRATION_FAULT_LOG)[-1]["code"] == "commit_raised"

    result = migrate.set_phase_memory(rec, migrate.Phase.PREPARED, migrate.Phase.TX_OPEN)
    assert isinstance(result, migrate.PhaseResult)
    assert result is not migrate.PhaseResult.REJECTED
    assert result is migrate.PhaseResult.SUCCESS
    assert result is not True


# --- 状态机：合法 / 非法转换 ---------------------------------------------------

def _walk_to(target: migrate.Phase) -> migrate.MigrationRecord:
    """按合法路径把记录推到目标 phase（只动内存）。"""
    rec = migrate.MigrationRecord(migration_id=f"m-{target.value}")
    paths = {
        migrate.Phase.PREPARED: (),
        migrate.Phase.TX_OPEN: (migrate.Phase.TX_OPEN,),
        migrate.Phase.COMMIT_ATTEMPTED: (migrate.Phase.TX_OPEN, migrate.Phase.COMMIT_ATTEMPTED),
        migrate.Phase.COMMITTED: (migrate.Phase.TX_OPEN, migrate.Phase.COMMIT_ATTEMPTED,
                                  migrate.Phase.COMMITTED),
        migrate.Phase.UNKNOWN: (migrate.Phase.TX_OPEN, migrate.Phase.COMMIT_ATTEMPTED,
                                migrate.Phase.UNKNOWN),
        migrate.Phase.TX_ROLLED_BACK: (migrate.Phase.TX_OPEN, migrate.Phase.TX_ROLLED_BACK),
    }
    previous = migrate.Phase.PREPARED
    for step in paths[target]:
        migrate.set_phase_memory(rec, previous, step)
        previous = step
    assert rec.phase is target
    return rec


def test_legal_transitions_are_exactly_eight_and_all_accepted(env):
    assert len(migrate.LEGAL_TRANSITIONS) == 8
    for old, new in migrate.LEGAL_TRANSITIONS:
        rec = _walk_to(old)
        before = rec.phase_epoch
        assert migrate.set_phase_memory(rec, old, new) is migrate.PhaseResult.SUCCESS
        assert rec.phase is new
        assert rec.phase_epoch == before + 1          # 每次成功转换只递增一次


def test_phase_epoch_equals_number_of_successful_transitions(env):
    for phase in (migrate.Phase.PREPARED, migrate.Phase.TX_OPEN,
                  migrate.Phase.COMMIT_ATTEMPTED, migrate.Phase.COMMITTED,
                  migrate.Phase.UNKNOWN, migrate.Phase.TX_ROLLED_BACK):
        rec = _walk_to(phase)
        assert rec.phase_epoch == _depth_of(phase)
        assert rec.phase is phase


def _depth_of(phase: migrate.Phase) -> int:
    return {
        migrate.Phase.PREPARED: 0,
        migrate.Phase.TX_OPEN: 1,
        migrate.Phase.COMMIT_ATTEMPTED: 2,
        migrate.Phase.COMMITTED: 3,
        migrate.Phase.UNKNOWN: 3,
        migrate.Phase.TX_ROLLED_BACK: 2,
    }[phase]


def test_illegal_transitions_rejected(env):
    illegal = [
        (migrate.Phase.COMMITTED, migrate.Phase.UNKNOWN),
        (migrate.Phase.COMMITTED, migrate.Phase.TX_ROLLED_BACK),
        (migrate.Phase.COMMITTED, migrate.Phase.COMMITTED),
        (migrate.Phase.TX_ROLLED_BACK, migrate.Phase.COMMITTED),
        (migrate.Phase.UNKNOWN, migrate.Phase.UNKNOWN),       # 用 append_reason
        (migrate.Phase.PREPARED, migrate.Phase.COMMITTED),
        (migrate.Phase.PREPARED, migrate.Phase.UNKNOWN),
        (migrate.Phase.TX_OPEN, migrate.Phase.COMMITTED),
        (migrate.Phase.TX_OPEN, migrate.Phase.UNKNOWN),
    ]
    for old, new in illegal:
        rec = _walk_to(old)
        epoch_before = rec.phase_epoch
        with pytest.raises(migrate.IllegalTransition):
            migrate.set_phase_memory(rec, old, new)
        assert rec.phase is old, f"{old.value} -> {new.value} 不得留下 phase 变化"
        assert rec.phase_epoch == epoch_before, "拒绝路径不得递增 phase_epoch"


def test_phase_mismatch_when_same_transition_written_twice(env):
    rec = _walk_to(migrate.Phase.PREPARED)
    migrate.set_phase_memory(rec, migrate.Phase.PREPARED, migrate.Phase.TX_OPEN)
    assert rec.phase_epoch == 1
    with pytest.raises(migrate.PhaseMismatch):
        migrate.set_phase_memory(rec, migrate.Phase.PREPARED, migrate.Phase.TX_OPEN)
    assert rec.phase is migrate.Phase.TX_OPEN
    assert rec.phase_epoch == 1                     # 重复转换不得递增


def test_halted_record_rejects_phase_and_reason(env):
    rec = _walk_to(migrate.Phase.COMMIT_ATTEMPTED)
    rec.halted = True
    epoch_before = rec.phase_epoch
    with pytest.raises(migrate.MigrationHalted):
        migrate.set_phase_memory(rec, migrate.Phase.COMMIT_ATTEMPTED, migrate.Phase.UNKNOWN)
    with pytest.raises(migrate.MigrationHalted):
        migrate.append_reason(rec, "after_halt", {})
    assert rec.phase is migrate.Phase.COMMIT_ATTEMPTED
    assert rec.phase_epoch == epoch_before


# --- run()：happy path / 拒绝路径 ----------------------------------------------

def test_run_dry_run_commits_empty_migration(env):
    _activate()
    conn = migrate.open_connection(env.copy)
    try:
        hash_before = migrate.structure_hash(conn)
    finally:
        conn.close()

    result = migrate.run(db_path=env.copy, **_fast_drain())

    assert result["ok"] is True
    assert result["code"] == "migration_committed"
    assert result["phase"] == migrate.Phase.COMMITTED.value
    assert result["phase_epoch"] == 3          # T1 + T2 + T4
    assert result["statements"] == 0 and result["dry_run"] is True

    rows = _rows(env.copy)
    assert len(rows) == 2
    assert rows[0]["status"] == "success" and rows[0]["version"] == migrate.BASELINE_VERSION
    assert rows[1]["status"] == "success"
    assert rows[1]["version"] == migrate.TARGET_VERSION
    assert rows[1]["seq"] == 2
    assert rows[1]["migration_id"] == result["migration_id"]
    detail = json.loads(rows[1]["detail_json"])
    assert detail["dry_run"] is True and detail["statements"] == 0

    conn = migrate.open_connection(env.copy)
    try:
        assert migrate.structure_hash(conn) == hash_before      # 空迁移：零 DDL
    finally:
        conn.close()

    # 空迁移不应产生 halt 记录
    assert [r for r in _fault_records(config.MIGRATION_FAULT_LOG) if r["event"] == "halt_tail"] == []


def test_run_refuses_live_db_and_non_dry_run(env):
    _activate()
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.run(db_path=env.live, **_fast_drain())
    assert exc.value.reason == "dry_run_requires_copy"
    with pytest.raises(migrate.MigrationRefused) as exc2:
        migrate.run(db_path=env.copy, dry_run=False, **_fast_drain())
    assert exc2.value.reason == "p17_required"
    assert _row_counts(env.live) == 1
    assert _row_counts(env.copy) == 1


def test_run_requires_active_maintenance(env):
    # 无维护文件 + 库内全 success ⇒ INACTIVE；run 必须拒绝，且不写任何行
    before = _row_counts(env.copy)
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.run(db_path=env.copy, **_fast_drain())
    assert exc.value.reason == "maintenance_required"
    assert _row_counts(env.copy) == before


def test_run_drain_failure_halts_and_keeps_block(env):
    _activate()
    kwargs = _fast_drain()
    kwargs["holders"] = lambda: ["19999"]
    result = migrate.run(db_path=env.copy, **kwargs)

    assert result["ok"] is False
    assert result["code"] == "drain_failed"
    assert result["halt_tail"] == pool.HALT_TAIL_OK
    assert pool.writes_blocked() is True
    assert _row_counts(env.copy) == 1                    # 排空失败：零写入
    halt = [r for r in _fault_records(config.MIGRATION_FAULT_LOG) if r["event"] == "halt_tail"]
    assert halt and halt[-1]["reason"] == "drain_failed"


def test_run_transaction_failure_writes_failed_row_and_blocks(env, monkeypatch):
    _activate()

    real_insert = migrate._insert_row
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:                  # 只让迁移事务内的那次插入失败
            raise sqlite3.OperationalError("simulated ddl failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(migrate, "_insert_row", boom)
    result = migrate.run(db_path=env.copy, **_fast_drain())

    assert result["ok"] is False
    assert result["code"] == "migration_failed"
    assert result["result"] == "known_failed"
    assert result["phase"] == migrate.Phase.TX_ROLLED_BACK.value
    # 插入失败发生在 T2 之前 ⇒ T1 + T3 共两次成功转换（回滚与复核均明确成功）
    assert result["phase_epoch"] == 2
    assert result["failed_row"] is True
    assert pool.writes_blocked() is True

    rows = _rows(env.copy)
    assert len(rows) == 2
    assert rows[1]["status"] == "failed"
    assert "simulated ddl failure" in json.loads(rows[1]["detail_json"])["failure"]

    summary = migrate.migration_state_summary(env.copy)
    assert summary["migration_incomplete"] is True
    assert summary["preflight"] == "incomplete"


class _CommitBoom:
    """包装真实连接：COMMIT/ROLLBACK 抛 `disk I/O error`，模拟"提交结果不可知"。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        head = sql.strip().upper()
        if head.startswith("COMMIT") or head.startswith("ROLLBACK"):
            raise sqlite3.OperationalError("disk I/O error (simulated)")
        return self._conn.execute(sql, *args, **kwargs)


def test_run_commit_exception_halts_without_advancing_phase(env, monkeypatch):
    _activate()
    monkeypatch.setattr(migrate, "open_connection",
                        lambda *a, **k: _CommitBoom(sqlite3.connect(str(env.copy),
                                                                    isolation_level=None)))
    result = migrate.run(db_path=env.copy, **_fast_drain())

    assert result["ok"] is False
    assert result["code"] == "commit_outcome_unknown"
    assert result["phase"] == migrate.Phase.COMMIT_ATTEMPTED.value     # 不得冒充 UNKNOWN
    assert result["phase_epoch"] == 2
    assert result["connection_closed"] is True
    assert result["status"] == "halted"
    assert pool.writes_blocked() is True
    # 提交异常后不得出现 success/failed 行（无事件链 ⇒ 不伪造任何持久证据）
    assert _row_counts(env.copy) == 1
    tail = [r for r in _fault_records(config.MIGRATION_FAULT_LOG) if r["event"] == "halt_tail"]
    assert tail and tail[-1]["reason"] == "commit_outcome_unknown"
    assert tail[-1]["halt_tail"] == pool.HALT_TAIL_OK


def test_second_run_after_commit_is_refused(env):
    _activate()
    assert migrate.run(db_path=env.copy, **_fast_drain())["ok"] is True
    _activate("mig-second")
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.run(db_path=env.copy, **_fast_drain())
    assert exc.value.reason == "migration_not_ready"
    assert _row_counts(env.copy) == 2                     # 不重复迁移、不重复记账


def test_concurrent_run_is_refused_by_single_instance_lock(env):
    _activate()
    with migrate.migration_run_lock(env.state / "migration.lock"):
        with pytest.raises(migrate.AlreadyRunning):
            migrate.run(db_path=env.copy, **_fast_drain())
    assert _row_counts(env.copy) == 1


def test_crash_between_begin_and_commit_leaves_no_running_row(env):
    """进程中断（os._exit）：未提交的 running 行必须消失，且不自动推进 phase。"""
    _activate()
    code = textwrap.dedent(
        """
        import os
        from backend.database import migrate
        conn = migrate.open_connection()
        conn.execute("BEGIN IMMEDIATE")
        migrate._insert_row(conn, migration_id="crash-run", seq=2, version="1.10",
                            status="running", started_at="2026-01-01 00:00:00",
                            finished_at=None, detail_json="{}")
        os._exit(7)      # 不 COMMIT、不 ROLLBACK：模拟进程被杀
        """
    )
    env_vars = dict(os.environ)
    env_vars.update({
        "NODEPOOL_DATA_DIR": str(env.root / "child-data"),
        "NODEPOOL_DB": str(env.copy),
        "NODEPOOL_STATE_DIR": str(env.state),
    })
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO_ROOT),
                          env=env_vars, capture_output=True, text=True)
    assert proc.returncode == 7

    rows = _rows(env.copy)
    assert [r["migration_id"] for r in rows] == [migrate.BASELINE_VERSION]
    assert all(r["status"] == "success" for r in rows)

    # 中断后重启：只有维护文件存在才是 ACTIVE；phase 不会被自动推进
    assert maint.maintenance_state() is maint.MaintState.ACTIVE
    rec = migrate.MigrationRecord(migration_id="after-crash")
    assert rec.phase is migrate.Phase.PREPARED
    assert migrate.run(db_path=env.copy, **_fast_drain())["ok"] is True


# --- PT-19 / PT-25：重启与 migration_incomplete ---------------------------------

def _leave_running_row(path: Path, migration_id: str = "half-done") -> None:
    conn = migrate.open_connection(path)
    try:
        migrate._insert_row(conn, migration_id=migration_id, seq=2, version="1.10",
                            status="running", started_at="2026-01-01 00:00:00",
                            finished_at=None, detail_json="{}")
    finally:
        conn.close()


def test_pt19_restart_without_maintenance_file_is_recovery_required(env):
    # 维护文件缺失时，唯一判据是主库是否仍有未完成迁移（`config.DB_PATH`）
    _leave_running_row(env.live)
    assert maint.maintenance_state() is maint.MaintState.RECOVERY_REQUIRED
    summary = migrate.migration_state_summary(env.live)
    assert summary["migration_incomplete"] is True
    before = _row_counts(env.live)
    # P-1.6 的 dry-run 只允许副本，因此 run 在维护判定之前就被副本守卫拒绝
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.run(db_path=env.live, **_fast_drain())
    assert exc.value.reason == "dry_run_requires_copy"
    assert _row_counts(env.live) == before                 # 不补写、不改写
    assert _rows(env.live)[1]["status"] == "running"
    assert migrate.MigrationRecord(migration_id="restart").phase is migrate.Phase.PREPARED


def test_pt19_restart_with_maintenance_file_stays_active_and_does_not_advance(env):
    _activate("half-done")
    _leave_running_row(env.copy)
    assert maint.maintenance_state() is maint.MaintState.ACTIVE
    assert migrate.migration_state_summary(env.copy)["migration_incomplete"] is True
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.run(db_path=env.copy, **_fast_drain())
    assert exc.value.reason == "migration_not_ready"
    assert _rows(env.copy)[1]["status"] == "running"


def test_pt25_incomplete_migration_blocks_run_and_preflight(env):
    _activate()
    _leave_running_row(env.copy)
    conn = migrate.open_connection(env.copy)
    try:
        pre = migrate.preflight(conn)
    finally:
        conn.close()
    assert pre["state"] == "incomplete"
    assert pre["action"] == "recovery_required"
    assert pre["allowed"] is False
    with pytest.raises(migrate.MigrationRefused):
        migrate.run(db_path=env.copy, **_fast_drain())


# --- halt_migration：可信发射器对象身份绑定 -----------------------------------

def test_halt_uses_trusted_issuer_and_registry_holds_function_object(env):
    _activate()
    kwargs = _fast_drain()
    kwargs["holders"] = lambda: ["4242"]
    assert pool._ISSUERS == {}                     # 惰性：进入 halt 之前不注册
    result = migrate.run(db_path=env.copy, **kwargs)
    assert result["code"] == "drain_failed"

    records = list(pool._ISSUERS.values())
    assert len(records) == 1
    assert records[0].function is migrate.halt_migration
    assert records[0].token is migrate._ISSUER_TOKEN
    assert records[0].issuer.endswith("halt_migration")


def test_halt_without_maintenance_is_fatal_and_keeps_writes_blocked(env):
    # 无维护文件 ⇒ maintenance_ok=False ⇒ 不得签发凭据、不得"尽力而为"继续
    assert pool._ISSUERS == {}
    with pytest.raises(migrate.FatalExit) as exc:
        migrate.halt_migration("commit_outcome_unknown",
                               rec=migrate.MigrationRecord(migration_id="m-fatal"))
    assert exc.value.reason == "halt_unconfirmed"
    assert exc.value.exit_code != 0
    assert pool.writes_blocked() is True
    assert pool._ISSUERS == {}                     # 未确认 ⇒ 连令牌都不注册
    fatal = [r for r in _fault_records(config.MIGRATION_FAULT_LOG) if r["event"] == "halt_requested"]
    assert fatal and fatal[-1]["maintenance_ok"] is False


def test_replaced_halt_migration_object_cannot_mint(env, monkeypatch):
    """P-1.5 对象身份绑定的 P-1.6 侧回归：同名函数被替换后令牌立即失效。"""
    _activate()
    kwargs = _fast_drain()
    kwargs["holders"] = lambda: ["4242"]
    migrate.run(db_path=env.copy, **kwargs)
    token = migrate._ISSUER_TOKEN
    assert token is not None

    def halt_migration(_reason, **_kwargs):        # 同名但不同对象（真实替换）
        return {}

    monkeypatch.setattr(migrate, "halt_migration", halt_migration)
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.mint_confirmation(token, "m-replaced", True, True)
    assert exc.value.code == "issuer_replaced"


def test_unregistered_token_cannot_mint(env):
    _activate()
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.mint_confirmation(object(), "m-x", True, True)
    assert exc.value.code == "untrusted_issuer"


# --- rollback_to_snapshot：受维护态 / 排空约束 --------------------------------

def _file_digest(path: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    for suffix in ("", "-wal", "-shm"):
        candidate = Path(str(path) + suffix)
        if candidate.exists():
            h.update(candidate.read_bytes())
    return h.hexdigest()


def test_rollback_requires_active_maintenance(env):
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.rollback_to_snapshot(None, db_path=env.copy)
    assert exc.value.reason == "maintenance_required"


def test_rollback_missing_set_refused(env):
    _activate()
    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.rollback_to_snapshot(None, db_path=env.copy)
    assert exc.value.reason == "no_backup_set"


def test_rollback_never_calls_restore_set(env, monkeypatch):
    """P-1.6 不执行真实恢复：`restore_set` 一次都不许被调用，目标库字节不变。"""
    _activate()
    info = backup.snapshot_set(stage="pre-migrate", src_db=env.copy,
                               backup_dir=env.backups, stop_write=True)
    assert backup.verify_set(info["path"])["ok"] is True
    calls = {"n": 0}

    def forbidden(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("P-1.6 不得调用 backup.restore_set()")

    monkeypatch.setattr(backup, "restore_set", forbidden)
    digest_before = _file_digest(env.copy)

    with pytest.raises(migrate.MigrationRefused) as exc:
        migrate.rollback_to_snapshot(info["path"], db_path=env.copy)
    assert exc.value.reason == "p17_required"
    assert calls["n"] == 0
    assert _file_digest(env.copy) == digest_before
    assert _row_counts(env.copy) == 1


def test_cli_rollback_refuses_and_leaves_db_untouched(env, monkeypatch):
    """CLI `rollback` 必须返回 p17_required，且不默认对主库执行恢复。"""
    from backend.database import cli

    _activate()
    info = backup.snapshot_set(stage="pre-migrate", src_db=env.copy,
                               backup_dir=env.backups, stop_write=True)
    calls = {"n": 0}

    def forbidden(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("CLI rollback 不得调用 restore_set()")

    monkeypatch.setattr(backup, "restore_set", forbidden)
    live_digest = _file_digest(env.live)
    copy_digest = _file_digest(env.copy)

    rc = cli.main(["rollback", "--set", str(info["path"]), "--json"])
    assert rc == cli.EXIT_REFUSED
    assert calls["n"] == 0
    assert _file_digest(env.live) == live_digest
    assert _file_digest(env.copy) == copy_digest
    assert _row_counts(env.live) == 1


# --- 修正 2：事务失败分支（BEGIN / ROLLBACK / 只读复核） ----------------------

class _SqlFail:
    """包装迁移连接：指定语句前缀抛错，其余透传（模拟磁盘/事务错误）。"""

    def __init__(self, conn: sqlite3.Connection, fail_on: tuple[str, ...]) -> None:
        self._conn = conn
        self._fail_on = tuple(token.upper() for token in fail_on)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *args, **kwargs):
        head = sql.strip().upper()
        for token in self._fail_on:
            if head.startswith(token):
                raise sqlite3.OperationalError(f"simulated {token} failure")
        return self._conn.execute(sql, *args, **kwargs)


def _failing_connection(target: Path, tokens: tuple[str, ...]):
    return lambda *_a, **_k: _SqlFail(
        sqlite3.connect(str(target), timeout=30, isolation_level=None), tokens)


def _fail_once_insert(monkeypatch):
    """只让迁移事务内的那次 `_insert_row` 失败，`failed` 行仍能写入。"""
    real_insert = migrate._insert_row
    calls = {"n": 0}

    def boom(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.OperationalError("simulated insert failure")
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(migrate, "_insert_row", boom)
    return calls


def test_begin_failure_keeps_prepared_and_writes_nothing(env, monkeypatch):
    _activate()
    monkeypatch.setattr(migrate, "open_connection", _failing_connection(env.copy, ("BEGIN",)))
    result = migrate.run(db_path=env.copy, **_fast_drain())

    assert result["ok"] is False
    assert result["code"] == "begin_failed"
    assert result["result"] == "no_transaction"
    assert result["phase"] == migrate.Phase.PREPARED.value      # 不伪造 TX_OPEN
    assert result["phase_epoch"] == 0
    assert result["rows_written"] == 0
    assert _row_counts(env.copy) == 1                           # 无 running / failed 行
    assert pool.writes_blocked() is True
    logs = _fault_records(config.MIGRATION_FAULT_LOG)
    assert logs[-1]["event"] == "migration_transaction_failed"
    assert logs[-1]["begin_ok"] is False


def test_known_failed_requires_rollback_and_recheck_success(env, monkeypatch):
    _activate()
    _fail_once_insert(monkeypatch)
    result = migrate.run(db_path=env.copy, **_fast_drain())
    assert result["result"] == "known_failed"
    assert result["phase"] == migrate.Phase.TX_ROLLED_BACK.value
    assert result["phase_epoch"] == 2
    assert _rows(env.copy)[1]["status"] == "failed"
    detail = json.loads(_rows(env.copy)[1]["detail_json"])
    assert detail["known_failed"] is True


def test_rollback_failure_is_outcome_unknown_and_halts(env, monkeypatch):
    _activate()
    _fail_once_insert(monkeypatch)
    monkeypatch.setattr(migrate, "open_connection", _failing_connection(env.copy, ("ROLLBACK",)))
    result = migrate.run(db_path=env.copy, **_fast_drain())

    assert result["ok"] is False
    assert result["code"] == "rollback_unverified"
    assert result["result"] == "outcome_unknown"
    assert result["phase"] == migrate.Phase.TX_OPEN.value        # 不伪造 TX_ROLLED_BACK
    assert result["phase_epoch"] == 1                            # 只有 T1
    assert _row_counts(env.copy) == 1                            # 不写误导性 failed 行
    assert pool.writes_blocked() is True
    tail = [r for r in _fault_records(config.MIGRATION_FAULT_LOG) if r["event"] == "halt_tail"]
    assert tail and tail[-1]["reason"] == "rollback_unverified"


def test_recheck_failure_is_outcome_unknown_and_halts(env, monkeypatch):
    _activate()
    _fail_once_insert(monkeypatch)
    monkeypatch.setattr(migrate, "_recheck_structure_unchanged",
                        lambda *_a, **_k: (False, "recheck_unavailable: simulated"))
    result = migrate.run(db_path=env.copy, **_fast_drain())

    assert result["code"] == "rollback_unverified"
    assert result["result"] == "outcome_unknown"
    # 回滚成功但复核不可用 ⇒ 不得认定 KNOWN_FAILED，phase 停在最后已知状态
    assert result["phase"] in (migrate.Phase.TX_OPEN.value, migrate.Phase.COMMIT_ATTEMPTED.value)
    assert result["phase_epoch"] in (1, 2)
    assert _row_counts(env.copy) == 1
    assert pool.writes_blocked() is True
    log = [r for r in _fault_records(config.MIGRATION_FAULT_LOG)
           if r["event"] == "migration_transaction_failed"][-1]
    assert log["recheck_ok"] is False


def test_structure_changed_is_outcome_unknown_not_known_failed(env, monkeypatch):
    _activate()
    _fail_once_insert(monkeypatch)
    monkeypatch.setattr(migrate, "_recheck_structure_unchanged",
                        lambda *_a, **_k: (False, "recheck_structure_changed"))
    result = migrate.run(db_path=env.copy, **_fast_drain())
    assert result["code"] == "rollback_unverified"
    assert _row_counts(env.copy) == 1                  # 结构变了也不能写 failed 行冒充确认


# --- 修正 4：COMMIT 异常时迁移连接必须先关闭 --------------------------------

def test_commit_exception_closes_connection_before_halt(env, monkeypatch):
    _activate()
    monkeypatch.setattr(migrate, "open_connection", _failing_connection(env.copy, ("COMMIT",)))
    result = migrate.run(db_path=env.copy, **_fast_drain())
    assert result["connection_closed"] is True
    logs = _fault_records(config.MIGRATION_FAULT_LOG)
    events = [r["event"] for r in logs]
    assert events.index("migration_commit_outcome_unknown") < events.index("halt_requested")
    assert logs[[i for i, e in enumerate(events)
                 if e == "migration_commit_outcome_unknown"][-1]]["connection_closed"] is True


def test_commit_exception_close_failure_is_fatal(env, monkeypatch):
    _activate()
    monkeypatch.setattr(migrate, "open_connection", _failing_connection(env.copy, ("COMMIT",)))
    monkeypatch.setattr(migrate, "_close_migration_connection",
                        lambda _conn: (False, "close_unverified: simulated"))
    with pytest.raises(migrate.FatalExit) as exc:
        migrate.run(db_path=env.copy, **_fast_drain())
    assert exc.value.reason == "migration_connection_open"
    assert exc.value.exit_code != 0
    assert pool.writes_blocked() is True
    assert _row_counts(env.copy) == 1                  # 不写 success/failed 猜结果


# --- 修正 3：halt_migration 的独立异常边界 -----------------------------------

def test_halt_block_writes_failure_still_confirms_other_actions(env, monkeypatch):
    _activate()
    calls: list[str] = []
    real_state = maint.maintenance_state
    real_confirm = pool.confirm_writes_blocked

    def bad_block(*_a, **_k):
        calls.append("block")
        raise RuntimeError("block boom")

    monkeypatch.setattr(pool, "block_writes", bad_block)
    monkeypatch.setattr(maint, "maintenance_state",
                        lambda: calls.append("state") or real_state())
    monkeypatch.setattr(pool, "confirm_writes_blocked",
                        lambda: calls.append("confirm") or real_confirm())

    rec = migrate.MigrationRecord(migration_id="m-block")
    with pytest.raises(migrate.FatalExit) as exc:
        migrate.halt_migration("x", rec=rec)
    assert exc.value.reason == "halt_unconfirmed"
    assert calls == ["block", "state", "confirm"]      # 三项都执行，未短路
    assert rec.halted is False
    assert pool._ISSUERS == {}                         # 不签发凭据


def test_halt_maintenance_state_failure_is_fatal_but_keeps_block(env, monkeypatch):
    _activate()

    def bad_state():
        raise OSError("maintenance file unreadable")

    monkeypatch.setattr(maint, "maintenance_state", bad_state)
    rec = migrate.MigrationRecord(migration_id="m-maint")
    with pytest.raises(migrate.FatalExit) as exc:
        migrate.halt_migration("x", rec=rec)
    assert exc.value.reason == "halt_unconfirmed"
    assert pool.writes_blocked() is True               # 封锁动作仍执行成功
    assert rec.halted is False
    assert pool._ISSUERS == {}


def test_halt_confirm_failure_is_fatal_without_credential(env, monkeypatch):
    _activate()

    def bad_confirm():
        raise RuntimeError("confirm boom")

    monkeypatch.setattr(pool, "confirm_writes_blocked", bad_confirm)
    rec = migrate.MigrationRecord(migration_id="m-confirm")
    with pytest.raises(migrate.FatalExit) as exc:
        migrate.halt_migration("x", rec=rec)
    assert exc.value.reason == "halt_unconfirmed"
    assert rec.halted is False
    assert pool._ISSUERS == {}


def test_halt_log_failure_does_not_block_lockdown(env, monkeypatch):
    _activate()

    def bad_log(*_a, **_k):
        raise OSError("log volume full")

    monkeypatch.setattr(migrate, "_write_fault_log", bad_log)
    rec = migrate.MigrationRecord(migration_id="m-log")
    result = migrate.halt_migration("x", rec=rec)
    assert result["halt_tail"] == pool.HALT_TAIL_OK
    assert rec.halted is True                          # halt_tail 复核成功后才置位
    assert result["diagnostics"]["log_errors"]         # 日志错误保留为诊断
