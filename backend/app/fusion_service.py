"""V4 三路融合在线推荐服务（并联召回 + 归一化融合打分）。

结构（与 holdout 消融实验 V4 变体一致，见
docs/superpowers/plans/2026-10-06-fusion-ablation.md）：

    ① 并联召回：R1 ItemCF 证据集（冻结邻居图查表）
               ∪ R2 ALS 门控 Top-K（折入点积，仅 rating_count >= als_min_support）
    ② 三路信号（各自在候选内 max 归一到 [0,1]，缺失退出加权）：
       itemcf = 裸求和/用户内max   als = 点积/5   genre = 类型∩偏好/偏好数
    ③ 融合：fused = Σ(可用信号·权重) / Σ(可用信号权重)，权重来自 config
    ④ 硬排除：已看 + 不感兴趣 → Top-N

权重 (0.4, 0.5, 0.1) 来自 holdout 消融 tune 半网格搜索（V4 变体，
test NDCG@20=0.135154 >= 裸求和 0.134289，配对显著优于第五轮基线）。

依赖注入：复用 ItemCFService 的冻结邻居图与仓储层；ALS 因子从
MySQL 活模型加载（与系统 ALS 链路同源）。融合分是**相对排序分**
（[0,1]，仅用户内可比），响应中放 ``score`` 字段，不进 predicted_rating。
"""

import json
import logging
from collections import Counter, defaultdict
from threading import RLock

import numpy as np

from .rating_weights import freq_weight, label_signal, rating_weight

logger = logging.getLogger(__name__)

#: 训练侧好评阈值（与 itemcf_service / holdout 协议一致）
POSITIVE_THRESHOLD = 4.0
#: ALS 折入点积的展示归一化除数（点积/5 截 [0,1]，与消融口径一致）
ALS_SCORE_SCALE = 5.0


class FusionModelError(Exception):
    """ALS 因子缺失、版本不匹配或结构错误。"""


