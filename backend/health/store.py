"""P2 probe-target inventory and health history storage.

The module keeps its tables outside the frozen 17-table migration baseline, in
the same lazy-created style as P1.  All writes happen inside an explicit
transaction supplied by the caller; read helpers use the shared DB connection.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable
from urllib.parse import urlparse, urlunparse

from backend.database import db
from backend.nodepool import store as p1_store

DEFAULT_TARGETS = (
    ("Google", "https://www.google.com/generate_204"),
    ("Wikipedia", "https://www.wikipedia.org/"),
)
TARGET_STATES = ("success", "failure", "unknown")
CHECK_STATES = ("running", "success", "failure", "unknown")
STAGE_NAMES = ("dns", "tcp", "tls", "https")

_DDL = (
    """CREATE TABLE IF NOT EXISTS np2_targets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        sort_order INTEGER NOT NULL DEFAULT 0,
        last_check_at TEXT,
        last_check_status TEXT,
        last_check_error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_targets_enabled ON np2_targets (enabled, sort_order, id)",
    """CREATE TABLE IF NOT EXISTS np2_target_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id INTEGER NOT NULL,
        scope TEXT NOT NULL DEFAULT 'selftest',
        status TEXT NOT NULL,
        duration_ms INTEGER,
        detail_json TEXT,
        checked_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_target_checks_target ON np2_target_checks (target_id, id DESC)",
    """CREATE TABLE IF NOT EXISTS np2_node_state (
        node_id INTEGER PRIMARY KEY,
        state TEXT NOT NULL DEFAULT 'new',
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        consecutive_successes INTEGER NOT NULL DEFAULT 0,
        last_classification TEXT,
        last_reason TEXT,
        last_run_at TEXT,
        next_due_at TEXT,
        last_check_id INTEGER,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_state_due ON np2_node_state (next_due_at)",
    "CREATE INDEX IF NOT EXISTS idx_np2_state_state ON np2_node_state (state)",
    """CREATE TABLE IF NOT EXISTS np2_checks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        reason TEXT,
        config_version INTEGER NOT NULL,
        target_count INTEGER NOT NULL DEFAULT 0,
        success_count INTEGER NOT NULL DEFAULT 0,
        failure_count INTEGER NOT NULL DEFAULT 0,
        unknown_count INTEGER NOT NULL DEFAULT 0,
        p80_ms INTEGER,
        health_pass INTEGER NOT NULL DEFAULT 0,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        duration_ms INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_checks_node ON np2_checks (node_id, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_np2_checks_status ON np2_checks (status, id DESC)",
    """CREATE TABLE IF NOT EXISTS np2_target_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id INTEGER NOT NULL,
        target_id INTEGER NOT NULL,
        target_name TEXT NOT NULL,
        target_url TEXT NOT NULL,
        status TEXT NOT NULL,
        reason TEXT,
        p80_ms INTEGER,
        tcp_valid_count INTEGER NOT NULL DEFAULT 0,
        tls_ms INTEGER,
        https_ms INTEGER,
        https_status INTEGER,
        health_pass INTEGER NOT NULL DEFAULT 0,
        started_at TEXT,
        finished_at TEXT,
        duration_ms INTEGER
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_target_results_check ON np2_target_results (check_id, id)",
    """CREATE TABLE IF NOT EXISTS np2_stage_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id INTEGER NOT NULL,
        target_result_id INTEGER NOT NULL,
        stage TEXT NOT NULL,
        executed INTEGER NOT NULL DEFAULT 1,
        ok INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        latency_ms INTEGER,
        slow INTEGER NOT NULL DEFAULT 0,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_stages_check ON np2_stage_results (check_id, target_result_id, id)",
    """CREATE TABLE IF NOT EXISTS np2_tcp_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        check_id INTEGER NOT NULL,
        target_result_id INTEGER NOT NULL,
        sample_index INTEGER NOT NULL,
        ok INTEGER NOT NULL DEFAULT 0,
        latency_ms INTEGER,
        error TEXT,
        sampled_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_tcp_samples_check ON np2_tcp_samples (check_id, target_result_id, sample_index)",
    """CREATE TABLE IF NOT EXISTS np2_queue (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        due_at TEXT NOT NULL,
        priority INTEGER NOT NULL DEFAULT 1,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        started_at TEXT
    )""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_np2_queue_active ON np2_queue (node_id) WHERE status IN ('pending','running')",
    "CREATE INDEX IF NOT EXISTS idx_np2_queue_pick ON np2_queue (status, priority, due_at, id)",
    """CREATE TABLE IF NOT EXISTS np2_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        node_id INTEGER,
        target_id INTEGER,
        event TEXT NOT NULL,
        reason TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np2_events_at ON np2_events (id DESC)",
)


