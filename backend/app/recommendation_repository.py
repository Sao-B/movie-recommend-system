"""SQLAlchemy repository used by the online recommendation service."""

from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from .db import db
from .models import (
    ModelItemFactor, ModelVersion, Movie, Recommendation, User, UserDislike,
    UserRating,
)


class SqlAlchemyRecommendationRepository:
    def get_active_model(self):
        return (
            ModelVersion.query
            .filter_by(status="active")
            .order_by(ModelVersion.activated_at.desc(), ModelVersion.generated_at.desc())
            .first()
        )

    def get_user_ratings(self, user_id):
        return (
            UserRating.query
            .filter_by(user_id=user_id)
            .order_by(UserRating.rated_at.desc())
            .all()
        )

    def get_model_factors(self, version):
        return (
            ModelItemFactor.query
            .filter_by(model_version=version)
            .order_by(ModelItemFactor.movie_id)
            .all()
        )

    def get_batch_recommendations(self, user_id, version):
        if version is None:
            return []
        return (
            Recommendation.query
            .options(joinedload(Recommendation.movie))
            .filter_by(user_id=user_id, model_version=version)
            .order_by(Recommendation.rank)
            .all()
        )

    def get_popular_movies(self, limit, min_rating_count=50, genres=None):
        """冷启动兜底数据源：全站高分热门电影（评分人数达标，按均分降序）。

        ``genres`` 非空时只取命中任一所选类型的电影（用户偏好类型加权热门）。
        """
        query = Movie.query.filter(
            Movie.avg_rating.isnot(None), Movie.rating_count >= min_rating_count)
        if genres:
            query = query.filter(or_(*[Movie.genres.like(f"%{g}%") for g in genres]))
        return query.order_by(
            Movie.avg_rating.desc(), Movie.rating_count.desc()
        ).limit(int(limit)).all()

    def get_user_dislikes(self, user_id):
        """用户标记"不感兴趣"的电影集合（推荐硬排除）。"""
        rows = UserDislike.query.filter_by(user_id=user_id).all()
        return {row.movie_id for row in rows}

    def get_user_favorite_genres(self, user_id):
        """用户自选偏好类型；从未选过返回 None，跳过返回 []。"""
        user = db.session.get(User, user_id)
        return user.favorite_genre_list if user else None

    def get_movies_above_support(self, min_rating_count):
        """评分人数 >= 阈值的电影集合（ALS 召回路的支持度门控）。"""
        rows = (Movie.query.filter(Movie.rating_count >= min_rating_count)
                .with_entities(Movie.movie_id).all())
        return {int(r[0]) for r in rows}

    def get_movie_genres(self, movie_id):
        """单部电影的类型集合；无标注返回空集合。"""
        movie = db.session.get(Movie, movie_id)
        if movie is None or not movie.genres:
            return set()
        return {g for g in movie.genres.split("|") if g}

    def get_movies(self, movie_ids):
        movie_ids = list(movie_ids)
        if not movie_ids:
            return {}
        return {
            movie.movie_id: movie
            for movie in Movie.query.filter(Movie.movie_id.in_(movie_ids)).all()
        }

    def get_movies_genres_map(self, movie_ids):
        """批量取电影类型集合 {movie_id: set(genre)}（V5 标签路避免逐条查询）。"""
        movie_ids = list(movie_ids)
        if not movie_ids:
            return {}
        rows = (Movie.query.filter(Movie.movie_id.in_(movie_ids))
                .with_entities(Movie.movie_id, Movie.genres).all())
        return {
            int(mid): {g for g in (genres or "").split("|") if g}
            for mid, genres in rows
        }
