import sys
from pathlib import Path
import unittest

import numpy as np


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from recommendation_core import (  # noqa: E402
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
            nonnegative=True,
        )
        self.assertTrue(np.all(user_vector >= -1e-9))


if __name__ == "__main__":
    unittest.main()