class FusionService:
    """V4 三路融合：ItemCF 裸求和 + 门控 ALS 折入 + 类型匹配。"""

    def __init__(self, itemcf_service, repository, weights=(0.4, 0.5, 0.1),
                 als_min_support=1000, als_top_k=500, top_n=20,
                 v5=False, popularity_alpha=0.5, als_neg_lambda=0.2,
                 als_neg_cap=0.5, recent_window=10, genre_quota=8):
        self.itemcf = itemcf_service
        self.repository = repository
        self.w_itemcf, self.w_als, self.w_genre = (float(w) for w in weights)
        self.als_min_support = int(als_min_support)
        self.als_top_k = int(als_top_k)
        self.top_n = int(top_n)
        # ---- V5（EDA 2026-10-10）：差评反馈 + 频率标签 + 热门降权 ----
        # v5=False 时行为与 V4 完全一致（消融基线）。
        self.v5 = bool(v5)
        self.popularity_alpha = float(popularity_alpha)
        self.als_neg_lambda = float(als_neg_lambda)
        self.als_neg_cap = float(als_neg_cap)
        self.recent_window = int(recent_window)
        self.genre_quota = int(genre_quota)
        self._factor_cache = {}
        self._factor_lock = RLock()

    # ---------- ALS 因子（活模型，版本级缓存） ----------

    def _load_als_factors(self, model):
        """加载 active ALS 模型的物品因子矩阵；返回 (version, ids, matrix)。"""
        version = str(model.version)
        with self._factor_lock:
            cached = self._factor_cache.get(version)
            if cached is not None:
                return cached
            rank = int(model.rank)
            ids, rows = [], []
            for row in self.repository.get_model_factors(version):
                values = row.factors
                if isinstance(values, str):
                    values = json.loads(values)
                if len(values) != rank:
                    raise FusionModelError(
                        "ALS factor rank mismatch for movie %s" % row.movie_id)
                ids.append(int(row.movie_id))
                rows.append(values)
            if not rows:
                raise FusionModelError("active ALS model has no item factors")
            order = np.argsort(np.asarray(ids, dtype=np.int64), kind="mergesort")
            ids_arr = np.asarray(ids, dtype=np.int64)[order]
            matrix = np.asarray(rows, dtype=np.float64)[order]
            if not np.isfinite(matrix).all():
                raise FusionModelError("ALS factor matrix has non-finite values")
            result = (version, ids_arr, matrix)
            self._factor_cache[version] = result
            logger.info("fusion: ALS factors loaded (%s, %d items, rank %d)",
                        version, ids_arr.shape[0], rank)
            return result

    # ---------- 主入口 ----------

    def recommendations_for(self, user_id):
        """V4/V5 融合 Top-N；无好评历史时返回空（调用方走冷启动兜底）。

        V5（self.v5=True）在 V4 骨架上叠加（EDA 2026-10-10 定稿参数）：
        ① 标签路改为「频率权重 × 近期均分自校准」，声明标签取自评分历史
          （≥4 分 genre 频次 Top3），不依赖用户自选偏好；
        ② ALS 分数扣减负反馈罚项（中心化余弦传播，λ 与单项上限可配）；
        ③ 候选内热门降权重排（popularity^α）；
        ④ 高频高差评标签的每页类型配额（近窗差评 >=3 的声明标签）。
        v5=False 时与 V4 行为逐位一致。
        """
        ratings = self.repository.get_user_ratings(user_id)
        if not ratings:
            return []
        dislikes = self.repository.get_user_dislikes(user_id)
        seen = {int(row.movie_id) for row in ratings} | set(dislikes)
        history = sorted(
            int(row.movie_id) for row in ratings
            if float(row.rating) >= POSITIVE_THRESHOLD)
        if not history:
            return []

        # ---- V5 前置：声明标签 / 频率权重 / 近期均分 / 配额标签 ----
        v5_labels = {}
        v5_capped = set()
        rated_pairs_all = None
        if self.v5:
            # 仓储按 rated_at 倒序返回：列表头部即最近评分
            rated_pairs_all = [(int(row.movie_id), float(row.rating))
                               for row in ratings]
            rated_genres = self.repository.get_movies_genres_map(
                [m for m, _ in rated_pairs_all])
            pos_cnt = Counter()
            watch_cnt = Counter()
            ratings_by_genre = defaultdict(list)
            for mid, r in rated_pairs_all:
                for g in rated_genres.get(mid, ()):
                    watch_cnt[g] += 1
                    ratings_by_genre[g].append(r)
                    if r >= POSITIVE_THRESHOLD:
                        pos_cnt[g] += 1
            declared = [g for g, _ in sorted(
                pos_cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:3]]
            for g in declared:
                rs = ratings_by_genre[g]
                if len(rs) < 2:
                    continue
                recent = rs[:self.recent_window]
                v5_labels[g] = (freq_weight(watch_cnt[g]),
                                label_signal(sum(recent) / len(recent)))
                if sum(1 for x in recent if x <= 2.0) >= 3:
                    v5_capped.add(g)

        # --- R1: ItemCF 证据（冻结邻居图查表） ------------------------
        graph = self.itemcf._load_graph()
        contrib = {}
        for src in history:
            for tgt, sim in graph.neighbors_of(src):
                if tgt in seen:
                    continue
                contrib.setdefault(tgt, []).append(float(sim))

        # --- R2: ALS 门控 Top-K（折入点积，仅高支持度电影） ------------
        model = self.repository.get_active_model()
        als_by = {}
        factor_pack = None
        if model is not None:
            version, fids, mat = self._load_als_factors(model)
            fpos = {int(v): p for p, v in enumerate(fids)}
            rated_pairs = [(int(row.movie_id), float(row.rating))
                           for row in ratings]
            rows = [fpos[m] for m, _r in rated_pairs if m in fpos]
            if rows:
                values = [r for m, r in rated_pairs if m in fpos]
                try:
                    vec = solve_fold_in(mat[rows], np.asarray(values),
                                        float(model.reg_param))
                except ValueError:
                    vec = None
                if vec is not None:
                    scores = mat @ vec
                    allowed = self.repository.get_movies_above_support(
                        self.als_min_support)
                    pairs = [(int(mm), float(ss))
                             for mm, ss in zip(fids.tolist(), scores.tolist())
                             if mm not in seen and mm in allowed]
                    pairs.sort(key=lambda x: (-x[1], x[0]))
                    als_by = dict(pairs[:self.als_top_k])
                    factor_pack = (fids, mat)

        # --- 候选并集 + 三路信号 --------------------------------------
        union = sorted(set(contrib) | set(als_by))
        if not union:
            return []
        n = len(union)
        i_sum = np.zeros(n); i_cnt = np.zeros(n, dtype=int)
        i_avail = np.zeros(n, dtype=bool)
        als_v = np.full(n, np.nan)
        als_avail = np.zeros(n, dtype=bool)
        gen_v = np.full(n, np.nan)
        gen_avail = np.zeros(n, dtype=bool)
        cand_genres = None
        if self.v5:
            # 批量取候选类型（V5 标签路 / 类型配额共用，避免逐条查询）
            cand_genres = self.repository.get_movies_genres_map(union)
            for p, mm in enumerate(union):
                pairs_ = contrib.get(mm)
                if pairs_:
                    i_sum[p] = sum(sims for sims in pairs_)
                    i_cnt[p] = len(pairs_)
                    i_avail[p] = True
                if mm in als_by:
                    als_v[p] = als_by[mm]; als_avail[p] = True
                gs = cand_genres.get(mm)
                if gs and v5_labels:
                    matched = [v5_labels[g] for g in gs if g in v5_labels]
                    if matched:
                        gen_v[p] = max(fw * sig for fw, sig in matched)
                        gen_avail[p] = True
        else:
            fav = self.repository.get_user_favorite_genres(user_id) or []
            fav_set = set(fav)
            for p, mm in enumerate(union):
                pairs_ = contrib.get(mm)
                if pairs_:
                    i_sum[p] = sum(sims for sims in pairs_)
                    i_cnt[p] = len(pairs_)
                    i_avail[p] = True
                if mm in als_by:
                    als_v[p] = als_by[mm]; als_avail[p] = True
                gs = self.repository.get_movie_genres(mm)
                if gs and fav_set:
                    gen_v[p] = len(gs & fav_set) / float(len(fav_set))
                    gen_avail[p] = True

        # --- V5: ALS 负反馈罚项（中心化余弦传播，评分差评 -> 连带降分） ---
        if self.v5 and factor_pack is not None and self.als_neg_lambda > 0 \
                and als_avail.any():
            fids, mat = factor_pack
            fpos = {int(v): p for p, v in enumerate(fids)}
            neg_pairs = [(m, rating_weight(r)) for m, r in rated_pairs_all
                         if rating_weight(r) < 0 and m in fpos]
            if neg_pairs:
                matc = mat - mat.mean(axis=0, keepdims=True)
                nrm = np.linalg.norm(matc, axis=1, keepdims=True)
                matcn = matc / np.maximum(nrm, 1e-12)
                neg_rows = np.array([fpos[m] for m, _ in neg_pairs])
                neg_w = np.array([-w for _, w in neg_pairs])
                cap = self.als_neg_cap * float(neg_w.sum())
                pos_idx = [p for p in range(n) if als_avail[p]]
                fac_idx = np.array([fpos[union[p]] for p in pos_idx])
                sim_mat = matcn[fac_idx] @ matcn[neg_rows].T
                pen = self.als_neg_lambda * (np.clip(sim_mat, 0.0, None) @ neg_w)
                als_v[pos_idx] -= np.minimum(pen, cap)

        i_n = _normalize(i_sum, i_avail)
        a_n = _normalize(als_v, als_avail)
        g_n = _normalize(gen_v, gen_avail)
        W = np.array([self.w_itemcf, self.w_als, self.w_genre])
        S = np.column_stack([i_n, a_n, g_n])
        M = np.column_stack([i_avail, als_avail, gen_avail]).astype(float)
        fused = (S @ W) / np.where(M @ W > 0, M @ W, 1.0)
        fused = np.where(M @ W > 0, fused, -np.inf)

        # --- V5: 热门降权重排（候选内 popularity^α） --------------------
        movies_all = None
        if self.v5 and self.popularity_alpha > 0:
            movies_all = self.repository.get_movies(union)
            pops = np.array([
                float(getattr(movies_all.get(mm), "rating_count", 0) or 0)
                for mm in union])
            pos_pops = pops[pops > 0]
            med = float(np.median(pos_pops)) if len(pos_pops) else 1.0
            # 只罚不奖：低于中位数的候选 scale 钳到 1，避免小热度候选被无上界放大
            scale = np.maximum(pops / max(med, 1.0), 1.0)
            fused = fused / np.power(scale, self.popularity_alpha)

        # --- Top-N 选择（V5: 高差评标签的类型配额） ---------------------
        order = np.argsort(-fused)
        if (self.v5 and self.genre_quota > 0 and v5_capped
                and cand_genres is not None):
            selected = []
            gcount = Counter()
            for p in order:
                if not np.isfinite(fused[p]):
                    continue
                mm = union[p]
                hit = cand_genres.get(mm, set()) & v5_capped
                if hit and any(gcount[g] >= self.genre_quota for g in hit):
                    continue
                selected.append(p)
                for g in hit:
                    gcount[g] += 1
                if len(selected) >= self.top_n:
                    break
            top = selected
        else:
            top = order[:self.top_n]

        movie_ids = [union[p] for p in top]
        pos_by_movie = dict((mm, p) for p, mm in enumerate(union))
        movies = (movies_all if movies_all is not None
                  else self.repository.get_movies(movie_ids))
        result = []
        for rank_, mm in enumerate(movie_ids, start=1):
            movie = movies.get(mm)
            if movie is None:
                continue
            p = pos_by_movie[mm]
            result.append({
                "rank": len(result) + 1,
                "score": round(float(fused[p]), 6),
                "predicted_rating": None,
                "signals": {
                    "itemcf": round(float(i_n[p]), 4) if i_avail[p] else None,
                    "als": round(float(a_n[p]), 4) if als_avail[p] else None,
                    "genre": round(float(g_n[p]), 4) if gen_avail[p] else None,
                },
                "evidence_count": int(i_cnt[p]),
                "movie": movie.to_dict() if hasattr(movie, "to_dict") else movie,
            })
        return result


def solve_fold_in(factor_rows, ratings, reg_param):
    """部署同款折入：非负最小二乘，正则 = reg_param * 评分数。

    与 backend/app/recommendation_core.solve_user_vector 同一算法
    （本服务不直接 import 该模块，保持服务边界；公式逐位一致）。
    """
    factors = np.asarray(factor_rows, dtype=float)
    values = np.asarray(ratings, dtype=float)
    if factors.shape[0] == 0:
        raise ValueError("no rated items")
    rank = factors.shape[1]
    from scipy.optimize import nnls
    lambda_eff = float(reg_param) * factors.shape[0]
    augmented = np.vstack((factors, np.sqrt(lambda_eff) * np.eye(rank)))
    target = np.concatenate((values, np.zeros(rank)))
    vector, _ = nnls(augmented, target)
    return vector


def _normalize(values, available):
    """候选内 max 归一到 [0,1]；不可用位置 0（由可用掩码接管）。"""
    out = np.zeros(values.shape[0], dtype=np.float64)
    if not available.any():
        return out
    peak = values[available].max()
    if peak > 0:
        out[available] = np.clip(values[available] / peak, 0, 1)
    return out
