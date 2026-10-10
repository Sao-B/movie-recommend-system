# -*- coding: utf-8 -*-
"""分布指标族：推荐列表的覆盖/热度/年份分布（评估协议扩展，2026-10-10）。

背景：holdout NDCG/Recall 对"全推热片"盲（holdout 标签本身偏热门），
分布指标负责感知这类灾难。与 NDCG 并报，验收两族都要看。

用法：
  python experiments/itemcf/distribution_metrics.py \
      --rec experiments/itemcf/out/holdout/fusion_ablation_v2/recommendations.csv \
      --movies data/raw/movies.csv --ratings data/raw/ratings.csv \
      --variants V0_raw_sum,V4_itemcf_als_genre --top-n 20 \
      --out <输出 json 路径>

指标（每个变体，Top-N 切片）：
  slots / distinct_movies / coverage（distinct / 全库被评物品数）
  popularity: median / p90 / mean；novelty = mean log10(pop+1)
  hot_share_top1pct / hot_share_top10pct（全库热度分位 cutoff）
  year share: pre1990 / pre2000 / 2010s（全库基线一并列出）
"""
import argparse
import hashlib
import json
import os
import re
from collections import Counter

import numpy as np
import pandas as pd

YEAR_RE = re.compile(r"\((\d{4})\)\s*$")


def popularity_counts(ratings_path, cache_path=None):
    """物品评分数（分块计数）；cache_path 存在且非空时直接读缓存。"""
    if cache_path and os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
        z = np.load(cache_path)
        return z["ids"], z["counts"]
    ids, counts = [], []
    for chunk in pd.read_csv(ratings_path, usecols=["movieId"], chunksize=5_000_000):
        vc = chunk["movieId"].value_counts()
        ids.append(vc.index.values.astype(np.int64))
        counts.append(vc.values.astype(np.int64))
    ids = np.concatenate(ids)
    counts = np.concatenate(counts)
    order = np.argsort(ids, kind="mergesort")
    ids, counts = ids[order], counts[order]
    uniq, starts = np.unique(ids, return_index=True)
    counts = np.add.reduceat(counts, starts)
    if cache_path:
        np.savez_compressed(cache_path, ids=uniq, counts=counts)
    return uniq, counts


def catalog_year_stats(movies_path):
    df = pd.read_csv(movies_path)
    years = df["title"].map(lambda t: int(m.group(1)) if (m := YEAR_RE.search(t or "")) else None)
    years = years.dropna().astype(int)
    n = len(years)
    return {
        "catalog_movies": int(n),
        "median_year": int(years.median()),
        "pre1990_pct": round(float((years < 1990).mean()) * 100, 1),
        "pre2000_pct": round(float((years < 2000).mean()) * 100, 1),
        "y2010s_pct": round(float(((years >= 2010) & (years < 2020)).mean()) * 100, 1),
    }


def main():
    ap = argparse.ArgumentParser(description="分布指标族（与 NDCG 并报）")
    ap.add_argument("--rec", required=True, help="recommendations.csv")
    ap.add_argument("--movies", required=True, help="movies.csv（含 title 取年份）")
    ap.add_argument("--ratings", default=None, help="ratings.csv（算热度；或 --pop-cache）")
    ap.add_argument("--pop-cache", default=None, help="热度缓存 npz")
    ap.add_argument("--variants", required=True, help="逗号分隔变体名")
    ap.add_argument("--top-n", type=int, default=20)
    ap.add_argument("--out", required=True, help="输出 json")
    args = ap.parse_args()

    item_ids, counts = popularity_counts(
        args.ratings, args.pop_cache)
    pop = dict(zip(item_ids.tolist(), counts.tolist()))
    pops_arr = counts.astype(np.float64)
    n_rated = len(item_ids)
    cutoff1 = float(np.percentile(pops_arr, 99))
    cutoff10 = float(np.percentile(pops_arr, 90))

    movies = pd.read_csv(args.movies)
    year = {}
    for mid, title in zip(movies["movieId"].values, movies["title"].values):
        m = YEAR_RE.search(str(title) or "")
        year[int(mid)] = int(m.group(1)) if m else None

    rec = pd.read_csv(args.rec)
    catalog = catalog_year_stats(args.movies)
    result = {
        "protocol": {
            "rec_file": os.path.abspath(args.rec),
            "top_n": args.top_n,
            "n_items_rated": int(n_rated),
            "hot_cutoff_top1pct": int(cutoff1),
            "hot_cutoff_top10pct": int(cutoff10),
        },
        "catalog_baseline": catalog,
        "variants": {},
    }
    for variant in args.variants.split(","):
        sub = rec[(rec["variant"] == variant) & (rec["rank"] <= args.top_n)]
        ids = sub["movie_id"].values
        p = np.array([pop.get(int(m), 0) for m in ids], dtype=np.float64)
        ys = np.array([year.get(int(m)) for m in ids])
        ys = ys[ys != None]  # noqa: E711
        result["variants"][variant] = {
            "slots": int(len(ids)),
            "distinct_movies": int(sub["movie_id"].nunique()),
            "coverage_pct": round(sub["movie_id"].nunique() / n_rated * 100, 3),
            "pop_median": float(np.median(p)),
            "pop_p90": float(np.percentile(p, 90)),
            "pop_mean": round(float(p.mean()), 1),
            "novelty_mean_log10pop": round(float(np.log10(p + 1).mean()), 3),
            "hot_share_top1pct_pct": round(float((p >= cutoff1).mean()) * 100, 1),
            "hot_share_top10pct_pct": round(float((p >= cutoff10).mean()) * 100, 1),
            "pre1990_pct": round(float((ys < 1990).mean()) * 100, 1) if len(ys) else None,
            "pre2000_pct": round(float((ys < 2000).mean()) * 100, 1) if len(ys) else None,
            "y2010s_pct": round(float(((ys >= 2010) & (ys < 2020)).mean()) * 100, 1) if len(ys) else None,
        }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
    print(json.dumps(result, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
