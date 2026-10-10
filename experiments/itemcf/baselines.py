# -*- coding: utf-8 -*-
"""ItemCF 实验的对照基线纯函数（计划书 4.1 / 4.3 节）。

- MostPop：直接复用 :func:`experiments.pilot.baselines.most_popular`（本模块只加
  全量排序便捷包装 :func:`most_popular_order`，不重写排序逻辑）。
- LikePop：按 ``n_like(i)``（训练段好评人数，rating >= 4.0）降序、平票 movieId
  升序；不足正好评物品时按相同规则继续零计数物品（计划书 4.1）。
- BiasOnly：仅 ``mu + b_u + b_i``，lambda=0.1、10 轮、按观测数正则（计划书 4.3），
  偏置闭式解复用 :func:`experiments.pilot.als.solve_bias`（不改旧文件）。

排序分数语义：本模块返回的 ``score`` 都是**排序分数**，不是 1–5 的预测评分；
BiasOnly 的 ``mu + b_u + b_i`` 是唯一带评分量纲的基线，仅供诊断。

Python 3.6 兼容；无 SciPy 依赖。
"""
import math

from experiments.pilot.als import solve_bias
from experiments.pilot.baselines import most_popular

#: 好评二值化阈值（与计划书 3.1 / 2.3 一致）。
POSITIVE_THRESHOLD = 4.0

#: BiasOnly 固定超参（计划书 4.3）。
BIAS_LAMBDA = 0.1
BIAS_ROUNDS = 10


def most_popular_order(train, limit=None):
    """全量 MostPop 排序：``n_all(i)`` 降序、平票 movieId 升序。

    直接复用 ``experiments.pilot.baselines.most_popular``（k = 物品数即全量排序），
    本函数只加"取前 limit 条"的截断，不重写排序逻辑。
    ``limit`` 为截断条数，None 表示全量。
    """
    total = len({row[1] for row in train})
    if total == 0:
        return []
    order = most_popular(train, total)
    return order if limit is None else order[:limit]


def n_like_counts(train, positive_threshold=POSITIVE_THRESHOLD):
    """``n_like(i) = sum_u X_ui``：训练段对 i 好评（rating >= 阈值）的用户数。"""
    counts = {}
    for row in train:
        item = row[1]
        if float(row[2]) >= positive_threshold:
            counts[item] = counts.get(item, 0) + 1
    return counts


def n_all_counts(train):
    """``n_all(i)``：训练段对 i 评过分的用户数（与 pilot ``most_popular`` 同口径）。"""
    counts = {}
    for row in train:
        item = row[1]
        counts[item] = counts.get(item, 0) + 1
    return counts


def like_popular_order(train, limit=None, positive_threshold=POSITIVE_THRESHOLD):
    """LikePop 全量排序：``n_like`` 降序、平票 movieId 升序、零计数物品补齐。

    不足正好评物品时按相同规则继续排零计数物品（movieId 升序）。
    ``limit`` 为截断条数，None 表示全量。
    """
    counts = n_like_counts(train, positive_threshold)
    order = [item for item, _ in sorted(counts.items(), key=lambda t: (-t[1], t[0]))]
    zero_items = sorted({row[1] for row in train if row[1] not in counts})
    order.extend(zero_items)
    return order if limit is None else order[:limit]


def like_popular(train, users, top_n, item_ids=None, positive_threshold=POSITIVE_THRESHOLD):
    """逐用户 LikePop Top-N（排除该用户 ``users[u]`` 的已看物品）。

    :param users: ``{user_id: seen_item_set}``。
    :returns: ``{user_id: [(movie_id, n_like_count), ...]}``，按 (-n_like, movieId)。
    """
    order = like_popular_order(train, None, positive_threshold)
    counts = n_like_counts(train, positive_threshold)
    out = {}
    for user in users:
        seen = set(users[user])
        picked = []
        for item in order:
            if item in seen:
                continue
            picked.append((item, counts.get(item, 0)))
            if len(picked) >= top_n:
                break
        out[user] = picked
    return out


# ---------------------------------------------------------------------------
# BiasOnly（计划书 4.3）
# ---------------------------------------------------------------------------

