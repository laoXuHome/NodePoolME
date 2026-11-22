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


def latest_results(batch_id: int | None = None) -> dict[int, dict[int, dict]]:
    """node_id -> {group_id: ...} using the newest row per pair, optionally batch-scoped."""
    if batch_id:
        sql = """
        SELECT r.node_id, r.group_id, r.ok, r.latency_ms, r.error_type, r.ts
        FROM results r
        JOIN (SELECT node_id, group_id, MAX(id) AS mx FROM results WHERE batch_id=?
              GROUP BY node_id, group_id) m
          ON m.node_id = r.node_id AND m.group_id = r.group_id AND m.mx = r.id
        WHERE r.batch_id=?
        """
        rows = db.rows(sql, (batch_id, batch_id))
    else:
        sql = """
        SELECT r.node_id, r.group_id, r.ok, r.latency_ms, r.error_type, r.ts
        FROM results r
        JOIN (SELECT node_id, group_id, MAX(id) AS mx FROM results GROUP BY node_id, group_id) m
          ON m.node_id = r.node_id AND m.group_id = r.group_id AND m.mx = r.id
        """
        rows = db.rows(sql)
    out: dict[int, dict[int, dict]] = {}
    for row in rows:
        out.setdefault(row["node_id"], {})[row["group_id"]] = {
            "ok": bool(row["ok"]),
            "latency_ms": row["latency_ms"],
            "error_type": row["error_type"],
            "ts": row["ts"],
        }
    return out


NAME_SEP = "⁃"   # outer separator: 机场⁃标签⁃节点名
TAG_SEP = "ᐧ"    # inner separator between tag idents


def latest_latency(batch_id: int | None = None) -> dict[int, dict]:
    """node_id -> dual-target summary.

    Each target keeps its last three rounds; the best passing round counts. The node only
    passes when every configured target has a passing round within its own ceiling.
    When batch_id is given, only results from that batch participate.
    """
    if batch_id:
        sql = f"""
        SELECT l.node_id, l.target, l.ok, l.latency_ms, l.error_type, l.ts, l.id
        FROM latency_results l
        WHERE l.batch_id = {int(batch_id)} AND l.id IN (
            SELECT l2.id FROM latency_results l2
            WHERE l2.node_id = l.node_id AND l2.target = l.target
              AND l2.batch_id = {int(batch_id)}
            ORDER BY l2.id DESC LIMIT 3
        )
        ORDER BY l.id
        """
    else:
        sql = """
        SELECT l.node_id, l.target, l.ok, l.latency_ms, l.error_type, l.ts, l.id
        FROM latency_results l
        WHERE l.id IN (
            SELECT l2.id FROM latency_results l2
            WHERE l2.node_id = l.node_id AND l2.target = l.target
            ORDER BY l2.id DESC LIMIT 3
        )
        ORDER BY l.id
        """
    caps = {
        "G": db.get_int("sub_max_latency", 1000),
        "W": db.get_int("sub_max_latency2", 1000),
    }
    per_target: dict[int, dict[str, list[dict]]] = {}
    for row in db.rows(sql):
        per_target.setdefault(row["node_id"], {}).setdefault(row["target"] or "G", []).append(row)
    out: dict[int, dict] = {}
    for node_id, targets in per_target.items():
        best: dict[str, int | None] = {}
        detail: dict[str, dict] = {}
        error_type = None
        ts = None
        for target, rows in targets.items():
            cap = caps.get(target, 1000)
            ok_rows = [r for r in rows if r["ok"] and r["latency_ms"] is not None
                       and (not cap or r["latency_ms"] <= cap)]
            best[target] = min((r["latency_ms"] for r in ok_rows), default=None)
            measured = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
            detail[target] = {
                "cap": cap,
                "best": best[target],
                # Display only: fastest measured round even when it failed or exceeded the cap.
                "min": min(measured, default=None),
                "attempts": len(rows),
                "last_ok": bool(rows[0]["ok"]) if rows else False,
            }
            if not ok_rows and rows:
                error_type = rows[0]["error_type"] or error_type
            ts = max(filter(None, [ts] + [r["ts"] for r in rows]), default=None)
        required = set(caps)
        gate = all(best.get(t) is not None for t in required)
        values = [v for v in best.values() if v]
        out[node_id] = {
            "ok": gate,
            "latency_ms": max(values) if values else None,
            "fastest_ms": min((d["min"] for d in detail.values() if d["min"] is not None),
                              default=None),
            "error_type": error_type,
            "ts": ts,
            "targets": detail,
        }
    return out


