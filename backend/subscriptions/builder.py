"""Unified subscription generation: filtering, naming, multi-format output, atomic publish."""
import base64
import json
import os
from datetime import datetime

import yaml

from backend import config
from backend.database import db
from backend.parsers import subscription as parser


def _fmt_ts() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def latest_results() -> dict[int, dict[int, dict]]:
    """node_id -> {group_id: {ok, latency_ms, error_type, ts}} using the newest row per pair."""
    sql = """
    SELECT r.node_id, r.group_id, r.ok, r.latency_ms, r.error_type, r.ts
    FROM results r
    JOIN (SELECT node_id, group_id, MAX(id) AS mx FROM results GROUP BY node_id, group_id) m
      ON m.node_id = r.node_id AND m.group_id = r.group_id AND m.mx = r.id
    """
    out: dict[int, dict[int, dict]] = {}
    for row in db.rows(sql):
        out.setdefault(row["node_id"], {})[row["group_id"]] = {
            "ok": bool(row["ok"]),
            "latency_ms": row["latency_ms"],
            "error_type": row["error_type"],
            "ts": row["ts"],
        }
    return out


def latest_latency() -> dict[int, dict]:
    """node_id -> newest latency probe row."""
    sql = """
    SELECT l.node_id, l.ok, l.latency_ms, l.error_type, l.ts
    FROM latency_results l
    JOIN (SELECT node_id, MAX(id) AS mx FROM latency_results GROUP BY node_id) m
      ON m.node_id = l.node_id AND m.mx = l.id
    """
    return {row["node_id"]: {"ok": bool(row["ok"]), "latency_ms": row["latency_ms"],
                             "error_type": row["error_type"], "ts": row["ts"]}
            for row in db.rows(sql)}


def collect_nodes() -> tuple[list[dict], dict]:
    """Apply eligibility rules and compute display names.

    Publish gate = newest latency probe within the configured ceiling. Test groups no longer
    decide eligibility; they only contribute the identifier segments of the display name.
    """
    groups = db.rows(
        "SELECT id, name, ident, sort_order, enabled FROM test_groups ORDER BY sort_order, id"
    )
    enabled_groups = [g for g in groups if g["enabled"]]
    results = latest_results()
    airports = {a["id"]: a for a in db.rows("SELECT id, name FROM airports ORDER BY id")}
    nodes = db.rows(
        """SELECT id, airport_id, raw_name, protocol, server, port, params, latency,
                  fail_count, status, region FROM nodes ORDER BY id"""
    )

    latests = latest_latency()
    max_latency = db.get_int("sub_max_latency", 500)
    max_nodes = db.get_int("sub_max_nodes", 0)
    fail_threshold = db.get_int("fail_threshold", 3)
    include_unavailable = db.get("sub_include_unavailable") == "1"

    selected: list[dict] = []
    skipped = {"no_result": 0, "failed_groups": 0, "slow": 0, "unavailable": 0,
               "bad_airport": 0, "latency_fail": 0}
    for node in nodes:
        airport = airports.get(node["airport_id"])
        if not airport:
            skipped["bad_airport"] += 1
            continue
        per_group = results.get(node["id"], {})
        passed = [g for g in enabled_groups if per_group.get(g["id"], {}).get("ok")]

        # The probe always runs per cycle and is the only publish gate
        probe = latests.get(node["id"])
        if not probe:
            skipped["no_result"] += 1
            continue
        if not probe["ok"]:
            skipped["latency_fail"] += 1
            continue
        latency = probe["latency_ms"] or (node["latency"] or 0)
        if max_latency and latency > max_latency:
            skipped["slow"] += 1
            continue
        if not include_unavailable and node["fail_count"] >= fail_threshold \
                and node["status"] == "unavailable":
            skipped["unavailable"] += 1
            continue
        # Names read as  机场｜GPT·YT·TG｜节点名  (the short ident, not the group's full name)
        idents = [g["ident"] for g in passed]
        display = (f"{airport['name']}｜{'·'.join(idents)}｜{node['raw_name']}" if idents
                   else f"{airport['name']}｜{node['raw_name']}")
        selected.append({**node, "display_name": display, "passed": passed,
                         "latency": latency, "params": db.loads(node["params"], {})})

    selected.sort(key=lambda n: (n["latency"] if n["latency"] else 99999, n["id"]))
    if max_nodes:
        selected = selected[:max_nodes]
    # Names must stay unique inside a client profile
    used: dict[str, int] = {}
    for node in selected:
        name = node["display_name"]
        used[name] = used.get(name, 0) + 1
        if used[name] > 1:
            node["display_name"] = f"{name} #{used[name]}"
    stats = {"total": len(nodes), "selected": len(selected), "skipped": skipped,
             "groups": len(enabled_groups)}
    return selected, stats


