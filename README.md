# 个性化电影推荐系统

基于 MovieLens-32M、Hadoop HDFS、PySpark ALS、MySQL、Flask 和 Vue 3 构建的电影推荐系统。

当前实现采用两阶段推荐：

```text
MovieLens 评分 + 系统用户评分
          ↓
     Spark ALS 定期训练
          ↓
电影隐因子 + 系统用户批量推荐

系统用户新增评分
          ↓
固定电影因子，在线求解当前用户向量
          ↓
当前用户立即得到新的推荐
```

> 当前代码已完成用户 ID 隔离、系统用户评分独立存储、离线 ALS 与在线推荐服务的主要实现。完整 Hadoop、Spark、JDBC、MySQL 端到端训练仍需要在具备相应环境的机器上验证。

## 1. 系统边界

### 1.1 用户 ID

系统注册用户和 MovieLens 用户属于两个独立命名空间：

| 用户类型 | 来源 | 模型中的 ID |
|---|---|---|
| 系统用户 | MySQL `users.id` | 直接使用真实 `users.id`（正整数） |
| MovieLens 用户 | HDFS `ratings.csv` 的 `userId` | `-userId`（取负，成为负整数） |

训练开始前会验证系统用户 ID 为正整数。MovieLens 用户 ID 取负后范围约为 `[-162541, -1]`，与系统用户 ID（从 1 开始自增）完全隔离，不会产生冲突。

MovieLens 用户不会写入 `users`，也不会把 MovieLens 推荐写入应用推荐表。

### 1.2 评分与推荐

| 数据 | 存储 | 用途 |
|---|---|---|
| MovieLens 原始评分 | HDFS `ratings.csv` | ALS 训练与 MovieLens 测试集 RMSE 评估 |
| 系统用户评分 | MySQL `user_ratings` | 应用评分、ALS 训练、当前用户在线推荐 |
| 遗留评分 | MySQL `ratings` | 保留历史数据，不再由应用或训练读取 |
| 批量推荐 | MySQL `recommendations` | 仅保存系统用户的离线推荐 |
| 电影隐因子 | MySQL `model_item_factors` | 在线求解系统用户向量 |
| 模型版本 | MySQL `model_versions` | 管理 `building`、`active`、`retired` 状态 |

推荐结果不会回流为评分，避免预测结果自我强化。

## 2. 推荐流程

### 2.1 离线训练

`spark/train_als.py` 的流程如下：

1. 从 HDFS 读取 MovieLens 评分，并将 MovieLens 用户 ID 取负；
2. 从 MySQL 读取 `user_ratings`，并与 `users` 表做半连接，只保留真实系统用户；
3. MovieLens 评分按 `train_ratio` 做确定性划分（每条评分按 `md5(userId:movieId)` 哈希分桶，每个用户在训练/测试两侧都有评分，结果可复现）；系统用户评分全部加入训练集；
4. 使用合并后的评分训练非负显式反馈 ALS；
5. 只使用 MovieLens 测试集计算 RMSE，保证指标口径稳定；
6. 保存当前模型的电影隐因子；
7. 生成全量用户推荐后，只保留 `users.id` 对应的系统用户，并把 `predicted_rating` 截断回评分量纲 [0.5, 5.0]；
8. 电影因子和批量推荐写入完成后，再将模型从 `building` 发布为 `active`；
9. 发布成功后清理非当前版本的 `recommendations` / `model_item_factors` 残留数据，防止表无限膨胀。

### 2.2 在线推荐

用户提交评分后，评分写入 `user_ratings`。下一次访问推荐接口时：

1. 读取当前用户的全部评分；
2. 加载当前 `active` 模型的电影隐因子；
3. 使用带非负约束的正则化最小二乘求解当前用户向量；
4. 对未评分电影计算预测分并返回 Top-N；
5. 在线结果直接返回，不写入 `recommendations`。

在线计算只更新当前用户向量，不重新训练全量 ALS，也不会启动 Spark。没有评分的新用户直接使用当前模型的批量推荐；没有 active 模型时返回空列表。

如果 active 模型的因子缺失、因子格式错误或在线计算失败，服务会回退到该模型对应的批量推荐。

## 3. 技术栈与目录

| 层次 | 技术 |
|---|---|
| 分布式存储 | Hadoop HDFS |
| 离线计算 | PySpark 3.5 + MLlib ALS |
| 数据库 | MySQL 8.0 |
| 后端 | Python 3.12 推荐、Flask 3、SQLAlchemy 2、JWT |
| 前端 | Vue 3、Vite、Element Plus、Pinia、Vue Router |

```text
Project/
├── config.example.yaml          # 配置模板，不含真实凭据
├── config.yaml                  # 本地配置，已被 .gitignore 忽略
├── sql/
│   ├── schema.sql               # 新数据库完整 DDL
│   └── migrations/              # 已有数据库的增量迁移
├── spark/
│   ├── preprocess.py            # 从 HDFS 清洗电影元数据
│   ├── train_als.py             # ALS 训练、评估、模型发布
│   └── utils/
├── backend/
│   ├── app.py                   # Flask 入口
│   ├── config.py                # 从根目录 config.yaml 加载配置
│   └── app/
│       ├── models.py
│       ├── recommendation_core.py   # 在线向量求解与排序
│       ├── recommendation_service.py
│       └── routes/
├── frontend/
│   └── src/
├── scripts/
│   ├── init_hdfs.sh
│   ├── run_training.sh
│   ├── start_backend.py         # 可复用的 Flask 启动入口
│   └── start_dev.sh
├── start.bat                    # Windows 开发启动入口
├── tests/                       # Python 单元与契约测试
└── docs/                        # 实现指南、系统架构图、论文草稿
```

