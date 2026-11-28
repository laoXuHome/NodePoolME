"""Layered DNS → TCP×5 → TLS → HTTPS probes.

Direct probes are used only to establish whether the target itself is reachable
from the controller.  Node probes travel through the node's local SOCKS listener
exposed by the shared Mihomo engine, so a result describes the node rather than
the controller's own network.

Every stage records its own timeout and the whole target has a hard 20 second
deadline.  A failed or unknown stage prevents later stages from running; those
later stages are still emitted as explicit ``unexecuted`` rows.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import socket
import ssl
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.database import db

DNS_TIMEOUT = 3.0
DNS_NORMAL_MS = 200
DNS_ABNORMAL_MS = 1000
TCP_ATTEMPTS = 5
TCP_TIMEOUT = 3.0
TCP_INTERVAL = 1.0
TCP_NORMAL_MS = 500
TCP_OBSERVE_MS = 800
TLS_TIMEOUT = 5.0
TLS_NORMAL_MS = 500
TLS_ABNORMAL_MS = 1000
HTTPS_TIMEOUT = 8.0
HTTPS_NORMAL_MS = 3000
TARGET_DEADLINE = 20.0
EXPECTED_HTTPS = range(200, 400)


class ProxyUnavailable(OSError):
    """The node's local SOCKS listener is not available."""


class ProxyError(OSError):
    """SOCKS protocol/handshake failure, responsibility not attributable to the target."""


class SocksTargetError(OSError):
    """SOCKS proxy reached the target but the target connection was refused/failed."""

    def __init__(self, reply: int):
        self.reply = int(reply)
        super().__init__(f"socks_connect_failed:{self.reply}")


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def p80_nearest_rank(values: list[int | float]) -> int | None:
    """Nearest-rank P80 (not interpolation, median, mean or max).

    For the mandated five samples, rank = ceil(0.8 × 5) = 4 and the fourth
    value after ascending sort is returned.
    """
    clean = sorted(int(v) for v in values if v is not None)
    if not clean:
        return None
    rank = math.ceil(0.8 * len(clean))
    rank = min(len(clean), max(1, rank))
    return clean[rank - 1]


def tcp_grade(p80_ms: int | None) -> dict:
    if p80_ms is None:
        return {"key": "none", "label": "无有效样本", "color": "info"}
    if p80_ms <= 150:
        return {"key": "excellent", "label": "优秀", "color": "success"}
    if p80_ms <= 250:
        return {"key": "good", "label": "良好", "color": "primary"}
    if p80_ms <= 400:
        return {"key": "normal", "label": "正常", "color": "info"}
    if p80_ms <= 500:
        return {"key": "fair", "label": "较差", "color": "warning"}
    if p80_ms <= 800:
        return {"key": "severe", "label": "严重", "color": "danger"}
    return {"key": "fail", "label": "不合格", "color": "danger"}


def _stage(stage: str, *, executed: bool, status: str, latency_ms: int | None = None,
           ok: bool = False, slow: bool = False, detail: Any = None) -> dict:
    return {"stage": stage, "executed": bool(executed), "ok": bool(ok),
            "status": status, "latency_ms": latency_ms, "slow": bool(slow),
            "detail": detail}


def _unexecuted(stage: str, reason: str = "前一阶段未通过，未执行") -> dict:
    return _stage(stage, executed=False, status="unexecuted", detail={"reason": reason})


def _elapsed_ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


async def _dns(host: str, port: int, timeout: float) -> tuple[dict, list[str], str | None]:
    started = time.perf_counter()
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
            timeout=max(0.05, timeout),
        )
        latency = _elapsed_ms(started)
        addresses = []
        for info in infos:
            addr = info[4][0]
            if addr not in addresses:
                addresses.append(addr)
        if not addresses:
            raise OSError("empty DNS result")
        return (_stage("dns", executed=True, status="success", ok=True,
                       latency_ms=latency, slow=latency > DNS_ABNORMAL_MS,
                       detail={"address": addresses[0], "addresses": addresses[:3],
                               "normal_ms": DNS_NORMAL_MS, "abnormal_ms": DNS_ABNORMAL_MS}),
                addresses, None)
    except asyncio.TimeoutError:
        return (_stage("dns", executed=True, status="unknown", latency_ms=_elapsed_ms(started),
                       detail={"reason": "dns_timeout", "timeout_seconds": timeout}),
                [], "dns_timeout")
    except Exception as exc:  # DNS responsibility is ambiguous without target evidence
        return (_stage("dns", executed=True, status="unknown", latency_ms=_elapsed_ms(started),
                       detail={"reason": "dns_error", "error": f"{type(exc).__name__}: {exc}"}),
                [], "dns_error")


