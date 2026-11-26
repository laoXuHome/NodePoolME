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

from backend.database import backup, cli, db, gate, maint, migrate, pool  # noqa: E402
from backend.database.maint import MaintState  # noqa: E402
from backend.scheduler import manager  # noqa: E402

ACTIVE_BLOB = {"maintenance": True, "migration_id": "p15-mig", "since": 1730000000,
               "phase": "PREPARED"}


_ORIGINAL_HALT = migrate.halt_migration      # 伪造发射器测试会 exec 覆盖模块里的同名函数


@pytest.fixture(autouse=True)
def _clean_gate():
    pool.reset_for_tests()
    yield
    # 还原本模块里被 exec 注入的伪造 `halt_migration`，避免污染后续测试模块
    migrate.__dict__["halt_migration"] = _ORIGINAL_HALT
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
        assert resp.json()["reason"] == "recovery_required"
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
        assert client.get("/healthz").json()["migration"] == "RECOVERY_REQUIRED"
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
def test_cli_import_refused_when_maintenance_active(maint_file, capsys):
    """简化版 CLI：维护态在跑时拒绝 import；备份导出是只读操作，不受维护态限制。"""
    _write_active(maint_file)
    assert cli.main(["import", "--file", "x.db"]) == cli.EXIT_REFUSED
    payload = json.loads(capsys.readouterr().out)
    assert payload["code"] == "maintenance_not_inactive"


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
    from backend.database import bootstrap

    decision = bootstrap.inspect_startup()
    bootstrap.apply_startup_gate(decision)
    assert decision.inactive is False
    assert decision.state is MaintState.RECOVERY_REQUIRED
    with pytest.raises(pool.PoolClosed):
        db.connect()                                 # 写入闸已置位


def test_startup_enters_recovery_on_corrupt(maint_file):
    _write_corrupt(maint_file)
    from backend.database import bootstrap

    decision = bootstrap.inspect_startup()
    bootstrap.apply_startup_gate(decision)
    assert decision.inactive is False
    assert decision.code == "maintenance_file_invalid"
    assert gate.judge()[0] is MaintState.RECOVERY_REQUIRED


def test_startup_proceeds_on_inactive(maint_file):
    from backend.database import bootstrap

    decision = bootstrap.inspect_startup()
    bootstrap.apply_startup_gate(decision)
    assert decision.inactive is True
    assert decision.state is MaintState.INACTIVE


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


def _module_halt_migration():
    import importlib

    return importlib.import_module(pool._TRUSTED_ISSUER_MODULE)


def _fake_halt_migration():
    """模拟 P-1.6：在 backend.database.migrate 的模块命名空间里定义 halt_migration。

    真实 P-1.6 会把函数写在 migrate.py 里；这里用 ``exec(..., module.__dict__)``
    复刻同一事实——函数对象的 ``__globals__`` 就是该模块的 ``__dict__``。
    同一个进程内重复调用返回**同一个函数对象**（模块属性已是可信定义时就复用），
    以便"同一对象重复注册 = 幂等"成立；需要新对象时用
    :func:`_replace_module_halt_migration`。
    """
    import types

    module = _module_halt_migration()
    existing = getattr(module, pool._TRUSTED_ISSUER_FUNC, None)
    if (isinstance(existing, types.FunctionType)
            and getattr(existing, "__globals__", None) is module.__dict__):
        return existing
    exec(compile(f"def {pool._TRUSTED_ISSUER_FUNC}():\n    return None\n",
                 module.__file__ or "<migrate>", "exec"),
         module.__dict__)                     # noqa: S102 - 测试内构造可信发射器
    return getattr(module, pool._TRUSTED_ISSUER_FUNC)


def _replace_module_halt_migration():
    """同名、同模块、**另一个函数对象**：重新定义并覆盖模块属性后返回新对象。"""
    module = _module_halt_migration()
    exec(compile(f"def {pool._TRUSTED_ISSUER_FUNC}():\n    return 'replacement'\n",
                 module.__file__ or "<migrate>", "exec"),
         module.__dict__)                     # noqa: S102 - 故意替换同名函数
    return getattr(module, pool._TRUSTED_ISSUER_FUNC)


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
    assert rec._issuer == pool._ISSUER_ID == "backend.database.migrate:halt_migration"
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


