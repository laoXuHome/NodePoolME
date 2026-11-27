import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.test_maturity_rating as helper  # noqa: E402  （先导入以设置测试环境变量）
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api.routes import score_grade_of  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402


@pytest.mark.parametrize("shown,grade", [
    (100, "优"), (80, "优"), (79.99, "良"), (60, "良"), (59.99, "中"),
    (40, "中"), (39.99, "差"), (20, "差"), (19.99, "劣"), (0, "劣"),
])
def test_grade_boundaries(shown, grade):
    assert score_grade_of(shown) == grade


def test_dashboard_uses_actual_score_without_bonus():
    db.init_db()
    db.execute("DELETE FROM nodes")
    helper._seed({back: helper._half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        row = helper._row(client)
    raw = row["raw_score"]
    assert row["score"] == int(round(raw))
    assert row["score_exact"] == round(raw, 2)
    assert row["score_grade"] == score_grade_of(raw)
    assert "bonus_score" not in row


def test_dashboard_orders_by_exact_actual_score():
    db.init_db()
    helper._seed({back: helper._half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        data = client.get("/api/dashboard?days=7").json()
    scores = [a["score_exact"] for a in data["airport_list"] if a["score_exact"] is not None]
    assert scores == sorted(scores, reverse=True)
