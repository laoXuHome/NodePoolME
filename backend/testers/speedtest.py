"""Public download-speed measurement through a node's local SOCKS listener.

The module is deliberately independent from availability/tag testing: it only writes
speed_runs / speed_samples, never touches nodes.status, fail_count or scoring fields.
Speed sources are public HTTPS files that support Range requests; health checks are
performed directly (never through a node), so a broken source cannot be mistaken for a
broken node.
"""
import asyncio
import math
import re
import socket
import statistics
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx

from backend import config
from backend.database import db
from backend.testers import engine as engine_mod

CONTENT_RANGE_RE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.I)
PROBE_BYTES = 256 * 1024
MIN_SOURCE_SIZE = 8 * 1024 * 1024
SPEED_UA = "NodePoolME-Speedtest/1.0"


@dataclass
class SourceCheck:
    source_id: int
    ok: bool
    dns_ms: int | None = None
    connect_ms: int | None = None
    tls_ms: int | None = None
    status_code: int | None = None
    range_ok: bool = False
    content_length: int = 0
    bytes: int = 0
    error_type: str | None = None
    message: str | None = None


@dataclass
class WorkerSample:
    ok: bool
    bytes: int = 0
    traffic_bytes: int = 0
    seconds: float = 0.0
    single_mbps: float | None = None
    single_mb_per_s: float | None = None
    status_code: int | None = None
    range_ok: bool = False
    connect_ms: int | None = None
    tls_ms: int | None = None
    ttfb_ms: int | None = None
    error_type: str | None = None
    message: str | None = None
    range_limited: bool = False
    traffic_limited: bool = False


@dataclass
class SourceResult:
    source: dict
    ok: bool
    bytes: int = 0
    seconds: float = 0.0
    single_mbps: float | None = None
    multi_mbps: float | None = None
    single_mb_per_s: float | None = None
    multi_mb_per_s: float | None = None
    status_code: int | None = None
    range_ok: bool = False
    connect_ms: int | None = None
    tls_ms: int | None = None
    ttfb_ms: int | None = None
    error_type: str | None = None
    message: str | None = None
    confidence: str = "none"
    range_limited: bool = False
    traffic_limited: bool = False
    samples: list[dict] = field(default_factory=list)


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


SPEED_MARKERS = ("🟣", "🟢", "🔵", "🟠", "🔴", "⚪", "⚠️", "🚫")
PENDING_PREFIX = "待测速"
GRADE_DEFS = {
    "excellent": {"label": "优", "emoji": "🟣", "color": "#8B5CF6", "key": "excellent"},
    "good": {"label": "良", "emoji": "🟢", "color": "#16A34A", "key": "good"},
    "medium": {"label": "中", "emoji": "🔵", "color": "#2563EB", "key": "medium"},
    "poor": {"label": "差", "emoji": "🟠", "color": "#EA580C", "key": "poor"},
    "danger": {"label": "危", "emoji": "🔴", "color": "#DC2626", "key": "danger"},
}


def grade_thresholds() -> dict[str, float]:
    return {
        "excellent": float(db.get("speed_grade_excellent", "5") or 5),
        "good": float(db.get("speed_grade_good", "2") or 2),
        "medium": float(db.get("speed_grade_medium", "1") or 1),
        "poor": float(db.get("speed_grade_poor", "0.25") or 0.25),
    }


def grade_for(mb_per_s: float | None, thresholds: dict | None = None) -> dict:
    """Five-level grade based on real download throughput in MB/s."""
    if mb_per_s is None or mb_per_s < 0:
        return {}
    th = thresholds or grade_thresholds()
    excellent, good = th["excellent"], th["good"]
    medium, poor = th["medium"], th["poor"]
    if mb_per_s >= excellent:
        key = "excellent"
    elif mb_per_s >= good:
        key = "good"
    elif mb_per_s >= medium:
        key = "medium"
    elif mb_per_s >= poor:
        key = "poor"
    else:
        key = "danger"
    return {**GRADE_DEFS[key], "threshold_mb_s": poor if key == "danger" else {
        "excellent": excellent, "good": good, "medium": medium, "poor": poor
    }[key]}


def strip_speed_prefix(name: str | None) -> str:
    """Remove any speed marker we prepended, so names are rebuilt from the raw name."""
    base = str(name or "")
    changed = True
    while changed:
        changed = False
        for marker in SPEED_MARKERS:
            if base.startswith(marker):
                base = base[len(marker):]
                changed = True
                break
        if base.startswith(PENDING_PREFIX):
            base = base[len(PENDING_PREFIX):]
            changed = True
    return base


