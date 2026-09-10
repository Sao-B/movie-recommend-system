import json
import sys
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from recommendation_service import OnlineRecommendationService  # noqa: E402


class FakeModel:
    def __init__(self, version):
        self.version = version
        self.rank = 2
        self.reg_param = 0.1


class FakeRating:
    def __init__(self, movie_id, rating):
        self.movie_id = movie_id
        self.rating = rating


class FakeFactor:
    def __init__(self, movie_id, values):
        self.movie_id = movie_id
        self.factors = json.dumps(values)


class FakeRecommendation:
    def __init__(self, movie_id):
        self.movie_id = movie_id

    def to_dict(self):
        return {"rank": 1, "predicted_rating": 3.0, "movie": {"movie_id": self.movie_id}}


class FakeRepository:
    def __init__(self):
        self.active_model = FakeModel("v1")
        self.ratings = {1: [FakeRating(101, 5.0)]}
        self.factors = {
            "v1": [
                FakeFactor(101, [1.0, 0.0]),
                FakeFactor(102, [0.0, 1.0]),
                FakeFactor(103, [0.9, 0.1]),
                FakeFactor(104, [0.1, 0.9]),
            ],
            "v2": [
                FakeFactor(101, [2.0, 0.0]),
                FakeFactor(102, [0.0, 1.0]),
                FakeFactor(103, [0.2, 0.8]),
                FakeFactor(104, [0.8, 0.2]),
            ],
        }
        self.factor_loads = []

    def get_active_model(self):
        return self.active_model

    def get_user_ratings(self, user_id):
        return self.ratings.get(user_id, [])

    def get_model_factors(self, version):
        self.factor_loads.append(version)
        return self.factors[version]

    def get_batch_recommendations(self, user_id, version):
        return [FakeRecommendation(999)]

    def get_movies(self, movie_ids):
        return {movie_id: {"movie_id": movie_id} for movie_id in movie_ids}


class RecommendationServiceTests(unittest.TestCase):
    def setUp(self):
        self.repo = FakeRepository()
        self.service = OnlineRecommendationService(self.repo, top_n=3)

    def test_second_rating_recomputes_from_all_user_ratings(self):
        first = self.service.get_for_user(1)
        self.repo.ratings[1].append(FakeRating(102, 1.0))
        second = self.service.get_for_user(1)

        self.assertNotEqual(first[0]["predicted_rating"], second[0]["predicted_rating"])

    def test_model_version_change_refreshes_factor_cache(self):
        first = self.service.get_for_user(1)
        self.repo.active_model = FakeModel("v2")
        second = self.service.get_for_user(1)

        self.assertEqual(self.repo.factor_loads, ["v1", "v2"])
        self.assertNotEqual(first[0]["predicted_rating"], second[0]["predicted_rating"])

    def test_missing_rated_factor_falls_back_to_batch_recommendations(self):
        self.repo.factors["v1"] = [FakeFactor(102, [0.0, 1.0])]
        result = self.service.get_for_user(1)

        self.assertEqual(result, [{"rank": 1, "predicted_rating": 3.0, "movie": {"movie_id": 999}}])

    def test_user_without_ratings_uses_batch_recommendations(self):
        result = self.service.get_for_user(2)

        self.assertEqual(result[0]["movie"]["movie_id"], 999)


if __name__ == "__main__":
    unittest.main()
