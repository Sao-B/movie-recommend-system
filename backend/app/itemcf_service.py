"""ItemCF 在线推荐服务：读冻结的邻居图，按用户好评历史查表打分。

模型来源：``experiments/itemcf`` 全量训练产物（冻结参数 k=100、beta=0、
好评阈值 4.0，本地目录见 config.yaml 的 ``itemcf.model_dir``）。

本文件**不 import experiments 包**——只按 model.json 声明的数组契约 mmap
加载，启动时校验结构一致性与 SHA-256（信任链沿用训练侧的哈希清单）。

打分口径（与 experiments/itemcf/core.score_user 一致，README §4/§6）：

    score(u,i) = sum_{j in H_u} s_ji ,   i in N_k(j) 且 i 不在已看集合

- H_u = 该用户评分 >= 4.0 的电影；已看 = 全部评过的电影（含低分）；
- 排序按 ``(-score, movie_id)``，短列表合法、不补热门、不用零分填满；
- **score 是排序分，不是预测评分**——响应里叫 ``score``，不进
  ``predicted_rating`` 字段，前端按"匹配度"展示。
"""

import hashlib
import json
import logging
import os
from threading import RLock

import numpy as np

logger = logging.getLogger(__name__)

#: 训练侧好评阈值（与 experiments/itemcf/core.POSITIVE_THRESHOLD 一致）
POSITIVE_THRESHOLD = 4.0

#: model.json 里允许接入在线服务的模型状态
_ALLOWED_STATUSES = {"validated_unpublished", "published"}

_ARRAY_FILES = (
    "item_ids.npy", "indptr.npy", "target_indices.npy", "similarities.npy",
    "cooccurrences.npy", "n_like.npy", "n_all.npy",
)


class ItemCFModelError(Exception):
    """模型目录缺失、结构不符或哈希校验失败。"""


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 22), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ItemCFGraph:
    """冻结邻居图的内存映像（数组 mmap 只读，movieId 索引一次性建好）。"""

    def __init__(self, model_dir, verify_hashes=True):
        meta_path = os.path.join(model_dir, "model.json")
        if not os.path.isfile(meta_path):
            raise ItemCFModelError("model.json not found in %s" % model_dir)
        with open(meta_path, encoding="utf-8") as handle:
            self.meta = json.load(handle)

        status = self.meta.get("status")
        if status not in _ALLOWED_STATUSES:
            raise ItemCFModelError(
                "model status %r is not publishable (allowed: %s)"
                % (status, sorted(_ALLOWED_STATUSES)))

        arrays = {}
        for name in _ARRAY_FILES:
            path = os.path.join(model_dir, name)
            if not os.path.isfile(path):
                raise ItemCFModelError("missing model array: %s" % name)
            if verify_hashes:
                expected = (self.meta.get("files") or {}).get(name, {}).get("sha256")
                if expected and _sha256(path) != expected:
                    raise ItemCFModelError("sha256 mismatch: %s" % name)
            arrays[name] = np.load(path, mmap_mode="r", allow_pickle=False)

        self.item_ids = arrays["item_ids.npy"]
        self.indptr = arrays["indptr.npy"]
        self.target_indices = arrays["target_indices.npy"]
        self.similarities = arrays["similarities.npy"]
        self.n_items = int(self.item_ids.shape[0])
        self.n_edges = int(self.target_indices.shape[0])

        counts = self.meta.get("counts") or {}
        if int(counts.get("n_items", -1)) != self.n_items:
            raise ItemCFModelError("model.json n_items != item_ids length")
        if int(counts.get("n_edges", -1)) != self.n_edges:
            raise ItemCFModelError("model.json n_edges != target_indices length")
        if self.indptr.shape[0] != self.n_items + 1:
            raise ItemCFModelError("indptr length != n_items + 1")
        if not (np.diff(self.indptr) >= 0).all() or int(self.indptr[0]) != 0:
            raise ItemCFModelError("indptr is not a monotonic CSR index")
        if int(self.indptr[-1]) != self.n_edges:
            raise ItemCFModelError("indptr[-1] != n_edges")
        if not np.isfinite(self.similarities).all():
            raise ItemCFModelError("similarities contain non-finite values")

        # movieId -> 数组下标（全量约 8.4 万项，一次性建好常驻）
        self._item_pos = {int(v): p for p, v in enumerate(self.item_ids)}
        logger.info("itemcf model loaded: %s (%d items, %d edges)",
                    self.meta.get("model_id"), self.n_items, self.n_edges)

    def neighbors_of(self, source_movie_id):
        """来源电影的 ``(target_movie_id, similarity)`` 邻居切片；无邻居返回空。"""
        pos = self._item_pos.get(int(source_movie_id))
        if pos is None:
            return []
        start = int(self.indptr[pos])
        stop = int(self.indptr[pos + 1])
        if start == stop:
            return []
        targets = self.item_ids[self.target_indices[start:stop]]
        sims = self.similarities[start:stop]
        return zip(targets.tolist(), sims.tolist())


