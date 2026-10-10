# -*- coding: utf-8 -*-
"""ItemCF 证据诊断传递测试（计划书 §6.2：证据集 Recall@20 上限 / 证据候选数）。

**缺口背景（本文件的由来）**：``score_from_graph.py`` 一直在逐用户算
``evidence_count``，但

1. 它把 ``|E_u|`` 错记成了 Top-N 长度（``len(ranked)``）；
2. 调 ``summarize()`` 时**没有传 ``evidence=``**。

结果是计划书 §6.2 的两个诊断在**所有**配置上恒为 ``N/A``：图 2 的
``evidence_max_recall@20`` / ``evidence_count_p50`` 两个子图为空。

本文件锁死修复后的契约：

- ``E_u`` 是**全部**有邻域证据的候选（``score(u,i) > 0``），**不因 top_n 截断**；
- 逐用户 ``|E_u|`` 与 ``min(20, |R_u∩E_u|)/|R_u|`` 必须真的进 ``summarize``；
- 每个配置的模型块必须带出非 None 的证据诊断；
- **不变式**：任一配置的 ``recall@20 <= evidence_max_recall@20``
  （计划书 §6.2：最终 Recall 不得超过证据上限）；
- 报告层图 2 的源 CSV 必须从逐配置 ``summary.json`` 取到这两个字段。

Python 3.6 兼容；只用标准库 + NumPy（构图/打分路径）。
"""
import csv
import json
import math
import os
import shutil
import sys
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT)

from experiments.itemcf import report  # noqa: E402
from experiments.itemcf import score_from_graph  # noqa: E402
from experiments.itemcf.evaluation import summarize  # noqa: E402

FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_tmp')


