"""P2 health rounds, attribution, recovery scheduling and P1 integration."""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from backend.database import db
from backend.health import prober, store
from backend.nodepool import store as p1_store
from backend.testers import engine as engine_mod

TRIGGER_LABELS = {
    "normal": "正常周期",
    "exception_recheck": "异常快速复查",
    "observation": "观察池复查",
    "recovery": "恢复检查",
    "manual": "手动检测",
    "target": "目标测试",
    "config": "配置变更后重检",
}

# Injection points used by the test suite (no real network / Mihomo required).
target_probe: Callable[..., Awaitable[dict]] = prober.probe_direct
node_probe: Callable[..., Awaitable[dict]] = prober.probe_target
prepare_ports: Callable[[list[dict]], Awaitable[dict[int, int]]] = None  # assigned below

_selftest_cache: dict[int, tuple[float, dict]] = {}
_batch_task: asyncio.Task | None = None
_started = False
SELFTEST_TTL = 60.0


def now() -> datetime:
    return datetime.now()


def stamp(value: datetime | None = None) -> str:
    return (value or now()).strftime("%Y-%m-%d %H:%M:%S")


def _setting_int(key: str, default: int) -> int:
    return db.get_int(key, default)


def interval_minutes() -> int:
    return max(5, min(1440, _setting_int("p2_interval_minutes", 30)))


def concurrency() -> int:
    return max(1, min(50, _setting_int("p2_concurrency", 10)))


def get_settings() -> dict:
    return {
        "interval_minutes": interval_minutes(),
        "concurrency": concurrency(),
        "recheck_first_seconds": 15,
        "recheck_second_seconds": 30,
        "observation_minutes": 5,
        "recovery_successes": 2,
        "failure_threshold": 3,
        "target_deadline_seconds": 20,
        "defaults": {
            "dns_timeout_seconds": 3,
            "tcp_attempts": 5,
            "tcp_timeout_seconds": 3,
            "tcp_interval_seconds": 1,
            "tls_timeout_seconds": 5,
            "https_timeout_seconds": 8,
        },
    }


def update_settings(*, interval_minutes_value: int | None = None,
                    concurrency_value: int | None = None) -> dict:
    with db.transaction() as conn:
        if interval_minutes_value is not None:
            value = int(interval_minutes_value)
            if not 5 <= value <= 1440:
                raise store.HealthError("invalid_interval", "正常检测周期必须在 5–1440 分钟之间")
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('p2_interval_minutes',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),)
            )
        if concurrency_value is not None:
            value = int(concurrency_value)
            if not 1 <= value <= 50:
                raise store.HealthError("invalid_concurrency", "全局并发必须在 1–50 之间")
            conn.execute(
                "INSERT INTO settings(key,value) VALUES ('p2_concurrency',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),)
            )
    return get_settings()


async def _default_prepare_ports(nodes: list[dict]) -> dict[int, int]:
    """Prepare one Mihomo block and return ``node_id -> SOCKS port``.

    P2 is only meaningful through the node.  When Mihomo is unavailable the
    caller turns every target into Unknown instead of blaming the node.
    """
    engine = engine_mod.engine_manager.engine
    if not engine_mod.engine_manager.ready or engine is None or engine.name != "mihomo":
        return {}
    await engine_mod.engine_manager.prepare(nodes)
    block_ports = getattr(engine, "block_ports", {}) or {}
    out = {}
    for node in nodes:
        port = block_ports.get(node["fingerprint"])
        if port:
            out[int(node["id"])] = int(port)
    return out


prepare_ports = _default_prepare_ports


def _node_dict(row: dict) -> dict:
    params = db.loads(row.get("params"), {}) if isinstance(row.get("params"), str) else row.get("params")
    return {
        "id": int(row["node_id"]),
        "airport_id": int(row.get("airport_id") or 0),
        "raw_name": row.get("current_name") or f"{row.get('server')}:{row.get('port')}",
        "current_name": row.get("current_name") or f"{row.get('server')}:{row.get('port')}",
        "protocol": row.get("protocol") or "",
        "server": row.get("server") or "",
        "port": int(row.get("port") or 0),
        "params": params or {},
        "region": row.get("region"),
        "fingerprint": f"p2-{int(row['node_id'])}",
    }


