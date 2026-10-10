# -*- coding: utf-8 -*-
"""**纯本地测试桩**：在 resource_guard.py 尚未交付时，让构图核心可被测试。

这不是生产代码，也不在生产 import 路径上——``full_train.py`` 对
``resource_guard`` 是**硬 import**（没有回落分支，这是 Lead 明确要求的：
没有监督就不许跑）。本模块只被测试导入，用来驱动
``build_block_edges`` / ``DiskCSRBuilder`` / ``BlockStore`` 这些纯函数。

它做的两件事：
1. 按 Lead 给的**草案契约**（``ResourceGuard`` / ``start`` / ``stop`` /
   ``set_phase`` / ``ok`` / ``failure_reason`` / ``violation`` + ``peak()``）
   提供一个记录型假对象，并断言调用序正确；
2. 直接把一段评分写成内存 CSR，绕过两遍扫描——因为扫描本身是 IO，测试要的是
   构图数学。

Python 3.6 兼容。
"""
import array
import csv
import os
import sys
import threading
import time

import numpy as np
from scipy import sparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_SEQ_LOCK = threading.Lock()
_SEQ = [0]


class FakeGuard(object):
    """记录型假监督（只为测试；生产路径永远用真的 resource_guard）。

    接口对齐 Lead 给的草案：``ResourceGuard`` 类、``start()`` / ``stop()`` /
    ``set_phase(name)``，属性 ``ok`` / ``failure_reason`` / ``violation``。
    另外实现 ``peak()``，因为 ``full_train.finalize`` 要记录采样最大值。
    """

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.phases = []
        self.started = False
        self.stopped = False
        self.ok = True
        self.failure_reason = None
        self.violation = None
        self._extra_stop_calls = 0

    def start(self):
        self.started = True
        return self

    def stop(self):
        # 幂等：重复 stop 不应报错（计划书要求"幂等停止"）。
        if self.stopped:
            self._extra_stop_calls += 1
        self.stopped = True
        return self

    def set_phase(self, name):
        self.phases.append(name)
        return self

    def peak(self):
        return {'samples': 0, 'peak_rss_mb': None, 'note': 'fake guard (test only)'}


def write_ratings_csv(path, rows, header=('userId', 'movieId', 'rating',
                                          'timestamp')):
    """写一份评分 CSV；``rows`` 是四元组序列。"""
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(row)
    return path


def rows_to_csr(rows, positive_threshold=4.0):
    """把四元组列表直接转成内存 0/1 好评 CSR（跳过两遍磁盘扫描）。

    :returns: ``(X, item_ids, user_ids, n_all, n_like)``
        - ``item_ids`` / ``user_ids``：升序；
        - ``n_all`` / ``n_like``：``int64``，对齐 ``item_ids``；
        - ``X``：``csr_matrix``，``int64``，**列顺序与 item_ids 一致**。
    """
    user_ids = sorted(set(row[0] for row in rows))
    item_ids = sorted(set(row[1] for row in rows))
    user_index = dict((u, p) for p, u in enumerate(user_ids))
    item_index = dict((i, p) for p, i in enumerate(item_ids))
    n_all = np.zeros(len(item_ids), dtype=np.int64)
    n_like = np.zeros(len(item_ids), dtype=np.int64)
    r_list, c_list = [], []
    for user, item, rating, _ts in rows:
        pos = item_index[item]
        n_all[pos] += 1
        if float(rating) >= positive_threshold:
            n_like[pos] += 1
            r_list.append(user_index[user])
            c_list.append(pos)
    if not r_list:
        return (sparse.csr_matrix((len(user_ids), len(item_ids)), dtype=np.int64),
                item_ids, user_ids, n_all, n_like)
    data = np.ones(len(r_list), dtype=np.int64)
    X = sparse.csr_matrix((data, (np.asarray(r_list, dtype=np.int64),
                                  np.asarray(c_list, dtype=np.int64))),
                          shape=(len(user_ids), len(item_ids)), dtype=np.int64)
    return X, item_ids, user_ids, n_all, n_like


def graph_to_dict(arrays, item_ids):
    """把边数组展开成 ``{source_movieId: [(target_movieId, sim, c), ...]}``。

    用于断言"向量化结果 == 逐条参考实现"。
    """
    indptr = arrays['indptr']
    targets = arrays['targets']
    sims = arrays['similarities']
    coocs = arrays['cooccurrences']
    graph = {}
    for pos in range(len(item_ids)):
        start = int(indptr[pos])
        stop = int(indptr[pos + 1])
        if stop <= start:
            continue
        graph[int(item_ids[pos])] = [
            (int(item_ids[targets[k]]), float(sims[k]), int(coocs[k]))
            for k in range(start, stop)
        ]
    return graph


def reference_graph_dict(X, item_ids, beta, max_k, block_size=128):
    """逐条 Python 参考图（直接调 full_train 里的测试用实现）。"""
    from experiments.itemcf import full_train as F
    return F.reference_neighbors_python(X, item_ids, beta, max_k,
                                        block_size=block_size)


def make_temp_dir(root, prefix):
    """建唯一临时目录（沿用既有 itemcf 测试的重试策略）。"""
    with _SEQ_LOCK:
        _SEQ[0] += 1
        seq = _SEQ[0]
    for attempt in range(1000):
        path = os.path.join(root, '%s-%d-%d'
                            % (prefix, int(time.time() * 1000), seq + attempt))
        try:
            os.makedirs(path)
            return path
        except OSError:
            continue
    raise RuntimeError('could not create a unique temp dir under %s' % root)
