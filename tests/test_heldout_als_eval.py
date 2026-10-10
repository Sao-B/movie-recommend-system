"""heldout_als_eval 的契约测试。

三件事：
1. ``solve_user_vector_explicit`` 与部署侧 ``recommendation_core.solve_user_vector``
   **逐位等价**（评估的必须是系统真实给出的推荐）；
2. 冻结名单/冻结 holdout 的校验逻辑：篡改即抛 ``CorrectnessError``；
3. 小样本端到端：打分口径、已看排除、短列表、summary 聚合。
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from app.recommendation_core import solve_user_vector as backend_solve  # noqa: E402
from experiments.itemcf.heldout_eval import (  # noqa: E402
    CorrectnessError, RatingSession, user_list_sha256, write_json_atomic,
)
from experiments.itemcf import heldout_als_eval as E  # noqa: E402


def write_ratings(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write("userId,movieId,rating,timestamp\n")
        for user, movie, rating, ts in rows:
            handle.write("%d,%d,%s,%d\n" % (user, movie, rating, ts))


def make_frozen_run_dir(directory, eval_users, ratings_rows, test_frac=0.2):
    """造一个最小冻结协议目录：eval_users.txt / users.json / split_holdout.csv。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "eval_users.txt").write_text(
        "".join("%d\n" % u for u in eval_users), encoding="utf-8")
    write_json_atomic(str(directory / "users.json"), {
        "counts": {"eval_users": len(eval_users)},
        "eval_salt": "test:",
        "eval_sha256": user_list_sha256(eval_users),
        "eval_size": len(eval_users),
    })
    input_path = directory / "ratings.csv"
    write_ratings(str(input_path), ratings_rows)
    session = RatingSession(str(input_path), str(directory), 1000,
                            test_frac=test_frac, eval_users=eval_users,
                            keep_all_train=True)
    session.build()
    return str(directory), str(input_path)


class TestSolverEquivalence(unittest.TestCase):
    """复刻求解器必须与部署侧逐位一致（10 组随机 + 两个边界）。"""

    def test_random_matrices_match_backend(self):
        rng = np.random.RandomState(42)
        for trial in range(10):
            rank = int(rng.randint(2, 8))
            n = int(rng.randint(1, 40))
            factors = rng.rand(n, rank)
            ratings = rng.uniform(0.5, 5.0, size=n)
            reg = float(rng.choice([0.0, 0.01, 0.1, 0.5]))
            mine = E.solve_user_vector_explicit(factors, ratings, reg)
            theirs = backend_solve(factors, ratings, reg)
            np.testing.assert_allclose(mine, theirs, atol=1e-10, rtol=0)

    def test_single_rating_and_zero_reg(self):
        factors = np.array([[1.0, 2.0]])
        ratings = np.array([4.0])
        np.testing.assert_allclose(
            E.solve_user_vector_explicit(factors, ratings, 0.0),
            backend_solve(factors, ratings, 0.0), atol=1e-12)


class FrozenProtocolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.rows = [
            (1, 101, 5.0, 100), (1, 102, 4.0, 200), (1, 103, 3.0, 300),
            (2, 101, 4.0, 110), (2, 104, 5.0, 210),
            (3, 105, 5.0, 120), (3, 106, 2.0, 220), (3, 107, 4.0, 320),
        ]
        self.eval_users = [1, 2, 3]

    def tearDown(self):
        self._tmp.cleanup()

    def test_verify_passes_on_intact_freeze(self):
        run_dir, _ = make_frozen_run_dir(
            self.base + "/run", self.eval_users, self.rows)
        users = E.verify_frozen_users(str(run_dir))
        self.assertEqual(users, self.eval_users)

    def test_verify_rejects_tampered_manifest(self):
        run_dir, _ = make_frozen_run_dir(
            self.base + "/run", self.eval_users, self.rows)
        payload = json.loads((Path(run_dir) / "users.json").read_text(encoding="utf-8"))
        payload["eval_sha256"] = "0" * 64
        (Path(run_dir) / "users.json").write_text(
            json.dumps(payload), encoding="utf-8")
        with self.assertRaises(CorrectnessError):
            E.verify_frozen_users(str(run_dir))

    def test_rebuild_split_verifies_and_rejects_tampering(self):
        run_dir, input_path = make_frozen_run_dir(
            self.base + "/run", self.eval_users, self.rows)
        work = self.base + "/work"
        E.rebuild_split_and_verify(input_path, work, str(run_dir),
                                   self.eval_users)
        # 篡改冻结 holdout：任何一行变化都必须中止
        frozen = Path(run_dir) / "split_holdout.csv"
        text = frozen.read_text(encoding="utf-8").replace("4.0", "4.5", 1)
        frozen.write_text(text, encoding="utf-8")
        with self.assertRaises(CorrectnessError):
            E.rebuild_split_and_verify(input_path, work + "2", str(run_dir),
                                       self.eval_users)


