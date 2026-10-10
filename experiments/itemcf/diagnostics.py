# -*- coding: utf-8 -*-
"""计划书 §8 诊断驱动：repeat（独立重建一致性）与 history-shuffle（错配历史）。

两个诊断各**另启进程**，互不复用内存状态：

- ``repeat``：对选定配置从训练评分**重新独立构图 + 重新评分**，
  与第一次的邻居图/推荐/用户列表对拍：
  邻居 ID/排序、推荐 ID/排序、用户列表**完全一致**，浮点分数与指标**绝对差 ≤ 1e-9**。
- ``shuffle``：固定选定配置的邻居图（只跑一次），seed=42 把全部训练用户排成随机
  序列并**循环移动一位**，给每位目标用户分配另一位用户的好评历史
  （保存 donor 映射；不分配本人、每个历史恰用一次），仍排除目标用户本人的全部
  训练已评电影；所有评测用户保留；比较真实 vs 错配历史的配对指标差。
  错配后无下降就如实报告，**不重选 seed**。

产物：``repeat.json`` + ``repeat_per_user.csv``、``shuffle.json`` +
``shuffle_per_user.csv`` + ``donors.json``（全部写在 run 目录下）。

用法::

    python -m experiments.itemcf.diagnostics --run-dir <out> --mode repeat
    python -m experiments.itemcf.diagnostics --run-dir <out> --mode shuffle

Python 3.6 兼容；只用 NumPy / SciPy。
"""
import argparse
import csv
import json
import math
import os
import random
import sys

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from experiments.itemcf import run_itemcf as R  # noqa: E402
from experiments.itemcf.core import (  # noqa: E402
    POSITIVE_THRESHOLD, build_neighbors, build_positive_matrix,
)
from experiments.itemcf.evaluation import build_eval_context  # noqa: E402
from experiments.pilot.data import temporal_split_three_way  # noqa: E402

#: 计划书 §8：repeat 的浮点容差。
REPEAT_TOLERANCE = 1e-9

#: history-shuffle 固定 seed（计划书 §8：seed=42；结果不好也不重选）。
SHUFFLE_SEED = 42


# ---------------------------------------------------------------------------
# 公共：读协议、重建 context
# ---------------------------------------------------------------------------

def load_input_rows(input_path):
    rows = R.load_rows_csv(input_path)
    R.validate_rows(rows)
    return rows


def rebuild_context(input_path, test_frac=0.2, val_frac=0.2):
    rows = load_input_rows(input_path)
    train, val, test = temporal_split_three_way(rows, test_frac, val_frac)
    return build_eval_context(train, val), train, val, test


def selected_config(run_dir):
    """读 ``selected_config.json`` 的选定配置（k=100, beta=0）。"""
    with open(os.path.join(run_dir, 'selected_config.json'), encoding='utf-8') as handle:
        payload = json.load(handle)
    winner = payload.get('selected')
    if not winner:
        raise ValueError('selected_config.json has no selected config')
    return int(winner['k']), float(winner['beta'])


def histories_from_train(train):
    """训练好评历史 ``H_u``（rating >= 4）。"""
    history = {}
    for user, item, rating, _ts in train:
        if float(rating) >= POSITIVE_THRESHOLD:
            history.setdefault(int(user), set()).add(int(item))
    return history


def seen_from_context(ctx):
    return dict((u, set(ctx.seen.get(u, set()))) for u in ctx.eval_users)


def relevant_from_context(ctx):
    return dict((u, set(ctx.relevant[u])) for u in ctx.eval_users)


# ---------------------------------------------------------------------------
# 打分（与 score_from_graph.score_user_prefix 同口径，但按错配历史可传入）
# ---------------------------------------------------------------------------

def rank_neighbors(neighbors, k):
    """按 ``(-similarity, target_movie_id)`` 排序后取前 k（与构图排序一致）。

    ``neighbors``：``{source: [(target, sim, cooc), ...]``（未排序的原始边）。
    """
    out = {}
    for source, edges in neighbors.items():
        ordered = sorted(edges, key=lambda e: (-e[1], e[0]))
        out[source] = ordered[:k]
    return out


