"""SQLAlchemy repository used by the online recommendation service."""

from sqlalchemy.orm import joinedload

from .models import ModelItemFactor, ModelVersion, Movie, Rating, Recommendation


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
            Rating.query
            .filter_by(user_id=user_id)
            .order_by(Rating.rated_at.desc())
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

    def get_movies(self, movie_ids):
        movie_ids = list(movie_ids)
        if not movie_ids:
            return {}
        return {
            movie.movie_id: movie
            for movie in Movie.query.filter(Movie.movie_id.in_(movie_ids)).all()
        }
