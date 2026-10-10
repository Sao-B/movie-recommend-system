# -*- coding: utf-8 -*-
"""新入口（prepare_confirmation + confirm）的针对性测试。

覆盖计划书 §4「针对性测试」要求的新增协议、存储与遍历契约：

- 哈希顺序：cohort/eval 的排序键与"无前导零"口径；
- 名单断言：新旧交集为空、评测名单是 cohort 子集、规模必须精确；
- 切分取整：复用 ``temporal_split`` 的规则（单评分用户整条进训练等）；
- 留出评分不进训练、候选外正例计入 Recall/IDCG 分母、短列表不改变 IDCG；
- 邻居存储存取回环：按 ``(-similarity, target_movieId)`` 正确排序；
- 内存契约：候选全集只有一份、逐用户只遍历其好评来源的邻居；
- 对拍模式与默认模式显式区分（模式名/切分函数不同）。

运行：
    backend/venv/Scripts/python.exe -m unittest tests.test_itemcf_confirmation -v
"""
import array
import csv
import hashlib
import json
import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf import confirm as C  # noqa: E402
from experiments.itemcf import prepare_confirmation as P  # noqa: E402
from experiments.pilot.data import temporal_split  # noqa: E402

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_confirm_tmp')

_SEQ_LOCK = threading.Lock()
_SEQ = [0]


def fixture_dir(prefix):
    """建唯一临时目录（沙箱下不做清理，与既有 itemcf 测试同策略）。

    ``time.time()*1000`` 在同一测试进程里会连续撞名（Windows 时钟分辨率约
    15 ms，一个用例跑完不到 1 ms），所以再叠一个单调序号并**重试**，
    绝不返回一个已被使用的目录——复用目录会让"落盘产物"读到上一轮遗留文件。
    """
    for _attempt in range(1000):
        with _SEQ_LOCK:
            _SEQ[0] += 1
            seq = _SEQ[0]
        path = os.path.join(FIXTURE_ROOT, '%s-%d-%d' % (prefix, int(time.time() * 1000), seq))
        try:
            os.makedirs(path)
            return path
        except OSError:
            continue
    raise RuntimeError('could not create a unique fixture dir for %r under %s'
                       % (prefix, FIXTURE_ROOT))


def write_ratings(path, rows):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['userId', 'movieId', 'rating', 'timestamp'])
        for row in rows:
            writer.writerow(row)
    return path


def synthetic_rows(n_users=40, seed=7, grouped=True):
    """合成评分：每用户 5 条、``(userId, movieId)`` 唯一、时间递增。

    最后一条（即留出段那一条）刻意给高分，保证存在可评测的 ``>=4`` 正例；
    低分（2.0）放在倒数第二条，用于验证"低分不进相似度但进 seen"。

    ``grouped=True`` 模拟真实 ``ratings.csv``（按 userId 分组）；
    ``grouped=False`` 故意交错，用来验证切分与行序无关。
    """
    ratings = [5.0, 4.0, 3.0, 4.5, 4.0]  # 末两条都是正例：两种切分都有正例可评
    rows = []
    for user in range(1, n_users + 1):
        base = 1000 + user * 100
        seen = set()
        for offset in range(5):
            movie = int(1 + (user * 3 + offset * 5) % 40)
            while movie in seen:
                movie = movie % 40 + 1
            seen.add(movie)
            rows.append((user, movie, ratings[offset], base + offset * 10))
    if grouped:
        return rows
    interleaved = []
    for index in range(5):
        for user in range(n_users):
            interleaved.append(rows[user * 5 + index])
    return interleaved


# ---------------------------------------------------------------------------
# 哈希顺序与名单口径
# ---------------------------------------------------------------------------

class ExtraExclusionSourceTests(unittest.TestCase):
    """额外排除来源只读 userId 列，**不**依赖文件字节稳定。

    ``fix14/per_user.csv`` 存在两个合法 vintage（本地 03:44 镜像 / VM 05:01
    被后置报告步骤重写），字节哈希不同但 user_id 列相同。若实现里做了
    "排除来源文件哈希必须等于某常量"的断言，会因为版本差异误报。
    """

    def test_gather_extra_exclusions_reads_user_ids_not_hashes(self):
        folder = fixture_dir('extra-src')
        rel = os.path.join('out', 'per_user.csv')
        target = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(target))
        with open(target, 'w', newline='', encoding='utf-8') as handle:
            writer = csv.writer(handle)
            writer.writerow(['user_id', 'ndcg@20'])
            writer.writerow([11, 0.5])
            writer.writerow([22, 0.1])
        users, provenance = P.gather_extra_exclusions(
            folder, (('fix14_per_user', rel, 'user_id'),))
        self.assertEqual(users, {11, 22})
        self.assertTrue(provenance['fix14_per_user']['present'])
        self.assertEqual(provenance['fix14_per_user']['users'], 2)

    def test_extra_sources_are_not_hash_asserted(self):
        """同内容不同 mtime 的两个文件必须给出同一排除名单。"""
        folder = fixture_dir('extra-src-vintage')
        rel = os.path.join('out', 'per_user.csv')
        target = os.path.join(folder, rel)
        os.makedirs(os.path.dirname(target))
        for _round in (1, 2):
            with open(target, 'w', newline='', encoding='utf-8') as handle:
                writer = csv.writer(handle)
                writer.writerow(['user_id', 'evidence_count'])
                writer.writerow([7, _round])  # 只有"后加的列"不同
                writer.writerow([9, _round])
            users, _prov = P.gather_extra_exclusions(
                folder, (('fix14_per_user', rel, 'user_id'),))
            self.assertEqual(users, {7, 9})

    def test_missing_extra_source_is_disclosed_not_silent(self):
        folder = fixture_dir('extra-src-missing')
        users, provenance = P.gather_extra_exclusions(
            folder, (('fix14_per_user', 'nope/per_user.csv', 'user_id'),))
        self.assertEqual(users, set())
        self.assertFalse(provenance['fix14_per_user']['present'])


