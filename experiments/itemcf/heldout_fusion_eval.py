# -*- coding: utf-8 -*-
"""融合打分消融评估：并联召回（ItemCF 证据集 ∪ ALS Top-500）+ 归一化融合。

计划书：``docs/superpowers/plans/2026-10-06-fusion-ablation.md``。在同一 holdout
协议（冻结 2000 人名单、时间切分 80/20、NDCG@20 / Recall@20）下对比五种打分变体：

=================  ==========================================  ==============
变体                打分公式                                     自由参数
=================  ==========================================  ==============
V0 raw-sum          ``Σ s_ji``（冻结口径复现）                    无
V1 mean             ``Σ s_ji / count``                           无
V2 smoothed         ``Σ s_ji / (count + k)``                     k ∈ {0,2,5,10}
V3                  归一 V2 + 归一 ALS（权重重归一）               k + (w_i, w_a)
V4                  V3 + 类型代理信号（权重重归一）                k + (w_i, w_a, w_g)
V6 label-freq       V4 但类型路改频率权重x近期均分（权重固定）        无（EDA 定死）
V7 als-neg          V6 + ALS 负反馈罚项（中心化余弦传播）            无（lambda/cap 定死）
V8 pop-rerank       V7 + 热门降权重排（=部署版 V5 全量）             无（alpha 定死）
=================  ==========================================  ==============

协议要点
========

1. 冻结名单与切分校验**逐字复用** ``heldout_als_eval``：``verify_frozen_users``
   对拍 ``users.json`` 指纹；``rebuild_split_and_verify`` 重建切分后与冻结的
   ``split_holdout.csv`` 字节对拍。
2. 候选并集：R1 = 训练段好评（>=4.0）的邻居表并集（部署同款查表，见
   ``backend/app/itemcf_service.ItemCFGraph``：mmap 数组 + ``item_pos`` +
   ``indptr`` 切片；相似度按来源 movieId 升序累加保证浮点确定）；R2 = holdout
   协议 ALS 因子（``--als-factors-jsonl``，**绝不连 MySQL**）折入向量点积 Top-500。
3. 信号逐用户候选内 max 归一到 [0,1]；**缺失信号退出加权**：
   ``fused = Σ(可用信号·权重) / Σ(可用信号权重)``——缺某路信号时绝不能按 0 分计入。
4. 类型代理：``user_genres`` = 训练段好评电影的类型集合（``--movies-genres-csv``，
   ``|`` 分隔）；``match = |genres(i) ∩ user_genres| / |user_genres|``；
   电影无类型标注或用户无好评类型 → 该信号**缺失**（不是 0 分）。
5. tune/test 分离（防泄漏）：评测用户按 ``sha256("fusion-ablation-split-v1:" +
   userId)`` 十六进制首字符（'0'-'7' → tune，'8'-'f' → test）对半。V2/V3/V4 的
   k 与权重 simplex（步长 0.1）**只在 tune 半**按 NDCG@20 均值最大选参；test 半
   出全部变体的正式数字。V0/V1 无自由参数，两半都报告。
6. 网格搜索性能：信号矩阵逐用户预计算成 numpy 向量，权重组合用矩阵形式
   ``fused = (S @ W) / (M @ W)`` 批量算，Top-20 用 ``np.argpartition`` 取候选
   后只对候选内小集合精排——不做 组合×用户 的纯 Python 双重全排序。

Python 3.6 兼容：无 f-string / dataclasses / walrus / ``from __future__
import annotations``。
"""
import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import defaultdict

import numpy as np

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
BACKEND_DIR = os.path.join(PROJECT_ROOT, 'backend')
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

from experiments.itemcf.heldout_eval import (  # noqa: E402
    CorrectnessError, HOLDOUT_CSV, POSITIVE_THRESHOLD, TOP_N, TRAIN_CSV,
    file_sha256, paired_bootstrap, per_user_metrics, user_list_sha256,
    write_csv_atomic, write_json_atomic,
)
from experiments.itemcf.heldout_als_eval import (  # noqa: E402
    load_compare_per_user, load_factors_from_jsonl, rebuild_split_and_verify,
    solve_user_vector_explicit, verify_frozen_users,
)
from app.itemcf_service import ItemCFGraph  # noqa: E402
from app.rating_weights import freq_weight, label_signal, rating_weight  # noqa: E402
from experiments.pilot.metrics import ndcg_at_k  # noqa: E402

#: tune/test 分离 salt（计划书 §5，先冻结后执行）。
FUSION_SPLIT_SALT = 'fusion-ablation-split-v1:'
#: R2 召回规模：ALS 折入点积 Top-500。
ALS_TOP_K = 500
#: V2/V3/V4 的 k 网格。
TUNE_TEST_KS = (0, 2, 5, 10)
#: 权重 simplex 步长（w_i + w_a + w_g = 1 的全部 0.1 组合）。
WEIGHT_STEP = 0.1
#: 第五轮 ItemCF holdout 基线 NDCG@20（V0 复现检查的参照值）。
FIFTH_ROUND_ITEMCF_NDCG20 = 0.132076
#: V0 复现容差。
REPRODUCTION_TOLERANCE = 0.005
#: 覆盖率导向选择的"NDCG 统计等价"容差（与最优组合差值不超过该值视为等价）。
COVERAGE_EQUIV_TOLERANCE = 0.003

VARIANT_NAMES = (
    'V0_raw_sum', 'V1_mean', 'V2_smoothed',
    'V3_itemcf_als', 'V4_itemcf_als_genre',
)
#: V6/V7/V8 的固定权重（与部署基线同起点；本次不进网格，权重重搜另跑）
V5_FIXED_WEIGHTS = (0.4, 0.5, 0.1)
#: V7 ALS 负反馈罚项系数与单项上限（EDA 2026-10-10 定稿）
V5_ALS_NEG_LAMBDA = 0.2
V5_ALS_NEG_CAP = 0.5
#: V6 近期均分窗口（最近 N 条同标签评分）
V5_RECENT_WINDOW = 10
#: V8 热门降权指数（pop/median 的幂；scale 钳 1 = 只罚不奖）
V5_POP_ALPHA = 0.5


# ---------------------------------------------------------------------------
# 输入装载与 tune/test 分离
# ---------------------------------------------------------------------------

