"""ItemCF 在线推荐服务测试：模型加载契约 + 查表打分口径。

打分口径对照 experiments/itemcf/core.score_user：
score(u,i) = sum_{j in H_u} s_ji，排除已看；(-score, movieId) 排序；短列表合法。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.itemcf_service import (  # noqa: E402
    ItemCFGraph,
    ItemCFModelError,
    ItemCFService,
)

ARRAY_DTYPE = np.int64


class FakeRating:
    def __init__(self, movie_id, rating):
        self.movie_id = movie_id
        self.rating = rating


class FakeRepository:
    def __init__(self, ratings):
        self.ratings = ratings
        self.movies = {}  # movie_id -> dict（默认空，按需填充）

    def get_user_ratings(self, user_id):
        return self.ratings.get(user_id, [])

    def get_movies(self, movie_ids):
        return {m: self.movies.get(m, {"movie_id": m}) for m in movie_ids}

    def get_user_dislikes(self, user_id):
        return set()

    def get_user_favorite_genres(self, user_id):
        return None


def write_model(directory, item_ids, edges, n_like, n_all, status="validated_unpublished",
                hashes=None):
    """把合成邻居图写成与全量训练产物同构的模型目录。

    :param edges: ``[(source_movie_id, target_movie_id, similarity), ...]``
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    item_ids = np.asarray(sorted(item_ids), dtype=ARRAY_DTYPE)
    pos = {int(v): p for p, v in enumerate(item_ids)}

    by_source = {}
    for source, target, sim in edges:
        by_source.setdefault(int(source), []).append((pos[int(target)], float(sim)))

    indptr = np.zeros(len(item_ids) + 1, dtype=ARRAY_DTYPE)
    targets, sims = [], []
    for p in range(len(item_ids)):
        indptr[p + 1] = indptr[p]
        source_id = int(item_ids[p])
        for target_pos, sim in sorted(by_source.get(source_id, [])):
            targets.append(target_pos)
            sims.append(sim)
            indptr[p + 1] += 1

    arrays = {
        "item_ids.npy": item_ids,
        "indptr.npy": indptr.astype(ARRAY_DTYPE),
        "target_indices.npy": np.asarray(targets, dtype=ARRAY_DTYPE),
        "similarities.npy": np.asarray(sims, dtype=np.float64),
        "cooccurrences.npy": np.ones(len(targets), dtype=ARRAY_DTYPE),
        "n_like.npy": np.asarray(n_like, dtype=ARRAY_DTYPE),
        "n_all.npy": np.asarray(n_all, dtype=ARRAY_DTYPE),
    }
    files = {}
    for name, arr in arrays.items():
        np.save(directory / name, arr, allow_pickle=False)
        if hashes is None:
            import hashlib

            digest = hashlib.sha256((directory / name).read_bytes()).hexdigest()
            files[name] = {"sha256": digest, "bytes": int((directory / name).stat().st_size)}
    payload = {
        "model_id": "model-test",
        "status": status,
        "counts": {"n_items": len(item_ids), "n_edges": len(targets)},
        "files": files if hashes is None else hashes,
        "parameters": {"beta": 0.0, "max_k": 100, "top_n": 20},
    }
    with open(directory / "model.json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return directory


class ItemCFServiceTests(unittest.TestCase):
    def setUp(self):
        # 图：10 -> 20(0.9), 30(0.5)；20 -> 30(0.7), 40(0.2)；30 -> 10(0.8)
        self._tmp = tempfile.TemporaryDirectory()
        self.model_dir = write_model(
            self._tmp.name,
            item_ids=[10, 20, 30, 40],
            edges=[(10, 20, 0.9), (10, 30, 0.5), (20, 30, 0.7), (20, 40, 0.2),
                   (30, 10, 0.8)],
            n_like=[2, 1, 2, 1],
            n_all=[10, 8, 9, 5],
        )

    def tearDown(self):
        self._tmp.cleanup()

    def _service(self, ratings, top_n=20, **kwargs):
        return ItemCFService(
            FakeRepository({1: [FakeRating(m, r) for m, r in ratings]}),
            self.model_dir, top_n=top_n, **kwargs)

    def test_basic_scoring_order_and_sum(self):
        """单来源：邻居按相似度降序，score = 来源边的相似度和。"""
        recs = self._service([(10, 5.0)]).recommendations_for(1)
        self.assertEqual([r["movie"]["movie_id"] for r in recs], [20, 30])
        self.assertAlmostEqual(recs[0]["score"], 0.9)
        self.assertAlmostEqual(recs[1]["score"], 0.5)

    def test_multi_source_sum_and_seen_exclusion(self):
        """多来源累加；已看电影（含低分 seen）不回出现在推荐里。"""
        recs = self._service([(10, 5.0), (20, 4.0), (40, 2.0)]).recommendations_for(1)
        # 30 = 0.5(来自10) + 0.7(来自20)；10/20 是好评历史、40 是低分已看 → 全排除
        self.assertEqual([r["movie"]["movie_id"] for r in recs], [30])
        self.assertAlmostEqual(recs[0]["score"], 1.2)
        self.assertEqual(recs[0]["evidence_count"], 2)
        self.assertEqual(recs[0]["top_source_movie_id"], 20)

    def test_short_list_allowed(self):
        """有证据的候选不足 top_n 时返回短列表（不补热门/零分项）。"""
        recs = self._service([(10, 5.0)], top_n=5).recommendations_for(1)
        self.assertEqual(len(recs), 2)

    def test_no_positive_ratings_returns_empty(self):
        """没有 >=4.0 的好评历史 → 空列表（由调用方决定兜底）。"""
        self.assertEqual(self._service([(40, 2.0)]).recommendations_for(1), [])
        self.assertEqual(self._service([]).recommendations_for(1), [])

    def test_history_item_not_in_graph_is_ignored(self):
        """好评了模型外的电影：只贡献 seen 排除，不贡献分数、不报错。"""
        recs = self._service([(10, 5.0), (9999, 5.0)]).recommendations_for(1)
        self.assertEqual([r["movie"]["movie_id"] for r in recs], [20, 30])

    def test_top_source_tie_breaks_by_smaller_movie_id(self):
        """最大贡献并列时取 movieId 较小的来源（与 core.score_user 一致）。"""
        directory = write_model(
            self._tmp.name + "_tie", item_ids=[1, 2, 3, 4],
            edges=[(1, 4, 0.6), (2, 4, 0.6), (3, 4, 0.1)],
            n_like=[1, 1, 1, 1], n_all=[1, 1, 1, 1])
        service = ItemCFService(
            FakeRepository({1: [FakeRating(1, 5.0), FakeRating(2, 5.0), FakeRating(3, 2.0)]}),
            directory)
        recs = service.recommendations_for(1)
        self.assertEqual(recs[0]["top_source_movie_id"], 1)
        # 只有好评历史 {1,2} 参与打分：0.6 + 0.6；3 分电影(3,2.0)只在 seen 里
        self.assertAlmostEqual(recs[0]["score"], 1.2)

    def test_load_rejects_unpublishable_status(self):
        bad = write_model(self._tmp.name + "_status", [1, 2], [(1, 2, 0.5)],
                          [1, 1], [1, 1], status="assembled_unverified")
        with self.assertRaises(ItemCFModelError):
            ItemCFGraph(bad)

    def test_load_rejects_hash_mismatch(self):
        wrong = {name: {"sha256": "0" * 64}
                 for name in ("item_ids.npy", "indptr.npy", "target_indices.npy",
                              "similarities.npy", "cooccurrences.npy",
                              "n_like.npy", "n_all.npy")}
        bad = write_model(self._tmp.name + "_hash", [1, 2], [(1, 2, 0.5)],
                          [1, 1], [1, 1], hashes=wrong)
        with self.assertRaises(ItemCFModelError):
            ItemCFGraph(bad, verify_hashes=True)

    def test_load_rejects_broken_indptr(self):
        directory = write_model(self._tmp.name + "_ptr", [1, 2], [(1, 2, 0.5)],
                                [1, 1], [1, 1])
        indptr = np.load(Path(directory) / "indptr.npy")
        indptr[-1] = 99  # indptr[-1] != n_edges
        np.save(Path(directory) / "indptr.npy", indptr, allow_pickle=False)
        import hashlib

        payload = json.loads((Path(directory) / "model.json").read_text(encoding="utf-8"))
        payload["files"]["indptr.npy"] = {
            "sha256": hashlib.sha256((Path(directory) / "indptr.npy").read_bytes()).hexdigest()}
        with open(Path(directory) / "model.json", "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with self.assertRaises(ItemCFModelError):
            ItemCFGraph(directory)


if __name__ == "__main__":
    unittest.main()