def _fixture_dir(name):
    path = os.path.join(FIXTURE_ROOT, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


def _graph(edges):
    """``{(source, target): (target, sim, c)}``（与 ``load_graph`` 同形）。"""
    return dict(edges)


class EvidenceSetNotTruncatedTests(unittest.TestCase):
    """``E_u`` 必须完整返回，不能被 ``top_n`` 截断。"""

    def setUp(self):
        # 来源 1 的邻居：目标 10/11/12 来自 h1 + h2 两个来源；13 只来自 h2。
        # h1/h2/h3 三个来源各给出一批目标，保证 |E_u| 远大于 top_n=2。
        self.neighbors = _graph({
            (1, 10): (10, 0.9, 3), (1, 11): (11, 0.8, 2),
            (2, 10): (10, 0.5, 1), (2, 12): (12, 0.7, 4), (2, 13): (13, 0.6, 5),
            (3, 14): (14, 0.4, 2), (3, 15): (15, 0.3, 1),
        })
        self.history = {1, 2, 3}
        self.seen = set()

    def test_returns_full_evidence_set_beyond_top_n(self):
        ranked, evidence_ids = score_from_graph.score_user_prefix(
            self.history, self.neighbors, self.seen, 2, return_evidence=True)
        # Top-N 只有 2 条（截断生效）
        self.assertEqual(len(ranked), 2)
        # 证据集是全部 6 个有证据候选 {10,11,12,13,14,15}，不是 2
        self.assertEqual(sorted(evidence_ids), [10, 11, 12, 13, 14, 15])
        self.assertNotEqual(len(evidence_ids), len(ranked))

    def test_evidence_set_matches_sum_of_scores(self):
        """``E_u = {i in C_u : score(u,i) > 0}``：证据集元素必须两两得分正。"""
        _ranked, evidence_ids = score_from_graph.score_user_prefix(
            self.history, self.neighbors, self.seen, 1, return_evidence=True)
        # 目标 10 有两条贡献（来源 1 与 2），仍在证据集内
        self.assertIn(10, evidence_ids)

    def test_excludes_seen_and_history_from_evidence(self):
        neighbors = _graph({
            (1, 10): (10, 0.9, 3),
            (1, 11): (11, 0.8, 2),   # 11 在 seen 里 -> 排除
            (1, 12): (12, 0.7, 4),   # 12 在 history 里 -> 排除
        })
        _ranked, evidence_ids = score_from_graph.score_user_prefix(
            {1, 12}, neighbors, {11}, 20, return_evidence=True)
        self.assertEqual(evidence_ids, [10])

    def test_empty_history_yields_empty_evidence(self):
        ranked, evidence_ids = score_from_graph.score_user_prefix(
            set(), self.neighbors, self.seen, 20, return_evidence=True)
        self.assertEqual(ranked, [])
        self.assertEqual(evidence_ids, [])

    def test_default_call_keeps_backward_compatible_return(self):
        """``return_evidence=False``（默认）仍返回裸列表，老调用方不受影响。"""
        ranked = score_from_graph.score_user_prefix(
            self.history, self.neighbors, self.seen, 2)
        self.assertIsInstance(ranked, list)
        self.assertEqual(len(ranked), 2)
        self.assertIn('movie_id', ranked[0])


class SummarizeWiringTests(unittest.TestCase):
    """逐用户 ``E_u`` 传给 ``summarize`` 后，§6.2 诊断必须算得出来。"""

    def test_evidence_diagnostics_are_not_none(self):
        recs = [[10, 11, 12], [10]]
        rel = [{10, 99, 98}, {10}]
        evidence = {
            0: {10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24,
                25, 26, 27, 28, 29, 30},   # |E_u| = 21
            1: {10, 12},                    # |E_u| = 2
        }
        summary = summarize(recs, rel, [1, 2], k_values=(20,), top_n=20,
                            evidence=evidence)
        self.assertIsNotNone(summary['evidence_max_recall@20'])
        self.assertIsNotNone(summary['evidence_count_p50'])
        self.assertIsNotNone(summary['evidence_count_p10'])
        self.assertIsNotNone(summary['evidence_count_p90'])
        # u0: R={10,99,98}∩E 命中 10 -> min(20,1)/3 = 1/3
        # u1: R={10}∩E 命中 10 -> min(20,1)/1 = 1
        self.assertAlmostEqual(summary['evidence_max_recall@20'], (1 / 3.0 + 1.0) / 2.0)
        # 证据候选数分位数落在 [2, 21]
        self.assertGreaterEqual(summary['evidence_count_p50'], 2)
        self.assertLessEqual(summary['evidence_count_p50'], 21)

    def test_without_evidence_stays_na(self):
        """不传 evidence 时保持 N/A（缺项不编造，口径不变）。"""
        summary = summarize([[10]], [{10}], [1], k_values=(20,), top_n=20)
        self.assertIsNone(summary['evidence_max_recall@20'])
        self.assertIsNone(summary['evidence_count_p50'])

    def test_recall_never_exceeds_evidence_cap(self):
        """计划书 §6.2 不变式：实际 Recall@20 不得超过证据上限。"""
        recs = [[10, 11], [12]]
        rel = [{10, 11, 12, 13}, {12}]
        evidence = {0: {10, 11}, 1: {12}}   # u0 只可能命中 2 个正例
        summary = summarize(recs, rel, [1, 2], k_values=(20,), top_n=20,
                            evidence=evidence)
        self.assertLessEqual(summary['recall@20'], summary['evidence_max_recall@20'])
        self.assertAlmostEqual(summary['evidence_max_recall@20'], (2 / 4.0 + 1.0) / 2.0)


class ModelBlockEvidenceTests(unittest.TestCase):
    """从落盘图打分产出的模型块必须带出证据诊断（端到端小样本）。"""

    def setUp(self):
        import numpy as np
        self.dir = _fixture_dir('itemcf_evidence_block')
        # 图：来源 1 -> {10, 11}，来源 2 -> {11, 12}，来源 3 -> {13}
        self.sources = np.asarray([1, 1, 2, 2, 3], dtype=np.int64)
        self.targets = np.asarray([10, 11, 11, 12, 13], dtype=np.int64)
        self.sims = np.asarray([0.9, 0.8, 0.7, 0.6, 0.5], dtype=np.float64)
        self.coocs = np.asarray([3, 2, 4, 1, 5], dtype=np.int64)
        self.npz = os.path.join(self.dir, 'beta0.npz')
        np.savez_compressed(self.npz, sources=self.sources, targets=self.targets,
                            similarities=self.sims, cooccurrences=self.coocs,
                            item_ids=np.asarray([1, 2, 3, 10, 11, 12, 13],
                                                dtype=np.int64))
        self.mapping = os.path.join(self.dir, 'beta0.mapping.json')
        with open(self.mapping, 'w', encoding='utf-8') as handle:
            json.dump({'max_k': 100, 'beta': 0.0}, handle)

    def test_load_graph_then_score_produces_evidence_diagnostics(self):
        neighbors, _mapping = score_from_graph.load_graph(self.npz, self.mapping, 100)
        # 单用户：历史 {1, 2} -> 证据 {10, 11, 12}（11 有两个来源）
        ranked, evidence_ids = score_from_graph.score_user_prefix(
            {1, 2}, neighbors, set(), 2, return_evidence=True)
        self.assertEqual(len(ranked), 2)
        self.assertEqual(sorted(evidence_ids), [10, 11, 12])

        recs = [[r['movie_id'] for r in ranked]]
        rel = [{10, 11, 12, 13}]
        evidence = {0: set(evidence_ids)}
        summary = summarize(recs, rel, [1], k_values=(20,), top_n=20,
                            evidence=evidence)
        self.assertIsNotNone(summary['evidence_max_recall@20'])
        self.assertAlmostEqual(summary['evidence_max_recall@20'], 3 / 4.0)
        self.assertIsNotNone(summary['evidence_count_p50'])
        self.assertEqual(summary['evidence_count_p50'], 3.0)
        # 不变式
        self.assertLessEqual(summary['recall@20'], summary['evidence_max_recall@20'])

    def test_model_block_declares_evidence_fields(self):
        """``score_from_graph.main`` 的模型块键必须包含证据诊断字段。

        只断言契约键存在（真实数值由 VM 复跑产出），避免把大样本数字写进测试。
        """
        source = os.path.join(PROJECT, 'experiments', 'itemcf', 'score_from_graph.py')
        with open(source, encoding='utf-8') as handle:
            text = handle.read()
        for field in ('evidence_positive_coverage', 'evidence_max_recall@20',
                      'evidence_count_p50', 'evidence_count_zero_users'):
            self.assertIn("'%s'" % field, text,
                          'score_from_graph model block must declare %s' % field)
        # 关键：summarize 调用必须真的收到 evidence=
        self.assertIn('evidence=evidence_by_index', text)


class Figure2EvidenceSourceTests(unittest.TestCase):
    """图 2 源 CSV 的证据两列必须来自逐配置 summary.json，而不是 N/A 哨兵。"""

    def _run_with_config(self, name="f2ev"):
        run = _fixture_dir(name)
        os.makedirs(os.path.join(run, 'configs', 'k100_beta0'))
        payload = {'status': 'success', 'models': {'k100_beta0': {
            'evidence_max_recall@20': 0.4201,
            'evidence_count_p50': 87.0,
            'full_list_rate': 1.0,
            'recall@20': 0.16227794901563303}}}
        with open(os.path.join(run, 'configs', 'k100_beta0', 'summary.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(payload, handle)
        return run

    def test_picks_evidence_columns_from_config_summary(self):
        run = self._run_with_config()
        # 主表行里这两列是 'N/A' 哨兵（真实产物就是这样）
        rows = [{'name': 'k100_beta0', 'status': 'success',
                 'ndcg@20': '0.127673', 'recall@20': '0.162278',
                 'full_list_rate': report.NA,
                 'evidence_max_recall@20': report.NA,
                 'evidence_count_p50': report.NA}]
        path = report.figure2_source(run, rows)
        with open(path, newline='', encoding='utf-8') as handle:
            produced = list(csv.DictReader(handle))
        row = [r for r in produced if r['name'] == 'k100_beta0'][0]
        self.assertNotEqual(row['evidence_max_recall@20'], report.NA)
        self.assertAlmostEqual(float(row['evidence_max_recall@20']), 0.4201, places=6)
        self.assertNotEqual(row['evidence_count_p50'], report.NA)
        self.assertAlmostEqual(float(row['evidence_count_p50']), 87.0, places=6)

    def test_figure2_draws_evidence_panel_when_data_present(self):
        """有证据数值时图 2 的四个子图都必须画出点（不能有空白面板）。

        这条用例专门锁一个已发生的回归：``draw_figure2`` 直接吃主表行时，
        主表里的 ``'N/A'`` 哨兵让 ``as_float`` 返回 None，证据上限/满额率/证据强度
        三个面板会**静默画成空白**——图里看着像"没数据"，其实真值就在逐配置
        ``summary.json`` 与图源 CSV 里。必须从图源 CSV 取数。
        """
        run = self._run_with_config('f2ev_draw')
        rows = [{'name': 'k100_beta0', 'status': 'success',
                 'ndcg@20': '0.127673', 'recall@20': '0.162278',
                 'full_list_rate': report.NA,
                 'evidence_max_recall@20': report.NA,
                 'evidence_count_p50': report.NA}]
        source = report.figure2_source(run, rows)
        plt = report._setup_matplotlib()
        if plt is None:
            self.skipTest('matplotlib unavailable')
        with open(source, newline='', encoding='utf-8') as handle:
            rows_csv = list(csv.DictReader(handle))
        # 图与源 CSV 必须同源：draw_figure2 读的就是这份 CSV
        self.assertTrue(os.path.isfile(source))
        self.assertIsNotNone(report.draw_figure2(plt, run, rows))

        # 四个面板都必须有可画的点（各自与 fixture 的真值相等）
        expected = {'evidence_max_recall@20': 0.4201,
                    'recall@20': 0.16227794901563303,
                    'full_list_rate': 1.0,
                    'evidence_count_p50': 87.0}
        for field, want in sorted(expected.items()):
            value = report.as_float(rows_csv[0].get(field))
            self.assertIsNotNone(
                value, 'panel %s has no plottable point (silent empty panel)'
                % field)
            self.assertAlmostEqual(value, want, places=6,
                                   msg='panel %s value mismatch' % field)


class VerifyEvidenceTests(unittest.TestCase):
    """``verify_results`` 必须能从**全精度**来源重算证据诊断。

    两个已发生的坑：

    1. 报告层会把 ``summary_table.csv`` 重写成 6 位小数的展示值；若核验拿它做
       1e-10 比对，**报告跑过一次之后核验必然全红**——那是口径错误不是数据错误。
       核验必须以逐配置 ``summary.json`` 的全精度值为准。
    2. ``per_user.csv`` 是驱动层落盘的聚合明细，**重新评分不会刷新它**。拿它当
       逐用户 ``|E_u|`` 来源会把旧的错误值（曾等于 Top-N 长度）当成证据数。
       核验必须优先读逐配置 ``summary.json`` 的 ``per_user``。
    """

    def _run(self, name, precise=True):
        from experiments.itemcf import verify_results
        run = _fixture_dir(name)
        # 逐配置 summary.json：全精度真值
        os.makedirs(os.path.join(run, 'configs', 'k20_beta0'))
        # NDCG@20 = DCG/IDCG，IDCG 按 min(20,|R_u|) 个理想位置算（计划书 §6.1）。
        # user1: |R|=4，Top-2=[10,11] 全中 -> DCG = 1/log2(2) + 1/log2(3)
        #        IDCG = 1/log2(2)+1/log2(3)+1/log2(4)+1/log2(5)
        dcg1 = 1.0 / 1.0 + 1.0 / math.log2(3)
        idcg1 = sum(1.0 / math.log2(p + 2) for p in range(4))
        ndcg1 = dcg1 / idcg1
        # user2: |R|=2，Top-2=[20,21] 全中 -> 理想排序 -> NDCG = 1.0
        dcg2 = 1.0 / 1.0 + 1.0 / math.log2(3)
        idcg2 = sum(1.0 / math.log2(p + 2) for p in range(2))
        ndcg2 = dcg2 / idcg2
        per_user = [
            {'user_id': 1, 'relevant_ids': '10|11|12|13', 'evidence_count': 40,
             'returned_count': 2, 'ndcg@20': ndcg1, 'recall@20': 0.5},
            {'user_id': 2, 'relevant_ids': '20|21', 'evidence_count': 30,
             'returned_count': 2, 'ndcg@20': ndcg2, 'recall@20': 1.0},
        ]
        payload = {
            'status': 'success',
            # 模型级 = 逐用户均值
            'models': {'k20_beta0': {'ndcg@20': (ndcg1 + ndcg2) / 2.0,
                                     'recall@20': 0.75}},
            'per_user': per_user,
            'recommendations': [],
        }
        with open(os.path.join(run, 'configs', 'k20_beta0', 'summary.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(payload, handle)
        # summary_table.csv：报告层重写后的**6 位小数展示值**（会丢精度，核验必须容忍）
        with open(os.path.join(run, 'summary_table.csv'), 'w', newline='',
                  encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['name', 'ndcg@20', 'recall@20'])
            writer.writeheader()
            writer.writerow({'name': 'k20_beta0',
                             'ndcg@20': '%.6f' % ((ndcg1 + ndcg2) / 2.0),
                             'recall@20': '%.6f' % 0.75})
        # per_user.csv：聚合明细，此处**故意放旧值**（evidence_count = Top-N 长度）
        with open(os.path.join(run, 'per_user.csv'), 'w', newline='',
                  encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                'model', 'user_id', 'relevant_ids', 'evidence_count', 'returned_count',
                'ndcg@20', 'recall@20'])
            writer.writeheader()
            for row in per_user:
                writer.writerow({'model': 'k20_beta0', 'user_id': row['user_id'],
                                 'relevant_ids': row['relevant_ids'],
                                 'evidence_count': 2,   # 旧错误口径
                                 'returned_count': row['returned_count'],
                                 'ndcg@20': row['ndcg@20'],
                                 'recall@20': row['recall@20']})
        with open(os.path.join(run, 'recommendations.csv'), 'w', newline='',
                  encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=[
                'model', 'user_id', 'rank', 'movie_id', 'raw_score'])
            writer.writeheader()
            writer.writerow({'model': 'k20_beta0', 'user_id': 1, 'rank': 1,
                             'movie_id': 10, 'raw_score': 1.0})
            writer.writerow({'model': 'k20_beta0', 'user_id': 1, 'rank': 2,
                             'movie_id': 11, 'raw_score': 0.9})
            writer.writerow({'model': 'k20_beta0', 'user_id': 2, 'rank': 1,
                             'movie_id': 20, 'raw_score': 1.0})
            writer.writerow({'model': 'k20_beta0', 'user_id': 2, 'rank': 2,
                             'movie_id': 21, 'raw_score': 0.5})
        with open(os.path.join(run, 'run_status.json'), 'w', encoding='utf-8') as handle:
            json.dump([{'name': 'k20_beta0', 'status': 'success', 'code_id': 'x'}], handle)
        return verify_results, run

    def test_model_level_compare_uses_full_precision_summary_json(self):
        """展示层 6 位小数的 summary_table 不得把核验判红。

        核验容差是 ``1e-10``，展示值只有 6 位小数：说明核验**必须**以
        summary.json 的全精度值为准。本用例构造"展示值相对真值差 1e-6"的场景：
        若核验错读展示值就会报 mismatch。
        """
        verify_results, run = self._run('verify_precision')
        # 真值 = 逐用户重算值（由 recommendations.csv 决定，不可篡改）
        rec = verify_results._read_csv(os.path.join(run, 'recommendations.csv'))
        by_user = {}
        for r in rec:
            by_user.setdefault(int(r['user_id']), []).append(r)
        rel = {1: {10, 11, 12, 13}, 2: {20, 21}}
        ndcgs, recalls = [], []
        for user in sorted(by_user):
            ids = [int(r['movie_id']) for r in
                   sorted(by_user[user], key=lambda r: int(r['rank']))]
            rset = rel[user]
            recalls.append(sum(1 for i in ids if i in rset) / float(len(rset)))
            dcg = sum(1.0 / math.log2(p + 2) for p, i in enumerate(ids) if i in rset)
            idcg = sum(1.0 / math.log2(p + 2) for p in range(min(20, len(rset))))
            ndcgs.append(dcg / idcg if idcg else 0.0)
        true_ndcg = sum(ndcgs) / len(ndcgs)
        true_recall = sum(recalls) / len(recalls)

        # summary.json 写真值（全精度）
        path = os.path.join(run, 'configs', 'k20_beta0', 'summary.json')
        with open(path, encoding='utf-8') as handle:
            payload = json.load(handle)
        payload['models']['k20_beta0']['ndcg@20'] = true_ndcg
        payload['models']['k20_beta0']['recall@20'] = true_recall
        for row in payload['per_user']:
            idx = 0 if row['user_id'] == 1 else 1
            row['ndcg@20'] = ndcgs[idx]
            row['recall@20'] = recalls[idx]
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle)

        # summary_table.csv 是 6 位小数的展示值（与真值差 ~1e-6 量级）
        with open(os.path.join(run, 'summary_table.csv'), 'w', newline='',
                  encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=['name', 'ndcg@20', 'recall@20'])
            writer.writeheader()
            writer.writerow({'name': 'k20_beta0',
                             'ndcg@20': '%.6f' % true_ndcg,
                             'recall@20': '%.6f' % true_recall})

        report = verify_results.verify(run, input_path=None)
        self.assertEqual(report['metric_mismatches'], [])
        self.assertTrue(report['all_recomputed'])
        # 关键契约：模型级比对用的是**全精度** summary.json，不是 6 位展示表。
        # 用"比真值差 1e-6"的展示值构造场景——真值取自 summary.json 时余差为 0，
        # 取展示表时余差为 1e-6。这里断言重算值确实等于 summary.json 的真值。
        self.assertAlmostEqual(report['models_recomputed']['k20_beta0']['ndcg@20'],
                               true_ndcg, places=15)
        self.assertAlmostEqual(report['models_recomputed']['k20_beta0']['recall@20'],
                               true_recall, places=15)
        # 展示表里的 6 位小数与真值的差应远超核验容差 1e-10（证明这不是"碰巧相等"）
        with open(os.path.join(run, 'summary_table.csv'), newline='',
                  encoding='utf-8') as handle:
            shown = list(csv.DictReader(handle))[0]
        self.assertGreater(abs(float(shown['ndcg@20']) - true_ndcg), 1e-10)

    def test_evidence_count_comes_from_config_summary_not_stale_per_user(self):
        """逐用户 |E_u| 必须取自 summary.json 的 per_user，而不是旧的聚合明细。"""
        verify_results, run = self._run('verify_evidence_src')
        report = verify_results.verify(run, input_path=None)
        entry = report['evidence_diagnostics']['k20_beta0']
        # summary.json 里是 40/30（真值），per_user.csv 里是 2（旧口径）
        self.assertEqual(entry['evidence_source'], 'summary.json')
        self.assertEqual(entry['evidence_count_max'], 40)
        self.assertEqual(entry['evidence_count_min'], 30)
        # 不变式仍成立：actual recall <= 证据上限上界
        self.assertTrue(entry['recall_within_evidence_cap'])


class MainTableSentinelTests(unittest.TestCase):
    """主表重跑必须幂等：上一次落的 ``'N/A'`` 哨兵不能挡住真数据。

    报告层是**幂等重跑**的：上一次跑会把缺项写成字面量 ``'N/A'`` 落进
    ``summary_table.csv``。若 ``build_main_table`` 只判 None/空串，重跑时这个
    非空字符串会被当成"真值"直接采用，逐配置 ``summary.json`` 里的真数据永远
    补不进来——主表的证据上限/满额率列就永久钉死在 N/A。
    """

    def test_na_sentinel_in_source_row_does_not_block_extra_metrics(self):
        rows = [{'name': 'k100_beta0', 'status': 'success',
                 'ndcg@20': '0.127673', 'recall@20': '0.162278',
                 'full_list_rate': report.NA,
                 'evidence_positive_coverage': report.NA,
                 'evidence_max_recall@20': report.NA,
                 'evidence_count_p50': report.NA}]
        extra = {'k100_beta0': {'full_list_rate': 1.0,
                                'evidence_positive_coverage': 0.7794,
                                'evidence_max_recall@20': 0.7452,
                                'evidence_count_p50': 991.0}}
        _cols, table = report.build_main_table(rows, extra)
        got = table[0]
        self.assertEqual(got['full_list_rate'], '1')
        self.assertNotEqual(got['evidence_max_recall@20'], report.NA)
        self.assertAlmostEqual(float(got['evidence_max_recall@20']), 0.7452, places=6)
        self.assertNotEqual(got['evidence_positive_coverage'], report.NA)
        self.assertAlmostEqual(float(got['evidence_positive_coverage']), 0.7794, places=6)
        self.assertEqual(got['evidence_count_p50'], '991')

    def test_na_sentinel_stays_na_when_no_extra_value(self):
        """真没有的指标仍必须是 N/A，不能编造。"""
        rows = [{'name': 'MostPop', 'status': 'success', 'ndcg@20': '0.079767',
                 'recall@20': '0.106633', 'evidence_max_recall@20': report.NA}]
        # 另一行有真值 -> 该列进入列集；MostPop 自己没有值 -> 保持 N/A
        extra = {'k100_beta0': {'evidence_max_recall@20': 0.7452}}
        _cols, table = report.build_main_table(rows, extra)
        self.assertEqual(table[0]['evidence_max_recall@20'], report.NA)


if __name__ == '__main__':
    unittest.main()
