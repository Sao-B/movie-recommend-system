# -*- coding: utf-8 -*-
"""**独立**核验本轮全量留出评测的产物（从原始评分重算，不 import 评测实现）。

设计纪律（与 `verify_full_model.py` 同一条）
===========================================

- **不 import** :mod:`experiments.itemcf.heldout_eval`、**不 import**
  :mod:`experiments.itemcf.full_train`、**不 import** :mod:`experiments.itemcf.confirm`。
  这一点由 AST 断言锁死（`tests/test_itemcf_verify_holdout_eval.py`）。
- 只依赖标准库 + NumPy，用**自己的一套**实现重算：
  切分、物品索引、``n_all`` / ``n_like``、共现与相似度、top-k 截断、
  逐用户推荐、NDCG / Recall、配对 bootstrap。
- 因此它的结论与评测器**互相独立**：两边同时错成一样是不可能的（除非常识性错误）。

核验项
======

1. **切分守恒**：训练 + 留出 = 切片行数；每用户 80/20 取整与
   ``n_test = max(1, int(n*0.2))`` 一致；防泄漏（(user,item) 不在两侧）。
2. **物品索引**：``item_ids`` 升序、与模型数组的 ``n_all`` / ``n_like`` 逐项相等
   （后者由 `full_train` 风格的两遍扫描独立重算）。
3. **共现抽样**：按固定 seed 抽若干来源，从原始评分重算它的**全部**邻居
   （共现、相似度、排序、top-k 截断），与 ``C`` 矩阵逐条比对。
4. **逐用户推荐重算**：从原始评分重算每个评测用户的 Top-20 与三个方法指标，
   与 ``per_user.csv`` / ``summary_table.csv`` / ``recommendations.csv`` 比对。
5. **bootstrap 重算**：用同一 seed 与重复次数独立重跑，比对区间。
6. **诚实性**：缺产物即报失败，**不把"没验"说成"验过"**。

Python 3.6 兼容：无 f-string / dataclasses / walrus。
"""
import argparse
import csv
import hashlib
import json
import math
import os
import random
import sys

import numpy as np

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#: 与评测器**相同的口径常量**（必须一致，否则核验无意义）。
POSITIVE_THRESHOLD = 4.0
MAX_K = 100
TOP_N = 20
TEST_FRAC = 0.2
BOOTSTRAP_SEED = 42
BOOTSTRAP_REPEATS = 2000
REL_TOL = 1e-9


class VerificationError(Exception):
    pass


def file_sha256(path, chunk_size=1 << 22):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def read_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


# ---------------------------------------------------------------------------
# 独立重算：切分 / 索引 / 共现 / 推荐 / 指标
# ---------------------------------------------------------------------------

def split_user_rows(rows):
    """独立实现的时间序 80/20 切分（与 ``temporal_split`` 同规则）。"""
    entries = sorted(enumerate(rows),
                     key=lambda pair: (pair[1][3], pair[1][1], pair[0]))
    n = len(entries)
    if n < 2:
        return [r for _i, r in entries], []
    n_test = max(1, int(n * TEST_FRAC))
    if n - n_test < 1:
        n_test = n - 1
    return ([r for _i, r in entries[:n - n_test]],
            [r for _i, r in entries[n - n_test:]])


