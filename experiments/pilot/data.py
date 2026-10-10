"""时序切分：每用户按 timestamp 升序，留最后 20%（最少 1 条）为测试集。

只做 train/test 两段：试点超参固定为文献值，不调参，故不切验证集。
单评分用户无法评测，整条留训练集。
同 timestamp 时按 movieId 决胜，保证与输入行序无关、切分可复现。
"""
from collections import defaultdict


def temporal_split(rows, test_frac=0.2):
    by_user = defaultdict(list)
    for idx, row in enumerate(rows):
        by_user[row[0]].append((idx, row))
    train_idx, test_idx = [], []
    for user in sorted(by_user):
        entries = sorted(by_user[user], key=lambda t: (t[1][3], t[1][1], t[0]))  # 防御性兜底：(ts, movieId) 在真实数据已唯一
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


def temporal_split_three_way(rows, test_frac=0.2, val_frac=0.2):
    """Reserve the old test segment, then split validation from its remainder."""
    by_user = defaultdict(list)
    for idx, row in enumerate(rows):
        by_user[row[0]].append((idx, row))
    train_idx, val_idx, test_idx = [], [], []
    for entries in by_user.values():
        entries.sort(key=lambda t: (t[1][3], t[1][1], t[0]))
        n = len(entries)
        nt = min(max(1, int(n * test_frac)), n - 1) if n >= 2 else 0
        rest = n - nt
        nv = min(max(1, int(rest * val_frac)), rest - 1) if rest >= 2 else 0
        train_idx.extend(idx for idx, _ in entries[:rest - nv])
        val_idx.extend(idx for idx, _ in entries[rest - nv:rest])
        test_idx.extend(idx for idx, _ in entries[rest:])
    return tuple([rows[idx] for idx in sorted(part)]
                 for part in (train_idx, val_idx, test_idx))