def apply_speed_prefix(name: str | None, marker: str = PENDING_PREFIX) -> str:
    return f"{marker}{strip_speed_prefix(name)}"


def _run_age_days(finished_at: str | None) -> float | None:
    if not finished_at:
        return None
    try:
        stamp = datetime.strptime(finished_at, "%Y-%m-%d %H:%M:%S").astimezone()
        return max(0.0, (datetime.now().astimezone() - stamp).total_seconds() / 86400)
    except ValueError:
        return None


def _elapsed_ms(start: float) -> int:
    return max(0, int((time.perf_counter() - start) * 1000))


def _median(values: list[float]) -> float | None:
    return round(statistics.median(values), 2) if values else None


def parse_content_range(value: str | None) -> tuple[int, int, int] | None:
    """Return (start, end, total). total is 0 when the server sent '*'."""
    if not value:
        return None
    match = CONTENT_RANGE_RE.search(value)
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = 0 if match.group(3) == "*" else int(match.group(3))
    if end < start:
        return None
    return start, end, total


def range_windows(size: int, connections: int) -> list[tuple[int, int]]:
    """Split the file into non-overlapping, independently requestable ranges."""
    if size <= 0:
        size = 100 * 1024 * 1024
    connections = max(1, min(connections, 8))
    span = max(1, size // connections)
    windows = []
    for i in range(connections):
        start = i * span
        end = size - 1 if i == connections - 1 else min(size - 1, start + span - 1)
        if start > size - 1:
            break
        windows.append((start, end))
    return windows


def _timings(request: httpx.Request) -> dict:
    """httpcore trace events (available through httpx request extensions)."""
    marks: dict[str, float] = {}

    async def trace(event: str, info: dict) -> None:
        marks[event] = time.perf_counter()

    request.extensions["trace"] = trace
    return marks


def _trace_ms(marks: dict, start_event: str, end_event: str) -> int | None:
    start, end = marks.get(start_event), marks.get(end_event)
    if start is None or end is None or end < start:
        return None
    return int((end - start) * 1000)


def _client(proxy: str | None, timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        proxy=proxy,
        timeout=httpx.Timeout(timeout, connect=min(timeout, 6.0)),
        follow_redirects=False,
        trust_env=False,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=0),
        headers={"User-Agent": SPEED_UA, "Accept": "*/*", "Accept-Encoding": "identity"},
    )


def list_sources(enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM speed_sources"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY priority, latency_ms IS NULL, latency_ms, id"
    return db.rows(sql)


def select_sources(limit: int = 3) -> list[dict]:
    healthy = [s for s in list_sources(enabled_only=True)
               if s["health"] == "ok" and s["range_ok"]]
    return healthy[:max(1, limit)]


async def _dns_ms(url: str) -> tuple[int | None, str | None]:
    host = urlsplit(url).hostname
    if not host:
        return None, "URL 缺少域名"
    started = time.perf_counter()
    try:
        await asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM)
        return _elapsed_ms(started), None
    except OSError as exc:
        return None, f"DNS 解析失败：{exc}"


async def check_source(source: dict, client: httpx.AsyncClient | None = None) -> SourceCheck:
    """Direct HTTPS + Range probe. Never routed through a node."""
    dns_ms, dns_error = await _dns_ms(source["url"])
    if dns_error:
        check = SourceCheck(source["id"], False, dns_ms=dns_ms, error_type="dns",
                            message=dns_error)
        _save_check(check)
        return check
    own_client = client is None
    if own_client:
        client = _client(None, 10.0)
    check = SourceCheck(source["id"], False, dns_ms=dns_ms)
    try:
        request = client.build_request(
            "GET", source["url"],
            headers={"Range": f"bytes=0-{PROBE_BYTES - 1}"},
        )
        marks = _timings(request)
        started = time.perf_counter()
        response = await client.send(request, stream=True)
        check.status_code = response.status_code
        check.connect_ms = _trace_ms(marks, "connection.connect_tcp.started",
                                     "connection.connect_tcp.complete")
        check.tls_ms = _trace_ms(marks, "connection.start_tls.started",
                                 "connection.start_tls.complete")
        try:
            if response.status_code != 206:
                check.error_type = "range" if response.status_code == 200 else "status"
                check.message = (f"Range 请求返回 HTTP {response.status_code}，"
                                 "不是 206" if response.status_code == 200
                                 else f"测速源返回 HTTP {response.status_code}")
                return check
            parsed = parse_content_range(response.headers.get("content-range"))
            if not parsed:
                check.error_type = "range"
                check.message = "缺少或无法解析 Content-Range"
                return check
            start, _, total = parsed
            if start != 0:
                check.error_type = "range"
                check.message = f"Content-Range 起点异常：{start}"
                return check
            check.range_ok = True
            check.content_length = total
            if total and total < MIN_SOURCE_SIZE:
                check.error_type = "size"
                check.message = f"测速文件过小（{total} 字节）"
                return check
            received = 0
            async for chunk in response.aiter_bytes():
                received += len(chunk)
                if received >= PROBE_BYTES:
                    break
            check.bytes = received
            if received < PROBE_BYTES // 2:
                check.error_type = "short"
                check.message = f"只收到 {received} 字节，疑似被限速或断流"
                return check
            check.ok = True
            check.message = "HTTPS / Range / 持续下载均正常"
            return check
        finally:
            await response.aclose()
    except asyncio.TimeoutError:
        check.error_type = "timeout"
        check.message = "健康检查超时"
        return check
    except httpx.HTTPError as exc:
        check.error_type = "tls" if "SSL" in type(exc).__name__ else "connect"
        check.message = f"HTTPS 连接失败：{type(exc).__name__}"
        return check
    finally:
        if own_client:
            await client.aclose()
        check.connect_ms = check.connect_ms or _elapsed_ms(started)
        _save_check(check)


