"""P6.2.2 unified subscription acceptance tests."""
from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-p622-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

import pytest
import yaml
from fastapi.testclient import TestClient

from backend import config as app_config
from backend.competition import store as p5_store
from backend.database import db
from backend.final_filter import service as filter_service
from backend.final_filter.store import CATEGORIES
from backend.final_results import service as final_service
from backend.main import app
from backend.nodepool import store as p1_store
from backend.unified_subscription import formats, service, store
from backend.unified_subscription.service import UnifiedError


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def seed_node(name: str, airport_name: str, *, protocol: str = "ss", server: str = "127.0.0.1",
              port: int = 443, params: dict | None = None,
              airport_url: str | None = None) -> int:
    stamp = _stamp()
    params = params or {"cipher": "aes-256-gcm", "password": "node-pass"}
    with db.transaction() as conn:
        p1_store.ensure_schema(conn)
        row = conn.execute("SELECT id FROM airports WHERE name=?", (airport_name,)).fetchone()
        if row:
            aid = int(row["id"])
        else:
            aid = conn.execute(
                """INSERT INTO airports(name,url,enabled,interval_minutes,created_at,updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (airport_name, airport_url or f"https://{abs(hash(airport_name))}.example/sub",
                 1, 60, stamp, stamp),
            ).lastrowid
        return int(conn.execute(
            """INSERT INTO np_nodes
               (airport_id,identity_key,occurrence,current_name,first_name,protocol,server,
                port,params,source_active,management_layer,base_health,data_status,
                first_seen_at,last_seen_at,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, f"{name}-{stamp}-{server}-key", 1, name, name, protocol, server, port,
             json.dumps(params, ensure_ascii=False), 1, "observation", "healthy", "ample",
             stamp, stamp, stamp, stamp),
        ).lastrowid)


def prepare_p6(nodes: list[tuple[int, list[str]]], run_status: str = "success"):
    with db.transaction() as conn:
        p5_store.ensure_states(conn)
    stamp = _stamp()
    with db.transaction() as conn:
        for node_id, categories in nodes:
            for category in categories:
                conn.execute(
                    """UPDATE np5_state SET state='preferred', score=88, maturity=88,
                       p80_ms=120, delay_grade='good', speed_grade='good', speed_mb_s=3,
                       preferred_since=?, last_reason='admission_ok', last_evaluated_at=?,
                       last_change_at=?, updated_at=?
                       WHERE node_id=? AND category=?""",
                    (stamp, stamp, stamp, stamp, node_id, category),
                )
        run_id = p5_store.start_run(conn, trigger="manual", categories=list(CATEGORIES))
        p5_store.finish_run(conn, run_id, status=run_status, nodes_scanned=len(nodes), changes=0)
    return final_service.snapshot("manual")


def clear_rules():
    payload = filter_service.summary()
    for rule in payload["rules"]:
        filter_service.delete_rule(rule["id"])


def seed_unified() -> dict:
    """Seven distinct stable IDs; one duplicate display name; one filtered node."""
    n1 = seed_node("新加坡专线03", "良心云", server="10.0.0.1", params={
        "cipher": "aes-256-gcm", "password": "p1"})
    n2 = seed_node("东京节点", "良心云", protocol="vmess", server="10.0.0.2",
                   params={"uuid": "uuid-2", "alterId": 0, "cipher": "auto",
                           "network": "ws", "security": "tls", "host": "cdn.example.com",
                           "path": "/ws", "sni": "cdn.example.com", "alpn": "h2,http/1.1",
                           "fingerprint": "chrome"})
    n3 = seed_node("洛杉矶节点", "星云", protocol="vless", server="10.0.0.3",
                   params={"uuid": "uuid-3", "network": "ws", "security": "reality",
                           "host": "reality.example.com", "path": "/r", "sni": "reality.example.com",
                           "publicKey": "pbk-test", "shortId": "sid-test", "fingerprint": "chrome",
                           "alpn": "h2"})
    n4 = seed_node("香港节点", "星云", protocol="trojan", server="10.0.0.4",
                   params={"password": "trojan-pass", "network": "tcp", "sni": "trojan.example.com",
                           "alpn": "h2,http/1.1"})
    n5 = seed_node("屏蔽节点", "测试机场", protocol="hysteria2", server="10.0.0.5",
                   params={"password": "hy2-pass", "sni": "hy.example.com",
                           "obfs": "salamander", "obfs-password": "obfs-pass"})
    n6 = seed_node("新加坡专线03", "备用机场", server="10.0.0.6", params={
        "cipher": "aes-256-gcm", "password": "p6"})
    prepare_p6([
        (n1, ["standard", "gpt", "youtube", "telegram", "github"]),
        (n2, ["standard", "global"]),
        (n3, ["youtube"]),
        (n4, ["gpt", "github"]),
        (n5, ["global"]),
        (n6, ["standard"]),
    ])
    clear_rules()
    filter_service.create_rule("屏蔽")
    return {"n1": n1, "n2": n2, "n3": n3, "n4": n4, "n5": n5, "n6": n6}


