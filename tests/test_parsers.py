"""Offline checks for parsers and subscription output. Run: python -m pytest tests/ or directly."""
import base64
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.parsers import subscription as sp  # noqa: E402

SS = "ss://YWVzLTI1Ni1nY206cGFzc3dvcmQ@1.2.3.4:8388#香港01"
VMESS = "vmess://" + base64.b64encode(json.dumps({
    "v": "2", "ps": "日本02", "add": "5.6.7.8", "port": "443", "id": "11111111-2222-3333-4444-555555555555",
    "aid": "0", "net": "ws", "host": "cdn.example.com", "path": "/ray", "tls": "tls", "sni": "cdn.example.com",
}).encode()).decode()
VLESS = ("vless://aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee@9.9.9.9:443?type=ws&security=reality&pbk=pubkey"
         "&sid=ab12&sni=www.microsoft.com&path=%2Fws&host=www.microsoft.com&fp=chrome#新加坡01")
TROJAN = "trojan://secret@10.0.0.1:443?sni=trojan.example.com&type=ws&path=/t#美国01"
HY2 = "hysteria2://pw123@11.11.11.11:8443?sni=hy.example.com&insecure=1#韩国01"
TUUIC = "tuic://uuid-x:pw-x@12.12.12.12:443?sni=tuic.example.com#英国01"

CLASH = """
proxies:
  - name: 香港02
    type: ss
    server: 13.13.13.13
    port: 8388
    cipher: aes-128-gcm
    password: pw
  - name: 台湾01
    type: vmess
    server: 14.14.14.14
    port: 443
    uuid: 11111111-2222-3333-4444-555555555555
    alterId: 0
    cipher: auto
    tls: true
    network: ws
    ws-opts:
      path: /ws
      headers:
        Host: cdn.example.com
"""


def test_links():
    nodes, errors = sp.parse_text("\n".join([SS, VMESS, VLESS, TROJAN, HY2, TUUIC]))
    assert len(nodes) == 6, (len(nodes), errors)
    protocols = sorted(n["protocol"] for n in nodes)
    assert protocols == ["hysteria2", "ss", "trojan", "tuic", "vless", "vmess"], protocols
    regions = [n["region"] for n in nodes]
    assert regions[:2] == ["HK", "JP"], regions
    vless = next(n for n in nodes if n["protocol"] == "vless")
    assert vless["params"]["security"] == "reality"
    assert vless["params"]["publicKey"] == "pubkey"
    assert vless["params"]["path"] == "/ws"
    hy = next(n for n in nodes if n["protocol"] == "hysteria2")
    assert hy["params"]["password"] == "pw123"


def test_base64_blob():
    blob = base64.b64encode((SS + "\n" + TROJAN).encode()).decode()
    nodes, errors = sp.parse_text(blob)
    assert len(nodes) == 2, errors


def test_clash_yaml():
    nodes, errors = sp.parse_text(CLASH)
    assert len(nodes) == 2, errors
    vmess = next(n for n in nodes if n["protocol"] == "vmess")
    assert vmess["params"]["path"] == "/ws"
    assert vmess["params"]["host"] == "cdn.example.com"


def test_dedup_fingerprint():
    a = sp.parse_link(SS)
    b = sp.parse_link(SS.replace("#香港01", "#香港改名"))
    assert a["fingerprint"] == b["fingerprint"]
    c = sp.parse_link(SS.replace("1.2.3.4", "1.2.3.5"))
    assert a["fingerprint"] != c["fingerprint"]


def test_outputs_roundtrip():
    node = sp.parse_link(VLESS)
    name = "机场A.G.TG.Y 新加坡01"
    clash = sp.clash_proxy(node, name)
    assert clash["type"] == "vless" and clash["reality-opts"]["public-key"] == "pubkey"
    sb = sp.singbox_outbound(node, name)
    assert sb["type"] == "vless" and sb["transport"]["type"] == "ws"
    uri = sp.to_uri(node)
    assert sp.parse_link(uri)["fingerprint"] == node["fingerprint"]


def test_status_match():
    from backend.testers.engine import status_matches
    assert status_matches(204, "204")
    assert status_matches(200, "200-399")
    assert not status_matches(500, "200-299")
    assert status_matches(302, "200,302")


if __name__ == "__main__":
    for fn in [v for k, v in list(globals().items()) if k.startswith("test_")]:
        fn()
        print("ok:", fn.__name__)
