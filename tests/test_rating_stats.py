import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app.rating_stats import (  # noqa: E402
    STAR_LEVELS,
    count_ratings,
    merge_counts,
    rating_to_star_bucket,
    summarize_counts,
)


# 构建 Flask app 需要 config.yaml 与 flask/numpy 等依赖；缺失时跳过路由契约测试，
# 不让这套纯函数测试因为环境问题整体失败。
try:
    from app import create_app
    from config import Config

    _APP = create_app(Config)
    _APP_ERROR = None
except Exception as exc:  # pragma: no cover - 取决于本机环境
    _APP = None
    _APP_ERROR = exc


class StarBucketTests(unittest.TestCase):
    """半星评分如何归入 1~5 档。"""

    def test_half_steps_round_to_nearest_star(self):
        cases = {
            0.5: 1,   # 半星向上归 1 档
            1.0: 1,
            1.5: 2,
            2.0: 2,
            2.5: 3,   # 恰好半程，四舍五入向上
            3.5: 4,
            4.4: 4,   # 不满 4.5 仍算 4 档
            4.5: 5,   # 到 4.5 归 5 档
            5.0: 5,
        }
        for rating, expected in cases.items():
            with self.subTest(rating=rating):
                self.assertEqual(rating_to_star_bucket(rating), expected)

    def test_out_of_range_values_are_clamped(self):
        self.assertEqual(rating_to_star_bucket(0.0), 1)
        self.assertEqual(rating_to_star_bucket(-3), 1)
        self.assertEqual(rating_to_star_bucket(5.4), 5)
        self.assertEqual(rating_to_star_bucket(99), 5)


class CountRatingsTests(unittest.TestCase):
    """具体评分 → 五档计数。"""

    def test_empty_input_gives_all_zero_buckets(self):
        self.assertEqual(count_ratings([]), {5: 0, 4: 0, 3: 0, 2: 0, 1: 0})

    def test_counts_land_in_expected_buckets(self):
        counts = count_ratings([5, 5, 4.5, 4, 1.5, 0.5])
        # 4.5 归 5 档、1.5 归 2 档、0.5 归 1 档
        self.assertEqual(counts, {5: 3, 4: 1, 3: 0, 2: 1, 1: 1})

    def test_all_star_levels_always_present(self):
        self.assertEqual(set(count_ratings([3]).keys()), set(STAR_LEVELS))


class MergeCountsTests(unittest.TestCase):
    """MovieLens 分布 + 本站分布 的逐档合并。"""

    def test_merges_bucket_by_bucket(self):
        merged = merge_counts({5: 2, 4: 1}, {5: 1, 3: 4})
        self.assertEqual(merged, {5: 3, 4: 1, 3: 4, 2: 0, 1: 0})

    def test_none_and_empty_are_ignored(self):
        self.assertEqual(
            merge_counts(None, {}, {5: 1}),
            {5: 1, 4: 0, 3: 0, 2: 0, 1: 0},
        )

    def test_missing_stars_default_to_zero(self):
        self.assertEqual(sum(merge_counts({5: 3}).values()), 3)

    def test_three_way_merge(self):
        merged = merge_counts({5: 1}, {4: 2}, {1: 3})
        self.assertEqual(merged, {5: 1, 4: 2, 3: 0, 2: 0, 1: 3})


