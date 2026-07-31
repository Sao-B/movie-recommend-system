"""
train_als.py — 基于 MovieLens-32M 的 ALS 协同过滤离线训练

流程：
    1. 从 HDFS 读取 ratings.csv
    2. 8:2 划分训练集 / 测试集
    3. 训练 ALS 模型
    4. 在测试集上计算 RMSE
    5. 为全部用户生成 Top-N 推荐
    6. 将推荐结果写入 MySQL recommendations 表

运行方式：
    spark-submit --master local[*] \\
                 --driver-memory 4g \\
                 spark/train_als.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(__file__))

from pyspark.ml.recommendation import ALS
from pyspark.ml.evaluation import RegressionEvaluator
from pyspark.sql import functions as F

from utils.spark_session import get_spark_session
from utils.mysql_utils import write_df_to_mysql


def main():
    spark, cfg, jdbc_url = get_spark_session("ALS-Training")
    mysql_cfg = cfg["mysql"]
    hdfs_cfg  = cfg["hdfs"]
    als_cfg   = cfg["als"]

    # ── 1. 读取评分数据 ───────────────────────────────────────────
    print(f"读取 {hdfs_cfg['ratings_path']} ...")
    ratings_raw = spark.read.csv(
        hdfs_cfg["ratings_path"], header=True, inferSchema=True
    )
    ratings = (
        ratings_raw
        .select(
            F.col("userId").cast("int"),
            F.col("movieId").cast("int"),
            F.col("rating").cast("float")
        )
        .na.drop()
    )
    # 缓存 ratings，避免 randomSplit 后多次 count() 重复计算
    ratings.cache()
    total = ratings.count()
    print(f"有效评分总数: {total:,}")

    # ── 2. 划分训练集 / 测试集 ────────────────────────────────────
    train_ratio = float(als_cfg["train_ratio"])
    train, test = ratings.randomSplit([train_ratio, 1 - train_ratio], seed=42)
    print(f"训练集: {train.count():,}  测试集: {test.count():,}")

    # ── 3. 训练 ALS 模型 ──────────────────────────────────────────
    als = ALS(
        rank=int(als_cfg["rank"]),
        maxIter=int(als_cfg["max_iter"]),
        regParam=float(als_cfg["reg_param"]),
        userCol="userId",
        itemCol="movieId",
        ratingCol="rating",
        coldStartStrategy=als_cfg["cold_start_strategy"],
        nonnegative=True,
        implicitPrefs=False
    )
    print(
        f"开始训练 ALS "
        f"(rank={als_cfg['rank']}, maxIter={als_cfg['max_iter']}, "
        f"regParam={als_cfg['reg_param']}) ..."
    )
    model = als.fit(train)
    print("模型训练完成 ✓")

    # ── 4. RMSE 评估 ──────────────────────────────────────────────
    predictions = model.transform(test)
    evaluator = RegressionEvaluator(
        metricName="rmse",
        labelCol="rating",
        predictionCol="prediction"
    )
    rmse = evaluator.evaluate(predictions)
    print(f"\n{'='*48}")
    print(f"  测试集 RMSE: {rmse:.4f}")
    print(f"{'='*48}\n")

    # ── 5. 为全部用户生成 Top-N 推荐 ─────────────────────────────
    top_n = int(als_cfg["top_n"])
    print(f"生成每位用户 Top-{top_n} 推荐 ...")
    user_recs = model.recommendForAllUsers(top_n)

    # 展开嵌套列：(userId, [{movieId, rating}, ...]) → 平铺行
    recs_flat = (
        user_recs
        .select(
            F.col("userId").alias("user_id"),
            F.posexplode(F.col("recommendations")).alias("pos", "rec")
        )
        .select(
            F.col("user_id"),
            F.col("rec.movieId").alias("movie_id"),
            F.col("rec.rating").alias("predicted_rating"),
            (F.col("pos") + 1).alias("rank")          # 排名从 1 开始
        )
    )
    rec_count = recs_flat.count()
    print(f"推荐记录总数: {rec_count:,}")

    # ── 6. 写入 MySQL recommendations 表 ─────────────────────────
    print("写入 MySQL recommendations 表 ...")
    write_df_to_mysql(
        recs_flat, "recommendations", jdbc_url,
        mysql_cfg["user"], mysql_cfg["password"],
        mode="overwrite"
    )
    print("写入完成 ✓")
    print(f"\n训练摘要：评分总数={total:,}  RMSE={rmse:.4f}  推荐条数={rec_count:,}")

    spark.stop()


if __name__ == "__main__":
    main()
