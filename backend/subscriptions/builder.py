"""Unified subscription generation: filtering, naming, multi-format output, atomic publish."""
import base64
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta

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


def filter_keywords() -> list[str]:
    """下发关键词过滤：关键词之间用空格、逗号或换行分隔（# 开头为注释）。

    只作用于「订阅输出」，不参与机场评分（评分用的是未过滤的检测快照）。
    """
    raw = db.get("sub_filter_keywords") or ""
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("#")]
    tokens = [t for t in re.split(r"[,，、;；\s]+", " ".join(lines)) if t]
    keys: list[str] = []
    for token in tokens:
        key = token.strip().lower()
        if key and not key.startswith("#") and key not in keys:
            keys.append(key)
    return keys


def apply_name_filter(nodes: list[dict]) -> tuple[list[dict], list[dict]]:
    """按关键词剔除不适合下发的节点（机场名、节点名、服务器地址任一命中即剔除）。"""
    keys = filter_keywords()
    if not keys:
        return nodes, []
    kept: list[dict] = []
    dropped: list[dict] = []
    for node in nodes:
        haystack = " ".join(str(x or "") for x in (
            node.get("display_name"), node.get("raw_name"), node.get("server"),
        )).lower()
        hit = next((k for k in keys if k in haystack), None)
        if hit:
            dropped.append({"id": node.get("id"), "name": node.get("display_name"),
                            "airport_id": node.get("airport_id"), "keyword": hit})
        else:
            kept.append(node)
    return kept, dropped


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


def _pool_counts() -> tuple[dict[int, int], int]:
    rows = db.rows(
        """SELECT n.airport_id, COUNT(*) AS c FROM nodes n
           JOIN airports a ON a.id=n.airport_id AND a.enabled=1
           GROUP BY n.airport_id"""
    )
    counts = {row["airport_id"]: row["c"] for row in rows}
    return counts, sum(counts.values())


def _available_counts() -> tuple[dict[int, int], int]:
    rows = db.rows(
        """SELECT n.airport_id, COUNT(*) AS c FROM nodes n
           JOIN airports a ON a.id=n.airport_id AND a.enabled=1
           WHERE n.status='available' GROUP BY n.airport_id"""
    )
    counts = {row["airport_id"]: row["c"] for row in rows}
    return counts, sum(counts.values())


def _batch_pool_counts(batch_id: int) -> tuple[dict[int, int], int]:
    rows = db.rows(
        """SELECT n.airport_id, COUNT(DISTINCT x.node_id) AS c FROM (
               SELECT node_id FROM results WHERE batch_id=?
               UNION
               SELECT node_id FROM latency_results WHERE batch_id=?
           ) x
           JOIN nodes n ON n.id=x.node_id
           JOIN airports a ON a.id=n.airport_id AND a.enabled=1
           GROUP BY n.airport_id""",
        (batch_id, batch_id),
    )
    counts = {row["airport_id"]: row["c"] for row in rows}
    return counts, sum(counts.values())


