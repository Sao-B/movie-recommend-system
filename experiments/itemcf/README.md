# ItemCF 独立实验（二值好评余弦 + 共现收缩）

本目录实现 `docs/superpowers/plans/2026-10-03-itemcf-test-plan.md` 定义的探索性
ItemCF 实验：**判断 ItemCF 是否比人气基线提供更有效的个性化排序**，并区分
"相似度不可靠 / 邻域覆盖不足 / 排序没有增益"几类失败原因。允许结果为负，
不预设必须超过 MostPop。

> 这部分**不接入线上**：不改 `backend/`、`frontend/`、`spark/`、`sql/`、配置或数据库。
> 复用 `experiments/pilot/` 的切分与指标（**只读**，不修改）。

## 1. 文件职责

| 文件 | 职责 |
|---|---|
| `core.py` | `build_positive_matrix` / `build_neighbors` / `score_user`：稀疏共现、相似度收缩、邻居截断、逐用户打分 |
| `baselines.py` | `LikePop`（按好评人数）、`BiasOnly`（仅偏置诊断）；`MostPop` 复用 `experiments.pilot.baselines.most_popular` |
| `evaluation.py` | 固定评测用户、指标与证据上限、分层、配对 bootstrap、案例选择、参数选择 |
| `run_itemcf.py` | CLI 入口、输入校验、构图/评分、缓存身份校验、失败/阻塞状态、原子写产物；**对照方法入口**（`comparison_methods` / `evaluate_comparison_method` / `load_reused_als` / `verify_als_protocol` / `write_summary_table`）也在这里 |
| `run_comparison.py` | **完整对照实验驱动**：一张 `summary_table.csv` 覆盖全部方法（三基线 + 两条 ALS 复用 + ItemCF 9 配置），串行、`run_status.json` + `driver_state.json`、资源监控、选参与案例 |
| `build_graph.py` | 子进程：建单个 β 的 `max_k=100` 邻居图 → `.npz` + `mapping.json` + `summary.json` |
| `score_from_graph.py` | 子进程：从已落盘图按 `k` 前缀打分 → `configs/<k>_beta<b>/summary.json` |
| `verify_results.py` | 从 `per_user.csv` / `recommendations.csv` **原始记录**独立重算指标与证据核对；**模型级聚合重算** + **14 方法覆盖守门**（成功方法缺落盘即报 mismatch） |
| `bootstrap_ci.py` | 配对 bootstrap（seed=42、2000 次、同用户索引）→ `paired_differences.csv` + `bootstrap.json`（含"探索性、非独立显著性验证"声明） |
| `diagnostics.py` | 计划书 §8 诊断驱动：`--mode repeat`（另启进程重新构图+评分，≤1e-9 对拍）、`--mode shuffle`（固定图 + seed=42 错配历史）→ `repeat.json` / `shuffle.json` / `donors.json` |
| `report.py` | 完整主表 + 简短结论 + **计划书 §12 效果判断** + 诊断段落（bootstrap / repeat / shuffle）+ 三张诊断图（配源 CSV） |

## 1.1 对照方法（计划书 4.1）

`run_comparison.py` 产出的 `summary_table.csv` 一张表覆盖下列**全部**方法，列含
方法/配置、NDCG@20、Recall@20、**相对 MostPop 的差值**、满额率、覆盖、支持度、
成本、状态、`code_id`：

| 方法 | 类别 | 说明 | RMSE / 双越界率 |
|---|---|---|---|
| `MostPop` | 基线 | 训练段 `n_all(i)` 降序（复用 `experiments.pilot.baselines.most_popular`） | `N/A` |
| `LikePop` | 基线 | 训练段 `n_like(i)` 降序 | `N/A` |
| `BiasOnly` | 基线 | `mu + b_u + b_i`，λ=0.1、10 轮、按观测数正则 | **报** |
| `ALS-ref` | **真实复用** | `rank50_reg0.1` 产物（run_id `20261003T125319Z`），标记 `reused_verified` | **报** |
| `ALS-best-ranking` | **真实复用** | `rank50_reg0.01` 产物，同一验证集选出的最佳排序配置，**非盲测** | **报** |
| `ItemCF` ×9 | 主方法 | k∈{20,50,100} × β∈{0,10,50} | `N/A` |

