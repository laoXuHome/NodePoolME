"""P6.2.1 final-delivery keyword filter.

This module is a read-only downstream gate over the current P6.1 version.
It never writes P1-P5 state, never changes P6.1 entries or versions, and never
builds a subscription file.  ``np621_results`` is a replaceable preview cache;
rule changes recompute it without triggering detection or competition.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from backend.competition import store as p5_store
from backend.final_results import store as final_store

CATEGORIES = p5_store.CATEGORIES
CATEGORY_LABELS = p5_store.CATEGORY_LABELS
CATEGORY_TITLES = p5_store.CATEGORY_TITLES
CATEGORY_CODES = p5_store.CATEGORY_CODES

MATCH_FIELDS = (
    ("node_name", "节点名称"),
    ("airport_name", "机场名称"),
    ("server", "服务器地址"),
)

DEFAULT_RULES = (
    "剩余流量",
    "过期时间",
    "发布页",
    "官网",
    "Traffic",
    "Expire",
    "Official",
    "Subscribe",
)

_DDL = (
    """CREATE TABLE IF NOT EXISTS np621_rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        keyword TEXT NOT NULL,
        keyword_key TEXT NOT NULL UNIQUE,
        enabled INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS np621_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        rule_revision INTEGER NOT NULL DEFAULT 0,
        p6_version_id INTEGER,
        p6_version_no TEXT,
        computed_at TEXT,
        computed_trigger TEXT,
        internal_count INTEGER NOT NULL DEFAULT 0,
        filtered_count INTEGER NOT NULL DEFAULT 0,
        allowed_count INTEGER NOT NULL DEFAULT 0,
        counts_json TEXT,
        defaults_seeded INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS np621_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        computed_at TEXT NOT NULL,
        rule_revision INTEGER NOT NULL,
        p6_version_id INTEGER,
        category TEXT NOT NULL,
        node_id INTEGER NOT NULL,
        node_name TEXT,
        airport_id INTEGER,
        airport_name TEXT,
        server TEXT,
        port INTEGER,
        status TEXT NOT NULL,
        hits_json TEXT,
        score REAL,
        maturity REAL,
        p80_ms INTEGER,
        delay_grade TEXT,
        speed_grade TEXT,
        speed_mb_s REAL,
        UNIQUE (category, node_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np621_results_status ON np621_results (status, category)",
    """CREATE TABLE IF NOT EXISTS np621_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np621_events_at ON np621_events (id DESC)",
)


class FinalFilterError(Exception):
    """Stable P6.2.1 error."""

    def __init__(self, code: str, message: str, *, status: int = 409):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _keyword(value: str | None) -> str:
    text = (value or "").strip()
    if not text:
        raise FinalFilterError("keyword_required", "关键词不能为空", status=400)
    if len(text) > 80:
        raise FinalFilterError("keyword_too_long", "关键词不能超过 80 个字符", status=400)
    return text


def _key(value: str) -> str:
    return value.casefold()


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _DDL:
        conn.execute(sql)
    conn.execute(
        """INSERT OR IGNORE INTO np621_state
           (id, rule_revision, internal_count, filtered_count, allowed_count, defaults_seeded)
           VALUES (1, 0, 0, 0, 0, 0)"""
    )
    state = conn.execute("SELECT defaults_seeded FROM np621_state WHERE id=1").fetchone()
    if int(state["defaults_seeded"] or 0) == 0:
        count = int(conn.execute("SELECT COUNT(*) AS c FROM np621_rules").fetchone()["c"] or 0)
        if count == 0:
            stamp = _stamp()
            for keyword in DEFAULT_RULES:
                conn.execute(
                    """INSERT INTO np621_rules(keyword,keyword_key,enabled,created_at,updated_at)
                       VALUES (?,?,?,?,?)""",
                    (keyword, _key(keyword), 1, stamp, stamp),
                )
        conn.execute("UPDATE np621_state SET defaults_seeded=1 WHERE id=1")


def state_row(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM np621_state WHERE id=1").fetchone()
    return dict(row) if row else {
        "id": 1, "rule_revision": 0, "p6_version_id": None, "p6_version_no": None,
        "computed_at": None, "computed_trigger": None, "internal_count": 0,
        "filtered_count": 0, "allowed_count": 0, "counts_json": None,
        "defaults_seeded": 0,
    }


def revision(conn: sqlite3.Connection) -> int:
    return int(state_row(conn).get("rule_revision") or 0)


def bump_revision(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE np621_state SET rule_revision=rule_revision+1 WHERE id=1")
    return revision(conn)


def record_event(conn: sqlite3.Connection, event: str, detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO np621_events(at,event,detail_json) VALUES (?,?,?)",
        (_stamp(), event, _json(detail) if detail is not None else None),
    )


def list_rules(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT id,keyword,enabled,created_at,updated_at FROM np621_rules ORDER BY id"
    ).fetchall()
    return [
        {
            "id": int(row["id"]),
            "keyword": row["keyword"],
            "enabled": bool(row["enabled"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in rows
    ]


def enabled_rules(conn: sqlite3.Connection) -> list[dict]:
    return [item for item in list_rules(conn) if item["enabled"]]


def find_rule(conn: sqlite3.Connection, rule_id: int) -> dict | None:
    row = conn.execute(
        "SELECT id,keyword,enabled,created_at,updated_at FROM np621_rules WHERE id=?",
        (int(rule_id),),
    ).fetchone()
    if not row:
        return None
    return {
        "id": int(row["id"]), "keyword": row["keyword"], "enabled": bool(row["enabled"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def add_rule(conn: sqlite3.Connection, keyword: str, enabled: bool) -> dict:
    text = _keyword(keyword)
    key = _key(text)
    if conn.execute("SELECT id FROM np621_rules WHERE keyword_key=?", (key,)).fetchone():
        raise FinalFilterError("keyword_exists", "关键词已存在（不区分大小写匹配）", status=409)
    stamp = _stamp()
    rule_id = conn.execute(
        """INSERT INTO np621_rules(keyword,keyword_key,enabled,created_at,updated_at)
           VALUES (?,?,?,?,?)""",
        (text, key, int(bool(enabled)), stamp, stamp),
    ).lastrowid
    record_event(conn, "rule_created", {"rule_id": int(rule_id), "keyword": text, "enabled": bool(enabled)})
    return find_rule(conn, int(rule_id))


def update_rule(conn: sqlite3.Connection, rule_id: int, *, keyword: str | None = None,
                enabled: bool | None = None) -> dict:
    row = find_rule(conn, rule_id)
    if not row:
        raise FinalFilterError("rule_not_found", "关键词规则不存在", status=404)
    if keyword is None and enabled is None:
        raise FinalFilterError("nothing_to_update", "没有需要更新的字段", status=400)
    text = row["keyword"] if keyword is None else _keyword(keyword)
    key = _key(text)
    other = conn.execute(
        "SELECT id FROM np621_rules WHERE keyword_key=? AND id<>?", (key, int(rule_id))
    ).fetchone()
    if other:
        raise FinalFilterError("keyword_exists", "关键词已存在（不区分大小写匹配）", status=409)
    next_enabled = int(bool(row["enabled"] if enabled is None else enabled))
    conn.execute(
        "UPDATE np621_rules SET keyword=?,keyword_key=?,enabled=?,updated_at=? WHERE id=?",
        (text, key, next_enabled, _stamp(), int(rule_id)),
    )
    record_event(
        conn, "rule_updated",
        {"rule_id": int(rule_id), "keyword": text, "enabled": bool(next_enabled)},
    )
    return find_rule(conn, int(rule_id))


def delete_rule(conn: sqlite3.Connection, rule_id: int) -> dict:
    row = find_rule(conn, rule_id)
    if not row:
        raise FinalFilterError("rule_not_found", "关键词规则不存在", status=404)
    conn.execute("DELETE FROM np621_rules WHERE id=?", (int(rule_id),))
    record_event(conn, "rule_deleted", {"rule_id": int(rule_id), "keyword": row["keyword"]})
    return row


def restore_defaults(conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM np621_rules")
    stamp = _stamp()
    for keyword in DEFAULT_RULES:
        conn.execute(
            """INSERT INTO np621_rules(keyword,keyword_key,enabled,created_at,updated_at)
               VALUES (?,?,?,?,?)""",
            (keyword, _key(keyword), 1, stamp, stamp),
        )
    record_event(conn, "defaults_restored", {"count": len(DEFAULT_RULES)})
    return len(DEFAULT_RULES)


def latest_p6_version(conn: sqlite3.Connection) -> dict | None:
    try:
        row = conn.execute(
            "SELECT id,version_no,created_at,total_nodes FROM np6_versions ORDER BY id DESC LIMIT 1"
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def _match_entry(entry: dict, rules: list[dict]) -> list[dict]:
    hits = []
    values = {
        "node_name": str(entry.get("node_name") or ""),
        "airport_name": str(entry.get("airport_name") or ""),
        "server": str(entry.get("server") or ""),
    }
    folded = {field: values[field].casefold() for field, _ in MATCH_FIELDS}
    for rule in rules:
        key = _key(rule["keyword"])
        for field, label in MATCH_FIELDS:
            if values[field] and key in folded[field]:
                hits.append({
                    "rule_id": int(rule["id"]),
                    "keyword": rule["keyword"],
                    "field": field,
                    "field_label": label,
                    "value": values[field],
                })
    return hits


def compute_preview(conn: sqlite3.Connection, trigger: str) -> dict:
    """Recompute the derived preview from the current P6.1 version and rules."""
    state = state_row(conn)
    ver = latest_p6_version(conn)
    computed_at = _stamp()
    rev = int(state.get("rule_revision") or 0)
    conn.execute("DELETE FROM np621_results")
    counts = {
        code: {"internal": 0, "allowed": 0, "filtered": 0}
        for code in CATEGORIES
    }
    if ver:
        entries = final_store.entries_of(conn, int(ver["id"]))
        rules = enabled_rules(conn)
        for entry in entries:
            code = entry["category"]
            if code not in counts:
                continue
            hits = _match_entry(entry, rules)
            status = "filtered" if hits else "allowed"
            counts[code]["internal"] += 1
            counts[code][status] += 1
            conn.execute(
                """INSERT INTO np621_results(computed_at,rule_revision,p6_version_id,category,
                   node_id,node_name,airport_id,airport_name,server,port,status,hits_json,
                   score,maturity,p80_ms,delay_grade,speed_grade,speed_mb_s)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (computed_at, rev, int(ver["id"]), code, int(entry["node_id"]),
                 entry.get("node_name"), entry.get("airport_id"), entry.get("airport_name"),
                 entry.get("server"), entry.get("port"), status,
                 _json(hits) if hits else None, entry.get("score"), entry.get("maturity"),
                 entry.get("p80_ms"), entry.get("delay_grade"), entry.get("speed_grade"),
                 entry.get("speed_mb_s")),
            )
    internal = sum(item["internal"] for item in counts.values())
    filtered = sum(item["filtered"] for item in counts.values())
    allowed = sum(item["allowed"] for item in counts.values())
    conn.execute(
        """UPDATE np621_state SET p6_version_id=?, p6_version_no=?, computed_at=?,
           computed_trigger=?, internal_count=?, filtered_count=?, allowed_count=?,
           counts_json=? WHERE id=1""",
        (int(ver["id"]) if ver else None, ver.get("version_no") if ver else None,
         computed_at, trigger, internal, filtered, allowed, _json(counts)),
    )
    record_event(conn, "preview_recomputed", {
        "trigger": trigger, "version_no": ver.get("version_no") if ver else None,
        "revision": rev, "internal": internal, "filtered": filtered, "allowed": allowed,
    })
    return dict(state_row(conn))