async def _direct_connect(host: str, port: int, timeout: float):
    started = time.perf_counter()
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    return reader, writer, _elapsed_ms(started)


def _socks_request(host: str, port: int) -> bytes:
    try:
        packed = socket.inet_aton(host)
        return b"\x05\x01\x00\x01" + packed + int(port).to_bytes(2, "big")
    except OSError:
        name = host.encode("idna")[:255]
        return b"\x05\x01\x00\x03" + len(name).to_bytes(1, "big") + name + int(port).to_bytes(2, "big")


async def _socks_open(proxy_port: int, host: str, port: int, timeout: float):
    """Open a SOCKS5 CONNECT and return ``(reader, writer, latency_ms, reply)``.

    The domain is always sent as ATYP=0x03 (or as an IPv4 literal only when the
    caller already supplied an IP).  The proxy therefore performs the DNS
    lookup; this module never resolves the target on the controller for a node
    probe.
    """
    started = time.perf_counter()
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", int(proxy_port)), timeout
        )
    except asyncio.TimeoutError as exc:
        raise ProxyUnavailable("proxy_connect_timeout") from exc
    except OSError as exc:
        raise ProxyUnavailable(f"proxy_unavailable:{type(exc).__name__}: {exc}") from exc
    try:
        writer.write(b"\x05\x01\x00")
        greet = await asyncio.wait_for(reader.readexactly(2), timeout)
        if greet != b"\x05\x00":
            raise ProxyError(f"socks_method_rejected:{greet!r}")
        writer.write(_socks_request(host, port))
        head = await asyncio.wait_for(reader.readexactly(4), timeout)
        if head[0] != 5:
            raise ProxyError(f"socks_bad_version:{head[0]}")
        reply = int(head[1])
        atyp = head[3]
        if atyp == 1:
            await asyncio.wait_for(reader.readexactly(4 + 2), timeout)
        elif atyp == 4:
            await asyncio.wait_for(reader.readexactly(16 + 2), timeout)
        elif atyp == 3:
            length = await asyncio.wait_for(reader.readexactly(1), timeout)
            await asyncio.wait_for(reader.readexactly(int(length[0]) + 2), timeout)
        else:
            raise ProxyError(f"socks_bad_atyp:{atyp}")
        return reader, writer, _elapsed_ms(started), reply
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(Exception):
            writer.close()
        raise ProxyError("socks_timeout") from exc
    except ProxyError:
        with contextlib.suppress(Exception):
            writer.close()
        raise
    except OSError as exc:
        with contextlib.suppress(Exception):
            writer.close()
        raise ProxyError(f"socks_io_error:{type(exc).__name__}: {exc}") from exc


async def _socks_connect(proxy_port: int, host: str, port: int, timeout: float):
    reader, writer, latency, reply = await _socks_open(proxy_port, host, port, timeout)
    if reply != 0:
        with contextlib.suppress(Exception):
            writer.close()
        raise SocksTargetError(reply)
    return reader, writer, latency


