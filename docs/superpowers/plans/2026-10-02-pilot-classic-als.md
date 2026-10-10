# 试点实验：经典 ALS 小范围验证 实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在 ML-32M 的 2,000 用户切片上，用经典 ALS（带偏置矩阵分解，交替最小二乘）完成「切分 → 分布式训练 → 评测 → 出图」小闭环，验证算法正确性、预测分越界率、小众霸榜现象与评测协议自检。

**架构：** 实验代码全部放 `experiments/pilot/`（不碰系统代码）；切片脚本在 VM 上从本地 ratings.csv 生成并上传 HDFS；训练用 PySpark RDD 实现的经典 ALS（每轮四次分组求解：b_u → b_i → p_u → q_i），评分与评测在 driver 端 NumPy 完成；matplotlib 出 3 张图。

**技术栈：** PySpark 3.5.1（VM，`--master 'local[*]'`）、NumPy、matplotlib（开发机）、unittest。

---

## 术语纪律（全文遵守）

- 模型表述为「**经典 ALS**（带偏置矩阵分解 + 交替最小二乘，Koren 2009）」，**不使用"自研"字样**；
- 正则按观测数缩放（λ·n），口径与 Spark MLlib ALS 一致（SPARK-2085），在代码注释与论文中注明出处；
- 预测式：$\hat r_{ui} = \mu + b_u + b_i + \mathbf p_u^\top \mathbf q_i$。

## 试点范围（明确不做）

ItemCF、级联、BiasMF 独立行、消融、敏感性扫描、bootstrap CI、分层热力图、口味吻合度——全部后置到第二步计划。试点模型只有三行 + 两个 sanity 基线：

| 行 | 模型 | 说明 |
|---|---|---|
| 1 | Random（seed=42） | sanity 下界，低于它 = 实现有 bug |
| 2 | MostPop（训练集评分人数降序） | 非个性化基线 |
| 3 | **经典 ALS**（rank=50, λ=0.1, iters=10） | 试点主角 |
| 4 | Const（预测恒为 μ） | 仅报 RMSE，评分预测 sanity 下界 |

**指标**：Recall@20、NDCG@20（相关 = 测试条目中 rating ≥ 4.0 的电影）、RMSE（仅 ALS 与 Const）。

## 文件结构

| 文件 | 职责 |
|---|---|
| 创建 `scripts/make_pilot_slice.py` | 两遍流式读 ratings.csv，固定种子抽 2,000 用户全部评分，输出切片 CSV（Py3.6 兼容，跑在 VM） |
| 创建 `experiments/__init__.py` | 空文件，使 experiments 成为包 |
| 创建 `experiments/pilot/__init__.py` | 空文件 |
| 创建 `experiments/pilot/data.py` | 纯函数：按用户时序留最后 20%（最少 1 条）切分 |
| 创建 `experiments/pilot/metrics.py` | 纯函数：recall_at_k / ndcg_at_k / rmse |
| 创建 `experiments/pilot/baselines.py` | 纯函数：most_popular / random_top |
| 创建 `experiments/pilot/als.py` | ALS 求解核心（纯函数）+ Spark 训练包装 `fit_spark` |
| 创建 `experiments/pilot/smoke_als.py` | VM 冒烟入口：合成 12 条评分跑 3 轮，打印损失 |
| 创建 `experiments/pilot/run_pilot.py` | spark-submit 编排入口：读切片→切分→训练→三模型评测→CSV |
| 创建 `experiments/pilot/plots.py` | 开发机跑，出 3 张 PNG |
| 创建 `tests/test_pilot_slice.py` | 切片脚本测试 |
| 创建 `tests/test_pilot_data.py` | 切分测试（防泄漏） |
| 创建 `tests/test_pilot_metrics.py` | 指标手算对拍 |
| 创建 `tests/test_pilot_baselines.py` | 基线测试 |
| 创建 `tests/test_pilot_als.py` | ALS 求解核心测试 |

> **commit 纪律**：本计划每个任务含 commit 步骤；**批准本计划即视为对其中 commit 步骤的预先同意**，除此之外不额外提交。测试统一用 `backend/venv/Scripts/python.exe -m unittest`（已装 NumPy）。

---

### 任务 1：试点切片脚本