**ALS 复用不是抄数字**：`load_reused_als` 先做协议校验（`train_rows` / `val_rows` /
`rows` / `candidate_items` / `users_scored` / `top_n` / `relevant_threshold` /
`evaluation_segment` 逐项相等，且**评测用户序完全一致**），任何一项不符即抛
`public:input` 并阻断后续比较；校验通过后，从 `top_recommendations.csv` 的逐用户
明细 + 本地验证段**重算**指标，`summary.json` 里声明的值只作 `summary_declared_*`
留痕对照。旧产物的字段别名（`validation_rows`、`config.top_n`）在
`PROTOCOL_FIELD_ALIASES` 里显式列出。

## 2. 环境依赖

Python **3.6 兼容**（VM 为 3.6.8）。数值只用 NumPy / SciPy，**不引入**
`surprise`、`implicit` 等现成推荐库。

| 包 | 用途 | 备注 |
|---|---|---|
| `numpy` | 稀疏矩阵索引与计数 | 必需 |
| `scipy` | `scipy.sparse` 分块矩阵乘 | 必需；缺失时构图阶段明确报 `blocked`，**不自动安装** |
| `matplotlib` | 三张诊断图 | **可选**；缺失时图记 `N/A`，主表与图源 CSV 照常产出 |
| `pyspark` | 仅用于从 HDFS 读输入（VM 上） | 本地开发机可改用 `--input` 指向本地 CSV |

开发机解释器（本仓库已验证可用）：

```powershell
backend/venv/Scripts/python.exe -c "import numpy, scipy; print(numpy.__version__, scipy.__version__)"
```

## 3. 真实可运行命令

### 3.1 测试

```powershell
# ItemCF 三个测试模块（core / evaluation / runner）
backend/venv/Scripts/python.exe -m unittest discover -s tests -p "test_itemcf*.py" -v

# 既有 pilot 测试不得回归
backend/venv/Scripts/python.exe -m unittest discover -s tests -p "test_pilot*.py" -v

# Python 3.6 语法与导入检查
python -m compileall -q experiments/itemcf
```

> 在受限沙箱（`%TEMP%` 不可写）下，pilot 里基于 `tempfile.TemporaryDirectory()` 的
> 用例会报 `PermissionError: [WinError 5]`。这是**环境性**失败，不是逻辑回归；
> 其余纯逻辑用例必须全绿。

### 3.2 主网格（VM，真实切片）

```bash
export PYSPARK_PYTHON=python3 PYSPARK_DRIVER_PYTHON=python3
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"

bash experiments/pilot/vm_monitor.sh /home/h/itemcf_out/<run_id>/resources.csv \
  spark-submit --master 'local[*]' experiments/itemcf/run_itemcf.py \
  --input hdfs:///movielens/pilot/ratings.csv \
  --out /home/h/itemcf_out/<run_id>/grid \
  --mode grid --neighbor-k 20 50 100 --beta 0 10 50 \
  --block-size 128 --positive-threshold 4.0 \
  --test-frac 0.2 --val-frac 0.2 --top-n 20 --seed 42
```

`<run_id>` 由执行时生成。**运行器拒绝覆盖已有非空产物目录**；`grid` 子目录必须
不存在。`repeat` / `shuffle` 使用新子目录与独立子进程，并显式传
`--selected-config` 指向已冻结配置。

### 3.3 完整对照实验（推荐入口：一张表出全部方法）

`run_comparison.py` 自己按 β 顺序建 3 张邻居图、按 (k,β) 顺序跑 9 次评分，
再串行跑 3 个基线与 2 条 ALS 复用——**任意时刻最多一个计算进程**。
外层用 `vm_monitor.sh` 包装即可同时采资源：

```bash
OUT=/home/h/itemcf_out/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT"
cd /home/h/itemcf_code

nohup bash experiments/pilot/vm_monitor.sh "$OUT/resources.csv" \
  /home/h/venvs/itemcf/bin/python experiments/itemcf/run_comparison.py \
  --input /home/h/pilot_ratings.csv \
  --out "$OUT" \
  --reuse-grid /home/h/pilot_out/grid/20261003T125319Z \
  --python /home/h/venvs/itemcf/bin/python \
  --block-size 128 --top-n 20 --seed 42 \
  > "$OUT/run.stdout.log" 2>&1 &
echo $! > "$OUT/run.pid"
```

进度看 `$OUT/driver_state.json`（驱动进程状态：`running`/`completed` + `stage`）与
`$OUT/run.log`（逐配置一行终态）；**`$OUT/run_status.json` 是逐配置终态列表**
（给 `verify_results` 与报告层用，不是驱动状态对象）。中断后可用
`--skip-graphs` 复用已建好的 `neighbors/graphs.json` 续跑。

