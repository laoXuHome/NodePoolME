"""P6.2.2 unified subscription service.

Source of truth:
* node selection - current P6.1 six-category preferred entries;
* keyword gate  - current P6.2.1 preview for the same P6.1 version/revision;
* protocol data - the stable P1 node ID linked by P6.1, used only to resolve
                  protocol/server parameters for rendering.

No P1-P6.1 write, no detection, no competition, no legacy publish path.
"""
from __future__ import annotations

import sqlite3
from collections import Counter, OrderedDict

from backend.competition import store as p5_store
from backend.database import db
from backend.final_filter import store as filter_store
from backend.final_results import store as final_store
from backend.testers import speedtest
from backend.unified_subscription import formats, store

CATEGORIES = p5_store.CATEGORIES
CATEGORY_LABELS = p5_store.CATEGORY_LABELS
BUSINESS_LABELS = (("gpt", "GPT"), ("youtube", "YT"), ("telegram", "TG"),
                   ("github", "GH"))


class UnifiedError(Exception):
    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _ensure(conn: sqlite3.Connection) -> None:
    final_store.ensure_schema(conn)
    filter_store.ensure_schema(conn)
    store.ensure_schema(conn)


def _state(conn: sqlite3.Connection) -> dict:
    return filter_store.state_row(conn)


def _display_prefix(grade: str | None) -> str:
    item = speedtest.GRADE_DEFS.get(str(grade or ""))
    return item["emoji"] if item else speedtest.PENDING_PREFIX


def _label_codes(categories: list[str]) -> list[str]:
    return [code for code, _ in BUSINESS_LABELS if code in categories]


def _merge_entries(entries: list[dict]) -> list[dict]:
    merged: "OrderedDict[int, dict]" = OrderedDict()
    for entry in entries:
        node_id = int(entry["node_id"])
        item = merged.get(node_id)
        if not item:
            item = dict(entry)
            item["categories"] = []
            item["business_labels"] = []
            merged[node_id] = item
        category = entry["category"]
        if category not in item["categories"]:
            item["categories"].append(category)
        if category not in item["business_labels"]:
            item["business_labels"].append(category)
    ordered = sorted(merged.values(), key=lambda x: (-(x.get("score") or -1), x["node_id"]))
    return ordered


def _filter_hits(rows: list[dict]) -> list[dict]:
    """Union all keyword/field causes; the same node may repeat across categories."""
    unique = []
    seen = set()
    for row in rows:
        for hit in store.loads(row.get("hits_json"), []) or []:
            key = (hit.get("rule_id"), hit.get("field"), hit.get("value"))
            if key in seen:
                continue
            seen.add(key)
            unique.append(hit)
    return unique


def _enrich(conn: sqlite3.Connection, nodes: list[dict]) -> dict[int, dict]:
    ids = [int(n["node_id"]) for n in nodes]
    if not ids:
        return {}
    rows = conn.execute(
        "SELECT id, protocol, server, port, params FROM np_nodes WHERE id IN (%s)"
        % ",".join("?" for _ in ids),
        tuple(ids),
    ).fetchall()
    out = {}
    for row in rows:
        out[int(row["id"])] = {
            "protocol": row["protocol"],
            "server": row["server"],
            "port": row["port"],
            "params": store.loads(row["params"], {}) or {},
        }
    return out


def _normalise_param(value):
    """Canonicalise effective values without changing their meaning."""
    if isinstance(value, dict):
        out = {}
        for key in sorted(value, key=lambda x: str(x)):
            item = _normalise_param(value[key])
            if item is not None and item != "":
                out[str(key)] = item
        return out
    if isinstance(value, (list, tuple)):
        items = [_normalise_param(v) for v in value]
        return [v for v in items if v is not None and v != ""]
    if isinstance(value, str):
        # Connection strings (passwords, UUIDs, paths) are case/space sensitive;
        # only structural emptiness is removed.
        return value
    if isinstance(value, (bool, int, float)):
        return value
    if value is None:
        return None
    return str(value)


def _normalise_params(value) -> dict:
    if isinstance(value, str):
        value = store.loads(value, {})
    if not isinstance(value, dict):
        return {}
    result = _normalise_param(value)
    return result if isinstance(result, dict) else {}


def connection_config(node: dict) -> dict:
    """All effective connection fields used to render a client profile."""
    try:
        port = int(node.get("port") or 0)
    except (TypeError, ValueError):
        port = 0
    return {
        "protocol": str(node.get("protocol") or "").strip().lower(),
        "server": str(node.get("server") or "").strip(),
        "port": port,
        "params": _normalise_params(node.get("params") or {}),
    }


