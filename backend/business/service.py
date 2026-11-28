"""P3 business capability rounds, qualification and independent scheduling."""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta
from typing import Awaitable, Callable

from backend.business import store
from backend.database import db
from backend.health import prober
from backend.health import service as health_service
from backend.testers import engine as engine_mod

target_probe: Callable[..., Awaitable[dict]] = prober.probe_target
node_probe: Callable[..., Awaitable[dict]] = prober.probe_target
prepare_ports: Callable[[list[dict]], Awaitable[dict[int, int]]] = health_service.prepare_ports

_selftest_cache: dict[tuple[int, int], tuple[float, dict]] = {}
_batch_task: asyncio.Task | None = None
SELFTEST_TTL = 300.0
TARGET_DEADLINE = 20.0


def now() -> datetime:
    return datetime.now()


def stamp(value: datetime | None = None) -> str:
    return (value or now()).strftime("%Y-%m-%d %H:%M:%S")


def _log(message: str) -> None:
    with contextlib.suppress(Exception):
        db.append_log("INFO", "P3业务", message)


def _setting(key: str, default: int) -> int:
    return db.get_int(key, default)


def settings() -> dict:
    return store.settings_snapshot()


def update_settings(*, interval_minutes: int | None = None, valid_hours: int | None = None,
                    concurrency: int | None = None) -> dict:
    return store.update_settings(interval_minutes_value=interval_minutes,
                                 valid_hours_value=valid_hours,
                                 concurrency_value=concurrency)


def _node_dict(row: dict) -> dict:
    params = db.loads(row.get("params"), {}) if isinstance(row.get("params"), str) else row.get("params")
    return {
        "id": int(row["node_id"]), "airport_id": int(row.get("airport_id") or 0),
        "raw_name": row.get("current_name") or f"{row.get('server')}:{row.get('port')}",
        "current_name": row.get("current_name") or f"{row.get('server')}:{row.get('port')}",
        "protocol": row.get("protocol") or "", "server": row.get("server") or "",
        "port": int(row.get("port") or 0), "params": params or {},
        "region": row.get("region"),
        # The engine's block_ports map is keyed by fingerprint.  Reuse the P2
        # namespace so the same node identity owns the same SOCKS listener.
        "fingerprint": f"p2-{int(row['node_id'])}",
    }


def _unknown_result(business: str, target: dict, reason: str) -> dict:
    stages = [prober._unexecuted(name, reason) for name in store.STAGE_NAMES]
    return {
        "business": business, "target_id": int(target["id"]), "target_name": target["name"],
        "target_url": target["url"], "status": "unknown", "reason": reason,
        "http_status": None, "http_ms": None, "tls_ms": None, "tcp_ms": None,
        "stages": stages, "duration_ms": 0, "started_at": stamp(), "finished_at": stamp(),
        "target_selftest": "unknown",
    }


def _stage(result: dict, name: str) -> dict:
    return next((s for s in result.get("stages") or [] if s.get("stage") == name), {})


def _classify(result: dict) -> tuple[str, str | None]:
    """Classify a node's real HTTPS result, not merely DNS or TCP success."""
    if not result:
        return "unknown", "probe_result_missing"
    https = _stage(result, "https")
    if result.get("status") == "unknown":
        return "unknown", result.get("reason") or "probe_unknown"
    if https.get("executed") and https.get("status") == "success":
        return "success", None
    if https.get("executed") and https.get("status") == "failure":
        status = (https.get("detail") or {}).get("status")
        return "failure", "business_http_mismatch" if status is not None else "https_failed"
    if _stage(result, "tls").get("status") == "failure":
        return "failure", "tls_failed"
    if _stage(result, "tcp").get("status") == "failure":
        return "failure", "tcp_failed"
    return "unknown", result.get("reason") or "incomplete_https"


def _shape(result: dict, business: str, target: dict, selftest: str) -> dict:
    https = _stage(result, "https")
    tls = _stage(result, "tls")
    tcp = _stage(result, "tcp")
    classification, reason = _classify(result)
    return {
        "business": business, "target_id": int(target["id"]), "target_name": target["name"],
        "target_url": target["url"], "status": classification, "reason": reason,
        "http_status": (https.get("detail") or {}).get("status"),
        "http_ms": https.get("latency_ms"), "tls_ms": tls.get("latency_ms"),
        "tcp_ms": tcp.get("latency_ms"), "stages": result.get("stages") or [],
        "duration_ms": result.get("duration_ms") or 0,
        "target_selftest": selftest,
        "started_at": result.get("started_at") or stamp(),
        "finished_at": result.get("finished_at") or stamp(),
    }


