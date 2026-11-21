"""Pull / test orchestration: discrete batches, dispersion, concurrency limits, publishing."""
import asyncio
import ipaddress
import json
import random
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend import config
from backend.database import db
from backend.services import fetcher
from backend.subscriptions import builder
from backend.testers import engine as engine_mod

BLOCK_SIZE = 200


class RuntimeState:
    def __init__(self) -> None:
        self.cycle_lock = asyncio.Lock()
        self.running = False
        self.batch_id: int | None = None
        self.started_at: str | None = None
        self.total = 0
        self.done = 0
        self.phase = "idle"
        self.current_groups: list[str] = []
        self.last_error: str | None = None
        self.fetching: set[int] = set()
        self.last_cycle_at: datetime | None = None
        self.background_task = None
        self.group_progress: list[dict] = []
        self.block_index = 0
        self.blocks = 0
        self.block_size = 0

    def as_dict(self) -> dict:
        return {
            "running": self.running,
            "batch_id": self.batch_id,
            "started_at": self.started_at,
            "total": self.total,
            "done": self.done,
            "progress": round(self.done / self.total * 100, 1) if self.total else 0,
            "phase": self.phase,
            "groups": self.current_groups,
            "last_error": self.last_error,
            "last_cycle_at": self.last_cycle_at.strftime("%Y-%m-%d %H:%M:%S") if self.last_cycle_at else None,
            "engine": engine_mod.engine_manager.name,
            "fetching": sorted(self.fetching),
            "group_progress": self.group_progress,
            "block_index": self.block_index,
            "blocks": self.blocks,
            "block_size": self.block_size,
        }


state = RuntimeState()
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


# ---------------------------------------------------------------------------- dispersion
def dispersion_key(node: dict, mask: int) -> str:
    server = node["server"]
    try:
        ip = ipaddress.ip_address(server)
        return str(ipaddress.ip_network(f"{ip}/{mask}", strict=False))
    except ValueError:
        return f"domain:{server.lower()}"


def disperse(nodes: list[dict], mask: int) -> list[dict]:
    """Shuffle, then interleave similar nodes so equal endpoints are never adjacent."""
    items = list(nodes)
    random.shuffle(items)
    buckets: dict[str, list[dict]] = {}
    for node in items:
        buckets.setdefault(dispersion_key(node, mask), []).append(node)
    keys = list(buckets)
    random.shuffle(keys)
    out: list[dict] = []
    while len(out) < len(items):
        for key in keys:
            if buckets[key]:
                out.append(buckets[key].pop())
    return out


def _semaphore_map_factory():
    return {}


# ---------------------------------------------------------------------------- latency probe
LATENCY_ROUNDS = 2

LATENCY_TARGETS = (
    ("Google", "G", "latency_test_url", "sub_max_latency",
     "http://www.google.com/favicon.ico"),
    ("Wikipedia", "W", "latency_test_url2", "sub_max_latency2",
     "http://www.wikipedia.org/favicon.ico"),
)


def latency_probes() -> list[dict]:
    """Two independent probes; a node publishes only when BOTH meet their own ceiling.

    Each probe runs LATENCY_ROUNDS times and the best round counts for that target.
    """
    timeout = db.get_int("latency_test_timeout", 8)
    status = db.get("latency_test_status") or "200-399"
    probes = []
    for name, ident, url_key, cap_key, fallback_url in LATENCY_TARGETS:
        probes.append({
            "id": 0,
            "name": f"延迟-{name}",
            "ident": ident,
            "url": db.get(url_key) or fallback_url,
            "method": "GET",
            "timeout": timeout,
            "expect_status": status,
            "expect_body": "",
            "max_latency": db.get_int(cap_key, 1000),
        })
    return probes


def _record_latency(node: dict, probe: dict, batch_id: int | None, outcome) -> None:
    db.execute(
        """INSERT INTO latency_results (node_id, batch_id, target, ok, latency_ms, error_type,
           message, ts) VALUES (?,?,?,?,?,?,?,?)""",
        (node["id"], batch_id, probe["ident"], 1 if outcome.ok else 0, outcome.latency_ms,
         outcome.error_type, (outcome.message or "")[:300], db.now()),
    )