def connection_fingerprint(node: dict) -> str:
    return store.sha256(store.canonical_json(connection_config(node)))


def batch_config_fingerprint(nodes: list[dict]) -> str:
    parts = [f"{int(n['node_id'])}:{n.get('config_sha256')}" for n in
             sorted(nodes, key=lambda x: int(x["node_id"]))]
    return store.sha256("\n".join(parts))


def _display_name(node: dict) -> str:
    marker = _display_prefix(node.get("speed_grade"))
    airport = node.get("airport_name") or "未知机场"
    labels = node.get("labels") or []
    raw = node.get("node_name") or f"节点 {node['node_id']}"
    if labels:
        return f"{marker}{airport}·{'·'.join(labels)}·{raw}"
    return f"{marker}{airport}·{raw}"


def _make_snapshot(conn: sqlite3.Connection) -> dict:
    _ensure(conn)
    filter_store.ensure_preview(conn)
    ver = final_store.latest_version(conn)
    state = _state(conn)
    if not ver:
        raise UnifiedError("p6_version_missing", "当前没有 P6.1 最终优选版本，无法生成统一订阅。")
    if state.get("p6_version_id") != int(ver["id"]) or not state.get("computed_at"):
        raise UnifiedError("filter_preview_stale",
                           "P6.2.1 过滤预览尚未绑定到当前 P6.1 版本，禁止生成。")
    if int(state.get("rule_revision") or 0) != filter_store.revision(conn):
        raise UnifiedError("filter_revision_stale",
                           "P6.2.1 规则版本与过滤预览不一致，禁止生成。")
    entries = final_store.entries_of(conn, int(ver["id"]))
    merged_nodes = _merge_entries(entries)
    rows = conn.execute(
        """SELECT node_id,category,status,hits_json,p6_version_id,rule_revision
           FROM np621_results WHERE p6_version_id=?""",
        (int(ver["id"]),),
    ).fetchall()
    filter_map: dict[int, list[dict]] = {}
    for row in rows:
        if int(row["rule_revision"]) != int(state.get("rule_revision") or 0):
            continue
        filter_map.setdefault(int(row["node_id"]), []).append(dict(row))
    if len(filter_map) < len(merged_nodes):
        missing = [n["node_id"] for n in merged_nodes if n["node_id"] not in filter_map]
        raise UnifiedError("filter_result_missing",
                           f"P6.2.1 过滤结果缺失 {len(missing)} 个节点，请重新计算后再生成。")
    enrichment = _enrich(conn, merged_nodes)
    missing_config = []
    for node in merged_nodes:
        rows_for_node = filter_map.get(int(node["node_id"]), [])
        node["filtered"] = any(r["status"] == "filtered" for r in rows_for_node)
        node["hits"] = _filter_hits(rows_for_node)
        categories = [c for c in CATEGORIES if c in node["categories"]]
        node["categories"] = categories
        node["labels"] = [_label for code, _label in BUSINESS_LABELS
                          if code in categories]
        node["label_codes"] = _label_codes(categories)
        meta = enrichment.get(int(node["node_id"]))
        node["config_available"] = meta is not None
        if meta:
            # Endpoint and protocol belong to the connection snapshot, not to the
            # immutable P6.1 ranking record.  P1 is read-only here.
            node["protocol"] = meta.get("protocol")
            node["server"] = meta.get("server")
            node["port"] = meta.get("port")
            node["params"] = meta.get("params") or {}
        else:
            node["protocol"] = None
            node["server"] = None
            node["port"] = None
            node["params"] = {}
            missing_config.append(int(node["node_id"]))
        node["params"] = _normalise_params(node.get("params") or {})
        node["config_sha256"] = connection_fingerprint(node)
        node["display_name"] = _display_name(node)
        node["unsupported"] = formats.unsupported_reasons(node)
        if not meta:
            node["unsupported"] = {
                fmt: {"code": "config_missing", "label": "节点连接配置不存在"}
                for fmt in formats.FORMAT_KEYS
            }
    # A client profile cannot contain duplicate names; suffix is applied only to the
    # final display name and is shared by all four formats.
    used: dict[str, int] = {}
    for node in merged_nodes:
        base = node["display_name"]
        used[base] = used.get(base, 0) + 1
        if used[base] > 1:
            node["display_name"] = f"{base} #{used[base]}"
    allowed = [n for n in merged_nodes if not n["filtered"]]
    output = [n for n in allowed if formats.can_output(n)]
    unsupported = formats.unsupported_summary(allowed)
    category_counts = {code: 0 for code in CATEGORIES}
    for entry in entries:
        category_counts[entry["category"]] = category_counts.get(entry["category"], 0) + 1
    labels = Counter()
    multi = 0
    for node in output:
        for code in node["label_codes"]:
            labels[code] += 1
        if len(node["label_codes"]) > 1:
            multi += 1
    for node in merged_nodes:
        if node["filtered"]:
            node["status"] = "filtered"
        elif node["unsupported"]:
            node["status"] = "unsupported"
        else:
            node["status"] = "allowed"
    node_set_sha = store.sha256("\n".join(
        f"{n['node_id']}:{n['display_name']}" for n in output
    ))
    config_sha = batch_config_fingerprint(merged_nodes)
    return {
        "version": ver,
        "state": state,
        "entries": entries,
        "category_counts": category_counts,
        "merged_count": len(entries),
        "unique_count": len(merged_nodes),
        "duplicate_count": len(entries) - len(merged_nodes),
        "filtered_count": len(merged_nodes) - len(allowed),
        "candidate_count": len(allowed),
        "excluded_count": len(allowed) - len(output),
        "output_count": len(output),
        "gpt_count": labels.get("gpt", 0),
        "yt_count": labels.get("youtube", 0),
        "tg_count": labels.get("telegram", 0),
        "gh_count": labels.get("github", 0),
        "multi_label_count": multi,
        "nodes": merged_nodes,
        "output": output,
        "unsupported": unsupported,
        "node_set_sha256": node_set_sha,
        "config_sha256": config_sha,
        "config_node_count": len(merged_nodes),
        "config_missing_ids": missing_config,
    }


