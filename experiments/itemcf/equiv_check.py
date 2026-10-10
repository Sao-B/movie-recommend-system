# -*- coding: utf-8 -*-
"""独立等价性对拍：不复用被验证的实现，从原始评分重算 ItemCF 并与旧基准逐条比较。

**为什么单独写一份**：如果对拍脚本 import ``experiments.itemcf.core``，那么它验证的
是"同一份代码跑两次结果一样"，而不是"新实现复现了旧结果"。计划书 §4 第 4 项要求的是
后者，所以这里**独立实现**相似度、邻居截断与打分，只用标准库 + NumPy。

对拍目标（旧 2k 最佳配置 k=100 / beta=0，三段 64/16/20 切分）：

- 推荐 ID 与顺序：1930 个评测用户逐人完全一致；
- 裸分数：差 <= 1e-9；
- NDCG@20 / Recall@20：差 <= 1e-10。

对照物：``experiments/itemcf/out/20261004T_fix14/recommendations.csv``（``k100_beta0``）
与 ``per_user.csv``。文件名与哈希记录在本目录 ``FROZEN_BASELINE.md``。

对拍模式**显式使用旧三段切分**（test_frac=0.2, val_frac=0.2），与正式确认的 80/20 分开，
避免两种口径混淆。

用法::

    python -m experiments.itemcf.equiv_check --input <pilot_ratings.csv> \
        --baseline-dir experiments/itemcf/out/20261004T_fix14

Python 3.6 兼容；只用标准库 + NumPy。
"""
import argparse
import csv
import hashlib
import json
import math
import os
import sys
from collections import defaultdict

import numpy as np

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

#: 对拍口径固定值（旧基准就是这套）。
POSITIVE_THRESHOLD = 4.0
NEIGHBOR_K = 100
BETA = 0.0
TOP_N = 20
TEST_FRAC = 0.2
VAL_FRAC = 0.2

#: 旧基准的 `recommendations.csv` 中 model 列取值。
BASELINE_MODEL = 'k100_beta0'

#: 容差（计划书 §4 第 4 项）。
SCORE_TOLERANCE = 1e-9
METRIC_TOLERANCE = 1e-10

#: 冻结的旧基准文件哈希（与 FROZEN_BASELINE.md 一致，用于发现基准被换过）。
#:
#: ``per_user.csv`` 有**两个合法版本**（见 FROZEN_BASELINE.md §2.1）：VM 上 05:01 的
#: 后置报告步骤重写过它（补算证据诊断列），本地镜像保存的是 03:44 版本。两者对
#: 对拍真正用到的 ``ndcg@20`` / ``recall@20`` / ``relevant_ids`` 三列数值一致。
#: 因此这里接受"任一已知版本"，但把实际命中的版本记进报告；出现**第三个**哈希
#: 才判定为基准被换过。
FROZEN_BASELINE_HASHES = {
    'recommendations.csv': {
        '7bcdcacd02412817face2af9bc499cc2283112ac0f107670843b02fee1f2474a':
            'local_mirror_0314',
    },
    'per_user.csv': {
        '442e7062b92b67a91d2ce717f996b935a2cd3bbf071fa88a54e5e52effbe00c7':
            'local_mirror_0344',
        '4b8acef93882fea84bafde5822fcb49f22d98fc83cffc61ae844bbc5cb1f29b0':
            'vm_regenerated_0501',
    },
}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(1 << 20)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_rows_csv(path):
    """读 ``(userId, movieId, rating, timestamp)``；用完即释放（调用方控制生命周期）。"""
    rows = []
    with open(path, newline='') as handle:
        for record in csv.DictReader(handle):
            rows.append((int(record['userId']), int(record['movieId']),
                         float(record['rating']), int(record['timestamp'])))
    return rows


