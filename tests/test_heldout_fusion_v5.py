"""V6/V7/V8 消融变体测试（EDA 2026-10-10 叠加层）。

- build_user_record 的 V5 字段（u_label / u_als_pen / u_pop）逐项算术验证；
- V6（标签路换频率x均分）、V7（+ALS 负反馈罚项）、V8（+热门降权）的排序效果；
- 旧形状记录（无 V5 字段）安全跳过，不影响 V0~V4。
"""
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from tests.test_itemcf_online import write_model            # noqa: E402
from tests.test_heldout_fusion_eval import make_record, make_params  # noqa: E402
from app.itemcf_service import ItemCFGraph                  # noqa: E402
from experiments.itemcf import heldout_fusion_eval as F     # noqa: E402


class BuildRecordV5FieldTests(unittest.TestCase):
    """build_user_record 的 V5 字段算术验证（合成图 + 合成因子）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # 图：101 -> 102(0.9)
        self.model_dir = write_model(
            self._tmp.name + "/model", item_ids=[101, 102, 104, 105],
            edges=[(101, 102, 0.9)],
            n_like=[1, 1, 1, 1, 1], n_all=[1, 1, 1, 1, 1])
        self.graph = ItemCFGraph(str(self.model_dir))
        self.genre_map = {
            101: frozenset(["SciFi"]), 105: frozenset(["SciFi"]),
            102: frozenset(["SciFi"]), 104: frozenset(["Action"]),
        }
        # 因子：105 与 102 同向（罚项全额命中），104 近正交
        self.factor_pos = {101: 0, 105: 1, 102: 2, 104: 3}
        self.movie_ids = np.asarray([101, 105, 102, 104], dtype=np.int64)
        self.matrix = np.asarray([[1.0, 0.0], [1.0, 0.0],
                                  [1.0, 0.0], [0.97, 0.242]])
        matc = self.matrix - self.matrix.mean(axis=0, keepdims=True)
        nrm = np.linalg.norm(matc, axis=1, keepdims=True)
        self.matcn = matc / np.maximum(nrm, 1e-12)
        self.pop_counts = {101: 5000, 105: 300, 102: 5000, 104: 20}

    def tearDown(self):
        self.graph = None
        import gc
        gc.collect()
        self._tmp.cleanup()

    def test_v5_fields_arithmetic(self):
        # 评分（时间序）：101 好评 5.0 -> 105 差评 1.0（w=-1.2）
        segments = {"rated": [(101, 5.0), (105, 1.0)], "positive": [101]}
        record = F.build_user_record(
            1, segments, {201}, self.graph, self.genre_map, self.factor_pos,
            self.movie_ids, self.matrix, 0.1, pop_counts=self.pop_counts,
            matcn=self.matcn)
        u_ids = record["u_ids"].tolist()
        self.assertIn(102, u_ids)

        # u_label：SciFi 频率 2 -> fw=0.301；近期均分 (5+1)/2=3 -> 信号 0
        at102 = u_ids.index(102)
        self.assertAlmostEqual(record["u_label"][at102], 0.0, places=9)
        self.assertTrue(np.isnan(record["u_label"][u_ids.index(104)]))

        # u_als_pen：V7 语义 = 正评分折入（101,5.0 -> vec_x=5/1.1）+ 罚项
        # 102 与 105 中心化余弦=1 -> 罚 0.2*1.2=0.24；104 近正交 -> 0
        als_by_id = dict(zip(u_ids, record["u_als"].tolist()))
        pen_by_id = dict(zip(u_ids, record["u_als_pen"].tolist()))
        expected_102 = 5.0 / 1.1 - 0.2 * 1.2     # 正评分折入点积 - 罚项
        self.assertAlmostEqual(pen_by_id[102], expected_102, places=9)
        self.assertAlmostEqual(pen_by_id[104], 0.97 * 5.0 / 1.1, places=9)
        # V4/V6 用的 u_als 仍是全评分折入（负分进目标），两者必须不同
        self.assertNotAlmostEqual(pen_by_id[102], als_by_id[102], places=3)
        self.assertNotIn(105, u_ids)   # 105 已看，不在候选中

        # u_pop：训练段全量评分数
        pop_by_id = dict(zip(u_ids, record["u_pop"].tolist()))
        self.assertEqual(pop_by_id[102], 5000)
        self.assertEqual(pop_by_id[104], 20)


class V5VariantOrderTests(unittest.TestCase):
    """手工构造记录，验证 V6/V7/V8 的排序效果与逐级叠加关系。"""

    def _record(self, u_als_pen):
        record = make_record(
            np.asarray([600, 601], dtype=np.int64),
            np.asarray([1.0, 0.5]), np.asarray([1.0, 1.0]),
            np.asarray([1.0, 0.96]), np.asarray([np.nan, np.nan]),
            {600})
        record["u_label"] = np.asarray([0.0, 1.0])
        record["u_als_pen"] = np.asarray(u_als_pen, dtype=np.float64)
        record["u_pop"] = np.asarray([5000.0, 100.0])
        return record

    def test_v7_penalty_flips_order(self):
        # V6：600 三路里 itemcf+als 双第一（als 1.0 vs 0.96）
        # V7：罚项把 600 的 als 压到 0.5（601 几乎不罚）-> 601 反超
        recs = F.score_variants_for_user(
            self._record([0.5, 0.96]), make_params(), 20)
        self.assertEqual(recs["V6_label_freq"], [600, 601])
        self.assertEqual(recs["V7_als_neg"], [601, 600])

    def test_v8_pop_rerank_flips_order(self):
        # V7 无罚项差异（pen=als）时 600 第一；V8 把热门的 600 压下去
        recs = F.score_variants_for_user(
            self._record([1.0, 0.96]), make_params(), 20)
        self.assertEqual(recs["V6_label_freq"], [600, 601])
        self.assertEqual(recs["V7_als_neg"], [600, 601])
        self.assertEqual(recs["V8_pop_rerank"], [601, 600])

    def test_v8_only_penalizes_above_median(self):
        # 601 是低热度候选（pop < 中位）-> scale 钳 1，分数不被放大
        record = self._record([1.0, 0.96])
        weights = np.asarray(F.V5_FIXED_WEIGHTS, dtype=np.float64).reshape(3, 1)
        signals, avail = F.variant_signals(
            record, with_genre=True, als_key='u_als_pen', genre_key='u_label')
        fused7 = F.fused_matrix(signals, avail, weights)
        u_pop = record["u_pop"]
        pos = u_pop[u_pop > 0]
        med = float(np.median(pos))
        scale = np.maximum(u_pop / max(med, 1.0), 1.0)
        fused8 = fused7[:, 0] / np.power(scale, F.V5_POP_ALPHA)
        self.assertEqual(scale[1], 1.0)               # 低热度不放大
        self.assertAlmostEqual(fused8[1], fused7[1, 0], places=12)

    def test_legacy_record_skips_v5_variants(self):
        """旧形状记录（无 u_label）不产出 V6/V7/V8，V0~V4 不受影响。"""
        record = make_record(
            np.asarray([600, 601], dtype=np.int64),
            np.asarray([1.0, 0.5]), np.asarray([1.0, 1.0]),
            np.asarray([1.0, 0.5]), np.asarray([np.nan, 0.3]), {600})
        recs = F.score_variants_for_user(record, make_params(), 20)
        self.assertNotIn("V6_label_freq", recs)
        self.assertNotIn("V7_als_neg", recs)
        self.assertNotIn("V8_pop_rerank", recs)
        self.assertIn("V4_itemcf_als_genre", recs)


if __name__ == "__main__":
    unittest.main()
