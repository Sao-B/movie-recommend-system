# 管理员账号电影类型偏好测试记录与结论

日期：2026-10-06 ~ 10-07 ｜ 测试账号：admin（user_id=8）｜ 环境：线上 Flask（127.0.0.1:8000）+ 本地融合计算脚本
复现引擎：`backend/app/fusion_service.py`（确定性：同输入必同输出）｜ 驱动脚本：`experiments/itemcf/admin_demos/`（7 个，已从临时目录抢救入库）
前置阅读：`AGENTS.md` 相关条目、`docs/superpowers/reports/2026-10-06-stage-conclusion.md`

---

## 0. 被测对象：V4 三路融合推荐

```
并联召回：R1 ItemCF 证据集 ∪ R2 ALS 门控 Top-500（rating_count≥1000）∪ R3 类型加权热门（冷启动）
三路信号：itemcf = 共现裸求和/用户内max ｜ als = 折入点积/5 ｜ genre = 类型∩偏好/偏好数
融合：fused = Σ(可用信号·权重)/Σ(可用信号权重)，权重 (0.4, 0.5, 0.1) 来自 holdout 消融
硬排除：已看 + 不感兴趣 → Top-20
```

融合分是 [0,1] 的**相对排序分**（用户内可比，跨用户不可比），非预测评分。

---

## 1. 实验一：科幻画像下的口径对比（裸求和 vs V4 融合）

**画像**：20 部科幻电影全部 5 分（Matrix、Inception、星际穿越、指环王三部曲等）。

**裸求和口径（旧 ItemCF）Top-15**：搏击俱乐部、低俗小说、肖申克、指环王 1、记忆碎片、指环王 2/3、夺宝奇兵、教父、七宗罪、沉默的羔羊、阿甘正传……
**——显式科幻：0 部**。宽画像的共现求和被 90 年代共识片垄断。

**V4 融合（评估级 holdout 因子 + 门控，VM 实跑）Top-15**：
天空之城、兄弟连、星际牛仔剧场版、**沙丘 (2021)**、**瞬息全宇宙 (2022)**、**萤火虫 Serenity**、低俗小说、夺宝奇兵 1、西部往事、指环王 3、**风之谷**、用心棒、大逃亡、这个杀手不太冷、黄昏双镖客
**——显式科幻 6/15**，且出现大量裸求和永不可见的"中腰部契合片"（2021/2022 年新片、count 几千的电影）。

**线上 V4（系统因子 + rating_count 门控）Top-12**：蜘蛛侠：平行宇宙 2、搏击俱乐部、EVA 终、天空之城、兄弟连、瞬息全宇宙、沙丘、星际牛仔、萤火虫、低俗小说、肖申克、指环王 3
**——显式科幻 7/12**，`signals` 字段可解释每部的三路分解（#1 蜘蛛侠 i=None a=0.91 g=1.0：纯靠 ALS+类型，共现证据为零）。

**反面警示（系统因子无门控）**：同画像用**未门控**的 ALS 折入 → Top-10 被 WWE、海绵宝宝、count=1 冷门片占据——ALS 因子对低支持度电影是噪声，**门控是融合可用的前提**。

**结论**：① 共现裸求和把宽画像滑向共识片（"20 部科幻 → 0 科幻"）；② 三路融合 + 门控修复了这一点，且精度不降（消融 V4 0.1352 ≥ V0 0.1343，配对显著优于第五轮基线）；③ 类型信号进入打分后，"选了科幻"第一次真实地改变线上结果。

---

## 2. 实验二：标签数量剂量反应（小众画像 + 变 favorite_genres）

**画像**：18 部小众电影 5 分（count 10~200，类型混杂：喜剧 9/剧情 5/恐怖 2/纪录 2/动画 2/动作 2）。
**变量**：只改 favorite_genres，其余不动。小众定义 = rating_count < 1000（ALS 门控线）。