def load_genres_csv(path):
    """读两列 ``movie_id,genres`` 的本地 CSV（``|`` 分隔）；返回 ``{movieId: frozenset}``。

    首列不是整数的行（表头等）跳过；genres 空串记为**空集**——调用方把空集
    当作"无类型标注"（信号缺失），与"有标注但匹配 0"严格区分。
    """
    genres = {}
    with open(path, newline='', encoding='utf-8') as handle:
        for record in csv.reader(handle):
            if not record:
                continue
            try:
                movie_id = int(record[0].strip())
            except ValueError:
                continue
            raw = record[1].strip() if len(record) > 1 else ''
            genres[movie_id] = frozenset(v for v in raw.split('|') if v)
    return genres


def tune_test_half(user_id, salt=FUSION_SPLIT_SALT):
    """确定性二分：``sha256(salt + str(userId))`` 首字符 '0'-'7' → tune，否则 test。"""
    digest = hashlib.sha256(
        ('%s%d' % (salt, int(user_id))).encode('utf-8')).hexdigest()
    return 'tune' if digest[0] in '01234567' else 'test'


def simplex_grid(n_members, step=WEIGHT_STEP):
    """权重 simplex 上步长为 ``step`` 的全部组合（和恒为 1），升序字典序。"""
    steps = int(round(1.0 / step))
    if steps <= 0:
        raise ValueError('weight step must be positive')
    combos = []

    def walk(prefix, remaining, slots):
        if slots == 1:
            combos.append(tuple(prefix + [float(remaining) / steps]))
            return
        for take in range(remaining + 1):
            walk(prefix + [float(take) / steps], remaining - take, slots - 1)

    walk([], steps, int(n_members))
    return combos


# ---------------------------------------------------------------------------
# 逐用户数据收集（照搬 heldout_als_eval.collect_user_segments 的流式接线）
# ---------------------------------------------------------------------------

def collect_fusion_segments(train_path, holdout_path, eval_users, log=None):
    """流式收集评测用户的训练段评分与留出段相关集。

    与 ``heldout_als_eval.collect_user_segments`` 的唯一差别：训练段**不**按 ALS
    因子表过滤——R1 邻居证据与类型画像都必须看到因子表之外的电影（因子过滤
    只影响 fold-in，那一步在 ``build_user_record`` 里做）。

    :returns: ``(train_by_user, relevant_by_user)``；训练段每用户
        ``{'rated': [(movieId, rating), ...], 'positive': [movieId, ...]}``；
        相关集 = 留出段 ``rating >= 4.0``。
    """
    eval_set = set(int(u) for u in eval_users)
    train_by_user = {}
    support_by_movie = {}
    pop_counts = {}   # V8 热门降权用：训练段全量物品评分数（不限评测用户）
    with open(train_path, newline='') as handle:
        for record in csv.reader(handle):
            user = int(record[0])
            movie_id = int(record[1])
            pop_counts[movie_id] = pop_counts.get(movie_id, 0) + 1
            if user not in eval_set:
                continue
            rating = float(record[2])
            entry = train_by_user.setdefault(
                user, {'rated': [], 'positive': []})
            entry['rated'].append((movie_id, rating))
            if rating >= POSITIVE_THRESHOLD:
                entry['positive'].append(movie_id)
                support_by_movie[movie_id] = support_by_movie.get(movie_id, 0) + 1
    relevant_by_user = {}
    with open(holdout_path, newline='') as handle:
        for record in csv.reader(handle):
            user = int(record[0])
            if user not in eval_set:
                continue
            if float(record[2]) >= POSITIVE_THRESHOLD:
                relevant_by_user.setdefault(user, set()).add(int(record[1]))
    if log:
        log('eval users with train ratings: %d; with holdout positives: %d; '
            'train movies: %d' % (len(train_by_user), len(relevant_by_user),
                                  len(pop_counts)))
    return train_by_user, relevant_by_user, support_by_movie, pop_counts


def build_r1_evidence(graph, positives, seen):
    """R1 证据集：训练段好评电影的邻居表并集（部署同款查表打分）。

    贡献按来源 movieId 升序累加（``item_ids`` 升序 + history 排序，与
    ``itemcf_service.recommendations_for`` 一致，浮点求和顺序确定）。

    :returns: ``(ids, sums, counts)``——ids 升序 int64 数组；sums = 相似度和；
        counts = 贡献来源数（V1/V2 的分母）。
    """
    sums = {}
    counts = {}
    for source_id in sorted(int(m) for m in positives):
        for target_id, sim in graph.neighbors_of(source_id):
            if target_id in seen:
                continue
            value = float(sim)
            if target_id in sums:
                sums[target_id] += value
                counts[target_id] += 1
            else:
                sums[target_id] = value
                counts[target_id] = 1
    ids = np.asarray(sorted(sums), dtype=np.int64)
    total = np.asarray([sums[int(i)] for i in ids], dtype=np.float64)
    n_sources = np.asarray([counts[int(i)] for i in ids], dtype=np.float64)
    return ids, total, n_sources


def als_top_k_candidates(user_vector, factor_movie_ids, factor_matrix, seen,
                         top_k=ALS_TOP_K, allowed=None):
    """折入向量点积 Top-K（排除已看；``(-score, movieId)`` 序）。

    ``np.argpartition`` 取候选切片后只对切片精排，避免全量 8.4 万项排序。
    ``allowed``：候选 movieId 白名单（None = 不限制）。ALS 路接支持度门槛时
    传高支持度电影集合——未训练的冷门因子会产生极端点积，不过门则 Top-K
    被垃圾片占满，信号退化为随机排序（V3 调参收敛到 w_als=0 的根源）。
    """
    scores = factor_matrix @ user_vector
    if allowed is not None:
        allowed_arr = np.isin(factor_movie_ids, np.asarray(sorted(allowed),
                                                            dtype=np.int64))
        scores = np.where(allowed_arr, scores, -np.inf)
    n = factor_movie_ids.shape[0]
    need = min(int(n), int(top_k) + len(seen))
    if need < n:
        part = np.argpartition(-scores, need - 1)[:need]
    else:
        part = np.arange(n)
    order = part[np.lexsort((factor_movie_ids[part], -scores[part]))]
    picked = []
    for pos in order.tolist():
        movie_id = int(factor_movie_ids[pos])
        if movie_id in seen:
            continue
        if not np.isfinite(scores[pos]):
            continue
        picked.append((movie_id, float(scores[pos])))
        if len(picked) >= int(top_k):
            break
    return picked


