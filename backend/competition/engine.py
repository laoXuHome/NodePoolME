"""P5 competition engine: six independent state machines over P1–P4 evidence.

The engine is deliberately conservative.  It never turns missing evidence into
zero, never recomputes P4, and never promotes a candidate on one high sample.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from backend.business import store as business_store
from backend.database import db
from backend.health.prober import tcp_grade
from backend.quality import service as quality_service
from backend.quality import store as quality_store
from backend.competition import store

ENTRY_SCORE = 75.0
ENTRY_MATURITY = 70.0
TCP_P80_LIMIT = 500
STABILITY_MAX_CONSECUTIVE = 3

CATEGORY_BUSINESS = {
    "gpt": "gpt", "youtube": "youtube", "telegram": "telegram",
    "github": "github", "global": "global",
}

_run_lock = threading.Lock()


def _stamp(value: datetime | None = None) -> str:
    return (value or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")


def _float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _score(value: Any) -> float | None:
    number = _float(value)
    return round(number, 2) if number is not None else None


def _evidence(node: dict, category: str, *, conn=None) -> dict | None:
    """Read P4/P3 evidence without recalculating or writing their state."""
    quality = quality_store.current(int(node["id"]), conn=conn)
    if quality is None:
        return None
    detail = quality.get("detail") or {}
    qscore = _score(quality.get("quality_score"))
    maturity = _score(quality.get("maturity_score"))
    p80 = detail.get("p80_ms")
    p80 = int(round(float(p80))) if p80 is not None else None
    speed_grade = quality.get("speed_grade") or "empty"
    speed_mb = _float(quality.get("speed_mb_s"))
    speed_min = _float((quality_store.get_settings() or {}).get("speed_minimum")) or 0.5
    severe = detail.get("severe_days")
    max_consecutive = detail.get("max_consecutive_failures")
    stability_score = _float(detail.get("stability_score"))
    p3 = business_store.states_for_node(int(node["id"]), conn=conn)
    business = p3.get(CATEGORY_BUSINESS[category]) if category in CATEGORY_BUSINESS else None
    p3_state = (business or {}).get("state") or "untested"
    p3_expired = bool((business or {}).get("expired"))
    p3_active = bool((business or {}).get("qualification_active"))
    key_parts = [
        str(quality.get("evaluated_at") or ""),
        str(qscore), str(maturity), str(p80), str(speed_grade), str(speed_mb),
        str(node.get("base_health")), str(node.get("source_active")),
        str(node.get("airport_enabled")), p3_state, str(p3_expired), str(p3_active),
        str((business or {}).get("last_run_at") or ""),
    ]
    return {
        "key": "|".join(key_parts),
        "quality": quality,
        "score": qscore,
        "maturity": maturity,
        "mature": maturity is not None and maturity >= ENTRY_MATURITY,
        "quality_at": quality.get("evaluated_at"),
        "p80_ms": p80,
        "delay_grade": tcp_grade(p80)["key"] if p80 is not None else "none",
        "delay_label": tcp_grade(p80)["label"] if p80 is not None else "无有效样本",
        "speed_grade": speed_grade,
        "speed_mb_s": speed_mb,
        "speed_minimum": speed_min,
        "speed_at": quality.get("speed_at"),
        "severe_days": int(severe or 0) if severe is not None else None,
        "max_consecutive_failures": (
            int(max_consecutive) if max_consecutive is not None else None),
        "stability_score": stability_score,
        "business": business,
        "business_state": p3_state,
        "business_expired": p3_expired,
        "business_active": p3_active,
        "has_speed_data": speed_grade not in ("empty", "excluded"),
    }


def _admission(ev: dict, category: str, *, min_score: float = ENTRY_SCORE) -> dict:
    checks: list[dict] = []
    hard: list[str] = []
    pending: list[str] = []
    warnings: list[str] = []

    def add(code: str, ok: bool, *, severity: str = "hard", detail: Any = None):
        checks.append({"code": code, "ok": bool(ok), "severity": severity,
                       "detail": detail})
        if not ok:
            if severity == "pending":
                pending.append(code)
            elif severity == "warning":
                warnings.append(code)
            else:
                hard.append(code)

    node = ev.get("node") or {}
    add("source_inactive", bool(node.get("source_active")), detail="P1 来源失活")
    add("airport_disabled", bool(node.get("airport_enabled", True)), detail="机场已停用")
    health = node.get("base_health")
    if health == "healthy":
        add("health_ok", True, detail=health)
    elif health == "unknown" or health is None:
        add("health_unknown", False, severity="pending", detail=health)
    else:
        add("health_unhealthy", False, detail=health)
    add("p80_missing", ev.get("p80_ms") is not None, severity="pending")
    add("p80_over_500", (ev.get("p80_ms") is None or ev["p80_ms"] <= TCP_P80_LIMIT),
        detail=ev.get("p80_ms"))
    score = ev.get("score")
    add("quality_missing", score is not None, severity="pending")
    add("quality_below_line", score is None or score >= float(min_score),
        detail={"score": score, "line": float(min_score)})
    maturity = ev.get("maturity")
    add("maturity_missing", maturity is not None, severity="pending")
    add("maturity_below_70", maturity is None or maturity >= ENTRY_MATURITY,
        detail=maturity)
    severe = ev.get("severe_days")
    max_consecutive = ev.get("max_consecutive_failures")
    add("stability_failed",
        not ((severe is not None and severe > 0)
             or (max_consecutive is not None and max_consecutive >= STABILITY_MAX_CONSECUTIVE)),
        detail={"severe_days": severe, "max_consecutive_failures": max_consecutive})
    speed_grade = ev.get("speed_grade") or "empty"
    if speed_grade == "danger":
        add("speed_danger", False, detail=ev.get("speed_mb_s"))
    elif speed_grade in ("empty", "excluded") or ev.get("speed_mb_s") is None:
        add("no_speed_data", True, severity="warning", detail=speed_grade)
    else:
        add("speed_ok", True, detail={"grade": speed_grade, "mb_s": ev.get("speed_mb_s")})
        minimum = _float(ev.get("speed_minimum")) or 0.5
        add("speed_below_minimum", float(ev["speed_mb_s"]) >= minimum,
            detail={"mb_s": ev.get("speed_mb_s"), "minimum": minimum})

    if category in CATEGORY_BUSINESS:
        business = ev.get("business") or {}
        state = business.get("state") or "untested"
        expired = bool(business.get("expired"))
        if expired:
            add("business_expired", False, severity="pending", detail=state)
        elif state == "qualified":
            add("business_qualified", True, detail=state)
        elif state in ("untested", "unknown", "pending"):
            add("business_unknown", False, severity="pending", detail=state)
        else:
            add("business_not_qualified", False, detail=state)
    else:
        add("business_not_required", True, severity="info")

    ok = not hard and not pending
    return {
        "ok": ok,
        "hard": hard,
        "pending": pending,
        "warnings": warnings,
        "checks": checks,
        "reasons": hard + pending + warnings,
        "no_speed_data": speed_grade in ("empty", "excluded"),
        "min_score": float(min_score),
    }


def _reason(admission: dict) -> str | None:
    reasons = admission.get("hard") or admission.get("pending") or []
    return reasons[0] if reasons else None


def _base_state(node: dict, category: str, cfg: dict, now: datetime,
                prior: dict | None = None) -> dict:
    if prior is not None:
        state = dict(prior)
        state["admission"] = state.get("admission") or {}
        state["detail"] = state.get("detail") or {}
        state["recovery_required"] = bool(state.get("recovery_required"))
        return state
    start = store._parse(node.get("first_seen_at")) or now
    return {
        "node_id": int(node["id"]),
        "category": category,
        "state": "observation",
        "score": None,
        "maturity": None,
        "p80_ms": None,
        "delay_grade": "none",
        "speed_grade": "empty",
        "speed_mb_s": None,
        "admission_ok": False,
        "admission_reason": None,
        "admission": {},
        "observation_started_at": _stamp(start),
        "observation_until": _stamp(start + timedelta(days=int(cfg["observation_days"]))),
        "candidate_since": None,
        "preferred_since": None,
        "eliminated_since": None,
        "last_evaluated_at": None,
        "last_change_at": None,
        "last_evidence_key": None,
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "advantage_count": 0,
        "advantage_target_id": None,
        "recovery_required": False,
        "last_reason": "initial_observation",
        "config_version": int(cfg["config_version"]),
        "detail": {},
        "updated_at": _stamp(now),
    }


def _history(conn, st: dict, *, from_state: str | None, to_state: str, before_score,
             reason: str | None, now: str, replaced_node_id: int | None = None,
             replace_reason: str | None = None) -> int:
    return store.record_change(
        conn, at=now, node_id=int(st["node_id"]), category=st["category"],
        from_state=from_state, to_state=to_state, before_score=before_score,
        after_score=st.get("score"), admission=st.get("admission") or {},
        reason=reason, replaced_node_id=replaced_node_id,
        replace_reason=replace_reason, config_version=int(st.get("config_version") or 1),
        evidence_key=st.get("last_evidence_key"),
        detail={"maturity": st.get("maturity"), "speed_grade": st.get("speed_grade"),
                "p80_ms": st.get("p80_ms")},
    )


def _transition(conn, st: dict, to_state: str, *, now: str, reason: str | None,
                before_score, recovery: bool = False, replaced_node_id: int | None = None,
                replace_reason: str | None = None) -> int:
    from_state = st.get("state")
    if from_state == to_state:
        st["last_reason"] = reason
        return 0
    st["state"] = to_state
    st["last_change_at"] = now
    st["last_reason"] = reason
    if to_state == "candidate":
        st["candidate_since"] = now
        st["preferred_since"] = None
        st["eliminated_since"] = None
        st["recovery_required"] = False
        st["consecutive_failures"] = 0
        st["consecutive_successes"] = 0
        st["advantage_count"] = 0
        st["advantage_target_id"] = None
    elif to_state == "preferred":
        st["preferred_since"] = now
        st["candidate_since"] = None
        st["eliminated_since"] = None
        st["recovery_required"] = False
        st["consecutive_failures"] = 0
        st["consecutive_successes"] = 0
        st["advantage_count"] = 0
        st["advantage_target_id"] = None
    elif to_state == "observation":
        st["observation_started_at"] = now
        st["observation_until"] = now
        st["candidate_since"] = None
        st["preferred_since"] = None
        st["eliminated_since"] = None
        st["recovery_required"] = bool(recovery)
        st["consecutive_failures"] = 0
        st["consecutive_successes"] = 0
        st["advantage_count"] = 0
        st["advantage_target_id"] = None
    elif to_state == "eliminated":
        st["eliminated_since"] = now
        st["candidate_since"] = None
        st["preferred_since"] = None
        st["recovery_required"] = False
        st["consecutive_failures"] = 0
        st["consecutive_successes"] = 0
        st["advantage_count"] = 0
        st["advantage_target_id"] = None
    _history(conn, st, from_state=from_state, to_state=to_state,
             before_score=before_score, reason=reason, now=now,
             replaced_node_id=replaced_node_id, replace_reason=replace_reason)
    return 1


def _weakest_preferred(conn, category: str, candidate: dict) -> dict | None:
    rows = conn.execute(
        """SELECT s.*,n.current_name,n.first_name
           FROM np5_state s JOIN np_nodes n ON n.id=s.node_id
           LEFT JOIN airports a ON a.id=n.airport_id
           WHERE s.category=? AND s.state='preferred' AND s.node_id<>?
             AND n.source_active=1 AND COALESCE(a.enabled,1)=1
             AND COALESCE(s.maturity,0)>=70
           ORDER BY COALESCE(s.score,-1), s.preferred_since, s.node_id""",
        (category, int(candidate["node_id"])),
    ).fetchall()
    for row in rows:
        target = dict(row)
        if _float(target.get("score")) is None:
            continue
        target["admission"] = db.loads(target.get("admission_json"), {}) or {}
        target["detail"] = db.loads(target.get("detail_json"), {}) or {}
        target["recovery_required"] = bool(target.get("recovery_required"))
        return target
    return None


def _find_replacement(conn, category: str, candidate: dict, *, gap: int) -> dict | None:
    """The weakest mature preferred that the candidate can actually overtake."""
    target = _weakest_preferred(conn, category, candidate)
    if target is None:
        return None
    score = _float(target.get("score"))
    if score is not None and float(candidate.get("score") or -1) >= score + float(gap):
        return target
    return None


def _promote_with_replacement(conn, candidate: dict, target: dict | None, *, now: str,
                              gap: int, reason: str | None = None) -> int:
    changes = 0
    if target is not None:
        target_before = target.get("score")
        target_from = target.get("state")
        target["state"] = "observation"
        target["preferred_since"] = None
        target["candidate_since"] = None
        target["observation_started_at"] = now
        target["observation_until"] = now
        target["recovery_required"] = True
        target["consecutive_failures"] = 0
        target["consecutive_successes"] = 0
        target["last_change_at"] = now
        target["last_reason"] = "replaced_by_new_node"
        target["updated_at"] = now
        store.save_state(conn, target)
        store.record_change(
            conn, at=now, node_id=int(target["node_id"]), category=target["category"],
            from_state=target_from, to_state="observation", before_score=target_before,
            after_score=target.get("score"), admission=target.get("admission") or {},
            reason="replaced_by_new_node", replaced_node_id=int(candidate["node_id"]),
            replace_reason="new_node_led_by_gap_and_two_confirmations",
            config_version=int(target.get("config_version") or 1),
            evidence_key=target.get("last_evidence_key"),
            detail={"gap": gap, "new_node": int(candidate["node_id"])},
        )
        changes += 1
    changes += _transition(
        conn, candidate, "preferred", now=now,
        reason=(reason or ("candidate_promoted" if target is None
                           else "candidate_replaced_mature")),
        before_score=candidate.get("_before_score"),
        replaced_node_id=int(target["node_id"]) if target is not None else None,
        replace_reason=("mature_yields_after_gap_and_two_confirmations"
                        if target is not None else None),
    )
    return changes


def _process(conn, node: dict, st: dict, category: str, cfg: dict, ev: dict,
             now: datetime, now_text: str) -> int:
    old_key = st.get("last_evidence_key")
    before_state = st.get("state")
    before_score = st.get("score")
    st["_before_score"] = before_score
    st["score"] = ev.get("score")
    st["maturity"] = ev.get("maturity")
    st["p80_ms"] = ev.get("p80_ms")
    st["delay_grade"] = ev.get("delay_grade") or "none"
    st["speed_grade"] = ev.get("speed_grade") or "empty"
    st["speed_mb_s"] = ev.get("speed_mb_s")
    st["last_evaluated_at"] = now_text
    st["last_evidence_key"] = ev["key"]
    st["config_version"] = int(cfg["config_version"])
    st["updated_at"] = now_text
    st["detail"] = {
        "quality_at": ev.get("quality_at"),
        "p80_ms": ev.get("p80_ms"),
        "delay_grade": ev.get("delay_grade"),
        "delay_label": ev.get("delay_label"),
        "speed_at": ev.get("speed_at"),
        "business_state": ev.get("business_state"),
        "business_expired": ev.get("business_expired"),
    }
    admission = _admission(ev, category)
    st["admission"] = admission
    st["admission_ok"] = bool(admission["ok"])
    st["admission_reason"] = _reason(admission)
    changes = 0

    # Time-based observation decisions may change with no new evidence.  A
    # duplicate evidence key must never add a second "successful evaluation".
    duplicate = old_key == ev["key"] and old_key is not None
    if duplicate and (st["state"] in ("candidate", "preferred", "eliminated")
                      or (st["state"] == "observation" and st.get("recovery_required"))):
        st["last_reason"] = st.get("last_reason") or "duplicate_evidence"
        store.save_state(conn, st)
        return 0

    if st["state"] == "eliminated":
        if admission["ok"] and node.get("source_active"):
            changes += _transition(conn, st, "observation", now=now_text,
                                   reason="source_readmitted", before_score=before_score,
                                   recovery=True)
        else:
            st["last_reason"] = _reason(admission) or "source_inactive"
        store.save_state(conn, st)
        return changes

    if st["state"] == "observation":
        if not node.get("source_active") or not node.get("airport_enabled", True):
            changes += _transition(conn, st, "eliminated", now=now_text,
                                   reason="source_inactive", before_score=before_score)
            store.save_state(conn, st)
            return changes
        if st.get("recovery_required"):
            if admission["ok"]:
                st["consecutive_successes"] = int(st.get("consecutive_successes") or 0) + 1
                if st["consecutive_successes"] >= int(cfg["recovery_successes"]):
                    changes += _transition(conn, st, "candidate", now=now_text,
                                           reason="recovery_confirmed",
                                           before_score=before_score)
                else:
                    st["last_reason"] = "recovery_confirming"
            elif admission["hard"]:
                st["consecutive_successes"] = 0
                st["last_reason"] = _reason(admission)
            else:
                st["last_reason"] = _reason(admission) or "recovery_waiting_data"
            store.save_state(conn, st)
            return changes
        until = store._parse(st.get("observation_until")) or now
        if now < until:
            st["last_reason"] = "observation_period"
            store.save_state(conn, st)
            return 0
        if admission["ok"]:
            changes += _transition(conn, st, "candidate", now=now_text,
                                   reason="observation_complete", before_score=before_score)
            if (st.get("score") or 0) >= float(cfg["candidate_score"]):
                # This observation-complete evaluation is the first candidate
                # sample; the next distinct evidence sample is the confirmation.
                st["consecutive_successes"] = 1
                weakest = _weakest_preferred(conn, category, st)
                if weakest is not None:
                    target_score = _float(weakest.get("score")) or 0
                    if (st.get("score") or 0) >= target_score + float(cfg["replace_gap"]):
                        st["advantage_target_id"] = int(weakest["node_id"])
                        st["advantage_count"] = 1
        else:
            st["last_reason"] = _reason(admission) or "observation_not_mature"
        store.save_state(conn, st)
        return changes

    if not node.get("source_active") or not node.get("airport_enabled", True):
        changes += _transition(conn, st, "eliminated", now=now_text,
                               reason="source_inactive", before_score=before_score)
        store.save_state(conn, st)
        return changes

    if st["state"] == "candidate":
        if ev.get("business_expired"):
            changes += _transition(conn, st, "observation", now=now_text,
                                   reason="business_expired", before_score=before_score,
                                   recovery=True)
            store.save_state(conn, st)
            return changes
        if admission["hard"]:
            changes += _transition(conn, st, "observation", now=now_text,
                                   reason=_reason(admission), before_score=before_score,
                                   recovery=True)
            store.save_state(conn, st)
            return changes
        if not admission["ok"]:
            st["last_reason"] = _reason(admission) or "candidate_pending_data"
            store.save_state(conn, st)
            return 0
        if (st.get("score") or 0) < float(cfg["candidate_score"]):
            st["consecutive_successes"] = 0
            st["advantage_count"] = 0
            st["advantage_target_id"] = None
            st["last_reason"] = "below_candidate_score"
            store.save_state(conn, st)
            return 0
        st["consecutive_successes"] = int(st.get("consecutive_successes") or 0) + 1
        weakest = _weakest_preferred(conn, category, st)
        if weakest is not None:
            target_id = int(weakest["node_id"])
            target_score = _float(weakest.get("score")) or 0
            if (st.get("score") or 0) >= target_score + float(cfg["replace_gap"]):
                if st.get("advantage_target_id") != target_id:
                    st["advantage_target_id"] = target_id
                    st["advantage_count"] = 0
                st["advantage_count"] = int(st.get("advantage_count") or 0) + 1
            else:
                st["advantage_target_id"] = target_id
                st["advantage_count"] = 0
        else:
            st["advantage_target_id"] = None
            st["advantage_count"] = 0
        if st["consecutive_successes"] < 2:
            st["last_reason"] = "candidate_confirming"
            store.save_state(conn, st)
            return 0
        if weakest is not None:
            if st["advantage_count"] >= 2:
                changes += _promote_with_replacement(conn, st, weakest, now=now_text,
                                                     gap=int(cfg["replace_gap"]))
            elif st["advantage_count"] > 0:
                # The replacement gap has been reached once.  Do not replace
                # yet; wait for the second distinct confirmation.  This only
                # delays a replacement, never a normal promotion without one.
                st["last_reason"] = "replacement_confirming"
            else:
                # A qualified candidate that does not lead the weakest mature
                # preferred by the replacement gap must still be promoted.  It
                # joins the preferred list without displacing anyone: there is
                # no fixed quota, and mature nodes are kept.
                changes += _promote_with_replacement(
                    conn, st, None, now=now_text, gap=int(cfg["replace_gap"]),
                    reason="candidate_promoted_without_replacement")
            store.save_state(conn, st)
            return changes
        changes += _promote_with_replacement(conn, st, None, now=now_text,
                                             gap=int(cfg["replace_gap"]))
        store.save_state(conn, st)
        return changes

    if st["state"] == "preferred":
        if ev.get("business_expired"):
            changes += _transition(conn, st, "observation", now=now_text,
                                   reason="business_expired", before_score=before_score,
                                   recovery=True)
            store.save_state(conn, st)
            return changes
        retention = _admission(ev, category, min_score=float(cfg["retain_score"]))
        if retention["ok"]:
            st["consecutive_failures"] = 0
            st["consecutive_successes"] = 0
            st["last_reason"] = "retained"
            store.save_state(conn, st)
            return 0
        # A speed-danger result is a hard prohibition, not an ordinary score
        # fluctuation: the node cannot remain in the preferred list.
        if "speed_danger" in (retention.get("hard") or []):
            changes += _transition(conn, st, "observation", now=now_text,
                                   reason="speed_danger", before_score=before_score,
                                   recovery=True)
            store.save_state(conn, st)
            return changes
        if retention.get("pending"):
            # Unknown, expired target or a missing quality sample is never a
            # consecutive failure.  Keep the last valid state and wait.
            st["last_reason"] = _reason(retention) or "retention_pending_data"
            store.save_state(conn, st)
            return 0
        st["consecutive_failures"] = int(st.get("consecutive_failures") or 0) + 1
        st["consecutive_successes"] = 0
        if st["consecutive_failures"] >= int(cfg["failure_limit"]):
            changes += _transition(conn, st, "observation", now=now_text,
                                   reason="retain_failed", before_score=before_score,
                                   recovery=True)
        else:
            st["last_reason"] = "retain_watch"
        store.save_state(conn, st)
        return changes

    store.save_state(conn, st)
    return 0


def _run_category(category: str, cfg: dict, at: datetime) -> dict:
    changes = 0
    scanned = 0
    errors: list[str] = []
    with db.transaction() as conn:
        store.ensure_schema(conn)
        store.ensure_defaults(conn)
        quality_store.ensure_schema(conn)
        business_store.ensure_schema(conn)
        store.ensure_states(conn, now=at)
        rows = conn.execute(
            """SELECT s.* FROM np5_state s
               JOIN np_nodes n ON n.id=s.node_id
               LEFT JOIN airports a ON a.id=n.airport_id
               WHERE s.category=?
               ORDER BY s.node_id""",
            (category,),
        ).fetchall()
        for row in rows:
            st = dict(row)
            st["admission"] = db.loads(st.pop("admission_json"), {}) or {}
            st["detail"] = db.loads(st.pop("detail_json"), {}) or {}
            st["recovery_required"] = bool(st.get("recovery_required"))
            node = conn.execute(
                """SELECT n.*,a.enabled AS airport_enabled,a.name AS airport_name
                   FROM np_nodes n LEFT JOIN airports a ON a.id=n.airport_id
                   WHERE n.id=?""", (int(st["node_id"]),)
            ).fetchone()
            if node is None:
                continue
            node = dict(node)
            ev = _evidence(node, category, conn=conn)
            scanned += 1
            if ev is None:
                # Missing evidence must not overwrite the previous valid state.
                continue
            ev["node"] = node
            try:
                conn.execute("SAVEPOINT p5_node")
                changes += _process(conn, node, st, category, cfg, ev, at, _stamp(at))
                conn.execute("RELEASE p5_node")
            except Exception as exc:  # one node must not abort the category
                conn.execute("ROLLBACK TO p5_node")
                conn.execute("RELEASE p5_node")
                errors.append(f"node {node['id']}: {type(exc).__name__}: {exc}")
    return {"category": category, "nodes_scanned": scanned, "changes": changes,
            "errors": errors}


def run(*, trigger: str = "manual", categories: list[str] | None = None,
        now: datetime | None = None, refresh_p4: bool = True,
        _ignore_lock: bool = False) -> dict:
    """Run one competition pass.  A busy pass returns ``duplicate`` safely."""
    if not _ignore_lock and not _run_lock.acquire(blocking=False):
        with db.transaction() as conn:
            store.record_event(conn, "run_skipped", reason="duplicate_run",
                               detail={"trigger": trigger})
        return {"ok": False, "duplicate": True, "run_id": None,
                "changes": 0, "trigger": trigger}
    at = now or datetime.now()
    selected = [c for c in (categories or list(store.CATEGORIES)) if c in store.CATEGORIES]
    selected = selected or list(store.CATEGORIES)
    try:
        with db.transaction() as conn:
            store.ensure_schema(conn)
            store.ensure_defaults(conn)
            cfg = store.get_settings(conn)
            run_id = store.start_run(conn, trigger=trigger, categories=selected, now=at)
        if refresh_p4:
            try:
                # Refresh P4 once, from existing P1–P4 evidence.  It does not
                # re-run P2/P3 probes and must not be repeated per category.
                quality_service.ensure_evaluations(force=False)
            except Exception as exc:
                _log(f"P5 刷新 P4 评价失败，使用现有证据：{type(exc).__name__}: {exc}")
        results = []
        total_scanned = 0
        total_changes = 0
        total_errors = 0
        for category in selected:
            try:
                result = _run_category(category, cfg, at)
            except Exception as exc:  # category rollback preserves its last valid state
                result = {"category": category, "nodes_scanned": 0, "changes": 0,
                          "errors": [f"{type(exc).__name__}: {exc}"]}
            results.append(result)
            total_scanned += int(result["nodes_scanned"])
            total_changes += int(result["changes"])
            total_errors += len(result["errors"])
        status = "success" if not total_errors else "partial"
        with db.transaction() as conn:
            store.finish_run(
                conn, run_id, status=status, nodes_scanned=total_scanned,
                changes=total_changes,
                message=(None if not total_errors else f"{total_errors} 个节点/类别异常，已保留旧结果"),
                detail={"categories": results, "errors": total_errors}, now=at,
            )
            store.record_event(conn, "run_finished", reason=trigger,
                               detail={"run_id": run_id, "changes": total_changes,
                                       "status": status})
        _log(f"P5 竞争完成：{trigger}，{total_scanned} 节点/类别，状态变化 {total_changes}"
             + (f"，异常 {total_errors}" if total_errors else ""))
        return {"ok": True, "duplicate": False, "run_id": run_id, "status": status,
                "changes": total_changes, "nodes_scanned": total_scanned,
                "errors": results, "trigger": trigger,
                "at": _stamp(at)}
    finally:
        _run_lock.release()


def due(*, now: datetime | None = None) -> bool:
    cfg = store.get_settings()
    last = store.last_run()
    if not last:
        return True
    stamp = last.get("finished_at") or last.get("started_at")
    parsed = store._parse(stamp)
    if not parsed:
        return True
    return (now or datetime.now()) >= parsed + timedelta(minutes=int(cfg["cycle_minutes"]))


def _log(message: str) -> None:
    try:
        db.append_log("INFO", "竞争", message)
    except Exception:
        pass
