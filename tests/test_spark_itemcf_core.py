# -*- coding: utf-8 -*-
"""spark/utils/itemcf_math 的本地单测（无 PySpark 依赖，本机 Python 3.12 可跑）。

核心断言：纯函数组合出的邻居图与权威参考实现
``experiments/itemcf/core.build_neighbors``（scipy 路径）**逐位一致**——
共现计数（整数，精确）、相似度（同一条 IEEE-754 double 公式的同序求值，
逐位相等）、Top-K（排序键 (-sim, target movieId) 与截断规则一致）。

覆盖：
1. 小合成数据集（5 用户 / 8 部电影 / 手工构造共现）上与 core 的全图对拍；
2. beta 参数改变相似度（含精确分数断言与邻居顺序翻转场景）；
3. 边界：无共现的物品对不出边、零好评物品不出边、Top-K 截断
   （邻居数 <= k）、真正平票时 target movieId 升序打破。
"""

import os
import sys
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for candidate in (str(PROJECT_ROOT), str(PROJECT_ROOT / "spark")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from experiments.itemcf import core  # noqa: E402
from utils import itemcf_math  # noqa: E402


#: 小合成数据集：(userId, movieId, rating, timestamp)。
#: 手工构造的共现：
#:   c(101,102)=2  c(101,103)=2  c(101,104)=2  c(102,103)=2  c(102,104)=1
#:   c(102,105)=1  c(103,104)=1  c(103,105)=1  c(105,106)=1  c(105,107)=1
#:   c(106,107)=1；104 与 105 无共现；108 只有低分（零好评物品）。
#: n_like：101=3, 102=3, 103=3, 104=2, 105=2, 106=1, 107=1, 108=0。
ROWS = [
    (1, 101, 5.0, 1000),
    (1, 102, 5.0, 1001),
    (1, 103, 5.0, 1002),
    (1, 107, 3.0, 1003),   # 低分：进物品全集与 Seen，但不进好评
    (2, 101, 5.0, 1004),
    (2, 102, 4.0, 1005),
    (2, 104, 5.0, 1006),
    (3, 102, 4.5, 1007),
    (3, 103, 5.0, 1008),
    (3, 105, 4.0, 1009),
    (4, 101, 4.0, 1010),
    (4, 103, 4.0, 1011),
    (4, 104, 4.0, 1012),
    (4, 106, 2.0, 1013),
    (5, 105, 5.0, 1014),
    (5, 106, 5.0, 1015),
    (5, 107, 4.0, 1016),
    (6, 108, 2.5, 1017),   # 零好评物品（rating < 4.0 且无人好评）
]

#: 顺序翻转场景：u1 -> {201, 202}；u2/u3 -> {201, 203}；u4/u5 -> {203}。
#: n_like：201=3, 202=1, 203=4；c(201,202)=1（cos=1/sqrt(3)）、c(201,203)=2
#: （cos=2/sqrt(12)=1/sqrt(3)）——**cos 数值相同**。beta=0 时平票、按
#: movieId 取 202 在前；beta=20 时收缩反转：202 -> 0.577*1/21=0.0275，
#: 203 -> 0.577*2/22=0.0525，203 反超排前（收缩偏袒高共现边）。
FLIP_ROWS = [
    (1, 201, 5.0, 2000),
    (1, 202, 5.0, 2001),
    (2, 201, 5.0, 2002),
    (2, 203, 5.0, 2003),
    (3, 201, 5.0, 2004),
    (3, 203, 5.0, 2005),
    (4, 203, 5.0, 2006),
    (5, 203, 5.0, 2007),
]


def reference_graph(rows, beta, max_k):
    """用权威参考实现（core）构造邻居图 dict。"""
    matrix, item_ids, _user_ids = core.build_positive_matrix(rows)
    return core.build_neighbors(matrix, item_ids, beta, max_k)


def targets_of(graph, source_id):
    """按契约序（-sim 升序 = 相似度降序，平票 target movieId 升序）取某来源的邻居目标。

    不依赖 dict 插入顺序：直接按排序键重排，与 core 的逐来源
    ``sorted(key=(-sim, target_movieId))`` 语义一致。
    """
    edges = [(key[1], value[1]) for key, value in graph.items()
             if key[0] == source_id]
    edges.sort(key=lambda edge: (-edge[1], edge[0]))
    return [target for target, _sim in edges]


class ItemCFMathVsCoreTests(unittest.TestCase):
    """与 core.build_neighbors 的逐位对拍（多个 beta / max_k 组合）。"""

    def test_graph_matches_core_reference(self):
        for beta in (0.0, 2.5):
            for max_k in (100, 2, 0):
                with self.subTest(beta=beta, max_k=max_k):
                    expected = reference_graph(ROWS, beta, max_k)
                    actual = itemcf_math.build_neighbors_from_rows(
                        ROWS, beta, max_k)
                    self.assertEqual(actual, expected)

    def test_cooccurrence_counts_match_matrix_semantics(self):
        """cooccurrence_counts 与 core 的 X.T @ X 共现口径一致（整数精确）。"""
        matrix, item_ids, _ = core.build_positive_matrix(ROWS)
        index = dict((movie_id, pos) for pos, movie_id in enumerate(item_ids))
        counts = itemcf_math.cooccurrence_counts(
            [set(int(r[1]) for r in ROWS
                 if r[0] == user and float(r[2]) >= 4.0)
             for user in sorted(set(int(r[0]) for r in ROWS))])
        # 抽查全部已知非零对与若干应为零的对（逐元素乘积求和 = 共现计数）。
        for (i, j) in ((101, 102), (101, 104), (102, 105), (105, 107)):
            expected = int(
                matrix[:, index[i]].multiply(matrix[:, index[j]]).sum())
            self.assertEqual(counts[(i, j)], expected)
            self.assertEqual(counts[(j, i)], expected)
        for (i, j) in ((104, 105), (101, 105), (101, 106), (103, 106),
                       (107, 101)):
            self.assertNotIn((i, j), counts)


class BetaParameterTests(unittest.TestCase):
    """beta 改变相似度值（精确分数）与邻居顺序。"""

    def test_beta_zero_disables_shrinkage(self):
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 100)
        # c=2, n_like=3,3 -> cos = 2/3，beta=0 无收缩。
        self.assertEqual(graph[(101, 102)][1], 2.0 / 3.0)

    def test_beta_shrinks_similarity(self):
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 2.0, 100)
        # c=2, n=3,3 -> (2/3) * (2/(2+2)) = 1/3。
        self.assertEqual(graph[(101, 102)][1], 1.0 / 3.0)

    def test_beta_flips_neighbor_order(self):
        base = itemcf_math.build_neighbors_from_rows(FLIP_ROWS, 0.0, 100)
        shrunk = itemcf_math.build_neighbors_from_rows(FLIP_ROWS, 20.0, 100)
        self.assertEqual(targets_of(base, 201), [202, 203])
        self.assertEqual(targets_of(shrunk, 201), [203, 202])
        # 收缩是单调的：c 越大收缩越轻，逐边 sim 不会变大。
        for key in ((201, 202), (201, 203)):
            self.assertLess(shrunk[key][1], base[key][1])

    def test_invalid_beta_rejected(self):
        for bad in (-1.0, float('inf'), float('nan'), 'x'):
            with self.assertRaises(ValueError):
                itemcf_math.validate_beta(bad)
            with self.assertRaises(ValueError):
                itemcf_math.similarity_from_counts(
                    np.array([1], dtype=np.int64),
                    np.array([1], dtype=np.int64),
                    np.array([1], dtype=np.int64), bad)


