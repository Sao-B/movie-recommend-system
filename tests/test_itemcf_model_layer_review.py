# -*- coding: utf-8 -*-
"""**对抗性审查**：模型存储 / 核验 / 结论 / 监督四层（计划书 §4、§5.A、§5.D、§6）。

本文件是 task-9（审查 B）的产物，与既有测试**不重复**：

* ``tests/test_itemcf_full_training.py`` 覆盖构图数学、存取回环、状态机正例；
* ``tests/test_itemcf_resource_guard.py`` 覆盖阈值、采样、终止范围；
* ``tests/test_itemcf_conclusion_writer.py`` 覆盖章节完整性与诚实性正例。

这里只放**审查中新发现**的问题。每条都注明是"已修复（锁死修复后行为）"
还是"仍未修复（如实记录当前行为）"——**不要**把未修条目改成正向断言来
消灭红灯，那等于把缺陷藏起来。

======================  ==========  ===============================================
编号                    状态        内容
======================  ==========  ===============================================
D1                      ✅ 已修复    ``validate_arrays`` 对 indptr 自相矛盾会抛裸
                                    ``IndexError``/``ValueError``（非
                                    ``ModelIOError``）；现改为 fail-fast 提前返回
H1                      ✅ 已修复    ``verify_full_model`` 在"一次都没重算"时报
                                    ``pass=True``；现为 ``pass = 无 failures 且
                                    recompute 真的跑过且通过``
D4                      ✅ 已修复    ``select_sample_sources(count=N)`` 返回远超 N
                                    （``count=2`` → 40）；现配额按 count 收缩
D5                      ✅ 已修复    ``input_rows`` 未知时规模警告整条消失；现明确
                                    警告"无法确认输入规模"
H2                      ❌ 未修复    ``validated_unpublished`` 从不以独立核验为条件
D2                      ❌ 未修复    ``save()`` 只保证 model.json 原子、不保证目录原子
D3                      ❌ 未修复    ``validate_arrays`` 堆临时量约为边数组的 9 倍
======================  ==========  ===============================================

对应测试类：

1. :class:`ValidateArraysRobustnessTests` —— D1（已修复，锁死修复后行为）。
   复核方式：把源码里 ``if indptr_broken:`` 的提前返回改成 ``if False:``，
   两个用例立刻抛回 ``IndexError`` / ``ValueError``；脚本见
   ``.vmwork/review_model/exp17_d1_forensics.py``（不改生产文件）。
2. :class:`SaveAtomicityTests` —— **D2（未修复）**：``save()`` 只保证
   ``model.json`` 原子，不保证**目录**原子。重写已有目录后 7 个数组与旧
   ``model.json`` 并存、无半写标记。
3. :class:`VerificationStatusGateTests` —— **H2（未修复）**：
   ``validated_unpublished`` 从不以独立核验为条件；被污染的模型
   （漏掉最优邻居）能走完全部自证检查并落地。
4. :class:`SkippedVerificationIsNotPassTests` —— H1（已修复）。
5. :class:`SampleSelectionContractTests` —— D4（已修复）。
6. :class:`ConclusionHonestyGapTests` —— D5（已修复）+ H1 的渲染后果。
7. :class:`ValidateArraysMemoryTests` —— **D3（未修复）**：``validate_arrays``
   的堆临时量约为边数组的 9 倍（用 tracemalloc 度量）。
8. :class:`ConfirmedSoundTests` —— **查过没问题的项**，锁死正确行为，
   防止审查结论被误读成"这一层全是坏的"。

**本地未能验证的一项**：任务要求"用 RSS 实测验证 mmap"。本沙箱无法可信读取
RSS（``GetProcessMemoryInfo`` 返回 0；``Get-Process WorkingSet64`` 对一个
明知 touch 了 300 MB 的进程恒返回 4.7 MB，正对照见
``.vmwork/review_model/exp4_runner.ps1``）。因此本文件只断言**堆拷贝**层面
可测的事实（``tracemalloc``），**不做** RSS 断言，也不声称验证过 RSS。

Python 3.6 兼容。运行::

    backend/venv/Scripts/python.exe -m unittest tests.test_itemcf_model_layer_review
"""
import gc
import io
import json
import os
import shutil
import sys
import threading
import time
import tracemalloc
import unittest

import numpy as np

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from experiments.itemcf import conclusion_writer as C  # noqa: E402
from experiments.itemcf import model_io as M  # noqa: E402
from experiments.itemcf import verify_full_model as V  # noqa: E402

FIXTURE_ROOT = os.path.join(PROJECT, '.vmwork', '_model_layer_review')

_SEQ_LOCK = threading.Lock()
_SEQ = [0]


def fixture_dir(prefix):
    """建唯一临时目录（沙箱下不清理，沿用既有 itemcf 测试的做法）。"""
    for _attempt in range(1000):
        with _SEQ_LOCK:
            _SEQ[0] += 1
            seq = _SEQ[0]
        path = os.path.join(FIXTURE_ROOT,
                            '%s-%d-%d-%d' % (prefix, int(time.time() * 1000),
                                             os.getpid(), seq))
        try:
            os.makedirs(path)
            return path
        except OSError:
            continue
    raise RuntimeError('could not create a unique fixture dir under %s'
                       % FIXTURE_ROOT)


RUN_ID = '20261004T120000Z-abcdef01'


def small_model(n_items=4, k=2, seed=1):
    """一份**满足全部结构契约**的小模型（validate_arrays 必须返回空 problems）。

    单独写而不是复用 full_train 的构件，是为了让本文件的用例在最坏情况下
    也能独立运行（审查对象只有 model_io 一层）。
    """
    rng = np.random.RandomState(seed)
    item_ids = np.arange(10, 10 + 10 * n_items, 10, dtype=np.int64)
    n_like = rng.randint(5, 40, size=n_items).astype(np.int64)
    n_all = (n_like + rng.randint(0, 5, size=n_items)).astype(np.int64)
    indptr = np.zeros(n_items + 1, dtype=np.int64)
    t_all, c_all, s_all = [], [], []
    for pos in range(n_items):
        cand = np.array([p for p in range(n_items) if p != pos][:k], dtype=np.int64)
        if len(cand) == 0:
            indptr[pos + 1] = indptr[pos]
            continue
        cmax = np.minimum(n_like[pos], n_like[cand])
        c = np.minimum(cmax, 1 + (np.arange(len(cand)) % 7))
        keep = c > 0
        cand, c = cand[keep], c[keep]
        if len(cand) == 0:
            indptr[pos + 1] = indptr[pos]
            continue
        s = c.astype(np.float64) / np.sqrt(
            float(n_like[pos]) * n_like[cand].astype(np.float64))
        s = np.minimum(s, 1.0)
        order = np.lexsort((cand, -s))
        cand, c, s = cand[order], c[order], s[order]
        t_all.append(cand)
        c_all.append(c)
        s_all.append(s)
        indptr[pos + 1] = indptr[pos] + len(cand)
    targets = (np.concatenate(t_all) if t_all else np.zeros(0, dtype=np.int64))
    coocs = (np.concatenate(c_all) if c_all else np.zeros(0, dtype=np.int64))
    sims = np.concatenate(s_all) if s_all else np.zeros(0, dtype=np.float64)
    return M.NeighborModel(item_ids, indptr,
                           targets.astype(np.int64), sims.astype(np.float64),
                           coocs.astype(np.int64), n_like, n_all)


def metadata_for(extra=None):
    payload = M.build_metadata(run_id=RUN_ID, source={},
                               parameters={'max_k': 100})
    if extra:
        payload.update(extra)
    return payload


def save_verified(model, model_dir, source=None):
    """模拟 ``full_train`` 拿到 ``validated_unpublished`` 的**全部**步骤。

    刻意逐字复刻 full_train.py:2230-2260 的凭据链，好让用例证明：
    这条链子只看自己，不看任何独立核验。
    """
    saved = model.save(model_dir, metadata_for())
    if source:
        saved['source'] = source
    problems, stats = M.validate_arrays(model, max_k=100)
    assert not problems, 'fixture must satisfy the contract: %r' % (problems,)
    hash_ok = M.verify_hashes(model_dir, saved)
    assert all(hash_ok.values())
    saved['status'] = M.STATUS_VALIDATED_UNPUBLISHED
    saved['validation'] = {'hash_verified': True, 'structure_problems': [],
                           'structure_stats': stats}
    M.write_json_atomic(os.path.join(model_dir, M.METADATA_FILE), saved)
    return saved


def module_source(name):
    """读 ``experiments/itemcf/<name>`` 的源码（用于静态契约断言）。"""
    path = os.path.join(PROJECT, 'experiments', 'itemcf', name)
    with io.open(path, encoding='utf-8') as handle:
        return handle.read()


