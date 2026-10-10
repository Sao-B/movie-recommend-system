"""ItemCF 核心纯函数：二值好评余弦相似度（含共现收缩）与用户打分。

严格实现 docs/superpowers/plans/2026-10-03-itemcf-test-plan.md 第 3、5 节：

- ``X_ui = 1`` 当且仅当训练段评分 ``rating >= 4.0``，否则 0；低于阈值的
  评分不进相似度矩阵，但调用方仍应把对应电影放入 ``Seen_u``（本模块的
  ``score_user`` 通过 ``seen`` 参数接收该排除集合）。
- ``n_like(i)``：训练段给 i 好评的用户数；``c_ij``：同时给 i、j 好评的用户数。
- ``cos(i,j) = c_ij / sqrt(n_like(i) * n_like(j))``；
  ``s_ij = (c_ij / (c_ij + beta)) * cos(i,j)``，``i != j``；``c_ij == 0`` 直接 0。
- 邻居方向：对每个**来源** j 按 ``(-s_ji, target_movie_id)`` 保留最多
  ``max_k`` 个正相似度邻居，图是有向的（不取双向并集）。
- 打分：``score(u,i) = sum_{j in H_u, i in N_k(j)} s_ji``，裸加权和，
  不归一化；候选排除 ``Seen_u``；Top-N 按 ``(-score, movieId)`` 取前 ``top_n``。

异常契约（都在 docstring 中声明，不静默裁剪）：

- 训练行重复 ``(userId, movieId)`` -> :class:`ValueError`。
- ``beta`` 非有限或为负 -> :class:`ValueError`。
- 任何非有限、负值、或 ``> 1 + 1e-12`` 的相似度 -> :class:`ValueError`
  （视为实现异常，停止排查）。
- 空训练集返回空矩阵/空映射；空历史或无证据候选返回空推荐列表。

Python 3.6 兼容：不使用 dataclasses / ``from __future__ import annotations``
等 3.7+ 特性；数值只用 NumPy / SciPy。
"""
import numpy as np
from scipy import sparse

#: 正向评分二值化阈值（固定，见计划书 3.1 节）。
POSITIVE_THRESHOLD = 4.0

#: 相似度取值上限容差（计划书 3.2 节第 4 条）。
_SIM_TOL = 1e-12


def _checked_similarity(value, source_id, target_id):
    """校验单个相似度值；越界/非有限抛 ValueError（不静默裁剪）。"""
    v = float(value)
    if not np.isfinite(v):
        raise ValueError('non-finite similarity s(%r, %r) = %r'
                         % (source_id, target_id, value))
    if v < 0.0:
        raise ValueError('negative similarity s(%r, %r) = %r'
                         % (source_id, target_id, value))
    if v > 1.0 + _SIM_TOL:
        raise ValueError('similarity out of range s(%r, %r) = %r'
                         % (source_id, target_id, value))
    return v


def _as_int(value, name):
    try:
        out = int(value)
    except (TypeError, ValueError):
        raise ValueError('%s must be an integer id, got %r' % (name, value))
    return out


def build_positive_matrix(train):
    """把训练评分行构造成稀疏 0/1 好评矩阵。

    :param train: ``(userId, movieId, rating, timestamp)`` 四元组列表
        （与 :func:`experiments.pilot.data.temporal_split_three_way` 的行格式
        一致）。只应传入训练段；重复 ``(userId, movieId)`` 直接拒绝。
    :returns: ``(X, item_ids, user_ids)``：

        - ``X``：``scipy.sparse.csr_matrix``，形状 ``(len(user_ids), len(item_ids))``，
          dtype ``int64``（禁止 uint8/bool 溢出或布尔化），``X[u, i] = 1`` 当
          该用户对该物品评分 ``>= POSITIVE_THRESHOLD``。
        - ``item_ids``：训练段出现过的全部 movieId（含零好评物品），数值升序。
        - ``user_ids``：训练段出现过的全部 userId，数值升序。

    空输入返回 ``(空 CSR (0,0), [], [])``。
    """
    rows_seen = set()
    user_ids, item_ids = set(), set()
    records = []
    for idx, row in enumerate(train):
        try:
            user_id, item_id, rating, _ts = row
        except (TypeError, ValueError):
            raise ValueError('train[%d] must be (userId, movieId, rating, timestamp)'
                             % idx)
        user_id = _as_int(user_id, 'train[%d].userId' % idx)
        item_id = _as_int(item_id, 'train[%d].movieId' % idx)
        try:
            rating = float(rating)
        except (TypeError, ValueError):
            raise ValueError('train[%d].rating must be numeric, got %r'
                             % (idx, rating))
        key = (user_id, item_id)
        if key in rows_seen:
            raise ValueError('duplicate (userId, movieId) pair: %r' % (key,))
        rows_seen.add(key)
        user_ids.add(user_id)
        item_ids.add(item_id)
        if rating >= POSITIVE_THRESHOLD:
            records.append((user_id, item_id))

    user_list = sorted(user_ids)
    item_list = sorted(item_ids)
    user_index = {uid: pos for pos, uid in enumerate(user_list)}
    item_index = {iid: pos for pos, iid in enumerate(item_list)}

    n_rows, n_cols = len(user_list), len(item_list)
    if not records:
        X = sparse.csr_matrix((n_rows, n_cols), dtype=np.int64)
        return X, item_list, user_list

    data = np.ones(len(records), dtype=np.int64)
    row_idx = np.fromiter((user_index[u] for u, _ in records),
                          dtype=np.int64, count=len(records))
    col_idx = np.fromiter((item_index[i] for _, i in records),
                          dtype=np.int64, count=len(records))
    X = sparse.csr_matrix((data, (row_idx, col_idx)),
                          shape=(n_rows, n_cols), dtype=np.int64)
    return X, item_list, user_list


