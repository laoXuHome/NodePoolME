"""P4 evaluation orchestration: evidence -> quality/maturity/speed -> np4 tables."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from backend.database import db
from backend.business import store as business_store
from backend.quality import calculator, store
from backend.services import speed_queue
from backend.testers import speedtest

EVAL_STALE_SECONDS = 600


def _now() -> datetime:
    return datetime.now()


def _log(message: str) -> None:
    db.append_log("INFO", "评价", message)


def _p2_checks(node_id: int, start: datetime, end: datetime) -> list[dict]:
    return db.rows(
        """SELECT started_at, finished_at, status, reason, p80_ms, health_pass
           FROM np2_checks
           WHERE node_id=? AND status IN ('success','failure')
             AND COALESCE(finished_at, started_at) >= ?
             AND COALESCE(finished_at, started_at) <= ?
           ORDER BY COALESCE(finished_at, started_at), id""",
        (int(node_id), start.strftime("%Y-%m-%d %H:%M:%S"),
         end.strftime("%Y-%m-%d %H:%M:%S")),
    )


def evaluate_node(node_id: int, *, settings: dict | None = None,
                  now: datetime | None = None, save: bool = True,
                  sync_speed: bool = True) -> dict:
    cfg = settings or store.get_settings()
    at = now or _now()
    start = at - timedelta(days=int(cfg.get("window_days") or 7))
    node = db.row(
        """SELECT n.*, a.name AS airport_name, a.speed_excluded, a.enabled AS airport_enabled
           FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id WHERE n.id=?""",
        (int(node_id),),
    )
    if not node:
        raise ValueError("节点不存在")
    checks = _p2_checks(node_id, start, at)
    quality = calculator.quality_from_checks(checks, cfg, at)
    maturity = calculator.maturity_from_quality(quality, cfg, at)
    if sync_speed:
        store.sync_speed_meta()
    runs = store.speed_runs_for(node_id, limit=60)
    speed = calculator.speed_evaluation(runs, cfg, excluded=bool(node.get("speed_excluded")), now=at)
    reasons = list(dict.fromkeys((quality.get("reasons") or []) + (maturity.get("reasons") or [])))
    pass_line = float(cfg.get("quality_pass") or 75)
    quality_score = quality.get("quality_score")
    mature = bool(maturity.get("mature"))
    quality_pass = bool(quality_score is not None and mature and quality_score >= pass_line)
    if quality_score is None or not mature:
        quality_state = "unknown"   # observation, never a zero/failure verdict
    elif quality_pass:
        quality_state = "pass"
    else:
        quality_state = "fail"
    latest_run = speed.get("run") or {}
    result = {
        "node_id": int(node_id),
        "node_name": node.get("current_name") or node.get("first_name") or f"节点 {node_id}",
        "airport_id": node.get("airport_id"),
        "airport_name": node.get("airport_name"),
        "airport_enabled": bool(node.get("airport_enabled")),
        "excluded": bool(node.get("speed_excluded")),
        "layer": node.get("management_layer"),
        "base_health": node.get("base_health"),
        "data_status": node.get("data_status"),
        "quality_score": quality.get("quality_score"),
        "available_score": quality.get("available_score"),
        "delay_score": quality.get("delay_score"),
        "stability_score": quality.get("stability_score"),
        "availability": quality.get("availability"),
        "p80_ms": quality.get("p80_ms"),
        "p80_stddev_ms": quality.get("p80_stddev_ms"),
        "valid_snapshots": quality.get("valid_snapshots", 0),
        "success_snapshots": quality.get("success_snapshots", 0),
        "failure_snapshots": quality.get("failure_snapshots", 0),
        "severe_days": quality.get("severe_days", 0),
        "max_consecutive_failures": quality.get("max_consecutive_failures", 0),
        "band_stats": quality.get("band_stats") or {},
        "daily": quality.get("daily") or [],
        "maturity_score": maturity.get("maturity_score"),
        "mature": mature,
        "valid_days": maturity.get("valid_days"),
        "full_days": maturity.get("full_days"),
        "daily_completion": maturity.get("daily_completion"),
        "coverage": maturity.get("coverage"),
        "reasons": reasons,
        "quality_state": quality_state,
        "quality_pass": quality_pass,
        "quality_line": pass_line,
        "speed_grade": speed.get("key") or "empty",
        "speed_label": speed.get("label") or "无数据",
        "speed_emoji": speed.get("emoji") or "⚪",
        "speed_color": speed.get("color"),
        "speed_mb_s": speed.get("median_mb_s"),
        "speed_kb_s": speed.get("kb_per_s"),
        "speed_latest_mb_s": speed.get("latest_mb_s"),
        "speed_valid_runs": speed.get("valid_runs"),
        "speed_reason": speed.get("reason"),
        "speed_run_id": latest_run.get("id"),
        "speed_at": latest_run.get("finished_at") or latest_run.get("started_at"),
        "speed_bytes": int(latest_run.get("bytes_total") or 0),
        "speed_limit_bytes": int(cfg.get("speed_limit_mb") or 50) * 1024 * 1024,
        "speed_ttl_days": cfg.get("speed_ttl_days"),
        "config_version": int(cfg.get("config_version") or 1),
        "evaluated_at": at.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if save:
        store.save_evaluation(node_id, result, now=at)
    return result


def evaluate_all(*, force: bool = False, now: datetime | None = None) -> dict:
    at = now or _now()
    ensure_defaults()
    store.sync_speed_meta()
    latest = db.row("SELECT MAX(evaluated_at) AS at FROM np4_current")
    last = calculator.parse_stamp((latest or {}).get("at"))
    if not force and last and (at - last).total_seconds() < EVAL_STALE_SECONDS:
        return {"evaluated": 0, "skipped": True, "at": (latest or {}).get("at")}
    settings = store.get_settings()
    nodes = store.node_inventory()
    count = 0
    for node in nodes:
        evaluate_node(int(node["id"]), settings=settings, now=at, save=True,
                      sync_speed=False)
        count += 1
    _log(f"P4 七天质量评价刷新完成：{count} 个节点")
    return {"evaluated": count, "skipped": False,
            "at": at.strftime("%Y-%m-%d %H:%M:%S")}


def ensure_defaults() -> None:
    with db.transaction() as conn:
        store.ensure_defaults(conn)


def ensure_evaluations(*, force: bool = False) -> dict:
    return evaluate_all(force=force)


def summary() -> dict:
    ensure_defaults()
    ensure_evaluations()
    current = db.rows("SELECT * FROM np4_current")
    total = len(store.node_inventory())
    evaluated = len([r for r in current if r["quality_score"] is not None])
    mature = len([r for r in current if r["mature"]])
    insufficient = max(0, total - mature)
    passed = len([r for r in current if r["quality_pass"]])
    failed = len([r for r in current if r["mature"] and not r["quality_pass"]
                  and r["quality_score"] is not None])
    grades = {key: 0 for key in ("excellent", "good", "poor", "danger", "empty", "excluded")}
    for row in current:
        grades[row["speed_grade"]] = grades.get(row["speed_grade"], 0) + 1
    band_coverage = {band: 0 for band in calculator.BANDS}
    for row in current:
        detail = db.loads(row.get("detail_json"), {}) or {}
        for band, stats in (detail.get("band_stats") or {}).items():
            if int((stats or {}).get("valid") or 0) > 0:
                band_coverage[band] = band_coverage.get(band, 0) + 1
    latest_speed = db.row("SELECT MAX(finished_at) AS at FROM speed_runs WHERE status IN ('ok','partial')")
    airports = [dict(row) for row in db.rows(
        "SELECT id, name, speed_excluded FROM airports ORDER BY name")]
    excluded = [a for a in airports if a["speed_excluded"]]
    cfg = store.get_settings()
    queue = speed_queue.queue.snapshot()
    schedule = speed_queue.schedule_state()
    return {
        "nodes_total": total,
        "quality_evaluated": evaluated,
        "quality_mature": mature,
        "quality_insufficient": insufficient,
        "quality_pass": passed,
        "quality_fail": failed,
        "speed_grades": grades,
        "speed_tested": grades.get("excellent", 0) + grades.get("good", 0)
                        + grades.get("poor", 0) + grades.get("danger", 0),
        "speed_no_data": grades.get("empty", 0),
        "band_coverage": band_coverage,
        "speed_excluded": grades.get("excluded", 0),
        "speed_latest_at": (latest_speed or {}).get("at"),
        "excluded_airports": excluded,
        "all_airports": airports,
        "settings": cfg,
        "schedule": schedule,
        "queue": queue,
        "timezone": cfg.get("timezone"),
        "boundary": {
            "p1_write": False, "p2_write": False, "p3_write": False,
            "p4_only": True, "final_list": False,
        },
    }


def node_rows(*, page: int = 1, per_page: int = 20, q: str | None = None,
              quality_state: str | None = None, speed_grade: str | None = None) -> dict:
    ensure_defaults()
    ensure_evaluations()
    rows = []
    for node in store.node_inventory(q=q):
        current = store.current(int(node["id"]))
        if current is None:
            result = evaluate_node(int(node["id"]))
            current = store.current(int(node["id"]))
        if quality_state and (current or {}).get("quality_state") != quality_state:
            continue
        if speed_grade and (current or {}).get("speed_grade") != speed_grade:
            continue
        detail = (current or {}).get("detail") or {}
        item = {**node, **detail, **(current or {}),
                "node_name": node.get("current_name") or f"节点 {node['id']}",
                "reasons": (current or {}).get("reasons") or [],
                "quality_score": (current or {}).get("quality_score"),
                "maturity_score": (current or {}).get("maturity_score"),
                "quality_state": (current or {}).get("quality_state") or "unknown",
                "speed_grade": (current or {}).get("speed_grade") or "empty"}
        rows.append(item)
    rows.sort(key=lambda x: (x.get("quality_score") is None,
                             -(x.get("quality_score") or 0),
                             x.get("node_name") or ""))
    total = len(rows)
    start = (max(1, page) - 1) * per_page
    return {"items": rows[start:start + per_page], "total": total,
            "page": max(1, page), "per_page": per_page,
            "pages": (total + per_page - 1) // per_page}


def node_detail(node_id: int) -> dict:
    ensure_defaults()
    result = evaluate_node(int(node_id))
    node = db.row(
        """SELECT n.*, a.name AS airport_name, a.enabled AS airport_enabled,
                  a.speed_excluded FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id
           WHERE n.id=?""", (int(node_id),))
    if not node:
        raise ValueError("节点不存在")
    stored = store.current(node_id) or {}
    current = {**(stored.get("detail") or {}), **stored, **result}
    history = store.recent_evaluations(node_id, limit=40)
    speeds = store.speed_runs_for(node_id, limit=30)
    p2 = db.rows(
        """SELECT id, started_at, finished_at, status, reason, p80_ms, success_count,
                  failure_count, unknown_count, health_pass
           FROM np2_checks WHERE node_id=? ORDER BY id DESC LIMIT 12""",
        (int(node_id),),
    )
    p3 = []
    try:
        for code, state in business_store.states_for_node(node_id).items():
            p3.append({"business": code, "state": state.get("state"),
                       "label": business_store.BUSINESS_LABELS.get(code, code),
                       "last_reason": state.get("last_reason"),
                       "last_run_at": state.get("last_run_at")})
    except Exception:
        p3 = []
    return {"node": {**node, "stable_uid": node.get("identity_key")},
            "quality": current, "history": history, "speed_runs": speeds,
            "p2_recent": p2, "p3_states": p3,
            "detail": current.get("detail") or result}


def enqueue_speed(node_ids: list[int], mode: str = "manual") -> dict:
    ensure_defaults()
    nodes = [int(x) for x in node_ids]
    trigger = {"manual": "manual", "retest": "manual", "new": "new", "all": "all"}.get(mode, "manual")
    result = speed_queue.queue.enqueue_many(nodes, trigger)
    _log(f"P4 测速操作：{mode}，请求 {len(nodes)}，新建 {result['created']}，跳过 {result['duplicate']}")
    return result


def set_airport_excluded(airport_id: int, excluded: bool) -> dict:
    ensure_defaults()
    airport = db.row("SELECT id, name, speed_excluded FROM airports WHERE id=?", (int(airport_id),))
    if not airport:
        raise ValueError("机场不存在")
    db.execute("UPDATE airports SET speed_excluded=?, updated_at=? WHERE id=?",
               (1 if excluded else 0, db.now(), int(airport_id)))
    _log(f"机场「{airport['name']}」{'已排除测速与速度评价' if excluded else '已恢复测速与速度评价'}")
    store.record_event("airport_excluded" if excluded else "airport_restored",
                       reason=airport["name"], detail={"airport_id": airport_id})
    return {"airport_id": int(airport_id), "speed_excluded": bool(excluded)}


async def tick() -> dict:
    result = evaluate_all()
    return result
