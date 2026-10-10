# -*- coding: utf-8 -*-
"""ItemCF 报告层测试：计划书 §12 效果判断分支与诊断段落渲染。

重点锁三件事：

1. §12 情况 2（双超人气基线）必须给出"值得进入独立数据确认、**不宣布上线**"；
2. 情况 1（双不超）如实报差距；
3. conclusion.md 必须内嵌配对 bootstrap 表与 repeat / shuffle 结果，
   且明确写出"探索性区间、不构成独立显著性验证"。
"""
import json
import csv
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf import report

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_tmp')


def _fixture_dir(name):
    path = os.path.join(FIXTURE_ROOT, name)
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    return path


class VerdictTests(unittest.TestCase):
    def test_case2_beats_both_baselines(self):
        best = {'name': 'k100_beta0', 'ndcg@20': 0.12767266300943844,
                'recall@20': 0.16227794901563303}
        baselines = {'MostPop': {'ndcg@20': 0.07976736506824102,
                                 'recall@20': 0.10663327838454675}}
        case, verdict, launch = report.effect_verdict(best, baselines)
        self.assertEqual(case, 2)
        self.assertIn('情况 2', verdict)
        self.assertIn('独立数据确认', verdict)
        self.assertFalse(launch)

    def test_case1_loses_to_both(self):
        best = {'name': 'k20_beta0', 'ndcg@20': 0.05, 'recall@20': 0.05}
        baselines = {'MostPop': {'ndcg@20': 0.0797, 'recall@20': 0.1066}}
        case, verdict, _ = report.effect_verdict(best, baselines)
        self.assertEqual(case, 1)
        self.assertIn('情况 1', verdict)
        self.assertIn('不值得上线', verdict)

    def test_mixed_case_is_not_reported_as_double_win(self):
        best = {'name': 'x', 'ndcg@20': 0.5, 'recall@20': 0.01}
        baselines = {'MostPop': {'ndcg@20': 0.0797, 'recall@20': 0.1066}}
        case, verdict, _ = report.effect_verdict(best, baselines)
        self.assertEqual(case, 3)
        self.assertNotIn('情况 2', verdict)

    def test_missing_baseline_does_not_invent_a_verdict(self):
        best = {'name': 'x', 'ndcg@20': 0.5, 'recall@20': 0.5}
        case, verdict, _ = report.effect_verdict(best, {})
        self.assertIsNone(case)
        self.assertIn('无法', verdict)


class ConclusionRenderingTests(unittest.TestCase):
    def _run(self, name, with_diag=True):
        run = _fixture_dir(name)
        rows = [
            {'name': 'MostPop', 'method': 'MostPop', 'status': 'success',
             'ndcg@20': 0.07976736506824102, 'recall@20': 0.10663327838454675},
            {'name': 'LikePop', 'method': 'LikePop', 'status': 'success',
             'ndcg@20': 0.0787458112448906, 'recall@20': 0.10400597186092124},
            {'name': 'k100_beta0', 'method': 'ItemCF', 'status': 'success',
             'ndcg@20': 0.12767266300943844, 'recall@20': 0.16227794901563303},
        ]
        with open(os.path.join(run, 'summary_table.csv'), 'w',
                  newline='', encoding='utf-8') as fh:
            import csv
            fields = ['name', 'method', 'status', 'ndcg@20', 'recall@20']
            writer = csv.DictWriter(fh, fieldnames=fields)
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
        if with_diag:
            with open(os.path.join(run, 'bootstrap.json'), 'w', encoding='utf-8') as fh:
                json.dump({'seed': 42, 'repeats': 2000, 'comparisons': {
                    'k100_beta0 vs MostPop': {
                        'ndcg@20': {'mean_difference': 0.0479, 'ci95_low': 0.041,
                                    'ci95_high': 0.055},
                        'recall@20': {'mean_difference': 0.0556, 'ci95_low': 0.048,
                                      'ci95_high': 0.063},
                        'excludes_zero_ndcg': True, 'excludes_zero_recall': True}}}, fh)
            with open(os.path.join(run, 'repeat.json'), 'w', encoding='utf-8') as fh:
                json.dump({'neighbor_order_equal': True,
                           'recommendation_order_equal': True, 'users_equal': True,
                           'max_abs_float_diff': 0.0, 'max_abs_metric_diff': 0.0,
                           'tolerance': 1e-9, 'consistent': True}, fh)
            with open(os.path.join(run, 'shuffle.json'), 'w', encoding='utf-8') as fh:
                json.dump({'seed': 42, 'real_ndcg@20': 0.127, 'shuffled_ndcg@20': 0.03,
                           'real_recall@20': 0.162, 'shuffled_recall@20': 0.04,
                           'ndcg@20': {'mean_difference': 0.097, 'ci95_low': 0.09,
                                       'ci95_high': 0.104},
                           'recall@20': {'mean_difference': 0.122, 'ci95_low': 0.11,
                                         'ci95_high': 0.13}}, fh)
        text = report.build_conclusion(run, rows, [None, None, None],
                                       ['a.csv', 'b.csv', 'c.csv'])
        return run, text

    def test_conclusion_states_case2_and_no_launch(self):
        _run, text = self._run('report_c2')
        self.assertIn('情况 2', text)
        self.assertIn('不宣布上线', text)
        self.assertIn('独立数据确认', text)

    def test_case2_works_when_rows_only_carry_name_column(self):
        """summary_table.csv 只有 ``name`` 列（没有 ``method``），基线仍必须被认出。

        只按 ``method`` 查基线会让 §12 判断退化成"人气基线未产出"，把已经做出来的
        结论丢掉——这是真实产物列序触发的回归。
        """
        run = _fixture_dir('report_nameonly')
        rows = [
            {'name': 'MostPop', 'status': 'success', 'ndcg@20': 0.07976736506824102,
             'recall@20': 0.10663327838454675},
            {'name': 'k100_beta0', 'status': 'success',
             'ndcg@20': 0.12767266300943844, 'recall@20': 0.16227794901563303},
        ]
        text = report.build_conclusion(run, rows, [None, None, None],
                                       ['a.csv', 'b.csv', 'c.csv'])
        self.assertIn('情况 2', text)
        self.assertNotIn('人气基线未产出', text)

    def test_conclusion_embeds_bootstrap_table_and_caveat(self):
        _run, text = self._run('report_bs')
        self.assertIn('配对 bootstrap', text)
        self.assertIn('k100_beta0 vs MostPop', text)
        self.assertIn('不构成', text)
        self.assertIn('探索性', text)

    def test_conclusion_reports_repeat_and_shuffle(self):
        _run, text = self._run('report_diag')
        self.assertIn('repeat', text)
        self.assertIn('history-shuffle', text)
        self.assertIn('一致=True', text)
        self.assertIn('不因结果重选 seed', text)

    def test_missing_diagnostics_render_na_not_fabricated(self):
        _run, text = self._run('report_nodiag', with_diag=False)
        self.assertIn('N/A', text)
        # 没有 shuffle.json 时不得出现编造的数字
        self.assertNotIn('真实 NDCG@20=0.127', text)


