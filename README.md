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

## 训练结果

| 指标 | 值 |
|------|-----|
| 数据集 | MovieLens-32M |
| 电影数 | 87,585 |
| 用户数 | 200,961 |
| 测试集 RMSE | **0.8072**（目标 < 0.85 ✅） |
| 推荐条数写入 MySQL | 4,018,960 |

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