class EndToEndSmokeTests(unittest.TestCase):
    """6 个评测用户的小样本全链路：因子 jsonl -> 排序 -> summary 聚合。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        # 电影 101..108，rank=2 因子：前四部彼此相近、后四部彼此相近
        self.factor_rows = []
        vectors = {101: [1.0, 0.0], 102: [0.9, 0.1], 103: [0.8, 0.2],
                   104: [0.95, 0.05], 105: [0.0, 1.0], 106: [0.1, 0.9],
                   107: [0.2, 0.8], 108: [0.05, 0.95]}
        for movie_id in sorted(vectors):
            self.factor_rows.append({"movie_id": movie_id,
                                     "factors": vectors[movie_id]})
        self.factors_path = self.base + "/factors.jsonl"
        with open(self.factors_path, "w", encoding="utf-8") as handle:
            for row in self.factor_rows:
                handle.write(json.dumps(row) + "\n")

        # 用户 1（评测）：训练段好评 101/102，留出段 103（相关）
        # 用户 2（评测）：训练段好评 105，留出段 106（相关）
        # 用户 3（评测）：训练段只有低分 -> 跳过（no positive train -> 仍可解，
        #   但这里让训练段为空：只有留出段）
        # 用户 4：留出段无正例 -> 跳过
        rows = [
            (1, 101, 5.0, 100), (1, 102, 5.0, 200), (1, 103, 5.0, 300),
            (2, 105, 5.0, 110), (2, 106, 5.0, 210),
            (3, 104, 2.0, 120), (4, 101, 5.0, 130), (4, 107, 2.0, 230),
            (5, 101, 5.0, 140), (5, 108, 4.5, 240),
            (6, 105, 4.0, 150), (6, 103, 4.5, 250),
        ]
        self.run_dir, self.input_path = make_frozen_run_dir(
            self.base + "/run", [1, 2, 3, 4, 5, 6], rows)

    def tearDown(self):
        self._tmp.cleanup()

    def test_end_to_end_small_run(self):
        out = self.base + "/out"
        rc = E.main(["--input", self.input_path,
                     "--holdout-run-dir", self.run_dir,
                     "--out", out,
                     "--factors-jsonl", self.factors_path,
                     "--top-n", "3"])
        self.assertEqual(rc, 0)
        summary = json.loads(Path(out, "summary.json").read_text(encoding="utf-8"))
        # 手算：用户1 训练{101,102} 留出{103}；用户2 训练{105} 留出{106}；
        # 用户3 只有 1 条评分 -> 全进训练、无留出 -> 跳过(no_relevant)；
        # 用户4 留出{107,2.0} 无正例 -> 跳过(no_relevant)；用户5 训练{101} 留出{108}；
        # 用户6 训练{105} 留出{103}。=> scored=4, skipped_no_relevant=2
        self.assertEqual(summary["users_scored"], 4)
        self.assertEqual(summary["users_skipped_no_relevant"], 2)
        self.assertEqual(summary["users_skipped_no_train"], 0)
        self.assertGreater(summary["ndcg@20"], 0.0)

        import csv as csv_mod
        recs = {}
        with open(Path(out, "recommendations.csv"), encoding="utf-8") as handle:
            for row in csv_mod.DictReader(handle):
                recs.setdefault(int(row["user_id"]), []).append(int(row["movie_id"]))
        # 用户 1 好评 101/102（第一象限），已看排除；Top-1 是同象限的 104
        self.assertEqual(recs[1][0], 104)
        self.assertNotIn(101, recs[1])
        self.assertNotIn(102, recs[1])
        # 用户 2 好评 105 [0,1] -> Top-1 是同方向的 108
        self.assertEqual(recs[2][0], 108)
        # 用户 5 训练{101}：Top-3 = 104,102,103（按与 [1,0] 的点积降序）
        self.assertEqual(recs[5], [104, 102, 103])


if __name__ == "__main__":
    unittest.main()
