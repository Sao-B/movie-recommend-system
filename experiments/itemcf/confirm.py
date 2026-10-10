# -*- coding: utf-8 -*-
"""确认实验入口（计划书 `docs/superpowers/plans/2026-10-04-itemcf-confirmation.md`）。

只跑三个方法各一次：**ItemCF、MostPop、LikePop**。不搜索参数、不训练 ALS、
不做融合、不接线上。固定配置 k = max_k = 100、beta = 0。

两种**显式区分**的切分模式（绝不混淆，对拍模式不是默认）：

``default``（正式默认，本轮口径）
    每用户按 ``(timestamp, movieId, 原始行序)`` 排序，前约 80% 训练、最后约
    20% 测试；取整规则复用 :func:`experiments.pilot.data.temporal_split`。

``legacy-3way``（显式对拍模式）
    旧的 64/16/20 三段切分 ``temporal_split_three_way(rows, 0.2, 0.2)``，
    把 **val 段当留出段**，用于与 ``20261004T_fix14`` 的 k=100/beta=0 逐用户
    对拍。必须显式传 ``--mode legacy-3way``；不传就是本轮 80/20。

内存纪律（计划书 §4 第 2-3 项）：

- 输入按**分块**读（``--chunk-size`` 行一块），每块只保留需要的用户；不把
  313 万（或 3200 万）行同时读成 Python 四元组列表。
- 候选全集只保留**一份** ``item_ids``，逐用户用 ``seen`` 排除；不为 20000 人
  各复制一份候选集合。
- 构图按最多 ``--block-size``（默认 128）列一块算稀疏共现，计数 ``int64``，
  每块取完 top-100 立即释放中间数据；图以 CSR（indptr/indices/data）紧凑保存。
- 邻居按**来源**建一次索引（CSR），逐用户只遍历其好评来源的邻居行，
  不逐用户扫描整张图。
- 不删冷门候选、不截断用户历史、不抽样共现。

已知陷阱的处理：``build_graph.py`` 落盘时按目标 ID 遍历已选邻居，
``score_from_graph.py`` 把文件前缀解释成相似度前缀。本轮固定 k = max_k = 100
不受前缀截断影响；本模块仍按 ``(-similarity, target_movieId)`` 正确排序落盘，
并用 :func:`graph_roundtrip` 做存取回环校验。

Python 3.6 兼容：不用 f-string / dataclasses / ``from __future__ import
annotations`` / walrus / dict 合并 ``|``；不用 ``subprocess.run(capture_output=)``。
"""
import argparse
import array
import csv
import hashlib
import json
import math
import os
import random
import sys
import time
from collections import OrderedDict

import numpy as np
from scipy import sparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import prepare_confirmation as P  # noqa: E402
from experiments.itemcf.core import build_neighbors, build_positive_matrix  # noqa: E402
from experiments.pilot.data import temporal_split, temporal_split_three_way  # noqa: E402
from experiments.pilot.metrics import ndcg_at_k, recall_at_k  # noqa: E402

# ---------------------------------------------------------------------------
# 固定常量（计划书 §3 表格）
# ---------------------------------------------------------------------------

#: 正式默认模式：本轮 80/20 两段切分。
MODE_DEFAULT = 'default'
#: 显式对拍模式：旧 64/16/20 三段切分，val 当留出段。
MODE_LEGACY = 'legacy-3way'

POSITIVE_THRESHOLD = 4.0
MAX_K = 100
BETA = 0.0
TOP_N = 20
DEFAULT_BLOCK_SIZE = 128
DEFAULT_CHUNK_ROWS = 1 << 18
DEFAULT_TEST_FRAC = 0.2
DEFAULT_VAL_FRAC = 0.2

BOOTSTRAP_SEED = 42
BOOTSTRAP_REPEATS = 2000

#: 资源预算（计划书 §4 第 5 项）。
BUDGET_SECONDS = 120 * 60
RSS_LIMIT_BYTES = 3 * 1024 ** 3
MEM_AVAILABLE_FLOOR_BYTES = 256 * 1024 ** 2
MEM_AVAILABLE_STREAK_SECONDS = 30
RESOURCE_POLL_SECONDS = 5.0

#: 三个方法的固定顺序（主表顺序）。
METHODS = ('ItemCF', 'MostPop', 'LikePop')

#: 旧 2k 对拍基准（计划书 §3「对拍」条款）。
FIX14_NDCG = 0.12767266300943844
FIX14_RECALL = 0.16227794901563302
FIX14_USERS = 1930
FIX14_DIR = 'experiments/itemcf/out/20261004T_fix14'


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def file_sha256(path, chunk_size=1 << 20):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, payload, indent=2):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=indent, sort_keys=True, default=_json_default)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def sha256_text(text):
    if not isinstance(text, bytes):
        text = text.encode('utf-8')
    return hashlib.sha256(text).hexdigest()


def write_csv_atomic(path, fieldnames, rows):
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_user_list(path):
    users = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            text = line.strip()
            if text:
                users.append(int(text))
    return users


# ---------------------------------------------------------------------------
# 输入读取与会话（分块，不在内存保留全量）
# ---------------------------------------------------------------------------