**文件：**
- 创建：`scripts/make_pilot_slice.py`
- 测试：`tests/test_pilot_slice.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_pilot_slice.py
import csv
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.make_pilot_slice import count_users, sample_users, filter_rows


class PilotSliceTests(unittest.TestCase):
    def _write_csv(self, path, rows):
        with open(path, 'w', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            w.writerow(['userId', 'movieId', 'rating', 'timestamp'])
            w.writerows(rows)

    def test_sample_deterministic(self):
        counts = {'1': 5, '2': 3, '3': 9, '4': 1}
        a = sample_users(counts, 2, seed=42)
        b = sample_users(counts, 2, seed=42)
        self.assertEqual(a, b)

    def test_filter_rows_keeps_only_sampled(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'in.csv')
            dst = os.path.join(d, 'out.csv')
            self._write_csv(src, [
                ['1', '10', '4.0', '100'],
                ['2', '11', '3.0', '101'],
                ['1', '12', '5.0', '102'],
            ])
            n = filter_rows(src, dst, {'1'})
            self.assertEqual(n, 2)
            with open(dst, encoding='utf-8') as f:
                lines = f.read().strip().splitlines()
            self.assertEqual(len(lines), 3)  # header + 2 rows
            self.assertIn('10', lines[1])
            self.assertIn('12', lines[2])

    def test_count_users(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, 'in.csv')
            self._write_csv(src, [
                ['1', '10', '4.0', '100'],
                ['1', '11', '3.0', '101'],
                ['2', '12', '5.0', '102'],
            ])
            self.assertEqual(count_users(src), {'1': 2, '2': 1})


if __name__ == '__main__':
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_slice -v`
预期：FAIL / ImportError（`scripts.make_pilot_slice` 不存在）

- [ ] **步骤 3：编写最少实现代码**

```python
# scripts/make_pilot_slice.py
"""从 ratings.csv 生成试点切片：固定种子抽 N 个用户的全部评分。

两遍扫描：第一遍统计每用户评分数，第二遍过滤输出。
在 VM 上对 /home/h/movielens/ratings.csv 运行（Python 3.6 兼容）。
"""
import argparse
import csv
import random
from collections import Counter


def count_users(path):
    counts = Counter()
    with open(path, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            counts[row['userId']] += 1
    return counts


def sample_users(counts, n_users, seed):
    rng = random.Random(seed)
    users = sorted(counts, key=lambda u: int(u))
    if n_users > len(users):
        raise ValueError('n_users=%d 超过用户总数 %d' % (n_users, len(users)))
    return set(rng.sample(users, n_users))


def filter_rows(src, dst, keep):
    written = 0
    with open(src, newline='', encoding='utf-8') as fin, \
            open(dst, 'w', newline='', encoding='utf-8') as fout:
        reader = csv.DictReader(fin)
        writer = csv.DictWriter(fout, fieldnames=reader.fieldnames)
        writer.writeheader()
        for row in reader:
            if row['userId'] in keep:
                writer.writerow(row)
                written += 1
    return written


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--src', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--users', type=int, default=2000)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()
    counts = count_users(args.src)
    keep = sample_users(counts, args.users, args.seed)
    written = filter_rows(args.src, args.out, keep)
    print('users=%d rows=%d -> %s' % (len(keep), written, args.out))


if __name__ == '__main__':
    main()
```

注意：测试首行已把项目根加入 `sys.path`，Python 3 命名空间包语义下 `scripts/` 无需 `__init__.py` 即可导入。

- [ ] **步骤 4：运行测试验证通过**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_slice -v`
预期：`Ran 3 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add scripts/make_pilot_slice.py tests/test_pilot_slice.py
git commit -m "feat: 试点切片脚本（固定种子抽用户）"
```

---

### 任务 2：时序切分

**文件：**
- 创建：`experiments/__init__.py`、`experiments/pilot/__init__.py`（空文件）
- 创建：`experiments/pilot/data.py`
- 测试：`tests/test_pilot_data.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_pilot_data.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.data import temporal_split


class TemporalSplitTests(unittest.TestCase):
    def test_split_sizes_and_leakage(self):
        rows = []
        # user 1: ts 1..5；user 2: ts 1..2；user 3: 只有 1 条
        for ts in range(1, 6):
            rows.append((1, 100 + ts, 4.0, ts))
        for ts in range(1, 3):
            rows.append((2, 200 + ts, 3.0, ts))
        rows.append((3, 300, 5.0, 1))
        train, test = temporal_split(rows)
        self.assertEqual(len(train), 4 + 1 + 1)  # u1:4 u2:1 u3:1
        self.assertEqual(len(test), 1 + 1)       # u1:1 u2:1
        # 无泄漏：每个用户的测试条目 ts 严格晚于其训练条目
        for u in (1, 2):
            tr = [r for r in train if r[0] == u]
            te = [r for r in test if r[0] == u]
            self.assertLess(max(r[3] for r in tr), min(r[3] for r in te))

    def test_single_rating_user_stays_in_train(self):
        train, test = temporal_split([(9, 1, 5.0, 1)])
        self.assertEqual(len(train), 1)
        self.assertEqual(len(test), 0)

    def test_deterministic(self):
        rows = [(1, i, 4.0, i) for i in range(10)]
        self.assertEqual(temporal_split(rows), temporal_split(rows))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_data -v`
预期：FAIL / ImportError

- [ ] **步骤 3：编写最少实现代码**

```python
# experiments/pilot/data.py
"""时序切分：每用户按 timestamp 升序，留最后 20%（最少 1 条）为测试集。

只做 train/test 两段：试点超参固定为文献值，不调参，故不切验证集。
单评分用户无法评测，整条留训练集。
"""
from collections import defaultdict


