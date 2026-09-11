#!/usr/bin/env python3
"""检查 MySQL 中各表的数据量。凭据统一从 config.yaml 读取。"""
import pymysql

from config_loader import mysql_kwargs

conn = pymysql.connect(**mysql_kwargs())

cursor = conn.cursor()

# 检查各表的数据量
queries = [
    ("MovieLens 评分数", "SELECT COUNT(*) FROM ratings WHERE user_id < 0"),
    ("推荐条数", "SELECT COUNT(*) FROM recommendations"),
    ("有推荐的用户数", "SELECT COUNT(DISTINCT user_id) FROM recommendations"),
    ("电影总数", "SELECT COUNT(*) FROM movies"),
    ("活跃模型", "SELECT name, rmse FROM model_versions WHERE is_active = 1")
]

for label, query in queries:
    cursor.execute(query)
    result = cursor.fetchone()
    if label == "活跃模型":
        if result and result[0]:
            print(f"{label}: {result[0]} (RMSE: {result[1]})")
        else:
            print(f"{label}: 无")
    else:
        print(f"{label}: {result[0]:,}")

conn.close()
