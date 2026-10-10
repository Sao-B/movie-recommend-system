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
    """Deterministic split of a ratings DataFrame, independent of partitioning.

    每条评分按 ``md5(userId:movieId)`` 确定性地落到一个桶，桶号 < 阈值进训练集、
    否则进测试集。**结果只取决于 (userId, movieId)，与分区数、运行次数、Spark
    版本无关**——这正是它取代 ``randomSplit`` 的原因：后者按分区内的 RNG 抽样，
    换分区数就会得到不同的训练/测试集，同一份数据复现不出同一批数字。

    三点必须说清楚，避免误用：

    1. **哈希的输入是"取负之后的"userId。** ``train_als.py`` 先把 MovieLens 的
       userId 取负（与系统用户隔离），**再**调用本函数。由于
       ``md5("-8:1") != md5("8:1")``，用原始正数 userId 计算会得到**另一套**
       训练/测试集（实测：正数口径 train=25,600,540 / test=6,399,664；
       取负口径 train=25,599,242 / test=6,400,962）。复现线上结果必须同样取负。
    2. **这是"逐条评分"的划分，不是"按用户分层"的划分。** 哈希的输入虽含
       ``userId``，但每条评分仍各自独立地进桶，因此某用户在测试集中的评分条数
       服从二项分布 ``B(n_u, 1 - train_ratio)``。**它并不保证每个用户在训练集和
       测试集中都有评分**——评分很少的用户有一定概率一条都不进测试集。
       MovieLens-32M 上实测这类用户约占 0.1%（且他们贡献的评分不足 0.05%），
       规模上可忽略，但不应声称"保证"。
    3. **线上口径已实测复现**（2026-09-21 用本文件在生产 VM 上完整训练）：
       train=25,599,242 / test=6,400,962、**RMSE=0.8074**、MAE=0.6277。
       该数字是确定性的，同配置重跑应完全一致。
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
