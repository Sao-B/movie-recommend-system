# 基于 Hadoop 生态的个性化电影推荐系统

## 项目概述

本项目以 MovieLens-32M 为数据源，构建一套完整的电影推荐系统，涵盖从数据存储、离线训练到前端展示的全链路。

**核心技术栈：**
- 存储层：Hadoop HDFS（伪分布式单机）
- 计算层：PySpark + MLlib ALS 协同过滤
- 数据库：MySQL（存储电影元数据、用户评分、推荐结果）
- 后端：Flask RESTful API（Python）
- 前端：Vue 3 SPA + Element Plus

---

## 目录结构

```
Project/
├── AGENTS.md              # 本文件
├── README.md              # 项目说明与快速启动
├── .gitignore
├── config.yaml            # 全局配置（DB、HDFS、Spark 参数）
│
├── data/
│   ├── raw/               # MovieLens-32M 原始 CSV（不入 git）
│   └── processed/         # 预处理后的数据（不入 git）
│
├── hadoop/                # Hadoop 伪分布式配置文件
│   ├── core-site.xml
│   ├── hdfs-site.xml
│   ├── mapred-site.xml
│   └── yarn-site.xml
│
├── spark/                 # PySpark 离线训练作业
│   ├── preprocess.py      # 数据清洗，写回 HDFS
│   ├── train_als.py       # ALS 训练 + RMSE 评估 + 写入 MySQL
│   └── utils/
│       ├── spark_session.py
│       └── mysql_utils.py
│
├── sql/
│   └── schema.sql         # 完整建表 DDL
│
├── backend/               # Flask API
│   ├── app.py             # 入口
│   ├── config.py
│   ├── requirements.txt
│   └── app/
│       ├── __init__.py
│       ├── models.py      # SQLAlchemy ORM
│       ├── db.py
│       └── routes/
│           ├── auth.py           # 登录/注册
│           ├── movies.py         # 电影浏览
│           ├── recommendations.py # 推荐列表
│           └── ratings.py        # 评分提交
│
├── frontend/              # Vue 3 SPA
│   ├── package.json
│   ├── vite.config.js
│   ├── index.html
│   └── src/
│       ├── main.js
│       ├── App.vue
│       ├── router/index.js
│       ├── stores/        # Pinia stores
│       ├── api/           # Axios 封装
│       ├── views/         # 页面组件
│       └── components/    # 通用组件
│
└── scripts/               # 一键启动脚本
    ├── init_hdfs.sh       # 创建 HDFS 目录并上传数据
    ├── run_training.sh    # 提交 Spark 训练作业
    └── start_dev.sh       # 同时启动 Flask + Vue 开发服务器
```

---

## 关键配置

所有连接参数统一在 `config.yaml` 中管理，代码通过它读取，**不硬编码**。

| 参数 | 默认值 |
|------|--------|
| HDFS NameNode | `hdfs://localhost:9000` |
| HDFS 数据目录 | `/movielens/` |
| MySQL Host | `localhost:3306` |
| MySQL DB | `movie_recommend` |
| Flask Port | `5000` |
| Vue Dev Port | `5173`（Vite 默认）|

---

## 数据库表结构

```
users           — 系统用户（id, username, password_hash）
movies          — 电影元数据（movie_id, title, genres, year）
ratings         — 用户评分（user_id, movie_id, rating, timestamp）
recommendations — ALS 输出的推荐列表（user_id, movie_id, predicted_rating, rank）
```

> ratings 表既存 MovieLens 原始评分（离线训练用），也存用户在前端提交的实时评分。

---

## ALS 模型参数

| 参数 | 值 | 说明 |
|------|-----|------|
| rank | 50 | 隐因子维度 |
| maxIter | 10 | 迭代次数 |
| regParam | 0.1 | 正则化系数 |
| coldStartStrategy | drop | 冷启动处理 |
| 训练/测试分割 | 8:2 | |
| 评估指标 | RMSE | 目标 < 0.85 |

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

# 运行 Spark 训练
bash scripts/run_training.sh

# 启动 Flask（backend/）
python app.py

# 启动 Vue（frontend/）
npm run dev
```

---

## 注意事项

- `data/raw/` 和 `data/processed/` 已加入 `.gitignore`，MovieLens-32M 原始文件不提交到 git。
- Spark 作业通过 JDBC 将推荐结果批量写入 MySQL，Flask 只做读操作（推荐列表不实时计算）。
- 前端评分提交后写入 `ratings` 表，下次离线训练时纳入数据集。
- 伪分布式 Hadoop 需要本机配置 SSH 免密登录（`ssh-keygen` + `authorized_keys`）。

---

## 上下文忽略清单（所有 Agent 必读）

无论使用哪个 Agent / CLI（DSH / Codex / Claude Code / Cursor / Aider…）接手本项目，都必须跳过下列路径：**不读取、不注入上下文、不作为实现参考**。

完整清单的唯一事实来源是 **`.aiignore`**（跨工具通用）。以下为高频雷区摘要：

| 路径 / 模式 | 原因 |
|------|------|
| `poster_fetch_full.log` | 抓取日志，**5.8 MB**，最容易一次性吃满上下文 |
| `poster_fetch.log` | 同上，123 KB |
| `poster_fetch_progress.txt` | 进度快照，非源码 |
| `arch_diagram.html` / `thesis_draft.txt` | 生成物与论文草稿，与业务代码无关 |
| `.claude/settings.local.json`、`.claude/plans/` | 本机权限配置与会话草稿 |
| `.agents/`、`.claude/skills/` | 技能框架文件，非业务代码 |
| `.cursor/`、`.aider*`、`.continue/`、`.cody/` | 其他工具的本地配置 |
| `data/raw/`、`data/processed/`、`*.csv`、`*.dat` | 数据集过大 |
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