async def _proxy_dns(proxy_port: int, host: str, port: int, timeout: float) -> tuple[dict, str | None]:
    """Confirm domain resolution through the node's SOCKS listener.

    SOCKS5 has no resolve-only command, so a separate domain CONNECT is used as
    the resolution witness.  Only a successful CONNECT proves that the node-side
    proxy resolved the hostname and reached the target.  A refusal (0x05) is not
    a resolution proof because a proxy may reject the request by policy before
    resolving; host unreachable and all other replies remain Unknown.  The
    controller's ``getaddrinfo`` is never consulted.
    """
    started = time.perf_counter()
    writer = None
    try:
        reader, writer, latency, reply = await _socks_open(proxy_port, host, port, timeout)
        with contextlib.suppress(Exception):
            writer.close()
        if reply == 0:
            return (_stage("dns", executed=True, status="success", ok=True,
                           latency_ms=latency, slow=latency > DNS_ABNORMAL_MS,
                           detail={"resolver": "node_proxy", "via": "socks5_domain",
                                   "reply": reply, "dns_result": "resolved",
                                   "normal_ms": DNS_NORMAL_MS,
                                   "abnormal_ms": DNS_ABNORMAL_MS}), None)
        if reply == 5:
            return (_stage("dns", executed=True, status="unknown",
                           latency_ms=_elapsed_ms(started),
                           detail={"resolver": "node_proxy", "via": "socks5_domain",
                                   "reason": "proxy_dns_target_refused", "reply": reply}),
                    "proxy_dns_target_refused")
        if reply == 4:
            return (_stage("dns", executed=True, status="unknown",
                           latency_ms=_elapsed_ms(started),
                           detail={"resolver": "node_proxy", "via": "socks5_domain",
                                   "reason": "proxy_dns_host_unreachable", "reply": reply}),
                    "proxy_dns_host_unreachable")
        return (_stage("dns", executed=True, status="unknown",
                       latency_ms=_elapsed_ms(started),
                       detail={"resolver": "node_proxy", "via": "socks5_domain",
                               "reason": "proxy_dns_error", "reply": reply}),
                "proxy_dns_error")
    except ProxyUnavailable as exc:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        return (_stage("dns", executed=True, status="unknown", latency_ms=_elapsed_ms(started),
                       detail={"resolver": "node_proxy", "reason": "proxy_unavailable",
                               "error": str(exc)}), "proxy_unavailable")
    except ProxyError as exc:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        return (_stage("dns", executed=True, status="unknown", latency_ms=_elapsed_ms(started),
                       detail={"resolver": "node_proxy", "reason": "proxy_error",
                               "error": str(exc)}), "proxy_error")
    except asyncio.TimeoutError:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()
        return (_stage("dns", executed=True, status="unknown", latency_ms=_elapsed_ms(started),
                       detail={"resolver": "node_proxy", "reason": "dns_timeout"}), "dns_timeout")


async def _tcp_samples(*, host: str, port: int, timeout: float, deadline: float,
                       proxy_port: int | None, strict_proxy: bool = False
                       ) -> tuple[list[dict], dict]:
    samples: list[dict] = []
    for idx in range(TCP_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            samples.append({"index": idx, "ok": False, "latency_ms": None,
                            "error": "deadline_exceeded", "sampled_at": now_stamp()})
            continue
        attempt_timeout = min(TCP_TIMEOUT, remaining)
        started = time.perf_counter()
        writer = None
        try:
            if proxy_port is None:
                _reader, writer, latency = await _direct_connect(host, port, attempt_timeout)
            else:
                _reader, writer, latency = await _socks_connect(
                    proxy_port, host, port, attempt_timeout
                )
            samples.append({"index": idx, "ok": True, "latency_ms": latency,
                            "error": None, "sampled_at": now_stamp()})
        except asyncio.TimeoutError:
            samples.append({"index": idx, "ok": False, "latency_ms": None,
                            "error": "timeout", "error_kind": "ambiguous",
                            "sampled_at": now_stamp(),
                            "elapsed_ms": _elapsed_ms(started)})
        except (ProxyUnavailable, ProxyError) as exc:
            samples.append({"index": idx, "ok": False, "latency_ms": None,
                            "error": f"{type(exc).__name__}: {exc}", "error_kind": "proxy",
                            "sampled_at": now_stamp(),
                            "elapsed_ms": _elapsed_ms(started)})
        except SocksTargetError as exc:
            samples.append({"index": idx, "ok": False, "latency_ms": None,
                            "error": f"{type(exc).__name__}: {exc}", "error_kind": "target",
                            "sampled_at": now_stamp(),
                            "elapsed_ms": _elapsed_ms(started)})
        except (ConnectionRefusedError, OSError) as exc:
            samples.append({"index": idx, "ok": False, "latency_ms": None,
                            "error": f"{type(exc).__name__}: {exc}", "error_kind": "target",
                            "sampled_at": now_stamp(),
                            "elapsed_ms": _elapsed_ms(started)})
        finally:
            if writer is not None:
                with contextlib.suppress(Exception):
                    writer.close()
        if idx < TCP_ATTEMPTS - 1:
            remaining = deadline - time.monotonic()
            if remaining <= 1.05:
                for rest in range(idx + 1, TCP_ATTEMPTS):
                    samples.append({"index": rest, "ok": False, "latency_ms": None,
                                    "error": "deadline_exceeded", "sampled_at": now_stamp()})
                break
            await asyncio.sleep(TCP_INTERVAL)
    valid = [s["latency_ms"] for s in samples if s["ok"] and s["latency_ms"] is not None]
    p80 = p80_nearest_rank(valid)
    detail = {"samples": samples, "valid_count": len(valid), "p80_ms": p80,
              "method": "nearest_rank", "rank": math.ceil(0.8 * len(valid)) if valid else None}
    if not valid:
        errors = [s["error"] or "" for s in samples]
        kinds = {s.get("error_kind") for s in samples}
        if all("deadline" in e for e in errors):
            status, reason = "unknown", "tcp_all_failed"
        elif strict_proxy and kinds <= {"proxy", "ambiguous"}:
            status = "unknown"
            reason = "proxy_unavailable" if all(
                s.get("error_kind") == "proxy" and "ProxyUnavailable" in (s.get("error") or "")
                for s in samples
            ) else "proxy_error"
        else:
            status, reason = "failure", "tcp_all_failed"
        return samples, _stage("tcp", executed=True, status=status,
                               detail={**detail, "reason": reason})
    return samples, _stage("tcp", executed=True, status="success", ok=True, latency_ms=p80,
                           slow=p80 > TCP_OBSERVE_MS, detail=detail)


async def _open_tls(*, host: str, port: int, timeout: float,
                    proxy_port: int | None) -> tuple[asyncio.StreamReader,
                                                     asyncio.StreamWriter, int]:
    context = ssl.create_default_context()
    if proxy_port is None:
        started = time.perf_counter()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=context, server_hostname=host), timeout
        )
        return reader, writer, _elapsed_ms(started)
    reader, writer, _ = await _socks_connect(proxy_port, host, port, timeout)
    started = time.perf_counter()
    await asyncio.wait_for(writer.start_tls(context, server_hostname=host), timeout)
    return reader, writer, _elapsed_ms(started)


