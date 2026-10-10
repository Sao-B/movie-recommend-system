# -*- coding: utf-8 -*-
"""ItemCF 实验的评测层（计划书 2.3、6 节）：固定评测用户、指标/证据、分层、配对区间。

契约要点：
- ``build_eval_context`` 只接收训练段与验证段；**保留测试段不进入任何指标**
  （数据防泄漏，计划书 2.2 节）。
- 指标函数复用 :mod:`experiments.pilot.metrics` 的 Recall/NDCG，不重写；
  Recall 分母是 ``|R_u|``，NDCG 的 IDCG 按 ``min(K, |R_u|)``，短列表/空列表
  未返回的位置贡献 0，**不按实际返回长度重算 IDCG**。
- 所有均值都在共同评测用户集（``|R_u| > 0``）上先逐人算再求平均；正例全在
  候选外或无证据的用户计零分，不从分母剔除。

Python 3.6 兼容；只依赖标准库 + ``experiments.pilot.metrics``。
"""
import hashlib
import json
import math
import random
from collections import OrderedDict, namedtuple

from experiments.pilot.metrics import ndcg_at_k, recall_at_k, recommendable_positive_ratio

#: 好评阈值（计划书 2.3 / 3.1）。
POSITIVE_THRESHOLD = 4.0

#: 主指标 K（计划书 6.1）；K=5/10 用同一份 Top-20 前缀。
DEFAULT_K_VALUES = (5, 10, 20)

#: 训练好评条数分层（计划书 6.3）。
HISTORY_LAYERS = ('0', '1-5', '6-20', '>20')

#: 训练 ``n_all(i)`` 固定分箱（计划书 6.3）；None 表示候选外。
CANDIDATE_BINS = ['out_of_candidate', '1-4', '5-19', '20-99', '>=100']

#: 邻居图截断的默认 max_k（计划书 5.1：三张 β 图各存最多 100 邻居）。
N_LIKE_MAX = 100

#: 配对 bootstrap 默认参数（计划书 6.4）。
BOOTSTRAP_SEED = 42
BOOTSTRAP_REPEATS = 2000

#: 参数选择平手容差（计划书 6.4）。
SELECTION_TIE_TOLERANCE = 1e-12

EvalContext = namedtuple(
    'EvalContext',
    'item_ids item_index train val eval_users excluded_no_positive '
    'seen relevant candidates history_len n_like n_all')


# ---------------------------------------------------------------------------
# 评测上下文（计划书 2.3）
# ---------------------------------------------------------------------------

def build_eval_context(train, val):
    """由训练段/验证段构造固定评测协议。

    保留测试段**不是**本函数的输入（指标层不得接触 test 段，计划书 2.2 防泄漏）；
    需要三段总行数做协议核对时，由调用方另传 ``total_rows``（见
    :func:`experiments.itemcf.run_itemcf.expected_protocol`）。

    :param train: ``(userId, movieId, rating, timestamp)`` 训练段行。
    :param val: 验证段行（只用于 ``R_u``）。
    :returns: :class:`EvalContext`：

      - ``item_ids``：``I_train``，movieId 数值升序；
      - ``seen[u]``：训练段评过的全部物品（含低分）；
      - ``relevant[u]``：验证段 rating >= 4.0 的去重物品；
      - ``candidates[u] = I_train - seen[u]``；
      - ``eval_users``：``|R_u| > 0`` 的 userId 升序；其余进 ``excluded_no_positive``；
      - ``history_len[u]``：``|H_u|``（训练好评条数）；``n_like`` / ``n_all``：计数。
    """
    item_ids = sorted({int(row[1]) for row in train})
    item_index = dict((i, p) for p, i in enumerate(item_ids))
    seen, history_len, n_like, n_all = {}, {}, {}, {}
    for row in train:
        user, item, rating = int(row[0]), int(row[1]), float(row[2])
        seen.setdefault(user, set()).add(item)
        n_all[item] = n_all.get(item, 0) + 1
        if rating >= POSITIVE_THRESHOLD:
            history_len[user] = history_len.get(user, 0) + 1
            n_like[item] = n_like.get(item, 0) + 1
    relevant = {}
    for row in val:
        user, item, rating = int(row[0]), int(row[1]), float(row[2])
        if rating >= POSITIVE_THRESHOLD:
            relevant.setdefault(user, set()).add(item)
    item_set = set(item_ids)
    candidates = dict((u, item_set - seen.get(u, set())) for u in seen)
    for u in relevant:
        candidates.setdefault(u, set(item_set))
        seen.setdefault(u, set())
    eval_users = sorted(u for u, rel in relevant.items() if rel)
    excluded = sorted(set(seen) | set(relevant))
    excluded = [u for u in excluded if u not in set(eval_users)]
    for u in eval_users:
        history_len.setdefault(u, 0)
    return EvalContext(item_ids, item_index, train, val,
                       eval_users, excluded, seen, relevant, candidates,
                       history_len, n_like, n_all)