def _public_snapshot(snapshot: dict) -> dict:
    return {
        "p6": {"version_id": int(snapshot["version"]["id"]),
               "version_no": snapshot["version"]["version_no"],
               "created_at": snapshot["version"].get("created_at")},
        "p621": {"revision": int(snapshot["state"].get("rule_revision") or 0),
                 "computed_at": snapshot["state"].get("computed_at")},
        "category_counts": snapshot["category_counts"],
        "merged_count": snapshot["merged_count"],
        "unique_count": snapshot["unique_count"],
        "duplicate_count": snapshot["duplicate_count"],
        "filtered_count": snapshot["filtered_count"],
        "candidate_count": snapshot["candidate_count"],
        "compatibility_excluded_count": snapshot["excluded_count"],
        "output_count": snapshot["output_count"],
        "labels": {"gpt": snapshot["gpt_count"], "yt": snapshot["yt_count"],
                   "tg": snapshot["tg_count"], "gh": snapshot["gh_count"],
                   "multi": snapshot["multi_label_count"]},
        "unsupported": snapshot["unsupported"],
        "node_set_sha256": snapshot["node_set_sha256"],
        "config_sha256": snapshot["config_sha256"],
        "config_node_count": snapshot["config_node_count"],
        "config_missing_count": len(snapshot["config_missing_ids"]),
        "nodes": [store.node_payload(n) for n in snapshot["nodes"]],
    }


