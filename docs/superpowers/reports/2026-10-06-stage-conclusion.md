# 阶段结论报告：ItemCF 选型验证闭环 + 系统接入

日期：2026-10-06 ｜ 分支 `codex/als-ranking-eval`（含 ItemCF 在线接入等未提交改动）
状态：**选型验证闭环完成——ItemCF 对全部对手（人气基线、ALS±修正、自家打分变体）的优势均有同协议实测支撑；在线系统双链路可用。**

对应计划：`docs/superpowers/plans/2026-10-06-fusion-ablation.md`

---

## 一、一句话结论

**"最终选型 ItemCF"这个决定，至此拿到了三重实验保险：对人气基线（+53% NDCG）、对 ALS 及其修正形态（+47%，且 ALS 加候选门槛后仍低于人气基线）、对自家打分变体（消融证明经典聚合最优）。同时系统侧完成双链路接入与冷启动方案，全部功能有实测验证。**

---

## 二、今日完成的五块工作（均有实测证据）

### 2.1 ItemCF 接入在线链路（`backend/app/itemcf_service.py`）

- 冻结邻居图（k=100/β=0，540 万边）mmap 加载 + SHA-256 哈希链校验，只接受 `validated_unpublished`/`published` 状态；
- 实测：模型加载 0.87s（每进程一次性），单请求 **~0.02s**；
- `GET /api/recommendations/?model=itemcf` 与 ALS 链路并存，响应带 `evidence_count`/`top_source_movie_id`（可解释推荐）。

### 2.2 ALS 双模型重训（恢复线上 + 提供对照）

| 模型 | 训练数据 | 用途 | 状态 |
|---|---|---|---|
| 系统模型 `als-20261005T190626Z` | 管线标准流程（哈希切分 25.6M 行 + user_ratings 反馈 25 行） | 恢复线上 ALS 链路 | 已发布，RMSE=0.8074 |
| 对照模型（时间切分） | holdout 协议训练段 25.7M 行（`train_als_holdout.py`） | 消融/评估专用，不进系统 | 因子 jsonl 已产出 |

重要发现：宿主机库的 ALS 因子曾被清空且无备份——重训是恢复线上链路的唯一路径。

### 2.3 ALS 同协议排序评估（`heldout_als_eval.py`）

切分重建与冻结文件**字节对拍**、名单指纹对拍、求解器与部署侧**逐位对拍**（测试锁定）。
结果（1952 有效用户）：**ALS 裸排序 NDCG@20=0.000000**（冷门未训练因子霸榜，最好相关电影排第 43989 名）；加支持度门槛（≥1000）后 NDCG@20=0.069353 / Recall@20=0.086646，**仍低于 MostPop（0.085477）**。对 ItemCF 配对 bootstrap：NDCG -47.5%、Recall -40.5%，区间全部排除 0。

### 2.4 用户偏好问卷 + "不感兴趣"（migrations/014）

- `users.favorite_genres`（NULL=未选过弹窗 / `[]`=跳过）+ 新表 `user_dislikes`（推荐硬排除）；
- 首次引导弹窗、个人主页编辑、推荐卡片 ✕ 按钮，全部上线；
- 冷启动兜底升级为**类型加权热门**；三条推荐链路全部硬排除"不感兴趣"；
- 测试：新增 14 条，后端全量 902 条通过；前端 build 通过；实库 E2E 通过。

### 2.5 打分口径消融（`heldout_fusion_eval.py`，子智能体实现 + 主 Agent 验收）

同一 holdout 协议（tune 965 人调参 / test 987 人报告，防泄漏分离），候选 = ItemCF 证据集 ∪ ALS Top-500（对照因子）：