class HealthError(Exception):
    """Stable machine-readable P2 management error."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _read_conn(*, seed: bool = False) -> sqlite3.Connection:
    ensure_schema()
    if seed:
        with db.transaction() as conn:
            ensure_defaults(conn)
    return db.read_connection()


def _now() -> str:
    return db.now()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        p1_store.ensure_schema(conn)
        for ddl in _DDL:
            conn.execute(ddl)
        return
    with db.transaction() as own:
        ensure_schema(own)


def ensure_defaults(conn: sqlite3.Connection) -> int:
    """Seed the two mandated targets exactly once and return the config version."""
    ensure_schema(conn)
    version = conn.execute(
        "SELECT value FROM settings WHERE key='p2_config_version'"
    ).fetchone()
    if version is None:
        conn.execute(
            "INSERT INTO settings(key,value) VALUES ('p2_config_version','1') "
            "ON CONFLICT(key) DO NOTHING"
        )
    count = int(conn.execute("SELECT COUNT(*) FROM np2_targets").fetchone()[0])
    if count == 0:
        stamp = _now()
        for idx, (name, url) in enumerate(DEFAULT_TARGETS):
            conn.execute(
                """INSERT OR IGNORE INTO np2_targets
                   (name,url,enabled,sort_order,created_at,updated_at)
                   VALUES (?,?,1,?,?,?)""",
                (name, url, idx, stamp, stamp),
            )
        conn.execute(
            "INSERT INTO np2_events(at,event,reason) VALUES (?,?,?)",
            (stamp, "targets_default", "首次启用默认检测目标 Google / Wikipedia"),
        )
    return config_version(conn)


def config_version(conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        row = db.read_connection().execute(
            "SELECT value FROM settings WHERE key='p2_config_version'"
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='p2_config_version'"
        ).fetchone()
    try:
        return int((row["value"] if row else 1) or 1)
    except (TypeError, ValueError):
        return 1


def _bump_config_version(conn: sqlite3.Connection) -> int:
    value = config_version(conn) + 1
    conn.execute(
        "INSERT INTO settings(key,value) VALUES ('p2_config_version',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(value),),
    )
    return value


def _target_row(row: sqlite3.Row | dict | None) -> dict | None:
    if row is None:
        return None
    item = dict(row)
    try:
        parsed = urlparse(item["url"])
        item["host"] = parsed.hostname or ""
        item["port"] = parsed.port or (443 if parsed.scheme == "https" else 80)
        item["path"] = parsed.path or "/"
        if parsed.query:
            item["path"] += "?" + parsed.query
        item["secure"] = parsed.scheme == "https"
    except ValueError:
        item["host"] = ""
        item["port"] = 443
        item["path"] = "/"
        item["secure"] = True
    item["enabled"] = bool(item.get("enabled"))
    return item


def _clean_url(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise HealthError("invalid_url", "检测地址不能为空")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise HealthError("invalid_url", "检测地址必须使用 http 或 https")
    if not parsed.hostname:
        raise HealthError("invalid_url", "检测地址缺少主机名")
    try:
        port = parsed.port
    except ValueError:
        raise HealthError("invalid_url", "检测地址端口无效")
    if port is not None and not (1 <= port <= 65535):
        raise HealthError("invalid_url", "检测地址端口超出范围")
    path = parsed.path or "/"
    return urlunparse((parsed.scheme, parsed.netloc, path, "", parsed.query, ""))


def _clean_name(value: str) -> str:
    name = str(value or "").strip()
    if not name:
        raise HealthError("invalid_name", "检测目标名称不能为空")
    if len(name) > 40:
        raise HealthError("invalid_name", "检测目标名称不能超过 40 个字符")
    return name


def list_targets(*, enabled_only: bool = False, conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    sql = "SELECT * FROM np2_targets"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY sort_order,id"
    return [_target_row(r) for r in conn.execute(sql).fetchall()]


def target(target_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn()
    row = conn.execute("SELECT * FROM np2_targets WHERE id=?", (target_id,)).fetchone()
    item = _target_row(row)
    if item is None:
        raise HealthError("target_not_found", "检测目标不存在")
    return item


def _enabled_count(conn: sqlite3.Connection, *, exclude: int | None = None) -> int:
    if exclude is None:
        return int(conn.execute("SELECT COUNT(*) FROM np2_targets WHERE enabled=1").fetchone()[0])
    return int(conn.execute(
        "SELECT COUNT(*) FROM np2_targets WHERE enabled=1 AND id<>?", (exclude,)
    ).fetchone()[0])


def _record_target_event(conn: sqlite3.Connection, event: str, *, target_id: int | None = None,
                         reason: str | None = None, detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO np2_events(at,node_id,target_id,event,reason,detail_json) VALUES (?,?,?,?,?,?)",
        (_now(), None, target_id, event, reason, _json(detail) if detail is not None else None),
    )


def create_target(name: str, url: str, *, enabled: bool = True,
                  conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return create_target(name, url, enabled=enabled, conn=own_conn)
    ensure_schema(conn)
    name = _clean_name(name)
    url = _clean_url(url)
    stamp = _now()
    try:
        cur = conn.execute(
            """INSERT INTO np2_targets(name,url,enabled,sort_order,created_at,updated_at)
               VALUES (?,?,?,(SELECT COALESCE(MAX(sort_order),-1)+1 FROM np2_targets),?,?)""",
            (name, url, 1 if enabled else 0, stamp, stamp),
        )
    except sqlite3.IntegrityError:
        raise HealthError("duplicate_name", "检测目标名称已存在")
    target_id = int(cur.lastrowid)
    _bump_config_version(conn)
    _record_target_event(conn, "target_added", target_id=target_id,
                         reason="新增检测目标", detail={"name": name, "url": url})
    invalidate_all(conn, reason="probe_target_added")
    return target(target_id, conn=conn)


def update_target(target_id: int, *, name: str | None = None, url: str | None = None,
                  enabled: bool | None = None, sort_order: int | None = None,
                  conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return update_target(target_id, name=name, url=url, enabled=enabled,
                                 sort_order=sort_order, conn=own_conn)
    ensure_schema(conn)
    current = target(target_id, conn=conn)
    technical = False
    before = {"name": current["name"], "url": current["url"], "enabled": current["enabled"]}
    if name is not None:
        new_name = _clean_name(name)
        if new_name != current["name"]:
            try:
                conn.execute("UPDATE np2_targets SET name=? WHERE id=?", (new_name, target_id))
            except sqlite3.IntegrityError:
                raise HealthError("duplicate_name", "检测目标名称已存在")
    if url is not None:
        new_url = _clean_url(url)
        if new_url != current["url"]:
            conn.execute("UPDATE np2_targets SET url=? WHERE id=?", (new_url, target_id))
            technical = True
    if enabled is not None:
        enabled = bool(enabled)
        if not enabled and current["enabled"] and _enabled_count(conn, exclude=target_id) < 2:
            raise HealthError("min_targets", "至少必须保留两个有效启用检测目标")
        if enabled != current["enabled"]:
            conn.execute("UPDATE np2_targets SET enabled=? WHERE id=?",
                         (1 if enabled else 0, target_id))
            technical = True
    if sort_order is not None:
        conn.execute("UPDATE np2_targets SET sort_order=? WHERE id=?",
                     (int(sort_order), target_id))
    conn.execute("UPDATE np2_targets SET updated_at=? WHERE id=?", (_now(), target_id))
    after = target(target_id, conn=conn)
    _record_target_event(conn, "target_updated", target_id=target_id,
                         reason="修改检测目标", detail={"before": before,
                                                         "after": {"name": after["name"],
                                                                   "url": after["url"],
                                                                   "enabled": after["enabled"]}})
    if technical:
        _bump_config_version(conn)
        invalidate_all(conn, reason="probe_target_changed")
    return after


def delete_target(target_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return delete_target(target_id, conn=own_conn)
    ensure_schema(conn)
    current = target(target_id, conn=conn)
    if current["enabled"] and _enabled_count(conn, exclude=target_id) < 2:
        raise HealthError("min_targets", "至少必须保留两个有效启用检测目标")
    conn.execute("DELETE FROM np2_targets WHERE id=?", (target_id,))
    _record_target_event(conn, "target_deleted", target_id=target_id,
                         reason="删除检测目标", detail={"name": current["name"], "url": current["url"]})
    _bump_config_version(conn)
    invalidate_all(conn, reason="probe_target_deleted")
    return {"deleted": target_id}


def restore_defaults(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return restore_defaults(conn=own_conn)
    ensure_schema(conn)
    stamp = _now()
    conn.execute("DELETE FROM np2_targets")
    for idx, (name, url) in enumerate(DEFAULT_TARGETS):
        conn.execute(
            """INSERT INTO np2_targets(name,url,enabled,sort_order,created_at,updated_at)
               VALUES (?,?,1,?,?,?)""",
            (name, url, idx, stamp, stamp),
        )
    _bump_config_version(conn)
    _record_target_event(conn, "targets_default", reason="恢复默认检测目标")
    invalidate_all(conn, reason="probe_targets_restored")
    return list_targets(conn=conn)


def invalidate_all(conn: sqlite3.Connection, *, reason: str) -> None:
    """Discard every old-config conclusion without deleting history.

    P1 inventory identity is untouched; only the health axis is reset to Unknown,
    the node is placed in observation, and all subsequent checks start from the
    new target generation.  Competition state is deliberately preserved (P2 does
    not own the P5 competition rules).
    """
    stamp = _now()
    conn.execute(
        """UPDATE np2_node_state SET state='unknown', consecutive_failures=0,
           consecutive_successes=0, last_classification='unknown', last_reason=?,
           next_due_at=?, updated_at=? WHERE node_id IN
           (SELECT id FROM np_nodes WHERE source_active=1)""",
        (reason, stamp, stamp),
    )
    conn.execute(
        """UPDATE np2_checks SET status='unknown', reason='config_changed',
           finished_at=? WHERE status='running'""",
        (stamp,),
    )
    conn.execute("DELETE FROM np2_queue WHERE status IN ('pending','running')")
    conn.execute(
        """UPDATE np_nodes SET base_health='unknown', data_status='insufficient',
           management_layer='observation', observation_reason='probe_config_changed',
           updated_at=? WHERE source_active=1""",
        (stamp,),
    )
    _record_target_event(conn, "config_invalidated", reason=reason)


def record_target_check(target_id: int, status: str, *, scope: str = "selftest",
                        duration_ms: int | None = None, detail: Any = None,
                        conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return record_target_check(target_id, status, scope=scope,
                                       duration_ms=duration_ms, detail=detail, conn=own_conn)
    ensure_schema(conn)
    if status not in TARGET_STATES:
        raise HealthError("invalid_status", "检测目标状态无效")
    stamp = _now()
    conn.execute(
        """INSERT INTO np2_target_checks(target_id,scope,status,duration_ms,detail_json,checked_at)
           VALUES (?,?,?,?,?,?)""",
        (target_id, scope, status, duration_ms, _json(detail) if detail is not None else None, stamp),
    )
    conn.execute(
        """UPDATE np2_targets SET last_check_at=?, last_check_status=?,
           last_check_error=? WHERE id=?""",
        (stamp, status, detail.get("reason") if isinstance(detail, dict) else None, target_id),
    )
    return {"target_id": target_id, "status": status, "checked_at": stamp}


def sync_states(conn: sqlite3.Connection) -> int:
    ensure_schema(conn)
    stamp = _now()
    before = int(conn.execute("SELECT COUNT(*) FROM np2_node_state").fetchone()[0])
    conn.execute(
        """INSERT OR IGNORE INTO np2_node_state(node_id,state,next_due_at,updated_at)
           SELECT id,'new',?,? FROM np_nodes WHERE source_active=1""",
        (stamp, stamp),
    )
    return int(conn.execute("SELECT COUNT(*) FROM np2_node_state").fetchone()[0]) - before


def enqueue_due(conn: sqlite3.Connection, *, trigger: str = "normal",
                priority: int = 1, limit: int = 500) -> int:
    """Queue nodes whose persisted next_due time has arrived.

    The partial unique index is the single authority preventing duplicate node
    queue entries; this function is therefore safe to call on every tick.
    """
    ensure_schema(conn)
    stamp = _now()
    rows = conn.execute(
        """SELECT s.node_id, s.next_due_at, s.state
           FROM np2_node_state s JOIN np_nodes n ON n.id=s.node_id
           WHERE n.source_active=1 AND COALESCE(s.next_due_at, ?) <= ?
           ORDER BY COALESCE(s.next_due_at, ?), s.node_id LIMIT ?""",
        (stamp, stamp, stamp, limit),
    ).fetchall()
    added = 0
    for row in rows:
        state = row["state"] or "new"
        if trigger == "normal":
            if state == "exception":
                row_trigger, row_priority = "exception_recheck", 0
            elif state in ("unhealthy", "recovering"):
                row_trigger, row_priority = "recovery", 0
            elif state == "delay":
                row_trigger, row_priority = "observation", 0
            else:
                row_trigger, row_priority = "normal", priority
        else:
            row_trigger, row_priority = trigger, priority
        cur = conn.execute(
            """INSERT OR IGNORE INTO np2_queue(node_id,due_at,priority,trigger,status,created_at)
               VALUES (?,?,?,?, 'pending', ?)""",
            (int(row["node_id"]), row["next_due_at"] or stamp, row_priority, row_trigger, stamp),
        )
        added += int(cur.rowcount or 0)
    return added


def enqueue_nodes(conn: sqlite3.Connection, node_ids: Iterable[int], *,
                  trigger: str = "manual", priority: int = 0) -> int:
    ensure_schema(conn)
    stamp = _now()
    added = 0
    for node_id in node_ids:
        row = conn.execute(
            "SELECT id FROM np_nodes WHERE id=? AND source_active=1", (int(node_id),)
        ).fetchone()
        if row is None:
            continue
        existing = conn.execute(
            "SELECT id,status FROM np2_queue WHERE node_id=?", (int(node_id),)
        ).fetchone()
        if existing:
            if existing["status"] == "pending":
                conn.execute(
                    """UPDATE np2_queue SET due_at=?, priority=?, trigger=? WHERE id=?""",
                    (stamp, priority, trigger, int(existing["id"])),
                )
            continue
        conn.execute(
            """INSERT INTO np2_queue(node_id,due_at,priority,trigger,status,created_at)
               VALUES (?,?,?,?, 'pending', ?)""",
            (int(node_id), stamp, priority, trigger, stamp),
        )
        added += 1
    return added


def reserve_queue(conn: sqlite3.Connection, *, limit: int) -> list[dict]:
    ensure_schema(conn)
    if limit <= 0:
        return []
    rows = conn.execute(
        """SELECT q.id,q.node_id,q.due_at,q.priority,q.trigger,
                  n.current_name,n.protocol,n.server,n.port,n.params,n.airport_id
           FROM np2_queue q JOIN np_nodes n ON n.id=q.node_id
           WHERE q.status='pending' AND q.due_at<=?
           ORDER BY q.priority,q.due_at,q.id LIMIT ?""",
        (_now(), int(limit)),
    ).fetchall()
    reserved = []
    for row in rows:
        changed = conn.execute(
            "UPDATE np2_queue SET status='running', started_at=? WHERE id=? AND status='pending'",
            (_now(), int(row["id"])),
        ).rowcount
        if changed:
            reserved.append(dict(row))
    return reserved


def finish_queue(conn: sqlite3.Connection, queue_id: int) -> None:
    conn.execute("DELETE FROM np2_queue WHERE id=? AND status='running'", (int(queue_id),))


def active_queue_count(conn: sqlite3.Connection | None = None) -> int:
    if conn is None:
        conn = _read_conn()
    return int(conn.execute(
        "SELECT COUNT(*) FROM np2_queue WHERE status='running'"
    ).fetchone()[0])


def recover_stale_queue(conn: sqlite3.Connection) -> int:
    """A process restart cannot leave a queue row permanently running."""
    ensure_schema(conn)
    return int(conn.execute(
        "UPDATE np2_queue SET status='pending', started_at=NULL WHERE status='running'"
    ).rowcount)


def create_check(node_id: int, trigger: str, config_version: int,
                 target_count: int, *, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return create_check(node_id, trigger, config_version, target_count, conn=own_conn)
    ensure_schema(conn)
    stamp = _now()
    cur = conn.execute(
        """INSERT INTO np2_checks(node_id,trigger,status,config_version,target_count,started_at)
           VALUES (?,?, 'running', ?, ?, ?)""",
        (int(node_id), trigger, int(config_version), int(target_count), stamp),
    )
    return {"id": int(cur.lastrowid), "node_id": int(node_id), "trigger": trigger,
            "config_version": int(config_version), "target_count": int(target_count),
            "started_at": stamp}


def save_target_result(conn: sqlite3.Connection, check_id: int, result: dict) -> int:
    """Persist one target's stages, five TCP samples and the computed P80."""
    ensure_schema(conn)
    stamp = _now()
    cur = conn.execute(
        """INSERT INTO np2_target_results
           (check_id,target_id,target_name,target_url,status,reason,p80_ms,
            tcp_valid_count,tls_ms,https_ms,https_status,health_pass,
            started_at,finished_at,duration_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(check_id), int(result.get("target_id") or 0), result.get("target_name") or "",
         result.get("target_url") or "", result.get("status") or "unknown",
         result.get("reason"), result.get("p80_ms"), int(result.get("tcp_valid_count") or 0),
         result.get("tls_ms"), result.get("https_ms"), result.get("https_status"),
         1 if result.get("health_pass") else 0, result.get("started_at") or stamp,
         result.get("finished_at") or stamp, result.get("duration_ms")),
    )
    result_id = int(cur.lastrowid)
    for stage in result.get("stages") or []:
        conn.execute(
            """INSERT INTO np2_stage_results
               (check_id,target_result_id,stage,executed,ok,status,latency_ms,slow,detail_json)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (int(check_id), result_id, stage.get("stage"), 1 if stage.get("executed") else 0,
             1 if stage.get("ok") else 0, stage.get("status") or "unexecuted",
             stage.get("latency_ms"), 1 if stage.get("slow") else 0,
             _json(stage.get("detail")) if stage.get("detail") is not None else None),
        )
    for sample in result.get("tcp_samples") or []:
        conn.execute(
            """INSERT INTO np2_tcp_samples
               (check_id,target_result_id,sample_index,ok,latency_ms,error,sampled_at)
               VALUES (?,?,?,?,?,?,?)""",
            (int(check_id), result_id, int(sample.get("index") or 0),
             1 if sample.get("ok") else 0, sample.get("latency_ms"), sample.get("error"),
             sample.get("sampled_at") or stamp),
        )
    return result_id