def ensure_preview(conn: sqlite3.Connection) -> dict:
    state = state_row(conn)
    ver = latest_p6_version(conn)
    version_id = int(ver["id"]) if ver else None
    if (state.get("computed_at") is None
            or state.get("p6_version_id") != version_id
            or int(state.get("rule_revision") or 0) != revision(conn)):
        return compute_preview(conn, "read_refresh")
    return state


def _rule_hit_stats(conn: sqlite3.Connection) -> dict[int, dict]:
    rows = conn.execute(
        "SELECT category,node_id,hits_json FROM np621_results WHERE status='filtered'"
    ).fetchall()
    stats: dict[int, dict] = {}
    for row in rows:
        for hit in _loads(row["hits_json"], []) or []:
            rid = int(hit.get("rule_id"))
            item = stats.setdefault(rid, {
                "hit_record_count": 0,
                "_nodes": set(), "_categories": set(),
            })
            item["hit_record_count"] += 1
            item["_nodes"].add(int(row["node_id"]))
            item["_categories"].add(row["category"])
    for item in stats.values():
        # A hit count counts distinct filtered nodes; every reason is kept in
        # hit_record_count so repeated keyword/field reasons are not lost.
        item["hit_count"] = len(item["_nodes"])
        item["hit_node_count"] = len(item["_nodes"])
        item["matched_categories"] = [
            code for code in CATEGORIES if code in item["_categories"]
        ]
        item.pop("_nodes", None)
        item.pop("_categories", None)
    return stats


