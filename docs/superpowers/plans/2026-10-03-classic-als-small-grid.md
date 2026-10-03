# 经典 ALS 小范围参数测试（rank × λ 网格）实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 在现有 2,000 用户切片上，只扫 rank 与 λ 两项，观察它们对推荐排序、评分误差、预测分越界与运行成本的影响，给出「哪个配置较好、改善多少、是否收敛、是否值得继续」的可追溯结论。

**架构：** 全程复用 `experiments/pilot/`，实验与产物不出该目录，**不改应用代码、不写生产数据库**。模型沿用当前带偏置项的矩阵分解 `r̂ = μ + b_u + b_i + p_u·q_i`，用交替最小二乘求解；因子不加非负约束；正则沿用 `λ × 观测数` 口径（与 MLlib 一致，SPARK-2085），λ 同时作用于因子与偏置。评测协议改为**三段时序切分（64% 训练 / 16% 验证 / 20% 保留测试）**，只报告验证集结果。

**技术栈：** PySpark 3.5.1（VM，`--master 'local[*]'`）、NumPy、unittest（开发机 `backend/venv`）、bash（VM 串行编排）。

---

## 0. 本次修订：初稿 → 定稿的口径

> 2026-10-03 复核（对着 `experiments/pilot/` 现有代码逐行核对）后补全。初稿方向不变，以下 10 处从「有歧义 / 与代码不符」改为「唯一写法」。

| # | 初稿状态 | 定稿口径 | 落点 |
|---|---|---|---|
| 1 | 「沿用向下取整、至少留 1 条且至少保留 1 条训练评分」 | 三段切分完整规则，含 **n=2 不切验证** 的硬约束 | §3.1 |
| 2 | 「逐轮训练目标值（含正则）」 | 现码 `fit_spark` 只产出训练 RMSE → **新增 `objective()` 纯函数 + `on_iter` 回调** | §3.7、§4 |
| 3 | 「修正越界区间为 [0.5, 5.0]」 | 明确上下界含端、剔除口径、**两个口径分别统计**、MostPop 记 N/A | §3.5 |
| 4 | 「报告正例可推荐比例」 | 指标定义为「候选内正例数 / 正例总数」，按用户平均 | §3.3 |
| 5 | 「Top-20 支持度中位数」 | 明确为**全池合并**取中位数；每用户中位数仅作附录诊断 | §3.6 |
| 6 | 「各模型使用同一批用户」 | 锁死：无正例用户不计入；**正例全不可推荐者仍计入并记 0** | §3.3 |
| 7 | 「按新切分跑一次 MostPop」 | `run_pilot.py` 加 `--models`，MostPop 只跑一次、表里只出现一行 | §4、§5 任务 4 |
| 8 | 「固定 16 个分区」 | `als.py` 默认是 16 但未暴露 → 加 `--num-slices` 显式传入并记录 | §4 |
| 9 | §5 未写越界验收阈值 | 本轮**不设阈值**，只报告数字与幅度（试点那 0.1% 判据随协议变更作废） | §6 |
| 10 | 未提资源列口径 | `vm_monitor.sh` 的 RSS 是「用户 h 全部 java / 全部 python 进程之和」，报告须注明，不得写成 driver 内存 | §3.8 |

---

## 1. 目标与固定条件

在 2,000 用户切片上，观察 rank、λ 对推荐排序、评分误差和运行成本的影响，找出值得后续验证的配置。**允许所有配置效果都不理想**，不以「必须训练出好模型」为验收条件。

固定项（逐项落到 CLI 参数或代码常量，报告里回填实测值）：

