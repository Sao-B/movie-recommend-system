#!/usr/bin/env python3
"""从 ratings_clean.csv 导入电影评分统计到 MySQL"""
import csv
import os
import sys
import pymysql

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config_loader import mysql_kwargs

conn = pymysql.connect(**mysql_kwargs())

cursor = conn.cursor()

print("正在读取 ratings_clean.csv...")
with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'raw', 'ratings_clean.csv'), 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)

    batch = []
    count = 0

    for row in reader:
        movie_id = int(row['movieId'])
        rating_count = int(row['rating_count'])
        rating_mean = float(row['rating_mean'])

        batch.append((rating_mean, rating_count, movie_id))
        count += 1

        # 每 1000 条批量更新
        if len(batch) >= 1000:
            cursor.executemany(
                "UPDATE movies SET avg_rating = %s, rating_count = %s WHERE movie_id = %s",
                batch
            )
            conn.commit()
            batch = []
            print(f"已处理 {count} 条...")

    # 处理剩余数据
    if batch:
        cursor.executemany(
            "UPDATE movies SET avg_rating = %s, rating_count = %s WHERE movie_id = %s",
            batch
        )
        conn.commit()

print(f"\n总共处理 {count} 条记录")

# 验证结果
cursor.execute("SELECT COUNT(*) FROM movies WHERE rating_count > 0")
updated_count = cursor.fetchone()[0]
print(f"有评分的电影数量: {updated_count}")

cursor.execute(
    "SELECT movie_id, title, rating_count, avg_rating "
    "FROM movies ORDER BY rating_count DESC LIMIT 5"
)
print("\n评分最多的前5部电影:")
for row in cursor.fetchall():
    print(f"  {row[0]}: {row[1]} - {row[2]}评分, 平均{row[3]:.2f}★")

cursor.close()
conn.close()
print("\n导入完成！")
