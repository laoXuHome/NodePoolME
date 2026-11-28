"""P3 node-exit DNS and proxy/TLS attribution tests."""
from __future__ import annotations

import asyncio
import ssl

import pytest

from backend.health import prober


TARGET = {
    "id": 1, "name": "ChatGPT", "url": "https://chatgpt.com/",
    "host": "chatgpt.com", "port": 443, "secure": True,
    "path": "/", "expected": range(200, 400), "expected_status": "200-399",
}


def _proxy_reply(code: int = 0) -> bytes:
    return bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0])


async def _start_proxy(handler):
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    port = int(server.sockets[0].getsockname()[1])
    return server, port


def test_proxy_dns_uses_socks_domain_not_local_resolver(monkeypatch):
    requested = {}
    local_calls = {"count": 0}

    async def handle(reader, writer):
        try:
            greet = await reader.readexactly(3)
            assert greet == b"\x05\x01\x00"
            writer.write(b"\x05\x00")
            head = await reader.readexactly(4)
            assert head[0] == 5 and head[3] == 3, "DNS must be sent as SOCKS domain ATYP"
            length = await reader.readexactly(1)
            requested["host"] = (await reader.readexactly(length[0])).decode()
            requested["port"] = int.from_bytes(await reader.readexactly(2), "big")
            writer.write(_proxy_reply(0))
            await writer.drain()
        finally:
            writer.close()

    async def fake_local_dns(*args, **kwargs):
        local_calls["count"] += 1
        raise AssertionError("P3 must not use controller getaddrinfo for target DNS")

    monkeypatch.setattr(prober, "_dns", fake_local_dns)

    async def run():
        server, port = await _start_proxy(handle)
        try:
            stage, reason = await prober._proxy_dns(port, "chatgpt.com", 443, 3)
            assert reason is None
            assert stage["status"] == "success" and stage["ok"] is True
            assert stage["detail"]["resolver"] == "node_proxy"
            assert stage["detail"]["via"] == "socks5_domain"
            assert "address" not in stage["detail"]
            assert requested == {"host": "chatgpt.com", "port": 443}
            assert local_calls["count"] == 0
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_proxy_dns_target_refused_is_unknown_not_success(monkeypatch):
    async def handle(reader, writer):
        try:
            await reader.readexactly(3)
            writer.write(b"\x05\x00")
            await reader.readexactly(4)
            length = await reader.readexactly(1)
            await reader.readexactly(length[0] + 2)
            writer.write(_proxy_reply(5))
            await writer.drain()
        finally:
            writer.close()

    async def run():
        server, port = await _start_proxy(handle)
        try:
            stage, reason = await prober._proxy_dns(port, "chatgpt.com", 443, 3)
            assert stage["status"] == "unknown" and stage["ok"] is False
            assert reason == "proxy_dns_target_refused"
            assert stage["detail"]["reply"] == 5
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_proxy_dns_host_unreachable_is_unknown(monkeypatch):
    async def handle(reader, writer):
        try:
            await reader.readexactly(3)
            writer.write(b"\x05\x00")
            await reader.readexactly(4)
            length = await reader.readexactly(1)
            await reader.readexactly(length[0] + 2)
            writer.write(_proxy_reply(4))
            await writer.drain()
        finally:
            writer.close()

    async def run():
        server, port = await _start_proxy(handle)
        try:
            stage, reason = await prober._proxy_dns(port, "chatgpt.com", 443, 3)
            assert stage["status"] == "unknown" and stage["ok"] is False
            assert reason == "proxy_dns_host_unreachable"
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())


def test_proxy_dns_port_unavailable_is_unknown(monkeypatch):
    stage, reason = asyncio.run(prober._proxy_dns(9, "chatgpt.com", 443, 1))
    assert stage["status"] == "unknown"
    assert reason == "proxy_unavailable"


