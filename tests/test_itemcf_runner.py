# -*- coding: utf-8 -*-
"""ItemCF 运行编排测试（计划书 7.2 节：序列化 / 运行失败 / 完整合成管线）。

运行器测试用合成小数据 + 桩邻居图（core.py 由并行任务实现，本模块不 import 它的
实现细节）；reused ALS 对拍针对仓库内已核验产物目录（缺失时显式 N/A）。

对照方法入口（计划书 4.1）的用例同样落在这里：MostPop / LikePop / BiasOnly 三个
基线的入口、ALS 复用的协议校验与逐用户重算、summary_table.csv 的列口径与选参规则。
"""
import csv
import inspect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf import run_itemcf
from experiments.itemcf.run_itemcf import (
    GRID_DEFAULTS,
    build_commands,
    cache_identity,
    cache_matches,
    load_rows_csv,
    parse_args,
    validate_rows,
    write_summary_atomic,
    read_summary_checked,
    TopRecommendationAccumulator,
    run_repeat_comparison,
    build_shuffle_donors,
    shuffled_histories,
    source_hashes,
)

#: 对照方法入口的固定顺序（计划书 4.1 的全部方法；ItemCF 9 配置随后追加）。
COMPARISON_METHOD_ORDER = ['MostPop', 'LikePop', 'BiasOnly', 'ALS-ref',
                           'ALS-best-ranking']

#: summary_table.csv 的必备列（计划书 11 节 + 4.1 对照列）。
REQUIRED_TABLE_COLUMNS = ['method', 'config', 'status', 'ndcg@20', 'recall@20',
                          'delta_ndcg_vs_mostpop', 'full_list_rate',
                          'catalog_coverage', 'support_median', 'cost_seconds',
                          'code_id', 'rmse', 'out_of_range_rate']

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REUSE_GRID = os.path.join(PROJECT, 'experiments', 'pilot', 'out', 'grid', '20261003T125319Z')
LOCAL_INPUT = os.path.join(PROJECT, 'data', 'processed', 'pilot_ratings.csv')

# 测试临时目录落在工作区内（系统 %TEMP% 的清理受沙箱 ACL 影响会报 WinError 5）。
# 每个用例用唯一目录、不自动清理，避免沙箱对 rmtree/chmod 的限制打断测试。
FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_tmp')

#: 目录名唯一化用的单调计数器：``time.time_ns()`` 在 Windows 上的实际分辨率约
#: 100 ns，同一测试进程里连续调用会撞名（FileExistsError），所以再叠一个计数器。
_FIXTURE_LOCK = threading.Lock()
_FIXTURE_SEQ = [0]


class _PersistentDir(object):
    def __init__(self, path):
        self.name = path

    def __enter__(self):
        return self.name

    def __exit__(self, *exc):
        return False


def fixture_dir(prefix):
    """建唯一临时目录；重名时递增序号重试，不因目录已存在而失败。"""
    if not os.path.isdir(FIXTURE_ROOT):
        try:
            os.makedirs(FIXTURE_ROOT)
        except OSError:
            if not os.path.isdir(FIXTURE_ROOT):
                raise
    last_error = None
    for _attempt in range(100):
        with _FIXTURE_LOCK:
            _FIXTURE_SEQ[0] += 1
            seq = _FIXTURE_SEQ[0]
        path = os.path.join(FIXTURE_ROOT, '%s%d_%d' % (prefix, time.time_ns(), seq))
        try:
            os.makedirs(path)
        except OSError as error:
            last_error = error
            continue
        return _PersistentDir(path)
    raise last_error


def write_input(rows, path):
    with open(path, 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['userId', 'movieId', 'rating', 'timestamp'])
        writer.writerows(rows)
    return path


def synth_rows():
    """合成 4 用户数据：切分/评测/管线都用它，不依赖真实切片。

    每人 20 条、时间戳递增，切分后每人保留测试 4 条 / 验证 4 条 / 训练 12 条。
    物品池按用户错开（``100*u + n`` 的重叠部分只留少量共同片），这样
    ``C_u = I_train - Seen_u`` 非空——如果所有用户评的是同一批 movieId，
    候选集会全空，合成管线写出的推荐明细就是 0 行，测试退化成只验证"文件存在"。
    验证段留出 rating>=4.0 的正例，保证评测用户集非空。
    """
    rows = []
    for u in range(1, 5):
        for n in range(20):
            # 每人 12 部独有片 + 8 部与相邻用户共享的片，制造共同好评。
            movie = 1000 * u + n if n < 12 else 100 + n + (u - 1) * 30
            rows.append((u, movie, 5.0 if n % 3 else 2.0, 1000 + u * 100 + n))
    return rows


class InputAndValidationTests(unittest.TestCase):
    """计划书 2.1：输入格式与拒收规则。"""

    def test_load_rows_csv_reads_header(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'in.csv')
            write_input(synth_rows(), path)
            rows = load_rows_csv(path)
        self.assertEqual(rows[0], (1, 1000, 2.0, 1100))
        self.assertEqual(len(rows), 80)

    def test_duplicate_user_item_rejected(self):
        rows = [(1, 1, 5.0, 1), (1, 1, 4.0, 2)]
        with self.assertRaises(ValueError):
            validate_rows(rows)

    def test_non_finite_rating_rejected(self):
        with self.assertRaises(ValueError):
            validate_rows([(1, 1, float('nan'), 1)])
        with self.assertRaises(ValueError):
            validate_rows([(1, 1, float('inf'), 1)])

    def test_out_of_half_step_rating_rejected(self):
        with self.assertRaises(ValueError):
            validate_rows([(1, 1, 4.25, 1)])

    def test_valid_rows_pass(self):
        validate_rows(synth_rows())