def _target_available(result: dict) -> bool:
    return bool(result.get("available"))


def _record_target_probe(target: dict, result: dict) -> None:
    stages = result.get("stages") or []
    https = next((s for s in stages if s.get("stage") == "https"), {})
    available = _target_available(result)
    status = "success" if available else (result.get("status") if result.get("status") in store.CLASSIFICATIONS else "unknown")
    with db.transaction() as conn:
        store.record_target_check(
            int(target["id"]), status, available=available,
            http_status=(https.get("detail") or {}).get("status"),
            http_ms=https.get("latency_ms"),
            duration_ms=result.get("duration_ms"),
            detail={"reason": result.get("reason"), "available": available,
                    "stages": stages},
            conn=conn,
        )


async def _target_selftests(targets: list[dict], *, force: bool = False) -> dict[int, dict]:
    results: dict[int, dict] = {}
    async def one(target: dict) -> tuple[int, dict]:
        tid = int(target["id"])
        key = (tid, int(target["config_version"]))
        cached = _selftest_cache.get(key)
        if not force and cached and time.monotonic() - cached[0] < SELFTEST_TTL:
            return tid, cached[1]
        try:
            result = await target_probe(target, deadline_seconds=TARGET_DEADLINE)
        except Exception as exc:
            result = _unknown_result(target["business"], target,
                                     f"target_probe_exception:{type(exc).__name__}")
        _selftest_cache[key] = (time.monotonic(), result)
        try:
            _record_target_probe(target, result)
        except Exception:
            pass
        return tid, result

    for tid, result in await asyncio.gather(*(one(t) for t in targets)):
        results[int(tid)] = result
    return results


async def test_target(business: str, *, force: bool = True) -> dict:
    target = store.target(business)
    try:
        result = await target_probe(target, deadline_seconds=TARGET_DEADLINE)
    except Exception as exc:
        result = _unknown_result(business, target, f"target_probe_exception:{type(exc).__name__}")
    _record_target_probe(target, result)
    _selftest_cache[(int(target["id"]), int(target["config_version"]))] = (
        time.monotonic(), result
    )
    return {"target": target, "result": result, "available": _target_available(result)}


def _next_after_success(target_row: dict, conn) -> int:
    interval = store.effective_interval(target_row, conn)
    validity = store.valid_hours(conn) * 60
    return max(15, min(interval, validity))


def _advance(current: dict, classification: str, reason: str | None, target_row: dict,
             conn) -> tuple[str, int, int, str | None, str]:
    """Return (state, successes, failures, qualification_changed_at, next_due_at)."""
    state = current.get("state") or "untested"
    successes = int(current.get("consecutive_successes") or 0)
    failures = int(current.get("consecutive_failures") or 0)
    last_valid = current.get("last_valid_at")
    now_ts = now()
    if classification == "success":
        successes += 1
        failures = 0
        last_valid = stamp(now_ts)
        interval = _next_after_success(target_row, conn)
        next_due = stamp(now_ts + timedelta(minutes=interval))
        if state == "qualified":
            return "qualified", successes, failures, None, next_due
        if successes >= 2:
            return "qualified", successes, failures, stamp(now_ts), next_due
        return "pending", successes, failures, None, stamp(now_ts + timedelta(minutes=5))
    if classification == "unknown":
        next_state = "unknown" if state in ("untested", "unknown") else state
        return next_state, successes, failures, None, stamp(now_ts + timedelta(minutes=5))
    # failure
    failures += 1
    successes = 0
    if failures >= 3:
        changed = stamp(now_ts) if state != "suspended" else None
        return "suspended", successes, failures, changed, stamp(now_ts + timedelta(minutes=5))
    delay = 15 if failures == 1 else 30
    next_due = stamp(now_ts + timedelta(seconds=delay))
    if state == "qualified":
        return "qualified", successes, failures, None, next_due
    return "pending", successes, failures, None, next_due