def build_user_record(user, segments, relevant, graph, genre_map, factor_pos,
                      factor_movie_ids, factor_matrix, reg_param,
                      als_top_k=ALS_TOP_K, support_by_movie=None,
                      als_min_support=0, pop_counts=None, matcn=None,
                      recent_window=V5_RECENT_WINDOW):
    """单用户的候选并集与三路信号（对齐到升序 union 数组）。

    - R1：邻居表并集（相似度和 + 来源数）；
    - R2：ALS 折入 Top-K（不在因子表的候选 ALS 信号 = NaN = 缺失）；
    - 类型：``match = |genres(i) ∩ user_genres| / |user_genres|``；候选无类型
      标注或用户无好评类型 → NaN = 缺失。
    """
    rated = segments.get('rated') or []
    positive = sorted(int(m) for m in (segments.get('positive') or []))
    seen = set(int(m) for m, _r in rated)
    r1_ids, r1_sum, r1_cnt = build_r1_evidence(graph, positive, seen)

    vector = None
    rows = [factor_pos[m] for m, _r in rated if m in factor_pos]
    if rows:
        values = [r for m, r in rated if m in factor_pos]
        vector = solve_user_vector_explicit(
            factor_matrix[rows], np.asarray(values, dtype=np.float64),
            reg_param)
    als_ids = np.zeros(0, dtype=np.int64)
    als_dot = np.zeros(0, dtype=np.float64)
    allowed = None
    if als_min_support > 0 and support_by_movie is not None:
        allowed = set(m for m, n in support_by_movie.items()
                      if n >= als_min_support)
    if vector is not None:
        picked = als_top_k_candidates(vector, factor_movie_ids, factor_matrix,
                                      seen, als_top_k, allowed=allowed)
        if picked:
            als_ids = np.asarray([p[0] for p in picked], dtype=np.int64)
            als_dot = np.asarray([p[1] for p in picked], dtype=np.float64)

    # V7：折入只用 w(r)>=0 的评分——差评进 NNLS 会把整个用户向量拽离该类型
    # （部署端 e 用户实测：Top-20 科幻清零、类型跑飞）。负反馈交给罚项。
    als_pen_ids = np.zeros(0, dtype=np.int64)
    als_pen_dot = np.zeros(0, dtype=np.float64)
    if matcn is not None:
        pairs_p = [(m, r) for m, r in rated
                   if rating_weight(r) >= 0 and m in factor_pos]
        if pairs_p:
            rows_p = [factor_pos[m] for m, _r in pairs_p]
            values_p = [r for _m, r in pairs_p]
            vector_pen = solve_user_vector_explicit(
                factor_matrix[rows_p], np.asarray(values_p, dtype=np.float64),
                reg_param)
            picked_pen = als_top_k_candidates(
                vector_pen, factor_movie_ids, factor_matrix, seen,
                als_top_k, allowed=allowed)
            if picked_pen:
                als_pen_ids = np.asarray([p[0] for p in picked_pen],
                                         dtype=np.int64)
                als_pen_dot = np.asarray([p[1] for p in picked_pen],
                                         dtype=np.float64)

    union_ids = np.union1d(np.union1d(r1_ids, als_ids), als_pen_ids)
    n = union_ids.shape[0]
    u_sum = np.zeros(n, dtype=np.float64)
    u_cnt = np.zeros(n, dtype=np.float64)
    u_als = np.full(n, np.nan, dtype=np.float64)
    u_genre = np.full(n, np.nan, dtype=np.float64)
    if r1_ids.shape[0]:
        at = np.searchsorted(union_ids, r1_ids)
        u_sum[at] = r1_sum
        u_cnt[at] = r1_cnt
    if als_ids.shape[0]:
        at = np.searchsorted(union_ids, als_ids)
        u_als[at] = als_dot

    user_genres = set()
    for movie_id in positive:
        user_genres |= set(genre_map.get(movie_id) or ())
    if user_genres:
        profile = frozenset(user_genres)
        for pos, movie_id in enumerate(union_ids.tolist()):
            values = genre_map.get(movie_id)
            if values:
                u_genre[pos] = len(values & profile) / float(len(profile))
    # ---- V5 字段（V6/V7/V8；upstream 不给 matcn/pop_counts 时安全降级）----
    # 声明标签：训练段 >=4 分 genre 频次 Top3；频率 = 全部评分计数；
    # 分数信号 = 近 recent_window 条同标签评分均分（rated 列表按训练文件
    # 时间序追加，尾部即最近）。
    pos_cnt = {}
    watch_cnt = {}
    ratings_by_genre = defaultdict(list)
    for movie_id, rating in rated:
        for g in (genre_map.get(movie_id) or ()):
            watch_cnt[g] = watch_cnt.get(g, 0) + 1
            ratings_by_genre[g].append(float(rating))
            if rating >= POSITIVE_THRESHOLD:
                pos_cnt[g] = pos_cnt.get(g, 0) + 1
    declared = [g for g, _c in sorted(pos_cnt.items(),
                                      key=lambda kv: (-kv[1], kv[0]))[:3]]
    v5_labels = {}
    for g in declared:
        rs = ratings_by_genre[g]
        if len(rs) < 2:
            continue
        recent = rs[-int(recent_window):]
        v5_labels[g] = (freq_weight(watch_cnt[g]),
                        label_signal(sum(recent) / len(recent)))
    u_label = np.full(n, np.nan, dtype=np.float64)
    for pos, movie_id in enumerate(union_ids.tolist()):
        matched = [v5_labels[g] for g in (genre_map.get(movie_id) or ())
                   if g in v5_labels]
        if matched:
            u_label[pos] = max(fw * sig for fw, sig in matched)

    # V7 的 ALS 列：正评分折入 + 负反馈罚项（中心化余弦传播，单项上限钳制）
    u_als_pen = np.full(n, np.nan, dtype=np.float64)
    if als_pen_ids.shape[0]:
        at_pen = np.searchsorted(union_ids, als_pen_ids)
        dots = als_pen_dot.copy()
        neg_pairs = [(m, rating_weight(r)) for m, r in rated
                     if rating_weight(r) < 0 and m in factor_pos]
        if neg_pairs:
            neg_rows = np.asarray([factor_pos[m] for m, _w in neg_pairs],
                                  dtype=np.int64)
            neg_w = np.asarray([-w for _m, w in neg_pairs],
                               dtype=np.float64)
            cap = V5_ALS_NEG_CAP * float(neg_w.sum())
            fac_rows = np.asarray([factor_pos[int(m)]
                                   for m in als_pen_ids.tolist()],
                                  dtype=np.int64)
            sim_mat = matcn[fac_rows] @ matcn[neg_rows].T
            pen = V5_ALS_NEG_LAMBDA * (np.clip(sim_mat, 0.0, None) @ neg_w)
            dots -= np.minimum(pen, cap)
        u_als_pen[at_pen] = dots

    # V8 热度：训练段全量评分数（未提供的电影记 0）
    u_pop = None
    if pop_counts is not None:
        u_pop = np.asarray([pop_counts.get(int(m), 0)
                            for m in union_ids.tolist()], dtype=np.float64)

    return {
        'user_id': int(user), 'relevant': set(relevant),
        'u_ids': union_ids, 'u_sum': u_sum, 'u_cnt': u_cnt,
        'u_als': u_als, 'u_genre': u_genre, 'seen_count': len(seen),
        'als_candidates': int(als_ids.shape[0]),
        'u_label': u_label, 'u_als_pen': u_als_pen, 'u_pop': u_pop,
    }


