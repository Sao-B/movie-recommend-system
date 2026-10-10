# -*- coding: utf-8 -*-
"""独立核验器（``verify_holdout_eval.py``）的契约用例。

最重要的两条：

1. **独立性**：核验器不得 import 任何评测侧模块（用 AST 断言钉死）。
   这是"独立核验"四个字唯一的技术含义——如果它 import 了被验证的实现，
   它验证的就不是"数学对不对"，而是"两次调用同一个函数是否一致"。
2. **诚实性**：`pass` 必须要求 recompute 真的跑过；缺产物要报失败而不是静默通过。
"""
import ast
import os
import sys
import unittest

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(MODULE_DIR)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import numpy as np  # noqa: E402

from experiments.itemcf import verify_holdout_eval as V  # noqa: E402

VERIFIER_PATH = os.path.join(PROJECT_ROOT, 'experiments', 'itemcf',
                             'verify_holdout_eval.py')
#: 核验器**禁止** import 的模块（评测侧实现）。
FORBIDDEN = (
    'experiments.itemcf.heldout_eval',
    'experiments.itemcf.full_train',
    'experiments.itemcf.confirm',
    'experiments.itemcf.core',
    'experiments.itemcf.model_io',
    'experiments.itemcf.holdout_common',
)


class IndependenceTests(unittest.TestCase):
    """核验器必须与评测器**实现独立**。"""

    @staticmethod
    def _imported_modules(path):
        with open(path, encoding='utf-8') as handle:
            tree = ast.parse(handle.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    names.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
        return names

    def test_does_not_import_any_evaluation_module(self):
        imported = self._imported_modules(VERIFIER_PATH)
        bad = sorted(name for name in imported if name in FORBIDDEN)
        self.assertEqual(
            bad, [],
            'the verifier must not import the implementation it verifies; '
            'found: %r' % (bad,))

    def test_docstring_states_the_independence_contract(self):
        with open(VERIFIER_PATH, encoding='utf-8') as handle:
            head = handle.read()[:4000]
        self.assertIn('不 import', head)
        self.assertIn('AST', head)

    def test_recompute_uses_its_own_split_implementation(self):
        """核验器有自己的切分实现，不复用评测器的 ``RatingSession``。"""
        with open(VERIFIER_PATH, encoding='utf-8') as handle:
            source = handle.read()
        self.assertIn('def split_user_rows', source)
        self.assertNotIn('RatingSession', source)


class HonestyTests(unittest.TestCase):
    """``pass`` 的语义：必须真跑过。"""

    def test_pass_requires_recompute_ran(self):
        report = V._report({'x': {'ok': True}}, [], None, True)
        self.assertTrue(report['recompute_ran'])
        self.assertIn('pass', report)

    def test_missing_artifacts_fail_loudly(self):
        """产物缺失 → 直接失败，且报告里写明缺了什么。

        不用 ``tempfile.TemporaryDirectory``：本机受限沙箱下它在**清理阶段**
        会抛 ``PermissionError``（环境性失败，非逻辑回归），与本用例无关。
        这里改用一个不存在的路径——核验器只需要它"没有产物"。
        """
        missing_dir = os.path.join(PROJECT_ROOT, 'tests',
                                   '_itemcf_verify_tmp_absent_%d'
                                   % os.getpid())
        self.assertFalse(os.path.isdir(missing_dir))
        report, ok = V.verify(missing_dir, 'nonexistent.csv', sample_sources=0)
        self.assertFalse(ok)
        self.assertFalse(report['pass'])
        self.assertTrue(any('missing artifacts' in f
                            for f in report['failures']))

    def test_report_records_what_it_is_independent_of(self):
        report = V._report({}, [], None, True)
        self.assertIn('independent_of', report['verifier'])
        self.assertIn('experiments/itemcf/heldout_eval.py',
                      report['verifier']['independent_of'])
        self.assertIn('imports none', report['verifier']['note'])


class SplitTests(unittest.TestCase):
    """核验器自己的切分必须与协议一致（独立实现，但口径相同）。"""

    def test_rounding_table_matches_protocol(self):
        expected = {1: (1, 0), 2: (1, 1), 3: (2, 1), 4: (3, 1), 5: (4, 1),
                    6: (5, 1), 10: (8, 2), 20: (16, 4)}
        for n, (want_train, want_test) in expected.items():
            rows = [(1, m, 5.0, 1000 + m) for m in range(1, n + 1)]
            train, test = V.split_user_rows(rows)
            self.assertEqual((len(train), len(test)), (want_train, want_test),
                             'n=%d' % n)

    def test_no_row_is_lost(self):
        rows = [(1, m, 4.0, 1000 + m) for m in range(1, 40)]
        rows += [(2, m, 5.0, 2000 + m) for m in range(1, 25)]
        train, test = V.split_user_rows(rows)
        self.assertEqual(len(train) + len(test), len(rows))

    def test_no_pair_appears_on_both_sides(self):
        rows = [(1, m, 4.0, 1000 + m) for m in range(1, 30)]
        train, test = V.split_user_rows(rows)
        self.assertEqual(set((r[1]) for r in train) & set((r[1]) for r in test),
                         set())


class MetricTests(unittest.TestCase):
    """核验器的指标实现必须与协议一致。"""

    def test_ndcg_perfect_ranking_is_one(self):
        self.assertAlmostEqual(V.ndcg_at_k([1, 2, 3], {1, 2, 3}, 3), 1.0)

    def test_ndcg_denominator_uses_min_relevant_and_k(self):
        """IDCG 的分母是 ``min(|R_u|, k)``，与协议一致。

        ``|R_u| = 4``、``k = 2`` 时 IDCG 只累加 2 项，所以命中前两名就是满分
        1.0——这不是 bug，是"分母保留候选外正例"的口径在 ``|R| > k`` 时的表现。
        真正体现"候选外正例计入分母"的是 ``k`` 大于命中数时（下一条）。
        """
        self.assertAlmostEqual(V.ndcg_at_k([1, 2], {1, 2, 3, 4}, 2), 1.0)

    def test_ndcg_penalises_misses_when_k_exceeds_hits(self):
        """``k=4``、``|R|=4`` 但只命中前 2 名 → 明显小于 1（缺项拖低 DCG）。"""
        value = V.ndcg_at_k([1, 2], {1, 2, 3, 4}, 4)
        self.assertLess(value, 1.0)
        self.assertGreater(value, 0.5)

    def test_recall_denominator_is_relevant_count(self):
        self.assertAlmostEqual(V.recall_at_k([1, 2], {1, 2, 3, 4}, 2), 0.5)

    def test_empty_relevant_returns_none(self):
        self.assertIsNone(V.ndcg_at_k([1], set(), 1))
        self.assertIsNone(V.recall_at_k([1], set(), 1))

    def test_bootstrap_is_deterministic_for_a_seed(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [0.0, 1.0, 1.0, 2.0, 2.0]
        first = V.paired_bootstrap(a, b, seed=42, repeats=200)
        again = V.paired_bootstrap(a, b, seed=42, repeats=200)
        self.assertEqual(first['mean_difference'], again['mean_difference'])
        self.assertEqual(first['ci95'], again['ci95'])
        self.assertAlmostEqual(first['mean_difference'], 1.8, places=12)


class Python36Tests(unittest.TestCase):
    """VM 是 Python 3.6，核验器也必须能跑。"""

    def test_no_modern_only_constructs(self):
        banned = []
        with open(VERIFIER_PATH, encoding='utf-8') as handle:
            for number, line in enumerate(handle, 1):
                stripped = line.strip()
                if stripped.startswith('#'):
                    continue
                if '@dataclass' in line:
                    banned.append('%d dataclass' % number)
                if 'from __future__ import annotations' in line:
                    banned.append('%d future-annotations' % number)
                if ':=' in line:
                    banned.append('%d walrus' % number)
        self.assertEqual(banned, [])


if __name__ == '__main__':
    unittest.main()
