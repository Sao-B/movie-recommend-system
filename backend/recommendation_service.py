"""Online recommendation service backed by the latest active item factors."""

from __future__ import annotations

import json
import logging
from threading import RLock

import numpy as np

try:
    from recommendation_core import rank_unrated_items, solve_user_vector
except ModuleNotFoundError:
    from .recommendation_core import rank_unrated_items, solve_user_vector


logger = logging.getLogger(__name__)


class OnlineRecommendationService:
    """Calculate recommendations for one application user without retraining ALS."""

    def __init__(self, repository, top_n=20):
        self.repository = repository
        self.top_n = int(top_n)
        self._factor_cache = {}
        self._cache_lock = RLock()

    def _load_factors(self, model):
        version = str(model.version)
        with self._cache_lock:
            cached = self._factor_cache.get(version)
            if cached is not None:
                return cached

            factors = {}
            for row in self.repository.get_model_factors(version):
                values = row.factors
                if isinstance(values, str):
                    values = json.loads(values)
                vector = np.asarray(values, dtype=float)
                if vector.ndim != 1 or vector.shape[0] != int(model.rank):
                    raise ValueError("model item factor has an invalid rank")
                if not np.all(np.isfinite(vector)):
                    raise ValueError("model item factor contains a non-finite value")
                factors[int(row.movie_id)] = vector

            if not factors:
                raise ValueError("active model has no item factors")
            self._factor_cache[version] = factors
            return factors

    def _batch_recommendations(self, user_id, version):
        return [item.to_dict() for item in self.repository.get_batch_recommendations(user_id, version)]

    def _online_recommendations(self, user_id, model, ratings):
        factors = self._load_factors(model)
        rating_by_movie = {int(row.movie_id): float(row.rating) for row in ratings}
        rated_movie_ids = sorted(rating_by_movie)
        if not set(rated_movie_ids).issubset(factors):
            raise ValueError("active model is missing a rated movie factor")

        rated_factors = [factors[movie_id] for movie_id in rated_movie_ids]
        rated_values = [rating_by_movie[movie_id] for movie_id in rated_movie_ids]
        user_vector = solve_user_vector(
            np.asarray(rated_factors),
            np.asarray(rated_values),
            reg_param=float(model.reg_param),
            nonnegative=True,
        )

        movie_ids = sorted(factors)
        item_factors = np.asarray([factors[movie_id] for movie_id in movie_ids])
        ranked = rank_unrated_items(
            movie_ids,
            item_factors,
            user_vector,
            set(rated_movie_ids),
            self.top_n,
        )
        movies = self.repository.get_movies([movie_id for movie_id, _ in ranked])

        result = []
        for rank, (movie_id, score) in enumerate(ranked, start=1):
            movie = movies.get(movie_id)
            if movie is None:
                raise ValueError("active model item factor has no movie metadata")
            movie_data = movie.to_dict() if hasattr(movie, "to_dict") else movie
            result.append({
                "rank": rank,
                "predicted_rating": round(score, 4),
                "movie": movie_data,
            })
        return result

    def get_for_user(self, user_id):
        """Return online recommendations, falling back to the active batch result."""
        model = self.repository.get_active_model()
        if model is None:
            return self._batch_recommendations(user_id, None)

        ratings = self.repository.get_user_ratings(user_id)
        if not ratings:
            return self._batch_recommendations(user_id, str(model.version))

        try:
            return self._online_recommendations(user_id, model, ratings)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return self._batch_recommendations(user_id, str(model.version))
        except Exception:
            logger.exception("online recommendation failed for user_id=%s", user_id)
            return self._batch_recommendations(user_id, str(model.version))