class HashOrderTests(unittest.TestCase):
    def test_user_key_matches_documented_formula(self):
        for user in (1, 3, 42, 200948):
            expected = hashlib.sha256(
                ('itemcf-confirm-cohort-v1:' + str(user)).encode('utf-8')).hexdigest()
            self.assertEqual(P.cohort_key(user), (expected, user))

    def test_production_defaults_match_plan(self):
        self.assertEqual(P.COHORT_SALT, 'itemcf-confirm-cohort-v1:')
        self.assertEqual(P.EVAL_SALT, 'itemcf-confirm-eval-v1:')
        self.assertEqual(P.COHORT_SIZE, 20000)
        self.assertEqual(P.EVAL_SIZE, 2000)

    def test_no_leading_zero_is_same_key(self):
        """``"003"`` / ``3`` / ``3.0`` 必须给出同一把钥匙（无前导零口径）。"""
        self.assertEqual(P.user_key(P.COHORT_SALT, '003'),
                         P.user_key(P.COHORT_SALT, 3))
        self.assertEqual(P.user_key(P.COHORT_SALT, 3.0),
                         P.user_key(P.COHORT_SALT, 3))
        self.assertEqual(P.user_key(P.COHORT_SALT, ' 3 '),
                         P.user_key(P.COHORT_SALT, 3))

    def test_invalid_user_ids_rejected(self):
        for bad in ('', 'abc', '1.0', '-5', 0, -1, '1e3', True):
            with self.assertRaises(ValueError):
                P.normalize_user_id(bad)

    def test_selection_order_is_hash_then_id(self):
        users = list(range(1, 51))
        ordered = sorted(users, key=lambda u: P.cohort_key(u))
        picked = P.select_top_by_hash(users, P.COHORT_SALT, 10)
        self.assertEqual(picked, sorted(ordered[:10]))

    def test_selection_is_independent_of_input_order_and_duplicates(self):
        users = list(range(1, 51))
        shuffled = list(reversed(users)) + users[:10]
        self.assertEqual(P.select_top_by_hash(users, P.EVAL_SALT, 7),
                         P.select_top_by_hash(shuffled, P.EVAL_SALT, 7))

    def test_eval_and_cohort_orders_are_independent(self):
        users = list(range(1, 201))
        cohort = P.select_top_by_hash(users, P.COHORT_SALT, 100)
        eval_users = P.select_top_by_hash(users, P.EVAL_SALT, 20)
        self.assertEqual(sorted(eval_users),
                         sorted(sorted(users, key=lambda u: P.eval_key(u))[:20]))
        self.assertNotEqual(cohort[:20], eval_users)

    def test_no_activity_filtering_in_selection(self):
        """选人只看哈希，不看活跃度/好评数（同一集合换顺序结果不变）。"""
        users = list(range(1, 101))
        self.assertEqual(P.select_top_by_hash(users, P.COHORT_SALT, 50),
                         sorted(sorted(users, key=lambda u: P.cohort_key(u))[:50]))

    def test_user_list_hash_is_line_based(self):
        self.assertEqual(P.user_list_sha256([1, 2, 3]),
                         hashlib.sha256(b'1\n2\n3\n').hexdigest())


class AssertionTests(unittest.TestCase):
    def test_intersection_must_be_empty(self):
        with self.assertRaises(AssertionError):
            P.assert_lists_consistent([1, 2], [2, 3], [2],
                                      cohort_size=2, eval_size=1)

    def test_eval_must_be_subset(self):
        with self.assertRaises(AssertionError):
            P.assert_lists_consistent([1], [2, 3], [9],
                                      cohort_size=2, eval_size=1)

    def test_size_must_match_exactly(self):
        with self.assertRaises(AssertionError):
            P.assert_lists_consistent([], [1, 2], [1],
                                      cohort_size=3, eval_size=1)

    def test_duplicates_rejected(self):
        with self.assertRaises(AssertionError):
            P.assert_lists_consistent([], [1, 1], [1],
                                      cohort_size=2, eval_size=1)

    def test_happy_path(self):
        self.assertTrue(P.assert_lists_consistent([1, 2], [3, 4], [4],
                                                  cohort_size=2, eval_size=1))

    def test_assertions_raise_not_warn(self):
        """计划书要求断言失败抛错；这里锁死"不是 warning 后继续"。"""
        import warnings
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter('always')
            with self.assertRaises(AssertionError):
                P.assert_lists_consistent([1], [1], [1],
                                          cohort_size=1, eval_size=1)
        self.assertEqual(caught, [])


class LegacySliceTests(unittest.TestCase):
    def test_legacy_sampler_matches_old_implementation(self):
        import random
        users = list(range(1, 301))
        expected = set(random.Random(42).sample(sorted(users), 50))
        self.assertEqual(P.legacy_slice_users(users, 50, 42), expected)

    def test_legacy_constants_match_history(self):
        self.assertEqual(P.LEGACY_20K_ROWS, 3135106)
        self.assertEqual(P.LEGACY_20K_USERS, 20000)
        self.assertEqual(P.LEGACY_SEED, 42)

    def test_simulate_rows_counts_only_kept_users(self):
        folder = fixture_dir('legacy-count')
        path = os.path.join(folder, 'ratings.csv')
        write_ratings(path, [(1, 1, 5.0, 1), (2, 1, 4.0, 2), (1, 2, 3.0, 3)])
        self.assertEqual(P.simulate_legacy_slice_rows(path, {1}), 2)
        self.assertEqual(P.simulate_legacy_slice_rows(path, {'1'}), 2)
        self.assertEqual(P.simulate_legacy_slice_rows(path, {1, 2}), 3)


# ---------------------------------------------------------------------------
# 切分会话
# ---------------------------------------------------------------------------

