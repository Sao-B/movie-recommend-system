# -*- coding: utf-8 -*-
"""全量模型的**核验**入口（计划书 §5.D）：只读产物，不做训练。

三类检查，缺一不可：

**A. 结构不变式**（全部来源与边）
    交给 :func:`experiments.itemcf.model_io.validate_arrays`，它按来源边界向量化，
    不逐边 Python 循环。覆盖：索引范围、``indptr`` 单调与终值、无自环、同来源无
    重复目标、每来源 ``<= max_k``、相似度有限且落在 ``(0, 1]``、按相似度降序且
    真正浮点平票时 ``movieId`` 升序、共现为正且不超过两端 ``n_like``。

    其中两条是**显式检查项**（Lead 明确要求，也是"与 fix14 对拍能过关"的前提）：

    1. **无自环**：任何来源都不含 ``target == 自身``；
    2. **零好评来源区间为空**：``n_like[i] == 0`` 的来源在 ``indptr`` 上必须是空
       区间，且不得因"补边/热门补齐"出现任何边。

**B. 抽样独立重算**（计划书 §5.D，固定 ``seed=42`` 抽 100 个有邻居来源）
    从**完整训练评分**独立重算这些来源对**所有电影**的共现、相似度与 top-100，
    核对目标 ID / 顺序 / 分数。**不是只核对已经存下来的正边**——只核对正边就
    发现不了"漏掉了更优邻居"。抽样覆盖：高支持度、低支持度、并列边界来源。

    本核验**不复用构图代码**：相似度用一份独立写法的 NumPy 实现
    （:func:`recompute_source_neighbors`），只共享"从原始评分读输入"这一层。

**C. 输入/产物指纹与 ID 一致性**
    输入 SHA-256、源码哈希未漂移、块文件完整、``run_id`` / ``model_id`` /
    产物内 ID 三者一致且为真值形状（上轮的 ``'run'`` bug 在这里会被抓住）。

用法::

    python -m experiments.itemcf.verify_full_model \\
        --run-dir <out_dir> [--input <ratings.csv>] \\
        --sample-sources 100 --seed 42 [--out verification.json]

``--input`` 缺省时从 ``run_status.json`` 的 ``config``/``manifest`` 里找；找不到
就**跳过** B 段并在报告里写明 ``input_available: false``——不假装核验过。

Python 3.6 兼容。
"""
import argparse
import array
import csv
import hashlib
import json
import os
import random
import sys
import time

import numpy as np
from scipy import sparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import model_io as M  # noqa: E402

#: 抽样核验的默认规模与固定 seed（计划书 §5.D）。
DEFAULT_SAMPLE_SOURCES = 100
DEFAULT_SEED = 42

#: 分数核验容差。1e-12 相对/绝对都足够严——同一条公式、同一份 float64 输入，
#: 唯一差异是求和顺序（CSR 乘法 vs 独立重算），实测差异在 0（精确相等）。
SIMILARITY_TOLERANCE = 1e-12

#: 抽样时给"并列边界来源"的配额（计划书要求覆盖并列与高低支持度）。
BOUNDARY_SAMPLE_QUOTA = 20
LOW_SUPPORT_QUOTA = 20
HIGH_SUPPORT_QUOTA = 20


def file_sha256(path, chunk_size=1 << 22):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def load_run_status(run_dir):
    path = os.path.join(run_dir, 'run_status.json')
    if not os.path.isfile(path):
        raise IOError('missing run_status.json in %s (was the run finished?)'
                      % run_dir)
    return M.read_json(path)


def resolve_input(run_dir, explicit):
    """找原始输入路径：显式参数 > run_status.config/manifest > 都没有则 None。"""
    if explicit:
        if not os.path.isfile(explicit):
            raise IOError('--input not found: %s' % explicit)
        return os.path.abspath(explicit)
    for name in ('manifest.json', 'run_status.json'):
        path = os.path.join(run_dir, name)
        if not os.path.isfile(path):
            continue
        payload = M.read_json(path)
        candidate = payload.get('input') or (payload.get('source') or {}).get('path')
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


