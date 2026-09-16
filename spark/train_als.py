"""Train ALS with isolated MovieLens IDs and publish a versioned model."""

import os
import sys
from datetime import datetime, timezone
from uuid import uuid4

sys.path.insert(0, os.path.dirname(__file__))

from pyspark.ml.evaluation import RegressionEvaluator
from pyspark.ml.recommendation import ALS
from pyspark.sql import functions as F

from utils.mysql_utils import (
    activate_model_version,
    cleanup_stale_model_data,
    create_model_version,
    read_df_from_mysql,
    retire_model_version,
    write_df_to_mysql,
)
from utils.spark_session import get_spark_session
from utils.training_contract import (
    RATING_MAX,
    RATING_MIN,
    build_system_ratings_query,
    combine_rating_sources,
    filter_recommendations_to_system_users,
    split_ratings_for_eval,
)
from utils.user_id_namespace import validate_user_id_namespace


def _new_model_version():
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"als-{timestamp}-{uuid4().hex[:12]}"


def _read_max_system_user_id(spark, jdbc_url, mysql_cfg):
    row = read_df_from_mysql(
        spark,
        "(SELECT COALESCE(MAX(id), 0) AS max_system_user_id FROM users) AS t",
        jdbc_url,
        mysql_cfg["user"],
        mysql_cfg["password"],
    ).first()
    return int(row["max_system_user_id"] or 0)


def _read_system_users(spark, jdbc_url, mysql_cfg):
    return (
        read_df_from_mysql(spark, "users", jdbc_url, mysql_cfg["user"], mysql_cfg["password"])
        .select(F.col("id").cast("int").alias("user_id"))
        .distinct()
    )