class CommandTests(unittest.TestCase):
    """计划书 10.2：CLI 契约。"""

    def test_grid_cli_defaults(self):
        args = parse_args(['--input', 'x.csv', '--out', 'out', '--mode', 'grid',
                           '--neighbor-k', '20', '50', '100', '--beta', '0', '10', '50'])
        self.assertEqual(args.neighbor_k, [20, 50, 100])
        self.assertEqual(args.beta, [0, 10, 50])
        self.assertEqual(args.block_size, GRID_DEFAULTS['block_size'])
        self.assertEqual(args.positive_threshold, GRID_DEFAULTS['positive_threshold'])
        self.assertEqual(args.test_frac, GRID_DEFAULTS['test_frac'])
        self.assertEqual(args.val_frac, GRID_DEFAULTS['val_frac'])
        self.assertEqual(args.top_n, GRID_DEFAULTS['top_n'])
        self.assertEqual(args.seed, GRID_DEFAULTS['seed'])

    def test_repeat_and_shuffle_require_selected_config(self):
        for mode in ('repeat', 'shuffle'):
            with self.assertRaises(SystemExit):
                parse_args(['--input', 'x', '--out', 'o', '--mode', mode])
        args = parse_args(['--input', 'x', '--out', 'o', '--mode', 'repeat',
                           '--selected-config', 'sel.json'])
        self.assertEqual(args.selected_config, 'sel.json')

    def test_grid_rejects_selected_config(self):
        with self.assertRaises(SystemExit):
            parse_args(['--input', 'x', '--out', 'o', '--mode', 'grid',
                        '--selected-config', 'sel.json'])

    def test_build_commands_covers_3x3_grid(self):
        commands = build_commands('spark-submit', 'in.csv', 'out', mode='grid',
                                  neighbor_k=[20, 50, 100], beta=[0, 10, 50])
        self.assertEqual(len(commands), 9)
        pairs = {(c['neighbor_k'], c['beta']) for c in commands}
        self.assertEqual(pairs, {(k, b) for k in (20, 50, 100) for b in (0, 10, 50)})

    def test_repeat_and_shuffle_run_one_command(self):
        for mode in ('repeat', 'shuffle'):
            commands = build_commands('spark-submit', 'in.csv', 'out', mode=mode,
                                      neighbor_k=[20], beta=[10], selected_config='sel.json')
            self.assertEqual(len(commands), 1)
            self.assertEqual(commands[0]['mode'], mode)


class SerializationTests(unittest.TestCase):
    """计划书 7.2 序列化行：缓存身份校验、原子写、损坏 JSON 拒收。"""

    def test_cache_identity_covers_input_config_index(self):
        identity = cache_identity(input_md5='m', rows=10, config={'beta': 10}, item_ids=[1, 2])
        self.assertEqual(identity, cache_identity(input_md5='m', rows=10, config={'beta': 10}, item_ids=[1, 2]))
        self.assertNotEqual(identity, cache_identity(input_md5='m', rows=10, config={'beta': 20}, item_ids=[1, 2]))
        self.assertNotEqual(identity, cache_identity(input_md5='m', rows=10, config={'beta': 10}, item_ids=[1, 3]))
        self.assertNotEqual(identity, cache_identity(input_md5='x', rows=10, config={'beta': 10}, item_ids=[1, 2]))
        self.assertNotEqual(identity, cache_identity(input_md5='m', rows=11, config={'beta': 10}, item_ids=[1, 2]))

    def test_cache_rejects_mismatched_identity(self):
        stored = cache_identity(input_md5='m', rows=10, config={'a': 1}, item_ids=[1])
        other = cache_identity(input_md5='m', rows=10, config={'a': 2}, item_ids=[1])
        self.assertFalse(cache_matches(stored, other))
        self.assertTrue(cache_matches(stored, stored))

    def test_atomic_summary_write_and_checked_read(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'summary.json')
            write_summary_atomic(path, {'status': 'success', 'value': 1})
            self.assertEqual(read_summary_checked(path), {'status': 'success', 'value': 1})
            self.assertFalse(os.path.exists(path + '.tmp'))

    def test_truncated_summary_recorded_not_silently_accepted(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'summary.json')
            with open(path, 'w') as handle:
                handle.write('{"status":')
            with self.assertRaises(ValueError):
                read_summary_checked(path)

    def test_summary_without_status_rejected(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'summary.json')
            with open(path, 'w') as handle:
                handle.write('{"value": 1}')
            with self.assertRaises(ValueError):
                read_summary_checked(path)


class TopAccumulatorTests(unittest.TestCase):
    """计划书 11 节 recommendations.csv：原始排序分数 + 证据字段。"""

    def test_keeps_top_n_and_evidence(self):
        acc = TopRecommendationAccumulator(top_n=2)
        acc.add(7, 3.5, evidence={'evidence_neighbor_count': 2, 'top_source_movie_id': 5,
                                  'top_source_c': 3, 'contrib_sum': 3.5})
        acc.add(2, 3.5, evidence={'evidence_neighbor_count': 1, 'top_source_movie_id': 4,
                                  'top_source_c': 2, 'contrib_sum': 3.5})
        acc.add(9, 1.0, evidence={'evidence_neighbor_count': 1, 'top_source_movie_id': 4,
                                  'top_source_c': 2, 'contrib_sum': 1.0})
        top = acc.top()
        self.assertEqual([t[0] for t in top], [2, 7])   # 平分按 movieId 升序
        self.assertEqual(top[0][1], 3.5)

    def test_full_contributions_only_for_case_users(self):
        acc = TopRecommendationAccumulator(top_n=2, case_users={7},
                                           contributions={7: {3: [(5, 0.6)]}})
        acc.add(3, 0.6, user_id=7, evidence={'evidence_neighbor_count': 1,
                                             'top_source_movie_id': 5,
                                             'top_source_c': 2, 'contrib_sum': 0.6})
        rec = acc.top()[0][2]
        self.assertEqual(rec['contributions'], [(5, 0.6)])
        acc2 = TopRecommendationAccumulator(top_n=2, case_users=set(),
                                            contributions={7: {3: [(5, 0.6)]}})
        acc2.add(3, 0.6, user_id=7, evidence={'evidence_neighbor_count': 1,
                                              'top_source_movie_id': 5,
                                              'top_source_c': 2, 'contrib_sum': 0.6})
        self.assertNotIn('contributions', acc2.top()[0][2])