def _best_latency(probe: dict, outcomes: list) -> int | None:
    """Best of LATENCY_ROUNDS rounds; an over-limit round does not count as a pass."""
    ok_latencies = [o.latency_ms for o in outcomes
                    if o.ok and o.latency_ms is not None and o.latency_ms <= probe["max_latency"]]
    return min(ok_latencies) if ok_latencies else None


def _apply_latency_gate(node: dict, probes: list[dict], per_probe: dict[str, list]) -> bool:
    """Update node status from the dual-target gate; return whether both targets passed."""
    best = {p["ident"]: _best_latency(p, per_probe.get(p["ident"], [])) for p in probes}
    passed = all(best.get(p["ident"]) is not None for p in probes) if len(probes) > 1 \
        else best.get(probes[0]["ident"]) is not None
    values = [v for v in best.values() if v]
    latency = max(values) if values else (node.get("latency") or None)
    threshold = db.get_int("fail_threshold", 3)
    if passed:
        db.execute(
            """UPDATE nodes SET latency=?, fail_count=0, status='available', last_tested_at=?,
               updated_at=? WHERE id=?""",
            (latency, db.now(), db.now(), node["id"]),
        )
    else:
        row = db.row("SELECT fail_count FROM nodes WHERE id=?", (node["id"],))
        fail_count = (row["fail_count"] + 1) if row else 1
        status = "unavailable" if fail_count >= threshold else "available"
        db.execute(
            """UPDATE nodes SET latency=?, fail_count=?, status=?, last_tested_at=?, updated_at=?
               WHERE id=?""",
            (latency, fail_count, status, db.now(), db.now(), node["id"]),
        )
    return passed


async def _latency_task(nodes: list[dict], probes: list[dict], batch_id: int, global_sem,
                        counters: dict) -> None:
    for node in nodes:
        per_probe: dict[str, list] = {}
        try:
            async def once(probe: dict):
                async with global_sem:
                    await asyncio.sleep(engine_mod.jitter_ms())
                    return probe, await engine_mod.engine_manager.test(node, probe)

            jobs = [once(p) for p in probes for _ in range(LATENCY_ROUNDS)]
            for probe, outcome in await asyncio.gather(*jobs):
                per_probe.setdefault(probe["ident"], []).append(outcome)
                _record_latency(node, probe, batch_id, outcome)
                counters["ok"] += 1 if outcome.ok else 0
            _apply_latency_gate(node, probes, per_probe)
            for probe in probes:
                counters["groups"][probe["name"]] = counters["groups"].get(probe["name"], 0) + 1
                for item in state.group_progress:
                    if item["name"] == probe["name"]:
                        item["done"] = counters["groups"][probe["name"]]
            counters["done"] += len(probes)
            if counters["done"] % 10 == 0 and state.batch_id == batch_id:
                state.done = counters["done"]
                db.execute("UPDATE batches SET processed_count=? WHERE id=?",
                           (counters["done"], batch_id))
        except Exception as exc:  # never let one node abort the probe
            db.append_log("ERROR", "latency", f"{node['raw_name']}: {exc}")


# ---------------------------------------------------------------------------- results
def _record(node: dict, group: dict, batch_id: int | None, outcome, manage_status: bool = True) -> None:
    ok = 1 if outcome.ok else 0
    db.execute(
        """INSERT INTO results (node_id, group_id, batch_id, ok, latency_ms, error_type, message, ts)
           VALUES (?,?,?,?,?,?,?,?)""",
        (node["id"], group["id"], batch_id, ok, outcome.latency_ms, outcome.error_type,
         (outcome.message or "")[:300], db.now()),
    )
    if not manage_status:
        return
    threshold = db.get_int("fail_threshold", 3)
    if outcome.ok:
        db.execute(
            """UPDATE nodes SET latency=?, fail_count=0, status='available', last_tested_at=?, updated_at=?
               WHERE id=?""",
            (outcome.latency_ms, db.now(), db.now(), node["id"]),
        )
    else:
        row = db.row("SELECT fail_count FROM nodes WHERE id=?", (node["id"],))
        fail_count = (row["fail_count"] + 1) if row else 1
        status = "unavailable" if fail_count >= threshold else "available"
        db.execute(
            """UPDATE nodes SET fail_count=?, status=?, last_tested_at=?, updated_at=? WHERE id=?""",
            (fail_count, status, db.now(), db.now(), node["id"]),
        )