def temporal_split(rows, test_frac=0.2):
    by_user = defaultdict(list)
    for idx, row in enumerate(rows):
        by_user[row[0]].append((idx, row))
    train_idx, test_idx = [], []
    for user in sorted(by_user):
        entries = sorted(by_user[user], key=lambda t: (t[1][3], t[0]))
        n = len(entries)
        if n < 2:
            train_idx.extend(i for i, _ in entries)
            continue
        n_test = max(1, int(n * test_frac))
        if n - n_test < 1:
            n_test = n - 1
        train_idx.extend(i for i, _ in entries[:n - n_test])
        test_idx.extend(i for i, _ in entries[n - n_test:])
    train = [rows[i] for i in sorted(train_idx)]
    test = [rows[i] for i in sorted(test_idx)]
    return train, test
```

- [ ] **步骤 4：运行测试验证通过**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_data -v`
预期：`Ran 3 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add experiments/__init__.py experiments/pilot/__init__.py experiments/pilot/data.py tests/test_pilot_data.py
git commit -m "feat: 试点时序切分（防泄漏断言覆盖）"
```

---

### 任务 3：评测指标

**文件：**
- 创建：`experiments/pilot/metrics.py`
- 测试：`tests/test_pilot_metrics.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_pilot_metrics.py
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.metrics import recall_at_k, ndcg_at_k, rmse


class MetricsTests(unittest.TestCase):
    def test_recall(self):
        self.assertEqual(recall_at_k(['x', 'b', 'a'], ['a', 'b'], 2), 0.5)
        self.assertEqual(recall_at_k(['a', 'b'], ['a', 'b'], 2), 1.0)

    def test_ndcg(self):
        got = ndcg_at_k(['x', 'b', 'a'], ['a', 'b'], 2)
        expected = (1.0 / math.log2(3)) / (1.0 / math.log2(2) + 1.0 / math.log2(3))
        self.assertAlmostEqual(got, expected, places=9)
        self.assertEqual(ndcg_at_k(['a', 'b'], ['a', 'b'], 2), 1.0)

    def test_rmse(self):
        self.assertAlmostEqual(rmse([(1.0, 3.0), (3.0, 3.0)]), math.sqrt(2.0), places=9)

    def test_empty_relevant_returns_none(self):
        self.assertIsNone(recall_at_k(['a'], [], 5))
        self.assertIsNone(ndcg_at_k(['a'], [], 5))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_metrics -v`
预期：FAIL / ImportError

- [ ] **步骤 3：编写最少实现代码**

```python
# experiments/pilot/metrics.py
"""排序与评分指标（二值相关性：相关 = 测试条目 rating >= 4.0）。"""
import math


def recall_at_k(recommended, relevant, k):
    if not relevant:
        return None
    hits = len(set(recommended[:k]) & set(relevant))
    return hits / float(len(set(relevant)))


def ndcg_at_k(recommended, relevant, k):
    rel = set(relevant)
    if not rel:
        return None
    dcg = sum(1.0 / math.log2(pos + 2)
              for pos, item in enumerate(recommended[:k]) if item in rel)
    idcg = sum(1.0 / math.log2(pos + 2) for pos in range(min(len(rel), k)))
    return dcg / idcg if idcg > 0 else 0.0


def rmse(pairs):
    if not pairs:
        return None
    return math.sqrt(sum((p - a) ** 2 for p, a in pairs) / float(len(pairs)))
```

- [ ] **步骤 4：运行测试验证通过**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_metrics -v`
预期：`Ran 4 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add experiments/pilot/metrics.py tests/test_pilot_metrics.py
git commit -m "feat: 试点评测指标（Recall/NDCG/RMSE）"
```

---

### 任务 4：非个性化基线

**文件：**
- 创建：`experiments/pilot/baselines.py`
- 测试：`tests/test_pilot_baselines.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_pilot_baselines.py
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.baselines import most_popular, random_top