| 变体 | test NDCG@20 | test Recall@20 | 判定 |
|---|---:|---:|---|
| **V0 裸求和（冻结口径）** | **0.1343** | **0.1391** | **最优**；全量用户复现第五轮基线（0.1346 vs 0.1321，verdict=reproduced） |
| V1 平均分 | 0.0491 | 0.0654 | **-62% NDCG**（区间排除 0） |
| V2 平滑（k=10 调优） | 0.1281 | 0.1317 | Recall 显著 -5.5% |
| V3 V2+ALS | 0.1281 | 0.1317 | **调参器将 w_ALS 收敛到 0** |
| V4 V3+类型代理 | 0.1281 | 0.1317 | **调参器将 w_genre 收敛到 0** |

探针独立验证：pure_als 信号 NDCG=0.000、pure_genre=0.003（均≈随机排序器）——调参器拒绝融合是正确行为，非数据缺陷。

---

## 三、结论及其边界（论文可直接引用的表述骨架）

1. **ItemCF 优势三重成立**：对人气基线（+53% NDCG）、对 ALS 修正形态（+47%）、对自家打分变体（全部更差或无显著差异）。
2. **经典聚合最优**：标准 ItemCF 加权聚合（Sarwar et al., 2001）在消融中胜过均值化、平滑化与多信号融合变体——与 Dacrema et al. (2019)"简单基线强于复杂变体"的文献观察一致。
3. **ALS 排序失效有文献背书**：RMSE 目标与 top-N 排序脱节（Cremonesi et al., 2010），叠加低支持度因子欠拟合；这是方法边界，不是实现缺陷。
4. **已知未解**：宽画像用户的共识偏置（如"20 部科幻 → 90 年代共识片"）是裸求和的固有权衡，消融证明打分公式层无解；产品层解法（双通道：精度主列 + 类型/多样性探索列）为未来工作。
5. **长期机制**：结论的边界是"本数据规模下最优"；评估设施（冻结协议 + 脚本）可随数据规模复评，训练可迁移 Spark，共现图随数据自愈——保障靠机制，不靠承诺。

---

## 四、纪律记录（本次实验的防泄漏设计）

- 切分重建与冻结 `split_holdout.csv` **字节对拍**（sha256 一致才继续）；
- 评测名单指纹对拍；对照 ALS 绝不用系统模型（哈希切分见过约 80% 的留出考题）；
- 消融权重在 tune 半调、test 半报；V0 复现检查（reproduced）作为口径漂移哨兵；
- 评估脚本的求解器与部署侧 `recommendation_core.solve_user_vector` 逐位对拍（测试锁定）。

---

## 五、产物索引

| 内容 | 位置 |
|---|---|
| 消融评估脚本 + 测试 | `experiments/itemcf/heldout_fusion_eval.py`、`tests/test_heldout_fusion_eval.py` |
| ALS 评估脚本 + 测试 | `experiments/itemcf/heldout_als_eval.py`、`tests/test_heldout_als_eval.py` |
| 对照训练脚本 | `experiments/itemcf/train_als_holdout.py` |
| 消融产物（本地镜像） | `experiments/itemcf/out/holdout/fusion_ablation/` |
| ALS 评估产物 | `experiments/itemcf/out/holdout/als_ranking_eval/` |
| 本计划 | `docs/superpowers/plans/2026-10-06-fusion-ablation.md` |
| VM 运行根 | `/home/h/itemcf_holdout/`（fusion_ablation / als_holdout_factors / fusion_eval.log） |

---

## 六、下一步（待定优先级）

1. **代码提交**：两个分支的改动（ItemCF 接入 + 评估/训练脚本 + 前端功能）均未 commit，建议按模块拆分提交；
2. **Spark ItemCF**（可选）：训练引擎替换实验——与 scipy 版对拍 + 同协议重评，补齐"两套模型均在 Hadoop 生态训练"的叙事；
3. **论文撰写**：实验章（主表 + ALS 对比 + 消融表 + 协议描述）、系统章（四层架构 + 冷启动方案）、讨论章（共识偏置权衡 + 长期机制）——素材全部就绪；
4. 探索列（多样性/类型驱动的第二推荐位）为产品级未来工作，不阻塞论文。
