import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "spark"))

from utils.training_contract import (  # noqa: E402
    build_system_ratings_query,
    combine_rating_sources,
    filter_recommendations_to_system_users,
)


class TrainingContractTests(unittest.TestCase):
    def test_training_query_reads_only_system_user_ratings(self):
        query = build_system_ratings_query()
        self.assertIn("FROM user_ratings", query)
        self.assertNotIn("FROM ratings", query)

    def test_movielens_and_system_ratings_are_both_combined(self):
        class FakeDataFrame:
            def __init__(self, name):
                self.name = name

            def unionByName(self, other):
                return (self.name, other.name)

        result = combine_rating_sources(FakeDataFrame("movielens"), FakeDataFrame("system"))
        self.assertEqual(result, ("movielens", "system"))

    def test_recommendations_are_filtered_to_registered_users(self):
        class FakeDataFrame:
            def join(self, other, condition, how):
                return (other, condition, how)

        result = filter_recommendations_to_system_users(
            FakeDataFrame(), "registered_users", "user_id"
        )
        self.assertEqual(result[1], "user_id")
        self.assertEqual(result[2], "left_semi")


if __name__ == "__main__":
    unittest.main()
