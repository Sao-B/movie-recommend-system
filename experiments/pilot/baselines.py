"""非个性化基线：MostPop 与 Random。"""
import random
from collections import Counter


def most_popular(train, k):
    counts = Counter(row[1] for row in train)
    return [item for item, _ in sorted(counts.items(), key=lambda t: (-t[1], t[0]))[:k]]


def random_top(items, k, seed=42):
    pool = sorted(items)
    random.Random(seed).shuffle(pool)
    return pool[:k]
