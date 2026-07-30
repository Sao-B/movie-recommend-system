# 基于 Hadoop 生态的个性化电影推荐系统

## 系统架构

```
MovieLens-32M (CSV)
        │
        ▼
   Hadoop HDFS          ← 原始数据分布式存储
        │
        ▼
 PySpark + ALS          ← 离线协同过滤训练（RMSE 评估）
        │
        ▼
  MySQL Database        ← 存储电影、评分、推荐结果
        │
        ▼
  Flask REST API        ← 后端服务层
        │
        ▼
  Vue 3 SPA (前端)      ← 用户交互界面
```

## 技术栈

| 层次 | 技术 |
|------|------|
| 分布式存储 | Hadoop HDFS（伪分布式，hadoop102 主节点） |
| 离线计算 | PySpark 3.5 + MLlib ALS |
| 关系数据库 | MySQL 8.0 |
| 后端 API | Python 3.12 + Flask 3 + SQLAlchemy 2.0.40 |
| 前端 | Vue 3 + Vite + Element Plus + Pinia + Vue Router |

## 核心技术原理

### ALS 协同过滤

推荐系统的核心是 **矩阵分解（Matrix Factorization）**。将 m 个用户 × n 部电影的评分矩阵 **R** 分解为两个低秩矩阵：

```
R (m×n) ≈ U (m×k) × Vᵀ (k×n)
```

- **U** 是用户隐因子矩阵，每行代表一个用户的偏好向量
- **V** 是电影隐因子矩阵，每行代表一部电影的特征向量
- **k = 50**（隐因子维度），是精度与计算成本的折中

**交替最小二乘（ALS）** 的求解方式：固定 V，对 U 做最小二乘求解；再固定 U，对 V 求解，交替迭代直至收敛。损失函数为：

```
L = Σ(r_ui - uᵀv)² + λ(‖u‖² + ‖v‖²)
```

其中 λ（`reg_param=0.1`）为 L2 正则化系数，防止过拟合。`coldStartStrategy=drop` 表示预测阶段对训练集中从未出现的用户或电影直接丢弃，避免预测结果出现 NaN。

### 为什么用 HDFS

MovieLens-32M 的 `ratings.csv` 约 700 MB，存储在 HDFS 上有两个好处：
1. Spark 可直接以分布式方式读取，自动按 Block 拆分到各 DataNode 就近计算
2. 数据与计算解耦，后续数据量增大只需扩容 HDFS，训练代码无需改动

### Spark 离线计算流程

```
ratings.csv (HDFS)
        │  sc.read.csv(hdfs_path)
        ▼
  DataFrame 预处理
  （类型转换、过滤无效评分、8:2 随机分割）
        │
        ▼
  ALS.fit(train_df)          ← 分布式训练，模型参数见 config.yaml
        │
        ▼
  model.recommendForAllUsers(top_n)   ← 为全量用户生成 Top-20 推荐
        │  Spark JDBC
        ▼
  MySQL recommendations 表   ← Flask 直接查询，无需实时计算
```

---

## 建模流程详解

本项目的机器学习部分只使用了 **一种模型：ALS 协同过滤**，没有引入内容过滤、深度学习或集成方法。但在数据准备和模型配置上有若干具体的工程决策，整体分三个阶段。

### 阶段一：数据预处理（preprocess.py）

目标是把 HDFS 上的原始 `movies.csv` 清洗后写入 MySQL `movies` 表，供前端浏览和 ALS 训练使用。

| 步骤 | 操作 | 代码实现 |
|------|------|----------|
| 年份提取 | 用正则 `\((\d{4})\)\s*$` 从标题末尾解析年份，如 `"Toy Story (1995)"` → `1995` | Spark UDF `_parse_year` |
| 去重 | 以 `movie_id` 为主键去重，消除可能的重复行 | `dropDuplicates(["movie_id"])` |
| 缺失值过滤 | 丢弃 `movie_id` 或 `title` 为空的行 | `na.drop(subset=["movie_id", "title"])` |

> 年份单独存为 `movies.year` 字段，是为了支持前端按年份筛选，避免每次查询时重复正则解析。

### 阶段二：协同过滤建模（train_als.py）

**为什么选协同过滤而非内容过滤**

