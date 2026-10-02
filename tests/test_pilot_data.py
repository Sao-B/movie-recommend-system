import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.data import temporal_split


class TemporalSplitTests(unittest.TestCase):
    def test_split_sizes_and_leakage(self):
        rows = []
        # user 1: ts 1..5；user 2: ts 1..2；user 3: 只有 1 条
        for ts in range(1, 6):
            rows.append((1, 100 + ts, 4.0, ts))
        for ts in range(1, 3):
            rows.append((2, 200 + ts, 3.0, ts))
        rows.append((3, 300, 5.0, 1))
        train, test = temporal_split(rows)
        self.assertEqual(len(train), 4 + 1 + 1)  # u1:4 u2:1 u3:1
        self.assertEqual(len(test), 1 + 1)       # u1:1 u2:1
        # 无泄漏：每个用户的测试条目 ts 严格晚于其训练条目
        for u in (1, 2):
            tr = [r for r in train if r[0] == u]
            te = [r for r in test if r[0] == u]
            self.assertLess(max(r[3] for r in tr), min(r[3] for r in te))

    def test_single_rating_user_stays_in_train(self):
        train, test = temporal_split([(9, 1, 5.0, 1)])
        self.assertEqual(len(train), 1)
        self.assertEqual(len(test), 0)

    def test_deterministic(self):
        rows = [(1, i, 4.0, i) for i in range(10)]
        self.assertEqual(temporal_split(rows), temporal_split(rows))

    def test_tie_break_input_order_invariant(self):
        # 4 条评分同一秒（ts 全为 1），tie 由 movieId 决胜，与输入行序无关
        rows = [(1, 101, 4.0, 1), (1, 102, 3.0, 1), (1, 103, 5.0, 1), (1, 104, 2.0, 1)]
        train, test = temporal_split(rows)
        self.assertEqual({r[1] for r in test}, {104})  # movieId 最大的进测试集
        # 承重断言是下面两条反转比较：成员断言（{104}）在旧实现下也可能过，
        # 只有「输入行序反转后切分结果逐条相同」才真正锁住按 movieId 决胜。
        rev_train, rev_test = temporal_split(rows[::-1])
        self.assertEqual(sorted(train), sorted(rev_train))
        self.assertEqual(sorted(test), sorted(rev_test))

    def test_split_fraction_uses_ratio(self):
        # 8 条评分按 20% 留测试：锁住向下取整语义——int(8*0.2)=1（区别于
        # round(8*0.2)=2）。n=10 时 int 与 round 同为 2，锁不住这个分支。
        rows = [(1, i, 4.0, i) for i in range(1, 9)]
        train, test = temporal_split(rows)
        self.assertEqual(len(train), 7)
        self.assertEqual(len(test), 1)


if __name__ == '__main__':
    unittest.main()