def temporal_split_two_way(rows, test_frac=TEST_FRAC, val_frac=VAL_FRAC):
    """旧 64/16/20 三段切分的**前两段**：先留最后 20% 作 test，再从余下取同样规则。

    与 ``experiments.pilot.data.temporal_split_three_way(rows, 0.2, 0.2)`` 的
    train/val 完全同构（这里为独立实现，不 import 被验证代码）：

    - ``nt = min(max(1, int(n*0.2)), n-1)``（n>=2），``rest = n - nt``
    - ``nv = min(max(1, int(rest*0.2)), rest-1)``（rest>=2）
    - train = entries[:rest-nv]，val = entries[rest-nv:rest]

    返回 ``(train, val)``。
    """
    by_user = defaultdict(list)
    for index, row in enumerate(rows):
        by_user[row[0]].append((index, row))
    train_idx, val_idx = [], []
    for entries in by_user.values():
        entries.sort(key=lambda t: (t[1][3], t[1][1], t[0]))
        n = len(entries)
        nt = min(max(1, int(n * test_frac)), n - 1) if n >= 2 else 0
        rest = n - nt
        nv = min(max(1, int(rest * val_frac)), rest - 1) if rest >= 2 else 0
        train_idx.extend(index for index, _ in entries[:rest - nv])
        val_idx.extend(index for index, _ in entries[rest - nv:rest])
    return ([rows[i] for i in sorted(train_idx)],
            [rows[i] for i in sorted(val_idx)])


def build_positive_matrix(train):
    """0/1 好评矩阵（userId x movieId），只含训练段。返回 (X, item_ids, user_ids)。"""
    user_ids, item_ids = set(), set()
    pairs = []
    for user, item, rating, _ts in train:
        user_ids.add(user)
        item_ids.add(item)
        if rating >= POSITIVE_THRESHOLD:
            pairs.append((user, item))
    user_list = sorted(user_ids)
    item_list = sorted(item_ids)
    user_index = dict((u, p) for p, u in enumerate(user_list))
    item_index = dict((i, p) for p, i in enumerate(item_list))
    if not pairs:
        from scipy import sparse
        return sparse.csr_matrix((len(user_list), len(item_list)), dtype=np.int64), \
            item_list, user_list
    from scipy import sparse
    data = np.ones(len(pairs), dtype=np.int64)
    row_idx = np.fromiter((user_index[u] for u, _ in pairs), dtype=np.int64,
                          count=len(pairs))
    col_idx = np.fromiter((item_index[i] for _, i in pairs), dtype=np.int64,
                          count=len(pairs))
    X = sparse.csr_matrix((data, (row_idx, col_idx)),
                          shape=(len(user_list), len(item_list)), dtype=np.int64)
    return X, item_list, user_list


def build_neighbors_independent(X, item_ids, beta=BETA, max_k=NEIGHBOR_K, block_size=128):
    """独立实现的有向 top-k 邻居图。

    与 ``core.build_neighbors`` **数学等价但分别实现**：分块算稀疏共现，先按 beta
    收缩得到相似度，再在收缩后的相似度上按 ``(-sim, target_movieId)`` 取前 max_k。
    ``beta=0`` 时收缩系数恒为 1。

    :returns: ``{source_movieId: [(target_movieId, sim), ...]}``，已按相似度降序、
    平票目标 ID 升序排序（**真正按相似度排序**，不留旧存储的顺序陷阱）。
    """
    n_items = len(item_ids)
    if n_items == 0 or max_k == 0:
        return {}
    X = X.tocsr().astype(np.int64)
    n_like = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)
    graph = {}
    for start in range(0, n_items, block_size):
        stop = min(start + block_size, n_items)
        block = X[:, start:stop]
        coo = (block.T @ X).tocoo()
        buckets = {}
        for r, c, val in zip(coo.row, coo.col, coo.data):
            j = start + int(r)
            i = int(c)
            if j == i:
                continue
            c_ij = int(val)
            if c_ij <= 0:
                continue
            nj = int(n_like[j])
            ni = int(n_like[i])
            if nj <= 0 or ni <= 0:
                continue
            cos = c_ij / float(np.sqrt(nj * ni))
            sim = cos * (c_ij / float(c_ij + beta)) if beta > 0.0 else cos
            if not np.isfinite(sim) or sim < 0.0 or sim > 1.0 + 1e-12:
                raise ValueError('bad similarity s(%r,%r)=%r'
                                 % (item_ids[j], item_ids[i], sim))
            buckets.setdefault(j, []).append((i, sim))
        for j in sorted(buckets):
            ordered = sorted(buckets[j], key=lambda e: (-e[1], item_ids[e[0]]))
            kept = []
            for i, sim in ordered:
                if sim <= 0.0 or len(kept) >= max_k:
                    break
                kept.append((item_ids[i], float(sim)))
            graph[item_ids[j]] = kept
        del coo, buckets
    return graph


