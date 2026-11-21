"""HTTP API."""
import asyncio
import base64
import json
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse

from backend import config
from backend.api import auth
from backend.database import db
from backend.models.schemas import (AirportIn, AirportPatch, GroupIn, GroupPatch, LoginIn,
                                    NodeTestIn, PasswordIn, RunIn, SettingsIn, SubSettings)
from backend.scheduler import manager
from backend.services import fetcher
from backend.subscriptions import builder
from backend.testers import engine as engine_mod

api = APIRouter(prefix="/api")
protected = [Depends(auth.require_auth)]


# ---------------------------------------------------------------------------- auth
@api.post("/auth/login")
async def login(payload: LoginIn, response: Response):
    if not auth.verify_password(payload.password):
        db.append_log("WARN", "auth", "failed login attempt")
        raise HTTPException(status_code=401, detail="wrong password")
    token = auth.make_token()
    auth.set_session(response, token)
    return {"ok": True}


@api.post("/auth/logout")
async def logout(response: Response):
    auth.clear_session(response)
    return {"ok": True}


@api.get("/auth/me", dependencies=protected)
async def me():
    return {"user": "admin", "version": config.VERSION}


@api.post("/auth/password", dependencies=protected)
async def change_password(payload: PasswordIn):
    if not auth.verify_password(payload.old_password):
        raise HTTPException(status_code=400, detail="current password is wrong")
    db.set_setting("admin_password_hash", auth.hash_password(payload.new_password))
    db.append_log("INFO", "auth", "admin password changed")
    return {"ok": True}


# ---------------------------------------------------------------------------- dashboard
def _group_stats() -> list[dict]:
    groups = db.rows("SELECT * FROM test_groups ORDER BY sort_order, id")
    for group in groups:
        row = db.row(
            """SELECT COUNT(*) AS c FROM (
                   SELECT r.node_id FROM results r
                   JOIN (SELECT node_id, MAX(id) AS mx FROM results WHERE group_id=? GROUP BY node_id) m
                     ON m.node_id = r.node_id AND m.mx = r.id
                   WHERE r.ok = 1) t""",
            (group["id"],),
        )
        group["passed"] = row["c"] if row else 0
        group["exists"] = True
    return groups