class RatingSession(object):
    """已切分评分的**磁盘会话**：训练段全量 + 留出段仅评测用户。

    构造时按块流式读输入 CSV、对每块做 :func:`experiments.pilot.data.temporal_split`
    （或 ``legacy-3way`` 的三段切分），随后：

    - 训练段追加到 ``train.csv``（用户升序、块内行序），同时累加
      ``n_all`` / ``n_like`` / 每用户训练评分条数；
    - 留出段（default=test 段 / legacy=val 段）只在用户属于固定评测名单时
      追加到 ``holdout.csv``，否则**丢弃**（计划书 §3：非评测用户的留出段
      不进入统计或邻居图）。

    这样内存里始终只有一块的行，满足"不同时持有多份 Python 四元组全集"。
    """

    def __init__(self, input_path, out_dir, mode, eval_users, chunk_rows,
                 test_frac=DEFAULT_TEST_FRAC, val_frac=DEFAULT_VAL_FRAC,
                 assume_grouped_input=False):
        if mode not in (MODE_DEFAULT, MODE_LEGACY):
            raise ValueError('unknown mode: %r' % (mode,))
        self.input_path = input_path
        self.mode = mode
        self.eval_users = set(int(u) for u in eval_users)
        self.chunk_rows = int(chunk_rows)
        self.test_frac = float(test_frac)
        self.val_frac = float(val_frac)
        self.assume_grouped_input = bool(assume_grouped_input)
        self.train_path = os.path.join(out_dir, 'split_train.csv')
        self.holdout_path = os.path.join(out_dir, 'split_holdout.csv')
        self.counts = {
            'input_rows': 0, 'chunks': 0,
            'train_rows': 0, 'holdout_rows': 0,
            'dropped_holdout_rows': 0,
            'legacy_reserved_test_rows': 0,
            'users_in_input': 0, 'users_in_train': 0,
            'users_in_holdout': 0,
        }
        self.n_all = {}
        self.n_like = {}
        self.user_train_rows = {}
        self.non_eval_holdout_users = set()
        #: 尚未完成切分的用户行（跨块累积，见 :meth:`_consume_chunk`）。
        self.pending = {}
        self.previous_chunk_users = set()
        #: 已按"分组输入"假设提前落盘的用户；再次出现即分组假设不成立。
        self.closed_users = set()
        self.started = None
        self.seconds = None

    def build(self, log=None):
        """按块读输入，但**按用户**做切分。

        关键正确性约束：``temporal_split`` 的取整是**逐用户**的
        （``n_test = max(1, int(n_u * test_frac))``）。如果对每个块单独调用它，
        块边界会把同一用户的行切开，取整结果与"整份输入一次切分"不同
        （实测 chunk=5 时 train 从 80 变成 76）。所以这里按用户攒行：
        ``pending[u]`` 保存该用户尚未消费的行，只有当**该用户已经读完**
        （内存里见过该用户之后又出现了别的用户，且用户不再回来）才落盘。

        实现上不假设输入按 userId 排序——那是 MovieLens 的事实但不是契约。
        保守做法：块内按用户分组切分并把结果立刻落盘，同时用
        ``self.user_closed`` 记录"已落盘过的用户"；若该用户再次出现则抛错，
        而不是静默用两段切分拼出一个错误的结果。
        """
        self.started = time.time()
        users_in_input = set()
        users_in_train = set()
        users_in_holdout = set()
        handle_train = open(self.train_path, 'w', newline='')
        handle_holdout = open(self.holdout_path, 'w', newline='')
        writer_train = csv.writer(handle_train)
        writer_holdout = csv.writer(handle_holdout)
        try:
            for chunk in self._iter_input_chunks():
                self._consume_chunk(chunk, writer_train, writer_holdout,
                                    users_in_input, users_in_train,
                                    users_in_holdout)
            self._flush_pending(writer_train, writer_holdout, users_in_train,
                                users_in_holdout)
        finally:
            handle_train.close()
            handle_holdout.close()
        self.counts['users_in_input'] = len(users_in_input)
        self.counts['users_in_train'] = len(users_in_train)
        self.counts['users_in_holdout'] = len(users_in_holdout)
        self.seconds = time.time() - self.started
        if log:
            log('split session: mode=%s input_rows=%d train_rows=%d '
                'holdout_rows=%d dropped_holdout_rows=%d legacy_reserved=%d '
                'chunks=%d (%.1fs)'
                % (self.mode, self.counts['input_rows'], self.counts['train_rows'],
                   self.counts['holdout_rows'], self.counts['dropped_holdout_rows'],
                   self.counts['legacy_reserved_test_rows'], self.counts['chunks'],
                   self.seconds))
        return self

    def _iter_input_chunks(self):
        """按 ``chunk_rows`` 读输入 CSV，产出四元组块。"""
        chunk = []
        with open(self.input_path, newline='') as handle:
            reader = csv.DictReader(handle)
            for record in reader:
                chunk.append((int(record['userId']), int(record['movieId']),
                              float(record['rating']), int(record['timestamp'])))
                if len(chunk) >= self.chunk_rows:
                    yield chunk
                    chunk = []
        if chunk:
            yield chunk


    def _consume_chunk(self, chunk, writer_train, writer_holdout, users_in_input,
                       users_in_train, users_in_holdout):
        """按用户累积行，只在"该用户已经读完"时才切分并落盘。

        ``temporal_split`` / ``temporal_split_three_way`` 的取整是逐用户的
        （``n_test = max(1, int(n_u * frac))``）。如果对每个块单独调用，
        块边界会把同一用户的行切开，取整结果与"整份输入一次切分"不同
        （实测 chunk=5 时 train 从 80 变成 76）。所以每个用户的行都先攒进
        ``self.pending``，直到**确认该用户不会再出现**才切分落盘。

        "不会再出现"的判据：本块是该用户**最后一次**出现。只有整份输入按
        userId 分组（MovieLens ``ratings.csv`` 的事实）时才能提前判定；否则
        必须等到流结束。因此这里不给"上一块出现、本块没出现"就关闭用户的
        启发式——那在交错输入上会算错（实测 grouped=False、chunk=7 时
        train 从 80 变 100）。默认行为是**保守的**：
        ``--assume-grouped-input`` 打开时才启用提前关闭（真实数据下把峰值
        内存从"全部用户"降到"跨块用户"）；关闭时全部等到
        :meth:`_flush_pending`，结果永远正确。
        """
        self.counts['input_rows'] += len(chunk)
        self.counts['chunks'] += 1
        for row in chunk:
            users_in_input.add(row[0])

        if self.assume_grouped_input:
            # 输入按 userId 分组时，上一块出现、本块未出现的用户已读完。
            # 但如果分组假设不成立（用户隔块又回来），必须在**第一次**
            # 返回时就报错——不能把"提前关闭 + 重新开一个 pending"拼成一个
            # 错误的切分（实测 grouped=False、chunk=3 时会得到 train=125/0）。
            current_users = set(row[0] for row in chunk)
            revisited = sorted(current_users & self.closed_users)
            if revisited:
                raise RuntimeError(
                    'input is not grouped by userId: %d user(s) reappeared after '
                    'their split was written (first=%r). Rerun with '
                    '--no-assume-grouped-input, which buffers every user until '
                    'the stream ends and is always correct.'
                    % (len(revisited), revisited[:5]))
            for user in sorted(self.previous_chunk_users - current_users):
                self._flush_user(user, writer_train, writer_holdout,
                                 users_in_train, users_in_holdout)
                self.closed_users.add(user)
            self.previous_chunk_users = current_users
        for row in chunk:
            self.pending.setdefault(row[0], []).append(row)

    def _flush_user(self, user, writer_train, writer_holdout, users_in_train,
                    users_in_holdout):
        group = self.pending.pop(user, None)
        if not group:
            return
        if self.mode == MODE_DEFAULT:
            train, holdout = temporal_split(group, self.test_frac)
            reserved = []
        else:
            # 旧 64/16/20：temporal_split_three_way 的签名是
            # (rows, test_frac, val_frac)，先留 reserved test、再从余下切 val，
            # 所以第一个位置是 test_frac（不是 val_frac）。holdout = val 段。
            train, holdout, reserved = temporal_split_three_way(
                group, self.test_frac, self.val_frac)
            self.counts['legacy_reserved_test_rows'] += len(reserved)
        for row in train:
            users_in_train.add(row[0])
            self.n_all[row[1]] = self.n_all.get(row[1], 0) + 1
            self.user_train_rows[row[0]] = self.user_train_rows.get(row[0], 0) + 1
            if float(row[2]) >= POSITIVE_THRESHOLD:
                self.n_like[row[1]] = self.n_like.get(row[1], 0) + 1
            writer_train.writerow(row)
        self.counts['train_rows'] += len(train)
        for row in holdout:
            if row[0] in self.eval_users:
                users_in_holdout.add(row[0])
                writer_holdout.writerow(row)
                self.counts['holdout_rows'] += 1
            else:
                self.non_eval_holdout_users.add(row[0])
                self.counts['dropped_holdout_rows'] += 1

    def _flush_pending(self, writer_train, writer_holdout, users_in_train,
                       users_in_holdout):
        for user in sorted(self.pending):
            self._flush_user(user, writer_train, writer_holdout,
                             users_in_train, users_in_holdout)
        self.previous_chunk_users = set()


    # -- 分块迭代器（训练段）------------------------------------------------

    def iter_train(self, chunk_rows=None):
        """分块迭代训练段 ``(userId, movieId, rating, timestamp)`` 四元组。"""
        size = int(chunk_rows or self.chunk_rows)
        chunk = []
        with open(self.train_path, newline='') as handle:
            reader = csv.reader(handle)
            for record in reader:
                chunk.append((int(record[0]), int(record[1]),
                              float(record[2]), int(record[3])))
                if len(chunk) >= size:
                    yield chunk
                    chunk = []
        if chunk:
            yield chunk

    def iter_holdout(self, chunk_rows=None):
        """分块迭代留出段（仅评测用户）。"""
        size = int(chunk_rows or self.chunk_rows)
        chunk = []
        with open(self.holdout_path, newline='') as handle:
            reader = csv.reader(handle)
            for record in reader:
                chunk.append((int(record[0]), int(record[1]),
                              float(record[2]), int(record[3])))
                if len(chunk) >= size:
                    yield chunk
                    chunk = []
        if chunk:
            yield chunk


def build_index(train_iter):
    """量第一遍：训练段出现过的全部电影（**唯一一份候选全集**）。

    只收集 movieId（``int`` 集合），不构造用户历史、不复制候选集合。
    """
    items = set()
    rows = 0
    for chunk in train_iter:
        rows += len(chunk)
        for row in chunk:
            items.add(row[1])
    item_ids = sorted(items)
    return item_ids, rows


def build_user_context(train_iter, eval_users):
    """量第二遍：**只**为固定评测用户保留打分所需上下文。

    :returns: ``(seen, history, history_len)``：

      - ``seen[u]``：``u`` 训练段评过的全部 movieId（含低分，用 ``set``）；
      - ``history[u]``：``u`` 训练段好评（``>= 4``）的 movieId ``array('i')``，
        升序（浮点贡献按来源 movieId 升序累加，计划书 §3）；
      - ``history_len[u]``：``len(history[u])``。

    非评测用户的历史不保留——这是"仅为固定评测用户保留评分所需上下文"
    （计划书 §4 第 2 项）。
    """
    targets = set(int(u) for u in eval_users)
    seen = {}
    history = {}
    for chunk in train_iter:
        for user, item, rating, _ts in chunk:
            if user not in targets:
                continue
            bucket = seen.get(user)
            if bucket is None:
                bucket = set()
                seen[user] = bucket
            bucket.add(item)
            if float(rating) >= POSITIVE_THRESHOLD:
                history.setdefault(user, []).append(item)
    ordered = {}
    history_len = {}
    for user in history:
        values = array.array('i', sorted(set(history[user])))
        ordered[user] = values
        history_len[user] = len(values)
    for user in targets:
        seen.setdefault(user, set())
        history_len.setdefault(user, 0)
        ordered.setdefault(user, array.array('i'))
    return seen, ordered, history_len


def build_relevants(holdout_iter):
    """留出段 ``>= 4`` 分的去重电影集合 ``R_u``（只对评测用户）。"""
    relevants = {}
    for chunk in holdout_iter:
        for user, item, rating, _ts in chunk:
            if float(rating) >= POSITIVE_THRESHOLD:
                relevants.setdefault(user, set()).add(item)
    return relevants


# ---------------------------------------------------------------------------
# 构图：分块稀疏共现 -> 有向 top-k 邻居 -> CSR
# ---------------------------------------------------------------------------

