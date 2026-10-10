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
import json
import math
from collections import Counter

import numpy as np
if __package__:
    from .als import fit_spark, train_rmse, objective, predict_model
    from .baselines import most_popular, random_top
    from .data import temporal_split, temporal_split_three_way
    from .metrics import ndcg_at_k, recall_at_k, rmse, recommendable_positive_ratio, prediction_bounds
else:
    from als import fit_spark, train_rmse, objective, predict_model
    from baselines import most_popular, random_top
    from data import temporal_split, temporal_split_three_way
    from metrics import ndcg_at_k, recall_at_k, rmse, recommendable_positive_ratio, prediction_bounds


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
        if not np.all(np.isfinite(scores)):
            raise ValueError('non-finite candidate prediction')
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


def legacy_main(args=None):
    from pyspark.sql import SparkSession
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--rank', type=int, default=50)
    ap.add_argument('--reg', type=float, default=0.1)
    ap.add_argument('--iters', type=int, default=10)
    ap.add_argument('--top-n', type=int, default=20)
    ap.add_argument('--seed', type=int, default=42)
    if args is None:
        args = ap.parse_args()

    spark = SparkSession.builder.appName('pilot-als-eval').getOrCreate()
    rows = load_rows(spark, args.input)
    train, test = temporal_split(rows)
    print('rows=%d train=%d test=%d' % (len(rows), len(train), len(test)))

    model, losses = fit_spark(spark, train, rank=args.rank, reg=args.reg,
                              iters=args.iters, seed=args.seed, log=print,
                              num_slices=getattr(args, 'num_slices', 16))
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
    # 冷物品（不在训练候选集）按 MLlib coldStartStrategy=drop 口径剔除后评估
    pred_rows = [row for row in test if row[1] in scorer.i_index]
    dropped_cold = len(test) - len(pred_rows)
    u_arr = np.fromiter((u for u, _, _, _ in pred_rows), dtype=np.int64, count=len(pred_rows))
    i_arr = np.fromiter((scorer.i_index[i] for _, i, _, _ in pred_rows), dtype=np.int64, count=len(pred_rows))
    r_arr = np.fromiter((r for _, _, r, _ in pred_rows), dtype=np.float64, count=len(pred_rows))
    pred_pairs = scorer.pred_pairs(u_arr, i_arr, r_arr)
    all_preds = [p for p, _ in pred_pairs]
    out_of_range = sum(1 for p in all_preds if p < 1.0 or p > 5.0) / float(len(all_preds))
    rmse_rows = [
        {'model': 'ALS', 'rmse': rmse(pred_pairs)},
        {'model': 'Const(mu)', 'rmse': rmse([(model['mu'], r) for _, _, r, _ in pred_rows])},
    ]
    print('pred_rows=%d dropped_cold=%d' % (len(pred_rows), dropped_cold))

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
        w.writerow(['dropped_cold', '%d' % dropped_cold])
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