def build_neighbors(X, item_ids, beta, max_k, block_size=128):
    """分块稀疏计算物品相似度并截断成有向 top-k 邻居图。

    :param X: :func:`build_positive_matrix` 返回的稀疏好评矩阵
        （用户 x 物品，整数计数）。
    :param item_ids: 与 ``X`` 列顺序一致的 movieId 列表（数值升序，含零好评）。
    :param beta: 共现收缩系数，有限且 ``>= 0``；``beta=0`` 时收缩系数为 1。
    :param max_k: 每个来源物品保留的最大正邻居数。
    :param block_size: 逐块列数，默认 128；只影响计算方式，不影响结果。
    :returns: ``dict``，键为 ``(source_j, target_i)``（movieId，``j != i``），
        值为 ``(target_i, s_ji, c_ji)``；每条边还附带解释所需的
        ``n_like(j)``、``n_like(i)``。键按来源 movieId 升序、目标 movieId 升序
        插入，保证遍历顺序稳定。每个来源 j 最多 ``max_k`` 个正相似度邻居，
        按 ``(-s_ji, target_i)`` 排序后截断（相似度降序，平票取目标 movieId 升序）。

    算法：对每个列块先算出块内全部非零共现 ``X[:, block].T @ X``，
    **先按指定 beta 收缩得到相似度，再在收缩后的相似度上取 top-k**
    （不能先按原始余弦取 top-k 再收缩，收缩会改变邻居顺序）。``X`` 内部用
    ``int64`` 计数防溢出；只保留稀疏结果，不构造全物品 x 全物品稠密矩阵。

    异常：``beta`` 非有限或为负、``block_size < 1``、``max_k < 0``、
    ``X`` 与 ``item_ids`` 形状不符 -> :class:`ValueError`；任何越界/非有限
    相似度 -> :class:`ValueError`（见模块 docstring）。
    """
    try:
        beta = float(beta)
    except (TypeError, ValueError):
        raise ValueError('beta must be numeric, got %r' % (beta,))
    if not np.isfinite(beta) or beta < 0.0:
        raise ValueError('beta must be finite and >= 0, got %r' % (beta,))
    try:
        max_k = int(max_k)
        block_size = int(block_size)
    except (TypeError, ValueError):
        raise ValueError('max_k and block_size must be integers')
    if max_k < 0:
        raise ValueError('max_k must be >= 0, got %r' % (max_k,))
    if block_size < 1:
        raise ValueError('block_size must be >= 1, got %r' % (block_size,))
    if sparse.issparse(X):
        if X.shape[1] != len(item_ids):
            raise ValueError('X has %d columns but item_ids has %d entries'
                             % (X.shape[1], len(item_ids)))
        X = X.tocsr().astype(np.int64)
    else:
        X = sparse.csr_matrix(np.asarray(X, dtype=np.int64))
        if X.shape[1] != len(item_ids):
            raise ValueError('X has %d columns but item_ids has %d entries'
                             % (X.shape[1], len(item_ids)))

    n_items = len(item_ids)
    if n_items == 0 or max_k == 0:
        return {}

    # n_like(i)：训练段给 i 好评的用户数（相似度归一化与 LikePop 共用口径）。
    n_like = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)

    neighbors = {}
    for start in range(0, n_items, block_size):
        stop = min(start + block_size, n_items)
        block = X[:, start:stop]
        coo = (block.T @ X).tocoo()  # 行=块内来源 j，列=目标 i，值=c_ji
        # 按 (来源, 目标) 收集候选，块内全部非零共现先算收缩相似度。
        candidates = {}
        for r, c, val in zip(coo.row, coo.col, coo.data):
            j_pos = start + int(r)
            i_pos = int(c)
            if j_pos == i_pos:
                continue  # 对角线设为 0
            c_ij = int(val)
            if c_ij <= 0:
                continue
            n_j = int(n_like[j_pos])
            n_i = int(n_like[i_pos])
            if n_j <= 0 or n_i <= 0:
                # 任一好评人数为 0 时相似度为 0（计划书 3.2 节规则 1）。
                continue
            cos = c_ij / float(np.sqrt(n_j * n_i))
            shrink = c_ij / float(c_ij + beta) if beta > 0.0 else 1.0
            sim = shrink * cos
            _checked_similarity(sim, item_ids[j_pos], item_ids[i_pos])
            candidates.setdefault(j_pos, []).append((i_pos, sim, c_ij, n_j, n_i))

        for j_pos in sorted(candidates):
            source_id = item_ids[j_pos]
            # 按 (-s_ji, target_movie_id) 排序，再截断 max_k 个正相似度邻居。
            ordered = sorted(candidates[j_pos],
                             key=lambda e: (-e[1], item_ids[e[0]]))
            kept = 0
            for i_pos, sim, c_ij, n_j, n_i in ordered:
                if sim <= 0.0:
                    break
                if kept >= max_k:
                    break
                key = (source_id, item_ids[i_pos])
                neighbors[key] = (item_ids[i_pos], sim, c_ij, n_j, n_i)
                kept += 1
    return neighbors