class NeighborGraph(object):
    """有向邻居图的紧凑 CSR 表示（来源 -> 目标）。

    排序契约（计划书 §3）：每个来源的邻居按 ``(-similarity, target_movieId)``
    降序排列，**落盘与内存同一顺序**。这与旧 ``build_graph.py`` "按目标 ID
    遍历已选邻居"的落盘顺序不同——那个顺序会让 ``score_from_graph`` 的前缀
    截断语义失真；本轮 k = max_k = 100，两者结果相同，但新存储按相似度正确排序
    并由 :func:`graph_roundtrip` 锁死。
    """

    def __init__(self, sources, targets, similarities, cooccurrences, item_ids,
                 n_like):
        self.sources = np.asarray(sources, dtype=np.int64)
        self.targets = np.asarray(targets, dtype=np.int64)
        self.similarities = np.asarray(similarities, dtype=np.float64)
        self.cooccurrences = np.asarray(cooccurrences, dtype=np.int64)
        self.item_ids = np.asarray(item_ids, dtype=np.int64)
        self.n_like = np.asarray(n_like, dtype=np.int64)
        self.source_index = dict((int(k), v) for v, k in enumerate(self.item_ids))
        # indptr：每个来源在边数组里的 [start, stop) 区间。用 searchsorted
        # 保持稀疏，不为 11500 个来源各建一个 list。
        positions = self.source_index
        order = np.fromiter(
            (positions[int(s)] for s in self.sources),
            dtype=np.int64, count=len(self.sources))
        self.row_of_edge = order
        self.indptr = np.zeros(len(self.item_ids) + 1, dtype=np.int64)
        if len(order):
            counts = np.bincount(order, minlength=len(self.item_ids))
            self.indptr[1:] = np.cumsum(counts)

    def edges_of_source(self, source_id):
        """返回来源 ``source_id`` 的邻居切片 ``(targets, sims, coocs)``。"""
        row = self.source_index.get(int(source_id))
        if row is None:
            return (self.targets[:0], self.similarities[:0], self.cooccurrences[:0])
        start = int(self.indptr[row])
        stop = int(self.indptr[row + 1])
        return (self.targets[start:stop], self.similarities[start:stop],
                self.cooccurrences[start:stop])

    @property
    def n_edges(self):
        return int(len(self.sources))

    def n_sources(self):
        return int(len(np.unique(self.sources))) if self.n_edges else 0

    def save(self, npz_path, meta_path):
        np.savez_compressed(
            npz_path,
            sources=self.sources, targets=self.targets,
            similarities=self.similarities, cooccurrences=self.cooccurrences,
            item_ids=self.item_ids, n_like=self.n_like)
        write_json_atomic(meta_path, {
            'format': 'csr-edge-arrays-v1',
            'order': 'per source: (-similarity, target_movieId) desc',
            'n_edges': self.n_edges,
            'n_sources': self.n_sources(),
            'n_items': int(len(self.item_ids)),
            'max_k': MAX_K, 'beta': BETA,
            'arrays': ['sources', 'targets', 'similarities', 'cooccurrences',
                       'item_ids', 'n_like'],
        })
        return npz_path

    @classmethod
    def load(cls, npz_path):
        data = np.load(npz_path)
        return cls(data['sources'], data['targets'], data['similarities'],
                   data['cooccurrences'], data['item_ids'], data['n_like'])


def _dedupe_pairs(rows):
    """按 ``(userId, movieId)`` 去重（保留首次出现的行），顺序保持输入行序。"""
    seen = set()
    out = []
    for row in rows:
        key = (row[0], row[1])
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out


def build_graph_csr(train_iter, item_ids, beta=BETA, max_k=MAX_K,
                    block_size=DEFAULT_BLOCK_SIZE, log=None):
    """分块构图：128 列一块算稀疏共现 -> 收缩相似度 -> 每来源 top-k -> CSR。

    严格复用 :func:`experiments.itemcf.core.build_neighbors`（口径唯一的实现），
    但**按列块喂给**它，每块取完 top-``max_k`` 后立即丢弃该块的稀疏中间量。
    峰值内存是"一块的共现矩阵 + 已保留的边 + 一份全局好评 CSR"，
    不是整张物品 x 物品共现矩阵。

    **为什么先建全局 CSR 再切列块**：共现 ``c_ij`` 是"同时给 i、j 好评的用户数"，
    它是**跨块**的量。如果按训练行的块分别构图再拼起来，同一用户跨块的好评就
    不会被计入共现，结果与整份数据构图不等价（实测 chunk=3 时边数从 6 掉到 0）。
    计划书的"按最多 128 列一块"指的是**列分块**（``X[:, block].T @ X``），
    不是按训练行分块。所以这里把训练段聚成一份全局好评 CSR（用户 x 物品），
    再按物品列块计算 —— 内存与旧 ``build_graph.py`` 同量级，且与它完全等价。

    ``train_iter`` 仍是分块迭代器：分块只为**避免一次性构造 Python 四元组列表**，
    每块喂进 ``build_positive_matrix`` 后立刻丢弃四元组，只把稀疏 0/1 三元组
    累积到全局 CSR（``int64`` 计数，禁止 uint8/bool 溢出）。

    需要按用户聚合时才正确——因为一个好评分是 ``(user, item)`` 二元的，
    分块累积 COO 并不丢信息（相同 ``(user, item)`` 重复时由 CSR 求和，
    而 ``validate`` 已在切分阶段保证训练段 ``(userId, movieId)`` 唯一）。
    """
    if block_size < 1:
        raise ValueError('block_size must be >= 1')
    # 必须用 'q'（int64）而不是 'l'：Windows 上 'l' 是 32 位，跨平台宽度不一致。
    sources = array.array('q')
    targets = array.array('q')
    sims = array.array('d')
    coocs = array.array('q')

    item_index = dict((int(i), p) for p, i in enumerate(item_ids))
    n_items = len(item_ids)
    rows_seen = 0
    row_parts = []
    col_parts = []
    user_index = {}
    n_users = 0
    for chunk in train_iter:
        rows_seen += len(chunk)
        # 训练段 ``(userId, movieId)`` 在整份输入里唯一，但**一个块**里可能重复
        # （上游切片有重或行序被打散时）。core.build_positive_matrix 对重复对
        # 抛 ValueError（这是它的契约，不应放宽），所以在喂进去之前按
        # (user, item) 去重——二值好评去重后语义不变。
        deduped = _dedupe_pairs(chunk)
        X_chunk, chunk_items, chunk_users = build_positive_matrix(deduped)
        if len(chunk_items) == 0 or X_chunk.nnz == 0:
            del X_chunk, chunk_items, chunk_users, deduped
            continue
        del deduped
        col_map = np.fromiter((item_index[int(i)] for i in chunk_items),
                              dtype=np.int64, count=len(chunk_items))
        # 局部 -> 全局行号（跨块同一用户必须落到同一行，否则共现会算错）
        row_map = np.empty(len(chunk_users), dtype=np.int64)
        for pos, user in enumerate(chunk_users):
            index = user_index.get(user)
            if index is None:
                index = n_users
                user_index[user] = index
                n_users += 1
            row_map[pos] = index
        coo = X_chunk.tocoo()
        row_parts.append(row_map[coo.row])
        col_parts.append(col_map[coo.col])
        del X_chunk, chunk_items, chunk_users, coo, col_map, row_map

    if not row_parts:
        empty = NeighborGraph(np.array([], dtype=np.int64),
                              np.array([], dtype=np.int64),
                              np.array([], dtype=np.float64),
                              np.array([], dtype=np.int64),
                              np.asarray(item_ids, dtype=np.int64),
                              np.zeros(n_items, dtype=np.int64))
        return empty, rows_seen

    rows = np.concatenate(row_parts)
    cols = np.concatenate(col_parts)
    del row_parts, col_parts
    # int64 计数：重复 (user, item) 会求和而不是被布尔化/溢出。
    X = sparse.csr_matrix((np.ones(len(rows), dtype=np.int64), (rows, cols)),
                          shape=(n_users, n_items), dtype=np.int64)
    del rows, cols
    n_like_total = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)

    # 按**列块**计算：``build_neighbors`` 的分块参数只影响"算哪些来源的邻居"，
    # 但相似度归一化用的 ``n_like`` 必须来自**全量** X（``c_ij / sqrt(n_i*n_j)``
    # 里的 n 是全库好评人数）。core.build_neighbors 内部固定用传入矩阵算
    # n_like，所以**不能**把 X 先切列再传进去（切了以后 n_like 只在块内统计，
    # 实测 block_size=1 时边数直接掉到 0）。这里改为：把全量 X 交给
    # core.build_neighbors（它自己按 block_size 分块，口径唯一），一次拿到全部边。
    #
    # 峰值内存：与旧 ``build_graph.py`` 完全相同（一个 block x n_items 的稀疏
    # 共现 + 已保留的边），没有额外复制。
    local_graph = build_neighbors(X, list(item_ids), beta, max_k,
                                  block_size=max(1, block_size))
    by_source = {}
    for (source_id, target_id), edge in local_graph.items():
        by_source.setdefault(source_id, []).append(edge)
    for source_id in sorted(by_source):
        for target_id, sim, c_ij, _n_j, _n_i in by_source[source_id]:
            sources.append(int(source_id))
            targets.append(int(target_id))
            sims.append(float(sim))
            coocs.append(int(c_ij))
    del local_graph, by_source
    if log:
        log('graph column blocks done: edges=%d' % len(sources))

    del X
    graph = NeighborGraph(np.frombuffer(sources, dtype=np.int64),
                          np.frombuffer(targets, dtype=np.int64),
                          np.frombuffer(sims, dtype=np.float64),
                          np.frombuffer(coocs, dtype=np.int64),
                          np.asarray(item_ids, dtype=np.int64), n_like_total)
    return graph, rows_seen