async def _test_one(node: dict, group: dict, batch_id: int | None, global_sem, group_sem,
                    ip_sems: dict, subnet_sems: dict, counters: dict,
                    manage_status: bool = True) -> None:
    mask = db.get_int("dispersion_mask", 24)
    if mask not in (8, 16, 24, 32):
        mask = 24
    ip_sem = None
    subnet_sem = None
    try:
        ip = ipaddress.ip_address(node["server"])
        ip_key = str(ip)
        subnet = dispersion_key(node, mask)
        ip_sem = ip_sems.setdefault(ip_key, asyncio.Semaphore(max(1, db.get_int("ip_concurrency", 1))))
        subnet_sem = subnet_sems.setdefault(
            subnet, asyncio.Semaphore(max(1, db.get_int("subnet_concurrency", 2))))
    except ValueError:
        pass

    async with group_sem:
        async with global_sem:
            if ip_sem is not None:
                await ip_sem.acquire()
            if subnet_sem is not None:
                await subnet_sem.acquire()
            try:
                await asyncio.sleep(engine_mod.jitter_ms())
                outcome = await engine_mod.engine_manager.test(node, group)
                _record(node, group, batch_id, outcome, manage_status=manage_status)
                counters["done"] += 1
                counters["ok"] += 1 if outcome.ok else 0
                counters["groups"][group["name"]] = counters["groups"].get(group["name"], 0) + 1
                for item in state.group_progress:
                    if item["name"] == group["name"]:
                        item["done"] = counters["groups"][group["name"]]
                if counters["done"] % 10 == 0 and state.batch_id == batch_id:
                    state.done = counters["done"]
                    db.execute("UPDATE batches SET processed_count=? WHERE id=?",
                               (counters["done"], batch_id))
            finally:
                if subnet_sem is not None:
                    subnet_sem.release()
                if ip_sem is not None:
                    ip_sem.release()


async def _group_task(group: dict, nodes: list[dict], batch_id: int, global_sem,
                      ip_sems: dict, subnet_sems: dict, counters: dict,
                      manage_status: bool = True) -> dict:
    group_sem = asyncio.Semaphore(max(1, db.get_int("group_concurrency", 5)))
    failures = 0
    for node in nodes:
        try:
            await _test_one(node, group, batch_id, global_sem, group_sem, ip_sems, subnet_sems,
                            counters, manage_status=manage_status)
        except Exception as exc:  # one node must never abort the group
            failures += 1
            db.append_log("ERROR", "test", f"{group['name']} / {node['raw_name']}: {exc}")
    db.execute("UPDATE test_groups SET last_run_at=? WHERE id=?", (db.now(), group["id"]))
    db.append_log("INFO", "test", f"group {group['name']} finished block ({len(nodes)} nodes)")
    return {"group": group["name"], "nodes": len(nodes), "failures": failures}


# ---------------------------------------------------------------------------- cycles
def due_groups(force: bool = False, ids: list[int] | None = None) -> list[dict]:
    """All enabled groups run together in every cycle.

    The per-group cadence field is no longer used: the batch cadence is global
    (`test_interval_minutes` / publish schedule) and the timeout is global too.
    """
    groups = db.rows("SELECT * FROM test_groups WHERE enabled=1 ORDER BY sort_order, id")
    if ids:
        groups = [g for g in groups if g["id"] in ids]
    timeout = db.get_int("latency_test_timeout", 8)
    for group in groups:
        group["timeout"] = timeout
    return groups