class SummarizeCountsTests(unittest.TestCase):
    """五档计数 → 总条数 / 平均分 / 分布。"""

    def test_uses_exact_sum_when_available(self):
        # 档位近似均值是 4.5，但真实总和 8.7（两条评分不是 5.0 与 4.0）→ 4.35
        result = summarize_counts({5: 1, 4: 1}, 2, 8.7)
        self.assertEqual(result["average"], 4.35)

    def test_falls_back_to_bucket_values_without_sum(self):
        result = summarize_counts({5: 1, 4: 1})
        self.assertEqual(result["count"], 2)
        self.assertEqual(result["average"], 4.5)

    def test_empty_is_well_formed(self):
        result = summarize_counts({})
        self.assertEqual(result["count"], 0)
        self.assertIsNone(result["average"])
        self.assertEqual(len(result["distribution"]), 5)
        for row in result["distribution"]:
            self.assertEqual(row["count"], 0)
            self.assertEqual(row["percent"], 0.0)

    def test_explicit_total_takes_precedence(self):
        # 防御：计数与 total 不一致时以 total 为准，不抛异常
        result = summarize_counts({5: 1}, 4, 20.0)
        self.assertEqual(result["count"], 4)
        self.assertEqual(result["average"], 5.0)

    def test_distribution_order_and_percent(self):
        result = summarize_counts({5: 3, 1: 1})
        self.assertEqual([r["stars"] for r in result["distribution"]], [5, 4, 3, 2, 1])
        by_star = {r["stars"]: r for r in result["distribution"]}
        self.assertEqual(by_star[5]["percent"], 75.0)
        self.assertEqual(by_star[1]["percent"], 25.0)


class CommentEndpointContractTests(unittest.TestCase):
    """影评接口的路由契约（行为本身由 test_comment_flow.py 用真实请求覆盖）。

    （原先这里有一批读 comments.py / ratings.py 源码文本的断言，已删：改个变量名就红，
    把行为真改坏了反而可能绿。）
    """

    @unittest.skipIf(_APP is None, f"无法构建 Flask app: {_APP_ERROR}")
    def test_anonymous_submit_is_rejected(self):
        client = _APP.test_client()
        response = client.post("/api/movies/318/comments", json={"content": "x"})
        self.assertEqual(response.status_code, 401)

    @unittest.skipIf(_APP is None, f"无法构建 Flask app: {_APP_ERROR}")
    def test_edit_and_delete_routes_are_registered(self):
        # 同一路径的 PUT / DELETE 是两个 endpoint、url_map 里是两条 rule，
        # 所以要把该路径上所有 rule 的方法并起来看，不能用 dict 按路径收敛。
        methods = set()
        for rule in _APP.url_map.iter_rules():
            if rule.rule == "/api/comments/<int:comment_id>":
                methods |= set(rule.methods or ())
        self.assertIn("PUT", methods, "编辑影评的 PUT 路由未注册")
        self.assertIn("DELETE", methods, "删除影评的 DELETE 路由未注册")


class RatingStatsEndpointContractTests(unittest.TestCase):
    """评分统计接口的路由注册与匿名可访问性——用真实请求，不是读源码。"""

    def test_offline_builder_reuses_the_same_bucket_rule(self):
        """离线聚合脚本必须 import 同一份档位规则，否则两处口径会漂移。

        （这条只能读源码：跨进程边界的调用关系没有更「行为化」的检查方式。）
        """
        builder = (PROJECT_ROOT / "scripts" / "build_ml_rating_stats.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("from app.rating_stats import", builder)
        self.assertIn("rating_to_star_bucket", builder)

    @unittest.skipIf(_APP is None, f"无法构建 Flask app: {_APP_ERROR}")
    def test_route_is_registered_and_public(self):
        rules = {rule.rule: rule for rule in _APP.url_map.iter_rules()}
        rule = rules.get("/api/movies/<int:movie_id>/rating-stats")
        self.assertIsNotNone(rule, "rating-stats 路由未注册")
        self.assertIn("GET", rule.methods)
        # 未登录也应能拿到统计（身份可选），因此匿名请求不能是 401
        client = _APP.test_client()
        response = client.get("/api/movies/318/rating-stats")
        self.assertNotEqual(response.status_code, 401)
        self.assertIn(response.status_code, (200, 404))
        if response.status_code == 200:
            body = response.get_json()
            self.assertEqual(len(body["distribution"]), 5)
            self.assertEqual(
                [row["stars"] for row in body["distribution"]], [5, 4, 3, 2, 1]
            )
            self.assertIn("my_rating", body)

    @unittest.skipIf(_APP is None, f"无法构建 Flask app: {_APP_ERROR}")
    def test_unknown_movie_returns_404(self):
        client = _APP.test_client()
        response = client.get("/api/movies/99999999/rating-stats")
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