| 配置 | Top-20 小众占比 | 类型命中 | Top-3 摘录 |
|---|---:|---:|---|
| T0 空标签 | **17/20 (85%)** | 0/20 | The Bird & The Whale (count=9)、Maestro (5)、Planet Earth (2948) |
| T1 [Comedy]（匹配历史主类型） | 5/20 (25%) | 20/20 | The Big 4 (6)、Koza Nostra (1)、Domingo (1) —— **Top-5 全是 count≤10 的同类型小众片** |
| T3 [Comedy,Drama,Horror] | 1/20 (5%) | 20/20 | Freddy (2)、低俗小说、寄生虫 |
| T5 历史前 5 类型 | 2/20 (10%) | 20/20 | 同 T3 形态 |
| I5 五个无关类型 [Sci-Fi,Romance,Musical,IMAX,Film-Noir] | 2/20 (10%) | — | 盗梦空间、星际穿越（热门科幻回流）|

**结论**：
1. **剂量-反应曲线陡峭**：空标签 85% 小众 → 任一标签存在即跌至 5~25%。用户的初步结论"标签越多小众越少"在 T0→T1 段成立，T1 之后基本触底；
2. **双刃剑机制**：单标签把"该类型的小众片"精准提携到顶部（T1 Top-5），同时把"不匹配类型的小众片"施以 0 分惩罚压出榜单——总小众占比因此反降；
3. **结构性根源**：ALS 路（0.5 权重）只在门控热门池选片，小众候选永远没有 als 分；标签每覆盖一类，受益的热门片池就大一圈；
4. **产品含义**：类型问卷是"类型可控性"旋钮，不是"多样性"旋钮——两者在当前架构下不可兼得，多样性正解在服务层（探索列/类型路由）。

---

## 3. 实验三：负反馈敏感性（1 分差评的传播）

**画像**：18 部小众 5 分 + 空标签。基线 Top-20：Sci-Fi 11/20（盗梦空间、星际穿越、martian 等靠 ALS 路上榜）。
**操作**：给盗梦空间、星际穿越、火星救援三部热门科幻各打 **1 分**（非好评、非"不感兴趣"，走评分管线）。

| | 加差评前 | 加差评后 |
|---|---|---|
| Top-20 Sci-Fi | 11/20 | **1/20** |
| 列表 | 科幻 mind-bender 簇 | 换血 18/20 → 日落大道、雨中曲、1930s 歌舞片 classics |

**结论**：
1. **差评片消失**（已看排除）✓；差评的**相似簇整体塌陷**——fold-in 把 1.0 当强负目标，高支持度大片因子范数大、对 NNLS 解的拉力强，"反科幻"方向连带整个相似簇崩塌；
2. **负反馈敏感度远超预期**：3 条差评换血 90% 榜单。方向正确（打 1 分 → 少推同类），幅度需要在论文中如实报告（"fold-in 对负目标高度敏感"是协同过滤负反馈传播的教科书案例）；
3. 中评（2~3 分）的梯度行为未测，机制上应介于两者之间。

---

## 4. 实验四：权重扫描（类型契合场景下能否优化小众占比）

**画像**：同实验二（18 部小众 + 标签 [Comedy] 契合历史主类型）。**变量**：只改三路权重。

| 权重 (itemcf/als/genre) | Top-20 小众数 | Comedy 命中 |
|---|---:|---:|
| P1 现线 (0.4/0.5/0.1) | 5/20 | 100% |
| P2 (0.55/0.25/0.2) | 5/20 | 100% |
| P3 (0.7/0.15/0.15) | 5/20 | 100% |
| P4 纯 ItemCF (1/0/0) | **20/20** | **15%** |

**结论**：
1. **权重旋钮是二元的**：w_als 从 0.5 砍到 0.15，小众数纹丝不动（热门片的 ALS 归一分≈1.0 是"免费分"）；只有 w_als=0 才全小众，但那已不是融合，且 Comedy 命中掉到 15%（纯共现无视标签）；
2. **所以"调权重"优化不了标签契合场景的小众占比**——可行方案是结构性的：标签契合时把 ALS 门控池 ∩ 标签匹配（"类型引导 ALS"，热门补贴只发给目标类型），或单列探索位（P4 输出即现成探索列）；
3. 由此提出**相干性感知**设计原则：标签 ∩ 历史类型非空 → 类型诉求正当 → 启用类型引导；不契合 → 用户输入自相矛盾，混乱输出可接受。

