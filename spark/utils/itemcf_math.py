# -*- coding: utf-8 -*-
"""ItemCF 核心数学（纯 NumPy，无 PySpark 依赖，可独立测试）。

本模块是 Spark 训练作业 :mod:`train_itemcf` 的**纯计算层**：共现计数、
相似度公式、每来源 Top-K 截断与 CSR indptr 装配全部在这里，输入输出只用
Python 原生结构 / NumPy 数组。Spark 层只做分布式数据准备（读 CSV、校验、
正样本自连接求共现计数、按来源重分区），逐分区调用本模块完成数学部分。

与权威实现 :mod:`experiments.itemcf.core` 的**逐位一致**性（测试
``tests/test_spark_itemcf_core.py`` 用 core.build_neighbors 对拍验证）：

- ``X[u,i] = 1`` 当且仅当 ``rating >= POSITIVE_THRESHOLD``（默认 4.0）；
- ``n_like(i)``：给 i 好评的用户数；``c_ij``：同时给 i、j 好评的用户数；
- ``cos(i,j) = c_ij / sqrt(n_like(i) * n_like(j))``；
  ``s_ij = (c_ij / (c_ij + beta)) * cos(i,j)``（``beta > 0`` 时），``i != j``；
- ``c_ij <= 0`` 或任一端 ``n_like <= 0`` → 无边（相似度按 0 处理，不落盘）；
- 每来源按 ``(-s_ji, target_movieId)`` 保留最多 ``max_k`` 个**正相似度**邻居，
  图是有向的（不取双向并集），**先收缩再取 top-k**（收缩会改变邻居顺序）。

逐位一致的依据：共现计数是整数（精确）；相似度是同一条 IEEE-754 double
公式的同序求值——``n_j * n_i < 2**53`` 时整数乘积转 double 精确，``sqrt``、
除法、乘法在 NumPy/纯 Python 中都是正确舍入且求值顺序相同，故 ``s_ij``
与 core.py 逐位相等（core 里 ``shrink * cos`` 与本模块 ``cos * shrink``
利用 IEEE 乘法交换律，逐位相等）。差异只可能来自求和顺序，而相似度公式
里没有求和。若上游把 ``n_like`` 换成非整数口径（本模块禁止），才需要容差；
对拍建议容差 1e-12（相对），与 ``verify_full_model.SIMILARITY_TOLERANCE`` 一致。

Python 3.6 兼容：无 f-string / walrus / dataclass。依赖标准库 + NumPy。
"""
import numpy as np

#: 好评二值化阈值默认值（与 core.POSITIVE_THRESHOLD / full_train 一致）。
POSITIVE_THRESHOLD = 4.0

#: 相似度取值上限容差（与 core._SIM_TOL 一致）。
SIM_TOL = 1e-12


def validate_beta(beta):
    """校验收缩系数：有限且 >= 0；返回 float。违例抛 ValueError。"""
    try:
        beta = float(beta)
    except (TypeError, ValueError):
        raise ValueError('beta must be numeric, got %r' % (beta,))
    if not np.isfinite(beta) or beta < 0.0:
        raise ValueError('beta must be finite and >= 0, got %r' % (beta,))
    return beta


def similarity_from_counts(cooc, n_src, n_tgt, beta):
    """向量化相似度：``s = c / sqrt(n_src * n_tgt) * shrink(beta, c)``。

    :param cooc: 共现计数数组（int64 语义，必须 > 0 才会有正相似度）。
    :param n_src: 来源端好评人数（与 ``cooc`` 等长）。
    :param n_tgt: 目标端好评人数（与 ``cooc`` 等长）。
    :param beta: 共现收缩系数（有限且 >= 0；0 表示不收缩）。
    :returns: float64 相似度数组；逐元素与
        ``core.build_neighbors`` 内的 ``shrink * cos`` 逐位相等。

    异常：``beta`` 非法、形状不符、产生非有限或 ``> 1 + 1e-12`` 的相似度 ->
    :class:`ValueError`（实现异常要停，不静默裁剪——与 core 一致）。
    """
    beta = validate_beta(beta)
    cooc = np.asarray(cooc, dtype=np.int64)
    n_src = np.asarray(n_src, dtype=np.int64)
    n_tgt = np.asarray(n_tgt, dtype=np.int64)
    if not (cooc.shape == n_src.shape == n_tgt.shape):
        raise ValueError('cooc / n_src / n_tgt must have the same shape, got '
                         '%r / %r / %r' % (cooc.shape, n_src.shape, n_tgt.shape))

    sims = cooc.astype(np.float64) / np.sqrt(
        n_src.astype(np.float64) * n_tgt.astype(np.float64))
    if beta > 0.0:
        shrink = cooc.astype(np.float64) / (cooc.astype(np.float64) + beta)
        sims = sims * shrink
    if np.any(~np.isfinite(sims)):
        raise ValueError('non-finite similarity produced (check n_src/n_tgt)')
    if np.any(sims > 1.0 + SIM_TOL):
        raise ValueError('similarity above 1 + 1e-12: max=%.17g'
                         % float(np.max(sims)))
    return sims


