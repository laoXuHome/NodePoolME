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
    traffic_upload INTEGER NOT NULL DEFAULT 0,
    traffic_download INTEGER NOT NULL DEFAULT 0,
    traffic_total INTEGER NOT NULL DEFAULT 0,
    traffic_expire INTEGER NOT NULL DEFAULT 0,
    traffic_updated_at TEXT,
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

CREATE TABLE IF NOT EXISTS airport_filtered_nodes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    airport_id INTEGER NOT NULL,
    raw_name TEXT NOT NULL,
    protocol TEXT NOT NULL,
    server TEXT NOT NULL,
    port INTEGER NOT NULL,
    fingerprint TEXT NOT NULL,
    params TEXT NOT NULL,
    region TEXT,
    keyword TEXT NOT NULL DEFAULT '',
    admitted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (airport_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_filtered_airport ON airport_filtered_nodes (airport_id);

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
    target TEXT NOT NULL DEFAULT 'G',
    ok INTEGER NOT NULL,
    latency_ms INTEGER,
    error_type TEXT,
    message TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_latency_node ON latency_results (node_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_latency_batch ON latency_results (batch_id);

CREATE TABLE IF NOT EXISTS share_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    batch_id INTEGER,
    reason TEXT NOT NULL DEFAULT 'publish',
    airport_id INTEGER NOT NULL,
    pool_count INTEGER NOT NULL DEFAULT 0,
    published_count INTEGER NOT NULL DEFAULT 0,
    available_count INTEGER NOT NULL DEFAULT 0,
    total_nodes INTEGER NOT NULL DEFAULT 0,
    total_published INTEGER NOT NULL DEFAULT 0,
    total_available INTEGER NOT NULL DEFAULT 0,
    available_known INTEGER NOT NULL DEFAULT 1,
    UNIQUE (ts, airport_id)
);

CREATE TABLE IF NOT EXISTS schedule_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    changed_at TEXT NOT NULL,
    interval_minutes INTEGER NOT NULL,
    source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_share_history_ts ON share_history(ts);

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

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    batch_id INTEGER,
    note TEXT
);

CREATE TABLE IF NOT EXISTS speed_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    region TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL UNIQUE,
    priority INTEGER NOT NULL DEFAULT 1,      -- 0 = 主测速源, 1 = 备用
    enabled INTEGER NOT NULL DEFAULT 1,
    health TEXT NOT NULL DEFAULT 'unknown',    -- unknown / ok / fail
    range_ok INTEGER NOT NULL DEFAULT 0,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER,
    last_error TEXT,
    last_check_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_speed_sources_priority ON speed_sources (priority, enabled, health);

CREATE TABLE IF NOT EXISTS speed_source_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    ok INTEGER NOT NULL,
    dns_ms INTEGER,
    connect_ms INTEGER,
    tls_ms INTEGER,
    status_code INTEGER,
    range_ok INTEGER NOT NULL DEFAULT 0,
    content_length INTEGER NOT NULL DEFAULT 0,
    bytes INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_speed_check_source ON speed_source_checks (source_id, id DESC);

CREATE TABLE IF NOT EXISTS speed_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',   -- ok / partial / failed / source_unavailable / node_error / engine_error / canceled
    confidence TEXT NOT NULL DEFAULT 'none',  -- normal / low / none
    multi_mbps REAL,                          -- 多连接合并速率，Mbit/s（保留兼容）
    single_mbps REAL,                         -- 单连接中位速率，Mbit/s（保留兼容）
    mb_per_s REAL,                            -- 下载速度，MB/s（评级与展示口径）
    single_mb_per_s REAL,                     -- 单连接中位速度，MB/s
    bytes_total INTEGER NOT NULL DEFAULT 0,   -- 本轮实际下载字节数
    download_seconds REAL,                    -- 有效下载采样耗时
    connect_ms INTEGER,                       -- 有效测速源连接耗时中位数
    airport_name TEXT,
    trigger TEXT,                             -- schedule / manual / all / new
    grade TEXT,                               -- excellent / good / medium / poor / danger
    traffic_limit_bytes INTEGER NOT NULL DEFAULT 0,
    sources_total INTEGER NOT NULL DEFAULT 0,
    sources_ok INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    message TEXT
);
CREATE INDEX IF NOT EXISTS idx_speed_runs_node ON speed_runs (node_id, id DESC);