def fit_bias_only(train, lam=BIAS_LAMBDA, rounds=BIAS_ROUNDS, return_objectives=False):
    """拟合 mu + b_u + b_i（固定 mu = 训练均分，初始偏置为 0）。

    每轮先更新全部用户偏置、再更新全部物品偏置（计划书 4.3 的固定顺序）：
    ``b_u = sum_{i in I_u}(r_ui - mu - b_i) / (n_u (1 + lam))``，``b_i`` 同理。
    正则口径与 :func:`experiments.pilot.als.solve_bias` 一致（按观测数）。

    :returns: ``{'mu', 'bu': {u: b_u}, 'bi': {i: b_i}, 'n_u', 'n_i'}``；
      ``return_objectives=True`` 时再返回逐轮目标值列表
      ``SSE + lam * [sum_u n_u b_u^2 + sum_i n_i b_i^2]``（含第 0 轮初值，
      共 ``rounds + 1`` 项；**测试锁定的循环次数是 rounds=1 时 bu/bi 的闭式值**，
      调用方按需取前缀）。

    训练行重复 ``(userId, movieId)`` 时抛 ValueError（与 ``als.fit_spark`` 同契约）。
    """
    if rounds < 0:
        raise ValueError('rounds must be >= 0, got %r' % (rounds,))
    n_u, n_i, sums = {}, {}, {}
    seen_pairs = set()
    total = 0.0
    for row in train:
        user, item, rating, _ts = row
        key = (user, item)
        if key in seen_pairs:
            raise ValueError('duplicate (userId, movieId) pair: %r' % (key,))
        seen_pairs.add(key)
        rating = float(rating)
        n_u[user] = n_u.get(user, 0) + 1
        n_i[item] = n_i.get(item, 0) + 1
        sums[key] = rating
        total += rating
    observations = len(seen_pairs)
    mu = total / observations if observations else 0.0
    bu = dict((u, 0.0) for u in n_u)
    bi = dict((i, 0.0) for i in n_i)

    def objective_value():
        sse = 0.0
        for (user, item), rating in sums.items():
            error = mu + bu[user] + bi[item] - rating
            sse += error * error
        penalty = sum(n_u[u] * bu[u] * bu[u] for u in bu)
        penalty += sum(n_i[i] * bi[i] * bi[i] for i in bi)
        return float(sse + lam * penalty)

    objectives = [objective_value()]
    by_user, by_item = {}, {}
    for (user, item), rating in sums.items():
        by_user.setdefault(user, []).append((item, rating))
        by_item.setdefault(item, []).append((user, rating))

    for _ in range(rounds):
        # 先全部用户偏置（用上一轮物品偏置），再全部物品偏置（用新用户偏置）。
        for user, pairs in by_user.items():
            residual = [rating - mu - bi[item] for item, rating in pairs]
            bu[user] = solve_bias(residual, lam, n_u[user])
        for item, pairs in by_item.items():
            residual = [rating - mu - bu[user] for user, rating in pairs]
            bi[item] = solve_bias(residual, lam, n_i[item])
        objectives.append(objective_value())

    model = {'mu': mu, 'bu': bu, 'bi': bi, 'n_u': n_u, 'n_i': n_i}
    if return_objectives:
        return model, objectives
    return model


def bias_only_ranking(model, user, seen, item_ids, top_n):
    """BiasOnly 排序：``mu + b_u + b_i`` 降序、平票 movieId 升序，排除 ``seen``。

    对固定用户 ``mu + b_u`` 是常数，不改变电影间顺序，故排序等价于按 ``b_i``
    排序（计划书 4.3；该等价性有测试锁定）。``user``/``item`` 不在训练中时
    偏置按 0 计（冷启动降级）。
    """
    offset = model['mu'] + model['bu'].get(user, 0.0)
    scored = []
    for item in item_ids:
        if item in seen:
            continue
        scored.append((item, offset + model['bi'].get(item, 0.0)))
    scored.sort(key=lambda t: (-t[1], t[0]))
    return scored[:top_n]


def bias_only_evidence(model):
    """BiasOnly 的目标/偏置摘要，供 summary.json 的 objective_diagnostics 使用。"""
    return {'mu': model['mu'],
            'bu_abs_max': max([abs(v) for v in model['bu'].values()] or [0.0]),
            'bi_abs_max': max([abs(v) for v in model['bi'].values()] or [0.0])}
