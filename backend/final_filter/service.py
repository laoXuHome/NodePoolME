"""P6.2.1 final-delivery keyword filter facade."""
from __future__ import annotations

from backend.database import db

from . import store
from .store import CATEGORIES, FinalFilterError


def _ensure(conn) -> None:
    store.ensure_schema(conn)


def summary() -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        return store.summary_payload(conn)


def rules() -> dict:
    payload = summary()
    return {
        "rules": payload["rules"],
        "rule_stats": payload["rule_stats"],
        "revision": payload["revision"],
        "computed_at": payload["computed_at"],
        "p6": payload["p6"],
        "default_rules": payload["default_rules"],
    }


def preview(category: str | None = None) -> dict:
    if category and category not in CATEGORIES:
        raise FinalFilterError("invalid_category", "未知的优选类别", status=400)
    with db.transaction() as conn:
        _ensure(conn)
        return store.preview_payload(conn, category=category)


def _mutated(conn, result: dict, *, trigger: str) -> dict:
    store.bump_revision(conn)
    store.compute_preview(conn, trigger)
    return {**result, "summary": store.summary_payload(conn)}


def create_rule(keyword: str, enabled: bool = True) -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        rule = store.add_rule(conn, keyword, enabled)
        return _mutated(conn, {"rule": rule}, trigger="rule_create")


def update_rule(rule_id: int, *, keyword: str | None = None, enabled: bool | None = None) -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        rule = store.update_rule(conn, rule_id, keyword=keyword, enabled=enabled)
        return _mutated(conn, {"rule": rule}, trigger="rule_update")


def delete_rule(rule_id: int) -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        rule = store.delete_rule(conn, rule_id)
        return _mutated(conn, {"rule": rule}, trigger="rule_delete")


def restore_defaults() -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        count = store.restore_defaults(conn)
        return _mutated(conn, {"restored": count}, trigger="rule_defaults")


def recompute() -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        store.compute_preview(conn, "manual")
        return store.summary_payload(conn)


def dashboard_summary() -> dict:
    with db.transaction() as conn:
        _ensure(conn)
        payload = store.summary_payload(conn)
        return {
            "internal": payload["totals"]["internal"],
            "filtered": payload["totals"]["filtered"],
            "allowed": payload["totals"]["allowed"],
            "enabled_rules": payload["rule_stats"]["enabled"],
            "total_rules": payload["rule_stats"]["total"],
            "matched_rules": payload["rule_stats"]["matched"],
            "version_no": payload["p6"]["version_no"],
            "computed_at": payload["computed_at"],
            "revision": payload["revision"],
        }
