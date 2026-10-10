# -*- coding: utf-8 -*-
"""ItemCF 全量训练的**契约测试**：``experiments/itemcf/full_train.py`` + ``model_io.py``。

对应计划书 ``docs/superpowers/plans/2026-10-04-itemcf-full-training.md``：

* §4 模型固定规则与产物契约（数组/dtype/排序/零邻居/状态机）；
* §5.B 低内存精确构图（手算共现、260 人计数防溢出、同用户跨输入块、重复评分拒绝、
  零好评来源、相似度平票、存取回环、block_size 16/32/64/128 等价性）；
* §5.B 末条 **等价性对拍**：旧 2k 固定训练段（196,614 行，64/16/20 切分）对照
  ``fix14`` 的 k=100/beta=0，1930 用户推荐 ID/次序完全一致、分数差 ≤1e-9、
  NDCG/Recall 差 ≤1e-10。

设计原则
========

1. **测真代码，不测桩**：构图一律调 ``full_train.build_block_edges`` /
   ``build_graph_arrays`` / ``DiskCSRBuilder`` / ``StreamingIndex`` 的**真实实现**，
   不复制一份数学再自证。``reference_neighbors_python`` 是模块**自带的独立慢路径**，
   用它做逐边对拍。
2. **对拍用两套互不依赖的实现**：对拍既比对冻结的 ``fix14`` 旧产物，也用
   ``verify_full_model.recompute_source_neighbors``（"一列乘全矩阵"的独立写法）
   复核抽样来源——不是"同一份代码跑两次"。
3. **不能本地跑的用例明确 skip 并写清原因**，绝不静默通过或假装成功。
   依赖 VM 路径 / 原始 32M CSV 的用例走 ``skipTest`` 并注明缺什么。

三条已修复缺陷的回归测试（Lead 已按报告修掉，本文件锁死）
=====================================================

实现期间实测到三处缺陷，均已报告 Lead 并由 Lead 修复；下面是**回归测试**，
防止它们被改回去（每条用例都注明了"修复前会怎么坏"）：

* **缺陷 1** ``model_io.NeighborModel.save()`` 曾 ``return write_json_atomic(...)``，
  即返回**路径字符串**，而 docstring 写的是"返回元数据 dict"。调用方
  ``run_worker`` 随后 ``M.verify_hashes(model_dir, saved)`` 会在
  ``meta.get('files')`` 上抛 ``AttributeError: 'str' object has no attribute 'get'``
  → 正式全量永远到不了 ``validated_unpublished``。
  回归用例：:class:`ModelSaveContractTests`（``save()`` 必须返回**可变 dict**
  且与磁盘 ``model.json`` 内容一致；返回值可直接喂给 ``verify_hashes``）。
* **缺陷 2** ``finalize`` 曾用 ``sparse.__version__``，而 ``scipy.sparse`` 不暴露该
  属性（scipy 1.13.1 实测 ``hasattr(scipy.sparse, '__version__') is False``）→ 抛异常。
  ``finalize`` 是所有路径（success / correctness_failed / resource_blocked）的唯一
  出口，它一崩，``run_status.json`` / ``manifest.json`` 一个都不落盘。
  回归用例：``FinalizeContractTests.test_finalize_writes_terminal_artifacts``
  （走**真实** ``finalize``，断言四个终态文件都落盘、``scipy`` 字段等于顶层版本）。
* **缺陷 3** ``DuplicateChecker._segment_iterator`` 曾用
  ``np.fromfile(count=1<<19)`` 读 key 块；``np.fromfile`` 是"读到 EOF 为止"，不会停在
  key 块末尾，所以任一段 key 数 < 524288 时会把 line 块读进 key、随后 ``IndexError``。
  仅当 ``len(segments) > 1``（走 ``finalize`` 归并）时触发；32M 全量因尾段
  2,640,076 ≥ 524288 侥幸躲过，属数据巧合。
  回归用例：``DuplicateAndValidationTests.test_cross_segment_duplicates_are_detected``
  （``segment_rows=2`` 的小段多段输入必须正确检出重复，且不崩）。

运行::

    backend/venv/Scripts/python.exe -m unittest tests.test_itemcf_full_training -v
"""
import csv
import gc
import hashlib
import json
import os
import sys
import threading
import time
import unittest

import numpy as np
from scipy import sparse

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from experiments.itemcf import full_train as F  # noqa: E402
from experiments.itemcf import model_io as M  # noqa: E402
from experiments.itemcf import verify_full_model as V  # noqa: E402
from experiments.itemcf._full_train_testkit import graph_to_dict  # noqa: E402

FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_full_tmp')

#: 旧 2k 切片（与 fix14 基准同一份输入，MD5 已核验）。
PILOT_CSV = os.path.join(PROJECT, '.vmwork', 'pilot_ratings.csv')
PILOT_MD5 = '736601f707fc6b048bae46caaf154ee9'

#: 冻结基准目录（k100_beta0）。
FIX14_DIR = os.path.join(PROJECT, 'experiments', 'itemcf', 'out', '20261004T_fix14')

#: 对拍口径（计划书 §5.B 末条）。
EQUIV_TRAIN_ROWS = 196614
EQUIV_ITEMS = 11380
EQUIV_GRAPH_EDGES = 716232
EQUIV_EVAL_USERS = 1930
EQUIV_SCORE_TOLERANCE = 1e-9
EQUIV_METRIC_TOLERANCE = 1e-10

_SEQ_LOCK = threading.Lock()
_SEQ = [0]


def fixture_dir(prefix):
    """建唯一临时目录（沿用既有 itemcf 测试的重试策略，沙箱下不清理）。"""
    for _attempt in range(1000):
        with _SEQ_LOCK:
            _SEQ[0] += 1
            seq = _SEQ[0]
        path = os.path.join(
            FIXTURE_ROOT,
            '%s-%d-%d-%d' % (prefix, int(time.time() * 1000), os.getpid(), seq))
        try:
            os.makedirs(path)
            return path
        except OSError:
            continue
    raise RuntimeError('could not create a unique fixture dir under %s'
                       % FIXTURE_ROOT)


def write_ratings(path, rows, header=('userId', 'movieId', 'rating', 'timestamp')):
    """写一份评分 CSV（评测入口一律走真实 CSV，不绕过校验）。"""
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(row)
    return path


def rows_to_csr(rows, positive_threshold=4.0):
    """内存构造好评 0/1 矩阵（**只用于构造输入**，构图仍调真实现）。

    :returns: ``(X, item_ids, user_ids, n_like, n_all)``
    """
    user_ids = sorted(set(row[0] for row in rows))
    item_ids = sorted(set(row[1] for row in rows))
    user_index = dict((u, p) for p, u in enumerate(user_ids))
    item_index = dict((i, p) for p, i in enumerate(item_ids))
    n_all = np.zeros(len(item_ids), dtype=np.int64)
    n_like = np.zeros(len(item_ids), dtype=np.int64)
    r_list, c_list = [], []
    for user, item, rating, _ts in rows:
        pos = item_index[item]
        n_all[pos] += 1
        if float(rating) >= positive_threshold:
            n_like[pos] += 1
            r_list.append(user_index[user])
            c_list.append(pos)
    if not r_list:
        empty = sparse.csr_matrix((len(user_ids), len(item_ids)), dtype=np.int64)
        return empty, item_ids, user_ids, n_like, n_all
    data = np.ones(len(r_list), dtype=np.int64)
    X = sparse.csr_matrix((data, (np.asarray(r_list, dtype=np.int64),
                                  np.asarray(c_list, dtype=np.int64))),
                          shape=(len(user_ids), len(item_ids)), dtype=np.int64)
    return X, item_ids, user_ids, n_like, n_all


def scoring_graph(arrays, item_ids):
    """把边数组转成 ``equiv_check.score_users`` 需要的 ``{source: [(target, sim)]}``。

    ``graph_to_dict`` 给的是三元组 ``(target, sim, c)``（用于逐边对拍共现），
    而 ``score_users`` 只要 ``(target, sim)``——这里显式转换，避免在断言里
    混用两种形状。
    """
    full = graph_to_dict(arrays, item_ids)
    return dict((source, [(target, sim) for target, sim, _c in neighbors])
                for source, neighbors in full.items())


def hand_cooccurrence(rows, positive_threshold=4.0):
    """**手算**共现与相似度：``{source_movieId: {target_movieId: (c, sim)}}``。

    刻意写成最直白的双重循环（不做稀疏运算、不做 numpy 广播），作为
    ``build_block_edges`` 的"手算裁判"。对角线不出现；零共现不出现。
    """
    user_likes = {}
    for user, item, rating, _ts in rows:
        if float(rating) >= positive_threshold:
            user_likes.setdefault(user, set()).add(item)
    n_like = {}
    for likes in user_likes.values():
        for item in likes:
            n_like[item] = n_like.get(item, 0) + 1
    cooc = {}
    for likes in user_likes.values():
        ordered = sorted(likes)
        for a in ordered:
            for b in ordered:
                if a == b:
                    continue
                cooc[(a, b)] = cooc.get((a, b), 0) + 1
    table = {}
    for (a, b), c in cooc.items():
        if c <= 0:
            continue
        na, nb = n_like.get(a, 0), n_like.get(b, 0)
        if na <= 0 or nb <= 0:
            continue
        table.setdefault(a, {})[b] = (c, float(c) / float(np.sqrt(na * nb)))
    return table, n_like


# ---------------------------------------------------------------------------
# 1. 手算共现核对
# ---------------------------------------------------------------------------

class HandComputedCooccurrenceTests(unittest.TestCase):
    """小矩阵逐边核对 ``c`` 与 ``sim``——用最直白的双重循环当裁判。

    小到可以口算：3 用户 x 4 物品，共现只能是 1 或 2。
    """

    def test_every_edge_matches_hand_computation(self):
        rows = [
            # u1: 喜欢 10, 20, 40        （高分）
            (1, 10, 5.0, 100), (1, 20, 4.0, 101), (1, 40, 4.5, 102),
            # u2: 喜欢 10, 30, 40
            (2, 10, 4.0, 200), (2, 30, 5.0, 201), (2, 40, 4.0, 202),
            # u3: 喜欢 20, 30, 40
            (3, 20, 4.5, 300), (3, 30, 4.0, 301), (3, 40, 5.0, 302),
        ]
        X, item_ids, _users, n_like, _n_all = rows_to_csr(rows)
        table, n_like_hand = hand_cooccurrence(rows)

        # 手算与矩阵的 n_like 必须一致（否则下面比的是两个错的东西）
        for pos, mid in enumerate(item_ids):
            self.assertEqual(int(n_like[pos]), n_like_hand[mid],
                             'n_like mismatch for movieId %d' % mid)

        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=2)
        graph = graph_to_dict(arrays, item_ids)

        # 逐来源、逐邻居核对
        expected_sources = sorted(table)
        self.assertEqual(sorted(graph), expected_sources)
        for source in expected_sources:
            expected = table[source]
            got = dict((target, (c, sim)) for target, sim, c in graph[source])
            self.assertEqual(sorted(got), sorted(expected),
                             'neighbor set differs for source %d' % source)
            for target, (c_hand, sim_hand) in expected.items():
                c_got, sim_got = got[target]
                self.assertEqual(c_got, c_hand,
                                 'cooccurrence c(%d,%d) = %d, hand = %d'
                                 % (source, target, c_got, c_hand))
                self.assertAlmostEqual(sim_got, sim_hand, places=12,
                                       msg='sim(%d,%d)' % (source, target))

        # 具体数字钉死（防"两个实现一起错"）
        self.assertEqual([t for t, _s, _c in graph[10]], [40, 20, 30],
                         'source 10 order should be by (-sim, movieId)')
        first_target, first_sim, first_c = graph[10][0]
        self.assertEqual(first_target, 40)
        self.assertEqual(first_c, 2)
        self.assertAlmostEqual(first_sim, 2.0 / np.sqrt(2.0 * 3.0), places=12)

    def test_similarity_formula_is_c_over_sqrt_ninj(self):
        """``s = c / sqrt(n_like(i) * n_like(j))``——直接按定义式复核全图。"""
        rows = []
        # 造出不同的 n_like，避免"全相等"掩盖公式错误
        for user in range(1, 9):
            for item in (100, 200):
                rows.append((user, item, 5.0, 1000 + user * 10 + item % 10))
        for user in range(1, 5):
            rows.append((user, 300, 4.0, 2000 + user))
        X, item_ids, _users, n_like, _n_all = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        graph = graph_to_dict(arrays, item_ids)
        position = dict((mid, p) for p, mid in enumerate(item_ids))
        checked = 0
        for source, neighbors in graph.items():
            for target, sim, c in neighbors:
                denom = np.sqrt(float(n_like[position[source]])
                                * float(n_like[position[target]]))
                self.assertAlmostEqual(sim, c / denom, places=12)
                checked += 1
        self.assertGreater(checked, 0)

    def test_zero_like_items_are_candidates_but_never_sources(self):
        """零好评物品：在 ``item_ids`` 里（是候选），但作来源无邻居且区间为空。

        计划书 §4："电影全集取全部合法评分中出现过的电影，**包含没有好评的电影**"。
        """
        rows = [
            (1, 10, 5.0, 100), (1, 20, 5.0, 101),
            (2, 10, 4.0, 200), (2, 20, 4.0, 201),
            # 30 / 40 只有低分 → n_all > 0 而 n_like == 0
            (1, 30, 2.0, 102), (2, 30, 3.0, 202),
            (3, 40, 1.0, 300),
        ]
        X, item_ids, _users, n_like, n_all = rows_to_csr(rows)
        self.assertIn(30, item_ids)
        self.assertIn(40, item_ids)
        zero_like = [mid for p, mid in enumerate(item_ids) if n_like[p] == 0]
        self.assertEqual(zero_like, [30, 40])

        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        graph = graph_to_dict(arrays, item_ids)
        for mid in zero_like:
            self.assertNotIn(mid, graph, 'zero-like item %d must have no neighbors'
                             % mid)
        # 空 indptr 区间：长度 n_items+1、单调、且零好评来源处 diff == 0
        indptr = arrays['indptr']
        self.assertEqual(len(indptr), len(item_ids) + 1)
        self.assertEqual(int(indptr[0]), 0)
        self.assertEqual(int(indptr[-1]), len(arrays['sources']))
        self.assertTrue(np.all(np.diff(indptr) >= 0))
        for mid in zero_like:
            pos = item_ids.index(mid)
            self.assertEqual(int(indptr[pos + 1]) - int(indptr[pos]), 0)
            self.assertGreater(int(n_all[pos]), 0,
                               'n_all must stay positive for %d' % mid)
        # 也不得作为任何来源的目标
        for source, neighbors in graph.items():
            for target, _sim, _c in neighbors:
                self.assertNotIn(target, zero_like)

    def test_positive_similarity_only_and_no_self_loops(self):
        ns = np.random.RandomState(20261004)
        rows = []
        for user in range(1, 61):
            for item in range(1, 41):
                if ns.rand() < 0.35:
                    rows.append((user, item, 4.0 + 0.5 * (user % 3),
                                 1000 + user * 100 + item))
        X, item_ids, _u, _nl, _na = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=8)
        sims = arrays['similarities']
        self.assertEqual(sims.dtype, np.float64)
        self.assertTrue(np.all(sims > 0.0), 'only positive similarities may be saved')
        self.assertTrue(np.all(sims <= 1.0 + 1e-12))
        self.assertTrue(np.all(np.isfinite(sims)))
        graph = graph_to_dict(arrays, item_ids)
        for source, neighbors in graph.items():
            for target, _sim, _c in neighbors:
                self.assertNotEqual(source, target, 'self loop at %d' % source)
        # 目标不得在来源的区间内重复
        indptr = arrays['indptr']
        for pos in range(len(item_ids)):
            start, stop = int(indptr[pos]), int(indptr[pos + 1])
            targets = arrays['targets'][start:stop]
            self.assertEqual(len(set(targets.tolist())), len(targets))