def rank_and_truncate(sources, targets, sims, coocs, max_k):
    """按 ``(source 升序, -sim, target 升序)`` 排序并做每来源 Top-K 截断。

    :param sources: 来源 movieId（或下标）数组。
    :param targets: 目标 movieId（或下标）数组——平票打破键，因此必须是
        movieId 本身（升序下标与之等价）。
    :param sims: 相似度数组（float64）。
    :param coocs: 共现计数数组（int64）。
    :param max_k: 每来源保留的最大正邻居数；负数抛 ValueError。
    :returns: ``(sources, targets, sims, coocs)`` 四个数组（新数组），
        已按契约排序；只保留 ``sim > 0`` 的边；每来源至多 ``max_k`` 条。

    与 :func:`core.build_neighbors` 的逐来源
    ``sorted(key=(-sim, target_movieId))`` + 截断语义一致；实现走
    ``np.lexsort`` + "组内名次" 技巧（与 ``full_train.build_block_edges``
    第 5/6 步相同），一次排序完成。
    """
    sources = np.asarray(sources, dtype=np.int64)
    targets = np.asarray(targets, dtype=np.int64)
    sims = np.asarray(sims, dtype=np.float64)
    coocs = np.asarray(coocs, dtype=np.int64)
    if not (len(sources) == len(targets) == len(sims) == len(coocs)):
        raise ValueError('edge arrays must have equal length')
    try:
        max_k = int(max_k)
    except (TypeError, ValueError):
        raise ValueError('max_k must be an integer, got %r' % (max_k,))
    if max_k < 0:
        raise ValueError('max_k must be >= 0, got %r' % (max_k,))

    if len(sources) == 0 or max_k == 0:
        empty_i = np.zeros(0, dtype=np.int64)
        return empty_i, empty_i.copy(), np.zeros(0, dtype=np.float64), \
            empty_i.copy()

    # np.lexsort 的最后一个键是主键；升序排 -sim 等价于降序排 sim。
    order = np.lexsort((targets, -sims, sources))
    sources = sources[order]
    targets = targets[order]
    sims = sims[order]
    coocs = coocs[order]

    # 组内名次 = 全局位置 - 该来源首条位置（sources 已升序）。
    changes = np.empty(sources.shape, dtype=bool)
    changes[0] = True
    changes[1:] = sources[1:] != sources[:-1]
    starts = np.flatnonzero(changes)
    group_first = starts[np.cumsum(changes) - 1]
    rank = np.arange(sources.shape[0], dtype=np.int64) - group_first
    keep = (rank < int(max_k)) & (sims > 0.0)
    if not np.all(keep):
        sources = sources[keep]
        targets = targets[keep]
        sims = sims[keep]
        coocs = coocs[keep]
    return sources, targets, sims, coocs


def build_indptr(sources, n_items):
    """来源下标升序的边数组 -> CSR ``indptr``（长度 n_items + 1，空区间保留）。

    :param sources: 每条边的来源下标（必须已按升序；只用于计数，升序性由
        调用方保证——本函数只累计数）。
    :param n_items: 物品总数。
    """
    sources = np.asarray(sources, dtype=np.int64)
    if len(sources):
        if np.any(sources < 0) or np.any(sources >= n_items):
            raise ValueError('source index out of range [0, %d)' % n_items)
        counts = np.bincount(sources, minlength=n_items)
    else:
        counts = np.zeros(n_items, dtype=np.int64)
    indptr = np.zeros(n_items + 1, dtype=np.int64)
    indptr[1:] = np.cumsum(counts.astype(np.int64))
    return indptr


