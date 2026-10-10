"""experiments.itemcf.core 的正确性测试（计划书第 7.1/7.2 节）。

按 TDD 先行：本文件先于 core.py 编写，断言均直接来自计划书第 7.1 节
手算金标准（u1={A,B}、u2={A,C}、u3={A,B,C}）与第 7.2 节验收表：
共现计数 / 相似度边界 / 截断顺序 / 邻居方向 / k 和并列 / 分块对拍 /
用户打分 / 证据解释。运行：
    backend/venv/Scripts/python.exe -m unittest tests.test_itemcf_core -v
"""
import os
import sys
import unittest

import numpy as np
from scipy import sparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf.core import (  # noqa: E402
    build_neighbors,
    build_positive_matrix,
    score_user,
)


def _cos_ab():  # 2 / sqrt(3*2) = 2/sqrt(6)
    return 2.0 / np.sqrt(6.0)


def _gold_rows():
    """计划书 7.1 节金标准 + 一条低分记录 + 零好评物品 + 非连续 movieId。"""
    # u1={A,B} u2={A,C} u3={A,B,C}，movieId: A=2, B=10, C=5（非连续），
    # D=42 零好评（仅出现过 2.0 分），E=100 从未出现。
    return [
        (1, 2, 5.0, 100),   # u1 likes A
        (1, 10, 4.5, 110),  # u1 likes B
        (1, 42, 2.0, 120),  # u1 rates D low -> seen but not liked
        (2, 2, 4.0, 130),   # u2 likes A
        (2, 5, 5.0, 140),   # u2 likes C
        (3, 2, 5.0, 150),   # u3 likes A
        (3, 10, 4.0, 160),  # u3 likes B
        (3, 5, 4.5, 170),   # u3 likes C
    ]


class PositiveMatrixTests(unittest.TestCase):
    """共现计数前置：二值好评矩阵与支持度。"""

    def test_gold_n_like_and_mappings(self):
        X, item_ids, user_ids = build_positive_matrix(_gold_rows())
        self.assertEqual(item_ids, [2, 5, 10, 42])
        self.assertEqual(user_ids, [1, 2, 3])
        self.assertEqual(X.dtype, np.int64)  # 禁止 uint8/bool 溢出
        n_like = np.asarray(X.sum(axis=0)).ravel()
        by_id = dict(zip(item_ids, n_like.tolist()))
        self.assertEqual(by_id, {2: 3, 5: 2, 10: 2, 42: 0})

    def test_low_rating_does_not_like_but_stays_seen(self):
        X, item_ids, user_ids = build_positive_matrix(_gold_rows())
        col = item_ids.index(42)
        row = user_ids.index(1)
        self.assertEqual(X[row, col], 0)  # 2.0 分不进相似度矩阵

    def test_duplicate_pair_rejected(self):
        rows = _gold_rows() + [(1, 2, 5.0, 180)]
        with self.assertRaises(ValueError):
            build_positive_matrix(rows)

    def test_260_co_likers_no_uint8_overflow(self):
        n = 260
        rows = [(u, 1, 5.0, u) for u in range(1, n + 1)]
        rows += [(u, 2, 5.0, u) for u in range(1, n + 1)]
        X, item_ids, _ = build_positive_matrix(rows)
        j = item_ids.index(1)
        block = sparse.csc_matrix(X[:, [j]].T @ X)  # 与 build_neighbors 同一算子
        self.assertEqual(int(block[0, item_ids.index(2)]), 260)

    def test_empty_train_ok(self):
        X, item_ids, user_ids = build_positive_matrix([])
        self.assertEqual(item_ids, [])
        self.assertEqual(user_ids, [])
        self.assertEqual(X.shape, (0, 0))