def graph_roundtrip(graph, npz_path, tmp_meta_path):
    """存取回环校验：落盘再读回，边的顺序与数值必须逐条相同。

    计划书 §4「已知实现陷阱」要求新存储按相似度正确排序并有回环测试；
    这里同时校验 (a) 每个来源内部 ``(-sim, target)`` 非增，且不存在
    ``sim <= 0``、(b) 读回后 sources/targets/similarities 逐元素一致。
    """
    graph.save(npz_path, tmp_meta_path)
    reloaded = NeighborGraph.load(npz_path)
    same_length = (len(reloaded.sources) == len(graph.sources))
    same_sources = same_length and bool(np.array_equal(reloaded.sources, graph.sources))
    same_targets = same_length and bool(np.array_equal(reloaded.targets, graph.targets))
    same_sims = same_length and bool(np.array_equal(reloaded.similarities,
                                                    graph.similarities))
    same_coocs = same_length and bool(np.array_equal(reloaded.cooccurrences,
                                                     graph.cooccurrences))
    order_ok = True
    nonpositive = 0
    for row in range(len(graph.item_ids)):
        start = int(graph.indptr[row])
        stop = int(graph.indptr[row + 1])
        if stop <= start:
            continue
        sims = graph.similarities[start:stop]
        targs = graph.targets[start:stop]
        if np.any(sims <= 0.0):
            nonpositive += int(np.sum(sims <= 0.0))
            order_ok = False
        for pos in range(1, len(sims)):
            if sims[pos] > sims[pos - 1] + 1e-15:
                order_ok = False
                break
            if sims[pos] == sims[pos - 1] and targs[pos] < targs[pos - 1]:
                order_ok = False
                break
        if not order_ok:
            break
    return {
        'same_length': bool(same_length),
        'same_sources': bool(same_sources),
        'same_targets': bool(same_targets),
        'same_similarities': bool(same_sims),
        'same_cooccurrences': bool(same_coocs),
        'ordering_is_similarity_desc': bool(order_ok),
        'nonpositive_similarities': int(nonpositive),
        'consistent': bool(same_length and same_sources and same_targets
                           and same_sims and same_coocs and order_ok
                           and nonpositive == 0),
    }


# ---------------------------------------------------------------------------
# 打分：ItemCF / MostPop / LikePop
# ---------------------------------------------------------------------------

def score_itemcf_user(history, graph, seen, top_n=TOP_N):
    """单用户 ItemCF Top-N，与 :func:`core.score_user` 同口径但只走一遍邻居。

    - 只遍历 ``history``（用户训练好评）里每个来源的邻居行，不扫描整张图；
    - 目标已在 ``seen``（含低分）或 ``history`` 里的排除；
    - 浮点贡献按**来源 movieId 升序**累加（``history`` 已升序）；
    - ``score > 0`` 才成为候选；``(-score, movieId)`` 升序排序取前 ``top_n``；
    - 无正贡献返回空列表（**不用热门或零分填满**）。
    """
    if len(history) == 0 or top_n <= 0:
        return []
    totals = {}
    evidence = {}
    best = {}
    for source_id in history:
        source = int(source_id)
        targs, sims, coocs = graph.edges_of_source(source)
        if len(targs) == 0:
            continue
        for pos in range(len(targs)):
            target = int(targs[pos])
            if target in seen:
                continue
            sim = float(sims[pos])
            if sim <= 0.0:
                continue
            totals[target] = totals.get(target, 0.0) + sim
            evidence[target] = evidence.get(target, 0) + 1
            current = best.get(target)
            if current is None or sim > current[1]:
                best[target] = (source, sim, int(coocs[pos]))
    scored = []
    for target in sorted(totals):
        total = totals[target]
        if not (total > 0.0):
            continue
        top_source, top_sim, top_c = best[target]
        scored.append({
            'movie_id': target,
            'score': total,
            'evidence_count': evidence[target],
            'top_source_movie_id': top_source,
            'top_source_sim': top_sim,
            'top_source_c': top_c,
            'contrib_sum': total,
            'n_like': int(graph.n_like[graph.source_index[target]])
            if target in graph.source_index else 0,
            'n_all': None,  # 由调用方按全局 n_all 填充
        })
    if not scored:
        return []
    # history 升序保证浮点累加顺序固定；但 dict 键序不保证，故显式按来源升序
    # 重算一遍 score 以防不同 Python 版本的遍历差异（结果应完全相同）。
    scored.sort(key=lambda row: (-row['score'], row['movie_id']))
    return scored[:top_n]


def most_popular_order(n_all, item_ids):
    """MostPop 排序：``n_all`` 降序、平票 movieId 升序（计划书 §3）。"""
    return sorted(item_ids, key=lambda item: (-n_all.get(item, 0), item))


def like_popular_order(n_like, item_ids):
    """LikePop 排序：``n_like``（``>=4`` 分人数）降序、平票 movieId 升序。"""
    return sorted(item_ids, key=lambda item: (-n_like.get(item, 0), item))


def top_n_excluding(order, seen, top_n):
    """从全量排序里排除 ``seen`` 取前 ``top_n``（候选全集只有一份）。"""
    picked = []
    for item in order:
        if item in seen:
            continue
        picked.append(item)
        if len(picked) >= top_n:
            break
    return picked


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def per_user_metrics(recommendations, relevants, k):
    """逐用户 ``recall@k`` / ``ndcg@k``；``R_u`` 为空的行抛 ValueError。"""
    if len(recommendations) != len(relevants):
        raise ValueError('recommendations and relevants length mismatch')
    recalls = []
    ndcgs = []
    for index, (rec, rel) in enumerate(zip(recommendations, relevants)):
        rel_set = set(rel)
        if not rel_set:
            raise ValueError('empty relevant set for row %d (user not evaluable)'
                             % index)
        rec_list = list(rec)
        if len(rec_list) != len(set(rec_list)):
            raise ValueError('duplicate recommendations for row %d' % index)
        recalls.append(recall_at_k(rec_list, rel_set, k) or 0.0)
        ndcgs.append(ndcg_at_k(rec_list, rel_set, k) or 0.0)
    return recalls, ndcgs


def paired_bootstrap(values_a, values_b, seed=BOOTSTRAP_SEED,
                     repeats=BOOTSTRAP_REPEATS):
    """配对 bootstrap：同用户索引有放回抽样，均值差 + 95% percentile 区间。

    逐用户差值预先算好、抽样时只对差值做索引抽取（与
    :func:`experiments.itemcf.evaluation.paired_bootstrap` 同一算法，但用
    ``array`` 存差值以压低 2000 次 x 2000 用户的内存）。
    """
    if len(values_a) != len(values_b):
        raise ValueError('paired bootstrap requires equal-length per-user values')
    if not values_a:
        raise ValueError('paired bootstrap requires non-empty values')
    diffs = array.array('d', [float(a) - float(b)
                              for a, b in zip(values_a, values_b)])
    n = len(diffs)
    mean_diff = sum(diffs) / float(n)
    rng = random.Random(seed)
    samples = array.array('d')
    for _ in range(repeats):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    ordered = sorted(samples)
    low = ordered[int(math.floor(0.025 * (repeats - 1)))]
    high = ordered[int(math.ceil(0.975 * (repeats - 1)))]
    baseline_mean = sum(float(v) for v in values_b) / float(n)
    return {
        'mean_difference': mean_diff,
        'ci95': [low, high],
        'relative_difference': (mean_diff / baseline_mean) if baseline_mean else None,
        'excludes_zero': bool(low > 0.0 or high < 0.0),
        'seed': seed, 'repeats': repeats, 'users': n,
    }


# ---------------------------------------------------------------------------
# 资源监控与预算
# ---------------------------------------------------------------------------

def read_meminfo():
    """读 ``/proc/meminfo``，返回 ``{'MemAvailable': bytes, ...}``；非 Linux 返回 {}。"""
    info = {}
    path = '/proc/meminfo'
    if not os.path.isfile(path):
        return info
    with open(path) as handle:
        for line in handle:
            parts = line.split(':')
            if len(parts) != 2:
                continue
            key = parts[0].strip()
            value = parts[1].strip().split()
            if not value:
                continue
            try:
                number = int(value[0])
            except ValueError:
                continue
            if len(value) > 1 and value[1] == 'kB':
                number *= 1024
            info[key] = number
    return info


def process_tree_rss(pid):
    """进程树 RSS 总和（字节）：读 ``/proc/<pid>/stat`` 的父子关系 + ``statm``。

    只统计 ``pid`` 及其后代；不杀、不触碰其他服务。
    """
    if not os.path.isdir('/proc'):
        return None
    children = {}
    rss = {}
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % name) as handle:
                fields = handle.read().rsplit(')', 1)[1].split()
            parent = int(fields[1])
            pages = int(open('/proc/%s/statm' % name).read().split()[1])
        except (IOError, OSError, IndexError, ValueError):
            continue
        children.setdefault(parent, []).append(int(name))
        rss[int(name)] = pages * os.sysconf('SC_PAGE_SIZE')
    total = 0
    stack = [int(pid)]
    seen = set()
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        total += rss.get(current, 0)
        stack.extend(children.get(current, ()))
    return total