class SourceHashTests(unittest.TestCase):
    def test_source_hashes_detect_midrun_change(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'code.py')
            with open(path, 'w') as handle:
                handle.write('x = 1\n')
            before = source_hashes([path])
            with open(path, 'w') as handle:
                handle.write('x = 2\n')
            after = source_hashes([path])
        self.assertNotEqual(before, after)

    def test_same_content_same_hash(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'code.py')
            with open(path, 'w') as handle:
                handle.write('x = 1\n')
            first = source_hashes([path])
            second = source_hashes([path])
        self.assertEqual(first, second)


class RepeatAndShuffleTests(unittest.TestCase):
    """计划书第 8 节：可复现性对拍与历史错配 donor 映射。"""

    def test_repeat_requires_identical_ids_and_small_numeric_gap(self):
        graph_a = {10: [(20, 0.5, 3), (30, 0.4, 2)]}
        graph_b = {10: [(20, 0.5 + 1e-12, 3), (30, 0.4, 2)]}
        rec_a = {1: [(10, 1.0, {}), (11, 0.5, {})]}
        rec_b = {1: [(10, 1.0 + 1e-12, {}), (11, 0.5, {})]}
        report = run_repeat_comparison(
            neighbors_a=graph_a, neighbors_b=graph_b,
            recs_a=rec_a, recs_b=rec_b,
            users_a=[1, 2], users_b=[1, 2],
            metrics_a={'ndcg@20': 0.1, 'recall@20': 0.2},
            metrics_b={'ndcg@20': 0.1 + 5e-10, 'recall@20': 0.2},
            tolerance=1e-9)
        self.assertTrue(report['consistent'])
        self.assertTrue(report['neighbor_order_equal'])
        self.assertTrue(report['recommendation_order_equal'])
        self.assertLessEqual(report['max_abs_metric_diff'], 1e-9)

    def test_repeat_flags_reordered_neighbors(self):
        graph_a = {10: [(20, 0.5, 3), (30, 0.4, 2)]}
        graph_b = {10: [(30, 0.4, 2), (20, 0.5, 3)]}
        report = run_repeat_comparison(
            neighbors_a=graph_a, neighbors_b=graph_b,
            recs_a={}, recs_b={}, users_a=[1], users_b=[1],
            metrics_a={}, metrics_b={}, tolerance=1e-9)
        self.assertFalse(report['consistent'])
        self.assertFalse(report['neighbor_order_equal'])

    def test_shuffle_donor_mapping_is_derangement_and_uses_every_history_once(self):
        histories = {1: {10, 11}, 2: {20}, 3: {30, 31, 32}, 4: {40}}
        donors = build_shuffle_donors(list(histories), seed=42)
        self.assertEqual(sorted(donors), sorted(histories))
        self.assertEqual(sorted(donors.values()), sorted(histories))
        for u, d in donors.items():
            self.assertNotEqual(u, d)

    def test_shuffle_reuses_each_history_exactly_once(self):
        histories = {1: {10}, 2: {20}, 3: {30}}
        donors = build_shuffle_donors([1, 2, 3], seed=42)
        shuffled = shuffled_histories(histories, donors)
        self.assertEqual(sorted(map(sorted, shuffled.values())),
                         sorted(map(sorted, histories.values())))
        for u in histories:
            self.assertNotEqual(shuffled[u], histories[u] if len(histories) > 1 else histories[u])

    def test_shuffle_excludes_target_user_seen_from_scoring(self):
        # 错配历史里即使含目标用户本人的 seen 电影，打分也必须排除它们
        # （evidence_from_history 只整理来源集合，排除由打分阶段的 seen 完成）
        shuffled_history = {20, 30}
        target_seen = {30}
        from experiments.itemcf.evaluation import evidence_from_history
        scored = evidence_from_history(shuffled_history, target_seen)
        self.assertEqual(scored, {20, 30})  # 来源集合完整保留
        # 打分阶段的排除口径（等价于 core.score_user 的 seen 参数）：
        recommended_pool = {i for i in scored if i not in target_seen}
        self.assertEqual(recommended_pool, {20})


def _missing_input():
    """失败处理用例的输入路径：刻意不存在（子进程桩不读它，校验由桩负责）。

    用固定父目录 + 不存在的文件名，不借道 ``fixture_dir``：目录句柄需要被调用方
    持有才能保证唯一，这里只当路径用，交给运行器时目录已经失去了归属。
    """
    return os.path.join(FIXTURE_ROOT, 'no_such_input', 'missing.csv')


class FailureStateTests(unittest.TestCase):
    """计划书 10.3：失败/阻塞状态与公共正确性阻断。"""

    def test_child_failure_recorded_with_reason_and_exit_code(self):
        with fixture_dir('itemcf-') as folder:
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            status = run_itemcf.run_grid(
                input_path=_missing_input(),
                out=out, mode='grid', neighbor_k=[20], beta=[0],
                child=run_itemcf.child_failing_stub, timeout=10)
        self.assertEqual(status['configs'][0]['status'], 'failed')
        self.assertEqual(status['configs'][0]['exit_code'], 1)
        self.assertIn('reason', status['configs'][0])

    def test_shared_graph_failure_blocks_dependent_configs(self):
        with fixture_dir('itemcf-') as folder:
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            status = run_itemcf.run_grid(
                input_path=_missing_input(),
                out=out, mode='grid', neighbor_k=[20, 50], beta=[0],
                child=run_itemcf.child_graph_failure_stub, timeout=10)
        states = {c['name']: c['status'] for c in status['configs']}
        self.assertIn('blocked', states.values())
        self.assertEqual(status['shared_graph_status'], 'failed')

    def test_public_protocol_error_blocks_remaining_comparisons(self):
        with fixture_dir('itemcf-') as folder:
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            status = run_itemcf.run_grid(
                input_path=_missing_input(),
                out=out, mode='grid', neighbor_k=[20, 50, 100], beta=[0],
                child=run_itemcf.child_protocol_error_stub, timeout=10)
        self.assertEqual(status['public_error'], 'input')
        states = [c['status'] for c in status['configs']]
        self.assertIn(states[0], ('failed', 'blocked'))
        self.assertTrue(all(s == 'blocked' for s in states[1:]))

    def test_source_change_midrun_invalidates_current_and_blocks_rest(self):
        with fixture_dir('itemcf-') as folder:
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            status = run_itemcf.run_grid(
                input_path=_missing_input(),
                out=out, mode='grid', neighbor_k=[20, 50], beta=[0],
                child=run_itemcf.child_source_change_stub, timeout=10)
        self.assertIn('source', status['public_error'])
        self.assertTrue(all(c['status'] in ('failed', 'blocked') for c in status['configs']))

    def test_timeout_recorded_not_hung(self):
        with fixture_dir('itemcf-') as folder:
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            status = run_itemcf.run_grid(
                input_path=_missing_input(),
                out=out, mode='grid', neighbor_k=[20], beta=[0],
                child=run_itemcf.child_slow_stub_argv, timeout=1)
        self.assertEqual(status['configs'][0]['status'], 'failed')
        self.assertIn('timeout', status['configs'][0]['reason'])

    def test_run_status_json_written_with_all_entries(self):
        with fixture_dir('itemcf-') as folder:
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            run_itemcf.run_grid(
                input_path=_missing_input(),
                out=out, mode='grid', neighbor_k=[20, 50], beta=[0],
                child=run_itemcf.child_failing_stub, timeout=10)
            with open(os.path.join(out, 'run_status.json')) as handle:
                entries = json.load(handle)
        self.assertEqual(len(entries), 2)
        self.assertTrue(all('status' in e and 'code_id' in e for e in entries))


