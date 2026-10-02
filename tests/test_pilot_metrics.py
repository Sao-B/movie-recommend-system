import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.metrics import recall_at_k, ndcg_at_k, rmse


class MetricsTests(unittest.TestCase):
    def test_recall(self):
        self.assertEqual(recall_at_k(['x', 'b', 'a'], ['a', 'b'], 2), 0.5)
        self.assertEqual(recall_at_k(['a', 'b'], ['a', 'b'], 2), 1.0)

    def test_ndcg(self):
        got = ndcg_at_k(['x', 'b', 'a'], ['a', 'b'], 2)
        expected = (1.0 / math.log2(3)) / (1.0 / math.log2(2) + 1.0 / math.log2(3))
        self.assertAlmostEqual(got, expected, places=9)
        self.assertEqual(ndcg_at_k(['a', 'b'], ['a', 'b'], 2), 1.0)

    def test_rmse(self):
        self.assertAlmostEqual(rmse([(1.0, 3.0), (3.0, 3.0)]), math.sqrt(2.0), places=9)

    def test_recall_denominator_is_full_relevant_set(self):
        # k 大于命中位置：分母必须是 |relevant| 全量，而不是 len(recommended[:k])。
        # 该输入下 recommended[:3] 长 3、|relevant| 长 2，两种公式才会分歧。
        self.assertEqual(recall_at_k(['x', 'b', 'a'], ['a', 'b'], 3), 1.0)

    def test_ndcg_idcg_uses_min_relevant_k(self):
        # |relevant| < k：理想 DCG 必须按 min(|relevant|, k) 项计（此处 3 项），
        # 而非固定 k 项——后者会算出 0.7227... 这种 < 1 的"完美排序"。
        self.assertAlmostEqual(ndcg_at_k(['a', 'b', 'c'], ['a', 'b', 'c'], 5),
                               1.0, places=9)

    def test_empty_relevant_returns_none(self):
        self.assertIsNone(recall_at_k(['a'], [], 5))
        self.assertIsNone(ndcg_at_k(['a'], [], 5))


class MetricsContractTests(unittest.TestCase):
    """契约用例：把此前只存在于手工探测中的语义固化下来。"""

    def test_recall_denominator_deduplicates_relevant(self):
        # 分母是 |set(relevant)|（全量去重），不是 len(relevant)。
        # 若不去重，['a','a'] 这种带重复的相关集合会把分母算成 2 而得到 0.5。
        self.assertEqual(recall_at_k(['a'], ['a', 'a'], 1), 1.0)
        self.assertEqual(recall_at_k(['a', 'b'], ['a', 'b', 'b'], 2), 1.0)

    def test_recall_deduplicates_via_set(self):
        # 推荐列表与控制集都按 set 语义去重：重复命中不重复计分。
        self.assertEqual(recall_at_k(['a', 'a', 'b'], ['a', 'b'], 3), 1.0)
        self.assertEqual(recall_at_k(['a', 'a', 'x'], ['a', 'b'], 3), 0.5)

    def test_recall_k_zero_is_zero_not_error(self):
        # k=0 时分子为空集，返回 0.0，不应抛 ZeroDivisionError。
        self.assertEqual(recall_at_k(['a'], ['a'], 0), 0.0)

    def test_ndcg_k_zero_is_zero(self):
        self.assertEqual(ndcg_at_k(['a'], ['a'], 0), 0.0)

    def test_ndcg_rewards_higher_rank(self):
        # 相关项排在前面必须得分更高（顺序敏感）。
        self.assertEqual(ndcg_at_k(['a', 'b', 'x'], ['a', 'b'], 2), 1.0)
        self.assertLess(ndcg_at_k(['b', 'x', 'a'], ['a', 'b'], 3),
                        ndcg_at_k(['a', 'b', 'x'], ['a', 'b'], 3))

    def test_ndcg_assumes_no_duplicate_recommendations(self):
        # 已文档化的前提：recommended 无重复项。若未去重，NDCG 可超过 1.0。
        self.assertGreater(ndcg_at_k(['a', 'a'], ['a'], 2), 1.0)
        self.assertLessEqual(ndcg_at_k(['a', 'b'], ['a'], 2), 1.0)

    def test_rmse_empty_returns_none(self):
        self.assertIsNone(rmse([]))

    def test_rmse_accepts_tuple_and_list_pairs(self):
        self.assertAlmostEqual(rmse([(1.0, 3.0), (3.0, 3.0)]), math.sqrt(2.0), places=9)
        self.assertAlmostEqual(rmse([[1.0, 3.0], [3.0, 3.0]]), math.sqrt(2.0), places=9)

    def test_rmse_rejects_non_numeric_pairs(self):
        # 传 dict 之类的错误形状应给出明确的 TypeError，而不是
        # 让 'str' - 'str' 这种底层报错冒出来。
        with self.assertRaises(TypeError):
            rmse([{'pred': 1.0, 'actual': 2.0}])

    def test_rmse_rejects_wrong_pair_arity(self):
        # 每项必须是 2 元组；3 元组 / 1 元组应显式报 TypeError
        # （原有实现抛的是 ValueError，与"形状不符 = TypeError"的契约不一致）。
        with self.assertRaises(TypeError):
            rmse([(1.0, 2.0, 3.0)])
        with self.assertRaises(TypeError):
            rmse([(1.0,)])

    def test_rmse_error_message_names_the_offending_index(self):
        # 报错要指出是第几条 pair 坏了，方便任务 7 定位。
        with self.assertRaises(TypeError) as ctx:
            rmse([(1.0, 2.0), {'pred': 1.0}])
        self.assertIn('pairs[1]', str(ctx.exception))

    def test_rmse_packed_parallel_lists_are_not_detectable(self):
        # 已文档化的歧义（非缺陷）：rmse(([1.0,3.0],[2.0,4.0])) 与
        # rmse([(1.0,3.0),(2.0,4.0)]) 在结构上完全一致（都是"2 条 pair"），
        # 唯一差别是内层容器为 list 还是 tuple，而 list pair 又是合法输入，
        # 因此无法在不破坏契约的前提下拒绝前者。此处固化该认知，避免
        # 日后有人误以为"传平行列表会报错"。
        self.assertEqual(rmse(([1.0, 3.0], [2.0, 4.0])),
                         rmse([(1.0, 3.0), (2.0, 4.0)]))

    def test_ndcg_idcg_deduplicates_relevant(self):
        # relevant 有重复项时理想 DCG 按去重后的大小算
        self.assertEqual(ndcg_at_k(['a'], ['a', 'a'], 2), 1.0)


if __name__ == '__main__':
    unittest.main()
