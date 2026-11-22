"""HTTP API."""
import asyncio
import base64
import html
import json
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
import urllib.parse

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
        db.append_log("WARN", "认证", "登录失败：密码错误")
        raise HTTPException(status_code=401, detail="密码错误")
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
        raise HTTPException(status_code=400, detail="当前密码错误")
    db.set_setting("admin_password_hash", auth.hash_password(payload.new_password))
    db.append_log("INFO", "认证", "管理员密码已修改")
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


def _latency_labels() -> dict[str, dict]:
    """Custom target names; the pool UI shows only the first character."""
    labels = {}
    for ident, key, fallback in (("G", "latency_test_name", "Google"),
                                 ("W", "latency_test_name2", "Wikipedia")):
        name = (db.get(key) or fallback).strip() or fallback
        labels[ident] = {"name": name, "short": name[0]}
    return labels


@api.get("/dashboard", dependencies=protected)
async def dashboard():
    airports = fetcher.all_airports()   # needs daily_time / interval_minutes for the next-run math
    # Only nodes from enabled airports belong to the pool
    pool = "SELECT COUNT(*) AS c FROM nodes n JOIN airports a ON a.id=n.airport_id WHERE a.enabled=1"
    nodes_total = db.row(pool)["c"]
    unavailable = db.row(pool + " AND n.status='unavailable'")["c"]
    available = db.row(pool + " AND n.status='available'")["c"]
    selected, stats = builder.collect_nodes()
    ceiling = db.get_int("sub_max_latency", 1000)
    ceiling2 = db.get_int("sub_max_latency2", 1000)
    latency_labels = _latency_labels()
    latests = builder.latest_latency()
    probe_passed = sum(1 for p in latests.values() if p["ok"])
    last_batch = db.row("SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    logs = db.rows("SELECT * FROM logs ORDER BY id DESC LIMIT 12")
    now = datetime.now().astimezone()
    fetch_next: list[str] = []
    manual_only = 0
    airport_list: list[dict] = []
    for airport in airports:
        nxt = fetcher.next_run_at(airport, now)
        airport["next_run_at"] = nxt.isoformat(sep=" ", timespec="seconds") if nxt else None
        airport["manual_only"] = nxt is None
        if nxt is None:
            manual_only += 1
        elif airport["enabled"]:
            fetch_next.append(airport["next_run_at"])
        payload = _airport_payload(airport)
        payload["next_run_at"] = airport["next_run_at"]
        payload["manual_only"] = airport["manual_only"]
        airport_list.append(payload)
    fetch_ok = sum(1 for a in airports if a["last_status"] == "ok")
    fetch_err = sum(1 for a in airports if a["last_status"] == "error")
    last_fetch = max((a["last_success_at"] or "" for a in airports), default="") or None
    fetch_block = {
        "total": len(airports),
        "enabled": sum(1 for a in airports if a["enabled"]),
        "ok": fetch_ok,
        "error": fetch_err,
        "manual_only": manual_only,
        "fetching": len(manager.state.fetching),
        "last_success_at": last_fetch,
        "next_at": min(fetch_next) if fetch_next else None,
    }
    last_publish = db.row("SELECT * FROM batches WHERE published=1 ORDER BY id DESC LIMIT 1")
    group_rows = _group_stats()
    hits = db.row("SELECT COUNT(*) AS c FROM (SELECT DISTINCT node_id FROM results WHERE ok=1) t")["c"]
    group_total = len([g for g in group_rows if g["enabled"]])
    group_block = {
        "count": group_total,
        "hits": hits,
        "cells": len(group_rows) and nodes_total * group_total or 0,
        "passed": sum(g["passed"] for g in group_rows if g["enabled"]),
    }
    publish_block = {
        "last_batch_at": last_publish["finished_at"] if last_publish else (last_batch["finished_at"] if last_batch else None),
        "last_auto": db.get("sub_last_auto_publish"),
        "last_auto_at": db.get("sub_last_auto_publish_at"),
        "nodes": builder.meta().get("nodes") or 0,
        "updated_at": builder.meta().get("updated_at"),
    }
    return {
        "airports": len(airports),
        "airports_enabled": sum(1 for a in airports if a["enabled"]),
        "nodes_total": nodes_total,
        "nodes_available": available,
        "nodes_unavailable": unavailable,
        "nodes_qualified": len(selected),
        "groups": _group_stats(),
        "airport_list": airport_list,
        "batch": last_batch,
        "runtime": manager.state.as_dict(),
        "subscription": builder.meta(),
        "logs": logs,
        "stats": stats,
        "latency_probe": {
            "enabled": True,
            "url": db.get("latency_test_url"),
            "url2": db.get("latency_test_url2"),
            "status": db.get("latency_test_status"),
            "ceiling": ceiling,
            "ceiling2": ceiling2,
            "names": latency_labels,
            "rounds": 2,
            "passed": probe_passed,
        },
        "fetch": fetch_block,
        "group_summary": group_block,
        "publish": publish_block,
        "publish_schedule": {
            "enabled": db.get("sub_publish_enabled", "0") == "1",
            "mode": db.get("sub_publish_mode", "time"),
            "time": db.get("sub_publish_time", "04:30"),
            "interval_minutes": db.get_int("sub_publish_interval_minutes", 180),
            "next_at": db.get("sub_publish_next_at"),
            "last_auto": db.get("sub_last_auto_publish"),
        },
    }


# ---------------------------------------------------------------------------- airports
def _airport_payload(row: dict) -> dict:
    filtered = fetcher.filtered_count(row["id"])
    upload = int(row.get("traffic_upload") or 0)
    download = int(row.get("traffic_download") or 0)
    used = upload + download
    total = int(row.get("traffic_total") or 0)
    percent = min(100, round(used * 100 / total)) if total else 0
    return {
        "id": row["id"], "name": row["name"], "url": row["url"], "enabled": bool(row["enabled"]),
        "interval_minutes": row["interval_minutes"], "daily_time": row["daily_time"],
        "last_attempt_at": row["last_attempt_at"], "last_success_at": row["last_success_at"],
        "last_status": row["last_status"], "last_error": row["last_error"],
        "node_count": row["node_count"] + filtered,
        "pool_count": row["node_count"],
        "filter_keywords": fetcher.normalize_keywords(row.get("filter_keywords")),
        "filtered_count": filtered,
        "traffic_available": bool(row.get("traffic_updated_at")),
        "traffic_upload": upload,
        "traffic_download": download,
        "traffic_used": used,
        "traffic_total": total,
        "traffic_percent": percent,
        "traffic_expire": int(row.get("traffic_expire") or 0),
        "traffic_updated_at": row.get("traffic_updated_at"),
        "url_masked": (row["url"][:32] + "...") if len(row["url"]) > 40 else row["url"],
    }


@api.get("/airports", dependencies=protected)
async def list_airports():
    return [_airport_payload(a) for a in fetcher.all_airports()]


@api.post("/airports", dependencies=protected)
async def create_airport(payload: AirportIn):
    exists = db.row("SELECT id FROM airports WHERE name=?", (payload.name,))
    if exists:
        raise HTTPException(status_code=400, detail="机场名称已被使用")
    stamp = db.now()
    keywords = (fetcher.normalize_keywords(payload.filter_keywords)
                if payload.filter_keywords is not None else list(config.DEFAULT_FILTER_KEYWORDS))
    airport_id = db.execute(
        """INSERT INTO airports (name, url, enabled, interval_minutes, daily_time, filter_keywords,
           created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)""",
        (payload.name.strip(), payload.url.strip(), int(payload.enabled), payload.interval_minutes,
         payload.daily_time, db.dumps(keywords), stamp, stamp),
    )
    db.append_log("INFO", "机场", f"新增机场：{payload.name}")
    return _airport_payload(fetcher.airport_by_id(airport_id))


@api.patch("/airports/{airport_id}", dependencies=protected)
async def update_airport(airport_id: int, payload: AirportPatch):
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        raise HTTPException(status_code=404, detail="机场不存在")
    data = payload.dict(exclude_unset=True)
    if "name" in data and data["name"]:
        if db.row("SELECT id FROM airports WHERE name=? AND id<>?", (data["name"], airport_id)):
            raise HTTPException(status_code=400, detail="机场名称已被使用")
        airport["name"] = data["name"].strip()
    for key in ("url", "daily_time"):
        if key in data and data[key] is not None:
            airport[key] = data[key].strip()
    was_enabled = bool(airport["enabled"])
    if "enabled" in data and data["enabled"] is not None:
        airport["enabled"] = int(bool(data["enabled"]))
    if data.get("interval_minutes") is not None:
        airport["interval_minutes"] = int(data["interval_minutes"])
    keywords_changed = "filter_keywords" in data
    keywords = (fetcher.normalize_keywords(data.get("filter_keywords"))
                if keywords_changed else fetcher.normalize_keywords(airport.get("filter_keywords")))
    db.execute(
        """UPDATE airports SET name=?, url=?, enabled=?, interval_minutes=?, daily_time=?,
           filter_keywords=?, updated_at=? WHERE id=?""",
        (airport["name"], airport["url"], airport["enabled"], airport["interval_minutes"],
         airport["daily_time"], db.dumps(keywords), db.now(), airport_id),
    )
    if keywords_changed:
        fetcher.reclassify_airport(airport_id, keywords, publish=True)
    if bool(airport["enabled"]) != was_enabled:
        if airport["enabled"]:
            # re-enable: cached nodes come back instantly, no immediate pull
            db.execute("UPDATE airports SET last_attempt_at=? WHERE id=?", (db.now(), airport_id))
            db.append_log("INFO", "拉取",
                          f"{airport['name']}：已启用，直接恢复缓存节点，无需重新拉取")
        else:
            db.append_log("INFO", "拉取",
                          f"{airport['name']}：已停用，节点已退出节点池，重新启用后恢复")
        try:
            builder.publish(reason="airport toggle")
        except Exception as exc:
            db.append_log("WARN", "下发", f"机场启停后重建订阅失败：{exc}")
    return _airport_payload(fetcher.airport_by_id(airport_id))


@api.delete("/airports/{airport_id}", dependencies=protected)
async def delete_airport(airport_id: int):
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        raise HTTPException(status_code=404, detail="机场不存在")
    affected = db.row("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?", (airport_id,))["c"]
    affected += db.row("SELECT COUNT(*) AS c FROM airport_filtered_nodes WHERE airport_id=?",
                       (airport_id,))["c"]
    db.execute("DELETE FROM nodes WHERE airport_id=?", (airport_id,))
    db.execute("DELETE FROM airport_filtered_nodes WHERE airport_id=?", (airport_id,))
    db.execute("DELETE FROM fetch_logs WHERE airport_id=?", (airport_id,))
    db.execute("DELETE FROM airports WHERE id=?", (airport_id,))
    db.append_log("WARN", "机场", f"删除机场 {airport['name']}，移除 {affected} 个缓存节点")
    return {"ok": True, "removed_nodes": affected,
            "note": "机场及其缓存节点、拉取记录已删除；检测历史保留"}


@api.get("/airport-filter-defaults", dependencies=protected)
async def airport_filter_defaults():
    return {"keywords": list(config.DEFAULT_FILTER_KEYWORDS)}


@api.get("/airports/{airport_id}/filter", dependencies=protected)
async def airport_filter(airport_id: int):
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        raise HTTPException(status_code=404, detail="机场不存在")
    return {
        "airport": _airport_payload(airport),
        "keywords": fetcher.normalize_keywords(airport.get("filter_keywords")),
        "default_keywords": list(config.DEFAULT_FILTER_KEYWORDS),
        "nodes": fetcher.filtered_nodes(airport_id),
    }


@api.post("/airports/{airport_id}/filter/admit", dependencies=protected)
async def airport_filter_admit(airport_id: int, body: dict):
    fingerprint = str(body.get("fingerprint") or "").strip()
    if not fingerprint:
        raise HTTPException(status_code=400, detail="缺少节点指纹")
    result = fetcher.admit_filtered_node(airport_id, fingerprint)
    if not result:
        raise HTTPException(status_code=404, detail="被过滤节点不存在，可能已加入节点池")
    return result


@api.post("/airports/{airport_id}/fetch", dependencies=protected)
async def fetch_airport_now(airport_id: int):
    result = await manager.fetch_now(airport_id, trigger="manual")
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "拉取失败")
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
        raise HTTPException(status_code=400, detail="标识已被其他检测组使用")
    group_id = db.execute(
        """INSERT INTO test_groups (name, ident, url, method, timeout, expect_status, expect_body,
           max_latency, interval_minutes, enabled, sort_order, is_default, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload.name, payload.ident.strip(), payload.url, payload.method.upper(),
         db.get_int("latency_test_timeout", 8),
         payload.expect_status, payload.expect_body, payload.max_latency, 30,
         int(payload.enabled), payload.sort_order, 0, db.now()),
    )
    db.append_log("INFO", "检测组", f"新增检测组：{payload.name}/{payload.ident}")
    return _group_payload(db.row("SELECT * FROM test_groups WHERE id=?", (group_id,)))


@api.patch("/groups/{group_id}", dependencies=protected)
async def update_group(group_id: int, payload: GroupPatch):
    group = db.row("SELECT * FROM test_groups WHERE id=?", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="检测组不存在")
    data = payload.dict(exclude_unset=True)
    # timeout and cadence are global now: ignore whatever the client still sends
    data.pop("timeout", None)
    data.pop("interval_minutes", None)
    if "ident" in data and data["ident"]:
        if db.row("SELECT id FROM test_groups WHERE ident=? AND id<>?", (data["ident"], group_id)):
            raise HTTPException(status_code=400, detail="标识已被其他检测组使用")
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
        raise HTTPException(status_code=404, detail="检测组不存在")
    if group["is_default"]:
        raise HTTPException(status_code=400,
                            detail="默认检测组不能删除，请改为停用")
    db.execute("DELETE FROM test_groups WHERE id=?", (group_id,))
    db.execute("DELETE FROM results WHERE group_id=?", (group_id,))
    db.append_log("WARN", "检测组", f"删除检测组：{group['name']}")
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
                     region: str | None = None, status: str | None = None,
                     group_id: str | None = None, tags: str | None = None,
                     passed: str | None = None, publishable: str | None = None,
                     limit: int = 200, offset: int = 0, sort: str = "latency"):
    nodes = db.rows(
        """SELECT n.*, a.name AS airport_name FROM nodes n
           JOIN airports a ON a.id = n.airport_id
           WHERE a.enabled=1"""
    )
    groups = {g["id"]: g for g in db.rows("SELECT * FROM test_groups ORDER BY sort_order, id")}
    enabled_groups = [g for g in groups.values() if g["enabled"]]
    results = builder.latest_results()
    latests = builder.latest_latency()
    ceiling = db.get_int("sub_max_latency", 1000)
    ceiling2 = db.get_int("sub_max_latency2", 1000)
    latency_labels = _latency_labels()
    required_idents = [t for t in (db.get("sub_required_tags") or "").split(",") if t]
    idents = _node_names()
    items = []
    for node in nodes:
        per_group = results.get(node["id"], {})
        passed_idents = idents.get(node["id"], [])
        display = (f"{node['airport_name']}{builder.NAME_SEP}"
                   f"{builder.TAG_SEP.join(passed_idents)}{builder.NAME_SEP}{node['raw_name']}"
                   if passed_idents
                   else f"{node['airport_name']}{builder.NAME_SEP}未通过{builder.NAME_SEP}{node['raw_name']}")
        node["display_name"] = display
        probe = latests.get(node["id"]) or {}
        targets = probe.get("targets") or {}
        node["latency_targets"] = {
            "G": {"best": (targets.get("G") or {}).get("best"),
                  "min": (targets.get("G") or {}).get("min"), "cap": ceiling,
                  "name": latency_labels["G"]["name"], "short": latency_labels["G"]["short"]},
            "W": {"best": (targets.get("W") or {}).get("best"),
                  "min": (targets.get("W") or {}).get("min"), "cap": ceiling2,
                  "name": latency_labels["W"]["name"], "short": latency_labels["W"]["short"]},
        }
        # Publish gate: BOTH targets need a passing round within their own ceiling.
        good = all(node["latency_targets"][t]["best"] is not None for t in ("G", "W"))
        latency = probe.get("fastest_ms") or probe.get("latency_ms") or (node["latency"] or 0)
        node["latency"] = latency
        missing_tags = [t for t in required_idents if t not in passed_idents]
        node["missing_tags"] = missing_tags
        node["publishable"] = bool(good) and not missing_tags
        if node["publishable"]:
            node["publish_reason"] = "下发"
        elif not targets:
            node["publish_reason"] = "无可用延迟数据"
        elif node["latency_targets"]["G"]["best"] is None:
            node["publish_reason"] = f"{latency_labels['G']['name']} 未通过（上限 {ceiling}ms）"
        elif node["latency_targets"]["W"]["best"] is None:
            node["publish_reason"] = f"{latency_labels['W']['name']} 未通过（上限 {ceiling2}ms）"
        else:
            node["publish_reason"] = f"缺少标签：{'、'.join(missing_tags)}"
        node["results"] = {
            str(gid): {**res, "ident": groups.get(gid, {}).get("ident", "?")}
            for gid, res in per_group.items() if gid in groups
        }
        node["passed_count"] = len(passed_idents)
        node["passed_idents_count"] = len(passed_idents)
        items.append(node)

    hit_mode = (passed or "").strip().lower() in ("0", "no", "fail", "miss", "missed")
    tag_list = [t.strip() for t in (tags or "").split(",") if t.strip()]
    # group_id accepts either the numeric id or the ident (e.g. "GPT")
    group_key: str | None = None
    if group_id not in (None, "", "0"):
        raw_gid = str(group_id).strip()
        if raw_gid.isdigit() and int(raw_gid) in groups:
            group_key = str(int(raw_gid))
        else:
            match = next((g for g in groups.values() if g["ident"] == raw_gid), None)
            group_key = str(match["id"]) if match else "__missing__"

    def keep(node: dict) -> bool:
        """All filters are ANDed; no branch may short-circuit another one."""
        if q:
            hay = f"{node['display_name']} {node['raw_name']} {node['server']} {node['protocol']}"
            if q.lower() not in hay.lower():
                return False
        if airport_id and node["airport_id"] != airport_id:
            return False
        if protocol and (node["protocol"] or "").lower() != protocol.lower():
            return False
        if region and (node["region"] or "").upper() != region.upper():
            return False
        if status and node["status"] != status:
            return False
        if group_key:
            res = node["results"].get(group_key)
            ok = bool(res.get("ok")) if res else False
            # "未命中所选组": no record or a failed record both count as a miss.
            # This mode belongs to the group filter only and never flips the tag filter.
            if hit_mode:
                if ok:
                    return False
            elif not ok:
                return False
        if tag_list:
            node_idents = set(idents.get(node["id"], []))
            # Tags are always a positive AND filter: every selected tag must be present.
            if not all(t in node_idents for t in tag_list):
                return False
        if passed == "qualified" and not node["passed_idents_count"]:
            return False
        if passed == "untagged" and node["passed_idents_count"]:
            return False
        if publishable in ("1", "0") and node["publishable"] != (publishable == "1"):
            return False
        return True

    items = [n for n in items if keep(n)]
    if sort == "latency":
        items.sort(key=lambda n: (n["latency"] if n["latency"] else 10 ** 6))
    elif sort == "name":
        items.sort(key=lambda n: n["display_name"] or "")
    elif sort == "tested":
        items.sort(key=lambda n: (n["last_tested_at"] or ""), reverse=True)
    elif sort == "tags":
        items.sort(key=lambda n: -n["passed_idents_count"])
    else:
        items.sort(key=lambda n: n["id"])
    total = len(items)
    return {"total": total, "items": items[offset:offset + max(1, min(limit, 1000))],
            "latency_labels": latency_labels,
            "protocols": sorted({n["protocol"] for n in nodes}),
            "regions": sorted({n["region"] for n in nodes if n["region"]})}


@api.get("/nodes/{node_id}", dependencies=protected)
async def node_detail(node_id: int):
    node = db.row("""SELECT n.*, a.name AS airport_name FROM nodes n
                     JOIN airports a ON a.id=n.airport_id WHERE n.id=?""", (node_id,))
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    node["params"] = db.loads(node.get("params"), {})
    node["history"] = db.rows(
        """SELECT r.*, g.name AS group_name, g.ident FROM results r
           LEFT JOIN test_groups g ON g.id=r.group_id
           WHERE r.node_id=? ORDER BY r.id DESC LIMIT 60""", (node_id,))
    node["latency_history"] = db.rows(
        "SELECT * FROM latency_results WHERE node_id=? ORDER BY id DESC LIMIT 60", (node_id,))
    summary = builder.latest_latency().get(node_id) or {}
    ceiling = db.get_int("sub_max_latency", 1000)
    ceiling2 = db.get_int("sub_max_latency2", 1000)
    latency_labels = _latency_labels()
    node["latency_labels"] = latency_labels
    node["latency_targets"] = {
        "G": {"best": ((summary.get("targets") or {}).get("G") or {}).get("best"),
              "min": ((summary.get("targets") or {}).get("G") or {}).get("min"), "cap": ceiling,
              "url": db.get("latency_test_url"), "name": latency_labels["G"]["name"],
              "short": latency_labels["G"]["short"]},
        "W": {"best": ((summary.get("targets") or {}).get("W") or {}).get("best"),
              "min": ((summary.get("targets") or {}).get("W") or {}).get("min"), "cap": ceiling2,
              "url": db.get("latency_test_url2"), "name": latency_labels["W"]["name"],
              "short": latency_labels["W"]["short"]},
    }
    node["latency"] = summary.get("fastest_ms") or summary.get("latency_ms") or (node["latency"] or 0)
    required = [t for t in (db.get("sub_required_tags") or "").split(",") if t]
    node_idents = set(_node_names().get(node_id, []))
    missing = [t for t in required if t not in node_idents]
    node["missing_tags"] = missing
    good = all(node["latency_targets"][t]["best"] is not None for t in ("G", "W"))
    node["publishable"] = bool(good) and not missing
    if node["publishable"]:
        node["publish_reason"] = "下发"
    elif not summary.get("targets"):
        node["publish_reason"] = "无可用延迟数据"
    elif node["latency_targets"]["G"]["best"] is None:
        node["publish_reason"] = f"{latency_labels['G']['name']} 未通过（上限 {ceiling}ms）"
    elif node["latency_targets"]["W"]["best"] is None:
        node["publish_reason"] = f"{latency_labels['W']['name']} 未通过（上限 {ceiling2}ms）"
    else:
        node["publish_reason"] = f"缺少标签：{'、'.join(missing)}"
    return node


@api.post("/nodes/{node_id}/test", dependencies=protected)
async def test_node(node_id: int, payload: NodeTestIn | None = None):
    result = await manager.test_single_node(
        node_id,
        payload.group_ids if payload else None,
        payload.mode if payload else "all",
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error") or "节点检测失败")
    try:
        result["subscription"] = builder.add_published_node(node_id)
    except Exception as exc:  # a publish hiccup must not fail the test itself
        db.append_log("ERROR", "下发", f"手动测试节点 #{node_id} 增量下发失败：{exc}")
        result["subscription"] = {"ok": False, "reason": "节点已检测，但订阅下发失败，请查看系统日志"}
    return result


# ---------------------------------------------------------------------------- batches & runs
@api.post("/run", dependencies=protected)
async def run_cycle(payload: RunIn | None = None):
    """Kick a batch off in the background; progress is polled through /api/runtime."""
    payload = payload or RunIn()
    if manager.state.running:
        raise HTTPException(status_code=409, detail="已有检测批次正在运行")
    if not db.row("SELECT COUNT(*) AS c FROM nodes n JOIN airports a ON a.id=n.airport_id"
                  " WHERE a.enabled=1")["c"]:
        raise HTTPException(status_code=409, detail="节点池为空")
    task = asyncio.create_task(manager.run_cycle(force=True, group_ids=payload.group_ids,
                                                 reason="manual"))
    manager.state.background_task = task
    return {"ok": True, "started": True}


@api.post("/run/pause", dependencies=protected)
async def pause_cycle():
    res = manager.state.request_pause()
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or "操作失败")
    return res


@api.post("/run/resume", dependencies=protected)
async def resume_cycle():
    res = manager.state.request_resume()
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or "操作失败")
    return res


@api.post("/run/stop", dependencies=protected)
async def stop_cycle():
    res = manager.state.request_stop()
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error") or "操作失败")
    return res


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
            "latency_test_enabled": True,
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
        "queryless": {
            "clash": f"/sub/all/{token}/clash",
            "singbox": f"/sub/all/{token}/singbox",
            "base64": f"/sub/all/{token}/base64",
        },
    }


@api.patch("/subscription", dependencies=protected)
async def update_subscription(payload: SubSettings):
    if payload.sub_name:
        db.set_setting("sub_name", payload.sub_name.strip())
    if payload.regenerate_token:
        db.set_setting("sub_token", secrets.token_urlsafe(24))
        db.append_log("INFO", "订阅", "订阅令牌已重新生成")
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
                "test_interval_minutes", "test_on_start", "fail_threshold", "history_days",
                "dispersion_mask",
                "sub_name", "latency_test_url", "latency_test_url2", "latency_test_name",
                "latency_test_name2", "latency_test_status",
                "latency_test_timeout", "sub_max_latency", "sub_max_latency2",
                "sub_publish_enabled", "sub_publish_time", "sub_publish_mode",
                "sub_publish_interval_minutes", "sub_required_tags"]


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
            "fetch_ua": config.HTTP_UA,
            "time": db.now(),
        },
    }


@api.patch("/settings", dependencies=protected)
async def update_settings(payload: SettingsIn):
    data = payload.dict(exclude_unset=True)
    if "sub_required_tags" in data:
        wanted = data.pop("sub_required_tags") or []
        enabled = db.rows("SELECT ident FROM test_groups WHERE enabled=1")
        valid = {g["ident"] for g in enabled}
        unknown = [t for t in wanted if t not in valid]
        if unknown:
            raise HTTPException(status_code=400, detail=f"未知或已停用的标签：{'、'.join(unknown)}")
        data["sub_required_tags"] = ",".join(wanted)
    for key, value in data.items():
        if value is None:
            continue
        if key == "dispersion_mask" and value not in (8, 16, 24, 32):
            raise HTTPException(status_code=400, detail="相似节点分散粒度只能是 8、16、24 或 32")
        db.set_setting(key, value)
    if "request_interval_min_ms" in data or "request_interval_max_ms" in data:
        if db.get_int("request_interval_max_ms") < db.get_int("request_interval_min_ms"):
            db.set_setting("request_interval_max_ms", db.get_int("request_interval_min_ms"))
    if any(k.startswith("sub_publish_") for k in data):
        # Changing the schedule recomputes the next run right away so the dashboard is never blank
        nxt = None
        if db.get("sub_publish_enabled", "0") == "1":
            nxt = manager.next_publish_at()
        if nxt:
            db.set_setting("sub_publish_next_at", nxt.strftime("%Y-%m-%d %H:%M:%S"))
        else:
            db.execute("DELETE FROM settings WHERE key='sub_publish_next_at'")
        db.append_log("INFO", "下发",
                      "下发计划已更改，下次运行："
                      + (nxt.strftime("%Y-%m-%d %H:%M:%S") if nxt else "已清除"))
    db.append_log("INFO", "设置", f"设置已更新，共 {len(data)} 项")
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
    db.append_log("INFO", "备份", f"已创建备份：{target.name}")
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
        raise HTTPException(status_code=400, detail="上传的备份文件过小")
    tmp.write_bytes(content)
    try:
        probe = sqlite3.connect(str(tmp))
        tables = {r[0] for r in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        probe.close()
    except sqlite3.DatabaseError as exc:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="文件不是有效的 SQLite 数据库")
    if "nodes" not in tables or "airports" not in tables:
        tmp.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail="这不是 NodePool 的备份文件")
    safety = config.BACKUP_DIR / f"pre-restore-{datetime.now():%Y%m%d-%H%M%S}.db"
    shutil.copy2(config.DB_PATH, safety)
    db.reset_connection()
    shutil.copy2(tmp, config.DB_PATH)
    tmp.unlink(missing_ok=True)
    db.init_db()
    try:
        builder.publish(reason="restore")
    except Exception as exc:
        db.append_log("WARN", "恢复", f"恢复后重建订阅失败：{exc}")
    db.append_log("WARN", "恢复", f"已从上传文件恢复数据库，原库备份在 {safety.name}")
    return {"ok": True, "safety_copy": safety.name}


# ---------------------------------------------------------------------------- public subscription
sub_router = APIRouter()


def _deep_unescape(value: str) -> str:
    """Reverse proxies sometimes HTML-escape &;: more than once."""
    for _ in range(3):
        unescaped = html.unescape(value)
        if unescaped == value:
            break
        value = unescaped
    return urllib.parse.unquote(value)


def _clean_param(value: str | None) -> str:
    """Repair values mangled by reverse proxies: HTML-escaped &, encoded &, trailing slashes."""
    value = _deep_unescape(value or "")
    value = urllib.parse.unquote(value)
    value = value.replace("&amp;", "&")
    value = value.split("&", 1)[0]
    return value.strip().strip("/")


def _serve_sub(token: str | None, fmt: str | None, ua: str,
               raw_query: str | None = None) -> Response:
    expected = auth.sub_token()
    token = _clean_param(token)
    if not token and raw_query:
        # reverse proxy may have turned "&" into "&amp;" so FastAPI saw one broken value
        parsed = urllib.parse.parse_qs(_deep_unescape(raw_query))
        token = _clean_param((parsed.get("token") or [""])[0])
        if not fmt:
            fmt = (parsed.get("format") or parsed.get("fmt") or [""])[0]
    if not fmt and raw_query:
        repaired = _deep_unescape(raw_query)
        parsed = urllib.parse.parse_qs(repaired)
        fmt = (parsed.get("format") or parsed.get("fmt") or [""])[0]
        if not fmt and "format=" in repaired:
            fmt = repaired.split("format=", 1)[1]
    fmt = _clean_param(fmt) if fmt else fmt
    if not expected or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="订阅 Token 无效")
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
        raise HTTPException(status_code=404, detail="订阅尚未生成")
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
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""),
                      request.url.query)


@sub_router.get("/sub/all/", include_in_schema=False)
async def sub_all_slash(request: Request, token: str | None = None, fmt: str | None = None,
                        format: str | None = None):
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""),
                      request.url.query)


@sub_router.get("/sub/all/{token}")
async def sub_all_token(token: str, request: Request, fmt: str | None = None,
                        format: str | None = None):
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""),
                      request.url.query)


@sub_router.get("/sub/all/{token}/")
async def sub_all_token_slash(token: str, request: Request, fmt: str | None = None,
                              format: str | None = None):
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""),
                      request.url.query)


@sub_router.get("/sub/all/{token}/{fmt}")
async def sub_all_token_fmt(token: str, fmt: str, request: Request, format: str | None = None):
    """Query-free form: /sub/all/<token>/<clash|singbox|base64>.
    Needed for reverse proxies that escape & or drop the query string."""
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""),
                      request.url.query)


@sub_router.get("/sub/all/{token}/{fmt}/")
async def sub_all_token_fmt_slash(token: str, fmt: str, request: Request,
                                  format: str | None = None):
    return _serve_sub(token, format or fmt, request.headers.get("user-agent", ""),
                      request.url.query)


@sub_router.get("/healthz")
async def healthz():
    return {
        "status": "ok",
        "version": config.VERSION,
        "engine": engine_mod.engine_manager.name,
        "db": config.DB_PATH.exists(),
        "scheduler": manager.scheduler.running,
    }