| 产物 | 内容 |
|---|---|
| `summary_table.csv` | **全部方法**一行不落（含失败/阻塞项与原因） |
| `run_status.json` | 逐配置终态列表（list schema） |
| `driver_state.json` | 驱动进程状态（dict，含 `stage`/`pid`） |
| `neighbors/beta*.npz` + `*.mapping.json` | 3 张邻居图与 movieId 映射 |
| `configs/<k>_beta<b>/summary.json` | 每个 ItemCF 配置的逐用户指标与推荐明细 |
| `per_user.csv` / `recommendations.csv` | 全方法汇总明细（供独立重算） |
| `selected_config.json` | 计划书 6.4 选参结果（只在 ItemCF 9 配置里选） |
| `cases.json` / `PRECHECK.md` / `resources.csv` / `run.log` | 固定案例、三段切分与 MostPop 硬门槛复现、资源采样、日志 |

### 3.4 重复与错配诊断

```bash
spark-submit --master 'local[*]' experiments/itemcf/run_itemcf.py \
  --input hdfs:///movielens/pilot/ratings.csv \
  --out /home/h/itemcf_out/<run_id>/repeat --mode repeat \
  --neighbor-k 20 --beta 10 --selected-config /home/h/itemcf_out/<run_id>/selected_config.json
```

### 3.5 独立核验与报告

```bash
python -m experiments.itemcf.verify_results --run-dir /home/h/itemcf_out/<run_id>/grid \
  --input /home/h/itemcf_out/<run_id>/pilot_ratings.csv

python -m experiments.itemcf.report --run-dir /home/h/itemcf_out/<run_id>/grid
# 只出主表与图源 CSV、跳过绘图：加 --no-figures
```

CLI 契约（`--help` 可直接验证）：

```powershell
backend/venv/Scripts/python.exe experiments/itemcf/run_itemcf.py --help
backend/venv/Scripts/python.exe experiments/itemcf/report.py --help
backend/venv/Scripts/python.exe -m experiments.itemcf.verify_results --help
```

## 4. 分数含义：排序分数 ≠ 预测评分

```
score(u,i) = Σ_{j ∈ H_u} s_ji · 1[i ∈ N_k(j)]        i ∈ C_u
```

- 这是**排序分数**，用于把候选电影排出先后，**不是** 1–5 的预测评分。
- 打分只累加"用户训练段好评（评分为 ≥4.0）的来源电影"的已存邻居；
  不除以相似度和、不加流行度项或偏差项、不做归一化。
- **因此 ItemCF / MostPop / LikePop 的 `rmse` 与 `out_of_range_rate` 一律记 `N/A`**
  （`summary_table.csv` 中就是字面量 `N/A`）。只有 `BiasOnly` 与既有 ALS 才报这两项。
- 排序并列时的决胜规则：`(-score, movieId)`，即分数相同取 **movieId 较小**者。
- 浮点贡献按来源 movieId 升序累加，避免遍历无序集合改变末几位而影响并列排序。

## 5. 邻居方向：来源 j → 目标 i

邻居图按**来源电影 j** 逐行截断：

```
N_k(j) = 按 (-s_ji, target_movie_id) 保留最多 k 个正相似邻居
```

这是"**从用户已经喜欢的电影向外找邻居**"的方向（j 是来源，i 是目标）。

**易错点（务必注意）**：原始余弦相似度是对称的，但**逐行截断后邻居图通常不
对称** —— `i ∈ N_k(j)` 不蕴含 `j ∈ N_k(i)`。

- 打分时累加的是 `s_ji`（来源 j → 目标 i），**不能**用图的转置；
- 不自动取双向并集，不强制互为邻居。

## 6. 短列表约定：不足不补齐，空列表给 0 分

令 `E_u = {i ∈ C_u : score(u,i) > 0}` 为**有邻域证据**的候选：

- `|E_u| < 20` 时**返回不足 20 条**（合法短列表，不是错误）；
- `H_u` 为空或 `E_u` 为空时返回**空列表**；
- **零分电影不按 ID 填满，热门电影也不暗中补齐**；
- 短列表 / 空列表没有返回的位置在 Recall 与 NDCG 里**贡献 0**，
  **不能**按实际返回长度重算 IDCG；
- ItemCF 的短列表是算法覆盖不足的结果，**不改变**相关集合或指标分母。

同理，报告层"缺项显示 `N/A`"，**绝不用 `0` 冒充缺失**。

## 7. 产物清单（计划书第 11 节）

落盘在 `experiments/itemcf/out/<run_id>/`（VM 为 `/home/h/itemcf_out/<run_id>/`）：