| 固定项 | 值 | 载体 |
|---|---|---|
| 用户切片 | 2000 用户，seed=42 | `scripts/make_pilot_slice.py`（已存在） |
| 切片指纹 | `pilot_ratings.csv` 8,357,080 B，**md5 = `736601f707fc6b048bae46caaf154ee9`**（开发机 `data/processed/` 与 VM `/home/h/` 两侧已核对一致） | 运行记录 |
| 输入 | `hdfs:///movielens/pilot/ratings.csv`（HDFS 上已有，8.0 M） | `--input` |
| 初始化 | `init_factors` 确定性派生，scale=0.1，seed=42 | `--seed 42` |
| 迭代 | 10 轮 | `--iters 10` |
| 分区 | 16 | `--num-slices 16` |
| Top-N | 20 | `--top-n 20` |
| 运行环境 | VM `local[*]`，Spark 3.5.1 / Java 8 / Python 3.6.8，2 核 / 3.9 GB（可用约 2.6 GB），无 YARN | 运行记录 |
| 非负约束 | **不加**（与生产 `nonnegative=true` 有意不同，本计划沿用试点的显式反馈设定） | 代码现状 |
| 正则口径 | `λ × n_obs`，λ 同时作用于因子与偏置，μ 不参与正则 | `als.py` 现状 |

仅改变 rank 与 λ（见 §2）。

## 2. 参数组合

| 参数 | 取值 |
|---|---|
| rank | 10、30、50 |
| λ | 0.01、0.1、1.0 |

共 9 组，**串行**运行。rank=50、λ=0.1 是原配置参照，但**必须按本次三段切分重跑**，不得引用试点旧数字。

## 3. 统一评测口径

### 3.1 三段切分（约 64% / 16% / 20%）

新增 `temporal_split_three_way`，**保留现有 `temporal_split` 不动**（其契约与用例 `tests/test_pilot_data.py` 已锁死）。按用户（升序）处理，用户内按 `(timestamp, movieId, 原行号)` 升序后，令条数 = n：

```python
# experiments/pilot/data.py 新增
def temporal_split_three_way(rows, test_frac=0.2, val_frac=0.2):
    """每用户时序三段切分：[train | val | test]，三段在时间上依次递增。

    规则（硬约束：train 至少 1 条）：
      n < 2 : 全部进 train（单条无法评测）
      n_test = max(1, int(n * test_frac))；若 n - n_test < 1 则 n_test = n - 1
      rest   = n - n_test
      n_val  = max(1, int(rest * val_frac)) if rest >= 2 else 0
      若 rest - n_val < 1 则 n_val = rest - 1      # 保证 train >= 1
    边界样例：n=1 -> (1,0,0)；n=2 -> (1,0,1)；n=3 -> (1,1,1)；
              n=10 -> (7,1,2)；n=100 -> (64,16,20)
    """
```

- 三段各自按原行号排序返回，**段内不跨时间**；同秒跨边界是允许的（秒级粒度），故不宣称严格时间隔离。
- **可验证的预测**：三段切分的 test 段与现有两段切分的 test 段**逐条相同**（同一条规则、同一份排序）。试点两段实测 `rows=304,944 / train=244,752 / test=60,192`，因此本计划预期 `test=60,192`、`train≈195,802`、`val≈48,950`；运行后必须核对（见 §5 任务 1 步骤 2）。
- 「保留测试段」本轮**完全不使用**：不参与训练、不参与评测、不参与候选集与支持度统计。理由如实记录：它已被试点使用过，不是全新盲测。注意验证段来自上轮训练段，故「未被评测过」成立、「从未被模型见过」不成立——只影响叙事，不影响本轮有效性。

### 3.2 候选集与正例

- 候选集 = **本次 64% 训练段**出现过的全部电影（无门槛，逐组重算但同一切分下完全相同）。
- 正例 = 验证集中 rating ≥ 4.0 的电影。
- 排除该用户训练段已评分电影；并列分数按 movieId 升序决胜（沿用 `run_pilot.py` 的 `np.lexsort`）。

### 3.3 排序指标与用户口径

- `Recall@20` / `NDCG@20`：沿用 `experiments/pilot/metrics.py`，分母 `|relevant|` 为**全量去重**，即训练候选之外的（不可命中）正例**仍计入分母并记未命中**。
- 新增 `hit_ratio`（正例可推荐比例）= `|relevant ∩ 候选| / |relevant|`，**按用户平均**，与 Recall/NDCG 同口径、同一批用户。
- **同一批用户** = 至少有 1 条正例的用户；各模型一致（该筛选与模型无关）。**正例全部不可命中的用户仍计入**，Recall/NDCG 记 0。报告该批用户数与排除的用户数。
- 排序指标只报验证集。

### 3.4 RMSE

