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
    # escape='"'：MovieLens 用 RFC4180 双引号转义（"" 表示字面引号）。
    # 缺这个 option 时（默认 escape='\\'），带引号的标题会被撕裂，
    # 甚至把标题后半截漏进 genres（历史事故：movie_id 284105）。
    movies_raw = spark.read.csv(
        hdfs_cfg["movies_path"], header=True, inferSchema=True, escape='"'
    )

    # ── 2. 解析年份，规范列名 ─────────────────────────────────────
    # 使用 Spark 原生 regexp_extract 替代 Python UDF，性能更好
    #
    # MovieLens 的 title 惯例把冠词倒装到末尾（"Shawshank Redemption, The (1994)"，
    # 便于字母序排序）。这里还原为自然语序 "The Shawshank Redemption (1994)"，
    # 与库内一次性清洗（2026-09-15，映射备份 title_fix_mapping.csv）保持一致。
    ARTICLES = ("The|An|A|La|Le|Les|Los|Las|El|Il|L|Der|Die|Das|Det|Den|De|Dem|"
                "Ein|Eine|Ett|O|Os|As|Un|Una|Une|En")
    title_fixed = F.trim(
        F.regexp_replace(
            F.regexp_replace(
                F.col("title"),
                rf"^(.+), ({ARTICLES}) (\(.+\))$", "$2 $1 $3",
            ),
            rf"^(.+), ({ARTICLES})$", "$2 $1",
        )
    )
    movies = (
        movies_raw
        .select(
            F.col("movieId").cast("int").alias("movie_id"),
            title_fixed.alias("title"),
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
