"""从 ratings.csv 生成试点切片：固定种子抽 N 个用户的全部评分。

两遍扫描：第一遍统计每用户评分数，第二遍过滤输出。
在 VM 上对 /home/h/movielens/ratings.csv 运行（Python 3.6 兼容）。
"""
import argparse
import csv
import random
from collections import Counter


def count_users(path):
    counts = Counter()
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            counts[row['userId']] += 1
    return counts


def sample_users(counts, n_users, seed):
    rng = random.Random(seed)
    users = sorted(counts, key=lambda u: int(u))
    if n_users > len(users):
        raise ValueError('n_users=%d 超过用户总数 %d' % (n_users, len(users)))
    return set(rng.sample(users, n_users))


def filter_rows(src, dst, keep):
    written = 0
    with open(src, newline='', encoding='utf-8') as fin, \
             open(dst, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row['userId'] in keep:
                writer.writerow(row)
                written += 1
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--users', type=int, default=2000)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()
    counts = count_users(args.src)
    keep = sample_users(counts, args.users, args.seed)
    written = filter_rows(args.src, args.out, keep)
    print('users=%d rows=%d -> %s' % (len(keep), written, args.out))


if __name__ == '__main__':
    main()
