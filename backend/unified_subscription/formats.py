"""P6.2.2 client-format adapters.

All four outputs receive the exact same ordered node collection and the exact
same ``display_name``.  Format support is checked before generation; nodes that
cannot be represented by every client are excluded from the unified output set
instead of being silently dropped by one format.
"""
from __future__ import annotations

import base64
import json
from collections import Counter

import yaml

from backend.parsers import subscription as parser
from backend.subscriptions import builder

SUPPORTED_CLASH = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
                   "tuic", "anytls", "http", "socks5"}
SUPPORTED_SINGBOX = {"ss", "vmess", "vless", "trojan", "hysteria", "hysteria2",
                     "tuic", "anytls", "http", "socks5"}
SUPPORTED_BASE64 = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
                    "tuic", "anytls", "http", "socks5"}

FORMAT_KEYS = ("clash", "singbox", "base64", "loon")
FORMAT_LABELS = {
    "clash": "Clash / Mihomo",
    "singbox": "sing-box",
    "base64": "通用 Base64",
    "loon": "Loon",
}
FORMAT_FILENAMES = {
    "clash": "nodepool-unified.yaml",
    "singbox": "nodepool-unified.json",
    "base64": "nodepool-unified.base64.txt",
    "loon": "nodepool-unified.loon.txt",
}
FORMAT_MEDIA = {
    "clash": "application/yaml; charset=utf-8",
    "singbox": "application/json; charset=utf-8",
    "base64": "text/plain; charset=utf-8",
    "loon": "text/plain; charset=utf-8",
}


def _reason(code: str, label: str) -> dict:
    return {"code": code, "label": label}


