# -*- coding: utf-8 -*-
"""双引擎逐边对拍：比较两个 ItemCF 模型目录的 CSR 数组。

背景
----
Spark 重写版（`spark/train_itemcf.py`）与 scipy 单机版（`experiments/itemcf/full_train.py`）
声称"同公式同参数"（k=100、beta=0、阈值 4.0）。本脚本**不重算**任何东西，只把两个
已落盘的模型目录**逐数组比对**，判定它们是否真的是同一个东西。

为什么不用 `equiv_check.py`
-------------------------
`equiv_check.py` 的机制是"从原始评分独立重算一遍，再跟一个 holdout 基线目录的冻结
哈希 + per-user 推荐逐用户比"（argparse 只有 ``--input/--baseline-dir/--out``，**没有
``--model-dir``**）。它验的是"我的重算能否复现基线"，**不能比较两个模型目录**。
逐边对拍要的就是后者，所以单独写。

比较策略
--------
两个模型目录都是 `model_io.NeighborModel` 的 CSR 形态（7 个 npy + model.json），
契约见 `model_io.py` 的模块 docstring：数组用 0-based 下标，``item_ids[pos]`` 才是
movieId；来源 pos 的邻居切片是 ``[indptr[pos], indptr[pos+1])``。

1. **整数数组（item_ids / indptr / target_indices / cooccurrences / n_like / n_all）
   走 ``np.array_equal``** —— 要求**完全相等**，不容差。索引错位就是错位，
   容差会掩盖 bug。
2. **相似度（similarities）走容差 1e-12 的相对误差** —— 它是 ``c / sqrt(n_i*n_j)``，
   理论上同公式同序求值应逐位相等（``tests/test_spark_itemcf_core.py`` 已在
   beta x k 六组合下锁定了这一点）。但既然允许容差，就按**相对**误差判，
   绝对容差在小数值上会误判。
3. 整数数组不相等时**先定位到第一个差异位置**再报，避免只丢一句 "not equal"。

内存纪律
--------
两侧都走 ``model_io.load_arrays(..., mmap_mode='r')`` —— 只映射不读进内存。
但"逐数组相等"需要看到每个元素，所以对整数数组是**分块**比（默认 200 万边/块），
峰值与总边数脱钩。542 万边全量一次性读进两个 int64 数组是 43MB x 2，其实不大，
但保持分块是为了不给"以后边数翻十倍"留坑。
"""
import argparse
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.itemcf import model_io  # noqa: E402

#: 整数数组：必须完全相等，不容差。
INT_ARRAYS = ('item_ids', 'indptr', 'target_indices', 'cooccurrences',
              'n_like', 'n_all')
#: 浮点数组：走相对容差。
FLOAT_ARRAYS = ('similarities',)

DEFAULT_CHUNK_EDGES = 2_000_000
DEFAULT_REL_TOL = 1e-12


def _first_int_mismatch(name, a, b, limit=5):
    """返回 ``name`` 数组第一个不相等的位置摘要（不做全量 diff 打印）。"""
    n = min(len(a), len(b))
    if len(a) != len(b):
        return {'array': name, 'reason': 'length differs',
                'len_left': int(len(a)), 'len_right': int(len(b))}
    if n == 0:
        return None
    step = 1 << 20
    for start in range(0, n, step):
        stop = min(start + step, n)
        la = np.asarray(a[start:stop])
        lb = np.asarray(b[start:stop])
        hit = np.nonzero(la != lb)[0]
        if hit.size:
            idx = int(hit[0]) + start
            return {'array': name, 'reason': 'value mismatch',
                    'first_diff_index': idx,
                    'left': int(la[hit[0]]), 'right': int(lb[hit[0]]),
                    'mismatch_count_in_chunk': int(hit.size),
                    'chunk_start': int(start)}
    return None


def compare_int_array(name, a, b):
    """分块逐元素比对整数数组；相等返回 ``None``，否则返回差异摘要。"""
    if len(a) != len(b):
        return {'array': name, 'reason': 'length differs',
                'len_left': int(len(a)), 'len_right': int(len(b))}
    chunk = DEFAULT_CHUNK_EDGES
    n = len(a)
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        if not np.array_equal(np.asarray(a[start:stop]),
                              np.asarray(b[start:stop])):
            return _first_int_mismatch(name, a, b)
    return None