def collect_nodes(batch_id: int | None = None) -> tuple[list[dict], dict]:
    """Apply eligibility rules and compute display names.

    Publish gate = newest latency probe within the configured ceiling. Test groups no longer
    decide eligibility; they only contribute the identifier segments of the display name.
    """
    groups = db.rows(
        "SELECT id, name, ident, sort_order, enabled FROM test_groups ORDER BY sort_order, id"
    )
    enabled_groups = [g for g in groups if g["enabled"]]
    results = latest_results(batch_id)
    airports = {a["id"]: a for a in db.rows("SELECT id, name FROM airports ORDER BY id")}
    nodes = db.rows(
        """SELECT n.id, n.airport_id, n.raw_name, n.protocol, n.server, n.port, n.params,
                  n.latency, n.fail_count, n.status, n.region
           FROM nodes n JOIN airports a ON a.id=n.airport_id
           WHERE a.enabled=1 ORDER BY n.id"""
    )

    latests = latest_latency(batch_id)
    required_tags = [t for t in (db.get("sub_required_tags") or "").split(",") if t]


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
        if not probe.get("ok"):
            skipped["latency_fail"] += 1
            continue
        # Per-target ceilings are enforced inside latest_latency(); the node's latency is
        # the worse of the two passing targets, so no extra global cap check is applied.
        latency = probe["latency_ms"] or (node["latency"] or 0)
        if required_tags:
            node_idents = {g["ident"] for g in passed}
            missing = [t for t in required_tags if t not in node_idents]
            if missing:
                skipped.setdefault("tag_filter", 0)
                skipped["tag_filter"] += 1
                continue
        # Names read as  机场⁃GPTᐧYTᐧTG⁃节点名  (the short ident, not the group's full name)
        idents = [g["ident"] for g in passed]
        display = (f"{airport['name']}{NAME_SEP}{TAG_SEP.join(idents)}{NAME_SEP}{node['raw_name']}"
                   if idents else f"{airport['name']}{NAME_SEP}{node['raw_name']}")
        selected.append({**node, "display_name": display, "passed": passed,
                         "latency": latency, "params": db.loads(node["params"], {})})

    selected.sort(key=lambda n: (n["latency"] if n["latency"] else 99999, n["id"]))
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
            raise ValueError("Clash 输出不是有效的 proxies 文档")
        if nodes and not doc["proxies"]:
            raise ValueError("Clash 输出没有 proxies")
    elif fmt == "singbox":
        doc = json.loads(content)
        if not isinstance(doc.get("outbounds"), list):
            raise ValueError("sing-box 输出没有 outbounds")
    elif fmt == "base64":
        if nodes:
            decoded = base64.b64decode(content).decode()
            if "://" not in decoded:
                raise ValueError("Base64 输出不是节点链接列表")
    else:
        raise ValueError(f"未知订阅格式：{fmt}")


FORMATS = {"clash": ("sub_all.yaml", build_clash),
           "singbox": ("sub_all.json", build_singbox),
           "base64": ("sub_all.base64", build_base64)}



def _snapshot_entry(node_id: int, name: str) -> dict | None:
    """Rebuild one previously published node from the DB, keeping its published name."""
    row = db.row(
        """SELECT n.id, n.airport_id, n.raw_name, n.protocol, n.server, n.port, n.params,
                  n.latency, n.fail_count, n.status, n.region, a.name AS airport_name
           FROM nodes n JOIN airports a ON a.id = n.airport_id
           WHERE n.id = ? AND a.enabled = 1""",
        (node_id,),
    )
    if not row:
        return None
    row["params"] = db.loads(row.get("params"), {})
    row["display_name"] = name
    row["passed"] = []
    return row