# ---------------------------------------------------------------------------
# 2. 260 人计数防溢出
# ---------------------------------------------------------------------------

class CooccurrenceCountingTests(unittest.TestCase):
    """计划书 §4："计数 int64，**禁止 bool/uint8** 共现计数"。

    构造 260 人共同好评同一条边——**260 越过 uint8 的 255**，正是上轮
    ``bool``/``uint8`` 会静默截断的那个点。用真实两遍扫描 + 磁盘 CSR 全链路。
    """

    N_USERS = 260

    def _pipeline(self, rows, block_size=1):
        """走真实的两遍流式扫描 + 磁盘 CSR（不绕过 IO 路径）。"""
        work = fixture_dir('counting')
        path = write_ratings(os.path.join(work, 'ratings.csv'), rows)
        index = F.stream_input_pass1(path)
        args = F.parse_args(['--input', path, '--out', work, '--worker',
                             '--max-k', '100', '--beta', '0'])
        csr_paths, _seconds = F.run_prepare(path, index, work, args)
        X = F.open_csr(csr_paths, index)
        n_like = np.asarray(index.n_like, dtype=np.int64).copy()
        n_all = np.asarray(index.n_all, dtype=np.int64).copy()
        item_ids = np.asarray(index.item_ids, dtype=np.int64)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=block_size)
        return index, X, arrays, item_ids, n_like, n_all

    def test_260_users_cooccurrence_is_exact_int64(self):
        rows = []
        for user in range(1, self.N_USERS + 1):
            rows.append((user, 1, 5.0, 1000 + user))
            rows.append((user, 2, 5.0, 2000 + user))
        _index, _X, arrays, item_ids, n_like, n_all = self._pipeline(rows)
        self.assertEqual(list(item_ids), [1, 2])
        self.assertEqual(list(n_like), [260, 260])
        self.assertEqual(list(n_all), [260, 260])
        self.assertEqual(arrays['cooccurrences'].dtype, np.int64,
                         'cooccurrence counts must be int64')
        self.assertEqual(arrays['sources'].dtype, np.int64)
        self.assertEqual(arrays['targets'].dtype, np.int64)
        self.assertEqual(arrays['similarities'].dtype, np.float64)
        graph = graph_to_dict(arrays, item_ids)
        self.assertEqual(graph[1], [(2, 1.0, 260)])
        self.assertEqual(graph[2], [(1, 1.0, 260)])
        self.assertEqual(int(arrays['cooccurrences'][0]), 260)
        # 260 > 255：uint8 会截断成 4，bool 只会是 1
        self.assertGreater(int(arrays['cooccurrences'].max()), 255)

    def test_255_and_256_boundary_are_not_truncated(self):
        """255 / 256 这两个边界值必须原样保留（uint8 恰好在 256 归零）。"""
        for count in (255, 256, 257):
            rows = []
            for user in range(1, count + 1):
                rows.append((user, 7, 5.0, 1000 + user))
                rows.append((user, 8, 4.0, 2000 + user))
            _index, _X, arrays, item_ids, _nl, _na = self._pipeline(rows)
            graph = graph_to_dict(arrays, item_ids)
            self.assertEqual(graph[7], [(8, 1.0, count)],
                             'c == %d must be preserved exactly' % count)

    def test_cooccurrence_never_exceeds_either_n_like(self):
        """不变式：``c(i,j) <= min(n_like(i), n_like(j))``（计划书 §5.D）。"""
        ns = np.random.RandomState(7)
        rows = []
        for user in range(1, 300 + 1):
            for item in range(1, 25):
                if ns.rand() < 0.3:
                    rows.append((user, item, 5.0, 1000 + user * 100 + item))
        _index, _X, arrays, item_ids, n_like, _na = self._pipeline(rows,
                                                                   block_size=7)
        position = dict((mid, p) for p, mid in enumerate(item_ids))
        indptr = arrays['indptr']
        self.assertGreater(len(arrays['sources']), 0)
        for pos in range(len(item_ids)):
            start, stop = int(indptr[pos]), int(indptr[pos + 1])
            source_pos = pos
            for k in range(start, stop):
                target_pos = int(arrays['targets'][k])
                c = int(arrays['cooccurrences'][k])
                self.assertGreater(c, 0)
                self.assertLessEqual(c, int(n_like[source_pos]))
                self.assertLessEqual(c, int(n_like[target_pos]))
        self.assertEqual(len(position), len(item_ids))


# ---------------------------------------------------------------------------
# 3. 同用户跨输入块
# ---------------------------------------------------------------------------

class CrossChunkUserTests(unittest.TestCase):
    """计划书 §5.B 第 1 条：用户的好评分布在**不同输入块**时共现不能丢。

    做法：把 ``full_train.DEFAULT_SCAN_ROWS`` 压到 2 行，强制多块交错，
    然后走**真实**的 ``stream_input_pass1`` + ``DiskCSRBuilder.run_pass2``
    （后者靠 ``index.line_users`` 逐行还原用户归属，不假设"用户成组"）。
    """

    def setUp(self):
        self._original_scan_rows = F.DEFAULT_SCAN_ROWS

    def tearDown(self):
        F.DEFAULT_SCAN_ROWS = self._original_scan_rows

    def test_interleaved_users_across_chunks_keep_cooccurrence(self):
        # 6 行 = 3 块（每块 2 行）；用户 1/2 的好评跨越全部 3 块。
        rows = [(1, 1, 5.0, 11), (2, 1, 5.0, 12),
                (1, 2, 5.0, 13), (2, 2, 5.0, 14),
                (1, 3, 5.0, 15), (2, 3, 5.0, 16)]
        F.DEFAULT_SCAN_ROWS = 2
        work = fixture_dir('crosschunk')
        path = write_ratings(os.path.join(work, 'ratings.csv'), rows)

        index = F.stream_input_pass1(path)
        # 两个用户必须被归并成同一份下标（不是每块一个新用户）
        self.assertEqual(index.n_users, 2)
        self.assertEqual(index.n_items, 3)
        self.assertEqual(index.positive_pairs(), 6)
        self.assertEqual(list(index.item_ids), [1, 2, 3])
        self.assertEqual([int(v) for v in index.n_like], [2, 2, 2])

        args = F.parse_args(['--input', path, '--out', work, '--worker',
                             '--max-k', '100', '--beta', '0', '--block-size', '1'])
        csr_paths, _seconds = F.run_prepare(path, index, work, args)
        X = F.open_csr(csr_paths, index)
        self.assertEqual(X.nnz, 6)
        # 矩阵必须真是"两个用户各喜欢 3 部"，不是被拆成 6 个用户各 1 部
        dense = np.asarray(X.todense())
        self.assertEqual(dense.shape, (2, 3))
        self.assertTrue(np.all(dense == 1))

        item_ids = np.asarray(index.item_ids, dtype=np.int64)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        graph = graph_to_dict(arrays, item_ids)
        # 跨块共现必须完整：每对电影都被 2 个用户共同好评
        self.assertEqual(sorted(graph), [1, 2, 3])
        for source in (1, 2, 3):
            neighbors = graph[source]
            self.assertEqual(len(neighbors), 2)
            for _target, sim, c in neighbors:
                self.assertEqual(c, 2, 'cross-chunk cooccurrence lost for %d'
                                 % source)
                self.assertAlmostEqual(sim, 1.0, places=12)

    def test_reference_and_block_agree_when_users_span_chunks(self):
        """交错输入的向量化结果 == 逐条参考实现（跨块场景也逐步对拍）。"""
        rows = []
        for item in range(1, 9):
            for user in (1, 2, 3, 4):
                if (item + user) % 3 != 0:
                    rows.append((user, item, 4.0 + 0.5 * (item % 3),
                                 100 + item * 10 + user))
        F.DEFAULT_SCAN_ROWS = 3
        work = fixture_dir('crosschunk-ref')
        path = write_ratings(os.path.join(work, 'ratings.csv'), rows)
        index = F.stream_input_pass1(path)
        args = F.parse_args(['--input', path, '--out', work, '--worker',
                             '--max-k', '100', '--beta', '0', '--block-size', '2'])
        csr_paths, _seconds = F.run_prepare(path, index, work, args)
        X = F.open_csr(csr_paths, index)
        item_ids = [int(v) for v in index.item_ids]
        arrays = F.build_graph_arrays(X, np.asarray(item_ids, dtype=np.int64),
                                      beta=0.0, max_k=100, block_size=2)
        reference = F.reference_neighbors_python(X, item_ids, 0.0, 100,
                                                 block_size=2)
        graph = graph_to_dict(arrays, item_ids)
        self.assertEqual(set(graph), set(reference))
        for source in reference:
            self.assertEqual([t for t, _s, _c in graph[source]],
                             [t for t, _s, _c in reference[source]])
            for (gt, gs, gc), (rt, rs, rc) in zip(graph[source],
                                                  reference[source]):
                self.assertEqual(gt, rt)
                self.assertEqual(gc, rc)
                self.assertAlmostEqual(gs, rs, places=12)


# ---------------------------------------------------------------------------
# 4. 重复评分拒绝 + 输入校验
# ---------------------------------------------------------------------------

