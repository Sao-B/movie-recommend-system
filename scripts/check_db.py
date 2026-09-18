#!/usr/bin/env python3
"""检查 MySQL 中各表的数据量。凭据统一从 config.yaml 读取。

用法::

    python scripts/check_db.py

说明：
- 评分以 `user_ratings` 为唯一事实来源；legacy `ratings` 表已由 migrations/008 删除。
- MovieLens 原始评分不在 MySQL，存放在 HDFS 的 ratings.csv。
"""
import os
import sys

import pymysql

# 本文件在 scripts/ 下，config_loader 在项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import mysql_kwargs  # noqa: E402

conn = pymysql.connect(**mysql_kwargs())

cursor = conn.cursor()

# 检查各表的数据量
queries = [
    ("系统用户数", "SELECT COUNT(*) FROM users"),
    ("电影总数", "SELECT COUNT(*) FROM movies"),
    ("系统用户评分数", "SELECT COUNT(*) FROM user_ratings"),
    ("评论数", "SELECT COUNT(*) FROM movie_comments"),
    ("推荐条数", "SELECT COUNT(*) FROM recommendations"),
    ("有推荐的用户数", "SELECT COUNT(DISTINCT user_id) FROM recommendations"),
    ("电影隐因子条数", "SELECT COUNT(*) FROM model_item_factors"),
]

for label, query in queries:
    cursor.execute(query)
    print(f"{label}: {cursor.fetchone()[0]:,}")

# model_versions 的实际列：version / rank / reg_param / status / generated_at / activated_at
cursor.execute(
    "SELECT version, `rank`, reg_param, activated_at "
    "FROM model_versions WHERE status = 'active' ORDER BY activated_at DESC"
)
active_models = cursor.fetchall()
if active_models:
    for version, rank, reg_param, activated_at in active_models:
        print(f"活跃模型: {version} (rank={rank}, reg_param={reg_param}, 激活于 {activated_at})")
else:
    print("活跃模型: 无")

conn.close()