class SimilarityBoundaryTests(unittest.TestCase):
    """相似度边界：自相似 / 无共现 / 零好评列 / beta=0 / 有限性与取值范围。"""

    def setUp(self):
        self.X, self.item_ids, self.user_ids = build_positive_matrix(_gold_rows())

    def _edge(self, neighbors, a, b):
        """返回 (source, target) 边的相似度 s，无该边返回 None。"""
        entry = neighbors.get((a, b))
        return None if entry is None else entry[1]

    def test_gold_similarity_values_beta10(self):
        g = build_neighbors(self.X, self.item_ids, beta=10.0, max_k=100)
        s_ab = (2.0 / 12.0) * _cos_ab()      # (2/12)*(2/sqrt(6))
        s_bc = (1.0 / 11.0) * 0.5            # (1/11)*(1/2)
        self.assertAlmostEqual(self._edge(g, 2, 10), s_ab, places=12)
        self.assertAlmostEqual(self._edge(g, 2, 5), s_ab, places=12)
        self.assertAlmostEqual(self._edge(g, 10, 5), s_bc, places=12)

    def test_gold_c_and_n_like_evidence(self):
        g = build_neighbors(self.X, self.item_ids, beta=10.0, max_k=100)
        # (source, target) -> (target, s, c, n_like_source, n_like_target)
        self.assertEqual(g[(2, 10)][2], 2)   # c_AB = 2
        self.assertEqual(g[(2, 10)][3], 3)   # n_like(A) = 3
        self.assertEqual(g[(2, 10)][4], 2)   # n_like(B) = 2
        self.assertEqual(g[(10, 5)][2], 1)   # c_BC = 1

    def test_no_self_similarity_and_zero_cooccurrence(self):
        g = build_neighbors(self.X, self.item_ids, beta=10.0, max_k=100)
        self.assertNotIn((2, 2), g)          # 对角线剔除
        self.assertIsNone(self._edge(g, 2, 42))  # 零好评物品无共现

    def test_beta0_unshrunk(self):
        g = build_neighbors(self.X, self.item_ids, beta=0.0, max_k=100)
        self.assertAlmostEqual(self._edge(g, 2, 10), _cos_ab(), places=12)
        self.assertAlmostEqual(self._edge(g, 10, 5), 0.5, places=12)

    def test_similarity_range_and_finiteness(self):
        for beta in (0.0, 10.0, 50.0):
            g = build_neighbors(self.X, self.item_ids, beta=beta, max_k=100)
            for (a, b), edge in g.items():
                s = edge[1]  # 值元组 (target, s, c, n_like_j, n_like_i)
                self.assertTrue(np.isfinite(s))
                self.assertGreater(s, 0.0)
                self.assertLessEqual(s, 1.0 + 1e-12)

    def test_invalid_beta_rejected(self):
        with self.assertRaises(ValueError):
            build_neighbors(self.X, self.item_ids, beta=-1.0, max_k=10)
        with self.assertRaises(ValueError):
            build_neighbors(self.X, self.item_ids, beta=float('nan'), max_k=10)

    def test_similarity_out_of_range_raises(self):
        # 构造一个 cos>1+1e-12 的输入（人为篡改矩阵一致性）：
        # n_like 与共现不一致时应视为实现异常、抛出而不是静默裁剪。
        X = sparse.csr_matrix(np.array([[1, 1], [1, 1]], dtype=np.int64))
        item_ids = [1, 2]
        class _Bad(object):
            pass
        # 直接用受支持路径无法产生越界相似度；改为断言内部校验函数存在并生效：
        from experiments.itemcf.core import _checked_similarity
        with self.assertRaises(ValueError):
            _checked_similarity(1.5, 1, 2)   # > 1+1e-12
        with self.assertRaises(ValueError):
            _checked_similarity(float('nan'), 1, 2)
        with self.assertRaises(ValueError):
            _checked_similarity(-0.1, 1, 2)