# ================== P-1.5 最终安全修复：提交原子性 + 可信令牌身份绑定
# ------------------------------------------------- 问题 1：提交与封锁原子化
def test_block_cannot_interleave_between_commit_check_and_commit(maint_file, monkeypatch):
    """关键竞争窗口：会话 1 已在"复核 + commit"临界区内、``conn.commit()`` 尚未执行。

    时序：
    T0 会话 1：transaction() 取写连接 → 登记在途事务 → BEGIN IMMEDIATE → 写入 →
      进入 pool.commit_slot()（持 _blocking_lock），停在复核之前（暂停点 P）。
    T1 主线程：确认暂停（in_critical 已置位、active_write_transactions()==1）。
    T2 会话 2（独立线程）：pool.block_writes() —— 先置位封锁意图（不取锁），再等锁。
    T3 主线程：断言 block_writes 0.3 秒内**没有返回**（临界区与封锁互斥），
       且此刻 block_pending()=True（意图已声明）、writes_blocked()=False（尚未落闸）。
    T4 主线程放行 → 会话 1 的复核读到封锁意图 → WriteRefused("writes_blocked")，
       事务回滚、登记注销、释放临界区。
    T5 会话 2 取得锁，中止/关闭并返回。
    """
    in_critical = threading.Event()
    resume = threading.Event()
    blocked_done = threading.Event()
    outcome: dict = {}
    real_assert = db._assert_txn_committable

    def pausing_assert(entry):
        in_critical.set()                 # 已进入临界区；复核尚未完成
        assert resume.wait(5.0), "会话 1 未被放行"
        return real_assert(entry)         # 此刻封锁意图已置位 ⇒ 应拒绝

    monkeypatch.setattr(db, "_assert_txn_committable", pausing_assert)

    def session1():
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO logs (ts, level, source, message) "
                             "VALUES ('x','I','t','t')")
            outcome["commit"] = "ok"
        except BaseException as exc:                      # noqa: BLE001 - 记录真实异常
            outcome["commit"] = type(exc).__name__
        finally:
            outcome["finished"] = True

    def session2():
        pool.block_writes(pool.REASON_MAINTENANCE)
        blocked_done.set()

    worker = threading.Thread(target=session1, daemon=True)
    worker.start()
    assert in_critical.wait(5.0), "会话 1 未进入提交临界区"
    assert db.active_write_transactions() == 1          # 事务确实在途、尚未提交

    blocker = threading.Thread(target=session2, daemon=True)
    blocker.start()
    assert blocked_done.wait(0.3) is False, "提交临界区未能挡住封锁：两者没有互斥"
    assert pool.block_pending() is True                 # 封锁意图已声明
    assert pool.writes_blocked() is False               # 但闸门尚未落下（仍在等锁）

    resume.set()
    worker.join(10)
    blocker.join(10)
    assert outcome["commit"] == "WriteRefused"          # 检查通过后仍被封锁拦下
    assert blocked_done.is_set() is True
    assert pool.writes_blocked() is True
    assert db.open_write_connections() == 0
    assert db.active_write_transactions() == 0
    assert pool.confirm_writes_blocked()[0] is True
    with pytest.raises(pool.PoolClosed):
        db.connect()                                    # 新会话 checkout 被拒
    assert db.rows("SELECT 1 AS x")[0]["x"] == 1        # 只读会话仍可用
    assert db.rows("SELECT COUNT(*) AS c FROM logs WHERE message='t'")[0]["c"] == 0
    with pytest.raises((pool.WriteRefused, pool.PoolClosed)):
        with db.transaction():
            pass                                        # 封锁后新事务拿不到写权
    assert db.rows("SELECT COUNT(*) AS c FROM logs WHERE message='t'")[0]["c"] == 0


