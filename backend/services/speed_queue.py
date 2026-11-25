"""Unified download-speed queue and evening schedule.

Every scheduled/manual/all/new test goes through this one FIFO queue. Batch concurrency
uses its own speed_parallel_nodes limit, duplicate node jobs are rejected, and the
Mihomo config is prepared once for a batch (the process has one core instance).
"""
import asyncio
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from backend import config
from backend.database import db
from backend.testers import engine as engine_mod
from backend.testers import speedtest
from backend.scheduler import manager as scheduler
from backend.services.runtime_gate import gate

TRIGGER_LABELS = {
    "schedule": "定时测速", "manual": "手动测速",
    "all": "全部重新测速", "new": "新节点测速",
}
JOB_LABELS = {
    "queued": "排队中", "waiting": "等待中", "running": "测速中",
    "ok": "完成", "partial": "完成（低可信）", "failed": "失败",
    "excluded": "已排除", "canceled": "已取消",
    "engine_error": "引擎不可用", "source_unavailable": "测速源全部失败",
    "node_error": "节点连接失败",
}


def _tz() -> ZoneInfo:
    try:
        return ZoneInfo(db.get("speed_timezone", "Asia/Shanghai") or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _clock(value: str | None, fallback: str) -> dtime:
    try:
        hh, mm = str(value or fallback).split(":")[:2]
        return dtime(int(hh), int(mm))
    except Exception:
        hh, mm = fallback.split(":")
        return dtime(int(hh), int(mm))


def window_bounds(now: datetime | None = None) -> tuple[datetime, datetime, datetime]:
    tz = _tz()
    current = (now or datetime.now(tz)).astimezone(tz)
    start = datetime.combine(current.date(), _clock(db.get("speed_window_start"), "18:00"), tz)
    end = datetime.combine(current.date(), _clock(db.get("speed_window_end"), "23:30"), tz)
    if end <= start:
        end += timedelta(days=1)
    return current, start, end


def schedule_state() -> dict:
    current, start, end = window_bounds()
    every = max(1, min(db.get_int("speed_schedule_every_days", 1), 30))
    last = db.get("speed_schedule_last_date") or ""
    next_due = ""
    if last:
        try:
            next_due = (datetime.strptime(last, "%Y-%m-%d").date()
                        + timedelta(days=every)).isoformat()
        except ValueError:
            next_due = ""
    return {
        "enabled": db.get("speed_schedule_enabled", "1") == "1",
        "timezone": db.get("speed_timezone", "Asia/Shanghai"),
        "window_start": db.get("speed_window_start", "18:00"),
        "window_end": db.get("speed_window_end", "23:30"),
        "every_days": every,
        "last_date": last,
        "next_due_date": next_due,
        "in_window": start <= current < end,
        "now": current.strftime("%Y-%m-%d %H:%M:%S"),
    }


@dataclass
class SpeedJob:
    id: int
    node_id: int
    trigger: str
    not_before: float
    source_ids: list[int] | None = None
    status: str = "queued"
    message: str = ""
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    event: asyncio.Event = field(default_factory=asyncio.Event)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "node_id": self.node_id, "trigger": self.trigger,
            "trigger_label": TRIGGER_LABELS.get(self.trigger, self.trigger),
            "status": self.status, "status_label": JOB_LABELS.get(self.status, self.status),
            "message": self.message, "run_id": self.run_id,
            "not_before": datetime.fromtimestamp(self.not_before, _tz()).strftime(
                "%Y-%m-%d %H:%M:%S"),
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class SpeedQueue:
    def __init__(self):
        self.jobs: dict[int, SpeedJob] = {}
        self._order: list[int] = []
        self._next_id = 1
        self._dispatcher: asyncio.Task | None = None
        self._schedule_task: asyncio.Task | None = None
        self._batch_task: asyncio.Task | None = None
        self._wake = asyncio.Event()
        self._stopping = False

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._dispatcher and not self._dispatcher.done():
            return
        self._stopping = False
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="speed-queue")
        self._schedule_task = asyncio.create_task(self._schedule_loop(), name="speed-schedule")
        db.append_log("INFO", "测速", "下载测速队列已启动")

    async def stop(self) -> None:
        self._stopping = True
        self._wake.set()
        for task in (self._schedule_task, self._dispatcher, self._batch_task):
            if task and not task.done():
                task.cancel()
        for task in (self._schedule_task, self._dispatcher, self._batch_task):
            if task:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        self._schedule_task = self._dispatcher = self._batch_task = None

    # ------------------------------------------------------------------ enqueue
    def _node(self, node_id: int) -> dict | None:
        return db.row(
            """SELECT n.*, a.name AS airport_name, a.enabled AS airport_enabled,
                      a.speed_excluded
               FROM nodes n JOIN airports a ON a.id=n.airport_id WHERE n.id=?""",
            (node_id,),
        )

    def _active_for_node(self, node_id: int) -> SpeedJob | None:
        for job in self.jobs.values():
            if job.node_id == node_id and job.status in ("queued", "waiting", "running"):
                return job
        return None

    def enqueue(self, node_id: int, trigger: str = "manual", *,
                not_before: float | None = None,
                source_ids: list[int] | None = None) -> tuple[SpeedJob, bool]:
        node = self._node(node_id)
        if not node:
            raise ValueError("节点不存在")
        if not node["airport_enabled"]:
            raise ValueError("节点所属机场已停用")
        if node["speed_excluded"]:
            raise ValueError("该机场已排除测速")
        existing = self._active_for_node(node_id)
        if existing:
            return existing, False
        job = SpeedJob(self._next_id, node_id, trigger,
                       not_before if not_before is not None else time.time(),
                       source_ids=source_ids)
        self._next_id += 1
        self.jobs[job.id] = job
        self._order.append(job.id)
        db.append_log("INFO", "测速",
                      f"创建{TRIGGER_LABELS.get(trigger, trigger)}任务：节点 #{node_id}")
        self._trim()
        self._wake.set()
        return job, True

    def enqueue_many(self, node_ids: list[int], trigger: str, *,
                     spread: tuple[float, float] | None = None) -> dict:
        created = duplicate = excluded = 0
        starts: list[float] = []
        if spread:
            left, right = spread
            count = max(1, len(node_ids))
            slot = max(1.0, (right - left) / (count + 1))
            for index in range(count):
                center = left + slot * (index + 1)
                starts.append(max(left, min(right - 1, center + random.uniform(-slot * .3, slot * .3))))
            starts.sort()
        for index, node_id in enumerate(node_ids):
            try:
                _, fresh = self.enqueue(
                    node_id, trigger,
                    not_before=(starts[index] if spread else time.time()),
                )
                created += int(fresh)
                duplicate += int(not fresh)
            except ValueError:
                excluded += 1
        self._wake.set()
        return {"requested": len(node_ids), "created": created,
                "duplicate": duplicate, "excluded": excluded}

    async def wait(self, job: SpeedJob, timeout: float = 90) -> dict:
        try:
            await asyncio.wait_for(job.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return {"node_id": job.node_id, "status": "queued", "job_id": job.id,
                    "message": "已进入测速队列，尚未完成"}
        if job.run_id:
            run = speedtest.get_run(job.run_id)
            if run:
                run["job_id"] = job.id
                return run
        return {"node_id": job.node_id, "status": job.status, "job_id": job.id,
                "message": job.message}

    def snapshot(self, limit: int = 20) -> dict:
        counts = {key: 0 for key in JOB_LABELS}
        for job in self.jobs.values():
            counts[job.status] = counts.get(job.status, 0) + 1
        active = [j.as_dict() for j in self.jobs.values()
                  if j.status in ("queued", "waiting", "running")]
        active.sort(key=lambda x: (x["status"] != "running", x["not_before"], x["id"]))
        recent = [j.as_dict() for j in self.jobs.values()
                  if j.status not in ("queued", "waiting", "running")]
        recent.sort(key=lambda x: x["id"], reverse=True)
        return {"counts": counts, "active": active[:limit], "recent": recent[:limit],
                "total_tracked": len(self.jobs), "work": gate.snapshot()}

    # ------------------------------------------------------------------ dispatcher
    async def _dispatch_loop(self) -> None:
        while not self._stopping:
            try:
                if self._batch_task and not self._batch_task.done():
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                now = time.time()
                ready = [self.jobs[jid] for jid in self._order
                         if self.jobs.get(jid) and self.jobs[jid].status in ("queued", "waiting")
                         and self.jobs[jid].not_before <= now]
                if not ready:
                    self._wake.clear()
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
                if scheduler.state.running or (gate.owner and gate.owner != "speed"):
                    waiting_for = "检测批次" if scheduler.state.running else (gate.label or "节点检测")
                    for job in ready:
                        job.status = "waiting"
                        job.message = f"等待{waiting_for}完成"
                    await asyncio.sleep(3)
                    continue
                # 测速批次自己的并发，复用节点检测的 global_concurrency 会让两套任务互相影响
                limit = max(1, min(db.get_int("speed_parallel_nodes", 4), 8))
                batch = ready[:limit]
                self._batch_task = asyncio.create_task(self._run_batch(batch),
                                                       name=f"speed-batch-{batch[0].id}")
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.append_log("WARN", "测速", f"测速队列异常：{type(exc).__name__}: {exc}")
                await asyncio.sleep(3)

    def _job_still_valid(self, job: SpeedJob) -> str | None:
        if db.get("speed_enabled", "1") != "1":
            return "下载测速已停用"
        node = self._node(job.node_id)
        if not node:
            return "节点已删除"
        if not node["airport_enabled"]:
            return "机场已停用"
        if node["speed_excluded"]:
            return "机场已排除测速"
        if job.trigger == "schedule":
            current, start, end = window_bounds()
            if not (start <= current < end):
                return "已超过定时测速窗口"
        return None

    async def _run_batch(self, jobs: list[SpeedJob]) -> None:
        if not await gate.try_acquire("speed", "下载测速"):
            for job in jobs:
                if job.status in ("queued", "waiting"):
                    job.status = "waiting"
                    job.message = f"等待{gate.label or '节点检测'}完成"
            return
        try:
            await self._run_batch_inner(jobs)
        finally:
            await gate.release("speed")

    async def _run_batch_inner(self, jobs: list[SpeedJob]) -> None:
        try:
            valid: list[tuple[SpeedJob, dict]] = []
            for job in jobs:
                job.status = "running"
                job.started_at = db.now()
                reason = self._job_still_valid(job)
                if reason:
                    job.status = "canceled"
                    job.message = reason
                    job.finished_at = db.now()
                    job.event.set()
                    db.append_log("INFO", "测速", f"节点 #{job.node_id} 任务取消：{reason}")
                    continue
                valid.append((job, self._node(job.node_id)))
            if not valid:
                return
            nodes = []
            for _, node in valid:
                node["params"] = db.loads(node.get("params"), {})
                engine_mod.normalize_node(node)
                nodes.append(node)
            engine = engine_mod.engine_manager.engine
            async with engine_mod.operation_lock:
                prepared = False
                if engine and engine.name == "mihomo":
                    try:
                        await engine_mod.engine_manager.prepare(nodes)
                        prepared = True
                    except Exception as exc:
                        for job, _ in valid:
                            job.status = "failed"
                            job.message = f"测速内核准备失败：{exc}"
                            job.finished_at = db.now()
                            job.event.set()
                        db.append_log("WARN", "测速",
                                      f"测速批次内核准备失败：{type(exc).__name__}: {exc}")
                        return
                db.append_log("INFO", "测速",
                              f"{TRIGGER_LABELS.get(valid[0][0].trigger, '测速')}批次开始："
                              f"{len(valid)} 个节点，并发 {len(valid)}")
                ports = getattr(engine, "block_ports", {}) if engine else {}

                async def one(job: SpeedJob, node: dict) -> dict:
                    try:
                        return await speedtest.run_node(
                            job.node_id, job.source_ids, trigger=job.trigger,
                            prepared=prepared, proxy_port=ports.get(node["fingerprint"]),
                        )
                    except Exception as exc:
                        db.append_log("WARN", "测速",
                                      f"节点 #{job.node_id} 测速异常：{type(exc).__name__}: {exc}")
                        return {"status": "failed", "message": str(exc)}

                results = await asyncio.gather(*(one(job, node) for job, node in valid))
                for (job, _), run in zip(valid, results):
                    job.run_id = run.get("id")
                    job.status = run.get("status", "failed")
                    job.message = run.get("message") or ""
                    job.finished_at = db.now()
                    job.event.set()
                    level = "INFO" if job.status in ("ok", "partial") else "WARN"
                    speed = run.get("mb_per_s")
                    extra = f"，{speed:.2f} MB/s" if speed is not None else ""
                    connect = run.get("connect_ms")
                    connect_text = f"，连接 {connect} ms" if connect is not None else ""
                    db.append_log(level, "测速",
                                  f"节点 #{job.node_id} {TRIGGER_LABELS.get(job.trigger, '测速')}"
                                  f"完成：{job.status}{extra}{connect_text}，"
                                  f"{run.get('bytes_total') or 0} 字节，"
                                  f"{run.get('sources_ok') or 0}/{run.get('sources_total') or 0} 源有效")
        except asyncio.CancelledError:
            for job in jobs:
                if job.status in ("queued", "waiting", "running"):
                    job.status = "canceled"
                    job.message = "服务停止"
                    job.finished_at = db.now()
                    job.event.set()
            raise
        except Exception as exc:
            db.append_log("WARN", "测速", f"测速批次异常：{type(exc).__name__}: {exc}")
            for job in jobs:
                if job.status in ("queued", "waiting", "running"):
                    job.status = "failed"
                    job.message = str(exc)
                    job.finished_at = db.now()
                    job.event.set()

    # ------------------------------------------------------------------ schedule
    async def _schedule_loop(self) -> None:
        while not self._stopping:
            try:
                await asyncio.sleep(20)
                today = datetime.now(_tz()).date().isoformat()
                if db.get("speed_expiry_log_date") != today:
                    expired = speedtest.expired_node_ids()
                    if expired:
                        ttl = speedtest._settings()["ttl_days"]
                        db.append_log("INFO", "测速",
                                      f"{len(expired)} 个节点的测速结果已超过 {ttl} 天有效期，"
                                      "显示待测速（不判故障、不自动补测）")
                    db.set_setting("speed_expiry_log_date", today)
                if db.get("speed_enabled", "1") != "1":
                    continue
                if db.get("speed_schedule_enabled", "1") != "1":
                    continue
                current, start, end = window_bounds()
                if not (start <= current < end):
                    continue
                last = db.get("speed_schedule_last_date") or ""
                today = current.date().isoformat()
                if last == today:
                    continue
                every = max(1, min(db.get_int("speed_schedule_every_days", 1), 30))
                if last:
                    try:
                        gap = (current.date() - datetime.strptime(last, "%Y-%m-%d").date()).days
                        if gap < every:
                            continue
                    except ValueError:
                        pass
                node_ids = speedtest.eligible_node_ids()
                if not node_ids:
                    db.append_log("INFO", "测速", "定时计划没有可测节点")
                    db.set_setting("speed_schedule_last_date", today)
                    continue
                left = max(time.time(), current.timestamp())
                right = end.timestamp()
                if right - left < 5:
                    continue
                db.set_setting("speed_schedule_last_date", today)
                result = self.enqueue_many(node_ids, "schedule", spread=(left, right))
                db.append_log("INFO", "测速",
                              f"定时计划已创建：{len(node_ids)} 个节点，"
                              f"{current.strftime('%H:%M')}—{end.strftime('%H:%M')} "
                              f"随机分散，新建 {result['created']}，跳过 {result['duplicate']}")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                db.append_log("WARN", "测速", f"定时计划异常：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ trim
    def _trim(self) -> None:
        finished = [jid for jid in self._order
                    if self.jobs.get(jid) and self.jobs[jid].status not in
                    ("queued", "waiting", "running")]
        while len(finished) > 200:
            jid = finished.pop(0)
            self.jobs.pop(jid, None)
            try:
                self._order.remove(jid)
            except ValueError:
                pass


queue = SpeedQueue()