async def run_cycle(force: bool = False, group_ids: list[int] | None = None,
                    reason: str = "schedule") -> dict:
    if state.running:
        return {"ok": False, "error": "a test cycle is already running", "batch_id": state.batch_id}
    groups = due_groups(force=force, ids=group_ids)
    probes = latency_probes()
    if not groups and not probes:
        return {"ok": False, "error": "no enabled test group is due"}
    nodes = db.rows(
        """SELECT n.id, n.airport_id, n.raw_name, n.protocol, n.server, n.port, n.params,
                  n.fingerprint
           FROM nodes n JOIN airports a ON a.id=n.airport_id
           WHERE a.enabled=1 ORDER BY n.id"""
    )
    if not nodes:
        return {"ok": False, "error": "node pool is empty; add an airport and fetch it first"}

    async with state.cycle_lock:
        state.running = True
        state.phase = "running"
        state.started_at = db.now()
        state.last_error = None
        units = len(groups) + len(probes)
        state.total = len(nodes) * units
        state.done = 0
        state.current_groups = [p["name"] for p in probes] + [g["name"] for g in groups]
        # A pull in flight rewrites the node pool; let it finish before we snapshot candidates
        waited = 0
        while state.fetching and waited < 120:
            await asyncio.sleep(1)
            waited += 1
        if state.fetching:
            db.append_log("WARN", "test", "batch started while a fetch is still running")
        state.last_cycle_at = datetime.now()
        batch_id = db.execute(
            "INSERT INTO batches (started_at, status, target_count, groups, note) VALUES (?,?,?,?,?)",
            (state.started_at, "running", state.total,
             db.dumps([{"id": 0, "name": p["name"], "ident": p["ident"]} for p in probes]
                      + [{"id": g["id"], "name": g["name"], "ident": g["ident"]} for g in groups]),
             reason),
        )
        state.batch_id = batch_id
        db.append_log("INFO", "test",
                      f"batch #{batch_id} started: {len(nodes)} nodes x {units} units"
                      f" ({len(groups)} groups + {len(probes)} latency targets)")
        counters = {"done": 0, "ok": 0, "groups": {}}
        state.group_progress = [
            {"name": item["name"], "ident": item["ident"], "done": 0, "total": len(nodes)}
            for item in [{"name": p["name"], "ident": p["ident"]} for p in probes]
            + [{"name": g["name"], "ident": g["ident"]} for g in groups]
        ]
        state.blocks = (len(nodes) + BLOCK_SIZE - 1) // BLOCK_SIZE
        state.block_index = 0
        state.block_size = 0
        global_sem = asyncio.Semaphore(max(1, db.get_int("global_concurrency", 10)))
        mask = db.get_int("dispersion_mask", 24)
        ip_sems: dict = {}
        subnet_sems: dict = {}
        try:
            for offset in range(0, len(nodes), BLOCK_SIZE):
                block = nodes[offset:offset + BLOCK_SIZE]
                block = disperse(block, mask)
                state.block_index = offset // BLOCK_SIZE + 1
                state.block_size = len(block)
                state.phase = f"binding block {state.block_index}/{state.blocks}"
                await engine_mod.engine_manager.prepare(block)
                state.phase = "testing"
                tasks = []
                if probes:
                    tasks.append(_latency_task(block, probes, batch_id, global_sem, counters))
                tasks.extend(
                    _group_task(g, block, batch_id, global_sem, ip_sems, subnet_sems, counters,
                                manage_status=not probes)
                    for g in groups
                )
                await asyncio.gather(*tasks)
            state.done = counters["done"]
            db.execute(
                "UPDATE batches SET finished_at=?, status='complete', processed_count=? WHERE id=?",
                (db.now(), counters["done"], batch_id),
            )
            state.phase = "publishing"
            try:
                meta = builder.publish(reason=f"batch#{batch_id}", batch_id=batch_id)
                db.execute("UPDATE batches SET published=1 WHERE id=?", (batch_id,))
                state.last_error = None
            except Exception as exc:
                state.last_error = f"publish failed: {exc}"
                db.append_log("ERROR", "publish", state.last_error)
                db.execute("UPDATE batches SET note=? WHERE id=?",
                           (state.last_error, batch_id))
            db.append_log("INFO", "test",
                          f"batch #{batch_id} complete: {counters['done']}/{state.total} processed")
            return {"ok": True, "batch_id": batch_id, "processed": counters["done"],
                    "total": state.total, "published": state.last_error is None}
        except Exception as exc:
            state.last_error = f"{type(exc).__name__}: {exc}"
            db.execute("UPDATE batches SET finished_at=?, status='failed', note=? WHERE id=?",
                       (db.now(), state.last_error[:500], batch_id))
            db.append_log("ERROR", "test", f"batch #{batch_id} failed: {state.last_error}")
            return {"ok": False, "batch_id": batch_id, "error": state.last_error}
        finally:
            state.running = False
            state.phase = "idle"
            state.batch_id = None
            state.current_groups = []


