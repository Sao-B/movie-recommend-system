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
├── CLAUDE.md              # 本文件
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
