# -*- coding: utf-8 -*-
"""构建单个 β 的 ItemCF 邻居图并落盘（计划书 5.1 / 10.2）。

一个子进程只做**一件事**：读训练段（自己切分，不共享内存图）、算
``max_k = 100`` 的收缩余弦邻居图、写成 ``.npz`` + 映射 JSON + 摘要 JSON。
k 的截断（20/50/100）在评分阶段从这张 max_k 图上取前缀，所以三张图对应三个 β。

产物（计划书 11：``neighbors .npz + 映射JSON``）：

- ``<name>.npz``：``sources`` / ``targets`` / ``similarities`` / ``cooccurrences``
  四个并行数组（按 (source, rank) 顺序）；
- ``<name>.mapping.json``：```item_ids`` 与边到 movieId 的映射、协议指纹、形状；
- ``<name>.summary.json``：行数/边数/协议/耗时，供驱动脚本核对。

Python 3.6 兼容；只依赖 NumPy / SciPy。
"""
import argparse
import hashlib
import json
import os
import sys
import time

import numpy as np

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import run_itemcf as R  # noqa: E402
from experiments.itemcf.core import (  # noqa: E402
    POSITIVE_THRESHOLD, build_neighbors, build_positive_matrix,
)
from experiments.itemcf.evaluation import build_eval_context  # noqa: E402
from experiments.pilot.data import temporal_split_three_way  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description='Build one beta neighbor graph')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--beta', type=float, required=True)
    parser.add_argument('--block-size', type=int, default=128)
    parser.add_argument('--max-k', type=int, default=100)
    parser.add_argument('--test-frac', type=float, default=0.2)
    parser.add_argument('--val-frac', type=float, default=0.2)
    args = parser.parse_args(argv)

    started = time.time()
    name = 'beta%g' % float(args.beta)
    rows = R.load_rows_csv(args.input)
    R.validate_rows(rows)
    input_md5 = hashlib.md5(open(args.input, 'rb').read()).hexdigest()
    train, val, test = temporal_split_three_way(rows, args.test_frac, args.val_frac)
    ctx = build_eval_context(train, val)

    X, item_ids, user_ids = build_positive_matrix(train)
    neighbors = build_neighbors(X, item_ids, args.beta, args.max_k,
                                block_size=args.block_size)

    sources, targets, sims, coocs = [], [], [], []
    for source_id in sorted(set(key[0] for key in neighbors)):
        edges = [neighbors[(source_id, other)] for other in sorted(
            key[1] for key in neighbors if key[0] == source_id)]
        for target_id, sim, c_ij, _n_j, _n_i in edges:
            sources.append(int(source_id))
            targets.append(int(target_id))
            sims.append(float(sim))
            coocs.append(int(c_ij))

    graph_path = os.path.join(args.out, '%s.npz' % name)
    np.savez_compressed(
        graph_path,
        sources=np.asarray(sources, dtype=np.int64),
        targets=np.asarray(targets, dtype=np.int64),
        similarities=np.asarray(sims, dtype=np.float64),
        cooccurrences=np.asarray(coocs, dtype=np.int64),
        item_ids=np.asarray(item_ids, dtype=np.int64))

    mapping = {
        'name': name, 'beta': float(args.beta), 'max_k': int(args.max_k),
        'input': os.path.abspath(args.input), 'input_md5': input_md5,
        'rows': len(rows), 'train_rows': len(train), 'val_rows': len(val),
        'test_rows': len(test), 'users': len({r[0] for r in rows}),
        'candidate_items': len(item_ids), 'candidate_rule':
            'train items minus own train seen; all distinct evaluation positives retained',
        'relevant_threshold': POSITIVE_THRESHOLD,
        'evaluation_segment': 'validation',
        'users_scored': len(ctx.eval_users),
        'user_ids': [int(u) for u in user_ids],
        'item_ids': [int(i) for i in item_ids],
        'n_edges': len(sources),
        'split_order': 'timestamp,movieId,original_row_index',
        'top_n': R.GRID_DEFAULTS['top_n'],
    }
    with open(os.path.join(args.out, '%s.mapping.json' % name), 'w',
              encoding='utf-8') as handle:
        json.dump(mapping, handle, indent=2, sort_keys=True)

    summary = {'status': 'success', 'name': name, 'max_k': int(args.max_k),
               'beta': float(args.beta), 'n_edges': len(sources),
               'n_sources': len(set(sources)), 'candidate_items': len(item_ids),
               'input_md5': input_md5, 'train_rows': len(train),
               'val_rows': len(val), 'test_rows': len(test),
               'users_scored': len(ctx.eval_users),
               'seconds': time.time() - started}
    with open(os.path.join(args.out, '%s.summary.json' % name), 'w',
              encoding='utf-8') as handle:
        json.dump(summary, handle, indent=2, sort_keys=True)
    print(json.dumps(summary, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
