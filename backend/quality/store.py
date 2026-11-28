"""P4 quality/speed evaluation storage (np4_* side tables).

P4 reads P1 inventory, P2 probes, P3 states and existing speed history.  It
never writes any of them.  Every quality result is kept in both a current row
and an hourly history row so later phases can audit the evidence.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Iterable

from backend.database import db
from backend.business import store as business_store
from backend.quality import calculator

_DDL = (
    """CREATE TABLE IF NOT EXISTS np4_current (
        node_id INTEGER PRIMARY KEY,
        evaluated_at TEXT NOT NULL,
        quality_score REAL,
        available_score REAL,
        delay_score REAL,
        stability_score REAL,
        maturity_score REAL,
        mature INTEGER NOT NULL DEFAULT 0,
        quality_pass INTEGER NOT NULL DEFAULT 0,
        quality_state TEXT NOT NULL DEFAULT 'unknown',
        speed_grade TEXT NOT NULL DEFAULT 'empty',
        speed_mb_s REAL,
        speed_kb_s REAL,
        speed_run_id INTEGER,
        speed_at TEXT,
        speed_bytes INTEGER NOT NULL DEFAULT 0,
        valid_snapshots INTEGER NOT NULL DEFAULT 0,
        valid_days INTEGER NOT NULL DEFAULT 0,
        full_days INTEGER NOT NULL DEFAULT 0,
        coverage REAL,
        reasons_json TEXT,
        detail_json TEXT,
        config_version INTEGER NOT NULL DEFAULT 1
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np4_current_quality ON np4_current (quality_state, quality_score)",
    "CREATE INDEX IF NOT EXISTS idx_np4_current_speed ON np4_current (speed_grade)",
    """CREATE TABLE IF NOT EXISTS np4_evaluations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        bucket TEXT NOT NULL,
        node_id INTEGER NOT NULL,
        evaluated_at TEXT NOT NULL,
        quality_score REAL,
        maturity_score REAL,
        mature INTEGER NOT NULL DEFAULT 0,
        quality_pass INTEGER NOT NULL DEFAULT 0,
        speed_grade TEXT NOT NULL DEFAULT 'empty',
        speed_mb_s REAL,
        config_version INTEGER NOT NULL DEFAULT 1,
        detail_json TEXT,
        UNIQUE (node_id, bucket)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np4_eval_node ON np4_evaluations (node_id, evaluated_at DESC)",
    """CREATE TABLE IF NOT EXISTS np4_speed_meta (
        run_id INTEGER PRIMARY KEY,
        node_id INTEGER NOT NULL,
        airport_id INTEGER,
        status TEXT NOT NULL,
        attribution TEXT NOT NULL,
        valid INTEGER NOT NULL DEFAULT 0,
        reason TEXT,
        bytes_total INTEGER NOT NULL DEFAULT 0,
        config_version INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np4_speed_meta_node ON np4_speed_meta (node_id, run_id DESC)",
    """CREATE TABLE IF NOT EXISTS np4_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        node_id INTEGER,
        event TEXT NOT NULL,
        reason TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np4_events_node ON np4_events (node_id, id DESC)",
)

SETTING_KEYS = (
    "p4_window_days", "p4_weights", "p4_min_daily_snapshots",
    "p4_min_quality_days", "p4_min_maturity", "p4_quality_pass",
    "p4_speed_excellent", "p4_speed_good", "p4_speed_minimum",
    "p4_speed_ttl_days", "p4_speed_limit_mb", "p4_speed_window_start",
    "p4_speed_window_end", "p4_speed_schedule_enabled", "p4_speed_every_days",
    "p4_speed_concurrency", "p4_timezone",
)
STANDARD_SPEED_KEYS = (
    "speed_window_start", "speed_window_end", "speed_schedule_enabled",
    "speed_schedule_every_days", "speed_parallel_nodes", "speed_traffic_limit_mb",
    "speed_result_ttl_days",
)


def _now() -> str:
    return db.now()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        business_store.ensure_schema(conn)
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
    defaults = {
        "p4_window_days": "7",
        "p4_weights": _json(calculator.DEFAULT_WEIGHTS),
        "p4_min_daily_snapshots": "12",
        "p4_min_quality_days": "5",
        "p4_min_maturity": "70",
        "p4_quality_pass": "75",
        "p4_speed_excellent": "5",
        "p4_speed_good": "2",
        "p4_speed_minimum": "0.5",
        "p4_speed_ttl_days": "7",
        "p4_speed_limit_mb": "50",
        "p4_speed_window_start": "17:00",
        "p4_speed_window_end": "24:00",
        "p4_speed_schedule_enabled": "1",
        "p4_speed_every_days": "1",
        "p4_speed_concurrency": "4",
        "p4_timezone": "Asia/Shanghai",
        "p4_config_version": "1",
    }
    for key, value in defaults.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))
    # v2.038's speed page still used 18:00–23:30 and a 3-day TTL.  Move only
    # those untouched defaults once; custom windows/TTLs are preserved.
    if conn.execute("SELECT value FROM settings WHERE key='p4_speed_migrated'").fetchone() is None:
        start = conn.execute("SELECT value FROM settings WHERE key='speed_window_start'").fetchone()
        end = conn.execute("SELECT value FROM settings WHERE key='speed_window_end'").fetchone()
        if (not start or start["value"] == "18:00") and (not end or end["value"] == "23:30"):
            conn.execute("INSERT INTO settings(key,value) VALUES ('speed_window_start','17:00') "
                         "ON CONFLICT(key) DO UPDATE SET value='17:00'")
            conn.execute("INSERT INTO settings(key,value) VALUES ('speed_window_end','24:00') "
                         "ON CONFLICT(key) DO UPDATE SET value='24:00'")
        ttl = conn.execute("SELECT value FROM settings WHERE key='speed_result_ttl_days'").fetchone()
        if not ttl or ttl["value"] in ("3", "3.0"):
            conn.execute("INSERT INTO settings(key,value) VALUES ('speed_result_ttl_days','7') "
                         "ON CONFLICT(key) DO UPDATE SET value='7'")
        conn.execute("INSERT INTO settings(key,value) VALUES ('p4_speed_migrated','1')")
    for key, value in (("speed_schedule_enabled", "1"), ("speed_schedule_every_days", "1"),
                       ("speed_parallel_nodes", "4"), ("speed_traffic_limit_mb", "50")):
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)", (key, value))


def _setting(conn: sqlite3.Connection | None, key: str, default: str) -> str:
    if conn is None:
        row = db.row("SELECT value FROM settings WHERE key=?", (key,))
    else:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def _setting_int(conn: sqlite3.Connection | None, key: str, default: int) -> int:
    try:
        return int(float(_setting(conn, key, str(default))))
    except (TypeError, ValueError):
        return default


def _setting_bool(conn: sqlite3.Connection | None, key: str, default: bool) -> bool:
    return _setting(conn, key, "1" if default else "0") in ("1", "true", "True")


def config_version(conn: sqlite3.Connection | None = None) -> int:
    return _setting_int(conn, "p4_config_version", 1)


def get_settings(conn: sqlite3.Connection | None = None) -> dict:
    weights_raw = _setting(conn, "p4_weights", _json(calculator.DEFAULT_WEIGHTS))
    try:
        weights = json.loads(weights_raw)
    except (TypeError, ValueError):
        weights = dict(calculator.DEFAULT_WEIGHTS)
    settings = {
        "window_days": 7,
        "weights": {band: float(weights.get(band, default))
                    for band, default in calculator.DEFAULT_WEIGHTS.items()},
        "min_daily_snapshots": _setting_int(conn, "p4_min_daily_snapshots", 12),
        "min_quality_days": _setting_int(conn, "p4_min_quality_days", 5),
        "min_maturity": _setting_int(conn, "p4_min_maturity", 70),
        "quality_pass": _setting_int(conn, "p4_quality_pass", 75),
        "speed_excellent": float(_setting(conn, "p4_speed_excellent", "5")),
        "speed_good": float(_setting(conn, "p4_speed_good", "2")),
        "speed_minimum": float(_setting(conn, "p4_speed_minimum", "0.5")),
        "speed_ttl_days": _setting_int(conn, "p4_speed_ttl_days", 7),
        "speed_limit_mb": _setting_int(conn, "p4_speed_limit_mb", 50),
        "speed_window_start": _setting(conn, "p4_speed_window_start", "17:00"),
        "speed_window_end": _setting(conn, "p4_speed_window_end", "24:00"),
        "speed_schedule_enabled": _setting_bool(conn, "p4_speed_schedule_enabled", True),
        "speed_every_days": _setting_int(conn, "p4_speed_every_days", 1),
        "speed_concurrency": _setting_int(conn, "p4_speed_concurrency", 4),
        "timezone": _setting(conn, "p4_timezone", "Asia/Shanghai"),
        "config_version": config_version(conn),
    }
    # Keep the legacy speed queue in sync with the P4-facing values.
    settings["speed_window_start_effective"] = _setting(conn, "speed_window_start", "17:00")
    settings["speed_window_end_effective"] = _setting(conn, "speed_window_end", "24:00")
    return settings


def update_settings(data: dict, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return update_settings(data, conn=own_conn)
    ensure_defaults(conn)
    current = get_settings(conn)
    merged = calculator.validate_settings(data, current)
    float_keys = ("p4_speed_excellent", "p4_speed_good", "p4_speed_minimum")
    int_keys = ("p4_min_daily_snapshots", "p4_min_quality_days", "p4_min_maturity",
                "p4_quality_pass", "p4_speed_ttl_days", "p4_speed_limit_mb",
                "p4_speed_every_days", "p4_speed_concurrency")
    values = {
        "p4_window_days": 7,
        "p4_weights": _json(merged["weights"]),
        "p4_min_daily_snapshots": merged["min_daily_snapshots"],
        "p4_min_quality_days": merged["min_quality_days"],
        "p4_min_maturity": merged["min_maturity"],
        "p4_quality_pass": merged["quality_pass"],
        "p4_speed_excellent": merged["speed_excellent"],
        "p4_speed_good": merged["speed_good"],
        "p4_speed_minimum": merged["speed_minimum"],
        "p4_speed_ttl_days": merged["speed_ttl_days"],
        "p4_speed_limit_mb": merged["speed_limit_mb"],
        "p4_speed_window_start": merged["speed_window_start"],
        "p4_speed_window_end": merged["speed_window_end"],
        "p4_speed_schedule_enabled": "1" if merged["speed_schedule_enabled"] else "0",
        "p4_speed_every_days": merged["speed_every_days"],
        "p4_speed_concurrency": merged["speed_concurrency"],
        "p4_timezone": merged.get("timezone") or "Asia/Shanghai",
    }
    for key, value in values.items():
        conn.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
    # Existing speed queue reads these standard keys.
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_window_start',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (merged["speed_window_start"],))
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_window_end',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (merged["speed_window_end"],))
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_schedule_enabled',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 ("1" if merged["speed_schedule_enabled"] else "0",))
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_schedule_every_days',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (str(merged["speed_every_days"]),))
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_parallel_nodes',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (str(merged["speed_concurrency"]),))
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_traffic_limit_mb',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (str(merged["speed_limit_mb"]),))
    conn.execute("INSERT INTO settings(key,value) VALUES ('speed_result_ttl_days',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (str(merged["speed_ttl_days"]),))
    version = config_version(conn) + 1
    conn.execute("INSERT INTO settings(key,value) VALUES ('p4_config_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
    conn.execute("INSERT INTO np4_events(at,event,reason) VALUES (?,?,?)",
                 (_now(), "settings_changed", "P4 评价参数更新"))
    return get_settings(conn)


def record_event(event: str, *, node_id: int | None = None, reason: str | None = None,
                 detail: Any = None, conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        conn.execute("INSERT INTO np4_events(at,node_id,event,reason,detail_json) VALUES (?,?,?,?,?)",
                     (_now(), node_id, event, reason, _json(detail) if detail is not None else None))
        return
    db.execute("INSERT INTO np4_events(at,node_id,event,reason,detail_json) VALUES (?,?,?,?,?)",
               (_now(), node_id, event, reason, _json(detail) if detail is not None else None))


def _speed_attribution(status: str, error_type: str | None = None) -> tuple[bool, str, str | None]:
    status = str(status or "").lower()
    if status in ("ok", "partial"):
        return True, "success", None
    if status == "excluded":
        return False, "excluded", "airport_excluded"
    if status == "node_error":
        return False, "node_failure", "node_failure"
    if status == "source_unavailable":
        return False, "source_failure", "source_failure"
    if status == "engine_error":
        return False, "detector_error", "detector_error"
    if status == "canceled":
        return False, "interrupted", "interrupted"
    if status == "failed":
        if error_type == "node":
            return False, "node_failure", "node_failure"
        if error_type == "source":
            return False, "source_failure", "source_failure"
        return False, "ambiguous", "timeout_unknown"
    if status in ("running", "queued"):
        return False, "pending", "pending"
    return False, "ambiguous", status or "unknown"


def sync_speed_meta(conn: sqlite3.Connection | None = None) -> int:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return sync_speed_meta(own_conn)
    ensure_schema(conn)
    rows = conn.execute(
        """SELECT r.*, n.airport_id, a.speed_excluded
           FROM speed_runs r
           LEFT JOIN nodes n ON n.id=r.node_id
           LEFT JOIN airports a ON a.id=n.airport_id
           WHERE r.id NOT IN (SELECT run_id FROM np4_speed_meta)
           ORDER BY r.id LIMIT 2000"""
    ).fetchall()
    version = config_version(conn)
    for row in rows:
        valid, attribution, reason = _speed_attribution(row["status"], row["error_type"])
        if row["speed_excluded"]:
            valid, attribution, reason = False, "excluded", "airport_excluded"
        conn.execute(
            """INSERT OR IGNORE INTO np4_speed_meta
               (run_id,node_id,airport_id,status,attribution,valid,reason,bytes_total,
                config_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (row["id"], row["node_id"], row["airport_id"], row["status"], attribution,
             1 if valid else 0, reason, int(row["bytes_total"] or 0), version, _now()),
        )
    return len(rows)


def speed_runs_for(node_id: int, *, limit: int = 20,
                   conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    rows = conn.execute(
        """SELECT r.*, m.attribution, m.valid, m.reason AS meta_reason,
                  m.config_version AS speed_config_version
           FROM speed_runs r LEFT JOIN np4_speed_meta m ON m.run_id=r.id
           WHERE r.node_id=? ORDER BY r.id DESC LIMIT ?""",
        (int(node_id), max(1, min(int(limit), 100))),
    ).fetchall()
    return [dict(row) for row in rows]


def save_evaluation(node_id: int, result: dict, *, now: datetime | None = None,
                    conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return save_evaluation(node_id, result, now=now, conn=own_conn)
    ensure_schema(conn)
    evaluated_at = (now or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    bucket = (now or datetime.now()).strftime("%Y-%m-%d %H:00")
    detail = _json(result)
    reasons = _json(result.get("reasons") or [])
    args_current = (
        evaluated_at, result.get("quality_score"), result.get("available_score"),
        result.get("delay_score"), result.get("stability_score"),
        result.get("maturity_score"), 1 if result.get("mature") else 0,
        1 if result.get("quality_pass") else 0, result.get("quality_state") or "unknown",
        result.get("speed_grade") or "empty", result.get("speed_mb_s"),
        result.get("speed_kb_s"), result.get("speed_run_id"), result.get("speed_at"),
        int(result.get("speed_bytes") or 0), int(result.get("valid_snapshots") or 0),
        int(result.get("valid_days") or 0), int(result.get("full_days") or 0),
        result.get("coverage"), reasons, detail, int(result.get("config_version") or 1),
    )
    conn.execute(
        """INSERT INTO np4_current
           (node_id,evaluated_at,quality_score,available_score,delay_score,stability_score,
            maturity_score,mature,quality_pass,quality_state,speed_grade,speed_mb_s,
            speed_kb_s,speed_run_id,speed_at,speed_bytes,valid_snapshots,valid_days,full_days,
            coverage,reasons_json,detail_json,config_version)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id) DO UPDATE SET
             evaluated_at=excluded.evaluated_at, quality_score=excluded.quality_score,
             available_score=excluded.available_score, delay_score=excluded.delay_score,
             stability_score=excluded.stability_score, maturity_score=excluded.maturity_score,
             mature=excluded.mature, quality_pass=excluded.quality_pass,
             quality_state=excluded.quality_state, speed_grade=excluded.speed_grade,
             speed_mb_s=excluded.speed_mb_s, speed_kb_s=excluded.speed_kb_s,
             speed_run_id=excluded.speed_run_id, speed_at=excluded.speed_at,
             speed_bytes=excluded.speed_bytes, valid_snapshots=excluded.valid_snapshots,
             valid_days=excluded.valid_days, full_days=excluded.full_days,
             coverage=excluded.coverage, reasons_json=excluded.reasons_json,
             detail_json=excluded.detail_json, config_version=excluded.config_version""",
        (int(node_id), *args_current),
    )
    conn.execute(
        """INSERT INTO np4_evaluations
           (bucket,node_id,evaluated_at,quality_score,maturity_score,mature,quality_pass,
            speed_grade,speed_mb_s,config_version,detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id,bucket) DO UPDATE SET
             evaluated_at=excluded.evaluated_at, quality_score=excluded.quality_score,
             maturity_score=excluded.maturity_score, mature=excluded.mature,
             quality_pass=excluded.quality_pass, speed_grade=excluded.speed_grade,
             speed_mb_s=excluded.speed_mb_s, config_version=excluded.config_version,
             detail_json=excluded.detail_json""",
        (bucket, int(node_id), evaluated_at, result.get("quality_score"),
         result.get("maturity_score"), 1 if result.get("mature") else 0,
         1 if result.get("quality_pass") else 0, result.get("speed_grade") or "empty",
         result.get("speed_mb_s"), int(result.get("config_version") or 1), detail),
    )
    return result


def current(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        conn = _read_conn()
    row = conn.execute("SELECT * FROM np4_current WHERE node_id=?", (int(node_id),)).fetchone()
    if not row:
        return None
    item = dict(row)
    item["detail"] = db.loads(item.pop("detail_json"), {}) or {}
    item["reasons"] = db.loads(item.pop("reasons_json"), []) or []
    item["mature"] = bool(item.get("mature"))
    item["quality_pass"] = bool(item.get("quality_pass"))
    return item


def recent_evaluations(node_id: int, *, limit: int = 30,
                       conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    rows = conn.execute(
        "SELECT * FROM np4_evaluations WHERE node_id=? ORDER BY evaluated_at DESC LIMIT ?",
        (int(node_id), max(1, min(int(limit), 200))),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        detail = db.loads(item.pop("detail_json"), {}) or {}
        item.update({"daily": detail.get("daily") or [],
                     "band_stats": detail.get("band_stats") or {},
                     "reasons": detail.get("reasons") or []})
        item["mature"] = bool(item.get("mature"))
        item["quality_pass"] = bool(item.get("quality_pass"))
        items.append(item)
    return items


def node_inventory(*, q: str | None = None, conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn()
    sql = """SELECT n.id, n.current_name, n.first_name, n.server, n.port, n.region,
                    n.airport_id, n.management_layer, n.base_health, n.data_status,
                    n.source_active, a.name AS airport_name, a.speed_excluded
             FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id"""
    args: list[Any] = []
    if q:
        sql += " WHERE (n.current_name LIKE ? OR n.server LIKE ? OR a.name LIKE ?)"
        needle = f"%{q.strip()}%"
        args.extend([needle, needle, needle])
    sql += " ORDER BY a.name, n.current_name, n.id"
    return [dict(row) for row in conn.execute(sql, args).fetchall()]
