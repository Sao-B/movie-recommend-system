# -*- coding: utf-8 -*-
"""从一张已落盘的 β 邻居图按 k 前缀打分（计划书 5.1 / 4.1）。

一个子进程只做**一件事**：读图 + 读输入（自己切分，与构图脚本同口径）、按
``k`` 截断每个来源的邻居、逐用户打分、算逐用户指标与聚合诊断、写
``configs/<k>_beta<b>/summary.json``。

k 前缀语义：图上按 ``(-similarity, target_movie_id)`` 已排好序，取前 k 条即
"该 β 下 k=..." 的邻居集（与单独按 k 构图等价，因为 top-k 是嵌套前缀）。

Python 3.6 兼容；只依赖 NumPy / SciPy。
"""
import argparse
import json
import os
import sys
import time

import numpy as np

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import run_itemcf as R  # noqa: E402
from experiments.itemcf.evaluation import (  # noqa: E402
    DEFAULT_K_VALUES, build_eval_context, per_user_metrics, summarize,
)
from experiments.pilot.data import temporal_split_three_way  # noqa: E402


def load_graph(graph_path, mapping_path, k):
    """读 npz + 映射，按 ``k`` 截断成 ``{(source, target): (target, sim, c)}``。

    截断依据是**文件里已排序的顺序**：每个来源的前 k 条（构图时按
    ``(-similarity, target_movie_id)`` 排序并截断 max_k），所以 k 前缀是精确的。
    """
    with open(mapping_path, encoding='utf-8') as handle:
        mapping = json.load(handle)
    data = np.load(graph_path)
    sources = data['sources']
    targets = data['targets']
    sims = data['similarities']
    coocs = data['cooccurrences']
    seen_per_source = {}
    neighbors = {}
    for pos in range(len(sources)):
        source = int(sources[pos])
        rank = seen_per_source.get(source, 0)
        if rank >= k:
            continue
        seen_per_source[source] = rank + 1
        target = int(targets[pos])
        neighbors[(source, target)] = (target, float(sims[pos]), int(coocs[pos]))
    return neighbors, mapping


def score_user_prefix(history, neighbors, seen, top_n, return_evidence=False):
    """按来源顺序累加得分，返回 ``(-score, movie_id)`` 排序的 Top-N（含证据）。

    与 :func:`experiments.itemcf.core.score_user` 同口径，但只做一遍邻居遍历
    （按来源分桶），避免对每个来源都全表扫描一次邻居字典。

    :param return_evidence: 为 True 时返回 ``(ranked, evidence_ids)``：

      - ``ranked``：Top-N（截断前按 ``(-score, movie_id)`` 排序）；
      - ``evidence_ids``：**全部**有邻域证据的候选 ``E_u``，即计划书 3.3 定义的
        ``E_u = {i in C_u : score(u,i) > 0}``，**不按 top_n 截断**。

    必须区分这两个量：``E_u`` 是"证据集"，用于计划书 §6.2 的
    ``min(20, |R_u∩E_u|)/|R_u|`` 证据上限与 ``|E_u|`` 证据候选数分布；而
    Top-N 只是它的前缀。早先调用方拿 Top-N 长度当证据候选数、又根本没把
    ``E_u`` 传给 :func:`summarize`，两个证据诊断恒为 N/A。
    """
    history_ids = set(int(h) for h in history)
    seen_ids = set(int(s) for s in seen)
    if not history_ids or top_n <= 0:
        return ([], []) if return_evidence else []
    by_source = {}
    for (source, target), edge in neighbors.items():
        if source not in history_ids:
            continue
        if target in seen_ids or target in history_ids:
            continue
        by_source.setdefault(target, []).append((source, edge[1], edge[2]))
    scored = []
    for target in sorted(by_source):
        contribs = sorted(by_source[target])
        total = 0.0
        for _src, sim, _c in contribs:
            total += sim
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
                       'top_source_c': best_edge[2],
                       'contrib_sum': total})
    scored.sort(key=lambda r: (-r['score'], r['movie_id']))
    ranked = scored[:top_n]
    if return_evidence:
        return ranked, [row['movie_id'] for row in scored]
    return ranked


