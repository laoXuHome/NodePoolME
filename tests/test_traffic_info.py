"""Subscription-Userinfo traffic/expiry metadata is parsed and exposed to the UI."""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-traffic-")
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


def test_parse_userinfo():
    assert fetcher.parse_subscription_userinfo(
        "upload=1024; download=2048; total=4096; expire=1798761600") == {
            "upload": 1024, "download": 2048, "total": 4096, "expire": 1798761600}
    assert fetcher.parse_subscription_userinfo(
        " upload = 1.5 ; download=2 ; total=0 ; expire=1798761600000 ") == {
            "upload": 1, "download": 2, "total": 0, "expire": 1798761600}
    assert fetcher.parse_subscription_userinfo("download=10") == {"download": 10}
    assert fetcher.parse_subscription_userinfo("") is None
    assert fetcher.parse_subscription_userinfo("garbage") is None


def test_fetch_stores_and_exposes_traffic():
    yaml = """proxies:
  - name: 香港 01
    type: ss
    server: 1.1.1.1
    port: 443
    cipher: aes-128-gcm
    password: pw
"""
    async def fake_download(_url):
        return yaml, {"upload": 100, "download": 300, "total": 1000,
                      "expire": 1798761600}

    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        created = client.post("/api/airports", json={
            "name": "流量机场", "url": "https://example.com/sub"}).json()
        aid = created["id"]
        assert created["traffic_available"] is False

        with patch.object(fetcher, "_download", fake_download):
            result = asyncio.run(fetcher.fetch_airport(fetcher.airport_by_id(aid), "manual"))
        assert result["ok"] is True

        row = db.row("SELECT * FROM airports WHERE id=?", (aid,))
        assert row["traffic_upload"] == 100 and row["traffic_download"] == 300
        assert row["traffic_total"] == 1000 and row["traffic_expire"] == 1798761600
        assert row["traffic_updated_at"]

        payload = next(a for a in client.get("/api/airports").json() if a["id"] == aid)
        assert payload["traffic_available"] is True
        assert payload["traffic_used"] == 400
        assert payload["traffic_percent"] == 40
        assert payload["traffic_expire"] == 1798761600


if __name__ == "__main__":
    test_parse_userinfo()
    print("PASS test_parse_userinfo")
    test_fetch_stores_and_exposes_traffic()
    print("PASS test_fetch_stores_and_exposes_traffic")
