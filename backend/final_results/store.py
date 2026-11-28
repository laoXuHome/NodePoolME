"""P6.1 final preferred-result storage.

P6.1 is a read-only projection over P5 preferred states.  It never writes a
P5 row, never filters nodes and never produces a subscription file.  Versions
are immutable; an invalid source run cannot create or overwrite a version.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

from backend.competition import store as p5_store

CATEGORIES = p5_store.CATEGORIES
CATEGORY_LABELS = p5_store.CATEGORY_LABELS
CATEGORY_TITLES = p5_store.CATEGORY_TITLES
CATEGORY_CODES = p5_store.CATEGORY_CODES

SIGNATURE_VERSION = 2
IDENTITY_FIELDS = (
    ("node_name", "节点名称"),
    ("airport_id", "机场 ID"),
    ("airport_name", "机场名称"),
    ("server", "服务器地址"),
    ("port", "端口"),
)

_DDL = (
    """CREATE TABLE IF NOT EXISTS np6_versions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_no TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        trigger TEXT NOT NULL,
        valid INTEGER NOT NULL DEFAULT 1,
        is_empty INTEGER NOT NULL DEFAULT 0,
        total_nodes INTEGER NOT NULL DEFAULT 0,
        category_counts_json TEXT,
        change_counts_json TEXT,
        previous_version_id INTEGER,
        previous_version_no TEXT,
        source_run_id INTEGER,
        source_run_at TEXT,
        signature TEXT NOT NULL,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np6_versions_at ON np6_versions (id DESC)",
    """CREATE TABLE IF NOT EXISTS np6_entries (
        version_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        node_id INTEGER NOT NULL,
        node_name TEXT,
        airport_id INTEGER,
        airport_name TEXT,
        server TEXT,
        port INTEGER,
        score REAL,
        maturity REAL,
        p80_ms INTEGER,
        delay_grade TEXT,
        speed_grade TEXT,
        speed_mb_s REAL,
        preferred_since TEXT,
        last_reason TEXT,
        detail_json TEXT,
        PRIMARY KEY (version_id, category, node_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np6_entries_version ON np6_entries (version_id, category, node_id)",
    """CREATE TABLE IF NOT EXISTS np6_changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        version_id INTEGER NOT NULL,
        previous_version_id INTEGER,
        category TEXT NOT NULL,
        node_id INTEGER NOT NULL,
        change_type TEXT NOT NULL,
        at TEXT,
        trigger TEXT,
        node_name TEXT,
        airport_name TEXT,
        before_score REAL,
        after_score REAL,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np6_changes_version ON np6_changes (version_id, category, change_type)",
    """CREATE TABLE IF NOT EXISTS np6_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        version_id INTEGER,
        reason TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np6_events_at ON np6_events (id DESC)",
)


class FinalError(Exception):
    """Stable P6.1 error with a machine-readable code."""

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


def _stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _DDL:
        conn.execute(sql)
    # Forward-only repair for a partially created P6.1 development table.
    _ensure_column(conn, "np6_changes", "at", "TEXT")
    _ensure_column(conn, "np6_changes", "trigger", "TEXT")


def latest_run(conn: sqlite3.Connection) -> dict | None:
    try:
        row = conn.execute("SELECT * FROM np5_runs ORDER BY id DESC LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row else None


def p5_state_count(conn: sqlite3.Connection) -> int:
    try:
        row = conn.execute("SELECT COUNT(*) AS c FROM np5_state").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row["c"] or 0)


def collect_entries(conn: sqlite3.Connection) -> list[dict]:
    """Directly project every P5 preferred row; no extra filters are applied."""
    rows = conn.execute(
        """SELECT s.node_id, s.category, s.score, s.maturity, s.p80_ms,
                  s.delay_grade, s.speed_grade, s.speed_mb_s, s.preferred_since,
                  s.last_reason, s.detail_json,
                  n.current_name, n.first_name, n.airport_id, n.server, n.port,
                  n.source_active, a.name AS airport_name, a.enabled AS airport_enabled
           FROM np5_state s
           JOIN np_nodes n ON n.id = s.node_id
           LEFT JOIN airports a ON a.id = n.airport_id
           WHERE s.state = 'preferred'
           ORDER BY s.category, COALESCE(s.score, -1) DESC, s.node_id"""
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["node_name"] = item.get("current_name") or item.get("first_name") or f"节点 {item['node_id']}"
        item["detail"] = _loads(item.pop("detail_json"), {}) or {}
        item["source_active"] = bool(item.get("source_active"))
        item["airport_enabled"] = bool(item.get("airport_enabled", 1))
        result.append(item)
    return result


def _identity_scalar(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    return text if text else None


def identity_of(item: dict) -> list:
    """Saved identity fields that must invalidate a snapshot when they change."""
    return [
        int(item["node_id"]),
        _identity_scalar(item.get("node_name")),
        _identity_scalar(item.get("airport_id")),
        _identity_scalar(item.get("airport_name")),
        _identity_scalar(item.get("server")),
        _identity_scalar(item.get("port")),
    ]


def identity_map(entries: list[dict]) -> dict[tuple[str, int], list]:
    return {
        (str(item["category"]), int(item["node_id"])): identity_of(item)
        for item in entries
    }


def identity_changes(before: dict | None, after: dict | None) -> list[dict]:
    """Return human-readable field changes for the same node/category."""
    if not before or not after:
        return []
    result = []
    for field, label in IDENTITY_FIELDS:
        old = _identity_scalar(before.get(field))
        new = _identity_scalar(after.get(field))
        if old != new:
            result.append({
                "field": field,
                "label": label,
                "before": before.get(field),
                "after": after.get(field),
            })
    return result


def signature_of(entries: list[dict]) -> str:
    """Membership plus saved identity; score noise must not bump a version."""
    payload = {code: [] for code in CATEGORIES}
    for item in entries:
        payload.setdefault(item["category"], []).append(identity_of(item))
    for code in payload:
        payload[code].sort(key=lambda value: value[0])
    raw = _json({"signature_version": SIGNATURE_VERSION, "categories": payload}).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def needs_snapshot(conn: sqlite3.Connection, entries: list[dict], current: dict | None) -> bool:
    """Whether the P5 preferred projection differs from the current version.

    v2.044 versions used a membership-only signature.  On upgrade they must not
    create a duplicate version merely because the signature algorithm changed:
    for those versions compare the saved identity of the current entries instead.
    """
    if current is None:
        return True
    signature = signature_of(entries)
    if current.get("signature") == signature:
        return False
    detail = _loads(current.get("detail_json"), {}) or {}
    if int(detail.get("signature_version") or 1) >= SIGNATURE_VERSION:
        return True
    old_entries = entries_of(conn, int(current["id"]))
    return identity_map(entries) != identity_map(old_entries)


def category_counts(entries: list[dict]) -> dict[str, int]:
    counts = {code: 0 for code in CATEGORIES}
    for item in entries:
        counts[item["category"]] = counts.get(item["category"], 0) + 1
    return counts


def latest_version(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM np6_versions ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def version_row(conn: sqlite3.Connection, version_id: int | None = None) -> dict | None:
    if version_id is None:
        return latest_version(conn)
    row = conn.execute("SELECT * FROM np6_versions WHERE id = ?", (int(version_id),)).fetchone()
    return dict(row) if row else None


def version_summary(row: dict | None) -> dict | None:
    if not row:
        return None
    return {
        "id": int(row["id"]),
        "version_no": row["version_no"],
        "created_at": row["created_at"],
        "trigger": row["trigger"],
        "valid": bool(row.get("valid", 1)),
        "is_empty": bool(row.get("is_empty", 0)),
        "total_nodes": int(row.get("total_nodes") or 0),
        "category_counts": _loads(row.get("category_counts_json"), {}) or {},
        "change_counts": _loads(row.get("change_counts_json"), {}) or {},
        "previous_version_id": row.get("previous_version_id"),
        "previous_version_no": row.get("previous_version_no"),
        "source_run_id": row.get("source_run_id"),
        "source_run_at": row.get("source_run_at"),
        "signature": row.get("signature"),
        "detail": _loads(row.get("detail_json"), {}) or {},
    }


def entry_payload(row: dict) -> dict:
    return {
        "category": row.get("category"),
        "node_id": int(row["node_id"]),
        "node_name": row.get("node_name") or f"节点 {row['node_id']}",
        "airport_id": row.get("airport_id"),
        "airport_name": row.get("airport_name"),
        "server": row.get("server"),
        "port": row.get("port"),
        "score": row.get("score"),
        "maturity": row.get("maturity"),
        "p80_ms": row.get("p80_ms"),
        "delay_grade": row.get("delay_grade") or "none",
        "speed_grade": row.get("speed_grade") or "empty",
        "speed_mb_s": row.get("speed_mb_s"),
        "preferred_since": row.get("preferred_since"),
        "last_reason": row.get("last_reason"),
        "detail": _loads(row.get("detail_json"), {}) or {},
    }


def entries_of(conn: sqlite3.Connection, version_id: int, category: str | None = None) -> list[dict]:
    sql = """SELECT * FROM np6_entries WHERE version_id = ?"""
    params: list[Any] = [int(version_id)]
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY category, COALESCE(score, -1) DESC, node_id"
    rows = conn.execute(sql, tuple(params)).fetchall()
    return [entry_payload(dict(row)) for row in rows]


def changes_of(conn: sqlite3.Connection, version_id: int, category: str | None = None) -> list[dict]:
    sql = """SELECT * FROM np6_changes WHERE version_id = ?"""
    params: list[Any] = [int(version_id)]
    if category:
        sql += " AND category = ?"
        params.append(category)
    sql += " ORDER BY category, change_type, node_id"
    rows = conn.execute(sql, tuple(params)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        detail = _loads(item.pop("detail_json"), {}) or {}
        item["detail"] = detail
        item["before"] = detail.get("before")
        item["after"] = detail.get("after")
        item["changed_fields"] = detail.get("changed_fields") or []
        result.append(item)
    return result


def changes_by_category(conn: sqlite3.Connection, version_id: int) -> dict[str, dict[str, list]]:
    grouped = {code: {"added": [], "removed": [], "retained": []} for code in CATEGORIES}
    for item in changes_of(conn, version_id):
        grouped.setdefault(item["category"], {"added": [], "removed": [], "retained": []})
        grouped[item["category"]].setdefault(item["change_type"], []).append(item)
    return grouped


def categories_payload(conn: sqlite3.Connection, version_id: int,
                       category: str | None = None) -> list[dict]:
    entries = entries_of(conn, version_id, category=category)
    by_code = {code: [] for code in CATEGORIES}
    for item in entries:
        by_code.setdefault(item["category"], []).append(item)
    result = []
    for code in CATEGORIES:
        if category and code != category:
            continue
        nodes = by_code.get(code, [])
        result.append({
            "code": code,
            "label": CATEGORY_LABELS[code],
            "title": CATEGORY_TITLES[code],
            "short": CATEGORY_CODES[code],
            "count": len(nodes),
            "nodes": nodes,
        })
    return result


def diff_payload(conn: sqlite3.Connection, version_id: int | None = None) -> dict:
    row = version_row(conn, version_id)
    if row is None:
        return {"current": None, "previous": None, "categories": [], "counts": {}}
    current = version_summary(row)
    previous = version_summary(version_row(conn, row.get("previous_version_id"))) if row.get("previous_version_id") else None
    grouped = changes_by_category(conn, int(row["id"]))
    return {
        "current": current,
        "previous": previous,
        "categories": [
            {"code": code, "label": CATEGORY_LABELS[code], "title": CATEGORY_TITLES[code],
             "short": CATEGORY_CODES[code],
             "added": grouped.get(code, {}).get("added", []),
             "removed": grouped.get(code, {}).get("removed", []),
             "retained": grouped.get(code, {}).get("retained", [])}
            for code in CATEGORIES
        ],
        "counts": current["change_counts"],
    }


def record_event(conn: sqlite3.Connection, event: str, *, reason: str | None = None,
                 version_id: int | None = None, detail: Any = None,
                 at: str | None = None) -> None:
    conn.execute(
        "INSERT INTO np6_events(at,event,version_id,reason,detail_json) VALUES (?,?,?,?,?)",
        (at or _stamp(), event, version_id, reason, _json(detail) if detail is not None else None),
    )


def create_version(conn: sqlite3.Connection, entries: list[dict], previous: dict | None,
                   *, trigger: str, source_run: dict, signature: str,
                   now: datetime | None = None) -> dict:
    stamp = _stamp(now)
    previous_id = int(previous["id"]) if previous else None
    previous_entries = entries_of(conn, previous_id) if previous_id else []
    old_by_cat = {code: {} for code in CATEGORIES}
    for item in previous_entries:
        old_by_cat.setdefault(item["category"], {})[int(item["node_id"])] = item
    new_by_cat = {code: {} for code in CATEGORIES}
    for item in entries:
        new_by_cat.setdefault(item["category"], {})[int(item["node_id"])] = item

    category_counts = {code: len(new_by_cat.get(code, {})) for code in CATEGORIES}
    changes = {code: {"added": [], "removed": [], "retained": []} for code in CATEGORIES}
    for code in CATEGORIES:
        old = old_by_cat.get(code, {})
        new = new_by_cat.get(code, {})
        for nid in sorted(new):
            if nid in old:
                changes[code]["retained"].append((old[nid], new[nid]))
            else:
                changes[code]["added"].append((None, new[nid]))
        for nid in sorted(old):
            if nid not in new:
                changes[code]["removed"].append((old[nid], None))
    change_counts = {
        code: {kind: len(changes[code][kind]) for kind in ("added", "removed", "retained")}
        for code in CATEGORIES
    }
    total_changes = {
        kind: sum(change_counts[code][kind] for code in CATEGORIES)
        for kind in ("added", "removed", "retained")
    }

    seq = conn.execute(
        "SELECT COALESCE(MAX(id), 0) + 1 AS n FROM np6_versions"
    ).fetchone()["n"]
    version_no = f"P6.1-{int(seq):04d}"
    cur = conn.execute(
        """INSERT INTO np6_versions(version_no,created_at,trigger,valid,is_empty,total_nodes,
           category_counts_json,change_counts_json,previous_version_id,previous_version_no,
           source_run_id,source_run_at,signature,detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (version_no, stamp, trigger, 1, 1 if not entries else 0, len(entries),
         _json(category_counts), _json(change_counts),
         previous_id, previous["version_no"] if previous else None,
         int(source_run["id"]), source_run.get("finished_at") or source_run.get("started_at"),
         signature,
         _json({"six_categories": category_counts, "changes": total_changes,
                "empty": not entries, "signature_version": SIGNATURE_VERSION})),
    )
    version_id = int(cur.lastrowid)

    for item in entries:
        conn.execute(
            """INSERT INTO np6_entries(version_id,category,node_id,node_name,airport_id,
               airport_name,server,port,score,maturity,p80_ms,delay_grade,speed_grade,
               speed_mb_s,preferred_since,last_reason,detail_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (version_id, item["category"], int(item["node_id"]), item["node_name"],
             item.get("airport_id"), item.get("airport_name"), item.get("server"),
             item.get("port"), item.get("score"), item.get("maturity"),
             item.get("p80_ms"), item.get("delay_grade"), item.get("speed_grade"),
             item.get("speed_mb_s"), item.get("preferred_since"), item.get("last_reason"),
             _json(item.get("detail") or {})),
        )

    for code in CATEGORIES:
        for kind in ("added", "removed", "retained"):
            for before, after in changes[code][kind]:
                node = after or before
                conn.execute(
                    """INSERT INTO np6_changes(version_id,previous_version_id,category,node_id,
                       change_type,at,trigger,node_name,airport_name,before_score,after_score,detail_json)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (version_id, previous_id, code, int(node["node_id"]), kind, stamp, trigger,
                     node.get("node_name"), node.get("airport_name"),
                     before.get("score") if before else None,
                     after.get("score") if after else None,
                     _json({"before": before or None, "after": after or None,
                            "changed_fields": identity_changes(before, after)})),
                )

    record_event(conn, "snapshot_created", version_id=version_id, reason=trigger,
                 detail={"version_no": version_no, "total": len(entries),
                         "previous_version_id": previous_id})
    return {
        "version": version_summary(latest_version(conn)),
        "created": True,
    }


