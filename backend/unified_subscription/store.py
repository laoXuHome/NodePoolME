"""Append-only P6.2.2 generation records.

A batch is one unified node collection.  Each batch owns exactly four output
records (Clash, sing-box, Base64, Loon).  Reproducing creates a new batch; old
batches, outputs and node snapshots are never updated or deleted.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from typing import Any

FORMAT_KEYS = ("clash", "singbox", "base64", "loon")

_DDL = (
    """CREATE TABLE IF NOT EXISTS np622_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_no TEXT NOT NULL UNIQUE,
        created_at TEXT NOT NULL,
        trigger TEXT,
        p6_version_id INTEGER NOT NULL,
        p6_version_no TEXT,
        p621_revision INTEGER NOT NULL,
        p621_computed_at TEXT,
        category_counts_json TEXT,
        merged_count INTEGER NOT NULL DEFAULT 0,
        unique_count INTEGER NOT NULL DEFAULT 0,
        duplicate_count INTEGER NOT NULL DEFAULT 0,
        filtered_count INTEGER NOT NULL DEFAULT 0,
        candidate_count INTEGER NOT NULL DEFAULT 0,
        compatibility_excluded_count INTEGER NOT NULL DEFAULT 0,
        output_count INTEGER NOT NULL DEFAULT 0,
        gpt_count INTEGER NOT NULL DEFAULT 0,
        yt_count INTEGER NOT NULL DEFAULT 0,
        tg_count INTEGER NOT NULL DEFAULT 0,
        gh_count INTEGER NOT NULL DEFAULT 0,
        multi_label_count INTEGER NOT NULL DEFAULT 0,
        is_empty INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        status_reason TEXT,
        unsupported_json TEXT,
        node_set_sha256 TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS np622_outputs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL,
        format TEXT NOT NULL,
        status TEXT NOT NULL,
        node_count INTEGER NOT NULL DEFAULT 0,
        generated_at TEXT NOT NULL,
        p6_version_id INTEGER NOT NULL,
        p6_version_no TEXT,
        p621_revision INTEGER NOT NULL,
        content_sha256 TEXT,
        content_bytes INTEGER NOT NULL DEFAULT 0,
        content_text TEXT,
        failure_reason TEXT,
        UNIQUE (batch_id, format)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np622_outputs_batch ON np622_outputs (batch_id, format)",
    """CREATE TABLE IF NOT EXISTS np622_nodes (
        batch_id INTEGER NOT NULL,
        node_id INTEGER NOT NULL,
        node_name TEXT,
        airport_id INTEGER,
        airport_name TEXT,
        server TEXT,
        port INTEGER,
        protocol TEXT,
        speed_grade TEXT,
        display_name TEXT,
        labels_json TEXT,
        categories_json TEXT,
        status TEXT NOT NULL,
        hits_json TEXT,
        unsupported_json TEXT,
        score REAL,
        p80_ms INTEGER,
        PRIMARY KEY (batch_id, node_id)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np622_nodes_status ON np622_nodes (batch_id, status)",
    """CREATE TABLE IF NOT EXISTS np622_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        at TEXT NOT NULL,
        event TEXT NOT NULL,
        batch_id INTEGER,
        detail_json TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_np622_events_batch ON np622_events (batch_id, id)",
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def now() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_schema(conn: sqlite3.Connection) -> None:
    for sql in _DDL:
        conn.execute(sql)


