# 基于 Hadoop 生态的个性化电影推荐系统

## 项目概述

本项目以 MovieLens-32M 为数据源，构建一套完整的电影推荐系统，涵盖从数据存储、离线训练到前端展示的全链路。

推荐分两条链路：

- **离线**：HDFS 上的 MovieLens 评分 + MySQL `user_ratings` 合并后训练 ALS，产出电影隐因子与系统用户批量推荐；
- **在线**：用户提交评分后固定已训练的电影因子，只用带非负约束的最小二乘求解当前用户向量，立即返回 Top-N（不启动 Spark）。

**核心技术栈：**
- 存储层：Hadoop HDFS（VMware 里建了 hadoop102/103/104 三台，**实际只启 hadoop102 单机**，副本数 1）
- 计算层：PySpark + MLlib ALS 协同过滤（`--master local[*]` **单机多线程**；VM 上未启动 YARN，无 ResourceManager/NodeManager）
- 数据库：MySQL 8.0（电影元数据、用户评分、评论、模型与推荐结果）
- 后端：Flask RESTful API（Python 3.12，JWT 鉴权）
- 前端：Vue 3 SPA + Element Plus

---

## 目录结构

```
Project/
├── AGENTS.md              # 本文件：Agent 入口说明
├── CLAUDE.md              # 仅指向 AGENTS.md 的兼容入口
├── README.md              # 项目说明与快速启动
├── docs/
│   ├── implementation-guide.md   # 按源码核对的实现指南
│   ├── arch_diagram.html         # 系统架构图（手工维护，无生成脚本）
│   └── thesis_draft.txt          # 论文草稿（80 KB，不进 AI 上下文）
├── archive/               # 已废弃但留作参考的历史件（废弃迁移、旧设计稿），不参与构建
├── config.example.yaml    # 配置模板（不含真实凭据）
├── config.yaml            # 本地真实配置，不入 git（含凭据，值不外传）
├── config_loader.py       # 统一读取 config.yaml 的工具
├── start.bat              # Windows 开发启动入口（隐藏窗口后台；自动开浏览器 + 到点自停）
│
├── data/
│   ├── raw/               # MovieLens-32M 原始 CSV（不入 git）
│   └── processed/         # 预处理后的数据（不入 git）
│
├── hadoop/                # Hadoop 集群配置文件
│   ├── core-site.xml
│   ├── hdfs-site.xml
│   ├── mapred-site.xml
│   └── yarn-site.xml
│
├── spark/                 # PySpark 离线训练作业
│   ├── preprocess.py      # 清洗 movies.csv 并写入 MySQL movies
│   ├── train_als.py       # ALS 训练 + RMSE 评估 + 写因子/推荐 + 发布模型
│   ├── requirements.txt
│   └── utils/
│       ├── spark_session.py       # SparkSession 与 JDBC 连接串
│       ├── mysql_utils.py         # JDBC 读写 + 模型版本生命周期 SQL
│       └── training_contract.py   # 训练输入取数契约（只读 user_ratings）
│
├── sql/
│   ├── schema.sql         # 全新数据库完整建表 DDL（8 张表）
│   └── migrations/        # 已有数据库的增量迁移
│
├── backend/               # Flask API
│   ├── app.py             # 入口
│   ├── config.py          # 从根目录 config.yaml 加载配置
│   ├── requirements.txt
│   ├── assets/avatars/    # 内置 SVG 头像（10 个，清单见 routes/profile.py 的 PRESET_AVATARS）
│   ├── uploads/avatars/   # 历史上传的头像文件（已 gitignore；界面不再上传，仅存量数据在用）
│   └── app/
│       ├── __init__.py    # create_app：注册蓝图与推荐服务单例 + GET /api/health 探针
│       ├── models.py      # SQLAlchemy ORM
│       ├── db.py
│       ├── account_rules.py               # 账号命名规则（纯函数，注册 / 后台建号共用）
│       ├── rating_stats.py                # 评分档位规则（纯函数，离线脚本复用）
│       ├── recommendation_core.py         # 在线求解与排序（纯函数）
│       ├── recommendation_service.py      # 在线推荐服务（含因子缓存与降级）
│       ├── recommendation_repository.py   # 仓储层（SQLAlchemy 查询）
│       ├── itemcf_service.py              # ItemCF 在线推荐（冻结邻居图 mmap + 查表打分，2026-10-06 接入）
│       ├── movie_search.py                # 电影搜索匹配规则（ID318 / 片名 / 简介，纯函数）
│       └── routes/
│           ├── auth.py            # 登录/注册
│           ├── movies.py          # 电影浏览、搜索、筛选、类型、详情
│           ├── ratings.py         # 评分提交与历史（写 user_ratings）
│           ├── recommendations.py # 推荐列表
│           ├── comments.py        # 电影评论
│           ├── profile.py         # 个人主页（资料/统计/评论 + 头像：内置 SVG 清单与读取）
│           └── admin.py           # 管理后台（用户/电影/评分/模型）
│
├── frontend/              # Vue 3 SPA
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.js
│       ├── App.vue
│       ├── router/index.js
│       ├── layouts/AdminLayout.vue
│       ├── stores/        # Pinia stores（auth 登录态、theme 主题三档）
│       ├── api/           # 按域拆分的 Axios 封装；index.js 是全站共享实例 + 拦截器
│       ├── views/         # HomeView / MovieDetailView / ProfileView / LoginView / RegisterView / RecommendationsView
│       │   └── admin/     # AdminDashboard / AdminUsers / AdminMovies / AdminRatings / AdminModels
│       └── components/    # MovieCard / NavBar / UserAvatar / AvatarPicker / CommentSection / CommentItem / ThemeSwitch
│
├── tests/                 # Python 单元与契约测试（39 个模块、902 个用例）
│
└── scripts/
    ├── init_hdfs.sh       # 创建 HDFS 目录并上传数据
    ├── run_training.sh    # 提交 Spark 训练作业
    ├── start_backend.py   # 可复用的 Flask 启动入口
    ├── start_dev.sh       # 联合启动（自动探测 backend/venv，兼容 bin/ 与 Scripts/）
    ├── start_bg.ps1       # start.bat 调用的启动脚本：停旧进程→起前后端→开浏览器→挂看门狗到点自停
    ├── sync_code_to_vm.py # 把代码同步到虚拟机（仅本地开发，手动执行，无自动调用方）
    ├── build_ratings_clean.py # 从 ratings.csv 聚合出 ratings_clean.csv（--check 可比对）
    ├── build_ml_rating_stats.py # 聚合五档分布写入 movie_rating_stats
    ├── import_movie_stats.py # 把 ratings_clean.csv 的均值/人数回填 movies 表
    ├── check_db.py        # 各表行数与活跃模型检查（2026-09-17 从根目录移入）
    └── fetch_posters.py   # TMDB 富化回填（tmdbId 直查 + 搜索级联，进度=库里缺口）
```

> 2026-09-16 结构清理：`api/client.js` 已并入 `api/index.js`（全站单一 axios 实例，错误 toast 统一在拦截器弹）；`stores/user.js`、`views/MoviesView.vue`、`GET /api/profile/ratings` 死接口已删除。

> 2026-09-16 脚本合并：TMDB 抓取脚本二合一，`fetch_missing_by_search.py` 已删除，其搜索级联（`imdbId` → `/find`、标题搜索等）并入 `fetch_posters.py`。合并时去掉了从未被调用的 `fuzzy_match()` 死代码。同日取消进度记录：临时的 `tmp_search_queue`、`tmp_movie_fetch_queue` 都不再用（活库无任何 `tmp_*` 表），脚本改为"数据库即进度"——待办与失败记录就是 `movies` 里缺 `poster_url`/`overview`/`director` 的行，一条 SQL 可查，运行中网络异常直接跳过该条、不留状态。

> 2026-09-18 启动顺序修好（首页要等一会儿才有数据）：`start.bat` 早先是「等前端端口能连上就开浏览器」，而 vite 两秒就绪、**Flask 冷启动（重启机器后第一次）实测 ~20s 才监听端口**（热启动 ~1.3s），于是首页那次 `/api/movies/?page=1&per_page=24&sort=popular` 直撞 vite 代理的 `ECONNREFUSED`（`logs\vue.err.log` 可查），列表空着、只能手动刷新。现在：① 后端加 `GET /api/health`（不鉴权，内含一次 `SELECT 1`，库不通返回 503 `degraded`）② `scripts/start_bg.ps1` 轮询它，通过后把首页那次查询打热（首查 1.35s → 之后 0.13s），再等前端端口、开浏览器 ③ 前端 `api/index.js` 兜底：遇「连不上后端」（无 response 且非超时）对 GET 退避重试两次，写请求与超时不重试。用例：`tests/test_startup_contract.py`（health 200、探针用 $flaskPort、开浏览器排在等后端之后、预热 URL 与 HomeView 首次请求一致）+ 前端 `api/__tests__/index.spec.js` 4 条。

> 2026-09-18 头像改成内置 SVG：个人主页点自己的头像不再选本地文件，改为从 **10 个内置 SVG 头像**里挑（文件 `backend/assets/avatars/*.svg`，清单是 `routes/profile.py` 的 `PRESET_AVATARS`，前端 `AvatarPicker.vue` 渲染网格），点一下即调 `PUT /api/profile/info` 落库；面板底部「恢复默认」＝把 `avatar_url` 置 `null`（前端回落显示名首字符）。接口侧 `avatar_url` 只接受清单里的地址或 `null`，其它值一律 400；切走时会删掉该用户历史上传的那张图（注意 `/api/profile/avatars/` 不以 `/api/profile/avatar/` 开头，内置头像不会被误删）。上传接口与读取路由保留（存量数据的来源 + 手工调用），前端 `uploadAvatar` 已删除。用例：`tests/test_profile_avatar.py`（清单完整性、逐个可取、非法名 404、PUT 接受预设 / 恢复默认 / 拒绝外链、切换后旧图被删）+ 前端 `AvatarPicker.spec.js`、`ProfileView.spec.js`。