def _uri_name(line: str) -> str:
    if line.startswith("vmess://"):
        payload = base64.b64decode(line[8:]).decode()
        return json.loads(payload).get("ps", "")
    if "#" in line:
        return urllib.parse.unquote(line.rsplit("#", 1)[-1])
    return ""


def _content_lines(fmt: str, content: str) -> list[str]:
    if fmt == "clash":
        return [p["name"] for p in yaml.safe_load(content)["proxies"]]
    if fmt == "singbox":
        doc = json.loads(content)
        return [o["tag"] for o in doc["outbounds"]
                if o["type"] not in ("selector", "urltest", "direct", "block")]
    if fmt == "base64":
        decoded = base64.b64decode(content).decode()
    else:
        decoded = content
    return [_uri_name(line) for line in decoded.splitlines()
            if line.strip() and not line.startswith("#")]


def test_merge_dedupe_labels_and_filter():
    ids = seed_unified()
    result = service.generate()
    batch = result["batch"]
    assert batch["status"] == "success"
    assert batch["merged_count"] == 12
    assert batch["unique_count"] == 6
    assert batch["duplicate_count"] == 6
    assert batch["filtered_count"] == 1
    assert batch["candidate_count"] == 5
    assert batch["compatibility_excluded_count"] == 0
    assert batch["output_count"] == 5
    assert batch["labels"] == {"gpt": 2, "yt": 2, "tg": 1, "gh": 2, "multi": 2}

    nodes = {n["node_id"]: n for n in service.summary()["nodes"]}
    assert set(nodes) == {ids[k] for k in ("n1", "n2", "n3", "n4", "n5", "n6")}
    assert nodes[ids["n1"]]["labels"] == ["GPT", "YT", "TG", "GH"]
    assert nodes[ids["n2"]]["labels"] == []
    assert nodes[ids["n3"]]["labels"] == ["YT"]
    assert nodes[ids["n4"]]["labels"] == ["GPT", "GH"]
    assert nodes[ids["n5"]]["labels"] == []
    assert nodes[ids["n5"]]["status"] == "filtered"
    assert nodes[ids["n5"]]["hits"][0]["keyword"] == "屏蔽"
    assert nodes[ids["n1"]]["display_name"].startswith("🔵良心云·GPT·YT·TG·GH·新加坡专线03")
    # Same raw name, different stable IDs: both retained, final names unique.
    assert nodes[ids["n1"]]["display_name"] != nodes[ids["n6"]]["display_name"]
    assert nodes[ids["n6"]]["node_name"] == "新加坡专线03"


