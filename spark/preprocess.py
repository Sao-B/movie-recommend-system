"""
preprocess.py — 从 HDFS 读取 MovieLens 原始 movies.csv，
                清洗后经 staging 表 upsert 进 MySQL movies 表。

可重复执行：upsert 只更新 title/genres/year，不会清空 TMDB 富化列与
MovieLens 统计列，也不会触发 movie_comments / movie_rating_stats 的外键冲突。

运行方式：
    spark-submit --master local[*] spark/preprocess.py
"""

import sys
import os

# 将 spark/ 目录加入路径，使 utils 可直接 import
sys.path.insert(0, os.path.dirname(__file__))

from pyspark.sql import functions as F

from utils.spark_session import get_spark_session
from utils.mysql_utils import (
    write_df_to_mysql,
    reset_movies_staging,
    upsert_movies_from_staging,
)


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

    # ── 3. 写入 MySQL：先 staging 暂存，再 upsert 进 movies ──────
    # 不能直接 overwrite movies：TRUNCATE 会被 movie_comments /
    # movie_rating_stats 的外键挡住，即使绕过也会清空统计与富化列。
    print("重建 movies_staging 暂存表 ...")
    reset_movies_staging(mysql_cfg)
    print("写入 MySQL movies_staging 暂存表 ...")
    write_df_to_mysql(
        movies, "movies_staging", jdbc_url,
        mysql_cfg["user"], mysql_cfg["password"],
        mode="overwrite"
    )
    print("upsert 进 movies 表 ...")
    affected, orphans = upsert_movies_from_staging(mysql_cfg)
    print(f"movies 表 upsert 完成 ✓（影响 {affected:,} 行）")
    if orphans:
        sample = ", ".join(str(m) for m in orphans[:10])
        print(
            f"WARNING: {len(orphans):,} 部电影已不在 MovieLens 数据中，"
            f"但被评论/评分统计外键引用，予以保留（示例: {sample}）"
        )

    spark.stop()


if __name__ == "__main__":
    main()
