"""P4-02: every received byte, including warmup and failure paths, is budgeted."""
from __future__ import annotations

import asyncio
import httpx

from backend.database import db
from backend.testers import engine as engine_mod
from backend.testers import speedtest


class FakeResponse:
    def __init__(self, chunks, *, status=206, content_range="bytes 0-999/1000",
                 after=None):
        self.chunks = chunks
        self.status_code = status
        self.headers = {"content-range": content_range}
        self.after = after

    async def aiter_bytes(self):
        for chunk in self.chunks:
            yield chunk
        if self.after == "raise":
            raise httpx.ReadError("stream broke after partial data")
        if self.after == "sleep":
            await asyncio.sleep(10)

    async def aclose(self):
        pass


class FakeClient:
    def __init__(self, response):
        self.response = response

    def build_request(self, method, url, headers=None):
        return httpx.Request(method, url, headers=headers or {})

    async def send(self, request, stream=True):
        return self.response

    async def aclose(self):
        pass


def _worker_response(response, *, warmup=0.0, conn_timeout=0.05, sample=1.0,
                     total_deadline=2.0, max_bytes=None):
    return asyncio.run(speedtest._worker(
        FakeClient(response), "https://example.com/file.bin", (0, 999),
        warmup, sample, conn_timeout, total_deadline, max_bytes=max_bytes,
    ))


def test_warmup_data_then_timeout_is_returned_and_budgeted():
    sample = _worker_response(FakeResponse([b"x" * 100], after="sleep"),
                              warmup=5.0)
    assert sample.ok is False
    assert sample.bytes == 100
    assert sample.traffic_bytes == 100


def test_midstream_timeout_keeps_warmup_and_measured_bytes():
    sample = _worker_response(
        FakeResponse([b"a" * 50, b"b" * 100], after="sleep"),
        warmup=0.0,
    )
    assert sample.ok is False
    assert sample.bytes == 150
    assert sample.traffic_bytes == 150


def test_http_error_after_partial_data_keeps_received_bytes():
    sample = _worker_response(FakeResponse([b"c" * 120], after="raise"))
    assert sample.ok is False
    assert sample.bytes == 120
    assert sample.traffic_bytes == 120


def test_multi_worker_partial_failure_sums_all_received_bytes(monkeypatch):
    async def fake_worker(client, url, window, warmup, sample, conn_timeout,
                          total_deadline, max_bytes=None):
        if window[0] == 0:
            return speedtest.WorkerSample(
                False, bytes=40, traffic_bytes=40, error_type="status",
                message="HTTP 403")
        return speedtest.WorkerSample(
            True, bytes=100, traffic_bytes=100, seconds=1,
            single_mbps=0.8, single_mb_per_s=0.1, status_code=206, range_ok=True)

    monkeypatch.setattr(speedtest, "_client", lambda *a, **k: FakeClient(FakeResponse([])))
    monkeypatch.setattr(speedtest, "_worker", fake_worker)
    source = {"id": 1, "name": "T", "url": "https://example.com/file.bin",
              "size_bytes": 100 * 1024 * 1024}
    settings = {"connections": 2, "warmup": 2, "sample": 10, "conn_timeout": 8,
                "total_timeout": 20}
    result = asyncio.run(speedtest._test_source(source, 1080, {}, settings, max_bytes=200))
    assert result.ok
    assert result.bytes == 140  # failed worker's bytes are not dropped


def _node(name: str) -> tuple[int, int]:
    airport_id = db.execute(
        "INSERT INTO airports (name, url, enabled, created_at, updated_at) "
        "VALUES (?,?,?,?,?)",
        ("预算机场", "https://example.com/sub", 1,
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    node_id = db.execute(
        "INSERT INTO nodes (airport_id, raw_name, protocol, server, port, fingerprint, params, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (airport_id, name, "ss", "1.2.3.4", 443, f"fp-{name}", "{}",
         "2026-11-25 09:00:00", "2026-11-25 09:00:00"),
    )
    return airport_id, node_id


def test_multi_source_retries_share_one_50mb_run_budget(monkeypatch):
    _, node_id = _node("预算01")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    sources = [
        {"id": 31, "name": "A", "url": "https://example.com/a", "region": ""},
        {"id": 32, "name": "B", "url": "https://example.com/b", "region": ""},
    ]
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1083}

    async def fake_sources(primary):
        return sources

    async def fake_confirm(source):
        return False

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append((source["id"], max_bytes))
        if len(calls) == 1:
            spend = 30 * 1024 * 1024
        elif len(calls) == 2:
            spend = 20 * 1024 * 1024
        else:
            spend = 50 * 1024 * 1024
        return speedtest.SourceResult(
            source, False, bytes=spend, error_type="status", message="HTTP 403")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    monkeypatch.setattr(speedtest, "_confirm_source_failure", fake_confirm)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert calls[0] == (31, 50 * 1024 * 1024)
    assert calls[1] == (31, 20 * 1024 * 1024)
    assert run["bytes_total"] == 50 * 1024 * 1024
    assert run["status"] == "source_unavailable"


