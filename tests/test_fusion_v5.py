"""V5 融合叠加层测试：评分映射、频率标签权重、ALS 负反馈罚项、热门降权、类型配额。

纯函数部分直接测 rating_weights；服务部分用合成 ItemCF 图 + 合成 ALS 因子
（与 test_fusion_service 同一套手法），V5 参数逐项开关验证。
"""
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_itemcf_online import write_model            # noqa: E402
from app.rating_weights import freq_weight, label_signal, rating_weight  # noqa: E402
from app.fusion_service import FusionService                # noqa: E402


class RatingWeightTests(unittest.TestCase):
    def test_rating_weight_piecewise(self):
        cases = {
            0.5: -1.2, 1.0: -1.2, 1.5: -1.2, 2.0: -1.0, 2.5: -0.5,
            3.0: 0.0, 3.5: 0.25, 4.0: 0.5, 4.5: 0.75, 5.0: 1.0,
        }
        for r, expected in cases.items():
            self.assertAlmostEqual(rating_weight(r), expected, places=6,
                                   msg=f"rating {r}")

    def test_freq_weight(self):
        self.assertEqual(freq_weight(0), 0.0)
        self.assertAlmostEqual(freq_weight(5), 0.5, places=6)
        self.assertGreater(freq_weight(6), 0.5)      # 6 以上进入稳定兴趣段
        self.assertAlmostEqual(freq_weight(50), 1.0, places=6)
        self.assertEqual(freq_weight(500), 1.0)      # 饱和

    def test_label_signal_self_calibrating(self):
        # EDA：高频高差评用户近期均分 ~3.06 -> 信号近 0；健康用户 3.74 -> +0.37
        self.assertAlmostEqual(label_signal(3.06), 0.03, places=6)
        self.assertAlmostEqual(label_signal(3.74), 0.37, places=6)
        self.assertEqual(label_signal(1.0), -1.0)    # 截断
        self.assertEqual(label_signal(6.0), 1.0)


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


class _V5Repo:
    """合成仓储（V5）：get_user_ratings 按给定顺序返回（头部=最近）。"""

    def __init__(self, ratings, movies, factors_by_version):
        self.ratings = ratings            # {user_id: [(movie_id, rating), ...] 最新在前}
        self.movies = movies              # {movie_id: _Movie}
        self.factors = factors_by_version
        # factors 为空 = 未启用 ALS 链路（无活模型）
        self.active = _Model("als-test", 2) if factors_by_version.get("als-test") else None

    def get_user_ratings(self, user_id):
        return [_Rating(m, r) for m, r in self.ratings.get(user_id, [])]

    def get_user_dislikes(self, user_id):
        return set()

    def get_user_favorite_genres(self, user_id):
        return []

    def get_active_model(self):
        return self.active

    def get_model_factors(self, version):
        return [_FactorRow(m, v) for m, v in sorted(self.factors[version].items())]

    def get_movies(self, movie_ids):
        return {m: self.movies[m] for m in movie_ids if m in self.movies}

    def get_movies_genres_map(self, movie_ids):
        out = {}
        for m in movie_ids:
            mv = self.movies.get(m)
            out[m] = ({g for g in (mv.genres or "").split("|") if g}
                      if mv else set())
        return out

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
    def __init__(self, model_dir):
        self.model_dir = model_dir

    def _load_graph(self):
        from app.itemcf_service import ItemCFGraph
        return ItemCFGraph(self.model_dir, verify_hashes=False)


