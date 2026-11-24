"""Shared test isolation: every test starts from an empty database."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TABLES = (
    "settings", "airports", "fetch_logs", "nodes", "airport_filtered_nodes",
    "test_groups", "results", "latency_results", "share_history", "schedule_history", "batches", "run_events",
    "logs",
)
_ORIGINAL_ENGINE = None


@pytest.fixture(autouse=True)
def clean_database():
    global _ORIGINAL_ENGINE

    from backend.database import db
    from backend.testers import engine as engine_mod

    if _ORIGINAL_ENGINE is None:
        _ORIGINAL_ENGINE = engine_mod.engine_manager
    db.reset_connection()
    db.init_db()
    for table in _TABLES:
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)" % ",".join(f"'{t}'" for t in _TABLES))
    db.reset_connection()
    db.init_db()
    yield
    engine_mod.engine_manager = _ORIGINAL_ENGINE