def _unknown_target_result(target: dict, reason: str) -> dict:
    stages = [prober._unexecuted(name, reason) for name in store.STAGE_NAMES]
    return {
        "target_id": int(target["id"]), "target_name": target["name"],
        "target_url": target["url"], "status": "unknown", "reason": reason,
        "p80_ms": None, "tcp_valid_count": 0, "tls_ms": None, "https_ms": None,
        "https_status": None, "health_pass": False, "available": False,
        "stages": stages, "tcp_samples": [], "duration_ms": 0,
        "started_at": stamp(), "finished_at": stamp(), "p80_grade": prober.tcp_grade(None),
    }


def _check_summary(results: list[dict]) -> dict:
    success = len([r for r in results if r.get("status") == "success"])
    failure = len([r for r in results if r.get("status") == "failure"])
    unknown = len([r for r in results if r.get("status") == "unknown"])
    p80_values = [int(r["p80_ms"]) for r in results if r.get("p80_ms") is not None]
    p80 = max(p80_values) if p80_values else None
    if failure:
        status = "failure"
    elif unknown:
        status = "unknown"
    else:
        status = "success"
    reason = None
    if status == "failure":
        reason = next((r.get("reason") for r in results if r.get("status") == "failure"), "target_failure")
    elif status == "unknown":
        reason = next((r.get("reason") for r in results if r.get("status") == "unknown"), "target_unknown")
    elif not all(r.get("health_pass") for r in results):
        reason = "tcp_p80_observe"
    return {"status": status, "reason": reason, "success_count": success,
            "failure_count": failure, "unknown_count": unknown, "p80_ms": p80,
            "health_pass": bool(status == "success" and all(r.get("health_pass") for r in results))}


def _state_trigger(state: str) -> tuple[str, int]:
    if state == "exception":
        return "exception_recheck", 0
    if state in ("unhealthy", "recovering"):
        return "recovery", 0
    if state in ("delay",):
        return "observation", 0
    return "normal", 1


def _next_due(current: dict, *, classification: str, failures: int,
              successes: int, health_pass: bool, reason: str | None) -> tuple[str, str, int]:
    """Return ``(next_state, next_due_at, delay_seconds)``."""
    now_ts = now()
    if classification == "failure":
        if failures >= 3:
            return "unhealthy", stamp(now_ts + timedelta(minutes=5)), 300
        delay = 15 if failures == 1 else 30
        return "exception", stamp(now_ts + timedelta(seconds=delay)), delay
    if classification == "unknown":
        if current.get("state") == "healthy":
            delay = interval_minutes() * 60
        else:
            delay = 300
        return (current.get("state") if current.get("state") not in (None, "new") else "unknown",
                stamp(now_ts + timedelta(seconds=delay)), delay)
    if not health_pass:
        return "delay", stamp(now_ts + timedelta(minutes=5)), 300
    prior = current.get("state")
    if prior in ("exception", "recovering", "unhealthy"):
        if successes >= 2:
            return "healthy", stamp(now_ts + timedelta(minutes=interval_minutes())), interval_minutes() * 60
        return "recovering", stamp(now_ts + timedelta(minutes=5)), 300
    return "healthy", stamp(now_ts + timedelta(minutes=interval_minutes())), interval_minutes() * 60