def _batch_stale_details(conn: sqlite3.Connection, batch: dict,
                         snapshot: dict | None = None) -> dict:
    """Compare batch-bound versions and the full P1 connection snapshot."""
    codes: list[str] = []
    reasons: list[str] = []
    if snapshot is not None:
        if int(batch["p6_version_id"]) != int(snapshot["version"]["id"]):
            codes.append("p6_version_changed")
            reasons.append("P6.1 最终优选版本已变化")
        if int(batch.get("p621_revision") or 0) != int(
                snapshot["state"].get("rule_revision") or 0):
            codes.append("p621_revision_changed")
            reasons.append("P6.2.1 关键词规则修订号已变化")
    nodes = store.nodes_of(conn, int(batch["id"]))
    if not batch.get("config_sha256") or int(batch.get("config_node_count") or 0) != len(nodes):
        codes.append("config_snapshot_missing")
        reasons.append("该批次缺少完整连接配置快照")
        return _stale_payload(codes, reasons, nodes, [], [])
    if any(not n.get("config_sha256") or n.get("params_json") is None for n in nodes):
        codes.append("config_snapshot_missing")
        reasons.append("该批次存在未固化连接配置的节点")
        return _stale_payload(codes, reasons, nodes, [], [])
    stored_fingerprint = batch_config_fingerprint(nodes)
    if stored_fingerprint != batch.get("config_sha256"):
        codes.append("config_snapshot_invalid")
        reasons.append("批次连接配置快照校验不一致")
        return _stale_payload(codes, reasons, nodes, [], [])
    current = _enrich(conn, nodes)
    changed: list[int] = []
    missing: list[int] = []
    for node in nodes:
        node_id = int(node["node_id"])
        meta = current.get(node_id)
        if not meta:
            missing.append(node_id)
            continue
        current_fp = connection_fingerprint({
            "protocol": meta.get("protocol"), "server": meta.get("server"),
            "port": meta.get("port"), "params": meta.get("params") or {},
        })
        if current_fp != node.get("config_sha256"):
            changed.append(node_id)
    if changed:
        codes.append("node_config_changed")
        reasons.append(f"{len(changed)} 个节点的连接配置已变化")
    if missing:
        codes.append("node_config_missing")
        reasons.append(f"{len(missing)} 个节点的当前连接配置已缺失")
    return _stale_payload(codes, reasons, nodes, changed, missing)


def _stale_payload(codes: list[str], reasons: list[str], nodes: list[dict],
                   changed: list[int], missing: list[int]) -> dict:
    stale = bool(codes)
    reason = "；".join(reasons)
    if stale:
        reason = f"{reason}，请重新生成统一订阅。"
    return {
        "stale": stale,
        "codes": codes,
        "code": codes[0] if codes else None,
        "reason": reason or None,
        "config_changed_count": len(changed),
        "config_changed_node_ids": changed,
        "config_missing_count": len(missing),
        "config_missing_node_ids": missing,
        "config_checked_count": len(nodes),
    }


def _attach_stale(payload: dict, details: dict) -> dict:
    payload["stale"] = details["stale"]
    payload["stale_code"] = details["code"]
    payload["stale_codes"] = details["codes"]
    payload["stale_reason"] = details["reason"]
    payload["config_changed_count"] = details["config_changed_count"]
    payload["config_missing_count"] = details["config_missing_count"]
    return payload


def _batch_view(conn: sqlite3.Connection, batch: dict, snapshot: dict | None = None) -> dict:
    payload = store.batch_payload(batch)
    payload["outputs"] = [store.output_payload(o) for o in store.outputs_of(conn, batch["id"])]
    details = _batch_stale_details(conn, batch, snapshot)
    return _attach_stale(payload, details)


def summary() -> dict:
    with db.transaction() as conn:
        snapshot = _make_snapshot(conn)
        current = _public_snapshot(snapshot)
        batch = store.latest_batch(conn)
        batch_payload = _batch_view(conn, batch, snapshot) if batch else None
        nodes = store.nodes_of(conn, batch["id"]) if batch else []
        status = "not_generated"
        if batch:
            status = "stale" if batch_payload["stale"] else batch["status"]
        return {
            "status": status,
            "current": current,
            "batch": batch_payload,
            "nodes": [store.node_payload(n) for n in nodes] if batch else current["nodes"],
            "formats": batch_payload["outputs"] if batch_payload else [],
            "format_catalog": [
                {"key": key, "label": formats.FORMAT_LABELS[key]}
                for key in formats.FORMAT_KEYS
            ],
            "stale": batch_payload["stale"] if batch_payload else False,
            "stale_code": batch_payload["stale_code"] if batch_payload else None,
            "stale_reason": batch_payload["stale_reason"] if batch_payload else None,
            "config_changed_count": (batch_payload["config_changed_count"]
                                     if batch_payload else 0),
            "config_missing_count": (batch_payload["config_missing_count"]
                                     if batch_payload else 0),
            "boundary": {
                "source": "P6.1 六类最终优选",
                "keyword_gate": "P6.2.1 当前规则预览",
                "connection": "P1 当前协议、服务器、端口与参数，生成时冻结",
                "public_subscription": False,
                "client_pull": False,
            },
        }


