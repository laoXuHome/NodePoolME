"""Shared test isolation: every test starts from an empty database."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TABLES = (
    "settings", "airports", "fetch_logs", "nodes", "airport_filtered_nodes",
    "test_groups", "results", "latency_results", "share_history", "schedule_history", "batches", "run_events",
    "logs", "speed_sources", "speed_source_checks", "speed_runs", "speed_samples",
)
_P1_TABLES = (
    "np_fetch_batches", "np_node_qualifications", "np_node_events", "np_node_names",
    "np_nodes", "np_meta",
)
_P2_TABLES = (
    "np2_events", "np2_queue", "np2_tcp_samples", "np2_stage_results",
    "np2_target_results", "np2_checks", "np2_node_state", "np2_target_checks",
    "np2_targets",
)
_P3_TABLES = (
    "np3_events", "np3_queue", "np3_stages", "np3_checks", "np3_state",
    "np3_target_checks", "np3_targets",
)
_ORIGINAL_ENGINE = None


@pytest.fixture(autouse=True)
def clean_database():
    global _ORIGINAL_ENGINE

    from backend.database import db, pool
    from backend.testers import engine as engine_mod

    # v1.10 §2.7 写入闸是进程级状态：每个测试开始前必须回到放行态
    pool.reset_for_tests()

    if _ORIGINAL_ENGINE is None:
        _ORIGINAL_ENGINE = engine_mod.engine_manager
    db.reset_connection()
    db.init_db()
    # P1 tables are created lazily. Drop them so every test still starts from
    # the frozen baseline structure (migration/backup tests depend on that).
    for table in _P1_TABLES + _P2_TABLES + _P3_TABLES:
        db.execute(f'DROP TABLE IF EXISTS "{table}"')
    for table in _TABLES:
        db.execute(f"DELETE FROM {table}")
    db.execute("DELETE FROM sqlite_sequence WHERE name IN (%s)" % ",".join(f"'{t}'" for t in _TABLES))
    db.reset_connection()
    db.init_db()
    yield
    engine_mod.engine_manager = _ORIGINAL_ENGINE