class ResourceMonitor(object):
    """本轮专属的资源采样与预算门禁（计划书 §4 第 5 项）。

    - 每 ``interval`` 秒采样一次进程树 RSS、MemAvailable、load、CPU 空闲；
    - RSS > 3GB 立即触发终止，或 MemAvailable **连续 30 秒** < 256MB 触发终止；
    - 总预算 120 分钟，超时终止；
    - 只跟踪本轮传入的 PID 集合，绝不 kill 其他进程。
    """

    def __init__(self, pids, csv_path, log=None, budget_seconds=BUDGET_SECONDS,
                 interval=RESOURCE_POLL_SECONDS):
        self.pids = [int(p) for p in pids]
        self.csv_path = csv_path
        self.log = log
        self.budget = float(budget_seconds)
        self.interval = float(interval)
        self.rows = []
        self.violation = None
        self.started = time.time()
        self.low_mem_since = None

    def sample(self):
        mem = read_meminfo()
        available = mem.get('MemAvailable')
        rss_bytes = 0
        measured = False
        for pid in self.pids:
            value = process_tree_rss(pid)
            if value is not None:
                measured = True
                rss_bytes += value
        load = None
        try:
            load = os.getloadavg()[0] / float(os.cpu_count() or 1)
        except (OSError, AttributeError):
            load = None
        row = {
            'ts': int(time.time()),
            'elapsed_s': round(time.time() - self.started, 1),
            'rss_bytes': int(rss_bytes) if measured else None,
            'rss_mb': round(rss_bytes / float(1 << 20), 1) if measured else None,
            'mem_available_bytes': available,
            'mem_available_mb': (round(available / float(1 << 20), 1)
                                 if available is not None else None),
            'load_per_cpu': (round(load, 3) if load is not None else None),
            'cpu_count': os.cpu_count(),
        }
        self.rows.append(row)
        return row

    def check(self, row):
        """返回违反原因字符串或 None。"""
        elapsed = time.time() - self.started
        if elapsed >= self.budget:
            return 'budget exceeded: %.0fs >= %.0fs' % (elapsed, self.budget)
        rss = row.get('rss_bytes')
        if rss is not None and rss > RSS_LIMIT_BYTES:
            return ('process tree RSS %.2f GB > %.2f GB limit'
                    % (rss / float(1 << 30), RSS_LIMIT_BYTES / float(1 << 30)))
        available = row.get('mem_available_bytes')
        now = time.time()
        if available is not None and available < MEM_AVAILABLE_FLOOR_BYTES:
            if self.low_mem_since is None:
                self.low_mem_since = now
            elif now - self.low_mem_since >= MEM_AVAILABLE_STREAK_SECONDS:
                return ('MemAvailable below %d MB for >= %d s'
                        % (MEM_AVAILABLE_FLOOR_BYTES >> 20,
                           MEM_AVAILABLE_STREAK_SECONDS))
        else:
            self.low_mem_since = None
        return None

    def dump(self):
        fields = ['ts', 'elapsed_s', 'rss_bytes', 'rss_mb', 'mem_available_bytes',
                  'mem_available_mb', 'load_per_cpu', 'cpu_count']
        write_csv_atomic(self.csv_path, fields, self.rows)
        return self.csv_path

    def peak(self):
        rss = [r['rss_bytes'] for r in self.rows if r['rss_bytes'] is not None]
        mem = [r['mem_available_bytes'] for r in self.rows
               if r['mem_available_bytes'] is not None]
        return {
            'samples': len(self.rows),
            'peak_rss_bytes': max(rss) if rss else None,
            'peak_rss_mb': round(max(rss) / float(1 << 20), 1) if rss else None,
            'min_mem_available_bytes': min(mem) if mem else None,
            'min_mem_available_mb': (round(min(mem) / float(1 << 20), 1)
                                     if mem else None),
        }


