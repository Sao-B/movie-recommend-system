# -*- coding: utf-8 -*-
"""heldout_fusion_eval 的契约测试。

覆盖五件事：
1. 变体打分公式正确性（V0=Σ、V1=Σ/n、V2=Σ/(n+k)，手算小例子）；
2. 缺失信号重归一化：候选缺 ALS 路时 fused = 其余信号加权和/其余权重和，
   绝不按 0 分计入；
3. 类型代理：交集比计算、无类型标注 = 缺失、用户无好评类型 = 整路缺失；
4. tune/test 分离确定性：同用户两次运行同半、两半互斥且并集=全体；
5. 端到端小样本：4 个用户跑通 main()，summary.json 结构完整、V0 与手算一致。
"""

import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_itemcf_online import write_model  # noqa: E402
from experiments.itemcf.heldout_eval import (  # noqa: E402
    RatingSession, user_list_sha256, write_json_atomic,
)
from experiments.itemcf import heldout_fusion_eval as F  # noqa: E402
from app.itemcf_service import ItemCFGraph  # noqa: E402


def write_ratings(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        handle.write("userId,movieId,rating,timestamp\n")
        for user, movie, rating, ts in rows:
            handle.write("%d,%d,%s,%d\n" % (user, movie, rating, ts))


def make_frozen_run_dir(directory, eval_users, ratings_rows, test_frac=0.2):
    """与 tests/test_heldout_als_eval.make_frozen_run_dir 同模式的最小冻结协议目录。"""
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


def make_record(u_ids, u_sum, u_cnt, u_als, u_genre, relevant):
    """手工构造 build_user_record 的输出形状（跳过候选构建）。"""
    return {
        "user_id": 0,
        "relevant": set(relevant),
        "u_ids": np.asarray(u_ids, dtype=np.int64),
        "u_sum": np.asarray(u_sum, dtype=np.float64),
        "u_cnt": np.asarray(u_cnt, dtype=np.float64),
        "u_als": np.asarray(u_als, dtype=np.float64),
        "u_genre": np.asarray(u_genre, dtype=np.float64),
        "seen_count": 0,
        "als_candidates": 0,
    }


def make_params(k2=0, w3=(0.5, 0.5, 0.0), w4=(0.4, 0.4, 0.2)):
    w3 = tuple(w3) + (0.0,) * (3 - len(w3))   # 兼容 2 维写法（w_genre 补 0）
    return {
        "V2_smoothed": {"k": k2},
        "V3_itemcf_als": {"weights": list(w3)},
        "V4_itemcf_als_genre": {"k": 0, "weights": list(w4)},
    }


def write_genres_csv(path, mapping):
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["movie_id", "genres"])
        for movie_id, values in mapping:
            writer.writerow([movie_id, "|".join(values)])


class TestVariantFormulas(unittest.TestCase):
    """V0=Σ、V1=Σ/n、V2=Σ/(n+k)：手算小例子，含 k 改变排序的用例。"""

    def test_v0_v1_v2_hand_computed(self):
        # 候选 200：sum=1.2, count=2；候选 300：sum=1.0, count=1
        record = make_record([200, 300], [1.2, 1.0], [2, 1],
                             [np.nan, np.nan], [np.nan, np.nan], {200})
        # V0：裸求和 1.2 > 1.0 -> [200, 300]
        # V1：mean 0.6 < 1.0 -> [300, 200]
        # V2 k=2：1.2/4=0.3 < 1.0/3≈0.333 -> [300, 200]
        # V2 k=5：1.2/7≈0.171 > 1.0/6≈0.167 -> [200, 300]
        recs = F.score_variants_for_user(record, make_params(k2=2), 20)
        self.assertEqual(recs["V0_raw_sum"], [200, 300])
        self.assertEqual(recs["V1_mean"], [300, 200])
        self.assertEqual(recs["V2_smoothed"], [300, 200])
        recs = F.score_variants_for_user(record, make_params(k2=5), 20)
        self.assertEqual(recs["V2_smoothed"], [200, 300])

    def test_v0_pool_excludes_no_evidence_union_members(self):
        # 400 无 ItemCF 证据（并集成员、只来自 ALS）：V0/V1/V2 候选=R1 证据集，
        # 400 不应出现；500 是唯一证据候选
        record = make_record([400, 500], [0.0, 2.0], [0, 1],
                             [0.8, np.nan], [np.nan, np.nan], {400})
        recs = F.score_variants_for_user(record, make_params(), 20)
        self.assertEqual(recs["V0_raw_sum"], [500])
        self.assertEqual(recs["V1_mean"], [500])
        self.assertEqual(recs["V2_smoothed"], [500])


