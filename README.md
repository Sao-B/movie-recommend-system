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
3. MovieLens 评分按 `train_ratio` 划分训练集和测试集；系统用户评分全部加入训练集；
4. 使用合并后的评分训练非负显式反馈 ALS；
5. 只使用 MovieLens 测试集计算 RMSE，保证指标口径稳定；
6. 保存当前模型的电影隐因子；
7. 生成全量用户推荐后，只保留 `users.id` 对应的系统用户；
8. 电影因子和批量推荐写入完成后，再将模型从 `building` 发布为 `active`。

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
│   ├── recommendation_core.py   # 在线向量求解与排序
│   ├── recommendation_service.py
│   └── app/
│       ├── models.py
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
└── arch_diagram.html            # 系统架构图
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
  ml_user_offset: 10000000

flask:
  host: 127.0.0.1
  port: 5000
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

`schema.sql` 会创建：

`users`、`movies`、`ratings`（legacy）、`user_ratings`、`model_versions`、`model_item_factors` 和 `recommendations`。

### 6.2 已有数据库

先备份，再执行增量迁移：

```bash
mysqldump -u root -p --single-transaction movie_recommend > movie_recommend_backup.sql
mysql -u root -p movie_recommend < sql/migrations/001_add_recommendation_model_tables.sql
```

迁移脚本不会复制、删除或自动解释旧 `ratings` 数据。迁移支持重复执行，`model_versions.rank` 已使用 MySQL 保留字安全写法。

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

预处理会解析电影年份、去除无效行、按 `movie_id` 去重，并写入 MySQL `movies` 表。

> 已知限制：`spark/preprocess.py` 当前通过 JDBC `overwrite` 写 `movies`。完整 schema 中 `user_ratings` 和 `recommendations` 对 `movies` 存在外键，数据库初始化后再次执行预处理可能因 MySQL 不允许清空被外键引用的表而失败。当前不要把这条命令当作可重复的生产刷新流程；应先改为 staging/upsert 或设计明确的元数据刷新迁移。

### 7.4 运行 ALS 训练

```bash
bash scripts/run_training.sh
```

训练前必须确认：

- HDFS 中的评分路径存在；
- `user_ratings`、`users` 和模型表已经创建；
- MySQL Connector/J 可被 Spark 加载；
- `ml_user_offset` 大于当前 `users.id` 最大值；
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

该入口会优先使用 `backend/venv`；如果虚拟环境启动器已经失效，会回退到系统 Python，并尝试加载现有本地依赖。

也可以分别启动：

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

Vite 会把 `/api/*` 代理到 `http://localhost:5000`。如果页面显示 `Total 0` 或“请求错误”，先确认 Flask 是否监听 5000 端口，再检查 MySQL 连接和后端终端日志。

> `scripts/start_dev.sh` 目前仍使用旧的虚拟环境相对路径判断，在 Linux 上可能回退到系统 Python。Linux 联合启动前应先确认脚本中的 Python 路径与实际虚拟环境一致。

## 9. API

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| `POST` | `/api/auth/register` | 注册并返回 JWT | 否 |
| `POST` | `/api/auth/login` | 登录并返回 JWT | 否 |
| `GET` | `/api/movies/` | 分页、关键词、类型、年份筛选 | 否 |
| `GET` | `/api/movies/<id>` | 电影详情及系统用户平均评分 | 否 |
| `GET` | `/api/movies/genres` | 类型列表 | 否 |
| `POST` | `/api/ratings/` | 新增或更新当前用户评分 | 是 |
| `GET` | `/api/ratings/me` | 当前用户评分历史 | 是 |
| `GET` | `/api/recommendations/` | 当前用户推荐 | 是 |

需要鉴权的请求附带：

```http
Authorization: Bearer <JWT>
```

评分示例：

```json
{
  "movie_id": 1,
  "rating": 4.5
}
```

同一系统用户对同一电影重复评分时，`user_ratings` 只保留最新评分。评分成功后，下一次推荐请求即可触发当前用户的在线计算；重新训练后，新评分才会进入新的全局模型并影响其他用户的批量推荐。

## 10. 前端功能

- 电影库分页浏览；
- 按电影名、类型和年份筛选；
- 查看电影详情和系统用户平均评分；
- 用户注册、登录和 JWT 持久化；
- 登录后提交半星评分；
- 查看当前用户的个性化推荐。

没有 active 模型时，推荐页显示空状态，这是正常状态，不代表电影数据丢失。

## 11. 测试与验证

本地静态验证：

```bash
python -m unittest discover -s tests -v
python -m compileall -q backend spark scripts
npm run build --prefix frontend
git diff --check
```

当前测试覆盖：

- MovieLens 与系统用户 ID 命名空间隔离；
- offset 冲突和 Spark 整数溢出校验；
- ALS 输入读取 `user_ratings`，不读取 legacy `ratings`；
- 推荐只保留系统用户；
- 第一条、第二条评分都会重新计算当前用户推荐；
- 已评分电影过滤；
- 模型版本变化和因子缓存刷新；
- 缺失因子时回退批量推荐；
- 模型生命周期 SQL 契约；
- MySQL `rank` 保留字和启动入口契约。

当前缺少或尚未在本地完成的验证：

- 真实 Spark/HDFS/JDBC ALS 训练；
- 真实模型发布后，其他系统用户批量推荐变化；
- Flask 路由的自动化 API 集成测试；
- MySQL 迁移在全新数据库和已有数据上的 CI 验证；
- 前端登录、评分、推荐的浏览器自动化测试。

## 12. 审查结论与已知问题

本次审查确认身份边界和两阶段推荐主流程已经落地，但以下问题仍应在正式部署前处理：

1. **预处理刷新流程不安全**：`preprocess.py` 的 JDBC `overwrite` 与电影表外键存在冲突，应改成 staging/upsert。
2. **Linux 联合启动脚本路径过时**：`scripts/start_dev.sh` 检查的是项目根目录下的 `venv`，而项目实际约定的是 `backend/venv`。
3. **JWT 有效期未显式配置**：代码与旧 README 的“7 天”描述不一致，应在配置中明确设置并同步文档。
4. **端到端测试不足**：当前测试主要是纯函数和代码契约测试，不能替代真实 Spark、MySQL 和浏览器验证。
5. **伴随文档存在历史描述**：`AGENTS.md`、`CLAUDE.md` 和 `arch_diagram.html` 中仍有旧的 `ratings` 双用途描述，维护时应同步更新。
6. **前端存在重复未使用模块**：`frontend/src/api/client.js`、`frontend/src/stores/user.js` 和 `frontend/src/views/MoviesView.vue` 当前不在主路由调用链中，后续应清理或明确其用途。

## 13. 安全与运维注意事项

- 不提交 `config.yaml`、密码、JWT 密钥或数据库备份；
- 数据库迁移前先备份；
- 生产环境关闭 `debug`，不要暴露 Flask 调试器；
- MySQL 使用最小权限账号，不建议直接使用 `root`；
- 训练任务和迁移任务都应保留日志；
- 模型发布前检查 `model_item_factors`、`recommendations` 和 `model_versions` 的数量与版本一致；
- 推荐结果只能来自评分和模型，不要把推荐结果重新写入 `user_ratings`。