class PerConfigMetricPathTests(unittest.TestCase):
    """run_comparison 把 summary.json 写在 configs/<name>/ 下，报告层必须也能找到。

    只找 <run_dir>/<name>/summary.json 会让完整对照实验的 ItemCF 诊断列整列 N/A，
    属于"看起来没数据、其实是路径没对上"的静默降级。
    """

    def test_finds_metrics_under_configs_subdir(self):
        run = _fixture_dir('report_paths')
        os.makedirs(os.path.join(run, 'configs', 'k100_beta0'))
        payload = {'status': 'success',
                   'models': {'k100_beta0': {'full_list_rate': 1.0,
                                             'catalog_coverage': 0.25,
                                             'users_scored': 1930}}}
        with open(os.path.join(run, 'configs', 'k100_beta0', 'summary.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump(payload, fh)
        got = report.load_per_config_metrics(run, ['k100_beta0', 'MostPop'])
        self.assertIn('k100_beta0', got)
        self.assertEqual(got['k100_beta0']['full_list_rate'], 1.0)
        self.assertEqual(got['k100_beta0']['catalog_coverage'], 0.25)
        # 没有产物的方法不得凭空出现
        self.assertNotIn('MostPop', got)

    def test_finds_metrics_under_legacy_flat_dir(self):
        run = _fixture_dir('report_paths_flat')
        os.makedirs(os.path.join(run, 'k20_beta0'))
        with open(os.path.join(run, 'k20_beta0', 'summary.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump({'status': 'success',
                       'models': {'k20_beta0': {'fill_rate': 0.9}}}, fh)
        got = report.load_per_config_metrics(run, ['k20_beta0'])
        self.assertEqual(got['k20_beta0']['fill_rate'], 0.9)

    def test_na_sentinel_in_summary_table_falls_back_to_config_summary(self):
        """主表里的 'N/A' 是哨兵，不能让图源数据整列变成 N/A。

        ``summary_table.csv`` 把缺项写成字符串 ``'N/A'``；若 ``figure2_source`` 的
        ``pick()`` 只判 None/空串，就会把这个哨兵当真实值返回，逐配置 summary.json
        里的满额率/覆盖率永远读不到——图 2 会静默变成"全 N/A"。
        """
        run = _fixture_dir('report_na_sentinel')
        os.makedirs(os.path.join(run, 'configs', 'k100_beta0'))
        with open(os.path.join(run, 'configs', 'k100_beta0', 'summary.json'), 'w',
                  encoding='utf-8') as fh:
            json.dump({'status': 'success',
                       'models': {'k100_beta0': {'full_list_rate': 1.0,
                                                 'empty_list_rate': 0.0}}}, fh)
        rows = [{'name': 'k100_beta0', 'status': 'success', 'ndcg@20': '0.127673',
                 'recall@20': '0.162278', 'full_list_rate': report.NA,
                 'empty_list_rate': report.NA, 'evidence_max_recall@20': report.NA,
                 'evidence_count_p50': report.NA, 'reason': report.NA}]
        path = report.figure2_source(run, rows)
        with open(path, newline='', encoding='utf-8') as fh:
            produced = list(csv.DictReader(fh))
        row = [r for r in produced if r['name'] == 'k100_beta0'][0]
        # 值确实从逐配置 summary.json 取到了（而不是沿用主表的 'N/A' 哨兵）
        self.assertNotEqual(row['full_list_rate'], report.NA)
        self.assertAlmostEqual(float(row['full_list_rate']), 1.0, places=9)
        self.assertNotEqual(row['empty_list_rate'], report.NA)
        self.assertAlmostEqual(float(row['empty_list_rate']), 0.0, places=9)
        # 真正没有的指标仍然 N/A，不编造
        self.assertEqual(row['evidence_max_recall@20'], report.NA)


if __name__ == '__main__':
    unittest.main()