def release_memmaps():
    """Windows 上 memmap 持有文件句柄，改写文件前必须先释放。"""
    gc.collect()


def _sizeable_legal_model():
    """一份**合法**但边数足够大的模型（>50000 边），用于度量 D3 的内存曲线。

    为什么需要它：``validate_arrays`` 的临时量与**边数**成正比，小模型上
    度量不出分块的收益。这里用 400 部电影、每部若干邻居构造出一个
    ``n_edges > 50000`` 且**满足全部结构契约**（``validate_arrays`` 返回空
    problems）的模型，让"分块 vs 不分块"的峰值差异在 tracemalloc 下可见。

    刻意不用 ``small_model`` 放大——那个构造是给契约用例当夹具的，规模一
    上去就会因为 ``max_k`` 与共现上界的关系而变慢。
    """
    n_items = 400
    rng = np.random.RandomState(11)
    item_ids = np.arange(10, 10 + 10 * n_items, 10, dtype=np.int64)
    n_like = rng.randint(50, 400, size=n_items).astype(np.int64)
    n_all = (n_like + rng.randint(0, 5, size=n_items)).astype(np.int64)

    indptr = np.zeros(n_items + 1, dtype=np.int64)
    t_all, c_all, s_all = [], [], []
    for pos in range(n_items):
        # 每个来源取 200 个候选目标，边数约 400 * 200 = 80000 > 50000
        cand = np.array([p for p in range(n_items) if p != pos][:200],
                        dtype=np.int64)
        cmax = np.minimum(n_like[pos], n_like[cand])
        cooc = np.minimum(cmax, 1 + (np.arange(len(cand)) % 5))
        keep = cooc > 0
        cand, cooc = cand[keep], cooc[keep]
        if len(cand) == 0:
            indptr[pos + 1] = indptr[pos]
            continue
        sim = cooc.astype(np.float64) / np.sqrt(
            float(n_like[pos]) * n_like[cand].astype(np.float64))
        sim = np.minimum(sim, 1.0)
        # 排序契约：(-similarity, target_movieId)
        order = np.lexsort((cand, -sim))
        cand, cooc, sim = cand[order], cooc[order], sim[order]
        t_all.append(cand)
        c_all.append(cooc)
        s_all.append(sim)
        indptr[pos + 1] = indptr[pos] + len(cand)

    return M.NeighborModel(
        item_ids, indptr,
        np.concatenate(t_all).astype(np.int64),
        np.concatenate(s_all).astype(np.float64),
        np.concatenate(c_all).astype(np.int64),
        n_like, n_all)


def _contract_violation_models():
    """返回 ``{名字: 模型}``，覆盖 11 类结构违约（含一份 clean 对照）。

    它们的作用是给"分块不得改变判定"提供**覆盖面**：每一类都精确命中
    ``validate_arrays`` 里的一条检查，因此如果分块实现漏检或误检，逐字
    比对 problems 就会失败。

    全部模型都用同一份 item_ids / n_all，只改动被检的那一项，保证
    problems 的差异只来自该违约本身。
    """
    item_ids = np.array([10, 20, 30], dtype=np.int64)
    n_all = np.array([10, 10, 10], dtype=np.int64)
    n_like = np.array([9, 9, 9], dtype=np.int64)

    def model(indptr, targets, sims, coocs, n_like_=None):
        return M.NeighborModel(
            item_ids, np.asarray(indptr, dtype=np.int64),
            np.asarray(targets, dtype=np.int64),
            np.asarray(sims, dtype=np.float64),
            np.asarray(coocs, dtype=np.int64),
            np.asarray(n_like if n_like_ is None else n_like_,
                       dtype=np.int64),
            n_all)

    # 一份契约合法的基准（来源 0 → 目标 1，similarity = 3/9）
    clean_sim = 3.0 / 9.0
    cases = {}

    # 0) clean：结构完全合法，problems 必须是空的
    cases['clean'] = model([0, 1, 1, 1], [1], [clean_sim], [3])

    # 1) 自环：来源 0 → 目标 0
    cases['self_loop'] = model([0, 1, 1, 1], [0], [clean_sim], [3])

    # 2) 降序：同一来源内 similarity 递增（应为递减）
    cases['bad_desc'] = model(
        [0, 3, 3, 3], [1, 2, 3],
        [1.0 / 9.0, 2.0 / 9.0, 3.0 / 9.0], [1, 2, 3])

    # 3) 平票乱序：similarity 全等，target_movieId 应升序
    cases['tie_wrong_id'] = model(
        [0, 3, 3, 3], [3, 2, 1],
        [3.0 / 9.0, 3.0 / 9.0, 3.0 / 9.0], [3, 3, 3])

    # 4) 重复目标：同一来源出现两次目标 1
    cases['dup_target'] = model([0, 2, 2, 2], [1, 1], [clean_sim, clean_sim],
                                [3, 3])

    # 5) 非正相似度：similarity == 0（必须 > 0）
    cases['nonpos_sim'] = model([0, 1, 1, 1], [1], [0.0], [3])

    # 6) 相似度 > 1
    cases['sim_gt_one'] = model([0, 1, 1, 1], [1], [1.5], [3])

    # 7) 非正共现：cooccurrences == 0
    cases['nonpos_cooc'] = model([0, 1, 1, 1], [1], [clean_sim], [0])

    # 8) 共现超上界：cooccurrences > min(n_like[src], n_like[tgt])
    cases['cooc_gt_bound'] = model([0, 1, 1, 1], [1], [clean_sim], [99])

    # 9) NaN 相似度
    cases['nan_sim'] = model([0, 1, 1, 1], [1], [float('nan')], [3])

    # 10) 公式不符：sim 与共现/好评数推出来的值对不上
    cases['sim_formula_off'] = model([0, 1, 1, 1], [1], [0.9], [2])

    return cases


# ---------------------------------------------------------------------------
# 1. D1：validate_arrays 的健壮性
# ---------------------------------------------------------------------------

class ValidateArraysRobustnessTests(unittest.TestCase):
    """**D1（已由 Lead 修复）**：损坏的 indptr 必须走 fail-fast，不能推到崩溃。

    审查发现：``indptr`` 与 ``target_indices`` 自相矛盾时，``validate_arrays``
    会一路推下去撞上未分类异常，而不是返回 problems：

    * ``indptr[-1] > n_edges`` → ``IndexError: index N is out of bounds …``
      （``internal[starts[nonempty]] = False`` 越界）；
    * 非单调 indptr（counts 为负）→ ``ValueError: repeats may not contain
      negative values``（``np.repeat``）。

    两者都**不是** ``ModelIOError``（它也 ``不是`` ``ValueError`` 子类），
    而 ``load_model(validate=True)`` 承诺"任一失败即抛 ModelIOError"。

    Lead 已修：``indptr`` 自身矛盾时置 ``indptr_broken`` 并带着已收集的
    problems 提前返回。下面锁死修复后的行为。

    **复核方式**（可独立重跑）：把当前源码里那段 ``if indptr_broken:``
    提前返回改成 ``if False:``，其余一字不动，两个用例立刻分别抛回
    ``IndexError`` / ``ValueError``。脚本见
    ``.vmwork/review_model/exp17_d1_forensics.py``（用 ``exec`` 装载改后的
    源码副本，**不修改生产文件**）。
    """

    def _model(self, indptr, targets, n_like=None):
        n_items = 4
        targets = np.asarray(targets, dtype=np.int64)
        return M.NeighborModel(
            item_ids=np.array([10, 20, 30, 40], dtype=np.int64),
            indptr=np.asarray(indptr, dtype=np.int64),
            target_indices=targets,
            similarities=np.full(len(targets), 0.5, dtype=np.float64),
            cooccurrences=np.ones(len(targets), dtype=np.int64),
            n_like=np.asarray(n_like if n_like is not None else [5, 4, 0, 0],
                              dtype=np.int64),
            n_all=np.array([6, 5, 3, 2], dtype=np.int64))

    def test_dangling_indptr_is_reported_not_raised(self):
        """D1 修复回归：悬垂 indptr 返回 problems，且打上 ``indptr_broken``。"""
        model = self._model([0, 1, 1, 1, 3], [1])
        problems, stats = M.validate_arrays(model, max_k=100)
        self.assertTrue(any('indptr[-1]' in p for p in problems),
                        'expected the indptr[-1] problem, got %r' % (problems,))
        self.assertTrue(stats.get('indptr_broken'),
                        'the fail-fast flag must be set so callers can tell '
                        '"structurally broken" from "merely non-optimal"')

    def test_negative_counts_indptr_is_reported_not_raised(self):
        """D1 修复回归：非单调 indptr 不再让 ``np.repeat`` 抛 ValueError。"""
        model = self._model([0, 3, 1, 2, 3], [1, 2, 3])
        problems, stats = M.validate_arrays(model, max_k=100)
        self.assertTrue(any('not non-decreasing' in p for p in problems),
                        'got %r' % (problems,))
        self.assertTrue(stats.get('indptr_broken'))

    def test_load_model_wraps_structural_failure_in_modelioerror(self):
        """契约落点：损坏的结构必须被 ``load_model`` 包成 ``ModelIOError``。"""
        work = fixture_dir('wrapd1')
        mdir = os.path.join(work, 'model')
        model = self._model([0, 1, 1, 1, 3], [1])
        saved = model.save(mdir, metadata_for())
        saved['status'] = M.STATUS_VALIDATED_UNPUBLISHED
        M.write_json_atomic(os.path.join(mdir, M.METADATA_FILE), saved)
        with self.assertRaises(M.ModelIOError) as caught:
            M.load_model(mdir, validate=True)
        self.assertIn('structural validation failed', str(caught.exception))

    def test_modelioerror_is_not_a_valueerror_subclass(self):
        """这条曾让 D1 的破坏力更大：``except ValueError`` 兜不住它。

        保留为**已修复缺陷的影响面记录**：D1 时期两类崩溃与 ``ModelIOError``
        没有继承关系，所以"统一按 ModelIOError 处理"的调用方两条都漏。
        """
        self.assertFalse(issubclass(M.ModelIOError, ValueError))
        self.assertFalse(issubclass(M.ModelIOError, IndexError))

    def test_indptr_broken_flag_is_absent_for_healthy_models(self):
        """正向对照：健康模型不得被打上 ``indptr_broken``（否则恒真）。"""
        model = small_model(n_items=20, k=4, seed=4)
        problems, stats = M.validate_arrays(model, max_k=100)
        self.assertEqual(problems, [])
        self.assertFalse(stats.get('indptr_broken', False))