# ---------------------------------------------------------------------------
# 归一化、融合与 Top-N（矩阵形式，权重组合批量算）
# ---------------------------------------------------------------------------

def normalize_signal(values, available):
    """逐用户候选内 max 归一到 [0,1]；不可用位置恒 0（由可用掩码接管）。

    ``peak <= 0``（该路信号全 0 或全负）时不缩放，可用位置保持原值截到 [0,1]。
    """
    normalized = np.zeros(values.shape[0], dtype=np.float64)
    if not np.any(available):
        return normalized
    masked = np.where(available, values, -np.inf)
    peak = masked.max()
    if peak > 0.0:
        normalized[available] = np.clip(values[available] / peak, 0.0, 1.0)
    return normalized


def variant_signals(record, with_genre, als_key='u_als', genre_key='u_genre'):
    """构造信号矩阵 ``(S, M)``：S 列 = [归一 itemcf裸求和, 归一 als(, 归一 genre)]，
    缺失位置记 0；M = 可用掩码（0/1）。

    V6/V7/V8 通过 ``als_key='u_als_pen'``（含负反馈罚项）与
    ``genre_key='u_label'``（频率权重 x 近期均分）切换信号列。

    设计修正（第二轮）：ItemCF 信号用**裸求和**逐用户 max 归一——此前用平滑分
    （÷(count+k)）导致网格里不存在"裸求和"这个点，w=[1,0,0] 也回不到 V0，
    等价性检验天然不可能通过。现在 w=[1,0,0] 严格等价 V0（归一不改变序）。
    无证据候选的 itemcf 信号 = 缺失（退出加权），不是 0 分。
    """
    u_sum = record['u_sum']
    u_cnt = record['u_cnt']
    evidence = u_cnt > 0
    cols = [normalize_signal(u_sum, evidence)]
    masks = [evidence.astype(np.float64)]
    als_values = record.get(als_key)
    if als_values is None:
        als_values = record['u_als']
    als_avail = ~np.isnan(als_values)
    cols.append(normalize_signal(als_values, als_avail))
    masks.append(als_avail.astype(np.float64))
    if with_genre:
        genre_values = record.get(genre_key)
        if genre_values is None:
            genre_values = record['u_genre']
        genre_avail = ~np.isnan(genre_values)
        cols.append(normalize_signal(genre_values, genre_avail))
        masks.append(genre_avail.astype(np.float64))
    return np.column_stack(cols), np.column_stack(masks)


def fused_matrix(signals, availability, weights):
    """加权融合（缺失信号退出加权）：``fused = (S @ W) / (M @ W)``。

    某候选的全部可用信号权重之和为 0 时无法打分 → ``-inf``（排到最后），
    绝不按 0 分计入。
    """
    numerator = signals @ weights
    denominator = availability @ weights
    positive = denominator > 0.0
    safe = np.where(positive, denominator, 1.0)
    return np.where(positive, numerator / safe, -np.inf)


def _exact_top_indices(scores, cand_ids, keep):
    """``(-score, movieId)`` 全序的前 ``keep`` 个下标（含并列的确定性打破）。

    ``np.argpartition`` 取候选切片；若切片边界值存在并列，把所有并列者补进
    候选池后统一精排——保证与全排序逐位一致，同时避免对全量候选排序。
    """
    n_cand = scores.shape[0]
    if keep >= n_cand:
        pool = np.arange(n_cand)
    else:
        idx = np.argpartition(-scores, keep - 1)[:keep]
        boundary = scores[idx].min()
        pool = np.union1d(idx, np.flatnonzero(scores == boundary))
    order = pool[np.lexsort((cand_ids[pool], -scores[pool]))]
    return order[:keep]


def rank_top_n(cand_ids, scores, top_n):
    """单路分数的 Top-N movieId 列表（``(-score, movieId)`` 序）。"""
    cand_ids = np.asarray(cand_ids, dtype=np.int64)
    scores = np.asarray(scores, dtype=np.float64)
    if cand_ids.shape[0] == 0:
        return []
    keep = min(int(top_n), cand_ids.shape[0])
    return cand_ids[_exact_top_indices(scores, cand_ids, keep)].tolist()


def top_n_from_matrix(fused, cand_ids, top_n):
    """融合分矩阵的逐列 Top-N（矩阵形式一次 argpartition + 列内小集合精排）。"""
    n_cand, n_combo = fused.shape
    if n_cand == 0:
        return [[] for _ in range(n_combo)]
    keep = min(int(top_n), n_cand)
    if keep < n_cand:
        parts = np.argpartition(-fused, keep - 1, axis=0)[:keep]
    else:
        parts = np.tile(np.arange(n_cand)[:, None], (1, n_combo))
    columns = np.arange(n_combo)
    boundaries = fused[parts, columns].min(axis=0)
    out = []
    for c in range(n_combo):
        pool = np.union1d(parts[:, c],
                          np.flatnonzero(fused[:, c] == boundaries[c]))
        order = pool[np.lexsort((cand_ids[pool], -fused[pool, c]))]
        out.append(cand_ids[order[:keep]].tolist())
    return out


