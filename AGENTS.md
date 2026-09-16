# 基于 Hadoop 生态的个性化电影推荐系统

## 项目概述

本项目以 MovieLens-32M 为数据源，构建一套完整的电影推荐系统，涵盖从数据存储、离线训练到前端展示的全链路。

推荐分两条链路：

- **离线**：HDFS 上的 MovieLens 评分 + MySQL `user_ratings` 合并后训练 ALS，产出电影隐因子与系统用户批量推荐；
- **在线**：用户提交评分后固定已训练的电影因子，只用带非负约束的最小二乘求解当前用户向量，立即返回 Top-N（不启动 Spark）。

**核心技术栈：**
- 存储层：Hadoop HDFS（本机多虚拟机模拟的小集群，hadoop102/103/104）
- 计算层：PySpark + MLlib ALS 协同过滤
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
│   └── implementation-guide.md   # 按源码核对的实现指南
├── config.example.yaml    # 配置模板（不含真实凭据）
├── config.yaml            # 本地真实配置，不入 git，禁止读取
├── config_loader.py       # 统一读取 config.yaml 的工具
├── start.bat              # Windows 开发启动入口
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
│       ├── training_contract.py   # 训练输入取数契约（只读 user_ratings）
│       └── user_id_namespace.py   # 用户 ID 命名空间校验（取负方案）
│
├── sql/
│   ├── schema.sql         # 全新数据库完整建表 DDL（8 张表）
│   └── migrations/        # 已有数据库的增量迁移
│
├── backend/               # Flask API
│   ├── app.py             # 入口
│   ├── config.py          # 从根目录 config.yaml 加载配置
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py    # create_app：注册蓝图与推荐服务单例
│       ├── models.py      # SQLAlchemy ORM
│       ├── db.py
│       ├── rating_stats.py                # 评分档位规则（纯函数，离线脚本复用）
│       ├── recommendation_core.py         # 在线求解与排序（纯函数）
│       ├── recommendation_service.py      # 在线推荐服务（含因子缓存与降级）
│       ├── recommendation_repository.py   # 仓储层（SQLAlchemy 查询）
│       └── routes/
│           ├── auth.py            # 登录/注册
│           ├── movies.py          # 电影浏览、筛选、类型、详情
│           ├── ratings.py         # 评分提交与历史（写 user_ratings）
│           ├── recommendations.py # 推荐列表
│           ├── comments.py        # 电影评论
│           ├── profile.py         # 个人主页（资料/统计/评论 + 头像上传与读取）
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
│       ├── api/           # 按域拆分的 Axios 封装（client.js 未使用）
│       ├── views/         # HomeView / MovieDetailView / ProfileView / LoginView / RegisterView / RecommendationsView
│       │   └── admin/     # AdminDashboard / AdminUsers / AdminMovies / AdminRatings / AdminModels
│       └── components/    # MovieCard / NavBar / CommentSection / CommentItem
│
├── tests/                 # Python 单元与契约测试（140 个用例）
│
├── scripts/
│   ├── init_hdfs.sh       # 创建 HDFS 目录并上传数据
│   ├── run_training.sh    # 提交 Spark 训练作业
│   ├── start_backend.py   # 可复用的 Flask 启动入口
│   ├── start_dev.sh       # 联合启动（自动探测 backend/venv，兼容 bin/ 与 Scripts/）
│   ├── build_ratings_clean.py # 从 ratings.csv 聚合出 ratings_clean.csv（--check 可比对）
│   ├── build_ml_rating_stats.py # 聚合五档分布写入 movie_rating_stats
│   └── import_movie_stats.py # 把 ratings_clean.csv 的均值/人数回填 movies 表
│
├── check_db.py            # 各表行数与活跃模型检查
├── sync_movie_stats.py    # 经 SSH 从虚拟机同步评分统计
├── fetch_posters.py       # 按 TMDB API 回填海报与简介等富化列
└── arch_diagram.html      # 系统架构图（生成物，不在上下文里注入）
```

> 2026-09-16 结构清理：`api/client.js` 已并入 `api/index.js`（全站单一 axios 实例，错误 toast 统一在拦截器弹）；`stores/user.js`、`views/MoviesView.vue`、`GET /api/profile/ratings` 死接口已删除。

---

## 关键配置

所有连接参数统一在 `config.yaml` 中管理，代码与脚本通过根目录 `config_loader.py` 读取，**不硬编码**。
模板见 `config.example.yaml`；`config.yaml` 含真实凭据，不入 git，也不注入 AI 上下文。

| 参数 | 默认值 |
|------|--------|
| HDFS NameNode | `hdfs://localhost:9000` |
| HDFS 数据目录 | `/movielens/` |
| MySQL Host | `localhost:3306` |
| MySQL DB | `movie_recommend` |
| Flask Port | `8000`（本机 5000/5001 被系统策略禁止绑定）|
| Vue Dev Port | `5173`（Vite 默认）|

---

## 数据库表结构

共 8 张表（完整 DDL 见 `sql/schema.sql`，增量迁移见 `sql/migrations/`）：

```
users             — 系统用户（id, username, password_hash, avatar_url, bio, created_at, role）
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
python check_db.py

# 启动 Web 应用（Windows，推荐；会优先使用 backend/venv）
start.bat

# 或分别启动
python scripts/start_backend.py
npm run dev --prefix frontend

# 测试与静态检查（需使用已装 numpy 的解释器，例如 backend/venv）
backend/venv/Scripts/python.exe -m unittest discover -s tests -v
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
- `config.yaml` 按忽略清单不读取；需要了解配置项时看 `config.example.yaml`。
- 用户头像走「点头像上传本地图片」：文件落在 `backend/uploads/avatars/`（已进 `.gitignore` 与 `.aiignore`），`users.avatar_url` 里存的是 `/api/profile/avatar/<文件名>` 相对地址；目录可用 `config.yaml` 的 `flask.avatar_upload_dir` 覆盖。

---

## 上下文忽略清单（所有 Agent 必读）

无论使用哪个 Agent / CLI（DSH / Codex / Claude Code / Cursor / Aider…）接手本项目，都必须跳过下列路径：**不读取、不注入上下文、不作为实现参考**。

完整清单的唯一事实来源是 **`.aiignore`**（跨工具通用）。以下为高频雷区摘要：

| 路径 / 模式 | 原因 |
|------|------|
| `*.log`、`logs/` | 运行日志，可能很大；抓取日志已改写到系统临时目录 |
| `arch_diagram.html` / `thesis_draft.txt` | 生成物与论文草稿，与业务代码无关 |
| `.claude/settings.local.json`、`.claude/plans/` | 本机权限配置与会话草稿 |
| `.agents/`、`.claude/skills/` | 技能框架文件，非业务代码 |
| `.cursor/`、`.aider*`、`.continue/`、`.cody/` | 其他工具的本地配置 |
| `data/raw/`、`data/processed/`、`*.csv`、`*.dat` | 数据集过大 |
| `backend/uploads/`、`hadoop/tmp/`、`hadoop/dfs/` | 运行时目录（头像图片、HDFS/YARN 本地数据），不是源码 |
| `config.yaml`、`*.pem`、`*.key`、`credentials.json`、`.npmrc`、`.aws/`、`.ssh/`、`.env` | 凭据与密钥，**禁止读取或输出** |

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