class TruncationOrderTests(unittest.TestCase):
    """截断顺序：先按指定 beta 收缩、再取 top-k。"""

    def test_shrinkage_changes_neighbor_order(self):
        # 验证"先收缩再 top-k"：beta>0 的收缩会改变邻居顺序，不能拿 beta=0 的
        # 邻居集合代替。构造两条边，beta=0 时按 cos 排序为 3 前 2 后，
        # beta=10 收缩后 2 前 3 后（低共现边被收缩得更狠）。
        #
        # 设 n_like(1)=4：
        #   边 (1,2)：c_12=2, n_like(2)=2  -> cos=2/sqrt(4*2)=0.7071
        #   边 (1,3)：c_13=1, n_like(3)=1  -> cos=1/sqrt(4*1)=0.5
        # beta=0:  s_2=0.7071 > s_3=0.5  -> 3 排第 2（顺序 2,3）
        # beta=10: s_2=(2/12)*0.7071=0.1179, s_3=(1/11)*0.5=0.0455 -> 仍 2 前
        # 需要低 c 边 cos 更大才能翻转：把 n_like(3) 调小使 cos_3 > cos_2。
        # 取 n_like(3)=1, n_like(2)=8, c_12=2, c_13=1, n_like(1)=4：
        #   cos_2 = 2/sqrt(4*8)=0.3536, cos_3 = 1/sqrt(4*1)=0.5
        # beta=0:  s_2=0.3536 < s_3=0.5 -> 3 前
        # beta=10: s_2=(2/12)*0.3536=0.0589, s_3=(1/11)*0.5=0.0455 -> 2 前
        rows = []
        # 4 名用户好评 1 -> n_like(1)=4
        for u in (1, 2, 3, 4):
            rows.append((u, 1, 5.0, u))
        # u1,u2 好评 2 且好评 1（共现 c_12=2）；u3..u8 只好评 2 -> n_like(2)=8
        rows.append((1, 2, 5.0, 1))
        rows.append((2, 2, 5.0, 2))
        for u in (5, 6, 7, 8):
            rows.append((u, 2, 5.0, u))
        # u1 好评 3 且好评 1（共现 c_13=1）；n_like(3)=1
        rows.append((1, 3, 5.0, 1))
        X, item_ids, _ = build_positive_matrix(rows)
        g0 = build_neighbors(X, item_ids, beta=0.0, max_k=1)
        g10 = build_neighbors(X, item_ids, beta=10.0, max_k=1)
        self.assertEqual(g0.get((1, 3))[0], 3)   # beta=0：cos 大者 3 前
        self.assertEqual(g0.get((1, 3))[2], 1)   # c_13 = 1
        self.assertEqual(g10.get((1, 2))[0], 2)  # beta=10：收缩后 2 前
        self.assertEqual(g10.get((1, 2))[2], 2)  # c_12 = 2

    def test_beta0_neighbor_set_not_reused_for_other_betas(self):
        rows = [(1, 1, 5.0, 1), (1, 2, 5.0, 1), (2, 1, 5.0, 2), (2, 3, 5.0, 2)]
        X, item_ids, _ = build_positive_matrix(rows)
        g0 = build_neighbors(X, item_ids, beta=0.0, max_k=1)
        g1 = build_neighbors(X, item_ids, beta=1.0, max_k=1)
        self.assertNotEqual(g0, g1)  # 相似度数值不同，不能拿 beta=0 图代替

    def test_beta0_neighbor_set_not_reused_for_other_betas(self):
        rows = [(1, 1, 5.0, 1), (1, 2, 5.0, 1), (2, 1, 5.0, 2), (2, 3, 5.0, 2)]
        X, item_ids, _ = build_positive_matrix(rows)
        g0 = build_neighbors(X, item_ids, beta=0.0, max_k=1)
        g1 = build_neighbors(X, item_ids, beta=1.0, max_k=1)
        self.assertNotEqual(g0, g1)  # 相似度数值不同，不能拿 beta=0 图代替


class NeighborDirectionTests(unittest.TestCase):
    """邻居方向：来源 j -> 目标 i 的单向截断图，打分只沿 j->i 累加。"""

    def _asymmetric_setup(self):
        # 非互惠 top-k：来源 1 与 2、3 共现相同，max_k=1 -> 只留目标 2；
        # 来源 3 只与 1 共现，max_k=1 -> 留目标 1。但打分只从 H_u 的来源出发，
        # 用户只好评 {3} 时走 3->1，不走 1->3（图里根本没有 1->3 这条边）。
        rows = [(1, 1, 5.0, 1), (1, 2, 5.0, 1), (1, 3, 5.0, 1),
                (2, 1, 5.0, 2), (2, 2, 5.0, 2),
                (3, 1, 5.0, 3), (3, 3, 5.0, 3)]
        X, item_ids, _ = build_positive_matrix(rows)
        g = build_neighbors(X, item_ids, beta=0.0, max_k=1)
        return g

    def test_graph_is_directional(self):
        g = self._asymmetric_setup()
        self.assertIn((1, 2), g)     # 来源 1 -> 2 入选（并列中 movieId 小者优先）
        self.assertNotIn((1, 3), g)  # 来源 1 -> 3 被 max_k=1 截断丢弃
        self.assertIn((3, 1), g)     # 来源 3 -> 1 入选（3 的唯一正邻居）

    def test_score_accumulates_source_to_target(self):
        g = self._asymmetric_setup()
        # 用户 u 好评 {1,3}：来源 1 贡献 1->2，来源 3 贡献 3->1；但 1 在 H_u
        # 里会被当作历史排除。2 只有来源 1 的一条贡献。
        # n_like(1)=3, n_like(2)=2, c_12=2 -> cos(1,2)=2/sqrt(3*2)=0.8165。
        recs = score_user(history={1, 3}, neighbors=g, seen=set(), top_n=10)
        by_id = {r['movie_id']: r for r in recs}
        self.assertIn(2, by_id)                 # 来自 1->2
        self.assertNotIn(3, by_id)              # 3 是历史项，被排除
        expected = 2.0 / np.sqrt(3.0 * 2.0)     # beta=0, c_12=2, cos=2/sqrt(6)
        self.assertAlmostEqual(by_id[2]['score'], expected, places=12)

    def test_transposed_graph_not_used(self):
        g = self._asymmetric_setup()
        # 用户只好评 {3}：只有 3->1 这条出边贡献，不能用不存在的入边 1->3 补分。
        recs = score_user(history={3}, neighbors=g, seen=set(), top_n=10)
        self.assertEqual([r['movie_id'] for r in recs], [1])
        self.assertNotIn(2, [r['movie_id'] for r in recs])