# ---------------------------------------------------------------------------
# tune 半网格搜索与全变体打分
# ---------------------------------------------------------------------------

def score_variants_for_user(record, params, top_n, pop_alpha=V5_POP_ALPHA,
                            v8_key='V8_pop_rerank'):
    """按变体清单给单用户出 Top-N；返回 ``{variant_name: [movieId, ...]}``。

    V0/V1/V2 的候选 = R1 证据集（分数 > 0；与第五轮 ItemCF"只返回正分条目"
    的口径一致）；V3/V4 的候选 = 并集（缺失信号退出加权）；
    V5 = 覆盖率导向选择的门控融合（参数存在时才计算）。
    """
    out = {}
    ev = np.flatnonzero((record['u_cnt'] > 0) & (record['u_sum'] > 0))
    ev_ids = record['u_ids'][ev]
    ev_sum = record['u_sum'][ev]
    ev_cnt = record['u_cnt'][ev]
    out['V0_raw_sum'] = rank_top_n(ev_ids, ev_sum, top_n)
    out['V1_mean'] = rank_top_n(ev_ids, ev_sum / ev_cnt, top_n)
    k2 = float(params['V2_smoothed']['k'])
    out['V2_smoothed'] = rank_top_n(ev_ids, ev_sum / (ev_cnt + k2), top_n)

    weights3 = np.asarray(params['V3_itemcf_als']['weights'],
                          dtype=np.float64).reshape(3, 1)
    signals, availability = variant_signals(record, with_genre=True)
    fused = fused_matrix(signals, availability, weights3)
    out['V3_itemcf_als'] = top_n_from_matrix(
        fused, record['u_ids'], top_n)[0]

    weights4 = np.asarray(params['V4_itemcf_als_genre']['weights'],
                          dtype=np.float64).reshape(3, 1)
    signals, availability = variant_signals(record, with_genre=True)
    fused = fused_matrix(signals, availability, weights4)
    out['V4_itemcf_als_genre'] = top_n_from_matrix(
        fused, record['u_ids'], top_n)[0]

    if 'V5_coverage_oriented' in params:
        weights5 = np.asarray(params['V5_coverage_oriented']['weights'],
                              dtype=np.float64).reshape(3, 1)
        signals, availability = variant_signals(record, with_genre=True)
        fused = fused_matrix(signals, availability, weights5)
        out['V5_coverage_oriented'] = top_n_from_matrix(
            fused, record['u_ids'], top_n)[0]

    # ---- V6/V7/V8（EDA 2026-10-10；固定权重与参数，无自由参数）----
    # V6 = V4 但类型路换「频率权重 x 近期均分」；V7 = V6 + ALS 负反馈罚项；
    # V8 = V7 + 热门降权重排（= 部署版 V5 全量）。record 缺 V5 字段
    # （旧测试的手工构造记录）时安全跳过。
    if record.get('u_label') is not None:
        weights_fixed = np.asarray(V5_FIXED_WEIGHTS,
                                   dtype=np.float64).reshape(3, 1)
        signals6, avail6 = variant_signals(
            record, with_genre=True, genre_key='u_label')
        fused6 = fused_matrix(signals6, avail6, weights_fixed)
        out['V6_label_freq'] = top_n_from_matrix(
            fused6, record['u_ids'], top_n)[0]

        signals7, avail7 = variant_signals(
            record, with_genre=True, als_key='u_als_pen',
            genre_key='u_label')
        fused7 = fused_matrix(signals7, avail7, weights_fixed)
        out['V7_als_neg'] = top_n_from_matrix(
            fused7, record['u_ids'], top_n)[0]

        u_pop = record.get('u_pop')
        fused8 = fused7
        if u_pop is not None and pop_alpha > 0.0:
            pos_pops = u_pop[u_pop > 0]
            med = (float(np.median(pos_pops)) if pos_pops.shape[0] else 1.0)
            # reshape(-1,1)：fused7 是 (n,1) 矩阵，scale 一维相除会被 NumPy
            # 广播成 (n,n)（每一列除以不同的 scale[j]），Top-N 就取错列了
            scale = np.maximum(u_pop / max(med, 1.0), 1.0).reshape(-1, 1)
            fused8 = fused7 / np.power(scale, pop_alpha)   # 只罚不奖
        out[v8_key] = top_n_from_matrix(
            fused8, record['u_ids'], top_n)[0]
    return out