class TestMissingSignalRenormalization(unittest.TestCase):
    """缺某路信号时 fused = Σ(可用·权重)/Σ(可用权重)，绝不按 0 分计入。"""

    def test_missing_als_signal_exits_weighting(self):
        # 400：只有 ALS（无 itemcf 证据）；500：只有 itemcf
        # itemcf 归一：500 -> 1.0；als 归一：0.8/0.4 -> 400 -> 1.0, 500 -> 0.5
        # fused(400) = (0.5*1.0)/0.5 = 1.0（若按 0 分计入会是 0.5，排到 500 后）
        # fused(500) = (0.5*1.0 + 0.5*0.5)/1.0 = 0.75
        record = make_record([400, 500], [0.0, 2.0], [0, 1],
                             [0.8, 0.4], [np.nan, np.nan], {400})
        signals, availability = F.variant_signals(record, with_genre=False)
        fused = F.fused_matrix(
            signals, availability, np.array([[0.5], [0.5]]))
        self.assertAlmostEqual(float(fused[0, 0]), 1.0, places=12)
        self.assertAlmostEqual(float(fused[1, 0]), 0.75, places=12)
        recs = F.score_variants_for_user(
            record, make_params(w3=(0.5, 0.5)), 20)
        self.assertEqual(recs["V3_itemcf_als"], [400, 500])

    def test_all_signals_missing_ranks_last_not_zero(self):
        # 候选 900 三路全缺（既无证据也不在因子表）-> -inf，绝不污染 Top
        record = make_record([600, 900], [1.0, 0.0], [1, 0],
                             [0.4, np.nan], [np.nan, np.nan], {600})
        signals, availability = F.variant_signals(record, with_genre=True)
        fused = F.fused_matrix(
            signals, availability,
            np.array([[0.4], [0.4], [0.2]]))
        self.assertTrue(np.isneginf(float(fused[1, 0])))
        recs = F.score_variants_for_user(
            record, make_params(w4=(0.4, 0.4, 0.2)), 20)
        self.assertEqual(recs["V4_itemcf_als_genre"][0], 600)


