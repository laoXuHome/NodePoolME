"""P6.1 final preferred-result facade.

The service intentionally owns no P5 decision logic.  It copies the current
P5 preferred rows into immutable P6 snapshots, and exposes read-only queries
that never create a version.  Keyword filtering and subscription output are
explicitly out of scope for this batch.
"""
from __future__ import annotations

import threading
from datetime import datetime

from backend.database import db

from . import store
from .store import CATEGORIES, FinalError

_SNAPSHOT_LOCK = threading.Lock()
_ALLOWED_TRIGGERS = {"manual", "auto", "startup", "cli"}


def _ensure(conn) -> None:
    store.ensure_schema(conn)


def ensure_schema() -> None:
    """Create the P6.1 side tables once; never creates a version."""
    with db.transaction() as conn:
        _ensure(conn)


def _trigger(value: str | None) -> str:
    value = (value or "manual").strip().lower()
    return value if value in _ALLOWED_TRIGGERS else "manual"


def _reject(reason: str, message: str, detail=None):
    with db.transaction() as conn:
        _ensure(conn)
        store.record_event(conn, "snapshot_rejected", reason=reason, detail=detail)
    raise FinalError(reason, message, status=409)


def summary() -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        return store.summary(conn)


def current(category: str | None = None) -> dict:
    if category and category not in CATEGORIES:
        raise FinalError("invalid_category", "未知的优选类别", status=400)
    with db.transaction() as conn:
        _ensure(conn)
        return store.current_payload(conn, category=category)


def versions(limit: int = 30) -> list[dict]:
    with db.transaction() as conn:
        _ensure(conn)
        return store.list_versions(conn, limit=limit)


def version(version_id: int, category: str | None = None) -> dict:
    if category and category not in CATEGORIES:
        raise FinalError("invalid_category", "未知的优选类别", status=400)
    with db.transaction() as conn:
        _ensure(conn)
        return store.version_detail(conn, version_id, category=category)


def diff(version_id: int | None = None) -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        return store.diff_payload(conn, version_id)


def _snapshot_locked(trigger: str) -> dict:
    rejection: tuple[str, str, dict | None] | None = None
    result: dict | None = None
    with db.transaction() as conn:
        _ensure(conn)
        run = store.latest_run(conn)
        if not run:
            rejection = ("no_p5_run", "P5 尚未执行竞争，不能生成有效优选版本",
                         {"trigger": trigger})
        elif run.get("status") != "success":
            rejection = ("run_not_success", "P5 最近一轮竞争未成功，保留上一有效版本",
                         {"trigger": trigger, "run_id": run.get("id"),
                          "status": run.get("status")})
        else:
            entries = store.collect_entries(conn)
            signature = store.signature_of(entries)
            counts = store.category_counts(entries)
            previous_row = store.latest_version(conn)
            if previous_row and previous_row.get("signature") == signature:
                store.record_event(conn, "snapshot_unchanged", version_id=previous_row.get("id"),
                                   reason=trigger,
                                   detail={"run_id": run.get("id"), "total": len(entries)})
                result = {
                    "created": False,
                    "reason": "unchanged",
                    "version": store.version_summary(previous_row),
                    "categories": [{"code": code, "count": counts[code]} for code in CATEGORIES],
                    "pending": False,
                }
            else:
                store.create_version(
                    conn, entries, previous_row, trigger=trigger, source_run=run,
                    signature=signature, now=datetime.now(),
                )
                summary = store.version_summary(store.latest_version(conn))
                result = {
                    "created": True,
                    "reason": "created",
                    "version": summary,
                    "categories": [{"code": code, "count": counts[code]} for code in CATEGORIES],
                    "pending": False,
                }
    if rejection:
        _reject(*rejection)
    if result is None:
        raise FinalError("snapshot_failed", "未能生成优选版本", status=500)
    return result


def snapshot(trigger: str = "manual") -> dict:
    """Create one immutable version, or return the existing identical one."""
    trigger = _trigger(trigger)
    acquired = _SNAPSHOT_LOCK.acquire(blocking=False)
    if not acquired:
        raise FinalError("snapshot_busy", "已有快照任务在执行，请稍后重试", status=409)
    try:
        return _snapshot_locked(trigger)
    finally:
        _SNAPSHOT_LOCK.release()


def auto_snapshot() -> dict:
    """Scheduler entry: cheap check first; no version for running/failed runs."""
    if not _SNAPSHOT_LOCK.acquire(blocking=False):
        return {"created": False, "reason": "busy"}
    try:
        with db.transaction() as conn:
            _ensure(conn)
            run = store.latest_run(conn)
            if not run:
                return {"created": False, "reason": "no_p5_run"}
            if run.get("status") != "success":
                return {"created": False, "reason": "run_not_success",
                        "run_status": run.get("status")}
            entries = store.collect_entries(conn)
            signature = store.signature_of(entries)
            current_row = store.latest_version(conn)
            if current_row and current_row.get("signature") == signature:
                return {"created": False, "reason": "unchanged",
                        "version_no": current_row.get("version_no")}
            if current_row and str(current_row.get("source_run_id")) == str(run.get("id")):
                return {"created": False, "reason": "same_run",
                        "version_no": current_row.get("version_no")}
        return _snapshot_locked("auto")
    finally:
        _SNAPSHOT_LOCK.release()


def dashboard_summary() -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        payload = store.summary(conn)
        cur = payload.get("current") or {}
        counts = cur.get("category_counts") or {}
        return {
            "total": int(cur.get("total_nodes") or 0),
            "version_no": cur.get("version_no"),
            "generated_at": cur.get("created_at"),
            "is_empty": bool(cur.get("is_empty", False)),
            "preferred_standard": int(counts.get("standard", 0)),
            "preferred_business": {
                code: int(counts.get(code, 0))
                for code in ("gpt", "youtube", "telegram", "github", "global")
            },
            "pending": bool(payload.get("pending", False)),
            "source_run_status": (payload.get("source") or {}).get("status"),
            "history_count": int(payload.get("history_count") or 0),
        }
