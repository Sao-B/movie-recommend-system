#!/usr/bin/env python
"""把 MovieLens 评分明细按 movieId 聚合成每部电影的 11 项评分统计，写出 ratings_clean.csv。

    backend/venv/Scripts/python.exe scripts/build_ratings_clean.py          # 重算并写出 data/raw/ratings_clean.csv
    backend/venv/Scripts/python.exe scripts/build_ratings_clean.py --check  # 只与现有产物比对，不写出

``scripts/import_movie_stats.py`` 取产物里的 rating_mean / rating_count 两列回填 movies 表，
供首页热门榜排序与电影卡片均分使用。
"""
import argparse
import os

import pandas as pd
from pandas.testing import assert_frame_equal

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RATINGS = os.path.join(ROOT, "data", "raw", "ratings.csv")
OUTPUT = os.path.join(ROOT, "data", "raw", "ratings_clean.csv")


def aggregate():
    """按 movieId 聚合出 11 列统计；单条评分的电影无样本标准差，rating_std 为 NaN。"""
    by_movie = pd.read_csv(RATINGS, usecols=["movieId", "rating"]).groupby("movieId")["rating"]

    stats = by_movie.agg(
        rating_count="count", rating_mean="mean", rating_median="median",
        rating_std="std", rating_min="min", rating_max="max", rating_sum="sum",
    )
    stats["rating_range"] = stats["rating_max"] - stats["rating_min"]

    quartiles = by_movie.quantile([0.25, 0.75]).unstack()
    stats["rating_q1"] = quartiles[0.25]
    stats["rating_q3"] = quartiles[0.75]
    return stats.reset_index()


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="只与现有产物比对，不写出")
    args = parser.parse_args()

    stats = aggregate()
    if not args.check:
        stats.to_csv(OUTPUT, index=False)
        print(f"已写出 {len(stats)} 行 -> {OUTPUT}")
        return

    if not os.path.exists(OUTPUT):
        raise SystemExit(f"没有可比的产物: {OUTPUT}")
    try:
        assert_frame_equal(stats, pd.read_csv(OUTPUT))
    except AssertionError as exc:
        raise SystemExit(f"重算结果与 {OUTPUT} 不一致:\n{exc}")
    print(f"一致：重算结果与 {OUTPUT} 完全相同。")


if __name__ == "__main__":
    main()