## 4. 环境要求

建议的完整环境：

- Python 3.12；
- Java 8 或更高版本；
- Node.js 18 或更高版本；
- MySQL 8.0；
- Hadoop 3.x；
- Spark 3.5；
- MySQL Connector/J，并放入 `$SPARK_HOME/jars/` 或通过 `spark-submit --jars` 提供。

Hadoop 和 Spark 作业通常运行在 Linux 节点，Flask 和 Vue 可以运行在开发机。配置中的 NameNode、MySQL 地址必须从实际运行环境可达。

## 5. 配置

先复制模板：

```bash
cp config.example.yaml config.yaml
```

Windows PowerShell：

```powershell
Copy-Item config.example.yaml config.yaml
```

重点配置：

```yaml
hdfs:
  namenode: hdfs://localhost:9000
  ratings_path: /movielens/ratings.csv
  movies_path: /movielens/movies.csv

mysql:
  host: localhost
  port: 3306
  user: movie_app
  password: change_me
  database: movie_recommend

als:
  rank: 50
  max_iter: 10
  reg_param: 0.1
  cold_start_strategy: drop
  train_ratio: 0.8
  top_n: 20

flask:
  host: 127.0.0.1
  port: 8000
  debug: false
  secret_key: replace_with_a_random_secret
```

不要把 `config.yaml`、数据库密码或 JWT 密钥提交到 Git。生产环境必须关闭 Flask debug，并限制 `flask.host`、CORS 来源和数据库账号权限。

当前代码没有显式设置 `JWT_ACCESS_TOKEN_EXPIRES`，JWT 有效期遵循 Flask-JWT-Extended 的默认配置；不要按旧文档中的“7 天”理解实际行为。

## 6. 数据库初始化与迁移

### 6.1 新数据库

```bash
mysql -u root -p < sql/schema.sql
```

`schema.sql` 共创建 8 张表：`users`、`movies`、`user_ratings`、`movie_rating_stats`、`movie_comments`、`model_versions`、`model_item_factors` 和 `recommendations`。（legacy `ratings` 已由迁移 `008_drop_legacy_ratings.sql` 删除，2026-09-14 之前的存量库执行该迁移即可。）

### 6.2 已有数据库

先备份，再执行增量迁移：

```bash
mysqldump -u root -p --single-transaction movie_recommend > movie_recommend_backup.sql
mysql -u root -p movie_recommend < sql/migrations/001_add_recommendation_model_tables.sql
mysql -u root -p movie_recommend < sql/migrations/001_add_user_role.sql
mysql -u root -p movie_recommend < sql/migrations/002_create_movie_comments_table.sql
mysql -u root -p movie_recommend < sql/migrations/004_add_user_profile_fields.sql
mysql -u root -p movie_recommend < sql/migrations/005_merge_ratings_into_user_ratings.sql
mysql -u root -p movie_recommend < sql/migrations/006_add_movie_rating_stats.sql
mysql -u root -p movie_recommend < sql/migrations/007_add_comment_edited_at.sql
mysql -u root -p movie_recommend < sql/migrations/008_drop_legacy_ratings.sql
mysql -u root -p movie_recommend < sql/migrations/009_align_schema_drift.sql
mysql -u root -p movie_recommend < sql/migrations/010_drop_redundant_indexes.sql
```

`sql/migrations/` 目录现有脚本：

| 脚本 | 作用 | 注意 |
|---|---|---|
| `001_add_recommendation_model_tables.sql` | 建 `user_ratings`、`model_versions`、`model_item_factors`、`recommendations` | 非破坏性，不改动 legacy `ratings` |
| `001_add_user_role.sql` | 加 `users.role` 字段与角色索引，并插入默认 admin 账号（密码 `admin123`） | 与上一个脚本编号撞车，需分别执行。注意其 `ALTER TABLE ... ADD COLUMN` 不幂等，已有 `role` 列时重跑会报 `Duplicate column name`。插入的 admin 哈希由 werkzeug `generate_password_hash` 生成（scrypt），与后端校验一致；首次部署后应立即修改该密码 |
| `002_create_movie_comments_table.sql` | 建 `movie_comments` 表 | 缺失会导致评论接口报表不存在 |
| `004_add_user_profile_fields.sql` | 加 `users.avatar_url`、`users.bio` | |
| `005_merge_ratings_into_user_ratings.sql` | 把 legacy `ratings` 中的评分合并进 `user_ratings` | 幂等，可重复执行 |
| `006_add_movie_rating_stats.sql` | 建 `movie_rating_stats` 表（MovieLens 五档分布） | 建表不灌数据，建完必须跑 `scripts/build_ml_rating_stats.py`，否则详情页「评分统计」只有本站评分 |
| `007_add_comment_edited_at.sql` | 给 `movie_comments` 加 `edited_at`（影评「上次修改于」） | 缺失会让影评接口报表列不存在（模型会 select 该列）。`ADD COLUMN` 不幂等，重跑报 1060 |
| `008_drop_legacy_ratings.sql` | 删除 legacy `ratings` 表（数据已由 005 合并进 `user_ratings`） | 幂等。仅 2026-09-14 之前的存量库需要执行；全新建库直接跑 `schema.sql` |
| `009_align_schema_drift.sql` | 把 2026-09-16 之前的旧库对齐到 `schema.sql`：加宽 `writers`/`countries`/`languages` 到 `VARCHAR(500)`、`movies` 补 `idx_popularity` 并去掉冗余的 `idx_rating_count`、去掉 `users` 的冗余索引、统一索引与外键名、两个外键补 `ON DELETE CASCADE` | **不可重复执行**，索引/约束已存在或不存在时报错属预期。线上库已于 2026-09-16 直接对齐，**不需要**执行 |
| `010_drop_redundant_indexes.sql` | 删 `user_ratings.idx_user`、`model_item_factors.idx_factor_version` 两个被最左前缀覆盖的冗余索引 | **不可重复执行**（重跑报 1091）。线上库已于 2026-09-17 执行 |