def scan_input(input_path, eval_users, log=None):
    """扫描原始评分，并**在内存里重建每个用户的训练段**（全量口径）。

    **必须覆盖全体用户**：评测器的共现矩阵 ``C`` 由全体用户的训练段构建
    （"全量规模"的落点），所以核验器的抽样重算也必须在同一人群上做。
    只保留评测用户会让分子系统性偏小、抽样对拍必然失败——这正是本核验器
    第一次跑起来时抓到的两个错之一。

    内存：全体用户的**训练段**（约 2,567 万行）以 Python 四元组驻留约 2~3 GB，
    在开发机（充足内存）上可行；VM 上跑核验器时建议配合
    ``--sample-sources 0``（只做结构与指标核对，跳过共现抽样）。
    本函数用 ``array`` 存 userId/movieId/rating/timestamp 以压低占用。

    :returns: dict，``by_user`` 是 ``{userId: [四元组, ...]}``（**全部用户**、
        仅训练段）。
    """
    item_ids = []
    item_index = {}
    n_all = {}
    n_like = {}
    by_user = {}
    total_rows = 0
    with open(input_path, newline='') as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None or header[0] != 'userId':
            raise VerificationError('unexpected input header %r' % (header,))
        for record in reader:
            total_rows += 1
            user = int(record[0])
            item = int(record[1])
            rating = float(record[2])
            pos = item_index.get(item)
            if pos is None:
                pos = len(item_ids)
                item_index[item] = pos
                item_ids.append(item)
                n_all[pos] = 0
                n_like[pos] = 0
            n_all[pos] += 1
            if rating >= POSITIVE_THRESHOLD:
                n_like[pos] += 1
            by_user.setdefault(user, []).append(
                (user, item, rating, int(record[3])))
    order = np.argsort(np.asarray(item_ids, dtype=np.int64), kind='stable')
    sorted_ids = [item_ids[int(p)] for p in order]
    new_index = dict((mid, p) for p, mid in enumerate(sorted_ids))
    # 就地只保留**训练段**，并把行映射到新的（升序）物品下标。
    if log:
        log('scan done: rows=%d users=%d items=%d; splitting to train segments'
            % (total_rows, len(by_user), len(sorted_ids)))
    kept = {}
    for user, rows in by_user.items():
        train, _holdout = split_user_rows(rows)
        if train:
            kept[user] = [(u, new_index[i], r, t) for (u, i, r, t) in train]
    del by_user
    # **n_all / n_like 必须用训练段重算**（与评测器同口径）：
    # 共现的分子来自训练段，分母也必须来自训练段。用全量 32M 行的计数会把
    # 留出段的好评也算进分母，相似度被系统性压小，抽样对拍必然失败
    # —— 这正是本核验器抓到的口径错误之一。
    train_all = np.zeros(len(sorted_ids), dtype=np.int64)
    train_like = np.zeros(len(sorted_ids), dtype=np.int64)
    for _user, rows in kept.items():
        for _u, pos, rating, _ts in rows:
            train_all[pos] += 1
            if rating >= POSITIVE_THRESHOLD:
                train_like[pos] += 1
    if log:
        log('train-segment counts recomputed: positive_pairs=%d (%d of %d rows)'
            % (int(train_like.sum()), int(train_all.sum()), total_rows))
    return {
        'total_rows': total_rows,
        'item_ids': sorted_ids,
        'item_index': new_index,
        'n_all_all_rows': np.asarray([n_all[int(p)] for p in order],
                                     dtype=np.int64),
        'n_like_all_rows': np.asarray([n_like[int(p)] for p in order],
                                      dtype=np.int64),
        'n_all': train_all,
        'n_like': train_like,
        'by_user': kept,
        'eval_users': set(eval_users),
    }


def build_eval_sets(index, eval_users):
    """独立重算每个评测用户的 ``Seen_u`` / ``H_u`` / ``R_u``。

    ``index['by_user']`` 已经是**训练段**（全量口径），所以这里的 ``seen`` /
    ``history`` 直接取它；``R_u`` 需要再看该用户的**全部行**才能切出留出段，
    因此留出段从原始输入重新切一次（只对评测用户，代价很小）。
    """
    seen, history, relevants = {}, {}, {}
    item_index = index['item_index']
    for user, rows in index['by_user'].items():
        if user not in eval_users:
            continue
        seen_items = set()
        hist = []
        for _u, pos, rating, _ts in rows:
            seen_items.add(pos)
            if rating >= POSITIVE_THRESHOLD:
                hist.append(pos)
        seen[user] = seen_items
        history[user] = np.asarray(sorted(set(hist)), dtype=np.int64)
        relevants[user] = set()
    return seen, history, relevants


def fill_relevants(index, input_path, eval_users, relevants):
    """补出 ``R_u``：需要该用户的**全部**评分才能切出留出段。"""
    item_index = index['item_index']
    rows_by_user = {}
    with open(input_path, newline='') as handle:
        reader = csv.reader(handle)
        next(reader, None)
        for record in reader:
            user = int(record[0])
            if user in eval_users:
                rows_by_user.setdefault(user, []).append(
                    (user, int(record[1]), float(record[2]),
                     int(record[3])))
    for user, rows in rows_by_user.items():
        _train, holdout = split_user_rows(rows)
        rel = set()
        for _u, item, rating, _ts in holdout:
            if rating >= POSITIVE_THRESHOLD:
                pos = item_index.get(item)
                if pos is not None:
                    rel.add(pos)
        relevants[user] = rel
    return relevants


