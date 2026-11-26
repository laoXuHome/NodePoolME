"""P-1.5 五入口 + 二次封锁 + 停写排空：PT-15 / PT-16 / PT-23。

全部在临时目录与一次性库上执行：不接触仓库开发实例的 STATE_DIR、不接触生产库、
不执行 P-7、不在 NAS 上跑启动自检。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-p15-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from backend.database import backup, cli, db, gate, maint, pool  # noqa: E402
from backend.database.maint import MaintState  # noqa: E402
from backend.scheduler import manager  # noqa: E402

ACTIVE_BLOB = {"maintenance": True, "migration_id": "p15-mig", "since": 1730000000,
               "phase": "PREPARED"}


@pytest.fixture(autouse=True)
def _clean_gate():
    pool.reset_for_tests()
    yield
    pool.reset_for_tests()
    db.close_all_connections()


@pytest.fixture
def maint_file(tmp_path, monkeypatch) -> Path:
    path = tmp_path / "state" / "maintenance.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(maint, "MAINT_FILE", path)
    return path


def _write_active(path: Path, blob: dict | None = None) -> Path:
    path.write_text(json.dumps(blob or ACTIVE_BLOB), encoding="utf-8")
    return path


def _write_corrupt(path: Path) -> Path:
    path.write_bytes(b'{"maintenance": true, ')      # 截断：CORRUPT
    return path


# ----------------------------------------------------------------- PT-15 入口 1
def test_http_write_refused_when_active(maint_file):
    from backend.main import app

    _write_active(maint_file)
    with TestClient(app) as client:
        resp = client.post("/api/does-not-matter", json={})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "maintenance"
        # 只读请求不受影响
        assert client.get("/api/docs").status_code in (200, 404)
        assert client.get("/healthz").status_code == 200


def test_http_write_refused_when_recovery_required(maint_file):
    from backend.main import app

    _write_corrupt(maint_file)
    with TestClient(app) as client:
        resp = client.post("/api/does-not-matter", json={})
        assert resp.status_code == 503
        assert resp.json()["reason"] == "recovery_required"
        assert client.get("/healthz").status_code == 200
        assert client.get("/healthz").json()["migration"] == "RECOVERY_REQUIRED"


def test_http_read_only_not_intercepted_when_active(maint_file):
    from backend.main import app

    _write_active(maint_file)
    with TestClient(app) as client:
        assert client.get("/healthz").json()["migration"] == "ACTIVE"
        assert client.get("/api/settings").status_code != 503


def test_http_write_passes_when_inactive(maint_file):
    from backend.main import app

    with TestClient(app) as client:
        # 没有维护文件、库里也没有非 success 迁移行 ⇒ INACTIVE，不拦 HTTP
        resp = client.post("/api/does-not-matter", json={})
        assert resp.status_code != 503


# ----------------------------------------------------------------- PT-15 入口 2
def test_scheduler_round_stops_without_reading_twice(maint_file, monkeypatch):
    _write_active(maint_file)
    calls = {"judge": 0, "cycle": 0}
    real_judge = gate.judge

    def counting_judge():
        calls["judge"] += 1
        return real_judge()

    async def fake_cycle(**kwargs):
        calls["cycle"] += 1
        return {"ok": True}

    monkeypatch.setattr(gate, "judge", counting_judge)
    monkeypatch.setattr(manager, "run_cycle", fake_cycle)
    monkeypatch.setattr(manager, "state", manager.RuntimeState())

    import asyncio

    asyncio.run(manager.job_cycle())
    assert calls["judge"] == 1          # 每轮只判定一次，调度器内不重复读文件
    assert calls["cycle"] == 0          # 非 INACTIVE：本轮不运行
    assert manager.state.phase == "stopped"
    assert manager.state.maintenance_blocked is True
    assert manager.state.maintenance_reason == "maintenance"


# ----------------------------------------------------------------- PT-15 入口 3
def test_cli_write_refused_and_allow_write_cannot_bypass(maint_file, capsys):
    _write_active(maint_file)
    assert cli.main(["snapshot"]) == cli.EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "maintenance"

    assert cli.main(["snapshot", "--allow-write"]) == cli.EXIT_REFUSED


def test_cli_read_command_allowed_when_active(maint_file, capsys):
    _write_active(maint_file)
    assert cli.main(["status"]) == cli.EXIT_OK
    assert json.loads(capsys.readouterr().out)["maintenance"] == "ACTIVE"


# ----------------------------------------------------------------- PT-15 入口 4
def test_write_guard_reasons(maint_file):
    _write_active(maint_file)
    with pytest.raises(pool.WriteRefused) as exc:
        with gate.write_guard():
            pass
    assert exc.value.reason == "maintenance"

    _write_corrupt(maint_file)
    with pytest.raises(pool.WriteRefused) as exc:
        with gate.write_guard():
            pass
    assert exc.value.reason == "recovery_required"


def test_write_guard_requires_pragmas(maint_file, monkeypatch):
    import sqlite3

    raw = sqlite3.connect(str(db.config.DB_PATH), check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys=OFF")
    raw.execute("PRAGMA recursive_triggers=OFF")
    monkeypatch.setattr(db, "connect", lambda: raw)
    try:
        with pytest.raises(pool.WriteRefused) as exc:
            with gate.write_guard():
                pass
        assert exc.value.reason == "pragmas_not_enabled"
    finally:
        raw.close()


def test_db_connection_pragmas_are_on():
    conn = db.connect()
    assert int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 1
    assert int(conn.execute("PRAGMA recursive_triggers").fetchone()[0]) == 1


# ----------------------------------------------------------------- PT-15 入口 5
def test_startup_blocks_before_init_on_active(maint_file):
    _write_active(maint_file)
    called = {"init": 0}
    import backend.main as main_mod

    real_init = db.init_db
    main_mod.db.init_db = lambda: called.__setitem__("init", called["init"] + 1)
    try:
        info = main_mod.startup_maintenance_state()
    finally:
        main_mod.db.init_db = real_init
    assert info["blocked"] is True
    assert info["state"] == "ACTIVE"
    assert called["init"] == 0                       # 判定先于 init_db
    with pytest.raises(pool.PoolClosed):
        db.connect()                                 # 写入闸已置位


def test_startup_enters_recovery_on_corrupt(maint_file):
    _write_corrupt(maint_file)
    import backend.main as main_mod

    info = main_mod.startup_maintenance_state()
    assert info["blocked"] is True
    assert info["reason"] == "recovery_required"
    assert gate.judge()[0] is MaintState.RECOVERY_REQUIRED


def test_startup_proceeds_on_inactive(maint_file):
    import backend.main as main_mod

    info = main_mod.startup_maintenance_state()
    assert info["blocked"] is False
    assert info["state"] == "INACTIVE"


# ------------------------------------------------------------- B 类：连接池/dead
def test_pool_dead_after_block_and_readonly_still_serves(maint_file):
    _write_active(maint_file)
    gate.apply_state(MaintState.ACTIVE)
    assert pool.pool_dead() is True
    with pytest.raises(pool.PoolClosed):
        db.connect()
    # 只读路径退化为 mode=ro + query_only，读请求仍可服务
    assert db.rows("SELECT 1 AS x")[0]["x"] == 1
    assert db.readonly_query_only_ok() is True


def _fake_halt_migration():
    """模拟 P-1.6：定义在 backend.database.migrate 里的 halt_migration 可调用对象。"""

    def halt_migration():
        return None

    halt_migration.__module__ = pool._TRUSTED_ISSUER_MODULE
    return halt_migration


def _register_fake_issuer() -> object:
    return pool.register_confirmation_issuer(_fake_halt_migration())


def test_halt_tail_requires_confirmed_flags():
    rec = pool.MaintConfirmation("p15-mig", True, True, 1730000000.0)   # 直接实例化
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.halt_tail(rec)
    assert exc.value.code == "no_trusted_issuer"      # P-1.5 没有可信发射器

    token = _register_fake_issuer()
    pool.block_writes(pool.REASON_MAINTENANCE)
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.halt_tail(rec)                            # 没带哨兵 ⇒ 仍无效
    assert exc2.value.code == "unconfirmed"

    ok = pool.mint_confirmation(token, "p15-mig", True, True, now=1730000000.0)
    assert pool.halt_tail(ok) == pool.HALT_TAIL_OK


def test_write_connection_recovers_after_unblock(maint_file):
    _write_active(maint_file)
    db.connect()                                     # 正常态先拿一个句柄
    gate.apply_state(MaintState.ACTIVE)
    with pytest.raises(pool.PoolClosed):
        db.connect()
    gate.release()
    # 解除封锁后旧句柄已被关闭，必须重新打开而不是继续用失效句柄
    before = db.rows("SELECT COUNT(*) AS c FROM logs")[0]["c"]
    db.execute("INSERT INTO logs (ts, level, source, message) VALUES ('x','I','t','t')")
    assert db.rows("SELECT COUNT(*) AS c FROM logs")[0]["c"] == before + 1


# --------------------------------------------------------------- PT-23 两会话
def test_second_session_cannot_write_after_block(maint_file):
    _write_active(maint_file)
    first = db.connect()                       # 会话 1 已 checkout 的连接
    assert first.execute("SELECT 1").fetchone() is not None
    gate.apply_state(MaintState.ACTIVE)
    assert db.open_write_connections() == 0    # 会话 1 的写连接已被关闭
    with pytest.raises((pool.PoolClosed, db.sqlite3.ProgrammingError)):
        first.execute("SELECT 1")              # 已关闭的句柄不能再写

    errors: list[BaseException] = []

    def second_session():
        try:
            db.execute("INSERT INTO logs (ts, level, source, message) VALUES ('x','I','t','t')")
        except BaseException as exc:           # noqa: BLE001 - 断言用
            errors.append(exc)

    thread = threading.Thread(target=second_session)
    thread.start()
    thread.join()
    assert errors and isinstance(errors[0], pool.PoolClosed)
    # write_guard 同样拦住第二个会话
    with pytest.raises(pool.WriteRefused):
        gate.guard_write()


def test_pool_module_does_not_read_maintenance_file():
    src = Path(pool.__file__).read_text(encoding="utf-8")
    for token in ("MAINT_FILE", "read_maint_file", "maintenance.json",
                  "maintenance_state", "import maint"):
        assert token not in src, f"B 类模块不得出现 {token}"


# --------------------------------------------------------------- PT-16 停写排空
def test_confirm_writes_blocked_conditions(maint_file):
    _write_active(maint_file)
    conn = db.connect()
    conn.execute("SELECT 1")
    ok, detail = pool.confirm_writes_blocked()
    assert ok is False                               # 尚未置位写入闸
    assert detail["open_write_connections"] == 1
    gate.apply_state(MaintState.ACTIVE)
    ok2, detail2 = pool.confirm_writes_blocked()
    assert ok2 is True
    assert detail2["open_write_connections"] == 0
    assert detail2["readonly_query_only"] is True


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds: float):
        self.t += seconds


def _drain_via(monkeypatch, maint_file, *, checkpoint, holders, blocked=True):
    _write_active(maint_file)
    if blocked:
        gate.apply_state(MaintState.ACTIVE)
    clock = _FakeClock()
    return gate.drain(rounds=2, min_interval=10.0, clock=clock, sleeper=clock.sleep,
                      checkpoint=checkpoint, holders=holders), clock


def test_drain_ok_requires_two_rounds_with_interval(maint_file, monkeypatch):
    result, clock = _drain_via(monkeypatch, maint_file,
                               checkpoint=lambda: (0, 0, 0), holders=lambda: [])
    assert result.ok is True
    assert result.code == gate.DRAIN_OK
    assert len(result.rounds) == 2
    assert result.rounds[1]["gap"] >= 10.0
    assert clock.t >= 10.0


@pytest.mark.parametrize("checkpoint", [(0, 1, 1), (1, 0, 0), None])
def test_drain_fails_on_bad_checkpoint(maint_file, monkeypatch, checkpoint):
    result, _ = _drain_via(monkeypatch, maint_file,
                           checkpoint=lambda: checkpoint, holders=lambda: [])
    assert result.ok is False
    assert result.code == gate.DRAIN_FAILED
    assert result.rounds[0]["checkpoint_ok"] is False


def test_drain_fails_on_external_holder(maint_file, monkeypatch):
    result, _ = _drain_via(monkeypatch, maint_file,
                           checkpoint=lambda: (0, 0, 0), holders=lambda: ["1234:5"])
    assert result.ok is False
    assert result.rounds[0]["holders_ok"] is False


def test_drain_fails_without_write_block_and_skips_checkpoint(maint_file, monkeypatch):
    calls = {"checkpoint": 0}

    def checkpoint():
        calls["checkpoint"] += 1
        return (0, 0, 0)

    result, _ = _drain_via(monkeypatch, maint_file, checkpoint=checkpoint,
                           holders=lambda: [], blocked=False)
    assert result.ok is False
    assert calls["checkpoint"] == 0          # 未确认停写时不得执行 checkpoint（写操作）
    assert result.rounds[0]["writes_blocked"]["writes_blocked"] is False


def test_wal_checkpoint_truncate_real(tmp_path):
    path = tmp_path / "c.db"
    conn = db.sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.commit()
    conn.close()
    assert gate.wal_checkpoint_truncate(path) == (0, 0, 0)


# ============================================ P-1.5 初审修复：新增回归测试
# ---------------------------------------------------------------- 问题 1：竞态
def test_block_cannot_overtake_inflight_checkout(maint_file, monkeypatch):
    """封锁不得抢跑：在途 checkout 未出同步边界前，block_writes 必须等待。"""
    events = {"inside": threading.Event(), "go": threading.Event()}
    real_connect = db.sqlite3.connect

    def slow_connect(*args, **kwargs):
        events["inside"].set()
        events["go"].wait(5.0)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(db.sqlite3, "connect", slow_connect)
    holder: dict = {}

    def checkout():
        try:
            holder["conn"] = db.connect()
        except BaseException as exc:            # noqa: BLE001 - 记录而非吞掉
            holder["err"] = exc

    worker = threading.Thread(target=checkout)
    worker.start()
    assert events["inside"].wait(5.0), "写连接尚未进入创建阶段"

    blocked = threading.Event()

    def blocker():
        pool.block_writes(pool.REASON_MAINTENANCE)
        blocked.set()

    thread_block = threading.Thread(target=blocker)
    thread_block.start()
    # 封锁操作不得在在途 checkout 之前完成（否则就是旧实现的竞态窗口）
    assert blocked.wait(0.3) is False
    events["go"].set()
    worker.join(5)
    thread_block.join(5)
    assert blocked.is_set() is True
    assert "err" not in holder and holder.get("conn") is not None
    assert db.open_write_connections() == 0        # 封锁完成时已关闭刚登记的连接
    with pytest.raises(pool.PoolClosed):
        db.connect()


def test_no_write_connection_survives_block_under_race(maint_file):
    """8 线程并发 checkout 与封锁对撞：封锁后不得存在可用写连接。"""
    attempts = 8
    barrier = threading.Barrier(attempts + 1)
    lock = threading.Lock()
    results: list[tuple[str, float]] = []

    def worker():
        barrier.wait()
        started = time.perf_counter()
        try:
            conn = db.connect()
            conn.execute("INSERT INTO logs (ts, level, source, message) "
                         "VALUES ('x','I','t','t')")
            outcome = "ok"
        except BaseException as exc:            # noqa: BLE001 - 记录而非吞掉
            outcome = type(exc).__name__
        with lock:
            results.append((outcome, started))

    threads = [threading.Thread(target=worker) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    barrier.wait()                              # 所有线程同时进入竞态窗口
    pool.block_writes(pool.REASON_MAINTENANCE)
    blocked_at = time.perf_counter()
    for thread in threads:
        thread.join(10)
    late_writes = [r for r in results if r[0] == "ok" and r[1] > blocked_at]
    assert late_writes == []
    assert db.open_write_connections() == 0
    assert pool.writes_blocked() is True
    with pytest.raises(pool.PoolClosed):
        db.connect()


# ------------------------------------------------------- 问题 2：写入覆盖范围
def test_every_write_helper_refused_after_block(maint_file):
    _write_active(maint_file)
    gate.apply_state(MaintState.ACTIVE)
    logs_before = db.rows("SELECT COUNT(*) AS c FROM logs")[0]["c"]   # 只读仍可用
    settings_before = db.get("test_interval_minutes")

    with pytest.raises(pool.PoolClosed):
        db.execute("INSERT INTO logs (ts, level, source, message) VALUES ('x','I','t','t')")
    with pytest.raises(pool.PoolClosed):
        db.set_setting("test_interval_minutes", "99")
    with pytest.raises(pool.WriteRefused):
        with db.transaction() as conn:
            conn.execute("INSERT INTO logs (ts, level, source, message) VALUES ('x','I','t','t')")
    with pytest.raises(pool.PoolClosed):
        db.record_run_event("start")
    db.append_log("INFO", "测试", "封锁期日志")                        # 不抛出、不落库

    assert db.rows("SELECT COUNT(*) AS c FROM logs")[0]["c"] == logs_before
    assert db.get("test_interval_minutes") == settings_before
    assert db.row("SELECT 1 AS x")["x"] == 1


def test_recovery_reason_is_preserved_after_block(maint_file):
    _write_corrupt(maint_file)
    gate.apply_state(MaintState.RECOVERY_REQUIRED)
    with pytest.raises(pool.PoolClosed) as exc:
        db.connect()
    assert exc.value.reason == "recovery_required"
    with pytest.raises(pool.WriteRefused) as exc2:
        with gate.write_guard():
            pass
    assert exc2.value.reason == "recovery_required"


# ------------------------------------------------------- 问题 3：排空参数下限
@pytest.mark.parametrize("rounds,min_interval", [(1, 10.0), (0, 10.0), (2, 0.0), (2, 9.9), (1, 0.0)])
def test_drain_parameters_cannot_weaken_floor(maint_file, rounds, min_interval):
    _write_active(maint_file)
    gate.apply_state(MaintState.ACTIVE)
    clock = _FakeClock()
    result = gate.drain(rounds=rounds, min_interval=min_interval, clock=clock,
                        sleeper=clock.sleep, checkpoint=lambda: (0, 0, 0), holders=lambda: [])
    assert result.ok is True
    assert len(result.rounds) >= 2                 # 成功必定代表 ≥2 次完整检测
    assert result.interval >= 10.0
    assert result.rounds[1]["gap"] >= 10.0
    assert result.coerced is True
    assert result.requested_rounds == rounds and result.requested_interval == min_interval


def test_drain_legal_parameters_not_coerced(maint_file):
    _write_active(maint_file)
    gate.apply_state(MaintState.ACTIVE)
    clock = _FakeClock()
    result = gate.drain(rounds=3, min_interval=10.0, clock=clock, sleeper=clock.sleep,
                        checkpoint=lambda: (0, 0, 0), holders=lambda: [])
    assert result.ok is True and result.coerced is False
    assert len(result.rounds) == 3
    assert all(r["gap"] >= 10.0 for r in result.rounds if "gap" in r)


# --------------------------------------------------- 问题 4：失败保持封锁
def test_drain_failure_keeps_gate_blocked_and_blocks_continue(maint_file):
    _write_active(maint_file)
    gate.apply_state(MaintState.ACTIVE)
    clock = _FakeClock()
    result = gate.drain(rounds=2, min_interval=10.0, clock=clock, sleeper=clock.sleep,
                        checkpoint=lambda: (0, 1, 1), holders=lambda: [])
    assert result.ok is False
    assert result.code == gate.DRAIN_FAILED
    assert result.halt_code == "drain_failed"
    assert result.halt_pending is True
    assert result.continue_allowed is False
    assert result.halt_request()["code"] == "drain_failed"
    assert pool.writes_blocked() is True           # 不自动解除
    with pytest.raises(pool.PoolClosed):
        db.connect()
    with pytest.raises(gate.DrainFailed):
        gate.assert_drain_ok(result)


def test_drain_failure_blocks_even_if_gate_was_open(maint_file):
    clock = _FakeClock()
    result = gate.drain(rounds=2, min_interval=10.0, clock=clock, sleeper=clock.sleep,
                        checkpoint=lambda: (0, 0, 0), holders=lambda: ["9:9"])
    assert result.ok is False
    assert pool.writes_blocked() is True           # 失败必须落到封锁态
    with pytest.raises(pool.PoolClosed):
        db.connect()


def test_assert_drain_ok_passes_on_success(maint_file):
    _write_active(maint_file)
    gate.apply_state(MaintState.ACTIVE)
    clock = _FakeClock()
    result = gate.drain(rounds=2, min_interval=10.0, clock=clock, sleeper=clock.sleep,
                        checkpoint=lambda: (0, 0, 0), holders=lambda: [])
    assert gate.assert_drain_ok(result) is result


# --------------------------------------------------------- 问题 5：halt_tail 契约
def test_halt_tail_rejects_unconfirmed_objects(maint_file):
    _write_active(maint_file)
    candidates = [
        {"maintenance_ok": True, "writes_blocked": True},
        type("Rec", (), {"maintenance_ok": True, "writes_blocked": True})(),
        pool.MaintConfirmation("m", True, True, 1.0, source="manual"),
        pool.MaintConfirmation("", True, True, 1.0),
        pool.MaintConfirmation("m", 1, True, 1.0),          # 非 True 的布尔真值
        pool.MaintConfirmation("m", True, 1, 1.0),
        pool.MaintConfirmation("m", True, True, "now"),
    ]
    for candidate in candidates:
        with pytest.raises(pool.HaltPreconditionFailed):
            pool.halt_tail(candidate)


def test_halt_tail_accepts_only_confirmation_from_halt_migration():
    pool.block_writes(pool.REASON_MAINTENANCE)
    token = _register_fake_issuer()
    rec = pool.mint_confirmation(token, "mig-42", True, True)
    assert rec.source == "halt_migration"
    assert rec._issuer == pool._TRUSTED_ISSUER_MODULE
    assert pool.halt_tail(rec) == pool.HALT_TAIL_OK


# ============================== P-1.5 最终修复：凭据真实性 + 在途写入封锁
# --------------------------------------------------- 问题 1：凭据真实性
def test_direct_instantiation_of_confirmation_is_never_valid():
    _register_fake_issuer()
    forged = pool.MaintConfirmation("m", True, True, 1.0)          # 无哨兵
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.halt_tail(forged)
    assert exc.value.code == "unconfirmed"

    class Fake(pool.MaintConfirmation):                          # 子类也不能绕过
        pass

    with pytest.raises(pool.HaltPreconditionFailed):
        pool.halt_tail(Fake("m", True, True, 1.0))


def test_public_factory_is_gone_and_mint_needs_registered_token():
    assert not hasattr(pool, "new_confirmation")
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.mint_confirmation(object(), "m", True, True)
    assert exc.value.code == "untrusted_issuer"
    with pytest.raises(pool.HaltPreconditionFailed):
        pool.mint_confirmation(None, "m", True, True)


def test_mint_refuses_when_writes_not_actually_blocked():
    token = _register_fake_issuer()
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.mint_confirmation(token, "m", True, True)
    assert exc.value.code == "unconfirmed_state"


def test_only_migrate_module_may_register_issuer():
    def foreign_halt_migration():
        return None

    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.register_confirmation_issuer(foreign_halt_migration)
    assert exc.value.code == "untrusted_issuer_module"
    assert pool._ISSUERS == {}


def test_mint_refuses_unconfirmed_state():
    pool.block_writes(pool.REASON_MAINTENANCE)
    token = _register_fake_issuer()
    for ok, blocked in ((False, True), (True, False), (1, True)):
        with pytest.raises(pool.HaltPreconditionFailed) as exc:
            pool.mint_confirmation(token, "m", ok, blocked)
        assert exc.value.code == "unconfirmed_state"


def test_halt_tail_fails_closed_when_no_issuer_registered(maint_file):
    pool.block_writes(pool.REASON_MAINTENANCE)
    token = _register_fake_issuer()
    rec = pool.mint_confirmation(token, "mig-7", True, True)
    assert pool.halt_tail(rec) == pool.HALT_TAIL_OK
    pool.reset_for_tests()                                       # 重新回到 P-1.5 常态
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.halt_tail(rec)
    assert exc.value.code == "no_trusted_issuer"


def test_halt_tail_rejects_unconfirmed_objects_with_codes(maint_file):
    _write_active(maint_file)
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.halt_tail({"maintenance_ok": True, "writes_blocked": True})
    assert exc.value.code == "not_confirmation"

    pool.block_writes(pool.REASON_MAINTENANCE)
    token = _register_fake_issuer()
    rec = pool.mint_confirmation(token, "m", True, True)
    object.__setattr__(rec, "writes_blocked", False)             # 篡改字段
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.halt_tail(rec)
    assert exc2.value.code == "unconfirmed"


# ----------------------------------------------- 问题 2：在途写事务封锁
@pytest.mark.parametrize("pause_point", ["after_write", "before_write"])
def test_inflight_transaction_cannot_commit_after_block(maint_file, pause_point):
    entered = threading.Event()
    resume = threading.Event()
    outcome: dict = {}

    def session1():
        try:
            with db.transaction() as conn:            # 会话 1：真的拿到写连接并开启事务
                if pause_point == "before_write":
                    entered.set()
                    assert resume.wait(5.0)
                    conn.execute("INSERT INTO logs (ts, level, source, message) "
                                 "VALUES ('x','I','t','t')")
                else:
                    conn.execute("INSERT INTO logs (ts, level, source, message) "
                                 "VALUES ('x','I','t','t')")
                    entered.set()
                    assert resume.wait(5.0)
            outcome["commit"] = "ok"
        except BaseException as exc:                  # noqa: BLE001 - 记录真实异常
            outcome["commit"] = type(exc).__name__

    worker = threading.Thread(target=session1)
    worker.start()
    assert entered.wait(5.0), "会话 1 未进入事务暂停点"
    assert db.active_write_transactions() == 1        # 事务确实在途

    pool.block_writes(pool.REASON_MAINTENANCE)        # 会话 2：触发全局封锁
    assert pool.writes_blocked() is True
    assert db.open_write_connections() == 0
    assert db.active_write_transactions() == 0        # 在途事务已被封锁中止
    assert pool.confirm_writes_blocked()[0] is True
    with pytest.raises(pool.PoolClosed):
        db.connect()                                  # 新会话 checkout 被拒
    assert db.rows("SELECT 1 AS x")[0]["x"] == 1      # 只读会话仍可用

    resume.set()
    worker.join(5)
    assert outcome["commit"] in ("WriteRefused", "ProgrammingError")
    assert outcome["commit"] != "ok"
    # 数据库最终状态：封锁后没有发生写入提交
    rows = db.rows("SELECT COUNT(*) AS c FROM logs WHERE message='t'")[0]["c"]
    assert rows == 0
    with pytest.raises((pool.WriteRefused, pool.PoolClosed)):
        with db.transaction():
            pass                                      # 封锁后新事务也拿不到写权