def finish_check(check_id: int, *, status: str, reason: str | None,
                 summary: dict, conn: sqlite3.Connection | None = None) -> dict | None:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return finish_check(check_id, status=status, reason=reason, summary=summary,
                                conn=own_conn)
    ensure_schema(conn)
    row = conn.execute("SELECT * FROM np2_checks WHERE id=?", (int(check_id),)).fetchone()
    if row is None or row["status"] != "running":
        return None
    started = datetime.strptime(row["started_at"], "%Y-%m-%d %H:%M:%S")
    duration = int((datetime.now() - started).total_seconds() * 1000)
    conn.execute(
        """UPDATE np2_checks SET status=?, reason=?, success_count=?, failure_count=?,
           unknown_count=?, p80_ms=?, health_pass=?, finished_at=?, duration_ms=?
           WHERE id=?""",
        (status, reason, int(summary.get("success_count") or 0),
         int(summary.get("failure_count") or 0), int(summary.get("unknown_count") or 0),
         summary.get("p80_ms"), 1 if summary.get("health_pass") else 0,
         _now(), duration, int(check_id)),
    )
    return {"id": int(check_id), "status": status, "duration_ms": duration}


def update_node_state(conn: sqlite3.Connection, node_id: int, *, state: str,
                      failures: int, successes: int, classification: str,
                      reason: str, next_due_at: str, check_id: int | None,
                      conn_ts: str | None = None) -> None:
    ensure_schema(conn)
    stamp = conn_ts or _now()
    conn.execute(
        """INSERT INTO np2_node_state
           (node_id,state,consecutive_failures,consecutive_successes,last_classification,
            last_reason,last_run_at,next_due_at,last_check_id,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id) DO UPDATE SET
             state=excluded.state, consecutive_failures=excluded.consecutive_failures,
             consecutive_successes=excluded.consecutive_successes,
             last_classification=excluded.last_classification,
             last_reason=excluded.last_reason, last_run_at=excluded.last_run_at,
             next_due_at=excluded.next_due_at, last_check_id=excluded.last_check_id,
             updated_at=excluded.updated_at""",
        (int(node_id), state, int(failures), int(successes), classification, reason,
         stamp, next_due_at, check_id, stamp),
    )