@api.get("/dashboard", dependencies=protected)
async def dashboard():
    airports = db.rows("SELECT id, name, enabled, node_count, last_success_at, last_status, last_error FROM airports")
    nodes_total = db.row("SELECT COUNT(*) AS c FROM nodes")["c"]
    unavailable = db.row("SELECT COUNT(*) AS c FROM nodes WHERE status='unavailable'")["c"]
    available = db.row("SELECT COUNT(*) AS c FROM nodes WHERE status='available'")["c"]
    degraded = db.row("SELECT COUNT(*) AS c FROM nodes WHERE status='degraded'")["c"]
    selected, stats = builder.collect_nodes()
    ceiling = db.get_int("sub_max_latency", 500)
    latests = builder.latest_latency()
    probe_passed = sum(1 for p in latests.values()
                       if p["ok"] and (not ceiling or (p["latency_ms"] or 0) <= ceiling))
    last_batch = db.row("SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    logs = db.rows("SELECT * FROM logs ORDER BY id DESC LIMIT 12")
    return {
        "airports": len(airports),
        "airports_enabled": sum(1 for a in airports if a["enabled"]),
        "nodes_total": nodes_total,
        "nodes_available": available,
        "nodes_unavailable": unavailable,
        "nodes_degraded": degraded,
        "nodes_qualified": len(selected),
        "groups": _group_stats(),
        "airport_list": airports,
        "batch": last_batch,
        "runtime": manager.state.as_dict(),
        "subscription": builder.meta(),
        "logs": logs,
        "stats": stats,
        "latency_probe": {
            "enabled": db.get("latency_test_enabled", "1") == "1",
            "url": db.get("latency_test_url"),
            "status": db.get("latency_test_status"),
            "ceiling": ceiling,
            "passed": probe_passed,
        },
    }


# ---------------------------------------------------------------------------- airports
def _airport_payload(row: dict) -> dict:
    return {
        "id": row["id"], "name": row["name"], "url": row["url"], "enabled": bool(row["enabled"]),
        "interval_minutes": row["interval_minutes"], "daily_time": row["daily_time"],
        "last_attempt_at": row["last_attempt_at"], "last_success_at": row["last_success_at"],
        "last_status": row["last_status"], "last_error": row["last_error"],
        "node_count": row["node_count"],
        "url_masked": (row["url"][:32] + "...") if len(row["url"]) > 40 else row["url"],
    }


@api.get("/airports", dependencies=protected)
async def list_airports():
    return [_airport_payload(a) for a in fetcher.all_airports()]


@api.post("/airports", dependencies=protected)
async def create_airport(payload: AirportIn):
    exists = db.row("SELECT id FROM airports WHERE name=?", (payload.name,))
    if exists:
        raise HTTPException(status_code=400, detail="airport name already used")
    stamp = db.now()
    airport_id = db.execute(
        """INSERT INTO airports (name, url, enabled, interval_minutes, daily_time, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?)""",
        (payload.name.strip(), payload.url.strip(), int(payload.enabled), payload.interval_minutes,
         payload.daily_time, stamp, stamp),
    )
    db.append_log("INFO", "airport", f"airport {payload.name} created")
    return _airport_payload(fetcher.airport_by_id(airport_id))


@api.patch("/airports/{airport_id}", dependencies=protected)
async def update_airport(airport_id: int, payload: AirportPatch):
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        raise HTTPException(status_code=404, detail="airport not found")
    data = payload.dict(exclude_unset=True)
    if "name" in data and data["name"]:
        if db.row("SELECT id FROM airports WHERE name=? AND id<>?", (data["name"], airport_id)):
            raise HTTPException(status_code=400, detail="airport name already used")
        airport["name"] = data["name"].strip()
    for key in ("url", "daily_time"):
        if key in data and data[key] is not None:
            airport[key] = data[key].strip()
    if "enabled" in data and data["enabled"] is not None:
        airport["enabled"] = int(bool(data["enabled"]))
    if "interval_minutes" in data and data["interval_minutes"]:
        airport["interval_minutes"] = int(data["interval_minutes"])
    db.execute(
        """UPDATE airports SET name=?, url=?, enabled=?, interval_minutes=?, daily_time=?, updated_at=?
           WHERE id=?""",
        (airport["name"], airport["url"], airport["enabled"], airport["interval_minutes"],
         airport["daily_time"], db.now(), airport_id),
    )
    return _airport_payload(fetcher.airport_by_id(airport_id))


@api.delete("/airports/{airport_id}", dependencies=protected)
async def delete_airport(airport_id: int):
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        raise HTTPException(status_code=404, detail="airport not found")
    affected = db.row("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?", (airport_id,))["c"]
    db.execute("DELETE FROM nodes WHERE airport_id=?", (airport_id,))
    db.execute("DELETE FROM fetch_logs WHERE airport_id=?", (airport_id,))
    db.execute("DELETE FROM airports WHERE id=?", (airport_id,))
    db.append_log("WARN", "airport", f"airport {airport['name']} deleted, {affected} cached nodes removed")
    return {"ok": True, "removed_nodes": affected,
            "note": "airport, its cached nodes and fetch history were removed; test history is retained"}


@api.post("/airports/{airport_id}/fetch", dependencies=protected)
async def fetch_airport_now(airport_id: int):
    result = await manager.fetch_now(airport_id, trigger="manual")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "fetch failed"))
    return result


@api.get("/airports/{airport_id}/logs", dependencies=protected)
async def airport_logs(airport_id: int, limit: int = 30):
    return db.rows("SELECT * FROM fetch_logs WHERE airport_id=? ORDER BY id DESC LIMIT ?",
                   (airport_id, min(limit, 200)))


# ---------------------------------------------------------------------------- groups
def _group_payload(g: dict) -> dict:
    return {**g, "enabled": bool(g["enabled"]), "is_default": bool(g["is_default"])}


@api.get("/groups", dependencies=protected)
async def list_groups():
    return [_group_payload(g) for g in _group_stats()]