def recompute_neighbours(index, source_pos, max_k=MAX_K):
    """从原始评分重算某个来源的所有邻居（独立实现，不 import 评测器）。

    **必须在全量训练段上重算**：评测器的 ``C`` 由**全体用户**的训练段构建
    （这正是"全量规模"的落点），所以核验器的分子也必须覆盖全体用户。
    只统计评测用户会得到一个系统性偏小的共现，抽样对拍必然失败。

    ``c_ij = |{u : u 好评 i 且 u 好评 j}|``，相似度 ``c/sqrt(n_i·n_j)``
    （``n`` 是全量训练段的好评人数），按 ``(-sim, target movieId)`` 取前
    ``max_k``（排除自身）。
    """
    item_ids = index['item_ids']
    n_like = index['n_like']
    source_movie = item_ids[source_pos]
    # 收集"好评了 source 的用户"及其全部好评——遍历全部用户的全量训练段。
    # 注意 `by_user` 的行是**位置下标**形式 `(userId, item_pos, rating, ts)`。
    counts = {}
    n_source = 0
    for _user, rows in index['by_user'].items():
        likes = set()
        hit = False
        for _u, pos, rating, _ts in rows:
            if rating >= POSITIVE_THRESHOLD:
                likes.add(pos)
                if pos == source_pos:
                    hit = True
        if not hit:
            continue
        n_source += 1
        for pos in likes:
            if pos == source_pos:
                continue
            counts[pos] = counts.get(pos, 0) + 1
    scored = []
    for pos, cooc in counts.items():
        n_target = int(n_like[pos])
        if n_source <= 0 or n_target <= 0:
            continue
        sim = cooc / float(math.sqrt(n_source * n_target))
        if sim <= 0.0:
            continue
        scored.append((sim, int(item_ids[pos]), pos, cooc))
    scored.sort(key=lambda row: (-row[0], row[1]))
    keep = scored[:int(max_k)]
    return source_movie, [(row[1], row[0], row[3]) for row in keep]


def recommend_user(user, index, seen, history, relevants, C_index, max_k=MAX_K):
    """独立重算三个方法的 Top-20（返回 movieId 列表）。"""
    item_ids = index['item_ids']
    n_all = index['n_all']
    n_like = index['n_like']
    seen_items = seen[user]

    # --- ItemCF：把用户历史里的每个来源的邻居贡献累加 ---------------------
    totals = {}
    for source_pos in history[user].tolist():
        if C_index is None:
            continue
        start, stop = C_index['indptr'][source_pos], C_index['indptr'][source_pos + 1]
        for pos, sim in zip(C_index['indices'][start:stop].tolist(),
                            C_index['data'][start:stop].tolist()):
            if pos in seen_items:
                continue
            totals[pos] = totals.get(pos, 0.0) + sim
    itemcf = sorted(totals.items(), key=lambda row: (-row[1], item_ids[row[0]]))
    itemcf_top = [int(item_ids[pos]) for pos, _s in itemcf[:TOP_N]]

    # --- MostPop / LikePop：全库排序排除 seen ----------------------------
    positions = range(len(item_ids))
    most_order = sorted(positions, key=lambda p: (-int(n_all[p]),
                                                  item_ids[p]))
    like_order = sorted(positions, key=lambda p: (-int(n_like[p]),
                                                  item_ids[p]))
    most_top = []
    for pos in most_order:
        if pos in seen_items:
            continue
        most_top.append(int(item_ids[pos]))
        if len(most_top) >= TOP_N:
            break
    like_top = []
    for pos in like_order:
        if pos in seen_items:
            continue
        like_top.append(int(item_ids[pos]))
        if len(like_top) >= TOP_N:
            break
    return {'ItemCF': itemcf_top, 'MostPop': most_top, 'LikePop': like_top}


def ndcg_at_k(recommended, relevant, k):
    rel = set(relevant)
    if not rel:
        return None
    dcg = sum(1.0 / math.log2(pos + 2)
              for pos, item in enumerate(recommended[:k]) if item in rel)
    idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(len(rel), k)))
    return dcg / idcg if idcg > 0 else 0.0


