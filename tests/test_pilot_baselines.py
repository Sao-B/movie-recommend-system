import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.baselines import most_popular, random_top


class BaselinesTests(unittest.TestCase):
    def test_most_popular_orders_by_count_then_id(self):
        # 30 比 20 先出现但计数平票——断言按 id 升序决胜，不是按输入顺序
        train = [(1, 10, 5.0, 1), (2, 10, 4.0, 2), (3, 30, 3.0, 3), (4, 20, 3.0, 4)]
        self.assertEqual(most_popular(train, 3), [10, 20, 30])

    def test_random_top_deterministic_and_order_invariant(self):
        items = list(range(100))
        self.assertEqual(random_top(items, 5, seed=42), random_top(items, 5, seed=42))
        self.assertNotEqual(random_top(items, 5, seed=42), random_top(items, 5, seed=7))
        self.assertEqual(random_top(items, 5, seed=42),
                         random_top(sorted(items, reverse=True), 5, seed=42))


if __name__ == '__main__':
    unittest.main()