- 只对「训练中已出现的 user 与 item」计算：冷物品（不在候选集）按 MLlib `coldStartStrategy=drop` 口径剔除；本轮切分下 user 侧天然全中，仍作为断言而非过滤器。
- 预测使用**原始分，不先截断**；同时报 `Const(μ)` 基线。
- 报告**有效条数与剔除条数**。

### 3.5 越界率（两个口径，区间 `[0.5, 5.0]`）

越界 = `pred < 0.5 或 pred > 5.0`（MovieLens 半分制；下限由初稿的 1.0 修正为 0.5）。**两个口径分别统计，不得互相替代，也不得合并成一列**：

| 口径 | 分子 / 分母 |
|---|---|
| A 验证评分预测越界率 | 越界的有效验证条数 / 有效验证条数（同 §3.4 的剔除口径） |
| B Top-20 越界率 | 所有参与评测用户的 Top-20 推荐（**未截断**预测分）中越界条数 / `用户数 × 20` |

- Top-20 分 = `μ + b_u + b_i + p_u·q_i`，不截断。
- MostPop / Random 没有预测分 → 该列记 **N/A**，不要填 0。
- 与试点报告的 0.1388% 不可直接比较（判据下限与切分都变了），表里注明。
- 预期：B 通常**高于** A（取头部等价于按分数选样，天然偏向分布尾部），见 §7。

### 3.6 支持度（诊断项，不参与判据）

- 支持度按**本次 64% 训练段**统计（不含 val/test，不含全量）。
- 「Top-20 支持度中位数」= 把所有参与评测用户的 Top-20 条目**合并成一个池**后取中位数。
- 附录可另报「每用户 Top-20 内中位数 → 再对用户取平均」，仅作诊断。
- 支持度只用于解释现象，**不参与胜负判据**。

### 3.7 逐轮目标值与收敛判定

新增纯函数（`experiments/pilot/als.py`）：

```python
def objective(rows, mu, bu, bi, P, Q, lam):
    """带正则的训练目标值（总和，不取平均）：
       Σ (r - r_hat)^2
       + λ (Σ_u n_u ||p_u||^2 + Σ_i n_i ||q_i||^2 + Σ_u n_u b_u^2 + Σ_i n_i b_i^2)
    n_u / n_i 为训练段中各自的观测数；μ 不参与正则。
    """
```

- `fit_spark` 增加 `on_iter=None` 回调，每轮算完 RMSE 后调用 `on_iter(iter_index, snapshot, loss)`，`snapshot` 为当轮 `{'mu','bu','bi','P','Q'}`。**返回值仍是 `(model, losses)` 两元组**（`smoke_als.py` 的 `_, losses = …` 与 `run_pilot.py` 的解包都不受影响）；不把 objectives 塞进 model dict。
- 报两个序列：`train_rmse_iter1..10`（已有）与 `objective_iter1..10`（新增，含正则）。objective 用**总和**（RMSE 已单列，避免两个平均口径互相污染）。
- 收敛判定：`objective[10] / objective[1]`，以及末轮相对降幅 `(objective[9] - objective[10]) / objective[9]`。末轮相对降幅 < 1% 记「本轮预算内基本收敛」，否则记「未收敛（只报告 10 轮预算下的表现）」。该判定只影响措辞，**不改变 9 组全跑**。

### 3.8 资源统计口径

- 复用 `experiments/pilot/vm_monitor.sh`，每组单独包一次，便于按组归因。
- 该脚本的 `java_rss_mb` = 用户 `h` 名下所有 `java|SparkSubmit` 进程 RSS 之和；`python_rss_mb` = 所有 `python` 进程 RSS 之和。报告必须写成这两个统计范围，**不得写成 driver 内存**。
- 报告字段：环境（nproc / mem / swap / Spark / Java / Python）、每组墙钟、峰值 swap、峰值 `java_rss_mb`、峰值 `python_rss_mb`。

## 4. 文件结构与代码改动清单

本轮只动 `experiments/pilot/` 与 `tests/`，**不碰 `backend/`、`frontend/`、`spark/`、生产数据库**。

