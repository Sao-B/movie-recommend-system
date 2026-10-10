# -*- coding: utf-8 -*-
"""配对 bootstrap 区间与逐用户差值（计划书 6.4 硬要求）。

从一次运行的原始记录出发，**重算**每个方法每个用户的 NDCG@20 / Recall@20，
再对每个 ItemCF 配置 vs 基线（MostPop、LikePop）做配对 bootstrap：

- 同一用户索引同时抽给两个模型（配对，计划书 6.4）；
- ``seed=42``、``repeats=2000``；
- 报告均值差与 **percentile 95% 区间**。

产出两份产物：

- ``paired_differences.csv``：逐用户逐指标的真实差（ItemCF - 基线），可复核；
- ``bootstrap.json``：每个 (配置, 基线, 指标) 的均值差、CI95、样本数、seed/repeats。

**明确注明**：区间基于**同一验证集选参之后**的探索性比较，不能把"区间不排除 0"
当成已做独立显著性验证（计划书 6.4 原文要求）。

与 ``verify_results.py`` 一致，这里**不 import** 项目的指标实现，独立按规格重算：
Recall = |Top-N ∩ R_u| / |R_u|；NDCG 的 IDCG 按 min(20, |R_u|)。

用法::

    python -m experiments.itemcf.bootstrap_ci --run-dir <out>

Python 3.6 兼容；只用标准库 + NumPy。
"""
import argparse
import csv
import json
import math
import os
import random
import sys

#: 配对抽样参数（计划书 6.4 固定值，不因结果好看与否而改）。
BOOTSTRAP_SEED = 42
BOOTSTRAP_REPEATS = 2000

#: 参与比较的基线（计划书 6.4：ItemCF 配置 vs 人气基线）。
BASELINES = ('MostPop', 'LikePop')

#: 选参规则（计划书 6.4；写进产物便于复核）。
SELECTION_RULE = ('NDCG@20 desc -> Recall@20 desc -> smaller k -> smaller beta '
                  '(tie tolerance 1e-12)')


def _read_csv(path):
    if not os.path.isfile(path):
        return []
    with open(path, newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def _parse_config_name(name):
    """``k100_beta0`` -> ``(100, 0.0)``；不是 ItemCF 配置名返回 (None, None)。"""
    if not name or not name.startswith('k') or '_beta' not in name:
        return None, None
    head, _, tail = name[1:].partition('_beta')
    try:
        return int(head), float(tail)
    except ValueError:
        return None, None


def recompute_per_user(run_dir):
    """独立重算逐用户 NDCG@20 / Recall@20；返回 ``{model: {user: (ndcg, recall)}}``。

    真值集取自 ``per_user.csv`` 的 ``relevant_ids``（评测协议的一部分），
    推荐列表取自 ``recommendations.csv`` 并按 rank 排序——两者都是原始记录，
    不读任何已算好的模型级汇总值。
    """
    per_user = _read_csv(os.path.join(run_dir, 'per_user.csv'))
    recs = _read_csv(os.path.join(run_dir, 'recommendations.csv'))
    by_key = {}
    for row in recs:
        key = (row.get('model', ''), int(row['user_id']))
        by_key.setdefault(key, []).append(row)

    out = {}
    for row in per_user:
        model = row.get('model', '')
        user = int(row['user_id'])
        if not row.get('relevant_ids'):
            continue
        rel = set(int(x) for x in row['relevant_ids'].split('|') if x)
        if not rel:
            continue
        rows = sorted(by_key.get((model, user), []), key=lambda r: int(r['rank']))
        ids = [int(r['movie_id']) for r in rows]
        if len(ids) != len(set(ids)):
            raise ValueError('duplicate recommendations for %s/user %d' % (model, user))
        recall = sum(1 for i in ids if i in rel) / float(len(rel))
        dcg = sum(1.0 / math.log2(pos + 2) for pos, i in enumerate(ids) if i in rel)
        idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(20, len(rel))))
        ndcg = dcg / idcg if idcg else 0.0
        out.setdefault(model, {})[user] = (ndcg, recall)
    return out


def paired_bootstrap(values_a, values_b, seed=BOOTSTRAP_SEED,
                     repeats=BOOTSTRAP_REPEATS):
    """相同用户索引的有放回配对抽样 → 均值差 + percentile 95% 区间。

    与 ``evaluation.paired_bootstrap`` 同一算法，但**独立实现**（本模块不 import
    项目代码），这样区间本身也被一份独立代码复核过。
    """
    if len(values_a) != len(values_b):
        raise ValueError('paired bootstrap requires equal-length values')
    if not values_a:
        raise ValueError('paired bootstrap requires non-empty values')
    diffs = [float(a) - float(b) for a, b in zip(values_a, values_b)]
    n = len(diffs)
    mean_diff = sum(diffs) / float(n)
    rng = random.Random(seed)
    samples = []
    for _ in range(repeats):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    low = samples[int(math.floor(0.025 * (repeats - 1)))]
    high = samples[int(math.ceil(0.975 * (repeats - 1)))]
    return {'mean_difference': mean_diff, 'ci95_low': low, 'ci95_high': high,
            'seed': seed, 'repeats': repeats, 'users': n}