async def test_single_node(node_id: int, group_ids: list[int] | None = None) -> dict:
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    if not node:
        return {"ok": False, "error": "node not found"}
    node["params"] = db.loads(node.get("params"), {})
    groups = db.rows("SELECT * FROM test_groups WHERE enabled=1 ORDER BY sort_order, id")
    if group_ids:
        groups = [g for g in groups if g["id"] in group_ids]
    if not groups:
        return {"ok": False, "error": "no enabled test group"}
    await engine_mod.engine_manager.prepare([node])
    probes = latency_probes()
    latency = None
    if probes:
        per_probe = {}
        targets = {}
        for probe in probes:
            outs = []
            for _ in range(LATENCY_ROUNDS):
                outcome = await engine_mod.engine_manager.test(node, probe)
                _record_latency(node, probe, None, outcome)
                outs.append(outcome)
                await asyncio.sleep(engine_mod.jitter_ms())
            per_probe[probe["ident"]] = outs
            targets[probe["ident"]] = {
                "best": _best_latency(probe, outs),
                "cap": probe["max_latency"],
                "url": probe["url"],
            }
        passed = _apply_latency_gate(node, probes, per_probe)
        latency = {"ok": passed, "targets": targets,
                   "latency_ms": max([t["best"] for t in targets.values() if t["best"]] or [0]) or None}
    results = []
    for group in groups:
        outcome = await engine_mod.engine_manager.test(node, group)
        _record(node, group, None, outcome)
        results.append({"group": group["name"], "ok": outcome.ok, "latency_ms": outcome.latency_ms,
                        "error_type": outcome.error_type, "message": outcome.message})
        await asyncio.sleep(engine_mod.jitter_ms())
    return {"ok": True, "node_id": node_id, "latency": latency, "results": results}


async def fetch_now(airport_id: int, trigger: str = "manual") -> dict:
    if state.running:
        return {"ok": False, "error": "a test cycle is running; retry when it finishes"}
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        return {"ok": False, "error": "airport not found"}
    if not airport["enabled"]:
        return {"ok": False, "error": "airport disabled: enable it first, its cached nodes return without refetching"}
    state.fetching.add(airport_id)
    try:
        return await fetcher.fetch_airport(airport, trigger=trigger)
    finally:
        state.fetching.discard(airport_id)


# ---------------------------------------------------------------------------- scheduler jobs
async def job_fetch() -> None:
    if state.fetching or state.running:
        return
    due = fetcher.due_airports()
    if not due:
        return
    sem = asyncio.Semaphore(3)

    async def one(airport):
        async with sem:
            state.fetching.add(airport["id"])
            try:
                await fetcher.fetch_airport(airport, trigger="schedule")
            finally:
                state.fetching.discard(airport["id"])

    await asyncio.gather(*[one(a) for a in due])


async def job_cycle() -> None:
    if state.running:
        return
    interval = max(1, db.get_int("test_interval_minutes", 30))
    if state.last_cycle_at and (datetime.now() - state.last_cycle_at) < timedelta(minutes=interval):
        return
    try:
        await run_cycle(reason="schedule")
    except Exception as exc:
        db.append_log("ERROR", "test", f"cycle crashed: {exc}")


