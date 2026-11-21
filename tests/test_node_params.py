"""Regression: SQLite rows carry params as JSON text; engine/parser must decode them."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.parsers import subscription as parser  # noqa: E402
from backend.testers import engine as engine_mod  # noqa: E402


def make_node(protocol: str, params: dict) -> dict:
    return {
        "id": 1, "airport_id": 1, "raw_name": "node-1", "protocol": protocol,
        "server": "1.2.3.4", "port": 443, "fingerprint": "fp-1",
        "params": json.dumps(params, ensure_ascii=False),
    }


CASES = [
    ("ss", {"cipher": "aes-128-gcm", "password": "pw"}),
    ("vmess", {"uuid": "11111111-2222-3333-4444-555555555555", "network": "ws"}),
    ("vless", {"uuid": "11111111-2222-3333-4444-555555555555", "tls": True,
               "reality": {"public-key": "pk", "server-name": "sni"}}),
    ("trojan", {"password": "pw", "sni": "example.com"}),
    ("hysteria2", {"password": "pw", "sni": "example.com"}),
]


def test_clash_proxy_requires_dict():
    node = make_node("ss", {"cipher": "aes-128-gcm", "password": "pw"})
    try:
        parser.clash_proxy(node, "n0")
    except AttributeError as exc:
        assert "'str' object has no attribute 'get'" in str(exc)
    else:
        raise AssertionError("string params should have failed before normalization")


def test_normalize_then_build():
    for protocol, params in CASES:
        node = make_node(protocol, params)
        engine_mod.normalize_node(node)
        assert isinstance(node["params"], dict)
        proxy = parser.clash_proxy(node, "n0")
        assert proxy["type"] == protocol and proxy["server"] == "1.2.3.4"


def test_normalize_handles_empty():
    node = make_node("ss", {})
    node["params"] = ""
    engine_mod.normalize_node(node)
    assert node["params"] == {}
    assert engine_mod.normalize_node({"params": None})["params"] == {}


def test_prepare_normalizes_block():
    nodes = [make_node(p, v) for p, v in CASES]
    asyncio.run(engine_mod.engine_manager.prepare(nodes))
    assert all(isinstance(n["params"], dict) for n in nodes)


def test_mihomo_config_written_from_string_params():
    from backend.testers.engine import MihomoEngine  # noqa: E402
    import tempfile

    nodes = [make_node(p, v) for p, v in CASES]
    for node in nodes:
        engine_mod.normalize_node(node)
    with tempfile.TemporaryDirectory() as tmp:
        eng = MihomoEngine.__new__(MihomoEngine)
        eng.cfg_path = str(Path(tmp) / "config.yaml")
        eng._lock = asyncio.Lock()
        path = eng._write_config(nodes)
        text = Path(path).read_text(encoding="utf-8")
        assert "proxies:" in text and "listeners:" in text
        assert len(text.split("proxy: n")) >= len(CASES)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("PASS", name)