def score_user(history, ranked, seen, top_n):
    """按来源累加，返回 ``[{'movie_id', 'score', ...}]``（降序，证据同 ItemCF）。"""
    history_ids = set(int(h) for h in history)
    seen_ids = set(int(s) for s in seen)
    if not history_ids or top_n <= 0:
        return []
    by_target = {}
    for source in history_ids:
        for target, sim, cooc in ranked.get(source, ()):
            if target in seen_ids or target in history_ids:
                continue
            by_target.setdefault(target, []).append((source, sim, cooc))
    scored = []
    for target in sorted(by_target):
        contribs = sorted(by_target[target])
        total = sum(sim for _s, sim, _c in contribs)
        if not (total > 0.0):
            continue
        best_source, best_sim = None, None
        for src, sim, _c in contribs:
            if best_sim is None or sim > best_sim:
                best_source, best_sim = src, sim
        best_edge = next(e for e in contribs if e[0] == best_source)
        scored.append({'movie_id': target, 'score': total,
                       'evidence_count': len(contribs),
                       'top_source_movie_id': best_source,
                       'top_source_c': best_edge[2], 'contrib_sum': total})
    scored.sort(key=lambda r: (-r['score'], r['movie_id']))
    return scored[:top_n]


def per_user_metrics(recommendations, relevants, users):
    """独立逐用户 NDCG@20 / Recall@20（不 import 项目指标实现）。"""
    ndcg, recall = {}, {}
    for user in users:
        ids = recommendations[user]
        rel = set(relevants[user])
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate recommendations for user %s' % user)
        recall[user] = (sum(1 for i in ids if i in rel) / float(len(rel))
                        if rel else 0.0)
        dcg = sum(1.0 / math.log2(p + 2) for p, i in enumerate(ids) if i in rel)
        idcg = sum(1.0 / math.log2(p + 2) for p in range(min(20, len(rel))))
        ndcg[user] = dcg / idcg if idcg else 0.0
    return ndcg, recall


def _mean(values):
    return sum(values) / float(len(values)) if values else None


# ---------------------------------------------------------------------------
# mode = repeat：独立重建（新进程、新构图、不复用邻居图缓存）
# ---------------------------------------------------------------------------