def record_share_snapshot(nodes: list[dict], batch_id: int | None = None, reason: str = "publish",
                          ts: str | None = None, pool_counts: dict[int, int] | None = None,
                          total_nodes: int | None = None, available_known: bool = True) -> None:
    """Persist one per-airport pool/publish snapshot for the dashboard trend charts."""
    if pool_counts is None:
        pool_counts, total_nodes = _pool_counts()
    total_nodes = total_nodes if total_nodes is not None else sum(pool_counts.values())
    if available_known:
        available_by_airport, total_available = _available_counts()
    else:
        available_by_airport, total_available = {}, 0
    published_by_airport = Counter(n.get("airport_id") for n in nodes)
    stamp = ts or _fmt_ts()
    for airport_id, pool_count in pool_counts.items():
        db.execute(
            """INSERT OR REPLACE INTO share_history
               (ts, batch_id, reason, airport_id, pool_count, published_count,
                available_count, total_nodes, total_published, total_available, available_known)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (stamp, batch_id, reason, airport_id, pool_count,
             published_by_airport.get(airport_id, 0), available_by_airport.get(airport_id, 0),
             total_nodes, len(nodes), total_available,
             1 if available_known else 0),
        )


def ensure_share_history(days: int = 7) -> None:
    """One-time backfill from published batches in the scoring window."""
    if db.get("share_history_seeded") == "1":
        return
    try:
        if not db.row("SELECT COUNT(*) AS c FROM share_history")["c"]:
            cutoff = (datetime.now().astimezone() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
            batches = db.rows(
                """SELECT id, started_at, finished_at FROM batches
                   WHERE published=1 AND COALESCE(finished_at, started_at) >= ?
                   ORDER BY id DESC LIMIT 200""",
                (cutoff,),
            )
            for batch in reversed(batches):
                selected, _ = collect_nodes(batch["id"])
                stamp = batch["finished_at"] or batch["started_at"]
                rows = db.rows(
                    """SELECT n.airport_id, COUNT(*) AS c FROM nodes n
                       JOIN airports a ON a.id=n.airport_id AND a.enabled=1
                       WHERE n.created_at <= ? GROUP BY n.airport_id""",
                    (stamp,),
                )
                pool_counts = {row["airport_id"]: row["c"] for row in rows}
                total_nodes = sum(pool_counts.values())
                if not total_nodes:
                    pool_counts, total_nodes = _batch_pool_counts(batch["id"])
                if not total_nodes:
                    continue
                record_share_snapshot(
                    selected, batch["id"], "backfill",
                    ts=stamp, pool_counts=pool_counts, total_nodes=total_nodes,
                    available_known=False,
                )
    except Exception as exc:  # history is diagnostic, never block the dashboard
        db.append_log("WARN", "仪表盘", f"占比历史回填失败：{exc}")
    finally:
        db.set_setting("share_history_seeded", "1")


def share_history(days: int = 7, complete_only: bool = False) -> list[dict]:
    """Return per-publish snapshots in the rolling window, oldest first.

    complete_only keeps only snapshots produced by a fully completed detection batch;
    stopped, interrupted and single-node/manual publishes are excluded from scoring.
    """
    cutoff = (datetime.now().astimezone() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    if complete_only:
        sql = """SELECT h.ts, h.batch_id, h.total_nodes, h.total_published, h.total_available,
                        h.airport_id, h.pool_count, h.published_count, h.available_count, h.available_known
                 FROM share_history h
                 JOIN batches b ON b.id = h.batch_id
                 WHERE h.ts >= ? AND b.status = 'complete'
                   AND b.target_count > 0 AND b.processed_count >= b.target_count
                 ORDER BY h.ts, h.id"""
    else:
        sql = """SELECT ts, batch_id, total_nodes, total_published, total_available,
                        airport_id, pool_count, published_count, available_count, available_known
                 FROM share_history WHERE ts >= ? ORDER BY ts, id"""
    rows = db.rows(sql, (cutoff,))
    points: dict[str, dict] = {}
    for row in rows:
        point = points.setdefault(row["ts"], {
            "ts": row["ts"], "batch_id": row["batch_id"], "total_nodes": row["total_nodes"],
            "total_published": row["total_published"], "total_available": row["total_available"],
            "available_known": row["available_known"], "airports": {},
        })
        point["airports"][str(row["airport_id"])] = {
            "pool_count": row["pool_count"], "published_count": row["published_count"],
            "available_count": row["available_count"],
        }
    return list(points.values())


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
        return {"ok": False, "reason": "当前还没有已生成的订阅"}

    ids = previous.get("node_ids")
    names = {int(x["id"]): x["name"] for x in (previous.get("node_names") or [])}
    if ids:
        nodes = [n for n in (_snapshot_entry(int(i), names.get(int(i), "")) for i in ids) if n]
    else:
        # Older manifests did not record ids: recover the live file first, so nodes that
        # failed later are still preserved instead of being silently dropped.
        nodes = _live_snapshot() or collect_nodes()[0]

    if any(n["id"] == node_id for n in nodes):
        return {**previous, "ok": True, "added": False, "reason": "该节点已在订阅中"}

    fresh = collect_nodes()[0]
    entry = next((n for n in fresh if n["id"] == node_id), None)
    if not entry:
        return {"ok": False, "reason": "节点未达到下发条件"}
    kept, dropped = apply_name_filter([entry])
    if not kept:
        return {"ok": False,
                "reason": f"该节点命中下发过滤关键词「{dropped[0]['keyword']}」，请先到设置页移除对应关键词"}
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
    out_nodes, dropped = apply_name_filter(nodes)
    if dropped:
        sample = "、".join(str(d["name"]) for d in dropped[:3])
        db.append_log("INFO", "下发",
                      f"关键词过滤剔除 {len(dropped)} 个节点（{sample}"
                      f"{'…' if len(dropped) > 3 else ''}），评分统计仍按 {len(nodes)} 个达标节点计算")
    stats = {**stats, "filtered": len(dropped), "after_filter": len(out_nodes),
             "filter_hits": dropped[:10]}
    sub_name = db.get("sub_name", "NodePool Unified") or "NodePool Unified"
    previous = meta()
    if not out_nodes and previous.get("nodes"):
        db.append_log("WARN", "下发",
                      "本批次没有可下发的节点（已达标的节点可能全部被关键词过滤），保留原有订阅不变")
        try:
            # 仍按未过滤的达标节点记录快照，保证下拉/评分曲线不因过滤出现断点
            record_share_snapshot(nodes, batch_id, reason, total_nodes=stats.get("total"))
        except Exception as exc:
            db.append_log("WARN", "仪表盘", f"占比历史写入失败：{exc}")
        return {**previous, "skipped": True, "stats": stats}
    written = {}
    for fmt, (filename, fn) in FORMATS.items():
        content = fn(out_nodes, sub_name) if fmt != "base64" else fn(out_nodes)
        validate(fmt, content, out_nodes)
        target = config.SUB_DIR / filename
        tmp = config.SUB_DIR / f".{filename}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, target)
        written[fmt] = str(target)
    manifest = {
        "name": sub_name,
        "nodes": len(out_nodes),
        "node_ids": [n["id"] for n in out_nodes],
        "node_names": [{"id": n["id"], "name": n["display_name"]} for n in out_nodes],
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
    db.set_setting("sub_node_count", str(len(out_nodes)))
    reason_cn = {
        "airport toggle": "机场启停", "manual": "手动", "manual rebuild": "手动重建",
        "restore": "数据库恢复", "schedule": "定时检测", "scheduled publish": "定时下发",
    }.get(reason, reason)
    if str(reason).startswith("batch#"):
        reason_cn = "批次 #" + str(reason)[6:]
    filtered_note = f"，过滤 {len(dropped)} 个" if dropped else ""
    db.append_log("INFO", "下发",
                  f"订阅已下发（{len(out_nodes)} 个节点{filtered_note}，原因：{reason_cn}）")
    try:
        record_share_snapshot(nodes, batch_id, reason, ts=manifest["updated_at"], total_nodes=stats.get("total"))
    except Exception as exc:
        db.append_log("WARN", "仪表盘", f"占比历史写入失败：{exc}")
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