MovieLens-32M 有 3,200 万条评分、20 万用户、8.7 万部电影，用户行为信号远比电影内容特征丰富。内容过滤依赖电影的文本/标签特征，而 MovieLens 的 `genres` 字段只有粗粒度的类型标签（如 `Action|Comedy`），信息量不足以区分相似电影。协同过滤只依赖"谁给谁打了多少分"，更适合这个数据集。

**关键配置说明**

```python
ALS(
    rank=50,                     # 隐因子维度：精度与计算成本的折中
    maxIter=10,                  # 迭代轮次：实验表明 10 轮已收敛
    regParam=0.1,                # L2 正则化系数：防止过拟合
    coldStartStrategy="drop",    # 预测时丢弃训练集未见过的用户/电影，避免 NaN
    nonnegative=True,            # 非负约束：隐因子不出现负值，物理意义更清晰
    implicitPrefs=False,         # 显式反馈：直接拟合评分值（0.5~5.0），非点击/播放等隐式信号
)
```

`nonnegative=True` 和 `implicitPrefs=False` 是两个容易被忽略的参数：
- **非负约束**让用户/电影的隐因子向量所有维度 ≥ 0，可以理解为"对某类题材的偏好程度"，不会出现"负偏好"抵消的情况，数值更稳定。
- **显式反馈模式**直接以评分值作为优化目标，适合 MovieLens 这种有明确数值评分的数据集；若用隐式反馈（`implicitPrefs=True`），模型会把评分值理解为"置信度"，语义完全不同。

**训练 / 测试集划分**

```python
train, test = ratings.randomSplit([0.8, 0.2], seed=42)
```

固定 `seed=42` 保证每次运行的数据划分一致，实验结果可复现。

### 阶段三：模型评估

使用均方根误差（RMSE）在测试集上评估预测精度：

```
RMSE = sqrt( (1/n) * Σ (r̂_ui - r_ui)² )
```

- `r_ui`：用户 u 对电影 i 的真实评分
- `r̂_ui`：模型预测值
- RMSE 越小说明预测越准确，本项目目标值为 < 0.85

实际训练结果：**RMSE = 0.8072**，低于目标阈值 ✅

`coldStartStrategy="drop"` 在评估阶段同样生效：测试集中若存在训练集完全未见过的用户或电影，该条记录直接从 RMSE 计算中剔除，避免预测值为 NaN 导致评估指标失真。

---

## Hadoop 集群拓扑

本项目采用宿主机上运行多台 Linux 虚拟机模拟分布式环境的部署方式，节点规划如下：

| 主机名 | 角色 | 运行服务 |
|--------|------|----------|
| hadoop100 | 基础模板（克隆源） | 不参与运行，仅作为其他节点的克隆基础 |
| hadoop102 | **主节点（Master）** | NameNode、ResourceManager、SecondaryNameNode |
| hadoop103 | 从节点（Worker） | DataNode、NodeManager |
| hadoop104 | 从节点（Worker） | DataNode、NodeManager |

**节点关系说明：**
- hadoop103、hadoop104 均从 hadoop100 克隆而来，配置高度一致
- 所有节点通过主机名互访，需在各节点 `/etc/hosts` 中配置映射
- hadoop102 ~ hadoop104 之间配置 SSH 免密登录，供 Hadoop 脚本批量操作

**日常操作入口为 hadoop102**，HDFS Web UI（`http://hadoop102:9870`）和 YARN Web UI（`http://hadoop102:8088`）均在此节点。

---

## 训练结果

| 指标 | 值 |
|------|-----|
| 数据集 | MovieLens-32M |
| 电影数 | 87,585 |
| 用户数 | 200,961 |
| 测试集 RMSE | **0.8072**（目标 < 0.85 ✅） |
| 推荐条数写入 MySQL | 4,018,960 |

---

## 配置文件说明

所有连接参数统一在根目录 `config.yaml` 中管理，各模块启动时读取，**代码中无硬编码**。

