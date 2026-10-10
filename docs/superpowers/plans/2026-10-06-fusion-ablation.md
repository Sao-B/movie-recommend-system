# 融合打分消融实验计划（并联召回 + 归一化融合）

日期：2026-10-06 ｜ 分支 `codex/als-ranking-eval`  
前置：ItemCF holdout 基线 NDCG@20=0.132076（第五轮）；ALS 对照因子（时间切分，`/home/h/itemcf_holdout/als_holdout_factors/factors.jsonl`）已就绪。

## 一、目标

在同一 holdout 协议（冻结 2000 人名单、时间切分 80/20、NDCG@20/Recall@20）下，  
对比 **五种打分变体**，回答两个问题：

1. 打分公式从裸求和改为归一化融合，排序质量是升是降；
2. 类型代理信号（问卷的离线代理）值多少分。

## 二、变体定义（对评测用户 u、候选 i）

| 变体          | 打分公式                    | 自由参数           |
| ----------- | ----------------------- | -------------- |
| V0 raw-sum  | `Σ s_ji`（= 冻结口径复现）      | 无              |
| V1 mean     | `Σ s_ji / count`        | 无              |
| V2 smoothed | `Σ s_ji / (count + k)`  | k ∈ {0,2,5,10} |
| V3 V2+ALS   | V2 + `w_a · als_norm`   | k + 权重         |
| V4 V3+类型    | V3 + `w_g · genre_norm` | k + 权重         |

## 三、候选并集

- R1：ItemCF 证据集（训练段好评的邻居表并集，裸求和有证据的候选）；
- R2：ALS Top-500（**holdout 协议因子** `factors.jsonl` 折入点积前 500，绝不用哈希切分的系统模型——泄漏）；
- 并集为全部变体的统一候选池；V0 在并集上等价于"证据集内排序"（无证据候选分为 0，排不到 Top-20）。

## 四、信号与归一化

- ItemCF 信号：V2+ 的平滑分，按**该用户候选内的最大值**归一到 [0,1]（排序只看用户内相对顺序，逐用户归一合法）；
- ALS 信号：`clip(dot/5, 0, 1)`；因子表没有的电影视为**信号缺失**；
- 类型信号（代理）：`user_genres` = 该用户训练段好评电影的类型集合；`match = |genres(i) ∩ user_genres| / |user_genres|`，无类型标注的电影视为信号缺失；
- **缺失信号退出加权**：`fused = Σ(可用信号·权重) / Σ(可用信号权重)`——缺失按 0 分惩罚是错的。

## 五、调参 / 报告分离（防泄漏）

- 1952 个有效评测用户按 `sha256("fusion-ablation-split-v1:" + userId)` 奇偶对半分为 tune / test；
- V2/V3/V4 的自由参数**只在 tune 半**上网格搜索（权重 simplex 步长 0.1 × k）选 NDCG@20 最优；
- **test 半**报告全部变体的 NDCG@20 / Recall@20，并对 ItemCF 全量 per_user（第五轮产物）做配对 bootstrap；
- V0 / V1 无自由参数，两半都报告（test 半与调参变体可比）。

## 六、验收标准

1. V0 在并集候选上的 NDCG@20 与第五轮 0.132076 一致（容差 ±0.005；差异来源=候选池语义，需在产物中说明）；
2. 全部变体在 **test 半**出数，tune 半数字不进结论；
3. `tests/test_heldout_fusion_eval.py` 通过（变体公式单测 + tune/test 分离 + 缺失信号重归一化）；
4. 产物：`out/holdout/fusion_ablation_<ts>/`（summary.json / per_user.csv / recommendations.csv / tuning.json）。

## 七、分工

- 实现：子智能体（本计划 + 既有骨架 `heldout_als_eval.py` / `heldout_eval.py`）；
- 监工：主 Agent 验收（代码走读 + 本地测试 + 协议合规检查）；
- VM 执行：主 Agent（SFTP 上传 + nohup + 轮询收数）。