def record_event(conn: sqlite3.Connection, event: str, *, node_id: int | None = None,
                 target_id: int | None = None, reason: str | None = None,
                 detail: Any = None) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT INTO np2_events(at,node_id,target_id,event,reason,detail_json) VALUES (?,?,?,?,?,?)",
        (_now(), node_id, target_id, event, reason,
         _json(detail) if detail is not None else None),
    )


def get_state(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn()
    row = conn.execute("SELECT * FROM np2_node_state WHERE node_id=?", (int(node_id),)).fetchone()
    return dict(row) if row else {"node_id": int(node_id), "state": "new",
                                 "consecutive_failures": 0, "consecutive_successes": 0}


def check(check_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn()
    row = conn.execute("SELECT * FROM np2_checks WHERE id=?", (int(check_id),)).fetchone()
    if row is None:
        raise HealthError("check_not_found", "检测记录不存在")
    return dict(row)


def check_results(check_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    targets = [dict(r) for r in conn.execute(
        "SELECT * FROM np2_target_results WHERE check_id=? ORDER BY id", (int(check_id),)
    ).fetchall()]
    for item in targets:
        item["stages"] = [dict(r) for r in conn.execute(
            "SELECT * FROM np2_stage_results WHERE target_result_id=? ORDER BY id",
            (int(item["id"]),)
        ).fetchall()]
        item["tcp_samples"] = [dict(r) for r in conn.execute(
            "SELECT * FROM np2_tcp_samples WHERE target_result_id=? ORDER BY sample_index",
            (int(item["id"]),)
        ).fetchall()]
        for stage in item["stages"]:
            stage["detail"] = db.loads(stage.pop("detail_json", None), None)
        item["health_pass"] = bool(item["health_pass"])
        from backend.health.prober import tcp_grade
        item["p80_grade"] = tcp_grade(item.get("p80_ms"))
    return targets


def latest_check_id(node_id: int, *, conn: sqlite3.Connection | None = None) -> int | None:
    if conn is None:
        conn = _read_conn()
    row = conn.execute(
        "SELECT id FROM np2_checks WHERE node_id=? ORDER BY id DESC LIMIT 1", (int(node_id),)
    ).fetchone()
    return int(row["id"]) if row else None


def recent_checks(node_id: int, *, limit: int = 20,
                  conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    return [dict(r) for r in conn.execute(
        """SELECT id,trigger,status,reason,config_version,success_count,failure_count,
                  unknown_count,p80_ms,health_pass,started_at,finished_at,duration_ms
           FROM np2_checks WHERE node_id=? ORDER BY id DESC LIMIT ?""",
        (int(node_id), int(limit)),
    ).fetchall()]


def summary(*, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    states = {r["state"]: int(r["count"]) for r in conn.execute(
        "SELECT state, COUNT(*) AS count FROM np2_node_state GROUP BY state"
    ).fetchall()}
    targets = list_targets(conn=conn)
    enabled = [t for t in targets if t["enabled"]]
    queue_rows = conn.execute(
        "SELECT status, COUNT(*) AS count FROM np2_queue GROUP BY status"
    ).fetchall()
    queue = {r["status"]: int(r["count"]) for r in queue_rows}
    p1 = p1_store.summary()
    return {
        "targets": targets,
        "enabled_targets": len(enabled),
        "target_healthy": len([t for t in enabled if t["last_check_status"] == "success"]),
        "states": {"healthy": states.get("healthy", 0), "exception": states.get("exception", 0),
                   "delay": states.get("delay", 0),
                   "unhealthy": states.get("unhealthy", 0), "recovering": states.get("recovering", 0),
                   "unknown": states.get("unknown", 0), "new": states.get("new", 0)},
        "queue": {"pending": queue.get("pending", 0), "running": queue.get("running", 0)},
        "config_version": config_version(conn),
        "interval_minutes": _setting_int(conn, "p2_interval_minutes", 30),
        "concurrency": _setting_int(conn, "p2_concurrency", 10),
        "p1": p1,
    }


def _setting_int(conn: sqlite3.Connection, key: str, default: int) -> int:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    try:
        return int((row["value"] if row else default) or default)
    except (TypeError, ValueError):
        return default


def list_nodes(*, page: int = 1, per_page: int = 20, q: str | None = None,
               state: str | None = None, classification: str | None = None,
               layer: str | None = None,
               conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    where = ["1=1"]
    params: list[Any] = []
    if q:
        where.append("(n.current_name LIKE ? OR n.server LIKE ? OR n.source_name LIKE ?)")
        like = f"%{q}%"
        params.extend([like, like, like])
    if state:
        where.append("COALESCE(s.state,'new')=?")
        params.append(state)
    if classification:
        where.append("c.status=?")
        params.append(classification)
    if layer:
        where.append("n.management_layer=?")
        params.append(layer)
    total = int(conn.execute(
        f"""SELECT COUNT(*) FROM np_nodes n LEFT JOIN np2_node_state s ON s.node_id=n.id
            LEFT JOIN (SELECT node_id, MAX(id) AS id FROM np2_checks GROUP BY node_id) l
              ON l.node_id=n.id
            LEFT JOIN np2_checks c ON c.id=l.id
            WHERE n.source_active=1 AND {' AND '.join(where)}""",
        params,
    ).fetchone()[0])
    page = max(1, int(page))
    per_page = min(100, max(1, int(per_page)))
    rows = conn.execute(
        f"""SELECT n.id,n.current_name,n.protocol,n.server,n.port,n.region,n.source_name,
                   n.management_layer,n.base_health,n.data_status,n.observation_reason,
                   n.last_test_at,n.competition_state,
                   COALESCE(s.state,'new') AS probe_state,
                   s.consecutive_failures,s.consecutive_successes,s.last_reason,
                   s.last_run_at,s.next_due_at,s.last_check_id,
                   c.status AS last_status,c.reason AS last_check_reason,c.p80_ms,
                   c.health_pass,c.success_count,c.failure_count,c.unknown_count,
                   c.started_at AS check_started_at,c.finished_at AS check_finished_at,
                   c.trigger AS last_trigger
            FROM np_nodes n LEFT JOIN np2_node_state s ON s.node_id=n.id
            LEFT JOIN (SELECT node_id, MAX(id) AS id FROM np2_checks GROUP BY node_id) l
              ON l.node_id=n.id
            LEFT JOIN np2_checks c ON c.id=l.id
            WHERE n.source_active=1 AND {' AND '.join(where)}
            ORDER BY CASE COALESCE(s.state,'new')
                       WHEN 'unhealthy' THEN 0 WHEN 'exception' THEN 1
                       WHEN 'recovering' THEN 2 WHEN 'unknown' THEN 3
                       WHEN 'new' THEN 4 ELSE 5 END,
                     COALESCE(s.next_due_at,'') ASC, n.id ASC
            LIMIT ? OFFSET ?""",
        params + [per_page, (page - 1) * per_page],
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["health_pass"] = bool(item["health_pass"])
        items.append(item)
    return {"items": items, "total": total, "page": page, "per_page": per_page}


def node_detail(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    node = p1_store.detail(node_id, conn=conn)
    state = get_state(node_id, conn=conn)
    latest = latest_check_id(node_id, conn=conn)
    result = None
    if latest is not None:
        payload = check(latest, conn=conn)
        payload["targets"] = check_results(latest, conn=conn)
        result = payload
    return {
        "node": node,
        "state": state,
        "latest": result,
        "recent_checks": recent_checks(node_id, conn=conn),
        "next_due_at": state.get("next_due_at"),
        "observation_reason": node.get("observation_reason"),
    }


def stale_running_checks(conn: sqlite3.Connection) -> int:
    return int(conn.execute(
        "SELECT COUNT(*) FROM np2_checks WHERE status='running'"
    ).fetchone()[0])
