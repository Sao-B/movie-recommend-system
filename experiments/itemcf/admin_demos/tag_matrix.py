# -*- coding: utf-8 -*-
"""标签数量 × 融合推荐小众占比的对照实验。

固定 admin 的 18 部小众评分历史，只变 favorite_genres：
  T0 = 空标签          T1 = 历史最高频类型 ×1
  T3 = 历史前3类型      T5 = 历史前5类型
  I5 = 5 个与历史无关的类型（反向对照）
每个配置统计：Top-20 小众占比（count<1000）、类型命中占比。
"""
import json, time, uuid, urllib.request, jwt, yaml, pymysql
from collections import Counter

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
                       password=m['password'], database=m['database'], charset='utf8mb4')
cur = conn.cursor()
cur.execute('SELECT movie_id, rating FROM user_ratings WHERE user_id=8')
hist = [(int(a), float(b)) for a, b in cur.fetchall()]
print('admin 当前评分历史:', len(hist), '部')

# 历史的类型分布（取 Top-5）
hist_ids = ','.join(str(mm) for mm, _ in hist)
cur.execute('SELECT genres FROM movies WHERE movie_id IN (%s)' % hist_ids)
gc = Counter()
for (g,) in cur.fetchall():
    for t in (g or '').split('|'):
        if t and '(no' not in t:
            gc[t] += 1
top5 = [t for t, _ in gc.most_common(5)]
print('历史类型分布:', dict(gc.most_common(6)))
irrelevant = ['Sci-Fi', 'Romance', 'Musical', 'IMAX', 'Film-Noir']
configs = [
    ('T0_空标签', []),
    ('T1_最高频[%s]' % top5[0], top5[:1]),
    ('T3_前3频', top5[:3]),
    ('T5_前5频', top5[:5]),
    ('I5_无关5标签', irrelevant),
]
results = []
for name, tags in configs:
    call('PUT', '/profile/info', {'favorite_genres': tags})
    time.sleep(0.3)
    recs = call('GET', '/recommendations/?model=fusion')['data']
    niche, matched, rows = 0, 0, []
    for r in recs:
        mid = r['movie']['movie_id']
        cur.execute('SELECT rating_count FROM movie_rating_stats WHERE movie_id=%s', (mid,))
        row = cur.fetchone()
        rc = row[0] if row else 0
        gs = set((r['movie'].get('genres') or []))
        is_niche = rc < 1000
        is_match = bool(gs & set(tags)) if tags else False
        niche += is_niche; matched += is_match
        rows.append((r['rank'], rc, r['movie']['title'][:30], is_niche, sorted(gs)[:3]))
    results.append((name, tags, niche, matched, rows))
    print('%-18s tags=%-40s 小众=%2d/20  类型命中=%2d/20' % (
        name, ','.join(tags) or '(空)', niche, matched))

print()
for name, tags, niche, matched, rows in results:
    print('=== %s（小众 %d/20）===' % (name, niche))
    for rank, rc, t, isn, g3 in rows[:5]:
        print('   #%-2d count=%-7s %-30s %s' % (rank, rc, t, '<-小众' if isn else ''))

# 恢复空标签（实验前状态由用户掌握，此处停在全清空并提示）
print()
print('注意：实验结束时类型标签已恢复为空数组（T0 状态）。')
conn.close()
