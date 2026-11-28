"""Pull / test orchestration: discrete batches, dispersion, concurrency limits, publishing."""
import asyncio
import ipaddress
import os
import random
import sys
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from backend.database import db
from backend.database import gate as maint_gate
from backend.health import service as health_service
from backend.business import service as business_service
from backend.quality import service as quality_service
from backend.services import fetcher
from backend.services.runtime_gate import gate
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
        self.auto_cycle_anchor: datetime | None = None
        self.group_progress: list[dict] = []
        self.block_index = 0
        self.blocks = 0
        self.block_size = 0
        self.paused = False
        self.stop_requested = False
        self.pause_event: asyncio.Event | None = None
        self.deferred = False
        self.deferred_reason: str | None = None
        self.defer_lock = asyncio.Lock()
        # 延迟检测 / 标签检测分开统计，供前端两条环形进度使用
        self.latency_done = 0
        self.latency_total = 0
        self.tag_done = 0
        self.tag_total = 0
        # 当前正在测的节点（机场、节点名、当前检测项、延迟/结果），随请求更新
        self.current: dict | None = None
        self.airport_names: dict = {}
        # v1.10 §2.7 A 类入口 2：调度器只看这一轮判定结果，不自行重试迁移
        self.maintenance_blocked = False
        self.maintenance_reason: str | None = None
        self.maintenance_logged = False

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
            "paused": self.paused,
            "stop_requested": self.stop_requested,
            "deferred": self.deferred,
            "deferred_reason": self.deferred_reason,
            "work": gate.snapshot(),
            "latency_done": self.latency_done,
            "latency_total": self.latency_total,
            "latency_pct": round(self.latency_done / self.latency_total * 100, 1) if self.latency_total else 0,
            "tag_done": self.tag_done,
            "tag_total": self.tag_total,
            "tag_pct": round(self.tag_done / self.tag_total * 100, 1) if self.tag_total else 0,
            "current": self.current,
            "maintenance_blocked": self.maintenance_blocked,
            "maintenance_reason": self.maintenance_reason,
        }

    def request_pause(self) -> dict:
        if not self.running or self.stop_requested:
            return {"ok": False, "error": "当前没有可暂停的检测批次"}
        if self.paused:
            return {"ok": True, "already": True, **self.as_dict()}
        if self.pause_event is None:
            self.pause_event = asyncio.Event()
        self.pause_event.clear()
        self.paused = True
        self.phase = "已暂停"
        db.append_log("INFO", "检测", f"批次 #{self.batch_id} 已暂停")
        db.record_run_event("pause", self.batch_id)
        return {"ok": True, **self.as_dict()}

    def request_resume(self) -> dict:
        if not self.running or not self.paused:
            return {"ok": False, "error": "当前没有已暂停的检测批次"}
        if self.pause_event is None:
            self.pause_event = asyncio.Event()
        self.pause_event.set()
        self.paused = False
        self.phase = "检测中"
        db.append_log("INFO", "检测", f"批次 #{self.batch_id} 已继续")
        db.record_run_event("resume", self.batch_id)
        return {"ok": True, **self.as_dict()}

    def request_stop(self) -> dict:
        if not self.running:
            return {"ok": False, "error": "当前没有正在运行的检测批次"}
        self.stop_requested = True
        self.paused = False
        self.phase = "正在停止"
        if self.pause_event is None:
            self.pause_event = asyncio.Event()
        self.pause_event.set()
        db.append_log("INFO", "检测", f"批次 #{self.batch_id} 正在停止，将按当前进度下发")
        db.record_run_event("stop", self.batch_id)
        return {"ok": True, **self.as_dict()}


state = RuntimeState()
scheduler = AsyncIOScheduler(timezone="Asia/Shanghai")


