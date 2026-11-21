"""Regression: subscriptions must be reachable without any query string."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-subroute-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")

from fastapi.testclient import TestClient  # noqa: E402
from backend.database import db  # noqa: E402
from backend.main import app  # noqa: E402
from backend.api import auth  # noqa: E402
from backend.subscriptions import builder  # noqa: E402


def test_queryless_route():
    db.init_db()
    builder.publish(reason="test")
    token = auth.sub_token()
    client = TestClient(app)
    for fmt in ("clash", "singbox", "base64"):
        r = client.get(f"/sub/all/{token}/{fmt}")
        assert r.status_code == 200, (fmt, r.status_code, r.text[:100])
    r = client.get(f"/sub/all/{token}")
    assert r.status_code == 200
    r = client.get("/sub/all/bad-token/clash")
    assert r.status_code == 403


if __name__ == "__main__":
    test_queryless_route()
    print("PASS test_queryless_route")