class FusionV5Tests(unittest.TestCase):
    def _build(self, ratings, edges=None, factors=None, movies=None, **kwargs):
        if not isinstance(ratings, dict):
            ratings = {1: ratings}
        self._tmp = tempfile.TemporaryDirectory()
        item_ids = sorted({m for pair in ratings.values() for m, _ in pair}
                          | {m for e in (edges or []) for m in e[:2]})
        n = len(item_ids)
        self.model_dir = write_model(
            self._tmp.name + "/graph", item_ids=item_ids,
            edges=edges or [], n_like=[1] * n, n_all=[1] * n)
        self.movies = movies
        fv = {"als-test": dict((m, json_vec(v)) for m, v in (factors or {}).items())}
        self.repo = _V5Repo(ratings=ratings, movies=self.movies,
                            factors_by_version=fv)
        self.itemcf = _FakeItemCF(self.model_dir)
        base = dict(weights=(0.4, 0.5, 0.1), als_min_support=1000,
                    als_top_k=500, top_n=20, v5=True,
                    popularity_alpha=0.0, als_neg_lambda=0.0, genre_quota=0)
        base.update(kwargs)
        self.service = FusionService(self.itemcf, self.repo, **base)
        return self.service

    def tearDown(self):
        import gc
        self.service = None
        gc.collect()
        self._tmp.cleanup()

    def _default_movies(self):
        return {
            101: _Movie(101, "Seed", "Sci-Fi", 5000),
            102: _Movie(102, "Hit sequel", "Sci-Fi", 5000),
            103: _Movie(103, "Niche drama", "Drama", 5000),
            104: _Movie(104, "Action A", "Action", 5000),
            105: _Movie(105, "Bad sci-fi", "Sci-Fi", 5000),
            106: _Movie(106, "Drama seed", "Drama", 5000),
            107: _Movie(107, "Drama sequel", "Drama", 5000),
        }

    def test_v4_path_untouched_when_v5_off(self):
        """v5=False 时走 fav_genres 类型匹配（V4 口径）。"""
        ratings = {1: [(105, 1.0), (101, 5.0)]}   # 105 差评
        self._build(ratings, edges=[(101, 102, 0.9)],
                    factors={101: [1.0, 0.0], 102: [1.0, 0.0],
                             104: [0.0, 1.0], 105: [1.0, 0.0]},
                    movies=self._default_movies(), v5=False)
        recs = self.service.recommendations_for(1)
        ids = [r["movie"]["movie_id"] for r in recs]
        self.assertIn(102, ids)

    def test_als_negative_penalty_lowers_targeted_item(self):
        """用户差评 105（与 102 因子相同、与 104 近正交）：
        罚项后 102 的 als 信号从第一跌到 104 之后（原始分差 0.09 < 罚项 0.6）。"""
        ratings = {1: [(105, 1.0), (101, 5.0)]}
        factors = {101: [1.0, 0.0], 105: [1.0, 0.0],
                   102: [1.0, 0.0], 104: [0.97, 0.242]}
        self._build(ratings, edges=[(101, 102, 0.9)], factors=factors,
                    movies=self._default_movies(), als_neg_lambda=0.0)
        base = {r["movie"]["movie_id"]: r["signals"]["als"]
                for r in self.service.recommendations_for(1)}
        self._build(ratings, edges=[(101, 102, 0.9)], factors=factors,
                    movies=self._default_movies(), als_neg_lambda=0.5)
        penalized = {r["movie"]["movie_id"]: r["signals"]["als"]
                     for r in self.service.recommendations_for(1)}
        self.assertGreater(base[102], base[104])          # 无罚项：102 第一
        self.assertLess(penalized[102], penalized[104])   # 罚项后反超

    def test_popularity_rerank_only_penalizes(self):
        """alpha=1：热门候选被降权；低于中位数的候选不被放大（scale 钳 1）。"""
        movies = {
            101: _Movie(101, "Seed", "Sci-Fi", 5000),
            102: _Movie(102, "Blockbuster", "Sci-Fi", 5000),
            103: _Movie(103, "Obscure", "Drama", 10),
        }
        ratings = {1: [(101, 5.0)]}
        self._build(ratings, edges=[(101, 102, 0.9), (101, 103, 0.6)],
                    movies=movies, popularity_alpha=0.0)
        base_ids = [r["movie"]["movie_id"]
                    for r in self.service.recommendations_for(1)]
        self.assertEqual(base_ids, [102, 103])
        self._build(ratings, edges=[(101, 102, 0.9), (101, 103, 0.6)],
                    movies=movies, popularity_alpha=1.0)
        rerank_ids = [r["movie"]["movie_id"]
                      for r in self.service.recommendations_for(1)]
        self.assertEqual(rerank_ids, [103, 102])

    def test_genre_quota_caps_high_neg_label(self):
        """声明标签 Sci-Fi 近窗 3 条差评 -> 每页 Sci-Fi 最多 quota 部，
        非该类型候选（104，Action）不被挤掉。"""
        sci = [102, 109, 110, 111, 112, 113]          # 6 部 Sci-Fi 候选
        movies = self._default_movies()
        ratings = {1: [(113, 1.0), (112, 1.0), (111, 1.0),   # 最近 3 条差评
                       (101, 5.0)]}
        edges = [(101, m, 0.9) for m in sci] + [(101, 104, 0.8)]
        self._build(ratings, edges=edges, movies=movies,
                    popularity_alpha=0.0, genre_quota=3)
        recs = self.service.recommendations_for(1)
        sci_count = sum(1 for r in recs if "Sci-Fi" in r["movie"]["genres"])
        other = [r["movie"]["movie_id"] for r in recs
                 if "Sci-Fi" not in r["movie"]["genres"]]
        self.assertLessEqual(sci_count, 3)
        self.assertIn(104, other)                      # 非该类型不被挤掉

    def test_label_signal_self_calibrates_order(self):
        """标签路自校准：近期给 Sci-Fi 差评（108）的用户，Drama 候选 103 反超
        Sci-Fi 候选 102；近期好评的用户相反——频率权重相同，只有分数信号不同。"""
        movies = self._default_movies()
        movies[108] = _Movie(108, "Sci-fi seed B", "Sci-Fi", 5000)
        edges = [(101, 102, 0.9), (108, 102, 0.9),
                 (106, 103, 0.9), (107, 103, 0.9)]
        healthy = [(108, 5.0), (101, 5.0), (107, 4.5), (106, 5.0)]
        sour = [(108, 2.5), (101, 5.0), (107, 4.5), (106, 5.0)]
        self._build(healthy, edges=edges, movies=movies,
                    weights=(0.0, 0.0, 1.0))
        healthy_ids = [r["movie"]["movie_id"]
                       for r in self.service.recommendations_for(1)]
        self._build(sour, edges=edges, movies=movies,
                    weights=(0.0, 0.0, 1.0))
        sour_ids = [r["movie"]["movie_id"]
                    for r in self.service.recommendations_for(1)]
        self.assertEqual(healthy_ids[0], 102)   # Sci-Fi 近期均分 5.0 -> 信号 1.0
        self.assertEqual(sour_ids[0], 103)      # Sci-Fi 近期均分 3.75 -> 0.375，被 Drama 反超


if __name__ == "__main__":
    unittest.main()