def _category_summary(conn: sqlite3.Connection) -> list[dict]:
    state = state_row(conn)
    counts = _loads(state.get("counts_json"), {}) or {}
    result = []
    for code in CATEGORIES:
        item = counts.get(code, {})
        result.append({
            "code": code,
            "label": CATEGORY_LABELS[code],
            "title": CATEGORY_TITLES[code],
            "short": CATEGORY_CODES[code],
            "internal": int(item.get("internal") or 0),
            "allowed": int(item.get("allowed") or 0),
            "filtered": int(item.get("filtered") or 0),
        })
    return result


def summary_payload(conn: sqlite3.Connection) -> dict:
    ensure_preview(conn)
    state = state_row(conn)
    rules = list_rules(conn)
    hit_stats = _rule_hit_stats(conn)
    rules_out = []
    for rule in rules:
        stats = hit_stats.get(rule["id"], {})
        rules_out.append({
            **rule,
            "hit_count": int(stats.get("hit_count") or 0),
            "hit_node_count": int(stats.get("hit_node_count") or 0),
            "hit_record_count": int(stats.get("hit_record_count") or 0),
            "matched_categories": stats.get("matched_categories") or [],
        })
    enabled = sum(1 for rule in rules if rule["enabled"])
    return {
        "rules": rules_out,
        "rule_stats": {
            "total": len(rules), "enabled": enabled, "disabled": len(rules) - enabled,
            "matched": sum(1 for item in rules_out if item["hit_count"] > 0),
        },
        "revision": int(state.get("rule_revision") or 0),
        "computed_at": state.get("computed_at"),
        "computed_trigger": state.get("computed_trigger"),
        "p6": {
            "version_id": state.get("p6_version_id"),
            "version_no": state.get("p6_version_no"),
        },
        "totals": {
            "internal": int(state.get("internal_count") or 0),
            "filtered": int(state.get("filtered_count") or 0),
            "allowed": int(state.get("allowed_count") or 0),
        },
        "categories": _category_summary(conn),
        "default_rules": list(DEFAULT_RULES),
        "boundary": {
            "p1_write": False, "p2_write": False, "p3_write": False, "p4_write": False,
            "p5_write": False, "p6_write": False, "p621_only": True,
            "subscription_generation": False, "subscription_publish": False,
            "p6_2_2": False, "p6_3": False,
        },
    }