async def job_publish() -> None:
    """Scheduled publish: either a fixed daily time or a repeating interval.

    Both modes force a full latency + label cycle before writing the subscription.
    """
    if db.get("sub_publish_enabled", "0") != "1":
        return
    mode = (db.get("sub_publish_mode", "time") or "time").strip().lower()
    now = datetime.now()
    ts_fmt = "%Y-%m-%d %H:%M:%S"

    if mode == "interval":
        interval = max(5, db.get_int("sub_publish_interval_minutes", 180))
        next_at = _parse_ts(db.get("sub_publish_next_at"))
        if next_at is None:
            nxt = next_publish_at("interval", interval=interval, base=now)
            db.set_setting("sub_publish_next_at", nxt.strftime(ts_fmt))
            db.append_log("INFO", "publish", f"interval publish armed: every {interval} min")
            return
        if now < next_at:
            return
        label = f"every {interval} min"
    else:
        hhmm = db.get("sub_publish_time", "04:30") or "04:30"
        try:
            hour, minute = (int(x) for x in hhmm.split(":"))
        except ValueError:
            return
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        offset = (now - target).total_seconds()
        if offset < 0 or offset > 900:   # run within 15 minutes after the configured time
            return
        if db.get("sub_last_auto_publish") == now.strftime("%Y-%m-%d"):
            return
        label = hhmm

    if state.running:
        db.append_log("WARN", "publish", "scheduled publish skipped: a cycle is already running")
        return
    db.append_log("INFO", "publish", f"scheduled publish started ({label})")
    result = await run_cycle(force=True, reason="scheduled publish")

    def settle(retry_minutes: int | None = None) -> None:
        now2 = datetime.now()
        if retry_minutes:
            nxt = now2 + timedelta(minutes=retry_minutes)
        else:
            nxt = next_publish_at("interval", interval=interval, base=now2) if mode == "interval" \
                else next_publish_at("time", hhmm=hhmm, base=now2)
        if nxt:
            db.set_setting("sub_publish_next_at", nxt.strftime(ts_fmt))
        db.set_setting("sub_last_auto_publish", now2.strftime("%Y-%m-%d"))
        db.set_setting("sub_last_auto_publish_at", now2.strftime(ts_fmt))

    if result.get("ok") and result.get("published"):
        settle(None)
        db.append_log("INFO", "publish", "scheduled publish done")
        return
    if not result.get("ok"):
        error = result.get("error", "")
        if "empty" in error or "no enabled" in error:
            settle(None)
            db.append_log("WARN", "publish", f"scheduled publish skipped: {error}")
        else:
            # retry in one minute instead of burning the whole interval
            settle(1)
            db.append_log("ERROR", "publish", f"scheduled publish failed, retrying next minute: {error}")
        return
    settle(None)
    db.append_log("WARN", "publish", "scheduled cycle finished but the subscription was not published")


def next_publish_at(mode: str | None = None, hhmm: str | None = None,
                    interval: int | None = None, base: datetime | None = None) -> datetime | None:
    """Next scheduled publish time, used both for arming jobs and for the dashboard."""
    base = base or datetime.now()
    mode = (mode or db.get("sub_publish_mode", "time") or "time").strip().lower()
    if mode == "interval":
        minutes = max(5, interval if interval is not None else db.get_int("sub_publish_interval_minutes", 180))
        return base + timedelta(minutes=minutes)
    value = hhmm or db.get("sub_publish_time", "04:30") or "04:30"
    try:
        hour, minute = (int(x) for x in value.split(":"))
    except ValueError:
        return None
    slot = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if slot <= base:
        slot += timedelta(days=1)
    return slot


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(str(value)[:19], fmt)
        except ValueError:
            continue
    return None


async def job_maintenance() -> None:
    days = db.get_int("history_days", 30)
    cutoff = (datetime.now() - timedelta(days=max(7, min(days, 365)))).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM results WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM fetch_logs WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM batches WHERE started_at < ?", (cutoff,))
    db.append_log("INFO", "maintenance", f"history older than {cutoff} purged")


def start_scheduler() -> None:
    if scheduler.running:
        return
    scheduler.add_job(job_fetch, "interval", minutes=1, id="fetch", max_instances=1,
                      coalesce=True, next_run_time=datetime.now() + timedelta(seconds=10))
    scheduler.add_job(job_cycle, "interval", minutes=1, id="cycle", max_instances=1,
                      coalesce=True, next_run_time=datetime.now() + timedelta(seconds=40))
    scheduler.add_job(job_publish, "interval", minutes=1, id="publish", max_instances=1,
                      coalesce=True, next_run_time=datetime.now() + timedelta(seconds=50))
    scheduler.add_job(job_maintenance, "interval", hours=6, id="maintenance", max_instances=1)
    scheduler.start()
    db.append_log("INFO", "scheduler", "scheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


def reschedule_group(group_id: int | None = None) -> None:  # kept for API symmetry
    return None
