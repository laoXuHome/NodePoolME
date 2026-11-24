"""Settings API: boolean payloads must land as "1"/"0" so the scheduler sees them."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-settings-bool-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

import asyncio  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402


def test_bool_setting_roundtrip():
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        fav = client.get("/favicon.ico")
        # 构建产物里带了 favicon.ico 就返回图标，没带才回 204（两种情况都不能是 HTML 兜底）
        assert fav.status_code in (200, 204), fav
        assert fav.headers.get("content-type", "").split(";")[0] in ("image/x-icon", "image/vnd.microsoft.icon", "")
        assert "text/html" not in fav.headers.get("content-type", "")
        assert client.patch("/api/settings", json={
            "sub_publish_enabled": True,
            "sub_publish_mode": "interval",
            "sub_publish_interval_minutes": 60,
        }).status_code == 200
        assert db.get("sub_publish_enabled") == "1"
        dash = client.get("/api/dashboard").json()
        assert dash["publish_schedule"]["enabled"] is True, dash["publish_schedule"]
        assert dash["publish_schedule"]["mode"] == "interval"
        assert dash["publish_schedule"]["interval_minutes"] == 60
        next_at = dash["publish_schedule"]["next_at"]
        assert next_at, "saving the schedule must arm the next run immediately"
        from datetime import datetime, timedelta
        nxt = datetime.strptime(next_at[:19], "%Y-%m-%d %H:%M:%S")
        assert timedelta(minutes=55) < (nxt - datetime.now()) <= timedelta(minutes=61), next_at

        assert client.patch("/api/settings", json={"test_on_start": False}).status_code == 200
        assert db.get("test_on_start") == "0"
        settings = client.get("/api/settings").json()["settings"]
        assert settings["test_on_start"] == "0"
        client.patch("/api/settings", json={"test_on_start": True})
        assert db.get("test_on_start") == "1"

        client.patch("/api/settings", json={"sub_publish_enabled": False})
        assert db.get("sub_publish_enabled") == "0"
        assert client.get("/api/dashboard").json()["publish_schedule"]["next_at"] is None
        assert client.get("/api/dashboard").json()["publish_schedule"]["enabled"] is False


def test_favicon_never_falls_back_to_html():
    from backend.main import favicon, favicon_svg
    res = asyncio.run(favicon())
    assert res.status_code in (200, 204)
    if res.status_code == 204:
        assert getattr(res, "body", b"") == b""
    svg = asyncio.run(favicon_svg())
    assert svg.status_code == 200
    assert svg.media_type == "image/svg+xml"


def test_legacy_true_string_is_migrated():
    db.init_db()
    db.set_setting("sub_publish_enabled", "True")
    db.init_db()   # migration on startup normalises it
    assert db.get("sub_publish_enabled") == "1", db.get("sub_publish_enabled")


if __name__ == "__main__":
    test_bool_setting_roundtrip()
    print("PASS test_bool_setting_roundtrip")
    test_legacy_true_string_is_migrated()
    print("PASS test_legacy_true_string_is_migrated")
    test_favicon_204_has_no_body()
    print("PASS favicon 204 no body")