def test_commit_inside_critical_section_serializes_before_block_return(maint_file, monkeypatch):
    """同一临界区的另一半：提交先到（复核已通过），封锁必须等 commit 落库才返回。

    验证"封锁返回 = 静默点"：block_writes 在 commit 进行中无法返回；它返回之后
    数据库中只有那次"封锁返回之前"完成的提交，且此后不可能再有任何写入落库。
    """
    checked = threading.Event()
    resume = threading.Event()
    blocked_done = threading.Event()
    outcome: dict = {}
    real_assert = db._assert_txn_committable

    def pausing_assert(entry):
        real_assert(entry)                # 复核通过（此刻尚未封锁）
        checked.set()
        assert resume.wait(5.0), "会话 1 未被放行"

    monkeypatch.setattr(db, "_assert_txn_committable", pausing_assert)

    def session1():
        try:
            with db.transaction() as conn:
                conn.execute("INSERT INTO logs (ts, level, source, message) "
                             "VALUES ('x','I','t','t')")
            outcome["commit"] = "ok"
        except BaseException as exc:                      # noqa: BLE001
            outcome["commit"] = type(exc).__name__

    def session2():
        pool.block_writes(pool.REASON_MAINTENANCE)
        blocked_done.set()

    worker = threading.Thread(target=session1, daemon=True)
    worker.start()
    assert checked.wait(5.0), "会话 1 未通过提交复核"
    assert db.active_write_transactions() == 1

    blocker = threading.Thread(target=session2, daemon=True)
    blocker.start()
    assert blocked_done.wait(0.3) is False, "封锁抢进了提交临界区"
    assert pool.block_pending() is True

    resume.set()
    worker.join(10)
    blocker.join(10)
    assert outcome["commit"] == "ok"                    # 提交在封锁返回之前完成
    assert blocked_done.is_set() is True
    assert pool.writes_blocked() is True
    assert pool.confirm_writes_blocked()[0] is True
    assert db.active_write_transactions() == 0

    # 封锁返回后的静默点：唯一一行是"封锁返回之前"那次提交，之后不能再写
    count_at_quiesce = db.rows("SELECT COUNT(*) AS c FROM logs WHERE message='t'")[0]["c"]
    assert count_at_quiesce == 1
    with pytest.raises(pool.PoolClosed):
        db.connect()
    assert db.rows("SELECT 1 AS x")[0]["x"] == 1
    with pytest.raises((pool.WriteRefused, pool.PoolClosed)):
        with db.transaction():
            pass
    assert db.rows("SELECT COUNT(*) AS c FROM logs WHERE message='t'")[0]["c"] == 1


def test_inflight_registration_cannot_slip_past_block(maint_file):
    """登记也必须在临界区内：封锁完成后不会凭空出现一条"可提交"的在途事务。"""
    pool.block_writes(pool.REASON_MAINTENANCE)
    with pytest.raises(pool.WriteRefused):
        with pool.commit_slot():
            pass
    assert db.active_write_transactions() == 0

    raw = db.sqlite3.connect(":memory:")
    try:
        with pytest.raises(pool.WriteRefused):
            db._register_txn(raw)          # 绕过 transaction() 直接登记也不行
        assert db.active_write_transactions() == 0
    finally:
        raw.close()