def next_batch_no(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT COALESCE(MAX(id), 0) AS mx FROM np622_batches").fetchone()
    seq = int(row["mx"] or 0) + 1
    return f"P6.2.2-{seq:04d}"


def insert_batch(conn: sqlite3.Connection, payload: dict) -> int:
    stamp = payload.get("created_at") or now()
    cur = conn.execute(
        """INSERT INTO np622_batches
           (batch_no,created_at,trigger,p6_version_id,p6_version_no,p621_revision,
            p621_computed_at,category_counts_json,merged_count,unique_count,duplicate_count,
            filtered_count,candidate_count,compatibility_excluded_count,output_count,
            gpt_count,yt_count,tg_count,gh_count,multi_label_count,is_empty,status,
            status_reason,unsupported_json,node_set_sha256)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload["batch_no"], stamp, payload.get("trigger"), int(payload["p6_version_id"]),
         payload.get("p6_version_no"), int(payload.get("p621_revision") or 0),
         payload.get("p621_computed_at"), _json(payload.get("category_counts") or {}),
         int(payload.get("merged_count") or 0), int(payload.get("unique_count") or 0),
         int(payload.get("duplicate_count") or 0), int(payload.get("filtered_count") or 0),
         int(payload.get("candidate_count") or 0),
         int(payload.get("compatibility_excluded_count") or 0),
         int(payload.get("output_count") or 0), int(payload.get("gpt_count") or 0),
         int(payload.get("yt_count") or 0), int(payload.get("tg_count") or 0),
         int(payload.get("gh_count") or 0), int(payload.get("multi_label_count") or 0),
         1 if payload.get("is_empty") else 0, payload.get("status") or "success",
         payload.get("status_reason"), _json(payload.get("unsupported") or {}),
         payload.get("node_set_sha256")),
    )
    return int(cur.lastrowid)


def insert_output(conn: sqlite3.Connection, batch_id: int, payload: dict) -> int:
    cur = conn.execute(
        """INSERT INTO np622_outputs
           (batch_id,format,status,node_count,generated_at,p6_version_id,p6_version_no,
            p621_revision,content_sha256,content_bytes,content_text,failure_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(batch_id), payload["format"], payload.get("status") or "success",
         int(payload.get("node_count") or 0), payload.get("generated_at") or now(),
         int(payload["p6_version_id"]), payload.get("p6_version_no"),
         int(payload.get("p621_revision") or 0), payload.get("content_sha256"),
         int(payload.get("content_bytes") or 0), payload.get("content_text"),
         payload.get("failure_reason")),
    )
    return int(cur.lastrowid)


def insert_node(conn: sqlite3.Connection, batch_id: int, payload: dict) -> None:
    conn.execute(
        """INSERT INTO np622_nodes
           (batch_id,node_id,node_name,airport_id,airport_name,server,port,protocol,
            speed_grade,display_name,labels_json,categories_json,status,hits_json,
            unsupported_json,score,p80_ms)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (int(batch_id), int(payload["node_id"]), payload.get("node_name"),
         payload.get("airport_id"), payload.get("airport_name"), payload.get("server"),
         payload.get("port"), payload.get("protocol"), payload.get("speed_grade"),
         payload.get("display_name"), _json(payload.get("labels") or []),
         _json(payload.get("categories") or []), payload.get("status") or "allowed",
         _json(payload.get("hits") or []), _json(payload.get("unsupported") or {}),
         payload.get("score"), payload.get("p80_ms")),
    )


def append_event(conn: sqlite3.Connection, event: str, batch_id: int | None, detail: dict) -> None:
    conn.execute(
        "INSERT INTO np622_events (at,event,batch_id,detail_json) VALUES (?,?,?,?)",
        (now(), event, batch_id, _json(detail)),
    )


def latest_batch(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM np622_batches ORDER BY id DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def batch_row(conn: sqlite3.Connection, batch_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM np622_batches WHERE id=?", (int(batch_id),)).fetchone()
    return dict(row) if row else None


def outputs_of(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM np622_outputs WHERE batch_id=? ORDER BY id", (int(batch_id),)
    ).fetchall()
    order = {fmt: i for i, fmt in enumerate(FORMAT_KEYS)}
    return sorted((dict(r) for r in rows), key=lambda r: order.get(r["format"], 99))


def output_of(conn: sqlite3.Connection, batch_id: int, fmt: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM np622_outputs WHERE batch_id=? AND format=?",
        (int(batch_id), fmt),
    ).fetchone()
    return dict(row) if row else None


def nodes_of(conn: sqlite3.Connection, batch_id: int) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM np622_nodes WHERE batch_id=?
           ORDER BY CASE status WHEN 'allowed' THEN 0 WHEN 'filtered' THEN 1 ELSE 2 END,
                    COALESCE(score,-1) DESC, node_id""",
        (int(batch_id),),
    ).fetchall()
    return [dict(r) for r in rows]


def history(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM np622_batches ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),)
    ).fetchall()
    batches = [dict(r) for r in rows]
    for batch in batches:
        batch["outputs"] = outputs_of(conn, batch["id"])
    return batches


def batch_payload(batch: dict) -> dict:
    return {
        "id": int(batch["id"]),
        "batch_no": batch["batch_no"],
        "created_at": batch["created_at"],
        "trigger": batch.get("trigger"),
        "p6": {"version_id": int(batch["p6_version_id"]),
               "version_no": batch.get("p6_version_no")},
        "p621": {"revision": int(batch.get("p621_revision") or 0),
                 "computed_at": batch.get("p621_computed_at")},
        "category_counts": loads(batch.get("category_counts_json"), {}) or {},
        "merged_count": int(batch.get("merged_count") or 0),
        "unique_count": int(batch.get("unique_count") or 0),
        "duplicate_count": int(batch.get("duplicate_count") or 0),
        "filtered_count": int(batch.get("filtered_count") or 0),
        "candidate_count": int(batch.get("candidate_count") or 0),
        "compatibility_excluded_count": int(batch.get("compatibility_excluded_count") or 0),
        "output_count": int(batch.get("output_count") or 0),
        "labels": {"gpt": int(batch.get("gpt_count") or 0),
                   "yt": int(batch.get("yt_count") or 0),
                   "tg": int(batch.get("tg_count") or 0),
                   "gh": int(batch.get("gh_count") or 0),
                   "multi": int(batch.get("multi_label_count") or 0)},
        "is_empty": bool(batch.get("is_empty")),
        "status": batch.get("status"),
        "status_reason": batch.get("status_reason"),
        "unsupported": loads(batch.get("unsupported_json"), {}) or {},
        "node_set_sha256": batch.get("node_set_sha256"),
    }


def output_payload(output: dict, *, include_content: bool = False) -> dict:
    payload = {
        "id": int(output["id"]),
        "format": output["format"],
        "status": output["status"],
        "node_count": int(output.get("node_count") or 0),
        "generated_at": output.get("generated_at"),
        "p6": {"version_id": int(output["p6_version_id"]),
               "version_no": output.get("p6_version_no")},
        "p621": {"revision": int(output.get("p621_revision") or 0)},
        "content_sha256": output.get("content_sha256"),
        "content_bytes": int(output.get("content_bytes") or 0),
        "failure_reason": output.get("failure_reason"),
    }
    if include_content:
        payload["content"] = output.get("content_text") or ""
    return payload


def node_payload(node: dict) -> dict:
    return {
        "node_id": int(node["node_id"]),
        "node_name": node.get("node_name"),
        "airport_id": node.get("airport_id"),
        "airport_name": node.get("airport_name"),
        "server": node.get("server"),
        "port": node.get("port"),
        "protocol": node.get("protocol"),
        "speed_grade": node.get("speed_grade"),
        "display_name": node.get("display_name"),
        "labels": loads(node.get("labels_json"), []) or [],
        "categories": loads(node.get("categories_json"), []) or [],
        "status": node.get("status"),
        "hits": loads(node.get("hits_json"), []) or [],
        "unsupported": loads(node.get("unsupported_json"), {}) or {},
        "score": node.get("score"),
        "p80_ms": node.get("p80_ms"),
    }
