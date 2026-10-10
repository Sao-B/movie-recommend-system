"""Online recommendation service backed by the latest active item factors."""

from __future__ import annotations

import json
import logging
from threading import RLock

import numpy as np

from .recommendation_core import clamp_rating, rank_unrated_items, solve_user_vector


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

    def _candidate_movie_ids(self, factors):
        """排序用的候选集：全部已训练出因子的电影。

        .. deprecated:: 2026-10-01
           这里原本有一道"候选集门槛"（``IDENTIFIABILITY_MIN_RATINGS = 50``），
           按 `Movie.rating_count >= 50` 把候选从 8 万收窄到约 1.6 万，理由是
           "50 维因子在观测数不足时不可辨识"。该门槛已整体移除，候选集回到
           全量因子——**排序结果因此可能重新被冷门条目（评分数个位数）占据**，
           这是已知且接受的代价，等新方案重建后再处理。
        """
        return sorted(factors)

    def _batch_recommendations(self, user_id, version):
        return [item.to_dict() for item in self.repository.get_batch_recommendations(user_id, version)]

    def _online_recommendations(self, user_id, model, ratings, dislikes=frozenset()):
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
        )

        movie_ids = self._candidate_movie_ids(factors)
        item_factors = np.asarray([factors[movie_id] for movie_id in movie_ids])
        excluded = set(rated_movie_ids) | set(dislikes)
        ranked = rank_unrated_items(
            movie_ids,
            item_factors,
            user_vector,
            excluded,
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
                # 在线分数是裸点积，截断到评分量纲，避免超出 [0.5, 5.0] 穿帮
                "predicted_rating": round(clamp_rating(score), 4),
                "movie": movie_data,
            })
        return result

    def _popular_recommendations(self, genres=None, dislikes=frozenset()):
        """冷启动兜底：批量推荐为空时回退全站高分热门电影，避免新用户看到空白页。

        ``genres`` 非空时优先在用户自选类型内取（不足再补全局热门），
        ``dislikes`` 里的电影一律排除。
        """
        pool = []
        if genres:
            pool = self.repository.get_popular_movies(self.top_n * 2, genres=genres)
        pool = pool or self.repository.get_popular_movies(self.top_n * 2)
        movies = [m for m in pool if m.movie_id not in dislikes]
        if len(movies) < self.top_n:
            extra = self.repository.get_popular_movies(self.top_n * 3)
            have = {m.movie_id for m in movies}
            movies += [m for m in extra if m.movie_id not in have
                       and m.movie_id not in dislikes]
        result = []
        for rank, movie in enumerate(movies[:self.top_n], start=1):
            movie_data = movie.to_dict() if hasattr(movie, "to_dict") else movie
            result.append({
                "rank": rank,
                # 借用 avg_rating 作为展示分值；无评分数据时为 None（前端已兼容）
                "predicted_rating": movie_data.get("avg_rating"),
                "movie": movie_data,
            })
        return result

    def get_popular_for_user(self, user_id, dislikes=None):
        """冷启动兜底入口（ItemCF 等其它链路空结果时共用）：
        按用户自选类型加权的全站高分热门，排除"不感兴趣"。"""
        if dislikes is None:
            dislikes = self.repository.get_user_dislikes(user_id)
        genres = self.repository.get_user_favorite_genres(user_id)
        return self._popular_recommendations(genres=genres, dislikes=dislikes)

    def get_for_user(self, user_id):
        """Return online recommendations, falling back to the active batch result,
        then to globally popular movies when batch results are empty."""
        dislikes = self.repository.get_user_dislikes(user_id)
        model = self.repository.get_active_model()
        if model is None:
            recs = [r for r in self._batch_recommendations(user_id, None)
                    if r["movie"]["movie_id"] not in dislikes]
        else:
            ratings = self.repository.get_user_ratings(user_id)
            if not ratings:
                recs = [r for r in self._batch_recommendations(user_id, str(model.version))
                        if r["movie"]["movie_id"] not in dislikes]
            else:
                try:
                    recs = self._online_recommendations(user_id, model, ratings,
                                                        dislikes=dislikes)
                except Exception:
                    # 在线求解失败（因子缺失、用户评分异常等）一律降级到批量推荐
                    logger.exception("online recommendation failed for user_id=%s", user_id)
                    recs = [r for r in self._batch_recommendations(user_id, str(model.version))
                            if r["movie"]["movie_id"] not in dislikes]

        return recs if recs else self.get_popular_for_user(user_id, dislikes=dislikes)
