# -*- coding: utf-8 -*-
"""实验②：admin 清空后加 20 部小众好评（count 10~200）→ 融合推荐小众占比。"""
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

cur.execute("""
    SELECT s.movie_id, m.title, s.rating_count, m.genres
    FROM movie_rating_stats s JOIN movies m ON m.movie_id = s.movie_id
    WHERE s.rating_count BETWEEN 10 AND 200 AND m.genres IS NOT NULL
      AND m.movie_id IN (SELECT movie_id FROM model_item_factors
                         WHERE model_version='als-20261005T190626Z-15df5d33fbc2')
    ORDER BY s.rating_count ASC
    LIMIT 20""")
niche = [r for r in cur.fetchall() if r[3] and '(no genres' not in r[3]]
for mid, title, rc, g in niche:
    cur.execute('INSERT INTO user_ratings (user_id, movie_id, rating, rated_at) '
                'VALUES (8, %s, 5.0, NOW())', (mid,))
print('已插入 %d 部小众好评（count 10~200，且在 ALS 因子表内）' % len(niche))
for mid, title, rc, g in niche:
    print('   - [count=%s] [%s] %s' % (rc, g[:24], title[:32]))

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
    s = r.get('signals') or {}
    print('  #%-2d count=%-7s [i=%s a=%s] %s%s' % (
        i, rc, s.get('itemcf'), s.get('als'), r['movie']['title'][:36], flag))
print('\nTop-20 中小众电影（count<1000）占比: %d/20' % niche_out)
conn.close()
