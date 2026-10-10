# -*- coding: utf-8 -*-
"""管理员账号的 V4 融合推荐实跑（holdout 协议因子 + 门控 ALS + 类型信号）。"""
import csv
import sys

import numpy as np

sys.path.insert(0, "/home/h/itemcf_holdout_code")
sys.path.insert(0, "/home/h/itemcf_holdout_code/backend")

from experiments.itemcf.heldout_als_eval import (
    load_factors_from_jsonl, solve_user_vector_explicit)
from app.itemcf_service import ItemCFGraph

ADMIN = {
    1: 5.0, 318: 5.0, 356: 5.0, 2959: 5.0, 593: 5.0,
    2571: 5.0, 260: 5.0, 1196: 5.0, 79132: 5.0, 1210: 5.0,
    589: 5.0, 1270: 5.0, 541: 5.0, 7361: 5.0, 1214: 5.0,
    109487: 5.0, 480: 5.0, 32: 5.0, 60069: 5.0, 924: 5.0,
    1206: 5.0, 1200: 5.0, 48780: 5.0, 1240: 5.0, 4878: 5.0,
}
hist = sorted(ADMIN)
seen = set(hist)
FAV = {"Sci-Fi", "Action"}
W = (0.4, 0.5, 0.1)

version, reg, fids, mat = load_factors_from_jsonl(
    "/home/h/itemcf_holdout/als_holdout_factors/factors.jsonl")
print("als factors:", version, mat.shape)
g = ItemCFGraph("/home/h/itemcf_full/20261004T195616Z-d535c178/model",
                verify_hashes=False)
print("itemcf graph:", g.n_items, "items", g.n_edges, "edges")

support = {}
with open("/home/h/itemcf_holdout/fusion_ablation_v2/split_work/split_train.csv") as f:
    for r in csv.reader(f):
        mm = int(r[1])
        if float(r[2]) >= 4.0:
            support[mm] = support.get(mm, 0) + 1

titles = {}
with open("/home/h/itemcf_holdout/movie_titles.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        titles[int(row["movie_id"])] = row["title"]
genre_map = {}
with open("/home/h/itemcf_holdout/movies_genres.csv", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        genre_map[int(row["movie_id"])] = frozenset(row["genres"].split("|"))

# R1: ItemCF 证据（裸求和 + 证据数）
contrib = {}
for src in hist:
    for tgt, sim in g.neighbors_of(src):
        if tgt in seen:
            continue
        contrib.setdefault(tgt, []).append(float(sim))
r1_ids = sorted(contrib)

# R2: ALS 门控 Top-500（折入向量点积，仅支持度>=1000 的电影）
fpos = {int(v): p for p, v in enumerate(fids)}
rows = [fpos[m] for m in hist if m in fpos]
vals = [ADMIN[m] for m in hist if m in fpos]
vec = solve_user_vector_explicit(mat[rows], np.array(vals), reg)
als_scores = mat @ vec
allowed = set(m for m, n in support.items() if n >= 1000)
pairs = [(int(m), float(s)) for m, s in zip(fids.tolist(), als_scores.tolist())
         if m not in seen and m in allowed]
pairs.sort(key=lambda x: (-x[1], x[0]))
als_top500 = pairs[:500]
als_by = dict(als_top500)

union = sorted(set(r1_ids) | set(m for m, _ in als_top500))
n = len(union)
print("candidates: R1=%d, R2=%d, union=%d" % (len(r1_ids), len(als_top500), n))

i_sum = np.zeros(n); i_cnt = np.zeros(n); i_avail = np.zeros(n, dtype=bool)
als_v = np.full(n, np.nan); als_avail = np.zeros(n, dtype=bool)
gen_v = np.full(n, np.nan); gen_avail = np.zeros(n, dtype=bool)
profile = set()
for m in hist:
    profile |= genre_map.get(m, frozenset())
for p, mm in enumerate(union):
    pairs_ = contrib.get(mm)
    if pairs_:
        i_sum[p] = sum(sims for sims in pairs_)
        i_cnt[p] = len(pairs_); i_avail[p] = True
    if mm in als_by:
        als_v[p] = max(0.0, als_by[mm] / 5.0); als_avail[p] = True
    gs = genre_map.get(mm)
    if gs:
        gen_v[p] = len(gs & FAV) / float(len(FAV)); gen_avail[p] = True

def norm(v, avail):
    out = np.zeros(n)
    if avail.any():
        peak = v[avail].max()
        if peak > 0:
            out[avail] = np.clip(v[avail] / peak, 0, 1)
    return out

S = np.column_stack([norm(i_sum, i_avail), norm(als_v, als_avail),
                     norm(gen_v, gen_avail)])
M = np.column_stack([i_avail, als_avail, gen_avail]).astype(float)
Wv = np.array(W)
fused = (S @ Wv) / np.where(M @ Wv > 0, M @ Wv, 1)
fused = np.where(M @ Wv > 0, fused, -np.inf)
top = np.argsort(-fused)[:15]
print()
print("=== V4 融合（w=%.1f/%.1f/%.1f）admin Top-15 ===" % W)
scifi = 0
for r, p in enumerate(top, 1):
    mm = union[p]
    gs = genre_map.get(mm, frozenset())
    if gs & {"Sci-Fi"}:
        scifi += 1
    print("  #%-2d %-44s genres=%s" % (r, titles.get(mm, mm)[:44],
                                       "|".join(sorted(gs))[:40]))
print("Top-15 中 Sci-Fi 占比: %d/15" % scifi)
