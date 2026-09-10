"""Small, dependency-free contracts shared by the ALS training job and tests."""


def build_system_ratings_query():
    """Return the only MySQL source allowed for application-user ratings."""
    return "(SELECT user_id AS userId, movie_id AS movieId, rating FROM user_ratings) AS t"


def combine_rating_sources(movielens_df, system_ratings_df):
    """Keep both MovieLens and application-user ratings in the ALS input."""
    return movielens_df.unionByName(system_ratings_df)


def filter_recommendations_to_system_users(
    recommendations_df, system_users_df, user_column="user_id"
):
    """Keep only recommendation rows whose ID exists in the users table."""
    return recommendations_df.join(system_users_df, user_column, "left_semi")