def build_clash(nodes: list[dict], sub_name: str) -> str:
    proxies = [parser.clash_proxy(n, n["display_name"]) for n in nodes]
    names = [n["display_name"] for n in nodes]
    doc = {
        "mixed-port": 7890,
        "allow-lan": False,
        "mode": "rule",
        "log-level": "info",
        "external-controller": "127.0.0.1:9090",
        "proxies": proxies,
        "proxy-groups": [
            {"name": sub_name, "type": "select",
             "proxies": (["自动选择"] + names) if names else ["DIRECT"]},
            {"name": "自动选择", "type": "url-test", "url": "https://www.gstatic.com/generate_204",
             "interval": 300, "tolerance": 50, "proxies": names or ["DIRECT"]},
        ],
        "rules": ["MATCH," + sub_name],
    }
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def build_singbox(nodes: list[dict], sub_name: str) -> str:
    built = [parser.singbox_outbound(n, n["display_name"]) for n in nodes]
    outbounds = [o for o in built if o]   # some protocols (ssr) have no sing-box equivalent
    tags = [o["tag"] for o in outbounds]
    selector = {"type": "selector", "tag": sub_name, "outbounds": tags + (["direct"] if tags else []),
                "default": tags[0] if tags else "direct"}
    urltest = {"type": "urltest", "tag": "自动选择", "outbounds": tags or ["direct"],
               "url": "https://www.gstatic.com/generate_204", "interval": "5m", "tolerance": 50}
    doc = {
        "log": {"level": "info", "timestamp": True},
        "dns": {
            "servers": [{"tag": "google", "address": "tls://8.8.8.8"}],
            "strategy": "prefer_ipv4",
        },
        "inbounds": [
            {"type": "mixed", "tag": "mixed-in", "listen": "0.0.0.0", "listen_port": 7890},
        ],
        "outbounds": [
            *outbounds,
            selector,
            urltest,
            {"type": "direct", "tag": "direct"},
            {"type": "block", "tag": "block"},
        ],
        "route": {"rules": [], "final": sub_name, "auto_detect_interface": True},
    }
    return json.dumps(doc, ensure_ascii=False, indent=2)


def build_base64(nodes: list[dict]) -> str:
    links = [parser.to_uri(n) for n in nodes]
    payload = "\n".join(links)
    return base64.b64encode(payload.encode()).decode()


def validate(fmt: str, content: str, nodes: list[dict]) -> None:
    if fmt == "clash":
        doc = yaml.safe_load(content)
        if not isinstance(doc, dict) or not isinstance(doc.get("proxies"), list):
            raise ValueError("clash output is not a valid proxies document")
        if nodes and not doc["proxies"]:
            raise ValueError("clash output has no proxies")
    elif fmt == "singbox":
        doc = json.loads(content)
        if not isinstance(doc.get("outbounds"), list):
            raise ValueError("sing-box output has no outbounds")
    elif fmt == "base64":
        if nodes:
            decoded = base64.b64decode(content).decode()
            if "://" not in decoded:
                raise ValueError("base64 output is not a link list")
    else:
        raise ValueError(f"unknown format {fmt}")


FORMATS = {"clash": ("sub_all.yaml", build_clash),
           "singbox": ("sub_all.json", build_singbox),
           "base64": ("sub_all.base64", build_base64)}


def publish(reason: str = "manual", batch_id: int | None = None) -> dict:
    """Generate all formats, validate each one, then atomically replace the live files."""
    nodes, stats = collect_nodes()
    sub_name = db.get("sub_name", "NodePool Unified") or "NodePool Unified"
    previous = meta()
    if not nodes and previous.get("nodes"):
        db.append_log("WARN", "publish",
                      "no node qualified in this batch; previous subscription kept untouched")
        return {**previous, "skipped": True, "stats": stats}
    written = {}
    for fmt, (filename, fn) in FORMATS.items():
        content = fn(nodes, sub_name) if fmt != "base64" else fn(nodes)
        validate(fmt, content, nodes)
        target = config.SUB_DIR / filename
        tmp = config.SUB_DIR / f".{filename}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
        written[fmt] = str(target)
    manifest = {
        "name": sub_name,
        "nodes": len(nodes),
        "updated_at": _fmt_ts(),
        "batch_id": batch_id,
        "reason": reason,
        "stats": stats,
        "formats": list(FORMATS),
    }
    tmp = config.SUB_DIR / ".meta.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config.SUB_DIR / "meta.json")
    db.set_setting("sub_last_generated_at", manifest["updated_at"])
    db.set_setting("sub_node_count", str(len(nodes)))
    db.append_log("INFO", "publish", f"subscription published ({len(nodes)} nodes, reason={reason})")
    return manifest


def meta() -> dict:
    path = config.SUB_DIR / "meta.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            pass
    return {"name": db.get("sub_name"), "nodes": 0, "updated_at": None, "formats": list(FORMATS)}


def read(fmt: str = "clash") -> tuple[str, str]:
    filename, _ = FORMATS.get(fmt, FORMATS["clash"])
    path = config.SUB_DIR / filename
    if not path.exists():
        raise FileNotFoundError(fmt)
    return fmt, path.read_text(encoding="utf-8")
