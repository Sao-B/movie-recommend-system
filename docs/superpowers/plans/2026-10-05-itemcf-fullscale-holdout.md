# 全量规模留出评测计划（ItemCF 优势是否仍成立）

日期：2026-10-05 ｜ 分支：`codex/itemcf-evaluation`（沿用，不新建分支，不 commit）

**本轮只回答一个问题：在全量规模的训练数据下，ItemCF 的推荐优势是否仍然成立。**

不调参（k=100、beta=0、threshold=4.0 全程冻结），不重启 ALS 研究，不接线上，不改数据库。

---

## 1. 结论相关的根本困难（必须先说清楚）

**执行前的第一版判断是错的，这里如实记录并更正。** 我原本以为"让全部用户都有时间留出"
做不到：按 `n_test = max(1, int(n_u * 0.2))`，`n_u < 5` 的用户会被取整吃掉唯一一条评分。
**这个担心在本数据集上不成立**，两条独立路径实测一致：

| 事实 | 实测值 | 来源 |
|---|---|---|
| 用户数 / 物品数 / 行数 | 200,948 / 84,432 / 32,000,204 | 全量流式扫描 |
| 每用户评分条数最小值 | **20** | 同上 |
| `n_u < 5` 的用户 / 其行数 | **0 / 0** | 同上 |
| `train_rows == 0` 的用户 | **0** | 同上（且 `n_u >= 20` 时 `n_test` 永不触发 `n-n_test<1` 的钳位） |

原因是数据集本身的构造：`data/raw/README.txt` 第 6 行原文——
"**All selected users had rated at least 20 movies.**" ML-32M 已经按 `n_u >= 20` 预筛过。

更早的一处相关误述也一并更正：`temporal_split` 在 `n < 2` 时**短路**（`data.py` L18-20），
所以 `n == 1 → train 1 / test 0`（不是 train 0 / test 1）。二者叠加的结论是：
**"训练图为薄用户丢掉共现"这个风险在 ML-32M 上不存在，"只保留 `n_u >= 5`"是恒等变换**
（保留 100% 的行与正例，每个物品的 `n_like` 比值恒为 1.0）。

因此本轮**不再需要**"协议 B"来修任何泄漏，也不再用它当敏感性口径——
它在这份数据上是一个 no-op。**全量口径就是唯一口径**：

| 口径 | 定义 | 角色 |
|---|---|---|
| **全量 80/20（唯一口径）** | **全部** 200,948 个用户按个人时间前 80/后 20 切分；训练图 = 全部用户的训练段（与既有 `confirm.py default` 模式**逐字节同口径**） | 主结论 |
| 分层报告（不是另一套口径） | 同一份结果按用户历史长度（`|H_u|`）分层呈现 | 描述性，用来看"优势是否只来自活跃用户" |

于是本轮真正的不确定性**不是**"训练图薄不薄"，而是另外两条必须如实处理的东西：

1. **规模**：训练图从上一轮的 2.57M 行涨到 25.6M 行，邻居图从 208 万条边涨到约 540 万条，
   评测用户从 1,953 涨到接近 2,000；Single-machine 上"打分"这一步的算法选择因此必须换
   （见 §5）。
2. **口径第一次被真正检验**：上一轮每个评测用户都有一批训练评分，所以 `R_u ⊆ C_u` 恒成立，
   "候选外正例计入分母"这条规则**从未被触发**。全量口径下它**依然**不会触发
   （因为 `train_rows == 0` 的用户为 0，且 `n_u >= 20` 保证每人至少 16 条训练评分）。
   这一条要在结论里写明"是**未触发**，不是**已验证**"——否则又是把"没验"说成"验过"。

## 2. 评测用户名单（在看任何指标之前冻结）

1. **排除名单**：既有 `20261004T100122Z` 的 cohort ∪ eval（20,000 人，`excluded_users.txt`）
   ∪ 旧 2k / 20k 切片用户（并集）∪ fix14 的 1930 用户（该集是 2k 切片子集）。