def maintenance_hold() -> str | None:
    """调度轮的 A 类维护态判定：每轮一次，非 INACTIVE 即停轮。

    * 只调用 ``gate.judge()``（唯一判定实现），不读维护文件、不自建判定；
    * 停止时不重试迁移、不推进任何迁移状态，只把运行态标记为 stopped；
    * 判定结果同步交给 B 类（写入闸 / 连接池），使调度轮内的写入一律落空。
    """
    mstate, reason = maint_gate.judge()
    if reason is not None:
        maint_gate.apply_state(mstate)
        state.maintenance_blocked = True
        state.maintenance_reason = reason
        if not state.running:
            state.phase = "stopped"
        if not state.maintenance_logged:
            state.maintenance_logged = True
            try:
                db.append_log("WARN", "调度",
                              f"维护态（{reason}）：后台任务已停止，等待人工恢复")
            except Exception:
                pass
        return reason
    state.maintenance_blocked = False
    state.maintenance_reason = None
    state.maintenance_logged = False
    return None


class CycleStopped(Exception):
    """User stopped the current batch; it is not a failure."""


async def _control_point() -> None:
    """Pause the caller at a safe boundary, or abort it when a stop was requested."""
    if state.pause_event is not None and not state.pause_event.is_set():
        state.paused = True
        if state.phase != "正在停止":
            state.phase = "已暂停"
        await state.pause_event.wait()
        state.paused = False
        if state.running and not state.stop_requested and state.phase == "已暂停":
            state.phase = "检测中"
    if state.stop_requested:
        raise CycleStopped


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


# ---------------------------------------------------------------------------- latency probe
LATENCY_ROUNDS = 3

LATENCY_TARGETS = (
    ("G", "latency_test_url", "sub_max_latency", "latency_test_name",
     "Google", "http://www.google.com/favicon.ico"),
    ("W", "latency_test_url2", "sub_max_latency2", "latency_test_name2",
     "Wikipedia", "http://www.wikipedia.org/favicon.ico"),
)