def main():
    spark, cfg, jdbc_url = get_spark_session("ALS-Training")
    mysql_cfg = cfg["mysql"]
    hdfs_cfg = cfg["hdfs"]
    als_cfg = cfg["als"]
    model_version = None

    try:
        print(f"Reading MovieLens ratings from {hdfs_cfg['ratings_path']} ...")

        ratings_raw = spark.read.csv(
            hdfs_cfg["ratings_path"], header=True, inferSchema=True
        )
        ratings_ml_raw = (
            ratings_raw
            .select(
                F.col("userId").cast("long").alias("ml_user_id"),
                F.col("movieId").cast("int").alias("movieId"),
                F.col("rating").cast("float").alias("rating"),
            )
            .na.drop()
        )
        max_ml_user_id = int(
            ratings_ml_raw.agg(F.max("ml_user_id").alias("max_ml_user_id")).first()[0]
        )
        max_system_user_id = _read_max_system_user_id(spark, jdbc_url, mysql_cfg)
        validate_user_id_namespace(
            max_system_user_id=max_system_user_id,
            max_movielens_user_id=max_ml_user_id,
        )

        ratings_ml = ratings_ml_raw.select(
            (-F.col("ml_user_id")).cast("int").alias("userId"),
            F.col("movieId"),
            F.col("rating"),
        )
        ml_count = ratings_ml.count()
        print(f"MovieLens ratings: {ml_count:,}; MovieLens user IDs negated")

        registered_users = _read_system_users(spark, jdbc_url, mysql_cfg)
        ratings_system = (
            read_df_from_mysql(
                spark,
                build_system_ratings_query(),
                jdbc_url,
                mysql_cfg["user"],
                mysql_cfg["password"],
            )
            .select(
                F.col("userId").cast("int"),
                F.col("movieId").cast("int"),
                F.col("rating").cast("float"),
            )
            .na.drop()
            .join(
                registered_users.withColumnRenamed("user_id", "userId"),
                "userId",
                "left_semi",
            )
        )
        system_count = ratings_system.count()
        print(f"Application-user ratings: {system_count:,}")

        ml_train, ml_test = split_ratings_for_eval(ratings_ml, float(als_cfg["train_ratio"]))
        training_ratings = combine_rating_sources(ml_train, ratings_system).cache()
        training_count = training_ratings.count()
        print(f"Training ratings: {training_count:,}; MovieLens test ratings: {ml_test.count():,}")

        als = ALS(
            rank=int(als_cfg["rank"]),
            maxIter=int(als_cfg["max_iter"]),
            regParam=float(als_cfg["reg_param"]),
            userCol="userId",
            itemCol="movieId",
            ratingCol="rating",
            coldStartStrategy=als_cfg["cold_start_strategy"],
            nonnegative=True,
            implicitPrefs=False,
        )
        model = als.fit(training_ratings)

        predictions = model.transform(ml_test)
        evaluator = RegressionEvaluator(
            metricName="rmse", labelCol="rating", predictionCol="prediction"
        )
        rmse = evaluator.evaluate(predictions)
        print(f"MovieLens test RMSE: {rmse:.4f}")

        model_version = _new_model_version()
        create_model_version(
            mysql_cfg,
            model_version,
            rank=int(als_cfg["rank"]),
            reg_param=float(als_cfg["reg_param"]),
        )

        item_factors = model.itemFactors.select(
            F.lit(model_version).alias("model_version"),
            F.col("id").cast("int").alias("movie_id"),
            F.to_json(F.col("features")).alias("factors"),
        )
        write_df_to_mysql(
            item_factors,
            "model_item_factors",
            jdbc_url,
            mysql_cfg["user"],
            mysql_cfg["password"],
            mode="append",
        )

        user_recs = model.recommendForAllUsers(int(als_cfg["top_n"]))
        recs_flat = (
            user_recs
            .select(
                F.col("userId").alias("user_id"),
                F.posexplode(F.col("recommendations")).alias("pos", "rec"),
            )
            .select(
                "user_id",
                F.col("rec.movieId").cast("int").alias("movie_id"),
                F.col("rec.rating").cast("float").alias("predicted_rating"),
                (F.col("pos") + 1).cast("smallint").alias("rank"),
            )
        )
        recs_flat = filter_recommendations_to_system_users(
            recs_flat, registered_users, "user_id"
        ).withColumn("model_version", F.lit(model_version))
        # ALS 预测是隐向量点积，可能超出评分量纲；入库前统一截断到 [0.5, 5.0]
        recs_flat = recs_flat.withColumn(
            "predicted_rating",
            F.least(
                F.greatest(F.col("predicted_rating"), F.lit(RATING_MIN)),
                F.lit(RATING_MAX),
            ),
        )
        recs_flat = recs_flat.select(
            "model_version", "user_id", "movie_id", "predicted_rating", "rank"
        )
        rec_count = recs_flat.count()
        if rec_count:
            clamp_check = recs_flat.agg(
                F.min("predicted_rating").alias("min_pred"),
                F.max("predicted_rating").alias("max_pred"),
            ).first()
            print(
                f"predicted_rating range after clamp: "
                f"[{clamp_check['min_pred']:.4f}, {clamp_check['max_pred']:.4f}] "
                f"(scale [{RATING_MIN}, {RATING_MAX}])"
            )
        else:
            print(
                "predicted_rating: no registered system users, "
                "0 recommendations to write (expected when users table is empty)"
            )
        write_df_to_mysql(
            recs_flat,
            "recommendations",
            jdbc_url,
            mysql_cfg["user"],
            mysql_cfg["password"],
            mode="append",
        )

        activate_model_version(mysql_cfg, model_version)
        try:
            cleanup_stale_model_data(mysql_cfg, model_version)
        except Exception as cleanup_error:
            # 模型已发布，清理失败只告警，不影响本次训练结果
            print(f"WARNING: stale model data cleanup failed: {cleanup_error}")
        print(
            f"Published model {model_version}: training={training_count:,}, "
            f"MovieLens={ml_count:,}, system={system_count:,}, "
            f"RMSE={rmse:.4f}, recommendations={rec_count:,}"
        )
    except Exception:
        if model_version is not None:
            try:
                retire_model_version(mysql_cfg, model_version)
            except Exception as cleanup_error:
                print(f"Could not retire failed model {model_version}: {cleanup_error}")
        raise
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