2. **按 `sha256("itemcf-fullscale-eval-v1:" + str(userId))` 升序**取前 **2,000 人**为评测名单。
   新的 salt ⇒ 与前两轮评测名单**独立**；不按活跃度或结果筛人。
3. 名单落盘后才允许计算任何指标。

## 3. 指标与判定规则（预先固定，不按结果改）

- 主表三行：ItemCF、MostPop、LikePop；指标 NDCG@20、Recall@20、有效用户数、
  满额率、目录覆盖率、耗时、采样峰值 RSS。
- 差值：对固定用户做配对 bootstrap（seed=42、2000 次、95% percentile），
  报绝对差、相对差、区间、是否排除 0。
- **判定规则（沿用上一轮，不改）**
  1. NDCG 与 Recall 均超过两个基线，且对**较强基线**的 NDCG 区间下界 > 0 → 优势在全量规模下成立；
  2. 均值提升但区间跨零，或两指标方向不一致 → 证据不足，如实写，不追加调参；
  3. 未超过基线 → 优势在全量规模下不成立，如实报告，不调参直到获胜；
  4. 资源/正确性失败 → 报工程未完成，**不把失败记为算法效果零分**。

## 4. 处理"训练段 > 留出段"的评测设计问题（本轮的核心工程点）

上一轮的 `confirm.py` 里，留出段正例 `R_u` 恒 ⊆ 候选集 `C_u`（因为每个用户都有一批训练评分），
所以"候选外正例是否计入分母"这条口径**从来没有被真正检验过**。全量协议 A 会出现
"用户整个历史都在测试段、训练段为空"的正式情形，`R_u ⊄ C_u` 因此**必然发生**。

口径（按计划书原文"候选外正例保留在 Recall 与 IDCG 分母中"）：

```
R_u          = 测试段评分 >= 4.0 的电影集合
Seen_u       = 训练段该用户评过的全部电影（含低分）
C_u          = 全库候选（= 图上出现过训练行为的物品）减去 Seen_u
命中（Recall / NDCG）= 返回的 Top-20 ∩ R_u
分母                 = R_u（Recall）、IDCG 用 |R_u|（NDCG）
```

用户被剔除当且仅当 `R_u` 为空（没有可评测的正例），**不因"训练段为空"而剔除**——
那属于"无证据用户计零不剔除"。

**关于"分数 > 0 才算命中"这条既有口径的如实标注**：`|R_u| <= 20` 时模型总能返回
足够的候选，只是其中分数为 0 的条目不返回、位置空着，所以**并非所有正例都有机会被排出**。
上一轮的实现与实测（`recommendations.csv` 的 `raw_score`）确认了这一点。本轮**不改**这条口径
（改了就不是同一协议），但会在结论里写明它对两个基线略微有利。

**为什么不能把"用户跨两段都出现的物品"从分母里去掉**（一个看似更严格的想法）：
时序切分按 `(timestamp, movieId, 原始行序)` 排序，跨边界的**同秒**评分会被分到两侧。
把重复项剔除会改变 `|R_u|`，从而改变 Recall 与 IDCG，属于**改变协议**，
且会让本轮无法与上一轮 0.1275/0.1411 对比。故**不采用**，只作为已知限制披露。

## 5. 评价的计算口径（避免在 2000 用户上重复扫描整张图）

用户 u 的命中集合满足

```
Hit_u = R_u ∩ { i : score(u,i) > 0 }        （来源排除定义，见下）
```

其中 `score(u,i) > 0 ⟺ Σ_{j ∈ H_u} c_ij · 1[i ∈ N_k(j)] > 0`（beta=0 时 `s_ji > 0 ⟺ c_ij > 0`）。
于是可以用**一次稀疏矩阵乘**精确算出全部 `(u,i) ∈ R_u` 的命中：

```
Order_u = 按 (n_all(i) desc, movieId asc) 取全库前 (|Seen_u| + 20) 名        ← MostPop
Like_u  = 按 (n_like(i) desc, movieId asc) 同上                              ← LikePop
Row_u   = [Order] + [Like] + [H_u]                                          （允许重复）
D       = Σ_u Row_u （行数不变），即"该物品在某用户候选池里出现多少行"
A       = M[train_csr]          （用户 x 物品，1 表示训练段好评）
Hit_u ∩ R_u = A[u, R_u] × (D − A)[:, R_u] > 0
```