def test_multi_source_failures_with_retry_stay_within_budget(monkeypatch):
    _, node_id = _node("预算03")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    sources = [
        {"id": 61, "name": "A", "url": "https://example.com/a", "region": ""},
        {"id": 62, "name": "B", "url": "https://example.com/b", "region": ""},
    ]
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1085}

    async def fake_sources(primary):
        return sources

    async def fake_confirm(source):
        return False

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append((source["id"], max_bytes))
        spend = 20 * 1024 * 1024 if len(calls) <= 2 else min(max_bytes or 0, 10 * 1024 * 1024)
        return speedtest.SourceResult(
            source, False, bytes=spend, error_type="status", message="HTTP 403")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    monkeypatch.setattr(speedtest, "_confirm_source_failure", fake_confirm)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert [source_id for source_id, _ in calls] == [61, 61, 62]
    assert calls[0][1] == 50 * 1024 * 1024
    assert calls[2][1] == 10 * 1024 * 1024
    assert run["bytes_total"] == 50 * 1024 * 1024


def test_retry_after_failure_never_restores_budget(monkeypatch):
    _, node_id = _node("预算02")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    source = {"id": 41, "name": "A", "url": "https://example.com/a", "region": ""}
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1084}

    async def fake_sources(primary):
        return [source]

    async def fake_confirm(source):
        return False

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append(max_bytes)
        return speedtest.SourceResult(
            source, False, bytes=min(max_bytes or 0, 40 * 1024 * 1024),
            error_type="status", message="HTTP 403")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    monkeypatch.setattr(speedtest, "_confirm_source_failure", fake_confirm)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert calls == [50 * 1024 * 1024, 10 * 1024 * 1024]
    assert run["bytes_total"] == 50 * 1024 * 1024
    assert run["bytes_allowed"] == 50 * 1024 * 1024
    assert run["bytes_over"] == 0


def test_timeout_source_result_reports_conservative_budget(monkeypatch):
    source = {"id": 51, "name": "A", "url": "https://example.com/a", "size_bytes": 100}
    settings = {"connections": 2, "warmup": 2, "sample": 10, "conn_timeout": 1,
                "total_timeout": 1}

    async def slow_worker(*args, **kwargs):
        await asyncio.sleep(5)
        return speedtest.WorkerSample(True)

    monkeypatch.setattr(speedtest, "_client", lambda *a, **k: FakeClient(FakeResponse([])))
    monkeypatch.setattr(speedtest, "_worker", slow_worker)
    result = asyncio.run(speedtest._test_source(source, 1080, {}, settings, max_bytes=100))
    assert result.ok is False
    assert result.bytes == 0
    assert result.bytes_allowed == 0
    assert result.bytes_over == 0
    assert result.charged_bytes == 100


def test_budget_take_charges_actual_chunk_and_records_overage():
    budget = speedtest._ByteBudget(5)
    granted = budget.take(8)
    assert granted == 5
    assert budget.remaining == 0
    assert budget.received == 8
    assert budget.allowed == 5
    assert budget.overage == 3
    assert budget.exhausted.is_set()
    # A later chunk cannot reclaim or redistribute the exhausted allowance.
    assert budget.take(4) == 0
    assert budget.received == 12
    assert budget.overage == 7


def test_worker_last_chunk_over_budget_keeps_actual_and_records_overage():
    budget = speedtest._ByteBudget(5)
    sample = asyncio.run(speedtest._worker(
        FakeClient(FakeResponse([b"x" * 8])),
        "https://example.com/file.bin", (0, 4), 0.0, 1.0, 0.05, 2.0,
        max_bytes=budget,
    ))
    assert sample.ok is True
    assert sample.bytes == 8
    assert sample.traffic_bytes == 8
    assert sample.traffic_limited is True
    assert budget.received == 8
    assert budget.allowed == 5
    assert budget.overage == 3
    assert budget.remaining == 0


def test_budget_exhaustion_notifies_and_stops_other_worker(monkeypatch):
    async def fake_worker(client, url, window, warmup, sample, conn_timeout,
                          total_deadline, max_bytes=None):
        if window[0] == 0:
            max_bytes.take(120)
            return speedtest.WorkerSample(
                True, bytes=120, traffic_bytes=120, seconds=1,
                single_mbps=0.8, single_mb_per_s=0.1, status_code=206, range_ok=True,
                traffic_limited=True)
        await max_bytes.exhausted.wait()
        return speedtest.WorkerSample(
            False, bytes=0, traffic_bytes=0, error_type="node_interrupted",
            message="共享流量预算已耗尽，停止读取")

    monkeypatch.setattr(speedtest, "_client", lambda *a, **k: FakeClient(FakeResponse([])))
    monkeypatch.setattr(speedtest, "_worker", fake_worker)
    source = {"id": 71, "name": "T", "url": "https://example.com/file.bin",
              "size_bytes": 100 * 1024 * 1024}
    settings = {"connections": 2, "warmup": 2, "sample": 10, "conn_timeout": 8,
                "total_timeout": 20}
    result = asyncio.run(speedtest._test_source(source, 1080, {}, settings, max_bytes=100))
    assert result.bytes == 120  # the exhausting worker's actual bytes survive
    assert result.bytes_allowed == 100
    assert result.bytes_over == 20
    assert "超出 20 字节" in (result.message or "")