| 文件 | 动作 | 说明 |
|---|---|---|
| `experiments/pilot/data.py` | 修改（追加） | 新增 `temporal_split_three_way`；`temporal_split` 一行不改 |
| `experiments/pilot/als.py` | 修改（追加/小改） | 新增 `objective()`；`fit_spark` 加 `on_iter=None` 参数（默认行为与返回契约不变） |
| `experiments/pilot/run_pilot.py` | 修改 | 三段切分；candidates 取 64% 训练段；新增 `--num-slices`（默认 16）、`--val-frac`（默认 0.2）、`--models`（`all`/`als`/`mostpop`，默认 `all`）；越界双口径；`hit_ratio`；Top-20 支持度中位数；objective 序列；输出 `grid_row.csv`（单组一行） |
| `experiments/pilot/plots.py` | 可选修改 | 若出图，`fig_pred_dist` 的越界判据同步改为 `0.5`（当前是 `1.0`），否则图注与表不一致 |
| `tests/test_pilot_data.py` | 修改（追加） | 三段切分：边界 n=1/2/3/4/10/100、三段并集 == 全集、逐用户时间递增、确定性、输入行序不变、test 段与两段切分逐条相同 |
| `tests/test_pilot_als.py` | 修改（追加） | `objective`：λ=0 时 == `n·RMSE²`（手算对拍）；λ>0 时 == λ=0 值 + `λ × Σ(n_u‖p_u‖² + n_i‖q_i‖² + n_u b_u² + n_i b_i²)` 手算值；因子与偏置全 0 时正则项为 0 |
| `tests/test_pilot_metrics.py` | 修改（追加） | `hit_ratio` 手算对拍（分母含不可命中正例、空集返回 None） |
| `experiments/pilot/out/grid/` | 产物 | 结果 CSV、`conclusion.md`、`PRECHECK.md`、每组 run 目录与 `*.log`；**`*.csv` 被 `.gitignore:31` 忽略**，进 git 的是 `conclusion.md`（内嵌结果表）与 `PRECHECK.md` |

`run_pilot.py` 新增输出（每组一个目录）：

| 文件 | 内容 |
|---|---|
| `results.csv` | 每个模型的 `recall@20` / `ndcg@20` / `hit_ratio` / `users_scored` |
| `rmse.csv` | ALS 与 Const(μ) 的 RMSE |
| `summary.csv` | `val_pred_out_of_range`、`top20_out_of_range`、`top20_support_median`、`valid_rows`、`dropped_cold`、`users_scored`、`excluded_no_positive`、`train_rmse_iter1..10`、`objective_iter1..10`、`wall_clock_s` |
| `grid_row.csv` | 单组一行：rank / reg / ndcg@20 / recall@20 / rmse / val_pred_out_of_range / top20_out_of_range / top20_support_median / wall_clock_s（`exit_status` 与 `code_rev` 由 §5 任务 5 的外层循环记录后，在任务 6 汇总时补齐——`run_pilot.py` 自己不知道退出码，也不该在 VM 上调 git） |

## 5. 执行步骤

### 任务 1：口径自检（先证协议能识别模型、手算样例对得上）

**文件：** 无改动；使用 `experiments/pilot/` 现有代码 + 少量合成数据。

- [ ] **步骤 1：核对切片指纹与代码版本**

```powershell
# 开发机
backend/venv/Scripts/python.exe -c "import hashlib;print(hashlib.md5(open('data/processed/pilot_ratings.csv','rb').read()).hexdigest())"
git rev-parse --short HEAD
```
预期：`736601f707fc6b048bae46caaf154ee9`；HEAD = 本计划所在提交。

- [ ] **步骤 2：三段切分手算对拍**

用 10 条单一用户评分（ts=1..10）跑 `temporal_split_three_way`，预期 `(train 7, val 1, test 2)`；用 n=2 的用户验证 `(1, 0, 1)`；用 n=1 验证 `(1, 0, 0)`。再用真实切片统计三段规模，预期 `test=60,192`、`train≈195,802`、`val≈48,950`。
运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_data -v`
预期：全绿（含新增三段用例）。

- [ ] **步骤 3：指标与越界口径手算对拍**

构造 6 条验证数据（含 1 条不可命中正例、1 条 `pred=0.4` 越界、2 条 Top-20 越界），手算 Recall/NDCG/`hit_ratio`/两个越界率，与代码输出逐项比对。
运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_metrics tests.test_pilot_als -v`
预期：全绿。