> 两个**已废弃**的脚本已移入 [`archive/`](archive/)：`001_add_movie_metadata_fields.sql`（MariaDB 语法，MySQL 8 报错）与 `003_rename_ratings_table.sql`（重命名从未启用），不要执行。

> 两个 `001_*.sql` 编号撞车（`001_add_recommendation_model_tables` 与 `001_add_user_role`，不同时间点分别新增），执行顺序不影响结果，但建议按上表顺序执行，`002` 不要漏。

自 2026-09-14 起，`user_ratings` 是评分的唯一事实来源：应用写入、Spark 训练、在线推荐都只读写 `user_ratings`。legacy `ratings` 已由迁移 `008_drop_legacy_ratings.sql` 删除（数据此前已由 `005` 合并）。

除 `005` 会把 `ratings` 中 `user_ratings` 尚未存在的评分行复制过来之外，其余迁移都不会复制或删除已有数据。所有迁移都支持重复执行，`model_versions.rank` 已使用 MySQL 保留字安全写法。

迁移后建议检查：

```sql
SHOW TABLES;
SHOW COLUMNS FROM model_versions;
SELECT COUNT(*) FROM movies;
SELECT COUNT(*) FROM user_ratings;
SELECT COUNT(*) FROM model_versions WHERE status = 'active';
```

## 7. MovieLens 数据与 Spark 作业

### 7.1 准备原始文件

