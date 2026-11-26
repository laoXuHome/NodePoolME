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


def test_halt_tail_requires_confirmed_flags():
    ok = type("Rec", (), {"maintenance_ok": True, "writes_blocked": True})()
    assert pool.halt_tail(ok) == pool.HALT_TAIL_OK
    for rec in (
        type("Rec", (), {"maintenance_ok": False, "writes_blocked": True})(),
        type("Rec", (), {"maintenance_ok": True, "writes_blocked": False})(),
        type("Rec", (), {})(),
    ):
        with pytest.raises(pool.HaltPreconditionFailed):
            pool.halt_tail(rec)


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