def _save_check(check: SourceCheck) -> None:
    source = db.row("SELECT * FROM speed_sources WHERE id=?", (check.source_id,))
    if not source:
        return
    db.execute(
        """INSERT INTO speed_source_checks
           (source_id, ts, ok, dns_ms, connect_ms, tls_ms, status_code, range_ok,
            content_length, bytes, error_type, message)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (check.source_id, now(), 1 if check.ok else 0, check.dns_ms, check.connect_ms,
         check.tls_ms, check.status_code, 1 if check.range_ok else 0,
         check.content_length, check.bytes, check.error_type, check.message),
    )
    db.execute(
        """UPDATE speed_sources SET health=?, range_ok=?, size_bytes=?, latency_ms=?,
           last_error=?, last_check_at=?, updated_at=? WHERE id=?""",
        ("ok" if check.ok else "fail", 1 if check.range_ok else 0,
         check.content_length, check.connect_ms, None if check.ok else check.message,
         now(), now(), check.source_id),
    )


async def check_sources(source_ids: list[int] | None = None) -> list[dict]:
    sources = [s for s in list_sources(enabled_only=True)
               if not source_ids or s["id"] in source_ids]
    sem = asyncio.Semaphore(3)

    async def one(source):
        async with sem:
            return await check_source(source)

    checks = await asyncio.gather(*(one(s) for s in sources))
    return [asdict(c) for c in checks]


async def _worker(client: httpx.AsyncClient, url: str, window: tuple[int, int],
                  warmup: float, sample: float, conn_timeout: float,
                  total_deadline: float, max_bytes: int | None = None) -> WorkerSample:
    start, end = window
    request = client.build_request("GET", url, headers={"Range": f"bytes={start}-{end}"})
    marks = _timings(request)
    sent = time.perf_counter()
    response = None
    measured_bytes = 0
    warm_bytes = 0
    total_bytes = 0
    measure_start = None
    last_data = None
    range_limited = False
    traffic_limited = False
    try:
        response = await client.send(request, stream=True)
        status = response.status_code
        ttfb = _elapsed_ms(sent)
        connect_ms = _trace_ms(marks, "connection.connect_tcp.started",
                               "connection.connect_tcp.complete")
        tls_ms = _trace_ms(marks, "connection.start_tls.started",
                           "connection.start_tls.complete")
        if status != 206:
            return WorkerSample(False, status_code=status, error_type="range" if status == 200
                                else "status", connect_ms=connect_ms, tls_ms=tls_ms,
                                ttfb_ms=ttfb,
                                message=f"Range 请求返回 HTTP {status}")
        parsed = parse_content_range(response.headers.get("content-range"))
        if not parsed or parsed[0] != start:
            return WorkerSample(False, status_code=status, error_type="range",
                                connect_ms=connect_ms, tls_ms=tls_ms, ttfb_ms=ttfb,
                                message="Content-Range 与请求区间不一致")
        began = time.perf_counter()
        iterator = response.aiter_bytes()
        while True:
            elapsed = time.perf_counter() - began
            if elapsed >= total_deadline:
                break
            if max_bytes is not None and total_bytes >= max_bytes:
                traffic_limited = True
                break
            try:
                chunk = await asyncio.wait_for(anext(iterator), timeout=conn_timeout)
            except StopAsyncIteration:
                range_limited = True
                break
            except asyncio.TimeoutError:
                if measure_start is None:
                    return WorkerSample(False, status_code=status, range_ok=True,
                                        connect_ms=connect_ms, tls_ms=tls_ms, ttfb_ms=ttfb,
                                        error_type="node_connect", message="等待首字节超时")
                return WorkerSample(False, status_code=status, range_ok=True,
                                    bytes=measured_bytes, connect_ms=connect_ms,
                                    tls_ms=tls_ms, ttfb_ms=ttfb,
                                    error_type="node_interrupted", message="下载中途断流")
            if not chunk:
                continue
            if max_bytes is not None and total_bytes + len(chunk) > max_bytes:
                chunk = chunk[:max(0, max_bytes - total_bytes)]
                traffic_limited = True
            total_bytes += len(chunk)
            last_data = time.perf_counter()
            if measure_start is None:
                if last_data - began < warmup:
                    warm_bytes += len(chunk)
                    continue
                measure_start = last_data
                measured_bytes += len(chunk)
            else:
                measured_bytes += len(chunk)
            if traffic_limited:
                break
            if measure_start is not None and last_data - measure_start >= sample:
                break
        if measure_start is None:
            elapsed = max(1e-6, (last_data or began) - began)
            mbps = total_bytes * 8 / elapsed / 1e6 if total_bytes else 0.0
            mb_s = total_bytes / elapsed / 1048576 if total_bytes else 0.0
            return WorkerSample(True, bytes=total_bytes, traffic_bytes=total_bytes, seconds=round(elapsed, 3),
                                single_mbps=round(mbps, 2),
                                single_mb_per_s=round(mb_s, 3), status_code=status,
                                range_ok=True, connect_ms=connect_ms, tls_ms=tls_ms,
                                ttfb_ms=ttfb, range_limited=True, traffic_limited=traffic_limited,
                                message="预热阶段区间已耗尽，按整段平均计算")
        seconds = max(1e-6, (last_data or measure_start) - measure_start)
        mbps = measured_bytes * 8 / seconds / 1e6 if measured_bytes else 0.0
        mb_s = measured_bytes / seconds / 1048576 if measured_bytes else 0.0
        return WorkerSample(True, bytes=measured_bytes, traffic_bytes=total_bytes, seconds=round(seconds, 3),
                            single_mbps=round(mbps, 2),
                            single_mb_per_s=round(mb_s, 3), status_code=status,
                            range_ok=True, connect_ms=connect_ms, tls_ms=tls_ms,
                            ttfb_ms=ttfb, range_limited=range_limited,
                            traffic_limited=traffic_limited)
    except httpx.HTTPError as exc:
        return WorkerSample(False, error_type="node_connect",
                            message=f"代理连接失败：{type(exc).__name__}")
    finally:
        if response is not None:
            await response.aclose()


async def _test_source(source: dict, proxy_port: int, node: dict,
                       settings: dict, max_bytes: int | None = None) -> SourceResult:
    connections = settings["connections"]
    warmup = settings["warmup"]
    sample = settings["sample"]
    conn_timeout = settings["conn_timeout"]
    total_timeout = settings["total_timeout"]
    size = int(source.get("size_bytes") or 100 * 1024 * 1024)
    windows = range_windows(size, connections)
    per_worker = None
    if max_bytes is not None:
        per_worker = max(1, int(max_bytes) // max(1, len(windows)))
    proxy = f"socks5://127.0.0.1:{proxy_port}"
    client = _client(proxy, conn_timeout)
    try:
        tasks = [_worker(client, source["url"], w, warmup, sample, conn_timeout,
                         total_timeout, per_worker) for w in windows]
        results = await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True),
                                         timeout=total_timeout)
    except asyncio.TimeoutError:
        # Tasks were cancelled without a final byte count; charge the whole
        # source budget so a timed-out source can never create extra traffic.
        return SourceResult(source, False, bytes=int(max_bytes or 0), error_type="node_connect",
                            message=f"整轮测速超过 {total_timeout:g} 秒")
    finally:
        await client.aclose()
    workers = []
    errors = []
    all_samples = []
    for item in results:
        if isinstance(item, Exception):
            errors.append(str(item))
        elif isinstance(item, WorkerSample):
            all_samples.append(item)
            if item.ok:
                workers.append(item)
            else:
                errors.append(item)
    traffic_bytes = sum(getattr(w, "traffic_bytes", w.bytes) or w.bytes for w in all_samples)
    if not workers:
        first = errors[0] if errors else None
        if isinstance(first, WorkerSample):
            return SourceResult(source, False, bytes=traffic_bytes,
                                status_code=first.status_code,
                                range_ok=first.range_ok, error_type=first.error_type,
                                message=first.message)
        return SourceResult(source, False, bytes=traffic_bytes, error_type="source_error",
                            message=f"四个连接全部失败：{first or '未知错误'}")
    seconds = max(w.seconds for w in workers)
    total_bytes = sum(w.bytes for w in workers)
    multi = round(total_bytes * 8 / max(seconds, 1e-6) / 1e6, 2)
    multi_mb = round(total_bytes / max(seconds, 1e-6) / 1048576, 3)
    single = _median([w.single_mbps for w in workers if w.single_mbps is not None])
    single_mb = _median([w.single_mb_per_s for w in workers if w.single_mb_per_s is not None])
    range_limited = any(w.range_limited for w in workers)
    traffic_limited = any(w.traffic_limited for w in workers)
    confidence = "normal" if len(workers) >= 3 and seconds >= sample * 0.5 else "low"
    note = "区间提前耗尽，按实际字节与时间计算" if range_limited else None
    if traffic_limited:
        note = "达到单节点流量上限，按已下载的有效数据计算"
    return SourceResult(
        source, True, bytes=traffic_bytes, seconds=round(seconds, 3),
        single_mbps=single, multi_mbps=multi, single_mb_per_s=single_mb,
        multi_mb_per_s=multi_mb, status_code=workers[0].status_code, range_ok=True,
        connect_ms=workers[0].connect_ms, tls_ms=workers[0].tls_ms,
        ttfb_ms=workers[0].ttfb_ms, confidence=confidence,
        range_limited=range_limited, traffic_limited=traffic_limited,
        message=note,
        samples=[asdict(w) for w in workers],
    )


def _settings() -> dict:
    limit_mb = max(1, min(db.get_int("speed_traffic_limit_mb", 50), 1024))
    return {
        "connections": max(1, min(db.get_int("speed_connections", 4), 8)),
        "warmup": max(0, float(db.get("speed_warmup_seconds", "2") or 2)),
        "sample": max(3, float(db.get("speed_sample_seconds", "10") or 10)),
        "conn_timeout": max(3, float(db.get("speed_connect_timeout", "8") or 8)),
        "total_timeout": max(8, float(db.get("speed_total_timeout", "20") or 20)),
        "retry": max(0, min(db.get_int("speed_retry", 1), 2)),
        "primary": max(1, min(db.get_int("speed_primary_sources", 3), 5)),
        "traffic_limit_mb": limit_mb,
        "traffic_limit_bytes": limit_mb * 1024 * 1024,
        "ttl_days": max(1, min(db.get_int("speed_result_ttl_days", 3), 30)),
    }


async def _ensure_sources(primary: int) -> list[dict]:
    healthy = select_sources(primary + 2)
    if len(healthy) >= primary:
        return healthy[:primary]
    unchecked = [s for s in list_sources(enabled_only=True)
                 if s["health"] != "fail" and s["id"] not in {h["id"] for h in healthy}][:8]
    if unchecked:
        await check_sources([s["id"] for s in unchecked])
    return select_sources(primary + 2)


async def run_node(node_id: int, source_ids: list[int] | None = None,
                   trigger: str = "manual", prepared: bool = False,
                   proxy_port: int | None = None) -> dict:
    """Test one node with one shared byte budget across every source and retry.

    ``prepared=True`` is used by the queue when several nodes share one Mihomo
    configuration; the caller has already called ``engine.prepare(nodes)``.
    """
    node = db.row("SELECT n.*, a.name AS airport_name, a.speed_excluded "
                  "FROM nodes n JOIN airports a ON a.id=n.airport_id WHERE n.id=?",
                  (node_id,))
    if not node:
        raise ValueError("节点不存在")
    if node.get("speed_excluded"):
        return {"node_id": node_id, "status": "excluded", "message": "机场已排除测速"}
    engine_mod.normalize_node(node)
    settings = _settings()
    run_id = db.execute(
        "INSERT INTO speed_runs (node_id, started_at, status, confidence, airport_name, "
        "trigger, traffic_limit_bytes) VALUES (?,?,?,?,?,?,?)",
        (node_id, now(), "running", "none", node["airport_name"], trigger,
         settings["traffic_limit_bytes"]),
    )
    engine = engine_mod.engine_manager.engine
    if not engine or engine.name != "mihomo":
        db.execute("UPDATE speed_runs SET finished_at=?, status='engine_error', "
                   "error_type='engine', message=? WHERE id=?",
                   (now(), "当前是 TCP 检测引擎，下载测速需要 Mihomo 内核", run_id))
        return get_run(run_id)
    try:
        if not prepared:
            await engine_mod.engine_manager.prepare([node])
        port = proxy_port or getattr(engine, "block_ports", {}).get(node["fingerprint"])
        if not port:
            raise RuntimeError("节点未绑定本地 SOCKS 端口")

        sources = await _ensure_sources(settings["primary"])
        if source_ids:
            chosen = {int(x) for x in source_ids}
            sources = [s for s in sources if s["id"] in chosen]
            missing = [s for s in list_sources(enabled_only=True)
                       if s["id"] in chosen and s["health"] == "unknown"]
            if missing:
                await check_sources([s["id"] for s in missing])
                sources = [s for s in select_sources(max(settings["primary"], len(chosen)))
                           if s["id"] in chosen]
        if not sources:
            db.execute("UPDATE speed_runs SET finished_at=?, status='source_unavailable', "
                       "error_type='source', message=? WHERE id=?",
                       (now(), "没有通过健康检查的测速源", run_id))
            return get_run(run_id)
        remaining = settings["traffic_limit_bytes"]
        valid: list[SourceResult] = []
        attempts = 0
        node_errors = 0
        source_errors = 0
        samples_bytes = 0
        consumed_bytes = 0
        max_attempts = min(len(sources), settings["primary"] + 2)
        db.execute("UPDATE speed_runs SET sources_total=? WHERE id=?",
                   (max_attempts, run_id))
        for index, source in enumerate(sources):
            if attempts >= max_attempts or len(valid) >= settings["primary"]:
                break
            if remaining <= 256 * 1024:
                break
            left_sources = max(1, max_attempts - attempts)
            budget = remaining if left_sources == 1 else max(1024 * 1024, remaining // left_sources)
            budget = min(remaining, budget)
            attempts += 1
            attempt = 0
            result = None
            while attempt <= settings["retry"] and remaining > 256 * 1024:
                attempt += 1
                result = await _test_source(source, port, node, settings, max_bytes=budget)
                remaining -= min(result.bytes, remaining)
                consumed_bytes += result.bytes
                _save_sample(run_id, node_id, result)
                if result.ok:
                    break
                # A node that cannot open the proxy tunnel will not recover on retry.
                if result.error_type == "node_connect":
                    break
                budget = min(remaining, max(256 * 1024, remaining))
            if result is None:
                break
            if result.ok:
                valid.append(result)
            elif result.error_type in ("node_connect", "node_interrupted"):
                node_errors += 1
            else:
                source_errors += 1
                db.execute("UPDATE speed_sources SET health='fail', last_error=?, "
                           "updated_at=? WHERE id=?",
                           (result.message or result.error_type, now(), source["id"]))
                db.append_log("WARN", "测速",
                              f"节点 #{node_id} 测速源「{source['name']}」失败："
                              f"{result.error_type or 'error'} {result.message or ''}".strip())
            if node_errors >= 2:
                break
        sources_ok = len(valid)
        samples_bytes = consumed_bytes
        finish = now()
        if sources_ok:
            multi_mb = _median([r.multi_mb_per_s for r in valid if r.multi_mb_per_s is not None])
            single_mb = _median([r.single_mb_per_s for r in valid
                                 if r.single_mb_per_s is not None])
            multi_mbps = _median([r.multi_mbps for r in valid if r.multi_mbps is not None])
            single_mbps = _median([r.single_mbps for r in valid if r.single_mbps is not None])
            seconds = max((r.seconds for r in valid), default=0)
            connect_ms = _median([r.connect_ms for r in valid if r.connect_ms is not None])
            confidence = "normal" if sources_ok >= 2 else "low"
            status = "ok" if sources_ok >= 2 else "partial"
            grade = grade_for(multi_mb)
            note = f"{sources_ok}/{attempts} 个测速源有效"
            if sources_ok == 1:
                note += "（低可信度）"
            if remaining <= 256 * 1024:
                note += "；达到单节点流量上限"
            db.execute(
                "UPDATE speed_runs SET finished_at=?, status=?, confidence=?, multi_mbps=?, "
                "single_mbps=?, mb_per_s=?, single_mb_per_s=?, bytes_total=?, "
                "download_seconds=?, connect_ms=?, grade=?, traffic_limit_bytes=?, "
                "sources_total=?, sources_ok=?, message=? WHERE id=?",
                (finish, status, confidence, multi_mbps, single_mbps, multi_mb, single_mb,
                 samples_bytes, round(seconds, 3), connect_ms, grade.get("key"),
                 settings["traffic_limit_bytes"],
                 attempts, sources_ok, note, run_id),
            )
            return get_run(run_id)
        multi_mb = single_mb = None
        confidence = "none"
        if node_errors and not source_errors:
            status, err = "node_error", "节点无法通过代理建立下载连接"
        elif node_errors and source_errors:
            status, err = "failed", "节点与测速源均有失败"
        else:
            status, err = "source_unavailable", "所有测速源均失败"
        db.execute("UPDATE speed_runs SET finished_at=?, status=?, confidence=?, "
                   "sources_total=?, sources_ok=?, bytes_total=?, traffic_limit_bytes=?, "
                   "error_type=?, message=? WHERE id=?",
                   (finish, status, confidence, attempts, sources_ok, samples_bytes,
                    settings["traffic_limit_bytes"], "node" if node_errors else "source",
                    err, run_id))
        return get_run(run_id)
    except Exception as exc:  # never let a speed test break the process
        db.execute("UPDATE speed_runs SET finished_at=?, status='failed', error_type=?, "
                   "message=? WHERE id=?",
                   (now(), type(exc).__name__, str(exc)[:300], run_id))
        db.append_log("WARN", "测速", f"节点 #{node_id} 下载测速失败：{type(exc).__name__}")
        return get_run(run_id)


def _save_sample(run_id: int, node_id: int, result: SourceResult) -> None:
    source = result.source
    db.execute(
        """INSERT INTO speed_samples
           (run_id, node_id, source_id, source_name, source_region, started_at,
            dns_ms, connect_ms, tls_ms, ttfb_ms, bytes, seconds, single_mbps, multi_mbps,
            status_code, range_ok, ok, error_type, message, confidence)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (run_id, node_id, source["id"], source["name"], source.get("region", ""), now(),
         None, result.connect_ms, result.tls_ms, result.ttfb_ms, result.bytes,
         result.seconds, result.single_mbps, result.multi_mbps, result.status_code,
         1 if result.range_ok else 0, 1 if result.ok else 0, result.error_type,
         result.message, result.confidence),
    )