def test_four_formats_same_set_names_and_protocol_params():
    seed_unified()
    result = service.generate()
    summary = service.summary()
    batch = summary["batch"]
    outputs = {o["format"]: o for o in summary["formats"]}
    assert set(outputs) == set(formats.FORMAT_KEYS)
    assert all(o["status"] == "success" for o in outputs.values())
    assert all(o["node_count"] == 5 for o in outputs.values())
    names = {fmt: set(_content_lines(fmt, service.format_content(fmt)["content"]))
             for fmt in formats.FORMAT_KEYS}
    assert all(value == names["clash"] for value in names.values())
    assert len(names["clash"]) == 5

    clash = yaml.safe_load(service.format_content("clash")["content"])
    by_suffix = {p["name"].split("·")[-1].replace(" #2", ""): p for p in clash["proxies"]}
    vmess = by_suffix["东京节点"]
    assert vmess["network"] == "ws"
    assert vmess["ws-opts"]["path"] == "/ws"
    assert vmess["ws-opts"]["headers"]["Host"] == "cdn.example.com"
    assert vmess["servername"] == "cdn.example.com"
    assert vmess["alpn"] == ["h2", "http/1.1"]
    vless = by_suffix["洛杉矶节点"]
    assert vless["reality-opts"] == {"public-key": "pbk-test", "short-id": "sid-test"}
    assert vless["client-fingerprint"] == "chrome"
    assert vless["alpn"] == ["h2"]
    trojan = by_suffix["香港节点"]
    assert trojan["password"] == "trojan-pass"
    assert trojan["alpn"] == ["h2", "http/1.1"]
    # filtered node is absent from client content

    sing = json.loads(service.format_content("singbox")["content"])
    sing_nodes = {o["tag"]: o for o in sing["outbounds"]
                  if o["type"] not in ("selector", "urltest", "direct", "block")}
    vless_s = next(o for tag, o in sing_nodes.items() if "洛杉矶节点" in tag)
    assert vless_s["reality"] == {"enabled": True, "public_key": "pbk-test",
                                  "short_id": "sid-test"}
    assert vless_s["tls"]["server_name"] == "reality.example.com"
    assert vless_s["tls"]["alpn"] == ["h2"]
    assert vless_s["utls"]["fingerprint"] == "chrome"
    assert vless_s["transport"]["type"] == "ws"

    b64 = base64.b64decode(service.format_content("base64")["content"]).decode()
    vless_line = next(line for line in b64.splitlines() if "洛杉矶节点" in line)
    assert "pbk=pbk-test" in vless_line and "sid=sid-test" in vless_line
    assert "type=ws" in vless_line and "path=/r" in vless_line

    loon = service.format_content("loon")["content"]
    vless_loon = next(line for line in loon.splitlines() if "洛杉矶节点" in line)
    assert "pbk=pbk-test" in vless_loon and "sid=sid-test" in vless_loon
    assert "security=reality" in vless_loon


def test_duplicate_final_display_name_gets_suffix_in_all_formats():
    n1 = seed_node("同名节点", "同一机场", server="10.0.2.1")
    n2 = seed_node("同名节点", "同一机场", server="10.0.2.2")
    prepare_p6([(n1, ["standard"]), (n2, ["standard"])])
    clear_rules()
    batch = service.generate()["batch"]
    assert batch["output_count"] == 2
    names = {n["node_id"]: n["display_name"] for n in service.summary()["nodes"]}
    assert len(set(names.values())) == 2
    assert names[n2] == names[n1] + " #2"
    for fmt in formats.FORMAT_KEYS:
        out = service.format_content(fmt)
        assert out["status"] == "success"
        assert out["node_count"] == 2
        assert set(_content_lines(fmt, out["content"])) == set(names.values())


def test_format_failure_is_recorded_as_partial_or_failed(monkeypatch):
    n1 = seed_node("正常节点", "机场A", server="10.0.3.1")
    prepare_p6([(n1, ["standard"])])
    clear_rules()
    real_build = formats.build

    def fail_loon(fmt, nodes, sub_name):
        if fmt == "loon":
            raise RuntimeError("Loon 渲染测试失败")
        return real_build(fmt, nodes, sub_name)

    monkeypatch.setattr(formats, "build", fail_loon)
    partial = service.generate()
    batch = partial["batch"]
    assert batch["status"] == "partial"
    assert batch["output_count"] == 1
    outputs = {o["format"]: o for o in batch["outputs"]}
    assert outputs["loon"]["status"] == "failed"
    assert outputs["loon"]["content_bytes"] == 0
    assert "Loon 渲染测试失败" in batch["status_reason"]
    assert all(outputs[f]["status"] == "success" for f in ("clash", "singbox", "base64"))
    with pytest.raises(UnifiedError) as err:
        service.download("loon")
    assert err.value.code == "output_failed"

    def fail_all(fmt, nodes, sub_name):
        raise RuntimeError(f"{fmt} 渲染测试失败")

    monkeypatch.setattr(formats, "build", fail_all)
    failed = service.generate()
    second = failed["batch"]
    assert failed["ok"] is False
    assert second["status"] == "failed"
    assert second["id"] != batch["id"]
    assert all(o["status"] == "failed" for o in second["outputs"])
    assert "clash 渲染测试失败" in second["status_reason"]
    for fmt in formats.FORMAT_KEYS:
        assert service.format_content(fmt, batch_id=second["id"])["status"] == "failed"