class BaselinesTests(unittest.TestCase):
    def test_most_popular_orders_by_count_then_id(self):
        train = [(1, 10, 5.0, 1), (2, 10, 4.0, 2), (3, 20, 3.0, 3), (4, 30, 3.0, 4)]
        self.assertEqual(most_popular(train, 3), [10, 20, 30])  # 10:2 次；20/30 平票按 id

    def test_random_top_deterministic(self):
        items = list(range(100))
        self.assertEqual(random_top(items, 5, seed=42), random_top(items, 5, seed=42))
        self.assertNotEqual(random_top(items, 5, seed=42), random_top(items, 5, seed=7))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_baselines -v`
预期：FAIL / ImportError

- [ ] **步骤 3：编写最少实现代码**

```python
# experiments/pilot/baselines.py
"""非个性化基线：MostPop 与 Random。"""
import random
from collections import Counter


def most_popular(train, k):
    counts = Counter(row[1] for row in train)
    return [item for item, _ in sorted(counts.items(), key=lambda t: (-t[1], t[0]))[:k]]


def random_top(items, k, seed=42):
    pool = sorted(items)
    random.Random(seed).shuffle(pool)
    return pool[:k]
```

- [ ] **步骤 4：运行测试验证通过**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_baselines -v`
预期：`Ran 2 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add experiments/pilot/baselines.py tests/test_pilot_baselines.py
git commit -m "feat: 试点非个性化基线（MostPop/Random）"
```

---

### 任务 5：ALS 求解核心（纯函数）

**文件：**
- 创建：`experiments/pilot/als.py`（本任务只含纯函数：solve_factor / solve_bias / init_factors / predict_one）
- 测试：`tests/test_pilot_als.py`

- [ ] **步骤 1：编写失败的测试**

```python
# tests/test_pilot_als.py
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.pilot.als import init_factors, predict_one, solve_bias, solve_factor


class AlsCoreTests(unittest.TestCase):
    def test_solve_factor_matches_ridge_lstsq(self):
        rng = np.random.RandomState(0)
        q = rng.normal(size=(6, 3))
        target = rng.normal(size=6)
        lam, n_obs = 0.1, 6
        got = solve_factor(q, target, lam, n_obs)
        # 岭回归的增广最小二乘等价形式
        aug_q = np.vstack([q, np.sqrt(lam * n_obs) * np.eye(3)])
        aug_t = np.concatenate([target, np.zeros(3)])
        expected = np.linalg.lstsq(aug_q, aug_t, rcond=None)[0]
        np.testing.assert_allclose(got, expected, rtol=1e-9, atol=1e-9)

    def test_solve_bias_closed_form(self):
        self.assertAlmostEqual(
            solve_bias(np.array([1.0, 2.0, 3.0]), lam=0.5, n_obs=3),
            6.0 / (3 * 1.5), places=9)

    def test_predict_one(self):
        got = predict_one(3.5, 0.1, -0.2, np.array([1.0, 0.0]), np.array([0.5, 2.0]))
        self.assertAlmostEqual(got, 3.9, places=9)

    def test_init_deterministic(self):
        a = init_factors([1, 2], rank=4, seed=42)
        b = init_factors([1, 2], rank=4, seed=42)
        np.testing.assert_array_equal(a[1], b[1])
        self.assertFalse(np.allclose(a[1], a[2]))  # 不同 id 初值不同


if __name__ == '__main__':
    unittest.main()
```

- [ ] **步骤 2：运行测试验证失败**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_als -v`
预期：FAIL / ImportError

- [ ] **步骤 3：编写最少实现代码**

```python
# experiments/pilot/als.py
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
```

- [ ] **步骤 4：运行测试验证通过**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_als -v`
预期：`Ran 4 tests ... OK`

- [ ] **步骤 5：Commit**

```bash
git add experiments/pilot/als.py tests/test_pilot_als.py
git commit -m "feat: ALS 求解核心（ridge 闭式解/偏置解/确定性初值）"
```

---

### 任务 6：Spark 分布式训练包装 + VM 冒烟

**文件：**
- 修改：`experiments/pilot/als.py`（追加 `fit_spark` 与损失函数 `train_rmse`）
- 创建：`experiments/pilot/smoke_als.py`

- [ ] **步骤 1：编写最少实现代码（追加到 als.py 末尾）**

```python
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
```

- [ ] **步骤 2：创建冒烟入口**

```python
# experiments/pilot/smoke_als.py
"""VM 冒烟：合成 3 用户 × 4 物品 × 12 条评分，跑 3 轮 ALS，打印损失。"""
from __future__ import print_function

from pyspark.sql import SparkSession

from als import fit_spark


def main():
    spark = SparkSession.builder.appName('pilot-smoke-als').getOrCreate()
    rows = [(1, 1, 5.0, 1), (1, 2, 4.0, 2), (1, 3, 2.0, 3), (1, 4, 1.0, 4),
            (2, 1, 4.0, 1), (2, 2, 5.0, 2), (2, 3, 3.0, 3), (2, 4, 1.0, 4),
            (3, 1, 2.0, 1), (3, 2, 1.0, 2), (3, 3, 4.0, 3), (3, 4, 5.0, 4)]
    _, losses = fit_spark(spark, rows, rank=4, reg=0.1, iters=3, seed=42,
                          num_slices=2, log=print)
    assert losses[-1] < losses[0], 'loss did not decrease: %s' % losses
    print('SMOKE OK losses=%s' % ['%.4f' % x for x in losses])
    spark.stop()


if __name__ == '__main__':
    main()
```

