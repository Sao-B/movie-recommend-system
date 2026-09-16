"""recommendations 路由的集成测试——不依赖真实 ALS 因子库。

路由的行为面很小但边界重要：
- JWT 必须：无 token / 坏 token 一律 401；
- 服务取自 ``app.extensions["recommendation_service"]`` 单例，测试时注入假服务；
- 有推荐 → 200 + ``user_id`` / ``count`` / ``data`` 透传；
- 无推荐 → 200 + 提示语 + 空列表（降级态不算错误）；
- identity 从 JWT 来且被转成 int（用户 ID 命名空间约定：正数才是系统用户）。
"""
import sys
import unittest
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app                                  # noqa: E402
from flask_jwt_extended import create_access_token          # noqa: E402


class _TestConfig:
    # 路由本身不查库，但 create_app 会初始化 SQLAlchemy 扩展，需要给个 URI
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "rec-route-test-key-32bytes-long!!!"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True


class _FakeService:
    def __init__(self, recs):
        self.recs = recs
        self.calls = []

    def get_for_user(self, user_id):
        self.calls.append(user_id)
        return self.recs


_FAKE_RECS = [
    {"movie_id": 318, "title": "The Shawshank Redemption", "predicted_rating": 4.8, "rank": 1},
    {"movie_id": 858, "title": "The Godfather", "predicted_rating": 4.6, "rank": 2},
]


class RecommendationsRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = create_app(_TestConfig)
        cls.client = cls.app.test_client()

    def setUp(self):
        self.service = _FakeService(_FAKE_RECS)
        self.app.extensions["recommendation_service"] = self.service
        with self.app.app_context():
            self.token = create_access_token(identity="42")
        self.headers = {"Authorization": f"Bearer {self.token}"}

    def test_missing_token_is_401(self):
        response = self.client.get("/api/recommendations/")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.service.calls, [])

    def test_invalid_token_is_401(self):
        response = self.client.get(
            "/api/recommendations/", headers={"Authorization": "Bearer not-a-token"}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.service.calls, [])

    def test_returns_recommendations_with_user_id(self):
        response = self.client.get("/api/recommendations/", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["user_id"], 42)
        self.assertEqual(body["count"], 2)
        self.assertEqual(body["data"], _FAKE_RECS)
        # identity 从 JWT 来，且已转成 int
        self.assertEqual(self.service.calls, [42])

    def test_empty_recommendations_is_a_200_with_message(self):
        self.service.recs = []
        response = self.client.get("/api/recommendations/", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIn("msg", body)
        self.assertEqual(body["data"], [])

    def test_route_uses_the_extension_singleton(self):
        """路由取的是 create_app 装进去的那个服务实例，而不是自己 new 一个。"""
        marker = _FakeService(_FAKE_RECS)
        self.app.extensions["recommendation_service"] = marker
        self.client.get("/api/recommendations/", headers=self.headers)
        self.assertEqual(marker.calls, [42])


if __name__ == "__main__":
    unittest.main()
