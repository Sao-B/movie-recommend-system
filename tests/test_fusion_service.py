"""V4 三路融合服务测试——临时 SQLite 库 + 合成 ItemCF 图 + 合成 ALS 因子。

覆盖：三路信号归一化与加权融合、缺失信号退出加权（重归一化）、
ALS 支持度门控、类型匹配、已看/不感兴趣硬排除、因子版本缓存。
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_itemcf_online import write_model            # noqa: E402
from app.fusion_service import FusionService                # noqa: E402


class _Rating:
    def __init__(self, movie_id, rating):
        self.movie_id = movie_id
        self.rating = rating


class _Movie:
    def __init__(self, movie_id, title, genres, rating_count):
        self.movie_id = movie_id
        self.title = title
        self.genres = genres
        self.rating_count = rating_count

    def to_dict(self):
        return {"movie_id": self.movie_id, "title": self.title,
                "genres": [g for g in (self.genres or "").split("|") if g]}


class _FactorRow:
    def __init__(self, movie_id, factors):
        self.movie_id = movie_id
        self.factors = factors


class _Model:
    def __init__(self, version, rank, reg_param=0.1):
        self.version = version
        self.rank = rank
        self.reg_param = reg_param


class _Repo:
    """合成仓储：评分/不感兴趣/偏好/电影（含 rating_count）/ALS 因子。"""

    def __init__(self, ratings, dislikes, fav_genres, movies, factors_by_version):
        self.ratings = ratings            # {user_id: [(movie_id, rating)]}
        self.dislikes = dislikes          # set(movie_id)
        self.fav_genres = fav_genres      # list[str] | None
        self.movies = movies              # {movie_id: _Movie}
        self.factors = factors_by_version  # {version: {movie_id: [float]}}
        self.active = _Model("als-test", 2)

    def get_user_ratings(self, user_id):
        return [_Rating(m, r) for m, r in self.ratings.get(user_id, [])]

    def get_user_dislikes(self, user_id):
        return set(self.dislikes)

    def get_user_favorite_genres(self, user_id):
        return self.fav_genres

    def get_active_model(self):
        return self.active

    def get_model_factors(self, version):
        # v 已是 JSON 字符串（与线上 model_item_factors.factors 同构），不再编码
        return [_FactorRow(m, v)
                for m, v in sorted(self.factors[version].items())]

    def get_movies(self, movie_ids):
        return {m: self.movies[m] for m in movie_ids if m in self.movies}

    def get_movies_above_support(self, min_rating_count):
        return {m for m, mv in self.movies.items()
                if mv.rating_count >= min_rating_count}

    def get_movie_genres(self, movie_id):
        mv = self.movies.get(movie_id)
        if mv is None or not mv.genres:
            return set()
        return {g for g in mv.genres.split("|") if g}


def json_vec(values):
    import json
    return json.dumps(values)


class _FakeItemCF:
    """只提供 _load_graph 的最小 ItemCF 替身。"""

    def __init__(self, model_dir):
        self.model_dir = model_dir

    def _load_graph(self):
        from app.itemcf_service import ItemCFGraph
        return ItemCFGraph(self.model_dir, verify_hashes=False)


class FusionServiceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # ItemCF 图：101 好评 → 102(0.9) / 103(0.1)；104/105 无证据
        self.model_dir = write_model(
            self._tmp.name + "/graph", item_ids=[101, 102, 103, 104, 105],
            edges=[(101, 102, 0.9), (101, 103, 0.1)],
            n_like=[1, 1, 1, 1, 1], n_all=[1, 1, 1, 1, 1])
        # ALS 因子（rank=2）：102 与 105 强、104 中、103 弱
        self.als_factors = {
            102: [1.0, 0.0], 105: [0.9, 0.1], 104: [0.5, 0.5], 103: [0.1, 0.9],
        }
        self.movies = {
            101: _Movie(101, "Seed", "Sci-Fi", 5000),
            102: _Movie(102, "Hit sequel", "Sci-Fi", 5000),
            103: _Movie(103, "Niche drama", "Drama", 5000),
            104: _Movie(104, "Mid movie", "Action", 5000),
            105: _Movie(105, "Hidden gem", "Action|Sci-Fi", 5000),
        }
        factors = {"als-test": dict((m, json_vec(v))
                                    for m, v in self.als_factors.items())}
        self.repo = _Repo(
            ratings={1: [(101, 5.0)]},
            dislikes=set(),
            fav_genres=["Sci-Fi"],
            movies=self.movies,
            factors_by_version=factors,
        )
        self.itemcf = _FakeItemCF(self.model_dir)
        self.service = FusionService(
            self.itemcf, self.repo, weights=(0.4, 0.5, 0.1),
            als_min_support=1000, als_top_k=500, top_n=5)

    def tearDown(self):
        import gc
        self.service = None
        gc.collect()
        self._tmp.cleanup()

    def test_fused_order_and_signals(self):
        """用户好评 101：R1 证据 102/103，R2 门控 ALS 候选 102/104/105。

        als 分：102=1.0, 105=0.9·? — 因子点积归一后 102 最高。
        融合后 102 应第一（三路全占），103 垫底（itemcf 弱+无 als+类型不符）。
        """
        recs = self.service.recommendations_for(1)
        ids = [r["movie"]["movie_id"] for r in recs]
        self.assertIn(102, ids)
        self.assertNotIn(101, ids)          # 已看排除
        self.assertEqual(recs[0]["movie"]["movie_id"], 102)
        # 信号字段存在且在 [0,1]
        for r in recs:
            for key in ("itemcf", "als", "genre"):
                v = r["signals"][key]
                if v is not None:
                    self.assertGreaterEqual(v, 0.0)
                    self.assertLessEqual(v, 1.0)

    def test_als_gate_excludes_low_support(self):
        """门控：rating_count < 1000 的电影不进 ALS 候选（此处全部 >= 1000，
        通过临时调低阈值验证门控路径本身）。"""
        self.service.als_min_support = 10 ** 9   # 门到全关
        recs = self.service.recommendations_for(1)
        # ALS 路全缺 → 只剩 ItemCF 信号（102 强 103 弱）
        self.assertEqual([r["movie"]["movie_id"] for r in recs], [102, 103])
        self.assertIsNone(recs[0]["signals"]["als"])

    def test_dislike_hard_excluded(self):
        self.repo.dislikes = {102}
        recs = self.service.recommendations_for(1)
        ids = [r["movie"]["movie_id"] for r in recs]
        self.assertNotIn(102, ids)

    def test_genre_match_boosts(self):
        """类型匹配信号：103 是 Drama（与 Sci-Fi 偏好不符），
        在纯 genre 视角下必排在类型匹配的候选之后。"""
        self.service.w_itemcf, self.service.w_als = 0.0, 0.0   # 只看类型
        self.service.w_genre = 1.0
        recs = self.service.recommendations_for(1)
        ids = [r["movie"]["movie_id"] for r in recs]
        # 103 (Drama) 类型匹配 0 分，必须排最后或缺席
        self.assertEqual(ids[-1], 103)

    def test_no_positive_history_returns_empty(self):
        self.repo.ratings = {1: [(103, 2.0)]}
        self.assertEqual(self.service.recommendations_for(1), [])

    def test_factor_cache_per_version(self):
        self.service.recommendations_for(1)
        self.service.recommendations_for(1)
        self.assertEqual(list(self.service._factor_cache.keys()), ["als-test"])


if __name__ == "__main__":
    unittest.main()