def latency_probes() -> list[dict]:
    """Two independent probes; a node publishes only when BOTH meet their own ceiling.

    Each probe runs LATENCY_ROUNDS times and the best round counts for that target.
    """
    timeout = db.get_int("latency_test_timeout", 8)
    status = db.get("latency_test_status") or "200-399"
    probes = []
    for ident, url_key, cap_key, name_key, fallback_name, fallback_url in LATENCY_TARGETS:
        name = (db.get(name_key) or fallback_name).strip() or fallback_name
        probes.append({
            "id": 0,
            "name": f"延迟-{name}",
            "label": name,
            "short": name[0],
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
                        node_locks: dict, counters: dict) -> None:
    """Probe every node in parallel, but keep each node's own rounds strictly serial.

    The global semaphore caps total in-flight requests. Within one node the order is
    always G, W, G, W, G, W and never two targets at the same time, so a weak node cannot be
    flooded. Running only one node at a time here made a 100+ node batch look frozen.
    """

    async def one(node: dict) -> None:
        per_probe: dict[str, list] = {}
        try:
            state.current = {"airport": state.airport_names.get(node.get("airport_id")),
                             "node": node["raw_name"], "stage": "延迟检测", "result": "检测中",
                             "protocol": node.get("protocol")}
            await _control_point()
            # Strictly alternating G, W, G, W, G, W; one request in flight per node.
            for _round in range(LATENCY_ROUNDS):
                for probe in probes:
                    # Node lock first, then the global slot: see _test_one for why.
                    async with node_locks[node["id"]]:
                        async with global_sem:
                            await _control_point()
                            await asyncio.sleep(engine_mod.jitter_ms())
                            outcome = await engine_mod.engine_manager.test(node, probe)
                    per_probe.setdefault(probe["ident"], []).append(outcome)
                    _record_latency(node, probe, batch_id, outcome)
                    counters["ok"] += 1 if outcome.ok else 0
            passed = _apply_latency_gate(node, probes, per_probe)
            best: dict[str, int | None] = {}
            for ident, outcomes in per_probe.items():
                values = [o.latency_ms for o in outcomes if o.ok and o.latency_ms]
                best[ident] = min(values) if values else None
            state.latency_done += 1
            state.current = {"airport": state.airport_names.get(node.get("airport_id")),
                             "node": node["raw_name"], "stage": "延迟检测",
                             "result": "达标" if passed else "未达标",
                             "g": best.get("G"), "w": best.get("W"),
                             "protocol": node.get("protocol")}
            for probe in probes:
                counters["groups"][probe["name"]] = counters["groups"].get(probe["name"], 0) + 1
                for item in state.group_progress:
                    if item["name"] == probe["name"]:
                        item["done"] = counters["groups"][probe["name"]]
            counters["done"] += len(probes)
            state.done = counters["done"]
            if counters["done"] % 10 == 0 and state.batch_id == batch_id:
                db.execute("UPDATE batches SET processed_count=? WHERE id=?",
                           (counters["done"], batch_id))
        except CycleStopped:
            return
        except Exception as exc:  # never let one node abort the probe
            db.append_log("ERROR", "延迟", f"{node['raw_name']}：延迟检测异常：{exc}")

    await asyncio.gather(*(one(node) for node in nodes))


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
                    node_lock: asyncio.Lock, ip_sems: dict, subnet_sems: dict, counters: dict,
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
        # Same order everywhere: node lock first, then the global concurrency slot.
        async with node_lock:
            async with global_sem:
                if ip_sem is not None:
                    await ip_sem.acquire()
                if subnet_sem is not None:
                    await subnet_sem.acquire()
                try:
                    state.current = {"airport": state.airport_names.get(node.get("airport_id")),
                                     "node": node["raw_name"], "stage": f"标签 {group['name']}",
                                     "result": "检测中", "protocol": node.get("protocol")}
                    await _control_point()
                    await asyncio.sleep(engine_mod.jitter_ms())
                    outcome = await engine_mod.engine_manager.test(node, group)
                    _record(node, group, batch_id, outcome, manage_status=manage_status)
                    state.current = {"airport": state.airport_names.get(node.get("airport_id")),
                                     "node": node["raw_name"], "stage": f"标签 {group['name']}",
                                     "result": "通过" if outcome.ok else "未通过",
                                     "latency": outcome.latency_ms, "protocol": node.get("protocol")}
                    state.tag_done += 1
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
                      node_locks: dict, ip_sems: dict, subnet_sems: dict, counters: dict,
                      manage_status: bool = True) -> dict:
    group_sem = asyncio.Semaphore(max(1, db.get_int("group_concurrency", 5)))
    failures = 0

    async def one(node: dict) -> None:
        nonlocal failures
        try:
            if state.stop_requested:
                return
            await _control_point()
            await _test_one(node, group, batch_id, global_sem, group_sem,
                            node_locks[node["id"]], ip_sems, subnet_sems,
                            counters, manage_status=manage_status)
        except CycleStopped:
            return
        except Exception as exc:  # one node must never abort the group
            failures += 1
            db.append_log("ERROR", "检测", f"{group['name']} / {node['raw_name']}：检测异常：{exc}")

    await asyncio.gather(*(one(node) for node in nodes))
    db.execute("UPDATE test_groups SET last_run_at=? WHERE id=?", (db.now(), group["id"]))
    db.append_log("INFO", "检测", f"节点标签 {group['name']} 完成本批（{len(nodes)} 个节点）")
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
    """Run a detection cycle only when the download-speed queue is idle.

    If speed testing owns the engine, the cycle stays pending instead of failing,
    and starts as soon as the current speed batch releases the gate.
    """
    if state.running:
        return {"ok": False, "error": "已有检测批次正在运行", "batch_id": state.batch_id}
    if not await gate.try_acquire("detect", "节点检测评分"):
        async with state.defer_lock:
            if state.deferred:
                return {"ok": False, "deferred": True,
                        "error": "节点检测已在等待下载测速完成"}
            state.deferred = True
            state.deferred_reason = f"{gate.label}进行中，检测已延迟"
            state.phase = "等待下载测速完成"
            db.append_log("INFO", "检测", f"检测批次延迟：{gate.label}进行中")
        try:
            await gate.acquire_wait("detect", "节点检测评分")
        finally:
            state.deferred = False
            state.deferred_reason = None
    try:
        return await _run_cycle(force=force, group_ids=group_ids, reason=reason)
    finally:
        await gate.release("detect")


