"""Regression: groups share one global timeout and one global cadence."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-group-global-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from backend.database import db  # noqa: E402
from backend.scheduler import manager  # noqa: E402


def test_group_timeout_is_global():
    db.init_db()
    db.set_setting("latency_test_timeout", 5)
    for g in manager.due_groups():
        assert g["timeout"] == 5, g
        assert "interval_minutes" in g  # column kept for compatibility, but unused


def test_all_enabled_groups_run_every_cycle():
    db.init_db()
    db.execute("UPDATE test_groups SET last_run_at=?", (db.now(),))
    assert len(manager.due_groups()) == len(manager.due_groups(force=True))


if __name__ == "__main__":
    test_group_timeout_is_global()
    test_all_enabled_groups_run_every_cycle()
    print("PASS group global timeout/cadence")