class BoundaryTests(unittest.TestCase):
    """边界：无共现不出边、零好评不出边、Top-K 截断、平票打破。"""

    def test_pairs_without_cooccurrence_have_no_edge(self):
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 100)
        for source, target in ((104, 105), (101, 105), (101, 106),
                               (103, 106), (106, 101), (107, 101)):
            self.assertNotIn((source, target), graph)

    def test_zero_like_item_has_no_edges(self):
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 100)
        self.assertEqual(len(graph), 22)
        for source, target in graph:
            self.assertNotEqual(source, 108)
            self.assertNotEqual(target, 108)

    def test_top_k_truncation_keeps_best_neighbors(self):
        # 来源 103 的候选（beta=0）：101 -> 2/3, 102 -> 2/3（平票）,
        # 104 -> 1/sqrt(6), 105 -> 1/sqrt(6)（平票）。
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 3)
        self.assertEqual(targets_of(graph, 103), [101, 102, 104])
        self.assertLessEqual(len(targets_of(graph, 103)), 3)
        graph1 = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 1)
        self.assertEqual(targets_of(graph1, 103), [101])
        # 全图邻居数不超过 k。
        graph2 = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 2)
        for source in (101, 102, 103, 104, 105, 106, 107):
            self.assertLessEqual(len(targets_of(graph2, source)), 2)

    def test_ties_break_by_ascending_movie_id(self):
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 100)
        # (103,101) 与 (103,102) 相似度真正相等（同 c 同 n_like）。
        self.assertEqual(graph[(103, 101)][1], graph[(103, 102)][1])
        # 高共现边 (103,101) 的相似度严格大于低共现边 (103,104)。
        self.assertGreater(graph[(103, 101)][1], graph[(103, 104)][1])
        self.assertEqual(targets_of(graph, 103)[:2], [101, 102])

    def test_neighbor_direction_is_directed(self):
        """图是有向的：c 对称但边按来源分别落位，且两侧相似度相等。"""
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 100)
        self.assertIn((101, 102), graph)
        self.assertIn((102, 101), graph)
        self.assertEqual(graph[(101, 102)][1], graph[(102, 101)][1])
        self.assertEqual(graph[(101, 102)][2], graph[(102, 101)][2])

    def test_edge_payload_fields(self):
        graph = itemcf_math.build_neighbors_from_rows(ROWS, 0.0, 100)
        target, sim, c_ij, n_like_j, n_like_i = graph[(101, 104)]
        self.assertEqual(target, 104)
        self.assertEqual(c_ij, 2)
        self.assertEqual(n_like_j, 3)
        self.assertEqual(n_like_i, 2)
        self.assertEqual(sim, 2.0 / np.sqrt(3 * 2))


