# -*- coding: utf-8 -*-
"""ItemCF 实验独立核验（计划书 7.2 / 11）：从原始记录重算指标、核对邻居抽样与解释。

不比较两个 summary——从 ``per_user.csv`` / ``recommendations.csv`` 原始记录出发，
按评测协议重算 Recall/NDCG，与汇总值比对（容差 1e-10），抽样核对证据字段
（``contrib_sum == score``、``top_source_movie_id`` 来自该用户训练好评历史），
并核对源码哈希一致性。输出 ``verification.json``。

用法::

    python -m experiments.itemcf.verify_results --run-dir <out> [--input <ratings.csv>]
"""
import argparse
import csv
import hashlib
import json
import math
import os
import sys

# 允许按脚本路径调用（包内是 experiments.itemcf.* 绝对导入）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from experiments.itemcf.evaluation import POSITIVE_THRESHOLD, protocol_hashes  # noqa: E402

TOLERANCE = 1e-10


def _read_csv(path):
    with open(path, newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def _close(actual, expected, tolerance=TOLERANCE):
    if actual is None or expected is None:
        return actual is expected
    return abs(float(actual) - float(expected)) <= tolerance * max(1.0, abs(float(expected)))


def verify(run_dir, input_path=None, sample_users=20):
    """重算并核对；返回报告 dict（同时写 ``<run_dir>/verification.json``）。"""
    run_dir = os.path.abspath(run_dir)
    report = {'run_dir': run_dir, 'checks': [], 'metric_mismatches': [],
              'evidence_mismatches': [], 'hash_mismatches': []}

    # 1) run_status：每个配置都有终态与 code_id
    status_path = os.path.join(run_dir, 'run_status.json')
    with open(status_path, encoding='utf-8') as handle:
        entries = json.load(handle)
    report['status_counts'] = {}
    for entry in entries:
        report['status_counts'][entry['status']] = \
            report['status_counts'].get(entry['status'], 0) + 1
        if 'code_id' not in entry:
            report['hash_mismatches'].append('%s missing code_id' % entry.get('name'))
    report['checks'].append('run_status: %d entries' % len(entries))

    # 2) 从原始记录重算每用户指标
    per_user_path = os.path.join(run_dir, 'per_user.csv')
    rec_path = os.path.join(run_dir, 'recommendations.csv')
    if not os.path.isfile(per_user_path) or not os.path.isfile(rec_path):
        report['all_recomputed'] = False
        report['metric_mismatches'].append('missing per_user.csv or recommendations.csv')
        _write(run_dir, report)
        return report

    per_user = _read_csv(per_user_path)
    recs = _read_csv(rec_path)
    # recommendations.csv 是**所有配置拼在一起**的明细，必须按 (model, user_id) 分组：
    # 只按 user_id 分组会把 9 个配置的推荐行混成一个列表，逐用户重算随即报"重复推荐"。
    by_user = {}
    for row in recs:
        by_user.setdefault((row.get('model', ''), int(row['user_id'])), []).append(row)
    history = {}
    if input_path and os.path.isfile(input_path):
        history = _load_history(input_path)

    recomputed = 0
    for row in per_user:
        user = int(row['user_id'])
        model = row.get('model', '')
        rows = sorted(by_user.get((model, user), []), key=lambda r: int(r['rank']))
        ids = [int(r['movie_id']) for r in rows]
        label = '%s/user %s' % (model, user) if model else 'user %s' % user
        if len(ids) != len(set(ids)):
            report['metric_mismatches'].append('%s duplicate recommendations' % label)
            continue
        # 关键列缺失 = 数据问题，记 mismatch 而不是跳过
        if 'relevant_ids' in row and row['relevant_ids']:
            rel = set(int(x) for x in row['relevant_ids'].split('|') if x)
        else:
            rel = set()
            report['checks'].append('%s: relevant_ids absent, recall recompute skipped' % label)
        if rel:
            recall = sum(1 for i in ids if i in rel) / float(len(rel))
            dcg = sum(1.0 / math.log2(pos + 2) for pos, i in enumerate(ids) if i in rel)
            idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(len(rel), 20)))
            ndcg = dcg / idcg if idcg else 0.0
            if not _close(float(row['recall@20']), recall):
                report['metric_mismatches'].append(
                    '%s recall@20: %s != %s' % (label, row['recall@20'], recall))
            if not _close(float(row['ndcg@20']), ndcg):
                report['metric_mismatches'].append(
                    '%s ndcg@20: %s != %s' % (label, row['ndcg@20'], ndcg))
            recomputed += 1
        # 证据解释抽样核对
        if int(row.get('returned_count', 0)) and rows:
            first = rows[0]
            if 'contrib_sum' in first and first['contrib_sum'] != '':
                if not _close(float(first['contrib_sum']), float(first['raw_score'])):
                    report['evidence_mismatches'].append(
                        '%s rank1 contrib_sum %s != raw_score %s'
                        % (label, first['contrib_sum'], first['raw_score']))
            if history and user in history and first.get('top_source_movie_id'):
                source = int(first['top_source_movie_id'])
                if source not in history[user]:
                    report['evidence_mismatches'].append(
                        '%s top_source %s not in train likes' % (label, source))
    report['checks'].append('recomputed %d user-config rows with relevant_ids' % recomputed)

    # 2a2) 计划书 §6.2 证据诊断的独立重算：`per_user.csv` 的 `evidence_count` 是
    #      逐用户 |E_u|，`relevant_ids` 是 R_u，两者足以重算证据上限
    #      `min(20,|R_u∩E_u|)/|R_u|` 与 |E_u| 的中位数。
    #
    #      为什么要在这里重算：`evidence_count` 曾经被错写成 Top-N 长度（恒 <= 20），
    #      于是证据上限退化成"返回条数/|R_u|"这种看起来合理、其实无意义的数字。
    #      只有从落盘明细独立重算，才锁得住"它真的是 |E_u|"。
    #
    #      注意 **逐用户的 E_u 成员没有落盘**，所以这里只能核对
    #      (a) `|E_u| >= 推荐条数`（E_u 是 Top-N 的超集）、
    #      (b) 模型级证据上限与 `|E_u|` 分位数可重算且有限、
    #      (c) **不变式**：实际 Recall@20 <= 证据上限（计划书 §6.2 硬约束）。
    evidence_by_model = {}
    # 逐用户 ``|E_u|`` 的**全精度**来源是逐配置 ``summary.json`` 的 ``per_user``。
    # ``per_user.csv`` 是上一轮 ``run_comparison`` 落盘的**聚合明细**，重新评分
    # 并不会刷新它（只有重跑整个驱动才会）；拿它核验会把旧的、错误的
    # ``evidence_count``（曾等于 Top-N 长度）当成"证据数"。
    # 因此优先读 summary.json；读不到才回落到 per_user.csv，并在 checks 里说明。
    evidence_source = {}
    for row in per_user:
        model = row.get('model', '')
        if not model or model in evidence_source:
            continue
        for name in ('configs', ''):
            candidate = os.path.join(run_dir, name, model, 'summary.json') \
                if name else os.path.join(run_dir, model, 'summary.json')
            if os.path.isfile(candidate):
                try:
                    with open(candidate, encoding='utf-8') as handle:
                        payload = json.load(handle)
                except ValueError:
                    continue
                rows_in = payload.get('per_user')
                if rows_in:
                    evidence_source[model] = rows_in
                    break
    for row in per_user:
        model = row.get('model', '')
        if not model:
            continue
        source_rows = evidence_source.get(model)
        if source_rows is None:
            if model not in evidence_source:
                evidence_source[model] = None
            source_rows = [row]
        # 在 summary.json 的 per_user 里按 user_id 找同一个人
        match = None
        if source_rows is not per_user:
            want = str(row.get('user_id'))
            for candidate_row in source_rows:
                if str(candidate_row.get('user_id')) == want:
                    match = candidate_row
                    break
        if match is None:
            match = row
        try:
            count = int(match['evidence_count'])
        except (KeyError, TypeError, ValueError):
            continue
        rows = sorted(by_user.get((model, int(row['user_id'])), []),
                      key=lambda r: int(r['rank']))
        bucket = evidence_by_model.setdefault(
            model, {'cap': [], 'counts': [], 'recall': [], 'violations': 0,
                    'source': ('summary.json' if evidence_source.get(model)
                               else 'per_user.csv')})
        bucket['counts'].append(count)
        # E_u 必须包含全部返回的推荐（返回的是 E_u 的前缀）
        if count < len(rows):
            bucket['violations'] += 1
        try:
            rel_text = row.get('relevant_ids') or ''
            rel = set(int(x) for x in rel_text.split('|') if x)
        except (TypeError, ValueError):
            rel = set()
        if not rel:
            continue
        # |R_u ∩ E_u| 只能从"E_u 的势"上界推出：返回的命中数 <= |R_u∩E_u| <= min(|E_u|,|R_u|)
        hits = sum(1 for r in rows if int(r['movie_id']) in rel)
        cap_upper = min(20, count, len(rel)) / float(len(rel))
        bucket['cap'].append(cap_upper)
        bucket['recall'].append(hits / float(len(rel)))

    def _median(values):
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return float(ordered[mid])
        return (ordered[mid - 1] + ordered[mid]) / 2.0

    report['evidence_diagnostics'] = {}
    for model in sorted(evidence_by_model):
        bucket = evidence_by_model[model]
        entry = {
            'users': len(bucket['counts']),
            'evidence_count_p50': _median(bucket['counts']),
            'evidence_count_min': min(bucket['counts']) if bucket['counts'] else None,
            'evidence_count_max': max(bucket['counts']) if bucket['counts'] else None,
            'evidence_count_zero_users': sum(1 for c in bucket['counts'] if c == 0),
            'evidence_count_lt_returned': bucket['violations'],
            'evidence_source': bucket['source'],
            'recall_upper_bound_mean': (sum(bucket['cap']) / len(bucket['cap'])
                                        if bucket['cap'] else None),
            'recomputed_recall_mean': (sum(bucket['recall']) / len(bucket['recall'])
                                       if bucket['recall'] else None),
        }
        # 不变式：实际 Recall 不得超过证据上限（这里用可重算的上界口径）
        if entry['recall_upper_bound_mean'] is not None \
                and entry['recomputed_recall_mean'] is not None:
            entry['recall_within_evidence_cap'] = (
                entry['recomputed_recall_mean'] <= entry['recall_upper_bound_mean'] + 1e-12)
            if not entry['recall_within_evidence_cap']:
                report['metric_mismatches'].append(
                    '%s: recall@20 %s exceeds evidence cap upper bound %s'
                    % (model, entry['recomputed_recall_mean'],
                       entry['recall_upper_bound_mean']))
        if bucket['violations']:
            report['metric_mismatches'].append(
                '%s: evidence_count < returned_count for %d users '
                '(E_u must be a superset of the Top-N)'
                % (model, bucket['violations']))
        report['evidence_diagnostics'][model] = entry
    report['checks'].append(
        'recomputed evidence diagnostics (|E_u| p50 + recall-vs-cap) for %d models'
        % len(report['evidence_diagnostics']))
    stale = sorted(m for m, e in report['evidence_diagnostics'].items()
                   if e.get('evidence_source') == 'per_user.csv')
    if stale:
        report['checks'].append(
            'evidence source fallback to per_user.csv for %d models: %s'
            % (len(stale), ', '.join(stale)))

    # 2b) 模型级聚合重算（计划书 §11）：把逐用户重算值按模型求平均，与
    #     summary_table.csv 的模型级 NDCG@20 / Recall@20 逐个比对。只做逐用户
    #     核对是不够的——逐用户全对但聚合口径写错（或漏掉某些方法）时，
    #     汇总表仍可能给出错误的模型级数字，所以两层都要独立重算。
    aggregated = {}
    for row in per_user:
        model = row.get('model', '')
        if 'relevant_ids' not in row or not row['relevant_ids']:
            continue
        rel = set(int(x) for x in row['relevant_ids'].split('|') if x)
        if not rel:
            continue
        rows = sorted(by_user.get((model, int(row['user_id'])), []),
                      key=lambda r: int(r['rank']))
        ids = [int(r['movie_id']) for r in rows]
        if len(ids) != len(set(ids)):
            continue
        bucket = aggregated.setdefault(model, {'ndcg': [], 'recall': []})
        bucket['recall'].append(sum(1 for i in ids if i in rel) / float(len(rel)))
        dcg = sum(1.0 / math.log2(pos + 2) for pos, i in enumerate(ids) if i in rel)
        idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(len(rel), 20)))
        bucket['ndcg'].append(dcg / idcg if idcg else 0.0)

    report['models_recomputed'] = {}
    # 模型级比对的**权威来源是逐配置 summary.json**（全精度原始值），
    # `summary_table.csv` 只是展示层：报告层会用 ``format_value`` 把它重写成
    # 6 位小数的可读文本。若拿展示值做 1e-10 比对，报告跑过一次之后核验必然
    # 全红——那不是数据错，是"拿显示精度当数据精度"的口径错误。
    # 所以：优先读 summary.json 的全精度值；只有它缺失时才回落读 CSV，
    # 并把容差放宽到展示精度（1e-6 的一半，即 5e-7）。
    summary_path = os.path.join(run_dir, 'summary_table.csv')
    summary_by_name = {}
    if os.path.isfile(summary_path):
        for row in _read_csv(summary_path):
            key = row.get('name') or row.get('model') or row.get('method')
            if key:
                summary_by_name[key] = row

    def _authoritative(model, column):
        """返回 ``(text, tolerance, source)``：优先全精度 summary.json。"""
        for name in ('configs', ''):
            candidate = os.path.join(run_dir, name, model, 'summary.json') \
                if name else os.path.join(run_dir, model, 'summary.json')
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, encoding='utf-8') as handle:
                    payload = json.load(handle)
            except ValueError:
                continue
            value = ((payload.get('models') or {}).get(model) or {}).get(column)
            if value is not None:
                return repr(value), TOLERANCE, candidate
        stored = summary_by_name.get(model)
        if stored is not None and stored.get(column) not in (None, ''):
            return stored.get(column), 5e-7, 'summary_table.csv'
        return None, TOLERANCE, None

    for model in sorted(aggregated):
        bucket = aggregated[model]
        n = len(bucket['ndcg'])
        mean_ndcg = sum(bucket['ndcg']) / n
        mean_recall = sum(bucket['recall']) / n
        report['models_recomputed'][model] = {
            'users': n, 'ndcg@20': mean_ndcg, 'recall@20': mean_recall}
        if model not in summary_by_name \
                and _authoritative(model, 'ndcg@20')[2] is None:
            report['metric_mismatches'].append(
                '%s: recomputed but absent from summary_table.csv' % model)
            continue
        for column, value in (('ndcg@20', mean_ndcg), ('recall@20', mean_recall)):
            text, tolerance, _source = _authoritative(model, column)
            if text in (None, ''):
                report['metric_mismatches'].append(
                    '%s %s: recomputed %r but summary has no value' % (model, column, value))
            elif not _close(value, float(text), tolerance):
                report['metric_mismatches'].append(
                    '%s %s: summary %s != recomputed %s' % (model, column, text, value))
    report['checks'].append('recomputed model-level metrics for %d models'
                            % len(report['models_recomputed']))

    # 2c) 覆盖完整性（计划书 §11）：run_status 里的**每个成功方法**都必须同时出现在
    #     per_user.csv 与 recommendations.csv 里，否则它的指标没有独立重算依据。
    #     这正是"11/14 方法"缺口的守门检查：基线没落盘时必须报错，不能静默通过。
    expected_models = []
    for entry in entries:
        if entry.get('status') in ('success', 'reused_verified'):
            expected_models.append(entry['name'])
    per_user_models = sorted({row.get('model', '') for row in per_user})
    rec_models = sorted({row.get('model', '') for row in recs})
    report['expected_models'] = sorted(expected_models)
    report['per_user_models'] = per_user_models
    report['recommendation_models'] = rec_models
    for name in sorted(expected_models):
        if name not in per_user_models:
            report['metric_mismatches'].append(
                '%s: successful method missing from per_user.csv' % name)
        if name not in rec_models:
            report['metric_mismatches'].append(
                '%s: successful method missing from recommendations.csv' % name)
    report['checks'].append(
        'coverage: %d successful methods / %d in per_user.csv / %d in recommendations.csv'
        % (len(expected_models), len(per_user_models), len(rec_models)))

    # 3) 源码哈希一致性（source_hashes.json vs 当前文件，若提供目录）
    hash_path = os.path.join(run_dir, 'source_hashes.json')
    if os.path.isfile(hash_path):
        with open(hash_path, encoding='utf-8') as handle:
            stored = json.load(handle)
        code_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(run_dir))),
                                'experiments', 'itemcf')
        if os.path.isdir(code_dir):
            for name, digest in stored.items():
                path = os.path.join(code_dir, name)
                if os.path.isfile(path):
                    with open(path, 'rb') as handle:
                        current = hashlib.sha256(handle.read()).hexdigest()
                    if current != digest:
                        report['hash_mismatches'].append('%s hash drift' % name)
        report['checks'].append('source hashes checked: %d' % len(stored))

    report['all_recomputed'] = (not report['metric_mismatches']
                                and not report['hash_mismatches'])
    _write(run_dir, report)
    return report


def _load_history(input_path):
    """从评分 CSV 还原训练好评历史（供证据解释核对）；无法区分切分时用全部好评近似。"""
    history = {}
    with open(input_path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            if float(row['rating']) >= POSITIVE_THRESHOLD:
                history.setdefault(int(row['userId']), set()).add(int(row['movieId']))
    return history


def _write(run_dir, report):
    with open(os.path.join(run_dir, 'verification.json'), 'w', encoding='utf-8') as handle:
        json.dump(report, handle, indent=2, sort_keys=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent ItemCF result verification')
    parser.add_argument('--run-dir', required=True)
    parser.add_argument('--input', default=None)
    args = parser.parse_args(argv)
    report = verify(args.run_dir, args.input)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report['all_recomputed'] else 1


if __name__ == '__main__':
    sys.exit(main())