async def _run_cycle(force: bool = False, group_ids: list[int] | None = None,
                     reason: str = "schedule") -> dict:
    if state.running:
        return {"ok": False, "error": "已有检测批次正在运行", "batch_id": state.batch_id}
    groups = due_groups(force=force, ids=group_ids)
    probes = latency_probes()
    if not groups and not probes:
        return {"ok": False, "error": "没有到期的启用节点标签"}
    nodes = db.rows(
        """SELECT n.id, n.airport_id, n.raw_name, n.protocol, n.server, n.port, n.params,
                  n.fingerprint
           FROM nodes n JOIN airports a ON a.id=n.airport_id
           WHERE a.enabled=1 ORDER BY n.id"""
    )
    if not nodes:
        return {"ok": False, "error": "缓存池为空，请先添加机场并拉取订阅"}

    async with state.cycle_lock, engine_mod.operation_lock:
        state.running = True
        state.paused = False
        state.stop_requested = False
        state.pause_event = asyncio.Event()
        state.pause_event.set()
        state.phase = "检测中"
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
            db.append_log("WARN", "检测", "开始批次时订阅拉取仍未结束，继续使用当前缓存节点")
        state.last_cycle_at = datetime.now()
        batch_id = db.execute(
            "INSERT INTO batches (started_at, status, target_count, groups, note) VALUES (?,?,?,?,?)",
            (state.started_at, "running", state.total,
             db.dumps([{"id": 0, "name": p["name"], "ident": p["ident"]} for p in probes]
                      + [{"id": g["id"], "name": g["name"], "ident": g["ident"]} for g in groups]),
             reason),
        )
        state.batch_id = batch_id
        db.append_log("INFO", "检测",
                      f"批次 #{batch_id} 开始：{len(nodes)} 个节点 × {units} 项"
                      f"（{len(groups)} 个节点标签 + {len(probes)} 个延迟目标）")
        counters = {"done": 0, "ok": 0, "groups": {}}
        state.group_progress = [
            {"name": item["name"], "ident": item["ident"], "done": 0, "total": len(nodes)}
            for item in [{"name": p["name"], "ident": p["ident"]} for p in probes]
            + [{"name": g["name"], "ident": g["ident"]} for g in groups]
        ]
        state.blocks = (len(nodes) + BLOCK_SIZE - 1) // BLOCK_SIZE
        state.latency_total = len(nodes) if probes else 0
        state.latency_done = 0
        state.tag_total = len(nodes) * len(groups)
        state.tag_done = 0
        state.current = None
        state.airport_names = {row["id"]: row["name"] for row in db.rows("SELECT id, name FROM airports")}
        state.block_index = 0
        state.block_size = 0
        global_sem = asyncio.Semaphore(max(1, db.get_int("global_concurrency", 10)))
        node_locks = {n["id"]: asyncio.Lock() for n in nodes}
        mask = db.get_int("dispersion_mask", 24)
        ip_sems: dict = {}
        subnet_sems: dict = {}
        try:
            for offset in range(0, len(nodes), BLOCK_SIZE):
                try:
                    await _control_point()
                except CycleStopped:
                    break
                block = nodes[offset:offset + BLOCK_SIZE]
                block = disperse(block, mask)
                state.block_index = offset // BLOCK_SIZE + 1
                state.block_size = len(block)
                state.phase = f"绑定第 {state.block_index}/{state.blocks} 块"
                await engine_mod.engine_manager.prepare(block)
                state.phase = "检测中"
                # 先跑完延迟检测再跑标签检测：两条进度环各自推进，不会互相抢并发导致一环卡在 0%
                if probes:
                    await _latency_task(block, probes, batch_id, global_sem, node_locks, counters)
                tasks = []
                tasks.extend(
                    _group_task(g, block, batch_id, global_sem, node_locks, ip_sems,
                                subnet_sems, counters, manage_status=not probes)
                    for g in groups
                )
                await asyncio.gather(*tasks)
            stopped = state.stop_requested
            state.done = counters["done"]
            db.execute(
                "UPDATE batches SET finished_at=?, status=?, processed_count=? WHERE id=?",
                (db.now(), "stopped" if stopped else "complete", counters["done"], batch_id),
            )
            state.phase = "下发中"
            try:
                meta = builder.publish(reason=f"batch#{batch_id}", batch_id=batch_id)
                db.execute("UPDATE batches SET published=1 WHERE id=?", (batch_id,))
                state.last_error = None
            except Exception as exc:
                state.last_error = "订阅下发失败，请查看系统日志"
                db.append_log("ERROR", "下发", f"{state.last_error}（{type(exc).__name__}：{exc}）")
                db.execute("UPDATE batches SET note=? WHERE id=?",
                           (state.last_error, batch_id))
            if stopped:
                db.append_log("INFO", "检测",
                              f"批次 #{batch_id} 已停止：按当前进度下发，"
                              f"已处理 {counters['done']}/{state.total}")
            else:
                db.append_log("INFO", "检测",
                              f"批次 #{batch_id} 完成：已处理 {counters['done']}/{state.total}")
            return {"ok": True, "batch_id": batch_id, "processed": counters["done"],
                    "total": state.total, "published": state.last_error is None,
                    "stopped": stopped, "reason": reason}
        except Exception as exc:
            state.last_error = "检测批次失败，请查看系统日志"
            db.record_run_event("crash", batch_id, str(exc)[:200])
            db.execute("UPDATE batches SET finished_at=?, status='failed', note=? WHERE id=?",
                       (db.now(), state.last_error[:500], batch_id))
            db.append_log("ERROR", "检测", f"批次 #{batch_id} 失败：{state.last_error}（{type(exc).__name__}：{exc}）")
            return {"ok": False, "batch_id": batch_id, "error": state.last_error}
        finally:
            state.running = False
            state.paused = False
            state.stop_requested = False
            if state.pause_event is not None:
                state.pause_event.set()
            state.phase = "空闲"
            state.batch_id = None
            state.current_groups = []


