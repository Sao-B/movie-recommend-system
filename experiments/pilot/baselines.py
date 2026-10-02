"""非个性化基线：MostPop 与 Random。"""
import random
from collections import Counter


def most_popular(train, k):
    """按训练集出现次数取 Top-k，平票按 item id 升序决胜。

    train 的行格式契约：(user, item, rating, ts)——这里只读 row[1] 作为 item，
    其余列不参与统计。
    """
    counts = Counter(row[1] for row in train)
    return [item for item, _ in sorted(counts.items(), key=lambda t: (-t[1], t[0]))[:k]]


def random_top(items, k, seed=42):
    """从 items 中按 seed 随机取 k 个。

    items 可为任意可迭代对象（内部先 sorted 归一），因此结果与输入顺序无关。
    """
    pool = sorted(items)
    random.Random(seed).shuffle(pool)
    return pool[:k]