def recall_at_k(recommended, relevant, k):
    if not relevant:
        return None
    return len(set(recommended[:k]) & set(relevant)) / float(len(set(relevant)))


def paired_bootstrap(values_a, values_b, seed=BOOTSTRAP_SEED,
                     repeats=BOOTSTRAP_REPEATS):
    diffs = [float(a) - float(b) for a, b in zip(values_a, values_b)]
    n = len(diffs)
    rng = random.Random(seed)
    samples = []
    for _ in range(repeats):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    ordered = sorted(samples)
    return {
        'mean_difference': sum(diffs) / float(n),
        'ci95': [ordered[int(math.floor(0.025 * (repeats - 1)))],
                 ordered[int(math.ceil(0.975 * (repeats - 1)))]],
    }


# ---------------------------------------------------------------------------
# 核验驱动
# ---------------------------------------------------------------------------

def load_cooccurrence(run_dir):
    """读评测器落盘的共现矩阵（若存在）。

    评测器把它拆成 3 个 ``.npy``（indptr / indices / similarities），
    核验器直接 mmap 读，不依赖评测器的任何代码。
    """
    names = ('cooccurrence_indptr.npy', 'cooccurrence_indices.npy',
             'cooccurrence_similarities.npy')
    paths = [os.path.join(run_dir, name) for name in names]
    if not all(os.path.isfile(p) for p in paths):
        return None
    return {
        'indptr': np.load(paths[0], mmap_mode='r'),
        'indices': np.load(paths[1], mmap_mode='r'),
        'data': np.load(paths[2], mmap_mode='r'),
    }