class DuplicateAndValidationTests(unittest.TestCase):
    """计划书 §3：重复/非法行**不得悄悄丢弃或累加**，应报告位置并停止。"""

    def test_duplicate_pair_is_rejected_with_position(self):
        work = fixture_dir('dup')
        path = write_ratings(os.path.join(work, 'ratings.csv'), [
            (1, 10, 5.0, 100),
            (2, 10, 4.0, 200),
            (1, 10, 2.0, 300),          # 第 4 行：重复的 (1, 10)
        ])
        with self.assertRaises(F.InputValidationError) as caught:
            F.stream_input_pass1(path)
        error = caught.exception
        self.assertIsNotNone(error.line_number,
                             'the error must carry the input position')
        self.assertIn('duplicate', str(error).lower())
        self.assertIn('(1, 10)', str(error).replace(' ', ' '))
        # 位置必须是重复对里**先出现**的那一行（表头算第 1 行 → 数据第 1 行 = 2）
        self.assertEqual(error.line_number, 2)

    def test_duplicate_is_not_silently_accumulated_or_dropped(self):
        """重复行的"两种错法"都要排除：不能累加、也不能丢弃后照常跑完。"""
        work = fixture_dir('dup2')
        path = write_ratings(os.path.join(work, 'ratings.csv'), [
            (1, 10, 5.0, 100),
            (1, 10, 5.0, 101),
        ])
        with self.assertRaises(F.InputValidationError):
            F.read_input_relevant_rows(path)
        # 也必须不能在磁盘 CSR 路径上放行
        with self.assertRaises(F.InputValidationError):
            F.stream_input_pass1(path)

    def test_three_way_duplicate_reports_all_occurrences(self):
        work = fixture_dir('dup3')
        path = write_ratings(os.path.join(work, 'ratings.csv'), [
            (1, 10, 5.0, 100),
            (1, 10, 5.0, 101),
            (1, 10, 5.0, 102),
        ])
        checker = F.DuplicateChecker(work_dir=os.path.join(work, 'seg'),
                                     segment_rows=2)
        for offset, row in enumerate([(1, 10, 5.0, 100), (1, 10, 5.0, 101),
                                      (1, 10, 5.0, 102)]):
            checker.add(row[0], row[1], offset + 2)
        checker.end_segment()
        report = checker.report()
        self.assertGreater(report['duplicate_occurrences'], 0)
        self.assertFalse(report['packing']['keys_unique'])
        self.assertEqual(report['rows_checked'], 3)
        self.assertEqual(report['packing']['max_user_id'], 1)
        self.assertEqual(report['packing']['max_movie_id'], 10)

    def test_intra_segment_duplicates_are_detected(self):
        """段内重复必须被检出（``DuplicateChecker`` 的段内查重）。"""
        work = fixture_dir('dupseg')
        checker = F.DuplicateChecker(work_dir=os.path.join(work, 'seg'),
                                     segment_rows=64)
        for line in range(2, 10):
            checker.add(1, 10, line)          # 8 行全是同一个 pair
        checker.end_segment()
        report = checker.report()
        self.assertGreater(report['duplicate_occurrences'], 0)
        first = report['duplicate_rows'][0]
        self.assertEqual((first['userId'], first['movieId']), (1, 10))
        self.assertEqual(len(first['lines']), 2)
        self.assertTrue(all(isinstance(v, int) for v in first['lines']))

    def test_cross_segment_duplicates_are_detected(self):
        """**跨段**重复必须被检出——回归缺陷 3。

        修复前：段文件布局是 ``[全体 key][全体 line]``，而读取用
        ``np.fromfile(count=1<<19)``（读到 EOF 才停），于是 key 数 < 524288 的段
        会把 line 块读进 key、随后 ``IndexError: index 0 is out of bounds``。
        只在 ``len(segments) > 1``（``finalize`` 的 k 路归并）时触发；
        32M 全量因尾段 2,640,076 ≥ 524288 侥幸躲过，是数据巧合不是正确性。

        本用例用 ``segment_rows=2`` 造出**小段**，并把重复对拆到两个段里，
        同时钉住"不崩"与"检出"两件事。
        """
        work = fixture_dir('dupseg-cross')
        checker = F.DuplicateChecker(work_dir=os.path.join(work, 'seg'),
                                     segment_rows=2)
        layout = [(1, 10), (2, 20),      # 第 1 段
                  (1, 10), (3, 30)]      # 第 2 段：(1, 10) 与第 1 段重复
        for offset, (user, item) in enumerate(layout):
            checker.add(user, item, offset + 2)
            if len(checker.keys) >= 2:
                checker.end_segment()
        report = checker.finalize().report()
        self.assertEqual(len(checker.segments), 2,
                         'fixture must produce two segments so the merge path runs')
        self.assertGreater(report['duplicate_occurrences'], 0,
                           'cross-segment duplicate must be detected')
        self.assertFalse(report['packing']['keys_unique'])
        pairs = set((d['userId'], d['movieId'])
                    for d in report['duplicate_rows'])
        self.assertIn((1, 10), pairs)
        self.assertEqual(report['rows_checked'], 4)

    def test_cross_segment_merge_handles_a_small_final_segment(self):
        """尾段很小（< 524288 key）时归并不得崩，且必须给出正确结论——回归缺陷 3。"""
        work = fixture_dir('dupseg-small')
        checker = F.DuplicateChecker(work_dir=os.path.join(work, 'seg'),
                                     segment_rows=3)
        # 3 + 3 + 1：最后一段只有 1 条，远小于 524288
        layout = [(1, 1), (2, 2), (3, 3),
                  (4, 4), (5, 5), (6, 6),
                  (7, 7)]
        for offset, (user, item) in enumerate(layout):
            checker.add(user, item, offset + 2)
            if len(checker.keys) >= 3:
                checker.end_segment()
        report = checker.finalize().report()
        self.assertEqual([n for _p, n in checker.segments], [3, 3, 1])
        self.assertEqual(report['duplicate_occurrences'], 0)
        self.assertTrue(report['packing']['keys_unique'])
        self.assertEqual(report['rows_checked'], 7)

    def test_cross_segment_merge_preserves_unique_key_count(self):
        """无重复时归并必须走完全部段、不吞行（回归缺陷 3 的另一面）。"""
        work = fixture_dir('dupseg-uniq')
        checker = F.DuplicateChecker(work_dir=os.path.join(work, 'seg'),
                                     segment_rows=2)
        expected = []
        for user in range(1, 6):
            for item in range(1, 4):
                expected.append(user * 100 + item)
        line = 2
        for user in range(1, 6):
            for item in range(1, 4):
                checker.add(user, item, line)
                line += 1
                if len(checker.keys) >= 2:
                    checker.end_segment()
        report = checker.finalize().report()
        self.assertGreater(len(checker.segments), 1)
        self.assertEqual(report['rows_checked'], len(expected))
        self.assertEqual(report['duplicate_occurrences'], 0)
        self.assertEqual(report['packing']['max_user_id'], 5)
        self.assertEqual(report['packing']['max_movie_id'], 3)

    def test_duplicate_rows_are_scattered_across_users(self):
        """不同用户之间的同一 movieId **不是**重复（键是 (userId, movieId)）。"""
        work = fixture_dir('dup4')
        path = write_ratings(os.path.join(work, 'ratings.csv'), [
            (1, 10, 5.0, 100),
            (2, 10, 5.0, 200),
            (1, 20, 5.0, 300),
            (2, 20, 5.0, 400),
        ])
        rows, report = F.read_input_relevant_rows(path)
        self.assertEqual(len(rows), 4)
        self.assertEqual(report['duplicate_occurrences'], 0)
        self.assertTrue(report['packing']['keys_unique'])
        self.assertEqual(report['rows_checked'], 4)

    def test_parse_rating_fields_accepts_and_rejects_exactly(self):
        """逐字段口径：正整数 ID、0.5 半星档、正整数 timestamp（计划书 §3）。"""
        accepted = [
            (['1', '2', '4.0', '100'], (1, 2, 4.0, 100)),
            (['12345', '67890', '0.5', '1'], (12345, 67890, 0.5, 1)),
            (['1', '2', '5.0', '9999999999'], (1, 2, 5.0, 9999999999)),
            (['1', '2', '4.0', '100', 'extra'], (1, 2, 4.0, 100)),
        ]
        for parts, expected in accepted:
            self.assertEqual(F.parse_rating_fields(parts, 7, parts), expected,
                             'should accept %r' % (parts,))
        rejected = [
            ['1.0', '2', '4.0', '100'],     # 非十进制整数字面量
            ['+1', '2', '4.0', '100'],
            ['1e2', '2', '4.0', '100'],
            ['0x1', '2', '4.0', '100'],
            ['', '2', '4.0', '100'],        # 空字段就不是数字
            ['0', '2', '4.0', '100'],       # 非正整数
            ['1', '0', '4.0', '100'],
            ['1', '-2', '4.0', '100'],
            ['1', '2', '0.0', '100'],       # 越界
            ['1', '2', '5.5', '100'],
            ['1', '2', '4.3', '100'],       # 非半星档
            ['1', '2', 'abc', '100'],
            ['1', '2', 'nan', '100'],
            ['1', '2', 'inf', '100'],
            ['1', '2', '4.0', '0'],         # 非正时间戳
            ['1', '2', '4.0', '-5'],
            ['1', '2', '4.0', '1.5'],
            ['1', '2', '4.0', 'abc'],
            ['1', '2'],                     # 字段不足
        ]
        for parts in rejected:
            with self.assertRaises(F.InputValidationError,
                                   msg='should reject %r' % (parts,)) as caught:
                F.parse_rating_fields(parts, 7, parts)
            self.assertEqual(caught.exception.line_number, 7,
                             'rejection must carry the line number')

    def test_surrounding_whitespace_is_stripped_not_rejected(self):
        """字段两侧空白会被 ``strip()`` 掉（CSV 里 ``1, 2`` 很常见）。

        这是**已实现口径**，与"``1.0``/``+1`` 一律拒绝"不冲突：strip 只去空白，
        不做宽松数值解析。
        """
        self.assertEqual(F.parse_rating_fields([' 1', '2', '4.0', '100'], 7),
                         (1, 2, 4.0, 100))
        self.assertEqual(F.parse_rating_fields(['1', ' 2 ', '4.0\t', '100'], 7),
                         (1, 2, 4.0, 100))

    def test_header_must_have_required_columns(self):
        work = fixture_dir('header')
        path = os.path.join(work, 'bad.csv')
        with open(path, 'w', newline='', encoding='utf-8') as handle:
            handle.write('userId,movieId,rating\n1,2,4.0\n')
        with self.assertRaises(F.InputValidationError) as caught:
            F.read_input_relevant_rows(path)
        self.assertIn('timestamp', str(caught.exception))

    def test_header_columns_may_be_reordered(self):
        """按列位置取值，所以列顺序可以不同（``csv.reader`` 按名字解析索引）。"""
        work = fixture_dir('reorder')
        path = os.path.join(work, 'r.csv')
        with open(path, 'w', newline='', encoding='utf-8') as handle:
            handle.write('timestamp,movieId,userId,rating\n100,10,1,5.0\n'
                         '200,20,1,4.0\n')
        rows, report = F.read_input_relevant_rows(path)
        self.assertEqual(rows, [(1, 10, 5.0, 100), (1, 20, 4.0, 200)])
        self.assertEqual(report['duplicate_occurrences'], 0)


# ---------------------------------------------------------------------------
# 5. 相似度平票
# ---------------------------------------------------------------------------

class SimilarityTieTests(unittest.TestCase):
    """计划书 §4：按 ``(-similarity, target_movieId)`` 升序。

    平票（**精确相等**的相似度）时，目标必须按 movieId 升序——注意目标
    movieId 可能远大于物品个数，所以这条**不能靠位置下标碰巧成立**来做。
    """

    def test_ties_break_by_ascending_target_movieid(self):
        # 3 部电影被同一批用户完全一致地好评 → 两两相似度全为 1.0（平票）
        rows = []
        for user in range(1, 6):
            for movie in (300, 100, 200):     # 故意乱序写，避免"输入顺序即答案"
                rows.append((user, movie, 5.0, 1000 + user * 10 + movie // 100))
        X, item_ids, _u, _nl, _na = rows_to_csr(rows)
        self.assertEqual(item_ids, [100, 200, 300])
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        graph = graph_to_dict(arrays, item_ids)
        for source in (100, 200, 300):
            targets = [t for t, _s, _c in graph[source]]
            self.assertEqual(targets, sorted(targets),
                             'ties must be ordered by ascending target movieId')
        self.assertEqual([t for t, _s, _c in graph[100]], [200, 300])
        self.assertEqual([t for t, _s, _c in graph[300]], [100, 200])
        for source in graph:
            for _t, sim, _c in graph[source]:
                self.assertAlmostEqual(sim, 1.0, places=12)

    def test_ties_on_large_sparse_movieids(self):
        """movieId 远大于物品个数（如 100k+），平票仍按 movieId 升序。

        若实现按"位置下标"排平票，只要 ``item_ids`` 是升序就等价；这条用例
        用大 ID 差把"忘了升序归一"这类错误暴露出来。
        """
        ids = [900000, 40000, 1500000, 7]
        rows = []
        for user in range(1, 7):
            for movie in ids:
                rows.append((user, movie, 5.0, 1000 + user * 100 + movie % 97))
        X, item_ids, _u, _nl, _na = rows_to_csr(rows)
        self.assertEqual(item_ids, [7, 40000, 900000, 1500000])
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=2)
        graph = graph_to_dict(arrays, item_ids)
        for source in item_ids:
            targets = [t for t, _s, _c in graph[source]]
            self.assertEqual(targets, sorted(targets))
            self.assertEqual(targets, [t for t in item_ids if t != source])

    def test_strictly_descending_similarity_within_source(self):
        """非平票时必须严格按相似度降序（上轮排序键写反过的那个坑）。"""
        ns = np.random.RandomState(99)
        rows = []
        for user in range(1, 200):
            for item in range(1, 30):
                if ns.rand() < 0.25:
                    rows.append((user, item, 5.0, 1000 + user * 100 + item))
        X, item_ids, _u, _nl, _na = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=5)
        graph = graph_to_dict(arrays, item_ids)
        saw_descending = False
        for source, neighbors in graph.items():
            sims = [s for _t, s, _c in neighbors]
            for prev, nxt in zip(sims, sims[1:]):
                self.assertGreaterEqual(prev, nxt,
                                        'similarity must be non-increasing in '
                                        'source %d' % source)
            if len(sims) > 1 and sims[0] > sims[-1]:
                saw_descending = True
            # 精确平票处目标必须升序
            for pos in range(1, len(neighbors)):
                if neighbors[pos][1] == neighbors[pos - 1][1]:
                    self.assertGreater(neighbors[pos][0], neighbors[pos - 1][0])
        self.assertTrue(saw_descending, 'fixture should contain varied sims')

    def test_module_reference_sorts_ties_the_same_way(self):
        rows = []
        for user in range(1, 5):
            for movie in (500, 60, 7000, 8):
                rows.append((user, movie, 4.0, 1000 + user * 10 + movie % 13))
        X, item_ids, _u, _nl, _na = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        graph = graph_to_dict(arrays, item_ids)
        reference = F.reference_neighbors_python(X, item_ids, 0.0, 100,
                                                 block_size=1)
        for source in reference:
            self.assertEqual([t for t, _s, _c in graph[source]],
                             [t for t, _s, _c in reference[source]])


# ---------------------------------------------------------------------------
# 6. 逐条参考实现对拍（向量化 == 慢路径）
# ---------------------------------------------------------------------------

