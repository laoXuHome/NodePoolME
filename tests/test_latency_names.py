"""Latency targets are user-named; the pool UI abbreviates them by first character."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-latency-names-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.scheduler import manager  # noqa: E402


def test_names_are_dynamic():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        res = client.patch("/api/settings", json={
            "latency_test_name": "谷歌", "latency_test_name2": "维基百科",
            "latency_test_url": "https://example.com/a.ico",
            "latency_test_url2": "https://example.com/b.ico",
        })
        assert res.status_code == 200, res.text
        assert db.get("latency_test_name") == "谷歌"
        assert db.get("latency_test_url") == "https://example.com/a.ico"

        probes = manager.latency_probes()
        assert probes[0]["label"] == "谷歌" and probes[0]["short"] == "谷"
        assert probes[1]["label"] == "维基百科" and probes[1]["short"] == "维"
        # internal target keys stay stable so history keeps working after a rename
        assert [p["ident"] for p in probes] == ["G", "W"]

        nodes = client.get("/api/nodes").json()
        assert nodes["latency_labels"]["G"] == {"name": "谷歌", "short": "谷"}
        assert nodes["latency_labels"]["W"]["name"] == "维基百科"

        # empty name falls back to the built-in label instead of producing an empty chip
        client.patch("/api/settings", json={"latency_test_name": "", "latency_test_name2": ""})
        assert manager.latency_probes()[0]["label"] == "Google"
        assert manager.latency_probes()[1]["short"] == "W"


if __name__ == "__main__":
    test_names_are_dynamic()
    print("PASS latency target names")