class SyntheticPipelineTests(unittest.TestCase):
    """计划书 7.2 末行：输入→切分→9 配置→明细→汇总→独立重算。"""

    def test_full_pipeline_with_stub_graph(self):
        from experiments.itemcf import verify_results
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'in.csv')
            write_input(synth_rows(), path)
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            status = run_itemcf.run_grid(
                input_path=path, out=out, mode='grid',
                neighbor_k=[20, 50, 100], beta=[0, 10, 50],
                child=run_itemcf.child_stub_pipeline, timeout=60)
            self.assertEqual(status['shared_graph_status'], 'success')
            self.assertTrue(all(c['status'] == 'success' for c in status['configs']))
            for name in ('summary_table.csv', 'per_user.csv', 'recommendations.csv',
                         'run_status.json', 'manifest.json'):
                self.assertTrue(os.path.exists(os.path.join(out, name)), name)
            report = verify_results.verify(out)
        self.assertTrue(report['all_recomputed'])
        self.assertEqual(report['metric_mismatches'], [])

    def test_recommendations_file_has_evidence_columns(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'in.csv')
            write_input(synth_rows(), path)
            out = os.path.join(folder, 'grid')
            os.makedirs(out)
            run_itemcf.run_grid(
                input_path=path, out=out, mode='grid',
                neighbor_k=[20], beta=[0], child=run_itemcf.child_stub_pipeline, timeout=60)
            with open(os.path.join(out, 'recommendations.csv')) as handle:
                fields = csv.DictReader(handle).fieldnames
        for column in ('user_id', 'rank', 'movie_id', 'raw_score',
                       'n_all', 'n_like', 'evidence_neighbor_count',
                       'top_source_movie_id', 'top_source_c'):
            self.assertIn(column, fields)


# ---------------------------------------------------------------------------
# 对照方法入口（计划书 4.1）
# ---------------------------------------------------------------------------

def als_fixture_context(users=(1, 2, 3)):
    """构造与 :func:`write_als_fixture` 对齐的评测上下文。

    复用侧必须拿本地切分重算逐用户指标，所以测试也要有一份真实 context——这正是
    "复用不能只抄 summary"的可执行证明。返回 ``(context, total_rows)``：三段总行数
    单独返回（评测上下文刻意不含测试段，防泄漏）。
    """
    from experiments.itemcf.evaluation import build_eval_context
    train, val, test = [], [], []
    for user in users:
        # 每人 4 条训练（2 条好评）+ 2 条验证正例（110/111）+ 1 条保留测试
        train.append((user, 100 + user, 5.0, 1000 + user))
        train.append((user, 101 + user, 4.0, 1001 + user))
        train.append((user, 102 + user, 2.0, 1002 + user))
        train.append((user, 103 + user, 3.0, 1003 + user))
        val.append((user, 110, 5.0, 2000 + user))
        val.append((user, 111, 4.0, 2001 + user))
        test.append((user, 120, 5.0, 3000 + user))
    return build_eval_context(train, val), len(train) + len(val) + len(test)


def write_als_fixture(folder, summary_ndcg=0.0, users=(1, 2, 3)):
    """写一份最小 ALS 复用产物（schema 与 VM 上真实产物一致）。

    真实产物列：``per_user.csv`` = model,user,relevant_count,candidate_count,
    returned_count,recommendable_positive_ratio,recall@20,ndcg@20；
    ``top_recommendations.csv`` = model,user,rank,movie_id,raw_prediction,train_support。
    注意 ``per_user.csv`` **没有** relevant_ids，所以复用侧必须能只靠
    ``top_recommendations.csv`` + 本地验证段重算逐用户指标。
    """
    summary = {
        'status': 'success',
        'config': {'rank': 50, 'reg': 0.1, 'top_n': 20},
        'models': {'ALS': {'ndcg@20': summary_ndcg, 'recall@20': 0.0,
                           'rmse': 0.8720278466009751,
                           'out_of_range_rate': 0.28886010362694303,
                           'users_scored': len(users)}},
        # 协议值必须与 als_fixture_context 造出的上下文一致，否则复用侧会（正确地）
        # 判成 public:input 协议不符并阻断。
        #   train 行 = 4/人；val 行 = 2/人；test 行 = 1/人；rows = 三段之和 = 7/人；
        #   candidate_items = |I_train| = 6（三人的 100+u..103+u 并集恰好是 101..106，
        #   验证段的 110/111 与测试段的 120 都不算 I_train）。
        'protocol': {'train_rows': 4 * len(users), 'val_rows': 2 * len(users),
                     'rows': 7 * len(users), 'candidate_items': 6,
                     'users_scored': len(users), 'relevant_threshold': 4.0,
                     'evaluation_segment': 'validation',
                     'run_id': '20261003T125319Z'},
    }
    with open(os.path.join(folder, 'summary.json'), 'w') as handle:
        json.dump(summary, handle)
    with open(os.path.join(folder, 'per_user.csv'), 'w', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['model', 'user', 'relevant_count', 'candidate_count',
                         'returned_count', 'recommendable_positive_ratio',
                         'recall@20', 'ndcg@20'])
        for user in users:
            writer.writerow(['ALS', user, 2, 6, 20, 1.0, 0.0, 0.0])
    with open(os.path.join(folder, 'top_recommendations.csv'), 'w',
              newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['model', 'user', 'rank', 'movie_id', 'raw_prediction',
                         'train_support'])
        for user in users:
            # rank1 命中一个验证正例（110），其余为负例 -> 逐用户重算 NDCG > 0
            for rank, movie in enumerate([110, 111, 112, 113], 1):
                writer.writerow(['ALS', user, rank, movie, 5.0 - 0.1 * rank, 2])