def list_versions(conn: sqlite3.Connection, limit: int = 30) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM np6_versions ORDER BY id DESC LIMIT ?",
        (max(1, min(int(limit), 100)),),
    ).fetchall()
    return [version_summary(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Read-only facades used by service.py / HTTP.  None of these generate a version.

def summary(conn: sqlite3.Connection) -> dict:
    current_row = latest_version(conn)
    current = version_summary(current_row)
    run = latest_run(conn)
    pending = None
    can_snapshot = False
    if run and run.get("status") == "success":
        entries = collect_entries(conn)
        pending = needs_snapshot(conn, entries, current_row)
        can_snapshot = pending
    categories = []
    for code in CATEGORIES:
        categories.append({
            "code": code,
            "label": CATEGORY_LABELS[code],
            "title": CATEGORY_TITLES[code],
            "short": CATEGORY_CODES[code],
            "count": int((current or {}).get("category_counts", {}).get(code, 0)),
        })
    return {
        "current": current,
        "categories": categories,
        "history_count": int(conn.execute("SELECT COUNT(*) AS c FROM np6_versions").fetchone()["c"] or 0),
        "pending": bool(pending) if pending is not None else False,
        "can_snapshot": bool(can_snapshot),
        "source": {
            "run_id": run.get("id") if run else None,
            "status": run.get("status") if run else None,
            "finished_at": run.get("finished_at") if run else None,
            "p5_state_rows": p5_state_count(conn),
        },
        "boundary": {
            "p1_write": False, "p2_write": False, "p3_write": False, "p4_write": False,
            "p5_write": False, "p6_only": True, "p6_2": False, "p7": False,
            "keyword_filter": False, "subscription_generation": False,
        },
    }


def current_payload(conn: sqlite3.Connection, category: str | None = None) -> dict:
    row = latest_version(conn)
    if row is None:
        return {"version": None, "categories": [], "diff": diff_payload(conn)}
    vid = int(row["id"])
    return {
        "version": version_summary(row),
        "categories": categories_payload(conn, vid, category=category),
        "diff": diff_payload(conn, vid),
    }


def version_detail(conn: sqlite3.Connection, version_id: int, category: str | None = None) -> dict:
    row = version_row(conn, version_id)
    if row is None:
        raise FinalError("version_not_found", "版本不存在", status=404)
    return {
        "version": version_summary(row),
        "categories": categories_payload(conn, int(row["id"]), category=category),
        "diff": diff_payload(conn, int(row["id"])),
    }