class VectorizedVersusReferenceTests(unittest.TestCase):
    """``build_block_edges`` 的向量化路径必须与 ``reference_neighbors_python``
    的逐条语义**逐边等价**（这是"向量化没有改变数学"的唯一证据）。
    """

    @staticmethod
    def _fixture(seed=20261004, n_users=260, n_items=300):
        ns = np.random.RandomState(seed)
        dense = (ns.rand(n_users, n_items) < 0.25).astype(np.int64)
        dense[:, :4] = 0                 # 4 个零好评物品
        X = sparse.csr_matrix(dense)
        item_ids = list(range(1001, 1001 + n_items))
        return X, item_ids

    def _compare(self, X, item_ids, block_size, beta, max_k):
        arrays = F.build_graph_arrays(X, item_ids, beta=beta, max_k=max_k,
                                      block_size=block_size)
        graph = graph_to_dict(arrays, item_ids)
        reference = F.reference_neighbors_python(X, item_ids, beta, max_k,
                                                 block_size=block_size)
        self.assertEqual(set(graph), set(reference),
                         'source sets differ at block_size=%d' % block_size)
        diffs = []
        for source in sorted(reference):
            mine = graph[source]
            theirs = reference[source]
            if len(mine) != len(theirs):
                diffs.append((source, 'len', len(mine), len(theirs)))
                continue
            for (mt, ms, mc), (rt, rs, rc) in zip(mine, theirs):
                if mt != rt or mc != rc or abs(ms - rs) > 1e-12:
                    diffs.append((source, mt, ms, mc, rt, rs, rc))
                    break
        self.assertEqual(diffs, [], 'edge differences at block_size=%d'
                         % block_size)
        return graph

    def test_block_size_16_32_64_128_are_equivalent(self):
        """计划书 §5.B：``block_size=16/32/64/128`` 必须给出**同一张图**。

        分块只影响中间临时量，不影响数学；这条用例把"分块边界算错"钉死。
        """
        X, item_ids = self._fixture()
        baseline = None
        stats = {}
        for block_size in (16, 32, 64, 128):
            graph = self._compare(X, item_ids, block_size, 0.0, 100)
            stats[block_size] = (len(graph),
                                 sum(len(v) for v in graph.values()))
            if baseline is None:
                baseline = graph
            else:
                self.assertEqual(sorted(graph), sorted(baseline),
                                 'source set differs at block_size=%d' % block_size)
                for source in baseline:
                    self.assertEqual(
                        [t for t, _s, _c in graph[source]],
                        [t for t, _s, _c in baseline[source]],
                        'neighbor order differs at block_size=%d source=%d'
                        % (block_size, source))
                    for (a_t, a_s, a_c), (b_t, b_s, b_c) in zip(graph[source],
                                                                baseline[source]):
                        self.assertEqual(a_t, b_t)
                        self.assertEqual(a_c, b_c)
                        self.assertAlmostEqual(a_s, b_s, places=12)
        self.assertEqual(len(set(stats.values())), 1,
                         'edge/source counts must be identical across block sizes: %r'
                         % (stats,))
        # 零好评物品全程无邻居、无自环
        for source, neighbors in baseline.items():
            for target, _sim, _c in neighbors:
                self.assertNotEqual(source, target)

    def test_equivalence_holds_with_beta_shrinkage(self):
        """``beta > 0`` 时先收缩再取 top-k（计划书 §4 与 core.py 一致）。"""
        X, item_ids = self._fixture(seed=5, n_users=120, n_items=150)
        for block_size in (16, 64):
            self._compare(X, item_ids, block_size, 50.0, 100)
        # 收缩必须真的改变数值（否则"beta 被忽略"也能过上面那条）
        arrays_cos = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                          block_size=64)
        arrays_shrunk = F.build_graph_arrays(X, item_ids, beta=50.0, max_k=100,
                                             block_size=64)
        self.assertTrue(np.all(arrays_shrunk['similarities']
                               <= arrays_cos['similarities'] + 1e-15))
        self.assertTrue(np.any(arrays_shrunk['similarities']
                               < arrays_cos['similarities'] - 1e-9))

    def test_max_k_cap_applies_per_source(self):
        """每来源最多 ``max_k`` 条；``max_k`` 变化时邻居集合是**前缀包含**关系。"""
        X, item_ids = self._fixture(seed=13, n_users=200, n_items=200)
        for max_k in (1, 3, 10, 100):
            arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=max_k,
                                          block_size=32)
            counts = np.diff(arrays['indptr'])
            self.assertLessEqual(int(counts.max()) if len(counts) else 0, max_k)
        wide = graph_to_dict(F.build_graph_arrays(X, item_ids, beta=0.0,
                                                  max_k=100, block_size=32),
                             item_ids)
        narrow = graph_to_dict(F.build_graph_arrays(X, item_ids, beta=0.0,
                                                    max_k=3, block_size=32),
                               item_ids)
        self.assertEqual(set(narrow), set(wide),
                         'the source set must not depend on max_k')
        for source in wide:
            self.assertEqual([t for t, _s, _c in narrow[source]],
                             [t for t, _s, _c in wide[source][:3]])

    def test_zero_and_negative_max_k_edges(self):
        X, item_ids = self._fixture(seed=3, n_users=30, n_items=20)
        reference = F.reference_neighbors_python(X, item_ids, 0.0, 0, block_size=8)
        self.assertEqual(reference, {})
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=0,
                                      block_size=8)
        self.assertEqual(len(arrays['sources']), 0)
        self.assertEqual(int(arrays['indptr'][-1]), 0)

    def test_empty_block_range_returns_typed_empty_arrays(self):
        """空区间（``stop <= start``）必须返回**正确 dtype** 的空数组。"""
        X, item_ids = self._fixture(seed=4, n_users=20, n_items=10)
        n_like = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)
        for start, stop in ((0, 0), (5, 5), (7, 3)):
            _s, _t, sims, coocs = F.build_block_edges(
                X, start, stop, n_like, 0.0, 100)
            self.assertEqual(len(sims), 0)
            self.assertEqual(len(coocs), 0)
            self.assertEqual(sims.dtype, np.float64)
            self.assertEqual(coocs.dtype, np.int64)

    def test_reference_path_ignores_low_rated_history(self):
        """低分不进好评矩阵，因此不产生相似度（计划书 §4：只用 rating >= 阈值）。"""
        rows = [
            (1, 10, 5.0, 100), (1, 20, 3.0, 101),   # 20 是低分
            (2, 10, 4.0, 200), (2, 20, 2.0, 201),
        ]
        X, item_ids, _u, n_like, _na = rows_to_csr(rows)
        self.assertEqual(list(n_like), [2, 0])
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        self.assertEqual(graph_to_dict(arrays, item_ids), {},
                         'low-rated movies must not become positive sources')


# ---------------------------------------------------------------------------
# 7. 存取回环（数组 / 顺序 / 哈希）
# ---------------------------------------------------------------------------