class SplitSessionTests(unittest.TestCase):
    def _session(self, mode, rows, eval_users, chunk_rows, grouped=True):
        folder = fixture_dir('session-%s' % mode)
        path = write_ratings(os.path.join(folder, 'in.csv'), rows)
        session = C.RatingSession(path, folder, mode, eval_users, chunk_rows,
                                  assume_grouped_input=grouped)
        session.build()
        return folder, session

    def test_default_mode_uses_two_way_split_rounding(self):
        rows = synthetic_rows(20)
        for chunk in (5, 11, 400):
            folder, session = self._session(C.MODE_DEFAULT, rows,
                                            [u for u in range(1, 21)], chunk)
            train, test = temporal_split(rows, 0.2)
            self.assertEqual(session.counts['train_rows'], len(train))
            self.assertEqual(session.counts['holdout_rows'],
                             len([r for r in test if r[0] in set(range(1, 21))]))
            self.assertEqual(session.counts['legacy_reserved_test_rows'], 0)

    def test_chunk_size_does_not_change_split(self):
        rows = synthetic_rows(30)
        results = []
        for chunk in (3, 17, 1000):
            folder, session = self._session(C.MODE_DEFAULT, rows,
                                            list(range(1, 31)), chunk)
            results.append(session.counts['train_rows'])
        self.assertEqual(len(set(results)), 1)

    def test_interleaved_input_matches_grouped_input(self):
        """行序不影响切分：交错输入必须与分组输入给出同样的 train/holdout。"""
        grouped = synthetic_rows(25, grouped=True)
        interleaved = synthetic_rows(25, grouped=False)
        users = list(range(1, 26))
        baseline = None
        for rows in (grouped, interleaved):
            for chunk in (3, 7, 400):
                folder, session = self._session(
                    C.MODE_DEFAULT, rows, users, chunk, grouped=False)
                signature = (session.counts['train_rows'],
                             session.counts['holdout_rows'],
                             session.counts['dropped_holdout_rows'])
                if baseline is None:
                    baseline = signature
                self.assertEqual(signature, baseline,
                                 'rows_grouped=%s chunk=%d'
                                 % (rows is grouped, chunk))
        # 分组输入下开优化（默认路径）结果一致
        for chunk in (3, 7, 400):
            folder, session = self._session(C.MODE_DEFAULT, grouped, users, chunk,
                                            grouped=True)
            self.assertEqual((session.counts['train_rows'],
                              session.counts['holdout_rows'],
                              session.counts['dropped_holdout_rows']), baseline)

    def test_ungrouped_input_with_optimization_fails_loudly(self):
        """开了分组优化但输入没分组 -> 必须报错，绝不能静默给出错误切分。"""
        interleaved = synthetic_rows(25, grouped=False)
        folder = fixture_dir('ungrouped')
        path = write_ratings(os.path.join(folder, 'in.csv'), interleaved)
        session = C.RatingSession(path, folder, C.MODE_DEFAULT,
                                  list(range(1, 26)), 3,
                                  assume_grouped_input=True)
        with self.assertRaises(RuntimeError):
            session.build()

    def test_legacy_mode_uses_val_segment_as_holdout(self):
        from experiments.pilot.data import temporal_split_three_way
        rows = synthetic_rows(20)
        folder, session = self._session(C.MODE_LEGACY, rows,
                                        [u for u in range(1, 21)], 7)
        train, val, reserved = temporal_split_three_way(rows, 0.2, 0.2)
        self.assertEqual(session.counts['train_rows'], len(train))
        self.assertEqual(session.counts['holdout_rows'], len(val))
        self.assertEqual(session.counts['legacy_reserved_test_rows'], len(reserved))

    def test_non_eval_holdout_rows_are_dropped(self):
        rows = synthetic_rows(20)
        folder, session = self._session(C.MODE_DEFAULT, rows, [1, 2], 7)
        self.assertLess(session.counts['holdout_rows'],
                        session.counts['train_rows'])
        self.assertTrue(session.non_eval_holdout_users)

    def test_items_and_history_are_collected(self):
        rows = synthetic_rows(20)
        folder, session = self._session(C.MODE_DEFAULT, rows, [1, 2, 3], 7)
        item_ids, train_rows = C.build_index(session.iter_train())
        self.assertEqual(train_rows, session.counts['train_rows'])
        self.assertEqual(item_ids, sorted(set(item_ids)))
        seen, history, history_len = C.build_user_context(session.iter_train(),
                                                          [1, 2, 3])
        self.assertEqual(set(seen), {1, 2, 3})
        for user in (1, 2, 3):
            self.assertTrue(set(history[user]).issubset(seen[user]))
            self.assertEqual(history_len[user], len(history[user]))
            # 好评历史必须升序（浮点贡献按来源 movieId 升序累加）。
            self.assertEqual(list(history[user]), sorted(history[user]))


# ---------------------------------------------------------------------------
# 候选 / 指标口径
# ---------------------------------------------------------------------------