class TestGenreProxy(unittest.TestCase):
    """类型代理：交集比、无标注 = 缺失、用户无好评类型 = 整路缺失。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # 图：101 -> 201(0.5)；102 -> 202(0.3), 203(0.2)
        self.model_dir = write_model(
            self._tmp.name + "/model", item_ids=[101, 102, 201, 202, 203],
            edges=[(101, 201, 0.5), (102, 202, 0.3), (102, 203, 0.2)],
            n_like=[1, 1, 1, 1, 1], n_all=[1, 1, 1, 1, 1])
        self.graph = ItemCFGraph(str(self.model_dir))
        self.genre_map = F.load_genres_csv(self._write_genres())
        self.factor_pos = {}
        self.movie_ids = np.zeros(0, dtype=np.int64)
        self.matrix = np.zeros((0, 2), dtype=np.float64)

    def tearDown(self):
        # ItemCFGraph 持有 mmap 句柄，必须先释放才能在 Windows 上删临时目录
        self.graph = None
        import gc
        gc.collect()
        self._tmp.cleanup()

    def _write_genres(self):
        path = self._tmp.name + "/genres.csv"
        write_genres_csv(path, [
            (101, ["Action", "SciFi"]), (102, ["Action"]),
            (201, ["Action"]), (202, ["Drama"]), (203, []),
        ])
        return path

    def test_genre_match_ratio_and_missing_annotation(self):
        # 用户好评 101(Action,SciFi) + 102(Action) -> 画像 {Action, SciFi}
        # 候选 201(Action)=1/2；202(Drama)=0.0（有标注、匹配 0）；203(空)=缺失
        segments = {"rated": [(101, 5.0), (102, 4.0)],
                    "positive": [101, 102]}
        record = F.build_user_record(
            1, segments, {201}, self.graph, self.genre_map, self.factor_pos,
            self.movie_ids, self.matrix, 0.1)
        np.testing.assert_array_equal(record["u_ids"], [201, 202, 203])
        np.testing.assert_allclose(record["u_genre"], [0.5, 0.0, np.nan])
        self.assertTrue(np.isnan(record["u_als"]).all())
        np.testing.assert_allclose(record["u_sum"], [0.5, 0.3, 0.2])
        np.testing.assert_allclose(record["u_cnt"], [1.0, 1.0, 1.0])

    def test_user_without_positive_genres_has_whole_signal_missing(self):
        # 好评为空（训练段只有低分），但 ALS 因子能给出候选 301/302：
        # 用户无好评类型 -> 整路类型信号缺失（NaN），而不是 0 分
        write_genres_csv(self._tmp.name + "/genres2.csv", [
            (301, ["Action"]), (302, ["Drama"]),
        ])
        genres = F.load_genres_csv(self._tmp.name + "/genres2.csv")
        factor_pos = {105: 0, 301: 1, 302: 2}
        movie_ids = np.asarray([105, 301, 302], dtype=np.int64)
        matrix = np.asarray([[1.0, 0.0], [0.9, 0.1], [0.8, 0.2]])
        segments = {"rated": [(105, 2.0)], "positive": []}
        record = F.build_user_record(
            7, segments, {301}, self.graph, genres, factor_pos,
            movie_ids, matrix, 0.1)
        self.assertTrue(set(record["u_ids"].tolist()) >= {301, 302})
        self.assertTrue(np.isnan(record["u_genre"]).all())

    def test_genre_missing_on_one_candidate_renormalizes(self):
        # 600：itemcf+als、类型缺失；601：三路齐备
        # itemcf 归一：600 -> 1.0, 601 -> 0.5；als 归一：600 -> 0.2, 601 -> 1.0
        # genre 归一：601 -> 1.0（600 缺失）
        # fused(600) = (0.2*1.0 + 0.2*0.2)/0.4 = 0.6（按 0 分计入会是 0.24）
        # fused(601) = (0.2*0.5 + 0.2*1.0 + 0.6*1.0)/1.0 = 0.9
        record = make_record([600, 601], [1.0, 0.5], [1, 1],
                             [0.2, 1.0], [np.nan, 0.3], {600})
        signals, availability = F.variant_signals(record, with_genre=True)
        fused = F.fused_matrix(
            signals, availability, np.array([[0.2], [0.2], [0.6]]))
        self.assertAlmostEqual(float(fused[0, 0]), 0.6, places=12)
        self.assertAlmostEqual(float(fused[1, 0]), 0.9, places=12)
        recs = F.score_variants_for_user(
            record, make_params(w4=(0.2, 0.2, 0.6)), 20)
        self.assertEqual(recs["V4_itemcf_als_genre"], [601, 600])


class TestTuneTestSplit(unittest.TestCase):
    """tune/test 分离：确定性、互斥、并集=全体、simplex 网格规模。"""

    def test_split_is_deterministic_and_exhaustive(self):
        users = list(range(1, 201))
        first = [F.tune_test_half(u) for u in users]
        second = [F.tune_test_half(u) for u in users]
        self.assertEqual(first, second)
        tune = {u for u, h in zip(users, first) if h == "tune"}
        test = {u for u, h in zip(users, first) if h == "test"}
        self.assertEqual(tune & test, set())
        self.assertEqual(tune | test, set(users))
        self.assertTrue(tune)
        self.assertTrue(test)

    def test_simplex_grid_shape_and_sum(self):
        combos2 = F.simplex_grid(2)
        combos3 = F.simplex_grid(3)
        self.assertEqual(len(combos2), 11)
        self.assertEqual(len(combos3), 66)
        for combo in combos3:
            self.assertAlmostEqual(sum(combo), 1.0, places=12)
        self.assertIn((0.5, 0.5), combos2)
        self.assertIn((0.4, 0.4, 0.2), combos3)


class EndToEndSmokeTests(unittest.TestCase):
    """4 个评测用户跑通 main()；summary.json 结构完整、V0 与手算一致。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = self._tmp.name
        self.model_dir = write_model(
            self.base + "/model", item_ids=list(range(101, 109)),
            edges=[(101, 103, 0.9), (101, 104, 0.5), (102, 103, 0.8),
                   (105, 106, 0.7), (105, 107, 0.4), (106, 108, 0.6),
                   (107, 108, 0.3)],
            n_like=[1] * 8, n_all=[1] * 8)
        vectors = {101: [1.0, 0.0], 102: [0.9, 0.1], 103: [0.8, 0.2],
                   104: [0.95, 0.05], 105: [0.0, 1.0], 106: [0.1, 0.9],
                   107: [0.2, 0.8], 108: [0.05, 0.95]}
        self.factors_path = self.base + "/factors.jsonl"
        with open(self.factors_path, "w", encoding="utf-8") as handle:
            for movie_id in sorted(vectors):
                handle.write(json.dumps(
                    {"movie_id": movie_id, "factors": vectors[movie_id]}) + "\n")
        self.genres_path = self.base + "/genres.csv"
        write_genres_csv(self.genres_path, [
            (101, ["Action"]), (102, ["Action"]), (103, ["Action", "SciFi"]),
            (104, ["Drama"]), (105, ["SciFi"]), (106, ["SciFi"]),
            (107, ["Comedy"]), (108, ["Comedy"]),
        ])
        # 用户1：训练好评{101,102}，留出{103}；用户2：训练{105}，留出{106}；
        # 用户3：训练{101(4.0)}，留出{104}；用户4：训练只有低分{105,2.0}，留出{108}
        rows = [
            (1, 101, 5.0, 100), (1, 102, 5.0, 200), (1, 103, 5.0, 300),
            (2, 105, 5.0, 110), (2, 106, 5.0, 210),
            (3, 101, 4.0, 120), (3, 104, 4.5, 320),
            (4, 105, 2.0, 130), (4, 108, 5.0, 230),
        ]
        self.run_dir, self.input_path = make_frozen_run_dir(
            self.base + "/run", [1, 2, 3, 4], rows)

    def tearDown(self):
        self._tmp.cleanup()

    def test_end_to_end_small_run(self):
        out = self.base + "/out"
        rc = F.main([
            "--input", self.input_path,
            "--holdout-run-dir", self.run_dir,
            "--als-factors-jsonl", self.factors_path,
            "--itemcf-model-dir", str(self.model_dir),
            "--movies-genres-csv", self.genres_path,
            "--out", out,
            "--top-n", "3",
        ])
        self.assertEqual(rc, 0)
        summary = json.loads(
            (Path(out) / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary["users_scored"], 4)
        self.assertEqual(summary["users_skipped_no_relevant"], 0)
        self.assertEqual(summary["tune_users"] + summary["test_users"], 4)

        # 五个变体齐全，各有 tune/test/all 三段与参数块
        for name in F.VARIANT_NAMES:
            entry = summary["variants"][name]
            for half in ("tune", "test", "all"):
                self.assertIn(half, entry)
                self.assertIn("ndcg@20", entry[half])
                self.assertIn("recall@20", entry[half])
            self.assertIn("params", entry)
        self.assertIn("k", summary["variants"]["V2_smoothed"]["params"])
        self.assertIn("weights",
                      summary["variants"]["V4_itemcf_als_genre"]["params"])

        # V0 手算（候选=R1 证据集，只返回正分条目）：
        #   u1: R1={103:1.7, 104:0.5} -> [103,104]，命中 rank1 -> ndcg 1.0
        #   u2: R1={106:0.7, 107:0.4} -> [106,107]，命中 rank1 -> ndcg 1.0
        #   u3: R1={103:0.9, 104:0.5} -> [103,104]，命中 rank2 -> 1/log2(3)
        #   u4: R1 为空（训练段只有低分）-> []，ndcg 0
        #   V0 recall = 3/4
        expected = (1.0 + 1.0 + 1.0 / np.log2(3.0) + 0.0) / 4.0
        self.assertAlmostEqual(
            summary["variants"]["V0_raw_sum"]["all"]["ndcg@20"],
            float(expected), places=9)
        self.assertAlmostEqual(
            summary["variants"]["V0_raw_sum"]["all"]["recall@20"], 0.75,
            places=9)
        self.assertIn(summary["v0_reproduction"]["verdict"],
                      ("reproduced", "not_reproduced"))

        # per_user.csv / recommendations.csv / tuning.json 结构
        with open(Path(out, "per_user.csv"), encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 4)
        self.assertIn("V0_raw_sum_ndcg@20", rows[0])
        self.assertIn("half", rows[0])
        with open(Path(out, "recommendations.csv"), encoding="utf-8") as handle:
            recs = list(csv.DictReader(handle))
        variants_seen = {row["variant"] for row in recs}
        expected = set(F.VARIANT_NAMES) | {"V5_coverage_oriented"}
        self.assertEqual(variants_seen, expected)
        # V5（覆盖率导向选择）的调参记录与覆盖层报告必须存在
        tuning = json.loads(
            (Path(out) / "tuning.json").read_text(encoding="utf-8"))
        self.assertIn("coverage_oriented", tuning)
        self.assertIn("picked", tuning["coverage_oriented"])
        summary = json.loads(
            (Path(out) / "summary.json").read_text(encoding="utf-8"))
        self.assertIn("coverage_test_half", summary)
        self.assertIn("V5_coverage_oriented", summary["coverage_test_half"])
        self.assertEqual(summary["als_min_support"], 0)


if __name__ == "__main__":
    unittest.main()
