"""P1 three-layer node inventory and state management.

P1 deliberately does not run any network probe.  It owns the stable inventory,
the two independent state axes (base health / intelligent competition), the
logical healthy/observation sub-pools and the audit trail.  Detection and
selection rules are supplied by later phases through :func:`set_health`.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from backend.database import db

LAYERS = ("raw", "healthy", "observation")
HEALTH_STATES = ("healthy", "unhealthy", "unknown")
COMPETITION_STATES = ("candidate", "preferred", "observation", "eliminated")
DATA_STATES = ("sufficient", "insufficient", "unknown")

_DDL = (
    """CREATE TABLE IF NOT EXISTS np_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS np_nodes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        airport_id INTEGER NOT NULL,
        identity_key TEXT NOT NULL,
        occurrence INTEGER NOT NULL DEFAULT 1,
        current_name TEXT NOT NULL,
        first_name TEXT NOT NULL,
        protocol TEXT NOT NULL,
        server TEXT NOT NULL,
        port INTEGER NOT NULL,
        params TEXT NOT NULL,
        region TEXT,
        source_name TEXT,
        source_url TEXT,
        source_active INTEGER NOT NULL DEFAULT 1,
        management_layer TEXT NOT NULL DEFAULT 'observation',
        base_health TEXT NOT NULL DEFAULT 'unknown',
        competition_state TEXT NOT NULL DEFAULT 'observation',
        data_status TEXT NOT NULL DEFAULT 'insufficient',
        observation_reason TEXT,
        last_error TEXT,
        last_test_at TEXT,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (airport_id, identity_key, occurrence)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np_nodes_airport ON np_nodes (airport_id)",
    "CREATE INDEX IF NOT EXISTS idx_np_nodes_layer ON np_nodes (management_layer)",
    "CREATE INDEX IF NOT EXISTS idx_np_nodes_health ON np_nodes (base_health)",
    "CREATE INDEX IF NOT EXISTS idx_np_nodes_competition ON np_nodes (competition_state)",
    "CREATE INDEX IF NOT EXISTS idx_np_nodes_active ON np_nodes (source_active)",
    "CREATE INDEX IF NOT EXISTS idx_np_nodes_name ON np_nodes (current_name)",
    """CREATE TABLE IF NOT EXISTS np_node_names (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        seen_count INTEGER NOT NULL DEFAULT 1,
        UNIQUE (node_id, name)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np_names_node ON np_node_names (node_id, id)",
    """CREATE TABLE IF NOT EXISTS np_node_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        from_layer TEXT,
        to_layer TEXT,
        from_health TEXT,
        to_health TEXT,
        from_competition TEXT,
        to_competition TEXT,
        reason TEXT,
        actor TEXT NOT NULL DEFAULT 'system',
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np_events_node ON np_node_events (node_id, id DESC)",
    """CREATE TABLE IF NOT EXISTS np_node_qualifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id INTEGER NOT NULL,
        qualification TEXT NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        granted_at TEXT NOT NULL,
        revoked_at TEXT,
        reason TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np_qual_node ON np_node_qualifications (node_id, active)",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_np_qual_active ON np_node_qualifications (node_id, qualification) WHERE active=1",
    """CREATE TABLE IF NOT EXISTS np_fetch_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        airport_id INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        seen_count INTEGER NOT NULL DEFAULT 0,
        new_count INTEGER NOT NULL DEFAULT 0,
        changed_count INTEGER NOT NULL DEFAULT 0,
        missing_count INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running',
        note TEXT
    )""",
)


class NodePoolError(Exception):
    """Stable, machine-readable management error."""

    def __init__(self, code: str, message: str = ""):
        self.code = code
        super().__init__(f"{code}: {message}" if message else code)


def _now() -> str:
    return db.now()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _params(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def identity_key(protocol: str, server: str, port: int, params: Any) -> str:
    """Stable identity: name is excluded, transport configuration is included.

    A renamed node keeps its key.  A changed credential/endpoint is a new node,
    because it is no longer the same usable configuration.
    """
    p = _params(params)
    keys = ("uuid", "password", "cipher", "network", "path", "host", "sni", "serviceName",
            "security", "flow", "publicKey", "shortId", "obfs", "alterId", "type")
    core = {k: p.get(k) for k in keys if p.get(k) not in (None, "", 0, False)}
    raw = f"{protocol}|{str(server).strip().lower()}|{int(port)}|{_json(core)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


def stable_uid(node: dict) -> str:
    return f"np:{node['airport_id']}:{node['identity_key'][:12]}:{node['occurrence']}"


def _existing_node(conn: sqlite3.Connection, node_id: int) -> dict:
    row = conn.execute("SELECT * FROM np_nodes WHERE id=?", (node_id,)).fetchone()
    if not row:
        raise NodePoolError("node_not_found", f"节点 {node_id} 不存在")
    return dict(row)


def _event(conn: sqlite3.Connection, node_id: int, kind: str, *, before: dict | None = None,
           after: dict | None = None, reason: str | None = None, actor: str = "system",
           detail: Any = None) -> None:
    before = before or {}
    after = after or {}
    conn.execute(
        """INSERT INTO np_node_events
           (node_id, ts, kind, from_layer, to_layer, from_health, to_health,
            from_competition, to_competition, reason, actor, detail_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (node_id, _now(), kind, before.get("management_layer"), after.get("management_layer"),
         before.get("base_health"), after.get("base_health"),
         before.get("competition_state"), after.get("competition_state"),
         reason, actor, _json(detail) if detail is not None else None),
    )


