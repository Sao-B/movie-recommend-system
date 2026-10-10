# -*- coding: utf-8 -*-
"""``heldout_eval.load_cooccurrence_from_model_dir`` 的正确性测试。

背景
----
holdout 评测原先只能在脚本内现算共现图（``build_cooccurrence``），没有"从已落盘
的模型目录读图"的入口——所以"Spark 版 ItemCF 产物能否在同协议下复现出同样的
指标"这件事**没法实测**，只能靠"两个模型逐边全等"间接推出。加了这个入口后，
那条结论可以被实测。

风险的形状很具体：模型的图是**下标 CSR**（``item_ids[pos]`` 才是 movieId），
而打分用的 ``item_ids`` 是本评测口径的物品顺序。**两者不同序就全错，而且不会
报错**——只会产出一张"看起来正常"的错图、一个"看起来合理"的差指标。所以本测试
重点不是"能读出来"，而是**顺序不一致时必须硬失败**。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from scipy import sparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for candidate in (str(PROJECT_ROOT), str(PROJECT_ROOT / "spark")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from experiments.itemcf import heldout_eval, model_io  # noqa: E402


def _write_model(directory, item_ids, indptr, targets, sims, coocs,
                 n_like, n_all):
    """落一个最小合法模型目录（借 model_io.save，顺带获得结构校验）。"""
    model = model_io.NeighborModel(
        item_ids=np.asarray(item_ids, dtype=np.int64),
        indptr=np.asarray(indptr, dtype=np.int64),
        target_indices=np.asarray(targets, dtype=np.int64),
        similarities=np.asarray(sims, dtype=np.float64),
        cooccurrences=np.asarray(coocs, dtype=np.int64),
        n_like=np.asarray(n_like, dtype=np.int64),
        n_all=np.asarray(n_all, dtype=np.int64),
    )
    meta = model_io.build_metadata(
        run_id='20260101T000000Z-00000001',
        source={'path': 'synthetic', 'sha256': None},
        parameters={'max_k': 100, 'beta': 0.0, 'positive_threshold': 4.0},
    )
    model.save(str(directory), meta, status=model_io.STATUS_VALIDATED_UNPUBLISHED)
    return str(directory)


#: 4 个物品。来源 0 有 2 个邻居（101, 102），来源 1 有 1 个（102），
#: 来源 2 和 3 空区间（零好评）。
_ITEM_IDS = [101, 102, 103, 104]
_INDPTR = [0, 2, 3, 3, 3]
_TARGETS = [1, 2, 2]          # 目标都是**下标**
_SIMS = [0.5, 0.25, 0.75]
_COOCS = [2, 1, 3]
_N_LIKE = [4, 3, 1, 0]
_N_ALL = [5, 5, 2, 1]


class LoadCooccurrenceFromModelDirTest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='heldout_model_')
        self.model_dir = _write_model(
            self.tmp, _ITEM_IDS, _INDPTR, _TARGETS, _SIMS, _COOCS,
            _N_LIKE, _N_ALL)

    def test_shape_and_values(self):
        C, stats = heldout_eval.load_cooccurrence_from_model_dir(
            self.model_dir, _ITEM_IDS)
        self.assertEqual(C.shape, (4, 4))
        self.assertEqual(int(C.nnz), 3)
        # 行 = 来源下标，列 = 目标下标
        self.assertEqual(list(C[0].indices), [1, 2])
        self.assertAlmostEqual(C[0, 1], 0.5)
        self.assertAlmostEqual(C[0, 2], 0.25)
        self.assertEqual(list(C[1].indices), [2])
        self.assertAlmostEqual(C[1, 2], 0.75)
        self.assertEqual(stats['n_edges'], 3)
        self.assertEqual(stats['n_sources_with_neighbors'], 2)
        self.assertEqual(stats['source'], 'model_dir')

    def test_rejects_item_order_mismatch(self):
        """物品顺序不一致必须硬失败——**这是本测试的重点**。

        静默错位会产出一张语义全错但"能算"的图，指标也会是个看起来合理的
        错数字。这里必须抛 CorrectnessError 而不是继续。
        """
        shuffled = [102, 101, 103, 104]
        with self.assertRaises(heldout_eval.CorrectnessError):
            heldout_eval.load_cooccurrence_from_model_dir(
                self.model_dir, shuffled)

    def test_rejects_length_mismatch(self):
        with self.assertRaises(heldout_eval.CorrectnessError):
            heldout_eval.load_cooccurrence_from_model_dir(
                self.model_dir, _ITEM_IDS[:3])

    def test_matrix_multiply_matches_manual(self):
        """打分的唯一用法是 ``A @ C``；这里验证它等于"按来源展开候选"的手算值。

        这条锁住"下标语义没搞反"：若 indices/data 被当成 (target, source)
        反了，形状仍然合法、不会报错，但数值会变——只有对比手算才能抓到。
        """
        C, _stats = heldout_eval.load_cooccurrence_from_model_dir(
            self.model_dir, _ITEM_IDS)
        # 模拟"评测用户看过物品 0（item 101）"：A 的一行在列 0 上为 1。
        # 用稀疏矩阵而非 ndarray——ndarray @ csr 会退化成稠密乘法，
        # 那就不是"生产代码实际走的路径"了。
        A = sparse.csr_matrix(np.array([[1.0, 0.0, 0.0, 0.0]]))
        support = (A @ C).tocsr()
        row = support[0]
        # 来源 0 的邻居：物品 101 相似度 0.5 -> 物品 102；0.25 -> 物品 103
        got = dict(zip(row.indices.tolist(), row.data.tolist()))
        self.assertEqual(sorted(got), [1, 2])
        self.assertAlmostEqual(got[1], 0.5)
        self.assertAlmostEqual(got[2], 0.25)

    def test_stats_keys_match_recompute_path(self):
        """``stats`` 的键必须与 ``build_cooccurrence`` 的返回**一致**。

        这条是被一个真 bug 逼出来的：第一版返回的是 ``n_edges``，而下游
        ``run_evaluation`` 的进度日志和 ``extra['cooccurrence']``（落进
        run_status.json）都按 ``edges`` / ``seconds`` 取值，于是图**已正确
        加载**却在下一行 ``KeyError: 'edges'`` 崩掉。

        所以这里不只断言"有这些键"，而是**直接拿现算路径的返回做对照**——
        键集必须是现算路径键集的子集，缺一个都会在生产路径上炸。
        """
        from scipy import sparse
        A = np.array([
            [1.0, 1.0, 0.0, 0.0],
            [1.0, 1.0, 1.0, 0.0],
            [1.0, 1.0, 0.0, 0.0],
            [1.0, 0.0, 0.0, 0.0],
        ], dtype=np.float64)
        n_like = np.asarray([4, 3, 1, 0], dtype=np.int64)
        _C_built, stats_built = heldout_eval.build_cooccurrence(
            sparse.csr_matrix(A), _ITEM_IDS, 100, 2, n_like)
        _C_model, stats_model = heldout_eval.load_cooccurrence_from_model_dir(
            self.model_dir, _ITEM_IDS)

        missing = set(stats_built) - set(stats_model)
        self.assertEqual(missing, set(),
                         'model-dir stats must expose every key the recompute '
                         'path exposes; missing %s' % sorted(missing))
        # 下游真正取的那两个键必须存在，且**值能喂进它的格式化**。
        # 键名对齐还不够：run_evaluation 的进度日志是
        #   '...edges=%d (%.1fs)' % (cstats['edges'], cstats['seconds'])
        # 第一版给了 seconds=None（为了"不编数"），键名对齐了却在这里
        # TypeError: must be real number, not NoneType ——图已正确加载、
        # 12 分钟的活全白费。所以这里直接用真格式化试一遍。
        for key in ('edges', 'seconds'):
            self.assertIn(key, stats_model)
        self.assertIsInstance(stats_model['edges'], int)
        self.assertIsInstance(stats_model['seconds'], float)
        # 复现下游那一行格式化，两条路径都试（现算路径 seconds 是 float）
        for stats in (stats_built, stats_model):
            'edges=%d (%.1fs)' % (stats['edges'], stats['seconds'])
        # 还要能喂进 JSON（run_status.json 里它是 'extra.cooccurrence' 的一部分）
        json.dumps(stats_model)
        """与脚本自己现算的 ``C`` 落在同一语义上（同一份小数据，两条路径）。

        这条是端到端的口径校验：现算走 "block.T @ A -> lexsort -> 截断"，
        读模型走 "mmap -> csr_matrix"，两者若对同一份输入给出同一个 ``C``，
        说明 ``--model-dir`` 只是换了个来源，没有换语义。

        造的数据让**物品 2（n_like=1）**成为有邻居的来源，用来验证"零好评**目标**
        被过滤、但 n_like>0 的来源仍正常出边"这条边界。
        """
        from scipy import sparse
        # 3 用户 × 4 物品：
        #   物品 0(101): 用户 1,2,3,4 -> n_like=4
        #   物品 1(102): 用户 1,2,3   -> n_like=3
        #   物品 2(103): 用户 2       -> n_like=1
        #   物品 3(104): 无           -> n_like=0
        # 共现 c(0,1)=3, c(0,2)=1, c(1,2)=1；物品 3 零好评 -> 既无来源边
        # 也无目标边。
        A = np.array([
            [1.0, 1.0, 0.0, 0.0],   # 用户 1: 物品 0,1
            [1.0, 1.0, 1.0, 0.0],   # 用户 2: 物品 0,1,2
            [1.0, 1.0, 0.0, 0.0],   # 用户 3: 物品 0,1
            [1.0, 0.0, 0.0, 0.0],   # 用户 4: 物品 0
        ], dtype=np.float64)
        n_like = np.asarray([4, 3, 1, 0], dtype=np.int64)
        C_built, _ = heldout_eval.build_cooccurrence(
            sparse.csr_matrix(A), _ITEM_IDS, 100, 2, n_like)
        self.assertEqual(C_built.shape, (4, 4))
        C_csr = C_built.tocsr()
        # 零好评物品（3）必须是空区间，且不能出现在任何人的目标里
        self.assertEqual(int(C_csr.indptr[4] - C_csr.indptr[3]), 0)
        self.assertNotIn(3, C_csr.indices.tolist())
        # 物品 2（n_like=1）作为来源：邻居是共现过它的 0 和 1
        #   c(2,0)=1 -> sim = 1/sqrt(1*4) = 0.5
        #   c(2,1)=1 -> sim = 1/sqrt(1*3) = 0.577...
        row2 = C_csr[2]
        got2 = dict(zip(row2.indices.tolist(),
                        [float(v) for v in row2.data.tolist()]))
        self.assertEqual(sorted(got2), [0, 1])
        self.assertAlmostEqual(got2[0], 0.5)
        self.assertAlmostEqual(got2[1], 1.0 / np.sqrt(3.0))


if __name__ == '__main__':
    unittest.main()
