"""试点编排：读切片 → 时序切分 → ALS 训练 → 三模型评测 → CSV 落盘。

用法（VM）:
  spark-submit --master 'local[*]' run_pilot.py \
    --input hdfs:///movielens/pilot/ratings.csv \
    --out /home/h/pilot_out/run001 --rank 50 --reg 0.1 --iters 10 --top-n 20 --seed 42

打分用 numpy 稠密索引向量化（每用户一次矩阵点积），保证 2 万用户档可跑。
"""
from __future__ import print_function

import argparse
import csv
import os

import numpy as np
from pyspark.sql import SparkSession

from als import fit_spark, train_rmse
from baselines import most_popular, random_top
from data import temporal_split
from metrics import ndcg_at_k, recall_at_k, rmse


def load_rows(spark, path):
    df = spark.read.csv(path, header=True).select('userId', 'movieId', 'rating', 'timestamp')
    return [(int(r.userId), int(r.movieId), float(r.rating), int(r.timestamp)) for r in df.collect()]


class ModelScorer(object):
    """model 的 dict 因子转 numpy 稠密索引，向量化打分。"""

    def __init__(self, model, item_ids):
        self.mu = model['mu']
        self.item_ids = list(item_ids)
        self.item_ids_arr = np.asarray(self.item_ids)
        self.i_index = dict((i, k) for k, i in enumerate(self.item_ids))
        rank = len(next(iter(model['Q'].values())))
        self.Q = np.empty((len(self.item_ids), rank))
        self.bi = np.empty(len(self.item_ids))
        for k, i in enumerate(self.item_ids):
            self.Q[k] = model['Q'][i]
            self.bi[k] = model['bi'][i]
        self.P = dict((u, np.asarray(p)) for u, p in model['P'].items())
        self.bu = model['bu']

    def top_for_user(self, u, exclude_idx, top_n):
        mask = np.ones(len(self.item_ids), dtype=bool)
        if exclude_idx:
            mask[np.asarray(exclude_idx, dtype=np.int64)] = False
        cand = np.flatnonzero(mask)
        scores = self.Q[cand].dot(self.P[u]) + self.bi[cand] + self.bu[u] + self.mu
        order = np.lexsort((self.item_ids_arr[cand], -scores))[:top_n]
        return [self.item_ids[k] for k in cand[order]]

    def pred_pairs(self, u_arr, i_arr, r_arr):
        pu = np.asarray([self.P[u] for u in u_arr])
        q = self.Q[i_arr]
        pred = self.mu + np.asarray([self.bu[u] for u in u_arr]) + self.bi[i_arr] \
            + np.einsum('ij,ij->i', pu, q)
        return list(zip(pred.tolist(), r_arr.tolist()))


