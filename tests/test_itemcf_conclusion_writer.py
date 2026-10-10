# -*- coding: utf-8 -*-
"""``conclusion_writer`` 的契约测试（计划书 §6 最后一项产物）。

这个模块负责把产物渲染成 ``conclusion.md``。它容易被当成"只是个格式化"，
但**结论文件里的每一句都会被论文直接引用**，所以下面这些点必须锁死：

1. 计划书 §6 要求的七项内容**都要出现**（纳入范围/规模/耗时/采样最大 RSS/
   覆盖缺口/是否可接入/未做泛化评估）；
2. **采样 RSS 必须写成"采样最大值"并注明不是峰值**——上一轮就是因为把
   8 个采样点的最大值当成峰值保证而得出错误结论，这里是防复发；
3. 输入不是完整 32M 时**必须显式警告**，不能谎称"完整 MovieLens-32M"；
4. 缺产物（verification.json / precheck.json）时要能降级渲染并**明确标出缺失**，
   不能静默假装通过；
5. 状态必须用计划书的明确值集，失败状态不能被写成成功。

Python 3.6 兼容。
"""
import json
import os
import shutil
import unittest

from experiments.itemcf import conclusion_writer as C


PROJECT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
WORK = os.path.join(PROJECT, '.vmwork', '_conclusion_tests')