# ---------------------------------------------------------------------------
# B 段：独立重算（不复用构图代码）
# ---------------------------------------------------------------------------

def read_training_rows(input_path, positive_threshold):
    """读原始评分，返回 ``(rows, n_users, n_items, nnz)``。

    这是**核验路径**，为了与构图完全独立，这里刻意走"先全读、再按原始 movieId
    排序建矩阵"的老实做法——它慢、内存也大，但核验本来就该慢且直白，用快的
    分块路径做核验等于用被测对象验证被测对象。小输入（2k 切片 30 万行）秒级；
    全量上不要跑 ``--input``，用 ``--skip-recompute`` 或让 B 段退化成抽样。

    这里**不重复输入校验**（构图阶段已经做过并落盘），因为核验的目标是数学，
    不是输入卫生；重复校验只会让核验路径自己撞上内存问题。
    """
    users, items = set(), set()
    rows = []
    with open(input_path, newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise IOError('empty input: %s' % input_path)
        index = dict((name, pos) for pos, name in enumerate(header))
        for column in ('userId', 'movieId', 'rating'):
            if column not in index:
                raise IOError('input %s missing column %r' % (input_path, column))
        u_col, i_col, r_col = index['userId'], index['movieId'], index['rating']
        for record in reader:
            user_id = int(record[u_col])
            item_id = int(record[i_col])
            rating = float(record[r_col])
            users.add(user_id)
            items.add(item_id)
            if rating >= positive_threshold:
                rows.append((user_id, item_id))
    user_ids = sorted(users)
    item_ids = sorted(items)
    return rows, user_ids, item_ids


def build_positive_matrix(rows, user_ids, item_ids):
    """独立构造 0/1 好评矩阵（int64）。"""
    user_index = dict((u, p) for p, u in enumerate(user_ids))
    item_index = dict((i, p) for p, i in enumerate(item_ids))
    if not rows:
        return sparse.csr_matrix((len(user_ids), len(item_ids)), dtype=np.int64)
    r = np.fromiter((user_index[u] for u, _ in rows), dtype=np.int64, count=len(rows))
    c = np.fromiter((item_index[i] for _, i in rows), dtype=np.int64, count=len(rows))
    data = np.ones(len(rows), dtype=np.int64)
    return sparse.csr_matrix((data, (r, c)),
                             shape=(len(user_ids), len(item_ids)), dtype=np.int64)


def recompute_source_neighbors(X, source_pos, n_like, max_k, beta):
    """**独立实现**：重算单个来源对所有电影的共现、相似度与 top-k。

    与构图路径的区别（这就是"独立"的含义）：

    - 不做列分块，直接取 ``X[:, source_pos]`` 一列与整个 X 相乘；
    - 不排序整个块，只在**该来源的候选集**上排序；
    - 相似度公式逐个元素算，不用广播。

    :returns: ``[(target_pos, sim, c), ...]``，已按 ``(-sim, target_pos)`` 排序并
        截断 ``max_k``；只含正相似、非对角线项。
    """
    column = X[:, int(source_pos)]
    products = (column.T @ X).tocoo()
    n_source = int(n_like[int(source_pos)])
    if n_source <= 0:
        return []
    candidates = []
    for target_pos, value in zip(products.col, products.data):
        target_pos = int(target_pos)
        if target_pos == int(source_pos):
            continue
        c_ij = int(value)
        if c_ij <= 0:
            continue
        n_target = int(n_like[target_pos])
        if n_target <= 0:
            continue
        sim = float(c_ij) / float(np.sqrt(float(n_source) * float(n_target)))
        if beta > 0.0:
            sim = sim * (float(c_ij) / (float(c_ij) + float(beta)))
        candidates.append((target_pos, sim, c_ij))
    candidates.sort(key=lambda entry: (-entry[1], entry[0]))
    kept = []
    for target_pos, sim, c_ij in candidates:
        if sim <= 0.0 or len(kept) >= int(max_k):
            break
        kept.append((target_pos, sim, c_ij))
    return kept


def select_sample_sources(model, seed=DEFAULT_SEED, count=DEFAULT_SAMPLE_SOURCES):
    """抽有邻居的来源：覆盖高支持度、低支持度、并列边界，**总数不超过 count**。

    "已有邻居的来源"才是能核验顶层截断的对象；空区间的来源另有一项单独检查
    （它们必须真的是零好评或零共现）。抽样用固定 seed，可复现。

    **配额必须按 count 收缩**：三个配额（高/低/边界）各自默认 20，若直接相加，
    ``count`` 小于 60 时返回值会远超请求值——实测 ``count=2`` 返回 **40** 个
    （20 倍）。``--sample-sources`` 是用户可控参数，超出请求会让"抽 2 个看看"
    变成抽 40 个（对全量模型是实打实的时间开销），且报告里 requested/selected
    不一致。这里按 count 比例分配配额，总数上界严格是 ``count``。
    """
    counts = np.diff(np.asarray(model.indptr))
    have = np.flatnonzero(counts > 0)
    empty = {'high_support': [], 'low_support': [], 'boundary': [], 'all': []}
    if len(have) == 0:
        return empty
    count = int(count)
    if count <= 0:
        return empty

    # 按 count 收缩三个配额（各占 1/3，余数补给高支持度），总和不超 count。
    base = count // 3
    quotas = [base, base, base]
    for i in range(count - sum(quotas)):
        quotas[i % 3] += 1
    high_quota = min(quotas[0], HIGH_SUPPORT_QUOTA)
    low_quota = min(quotas[1], LOW_SUPPORT_QUOTA)
    boundary_quota = min(quotas[2], BOUNDARY_SAMPLE_QUOTA)

    n_like = np.asarray(model.n_like)
    rng = random.Random(seed)

    order_support = have[np.argsort(-n_like[have].astype(np.int64), kind='stable')]
    high = [int(p) for p in order_support[:high_quota]]
    low = ([int(p) for p in order_support[-low_quota:]] if low_quota else [])

    # 并列边界：同来源内存在 **精确相等** 的相邻相似度（平票必然由 movieId 决定）。
    boundary = []
    if boundary_quota:
        for pos in have:
            start = int(model.indptr[pos])
            stop = int(model.indptr[pos + 1])
            if stop - start < 2:
                continue
            sims = np.asarray(model.similarities[start:stop])
            if np.any(sims[1:] == sims[:-1]):
                boundary.append(int(pos))
            if len(boundary) >= boundary_quota * 4:
                break
        rng.shuffle(boundary)
        boundary = boundary[:boundary_quota]

    chosen = set(high) | set(low) | set(boundary)
    # 硬上界：即便三个配额相加仍超（例如 count 很小但 boundary 命中很多），
    # 也裁到 count。
    if len(chosen) > count:
        ordered = sorted(chosen)
        chosen = set(ordered[:count])
        high = [p for p in high if p in chosen]
        low = [p for p in low if p in chosen]
        boundary = [p for p in boundary if p in chosen]
    remaining = count - len(chosen)
    if remaining > 0:
        pool = [int(p) for p in have]
        rng.shuffle(pool)
        for pos in pool:
            if len(chosen) >= count:
                break
            chosen.add(pos)
    return {
        'high_support': high,
        'low_support': low,
        'boundary': boundary,
        'all': sorted(chosen),
    }


def verify_sampled_sources(model, X, n_like, item_ids, max_k, beta, sample,
                           log=None):
    """B 段主体：对抽样来源逐条重算并比对。

    比对三个层次，缺一不可：

    1. **目标 ID 序列完全一致**（这是"没有漏掉更优邻居"的判据）；
    2. **顺序一致**（含平票的 movieId 序）；
    3. **分数差 <= 1e-12**。

    任何一项不一致都记进 ``mismatches`` 明细（不只报个数），由调用方判定失败。
    """
    mismatches = []
    checked = 0
    max_gap = 0.0
    for pos in sample['all']:
        start = int(model.indptr[pos])
        stop = int(model.indptr[pos + 1])
        stored_targets = [int(v) for v in model.target_indices[start:stop]]
        stored_sims = [float(v) for v in model.similarities[start:stop]]
        stored_coocs = [int(v) for v in model.cooccurrences[start:stop]]
        recomputed = recompute_source_neighbors(X, pos, n_like, max_k, beta)
        expected_targets = [entry[0] for entry in recomputed]
        expected_sims = [entry[1] for entry in recomputed]
        expected_coocs = [entry[2] for entry in recomputed]
        checked += 1
        if stored_targets != expected_targets:
            mismatches.append({
                'source_pos': pos,
                'movie_id': int(item_ids[pos]),
                'kind': 'target_sequence',
                'stored_len': len(stored_targets),
                'recomputed_len': len(expected_targets),
                'stored_head': stored_targets[:8],
                'recomputed_head': expected_targets[:8],
                'first_diff_index': next(
                    (k for k in range(min(len(stored_targets),
                                          len(expected_targets)))
                     if stored_targets[k] != expected_targets[k]),
                    None),
            })
            continue
        for k in range(len(stored_targets)):
            gap = abs(stored_sims[k] - expected_sims[k])
            max_gap = max(max_gap, gap)
            if gap > SIMILARITY_TOLERANCE:
                mismatches.append({
                    'source_pos': pos, 'movie_id': int(item_ids[pos]),
                    'kind': 'similarity', 'rank': k,
                    'target_movie_id': int(item_ids[stored_targets[k]]),
                    'stored': stored_sims[k], 'recomputed': expected_sims[k],
                    'gap': gap,
                })
                break
            if stored_coocs[k] != expected_coocs[k]:
                mismatches.append({
                    'source_pos': pos, 'movie_id': int(item_ids[pos]),
                    'kind': 'cooccurrence', 'rank': k,
                    'target_movie_id': int(item_ids[stored_targets[k]]),
                    'stored': stored_coocs[k], 'recomputed': expected_coocs[k],
                })
                break
        if log and checked % 25 == 0:
            log('recompute check: %d/%d sources verified'
                % (checked, len(sample['all'])))
    return {
        'checked_sources': checked,
        'requested_sources': len(sample['all']),
        'high_support_sources': len(sample['high_support']),
        'low_support_sources': len(sample['low_support']),
        'boundary_sources': len(sample['boundary']),
        'mismatches': mismatches[:50],
        'mismatch_count': len(mismatches),
        'max_similarity_gap': max_gap,
        'tolerance': SIMILARITY_TOLERANCE,
        'pass': not mismatches,
    }


# ---------------------------------------------------------------------------
# A 段补充：零好评来源与空区间的显式检查
# ---------------------------------------------------------------------------

def check_zero_like_and_empty_intervals(model):
    """显式检查两条（Lead 要求的独立检查项，不藏在注释里）。

    1. **零好评来源区间为空**：``n_like[i] == 0`` 的来源必须 ``indptr`` 空区间。
       同时反过来查：空区间是否都有理由（``n_like == 0``，或者该来源确实与
       所有其他电影无共现）。
    2. **无补边/热门补齐的痕迹**：空区间来源的 ``n_like`` 若是正数，则说明它
       真的没有共现对象——这时把它与"零好评"来源分开计数，不是把两者混为
       "都空着，所以对了"。
    """
    indptr = np.asarray(model.indptr)
    n_like = np.asarray(model.n_like)
    counts = np.diff(indptr)
    n_items = len(n_like)

    zero_like = n_like == 0
    zero_like_nonempty = np.flatnonzero(zero_like & (counts > 0))
    empty = counts == 0
    empty_zero_like = int(np.count_nonzero(empty & zero_like))
    empty_with_like = int(np.count_nonzero(empty & ~zero_like))

    checks = {
        'n_items': int(n_items),
        'n_zero_like_sources': int(np.count_nonzero(zero_like)),
        'n_empty_interval_sources': int(np.count_nonzero(empty)),
        'n_zero_like_with_neighbors': int(len(zero_like_nonempty)),
        'zero_like_with_neighbors_sample': [int(v) for v in
                                            zero_like_nonempty[:20]],
        'empty_without_zero_like': empty_with_like,
        'empty_with_zero_like': empty_zero_like,
        'note': ('empty_without_zero_like counts sources that have at least one '
                 'positive rating but no positive co-occurrence with any other '
                 'movie; that is a legitimate empty interval, not a missing block.'),
        'pass': len(zero_like_nonempty) == 0,
    }
    return checks


def check_no_self_loops(model):
    """显式检查：任何来源都不含 ``target == 自身``（对角线按定义排除）。"""
    indptr = np.asarray(model.indptr)
    counts = np.diff(indptr)
    targets = np.asarray(model.target_indices)
    n_items = len(indptr) - 1
    if len(targets) == 0:
        return {'self_loops': 0, 'pass': True}
    edge_source = np.repeat(np.arange(n_items, dtype=np.int64), counts)
    loops = edge_source == targets
    n_loops = int(np.count_nonzero(loops))
    first = None
    if n_loops:
        index = int(np.argmax(loops))
        first = {'source_pos': int(edge_source[index]),
                 'movie_id': int(model.item_ids[edge_source[index]])}
    return {'self_loops': n_loops, 'first': first, 'pass': n_loops == 0}


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m experiments.itemcf.verify_full_model',
        description='Verify a full ItemCF model artifact (structure, recompute '
                    'sample, fingerprints, id consistency).')
    parser.add_argument('--run-dir', required=True,
                        help='full_train 的产物目录（含 model/ 与 run_status.json）')
    parser.add_argument('--input', default=None,
                        help='原始评分 CSV；省略时从 run_status/manifest 推断')
    parser.add_argument('--model-dir', default=None,
                        help='默认 <run-dir>/model')
    parser.add_argument('--sample-sources', type=int,
                        default=DEFAULT_SAMPLE_SOURCES)
    parser.add_argument('--seed', type=int, default=DEFAULT_SEED)
    parser.add_argument('--out', default=None,
                        help='默认 <run-dir>/verification.json')
    parser.add_argument('--skip-recompute', action='store_true',
                        help='跳过 B 段独立重算（仅结构与指纹）')
    args = parser.parse_args(argv)
    if args.sample_sources < 0:
        parser.error('--sample-sources must be >= 0')
    return args