class ComparisonMethodEntryTests(unittest.TestCase):
    """计划书 4.1：一张 summary_table.csv 覆盖全部方法（基线 + ALS 复用 + ItemCF）。

    这些用例锁的是"入口存在且契约正确"，真实数值由开发机/VM 上的真实数据用例
    （:class:`ReusedArtifactsTests`）核对。
    """

    def test_method_roster_is_fixed_and_ordered(self):
        roster = run_itemcf.comparison_methods()
        self.assertEqual([m['name'] for m in roster], COMPARISON_METHOD_ORDER)
        # ItemCF 的 9 配置不在 method 清单里（由 grid 子进程产出），但 ALS 复用要在。
        self.assertEqual(set(run_itemcf.RMSE_METHODS),
                         {'BiasOnly', 'ALS-ref', 'ALS-best-ranking'})
        self.assertEqual(sorted(run_itemcf.ALS_REUSE_METHODS),
                         ['ALS-best-ranking', 'ALS-ref'])

    def test_mostpop_reuses_pilot_most_popular(self):
        """MostPop 必须复用 experiments.pilot.baselines.most_popular，不重写排序。"""
        from experiments.itemcf.baselines import most_popular_order
        module_source = inspect.getsource(
            sys.modules['experiments.itemcf.baselines'])
        self.assertIn('from experiments.pilot.baselines import most_popular',
                      module_source)
        self.assertIn('most_popular(train, total)', inspect.getsource(most_popular_order))

    def test_mostpop_row_matches_verified_baseline_on_synthetic(self):
        """合成数据上入口能跑通并给出与 per_user_metrics 重算一致的数值。"""
        rows = synth_rows()
        from experiments.pilot.data import temporal_split_three_way
        from experiments.itemcf.evaluation import build_eval_context, per_user_metrics
        train, val, _test = temporal_split_three_way(rows, 0.2, 0.2)
        ctx = build_eval_context(train, val)
        spec = run_itemcf.comparison_method_spec('MostPop')
        result = run_itemcf.evaluate_comparison_method(spec, ctx)
        self.assertEqual(result['status'], 'success')
        order = run_itemcf.baselines.most_popular_order(train, len(ctx.item_ids))
        recs = [[i for i in order if i not in ctx.seen.get(u, set())][:20]
                for u in ctx.eval_users]
        rel = [ctx.relevant[u] for u in ctx.eval_users]
        m = per_user_metrics(recs, rel, k_values=(20,))
        self.assertAlmostEqual(result['metrics']['ndcg@20'],
                               sum(m['ndcg@20']) / len(m['ndcg@20']), places=12)
        self.assertAlmostEqual(result['metrics']['recall@20'],
                               sum(m['recall@20']) / len(m['recall@20']), places=12)

    def test_likepop_and_biasonly_produce_full_rows(self):
        rows = synth_rows()
        from experiments.pilot.data import temporal_split_three_way
        from experiments.itemcf.evaluation import build_eval_context
        train, val, _test = temporal_split_three_way(rows, 0.2, 0.2)
        ctx = build_eval_context(train, val)
        for name in ('LikePop', 'BiasOnly'):
            spec = run_itemcf.comparison_method_spec(name)
            result = run_itemcf.evaluate_comparison_method(spec, ctx)
            self.assertEqual(result['status'], 'success', name)
            self.assertIsNotNone(result['metrics']['ndcg@20'], name)
            self.assertIsNotNone(result['metrics']['recall@20'], name)
            self.assertEqual(result['metrics']['users_scored'], len(ctx.eval_users))
            # 逐用户指标必须可重算（不是抄的汇总数字）
            self.assertEqual(len(result['per_user']), len(ctx.eval_users))

    def test_biasonly_reports_rmse_and_bounds_others_do_not(self):
        rows = synth_rows()
        from experiments.pilot.data import temporal_split_three_way
        from experiments.itemcf.evaluation import build_eval_context
        train, val, _test = temporal_split_three_way(rows, 0.2, 0.2)
        ctx = build_eval_context(train, val)
        bias = run_itemcf.evaluate_comparison_method(
            run_itemcf.comparison_method_spec('BiasOnly'), ctx)
        self.assertIsNotNone(bias['metrics']['rmse'])
        self.assertIsNotNone(bias['metrics']['out_of_range_rate'])
        pop = run_itemcf.evaluate_comparison_method(
            run_itemcf.comparison_method_spec('MostPop'), ctx)
        self.assertIsNone(pop['metrics']['rmse'])
        self.assertIsNone(pop['metrics']['out_of_range_rate'])

    def test_unknown_method_rejected(self):
        with self.assertRaises(KeyError):
            run_itemcf.comparison_method_spec('NoSuchMethod')

    def test_reused_als_spec_cannot_be_scored_without_artifacts(self):
        """reused_als 方法必须在 load_reused_als 里求值，不能走内存打分。"""
        rows = synth_rows()
        from experiments.pilot.data import temporal_split_three_way
        from experiments.itemcf.evaluation import build_eval_context
        train, val, _test = temporal_split_three_way(rows, 0.2, 0.2)
        ctx = build_eval_context(train, val)
        with self.assertRaises(ValueError):
            run_itemcf.evaluate_comparison_method(
                run_itemcf.comparison_method_spec('ALS-ref'), ctx)