def record_check(check_id: int, row: dict, result: dict, target_row: dict,
                 *, trigger: str | None = None, conn=None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return record_check(check_id, row, result, target_row, trigger=trigger,
                                conn=own_conn)
    store.ensure_schema(conn)
    check_row = store.check(check_id, conn=conn)
    if check_row["status"] != "running":
        store.finish_queue(conn, int(row.get("queue_id") or 0))
        return {"applied": False, "reason": "check_not_running"}
    if int(check_row["target_config_version"]) != int(target_row["config_version"]):
        conn.execute(
            "UPDATE np3_checks SET status='unknown',classification='unknown',reason='config_changed',"
            "finished_at=? WHERE id=?", (stamp(), int(check_id))
        )
        store.finish_queue(conn, int(row.get("queue_id") or 0))
        store._record_event(conn, "check_discarded", node_id=int(row["node_id"]),
                            business=row["business"], reason="业务检测目标配置已变更")
        return {"applied": False, "reason": "config_changed"}
    business = row["business"]
    classification = result["status"]
    current = store.get_state(int(row["node_id"]), business, conn=conn)
    state, successes, failures, changed_at, next_due = _advance(
        current, classification, result.get("reason"), target_row, conn
    )
    last_valid = stamp() if classification == "success" else current.get("last_valid_at")
    store.finish_check(
        conn, check_id, status=classification, reason=result.get("reason"),
        http_status=result.get("http_status"), http_ms=result.get("http_ms"),
        tls_ms=result.get("tls_ms"), duration_ms=result.get("duration_ms"),
        stages=result.get("stages") or [],
    )
    store.update_state(
        conn, int(row["node_id"]), business, state=state, successes=successes,
        failures=failures, classification=classification, reason=result.get("reason") or state,
        last_valid_at=last_valid, changed_at=changed_at, next_due_at=next_due,
        check_id=int(check_id),
    )
    before = current.get("state") or "untested"
    if changed_at and state == "qualified" and before != "qualified":
        last_q = conn.execute(
            "SELECT event FROM np3_events WHERE node_id=? AND business=? "
            "AND event IN ('qualification_granted','qualification_suspended','qualification_recovered') "
            "ORDER BY id DESC LIMIT 1", (int(row["node_id"]), business)
        ).fetchone()
        event = ("qualification_recovered"
                 if last_q and last_q["event"] == "qualification_suspended"
                 else "qualification_granted")
        store._record_event(conn, event, node_id=int(row["node_id"]), business=business,
                            reason="连续 2 次有效成功获得业务资格",
                            detail={"before": before, "after": state})
    elif changed_at and state == "suspended" and before == "qualified":
        store._record_event(conn, "qualification_suspended", node_id=int(row["node_id"]),
                            business=business, reason="连续 3 次有效失败暂停业务资格",
                            detail={"before": before, "after": state})
    elif classification == "unknown" and before != state:
        store._record_event(conn, "business_unknown", node_id=int(row["node_id"]),
                            business=business, reason=result.get("reason"))
    store.finish_queue(conn, int(row.get("queue_id") or 0))
    return {"applied": True, "check_id": int(check_id), "state": state,
            "classification": classification, "successes": successes, "failures": failures}


async def _run_batch(reserved: list[dict]) -> None:
    global _batch_task
    checks: dict[int, int] = {}
    done: set[int] = set()
    try:
        with db.transaction() as conn:
            store.ensure_defaults(conn)
            target_map = {t["business"]: t for t in store.list_targets(conn=conn)}
            for row in reserved:
                target_row = target_map.get(row["business"])
                if target_row is None or not target_row["enabled"]:
                    continue
                checks[int(row["id"])] = store.create_check(
                    conn, int(row["node_id"]), row["business"], target_row, row["trigger"]
                )
        unique = {}
        for row in reserved:
            unique.setdefault(int(row["node_id"]), row)
        nodes = [_node_dict(row) for row in unique.values()]
        try:
            selftests = await _target_selftests(list(target_map.values()))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(f"业务目标自测失败：{type(exc).__name__}: {exc}")
            selftests = {}
        try:
            async with engine_mod.operation_lock:
                ports = await prepare_ports(nodes)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(f"业务检测端口准备失败：{type(exc).__name__}: {exc}")
            ports = {}
        results: dict[int, dict] = {}
        for row in reserved:
            qid = int(row["id"])
            target_row = target_map.get(row["business"])
            if qid not in checks or target_row is None:
                continue
            if not target_row["enabled"]:
                results[qid] = _unknown_result(row["business"], target_row, "target_disabled")
                continue
            if not selftests.get(int(target_row["id"]), {}).get("available"):
                results[qid] = _unknown_result(row["business"], target_row,
                                               "probe_target_unavailable")
                continue
            port = ports.get(int(row["node_id"]))
            if not port:
                results[qid] = _unknown_result(row["business"], target_row, "engine_unavailable")
                continue
            try:
                raw = await node_probe(target_row, proxy_port=int(port),
                                       deadline_seconds=TARGET_DEADLINE)
                results[qid] = _shape(raw, row["business"], target_row,
                                      "available")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                results[qid] = _unknown_result(row["business"], target_row,
                                               f"probe_exception:{type(exc).__name__}")
        for row in reserved:
            qid = int(row["id"])
            check_id = checks.get(qid)
            if check_id is None:
                with db.transaction() as conn:
                    store.finish_queue(conn, int(row.get("queue_id") or 0))
                continue
            row["queue_id"] = qid
            try:
                record_check(check_id, row, results.get(qid) or _unknown_result(
                    row["business"], target_map[row["business"]], "probe_result_missing"),
                    target_map[row["business"]])
                done.add(qid)
            except Exception as exc:
                _log(f"业务结果落库失败 #{check_id}：{type(exc).__name__}: {exc}")
        _log(f"P3 完成 {len(reserved)} 个业务检测任务")
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        _log(f"P3 批次异常：{type(exc).__name__}: {exc}")
    finally:
        remaining = [int(r["id"]) for r in reserved if int(r["id"]) not in done]
        if remaining:
            try:
                with db.transaction() as conn:
                    for qid in remaining:
                        store.abandon_queue(conn, qid, checks.get(qid))
            except Exception as exc:
                _log(f"P3 队列释放失败：{type(exc).__name__}: {exc}")
        _batch_task = None


async def tick() -> dict:
    global _batch_task
    if _batch_task is not None and not _batch_task.done():
        return {"started": False, "reason": "batch_running"}
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        store.sync_states(conn)
        store.enqueue_due(conn, limit=1000)
        active = int(conn.execute(
            "SELECT COUNT(*) FROM np3_queue WHERE status='running'"
        ).fetchone()[0])
        limit = max(0, store.concurrency(conn) - active)
        reserved = store.reserve_queue(conn, limit=limit)
    if not reserved:
        return {"started": False, "reason": "nothing_due", "active": active}
    _batch_task = asyncio.create_task(_run_batch(reserved))
    _batch_task.add_done_callback(lambda _task: None)
    return {"started": True, "tasks": len(reserved), "active": active}


def enqueue_manual(node_ids: list[int], business: str | None = None) -> dict:
    if business is not None and business not in store.BUSINESSES:
        raise store.BusinessError("invalid_business", "业务编码必须是 gpt、youtube、telegram、github、global")
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        businesses = [business] if business in store.BUSINESSES else list(store.BUSINESSES)
        tasks = []
        for node_id in node_ids:
            for code in businesses:
                tasks.append((int(node_id), code))
        added = store.enqueue_tasks(tasks, trigger="manual", priority=0, conn=conn)
    return {"queued": added, "total": len(tasks),
            "note": "同一节点同一业务已在队列中时不会重复入队。"}


def enqueue_all() -> dict:
    with db.transaction() as conn:
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM np_nodes WHERE source_active=1 ORDER BY id"
        ).fetchall()]
    return enqueue_manual(ids)


def startup_reset() -> dict:
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        pending = store.recover_stale_queue(conn)
    _log(f"P3 启动恢复：队列 {pending} 条")
    return {"queue_recovered": pending}


def reset_runtime_for_tests() -> None:
    global _batch_task, _selftest_cache
    _selftest_cache = {}
    if _batch_task is not None and not _batch_task.done():
        _batch_task.cancel()
    _batch_task = None


async def wait_batch() -> None:
    if _batch_task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await _batch_task
