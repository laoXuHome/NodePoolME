"""P5 competition storage: independent per-category state and audit history.

P5 never writes a P1 node row, never changes P2/P3 qualification and never
recomputes a P4 quality score.  It only keeps six independent competition
ledgers: one standard ledger and five business ledgers.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from backend.database import db
from backend.nodepool import store as p1_store

CATEGORY_DEFS = (
    ("standard", "标准综合", "标准综合优选", "STANDARD"),
    ("gpt", "GPT", "GPT 专项优选", "GPT"),
    ("youtube", "YouTube", "YouTube 专项优选", "YOUTUBE"),
    ("telegram", "Telegram", "Telegram 专项优选", "TELEGRAM"),
    ("github", "GitHub", "GitHub 专项优选", "GITHUB"),
    ("global", "国外综合", "国外综合专项优选", "GLOBAL"),
)
CATEGORIES = tuple(item[0] for item in CATEGORY_DEFS)
CATEGORY_LABELS = {item[0]: item[1] for item in CATEGORY_DEFS}
CATEGORY_TITLES = {item[0]: item[2] for item in CATEGORY_DEFS}
CATEGORY_CODES = {item[0]: item[3] for item in CATEGORY_DEFS}
STATES = ("observation", "candidate", "preferred", "eliminated")
STATE_LABELS = {"observation": "观察", "candidate": "候选", "preferred": "优选", "eliminated": "淘汰"}
STATE_RANK = {"preferred": 0, "candidate": 1, "observation": 2, "eliminated": 3}

DEFAULTS = {
    "observation_days": 3,
    "candidate_score": 80,
    "retain_score": 70,
    "failure_limit": 3,
    "recovery_successes": 2,
    "replace_gap": 8,
    "cycle_minutes": 60,
}
FIELDS = (
    {"key": "observation_days", "label": "新节点观察期", "default": 3, "min": 1, "max": 7,
     "unit": "天", "description": "从节点首次进入 P1 起计算；观察期结束且数据成熟才可进入候选。"},
    {"key": "candidate_score", "label": "候选晋级分数", "default": 80, "min": 70, "max": 95,
     "unit": "分", "description": "候选节点连续有效评价达到该分数，才允许进入优选。"},
    {"key": "retain_score", "label": "优选保留分数", "default": 70, "min": 60, "max": 85,
     "unit": "分", "description": "成熟优选单次低于该分数才计一次不达标，不立即降级。"},
    {"key": "failure_limit", "label": "连续不达标次数", "default": 3, "min": 2, "max": 5,
     "unit": "次", "description": "连续有效评价低于保留线达到该次数，才降级回观察。"},
    {"key": "recovery_successes", "label": "恢复连续成功次数", "default": 2, "min": 2, "max": 5,
     "unit": "次", "description": "恢复节点连续有效成功达到该次数，才可重新进入候选。"},
    {"key": "replace_gap", "label": "替换最低分差", "default": 8, "min": 5, "max": 15,
     "unit": "分", "description": "新节点必须领先被替换成熟优选至少该分差，并连续两次确认。"},
    {"key": "cycle_minutes", "label": "自动竞争周期", "default": 60, "min": 10, "max": 1440,
     "unit": "分钟", "description": "自动调度检查周期；手动触发不受该周期限制。"},
)

_DDL = (
    """CREATE TABLE IF NOT EXISTS np5_state (
        node_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'observation',
        score REAL,
        maturity REAL,
        p80_ms INTEGER,
        delay_grade TEXT NOT NULL DEFAULT 'none',
        speed_grade TEXT NOT NULL DEFAULT 'empty',
        speed_mb_s REAL,
        admission_ok INTEGER NOT NULL DEFAULT 0,
        admission_reason TEXT,
        admission_json TEXT,
        observation_started_at TEXT,
        observation_until TEXT,
        candidate_since TEXT,
        preferred_since TEXT,
        eliminated_since TEXT,
        last_evaluated_at TEXT,
        last_change_at TEXT,
        last_evidence_key TEXT,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        consecutive_successes INTEGER NOT NULL DEFAULT 0,
        advantage_count INTEGER NOT NULL DEFAULT 0,
        advantage_target_id INTEGER,
        recovery_required INTEGER NOT NULL DEFAULT 0,
        last_reason TEXT,
        config_version INTEGER NOT NULL DEFAULT 1,
        detail_json TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (node_id, category)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np5_state_category ON np5_state (category, state, score)",
    "CREATE INDEX IF NOT EXISTS idx_np5_state_node ON np5_state (node_id)",
    """CREATE TABLE IF NOT EXISTS np5_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        node_id INTEGER NOT NULL,
        category TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        before_score REAL,
        after_score REAL,
        admission_json TEXT,
        reason TEXT,
        replaced_node_id INTEGER,
        replace_reason TEXT,
        config_version INTEGER NOT NULL DEFAULT 1,
        evidence_key TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np5_history_node ON np5_history (node_id, category, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_np5_history_category ON np5_history (category, id DESC)",
    """CREATE TABLE IF NOT EXISTS np5_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        trigger TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running',
        categories TEXT,
        nodes_scanned INTEGER NOT NULL DEFAULT 0,
        changes INTEGER NOT NULL DEFAULT 0,
        message TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np5_runs_at ON np5_runs (id DESC)",
    """CREATE TABLE IF NOT EXISTS np5_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        node_id INTEGER,
        category TEXT,
        reason TEXT,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np5_events_at ON np5_events (id DESC)",
)


class CompetitionError(Exception):
    """Stable machine-readable P5 error."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    if conn is not None:
        p1_store.ensure_schema(conn)
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


def _raw_setting(conn: sqlite3.Connection | None, key: str, default: str) -> str:
    if conn is None:
        row = db.row("SELECT value FROM settings WHERE key=?", (key,))
    else:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row is not None else default


def _setting_int(conn: sqlite3.Connection | None, key: str, default: int) -> int:
    try:
        return int(float(_raw_setting(conn, key, str(default))))
    except (TypeError, ValueError):
        return default


def config_version(conn: sqlite3.Connection | None = None) -> int:
    return _setting_int(conn, "p5_config_version", 1)


def ensure_defaults(conn: sqlite3.Connection) -> None:
    ensure_schema(conn)
    for key, value in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES (?,?)",
                     (f"p5_{key}", str(value)))
    conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES ('p5_config_version','1')")