def score_users(graph, train, eval_users):
    """逐用户打分：候选 = 邻居可达 - seen，按 ``(-score, movieId)`` 取 Top-20。

    浮点贡献按**来源 movieId 升序**累加（与 core 同口径，避免并列排序抖动）。
    ``score > 0`` 才进候选，短列表合法（不用零分/热门填满）。
    """
    history, seen = {}, {}
    for user, item, rating, _ts in train:
        seen.setdefault(user, set()).add(item)
        if rating >= POSITIVE_THRESHOLD:
            history.setdefault(user, set()).add(item)

    results = {}
    for user in eval_users:
        sources = history.get(user, set())
        excluded = seen.get(user, set())
        if not sources:
            results[user] = []
            continue
        contrib = {}
        for source in sorted(sources):
            for target, sim in graph.get(source, ()):
                if target in excluded or target in sources:
                    continue
                contrib.setdefault(target, []).append(sim)
        scored = []
        for target in sorted(contrib):
            total = 0.0
            for sim in contrib[target]:
                total += sim
            if total > 0.0:
                scored.append((target, total))
        scored.sort(key=lambda t: (-t[1], t[0]))
        results[user] = scored[:TOP_N]
    return results


def ndcg_at_k(recs, relevant, k=20):
    dcg = sum(1.0 / math.log2(pos + 2) for pos, item in enumerate(recs[:k])
              if item in relevant)
    idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(k, len(relevant))))
    return dcg / idcg if idcg else 0.0


def recall_at_k(recs, relevant, k=20):
    if not relevant:
        return 0.0
    return sum(1 for item in recs[:k] if item in relevant) / float(len(relevant))