# ---------------------------------------------------------------------------
# 2. D2：save() 的原子性范围
# ---------------------------------------------------------------------------

class SaveAtomicityTests(unittest.TestCase):
    """**D2（已由 Lead 修复）**：重写目录时旧 ``model.json`` 必须立刻失效。

    原始缺陷：``save()`` 只保证单个 ``model.json`` 原子，不保证目录安全。
    重写已有目录时，数组逐个就地覆盖，而上一轮的 ``model.json`` 一直可读
    ——"数组写了一半 + 旧元数据还在"是一个能长期存在的中间态，且目录里
    没有任何"半写"标记，裸 ``load_model`` 会静默读到新旧混杂的数组。

    修复：``save()`` 在写数组**之前**先删除旧 ``model.json`` 并写
    ``.building`` 标记；收尾时删标记、再原子写新元数据。于是任何中途崩溃都
    表现为"缺元数据"，属于**安全失败**。下面锁死修复后的行为。
    """

    def test_first_save_leaves_no_temp_or_partial_metadata(self):
        """首次写入的正常路径：不留 .tmp、不留 .building、model.json 完整。"""
        work = fixture_dir('saveatomic')
        mdir = os.path.join(work, 'model')
        small_model().save(mdir, metadata_for())
        self.assertTrue(os.path.isfile(os.path.join(mdir, M.METADATA_FILE)))
        self.assertFalse(os.path.isfile(os.path.join(mdir, M.METADATA_FILE) + '.tmp'))
        # 收尾必须清掉"正在构建"标记，否则调用方会永远拒绝这个目录。
        self.assertFalse(os.path.isfile(
            os.path.join(mdir, M.BUILDING_MARKER_FILE)),
            'the building marker must be removed once the model is complete')
        for name in M.ARRAY_FILES:
            self.assertTrue(os.path.isfile(os.path.join(mdir, name)))

    def test_rewrite_invalidates_stale_metadata_before_touching_arrays(self):
        """**D2 修复回归**：重写期间旧 ``model.json`` 必须不可见。

        修复前：数组就地覆盖的整个窗口里，上一轮的 ``model.json`` 一直可读，
        且它的哈希对应旧数组——裸 ``load_model`` 会拿它去校验"新旧混杂"的
        数组。修复后：写数组**之前**旧元数据已被删除、``.building`` 标记已就位。
        """
        work = fixture_dir('savetorn')
        mdir = os.path.join(work, 'model')
        model = small_model()
        model.save(mdir, metadata_for())
        original_ids = np.asarray(model.item_ids).tolist()
        meta_path = os.path.join(mdir, M.METADATA_FILE)
        marker_path = os.path.join(mdir, M.BUILDING_MARKER_FILE)
        self.assertTrue(os.path.isfile(meta_path))

        # 在第一次写数组时就观察目录状态：旧元数据应已消失、标记应已就位。
        observed = {'meta_present': None, 'marker_present': None}
        import numpy as _np
        real_save = _np.save

        def spy(file, arr, **kwargs):
            if observed['meta_present'] is None:
                observed['meta_present'] = os.path.isfile(meta_path)
                observed['marker_present'] = os.path.isfile(marker_path)
            return real_save(file, arr, **kwargs)

        _np.save = spy
        try:
            small_model(seed=2).save(mdir, metadata_for())
        finally:
            _np.save = real_save

        self.assertFalse(
            observed['meta_present'],
            'D2 regression: the OLD model.json must not be readable while the '
            'arrays are being rewritten')
        self.assertTrue(
            observed['marker_present'],
            'D2 regression: a .building marker must exist while the arrays are '
            'being rewritten')
        # 收尾后必须恢复成一个自洽的目录。
        self.assertTrue(os.path.isfile(meta_path))
        self.assertFalse(os.path.isfile(marker_path))
        self.assertTrue(os.path.isfile(os.path.join(mdir, 'item_ids.npy')))

    def test_failed_rewrite_leaves_no_trustworthy_metadata(self):
        """**D2 修复的后果**：中途失败只表现为"缺元数据"，而不是坏数据。

        修复前，重写途中的目录里同时存在"半新数组 + 旧 model.json"，
        而旧元数据的哈希对应**上一轮**的数组——这是唯一会导致"静默读到
        坏数据"的路径。修复后旧元数据在写数组前就被删除，于是任何中途
        失败都会被 :func:`load_model` 直接拒绝。
        """
        work = fixture_dir('savetorn')
        mdir = os.path.join(work, 'model')
        model = small_model()
        model.save(mdir, metadata_for())
        original_ids = np.asarray(model.item_ids).tolist()
        meta_path = os.path.join(mdir, M.METADATA_FILE)

        # 模拟"数组已就地覆盖、但再装配没能跑到收尾"的崩溃现场：保持长度
        # 不变，只改内容（长度变了会被 NeighborModel._check_shapes 直接拒掉，
        # 那反而掩盖了"目录里躺着旧元数据"这个真问题）。
        tampered = list(original_ids)
        tampered[-1] = tampered[-1] + 1
        # 先让旧元数据失效（这正是修复后的 save() 会做的事）
        os.remove(meta_path)
        M.write_json_atomic(os.path.join(mdir, M.BUILDING_MARKER_FILE), {})
        np.save(os.path.join(mdir, 'item_ids.npy'),
                np.array(tampered, dtype=np.int64), allow_pickle=False)

        # 目录里现在还留着"正在构建"的标记，且没有 model.json
        self.assertFalse(os.path.isfile(meta_path))
        self.assertTrue(os.path.isfile(
            os.path.join(mdir, M.BUILDING_MARKER_FILE)),
            'a half-written directory must carry the building marker')

        # 裸 NeighborModel.load() 仍能读到数组，但没有任何元数据背书它
        loaded = M.NeighborModel.load(mdir)
        self.assertNotEqual(np.asarray(loaded.item_ids).tolist(), original_ids)

        # 契约落点：没有 model.json 就必须拒绝加载，而不是拿半份文件当模型
        with self.assertRaises(M.ModelIOError):
            M.load_model(mdir, validate=True)


# ---------------------------------------------------------------------------
# 3. H1/H2：状态机的真实门槛
# ---------------------------------------------------------------------------