def generate(trigger: str = "manual") -> dict:
    with db.transaction() as conn:
        snapshot = _make_snapshot(conn)
        if snapshot["config_missing_ids"]:
            raise UnifiedError(
                "node_config_missing",
                f"{len(snapshot['config_missing_ids'])} 个节点缺少当前连接配置，禁止生成统一订阅。",
            )
        batch_no = store.next_batch_no(conn)
        sub_name = db.get("sub_name", "NodePoolME 统一订阅") or "NodePoolME 统一订阅"
        stamp = store.now()
        output_nodes = snapshot["output"]
        built: dict[str, dict] = {}
        for fmt in formats.FORMAT_KEYS:
            try:
                content = formats.build(fmt, output_nodes, sub_name)
                formats.validate(fmt, content, output_nodes)
                built[fmt] = {
                    "format": fmt, "status": "success", "node_count": len(output_nodes),
                    "generated_at": stamp, "content": content,
                    "content_sha256": store.sha256(content),
                    "content_bytes": len(content.encode("utf-8")), "failure_reason": None,
                }
            except Exception as exc:  # a real failure must be recorded, never masked
                built[fmt] = {
                    "format": fmt, "status": "failed", "node_count": 0,
                    "generated_at": stamp, "content": None, "content_sha256": None,
                    "content_bytes": 0, "failure_reason": str(exc),
                }
        failures = [f for f in built.values() if f["status"] == "failed"]
        if len(failures) == len(formats.FORMAT_KEYS):
            batch_status = "failed"
        elif failures:
            batch_status = "partial"
        elif not output_nodes:
            batch_status = "empty"
        else:
            batch_status = "success"
        reason = None
        if failures:
            reason = "；".join(f"{formats.FORMAT_LABELS[f['format']]}：{f['failure_reason']}"
                               for f in failures)
        elif not output_nodes:
            reason = "当前统一节点集合为空"
        if snapshot["excluded_count"]:
            note = f"{snapshot['excluded_count']} 个节点因无法被四种格式同时正确表达而排除"
            reason = f"{reason}；{note}" if reason else note
        batch_id = store.insert_batch(conn, {
            "batch_no": batch_no, "created_at": stamp, "trigger": trigger,
            "p6_version_id": snapshot["version"]["id"],
            "p6_version_no": snapshot["version"]["version_no"],
            "p621_revision": snapshot["state"].get("rule_revision") or 0,
            "p621_computed_at": snapshot["state"].get("computed_at"),
            "category_counts": snapshot["category_counts"],
            "merged_count": snapshot["merged_count"],
            "unique_count": snapshot["unique_count"],
            "duplicate_count": snapshot["duplicate_count"],
            "filtered_count": snapshot["filtered_count"],
            "candidate_count": snapshot["candidate_count"],
            "compatibility_excluded_count": snapshot["excluded_count"],
            "output_count": snapshot["output_count"],
            "gpt_count": snapshot["gpt_count"], "yt_count": snapshot["yt_count"],
            "tg_count": snapshot["tg_count"], "gh_count": snapshot["gh_count"],
            "multi_label_count": snapshot["multi_label_count"],
            "is_empty": not output_nodes, "status": batch_status,
            "status_reason": reason, "unsupported": snapshot["unsupported"],
            "node_set_sha256": snapshot["node_set_sha256"],
            "config_sha256": snapshot["config_sha256"],
            "config_node_count": snapshot["config_node_count"],
        })
        for fmt in formats.FORMAT_KEYS:
            item = built[fmt]
            store.insert_output(conn, batch_id, {
                "format": fmt, "status": item["status"], "node_count": item["node_count"],
                "generated_at": stamp,
                "p6_version_id": snapshot["version"]["id"],
                "p6_version_no": snapshot["version"]["version_no"],
                "p621_revision": snapshot["state"].get("rule_revision") or 0,
                "content_sha256": item["content_sha256"],
                "content_bytes": item["content_bytes"], "content_text": item["content"],
                "failure_reason": item["failure_reason"],
            })
        for node in snapshot["nodes"]:
            store.insert_node(conn, batch_id, node)
        store.append_event(conn, "batch_generated", batch_id, {
            "batch_no": batch_no, "format_status": {k: v["status"] for k, v in built.items()},
            "output_count": len(output_nodes),
        })
        batch = store.batch_row(conn, batch_id)
        return {
            "ok": batch_status not in ("failed",),
            "batch": _batch_view(conn, batch, snapshot),
            "formats": [store.output_payload(store.output_of(conn, batch_id, f))
                        for f in formats.FORMAT_KEYS],
        }


