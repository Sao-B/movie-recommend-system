"""
preprocess.py — 从 HDFS 读取 MovieLens 原始 movies.csv，
                清洗后将电影元数据写入 MySQL movies 表。

运行方式：
    spark-submit --master local[*] spark/preprocess.py
"""

import re
import sys
import os

# 将 spark/ 目录加入路径，使 utils 可直接 import
sys.path.insert(0, os.path.dirname(__file__))

from pyspark.sql import functions as F
from pyspark.sql.types import IntegerType

from utils.spark_session import get_spark_session
from utils.mysql_utils import write_df_to_mysql


def _parse_year(title: str):
    """从标题末尾的 (YYYY) 中提取年份，如 'Toy Story (1995)' → 1995。"""
    if title is None:
        return None
    match = re.search(r'\((\d{4})\)\s*$', title)
    return int(match.group(1)) if match else None


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
    parse_year_udf = F.udf(_parse_year, IntegerType())
    movies = (
        movies_raw
        .select(
            F.col("movieId").cast("int").alias("movie_id"),
            F.col("title"),
            F.col("genres"),
            parse_year_udf(F.col("title")).alias("year")
        )
        .dropDuplicates(["movie_id"])
        .na.drop(subset=["movie_id", "title"])
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
