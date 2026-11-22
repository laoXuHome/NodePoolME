"""Settings API: both latency ceilings must persist (target 2 used to be dropped by the schema)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-settings-caps-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402


def test_both_latency_caps_roundtrip():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        assert client.patch("/api/settings", json={
            "sub_max_latency": 777,
            "sub_max_latency2": 555,
        }).status_code == 200
        assert db.get("sub_max_latency") == "777"
        assert db.get("sub_max_latency2") == "555"
        settings = client.get("/api/settings").json()["settings"]
        assert settings["sub_max_latency"] == "777"
        assert settings["sub_max_latency2"] == "555"

        # Simulate a restart: migrations must not overwrite user-customized ceilings.
        assert client.patch("/api/settings", json={
            "sub_max_latency": 500,
            "sub_max_latency2": 555,
        }).status_code == 200
        db.init_db()
        settings = client.get("/api/settings").json()["settings"]
        assert settings["sub_max_latency"] == "500", settings
        assert settings["sub_max_latency2"] == "555", settings


if __name__ == "__main__":
    test_both_latency_caps_roundtrip()
    print("PASS both latency caps persist")
