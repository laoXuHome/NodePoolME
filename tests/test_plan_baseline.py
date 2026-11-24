"""升级上来的旧库没有计划记录：补推定基线，覆盖率不再整周空白。"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-baseline-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402


def _seed_legacy(days=3, interval=30):
    for table in ("schedule_history", "share_history", "batches", "airports"):
        db.execute(f"DELETE FROM {table}")
    db.set_setting("test_interval_minutes", interval)
    created = (datetime.now() - timedelta(days=8)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute(
        "INSERT INTO airports (name, url, enabled, interval_minutes, created_at, updated_at)"
        " VALUES ('老机场','http://x',1,60,?,?)", (created, created))
    airport_id = db.row("SELECT id FROM airports")["id"]
    start = datetime.now() - timedelta(days=days)
    db.execute(
        "INSERT INTO batches (started_at, finished_at, status, target_count, processed_count, published)"
        " VALUES (?,?, 'complete', 1, 1, 1)",
        (start.strftime("%Y-%m-%d %H:%M:%S"), start.strftime("%Y-%m-%d %H:%M:%S")))
    stamp = start
    end = datetime.now()
    while stamp < end:
        db.execute(
            "INSERT INTO share_history (ts, batch_id, total_nodes, total_published, total_available,"
            " airport_id, pool_count, published_count, available_count, available_known)"
            " VALUES (?, 1, 10, 8, 8, ?, 10, 8, 8, 1)",
            (stamp.strftime("%Y-%m-%d %H:%M:%S"), airport_id))
        stamp += timedelta(minutes=interval)


def test_legacy_db_gets_estimated_baseline():
    _seed_legacy()
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        data = client.get("/api/dashboard").json()
        row = data["airport_list"][0]
        assert row["plan_estimated"] is True
        assert row["coverage"] is not None and row["coverage"] >= 90
        # 推定计划不能进正式排名，最高只给参考
        assert row["data_status"] == "reference"
        assert row["rank_tier"] == 1
        assert "推定" in row["data_status_detail"]
        assert len(db.rows("SELECT * FROM schedule_history")) == 1


def test_recorded_plan_is_not_marked_estimated():
    _seed_legacy()
    db.execute("INSERT INTO schedule_history (changed_at, interval_minutes, source)"
               " VALUES (?, 30, 'settings')",
               ((datetime.now() - timedelta(days=9)).strftime("%Y-%m-%d %H:%M:%S"),))
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        row = client.get("/api/dashboard").json()["airport_list"][0]
        assert row["plan_estimated"] is False
        assert row["data_status"] == "normal"
        assert "推定" not in row["data_status_detail"]


if __name__ == "__main__":
    test_legacy_db_gets_estimated_baseline()
    test_recorded_plan_is_not_marked_estimated()
    print("PASS plan baseline")
