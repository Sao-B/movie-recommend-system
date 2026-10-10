# -*- coding: utf-8 -*-
"""实验：① 无类型标签下融合推荐的科幻占比；② 清空好评→20 部小众好评→小众占比。"""
import json, time, uuid, urllib.request, jwt, yaml
import pymysql

cfg = yaml.safe_load(open(r'D:/Sundries/Paper/Project/config.yaml', encoding='utf-8'))
now = int(time.time())
TOKEN = jwt.encode({'sub':'8','type':'access','fresh':False,'iat':now,'nbf':now,
                    'exp':now+7200,'jti':str(uuid.uuid4())},
                   cfg['flask']['secret_key'], algorithm='HS256')
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
BASE = 'http://127.0.0.1:8000/api'

def call(method, path, payload=None):
    req = urllib.request.Request(BASE+path, data=json.dumps(payload).encode() if payload is not None else None,
        headers={'Content-Type':'application/json','Authorization':'Bearer '+TOKEN}, method=method)
    resp = opener.open(req, timeout=180)
    return json.loads(resp.read().decode())

m = cfg['mysql']
conn = pymysql.connect(host=m['host'], port=m['port'], user=m['user'],
                       password=m['password'], database=m['database'],
                       charset='utf8mb4', autocommit=True)
cur = conn.cursor()

# 0. 确认当前状态
info = call('GET', '/profile/info')
print('favorite_genres 现在 =', info.get('favorite_genres'),
      '| needs_onboarding =', info.get('needs_onboarding'))

# 1. 无类型标签下的融合推荐 → 统计 Sci-Fi
recs = call('GET', '/recommendations/?model=fusion')['data']
scifi = [r['movie']['title'] for r in recs if 'Sci-Fi' in (r['movie'].get('genres') or [])]
print('\n=== 实验①：无类型标签（评分=25 部含20科幻）===')
print('Top-20 中 Sci-Fi: %d/20' % len(scifi))
for t in scifi[:8]:
    print('   -', t[:46])

# 2. 清空管理员好评
cur.execute('DELETE FROM user_ratings WHERE user_id=8')
print('\n已清空 admin 评分:', cur.rowcount, '行')

# 3. 选 20 部小众电影（好评人数 10~200，有类型标注），打 5 分
cur.execute("""
    SELECT s.movie_id, m.title, s.rating_count, m.genres
    FROM movie_rating_stats s JOIN movies m ON m.movie_id = s.movie_id
    WHERE s.rating_count BETWEEN 10 AND 200 AND m.genres IS NOT NULL
    ORDER BY s.rating_count ASC
    LIMIT 60""")
pool = cur.fetchall()
# 确定性取前 20（跳过 (no genres listed) 已由 genres IS NOT NULL 过滤，但再保险）
niche = [r for r in pool if r[3] and '(no genres' not in r[3]][:20]
for mid, title, rc, g in niche:
    cur.execute('INSERT INTO user_ratings (user_id, movie_id, rating, rating_time) '
                'VALUES (8, %s, 5.0, NOW())', (mid,))
print('已插入 %d 部小众好评（rating_count 10~200）' % len(niche))
for mid, title, rc, g in niche:
    print('   - [%s] count=%s %s' % (g[:20], rc, title[:34]))

# 4. 融合推荐 → 小众占比（rating_count < 1000 = ALS 门控线之外）
recs = call('GET', '/recommendations/?model=fusion')['data']
print('\n=== 实验②：小众画像下的融合 Top-20 ===')
niche_out = 0
for i, r in enumerate(recs, 1):
    mid = r['movie']['movie_id']
    cur.execute('SELECT rating_count FROM movie_rating_stats WHERE movie_id=%s', (mid,))
    row = cur.fetchone()
    rc = row[0] if row else 0
    flag = ' ← 小众' if rc < 1000 else ''
    if rc < 1000:
        niche_out += 1
    print('  #%-2d count=%-7s %s%s' % (i, rc, r['movie']['title'][:40], flag))
print('\nTop-20 中小众电影（count<1000）占比: %d/20' % niche_out)
conn.close()