def cooccurrence_counts(positive_lists):
    """每用户好评物品列表 -> 有序对共现计数 ``{(i, j): c}``（``i != j``）。

    与 Spark 层的"正样本自连接（同 userId，a.movieId < b.movieId）→
    无序对计数 → 双向展开"等价：每对 (i, j) 与 (j, i) 的计数相同（对称）。
    与 ``core`` 的 ``X.T @ X`` 口径一致（整数计数，精确）。
    """
    from collections import Counter
    counts = Counter()
    for items in positive_lists:
        uniq = sorted(set(int(i) for i in items))
        for a in uniq:
            for b in uniq:
                if a != b:
                    counts[(a, b)] += 1
    return counts


def edges_from_cooccurrence(pairs, n_like, beta, max_k):
    """共现对 -> 契约排序的边数组（过滤零支持度 + 相似度 + Top-K）。

    :param pairs: 可迭代的 ``(src_movieId, tgt_movieId, c)`` 有序对计数。
    :param n_like: ``{movieId: 好评人数}``；任一端 ``<= 0`` 的对无边
        （与 core 的"零好评支持度 → 相似度 0"一致）。
    :param beta: 收缩系数（见 :func:`similarity_from_counts`）。
    :param max_k: 每来源 Top-K。
    :returns: ``(sources, targets, sims, coocs)`` 四个 NumPy 数组；
        来源按 movieId 升序分组，组内按 ``(-sim, target movieId)`` 升序，
        每来源至多 ``max_k`` 条正相似度边。零邻居来源不出现（由 indptr
        表达为空区间）。
    """
    beta = validate_beta(beta)
    groups = {}
    for src, tgt, c in pairs:
        src = int(src)
        tgt = int(tgt)
        c = int(c)
        if c <= 0 or src == tgt:
            continue
        if n_like.get(src, 0) <= 0 or n_like.get(tgt, 0) <= 0:
            continue
        groups.setdefault(src, ([], []))
        groups[src][0].append(tgt)
        groups[src][1].append(c)

    out_sources, out_targets, out_sims, out_coocs = [], [], [], []
    for src in sorted(groups):
        tgts, cs = groups[src]
        tgts = np.asarray(tgts, dtype=np.int64)
        cs = np.asarray(cs, dtype=np.int64)
        n_src = np.full(tgts.shape, int(n_like[src]), dtype=np.int64)
        n_tgt = np.fromiter((int(n_like.get(int(t), 0)) for t in tgts),
                            dtype=np.int64, count=tgts.shape[0])
        sims = similarity_from_counts(cs, n_src, n_tgt, beta)
        src_arr = np.full(tgts.shape, src, dtype=np.int64)
        s, t, sm, c = rank_and_truncate(src_arr, tgts, sims, cs, max_k)
        out_sources.append(s)
        out_targets.append(t)
        out_sims.append(sm)
        out_coocs.append(c)

    if not out_sources:
        empty_i = np.zeros(0, dtype=np.int64)
        return empty_i, empty_i.copy(), np.zeros(0, dtype=np.float64), \
            empty_i.copy()
    return (np.concatenate(out_sources), np.concatenate(out_targets),
            np.concatenate(out_sims), np.concatenate(out_coocs))


def build_neighbors_from_rows(rows, beta, max_k,
                              positive_threshold=POSITIVE_THRESHOLD):
    """评分行 -> 邻居图 dict，形状与 :func:`core.build_neighbors` 返回值一致。

    :param rows: ``(userId, movieId, rating, timestamp)`` 四元组可迭代
        （只应传入训练段；重复 ``(userId, movieId)`` 的处理交给调用方，
        本函数按 set 收敛）。
    :returns: ``{(source_movieId, target_movieId):
                 (target_movieId, sim, c_ij, n_like_j, n_like_i)}``；
        与 ``core.build_neighbors(core.build_positive_matrix(rows)[0], ...)``
        逐位一致（浮点值逐位相等，见模块 docstring）。
    """
    beta = validate_beta(beta)
    liked = {}
    n_like = {}
    for row in rows:
        user_id = int(row[0])
        item_id = int(row[1])
        rating = float(row[2])
        items = liked.setdefault(user_id, set())
        if rating >= positive_threshold:
            items.add(item_id)
            n_like[item_id] = n_like.get(item_id, 0) + 1
    counts = cooccurrence_counts(liked.values())
    sources, targets, sims, coocs = edges_from_cooccurrence(
        ((s, t, c) for (s, t), c in counts.items()), n_like, beta, max_k)
    graph = {}
    for s, t, sim, c in zip(sources.tolist(), targets.tolist(),
                            sims.tolist(), coocs.tolist()):
        graph[(s, t)] = (t, sim, c, int(n_like[s]), int(n_like[t]))
    return graph
