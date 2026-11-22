"""Every parsed entry is cached, even when 60 entries share one endpoint/config."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-nodedup-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.services import fetcher  # noqa: E402


def _yaml(names):
    lines = ["proxies:"]
    for name in names:
        lines += [f"  - name: {name}", "    type: ss", "    server: 1.1.1.1",
                  "    port: 443", "    cipher: aes-128-gcm", "    password: pw"]
    return "\n".join(lines) + "\n"


def test_sixty_same_config_nodes_are_all_kept():
    names = [f"香港节点{i:02d}" for i in range(1, 61)]
    async def fake_download(_url):
        return _yaml(names), {}

    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        aid = client.post("/api/airports", json={
            "name": "六十节点机场", "url": "https://example.com/sub"}).json()["id"]

        with patch.object(fetcher, "_download", fake_download):
            result = asyncio.run(fetcher.fetch_airport(fetcher.airport_by_id(aid), "manual"))

        assert result["ok"] is True
        assert result["inserted"] == 60, result
        assert result["total"] == 60, result
        assert db.row("SELECT COUNT(*) AS c FROM nodes WHERE airport_id=?", (aid,))["c"] == 60
        assert db.row("SELECT COUNT(DISTINCT fingerprint) AS c FROM nodes WHERE airport_id=?",
                      (aid,))["c"] == 60
        payload = next(a for a in client.get("/api/airports").json() if a["id"] == aid)
        assert payload["node_count"] == 60

        log = db.row("SELECT message FROM fetch_logs WHERE airport_id=? ORDER BY id DESC LIMIT 1",
                     (aid,))
        assert "去重" not in log["message"]


if __name__ == "__main__":
    test_sixty_same_config_nodes_are_all_kept()
    print("PASS test_sixty_same_config_nodes_are_all_kept")