CREATE TABLE IF NOT EXISTS speed_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    node_id INTEGER NOT NULL,
    source_id INTEGER NOT NULL,
    source_name TEXT NOT NULL DEFAULT '',
    source_region TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL,
    dns_ms INTEGER,
    connect_ms INTEGER,
    tls_ms INTEGER,
    ttfb_ms INTEGER,
    bytes INTEGER NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    single_mbps REAL,
    multi_mbps REAL,
    status_code INTEGER,
    range_ok INTEGER NOT NULL DEFAULT 0,
    ok INTEGER NOT NULL DEFAULT 0,
    error_type TEXT,
    message TEXT,
    confidence TEXT NOT NULL DEFAULT 'none'
);
CREATE INDEX IF NOT EXISTS idx_speed_samples_run ON speed_samples (run_id);
CREATE INDEX IF NOT EXISTS idx_speed_samples_node ON speed_samples (node_id, id DESC);
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
    # Migration: per-airport node-name filter keywords (JSON array)
    airport_cols = {r["name"] for r in conn.execute("PRAGMA table_info(airports)").fetchall()}
    if "filter_keywords" not in airport_cols:
        conn.execute("ALTER TABLE airports ADD COLUMN filter_keywords TEXT NOT NULL DEFAULT '[]'")
        conn.commit()
    # Migration: subscription traffic/expiry metadata (standard Subscription-Userinfo header)
    for column, ddl in (
        ("traffic_upload", "INTEGER NOT NULL DEFAULT 0"),
        ("traffic_download", "INTEGER NOT NULL DEFAULT 0"),
        ("traffic_total", "INTEGER NOT NULL DEFAULT 0"),
        ("traffic_expire", "INTEGER NOT NULL DEFAULT 0"),
        ("traffic_updated_at", "TEXT"),
        ("speed_excluded", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in airport_cols:
            conn.execute(f"ALTER TABLE airports ADD COLUMN {column} {ddl}")
            conn.commit()
    # Migration: v1.7 speed runs keep MB/s, byte totals, trigger and grade.
    speed_cols = {r["name"] for r in conn.execute("PRAGMA table_info(speed_runs)").fetchall()}
    for column, ddl in (
        ("mb_per_s", "REAL"),
        ("single_mb_per_s", "REAL"),
        ("bytes_total", "INTEGER NOT NULL DEFAULT 0"),
        ("download_seconds", "REAL"),
        ("connect_ms", "INTEGER"),
        ("airport_name", "TEXT"),
        ("trigger", "TEXT"),
        ("grade", "TEXT"),
        ("traffic_limit_bytes", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in speed_cols:
            conn.execute(f"ALTER TABLE speed_runs ADD COLUMN {column} {ddl}")
            conn.commit()
    # Migration: first upgrade to the latency-probe model gets a 500 ms publish ceiling
    if get("latency_test_url") is None:
        set_setting("sub_max_latency", config.DEFAULT_SETTINGS["sub_max_latency"])
    for key, value in config.DEFAULT_SETTINGS.items():
        if key == "sub_token":
            continue
        if get(key) is None:
            set_setting(key, value)
    # Migration: v1.7 speed defaults changed from 12s/10s to 10s/8s. Only touch
    # values that are still exactly the old defaults, so custom settings survive.
    if get("speed_v17_defaults") != "1":
        if get("speed_sample_seconds") in ("12", "12.0"):
            set_setting("speed_sample_seconds", "10")
        if get("speed_connect_timeout") in ("10", "10.0"):
            set_setting("speed_connect_timeout", "8")
        set_setting("speed_v17_defaults", "1")
    import secrets

    if not get("sub_token"):
        set_setting("sub_token", secrets.token_urlsafe(24))
    # Migration: old rows may store Python bool repr ("True"/"False") instead of 1/0
    value = get("sub_publish_enabled")
    if value in ("True", "False"):
        set_setting("sub_publish_enabled", "1" if value == "True" else "0")
    # Migration: dual-target latency gate (Google + Wikipedia)
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(latency_results)").fetchall()}
    if "target" not in cols:
        conn.execute("ALTER TABLE latency_results ADD COLUMN target TEXT NOT NULL DEFAULT 'G'")
    conn.commit()
    if get("latency_dual_enabled") != "1":
        # Upgrade defaults once; never overwrite user-customized ceilings on later starts.
        if not get("latency_test_url2"):
            set_setting("latency_test_url2", config.DEFAULT_SETTINGS["latency_test_url2"])
        if get("sub_max_latency2") is None:
            set_setting("sub_max_latency2", config.DEFAULT_SETTINGS["sub_max_latency2"])
        for key in ("latency_test_name", "latency_test_name2"):
            if not get(key):
                set_setting(key, config.DEFAULT_SETTINGS[key])
        set_setting("latency_dual_enabled", "1")
        append_log("INFO", "设置", "已启用双延迟目标（默认 Google + Wikipedia）")
    for key in ("latency_test_name", "latency_test_name2"):
        if not get(key):
            set_setting(key, config.DEFAULT_SETTINGS[key])
    # Migration: "degraded" is no longer a state; status is just available/unavailable
    conn.execute("UPDATE nodes SET status='available' WHERE status='degraded'")
    conn.commit()
    # Migration: OVH's proof endpoints now 301 to regional speedtest hosts.
    for old, new in (
        ("https://sgp.proof.ovh.net/files/100Mb.dat",
         "https://sin1-sgp.speedtest.network.ovh.net/files/100Mb.dat"),
        ("https://gra.proof.ovh.net/files/100Mb.dat",
         "https://lil2-gra.speedtest.network.ovh.net/files/100Mb.dat"),
    ):
        conn.execute("UPDATE speed_sources SET url=?, health='unknown', range_ok=0, "
                     "last_error=NULL, updated_at=? WHERE url=?", (new, now(), old))
    conn.commit()
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
    # Seed public download-speed sources once. Health is unknown until the first check.
    cur = conn.execute("SELECT COUNT(*) AS c FROM speed_sources")
    if cur.fetchone()["c"] == 0:
        for src in config.DEFAULT_SPEED_SOURCES:
            conn.execute(
                "INSERT INTO speed_sources (name, region, url, priority, enabled, health,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (src["name"], src["region"], src["url"], src["priority"], 1, "unknown",
                 now(), now()),
            )
        conn.commit()
    # Migration: available-node share history (older rows get the current availability once)
    share_cols = {r["name"] for r in conn.execute("PRAGMA table_info(share_history)").fetchall()}
    availability_added = False
    for column, ddl in (
        ("available_count", "INTEGER NOT NULL DEFAULT 0"),
        ("total_available", "INTEGER NOT NULL DEFAULT 0"),
        ("available_known", "INTEGER NOT NULL DEFAULT 1"),
    ):
        if column not in share_cols:
            conn.execute(f"ALTER TABLE share_history ADD COLUMN {column} {ddl}")
            if column != "available_known":
                availability_added = True
    conn.commit()
    if availability_added:
        rows = conn.execute(
            """SELECT n.airport_id, COUNT(*) AS c FROM nodes n
               JOIN airports a ON a.id=n.airport_id AND a.enabled=1
               WHERE n.status='available' GROUP BY n.airport_id"""
        ).fetchall()
        counts = {row["airport_id"]: row["c"] for row in rows}
        total = sum(counts.values())
        conn.execute("UPDATE share_history SET available_count=0, total_available=?", (total,))
        for airport_id, count in counts.items():
            conn.execute(
                "UPDATE share_history SET available_count=? WHERE airport_id=?",
                (count, airport_id),
            )
        conn.commit()
    # Backfilled availability was copied from the current pool, not measured at that time.
    conn.execute("UPDATE share_history SET available_known=0 WHERE reason='backfill'")
    conn.commit()
    # Anything left mid-flight from a previous run must not be treated as live
    conn.execute("UPDATE batches SET status='aborted', finished_at=? WHERE status='running'", (now(),))
    conn.commit()


def get(key: str, default: str | None = None) -> str | None:
    row = connect().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def get_int(key: str, default: int = 0) -> int:
    try:
        return int(float(get(key, str(default)) or default))
    except (TypeError, ValueError):
        return default


def set_setting(key: str, value: Any) -> None:
    # Booleans must land as "1"/"0": several settings are compared against "1" as strings
    if isinstance(value, bool):
        value = "1" if value else "0"
    conn = connect()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()


def bootstrap_schedule_plan(interval_minutes: int) -> None:
    """首次启动且没有任何计划记录时补基线。

    有历史快照就从最早那份快照起算（不是伪造：只是把"有数据以来按当前间隔检测"
    当作推定计划，调用方会给这类记录打上参考标记）；否则从此刻起算。
    """
    earliest = None
    try:
        earliest = (row("SELECT MIN(ts) AS ts FROM share_history") or {}).get("ts")
    except Exception:
        earliest = None
    existing = rows("SELECT id, changed_at, source FROM schedule_history ORDER BY id")
    if existing:
        # v1.5.4 首次升级时只写了一条启动记录，可把它回推到首份快照并标为推定
        if len(existing) == 1 and (existing[0]["source"] or "") == "boot" and earliest \
                and (existing[0]["changed_at"] or "") > earliest:
            execute("UPDATE schedule_history SET changed_at=?, source='baseline' WHERE id=?",
                    (earliest, existing[0]["id"]))
        return
    stamp = earliest or now()
    execute(
        "INSERT INTO schedule_history (changed_at, interval_minutes, source) VALUES (?,?,?)",
        (stamp, int(interval_minutes), "baseline"),
    )


def record_schedule_plan(interval_minutes: int, source: str = "") -> None:
    """记录检测计划变更；间隔没变就什么也不做，历史不会被当前配置覆盖。"""
    interval = int(interval_minutes)
    last = row("SELECT interval_minutes FROM schedule_history ORDER BY id DESC LIMIT 1")
    if last and int(last["interval_minutes"]) == interval:
        return
    execute(
        "INSERT INTO schedule_history (changed_at, interval_minutes, source) VALUES (?,?,?)",
        (now(), interval, source),
    )


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


def record_run_event(kind: str, batch_id: int | None = None, note: str | None = None) -> None:
    """记录暂停/恢复/停止/启动/停服，供每日准入判断排除区间。"""
    execute("INSERT INTO run_events (ts, kind, batch_id, note) VALUES (?,?,?,?)",
            (now(), str(kind), batch_id, note))


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