def unsupported_reasons(node: dict) -> dict:
    """Return {format: reason} for formats that cannot faithfully express this node."""
    proto = str(node.get("protocol") or "").lower()
    reasons: dict[str, dict] = {}
    try:
        port = int(node.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    if not node.get("server") or not (1 <= port <= 65535):
        reason = _reason("endpoint", "服务器地址或端口无效")
        return {fmt: reason for fmt in FORMAT_KEYS}
    if proto not in SUPPORTED_CLASH:
        reasons["clash"] = _reason("protocol", f"Clash 不支持协议 {proto or 'unknown'}")
    if proto not in SUPPORTED_SINGBOX:
        reasons["singbox"] = _reason("protocol", f"sing-box 不支持协议 {proto or 'unknown'}")
    if proto not in SUPPORTED_BASE64:
        reasons["base64"] = _reason("protocol", f"Base64 不支持协议 {proto or 'unknown'}")
    loon_reason = builder._loon_skip_reason(node)
    if loon_reason:
        labels = {
            "port": "Loon 端口无效",
            "protocol": f"Loon 不支持协议 {proto or 'unknown'}",
            "transport": "Loon 不支持该传输方式",
            "cipher": "Loon 不支持该 Shadowsocks 加密方式",
            "plugin": "Loon 不支持该 Shadowsocks 插件",
        }
        reasons["loon"] = _reason(loon_reason, labels.get(loon_reason, loon_reason))
    return reasons


def can_output(node: dict) -> bool:
    return not unsupported_reasons(node)


def _tls_params(node: dict) -> dict:
    p = node.get("params") or {}
    return {
        "sni": p.get("sni") or node.get("server"),
        "alpn": p.get("alpn"),
        "fingerprint": p.get("fingerprint"),
        "skip": bool(p.get("skip-cert-verify") or p.get("insecure")),
    }


def _clash_proxy(node: dict) -> dict:
    proxy = parser.clash_proxy(node, node["display_name"])
    p = node.get("params") or {}
    t = str(node.get("protocol") or "").lower()
    # The shared parser is authoritative; fill only representation fields it leaves
    # out so ALPN/fingerprint/reality are not lost in the unified export.
    if p.get("alpn") and "alpn" not in proxy:
        proxy["alpn"] = p["alpn"] if isinstance(p["alpn"], list) else str(p["alpn"]).split(",")
    if t in ("vless", "trojan") and p.get("sni"):
        proxy.setdefault("servername", p["sni"])
    if p.get("fingerprint") and t in ("vless", "anytls"):
        proxy.setdefault("client-fingerprint", p["fingerprint"])
    if t in ("hysteria", "hysteria2") and p.get("up") and p.get("down"):
        proxy.setdefault("up", str(p["up"]))
        proxy.setdefault("down", str(p["down"]))
    return proxy


def build_clash(nodes: list[dict], sub_name: str) -> str:
    proxies = [_clash_proxy(n) for n in nodes]
    names = [n["display_name"] for n in nodes]
    doc = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "proxies": proxies,
        "proxy-groups": [
            {"name": sub_name, "type": "select",
             "proxies": (["自动选择"] + names) if names else ["DIRECT"]},
            {"name": "自动选择", "type": "url-test",
             "url": "https://www.gstatic.com/generate_204",
             "interval": 300, "tolerance": 50, "proxies": names or ["DIRECT"]},
        ],
        "rules": ["MATCH," + sub_name],
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def _singbox_tls(node: dict) -> dict:
    t = _tls_params(node)
    tls = {"enabled": True, "insecure": t["skip"], "server_name": t["sni"]}
    if t["alpn"]:
        tls["alpn"] = t["alpn"] if isinstance(t["alpn"], list) else str(t["alpn"]).split(",")
    return tls


def _singbox_outbound(node: dict) -> dict:
    t = str(node.get("protocol") or "").lower()
    p = node.get("params") or {}
    if t == "http":
        out = {"type": "http", "server": node["server"], "server_port": node["port"],
               "tag": node["display_name"]}
        if p.get("username"):
            out["username"] = p["username"]
            out["password"] = p.get("password", "")
        if p.get("tls"):
            out["tls"] = _singbox_tls(node)
        return out
    if t == "socks5":
        out = {"type": "socks", "server": node["server"], "server_port": node["port"],
               "tag": node["display_name"], "version": "5"}
        if p.get("username"):
            out["username"] = p["username"]
            out["password"] = p.get("password", "")
        return out
    out = parser.singbox_outbound(node, node["display_name"])
    if not out:
        raise ValueError(f"sing-box 无法表达协议 {t}")
    if t in ("vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "anytls"):
        out["tls"] = _singbox_tls(node)
    if t == "vless" and str(p.get("security") or "").lower() == "reality":
        out["tls"] = _singbox_tls(node)
        out["reality"] = {"enabled": True, "public_key": p.get("publicKey", ""),
                          "short_id": p.get("shortId", "")}
        if p.get("fingerprint"):
            out["utls"] = {"enabled": True, "fingerprint": p["fingerprint"]}
    if t in ("vmess", "vless", "trojan"):
        net = str(p.get("network") or "tcp").lower()
        if net == "ws":
            transport = {"type": "ws", "path": p.get("path") or "/"}
            if p.get("host"):
                transport["headers"] = {"Host": p["host"]}
            out["transport"] = transport
        elif net == "grpc":
            out["transport"] = {"type": "grpc", "service_name": p.get("serviceName") or ""}
        elif net == "h2":
            transport = {"type": "http", "path": p.get("path") or "/"}
            if p.get("host"):
                transport["host"] = [p["host"]]
            out["transport"] = transport
    return out


def build_singbox(nodes: list[dict], sub_name: str) -> str:
    outbounds = [_singbox_outbound(n) for n in nodes]
    tags = [o["tag"] for o in outbounds]
    selector = {"type": "selector", "tag": sub_name,
                "outbounds": tags + (["direct"] if tags else []),
                "default": tags[0] if tags else "direct"}
    urltest = {"type": "urltest", "tag": "自动选择", "outbounds": tags or ["direct"],
               "url": "https://www.gstatic.com/generate_204",
               "interval": "5m", "tolerance": 50}
    doc = {
        "log": {"level": "info", "timestamp": True},
        "dns": {"servers": [{"tag": "google", "address": "tls://8.8.8.8"}],
                "strategy": "prefer_ipv4"},
        "inbounds": [{"type": "mixed", "tag": "mixed-in", "listen": "0.0.0.0",
                      "listen_port": 7890}],
        "outbounds": [*outbounds, selector, urltest,
                      {"type": "direct", "tag": "direct"},
                      {"type": "block", "tag": "block"}],
        "route": {"rules": [], "final": sub_name, "auto_detect_interface": True},
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def build_base64(nodes: list[dict]) -> str:
    links = []
    for node in nodes:
        # URI fragments must escape "#"/space; otherwise a deduplicated name
        # such as "node #2" is parsed by clients as just "2".  VMess keeps
        # its name inside the base64 JSON payload, and SSR inside remarks.
        if str(node.get("protocol") or "").lower() not in ("vmess", "ssr"):
            node = {**node, "display_name": builder._frag(node.get("display_name") or "")}
        links.append(parser.to_uri(node))
    return base64.b64encode("\n".join(links).encode()).decode()


def build_loon(nodes: list[dict]) -> str:
    return builder.build_loon(nodes)


def build(fmt: str, nodes: list[dict], sub_name: str) -> str:
    if fmt == "clash":
        return build_clash(nodes, sub_name)
    if fmt == "singbox":
        return build_singbox(nodes, sub_name)
    if fmt == "base64":
        return build_base64(nodes)
    if fmt == "loon":
        return build_loon(nodes)
    raise ValueError(f"未知统一订阅格式：{fmt}")


def validate(fmt: str, content: str, nodes: list[dict]) -> None:
    """Structural and node-count validation for the unified export."""
    if fmt == "clash":
        doc = yaml.safe_load(content)
        proxies = (doc or {}).get("proxies")
        if not isinstance(proxies, list) or len(proxies) != len(nodes):
            raise ValueError("Clash proxies 数量与统一节点集合不一致")
        names = {p.get("name") for p in proxies}
        if names != {n["display_name"] for n in nodes}:
            raise ValueError("Clash 节点展示名称与统一集合不一致")
    elif fmt == "singbox":
        doc = json.loads(content)
        outbounds = doc.get("outbounds") or []
        proxies = [o for o in outbounds
                   if o.get("type") not in ("selector", "urltest", "direct", "block")]
        if len(proxies) != len(nodes):
            raise ValueError("sing-box 节点 outbounds 数量与统一节点集合不一致")
        if {o.get("tag") for o in proxies} != {n["display_name"] for n in nodes}:
            raise ValueError("sing-box 节点标签与统一集合不一致")
    elif fmt == "base64":
        from urllib.parse import unquote

        decoded = base64.b64decode(content).decode() if content else ""
        lines = [ln for ln in decoded.splitlines() if ln.strip()]
        if len(lines) != len(nodes):
            raise ValueError("Base64 节点行数与统一节点集合不一致")
        for line, node in zip(lines, nodes):
            # Keep the whole fragment.  A deduplicated display name may itself
            # contain "#2", so splitting from the right would truncate it.
            name = unquote(line.split("#", 1)[1]) if "#" in line else ""
            if name and name != node["display_name"]:
                raise ValueError("Base64 节点名称与统一集合不一致")
    elif fmt == "loon":
        lines = [ln.strip() for ln in content.splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if len(lines) != len(nodes):
            raise ValueError("Loon 节点行数与统一节点集合不一致")
        if any(not ln.startswith(builder.LOON_URI_SCHEMES) for ln in lines):
            raise ValueError("Loon 输出包含无法识别的节点行")
    else:
        raise ValueError(f"未知统一订阅格式：{fmt}")


def unsupported_summary(nodes: list[dict]) -> dict:
    counts: Counter = Counter()
    details = []
    for node in nodes:
        reasons = unsupported_reasons(node)
        if reasons:
            for fmt, reason in reasons.items():
                counts[f"{fmt}:{reason['code']}"] += 1
            details.append({
                "node_id": node.get("node_id"),
                "node_name": node.get("node_name"),
                "protocol": node.get("protocol"),
                "formats": reasons,
            })
    return {"count": len(details), "reasons": dict(counts), "nodes": details}
