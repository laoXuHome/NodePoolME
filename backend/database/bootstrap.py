"""Startup maintenance/DB classification (P-1.8 D3 + F-1…F-6).

This module is deliberately read-only until :func:`apply_startup_gate` is called.
It must never call ``init_db()``, ``ensure_schema_table_transactional()`` or
``record_baseline()``; the caller is responsible for doing that only after a
verified ``INACTIVE`` result.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from backend import config
from backend.database import gate, maint, pool
from backend.database.maint import MaintRead, MaintState
from backend.database.migrate import Phase

KNOWN_PHASES = {p.value for p in Phase}
FALLBACK_CODES = {
    "F-1": "unknown_phase",
    "F-2": "phase_db_mismatch",
    "F-3": "maintenance_file_invalid",
    "F-4": "db_rows_unexpected",
    "F-5": "probe_untrusted",
    "F-6": "lock_unavailable",
}


class StartupProbeError(RuntimeError):
    """The read-only probe could not establish a trustworthy state (F-5)."""


class StartupGateError(RuntimeError):
    """The unconditional startup write-block could not be confirmed."""


@dataclass
class DbState:
    kind: str                    # absent | schema_absent | baseline_only | baseline_running | baseline_failed | baseline_success | invalid
    exists: bool = False
    rows: list[dict[str, Any]] = field(default_factory=list)
    tables: int | None = None
    detail: str | None = None


@dataclass
class StartupDecision:
    state: MaintState
    code: str
    phase: str | None
    db_kind: str
    detail: str | None = None
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def inactive(self) -> bool:
        return self.state is MaintState.INACTIVE


def _phase_of(blob: dict[str, Any] | None) -> str | None:
    if not blob:
        return None
    value = blob.get("phase")
    return value if isinstance(value, str) else None


def _valid_phase(phase: str | None) -> bool:
    return phase is not None and phase in KNOWN_PHASES


_BASELINE = f"{config.VERSION}-baseline"


def probe_db(path: str | Path | None = None) -> DbState:
    """Read schema_migrations through a read-only handle.

    ``mode=ro`` is intentionally not used: it cannot see committed-but-not-
    checkpointed WAL pages.  ``mode=rw`` + ``PRAGMA query_only=1`` sees the live
    WAL content while rejecting writes at the SQLite layer.  If the database file
    is absent we do not create it.
    """
    target = Path(path or config.DB_PATH)
    if not target.exists():
        return DbState(kind="absent", exists=False)
    try:
        conn = sqlite3.connect(f"file:{target}?mode=rw", uri=True, timeout=10)
        try:
            conn.execute("PRAGMA query_only=1")
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
            ).fetchone() is not None
            if not has_table:
                tables = conn.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
                return DbState(kind="schema_absent", exists=True, tables=int(tables))
            rows = conn.execute(
                "SELECT migration_id, version, seq, status FROM schema_migrations ORDER BY seq"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise StartupProbeError(str(exc)) from exc

    parsed = [{"migration_id": r[0], "version": r[1], "seq": r[2], "status": r[3]}
              for r in rows]
    kind = _classify_rows(parsed)
    return DbState(kind=kind, exists=True, rows=parsed, tables=None)


def _classify_rows(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "invalid"
    if not all(isinstance(r.get("seq"), int) and isinstance(r.get("version"), str)
               and isinstance(r.get("status"), str) for r in rows):
        return "invalid"
    base = rows[0]
    if not (base["seq"] == 1 and base["version"] == _BASELINE and base["status"] == "success"):
        return "invalid"
    if len(rows) == 1:
        return "baseline_only"
    # Legal second row is exactly seq 2 / version 1.10 with running|success|failed.
    if len(rows) != 2:
        return "invalid"
    second = rows[1]
    if second["seq"] != 2 or second["version"] != "1.10":
        return "invalid"
    status = second["status"]
    if status == "running":
        return "baseline_running"
    if status == "success":
        return "baseline_success"
    if status == "failed":
        return "baseline_failed"
    return "invalid"


def _specific_code(phase: str | None, db_kind: str) -> str | None:
    """Known phase/DB pair names, or ``None`` when no defined pair matches."""
    if phase is None:
        return None
    if phase == Phase.PREPARED.value and db_kind == "baseline_only":
        return "interrupted_before_tx"
    if phase == Phase.TX_OPEN.value and db_kind == "baseline_only":
        return "interrupted_in_tx"
    if phase == Phase.TX_ROLLED_BACK.value and db_kind == "baseline_only":
        return "rolled_back_without_failed_row"
    if phase == Phase.COMMIT_ATTEMPTED.value and db_kind == "baseline_only":
        return "commit_not_applied_or_rolled_back"
    if phase in (Phase.TX_OPEN.value, Phase.COMMIT_ATTEMPTED.value,
                 Phase.TX_ROLLED_BACK.value) and db_kind == "baseline_failed":
        return "migration_failed"
    if phase == Phase.COMMIT_ATTEMPTED.value and db_kind == "baseline_running":
        return "committed_not_finalized"
    if phase in (Phase.COMMIT_ATTEMPTED.value, Phase.COMMITTED.value) \
            and db_kind == "baseline_success":
        return "success_pending_release"
    if phase == Phase.UNKNOWN.value:
        return "outcome_unknown"
    return None


def classify(read: maint.MaintReadResult, db: DbState) -> StartupDecision:
    """Apply the sixth-round table plus explicit F-1…F-6 fallback rules.

    The application startup path has no legal ``ACTIVE`` outcome.  A present
    maintenance file means the previous process either is still running (the
    process-lifetime lock would have refused startup) or died mid-operation.
    In either case every defined pair is ``RECOVERY_REQUIRED``; an unmatched
    pair falls back to ``stale_maintenance``.  ``INACTIVE`` is reachable only
    from a verified clean database or an explicitly released COMMITTED state.
    """
    # F-3: the file was present but could not be decoded/validated.
    if read.status is MaintRead.CORRUPT or read.status is MaintRead.UNREADABLE:
        return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-3"],
                               None, db.kind, "maintenance file corrupt/unreadable", db.rows)

    blob = read.blob if read.status is MaintRead.PRESENT else None
    phase = _phase_of(blob)

    # F-1: unknown phase spelling/type.  Check phase before the general field
    # validator so an unknown phase is reported as F-1 rather than F-3.
    if read.status is MaintRead.PRESENT and phase is not None and not _valid_phase(phase):
        return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-1"],
                               phase, db.kind, "unknown phase", db.rows)

    if read.status is MaintRead.PRESENT:
        if not maint.maint_fields_sane(blob):
            return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-3"],
                                   phase, db.kind, "maintenance fields invalid", db.rows)
        # Strict boolean: "false"/"true"/0/1/null/missing are not a maintenance
        # state and must never be coerced through bool().  F-3.
        if type(blob.get("maintenance")) is not bool:
            return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-3"],
                                   phase, db.kind, "maintenance flag is not boolean", db.rows)

    # F-5 is raised by probe_db; reaching here means the probe result is usable.
    if db.kind == "invalid":
        return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-4"],
                               phase, db.kind, "database row set is not a legal set", db.rows)

    specific = _specific_code(phase, db.kind)

    if read.status is MaintRead.ABSENT:
        # Only a clean DB may be released.  DB_ABSENT / no schema table are the
        # first-deploy cases; baseline-only and baseline+success are clean.
        if db.kind in ("absent", "schema_absent"):
            return StartupDecision(MaintState.INACTIVE, "first_deploy", None,
                                   db.kind, None, db.rows)
        if db.kind == "baseline_only":
            return StartupDecision(MaintState.INACTIVE, "db_clean_no_maint", None,
                                   db.kind, None, db.rows)
        if db.kind == "baseline_success":
            return StartupDecision(MaintState.INACTIVE, "db_migrated_no_maint", None,
                                   db.kind, None, db.rows)
        if specific:
            return StartupDecision(MaintState.RECOVERY_REQUIRED, specific, None,
                                   db.kind, None, db.rows)
        return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-2"],
                               None, db.kind, "no maintenance file, but DB is not clean", db.rows)

    # PRESENT and structurally valid.
    maintenance = blob["maintenance"]

    if maintenance is True:
        # Any present maintenance file at process startup is stale/abandoned:
        # the process lock already proved no live application owns it.  Defined
        # phase/DB pairs (including COMMITTED + success) still require manual
        # recovery; undefined pairs get stale_maintenance.
        if specific:
            return StartupDecision(MaintState.RECOVERY_REQUIRED, specific, phase,
                                   db.kind, None, db.rows)
        return StartupDecision(MaintState.RECOVERY_REQUIRED, "stale_maintenance", phase,
                               db.kind, "no defined phase/DB pair", db.rows)

    if maintenance is False:
        # A released maintenance file is a convenience record, not authority:
        # it can only be INACTIVE when the DB independently proves COMMITTED.
        if phase == Phase.COMMITTED.value and db.kind == "baseline_success":
            return StartupDecision(MaintState.INACTIVE, "maint_released_db_success",
                                   phase, db.kind, None, db.rows)
        if specific:
            return StartupDecision(MaintState.RECOVERY_REQUIRED, specific, phase,
                                   db.kind, None, db.rows)
        return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-2"], phase,
                               db.kind, "maintenance=false but phase/DB do not match", db.rows)


def apply_startup_gate(decision: StartupDecision) -> None:
    """Release only a verified INACTIVE decision; otherwise keep writes blocked.

    A failure to confirm the block is fatal.  Never log-and-continue: an
    unconfirmed gate would let the business layer write during recovery.
    """
    if decision.state is MaintState.INACTIVE:
        gate.release()
        if pool.writes_blocked():
            raise StartupGateError("INACTIVE decision but write gate is still blocked")
        return
    gate.apply_state(decision.state)
    pool.block_writes(pool.REASON_RECOVERY)
    ok, detail = pool.confirm_writes_blocked()
    if not ok:
        raise StartupGateError(f"write block not confirmed: {detail}")


def inspect_startup(db_path: str | Path | None = None) -> StartupDecision:
    # D3 step 2: read the maintenance file before touching the DB.  Step 3:
    # establish AND confirm the write gate before the read-only schema probe.
    read = maint.read_maint_file()
    gate.apply_state(MaintState.RECOVERY_REQUIRED)
    pool.block_writes(pool.REASON_RECOVERY)
    ok, detail = pool.confirm_writes_blocked()
    if not ok:
        raise StartupGateError(f"startup write block not confirmed: {detail}")
    try:
        db = probe_db(db_path)
    except StartupProbeError as exc:
        return StartupDecision(MaintState.RECOVERY_REQUIRED, FALLBACK_CODES["F-5"],
                               _phase_of(read.blob if read.status is MaintRead.PRESENT else None),
                               "untrusted", str(exc))
    return classify(read, db)


__all__ = ["DbState", "StartupDecision", "StartupGateError", "StartupProbeError",
           "apply_startup_gate", "classify", "inspect_startup", "probe_db"]
