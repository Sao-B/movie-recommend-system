#!/usr/bin/env python
"""把 MovieLens 全量评分聚合成「五档分布」并写入 movie_rating_stats 表。

为什么需要这个脚本
------------------
``movies`` 表只有 ``avg_rating`` / ``rating_count`` 两个标量，没有分档计数，
而详情页「评分统计」需要显示 5/4/3/2/1 星各占多少。MovieLens 的原始明细
（``data/raw/ratings.csv``，3200 万条）就在本地，本脚本一次性聚合后落表，
页面展示时再与本站 ``user_ratings`` 合并——页面上不区分来源。

档位规则**直接复用** ``backend/app/rating_stats.py`` 的 ``rating_to_star_bucket``，
不在这里另写一份，避免两处口径漂移（4.5 到底算 4 星还是 5 星必须只有一个答案）。

用法::

    backend/venv/Scripts/python.exe scripts/build_ml_rating_stats.py
    backend/venv/Scripts/python.exe scripts/build_ml_rating_stats.py --dry-run

可重复执行：写入用 ``ON DUPLICATE KEY UPDATE``，重跑结果一致。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "backend"))

import pymysql  # noqa: E402
from config_loader import mysql_kwargs  # noqa: E402
from app.rating_stats import rating_to_star_bucket  # noqa: E402

DEFAULT_INPUT = os.path.join(PROJECT_ROOT, "data", "raw", "ratings.csv")

# rating_count, rating_sum, cnt_1..cnt_5
_ROW_LEN = 7


def aggregate(input_path: str, progress_every: int = 5_000_000) -> dict:
    """流式聚合 ratings.csv，返回 ``{movie_id: [count, sum, c1, c2, c3, c4, c5]}``。

    用 ``str.split(",")`` 而不是 ``csv.reader``：MovieLens 的 ratings.csv 是
    纯数字四列、无引号无转义，split 快得多，3200 万行能省下可观时间。
    """
    agg: dict = {}
    bucket_of = rating_to_star_bucket

    with open(input_path, "r", encoding="utf-8-sig") as fh:
        header = fh.readline()  # userId,movieId,rating,timestamp
        if not header.lower().startswith("userid"):
            # 没有表头就当作数据行处理
            fh.seek(0)

        total = 0
        for line in fh:
            parts = line.split(",")
            if len(parts) < 3:
                continue
            movie_id = int(parts[1])
            rating = float(parts[2])
            star = bucket_of(rating)

            row = agg.get(movie_id)
            if row is None:
                row = [0] * _ROW_LEN
                agg[movie_id] = row

            row[0] += 1
            row[1] += rating
            row[1 + star] += 1  # cnt_1..cnt_5 落在下标 2..6

            total += 1
            if progress_every and total % progress_every == 0:
                print("  已读取 %s 行，电影数 %s" % (format(total, ","), format(len(agg), ",")),
                      flush=True)

    return agg


def main() -> int:
    parser = argparse.ArgumentParser(description="聚合 MovieLens 评分五档分布")
    parser.add_argument("--input", default=DEFAULT_INPUT, help="ratings.csv 路径")
    parser.add_argument("--batch", type=int, default=5000, help="写库批大小")
    parser.add_argument("--dry-run", action="store_true", help="只聚合不写库")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        print("找不到输入文件: %s" % args.input)
        return 1

    size_mb = os.path.getsize(args.input) / 1024 / 1024
    print("输入: %s (%.1f MB)" % (args.input, size_mb))

    started = time.time()
    agg = aggregate(args.input)
    print("聚合完成: %s 部电影, 用时 %.1fs" % (format(len(agg), ","), time.time() - started))

    if not agg:
        print("没有聚合到任何数据，终止")
        return 1

    total_ratings = sum(r[0] for r in agg.values())
    total_sum = sum(r[1] for r in agg.values())
    print("评分总条数: %s，总评分和: %.1f，全局均值: %.4f"
          % (format(total_ratings, ","), total_sum, total_sum / total_ratings))

    if args.dry_run:
        print("--dry-run：不写库")
        return 0

    conn = pymysql.connect(**mysql_kwargs(), autocommit=False)
    cur = conn.cursor()

    # 外键要求 movie_id 必须存在于 movies，先取白名单，避免中途插一半报 1452
    cur.execute("SELECT movie_id FROM movies")
    valid_ids = {row[0] for row in cur.fetchall()}
    print("movies 表电影数: %s" % format(len(valid_ids), ","))

    rows = []
    skipped = []
    for movie_id, r in agg.items():
        if movie_id not in valid_ids:
            skipped.append(movie_id)
            continue
        rows.append((movie_id, r[0], round(r[1], 1), r[2], r[3], r[4], r[5], r[6]))

    if skipped:
        print("跳过 %d 部不在 movies 表中的电影（外键会拒绝）: %s"
              % (len(skipped), sorted(skipped)[:10]))

    sql = (
        "INSERT INTO movie_rating_stats "
        "(movie_id, rating_count, rating_sum, cnt_1, cnt_2, cnt_3, cnt_4, cnt_5) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        "rating_count = VALUES(rating_count), rating_sum = VALUES(rating_sum), "
        "cnt_1 = VALUES(cnt_1), cnt_2 = VALUES(cnt_2), cnt_3 = VALUES(cnt_3), "
        "cnt_4 = VALUES(cnt_4), cnt_5 = VALUES(cnt_5)"
    )

    written = 0
    try:
        for i in range(0, len(rows), args.batch):
            chunk = rows[i:i + args.batch]
            cur.executemany(sql, chunk)
            conn.commit()
            written += len(chunk)
            print("  已写入 %s / %s" % (format(written, ","), format(len(rows), ",")), flush=True)
    except Exception as exc:
        conn.rollback()
        print("写入失败已回滚（本批未提交的部分丢弃）: %r" % (exc,))
        conn.close()
        return 1

    # 交叉校验：聚合结果应等于 movies.rating_count，均值应接近 movies.avg_rating
    cur.execute("""
        SELECT COUNT(*),
               SUM(CASE WHEN s.rating_count <> m.rating_count THEN 1 ELSE 0 END),
               SUM(CASE WHEN ABS(s.rating_sum / s.rating_count - m.avg_rating) > 0.005
                        THEN 1 ELSE 0 END)
        FROM movie_rating_stats s
        JOIN movies m ON m.movie_id = s.movie_id
        WHERE m.rating_count > 0
    """)
    joined, count_mismatch, mean_mismatch = cur.fetchone()
    cur.execute("SELECT COUNT(*) FROM movies WHERE rating_count > 0")
    movies_with_ratings = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*), SUM(rating_count), SUM(cnt_1 + cnt_2 + cnt_3 + cnt_4 + cnt_5) "
                "FROM movie_rating_stats")
    table_rows, table_count, table_bucket_sum = cur.fetchone()

    print("")
    print("=== 校验 ===")
    print("movies 中有评分的电影: %s" % format(movies_with_ratings, ","))
    print("movie_rating_stats 行数: %s" % format(table_rows, ","))
    print("评分条数: 表内 %s vs 明细 %s -> %s"
          % (format(table_count, ","), format(total_ratings, ","),
             "一致" if table_count == total_ratings else "不一致"))
    print("五档计数之和 == rating_count: %s" % (table_bucket_sum == table_count))
    print("与 movies JOIN 上的行数: %s" % format(joined, ","))
    print("rating_count 与 movies 不一致的电影数: %s" % count_mismatch)
    print("均值与 movies.avg_rating 偏差 >0.005 的电影数: %s" % mean_mismatch)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
