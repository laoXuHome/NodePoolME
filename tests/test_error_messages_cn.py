"""Common user-facing API errors must be Chinese (frontend pops them verbatim)."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = tempfile.mkdtemp(prefix="np-msg-")
os.environ["NODEPOOL_DATA_DIR"] = _TMP
os.environ["NODEPOOL_CONFIG_DIR"] = os.path.join(_TMP, "config")
os.environ["NODEPOOL_LOG_DIR"] = os.path.join(_TMP, "logs")
os.environ["NODEPOOL_BACKUP_DIR"] = os.path.join(_TMP, "backup")
os.environ["NODEPOOL_ADMIN_PASSWORD"] = "test123"
os.environ["NODEPOOL_TEST_ENGINE"] = "tcp"

from fastapi.testclient import TestClient  # noqa: E402
from backend.main import app  # noqa: E402
from backend.parsers.subscription import parse_text_full  # noqa: E402


def test_error_messages_are_chinese():
    with TestClient(app) as client:
        wrong = client.post("/api/auth/login", json={"password": "bad"})
        assert wrong.status_code == 401
        assert wrong.json()["detail"] == "密码错误"

        assert client.post("/api/auth/login", json={"password": "test123"}).status_code == 200
        airport = client.post("/api/airports", json={"name": "提示测试", "url": "http://x"})
        assert airport.status_code == 200
        duplicate = client.post("/api/airports", json={"name": "提示测试", "url": "http://x"})
        assert duplicate.json()["detail"] == "机场名称已被使用"
        assert client.get("/api/nodes/99999").json()["detail"] == "节点不存在"
        assert client.get("/api/airports/99999/filter").json()["detail"] == "机场不存在"
        assert client.delete("/api/groups/99999").json()["detail"] == "检测组不存在"

    nodes, errors, _ = parse_text_full("proxies:\n  a: b")
    assert nodes == [] and errors == ["YAML 中没有 proxies 列表"]
    nodes, errors, _ = parse_text_full("")
    assert nodes == [] and errors == ["订阅内容为空"]


if __name__ == "__main__":
    test_error_messages_are_chinese()
    print("PASS test_error_messages_are_chinese")