- ItemCF 的 `N_k(j)` 恒排除 `j` 自身（对角线），故 `1[i ∈ N_k(j)] = A[u,j] − [j == i]`，
  因此 `score = Σ_j A[u,j] c_ij − A[u,i] c_ii = Σ_j A[u,j] c_ij`（`c_ii` 不参与）——
  **恰好等于 c_csr[u] · c_csr[:, i]**，无需任何掩码。
- `D` 里的两个基线排序是**全库排序的前缀**，`|Order| = |Seen_u| + 20` 对
  "排除 seen 后取前 20"是充分的（seen 中的电影最多耗掉 `|Seen_u|` 个名额）。
- 同一用户用**同一行**累加 ORDER 段与 LIKE 段，是"两方法共用候选规则"在矩阵上的精确表达：
  该用户池内碰巧相同的电影（`n_all == n_like` 的片子，如好评率 100% 的电影）只计一次。
- 这一口径与 `confirm.py::score_itemcf_user` + `top_n_excluding` **数学等价**，
  必须有一条对拍测试钉死（见 §6 测试 T2，plan 与真实算法在同一 300 用户图上逐用户比对推荐列表）。

非命中用户按 `(-score, movieId)` 升序填满 Top-20（确定性；对 Recall/NDCG 无影响）。

## 6. 实施与验证

### 6.1 新增文件（只新增，不改生产代码与既有实验产物）

| 文件 | 作用 |
|---|---|
| `experiments/itemcf/holdout_common.py` | 本轮**只读**复用的口径模块（`full_train` 的流式索引 + `build_block_edges`），唯一作用是让**对拍测试**与主入口共用同一份实现；不含评测逻辑，无资源监控 |
| `experiments/itemcf/heldout_eval.py` | 本轮唯一评测入口：名单冻结、切分、**全量构图**（复用同一模块）、MostPop/LikePop 排序、命中矩阵、指标、bootstrap、资源监控、产物落盘（**自带** `rate_limited_guard`，不经由 `holdout_common` 引入任何额外依赖） |
| `experiments/itemcf/rate_limited_guard.py` | 独立进程监督 + **去除墙钟预算**（每块后显式刷新监督 deadline）；RSS 3 GiB 终止线真正生效 |
| `experiments/itemcf/verify_holdout_eval.py` | **独立**核验（不 import `heldout_eval`、不 import `full_train`）：从原始评分用独立实现重算切分、物品/用户计数、抽样共现与相似度，并独立重算物品排序集合摘要 |
| `tests/test_itemcf_holdout_eval.py` | 协议与口径的锚点用例 |
| `tests/test_itemcf_rate_limited_guard.py` | 监督 deadline 语义用例 |
| `tests/test_itemcf_verify_holdout_eval.py` | 独立核验器的用例 |

`full_train.py` 暴露两个**数据面**函数供 `holdout_common.py` 复用：`stream_input_pass1(input_path, ...)`（流式索引）
与 `run_prepare(input_path, index, out_dir, args, log)`（两遍扫描 → 磁盘 CSR），以及构图核心 `build_block_edges`。
它们不改数学、不改 CLI、不改 `source_hashes_of_modules()` 的 6 个模块哈希，
因此上一轮 `d535c178` 的模型与 `graph_blocks.json` 指纹**完全不受影响**（有测试断言）。
`heldout_eval.py` 需要的 `build_block_edges` 通过 `holdout_eval` 目录内的
**薄转发模块**取得（惰性 import，避免在主入口的模块级引入 `full_train` 的硬依赖链），
对拍测试也走同一条路。`verify_holdout_eval.py` 则连转发模块都不 import，
用完全独立的实现对拍。

### 6.2 测试锚点

- **T1 协议**：80/20 取整、`n_u=1..6` 的边界、切分守恒、防泄漏、时序性、
  `R_u` 为空才剔除、名单在看指标前落盘（`users.json` 先写）。