def _cert_detail(writer: asyncio.StreamWriter) -> dict:
    try:
        ssl_object = writer.get_extra_info("ssl_object")
        cert = ssl_object.getpeercert() if ssl_object else None
        if not cert:
            return {"certificate": "unavailable"}
        return {"certificate": "valid", "not_after": cert.get("notAfter"),
                "subject": cert.get("subject"), "issuer": cert.get("issuer")}
    except Exception as exc:
        return {"certificate": "error", "error": f"{type(exc).__name__}: {exc}"}


async def _tls_https(*, target: dict, timeout: float, deadline: float,
                     proxy_port: int | None, strict_proxy: bool = False
                     ) -> tuple[dict, dict]:
    writer = None
    tls_stage = None
    try:
        remaining = deadline - time.monotonic()
        tls_timeout = min(TLS_TIMEOUT, remaining)
        if tls_timeout <= 0.05:
            return (_stage("tls", executed=True, status="unknown",
                           detail={"reason": "deadline_exceeded"}),
                    _unexecuted("https", "总 deadline 已耗尽"))
        reader, writer, tls_ms = await _open_tls(
            host=target["host"], port=int(target.get("port") or 443),
            timeout=tls_timeout, proxy_port=proxy_port,
        )
        tls_stage = _stage("tls", executed=True, status="success", ok=True, latency_ms=tls_ms,
                           slow=tls_ms > TLS_ABNORMAL_MS,
                           detail={"normal_ms": TLS_NORMAL_MS, "abnormal_ms": TLS_ABNORMAL_MS,
                                   **_cert_detail(writer)})
        remaining = deadline - time.monotonic()
        https_timeout = min(HTTPS_TIMEOUT, remaining)
        if https_timeout <= 0.05:
            return tls_stage, _unexecuted("https", "总 deadline 已耗尽")
        started = time.perf_counter()
        host_header = target["host"]
        if int(target.get("port") or 443) != 443:
            host_header += f":{int(target['port'])}"
        path = target.get("path") or "/"
        writer.write(
            (f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\n"
             f"User-Agent: {db.get('fetch_ua', 'clash-verge/v1.19.6')}\r\n"
             "Accept: */*\r\nConnection: close\r\n\r\n").encode("latin-1", "ignore")
        )
        status_line = await asyncio.wait_for(reader.readline(), https_timeout)
        latency = _elapsed_ms(started)
        parts = status_line.decode("latin-1", "replace").strip().split()
        if len(parts) < 2 or not parts[1].isdigit():
            return tls_stage, _stage("https", executed=True, status="failure",
                                     latency_ms=latency, ok=False,
                                     detail={"reason": "invalid_status_line",
                                             "line": status_line.decode("latin-1", "replace")[:120]})
        status = int(parts[1])
        headers = {}
        for _ in range(30):
            line = await asyncio.wait_for(reader.readline(), max(0.05, https_timeout - latency / 1000))
            if not line or line in (b"\r\n", b"\n"):
                break
            key, _, value = line.decode("latin-1", "replace").partition(":")
            if key:
                headers[key.strip().lower()] = value.strip()
        expected = target.get("expected") or EXPECTED_HTTPS
        ok = status in expected
        expected_text = target.get("expected_status") or "200-399"
        return tls_stage, _stage(
            "https", executed=True, status="success" if ok else "failure",
            latency_ms=latency, ok=ok, slow=latency > HTTPS_NORMAL_MS,
            detail={"status": status, "expected": expected_text, "normal_ms": HTTPS_NORMAL_MS,
                    "headers": {k: headers.get(k) for k in ("content-type", "content-length",
                                                            "location") if k in headers}},
        )
    except (ProxyUnavailable, ProxyError) as exc:
        if strict_proxy:
            reason = "proxy_unavailable" if isinstance(exc, ProxyUnavailable) else "proxy_error"
            return (_stage("tls", executed=True, status="unknown",
                           detail={"reason": reason,
                                   "error": f"{type(exc).__name__}: {exc}"}),
                   _unexecuted("https", "节点代理出口不可用"))
        return (_stage("tls", executed=True, status="failure",
                       detail={"reason": "tls_proxy_error",
                               "error": f"{type(exc).__name__}: {exc}"}),
                _unexecuted("https"))
    except SocksTargetError as exc:
        return (_stage("tls", executed=True, status="failure",
                       detail={"reason": "tls_connect_failed", "reply": exc.reply}),
                _unexecuted("https"))
    except asyncio.TimeoutError:
        if tls_stage is not None:
            return (tls_stage, _stage("https", executed=True, status="unknown",
                                      detail={"reason": "https_timeout"}))
        return (_stage("tls", executed=True, status="failure",
                       detail={"reason": "tls_timeout"}), _unexecuted("https"))
    except ssl.SSLError as exc:
        return (_stage("tls", executed=True, status="failure",
                       detail={"reason": "tls_error", "error": f"{type(exc).__name__}: {exc}"}),
                _unexecuted("https"))
    except Exception as exc:
        if strict_proxy:
            return (_stage("tls", executed=True, status="unknown",
                           detail={"reason": "detector_error",
                                   "error": f"{type(exc).__name__}: {exc}"}),
                   _unexecuted("https", "检测器内部异常，责任方不明"))
        return (_stage("tls", executed=True, status="failure",
                       detail={"reason": "tls_unknown", "error": f"{type(exc).__name__}: {exc}"}),
                _unexecuted("https"))
    finally:
        if writer is not None:
            with contextlib.suppress(Exception):
                writer.close()