def get_settings(conn: sqlite3.Connection | None = None) -> dict:
    return {key: _setting_int(conn, f"p5_{key}", default)
            for key, default in DEFAULTS.items()} | {
        "config_version": config_version(conn)
    }


def settings_payload(conn: sqlite3.Connection | None = None) -> dict:
    current = get_settings(conn)
    return {"current": current, "fields": list(FIELDS),
            "defaults": dict(DEFAULTS),
            "category_titles": CATEGORY_TITLES,
            "stages": [
                {"key": "observation", "label": "观察", "description": "新节点或等待恢复；数据不成熟不按零分。"},
                {"key": "candidate", "label": "候选", "description": "满足准入，正在积累晋级确认。"},
                {"key": "preferred", "label": "优选", "description": "正式进入对应类别优选名单。"},
                {"key": "eliminated", "label": "淘汰", "description": "退出当前类别，不删除 P1 原始节点。"},
            ],
            "entrance": {
                "quality_score": 75, "maturity": 70, "tcp_p80_ms": 500,
                "stability": "无严重故障日且最长连续失败 < 3",
                "speed_danger": "禁止", "speed_empty": "允许但标记无数据",
            }}


def validate_settings(data: dict, current: dict | None = None) -> dict:
    base = dict(current or get_settings())
    out = dict(base)
    for field in FIELDS:
        key = field["key"]
        if key not in data or data.get(key) in (None, ""):
            continue
        try:
            value = int(float(data[key]))
        except (TypeError, ValueError):
            raise CompetitionError("invalid_setting", f"{field['label']}必须是整数")
        if not (int(field["min"]) <= value <= int(field["max"])):
            raise CompetitionError(
                "invalid_setting",
                f"{field['label']}必须在 {field['min']}–{field['max']}{field['unit']}",
            )
        out[key] = value
    if out["retain_score"] > out["candidate_score"]:
        raise CompetitionError("invalid_setting", "优选保留分数不能高于候选晋级分数")
    if out["recovery_successes"] > 5 or out["failure_limit"] > 5:
        raise CompetitionError("invalid_setting", "连续次数超出允许范围")
    return out