def _live_snapshot() -> list[dict]:
    """Recover the published node set from the live Clash file (manifests before v1.2.15)."""
    path = config.SUB_DIR / "sub_all.yaml"
    if not path.exists():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    nodes = []
    for proxy in (doc or {}).get("proxies") or []:
        name, server, port = proxy.get("name"), proxy.get("server"), proxy.get("port")
        if not name or not server:
            continue
        proto = proxy.get("type")
        rows = db.rows(
            "SELECT id, raw_name FROM nodes WHERE server=? AND port=? AND protocol=? "
            "ORDER BY LENGTH(raw_name) DESC",
            (server, port, proto),
        ) or db.rows("SELECT id, raw_name FROM nodes WHERE server=? AND port=? "
                     "ORDER BY LENGTH(raw_name) DESC", (server, port))
        # Published names end with the raw node name; prefer that over blind first-match
        # so duplicate server/port nodes (different users) don't collapse into one.
        match = next((r for r in rows if r["raw_name"] and name.endswith(r["raw_name"])), None)
        match = match or (rows[0] if rows else None)
        if not match:
            continue
        entry = _snapshot_entry(match["id"], name)
        if entry and all(x["id"] != entry["id"] for x in nodes):
            nodes.append(entry)
    return nodes


def add_published_node(node_id: int) -> dict:
    """Append a manually verified node to the live subscription.

    Other published nodes are kept exactly as they were; the manifest's updated_at,
    batch_id and reason stay untouched so the dashboard's publish/detection dates do not move.
    """
    previous = meta()
    if not previous.get("nodes") or not (config.SUB_DIR / "sub_all.yaml").exists():
        return {"ok": False, "reason": "no existing subscription"}

    ids = previous.get("node_ids")
    names = {int(x["id"]): x["name"] for x in (previous.get("node_names") or [])}
    if ids:
        nodes = [n for n in (_snapshot_entry(int(i), names.get(int(i), "")) for i in ids) if n]
    else:
        # Older manifests did not record ids: recover the live file first, so nodes that
        # failed later are still preserved instead of being silently dropped.
        nodes = _live_snapshot() or collect_nodes()[0]

    if any(n["id"] == node_id for n in nodes):
        return {**previous, "ok": True, "added": False, "reason": "already published"}

    fresh = collect_nodes()[0]
    entry = next((n for n in fresh if n["id"] == node_id), None)
    if not entry:
        return {"ok": False, "reason": "node does not meet the publish gate"}
    nodes.append(entry)

    sub_name = db.get("sub_name", "NodePool Unified") or "NodePool Unified"
    for fmt, (filename, fn) in FORMATS.items():
        content = fn(nodes, sub_name) if fmt != "base64" else fn(nodes)
        validate(fmt, content, nodes)
        target = config.SUB_DIR / filename
        tmp = config.SUB_DIR / f".{filename}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)

    manifest = {
        **previous,
        "nodes": len(nodes),
        "node_ids": [n["id"] for n in nodes],
        "node_names": [{"id": n["id"], "name": n["display_name"]} for n in nodes],
        "last_added": node_id,
    }
    tmp = config.SUB_DIR / ".meta.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, config.SUB_DIR / "meta.json")
    db.set_setting("sub_node_count", str(len(nodes)))
    db.append_log("INFO", "下发", f"节点 #{node_id} 已追加到当前订阅（当前 {len(nodes)} 个节点）")
    return {**manifest, "ok": True, "added": True}


def publish(reason: str = "manual", batch_id: int | None = None) -> dict:
    """Generate all formats, validate each one, then atomically replace the live files.

    A batch-scoped publish only considers results produced by that batch, so stopping a
    cycle early publishes exactly the progress achieved so far.
    """
    nodes, stats = collect_nodes(batch_id)
    sub_name = db.get("sub_name", "NodePool Unified") or "NodePool Unified"
    previous = meta()
    if not nodes and previous.get("nodes"):
        db.append_log("WARN", "下发",
                      "本批次没有符合下发条件的节点，保留原有订阅不变")
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
        "node_ids": [n["id"] for n in nodes],
        "node_names": [{"id": n["id"], "name": n["display_name"]} for n in nodes],
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
    reason_cn = {
        "airport toggle": "机场启停", "manual": "手动", "manual rebuild": "手动重建",
        "restore": "数据库恢复", "schedule": "定时检测", "scheduled publish": "定时下发",
    }.get(reason, reason)
    if str(reason).startswith("batch#"):
        reason_cn = "批次 #" + str(reason)[6:]
    db.append_log("INFO", "下发", f"订阅已下发（{len(nodes)} 个节点，原因：{reason_cn}）")
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
