"""经典 ALS（带偏置矩阵分解，Koren 2009）。

预测式: r_hat = mu + b_u + b_i + p_u . q_i
目标函数: min Σ (r - r_hat)^2
          + λ (Σ_u n_u ||p_u||^2 + Σ_i n_i ||q_i||^2 + Σ_u n_u b_u^2 + Σ_i n_i b_i^2)

正则按观测数缩放（λ·n），口径与 Spark MLlib ALS 一致（SPARK-2085）。
"""
import numpy as np


def solve_factor(obs_q, target, lam, n_obs):
    """解 (Q^T Q + λ n I) x = Q^T target。obs_q:(n,rank) target:(n,)"""
    rank = obs_q.shape[1]
    a = obs_q.T.dot(obs_q) + (lam * n_obs) * np.eye(rank)
    b = obs_q.T.dot(target)
    return np.linalg.solve(a, b)


def solve_bias(residual, lam, n_obs):
    """解 min Σ (res - b)^2 + λ n b^2 → b = Σ res / (n (1 + λ))"""
    return float(np.sum(residual)) / (n_obs * (1.0 + lam))


def init_factors(ids, rank, seed, scale=0.1):
    """按 id 派生确定性随机初值，保证分布式环境下可复现。"""
    out = {}
    for x in ids:
        rng = np.random.RandomState((seed * 1000003 + int(x)) % (2 ** 32))
        out[x] = rng.normal(0.0, scale, rank)
    return out


def predict_one(mu, bu, bi, p, q):
    return mu + bu + bi + float(np.dot(p, q))
