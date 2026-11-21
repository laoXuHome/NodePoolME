"""Extra subscription formats: ssr, hysteria v1, anytls, Clash yaml types, parse diagnostics."""
import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.parsers import subscription as p  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def test_ssr_link():
    pw = _b64("secret")
    name = _b64("HK 01")
    raw = f"5.6.7.8:8388:auth_aes128_md5:aes-256-cfb:tls1.2_ticket_auth:{pw}/?remarks={name}&obfsparam="
    node = p.parse_link("ssr://" + _b64(raw))
    assert node and node["protocol"] == "ssr", node
    assert node["server"] == "5.6.7.8" and node["port"] == 8388
    assert node["params"]["password"] == "secret"
    assert node["raw_name"] == "HK 01"
    clash = p.clash_proxy(node, "x")
    assert clash["type"] == "ssr" and clash["protocol"] == "auth_aes128_md5"
    assert p.singbox_outbound(node, "x") is None      # no sing-box equivalent


def test_hysteria_v1_link():
    node = p.parse_link("hysteria://pa%24sword@9.9.9.9:443?peer=cdn.example.com&insecure=1&upmbps=30&downmbps=80#US")
    assert node and node["protocol"] == "hysteria", node
    assert node["params"]["sni"] == "cdn.example.com"
    assert node["params"]["up"] == "30" and node["params"]["down"] == "80"
    clash = p.clash_proxy(node, "x")
    assert clash["type"] == "hysteria" and clash["auth-str"] == "pa$sword"
    assert p.singbox_outbound(node, "x")["type"] == "hysteria"


def test_anytls_link():
    node = p.parse_link("anytls://pw@2.2.2.2:8443?sni=a.example.com&insecure=1#JP")
    assert node and node["protocol"] == "anytls", node
    assert p.clash_proxy(node, "x")["type"] == "anytls"
    assert p.singbox_outbound(node, "x")["type"] == "anytls"


def test_clash_extra_types():
    yaml_text = """
proxies:
  - {name: A, type: ssr, server: 1.1.1.1, port: 443, cipher: aes-256-cfb, password: pw,
     protocol: origin, obfs: plain}
  - {name: B, type: anytls, server: 2.2.2.2, port: 443, password: pw, sni: b.com}
  - {name: C, type: wireguard, server: 3.3.3.3, port: 51820}
  - {name: D, type: vmess, server: 4.4.4.4, port: 443, uuid: u, alterId: 0, network: http,
     tls: true, ws-opts: {path: /p, headers: {Host: h.com}}}
"""
    nodes, errors, stats = p.parse_text_full(yaml_text)
    kinds = {n["raw_name"]: n["protocol"] for n in nodes}
    assert kinds["A"] == "ssr" and kinds["B"] == "anytls" and kinds["D"] == "vmess", kinds
    assert stats["total"] == 4 and stats["parsed"] == 3
    assert stats["unsupported"] == {"wireguard": 1}, stats


def test_parse_stats_for_links():
    text = "\n".join([
        "ss://" + _b64("aes-128-gcm:pw") + "@1.1.1.1:8388#one",
        "wireguard://abc@2.2.2.2:51820#wg",
        "ssr://" + _b64("3.3.3.3:8388:origin:aes-256-cfb:plain:" + _b64("pw") + "/?remarks=tw"),
    ])
    nodes, errors, stats = p.parse_text_full(text)
    assert len(nodes) == 2, nodes
    assert stats["total"] == 3 and stats["parsed"] == 2
    assert stats["unsupported"] == {"wireguard": 1}, stats
    assert any("wireguard" in e for e in errors)


def test_singbox_skips_ssr_only_nodes():
    nodes = [dict(raw_name="x", display_name="x", protocol="ssr", server="1.1.1.1", port=1,
                  params={"cipher": "aes-256-cfb", "password": "p", "protocol": "origin",
                          "obfs": "plain"})]
    import json
    doc = json.loads(builder.build_singbox(nodes, "sub"))
    assert all(o.get("type") != "ssr" for o in doc["outbounds"])


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