# --------------------------------------------- 问题 2：可信令牌身份绑定
def test_forged_module_attribute_cannot_register_issuer():
    """只改 ``__module__`` 字符串的普通函数不能拿到可信令牌。"""

    def foreign():
        return None

    foreign.__module__ = pool._TRUSTED_ISSUER_MODULE          # 伪造模块名
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.register_confirmation_issuer(foreign)
    assert exc.value.code == "untrusted_issuer_identity"      # __globals__ 身份不符
    assert pool._ISSUERS == {}

    # 同一模块命名空间但函数名不是 halt_migration，同样不认
    import importlib

    module = importlib.import_module(pool._TRUSTED_ISSUER_MODULE)
    # 注入名刻意避开模块里已有的任何符号（曾用 `run` 覆盖 `migrate.run`，污染后续测试）
    exec(compile("def not_halt():\n    return None\n", module.__file__ or "<migrate>", "exec"),
         module.__dict__)                                     # noqa: S102
    try:
        with pytest.raises(pool.HaltPreconditionFailed) as exc2:
            pool.register_confirmation_issuer(module.not_halt)
    finally:
        module.__dict__.pop("not_halt", None)
    assert exc2.value.code == "untrusted_issuer_identity"

    # 非函数（内建/实例）一律拒绝
    for candidate in (len, object(), "backend.database.migrate"):
        with pytest.raises(pool.HaltPreconditionFailed):
            pool.register_confirmation_issuer(candidate)
    assert pool._ISSUERS == {}


def test_registry_stores_token_object_identity():
    """注册表保留令牌对象本身，重复注册是幂等的（不会产生第二个可用令牌）。"""
    token = _register_fake_issuer()
    record = pool._ISSUERS[id(token)]
    assert record.token is token                             # 存的是对象，不是只存 id
    assert record.issuer == pool._ISSUER_ID
    assert len(pool._ISSUERS) == 1

    again = pool.register_confirmation_issuer(_fake_halt_migration())
    assert again is token                                    # 同一发射器 → 同一令牌对象
    assert len(pool._ISSUERS) == 1
    assert pool._lookup_token(again) is record


def test_unregistered_and_replaced_token_cannot_mint():
    """未注册令牌、以及"id 复用/登记项被替换"的令牌都不能签发。"""
    token = _register_fake_issuer()
    stranger = object()
    assert pool._lookup_token(stranger) is None
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.mint_confirmation(stranger, "m", True, True)
    assert exc.value.code == "untrusted_issuer"

    # 身份混淆 1：同一 id 位置上的登记项被换成另一个令牌对象（真实令牌被替换）
    pool._ISSUERS[id(token)] = pool._IssuerRecord(token=object(),
                                                  issuer=pool._ISSUER_ID,
                                                  function=_fake_halt_migration(),
                                                  registered_at=1.0)
    assert pool._lookup_token(token) is None                 # 同 id 但非同一对象
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.mint_confirmation(token, "m", True, True)
    assert exc2.value.code == "untrusted_issuer"

    # 身份混淆 2：登记表被清空后另一个对象占位 ⇒ 旧令牌同样失效
    replacement = object()
    pool._ISSUERS.clear()
    pool._ISSUERS[id(replacement)] = pool._IssuerRecord(token=replacement,
                                                        issuer=pool._ISSUER_ID,
                                                        function=_fake_halt_migration(),
                                                        registered_at=2.0)
    with pytest.raises(pool.HaltPreconditionFailed) as exc3:
        pool.mint_confirmation(token, "m", True, True)
    assert exc3.value.code == "untrusted_issuer"
    with pytest.raises(pool.HaltPreconditionFailed) as exc4:
        pool.mint_confirmation(replacement, "m", True, True)  # 替换后的令牌本体已注册
    assert exc4.value.code == "unconfirmed_state"             # 但此刻尚未封锁


def test_confirmation_is_bound_to_issuer_token_not_issuer_string(maint_file):
    """凭据绑定具体发射器令牌身份，不只是 issuer 字符串出现在注册表里。"""
    pool.block_writes(pool.REASON_MAINTENANCE)
    token = _register_fake_issuer()
    rec = pool.mint_confirmation(token, "mig-9", True, True)
    assert pool.halt_tail(rec) == pool.HALT_TAIL_OK

    # 1) 字段全对、只缺真实令牌对象（即便拿到模块哨兵也无效）
    forged = pool.MaintConfirmation("mig-9", True, True, 1.0,
                                    _token=pool._ISSUER_TOKEN,
                                    _issuer=pool._ISSUER_ID,
                                    _issuer_token=object())
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.halt_tail(forged)
    assert exc.value.code == "unconfirmed"

    # 2) 同 issuer 字符串、换掉令牌对象 ⇒ 旧凭据立即失效
    replacement = object()
    pool._ISSUERS.clear()
    pool._ISSUERS[id(replacement)] = pool._IssuerRecord(replacement, pool._ISSUER_ID,
                                                        _fake_halt_migration(), 2.0)
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.halt_tail(rec)
    assert exc2.value.code == "unconfirmed"

    # 3) 冒用另一个已注册令牌的对象身份（换 issuer 身份字符串也不认）
    other = pool.mint_confirmation(replacement, "mig-9", True, True)
    object.__setattr__(other, "_issuer", "someone.else:halt_migration")
    with pytest.raises(pool.HaltPreconditionFailed) as exc3:
        pool.halt_tail(other)
    assert exc3.value.code == "unconfirmed"