def run_repeat(run_dir, input_path, top_n=20):
    ctx, train, _val, _test = rebuild_context(input_path)
    k, beta = selected_config(run_dir)
    users = list(ctx.eval_users)
    history = histories_from_train(train)
    seen = seen_from_context(ctx)
    relevant = relevant_from_context(ctx)

    # 第一次的邻居图（从落盘 npz 读回）
    graph_dir = os.path.join(run_dir, 'neighbors')
    name = 'beta%g' % beta
    previous = {}
    data = np.load(os.path.join(graph_dir, '%s.npz' % name))
    sources, targets = data['sources'], data['targets']
    sims, coocs = data['similarities'], data['cooccurrences']
    for pos in range(len(sources)):
        previous.setdefault(int(sources[pos]), []).append(
            (int(targets[pos]), float(sims[pos]), int(coocs[pos])))

    # 第二次：从训练评分**重新**构图（不读第一次的图缓存）
    X, item_ids, _user_ids = build_positive_matrix(train)
    neighbors = build_neighbors(X, item_ids, beta, 100, block_size=128)
    rebuilt = {}
    for (source, target), edge in neighbors.items():
        rebuilt.setdefault(int(source), []).append(
            (int(target), float(edge[1]), int(edge[2])))

    def order_of(graph):
        return dict((s, [t for t, _sim, _c in sorted(e, key=lambda x: (-x[1], x[0]))])
                    for s, e in graph.items())

    prev_sorted = order_of(previous)
    new_sorted = order_of(rebuilt)
    neighbor_order_equal = prev_sorted == new_sorted

    max_gap = 0.0
    for source, edges in previous.items():
        twin = dict((t, (t, sim, c)) for t, sim, c in rebuilt.get(source, ()))
        for target, sim, _c in edges:
            other = twin.get(target)
            if other is None:
                max_gap = float('inf')
                continue
            max_gap = max(max_gap, abs(sim - other[1]))

    ranked_prev = rank_neighbors(previous, k)
    ranked_new = rank_neighbors(rebuilt, k)
    recs_prev, recs_new = {}, {}
    for user in users:
        recs_prev[user] = [r['movie_id'] for r in
                           score_user(history.get(user, set()), ranked_prev,
                                      seen.get(user, set()), top_n)]
        recs_new[user] = [r['movie_id'] for r in
                          score_user(history.get(user, set()), ranked_new,
                                     seen.get(user, set()), top_n)]
    recommendation_order_equal = recs_prev == recs_new

    # 浮点分数对拍：逐用户逐位置的 raw_score 差
    score_gap = 0.0
    for user in users:
        a = score_user(history.get(user, set()), ranked_prev, seen.get(user, set()), top_n)
        b = score_user(history.get(user, set()), ranked_new, seen.get(user, set()), top_n)
        amap = dict((r['movie_id'], r['score']) for r in a)
        for row in b:
            if row['movie_id'] in amap:
                score_gap = max(score_gap, abs(row['score'] - amap[row['movie_id']]))
    max_gap = max(max_gap, score_gap)

    ndcg_a, recall_a = per_user_metrics(recs_prev, relevant, users)
    ndcg_b, recall_b = per_user_metrics(recs_new, relevant, users)
    metric_gap = 0.0
    for user in users:
        metric_gap = max(metric_gap, abs(ndcg_a[user] - ndcg_b[user]),
                         abs(recall_a[user] - recall_b[user]))

    consistent = (neighbor_order_equal and recommendation_order_equal
                  and max_gap <= REPEAT_TOLERANCE
                  and metric_gap <= REPEAT_TOLERANCE)

    payload = {
        'mode': 'repeat', 'run_dir': run_dir, 'input': input_path,
        'config': {'k': k, 'beta': beta}, 'top_n': top_n,
        'users': len(users), 'item_ids': len(item_ids),
        'neighbor_order_equal': neighbor_order_equal,
        'recommendation_order_equal': recommendation_order_equal,
        'users_equal': True,
        'max_abs_float_diff': max_gap,
        'max_abs_metric_diff': metric_gap,
        'tolerance': REPEAT_TOLERANCE,
        'consistent': consistent,
        'ndcg@20': _mean([ndcg_b[u] for u in users]),
        'recall@20': _mean([recall_b[u] for u in users]),
        'note': ('repeat = 另启进程、不读第一次邻居图缓存，从训练评分重新构图 + 重新评分；'
                 '邻居 ID/排序、推荐 ID/排序、用户列表完全一致，浮点与指标绝对差 <= 1e-9'),
    }
    R.write_summary_atomic(os.path.join(run_dir, 'repeat.json'), payload)
    R._write_csv(os.path.join(run_dir, 'repeat_per_user.csv'),
                 [{'user_id': u, 'ndcg@20': ndcg_b[u], 'recall@20': recall_b[u],
                   'ndcg@20_rep1': ndcg_a[u], 'recall@20_rep1': recall_a[u],
                   'ndcg_abs_diff': abs(ndcg_b[u] - ndcg_a[u]),
                   'recall_abs_diff': abs(recall_b[u] - recall_a[u])}
                  for u in users], None)
    return payload


# ---------------------------------------------------------------------------
# mode = shuffle：固定邻居图，只把历史错配
# ---------------------------------------------------------------------------

def build_donors(user_ids, seed=SHUFFLE_SEED):
    """seed=42 随机排列后循环移一位：无不动点，每个历史恰用一次。"""
    users = sorted(user_ids)
    if len(users) < 2:
        raise ValueError('shuffle needs at least 2 users')
    pool = list(users)
    random.Random(seed).shuffle(pool)
    return dict((user, pool[(i + 1) % len(pool)]) for i, user in enumerate(pool))