def grid_search_tune(records, tune_users, top_n, log=None, catalog_size=None):
    """在 tune 半上网格搜索融合权重（目标 = tune 半 NDCG@20 均值最大）。

    设计修正（第二轮）：融合里的 ItemCF 信号改为**裸求和**逐用户归一，
    权重网格 (w_itemcf, w_als, w_genre) 的 simplex 顶点 [1,0,0] 严格等价
    V0——"精度不降"的选项真实存在于网格中（上一轮用平滑信号，网格里
    根本没有裸求和，等价性检验天然不可能通过）。

    - V3 = 网格中 w_genre=0 的最优组合（ItemCF+ALS 两路）；
    - V4 = 网格全局最优（三路）；
    - V5 = 覆盖率导向：NDCG 与网格最优统计等价（差 <= COVERAGE_EQUIV_
      TOLERANCE）的组合里选 tune 覆盖率最大者（catalog_size 给出时启用）。
    """
    combos3 = simplex_grid(3)
    weights3 = np.asarray(combos3, dtype=np.float64).T
    sum_v2 = dict((k, 0.0) for k in TUNE_TEST_KS)
    sum_v4 = np.zeros(len(combos3), dtype=np.float64)
    cov2 = dict((k, set()) for k in TUNE_TEST_KS)
    cov4 = [set() for _ in combos3]
    n = 0
    for user in tune_users:
        record = records[user]
        relevant = record['relevant']
        ev = np.flatnonzero((record['u_cnt'] > 0) & (record['u_sum'] > 0))
        ev_ids = record['u_ids'][ev]
        ev_sum = record['u_sum'][ev]
        ev_cnt = record['u_cnt'][ev]
        for k in TUNE_TEST_KS:
            rec = rank_top_n(ev_ids, ev_sum / (ev_cnt + float(k)), top_n)
            sum_v2[k] += (ndcg_at_k(rec, relevant, top_n) or 0.0)
            cov2[k].update(rec)
        signals, availability = variant_signals(record, with_genre=True)
        fused = fused_matrix(signals, availability, weights3)
        for c, rec in enumerate(
                top_n_from_matrix(fused, record['u_ids'], top_n)):
            sum_v4[c] += (ndcg_at_k(rec, relevant, top_n) or 0.0)
            cov4[c].update(rec)
        n += 1
        if log and n % 200 == 0:
            log('tuned %d users' % n)
    if n == 0:
        raise CorrectnessError('no tune-half users: cannot fit V2/V3/V4 params')

    mean_v2 = dict((k, sum_v2[k] / float(n)) for k in TUNE_TEST_KS)
    best_k2 = max(TUNE_TEST_KS, key=lambda k: (mean_v2[k], -k))
    means_v4 = sum_v4 / float(n)
    best_idx = int(np.argmax(means_v4))

    # V3：w_genre=0 子网格里的最优（ItemCF+ALS 两路）
    v3_indices = [i for i, w in enumerate(combos3) if w[2] == 0.0]
    best_v3_idx = max(v3_indices, key=lambda i: means_v4[i])

    params = {
        'V2_smoothed': {'k': int(best_k2)},
        'V3_itemcf_als': {'weights': [float(w) for w in combos3[best_v3_idx]]},
        'V4_itemcf_als_genre': {'weights': [float(w)
                                            for w in combos3[best_idx]]},
    }

    grid_rows = []
    for c, weights in enumerate(combos3):
        grid_rows.append({
            'weights': [float(w) for w in weights],
            'tune_ndcg': float(means_v4[c]),
            'tune_coverage': (len(cov4[c]) / float(catalog_size)
                              if catalog_size else None),
        })
    tuning = {
        'objective': 'mean NDCG@20 over tune-half users',
        'weight_step': WEIGHT_STEP,
        'tune_users': int(n),
        'grid_combos': len(grid_rows),
        'grid': grid_rows,
        'V2_smoothed': {
            'mean_ndcg_by_k': dict((str(k), float(mean_v2[k]))
                                   for k in TUNE_TEST_KS),
            'best_k': int(best_k2), 'best_mean_ndcg': float(mean_v2[best_k2]),
        },
        'V3_itemcf_als': {'weights': params['V3_itemcf_als']['weights'],
                          'tune_ndcg': float(means_v4[best_v3_idx])},
        'V4_itemcf_als_genre': {'weights': params['V4_itemcf_als_genre']['weights'],
                                'tune_ndcg': float(means_v4[best_idx])},
    }

    # 覆盖率导向选择（catalog_size 给出时启用）：NDCG 与网格最优**统计等价**
    # （差 <= COVERAGE_EQUIV_TOLERANCE）的组合里选 tune 覆盖率最大者。
    cov_params = None
    if catalog_size:
        best_ndcg = float(means_v4[best_idx])
        equivalent = [row for row in grid_rows
                      if row['tune_ndcg'] >= best_ndcg - COVERAGE_EQUIV_TOLERANCE]
        equivalent.sort(key=lambda row: (-row['tune_coverage'],
                                         -row['tune_ndcg']))
        picked = equivalent[0]
        cov_params = {'V5_coverage_oriented': {
            'weights': picked['weights'],
            'tune_ndcg': picked['tune_ndcg'],
            'tune_coverage': picked['tune_coverage'],
        }}
        tuning['coverage_oriented'] = {
            'rule': 'max tune coverage s.t. tune NDCG >= best - %s'
                    % COVERAGE_EQUIV_TOLERANCE,
            'grid_combos': len(grid_rows),
            'best_ndcg_combo': min(grid_rows,
                                   key=lambda row: (-row['tune_ndcg'],
                                                    row['weights'])),
            'picked': picked,
            'catalog_size': int(catalog_size),
        }
    return params, tuning, cov_params