@api.post("/groups", dependencies=protected)
async def create_group(payload: GroupIn):
    if db.row("SELECT id FROM test_groups WHERE ident=?", (payload.ident,)):
        raise HTTPException(status_code=400, detail="ident already used by another group")
    group_id = db.execute(
        """INSERT INTO test_groups (name, ident, url, method, timeout, expect_status, expect_body,
           max_latency, interval_minutes, enabled, sort_order, is_default, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload.name, payload.ident.strip(), payload.url, payload.method.upper(), payload.timeout,
         payload.expect_status, payload.expect_body, payload.max_latency, payload.interval_minutes,
         int(payload.enabled), payload.sort_order, 0, db.now()),
    )
    db.append_log("INFO", "group", f"test group {payload.name}/{payload.ident} created")
    return _group_payload(db.row("SELECT * FROM test_groups WHERE id=?", (group_id,)))


@api.patch("/groups/{group_id}", dependencies=protected)
async def update_group(group_id: int, payload: GroupPatch):
    group = db.row("SELECT * FROM test_groups WHERE id=?", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    data = payload.dict(exclude_unset=True)
    if "ident" in data and data["ident"]:
        if db.row("SELECT id FROM test_groups WHERE ident=? AND id<>?", (data["ident"], group_id)):
            raise HTTPException(status_code=400, detail="ident already used by another group")
    for key, value in data.items():
        if value is None:
            continue
        if key == "enabled":
            group[key] = int(bool(value))
        elif key == "method":
            group[key] = str(value).upper()
        else:
            group[key] = value
    db.execute(
        """UPDATE test_groups SET name=?, ident=?, url=?, method=?, timeout=?, expect_status=?,
           expect_body=?, max_latency=?, interval_minutes=?, enabled=?, sort_order=? WHERE id=?""",
        (group["name"], group["ident"], group["url"], group["method"], group["timeout"],
         group["expect_status"], group["expect_body"], group["max_latency"], group["interval_minutes"],
         group["enabled"], group["sort_order"], group_id),
    )
    return _group_payload(db.row("SELECT * FROM test_groups WHERE id=?", (group_id,)))


@api.delete("/groups/{group_id}", dependencies=protected)
async def delete_group(group_id: int):
    group = db.row("SELECT * FROM test_groups WHERE id=?", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="group not found")
    if group["is_default"]:
        raise HTTPException(status_code=400,
                            detail="default groups cannot be deleted; disable it instead")
    db.execute("DELETE FROM test_groups WHERE id=?", (group_id,))
    db.execute("DELETE FROM results WHERE group_id=?", (group_id,))
    db.append_log("WARN", "group", f"test group {group['name']} deleted")
    return {"ok": True}


# ---------------------------------------------------------------------------- nodes
def _node_names() -> dict[int, str]:
    groups = db.rows("SELECT id, ident, sort_order FROM test_groups WHERE enabled=1 ORDER BY sort_order, id")
    results = builder.latest_results()
    airports = {a["id"]: a["name"] for a in db.rows("SELECT id, name FROM airports")}
    names: dict[int, list[str]] = {}
    for node_id, per_group in results.items():
        passed = [g["ident"] for g in groups if per_group.get(g["id"], {}).get("ok")]
        names[node_id] = passed
    return names


@api.get("/nodes", dependencies=protected)
async def list_nodes(q: str = "", airport_id: int | None = None, protocol: str | None = None,
                     region: str | None = None, status: str | None = None, group_id: int | None = None,
                     passed: str | None = None, limit: int = 200, offset: int = 0,
                     sort: str = "latency"):
    nodes = db.rows(
        """SELECT n.*, a.name AS airport_name FROM nodes n
           JOIN airports a ON a.id = n.airport_id"""
    )
    groups = {g["id"]: g for g in db.rows("SELECT * FROM test_groups ORDER BY sort_order, id")}
    enabled_groups = [g for g in groups.values() if g["enabled"]]
    results = builder.latest_results()
    latests = builder.latest_latency()
    probe_on = db.get("latency_test_enabled", "1") == "1"
    ceiling = db.get_int("sub_max_latency", 500)
    idents = _node_names()
    items = []
    for node in nodes:
        per_group = results.get(node["id"], {})
        passed_idents = idents.get(node["id"], [])
        display = f"{node['airport_name']}.{'.'.join(passed_idents)} {node['raw_name']}" \
            if passed_idents else f"{node['airport_name']}(未通过) {node['raw_name']}"
        node["display_name"] = display
        probe = latests.get(node["id"])
        if probe_on:
            latency = (probe or {}).get("latency_ms") or (node["latency"] or 0)
            node["latency"] = latency
            node["publishable"] = bool(probe and probe["ok"]
                                      and (not ceiling or (probe["latency_ms"] or 0) <= ceiling))
        else:
            node["publishable"] = bool(passed_idents)
        node["results"] = {
            str(gid): {**res, "ident": groups.get(gid, {}).get("ident", "?")}
            for gid, res in per_group.items() if gid in groups
        }
        node["passed_count"] = len(passed_idents)
        if not probe_on:
            node["latency"] = min([r["latency_ms"] for r in per_group.values()
                                   if r.get("ok") and r.get("latency_ms")], default=node["latency"])
        items.append(node)

    def keep(node: dict) -> bool:
        if q:
            hay = f"{node['display_name']} {node['raw_name']} {node['server']} {node['protocol']}".lower()
            if q.lower() not in hay:
                return False
        if airport_id and node["airport_id"] != airport_id:
            return False
        if protocol and node["protocol"] != protocol:
            return False
        if region and (node["region"] or "").upper() != region.upper():
            return False
        if status and node["status"] != status:
            return False
        if group_id:
            res = node["results"].get(str(group_id))
            want = True if passed in (None, "", "1", "ok", "pass") else False
            if passed is None:
                return group_id in node["results"]
            return bool(res and bool(res.get("ok")) == want)
        if passed == "qualified":
            return node.get("publishable", False)
        return True

    items = [n for n in items if keep(n)]
    if sort == "latency":
        items.sort(key=lambda n: (n["latency"] if n["latency"] else 10 ** 6))
    elif sort == "name":
        items.sort(key=lambda n: n["display_name"])
    elif sort == "tested":
        items.sort(key=lambda n: (n["last_tested_at"] or ""), reverse=True)
    else:
        items.sort(key=lambda n: n["id"])
    total = len(items)
    return {"total": total, "items": items[offset:offset + max(1, min(limit, 1000))],
            "protocols": sorted({n["protocol"] for n in nodes}),
            "regions": sorted({n["region"] for n in nodes if n["region"]})}


@api.get("/nodes/{node_id}", dependencies=protected)
async def node_detail(node_id: int):
    node = db.row("""SELECT n.*, a.name AS airport_name FROM nodes n
                     JOIN airports a ON a.id=n.airport_id WHERE n.id=?""", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="node not found")
    node["params"] = db.loads(node.get("params"), {})
    node["history"] = db.rows(
        """SELECT r.*, g.name AS group_name, g.ident FROM results r
           LEFT JOIN test_groups g ON g.id=r.group_id
           WHERE r.node_id=? ORDER BY r.id DESC LIMIT 60""", (node_id,))
    node["latency_history"] = db.rows(
        "SELECT * FROM latency_results WHERE node_id=? ORDER BY id DESC LIMIT 60", (node_id,))
    latest = node["latency_history"][0] if node["latency_history"] else None
    ceiling = db.get_int("sub_max_latency", 500)
    node["publishable"] = bool(
        db.get("latency_test_enabled", "1") == "1" and latest and latest["ok"]
        and (not ceiling or (latest["latency_ms"] or 0) <= ceiling)
    )
    return node


@api.post("/nodes/{node_id}/test", dependencies=protected)
async def test_node(node_id: int, payload: NodeTestIn | None = None):
    result = await manager.test_single_node(node_id, payload.group_ids if payload else None)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ---------------------------------------------------------------------------- batches & runs
@api.post("/run", dependencies=protected)
async def run_cycle(payload: RunIn | None = None):
    """Kick a batch off in the background; progress is polled through /api/runtime."""
    payload = payload or RunIn()
    if manager.state.running:
        raise HTTPException(status_code=409, detail="a test cycle is already running")
    if not db.row("SELECT COUNT(*) AS c FROM nodes")["c"]:
        raise HTTPException(status_code=409, detail="node pool is empty")
    task = asyncio.create_task(manager.run_cycle(force=True, group_ids=payload.group_ids,
                                                 reason="manual"))
    manager.state.background_task = task
    return {"ok": True, "started": True}


@api.get("/batches", dependencies=protected)
async def batches(limit: int = 20):
    rows = db.rows("SELECT * FROM batches ORDER BY id DESC LIMIT ?", (min(limit, 100),))
    for row in rows:
        row["groups"] = db.loads(row["groups"], [])
    return rows


@api.get("/runtime", dependencies=protected)
async def runtime():
    return manager.state.as_dict()


# ---------------------------------------------------------------------------- subscription
def _sub_link(token: str) -> str:
    return f"/sub/all?token={token}"


@api.get("/subscription", dependencies=protected)
async def subscription_info(request: Request):
    token = auth.sub_token()
    return {
        "settings": {
            "sub_name": db.get("sub_name"),
            "sub_token": token,
            "sub_max_latency": db.get_int("sub_max_latency"),
            "sub_max_nodes": db.get_int("sub_max_nodes"),
            "sub_min_groups": db.get_int("sub_min_groups"),
            "sub_include_unavailable": db.get("sub_include_unavailable") == "1",
            "latency_test_enabled": db.get("latency_test_enabled") == "1",
            "latency_test_url": db.get("latency_test_url"),
            "latency_test_status": db.get("latency_test_status"),
        },
        "meta": builder.meta(),
        "links": {
            "path": _sub_link(token),
            "clash": _sub_link(token) + "&format=clash",
            "singbox": _sub_link(token) + "&format=singbox",
            "base64": _sub_link(token) + "&format=base64",
        },
        "base_url": str(request.base_url).rstrip("/"),
    }


@api.patch("/subscription", dependencies=protected)
async def update_subscription(payload: SubSettings):
    if payload.sub_name:
        db.set_setting("sub_name", payload.sub_name.strip())
    if payload.sub_max_latency is not None:
        db.set_setting("sub_max_latency", payload.sub_max_latency)
    if payload.sub_max_nodes is not None:
        db.set_setting("sub_max_nodes", payload.sub_max_nodes)
    if payload.sub_min_groups is not None:
        db.set_setting("sub_min_groups", payload.sub_min_groups)
    if payload.sub_include_unavailable is not None:
        db.set_setting("sub_include_unavailable", "1" if payload.sub_include_unavailable else "0")
    if payload.regenerate_token:
        db.set_setting("sub_token", secrets.token_urlsafe(24))
        db.append_log("INFO", "subscription", "subscription token regenerated")
    elif payload.sub_token:
        db.set_setting("sub_token", payload.sub_token.strip())
    return {"ok": True}


@api.post("/subscription/rebuild", dependencies=protected)
async def rebuild_subscription():
    meta = builder.publish(reason="manual rebuild")
    return meta


@api.get("/subscription/preview", dependencies=protected)
async def preview_subscription(fmt: str = "clash"):
    nodes, stats = builder.collect_nodes()
    sub_name = db.get("sub_name", "NodePool Unified")
    if fmt == "clash":
        content = builder.build_clash(nodes, sub_name)
    elif fmt == "singbox":
        content = builder.build_singbox(nodes, sub_name)
    else:
        content = builder.build_base64(nodes)
    return PlainTextResponse(content, media_type="text/plain; charset=utf-8",
                             headers={"X-Nodepool-Nodes": str(len(nodes))})


# ---------------------------------------------------------------------------- settings
SETTING_KEYS = ["global_concurrency", "group_concurrency", "ip_concurrency", "subnet_concurrency",
                "request_interval_min_ms", "request_interval_max_ms", "test_timeout",
                "test_interval_minutes", "fail_threshold", "history_days", "dispersion_mask",
                "sub_name", "latency_test_enabled", "latency_test_url", "latency_test_status",
                "latency_test_timeout", "sub_max_latency"]


@api.get("/settings", dependencies=protected)
async def get_settings():
    values = {key: db.get(key) for key in SETTING_KEYS}
    return {
        "settings": values,
        "system": {
            "version": config.VERSION,
            "data_dir": str(config.DATA_DIR),
            "db_path": str(config.DB_PATH),
            "engine": engine_mod.engine_manager.name,
            "engine_error": engine_mod.engine_manager.error,
            "db_size": os.path.getsize(config.DB_PATH) if config.DB_PATH.exists() else 0,
            "scheduler": manager.scheduler.running,
            "time": db.now(),
        },
    }


@api.patch("/settings", dependencies=protected)
async def update_settings(payload: SettingsIn):
    data = payload.dict(exclude_unset=True)
    for key, value in data.items():
        if value is None:
            continue
        if key == "dispersion_mask" and value not in (8, 16, 24, 32):
            raise HTTPException(status_code=400, detail="dispersion_mask must be 8, 16, 24 or 32")
        db.set_setting(key, value)
    if "request_interval_min_ms" in data or "request_interval_max_ms" in data:
        if db.get_int("request_interval_max_ms") < db.get_int("request_interval_min_ms"):
            db.set_setting("request_interval_max_ms", db.get_int("request_interval_min_ms"))
    db.append_log("INFO", "settings", f"settings updated: {', '.join(data)}")
    return {"ok": True}


# ---------------------------------------------------------------------------- logs & backup
@api.get("/logs", dependencies=protected)
async def logs(limit: int = 100, level: str | None = None, source: str | None = None):
    sql = "SELECT * FROM logs"
    where = []
    params: list = []
    if level:
        where.append("level=?")
        params.append(level.upper())
    if source:
        where.append("source=?")
        params.append(source)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(min(limit, 500))
    return db.rows(sql, params)


@api.post("/system/backup", dependencies=protected)
async def create_backup():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = config.BACKUP_DIR / f"nodepool-{stamp}.db"
    source = sqlite3.connect(str(config.DB_PATH))
    backup = sqlite3.connect(str(target))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    with open(config.BACKUP_DIR / "nodepool-latest.db", "wb") as fh:
        fh.write(target.read_bytes())
    db.append_log("INFO", "backup", f"backup created {target.name}")
    return {"ok": True, "file": target.name, "size": target.stat().st_size}


@api.get("/system/backup/download", dependencies=protected)
async def download_backup():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = config.BACKUP_DIR / f"nodepool-{stamp}.db"
    source = sqlite3.connect(str(config.DB_PATH))
    backup = sqlite3.connect(str(target))
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    return FileResponse(target, filename=target.name, media_type="application/octet-stream")


@api.post("/system/restore", dependencies=protected)
async def restore_backup(file: UploadFile = File(...)):
    tmp = config.BACKUP_DIR / "restore-upload.db"
    content = await file.read()
    if len(content) < 4096:
        raise HTTPException(status_code=400, detail="uploaded file is too small")
    tmp.write_bytes(content)
    try:
        probe = sqlite3.connect(str(tmp))
        tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        probe.close()
    except sqlite3.DatabaseError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=f"not a valid SQLite database: {exc}")
    if "nodes" not in tables or "airports" not in tables:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="database is not a NodePool backup")
    safety = config.BACKUP_DIR / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(config.DB_PATH, safety)
    db.reset_connection()
    shutil.copy2(tmp, config.DB_PATH)
    tmp.unlink(missing_ok=True)
    db.init_db()
    try:
        builder.publish(reason="restore")
    except Exception as exc:
        db.append_log("WARN", "restore", f"subscription rebuild after restore failed: {exc}")
    db.append_log("WARN", "restore", f"database restored from upload; previous copy at {safety.name}")
    return {"ok": True, "safety_copy": safety.name}


# ---------------------------------------------------------------------------- public subscription
sub_router = APIRouter()


def _serve_sub(token: str | None, fmt: str | None, ua: str) -> Response:
    expected = auth.sub_token()
    token = token or ""
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="invalid subscription token")
    if not fmt:
        low = ua.lower()
        if "sing-box" in low or "singbox" in low:
            fmt = "singbox"
        elif "clash" in low or "mihomo" in low or "meta" in low:
            fmt = "clash"
        else:
            fmt = "base64"
    if fmt in ("yaml", "clash", "mihomo"):
        fmt = "clash"
    if fmt in ("json", "sing-box"):
        fmt = "singbox"
    if fmt in ("v2ray", "base64", "txt"):
        fmt = "base64"
    try:
        fmt, content = builder.read(fmt)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="subscription not generated yet")
    media = {"clash": "text/yaml; charset=utf-8", "singbox": "application/json; charset=utf-8",
             "base64": "text/plain; charset=utf-8"}[fmt]
    ext = {"clash": "yaml", "singbox": "json", "base64": "txt"}[fmt]
    headers = {
        "Content-Disposition": f'inline; filename="nodepool.{ext}"',
        "Profile-Update-Interval": "12",
        "Cache-Control": "no-store",
    }
    return Response(content=content, media_type=media.split(";")[0], headers=headers)


@sub_router.get("/sub/all")
async def sub_all(request: Request, token: str | None = None, fmt: str | None = None,
                  format: str | None = None):
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""))


@sub_router.get("/sub/all/{token}")
async def sub_all_token(token: str, request: Request, fmt: str | None = None,
                        format: str | None = None):
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""))


@sub_router.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "version": config.VERSION,
        "engine": engine_mod.engine_manager.name,
        "db": config.DB_PATH.exists(),
        "scheduler": manager.scheduler.running,
    }