从 [MovieLens-32M](https://grouplens.org/datasets/movielens/32m/) 下载并解压，将以下文件放入 `data/raw/`：

```text
data/raw/ratings.csv
data/raw/movies.csv
```

### 7.2 上传到 HDFS

在可以访问 Hadoop 的节点执行：

```bash
start-dfs.sh
start-yarn.sh
bash scripts/init_hdfs.sh
```

### 7.3 电影元数据预处理

```bash
spark-submit --master local[*] spark/preprocess.py
```

预处理会解析电影年份、去除无效行、按 `movie_id` 去重，并经暂存表 upsert 写入 MySQL `movies` 表。

> 可重复执行（2026-09-16 起）：清洗结果先写入每次重建的 `movies_staging` 暂存表，
> 再由 `upsert_movies_from_staging()` 以 `INSERT ... ON DUPLICATE KEY UPDATE` 合并进
> `movies`——已有电影只更新 `title`、`genres`、`year`，`import_movie_stats.py` /
> `fetch_posters.py` 维护的统计与富化列原样保留，`movie_comments`、
> `movie_rating_stats` 的外键也不会再挡住刷新。
>
> - 早期实现是 JDBC `overwrite` 直写 `movies`：TRUNCATE 会被上述两条外键挡住
>   （与引用表有无数据无关），绕过外键也会把海报、简介等扩展列清空，重载后还得
>   重跑 `scripts/build_ml_rating_stats.py`。该问题已随 staging/upsert 改造消除。
> - 若某部电影已从 MovieLens 数据中消失但仍被评论/评分统计引用，upsert 会保留
>   该行并打印 WARNING，不会强行删除。

### 7.3.1 评分统计聚合（ratings_clean.csv）

`scripts/build_ratings_clean.py`（43 行，pandas 一次 `groupby`）把 `data/raw/ratings.csv` 按 `movieId` 聚合成 `data/raw/ratings_clean.csv`（84,432 行 × 11 列：评分条数、均值、中位数、标准差、极值、四分位数等）：

```bash
backend/venv/Scripts/python.exe scripts/build_ratings_clean.py          # 重算并写出
backend/venv/Scripts/python.exe scripts/build_ratings_clean.py --check  # 只与现有产物逐格比对
```

下游 `scripts/import_movie_stats.py` 只取 `rating_mean` / `rating_count` 两列回填 `movies` 表，支撑首页热门榜的贝叶斯加权排序与电影卡片均分；五档分布另见 `build_ml_rating_stats.py`。

### 7.4 运行 ALS 训练

```bash
bash scripts/run_training.sh
```

训练前必须确认：

- HDFS 中的评分路径存在；
- `user_ratings`、`users` 和模型表已经创建；
- MySQL Connector/J 可被 Spark 加载；
- `users.id` 全部为正整数（训练前会校验，见 1.1；系统用户与取负后的 MovieLens 用户 ID 天然隔离，不需要偏移量）；
- HDFS、MySQL 地址可以从 Spark 所在节点访问。

训练失败时，正在构建的模型会被标记为 `retired`，旧的 active 模型不应被替换。训练成功后可以检查：

```sql
SELECT version, status, generated_at, activated_at
FROM model_versions
ORDER BY generated_at DESC;

SELECT model_version, COUNT(*)
FROM model_item_factors
GROUP BY model_version;

SELECT model_version, COUNT(*)
FROM recommendations
GROUP BY model_version;
```

## 8. 启动 Web 应用

### Windows

推荐从项目根目录执行：

```bat
start.bat
```

该入口会以**隐藏窗口后台运行**两个服务（不弹 cmd 窗口，关掉启动它的终端也不影响）：
优先使用 `backend/venv`，失效时回退系统 Python；日志写入 `logs\flask.log` /
`logs\vue.log`（stderr 为 `.err.log`），PID 写入 `logs\backend.pid` / `logs\vue.pid`。

启动后它还会做两件事：

1. 等前端端口真正能连上，再自动打开 <http://localhost:5173/>；
2. 挂一个隐藏的看门狗，**到点自动把两个服务停掉**（默认 4 小时，停止记录写在
   `logs\watchdog.log`）。所以没有停止脚本，小时数由参数给：

```bat
start.bat          启动，4 小时后自动停
start.bat 8        启动，8 小时后自动停
start.bat 0        启动，不自动停
```

想提前停就结束 `python` / `node` 进程，或者再跑一次 `start.bat`：它会先把上一批停掉，
等于重启（旧的看门狗发现进程没了会自己收工）。

也可以分别在前台启动：

```powershell
python scripts/start_backend.py
npm run dev --prefix frontend
```

访问：<http://127.0.0.1:5173/>

### 手动启动

后端：

```bash
cd backend
python app.py
```

前端：

```bash
cd frontend
npm install
npm run dev
```

Vite 会把 `/api/*` 代理到 `http://localhost:8000`。如果页面显示 `Total 0` 或“请求错误”，先确认 Flask 是否监听 8000 端口，再检查 MySQL 连接和后端终端日志。

> 端口说明：本项目后端固定使用 `8000`（`config.yaml` 的 `flask.port`，前端 `vite.config.js` 的代理目标必须与之保持一致）。早期文档里的 `5000` 是历史值——本机因系统端口策略不允许绑定 5000/5001，已整体迁移到 8000。

> `scripts/start_dev.sh` 会优先探测 `backend/venv`（兼容 Linux `bin/` 与 Windows `Scripts/`），探测不到再回退系统 Python；日志写入 `logs/`。

## 9. API

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| `POST` | `/api/auth/register` | 注册并返回 JWT（**账号** 3-20 位，只允许字母 / 数字 / 下划线 / 连字符；昵称初始等于账号） | 否 |
| `POST` | `/api/auth/login` | 登录并返回 JWT（`username` 字段里填**账号或自己的用户 ID**；纯数字时先按账号找、再按 ID 找） | 否 |
| `GET` | `/api/movies/` | 分页；`q` 匹配片名 / 简介，纯数字再加 ID 命中（ID 那条排最前），**`q=ID318`（小写 `id318` 同样认）则只按电影 ID 精确命中**；类型、年份筛选；`sort=popular/latest/movie_id` | 否 |
| `GET` | `/api/movies/<id>` | 电影详情（含 MovieLens 平均分与评分数） | 否 |
| `GET` | `/api/movies/<id>/rating-stats` | 评分统计（MovieLens 与本站评分合并后的平均分、五档分布；带 token 时附当前用户评分） | 否* |
| `GET` | `/api/movies/genres` | 类型列表 | 否 |
| `POST` | `/api/ratings/` | 新增或更新当前用户评分（`rating` 取值范围 **0.5 ~ 5.0**，0 与其它越界值一律 400；评分可单独提交，但详情页的「发表」要求与影评成对） | 是 |
| `DELETE` | `/api/ratings/<movie_id>` | 删除自己对该电影的评分（**只删评分**，影评留在原地退成「仅自己可见」；详情页影评卡片的「删除 → 保留影评」会调它） | 是 |
| `POST` | `/api/movies/<id>/comments` | 发表影评（正文必填，**且要求作者此刻已有评分**，否则 400——评分与影评成对发表；同一 user 对同一电影只保留一条，重复提交即更新正文并记 `edited_at`） | 是 |
| `GET` | `/api/movies/<id>/comments` | 影评列表（**附带作者头像、显示名与星级**；另用 `my_comment` 单独返回当前用户那条，供置顶展示） | 否* |
| `PUT` | `/api/comments/<id>` | 编辑自己的影评（正文必填，403=非本人，404=不存在；作者没有评分时改草稿走这条） | 是 |
| `DELETE` | `/api/comments/<id>` | 删除自己的影评；带 `?with_rating=1` 时连同自己的评分一起删（详情页影评卡片「删除 → 不保留」走这条，两行同一次提交） | 是 |
| `GET`/`PUT` | `/api/profile/info` | 个人信息读取 / 修改（**昵称**、签名、**头像**；账号不可改——请求里带 `username` 一律 400。头像 `avatar_url` 只接受内置清单里的地址，或 `null`＝恢复默认） | 是 |
| `GET` | `/api/profile/avatars` | 内置 SVG 头像清单（`[{id, name, url}]`，顺序即选择面板里的顺序） | 否 |
| `GET` | `/api/profile/avatars/<name>.svg` | 读取内置 SVG 头像本身（文件名白名单校验，不鉴权） | 否 |
| `POST` | `/api/profile/avatar` | 上传头像（multipart，字段名 `file`，jpg/png/gif/webp 且 ≤2MB；落 `backend/uploads/avatars`，换新图即删旧图）——**界面上已不再提供这个入口**，保留是为了历史数据与手工调用 | 是 |
| `GET` | `/api/profile/avatar/<filename>` | 读取历史上传的头像图片本身（文件名含 32 位随机串，故不鉴权） | 否 |
| `GET` | `/api/profile/stats` | 观影统计（评分分布、最爱类型/导演） | 是 |
| `GET` | `/api/profile/comments` | 我的评论（分页，附带我在该片评分的星级） | 是 |
| `GET` | `/api/recommendations/` | 当前用户推荐 | 是 |
| `GET` | `/api/admin/stats` | 系统统计（用户/电影/评分/推荐量、active 模型） | admin |
| `GET`/`POST` | `/api/admin/users` | 用户列表 / 新建用户 | admin |
| `PATCH`/`DELETE` | `/api/admin/users/<id>` | 修改 / 删除用户 | admin |
| `GET` | `/api/admin/movies` | 电影列表 | admin |
| `PATCH`/`DELETE` | `/api/admin/movies/<id>` | 修改 / 删除电影 | admin |
| `GET` | `/api/admin/ratings` | 评分列表 | admin |
| `DELETE` | `/api/admin/ratings/<id>` | 删除评分 | admin |
| `GET` | `/api/admin/models` | 模型版本列表 | admin |
| `POST` | `/api/admin/models/<version>/activate` | 激活模型 | admin |
| `DELETE` | `/api/admin/models/<version>` | 删除模型（active 不可删） | admin |

需要鉴权的请求附带：

```http
Authorization: Bearer <JWT>
```

\* `/api/movies/<id>/rating-stats` 的可选鉴权：未登录也能拿到统计；若带 `Authorization` 头，额外返回 `my_rating`（当前用户对该片的评分，未评过为 `null`），用于详情页评分控件的回显。

\* `/api/movies/<id>/comments` 同样身份可选：游客也能读影评列表，不会 401；带 token 时额外返回 `my_comment`（当前用户在这部电影下的影评，没有则为 `null`），详情页据此在原输入框的位置置顶展示自己的影评。列表里每条影评都带 `visible` 字段——作者当前没有有效评分的影评对外隐藏（例如在个人中心把这部片子的评分删掉了，或在影评卡片的「删除」里选了「保留影评」），只有作者本人请求时才会出现在列表和自己的 `my_comment` 里。

> 统计口径：**MovieLens 全量评分 + 本站 `user_ratings` 合并**——平均分按评分条数加权、五档分布逐档相加，接口响应与页面上都不区分来源。
> MovieLens 侧的分档计数来自 `movie_rating_stats` 表（`scripts/build_ml_rating_stats.py` 聚合 `data/raw/ratings.csv` 生成），
> 精确总和用该表的 `rating_sum`（`movies.avg_rating` 只有 2 位小数，精度不足以做加权平均）。
> 例如电影 318：`count` = 102,930 = 102,929 条 MovieLens + 1 条本站评分，`average` = 4.4。

评分示例：

```json
{
  "movie_id": 1,
  "rating": 4.5
}
```

同一系统用户对同一电影重复评分时，`user_ratings` 只保留最新评分。评分成功后，下一次推荐请求即可触发当前用户的在线计算；重新训练后，新评分才会进入新的全局模型并影响其他用户的批量推荐。

## 10. 前端功能

### 10.1 电影浏览与筛选
- 电影库分页浏览；
- 搜索框四种输入：**`ID318` / `id318` 只按电影 ID 精确命中**（前缀大小写随意，数字必须紧挨着前缀；中间加空格 / 等号一律不认。走主键，最快）、纯数字如 `318` 宽匹配「片名 / 简介」且 ID 那条排最前、文字命中片名、文字命中简介；类型与年份另行筛选；
- 按热门度（评分数 ≥ 100）、评分高低、上映时间排序；其中评分人数门槛只作用于**无搜索词的浏览**——带搜索词时不设门槛，否则刚入库或冷门片按 ID / 简介都搜不到；
- 查看电影详情（海报、标题下方浅灰小字标出**电影 ID**（便于用 `ID318` 精确搜）、简介、导演、演员、国家、语言、时长）；
- 详情页展示评分统计（评分人数、平均分与五档分布；评分人数 = MovieLens 评分用户数 + 本站用户评分数，不区分评分来源）。

### 10.2 用户评分与评论
- 用户注册、登录和 JWT 持久化：**注册账号限 3-20 位，只允许字母 / 数字 / 下划线 / 连字符**（中文之类的名字走「昵称」，注册后在个人主页改）；**登录时填账号或自己的用户 ID 都行**（纯数字先按账号找、找不到再按 ID 找）；
- 详情页把评分与影评合成**一个动作、缺一不可**：录入区里**显示名（昵称优先、回落账号）+ 半星评分控件（0.5 - 5.0）排在评论框上方**（显示名在星星左侧），「发表」按钮在评论框下方，只有「打了分且写了正文」按钮才点得动，缺哪一半会在下面说明（「还差一个评分…」/「还差一句影评…」）；
- 提交顺序固定为**先落评分、再落影评**（`POST /api/ratings/` → `POST /api/movies/<id>/comments`）：服务端同样要求「有评分的影评」，绕过前端直接发也会 400；评分没改动时也不会白写一遍评分行；
- 评分可修改、可覆盖（同一用户对同一电影只保留最新一条），可在个人中心删除；
- 评分只能在 **0.5 ~ 5.0** 之间取值：详情页的评分控件不会「打 0 分」（Element Plus 的 `clearable` 已关掉，**点同一颗星两次也不会清零**），后端同样不接受 0（返回 400）；
- **星星旁边不再有「删除」**：评分不再单独撤销，撤销入口只有影评卡片上的「删除」（置顶那条与时间线里自己那条是同一个组件，行为完全一致）；
- 点「删除」会问「**这条影评草稿要保留吗**」，两个出口分别落在两个接口上：
  - **保留** → 只删评分（`DELETE /api/ratings/<movie_id>`，`user_ratings` 那一行被**硬删除**、不是改成 0 分），影评留在原地只是退成「仅自己可见」（带标签），并提示「评分已删除，这条影评现在仅你自己可见」；重新评分（0.5 星及以上）后同一条影评自动重新公开，不需要重新发表；此后该评分不再计入统计，下一次 ALS 训练也不会带上这条；
  - **不保留** → 评分与影评**一起删**（`DELETE /api/comments/<id>?with_rating=1`，两行在同一次 commit 里删掉），提示「影评与评分已删除」，置顶那块随即**变回评论输入框**；
  - 作者本来就没有评分（上一步选过「保留」）时没有评分可删，只有简单问法「确定要删除这条影评吗？」；点 X / Esc 关掉确认框则什么都不删；
  - 个人中心「我的评分」里同样能单独删除评分（同样先确认）；
- 同一用户对同一电影只保留一条影评，重复提交即更新（不追加）；
- **发表之后表单收起，不再有「修改」按钮**：输入框原来的位置被自己的影评顶上去（置顶展示，即那张带「我的影评」标签的卡片），表单只会在「还没发表」时出现；
  - **改正文** → 影评卡片上的「编辑」（就地编辑，走 `PUT /api/comments/<id>`）；「编辑」区里星星与正文一起改，点「保存」才一起落库；什么都没改就直接收起，不会白刷一次 `edited_at`；
  - **改评分** → 也在「编辑」里：**展示态的星星一律只读**（谁都不能直接点着改），进编辑态后星星才可点，值先落在草稿上，**点「保存」才落库**（`POST /api/ratings/`）；只改评分不改正文时不写 `edited_at`；控件不开 `clearable`，点同一颗星不会归零；
  - **编辑互斥** → 全页同时只允许一条影评处于编辑态：置顶那条、时间线里自己那条、别人的影评互相排他，谁先点「编辑」谁进入；**其余卡片上的操作（「编辑」和「删除」）全部禁用**，被锁住的卡片整体压暗（悬停提示「已有另一条影评正在编辑」），`remove()` 与 `onStartEdit()` 里也各有一道 `if (props.editLocked) return` 兜底，程序化触发同样不生效；
- 时间只在影评卡片**底部显示一处**：「发布于 …」（恒为发表时间 `created_at`，永不改变）与「上次修改于 …」（`edited_at`，未编辑过的影评为 `null`，此时不显示这一行）；卡片右上角不再单列时间；
- 自己那条影评除置顶展示外，仍留在下方时间线里（同一份数据）；删除行为两处一致，编辑则受上面的互斥约束；置顶展示块与时间线之间有一条分割线；
- 评论区每张影评卡片左侧都带**作者头像**（作者没设头像就回落成显示名首字符），并显示**署名（昵称优先、回落账号）**、发表时间和该用户的评分星级（无有效评分时不显示星星）；星级取列表接口返回的值，改过评分后由列表刷新带回；
- 改了评分或影评却没点「发表」就离开详情页（含切换路由、刷新/关标签页），会弹确认框提醒，可选择「提交并离开」或「不提交，直接离开」；
- 评论按时间倒序排列，支持分页浏览。

### 10.3 个性化推荐
- 查看当前用户的个性化推荐；
- 推荐卡片显示星级推荐指数（ALS 预测评分自动归一化到 0.5 - 5.0）；
- 未评分的新用户使用离线批量推荐；
- 已评分用户触发在线向量求解，立即获得更新后的推荐。

### 10.4 导航与交互
- 顶部导航栏：电影库 / 我的推荐 / 管理后台，右侧是主题切换与**显示名**下拉菜单（显示名左侧带头像，菜单含个人主页、退出登录）；
- 没设头像（含选了「恢复默认」）时，头像显示**显示名**首字符（中文取第一个字，英文首字母大写），顶栏、详情页影评卡片与个人主页同一套规则；
- 主题切换点开是三档下拉面板（日间 / 夜间 / 跟随系统），带图标、名称与当前档位勾选；
- 下拉箭头随机方向旋转动画（顺时针/逆时针，返回时反向）。

### 10.5 个人中心
- 资料就地编辑：点**昵称**或个性签名文本直接改（悬停时提示「点击编辑」，回车或「保存」落库）；昵称下方只读展示账号与 ID（账号是登录凭据，不在这里改；用 ID 也能登录）；
- 头像：点自己的头像（悬停显示「更换头像」遮罩）打开选择面板，从 **10 个内置 SVG 头像**里挑一个，**点一下立刻生效**；当前在用的那个高亮，面板底部是「恢复默认」（回落成显示名首字符）；
- 观影统计：已评分数量、平均分、发表评论数，以及评分分布与最爱类型/导演；
- 「我的评论」分页列表，每条左侧带自己的头像，可编辑（星级与正文一起改）或删除（连同该片评分一起删，二次确认，删完同步刷新统计）。

没有 active 模型时，推荐页显示空状态，这是正常状态，不代表电影数据丢失。

## 11. 测试与验证

本地静态验证：

```bash
python -m unittest discover -s tests -v
python -m compileall -q backend spark scripts
npm run lint --prefix frontend
npm run build --prefix frontend
git diff --check
```

当前测试覆盖（14 个测试模块、166 个用例，`discover` 默认收集全部）：

另有 19 个前端组件用例（`npm run test --prefix frontend`，vitest + jsdom，真实渲染 + DOM 断言）：

- 详情页影评区的交互约定：评分控件不开 `clearable`（点同一颗星不会归零）、星星旁没有独立的删除入口、自己的影评置顶且数据来自接口 `my_comment`、每张影评卡片都带作者头像（有头像走图片、没有回落显示名首字符）、全页同时只允许一条影评处于编辑态（`frontend/src/components/__tests__/CommentSection.spec.js`）；
- 登录页填好后能带着两个字段发出请求（`frontend/src/views/__tests__/LoginView.spec.js`）；
- 后台用户管理页的 `useTableQuery` 行为：点搜索回到第 1 页、表头排序映射成 `sort`/`order`（取消排序则不带）、删掉末页最后一条后页码回退（`frontend/src/views/admin/__tests__/AdminUsers.spec.js`）；
- 头像选择面板（`AvatarPicker`）：清单里的每个头像都渲染出来、当前那个高亮、点一下 emit 出它的 url、「恢复默认」在已用内置头像或历史上传头像时可点（已是默认时禁用）、保存中不可重复点（`frontend/src/components/__tests__/AvatarPicker.spec.js`）；
- 个人主页换头像的入口与落库：点头像打开面板并去取清单、面板里没有文件选择器、选中即用该地址调 `PUT /api/profile/info` 并同步顶栏、点「恢复默认」用 `null` 落库（`frontend/src/views/__tests__/ProfileView.spec.js`）。

- 用户 ID 命名空间隔离：系统用户 ID 为正整数，MovieLens 用户 ID 取负后落在 `[-162541, -1]`，两者天然不相交（取负逻辑见 `spark/train_als.py`）；
- ALS 输入只读 `user_ratings`、不读 legacy `ratings`，且 MovieLens 与系统用户评分都会被合并（`tests/test_training_contract.py`）；
- 评分表契约：ORM 模型指向 `user_ratings`、类名为 `UserRating`、后端各模块不再导入旧 `Rating`（`tests/test_rating_table_contract.py`）；
- 推荐结果只保留 `users` 表中的系统用户（`tests/test_training_contract.py`）；
- 第一条、第二条评分都会基于该用户全部历史评分重新计算用户向量（`tests/test_online_recommender.py`、`tests/test_recommendation_service.py`）；
- 已评分电影不进入推荐列表；非负约束下解向量不含负因子（`tests/test_online_recommender.py`）；
- 模型版本变化时刷新因子缓存；因子缺失或用户无评分时回退批量推荐（`tests/test_recommendation_service.py`）；
- 模型生命周期 SQL 契约：激活新模型前先退役旧 active，训练失败只清理 `building` 版本（`tests/test_model_lifecycle.py`）；
- auth 路由集成测试（临时 SQLite）：注册/登录成功链路、入参校验、**账号命名规则**（含边界值与非法字符）、**用用户 ID 登录**、昵称初始等于账号、账号重复 409、错误密码与不存在用户统一 401、历史 bcrypt 哈希按密码错误处理而非 500、token identity 与用户 id 一致（`tests/test_auth_routes.py`）；
- recommendations 路由集成测试：JWT 必须（无 token/坏 token 401）、推荐列表透传、空推荐降级为 200 + 提示语、路由使用 create_app 注入的服务单例（`tests/test_recommendations_route.py`）；
- 评分与影评端到端（临时 SQLite）：成对发表、一人一片一条、可见性由评分推导、编辑记 `edited_at` 等（`tests/test_comment_flow.py`）；
- 头像：内置 SVG 清单公开可读且清单里每个文件都真的存在、非法名（穿目录 / 大写 / 非 svg）404、`PUT /api/profile/info` 接受预设地址与 `null`（恢复默认）但拒绝外链与上传路径、切到预设或默认后旧的上传图被删；历史上传那条链路（落盘与地址生成、换图删旧图、扩展名/文件头/大小三类拒绝、未登录 401）仍全部覆盖（`tests/test_profile_avatar.py`，临时 SQLite + 临时上传目录）；
- 个人资料读写（临时 SQLite）：昵称任意字符（中文 / 空格 / 符号）、清空昵称后显示回落账号、超长 400、**账号不可改**（带 `username` 一律 400）、影评署名跟着昵称走（`tests/test_profile_info.py`）；
- MySQL `rank` 保留字转义（`tests/test_sql_schema_contract.py`）与 Windows 启动入口契约（`tests/test_startup_contract.py`）；
- 电影搜索的四种输入（`ID318` / `id318` 只认 ID、纯数字宽匹配且 ID 命中排最前、片名、简介）、显式 ID 的两种写法（前缀大小写随意但数字须紧挨，`id 318` / `ID=318` 会退回普通关键词）、热门榜评分人数门槛只在无搜索词时生效，前台与后台两个列表接口同规则（`tests/test_movie_search.py`，临时 SQLite）。

> 这些测试依赖 numpy，请使用已装好依赖的解释器运行（例如 `backend/venv/Scripts/python.exe -m unittest discover -s tests -v`），否则在线推荐相关的用例会因导入失败而报错。

当前缺少或尚未在本地完成的验证：

- 真实 Spark/HDFS/JDBC ALS 训练；
- 真实模型发布后，其他系统用户批量推荐变化；
- Flask 路由的自动化 API 集成测试；
- MySQL 迁移在全新数据库和已有数据上的 CI 验证；
- 前端登录、评分、推荐的浏览器自动化测试。

## 12. 审查结论与已知问题

本次审查确认身份边界和两阶段推荐主流程已经落地，但以下问题仍应在正式部署前处理：

0. **评分表分裂（已于 2026-09-14 修复）**：应用此前把评分写入 legacy `ratings`，而训练读 `user_ratings`，导致前端评分永远进不了训练集。已通过迁移 `005` 合并数据、`models.py` 改用 `UserRating`（`user_ratings` 表）修复，并由 `tests/test_rating_table_contract.py` 与 `tests/test_training_contract.py` 两端契约锁定。

1. **预处理刷新流程不安全（已于 2026-09-16 修复）**：`preprocess.py` 原先用 JDBC `overwrite` 直写 `movies`，会被 `movie_comments.movie_id → movies.movie_id` 外键挡住，即使绕过也会抹掉海报、简介等扩展元数据。现改为 staging/upsert：清洗结果先写每次重建的 `movies_staging`，再 `INSERT ... ON DUPLICATE KEY UPDATE` 合并进 `movies`，统计列与富化列保留、可重复执行（见 `spark/utils/mysql_utils.py` 的 `reset_movies_staging` / `upsert_movies_from_staging`）。
2. **Linux 联合启动脚本路径（已于 2026-09-16 确认修复）**：`scripts/start_dev.sh` 现已优先探测 `backend/venv`（兼容 `bin/` 与 `Scripts/`），探测失败才回退系统 Python，与项目约定一致。
3. **JWT 有效期（已于 2026-09-16 显式配置）**：`config.yaml` 新增 `jwt.access_token_expires_hours`（默认 168 小时 = 7 天，与文档一致），经 `backend/config.py` 注入 `JWT_ACCESS_TOKEN_EXPIRES`。另：实测签发 token 时 PyJWT 会告警 HMAC 密钥仅 27 字节，短于 SHA256 建议的 32 字节（RFC 7518 §3.2），正式部署前应更换为足够长的随机密钥。登录接口目前没有速率限制，公网部署前建议在反向代理层加限流。
4. **端到端测试不足**：auth、评分/影评、推荐三条后端链路已有路由级集成测试（2026-09-16 起，跑在临时 SQLite 上），但真实的 Spark/HDFS/JDBC 训练与浏览器端行为仍未自动化，需人工验证。
5. **伴随文档已同步（2026-09-14）**：`AGENTS.md` 的目录结构、数据库表说明、评分链路与常用命令已按代码重写；`arch_diagram.html` 的「ratings 双用途」卡片、bcrypt 描述、以及不自洽的统计数字已修正；`CLAUDE.md` 早已收敛为指向 `AGENTS.md` 的指针，不再重复维护正文。
6. **前端重复未使用模块已清理（2026-09-16）**：`api/client.js` 已并入 `api/index.js`（全站单一 axios 实例，错误 toast 统一由拦截器弹出）；`stores/user.js`、`views/MoviesView.vue` 与后端死接口 `GET /api/profile/ratings` 已删除。
7. **迁移脚本插入的默认 admin 账号曾无法登录（已于 2026-09-14 修复）**：`sql/migrations/001_add_user_role.sql` 原先写入 bcrypt 哈希（`$2b$12$...`），而 `backend/app/models.py` 用 `werkzeug.security.check_password_hash` 校验，且已安装的 werkzeug 3.0.3 早就移除了 bcrypt 支持，会抛 `ValueError: Invalid hash method ''`；`routes/auth.py` 的 `login()` 未捕获该异常，登录返回 500 而非 401。已把脚本里的哈希换成 `generate_password_hash` 生成的 scrypt 哈希（默认参数），并在活库补上 `admin` 账号（密码 `admin123`，首次部署后应立即修改）。端到端验证：`POST /api/auth/login` 用 `admin/admin123` 返回 200 并下发 token，错误密码返回 401。
8. **`schema.sql` 曾有三张表无法创建（已于 2026-09-14 修复）**：`user_ratings`、`model_item_factors`、`movie_comments` 原本写成 `CONSTRAINT ... FOREIGN KEY ... ON DELETE CASCADE COMMENT '...'`，而 MySQL 8.0 不允许在外键子句后接 `COMMENT`，会报 `1064`，导致全新库执行 `schema.sql` 时这三张表建不出来。已改为普通 SQL 注释，并同步修掉 `sql/migrations/002_create_movie_comments_table.sql` 里同样的两处；实测当时 8 张表全部可从零建出（新增的 `movie_rating_stats` 沿用同一写法，见 6.1；legacy `ratings` 已于 2026-09-16 从 `schema.sql` 移除并由迁移 008 删除，现共 8 张表）。
9. **活库与 `schema.sql` 的列漂移已消除（2026-09-14）**：活库 `movies` 曾多出 `poster_w185`、`poster_w342`、`poster_w780`、`poster_original` 四列（`varchar(500)`，TMDB 图片尺寸变体，早期“一部电影存 4 种尺寸海报”设计的残留）。它们不在 `schema.sql`、不在任何迁移脚本、全项目代码零引用，且实测 87,585 行中这四列 100% 全为 NULL——从未写入过任何值。已用 `ALTER TABLE movies DROP COLUMN` 删除，活库现为 15 列，与 `schema.sql` 一致。

## 13. 安全与运维注意事项

- 不提交 `config.yaml`、密码、JWT 密钥或数据库备份；
- 数据库迁移前先备份；
- 生产环境关闭 `debug`，不要暴露 Flask 调试器；
- MySQL 使用最小权限账号，不建议直接使用 `root`；
- 训练任务和迁移任务都应保留日志；
- 模型发布前检查 `model_item_factors`、`recommendations` 和 `model_versions` 的数量与版本一致；
- 推荐结果只能来自评分和模型，不要把推荐结果重新写入 `user_ratings`。
