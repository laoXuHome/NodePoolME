"""Subscription parsing: share links, base64 blobs and Clash proxy lists -> normalised nodes.

A node is a plain dict:

    {"raw_name", "protocol", "server", "port", "params", "fingerprint", "region"}

`params` keeps only the fields needed to rebuild the transport, for testing and for output.
"""
import base64
import binascii
import hashlib
import json
import re
from urllib.parse import parse_qs, unquote, urlsplit

import yaml

SCHEMES = ("ss://", "ssr://", "vmess://", "vless://", "trojan://", "hysteria://",
           "hysteria2://", "hy2://", "tuic://", "anytls://", "socks5://", "socks://",
           "http://", "https://", "wireguard://", "wg://")

REGION_HINTS = {
    "香港": "HK", "深港": "HK", "沪港": "HK", "京港": "HK", "hk": "HK", "hong kong": "HK", "hongkong": "HK",
    "台湾": "TW", "台北": "TW", "新北": "TW", "tw": "TW", "taiwan": "TW",
    "日本": "JP", "东京": "JP", "大阪": "JP", "埼玉": "JP", "jp": "JP", "japan": "JP", "tokyo": "JP",
    "新加坡": "SG", "狮城": "SG", "sg": "SG", "singapore": "SG",
    "美国": "US", "洛杉矶": "US", "硅谷": "US", "圣何塞": "US", "西雅图": "US", "us": "US", "united states": "US",
    "韩国": "KR", "首尔": "KR", "kr": "KR", "korea": "KR",
    "英国": "GB", "伦敦": "GB", "uk": "GB", "britain": "GB",
    "德国": "DE", "法兰克福": "DE", "de": "DE", "germany": "DE",
    "法国": "FR", "巴黎": "FR", "fr": "FR", "france": "FR",
    "荷兰": "NL", "阿姆斯特丹": "NL", "nl": "NL",
    "俄罗斯": "RU", "莫斯科": "RU", "ru": "RU",
    "加拿大": "CA", "ca": "CA", "canada": "CA",
    "澳大利亚": "AU", "悉尼": "AU", "au": "AU",
    "印度": "IN", "in": "IN", "india": "IN",
    "土耳其": "TR", "tr": "TR",
    "巴西": "BR", "br": "BR",
    "阿根廷": "AR", "ar": "AR",
    "马来西亚": "MY", "my": "MY",
    "泰国": "TH", "th": "TH",
    "越南": "VN", "vn": "VN",
    "菲律宾": "PH", "ph": "PH",
    "印尼": "ID", "印度尼西亚": "ID", "id": "ID",
    "阿联酋": "AE", "迪拜": "AE", "ae": "AE",
    "瑞士": "CH", "ch": "CH",
    "瑞典": "SE", "se": "SE",
    "波兰": "PL", "pl": "PL",
    "意大利": "IT", "it": "IT",
    "西班牙": "ES", "es": "ES",
    "乌克兰": "UA", "ua": "UA",
}

FLAG_RE = re.compile("[" + "".join(chr(0x1F1E6 + i) for i in range(26)) + "]{2}")


def b64decode(data: str) -> str:
    s = data.strip().replace("\n", "").replace("\r", "").replace(" ", "")
    s += "=" * (-len(s) % 4)
    try:
        return base64.urlsafe_b64decode(s).decode("utf-8", "ignore")
    except (binascii.Error, ValueError):
        try:
            return base64.b64decode(s).decode("utf-8", "ignore")
        except (binascii.Error, ValueError):
            return ""


def detect_region(name: str, server: str = "") -> str | None:
    low = (name or "").lower()
    flag = FLAG_RE.search(name or "")
    if flag:
        code = "".join(chr(ord(c) - 0x1F1E6 + 65) for c in flag.group(0))
        return code
    for key, code in REGION_HINTS.items():
        if key in low:
            return code
    return None


def _bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "tls", "reality")


