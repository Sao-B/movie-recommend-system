"""Pure online recommendation calculations used by the Flask service."""

from __future__ import annotations

from typing import Iterable

import numpy as np
from scipy.optimize import nnls


def solve_user_vector(
    item_factors: np.ndarray,
    ratings: np.ndarray,
    reg_param: float,
    nonnegative: bool = True,
) -> np.ndarray:
    """Solve the fixed-item ALS user step with optional nonnegative factors."""
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
    regularizer = np.sqrt(reg_param) * np.eye(rank)
    augmented_factors = np.vstack((factors, regularizer))
    augmented_ratings = np.concatenate((values, np.zeros(rank)))

    if nonnegative:
        vector, _ = nnls(augmented_factors, augmented_ratings)
        return vector

    lhs = factors.T @ factors + reg_param * np.eye(rank)
    rhs = factors.T @ values
    return np.linalg.solve(lhs, rhs)


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