- [ ] **步骤 3：先跑既有单测确认没破坏**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_als -v`
预期：`Ran 4 tests ... OK`（fit_spark 不依赖单测通过，纯函数不受影响）

- [ ] **步骤 4：同步实验代码到 VM 并冒烟**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "mkdir -p /home/h/project/experiments/pilot"
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no experiments/pilot/als.py experiments/pilot/smoke_als.py h@192.168.10.102:/home/h/project/experiments/pilot/
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && spark-submit --master 'local[*]' smoke_als.py 2>&1 | tail -5"
```

预期输出含：`SMOKE OK losses=['0.9x', '0.5x', '0.2x']`（单调下降；若 VM python3 缺 numpy，先 `pip3 install numpy`——本步顺带完成环境检查：`python3 -c "import numpy, pyspark"`）。

- [ ] **步骤 5：Commit**

```bash
git add experiments/pilot/als.py experiments/pilot/smoke_als.py
git commit -m "feat: ALS Spark 分布式训练包装与 VM 冒烟入口"
```

---

### 任务 7：试点编排、出图与双跑一致性

**文件：**
- 创建：`experiments/pilot/run_pilot.py`
- 创建：`experiments/pilot/plots.py`
- 创建：`experiments/pilot/vm_monitor.sh`（VM 资源监控：采样内存/CPU/进程 RSS 到 CSV）
- 创建：`experiments/pilot/resource_report.md`（资源报告：环境、峰值、极限结论）

- [ ] **步骤 1：编写编排入口**

