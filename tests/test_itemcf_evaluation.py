# -*- coding: utf-8 -*-
"""ItemCF 实验的评测层测试（计划书 7.2 节：排序指标 / 证据解释 / 基线与诊断 / 统计）。

先写失败测试（TDD），实现见 experiments/itemcf/evaluation.py 与 baselines.py。
基线不依赖 experiments/itemcf/core.py（并行任务），评测与统计接口按桩/字典输入写。
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf.baselines import (
    like_popular, like_popular_order, fit_bias_only, bias_only_ranking, most_popular_order,
)
from experiments.itemcf.evaluation import (
    CANDIDATE_BINS, HISTORY_LAYERS, N_LIKE_MAX,
    build_eval_context,
    candidate_bin_of,
    evidence_from_history,
    evidence_max_recall,
    fixed_case_users,
    history_layer_of,
    paired_bootstrap,
    per_user_metrics,
    select_config,
    summarize,
    user_seen_hash,
    user_relevant_hash,
    candidate_universe_hash,
    protocol_hashes,
)


def rows(u, items, rating=5.0, ts=None):
    """构造 (userId, movieId, rating, timestamp) 行；ts 默认用自增序号。"""
    out = []
    for n, i in enumerate(items):
        out.append((u, i, rating, ts if ts is not None else 1000 + n))
    return out


class EvalContextTests(unittest.TestCase):
    """计划书 2.3 节：I_train / Seen_u / R_u / C_u 与纳入用户规则。"""

    def setUp(self):
        # u1: 训练 3 部（含低分 2.0 的 i4，须进 seen 但不进好评）；验证正例 i2(4.5)、i3(5.0)
        # u2: 训练 1 部；验证全低分（不纳入）
        # u3: 训练 1 部；验证正例 i30 全在候选外（仍纳入、计零分）
        # u4: 无训练好评；验证正例 i2（无好评用户必须保留）
        self.train = (
            rows(1, [1, 2, 4]) + [(1, 4, 2.0, 999)] +
            rows(2, [2]) +
            rows(3, [3]) +
            rows(4, [1], rating=2.0)
        )
        self.val = (
            [(1, 2, 4.5, 2000), (1, 3, 5.0, 2001)] +
            [(2, 1, 2.0, 2002)] +
            [(3, 30, 4.0, 2003)] +
            [(4, 2, 5.0, 2004)]
        )

    def test_item_universe_is_train_items(self):
        ctx = build_eval_context(self.train, self.val)
        self.assertEqual(ctx.item_ids, [1, 2, 3, 4])

    def test_seen_includes_low_rating_train_items(self):
        ctx = build_eval_context(self.train, self.val)
        self.assertEqual(ctx.seen[1], {1, 2, 4})

    def test_relevant_dedups_and_thresholds(self):
        train = self.train + [(1, 5, 5.0, 100)]
        val = self.val + [(1, 5, 5.0, 2005), (1, 2, 4.0, 2006), (1, 3, 3.5, 2007)]
        ctx = build_eval_context(train, val)
        self.assertEqual(ctx.relevant[1], {2, 3, 5})

    def test_eval_users_need_positive_relevant(self):
        ctx = build_eval_context(self.train, self.val)
        self.assertEqual(ctx.eval_users, [1, 3, 4])
        self.assertEqual(ctx.excluded_no_positive, [2])

    def test_users_with_out_of_candidate_positives_stay_in(self):
        ctx = build_eval_context(self.train, self.val)
        self.assertIn(3, ctx.eval_users)
        self.assertEqual(ctx.candidates[3] - {30}, ctx.candidates[3])  # 30 不在候选
        self.assertEqual(ctx.candidates[3], {1, 2, 4})

    def test_candidates_are_train_items_minus_seen(self):
        ctx = build_eval_context(self.train, self.val)
        self.assertEqual(ctx.candidates[1], {3})
        self.assertEqual(ctx.candidates[4], {2, 3, 4})

    def test_changing_validation_changes_relevant_but_not_item_universe(self):
        ctx2 = build_eval_context(self.train, [(1, 2, 5.0, 2000)])
        self.assertEqual(ctx2.item_ids, [1, 2, 3, 4])
        self.assertEqual(ctx2.seen[1], {1, 2, 4})
        self.assertEqual(ctx2.relevant[1], {2})

    def test_changing_test_rows_cannot_change_context(self):
        # 保留测试段不属于 build_eval_context 输入：构造里根本不接受它
        # （这里锁"指标不读取 test 段"——调用面就没有 test 参数）
        import inspect
        self.assertNotIn('test', inspect.getfullargspec(build_eval_context).args)

    def test_universe_hash_distinguishes_different_item_sets(self):
        ctx_a = build_eval_context(self.train, self.val)
        ctx_b = build_eval_context(self.train + [(9, 999, 5.0, 1)], self.val)
        self.assertNotEqual(candidate_universe_hash(ctx_a.item_ids),
                            candidate_universe_hash(ctx_b.item_ids))
        self.assertEqual(candidate_universe_hash([1, 2, 3]),
                         candidate_universe_hash([3, 2, 1]))  # 只认集合

    def test_per_user_hashes_are_stable_and_distinct(self):
        ctx = build_eval_context(self.train, self.val)
        self.assertEqual(user_seen_hash(ctx.seen[1]), user_seen_hash({4, 1, 2}))
        self.assertNotEqual(user_seen_hash(ctx.seen[1]), user_seen_hash({1, 2, 3}))
        # 语义区分靠 protocol_hashes 的分桶名；相同集合的两类哈希本就该相同
        self.assertEqual(user_relevant_hash({2, 3}), user_seen_hash({2, 3}))
        hashes = protocol_hashes(ctx)
        self.assertIn('seen', hashes['per_user']['1'])
        self.assertIn('relevant', hashes['per_user']['1'])
        self.assertNotEqual(hashes['per_user']['1']['seen'],
                            hashes['per_user']['1']['relevant'])


class RankingMetricTests(unittest.TestCase):
    """计划书 6.1 节：固定分母、短列表、证据上限（复用 pilot 的 Recall/NDCG）。"""

    def test_metrics_use_full_relevant_denominator(self):
        rec = [[1, 2], [1, 2, 3], []]
        rel = [{1, 2, 3, 4}, {1, 2, 3, 4}, {1, 2, 3, 4}]
        m = per_user_metrics(rec, rel, k_values=(5,))
        self.assertAlmostEqual(m['recall@5'][0], 2 / 4.0)
        self.assertAlmostEqual(m['ndcg@5'][0],
                               (1.0 + 1 / math.log2(3)) / sum(1 / math.log2(p + 2) for p in range(4)))

    def test_short_list_and_empty_list_score_zero_not_drop_user(self):
        rec = [[], [7]]
        rel = [{7, 8}, {7, 8}]
        m = per_user_metrics(rec, rel, k_values=(5,))
        self.assertEqual(m['recall@5'], [0.0, 0.5])
        self.assertEqual(m['users_scored'], 2)

    def test_short_list_does_not_rescale_idcg(self):
        # 只回 1 条也按 min(K, |R_u|) 计 IDCG
        m = per_user_metrics([[1]], [{1, 2, 3}], k_values=(5,))
        self.assertAlmostEqual(m['ndcg@5'][0], (1 / math.log2(2)) / sum(1 / math.log2(p + 2) for p in range(3)))

    def test_prefix_metrics_use_one_top20_list(self):
        top20 = [[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]]
        rel = [{1, 10}]
        m = per_user_metrics(top20, rel, k_values=(5, 10, 20))
        self.assertAlmostEqual(m['recall@5'][0], 0.5)   # 只有 1 在前 5
        self.assertAlmostEqual(m['recall@10'][0], 1.0)
        self.assertAlmostEqual(m['recall@20'][0], 1.0)

    def test_evidence_max_recall_upper_bounds_actual_recall(self):
        rec = [[1, 2]]
        rel = [{1, 2, 3}]
        m = per_user_metrics(rec, rel, k_values=(20,), evidence={0: {1, 2, 3}})
        self.assertLessEqual(m['recall@20'][0], m['evidence_max_recall@20'][0])
        self.assertAlmostEqual(m['evidence_max_recall@20'][0], 1.0)
        m2 = per_user_metrics(rec, rel, k_values=(20,), evidence={0: {1}})
        self.assertGreater(m2['recall@20'][0], m2['evidence_max_recall@20'][0])
        self.assertAlmostEqual(m2['evidence_max_recall@20'][0], 1 / 3.0)

    def test_duplicate_recommendations_are_rejected(self):
        with self.assertRaises(ValueError):
            per_user_metrics([[1, 1]], [{1}], k_values=(20,))

    def test_relevant_left_out_of_candidates_keeps_zero_scores(self):
        # 正例全在候选外：必须返回 0 分而不是 None（None 会把用户踢出均值）
        m = per_user_metrics([[]], [{99}], k_values=(20,))
        self.assertEqual(m['recall@20'], [0.0])
        self.assertEqual(m['ndcg@20'], [0.0])


class EvidenceHelperTests(unittest.TestCase):
    def test_evidence_max_recall_formula(self):
        self.assertAlmostEqual(evidence_max_recall({1, 2, 3}, {1}, 20), 1 / 3.0)
        self.assertAlmostEqual(evidence_max_recall({1, 2}, {1, 2, 3, 4}, 20), 1.0)
        self.assertIsNone(evidence_max_recall(set(), {1}, 20))

    def test_evidence_max_recall_caps_hits_at_k(self):
        rel = set(range(1, 40))
        self.assertAlmostEqual(evidence_max_recall(rel, rel, 20), 20 / 39.0)


class StratificationTests(unittest.TestCase):
    """计划书 6.3 节：历史分层与 n_all 分箱标签。"""

    def test_history_layers(self):
        self.assertEqual(history_layer_of(0), '0')
        self.assertEqual(history_layer_of(1), '1-5')
        self.assertEqual(history_layer_of(5), '1-5')
        self.assertEqual(history_layer_of(6), '6-20')
        self.assertEqual(history_layer_of(20), '6-20')
        self.assertEqual(history_layer_of(21), '>20')

    def test_candidate_bins(self):
        self.assertEqual(candidate_bin_of(None), 'out_of_candidate')
        self.assertEqual(candidate_bin_of(0), 'out_of_candidate')
        self.assertEqual(candidate_bin_of(1), '1-4')
        self.assertEqual(candidate_bin_of(4), '1-4')
        self.assertEqual(candidate_bin_of(5), '5-19')
        self.assertEqual(candidate_bin_of(20), '20-99')
        self.assertEqual(candidate_bin_of(100), '>=100')
        self.assertEqual(CANDIDATE_BINS,
                         ['out_of_candidate', '1-4', '5-19', '20-99', '>=100'])

    def test_fixed_case_users_take_smallest_id_per_non_empty_layer(self):
        users = {1: 0, 2: 0, 5: 3, 6: 3, 9: 30, 11: 0}
        picked = fixed_case_users(users)
        self.assertEqual(picked['0'], [1, 2])
        self.assertEqual(picked['1-5'], [5, 6])
        self.assertEqual(picked['>20'], [9])
        self.assertEqual(picked['6-20'], [])  # 无该层用户：报告为空

    def test_fixed_case_layers_without_users_report_empty(self):
        picked = fixed_case_users({3: 100})
        self.assertEqual(picked['0'], [])
        self.assertEqual(picked['1-5'], [])
        self.assertEqual(picked['6-20'], [])
        self.assertEqual(picked['>20'], [3])


class PairedBootstrapTests(unittest.TestCase):
    """计划书 6.4 节：同索引配对 bootstrap。"""

    def test_identical_lists_give_zero_interval(self):
        v = [0.1, 0.0, 0.33, 0.5, 0.25]
        result = paired_bootstrap(v, v, seed=42, repeats=200)
        self.assertEqual(result['mean_difference'], 0.0)
        self.assertEqual(result['ci95'], [0.0, 0.0])
        self.assertEqual(result['seed'], 42)
        self.assertEqual(result['repeats'], 200)

    def test_seed_reproducible(self):
        a = [0.1, 0.2, 0.3, 0.4]
        b = [0.05, 0.25, 0.2, 0.35]
        self.assertEqual(paired_bootstrap(a, b, seed=42, repeats=100),
                         paired_bootstrap(a, b, seed=42, repeats=100))
        self.assertNotEqual(paired_bootstrap(a, b, seed=42, repeats=100),
                            paired_bootstrap(a, b, seed=7, repeats=100))

    def test_pairing_uses_same_user_indices(self):
        # 每人差值 -1；若错把两模型各自独立抽样（而非同索引配对），均值会在 0 附近抖动
        a = [0.0, 1.0, 0.0, 1.0, 0.5, 0.5]
        b = [1.0, 2.0, 1.0, 2.0, 1.5, 1.5]  # b = a + 1（逐人）
        result = paired_bootstrap(a, b, seed=42, repeats=500)
        self.assertAlmostEqual(result['mean_difference'], -1.0)  # a - b = -1 每人
        self.assertEqual(result['ci95'], [-1.0, -1.0])  # 每人差值相同 → 区间退化

    def test_length_mismatch_rejected(self):
        with self.assertRaises(ValueError):
            paired_bootstrap([1.0, 2.0], [1.0], seed=42, repeats=10)

    def test_empty_input_rejected(self):
        with self.assertRaises(ValueError):
            paired_bootstrap([], [], seed=42, repeats=10)


class LikePopTests(unittest.TestCase):
    """计划书 4.1 / 7.2：LikePop 按 n_like（好评人数），不是均分也不是 n_all。"""

    def setUp(self):
        self.train = (
            [(1, 10, 5.0, 1), (2, 10, 4.5, 2), (3, 10, 4.0, 3)] +      # n_like(10)=3
            [(4, 20, 3.0, 4), (5, 20, 5.0, 5)] +                        # n_like(20)=1, n_all=2
            [(6, 30, 4.0, 6)] +                                         # n_like(30)=1
            [(7, 40, 1.0, 7)]                                           # n_like(40)=0
        )

    def test_orders_by_like_count_then_movie_id(self):
        self.assertEqual(like_popular_order(self.train, N_LIKE_MAX), [10, 20, 30, 40])

    def test_not_by_mean_rating(self):
        # 20 均分 4.0、30 均分 4.0，但 20 的 n_like 与 30 相同 → movieId 决胜；
        # 若按均分排序则 20 与 30 同均分但顺序不变——换成均分可区分的例子：
        train = self.train + [(8, 30, 5.0, 8)]   # n_like(30)=2, 均分 4.5 > 20 的 4.0
        self.assertEqual(like_popular_order(train, N_LIKE_MAX)[:2], [10, 30])
        # 且 20（均分 4.0、n_like=1）排在 30 后面，验证计数而非均分驱动
        self.assertEqual(like_popular_order(train, N_LIKE_MAX), [10, 30, 20, 40])

    def test_zero_like_items_are_padded_in_id_order(self):
        train = [(1, 30, 4.0, 1), (2, 10, 4.0, 2), (3, 20, 2.0, 3)]
        self.assertEqual(like_popular_order(train, N_LIKE_MAX), [10, 30, 20])

    def test_like_popular_reuses_pilot_most_popular_for_mostpop(self):
        from experiments.pilot.baselines import most_popular
        train = self.train
        self.assertEqual(most_popular_order(train, 2), most_popular(train, 2))

    def test_per_user_recommendation_excludes_seen(self):
        recs = like_popular(self.train, users={1: {10}, 2: set()}, top_n=2, item_ids=None)
        self.assertEqual(recs[1], [(20, 1), (30, 1)])
        self.assertEqual([i for i, _ in recs[2]], [10, 20])


class BiasOnlyTests(unittest.TestCase):
    """计划书 4.3 / 7.2：BiasOnly 闭式解、目标单调、按 b_i 排序等价。"""

    def setUp(self):
        # 每用户 2 条、每物品 2 条，便于手算闭式解（n_u = n_i = 2）
        self.train = [
            (1, 10, 5.0, 1), (1, 20, 3.0, 2),
            (2, 10, 4.0, 3), (2, 30, 2.0, 4),
            (3, 20, 4.0, 5), (3, 30, 5.0, 6),
        ]

    def test_matches_hand_computed_closed_form(self):
        # 手算（Fraction 精确值，独立于实现），mu = 23/6，闭式解 b = sum(res)/(n(1+λ))：
        # 第一轮用户偏置（b_i 全 0）：b_u1 = 5/33，b_u2 = -25/33，b_u3 = 20/33
        # 第一轮物品偏置（res = r - mu - b_u）：
        #   i10: (5,4) → res 和 64/33 → b_i10 = 320/363
        #   i20: (3,4) → res 和 -47/33 → b_i20 = -235/363
        #   i30: (2,5) → res 和 -17/33 → b_i30 = -85/363
        model = fit_bias_only(self.train, lam=0.1, rounds=1)
        self.assertAlmostEqual(model['mu'], 23.0 / 6.0, places=12)
        self.assertAlmostEqual(model['bu'][1], 5.0 / 33.0, places=12)
        self.assertAlmostEqual(model['bu'][2], -25.0 / 33.0, places=12)
        self.assertAlmostEqual(model['bu'][3], 20.0 / 33.0, places=12)
        self.assertAlmostEqual(model['bi'][10], 320.0 / 363.0, places=12)
        self.assertAlmostEqual(model['bi'][20], -235.0 / 363.0, places=12)
        self.assertAlmostEqual(model['bi'][30], -85.0 / 363.0, places=12)

    def test_objective_non_increasing_across_rounds(self):
        _, objectives = fit_bias_only(self.train, lam=0.1, rounds=5, return_objectives=True)
        self.assertEqual(len(objectives), 6)  # 初值 + 5 轮
        for a, b in zip(objectives, objectives[1:]):
            self.assertLessEqual(b - a, 1e-8 * max(1.0, abs(a)))

    def test_ranking_equals_item_bias_ranking(self):
        model = fit_bias_only(self.train, lam=0.1, rounds=10)
        rec = bias_only_ranking(model, user=1, seen={10}, item_ids=[10, 20, 30], top_n=5)
        by_bias = [i for _, i in sorted(
            ((model['bi'][i], i) for i in (10, 20, 30) if i not in {10}),
            key=lambda t: (-t[0], t[1]))]
        self.assertEqual([i for i, _ in rec], by_bias)
        # mu + b_u 对固定用户是常数，不改变电影间顺序
        scores = [s for _, s in rec]
        offsets = [s - model['bi'][i] for (i, s) in rec]
        self.assertTrue(all(abs(o - offsets[0]) < 1e-12 for o in offsets))

    def test_zero_observation_user_bias_is_zero(self):
        model = fit_bias_only(self.train, lam=0.1, rounds=10)
        self.assertEqual(model['bu'].get(999, 0.0), 0.0)
        self.assertEqual(model['bi'].get(999, 0.0), 0.0)


class SummarizeTests(unittest.TestCase):
    """计划书 6.2：覆盖率 / 满额率 / 填充率 / 空列表率等聚合口径。"""

    def test_rates_use_common_user_count(self):
        recs = [[1, 2], [1], []]
        rel = [{1}, {1, 2}, {3}]
        ctx_users = [10, 11, 12]
        evidence = {0: {1, 2}, 1: {1}, 2: set()}
        candidates = {0: {1, 2, 3}, 1: {1, 2, 3}, 2: {1, 2}}
        summary = summarize(recs, rel, ctx_users, k_values=(20,),
                            top_n=2, evidence=evidence, item_universe={1, 2, 3},
                            n_all={1: 5, 2: 10, 3: 1}, candidates=candidates,
                            low_support_max=1)
        self.assertEqual(summary['users_scored'], 3)
        self.assertEqual(summary['full_list_rate'], 1 / 3.0)
        self.assertEqual(summary['empty_list_rate'], 1 / 3.0)
        self.assertEqual(summary['returned_count'], 3)
        self.assertAlmostEqual(summary['fill_rate'], 3 / 6.0)
        self.assertAlmostEqual(summary['catalog_coverage'], 2 / 3.0)
        # 证据正例覆盖率：u0 = 1/1，u1 = 1/2，u2 = 0/1
        self.assertAlmostEqual(summary['evidence_positive_coverage'], (1 + 0.5 + 0.0) / 3.0)
        # 证据上限：u0 = min(2,1)/1 = 1，u1 = min(2,1)/2 = 0.5，u2 = 0
        self.assertAlmostEqual(summary['evidence_max_recall@20'], (1.0 + 0.5 + 0.0) / 3.0)
        # 候选正例覆盖率：u0 = 1，u1 = 1，u2 = 0/1（正例 3 在候选外）
        self.assertAlmostEqual(summary['candidate_coverage'], (1.0 + 1.0 + 0.0) / 3.0)
        self.assertEqual(summary['evidence_count_zero_users'], 1)
        # 推荐位置 = [1(n_all=5), 2(10), 1(5)]，阈值 4 时无低支持度位置
        self.assertAlmostEqual(summary['low_support_rate'], 0.0)

    def test_empty_recommendations_report_na_not_crash(self):
        summary = summarize([[], []], [{1}, {2}], [1, 2], k_values=(20,),
                            top_n=20, evidence={0: set(), 1: set()},
                            item_universe={1, 2}, n_all={1: 1, 2: 1})
        self.assertIsNone(summary['low_support_rate'])
        self.assertIsNone(summary['recommended_popularity_mean'])
        self.assertEqual(summary['empty_list_rate'], 1.0)


if __name__ == '__main__':
    unittest.main()