class AlsReuseProtocolTests(unittest.TestCase):
    """计划书 4.2：ALS 复用必须**先校验协议一致**，再取逐用户记录重算主表。"""

    def test_protocol_mismatch_is_public_error_and_blocks(self):
        """切分/候选/用户顺序/Top-N 不一致 -> 公共错误（input），不静默采用。"""
        ours = {'train_rows': 196614, 'val_rows': 48138, 'rows': 304944,
                'candidate_items': 11380, 'users_scored': 1930, 'top_n': 20,
                'relevant_threshold': 4.0, 'evaluation_segment': 'validation'}
        same = run_itemcf.verify_als_protocol(dict(ours), ours)
        self.assertTrue(same['ok'])
        self.assertEqual(same['mismatches'], [])
        for field, bad in (('train_rows', 1), ('candidate_items', 1),
                           ('users_scored', 1), ('top_n', 5),
                           ('relevant_threshold', 3.0),
                           ('evaluation_segment', 'test')):
            broken = dict(ours)
            broken[field] = bad
            report = run_itemcf.verify_als_protocol(broken, ours)
            self.assertFalse(report['ok'], field)
            self.assertTrue(any(field in m for m in report['mismatches']), field)

    def test_protocol_accepts_real_als_field_aliases(self):
        """真实 ALS 产物用 ``validation_rows``，且 ``top_n`` 在 config 里，不在 protocol 里。"""
        stored = {'train_rows': 196614, 'validation_rows': 48138, 'rows': 304944,
                  'candidate_items': 11380, 'users_scored': 1930,
                  'relevant_threshold': 4.0, 'evaluation_segment': 'validation'}
        expected = {'train_rows': 196614, 'val_rows': 48138, 'rows': 304944,
                    'candidate_items': 11380, 'users_scored': 1930, 'top_n': 20,
                    'relevant_threshold': 4.0, 'evaluation_segment': 'validation'}
        self.assertFalse(run_itemcf.verify_als_protocol(stored, expected)['ok'])
        report = run_itemcf.verify_als_protocol(stored, expected, {'top_n': 20})
        self.assertTrue(report['ok'], report['mismatches'])
        self.assertEqual(report['resolved']['val_rows'], 48138)
        self.assertEqual(report['resolved']['top_n'], 20)

    def test_missing_protocol_field_rejected(self):
        ours = {'train_rows': 196614, 'candidate_items': 11380}
        report = run_itemcf.verify_als_protocol({'train_rows': 196614}, ours)
        self.assertFalse(report['ok'])
        self.assertTrue(report['mismatches'])

    def test_reused_metrics_recomputed_from_per_user_records(self):
        """复用指标从逐用户明细重算，不抄 summary.json 的四舍五入值。"""
        with fixture_dir('itemcf-') as folder:
            src = os.path.join(folder, 'als')
            os.makedirs(src)
            write_als_fixture(src)
            context, total_rows = als_fixture_context(users=(1, 2, 3))
            result = run_itemcf.load_reused_als(src, context=context,
                                                total_rows=total_rows)
        self.assertEqual(result['status'], 'reused_verified')
        self.assertEqual(result['run_id'], '20261003T125319Z')
        # 逐用户重算：指标来自 top_recommendations 明细，不是 summary.json 里的 0.0
        self.assertEqual(result['metrics']['users_scored'], 3)
        self.assertGreater(result['metrics']['ndcg@20'], 0.0)
        self.assertEqual(len(result['per_user']), 3)
        self.assertEqual(result['protocol']['users_scored'], 3)

    def test_reuse_without_protocol_check_is_refused(self):
        """不给 context 就没有可比口径——不能凭空算指标，只能返回原始明细。"""
        with fixture_dir('itemcf-') as folder:
            src = os.path.join(folder, 'als')
            os.makedirs(src)
            write_als_fixture(src)
            result = run_itemcf.load_reused_als(src)
        self.assertIsNone(result['metrics'])
        self.assertIsNone(result['per_user'])

    def test_reused_summary_alone_is_not_trusted(self):
        """summary.json 里的 ndcg@20 与逐用户重算值不同时，以重算值为准并留痕。"""
        with fixture_dir('itemcf-') as folder:
            src = os.path.join(folder, 'als')
            os.makedirs(src)
            write_als_fixture(src, summary_ndcg=0.999)
            context, total_rows = als_fixture_context(users=(1, 2, 3))
            result = run_itemcf.load_reused_als(src, context=context,
                                                total_rows=total_rows)
        self.assertNotAlmostEqual(result['metrics']['ndcg@20'], 0.999, places=6)
        self.assertEqual(result['summary_declared_ndcg@20'], 0.999)

    def test_reused_als_keeps_original_run_id_and_marks_provenance(self):
        with fixture_dir('itemcf-') as folder:
            src = os.path.join(folder, 'als')
            os.makedirs(src)
            write_als_fixture(src)
            context, total_rows = als_fixture_context(users=(1, 2, 3))
            result = run_itemcf.load_reused_als(src, context=context,
                                                total_rows=total_rows)
        self.assertEqual(result['run_id'], '20261003T125319Z')
        self.assertEqual(result['status'], 'reused_verified')
        self.assertTrue(result['source_dir'].endswith('als'))

    def test_user_order_mismatch_is_public_error(self):
        """复用产物的用户序与本次评测不一致 -> public:input，不取交集糊过去。"""
        with fixture_dir('itemcf-') as folder:
            src = os.path.join(folder, 'als')
            os.makedirs(src)
            write_als_fixture(src, users=(1, 2, 3, 4))
            context, total_rows = als_fixture_context(users=(1, 2, 3))
            with self.assertRaises(RuntimeError) as caught:
                run_itemcf.load_reused_als(src, context=context,
                                           total_rows=total_rows)
        self.assertIn('public:input', str(caught.exception))


