# -*- coding: utf-8 -*-
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
conn.close()

factor_ids = np.array(fids); factor_mat = np.array(frows, dtype=float)
fav = {'Sci-Fi', 'Action'}
hist = sorted(mm for mm, r in ratings if r >= 4.0)
seen = set(mm for mm, _ in ratings)

g = ItemCFGraph(r'D:/Sundries/Paper/Project/experiments/itemcf/out/full/20261004T195616Z-d535c178/model', verify_hashes=False)
contrib = {}
for src in hist:
    for tgt, sim in g.neighbors_of(src):
        if tgt in seen:
            continue
        contrib.setdefault(tgt, []).append(float(sim))

fpos = {int(v): p for p, v in enumerate(factor_ids)}
rows = [fpos[mm] for mm, _ in ratings if mm in fpos]
vals = [r for mm, r in ratings if mm in fpos]
vec = solve_user_vector(factor_mat[rows], np.array(vals), 0.1)
als_scores = factor_mat @ vec
als_by = dict(zip(factor_ids.tolist(), als_scores.tolist()))

def genres_of(mm):
    return set((movies.get(mm, ('', ''))[1] or '').split('|')) - {''}

union = sorted(set(contrib) | set(mm for mm in als_by if mm not in seen and genres_of(mm)))
n = len(union)
i_raw = np.zeros(n); i_avail = np.zeros(n, dtype=bool)
als = np.full(n, -np.inf); als_avail = np.zeros(n, dtype=bool)
gen = np.full(n, -np.inf); gen_avail = np.zeros(n, dtype=bool)
for p, mm in enumerate(union):
    pairs = contrib.get(mm)
    if pairs:
        i_raw[p] = sum(sims for sims in pairs); i_avail[p] = True
    if mm in als_by:
        als[p] = max(0.0, als_by[mm] / 5.0); als_avail[p] = True
    gs = genres_of(mm)
    if gs:
        gen[p] = len(gs & fav) / float(len(fav)); gen_avail[p] = True

def norm(v, avail):
    out = np.zeros(n)
    if avail.any():
        peak = v[avail].max()
        if peak > 0:
            out[avail] = np.clip(v[avail] / peak, 0, 1)
    return out

i_n = norm(i_raw, i_avail); a_n = norm(als, als_avail); g_n = norm(gen, gen_avail)
W = np.array([0.4, 0.5, 0.1])
S = np.column_stack([i_n, a_n, g_n])
M = np.column_stack([i_avail, als_avail, gen_avail]).astype(float)
fused = (S @ W) / np.where(M @ W > 0, M @ W, 1)
fused = np.where(M @ W > 0, fused, -np.inf)
top = np.argsort(-fused)[:10]
print('=== V4 三路融合（w=0.4/0.5/0.1）admin Top-10 ===')
for r, p in enumerate(top, 1):
    mm = union[p]
    t, gg = movies.get(mm, ('?', ''))
    print('  #%-2d %-42s genres=%s' % (r, t[:42], gg))