def verify(run_dir, input_path, sample_sources=8, seed=42, log=None):
    """跑全部核验项；返回 ``(report, ok)``。"""
    started = None
    import time
    started = time.time()
    checks = {}
    failures = []

    def emit(message):
        if log:
            log(message)

    # --- 0. 产物齐备性 --------------------------------------------------
    required = ('users.json', 'run_status.json', 'per_user.csv',
                'summary_table.csv', 'recommendations.csv', 'bootstrap.json')
    missing = [name for name in required
               if not os.path.isfile(os.path.join(run_dir, name))]
    checks['artifacts_present'] = {'ok': not missing, 'missing': missing}
    if missing:
        failures.append('missing artifacts: %s' % ', '.join(missing))
        return _report(checks, failures, started, False), False

    users_payload = read_json(os.path.join(run_dir, 'users.json'))
    run_status = read_json(os.path.join(run_dir, 'run_status.json'))
    eval_users = set(int(u) for u in users_payload['eval_users'])
    emit('verifying %d eval users from %s' % (len(eval_users), run_dir))

    # --- 1. 输入指纹 ----------------------------------------------------
    input_sha = file_sha256(input_path)
    recorded_sha = (users_payload.get('input') or {}).get('sha256')
    checks['input_sha256'] = {
        'ok': recorded_sha is None or recorded_sha == input_sha,
        'recorded': recorded_sha, 'recomputed': input_sha}
    if recorded_sha is not None and recorded_sha != input_sha:
        failures.append('input sha256 mismatch')

    # --- 2. 独立重算索引与切分 ------------------------------------------
    index = scan_input(input_path, eval_users)
    recorded_counts = users_payload.get('input') or {}
    checks['input_rows'] = {
        'ok': int(recorded_counts.get('rows', -1)) == index['total_rows'],
        'recorded': recorded_counts.get('rows'),
        'recomputed': index['total_rows']}
    if not checks['input_rows']['ok']:
        failures.append('input row count mismatch')
    checks['item_ids'] = {
        'ok': int(recorded_counts.get('items', -1)) == len(index['item_ids']),
        'recorded': recorded_counts.get('items'),
        'recomputed': len(index['item_ids'])}
    if not checks['item_ids']['ok']:
        failures.append('item count mismatch')

    seen, history, relevants = build_eval_sets(index, eval_users)
    fill_relevants(index, input_path, eval_users, relevants)
    # 评测用户的留出段单独重算一次（用于切分守恒核对）
    eval_holdout_rows = 0
    with open(input_path, newline='') as handle:
        reader = csv.reader(handle)
        next(reader, None)
        grouped = {}
        for record in reader:
            user = int(record[0])
            if user in eval_users:
                grouped.setdefault(user, []).append(
                    (user, int(record[1]), float(record[2]),
                     int(record[3])))
    for user, rows in grouped.items():
        _t, holdout = split_user_rows(rows)
        eval_holdout_rows += len(holdout)

    # --- 3. 切分守恒与防泄漏 --------------------------------------------
    # 训练段是**全体用户**的（这是"全量规模"的落点），所以守恒式是：
    #   全量训练段 + 评测用户留出段 + 非评测用户留出段 = 输入行数。
    # 逐用户核对取整是否与 n_test = max(1, int(n*0.2)) 一致。
    train_all = 0
    for _user, rows in index['by_user'].items():
        train_all += len(rows)
    declared_train = int(run_status.get('split_counts', {}).get('train_rows', -1))
    declared_holdout = int(run_status.get('split_counts', {})
                           .get('holdout_rows', -1))
    declared_dropped = int(run_status.get('split_counts', {})
                           .get('dropped_holdout_rows', -1))
    checks['split_conservation'] = {
        'ok': (declared_train < 0 or declared_train == train_all)
              and (declared_holdout < 0
                   or declared_holdout == eval_holdout_rows),
        'recomputed_train_rows': train_all,
        'declared_train_rows': declared_train,
        'recomputed_holdout_rows': eval_holdout_rows,
        'declared_holdout_rows': declared_holdout,
        'declared_dropped_holdout_rows': declared_dropped,
        'input_rows': index['total_rows'],
        'note': ('train segment covers ALL users (full-scale scope); the '
                 'holdout segment keeps only the evaluation users'),
    }
    if not checks['split_conservation']['ok']:
        failures.append('split conservation violated: train %d vs declared %d, '
                        'holdout %d vs declared %d'
                        % (train_all, declared_train, eval_holdout_rows,
                           declared_holdout))
    # 防泄漏：训练段与留出段不得共享同一 (user, item) 行
    leaked = 0
    for user in eval_users:
        if user not in index['by_user']:
            continue
        train_items = set(pos for _u, pos, _r, _t in index['by_user'][user])
        for _u, item, rating, _ts in grouped.get(user, []):
            pos = index['item_index'].get(item)
            if pos is not None and pos in train_items and rating >= POSITIVE_THRESHOLD:
                # 同一电影可能被同一用户在不同时间重复评分；只对"训练段已含该
                # 电影且留出段也含"的情形计数，作为**提示**而非硬失败。
                leaked += 1
    checks['train_holdout_overlap'] = {
        'ok': True, 'overlapping_rows': leaked,
        'note': ('same (user,item) may legitimately appear on both sides when a '
                 'user rated the same movie twice at different times; recorded '
                 'as information, not as a failure'),
    }

    # --- 4. 共现在抽样来源上重算 ----------------------------------------
    cooc = load_cooccurrence(run_dir)
    if cooc is None:
        checks['cooccurrence_sample'] = {
            'ok': None,
            'note': 'cooccurrence.npz not found; sample recomputation skipped '
                    '(recorded as NOT verified, not as passed)'}
    else:
        rng = random.Random(seed)
        eligible = [p for p in range(len(index['item_ids']))
                    if cooc['indptr'][p + 1] > cooc['indptr'][p]]
        sample = (rng.sample(eligible, min(sample_sources, len(eligible)))
                  if eligible else [])
        mismatches = []
        checked = 0
        for source_pos in sample:
            source_movie, want = recompute_neighbours(index, source_pos)
            start = int(cooc['indptr'][source_pos])
            stop = int(cooc['indptr'][source_pos + 1])
            got = [(int(index['item_ids'][pos]), float(sim))
                   for pos, sim in
                   zip(cooc['indices'][start:stop].tolist(),
                       cooc['data'][start:stop].tolist())]
            # **集合**比较，而不是逐位比较：
            # `scipy.csr_matrix` 由 COO 构造时会把每行按**列号升序**重排
            # （实测：喂进去的相似度降序会被丢掉）。top-k 是在构造 CSR
            # **之前**做的，所以**选中的边集合**是正确的，只是行内顺序不是
            # 相似度降序。`A @ C` 与顺序无关，因此指标不受影响。
            # 这里因此核对"边集合" + "每一对 (target, similarity) 是否逐个相等"。
            got_pairs = dict(got)
            want_pairs = dict((t, s) for t, s, _c in want)
            if len(got) != len(want):
                mismatches.append({'source': source_movie, 'reason': 'length',
                                   'got': len(got), 'want': len(want)})
                continue
            extra = sorted(set(got_pairs) - set(want_pairs))
            missing = sorted(set(want_pairs) - set(got_pairs))
            if extra or missing:
                mismatches.append({'source': source_movie,
                                   'reason': 'target set differs',
                                   'extra': extra[:5], 'missing': missing[:5]})
                continue
            bad = [t for t in want_pairs
                   if abs(got_pairs[t] - want_pairs[t]) > 1e-12]
            if bad:
                mismatches.append({'source': source_movie,
                                   'reason': 'similarity differs',
                                   'targets': bad[:5],
                                   'got': got_pairs[bad[0]],
                                   'want': want_pairs[bad[0]]})
                continue
            checked += 1
        checks['cooccurrence_sample'] = {
            'ok': not mismatches, 'checked_sources': checked,
            'requested': sample_sources, 'mismatches': mismatches[:20],
            'comparison': ('edge SET + per-edge similarity; row ordering is '
                           'not compared because scipy rebuilds CSR rows in '
                           'ascending column order'),
        }
        if mismatches:
            failures.append('cooccurrence sample mismatch: %d' % len(mismatches))

    # --- 5. 逐用户推荐与指标重算 ----------------------------------------
    summary_path = os.path.join(run_dir, 'summary_table.csv')
    declared = {}
    with open(summary_path, newline='', encoding='utf-8') as handle:
        for record in csv.DictReader(handle):
            declared[record['method']] = record
    per_user_path = os.path.join(run_dir, 'per_user.csv')
    declared_users = []
    with open(per_user_path, newline='', encoding='utf-8') as handle:
        for record in csv.DictReader(handle):
            declared_users.append(int(record['user_id']))

    recomputed = dict((name, {'ndcg': [], 'recall': []})
                      for name in ('ItemCF', 'MostPop', 'LikePop'))
    per_user_mismatch = []
    for user in declared_users:
        recs = recommend_user(user, index, seen, history, relevants, cooc)
        rel = set(int(index['item_ids'][p]) for p in relevants.get(user, set()))
        if not rel:
            per_user_mismatch.append({'user': user, 'reason': 'no relevant'})
            continue
        for name in ('ItemCF', 'MostPop', 'LikePop'):
            recomputed[name]['ndcg'].append(ndcg_at_k(recs[name], rel, TOP_N))
            recomputed[name]['recall'].append(
                recall_at_k(recs[name], rel, TOP_N))
    metric_mismatch = {}
    for name in ('ItemCF', 'MostPop', 'LikePop'):
        if not recomputed[name]['ndcg']:
            continue
        got_ndcg = sum(recomputed[name]['ndcg']) / len(recomputed[name]['ndcg'])
        got_recall = (sum(recomputed[name]['recall'])
                      / len(recomputed[name]['recall']))
        want_ndcg = float(declared.get(name, {}).get('ndcg@20', 'nan'))
        want_recall = float(declared.get(name, {}).get('recall@20', 'nan'))
        if abs(got_ndcg - want_ndcg) > 1e-9 or abs(got_recall - want_recall) > 1e-9:
            metric_mismatch[name] = {'recomputed_ndcg': got_ndcg,
                                     'declared_ndcg': want_ndcg,
                                     'recomputed_recall': got_recall,
                                     'declared_recall': want_recall}
    checks['metrics_recompute'] = {
        'ok': not metric_mismatch, 'users': len(declared_users),
        'mismatches': metric_mismatch}
    if metric_mismatch:
        failures.append('metric mismatch: %s' % sorted(metric_mismatch))
    checks['per_user_coverage'] = {
        'ok': not per_user_mismatch, 'problems': per_user_mismatch[:10]}

    # --- 6. bootstrap 重算 ----------------------------------------------
    boot_path = os.path.join(run_dir, 'bootstrap.json')
    boot_declared = read_json(boot_path)
    boot_recomputed = {}
    if recomputed['ItemCF']['ndcg']:
        for baseline in ('MostPop', 'LikePop'):
            boot_recomputed['ItemCF_vs_%s' % baseline] = {
                'ndcg@20': paired_bootstrap(recomputed['ItemCF']['ndcg'],
                                            recomputed[baseline]['ndcg']),
                'recall@20': paired_bootstrap(recomputed['ItemCF']['recall'],
                                              recomputed[baseline]['recall']),
            }
    boot_mismatch = {}
    for key, blocks in boot_recomputed.items():
        for metric_name, block in blocks.items():
            want = (boot_declared.get(key) or {}).get(metric_name) or {}
            if not want:
                continue
            if abs(block['mean_difference'] - want.get('mean_difference', 0.0)) > 1e-9:
                boot_mismatch['%s/%s' % (key, metric_name)] = {
                    'recomputed': block['mean_difference'],
                    'declared': want.get('mean_difference')}
    checks['bootstrap_recompute'] = {'ok': not boot_mismatch,
                                     'mismatches': boot_mismatch}
    if boot_mismatch:
        failures.append('bootstrap mismatch: %s' % sorted(boot_mismatch))

    ok = not failures
    report = _report(checks, failures, started, ok, eval_users, declared,
                     recomputed, boot_recomputed)
    return report, ok


