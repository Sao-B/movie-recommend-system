import sys
from pathlib import Path
import unittest

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.recommendation_core import (  # noqa: E402
    RATING_MAX,
    RATING_MIN,
    clamp_rating,
    effective_reg_param,
    rank_unrated_items,
    solve_user_vector,
)


class OnlineRecommenderTests(unittest.TestCase):
    def setUp(self):
        self.movie_ids = np.array([101, 102, 103, 104])
        self.item_factors = np.array(
            [
                [1.0, 0.0],
                [0.0, 1.0],
                [0.9, 0.1],
                [0.1, 0.9],
            ]
        )

    def test_second_rating_changes_online_user_vector(self):
        first = solve_user_vector(
            self.item_factors[[0]], np.array([5.0]), reg_param=0.1
        )
        second = solve_user_vector(
            self.item_factors[[0, 1]], np.array([5.0, 1.0]), reg_param=0.1
        )
        self.assertFalse(np.allclose(first, second))

    def test_rank_excludes_already_rated_movies(self):
        user_vector = solve_user_vector(
            self.item_factors[[0]], np.array([5.0]), reg_param=0.1
        )
        recommendations = rank_unrated_items(
            self.movie_ids,
            self.item_factors,
            user_vector,
            rated_movie_ids={101},
            top_n=3,
        )
        recommended_ids = [movie_id for movie_id, _ in recommendations]
        self.assertNotIn(101, recommended_ids)
        self.assertLessEqual(len(recommendations), 3)

    def test_nonnegative_solution_has_no_negative_factors(self):
        user_vector = solve_user_vector(
            self.item_factors[[0, 1]],
            np.array([5.0, 1.0]),
            reg_param=0.1,
        )
        self.assertTrue(np.all(user_vector >= -1e-9))

    def test_clamp_rating_keeps_online_scores_on_rating_scale(self):
        self.assertEqual(clamp_rating(0.2), RATING_MIN)
        self.assertEqual(clamp_rating(9.68), RATING_MAX)
        self.assertEqual(clamp_rating(4.2), 4.2)


class OnlineRegularizationAlignmentTests(unittest.TestCase):
    """在线 fold-in 的正则口径必须与 MLlib 训练一致（SPARK-2085）。

    这是"复用已训练物品因子"这一前提成立的必要条件：训练时解物品因子用的是
    ``numRatings(i) * lambda``，在线解用户向量也必须用 ``n_u * lambda``。
    曾经写成固定的 ``sqrt(reg_param)``，导致解出的用户向量模长失控
    （实测中位数 4.10，而全库物品因子模长中位数仅 1.56），点积溢出评分量纲。
    """

    def test_effective_reg_scales_with_rating_count(self):
        self.assertEqual(effective_reg_param(0.1, 20), 0.1 * 20)
        self.assertEqual(effective_reg_param(0.1, 1), 0.1)
        # 评分数越多，正则越强（MLlib 的行为）
        self.assertGreater(
            effective_reg_param(0.1, 100), effective_reg_param(0.1, 10)
        )

    def test_more_ratings_shrink_the_user_vector(self):
        """同样方向的数据，评分数越多正则越强 → 解出的向量模长越小。

        固定正则的实现不会有这个性质（所有用户共用同一个正则强度）。
        用带噪声、过完备的观测（评分数 > 因子维度），否则最小二乘能精确拟合、
        正则项不起作用，就测不出缩放行为。
        """
        rng = np.random.default_rng(7)
        basis = np.abs(rng.normal(0.6, 0.2, size=(30, 4)))
        ratings = 4.0 + rng.normal(0.0, 0.2, size=30)

        few = solve_user_vector(basis[:5], ratings[:5], reg_param=0.1)
        many = solve_user_vector(basis, ratings, reg_param=0.1)
        self.assertLess(np.linalg.norm(many), np.linalg.norm(few))

    def test_user_vector_stays_on_item_factor_scale(self):
        """解出的用户向量应与物品因子处于同一尺度，点积才落在评分量纲内。

        回归测试：固定正则时向量模长可以远超物品因子（实测 4.10 vs 1.56）。
        观测数需多于因子维度，否则最小二乘精确插值、正则无从发挥。
        """
        rng = np.random.default_rng(11)
        item_factors = np.abs(rng.normal(0.6, 0.2, size=(40, 5)))
        ratings = 4.0 + rng.normal(0.0, 0.3, size=40)
        vector = solve_user_vector(item_factors, ratings, reg_param=0.1)

        max_item_norm = np.linalg.norm(item_factors, axis=1).max()
        self.assertLessEqual(np.linalg.norm(vector), max_item_norm * 1.5)

    def test_dot_products_do_not_overflow_the_rating_scale(self):
        """裸点积不应普遍突破 5.0 上限（否则展示分只能靠截断，失去区分度）。"""
        rng = np.random.default_rng(20260921)
        item_factors = np.abs(rng.normal(0.4, 0.15, size=(800, 10)))
        # 过完备观测（50 > rank 10），正则才真正参与约束
        ratings = 4.0 + rng.normal(0.0, 0.3, size=50)
        vector = solve_user_vector(item_factors[:50], ratings, reg_param=0.1)

        scores = item_factors @ vector
        self.assertLess(
            float((scores > RATING_MAX).mean()), 0.10,
            "超过一成的候选点积突破评分上限，说明正则口径又退回了固定量",
        )


if __name__ == "__main__":
    unittest.main()