> 2026-09-18 账号与昵称分离：`users` 加 `nickname`（migrations/011），**账号 `username` 退化成纯登录凭据**——注册时按 `backend/app/account_rules.py` 的规则命名（3-20 位，只允许字母 / 数字 / 下划线 / 连字符），此后**接口上不可改**（`PUT /api/profile/info` 带 `username` 一律 400）；个人主页能改的是**昵称**（任意字符、不唯一、可清空），页面上显示的人名统一走 `display_name`（昵称 or 账号——后端 `User.display_name`、前端 `auth.displayName`，导航栏 / 影评署名 / 个人主页都取它）。登录接口的 `username` 字段现在同时接受**账号或用户 ID**（纯数字时先按账号精确查，没有才按 ID 查，见 `auth._find_user`），所以个人主页那个「ID: 8」可以直接拿去登录；后台建号与改账号走同一套字符规则。用例：`tests/test_auth_routes.py`（规则边界 + ID 登录）、`tests/test_profile_info.py`（昵称读写 + 账号不可改 + 影评署名跟昵称）。影评响应另补了 `avatar_url`（`MovieComment.to_dict`），评论卡片左侧据此渲染作者头像、没头像时回落显示名首字符。

> 2026-10-01 **⚠️ 以下两条门槛记录已废弃（代码已删除，保留作历史教训）**：`IDENTIFIABILITY_MIN_RATINGS`、`als.min_rating_count`、`ALS_MIN_RATING_COUNT`、`get_movie_ids_with_min_ratings()`、`_candidate_movie_ids()` 的筛选逻辑**已全部移除**，在线推荐回到**全量因子排序**。下方两条记录的机制描述**不再反映当前代码**，仅作为"曾如何理解这个问题"的存档。**注意其中仍然有效的部分**：λ 口径必须与 MLlib 训练一致（`effective_reg_param`）——那是**正确性**问题，与门槛无关，**仍然生效，不要一起删**。
>
> 2026-09-20 在线推荐加候选集门槛【已废弃】：评分个位数的电影 ALS 因子近乎未训练，点积分数虚高到 8~11，把 Top-N 头部全占满（实测不限时 Top20 全是评分人数 1~6 的冷门片）。`OnlineRecommendationService` 新增 `min_rating_count`（默认 1000，来自 `config.yaml` 的 `als.min_rating_count`，置 0 关闭），排序前由仓储的 `get_movie_ids_with_min_ratings()` 把候选从 8 万部收窄到约 4.4 千部；筛选后不足 `top_n` 时回退全量并打 warning，保证列表不空。真库复测：用户 8/11 的 Top-20 最低评分人数从 1~6 升到 1068/1544，8 号出《简爱》《理智与情感》一类，11 号出《低俗小说》《沉默的羔羊》《银翼杀手》《潜行者》。**仍待处理**：极端画像（如全 5 分用户）的 `predicted_rating` 依旧全部顶格 5.0——原始分有 1.3~1.6 的跨度，是 `clamp_rating` 硬截断抹掉了展示区分度。用例：`tests/test_recommendation_service.py`（过滤生效 / 阈值 0 关闭 / 候选不足回退）。