def _result_payload(row: dict, computed_at: str) -> dict:
    hits = _loads(row.get("hits_json"), []) or []
    return {
        "category": row["category"],
        "node_id": int(row["node_id"]),
        "node_name": row.get("node_name") or f"节点 {row['node_id']}",
        "airport_id": row.get("airport_id"),
        "airport_name": row.get("airport_name"),
        "server": row.get("server"),
        "port": row.get("port"),
        "status": row["status"],
        "hits": hits,
        "matched_keywords": [hit["keyword"] for hit in hits],
        "matched_fields": [hit["field_label"] for hit in hits],
        "filtered_at": computed_at if row["status"] == "filtered" else None,
        "score": row.get("score"),
        "maturity": row.get("maturity"),
        "p80_ms": row.get("p80_ms"),
        "delay_grade": row.get("delay_grade"),
        "speed_grade": row.get("speed_grade"),
        "speed_mb_s": row.get("speed_mb_s"),
    }


def preview_payload(conn: sqlite3.Connection, category: str | None = None) -> dict:
    ensure_preview(conn)
    state = state_row(conn)
    computed_at = state.get("computed_at")
    sql = "SELECT * FROM np621_results"
    params: list[Any] = []
    if category:
        sql += " WHERE category=?"
        params.append(category)
    sql += " ORDER BY category, status, COALESCE(score, -1) DESC, node_id"
    rows = conn.execute(sql, tuple(params)).fetchall()
    by_code = {
        code: {"allowed_nodes": [], "filtered_nodes": []}
        for code in CATEGORIES
    }
    for row in rows:
        item = _result_payload(dict(row), computed_at)
        by_code.setdefault(item["category"], {"allowed_nodes": [], "filtered_nodes": []})
        if item["status"] == "filtered":
            by_code[item["category"]]["filtered_nodes"].append(item)
        else:
            by_code[item["category"]]["allowed_nodes"].append(item)
    categories = []
    for code in CATEGORIES:
        if category and code != category:
            continue
        item = by_code.get(code, {"allowed_nodes": [], "filtered_nodes": []})
        categories.append({
            "code": code,
            "label": CATEGORY_LABELS[code],
            "title": CATEGORY_TITLES[code],
            "short": CATEGORY_CODES[code],
            "internal": len(item["allowed_nodes"]) + len(item["filtered_nodes"]),
            "allowed": len(item["allowed_nodes"]),
            "filtered": len(item["filtered_nodes"]),
            "allowed_nodes": item["allowed_nodes"],
            "filtered_nodes": item["filtered_nodes"],
        })
    return {
        "computed_at": computed_at,
        "revision": int(state.get("rule_revision") or 0),
        "p6_version_id": state.get("p6_version_id"),
        "p6_version_no": state.get("p6_version_no"),
        "categories": categories,
        "totals": {
            "internal": int(state.get("internal_count") or 0),
            "filtered": int(state.get("filtered_count") or 0),
            "allowed": int(state.get("allowed_count") or 0),
        },
    }
