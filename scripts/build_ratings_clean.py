#!/usr/bin/env python
"""从 MovieLens 原始评分明细聚合出「电影级评分统计」文件 ratings_clean.csv。

为什么需要这个文件
------------------
``data/raw/ratings.csv`` 是 3200 万条评分明细（877 MB），直接喂给 MySQL 没法做
「按电影聚合」的展示。预处理阶段把它按 movieId 聚合成每部电影的评分统计
（条数、均值、中位数、标准差、极值、四分位数等 11 列），产物即
``data/raw/ratings_clean.csv``（84,432 行）。下游由 ``import_movie_stats.py``
把其中的 ``rating_mean`` / ``rating_count`` 两列回填到 ``movies`` 表，
支撑首页热门榜的贝叶斯加权排序与电影卡片上的均分展示。

与本库 ``movie_rating_stats`` 表的关系
--------------------------------------
两者同源（都是对同一份 ratings.csv 聚合），行数一致（84,432）。
``movie_rating_stats`` 由 ``build_ml_rating_stats.py`` 重建，额外带五档计数与
精确 ``rating_sum``，支撑详情页的评分统计；本文件只服务于 ``movies`` 两列回填，
属于 Spark/Hadoop 预处理链路的本地复现。

用法::

    backend/venv/Scripts/python.exe scripts/build_ratings_clean.py --check
    backend/venv/Scripts/python.exe scripts/build_ratings_clean.py --force

- ``--check``  重算一遍并与现有 ratings_clean.csv 逐行数值比对（不写文件）；
- ``--force``  真正覆盖写出（默认目标文件已存在时必须显式指定）。
"""
from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from array import array

import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_INPUT = os.path.join(PROJECT_ROOT, "data", "raw", "ratings.csv")
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "data", "raw", "ratings_clean.csv")

HEADER = [
    "movieId", "rating_count", "rating_mean", "rating_median", "rating_std",
    "rating_min", "rating_max", "rating_sum", "rating_range",
    "rating_q1", "rating_q3",
]

REL_TOL = 1e-9


def aggregate(input_path: str):
    """单遍流式读取评分明细，按 movieId 聚合出全部统计列。

    评分以紧凑的 ``array('d')`` 收集（32M 个 float64 约 256 MB），
    中位数与四分位数需要全量值，无法像均值那样增量计算。
    """
    buckets: dict[int, array] = {}
    started = time.time()
    with open(input_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        idx_movie = header.index("movieId")
        idx_rating = header.index("rating")
        for row in reader:
            buckets.setdefault(int(row[idx_movie]), array("d")).append(float(row[idx_rating]))
    print(f"明细读取完成：{len(buckets)} 部电影，用时 {time.time() - started:.1f}s")

    rows = []
    for movie_id in sorted(buckets):
        values = np.frombuffer(buckets[movie_id], dtype=np.float64)
        count = values.size
        total = float(values.sum())
        low, high = float(values.min()), float(values.max())
        # 单条评分没有样本标准差（pandas groupby.agg 的语义是 NaN -> CSV 空串），保持一致
        rating_std = f"{float(np.std(values, ddof=1))}" if count > 1 else ""
        rows.append([
            movie_id,
            count,
            total / count,                                   # rating_mean
            float(np.median(values)),                        # rating_median
            rating_std,
            low,
            high,
            total,                                           # rating_sum
            high - low,                                      # rating_range
            float(np.percentile(values, 25)),                # rating_q1
            float(np.percentile(values, 75)),                # rating_q3
        ])
    return rows


def write_output(rows, output_path: str):
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(rows)
    print(f"已写出 {len(rows)} 行 -> {output_path}")


def close_enough(a: str, b: str) -> bool:
    """CSV 单元格数值比对：均为合法浮点时按相对容差比，否则按字符串比。"""
    if a == b:
        return True
    try:
        fa, fb = float(a), float(b)
    except ValueError:
        return False
    scale = max(abs(fa), abs(fb), 1.0)
    return abs(fa - fb) <= REL_TOL * scale


def check_against(rows, reference_path: str) -> int:
    """把重算结果与现有文件逐单元格比对，返回不一致的行数。"""
    with open(reference_path, "r", encoding="utf-8", newline="") as f:
        reference = list(csv.reader(f))
    if reference[0] != HEADER:
        print("表头不一致！")
        print(f"  现有: {reference[0]}")
        print(f"  重算: {HEADER}")
        return -1

    mismatch_rows = 0
    for i, ref_row in enumerate(reference[1:], start=1):
        new_row = rows[i - 1] if i - 1 < len(rows) else None
        if new_row is None or len(ref_row) != len(new_row):
            mismatch_rows += 1
            continue
        cells = [str(v) for v in new_row]
        if not all(close_enough(a, b) for a, b in zip(ref_row, cells)):
            mismatch_rows += 1
            if mismatch_rows <= 5:
                print(f"  行 {i} 不一致:")
                print(f"    现有: {','.join(ref_row)}")
                print(f"    重算: {','.join(cells)}")
    return mismatch_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--input", default=DEFAULT_INPUT, help="评分明细 CSV（默认 data/raw/ratings.csv）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="聚合统计输出路径")
    parser.add_argument("--check", action="store_true", help="只比对现有文件，不写出")
    parser.add_argument("--force", action="store_true", help="目标文件已存在时允许覆盖")
    args = parser.parse_args()

    if not os.path.exists(args.input):
        raise SystemExit(f"评分明细不存在: {args.input}")

    rows = aggregate(args.input)

    if args.check:
        if not os.path.exists(args.output):
            raise SystemExit(f"--check 需要已存在的参照文件: {args.output}")
        print(f"与现有文件比对: {args.output}")
        bad = check_against(rows, args.output)
        if bad == 0:
            print("全部一致：重算结果与现有 ratings_clean.csv 数值等价。")
        else:
            print(f"共 {bad} 行不一致。")
            raise SystemExit(1)
        return

    if os.path.exists(args.output) and not args.force:
        raise SystemExit(
            f"目标已存在: {args.output}\n"
            "如需覆盖请加 --force，或先用 --check 验证重算结果与现有文件是否一致。"
        )
    write_output(rows, args.output)


if __name__ == "__main__":
    main()