def _int(value, default=0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def fingerprint(protocol: str, server: str, port: int, params: dict) -> str:
    """Content hash for a node (protocol + endpoint + transport params; name ignored).

    This is not a unique row id: the fetcher appends an occurrence suffix when one
    airport publishes several entries with the same content, so no node is merged.
    """
    keys = ("uuid", "password", "cipher", "network", "path", "host", "sni", "serviceName",
            "security", "flow", "publicKey", "shortId", "obfs")
    core = {k: params.get(k) for k in keys if params.get(k) not in (None, "", 0, False)}
    raw = f"{protocol}|{server.lower()}|{port}|{json.dumps(core, sort_keys=True, ensure_ascii=False)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _make(protocol, server, port, params, name, region=None) -> dict | None:
    server = (server or "").strip().strip("[]")
    if not server or not port:
        return None
    node = {
        "raw_name": (name or f"{server}:{port}").strip()[:120],
        "protocol": protocol,
        "server": server,
        "port": int(port),
        "params": params,
    }
    node["fingerprint"] = fingerprint(protocol, server, node["port"], params)
    node["region"] = region or detect_region(node["raw_name"], server)
    return node


def parse_ss(link: str) -> dict | None:
    body, _, name = link[5:].partition("#")
    name = unquote(name)
    body = body.split("?", 1)[0]
    if "@" not in body:
        decoded = b64decode(body)
        if not decoded:
            return None
        body = decoded
    userinfo, _, hostport = body.rpartition("@")
    if ":" not in userinfo:
        decoded_userinfo = b64decode(userinfo)
        if decoded_userinfo and ":" in decoded_userinfo:
            userinfo = decoded_userinfo
    if ":" not in userinfo:
        return None
    cipher, _, password = userinfo.partition(":")
    host, _, port = hostport.rpartition(":")
    params = {"cipher": unquote(cipher), "password": unquote(password)}
    query = parse_qs(urlsplit(link).query)
    if query.get("plugin"):
        plugin = unquote(query["plugin"][0])
        parts = plugin.split(";")
        params["plugin"] = parts[0]
        opts = {}
        for item in parts[1:]:
            key, _, value = item.partition("=")
            opts[key] = value
        if opts:
            params["plugin-opts"] = opts
    return _make("ss", host, _int(port), params, name)


def parse_vmess(link: str) -> dict | None:
    decoded = b64decode(link[8:])
    try:
        data = json.loads(decoded)
    except ValueError:
        return None
    server = str(data.get("add", ""))
    port = _int(data.get("port"))
    network = (data.get("net") or "tcp").lower()
    params = {
        "uuid": data.get("id", ""),
        "alterId": _int(data.get("aid"), 0),
        "cipher": data.get("scy") or "auto",
        "network": network,
        "security": "tls" if _bool(data.get("tls")) else "none",
        "sni": data.get("sni") or data.get("host") or "",
        "skip-cert-verify": True,
    }
    if network == "ws":
        params["path"] = data.get("path") or "/"
        params["host"] = data.get("host") or ""
    elif network == "grpc":
        params["serviceName"] = data.get("path") or ""
        params["host"] = data.get("host") or ""
    elif network == "h2":
        params["path"] = data.get("path") or "/"
        params["host"] = data.get("host") or ""
    if data.get("alpn"):
        params["alpn"] = data.get("alpn")
    return _make("vmess", server, port, params, data.get("ps") or "")


def parse_vless(link: str) -> dict | None:
    parts = urlsplit(link)
    uuid = unquote(parts.username or "")
    server = parts.hostname or ""
    port = _int(parts.port)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    network = (query.get("type") or "tcp").lower()
    security = (query.get("security") or "none").lower()
    params = {
        "uuid": uuid,
        "network": network,
        "security": security,
        "sni": query.get("sni") or query.get("peer") or "",
        "flow": query.get("flow") or "",
        "skip-cert-verify": True,
        "fingerprint": query.get("fp") or "",
    }
    if network in ("ws", "httpupgrade"):
        params["path"] = query.get("path") or "/"
        params["host"] = query.get("host") or ""
    elif network == "grpc":
        params["serviceName"] = query.get("serviceName") or ""
    elif network == "h2":
        params["path"] = query.get("path") or "/"
        params["host"] = query.get("host") or ""
    if security == "reality":
        params["publicKey"] = query.get("pbk") or ""
        params["shortId"] = query.get("sid") or ""
    if query.get("alpn"):
        params["alpn"] = query.get("alpn")
    return _make("vless", server, port, params, unquote(parts.fragment))


def parse_trojan(link: str) -> dict | None:
    parts = urlsplit(link)
    password = unquote(parts.username or "")
    server = parts.hostname or ""
    port = _int(parts.port)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    network = (query.get("type") or "tcp").lower()
    params = {
        "password": password,
        "sni": query.get("sni") or query.get("peer") or "",
        "network": network,
        "skip-cert-verify": _bool(query.get("allowInsecure", "1")),
        "security": "tls",
    }
    if network == "ws":
        params["path"] = query.get("path") or "/"
        params["host"] = query.get("host") or ""
    elif network == "grpc":
        params["serviceName"] = query.get("serviceName") or ""
    if query.get("alpn"):
        params["alpn"] = query.get("alpn")
    return _make("trojan", server, port, params, unquote(parts.fragment))


def parse_hysteria2(link: str) -> dict | None:
    parts = urlsplit(link)
    password = unquote(parts.username or "")
    if not password and parts.password:
        password = unquote(parts.password)
    server = parts.hostname or ""
    port = _int(parts.port)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    params = {
        "password": password,
        "sni": query.get("sni") or "",
        "skip-cert-verify": _bool(query.get("insecure", "1")),
        "obfs": query.get("obfs") or "",
        "obfs-password": query.get("obfs-password") or "",
    }
    return _make("hysteria2", server, port, params, unquote(parts.fragment))


def parse_tuic(link: str) -> dict | None:
    parts = urlsplit(link)
    userinfo = unquote(parts.username or "")
    password = ""
    if parts.password:
        password = unquote(parts.password)
    uuid, sep, pw = userinfo.partition(":")
    if sep:
        password = pw
    server = parts.hostname or ""
    port = _int(parts.port)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    params = {
        "uuid": uuid,
        "password": password,
        "sni": query.get("sni") or "",
        "skip-cert-verify": _bool(query.get("allow_insecure", "1")),
        "congestion-controller": query.get("congestion_control") or "bbr",
        "alpn": (query.get("alpn") or "h3,h2").split(","),
        "udp-relay-mode": query.get("udp_relay_mode") or "native",
    }
    return _make("tuic", server, port, params, unquote(parts.fragment))


def parse_socks(link: str) -> dict | None:
    parts = urlsplit(link)
    server = parts.hostname or ""
    port = _int(parts.port)
    params = {
        "username": unquote(parts.username or ""),
        "password": unquote(parts.password or ""),
        "tls": parts.scheme == "https",
        "skip-cert-verify": True,
    }
    return _make("http" if parts.scheme in ("http", "https") else "socks5", server, port, params,
                 unquote(parts.fragment))


def parse_ssr(link: str) -> dict | None:
    """ssr:// base64(host:port:protocol:method:obfs:base64url(password)/?remarks=...)."""
    decoded = b64decode(link[6:])
    if not decoded:
        return None
    main, _, query = decoded.partition("/?")
    if not query:
        main, _, query = decoded.partition("?")
    fields = main.split(":")
    if len(fields) < 6:
        return None
    host, port, protocol, method, obfs = fields[0], fields[1], fields[2], fields[3], fields[4]
    password_raw = ":".join(fields[5:])
    password = b64decode(password_raw) or password_raw
    q = {k: v[0] for k, v in parse_qs(query).items()} if query else {}
    params = {
        "cipher": method, "password": password, "protocol": protocol,
        "protocol-param": b64decode(q.get("protoparam", "")) or q.get("protoparam", ""),
        "obfs": obfs, "obfs-param": b64decode(q.get("obfsparam", "")) or q.get("obfsparam", ""),
    }
    name = b64decode(q.get("remarks", "")) or q.get("remarks", "")
    return _make("ssr", host, _int(port), params, name)


def parse_hysteria(link: str) -> dict | None:
    """hysteria v1: hysteria://auth@host:port?peer=sni&insecure=1&upmbps=..&downmbps=.."""
    parts = urlsplit(link)
    auth = unquote(parts.username or "") or unquote(parts.password or "")
    server = parts.hostname or ""
    port = _int(parts.port)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    params = {
        "password": auth,
        "sni": query.get("peer") or query.get("sni") or "",
        "skip-cert-verify": _bool(query.get("insecure", "1")),
        "up": query.get("upmbps") or query.get("up") or "50",
        "down": query.get("downmbps") or query.get("down") or "100",
    }
    if query.get("obfs"):
        params["obfs"] = query["obfs"]
    if query.get("alpn"):
        params["alpn"] = query["alpn"]
    return _make("hysteria", server, port, params, unquote(parts.fragment))


def parse_anytls(link: str) -> dict | None:
    parts = urlsplit(link)
    password = unquote(parts.username or "") or unquote(parts.password or "")
    server = parts.hostname or ""
    port = _int(parts.port)
    query = {k: v[0] for k, v in parse_qs(parts.query).items()}
    params = {
        "password": password,
        "sni": query.get("sni") or query.get("peer") or "",
        "skip-cert-verify": _bool(query.get("insecure", "1")),
        "fingerprint": query.get("fp") or "",
    }
    return _make("anytls", server, port, params, unquote(parts.fragment))


PARSERS = {
    "ss": parse_ss, "ssr": parse_ssr, "hysteria": parse_hysteria, "anytls": parse_anytls,
    "vmess": parse_vmess,
    "vless": parse_vless,
    "trojan": parse_trojan,
    "hysteria2": parse_hysteria2,
    "hy2": parse_hysteria2,
    "tuic": parse_tuic,
    "socks5": parse_socks,
    "socks": parse_socks,
    "http": parse_socks,
    "https": parse_socks,
}


def parse_wireguard(link: str) -> dict | None:
    """WireGuard links need a full key pair; accepted only via Clash YAML for now."""
    return None


def parse_link(link: str) -> dict | None:
    link = link.strip()
    if not link or link.startswith("#") or link.startswith("//"):
        return None
    for scheme in SCHEMES:
        if link.lower().startswith(scheme):
            parser = PARSERS.get(scheme[:-3])
            if parser is None:
                return None
            try:
                return parser(link)
            except (ValueError, IndexError, AttributeError):
                return None
    return None


def clash_to_node(proxy: dict) -> dict | None:
    """Import a Clash/Mihomo `proxies:` entry."""
    if not isinstance(proxy, dict):
        return None
    ptype = str(proxy.get("type", "")).lower()
    mapping = {"ss": "ss", "ssr": "ssr", "vmess": "vmess", "vless": "vless",
               "trojan": "trojan", "hysteria": "hysteria", "hysteria2": "hysteria2",
               "tuic": "tuic", "anytls": "anytls", "socks5": "socks5", "http": "http"}
    if ptype not in mapping:
        return None
    params: dict = {}
    for key in ("cipher", "password", "uuid", "alterId", "flow", "sni", "servername",
                "network", "skip-cert-verify", "obfs", "obfs-password", "udp-relay-mode",
                "congestion-controller", "username", "tls", "protocol", "protocol-param",
                "obfs-param", "up", "down", "auth-str", "client-fingerprint", "fingerprint",
                "plugin", "plugin-opts"):
        if key in proxy:
            params[key] = proxy[key]
    if proxy.get("servername") and not params.get("sni"):
        params["sni"] = proxy["servername"]
    if proxy.get("auth-str") and not params.get("password"):
        params["password"] = proxy["auth-str"]
    ws = proxy.get("ws-opts") or {}
    if isinstance(ws, dict):
        params["path"] = ws.get("path") or "/"
        headers = ws.get("headers") or {}
        params["host"] = headers.get("Host") or headers.get("host") or ""
    grpc = proxy.get("grpc-opts") or {}
    if isinstance(grpc, dict):
        params["serviceName"] = grpc.get("grpc-service-name") or ""
    reality = proxy.get("reality-opts") or {}
    if isinstance(reality, dict):
        params["security"] = "reality"
        params["publicKey"] = reality.get("public-key") or ""
        params["shortId"] = reality.get("short-id") or ""
    if proxy.get("tls") or proxy.get("reality-opts"):
        params.setdefault("security", "tls")
    port = _int(proxy.get("port"))
    return _make(mapping[ptype], str(proxy.get("server", "")), port, params,
                 str(proxy.get("name", "")), proxy.get("region"))


def parse_text_full(text: str) -> tuple[list[dict], list[str], dict]:
    """Return (nodes, errors, stats). stats counts what we saw vs what we could use."""
    nodes: list[dict] = []
    errors: list[str] = []
    stats = {"total": 0, "parsed": 0, "unsupported": {}}
    text = (text or "").strip()
    if not text:
        return nodes, ["订阅内容为空"], stats

    if text.startswith("proxies:") or "\nproxies:" in text or text.startswith("proxy-providers:"):
        try:
            doc = yaml.safe_load(text) or {}
        except yaml.YAMLError as exc:
            return nodes, ["YAML 解析失败"], stats
        proxies = doc.get("proxies") if isinstance(doc, dict) else None
        if not isinstance(proxies, list):
            return nodes, ["YAML 中没有 proxies 列表"], stats
        stats["total"] = len(proxies)
        for item in proxies:
            node = clash_to_node(item)
            if node:
                nodes.append(node)
            else:
                kind = str(item.get("type") or "unknown") if isinstance(item, dict) else "unknown"
                stats["unsupported"][kind] = stats["unsupported"].get(kind, 0) + 1
                errors.append(f"不支持的代理类型：{kind}")
        stats["parsed"] = len(nodes)
        return nodes, errors, stats

    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not any(l.lower().startswith("://") or "://" in l for l in lines):
        decoded = b64decode(text)
        if decoded:
            text = decoded
            lines = [l.strip() for l in text.splitlines() if l.strip()]

    for line in lines:
        if line.startswith("//") or line.startswith("#"):
            continue
        if "://" not in line:
            continue
        stats["total"] += 1
        node = parse_link(line)
        if node:
            nodes.append(node)
        else:
            scheme = line.split("://", 1)[0].lower()
            stats["unsupported"][scheme] = stats["unsupported"].get(scheme, 0) + 1
            errors.append(f"不支持的链接：{line[:60]}")
    stats["parsed"] = len(nodes)
    if not nodes and not errors:
        errors.append("没有找到支持的节点")
    return nodes, errors[:10], stats


def parse_text(text: str) -> tuple[list[dict], list[str]]:
    nodes, errors, _ = parse_text_full(text)
    return nodes, errors


# --------------------------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------------------------

def clash_proxy(node: dict, name: str) -> dict:
    p = node["params"]
    t = node["protocol"]
    base = {"name": name, "type": t, "server": node["server"], "port": node["port"],
            "udp": True, "skip-cert-verify": bool(p.get("skip-cert-verify", True))}
    if t == "ssr":
        base.update({"cipher": p.get("cipher") or "rc4-md5", "password": p.get("password", ""),
                     "protocol": p.get("protocol") or "origin",
                     "protocol-param": p.get("protocol-param") or "",
                     "obfs": p.get("obfs") or "plain", "obfs-param": p.get("obfs-param") or ""})
    elif t == "hysteria":
        base.update({"auth-str": p.get("password", ""), "up": p.get("up") or "50",
                     "down": p.get("down") or "100",
                     "sni": p.get("sni") or node["server"]})
    elif t == "anytls":
        base.update({"password": p.get("password", ""), "sni": p.get("sni") or node["server"],
                     "client-fingerprint": p.get("fingerprint") or "chrome"})
    elif t == "ss":
        base.update({"cipher": p.get("cipher") or "aes-128-gcm", "password": p.get("password", "")})
        if p.get("plugin"):
            base["plugin"] = p["plugin"]
            if p.get("plugin-opts"):
                base["plugin-opts"] = p["plugin-opts"]
    elif t == "vmess":
        net = p.get("network") or "tcp"
        if net == "http":
            net = "h2"
        base.update({"uuid": p.get("uuid", ""), "alterId": int(p.get("alterId", 0)),
                     "cipher": p.get("cipher") or "auto", "network": net})
        if p.get("security") == "tls":
            base["tls"] = True
            if p.get("sni"):
                base["servername"] = p["sni"]
            if p.get("alpn"):
                base["alpn"] = p["alpn"] if isinstance(p["alpn"], list) else str(p["alpn"]).split(",")
        if net == "ws":
            base["ws-opts"] = {"path": p.get("path") or "/",
                               "headers": ({"Host": p["host"]} if p.get("host") else {})}
        elif net == "grpc":
            base["grpc-opts"] = {"grpc-service-name": p.get("serviceName") or ""}
        elif net == "h2":
            base["h2-opts"] = {"path": p.get("path") or "/",
                               "host": [p["host"]] if p.get("host") else []}
    elif t == "vless":
        net = p.get("network") or "tcp"
        base.update({"uuid": p.get("uuid", ""), "network": net, "udp": True})
        if net == "ws":
            base["ws-opts"] = {"path": p.get("path") or "/",
                                "headers": ({"Host": p["host"]} if p.get("host") else {})}
        elif net == "grpc":
            base["grpc-opts"] = {"grpc-service-name": p.get("serviceName") or ""}
        elif net == "h2":
            base["h2-opts"] = {"path": p.get("path") or "/",
                               "host": [p["host"]] if p.get("host") else []}
        if p.get("security") == "reality":
            base["tls"] = True
            base["reality-opts"] = {"public-key": p.get("publicKey", ""),
                                    "short-id": p.get("shortId", "")}
            base["client-fingerprint"] = p.get("fingerprint") or "chrome"
        elif p.get("security") == "tls":
            base["tls"] = True
        if p.get("sni"):
            base["servername"] = p["sni"]
        if p.get("flow"):
            base["flow"] = p["flow"]
    elif t == "trojan":
        base.update({"password": p.get("password", ""), "sni": p.get("sni") or node["server"]})
        net = p.get("network") or "tcp"
        if net == "ws":
            base["network"] = "ws"
            base["ws-opts"] = {"path": p.get("path") or "/",
                               "headers": ({"Host": p["host"]} if p.get("host") else {})}
        elif net == "grpc":
            base["network"] = "grpc"
            base["grpc-opts"] = {"grpc-service-name": p.get("serviceName") or ""}
    elif t == "hysteria2":
        base.update({"password": p.get("password", ""), "sni": p.get("sni") or node["server"]})
        if p.get("obfs"):
            base["obfs"] = p["obfs"]
            base["obfs-password"] = p.get("obfs-password") or ""
    elif t == "tuic":
        base.update({"uuid": p.get("uuid", ""), "password": p.get("password", ""),
                     "sni": p.get("sni") or node["server"],
                     "congestion-controller": p.get("congestion-controller") or "bbr",
                     "udp-relay-mode": p.get("udp-relay-mode") or "native",
                     "alpn": p.get("alpn") or ["h3"]})
    elif t in ("http", "socks5"):
        if p.get("username"):
            base["username"] = p["username"]
            base["password"] = p.get("password", "")
        if p.get("tls"):
            base["tls"] = True
    return {k: v for k, v in base.items() if v not in (None, "")}


def singbox_outbound(node: dict, tag: str) -> dict | None:
    p = node["params"]
    t = node["protocol"]
    server = node["server"]
    port = node["port"]
    tls = {"enabled": True, "insecure": True,
           "server_name": p.get("sni") or server}
    if t == "ssr":
        return None   # sing-box has no ShadowsocksR outbound
    if t == "anytls":
        out = {"type": "anytls", "password": p.get("password", ""), "tls": tls}
    elif t == "hysteria":
        out = {"type": "hysteria", "auth_str": p.get("password", ""), "tls": tls,
               "up_mbps": _int(p.get("up"), 50), "down_mbps": _int(p.get("down"), 100)}
    elif t == "ss":
        out = {"type": "shadowsocks", "method": p.get("cipher") or "aes-128-gcm",
               "password": p.get("password", "")}
    elif t == "vmess":
        out = {"type": "vmess", "uuid": p.get("uuid", ""), "security": p.get("cipher") or "auto",
               "alter_id": int(p.get("alterId", 0))}
        if p.get("security") in ("tls", "reality"):
            out["tls"] = tls
    elif t == "vless":
        out = {"type": "vless", "uuid": p.get("uuid", "")}
        if p.get("flow"):
            out["flow"] = p["flow"]
        if p.get("security") in ("tls", "reality"):
            out["tls"] = tls
    elif t == "trojan":
        out = {"type": "trojan", "password": p.get("password", "")}
        out["tls"] = tls
    elif t == "hysteria2":
        out = {"type": "hysteria2", "password": p.get("password", ""), "tls": tls}
        if p.get("obfs"):
            out["obfs"] = {"type": p["obfs"], "password": p.get("obfs-password") or ""}
    elif t == "tuic":
        out = {"type": "tuic", "uuid": p.get("uuid", ""), "password": p.get("password", ""),
               "congestion_control": p.get("congestion-controller") or "bbr", "tls": tls}
    else:
        out = {"type": "socks", "version": "5"}
        if p.get("username"):
            out["username"] = p["username"]
            out["password"] = p.get("password", "")
    net = (p.get("network") or "tcp").lower()
    if t in ("vmess", "vless", "trojan"):
        if net == "ws":
            transport = {"type": "ws", "path": p.get("path") or "/"}
            if p.get("host"):
                transport["headers"] = {"Host": p["host"]}
        elif net == "grpc":
            transport = {"type": "grpc", "service_name": p.get("serviceName") or ""}
        elif net == "h2":
            transport = {"type": "http", "path": p.get("path") or "/"}
            if p.get("host"):
                transport["host"] = [p["host"]]
        elif net in ("httpupgrade",):
            transport = {"type": "httpupgrade", "path": p.get("path") or "/",
                          "host": p.get("host") or ""}
        else:
            transport = None
        if transport:
            out["transport"] = transport
    out.update({"tag": tag, "server": server, "server_port": port})
    return out


def to_uri(node: dict) -> str:
    """Rebuild a share link so the base64 output stays importable."""
    p = node["params"]
    t = node["protocol"]
    name = node.get("display_name") or node["raw_name"]
    server, port = node["server"], node["port"]
    if t == "ss":
        userinfo = base64.urlsafe_b64encode(
            f"{p.get('cipher','aes-128-gcm')}:{p.get('password','')}".encode()
        ).decode().rstrip("=")
        return f"ss://{userinfo}@{server}:{port}#{name}"
    if t == "vmess":
        data = {"v": "2", "ps": name, "add": server, "port": str(port), "id": p.get("uuid", ""),
                "aid": str(p.get("alterId", 0)), "scy": p.get("cipher", "auto"),
                "net": p.get("network", "tcp"), "type": "none", "host": p.get("host", ""),
                "path": p.get("path", ""), "tls": "tls" if p.get("security") == "tls" else "",
                "sni": p.get("sni", "")}
        return "vmess://" + base64.b64encode(
            json.dumps(data, ensure_ascii=False).encode()).decode()
    if t == "vless":
        q = {"type": p.get("network", "tcp"), "security": p.get("security", "none"),
             "sni": p.get("sni", ""), "fp": p.get("fingerprint", "")}
        if p.get("path"):
            q["path"] = p["path"]
        if p.get("host"):
            q["host"] = p["host"]
        if p.get("serviceName"):
            q["serviceName"] = p["serviceName"]
        if p.get("publicKey"):
            q["pbk"] = p["publicKey"]
        if p.get("shortId"):
            q["sid"] = p["shortId"]
        if p.get("flow"):
            q["flow"] = p["flow"]
        query = "&".join(f"{k}={v}" for k, v in q.items() if v)
        return f"vless://{p.get('uuid','')}@{server}:{port}?{query}#{name}"
    if t == "trojan":
        q = {"sni": p.get("sni", ""), "type": p.get("network", "tcp")}
        if p.get("path"):
            q["path"] = p["path"]
        if p.get("host"):
            q["host"] = p["host"]
        query = "&".join(f"{k}={v}" for k, v in q.items() if v)
        return f"trojan://{p.get('password','')}@{server}:{port}?{query}#{name}"
    if t == "ssr":
        body = f"{server}:{port}:{p.get('protocol','origin')}:{p.get('cipher','rc4-md5')}:{p.get('obfs','plain')}"
        pw = base64.urlsafe_b64encode(str(p.get("password", "")).encode()).decode().rstrip("=")
        raw = f"{body}:{pw}/?remarks={name}"
        return "ssr://" + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
    if t == "hysteria":
        q = {"peer": p.get("sni", ""), "insecure": "1",
             "upmbps": p.get("up") or "50", "downmbps": p.get("down") or "100"}
        query = "&".join(f"{k}={v}" for k, v in q.items() if v)
        return f"hysteria://{p.get('password','')}@{server}:{port}?{query}#{name}"
    if t == "anytls":
        q = {"sni": p.get("sni", ""), "insecure": "1"}
        query = "&".join(f"{k}={v}" for k, v in q.items() if v)
        return f"anytls://{p.get('password','')}@{server}:{port}?{query}#{name}"
    if t == "hysteria2":
        q = {"sni": p.get("sni", ""), "insecure": "1"}
        query = "&".join(f"{k}={v}" for k, v in q.items() if v)
        return f"hysteria2://{p.get('password','')}@{server}:{port}?{query}#{name}"
    if t == "tuic":
        return f"tuic://{p.get('uuid','')}:{p.get('password','')}@{server}:{port}#{name}"
    return f"{t}://{server}:{port}#{name}"
