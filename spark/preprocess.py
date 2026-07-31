"""
preprocess.py — 从 HDFS 读取 MovieLens 原始 movies.csv，
                清洗后将电影元数据写入 MySQL movies 表。

运行方式：
    spark-submit --master local[*] spark/preprocess.py
"""

import sys
import os

# 将 spark/ 目录加入路径，使 utils 可直接 import
sys.path.insert(0, os.path.dirname(__file__))

from pyspark.sql import functions as F

from utils.spark_session import get_spark_session
from utils.mysql_utils import write_df_to_mysql


def main():
    spark, cfg, jdbc_url = get_spark_session("Preprocess-Movies")
    mysql_cfg = cfg["mysql"]
    hdfs_cfg  = cfg["hdfs"]

    # ── 1. 读取 movies.csv ────────────────────────────────────────
    print(f"读取 {hdfs_cfg['movies_path']} ...")
    movies_raw = spark.read.csv(
        hdfs_cfg["movies_path"], header=True, inferSchema=True
    )

    # ── 2. 解析年份，规范列名 ─────────────────────────────────────
    # 使用 Spark 原生 regexp_extract 替代 Python UDF，性能更好
    movies = (
        movies_raw
        .select(
            F.col("movieId").cast("int").alias("movie_id"),
            F.col("title"),
            F.col("genres"),
            F.regexp_extract(F.col("title"), r'\((\d{4})\)\s*$', 1)
             .cast("int")
             .alias("year")
        )
        .na.drop(subset=["movie_id", "title"])  # 先去除 null 行
        .dropDuplicates(["movie_id"])           # 再去重，效率更高
    )
    count = movies.count()
    print(f"清洗后电影总数: {count:,}")

    # ── 3. 写入 MySQL movies 表 ───────────────────────────────────
    print("写入 MySQL movies 表 ...")
    write_df_to_mysql(
        movies, "movies", jdbc_url,
        mysql_cfg["user"], mysql_cfg["password"],
        mode="overwrite"
    )
    print("movies 表写入完成 ✓")

    spark.stop()


if __name__ == "__main__":
    main()