def get_run(run_id: int) -> dict:
    run = db.row("SELECT * FROM speed_runs WHERE id=?", (run_id,))
    if not run:
        return {}
    run["samples"] = db.rows(
        "SELECT * FROM speed_samples WHERE run_id=? ORDER BY id", (run_id,)
    )
    node = db.row("SELECT raw_name, protocol, server FROM nodes WHERE id=?", (run["node_id"],))
    if node:
        run.update({f"node_{k}": v for k, v in node.items()})
    return run


def latest_by_node() -> dict[int, dict]:
    rows = db.rows(
        """SELECT r.* FROM speed_runs r JOIN (
               SELECT node_id, MAX(id) AS id FROM speed_runs GROUP BY node_id
           ) s ON s.id = r.id"""
    )
    for row in rows:
        _fill_legacy_units(row)
    return {row["node_id"]: row for row in rows}


def latest_valid_by_node(ttl_days: int | None = None) -> dict[int, dict]:
    rows = db.rows(
        """SELECT r.* FROM speed_runs r JOIN (
               SELECT node_id, MAX(id) AS id FROM speed_runs
               WHERE status IN ('ok','partial') AND (mb_per_s IS NOT NULL OR multi_mbps IS NOT NULL)
               GROUP BY node_id
           ) s ON s.id = r.id"""
    )
    for row in rows:
        _fill_legacy_units(row)
    if ttl_days is not None:
        rows = [r for r in rows
                if (age := _run_age_days(r.get("finished_at") or r.get("started_at"))) is not None
                and age <= ttl_days]
    return {row["node_id"]: row for row in rows}


