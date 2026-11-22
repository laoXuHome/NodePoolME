"""Node testing.

Two engines share one interface:

* ``mihomo`` — the bundled Mihomo/Clash core is started once and reloaded per working block.
  Every node gets its own SOCKS listener bound to that proxy, so tests observe real HTTP status
  codes and response bodies through the node (not just a TCP handshake).
* ``tcp`` — connectivity only: TCP handshake to server:port. Used when the core binary is missing.
"""
import asyncio
import os
import random
import shutil
import time
from dataclasses import dataclass
from typing import Iterable

import httpx

from backend import config
from backend.database import db
from backend.parsers import subscription as parser


@dataclass
class Outcome:
    ok: bool
    latency_ms: int | None = None
    error_type: str | None = None
    message: str | None = None


class BaseEngine:
    name = "base"

    async def start(self) -> None:  # pragma: no cover - no-op for tcp engine
        return None

    async def stop(self) -> None:
        return None

    async def prepare(self, nodes: Iterable[dict]) -> None:
        return None

    async def test(self, node: dict, group: dict, proxy: dict | None = None) -> Outcome:
        raise NotImplementedError


class TcpEngine(BaseEngine):
    """Fallback: only proves that the endpoint accepts TCP connections."""

    name = "tcp"

    async def test(self, node: dict, group: dict, proxy: dict | None = None) -> Outcome:
        started = time.perf_counter()
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(node["server"], node["port"]),
                timeout=float(group.get("timeout") or 8),
            )
            latency = int((time.perf_counter() - started) * 1000)
            return Outcome(True, latency, None, "TCP 握手成功（仅检查连通性）")
        except asyncio.TimeoutError:
            return Outcome(False, None, "timeout", "TCP 连接超时")
        except OSError as exc:
            return Outcome(False, None, "connect", f"TCP 连接失败：{exc}"[:300])
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass


class MihomoEngine(BaseEngine):
    name = "mihomo"

    def __init__(self) -> None:
        self.proc: asyncio.subprocess.Process | None = None
        self.workdir = str(config.CONFIG_DIR / "mihomo")
        # mihomo only reloads configs inside its home (-d) directory
        self.cfg_path = os.path.join(self.workdir, "config.yaml")
        self.block_ports: dict[str, int] = {}
        self._clients: dict[int, httpx.AsyncClient] = {}
        self._lock = asyncio.Lock()
        self._reload_count = 0
        self._block_signature = ""

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        if not shutil.which(config.MIHOMO_BIN) and not os.path.exists(config.MIHOMO_BIN):
            raise RuntimeError(f"未找到 mihomo 内核：{config.MIHOMO_BIN}")
        os.makedirs(self.workdir, exist_ok=True)
        self._write_config([])
        self.proc = await asyncio.create_subprocess_exec(
            config.MIHOMO_BIN, "-d", self.workdir, "-f", self.cfg_path,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        for _ in range(60):
            if await self._api_alive():
                db.append_log("INFO", "测试内核", "Mihomo 内核已就绪")
                return
            if self.proc.returncode is not None:
                raise RuntimeError(f"mihomo 进程异常退出，代码 {self.proc.returncode}")
            await asyncio.sleep(0.25)
        raise RuntimeError("mihomo 内核启动后未就绪")

    async def stop(self) -> None:
        for client in self._clients.values():
            try:
                await client.aclose()
            except Exception:
                pass
        self._clients.clear()
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()

    async def _api_alive(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=1.0) as c:
                r = await c.get(f"{config.MIHOMO_API}/version")
                return r.status_code == 200
        except Exception:
            return False

    # ---------------------------------------------------------------- config
    def _listener_port(self, index: int) -> int:
        return config.MIHOMO_BASE_PORT + index

    def _write_config(self, nodes: list[dict]) -> str:
        proxies = []
        listeners = []
        for idx, node in enumerate(nodes):
            tag = f"n{idx}"
            proxies.append(parser.clash_proxy(node, tag))
            listeners.append({
                "name": f"np-in-{idx}",
                "type": "socks",
                "listen": "127.0.0.1",
                "port": self._listener_port(idx),
                "proxy": tag,
            })
        if not proxies:  # mihomo refuses an empty proxies list
            proxies.append({"name": "np-placeholder", "type": "direct", "udp": True})
        import yaml

        doc = {
            "mode": "global",
            "log-level": "silent",
            "ipv6": False,
            "external-controller": config.MIHOMO_API.replace("http://", ""),
            "unified-delay": True,
            "tcp-concurrent": True,
            "profile": {"store-selected": False, "store-fake-ip": False},
            "proxies": proxies,
            "listeners": listeners,
            "rules": ["MATCH,DIRECT"],
        }
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False)
        os.replace(tmp, self.cfg_path)
        return self.cfg_path

    async def prepare(self, nodes: list[dict]) -> None:
        async with self._lock:
            signature = ",".join(n["fingerprint"] for n in nodes)
            if signature == self._block_signature:
                return
            self._write_config(nodes)
            for client in self._clients.values():
                try:
                    await client.aclose()
                except Exception:
                    pass
            self._clients.clear()
            await self._reload()
            self.block_ports = {node["fingerprint"]: self._listener_port(i)
                                for i, node in enumerate(nodes)}
            self._block_signature = signature
            self._reload_count += 1

    async def _reload(self) -> None:
        payload = {"path": self.cfg_path, "force": True}
        last_error = ""
        for _ in range(20):
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.put(f"{config.MIHOMO_API}/configs", json=payload)
                    if r.status_code in (200, 204):
                        await asyncio.sleep(0.35)
                        return
                    last_error = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as exc:  # core may be restarting
                last_error = str(exc)
            await asyncio.sleep(0.5)
        raise RuntimeError(f"mihomo 配置重载失败：{last_error}")

    # ---------------------------------------------------------------- testing
    def _client(self, port: int, timeout: float) -> httpx.AsyncClient:
        client = self._clients.get(port)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                proxy=f"socks5://127.0.0.1:{port}",
                timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
                verify=False,
                follow_redirects=False,
                trust_env=False,
                headers={"User-Agent": config.HTTP_UA, "Accept": "*/*"},
            )
            self._clients[port] = client
        return client

    async def test(self, node: dict, group: dict, proxy: dict | None = None) -> Outcome:
        port = self.block_ports.get(node["fingerprint"])
        if port is None:
            return Outcome(False, None, "engine", "节点未绑定检测端口")
        timeout = float(group.get("timeout") or 8)
        client = self._client(port, timeout)
        method = (group.get("method") or "GET").upper()
        started = time.perf_counter()
        try:
            resp = await client.request(method, group["url"])
            latency = int((time.perf_counter() - started) * 1000)
        except httpx.TimeoutException:
            return Outcome(False, None, "timeout", f"{timeout:g} 秒内无响应")
        except httpx.HTTPError as exc:
            return Outcome(False, None, "connect", f"代理或传输错误：{type(exc).__name__}: {exc}"[:300])
        except Exception as exc:  # defensive: never let a test crash a batch
            return Outcome(False, None, "engine", f"检测异常：{type(exc).__name__}: {exc}"[:300])

        body = ""
        if group.get("expect_body"):
            try:
                body = resp.text[:200000]
            except Exception:
                body = ""
        ok_status = status_matches(resp.status_code, group.get("expect_status") or "200-399")
        if not ok_status:
            return Outcome(False, latency, "status",
                           f"状态码 {resp.status_code} 不在允许范围 {group.get('expect_status')}")
        if group.get("expect_body") and group["expect_body"] not in body:
            return Outcome(False, latency, "content", "响应内容未包含预期文本")
        max_latency = int(group.get("max_latency") or 0)
        if max_latency and latency > max_latency:
            return Outcome(False, latency, "slow", f"延迟 {latency}ms 超过上限 {max_latency}ms")
        return Outcome(True, latency, None, None)


