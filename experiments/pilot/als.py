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


def train_rmse(rows, mu, bu, bi, P, Q):
    """训练集 RMSE，用于收敛监控。rows:[(u,i,r,ts)]"""
    errs = []
    for u, i, r, _ in rows:
        errs.append((predict_one(mu, bu[u], bi[i], P[u], Q[i]) - r) ** 2)
    return float(np.sqrt(np.mean(errs)))


def fit_spark(spark, rows, rank=50, reg=0.1, iters=10, seed=42, num_slices=16, log=None):
    """分布式交替求解：每轮 b_u → b_i → p_u → q_i。

    rows:[(u,i,r,ts)]，返回 (model, losses)。
    model = {'mu':…, 'bu':{u:…}, 'bi':{i:…}, 'P':{u:ndarray}, 'Q':{i:ndarray}}
    """
    sc = spark.sparkContext
    mu = sum(r for _, _, r, _ in rows) / float(len(rows))
    users = sorted({u for u, _, _, _ in rows})
    items = sorted({i for _, i, _, _ in rows})
    P = init_factors(users, rank, seed)
    Q = init_factors(items, rank, seed + 1)
    bu = dict((u, 0.0) for u in users)
    bi = dict((i, 0.0) for i in items)
    rdd = sc.parallelize([(u, i, r) for u, i, r, _ in rows], num_slices)

    losses = []
    for it in range(iters):
        # 1) 用户偏置：b_u = Σ(r - mu - b_i - p.q) / (n_u (1 + λ))
        bi_b, P_b, Q_b = sc.broadcast(bi), sc.broadcast(P), sc.broadcast(Q)

        def _bu(groups):
            for u, pairs in groups:
                res = [r - mu - bi_b.value[i] - float(np.dot(P_b.value[u], Q_b.value[i]))
                       for i, r in pairs]
                yield (u, solve_bias(res, reg, len(pairs)))

        bu = dict(rdd.map(lambda x: (x[0], (x[1], x[2]))).groupByKey().mapPartitions(_bu).collect())

        # 2) 物品偏置：b_i = Σ(r - mu - b_u - p.q) / (n_i (1 + λ))
        bu_b = sc.broadcast(bu)

        def _bi(groups):
            for i, pairs in groups:
                res = [r - mu - bu_b.value[u] - float(np.dot(P_b.value[u], Q_b.value[i]))
                       for u, r in pairs]
                yield (i, solve_bias(res, reg, len(pairs)))

        bi = dict(rdd.map(lambda x: (x[1], (x[0], x[2]))).groupByKey().mapPartitions(_bi).collect())

        # 3) 用户因子：p_u = (Σ q q^T + λ n_u I)^-1 Σ (r - mu - b_u - b_i) q
        bu_b, bi_b = sc.broadcast(bu), sc.broadcast(bi)

        def _pu(groups):
            for u, pairs in groups:
                q_sub = np.array([Q_b.value[i] for i, _ in pairs])
                tgt = np.array([r - mu - bu_b.value[u] - bi_b.value[i] for i, r in pairs])
                yield (u, solve_factor(q_sub, tgt, reg, len(pairs)))

        P = dict(rdd.map(lambda x: (x[0], (x[1], x[2]))).groupByKey().mapPartitions(_pu).collect())

        # 4) 物品因子：q_i = (Σ p p^T + λ n_i I)^-1 Σ (r - mu - b_u - b_i) p
        P_b = sc.broadcast(P)

        def _qi(groups):
            for i, pairs in groups:
                p_sub = np.array([P_b.value[u] for u, _ in pairs])
                tgt = np.array([r - mu - bu_b.value[u] - bi_b.value[i] for u, r in pairs])
                yield (i, solve_factor(p_sub, tgt, reg, len(pairs)))

        Q = dict(rdd.map(lambda x: (x[1], (x[0], x[2]))).groupByKey().mapPartitions(_qi).collect())

        loss = train_rmse(rows, mu, bu, bi, P, Q)
        losses.append(loss)
        if log:
            log('iter=%d train_rmse=%.6f' % (it + 1, loss))

    return {'mu': mu, 'bu': bu, 'bi': bi, 'P': P, 'Q': Q}, losses