| 产物 | 内容 |
|---|---|
| `PRECHECK.md` | 环境、指纹、切分/候选/用户哈希、稀疏度、案例列表、旧基线对拍 |
| `manifest.json`、`source_hashes.json` | Git HEAD 与 dirty 状态、代码哈希、完整参数、版本、输入指纹、执行命令 |
| `run_status.json` | 每个基线/配置/repeat/shuffle 的状态、时间、退出码、耗时、失败原因、`code_id` |
| 邻居 `.npz` + 映射 JSON | 三张 β 图及 `c_ij` 证据；含输入/索引/β/max_k/公式版本哈希 |
| `summary_table.csv` | 全部方法/配置：NDCG/Recall、满额率、覆盖、支持度、成本、状态；仅评分模型报 RMSE |
| `per_user.csv` | userId、相关数/候选数/证据数、返回数、各 K 指标、证据上限、历史长度、分层标签 |
| `recommendations.csv` | userId、rank、movieId、原始排序分数、`n_all`/`n_like`、证据数、最大贡献来源与 `c_ji` |
| `cases.json` | 固定案例的训练好评、各模型推荐与验证命中、ItemCF 全部邻居贡献 |
| `paired_differences.csv`、`bootstrap.json` | 相同用户差值、seed、重复次数、区间（探索性） |
| `selected_config.json` | 选择规则、9 组状态、冻结时间、被选参数 |
| `verification.json` | 原始重算结果、图抽样对拍、源哈希一致性 |
| `conclusion.md` | 内嵌**完整**主表与失败项，回答计划书第 1 节问题与第 12 节判断 |
| `fig1..fig3` `.png` + `.csv` | 三张诊断图，**每张都配源 CSV** |

### 三张图（最多三张）

1. `fig1_ndcg_recall`：全部配置 + 基线的 NDCG@20 / Recall@20；
2. `fig2_k_beta_diagnostics`：k 与 β 对**证据上限 / 实际 Recall / 满额率**的影响；
3. `fig3_case_evidence`：固定案例的推荐支持度与解释证据。

图表**不隐藏失败项、短列表或只保留获胜配置**；失败/阻塞项在图上单独标注。

> `*.csv` 仍受仓库忽略规则约束，所以 `conclusion.md` 内嵌 Markdown 主表保证可审阅；
> **不为这些产物擅改 `.gitignore`**。

## 8. 参数选择与解释边界

选择规则固定（计划书 6.4）：**NDCG@20 降序**；`|差| ≤ 1e-12` 视为平手，
再依次比较 Recall@20、**较小 k**、**较小 β**。不得依据口味图、某个案例或
其他 K 临时改主指标。

- 9 个配置**全部公开**，不只展示最佳项；
- 由于是在同一验证集上选最佳，配对区间只是**这批用户上的探索性不确定性描述**；
- **不把"最佳项区间排除 0"当作已完成独立显著性验证**，不宣称对未来用户必然有增益；
- 验证集已用于 ALS 网格、保留测试曾被更早试点评估过，因此本轮比较**不宣称独立盲测**。

## 9. 失败处理口径（计划书 10.3）

- 单配置资源/普通失败 → 记 `failed` 并保留完整命令、退出码、日志与原因；独立配置可继续。
- 共享构图失败 → 依赖该图的配置记 `blocked`，**不记成"已训练但效果为零"**。
- 输入/切分/索引/有限性/证据上限/缓存归属/代码哈希不一致 → **公共正确性问题**，
  立即暂停后续比较；修复后用**新 run_id**，旧结果不得混进同一主表。
- 源码在子进程开始前、退出后都核验；运行中发生修改则当前结果无效并阻断后续。
- 摘要采用**临时文件写完再原子替换**，读取端仍校验结构；损坏/截断的摘要也必须留下失败状态。
- 推荐效果差、低于 MostPop、低于 LikePop **都不是程序异常**：保留结果并按预算完成，
  不临时加热门补齐、不改正例阈值、不过滤冷门片。

## 10. 已知局限

- 二值好评（阈值 4.0）丢弃了评分细粒度与负反馈；训练低分电影只进 `Seen_u`（不被推荐），
  不参与共现矩阵。
- 不实现 ALS+ItemCF 融合、热门补齐、内容过滤、BPR 或其他相似度变体。
- 元数据（genre）仅用于结果解释，**不进入**相似度、打分或候选筛选；
  元数据不可得时相关指标记 `N/A`。
- Spark 只负责读输入；**相似度在单机 driver 分块计算，不称为分布式训练**。