def load_baseline(baseline_dir):
    """读旧基准的 ``recommendations.csv``（k100_beta0）与 ``per_user.csv``。"""
    rec_path = os.path.join(baseline_dir, 'recommendations.csv')
    per_user_path = os.path.join(baseline_dir, 'per_user.csv')
    hashes = {}
    for name, path in (('recommendations.csv', rec_path), ('per_user.csv', per_user_path)):
        hashes[name] = sha256_file(path)

    recs = {}
    with open(rec_path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row.get('model') != BASELINE_MODEL:
                continue
            user = int(row['user_id'])
            recs.setdefault(user, []).append(
                (int(row['rank']), int(row['movie_id']), float(row['raw_score'])))
    for user in recs:
        recs[user].sort()

    per_user = {}
    with open(per_user_path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if row.get('model') != BASELINE_MODEL:
                continue
            user = int(row['user_id'])
            rel = set(int(x) for x in (row.get('relevant_ids') or '').split('|') if x)
            per_user[user] = {
                'relevant': rel,
                'ndcg@20': float(row['ndcg@20']),
                'recall@20': float(row['recall@20']),
            }
    return recs, per_user, hashes


def compare(mine, baseline_recs, baseline_per_user):
    """逐用户比较；返回报告 dict（含全部不一致明细，不只报个数）。"""
    report = {'users_mine': len(mine), 'users_baseline': len(baseline_recs),
              'id_order_mismatches': [], 'score_mismatches': [],
              'metric_mismatches': [], 'missing_users': []}
    all_users = sorted(set(mine) | set(baseline_recs))
    max_score_gap = 0.0
    max_ndcg_gap = 0.0
    max_recall_gap = 0.0
    for user in all_users:
        if user not in mine or user not in baseline_recs:
            report['missing_users'].append(user)
            continue
        mine_ids = [item for item, _s in mine[user]]
        base_rows = baseline_recs[user]
        base_ids = [item for _r, item, _s in base_rows]
        if mine_ids != base_ids:
            report['id_order_mismatches'].append({
                'user_id': user, 'mine': mine_ids[:5], 'baseline': base_ids[:5],
                'mine_len': len(mine_ids), 'baseline_len': len(base_ids)})
        for (item, score), (_rank, base_item, base_score) in zip(mine[user], base_rows):
            if item != base_item:
                continue
            gap = abs(float(score) - float(base_score))
            max_score_gap = max(max_score_gap, gap)
            if gap > SCORE_TOLERANCE:
                report['score_mismatches'].append(
                    {'user_id': user, 'movie_id': item,
                     'mine': score, 'baseline': base_score, 'gap': gap})
        entry = baseline_per_user.get(user)
        if entry is not None:
            rel = entry['relevant']
            my_ndcg = ndcg_at_k(mine_ids, rel, 20)
            my_recall = recall_at_k(mine_ids, rel, 20)
            ndcg_gap = abs(my_ndcg - entry['ndcg@20'])
            recall_gap = abs(my_recall - entry['recall@20'])
            max_ndcg_gap = max(max_ndcg_gap, ndcg_gap)
            max_recall_gap = max(max_recall_gap, recall_gap)
            if ndcg_gap > METRIC_TOLERANCE or recall_gap > METRIC_TOLERANCE:
                report['metric_mismatches'].append(
                    {'user_id': user, 'my_ndcg': my_ndcg,
                     'base_ndcg': entry['ndcg@20'], 'my_recall': my_recall,
                     'base_recall': entry['recall@20']})
    report['max_score_gap'] = max_score_gap
    report['max_ndcg_gap'] = max_ndcg_gap
    report['max_recall_gap'] = max_recall_gap
    report['score_tolerance'] = SCORE_TOLERANCE
    report['metric_tolerance'] = METRIC_TOLERANCE
    report['pass'] = (not report['id_order_mismatches']
                      and not report['score_mismatches']
                      and not report['metric_mismatches']
                      and not report['missing_users'])
    return report


def check_frozen_hashes(hashes):
    """基准文件哈希必须是**已记录的已知版本**，否则不允许把对拍当验收依据。

    :returns: ``(problems, matched)``：``problems`` 是未知哈希的文件清单，
    ``matched`` 是 ``{文件名: 命中的版本名}``（便于报告里写清用的是哪个 vintage）。
    """
    problems = []
    matched = {}
    for name, accepted in sorted(FROZEN_BASELINE_HASHES.items()):
        actual = hashes.get(name)
        if actual is None:
            problems.append('%s: missing' % name)
            continue
        label = accepted.get(actual)
        if label is None:
            problems.append('%s: %s matches no frozen version (%s)'
                            % (name, actual, '/'.join(sorted(accepted.values()))))
        else:
            matched[name] = label
    return problems, matched


def run(input_path, baseline_dir):
    """执行对拍；返回报告 dict（不写盘，由调用方决定落点）。"""
    if not os.path.isfile(input_path):
        raise IOError('input not found: %s' % input_path)
    baseline_recs, baseline_per_user, hashes = load_baseline(baseline_dir)
    frozen_problems, frozen_matched = check_frozen_hashes(hashes)
    rows = load_rows_csv(input_path)
    train, val = temporal_split_two_way(rows, TEST_FRAC)
    del rows
    X, item_ids, _user_ids = build_positive_matrix(train)
    graph = build_neighbors_independent(X, item_ids, BETA, NEIGHBOR_K)
    del X
    eval_users = sorted(set(user for user, _i, rating, _ts in val
                            if rating >= POSITIVE_THRESHOLD))
    # 只保留有 >=4 分正例的评测用户（旧基准 1930 人的口径）
    relevant = {}
    for user, item, rating, _ts in val:
        if rating >= POSITIVE_THRESHOLD:
            relevant.setdefault(user, set()).add(item)
    eval_users = sorted(u for u, rel in relevant.items() if rel)
    del val
    mine = score_users(graph, train, eval_users)
    report = compare(mine, baseline_recs, baseline_per_user)
    report['input'] = os.path.abspath(input_path)
    report['baseline_dir'] = os.path.abspath(baseline_dir)
    report['baseline_hashes'] = hashes
    report['frozen_hash_problems'] = frozen_problems
    report['frozen_hash_matched'] = frozen_matched
    report['split'] = {'mode': 'legacy_three_way_train_val',
                       'test_frac': TEST_FRAC, 'val_frac': VAL_FRAC,
                       'train_rows': len(train), 'val_rows': len(relevant)}
    report['candidate_items'] = len(item_ids)
    report['graph_sources'] = len(graph)
    report['graph_edges'] = sum(len(v) for v in graph.values())
    if frozen_problems:
        report['pass'] = False
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent ItemCF equivalence check')
    parser.add_argument('--input', required=True)
    parser.add_argument('--baseline-dir', required=True)
    parser.add_argument('--out', default=None)
    args = parser.parse_args(argv)
    report = run(args.input, args.baseline_dir)
    if args.out:
        with open(args.out, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, sort_keys=True)
    brief = dict((k, report[k]) for k in
                 ('pass', 'users_mine', 'users_baseline', 'max_score_gap',
                  'max_ndcg_gap', 'max_recall_gap', 'graph_edges',
                  'candidate_items'))
    brief['id_order_mismatches'] = len(report['id_order_mismatches'])
    brief['score_mismatches'] = len(report['score_mismatches'])
    brief['metric_mismatches'] = len(report['metric_mismatches'])
    brief['frozen_hash_problems'] = report['frozen_hash_problems']
    print(json.dumps(brief, indent=2, sort_keys=True))
    return 0 if report['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
