"""Application entry point: API, scheduler, bundled frontend."""
import logging
import logging.handlers
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from backend import config
from backend.api import routes
from backend.database import db
from backend.scheduler import manager
from backend.services import fetcher
from backend.subscriptions import builder
from backend.testers import engine as engine_mod

STATIC_DIR = Path(__file__).resolve().parent / "static"

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
    manager.start_scheduler()
    log.info("NodePool 已启动（内核：%s）", engine_mod.engine_manager.name)
    try:
        yield
    finally:
        manager.stop_scheduler()
        await engine_mod.engine_manager.stop()
        db.append_log("INFO", "系统", "NodePool 已停止")
        db.record_run_event("shutdown")


app = FastAPI(title="NodePoolME", version=config.VERSION, lifespan=lifespan,
              docs_url="/api/docs", openapi_url="/api/openapi.json")

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