- [ ] **步骤 4：记录本任务的指纹、代码版本与固定配置**

写入 `experiments/pilot/out/grid/PRECHECK.md`（切片 md5、HEAD、固定项表格取自 §1）。

### 任务 2：代码改动（TDD，逐文件）

**文件：** 见 §4 清单。

- [ ] **步骤 1：写失败测试**（三段切分边界 / objective 手算 / hit_ratio 手算）
- [ ] **步骤 2：运行验证失败**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_data tests.test_pilot_als tests.test_pilot_metrics -v`
预期：FAIL / ImportError 或 AttributeError（新函数不存在）。

- [ ] **步骤 3：写最少实现**（`temporal_split_three_way`、`objective`、`fit_spark(on_iter=…)`、`run_pilot.py` 的 CLI 与输出）
- [ ] **步骤 4：运行验证通过**

运行：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_data tests.test_pilot_als tests.test_pilot_metrics -v`
预期：全绿；且 `temporal_split` 的 5 个旧用例仍全绿。

- [ ] **步骤 5：语法与 Py3.6 兼容门**

运行：`backend/venv/Scripts/python.exe -m compileall -q experiments/pilot tests/test_pilot_data.py tests/test_pilot_als.py tests/test_pilot_metrics.py`
预期：无输出（成功）。VM 是 Python 3.6.8：f-string 可用，但不得用 3.7+ 独有语法（`dataclasses`、`from __future__ import annotations`）或 3.8+ 的赋值表达式。

- [ ] **步骤 6：Commit**

```bash
git add experiments/pilot/data.py experiments/pilot/als.py experiments/pilot/run_pilot.py tests/test_pilot_data.py tests/test_pilot_als.py tests/test_pilot_metrics.py
git commit -m "feat: 网格实验三段切分/含正则目标值/越界双口径"
```

### 任务 3：同步到 VM 并做前置检查

- [ ] **步骤 1：同步实验代码**

```powershell
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no experiments/pilot/data.py experiments/pilot/als.py experiments/pilot/run_pilot.py h@192.168.10.102:/home/h/project/experiments/pilot/
```
（VM 现有 `als.py`/`run_pilot.py`/`metrics.py` 与分支 tip 逐字节一致，只有 `data.py`/`baselines.py` 差注释，同步后复核 md5。）

- [ ] **步骤 2：复核 VM 侧 md5 与 HDFS 就绪**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "md5sum /home/h/project/experiments/pilot/{data.py,als.py,run_pilot.py}; hdfs dfs -ls -h /movielens/pilot/"
```
预期：md5 与开发机一致；`/movielens/pilot/ratings.csv` 在（约 8.0 M）。

- [ ] **步骤 3：冒烟（合成 12 条，确认训练链路仍通）**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && spark-submit --master 'local[*]' smoke_als.py 2>&1 | tail -3"
```
预期：`SMOKE OK losses=['0.3979', '0.2286', '0.1948']`（±1e-3 黄金值）。

### 任务 4：MostPop 参照（只跑一次，9 组共用）