def _fill_legacy_units(run: dict) -> dict:
    if run.get("mb_per_s") is None and run.get("multi_mbps") is not None:
        run["mb_per_s"] = round(float(run["multi_mbps"]) / 8, 3)
    if run.get("single_mb_per_s") is None and run.get("single_mbps") is not None:
        run["single_mb_per_s"] = round(float(run["single_mbps"]) / 8, 3)
    return run


def speed_map() -> dict[int, dict]:
    """Per-node latest speed tag. Valid failures never overwrite an older valid result."""
    ttl = _settings()["ttl_days"]
    valid = latest_valid_by_node()
    last = latest_by_node()
    thresholds = grade_thresholds()
    excluded = {r["id"]: bool(r["speed_excluded"]) for r in db.rows(
        "SELECT id, speed_excluded FROM airports")}
    nodes = db.rows("SELECT id, airport_id FROM nodes")
    out: dict[int, dict] = {}
    for node in nodes:
        node_id = node["id"]
        if excluded.get(node["airport_id"]):
            out[node_id] = {
                "status": "excluded", "marker": "⚪", "label": "机场已排除测速",
                "grade": None, "grade_label": "", "emoji": "⚪", "color": "#9CA3AF",
                "mb_per_s": None, "mbps": None,
            }
            continue
        run = valid.get(node_id)
        age = _run_age_days((run or {}).get("finished_at") or (run or {}).get("started_at"))
        if run and age is not None and age <= ttl:
            mb = run.get("mb_per_s")
            grade = grade_for(float(mb) if mb is not None else None, thresholds)
            last_run = last.get(node_id) or {}
            out[node_id] = {
                "status": "ok", "marker": grade.get("emoji", ""),
                "label": grade.get("label", ""), "grade": grade.get("key"),
                "grade_label": grade.get("label", ""), "emoji": grade.get("emoji", ""),
                "color": grade.get("color", ""), "mb_per_s": mb,
                "mbps": run.get("multi_mbps"),
                "finished_at": run.get("finished_at"), "age_days": round(age, 2),
                "sources_ok": run.get("sources_ok"), "sources_total": run.get("sources_total"),
                "bytes_total": run.get("bytes_total"), "run_id": run.get("id"),
                "last_run_status": last_run.get("status"),
                "last_message": last_run.get("message"),
            }
            continue
        last_run = last.get(node_id) or {}
        last_status = last_run.get("status")
        if run:
            base = {
                "status": "stale", "marker": PENDING_PREFIX,
                "label": f"测速结果已过期（{round(age or 0, 1)} 天前）",
                "grade": None, "grade_label": "", "emoji": "", "color": "#F59E0B",
                "mb_per_s": run.get("mb_per_s"), "mbps": run.get("multi_mbps"),
                "finished_at": run.get("finished_at"), "age_days": round(age or 0, 2),
                "sources_ok": run.get("sources_ok"), "sources_total": run.get("sources_total"),
                "bytes_total": run.get("bytes_total"), "run_id": run.get("id"),
            }
        elif last_status == "source_unavailable":
            base = {"status": "source_failed", "marker": "🚫",
                    "label": "本轮所有测速网址均失败", "grade": None,
                    "grade_label": "", "emoji": "🚫", "color": "#DC2626",
                    "mb_per_s": None, "mbps": None}
        elif last_status in ("engine_error", "node_error", "failed"):
            base = {"status": "error", "marker": "⚠️",
                    "label": last_run.get("message") or "测速异常",
                    "grade": None, "grade_label": "", "emoji": "⚠️",
                    "color": "#F59E0B", "mb_per_s": None, "mbps": None}
        else:
            base = {"status": "pending", "marker": PENDING_PREFIX,
                    "label": "尚未取得有效测速结果", "grade": None,
                    "grade_label": "", "emoji": "", "color": "#9CA3AF",
                    "mb_per_s": None, "mbps": None}
        base["last_run_status"] = last_status
        base["last_message"] = last_run.get("message")
        out[node_id] = base
    return out