def default_params():
    """tune 半为空（极端冒烟）时的兜底参数：中庸取值，只保证流程可跑。"""
    return {
        'V2_smoothed': {'k': 2},
        'V3_itemcf_als': {'k': 2, 'weights': [0.5, 0.5]},
        'V4_itemcf_als_genre': {'k': 2, 'weights': [0.4, 0.4, 0.2]},
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def _mean(values):
    return sum(values) / float(len(values)) if values else None


def _variant_entry(name, ndcgs, recalls, scored_users, halves, params):
    entry = {'params': dict(params.get(name) or {})}
    for label in ('tune', 'test', 'all'):
        if label == 'all':
            indices = list(range(len(scored_users)))
        else:
            indices = [i for i, u in enumerate(scored_users)
                       if halves[u] == label]
        entry[label] = {
            'users': len(indices),
            'ndcg@20': _mean([ndcgs[i] for i in indices]) if indices else None,
            'recall@20': _mean([recalls[i] for i in indices])
            if indices else None,
        }
    return entry


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Fusion scoring ablation under the ItemCF holdout '
                    'protocol (parallel recall + normalized fusion)')
    parser.add_argument('--input', required=True,
                        help='ratings.csv（userId 分组，与冻结协议同源）')
    parser.add_argument('--holdout-run-dir', required=True,
                        help='冻结协议目录（eval_users.txt / users.json / '
                             'split_holdout.csv）')
    parser.add_argument('--als-factors-jsonl', required=True,
                        help='holdout 协议 ALS 因子（jsonl）；本脚本不连 MySQL')
    parser.add_argument('--itemcf-model-dir', required=True,
                        help='冻结的 ItemCF 邻居图模型目录（model.json + npy，'
                             '部署同款，R1 证据集来源）')
    parser.add_argument('--movies-genres-csv', required=True,
                        help='两列 movie_id,genres 的本地 CSV（| 分隔，类型代理）')
    parser.add_argument('--out', required=True)
    parser.add_argument('--compare-per-user', default=None,
                        help='第五轮 ItemCF 全量 per_user.csv（配对 bootstrap）')
    parser.add_argument('--top-n', type=int, default=TOP_N)
    parser.add_argument('--chunk-rows', type=int, default=200000)
    parser.add_argument('--limit-users', type=int, default=None,
                        help='只评前 N 个评测用户（冒烟用；会写 smoke 标记）')
    parser.add_argument('--als-min-support', type=int, default=0,
                        help='ALS 召回路的支持度门槛（训练段好评人数 >= 该值的'
                             '电影才进 ALS Top-K）。0 = 关闭（不推荐：未训练冷门'
                             '因子的极端点积会让 ALS 信号退化为随机排序）。')
    parser.add_argument('--catalog-size', type=int, default=None,
                        help='片库总数（覆盖率分母）。缺省用 ALS 因子表大小。')
    parser.add_argument('--v5-pop-alpha', type=float, default=V5_POP_ALPHA,
                        help='V8 热门降权指数（缺省 0.5）。敏感性分析可调。')
    args = parser.parse_args(argv)

    started = time.time()
    run_dir = os.path.abspath(args.holdout_run_dir)
    out_dir = os.path.abspath(args.out)
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise SystemExit('refusing to overwrite non-empty out dir: %s' % out_dir)
    os.makedirs(out_dir)

    def log(message):
        print('[fusion-eval] %s' % message)
        sys.stdout.flush()

    # --- 1. 冻结名单 + 2. 切分重建字节对拍（复用 heldout_als_eval）------
    frozen_users = verify_frozen_users(run_dir)
    log('frozen eval users: %d' % len(frozen_users))
    work_dir = os.path.join(out_dir, 'split_work')
    os.makedirs(work_dir)
    rebuild_split_and_verify(args.input, work_dir, run_dir, frozen_users,
                             chunk_rows=args.chunk_rows, log=log)
    # 冒烟：切分校验必须用**全量**名单对拍冻结协议，通过后再截断评测范围。
    eval_users = list(frozen_users)
    if args.limit_users:
        eval_users = eval_users[:args.limit_users]
        log('smoke mode: limit to first %d users' % len(eval_users))
    train_path = os.path.join(work_dir, TRAIN_CSV)
    holdout_path = os.path.join(work_dir, HOLDOUT_CSV)

    # --- 3. 因子 / 邻居图 / 类型表装载 -------------------------------
    version, reg_param, movie_ids, matrix = load_factors_from_jsonl(
        args.als_factors_jsonl, log=log)
    log('als factors: version=%s items=%d' % (version, movie_ids.shape[0]))
    graph = ItemCFGraph(args.itemcf_model_dir)
    log('itemcf graph: items=%d edges=%d' % (graph.n_items, graph.n_edges))
    genre_map = load_genres_csv(args.movies_genres_csv)
    log('genre map: %d movies' % len(genre_map))

    # --- 4. 逐用户候选并集与信号 -------------------------------------
    train_by_user, relevant_by_user, support_by_movie, pop_counts = \
        collect_fusion_segments(train_path, holdout_path, eval_users,
                                log=log)
    factor_pos = dict((int(m), p) for p, m in enumerate(movie_ids.tolist()))
    # V7 中心化余弦基准（因子矩阵全局均值中心化后归一，只算一次）
    _matc = matrix - matrix.mean(axis=0, keepdims=True)
    _nrm = np.linalg.norm(_matc, axis=1, keepdims=True)
    matcn = _matc / np.maximum(_nrm, 1e-12)
    catalog_size = args.catalog_size or int(movie_ids.shape[0])
    log('catalog size (coverage denominator): %d' % catalog_size)
    records = {}
    scored_users = []
    skipped_no_relevant = 0
    for user in eval_users:
        relevant = relevant_by_user.get(user)
        if not relevant:
            skipped_no_relevant += 1
            continue
        segments = train_by_user.get(user) or {'rated': [], 'positive': []}
        records[user] = build_user_record(
            user, segments, relevant, graph, genre_map, factor_pos,
            movie_ids, matrix, reg_param, support_by_movie=support_by_movie,
            als_min_support=args.als_min_support, pop_counts=pop_counts,
            matcn=matcn)
        scored_users.append(user)
        if len(scored_users) % 500 == 0:
            log('built candidates for %d users' % len(scored_users))
    log('scored users: %d (skipped no-relevant: %d)'
        % (len(scored_users), skipped_no_relevant))

    # --- 5. tune/test 分离 + 网格搜索（只看 tune 半）------------------
    halves = dict((u, tune_test_half(u)) for u in scored_users)
    tune_users = [u for u in scored_users if halves[u] == 'tune']
    test_users = [u for u in scored_users if halves[u] == 'test']
    log('split halves: tune=%d test=%d' % (len(tune_users), len(test_users)))
    if tune_users:
        params, tuning, cov_params = grid_search_tune(
            records, tune_users, args.top_n, log=log,
            catalog_size=catalog_size)
    else:
        params = default_params()
        cov_params = None
        tuning = {'skipped': True,
                  'reason': 'no tune-half users (smoke run?)',
                  'fallback_params': params}
        log('WARNING: empty tune half; using fallback params')
    if cov_params:
        params.update(cov_params)
    log('tuned params: %s' % json.dumps(params, sort_keys=True))

    # --- 6. 全变体打分 + 指标 ----------------------------------------
    variant_names = list(VARIANT_NAMES)
    if 'V5_coverage_oriented' in params:
        variant_names.append('V5_coverage_oriented')
    # V6/V7/V8：无自由参数，记录里带 V5 字段就一定产出（按首个用户探测）
    v8_name = ('V8_pop_rerank' if args.v5_pop_alpha == V5_POP_ALPHA
               else 'V8_pop_rerank_a%s' % ('%g' % args.v5_pop_alpha))
    for name in ('V6_label_freq', 'V7_als_neg', v8_name):
        if name not in variant_names and scored_users \
                and name in score_variants_for_user(
                    records[scored_users[0]], params, args.top_n,
                    pop_alpha=args.v5_pop_alpha, v8_key=v8_name):
            variant_names.append(name)
    recs_by_variant = dict((name, []) for name in variant_names)
    relevants = []
    for user in scored_users:
        scored = score_variants_for_user(records[user], params, args.top_n,
                                         pop_alpha=args.v5_pop_alpha,
                                         v8_key=v8_name)
        for name in variant_names:
            recs_by_variant[name].append(scored[name])
        relevants.append(records[user]['relevant'])
    metrics_by_variant = {}
    for name in variant_names:
        recalls, ndcgs = per_user_metrics(recs_by_variant[name], relevants,
                                          k=args.top_n)
        metrics_by_variant[name] = {'recall': recalls, 'ndcg': ndcgs}

    summary = {
        'method': 'fusion-ablation-v1',
        'factor_source': version,
        'reg_param': reg_param,
        'protocol': {
            'holdout_run_dir': run_dir,
            'eval_sha256': user_list_sha256(frozen_users),
            'holdout_csv_sha256': file_sha256(holdout_path),
            'frozen_holdout_sha256': file_sha256(
                os.path.join(run_dir, HOLDOUT_CSV)),
        },
        'top_n': args.top_n,
        'als_top_k': ALS_TOP_K,
        'split_salt': FUSION_SPLIT_SALT,
        'positive_threshold': POSITIVE_THRESHOLD,
        'users_scored': len(scored_users),
        'tune_users': len(tune_users),
        'test_users': len(test_users),
        'users_skipped_no_relevant': skipped_no_relevant,
        'als_min_support': args.als_min_support,
        'v5_pop_alpha': args.v5_pop_alpha,
        'catalog_size': catalog_size,
        'variants': dict(
            (name, _variant_entry(name, metrics_by_variant[name]['ndcg'],
                                  metrics_by_variant[name]['recall'],
                                  scored_users, halves, params))
            for name in variant_names),
        'smoke': bool(args.limit_users),
        'seconds': time.time() - started,
    }

    # V0 与第五轮基线的一致性检查（V0 无自由参数，对全体有效用户取均值）。
    v0_all = summary['variants']['V0_raw_sum']['all']['ndcg@20']
    difference = (abs(v0_all - FIFTH_ROUND_ITEMCF_NDCG20)
                  if v0_all is not None else None)
    summary['v0_reproduction'] = {
        'reference_ndcg@20': FIFTH_ROUND_ITEMCF_NDCG20,
        'v0_ndcg@20_all_users': v0_all,
        'abs_difference': difference,
        'tolerance': REPRODUCTION_TOLERANCE,
        'note': '差异来源=候选池语义（第五轮在全库证据集上打分，V0 同口径；'
                '并集成员不进 V0 候选）',
        'verdict': ('reproduced' if difference is not None
                    and difference <= REPRODUCTION_TOLERANCE
                    else 'not_reproduced'),
    }

    # --- 7. 产物（原子写）--------------------------------------------
    per_user_rows = []
    for index, user in enumerate(scored_users):
        row = {'user_id': user, 'half': halves[user],
               'relevant_count': len(relevants[index])}
        for name in variant_names:
            row['%s_ndcg@20' % name] = metrics_by_variant[name]['ndcg'][index]
            row['%s_recall@20' % name] = \
                metrics_by_variant[name]['recall'][index]
        per_user_rows.append(row)
    write_csv_atomic(
        os.path.join(out_dir, 'per_user.csv'),
        ['user_id', 'half', 'relevant_count']
        + ['%s_%s' % (name, metric) for name in variant_names
           for metric in ('ndcg@20', 'recall@20')],
        per_user_rows)

    rec_rows = []
    for index, user in enumerate(scored_users):
        for name in variant_names:
            for rank_, movie_id in enumerate(recs_by_variant[name][index],
                                             start=1):
                rec_rows.append({'user_id': user, 'variant': name,
                                 'rank': rank_, 'movie_id': movie_id})
    write_csv_atomic(os.path.join(out_dir, 'recommendations.csv'),
                     ['user_id', 'variant', 'rank', 'movie_id'], rec_rows)

    # 覆盖率（test 半用户 Top-20 去重电影 / catalog_size）
    coverage = {}
    for name in variant_names:
        distinct = set()
        for i, u in enumerate(scored_users):
            if halves[u] == 'test':
                distinct.update(recs_by_variant[name][i])
        coverage[name] = {
            'distinct_movies': len(distinct),
            'coverage': round(len(distinct) / float(catalog_size), 6),
        }
        all_g = set()
        for m in distinct:
            all_g |= genre_map.get(m, frozenset())
        coverage[name]['distinct_genres'] = len(all_g)
    summary['coverage_test_half'] = coverage
    summary['coverage_note'] = (
        '覆盖率 = test 半用户 Top-20 去重电影数 / catalog_size；'
        '开题"混合模型提升覆盖率"的检验就在这一节')

    write_json_atomic(os.path.join(out_dir, 'tuning.json'), tuning)

    if args.compare_per_user:
        baseline = load_compare_per_user(args.compare_per_user)
        paired = {}
        for name in variant_names:
            pairs = [(i, u) for i, u in enumerate(scored_users)
                     if halves[u] == 'test' and u in baseline]
            if not pairs:
                continue
            paired[name] = {
                'users': len(pairs),
                'ndcg@20': paired_bootstrap(
                    [metrics_by_variant[name]['ndcg'][i] for i, _u in pairs],
                    [baseline[u][1] for _i, u in pairs]),
                'recall@20': paired_bootstrap(
                    [metrics_by_variant[name]['recall'][i] for i, _u in pairs],
                    [baseline[u][0] for _i, u in pairs]),
            }
        summary['paired_vs_itemcf'] = paired

    write_json_atomic(os.path.join(out_dir, 'summary.json'), summary)
    write_json_atomic(os.path.join(out_dir, 'run_status.json'), {
        'status': 'success', 'seconds': summary['seconds'],
        'factor_source': version, 'users_scored': len(scored_users),
        'tune_users': len(tune_users), 'test_users': len(test_users),
    })
    log('V0 ndcg@20(all)=%.6f vs fifth round %.6f -> %s'
        % (v0_all, FIFTH_ROUND_ITEMCF_NDCG20,
           summary['v0_reproduction']['verdict']))
    for name in variant_names:
        entry = summary['variants'][name]
        log('%s test: ndcg@20=%s recall@20=%s params=%s'
            % (name, entry['test']['ndcg@20'], entry['test']['recall@20'],
               entry['params']))
    log('done (%.1fs)' % summary['seconds'])
    return 0


if __name__ == '__main__':
    sys.exit(main())
