"""Small, dependency-free contracts shared by the ALS training job and tests."""

import hashlib

# 评分量纲（MovieLens 半星制）：预测分写入/展示前统一截断到该区间
RATING_MIN = 0.5
RATING_MAX = 5.0

# 训练/测试划分粒度：每条评分按 md5 哈希映射到 [0, SPLIT_BUCKETS) 的桶
SPLIT_BUCKETS = 10000


def build_system_ratings_query():
    """Return the only MySQL source allowed for application-user ratings."""
    return "(SELECT user_id AS userId, movie_id AS movieId, rating FROM user_ratings) AS t"


def clamp_rating(value):
    """Clamp a predicted rating back into the observed rating scale [0.5, 5.0].

    ALS 预测是隐向量点积，训练不足时可能超出评分量纲；写入/展示前必须截断。
    """
    return max(RATING_MIN, min(RATING_MAX, float(value)))


def rating_bucket(user_id, movie_id):
    """Deterministic bucket in [0, SPLIT_BUCKETS) for one (user, movie) rating.

    参考实现（纯 Python）。Spark 侧 split_ratings_for_eval 必须用同样的
    md5(userId:movieId) 前 8 位十六进制 mod SPLIT_BUCKETS 计算，保证两边一致。
    """
    digest = hashlib.md5(f"{user_id}:{movie_id}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % SPLIT_BUCKETS


def train_bucket_threshold(train_ratio):
    """Bucket threshold: ratings with bucket < threshold go to the training side."""
    if not 0.0 < float(train_ratio) < 1.0:
        raise ValueError(f"train_ratio must be in (0, 1), got {train_ratio}")
    return int(round(float(train_ratio) * SPLIT_BUCKETS))


def split_ratings_for_eval(ratings_df, train_ratio):
    """Deterministic per-user stratified split of a ratings DataFrame.

    每个用户的评分按 (userId, movieId) 哈希确定性地拆成 ~train_ratio / 其余两份，
    保证每个用户在训练集和测试集中都有评分（标准的 held-out ratings 评估协议），
    且结果与分区数、运行次数无关，完全可复现。
    """
    from pyspark.sql import functions as F

    threshold = train_bucket_threshold(train_ratio)
    bucket_col = F.pmod(
        F.conv(
            F.substring(
                F.md5(F.concat_ws(":", F.col("userId"), F.col("movieId"))), 1, 8
            ),
            16,
            10,
        ).cast("long"),
        F.lit(SPLIT_BUCKETS),
    )
    with_bucket = ratings_df.withColumn("_bucket", bucket_col)
    train_df = with_bucket.filter(F.col("_bucket") < threshold).drop("_bucket")
    test_df = with_bucket.filter(F.col("_bucket") >= threshold).drop("_bucket")
    return train_df, test_df


def combine_rating_sources(movielens_df, system_ratings_df):
    """Keep both MovieLens and application-user ratings in the ALS input."""
    return movielens_df.unionByName(system_ratings_df)


def filter_recommendations_to_system_users(
    recommendations_df, system_users_df, user_column="user_id"
):
    """Keep only recommendation rows whose ID exists in the users table."""
    return recommendations_df.join(system_users_df, user_column, "left_semi")