def write_csv(out, name, fields, rows):
    with open(os.path.join(out, name), 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def write_summary(out, summary):
    with open(os.path.join(out, 'summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, sort_keys=True, allow_nan=False)


def objective_diagnostics(values):
    finite = all(math.isfinite(value) for value in values)
    increases = [idx + 1 for idx in range(1, len(values))
                 if values[idx] - values[idx - 1] > 1e-8 * max(1., abs(values[idx - 1]))]
    changes = [(values[idx] - values[idx - 1]) / max(abs(values[idx - 1]), 1e-12)
               for idx in range(1, len(values))] if finite else []
    if not finite or increases:
        status = 'objective_anomaly'
    elif len(changes) >= 3 and all(abs(x) < .01 for x in changes[-3:]):
        status = 'small_recent_changes'
    else:
        status = 'still_changing'
    return dict(status=status, finite=finite, increase_iterations=increases,
                first_last_ratio=values[-1] / values[0] if finite and values and values[0] else None,
                last_three_relative_changes=changes[-3:],
                interpretation='Fixed iteration budget; small recent changes are heuristic, not proof of convergence.')


def run_experiment(spark, rows, args):
    """Explicit-model and three-way runs. Test rows are only counted, never evaluated."""
    os.makedirs(args.out, exist_ok=True)
    if args.split == 'three-way':
        train, validation, reserved = temporal_split_three_way(rows, args.test_frac, args.val_frac)
        assert reserved == temporal_split(rows, args.test_frac)[1]
        evaluation = validation
    else:
        train, evaluation = temporal_split(rows, args.test_frac)
        validation, reserved = [], evaluation
    train_users = {u for u, _, _, _ in train}
    assert all(u in train_users for u, _, _, _ in evaluation), 'evaluation user absent from train'
    candidates = sorted({i for _, i, _, _ in train})
    candidate_set = set(candidates)
    seen, relevant = {}, {}
    for u, i, _, _ in train:
        seen.setdefault(u, set()).add(i)
    for u, i, r, _ in evaluation:
        relevant.setdefault(u, set())
        if r >= 4.:
            relevant[u].add(i)
    users = sorted(u for u in relevant if relevant[u])
    support = Counter(i for _, i, _, _ in train)
    known_rows = [row for row in evaluation if row[1] in candidate_set]
    protocol = dict(rows=len(rows), train_rows=len(train), validation_rows=len(validation),
                    test_rows=len(reserved), evaluation_segment='validation' if args.split == 'three-way' else 'test',
                    evaluation_rows=len(evaluation), train_users=len(train_users),
                    candidate_items=len(candidates), evaluation_users=len(relevant),
                    users_scored=len(users), users_excluded_no_positive=len(relevant) - len(users),
                    users_without_evaluation=len(train_users - set(relevant)),
                    effective_prediction_rows=len(known_rows), dropped_cold_items=len(evaluation) - len(known_rows),
                    test_matches_legacy=True, relevant_threshold=4.0,
                    candidate_rule='train items minus own train seen; all distinct evaluation positives retained',
                    support_rule='train counts, recommendations pooled with cross-user duplicates retained',
                    prediction_bounds='raw < 0.5 or raw > 5.0',
                    split_order='timestamp,movieId,original_row_index',
                    interpretation='Exploratory validation; reserved test was used by an earlier pilot.')
    summary = dict(config=vars(args).copy(), protocol=protocol, models={}, status='running',
                   objective_values=[], objective_diagnostics=None)
    summary['config'].update(model_equation='mu + bu + bi + dot(P,Q)',
                             init_scale=0.1, nonnegative=False,
                             regularization='lambda times observation count; factors and biases',
                             mu='training mean, fixed and unregularized',
                             ranking='raw score descending, movieId ascending on ties')
    write_summary(args.out, summary)
    trace = []
    model = None

    def monitor(iteration, current, loss):
        value = objective(train, current, args.reg)
        entry = dict(iteration=iteration, train_rmse=loss, objective=value)
        trace.append(entry)
        write_csv(args.out, 'objectives.csv', ['iteration', 'train_rmse', 'objective'], trace)
        if not math.isfinite(value) or not math.isfinite(loss):
            summary['status'] = 'objective_anomaly'
            summary['failure_reason'] = 'non-finite training objective or RMSE at iteration %d' % iteration
            write_summary(args.out, summary)
            raise RuntimeError(summary['failure_reason'])
        summary['objective_values'].append(value)
        summary['objective_diagnostics'] = objective_diagnostics(summary['objective_values'])
        if summary['objective_diagnostics']['status'] == 'objective_anomaly':
            summary['status'] = 'objective_anomaly'
            summary['failure_reason'] = 'objective increased beyond tolerance at iteration %d' % iteration
            write_summary(args.out, summary)
            raise RuntimeError(summary['failure_reason'])
        write_summary(args.out, summary)

    if args.models in ('als', 'all'):
        model, _ = fit_spark(spark, train, rank=args.rank, reg=args.reg, iters=args.iters,
                            seed=args.seed, num_slices=args.num_slices, log=print, on_iter=monitor)
    write_csv(args.out, 'objectives.csv', ['iteration', 'train_rmse', 'objective'], trace)
    scorer = ModelScorer(model, candidates) if model is not None else None
    pop = most_popular(train, len(candidates))
    names = ['ALS'] if args.models == 'als' else ['MostPop'] if args.models == 'mostpop' else ['Random', 'MostPop', 'ALS']
    per_user, recommendations, results, prediction_rows, rmse_rows = [], [], [], [], []
    rec_key, ndcg_key = 'recall@%d' % args.top_n, 'ndcg@%d' % args.top_n
    for name in names:
        details, pooled_predictions, pooled_support = [], [], []
        for u in users:
            pool = candidate_set - seen[u]
            if name == 'ALS':
                excluded = [scorer.i_index[i] for i in seen[u]]
                rec = scorer.top_for_user(u, excluded, args.top_n)
            elif name == 'MostPop':
                rec = [i for i in pop if i in pool][:args.top_n]
            else:
                rec = random_top(pool, args.top_n, args.seed)
            detail = dict(model=name, user=u, relevant_count=len(relevant[u]),
                          candidate_count=len(pool), returned_count=len(rec),
                          recommendable_positive_ratio=recommendable_positive_ratio(relevant[u], pool))
            detail[rec_key] = recall_at_k(rec, relevant[u], args.top_n)
            detail[ndcg_key] = ndcg_at_k(rec, relevant[u], args.top_n)
            details.append(detail)
            per_user.append(detail)
            for position, i in enumerate(rec, 1):
                pred = predict_model(model, u, i) if name == 'ALS' else None
                if pred is not None:
                    pooled_predictions.append(pred)
                pooled_support.append(support[i])
                recommendations.append(dict(model=name, user=u, rank=position, movie_id=i,
                                            raw_prediction=pred, train_support=support[i]))
        result = dict(model=name, users_scored=len(users),
                      users_excluded_no_positive=protocol['users_excluded_no_positive'])
        for key in (rec_key, ndcg_key, 'recommendable_positive_ratio'):
            result[key] = sum(row[key] for row in details) / len(details) if details else None
        result['top_support_median'] = float(np.median(pooled_support)) if pooled_support else None
        result['top_recommendation_count'] = len(pooled_support)
        result['top_prediction_bounds'] = prediction_bounds(pooled_predictions)
        result['validation_prediction_bounds'] = prediction_bounds([])
        result['rmse'], result['const_rmse'] = None, None
        if name == 'ALS':
            for u, i, actual, ts in known_rows:
                prediction_rows.append(dict(user=u, movie_id=i, rating=actual, timestamp=ts,
                                            raw_prediction=predict_model(model, u, i), constant_prediction=model['mu']))
            pairs = [(r['raw_prediction'], r['rating']) for r in prediction_rows]
            result['validation_prediction_bounds'] = prediction_bounds([p for p, _ in pairs])
            result['rmse'] = rmse(pairs)
            result['const_rmse'] = rmse([(model['mu'], r) for _, r in pairs])
            rmse_rows.extend([dict(model='ALS', rmse=result['rmse']), dict(model='Const(mu)', rmse=result['const_rmse'])])
        else:
            rmse_rows.append(dict(model=name, rmse=None))
        summary['models'][name] = result
        results.append({key: result[key] for key in ('model', rec_key, ndcg_key, 'users_scored',
                        'users_excluded_no_positive', 'recommendable_positive_ratio', 'top_support_median', 'top_recommendation_count')})
    write_csv(args.out, 'results.csv', ['model', rec_key, ndcg_key, 'users_scored', 'users_excluded_no_positive',
                                      'recommendable_positive_ratio', 'top_support_median', 'top_recommendation_count'], results)
    write_csv(args.out, 'rmse.csv', ['model', 'rmse'], rmse_rows)
    write_csv(args.out, 'per_user.csv', ['model', 'user', 'relevant_count', 'candidate_count', 'returned_count',
                                       'recommendable_positive_ratio', rec_key, ndcg_key], per_user)
    write_csv(args.out, 'top_recommendations.csv', ['model', 'user', 'rank', 'movie_id', 'raw_prediction', 'train_support'], recommendations)
    write_csv(args.out, 'validation_predictions.csv', ['user', 'movie_id', 'rating', 'timestamp', 'raw_prediction', 'constant_prediction'], prediction_rows)
    summary['status'] = 'success'
    write_summary(args.out, summary)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--rank', type=int, default=50)
    ap.add_argument('--reg', type=float, default=.1)
    ap.add_argument('--iters', type=int, default=10)
    ap.add_argument('--top-n', type=int, default=20)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--split', choices=['two-way', 'three-way'], default='two-way')
    ap.add_argument('--val-frac', type=float, default=.2)
    ap.add_argument('--test-frac', type=float, default=.2)
    ap.add_argument('--num-slices', type=int, default=16)
    ap.add_argument('--models', choices=['als', 'mostpop', 'all'], default='all')
    args = ap.parse_args()
    if not 0 < args.test_frac < 1 or not 0 < args.val_frac < 1 or args.top_n < 1 or args.num_slices < 1:
        ap.error('fractions must be in (0,1); top-n and num-slices must be positive')
    if args.split == 'two-way' and args.models == 'all' and args.test_frac == .2:
        return legacy_main(args)
    from pyspark.sql import SparkSession
    spark = SparkSession.builder.appName('pilot-classic-als-grid').getOrCreate()
    try:
        # Workers must import the same solver even when spark-submit runs outside this directory.
        spark.sparkContext.addPyFile(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'als.py'))
        run_experiment(spark, load_rows(spark, args.input), args)
    finally:
        spark.stop()


if __name__ == '__main__':
    main()
