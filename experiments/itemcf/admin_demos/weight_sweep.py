# -*- coding: utf-8 -*-
"""小众画像 + [Comedy] 标签下，不同权重配比的融合推荐小众占比。"""
import sys, json
sys.path.insert(0, r'D:/Sundries/Paper/Project/backend')
import pymysql, yaml
import numpy as np
from app.itemcf_service import ItemCFGraph
from app.recommendation_core import solve_user_vector

cfg = yaml.safe_load(open(r'D:/Sundries/Paper/Project/config.yaml', encoding='utf-8'))
m = cfg['mysql']
conn = pymysql.connect(host=m['host'], port=m['port'], user=m['user'],
                       password=m['password'], database=m['database'], charset='utf8mb4')
cur = conn.cursor()
cur.execute('SELECT movie_id, rating FROM user_ratings WHERE user_id=8')
ratings = [(int(a), float(b)) for a, b in cur.fetchall()]
cur.execute("SELECT movie_id, factors FROM model_item_factors WHERE model_version='als-20261005T190626Z-15df5d33fbc2'")
fids, frows = [], []
for mid, fx in cur:
    fids.append(int(mid)); frows.append(json.loads(fx) if isinstance(fx, str) else list(fx))
cur.execute('SELECT movie_id, title, genres FROM movies')
movies = {int(a): (b, c) for a, b, c in cur.fetchall()}
cur.execute('SELECT movie_id, rating_count FROM movie_rating_stats')
pop = {int(a): int(b) for a, b in cur.fetchall()}
conn.close()

factor_ids = np.array(fids); factor_mat = np.array(frows, dtype=float)
fav = {'Comedy'}                      # 标签=[Comedy]，与历史主类型契合
hist = sorted(mm for mm, r in ratings if r >= 4.0)
seen = set(mm for mm, _ in ratings)
print('历史:', len(hist), '部（小众 count 10~200）; 标签: Comedy')

g = ItemCFGraph(r'D:/Sundries/Paper/Project/experiments/itemcf/out/full/20261004T195616Z-d535c178/model', verify_hashes=False)
contrib = {}
for src in hist:
    for tgt, sim in g.neighbors_of(src):
        if tgt in seen: continue
        contrib.setdefault(tgt, []).append(float(sim))

fpos = {int(v): p for p, v in enumerate(factor_ids)}
rows = [fpos[mm] for mm, _r in ratings if mm in fpos]
vals = [r for mm, r in ratings if mm in fpos]
vec = solve_user_vector(factor_mat[rows], np.array(vals), 0.1)
als_scores = factor_mat @ vec
als_by_all = dict(zip(factor_ids.tolist(), als_scores.tolist()))
gate_pool = set(mm for mm, c in pop.items() if c >= 1000)
als_by = dict((mm, s) for mm, s in als_by_all.items() if mm in gate_pool)

def genres_of(mm):
    return set((movies.get(mm, ('', ''))[1] or '').split('|')) - {''}

union = sorted(set(contrib) | set(als_by))
n = len(union)
i_raw = np.zeros(n); i_avail = np.zeros(n, dtype=bool)
als = np.full(n, np.nan); a_avail = np.zeros(n, dtype=bool)
gen = np.full(n, np.nan); g_avail = np.zeros(n, dtype=bool)
for p, mm in enumerate(union):
    pairs = contrib.get(mm)
    if pairs:
        i_raw[p] = sum(sims for sims in pairs); i_avail[p] = True
    if mm in als_by:
        als[p] = max(0.0, als_by[mm] / 5.0); a_avail[p] = True
    gs = genres_of(mm)
    if gs and fav:
        gen[p] = len(gs & fav) / float(len(fav)); g_avail[p] = True

def norm(v, avail):
    out = np.zeros(n)
    if avail.any():
        peak = v[avail].max()
        if peak > 0: out[avail] = np.clip(v[avail] / peak, 0, 1)
    return out
i_n, a_n, g_n = norm(i_raw, i_avail), norm(als, a_avail), norm(gen, g_avail)
S = np.column_stack([i_n, a_n, g_n])
M = np.column_stack([i_avail, a_avail, g_avail]).astype(float)

print()
print('%-28s %8s %10s %10s' % ('权重配比 (i/a/g)', '小众数', '热门占比', 'Comedy占比'))
for name, W in [
    ('P1 现线 (0.4/0.5/0.1)', (0.4, 0.5, 0.1)),
    ('P2 均衡 (0.55/0.25/0.2)', (0.55, 0.25, 0.2)),
    ('P3 小众友好 (0.7/0.15/0.15)', (0.7, 0.15, 0.15)),
    ('P4 纯ItemCF (1.0/0/0)', (1.0, 0.0, 0.0)),
]:
    f = (S @ W) / np.where(M @ W > 0, M @ W, 1)
    f = np.where(M @ W > 0, f, -np.inf)
    top = np.argsort(-f)[:20]
    ids = [union[p] for p in top]
    niche = sum(1 for mm in ids if pop.get(mm, 0) < 1000)
    comedy = sum(1 for mm in ids if 'Comedy' in genres_of(mm))
    print('%-28s %8d %9.0f%% %10.0f%%' % (name, niche, 100*(1-niche/20), 100*comedy/20))
    for r, mm in enumerate(ids[:5], 1):
        t, gg = movies.get(mm, ('?', ''))
        print('     #%-2d count=%-7s %-34s %s' % (r, pop.get(mm, 0), t[:34], gg[:26]))
    print()