def update_settings(data: dict, conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return update_settings(data, conn=own_conn)
    ensure_defaults(conn)
    merged = validate_settings(data, get_settings(conn))
    for field in FIELDS:
        key = field["key"]
        conn.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"p5_{key}", str(merged[key])))
    version = config_version(conn) + 1
    conn.execute("INSERT INTO settings(key,value) VALUES ('p5_config_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
    record_event(conn, "settings_changed", reason="P5 竞争参数更新",
                 detail={"config_version": version})
    return settings_payload(conn)


def restore_defaults(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    if own:
        with db.transaction() as own_conn:
            return restore_defaults(conn=own_conn)
    ensure_defaults(conn)
    for key, value in DEFAULTS.items():
        conn.execute("INSERT INTO settings(key,value) VALUES (?,?) "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (f"p5_{key}", str(value)))
    version = config_version(conn) + 1
    conn.execute("INSERT INTO settings(key,value) VALUES ('p5_config_version',?) "
                 "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(version),))
    record_event(conn, "settings_restored", reason="P5 竞争参数恢复默认",
                 detail={"config_version": version})
    return settings_payload(conn)


def ensure_states(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    """Create the six observation ledgers for every currently active P1 node."""
    ensure_schema(conn)
    ensure_defaults(conn)
    cfg = get_settings(conn)
    at = now or datetime.now()
    before = int(conn.execute("SELECT COUNT(*) FROM np5_state").fetchone()[0])
    nodes = conn.execute(
        """SELECT n.id, n.first_seen_at FROM np_nodes n
           LEFT JOIN airports a ON a.id=n.airport_id"""
    ).fetchall()
    days = int(cfg["observation_days"])
    for node in nodes:
        start = _parse(node["first_seen_at"]) or at
        until = start + timedelta(days=days)
        for category in CATEGORIES:
            conn.execute(
                """INSERT OR IGNORE INTO np5_state
                   (node_id,category,state,observation_started_at,observation_until,
                    last_change_at,updated_at,config_version)
                   VALUES (?,?, 'observation',?,?,?,?,?)""",
                (int(node["id"]), category, _stamp(start), _stamp(until),
                 _stamp(at), _stamp(at), int(cfg["config_version"])),
            )
    return int(conn.execute("SELECT COUNT(*) FROM np5_state").fetchone()[0]) - before


def get_state(node_id: int, category: str, *, conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        conn = _read_conn(seed=True)
    row = conn.execute("SELECT * FROM np5_state WHERE node_id=? AND category=?",
                       (int(node_id), category)).fetchone()
    if row is None:
        return None
    item = dict(row)
    item["admission"] = db.loads(item.pop("admission_json"), {}) or {}
    item["detail"] = db.loads(item.pop("detail_json"), {}) or {}
    item["admission_ok"] = bool(item.get("admission_ok"))
    item["recovery_required"] = bool(item.get("recovery_required"))
    return item


def states_for_node(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict[str, dict]:
    if conn is None:
        conn = _read_conn(seed=True)
    out = {}
    for row in conn.execute("SELECT * FROM np5_state WHERE node_id=?", (int(node_id),)).fetchall():
        item = dict(row)
        item["admission"] = db.loads(item.pop("admission_json"), {}) or {}
        item["detail"] = db.loads(item.pop("detail_json"), {}) or {}
        item["admission_ok"] = bool(item.get("admission_ok"))
        item["recovery_required"] = bool(item.get("recovery_required"))
        out[item["category"]] = item
    return out


def save_state(conn: sqlite3.Connection, state: dict) -> None:
    ensure_schema(conn)
    conn.execute(
        """INSERT INTO np5_state
           (node_id,category,state,score,maturity,p80_ms,delay_grade,speed_grade,
            speed_mb_s,admission_ok,admission_reason,admission_json,observation_started_at,
            observation_until,candidate_since,preferred_since,eliminated_since,
            last_evaluated_at,last_change_at,last_evidence_key,consecutive_failures,
            consecutive_successes,advantage_count,advantage_target_id,recovery_required,
            last_reason,config_version,detail_json,updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(node_id,category) DO UPDATE SET
             state=excluded.state, score=excluded.score, maturity=excluded.maturity,
             p80_ms=excluded.p80_ms, delay_grade=excluded.delay_grade,
             speed_grade=excluded.speed_grade, speed_mb_s=excluded.speed_mb_s,
             admission_ok=excluded.admission_ok, admission_reason=excluded.admission_reason,
             admission_json=excluded.admission_json,
             observation_started_at=excluded.observation_started_at,
             observation_until=excluded.observation_until,
             candidate_since=excluded.candidate_since,
             preferred_since=excluded.preferred_since,
             eliminated_since=excluded.eliminated_since,
             last_evaluated_at=excluded.last_evaluated_at,
             last_change_at=COALESCE(excluded.last_change_at,np5_state.last_change_at),
             last_evidence_key=excluded.last_evidence_key,
             consecutive_failures=excluded.consecutive_failures,
             consecutive_successes=excluded.consecutive_successes,
             advantage_count=excluded.advantage_count,
             advantage_target_id=excluded.advantage_target_id,
             recovery_required=excluded.recovery_required,
             last_reason=excluded.last_reason,
             config_version=excluded.config_version,
             detail_json=excluded.detail_json, updated_at=excluded.updated_at""",
        (int(state["node_id"]), state["category"], state["state"], state.get("score"),
         state.get("maturity"), state.get("p80_ms"), state.get("delay_grade") or "none",
         state.get("speed_grade") or "empty", state.get("speed_mb_s"),
         1 if state.get("admission_ok") else 0, state.get("admission_reason"),
         _json(state.get("admission") or {}), state.get("observation_started_at"),
         state.get("observation_until"), state.get("candidate_since"),
         state.get("preferred_since"), state.get("eliminated_since"),
         state.get("last_evaluated_at"), state.get("last_change_at"),
         state.get("last_evidence_key"), int(state.get("consecutive_failures") or 0),
         int(state.get("consecutive_successes") or 0),
         int(state.get("advantage_count") or 0), state.get("advantage_target_id"),
         1 if state.get("recovery_required") else 0,
         state.get("last_reason"), int(state.get("config_version") or 1),
         _json(state.get("detail") or {}), state.get("updated_at") or _stamp()),
    )


def record_change(conn: sqlite3.Connection, *, at: str, node_id: int, category: str,
                  from_state: str | None, to_state: str, before_score: float | None,
                  after_score: float | None, admission: dict | None = None,
                  reason: str | None = None, replaced_node_id: int | None = None,
                  replace_reason: str | None = None, config_version: int = 1,
                  evidence_key: str | None = None, detail: Any = None) -> int:
    cur = conn.execute(
        """INSERT INTO np5_history(at,node_id,category,from_state,to_state,before_score,
           after_score,admission_json,reason,replaced_node_id,replace_reason,
           config_version,evidence_key,detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (at, int(node_id), category, from_state, to_state, before_score, after_score,
         _json(admission) if admission is not None else None, reason, replaced_node_id,
         replace_reason, int(config_version), evidence_key,
         _json(detail) if detail is not None else None),
    )
    return int(cur.lastrowid)


def record_event(conn: sqlite3.Connection, event: str, *, node_id: int | None = None,
                 category: str | None = None, reason: str | None = None,
                 detail: Any = None) -> None:
    conn.execute(
        "INSERT INTO np5_events(at,event,node_id,category,reason,detail_json) VALUES (?,?,?,?,?,?)",
        (_stamp(), event, node_id, category, reason,
         _json(detail) if detail is not None else None),
    )


def start_run(conn: sqlite3.Connection, *, trigger: str, categories: list[str],
              now: datetime | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO np5_runs(started_at,trigger,status,categories)
           VALUES (?,?, 'running',?)""",
        (_stamp(now), trigger, _json(categories)),
    )
    return int(cur.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, *, status: str, nodes_scanned: int,
               changes: int, message: str | None = None, detail: Any = None,
               now: datetime | None = None) -> None:
    conn.execute(
        """UPDATE np5_runs SET finished_at=?,status=?,nodes_scanned=?,changes=?,message=?,
           detail_json=? WHERE id=?""",
        (_stamp(now), status, int(nodes_scanned), int(changes), message,
         _json(detail) if detail is not None else None, int(run_id)),
    )


def last_run(conn: sqlite3.Connection | None = None) -> dict | None:
    if conn is None:
        conn = _read_conn()
    row = conn.execute("SELECT * FROM np5_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    item = dict(row)
    item["categories"] = db.loads(item.get("categories"), []) or []
    item["detail"] = db.loads(item.get("detail_json"), {}) or {}
    return item


def history(node_id: int | None = None, *, category: str | None = None, limit: int = 60,
            conn: sqlite3.Connection | None = None) -> list[dict]:
    if conn is None:
        conn = _read_conn(seed=True)
    sql = "SELECT * FROM np5_history"
    args: list[Any] = []
    where = []
    if node_id is not None:
        where.append("node_id=?")
        args.append(int(node_id))
    if category:
        where.append("category=?")
        args.append(category)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(int(limit), 500)))
    items = []
    for row in conn.execute(sql, args).fetchall():
        item = dict(row)
        item["admission"] = db.loads(item.pop("admission_json"), {}) or {}
        item["detail"] = db.loads(item.pop("detail_json"), {}) or {}
        item["category_label"] = CATEGORY_LABELS.get(item["category"], item["category"])
        items.append(item)
    return items


def node_inventory(*, conn: sqlite3.Connection | None = None) -> list[dict]:
    """Only P1's active inventory; P5 never writes back to this table."""
    if conn is None:
        conn = _read_conn()
    rows = conn.execute(
        """SELECT n.id,n.current_name,n.first_name,n.server,n.port,n.region,n.airport_id,
                  n.management_layer,n.base_health,n.data_status,n.source_active,
                  n.first_seen_at,n.last_seen_at,a.name AS airport_name,a.enabled AS airport_enabled
           FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id
           ORDER BY a.name,n.current_name,n.id"""
    ).fetchall()
    return [dict(row) for row in rows]


def state_rows(*, category: str, state: str | None = None, q: str | None = None,
               page: int = 1, per_page: int = 20, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    rows = conn.execute(
        """SELECT s.*,n.current_name,n.first_name,n.server,n.port,n.region,
                  n.airport_id,n.management_layer,n.base_health,n.data_status,
                  n.source_active,a.name AS airport_name
           FROM np5_state s JOIN np_nodes n ON n.id=s.node_id
           LEFT JOIN airports a ON a.id=n.airport_id
           WHERE s.category=?""",
        (category,),
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["admission"] = db.loads(item.pop("admission_json"), {}) or {}
        item["detail"] = db.loads(item.pop("detail_json"), {}) or {}
        item["admission_ok"] = bool(item.get("admission_ok"))
        item["recovery_required"] = bool(item.get("recovery_required"))
        item["category_label"] = CATEGORY_LABELS.get(item["category"], item["category"])
        item["state_label"] = STATE_LABELS.get(item["state"], item["state"])
        item["node_name"] = item.get("current_name") or item.get("first_name") or f"节点 {item['node_id']}"
        item["has_speed_data"] = item.get("speed_grade") not in (None, "", "empty", "excluded")
        if state and item["state"] != state:
            continue
        if q:
            needle = str(q).strip().lower()
            hay = " ".join(str(item.get(k) or "") for k in
                           ("node_name", "server", "airport_name", "last_reason")).lower()
            if needle not in hay:
                continue
        items.append(item)
    items.sort(key=lambda x: (
        STATE_RANK.get(x.get("state"), 9),
        -(x.get("score") if x.get("score") is not None else -1),
        x.get("preferred_since") or "9999",
        x.get("node_name") or "",
    ))
    total = len(items)
    start = (max(1, page) - 1) * max(1, per_page)
    return {"items": items[start:start + max(1, per_page)], "total": total,
            "page": max(1, page), "per_page": max(1, per_page),
            "pages": (total + max(1, per_page) - 1) // max(1, per_page),
            "category": category, "state": state}


def node_detail(node_id: int, *, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    node = conn.execute(
        """SELECT n.*,a.name AS airport_name,a.enabled AS airport_enabled
           FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id
           WHERE n.id=?""", (int(node_id),)
    ).fetchone()
    if node is None:
        raise CompetitionError("node_not_found", "节点不存在")
    item = dict(node)
    item["node_name"] = item.get("current_name") or item.get("first_name") or f"节点 {node_id}"
    states = states_for_node(node_id, conn=conn)
    for category, state in states.items():
        state["category_label"] = CATEGORY_LABELS.get(category, category)
        state["state_label"] = STATE_LABELS.get(state["state"], state["state"])
        state["has_speed_data"] = state.get("speed_grade") not in (None, "", "empty", "excluded")
    return {"node": item, "states": states, "history": history(
        node_id, limit=200, conn=conn)}


def counts(conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        conn = _read_conn(seed=True)
    result = {}
    for category in CATEGORIES:
        by_state = {state: 0 for state in STATES}
        for row in conn.execute(
            "SELECT state,COUNT(*) AS count FROM np5_state WHERE category=? GROUP BY state",
            (category,),
        ).fetchall():
            by_state[row["state"]] = int(row["count"])
        result[category] = by_state
    return result