> 2026-09-21 在线正则口径对齐 + 门槛改性质（上面那条的「仍待处理」已解决）【门槛部分已废弃，λ 部分仍生效】：
> ① **λ 口径 bug**——MLlib 自 [SPARK-2085](https://github.com/apache/spark/commit/a6e0afdcf0174425e8a6ff20b2bc2e3a7a374f19.patch) 起，ALS 每步求解的正则按评分数缩放（`numRatings(i) * lambda`），而 `recommendation_core.py` 拼的是固定量 `sqrt(reg_param)`。**两侧解的不是同一个目标函数**，等于"复用了按 A 目标训出的物品因子、却在 B 目标下求用户向量"。实测 300 个真实用户：两种口径解出的用户向量余弦相似度中位数仅 **0.741**（方向差 25%+）；固定口径下向量模长中位数 **4.101**，而全库物品因子模长中位数才 **1.563**——尺度根本不在一个坐标系里，点积自然溢出。改为 `sqrt(reg_param * n_ratings)`（新函数 `effective_reg_param`）后模长降到 **2.479**，溢出（raw>5.0）从中位数 0.9% 降到 **0.0%**，Top-20 中被截断成整数 5.0 的条数从 **15.34/20 降到 0.27/20**——展示区分度回来了。
> ② **门槛从「流行度先验」改成「可辨识性下限」**，值 1000 → **50**（`IDENTIFIABILITY_MIN_RATINGS` 常量，与 `get_popular_movies` 兜底口径统一）。原来的 1000 砍掉 94.5% 候选、只剩 4.4 千部，且理由是"冷门片不该推荐"——**这是人为假设"大众=用户爱看"**，而流行度与偏好几乎无关（Spearman(预测分, 评分人数) 仅 0.283）。改后理由变成"rank=50 的因子有 50 个待估参数，观测数远小于此则不可辨识"，筛的是**因子有没有被训练**，不是**够不够受欢迎**。候选回到 **16,034 部**（20.0%），Top-20 支持度中位数 **688**，不再出现「20 部里 15 部被截断成 5.0」的矛盾画面。
> ③ **诚实记录一条证伪**：λ 口径是**正确性**问题（必须修），但**不是排序问题的解药**。实测 300 用户，修 λ 后 Top-20 中 support<5 的占比反而从 63.6% 升到 95.6%——因为低 support 物品的因子**本身就没有信息**（秩不足），改正则只改尺度、改不了这一点。真正把 cold-item 从 Top-20 清出去的是候选门槛（gate=50 时 support<5 占比 **0.0%**）。这与 `docs/online-recommendation-audit.md` 的 C 变体（Recall 0.00000）一致，勿再把 λ 当成排序提升手段。
> 用例：`tests/test_online_recommender.py::OnlineRegularizationAlignmentTests`（缩放律 / 评分数越多向量越短 / 向量与物品因子同尺度 / 点积不溢出）+ `test_recommendation_service.py::test_gate_default_matches_identifiability_floor`。

> 2026-10-06 **融合实验设计修正（第二轮）：网格缺正确答案的bug + V4 三路融合精度不降反微升**：发现第一轮消融的设计缺陷——融合里的 ItemCF 信号用了平滑分（÷(count+k)），导致权重网格中**根本不存在裸求和这个点**（k=0 的"平滑"=平均分），"精度不降"的选项从未进过考场。修正：融合的 ItemCF 信号改为裸求和逐用户 max 归一（w=[1,0,0] 严格等价 V0），并新增类型贴合度指标。重跑结果：**V4 三路融合（w=[0.4, 0.5, 0.1]）test NDCG@20=0.135154 > V0 0.134289**（对第五轮基线配对 +0.0042 [+0.0006,+0.0078] 显著），Recall 0.1403 亦更高，且全部 1952 用户的推荐列表与 V0 不同——**三路融合（ItemCF 0.4 + 门控 ALS 0.5 + 类型 0.1）精度不降反微升，类型信号正式进入打分公式**。V3（w=[0.1,0.9,0]）与 V0 的推荐列表在全部 1952 用户上完全一致（已验证）。**部署警示**：线上系统模型（哈希切分）的 fold-in 存在冷门退化，admin 用其因子做 V4 融合的 Top-10 被冷门垃圾片占据——线上启用融合打分需先按 `rating_count>=1000` 门控 ALS 候选（元数据在线可得）或系统模型改时间切分重训。产物 `out/holdout/fusion_ablation_v2/`。
>
> 2026-10-07 **admin 小众画像实验组（类型可控性 vs 多样性的定量数据）**：两个对照实验，均针对 V4 融合（w=[0.4,0.5,0.1]）在线实测。①**标签数量剂量反应**：固定 18 部小众评分历史（count 10~200、类型混杂），只变 favorite_genres——空标签 Top-20 小众占比 **17/20 (85%)**，1 个匹配标签 **5/20**（且 Top-5 全是 count≤10 的同类型小众片：标签精准提携目标类型的小众片），3~5 个标签 **1~2/20**（标签集越大受益热门片池越大），5 个无关标签 2/20。结论：**类型信号是"类型可控性 vs 多样性"的负相关旋钮，每开一分压一分多样性**；结构性根源=ALS 路（0.5 权重）只在门控热门池选片。②**负反馈敏感性**：3 条热门科幻 1 分差评 → Top-20 换血 18/20、Sci-Fi 11/20→1/20——fold-in 把 1.0 当强负目标，高支持度大片因子范数大拉力强，负反馈传播剧烈（协同过滤教科书案例，写进讨论节）。实验脚本：`D://_workbuddy_tmp//tag_matrix.py`、`fusion_experiment.py`、`neg_rating_test.py`（临时目录，每日 03:00 清理）。admin 实验后状态：18 部小众 5 分 + 3 部热门 1 分、标签空。**完整记录文档**：`docs/superpowers/reports/2026-10-07-admin-genre-tests.md`（含全部 Top-20 列表与结论，驱动脚本已入库 `experiments/itemcf/admin_demos/`）。
>
> 2026-10-07 **Spark ItemCF 训练（进行中）+ 交接文档**：PySpark 版 ItemCF 训练作业已实现（子智能体交付：`spark/train_itemcf.py`、`spark/utils/itemcf_math.py` 纯 NumPy 数学核心、`scripts/run_itemcf_training.sh`、`tests/test_spark_itemcf_core.py` 16 测试——数学核心与 `core.py` 在 β×k 六组合下**全图逐位相等**）。VM 上已跑多轮：失败均为部署环境问题（HDFS 路径劫持→`file://` 前缀、CRLF 行尾、cloudpickle worker 按引用导入模块→`executorEnv.PYTHONPATH`+局部 import 双修复、`itemcf_math.py` 需在项目根 utils/ 有一份），**非算法问题**。第四轮运行中。**完整交接文档**：`docs/superpowers/reports/2026-10-07-spark-itemcf-handover.md`（自包含：在飞状态、验收三连 verify→equiv_check→heldout 复现 0.1321、五坑清单、环境参数）——接手的智能体从它开始。VM 产出：`/home/h/itemcf_out/itemcf_spark/<run_id>/`。
>
> > 2026-10-06 **门控融合消融（第二轮）：ALS 信号门控后复活但仍不敌裸求和；覆盖率在精度等价约束内无法提升**：`heldout_fusion_eval.py` 加入 `--als-min-support`（ALS 召回路支持度门控——未训练冷门因子的极端点积会让 ALS Top-K 被垃圾片占满，信号退化为随机，这是第一轮 w_ALS→0 的根源）与覆盖率指标（test 半 Top-20 去重电影/catalog）。重跑结果（tune 965/test 987）：门控后 ALS 信号复活（V3 调参权重从 [1.0,0.0] 变为 **[0.1,0.9]**，ALS 拿到 90% 权重），但 V3 NDCG=0.1281 仍低于 V0 裸求和 0.1343；V4（+类型）权重 [0.9,0.0,0.1]、Recall 0.1337 略高于 V2；V5 覆盖率导向选择（w=[0.7,0.1,0.2]）NDCG=0.1232/Recall=0.1321。**覆盖率全部未超 V0**（V0=688 部/0.79%，V2/V3=652/0.74%，V1 平均分=2264/2.58% 但中位人气仅 27、精度 -62%）。结论：① 打分公式层的所有修正（均值/平滑/融合/类型）在精度-覆盖率权衡下均不敌经典裸求和，开题"混合提升覆盖率"在精度等价约束内被实验证伪——覆盖率的正解在服务层多样性策略与冷启动类型路由（后者已上线）；② 工程发现：**异源信号必须支持度门控后才可用**（未门控=随机噪声），`--als-min-support` 参数固化该机制。产物 `out/holdout/fusion_ablation_gated/`。
>
> 2026-10-06 **打分口径消融：裸求和是最优公式，融合/归一化全部被否**（`heldout_fusion_eval.py`，子智能体实现+主 Agent 验收）：同一 holdout 协议下对比五种打分变体（候选=R1 ItemCF 证据集 ∪ R2 ALS Top-500，ALS 用时间切分对照因子）。结果（tune 965 / test 987 用户，权重在 tune 半网格搜、test 半报告）：**V0 裸求和 test NDCG@20=0.1343 / Recall=0.1391 为最优**，且 V0 在全量用户上复现第五轮基线（0.1346 vs 0.1321，差 0.0025<0.005，verdict=reproduced）；V1 平均分 **-62% NDCG**（区间排除 0）；V2 平滑(k=10) Recall **-5.5%**（显著）、NDCG -2.2%（不显著）；V3 融合 ALS、V4 融合类型代理——**调参器自动把 w_als、w_genre 收敛到 0**（与 V2 完全同值），即 ALS 折入与类型代理在离线协议下无增量。结论：admin"宽画像滑向共识片"是个例代价，整体上裸求和就是最优；冻结口径经受住消融检验，**三期"接入线上"取消——保持裸求和**。产物 `out/holdout/fusion_ablation/`（summary/tuning/per_user/recommendations），计划 `docs/superpowers/plans/2026-10-06-fusion-ablation.md`。教训：子智能体交付的脚本在 VM 上缺 `--itemcf-model-dir` 必填参数与 backend/app shim，监工补齐后跑通——交接时参数清单要逐项核对。
>
> 2026-10-07 **V4 三路融合接入线上（第三个推荐开关）**：`backend/app/fusion_service.py` 新增 FusionService——并联召回（ItemCF 证据集 ∪ ALS 门控 Top-500，支持度门槛用 `Movie.rating_count >= als_min_support`，线上元数据直接可得）+ 三路信号（ItemCF 裸求和 / ALS 折入点积 / 类型匹配，各自候选内 max 归一，缺失退出加权）+ 权重融合（config `fusion.weights`，默认消融最优 [0.4, 0.5, 0.1]）。`GET /api/recommendations/?model=fusion`，响应含逐路 `signals` 分解与 `evidence_count`；前端推荐页第三个开关"混合融合"。admin（25 部科幻 5 分）实测 Top-12 中 7 部显式科幻（蜘蛛侠平行宇宙 2、EVA、天空之城、沙丘 2021、瞬息全宇宙、Cowboy Bebop、萤火虫）——纯 ItemCF 口径下这些片因无共现证据永不可见，ALS 路与类型信号把它们捞了回来；温请求 0.43s。坑位记录：recommendation_repository 此前无 `db` 导入（`get_movies_above_support`/`get_movie_genres` 用到），漏 import 已补；ORM 对象不能元组解包（`for m, r in ratings` 对 UserRating 会炸），统一走属性访问。
>
> 2026-10-07 **推荐页三条链路拆成独立网址**：推荐页原先三个按钮只是组件内 state 切换（URL 一直是 `/recommendations`），现在改成「一个模型一个网址」——路由加 `path: '/recommendations/:model(als|itemcf|fusion)'`（name `recommendation-model`，带 `meta.requiresAuth`），老链接 `/recommendations` 重定向到 `/recommendations/als`；页面不再持有 `currentModel`，改由 `route.params.model` 派生（不是这三个值就回落 als，不会拿脏值打接口），点按钮 = `router.push`，取数交给 `watch(currentModel, load, { immediate: true })`。好处：刷新、新开标签、分享链接都停在同一路推荐，浏览器前进/后退可用。用例 `frontend/src/views/__tests__/RecommendationsView.spec.js`（5 条：按钮文案、按网址取数、点按钮走路由跳转、网址变更重新拉取、非法模型回落 ALS）。
>
> 2026-10-07 **「不感兴趣」改成两段式就地确认（去掉浮窗）+ 菜单项红色**：按用户要求"点一下变『确定吗？』、再点才标记"。`MovieCard.vue` 的 `⋯` 菜单加 `:hide-on-click="false"`（否则点一下菜单就收，第二下没地方点）+ `dislikeArmed`：第一下只把菜单项变成红字加粗的「确定吗？」并 return，第二下才 `emit('dislike')`；`visible-change(false)`（点了别处）复位，避免红色确认态一直挂着。红色第一版写成 scoped 的 `.dislike-item[data-v]` + 覆盖 Element Plus 变量，**实测完全不生效**（用户截图反馈「并没有变为红色底色」）：scoped 的 data-v 只会落在直接子组件的根节点上，追不进「teleport 到 body + 由 EP 内部子组件渲染」的菜单项 li；而且 EP 的常态色取自 `--el-text-color-regular`，压根没有 `--el-dropdown-menuItem-color` 这个变量。**现已改为**：`el-dropdown` 加 `popper-class="movie-dislike-menu"`，红色写在 `MovieCard.vue` 末尾一段**非 scoped** 样式里——常态 `.movie-dislike-menu .el-dropdown-menu__item { color: var(--el-color-danger) }`（2 级压 EP 的 1 级），hover/focus 用 `:not(.is-disabled)` 凑到 4 级压 EP 的 3 级，确认态**只加粗红字**（`is-armed` 不再带常驻浅红底——否则鼠标移开菜单项红底还挂着，用户觉得怪；浅红底留给 hover 反馈）。已用无头 Chrome 引构建产物 CSS 做静态渲染实测：常态红字、确认态红字加粗带浅红底（EP 的 dropdown 规则无 `!important`，具体度即可取胜）。推荐页 `onDislike` 删掉 `ElMessageBox.confirm`（连同 import），收到 dislike 直接 `POST /api/dislikes/` + 把卡片从当前列表摘掉。**测试坑（重要）**：jsdom 下只要 stub `teleport`，ElTooltip 的定位逻辑就会反复触发更新并抛 `Maximum recursive updates exceeded in <ElDropdown>`（错误落在 app errorHandler 里，看着像实现 bug、其实是环境）——菜单默认 teleport 到 body，所以 spec 不 stub teleport，改 `document.querySelector('.dislike-item')` 断言 + `afterEach` 清 body，并走**真实点击**（点 `.more-btn` 开菜单、再 dispatch 菜单项 click），直接 `$emit('command')` 会绕过 ElDropdown 自己的逻辑、测不出 hide-on-click 到底有没有用。用例：新增 `frontend/src/components/__tests__/MovieCard.spec.js`（6 条），`RecommendationsView.spec.js` 补 1 条（确认后直接入库并摘卡），前端 40 条全绿。
>
> 2026-10-06 **用户偏好问卷 + "不感兴趣"（冷启动信号）**：migrations/014——`users.favorite_genres`（JSON 数组，最多 5 个；**NULL=从未选过（弹首次引导）**，`[]`=明确跳过（不再弹））+ 新表 `user_dislikes`（推荐**硬排除**集合，unique(user_id,movie_id)）。校验用 `app/genres.py` 的 19 种标准类型常量；`PUT /api/profile/info` 接受 `favorite_genres`（非空列表 1..5 项、`[]` 跳过），`User.to_dict()` 增加 `favorite_genres`/`needs_onboarding`。新路由 `app/routes/dislikes.py`：`POST /api/dislikes`（幂等）/`DELETE /api/dislikes/<id>`/`GET /api/dislikes`。推荐链路三处硬排除"不感兴趣"：ItemCF 服务（并入 seen）、ALS 在线排序（并入 excluded）、批量/热门兜底（过滤）；冷启动兜底升级为**类型加权热门**——`get_popular_for_user()` 先在自选类型内按均分取（不足补全局热门），ItemCF 空结果也共用它。前端：`GenrePickerDialog.vue`（类型标签多选，首次引导与个人主页编辑共用）、`App.vue` 登录后拉资料按 `needs_onboarding` 弹一次、ProfileView 加"喜欢的电影类型"整宽一条（`genres-band`，标签 + 右侧主色「修改偏好」按钮，同日稍后从信息区末尾的小链接提上来）、MovieCard 加 `dismissible` 悬停"✕"按钮（推荐页专用），`api/dislikes.js`。测试 `tests/test_user_preferences.py`（14 条：引导语义/校验/幂等/硬排除/类型加权热门补位）。全量后端 902 测试通过；前端 build 通过。类型匹配对 ItemCF/ALS 候选池的精排加权留待级联实验（二期）。同日稍后两处 UI 调整（纯前端）：① 个人主页的偏好入口原先是信息区末尾一个 `link` 小按钮、太不显眼，改为资料卡底部**整宽一条 `genres-band`**（浅底 + 描边 + 星形图标，「修改偏好」用主色实心按钮）；② 顶栏用户名旁的下拉**箭头图标整个删掉**——`ArrowDown` 导入、`.arrow-icon` 及其随机方向旋转动画 CSS、`dropdownVisible` / `@visible-change` 一并移除，触发区就是头像 + 显示名（README 10.4 那条"下拉箭头随机方向旋转动画"已同步删掉，它描述的正是这个箭头）。同日更晚（偏好可视化第二轮）：③ MovieCard 的悬停"✕"按钮（推荐页 `dismissible`）换成**常驻的横向三点「更多操作」按钮**——位置从海报右上挪到内容区右上（与年份行齐平，参考用户截图红框），点开 `el-dropdown`（横向 `More` 图标）选「不感兴趣」才 emit `dislike`，父组件 `onDislike` 流程不变；④ 个人主页把 `genres-band` 升级成独立的**「偏好设置」卡**（资料卡与观影统计之间）：上半区类型偏好（标签 + 「修改偏好」），下半区"不感兴趣的电影"管理——为此 `GET /api/dislikes/` 从只回 `movie_ids` 扩成**附 `items`（`{movie_id,title,year,genres}`**，电影行可能已被删则 title=None），`movie_ids` 原样保留；每条可点「取消标记」走 `DELETE`。测试：`tests/test_user_preferences.py` 的列表断言改为精确比对 `items`；前端 `ProfileView.spec.js` 新增 3 条（拉取展示/取消标记落库/空状态），前端用例 23→27 且 `npm run lint` 清零（顺带删掉 `GenrePickerDialog.vue` 未使用的 `ElMessage` 导入）。同日收尾（用户反馈两处）：⑤ ⋯ 按钮改样式——`More` 换 **`MoreFilled` 实心点**、去掉浅色圆底（`background: transparent` 直接融进卡片），悬停染 `--el-color-primary` 并轻微放大；⑥ **标记与取消标记都加 `ElMessageBox.confirm` 二次确认**（推荐页 `onDislike` 与个人主页 `removeDislike`，取消则什么都不动），确认框按钮「不感兴趣 / 取消标记」+「再想想」；前端用例 27→28（新增「确认框点取消 → 不调 DELETE、行保留」）。
>
> 2026-10-06 **ALS 同协议排序评估：ItemCF 优势对 ALS 也成立**（分支 `codex/als-ranking-eval`）：`experiments/itemcf/heldout_als_eval.py` 把 ALS（rank50/reg0.1/maxIter10，RMSE 训练）放到与 ItemCF **完全相同**的 holdout 协议下评 NDCG@20/Recall@20——复用 `RatingSession` 重建切分并与冻结 `split_holdout.csv` **字节对拍**（sha256 一致才继续）、复用冻结 2000 人名单（名单指纹对拍）、指标同用 `per_user_metrics`；用户向量按部署同款非负最小二乘（λ=reg×n_ratings），求解器在实验侧复刻并以 `tests/test_heldout_als_eval.py` 与 `recommendation_core.solve_user_vector` **逐位对拍**。同批对照训练 `train_als_holdout.py`：在 holdout 训练段（25.7M 行）上重训 ALS（防泄漏——管线哈希切分与时间留出独立，管线模型见过约 80% 的留出正例），因子落 jsonl，不进系统。结果（1952 有效用户）：**ALS 裸排序 NDCG@20=0.000000**（冷门未训练因子霸榜，最好相关电影排第 43989 名——09-20 记录的病在全量规模复现）；**ALS+支持度门槛(≥1000，2090 候选) NDCG@20=0.069353 / Recall@20=0.086646，低于 MostPop(0.085477)**；对 ItemCF 配对 bootstrap：NDCG -47.5% [-0.0699,-0.0561]、Recall -40.5% [-0.0678,-0.0504]，区间全部排除 0。产物镜像 `out/holdout/als_ranking_eval/`。同晚系统管线重训发布 `als-20261005T190626Z-15df5d33fbc2`（RMSE=0.8074，训练含 user_ratings 反馈 25 行），ALS 在线链路恢复。教训沉淀：评估脚本对 `per_user.csv` 列名做了裸名/`ItemCF_` 前缀双兼容。
>
> 2026-10-06 **ItemCF 接入在线链路（可选旁路，不替换 ALS）**：`backend/app/itemcf_service.py` 新增 ItemCF 在线推荐——读 `experiments/itemcf` 全量冻结邻居模型（k=100、β=0，本地目录由 `config.yaml` 的 `itemcf.model_dir` 指向，留空 = 不启用），mmap 加载 + SHA-256 哈希链校验（仅接受 `validated_unpublished`/`published` 状态），按用户好评历史（rating≥4.0）查表累加相似度打分。**打分与 `experiments/itemcf/core.score_user` 同口径**：score 为排序分不是预测评分、排除已看、`(-score, movieId)` 排序、短列表合法不补热门、浮点按来源 movieId 升序累加。接口：`GET /api/recommendations/?model=itemcf`（默认缺省仍走 ALS，两条链路并存），响应条目带 `score`/`evidence_count`/`top_source_movie_id`；推荐页加"ALS / ItemCF"切换，ItemCF 卡片显示"匹配度"（相对列表最高分归一的百分比）。性能实测：模型加载 0.87s（每进程一次性，懒加载不拖启动），单请求 ~0.02s。真实库验证：好评《玩具总动员》《肖申克》《阿甘正传》《搏击俱乐部》《沉默的羔羊》→ 出《低俗小说》《黑客帝国》《七宗罪》《教父》等经典邻居链，每条 5 个证据来源。本服务**不 import experiments 包**，只按 model.json 契约读数组。用例：`tests/test_itemcf_online.py`（9 条：打分口径/排除/短列表/并列 tie-break/状态与哈希与结构校验拒绝）。
>
> 2026-10-07 **ItemCF 训练层迁到 PySpark，双引擎对拍全等，线上已切到 Spark 产物**（分支 `codex/als-ranking-eval`）：新增 `spark/train_itemcf.py`（566 行，`spark-submit --master local[*]`，VM hadoop102 单机）与 `spark/utils/itemcf_math.py`（纯 NumPy 数学核心，与 `core.py` 逐位一致），输入 MovieLens-32M 全量 32,000,204 行，**无时间切分**（全量训练，与 scipy 版口径相同），`timestamp` 只做格式校验。**产物** `run_id=20261007T095257Z-e7b8ce95`，5,425,546 边 / 84,432 物品 / 55,174 有邻居来源，状态 `validated_unpublished`，`model.json` 的 `parameters.engine="pyspark"`——可用它确认线上加载的是哪一版。**已从 VM 拉回本地 `experiments/itemcf/out/full/20261007T095257Z-e7b8ce95/model/`（7 npy + model.json，127 MB），`config.yaml` 的 `itemcf.model_dir` 已切过去**（前一版 `20261004T195616Z-d535c178` 原地保留可回退）；拉取时三方核对哈希（本地重算 vs VM `model.json` 记录）全部 MATCH，本地 `load_model(validate=True)` 与后端 `ItemCFGraph(verify_hashes=True)` 均通过（84432 物品 / 542 万边）。
> ① **踩坑（唯一致命的一条：分区数与内存不匹配 → 内核 OOM killer）**：`make_edge_partition_fn` 把整个分区的候选边攒进纯 Python `dict {src: ([tgt...], [c...])}` 才做 Top-100 截断，内存 **∝ 分区行数**（约 72B/边）。实测共现 distinct 无序对 142,418,942 条、对称展开后 2.85 亿行进 `mapPartitions`；`shuffle.partitions=8` 时每分区 3560 万行 → dict 2.56 GB，2 核并发峰值 ~5.1 GB > 4 GB 物理内存 → **17:48:50 被内核杀掉**（`dmesg` 里 `Out of memory: Kill process ... (java)`；日志末尾的 `BlockManager: Putting block ... Connection reset` 是**后果不是原因**）。**修法**：`shuffle.partitions` 8 → 32（每分区 0.64 GB、2 并发 ~1.3 GB），71 分钟跑完，峰值 swap 2.7 GB。**别改回 8、别加核**——加核是让更多 task 各持一份 dict，是乘法。该参数**只影响内存、不影响结果**（`repartition(N,'src')` 保证同一 src 的边不跨分区，Top-K 不跨分区）。备用实现 `make_streaming_edge_partition_fn`（`--edge-mode streaming`，只保留当前一个 src 的候选，峰值约 6 MB，与总量解耦）已写好且单测覆盖，但**本轮没用上、未同步到 VM**。
> ② **四步验收（全绿）**：`verify_full_model` `exit=0`（从原始 3200 万行**独立重算** 100/100 抽样来源，0 不符）；**新增** `experiments/itemcf/compare_models.py` 做双引擎逐边对拍（整数数组零容差、相似度 1e-12 相对容差，报第一个差异位置；`pass=true`、`mismatches=[]`、`max_relative_similarity_gap=0.0`）；`heldout_eval.py` **新增 `--model-dir`**（原先只能现算共现图，见下）跑全量指标 **NDCG@20=0.134616 / Recall@20=0.198116**（第五轮基线 0.132076，判定 `advantage_holds_at_full_scale`）；**V0~V4 六个融合变体在 Spark 图与 scipy 图上逐位相同**（V4 `0.136368`、权重 `[0.4,0.5,0.1]`，精确复现 `fusion_ablation_v2`）。另在**小规模**（从全量重抽的 2000 个非排除名单用户 / 31.1 万行）上做了同样对照：产物 1,026,020 边全等、三个方法指标九位小数全等——**V3/V4 在小规模下退化成 V0**（`als_min_support=10` 且 ALS 因子是全量训练，错配导致权重被压到 `[1,0,0]`），全量下才恢复 `[0.4,0.5,0.1]`。
> ③ **两处旧脚本的接口缺口（已补）**：`equiv_check.py` 与 `heldout_eval.py` 都没有"比较/加载外部模型目录"的参数——前者 argparse 只有 `--input/--baseline-dir/--out`（机制是"从原始评分重算再与 holdout 基线比"，**不能比两个模型目录**），后者原���只在脚本内现算共现图。`heldout_fusion_eval.py` 则**本来就支持** `--itemcf-model-dir`。新增的 `load_cooccurrence_from_model_dir` 会**校验模型 `item_ids` 与评测 `item_ids` 逐元素相同**（下标 CSR 语义，错序会静默产出"看起来正常但全错"的图，故必须硬失败），返回的 `stats` 键与 `build_cooccurrence` **完全对齐**（`edges`/`seconds`/`items`/`max_k`，`seconds` 必须是 float——见 ④）。
> ④ **今天连续三次"加新路径没对齐旧契约"的自伤**（都靠跑全量才发现，教训是**先单跑新代码那一步验证再启动全量**）：(a) `yield` 写在了 `make_streaming_edge_partition_fn` 的**外层函数体**里 → 整个函数成了生成器函数，`return _partition_edges` 返回的是生成器对象而非函数，`mapPartitions` 拿到不可调用物；(b) `stats` 返回 `n_edges` 而下游取 `cstats['edges']` → `KeyError`，**图已正确加载却在下一行崩**，12 分钟的活全白费；(c) 为"不编数"把 `seconds` 设成 `None`，下游 `'%.1fs' % cstats['seconds']` → `TypeError`（**"不撒谎"和"能用"不是一回事**，加载外部图本来就有成本，实测 0.2 s，测出来就是事实）。修法统一为"**逐字段对齐旧路径返回值 + 用真格式化试一遍**"，并把 `compare_models` 之外再补 `tests/test_heldout_model_dir.py`（5 条，重点是顺序错乱必须抛 `CorrectnessError`，以及拿现算路径的键集做子集对照）与 `tests/test_spark_itemcf_streaming.py`（7 条，流式 vs dict 逐边相等 + 钉住"缺 sort 会静默产出超 max_k 的图"这一失效模式）。本地 21 + 33 测试全绿。
> ⑤ **别踩的其它坑**：Windows 传脚本到 VM 必须转 LF（坑位 #2）；executor Python worker 不继承 driver 的 `sys.path`，分区函数内**局部** `import utils.itemcf_math`（坑位 #3）；VM 侧多行逻辑一律写成 `.sh`/`.py` 用 sftp 上传，别用内联引号（坑位 #5）；**`pilot_ratings.csv` 的 2000 个用户 100% 落在历史排除名单里**（实测重叠 2000/2000），不能用于评测——要做小规模对照得从全量重抽排除名单外的用户。交接文档 `docs/superpowers/reports/2026-10-07-spark-itemcf-handover.md` 已同步更正（补 OOM 坑位、纠正那两条不存在的命令行、记录 ④ 的接口对齐教训）。

> 2026-10-05 **全量规模留出评测：ItemCF 优势成立**（第五轮 ItemCF 实验，产物在 `experiments/itemcf/out/holdout/`）：

> ① **问题与答案**：在全量训练数据下 ItemCF 是否仍领先 → **是**。训练段是**全体 200,948 用户**的 **25,677,530 行**（好评 pair 12,995,573），邻居图 **4,565,112 条边**；固定 2,000 人评测名单（有效 **1,952**）。主表：**ItemCF NDCG@20 = 0.132076 / Recall@20 = 0.145742**，MostPop 0.085477 / 0.093561，LikePop 0.086289 / 0.094421。对较强基线（LikePop）配对 bootstrap（seed=42、2000 次）四个区间**全部排除 0**（NDCG +53.06% [+0.039678, +0.052458]、Recall +54.35% [+0.043761, +0.059078]）。判定 `advantage_holds_at_full_scale`。VM 总耗时 **1,103 秒**，**未触发任何资源终止线**。
> ② **协议要点**（执行前冻结在 `docs/superpowers/specs/2026-10-05-fullscale-holdout-protocol.md`）：全体用户按个人时间 80/20 切分；**训练段取全体用户**（这才是"全量规模"），**留出段只保留评测用户**；名单 salt 换新为 `itemcf-fullscale-eval-v1:`，排除已识别的 40,000 历史实验用户（上一轮 cohort∪eval ∪ 旧 2k/20k 切片 ∪ fix14）。
> ③ **两处执行前的判断被实测证伪（如实记录）**：**(a)** 曾担心 `n_u < 5` 的用户训练段为空、会让全量训练图丢共现，并据此设了"只保留 `n_u >= 5`"的敏感性口径——**该担心不成立**：`data/raw/README.txt` 第 6 行写明 ML-32M 用户"All selected users had rated at least 20 movies"，实测 `min(n_u) = 20`、`n_u < 5` 的用户为 **0**、`train_rows == 0` 的用户为 **0**，那套口径是**恒等变换**，已删除。**(b)** `temporal_split` 在 `n < 2` 时**短路**（`data.py` L18-20），故 `n == 1 → train 1 / test 0`（单评分用户整体进训练段、且永远不可评测），不是"train 0 / test 1"。
> ④ **实现过程中实测证伪 7 处写法**，全部写成回归用例钉死（详见 `.vmwork/holdout_eval_verified_contract.md`）：(1) `searchsorted` 对整条 CSR `indices`（跨行回绕，行 0 命中全丢）；(2) 行偏移法做全局单调（空行使相邻行键并列，误判命中）；(3) 命中 = `A` 的非零项（**没做乘加**，每人 15 条而非 366）；(4) **Gram 恒等式 `(A@A.T−diag)@A`**（清的是 `G[v,v]` 而不是 `c_ii`，把已评电影算回分数：正确 0 → Gram 给 27）；(5) 三个方法共用同一候选池（ItemCF 命中 66,000 → **0**）；(6) **分子用评测用户、分母用全库 `n_like`**（ItemCF 的 NDCG/Recall **双双 0.0**、覆盖率只剩 202 部）；(7) **分子用训练段、分母用全量 32M 行**（相似度差 1.1~1.5 倍，倍率不固定）。
> ⑤ **方法论教训（最重要的一条）**：第 6、7 两处**都不是单元测试能发现的**——第 6 条靠"全量跑完看指标是否合理"，第 7 条靠**独立核验器的抽样对拍**。第 7 条尤其危险：**Top-20 排序对全体用户的共同缩放因子不敏感**，指标看起来完全正常（0.1418 vs 正确 0.1383），只有从原始评分重算相似度才能发现。**"有单元测试 + 指标合理"不足以支撑论文数字。**
> ⑥ **两条容易被误读的产物细节**：**(a)** `verification.json` 是**自查**（`status=self_check_only`，由算指标的同一模块产出），**不是**独立核验；独立核验在 `verification_independent.json`（另一 CLI `verify_holdout_eval.py`，从原始评分重算，**不 import** 评测侧——由 AST 断言钉死）。这一区分源自上一轮的缺陷 H2，有专门用例。**(b)** `scipy.csr_matrix` 由 COO 构造时会**按列号升序重排每行**，所以磁盘上的共现矩阵行内顺序**不是**相似度降序；**对正确性无影响**（top-k 在构造 CSR 之前做完、`A @ C` 与顺序无关），但核验器必须按**边集合**而非行内顺序比对（第一版逐位比对报了 100% 假阳性）。
> ⑦ **资源记录**：VM 上跑**核验器会被 OOM 杀掉**（`exit=137`）——它要把全体用户训练段驻留内存（约 2~3 GB Python 对象），而 VM 只有 3.9 GB。核验器应在**内存宽裕的机器**上跑（本轮在开发机跑，同一份输入 + 同一份代码，496 秒，10 项检查全通过、指标差 `0.000e+00`）。另外第一次启动时**漏了排除名单**（日志 `prior exclusions: 0 user(s)`），已停掉换新 run_id 重跑——启动检查必须盯住这一行。
> ⑧ **代码位置**：`experiments/itemcf/heldout_eval.py`（评测入口：名单冻结、全量切分、共现、精确打分、指标、产物）、`holdout_common.py`（**只读复用** `full_train` 的数据面函数，不另写一套构图）、`rate_limited_guard.py`（独立监督 + **可刷新 deadline**，去掉单阶段墙钟误杀）、`verify_holdout_eval.py`（独立核验器）。**既有文件一律未改**（`full_train.py`/`model_io.py`/`resource_guard.py`/`confirm.py`/`core.py` 原样），第四轮模型与 `graph_blocks.json` 指纹不受影响（有用例断言 `source_hashes_of_modules()` 仍是那 6 个模块）。新增用例：`test_itemcf_holdout_eval.py`（36）、`test_itemcf_verify_holdout_eval.py`（16）、`test_itemcf_rate_limited_guard.py`（59），ItemCF 全套 **644 个用例 OK**。
> ⑨ **本轮未做**：调参、ALS 训练/融合、接线上、改数据库、genres 冷启动（本轮只回答"优势是否仍成立"这一个问题）。**未** commit / push。

> 2026-09-21 训练切分方式核实（**结论修正版：线上一直是哈希切分，论文数字正确且已完整复现**）：
> ① **时间线（我最初判断错过一次，记录教训）**——本地 git 里 `train_als.py` 直到 2026-09-16（`7eecf45`）才提交哈希切分，据此我曾判定"库里的模型都是 `randomSplit` 训的"。**这是错的**：VM 备份显示 `train_als.py` 修改于 **09-15 13:04**、`training_contract.py` 于 **09-15 11:41**，而旧 active 模型生成于 **09-15 13:25**——**旧模型就是用哈希切分训的**。VM 上的代码比本地提交早，只查 git 会得出错误结论。
> ② **关键实现细节：哈希的输入是"取负之后"的 userId。** `train_als.py` 先把 MovieLens userId 取负再调用 `split_ratings_for_eval`，而 `md5("-8:1") != md5("8:1")`（实测 (8,1) 正数桶 8282、取负桶 4080）。用原始正数计算会得到**另一套**切分：正数口径 train=25,600,540 / test=6,399,664；**取负口径（线上真实）train=25,599,242 / test=6,400,962**。复现线上结果必须同样取负——我第一次写的评测脚本正是漏了取负，才算出对不上的 0.8077。
> ③ **生产完整重训已执行并复现论文数字**（2026-09-21，VM，Spark 3.5.1 / Java 8 / Python 3.6.8 / 2 核 / 3.9GB RAM + 4GB swap / `--master local[*]`，用项目自带 `scripts/run_training.sh`）：新模型 **`als-20260921T135323Z-dd29dce5bed4` 发布为 active**；**RMSE=0.8074**（< 0.85）；MovieLens 训练集 **25,599,242**、测试集 **6,400,962**；训练集合计 25,599,264（含 22 条系统用户评分）；隐因子 **80,194**；**recommendations 从 0 → 40 条**（2 个有评分的系统用户各 Top-20）——**离线批量推荐链路首次真正落库**。旧模型全部 retired，旧因子由 `cleanup_stale_model_data` 清理（**不可回滚**，需重训）。
> ④ **论文数字因此是对的**：25,599,242 / 6,400,962 / RMSE 0.8074 都是线上口径的真实值，且**确定性、可复现**。但论文文字若写「随机划分（seed=42）」**是过时描述**——实际代码是确定性哈希切分（数字对、描述错）。另一处 25,600,163 / 6,400,041 则来自更早的 `randomSplit` 时期。
> ⑤ **仍然成立的发现**：哈希切分**不改善每用户覆盖**——它是逐条评分独立分桶，用户测试集条数服从 `B(n_u, 0.2)`，与 `randomSplit` 的逐行独立机制同构，因此"保证每个用户在两侧都有评分"的说法不成立（实测约 0.1% 用户无测试评分，贡献不足 0.05% 的评分）。`training_contract.py` docstring 已改为如实描述。
> ⑥ **VM 与本地代码曾分叉**：VM 停在 09-15/09-16 状态（多一个 `utils/user_id_namespace.py`、缺 preprocess 的 2 个 staging 函数、`preprocess.py` 是旧版），本地已到 09-18。重训前已把本地 `spark/` 同步到 VM（备份 `spark_backup_20260921_213450.tar.gz`），4 个文件 md5 现已一致，且哈希逻辑原本就相同、故不影响模型。
> ⑦ 附带修正：`backend/app/models.py` 的 writers/countries/languages 仍是 009 迁移前的旧宽度（`Text`/`String(200)`），已对齐为 `String(500)`；新增 `tests/test_sql_schema_contract.py::test_orm_column_widths_match_schema` 锁死 ORM 与 schema 列宽一致。
> 实验脚本与日志留档在 `.vmwork/`（`exp_train_eval.py` 已参数化，但**注意它当前对 userId 未取负**，与线上口径不同，复用时须先修）。

> 2026-09-18 搜索规则（含 `ID318` 精准查 ID）：`q` 支持四种输入——**`ID318` / `id318` 只按 `movie_id` 精确命中**（主键查询，约 1ms，结果是唯一那一条）、纯数字 `318` 宽匹配「片名 / 简介」并额外命中 `movie_id`（该条排最前）、文字命中片名、文字命中简介。显式 ID **只有这两种写法**：前缀大小写随意，但数字必须紧挨着（`id 318` / `ID=318` / `id:318` 都不算，会退回普通关键词匹配）；写出 `id` 但后面没有数字同样退回普通关键词。规则仍是 `backend/app/movie_search.py` 里的纯函数，前台 `GET /api/movies/` 与后台 `GET /api/admin/movies` 共用（用例见 `tests/test_movie_search.py`）。另有两条附带口径：① `sort=popular` 的「评分人数 ≥ 100」门槛只在**无关键词**时生效，否则冷门片按 ID / 简介搜不到；② 模糊匹配用 `LIKE` 而非 `ilike()`——`title` / `overview` 的排序规则是 `utf8mb4_0900_ai_ci`，`LIKE` 本就大小写不敏感，`ilike()` 的 `LOWER()` 在 8.7 万行 TEXT 上白烧 CPU（实测单次扫描 583ms → 383ms）。同日详情页：标题下方加浅灰小字「电影 ID：xxx」（便于照抄成 `ID318` 去搜），「剧情简介」标题字号 14 → 20px（简介正文 18px 不变）。

---

## 关键配置

所有连接参数统一在 `config.yaml` 中管理，代码与脚本通过根目录 `config_loader.py` 读取，**不硬编码**。
模板见 `config.example.yaml`；`config.yaml` 含真实凭据、不入 git，需要时按需读取，但不要把其中的值抄进源码、日志或提交内容。

| 参数 | 默认值 |
|------|--------|
| HDFS NameNode | `hdfs://localhost:9000` |
| HDFS 数据目录 | `/movielens/` |
| MySQL Host | `localhost:3306` |
| MySQL DB | `movie_recommend` |
| Flask Port | `8000`（本机 5000/5001 被系统策略禁止绑定）|
| Vue Dev Port | `5173`（Vite 默认）|

---

## 两端拓扑与联通方式（接手必读）

> 2026-10-01 实地核查记录（只读排查，未改动任何代码与数据）。**这一节是给下一个 Agent 的地图**：
> 本项目跨两台机器，两者职责、凭据、可达性都不同，搞混会得出完全错误的结论。
>
> **先看这三条，能省掉最多弯路**：① 计算在 VM、**数据（MySQL）在开发机**，Spark 通过 JDBC 连回来；
> ② **VM 上从来没有 MySQL 服务端**（与 `implementation-guide.md` 2.1.2/2.5 节的描述不符，别去找）；
> ③ **VM 上的代码比本地 git 旧**，排查历史口径必须看 VM 文件时间戳，只看 git 会得出错误结论。

### 两台机器各是什么

| | 开发机（Windows，本工作区） | 虚拟机 hadoop102 |
|---|---|---|
| 地址 | 本机 | `192.168.10.102` |
| 跑什么 | Flask API + Vue 前端 + **MySQL 8.0（数据都在这里）** | Hadoop HDFS + Spark + JDK 8 |
| 项目路径 | 本工作区 `D:\Sundries\Paper\Project` | `/home/h/project` |
| 登录方式 | — | `ssh h@192.168.10.102`，**已配密钥免密**（`BatchMode=yes` 直接可用） |
| MySQL | ✅ 服务端在跑，`movie_recommend` 库在这里 | ❌ **没有 MySQL 服务端**（详见下） |

**一句话：计算在 VM，数据在开发机。Spark 跑在 VM 上，通过 JDBC 连回开发机的 MySQL。**

### 四条链路各自的走法

| 链路 | 方向 | 机制 / 落点 |
|---|---|---|
| **① 代码同步** | 开发机 → VM | `python scripts/sync_code_to_vm.py`（**手动执行，无任何自动调用方**）。用 paramiko + SFTP 打包 git 跟踪文件，解压覆盖 `/home/h/project`，覆盖前自动备份为 `<项目>_backup_<时间戳>.tar.gz`。**注意它用密码登录**（读 `config.yaml` 的 `vm.ssh_password`），依赖 `paramiko`——本机当前**没装**，要跑得先 `pip install paramiko` |
| **② 数据同步** | 开发机 → HDFS | `bash scripts/init_hdfs.sh`：把 `data/raw/ratings.csv`（836 MB）与 `movies.csv` 传到 HDFS `/movielens/`。**必须在 VM 上执行**，且要求本地 `data/raw/` 有文件 |
| **③ 训练回写** | VM Spark → 开发机 MySQL | `bash scripts/run_training.sh`（VM 上跑）→ `spark-submit --master local[*]` → JDBC 写 `model_item_factors` / `recommendations` / `model_versions`。**JDBC 驱动 `mysql-connector-j-8.0.33.jar` 已装在 VM 的 `$SPARK_HOME/jars/`** |
| **④ 应用读库** | 开发机 Flask → 本机 MySQL | `config.yaml` 的 `mysql.host` = `192.168.10.1`，直连本机 |

### 关键事实：VM 上没有 MySQL（已核实，别再去找）

`implementation-guide.md` 的 2.1.2 节把 hadoop102 写成「主节点 | NameNode、ResourceManager、**MySQL**、Spark」，2.5 节还有完整的 MySQL 8 安装步骤。**但实际从未装成**，本次全盘核查结论：

- 全盘 `find` 找不到 `mysqld` / `mariadbd` / `mysql.server`；
- `rpm -qa` 里只有 `mariadb-libs-5.5.60`（被别的包带进来的**客户端库**，不是服务端）；
- `my.cnf` 指定的 `datadir=/var/lib/mysql` **不存在**，`/etc/mysql` 不存在，无任何 systemd 单元；
- **3306 无进程监听**；
- `/etc/my.cnf` 的内容是 **MariaDB 的默认配置**（引用 `/var/log/mariadb/`），不是 MySQL 8 安装会产生的；
- 但 **Spark 的 JDBC 驱动装了**——说明部署走到了「给 Spark 配驱动」就停了，服务端那步没落地。

推论：`config.yaml` 里的 `vm.mysql_password` / `vm.mysql_database` 两个字段是**未落地的残留配置**，没有任何代码在 VM 上连本机 MySQL。**不要照 `implementation-guide.md` 2.5 节去 VM 上找库。**

### 已验证的可达性（2026-10-01）

| 检查 | 结果 |
|---|---|
| `ssh h@192.168.10.102` | ✅ 通（密钥免密） |
| VM → `192.168.10.1:3306` | ✅ **REACHABLE**（训练回写这条路是通的） |
| VM 上的 HDFS `hadoop102:9000` | ❌ **拒绝连接——HDFS 当时没启动**（`jps` 只有 `Jps`，无 NameNode/DataNode/ResourceManager） |
| VM 上 `hadoop version` / `spark-submit --version` | ✅ Hadoop 3.3.6 / Spark 3.5.1（Scala 2.12.18, Java 1.8.0_291）——**已安装，只是进程没起** |
| HDFS 数据文件 | ✅ 本地 `/home/h/movielens/` 有 `ratings.csv`(877 MB) + `movies.csv`(4.2 MB)；HDFS 内的 `/movielens/` **因 NameNode 没起而无法确认**（`dfs -ls` 也报连接拒绝） |
| VM 的 `~/config.yaml` 与 `/home/h/project/config.yaml` | ⚠️ **版本旧**：`flask.port: 5000`（现为 8000）、`hdfs.namenode` 写的是 `hdfs://192.168.10.102:9000`（VM 的 `core-site.xml` 里是 `hdfs://hadoop102:9000`）、多一个已废弃的 `als.ml_user_offset: 10000000`（现行方案是 userId **取负**，不是偏移）。**别拿 VM 上的 config.yaml 当准** |

### 另一个必须记住的坑：VM 代码比本地旧

VM 停在 **2026-09-15/16** 的状态，本地已到 09-18+。已知差异：

- VM 多一个 `utils/user_id_namespace.py`；缺 `preprocess.py` 的 2 个 staging 函数，`preprocess.py` 是旧版；
- VM 上残留 `fetch_missing_by_search.py`（本地已合并进 `fetch_posters.py`）、`sync_movie_stats.py`、根目录 `check_db.py`（本地已移入 `scripts/`）、`start.bat.broken` 等历史文件。

> **教训（曾被这个坑误导过）**：VM 上的文件修改时间可能**早于**本地 git 提交时间。2026-09-21 有人只查本地 git，看到哈希切分到 09-16 才提交，就判定「库里的模型是 `randomSplit` 训的」——**结论是错的**，VM 备份显示 `train_als.py` 修改于 09-15 13:04，旧 active 模型生成于 09-15 13:25，**旧模型本来就用的哈希切分**。**排查历史口径时要看 VM 上的文件时间戳，不能只看 git。**

### 技能与工具的可用性（实测，省得白试）

- `ssh` 免密可用，但**写 `C:\Users\admin\.ssh\known_hosts` 会被沙箱拒绝**（工作区外）——加 `-o UserKnownHostsFile=NUL` 绕过，或忽略那条 `exit code: 1`（它不代表远程命令失败）；
- 本机**没有** `sshpass` / `plink` / `paramiko`，所以 `sync_code_to_vm.py` 直接跑会因缺依赖失败；
- 复杂远程脚本别走 `ssh "..."` 内联（PowerShell 会吞引号），**写成 `.sh` 用 `Get-Content -Raw | ssh ... "bash -s"` 喂进去**，并注意把 CRLF 换成 LF（否则报 `$'\r': 未找到命令`）。

---

## 数据库表结构

共 8 张表（完整 DDL 见 `sql/schema.sql`，增量迁移见 `sql/migrations/`）：

```
users             — 系统用户（id, username, password_hash, avatar_url, bio, nickname, created_at, role）
                    · username = 账号（登录凭据，3-20 位字母/数字/下划线/连字符，注册后不可改；登录也可用 id）
                    · nickname = 昵称（展示名，任意字符、可空；为空时显示回落 username）
movies            — 电影元数据（movie_id, title, genres, year + 评分统计与 TMDB 富化列）
user_ratings      — 系统用户评分（user_id, movie_id, rating, rated_at）← 评分的唯一事实来源
movie_rating_stats— MovieLens 评分五档分布（movie_id, rating_count, rating_sum, cnt_1..cnt_5）
movie_comments    — 电影评论（user_id, movie_id, content, created_at, updated_at, edited_at）
model_versions    — ALS 模型版本（version, rank, reg_param, status, generated_at, activated_at）
model_item_factors— 电影隐因子（model_version, movie_id, factors, generated_at）
recommendations   — 离线批量推荐（model_version, user_id, movie_id, predicted_rating, rank）
```

> `movie_rating_stats` 是**离线聚合的派生表**，由 `scripts/build_ml_rating_stats.py`
> 聚合 `data/raw/ratings.csv` 写入，不随 ALS 训练变化；详情页「评分统计」把它与
> `user_ratings` 合并后展示，页面上不区分来源。重载 `movies` 后需重跑该脚本。

> 2026-09-14 起评分表统一到 `user_ratings`：应用写入、Spark 训练、在线推荐都只读写这一张表。
> legacy `ratings` 的数据由 `migrations/005` 合并进 `user_ratings`，表本身已由
> `migrations/008_drop_legacy_ratings.sql` 删除（2026-09-16）；由 `tests/test_rating_table_contract.py`
> 与 `tests/test_training_contract.py` 两端契约锁定，不要再把应用评分写进 `ratings`。
>
> MovieLens 原始评分不存在 MySQL 里，它在 HDFS 的 `ratings.csv`，训练时读取并将 `userId` 取负。

---

## ALS 模型参数

| 参数 | 值 | 说明 |
|------|-----|------|
| rank | 50 | 隐因子维度 |
| maxIter | 10 | 迭代次数 |
| regParam | 0.1 | 正则化系数 |
| nonnegative | true | 非负约束（在线求解也沿用） |
| coldStartStrategy | drop | 冷启动处理 |
| 训练/测试分割 | 8:2 | 只对 MovieLens 评分划分；系统用户评分全部进训练集 |
| 评估指标 | RMSE | 只用 MovieLens 测试集计算，目标 < 0.85 |

模型版本状态流转 `building → active → retired`：因子与批量推荐写完后才发布为 `active`，训练失败只把 `building` 版本标为 `retired`，不会动旧的 active 模型。

---

## 开发顺序

1. `sql/schema.sql` — 建表
2. `spark/` — 数据预处理 + ALS 训练，向 MySQL 写入推荐结果
3. `backend/` — Flask API，读 MySQL 提供数据接口
4. `frontend/` — Vue3 调用 API 展示结果

---

## 常用命令

```bash
# 格式化 HDFS（首次）
hdfs namenode -format

# 启动 Hadoop
start-dfs.sh && start-yarn.sh

# 上传数据到 HDFS
bash scripts/init_hdfs.sh

# 运行 Spark 训练（内部调用 spark-submit spark/train_als.py）
bash scripts/run_training.sh

# 新库建表 / 已有库增量迁移
mysql -u root -p < sql/schema.sql
mysql -u root -p movie_recommend < sql/migrations/005_merge_ratings_into_user_ratings.sql

# 查看各表行数与当前 active 模型
python scripts/check_db.py

# 启动 Web 应用（Windows，推荐；隐藏窗口后台运行，日志/PID 在 logs\，优先使用 backend/venv）
# 启动后自动打开浏览器；到点自动停止服务（没有停止脚本）
# 项目正在运行时再执行一次 = 重启：先停旧进程（按 logs\*.pid，再按端口 8000/5173 兜底）再起新的
# start.bat = 4 小时后自停；start.bat 8 = 8 小时；start.bat 0 = 不自动停
start.bat

# 或分别启动（前台）
python scripts/start_backend.py
npm run dev --prefix frontend

# 测试与静态检查（需使用已装 numpy 的解释器，例如 backend/venv）
backend/venv/Scripts/python.exe -m unittest discover -s tests -v   # 后端：902 个用例
npm run test --prefix frontend                                     # 前端组件测试（vitest + jsdom）：28 个用例
npm run lint --prefix frontend                                     # ESLint（Vue 3 SFC + 浏览器/Node 全局）
python -m compileall -q backend spark scripts
npm run build --prefix frontend
```

---

## 注意事项

- `data/raw/` 和 `data/processed/` 已加入 `.gitignore`，MovieLens-32M 原始文件不提交到 git。
- Spark 作业通过 JDBC 把电影因子与批量推荐写入 MySQL；Flask 主路径是在线实时求解，因子缺失或用户无评分时才降级读 `recommendations`。
- 前端评分写入 `user_ratings`：下一个推荐请求即触发该用户的在线计算；只有重新训练后，新评分才会进入全局模型并影响其他用户。
- 集群需要本机配置 SSH 免密登录（`ssh-keygen` + `authorized_keys`）。
- `spark/preprocess.py` 自 2026-09-16 起走 staging/upsert（`movies_staging` 暂存 + `ON DUPLICATE KEY UPDATE`），可重复执行，不会清空评分统计与 TMDB 富化列（详见 README 第 7.3 节）。
- `config.yaml` 只受 `.gitignore` 约束（本机留存、不入 git），不在 `.aiignore` 里：要看当前实际值就直接读；只关心有哪些配置项时看 `config.example.yaml`。
- `archive/` 只放已废弃的历史件（两个失效的迁移脚本、两份后台系统的旧计划稿），**不要**执行其中 SQL，也不要把它们当实现参考。
- 用户头像现在走「点自己的头像 → 从内置 SVG 里挑一个」：清单是 `backend/app/routes/profile.py` 里的 `PRESET_AVATARS` 常量（10 个，顺序即面板顺序），文件在 `backend/assets/avatars/<名字>.svg`（目录可用 `config.yaml` 的 `flask.avatar_preset_dir` 覆盖），`users.avatar_url` 里存 `/api/profile/avatars/<名字>.svg`；清空即「恢复默认」（前端回落成显示名首字符）。**历史上传那套仍在**：文件落在 `backend/uploads/avatars/`（已进 `.gitignore`），`users.avatar_url` 存 `/api/profile/avatar/<文件名>`，上传接口与读取路由都保留（界面不再提供上传入口），换成预设或默认时会把这名用户的那张旧图删掉。

---

## 上下文忽略清单（所有 Agent 必读）

无论使用哪个 Agent / CLI（DSH / Codex / Claude Code / Cursor / Aider…）接手本项目，下列路径都不读取、不注入上下文。

完整清单的唯一事实来源是 **`.aiignore`**（跨工具通用）。收录标准只有一条：**读了会大幅撑爆上下文**。
以下按规模排列：

| 路径 / 模式 | 规模（实测） |
|------|------|
| `data/raw/`、`data/processed/`、`*.csv` | 数据集：`ratings.csv` 单个 836 MB，派生 CSV 同为百万行级 |
| `venv/`、`.venv/`、`node_modules/` | 依赖：352 MB + 149 MB，合计 2.6 万个文件 |
| `__pycache__/`、`*.py[cod]` | 缓存：项目代码里 8 个目录 / 97 个 `.pyc`；另 416 个目录 / 91 MB 在 `venv/` 内（已被上一行覆盖） |
| `frontend/dist/` | 构建产物：压缩后的单个 JS 就能塞满上下文 |
| `logs/`、`*.log` | 现在只有 9 KB，但随运行无限增长 |
| `.agents/`、`.claude/skills/` | 技能框架 374 KB 纯文本；技能经 Skill 工具按需加载，整棵树读进来没有意义 |
| `thesis_draft.txt` | 论文草稿 80 KB；任务明确是改写论文时，由用户单独点名 |

凭据与运行时目录不在这份清单里：能由 `.gitignore` 兜住的就交给它（`config.yaml`、`.env`、`backend/uploads/`）。

工具配置指向同一份清单，不重复维护：

- `AGENTS.md`（本节）— 通用 Agent 入口
- `.aiignore` — 完整清单，唯一事实来源
- `.claudeignore.md` — 指向 `.aiignore` 的指针
- `.gitignore` — git 层兜底

<!-- superpowers-zh:begin (do not edit between these markers) -->
# Superpowers-ZH 中文增强版

本项目已安装 superpowers-zh 技能框架（20 个 skills）。

## 核心规则

1. **收到任务时，先检查是否有匹配的 skill** — 哪怕只有 1% 的可能性也要检查
2. **设计先于编码** — 收到功能需求时，先用 brainstorming skill 做需求分析
3. **测试先于实现** — 写代码前先写测试（TDD）
4. **验证先于完成** — 声称完成前必须运行验证命令

## 可用 Skills

Skills 位于 `.agents/skills/` 目录，每个 skill 有独立的 `SKILL.md` 文件。

- **brainstorming**: 在任何创造性工作之前必须使用此技能——创建功能、构建组件、添加功能或修改行为。在实现之前先探索用户意图、需求和设计。
- **chinese-code-review**: 中文 review 沟通参考——话术模板、分级标注（必须修复/建议修改/仅供参考）、国内团队常见反模式应对。仅在用户显式 /chinese-code-review 时调用，不要根据上下文自动触发。
- **chinese-commit-conventions**: 中文 commit 与 changelog 配置参考——Conventional Commits 中文适配、commitlint/husky/commitizen 中文模板、conventional-changelog 中文配置。仅在用户显式 /chinese-commit-conventions 时调用，不要根据上下文自动触发。
- **chinese-documentation**: 中文文档排版参考——中英文空格、全半角标点、术语保留、链接格式、中文文案排版指北约定。仅在用户显式 /chinese-documentation 时调用，不要根据上下文自动触发。
- **chinese-git-workflow**: 国内 Git 平台配置参考——Gitee、Coding.net、极狐 GitLab、CNB 的 SSH/HTTPS/凭据/CI 接入差异与镜像同步配置。仅在用户显式 /chinese-git-workflow 时调用，不要根据上下文自动触发。
- **dispatching-parallel-agents**: 当面对 2 个以上可以独立进行、无共享状态或顺序依赖的任务时使用
- **executing-plans**: 当你有一份书面实现计划需要在单独的会话中执行，并设有审查检查点时使用
- **finishing-a-development-branch**: 当实现完成、所有测试通过、需要决定如何集成工作时使用——通过提供合并、PR 或清理等结构化选项来引导开发工作的收尾
- **mcp-builder**: MCP 服务器构建方法论 — 系统化构建生产级 MCP 工具，让 AI 助手连接外部能力
- **receiving-code-review**: 收到代码审查反馈后、实施建议之前使用，尤其当反馈不明确或技术上有疑问时——需要技术严谨性和验证，而非敷衍附和或盲目执行
- **requesting-code-review**: 完成任务、实现重要功能或合并前使用，用于验证工作成果是否符合要求
- **subagent-driven-development**: 当在当前会话中执行包含独立任务的实现计划时使用
- **systematic-debugging**: 遇到任何 bug、测试失败或异常行为时使用，在提出修复方案之前执行
- **test-driven-development**: 在实现任何功能或修复 bug 时使用，在编写实现代码之前
- **using-git-worktrees**: 当需要开始与当前工作区隔离的功能开发，或在执行实现计划之前使用——通过原生工具或 git worktree 回退机制确保隔离工作区存在
- **using-superpowers**: 在开始任何对话时使用——确立如何查找和使用技能，要求在任何响应（包括澄清性问题）之前调用 Skill 工具
- **verification-before-completion**: 在宣称工作完成、已修复或测试通过之前使用，在提交或创建 PR 之前——必须运行验证命令并确认输出后才能声称成功；始终用证据支撑断言
- **workflow-runner**: 在 Codex / OpenClaw / Cursor 中直接运行 agency-orchestrator YAML 工作流——无需 API key，使用当前会话的 LLM 作为执行引擎。当用户提供 .yaml 工作流文件或要求多角色协作完成任务时触发。
- **writing-plans**: 当你有规格说明或需求用于多步骤任务时使用，在动手写代码之前
- **writing-skills**: 当创建新技能、编辑现有技能或在部署前验证技能是否有效时使用

## 如何使用

当任务匹配某个 skill 时，使用 `Skill` 工具加载对应 skill 并严格遵循其流程。绝不要用 Read 工具读取 SKILL.md 文件。

如果你认为哪怕只有 1% 的可能性某个 skill 适用于你正在做的事情，你必须调用该 skill 检查。
<!-- superpowers-zh:end -->
