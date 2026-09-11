#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 TMDB API 批量获取电影封面 URL 并更新到 MySQL"""
import csv
import time
import pymysql
import requests
import sys
from collections import defaultdict

from config_loader import mysql_kwargs, tmdb_config

# 设置输出编码为 UTF-8
sys.stdout.reconfigure(encoding='utf-8')

# TMDB API 配置（凭据从 config.yaml 读取）
_tmdb = tmdb_config()
TMDB_API_KEY = _tmdb['api_key']
TMDB_BASE_URL = _tmdb['base_url']
TMDB_IMAGE_BASE = _tmdb['poster_base_url']  # w500 是中等尺寸

# 代理配置 - 根据你的代理类型选择一个
# HTTP 代理
PROXIES = {
    'http': 'http://127.0.0.1:65532',
    'https': 'http://127.0.0.1:65532'
}
# 如果是 SOCKS5 代理，取消下面的注释并注释掉上面的 HTTP 代理
# PROXIES = {
#     'http': 'socks5://127.0.0.1:65532',
#     'https': 'socks5://127.0.0.1:65532'
# }
# 如果不需要代理，设置为 None
# PROXIES = None

# MySQL 配置（凭据从 config.yaml 读取）
conn = pymysql.connect(**mysql_kwargs())

def get_movie_info_from_tmdb(tmdb_id, retry=3):
    """从 TMDB 获取电影完整信息"""
    if not tmdb_id or tmdb_id == '':
        return None

    for attempt in range(retry):
        try:
            url = f"{TMDB_BASE_URL}/movie/{tmdb_id}"
            params = {"api_key": TMDB_API_KEY, "language": "zh-CN", "append_to_response": "credits"}
            response = requests.get(url, params=params, proxies=PROXIES, timeout=10)

            if response.status_code == 200:
                data = response.json()

                # 海报
                poster_path = data.get('poster_path')
                poster_url = f"{TMDB_IMAGE_BASE}{poster_path}" if poster_path else None

                # 简介
                overview = data.get('overview', '').strip()
                if not overview:
                    params_en = {"api_key": TMDB_API_KEY, "language": "en-US", "append_to_response": "credits"}
                    response_en = requests.get(url, params=params_en, proxies=PROXIES, timeout=10)
                    if response_en.status_code == 200:
                        overview = response_en.json().get('overview', '').strip()

                # 导演
                credits = data.get('credits', {})
                crew = credits.get('crew', [])
                directors = [c['name'] for c in crew if c.get('job') == 'Director']
                director = ', '.join(directors[:3]) if directors else None

                # 编剧
                writers = [c['name'] for c in crew if c.get('department') == 'Writing']
                writers_str = ', '.join(writers[:5]) if writers else None

                # 主演
                cast = credits.get('cast', [])
                cast_names = [c['name'] for c in cast[:10]]
                cast_str = ', '.join(cast_names) if cast_names else None

                # 制片国家/地区
                countries = data.get('production_countries', [])
                countries_str = ', '.join([c.get('name', c.get('iso_3166_1', '')) for c in countries]) if countries else None

                # 语言
                languages = data.get('spoken_languages', [])
                languages_str = ', '.join([l.get('name', l.get('iso_639_1', '')) for l in languages]) if languages else None

                # 上映日期
                release_date = data.get('release_date') or None

                # 片长
                runtime = data.get('runtime') or None

                return {
                    'poster_url': poster_url,
                    'overview': overview,
                    'director': director,
                    'writers': writers_str,
                    'cast': cast_str,
                    'countries': countries_str,
                    'languages': languages_str,
                    'release_date': release_date,
                    'runtime': runtime
                }
            elif response.status_code == 429:
                print("  触发 API 速率限制，等待 2 秒...")
                time.sleep(2)
                continue
            elif response.status_code == 404:
                return None

        except requests.exceptions.Timeout:
            if attempt < retry - 1:
                print(f"  超时，重试 {attempt + 1}/{retry}...")
                time.sleep(1)
                continue
            else:
                print(f"  获取失败: 连接超时")
                return None
        except Exception as e:
            print(f"  获取失败: {e}")
            return None

    return None

# 读取 links.csv，建立 movieId -> tmdbId 的映射
print("正在读取 links.csv...")
movie_to_tmdb = {}