def _touch_name(conn: sqlite3.Connection, node_id: int, name: str, stamp: str,
                is_new_node: bool) -> bool:
    """Record every distinct name.  Returns True when the current name changed."""
    row = conn.execute(
        "SELECT * FROM np_node_names WHERE node_id=? AND name=?", (node_id, name)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE np_node_names SET last_seen_at=?, seen_count=seen_count+1 WHERE id=?",
            (stamp, row["id"]),
        )
        return False
    conn.execute(
        """INSERT INTO np_node_names (node_id,name,first_seen_at,last_seen_at,seen_count)
           VALUES (?,?,?,?,1)""",
        (node_id, name, stamp, stamp),
    )
    return not is_new_node


def _insert_node(conn: sqlite3.Connection, *, airport_id: int, airport_name: str,
                 airport_url: str, node: dict, identity: str, occurrence: int,
                 layer: str, health: str, competition: str, data_status: str,
                 observation_reason: str | None, source_active: int, first_seen: str,
                 last_seen: str, actor: str = "system") -> int:
    stamp = _now()
    name = str(node.get("raw_name") or f"{node.get('server')}:{node.get('port')}")[:160]
    cur = conn.execute(
        """INSERT INTO np_nodes
           (airport_id, identity_key, occurrence, current_name, first_name, protocol, server,
            port, params, region, source_name, source_url, source_active, management_layer,
            base_health, competition_state, data_status, observation_reason,
            first_seen_at, last_seen_at, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (airport_id, identity, occurrence, name, name, node.get("protocol") or "",
         node.get("server") or "", int(node.get("port") or 0),
         json.dumps(node.get("params") or {}, ensure_ascii=False, sort_keys=True),
         node.get("region"), airport_name, airport_url, source_active, layer, health,
         competition, data_status, observation_reason, first_seen, last_seen, stamp, stamp),
    )
    node_id = int(cur.lastrowid)
    _touch_name(conn, node_id, name, stamp, True)
    _event(conn, node_id, "node_ingested" if actor == "system" else "node_backfilled",
           after={"management_layer": layer, "base_health": health,
                  "competition_state": competition, "data_status": data_status},
           reason=observation_reason, actor=actor,
           detail={"airport_id": airport_id, "name": name, "occurrence": occurrence})
    return node_id


def _backfill(conn: sqlite3.Connection) -> None:
    marker = conn.execute("SELECT value FROM np_meta WHERE key='backfilled_v1'").fetchone()
    if marker:
        return
    rows = conn.execute(
        """SELECT n.*, a.name AS airport_name, a.url AS airport_url
           FROM nodes n LEFT JOIN airports a ON a.id=n.airport_id"""
    ).fetchall()
    for row in rows:
        n = dict(row)
        key = identity_key(n["protocol"], n["server"], n["port"], n.get("params"))
        occurrence = int(conn.execute(
            "SELECT COALESCE(MAX(occurrence),0)+1 AS n FROM np_nodes"
            " WHERE airport_id=? AND identity_key=?", (n["airport_id"], key)
        ).fetchone()["n"])
        _insert_node(
            conn, airport_id=n["airport_id"], airport_name=n.get("airport_name") or "",
            airport_url=n.get("airport_url") or "", node=n, identity=key,
            occurrence=occurrence, layer="observation", health="unknown",
            competition="observation", data_status="insufficient",
            observation_reason="backfill", source_active=1,
            first_seen=n.get("created_at") or _now(), last_seen=n.get("updated_at") or _now(),
            actor="backfill",
        )
    filtered = conn.execute(
        """SELECT f.*, a.name AS airport_name, a.url AS airport_url
           FROM airport_filtered_nodes f LEFT JOIN airports a ON a.id=f.airport_id
           WHERE f.admitted=0"""
    ).fetchall()
    for row in filtered:
        n = dict(row)
        key = identity_key(n["protocol"], n["server"], n["port"], n.get("params"))
        occurrence = int(conn.execute(
            "SELECT COALESCE(MAX(occurrence),0)+1 AS n FROM np_nodes"
            " WHERE airport_id=? AND identity_key=?", (n["airport_id"], key)
        ).fetchone()["n"])
        _insert_node(
            conn, airport_id=n["airport_id"], airport_name=n.get("airport_name") or "",
            airport_url=n.get("airport_url") or "", node=n, identity=key,
            occurrence=occurrence, layer="raw", health="unknown",
            competition="observation", data_status="insufficient",
            observation_reason="filtered", source_active=1,
            first_seen=n.get("created_at") or _now(), last_seen=n.get("updated_at") or _now(),
            actor="backfill",
        )
    conn.execute("INSERT OR REPLACE INTO np_meta (key,value) VALUES ('backfilled_v1',?)",
                 (_now(),))


def ensure_schema(conn: sqlite3.Connection | None = None) -> None:
    """Create the side tables lazily and backfill the current cache once."""
    if conn is None:
        with db.transaction() as own:
            ensure_schema(own)
        return
    for ddl in _DDL:
        conn.execute(ddl)
    _backfill(conn)


def _airport(conn: sqlite3.Connection, airport_id: int) -> tuple[str, str]:
    row = conn.execute("SELECT name,url FROM airports WHERE id=?", (airport_id,)).fetchone()
    return (row["name"], row["url"]) if row else ("", "")


def ingest_fetch(conn: sqlite3.Connection, airport_id: int, nodes: Iterable[dict],
                 *, seen_at: str | None = None) -> dict:
    """Ingest every standardized node from one airport fetch.

    The source list is authoritative only for ``source_active``.  A node absent
    from a later fetch is never deleted; it becomes historical and, if it was
    healthy, moves to observation pending confirmation.
    """
    ensure_schema(conn)
    stamp = seen_at or _now()
    airport_name, airport_url = _airport(conn, airport_id)
    batch = conn.execute(
        "INSERT INTO np_fetch_batches (airport_id,started_at,status) VALUES (?,?, 'running')",
        (airport_id, stamp),
    )
    batch_id = int(batch.lastrowid)
    incoming = []
    for raw in nodes:
        if not raw.get("server") or not raw.get("port"):
            continue
        item = dict(raw)
        item["raw_name"] = str(item.get("raw_name") or f"{item['server']}:{item['port']}")[:160]
        key = identity_key(item.get("protocol") or "", item["server"], int(item["port"]),
                           item.get("params") or {})
        incoming.append((item, key))

    matched_ids: set[int] = set()
    new_count = changed_count = 0
    by_key: dict[str, list[dict]] = {}
    for row in conn.execute("SELECT * FROM np_nodes WHERE airport_id=? ORDER BY occurrence,id",
                            (airport_id,)).fetchall():
        by_key.setdefault(row["identity_key"], []).append(dict(row))

    for item, key in incoming:
        existing = by_key.setdefault(key, [])
        unused = [row for row in existing if int(row["id"]) not in matched_ids]
        same_name = next((row for row in unused if row["current_name"] == item["raw_name"]), None)
        row = same_name or (unused[0] if unused else None)
        if row:
            before = dict(row)
            node_id = int(row["id"])
            matched_ids.add(node_id)
            old_name = row["current_name"]
            source_returned = not int(row["source_active"])
            layer = row["management_layer"]
            competition = row["competition_state"]
            observation_reason = row["observation_reason"]
            if source_returned:
                layer = "observation" if layer == "raw" else layer
                competition = "observation" if competition in ("candidate", "preferred") else competition
                observation_reason = "source_returned"
            conn.execute(
                """UPDATE np_nodes SET current_name=?, protocol=?, server=?, port=?, params=?,
                   region=?, source_name=?, source_url=?, source_active=1,
                   management_layer=?, competition_state=?, observation_reason=?, last_seen_at=?,
                   updated_at=? WHERE id=?""",
                (item["raw_name"], item.get("protocol") or "", item["server"], int(item["port"]),
                 json.dumps(item.get("params") or {}, ensure_ascii=False, sort_keys=True),
                 item.get("region"), airport_name, airport_url, layer, competition,
                 observation_reason, stamp, _now(), node_id),
            )
            _touch_name(conn, node_id, item["raw_name"], stamp, False)
            after = _existing_node(conn, node_id)
            if old_name != item["raw_name"]:
                changed_count += 1
                _event(conn, node_id, "name_changed", before=before, after=after,
                       reason="source name changed", detail={"old": old_name, "new": item["raw_name"]})
            if source_returned:
                _event(conn, node_id, "source_returned", before=before, after=after,
                       reason="节点重新出现在订阅中")
            continue
        occurrence = max([int(r["occurrence"]) for r in existing], default=0) + 1
        node_id = _insert_node(
            conn, airport_id=airport_id, airport_name=airport_name, airport_url=airport_url,
            node=item, identity=key, occurrence=occurrence, layer="observation",
            health="unknown", competition="observation", data_status="insufficient",
            observation_reason="new", source_active=1, first_seen=stamp, last_seen=stamp,
        )
        matched_ids.add(node_id)
        existing.append(_existing_node(conn, node_id))
        new_count += 1

    missing_count = 0
    for row in conn.execute(
        "SELECT * FROM np_nodes WHERE airport_id=? AND source_active=1", (airport_id,)
    ).fetchall():
        node = dict(row)
        if int(node["id"]) in matched_ids:
            continue
        before = dict(node)
        layer = node["management_layer"]
        competition = node["competition_state"]
        if layer == "healthy":
            layer = "observation"
        if competition in ("candidate", "preferred"):
            competition = "observation"
        conn.execute(
            """UPDATE np_nodes SET source_active=0, management_layer=?, competition_state=?,
               observation_reason=?, updated_at=? WHERE id=?""",
            (layer, competition, "source_missing", _now(), node["id"]),
        )
        after = _existing_node(conn, node["id"])
        _event(conn, node["id"], "source_missing", before=before, after=after,
               reason="节点已从来源订阅消失，保留历史")
        missing_count += 1

    conn.execute(
        """UPDATE np_fetch_batches SET finished_at=?, seen_count=?, new_count=?,
           changed_count=?, missing_count=?, status='ok'
           WHERE id=?""",
        (_now(), len(incoming), new_count, changed_count, missing_count, batch_id),
    )
    return {"batch_id": batch_id, "seen": len(incoming), "new": new_count,
            "changed": changed_count, "missing": missing_count}


def _target_layer(health: str, data_status: str, source_active: int) -> str:
    if health == "healthy" and data_status == "sufficient" and source_active:
        return "healthy"
    return "observation"


def apply_probe_health(node_id: int, *, health: str, data_status: str,
                       layer: str | None = None, reason: str | None = None,
                       observation_reason: str | None = None,
                       last_error: str | None = None, actor: str = "p2") -> dict:
    """Apply a P2 health round without touching the P5 competition axis.

    P1's admin ``set_health`` deliberately also normalises competition state.
    P2 is forbidden from changing competition rules, so its scheduled rounds
    use this entry instead: health/data/layer change, competition is preserved.
    """
    with db.transaction() as conn:
        apply_probe_health_conn(
            conn, node_id, health=health, data_status=data_status, layer=layer,
            reason=reason, observation_reason=observation_reason,
            last_error=last_error, actor=actor,
        )
        return detail(node_id, conn=conn)


def apply_probe_health_conn(conn: sqlite3.Connection, node_id: int, *, health: str,
                            data_status: str, layer: str | None = None,
                            reason: str | None = None,
                            observation_reason: str | None = None,
                            last_error: str | None = None, actor: str = "p2") -> dict:
    if health not in HEALTH_STATES:
        raise NodePoolError("invalid_health", f"基础健康状态必须是 {HEALTH_STATES}")
    if data_status not in DATA_STATES:
        raise NodePoolError("invalid_data_status", f"数据状态必须是 {DATA_STATES}")
    ensure_schema(conn)
    node = _existing_node(conn, node_id)
    before = dict(node)
    if layer is None:
        layer = _target_layer(health, data_status, int(node["source_active"]))
    if layer not in LAYERS:
        raise NodePoolError("invalid_layer", f"管理层必须是 {LAYERS}")
    if layer == "healthy" and (health != "healthy" or data_status != "sufficient"
                               or not int(node["source_active"])):
        raise NodePoolError("not_health_eligible",
                            "进入健康池需要基础健康=健康、数据充足且来源有效")
    if layer == "healthy":
        observation_reason = None
    elif observation_reason is None:
        observation_reason = reason or "health_unknown"
    conn.execute(
        """UPDATE np_nodes SET base_health=?, data_status=?, management_layer=?,
           observation_reason=?, last_error=?, last_test_at=?, updated_at=? WHERE id=?""",
        (health, data_status, layer, observation_reason, last_error,
         _now(), _now(), node_id),
    )
    after = _existing_node(conn, node_id)
    _event(conn, node_id, "health_update", before=before, after=after,
           reason=reason or observation_reason, actor=actor,
           detail={"health": health, "data_status": data_status,
                   "competition_preserved": True})
    return {"before": before, "after": after}


def set_health(node_id: int, *, health: str, data_status: str | None = None,
               reason: str | None = None, observation_reason: str | None = None,
               actor: str = "admin", last_error: str | None = None) -> dict:
    """Apply a P2/P3 supplied health result.  P1 only manages the state."""
    if health not in HEALTH_STATES:
        raise NodePoolError("invalid_health", f"基础健康状态必须是 {HEALTH_STATES}")
    data_status = data_status or "unknown"
    if data_status not in DATA_STATES:
        raise NodePoolError("invalid_data_status", f"数据状态必须是 {DATA_STATES}")
    with db.transaction() as conn:
        ensure_schema(conn)
        node = _existing_node(conn, node_id)
        before = dict(node)
        target = _target_layer(health, data_status, int(node["source_active"]))
        competition = node["competition_state"]
        old_reason = node["observation_reason"]
        if target == "healthy":
            if competition in ("observation", "candidate"):
                competition = "candidate"
            observation_reason = None
        else:
            if competition in ("candidate", "preferred"):
                competition = "observation"
            if observation_reason:
                pass
            elif reason:
                observation_reason = reason
            elif not int(node["source_active"]):
                observation_reason = "source_missing"
            elif health == "unhealthy":
                observation_reason = "health_unhealthy"
            elif data_status == "insufficient":
                observation_reason = "insufficient_data"
            else:
                observation_reason = "health_unknown"
        conn.execute(
            """UPDATE np_nodes SET base_health=?, data_status=?, management_layer=?,
               competition_state=?, observation_reason=?, last_error=?, last_test_at=?,
               updated_at=? WHERE id=?""",
            (health, data_status, target, competition, observation_reason,
             last_error, _now(), _now(), node_id),
        )
        after = _existing_node(conn, node_id)
        _event(conn, node_id, "health_update", before=before, after=after,
               reason=reason or observation_reason, actor=actor,
               detail={"health": health, "data_status": data_status,
                       "previous_observation_reason": old_reason})
        return detail(node_id, conn=conn)


def set_layer(node_id: int, *, layer: str, reason: str | None = None,
              actor: str = "admin") -> dict:
    if layer not in LAYERS:
        raise NodePoolError("invalid_layer", f"管理层必须是 {LAYERS}")
    with db.transaction() as conn:
        ensure_schema(conn)
        node = _existing_node(conn, node_id)
        before = dict(node)
        if layer == "healthy":
            if (node["base_health"] != "healthy" or node["data_status"] != "sufficient"
                    or not int(node["source_active"])):
                raise NodePoolError("not_health_eligible",
                                    "进入健康池需要基础健康=健康、数据充足且来源有效")
        competition = node["competition_state"]
        if layer != "healthy" and competition in ("candidate", "preferred"):
            competition = "observation"
        if layer == "healthy" and competition == "observation":
            competition = "candidate"
        observation_reason = None if layer == "healthy" else (reason or "manual_observation")
        conn.execute(
            """UPDATE np_nodes SET management_layer=?, competition_state=?,
               observation_reason=?, updated_at=? WHERE id=?""",
            (layer, competition, observation_reason, _now(), node_id),
        )
        after = _existing_node(conn, node_id)
        _event(conn, node_id, "layer_transition", before=before, after=after,
               reason=reason, actor=actor)
        return detail(node_id, conn=conn)


def set_competition(node_id: int, *, state: str, reason: str | None = None,
                    actor: str = "admin") -> dict:
    if state not in COMPETITION_STATES:
        raise NodePoolError("invalid_competition", f"智能竞争状态必须是 {COMPETITION_STATES}")
    with db.transaction() as conn:
        ensure_schema(conn)
        node = _existing_node(conn, node_id)
        before = dict(node)
        if state in ("candidate", "preferred"):
            if (node["base_health"] != "healthy" or node["data_status"] != "sufficient"
                    or not int(node["source_active"]) or node["management_layer"] != "healthy"):
                raise NodePoolError("not_competition_eligible",
                                    "候选/优选需要节点处于健康池且基础健康合格")
        conn.execute(
            "UPDATE np_nodes SET competition_state=?, updated_at=? WHERE id=?",
            (state, _now(), node_id),
        )
        after = _existing_node(conn, node_id)
        _event(conn, node_id, "competition_update", before=before, after=after,
               reason=reason, actor=actor)
        return detail(node_id, conn=conn)


def grant_qualification(node_id: int, qualification: str, *, reason: str | None = None,
                        actor: str = "admin") -> dict:
    q = str(qualification or "").strip()[:40]
    if not q:
        raise NodePoolError("invalid_qualification", "资格名称不能为空")
    with db.transaction() as conn:
        ensure_schema(conn)
        node = _existing_node(conn, node_id)
        active = conn.execute(
            "SELECT * FROM np_node_qualifications WHERE node_id=? AND qualification=? AND active=1",
            (node_id, q),
        ).fetchone()
        if active:
            return detail(node_id, conn=conn)
        conn.execute(
            """INSERT INTO np_node_qualifications
               (node_id,qualification,active,granted_at,reason) VALUES (?,?,1,?,?)""",
            (node_id, q, _now(), reason),
        )
        _event(conn, node_id, "qualification_granted", before=node, after=node,
               reason=reason, actor=actor, detail={"qualification": q})
        return detail(node_id, conn=conn)


def revoke_qualification(node_id: int, qualification: str, *, reason: str | None = None,
                         actor: str = "admin") -> dict:
    with db.transaction() as conn:
        ensure_schema(conn)
        node = _existing_node(conn, node_id)
        changed = conn.execute(
            """UPDATE np_node_qualifications SET active=0, revoked_at=?, reason=?
               WHERE node_id=? AND qualification=? AND active=1""",
            (_now(), reason, node_id, qualification),
        ).rowcount
        if changed:
            _event(conn, node_id, "qualification_revoked", before=node, after=node,
                   reason=reason, actor=actor, detail={"qualification": qualification})
        return detail(node_id, conn=conn)


def summary() -> dict:
    with db.transaction() as conn:
        ensure_schema(conn)
        total = conn.execute("SELECT COUNT(*) c FROM np_nodes").fetchone()["c"]
        counts = {row["management_layer"]: row["c"] for row in conn.execute(
            "SELECT management_layer, COUNT(*) c FROM np_nodes GROUP BY management_layer")}
        health = {row["base_health"]: row["c"] for row in conn.execute(
            "SELECT base_health, COUNT(*) c FROM np_nodes GROUP BY base_health")}
        competition = {row["competition_state"]: row["c"] for row in conn.execute(
            "SELECT competition_state, COUNT(*) c FROM np_nodes GROUP BY competition_state")}
        data = {row["data_status"]: row["c"] for row in conn.execute(
            "SELECT data_status, COUNT(*) c FROM np_nodes GROUP BY data_status")}
        source_active = conn.execute("SELECT COUNT(*) c FROM np_nodes WHERE source_active=1").fetchone()["c"]
        historical = total - source_active
        qualifications = conn.execute(
            "SELECT COUNT(*) c FROM np_node_qualifications WHERE active=1").fetchone()["c"]
        airports = [dict(row) for row in conn.execute(
            """SELECT n.airport_id,
                      COALESCE(MAX(a.name), MAX(n.source_name), '未命名机场') airport_name,
                      COUNT(*) raw_total,
                      SUM(CASE WHEN n.management_layer='healthy' THEN 1 ELSE 0 END) healthy,
                      SUM(CASE WHEN n.management_layer='observation' THEN 1 ELSE 0 END) observation,
                      SUM(CASE WHEN n.management_layer='raw' THEN 1 ELSE 0 END) raw_only,
                      SUM(CASE WHEN n.source_active=0 THEN 1 ELSE 0 END) historical
               FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id
               GROUP BY n.airport_id ORDER BY raw_total DESC, n.airport_id"""
        ).fetchall()]
        return {
            "raw_total": total,
            "healthy": counts.get("healthy", 0),
            "observation": counts.get("observation", 0),
            "raw_only": counts.get("raw", 0),
            "source_active": source_active,
            "historical": historical,
            "base_health": {k: health.get(k, 0) for k in HEALTH_STATES},
            "competition": {k: competition.get(k, 0) for k in COMPETITION_STATES},
            "data_status": {k: data.get(k, 0) for k in DATA_STATES},
            "active_qualifications": qualifications,
            "airports": airports,
        }


def list_nodes(*, layer: str | None = None, health: str | None = None,
               competition: str | None = None, data_status: str | None = None,
               airport_id: int | None = None, source_active: int | None = None,
               q: str | None = None, page: int = 1, per_page: int = 20) -> dict:
    if layer and layer not in LAYERS:
        raise NodePoolError("invalid_layer", f"管理层必须是 {LAYERS}")
    if health and health not in HEALTH_STATES:
        raise NodePoolError("invalid_health", f"基础健康状态必须是 {HEALTH_STATES}")
    if competition and competition not in COMPETITION_STATES:
        raise NodePoolError("invalid_competition", f"智能竞争状态必须是 {COMPETITION_STATES}")
    if data_status and data_status not in DATA_STATES:
        raise NodePoolError("invalid_data_status", f"数据状态必须是 {DATA_STATES}")
    page = max(int(page or 1), 1)
    per_page = min(max(int(per_page or 20), 1), 100)
    with db.transaction() as conn:
        ensure_schema(conn)
        where: list[str] = []
        args: list[Any] = []
        if layer:
            where.append("n.management_layer=?")
            args.append(layer)
        if health:
            where.append("n.base_health=?")
            args.append(health)
        if competition:
            where.append("n.competition_state=?")
            args.append(competition)
        if data_status:
            where.append("n.data_status=?")
            args.append(data_status)
        if airport_id is not None:
            where.append("n.airport_id=?")
            args.append(int(airport_id))
        if source_active is not None:
            where.append("n.source_active=?")
            args.append(1 if int(source_active) else 0)
        if q:
            needle = f"%{q.strip()}%"
            where.append("(n.current_name LIKE ? OR n.first_name LIKE ? OR n.server LIKE ?"
                         " OR EXISTS (SELECT 1 FROM np_node_names nh WHERE nh.node_id=n.id AND nh.name LIKE ?))")
            args.extend([needle, needle, needle, needle])
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) c FROM np_nodes n{clause}", args).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT n.*, a.name airport_name, a.url airport_url,
                   (SELECT GROUP_CONCAT(q.qualification, ',') FROM np_node_qualifications q
                    WHERE q.node_id=n.id AND q.active=1) qualifications
                FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id
                {clause}
                ORDER BY n.updated_at DESC, n.id DESC LIMIT ? OFFSET ?""",
            (*args, per_page, (page - 1) * per_page),
        ).fetchall()
        items = [_node_out(dict(row)) for row in rows]
        return {"items": items, "total": total, "page": page, "per_page": per_page,
                "pages": (total + per_page - 1) // per_page}


def _node_out(node: dict) -> dict:
    node["stable_uid"] = stable_uid(node)
    node["params"] = _params(node.get("params"))
    node["source_active"] = bool(node.get("source_active"))
    quals = node.get("qualifications")
    node["qualifications"] = sorted(quals.split(",")) if quals else []
    return node


def detail(node_id: int, conn: sqlite3.Connection | None = None) -> dict:
    if conn is None:
        with db.transaction() as own:
            ensure_schema(own)
            return detail(node_id, conn=own)
    node = conn.execute(
        """SELECT n.*, a.name airport_name, a.url airport_url FROM np_nodes n
           LEFT JOIN airports a ON a.id=n.airport_id WHERE n.id=?""", (node_id,)
    ).fetchone()
    if not node:
        raise NodePoolError("node_not_found", f"节点 {node_id} 不存在")
    out = _node_out(dict(node))
    out["names"] = [dict(row) for row in conn.execute(
        "SELECT name,first_seen_at,last_seen_at,seen_count FROM np_node_names"
        " WHERE node_id=? ORDER BY id", (node_id,)).fetchall()]
    out["events"] = [dict(row) for row in conn.execute(
        "SELECT * FROM np_node_events WHERE node_id=? ORDER BY id DESC LIMIT 100",
        (node_id,)).fetchall()]
    for event in out["events"]:
        event["detail"] = json.loads(event.pop("detail_json") or "{}")
    out["qualifications"] = [dict(row) for row in conn.execute(
        "SELECT qualification,active,granted_at,revoked_at,reason FROM np_node_qualifications"
        " WHERE node_id=? ORDER BY id", (node_id,)).fetchall()]
    return out
