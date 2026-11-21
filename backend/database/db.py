"""SQLite access layer: schema, migrations, settings and helpers."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable

from backend import config

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS airports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 60,
    daily_time TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_status TEXT,
    last_error TEXT,
    node_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airport_id INTEGER,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    node_count INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    message TEXT
);

CREATE TABLE IF NOT EXISTS nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airport_id INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    server TEXT NOT NULL,
    port INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    params TEXT NOT NULL,
    region TEXT,
    latency INTEGER,
    fail_count INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_tested_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (airport_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_nodes_airport ON nodes (airport_id);
CREATE INDEX IF NOT EXISTS idx_nodes_server ON nodes (server);

CREATE TABLE IF NOT EXISTS test_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    ident TEXT NOT NULL,
    url TEXT NOT NULL,
    method TEXT NOT NULL DEFAULT 'GET',
    timeout REAL NOT NULL DEFAULT 8,
    expect_status TEXT NOT NULL DEFAULT '200-399',
    expect_body TEXT NOT NULL DEFAULT '',
    max_latency INTEGER NOT NULL DEFAULT 0,
    interval_minutes INTEGER NOT NULL DEFAULT 30,
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_default INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    last_run_at TEXT
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    group_id INTEGER NOT NULL,
    batch_id INTEGER,
    ok INTEGER NOT NULL,
    latency_ms INTEGER,
    error_type TEXT,
    message TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_node_group ON results (node_id, group_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_results_batch ON results (batch_id);

CREATE TABLE IF NOT EXISTS latency_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    batch_id INTEGER,
    ok INTEGER NOT NULL,
    latency_ms INTEGER,
    error_type TEXT,
    message TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_latency_node ON latency_results (node_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_latency_batch ON latency_results (batch_id);

CREATE TABLE IF NOT EXISTS batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    target_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    groups TEXT NOT NULL DEFAULT '[]',
    note TEXT,
    published INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    source TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    detail TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        _local.conn = conn
    return conn


@contextmanager
def transaction():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def reset_connection() -> None:
    """Drop the thread-local handle (needed before/after replacing the database file)."""
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None


def init_db() -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    # Migration: first upgrade to the latency-probe model gets a 500 ms publish ceiling
    if get("latency_test_url") is None:
        set_setting("sub_max_latency", config.DEFAULT_SETTINGS["sub_max_latency"])
    for key, value in config.DEFAULT_SETTINGS.items():
        if key == "sub_token":
            continue
        if get(key) is None:
            set_setting(key, value)
    import secrets

    if not get("sub_token"):
        set_setting("sub_token", secrets.token_urlsafe(24))
    # Seed default test groups only on a fresh database
    cur = conn.execute("SELECT COUNT(*) AS c FROM test_groups")
    if cur.fetchone()["c"] == 0:
        for g in config.DEFAULT_GROUPS:
            conn.execute(
                "INSERT INTO test_groups (name, ident, url, method, timeout, expect_status,"
                " expect_body, max_latency, interval_minutes, enabled, sort_order, is_default, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    g["name"], g["ident"], g["url"], "GET", 8, g["expect_status"], "", 0, 30, 1,
                    g["sort_order"], 1, now(),
                ),
            )
        conn.commit()
    # Anything left mid-flight from a previous run must not be treated as live
    conn.execute("UPDATE batches SET status='aborted', finished_at=? WHERE status='running'", (now(),))
    conn.execute("UPDATE tasks SET status='aborted', finished_at=? WHERE status='running'", (now(),))
    conn.commit()


def get(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get(key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def get_float(key: str, default: float = 0.0) -> float:
    try:
        return float(get(key, str(default)) or default)
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value: Any) -> None:
    conn = connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def rows(sql: str, params: Iterable = ()) -> list[dict]:
    conn = connect()
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def row(sql: str, params: Iterable = ()) -> dict | None:
    conn = connect()
    r = conn.execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def execute(sql: str, params: Iterable = ()) -> int:
    conn = connect()
    cur = conn.execute(sql, tuple(params))
    conn.commit()
    return cur.lastrowid


def append_log(level: str, source: str, message: str) -> None:
    try:
        conn = connect()
        conn.execute(
            "INSERT INTO logs (ts, level, source, message) VALUES (?,?,?,?)",
            (now(), level.upper(), source, message[:2000]),
        )
        conn.commit()
        keep = get_int("history_days", 30)
        conn.execute(
            "DELETE FROM logs WHERE ts < datetime('now', ?)", (f"-{max(keep, 7)} days",)
        )
        conn.commit()
    except Exception:  # logging must never break the caller
        pass


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default