class ModelRoundTripTests(unittest.TestCase):
    """计划书 §4：读回时 mmap、验证结构和哈希后再使用；零邻居保留空区间。"""

    @staticmethod
    def _model_arrays():
        """一份含零好评来源、空区间、多来源的合法模型。"""
        rows = [
            (1, 10, 5.0, 100), (1, 20, 5.0, 101), (1, 30, 2.0, 102),
            (2, 10, 4.0, 200), (2, 20, 4.0, 201),
            (3, 10, 5.0, 300), (3, 20, 5.0, 301),
            (4, 40, 1.0, 400),                     # 40 零好评
        ]
        X, item_ids, _u, n_like, n_all = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=2)
        model = F.arrays_to_model(arrays, item_ids, n_like, n_all)
        return model, arrays, item_ids, n_like, n_all

    def test_save_load_roundtrip_is_byte_and_order_identical(self):
        model, arrays, item_ids, n_like, n_all = self._model_arrays()
        work = fixture_dir('roundtrip')
        model_dir = os.path.join(work, 'model')
        run_id = '20261004T120000Z-abcdef01'
        metadata = M.build_metadata(run_id=run_id,
                                    source={'path': 'synthetic',
                                            'sha256': 'deadbeef'},
                                    parameters={'max_k': 100, 'beta': 0.0,
                                                'positive_threshold': 4.0,
                                                'block_size': 2})
        saved = model.save(model_dir, metadata)
        # model_io.save() 的契约：返回**写好的元数据 dict**（缺陷 1 回归）
        self.assertIsInstance(saved, dict)
        self.assertEqual(saved['status'], M.STATUS_ASSEMBLED_UNVERIFIED)
        self.assertTrue(os.path.isfile(os.path.join(model_dir, M.METADATA_FILE)))

        reloaded, meta = M.load_model(model_dir, validate=True)
        self.assertEqual(meta['schema_version'], M.SCHEMA_VERSION)
        self.assertEqual(meta['run_id'], run_id)
        self.assertEqual(meta['model_id'], 'model-' + run_id)
        self.assertEqual(meta['status'], M.STATUS_ASSEMBLED_UNVERIFIED)

        # 逐数组：dtype / shape / 内容完全一致
        for name, original in (('item_ids', model.item_ids),
                               ('indptr', model.indptr),
                               ('target_indices', model.target_indices),
                               ('similarities', model.similarities),
                               ('cooccurrences', model.cooccurrences),
                               ('n_like', model.n_like),
                               ('n_all', model.n_all)):
            got = getattr(reloaded, name)
            want = np.asarray(original)
            self.assertEqual(got.dtype, want.dtype, '%s dtype' % name)
            self.assertEqual(got.shape, want.shape, '%s shape' % name)
            self.assertTrue(np.array_equal(np.asarray(got), want),
                            '%s content differs after round trip' % name)

        # 顺序语义：每来源按 (-sim, target_movieId) 升序
        graph = {}
        for pos in range(len(reloaded.item_ids)):
            start = int(reloaded.indptr[pos])
            stop = int(reloaded.indptr[pos + 1])
            if stop <= start:
                continue
            graph[int(reloaded.item_ids[pos])] = [
                (int(reloaded.item_ids[reloaded.target_indices[k]]),
                 float(reloaded.similarities[k]),
                 int(reloaded.cooccurrences[k]))
                for k in range(start, stop)]
        self.assertEqual(graph, graph_to_dict(arrays, item_ids))

        # 哈希全绿
        self.assertTrue(all(M.verify_hashes(model_dir).values()))

    def test_mmap_arrays_are_not_loaded_into_memory(self):
        """读取默认 mmap：数组应为 ``memmap``，不是一次性读满内存的 ndarray。"""
        model, _arrays, _ids, _nl, _na = self._model_arrays()
        work = fixture_dir('mmap')
        model_dir = os.path.join(work, 'model')
        model.save(model_dir, M.build_metadata(
            run_id='20261004T120000Z-aaaaaaaa', source={}, parameters={}))
        arrays = M.load_arrays(model_dir, mmap_mode='r')
        for name in M.ARRAY_FILES:
            key = name.replace('.npy', '')
            self.assertIsInstance(arrays[key], np.memmap,
                                  '%s should be memory-mapped' % name)
            self.assertTrue(np.array_equal(np.asarray(arrays[key]),
                                           np.asarray(getattr(model, key))))

    def test_zero_neighbor_sources_keep_empty_intervals(self):
        """零邻居来源必须保留**空区间**（不省略、不共享哨兵）。"""
        model, _arrays, item_ids, n_like, _na = self._model_arrays()
        work = fixture_dir('emptyint')
        model_dir = os.path.join(work, 'model')
        model.save(model_dir, M.build_metadata(
            run_id='20261004T120000Z-bbbbbbbb', source={}, parameters={}))
        reloaded, _meta = M.load_model(model_dir, validate=True)
        self.assertEqual(len(reloaded.indptr), len(item_ids) + 1)
        counts = np.diff(reloaded.indptr)
        zero_positions = [p for p, mid in enumerate(item_ids)
                          if int(reloaded.n_like[p]) == 0]
        self.assertTrue(zero_positions, 'fixture must contain a zero-like item')
        for pos in zero_positions:
            self.assertEqual(int(counts[pos]), 0,
                             'zero-like source %d must have an empty interval'
                             % pos)
        problems, stats = M.validate_arrays(reloaded, max_k=100)
        self.assertEqual(problems, [])
        self.assertEqual(stats['n_items'], len(item_ids))
        self.assertEqual(stats['n_edges'], int(reloaded.indptr[-1]))
        self.assertGreaterEqual(stats['n_empty_interval_sources'],
                                len(zero_positions))

    def test_status_machine_and_unknown_status_rejected(self):
        model, _arrays, _ids, _nl, _na = self._model_arrays()
        work = fixture_dir('status')
        model_dir = os.path.join(work, 'model')
        metadata = M.build_metadata(run_id='20261004T120000Z-cccccccc',
                                    source={}, parameters={})
        # 未验证 → assembled_unverified
        saved = model.save(model_dir, metadata)
        self.assertEqual(saved['status'], M.STATUS_ASSEMBLED_UNVERIFIED)
        self.assertEqual(
            M.read_json(os.path.join(model_dir, M.METADATA_FILE))['status'],
            M.STATUS_ASSEMBLED_UNVERIFIED)
        # 要求 validated_unpublished 时必须被拒（尚未验证）
        with self.assertRaises(M.ModelIOError):
            M.load_model(model_dir, validate=True,
                         require_status=M.STATUS_VALIDATED_UNPUBLISHED)
        # 非法状态值必须被拒
        with self.assertRaises(ValueError):
            model.save(model_dir, dict(metadata, status='active'))
        self.assertIn('validated_unpublished', M.ALLOWED_STATUSES)
        self.assertNotIn('active', M.ALLOWED_STATUSES)

    def test_verified_status_loads_and_then_hash_tamper_is_detected(self):
        model, _arrays, _ids, _nl, _na = self._model_arrays()
        work = fixture_dir('tamper')
        model_dir = os.path.join(work, 'model')
        metadata = M.build_metadata(run_id='20261004T120000Z-dddddddd',
                                    source={}, parameters={})
        path = os.path.join(model_dir, M.METADATA_FILE)
        saved = model.save(model_dir, metadata)
        self.assertEqual(saved['status'], M.STATUS_ASSEMBLED_UNVERIFIED)
        payload = M.read_json(path)
        payload['status'] = M.STATUS_VALIDATED_UNPUBLISHED
        M.write_json_atomic(path, payload)
        loaded, meta = M.load_model(model_dir, validate=True,
                                    require_status=M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertEqual(meta['status'], M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertEqual(loaded.n_items, model.n_items)

        # 篡改一个数组字节（不改长度）→ 哈希必须失配、加载必须失败。
        # Windows 上 memmap 会持有文件句柄，改写前必须释放（否则 OSError WinError 22）。
        del loaded
        gc.collect()
        target = os.path.join(model_dir, 'similarities.npy')
        raw = bytearray(open(target, 'rb').read())
        raw[-1] = raw[-1] ^ 0xFF
        with open(target, 'wb') as handle:
            handle.write(bytes(raw))
        hash_ok = M.verify_hashes(model_dir)
        self.assertFalse(hash_ok['similarities.npy'],
                         'tampering must fail the frozen hash')
        self.assertTrue(hash_ok['item_ids.npy'])
        with self.assertRaises(M.ModelIOError):
            M.load_model(model_dir, validate=True)

    def test_missing_metadata_is_an_error_not_a_degraded_load(self):
        model, _arrays, _ids, _nl, _na = self._model_arrays()
        work = fixture_dir('nometa')
        model_dir = os.path.join(work, 'model')
        model.save(model_dir, M.build_metadata(
            run_id='20261004T120000Z-eeeeeeee', source={}, parameters={}))
        os.remove(os.path.join(model_dir, M.METADATA_FILE))
        with self.assertRaises(M.ModelIOError) as caught:
            M.load_model(model_dir)
        self.assertIn('missing metadata', str(caught.exception))

    def test_wrong_dtype_on_disk_is_rejected(self):
        """数组 dtype 不符即拒（bool/uint8 计数永远不许被当模型读进来）。"""
        model, _arrays, _ids, _nl, _na = self._model_arrays()
        work = fixture_dir('dtype')
        model_dir = os.path.join(work, 'model')
        model.save(model_dir, M.build_metadata(
            run_id='20261004T120000Z-ffffffff', source={}, parameters={}))
        np.save(os.path.join(model_dir, 'cooccurrences.npy'),
                np.asarray(model.cooccurrences, dtype=np.uint8),
                allow_pickle=False)
        with self.assertRaises(M.ModelIOError) as caught:
            M.load_arrays(model_dir)
        self.assertIn('cooccurrences', str(caught.exception))

    def test_model_json_contains_required_schema_fields(self):
        """``model.json`` 的字段契约（计划书 §4 表格末行 + §6）。"""
        model, _arrays, _ids, _nl, _na = self._model_arrays()
        work = fixture_dir('schema')
        model_dir = os.path.join(work, 'model')
        run_id = '20261004T120000Z-12345678'
        source = {'path': '/x/ratings.csv', 'bytes': 123,
                  'sha256': 'abc', 'rows': 7, 'users': 3, 'items': 5}
        parameters = {'positive_threshold': 4.0, 'max_k': 100, 'beta': 0.0,
                      'block_size': 128}
        saved = model.save(model_dir, M.build_metadata(
            run_id=run_id, source=source, parameters=parameters))
        # 返回值必须与磁盘上的 model.json 内容一致（缺陷 1 回归的核心断言）
        meta = M.read_json(os.path.join(model_dir, M.METADATA_FILE))
        self.assertEqual(meta, dict((k, v) for k, v in saved.items()),
                         'save() must return the metadata that was written')
        for field in ('schema_version', 'model_id', 'run_id', 'status',
                      'created_at', 'source', 'parameters'):
            self.assertIn(field, meta, 'model.json must record %r' % field)
        self.assertEqual(meta['schema_version'], 1)
        self.assertEqual(meta['model_id'], 'model-' + run_id)
        self.assertEqual(meta['run_id'], run_id)
        self.assertEqual(meta['source'], source)
        self.assertEqual(meta['parameters'], parameters)
        # counts / dtypes / shapes / files 必须齐
        self.assertEqual(meta['counts']['n_items'], model.n_items)
        self.assertEqual(meta['counts']['n_edges'], model.n_edges)
        for name in M.ARRAY_FILES:
            self.assertIn(name, meta['dtypes'])
            self.assertIn(name, meta['shapes'])
            self.assertIn(name, meta['files'])
            self.assertIn('sha256', meta['files'][name])
            self.assertEqual(meta['files'][name]['bytes'],
                             os.path.getsize(os.path.join(model_dir, name)))
        # dtype 契约本身：计数一律 int64，相似度 float64
        self.assertEqual(meta['dtypes']['cooccurrences.npy'], 'int64')
        self.assertEqual(meta['dtypes']['item_ids.npy'], 'int64')
        self.assertEqual(meta['dtypes']['indptr.npy'], 'int64')
        self.assertEqual(meta['dtypes']['target_indices.npy'], 'int64')
        self.assertEqual(meta['dtypes']['similarities.npy'], 'float64')
        self.assertEqual(meta['dtypes']['n_like.npy'], 'int64')
        self.assertEqual(meta['dtypes']['n_all.npy'], 'int64')

    def test_validated_unpublished_does_not_mean_online_active(self):
        """状态值里没有 ``active``；``validated_unpublished`` 只是"自洽可加载"。

        ``verification_failed`` 是 H2 修复新增的**降级**状态：独立核验发现不符时
        由 ``apply_verification_verdict`` 写入，使 ``model.json`` 与它自己的核验者
        口径一致（不再出现"核验失败但仍写 validated_unpublished"的矛盾）。
        它**不是**发布状态，也不代表 online active。
        """
        self.assertEqual(sorted(M.ALLOWED_STATUSES),
                         ['assembled_unverified', 'build_incomplete',
                          'validated_unpublished', 'verification_failed'])
        self.assertNotIn('active', M.ALLOWED_STATUSES)
        self.assertNotIn('published', M.ALLOWED_STATUSES)
        self.assertIn(M.STATUS_VERIFICATION_FAILED, M.TERMINAL_FAILURE_STATUSES)

    def test_write_json_atomic_leaves_no_partial_file(self):
        work = fixture_dir('atomic')
        path = os.path.join(work, 'x.json')
        M.write_json_atomic(path, {'a': 1})
        self.assertEqual(M.read_json(path), {'a': 1})
        self.assertFalse(os.path.exists(path + '.tmp'),
                         'temp file must be renamed away, not left behind')

    def test_metadata_ids_must_be_real_shape(self):
        """禁止把通用 ``run`` 写成产物 ID（上轮的 bug）。"""
        with self.assertRaises(ValueError):
            M.compute_model_id('run')
        with self.assertRaises(ValueError):
            M.build_metadata(run_id='run', source={}, parameters={})
        with self.assertRaises(ValueError):
            M.verify_id_format('20261004T120000Z-DEADBEEF')   # 必须小写十六进制
        with self.assertRaises(ValueError):
            M.verify_id_format('20261004T120000-deadbeef')    # 缺 Z
        self.assertEqual(M.compute_model_id('20261004T120000Z-deadbeef'),
                         'model-20261004T120000Z-deadbeef')


# ---------------------------------------------------------------------------
# 8. BlockStore / 磁盘 CSR / 块索引契约
# ---------------------------------------------------------------------------

class BlockStoreAndDiskCSRTests(unittest.TestCase):
    """计划书 §5.B："图边文件按块落盘便于验收和失败留档；禁止拼入来源/参数/
    源码不同的旧块"。
    """

    SIGNATURE = {'signature': 'sig-1', 'input_sha256': 'abc', 'max_k': 100}

    def test_block_roundtrip_preserves_arrays_and_order(self):
        work = fixture_dir('blockstore')
        store = F.BlockStore(work, self.SIGNATURE)
        sources = np.array([0, 0, 2, 2, 2], dtype=np.int64)
        targets = np.array([1, 3, 0, 1, 3], dtype=np.int64)
        sims = np.array([0.9, 0.4, 0.8, 0.6, 0.2])
        coocs = np.array([9, 4, 8, 6, 2], dtype=np.int64)
        store.write_block(0, 0, 3, sources, targets, sims, coocs, 0.25)
        store.write_block(1, 3, 4, np.zeros(0, dtype=np.int64),
                          np.zeros(0, dtype=np.int64), np.zeros(0, np.float64),
                          np.zeros(0, dtype=np.int64), 0.01)
        store.flush()

        arrays, problems = store.load_blocks()
        self.assertEqual(problems, [])
        self.assertEqual(arrays['sources'].dtype, np.int64)
        self.assertEqual(arrays['targets'].dtype, np.int64)
        self.assertEqual(arrays['similarities'].dtype, np.float64)
        self.assertEqual(arrays['cooccurrences'].dtype, np.int64)
        self.assertTrue(np.array_equal(arrays['sources'], sources))
        self.assertTrue(np.array_equal(arrays['targets'], targets))
        self.assertTrue(np.array_equal(arrays['similarities'], sims))
        self.assertTrue(np.array_equal(arrays['cooccurrences'], coocs))
        # 索引里每块都记了来源区间与 dtype
        index = M.read_json(os.path.join(work, F.BLOCK_INDEX_FILE))
        self.assertEqual(index['schema_version'], F.BLOCK_SCHEMA_VERSION)
        self.assertEqual(index['blocks_written'], 2)
        self.assertEqual(index['blocks_failed'], 0)
        for record in index['blocks']:
            self.assertEqual(record['dtypes']['cooccurrences'], 'int64')
            self.assertEqual(record['dtypes']['similarities'], 'float64')
            self.assertEqual(record['signature'], self.SIGNATURE)

    def test_failed_block_is_recorded_and_poisons_the_load(self):
        """失败块也留档；且**有任何失败块时不得装配**（不"凑合装配"）。"""
        work = fixture_dir('blockfail')
        store = F.BlockStore(work, self.SIGNATURE)
        store.write_block(0, 0, 2, np.array([0], dtype=np.int64),
                          np.array([1], dtype=np.int64),
                          np.array([1.0]), np.array([1], dtype=np.int64), 0.1)
        store.write_failure(1, 2, 4, 'MemoryError: boom', 0.05)
        store.flush()
        arrays, problems = store.load_blocks()
        self.assertIsNone(arrays)
        self.assertTrue(any('marked failed' in p for p in problems))
        failed = M.read_json(os.path.join(work, F.FAILED_BLOCKS_FILE))
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]['status'], 'failed')
        self.assertIn('boom', failed[0]['reason'])
        success = M.read_json(os.path.join(work, F.SUCCESS_BLOCKS_FILE))
        self.assertEqual(success, {'0': 1})

    def test_existing_block_index_is_refused(self):
        """独占目录：已有块索引时必须报错，不得拼入旧块。"""
        work = fixture_dir('blockreuse')
        F.BlockStore(work, self.SIGNATURE).flush()
        store = F.BlockStore(work, self.SIGNATURE)
        with self.assertRaises(F.CorrectnessError) as caught:
            store._check_existing()
        self.assertIn('exclusive', str(caught.exception))

    def test_signature_mismatch_is_reported(self):
        work = fixture_dir('blocksig')
        store = F.BlockStore(work, self.SIGNATURE)
        store.write_block(0, 0, 2, np.array([0], dtype=np.int64),
                          np.array([1], dtype=np.int64), np.array([0.5]),
                          np.array([1], dtype=np.int64), 0.0)
        store.flush()
        other = F.BlockStore.__new__(F.BlockStore)
        other.out_dir = work
        other.index_path = os.path.join(work, F.BLOCK_INDEX_FILE)
        other.signature = {'signature': 'DIFFERENT'}
        other.log = None
        other.records = {}
        other._last_flush = 0
        arrays, problems = other.load_blocks()
        self.assertIsNone(arrays)
        self.assertTrue(any('signature mismatch' in p for p in problems))

    def test_disk_csr_roundtrip_matches_in_memory_matrix(self):
        """两遍扫描 → 磁盘段 → 归并 CSR 的结果必须等于内存构造的矩阵。"""
        rows = []
        for user in range(1, 41):
            for item in range(1, 16):
                if (user * item) % 3 != 0:
                    rating = 5.0 if (user + item) % 4 else 2.0
                    rows.append((user, item, rating, 1000 + user * 100 + item))
        work = fixture_dir('disksr')
        path = write_ratings(os.path.join(work, 'ratings.csv'), rows)
        index = F.stream_input_pass1(path)
        args = F.parse_args(['--input', path, '--out', work, '--worker',
                             '--max-k', '100', '--beta', '0'])
        csr_paths, _seconds = F.run_prepare(path, index, work, args)
        X = F.open_csr(csr_paths, index)
        reference, item_ids_ref, user_ids_ref, _nl, _na = rows_to_csr(rows)
        self.assertEqual(X.shape,
                         (len(user_ids_ref), len(item_ids_ref)))
        self.assertEqual(X.nnz, reference.nnz)
        self.assertEqual([int(v) for v in index.item_ids], item_ids_ref)
        self.assertTrue(np.array_equal(np.asarray(X.todense()),
                                       np.asarray(reference.todense())))
        self.assertEqual(X.dtype, np.int64)
        # 磁盘数组必须是 int64 的 npy（可 mmap），不是 npz
        for name in ('indptr', 'indices', 'shape'):
            path_npy = os.path.join(work, 'csr_work', 'csr_%s.npy' % name)
            self.assertTrue(os.path.isfile(path_npy))
        self.assertEqual(np.load(os.path.join(work, 'csr_work',
                                              'csr_indptr.npy')).dtype,
                         np.int64)

    def test_nnz_mismatch_between_passes_is_a_correctness_error(self):
        """两遍扫描不一致时必须抛错，不能"凑合用"（计划书 §5.B）。"""
        work = fixture_dir('nnzmismatch')
        path = write_ratings(os.path.join(work, 'ratings.csv'), [
            (1, 10, 5.0, 100), (1, 20, 5.0, 101), (2, 10, 4.0, 200)])
        index = F.stream_input_pass1(path)
        index.n_like[0] = 99               # 人为制造"两遍不一致"
        args = F.parse_args(['--input', path, '--out', work, '--worker',
                             '--max-k', '100', '--beta', '0'])
        with self.assertRaises(F.CorrectnessError) as caught:
            F.run_prepare(path, index, work, args)
        self.assertIn('nnz mismatch', str(caught.exception))

    def test_load_blocks_detects_unsorted_sources_on_disk(self):
        """``load_blocks`` 必须检出块内来源未升序（写盘后改文件才可能发生）。

        ``write_block`` 只校验 dtype/范围；**来源升序** 的强制点在
        ``arrays_to_model``（见下一条）与 ``load_blocks`` 的完整性检查。
        这里手工造一个乱序块文件，验证读侧不放过它。
        """
        work = fixture_dir('unsorted-load')
        store = F.BlockStore(work, self.SIGNATURE)
        path = store.block_path(0)
        np.savez(path, sources=np.array([2, 0], dtype=np.int64),
                 targets=np.array([1, 1], dtype=np.int64),
                 similarities=np.array([0.5, 0.5]),
                 cooccurrences=np.array([1, 1], dtype=np.int64),
                 schema_version=np.int64(F.BLOCK_SCHEMA_VERSION))
        store.records[0] = {
            'index': 0, 'start': 0, 'stop': 3, 'edges': 2, 'seconds': 0.0,
            'status': 'success',
            'dtypes': {'sources': 'int64', 'targets': 'int64',
                       'similarities': 'float64', 'cooccurrences': 'int64'},
            'file': os.path.relpath(path, work), 'signature': self.SIGNATURE,
        }
        store.flush()
        arrays, problems = store.load_blocks()
        self.assertIsNone(arrays)
        self.assertTrue(any('not sorted ascending' in p for p in problems),
                        'unsorted sources must be reported: %r' % (problems,))

    def test_write_block_refuses_wrong_dtypes_and_records_ranges(self):
        """``write_block`` 的写前校验：dtype 与记录内容。"""
        work = fixture_dir('writeblock')
        store = F.BlockStore(work, self.SIGNATURE)
        good_s = np.array([0, 0], dtype=np.int64)
        good_t = np.array([1, 2], dtype=np.int64)
        good_sim = np.array([0.75, 0.5], dtype=np.float64)
        good_c = np.array([9, 5], dtype=np.int64)
        for bad_sources, bad_sims, bad_coocs in (
                (good_s.astype(np.int32), good_sim, good_c),
                (good_s.astype(np.uint8), good_sim, good_c),
                (good_s, good_sim.astype(np.float32), good_c),
                (good_s, good_sim, good_c.astype(np.uint8)),
                (good_s, good_sim, good_c.astype(bool))):
            with self.assertRaises(F.CorrectnessError):
                store.write_block(0, 0, 3, bad_sources, good_t, bad_sims,
                                  bad_coocs, 0.0)
        record = store.write_block(0, 0, 3, good_s, good_t, good_sim, good_c, 0.5)
        self.assertEqual(record['index'], 0)
        self.assertEqual(record['start'], 0)
        self.assertEqual(record['stop'], 3)
        self.assertEqual(record['edges'], 2)
        self.assertEqual(record['status'], 'success')
        self.assertEqual(record['seconds'], 0.5)
        self.assertEqual(record['dtypes']['cooccurrences'], 'int64')

    def test_arrays_to_model_rejects_unsorted_sources(self):
        """``arrays_to_model`` 也必须拒——不静默重排（静默重排会掩盖上游 bug）。"""
        arrays = {'sources': np.array([2, 0], dtype=np.int64),
                  'targets': np.array([1, 1], dtype=np.int64),
                  'similarities': np.array([0.5, 0.5]),
                  'cooccurrences': np.array([1, 1], dtype=np.int64)}
        with self.assertRaises(F.CorrectnessError) as caught:
            F.arrays_to_model(arrays, [10, 20, 30],
                              np.array([1, 1, 1], dtype=np.int64),
                              np.array([1, 1, 1], dtype=np.int64))
        self.assertIn('not sorted ascending', str(caught.exception))

    def test_arrays_to_model_rejects_out_of_range_source(self):
        arrays = {'sources': np.array([0, 5], dtype=np.int64),
                  'targets': np.array([1, 1], dtype=np.int64),
                  'similarities': np.array([0.5, 0.5]),
                  'cooccurrences': np.array([1, 1], dtype=np.int64)}
        with self.assertRaises(F.CorrectnessError) as caught:
            F.arrays_to_model(arrays, [10, 20, 30],
                              np.array([1, 1, 1], dtype=np.int64),
                              np.array([1, 1, 1], dtype=np.int64))
        self.assertIn('out of range', str(caught.exception))

    def test_indptr_semantics_after_assembly(self):
        """``indptr`` 语义：长度 n+1、首 0、单调、末值 == n_edges、区间左闭右开。"""
        work = fixture_dir('indptr')
        rows = [
            (1, 10, 5.0, 1), (1, 20, 5.0, 2), (1, 30, 1.0, 3),
            (2, 10, 5.0, 4), (2, 20, 5.0, 5),
            (3, 20, 5.0, 6), (3, 30, 5.0, 7),
        ]
        X, item_ids, _u, n_like, n_all = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=1)
        model = F.arrays_to_model(arrays, item_ids, n_like, n_all)
        self.assertEqual(len(model.indptr), len(item_ids) + 1)
        self.assertEqual(int(model.indptr[0]), 0)
        self.assertEqual(int(model.indptr[-1]), model.n_edges)
        self.assertTrue(np.all(np.diff(model.indptr) >= 0))
        # 反查来源：每条边的来源就是它的 indptr 区间
        for pos in range(len(item_ids)):
            start, stop = int(model.indptr[pos]), int(model.indptr[pos + 1])
            for k in range(start, stop):
                self.assertNotEqual(int(model.target_indices[k]), pos)


