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

def cut(value, limit=500):
    """截断到列宽以内。movies 表 poster_url/director/writers/countries/languages 都是
    varchar(500)，历史上长字符串直接把整个任务崩掉（Data too long）。"""
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None

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
                    'poster_url': cut(poster_url),
                    'overview': overview,
                    'director': cut(director),
                    'writers': cut(writers_str),
                    'cast': cast_str,
                    'countries': cut(countries_str),
                    'languages': cut(languages_str),
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
                print(f"  超时，重试 {attempt + 1}/{retry}...", end=' ', flush=True)
                time.sleep(1)
                continue
            else:
                # 网络持续超时：抛给主循环，队列行保留，下次运行自动重试
                raise
        except Exception as e:
            print(f"  获取失败: {e}", end=' ', flush=True)
            raise

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

# ---------- 待抓取队列（临时表）：断点与进度都由这张表承担 ----------
QUEUE_TABLE = "tmp_movie_fetch_queue"
UPDATE_SQL = """UPDATE movies SET
   poster_url = %s, overview = %s, director = %s, writers = %s,
   cast = %s, countries = %s, languages = %s, release_date = %s, runtime = %s
   WHERE movie_id = %s"""
DELETE_SQL = f"DELETE FROM {QUEUE_TABLE} WHERE movie_id = %s"

cursor = conn.cursor()
cursor.execute(f"""
    CREATE TABLE IF NOT EXISTS {QUEUE_TABLE} (
        movie_id INT PRIMARY KEY,
        tmdb_id VARCHAR(20) NOT NULL,
        rating_count INT NULL
    ) ENGINE=InnoDB
""")

cursor.execute(f"SELECT COUNT(*) FROM {QUEUE_TABLE}")
queued = cursor.fetchone()[0]

if queued == 0:
    print("队列为空，正在从 movies 表生成（信息不全且有 TMDB ID 的电影）...")
    cursor.execute(
        "SELECT movie_id, rating_count FROM movies "
        "WHERE poster_url IS NULL OR overview IS NULL OR director IS NULL"
    )
    rows = cursor.fetchall()
    fill = [(mid, movie_to_tmdb[mid], rc) for mid, rc in rows if mid in movie_to_tmdb]
    no_tmdb = len(rows) - len(fill)
    cursor.executemany(
        f"INSERT INTO {QUEUE_TABLE} (movie_id, tmdb_id, rating_count) VALUES (%s, %s, %s)",
        fill
    )
    conn.commit()
    queued = len(fill)
    print(f"已入队 {queued} 部（另有 {no_tmdb} 部在 links.csv 中无 tmdbId，永远补不上，不入队）")
else:
    print(f"从队列恢复，剩余 {queued} 部未处理")

cursor.execute(
    f"SELECT movie_id, tmdb_id FROM {QUEUE_TABLE} ORDER BY rating_count IS NULL, rating_count DESC"
)
queue = cursor.fetchall()
total = len(queue)
print(f"本次待抓取: {total} 部（按评分量从高到低）")

# 批量获取电影信息（单线程，不开并发）
batch = []      # [(movie_id, update_params), ...] 待写 movies
done_ids = []   # 本窗口已终结（成功或确认无数据）的队列行
count = 0
success = 0
failed = 0
skipped_error = 0

def flush():
    """提交窗口：同一事务里写 movies + 删队列行，崩溃时两边不会错位"""
    if not batch and not done_ids:
        return
    try:
        if batch:
            cursor.executemany(UPDATE_SQL, [params for _, params in batch])
        if done_ids:
            cursor.executemany(DELETE_SQL, [(i,) for i in done_ids])
        conn.commit()
    except Exception as e:
        print(f"  批量提交失败: {e}，降级为逐条提交...", flush=True)
        try:
            conn.rollback()
        except Exception:
            pass
        updated_ok = set()
        for mid, params in batch:
            try:
                cursor.execute(UPDATE_SQL, params)
                conn.commit()
                updated_ok.add(mid)
            except Exception as e2:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"  movie_id={mid} 单条提交失败，队列行保留待重试: {e2}", flush=True)
        for mid in done_ids:
            if mid in updated_ok:
                continue  # 对应 movies 更新失败的保留队列行，下次重试
            try:
                cursor.execute(DELETE_SQL, (mid,))
                conn.commit()
            except Exception as e2:
                try:
                    conn.rollback()
                except Exception:
                    pass
                print(f"  movie_id={mid} 队列出队失败，行保留待重试: {e2}", flush=True)

try:
    for movie_id, tmdb_id in queue:
        count += 1
        print(f"[{count}/{total}] movie_id={movie_id}, tmdb_id={tmdb_id}...", end=' ', flush=True)

        # 网络异常只跳过该条：队列行保留，下次运行自动重试
        try:
            info = get_movie_info_from_tmdb(tmdb_id)
        except Exception as e:
            skipped_error += 1
            print(f"✗ 获取异常，跳过（队列行保留）: {type(e).__name__}: {e}", flush=True)
            time.sleep(1)
            continue

        if info and any(v is not None for v in info.values()):
            batch.append((movie_id, (
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
            )))
            done_ids.append(movie_id)
            success += 1
            status = []
            if info['poster_url']:
                status.append("封面")
            if info['overview']:
                status.append("简介")
            if info['director']:
                status.append(f"导演: {info['director'][:20]}")
            print(f"✓ {' | '.join(status) if status else '次要字段'}", flush=True)
        else:
            # 404，或 TMDB 有记录但没有任何可用信息：确认终结，出队
            done_ids.append(movie_id)
            failed += 1
            print("✗ 未找到", flush=True)

        # 每 50 条一个提交窗口
        if len(done_ids) >= 50:
            flush()
            batch = []
            done_ids = []
            print(f"  进度 {count}/{total}，成功 {success}，无数据 {failed}，已提交", flush=True)

        # 控制请求频率，避免触发 API 限制
        time.sleep(0.1)
except KeyboardInterrupt:
    print("\n手动中断，提交已完成部分后退出...", flush=True)
    flush()
    sys.exit(130)

# 提交剩余数据
flush()

print(f"\n完成！", flush=True)
print(f"  成功入库: {success} 部", flush=True)
print(f"  确认无数据出队: {failed} 部", flush=True)
print(f"  网络异常跳过(队列行保留，下次重试): {skipped_error} 部", flush=True)

cursor.execute(f"SELECT COUNT(*) FROM {QUEUE_TABLE}")
print(f"  队列剩余: {cursor.fetchone()[0]} 部", flush=True)

# 验证结果
cursor.execute("SELECT COUNT(*) FROM movies WHERE poster_url IS NOT NULL")
total_with_poster = cursor.fetchone()[0]
cursor.execute("SELECT COUNT(*) FROM movies WHERE overview IS NOT NULL")
total_with_overview = cursor.fetchone()[0]
print(f"  数据库中有封面的电影总数: {total_with_poster}")
print(f"  数据库中有简介的电影总数: {total_with_overview}")

cursor.close()
conn.close()
