"""时序切分：每用户按 timestamp 升序，留最后 20%（最少 1 条）为测试集。

只做 train/test 两段：试点超参固定为文献值，不调参，故不切验证集。
单评分用户无法评测，整条留训练集。
"""
from collections import defaultdict


def temporal_split(rows, test_frac=0.2):
    by_user = defaultdict(list)
    for idx, row in enumerate(rows):
        by_user[row[0]].append((idx, row))
    train_idx, test_idx = [], []
    for user in sorted(by_user):
        entries = sorted(by_user[user], key=lambda t: (t[1][3], t[0]))
        n = len(entries)
        if n < 2:
            train_idx.extend(i for i, _ in entries)
            continue
        n_test = max(1, int(n * test_frac))
        if n - n_test < 1:
            n_test = n - 1
        train_idx.extend(i for i, _ in entries[:n - n_test])
        test_idx.extend(i for i, _ in entries[n - n_test:])
    train = [rows[i] for i in sorted(train_idx)]
    test = [rows[i] for i in sorted(test_idx)]
    return train, test
