"""Contract test: dashboard payload must always carry latency_probe (the UI dereferences it)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-dashboard-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402


def test_dashboard_has_latency_probe():
    with TestClient(app) as client:
        login = client.post("/api/auth/login", json={"password": "test123"})
        assert login.status_code == 200
        res = client.get("/api/dashboard")
        assert res.status_code == 200, res.text[:500]
        data = res.json()
        probe = data.get("latency_probe")
        assert isinstance(probe, dict), data.keys()
        assert {"enabled", "url", "status", "ceiling", "passed"} <= set(probe)
        assert isinstance(data["nodes_qualified"], int)
        runtime = data["runtime"]
        for key in ("group_progress", "block_index", "blocks", "block_size"):
            assert key in runtime, runtime.keys()
        assert isinstance(runtime["group_progress"], list)
        assert {"total", "ok", "error", "manual_only", "next_at"} <= set(data["fetch"].keys())
        assert {"count", "hits", "passed"} <= set(data["group_summary"].keys())
        assert {"last_auto", "nodes"} <= set(data["publish"].keys())
        assert isinstance(data.get("share_history"), list)
        for point in data.get("share_history", []):
            assert "total_available" in point
            for airport_row in point.get("airports", {}).values():
                assert "available_count" in airport_row
        for airport in data.get("airport_list", []):
            assert {"available_count", "unavailable_count", "score", "score_grade"} <= set(airport)


if __name__ == "__main__":
    test_dashboard_has_latency_probe()
    print("PASS test_dashboard_has_latency_probe")