class TieAndKTests(unittest.TestCase):
    """k 与并列：正邻居不足 k、并列按 movieId 升序、k 前缀一致。"""

    def _fan_rows(self):
        # 来源 1 与 2,3,4 各共现 1；每个目标 n_like 都=1、来源 n_like=3，
        # cos = 1/sqrt(3*1) 三者完全相同 -> 真并列，按目标 movieId 升序。
        rows = []
        for t in (2, 3, 4):
            rows.append((t, 1, 5.0, t))
            rows.append((t, t, 5.0, t))
        return rows

    def test_tie_break_by_movie_id(self):
        X, item_ids, _ = build_positive_matrix(self._fan_rows())
        g = build_neighbors(X, item_ids, beta=0.0, max_k=100)
        targets = [t for (s, t) in g if s == 1]
        self.assertEqual(targets, [2, 3, 4])  # 序列按 (-s, movieId)

    def test_fewer_than_k_positive_neighbors(self):
        X, item_ids, _ = build_positive_matrix(self._fan_rows())
        g = build_neighbors(X, item_ids, beta=0.0, max_k=50)
        self.assertEqual(len([1 for (s, t) in g if s == 1]), 3)  # 只有 3 个正邻居

    def test_k_prefix_is_prefix_of_larger_k(self):
        X, item_ids, _ = build_positive_matrix(self._fan_rows())
        g1 = build_neighbors(X, item_ids, beta=0.0, max_k=1)
        g2 = build_neighbors(X, item_ids, beta=0.0, max_k=2)
        seq1 = [t for (s, t) in g1 if s == 1]
        seq2 = [t for (s, t) in g2 if s == 1]
        self.assertEqual(seq1, seq2[:1])


class BlockConsistencyTests(unittest.TestCase):
    """分块对拍：块大小 1/2/128 与输入行序不影响结果。"""

    def _rows(self):
        rows = []
        rng = np.random.RandomState(0)
        for u in range(1, 31):
            picks = rng.choice([1, 2, 3, 4, 5, 6], size=3, replace=False)
            for m in picks:
                rows.append((u, int(m), 5.0, u * 10 + int(m)))
        return rows

    def test_block_size_invariance(self):
        rows = self._rows()
        X, item_ids, _ = build_positive_matrix(rows)
        base = build_neighbors(X, item_ids, beta=10.0, max_k=3, block_size=128)
        for bs in (1, 2, 7):
            g = build_neighbors(X, item_ids, beta=10.0, max_k=3, block_size=bs)
            self.assertEqual(set(g), set(base))
            for key, val in g.items():
                self.assertEqual(val[0], base[key][0])
                self.assertEqual(val[1], base[key][1])
                self.assertAlmostEqual(val[2], base[key][2], places=12)

    def test_row_order_invariance(self):
        rows = self._rows()
        X1, item_ids1, _ = build_positive_matrix(rows)
        g1 = build_neighbors(X1, item_ids1, beta=10.0, max_k=3, block_size=2)
        rev = list(reversed(rows))
        X2, item_ids2, _ = build_positive_matrix(rev)
        g2 = build_neighbors(X2, item_ids2, beta=10.0, max_k=3, block_size=2)
        self.assertEqual(item_ids1, item_ids2)
        self.assertEqual(g1, g2)

    def test_matches_dense_bruteforce(self):
        rows = self._rows()
        X, item_ids, _ = build_positive_matrix(rows)
        g = build_neighbors(X, item_ids, beta=5.0, max_k=3, block_size=3)
        dense = (X.T @ X).toarray()
        n = np.asarray(X.sum(axis=0)).ravel()
        n_items = len(item_ids)
        for s in range(n_items):
            best = []
            for t in range(n_items):
                if s == t:
                    continue
                c = int(dense[s, t])
                if c == 0:
                    continue
                cos = c / np.sqrt(n[s] * n[t])
                sim = (c / (c + 5.0)) * cos
                best.append((t, sim, c))
            best.sort(key=lambda e: (-e[1], item_ids[e[0]]))
            want = [(item_ids[t], c) for t, sim, c in best[:3]]
            # g 的值元组为 (target, s, c, n_like_source, n_like_target)
            got = [(k[1], v[2]) for k, v in g.items() if k[0] == item_ids[s]]
            self.assertEqual(got, want)