def test_p16_issuer_handoff_contract(maint_file):
    """P-1.6 可信交接契约（P-1.5 只提供接口，不实现状态机）。

    时序：migrate.py 内 ``_TOKEN = register_confirmation_issuer(halt_migration)``
    → halt_migration 置位封锁、回读维护文件得 maintenance_ok、用
    ``pool.confirm_writes_blocked()[0]`` 得 writes_blocked
    → ``mint_confirmation(_TOKEN, mid, maintenance_ok, writes_blocked)``
    → ``halt_tail(rec)``。
    """
    issuer = _fake_halt_migration()
    token = pool.register_confirmation_issuer(issuer)
    # 封锁尚未成立时不能签发出"已封锁"的凭据
    with pytest.raises(pool.HaltPreconditionFailed) as early:
        pool.mint_confirmation(token, "mig-p16", True, True)
    assert early.value.code == "unconfirmed_state"

    pool.block_writes(pool.REASON_MAINTENANCE)               # halt_migration 第一步
    blocked, detail = pool.confirm_writes_blocked()
    assert blocked is True and detail["active_write_transactions"] == 0
    rec = pool.mint_confirmation(token, "mig-p16", True, blocked)
    assert pool.halt_tail(rec) == pool.HALT_TAIL_OK
    assert rec.migration_id == "mig-p16" and rec._issuer == pool._ISSUER_ID


# ================= P-1.5 最后一项修复：发射器函数对象的身份绑定
def test_same_function_object_registration_is_idempotent():
    """同一函数对象重复注册：返回同一令牌，登记项绑定该函数对象（强引用）。"""
    issuer = _fake_halt_migration()
    token = pool.register_confirmation_issuer(issuer)
    record = pool._ISSUERS[id(token)]
    assert record.token is token
    assert record.function is issuer            # 登记项持有函数对象本体
    assert record.issuer == pool._ISSUER_ID
    assert len(pool._ISSUERS) == 1

    again = pool.register_confirmation_issuer(issuer)
    assert again is token
    assert len(pool._ISSUERS) == 1
    assert pool._ISSUERS[id(again)].function is issuer


def test_same_module_and_name_but_different_function_object_is_rejected():
    """同模块、同函数名、另一个函数对象：拒绝注册，绝不交出原令牌。"""
    original = _fake_halt_migration()
    token = pool.register_confirmation_issuer(original)

    replacement = _replace_module_halt_migration()      # 真实的新函数对象
    assert replacement is not original
    assert replacement.__module__ == original.__module__
    assert replacement.__name__ == original.__name__

    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.register_confirmation_issuer(replacement)
    assert exc.value.code == "issuer_identity_conflict"
    assert len(pool._ISSUERS) == 1
    assert pool._ISSUERS[id(token)].function is original   # 原登记项未被改写
    assert pool._ISSUERS[id(token)].token is token