def _apply_round(conn, *, check_row: dict, node: dict, results: list[dict],
                 summary: dict) -> dict:
    state = store.get_state(int(node["id"]), conn=conn)
    prior_state = state.get("state") or "new"
    failures = int(state.get("consecutive_failures") or 0)
    successes = int(state.get("consecutive_successes") or 0)
    classification = summary["status"]

    if classification == "failure":
        failures += 1
        successes = 0
    elif classification == "success":
        successes += 1
        failures = 0
    # Unknown never changes either counter.

    next_state, next_due, _ = _next_due(
        state, classification=classification, failures=failures,
        successes=successes, health_pass=summary.get("health_pass", False),
        reason=summary.get("reason"),
    )

    if classification == "unknown":
        # Never overwrite a previously valid health conclusion with Unknown.
        if prior_state not in ("healthy", "exception", "unhealthy", "recovering", "delay"):
            p1_store.apply_probe_health_conn(
                conn, int(node["id"]), health="unknown", data_status="insufficient",
                layer="observation", reason=summary.get("reason") or "probe_unknown",
                observation_reason="probe_unknown", last_error=summary.get("reason"),
            )
        health = None
        data_status = None
        observation_reason = None
    elif next_state == "healthy":
        health, data_status, observation_reason = "healthy", "sufficient", None
    elif next_state == "unhealthy":
        health, data_status = "unhealthy", "sufficient"
        observation_reason = "health_unhealthy"
    elif next_state == "exception":
        health, data_status = "unknown", "insufficient"
        observation_reason = "exception_pending"
    elif next_state == "recovering":
        health, data_status = "unknown", "sufficient"
        observation_reason = "recovery_pending"
    else:  # delay
        health, data_status = "unknown", "sufficient"
        observation_reason = "tcp_p80_observe"

    if health is not None:
        p1_store.apply_probe_health_conn(
            conn, int(node["id"]), health=health, data_status=data_status,
            layer="healthy" if next_state == "healthy" else "observation",
            reason=summary.get("reason") or observation_reason,
            observation_reason=observation_reason,
            last_error=None if next_state == "healthy" else summary.get("reason"),
        )

    store.update_node_state(
        conn, int(node["id"]), state=next_state, failures=failures,
        successes=successes, classification=classification,
        reason=summary.get("reason") or observation_reason or classification,
        next_due_at=next_due, check_id=int(check_row["id"]),
    )
    if prior_state != next_state:
        store.record_event(
            conn, "state_transition", node_id=int(node["id"]),
            reason=summary.get("reason") or observation_reason,
            detail={"from": prior_state, "to": next_state,
                    "failures": failures, "successes": successes,
                    "classification": classification,
                    "observation_reason": observation_reason},
        )
    return {
        "state": next_state, "consecutive_failures": failures,
        "consecutive_successes": successes, "classification": classification,
        "health_pass": summary.get("health_pass"),
        "observation_reason": observation_reason,
        "next_due_at": next_due,
    }


def record_round(check_id: int, node: dict, results: list[dict]) -> dict:
    """Persist stage evidence and advance the node state machine atomically.

    A result produced under an older target generation is recorded as a
    historical Unknown check and never applied to the node.
    """
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM np2_checks WHERE id=?", (int(check_id),)).fetchone()
        if row is None:
            raise store.HealthError("check_not_found", "检测记录不存在")
        check_row = dict(row)
        if check_row["status"] != "running":
            store.finish_queue(conn, int(node.get("queue_id") or 0))
            return {"applied": False, "reason": "check_not_running", "check_id": int(check_id)}

        if int(check_row["config_version"]) != store.config_version(conn):
            conn.execute(
                "UPDATE np2_checks SET status='unknown',reason='config_changed',finished_at=? WHERE id=?",
                (stamp(), int(check_id)),
            )
            store.finish_queue(conn, int(node.get("queue_id") or 0))
            store.record_event(conn, "check_discarded", node_id=int(node["id"]),
                               reason="配置已变更，旧配置结果不参与判定")
            return {"applied": False, "reason": "config_changed", "check_id": int(check_id)}

        summary = _check_summary(results)
        for result in results:
            store.save_target_result(conn, int(check_id), result)
        conn.execute(
            """UPDATE np2_checks SET status=?, reason=?, success_count=?, failure_count=?,
               unknown_count=?, p80_ms=?, health_pass=?, finished_at=? WHERE id=?""",
            (summary["status"], summary["reason"], summary["success_count"],
             summary["failure_count"], summary["unknown_count"], summary["p80_ms"],
             1 if summary["health_pass"] else 0, stamp(), int(check_id)),
        )
        state = _apply_round(conn, check_row=check_row, node=node, results=results,
                             summary=summary)
        store.finish_queue(conn, int(node.get("queue_id") or 0))
        return {"applied": True, "check_id": int(check_id), "summary": summary,
                "state": state}


