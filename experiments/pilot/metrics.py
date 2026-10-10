"""排序与评分指标（二值相关性：相关 = 测试条目 rating >= 4.0）。

契约：
- ``recall_at_k`` 的分母是相关集合大小 ``|relevant|``（全量去重），
  不是 ``len(recommended[:k])``，也不是 top-k 内的相关数。
- ``ndcg_at_k`` 二值相关，理想 DCG 按 ``min(|relevant|, k)`` 项计；
  ``recommended`` 假定无重复项（重复项会被逐个计入 DCG 而使 NDCG 超过 1.0）。
- 空相关集合 / 空 pairs 返回 ``None``（该用户不参与平均）。
- ``rmse`` 接受 ``(pred, actual)`` 对的序列，例如 ``[(1.0, 3.0), ...]``
  （内层用 tuple 或 2 元素 list 均可）；dict 或元素个数不为 2 一律 ``TypeError``。
  **已知歧义**：``rmse(([1.0, 3.0], [2.0, 4.0]))`` —— 把两个平行列表打包成
  一个 2 元组 —— 与合法的 ``[(1.0, 3.0), (2.0, 4.0)]`` 在结构上完全一致
  （都解释为"2 条 pair"），无法在不破坏 list pair 契约的前提下拒绝它。
  调用方须直接传 pair 序列，不要传两个平行列表。
"""
import math


def recommendable_positive_ratio(relevant, candidates):
    rel = set(relevant)
    return len(rel & set(candidates)) / float(len(rel)) if rel else None


def prediction_bounds(predictions):
    values = [float(p) for p in predictions]
    if any(not math.isfinite(p) for p in values):
        raise ValueError('non-finite prediction')
    count = len(values)
    outside = sum(p < 0.5 or p > 5.0 for p in values)
    return dict(count=count, out_of_range_count=outside,
                rate=outside / float(count) if count else None,
                minimum=min(values) if count else None,
                maximum=max(values) if count else None)


def recall_at_k(recommended, relevant, k):
    if not relevant:
        return None
    hits = len(set(recommended[:k]) & set(relevant))
    return hits / float(len(set(relevant)))


def ndcg_at_k(recommended, relevant, k):
    rel = set(relevant)
    if not rel:
        return None
    dcg = sum(1.0 / math.log2(pos + 2)
              for pos, item in enumerate(recommended[:k]) if item in rel)
    idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(len(rel), k)))
    return dcg / idcg if idcg > 0 else 0.0


def rmse(pairs):
    if not pairs:
        return None
    total = 0.0
    for index, pair in enumerate(pairs):
        # 形状校验：必须是长度为 2 的 (pred, actual)。少了这一步，dict 输入
        # 会报出 'str' - 'str' 这种与调用方意图无关的底层错误，3 元组则抛
        # ValueError（与应用层"形状不符 = TypeError"的惯例不一致）。
        try:
            pred, actual = pair
        except (TypeError, ValueError):
            raise TypeError(
                'rmse 的 pairs[%d] 必须是 (pred, actual) 二元组，实际为 %r'
                % (index, pair))
        try:
            diff = pred - actual
        except TypeError:
            raise TypeError(
                'rmse 的 pairs[%d] 两项必须是数值，实际为 %r' % (index, pair))
        total += diff * diff
    return math.sqrt(total / float(len(pairs)))