# ---------------------------------------------------------------------------
# 9. 预检块选择 / 运行时外推 / 磁盘估算 / 命名契约
# ---------------------------------------------------------------------------

class PrecheckAndProjectionTests(unittest.TestCase):
    """计划书 §5.C：最多六块、按 ``128→64→32→16`` 阶梯、mean/max 双界外推。"""

    def test_precheck_ladder_is_exactly_the_specified_one(self):
        self.assertEqual(F.PRECHECK_BLOCK_LADDER, (128, 64, 32, 16))

    def test_select_precheck_blocks_covers_first_middle_last_and_peak(self):
        n_items, block_size = 64, 4          # 16 块
        n_like = np.ones(n_items, dtype=np.int64)
        n_like[37] = 10 ** 6                 # 支持度最高的物品在第 9 块
        chosen = F.select_precheck_blocks(n_items, block_size, n_like)
        self.assertLessEqual(len(chosen), 6)
        self.assertEqual(sorted(set(chosen)), chosen, 'must be sorted/unique')
        self.assertIn(0, chosen)
        self.assertIn(15, chosen)            # 末块
        self.assertIn(7, chosen)             # 中间块
        self.assertIn(9, chosen)             # 最高支持度物品所在块
        for index in chosen:
            self.assertTrue(0 <= index < 16)

    def test_select_precheck_blocks_is_deterministic_and_bounded(self):
        n_like = np.arange(1, 101, dtype=np.int64)
        first = F.select_precheck_blocks(100, 3, n_like)
        second = F.select_precheck_blocks(100, 3, n_like)
        self.assertEqual(first, second, 'hash-selected blocks must be stable')
        self.assertLessEqual(len(first), 6)

    def test_select_precheck_blocks_degenerate_inputs(self):
        self.assertEqual(F.select_precheck_blocks(0, 128,
                                                  np.zeros(0, dtype=np.int64)),
                         [])
        self.assertEqual(F.select_precheck_blocks(5, 128,
                                                  np.ones(5, dtype=np.int64)),
                         [0])
        self.assertEqual(F.select_precheck_blocks(1, 1,
                                                  np.array([3], dtype=np.int64)),
                         [0])

    def test_project_runtime_reports_mean_max_and_gates_on_max(self):
        precheck = {'blocks': [{'seconds': 1.0}, {'seconds': 4.0},
                               {'seconds': 2.0}]}
        projection = F.project_runtime(precheck, n_blocks=10,
                                       budget_seconds=100.0)
        self.assertEqual(projection['samples'], 3)
        self.assertAlmostEqual(projection['mean_block_seconds'], 7.0 / 3.0,
                               places=5)      # 落盘前 round 到 6 位
        self.assertEqual(projection['max_block_seconds'], 4.0)
        self.assertEqual(projection['min_block_seconds'], 1.0)
        # 判定必须用 max 界 + 装配预留（保守），不是 mean
        self.assertAlmostEqual(projection['projected_seconds_max'], 40.0)
        self.assertAlmostEqual(projection['assembly_reserve_seconds'], 6.0)
        self.assertTrue(projection['within_budget'])          # 46 <= 100
        tight = F.project_runtime(precheck, 10, 30.0)
        self.assertFalse(tight['within_budget'])              # 46 > 30
        self.assertIn('NOT a reliable runtime upper bound', projection['note'])

    def test_project_runtime_without_samples_never_claims_pass(self):
        projection = F.project_runtime({'blocks': []}, 10, 10 ** 9)
        self.assertEqual(projection['samples'], 0)
        self.assertFalse(projection['within_budget'])
        self.assertIsNone(projection['projected_seconds_max'])

    def test_estimate_disk_bytes_uses_int64_sized_arrays(self):
        class FakeIndex(object):
            n_items = 1000
            n_users = 100
            positive_pairs = staticmethod(lambda: 5000)

        estimate = F.estimate_disk_bytes(FakeIndex(), max_k=100,
                                         block_size=128)
        # worst_case_edges = min(positive_pairs, n_items * max_k) = min(5000, 100000)
        self.assertEqual(estimate['worst_case_edges'], 5000)
        # 每个边数组 32 字节 = 4 个 int64（source/target/sim/c）
        self.assertEqual(estimate['edge_arrays_bytes'], 5000 * 32)
        self.assertEqual(estimate['n_blocks'], 8)
        self.assertEqual(estimate['csr_data_bytes'], 5000 * 8)
        self.assertEqual(estimate['csr_indices_bytes'], 5000 * 8)
        self.assertEqual(estimate['csr_indptr_bytes'], 101 * 8)
        self.assertGreater(estimate['total_bytes'], 0)
        # max_k 收紧时最坏边数随之下降（说明 max_k 真的进了估算）
        tight = F.estimate_disk_bytes(FakeIndex(), max_k=10, block_size=128)
        self.assertEqual(tight['worst_case_edges'], min(5000, 1000 * 10))

    def test_run_id_shape_and_uniqueness(self):
        first = F.make_run_id()
        second = F.make_run_id()
        self.assertNotEqual(first, second)
        M.verify_id_format(first, 'run_id')
        self.assertRegex(first, r'^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$')
        self.assertEqual(F.make_run_id(now='20261004T120000Z', token='deadbeef'),
                         '20261004T120000Z-deadbeef')

    def test_ids_are_consistent_with_output_directory_name(self):
        """计划书 §5.D：父 run ID / model ID / 产物内 ID 三重一致。"""
        out = os.path.join(PROJECT, 'x', '20261004T120000Z-deadbeef')
        run_id, model_id = F.assembled_ids(out)
        self.assertEqual(run_id, '20261004T120000Z-deadbeef')
        self.assertEqual(model_id, 'model-20261004T120000Z-deadbeef')
        with self.assertRaises(ValueError):
            F.assembled_ids(os.path.join(PROJECT, 'x', 'run'))

    def test_run_id_from_out_and_resolve_out_dir(self):
        tail = os.path.join(PROJECT, 'out', '20261004T120000Z-deadbeef')
        self.assertEqual(F.run_id_from_out(tail), '20261004T120000Z-deadbeef')
        plain = os.path.join(PROJECT, 'out')
        self.assertIsNone(F.run_id_from_out(plain))
        # 末段等于 run_id → 直接用
        work = fixture_dir('resolve')
        pinned = os.path.join(work, '20261004T120000Z-deadbeef')
        self.assertEqual(F.resolve_out_dir(pinned, '20261004T120000Z-deadbeef'),
                         os.path.abspath(pinned))
        # 普通目录 → 套一层 <out>/<run_id>
        nested = F.resolve_out_dir(os.path.join(work, 'parent'),
                                   '20261004T120000Z-11111111')
        self.assertTrue(nested.endswith('20261004T120000Z-11111111'))
        # 已有目录 → 拒绝复用
        os.makedirs(pinned)
        with self.assertRaises(RuntimeError):
            F.resolve_out_dir(pinned, '20261004T120000Z-deadbeef')

    def test_no_bypass_switch_in_cli(self):
        """计划书 §6："不提供默认绕过保护的开关"。"""
        with self.assertRaises(SystemExit):
            F.parse_args([])
        args = F.parse_args(['--input', 'a.csv', '--out', 'o'])
        self.assertFalse(args.worker)
        self.assertEqual(args.max_k, 100)
        self.assertEqual(args.beta, 0.0)
        self.assertEqual(args.positive_threshold, 4.0)
        self.assertEqual(args.block_size, 128)
        self.assertEqual(args.monitor_interval, 2.0)
        self.assertEqual(args.prepare_budget_seconds, 3600.0)
        self.assertEqual(args.precheck_budget_seconds, 1800.0)
        self.assertEqual(args.train_budget_seconds, 14400.0)
        self.assertEqual(args.duplicate_policy, 'reject')
        for forbidden in ('--no-guard', '--skip-precheck', '--unsupervised',
                          '--no-monitor', '--force'):
            with self.assertRaises(SystemExit,
                                   msg='%s must not exist' % forbidden):
                F.parse_args(['--input', 'a.csv', '--out', 'o', forbidden])

    def test_invalid_cli_values_are_refused(self):
        for argv in (['--input', 'a', '--out', 'o', '--max-k', '0'],
                     ['--input', 'a', '--out', 'o', '--block-size', '0'],
                     ['--input', 'a', '--out', 'o', '--beta', '-1'],
                     ['--input', 'a', '--out', 'o', '--monitor-interval', '0'],
                     ['--input', 'a', '--out', 'o',
                      '--train-budget-seconds', '0'],):
            with self.assertRaises(SystemExit, msg='should refuse %r' % (argv,)):
                F.parse_args(argv)
        # 非法 --run-id 走 verify_id_format，抛的是 ValueError（不是 SystemExit）
        with self.assertRaises(ValueError):
            F.parse_args(['--input', 'a', '--out', 'o', '--run-id', 'run'])

    def test_worker_argv_preserves_all_build_parameters(self):
        args = F.parse_args(['--input', 'in.csv', '--out', 'outdir',
                             '--max-k', '7', '--beta', '0.5',
                             '--positive-threshold', '3.5',
                             '--block-size', '16', '--seed', '9',
                             '--run-id', '20261004T120000Z-aabbccdd'])
        argv = F.worker_argv(args)
        self.assertIn('--worker', argv)
        joined = ' '.join(argv)
        for expected in ('--max-k 7', '--beta 0.5', '--positive-threshold 3.5',
                         '--block-size 16', '--seed 9',
                         '--run-id 20261004T120000Z-aabbccdd',
                         '--duplicate-policy reject'):
            self.assertIn(expected, joined)
        # 不带 --run-id 时不得留下空参数
        args2 = F.parse_args(['--input', 'in.csv', '--out', 'outdir'])
        argv2 = F.worker_argv(args2)
        self.assertNotIn('--run-id', argv2)
        self.assertNotIn('', argv2)

    def test_parameters_of_records_every_build_parameter(self):
        args = F.parse_args(['--input', 'a', '--out', 'o'])
        params = F.parameters_of(args)
        for field in ('positive_threshold', 'max_k', 'beta', 'block_size',
                      'monitor_interval', 'prepare_budget_seconds',
                      'precheck_budget_seconds', 'train_budget_seconds',
                      'seed', 'duplicate_policy'):
            self.assertIn(field, params)
        self.assertEqual(params['max_k'], 100)
        self.assertEqual(params['beta'], 0.0)
        self.assertEqual(params['positive_threshold'], 4.0)

    def test_blas_threads_are_pinned_to_one(self):
        env = F.set_blas_threads_to_one()
        for name in ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                     'MKL_NUM_THREADS', 'NUMEXPR_NUM_THREADS'):
            self.assertEqual(os.environ.get(name), '1')
            self.assertEqual(env[name], '1')

    def test_stage_file_roundtrip_and_rejects_unknown_stage(self):
        work = fixture_dir('stage')
        self.assertIsNone(F.read_stage(work))
        for stage in F.PHASES:
            F._notify_stage(work, stage)
            self.assertEqual(F.read_stage(work), stage)
        with self.assertRaises(ValueError):
            F._notify_stage(work, 'not-a-stage')
        with open(os.path.join(work, 'stage.txt'), 'w') as handle:
            handle.write('{ this is not json')
        self.assertIsNone(F.read_stage(work),
                          'a corrupt stage file must read as None, not guess')

    def test_source_signature_changes_with_parameters(self):
        work = fixture_dir('signature')
        path = write_ratings(os.path.join(work, 'r.csv'),
                             [(1, 10, 5.0, 100), (2, 10, 4.0, 200)])
        index = F.stream_input_pass1(path)
        base = F.parse_args(['--input', path, '--out', work, '--worker'])
        first = F.source_signature(index, base, 'sha-abc')
        same = F.source_signature(index, base, 'sha-abc')
        self.assertEqual(first['signature'], same['signature'])
        other_input = F.source_signature(index, base, 'sha-xyz')
        self.assertNotEqual(first['signature'], other_input['signature'])
        changed = F.parse_args(['--input', path, '--out', work, '--worker',
                                '--max-k', '5'])
        self.assertNotEqual(first['signature'],
                            F.source_signature(index, changed, 'sha-abc')['signature'])
        changed_beta = F.parse_args(['--input', path, '--out', work, '--worker',
                                     '--beta', '10'])
        self.assertNotEqual(first['signature'],
                            F.source_signature(index, changed_beta,
                                               'sha-abc')['signature'])
        # 源码哈希必须进签名（"禁止拼入源码不同的旧块"）
        self.assertIn('full_train.py', first['source_sha256'])
        self.assertIn('model_io.py', first['source_sha256'])