def score_user(history, neighbors, seen, top_n):
    """给单个用户打分并返回 Top-N 推荐（含证据字段）。

    :param history: 该用户训练好评的 movieId 集合 ``H_u``（``rating >= 4.0``）。
    :param neighbors: :func:`build_neighbors` 返回的有向邻居图。
    :param seen: 该用户训练段评过的全部 movieId（含低分），用于排除候选。
    :param top_n: 返回条数上限。
    :returns: 有序推荐列表，每条是 dict：

        - ``movie_id``：推荐物品 movieId。
        - ``score``：裸加权和 ``sum_{j in H_u} s_ji``（排序分数，不是预测评分）。
        - ``contributions``：``[(source_j, s_ji, c_ji, n_like_j, n_like_i), ...]``
          按来源 movieId 升序（浮点累加顺序固定，避免并列排序抖动）。
        - ``contrib_sum``：贡献总和（应等于 ``score``，容差 1e-10）。
        - ``evidence_count``：证据邻居数（贡献来源个数）。
        - ``top_source_movie_id``：最大贡献来源 movieId；贡献并列取 movieId 最小者。
        - ``top_source_c``：最大贡献边的 ``c_ji``。
        - ``top_source_n_like``：最大贡献来源的 ``n_like(j)``。
        - ``n_like``：候选物品的 ``n_like(i)``。

    排序按 ``(-score, movie_id)``；候选全集是邻居图可达物品减去 ``seen``；
    ``score > 0`` 才进入候选 ``E_u``；``|E_u| < top_n`` 时返回不足 ``top_n``
    条（不补零分/热门）；``H_u`` 为空或 ``E_u`` 为空返回空列表。

    异常：``top_n`` 不是非负整数 -> :class:`ValueError`。
    """
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        raise ValueError('top_n must be an integer, got %r' % (top_n,))
    if top_n < 0:
        raise ValueError('top_n must be >= 0, got %r' % (top_n,))

    history_ids = {_as_int(h, 'history') for h in history}
    seen_ids = {_as_int(s, 'seen') for s in seen}
    if not history_ids or top_n == 0:
        return []

    # 按来源 movieId 升序累加贡献，保证浮点求和顺序稳定。
    contrib_by_target = {}
    for source_id in sorted(history_ids):
        for key, edge in neighbors.items():
            if key[0] != source_id:
                continue
            target_id, sim, c_ij, n_j, n_i = edge
            if target_id in seen_ids or target_id in history_ids:
                continue
            contrib_by_target.setdefault(target_id, []).append(
                (source_id, sim, c_ij, n_j, n_i))

    scored = []
    for target_id in sorted(contrib_by_target):
        contribs = sorted(contrib_by_target[target_id])  # 来源升序
        total = 0.0
        for _src, sim, _c, _nj, _ni in contribs:
            total += sim
        if not (total > 0.0):
            continue
        # 最大贡献来源：贡献并列取 movieId 最小者（contribs 已按来源升序，
        # 严格大于比较即可保序保留先出现的最小 movieId）。
        top_src, top_sim = None, None
        for src, sim, _c, _nj, _ni in contribs:
            if top_sim is None or sim > top_sim:
                top_src, top_sim = src, sim
        top_edge = next(e for e in contribs if e[0] == top_src)
        scored.append({
            'movie_id': target_id,
            'score': total,
            'contributions': contribs,
            'contrib_sum': total,
            'evidence_count': len(contribs),
            'top_source_movie_id': top_src,
            'top_source_c': top_edge[2],
            'top_source_n_like': top_edge[3],
            'n_like': top_edge[4],
        })

    scored.sort(key=lambda r: (-r['score'], r['movie_id']))
    return scored[:top_n]