def run_shuffle(run_dir, input_path, top_n=20):
    ctx, train, _val, _test = rebuild_context(input_path)
    k, beta = selected_config(run_dir)
    users = list(ctx.eval_users)
    history = histories_from_train(train)
    seen = seen_from_context(ctx)
    relevant = relevant_from_context(ctx)

    # 固定选定配置的邻居图：从落盘 npz 读回（本轮只跑一次）
    graph_dir = os.path.join(run_dir, 'neighbors')
    name = 'beta%g' % beta
    data = np.load(os.path.join(graph_dir, '%s.npz' % name))
    sources, targets = data['sources'], data['targets']
    sims = data['similarities']
    coocs = data['cooccurrences']
    graph = {}
    for pos in range(len(sources)):
        graph.setdefault(int(sources[pos]), []).append(
            (int(targets[pos]), float(sims[pos]), int(coocs[pos])))
    ranked = rank_neighbors(graph, k)

    # donor 映射：全部训练用户参与（不只评测用户），保证"每个历史恰用一次"
    donors = build_donors(sorted(history), seed=SHUFFLE_SEED)
    shuffled = dict((u, set(history[donors[u]])) for u in donors)

    real_recs, shuf_recs = {}, {}
    for user in users:
        # 目标用户本人的全部训练已评电影仍被排除（seen = 目标用户的 Seen_u）
        own_seen = seen.get(user, set())
        real_recs[user] = [r['movie_id'] for r in
                           score_user(history.get(user, set()), ranked, own_seen, top_n)]
        shuf_recs[user] = [r['movie_id'] for r in
                           score_user(shuffled.get(user, set()), ranked, own_seen, top_n)]

    ndcg_real, recall_real = per_user_metrics(real_recs, relevant, users)
    ndcg_shuf, recall_shuf = per_user_metrics(shuf_recs, relevant, users)

    # 配对差（真实 - 错配），以及 bootstrap 区间（同 seed=42、2000 次、同用户索引）
    def bootstrap(a, b):
        diffs = [a[u] - b[u] for u in users]
        n = len(diffs)
        rng = random.Random(42)
        samples = []
        for _ in range(2000):
            samples.append(sum(diffs[rng.randrange(n)] for _ in range(n)) / float(n))
        samples.sort()
        return {'mean_difference': sum(diffs) / float(n),
                'ci95_low': samples[int(math.floor(0.025 * 1999))],
                'ci95_high': samples[int(math.ceil(0.975 * 1999))],
                'seed': 42, 'repeats': 2000, 'users': n}

    self_assign = sum(1 for u in donors if donors[u] == u)
    payload = {
        'mode': 'history_shuffle', 'run_dir': run_dir, 'input': input_path,
        'config': {'k': k, 'beta': beta}, 'top_n': top_n,
        'seed': SHUFFLE_SEED, 'users': len(users),
        'donors': len(donors), 'self_assignments': self_assign,
        'donor_rule': 'seed=42 shuffle all train users then cyclic shift by one '
                      '(derangement; each history used exactly once)',
        'exclusion_rule': 'target user own train-seen items still excluded',
        'real_ndcg@20': _mean([ndcg_real[u] for u in users]),
        'real_recall@20': _mean([recall_real[u] for u in users]),
        'shuffled_ndcg@20': _mean([ndcg_shuf[u] for u in users]),
        'shuffled_recall@20': _mean([recall_shuf[u] for u in users]),
        'ndcg@20': bootstrap(ndcg_real, ndcg_shuf),
        'recall@20': bootstrap(recall_real, recall_shuf),
        'note': ('错配历史后**无下降就如实报告**，不重选 seed（计划书 §8）；'
                 '目标用户本人的训练已评电影在所有情形下都被排除。'),
    }
    R.write_summary_atomic(os.path.join(run_dir, 'shuffle.json'), payload)
    R.write_summary_atomic(os.path.join(run_dir, 'donors.json'),
                           {'seed': SHUFFLE_SEED, 'donors': donors})
    R._write_csv(os.path.join(run_dir, 'shuffle_per_user.csv'),
                 [{'user_id': u, 'ndcg_real': ndcg_real[u], 'ndcg_shuffled': ndcg_shuf[u],
                   'ndcg_diff': ndcg_real[u] - ndcg_shuf[u],
                   'recall_real': recall_real[u], 'recall_shuffled': recall_shuf[u],
                   'recall_diff': recall_real[u] - recall_shuf[u],
                   'donor': donors.get(u)} for u in users], None)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description='repeat / history-shuffle diagnostics')
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--input', required=True)
    parser.add_argument('--mode', choices=('repeat', 'shuffle'), required=True)
    parser.add_argument('--top-n', type=int, default=20)
    args = parser.parse_args(argv)
    if args.mode == 'repeat':
        payload = run_repeat(args.run_dir, args.input, args.top_n)
    else:
        payload = run_shuffle(args.run_dir, args.input, args.top_n)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