```yaml
hdfs:
  namenode: hdfs://192.168.10.102:9000  # hadoop102 内网 IP，即 NameNode 地址
  data_dir: /movielens                  # HDFS 数据根目录
  ratings_path: /movielens/ratings.csv  # 原始评分文件
  movies_path:  /movielens/movies.csv   # 原始电影元数据

mysql:
  host: 192.168.10.1    # 宿主机 IP（VM 通过此地址连接宿主机上的 MySQL）
  port: 3306
  user: root
  password: "000000"    # ⚠ 按实际密码修改
  database: movie_recommend
  charset: utf8mb4

als:
  rank: 50              # 隐因子维度，越大越精确但训练越慢
  max_iter: 10          # ALS 迭代轮次
  reg_param: 0.1        # L2 正则化系数
  cold_start_strategy: drop  # 冷启动：丢弃预测集中未见过的用户/电影
  train_ratio: 0.8      # 训练集比例（其余 0.2 作测试集计算 RMSE）
  top_n: 20             # 每位用户的推荐条数

flask:
  host: "0.0.0.0"
  port: 5000
  debug: true           # 生产部署时改为 false
  secret_key: "..."     # ⚠ 生产环境必须替换为随机强密钥

cors:
  origins:              # 允许跨域访问的前端地址
    - "http://localhost:5173"
```

> **部署时必须修改的字段：** `mysql.password`、`flask.secret_key`；若 HDFS 节点 IP 有变化同步更新 `hdfs.namenode`。

---

## 快速开始

### 1. 环境准备

- Java 8+（Hadoop 依赖）
- Python 3.12
- Node.js 18+
- MySQL 8.0
- Hadoop 3.x + Spark 3.5（部署于 Linux VM，hadoop102 主节点）

### 2. 数据库初始化

```bash
mysql -u root -p < sql/schema.sql
```

编辑 `config.yaml`，将 `mysql.password` 改为实际密码。

### 3. 安装 Python 依赖

```bash
# 后端（建议使用 venv）
cd backend
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Linux/macOS
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

> 注意：需要 SQLAlchemy >= 2.0.40，requirements.txt 已固定版本。

### 4. 下载并上传数据集（在 hadoop102 执行）

1. 从 https://grouplens.org/datasets/movielens/32m/ 下载 MovieLens-32M
2. 解压到 `data/raw/`（需要 `ratings.csv` 和 `movies.csv`）
3. 确保 Hadoop 已启动，然后上传：

```bash
start-dfs.sh && start-yarn.sh
bash scripts/init_hdfs.sh
```

### 5. 数据预处理 & ALS 训练（在 hadoop102 执行）

```bash
# 将 mysql-connector-java-*.jar 放到 $SPARK_HOME/jars/
bash scripts/run_training.sh
```

训练完成后终端输出示例：
```
测试集 RMSE: 0.8072
推荐条数: 4,018,960
写入完成 ✓
```

### 6. 启动后端（Windows 开发机）

```bash
cd backend
venv\Scripts\activate
python app.py
# Flask 运行在 http://localhost:5000
```

### 7. 启动前端（Windows 开发机）

```bash
cd frontend
npm run dev
# Vite 运行在 http://localhost:5173（自动代理 /api → :5000）
```

访问 http://localhost:5173 即可使用。

---

## 数据库设计

```
users           ratings          movies          recommendations
─────────       ────────         ──────          ───────────────
id (PK)    ←── user_id      ──→ movie_id (PK)  user_id
username        movie_id    ←─  title           movie_id
password_hash   rating          genres          predicted_rating
created_at      rated_at        year            rank
                                                generated_at
