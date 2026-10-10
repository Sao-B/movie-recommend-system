"""Pure online recommendation calculations used by the Flask service."""

from __future__ import annotations

from typing import Iterable

import numpy as np
from scipy.optimize import nnls

# 评分量纲（MovieLens 半星制）：在线预测分展示前统一截断到该区间
RATING_MIN = 0.5
RATING_MAX = 5.0


def clamp_rating(value):
    """Clamp a predicted score back into the observed rating scale [0.5, 5.0]."""
    return max(RATING_MIN, min(RATING_MAX, float(value)))


def effective_reg_param(reg_param: float, n_ratings: int) -> float:
    """把 ``reg_param`` 换算成 MLlib 口径下的等效正则强度。

    Spark MLlib 自 SPARK-2085 起，ALS 每一步求解的正则都按该行/列的评分数缩放::

        val regParam = numRatings(index) * lambda      // MLlib 内部

    也就是说训练时学到物品因子所用的目标是
    ``min ||X_i p_i - r_i||^2 + (lambda * n_i) ||p_i||^2``。
    在线 fold-in 必须解**同一个**目标，否则复用了已训练因子、却在一个不同的
    目标下求用户向量，解出的向量与物品因子不在同一尺度上（实测余弦相似度
    中位数仅 0.741，即两个"最优解"方向差 25% 以上）。

    这里返回的就是 ``lambda * n_u``，调用方取平方根拼进增广矩阵。
    """
    return float(reg_param) * int(n_ratings)


def solve_user_vector(
    item_factors: np.ndarray,
    ratings: np.ndarray,
    reg_param: float,
) -> np.ndarray:
    """Solve the fixed-item ALS user step，非负约束（与离线训练一致）。

    正则强度按评分数缩放（``reg_param * n_ratings``），与 MLlib 的
    ``numRatings(index) * lambda`` 对齐，见 :func:`effective_reg_param`。

    把 ``sqrt(lambda_eff) * I`` 纵向拼到因子矩阵、把对应评分补 0，等价于带 L2
    正则的最小二乘；交给 ``nnls`` 解就能顺带满足非负约束。

    注意：**这里不能再用固定的 ``sqrt(reg_param)``**。那样相当于假设"无论用户评了
    多少部，正则强度都一样"，对只有寥寥数条评分的用户而言正则相对过弱，解出的
    向量模长会失控（实测中位数 4.10，而全库物品因子模长中位数仅 1.56），点积随之
    溢出评分量纲上界。
    """
    factors = np.asarray(item_factors, dtype=float)
    values = np.asarray(ratings, dtype=float)
    if factors.ndim != 2 or values.ndim != 1:
        raise ValueError("item_factors must be 2-D and ratings must be 1-D")
    if factors.shape[0] != values.shape[0]:
        raise ValueError("item_factors and ratings must have the same row count")
    if factors.shape[0] == 0:
        raise ValueError("at least one rated item is required")
    if reg_param < 0:
        raise ValueError("reg_param must be nonnegative")

    rank = factors.shape[1]
    lambda_eff = effective_reg_param(reg_param, factors.shape[0])
    regularizer = np.sqrt(lambda_eff) * np.eye(rank)
    augmented_factors = np.vstack((factors, regularizer))
    augmented_ratings = np.concatenate((values, np.zeros(rank)))

    vector, _ = nnls(augmented_factors, augmented_ratings)
    return vector


def rank_unrated_items(
    movie_ids: Iterable[int],
    item_factors: np.ndarray,
    user_vector: np.ndarray,
    rated_movie_ids: set[int],
    top_n: int,
) -> list[tuple[int, float]]:
    """Score, filter, and rank movies for one online user vector."""
    ids = np.asarray(list(movie_ids), dtype=int)
    factors = np.asarray(item_factors, dtype=float)
    vector = np.asarray(user_vector, dtype=float)
    if ids.ndim != 1 or factors.ndim != 2 or vector.ndim != 1:
        raise ValueError("movie_ids, item_factors, and user_vector have invalid shapes")
    if ids.shape[0] != factors.shape[0] or factors.shape[1] != vector.shape[0]:
        raise ValueError("movie IDs and factor dimensions do not match")
    if top_n <= 0:
        return []

    scores = factors @ vector
    candidates = [
        (int(movie_id), float(score))
        for movie_id, score in zip(ids, scores)
        if int(movie_id) not in rated_movie_ids
    ]
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates[:top_n]