def _digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def candidate_universe_hash(item_ids):
    """候选全集哈希：只认集合，与列表顺序无关（计划书 2.3）。"""
    return _digest(sorted(set(int(i) for i in item_ids)))


def user_seen_hash(seen):
    """逐用户 ``Seen_u`` 哈希。"""
    return _digest(sorted(set(int(i) for i in seen)))


def user_relevant_hash(relevant):
    """逐用户 ``R_u`` 哈希（与 seen 哈希区分，避免误用同一份集合）。"""
    return _digest(sorted(set(int(i) for i in relevant)))


def protocol_hashes(context):
    """所有方法必须导出的共同标识：有序用户清单 + 候选哈希 + 逐用户哈希。"""
    per_user = OrderedDict()
    for user in context.eval_users:
        per_user[str(user)] = {
            'seen': user_seen_hash(context.seen.get(user, ())),
            'relevant': user_relevant_hash(context.relevant.get(user, ())),
            'candidate_count': len(context.candidates.get(user, ())),
        }
    return {
        'eval_users': list(context.eval_users),
        'candidate_universe_hash': candidate_universe_hash(context.item_ids),
        'per_user': per_user,
    }


# ---------------------------------------------------------------------------
# 指标（复用 pilot 的 Recall/NDCG）
# ---------------------------------------------------------------------------

def evidence_max_recall(relevant, evidence, k):
    """证据集 Recall@20 上限：``min(K, |R_u ∩ E_u|) / |R_u|``（计划书 6.2）。

    ``relevant`` 为空返回 None（该用户不参与均值）。
    """
    rel = set(relevant)
    if not rel:
        return None
    return min(k, len(rel & set(evidence))) / float(len(rel))


def per_user_metrics(recommendations, relevants, k_values=DEFAULT_K_VALUES,
                     evidence=None, k_cap=None):
    """逐用户排序指标（共同评测用户逐人计算，之后才能求均值）。

    :param recommendations: 有序 movieId 列表的列表（不足 top_n 的短列表合法）。
    :param relevants: ``R_u`` 集合的列表，与 recommendations 一一对应。
    :param evidence: 可选 ``{序号: E_u 集合}``，提供时计算证据上限与覆盖率。
    :returns: dict of list：``recall@K``、``ndcg@K``、``evidence_max_recall@K``、
      ``evidence_ratio``、``returned``、``users_scored``。

    空列表/短列表按 0 分计；``R_u`` 为空的行抛 ValueError（不应进评测集）。
    重复推荐抛 ValueError（计划书 7.2：禁止重复推荐）。
    """
    if len(recommendations) != len(relevants):
        raise ValueError('recommendations and relevants length mismatch')
    out = OrderedDict()
    recalls = dict((k, []) for k in k_values)
    ndcgs = dict((k, []) for k in k_values)
    ev_max = dict((k, []) for k in k_values)
    ev_ratio, returned, evidence_counts = [], [], []
    for index, (rec, rel) in enumerate(zip(recommendations, relevants)):
        rel_set = set(rel)
        if not rel_set:
            raise ValueError('empty relevant set for row %d (user not evaluable)' % index)
        rec_list = list(rec)
        if len(rec_list) != len(set(rec_list)):
            raise ValueError('duplicate recommendations for row %d' % index)
        returned.append(len(rec_list))
        for k in k_values:
            recalls[k].append(recall_at_k(rec_list, rel_set, k) or 0.0)
            ndcgs[k].append(ndcg_at_k(rec_list, rel_set, k) or 0.0)
        if evidence is not None:
            ev = set(evidence.get(index, ()))
            evidence_counts.append(len(ev))
            ratio = len(rel_set & ev) / float(len(rel_set))
            ev_ratio.append(ratio)
            for k in k_values:
                ev_max[k].append(evidence_max_recall(rel_set, ev, k))
    for k in k_values:
        out['recall@%d' % k] = recalls[k]
        out['ndcg@%d' % k] = ndcgs[k]
        if evidence is not None:
            out['evidence_max_recall@%d' % k] = ev_max[k]
    out['returned'] = returned
    out['users_scored'] = len(relevants)
    if evidence is not None:
        out['evidence_ratio'] = ev_ratio
        out['evidence_count'] = evidence_counts
    return out