class ItemCFService:
    """基于冻结邻居图的在线推荐（历史查表累加，无训练、无逐用户求解）。"""

    def __init__(self, repository, model_dir, top_n=20, verify_hashes=True):
        self.repository = repository
        self.model_dir = model_dir
        self.top_n = int(top_n)
        self._verify_hashes = verify_hashes
        self._graph = None
        self._lock = RLock()

    def _load_graph(self):
        with self._lock:
            if self._graph is None:
                self._graph = ItemCFGraph(self.model_dir, self._verify_hashes)
            return self._graph

    def recommendations_for(self, user_id):
        """返回与 ALS 推荐同构的列表结构；无好评历史时返回空（由调用方兜底）。"""
        ratings = self.repository.get_user_ratings(user_id)
        if not ratings:
            return []
        # 硬排除：训练段看过的 + 用户标记"不感兴趣"的
        seen = {int(row.movie_id) for row in ratings}
        seen |= self.repository.get_user_dislikes(user_id)
        history = sorted(
            int(row.movie_id) for row in ratings
            if float(row.rating) >= POSITIVE_THRESHOLD
        )
        if not history:
            return []

        graph = self._load_graph()
        # 贡献按来源 movieId 升序累加（item_ids 本身升序，history 也已排序），
        # 保证浮点求和顺序确定，与 core.score_user 的并列排序口径一致。
        contribs = {}
        for source_id in history:
            for target_id, sim in graph.neighbors_of(source_id):
                if target_id in seen:
                    continue
                contribs.setdefault(target_id, []).append((source_id, float(sim)))

        scored = []
        for target_id in sorted(contribs):
            pairs = contribs[target_id]
            total = 0.0
            top_source, top_sim = None, None
            for source_id, sim in pairs:
                total += sim
                if top_sim is None or sim > top_sim:
                    top_source, top_sim = source_id, sim
            if not total > 0.0:
                continue
            scored.append({
                "movie_id": target_id,
                "score": round(total, 6),
                "evidence_count": len(pairs),
                "top_source_movie_id": top_source,
            })
        scored.sort(key=lambda r: (-r["score"], r["movie_id"]))
        scored = scored[:self.top_n]
        if not scored:
            return []

        movies = self.repository.get_movies([r["movie_id"] for r in scored])
        result = []
        for rank, row in enumerate(scored, start=1):
            movie = movies.get(row["movie_id"])
            if movie is None:
                continue  # 元数据缺失的电影不进列表，也不伪造条目
            result.append({
                "rank": len(result) + 1,
                "score": row["score"],
                "predicted_rating": None,
                "evidence_count": row["evidence_count"],
                "top_source_movie_id": row["top_source_movie_id"],
                "movie": movie.to_dict() if hasattr(movie, "to_dict") else movie,
            })
        return result