def format_content(fmt: str, batch_id: int | None = None) -> dict:
    if fmt not in formats.FORMAT_KEYS:
        raise UnifiedError("invalid_format", "未知的统一订阅格式", status=400)
    with db.transaction() as conn:
        snapshot = _make_snapshot(conn)
        batch = store.batch_row(conn, batch_id) if batch_id else store.latest_batch(conn)
        if not batch:
            raise UnifiedError("not_generated", "尚未生成统一订阅", status=404)
        output = store.output_of(conn, int(batch["id"]), fmt)
        if not output:
            raise UnifiedError("output_missing", "该批次缺少所请求的格式记录", status=404)
        payload = store.output_payload(output, include_content=True)
        details = _batch_stale_details(conn, batch, snapshot)
        _attach_stale(payload, details)
        payload["batch"] = _attach_stale(store.batch_payload(batch), details)
        return payload


def download(fmt: str) -> dict:
    if fmt not in formats.FORMAT_KEYS:
        raise UnifiedError("invalid_format", "未知的统一订阅格式", status=400)
    with db.transaction() as conn:
        snapshot = _make_snapshot(conn)
        batch = store.latest_batch(conn)
        if not batch:
            raise UnifiedError("not_generated", "尚未生成统一订阅", status=404)
        details = _batch_stale_details(conn, batch, snapshot)
        if details["stale"]:
            raise UnifiedError(
                "stale_generation",
                details["reason"] or "批次已过期，请先重新生成统一订阅。",
            )
        output = store.output_of(conn, int(batch["id"]), fmt)
        if not output:
            raise UnifiedError("output_missing", "当前批次缺少该格式", status=404)
        if output["status"] != "success":
            raise UnifiedError("output_failed",
                               output.get("failure_reason") or "该格式生成失败，无法下载。")
        return {
            "format": fmt,
            "filename": formats.FORMAT_FILENAMES[fmt],
            "media_type": formats.FORMAT_MEDIA[fmt],
            "content": output.get("content_text") or "",
            "batch_no": batch["batch_no"],
            "node_count": int(output.get("node_count") or 0),
        }


def history(limit: int = 20) -> dict:
    with db.transaction() as conn:
        snapshot = _make_snapshot(conn)
        rows = store.history(conn, limit)
        items = []
        for batch in rows:
            payload = store.batch_payload(batch)
            payload["outputs"] = [store.output_payload(o) for o in batch["outputs"]]
            _attach_stale(payload, _batch_stale_details(conn, batch, snapshot))
            items.append(payload)
        return {
            "items": items,
            "current": _public_snapshot(snapshot),
        }


def dashboard() -> dict:
    with db.transaction() as conn:
        try:
            snapshot = _make_snapshot(conn)
        except UnifiedError:
            batch = store.latest_batch(conn)
            outputs = store.outputs_of(conn, batch["id"]) if batch else []
            return {
                "status": "not_generated", "nodes": 0, "filtered": 0,
                "labels": {"gpt": 0, "yt": 0, "tg": 0, "gh": 0},
                "last_generated": None, "stale": True,
                "p6_version_no": None, "revision": 0,
                "formats": {o["format"]: o["status"] for o in outputs}
                or {k: "not_generated" for k in formats.FORMAT_KEYS},
            }
        batch = store.latest_batch(conn)
        if not batch:
            return {
                "status": "not_generated", "nodes": 0, "filtered": snapshot["filtered_count"],
                "labels": {"gpt": 0, "yt": 0, "tg": 0, "gh": 0},
                "last_generated": None, "stale": True,
                "p6_version_no": snapshot["version"]["version_no"],
                "revision": int(snapshot["state"].get("rule_revision") or 0),
                "formats": {k: "not_generated" for k in formats.FORMAT_KEYS},
            }
        outputs = store.outputs_of(conn, batch["id"])
        details = _batch_stale_details(conn, batch, snapshot)
        return {
            "status": "stale" if details["stale"] else batch["status"],
            "nodes": int(batch.get("output_count") or 0),
            "filtered": int(batch.get("filtered_count") or 0),
            "labels": {"gpt": int(batch.get("gpt_count") or 0),
                       "yt": int(batch.get("yt_count") or 0),
                       "tg": int(batch.get("tg_count") or 0),
                       "gh": int(batch.get("gh_count") or 0)},
            "last_generated": batch.get("created_at"),
            "stale": details["stale"],
            "stale_code": details["code"],
            "stale_reason": details["reason"],
            "config_changed_count": details["config_changed_count"],
            "config_missing_count": details["config_missing_count"],
            "p6_version_no": batch.get("p6_version_no"),
            "revision": int(batch.get("p621_revision") or 0),
            "formats": {o["format"]: o["status"] for o in outputs},
        }
