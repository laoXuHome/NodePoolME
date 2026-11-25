"""HTTP API."""
import asyncio
import html
import json
import math
import os
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta
import urllib.parse
import urllib.request

from fastapi import APIRouter, Depends, HTTPException, Request, Response, UploadFile, File
from fastapi.responses import FileResponse, PlainTextResponse

from backend import config
from backend.api import auth
from backend.database import db
from backend.models.schemas import (AirportIn, AirportPatch, GroupIn, GroupPatch, LoginIn,
                                    NodeTestIn, PasswordIn, RunIn, SettingsIn, SubSettings,
                                    SpeedSourcePatch, SpeedTestIn)
from backend.scheduler import manager
from backend.services import fetcher
from backend.services.speed_queue import queue as speed_queue, schedule_state
from backend.scoring import eligibility, sampler
from backend.subscriptions import builder
from backend.testers import engine as engine_mod
from backend.testers import speedtest

api = APIRouter(prefix="/api")
protected = [Depends(auth.require_auth)]
SCORE_WEIGHTS = {"available": 60, "publish": 35, "tag": 5}
# 追加分：原始 10 分加 1 分，70 分及以上固定加 15 分，10–70 分之间线性递增
BONUS_MIN, BONUS_MAX = 1.0, 15.0
BONUS_KNEE_LOW, BONUS_KNEE_HIGH = 10.0, 70.0


def bonus_score(raw):
    """在原始评分上追加的分值，只按原始分算一次。"""
    raw = float(raw)
    return max(BONUS_MIN, min(BONUS_MAX, BONUS_MIN + (raw - BONUS_KNEE_LOW)
                                     / (BONUS_KNEE_HIGH - BONUS_KNEE_LOW) * (BONUS_MAX - BONUS_MIN)))


def final_score(raw):
    """最终展示分数：原始分 + 追加分，封顶 100。"""
    raw = float(raw)
    return min(100.0, raw + bonus_score(raw))


def score_grade_of(value):
    """按最终展示分数判定五级，严格 20 分一档：优 80–100、良 60–79、中 40–59、差 20–39、劣 <20。"""
    value = float(value)
    return ("优" if value >= 80 else "良" if value >= 60 else "中" if value >= 40
            else "差" if value >= 20 else "劣")
DATA_STATUS_TEXT = {
    "ample": "充足", "normal": "正常", "reference": "参考", "rough": "大概",
    "insufficient": "数据不足", "unknown": "计划未知",
}

# 时段权重：傍晚到凌晨是实际使用高峰，评分按快照发生的时间加权
# 17:00–01:00 占 50%，01:00–08:00 占 15%，08:00–17:00 占 35%
TIME_BANDS = (((17, 24), 0.50), ((0, 1), 0.50), ((1, 8), 0.15), ((8, 17), 0.35))


def _time_band(ts: str) -> tuple[float, int]:
    """返回 (时段权重, 时段序号)；ts 解析失败按白天档处理。"""
    try:
        hour = datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").hour
    except (TypeError, ValueError):
        return 0.35, 3
    for idx, ((start, end), weight) in enumerate(TIME_BANDS):
        if start <= hour < end:
            return weight, idx
    return 0.35, 3