async def test_single_node(node_id: int, group_ids: list[int] | None = None,
                           mode: str = "all") -> dict:
    """mode: all | latency | tags. Lets the detail dialog test either half alone."""
    if state.running:
        return {"ok": False, "error": "已有检测批次在运行，请完成后重试"}
    if not await gate.try_acquire("detect", "节点检测评分"):
        return {"ok": False, "deferred": True,
                "error": f"{gate.label}进行中，节点检测已延迟"}
    try:
        return await _test_single_node(node_id, group_ids=group_ids, mode=mode)
    finally:
        await gate.release("detect")


async def _test_single_node(node_id: int, group_ids: list[int] | None = None,
                            mode: str = "all") -> dict:
    if state.running:
        return {"ok": False, "error": "已有检测批次在运行，请完成后重试"}
    node = db.row(
        """SELECT n.*, a.enabled AS airport_enabled FROM nodes n
           JOIN airports a ON a.id=n.airport_id WHERE n.id=?""", (node_id,))
    if not node:
        return {"ok": False, "error": "节点不存在"}
    if not node["airport_enabled"]:
        return {"ok": False, "error": "节点所属机场已停用：请先启用该机场后再检测"}
    node["params"] = db.loads(node.get("params"), {})
    groups = []
    if mode != "latency":
        groups = db.rows("SELECT * FROM test_groups WHERE enabled=1 ORDER BY sort_order, id")
        if group_ids:
            groups = [g for g in groups if g["id"] in group_ids]
        if not groups:
            return {"ok": False, "error": "没有启用的节点标签"}
    async with engine_mod.operation_lock:
        await engine_mod.engine_manager.prepare([node])
        probes = latency_probes() if mode != "tags" else []
        latency = None
        if probes:
            per_probe = {}
            targets = {}
            for _round in range(LATENCY_ROUNDS):
                for probe in probes:
                    outcome = await engine_mod.engine_manager.test(node, probe)
                    _record_latency(node, probe, None, outcome)
                    per_probe.setdefault(probe["ident"], []).append(outcome)
                    await asyncio.sleep(engine_mod.jitter_ms())
            for probe in probes:
                targets[probe["ident"]] = {
                    "best": _best_latency(probe, per_probe.get(probe["ident"], [])),
                    "cap": probe["max_latency"],
                    "url": probe["url"],
                }
            passed = _apply_latency_gate(node, probes, per_probe)
            best_values = [t["best"] for t in targets.values() if t["best"] is not None]
            latency = {"ok": passed, "targets": targets,
                       "latency_ms": max(best_values) if best_values else None}
        results = []
        for group in groups:
            outcome = await engine_mod.engine_manager.test(node, group)
            _record(node, group, None, outcome)
            results.append({"group": group["name"], "ok": outcome.ok,
                            "latency_ms": outcome.latency_ms,
                            "error_type": outcome.error_type, "message": outcome.message})
            await asyncio.sleep(engine_mod.jitter_ms())
        return {"ok": True, "node_id": node_id, "mode": mode,
                "latency": latency, "results": results}