class ComparisonTableTests(unittest.TestCase):
    """计划书 11 节：summary_table.csv 列口径（含对照差值/满额率/覆盖/支持度/成本）。"""

    def _rows(self):
        return [
            {'method': 'MostPop', 'config': 'MostPop', 'status': 'success',
             'ndcg@20': 0.0798, 'recall@20': 0.1066, 'rmse': None,
             'out_of_range_rate': None, 'full_list_rate': 1.0,
             'catalog_coverage': 0.001, 'support_median': 100.0,
             'cost_seconds': 1.0, 'code_id': 'abc'},
            {'method': 'ItemCF', 'config': 'k20_beta0', 'status': 'success',
             'ndcg@20': 0.05, 'recall@20': 0.07, 'rmse': None,
             'out_of_range_rate': None, 'full_list_rate': 0.9,
             'catalog_coverage': 0.01, 'support_median': 10.0,
             'cost_seconds': 30.0, 'code_id': 'abc'},
        ]

    def test_delta_against_mostpop_computed(self):
        rows = run_itemcf.add_comparison_columns(self._rows())
        by_name = dict((r['config'], r) for r in rows)
        self.assertEqual(by_name['MostPop']['delta_ndcg_vs_mostpop'], 0.0)
        self.assertAlmostEqual(by_name['k20_beta0']['delta_ndcg_vs_mostpop'],
                               0.05 - 0.0798, places=12)

    def test_columns_present_and_na_rules(self):
        rows = run_itemcf.add_comparison_columns(self._rows())
        for row in rows:
            for column in REQUIRED_TABLE_COLUMNS:
                self.assertIn(column, row, column)
            if row['method'] not in run_itemcf.RMSE_METHODS:
                self.assertIsNone(row['rmse'], row['config'])
                self.assertIsNone(row['out_of_range_rate'], row['config'])

    def test_write_summary_table_csv(self):
        with fixture_dir('itemcf-') as folder:
            path = os.path.join(folder, 'summary_table.csv')
            run_itemcf.write_summary_table(path, self._rows())
            with open(path, newline='') as handle:
                reader = csv.DictReader(handle)
                fields = reader.fieldnames
                rows = list(reader)
        for column in REQUIRED_TABLE_COLUMNS:
            self.assertIn(column, fields, column)
        self.assertEqual(len(rows), 2)


class ComparisonSelectionTests(unittest.TestCase):
    """计划书 6.4：选参规则（NDCG 降序 → Recall → 较小 k → 较小 β，1e-12 平手）。"""


    def test_selection_rule_ndcg_then_recall_then_k_then_beta(self):
        from experiments.itemcf.evaluation import select_config
        configs = [
            {'k': 100, 'beta': 0, 'ndcg@20': 0.10, 'recall@20': 0.20, 'status': 'success'},
            {'k': 20, 'beta': 10, 'ndcg@20': 0.10, 'recall@20': 0.20, 'status': 'success'},
            {'k': 20, 'beta': 50, 'ndcg@20': 0.09, 'recall@20': 0.30, 'status': 'success'},
        ]
        winner = select_config(configs)
        self.assertEqual((winner['k'], winner['beta']), (20, 10))

    def test_tie_within_tolerance_prefers_smaller_k_and_beta(self):
        from experiments.itemcf.evaluation import select_config
        configs = [
            {'k': 50, 'beta': 10, 'ndcg@20': 0.1 + 1e-13, 'recall@20': 0.2,
             'status': 'success'},
            {'k': 20, 'beta': 50, 'ndcg@20': 0.1, 'recall@20': 0.2,
             'status': 'success'},
        ]
        winner = select_config(configs)
        self.assertEqual((winner['k'], winner['beta']), (20, 50))

    def test_selection_ignores_failed_configs(self):
        from experiments.itemcf.evaluation import select_config
        configs = [
            {'k': 20, 'beta': 0, 'ndcg@20': 0.9, 'recall@20': 0.9, 'status': 'failed'},
            {'k': 50, 'beta': 10, 'ndcg@20': 0.1, 'recall@20': 0.2, 'status': 'success'},
        ]
        winner = select_config(configs)
        self.assertEqual((winner['k'], winner['beta']), (50, 10))


class ComparisonDriverContractTests(unittest.TestCase):
    """驱动脚本（run_comparison.py）的产物契约。

    ``run_status.json`` 必须是**逐配置条目列表**：``verify_results`` 与报告层都按
    list 解析。驱动进程自身的进度状态另写 ``driver_state.json``，不能挤掉那份列表。
    真实运行产物存在时顺带核对（缺失则跳过，不在合成数据上编造结论）。
    """

    def test_run_status_is_entry_list_not_state_object(self):
        sys.path.insert(0, os.path.join(PROJECT, 'experiments', 'itemcf'))
        try:
            import run_comparison
        finally:
            sys.path.pop(0)
        source = inspect.getsource(run_comparison.run)
        self.assertIn("entries_path = os.path.join(out, 'run_status.json')", source)
        self.assertIn("state_path = os.path.join(out, 'driver_state.json')", source)
        self.assertIn('R.write_summary_atomic(entries_path, entries)', source)
        self.assertIn('R.write_summary_atomic(state_path, payload)', source)

    def test_real_run_artifacts_are_consistent(self):
        """真实完整运行目录（若存在）：列表 schema + 全部方法一行不落 + 可独立重算。"""
        run_dir = os.environ.get('ITEMCF_RUN_DIR')
        if not run_dir or not os.path.isfile(os.path.join(run_dir, 'summary_table.csv')):
            self.skipTest('N/A: set ITEMCF_RUN_DIR to a completed comparison run dir')
        with open(os.path.join(run_dir, 'run_status.json')) as handle:
            entries = json.load(handle)
        self.assertIsInstance(entries, list, 'run_status.json must be a list')
        self.assertTrue(all(isinstance(e, dict) and 'status' in e for e in entries))
        table = run_comparison_read_table(run_dir)
        methods = {row['method'] for row in table}
        for required in COMPARISON_METHOD_ORDER:
            self.assertIn(required, methods)
        self.assertIn('ItemCF', methods)
        itemcf_rows = [r for r in table if r['method'] == 'ItemCF']
        self.assertEqual(len(itemcf_rows), 9)
        # 对照差值：MostPop 行必须为 0.0，且各行都能算出
        base = [r for r in table if r['method'] == 'MostPop'][0]
        self.assertEqual(float(base['delta_ndcg_vs_mostpop']), 0.0)
        for row in itemcf_rows:
            self.assertNotEqual(row['delta_ndcg_vs_mostpop'], '')
        # 只有 BiasOnly/ALS 报 RMSE 与越界率
        for row in table:
            if row['method'] in run_itemcf.RMSE_METHODS:
                self.assertNotEqual(row['rmse'], '', row['config'])
            else:
                self.assertEqual(row['rmse'], '', row['config'])
                self.assertEqual(row['out_of_range_rate'], '', row['config'])