def status_matches(status: int, spec: str) -> bool:
    spec = (spec or "").replace(" ", "")
    for part in spec.split(","):
        if not part:
            continue
        if "-" in part:
            low, _, high = part.partition("-")
            if low.isdigit() and high.isdigit() and int(low) <= status <= int(high):
                return True
        elif part.isdigit() and int(part) == status:
            return True
    return False


class EngineManager:
    """Keeps the active engine and falls back to TCP when the core is unavailable."""

    def __init__(self) -> None:
        self.engine: BaseEngine | None = None
        self.ready = False
        self.error: str | None = None

    async def start(self) -> None:
        if config.TEST_ENGINE == "tcp":
            self.engine = TcpEngine()
        else:
            candidate = MihomoEngine()
            try:
                await candidate.start()
                self.engine = candidate
            except Exception as exc:
                self.error = str(exc)
                db.append_log("WARN", "测试内核", f"Mihomo 不可用（{exc}），改用 TCP 引擎")
                self.engine = TcpEngine()
        self.ready = True

    async def stop(self) -> None:
        if self.engine:
            await self.engine.stop()

    @property
    def name(self) -> str:
        return self.engine.name if self.engine else "none"

    async def prepare(self, nodes: list[dict]) -> None:
        for node in nodes:
            normalize_node(node)
        if self.engine:
            await self.engine.prepare(nodes)

    async def test(self, node: dict, group: dict) -> Outcome:
        normalize_node(node)
        if not self.engine:
            return Outcome(False, None, "engine", "检测内核尚未启动")
        return await self.engine.test(node, group)


engine_manager = EngineManager()


def normalize_node(node: dict) -> dict:
    """params may arrive as a JSON string straight from SQLite rows."""
    params = node.get("params")
    if isinstance(params, str):
        node["params"] = db.loads(params, {}) if params.strip() else {}
    elif params is None:
        node["params"] = {}
    return node


def proxied(node: dict) -> bool:
    """Nodes reachable only through a proxy are all of them; kept for future direct checks."""
    return node.get("protocol") not in ("direct",)


def jitter_ms() -> float:
    lo = db.get_int("request_interval_min_ms", 200)
    hi = db.get_int("request_interval_max_ms", 800)
    lo, hi = max(0, lo), max(0, hi)
    if hi <= lo:
        return lo / 1000
    return random.uniform(lo, hi) / 1000