```

**设计决策说明：**

- **`ratings` 双用途**：同时存储 MovieLens 原始评分（离线训练数据源）和前端用户实时评分。`UNIQUE(user_id, movie_id)` 保证同一用户对同一电影只保留最新评分；下次离线训练时新评分自动纳入数据集。

- **`recommendations` 全量覆盖**：每次 Spark 训练完成后全量写入，旧数据由 `mysql_utils.py` 的 `truncate` 选项清除再插入，保证推荐结果与最新模型一致。`rank` 字段（1 = 最高）便于 Flask 直接 `ORDER BY rank` 返回有序列表。

- **`movies.year` 冗余字段**：MovieLens 原始 title 格式为 `"Toy Story (1995)"`，导入时解析年份存入独立字段，支持前端按年份筛选而无需每次正则解析。

- **无外键约束**：`ratings` 和 `recommendations` 的 `movie_id` 均无 FK，改由应用层保证一致性。原因是 Spark JDBC 批量写入时外键检查开销较大，且 MovieLens 数据在入库前已做清洗，一致性有保证。

---

## 主要功能

- **用户注册 / 登录**（JWT 鉴权，有效期 7 天）
- **电影库浏览**（分页 + 类型 / 关键词 / 年份筛选，共 87,585 部）
- **电影详情 + 评分**（0.5 ~ 5.0 星，半星精度）
- **个性化推荐列表**（ALS 离线预计算，Top-20）

---

## API 文档

| 方法 | 路径 | 说明 | 鉴权 |
|------|------|------|------|
| POST | `/api/auth/register` | 注册 | 否 |
| POST | `/api/auth/login` | 登录，返回 JWT | 否 |
| GET  | `/api/movies/` | 电影列表（分页 + 筛选） | 否 |
| GET  | `/api/movies/<id>` | 电影详情 + 平均评分 | 否 |
| GET  | `/api/movies/genres` | 所有类型列表 | 否 |
| POST | `/api/ratings/` | 提交评分 | 是 |
| GET  | `/api/ratings/me` | 我的评分历史 | 是 |
| GET  | `/api/recommendations/` | 我的推荐列表 | 是 |

> Flask Blueprint 路由统一带尾部斜杠，请求时需保持一致。

---

## 开发联调说明

### Vite 代理

前端开发时所有 `/api/*` 请求由 Vite 代理转发到 Flask，无需手动处理跨域：

```js
// frontend/vite.config.js
proxy: {
  '/api': { target: 'http://localhost:5000', changeOrigin: true }
}
```

生产部署时需改用 Nginx 做反向代理，或将 Flask 与 Vue 构建产物同域部署。

### Flask 路由尾部斜杠

Blueprint 路由统一注册为带尾部斜杠（如 `/api/movies/`）。Axios 请求必须与之一致，否则 Flask 返回 **308 重定向**，部分请求会丢失请求体（POST body）。

### JWT 鉴权流程

```
1. POST /api/auth/login  →  返回 { token: "eyJ..." }
2. 前端 Pinia store 保存 token
3. Axios 请求拦截器自动注入：
   Authorization: Bearer eyJ...
4. Flask @jwt_required() 装饰器验证，有效期 7 天
```

需要鉴权的接口若缺少 Header，返回 `401 Unauthorized`。

### 冷启动处理

新注册用户在系统中没有评分记录，`recommendations` 表中也没有对应数据。当前方案：
- 首次访问推荐页时，后端返回空列表，前端提示"先去评几部电影吧"
- 用户提交评分后，下一次离线训练（重新运行 `run_training.sh`）即可生成该用户的推荐

---

## 目录结构

```
Project/
├── CLAUDE.md              # AI 上下文文件
├── README.md              # 本文件
├── config.yaml            # 全局配置（DB、HDFS、ALS 参数）
├── .gitignore
├── data/
│   ├── raw/               # MovieLens-32M 原始文件（不入 git）
│   └── processed/         # 预处理中间结果（不入 git）
├── hadoop/                # Hadoop 伪分布式 XML 配置
├── spark/                 # PySpark 离线训练作业
│   ├── preprocess.py      # 数据清洗 → HDFS Parquet
│   ├── train_als.py       # ALS 训练 + RMSE 评估 → MySQL
│   └── utils/
│       ├── spark_session.py
│       └── mysql_utils.py
├── sql/schema.sql         # 完整建表 DDL
├── backend/               # Flask RESTful API
│   ├── app.py
│   ├── config.py
│   ├── requirements.txt
│   └── app/
│       ├── models.py      # SQLAlchemy ORM
│       ├── db.py
│       └── routes/
│           ├── auth.py
│           ├── movies.py
│           ├── recommendations.py
│           └── ratings.py
├── frontend/              # Vue 3 SPA
│   ├── package.json
│   ├── vite.config.js
│   └── src/
│       ├── main.js
│       ├── App.vue
│       ├── router/index.js
│       ├── stores/auth.js
│       ├── api/           # Axios 封装（index / auth / movies / ratings / recommendations）
│       ├── views/         # HomeView / MovieDetailView / RecommendationsView / LoginView / RegisterView
│       └── components/    # NavBar / MovieCard
└── scripts/               # 一键脚本
    ├── init_hdfs.sh       # 创建 HDFS 目录并上传数据
    ├── run_training.sh    # 提交 Spark 训练作业
    └── start_dev.sh       # 同时启动 Flask + Vue 开发服务器
```
