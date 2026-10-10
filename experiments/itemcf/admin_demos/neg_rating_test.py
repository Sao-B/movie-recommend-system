# -*- coding: utf-8 -*-
"""中差评行为验证：给 admin 加 3 条 1 分差评（热门科幻），观察推荐变化。"""
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
                       password=m['password'], database=m['database'], charset='utf8mb4',
                       autocommit=True)
cur = conn.cursor()

def fusion_snapshot(label):
    recs = call('GET', '/recommendations/?model=fusion')['data']
    scifi = 0
    ids = []
    print('--- %s ---' % label)
    for i, r in enumerate(recs, 1):
        mid = r['movie']['movie_id']; ids.append(mid)
        gs = r['movie'].get('genres') or []
        s = r.get('signals') or {}
        is_scifi = 'Sci-Fi' in gs
        scifi += is_scifi
        if i <= 8:
            print('  #%-2d %-38s %s' % (i, r['movie']['title'][:38],
                  '[Sci-Fi]' if is_scifi else ''))
    print('  Top-20 Sci-Fi: %d/20' % scifi)
    return ids, scifi

before_ids, before_scifi = fusion_snapshot('加差评前（18 部小众好评 + 3 部差评待加）')

# 给三部热门科幻打 1 分（中差评路径：非好评、非不感兴趣）
targets = ['Inception', 'Interstellar', 'The Martian']
for t in targets:
    cur.execute("SELECT movie_id FROM movies WHERE title LIKE %s AND movie_id IN "
                "(SELECT movie_id FROM movie_rating_stats WHERE rating_count > 5000)", (t + '%',))
    row = cur.fetchone()
    if row:
        cur.execute('INSERT INTO user_ratings (user_id, movie_id, rating, rated_at) VALUES (8, %s, 1.0, NOW())', (row[0],))
        print('差评已加: movie_id=%s (%s) rating=1.0' % (row[0], t))

after_ids, after_scifi = fusion_snapshot('加差评后')
gone = [m for m in before_ids if m not in after_ids]
new = [m for m in after_ids if m not in before_ids]
print('被移出 Top-20 的:', len(gone), '部（应含 3 部差评片）')
print('新进入 Top-20 的:', len(new), '部')
print('Sci-Fi 占比变化: %d/20 -> %d/20' % (before_scifi, after_scifi))
conn.close()