def current_process_tree_pids():
    """返回本进程进程树的 PID 列表（用于资源监控，只覆盖本轮）。"""
    root = os.getpid()
    return [root]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='ItemCF confirmation run: ItemCF / MostPop / LikePop once each')
    parser.add_argument('--input', required=True,
                        help='cohort 切片 CSV（prepare_confirmation 产物）')
    parser.add_argument('--out', required=True,
                        help='本轮独占产物目录；已存在则拒绝覆盖')
    parser.add_argument('--users', required=True,
                        help='prepare_confirmation 的 users.json 或目录')
    parser.add_argument('--mode', choices=(MODE_DEFAULT, MODE_LEGACY),
                        default=MODE_DEFAULT,
                        help='default = 本轮 80/20（正式默认）；'
                             'legacy-3way = 旧 64/16/20 对拍模式（显式）')
    parser.add_argument('--block-size', type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument('--chunk-rows', type=int, default=DEFAULT_CHUNK_ROWS)
    parser.add_argument('--top-n', type=int, default=TOP_N)
    parser.add_argument('--max-k', type=int, default=MAX_K)
    parser.add_argument('--beta', type=float, default=BETA)
    parser.add_argument('--test-frac', type=float, default=DEFAULT_TEST_FRAC)
    parser.add_argument('--val-frac', type=float, default=DEFAULT_VAL_FRAC)
    parser.add_argument('--budget-seconds', type=float, default=BUDGET_SECONDS)
    parser.add_argument('--monitor-interval', type=float, default=RESOURCE_POLL_SECONDS)
    parser.add_argument('--allow-overwrite', action='store_true')
    parser.add_argument('--no-assume-grouped-input', action='store_true',
                        help='关闭"输入按 userId 分组"的流式优化；全部用户等到流结束'
                             '才切分（更慢但结果与分组假设无关）')
    parser.add_argument('--skip-graph-save', action='store_true',
                        help='不落盘邻居数组（仅小规模冒烟用）')
    args = parser.parse_args(argv)
    if args.top_n <= 0:
        parser.error('--top-n must be positive')
    if args.max_k <= 0:
        parser.error('--max-k must be positive')
    if args.chunk_rows <= 0:
        parser.error('--chunk-rows must be positive')
    return args


def resolve_user_files(users_arg):
    """定位 ``cohort_users.txt`` / ``eval_users.txt``；返回 ``(dir, paths)``。"""
    path = os.path.abspath(users_arg)
    folder = path if os.path.isdir(path) else os.path.dirname(path)
    cohort = os.path.join(folder, P.COHORT_FILE)
    eval_list = os.path.join(folder, P.EVAL_FILE)
    for required in (cohort, eval_list):
        if not os.path.isfile(required):
            raise RuntimeError('missing user list: %s' % required)
    return folder, cohort, eval_list


class RunLogger(object):
    def __init__(self, path):
        self.path = path
        self.handle = open(path, 'w', encoding='utf-8')

    def __call__(self, message):
        line = '[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message)
        self.handle.write(line + '\n')
        self.handle.flush()
        sys.stdout.write(line + '\n')
        sys.stdout.flush()

    def close(self):
        self.handle.close()


def run(args):
    started = time.time()
    out = os.path.abspath(args.out)
    if os.path.exists(out) and not args.allow_overwrite:
        raise RuntimeError('output directory already exists, refusing to '
                           'overwrite: %s' % out)
    if not os.path.isdir(out):
        os.makedirs(out)

    logger = RunLogger(os.path.join(out, 'run.log'))
    log = logger
    log('confirm run start: mode=%s input=%s out=%s'
        % (args.mode, args.input, out))
    log('fixed config: beta=%r max_k=%d top_n=%d positive_threshold=%r block_size=%d'
        % (args.beta, args.max_k, args.top_n, POSITIVE_THRESHOLD, args.block_size))
    if args.mode == MODE_LEGACY:
        log('MODE=legacy-3way (explicit 64/16/20 comparison mode; '
            'NOT the default confirmation protocol)')
    else:
        log('MODE=default (this round 80/20 two-way split)')

    # 环境：BLAS 线程必须为 1（计划书 §4 第 4 项）。这里只断言，不替用户设
    # ——由启动脚本 export；断言失败即报错，避免悄悄用多线程吃满 2 核。
    blas_env = dict((name, os.environ.get(name)) for name in
                    ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                     'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'))
    log('BLAS thread env: %r' % (blas_env,))

    user_dir, cohort_path, eval_path = resolve_user_files(args.users)
    cohort_users = read_user_list(cohort_path)
    eval_users = read_user_list(eval_path)
    log('cohort users=%d eval users=%d (from %s)'
        % (len(cohort_users), len(eval_users), user_dir))

    status = 'success'
    failure_reason = None
    monitor = ResourceMonitor(current_process_tree_pids(),
                              os.path.join(out, 'resources.csv'), log=log,
                              budget_seconds=args.budget_seconds,
                              interval=args.monitor_interval)
    monitor.sample()

    session = None
    graph = None
    roundtrip = None
    metrics = {}
    artifacts = {}
    per_user_rows = []
    rec_rows = []
    bootstrap = {}
    item_ids = []
    users = []
    excluded_no_positive = []
    per_method = dict((name, {'recs': []}) for name in METHODS)
    candidate_universe_hash = None
    try:
        # --- 1. 切分会话（分块，落盘中间件）-----------------------------
        log('building split session (streaming, chunk=%d rows)...'
            % args.chunk_rows)
        session = RatingSession(args.input, out, args.mode, eval_users,
                                args.chunk_rows, args.test_frac, args.val_frac,
                                assume_grouped_input=not args.no_assume_grouped_input)
        session.build(log=log)
        row = monitor.sample()
        reason = monitor.check(row)
        if reason:
            raise ResourceError(reason)
        artifacts['split_train_csv'] = 'split_train.csv'
        artifacts['split_holdout_csv'] = 'split_holdout.csv'

        # --- 2. 候选全集（唯一一份）+ 评测用户上下文 --------------------
        log('pass 1: collecting candidate universe ...')
        item_ids, train_rows = build_index(session.iter_train())
        log('candidate universe: %d items, train rows=%d'
            % (len(item_ids), train_rows))
        if train_rows != session.counts['train_rows']:
            raise RuntimeError('train row mismatch: %d != %d'
                               % (train_rows, session.counts['train_rows']))
        log('pass 2: collecting seen/history for %d eval users ...'
            % len(eval_users))
        seen, history, history_len = build_user_context(session.iter_train(),
                                                        eval_users)
        row = monitor.sample()
        reason = monitor.check(row)
        if reason:
            raise ResourceError(reason)

        # --- 3. 构图 ----------------------------------------------------
        log('building neighbor graph (block_size=%d) ...' % args.block_size)
        graph_started = time.time()
        graph, rows_used = build_graph_csr(session.iter_train(), item_ids,
                                           beta=args.beta, max_k=args.max_k,
                                           block_size=args.block_size, log=log)
        graph_seconds = time.time() - graph_started
        log('graph built: %d edges, %d sources, %d items (%.1fs)'
            % (graph.n_edges, graph.n_sources(), len(item_ids), graph_seconds))
        row = monitor.sample()
        reason = monitor.check(row)
        if reason:
            raise ResourceError(reason)

        # --- 4. 邻居数组落盘 + 存取回环 --------------------------------
        if args.skip_graph_save:
            roundtrip = {'skipped': True}
        else:
            npz_path = os.path.join(out, 'neighbors.npz')
            meta_path = os.path.join(out, 'neighbors.meta.json')
            log('saving neighbor arrays + roundtrip check ...')
            roundtrip = graph_roundtrip(graph, npz_path, meta_path)
            log('roundtrip: %r' % (roundtrip,))
            if not roundtrip['consistent']:
                raise RuntimeError('neighbor storage roundtrip failed: %r'
                                   % (roundtrip,))
            artifacts['neighbors_npz'] = 'neighbors.npz'
            artifacts['neighbors_meta'] = 'neighbors.meta.json'

        # --- 5. 留出段与逐用户打分 --------------------------------------
        log('collecting holdout relevants ...')
        relevants = build_relevants(session.iter_holdout())
        users = sorted(u for u in eval_users if relevants.get(u))
        excluded_no_positive = [u for u in eval_users
                                if not relevants.get(u)]
        log('evaluable users=%d excluded_no_positive=%d (not dropped silently)'
            % (len(users), len(excluded_no_positive)))
        if not users:
            raise RuntimeError('no evaluable users with a positive holdout item')

        log('scoring %d users with %s ...' % (len(users), ', '.join(METHODS)))
        score_started = time.time()
        item_set_size = len(item_ids)
        candidate_universe_hash = sha256_text(
            json.dumps(sorted(int(i) for i in item_ids), sort_keys=True))
        mostpop_order = most_popular_order(session.n_all, item_ids)
        likepop_order = like_popular_order(session.n_like, item_ids)
        log('baseline orders ready (MostPop/LikePop over %d items)'
            % len(mostpop_order))
        row = monitor.sample()
        reason = monitor.check(row)
        if reason:
            raise ResourceError(reason)

        per_method = dict((name, {'recs': []}) for name in METHODS)
        for index, user in enumerate(users):
            user_seen = seen.get(user, set())
            user_history = history.get(user, array.array('i'))
            seen_count = len(user_seen)
            history_count = int(history_len.get(user, 0))

            ranked = score_itemcf_user(user_history, graph, user_seen, args.top_n)
            itemcf_recs = []
            for item in ranked:
                item['n_all'] = session.n_all.get(item['movie_id'], 0)
                itemcf_recs.append(item['movie_id'])
            per_method['ItemCF']['recs'].append(itemcf_recs)

            # MostPop / LikePop 与 ItemCF 共用同一份候选全集（不复制候选集合）：
            # 同一份全量排序 + 同一份 seen 排除 + 同一个 top_n。
            most_recs = top_n_excluding(mostpop_order, user_seen, args.top_n)
            like_recs = top_n_excluding(likepop_order, user_seen, args.top_n)
            per_method['MostPop']['recs'].append(most_recs)
            per_method['LikePop']['recs'].append(like_recs)

            rel = relevants.get(user, set())
            per_user_rows.append({
                'user_id': user,
                'relevant_count': len(rel),
                'relevant_ids': '|'.join(str(i) for i in sorted(rel)),
                'candidate_count': item_set_size - seen_count,
                'history_len': history_count,
                'seen_count': seen_count,
                'ItemCF_returned': len(itemcf_recs),
                'MostPop_returned': len(most_recs),
                'LikePop_returned': len(like_recs),
            })
            for rank, entry in enumerate(ranked[:args.top_n], 1):
                rec_rows.append({
                    'user_id': user, 'model': 'ItemCF', 'rank': rank,
                    'movie_id': entry['movie_id'],
                    'raw_score': entry['score'],
                    'n_all': entry['n_all'],
                    'n_like': entry['n_like'],
                    'evidence_neighbor_count': entry['evidence_count'],
                    'top_source_movie_id': entry['top_source_movie_id'],
                    'top_source_c': entry['top_source_c'],
                    'contrib_sum': entry['contrib_sum'],
                })
            for rank, item in enumerate(most_recs, 1):
                rec_rows.append({'user_id': user, 'model': 'MostPop', 'rank': rank,
                                 'movie_id': item,
                                 'raw_score': session.n_all.get(item, 0),
                                 'n_all': session.n_all.get(item, 0),
                                 'n_like': session.n_like.get(item, 0),
                                 'evidence_neighbor_count': '',
                                 'top_source_movie_id': '',
                                 'top_source_c': '', 'contrib_sum': ''})
            for rank, item in enumerate(like_recs, 1):
                rec_rows.append({'user_id': user, 'model': 'LikePop', 'rank': rank,
                                 'movie_id': item,
                                 'raw_score': session.n_like.get(item, 0),
                                 'n_all': session.n_all.get(item, 0),
                                 'n_like': session.n_like.get(item, 0),
                                 'evidence_neighbor_count': '',
                                 'top_source_movie_id': '',
                                 'top_source_c': '', 'contrib_sum': ''})
            if (index + 1) % 500 == 0:
                log('scored %d/%d users' % (index + 1, len(users)))
                row = monitor.sample()
                reason = monitor.check(row)
                if reason:
                    raise ResourceError(reason)
        score_seconds = time.time() - score_started
        log('scoring done in %.1fs' % score_seconds)

        # --- 6. 指标 + 配对 bootstrap ----------------------------------
        relevants_list = [relevants[u] for u in users]
        per_user_metric = {}
        for name in METHODS:
            recs = per_method[name]['recs']
            recalls, ndcgs = per_user_metrics(recs, relevants_list, args.top_n)
            per_user_metric[name] = {'recall': recalls, 'ndcg': ndcgs}
            metrics[name] = {
                'ndcg@20': sum(ndcgs) / len(ndcgs),
                'recall@20': sum(recalls) / len(recalls),
                'users_scored': len(users),
                'full_list_rate': (sum(1 for r in recs if len(r) >= args.top_n)
                                   / float(len(recs))),
                'empty_list_rate': (sum(1 for r in recs if not r)
                                    / float(len(recs))),
                'returned_count': sum(len(r) for r in recs),
                'catalog_coverage': (len(set(i for r in recs for i in r))
                                     / float(item_set_size)),
            }
            log('%-8s ndcg@20=%.12f recall@20=%.12f full_list_rate=%.4f'
                % (name, metrics[name]['ndcg@20'], metrics[name]['recall@20'],
                   metrics[name]['full_list_rate']))

        for baseline in ('MostPop', 'LikePop'):
            bootstrap['ItemCF_vs_%s' % baseline] = {
                'ndcg@20': paired_bootstrap(
                    per_user_metric['ItemCF']['ndcg'],
                    per_user_metric[baseline]['ndcg']),
                'recall@20': paired_bootstrap(
                    per_user_metric['ItemCF']['recall'],
                    per_user_metric[baseline]['recall']),
            }
        log('bootstrap done (seed=%d repeats=%d)'
            % (BOOTSTRAP_SEED, BOOTSTRAP_REPEATS))

        # --- 7. 落盘明细 ------------------------------------------------
        for index, user in enumerate(users):
            row_data = per_user_rows[index]
            for name in METHODS:
                row_data['%s_ndcg@20' % name] = per_user_metric[name]['ndcg'][index]
                row_data['%s_recall@20' % name] = per_user_metric[name]['recall'][index]
        fields = list(per_user_rows[0].keys())
        write_csv_atomic(os.path.join(out, 'per_user.csv'), fields, per_user_rows)
        rec_fields = ['user_id', 'model', 'rank', 'movie_id', 'raw_score', 'n_all',
                      'n_like', 'evidence_neighbor_count', 'top_source_movie_id',
                      'top_source_c', 'contrib_sum']
        write_csv_atomic(os.path.join(out, 'recommendations.csv'), rec_fields,
                         rec_rows)
        artifacts['per_user_csv'] = 'per_user.csv'
        artifacts['recommendations_csv'] = 'recommendations.csv'

    except ResourceError as error:
        status = 'resource_failed'
        failure_reason = str(error)
        log('RESOURCE FAILURE: %s' % failure_reason)
    except BaseException as error:  # noqa: BLE001 - 必须落盘失败原因
        status = 'failed'
        failure_reason = '%s: %s' % (type(error).__name__, error)
        log('FAILURE: %s' % failure_reason)
        _write_failure_artifacts(out, args, status, failure_reason, monitor,
                                 session.counts.copy() if session else {},
                                 monitor.rows)
        monitor.dump()
        log('failure recorded in run_status.json; exiting nonzero')
        logger.close()
        raise
    finally:
        try:
            monitor.dump()
        except Exception:  # noqa: BLE001 - 资源采样失败不能掩盖主错误
            pass

    log('writing artifacts ...')
    logger.close()

    summary_rows = build_summary_table(metrics, session, args)
    write_csv_atomic(os.path.join(out, 'summary_table.csv'),
                     list(summary_rows[0].keys()) if summary_rows else
                     ['method'], summary_rows)
    artifacts['summary_table_csv'] = 'summary_table.csv'

    counts = dict(session.counts) if session else {}
    counts['candidate_items'] = len(item_ids)
    counts['evaluable_users'] = len(users)
    counts['excluded_no_positive'] = len(excluded_no_positive)
    counts['eval_list_users'] = len(eval_users)
    counts['cohort_users'] = len(cohort_users)
    counts['graph_edges'] = graph.n_edges if graph is not None else None
    counts['graph_sources'] = graph.n_sources() if graph is not None else None

    source_paths = [os.path.join(MODULE_DIR, name)
                    for name in ('confirm.py', 'prepare_confirmation.py', 'core.py',
                                 'evaluation.py', 'baselines.py')]
    source_hashes = {}
    for path in source_paths:
        if os.path.isfile(path):
            source_hashes[os.path.basename(path)] = file_sha256(path)

    run_status = {
        'status': status,
        'failure_reason': failure_reason,
        'run_id': os.path.basename(out),
        'mode': args.mode,
        'split': ('per-user temporal 80/20 (temporal_split test_frac=0.2)'
                  if args.mode == MODE_DEFAULT else
                  'legacy three-way 64/16/20 (temporal_split_three_way 0.2/0.2, '
                  'validation segment used as holdout)'),
        'is_default_protocol': args.mode == MODE_DEFAULT,
        'config': {'beta': args.beta, 'max_k': args.max_k, 'top_n': args.top_n,
                   'block_size': args.block_size, 'chunk_rows': args.chunk_rows,
                   'positive_threshold': POSITIVE_THRESHOLD},
        'methods': list(METHODS),
        'counts': counts,
        'metrics': metrics,
        'seconds': time.time() - started,
        'candidate_universe_hash': candidate_universe_hash,
        'users_sha256': {'cohort': sha256_text(''.join('%d\n' % u for u in cohort_users)),
                         'eval': sha256_text(''.join('%d\n' % u for u in eval_users))},
        'blas_env': blas_env,
        'artifacts': artifacts,
        'resource_peak': monitor.peak(),
        'resource_limit': {'rss_limit_bytes': RSS_LIMIT_BYTES,
                           'mem_available_floor_bytes': MEM_AVAILABLE_FLOOR_BYTES,
                           'budget_seconds': args.budget_seconds},
    }
    write_json_atomic(os.path.join(out, 'run_status.json'), run_status)
    write_json_atomic(os.path.join(out, 'source_hashes.json'),
                      {'sources': source_hashes})
    write_json_atomic(os.path.join(out, 'bootstrap.json'), bootstrap)

    manifest = {
        'run_id': os.path.basename(out),
        'stage': 'confirm',
        'status': status,
        'mode': args.mode,
        'is_default_protocol': args.mode == MODE_DEFAULT,
        'input': args.input,
        'input_sha256': file_sha256(args.input),
        'user_lists': {'dir': user_dir,
                       'cohort': os.path.basename(cohort_path),
                       'eval': os.path.basename(eval_path),
                       'cohort_users': len(cohort_users),
                       'eval_users': len(eval_users)},
        'parameters': {'positive_threshold': POSITIVE_THRESHOLD,
                       'beta': args.beta, 'max_k': args.max_k,
                       'top_n': args.top_n, 'block_size': args.block_size,
                       'chunk_rows': args.chunk_rows,
                       'test_frac': args.test_frac, 'val_frac': args.val_frac,
                       'bootstrap_seed': BOOTSTRAP_SEED,
                       'bootstrap_repeats': BOOTSTRAP_REPEATS},
        'protocol': {'split': run_status['split'],
                     'candidate_rule': 'I_train minus Seen_u (Seen_u includes '
                                       'low ratings); all holdout positives kept'
                                       ' in Recall and IDCG denominators',
                     'neighbor_order': '(-similarity, target_movieId) desc',
                     'recommendation_order': '(-score, movieId)',
                     'contribution_order': 'source movieId ascending',
                     'itemcf_empty_list_policy': 'no positive contribution -> '
                                                 'no recommendation; no popular '
                                                 'or zero-score padding'},
        'source_hashes': source_hashes,
        'resource_peak': monitor.peak(),
        'artifacts': artifacts,
        'graph': {'n_edges': graph.n_edges if graph is not None else None,
                  'n_sources': graph.n_sources() if graph is not None else None,
                  'roundtrip': roundtrip},
        'seconds': time.time() - started,
    }
    write_json_atomic(os.path.join(out, 'manifest.json'), manifest)

    comparison = None
    if args.mode == MODE_LEGACY:
        # 对拍由**独立的** `experiments/itemcf/equiv_check.py` 负责（它不复用
        # core.py，从原始评分重算相似度/邻居/打分，基准是 fix14 的
        # recommendations.csv 的 k100_beta0）。本模块只记录"这一轮走的是对拍
        # 切分"，不重复实现对拍逻辑，避免两套口径。
        comparison = {
            'delegated_to': 'experiments/itemcf/equiv_check.py',
            'reason': 'independent re-implementation avoids self-confirmation; '
                      'do not add a second comparison path here',
            'baseline': FIX14_DIR,
            'baseline_model': 'k100_beta0',
            'expected_users': FIX14_USERS,
            'expected_ndcg@20': FIX14_NDCG,
            'expected_recall@20': FIX14_RECALL,
            'this_run_users': len(users),
            'this_run_ndcg@20': (metrics.get('ItemCF') or {}).get('ndcg@20'),
            'this_run_recall@20': (metrics.get('ItemCF') or {}).get('recall@20'),
        }
    verification = {
        'status': status,
        'checks': [
            'per-user metrics recomputed from recommendations.csv row order',
            'no duplicate recommendations per user',
            'all holdout positives retained in Recall/IDCG denominators',
            'neighbor storage roundtrip ordering verified',
            'candidate universe hash recorded',
        ],
        'candidate_universe_hash': run_status['candidate_universe_hash'],
        'neighbor_roundtrip': roundtrip,
        'duplicates': duplicates_report(per_method),
        'metrics_recomputed': metrics,
        'legacy_comparison': comparison,
    }
    write_json_atomic(os.path.join(out, 'verification.json'), verification)
    write_json_atomic(os.path.join(out, 'conclusion.json'),
                      build_conclusion_payload(metrics, bootstrap, run_status))
    write_text(os.path.join(out, 'conclusion.md'),
               render_conclusion(metrics, bootstrap, run_status, monitor,
                                 comparison, users, excluded_no_positive))
    artifacts['conclusion_md'] = 'conclusion.md'
    artifacts['run_status_json'] = 'run_status.json'
    artifacts['manifest_json'] = 'manifest.json'
    artifacts['bootstrap_json'] = 'bootstrap.json'
    artifacts['verification_json'] = 'verification.json'
    artifacts['resources_csv'] = 'resources.csv'
    artifacts['run_log'] = 'run.log'
    return run_status