- [ ] **步骤 1：按同一套三段切分跑 MostPop**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "cd /home/h/project/experiments/pilot && spark-submit --master 'local[*]' run_pilot.py --input hdfs:///movielens/pilot/ratings.csv --out /home/h/pilot_out/grid/mostpop --models mostpop --num-slices 16 --top-n 20 --seed 42 --val-frac 0.2 2>&1 | tail -5"
```
预期：`results.csv` 只有 MostPop 一行；`users_scored` 与 9 组一致。

### 任务 5：串行跑 9 组

- [ ] **步骤 1：启动 HDFS（若未起）并确认**

```powershell
ssh -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no h@192.168.10.102 "jps"
```
预期：含 `NameNode`、`DataNode`（2026-10-03 已实测在跑）。

- [ ] **步骤 2：串行执行 9 组（每组独立进程 + 独立资源采样 + 记录退出状态）**

```bash
# VM 上执行；产物落 /home/h/pilot_out/grid/rank{R}_reg{L}/
# CODE_REV 由开发机记下后带进来（VM 的 /home/h/project 不是 git 仓库）：
#   开发机执行 git rev-parse --short HEAD，然后 CODE_REV=<值> bash run_grid.sh
mkdir -p /home/h/pilot_out/grid
cd /home/h/project/experiments/pilot
echo "rank,reg,wall_clock_s,exit_status,code_rev" > /home/h/pilot_out/grid/status.csv
for R in 10 30 50; do
  for L in 0.01 0.1 1.0; do
    OUT=/home/h/pilot_out/grid/rank${R}_reg${L}
    T0=$(date +%s)
    ./vm_monitor.sh /home/h/pilot_out/grid/resource_rank${R}_reg${L}.csv \
      spark-submit --master 'local[*]' run_pilot.py \
        --input hdfs:///movielens/pilot/ratings.csv --out "$OUT" \
        --rank $R --reg $L --iters 10 --top-n 20 --seed 42 --num-slices 16 \
        --models als > "$OUT.log" 2>&1
    RC=$?; T1=$(date +%s)
    echo "$R,$L,$((T1-T0)),$RC,${CODE_REV:-unknown}" >> /home/h/pilot_out/grid/status.csv
  done
done
```

- [ ] **步骤 3：逐组核对与失败处理**

每组必须存在 `results.csv` / `rmse.csv` / `summary.csv` / `grid_row.csv` 且 `exit_status=0`。**失败如实记录**（保留 `*.log` 尾部与退出码），继续下一组；**不中途更换数据、候选集或切分**。

- [ ] **步骤 4：取回产物**

```powershell
scp -o UserKnownHostsFile=NUL -o StrictHostKeyChecking=no -r h@192.168.10.102:/home/h/pilot_out/grid experiments/pilot/out/
```

### 任务 6：汇总与结论

- [ ] **步骤 1：生成结果表（`experiments/pilot/out/grid/summary_table.csv`）**

列：`rank / λ / NDCG@20 / Recall@20 / RMSE / 验证预测越界率 / Top-20 越界率 / Top-20 支持度中位数 / 耗时`；MostPop 参照**只出现一行**，其越界列记 N/A。每行附 `exit_status` 与代码版本。

> **注意：`.gitignore:31` 是 `*.csv`，CSV 产物进不了 git。** 因此交付的「结果 CSV」留在实验目录（本地/VM 产物，可追溯），而**进 git 的那份表必须内嵌进 `conclusion.md`**（见步骤 2）；不要为了提交 CSV 去改 `.gitignore`。

- [ ] **步骤 2：写简短结论（`experiments/pilot/out/grid/conclusion.md`）**

必须回答 4 问：**哪个配置较好、改善多少（相对参照组 rank=50/λ=0.1 与 MostPop）、是否收敛（末轮相对降幅）、是否值得继续**；并逐条给出 §7 两条假设的判读。不做全局最优声明。**`summary_table.csv` 的同表以 markdown 表格内嵌在本文件里**（这是进 git 的结果表）。

- [ ] **步骤 3：出图（可选）**

若出图，`plots.py` 的越界判据同步改为 `0.5`，避免图注与表不一致。

### 任务 7：提交（严格限定范围）

- [ ] **步骤 1：确认未把主工作区的脏改动带进来**

本分支工作区存在 21 个与本次实验无关的未提交改动（`AGENTS.md`、`README.md`、`backend/`、`frontend/`、`tests/test_online_recommender.py` 等）。提交时**只 add 本次实验相关文件**：

```bash
git add experiments/pilot/data.py experiments/pilot/als.py experiments/pilot/run_pilot.py \
        tests/test_pilot_data.py tests/test_pilot_als.py tests/test_pilot_metrics.py \
        docs/superpowers/plans/2026-10-03-classic-als-small-grid.md