async def fetch_now(airport_id: int, trigger: str = "manual") -> dict:
    if state.running or gate.owner:
        return {"ok": False, "error": "检测或下载测速正在运行，请完成后重试"}
    airport = fetcher.airport_by_id(airport_id)
    if not airport:
        return {"ok": False, "error": "机场不存在"}
    if not airport["enabled"]:
        return {"ok": False, "error": "机场已停用：请先启用该机场；启用后会直接恢复缓存节点，不会重新拉取"}
    state.fetching.add(airport_id)
    try:
        return await fetcher.fetch_airport(airport, trigger=trigger)
    finally:
        state.fetching.discard(airport_id)


# ---------------------------------------------------------------------------- scheduler jobs
async def job_fetch() -> None:
    if maintenance_hold():
        return
    if state.fetching or state.running or gate.owner:
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
    if maintenance_hold():
        return
    if state.running:
        return
    interval = max(1, db.get_int("test_interval_minutes", 30))
    anchor = state.last_cycle_at or state.auto_cycle_anchor
    if anchor and (datetime.now() - anchor) < timedelta(minutes=interval):
        return
    try:
        await run_cycle(reason="schedule")
    except Exception as exc:
        db.append_log("ERROR", "检测", f"检测周期异常退出：{exc}")


async def job_publish() -> None:
    """Scheduled publish: either a fixed daily time or a repeating interval.

    Both modes force a full latency + label cycle before writing the subscription.
    """
    if maintenance_hold():
        return
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
            db.append_log("INFO", "下发", f"间隔下发已启用：每 {interval} 分钟")
            return
        if now < next_at:
            return
        label = f"每 {interval} 分钟"
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
        if not state.paused and not state.stop_requested:
            db.append_log("WARN", "下发", "定时下发跳过：已有检测批次在运行")
        return
    planned_next = _parse_ts(db.get("sub_publish_next_at"))
    db.append_log("INFO", "下发", f"定时下发开始（{label}）")
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

    if result.get("ok") and result.get("stopped"):
        if result.get("published"):
            # A partial publish must not restart the cadence. Advance from the originally
            # planned slot so the configured interval itself stays unchanged.
            now2 = datetime.now()
            if mode == "interval":
                nxt = next_publish_at("interval", interval=interval, base=planned_next or now2)
                if nxt and nxt <= now2:
                    nxt = next_publish_at("interval", interval=interval, base=now2)
            else:
                nxt = next_publish_at("time", hhmm=hhmm, base=now2)
            if nxt:
                db.set_setting("sub_publish_next_at", nxt.strftime(ts_fmt))
            db.set_setting("sub_last_auto_publish", now2.strftime("%Y-%m-%d"))
            db.set_setting("sub_last_auto_publish_at", now2.strftime(ts_fmt))
            db.append_log("WARN", "下发",
                          f"检测被停止，已按当前进度下发；下发间隔保持不变，下次 "
                          f"{nxt.strftime(ts_fmt) if nxt else '未安排'}")
        else:
            settle(1)
            db.append_log("ERROR", "下发", "检测被停止，但当前进度下发失败，下一分钟重试")
        return
    if result.get("ok") and result.get("published"):
        settle(None)
        db.append_log("INFO", "下发", "定时下发完成")
        return
    if not result.get("ok"):
        error = result.get("error", "")
        if "为空" in error or "没有启用" in error or "没有到期" in error:
            settle(None)
            db.append_log("WARN", "下发", f"定时下发跳过：{error}")
        else:
            # retry in one minute instead of burning the whole interval
            settle(1)
            db.append_log("ERROR", "下发", f"定时下发失败，下一分钟重试：{error}")
        return
    settle(None)
    db.append_log("WARN", "下发", "定时检测已完成，但订阅未下发")


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
    if maintenance_hold():
        return
    days = db.get_int("history_days", 30)
    cutoff = (datetime.now() - timedelta(days=max(7, min(days, 365)))).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM results WHERE ts < ?", (cutoff,))
    db.execute("DELETE FROM latency_results WHERE ts < ?", (cutoff,))
    # 仪表盘提供「近 30 天」曲线，占比快照至少保留 30 天，不受历史保留天数影响
    share_cutoff = (datetime.now() - timedelta(days=max(30, min(days, 365)))).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("DELETE FROM share_history WHERE ts < ?", (share_cutoff,))
    db.execute("DELETE FROM fetch_logs WHERE ts < ?", (cutoff,))
    # 检测计划留档要覆盖评分窗口，保留期固定 180 天
    db.execute("DELETE FROM schedule_history WHERE changed_at < ?",
               ((datetime.now() - timedelta(days=180)).strftime("%Y-%m-%d %H:%M:%S"),))
    db.execute("DELETE FROM batches WHERE started_at < ?", (cutoff,))
    # 节点被删除或从机场订阅里消失后，结果行会变成孤儿，一并清掉
    db.execute("DELETE FROM results WHERE node_id NOT IN (SELECT id FROM nodes)")
    db.execute("DELETE FROM latency_results WHERE node_id NOT IN (SELECT id FROM nodes)")
    db.append_log("INFO", "维护", f"已清理 {cutoff} 之前的历史记录")