def _parse_local(ts) -> datetime | None:
    try:
        return datetime.strptime(str(ts)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _ensure_schedule_baseline(earliest_ts: datetime | None, interval: int) -> bool:
    """旧版本升级上来时没有任何计划记录：补一条以首份快照为起点的基线。

    这条记录只是"从有数据那一刻起，按当前间隔"的推定，不是伪造采样点——
    历史真实间隔无从查证，所以调用方会把这类推定标记为参考、不参与正式排名。
    """
    if db.row("SELECT id FROM schedule_history LIMIT 1"):
        return False
    stamp = earliest_ts or datetime.now()
    db.execute(
        "INSERT INTO schedule_history (changed_at, interval_minutes, source) VALUES (?,?,?)",
        (stamp.strftime("%Y-%m-%d %H:%M:%S"), int(interval), "baseline"),
    )
    return True


def _schedule_plan() -> list[tuple[datetime, int]]:
    """检测计划变更记录（changed_at, 间隔分钟）；旧库没有该表时返回空。"""
    try:
        rows = db.rows("SELECT changed_at, interval_minutes FROM schedule_history ORDER BY changed_at, id")
    except Exception:
        return []
    plan = []
    for row in rows:
        stamp = _parse_local(row["changed_at"])
        if stamp:
            plan.append((stamp, int(row["interval_minutes"])))
    return plan


def _weighted_mean(samples: list[dict], key: str) -> float | None:
    """时段加权均值：同一时段内的样本平分该时段权重。

    采样点已经由"计划驱动 + 一对一匹配"保证了代表性，这里不再按检测次数加权；
    某档没有样本就整档缺席、不补 0，剩余档按自身权重归一化。
    """
    buckets: dict[int, list[float]] = {}
    for item in samples:
        value = item.get(key)
        if value is None:
            continue
        _, band = _time_band(item.get("ts") or "")
        buckets.setdefault(band, []).append(float(value))
    weighted = 0.0
    total = 0.0
    for band, values in buckets.items():
        band_weight = TIME_BANDS[band][1] / len(values)
        for value in values:
            weighted += band_weight * value
            total += band_weight
    return weighted / total if total else None


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


_UPDATE_CACHE = {"at": 0.0, "data": None}
REPO_SLUG = "laoXuHome/NodePoolME"


def _parse_ver(text):
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").split("."):
        num = "".join(ch for ch in chunk if ch.isdigit())
        parts.append(int(num) if num else 0)
    return tuple(parts + [0, 0, 0])[:3]


def _get_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "NodePoolME-Update-Check",
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _latest_tag_web():
    """走网页版 releases/latest 跟随跳转拿 tag，绕开 API 未登录的 403 限流。"""
    req = urllib.request.Request(f"https://github.com/{REPO_SLUG}/releases/latest",
                                 headers={"User-Agent": "Mozilla/5.0 NodePoolME-Update"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        final = resp.geturl()
    tag = final.rstrip("/").split("/")[-1]
    return tag if tag.startswith("v") else None


def _asset_size(url):
    try:
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "NodePoolME-Update"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            length = resp.headers.get("Content-Length")
        return int(length) if length else None
    except Exception:
        return None


@api.get("/system/update", dependencies=protected)
def check_update(force: bool = False):
    """对比仓库最新 Release 的 amd64 离线包，判断是否有更新。"""
    now = datetime.now().timestamp()
    cached = _UPDATE_CACHE["data"]
    if cached and not force and now - _UPDATE_CACHE["at"] < 600:
        return cached
    current = config.VERSION
    latest, published, api_asset = None, None, None
    try:
        release = _get_json(f"https://api.github.com/repos/{REPO_SLUG}/releases/latest")
        latest = str(release.get("tag_name") or "").lstrip("v") or None
        published = release.get("published_at")
        assets = [a for a in (release.get("assets") or []) if str(a.get("name", "")).endswith(".tar.gz")]
        if assets:
            api_asset = {"name": assets[0].get("name"), "size": assets[0].get("size"),
                         "url": assets[0].get("browser_download_url")}
    except Exception:
        pass
    if not latest:
        try:
            tag = _latest_tag_web()
            if tag:
                latest = tag.lstrip("v")
        except Exception as exc:
            return {"current": current, "latest": None, "has_update": False,
                    "error": f"无法访问 GitHub：{exc.__class__.__name__}"}
    if not latest:
        return {"current": current, "latest": None, "has_update": False,
                "error": "未能解析仓库最新版本"}
    asset = api_asset
    if not asset:
        name = f"nodepoolme-v{latest}-amd64.tar.gz"
        asset = {"name": name, "size": _asset_size(
            f"https://github.com/{REPO_SLUG}/releases/download/v{latest}/{name}"),
            "url": f"https://github.com/{REPO_SLUG}/releases/download/v{latest}/{name}"}
    data = {
        "current": current,
        "latest": latest,
        "has_update": _parse_ver(latest) > _parse_ver(current),
        "published_at": published,
        "html_url": f"https://github.com/{REPO_SLUG}/releases/tag/v{latest}",
        "asset": asset,
        "error": None,
    }
    _UPDATE_CACHE.update(at=now, data=data)
    return data


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
                   JOIN nodes n ON n.id = r.node_id
                   JOIN airports a ON a.id = n.airport_id AND a.enabled = 1
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
async def dashboard(days: int = 7):
    """days 控制趋势曲线的历史窗口（1/7/30），评分窗口固定 7 天不受影响。"""
    history_days = days if days in (1, 7, 30) else 7
    airports = fetcher.all_airports()   # needs daily_time / interval_minutes for the next-run math
    # Only nodes from enabled airports belong to the pool
    pool = "SELECT COUNT(*) AS c FROM nodes n JOIN airports a ON a.id=n.airport_id WHERE a.enabled=1"
    nodes_total = db.row(pool)["c"]
    unavailable = db.row(pool + " AND n.status='unavailable'")["c"]
    available = db.row(pool + " AND n.status='available'")["c"]
    selected, _ = builder.collect_nodes()
    last_batch = db.row("SELECT * FROM batches ORDER BY id DESC LIMIT 1")
    logs = db.rows("SELECT * FROM logs ORDER BY id DESC LIMIT 12")
    published_by_airport = {}
    for node in selected:
        aid = node.get("airport_id")
        published_by_airport[aid] = published_by_airport.get(aid, 0) + 1
    status_by_airport: dict[int, dict[str, int]] = {}
    for row in db.rows(
        """SELECT n.airport_id, n.status, COUNT(*) AS c FROM nodes n
           JOIN airports a ON a.id=n.airport_id AND a.enabled=1
           GROUP BY n.airport_id, n.status"""
    ):
        status_by_airport.setdefault(row["airport_id"], {})[row["status"]] = row["c"]
    now = datetime.now().astimezone()
    score_days = 7
    score_start = (now - timedelta(days=score_days)).strftime("%Y-%m-%d %H:%M:%S")
    # 标签合格：每个节点在任一启用标签、最近 7 天内的最新一次检测中通过
    tag_by_airport: dict[int, int] = {
        row["airport_id"]: row["c"]
        for row in db.rows(
            """SELECT n.airport_id, COUNT(DISTINCT r.node_id) AS c
               FROM results r
               JOIN (SELECT node_id, group_id, MAX(id) AS mx FROM results
                     GROUP BY node_id, group_id) m
                 ON m.node_id=r.node_id AND m.group_id=r.group_id AND m.mx=r.id
               JOIN test_groups g ON g.id=r.group_id AND g.enabled=1
               JOIN nodes n ON n.id=r.node_id
               JOIN airports a ON a.id=n.airport_id AND a.enabled=1
               WHERE r.ok=1 AND r.ts >= ?
               GROUP BY n.airport_id""",
            (score_start,),
        )
    }
    builder.ensure_share_history(score_days)
    history = builder.share_history(max(history_days, score_days))
    if len(history) > 480:   # 30 天窗口可能上千个快照，抽稀到约 480 个点，末点保留
        step = math.ceil(len(history) / 480)
        history = [p for i, p in enumerate(history) if i % step == 0 or i == len(history) - 1]
    score_history = builder.share_history(score_days, complete_only=True)
    snapshot_stamps: list[datetime] = []
    snapshot_points: list[dict] = []
    for point in score_history:
        stamp = _parse_local(point.get("ts"))
        if stamp:
            snapshot_stamps.append(stamp)
            snapshot_points.append(point)
    local_now = now.replace(tzinfo=None)
    window_start = local_now - timedelta(days=score_days)
    if snapshot_stamps:
        window_start = max(window_start, min(snapshot_stamps))
    plan_estimated = False
    if not _schedule_plan():
        # 升级上来的库：以首份快照为起点补一条推定基线，覆盖率才不会整周空白
        interval = db.get_int("test_interval_minutes", 30)
        plan_estimated = _ensure_schedule_baseline(
            min(snapshot_stamps) if snapshot_stamps else None, interval)
    else:
        first = db.row("SELECT changed_at, source FROM schedule_history ORDER BY changed_at, id LIMIT 1")
        baseline_stamp = _parse_local(first["changed_at"]) if first else None
        # 基线早于窗口起点：整个窗口都在推定间隔之下，等基线滚出窗口后按真实记录算
        plan_estimated = bool(first and (first.get("source") or "") == "baseline"
                              and baseline_stamp and baseline_stamp > window_start)
    segments = sampler.build_segments(_schedule_plan(), window_start, local_now)
    sample_points = sampler.generate_points(segments, snapshot_stamps, local_now)
    # 每日准入：整日 ≥4 份 → 60 分钟密度 ≤2 份 → 复核 ≥4 份，淘汰日的点不进分母
    abnormal = eligibility.build_abnormal_ranges(
        db.rows("SELECT started_at, finished_at, status FROM batches WHERE started_at >= ?",
                (window_start.strftime("%Y-%m-%d %H:%M:%S"),)),
        db.rows("SELECT ts, kind, batch_id FROM run_events WHERE ts >= ?",
                (window_start.strftime("%Y-%m-%d %H:%M:%S"),)),
        db.rows("SELECT ts, message FROM logs WHERE ts >= ? AND source='系统'",
                (window_start.strftime("%Y-%m-%d %H:%M:%S"),)),
        window_start, local_now,
    )
    selection = eligibility.select_snapshots(snapshot_stamps, sample_points, abnormal)
    score_days_set = selection.scored_days
    excluded_points = sum(1 for point in sample_points
                          if point.known and eligibility.is_excluded(point.time, abnormal))
    known_points = [point for point in sample_points
                    if point.known and eligibility.local_day(point.time) in score_days_set
                    and not eligibility.is_excluded(point.time, abnormal)]
    matched = sampler.match_points(known_points, selection.kept)
    coverage = (len(matched) / len(known_points)) if known_points else None
    plan_known = bool(segments) and all(segment.known for segment in segments)
    # 只有"计划要求、又被真实快照匹配上"的采样点参与评分；缺失的点不补零、不去凑
    snapshot_by_ts = {point["ts"][:19]: point for point in snapshot_points}
    score_samples: dict[int, list[dict]] = {}
    for point_index, snapshot_index in matched:
        point = snapshot_by_ts.get(selection.kept[snapshot_index].strftime("%Y-%m-%d %H:%M:%S"))
        if not point:
            continue
        point_total = point.get("total_nodes") or 0
        stamp = known_points[point_index].time.strftime("%Y-%m-%d %H:%M:%S")
        for airport_id, counts in (point.get("airports") or {}).items():
            pool = counts.get("pool_count") or 0
            if not pool:
                continue
            score_samples.setdefault(int(airport_id), []).append({
                "available": ((counts.get("available_count") or 0) / pool)
                if point.get("available_known", 1) else None,
                "published": (counts.get("published_count") or 0) / pool,
                "share": pool / point_total if point_total else 0,
                "ts": stamp,
            })
    ok_days = len(score_days_set)
    # 近 7 个自然日（含今天），分母固定 7；今天未走完时按已保留份数如实统计
    window_dates = [(local_now.date() - timedelta(days=i)).strftime("%Y-%m-%d")
                    for i in range(6, -1, -1)]
    full_days, qualified_days = eligibility.day_ratios(selection.day_stats, window_dates)
    maturity_value = eligibility.maturity_score(
        full_days, qualified_days, coverage if plan_known else None)
    maturity_status = ("insufficient" if qualified_days == 0
                       else eligibility.maturity_grade(maturity_value))
    if plan_estimated and maturity_status in ("ample", "normal"):
        maturity_status = "reference"
    day_stats_payload = [
        {"date": item.date, "eligible": item.eligible,
         "kept": item.kept, "reason": item.reason}
        for item in selection.day_stats
    ]
    fetch_next: list[str] = []
    manual_only = 0
    airport_list: list[dict] = []
    for airport in airports:
        nxt = fetcher.next_run_at(airport, now) if airport["enabled"] else None
        airport["next_run_at"] = nxt.isoformat(sep=" ", timespec="seconds") if nxt else None
        airport["manual_only"] = bool(airport["enabled"] and nxt is None)
        if airport["enabled"]:
            if nxt is None:
                manual_only += 1
            else:
                fetch_next.append(airport["next_run_at"])
        payload = _airport_payload(airport)
        payload["eligible_days"] = qualified_days
        payload["dropped_days"] = max(0, 7 - qualified_days)
        payload["day_stats"] = day_stats_payload
        payload["excluded_points"] = excluded_points
        payload["next_run_at"] = airport["next_run_at"]
        payload["manual_only"] = airport["manual_only"]
        payload["published_count"] = published_by_airport.get(airport["id"], 0)
        states = status_by_airport.get(airport["id"], {})
        pool_count = payload["pool_count"]
        avail_count = states.get("available", 0)
        payload["available_count"] = avail_count
        payload["unavailable_count"] = states.get("unavailable", 0)
        payload["unknown_count"] = states.get("unknown", 0)
        tag_count = min(pool_count, tag_by_airport.get(airport["id"], 0))
        payload["tag_ok_count"] = tag_count
        payload["tag_rate"] = round(tag_count / pool_count * 100, 1) if pool_count else 0.0
        samples = score_samples.get(airport["id"], [])
        payload["score_samples"] = len(samples)
        created = _parse_local(airport.get("created_at")) or local_now
        maturity_days = max(0.0, min((local_now - created).total_seconds() / 86400, score_days))
        payload["coverage"] = round(coverage * 100, 1) if (coverage is not None and plan_known) else None
        payload["maturity_days"] = round(maturity_days, 1)
        payload["maturity_score"] = round(maturity_value, 1)
        payload["full_days"] = full_days
        payload["qualified_days"] = qualified_days
        payload["data_status"] = maturity_status
        payload["data_status_text"] = DATA_STATUS_TEXT.get(maturity_status, maturity_status)
        payload["rank_tier"] = sampler.status_rank(maturity_status)
        payload["sampled_points"] = len(known_points)
        payload["plan_estimated"] = plan_estimated
        detail = f"{maturity_value:.0f} 分" + ("（推定）" if plan_estimated else "")
        if airport["enabled"] and pool_count:
            # 综合评分：近 7 天可用率 60% + 下发率 35% + 标签合格率 5%
            # 池占比是节点池自身的结构特征，不代表机场质量，不参与评分
            if samples:
                publish_rate = _weighted_mean(samples, "published")
                if publish_rate is None:
                    publish_rate = payload["published_count"] / pool_count
            else:
                publish_rate = payload["published_count"] / pool_count
            share = pool_count / nodes_total if nodes_total else 0
            health = _weighted_mean(samples, "available")
            if health is None:
                health = avail_count / pool_count
            tag_rate = tag_count / pool_count
            payload["available_rate"] = round(health * 100, 1)
            payload["publish_rate"] = round(publish_rate * 100, 1)
            payload["share_rate"] = round(share * 100, 1)
            score = round(100 * (
                SCORE_WEIGHTS["available"] / 100 * health
                + SCORE_WEIGHTS["publish"] / 100 * publish_rate
                + SCORE_WEIGHTS["tag"] / 100 * tag_rate
            ))
            raw = max(0, min(100, score))
            payload["raw_score"] = round(raw, 1)
            payload["bonus_score"] = round(bonus_score(raw), 2)
            shown = final_score(raw)
            if maturity_status == "insufficient":
                # 数据不足只表示缺证据，不代表机场质量差：评分留空，不参与正式排名
                payload["score"] = None
                payload["score_exact"] = None
                payload["score_grade"] = "—"
            else:
                # 展示用四舍五入整数，排序仍用保留精度的 score_exact
                payload["score_exact"] = round(shown, 2)
                payload["score"] = int(round(shown))
                payload["score_grade"] = score_grade_of(shown)
            payload["data_status_detail"] = detail
        else:
            payload["available_rate"] = None
            payload["publish_rate"] = None
            payload["share_rate"] = None
            payload["score"] = None
            payload["score_exact"] = None
            payload["raw_score"] = None
            payload["bonus_score"] = None
            payload["score_grade"] = "停用" if not airport["enabled"] else "空"
            payload["data_status_detail"] = detail
        airport_list.append(payload)
    airport_list.sort(key=lambda item: (
        0 if item.get("enabled") else 1,
        int(item.get("rank_tier") or 0),
        -float(item.get("score_exact") if item.get("score_exact") is not None else item["score"])
        if item.get("score_exact") is not None or item.get("score") is not None else 0,
        -(item.get("pool_count") or 0),
    ))
    enabled_airports = [a for a in airports if a["enabled"]]
    fetch_ok = sum(1 for a in enabled_airports if a["last_status"] == "ok")
    fetch_err = sum(1 for a in enabled_airports if a["last_status"] == "error")
    last_fetch = max((a["last_success_at"] or "" for a in enabled_airports), default="") or None
    fetch_block = {
        "total": len(enabled_airports),
        "enabled": len(enabled_airports),
        "ok": fetch_ok,
        "error": fetch_err,
        "manual_only": manual_only,
        "fetching": len(manager.state.fetching),
        "last_success_at": last_fetch,
        "next_at": min(fetch_next) if fetch_next else None,
    }
    last_publish = db.row("SELECT * FROM batches WHERE published=1 ORDER BY id DESC LIMIT 1")
    group_rows = _group_stats()
    hits = db.row("""SELECT COUNT(*) AS c FROM (
        SELECT DISTINCT r.node_id FROM results r
        JOIN nodes n ON n.id = r.node_id
        JOIN airports a ON a.id = n.airport_id AND a.enabled = 1
        WHERE r.ok = 1) t""")["c"]
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
        "nodes_total": nodes_total,
        "nodes_available": available,
        "nodes_unavailable": unavailable,
        "nodes_unknown": max(0, nodes_total - available - unavailable),
        "nodes_qualified": len(selected),
        "share_history": history,
        "score_window_days": score_days,
        "score_weights": SCORE_WEIGHTS,
        "airport_list": airport_list,
        "batch": last_batch,
        "runtime": manager.state.as_dict(),
        "logs": logs,
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
        "pool_count": row["node_count"] if row["enabled"] else 0,
        "filter_keywords": fetcher.normalize_keywords(row.get("filter_keywords")),
        "filtered_count": filtered,
        "speed_excluded": bool(row.get("speed_excluded")),
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
    data = payload.model_dump(exclude_unset=True)
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
                          f"{airport['name']}：已停用，节点已退出缓存池，重新启用后恢复")
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
        raise HTTPException(status_code=404, detail="被过滤节点不存在，可能已加入缓存池")
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
        raise HTTPException(status_code=400, detail="标识已被其他节点标签使用")
    group_id = db.execute(
        """INSERT INTO test_groups (name, ident, url, method, timeout, expect_status, expect_body,
           max_latency, interval_minutes, enabled, sort_order, is_default, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload.name, payload.ident.strip(), payload.url, payload.method.upper(),
         db.get_int("latency_test_timeout", 8),
         payload.expect_status, payload.expect_body, payload.max_latency, 30,
         int(payload.enabled), payload.sort_order, 0, db.now()),
    )
    db.append_log("INFO", "节点标签", f"新增节点标签：{payload.name}/{payload.ident}")
    return _group_payload(db.row("SELECT * FROM test_groups WHERE id=?", (group_id,)))


@api.patch("/groups/{group_id}", dependencies=protected)
async def update_group(group_id: int, payload: GroupPatch):
    group = db.row("SELECT * FROM test_groups WHERE id=?", (group_id,))
    if not group:
        raise HTTPException(status_code=404, detail="节点标签不存在")
    data = payload.model_dump(exclude_unset=True)
    # timeout and cadence are global now: ignore whatever the client still sends
    data.pop("timeout", None)
    data.pop("interval_minutes", None)
    if "ident" in data and data["ident"]:
        if db.row("SELECT id FROM test_groups WHERE ident=? AND id<>?", (data["ident"], group_id)):
            raise HTTPException(status_code=400, detail="标识已被其他节点标签使用")
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
        raise HTTPException(status_code=404, detail="节点标签不存在")
    if group["is_default"]:
        raise HTTPException(status_code=400,
                            detail="默认节点标签不能删除，请改为停用")
    db.execute("DELETE FROM test_groups WHERE id=?", (group_id,))
    db.execute("DELETE FROM results WHERE group_id=?", (group_id,))
    db.append_log("WARN", "节点标签", f"删除节点标签：{group['name']}")
    return {"ok": True}


# ---------------------------------------------------------------------------- nodes
def _node_names() -> dict[int, str]:
    groups = db.rows("SELECT id, ident, sort_order FROM test_groups WHERE enabled=1 ORDER BY sort_order, id")
    results = builder.latest_results()
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
    results = builder.latest_results()
    latests = builder.latest_latency()
    ceiling = db.get_int("sub_max_latency", 1000)
    ceiling2 = db.get_int("sub_max_latency2", 1000)
    latency_labels = _latency_labels()
    idents = _node_names()
    speeds = speedtest.speed_map()
    items = []
    for node in nodes:
        per_group = results.get(node["id"], {})
        passed_idents = idents.get(node["id"], [])
        display = (f"{node['airport_name']}{builder.NAME_SEP}"
                   f"{builder.TAG_SEP.join(passed_idents)}{builder.NAME_SEP}{node['raw_name']}"
                   if passed_idents
                   else f"{node['airport_name']}{builder.NAME_SEP}未通过{builder.NAME_SEP}{node['raw_name']}")
        speed = speeds.get(node["id"]) or {}
        node["display_name"] = speedtest.apply_speed_prefix(
            display, speed.get("marker") or speedtest.PENDING_PREFIX)
        if speed:
            speed["multi_mbps"] = speed.get("mbps")
        node["speed"] = speed
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
        node["publishable"] = bool(good)
        if node["publishable"]:
            node["publish_reason"] = "下发"
        elif not targets:
            node["publish_reason"] = "无可用延迟数据"
        elif node["latency_targets"]["G"]["best"] is None:
            node["publish_reason"] = f"{latency_labels['G']['name']} 未通过（上限 {ceiling}ms）"
        else:
            node["publish_reason"] = f"{latency_labels['W']['name']} 未通过（上限 {ceiling2}ms）"
        node["results"] = {
            str(gid): {**res, "ident": groups.get(gid, {}).get("ident", "?")}
            for gid, res in per_group.items() if gid in groups
        }
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
    state = speedtest.speed_map().get(node_id) or {}
    if state:
        state["multi_mbps"] = state.get("mbps")
    node["speed"] = state
    node["speed_history"] = speedtest.node_history(node_id, 10)
    good = all(node["latency_targets"][t]["best"] is not None for t in ("G", "W"))
    node["publishable"] = bool(good)
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
        raise HTTPException(status_code=409, detail="缓存池为空")
    task = asyncio.create_task(manager.run_cycle(force=True, reason="manual"))
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


@api.get("/runtime", dependencies=protected)
async def runtime():
    return manager.state.as_dict()


# ---------------------------------------------------------------------------- subscription
def _sub_link(token: str) -> str:
    return f"/sub/all?token={token}"


@api.get("/subscription", dependencies=protected)
async def subscription_info(request: Request):
    token = auth.sub_token()
    # 只读提示：当前关键词过滤命中了多少个已筛选出的合格节点（不影响检测与评分）
    keywords = builder.filter_keywords()
    speed_levels = builder.speed_filter_tags()
    eligible, _ = builder.collect_nodes()
    keyword_kept, keyword_dropped = builder.apply_name_filter(eligible)
    kept, speed_dropped = builder.apply_speed_filter(keyword_kept)
    dropped = keyword_dropped + speed_dropped
    loon_kept, loon_skipped = builder.loon_plan(kept)
    filter_hint = {
        "keywords": keywords,
        "speed_levels": speed_levels,
        "eligible": len(eligible),
        "kept": len(kept),
        "dropped": len(dropped),
        "keyword_dropped": len(keyword_dropped),
        "speed_dropped": len(speed_dropped),
        "active": bool(keywords or speed_levels),
        "sample": [d.get("name") for d in dropped[:5]],
        # 订阅输出页的两个列表：实际下发节点 / 被下发过滤剔除的节点
        "nodes": [n.get("display_name") for n in kept],
        "filtered": [{"name": d.get("name"), "keyword": d.get("keyword"),
                      "reason": d.get("reason"), "reason_type": d.get("reason_type"),
                      "grade": d.get("grade")} for d in dropped],
        "loon": {"available": len(loon_kept), "excluded": sum(loon_skipped.values()),
                 "reasons": loon_skipped},
    }
    return {
        "filter": filter_hint,
        "settings": {
            "sub_name": db.get("sub_name"),
            "sub_token": token,
            "sub_filter_keywords": db.get("sub_filter_keywords") or "",
            "sub_filter_speed_levels": ",".join(builder.speed_filter_tags()),
        },
        "meta": builder.meta(),
        "links": {
            "path": _sub_link(token),
            "loon": _sub_link(token) + "&format=loon",
            "clash": _sub_link(token) + "&format=clash",
            "singbox": _sub_link(token) + "&format=singbox",
            "base64": _sub_link(token) + "&format=base64",
        },
        "base_url": str(request.base_url).rstrip("/"),
        "queryless": {
            "loon": f"/sub/all/{token}/loon",
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
        token = payload.sub_token.strip()
        if token:
            db.set_setting("sub_token", token)
    if "sub_filter_keywords" in payload.model_fields_set:
        db.set_setting("sub_filter_keywords", (payload.sub_filter_keywords or "").strip())
    if "sub_filter_speed_levels" in payload.model_fields_set:
        raw = (payload.sub_filter_speed_levels or "").strip()
        try:
            tags = builder.parse_speed_filter_tags(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        db.set_setting("sub_filter_speed_levels",
                       ",".join(t for t in builder.SPEED_FILTER_ORDER if t in tags))
    return {"ok": True}


@api.post("/subscription/rebuild", dependencies=protected)
async def rebuild_subscription():
    meta = builder.publish(reason="manual rebuild")
    return meta


# ---------------------------------------------------------------------------- settings
SETTING_KEYS = ["global_concurrency", "group_concurrency", "ip_concurrency", "subnet_concurrency",
                "request_interval_min_ms", "request_interval_max_ms", "test_timeout",
                "test_interval_minutes", "test_on_start", "fail_threshold", "history_days",
                "dispersion_mask",
                "latency_test_url", "latency_test_url2", "latency_test_name",
                "latency_test_name2", "latency_test_status",
                "latency_test_timeout", "sub_max_latency", "sub_max_latency2",
                "sub_publish_enabled", "sub_publish_time", "sub_publish_mode",
                "sub_publish_interval_minutes",
                "speed_enabled", "speed_connections", "speed_warmup_seconds",
                "speed_sample_seconds", "speed_connect_timeout", "speed_total_timeout",
                "speed_retry", "speed_schedule_enabled", "speed_timezone",
                "speed_window_start", "speed_window_end", "speed_schedule_every_days",
                "speed_traffic_limit_mb", "speed_result_ttl_days",
                "speed_grade_excellent", "speed_grade_good", "speed_grade_medium",
                "speed_grade_poor",
                "speed_auto_enabled", "speed_auto_interval_minutes",
                "speed_auto_nodes"]


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
    data = payload.model_dump(exclude_unset=True)
    if any(k.startswith("speed_grade_") for k in data):
        values = []
        for key, default in (("excellent", 5), ("good", 2), ("medium", 1), ("poor", 0.25)):
            raw = data.get(f"speed_grade_{key}", db.get(f"speed_grade_{key}", str(default)))
            values.append(float(raw or default))
        if not (values[0] > values[1] > values[2] > values[3] > 0):
            raise HTTPException(status_code=400, detail="速度阈值必须满足 优 > 良 > 中 > 差 > 0")
    if "speed_window_start" in data or "speed_window_end" in data:
        start = data.get("speed_window_start", db.get("speed_window_start"))
        end = data.get("speed_window_end", db.get("speed_window_end"))
        try:
            sh, sm = [int(x) for x in str(start).split(":")]
            eh, em = [int(x) for x in str(end).split(":")]
            if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59):
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="测速窗口时间格式必须为 HH:MM")
        if (eh * 60 + em) <= (sh * 60 + sm):
            raise HTTPException(status_code=400, detail="测速窗口结束时间必须晚于开始时间")
    for key, value in data.items():
        if value is None:
            continue
        if key == "dispersion_mask" and value not in (8, 16, 24, 32):
            raise HTTPException(status_code=400, detail="相似节点分散粒度只能是 8、16、24 或 32")
        db.set_setting(key, value)
    if "test_interval_minutes" in data:
        # 间隔变化要留档：评分按"当时的计划"生成采样点，历史口径不追溯
        db.record_schedule_plan(db.get_int("test_interval_minutes", 30), "settings")
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
    if "speed_window_start" in data or "speed_window_end" in data:
        db.append_log("INFO", "测速",
                      f"测速窗口已改为 {db.get('speed_window_start')}—{db.get('speed_window_end')}")
    if any(k in data for k in ("speed_schedule_enabled", "speed_schedule_every_days")):
        db.append_log("INFO", "测速",
                      f"定时测速：{'启用' if db.get('speed_schedule_enabled') == '1' else '停用'}，"
                      f"每 {db.get_int('speed_schedule_every_days', 1)} 天一次")
    if "speed_traffic_limit_mb" in data:
        db.append_log("INFO", "测速", f"单节点单次流量上限改为 {db.get_int('speed_traffic_limit_mb', 50)} MB")
    db.append_log("INFO", "设置", f"设置已更新，共 {len(data)} 项")
    return {"ok": True}


# ---------------------------------------------------------------------------- download speed
def _speed_guard():
    if db.get("speed_enabled", "1") != "1":
        raise HTTPException(status_code=403, detail="下载测速模块已停用")


@api.get("/speed/overview", dependencies=protected)
async def speed_overview():
    airports = [{"id": a["id"], "name": a["name"], "enabled": bool(a["enabled"]),
                 "speed_excluded": bool(a.get("speed_excluded"))}
                for a in fetcher.all_airports()]
    return {**speedtest.overview(), "items": speedtest.list_sources(),
            "queue": speed_queue.snapshot(), "schedule": schedule_state(),
            "airports": airports}


@api.get("/speed/sources", dependencies=protected)
async def speed_sources():
    return {"items": speedtest.list_sources(), "settings": speedtest._settings()}


@api.get("/speed/queue", dependencies=protected)
async def speed_queue_status():
    return {"queue": speed_queue.snapshot(), "schedule": schedule_state()}


@api.get("/speed/nodes", dependencies=protected)
async def speed_node_list():
    states = speedtest.speed_map()
    rows = db.rows(
        """SELECT n.id, n.raw_name, a.name AS airport_name, a.enabled AS airport_enabled,
                  a.speed_excluded
           FROM nodes n JOIN airports a ON a.id=n.airport_id
           WHERE a.enabled=1 ORDER BY a.name, n.raw_name"""
    )
    items = []
    for row in rows:
        state = states.get(row["id"]) or {}
        base = f"{row['airport_name']}·{row['raw_name']}"
        items.append({
            "id": row["id"],
            "name": speedtest.apply_speed_prefix(base,
                                                 state.get("marker") or speedtest.PENDING_PREFIX),
            "airport": row["airport_name"],
            "excluded": bool(row["speed_excluded"]),
            "speed": state,
        })
    return {"items": items}


@api.patch("/speed/sources/{source_id}", dependencies=protected)
async def speed_source_update(source_id: int, payload: SpeedSourcePatch):
    source = db.row("SELECT * FROM speed_sources WHERE id=?", (source_id,))
    if not source:
        raise HTTPException(status_code=404, detail="测速源不存在")
    data = payload.model_dump(exclude_unset=True)
    if "url" in data and data["url"]:
        if not data["url"].lower().startswith("https://"):
            raise HTTPException(status_code=400, detail="测速源必须是 HTTPS 地址")
        if db.row("SELECT id FROM speed_sources WHERE url=? AND id<>?",
                  (data["url"], source_id)):
            raise HTTPException(status_code=409, detail="该测速源地址已存在")
    for key, value in data.items():
        if value is None:
            continue
        db.execute(f"UPDATE speed_sources SET {key}=?, updated_at=? WHERE id=?",
                   (int(value) if isinstance(value, bool) else value, db.now(), source_id))
    if data.get("url") or data.get("enabled"):
        # A changed or re-enabled source must pass a fresh health check.
        db.execute("UPDATE speed_sources SET health='unknown', range_ok=0, last_error=NULL "
                   "WHERE id=?", (source_id,))
    db.append_log("INFO", "测速", f"测速源 #{source_id} 配置已更新")
    return db.row("SELECT * FROM speed_sources WHERE id=?", (source_id,))


@api.post("/speed/sources/check", dependencies=protected)
async def speed_source_check(payload: dict | None = None):
    ids = (payload or {}).get("source_ids") or []
    checks = await speedtest.check_sources([int(x) for x in ids] if ids else None)
    ok = len([c for c in checks if c["ok"]])
    db.append_log("INFO", "测速", f"测速源健康检查完成：{ok}/{len(checks)} 正常")
    return {"checks": checks, "ok": ok, "total": len(checks),
            "items": speedtest.list_sources()}


@api.post("/speed/test", dependencies=protected)
async def speed_test_node(payload: SpeedTestIn):
    _speed_guard()
    try:
        job, _ = speed_queue.enqueue(payload.node_id, "manual",
                                     source_ids=payload.source_ids)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    run = await speed_queue.wait(job, timeout=90)
    if not run:
        raise HTTPException(status_code=500, detail="测速结果写入失败")
    db.append_log("INFO", "测速",
                  f"节点 #{payload.node_id} 手动测速：{run.get('status')}，"
                  f"{run.get('sources_ok', 0)}/{run.get('sources_total', 0)} 源有效")
    return run


@api.post("/speed/run/all", dependencies=protected)
async def speed_run_all():
    _speed_guard()
    ids = speedtest.eligible_node_ids()
    result = speed_queue.enqueue_many(ids, "all")
    db.append_log("INFO", "测速", f"全部重新测速：{result['created']}/{len(ids)} 个节点入队")
    return {"ok": True, **result, "queue": speed_queue.snapshot()}


@api.post("/speed/run/new", dependencies=protected)
async def speed_run_new():
    _speed_guard()
    ids = speedtest.eligible_node_ids(new_only=True)
    result = speed_queue.enqueue_many(ids, "new")
    db.append_log("INFO", "测速", f"新节点测速：{result['created']}/{len(ids)} 个节点入队")
    return {"ok": True, **result, "queue": speed_queue.snapshot()}


@api.patch("/speed/airports/{airport_id}", dependencies=protected)
async def speed_airport_exclusion(airport_id: int, payload: dict):
    airport = db.row("SELECT id, name FROM airports WHERE id=?", (airport_id,))
    if not airport:
        raise HTTPException(status_code=404, detail="机场不存在")
    if "speed_excluded" not in payload:
        raise HTTPException(status_code=400, detail="缺少 speed_excluded")
    excluded = bool(payload["speed_excluded"])
    db.execute("UPDATE airports SET speed_excluded=?, updated_at=? WHERE id=?",
               (1 if excluded else 0, db.now(), airport_id))
    db.append_log("INFO", "测速",
                  f"机场「{airport['name']}」{'已排除测速' if excluded else '已恢复测速'}")
    return {"ok": True, "airport_id": airport_id, "speed_excluded": excluded}


@api.get("/speed/node/{node_id}", dependencies=protected)
async def speed_node_history(node_id: int, limit: int = 10):
    if not db.row("SELECT id FROM nodes WHERE id=?", (node_id,)):
        raise HTTPException(status_code=404, detail="节点不存在")
    return {"node_id": node_id, "items": speedtest.node_history(node_id, limit)}


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
        if "loon" in low:
            fmt = "loon"
        elif "sing-box" in low or "singbox" in low:
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
    speed_raw = None
    if raw_query:
        repaired = _deep_unescape(raw_query)
        parsed = urllib.parse.parse_qs(repaired)
        speed_raw = (parsed.get("speed") or [None])[0]
    try:
        levels = builder.parse_speed_levels(speed_raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        fmt, content = builder.read(fmt, levels)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="订阅尚未生成")
    media = {"clash": "text/yaml; charset=utf-8", "singbox": "application/json; charset=utf-8",
             "base64": "text/plain; charset=utf-8", "loon": "text/plain; charset=utf-8"}[fmt]
    ext = {"clash": "yaml", "singbox": "json", "base64": "txt", "loon": "txt"}[fmt]
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
