"""P5 service facade used by HTTP, the scheduler and the dashboard."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.database import db
from backend.competition import engine, store


def ensure_defaults() -> None:
    with db.transaction() as conn:
        store.ensure_defaults(conn)
        store.ensure_states(conn)


def settings() -> dict:
    ensure_defaults()
    return store.settings_payload()


def update_settings(data: dict) -> dict:
    ensure_defaults()
    return store.update_settings(data)


def restore_defaults() -> dict:
    ensure_defaults()
    return store.restore_defaults()


def summary() -> dict:
    ensure_defaults()
    counts = store.counts()
    categories = []
    for code in store.CATEGORIES:
        by_state = counts.get(code) or {}
        categories.append({
            "code": code,
            "label": store.CATEGORY_LABELS[code],
            "title": store.CATEGORY_TITLES[code],
            "short": store.CATEGORY_CODES[code],
            "states": {state: int(by_state.get(state, 0)) for state in store.STATES},
            "total": sum(int(by_state.get(state, 0)) for state in store.STATES),
        })
    preferred = {code: int((counts.get(code) or {}).get("preferred", 0))
                 for code in store.CATEGORIES}
    candidate_total = sum(int((counts.get(code) or {}).get("candidate", 0))
                          for code in store.CATEGORIES)
    observation_total = sum(int((counts.get(code) or {}).get("observation", 0))
                            for code in store.CATEGORIES)
    eliminated_total = sum(int((counts.get(code) or {}).get("eliminated", 0))
                           for code in store.CATEGORIES)
    distinct = {}
    for state in store.STATES:
        distinct[state] = int(db.row(
            "SELECT COUNT(DISTINCT node_id) AS c FROM np5_state WHERE state=?",
            (state,),
        )["c"] or 0)
    last = store.last_run()
    return {
        "categories": categories,
        "preferred": preferred,
        "preferred_standard": preferred.get("standard", 0),
        "preferred_business": {code: preferred.get(code, 0)
                               for code in store.CATEGORIES if code != "standard"},
        "candidate_total": candidate_total,
        "observation_total": observation_total,
        "eliminated_total": eliminated_total,
        "distinct": distinct,
        "last_run": last,
        "last_competition_at": (last or {}).get("finished_at")
                                  or (last or {}).get("started_at"),
        "settings": store.get_settings(),
        "config_version": store.get_settings().get("config_version"),
        "stages": store.STATE_LABELS,
        "boundary": {
            "p1_write": False, "p2_write": False, "p3_write": False, "p4_write": False,
            "p5_only": True, "p6": False, "p7": False,
            "keyword_filter": False,
        },
    }


def dashboard_summary() -> dict:
    data = summary()
    return {
        "preferred_standard": data["preferred_standard"],
        "preferred_business": data["preferred_business"],
        "candidate_total": data["candidate_total"],
        "observation_total": data["observation_total"],
        "eliminated_total": data["eliminated_total"],
        "last_competition_at": data["last_competition_at"],
        "categories": data["categories"],
    }


def node_rows(*, category: str = "standard", state: str | None = None,
              q: str | None = None, page: int = 1, per_page: int = 20) -> dict:
    ensure_defaults()
    if category not in store.CATEGORIES:
        raise store.CompetitionError("invalid_category", "竞争类别不存在")
    if state and state not in store.STATES:
        raise store.CompetitionError("invalid_state", "竞争状态不存在")
    return store.state_rows(category=category, state=state, q=q, page=page,
                            per_page=per_page)


def node_detail(node_id: int) -> dict:
    ensure_defaults()
    return store.node_detail(node_id)


def history(node_id: int | None = None, *, category: str | None = None,
            limit: int = 60) -> dict:
    ensure_defaults()
    if category and category not in store.CATEGORIES:
        raise store.CompetitionError("invalid_category", "竞争类别不存在")
    return {"items": store.history(node_id, category=category, limit=limit),
            "node_id": node_id, "category": category}


def run(*, trigger: str = "manual", categories: list[str] | None = None,
        now: datetime | None = None, refresh_p4: bool = True) -> dict:
    ensure_defaults()
    result = engine.run(trigger=trigger, categories=categories, now=now,
                        refresh_p4=refresh_p4)
    return result


async def tick() -> dict:
    if not engine.due():
        return {"ok": True, "skipped": True, "reason": "not_due"}
    return run(trigger="auto", refresh_p4=True)


def runs(limit: int = 20) -> list[dict]:
    ensure_defaults()
    rows = db.rows("SELECT * FROM np5_runs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 100)),))
    for row in rows:
        row["categories"] = db.loads(row.get("categories"), []) or []
        row["detail"] = db.loads(row.get("detail_json"), {}) or {}
    return rows