def fixture(name, **files):
    """建一个干净目录并写入给定 JSON 文件；返回目录。"""
    path = os.path.join(WORK, name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    for filename, payload in files.items():
        target = os.path.join(path, filename)
        parent = os.path.dirname(target)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        with open(target, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle)
    return path


#: 一份"全量成功"的 run_status（数字贴近真实全量，用于断言渲染）。
FULL_RUN_STATUS = {
    'status': 'success',
    'failure_reason': None,
    'run_id': '20261005T120000Z-abcd1234',
    'model_id': 'model-20261005T120000Z-abcd1234',
    'counts': {
        'input_rows': 32000204,
        'users': 200948,
        'items': 87585,
        'items_zero_like': 1234,
        'positive_pairs': 16000000,
        'graph_edges': 8000000,
        'graph_sources': 86000,
    },
    'config': {'max_k': 100, 'beta': 0.0, 'positive_threshold': 4.0,
               'block_size': 128, 'monitor_interval': 2.0,
               'duplicate_policy': 'reject'},
    'phase_seconds': {'prepare': 1800.0, 'precheck': 600.0,
                      'train': 9000.0, 'verify': 300.0},
    'seconds': 11700.0,
    'duplicate_check': {'rows_checked': 32000204, 'duplicate_occurrences': 0},
    'sampled_max': {
        'sample_count': 5850,
        'max_sample_gap_s': 2.4,
        'rss_sample_max_mb': 1873.5,
        'rss_sample_max_gi': 1.83,
        'mem_available_sample_min_mb': 412.0,
        'disk_free_sample_min_mb': 21000.0,
        'unmeasured_samples': 0,
    },
}

FULL_MANIFEST = {
    'run_id': '20261005T120000Z-abcd1234',
    'model_id': 'model-20261005T120000Z-abcd1234',
    'status': 'success',
    'input': {'path': '/home/h/movielens/ratings.csv',
              'sha256': 'a' * 64, 'rows': 32000204},
    'parameters': FULL_RUN_STATUS['config'],
}

FULL_MODEL_META = {
    'status': 'validated_unpublished',
    'run_id': '20261005T120000Z-abcd1234',
    'model_id': 'model-20261005T120000Z-abcd1234',
    'counts': {'n_edges': 8000000, 'n_items': 87585,
               'n_sources_with_neighbors': 86000},
}

FULL_VERIFICATION = {
    'pass': True,
    'run_id': '20261005T120000Z-abcd1234',
    'failures': [],
    'checks': {
        'recompute': {'checked_sources': 100, 'mismatch_count': 0,
                      'high_support_sources': 34, 'low_support_sources': 33,
                      'boundary_sources': 33, 'pass': True},
        'no_self_loops': {'self_loops': 0, 'pass': True},
        'zero_like_empty_intervals': {'empty_with_zero_like': 1234,
                                      'empty_without_zero_like': 0},
        'structure': {'pass': True},
        'n_like_from_input': {'mismatch_count': 0, 'pass': True},
        'id_consistency': {'pass': True},
    },
}


class RenderContractTests(unittest.TestCase):
    """计划书 §6 要求的七项内容必须都在结论里。"""

    def _render_full(self):
        run_dir = fixture('full', **{
            'run_status.json': FULL_RUN_STATUS,
            'manifest.json': FULL_MANIFEST,
            'precheck.json': {'input': FULL_MANIFEST['input'],
                              'projection': {'projected_hours_mean': 2.5,
                                             'projected_hours_max': 3.1,
                                             'assembly_reserve_seconds': 900.0}},
            'verification.json': FULL_VERIFICATION,
        })
        os.makedirs(os.path.join(run_dir, 'model'))
        with open(os.path.join(run_dir, 'model', 'model.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(FULL_MODEL_META, handle)
        return C.render(C.collect(run_dir))

    def test_all_seven_required_sections_present(self):
        text = self._render_full()
        for marker in ('## 1. 全部评分是否纳入', '## 2. 规模', '## 3. 阶段耗时',
                       '## 4. 资源', '## 5. 覆盖缺口', '## 6. 正确性核验',
                       '## 7. 是否可接入', '## 8. 本轮'):
            self.assertIn(marker, text, '缺少计划书 §6 要求的章节: %s' % marker)

    def test_scale_numbers_are_rendered(self):
        text = self._render_full()
        self.assertIn('32,000,204', text)      # 输入行数
        self.assertIn('200,948', text)         # 用户
        self.assertIn('87,585', text)          # 电影
        self.assertIn('8,000,000', text)       # 边

    def test_phase_seconds_rendered(self):
        text = self._render_full()
        for stage in ('prepare', 'precheck', 'train', 'verify'):
            self.assertIn(stage, text)

    def test_sampled_rss_is_labelled_as_sample_max_not_peak(self):
        """计划书 §5.A 的用词纪律：必须写"采样最大值"，不能写成峰值保证。"""
        text = self._render_full()
        self.assertIn('采样最大值', text)
        self.assertIn('不是精确连续峰值', text)
        self.assertIn('1873.5', text)          # 采样最大 RSS 数值
        # 措辞纪律：**不能**把采样值断言成"峰值"或"峰值保证"。正确写法是
        # "不是峰值保证"（否定式）——所以这里检查的是"没有肯定式用法"，
        # 而不是粗暴地断言"峰值保证"这四个字不出现（那样会误伤正确措辞）。
        for line in text.splitlines():
            if '峰值' not in line:
                continue
            # 每一处提到"峰值"的地方都必须带否定或限定词
            self.assertTrue(
                ('不是' in line) or ('未' in line) or ('不要' in line)
                or ('非' in line),
                '提到"峰值"的行缺少否定/限定措辞，可能被读成峰值保证: %r'
                % line)

    def test_explicitly_states_no_new_generalisation_evaluation(self):
        text = self._render_full()
        self.assertIn('没有进行新的泛化效果评估', text)
        self.assertIn('不在训练数据上报告 NDCG/Recall', text)

    def test_marks_unpublished_status_and_does_not_claim_online_active(self):
        text = self._render_full()
        self.assertIn('validated_unpublished', text)
        self.assertIn('不代表线上 active', text)


class HonestyTests(unittest.TestCase):
    """结论文件必须如实——宁可自曝其短，也不能把玩具结果说成全量。"""

    def test_non_full_input_is_flagged_loudly(self):
        """输入不是完整 32M 时必须警告，不能谎称"完整 MovieLens-32M"。"""
        rs = dict(FULL_RUN_STATUS)
        rs['counts'] = dict(FULL_RUN_STATUS['counts'], input_rows=3262)
        mf = dict(FULL_MANIFEST)
        mf['input'] = {'path': '/tmp/tiny.csv', 'sha256': 'b' * 64, 'rows': 3262}
        run_dir = fixture('tiny', **{'run_status.json': rs, 'manifest.json': mf})
        text = C.render(C.collect(run_dir))
        self.assertIn('不是', text)
        self.assertIn('完整', text)
        self.assertIn('不要把本轮结果当作全量结论', text)

    def test_full_input_is_not_flagged(self):
        run_dir = fixture('fullinput', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST})
        text = C.render(C.collect(run_dir))
        self.assertIn('完整', text)
        self.assertNotIn('不要把本轮结果当作全量结论', text)

    def test_missing_verification_is_reported_not_hidden(self):
        """没有 verification.json 时必须显式警告，不能假装已核验。"""
        run_dir = fixture('noverif', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST})
        text = C.render(C.collect(run_dir))
        self.assertIn('未找到 `verification.json`', text)
        self.assertIn('不要把模型视为可用', text)

    def test_unknown_input_rows_still_warns_about_scale(self):
        """D5 回归：``input_rows`` 未知时，"不是全量"的警告**不得整条消失**。

        旧实现写成 `... if rows is not None else ''`，于是 rows 缺失时那条警告
        退化成**一个空行**：规模行显示 N/A 却毫无提醒，读者会以为"没说就是没问题"。
        必须三态都有明确措辞（等于 32M / 不等于 32M / 未知）。
        """
        rs = dict(FULL_RUN_STATUS)
        rs['counts'] = {'users': 100, 'items': 50}   # 没有 input_rows
        run_dir = fixture('norows', **{'run_status.json': rs})
        text = C.render(C.collect(run_dir))
        self.assertIn('无法确认输入规模', text)
        self.assertIn('不要把本轮结果当作全量结论', text)

    def test_verification_skipped_is_not_reported_as_passed(self):
        """H1/H2 回归：跳过重算的 verification **不能**被渲染成"已通过独立核验"。

        组合缺陷：``verify_full_model`` 曾在跳过重算时报 pass=True，
        ``conclusion_writer`` 又只读顶层 pass，于是结论同时写出
        "独立核验总体：通过" 与 "重算 N/A 个来源"。两侧都已修：
        这里独立判断 ``recompute_ran``，不信顶层 pass。
        """
        vf = {'pass': True,   # 故意给一个"假 pass"，模拟老版本产物文件
              'checks': {'recompute': {'skipped': True,
                                       'checked_sources': None},
                         'no_self_loops': {'self_loops': 0, 'pass': True},
                         'structure': {'pass': True}}}
        run_dir = fixture('h1skip', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST,
            'verification.json': vf})
        os.makedirs(os.path.join(run_dir, 'model'))
        with open(os.path.join(run_dir, 'model', 'model.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(FULL_MODEL_META, handle)
        text = C.render(C.collect(run_dir))
        self.assertIn('独立核验未通过或未执行', text)
        self.assertNotIn('独立核验已通过', text)
        self.assertIn('不满足接入条件', text)

    def test_self_consistency_is_not_sold_as_independent_verification(self):
        """H2 回归：`validated_unpublished` 的凭据只有自查，不能被说成独立核验。"""
        run_dir = fixture('h2wording', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST,
            'verification.json': FULL_VERIFICATION})
        os.makedirs(os.path.join(run_dir, 'model'))
        with open(os.path.join(run_dir, 'model', 'model.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(FULL_MODEL_META, handle)
        text = C.render(C.collect(run_dir))
        # 必须显式说明该状态的凭据里"没有独立核验"
        self.assertIn('里面没有独立核验', text)
        # 两件事分开陈述
        self.assertIn('只说明文件自洽', text)
        self.assertIn('独立核验已通过', text)

    def test_failed_run_does_not_claim_ready_for_integration(self):
        rs = dict(FULL_RUN_STATUS, status='resource_blocked',
                  failure_reason='precheck did not pass')
        run_dir = fixture('blocked', **{
            'run_status.json': rs, 'manifest.json': FULL_MANIFEST})
        text = C.render(C.collect(run_dir))
        self.assertIn('resource_blocked', text)
        self.assertIn('不满足接入条件', text)
        self.assertNotIn('已通过独立核验', text)

    def test_resource_failure_is_not_scored_as_zero_effect(self):
        """计划书 §7：资源失败不能记成"算法效果零分"。"""
        rs = dict(FULL_RUN_STATUS, status='resource_failed',
                  failure_reason='rss_limit')
        run_dir = fixture('resfail', **{
            'run_status.json': rs, 'manifest.json': FULL_MANIFEST})
        text = C.render(C.collect(run_dir))
        self.assertIn('不是算法效果零分', text)

    def test_success_without_verification_is_not_declared_integratable(self):
        """success + validated_unpublished，但**没跑核验** → 不能说可接入。"""
        run_dir = fixture('successnoverif', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST})
        os.makedirs(os.path.join(run_dir, 'model'))
        with open(os.path.join(run_dir, 'model', 'model.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(FULL_MODEL_META, handle)
        text = C.render(C.collect(run_dir))
        self.assertIn('不满足接入条件', text)

    def test_verification_failure_blocks_integration_claim(self):
        vf = dict(FULL_VERIFICATION, pass_=False)
        vf['pass'] = False
        vf['failures'] = ['recompute mismatch on source 12345']
        rs = dict(FULL_RUN_STATUS, status='correctness_failed')
        run_dir = fixture('veriffail', **{
            'run_status.json': rs, 'manifest.json': FULL_MANIFEST,
            'verification.json': vf})
        os.makedirs(os.path.join(run_dir, 'model'))
        with open(os.path.join(run_dir, 'model', 'model.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(FULL_MODEL_META, handle)
        text = C.render(C.collect(run_dir))
        self.assertIn('不满足接入条件', text)
        self.assertIn('recompute mismatch on source 12345', text)


class DegradationTests(unittest.TestCase):
    """产物不全时必须能降级渲染（结论是摘要，不该因缺文件就崩）。"""

    def test_renders_with_only_run_status(self):
        run_dir = fixture('minimal', **{'run_status.json': FULL_RUN_STATUS})
        text = C.render(C.collect(run_dir))
        self.assertIn('# ItemCF 全量训练结论', text)
        self.assertIn('32,000,204', text)

    def test_renders_with_no_products_at_all(self):
        run_dir = fixture('empty')
        text = C.render(C.collect(run_dir))
        self.assertIn('# ItemCF 全量训练结论', text)

    def test_corrupt_json_is_tolerated(self):
        run_dir = fixture('corrupt')
        with open(os.path.join(run_dir, 'run_status.json'), 'w') as handle:
            handle.write('{not json')
        text = C.render(C.collect(run_dir))
        self.assertIn('# ItemCF 全量训练结论', text)

    def test_missing_precheck_still_renders(self):
        run_dir = fixture('noprecheck', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST})
        text = C.render(C.collect(run_dir))
        self.assertIn('## 3. 阶段耗时', text)


class WriteTests(unittest.TestCase):
    """``write_conclusion`` 的落盘契约。"""

    def test_writes_atomically_to_default_path(self):
        run_dir = fixture('write', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST})
        path = C.write_conclusion(run_dir)
        self.assertEqual(path, os.path.join(run_dir, 'conclusion.md'))
        self.assertTrue(os.path.isfile(path))
        with open(path, encoding='utf-8') as handle:
            text = handle.read()
        self.assertIn('# ItemCF 全量训练结论', text)
        # 原子写：不留 .tmp
        self.assertFalse(os.path.isfile(path + '.tmp'))

    def test_respects_explicit_out_path(self):
        run_dir = fixture('write2', **{'run_status.json': FULL_RUN_STATUS})
        target = os.path.join(run_dir, 'custom.md')
        self.assertEqual(C.write_conclusion(run_dir, out_path=target), target)
        self.assertTrue(os.path.isfile(target))

    def test_rerender_is_idempotent(self):
        run_dir = fixture('write3', **{
            'run_status.json': FULL_RUN_STATUS, 'manifest.json': FULL_MANIFEST})
        first = open(C.write_conclusion(run_dir), encoding='utf-8').read()
        second = open(C.write_conclusion(run_dir), encoding='utf-8').read()
        self.assertEqual(first, second)


class StatusLabelTests(unittest.TestCase):
    """状态值必须覆盖计划书 §6 的明确取值集。"""

    def test_all_planned_statuses_have_labels(self):
        for status in ('success', 'resource_blocked', 'resource_failed',
                       'monitor_failed', 'correctness_failed'):
            self.assertIn(status, C.STATUS_LABELS,
                          'status %r 缺标签' % status)

    def test_unknown_status_is_flagged_not_silently_accepted(self):
        rs = dict(FULL_RUN_STATUS, status='something_new')
        run_dir = fixture('unknown', **{'run_status.json': rs})
        text = C.render(C.collect(run_dir))
        self.assertIn('未在计划书状态表内', text)


if __name__ == '__main__':
    unittest.main()