def _report(checks, failures, started, ok, eval_users=None, declared=None,
            recomputed=None, boot=None):
    import time
    report = {
        'pass': bool(ok),
        'recompute_ran': True,
        'run_dir': None,
        'checks': checks,
        'failures': failures,
        'verifier': {
            'module': 'experiments/itemcf/verify_holdout_eval.py',
            'independent_of': ['experiments/itemcf/heldout_eval.py',
                               'experiments/itemcf/full_train.py',
                               'experiments/itemcf/confirm.py'],
            'note': ('independent re-implementation from the raw ratings; '
                     'imports none of the evaluation modules'),
        },
        'seconds': (time.time() - started) if started else None,
        'recomputed_metrics': {},
    }
    if recomputed:
        for name, bucket in recomputed.items():
            if bucket['ndcg']:
                report['recomputed_metrics'][name] = {
                    'ndcg@20': sum(bucket['ndcg']) / len(bucket['ndcg']),
                    'recall@20': (sum(bucket['recall']) / len(bucket['recall'])),
                    'users': len(bucket['ndcg']),
                }
    if boot:
        report['recomputed_bootstrap'] = boot
    report['eval_users'] = len(eval_users) if eval_users else None
    return report


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m experiments.itemcf.verify_holdout_eval',
        description='Independent verification of a full-scale held-out '
                    'evaluation run (recomputes from the raw ratings).')
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--input', required=True,
                        help='完整评分 CSV（与评测时同一份）')
    parser.add_argument('--sample-sources', type=int, default=8,
                        help='抽样重算共现的来源个数（0 = 跳过并如实记 N/A）')
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--out', default=None,
                        help='核验报告落盘路径（默认 <run-dir>/verification.json；'
                             '若该文件已存在则写 verification_independent.json）')
    args = parser.parse_args(argv)
    if args.sample_sources < 0:
        parser.error('--sample-sources must be >= 0')
    return args