class MetricContractTests(unittest.TestCase):
    def test_out_of_candidate_positive_stays_in_denominator(self):
        """候选外正例仍计入 Recall 与 IDCG 分母（计划书 §3）。"""
        recs = [1, 2, 3]
        rel = {1, 999}
        recalls, ndcgs = C.per_user_metrics([recs], [rel], 20)
        self.assertAlmostEqual(recalls[0], 1.0 / 2.0, places=12)
        # IDCG 按 min(|R_u|, K) = 2 项计算：命中 1 项在位置 0。
        expected_idcg = 1.0 + 1.0 / np.log2(3.0)
        self.assertAlmostEqual(ndcgs[0], 1.0 / expected_idcg, places=12)

    def test_short_list_does_not_change_idcg(self):
        recalls_long, ndcgs_long = C.per_user_metrics([[1]], [{1, 2}], 20)
        recalls_short, ndcgs_short = C.per_user_metrics([[1, 3, 4, 5]],
                                                        [{1, 2}], 20)
        self.assertEqual(ndcgs_long[0], ndcgs_short[0])
        self.assertEqual(recalls_long[0], recalls_short[0])

    def test_empty_relevant_raises(self):
        with self.assertRaises(ValueError):
            C.per_user_metrics([[]], [set()], 20)

    def test_duplicate_recommendations_rejected(self):
        with self.assertRaises(ValueError):
            C.per_user_metrics([[1, 1]], [{1}], 20)

    def test_bootstrap_paired_same_index(self):
        a = [1.0, 1.0, 1.0, 0.0, 1.0, 0.0, 1.0, 1.0]
        b = [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        block = C.paired_bootstrap(a, b, seed=42, repeats=200)
        self.assertEqual(block['users'], 8)
        self.assertAlmostEqual(block['mean_difference'], 0.75, places=12)
        self.assertGreater(block['ci95'][0], 0.0)
        self.assertTrue(block['excludes_zero'])
        self.assertEqual(block['relative_difference'], None)

    def test_bootstrap_relative_difference_when_baseline_nonzero(self):
        a = [0.4, 0.6]
        b = [0.2, 0.3]
        block = C.paired_bootstrap(a, b, seed=42, repeats=100)
        self.assertAlmostEqual(block['relative_difference'], 0.25 / 0.25, places=12)

    def test_bootstrap_reproducible(self):
        a = [0.3, 0.1, 0.5, 0.2, 0.0]
        b = [0.2, 0.2, 0.4, 0.1, 0.1]
        self.assertEqual(C.paired_bootstrap(a, b, seed=42, repeats=100),
                         C.paired_bootstrap(a, b, seed=42, repeats=100))

    def test_verdict_rules_are_fixed(self):
        strong = {'ItemCF': {'ndcg@20': 0.2, 'recall@20': 0.3},
                  'MostPop': {'ndcg@20': 0.1, 'recall@20': 0.1},
                  'LikePop': {'ndcg@20': 0.12, 'recall@20': 0.11}}
        boot = {'ItemCF_vs_LikePop': {'ndcg@20': {'ci95': [0.01, 0.09]}}}
        self.assertEqual(C.verdict_of(strong, boot), C.VERDICT_SUPPORTED)
        crossing = {'ItemCF_vs_LikePop': {'ndcg@20': {'ci95': [-0.01, 0.09]}}}
        self.assertEqual(C.verdict_of(strong, crossing), C.VERDICT_CROSSES_ZERO)
        weak = {'ItemCF': {'ndcg@20': 0.05, 'recall@20': 0.05},
                'MostPop': {'ndcg@20': 0.1, 'recall@20': 0.1},
                'LikePop': {'ndcg@20': 0.12, 'recall@20': 0.11}}
        self.assertEqual(C.verdict_of(weak, boot), C.VERDICT_NOT_REPRODUCED)

    def test_verdict_metrics_disagreement_is_rule_2_not_rule_3(self):
        """方向不一致（NDCG 赢、Recall 输）必须落到规则 2，不是规则 3。

        Lead 用这份数字复现过：旧实现把它判成 ``baseline_advantage_not_reproduced``，
        把"存在权衡"误报成"未超过基线"。
        """
        metrics = {'ItemCF': {'ndcg@20': 0.130, 'recall@20': 0.100},
                   'MostPop': {'ndcg@20': 0.080, 'recall@20': 0.107},
                   'LikePop': {'ndcg@20': 0.079, 'recall@20': 0.104}}
        boot = {'ItemCF_vs_MostPop': {'ndcg@20': {'ci95': [0.04, 0.06]}}}
        verdict = C.verdict_of(metrics, boot)
        self.assertEqual(verdict, C.VERDICT_METRICS_DISAGREE)
        self.assertNotEqual(verdict, C.VERDICT_NOT_REPRODUCED)

    def test_verdict_recall_wins_ndcg_loses_is_also_rule_2(self):
        metrics = {'ItemCF': {'ndcg@20': 0.070, 'recall@20': 0.120},
                   'MostPop': {'ndcg@20': 0.080, 'recall@20': 0.107},
                   'LikePop': {'ndcg@20': 0.079, 'recall@20': 0.104}}
        self.assertEqual(C.verdict_of(metrics, {}), C.VERDICT_METRICS_DISAGREE)

    def test_verdict_beating_only_weaker_baseline_is_rule_2(self):
        """只赢较弱基线（LikePop 更强）也算权衡，不是"未超过基线"。"""
        metrics = {'ItemCF': {'ndcg@20': 0.110, 'recall@20': 0.105},
                   'MostPop': {'ndcg@20': 0.100, 'recall@20': 0.100},
                   'LikePop': {'ndcg@20': 0.120, 'recall@20': 0.110}}
        boot = {'ItemCF_vs_LikePop': {'ndcg@20': {'ci95': [0.01, 0.09]}}}
        self.assertEqual(C.verdict_of(metrics, boot), C.VERDICT_METRICS_DISAGREE)

    def test_verdict_rule_1_uses_stronger_baseline_interval(self):
        """规则 1 的区间下界判定必须针对**较强**基线（按 NDCG@20 选）。"""
        metrics = {'ItemCF': {'ndcg@20': 0.200, 'recall@20': 0.300},
                   'MostPop': {'ndcg@20': 0.100, 'recall@20': 0.100},
                   'LikePop': {'ndcg@20': 0.150, 'recall@20': 0.120}}
        # 对较弱基线（MostPop）的区间下界 > 0，但对较强基线（LikePop）跨零。
        boot = {'ItemCF_vs_MostPop': {'ndcg@20': {'ci95': [0.05, 0.15]}},
                'ItemCF_vs_LikePop': {'ndcg@20': {'ci95': [-0.01, 0.11]}}}
        self.assertEqual(C.verdict_of(metrics, boot), C.VERDICT_CROSSES_ZERO)

    def test_verdict_failure_stays_failure(self):
        self.assertEqual(C.verdict_of({}, {}), C.VERDICT_FAILURE)
        self.assertEqual(C.verdict_of({'ItemCF': {'ndcg@20': 1.0}}, {}),
                         C.VERDICT_FAILURE)


class BaselineOrderTests(unittest.TestCase):
    def test_mostpop_tie_breaks_by_movie_id(self):
        order = C.most_popular_order({5: 3, 2: 3, 9: 1}, [2, 5, 9])
        self.assertEqual(order, [2, 5, 9])

    def test_likepop_tie_breaks_by_movie_id_and_keeps_zero_items(self):
        order = C.like_popular_order({5: 2, 2: 2}, [2, 5, 9])
        self.assertEqual(order, [2, 5, 9])

    def test_top_n_excluding_uses_single_candidate_universe(self):
        order = [1, 2, 3, 4, 5]
        self.assertEqual(C.top_n_excluding(order, {1, 2}, 2), [3, 4])
        self.assertEqual(C.top_n_excluding(order, set(), 20), [1, 2, 3, 4, 5])


# ---------------------------------------------------------------------------
# 图：分块一致性 + 存储回环 + 遍历方向
# ---------------------------------------------------------------------------

def _gold_train_iter(rows, chunk):
    return [rows[i:i + chunk] for i in range(0, len(rows), chunk)]


class GraphTests(unittest.TestCase):
    def _gold(self):
        return [
            (1, 2, 5.0, 100), (1, 10, 4.5, 110), (1, 42, 2.0, 120),
            (2, 2, 4.0, 130), (2, 5, 5.0, 140),
            (3, 2, 5.0, 150), (3, 10, 4.0, 160), (3, 5, 4.5, 170),
        ]

    def _richer(self):
        """更稠密的合成训练段：保证跨块共现存在（块大小不变性才有意义）。"""
        rows = []
        for user in range(1, 26):
            for offset in range(4):
                movie = 1 + (user * 2 + offset * 7) % 30
                rows.append((user, movie, 5.0, user * 100 + offset))
        # 去重 (user, movie)
        seen = set()
        unique = []
        for row in rows:
            if (row[0], row[1]) in seen:
                continue
            seen.add((row[0], row[1]))
            unique.append(row)
        return unique

    def _graph(self, rows, chunk=128, block=128, max_k=100):
        item_ids, _rows = C.build_index(_gold_train_iter(rows, chunk))
        graph, _n = C.build_graph_csr(_gold_train_iter(rows, chunk), item_ids,
                                      beta=0.0, max_k=max_k, block_size=block)
        return item_ids, graph

    def test_block_size_invariance(self):
        rows = self._richer()
        _items, base = self._graph(rows, chunk=128, block=128)
        self.assertGreater(base.n_edges, 0)
        for block in (1, 2, 3, 7):
            _items2, other = self._graph(rows, chunk=128, block=block)
            self.assertEqual(base.n_edges, other.n_edges)
            self.assertTrue(np.array_equal(base.sources, other.sources))
            self.assertTrue(np.array_equal(base.targets, other.targets))
            self.assertTrue(np.allclose(base.similarities, other.similarities))

    def test_chunk_size_invariance(self):
        """训练行分块**不能**改变共现：跨块的用户好评必须计入 c_ij。"""
        rows = self._richer()
        _items, base = self._graph(rows, chunk=128, block=128)
        self.assertGreater(base.n_edges, 0)
        for chunk in (1, 3, 5, 11):
            _items2, other = self._graph(rows, chunk=chunk, block=128)
            self.assertEqual(base.n_edges, other.n_edges,
                             'chunk=%d changed edge count' % chunk)
            self.assertTrue(np.array_equal(base.sources, other.sources))
            self.assertTrue(np.array_equal(base.targets, other.targets))
            self.assertTrue(np.allclose(base.similarities, other.similarities))

    def test_graph_matches_whole_data_reference(self):
        """新构图必须与"整份数据一次构图"（core 直接调用）逐边一致。"""
        from experiments.itemcf.core import (build_neighbors,
                                             build_positive_matrix)
        rows = self._richer()
        item_ids, _r = C.build_index([rows])
        X, core_items, _u = build_positive_matrix(rows)
        reference = build_neighbors(X, core_items, beta=0.0, max_k=100,
                                    block_size=128)
        _items, graph = self._graph(rows, chunk=7, block=5)
        mine = {}
        for source in graph.item_ids:
            targs, sims, coocs = graph.edges_of_source(int(source))
            for pos in range(len(targs)):
                mine[(int(source), int(targs[pos]))] = float(sims[pos])
        self.assertEqual(len(mine), len(reference))
        for key, edge in reference.items():
            self.assertIn(key, mine)
            self.assertAlmostEqual(mine[key], float(edge[1]), places=12)

    def test_gold_cosine_values(self):
        items, graph = self._graph(self._gold(), max_k=100)
        cos_ab = 2.0 / np.sqrt(6.0)
        edges = {}
        for source in items:
            targs, sims, _c = graph.edges_of_source(source)
            for pos in range(len(targs)):
                edges[(source, int(targs[pos]))] = float(sims[pos])
        self.assertAlmostEqual(edges[(2, 10)], cos_ab, places=12)
        self.assertAlmostEqual(edges[(2, 5)], cos_ab, places=12)
        self.assertAlmostEqual(edges[(10, 5)], 0.5, places=12)

    def test_storage_roundtrip_preserves_order(self):
        items, graph = self._graph(self._gold(), max_k=100)
        folder = fixture_dir('roundtrip')
        report = C.graph_roundtrip(graph, os.path.join(folder, 'n.npz'),
                                   os.path.join(folder, 'n.meta.json'))
        self.assertTrue(report['consistent'])
        self.assertTrue(report['ordering_is_similarity_desc'])
        self.assertEqual(report['nonpositive_similarities'], 0)

    def test_roundtrip_detects_unsorted_storage(self):
        """存取回环必须能发现"没按相似度排序"的存储（计划书已知陷阱）。"""
        graph = C.NeighborGraph(
            sources=[1, 1], targets=[5, 6], similarities=[0.1, 0.9],
            cooccurrences=[1, 1], item_ids=[1, 5, 6], n_like=[2, 1, 1])
        folder = fixture_dir('roundtrip-bad')
        report = C.graph_roundtrip(graph, os.path.join(folder, 'n.npz'),
                                   os.path.join(folder, 'n.meta.json'))
        self.assertFalse(report['ordering_is_similarity_desc'])
        self.assertFalse(report['consistent'])

    def test_storage_sorts_by_similarity_then_target(self):
        rows = self._gold()
        _items, graph = self._graph(rows, max_k=100)
        for source in graph.item_ids:
            targs, sims, _c = graph.edges_of_source(int(source))
            pairs = list(zip(sims.tolist(), targs.tolist()))
            self.assertEqual(pairs, sorted(pairs, key=lambda t: (-t[0], t[1])))

    def test_max_k_truncation_keeps_prefix(self):
        rows = self._gold()
        _items, small = self._graph(rows, max_k=1)
        _items2, large = self._graph(rows, max_k=100)
        for source in small.item_ids:
            small_targets = small.edges_of_source(int(source))[0].tolist()
            if not small_targets:
                continue
            large_targets = large.edges_of_source(int(source))[0].tolist()
            self.assertEqual(small_targets, large_targets[:1])

    def test_edges_are_directed(self):
        """用户只好评 {3} 时不能走到不存在的入边（图是有向的）。"""
        rows = [(1, 1, 5.0, 1), (1, 2, 5.0, 1), (1, 3, 5.0, 1),
                (2, 1, 5.0, 2), (2, 2, 5.0, 2),
                (3, 1, 5.0, 3), (3, 3, 5.0, 3)]
        _items, graph = self._graph(rows, max_k=1)
        recs = C.score_itemcf_user(array.array('i', [3]), graph, set(), 10)
        self.assertEqual([r['movie_id'] for r in recs], [1])


class ScoreUserTests(unittest.TestCase):
    def _setup(self):
        rows = [(1, 2, 5.0, 100), (1, 10, 4.5, 110), (1, 42, 2.0, 120),
                (2, 2, 4.0, 130), (2, 5, 5.0, 140),
                (3, 2, 5.0, 150), (3, 10, 4.0, 160), (3, 5, 4.5, 170)]
        item_ids, _r = C.build_index(_gold_train_iter(rows, 128))
        graph, _n = C.build_graph_csr(_gold_train_iter(rows, 128), item_ids,
                                      beta=0.0, max_k=100, block_size=128)
        return graph

    def test_low_rated_item_excluded_but_history_liked(self):
        graph = self._setup()
        recs = C.score_itemcf_user(array.array('i', [2, 10]), graph,
                                   {2, 10, 42}, 20)
        self.assertEqual([r['movie_id'] for r in recs], [5])

    def test_no_positive_contribution_returns_empty(self):
        graph = self._setup()
        self.assertEqual(C.score_itemcf_user(array.array('i', []), graph,
                                             set(), 20), [])
        self.assertEqual(C.score_itemcf_user(array.array('i', [42]), graph,
                                             set(), 20), [])

    def test_short_list_not_padded(self):
        graph = self._setup()
        recs = C.score_itemcf_user(array.array('i', [2, 10]), graph,
                                   {2, 10, 42}, 20)
        self.assertEqual(len(recs), 1)

    def test_evidence_fields_and_order(self):
        graph = self._setup()
        recs = C.score_itemcf_user(array.array('i', [2, 10]), graph,
                                   {2, 10, 42}, 20)
        self.assertEqual(recs[0]['evidence_count'], 2)
        self.assertAlmostEqual(recs[0]['contrib_sum'], recs[0]['score'], places=12)
        self.assertIn(recs[0]['top_source_movie_id'], (2, 10))

    def test_scores_match_reference_core_score_user(self):
        """新入口打分必须与口径唯一的 core.score_user 完全一致。"""
        from experiments.itemcf.core import (build_neighbors,
                                             build_positive_matrix, score_user)
        rows = [(1, 2, 5.0, 100), (1, 10, 4.5, 110), (1, 42, 2.0, 120),
                (2, 2, 4.0, 130), (2, 5, 5.0, 140),
                (3, 2, 5.0, 150), (3, 10, 4.0, 160), (3, 5, 4.5, 170),
                (4, 5, 5.0, 180), (4, 10, 5.0, 190), (4, 2, 4.0, 200)]
        X, item_list, _users = build_positive_matrix(rows)
        reference = build_neighbors(X, item_list, beta=0.0, max_k=100, block_size=128)
        item_ids, _r = C.build_index(_gold_train_iter(rows, 128))
        graph, _n = C.build_graph_csr(_gold_train_iter(rows, 128), item_ids,
                                      beta=0.0, max_k=100, block_size=128)
        ref_recs = score_user({2, 10}, reference, {2, 10, 42}, 20)
        new_recs = C.score_itemcf_user(array.array('i', [2, 10]), graph,
                                       {2, 10, 42}, 20)
        self.assertEqual([r['movie_id'] for r in ref_recs],
                         [r['movie_id'] for r in new_recs])
        for ref, new in zip(ref_recs, new_recs):
            self.assertAlmostEqual(ref['score'], new['score'], places=12)
            self.assertEqual(ref['evidence_count'], new['evidence_count'])
            self.assertEqual(ref['top_source_movie_id'], new['top_source_movie_id'])
            self.assertEqual(ref['top_source_c'], new['top_source_c'])


# ---------------------------------------------------------------------------
# 内存契约（结构性，不靠"看起来没问题"）
# ---------------------------------------------------------------------------

class MemoryContractTests(unittest.TestCase):
    def test_neighbor_graph_has_one_array_per_edge(self):
        """图必须用并行紧凑数组保存，而不是 dict-of-list-of-dict。"""
        rows = synthetic_rows(30)
        item_ids, _r = C.build_index(_gold_train_iter(rows, 128))
        graph, _n = C.build_graph_csr(_gold_train_iter(rows, 128), item_ids,
                                      beta=0.0, max_k=100, block_size=16)
        self.assertEqual(graph.sources.dtype, np.int64)
        self.assertEqual(graph.targets.dtype, np.int64)
        self.assertEqual(graph.similarities.dtype, np.float64)
        self.assertEqual(graph.cooccurrences.dtype, np.int64)
        self.assertEqual(len(graph.indptr), len(graph.item_ids) + 1)

    def test_single_candidate_universe_is_reused(self):
        """候选全集只有一份：三个方法都从同一份 item_ids 派生。"""
        rows = synthetic_rows(30)
        item_ids, _r = C.build_index(_gold_train_iter(rows, 128))
        order_most = C.most_popular_order({i: i for i in item_ids}, item_ids)
        order_like = C.like_popular_order({i: i for i in item_ids}, item_ids)
        self.assertEqual(set(order_most), set(item_ids))
        self.assertEqual(set(order_like), set(item_ids))
        # 逐用户排除 seen 时没有复制候选集合：top_n_excluding 只消费原列表。
        seen = set(item_ids[:5])
        picked = C.top_n_excluding(order_most, seen, 3)
        self.assertEqual(len(picked), 3)
        self.assertFalse(set(picked) & seen)

    def test_score_user_does_not_scan_whole_graph(self):
        """逐用户只遍历其好评来源的邻居：来源外的边不参与。"""
        graph = C.NeighborGraph(
            sources=[1, 1, 7], targets=[2, 3, 9],
            similarities=[0.5, 0.4, 0.9], cooccurrences=[1, 1, 1],
            item_ids=[1, 2, 3, 7, 9], n_like=[1, 1, 1, 1, 1])
        recs = C.score_itemcf_user(array.array('i', [1]), graph, set(), 20)
        self.assertEqual([r['movie_id'] for r in recs], [2, 3])
        # 来源 7 的边（0.9，比两条都大）不属于用户历史，不得出现。
        self.assertNotIn(9, [r['movie_id'] for r in recs])

    def test_split_session_writes_disk_middleware(self):
        """切分中间件落盘，避免同时持有多份四元组全集。"""
        folder = fixture_dir('middleware')
        path = write_ratings(os.path.join(folder, 'in.csv'), synthetic_rows(20))
        session = C.RatingSession(path, folder, C.MODE_DEFAULT,
                                  list(range(1, 21)), 7)
        session.build()
        self.assertTrue(os.path.isfile(session.train_path))
        self.assertTrue(os.path.isfile(session.holdout_path))
        rows_on_disk = sum(len(chunk) for chunk in session.iter_train())
        self.assertEqual(rows_on_disk, session.counts['train_rows'])
        holdout_on_disk = sum(len(chunk) for chunk in session.iter_holdout())
        self.assertEqual(holdout_on_disk, session.counts['holdout_rows'])


# ---------------------------------------------------------------------------
# 模式区分 / CLI
# ---------------------------------------------------------------------------

class ModeSeparationTests(unittest.TestCase):
    def test_default_mode_is_the_confirmation_protocol(self):
        args = C.parse_args(['--input', 'a.csv', '--out', 'o', '--users', 'u'])
        self.assertEqual(args.mode, C.MODE_DEFAULT)
        self.assertEqual(C.MODE_DEFAULT, 'default')

    def test_legacy_mode_requires_explicit_flag(self):
        args = C.parse_args(['--input', 'a.csv', '--out', 'o', '--users', 'u',
                             '--mode', 'legacy-3way'])
        self.assertEqual(args.mode, C.MODE_LEGACY)

    def test_unknown_mode_rejected(self):
        with self.assertRaises(SystemExit):
            C.parse_args(['--input', 'a.csv', '--out', 'o', '--users', 'u',
                          '--mode', 'three-way'])

    def test_fixed_config_defaults_match_plan(self):
        args = C.parse_args(['--input', 'a.csv', '--out', 'o', '--users', 'u'])
        self.assertEqual(args.max_k, 100)
        self.assertEqual(args.beta, 0.0)
        self.assertEqual(args.top_n, 20)
        self.assertEqual(args.block_size, 128)
        self.assertEqual(C.BOOTSTRAP_SEED, 42)
        self.assertEqual(C.BOOTSTRAP_REPEATS, 2000)
        self.assertEqual(C.POSITIVE_THRESHOLD, 4.0)

    def test_budget_constants_match_plan(self):
        self.assertEqual(C.BUDGET_SECONDS, 7200)
        self.assertEqual(C.RSS_LIMIT_BYTES, 3 * 1024 ** 3)
        self.assertEqual(C.MEM_AVAILABLE_FLOOR_BYTES, 256 * 1024 ** 2)
        self.assertEqual(C.MEM_AVAILABLE_STREAK_SECONDS, 30)

    def test_modes_use_different_split_functions(self):
        """两种模式必须走不同切分，绝不能互相冒充。"""
        rows = synthetic_rows(20)
        folder = fixture_dir('mode-diff')
        path = write_ratings(os.path.join(folder, 'in.csv'), rows)
        users = list(range(1, 21))
        default = C.RatingSession(path, fixture_dir('m-default'), C.MODE_DEFAULT,
                                  users, 400).build()
        legacy = C.RatingSession(path, fixture_dir('m-legacy'), C.MODE_LEGACY,
                                 users, 400).build()
        self.assertNotEqual(default.counts['train_rows'],
                            legacy.counts['train_rows'])
        self.assertGreater(legacy.counts['legacy_reserved_test_rows'], 0)
        self.assertEqual(default.counts['legacy_reserved_test_rows'], 0)


class ResourceMonitorTests(unittest.TestCase):
    def test_peak_and_dump(self):
        folder = fixture_dir('monitor')
        monitor = C.ResourceMonitor(C.current_process_tree_pids(),
                                   os.path.join(folder, 'resources.csv'))
        monitor.sample()
        monitor.sample()
        self.assertEqual(monitor.peak()['samples'], 2)
        path = monitor.dump()
        self.assertTrue(os.path.isfile(path))
        with open(path, newline='', encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 2)

    def test_rss_limit_triggers(self):
        folder = fixture_dir('monitor-rss')
        monitor = C.ResourceMonitor([os.getpid()],
                                   os.path.join(folder, 'r.csv'))
        reason = monitor.check({'rss_bytes': C.RSS_LIMIT_BYTES + 1,
                                'mem_available_bytes': None})
        self.assertIsNotNone(reason)
        self.assertIn('RSS', reason)

    def test_low_memory_needs_continuous_streak(self):
        folder = fixture_dir('monitor-mem')
        monitor = C.ResourceMonitor([os.getpid()],
                                   os.path.join(folder, 'r.csv'))
        low = {'rss_bytes': 1, 'mem_available_bytes': 1}
        self.assertIsNone(monitor.check(low))
        monitor.low_mem_since = time.time() - (C.MEM_AVAILABLE_STREAK_SECONDS + 1)
        self.assertIsNotNone(monitor.check(low))

    def test_recovering_memory_resets_streak(self):
        folder = fixture_dir('monitor-mem-ok')
        monitor = C.ResourceMonitor([os.getpid()],
                                   os.path.join(folder, 'r.csv'))
        monitor.check({'rss_bytes': 1, 'mem_available_bytes': 1})
        self.assertIsNotNone(monitor.low_mem_since)
        monitor.check({'rss_bytes': 1,
                       'mem_available_bytes': C.MEM_AVAILABLE_FLOOR_BYTES * 10})
        self.assertIsNone(monitor.low_mem_since)

    def test_budget_exceeded(self):
        folder = fixture_dir('monitor-budget')
        monitor = C.ResourceMonitor([os.getpid()],
                                   os.path.join(folder, 'r.csv'),
                                   budget_seconds=0.0)
        self.assertIsNotNone(monitor.check({'rss_bytes': 1,
                                            'mem_available_bytes': None}))

    def test_only_own_pid_is_tracked(self):
        monitor = C.ResourceMonitor([os.getpid()], 'x.csv')
        self.assertEqual(monitor.pids, [os.getpid()])


# ---------------------------------------------------------------------------
# 端到端小规模
# ---------------------------------------------------------------------------

class EndToEndTests(unittest.TestCase):
    def _prepare(self, rows, cohort_users, eval_users, folder=None, out=None):
        folder = folder or fixture_dir('e2e-prepare')
        src = os.path.join(folder, 'full.csv')
        if not os.path.isfile(src):
            write_ratings(src, rows)
        pilot = os.path.join(folder, 'pilot.csv')
        twenty = os.path.join(folder, 'twenty.csv')
        if not os.path.isfile(pilot):
            write_ratings(pilot, [r for r in rows if r[0] in (1, 2)])
            write_ratings(twenty, [r for r in rows if r[0] in (3, 4)])
        out = out or os.path.join(folder, 'prep')
        args = P.parse_args(['--input', src, '--out', out,
                             '--pilot-slice', pilot,
                             '--pilot-20k-slice', twenty,
                             '--cohort-size', str(len(cohort_users)),
                             '--eval-size', str(len(eval_users)),
                             '--project-root', folder])
        manifest = P.run(args)
        return folder, out, manifest

    def test_prepare_produces_all_list_artifacts(self):
        rows = synthetic_rows(40)
        # 排除名单 = 切片用户 {1,2,3,4}；cohort 从其余 36 人中选。
        folder, out, manifest = self._prepare(rows, list(range(5, 25)),
                                              list(range(5, 15)))
        for name in (P.COHORT_FILE, P.EVAL_FILE, P.EXCLUDED_FILE, P.USERS_JSON,
                     P.SOURCE_HASHES_FILE, 'manifest.json', 'prepare.log',
                     'cohort_ratings.csv'):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertEqual(manifest['status'], 'success')
        self.assertTrue(manifest['assertions']['excluded_cohort_intersection_empty'])
        self.assertTrue(manifest['assertions']['eval_subset_of_cohort'])

    def test_prepare_refuses_overwrite(self):
        rows = synthetic_rows(40)
        folder, out, _manifest = self._prepare(rows, list(range(5, 25)),
                                               list(range(5, 15)))
        with self.assertRaises(RuntimeError):
            self._prepare(rows, list(range(5, 25)), list(range(5, 15)),
                          folder=folder, out=out)

    def test_prepare_cohort_slice_only_contains_cohort_users(self):
        rows = synthetic_rows(40)
        folder, out, manifest = self._prepare(rows, list(range(5, 25)),
                                              list(range(5, 15)))
        cohort = set(json.load(open(os.path.join(out, P.USERS_JSON),
                                    encoding='utf-8')) and
                     [int(x) for x in open(os.path.join(out, P.COHORT_FILE),
                                           encoding='utf-8')])
        with open(os.path.join(out, 'cohort_ratings.csv'), newline='',
                  encoding='utf-8') as handle:
            users = set(int(r['userId']) for r in csv.DictReader(handle))
        self.assertEqual(users, cohort)

    def _run_confirm(self, mode, n_users=40, top_n=5, chunk=7, max_k=100):
        rows = synthetic_rows(n_users)
        cohort = list(range(1, n_users + 1))
        folder = fixture_dir('e2e-confirm-%s' % mode)
        src = write_ratings(os.path.join(folder, 'in.csv'), rows)
        out = os.path.join(folder, 'run')
        args = C.parse_args(['--input', src, '--out', out,
                             '--users', folder, '--mode', mode,
                             '--top-n', str(top_n), '--max-k', str(max_k),
                             '--chunk-rows', str(chunk)])
        # 名单：目录里需要 cohort_users.txt / eval_users.txt
        P.write_user_list(os.path.join(folder, P.COHORT_FILE), cohort)
        P.write_user_list(os.path.join(folder, P.EVAL_FILE), cohort)
        status = C.run(args)
        return out, status

    def test_confirm_produces_all_artifacts(self):
        out, status = self._run_confirm(C.MODE_DEFAULT)
        for name in ('run.log', 'resources.csv', 'run_status.json',
                     'source_hashes.json', 'manifest.json', 'bootstrap.json',
                     'verification.json', 'per_user.csv', 'recommendations.csv',
                     'summary_table.csv', 'conclusion.json', 'conclusion.md',
                     'neighbors.npz', 'neighbors.meta.json', 'split_train.csv',
                     'split_holdout.csv'):
            self.assertTrue(os.path.isfile(os.path.join(out, name)), name)
        self.assertEqual(status['status'], 'success')
        self.assertEqual(status['mode'], C.MODE_DEFAULT)
        self.assertTrue(status['is_default_protocol'])
        self.assertEqual(set(status['metrics']), set(C.METHODS))

    def test_confirm_summary_table_has_three_rows(self):
        out, _status = self._run_confirm(C.MODE_DEFAULT)
        with open(os.path.join(out, 'summary_table.csv'), newline='',
                  encoding='utf-8') as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual([r['method'] for r in rows], list(C.METHODS))

    def test_confirm_three_methods_share_users_and_input(self):
        out, status = self._run_confirm(C.MODE_DEFAULT)
        with open(os.path.join(out, 'per_user.csv'), newline='',
                  encoding='utf-8') as handle:
            per_user = list(csv.DictReader(handle))
        users = [int(r['user_id']) for r in per_user]
        self.assertEqual(users, sorted(users))
        with open(os.path.join(out, 'recommendations.csv'), newline='',
                  encoding='utf-8') as handle:
            recs = list(csv.DictReader(handle))
        rec_users = dict((m, set()) for m in C.METHODS)
        for row in recs:
            rec_users[row['model']].add(int(row['user_id']))
        for name in C.METHODS:
            self.assertEqual(rec_users[name], set(users))

    def test_confirm_verification_has_roundtrip_and_no_duplicates(self):
        out, _status = self._run_confirm(C.MODE_DEFAULT)
        with open(os.path.join(out, 'verification.json'), encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertTrue(payload['neighbor_roundtrip']['consistent'])
        for name in C.METHODS:
            self.assertTrue(payload['duplicates'][name]['ok'])

    def test_confirm_refuses_overwrite(self):
        rows = synthetic_rows(30)
        folder = fixture_dir('e2e-overwrite')
        src = write_ratings(os.path.join(folder, 'in.csv'), rows)
        P.write_user_list(os.path.join(folder, P.COHORT_FILE), list(range(1, 31)))
        P.write_user_list(os.path.join(folder, P.EVAL_FILE), list(range(1, 31)))
        out = os.path.join(folder, 'run')
        base = ['--input', src, '--out', out, '--users', folder,
                '--top-n', '5', '--chunk-rows', '7']
        C.run(C.parse_args(base))
        with self.assertRaises(RuntimeError):
            C.run(C.parse_args(base))

    def test_legacy_mode_records_explicit_comparison(self):
        out, status = self._run_confirm(C.MODE_LEGACY)
        self.assertEqual(status['mode'], C.MODE_LEGACY)
        self.assertFalse(status['is_default_protocol'])
        with open(os.path.join(out, 'verification.json'), encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertIn('legacy_comparison', payload)
        self.assertEqual(payload['legacy_comparison']['expected_users'],
                         C.FIX14_USERS)
        # 对拍由独立实现（equiv_check.py）负责，本模块不重复实现对拍逻辑。
        self.assertEqual(payload['legacy_comparison']['delegated_to'],
                         'experiments/itemcf/equiv_check.py')

    def test_confirm_has_no_self_comparison_path(self):
        """confirm.py 不得内置对拍比较（避免两套口径 / 自我确认）。"""
        self.assertFalse(hasattr(C, 'compare_with_fix14'))

    def test_default_mode_has_no_legacy_comparison(self):
        out, _status = self._run_confirm(C.MODE_DEFAULT)
        with open(os.path.join(out, 'verification.json'), encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertIsNone(payload['legacy_comparison'])

    def test_conclusion_mentions_limitations(self):
        out, _status = self._run_confirm(C.MODE_DEFAULT)
        with open(os.path.join(out, 'conclusion.md'), encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('限制', text)
        self.assertIn('ItemCF', text)

    def test_items_never_recommended_before_rated(self):
        """留出段评分绝不进入训练：推荐里不得出现该用户训练段已评电影。"""
        out, _status = self._run_confirm(C.MODE_DEFAULT, n_users=40, top_n=5)
        rows = synthetic_rows(40)
        train_by_user = {}
        for row in rows:
            train_by_user.setdefault(row[0], set())
        with open(os.path.join(out, 'split_train.csv'), newline='',
                  encoding='utf-8') as handle:
            for record in csv.reader(handle):
                train_by_user[int(record[0])].add(int(record[1]))
        with open(os.path.join(out, 'recommendations.csv'), newline='',
                  encoding='utf-8') as handle:
            for row in csv.DictReader(handle):
                user = int(row['user_id'])
                self.assertNotIn(int(row['movie_id']), train_by_user.get(user, set()))


if __name__ == '__main__':
    unittest.main()
