"""SQLAlchemy repository used by the online recommendation service."""

from sqlalchemy.orm import joinedload

from .models import ModelItemFactor, ModelVersion, Movie, Recommendation, UserRating


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

    def get_popular_movies(self, limit, min_rating_count=50):
        """冷启动兜底数据源：全站高分热门电影（评分人数达标，按均分降序）。"""
        return (
            Movie.query
            .filter(Movie.avg_rating.isnot(None), Movie.rating_count >= min_rating_count)
            .order_by(Movie.avg_rating.desc(), Movie.rating_count.desc())
            .limit(int(limit))
            .all()
        )

    def get_movies(self, movie_ids):
        movie_ids = list(movie_ids)
        if not movie_ids:
            return {}
        return {
            movie.movie_id: movie
            for movie in Movie.query.filter(Movie.movie_id.in_(movie_ids)).all()
        }