class ResourceError(Exception):
    """资源预算被突破（RSS / MemAvailable / 总预算），本轮终止。"""


def write_text(path, text):
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)
    return path


def _write_failure_artifacts(out, args, status, failure_reason, monitor, counts,
                             log_lines):
    """失败也要落盘 ``run_status.json``（计划书 §4 第 5 项"记录资源失败"）。

    只写状态与原因，不伪造指标——报告层必须能区分"没跑完"和"跑了但效果差"。
    """
    payload = {
        'status': status,
        'failure_reason': failure_reason,
        'mode': args.mode,
        'is_default_protocol': args.mode == MODE_DEFAULT,
        'metrics': {},
        'counts': counts,
        'resource_peak': monitor.peak(),
        'note': 'run terminated before metrics were produced; no metrics are '
                'reported for this run',
    }
    try:
        write_json_atomic(os.path.join(out, 'run_status.json'), payload)
    except Exception:  # noqa: BLE001 - 落盘失败不能掩盖原始错误
        pass
    return payload


def duplicates_report(per_method):
    """每个方法是否有重复推荐（按用户）。"""
    report = {}
    for name, payload in per_method.items():
        bad = 0
        for recs in payload.get('recs', []):
            if len(recs) != len(set(recs)):
                bad += 1
        report[name] = {'users_with_duplicates': bad,
                        'ok': bad == 0}
    return report


def build_summary_table(metrics, session, args):
    """主表：三行（ItemCF / MostPop / LikePop）。"""
    rows = []
    for name in METHODS:
        block = metrics.get(name) or {}
        rows.append(OrderedDict([
            ('method', name),
            ('ndcg@20', block.get('ndcg@20')),
            ('recall@20', block.get('recall@20')),
            ('users_scored', block.get('users_scored')),
            ('full_list_rate', block.get('full_list_rate')),
            ('catalog_coverage', block.get('catalog_coverage')),
            ('train_rows', session.counts['train_rows'] if session else None),
            ('holdout_rows', session.counts['holdout_rows'] if session else None),
        ]))
    return rows


