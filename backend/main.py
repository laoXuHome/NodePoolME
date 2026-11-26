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
from backend.database import db, gate, maint, pool
from backend.database.maint import MaintRead, MaintState
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


def startup_maintenance_state() -> dict:
    """A 类入口 5（重启恢复）：维护态判定必须先于 ``init_db()``。

    * ACTIVE ⇒ 进入维护态：写入闸置位、连接池 dead、业务写入 503；
    * RECOVERY_REQUIRED ⇒ 进入 ``migration_recovery`` 只读态；
    * 首次部署（库文件尚不存在且维护文件 ABSENT）视为 INACTIVE，与现状一致；
    * 不自动推进 phase、不补写事件、不自动重试迁移。
    """
    state = maint.maintenance_state()
    if state is MaintState.RECOVERY_REQUIRED and not config.DB_PATH.exists():
        # migration_incomplete() 无从查询（库还没建），只有维护文件明确不存在
        # 才能判定「迁移从未开始」；CORRUPT/UNREADABLE 一律保持保守恢复。
        try:
            read = maint.read_maint_file()
        except Exception:
            read = None
        if read is not None and read.status is MaintRead.ABSENT:
            state = MaintState.INACTIVE
    reason = gate.write_reason(state)
    if reason is not None:
        gate.apply_state(state)
        pool.block_writes(reason)
    return {"state": state.name, "reason": reason, "blocked": reason is not None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # 启动自检：STATE_DIR 同卷 + 原子写全链路。每次启动一次、结果缓存。
    # 失败只禁用迁移入口（start_migration 拒绝执行），**不得**影响业务启动、
    # 不得使全站 503、不得自动迁移或改动数据库。
    try:
        state_dir_check = maint.selfcheck_state_dir()
    except Exception as exc:                      # 自检自身异常也只降级，不阻断启动
        log.warning("STATE_DIR 自检异常，迁移入口禁用：%r", exc)
        state_dir_check = None
    app.state.state_dir_check = state_dir_check
    if state_dir_check is not None and not state_dir_check.ok:
        log.warning("STATE_DIR 自检未通过（%s）：迁移入口已禁用，业务照常启动（%s）",
                    state_dir_check.code, state_dir_check.detail)
    # A 类入口 5：维护态判定在 init_db() 之前完成；非 INACTIVE 不初始化、不建调度
    startup = startup_maintenance_state()
    app.state.maintenance = startup
    blocked = startup["blocked"]
    if blocked:
        log.warning("维护态（%s）：业务写入已封锁（503），后台任务未启动，等待人工恢复",
                    startup["state"])
    else:
        db.init_db()
        migrated = fetcher.migrate_filter_defaults()
        if migrated:
            try:
                builder.publish(reason="filter defaults migration")
            except Exception as exc:
                log.warning("过滤默认关键词迁移后重建订阅失败：%s", exc)
        db.append_log("INFO", "系统", f"NodePool {config.VERSION} 启动，数据目录 {config.DATA_DIR}")
        db.record_run_event("start")
    await engine_mod.engine_manager.start()
    if not blocked:
        manager.start_scheduler()
        speed_queue.start()
    log.info("NodePool 已启动（内核：%s）", engine_mod.engine_manager.name)
    try:
        yield
    finally:
        await speed_queue.stop()
        manager.stop_scheduler()
        await engine_mod.engine_manager.stop()
        if not blocked:
            db.append_log("INFO", "系统", "NodePool 已停止")
            db.record_run_event("shutdown")


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