def evaluate(name, rec_fn, train_items_by_user, items_by_user, candidates, top_n):
    """rec_fn(user, exclude_idx) -> 候选内 Top-N；相关 = 测试条目 rating >= 4.0。"""
    i_index = dict((i, k) for k, i in enumerate(candidates))
    recs, ndcgs, users_used = [], [], 0
    for u in sorted(items_by_user):
        rel = set(i for i, r in items_by_user[u] if r >= 4.0)
        if not rel:
            continue
        exclude_idx = [i_index[i] for i in train_items_by_user.get(u, ()) if i in i_index]
        rec = rec_fn(u, exclude_idx)[:top_n]
        users_used += 1
        recs.append(recall_at_k(rec, rel, top_n))
        ndcgs.append(ndcg_at_k(rec, rel, top_n))
    return {'model': name,
            'recall@%d' % top_n: sum(recs) / len(recs) if recs else None,
            'ndcg@%d' % top_n: sum(ndcgs) / len(ndcgs) if ndcgs else None,
            'users_scored': users_used}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--rank', type=int, default=50)
    ap.add_argument('--reg', type=float, default=0.1)
    ap.add_argument('--iters', type=int, default=10)
    ap.add_argument('--top-n', type=int, default=20)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    spark = SparkSession.builder.appName('pilot-als-eval').getOrCreate()
    rows = load_rows(spark, args.input)
    train, test = temporal_split(rows)
    print('rows=%d train=%d test=%d' % (len(rows), len(train), len(test)))

    model, losses = fit_spark(spark, train, rank=args.rank, reg=args.reg,
                              iters=args.iters, seed=args.seed, log=print)
    candidates = sorted(set(i for _, i, _, _ in train))
    scorer = ModelScorer(model, candidates)

    train_items_by_user = {}
    for u, i, _, _ in train:
        train_items_by_user.setdefault(u, set()).add(i)
    items_by_user = {}
    for u, i, r, _ in test:
        items_by_user.setdefault(u, []).append((i, r))

    pop_full = most_popular(train, len(candidates))
    i_index = dict((i, k) for k, i in enumerate(candidates))

    def als_rec(u, exclude_idx):
        return scorer.top_for_user(u, exclude_idx, args.top_n)

    def pop_rec(u, exclude_idx):
        excl = set(candidates[k] for k in exclude_idx)
        return [i for i in pop_full if i not in excl][:args.top_n]

    def rand_rec(u, exclude_idx):
        excl = set(candidates[k] for k in exclude_idx)
        pool = [i for i in candidates if i not in excl]
        return random_top(pool, args.top_n, args.seed)

    results = [
        evaluate('Random', rand_rec, train_items_by_user, items_by_user, candidates, args.top_n),
        evaluate('MostPop', pop_rec, train_items_by_user, items_by_user, candidates, args.top_n),
        evaluate('ALS', als_rec, train_items_by_user, items_by_user, candidates, args.top_n),
    ]

    # 测试集预测（向量化）与 RMSE / 越界率
    u_arr = np.fromiter((u for u, _, _, _ in test), dtype=np.int64, count=len(test))
    i_arr = np.fromiter((scorer.i_index[i] for _, i, _, _ in test), dtype=np.int64, count=len(test))
    r_arr = np.fromiter((r for _, _, r, _ in test), dtype=np.float64, count=len(test))
    pred_pairs = scorer.pred_pairs(u_arr, i_arr, r_arr)
    all_preds = [p for p, _ in pred_pairs]
    out_of_range = sum(1 for p in all_preds if p < 1.0 or p > 5.0) / float(len(all_preds))
    rmse_rows = [
        {'model': 'ALS', 'rmse': rmse(pred_pairs)},
        {'model': 'Const(mu)', 'rmse': rmse([(model['mu'], r) for _, _, r, _ in test])},
    ]

    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    with open(os.path.join(args.out, 'results.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['model', 'recall@%d' % args.top_n,
                                          'ndcg@%d' % args.top_n, 'users_scored'])
        w.writeheader()
        for row in results:
            w.writerow(row)
    with open(os.path.join(args.out, 'rmse.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['model', 'rmse'])
        w.writeheader()
        for row in rmse_rows:
            w.writerow(row)
    with open(os.path.join(args.out, 'pred_dist.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['pred'])
        for p in all_preds:
            w.writerow(['%.6f' % p])
    with open(os.path.join(args.out, 'summary.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['metric', 'value'])
        w.writerow(['out_of_range_rate', '%.6f' % out_of_range])
        for k, v in enumerate(losses):
            w.writerow(['train_rmse_iter%d' % (k + 1), '%.6f' % v])

    # 案例：训练评分最少的 3 个用户，ALS 与 MostPop 的 Top-20 对照（含支持度）
    counts = {}
    user_counts = {}
    for u, i, _, _ in train:
        counts[i] = counts.get(i, 0) + 1
        user_counts[u] = user_counts.get(u, 0) + 1
    sparse_users = sorted(model['bu'], key=lambda u: (user_counts.get(u, 0), u))[:3]
    with open(os.path.join(args.out, 'case_users.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['user', 'model', 'rank', 'movie_id', 'train_support'])
        for u in sparse_users:
            excl = [i_index[i] for i in train_items_by_user.get(u, ()) if i in i_index]
            for name, rec in (('ALS', scorer.top_for_user(u, excl, args.top_n)),
                              ('MostPop', [i for i in pop_full
                                           if i not in set(candidates[k] for k in excl)][:args.top_n])):
                for pos, i in enumerate(rec, 1):
                    w.writerow([u, name, pos, i, counts.get(i, 0)])
    print('out_of_range_rate=%.6f' % out_of_range)
    print('WROTE %s' % args.out)
    spark.stop()


if __name__ == '__main__':
    main()