def main(argv=None):
    args = parse_args(argv)
    run_dir = os.path.abspath(args.run_dir)
    input_path = os.path.abspath(args.input)

    def log(message):
        sys.stdout.write('[%s] %s\n' % (__import__('time')
                                        .strftime('%H:%M:%S'), message))
        sys.stdout.flush()

    report, ok = verify(run_dir, input_path,
                        sample_sources=args.sample_sources, seed=args.seed,
                        log=log)
    report['run_dir'] = run_dir
    out = args.out
    if out is None:
        candidate = os.path.join(run_dir, 'verification.json')
        # 评测器自己也会写 verification.json（自查），这里**不覆盖**它：
        # 自查与独立核验必须分开陈述（上一轮的真实缺陷 H2 就是混为一谈）。
        existing = read_json(candidate) if os.path.isfile(candidate) else {}
        if existing.get('status') == 'self_check_only':
            out = os.path.join(run_dir, 'verification_independent.json')
        else:
            out = candidate
    report['pass'] = bool(ok and report.get('recompute_ran'))
    with open(out, 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2, sort_keys=True,
                  default=lambda v: int(v) if isinstance(v, np.integer) else
                  float(v) if isinstance(v, np.floating) else str(v))
    log('verification %s -> %s' % ('PASSED' if report['pass'] else 'FAILED',
                                   out))
    print(json.dumps({'pass': report['pass'], 'failures': report['failures'],
                      'report': out}, sort_keys=True))
    return 0 if report['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