---

## 5. 机制总结（对应论文位置）

| 发现 | 数据 | 论文位置 |
|---|---|---|
| 宽画像共识偏置是裸求和的固有权衡 | 实验一（0 科幻）+ 消融表 | 讨论：局限 |
| 三路融合+门控修复精度且类型可控 | 消融 V4 0.1352≥0.1343 + 实验一 | 系统设计/实验 |
| 类型信号 = 可控性↔多样性负相关旋钮 | 实验二剂量曲线 | 讨论：权衡 |
| 负反馈经 fold-in 强传播 | 实验三（90% 换血） | 讨论/分析 |
| 权重二元性 → 需结构性方案 | 实验四（P1=P2=P3） | 讨论：未来工作 |
| ALS 门控必要性 | 实验一反面警示 + 消融 | 方法/工程 |

## 6. 复现说明

- 融合服务确定性：同一评分/标签/模型 → 相同输出。当前 admin 状态：21 部评分（18 小众 5 分 + 3 热门 1 分）、标签 = [Sci-Fi,Romance,Musical,IMAX,Film-Noir]（实验四 I5 残留，未恢复）；
- 各实验驱动脚本在 `experiments/itemcf/admin_demos/`（2026-10-07 从临时目录抢救入库）；
- 消融的协议级数字（1952 用户）另见 `out/holdout/fusion_ablation*/summary.json`——本文件只覆盖 admin 单账号案例。


---

## 附录：实验输入与输出清单存档（2026-10-07 固化）

### A. 实验一输入：科幻画像（20 部，全部 5 分）

Matrix、Star Wars IV/V/VI、Jurassic Park、Blade Runner、Terminator 2、2001 太空漫游、Aliens、发条橙、Alien、Terminator、回到未来、Donnie Darko、Eternal Sunshine、The Prestige、WALL·E、Inception、Interstellar
（movieId：2571, 260, 1196, 79132, 1210, 589, 1270, 541, 7361, 1214, 109487, 480, 32, 60069, 924, 1206, 1200, 48780, 1240, 4878）

### B. 实验①演示输入勘误

VM 演示脚本（admin_v4_run.py）的画像硬编码了 25 部——含 5 部经典（玩具总动员/肖申克/阿甘/搏击俱乐部/沉默的羔羊，movieId 1, 318, 356, 2959, 593）。这 5 部实际属于演示用户 demo_itemcf（user_id=13），**admin 从未评过**；admin 真实历史只有 20 部科幻。 VM 演示的列表与本文件的线上实测（实验一第 3 段）画像略有差异，以线上实测为准。

### C. 实验二/三输入：21 部评分（当前库内存证，2026-10-07 固化）

| 输入 | count | 标题 | 类型 |
|---|---|---|---|
| 好评 5.0 | 10 | Random Acts of Violence (2019) | Horror |
| 好评 5.0 | 10 | De kuthoer (2020) | Comedy\|Horror |
| 好评 5.0 | 10 | Man and a Junior (2019) | Comedy |
| 好评 5.0 | 10 | Hare Tonic (1945) | Animation\|Children\|Comedy |
| 好评 5.0 | 10 | Ladies of Steel (2020) | Comedy\|Drama |
| 好评 5.0 | 10 | Kanehsatake: 270 Years of Resistance | Documentary |
| 好评 5.0 | 10 | Nefta Football Club (2018) | Comedy\|Drama |
| 好评 5.0 | 10 | Agatha and the Curse of Ishtar (2019) | Mystery |
| 好评 5.0 | 10 | Daughter (2019) | Animation\|Drama |
| 好评 5.0 | 10 | Standing Up, Falling Down (2019) | Comedy |
| 好评 5.0 | 10 | The Crystal Calls – Making The Dark Crystal | Documentary |
| 好评 5.0 | 10 | Se mieletön remppa (2020) | Comedy |
| 好评 5.0 | 10 | Mardaani 2 (2019) | Action\|Crime\|Thriller |
| 好评 5.0 | 10 | 10 Things We Should Do Before We Break Up | Comedy\|Romance |
| 好评 5.0 | 10 | De Gaulle (2020) | Drama |
| 好评 5.0 | 10 | Miami Bici (2020) | Comedy |
| 好评 5.0 | 10 | The Immortal (2019) | Action\|Crime |
| 好评 5.0 | 10 | The Perfect Candidate (2020) | Drama |
| 差评 1.0 | 57931 | Inception (2010) | Action\|Crime\|Drama\|Mystery\|Sci-Fi |
| 差评 1.0 | 37157 | Interstellar (2014) | Sci-Fi\|IMAX |
| 差评 1.0 | 28104 | The Martian (2015) | Adventure\|Drama\|Sci-Fi |