def _log(message: str) -> None:
    with contextlib.suppress(Exception):
        db.append_log("INFO", "P2健康", message)


async def _target_selftests(targets: list[dict], *, force: bool = False) -> dict[int, dict]:
    results: dict[int, dict] = {}
    for target in targets:
        tid = int(target["id"])
        cached = _selftest_cache.get(tid)
        if not force and cached and time.monotonic() - cached[0] < SELFTEST_TTL:
            results[tid] = cached[1]
            continue
        try:
            result = await target_probe(target, deadline_seconds=20)
        except Exception as exc:
            result = _unknown_target_result(target, f"target_probe_exception:{type(exc).__name__}")
        results[tid] = result
        _selftest_cache[tid] = (time.monotonic(), result)
        status = "success" if result.get("available") else "unknown"
        with db.transaction() as conn:
            store.record_target_check(
                tid, status, scope="selftest",
                duration_ms=result.get("duration_ms"),
                detail={"reason": result.get("reason"), "p80_ms": result.get("p80_ms"),
                        "available": result.get("available"),
                        "stages": result.get("stages")},
                conn=conn,
            )
    return results


async def test_target(target_id: int) -> dict:
    """Manual target-only probe; never changes node health."""
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
    target = store.target(target_id)
    try:
        result = await target_probe(target, deadline_seconds=20)
        error = None
    except Exception as exc:
        result = _unknown_target_result(target, f"target_probe_exception:{type(exc).__name__}")
        error = f"{type(exc).__name__}: {exc}"
    available = bool(result.get("available"))
    with db.transaction() as conn:
        store.record_target_check(
            target_id, "success" if available else "unknown", scope="manual",
            duration_ms=result.get("duration_ms"),
            detail={"reason": result.get("reason"), "error": error,
                    "p80_ms": result.get("p80_ms"), "stages": result.get("stages")},
            conn=conn,
        )
        store.record_event(conn, "target_manual_test", target_id=target_id,
                           reason="success" if available else "unknown",
                           detail={"p80_ms": result.get("p80_ms")})
    return {"target": target, "result": result, "available": available, "error": error}


def enqueue_manual(node_ids: list[int]) -> dict:
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        added = store.enqueue_nodes(conn, node_ids, trigger="manual", priority=0)
    return {"queued": added, "total": len(node_ids),
            "note": "同一节点已在队列中时不会重复入队；运行中的节点会在本轮结束后按新周期重排。"}


def enqueue_all() -> dict:
    with db.transaction() as conn:
        ids = [int(r["id"]) for r in conn.execute(
            "SELECT id FROM np_nodes WHERE source_active=1 ORDER BY id"
        ).fetchall()]
    return enqueue_manual(ids)


async def _engine_prepared(nodes: list[dict], targets: list[dict],
                           selftests: dict[int, dict]) -> dict[int, list[dict]]:
    async def one(node: dict) -> tuple[int, list[dict]]:
        port = ports.get(int(node["id"]))
        if not port:
            return int(node["id"]), [
                _unknown_target_result(t, "engine_unavailable") for t in targets
            ]
        node_results = []
        for target in targets:
            if not selftests.get(int(target["id"]), {}).get("available"):
                node_results.append(_unknown_target_result(target, "probe_target_unavailable"))
                continue
            try:
                node_results.append(await node_probe(
                    target, proxy_port=int(port), deadline_seconds=20
                ))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                node_results.append(_unknown_target_result(
                    target, f"probe_exception:{type(exc).__name__}"
                ))
        return int(node["id"]), node_results

    ports = await prepare_ports(nodes)
    gathered = await asyncio.gather(*(one(node) for node in nodes))
    return {node_id: results for node_id, results in gathered}