def _combine(target: dict, dns_stage: dict, tcp_stage: dict,
             tls_stage: dict, https_stage: dict, samples: list[dict],
             started: float, p80: int | None, *, proxy: bool) -> dict:
    stages = [dns_stage, tcp_stage, tls_stage, https_stage]
    availability = all(s["executed"] and s["status"] == "success" for s in stages)
    statuses = [s["status"] for s in stages if s["executed"]]
    if "failure" in statuses:
        status = "failure"
        failing = next(s for s in stages if s["status"] == "failure")
        reason = (failing.get("detail") or {}).get("reason") or f"{failing['stage']}_failed"
    elif "unknown" in statuses:
        status = "unknown"
        failing = next(s for s in stages if s["status"] == "unknown")
        reason = (failing.get("detail") or {}).get("reason") or f"{failing['stage']}_unknown"
    elif availability:
        status = "success"
        reason = "tcp_p80_observe" if p80 and p80 > TCP_NORMAL_MS else None
        if p80 and p80 > TCP_OBSERVE_MS:
            status = "failure"
            reason = "tcp_p80_exceeded"
    else:
        status = "unknown"
        reason = "incomplete_stages"
    health_pass = bool(status == "success" and p80 is not None and p80 <= TCP_NORMAL_MS
                       and availability)
    finished = datetime.now()
    duration_ms = int((time.monotonic() - started) * 1000)
    started_at = (finished - timedelta(milliseconds=duration_ms)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    return {
        "target_id": int(target["id"]),
        "target_name": target["name"],
        "target_url": target["url"],
        "status": status,
        "reason": reason,
        "p80_ms": p80,
        "tcp_valid_count": len([s for s in samples if s["ok"] and s["latency_ms"] is not None]),
        "tls_ms": tls_stage.get("latency_ms"),
        "https_ms": https_stage.get("latency_ms"),
        "https_status": (https_stage.get("detail") or {}).get("status"),
        "health_pass": health_pass,
        "available": availability,
        "stages": stages,
        "tcp_samples": samples,
        "started_at": started_at,
        "finished_at": finished.strftime("%Y-%m-%d %H:%M:%S"),
        "duration_ms": duration_ms,
        "proxy": proxy,
        "p80_grade": tcp_grade(p80),
    }


async def probe_target(target: dict, *, proxy_port: int | None = None,
                       deadline_seconds: float = TARGET_DEADLINE,
                       allow_http: bool = False, proxy_dns: bool = False) -> dict:
    """Run one target probe, direct or through the node's SOCKS listener.

    ``proxy_dns=True`` is the P3 mode: DNS is confirmed by the node's SOCKS
    listener and the original hostname (never a controller-resolved IP) is sent
    through the same listener for TCP/TLS/HTTPS.  The default keeps P2's
    controller-DNS behavior unchanged.
    """
    started = time.monotonic()
    deadline = started + deadline_seconds
    host = target.get("host") or ""
    port = int(target.get("port") or (443 if target.get("secure") else 80))
    if proxy_dns:
        if proxy_port is None:
            dns_stage = _stage("dns", executed=True, status="unknown", latency_ms=0,
                               detail={"resolver": "node_proxy",
                                       "reason": "proxy_dns_requires_node_port"})
            return _combine(target, dns_stage, _unexecuted("tcp"), _unexecuted("tls"),
                            _unexecuted("https"), [], started, None, proxy=False)
        dns_stage, _dns_error = await _proxy_dns(
            int(proxy_port), host, port, min(DNS_TIMEOUT, deadline - started)
        )
        addresses = []
    else:
        dns_stage, addresses, _dns_error = await _dns(
            host, port, min(DNS_TIMEOUT, deadline - started)
        )
    if not dns_stage["ok"]:
        return _combine(target, dns_stage, _unexecuted("tcp"), _unexecuted("tls"),
                        _unexecuted("https"), [], started, None,
                        proxy=proxy_port is not None)
    # P3 always sends the hostname to the node proxy so the proxy performs DNS.
    # P2 retains its existing resolved-IPv4 TCP behavior.
    if proxy_dns:
        tcp_host = host
    else:
        ipv4 = next((a for a in addresses if ":" not in a), addresses[0])
        tcp_host = ipv4 if ":" not in ipv4 else host
    samples, tcp_stage = await _tcp_samples(
        host=tcp_host, port=port, timeout=TCP_TIMEOUT,
        deadline=deadline, proxy_port=proxy_port, strict_proxy=proxy_dns,
    )
    if not tcp_stage.get("ok"):
        return _combine(target, dns_stage, tcp_stage, _unexecuted("tls"),
                        _unexecuted("https"), samples, started, None,
                        proxy=proxy_port is not None)
    tls_stage, https_stage = await _tls_https(
        target=target, timeout=TLS_TIMEOUT, deadline=deadline, proxy_port=proxy_port,
        strict_proxy=proxy_dns,
    )
    return _combine(target, dns_stage, tcp_stage, tls_stage, https_stage, samples,
                    started, (tcp_stage.get("detail") or {}).get("p80_ms"),
                    proxy=proxy_port is not None)


async def probe_direct(target: dict, deadline_seconds: float = TARGET_DEADLINE) -> dict:
    return await probe_target(target, proxy_port=None, deadline_seconds=deadline_seconds)
