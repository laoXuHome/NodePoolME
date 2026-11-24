import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.test_maturity_rating as helper  # noqa: E402  （先导入以设置测试环境变量）
from fastapi.testclient import TestClient  # noqa: E402
from backend.api.routes import SCORE_WEIGHTS  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402


def test_weights_exclude_pool_share_and_sum_to_hundred():
    assert SCORE_WEIGHTS == {"available": 60, "publish": 35, "tag": 5}
    assert "share" not in SCORE_WEIGHTS
    assert sum(SCORE_WEIGHTS.values()) == 100


def test_score_ignores_pool_share():
    db.init_db()
    helper._seed({back: helper._half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        row = helper._row(client)
    expected = round(
        SCORE_WEIGHTS["available"] / 100 * row["available_rate"]
        + SCORE_WEIGHTS["publish"] / 100 * row["publish_rate"]
        + SCORE_WEIGHTS["tag"] / 100 * row["tag_rate"]
    )
    assert abs(row["raw_score"] - expected) <= 1
    # 池占比无论多少都不进入评分
    assert row["share_rate"] >= 0


def test_dashboard_exposes_new_weights():
    db.init_db()
    helper._seed({back: helper._half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        data = client.get("/api/dashboard?days=7").json()
    assert data["score_weights"] == {"available": 60, "publish": 35, "tag": 5}