def eligible_node_ids(new_only: bool = False) -> list[int]:
    """Airport enabled + not speed-excluded; optionally without a fresh valid result."""
    sql = """SELECT n.id FROM nodes n JOIN airports a ON a.id=n.airport_id
             WHERE a.enabled=1 AND a.speed_excluded=0"""
    sql += " ORDER BY n.id"
    ids = [r["id"] for r in db.rows(sql)]
    if new_only:
        fresh = latest_valid_by_node(ttl_days=_settings()["ttl_days"])
        ids = [node_id for node_id in ids if node_id not in fresh]
    return ids


def expired_node_ids(ttl_days: int | None = None) -> list[int]:
    ttl = _settings()["ttl_days"] if ttl_days is None else ttl_days
    out = []
    for node_id, run in latest_valid_by_node().items():
        age = _run_age_days(run.get("finished_at") or run.get("started_at"))
        if age is not None and age > ttl:
            out.append(node_id)
    return out


def node_history(node_id: int, limit: int = 20) -> list[dict]:
    runs = db.rows("SELECT * FROM speed_runs WHERE node_id=? ORDER BY id DESC LIMIT ?",
                   (node_id, max(1, min(limit, 100))))
    for run in runs:
        _fill_legacy_units(run)
        run["samples"] = db.rows(
            "SELECT * FROM speed_samples WHERE run_id=? ORDER BY id", (run["id"],)
        )
    return runs


def overview() -> dict:
    sources = list_sources()
    healthy = [s for s in sources if s["health"] == "ok" and s["enabled"]]
    runs = db.rows("SELECT status, confidence FROM speed_runs "
                   "WHERE started_at >= datetime('now','localtime','-1 day')")
    return {
        "sources": len(sources),
        "sources_enabled": len([s for s in sources if s["enabled"]]),
        "sources_healthy": len(healthy),
        "runs_24h": len(runs),
        "runs_ok_24h": len([r for r in runs if r["status"] == "ok"]),
        "settings": _settings(),
        "grades": GRADE_DEFS,
    }