# ---------------------------------------------------------------------------
# 10. 采样来源独立重算（verify_full_model 的裁判路径）
# ---------------------------------------------------------------------------

class IndependentRecomputeTests(unittest.TestCase):
    """计划书 §5.D：从完整训练评分**独立重算**抽样来源的全部邻居。

    这里用 ``verify_full_model.recompute_source_neighbors``（"一列乘全矩阵"）作
    裁判，它对抽样来源重算共现、相似度、top-k，覆盖的**不只是已保存的正边**
    ——否则发现不了"漏掉更优邻居"。
    """

    def test_recompute_matches_saved_neighbors_for_all_sources(self):
        ns = np.random.RandomState(4242)
        rows = []
        for user in range(1, 151):
            for item in range(1, 60):
                if ns.rand() < 0.3:
                    rows.append((user, item, 5.0, 1000 + user * 100 + item))
        X, item_ids, _u, n_like, n_all = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=8)
        model = F.arrays_to_model(arrays, item_ids, n_like, n_all)
        margin = V.SIMILARITY_TOLERANCE

        checked = 0
        for source_pos in range(len(item_ids)):
            start = int(model.indptr[source_pos])
            stop = int(model.indptr[source_pos + 1])
            expected = V.recompute_source_neighbors(
                X.tocsr(), source_pos, np.asarray(n_like, dtype=np.int64),
                100, 0.0)
            self.assertEqual(stop - start, len(expected),
                             'edge count differs for source pos %d' % source_pos)
            for offset, (target_pos, sim, c) in enumerate(expected):
                edge = start + offset
                self.assertEqual(int(model.target_indices[edge]), target_pos)
                self.assertEqual(int(model.cooccurrences[edge]), c)
                self.assertAlmostEqual(float(model.similarities[edge]), sim,
                                       delta=max(margin, 1e-12 * abs(sim)))
            checked += 1
        self.assertEqual(checked, len(item_ids))

    def test_recompute_catches_a_dropped_better_neighbor(self):
        """负向对照：如果实现漏掉了一条更优邻居，独立重算必须能发现。

        做法：把某个来源的**第一条**（最优）邻居删掉，再跑同一条重算比对——
        必须报"边数不一致"。这证明上面的用例不是空转。
        """
        rows = []
        for user in range(1, 61):
            for item in range(1, 12):
                if (user + item) % 3 != 0:
                    rows.append((user, item, 5.0, 1000 + user * 100 + item))
        X, item_ids, _u, n_like, n_all = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                      block_size=4)
        model = F.arrays_to_model(arrays, item_ids, n_like, n_all)

        victim = None
        for source_pos in range(len(item_ids)):
            start = int(model.indptr[source_pos])
            stop = int(model.indptr[source_pos + 1])
            if stop - start >= 2:
                victim = source_pos
                break
        self.assertIsNotNone(victim, 'fixture must have a multi-neighbor source')

        # 复制一份 indptr，删掉 victim 的最优邻居 → 边数少 1
        broken_indptr = np.array(model.indptr, dtype=np.int64)
        broken_indptr[victim + 1:] -= 1
        broken = M.NeighborModel(
            item_ids=model.item_ids, indptr=broken_indptr,
            target_indices=model.target_indices[1:],
            similarities=model.similarities[1:],
            cooccurrences=model.cooccurrences[1:],
            n_like=model.n_like, n_all=model.n_all)
        expected = V.recompute_source_neighbors(
            X.tocsr(), victim, np.asarray(n_like, dtype=np.int64), 100, 0.0)
        got_start = int(broken.indptr[victim])
        got_stop = int(broken.indptr[victim + 1])
        self.assertNotEqual(got_stop - got_start, len(expected),
                            'a dropped best neighbor must be detectable')

    def test_recompute_with_beta_matches_block_construction(self):
        ns = np.random.RandomState(31)
        rows = []
        for user in range(1, 121):
            for item in range(1, 40):
                if ns.rand() < 0.3:
                    rows.append((user, item, 5.0, 1000 + user * 100 + item))
        X, item_ids, _u, n_like, _n_a = rows_to_csr(rows)
        arrays = F.build_graph_arrays(X, item_ids, beta=50.0, max_k=100,
                                      block_size=8)
        model = F.arrays_to_model(arrays, item_ids, n_like,
                                  np.asarray(n_like, dtype=np.int64))
        for source_pos in range(0, len(item_ids), 3):
            start = int(model.indptr[source_pos])
            stop = int(model.indptr[source_pos + 1])
            expected = V.recompute_source_neighbors(
                X.tocsr(), source_pos, np.asarray(n_like, dtype=np.int64),
                100, 50.0)
            self.assertEqual(stop - start, len(expected))
            for offset, (target_pos, sim, c) in enumerate(expected):
                edge = start + offset
                self.assertEqual(int(model.target_indices[edge]), target_pos)
                self.assertEqual(int(model.cooccurrences[edge]), c)
                self.assertAlmostEqual(float(model.similarities[edge]), sim,
                                       places=12)

    def test_sampled_source_selection_is_deterministic_and_relevant(self):
        ns = np.random.RandomState(8)
        rows = []
        for user in range(1, 101):
            for item in range(1, 30):
                if ns.rand() < 0.3:
                    rows.append((user, item, 5.0, 1000 + user * 100 + item))
        X, item_ids, _u, n_like, n_all = rows_to_csr(rows)
        model = F.arrays_to_model(
            F.build_graph_arrays(X, item_ids, beta=0.0, max_k=100,
                                 block_size=8),
            item_ids, n_like, n_all)
        first = V.select_sample_sources(model, seed=42, count=10)
        second = V.select_sample_sources(model, seed=42, count=10)
        self.assertEqual(first, second, 'sampling must be reproducible')
        counts = np.diff(model.indptr)
        for group in ('high_support', 'low_support', 'boundary', 'all'):
            for pos in first[group]:
                self.assertGreater(int(counts[pos]), 0,
                                   'only sources with neighbors may be sampled')
        self.assertTrue(first['all'])


# ---------------------------------------------------------------------------
# 11. 等价性对拍：旧 2k 固定训练段 vs fix14（k=100, beta=0）
# ---------------------------------------------------------------------------

@unittest.skipUnless(os.path.isfile(PILOT_CSV),
                     'legacy 2k slice .vmwork/pilot_ratings.csv is absent; '
                     'the equivalence baseline cannot be reproduced locally '
                     '(do NOT download it — the VM copy is authoritative)')