def test_multi_worker_near_budget_records_actual_allowed_and_over(monkeypatch):
    async def fake_worker(client, url, window, warmup, sample, conn_timeout,
                          total_deadline, max_bytes=None):
        max_bytes.take(60)
        return speedtest.WorkerSample(
            False, bytes=60, traffic_bytes=60, error_type="status", message="HTTP 403")

    monkeypatch.setattr(speedtest, "_client", lambda *a, **k: FakeClient(FakeResponse([])))
    monkeypatch.setattr(speedtest, "_worker", fake_worker)
    source = {"id": 72, "name": "T", "url": "https://example.com/file.bin",
              "size_bytes": 100 * 1024 * 1024}
    settings = {"connections": 2, "warmup": 2, "sample": 10, "conn_timeout": 8,
                "total_timeout": 20}
    result = asyncio.run(speedtest._test_source(source, 1080, {}, settings, max_bytes=100))
    assert result.bytes == 120
    assert result.bytes_allowed == 100
    assert result.bytes_over == 20


def test_run_level_bytes_total_keeps_overage_visible(monkeypatch):
    _, node_id = _node("预算04")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    source = {"id": 81, "name": "A", "url": "https://example.com/a", "region": ""}
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1086}

    async def fake_sources(primary):
        return [source]

    async def fake_confirm(source):
        return False

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append(max_bytes)
        return speedtest.SourceResult(
            source, False, bytes=60 * 1024 * 1024,
            bytes_allowed=50 * 1024 * 1024, bytes_over=10 * 1024 * 1024,
            error_type="status", message="HTTP 403")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    monkeypatch.setattr(speedtest, "_confirm_source_failure", fake_confirm)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert calls == [50 * 1024 * 1024]
    assert run["bytes_total"] == 60 * 1024 * 1024
    assert run["bytes_allowed"] == 50 * 1024 * 1024
    assert run["bytes_over"] == 10 * 1024 * 1024



def test_timeout_charged_bytes_do_not_restore_run_budget(monkeypatch):
    _, node_id = _node("预算05")
    node = db.row("SELECT * FROM nodes WHERE id=?", (node_id,))
    source = {"id": 91, "name": "A", "url": "https://example.com/a", "region": ""}
    calls = []

    class FakeEngine:
        name = "mihomo"
        block_ports = {node["fingerprint"]: 1087}

    async def fake_sources(primary):
        return [source]

    async def fake_confirm(source):
        return False

    async def fake_source(source, port, node, settings, max_bytes=None):
        calls.append(max_bytes)
        # Timeout path: actual bytes are known (10 MB), but the full allowance
        # remains charged so a retry cannot spend the unused 40 MB again.
        return speedtest.SourceResult(
            source, False, bytes=10 * 1024 * 1024,
            bytes_allowed=10 * 1024 * 1024, bytes_over=0,
            charged_bytes=50 * 1024 * 1024, error_type="status",
            message="整轮超时")

    monkeypatch.setattr(engine_mod.engine_manager, "engine", FakeEngine(), raising=False)
    monkeypatch.setattr(speedtest, "_ensure_sources", fake_sources)
    monkeypatch.setattr(speedtest, "_test_source", fake_source)
    monkeypatch.setattr(speedtest, "_confirm_source_failure", fake_confirm)
    run = asyncio.run(speedtest.run_node(node_id, trigger="manual", prepared=True))
    assert calls == [50 * 1024 * 1024]
    assert run["bytes_total"] == 10 * 1024 * 1024
    assert run["bytes_allowed"] == 10 * 1024 * 1024
    assert run["bytes_over"] == 0


def test_next_chunk_races_exhausted_event_and_cancels_slow_read():
    async def scenario():
        budget = speedtest._ByteBudget(5)

        async def slow_chunks():
            await asyncio.sleep(0.2)
            yield b"x" * 10

        async def exhaust_soon():
            await asyncio.sleep(0.01)
            budget.take(5)

        agen = slow_chunks()
        exhaust_task = asyncio.create_task(exhaust_soon())
        chunk, stopped = await speedtest._next_chunk(agen, budget, 0.05)
        exhaust_task.cancel()
        await asyncio.gather(exhaust_task, return_exceptions=True)
        await agen.aclose()
        return chunk, stopped

    chunk, stopped = asyncio.run(scenario())
    assert chunk is None
    assert stopped is True
