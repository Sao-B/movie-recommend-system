"""试点 3 张图：长尾分布 / 预测分分布 / 稀疏用户案例 Top-20 支持度。"""
import argparse
import csv
import os
from collections import Counter

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def read_csv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def fig_longtail(slice_path, out):
    counts = Counter()
    with open(slice_path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            counts[row['movieId']] += 1
    xs = sorted(counts.values(), reverse=True)
    plt.figure(figsize=(6, 4))
    plt.loglog(range(1, len(xs) + 1), xs)
    plt.xlabel('movie rank')
    plt.ylabel('ratings count')
    plt.title('Pilot slice long tail (log-log)')
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close()


def fig_pred_dist(run_dir, out):
    preds = [float(r['pred']) for r in read_csv(os.path.join(run_dir, 'pred_dist.csv'))]
    oor = sum(1 for p in preds if p < 1.0 or p > 5.0) / float(len(preds))
    plt.figure(figsize=(6, 4))
    plt.hist(preds, bins=50)
    plt.axvline(1.0, color='r', linestyle='--')
    plt.axvline(5.0, color='r', linestyle='--')
    plt.xlabel('predicted rating')
    plt.ylabel('count')
    plt.title('ALS test predictions (out-of-range = %.4f%%)' % (oor * 100))
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close()


def fig_case(run_dir, out):
    rows = read_csv(os.path.join(run_dir, 'case_users.csv'))
    users = sorted(set(r['user'] for r in rows))
    fig, axes = plt.subplots(len(users), 2, figsize=(10, 2.6 * len(users)))
    for ax_row, u in zip(axes, users):
        for ax, name in zip(ax_row, ('ALS', 'MostPop')):
            sel = [r for r in rows if r['user'] == u and r['model'] == name]
            sel.sort(key=lambda r: int(r['rank']))
            ax.bar(range(len(sel)), [int(r['train_support']) for r in sel])
            ax.set_title('user %s - %s top-%d' % (u, name, len(sel)))
            ax.set_xlabel('rank')
            ax.set_ylabel('train support')
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='run_dir', required=True)
    ap.add_argument('--slice', dest='slice_path', required=True)
    ap.add_argument('--outdir', required=True)
    args = ap.parse_args()
    fig_longtail(args.slice_path, os.path.join(args.outdir, 'fig1_longtail.png'))
    fig_pred_dist(args.run_dir, os.path.join(args.outdir, 'fig2_pred_dist.png'))
    fig_case(args.run_dir, os.path.join(args.outdir, 'fig3_case_top20.png'))
    print('WROTE 3 PNG to %s' % args.outdir)


if __name__ == '__main__':
    main()
