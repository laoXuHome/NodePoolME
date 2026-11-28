"""P3 business targets, checks, qualification state and queue storage.

Every row is kept in P3-owned ``np3_*`` tables.  P3 never writes P1 inventory,
P2 health state or the P1 competition axis; it only reads the original node
inventory and keeps an independent per-business capability record.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

from backend.database import db
from backend.health import store as health_store
from backend.nodepool import store as p1_store

BUSINESS_DEFS = (
    ("gpt", "GPT", "GPT / ChatGPT", "https://chatgpt.com/", "200-399", 360, 0),
    ("youtube", "YouTube", "YouTube", "https://www.youtube.com/generate_204", "200-399", 360, 1),
    ("telegram", "Telegram", "Telegram", "https://telegram.org/", "200-399", 360, 2),
    ("github", "GitHub", "GitHub", "https://github.com/robots.txt", "200-399", 360, 3),
    ("global", "GLOBAL", "国外综合", "https://www.cloudflare.com/cdn-cgi/trace", "200-399", 360, 4),
)
BUSINESSES = tuple(item[0] for item in BUSINESS_DEFS)
BUSINESS_LABELS = {item[0]: item[1] for item in BUSINESS_DEFS}
STATES = ("untested", "unknown", "pending", "qualified", "suspended")
CLASSIFICATIONS = ("success", "failure", "unknown")
STAGE_NAMES = ("dns", "tcp", "tls", "https")

_DDL = (
    """CREATE TABLE IF NOT EXISTS np3_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        expected_status TEXT NOT NULL DEFAULT '200-399',
        enabled INTEGER NOT NULL DEFAULT 1,
        interval_minutes INTEGER NOT NULL DEFAULT 0,
        sort_order INTEGER NOT NULL DEFAULT 0,
        config_version INTEGER NOT NULL DEFAULT 1,
        last_check_at TEXT,
        last_check_status TEXT,
        last_check_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np3_targets_enabled ON np3_targets (enabled, sort_order, id)",
    """CREATE TABLE IF NOT EXISTS np3_target_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER NOT NULL,
        scope TEXT NOT NULL DEFAULT 'selftest',
        status TEXT NOT NULL,
        available INTEGER NOT NULL DEFAULT 0,
        http_status INTEGER,
        http_ms INTEGER,
        duration_ms INTEGER,
        detail_json TEXT,
        checked_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np3_target_checks_target ON np3_target_checks (target_id, id DESC)",
    """CREATE TABLE IF NOT EXISTS np3_state (
        node_id INTEGER NOT NULL,
        business TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'untested',
        consecutive_successes INTEGER NOT NULL DEFAULT 0,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_classification TEXT,
        last_reason TEXT,
        last_run_at TEXT,
        last_valid_at TEXT,
        qualification_changed_at TEXT,
        next_due_at TEXT,
        last_check_id INTEGER,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (node_id, business)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np3_state_due ON np3_state (next_due_at)",
    "CREATE INDEX IF NOT EXISTS idx_np3_state_business ON np3_state (business, state)",
    """CREATE TABLE IF NOT EXISTS np3_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        business TEXT NOT NULL,
        target_id INTEGER NOT NULL,
        target_name TEXT NOT NULL,
        target_url TEXT NOT NULL,
        target_config_version INTEGER NOT NULL,
        target_selftest TEXT,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        classification TEXT,
        reason TEXT,
        http_status INTEGER,
        http_ms INTEGER,
        tls_ms INTEGER,
        tcp_ms INTEGER,
        duration_ms INTEGER,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        checked_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np3_checks_node ON np3_checks (node_id, business, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_np3_checks_status ON np3_checks (status, id DESC)",
    """CREATE TABLE IF NOT EXISTS np3_stages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id INTEGER NOT NULL,
        stage TEXT NOT NULL,
        executed INTEGER NOT NULL DEFAULT 1,
        ok INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        latency_ms INTEGER,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np3_stages_check ON np3_stages (check_id, id)",
    """CREATE TABLE IF NOT EXISTS np3_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        business TEXT NOT NULL,
        due_at TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 1,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        started_at TEXT
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_np3_queue_active ON np3_queue (node_id, business) WHERE status IN ('pending','running')",
    "CREATE INDEX IF NOT EXISTS idx_np3_queue_pick ON np3_queue (status, priority, due_at, id)",
    """CREATE TABLE IF NOT EXISTS np3_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        node_id INTEGER,
        business TEXT,
        target_id INTEGER,
        event TEXT NOT NULL,
        reason TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np3_events_at ON np3_events (id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_np3_events_node ON np3_events (node_id, business, id DESC)",
)


class BusinessError(Exception):
    """Stable machine-readable P3 management error."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _now() -> str:
    return db.now()


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """P3 schema always includes P1 and P2, so a single profile is self-contained."""
    if conn is not None:
        health_store.ensure_schema(conn)
        for ddl in _DDL:
            conn.execute(ddl)
        return
    with db.transaction() as own:
        ensure_schema(own)


def _read_conn(*, seed: bool = False) -> sqlite3.Connection:
    ensure_schema()
    if seed:
        with db.transaction() as conn:
            ensure_defaults(conn)
    return db.read_connection()


def ensure_defaults(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    count = int(conn.execute("SELECT COUNT(*) FROM np3_targets").fetchone()[0])
    if count:
        # Existing installations may be missing a newly introduced business only
        # after a partial deployment; fill the gap without touching history.
        present = {r["business"] for r in conn.execute("SELECT business FROM np3_targets")}
        if len(present) == len(BUSINESSES):
            return
    stamp = _now()
    for code, short, label, url, expected, interval, order in BUSINESS_DEFS:
        conn.execute(
            """INSERT INTO np3_targets(business,name,url,expected_status,enabled,
               interval_minutes,sort_order,config_version,created_at,updated_at)
               VALUES (?,?,?,?,1,?,?,1,?,?)
               ON CONFLICT(business) DO NOTHING""",
            (code, label, url, expected, int(interval), int(order), stamp, stamp),
        )
    conn.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES ('p3_interval_minutes','360')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES ('p3_valid_hours','24')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO settings(key,value) VALUES ('p3_concurrency','10')"
    )


def _parse_expected(text: str) -> list[tuple[int, int]]:
    raw = str(text or "200-399").strip()
    ranges: list[tuple[int, int]] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            left, right = part.split("-", 1)
            try:
                lo, hi = int(left), int(right)
            except ValueError:
                raise BusinessError("invalid_expected", "期望状态码格式无效")
        else:
            try:
                lo = hi = int(part)
            except ValueError:
                raise BusinessError("invalid_expected", "期望状态码格式无效")
        if not (100 <= lo <= 599 and 100 <= hi <= 599 and lo <= hi):
            raise BusinessError("invalid_expected", "期望状态码必须在 100–599 且范围有效")
        ranges.append((lo, hi))
    if not ranges:
        raise BusinessError("invalid_expected", "期望状态码不能为空")
    return ranges


def expected_codes(text: str) -> set[int]:
    return {code for lo, hi in _parse_expected(text) for code in range(lo, hi + 1)}


def _clean_expected(text: str) -> str:
    _parse_expected(text)
    return ",".join(str(x) for x in str(text).replace(" ", "").split(",") if x)


def _clean_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise BusinessError("invalid_url", "检测地址不能为空")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise BusinessError("invalid_url", "业务检测必须使用 https")
    if not parsed.hostname:
        raise BusinessError("invalid_url", "检测地址缺少主机名")
    try:
        port = parsed.port
    except ValueError:
        raise BusinessError("invalid_url", "检测地址端口无效")
    if port is not None and not (1 <= port <= 65535):
        raise BusinessError("invalid_url", "检测地址端口超出范围")
    return urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", parsed.query, ""))


def _target_row(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        parsed = urlparse(item["url"])
        item["host"] = parsed.hostname or ""
        item["port"] = parsed.port or 443
        item["path"] = (parsed.path or "/") + (("?" + parsed.query) if parsed.query else "")
        item["secure"] = parsed.scheme == "https"
        item["path"] = item["path"] or "/"
    except ValueError:
        item["host"] = ""
        item["port"] = 443
        item["path"] = "/"
        item["secure"] = True
    try:
        item["expected"] = expected_codes(item.get("expected_status") or "200-399")
    except BusinessError:
        item["expected"] = set(range(200, 400))
    item["enabled"] = bool(item.get("enabled"))
    item["interval_minutes"] = int(item.get("interval_minutes") or 0)
    return item


def list_targets(*, enabled_only: bool = False,
                  conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn(seed=True)
    sql = "SELECT * FROM np3_targets"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_order,id"
    return [_target_row(r) for r in conn.execute(sql).fetchall()]


def target(business: str, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    row = conn.execute("SELECT * FROM np3_targets WHERE business=?", (business,)).fetchone()
    item = _target_row(row)
    if item is None:
        raise BusinessError("target_not_found", "业务检测目标不存在")
    return item


def _setting_int(conn: sqlite3.Connection | None, key: str, default: int) -> int:
    if conn is None:
        row = db.read_connection().execute(
            "SELECT value FROM settings WHERE key=?", (key,)
        ).fetchone()
    else:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return int((row["value"] if row else default) or default)
    except (TypeError, ValueError):
        return default


def interval_minutes(conn: sqlite3.Connection | None = None) -> int:
    return max(30, min(1440, _setting_int(conn, "p3_interval_minutes", 360)))


def valid_hours(conn: sqlite3.Connection | None = None) -> int:
    return max(1, min(72, _setting_int(conn, "p3_valid_hours", 24)))


def concurrency(conn: sqlite3.Connection | None = None) -> int:
    return max(1, min(50, _setting_int(conn, "p3_concurrency", 10)))


def effective_interval(target_row: dict, conn: sqlite3.Connection | None = None) -> int:
    configured = int(target_row.get("interval_minutes") or 0)
    if 15 <= configured <= 1440:
        return configured
    return interval_minutes(conn)


def valid_until(last_valid_at: str | None, hours: int) -> str | None:
    if not last_valid_at:
        return None
    try:
        parsed = datetime.strptime(last_valid_at[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return _stamp(parsed + timedelta(hours=hours))


def is_expired(last_valid_at: str | None, hours: int | None = None) -> bool:
    until = valid_until(last_valid_at, hours or valid_hours())
    return bool(until and until <= _now())


def _record_event(conn: sqlite3.Connection, event: str, *, node_id: int | None = None,
                  business: str | None = None, target_id: int | None = None,
                  reason: str | None = None, detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO np3_events(at,node_id,business,target_id,event,reason,detail_json) "
        "VALUES (?,?,?,?,?,?,?)",
        (_now(), node_id, business, target_id, event, reason,
         _json(detail) if detail is not None else None),
    )


def _invalidate_business(conn: sqlite3.Connection, business: str, reason: str) -> None:
    stamp = _now()
    conn.execute(
        """UPDATE np3_state SET state='untested', consecutive_successes=0,
           consecutive_failures=0, last_classification='unknown', last_reason=?,
           last_valid_at=NULL, qualification_changed_at=?, next_due_at=?, updated_at=?
           WHERE business=?""",
        (reason, stamp, stamp, stamp, business),
    )
    conn.execute(
        "UPDATE np3_checks SET status='unknown', classification='unknown', reason='config_changed', "
        "finished_at=? WHERE business=? AND status='running'", (stamp, business)
    )
    conn.execute("DELETE FROM np3_queue WHERE business=? AND status IN ('pending','running')", (business,))


def update_target(business: str, *, name: str | None = None, url: str | None = None,
                  expected_status: str | None = None, enabled: bool | None = None,
                  interval_minutes_value: int | None = None,
                  conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return update_target(business, name=name, url=url, expected_status=expected_status,
                                 enabled=enabled, interval_minutes_value=interval_minutes_value,
                                 conn=own_conn)
    ensure_schema(conn)
    current = target(business, conn=conn)
    technical = False
    before = {"name": current["name"], "url": current["url"],
              "expected_status": current["expected_status"], "enabled": current["enabled"],
              "interval_minutes": current["interval_minutes"]}
    if name is not None:
        clean = str(name).strip()
        if not clean:
            raise BusinessError("invalid_name", "检测目标名称不能为空")
        if len(clean) > 40:
            raise BusinessError("invalid_name", "检测目标名称不能超过 40 个字符")
        conn.execute("UPDATE np3_targets SET name=? WHERE business=?", (clean, business))
    if url is not None:
        clean_url = _clean_url(url)
        if clean_url != current["url"]:
            conn.execute("UPDATE np3_targets SET url=? WHERE business=?", (clean_url, business))
            technical = True
    if expected_status is not None:
        clean_exp = _clean_expected(expected_status)
        if clean_exp != current["expected_status"]:
            conn.execute("UPDATE np3_targets SET expected_status=? WHERE business=?",
                         (clean_exp, business))
            technical = True
    if enabled is not None:
        value = bool(enabled)
        if value != current["enabled"]:
            conn.execute("UPDATE np3_targets SET enabled=? WHERE business=?",
                         (1 if value else 0, business))
            technical = True
    if interval_minutes_value is not None:
        value = int(interval_minutes_value)
        if value != 0 and not 15 <= value <= 1440:
            raise BusinessError("invalid_interval", "业务检测周期必须为 0（跟随全局）或 15–1440 分钟")
        if value != current["interval_minutes"]:
            conn.execute("UPDATE np3_targets SET interval_minutes=? WHERE business=?",
                         (value, business))
            technical = True
    if technical:
        new_version = int(current["config_version"]) + 1
        conn.execute("UPDATE np3_targets SET config_version=?, updated_at=? WHERE business=?",
                     (new_version, _now(), business))
        _invalidate_business(conn, business, "target_config_changed")
        _record_event(conn, "target_config_changed", business=business, target_id=current["id"],
                      reason="检测目标配置变更", detail={"before": before, "version": new_version})
    else:
        conn.execute("UPDATE np3_targets SET updated_at=? WHERE business=?", (_now(), business))
        _record_event(conn, "target_named", business=business, target_id=current["id"],
                      reason="修改目标名称")
    return target(business, conn=conn)


def restore_defaults(conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return restore_defaults(conn=own_conn)
    ensure_schema(conn)
    stamp = _now()
    for code, short, label, url, expected, interval, order in BUSINESS_DEFS:
        row = conn.execute("SELECT * FROM np3_targets WHERE business=?", (code,)).fetchone()
        if row is None:
            conn.execute(
                """INSERT INTO np3_targets(business,name,url,expected_status,enabled,
                   interval_minutes,sort_order,config_version,created_at,updated_at)
                   VALUES (?,?,?,?,1,?,?,1,?,?)""",
                (code, label, url, expected, int(interval), int(order), stamp, stamp),
            )
        else:
            conn.execute(
                """UPDATE np3_targets SET name=?,url=?,expected_status=?,enabled=1,
                   interval_minutes=?,sort_order=?,config_version=config_version+1,updated_at=?
                   WHERE business=?""",
                (label, url, expected, int(interval), int(order), stamp, code),
            )
        _invalidate_business(conn, code, "targets_restored")
        _record_event(conn, "targets_restored", business=code, reason="恢复默认业务检测目标")
    return list_targets(conn=conn)


def record_target_check(target_id: int, status: str, *, available: bool = False,
                        http_status: int | None = None, http_ms: int | None = None,
                        duration_ms: int | None = None, detail: Any = None,
                        scope: str = "selftest",
                        conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return record_target_check(target_id, status, available=available,
                                       http_status=http_status, http_ms=http_ms,
                                       duration_ms=duration_ms, detail=detail,
                                       scope=scope, conn=own_conn)
    ensure_schema(conn)
    if status not in ("success", "failure", "unknown"):
        raise BusinessError("invalid_status", "目标检测状态无效")
    stamp = _now()
    conn.execute(
        """INSERT INTO np3_target_checks(target_id,scope,status,available,http_status,http_ms,
           duration_ms,detail_json,checked_at) VALUES (?,?,?,?,?,?,?,?,?)""",
        (int(target_id), scope, status, 1 if available else 0, http_status, http_ms,
         duration_ms, _json(detail) if detail is not None else None, stamp),
    )
    conn.execute(
        """UPDATE np3_targets SET last_check_at=?,last_check_status=?,
           last_check_error=? WHERE id=?""",
        (stamp, status, detail.get("reason") if isinstance(detail, dict) else None,
         int(target_id)),
    )
    return {"target_id": int(target_id), "status": status, "available": bool(available),
            "checked_at": stamp}


def sync_states(conn: sqlite3.Connection) -> int:
    ensure_schema(conn)
    stamp = _now()
    before = int(conn.execute("SELECT COUNT(*) FROM np3_state").fetchone()[0])
    for business in BUSINESSES:
        conn.execute(
            """INSERT OR IGNORE INTO np3_state(node_id,business,state,next_due_at,updated_at)
               SELECT id,?, 'untested', ?, ? FROM np_nodes WHERE source_active=1""",
            (business, stamp, stamp),
        )
    return int(conn.execute("SELECT COUNT(*) FROM np3_state").fetchone()[0]) - before


def _business_interval_map(conn: sqlite3.Connection) -> dict[str, int]:
    out = {}
    for row in conn.execute("SELECT business,interval_minutes FROM np3_targets").fetchall():
        configured = int(row["interval_minutes"] or 0)
        out[row["business"]] = configured if 15 <= configured <= 1440 else interval_minutes(conn)
    return out


def enqueue_due(conn: sqlite3.Connection, *, trigger: str = "normal", priority: int = 1,
                limit: int = 500) -> int:
    """Queue one row per node/business whose own due time has arrived."""
    ensure_schema(conn)
    stamp = _now()
    hours = valid_hours(conn)
    rows = conn.execute(
        """SELECT s.node_id,s.business,s.state,s.next_due_at,s.last_valid_at,
                  t.id AS target_id,t.enabled,t.interval_minutes
           FROM np3_state s JOIN np3_targets t ON t.business=s.business
           JOIN np_nodes n ON n.id=s.node_id
           WHERE n.source_active=1 AND t.enabled=1
             AND (COALESCE(s.next_due_at, ? ) <= ?
                  OR (s.state='qualified' AND s.last_valid_at IS NOT NULL
                      AND datetime(s.last_valid_at, ?) <= datetime(?)))
           ORDER BY COALESCE(s.next_due_at, ?), s.node_id,s.business LIMIT ?""",
        (stamp, stamp, f"+{hours} hours", stamp, stamp, int(limit)),
    ).fetchall()
    added = 0
    intervals = _business_interval_map(conn)
    for row in rows:
        business = row["business"]
        state = row["state"] or "untested"
        if trigger == "normal":
            if state in ("pending", "suspended"):
                row_trigger, row_priority = "business_recheck", 0
            else:
                row_trigger, row_priority = "normal", priority
        else:
            row_trigger, row_priority = trigger, priority
        # Keep the queue row's own due timestamp but never schedule a qualified
        # business beyond its configured validity window.
        due = row["next_due_at"] or stamp
        if state == "qualified" and row["last_valid_at"]:
            expiry = valid_until(row["last_valid_at"], hours) or due
            if expiry < due:
                due = expiry
        cur = conn.execute(
            """INSERT OR IGNORE INTO np3_queue(node_id,business,due_at,priority,trigger,status,created_at)
               VALUES (?,?,?,?,?, 'pending', ?)""",
            (int(row["node_id"]), business, due, int(row_priority), row_trigger, stamp),
        )
        added += int(cur.rowcount or 0)
    return added


def enqueue_tasks(tasks: Iterable[tuple[int, str]], *, trigger: str = "manual",
                  priority: int = 0, conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return enqueue_tasks(tasks, trigger=trigger, priority=priority, conn=own_conn)
    ensure_schema(conn)
    stamp = _now()
    added = 0
    ensure_defaults(conn)
    if True:
        for node_id, business in tasks:
            if business not in BUSINESSES:
                continue
            node = conn.execute("SELECT id FROM np_nodes WHERE id=? AND source_active=1",
                                (int(node_id),)).fetchone()
            target_row = conn.execute("SELECT enabled FROM np3_targets WHERE business=?",
                                      (business,)).fetchone()
            if node is None or target_row is None or not target_row["enabled"]:
                continue
            existing = conn.execute(
                "SELECT id,status FROM np3_queue WHERE node_id=? AND business=?",
                (int(node_id), business),
            ).fetchone()
            if existing:
                if existing["status"] == "pending":
                    conn.execute(
                        "UPDATE np3_queue SET due_at=?,priority=?,trigger=? WHERE id=?",
                        (stamp, int(priority), trigger, int(existing["id"])),
                    )
                continue
            conn.execute(
                """INSERT INTO np3_queue(node_id,business,due_at,priority,trigger,status,created_at)
                   VALUES (?,?,?,?,?, 'pending', ?)""",
                (int(node_id), business, stamp, int(priority), trigger, stamp),
            )
            added += 1
    return added


def reserve_queue(conn: sqlite3.Connection, *, limit: int) -> list[dict]:
    ensure_schema(conn)
    if limit <= 0:
        return []
    rows = conn.execute(
        """SELECT q.id,q.node_id,q.business,q.due_at,q.priority,q.trigger,
                  n.current_name,n.protocol,n.server,n.port,n.params,n.airport_id,n.region
           FROM np3_queue q JOIN np_nodes n ON n.id=q.node_id
           WHERE q.status='pending' AND q.due_at<=?
           ORDER BY q.priority,q.due_at,q.id LIMIT ?""",
        (_now(), int(limit)),
    ).fetchall()
    reserved = []
    for row in rows:
        changed = conn.execute(
            "UPDATE np3_queue SET status='running',started_at=? WHERE id=? AND status='pending'",
            (_now(), int(row["id"])),
        ).rowcount
        if changed:
            reserved.append(dict(row))
    return reserved


def abandon_queue(conn: sqlite3.Connection, queue_id: int, check_id: int | None,
                  reason: str = "batch_aborted") -> None:
    if not queue_id:
        return
    conn.execute(
        "UPDATE np3_queue SET status='error' WHERE id=? AND status IN ('pending','running')",
        (int(queue_id),),
    )
    if check_id:
        conn.execute(
            "UPDATE np3_checks SET status='unknown',classification='unknown',reason=?,"
            "finished_at=? WHERE id=? AND status='running'",
            (reason, _now(), int(check_id)),
        )


def finish_queue(conn: sqlite3.Connection, queue_id: int) -> None:
    conn.execute("DELETE FROM np3_queue WHERE id=? AND status='running'", (int(queue_id),))


def active_queue_count(conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        conn = _read_conn()
    return int(conn.execute(
        "SELECT COUNT(*) FROM np3_queue WHERE status='running'"
    ).fetchone()[0])


def queue_counts(conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn()
    rows = conn.execute("SELECT status,COUNT(*) AS count FROM np3_queue GROUP BY status").fetchall()
    return {r["status"]: int(r["count"]) for r in rows}


def recover_stale_queue(conn: sqlite3.Connection) -> int:
    ensure_schema(conn)
    queues = int(conn.execute(
        "UPDATE np3_queue SET status='pending',started_at=NULL WHERE status='running'"
    ).rowcount)
    # A process restart must not leave a check in running; its evidence is incomplete.
    conn.execute(
        "UPDATE np3_checks SET status='unknown',classification='unknown',reason='task_interrupted',"
        "finished_at=? WHERE status='running'", (_now(),)
    )
    return queues


def create_check(conn: sqlite3.Connection, node_id: int, business: str, target_row: dict,
                 trigger: str) -> int:
    ensure_schema(conn)
    stamp = _now()
    cur = conn.execute(
        """INSERT INTO np3_checks(node_id,business,target_id,target_name,target_url,
           target_config_version,trigger,status,started_at,checked_at)
           VALUES (?,?,?,?,?,?,?, 'running',?,?)""",
        (int(node_id), business, int(target_row["id"]), target_row["name"], target_row["url"],
         int(target_row["config_version"]), trigger, stamp, stamp),
    )
    return int(cur.lastrowid)


def save_stages(conn: sqlite3.Connection, check_id: int, stages: list[dict]) -> None:
    for stage in stages:
        conn.execute(
            """INSERT INTO np3_stages(check_id,stage,executed,ok,status,latency_ms,detail_json)
               VALUES (?,?,?,?,?,?,?)""",
            (int(check_id), stage.get("stage"), 1 if stage.get("executed") else 0,
             1 if stage.get("ok") else 0, stage.get("status") or "unexecuted",
             stage.get("latency_ms"),
             _json(stage.get("detail")) if stage.get("detail") is not None else None),
        )


def finish_check(conn: sqlite3.Connection, check_id: int, *, status: str, reason: str | None,
                 http_status: int | None, http_ms: int | None, tls_ms: int | None,
                 duration_ms: int | None, stages: list[dict]) -> None:
    conn.execute(
        """UPDATE np3_checks SET status=?,classification=?,reason=?,http_status=?,http_ms=?,
           tls_ms=?,duration_ms=?,finished_at=? WHERE id=? AND status='running'""",
        (status, status, reason, http_status, http_ms, tls_ms, duration_ms, _now(), int(check_id)),
    )
    save_stages(conn, check_id, stages)


def update_state(conn: sqlite3.Connection, node_id: int, business: str, *, state: str,
                 successes: int, failures: int, classification: str, reason: str,
                 last_valid_at: str | None, changed_at: str | None,
                 next_due_at: str, check_id: int) -> None:
    stamp = _now()
    conn.execute(
        """INSERT INTO np3_state(node_id,business,state,consecutive_successes,
           consecutive_failures,last_classification,last_reason,last_run_at,last_valid_at,
           qualification_changed_at,next_due_at,last_check_id,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id,business) DO UPDATE SET
             state=excluded.state, consecutive_successes=excluded.consecutive_successes,
             consecutive_failures=excluded.consecutive_failures,
             last_classification=excluded.last_classification, last_reason=excluded.last_reason,
             last_run_at=excluded.last_run_at, last_valid_at=excluded.last_valid_at,
             qualification_changed_at=COALESCE(excluded.qualification_changed_at,
                                               np3_state.qualification_changed_at),
             next_due_at=excluded.next_due_at, last_check_id=excluded.last_check_id,
             updated_at=excluded.updated_at""",
        (int(node_id), business, state, int(successes), int(failures), classification,
         reason, stamp, last_valid_at, changed_at, next_due_at, int(check_id), stamp),
    )


def get_state(node_id: int, business: str, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    row = conn.execute("SELECT * FROM np3_state WHERE node_id=? AND business=?",
                       (int(node_id), business)).fetchone()
    if row is None:
        return {"node_id": int(node_id), "business": business, "state": "untested",
                "consecutive_successes": 0, "consecutive_failures": 0,
                "last_valid_at": None, "next_due_at": None}
    return dict(row)


def states_for_node(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict[str, dict]:
    if conn is None:
        conn = _read_conn(seed=True)
    hours = valid_hours(conn)
    out = {}
    for row in conn.execute("SELECT * FROM np3_state WHERE node_id=?", (int(node_id),)).fetchall():
        item = dict(row)
        item["expired"] = is_expired(item.get("last_valid_at"), hours)
        item["valid_until"] = valid_until(item.get("last_valid_at"), hours)
        item["qualification_active"] = bool(item["state"] == "qualified" and not item["expired"])
        out[item["business"]] = item
    return out


def check(check_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn()
    row = conn.execute("SELECT * FROM np3_checks WHERE id=?", (int(check_id),)).fetchone()
    if row is None:
        raise BusinessError("check_not_found", "业务检测记录不存在")
    return dict(row)


def check_stages(check_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM np3_stages WHERE check_id=? ORDER BY id", (int(check_id),)
    ).fetchall()]
    for stage in rows:
        stage["detail"] = db.loads(stage.pop("detail_json", None), None)
    return rows


def recent_checks(node_id: int, business: str, *, limit: int = 12,
                  conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn(seed=True)
    rows = [dict(r) for r in conn.execute(
        """SELECT * FROM np3_checks WHERE node_id=? AND business=?
           ORDER BY id DESC LIMIT ?""",
        (int(node_id), business, int(limit)),
    ).fetchall()]
    for row in rows:
        row["stages"] = check_stages(int(row["id"]), conn=conn)
    return rows


def latest_check(node_id: int, business: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        conn = _read_conn(seed=True)
    row = conn.execute(
        "SELECT * FROM np3_checks WHERE node_id=? AND business=? ORDER BY id DESC LIMIT 1",
        (int(node_id), business),
    ).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["stages"] = check_stages(int(item["id"]), conn=conn)
    return item


def events_for(node_id: int, business: str | None = None, *, limit: int = 30,
               conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn(seed=True)
    sql = "SELECT * FROM np3_events WHERE node_id=?"
    args: list[Any] = [int(node_id)]
    if business:
        sql += " AND business=?"
        args.append(business)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    for row in rows:
        row["detail"] = db.loads(row.pop("detail_json", None), None)
    return rows


def _active_state(item: dict, hours: int) -> bool:
    return bool(item.get("state") == "qualified" and not is_expired(item.get("last_valid_at"), hours))


def node_rows(*, page: int = 1, per_page: int = 20, q: str | None = None,
              business: str | None = None, state: str | None = None,
              conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    where = ["n.source_active=1"]
    args: list[Any] = []
    if q:
        like = f"%{q}%"
        where.append("(n.current_name LIKE ? OR n.server LIKE ? OR n.source_name LIKE ?)")
        args.extend([like, like, like])
    if business and state:
        where.append("EXISTS (SELECT 1 FROM np3_state sx WHERE sx.node_id=n.id "
                     "AND sx.business=? AND sx.state=?)")
        args.extend([business, state])
    elif state:
        where.append("EXISTS (SELECT 1 FROM np3_state sx WHERE sx.node_id=n.id "
                     "AND sx.state=?)")
        args.append(state)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM np_nodes n WHERE {' AND '.join(where)}", args
    ).fetchone()[0])
    page = max(1, int(page))
    per_page = min(100, max(1, int(per_page)))
    nodes = [dict(r) for r in conn.execute(
        f"""SELECT n.id,n.current_name,n.protocol,n.server,n.port,n.region,n.source_name,
                   n.management_layer,n.base_health,n.data_status,n.observation_reason,
                   n.competition_state,n.last_test_at
            FROM np_nodes n WHERE {' AND '.join(where)}
            ORDER BY n.id LIMIT ? OFFSET ?""",
        args + [per_page, (page - 1) * per_page],
    ).fetchall()]
    targets = list_targets(conn=conn)
    target_map = {t["business"]: t for t in targets}
    hours = valid_hours(conn)
    all_states = {}
    for row in conn.execute(
        """SELECT s.* FROM np3_state s JOIN np_nodes n ON n.id=s.node_id
           WHERE n.source_active=1"""
    ).fetchall():
        all_states.setdefault(int(row["node_id"]), {})[row["business"]] = dict(row)
    active_queue = {}
    for row in conn.execute(
        "SELECT node_id,business FROM np3_queue WHERE status IN ('pending','running')"
    ).fetchall():
        active_queue[(int(row["node_id"]), row["business"])] = True
    items = []
    for node in nodes:
        biz = {}
        for code in BUSINESSES:
            target_row = target_map.get(code) or {}
            st = all_states.get(int(node["id"]), {}).get(code) or {
                "state": "untested", "consecutive_successes": 0,
                "consecutive_failures": 0, "last_valid_at": None,
            }
            st["expired"] = is_expired(st.get("last_valid_at"), hours)
            st["valid_until"] = valid_until(st.get("last_valid_at"), hours)
            st["qualification_active"] = _active_state(st, hours)
            st["target_enabled"] = bool(target_row.get("enabled", True))
            st["target_name"] = target_row.get("name") or BUSINESS_LABELS[code]
            st["checking"] = active_queue.get((int(node["id"]), code), False)
            biz[code] = st
        item = dict(node)
        item["businesses"] = biz
        item["qualified_count"] = len([b for b in biz.values() if b["qualification_active"]])
        items.append(item)
    return {"items": items, "total": total, "page": page, "per_page": per_page,
            "targets": targets}


def node_detail(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    row = conn.execute(
        """SELECT n.*, a.name AS airport_name FROM np_nodes n
           LEFT JOIN airports a ON a.id=n.airport_id WHERE n.id=?""", (int(node_id),)
    ).fetchone()
    if row is None:
        raise BusinessError("node_not_found", "节点不存在")
    node = dict(row)
    hours = valid_hours(conn)
    targets = {t["business"]: t for t in list_targets(conn=conn)}
    states = states_for_node(node_id, conn=conn)
    items = {}
    for code in BUSINESSES:
        st = states.get(code) or {"state": "untested", "consecutive_successes": 0,
                                  "consecutive_failures": 0, "last_valid_at": None}
        st["expired"] = is_expired(st.get("last_valid_at"), hours)
        st["valid_until"] = valid_until(st.get("last_valid_at"), hours)
        st["qualification_active"] = _active_state(st, hours)
        st["target"] = targets.get(code)
        st["target_enabled"] = bool((targets.get(code) or {}).get("enabled", True))
        st["checking"] = conn.execute(
            "SELECT 1 FROM np3_queue WHERE node_id=? AND business=? "
            "AND status IN ('pending','running') LIMIT 1", (int(node_id), code)
        ).fetchone() is not None
        st["latest"] = latest_check(node_id, code, conn=conn)
        st["recent_checks"] = recent_checks(node_id, code, limit=8, conn=conn)
        st["events"] = events_for(node_id, code, limit=12, conn=conn)
        items[code] = st
    return {"node": node, "businesses": items,
            "events": events_for(node_id, limit=40, conn=conn),
            "settings": settings_snapshot(conn)}


def summary(*, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    hours = valid_hours(conn)
    targets = {t["business"]: t for t in list_targets(conn=conn)}
    states = [dict(r) for r in conn.execute(
        """SELECT s.* FROM np3_state s JOIN np_nodes n ON n.id=s.node_id
           WHERE n.source_active=1"""
    ).fetchall()]
    per_business = {}
    active_nodes_by_business = {code: set() for code in BUSINESSES}
    pending_nodes = set()
    unknown_nodes = set()
    qualified_nodes = set()
    for item in states:
        below = per_business.setdefault(item["business"], {
            "qualified": 0, "pending": 0, "unknown": 0, "suspended": 0, "untested": 0,
            "total": 0,
        })
        below["total"] += 1
        active = _active_state(item, hours)
        if active:
            below["qualified"] += 1
            qualified_nodes.add(int(item["node_id"]))
            active_nodes_by_business.setdefault(item["business"], set()).add(int(item["node_id"]))
        elif item["state"] == "qualified":
            # Expired qualification is not currently active; it stays historical
            # and the scheduler will refresh it.
            below["qualified"] += 0
        if item["state"] == "pending":
            below["pending"] += 1
            pending_nodes.add(int(item["node_id"]))
        elif item["state"] == "unknown":
            below["unknown"] += 1
            unknown_nodes.add(int(item["node_id"]))
        elif item["state"] == "suspended":
            below["suspended"] += 1
        elif item["state"] == "untested":
            below["untested"] += 1
    business_list = []
    for code, short, label, url, expected, interval, order in BUSINESS_DEFS:
        target_row = targets.get(code)
        stats = per_business.get(code, {"qualified": 0, "pending": 0, "unknown": 0,
                                        "suspended": 0, "untested": 0, "total": 0})
        stats["qualified_nodes"] = len(active_nodes_by_business.get(code, set()))
        business_list.append({
            "code": code, "short": short, "label": label,
            "target": target_row, "stats": stats,
        })
    queue = queue_counts(conn)
    return {
        "businesses": business_list,
        "qualified_nodes": len(qualified_nodes),
        "pending_nodes": len(pending_nodes),
        "unknown_nodes": len(unknown_nodes),
        "queue": {"pending": queue.get("pending", 0), "running": queue.get("running", 0),
                  "total": queue.get("pending", 0) + queue.get("running", 0)},
        "settings": settings_snapshot(conn),
        "p2_health": _p2_snapshot(conn),
    }


def _p2_snapshot(conn: sqlite3.Connection) -> dict:
    try:
        rows = conn.execute(
            "SELECT base_health,COUNT(*) AS count FROM np_nodes WHERE source_active=1 GROUP BY base_health"
        ).fetchall()
        return {r["base_health"]: int(r["count"]) for r in rows}
    except Exception:
        return {}


def settings_snapshot(conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    return {
        "interval_minutes": interval_minutes(conn),
        "valid_hours": valid_hours(conn),
        "concurrency": concurrency(conn),
        "recheck_first_seconds": 15,
        "recheck_second_seconds": 30,
        "recheck_minutes": 5,
        "success_threshold": 2,
        "failure_threshold": 3,
    }


def update_settings(*, interval_minutes_value: int | None = None,
                    valid_hours_value: int | None = None,
                    concurrency_value: int | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return update_settings(interval_minutes_value=interval_minutes_value,
                                   valid_hours_value=valid_hours_value,
                                   concurrency_value=concurrency_value, conn=own_conn)
    ensure_schema(conn)
    if interval_minutes_value is not None:
        value = int(interval_minutes_value)
        if not 30 <= value <= 1440:
            raise BusinessError("invalid_interval", "业务检测周期必须在 30–1440 分钟之间")
        conn.execute("INSERT INTO settings(key,value) VALUES ('p3_interval_minutes',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
    if valid_hours_value is not None:
        value = int(valid_hours_value)
        if not 1 <= value <= 72:
            raise BusinessError("invalid_validity", "业务结果有效期必须在 1–72 小时之间")
        conn.execute("INSERT INTO settings(key,value) VALUES ('p3_valid_hours',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
    if concurrency_value is not None:
        value = int(concurrency_value)
        if not 1 <= value <= 50:
            raise BusinessError("invalid_concurrency", "全局并发必须在 1–50 之间")
        conn.execute("INSERT INTO settings(key,value) VALUES ('p3_concurrency',?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(value),))
    return settings_snapshot(conn)