def test_no_upstream_url_or_credentials_in_content():
    ids = seed_unified()
    # Mark the source URL with a value that must never reach the subscription.
    with db.transaction() as conn:
        conn.execute("UPDATE airports SET url='https://upstream.example/secret-token-xyz'")
    result = service.generate()
    for fmt in formats.FORMAT_KEYS:
        content = service.format_content(fmt)["content"]
        assert "upstream.example" not in content
        assert "secret-token-xyz" not in content
        assert "source_url" not in content


def test_rule_or_p6_change_invalidates_old_batch_and_adds_new_batch():
    seed_unified()
    first = service.generate()["batch"]
    old_batch_id = first["id"]
    old_text = service.format_content("clash")["content"]
    filter_service.create_rule("东京")
    summary = service.summary()
    assert summary["status"] == "stale"
    assert summary["batch"]["stale"] is True
    with pytest.raises(UnifiedError) as err:
        service.download("clash")
    assert err.value.code == "stale_generation"

    second = service.generate()["batch"]
    assert second["id"] != old_batch_id
    assert second["batch_no"] != first["batch_no"]
    assert second["filtered_count"] == 2
    assert second["output_count"] == 4
    assert service.format_content("clash", batch_id=old_batch_id)["content"] == old_text
    assert service.history()["items"][-1]["id"] == old_batch_id
    ids = [item["id"] for item in service.history()["items"]]
    assert ids == [second["id"], old_batch_id]


def test_unsupported_protocol_is_real_failure_not_silent_or_fake():
    n1 = seed_node("正常SS", "机场A", params={"cipher": "aes-256-gcm", "password": "p"})
    n2 = seed_node("TUIC节点", "机场A", protocol="tuic", server="10.0.1.2",
                   params={"uuid": "u", "password": "p", "sni": "tuic.example.com"})
    prepare_p6([(n1, ["standard"]), (n2, ["standard"])])
    clear_rules()
    result = service.generate()
    batch = result["batch"]
    assert batch["compatibility_excluded_count"] == 1
    assert batch["output_count"] == 1
    assert batch["candidate_count"] == 2
    assert batch["unsupported"]["nodes"][0]["protocol"] == "tuic"
    assert "loon" in batch["unsupported"]["nodes"][0]["formats"]
    assert batch["status"] == "success"
    nodes = {n["node_id"]: n for n in service.summary()["nodes"]}
    assert nodes[n2]["status"] == "unsupported"
    for fmt in formats.FORMAT_KEYS:
        assert service.format_content(fmt)["node_count"] == 1


def test_empty_unified_subscription_is_valid_and_recorded():
    prepare_p6([])
    clear_rules()
    result = service.generate()
    batch = result["batch"]
    assert batch["status"] == "empty"
    assert batch["is_empty"] is True
    assert batch["output_count"] == 0
    for fmt in formats.FORMAT_KEYS:
        out = service.format_content(fmt)
        assert out["status"] == "success"
        assert out["node_count"] == 0
        formats.validate(fmt, out["content"], [])


def test_api_and_dashboard_real_data():
    seed_unified()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        summary = client.get("/api/unified-subscription/summary")
        assert summary.status_code == 200
        assert summary.json()["status"] == "not_generated"
        generated = client.post("/api/unified-subscription/generate")
        assert generated.status_code == 200
        data = generated.json()
        assert data["batch"]["output_count"] == 5
        current = client.get("/api/unified-subscription/summary").json()
        assert current["status"] == "success"
        fmt = client.get("/api/unified-subscription/formats/clash").json()
        assert fmt["status"] == "success" and "proxies:" in fmt["content"]
        download = client.get("/api/unified-subscription/download/clash")
        assert download.status_code == 200
        assert download.headers["content-disposition"].startswith("attachment")
        assert len(yaml.safe_load(download.text)["proxies"]) == 5
        dashboard = client.get("/api/dashboard").json()
        block = dashboard["unified_subscription"]
        assert block["nodes"] == 5
        assert block["labels"]["gpt"] == 2
        assert block["status"] == "success"
        assert set(block["formats"]) == set(formats.FORMAT_KEYS)


def test_stale_download_is_blocked_by_api():
    seed_unified()
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        client.post("/api/unified-subscription/generate")
        client.post("/api/final-filter/rules", json={"keyword": "洛杉矶"})
        summary = client.get("/api/unified-subscription/summary").json()
        assert summary["status"] == "stale"
        download = client.get("/api/unified-subscription/download/clash")
        assert download.status_code == 409