def build(run_dir):
    """产出 ``paired_differences.csv`` 与 ``bootstrap.json``；返回摘要 dict。"""
    run_dir = os.path.abspath(run_dir)
    metrics = recompute_per_user(run_dir)
    if not metrics:
        raise ValueError('no per-user records with relevant_ids in %s' % run_dir)

    baselines = [b for b in BASELINES if b in metrics]
    items = sorted(m for m in metrics
                   if m not in BASELINES and _parse_config_name(m)[0] is not None)
    items = sorted(items, key=lambda m: (_parse_config_name(m)[0],
                                         _parse_config_name(m)[1]))
    if not baselines:
        raise ValueError('no baseline (%s) found in per_user.csv' % ', '.join(BASELINES))

    # ---- 逐用户差值（ItemCF - 基线），共同用户交集上配对 ----
    diff_rows = []
    for item in items:
        for base in baselines:
            shared = sorted(set(metrics[item]) & set(metrics[base]))
            for user in shared:
                mine, theirs = metrics[item][user], metrics[base][user]
                diff_rows.append({
                    'model': item, 'baseline': base, 'user_id': user,
                    'ndcg_item': '%.17g' % mine[0], 'ndcg_baseline': '%.17g' % theirs[0],
                    'ndcg_diff': '%.17g' % (mine[0] - theirs[0]),
                    'recall_item': '%.17g' % mine[1],
                    'recall_baseline': '%.17g' % theirs[1],
                    'recall_diff': '%.17g' % (mine[1] - theirs[1]),
                })
    fields = ['model', 'baseline', 'user_id', 'ndcg_item', 'ndcg_baseline',
              'ndcg_diff', 'recall_item', 'recall_baseline', 'recall_diff']
    with open(os.path.join(run_dir, 'paired_differences.csv'), 'w',
              newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in diff_rows:
            writer.writerow(row)

    # ---- bootstrap 区间 ----
    comparisons = {}
    for item in items:
        for base in baselines:
            shared = sorted(set(metrics[item]) & set(metrics[base]))
            mine = [metrics[item][u][0] for u in shared]
            theirs = [metrics[base][u][0] for u in shared]
            ndcg = paired_bootstrap(mine, theirs)
            mine_r = [metrics[item][u][1] for u in shared]
            theirs_r = [metrics[base][u][1] for u in shared]
            recall = paired_bootstrap(mine_r, theirs_r)
            comparisons['%s vs %s' % (item, base)] = {
                'model': item, 'baseline': base, 'users': len(shared),
                'itemcf_mean_ndcg@20': sum(mine) / len(mine),
                'baseline_mean_ndcg@20': sum(theirs) / len(theirs),
                'ndcg@20': ndcg,
                'itemcf_mean_recall@20': sum(mine_r) / len(mine_r),
                'baseline_mean_recall@20': sum(theirs_r) / len(theirs_r),
                'recall@20': recall,
                'excludes_zero_ndcg': (ndcg['ci95_low'] > 0.0 or ndcg['ci95_high'] < 0.0),
                'excludes_zero_recall': (recall['ci95_low'] > 0.0
                                         or recall['ci95_high'] < 0.0),
            }

    payload = {
        'run_dir': run_dir,
        'seed': BOOTSTRAP_SEED,
        'repeats': BOOTSTRAP_REPEATS,
        'baselines': baselines,
        'configs': items,
        'selection_rule': SELECTION_RULE,
        'method': 'paired bootstrap: same user indices resampled for both models',
        'interval_type': 'percentile 95%',
        'caveat': ('Exploratory interval on the SAME validation set used to select '
                   'the configuration (plan 6.4). A CI that excludes 0 is NOT an '
                   'independent significance test: no held-out test set was used '
                   'for selection, and multiple configurations were compared.'),
        'caveat_zh': ('这是同一验证集选参后的探索性区间（计划书 6.4）；区间不排除 0 '
                      '**不构成**已做独立显著性验证——选参与评估共用验证集，'
                      '且同时比较了多个配置，未做多重比较校正。'),
        'comparisons': comparisons,
    }
    with open(os.path.join(run_dir, 'bootstrap.json'), 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    return {'run_dir': run_dir, 'configs': len(items), 'baselines': baselines,
            'comparisons': len(comparisons), 'diff_rows': len(diff_rows),
            'bootstrap': os.path.join(run_dir, 'bootstrap.json'),
            'paired_differences': os.path.join(run_dir, 'paired_differences.csv')}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Paired bootstrap CI for ItemCF')
    parser.add_argument('--run-dir', required=True)
    args = parser.parse_args(argv)
    result = build(args.run_dir)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