async def _run_batch(reserved: list[dict]) -> None:
    global _batch_task
    try:
        with db.transaction() as conn:
            store.ensure_defaults(conn)
            version = store.config_version(conn)
            targets = store.list_targets(enabled_only=True, conn=conn)
        nodes = [_node_dict(row) for row in reserved]
        checks: dict[int, int] = {}
        with db.transaction() as conn:
            for node in nodes:
                row = conn.execute(
                    """INSERT INTO np2_checks(node_id,trigger,status,config_version,
                       target_count,started_at) VALUES (?,?, 'running',?,?,?)""",
                    (int(node["id"]), str(next(r["trigger"] for r in reserved
                                               if int(r["node_id"]) == int(node["id"]))),
                     version, len(targets), stamp()),
                )
                checks[int(node["id"])] = int(row.lastrowid)
        selftests = await _target_selftests(targets)
        try:
            async with engine_mod.operation_lock:
                results = await _engine_prepared(nodes, targets, selftests)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _log(f"检测批次失败：{type(exc).__name__}: {exc}")
            results = {int(n["id"]): [_unknown_target_result(t, f"batch_exception:{type(exc).__name__}")
                                      for t in targets] for n in nodes}
        for node in nodes:
            node["queue_id"] = int(next(r["id"] for r in reserved
                                        if int(r["node_id"]) == int(node["id"])))
            try:
                record_round(checks[int(node["id"])], node,
                             results.get(int(node["id"]), [
                                 _unknown_target_result(t, "probe_result_missing") for t in targets
                             ]))
            except Exception as exc:
                _log(f"节点 #{node['id']} 结果落库失败：{type(exc).__name__}: {exc}")
        _log(f"P2 完成 {len(nodes)} 个节点的基础检测")
    finally:
        _batch_task = None


async def tick() -> dict:
    """Scheduler entry: enqueue due nodes, then start one bounded batch."""
    global _batch_task, _started
    if _batch_task is not None and not _batch_task.done():
        return {"started": False, "reason": "batch_running"}
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        store.sync_states(conn)
        store.enqueue_due(conn, limit=500)
        active = int(conn.execute(
            "SELECT COUNT(*) FROM np2_queue WHERE status='running'"
        ).fetchone()[0])
        limit = max(0, concurrency() - active)
        reserved = store.reserve_queue(conn, limit=limit)
    if not reserved:
        return {"started": False, "reason": "nothing_due", "active": active}
    _batch_task = asyncio.create_task(_run_batch(reserved))
    _batch_task.add_done_callback(lambda _task: None)
    return {"started": True, "nodes": len(reserved), "active": active}


def startup_reset() -> dict:
    """Called once when the app scheduler starts; clears interrupted rows."""
    store.ensure_schema()
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        pending = store.recover_stale_queue(conn)
        running = store.stale_running_checks(conn)
    _log(f"P2 启动恢复：队列 {pending} 条，运行中检查 {running} 条")
    return {"queue_recovered": pending, "running_checks": running}


def reset_runtime_for_tests() -> None:
    global _batch_task, _selftest_cache, _started
    _selftest_cache = {}
    if _batch_task is not None and not _batch_task.done():
        _batch_task.cancel()
    _batch_task = None
    _started = False


async def wait_batch() -> None:
    """Tests and graceful shutdown can await the current batch."""
    if _batch_task is not None:
        with contextlib.suppress(asyncio.CancelledError):
            await _batch_task


def grade(p80: int | None) -> dict:
    return prober.tcp_grade(p80)