class UnitFunctionTests(unittest.TestCase):
    """底层纯函数的直接行为与异常契约。"""

    def test_rank_and_truncate_sorts_and_truncates(self):
        sources = np.array([7, 7, 7, 5], dtype=np.int64)
        targets = np.array([30, 10, 20, 40], dtype=np.int64)
        sims = np.array([0.1, 0.5, 0.5, 0.9], dtype=np.float64)
        coocs = np.array([1, 2, 2, 3], dtype=np.int64)
        out_s, out_t, out_sim, out_c = itemcf_math.rank_and_truncate(
            sources, targets, sims, coocs, 2)
        # 来源 7：0.5 平票 -> 目标 10 在 20 前；截断后只剩 2 条；0.1 被挤掉。
        np.testing.assert_array_equal(out_s, [5, 7, 7])
        np.testing.assert_array_equal(out_t, [40, 10, 20])
        np.testing.assert_allclose(out_sim, [0.9, 0.5, 0.5])
        np.testing.assert_array_equal(out_c, [3, 2, 2])

    def test_rank_and_truncate_rejects_bad_max_k(self):
        with self.assertRaises(ValueError):
            itemcf_math.rank_and_truncate(
                np.zeros(1, np.int64), np.zeros(1, np.int64),
                np.zeros(1, np.float64), np.zeros(1, np.int64), -1)

    def test_similarity_from_counts_validates(self):
        sims = itemcf_math.similarity_from_counts(
            np.array([2], np.int64), np.array([3], np.int64),
            np.array([3], np.int64), 0.0)
        self.assertEqual(sims[0], 2.0 / 3.0)
        with self.assertRaises(ValueError):
            itemcf_math.similarity_from_counts(
                np.array([2], np.int64), np.array([0], np.int64),
                np.array([3], np.int64), 0.0)  # n_src=0 -> 除零 -> 非有限 -> 拒绝

    def test_build_indptr_semantics(self):
        sources = np.array([0, 0, 2, 2, 2], dtype=np.int64)
        indptr = itemcf_math.build_indptr(sources, 4)
        np.testing.assert_array_equal(indptr, [0, 2, 2, 5, 5])
        self.assertEqual(len(indptr), 5)
        self.assertEqual(int(indptr[0]), 0)
        self.assertEqual(int(indptr[-1]), 5)

    def test_build_indptr_empty(self):
        indptr = itemcf_math.build_indptr(np.zeros(0, np.int64), 3)
        np.testing.assert_array_equal(indptr, [0, 0, 0, 0])


if __name__ == '__main__':
    unittest.main()