# ---------------------------------------------------------------------------
# 聚合诊断（计划书 6.2）
# ---------------------------------------------------------------------------

def _percentile(values, q):
    """线性插值分位数（p10/p50/p90）；空序列返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    pos = q / 100.0 * (len(ordered) - 1)
    low = int(math.floor(pos))
    high = int(math.ceil(pos))
    if low == high:
        return float(ordered[low])
    frac = pos - low
    return float(ordered[low] * (1.0 - frac) + ordered[high] * frac)


def summarize(recommendations, relevants, users, k_values=DEFAULT_K_VALUES,
              top_n=20, evidence=None, item_universe=(), n_all=None,
              candidates=None, mostpop_recs=None, disliked=None,
              low_support_max=4):
    """聚合覆盖/证据/支持度诊断；不产生排序均值以外的推断。

    :param users: 与 recommendations 对齐的 userId 列表（共同评测用户序）。
    :param evidence: ``{序号: E_u}``，提供时输出证据覆盖率/上限/证据候选数分布。
    :param n_all: ``{movie_id: n_all}``，提供时输出推荐流行度与低支持度比例。
    :param candidates: ``{序号: C_u}``，提供时输出候选正例覆盖率。
    :param disliked: ``{序号: 验证评分 <= 2 的物品集合}``，提供时输出差评命中。
    :param mostpop_recs: ``{user_id: MostPop Top-20 列表}``，提供时输出个性化重合度。
    缺项一律 None（报告层显示 N/A），不抛错。
    """
    metrics = per_user_metrics(recommendations, relevants, k_values, evidence)
    count = len(recommendations)
    returned = metrics['returned']
    summary = OrderedDict()
    summary['users_scored'] = count
    summary['returned_count'] = sum(returned)
    summary['full_list_rate'] = (sum(1 for r in returned if r >= top_n) / float(count)
                                 if count else None)
    summary['empty_list_rate'] = (sum(1 for r in returned if r == 0) / float(count)
                                  if count else None)
    summary['fill_rate'] = (sum(returned) / float(top_n * count) if count and top_n else None)
    for k in k_values:
        summary['recall@%d' % k] = (sum(metrics['recall@%d' % k]) / count if count else None)
        summary['ndcg@%d' % k] = (sum(metrics['ndcg@%d' % k]) / count if count else None)
    if candidates is not None and count:
        ratios = [recommendable_positive_ratio(rel, candidates.get(index, ()))
                  for index, rel in enumerate(relevants)]
        summary['candidate_coverage'] = sum(r for r in ratios if r is not None) / count
    else:
        summary['candidate_coverage'] = None
    if evidence is not None:
        ev_counts = metrics['evidence_count']
        summary['evidence_positive_coverage'] = (sum(metrics['evidence_ratio']) / count
                                                 if count else None)
        for k in k_values:
            summary['evidence_max_recall@%d' % k] = (
                sum(metrics['evidence_max_recall@%d' % k]) / count if count else None)
        summary['evidence_count_p10'] = _percentile(ev_counts, 10)
        summary['evidence_count_p50'] = _percentile(ev_counts, 50)
        summary['evidence_count_p90'] = _percentile(ev_counts, 90)
        summary['evidence_count_zero_users'] = sum(1 for c in ev_counts if c == 0)
    else:
        summary['evidence_positive_coverage'] = None
        for k in k_values:
            summary['evidence_max_recall@%d' % k] = None
        summary['evidence_count_p10'] = None
        summary['evidence_count_p50'] = None
        summary['evidence_count_p90'] = None
        summary['evidence_count_zero_users'] = None
    universe = set(item_universe)
    recommended_items = set()
    for rec in recommendations:
        recommended_items.update(rec)
    summary['catalog_coverage'] = (len(recommended_items) / float(len(universe))
                                   if universe else None)
    popularities, low_support, total_positions = [], 0, 0
    if n_all is not None:
        for rec in recommendations:
            for item in rec:
                support = n_all.get(item)
                if support is None:
                    continue
                popularities.append(support)
                total_positions += 1
                if support <= low_support_max:
                    low_support += 1
    if popularities:
        summary['recommended_popularity_mean'] = sum(popularities) / float(len(popularities))
        summary['recommended_popularity_p50'] = _percentile(popularities, 50)
        summary['recommended_popularity_p90'] = _percentile(popularities, 90)
        summary['low_support_rate'] = low_support / float(total_positions)
    else:
        summary['recommended_popularity_mean'] = None
        summary['recommended_popularity_p50'] = None
        summary['recommended_popularity_p90'] = None
        summary['low_support_rate'] = None
    if disliked is not None and total_positions:
        hits = sum(len(set(rec) & set(disliked.get(index, ())))
                   for index, rec in enumerate(recommendations))
        summary['disliked_holdout_hits'] = hits
        summary['disliked_holdout_rate'] = hits / float(sum(returned) or 1)
        summary['disliked_holdout_known'] = sum(len(set(disliked.get(index, ())))
                                                for index in range(count))
    else:
        summary['disliked_holdout_hits'] = None
        summary['disliked_holdout_rate'] = None
        summary['disliked_holdout_known'] = None
    if mostpop_recs is not None and count:
        overlaps = []
        for user, rec in zip(users, recommendations):
            base = set(mostpop_recs.get(user, ()))
            mine = set(rec)
            union = base | mine
            overlaps.append(len(base & mine) / float(len(union)) if union else 0.0)
        summary['personalization_jaccard'] = sum(overlaps) / float(len(overlaps))
    else:
        summary['personalization_jaccard'] = None
    return summary


# ---------------------------------------------------------------------------
# 分层（计划书 6.3）
# ---------------------------------------------------------------------------

def history_layer_of(history_len):
    """按训练好评条数分层：0 / 1-5 / 6-20 / >20。"""
    n = int(history_len)
    if n <= 0:
        return '0'
    if n <= 5:
        return '1-5'
    if n <= 20:
        return '6-20'
    return '>20'


def candidate_bin_of(n_all_value):
    """按训练 ``n_all(i)`` 固定分箱；``None`` 或候选外用 0 语义。"""
    if n_all_value is None:
        return 'out_of_candidate'
    n = int(n_all_value)
    if n <= 0:
        return 'out_of_candidate'
    if n <= 4:
        return '1-4'
    if n <= 19:
        return '5-19'
    if n <= 99:
        return '20-99'
    return '>=100'


def stratified_layers(users, recommendations, relevants, history_len_by_user,
                      k_values=(20,), top_n=20):
    """按训练好评条数分层：逐层人数 / NDCG / Recall / 满额率。"""
    buckets = OrderedDict((layer, []) for layer in HISTORY_LAYERS)
    for index, user in enumerate(users):
        layer = history_layer_of(history_len_by_user.get(user, 0))
        buckets[layer].append(index)
    out = OrderedDict()
    for layer, indices in buckets.items():
        if not indices:
            out[layer] = {'users': 0, 'ndcg@20': None, 'recall@20': None,
                          'full_list_rate': None}
            continue
        recs = [recommendations[i] for i in indices]
        rels = [relevants[i] for i in indices]
        metrics = per_user_metrics(recs, rels, k_values)
        out[layer] = {
            'users': len(indices),
            'ndcg@20': sum(metrics['ndcg@20']) / len(indices),
            'recall@20': sum(metrics['recall@20']) / len(indices),
            'full_list_rate': (sum(1 for r in metrics['returned'] if r >= top_n)
                               / float(len(indices))),
        }
    return out


def stratified_support_bins(relevants, candidates, n_all, k_values=(20,)):
    """按训练 ``n_all(i)`` 分箱：各箱验证正例数与被 Top-20 命中比例。

    ``recommendations`` 不在此函数内——命中比例由调用方传入 ``hits_by_bin``
    后计算；这里输出各箱正例数，保证"分箱正例召回"命名不与全体 NDCG 混称。
    """
    counts = OrderedDict((b, 0) for b in CANDIDATE_BINS)
    for rel in relevants:
        for item in rel:
            if item in candidates:
                counts[candidate_bin_of(n_all.get(item))] += 1
            else:
                counts['out_of_candidate'] += 1
    return counts


# ---------------------------------------------------------------------------
# 配对 bootstrap（计划书 6.4）
# ---------------------------------------------------------------------------

def paired_bootstrap(values_a, values_b, seed=BOOTSTRAP_SEED, repeats=BOOTSTRAP_REPEATS):
    """对相同用户索引做有放回配对抽样，返回均值差与 percentile 95% 区间。

    两模型每次抽样使用同一份用户索引（计划书 6.4）。
    """
    if len(values_a) != len(values_b):
        raise ValueError('paired bootstrap requires equal-length per-user values')
    if not values_a:
        raise ValueError('paired bootstrap requires non-empty values')
    diffs = [float(a) - float(b) for a, b in zip(values_a, values_b)]
    mean_diff = sum(diffs) / float(len(diffs))
    rng = random.Random(seed)
    n = len(diffs)
    samples = []
    for _ in range(repeats):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    samples.sort()
    low = samples[int(math.floor(0.025 * (repeats - 1)))]
    high = samples[int(math.ceil(0.975 * (repeats - 1)))]
    return {'mean_difference': mean_diff,
            'ci95': [low, high],
            'seed': seed,
            'repeats': repeats,
            'users': n}


# ---------------------------------------------------------------------------
# 固定案例与参数选择（计划书 6.3 / 6.4）
# ---------------------------------------------------------------------------

def fixed_case_users(history_len_by_user):
    """跑模型前固定案例：每个非空历史层取最小 userId 的 2 人，无该层报空。"""
    picked = OrderedDict((layer, []) for layer in HISTORY_LAYERS)
    for user in sorted(history_len_by_user):
        layer = history_layer_of(history_len_by_user[user])
        if len(picked[layer]) < 2:
            picked[layer].append(user)
    return picked


def select_config(configs):
    """固定选择规则：NDCG@20 降序；|差| <= 1e-12 平手，再比 Recall@20、较小 k、较小 β。

    :param configs: ``[{'k': int, 'beta': float, 'ndcg@20': float, 'recall@20': float,
      'status': str}, ...]``；``status != 'success'`` 的配置不参与选择。
    :returns: 胜出配置 dict；无成功配置返回 None。
    """
    valid = [c for c in configs if c.get('status', 'success') == 'success']
    if not valid:
        return None

    def key(cfg):
        # 用 (负 NDCG, 负 Recall, k, beta) 排序；平手容差在比较时单独处理。
        return (-cfg['ndcg@20'], -cfg['recall@20'], cfg['k'], cfg['beta'])

    best = sorted(valid, key=key)
    winner = best[0]
    for other in best[1:]:
        ndcg_gap = abs(winner['ndcg@20'] - other['ndcg@20'])
        if ndcg_gap > SELECTION_TIE_TOLERANCE:
            break
        recall_gap = abs(winner['recall@20'] - other['recall@20'])
        if recall_gap > SELECTION_TIE_TOLERANCE:
            if other['recall@20'] > winner['recall@20']:
                winner = other
            continue
        if (other['k'], other['beta']) < (winner['k'], winner['beta']):
            winner = other
    return winner


# ---------------------------------------------------------------------------
# 证据/历史辅助（供 runner 与 verify 复用）
# ---------------------------------------------------------------------------

def evidence_from_history(history, seen):
    """错配诊断打分用的历史来源集合：返回 ``history`` 本身（去重、int 化）。

    计划书 8 节要求"按错配历史打分但仍排除**目标用户本人**的全部训练已评电影"：
    排除动作发生在打分阶段（core 的 ``seen`` 参数 = 目标用户的 ``Seen_u``），
    所以这里**不能**按 ``seen`` 裁剪错配历史——裁剪会误删 donor 的来源电影。
    """
    return {int(i) for i in history}
