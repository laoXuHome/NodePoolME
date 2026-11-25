"""Loon-specific subscription: only emit what Loon can parse, with safe escaping."""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.subscriptions import builder  # noqa: E402


def node(kind, port=443, name="测试节点", server="node.example.com", **params):
    return {"protocol": kind, "server": server, "port": port,
            "display_name": name, "params": params}


def test_loon_filters_unupported_protocols_and_transports():
    nodes = [
        node("ss", cipher="aes-256-gcm", password="pw"),
        node("hysteria", password="pw"),
        node("tuic", uuid="u", password="pw"),
        node("vmess", network="grpc", uuid="u"),
        node("vless", network="httpupgrade", uuid="u"),
        node("trojan", port=2, password="pw"),
        node("ss", cipher="2022-blake3-chacha20-poly1305", password="pw"),
        node("ss", cipher="chacha20-poly1305", password="pw"),
    ]
    kept, skipped = builder.loon_plan(nodes)
    assert len(kept) == 2
    assert skipped == {"protocol": 2, "transport": 2, "port": 1, "cipher": 1}
    text = builder.build_loon(nodes)
    assert "hysteria://" not in text and "tuic://" not in text
    assert "grpc" not in text and "httpupgrade" not in text
    assert "2022-blake3-chacha20-poly1305" not in text


def test_loon_emits_supported_protocols():
    nodes = [
        node("ss", cipher="2022-blake3-aes-256-gcm", password="pw"),
        node("ssr", cipher="aes-256-cfb", password="pw", protocol="origin", obfs="plain"),
        node("vmess", network="ws", uuid="u", security="tls", sni="s.example.com"),
        node("vless", network="ws", uuid="u", security="reality", publicKey="pk",
             shortId="sid", flow="xtls-rprx-vision"),
        node("trojan", network="ws", password="pw", sni="s.example.com"),
        node("hysteria2", password="pw", sni="s.example.com"),
        node("anytls", password="pw", sni="s.example.com"),
    ]
    text = builder.build_loon(nodes)
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == len(nodes)
    assert all(ln.startswith(builder.LOON_URI_SCHEMES) for ln in lines)


def test_loon_escapes_credentials_and_fragment():
    n = node("trojan", password="p@ss#1&x", name="机场A#1 节点", sni="s.example.com")
    line = builder.build_loon([n]).splitlines()[-1]
    assert "p%40ss%231%26x@" in line
    assert line.endswith("#机场A%231%20节点")


def test_loon_vless_query_is_encoded():
    n = node("vless", uuid="u", network="ws", security="tls", sni="s.example.com",
             path="/ws?ed=2048", host="cdn.example.com")
    line = builder.build_loon([n]).splitlines()[-1]
    assert "path=/ws%3Fed%3D2048" in line
    assert "host=cdn.example.com" in line


def test_loon_ss_plugin_and_cipher_alias():
    plain = node("ss", cipher="chacha20-poly1305", password="pw")
    line = builder.build_loon([plain]).splitlines()[-1]
    userinfo = line[5:].split("@")[0]
    assert base64.urlsafe_b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode() == \
        "chacha20-ietf-poly1305:pw"

    obfs = node("ss", cipher="aes-128-gcm", password="pw", plugin="obfs-local",
                **{"plugin-opts": {"mode": "http", "host": "cdn.example.com"}})
    line = builder.build_loon([obfs]).splitlines()[-1]
    assert "plugin=obfs-local%3Bobfs%3Dhttp%3Bobfs-host%3Dcdn.example.com" in line


def test_loon_vmess_json_is_valid():
    n = node("vmess", uuid="u", network="ws", security="tls", sni="s.example.com",
             host="cdn.example.com", path="/ws")
    line = builder.build_loon([n]).splitlines()[-1]
    data = __import__("json").loads(base64.b64decode(line[8:]).decode())
    assert data["net"] == "ws" and data["tls"] == "tls"
    assert data["sni"] == "s.example.com" and data["ps"] == "测试节点"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok:", name)