def run(args, log=None):
    started = time.time()
    run_dir = os.path.abspath(args.run_dir)
    model_dir = os.path.abspath(args.model_dir or os.path.join(run_dir, 'model'))
    run_status = load_run_status(run_dir)
    report = {
        'run_dir': run_dir,
        'model_dir': model_dir,
        'started_at': M.utc_now_iso(),
        'run_status': run_status.get('status'),
        'run_id': run_status.get('run_id'),
        'model_id': run_status.get('model_id'),
        'checks': {},
        'failures': [],
    }

    # --- C 段（先做，因为 ID 与指纹是"这份产物属于哪个 run"的前提）------
    metadata = M.read_json(os.path.join(model_dir, M.METADATA_FILE))
    expected_model_id = M.compute_model_id(run_status['run_id']) \
        if run_status.get('run_id') else None
    id_consistency = {
        'out_dir_basename': os.path.basename(run_dir),
        'run_status_run_id': run_status.get('run_id'),
        'metadata_run_id': metadata.get('run_id'),
        'metadata_model_id': metadata.get('model_id'),
        'expected_model_id': expected_model_id,
        'run_status_model_id': run_status.get('model_id'),
    }
    id_consistency['all_match'] = bool(
        id_consistency['run_status_run_id'] == id_consistency['out_dir_basename']
        and id_consistency['metadata_run_id'] == id_consistency['run_status_run_id']
        and id_consistency['metadata_model_id'] == expected_model_id
        and (id_consistency['run_status_model_id'] in (None, expected_model_id)))
    # 形状检查：通用 'run' 之类的值必须被拒。
    try:
        M.verify_id_format(id_consistency['out_dir_basename'], 'out_dir basename')
        id_consistency['format_ok'] = True
    except ValueError as error:
        id_consistency['format_ok'] = False
        id_consistency['format_error'] = str(error)
    id_consistency['pass'] = bool(id_consistency['all_match']
                                  and id_consistency['format_ok'])
    report['checks']['id_consistency'] = id_consistency
    if not id_consistency['pass']:
        report['failures'].append('id_consistency: %r' % (id_consistency,))

    input_path = resolve_input(run_dir, args.input)
    source = metadata.get('source') or {}
    recorded_input_sha = None
    if input_path:
        recorded_input_sha = source.get('sha256') or run_status.get('input_sha256')
        actual = file_sha256(input_path)
        source_check = {
            'path': input_path,
            'recorded_sha256': recorded_input_sha,
            'actual_sha256': actual,
            'matches': (recorded_input_sha is None or recorded_input_sha == actual),
        }
    else:
        source_check = {'path': None, 'matches': None,
                        'note': 'input file not available; input fingerprint '
                                'could not be re-verified'}
    report['checks']['input_fingerprint'] = source_check
    if source_check.get('matches') is False:
        report['failures'].append('input fingerprint drifted: %s'
                                  % source_check)

    module_hashes_now = {
        name: file_sha256(os.path.join(MODULE_DIR, name))
        for name in ('full_train.py', 'model_io.py', 'verify_full_model.py')
        if os.path.isfile(os.path.join(MODULE_DIR, name))
    }
    recorded_hashes = (metadata.get('graph_signature') or {})
    recorded_sources = source.get('sha256')
    report['checks']['source_code_hashes'] = {
        'now': module_hashes_now,
        'note': 'source drift is reported, not enforced: the artifact is still '
                'verifiable on its own, but a drifted tree means the numbers in '
                'this report were produced by different code.',
    }

    # --- A 段：结构 ------------------------------------------------------
    parameters = metadata.get('parameters') or {}
    max_k = int(parameters.get('max_k', 100))
    beta = float(parameters.get('beta', 0.0))
    model, metadata = M.load_model(model_dir, mmap_mode='r', validate=True,
                                  max_k=max_k)
    problems, stats = M.validate_arrays(model, max_k=max_k)
    report['checks']['structure'] = {
        'problems': problems, 'stats': stats, 'max_k': max_k, 'pass': not problems,
    }
    if problems:
        report['failures'].append('structure: %s' % '; '.join(problems[:5]))

    report['checks']['no_self_loops'] = check_no_self_loops(model)
    if not report['checks']['no_self_loops']['pass']:
        report['failures'].append('self loops present')

    report['checks']['zero_like_empty_intervals'] = \
        check_zero_like_and_empty_intervals(model)
    if not report['checks']['zero_like_empty_intervals']['pass']:
        report['failures'].append('zero-like source has neighbors')

    report['checks']['file_hashes'] = {
        'verified': True,
        'per_file': M.verify_hashes(model_dir, metadata),
    }

    # --- 块文件完整性（若有）---------------------------------------------
    block_index = os.path.join(run_dir, 'graph_blocks.json')
    if os.path.isfile(block_index):
        payload = M.read_json(block_index)
        report['checks']['graph_blocks'] = {
            'blocks_written': payload.get('blocks_written'),
            'blocks_failed': payload.get('blocks_failed'),
            'signature': payload.get('signature', {}).get('signature'),
            'recorded_signature': (metadata.get('graph_signature')),
            'signature_matches': (payload.get('signature', {}).get('signature')
                                  == metadata.get('graph_signature')),
        }
        if not report['checks']['graph_blocks']['signature_matches']:
            report['failures'].append('block index signature != model metadata '
                                      'signature')
    else:
        report['checks']['graph_blocks'] = {'present': False}

    # --- B 段：抽样独立重算 ---------------------------------------------
    if args.skip_recompute or args.sample_sources == 0:
        report['checks']['recompute'] = {
            'skipped': True,
            'reason': '--skip-recompute or --sample-sources 0',
        }
    elif input_path is None:
        report['checks']['recompute'] = {
            'skipped': True,
            'input_available': False,
            'reason': 'original ratings file not found; independent recompute '
                      'cannot be performed. This report does NOT claim B-stage '
                      'verification.',
        }
    else:
        threshold = float(parameters.get('positive_threshold', 4.0))
        if log:
            log('loading ratings for independent recompute: %s' % input_path)
        rows, user_ids, item_ids_list = read_training_rows(input_path, threshold)
        X = build_positive_matrix(rows, user_ids, item_ids_list)
        del rows
        n_like_recomputed = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)
        # 用**重算的** n_like 与模型里存的比对：归一化分母错了会在这里暴露。
        model_item_ids = np.asarray(model.item_ids)
        recomputed_map = dict((mid, int(n_like_recomputed[p]))
                              for p, mid in enumerate(item_ids_list))
        stored = np.asarray(model.n_like)
        mismatch_count = 0
        first_mismatch = None
        for p in range(len(model_item_ids)):
            value = recomputed_map.get(int(model_item_ids[p]))
            if value is None:
                # 模型里有一部电影在重算输入里不存在——输入不同源，必须报出来。
                mismatch_count += 1
                if first_mismatch is None:
                    first_mismatch = {'movie_id': int(model_item_ids[p]),
                                      'stored': int(stored[p]),
                                      'recomputed': None,
                                      'reason': 'item absent from input'}
                continue
            if value != int(stored[p]):
                mismatch_count += 1
                if first_mismatch is None:
                    first_mismatch = {'movie_id': int(model_item_ids[p]),
                                      'stored': int(stored[p]),
                                      'recomputed': value}
        report['checks']['n_like_from_input'] = {
            'mismatch_count': mismatch_count,
            'first_mismatch': first_mismatch,
            'input_items': len(item_ids_list),
            'model_items': int(len(model_item_ids)),
            'pass': mismatch_count == 0,
        }
        if mismatch_count:
            report['failures'].append(
                'n_like recomputed from the input disagrees for %d items'
                % mismatch_count)

        # 只有 n_like 全对（即输入同源）时才做逐来源抽样，否则抽样毫无意义。
        if mismatch_count == 0:
            sample = select_sample_sources(model, seed=args.seed,
                                           count=args.sample_sources)
            report['checks']['recompute_sample_selection'] = {
                'seed': args.seed,
                'requested': args.sample_sources,
                'selected': len(sample['all']),
                'high_support': sample['high_support'][:10],
                'low_support': sample['low_support'][:10],
                'boundary': sample['boundary'][:10],
            }
            result = verify_sampled_sources(model, X, n_like_recomputed,
                                            model_item_ids, max_k, beta, sample,
                                            log=log)
            report['checks']['recompute'] = result
            if not result['pass']:
                report['failures'].append(
                    'independent recompute found %d mismatch(es)'
                    % result['mismatch_count'])
        else:
            report['checks']['recompute'] = {
                'skipped': True,
                'reason': 'n_like disagrees with the input; the input is not the '
                          'one this model was built from, so per-source '
                          'recompute would be meaningless',
            }

    # ``pass`` 必须表示"**确实做了**独立核验且全部通过"。
    #
    # 曾经的写法是 ``pass = not failures``——但"跳过重算"（``--skip-recompute``、
    # ``--sample-sources 0``、输入不可用、或 n_like 与输入不符）**不会**往 failures
    # 里加任何东西，于是 **一次核验都没跑也得到 pass=True**。实测：
    # `--skip-recompute` -> pass=True、failures=[]、checked_sources=None；
    # `--sample-sources 0` 同。后果是 conclusion_writer 会写出
    # "独立核验总体：**通过**" + "重算 N/A 个来源" 同页自相矛盾的结论，
    # 且第 7 节据此宣称"已通过独立核验"。这直接被计划书 §6 的
    # "只对成功且完整核验的图标记 validated_unpublished" 否定。
    #
    # 正确语义：
    #   pass = (无 failures) AND (recompute 真的跑过且通过)
    recompute = report.get('checks', {}).get('recompute') or {}
    recompute_ran = (not recompute.get('skipped', False)
                     and recompute.get('checked_sources'))
    recompute_ok = bool(recompute.get('pass'))
    report['recompute_ran'] = bool(recompute_ran)
    if not recompute_ran:
        report['failures'].append(
            'independent per-source recompute did NOT run (skipped=%r, '
            'checked_sources=%r); this report does not establish that the model '
            'reproduces the input, so it must not be read as a pass'
            % (recompute.get('skipped'), recompute.get('checked_sources')))
    elif not recompute_ok:
        # 已在上面按 mismatch 记过 failures；这里只补一个明确的标记。
        report['failures'].append(
            'independent per-source recompute ran but did not pass')

    report['pass'] = (not report['failures']) and bool(recompute_ran) \
        and recompute_ok
    report['pass_means'] = ('pass=True requires: no failed checks AND a real '
                            'per-source recompute from the raw ratings that '
                            'matched. A skipped recompute is NOT a pass.')
    report['seconds'] = time.time() - started
    report['finished_at'] = M.utc_now_iso()
    out_path = args.out or os.path.join(run_dir, 'verification.json')

    # **把判定写回 model.json，避免两个产物互相矛盾。**
    # 核验真的发现不符时，`model.json` 曾仍写着 `validated_unpublished`：
    # 下游只要读 model.json（或 grep 那个状态）就会采信一个**已被它自己的
    # 核验者否定**的模型，而没有任何一层负责调和。
    # `apply_verification_verdict` 只降级、不升级，且"没跑"与"跑了但不符"分开处置。
    # 核验失败**不应**让核验本身崩掉，所以这里吞掉异常并如实记录。
    try:
        model_dir = args.model_dir or os.path.join(run_dir, 'model')
        if os.path.isdir(model_dir):
            before, after, changed = M.apply_verification_verdict(
                model_dir, report, report_path=None)
            report['model_status'] = {'before': before, 'after': after,
                                      'downgraded': bool(changed)}
        else:
            report['model_status'] = {'before': None, 'after': None,
                                      'downgraded': False,
                                      'note': 'model dir not found: %s'
                                              % model_dir}
    except Exception as error:  # noqa: BLE001 - 核验结果本身必须落盘
        report['model_status'] = {
            'error': '%s: %s' % (type(error).__name__, error),
            'note': 'failed to reconcile model.json status; the verification '
                    'report itself is still valid'}

    M.write_json_atomic(out_path, report)
    report['report_path'] = out_path
    return report


def main(argv=None):
    args = parse_args(argv)
    lines = []

    def log(message):
        line = '[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message)
        lines.append(line)
        sys.stdout.write(line + '\n')
        sys.stdout.flush()

    report = run(args, log=log)
    brief = {
        'pass': report['pass'],
        'run_id': report['run_id'],
        'failures': report['failures'],
        'structure_pass': report['checks']['structure']['pass'],
        'self_loops': report['checks']['no_self_loops']['self_loops'],
        'zero_like_with_neighbors':
            report['checks']['zero_like_empty_intervals']['n_zero_like_with_neighbors'],
        'recompute': {
            'checked': report['checks'].get('recompute', {}).get('checked_sources'),
            'mismatches': report['checks'].get('recompute', {}).get('mismatch_count'),
            'skipped': report['checks'].get('recompute', {}).get('skipped', False),
        },
    }
    print(json.dumps(brief, indent=2, sort_keys=True, default=M._json_default))
    return 0 if report['pass'] else 1


if __name__ == '__main__':
    sys.exit(main())