def compare_float_array(name, a, b, rel_tol=DEFAULT_REL_TOL):
    """相对误差比对浮点数组；相等返回 ``None``。

    相对误差的定义与 `model_io._check_edge_chunk` 一致（``diff / max(|implied|, 1e-300)``），
    避免小数值上绝对容差放大成"看起来差很多其实没错"。
    """
    if len(a) != len(b):
        return {'array': name, 'reason': 'length differs',
                'len_left': int(len(a)), 'len_right': int(len(b))}
    n = len(a)
    worst = 0.0
    worst_idx = None
    chunk = DEFAULT_CHUNK_EDGES
    for start in range(0, n, chunk):
        stop = min(start + chunk, n)
        la = np.asarray(a[start:stop], dtype=np.float64)
        lb = np.asarray(b[start:stop], dtype=np.float64)
        diff = np.abs(la - lb)
        scale = np.maximum(np.maximum(np.abs(la), np.abs(lb)), 1e-300)
        rel = diff / scale
        if rel.size:
            local = float(rel.max())
            if local > worst:
                worst = local
                worst_idx = int(np.argmax(rel)) + start
    if worst > rel_tol:
        return {'array': name, 'reason': 'relative tolerance exceeded',
                'max_relative_gap': worst, 'tolerance': rel_tol,
                'worst_index': worst_idx,
                'left': float(a[worst_idx]), 'right': float(b[worst_idx])}
    return None


def run(left_dir, right_dir, rel_tol=DEFAULT_REL_TOL):
    """执行对拍；返回报告 dict。"""
    report = {
        'left_dir': os.path.abspath(left_dir),
        'right_dir': os.path.abspath(right_dir),
        'relative_tolerance': rel_tol,
        'arrays': {},
        'mismatches': [],
    }
    for path, key in ((left_dir, 'left'), (right_dir, 'right')):
        if not os.path.isdir(path):
            report['pass'] = False
            report['error'] = 'missing model dir (%s): %s' % (key, path)
            return report

    left = model_io.load_arrays(left_dir, mmap_mode='r')
    right = model_io.load_arrays(right_dir, mmap_mode='r')

    for name in INT_ARRAYS:
        a = left[name]
        b = right[name]
        report['arrays'][name] = {
            'kind': 'int', 'dtype': str(a.dtype),
            'len_left': int(len(a)), 'len_right': int(len(b)),
        }
        diff = compare_int_array(name, a, b)
        if diff:
            report['mismatches'].append(diff)

    for name in FLOAT_ARRAYS:
        a = left[name]
        b = right[name]
        report['arrays'][name] = {
            'kind': 'float', 'dtype': str(a.dtype),
            'len_left': int(len(a)), 'len_right': int(len(b)),
        }
        diff = compare_float_array(name, a, b, rel_tol=rel_tol)
        if diff:
            report['mismatches'].append(diff)

    # model.json 里值得比的元数据（**不含** created_at / status / run_id，
    # 那些按设计就该不同）
    meta_fields = ('schema_version', 'parameters', 'engine', 'counts')
    meta_cmp = {}
    for side, d in (('left', left_dir), ('right', right_dir)):
        mp = os.path.join(d, model_io.METADATA_FILE)
        if not os.path.isfile(mp):
            meta_cmp[side] = None
            continue
        with open(mp, 'r', encoding='utf-8') as handle:
            meta = json.load(handle)
        meta_cmp[side] = dict((f, meta.get(f)) for f in meta_fields)
    report['metadata'] = meta_cmp

    report['n_edges'] = int(len(left['target_indices']))
    report['n_items'] = int(len(left['item_ids']))
    report['pass'] = not report['mismatches']
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m experiments.itemcf.compare_models',
        description='Compare two ItemCF model directories edge by edge '
                    '(Spark engine vs scipy engine).')
    parser.add_argument('--left', required=True, help='左侧模型目录（通常 Spark 产物）')
    parser.add_argument('--right', required=True, help='右侧模型目录（通常 scipy 参照）')
    parser.add_argument('--rel-tol', type=float, default=DEFAULT_REL_TOL,
                        help='相似度相对容差（默认 1e-12）')
    parser.add_argument('--out', default=None, help='报告 JSON 落点')
    args = parser.parse_args(argv)

    report = run(args.left, args.right, rel_tol=args.rel_tol)
    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory and not os.path.isdir(directory):
            os.makedirs(directory)
        with open(args.out, 'w', encoding='utf-8') as handle:
            json.dump(report, handle, indent=2, sort_keys=True, ensure_ascii=False)
    print(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False))
    return 0 if report.get('pass') else 1


if __name__ == '__main__':
    sys.exit(main())