git status --short   # 复核暂存区不含上述 21 个文件
```

- [ ] **步骤 2：提交产物与结论（文本 md，不含大 CSV 与 PNG）**

```bash
git add experiments/pilot/out/grid/conclusion.md experiments/pilot/out/grid/PRECHECK.md
git commit -m "docs: rank×λ 网格实验结论与结果表"
```

（`*.csv` 被 `.gitignore:31` 忽略，`summary_table.csv` 与各组 `results.csv` 默认**不进 git**；若确实要提交结果 CSV，用 `git add -f` 并在此说明绕过了 `*.csv` 规则——默认不做。不要为了提交产物去改 `.gitignore`。）

## 6. 判断与停止条件

- 以**验证 NDCG@20 为主、Recall@20 为辅**；同时展示 RMSE 与成本，**不用 RMSE 单独决定推荐模型优劣**。
- 越界率**本轮不设阈值**（试点那 0.1% 判据随协议变更作废），只报告两个口径的数字与越界幅度（max/min）。
- 检查末轮相对降幅与总降幅。未收敛的配置只报告「10 轮预算下的表现」，**不得**据此断言其充分训练后的优劣。
- 效果接近时优先保留更低维、更省资源的配置。
- 本轮只推荐后续候选，**不宣称**获得全局最优参数，也不宣称证明某种病因。
- 完成 9 组 + 简短结论即停止。**即使最佳值落在搜索边界**（rank=10 或 λ=1.0），也只提出后续建议：不自动细化网格、不扩到 20k 或 32M、不增加 ItemCF / 级联 / 收缩机制。

## 7. 预先声明的假设（让 9 组结果可判读）

| # | 假设 | 支持它的观测模式 | 若被证伪 |
|---|---|---|---|
| H1 | λ 从 0.1 升到 1.0 通过 `λ·n` 强收缩欠定物品因子，**提高验证 NDCG@20**，同时降低 Top-20 越界率与 Top-20 支持度中位数 | 三项同向变化 | 若 NDCG 不动而越界率/支持度下降 → 说明收缩只改尺度不改排序，与试点 §6「低支持度欠定」一致；若 λ=1.0 反而更差 → 记录该配置在本预算下不可用 |
| H2 | rank 10/30/50 之间 NDCG 差异小于 λ 的影响（试点 rank 50 vs 100 已显示容量不救排序） | rank 三档接近、λ 三档有梯度 | 如实报告，不解释为「容量不足」以外的结论 |

- 预期方向：Top-20 越界率 **高于** 验证预测越界率（Top-N 是按分数取头部，天然偏向分布尾部）。
- λ 与 rank 在 `λ·n` 口径下**耦合**，报告不声称交互效应。

## 8. 交付物与验收清单

交付：一份结果 CSV、一份简短结论、可追溯的配置与运行记录。结论回答「哪个配置较好、改善多少、是否收敛、是否值得继续」。

- [ ] 单测全绿：`backend/venv/Scripts/python.exe -m unittest tests.test_pilot_data tests.test_pilot_als tests.test_pilot_metrics -v`
- [ ] 旧契约未破：`temporal_split` 的 5 个用例、`metrics` 的 19 个用例仍全绿
- [ ] 三段规模核对通过：`test=60,192`（与两段切分逐条相同），`train+val+test == rows`
- [ ] 9 组 `grid_row.csv` 齐全，`exit_status` 逐组记录（失败也保留）
- [ ] 汇总表列齐全（§5 任务 6 步骤 1 的 9 列 + 状态 + 版本），且同表已内嵌 `conclusion.md`（CSV 被 `.gitignore` 的 `*.csv` 忽略，不能作为唯一交付）
- [ ] 两个越界口径都有独立数字；MostPop 的越界列是 N/A
- [ ] MostPop 参照在表中只出现一行
- [ ] `objective_iter1..10` 与 `train_rmse_iter1..10` 都在 `summary.csv` 里
- [ ] 资源报告注明 RSS 统计范围（全部 java / 全部 python 进程，非 driver）
- [ ] 提交暂存区不含 §5 任务 7 列出的 21 个无关脏改动
- [ ] 切片指纹 `736601f707fc6b048bae46caaf154ee9`、代码版本、固定配置三者与运行记录一致

## 9. 范围外（明确不做）

- 不扩数据集（不上 20k、不上全量 32M）；不新增 ItemCF、级联、收缩、隐式反馈目标。
- 不做 bootstrap CI、分层热力图、多路召回对比。
- 不改应用代码、不写生产数据库、不发布模型版本（`model_versions` 保持 `active` 不变）。
- 不自动细化网格（不补 rank=20/40 或 λ=0.3/3.0），即使最佳值落在边界。
