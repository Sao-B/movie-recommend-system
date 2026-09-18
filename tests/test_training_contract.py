import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "spark"))

from utils.training_contract import (  # noqa: E402
    SPLIT_BUCKETS,
    build_system_ratings_query,
    combine_rating_sources,
    filter_recommendations_to_system_users,
    rating_bucket,
    train_bucket_threshold,
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

    def test_rating_bucket_is_deterministic_and_in_range(self):
        self.assertEqual(rating_bucket(12345, 678), rating_bucket(12345, 678))
        self.assertNotEqual(rating_bucket(1, 2), rating_bucket(2, 1))
        for user_id, movie_id in [(0, 0), (1, 1), (162541, 87585), (10**9, 10**6)]:
            bucket = rating_bucket(user_id, movie_id)
            self.assertGreaterEqual(bucket, 0)
            self.assertLess(bucket, SPLIT_BUCKETS)

    def test_rating_bucket_matches_md5_reference(self):
        import hashlib

        user_id, movie_id = 98765, 4321
        digest = hashlib.md5(f"{user_id}:{movie_id}".encode("utf-8")).hexdigest()
        expected = int(digest[:8], 16) % SPLIT_BUCKETS
        self.assertEqual(rating_bucket(user_id, movie_id), expected)

    def test_train_bucket_threshold_follows_ratio(self):
        self.assertEqual(train_bucket_threshold(0.8), int(0.8 * SPLIT_BUCKETS))
        self.assertEqual(train_bucket_threshold(0.5), SPLIT_BUCKETS // 2)

    def test_train_bucket_threshold_rejects_invalid_ratio(self):
        for bad in (0.0, 1.0, -0.5, 1.5):
            with self.assertRaises(ValueError):
                train_bucket_threshold(bad)


if __name__ == "__main__":
    unittest.main()