def main(argv=None):
    parser = argparse.ArgumentParser(description='Score users from a saved graph')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--graph', required=True)
    parser.add_argument('--mapping', required=True)
    parser.add_argument('--name', required=True)
    parser.add_argument('--neighbor-k', type=int, required=True)
    parser.add_argument('--beta', type=float, required=True)
    parser.add_argument('--top-n', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--test-frac', type=float, default=0.2)
    parser.add_argument('--val-frac', type=float, default=0.2)
    args = parser.parse_args(argv)

    started = time.time()
    rows = R.load_rows_csv(args.input)
    R.validate_rows(rows)
    train, val, _test = temporal_split_three_way(rows, args.test_frac, args.val_frac)
    ctx = build_eval_context(train, val)
    neighbors, mapping = load_graph(args.graph, args.mapping, args.neighbor_k)

    history = {}
    for user, item, rating, _ts in train:
        if float(rating) >= 4.0:
            history.setdefault(user, set()).add(item)

    users = list(ctx.eval_users)
    recommendations, relevants, per_user_rows, rec_rows = [], [], [], []
    evidence_by_index = {}
    for index, user in enumerate(users):
        ranked, evidence_ids = score_user_prefix(
            history.get(user, set()), neighbors, ctx.seen.get(user, set()),
            args.top_n, return_evidence=True)
        evidence_by_index[index] = set(evidence_ids)
        recs = [r['movie_id'] for r in ranked]
        recommendations.append(recs)
        relevants.append(ctx.relevant[user])
        rel = ctx.relevant[user]
        per_user_rows.append({
            'user_id': user, 'relevant_count': len(rel),
            'relevant_ids': '|'.join(str(i) for i in sorted(rel)),
            'candidate_count': len(ctx.candidates.get(user, ())),
            # 证据候选数 = |E_u|（全部有邻域证据的候选），不是 Top-N 长度。
            'evidence_count': len(evidence_ids),
            'returned_count': len(recs),
            'history_len': len(history.get(user, set())),
        })
        for rank, item in enumerate(ranked, 1):
            rec_rows.append({'user_id': user, 'model': args.name, 'rank': rank,
                             'movie_id': item['movie_id'],
                             'raw_score': item['score'],
                             'n_all': ctx.n_all.get(item['movie_id'], 0),
                             'n_like': ctx.n_like.get(item['movie_id'], 0),
                             'evidence_neighbor_count': item['evidence_count'],
                             'top_source_movie_id': item['top_source_movie_id'],
                             'top_source_c': item['top_source_c'],
                             'contrib_sum': item['contrib_sum'],
                             'top_source_n_like': ctx.n_like.get(
                                 item['top_source_movie_id'], 0)})

    # 逐用户指标由 evaluation 的纯函数算（口径与 verify_results 一致）
    metrics = per_user_metrics(recommendations, relevants, k_values=DEFAULT_K_VALUES)
    candidates_by_index = dict(
        enumerate([ctx.candidates.get(u, set()) for u in users]))
    aggregate = summarize(recommendations, relevants, users,
                          k_values=DEFAULT_K_VALUES, top_n=args.top_n,
                          item_universe=ctx.item_ids, n_all=ctx.n_all,
                          candidates=candidates_by_index,
                          evidence=evidence_by_index)
    for index, row in enumerate(per_user_rows):
        row['ndcg@20'] = metrics['ndcg@20'][index]
        row['recall@20'] = metrics['recall@20'][index]
        row['ndcg@10'] = metrics['ndcg@10'][index]
        row['recall@10'] = metrics['recall@10'][index]
        row['ndcg@5'] = metrics['ndcg@5'][index]
        row['recall@5'] = metrics['recall@5'][index]

    model = {
        'ndcg@20': aggregate['ndcg@20'], 'recall@20': aggregate['recall@20'],
        'ndcg@10': aggregate['ndcg@10'], 'recall@10': aggregate['recall@10'],
        'ndcg@5': aggregate['ndcg@5'], 'recall@5': aggregate['recall@5'],
        'users_scored': aggregate['users_scored'],
        'full_list_rate': aggregate['full_list_rate'],
        'fill_rate': aggregate['fill_rate'],
        'empty_list_rate': aggregate['empty_list_rate'],
        'catalog_coverage': aggregate['catalog_coverage'],
        'candidate_coverage': aggregate['candidate_coverage'],
        'support_median': aggregate['recommended_popularity_p50'],
        'recommended_popularity_mean': aggregate['recommended_popularity_mean'],
        'low_support_rate': aggregate['low_support_rate'],
        'returned_count': aggregate['returned_count'],
        # 计划书 §6.2 证据诊断（此前恒为 N/A：逐用户 E_u 没传给 summarize）。
        'evidence_positive_coverage': aggregate['evidence_positive_coverage'],
        'evidence_max_recall@20': aggregate['evidence_max_recall@20'],
        'evidence_max_recall@10': aggregate['evidence_max_recall@10'],
        'evidence_max_recall@5': aggregate['evidence_max_recall@5'],
        'evidence_count_p10': aggregate['evidence_count_p10'],
        'evidence_count_p50': aggregate['evidence_count_p50'],
        'evidence_count_p90': aggregate['evidence_count_p90'],
        'evidence_count_zero_users': aggregate['evidence_count_zero_users'],
        'rmse': None, 'out_of_range_rate': None,
    }
    payload = {'status': 'success', 'name': args.name,
               'config': {'name': args.name, 'k': args.neighbor_k,
                          'beta': args.beta, 'top_n': args.top_n,
                          'seed': args.seed, 'max_k': mapping.get('max_k'),
                          'graph': os.path.abspath(args.graph)},
               'models': {args.name: model},
               'per_user': per_user_rows, 'recommendations': rec_rows,
               'seconds': time.time() - started}
    folder = os.path.join(args.out, 'configs', args.name)
    if not os.path.isdir(folder):
        os.makedirs(folder)
    R.write_summary_atomic(os.path.join(folder, 'summary.json'), payload)
    print(json.dumps({'name': args.name, 'ndcg@20': model['ndcg@20'],
                      'recall@20': model['recall@20'],
                      'seconds': payload['seconds']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