class ScoreUserTests(unittest.TestCase):
    """用户打分：贡献求和、低分不推荐、空历史/空证据返回空、短列表不补齐。"""

    def setUp(self):
        self.X, self.item_ids, self.user_ids = build_positive_matrix(_gold_rows())
        self.g = build_neighbors(self.X, self.item_ids, beta=10.0, max_k=100)

    def test_gold_scores(self):
        # H_u1={2,10}，seen 含低分电影 42。
        recs = score_user(history={2, 10}, neighbors=self.g,
                          seen={2, 10, 42}, top_n=20)
        self.assertEqual([r['movie_id'] for r in recs], [5])
        s_ab = (2.0 / 12.0) * _cos_ab()
        s_bc = (1.0 / 11.0) * 0.5
        self.assertAlmostEqual(recs[0]['score'], s_ab + s_bc, places=12)

    def test_low_rated_movie_not_recommended(self):
        recs = score_user(history={2, 10}, neighbors=self.g, seen=set(), top_n=20)
        self.assertNotIn(42, [r['movie_id'] for r in recs])

    def test_empty_history_or_evidence_returns_empty(self):
        self.assertEqual(score_user(set(), self.g, set(), 20), [])
        self.assertEqual(score_user({2}, self.g, {2, 5, 10, 42}, 20), [])

    def test_short_list_not_padded(self):
        recs = score_user(history={2, 10}, neighbors=self.g,
                          seen={2, 10, 42}, top_n=20)
        self.assertEqual(len(recs), 1)  # 不补零分/热门

    def test_multi_source_sum_independent_of_history_order(self):
        r1 = score_user(history={2, 10}, neighbors=self.g,
                        seen={2, 10, 42}, top_n=20)
        r2 = score_user(history=[10, 2], neighbors=self.g,
                        seen=[42, 10, 2], top_n=20)
        self.assertEqual(r1, r2)


class EvidenceExplanationTests(unittest.TestCase):
    """证据解释：分数=贡献总和、最大贡献来源、c_ji、n_like。"""

    def test_evidence_fields_match_contributions(self):
        X, item_ids, _ = build_positive_matrix(_gold_rows())
        g = build_neighbors(X, item_ids, beta=10.0, max_k=100)
        recs = score_user(history={2, 10}, neighbors=g, seen={2, 10, 42}, top_n=20)
        rec = recs[0]
        self.assertEqual(rec['movie_id'], 5)
        self.assertEqual(rec['evidence_count'], 2)          # 来源 2 和 10
        self.assertAlmostEqual(rec['score'],
                               rec['contrib_sum'], places=12)
        self.assertAlmostEqual(rec['contrib_sum'],
                               rec['contributions'][0][1] + rec['contributions'][1][1],
                               places=12)
        self.assertEqual([c[0] for c in rec['contributions']], [2, 10])  # 源升序
        self.assertEqual(rec['top_source_movie_id'], 2)     # 贡献大者
        self.assertEqual(rec['top_source_c'], 2)            # c_{2,5}=2
        self.assertEqual(rec['top_source_n_like'], 3)       # n_like(2)=3
        self.assertEqual(rec['n_like'], 2)                  # n_like(5)=2

    def test_max_contrib_tie_breaks_to_smaller_movie_id(self):
        # 来源 2 与 10 对 5 的贡献完全并列（同 c=1、同 n_like 对）：
        # u1 好评 {2,5}，u2 好评 {10,5}；n_like(2)=n_like(10)=1，n_like(5)=2，
        # 于是 cos(2,5)=cos(10,5)=1/sqrt(1*2)，s_2,5 == s_10,5。
        rows = [(1, 2, 5.0, 1), (1, 5, 5.0, 2),
                (2, 10, 5.0, 3), (2, 5, 5.0, 4)]
        X, item_ids, _ = build_positive_matrix(rows)
        g = build_neighbors(X, item_ids, beta=10.0, max_k=100)
        recs = score_user(history={2, 10}, neighbors=g, seen={2, 10}, top_n=5)
        self.assertEqual(recs[0]['movie_id'], 5)
        each = (1.0 / 11.0) * (1.0 / np.sqrt(2.0))  # (c/(c+beta))*cos
        self.assertAlmostEqual(recs[0]['score'], 2.0 * each, places=12)
        self.assertEqual(recs[0]['top_source_movie_id'], 2)  # 并列取较小 movieId


if __name__ == '__main__':
    unittest.main()