class LegacyEquivalenceTests(unittest.TestCase):
    """计划书 §5.B 末条：**只验证实现等价，不计为新的推荐效果实验**。

    用旧 **三段 64/16/20** 切分的 train 段（196,614 行）构图，对照
    ``fix14`` 的 k=100/beta=0：

    * 1930 个评测用户推荐 **ID / 次序完全一致**；
    * 分数差 ≤ **1e-9**；
    * NDCG@20 / Recall@20 差 ≤ **1e-10**。

    **必须用旧三段切分**，不能和本轮 80/20 混淆。
    与 ``equiv_check.py`` 的区别：这里构图走**被交付的
    ``full_train.build_graph_arrays``**（这是本用例的目的——验证新实现的
    等价性），打分/比对仍复用 ``equiv_check`` 的独立实现。
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(FIX14_DIR):
            raise unittest.SkipTest('frozen fix14 baseline directory is absent: %s'
                                    % FIX14_DIR)
        cls.rows = cls._load_rows()
        cls.train, cls.val = cls._split(cls.rows)
        cls.X, cls.item_ids, cls.user_ids = cls._matrix(cls.train)
        cls.n_like = np.asarray(cls.X.sum(axis=0)).ravel().astype(np.int64)

    @classmethod
    def _load_rows(cls):
        from experiments.itemcf import equiv_check as E
        return E.load_rows_csv(PILOT_CSV)

    @classmethod
    def _split(cls, rows):
        from experiments.itemcf import equiv_check as E
        return E.temporal_split_two_way(rows, 0.2, 0.2)

    @classmethod
    def _matrix(cls, train):
        from experiments.itemcf import equiv_check as E
        return E.build_positive_matrix(train)

    def test_pilot_input_matches_the_frozen_md5(self):
        digest = hashlib.md5()
        with open(PILOT_CSV, 'rb') as handle:
            while True:
                chunk = handle.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
        self.assertEqual(digest.hexdigest(), PILOT_MD5,
                         'the local 2k slice must be the same file that produced '
                         'the frozen fix14 baseline')

    def test_legacy_three_way_split_row_counts_are_frozen(self):
        self.assertEqual(len(self.train), EQUIV_TRAIN_ROWS,
                         'train rows must be the frozen 196,614')
        self.assertEqual(len(self.item_ids), EQUIV_ITEMS,
                         '|I_train| must be the frozen 11,380')
        self.assertEqual(self.X.shape[0], 2000)
        self.assertEqual(self.X.nnz, 101845)

    def test_new_implementation_reproduces_frozen_fix14_recommendations(self):
        from experiments.itemcf import equiv_check as E

        arrays = F.build_graph_arrays(self.X, self.item_ids, beta=0.0, max_k=100,
                                      block_size=128)
        graph = scoring_graph(arrays, self.item_ids)
        # 图规模必须与冻结记录一致
        self.assertEqual(len(arrays['sources']), EQUIV_GRAPH_EDGES,
                         'graph edges must equal fix14 n_edges = %d'
                         % EQUIV_GRAPH_EDGES)
        self.assertEqual(len(graph), 7642)

        relevant = {}
        for user, item, rating, _ts in self.val:
            if rating >= 4.0:
                relevant.setdefault(user, set()).add(item)
        eval_users = sorted(user for user, rel in relevant.items() if rel)
        self.assertEqual(len(eval_users), EQUIV_EVAL_USERS,
                         'expected the frozen 1,930 evaluable users')

        mine = E.score_users(graph, self.train, eval_users)
        baseline_recs, baseline_per_user, hashes = E.load_baseline(FIX14_DIR)

        frozen_problems, matched = E.check_frozen_hashes(hashes)
        self.assertEqual(frozen_problems, [],
                         'baseline files must match a frozen known vintage')
        self.assertIn(matched['recommendations.csv'],
                      ('local_mirror_0314',))

        report = E.compare(mine, baseline_recs, baseline_per_user)
        self.assertEqual(report['missing_users'], [])
        self.assertEqual(report['id_order_mismatches'], [],
                         '推荐 ID/次序必须 1930/1930 完全一致')
        self.assertEqual(report['score_mismatches'], [],
                         '分数差必须 <= 1e-9')
        self.assertEqual(report['metric_mismatches'], [],
                         'NDCG/Recall 差必须 <= 1e-10')
        self.assertLessEqual(report['max_score_gap'], EQUIV_SCORE_TOLERANCE)
        self.assertLessEqual(report['max_ndcg_gap'], EQUIV_METRIC_TOLERANCE)
        self.assertLessEqual(report['max_recall_gap'], EQUIV_METRIC_TOLERANCE)
        self.assertEqual(report['users_mine'], EQUIV_EVAL_USERS)
        self.assertEqual(report['users_baseline'], EQUIV_EVAL_USERS)
        self.assertTrue(report['pass'])

    def test_equivalence_is_insensitive_to_block_size(self):
        """对拍结论必须与分块无关（16/64/128 三档都复现 fix14）。"""
        from experiments.itemcf import equiv_check as E

        baseline_recs, baseline_per_user, _h = E.load_baseline(FIX14_DIR)
        relevant = {}
        for user, item, rating, _ts in self.val:
            if rating >= 4.0:
                relevant.setdefault(user, set()).add(item)
        eval_users = sorted(user for user, rel in relevant.items() if rel)
        for block_size in (16, 64, 128):
            arrays = F.build_graph_arrays(self.X, self.item_ids, beta=0.0,
                                          max_k=100, block_size=block_size)
            graph = scoring_graph(arrays, self.item_ids)
            mine = E.score_users(graph, self.train, eval_users)
            report = E.compare(mine, baseline_recs, baseline_per_user)
            self.assertTrue(report['pass'],
                            'block_size=%d broke equivalence: %r'
                            % (block_size,
                               {'id': len(report['id_order_mismatches']),
                                'score': len(report['score_mismatches']),
                                'metric': len(report['metric_mismatches'])}))
            self.assertLessEqual(report['max_score_gap'], EQUIV_SCORE_TOLERANCE)

    def test_top_k_is_full_at_k_equals_max_k(self):
        """fix14 的 k=100 == max_k=100（取全部已选邻居），本用例记录这一口径。

        ``build_graph.py`` 那个"按目标 ID 写盘、读侧当相似度前缀"的陷阱在
        k == max_k 时不影响结果（FROZEN_BASELINE.md §7 已实证）——这里再把
        "每个来源的邻居数不超过 100" 钉一遍。
        """
        arrays = F.build_graph_arrays(self.X, self.item_ids, beta=0.0, max_k=100,
                                      block_size=128)
        counts = np.diff(arrays['indptr'])
        self.assertLessEqual(int(counts.max()), 100)
        model = F.arrays_to_model(arrays, self.item_ids, self.n_like,
                                  self.n_like.copy())
        problems, stats = M.validate_arrays(model, max_k=100)
        self.assertEqual(problems, [])
        self.assertEqual(stats['n_edges'], EQUIV_GRAPH_EDGES)
        self.assertEqual(stats['n_sources_with_neighbors'], 7642)


# ---------------------------------------------------------------------------
# 12. 终态落盘（回归缺陷 2）
# ---------------------------------------------------------------------------

class _ArgsStub(object):
    """``finalize`` 需要的最小 args 形状（不跑 CLI，只测落盘）。"""

    def __init__(self, **overrides):
        args = F.parse_args(['--input', 'unused-ratings.csv', '--out', 'unused'])
        self.__dict__.update(vars(args))
        self.__dict__.update(overrides)


class FinalizeContractTests(unittest.TestCase):
    """``finalize`` 是所有路径的唯一出口，必须**永远**能落终态文件。

    回归缺陷 2：它曾用 ``sparse.__version__``，而 ``scipy.sparse`` 不暴露该属性
    （scipy 1.13.1 实测 ``hasattr(scipy.sparse, '__version__') is False``）→
    ``AttributeError``，于是 ``run_status.json`` / ``manifest.json`` /
    ``source_hashes.json`` 一个都不落盘，连"为什么失败"都留不下。这条契约
    对 resource_blocked / correctness_failed 这类**失败路径**尤其重要。
    """

    def _run_finalize(self, status, failure_reason):
        work = fixture_dir('finalize')
        input_path = write_ratings(os.path.join(work, 'ratings.csv'), [
            (1, 10, 5.0, 100), (1, 20, 4.0, 101), (2, 10, 4.0, 200)])
        out_dir = os.path.join(work, '20261004T120000Z-aaaabbbb')
        os.makedirs(out_dir)
        logger = F.RunLogger(os.path.join(out_dir, 'run.log'))
        args = _ArgsStub(input=input_path, out=out_dir)
        index = F.stream_input_pass1(input_path)
        run_status = F.finalize(out_dir, logger, logger, None, index, args,
                                '20261004T120000Z-aaaabbbb', status,
                                failure_reason, time.time(), {})
        # 同时返回 input_path：下面要断言 manifest/source_hashes 记的输入指纹
        # 与真实文件一致（原先漏了返回值，测试里引用了未定义变量）。
        return out_dir, run_status, input_path

    def test_finalize_writes_every_terminal_artifact(self):
        import scipy

        status = F.STATUS_RESOURCE_BLOCKED
        out_dir, run_status, input_path = self._run_finalize(
            status, 'precheck did not pass')
        for name in ('run_status.json', 'manifest.json', 'source_hashes.json'):
            self.assertTrue(os.path.isfile(os.path.join(out_dir, name)),
                            'finalize must always write %s' % name)
        self.assertEqual(run_status['status'], status)
        self.assertEqual(run_status['failure_reason'], 'precheck did not pass')

        # run_status.json：身份、状态、失败原因、计数
        on_disk = M.read_json(os.path.join(out_dir, 'run_status.json'))
        self.assertEqual(on_disk['status'], status)
        self.assertEqual(on_disk['run_id'], '20261004T120000Z-aaaabbbb')
        self.assertEqual(on_disk['model_id'], None,
                         'no model was saved -> model_id must stay None')
        self.assertEqual(on_disk['counts']['input_rows'], 3)
        self.assertEqual(on_disk['counts']['users'], 2)
        self.assertEqual(on_disk['counts']['items'], 2)
        # 监督口径：worker 里**没有** guard 对象，采样在父进程
        self.assertEqual(on_disk['supervision']['mode'], 'separate_process')
        self.assertIn('parent', on_disk['supervision']['guard_owner'])
        self.assertIn('duplicate_check', on_disk)
        self.assertTrue(on_disk['duplicate_check']['pass'])
        self.assertEqual(on_disk['duplicate_check']['duplicate_occurrences'], 0)

        # manifest.json：环境指纹 + scipy 版本（缺陷 2 回归断言）
        manifest = M.read_json(os.path.join(out_dir, 'manifest.json'))
        self.assertEqual(manifest['run_id'], '20261004T120000Z-aaaabbbb')
        self.assertEqual(manifest['model_id'], 'model-20261004T120000Z-aaaabbbb')
        self.assertEqual(manifest['stage'], 'full_train')
        self.assertIsNotNone(manifest['scipy'],
                             'manifest["scipy"] must record a real version')
        self.assertEqual(manifest['scipy'], scipy.__version__)
        self.assertEqual(manifest['numpy'], np.__version__)
        self.assertIn('python', manifest)
        self.assertIn('parameters', manifest)
        self.assertEqual(manifest['parameters']['max_k'], 100)
        self.assertEqual(manifest['parameters']['beta'], 0.0)
        self.assertEqual(manifest['counts']['input_rows'], 3)
        self.assertIn('input_sha256', manifest)
        self.assertEqual(manifest['input_sha256'],
                         F.file_sha256(input_path))

        # source_hashes.json：源码指纹（"源码未漂移"核对依据）
        hashes = M.read_json(os.path.join(out_dir, 'source_hashes.json'))
        for name in ('full_train.py', 'model_io.py', 'resource_guard.py'):
            self.assertIn(name, hashes['sources'])
            self.assertRegex(hashes['sources'][name], r'^[0-9a-f]{64}$')
        self.assertEqual(hashes['input_sha256'], F.file_sha256(input_path))

    def test_finalize_records_correctness_failure_with_reason(self):
        status = F.STATUS_CORRECTNESS_FAILED
        out_dir, run_status, _input_path = self._run_finalize(
            status, 'CorrectnessError: boom')
        self.assertEqual(run_status['status'], status)
        on_disk = M.read_json(os.path.join(out_dir, 'run_status.json'))
        self.assertEqual(on_disk['status'], status)
        self.assertIn('boom', on_disk['failure_reason'])
        self.assertTrue(os.path.isfile(os.path.join(out_dir, 'manifest.json')))

    def test_finalize_tolerates_missing_index(self):
        """``index`` 可能是 None（prepare 阶段就崩了），也必须能落终态。"""
        work = fixture_dir('finalize-noindex')
        input_path = write_ratings(os.path.join(work, 'ratings.csv'),
                                   [(1, 10, 5.0, 100)])
        out_dir = os.path.join(work, '20261004T120000Z-ccccdddd')
        os.makedirs(out_dir)
        logger = F.RunLogger(os.path.join(out_dir, 'run.log'))
        args = _ArgsStub(input=input_path, out=out_dir)
        run_status = F.finalize(out_dir, logger, logger, None, None, args,
                                '20261004T120000Z-ccccdddd',
                                F.STATUS_RESOURCE_FAILED, 'guard tripped',
                                time.time(), {})
        self.assertEqual(run_status['status'], F.STATUS_RESOURCE_FAILED)
        self.assertEqual(run_status['counts'], {'graph_edges': None,
                                                'graph_sources': None})
        on_disk = M.read_json(os.path.join(out_dir, 'run_status.json'))
        self.assertEqual(on_disk['failure_reason'], 'guard tripped')
        self.assertTrue(os.path.isfile(os.path.join(out_dir, 'manifest.json')))

    def test_status_values_come_from_the_guard_module(self):
        """计划书 §6：状态只取明确值，且**复用** resource_guard 的常量。"""
        from experiments.itemcf import resource_guard as G

        self.assertEqual(F.STATUS_SUCCESS, G.STATUS_SUCCESS)
        self.assertEqual(F.STATUS_RESOURCE_BLOCKED, G.STATUS_RESOURCE_BLOCKED)
        self.assertEqual(F.STATUS_RESOURCE_FAILED, G.STATUS_RESOURCE_FAILED)
        self.assertEqual(F.STATUS_MONITOR_FAILED, G.STATUS_MONITOR_FAILED)
        self.assertEqual(F.STATUS_CORRECTNESS_FAILED, G.STATUS_CORRECTNESS_FAILED)
        for status in (F.STATUS_RUNNING, F.STATUS_SUCCESS,
                       F.STATUS_RESOURCE_BLOCKED, F.STATUS_RESOURCE_FAILED,
                       F.STATUS_MONITOR_FAILED, F.STATUS_CORRECTNESS_FAILED):
            self.assertIsInstance(status, str)
            self.assertTrue(status)

    def test_reconcile_status_lets_supervision_override_worker(self):
        """监督的终止判定 > worker 自报（上轮缺的那道保险）。"""
        reconcile = F._reconcile_status
        # worker 自报 success，但监督说资源超限 → 以监督为准
        self.assertEqual(
            reconcile({'status': F.STATUS_RESOURCE_FAILED, 'exit_code': 0},
                      {'status': F.STATUS_SUCCESS}),
            F.STATUS_RESOURCE_FAILED)
        self.assertEqual(
            reconcile({'status': F.STATUS_MONITOR_FAILED, 'exit_code': 0},
                      {'status': F.STATUS_SUCCESS}),
            F.STATUS_MONITOR_FAILED)
        # 监督无判定 → 用 worker 自报
        self.assertEqual(
            reconcile({'status': F.STATUS_RUNNING, 'exit_code': 0},
                      {'status': F.STATUS_CORRECTNESS_FAILED}),
            F.STATUS_CORRECTNESS_FAILED)
        # 都没有 → 看退出码
        self.assertEqual(reconcile({'status': None, 'exit_code': 0}, None),
                         F.STATUS_SUCCESS)
        self.assertEqual(reconcile({'status': None, 'exit_code': 1}, None),
                         F.STATUS_CORRECTNESS_FAILED)

    def test_python36_compatibility_of_the_new_modules(self):
        """计划书："Python 3.6 兼容"——3.6 上不可用的语法一律不得出现。

        本地是 3.12，跑不了 3.6，所以这里做**源码级**检查：按 AST 判断（而不
        是搜字符串，否则注释里写"不用 dataclasses"就会被误判）。检查项：
        f-string / walrus（``:=``）/ ``from __future__ import annotations`` /
        ``dataclasses``。
        """
        import ast

        for name in ('full_train.py', 'model_io.py', 'verify_full_model.py'):
            path = os.path.join(PROJECT, 'experiments', 'itemcf', name)
            with open(path, encoding='utf-8') as handle:
                source = handle.read()
            tree = ast.parse(source, filename=name)

            # 1. f-string：3.6 支持 f-string，但本项目的 3.6 目标环境用的是
            #    "不用 f-string" 的更严格约定（与既有模块一致），故按约定检查。
            joined = ast.JoinedStr
            bad_fstrings = [node.lineno for node in ast.walk(tree)
                            if isinstance(node, joined)]
            self.assertEqual(bad_fstrings, [],
                             '%s uses f-strings at lines %r' % (name, bad_fstrings))

            # 2. walrus（3.8+）
            walrus = [node.lineno for node in ast.walk(tree)
                      if node.__class__.__name__ == 'NamedExpr']
            self.assertEqual(walrus, [],
                             '%s uses the walrus operator at lines %r'
                             % (name, walrus))

            # 3. from __future__ import annotations（3.7+ 才有实际效果）
            future_annotations = []
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                    for alias in node.names:
                        if alias.name == 'annotations':
                            future_annotations.append(node.lineno)
            self.assertEqual(future_annotations, [],
                             '%s imports annotations from __future__' % name)

            # 4. dataclasses 装饰器（3.7+）
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                    for decorator in node.decorator_list:
                        text = ast.dump(decorator)
                        self.assertNotIn('dataclass', text,
                                         '%s uses @dataclass on %s'
                                         % (name, node.name))

            # 5. 3.6 的 dict 保序是 CPython 实现细节，本项目**依赖**字面量顺序时
            #    必须显式用 OrderedDict 或排序——这里只确认模块可被本机解析。
            self.assertIsInstance(tree, ast.Module)


# ---------------------------------------------------------------------------
# 13. 不能本地跑的用例：明确 skip 并给原因
# ---------------------------------------------------------------------------

class VMDependentTests(unittest.TestCase):
    """依赖 VM 路径 / 原始 32M CSV 的检查**不能**在本地跑。

    这些用例**必须显示为 skip 并带原因**——静默通过或假装成功是禁止的。
    """

    def test_full_32m_input_is_not_available_locally(self):
        self.skipTest(
            'the full MovieLens-32M ratings.csv (32,000,204 rows / 200,948 users) '
            'lives only on the VM at /home/h/movielens/ratings.csv; it is not in '
            'this workspace and must not be downloaded. Full-scale construction, '
            'resource precheck and the 4h training stage are task-7 (Lead), not a '
            'local unit test.')

    def test_supervised_orchestrator_end_to_end_is_out_of_scope(self):
        self.skipTest(
            'run() spawns a GuardedRun/ResourceGuard supervised worker and needs '
            'a real subprocess tree plus stage budgets; that belongs to the VM '
            'smoke run (task-7). The worker-side contracts it depends on '
            '(terminal artifacts, id consistency, status reconciliation) are '
            'covered here via FinalizeContractTests, and the guard itself is '
            'covered by tests/test_itemcf_resource_guard.py.')

    def test_vm_python36_and_scipy154_compatibility_is_not_runnable_locally(self):
        self.skipTest(
            'the deliverable must run on the VM interpreter '
            '/home/h/venvs/itemcf/bin/python = Python 3.6.8 / NumPy 1.19.5 / '
            'SciPy 1.5.4. This workspace has Python 3.12 / NumPy 2.2.6 / '
            'SciPy 1.13.1, so 3.6-only behaviour (e.g. dict ordering guarantees, '
            'np.fromfile semantics on old NumPy) cannot be exercised here. '
            'Source-level 3.6 syntax is checked in '
            'FinalizeContractTests.test_python36_compatibility_of_the_new_modules.')

    def test_guard_threshold_reachability_on_the_vm(self):
        self.skipTest(
            'proving the 3 GiB RSS termination line actually trips during '
            'full-scale construction requires the VM (3932 MB RAM) and the real '
            '32M matrix. Locally we only test the guard mechanism with injected '
            'readings and small thresholds '
            '(tests/test_itemcf_resource_guard.py).')


if __name__ == '__main__':
    unittest.main()
