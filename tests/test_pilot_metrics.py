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

    def test_empty_relevant_returns_none(self):
        self.assertIsNone(recall_at_k(['a'], [], 5))
        self.assertIsNone(ndcg_at_k(['a'], [], 5))


if __name__ == '__main__':
    unittest.main()