```python
# experiments/pilot/run_pilot.py
"""试点编排：读切片 → 时序切分 → ALS 训练 → 三模型评测 → CSV 落盘。

用法（VM）:
  spark-submit --master 'local[*]' run_pilot.py \
    --input hdfs:///movielens/pilot/ratings.csv \
    --out /home/h/pilot_out/run001 --rank 50 --reg 0.1 --iters 10 --top-n 20 --seed 42
"""
from __future__ import print_function

import argparse
import csv
import os

from pyspark.sql import SparkSession

from als import fit_spark, predict_one
from baselines import most_popular, random_top
from data import temporal_split
from metrics import ndcg_at_k, recall_at_k, rmse


def load_rows(spark, path):
    df = spark.read.csv(path, header=True).select(
        'userId', 'movieId', 'rating', 'timestamp')
    return [(int(r.userId), int(r.movieId), float(r.rating), int(r.timestamp))
            for r in df.collect()]


def evaluate(name, rec_fn, train, test, top_n):
    """rec_fn(user, candidates) -> 候选内排好序的推荐列表（已排除训练已见项）；
    相关 = 测试条目 rating >= 4.0。"""
    train_items = {}
    for u, i, _, _ in train:
        train_items.setdefault(u, set()).add(i)
    items_by_user = {}
    for u, i, r, _ in test:
        items_by_user.setdefault(u, []).append((i, r))
    candidates = sorted({i for _, i, _, _ in train})
    recs, ndcgs, users_used = [], [], 0
    for u in sorted(items_by_user):
        rel = {i for i, r in items_by_user[u] if r >= 4.0}
        cand = [i for i in candidates if i not in train_items.get(u, set())]
        rec = rec_fn(u, cand)[:top_n]
        if rel:
            users_used += 1
            recs.append(recall_at_k(rec, rel, top_n))
            ndcgs.append(ndcg_at_k(rec, rel, top_n))
    return {'model': name,
            'recall@%d' % top_n: sum(recs) / len(recs) if recs else None,
            'ndcg@%d' % top_n: sum(ndcgs) / len(ndcgs) if ndcgs else None,
            'users_scored': users_used}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--rank', type=int, default=50)
    ap.add_argument('--reg', type=float, default=0.1)
    ap.add_argument('--iters', type=int, default=10)
    ap.add_argument('--top-n', type=int, default=20)
    ap.add_argument('--seed', type=int, default=42)
    args = ap.parse_args()

    spark = SparkSession.builder.appName('pilot-als-eval').getOrCreate()
    rows = load_rows(spark, args.input)
    train, test = temporal_split(rows)
    print('rows=%d train=%d test=%d' % (len(rows), len(train), len(test)))

    model, losses = fit_spark(spark, train, rank=args.rank, reg=args.reg,
                              iters=args.iters, seed=args.seed, log=print)
    candidates = sorted({i for _, i, _, _ in train})

    def als_scores(u, cand):
        p, bu = model['P'][u], model['bu'][u]
        scored = [(predict_one(0.0, bu, model['bi'][i], p, model['Q'][i]), i) for i in cand]
        return [i for _, i in sorted(scored, key=lambda t: (-t[0], t[1]))]

    results = [
        evaluate('Random', lambda u, c: random_top(c, args.top_n, args.seed),
                 train, test, args.top_n),
        evaluate('MostPop', lambda u, c: [i for i in most_popular(train, len(c)) if i in set(c)],
                 train, test, args.top_n),
        evaluate('ALS', als_scores, train, test, args.top_n),
    ]

    # RMSE 行（ALS 与 Const），以及预测分越界率
    pred_pairs, all_preds = [], []
    for u, i, r, _ in test:
        pred = predict_one(model['mu'], model['bu'][u], model['bi'][i],
                           model['P'][u], model['Q'][i])
        pred_pairs.append((pred, r))
        all_preds.append(pred)
    out_of_range = sum(1 for p in all_preds if p < 1.0 or p > 5.0) / float(len(all_preds))
    rmse_rows = [
        {'model': 'ALS', 'rmse': rmse(pred_pairs)},
        {'model': 'Const(mu)', 'rmse': rmse([(model['mu'], r) for _, _, r, _ in test])},
    ]

    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    with open(os.path.join(args.out, 'results.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['model', 'recall@%d' % args.top_n,
                                          'ndcg@%d' % args.top_n, 'users_scored'])
        w.writeheader()
        for row in results:
            w.writerow(row)
    with open(os.path.join(args.out, 'rmse.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=['model', 'rmse'])
        w.writeheader()
        for row in rmse_rows:
            w.writerow(row)
    with open(os.path.join(args.out, 'pred_dist.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['pred'])
        for p in all_preds:
            w.writerow(['%.6f' % p])
    with open(os.path.join(args.out, 'summary.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['metric', 'value'])
        w.writerow(['out_of_range_rate', '%.6f' % out_of_range])
        for k, v in enumerate(losses):
            w.writerow(['train_rmse_iter%d' % (k + 1), '%.6f' % v])

    # 案例：训练评分最少的 3 个用户，ALS 与 MostPop 的 Top-20 对照（含支持度）
    counts = {}
    user_counts = {}
    for u, i, _, _ in train:
        counts[i] = counts.get(i, 0) + 1
        user_counts[u] = user_counts.get(u, 0) + 1
    sparse_users = sorted(model['bu'], key=lambda u: (user_counts.get(u, 0), u))[:3]
    with open(os.path.join(args.out, 'case_users.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['user', 'model', 'rank', 'movie_id', 'train_support'])
        for u in sparse_users:
            seen = {x[1] for x in train if x[0] == u}
            cand = [i for i in candidates if i not in seen]
            for name, rec in (('ALS', als_scores(u, cand)),
                              ('MostPop', most_popular(train, args.top_n))):
                for pos, i in enumerate(rec[:args.top_n], 1):
                    w.writerow([u, name, pos, i, counts.get(i, 0)])
    print('out_of_range_rate=%.6f' % out_of_range)
    print('WROTE %s' % args.out)
    spark.stop()


if __name__ == '__main__':
    main()
```

- [ ] **步骤 2：创建资源监控脚本（VM 用量/峰值内存记录的载体）**

```bash
#!/bin/bash
# VM 资源监控：./vm_monitor.sh <输出CSV> <命令...>
# 每 2 秒采样：epoch秒, 总内存MB, 已用MB, 可用MB, swap已用MB, java_rss_mb, python_rss_mb, CPU空闲%
set -u
OUT="$1"; shift
echo "ts,mem_total_mb,mem_used_mb,mem_avail_mb,swap_used_mb,java_rss_mb,python_rss_mb,cpu_idle_pct" > "$OUT"
"$@" &
CMD_PID=$!
while kill -0 "$CMD_PID" 2>/dev/null; do
  TS=$(date +%s)
  read -r T U A S < <(free -m | awk '/^Mem:/{t=$2;u=$3;a=$7} /^Swap:/{s=$3} END{print t,u,a,s}')
  read -r JRSS < <(ps -u "$(whoami)" -o rss=,comm= | awk '/java|SparkSubmit/{j+=$1} END{print j+0}')
  read -r PRSS < <(ps -u "$(whoami)" -o rss=,comm= | awk '/python/{p+=$1} END{print p+0}')
  IDLE=$(top -bn1 | awk '/Cpu\(s\)/{print $8}' | head -1)
  echo "$TS,$T,$U,$A,$S,$((JRSS/1024)),$((PRSS/1024)),${IDLE:-0}" >> "$OUT"
  sleep 2
done
wait "$CMD_PID"
RC=$?
echo "# exit=$RC ts=$(date +%s)" >> "$OUT"
exit $RC
```

