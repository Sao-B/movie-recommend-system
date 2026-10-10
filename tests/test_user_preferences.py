"""用户偏好（类型问卷）与"不感兴趣"标记的集成测试——临时 SQLite 库。

覆盖：
- ``PUT /api/profile/info`` 的 ``favorite_genres`` 校验（1..5 个、标准类型、
  空数组 = 明确跳过）；``needs_onboarding`` 的 NULL / '[]' 语义；
- ``POST/DELETE/GET /api/dislikes``（幂等、未知电影 404）；
- 推荐链路把"不感兴趣"当硬排除（ItemCF 服务层）；
- 冷启动兜底按偏好类型加权 + 排除"不感兴趣"。
"""
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app                                      # noqa: E402
from app.db import db                                           # noqa: E402
from app.itemcf_service import ItemCFService                    # noqa: E402
from app.models import Movie, User                              # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch        # noqa: E402

ACCOUNT = "pref_user"


class _TestConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "user-pref-test-key-32bytes!!"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True


class FavoriteGenresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="user_pref_")
        _TestConfig.SQLALCHEMY_DATABASE_URI = (
            "sqlite:///" + (Path(cls._tmpdir.name) / "pref.db").as_posix()
        )
        with sqlite_integer_pk_patch():
            cls.app = create_app(_TestConfig)
            with cls.app.app_context():
                db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls._tmpdir.cleanup()

    def setUp(self):
        with self.app.app_context():
            db.session.query(User).delete()
            db.session.commit()
        registered = self.client.post(
            "/api/auth/register", json={"username": ACCOUNT, "password": "pw123456"}
        ).get_json()
        self.user_id = registered["user"]["id"]
        self.headers = {"Authorization": "Bearer " + registered["token"]}

    # ---------- 首次引导语义 ----------

    def test_new_user_needs_onboarding(self):
        info = self.client.get("/api/profile/info", headers=self.headers).get_json()
        self.assertIsNone(info["favorite_genres"])
        self.assertTrue(info["needs_onboarding"])

    def test_save_genres_clears_onboarding(self):
        resp = self.client.put("/api/profile/info", json={
            "favorite_genres": ["Sci-Fi", "Action"]}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["favorite_genres"], ["Sci-Fi", "Action"])
        self.assertFalse(data["needs_onboarding"])

    def test_empty_list_means_explicit_skip(self):
        resp = self.client.put("/api/profile/info", json={
            "favorite_genres": []}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["favorite_genres"], [])
        self.assertFalse(data["needs_onboarding"])   # 跳过过就不再弹

    # ---------- 校验 ----------

    def test_reject_more_than_five(self):
        resp = self.client.put("/api/profile/info", json={
            "favorite_genres": ["Action", "Comedy", "Drama", "Horror",
                                 "Romance", "Sci-Fi"]}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_reject_unknown_genre(self):
        resp = self.client.put("/api/profile/info", json={
            "favorite_genres": ["武侠"]}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未知类型", resp.get_json()["error"])

    def test_reject_non_list(self):
        resp = self.client.put("/api/profile/info", json={
            "favorite_genres": "Sci-Fi"}, headers=self.headers)
        self.assertEqual(resp.status_code, 400)

    def test_at_least_one_when_provided_list_is_untouched(self):
        """不带字段 = 维持原值（None 语义不被空数组意外覆盖的反向确认）。"""
        self.client.put("/api/profile/info", json={
            "favorite_genres": ["Sci-Fi"]}, headers=self.headers)
        resp = self.client.put("/api/profile/info", json={
            "bio": "hi"}, headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["favorite_genres"], ["Sci-Fi"])


class DislikeTests(unittest.TestCase):
    """"不感兴趣"：幂等标记/取消 + 未知电影 404。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="user_dislike_")
        _TestConfig.SQLALCHEMY_DATABASE_URI = (
            "sqlite:///" + (Path(cls._tmpdir.name) / "dislike.db").as_posix()
        )
        with sqlite_integer_pk_patch():
            cls.app = create_app(_TestConfig)
            with cls.app.app_context():
                db.create_all()
                db.session.add(Movie(movie_id=318, title="Shawshank"))
                db.session.commit()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls._tmpdir.cleanup()

    def setUp(self):
        with self.app.app_context():
            db.session.query(User).delete()
            db.session.commit()
        registered = self.client.post(
            "/api/auth/register", json={"username": "dislike_user",
                                        "password": "pw123456"}).get_json()
        self.headers = {"Authorization": "Bearer " + registered["token"]}

    def test_add_list_remove_cycle(self):
        resp = self.client.post("/api/dislikes/", json={"movie_id": 318},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 201)
        # 幂等：重复标记不报错
        resp = self.client.post("/api/dislikes/", json={"movie_id": 318},
                                headers=self.headers)
        self.assertIn(resp.status_code, (200, 201))
        listing = self.client.get("/api/dislikes/", headers=self.headers).get_json()
        # items 带片名/年份/类型（个人主页偏好卡片直接渲染，不用逐个查详情）；
        # 测试库里这部电影只填了 title，year/genres 走空值分支
        self.assertEqual(listing, {
            "count": 1,
            "movie_ids": [318],
            "items": [{"movie_id": 318, "title": "Shawshank",
                       "year": None, "genres": []}],
        })
        resp = self.client.delete("/api/dislikes/318", headers=self.headers)
        self.assertEqual(resp.status_code, 200)
        listing = self.client.get("/api/dislikes/", headers=self.headers).get_json()
        self.assertEqual(listing["count"], 0)

    def test_add_unknown_movie_404(self):
        resp = self.client.post("/api/dislikes/", json={"movie_id": 999999},
                                headers=self.headers)
        self.assertEqual(resp.status_code, 404)

    def test_requires_auth(self):
        resp = self.client.post("/api/dislikes/", json={"movie_id": 318})
        self.assertEqual(resp.status_code, 401)


class _FakeMovie:
    def __init__(self, movie_id, avg_rating):
        self.movie_id = movie_id
        self.avg_rating = avg_rating

    def to_dict(self):
        return {"movie_id": self.movie_id, "avg_rating": self.avg_rating}


class _FakeRepo:
    """冷启动兜底的假仓储：热门池 + 偏好类型 + 不感兴趣集合。"""

    def __init__(self, popular, genres=None, dislikes=None):
        self.popular = popular
        self.genres = genres
        self.dislikes = dislikes or set()

    def get_popular_movies(self, limit, min_rating_count=50, genres=None):
        rows = self.popular
        if genres:
            rows = [m for m in rows if getattr(m, "_genres", ()) and
                    set(getattr(m, "_genres", ())) & set(genres)]
        return rows[:limit]

    def get_user_dislikes(self, user_id):
        return self.dislikes

    def get_user_favorite_genres(self, user_id):
        return self.genres


class PopularFallbackTests(unittest.TestCase):
    """冷启动兜底：类型加权 + 不感兴趣排除。"""

    def _service(self, genres, dislikes):
        from app.recommendation_service import OnlineRecommendationService
        popular = [
            _FakeMovie(1, 4.5), _FakeMovie(2, 4.4), _FakeMovie(3, 4.3),
            _FakeMovie(4, 4.2), _FakeMovie(5, 4.1),
        ]
        for m, g in zip(popular, (["Sci-Fi"], ["Drama"], ["Sci-Fi"],
                                   ["Romance"], ["Drama"])):
            m._genres = g
        return OnlineRecommendationService(
            _FakeRepo(popular, genres=genres, dislikes=dislikes), top_n=3)

    def test_genre_weighted_popular(self):
        """偏好 Sci-Fi：Sci-Fi 池（1,3）排前，不足 top_n 由全局热门补位（2）。"""
        recs = self._service(["Sci-Fi"], set()).get_popular_for_user(1)
        self.assertEqual([r["movie"]["movie_id"] for r in recs], [1, 3, 2])

    def test_dislikes_excluded(self):
        recs = self._service(None, {1}).get_popular_for_user(1)
        ids = [r["movie"]["movie_id"] for r in recs]
        self.assertNotIn(1, ids)

    def test_genre_pool_fills_from_global(self):
        """类型池不足 top_n 时补全局热门（排除已选）。"""
        recs = self._service(["Romance"], set()).get_popular_for_user(1)
        ids = [r["movie"]["movie_id"] for r in recs]
        self.assertEqual(ids[0], 4)          # Romance 池只有 4
        self.assertEqual(len(ids), 3)        # 不足部分由全局热门补齐


class ItemCFDislikeExclusionTests(unittest.TestCase):
    """ItemCF 服务层把"不感兴趣"当硬排除。"""

    def test_disliked_candidates_excluded(self):
        """ItemCF 服务层把"不感兴趣"当硬排除（用迷你真实模型目录）。"""
        import tempfile
        from tests.test_itemcf_online import write_model

        class _Repo:
            def __init__(self):
                self.ratings = [type("R", (), {"movie_id": 101, "rating": 5.0})()]
                self.dislikes = {103}

            def get_user_ratings(self, user_id):
                return self.ratings

            def get_user_dislikes(self, user_id):
                return self.dislikes

            def get_movies(self, movie_ids):
                return {m: {"movie_id": m} for m in movie_ids}

        with tempfile.TemporaryDirectory() as tmp:
            write_model(tmp, item_ids=[101, 102, 103, 109],
                        edges=[(101, 102, 0.9), (101, 103, 0.1), (101, 109, 0.8)],
                        n_like=[1, 1, 1, 1], n_all=[1, 1, 1, 1])
            service = ItemCFService(_Repo(), tmp, top_n=5)
            recs = service.recommendations_for(1)
            # Windows 下 mmap 打开的 npy 会锁住临时目录，先释放再让 cleanup 删
            service._graph = None
            import gc
            gc.collect()
        ids = [r["movie"]["movie_id"] for r in recs]
        self.assertIn(102, ids)
        self.assertNotIn(103, ids)   # 被标记不感兴趣 → 硬排除


if __name__ == "__main__":
    unittest.main()