with open('data/raw/links.csv', 'r', encoding='utf-8') as f:
    reader = csv.DictReader(f)
    for row in reader:
        movie_id = int(row['movieId'])
        tmdb_id = row['tmdbId'].strip()
        if tmdb_id and tmdb_id != '':
            movie_to_tmdb[movie_id] = tmdb_id

print(f"读取完成，共 {len(movie_to_tmdb)} 部电影有 TMDB ID")

# 从数据库获取需要更新信息的电影（缺少海报、简介或导演信息）
cursor = conn.cursor()
cursor.execute(
    "SELECT movie_id FROM movies WHERE poster_url IS NULL OR overview IS NULL OR director IS NULL ORDER BY rating_count DESC"
)
movies_need_update = [row[0] for row in cursor.fetchall()]
print(f"需要获取信息的电影: {len(movies_need_update)} 部")

# 读取进度文件（如果存在）
progress_file = 'fetch_posters_progress.txt'
processed_ids = set()
try:
    with open(progress_file, 'r') as f:
        for line in f:
            movie_id = line.strip()
            if movie_id:
                processed_ids.add(int(movie_id))
    print(f"从上次进度恢复，已处理 {len(processed_ids)} 部电影")
except FileNotFoundError:
    print("首次运行，从头开始")

# 过滤掉已处理的电影
movies_need_update = [mid for mid in movies_need_update if mid not in processed_ids]
print(f"剩余需要获取信息的电影: {len(movies_need_update)} 部")

# 批量获取电影信息
batch = []
count = 0
success = 0
failed = 0

# 打开进度文件用于追加
progress_f = open(progress_file, 'a')

for movie_id in movies_need_update:
    tmdb_id = movie_to_tmdb.get(movie_id)

    if not tmdb_id:
        failed += 1
        continue

    count += 1
    print(f"[{count}/{len(movies_need_update)}] 获取 movie_id={movie_id}, tmdb_id={tmdb_id}...", end=' ')

    info = get_movie_info_from_tmdb(tmdb_id)

    if info:
        batch.append((
            info['poster_url'],
            info['overview'],
            info['director'],
            info['writers'],
            info['cast'],
            info['countries'],
            info['languages'],
            info['release_date'],
            info['runtime'],
            movie_id
        ))
        success += 1
        status = []
        if info['poster_url']:
            status.append(f"封面")
        if info['overview']:
            status.append(f"简介")
        if info['director']:
            status.append(f"导演: {info['director'][:20]}...")
        print(f"✓ {' | '.join(status)}")
    else:
        failed += 1
        print("✗ 未找到")

    # 记录已处理的电影 ID
    progress_f.write(f"{movie_id}\n")
    progress_f.flush()

    # 每 100 条批量更新一次
    if len(batch) >= 100:
        cursor.executemany(
            """UPDATE movies SET
               poster_url = %s, overview = %s, director = %s, writers = %s,
               cast = %s, countries = %s, languages = %s, release_date = %s, runtime = %s
               WHERE movie_id = %s""",
            batch
        )
        conn.commit()
        batch = []
        print(f"  已提交 {success} 条到数据库")

    # 控制请求频率，避免触发 API 限制
    time.sleep(0.1)

# 提交剩余数据
if batch:
    cursor.executemany(
        """UPDATE movies SET
           poster_url = %s, overview = %s, director = %s, writers = %s,
           cast = %s, countries = %s, languages = %s, release_date = %s, runtime = %s
           WHERE movie_id = %s""",
        batch
    )
    conn.commit()

# 关闭进度文件
progress_f.close()

print(f"\n完成！")
print(f"  成功: {success} 部")
print(f"  失败: {failed} 部")

# 验证结果
cursor.execute("SELECT COUNT(*) FROM movies WHERE poster_url IS NOT NULL")
total_with_poster = cursor.fetchone()[0]
cursor.execute("SELECT COUNT(*) FROM movies WHERE overview IS NOT NULL")
total_with_overview = cursor.fetchone()[0]
print(f"  数据库中有封面的电影总数: {total_with_poster}")
print(f"  数据库中有简介的电影总数: {total_with_overview}")

cursor.close()
conn.close()

# 完成后删除进度文件
import os
if os.path.exists(progress_file):
    os.remove(progress_file)
    print(f"  已删除进度文件 {progress_file}")
