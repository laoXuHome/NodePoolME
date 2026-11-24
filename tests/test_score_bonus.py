import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tests.test_maturity_rating as helper  # noqa: E402  （先导入以设置测试环境变量）
import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from backend.api.routes import bonus_score, final_score, score_grade_of  # noqa: E402
from backend.main import app  # noqa: E402
from backend.database import db  # noqa: E402


@pytest.mark.parametrize("raw,bonus,final", [
    (10, 1.0, 11.0),
    (30, 5.67, 35.67),
    (50, 10.33, 60.33),
    (54, 11.27, 65.27),
    (60, 12.67, 72.67),
    (70, 15.0, 85.0),
    (85, 15.0, 100.0),
    (100, 15.0, 100.0),
])
def test_bonus_table(raw, bonus, final):
    assert round(bonus_score(raw), 2) == bonus
    assert round(final_score(raw), 2) == final


@pytest.mark.parametrize("raw,expected", [(0, 1.0), (5, 1.0), (10, 1.0), (100, 15.0), (120, 15.0)])
def test_bonus_clamped(raw, expected):
    assert bonus_score(raw) == expected


@pytest.mark.parametrize("shown,grade", [
    (85, "优"), (100, "优"), (84.99, "良"), (70, "良"), (69.99, "中"),
    (55, "中"), (54.99, "差"), (40, "差"), (39.99, "劣"), (0, "劣"),
])
def test_grade_boundaries(shown, grade):
    assert score_grade_of(shown) == grade


def test_bonus_applied_exactly_once_in_dashboard():
    db.init_db()
    db.execute("DELETE FROM nodes")
    helper._seed({back: helper._half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        row = helper._row(client)
    raw = row["raw_score"]
    assert row["score"] == int(round(final_score(raw)))
    assert row["score_exact"] == round(final_score(raw), 2)
    # 若重复追加，展示分会等于 final_score(最终分)，这里必须不相等
    assert row["score_exact"] != round(final_score(final_score(raw)), 2)
    assert row["score_grade"] == score_grade_of(final_score(raw))


def test_dashboard_orders_by_exact_final_score():
    db.init_db()
    helper._seed({back: helper._half_hour_day() for back in range(0, 7)})
    with TestClient(app) as client:
        client.post("/api/auth/login", json={"password": "test123"})
        data = client.get("/api/dashboard?days=7").json()
    scores = [a["score_exact"] for a in data["airport_list"] if a["score_exact"] is not None]
    assert scores == sorted(scores, reverse=True)