def test_replaced_module_function_breaks_token_and_confirmation(maint_file):
    """原发射器注册后替换模块里的同名函数：旧令牌不能再签发，旧凭据失效。"""
    pool.block_writes(pool.REASON_MAINTENANCE)
    original = _fake_halt_migration()
    token = pool.register_confirmation_issuer(original)
    rec = pool.mint_confirmation(token, "mig-replace", True, True)
    assert pool.halt_tail(rec) == pool.HALT_TAIL_OK

    replacement = _replace_module_halt_migration()        # 模块属性指向新对象
    assert getattr(_module_halt_migration(), pool._TRUSTED_ISSUER_FUNC) is replacement

    # 1) 用替换后的函数尝试注册：同身份串不同对象 ⇒ 拒绝，拿不到原令牌
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.register_confirmation_issuer(replacement)
    assert exc.value.code == "issuer_identity_conflict"

    # 2) 用替换后的函数尝试签发：没有令牌，直接失败
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.mint_confirmation(replacement, "mig-replace", True, True)
    assert exc2.value.code == "untrusted_issuer"

    # 3) 原令牌仍在注册表里，但绑定已被替换 ⇒ 拒绝签发
    with pytest.raises(pool.HaltPreconditionFailed) as exc3:
        pool.mint_confirmation(token, "mig-replace", True, True)
    assert exc3.value.code == "issuer_replaced"

    # 4) 替换前签发的凭据同样失效
    with pytest.raises(pool.HaltPreconditionFailed) as exc4:
        pool.halt_tail(rec)
    assert exc4.value.code == "unconfirmed"


def test_record_binding_replacement_is_rejected(maint_file):
    """令牌对象、登记记录、发射器对象三者错配 ⇒ 签发与确认都必须失败。"""
    pool.block_writes(pool.REASON_MAINTENANCE)
    module = _module_halt_migration()
    original = _fake_halt_migration()
    token = pool.register_confirmation_issuer(original)
    rec = pool.mint_confirmation(token, "mig-swap", True, True)
    assert pool.halt_tail(rec) == pool.HALT_TAIL_OK

    other = _replace_module_halt_migration()              # 另一个函数对象
    setattr(module, pool._TRUSTED_ISSUER_FUNC, original)  # 模块侧恢复原对象
    # 登记项里的函数对象被改写成 other ⇒ 与模块当前定义不符
    pool._ISSUERS[id(token)] = pool._IssuerRecord(token=token, issuer=pool._ISSUER_ID,
                                                   function=other, registered_at=1.0)
    with pytest.raises(pool.HaltPreconditionFailed) as exc:
        pool.mint_confirmation(token, "mig-swap", True, True)
    assert exc.value.code == "issuer_replaced"
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.halt_tail(rec)
    assert exc2.value.code == "unconfirmed"

    # 反过来：登记项恢复原函数对象、模块属性却指向别的对象 ⇒ 同样失效
    pool._ISSUERS[id(token)] = pool._IssuerRecord(token=token, issuer=pool._ISSUER_ID,
                                                   function=original, registered_at=1.0)
    setattr(module, pool._TRUSTED_ISSUER_FUNC, other)
    with pytest.raises(pool.HaltPreconditionFailed) as exc3:
        pool.mint_confirmation(token, "mig-swap", True, True)
    assert exc3.value.code == "issuer_replaced"
    assert pool._record_binding_live(pool._ISSUERS[id(token)]) is False


def test_no_issuer_registered_keeps_halt_tail_fail_closed(maint_file):
    """P-1.5 不注册发射器：任何形状的凭据都得到 no_trusted_issuer。"""
    assert pool._ISSUERS == {}
    for candidate in (pool.MaintConfirmation("m", True, True, 1.0),
                      type("Rec", (), {"maintenance_ok": True, "writes_blocked": True})(),
                      {"maintenance_ok": True, "writes_blocked": True}):
        with pytest.raises(pool.HaltPreconditionFailed) as exc:
            pool.halt_tail(candidate)
        assert exc.value.code in ("not_confirmation", "no_trusted_issuer")
    pool.block_writes(pool.REASON_MAINTENANCE)
    with pytest.raises(pool.HaltPreconditionFailed) as exc2:
        pool.halt_tail(pool.MaintConfirmation("m", True, True, 1.0))
    assert exc2.value.code == "no_trusted_issuer"