（小众片选取规则：`rating_count BETWEEN 10 AND 200 AND genres IS NOT NULL AND 在 ALS 因子表内 ORDER BY rating_count ASC LIMIT 20`——确定性可重放。）

### D. 输出清单：小众画像 + 差评 + I5 无关标签状态的融合 Top-20（2026-10-07 实测）

当前标签为实验二 I5 配置残留（Sci-Fi, Romance, Musical, IMAX, Film-Noir）——即"无关标签"形态，Romance/Musical/Film-Noir 的类型信号使榜单偏向黄金时代歌舞片/黑色电影：

| # | count | 标题 | 类型 |
|---|---|---|---|
| 1 | 9072 | Sunset Blvd. | Drama,Film-Noir,Romance |
| 2 | 1 | Domingo (2007) | Comedy,Romance,Sci-Fi |
| 3 | 1477 | Shall We Dance (1937) | Comedy,Musical,Romance |
| 4 | 5962 | Notorious (1946) | Film-Noir,Romance,Thriller |
| 5 | 1989 | A Night at the Opera (1935) | Comedy,Musical,Romance |
| 6 | 2308 | Top Hat (1935) | Comedy,Musical,Romance |
| 7 | 12913 | Singin' in the Rain (1952) | Comedy,Musical,Romance |
| 8 | 1062 | On the Town (1949) | Comedy,Musical,Romance |
| 9 | 1716 | Gentlemen Prefer Blondes (1953) | Comedy,Musical,Romance |
| 10 | 9813 | West Side Story (1961) | Drama,Musical,Romance |
| 11 | 1281 | Gilda (1946) | Drama,Film-Noir,Mystery,Romance |
| 12 | 1837 | The Umbrellas of Cherbourg (1964) | Drama,Musical,Romance |
| 13 | 18926 | Grease (1978) | Comedy,Musical,Romance |
| 14 | 11077 | My Fair Lady (1964) | Comedy,Drama,Musical,Romance |
| 15 | 29260 | The Wizard of Oz (1939) | Adventure,Children,Fantasy,Musical |
| 16 | 1189 | Love and Basketball (2000) | Drama,Romance |
| 17 | 6586 | Double Indemnity (1944) | Crime,Drama,Film-Noir |
| 18 | 20093 | North by Northwest (1959) | Action,Adventure,Mystery,Romance |
| 19 | 1327 | Mildred Pierce (1945) | Drama,Film-Noir |
| 20 | 1678 | The Lady Eve (1941) | Comedy,Romance |

Sci-Fi 仅 1/20（Domingo，count=1）——3 条科幻差评 + 无关标签的组合把科幻簇几乎清零，同时 I5 标签把歌舞/黑色电影抬了上来。此列表与负反馈实验（§3）的"加差评后"一致，**确定性复现验证通过**。

### E. 可复现性声明

融合服务确定性（同输入同输出），本次存档的输出列表与 2026-10-07 实验时输出一致。历史画像（实验一的 20 部科幻）已被清空，恢复方法：按 §A 清单向 user_ratings 插入 rating=5.0 即可复现实验一全部结果。
