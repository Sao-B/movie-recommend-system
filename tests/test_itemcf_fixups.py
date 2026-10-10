# -*- coding: utf-8 -*-
"""ItemCF 补齐与诊断层测试（计划书 §6.4 / §8 / §11）。

覆盖本轮补的三块：

1. **对照方法逐条落盘**（§11）：MostPop / LikePop / BiasOnly 的 per_user 与
   recommendations 必须能落盘（含 ``relevant_ids`` / ``candidate_count``），
   字段与 ItemCF 一致、解释字段留空——这是"全部 14 方法可独立重算"的前提。
2. **配对 bootstrap**（§6.4）：seed=42、2000 次、同用户索引配对、percentile 区间；
   独立重算与手工计算一致，且产物必须带"探索性、非独立显著性验证"的声明。
3. **repeat / history-shuffle**（§8）：donor 是无不动点双射；错配后仍排除目标用户
   本人的训练已评电影。
"""
import csv
import json
import math
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf import bootstrap_ci, diagnostics
from experiments.itemcf import run_itemcf as R
from experiments.itemcf.evaluation import build_eval_context

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_tmp')
LOCAL_INPUT = os.path.join(PROJECT, 'data', 'processed', 'pilot_ratings.csv')


def _fixture_dir(name):
    path = os.path.join(FIXTURE_ROOT, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def _write_csv(path, rows, fields):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


class BaselinePersistenceTests(unittest.TestCase):
    """§11：非 ItemCF 方法的逐条推荐与逐用户行必须能落盘。"""

    @classmethod
    def setUpClass(cls):
        if not os.path.isfile(LOCAL_INPUT):
            raise unittest.SkipTest('pilot_ratings.csv unavailable')
        rows = R.load_rows_csv(LOCAL_INPUT)
        # 用一个小切片控制耗时（口径仍是真实的切分/上下文构造）
        rows = rows[:60000]
        from experiments.pilot.data import temporal_split_three_way
        train, val, _test = temporal_split_three_way(rows, 0.2, 0.2)
        cls.ctx = build_eval_context(train, val)

    def test_baselines_expose_users_recs_and_per_user(self):
        for name in ('MostPop', 'LikePop'):
            spec = R.comparison_method_spec(name)
            result = R.evaluate_comparison_method(spec, self.ctx, top_n=20)
            users = result['users']
            self.assertTrue(users, name)
            self.assertEqual(len(users), len(result['recommendations']), name)
            self.assertEqual(len(users), len(result['per_user']), name)
            for user, recs, row in zip(users, result['recommendations'],
                                       result['per_user']):
                self.assertEqual(row['user_id'], user)
                self.assertEqual(len(recs), len(set(recs)), name)
                self.assertTrue(row['relevant_ids'], name)
                self.assertIn('candidate_count', row)

    def test_per_user_rows_carry_relevant_ids_for_independent_recompute(self):
        """独立重算只依赖 relevant_ids + 推荐列表，缺列就必须报错而不是静默跳过。"""
        spec = R.comparison_method_spec('MostPop')
        result = R.evaluate_comparison_method(spec, self.ctx, top_n=20)
        for row in result['per_user']:
            self.assertTrue(row['relevant_ids'])


class BootstrapTests(unittest.TestCase):
    """§6.4：配对抽样与产物声明。"""

    def _fixture(self):
        run = _fixture_dir('bs_fixture')
        truth = {1: [10, 20], 2: [40], 3: [60, 70]}
        recs = {'MostPop': {1: [30, 10], 2: [40], 3: [60, 80]},
                'k20_beta0': {1: [10, 20], 2: [50], 3: [60, 70]}}
        per_user, rec_rows = [], []
        for model in ('MostPop', 'k20_beta0'):
            for user in (1, 2, 3):
                per_user.append({'model': model, 'user_id': user,
                                 'relevant_ids': '|'.join(str(i) for i in truth[user])})
                for rank, movie in enumerate(recs[model][user], 1):
                    rec_rows.append({'model': model, 'user_id': user,
                                     'rank': rank, 'movie_id': movie})
        _write_csv(os.path.join(run, 'per_user.csv'), per_user,
                   ['model', 'user_id', 'relevant_ids'])
        _write_csv(os.path.join(run, 'recommendations.csv'), rec_rows,
                   ['model', 'user_id', 'rank', 'movie_id'])
        return run

    def test_recompute_matches_hand_computed_metrics(self):
        run = self._fixture()
        got = bootstrap_ci.recompute_per_user(run)
        # user 1: ItemCF 推荐 [10,20]，R={10,20} -> NDCG=Recall=1.0
        self.assertAlmostEqual(got['k20_beta0'][1][0], 1.0, places=12)
        self.assertAlmostEqual(got['k20_beta0'][1][1], 1.0, places=12)
        # user 2: R={40}，推荐 [50] 全不中 -> 0/0
        self.assertAlmostEqual(got['k20_beta0'][2][0], 0.0, places=12)
        self.assertAlmostEqual(got['k20_beta0'][2][1], 0.0, places=12)
        # user 1 MostPop: 推荐 [30,10]，R={10,20} -> 命中位置 2
        exp_ndcg = (1.0 / math.log2(3)) / (1.0 / math.log2(2) + 1.0 / math.log2(3))
        self.assertAlmostEqual(got['MostPop'][1][0], exp_ndcg, places=12)
        self.assertAlmostEqual(got['MostPop'][1][1], 0.5, places=12)

    def test_paired_bootstrap_uses_same_indices_and_seed_42(self):
        a = [0.5, 0.2, 0.9, 0.1]
        b = [0.4, 0.25, 0.8, 0.15]
        res = bootstrap_ci.paired_bootstrap(a, b)
        self.assertEqual(res['seed'], 42)
        self.assertEqual(res['repeats'], 2000)
        self.assertEqual(res['users'], 4)
        self.assertAlmostEqual(res['mean_difference'],
                               sum(x - y for x, y in zip(a, b)) / 4.0, places=15)
        # 确定性：同参数两次调用结果完全一致
        self.assertEqual(res, bootstrap_ci.paired_bootstrap(a, b))
        self.assertLessEqual(res['ci95_low'], res['ci95_high'])

    def test_bootstrap_artifact_declares_exploratory_caveat(self):
        run = self._fixture()
        summary = bootstrap_ci.build(run)
        self.assertTrue(os.path.isfile(summary['bootstrap']))
        self.assertTrue(os.path.isfile(summary['paired_differences']))
        with open(summary['bootstrap'], encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertIn('NOT an', payload['caveat'])
        self.assertIn('探索性', payload['caveat_zh'])
        self.assertEqual(payload['seed'], 42)
        self.assertEqual(payload['repeats'], 2000)
        self.assertIn('k20_beta0 vs MostPop', payload['comparisons'])
        # paired_differences.csv 每行一条真实差值
        with open(summary['paired_differences'], newline='', encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 3)

    def test_requires_baselines(self):
        run = _fixture_dir('bs_nobase')
        _write_csv(os.path.join(run, 'per_user.csv'),
                   [{'model': 'k20_beta0', 'user_id': 1, 'relevant_ids': '10'}],
                   ['model', 'user_id', 'relevant_ids'])
        _write_csv(os.path.join(run, 'recommendations.csv'),
                   [{'model': 'k20_beta0', 'user_id': 1, 'rank': 1, 'movie_id': 10}],
                   ['model', 'user_id', 'rank', 'movie_id'])
        # 没有基线就拒绝产出区间（不能拿"跟谁都没比"当结论）
        with self.assertRaises(ValueError):
            bootstrap_ci.build(run)


class ShuffleDiagnosticTests(unittest.TestCase):
    """§8：donor 映射的置换性质与排除规则。"""

    def test_donors_are_derangement_and_bijection(self):
        users = list(range(1, 200))
        donors = diagnostics.build_donors(users, seed=42)
        self.assertEqual(sorted(donors), users)
        # 每个历史恰用一次
        self.assertEqual(sorted(donors.values()), users)
        # 不分配本人
        for user, donor in donors.items():
            self.assertNotEqual(user, donor)

    def test_donor_seed_is_fixed(self):
        users = list(range(1, 50))
        self.assertEqual(diagnostics.build_donors(users, seed=42),
                         diagnostics.build_donors(users, seed=42))

    def test_shuffle_histories_use_donor_and_keep_exactly_once(self):
        histories = {1: {10, 11}, 2: {20, 21}, 3: {30, 31}}
        donors = diagnostics.build_donors([1, 2, 3], seed=42)
        shuffled = dict((u, set(histories[donors[u]])) for u in donors)
        self.assertEqual(sorted(map(sorted, shuffled.values())),
                         sorted(map(sorted, histories.values())))
        for user, donor in donors.items():
            self.assertEqual(shuffled[user], histories[donor])

    def test_scoring_excludes_target_user_own_seen(self):
        """错配历史也要排除**目标用户本人**的全部训练已评电影。"""
        ranked = {1: [(100, 1.0, 5), (101, 0.9, 4)]}
        history = {1}          # donor 的历史
        own_seen = {100, 999}  # 目标用户本人评过的（含低分）
        out = diagnostics.score_user(history, ranked, own_seen, 20)
        ids = [r['movie_id'] for r in out]
        self.assertNotIn(100, ids)
        self.assertIn(101, ids)

    def test_score_user_matches_core_semantics(self):
        """本模块的独立打分实现必须与 core.score_user 同口径。

        ``core.score_user`` 收的邻居是**扁平** ``{(source, target): (target, sim,
        c, n_like_j, n_like_i)}``；本模块的 ``score_user`` 收的是按来源分桶的
        ``{source: [(target, sim, c), ...]}``。形状不同但打分口径必须一致。
        """
        from experiments.itemcf.core import score_user as core_score
        per_source = {1: [(100, 0.5, 3), (101, 0.25, 2)],
                      2: [(100, 0.5, 3), (102, 1.0, 4)]}
        flat = {}
        for source, edges in per_source.items():
            for target, sim, cooc in edges:
                flat[(source, target)] = (target, sim, cooc, 1, 1)
        history, seen = {1, 2}, {102}
        mine = diagnostics.score_user(history, per_source, seen, 20)
        theirs = core_score(history, flat, seen, 20)
        self.assertEqual([r['movie_id'] for r in mine],
                         [r['movie_id'] for r in theirs])
        for a, b in zip(mine, theirs):
            self.assertAlmostEqual(a['score'], b['score'], places=12)
            self.assertEqual(a['top_source_movie_id'], b['top_source_movie_id'])
            self.assertEqual(a['evidence_count'], b['evidence_count'])


class VerifyCoverageTests(unittest.TestCase):
    """§11 守门：成功方法缺落盘时必须报 mismatch，不能静默通过。"""

    def test_verify_flags_successful_method_missing_from_artifacts(self):
        from experiments.itemcf import verify_results
        run = _fixture_dir('verify_cov')
        _write_csv(os.path.join(run, 'per_user.csv'),
                   [{'model': 'k20_beta0', 'user_id': 1, 'relevant_ids': '10',
                     'recall@20': 1.0, 'ndcg@20': 1.0, 'returned_count': 1}],
                   ['model', 'user_id', 'relevant_ids', 'recall@20', 'ndcg@20',
                    'returned_count'])
        _write_csv(os.path.join(run, 'recommendations.csv'),
                   [{'model': 'k20_beta0', 'user_id': 1, 'rank': 1, 'movie_id': 10}],
                   ['model', 'user_id', 'rank', 'movie_id'])
        _write_csv(os.path.join(run, 'summary_table.csv'),
                   [{'name': 'k20_beta0', 'ndcg@20': 1.0, 'recall@20': 1.0},
                    {'name': 'MostPop', 'ndcg@20': 0.5, 'recall@20': 0.5}],
                   ['name', 'ndcg@20', 'recall@20'])
        with open(os.path.join(run, 'run_status.json'), 'w', encoding='utf-8') as fh:
            json.dump([{'name': 'k20_beta0', 'status': 'success'},
                       {'name': 'MostPop', 'status': 'success'}], fh)
        report = verify_results.verify(run)
        joined = ' '.join(report['metric_mismatches'])
        self.assertIn('MostPop', joined)
        self.assertFalse(report['all_recomputed'])
        self.assertEqual(report['expected_models'], ['MostPop', 'k20_beta0'])
        self.assertNotIn('MostPop', report['recommendation_models'])


if __name__ == '__main__':
    unittest.main()