def test_p3_probe_keeps_hostname_and_same_proxy_for_all_stages(monkeypatch):
    seen = {"tcp": {}, "tls": {}, "local_dns": 0}

    async def fake_proxy_dns(proxy_port, host, port, timeout):
        seen["dns"] = {"proxy_port": proxy_port, "host": host, "port": port}
        return prober._stage("dns", executed=True, status="success", ok=True,
                             latency_ms=8,
                             detail={"resolver": "node_proxy", "via": "socks5_domain"}), None

    async def fake_local_dns(*args, **kwargs):
        seen["local_dns"] += 1
        raise AssertionError("node probe must not call controller DNS")

    async def fake_tcp(**kwargs):
        seen["tcp"] = kwargs
        return ([{"index": i, "ok": True, "latency_ms": 100}
                 for i in range(5)],
                {"stage": "tcp", "executed": True, "status": "success", "ok": True,
                 "latency_ms": 100, "detail": {"p80_ms": 100}})

    async def fake_tls(**kwargs):
        seen["tls"] = kwargs
        return ({"stage": "tls", "executed": True, "status": "success", "ok": True,
                 "latency_ms": 50, "detail": {}},
                {"stage": "https", "executed": True, "status": "success", "ok": True,
                 "latency_ms": 60, "detail": {"status": 200}})

    monkeypatch.setattr(prober, "_proxy_dns", fake_proxy_dns)
    monkeypatch.setattr(prober, "_dns", fake_local_dns)
    monkeypatch.setattr(prober, "_tcp_samples", fake_tcp)
    monkeypatch.setattr(prober, "_tls_https", fake_tls)

    result = asyncio.run(prober.probe_target(TARGET, proxy_port=31001, proxy_dns=True))
    assert result["status"] == "success"
    assert seen["dns"] == {"proxy_port": 31001, "host": "chatgpt.com", "port": 443}
    assert seen["local_dns"] == 0
    assert seen["tcp"]["host"] == "chatgpt.com"
    assert seen["tcp"]["proxy_port"] == 31001
    assert seen["tcp"]["strict_proxy"] is True
    assert seen["tls"]["proxy_port"] == 31001
    assert seen["tls"]["strict_proxy"] is True


def test_tls_timeout_and_ssl_error_are_failure_but_detector_errors_unknown(monkeypatch):
    async def run(exc):
        async def boom(**kwargs):
            raise exc
        monkeypatch.setattr(prober, "_open_tls", boom)
        return await prober._tls_https(target=TARGET, timeout=5, deadline=10**9,
                                       proxy_port=31001, strict_proxy=True)

    tls, https = asyncio.run(run(asyncio.TimeoutError()))
    assert tls["status"] == "failure"
    assert tls["detail"]["reason"] == "tls_timeout"

    tls, https = asyncio.run(run(ssl.SSLError("handshake failed")))
    assert tls["status"] == "failure"
    assert tls["detail"]["reason"] == "tls_error"

    tls, https = asyncio.run(run(prober.ProxyUnavailable("proxy refused")))
    assert tls["status"] == "unknown"
    assert tls["detail"]["reason"] == "proxy_unavailable"

    tls, https = asyncio.run(run(prober.ProxyError("socks method rejected")))
    assert tls["status"] == "unknown"
    assert tls["detail"]["reason"] == "proxy_error"

    tls, https = asyncio.run(run(RuntimeError("detector bug")))
    assert tls["status"] == "unknown"
    assert tls["detail"]["reason"] == "detector_error"


def test_proxy_unavailable_tcp_is_unknown_in_strict_p3_mode(monkeypatch):
    async def boom(*args, **kwargs):
        raise prober.ProxyUnavailable("proxy refused")

    monkeypatch.setattr(prober, "_socks_connect", boom)
    monkeypatch.setattr(prober, "TCP_INTERVAL", 0)

    async def run(strict):
        return await prober._tcp_samples(host="chatgpt.com", port=443, timeout=1,
                                         deadline=10**9, proxy_port=31001,
                                         strict_proxy=strict)

    samples, strict_stage = asyncio.run(run(True))
    assert strict_stage["status"] == "unknown"
    assert strict_stage["detail"]["reason"] == "proxy_unavailable"
    assert all(s.get("error_kind") == "proxy" for s in samples)

    samples, p2_stage = asyncio.run(run(False))
    assert p2_stage["status"] == "failure"
    assert p2_stage["detail"]["reason"] == "tcp_all_failed"


def test_p3_service_entry_forces_proxy_dns(monkeypatch):
    from backend.business import service

    seen = {}

    async def fake_probe(target, *, proxy_port, deadline_seconds=20, proxy_dns=False):
        seen.update(proxy_port=proxy_port, proxy_dns=proxy_dns,
                    deadline_seconds=deadline_seconds)
        return {"status": "unknown", "stages": [], "tcp_samples": []}

    monkeypatch.setattr(prober, "probe_target", fake_probe)
    asyncio.run(service.node_probe(TARGET, proxy_port=31001))
    assert seen == {"proxy_port": 31001, "proxy_dns": True, "deadline_seconds": 20}