- **T2 等价性（最重要）**：同一份小图（300~500 用户、block_size=16/64），
  ① 命中矩阵路径 ② 直接调用 `confirm.py::score_itemcf_user`/`top_n_excluding` 逐用户比对
  Top-20 的 ID 与顺序；③ `MostPop`/`LikePop` 与 `most_popular_order`/`like_popular_order` 一致。
- **T3 共现 oracle**：`c_ij = n_like(i)` ⟺ `i` 的每个好评用户都好评了 `j`。
  用**独立实现**（Python 集合）在 150 行小数据上核对整份 `A`，并核对抽样边的
  `c_ij` 与相似度（**先画掩码再算**，禁止把 `beta>0` 的贡献当 top-k）。
- **T4 判定与诚实性**：区间跨零/方向不一致/未超基线三条路径的措辞；
  缺核验时**不得**写"已通过独立核验"。
- **T5 回归**：ItemCF 8 个既有测试模块不得变红；`full_train` 的源码哈希清单不变。

### 6.3 独立核验（另一条实现）

`verify_holdout_eval.py` 从**原始评分 CSV** 出发，用独立实现重算：

1. 每用户 `(ts, movieId, 行序)` 排序后的前 80% / 后 20% 行数（与产物计数逐项相等）；
2. 物品全集、`n_all`、`n_like`（与 `item_ids.npy` / `n_all.npy` / `n_like.npy` 相等）；
3. `R_u`（评测用户）与 `H_u` 条数；
4. 抽样来源（seed 固定）的**全部**邻居：从原始评分重算共现并逐边比对
   `target_indices` / `similarities` / `cooccurrences`；
5. 结构不变式（`indptr` 单调、区间合法、每来源 ≤ 100、无自环、`(-sim, target)` 非增）；
6. 排序集合摘要（`Order`/`Like` 的 `item_ids` 前缀）与 `n_all`/`n_like` 独立重算值一致。

核验器**不 import** `heldout_eval`／`full_train`，用 AST 断言把这条钉死。

### 6.4 资源与耗时

- 监督：`rate_limited_guard`，`RSS_LIMIT=3 GiB`、`MemAvailable` 下限 256 MB（连续 30 s）、
  采样间隔 2 s、盘限按实际可用空间（VM 上此前的 2 GiB 下限会**立即**误杀，因为最近半小时构造输出占满了盘）。
- 预算：总量 8 小时（计划书口径，超时即停），但**不作为**单块墙钟依据。
- BLAS 线程设 1；只允许一个计算进程。

**已知风险（如实预判）**：VM 只有 2 核 / 3.9 GB RAM。`n_like` 判据使 `H_u` 比上一轮口径少约 63%，
但 2,000 用户全量 Top-20 打分仍需数小时；若实测外推超过预算，按规则 4 报告
"本轮未产出"并给出已完成部分的实测数据，**不得**缩规模后冒充全量结果。

## 7. 产物

VM：`/home/h/itemcf_holdout/<run_id>/`；本地：`experiments/itemcf/out/holdout/<run_id>/`

`users.json`（名单与哈希）、`split_*`、`model/`（8 个数组 + `model.json`）、
`holdout_cache/`、`itemcf_hits.npz`、`per_user.csv`、`recommendations.csv`、
`summary_table.csv`、`bootstrap.json`、`verification.json`（**独立核验器写**）、
`run_status.json`、`manifest.json`、`source_hashes.json`、`resources.csv`、`run.log`、
`conclusion.md`。

## 8. 诚实性要求

- 不得把"图是在全量数据上建的"写成"结论是严格盲测"：完整 MovieLens 曾用于生产 ALS 训练，
  排除名单无法穷尽历史评测用户，故只表述为"排除了已识别的历史实验用户的全量规模留出评测"。
- 协议 A 的**用户覆盖比上一轮宽得多**（几乎全部评测用户都有正式留出），这一点必须写明。
- 若结论否证了优势，**照实写**，不做任何"再调一版"的补救。
