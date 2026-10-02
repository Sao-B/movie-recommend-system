"""排序与评分指标（二值相关性：相关 = 测试条目 rating >= 4.0）。"""
import math


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
    return math.sqrt(sum((p - a) ** 2 for p, a in pairs) / float(len(pairs)))