async def job_health_p2() -> None:
    """P2 tick: enqueue due nodes and start a bounded probe batch."""
    if maintenance_hold():
        return
    await health_service.tick()


async def job_business_p3() -> None:
    """P3 tick: independently schedule node/business capability checks."""
    if maintenance_hold():
        return
    await business_service.tick()


async def job_quality_p4() -> None:
    """P4 tick: refresh seven-day quality and independent speed evaluation."""
    if maintenance_hold():
        return
    await quality_service.tick()


def start_scheduler() -> None:
    if scheduler.running:
        return
    db.bootstrap_schedule_plan(db.get_int("test_interval_minutes", 30))
    db.record_schedule_plan(db.get_int("test_interval_minutes", 30), "boot")
    # With the startup test disabled, arm the first automatic cycle one full interval
    # after boot instead of letting job_cycle fire as soon as it sees last_cycle_at=None.
    state.auto_cycle_anchor = None
    if db.get("test_on_start", "1") != "1":
        state.auto_cycle_anchor = datetime.now()
    scheduler.add_job(job_fetch, "interval", minutes=1, id="fetch", max_instances=1,
                      coalesce=True, next_run_time=datetime.now() + timedelta(seconds=10))
    scheduler.add_job(job_cycle, "interval", minutes=1, id="cycle", max_instances=1,
                      coalesce=True, next_run_time=datetime.now() + timedelta(seconds=40))
    scheduler.add_job(job_publish, "interval", minutes=1, id="publish", max_instances=1,
                      coalesce=True, next_run_time=datetime.now() + timedelta(seconds=50))
    scheduler.add_job(job_maintenance, "interval", hours=6, id="maintenance", max_instances=1)
    # P2 runs on a five-second dispatcher so 15/30-second rechecks are reachable.
    # It still starts one bounded batch at a time and never duplicates a node.
    # Tests drive tick() directly; never let a wall-clock probe alter fixtures.
    if os.getenv("NODEPOOL_P2_SCHEDULER", "1") == "1" and "pytest" not in sys.modules:
        health_service.startup_reset()
        scheduler.add_job(job_health_p2, "interval", seconds=5, id="p2_health",
                          max_instances=1, coalesce=True,
                          next_run_time=datetime.now() + timedelta(seconds=10))
    if os.getenv("NODEPOOL_P3_SCHEDULER", "1") == "1" and "pytest" not in sys.modules:
        business_service.startup_reset()
        scheduler.add_job(job_business_p3, "interval", seconds=10, id="p3_business",
                          max_instances=1, coalesce=True,
                          next_run_time=datetime.now() + timedelta(seconds=20))
    if os.getenv("NODEPOOL_P4_SCHEDULER", "1") == "1" and "pytest" not in sys.modules:
        scheduler.add_job(job_quality_p4, "interval", minutes=10, id="p4_quality",
                          max_instances=1, coalesce=True,
                          next_run_time=datetime.now() + timedelta(seconds=30))
    scheduler.start()
    db.append_log("INFO", "调度", "调度器已启动")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)