def run_comparison_read_table(run_dir):
    # 显式 utf-8：产物含中文 notes，Windows 默认 GBK 会 UnicodeDecodeError。
    with open(os.path.join(run_dir, 'summary_table.csv'), newline='',
              encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


@unittest.skipUnless(os.path.isdir(REUSE_GRID) and os.path.isfile(LOCAL_INPUT),
                     'N/A: 旧 ALS 网格产物或本地评分副本缺失，无法做复用对拍')
class ReusedArtifactsTests(unittest.TestCase):
    """计划书 4.2 / 11：复用 ALS 产物与 MostPop 基线对拍（真实数据，开发机执行）。"""

    def test_mostpop_reproduces_verified_baseline(self):
        from experiments.pilot.data import temporal_split_three_way
        from experiments.itemcf.evaluation import build_eval_context, per_user_metrics
        from experiments.itemcf.baselines import most_popular_order
        rows = load_rows_csv(LOCAL_INPUT)
        train, val, _test = temporal_split_three_way(rows, test_frac=0.2, val_frac=0.2)
        ctx = build_eval_context(train, val)
        order = most_popular_order(train, len(ctx.item_ids))
        recs, rel = [], []
        for u in ctx.eval_users:
            seen = ctx.seen.get(u, set())
            recs.append([i for i in order if i not in seen][:20])
            rel.append(ctx.relevant[u])
        m = per_user_metrics(recs, rel, k_values=(20,))
        self.assertAlmostEqual(sum(m['ndcg@20']) / len(m['ndcg@20']),
                               0.07976736506824102, places=10)
        self.assertAlmostEqual(sum(m['recall@20']) / len(m['recall@20']),
                               0.10663327838454675, places=10)

    def test_reused_als_protocol_hashes_match(self):
        from experiments.itemcf.run_itemcf import reused_als_protocol
        from experiments.itemcf.evaluation import build_eval_context
        from experiments.pilot.data import temporal_split_three_way
        rows = load_rows_csv(LOCAL_INPUT)
        train, val, _test = temporal_split_three_way(rows, test_frac=0.2, val_frac=0.2)
        ctx = build_eval_context(train, val)
        for folder in ('rank50_reg0.1', 'rank50_reg0.01'):
            protocol = reused_als_protocol(os.path.join(REUSE_GRID, folder))
            self.assertEqual(protocol['train_rows'], 196614)
            self.assertEqual(protocol['users_scored'], 1930)
            self.assertEqual(protocol['candidate_items'], 11380)
            self.assertEqual(protocol['users_scored'], len(ctx.eval_users))

    def test_real_als_reuse_validates_protocol_and_recomputes(self):
        """真实产物：协议 8 项全过，且逐用户重算值与产物自报值一致（12 位）。"""
        from experiments.itemcf.run_itemcf import (load_reused_als,
                                                   verify_als_protocol,
                                                   expected_protocol)
        from experiments.itemcf.evaluation import build_eval_context
        from experiments.pilot.data import temporal_split_three_way
        rows = load_rows_csv(LOCAL_INPUT)
        train, val, test = temporal_split_three_way(rows, test_frac=0.2, val_frac=0.2)
        ctx = build_eval_context(train, val)
        expected = expected_protocol(ctx, total_rows=len(train) + len(val) + len(test))
        # 三段总行数口径必须与 PRECHECK 一致（304944 = 196614+48138+60192）
        self.assertEqual(expected['rows'], 304944)
        for folder in ('rank50_reg0.1', 'rank50_reg0.01'):
            src = os.path.join(REUSE_GRID, folder)
            with open(os.path.join(src, 'summary.json')) as handle:
                payload = json.load(handle)
            report = verify_als_protocol(payload['protocol'], expected,
                                         payload.get('config'))
            self.assertTrue(report['ok'], (folder, report['mismatches']))
            result = load_reused_als(src, context=ctx, run_id='20261003T125319Z')
            self.assertEqual(result['status'], 'reused_verified')
            self.assertEqual(result['run_id'], '20261003T125319Z')
            self.assertEqual(result['metrics']['users_scored'], 1930)
            # 逐用户重算 == 产物自报（说明我们没有抄 summary 的截断值）
            self.assertAlmostEqual(result['metrics']['ndcg@20'],
                                   payload['models']['ALS']['ndcg@20'], places=12)
            self.assertAlmostEqual(result['metrics']['recall@20'],
                                   payload['models']['ALS']['recall@20'], places=12)

    def test_real_comparison_methods_agree_with_precheck(self):
        """MostPop 必须 bit-identical 复现 PRECHECK 硬门槛，三个基线都能产出。"""
        from experiments.itemcf.run_itemcf import (comparison_method_spec,
                                                   evaluate_comparison_method)
        from experiments.itemcf.evaluation import build_eval_context
        from experiments.pilot.data import temporal_split_three_way
        rows = load_rows_csv(LOCAL_INPUT)
        train, val, _test = temporal_split_three_way(rows, test_frac=0.2, val_frac=0.2)
        ctx = build_eval_context(train, val)
        mostpop = evaluate_comparison_method(comparison_method_spec('MostPop'), ctx)
        self.assertAlmostEqual(mostpop['metrics']['ndcg@20'],
                               0.079767365068241017, places=15)
        self.assertAlmostEqual(mostpop['metrics']['recall@20'],
                               0.10663327838454675, places=15)
        for name in ('LikePop', 'BiasOnly'):
            result = evaluate_comparison_method(comparison_method_spec(name), ctx)
            self.assertEqual(result['status'], 'success', name)
            self.assertEqual(result['metrics']['users_scored'], 1930, name)
            self.assertIsNotNone(result['metrics']['ndcg@20'], name)


if __name__ == '__main__':
    unittest.main()