- [ ] **步骤 3：在 VM 上端到端跑试点（全程资源监控 + 起 HDFS）**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "bash -lc 'start-dfs.sh' 2>&1 | tail -3"
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "jps"
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no scripts/make_pilot_slice.py h@192.168.10.102:/home/h/project/scripts/
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no experiments/pilot/*.py h@192.168.10.102:/home/h/project/experiments/pilot/
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no experiments/pilot/vm_monitor.sh h@192.168.10.102:/home/h/project/experiments/pilot/
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "chmod +x /home/h/project/experiments/pilot/vm_monitor.sh"
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project && python3 scripts/make_pilot_slice.py --src /home/h/movielens/ratings.csv --out /home/h/pilot_ratings.csv --users 2000 --seed 42"
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "hdfs dfs -mkdir -p /movielens/pilot && hdfs dfs -put -f /home/h/pilot_ratings.csv /movielens/pilot/ratings.csv && hdfs dfs -ls /movielens/pilot"
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && ./vm_monitor.sh /home/h/pilot_out/resource_2k.csv spark-submit --master 'local[*]' run_pilot.py --input hdfs:///movielens/pilot/ratings.csv --out /home/h/pilot_out/run001 2>&1 | tail -8"
```

预期：日志出现 `rows≈300000 train=… test=…`、10 行 `iter=… train_rmse=…` 单调下降、`out_of_range_rate=…`、`WROTE /home/h/pilot_out/run001`。

- [ ] **步骤 4：双跑一致性（可复现性验收）**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && spark-submit --master 'local[*]' run_pilot.py --input hdfs:///movielens/pilot/ratings.csv --out /home/h/pilot_out/run002 2>&1 | tail -3 && diff /home/h/pilot_out/run001/results.csv /home/h/pilot_out/run002/results.csv && diff /home/h/pilot_out/run001/rmse.csv /home/h/pilot_out/run002/rmse.csv && echo REPRO_OK"
```

预期：`REPRO_OK`（两次结果逐位一致；若不一致，先查并行分区与随机初值，再谈指标）。

- [ ] **步骤 5：取回产物并出 3 张图**

```powershell
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no -r h@192.168.10.102:/home/h/pilot_out/run001 experiments/pilot/out/
backend/venv/Scripts/python.exe experiments/pilot/plots.py --in experiments/pilot/out/run001 --slice data/processed/pilot_ratings.csv --outdir experiments/pilot/out/run001
```

`plots.py` 内容：

```python
# experiments/pilot/plots.py
"""试点 3 张图：长尾分布 / 预测分分布 / 稀疏用户案例 Top-20 支持度。"""
import argparse
import csv
import os
from collections import Counter

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt


def read_csv(path):
    with open(path, encoding='utf-8') as f:
        return list(csv.DictReader(f))


def fig_longtail(slice_path, out):
    counts = Counter()
    with open(slice_path, encoding='utf-8') as f:
        for row in csv.DictReader(f):
            counts[row['movieId']] += 1
    xs = sorted(counts.values(), reverse=True)
    plt.figure(figsize=(6, 4))
    plt.loglog(range(1, len(xs) + 1), xs)
    plt.xlabel('movie rank')
    plt.ylabel('ratings count')
    plt.title('Pilot slice long tail (log-log)')
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close()


def fig_pred_dist(run_dir, out):
    preds = [float(r['pred']) for r in read_csv(os.path.join(run_dir, 'pred_dist.csv'))]
    oor = sum(1 for p in preds if p < 1.0 or p > 5.0) / float(len(preds))
    plt.figure(figsize=(6, 4))
    plt.hist(preds, bins=50)
    plt.axvline(1.0, color='r', linestyle='--')
    plt.axvline(5.0, color='r', linestyle='--')
    plt.xlabel('predicted rating')
    plt.ylabel('count')
    plt.title('ALS test predictions (out-of-range = %.4f%%)' % (oor * 100))
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close()


def fig_case(run_dir, out):
    rows = read_csv(os.path.join(run_dir, 'case_users.csv'))
    users = sorted({r['user'] for r in rows})
    fig, axes = plt.subplots(len(users), 2, figsize=(10, 2.6 * len(users)))
    for ax_row, u in zip(axes, users):
        for ax, name in zip(ax_row, ('ALS', 'MostPop')):
            sel = [r for r in rows if r['user'] == u and r['model'] == name]
            sel.sort(key=lambda r: int(r['rank']))
            ax.bar(range(len(sel)), [int(r['train_support']) for r in sel])
            ax.set_title('user %s - %s top-%d' % (u, name, len(sel)))
            ax.set_xlabel('rank')
            ax.set_ylabel('train support')
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    plt.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--in', dest='run_dir', required=True)
    ap.add_argument('--slice', dest='slice_path', required=True)
    ap.add_argument('--outdir', required=True)
    args = ap.parse_args()
    fig_longtail(args.slice_path, os.path.join(args.outdir, 'fig1_longtail.png'))
    fig_pred_dist(args.run_dir, os.path.join(args.outdir, 'fig2_pred_dist.png'))
    fig_case(args.run_dir, os.path.join(args.outdir, 'fig3_case_top20.png'))
    print('WROTE 3 PNG to %s' % args.outdir)


if __name__ == '__main__':
    main()
```

注：切片 CSV 从 VM 取回供 fig1 使用（`scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102:/home/h/pilot_ratings.csv data/processed/`）；开发机缺 matplotlib 时执行 `backend/venv/Scripts/python.exe -m pip install matplotlib`（一次性）。

- [ ] **步骤 6：性能极限探测与资源报告（用量支撑 / 峰值内存 / 极限）**

```powershell
# 加压档 A：2 万用户切片（约 300 万条评分）
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project && python3 scripts/make_pilot_slice.py --src /home/h/movielens/ratings.csv --out /home/h/pilot_ratings_20k.csv --users 20000 --seed 42"
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "hdfs dfs -put -f /home/h/pilot_ratings_20k.csv /movielens/pilot/ratings_20k.csv"
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && ./vm_monitor.sh /home/h/pilot_out/resource_20k.csv spark-submit --master 'local[*]' run_pilot.py --input hdfs:///movielens/pilot/ratings_20k.csv --out /home/h/pilot_out/run20k 2>&1 | tail -5"
# 加压档 B：rank 100（因子内存上探）
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && ./vm_monitor.sh /home/h/pilot_out/resource_20k_r100.csv spark-submit --master 'local[*]' run_pilot.py --input hdfs:///movielens/pilot/ratings_20k.csv --out /home/h/pilot_out/run20k_r100 --rank 100 2>&1 | tail -5"
# 若出现 OOM / swap 打满 / 采样间隔内 CPU 空闲率接近 0 且墙钟骤增（换页抖动）：停止加压，记录断点，不再往上试
```

汇总 `experiments/pilot/resource_report.md`（用 `awk -F,` 从各 CSV 取 max 列填表）：

```markdown
# VM 资源报告（试点）
## 环境：nproc=… ｜ mem=…MB ｜ swap=…MB ｜ Spark 3.5.1 / Java 8 / Python 3.6.8
## 运行记录
| 档位 | 数据规模 | rank/iters | 墙钟 | 峰值 java RSS(MB) | 峰值 python RSS(MB) | swap 峰值(MB) |
## 极限结论
- 单机可稳定支撑：…（示例：4G 内存下 X 用户 / Y 条评分 / rank 50 / 10 轮稳定）
- 越界表现：…（示例：rank 100 于 2 万用户开始占用 swap Z MB，墙钟 ×N）
- 对全量 32M 的含义：…（需分块/多轮外推或 YARN 多节点，写明推算依据）
```

- [ ] **步骤 7：验收判据核对并汇报数字**

- [ ] 全部单测通过（5 个测试模块）；
- [ ] `diff` 双跑一致（REPRO_OK）；
- [ ] Random < MostPop 且 Random < ALS（Recall@20 与 NDCG@20，违反则先查实现）；
- [ ] RMSE(ALS) < RMSE(Const)（违反则先查实现）；
- [ ] `out_of_range_rate < 0.001`（图 2 标注；不满足则如实记录，回设计讨论，不许静默截断）；
- [ ] 图 3 直观回答"小众霸榜是否依旧"：对比稀疏用户 ALS Top-20 与 MostPop Top-20 的支持度分布，如实现象依旧，如实记录，作为下一步（ItemCF/级联或收缩）的依据。
- [ ] `resource_report.md` 出具：每档峰值内存/swap 有具体数字，极限结论有断点依据。

- [ ] **步骤 8：Commit（含产物说明，不含大 CSV/PNG）**

```bash
git add experiments/pilot/run_pilot.py experiments/pilot/plots.py
git commit -m "feat: 试点编排与出图（经典 ALS 三模型对比）"
```

---

## 试点之后（本计划范围外，预告）

ItemCF、ALS+ItemCF 级联、BiasMF 消融（仅当 ALS 效果不足以支撑结论时）、全量 32M 训练、完整验证矩阵——全部等试点数字出来后另立计划。
