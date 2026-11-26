"""Application entry point: API, scheduler, bundled frontend."""
import asyncio
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.api import routes
from backend.database import bootstrap, db, gate, instance_lock, maint, migrate, pool
from backend.database.maint import MaintState
from backend.scheduler import manager
from backend.services import fetcher
from backend.services.speed_queue import queue as speed_queue
from backend.subscriptions import builder
from backend.testers import engine as engine_mod

STATIC_DIR = Path(__file__).resolve().parent / "static"

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
GATED_PREFIXES = ("/api", "/sub")

log = logging.getLogger("nodepool")


def setup_logging() -> None:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        config.LOG_DIR / "nodepool.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    if not any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        root.addHandler(handler)
    root.setLevel(logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # D2/D3: the process-lifetime lock is the first action.  A second process on
    # the same STATE_DIR must fail before it can read or write the live database.
    try:
        lock = instance_lock.acquire()
    except instance_lock.AlreadyRunning:
        log.error("同一 STATE_DIR 已有 NodePoolME 实例运行，拒绝启动：%s",
                  instance_lock.lock_path())
        raise SystemExit(2)
    except instance_lock.LockUnsupported as exc:
        log.error("文件系统不支持实例锁，拒绝启动：%s", exc)
        raise SystemExit(2)
    app.state.instance_lock = lock

    try:
        # 启动自检不触碰数据库；失败只禁用迁移入口，不改变恢复判定。
        try:
            state_dir_check = maint.selfcheck_state_dir()
        except Exception as exc:
            log.warning("STATE_DIR 自检异常，迁移入口禁用：%r", exc)
            state_dir_check = None
        app.state.state_dir_check = state_dir_check
        if state_dir_check is not None and not state_dir_check.ok:
            log.warning("STATE_DIR 自检未通过（%s）：迁移入口已禁用，业务照常启动（%s）",
                        state_dir_check.code, state_dir_check.detail)

        # Read maintenance file, block unconditionally, then probe schema and
        # classify.  init_db()/ensure_schema_table_transactional()/record_baseline()
        # are only reachable after a verified INACTIVE decision.
        decision = bootstrap.inspect_startup()
        bootstrap.apply_startup_gate(decision)
        app.state.startup_decision = decision
        app.state.maintenance = {
            "state": decision.state.name,
            "reason": gate.write_reason(decision.state),
            "blocked": not decision.inactive,
            "code": decision.code,
            "phase": decision.phase,
            "db_kind": decision.db_kind,
        }
        blocked = not decision.inactive
        if blocked:
            log.warning("启动恢复态（%s/%s）：写入已封锁，初始化/调度器/引擎未启动",
                        decision.code, decision.db_kind)
        else:
            db.init_db()
            # The new architecture needs one immutable baseline row before a
            # later CLI `run --apply` can preflight.  This is reachable only
            # after INACTIVE was verified; an existing non-pristine table is
            # never repaired or overwritten.
            try:
                conn = migrate.open_connection()
                try:
                    if not migrate.schema_table_exists(conn):
                        baseline = migrate.record_baseline(conn)
                        log.info("迁移基线已登记：%s/%s",
                                 baseline["status"], baseline["structure_profile"])
                finally:
                    conn.close()
            except Exception as exc:      # noqa: BLE001 - 基线失败只禁用迁移，不阻断业务
                log.warning("迁移基线登记失败（迁移入口将不可用，业务继续）：%s", exc)
            migrated = fetcher.migrate_filter_defaults()
            if migrated:
                try:
                    builder.publish(reason="filter defaults migration")
                except Exception as exc:
                    log.warning("过滤默认关键词迁移后重建订阅失败：%s", exc)
            db.append_log("INFO", "系统", f"NodePool {config.VERSION} 启动，数据目录 {config.DATA_DIR}")
            db.record_run_event("start")

        if not blocked:
            await engine_mod.engine_manager.start()
            manager.start_scheduler()
            speed_queue.start()
        log.info("NodePool 已启动（内核：%s）", engine_mod.engine_manager.name)
    except BaseException:
        lock.release()
        app.state.instance_lock = None
        raise

    try:
        yield
    finally:
        await speed_queue.stop()
        manager.stop_scheduler()
        await engine_mod.engine_manager.stop()
        if not blocked:
            try:
                db.append_log("INFO", "系统", "NodePool 已停止")
                db.record_run_event("shutdown")
            except Exception:
                log.warning("停机日志写入失败", exc_info=True)
        lock.release()
        app.state.instance_lock = None


app = FastAPI(title="NodePoolME", version=config.VERSION, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")


@app.middleware("http")
async def maintenance_write_gate(request: Request, call_next):
    """A 类入口 1：写请求进入业务逻辑前判定维护态（只读请求不拦）。

    ACTIVE ⇒ 503 ``reason=maintenance``；RECOVERY_REQUIRED ⇒ 503
    ``reason=recovery_required``；INACTIVE ⇒ 原样放行。
    """
    if request.method not in WRITE_METHODS or not request.url.path.startswith(GATED_PREFIXES):
        return await call_next(request)
    startup = getattr(request.app.state, "startup_decision", None)
    if startup is not None and not startup.inactive:
        # Do not trust a later re-read of the maintenance file here: startup may
        # have classified RECOVERY_REQUIRED from the DB while a concurrent file
        # read (or WAL visibility) disagrees.  The startup decision is authoritative
        # for the lifetime of this process.
        return JSONResponse(status_code=503,
                            content={"ok": False,
                                     "detail": "迁移恢复中，仅允许只读访问",
                                     "reason": pool.REASON_RECOVERY,
                                     "code": startup.code},
                            headers={"Retry-After": "60"})
    try:
        state, reason = gate.judge()
    except Exception as exc:                       # 判定自身异常一律保守拒绝
        log.error("维护态判定异常，按 recovery_required 拒绝写入：%r", exc)
        state, reason = MaintState.RECOVERY_REQUIRED, pool.REASON_RECOVERY
    if reason is not None:
        gate.apply_state(state)
        detail = "维护中，暂不接受写入" if reason == pool.REASON_MAINTENANCE \
            else "迁移恢复中，仅允许只读访问"
        return JSONResponse(status_code=503,
                            content={"ok": False, "detail": detail, "reason": reason},
                            headers={"Retry-After": "60"})
    return await call_next(request)


app.include_router(routes.api)
app.include_router(routes.sub_router)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    db.append_log("ERROR", "接口", f"{request.method} {request.url.path} 未处理异常：{type(exc).__name__}: {exc}")
    return JSONResponse(status_code=500, content={"detail": "服务器内部错误，请查看系统日志"})


if (STATIC_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")


@app.get("/")
@app.get("/index.html")
async def index():
    file = STATIC_DIR / "index.html"
    if file.exists():
        return FileResponse(file)
    return JSONResponse({"app": "NodePoolME", "api": "/api", "docs": "/api/docs",
                         "note": "前端资源尚未构建"})


_ICON_TYPES = {
    "favicon.svg": "image/svg+xml",
    "favicon.ico": "image/x-icon",
    "favicon-32.png": "image/png",
    "apple-touch-icon.png": "image/png",
}


@app.get("/favicon.ico")
async def favicon():
    icon = STATIC_DIR / "favicon.ico"
    if icon.exists():
        return FileResponse(icon, media_type="image/x-icon")
    # A 204 response must not carry a body. JSONResponse(None) serialises "null"
    # and uvicorn then raises "Response content longer than Content-Length".
    return Response(status_code=204)


@app.get("/favicon.svg")
async def favicon_svg():
    icon = STATIC_DIR / "favicon.svg"
    if icon.exists():
        return FileResponse(icon, media_type="image/svg+xml")
    return Response(status_code=204)


@app.get("/favicon-32.png")
async def favicon_png():
    icon = STATIC_DIR / "favicon-32.png"
    if icon.exists():
        return FileResponse(icon, media_type="image/png")
    return Response(status_code=204)


@app.get("/apple-touch-icon.png")
async def apple_touch_icon():
    icon = STATIC_DIR / "apple-touch-icon.png"
    if icon.exists():
        return FileResponse(icon, media_type="image/png")
    return Response(status_code=204)


@app.get("/{path:path}")
async def spa(path: str):
    """Fallback so the Vue router works for deep links."""
    if path.startswith(("api/", "sub/", "assets/", "healthz")):
        return JSONResponse(status_code=404, content={"detail": "页面不存在"})
    file = STATIC_DIR / "index.html"
    if file.exists():
        return FileResponse(file)
    return JSONResponse(status_code=404, content={"detail": "页面不存在"})
