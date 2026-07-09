# 基于 Hadoop 生态的个性化电影推荐系统

## 系统架构

```
MovieLens-32M (CSV)
        │
        ▼
   Hadoop HDFS          ← 原始数据分布式存储
        │
        ▼
 PySpark + ALS           ← 离线协同过滤训练（RMSE 评估）
        │
        ▼
  MySQL Database         ← 存储电影、评分、推荐结果
        │
        ▼
  Flask REST API         ← 后端服务层
        │
        ▼
  Vue 3 SPA (前端)       ← 用户交互界面
```

## 技术栈

| 层次 | 技术 |
|------|------|
| 分布式存储 | Hadoop HDFS（伪分布式单机） |
| 离线计算 | PySpark 3.5 + MLlib ALS |
| 关系数据库 | MySQL 8.0 |
| 后端 API | Python 3 + Flask 3 + SQLAlchemy |
| 前端 | Vue 3 + Vite + Element Plus + Pinia |

---

## 快速开始

### 1. 环境准备

- Java 8+（Hadoop 依赖）
- Python 3.10+
- Node.js 18+
- MySQL 8.0

### 2. 安装 Python 依赖

```bash
# 后端
pip install -r backend/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# Spark 作业
pip install -r spark/requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
```

### 3. 配置数据库密码

编辑 `config.yaml`，将 `mysql.password` 改为实际密码，并创建数据库：

```bash
mysql -u root -p < sql/schema.sql
```

### 4. 下载并上传数据集

1. 从 https://grouplens.org/datasets/movielens/32m/ 下载 MovieLens-32M
2. 解压到 `data/raw/`（需要 `ratings.csv` 和 `movies.csv`）
3. 确保 Hadoop 已启动，然后上传：

```bash
bash scripts/init_hdfs.sh
```

### 5. 运行数据预处理 & ALS 训练

```bash
# 将 mysql-connector-java-*.jar 放到 $SPARK_HOME/jars/
bash scripts/run_training.sh
```

训练完成后，`recommendations` 表中将包含所有 MovieLens 用户的 Top-20 推荐结果，终端会输出 RMSE 值。

### 6. 启动开发服务器

```bash
bash scripts/start_dev.sh
# 或分开启动：
# cd backend && python app.py
# cd frontend && npm run dev
```

访问 http://localhost:5173 即可使用。

---

## 主要功能

- **用户登录/注册**（JWT 鉴权）
- **电影库浏览**（分页 + 类型/关键词/年份筛选）
- **电影详情 + 评分**（0.5 ~ 5.0 星）
- **个性化推荐列表**（ALS 离线计算结果）

---

## API 文档

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/auth/register` | 注册 |
| POST | `/api/auth/login` | 登录，返回 JWT |
| GET  | `/api/movies` | 电影列表（支持分页、筛选） |
| GET  | `/api/movies/<id>` | 电影详情 + 平均评分 |
| GET  | `/api/movies/genres` | 所有类型 |
| POST | `/api/ratings` | 提交评分（需 JWT） |
| GET  | `/api/ratings/me` | 我的评分历史（需 JWT） |
| GET  | `/api/recommendations` | 我的推荐列表（需 JWT） |

---

## 目录结构

```
Project/
├── CLAUDE.md          # AI 上下文文件
├── README.md          # 本文件
├── config.yaml        # 全局配置（DB、HDFS、ALS 参数）
├── .gitignore
├── data/raw/          # MovieLens-32M 原始文件（不入 git）
├── hadoop/            # Hadoop 伪分布式 XML 配置
├── spark/             # PySpark 离线训练作业
│   ├── preprocess.py  # 电影元数据清洗 → MySQL
│   ├── train_als.py   # ALS 训练 + RMSE 评估 → MySQL
│   └── utils/
├── sql/schema.sql     # 完整建表 DDL
├── backend/           # Flask RESTful API
│   ├── app.py
│   └── app/
│       ├── models.py
│       └── routes/
├── frontend/          # Vue 3 SPA
│   └── src/
│       ├── views/
│       ├── components/
│       ├── stores/
│       └── api/
└── scripts/           # 一键启动脚本
```