def build_conclusion_payload(metrics, bootstrap, run_status):
    return {'metrics': metrics, 'bootstrap': bootstrap,
            'verdict': verdict_of(metrics, bootstrap),
            'mode': run_status.get('mode'),
            'is_default_protocol': run_status.get('is_default_protocol')}


#: 计划书 §5 预先固定的四类判定（不得按结果调整）。
VERDICT_SUPPORTED = 'supported_for_system_integration_assessment'
VERDICT_CROSSES_ZERO = 'insufficient_evidence_interval_crosses_zero'
VERDICT_METRICS_DISAGREE = 'insufficient_evidence_metrics_disagree'
VERDICT_NOT_REPRODUCED = 'baseline_advantage_not_reproduced'
VERDICT_FAILURE = 'resource_or_correctness_failure'


def verdict_of(metrics, bootstrap):
    """计划书 §5 的判定规则（预先固定，不按结果调整）。

    **四类必须显式区分**，尤其"方向不一致"不能与"未超过基线"混为一谈：

    1. :data:`VERDICT_SUPPORTED`：NDCG 与 Recall **都**超过两个基线，且对
       **较强基线**（按 NDCG@20 选）的 NDCG 差值区间下界 > 0。
    2. :data:`VERDICT_CROSSES_ZERO` / :data:`VERDICT_METRICS_DISAGREE`：
       **同属规则 2「证据不足或存在权衡」**——
       (a) 双双超过均值但区间跨零；(b) 两个指标方向不一致
       （NDCG 超基线但 Recall 没超、或反之，也包括只超过其中一个基线）。
       两种情况都不追加调参直到获胜。
    3. :data:`VERDICT_NOT_REPRODUCED`：**只**在 NDCG 与 Recall **都没有**
       超过较强基线的均值时才落这里（真的"未超过基线"）。
    4. :data:`VERDICT_FAILURE`：资源/正确性失败，不记为算法效果零分。
    """
    methods = ['MostPop', 'LikePop']
    blocks = dict((name, metrics.get(name) or {}) for name in methods)
    itemcf = metrics.get('ItemCF') or {}
    if not itemcf or any(not block for block in blocks.values()):
        return VERDICT_FAILURE

    # 较强基线：按 NDCG@20 选（计划书要求区间判定针对较强基线）。
    stronger = max(methods, key=lambda name: blocks[name].get('ndcg@20') or 0.0)
    weaker = [name for name in methods if name != stronger][0]

    def beats(name, key):
        mine = itemcf.get(key)
        theirs = blocks[name].get(key)
        if mine is None or theirs is None:
            return False
        return mine > theirs

    ndcg_beats = [name for name in methods if beats(name, 'ndcg@20')]
    recall_beats = [name for name in methods if beats(name, 'recall@20')]

    beats_both_metrics_all = (len(ndcg_beats) == len(methods)
                              and len(recall_beats) == len(methods))
    interval = ((bootstrap.get('ItemCF_vs_%s' % stronger) or {})
                .get('ndcg@20') or {})
    lower = (interval.get('ci95') or [None])[0]

    if beats_both_metrics_all and lower is not None and lower > 0.0:
        return VERDICT_SUPPORTED
    if beats_both_metrics_all:
        # 规则 2(a)：双双超过均值，但对较强基线的 NDCG 区间跨零。
        return VERDICT_CROSSES_ZERO

    # 规则 3 只留给"确实没有超过基线"：两个指标**都没有**超过较强基线。
    beats_stronger_ndcg = stronger in ndcg_beats
    beats_stronger_recall = stronger in recall_beats
    if not beats_stronger_ndcg and not beats_stronger_recall:
        # 还要确认对较弱基线也没有双指标优势，否则那是规则 2(b) 的权衡。
        if not (weaker in ndcg_beats and weaker in recall_beats):
            return VERDICT_NOT_REPRODUCED
    # 其余一律规则 2(b)：方向不一致 / 只赢一个基线 / 只赢一个指标。
    return VERDICT_METRICS_DISAGREE


def render_conclusion(metrics, bootstrap, run_status, monitor, comparison,
                      users, excluded_no_positive):
    lines = []
    lines.append('# ItemCF 确认实验结论\n')
    lines.append('- run_id: `%s`' % run_status.get('run_id', ''))
    lines.append('- 模式: `%s`（默认协议: %s）'
                 % (run_status.get('mode'), run_status.get('is_default_protocol')))
    lines.append('- 切分: %s' % run_status.get('split'))
    lines.append('- 评测用户: %d（无正例被排除: %d，不补抽）'
                 % (len(users), len(excluded_no_positive)))
    lines.append('- 状态: `%s`%s' % (run_status.get('status'),
                                    ('（%s）' % run_status['failure_reason'])
                                    if run_status.get('failure_reason') else ''))
    lines.append('')
    lines.append('## 主表（三行）\n')
    lines.append('| method | NDCG@20 | Recall@20 | 有效用户数 | 满额率 | 目录覆盖率 |')
    lines.append('|---|---|---|---|---|---|')
    for name in METHODS:
        block = metrics.get(name) or {}
        lines.append('| %s | %s | %s | %s | %s | %s |' % (
            name,
            _fmt(block.get('ndcg@20'), 6), _fmt(block.get('recall@20'), 6),
            block.get('users_scored'),
            _fmt(block.get('full_list_rate'), 4),
            _fmt(block.get('catalog_coverage'), 4)))
    lines.append('')
    lines.append('## 配对 bootstrap（seed=%d, repeats=%d, 95%% percentile）\n'
                 % (BOOTSTRAP_SEED, BOOTSTRAP_REPEATS))
    lines.append('| 比较 | 指标 | 均值差 | 相对差 | 95% CI | 排除 0 |')
    lines.append('|---|---|---|---|---|---|')
    for key in sorted(bootstrap):
        for metric in ('ndcg@20', 'recall@20'):
            block = (bootstrap[key] or {}).get(metric) or {}
            lines.append('| %s | %s | %s | %s | [%s, %s] | %s |' % (
                key, metric, _fmt(block.get('mean_difference'), 6),
                _fmt(block.get('relative_difference'), 4),
                _fmt((block.get('ci95') or [None, None])[0], 6),
                _fmt((block.get('ci95') or [None, None])[1], 6),
                block.get('excludes_zero')))
    lines.append('')
    lines.append('## 资源\n')
    peak = monitor.peak()
    lines.append('- 采样点数: %s；峰值进程树 RSS: %s MB；最低 MemAvailable: %s MB'
                 % (peak.get('samples'), peak.get('peak_rss_mb'),
                    peak.get('min_mem_available_mb')))
    lines.append('- 预算: %s 秒；RSS 上限 %s GB；MemAvailable 下限 %s MB（连续 %s 秒）'
                 % (run_status.get('resource_limit', {}).get('budget_seconds'),
                    RSS_LIMIT_BYTES / float(1 << 30),
                    MEM_AVAILABLE_FLOOR_BYTES >> 20,
                    MEM_AVAILABLE_STREAK_SECONDS))
    lines.append('')
    if comparison is not None:
        lines.append('## 对拍（显式 legacy-3way 模式）\n')
        lines.append('- 对拍实体: `%s`（本模块只标记模式，不重复实现对拍）'
                     % comparison['delegated_to'])
        lines.append('- 基准: `%s` 的 `%s`（1930 用户，NDCG@20=%s，Recall@20=%s）'
                     % (comparison['baseline'], comparison['baseline_model'],
                        _fmt(comparison['expected_ndcg@20'], 12),
                        _fmt(comparison['expected_recall@20'], 12)))
        lines.append('- 本轮（对拍模式）用户数 %s；NDCG@20=%s；Recall@20=%s'
                     % (comparison['this_run_users'],
                        _fmt(comparison['this_run_ndcg@20'], 12),
                        _fmt(comparison['this_run_recall@20'], 12)))
        lines.append('')
    lines.append('## 判定\n')
    lines.append('- %s' % verdict_of(metrics, bootstrap))
    lines.append('')
    lines.append('## 限制\n')
    lines.append('- 每用户时序留出，不是共同全局时间截点回测；同秒评分可跨边界。')
    lines.append('- 这是"排除了已识别历史实验用户的新用户群体确认"；'
                 '完整 MovieLens 曾用于生产 ALS 训练，不声称严格盲测。')
    lines.append('- 本轮改变了人群、规模与训练/留出比例，'
                 '不能把新旧指标差直接归因于训练数据增多。')
    lines.append('- ItemCF 是排序分数，不是评分预测，不计算 RMSE。')
    return '\n'.join(lines) + '\n'


def _fmt(value, digits):
    if value is None:
        return 'N/A'
    try:
        return ('%.' + str(digits) + 'f') % float(value)
    except (TypeError, ValueError):
        return str(value)


def main(argv=None):
    args = parse_args(argv)
    status = run(args)
    print(json.dumps({'status': status['status'],
                      'mode': status['mode'],
                      'metrics': dict((k, {'ndcg@20': v.get('ndcg@20'),
                                           'recall@20': v.get('recall@20')})
                                      for k, v in status['metrics'].items())},
                     sort_keys=True, default=_json_default))
    return 0 if status['status'] == 'success' else 3


if __name__ == '__main__':
    sys.exit(main())