class VerificationStatusGateTests(unittest.TestCase):
    """**确认缺陷 H2**：``validated_unpublished`` 从不以独立核验为条件。

    ``full_train.py:2230-2260`` 拿这个状态的凭据只有两条：
    ``validate_arrays``（装配它的同一个模块自查）与 ``verify_hashes``
    （校验"刚写的哈希等于刚记的哈希"，恒真）。唯一从原始评分独立重算的
    ``verify_full_model`` 是另一个 CLI，产物不被 model_io 读取。
    """

    def test_status_is_earned_without_any_verification_json(self):
        """走完"自证链"后目录里没有 verification.json，但状态已是已验证。"""
        work = fixture_dir('gate')
        mdir = os.path.join(work, 'model')
        save_verified(small_model(), mdir)

        self.assertFalse(os.path.isfile(os.path.join(work, 'verification.json')))
        loaded, meta = M.load_model(
            mdir, validate=True,
            require_status=M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertEqual(meta['status'], M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertEqual(loaded.n_items, 4)

    def test_verification_failure_downgrades_the_model_status(self):
        """**H2 修复回归**：核验真的发现不符时，``model.json`` 必须被降级。

        修复前 ``model.json`` 始终写着 ``validated_unpublished``，即使它自己的
        核验者已经判定"复现不了输入"——两个产物互相矛盾、无人调和，
        下游只读 ``model.json`` 就会采信一个被否定的模型。

        修复：``model_io.apply_verification_verdict`` 在**核验真跑过且发现不符**时
        降级为 ``verification_failed``；``verify_full_model`` 收尾时自动调用它。
        """
        work = fixture_dir('downgrade')
        mdir = os.path.join(work, 'model')
        save_verified(small_model(), mdir)

        verdict = {'pass': False, 'recompute_ran': True,
                   'checks': {'recompute': {'checked_sources': 4,
                                            'mismatch_count': 2}}}
        before, after, changed = M.apply_verification_verdict(mdir, verdict)
        self.assertEqual(before, M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertEqual(after, M.STATUS_VERIFICATION_FAILED)
        self.assertTrue(changed)
        self.assertEqual(M.read_json(os.path.join(mdir, 'model.json'))['status'],
                         M.STATUS_VERIFICATION_FAILED)
        # 降级后**不能**再按"已核验未发布"加载。
        with self.assertRaises(M.ModelIOError):
            M.load_model(mdir, validate=True,
                         require_status=M.STATUS_VALIDATED_UNPUBLISHED)

    def test_downgrade_is_monotonic_and_never_upgrades(self):
        """降级后不得被"洗白"回 ``validated_unpublished``。"""
        work = fixture_dir('monotonic')
        mdir = os.path.join(work, 'model')
        save_verified(small_model(), mdir)
        M.apply_verification_verdict(mdir, {'pass': False, 'recompute_ran': True,
                                            'checks': {'recompute': {
                                                'checked_sources': 1,
                                                'mismatch_count': 1}}})
        # 再喂一个"通过"的判定，状态**必须**保持失败。
        before, after, changed = M.apply_verification_verdict(
            mdir, {'pass': True, 'recompute_ran': True,
                   'checks': {'recompute': {'checked_sources': 4,
                                            'mismatch_count': 0}}})
        self.assertEqual(before, M.STATUS_VERIFICATION_FAILED)
        self.assertEqual(after, M.STATUS_VERIFICATION_FAILED)
        self.assertFalse(changed)

    def test_verification_that_never_ran_does_not_downgrade(self):
        """**"没跑"与"跑了但不符"必须分开**：拿错输入不该把好模型判死。"""
        work = fixture_dir('norun')
        mdir = os.path.join(work, 'model')
        save_verified(small_model(), mdir)
        skipped = {'pass': False, 'recompute_ran': False,
                   'checks': {'recompute': {'skipped': True,
                                            'checked_sources': None}}}
        before, after, changed = M.apply_verification_verdict(mdir, skipped)
        self.assertEqual(after, M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertFalse(changed)
        self.assertIn(M.STATUS_VERIFICATION_FAILED, M.ALLOWED_STATUSES)

    def test_model_io_reads_verification_only_to_downgrade(self):
        """``model_io`` 现在**会**看到核验判定 —— 但只为降级，不为升级。

        修复前这条断言是 ``assertNotIn('verification.json', source)``：
        "状态无从依赖核验"。那正是缺陷本身。现在的契约是：
        核验判定**可以**把状态往下压，但**没有任何路径**把它往上抬。
        """
        source = module_source('model_io.py')
        self.assertIn('apply_verification_verdict', source)
        # 只降级：函数体里不得出现"把状态设为 validated_unpublished"的赋值。
        verdict_source = source[source.index('def apply_verification_verdict'):]
        self.assertNotIn("metadata['status'] = STATUS_VALIDATED_UNPUBLISHED",
                         verdict_source)

    def test_a_model_missing_its_best_neighbour_passes_every_self_check(self):
        """**缺陷的后果**：漏掉最优邻居的模型能拿到 ``validated_unpublished``。

        删掉来源 0 的最优边、重算哈希（模拟一次成功的再装配），然后走
        完整自证链——``validate_arrays`` 无问题、``verify_hashes`` 全真、
        ``load_model(require_status=...)`` 接受。而这个模型确实少了一条
        独立重算就能发现的邻居。
        """
        work = fixture_dir('gatepoison')
        mdir = os.path.join(work, 'model')
        model = small_model()
        smallest = None
        counts = np.diff(np.asarray(model.indptr))
        for pos in range(len(counts)):
            if counts[pos] >= 1 and int(model.n_like[pos]) > 0:
                smallest = pos
                break
        self.assertIsNotNone(smallest, 'fixture needs a source with neighbours')

        indptr = np.array(model.indptr, dtype=np.int64)
        indptr[smallest + 1:] -= 1
        poisoned = M.NeighborModel(
            item_ids=model.item_ids, indptr=indptr,
            target_indices=model.target_indices[1:],
            similarities=model.similarities[1:],
            cooccurrences=model.cooccurrences[1:],
            n_like=model.n_like, n_all=model.n_all)

        saved = poisoned.save(mdir, metadata_for())
        saved['status'] = M.STATUS_VALIDATED_UNPUBLISHED
        M.write_json_atomic(os.path.join(mdir, M.METADATA_FILE), saved)

        problems, _stats = M.validate_arrays(poisoned, max_k=100)
        self.assertEqual(problems, [],
                         'the poisoned model is still internally consistent')
        self.assertTrue(all(M.verify_hashes(mdir, saved).values()))
        _loaded, meta = M.load_model(
            mdir, validate=True,
            require_status=M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertEqual(meta['status'], M.STATUS_VALIDATED_UNPUBLISHED)
        self.assertTrue(
            np.array_equal(np.asarray(poisoned.target_indices),
                           np.asarray(model.target_indices[1:])),
            'the poisoned model really is missing the dropped edge')


# ---------------------------------------------------------------------------
# 4. H1：跳过核验却报 pass
# ---------------------------------------------------------------------------

def build_verification_run(tag, sample_sources=None, with_input=False):
    """建一个最小的完整 run 目录（model/ + run_status.json），供 verifier 用。

    :param with_input: 同时写一份能被独立重算读懂的 ``ratings.csv``，并把
        它的路径与 sha256 记进 ``model.json`` 的 ``source``，这样 B 段能真正
        跑起来（用于"真核验必须 pass"的正向对照）。
    """
    work = fixture_dir(tag)
    run_dir = os.path.join(work, RUN_ID)
    mdir = os.path.join(run_dir, 'model')
    os.makedirs(mdir)

    model = small_model(n_items=12, k=3, seed=3)
    source = {}
    if with_input:
        # 独立重算会从这份 CSV 重算 n_like，并要求它**逐个电影**等于
        # model.n_like（否则报 "n_like disagrees"，那是"输入不同源"的保护）。
        # 所以这里按 model.n_like 精确构造：每部电影的好评数 = n_like[pos]，
        # 另加若干低分评分让它满足 n_all = n_like + 1。
        input_path = os.path.join(work, 'ratings.csv')
        with open(input_path, 'w', newline='', encoding='utf-8') as handle:
            handle.write('userId,movieId,rating,timestamp\n')
            stamp = 100000
            for pos in range(12):
                movie_id = 10 * (pos + 1)          # small_model 的 movieId 步长
                likes = int(model.n_like[pos])
                total = int(model.n_all[pos])
                for user in range(total):
                    stamp += 1
                    rating = 5.0 if user < likes else 2.0
                    handle.write('%d,%d,%s,%d\n'
                                 % (user + 1, movie_id, rating, stamp))
        source = {'path': input_path, 'sha256': M.file_sha256(input_path),
                  'rows': int(model.n_all.sum())}

    saved = save_verified(model, mdir, source=source)
    # ``verify_full_model.resolve_input`` 从 manifest.json / run_status.json 的
    # ``input`` 键找原始评分（不读 model.json），所以这里也要写上。
    M.write_json_atomic(os.path.join(run_dir, 'run_status.json'), {
        'status': 'success', 'run_id': RUN_ID,
        'model_id': M.compute_model_id(RUN_ID),
        'input': source.get('path'),
        'input_sha256': source.get('sha256'),
    })
    return run_dir, mdir, saved


class SkippedVerificationIsNotPassTests(unittest.TestCase):
    """**H1（已由 Lead 修复）**：一次都没重算的核验，不得报 ``pass=True``。

    审查中发现：``verify_full_model.run()`` 把 recompute 段标成 ``skipped``
    时**不往 failures 里加东西**，而 ``report['pass'] = not report['failures']``，
    于是 ``verification.json`` 顶层 ``pass: true``、``checked_sources: None``，
    CLI 退出码 0 —— 任何读 ``pass`` 或退出码的下游都会判定"独立核验通过"。

    Lead 已修：现在 ``pass = (无 failures) AND (recompute 真的跑过且通过)``，
    并新增 ``report['recompute_ran']``。下面把**修复后的行为**锁死，防止回退。
    """

    def test_skip_recompute_does_not_pass(self):
        run_dir, _mdir, _saved = build_verification_run('skipflag')
        report = V.run(V.parse_args(['--run-dir', run_dir,
                                     '--skip-recompute']))
        self.assertTrue(report['checks']['recompute']['skipped'])
        self.assertFalse(report.get('recompute_ran'))
        self.assertFalse(report['pass'],
                         'H1 regression: a run that recomputed 0 sources must '
                         'NOT report pass')
        self.assertTrue(any('did NOT run' in f for f in report['failures']),
                        'the failure must name the skipped recompute: %r'
                        % (report['failures'],))

        on_disk = M.read_json(os.path.join(run_dir, 'verification.json'))
        self.assertFalse(on_disk['pass'])
        self.assertIsNone(on_disk['checks']['recompute'].get('checked_sources'))

    def test_sample_sources_zero_does_not_pass(self):
        run_dir, _mdir, _saved = build_verification_run('skipzero')
        report = V.run(V.parse_args(['--run-dir', run_dir,
                                     '--sample-sources', '0']))
        self.assertTrue(report['checks']['recompute']['skipped'])
        self.assertFalse(report['pass'],
                         'H1 regression: --sample-sources 0 must not pass')

    def test_missing_input_file_does_not_pass(self):
        """输入文件找不到 → B 段无法执行 → 同样不得报 pass。"""
        run_dir, _mdir, _saved = build_verification_run('skipnoinput')
        report = V.run(V.parse_args(['--run-dir', run_dir,
                                     '--sample-sources', '5']))
        rec = report['checks']['recompute']
        self.assertTrue(rec['skipped'])
        self.assertFalse(rec['input_available'])
        self.assertIn('does NOT claim', rec['reason'])
        self.assertFalse(report['pass'],
                         'H1 regression: no input file means recompute is '
                         'impossible, so the report must not pass')

    def test_recompute_ran_flag_is_true_for_a_real_verification(self):
        """正向对照：真跑了重算时 ``recompute_ran`` 为真。

        没有这条，"跳过就不通过"可以靠一根恒假的开关骗过去。
        这里提供真实的 ``ratings.csv``，让 B 段真的执行。
        """
        run_dir, _mdir, _saved = build_verification_run('skipctl',
                                                        with_input=True)
        report = V.run(V.parse_args(['--run-dir', run_dir,
                                     '--sample-sources', '3']))
        rec = report['checks']['recompute']
        self.assertFalse(rec.get('skipped', False),
                         'a real recompute must not be marked skipped: %r'
                         % (rec,))
        self.assertTrue(report.get('recompute_ran'))
        self.assertTrue(rec['checked_sources'])

    def test_a_real_mismatch_does_fail(self):
        """负向对照：结构损坏时必须失败，证明上面不是恒真断言。"""
        run_dir, mdir, _saved = build_verification_run('skipbad')
        release_memmaps()
        target = os.path.join(mdir, 'similarities.npy')
        with open(target, 'rb') as handle:
            raw = bytearray(handle.read())
        raw[-1] ^= 0xFF
        with open(target, 'wb') as handle:
            handle.write(bytes(raw))
        with self.assertRaises(M.ModelIOError):
            V.run(V.parse_args(['--run-dir', run_dir, '--skip-recompute']))


# ---------------------------------------------------------------------------
# 5. D4：抽样数量契约
# ---------------------------------------------------------------------------

class SampleSelectionContractTests(unittest.TestCase):
    """**D4（已由 Lead 修复）**：``select_sample_sources(count=N)`` 必须 <= N。

    审查发现：高/低支持度各 20、并列边界 20 的三份配额是在**减去** ``count``
    之前就定死的，于是 ``count < 60`` 时 ``chosen`` 直接冲过 ``count`` ——
    实测 ``count=2`` 返回 **40** 个（20 倍），``count=0`` 也返回 40。
    ``--sample-sources`` 是用户可控参数，超出请求会变成对全量模型实打实的
    额外重算开销，且报告里 ``requested`` 与 ``selected`` 不一致。

    Lead 已修：配额按 ``count`` 比例收缩（各占 1/3，余数补给高支持度），
    总数上界严格是 ``count``；``count <= 0`` 返回空。下面锁死修复后行为。
    """

    def test_selection_never_exceeds_the_requested_count(self):
        """D4 修复回归：请求几个就最多几个。"""
        model = small_model(n_items=120, k=4, seed=5)
        for count in (0, 1, 2, 5, 10, 59, 60, 100):
            selected = len(V.select_sample_sources(model, seed=42,
                                                   count=count)['all'])
            self.assertLessEqual(
                selected, count,
                'DEFECT D4 regression: count=%d returned %d sources'
                % (count, selected))

    def test_zero_means_zero(self):
        """``count=0`` 是明确"不要抽样"，必须返回空。"""
        model = small_model(n_items=120, k=4, seed=5)
        sample = V.select_sample_sources(model, seed=42, count=0)
        self.assertEqual(sample['all'], [])
        self.assertEqual(sample['high_support'], [])
        self.assertEqual(sample['low_support'], [])
        self.assertEqual(sample['boundary'], [])

    def test_a_small_request_still_returns_something_useful(self):
        """收缩配额不等于"小 count 就抽不到"：count=5 应尽量给满 5 个。"""
        model = small_model(n_items=120, k=4, seed=5)
        sample = V.select_sample_sources(model, seed=42, count=5)
        self.assertEqual(len(sample['all']), 5)

    def test_full_size_request_is_unchanged(self):
        """默认 100 的配额行为不变（修复不应收窄正规路径）。"""
        model = small_model(n_items=120, k=4, seed=5)
        sample = V.select_sample_sources(model, seed=42, count=100)
        self.assertEqual(len(sample['all']), 100)
        self.assertLessEqual(len(sample['high_support']), 20)
        self.assertLessEqual(len(sample['low_support']), 20)
        self.assertLessEqual(len(sample['boundary']), 20)

    def test_count_is_capped_by_the_number_of_eligible_sources(self):
        """来源不足时按实际数量返回（不会无限撑）。"""
        model = small_model(n_items=120, k=4, seed=5)
        self.assertEqual(len(V.select_sample_sources(model, seed=42,
                                                     count=500)['all']), 120)

    def test_selection_remains_deterministic_after_the_fix(self):
        """配额收缩后抽样仍须可复现（固定 seed）。"""
        model = small_model(n_items=120, k=4, seed=5)
        for count in (3, 7, 42):
            self.assertEqual(V.select_sample_sources(model, seed=42, count=count),
                             V.select_sample_sources(model, seed=42, count=count))


# ---------------------------------------------------------------------------
# 6. D5 + H1 的渲染后果
# ---------------------------------------------------------------------------

class ConclusionHonestyGapTests(unittest.TestCase):
    """``conclusion_writer`` 的诚实性缺口。

    **D5**：``input_rows`` 未知时，"不是完整 32M"的警告整条消失，只剩一个
    空列表项。

    **H1 的渲染后果**：顶层 ``pass=True``（哪怕重算了 0 个来源）会让第 7 节
    写出"已通过独立核验"，而第 6 节同一页却显示"重算 N/A 个来源"。
    """

    def _render(self, name, **files):
        path = fixture_dir('concl-' + name)
        for filename, payload in files.items():
            target = os.path.join(path, filename)
            parent = os.path.dirname(target)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            with open(target, 'w', encoding='utf-8') as handle:
                json.dump(payload, handle)
        return path, C.render(C.collect(path))

    BASE_STATUS = {
        'status': 'success', 'run_id': RUN_ID,
        'model_id': 'model-' + RUN_ID,
        'counts': {'input_rows': 32000204, 'users': 200948, 'items': 87585,
                   'items_zero_like': 1, 'positive_pairs': 16000000,
                   'graph_edges': 8000000, 'graph_sources': 86000},
        'config': {'max_k': 100, 'beta': 0.0, 'positive_threshold': 4.0,
                   'block_size': 128, 'monitor_interval': 2.0},
    }
    MODEL_META = {'status': 'validated_unpublished',
                  'counts': {'n_edges': 8000000}}

    def test_unknown_input_rows_is_warned_not_silently_dropped(self):
        """**D5（已由 Lead 修复）**：``input_rows`` 未知时必须警告。

        修复前：分支写成 ``… if rows is not None else ''``，于是行数缺失时
        唯一那条规模警告渲染成**空字符串**（整条消失），同一页"读取行数"
        却显示 ``N/A`` —— 读者会以为"没说就是没问题"。

        Lead 已修：新增 ``elif rows is None`` 分支，明确写出"无法确认输入
        规模 / 不要把本轮结果当作全量结论"。下面锁死修复后行为。
        """
        status = dict(self.BASE_STATUS)
        status['counts'] = dict(self.BASE_STATUS['counts'])
        status['counts'].pop('input_rows')
        _path, text = self._render('d5', **{'run_status.json': status})

        self.assertIn('读取行数：**N/A**', text,
                      'the page must admit the row count is unknown')
        self.assertIn('不要把本轮结果当作全量结论', text,
                      'D5 regression: an unknown scale must still warn')
        self.assertIn('无法确认输入规模', text)
        self.assertNotIn('该文件是**完整** MovieLens-32M', text,
                         'unknown rows must never be rendered as "full 32M"')

    def test_three_scale_states_have_three_distinct_warnings(self):
        """三种规模状态必须是**三条不同措辞**，不能混为一谈。"""
        base = dict(self.BASE_STATUS)
        _p1, full = self._render(
            'scale-full', **{'run_status.json': dict(
                base, counts=dict(base['counts'], input_rows=32000204))})
        _p2, small = self._render(
            'scale-small', **{'run_status.json': dict(
                base, counts=dict(base['counts'], input_rows=3262))})
        counts_no_rows = dict(base['counts'])
        counts_no_rows.pop('input_rows')
        _p3, unknown = self._render(
            'scale-unknown', **{'run_status.json': dict(
                base, counts=counts_no_rows)})

        self.assertIn('该文件是**完整** MovieLens-32M', full)
        self.assertNotIn('不要把本轮结果当作全量结论', full)

        self.assertIn('不是**完整的 MovieLens-32M', small)
        self.assertIn('不要把本轮结果当作全量结论', small)

        self.assertIn('无法确认输入规模', unknown)
        self.assertNotIn('不是**完整的 MovieLens-32M', unknown)

    def test_non_full_input_is_still_flagged(self):
        """负向对照：行数已知且不是 32M 时警告必须在。"""
        status = dict(self.BASE_STATUS)
        status['counts'] = dict(self.BASE_STATUS['counts'], input_rows=3262)
        _path, text = self._render('d5ctl', **{'run_status.json': status})
        self.assertIn('不要把本轮结果当作全量结论', text)

    def test_skipped_verification_no_longer_renders_as_verified(self):
        """**H1 的渲染后果（已随 H1 一并修复）**。

        修复前：顶层 ``pass=True`` → 第 7 节写"已通过独立核验"，而第 6 节
        同一页写着"重算 N/A 个来源"。修复后 ``pass=False``，第 7 节必须
        改口为"不满足接入条件"。本用例锁死修复后的行为。

        用一份**真实** ``verify_full_model`` 在 ``--skip-recompute`` 下产出的
        verification.json（不是手写桩），喂给结论渲染。
        """
        run_dir, _mdir, _saved = build_verification_run('conclskip')
        report = V.run(V.parse_args(['--run-dir', run_dir,
                                     '--skip-recompute']))
        self.assertFalse(report['pass'], 'H1 regression: must not pass')

        # 把该 run 的产物补齐到结论渲染所需的最小集合
        M.write_json_atomic(
            os.path.join(run_dir, 'run_status.json'),
            dict(self.BASE_STATUS, run_id=RUN_ID,
                 model_id='model-' + RUN_ID))
        text = C.render(C.collect(run_dir))

        self.assertIn('重算 N/A 个来源', text,
                      'section 6 shows the sample size was never measured')
        self.assertNotIn('已通过独立核验', text,
                         'H1 render regression: the conclusion must not claim '
                         'independent verification that did not run')
        self.assertIn('不满足接入条件', text)

    def test_empty_verification_summary_renders_as_pass(self):
        """一个 ``{'pass': True, 'checks': {}}`` 的 verification.json 会被读成"通过"。

        这是 H1 渲染链的**最小形式**：只要顶层 ``pass`` 为真，
        ``_verification_summary`` 就把它当作核验通过，而 ``checks`` 里
        每个字段都是 ``None``（结论会渲染成一串 N/A，但第 7 节仍给肯定结论）。
        """
        summary = C._verification_summary({'pass': True, 'checks': {}})
        self.assertTrue(summary['pass'])
        self.assertIsNone(summary['checked_sources'])
        self.assertIsNone(summary['structure_pass'])

    def test_missing_verification_file_is_warned(self):
        """负向对照：真的没有 verification.json 时会明确警告。"""
        _path, text = self._render('noverif2',
                                   **{'run_status.json': self.BASE_STATUS})
        self.assertIn('未找到 `verification.json`', text)
        self.assertIn('不要把模型视为可用', text)

    def test_resource_failure_is_not_rendered_as_zero_effect(self):
        """查过没问题：资源失败明确写成"不是算法效果零分"。"""
        status = dict(self.BASE_STATUS, status='resource_failed',
                      failure_reason='rss_limit')
        _path, text = self._render('resfail', **{'run_status.json': status})
        self.assertIn('不是算法效果零分', text)
        self.assertIn('不满足接入条件', text)


# ---------------------------------------------------------------------------
# 7. D3：validate_arrays 的堆临时量
# ---------------------------------------------------------------------------

class ValidateArraysMemoryTests(unittest.TestCase):
    """**确认缺陷 D3**：``validate_arrays`` 的堆临时量约是边数组的 8 倍。

    docstring 强调"没有逐边 Python 循环，全量上千万边也能在秒级跑完"，
    但没提**瞬时内存**：它以 ``np.repeat`` 造出 n_edges 的 int64 映射，
    再叠若干同长临时量。实测（tracemalloc，20M 边 / 464 MB 模型）峰值约
    1.5 GB。而它就跑在"3 GiB RSS 上限"那条被监督的路径上，且
    ``verify_full_model`` 还会再调一次。

    这里用小模型按**比例**断言，避免测试真的吃掉 GB 级内存。
    """

    def test_heap_temporaries_scale_with_edge_count_not_item_count(self):
        """**D3（已由 Lead 修复）**：``validate_arrays`` 的堆临时量按边数增长。

        原始缺陷：逐边检查一次性在整条边数组上开临时量，tracemalloc 实测
        峰值约为边数组的 2.5~9.9 倍，全量 2000 万边外推约 1.5 GB，而它跑在
        "3 GiB 上限"那条受监督路径上。修复：按来源边界分块（``chunk_edges``，
        默认 200 万边）。下面的 fixture 低于默认阈值，所以这里度量的是
        **不分块**分支的比值，把记录到的区间锁死；分块是否真的封顶由
        :meth:`test_chunked_validation_bounds_the_heap_peak` 验证。
        """
        # 每个来源最多 100 条邻居，所以要 >500 列才能拿到 >50000 条边。
        cols, rows_n = 800, 6000
        rng = np.random.RandomState(11)
        r = rng.randint(0, rows_n, size=900000)
        c = rng.randint(0, cols, size=900000)
        pairs = sorted(set(zip(r.tolist(), c.tolist())))
        item_ids = np.arange(1, cols + 1, dtype=np.int64)
        n_like = np.zeros(cols, dtype=np.int64)
        for _u, i in pairs:
            n_like[i] += 1
        n_all = (n_like + 1).astype(np.int64)

        cooc = {}
        for u, i in pairs:
            cooc.setdefault(i, set()).add(u)
        indptr = np.zeros(cols + 1, dtype=np.int64)
        t_all, c_all, s_all = [], [], []
        for i in range(cols):
            cand = []
            ni = int(n_like[i])
            for j in range(cols):
                if i == j:
                    continue
                common = len(cooc.get(i, ()) & cooc.get(j, ()))
                if common > 0 and int(n_like[j]) > 0:
                    cand.append((j, min(common, ni, int(n_like[j]))))
            if not cand:
                indptr[i + 1] = indptr[i]
                continue
            scored = [(j, cc, cc / np.sqrt(float(ni) * float(n_like[j])))
                      for j, cc in cand]
            scored.sort(key=lambda e: (-e[2], e[0]))
            scored = scored[:100]
            arr_t = np.array([e[0] for e in scored], dtype=np.int64)
            arr_c = np.array([e[1] for e in scored], dtype=np.int64)
            arr_s = arr_c.astype(np.float64) / np.sqrt(
                float(ni) * n_like[arr_t].astype(np.float64))
            t_all.append(arr_t)
            c_all.append(arr_c)
            s_all.append(np.minimum(arr_s, 1.0))
            indptr[i + 1] = indptr[i] + len(scored)

        model = M.NeighborModel(
            item_ids, indptr,
            np.concatenate(t_all).astype(np.int64),
            np.concatenate(s_all).astype(np.float64),
            np.concatenate(c_all).astype(np.int64),
            n_like, n_all)
        problems, _stats = M.validate_arrays(model, max_k=100)
        self.assertEqual(problems, [], 'fixture must be contract-legal')

        n_edges = model.n_edges
        self.assertGreater(n_edges, 50000, 'fixture needs a sizeable edge count')
        edge_bytes = n_edges * 8

        # 注意：这个 fixture 只有 ~6 万边，**低于**默认分块阈值
        # （DEFAULT_VALIDATE_CHUNK_EDGES = 200 万），所以默认调用仍然是
        # 不分块路径、临时量依旧约为边数组的数倍。这是**预期行为**：
        # 分块是为了给全量模型封顶，不是为了让小模型变快。
        # 真正验证分块收效的是 test_chunked_validation_bounds_the_heap_peak。
        self.assertGreater(
            M.DEFAULT_VALIDATE_CHUNK_EDGES, n_edges,
            'this fixture is meant to sit below the default chunk threshold')

        tracemalloc.start()
        try:
            M.validate_arrays(model, max_k=100)
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        ratio = peak / float(edge_bytes)
        # 不分块路径的比值仍在个位数（修复前实测 2.5~9.9x）
        self.assertGreater(ratio, 1.0)
        self.assertLess(
            ratio, 30.0,
            'unchunked validate_arrays peaked at %.1f MB for %.1f MB of edge '
            'arrays (ratio %.2fx) -- beyond the recorded range'
            % (peak / 1048576.0, edge_bytes / 1048576.0, ratio))

    def test_chunked_validation_bounds_the_heap_peak(self):
        """**D3 修复回归**：分块后堆峰值显著低于不分块，且与总边数脱钩。

        修复前峰值是 n_edges 的线性函数（实测约 2.5~9.9×边数组）：全量
        2000 万边约 1.2~1.5 GB，而这段跑在"3 GiB 上限"的受监督路径上。
        修复：``validate_arrays`` 按来源边界分块（默认 200 万边/块），峰值
        只与块大小有关。

        这里用**同一个模型**对比"不分块 vs 分块"，断言分块更低、且分块峰值
        不超过整条边数组的字节数（宽松上界；实测小模型上约 0.1x）。
        """
        model = _sizeable_legal_model()
        n_edges = model.n_edges
        self.assertGreater(n_edges, 50000,
                           'fixture needs a sizeable edge count')
        edge_bytes = n_edges * (8 + 8 + 8)      # targets + sims + coocs

        def peak_with(**kwargs):
            tracemalloc.start()
            try:
                M.validate_arrays(model, max_k=100, **kwargs)
                _cur, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            return peak

        unchunked = peak_with(chunk_edges=None)
        chunked = peak_with(chunk_edges=20000)

        self.assertLess(
            chunked, unchunked,
            'chunked validation must use less heap than unchunked '
            '(%.1f MB vs %.1f MB)' % (chunked / 1048576.0,
                                      unchunked / 1048576.0))
        self.assertLess(
            chunked / float(edge_bytes), 1.0,
            'chunked peak %.1f MB is still %.2fx the edge arrays -- the '
            'chunking is not actually bounding the temporaries'
            % (chunked / 1048576.0, chunked / float(edge_bytes)))

    def test_chunking_does_not_change_any_verdict(self):
        """分块**不得改变判定**：不同 ``chunk_edges`` 必须给出逐字相同的 problems。

        这是 D3 修复的**正确性前提**——省内存不能以漏检为代价。覆盖合法模型
        与 11 类违约（自环 / 降序 / 平票乱序 / 重复目标 / 非正相似度 /
        相似度>1 / 非正共现 / 共现超上界 / NaN / 公式不符 / clean）。
        """
        cases = _contract_violation_models()
        chunk_sizes = (None, 1, 2, 3, 5, 7, 1000)
        for name, model in sorted(cases.items()):
            verdicts = [M.validate_arrays(model, max_k=100, chunk_edges=c)[0]
                        for c in chunk_sizes]
            first = verdicts[0]
            for c, got in zip(chunk_sizes, verdicts):
                self.assertEqual(
                    got, first,
                    'chunk_edges=%r changed the verdict for case %r:\n'
                    '  unchunked: %r\n  chunked  : %r'
                    % (c, name, first, got))

    def test_chunk_edges_default_is_used_and_tunable(self):
        """**D3 的默认口径**：默认分块阈值存在、可用、且显式关闭时不生效。

        为什么单独测这一条：``chunk_edges`` 的默认值决定了全量模型走分块还是
        不分块。若默认值被误改成 ``None``，全量 2000 万边会重新回到约 1.5 GB
        的峰值路径——而那条路径跑在 3 GiB 上限下。这里把默认值的量级与
        "``None`` / ``<= 0`` 即关闭分块"这两条口径锁死。
        """
        self.assertGreater(M.DEFAULT_VALIDATE_CHUNK_EDGES, 0)
        self.assertLessEqual(M.DEFAULT_VALIDATE_CHUNK_EDGES, 20000000,
                             'the default chunk must stay well below the full '
                             'edge count so full models are actually chunked')

        cases = _contract_violation_models()
        model = cases['clean']
        default_verdict = M.validate_arrays(model, max_k=100)[0]
        # 显式 None 与 <=0 都表示"不分块"，判定必须与默认调用一致
        for off in (None, 0, -1):
            got = M.validate_arrays(model, max_k=100, chunk_edges=off)[0]
            self.assertEqual(got, default_verdict,
                             'chunk_edges=%r must not change a clean model '
                             'verdict' % (off,))

    def test_load_arrays_with_mmap_does_not_heap_copy(self):
        """查过没问题：mmap 读取本身不复制（tracemalloc 视角）。

        注意：这**不**等于"内存不受影响"——memmap 会因为访问而把页计入 RSS，
        而 ``validate_arrays`` 恰好会访问全部数据。本地沙箱无法可信读取 RSS
        （``Get-Process`` 对一个 touch 300 MB 的进程恒报 4.7 MB），所以本文件
        **不做** RSS 断言，只断言"没有堆拷贝"这一条能测的事实。
        """
        work = fixture_dir('mmapheap')
        mdir = os.path.join(work, 'model')
        model = small_model(n_items=400, k=100, seed=7)
        model.save(mdir, metadata_for())

        tracemalloc.start()
        try:
            arrays = M.load_arrays(mdir, mmap_mode='r')
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        for name in M.ARRAY_FILES:
            self.assertIsInstance(arrays[name.replace('.npy', '')], np.memmap)
        total_bytes = sum(os.path.getsize(os.path.join(mdir, n))
                          for n in M.ARRAY_FILES)
        self.assertLess(peak, total_bytes / 4.0,
                        'mmap load allocated %.1f MB for %.1f MB of arrays'
                        % (peak / 1048576.0, total_bytes / 1048576.0))


# ---------------------------------------------------------------------------
# 8. 查过没问题的项（锁死正确行为）
# ---------------------------------------------------------------------------

class ConfirmedSoundTests(unittest.TestCase):
    """审查中**确认没问题**的部分，写成用例防止被误改。

    这一节同样是交付物：审查报告要能说清哪些结论是"查过没问题"，
    而不是只有一堆缺陷。
    """

    def test_load_model_rejects_hash_mismatch(self):
        work = fixture_dir('soundhash')
        mdir = os.path.join(work, 'model')
        small_model().save(mdir, metadata_for())
        release_memmaps()
        target = os.path.join(mdir, 'cooccurrences.npy')
        with open(target, 'rb') as handle:
            raw = bytearray(handle.read())
        raw[-1] ^= 0xFF
        with open(target, 'wb') as handle:
            handle.write(bytes(raw))
        with self.assertRaises(M.ModelIOError):
            M.load_model(mdir, validate=True)

    def test_require_status_is_actually_enforced(self):
        work = fixture_dir('soundstatus')
        mdir = os.path.join(work, 'model')
        small_model().save(mdir, metadata_for())   # assembled_unverified
        with self.assertRaises(M.ModelIOError):
            M.load_model(mdir, validate=True,
                         require_status=M.STATUS_VALIDATED_UNPUBLISHED)

    def test_unknown_status_is_refused(self):
        work = fixture_dir('soundunknown')
        mdir = os.path.join(work, 'model')
        model = small_model()
        meta = metadata_for()
        meta['status'] = 'active'
        with self.assertRaises(ValueError):
            model.save(mdir, meta)

    def test_dtype_contract_is_enforced_on_load(self):
        work = fixture_dir('sounddtype')
        mdir = os.path.join(work, 'model')
        model = small_model()
        model.save(mdir, metadata_for())
        np.save(os.path.join(mdir, 'similarities.npy'),
                np.asarray(model.similarities, dtype=np.float32),
                allow_pickle=False)
        with self.assertRaises(M.ModelIOError):
            M.load_arrays(mdir)

    def test_wrong_length_indptr_is_refused(self):
        work = fixture_dir('soundshape')
        mdir = os.path.join(work, 'model')
        model = small_model()
        model.save(mdir, metadata_for())
        np.save(os.path.join(mdir, 'indptr.npy'),
                np.array([0, 1], dtype=np.int64), allow_pickle=False)
        meta = M.read_json(os.path.join(mdir, M.METADATA_FILE))
        for name in M.ARRAY_FILES:
            meta['files'][name]['sha256'] = M.file_sha256(
                os.path.join(mdir, name))
            meta['files'][name]['bytes'] = os.path.getsize(
                os.path.join(mdir, name))
        M.write_json_atomic(os.path.join(mdir, M.METADATA_FILE), meta)
        with self.assertRaises(M.ModelIOError):
            M.load_model(mdir, validate=True)

    def test_tie_break_rule_is_actually_enforced(self):
        """``(-sim, target_movieId)`` 的平票规则真的被检查（非死代码）。"""
        item_ids = np.array([10, 20, 30, 40], dtype=np.int64)
        # 来源 0 有 3 条平票边（目标 1/2/3），来源 1-3 为空
        indptr = np.array([0, 3, 3, 3, 3], dtype=np.int64)
        n_like = np.array([9, 9, 9, 9], dtype=np.int64)
        n_all = np.array([10, 10, 10, 10], dtype=np.int64)
        coocs = np.array([3, 3, 3], dtype=np.int64)
        sims = np.full(3, 3.0 / 9.0)
        # 平票且按 movieId **降序** → 违约
        bad = M.NeighborModel(item_ids, indptr,
                              np.array([3, 2, 1], dtype=np.int64), sims,
                              coocs, n_like, n_all)
        problems, _stats = M.validate_arrays(bad, max_k=100)
        self.assertTrue(any('ties not broken' in p for p in problems),
                        'got %r' % (problems,))
        # 正确的升序 → 通过
        good = M.NeighborModel(item_ids, indptr,
                               np.array([1, 2, 3], dtype=np.int64), sims,
                               coocs, n_like, n_all)
        problems_ok, _stats_ok = M.validate_arrays(good, max_k=100)
        self.assertEqual(problems_ok, [])

    def test_self_loop_and_zero_like_violations_are_caught(self):
        item_ids = np.array([10, 20, 30], dtype=np.int64)
        n_all = np.array([10, 10, 10], dtype=np.int64)
        # 自环：来源 0 → 目标 0
        loops = M.NeighborModel(item_ids, np.array([0, 1, 1, 1], dtype=np.int64),
                                np.array([0], dtype=np.int64),
                                np.array([0.5]), np.array([3], dtype=np.int64),
                                np.array([9, 9, 9], dtype=np.int64), n_all)
        problems, _stats = M.validate_arrays(loops, max_k=100)
        self.assertTrue(any('self loops' in p for p in problems))
        # 零好评来源却有邻居
        zero = M.NeighborModel(item_ids, np.array([0, 1, 1, 1], dtype=np.int64),
                               np.array([1], dtype=np.int64),
                               np.array([0.5]), np.array([3], dtype=np.int64),
                               np.array([0, 9, 9], dtype=np.int64), n_all)
        problems2, _stats2 = M.validate_arrays(zero, max_k=100)
        self.assertTrue(any('n_like == 0' in p for p in problems2))

    def test_max_k_violation_is_caught(self):
        work = fixture_dir('soundmaxk')
        model = small_model(n_items=10, k=4, seed=6)
        counts = np.diff(np.asarray(model.indptr))
        self.assertTrue(np.any(counts > 1), 'fixture needs multi-edge sources')
        problems, _stats = M.validate_arrays(model, max_k=1)
        self.assertTrue(any('max_k' in p for p in problems))

    def test_verify_full_model_does_not_import_the_construction_module(self):
        """独立重算是**真独立**：零个从构图侧 import 的函数。"""
        source = module_source('verify_full_model.py')
        for forbidden in ('from experiments.itemcf.full_train',
                          'import full_train',
                          'build_block_edges',
                          'build_graph_arrays',
                          'collect_csr_block',
                          'arrays_to_model'):
            self.assertNotIn(forbidden, source,
                             'verify_full_model must not reuse construction '
                             'code (%s)' % forbidden)

    def test_recompute_uses_its_own_similarity_formula(self):
        """相似度公式在核验侧是独立手写的（不是调构图函数）。"""
        import inspect
        body = inspect.getsource(V.recompute_source_neighbors)
        self.assertIn('np.sqrt', body)
        self.assertIn('float(c_ij)', body)
        self.assertNotIn('full_train', body)

    def test_select_sample_sources_is_deterministic(self):
        """查过没问题：抽样可复现，且只抽有邻居的来源。"""
        model = small_model(n_items=60, k=3, seed=8)
        first = V.select_sample_sources(model, seed=42, count=10)
        second = V.select_sample_sources(model, seed=42, count=10)
        self.assertEqual(first, second)
        counts = np.diff(np.asarray(model.indptr))
        for group in ('high_support', 'low_support', 'boundary', 'all'):
            for pos in first[group]:
                self.assertGreater(int(counts[pos]), 0)

    def test_python36_syntax_of_the_reviewed_modules(self):
        """四个模块都不含 py3.6 之后的语法（AST 判定，不看注释）。

        注意：注释/文档字符串里出现 "dataclasses" 这个词**不算**违规 ——
        它们只是声明"本模块不用 dataclasses"。这里检查的是真的没有用。
        """
        import ast
        banned = []
        for name in ('model_io.py', 'verify_full_model.py',
                     'conclusion_writer.py', 'resource_guard.py'):
            path = os.path.join(PROJECT, 'experiments', 'itemcf', name)
            with io.open(path, encoding='utf-8') as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if hasattr(ast, 'NamedExpr') and isinstance(node, ast.NamedExpr):
                    banned.append('%s: walrus at line %d' % (name, node.lineno))
                if isinstance(node, ast.JoinedStr):
                    banned.append('%s: f-string at line %d' % (name, node.lineno))
                if isinstance(node, ast.AnnAssign):
                    banned.append('%s: annotation at line %d'
                                  % (name, node.lineno))
                if isinstance(node, ast.ClassDef) and any(
                        'dataclass' in ast.dump(d) for d in node.decorator_list):
                    banned.append('%s: @dataclass at line %d'
                                  % (name, node.lineno))
        self.assertEqual(banned, [])

    def test_json_default_handles_numpy_scalars(self):
        """查过没问题：``_json_default`` 覆盖 NumPy 标量/数组/布尔。"""
        self.assertEqual(M._json_default(np.int64(7)), 7)
        self.assertEqual(M._json_default(np.float64(1.5)), 1.5)
        self.assertIs(M._json_default(np.bool_(True)), True)
        self.assertEqual(M._json_default(np.array([1, 2])), [1, 2])

    def test_run_id_format_is_enforced(self):
        """查过没问题：通用 ``'run'`` 之类的 ID 会被拒（上轮 bug 的回归）。"""
        with self.assertRaises(ValueError):
            M.verify_id_format('run')
        self.assertEqual(M.verify_id_format(RUN_ID), RUN_ID)
        self.assertEqual(M.compute_model_id(RUN_ID), 'model-' + RUN_ID)


if __name__ == '__main__':
    unittest.main()
