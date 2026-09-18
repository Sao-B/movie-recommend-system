"""电影搜索框的匹配规则（前台 `GET /api/movies/` 与后台 `GET /api/admin/movies`）。

一个搜索词吃四种输入（规则见 `app/movie_search.py`）：`ID318`（小写 `id318` 同样认）只按 ID
精确命中、`318` 宽匹配片名 / 简介（ID 命中排最前）、`toy` 命片名、`监狱` 命简介。

显式 ID 只有「前缀 + 数字紧挨着」两种写法，前缀大小写随意；中间加空格 / 等号 / 冒号
（`id 318`、`ID=318`）一律不算，会退回普通关键词匹配。

自带的临时 SQLite 库里造了四部片，各自占住一个坑位：

| movie_id | 片名  | 简介          | 评分人数 | 用途                          |
|----------|-------|---------------|----------|-------------------------------|
| 1        | Toy Story | 玩具总动员 | 800  | 片名命中                     |
| 318      | Shawshank Redemption, The | 希望与救赎 | 2 | ID 命中，且是全场最冷门       |
| 9999     | Blast From The Past | 故事发生在 318 号公路旁 | 9000 | 简介命中，且是全场最热门 |
| 356      | Forrest Gump | NULL      | 700      | 简介为 NULL 时不炸            |

318 与 9999 是故意错开的：搜 "318" 时两者都命中，而 9999 的人气远高于 318，
所以「ID 命中排最前」这条只有在排序真的生效时才能通过——错把它写成
「按热门排」就会被这条用例抓住。搜 `ID318` 则只剩 318 一条，用来锁定
「显式 ID 不掺片名 / 简介匹配」。
"""
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from app import create_app                                        # noqa: E402
from app.db import db                                             # noqa: E402
from app.models import Movie, User                                # noqa: E402
from app.movie_search import parse_explicit_id, parse_movie_id     # noqa: E402
from flask_jwt_extended import create_access_token                # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch          # noqa: E402

ID_HIT = 318          # 简介里的 318 属于别人，自己只有 ID 能被搜到
OVERVIEW_HIT = 9999   # 简介含 "318"
TITLE_HIT = 1

SEED = [
    dict(movie_id=TITLE_HIT, title="Toy Story", year=1995,
         overview="玩具总动员", avg_rating=4.0, rating_count=800),
    dict(movie_id=ID_HIT, title="Shawshank Redemption, The", year=1994,
         overview="监狱里的希望与救赎", avg_rating=3.0, rating_count=2),
    dict(movie_id=OVERVIEW_HIT, title="Blast From The Past", year=1999,
         overview="故事发生在 318 号公路旁", avg_rating=4.9, rating_count=9000),
    dict(movie_id=356, title="Forrest Gump", year=1994,
         overview=None, avg_rating=4.4, rating_count=700),
]


class _TestConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "movie-search-test-key-32bytes-long"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True


class ParseMovieIdTests(unittest.TestCase):
    """关键词 → movie_id 的判定（纯函数）。"""

    def test_plain_decimal_is_an_id(self):
        self.assertEqual(parse_movie_id("318"), 318)
        self.assertEqual(parse_movie_id("  318  "), 318)
        self.assertEqual(parse_movie_id("0"), 0)

    def test_non_decimal_is_not_an_id(self):
        for text in ("", "abc", "toy", "-1", "3.18", "318a", None):
            with self.subTest(text=text):
                self.assertIsNone(parse_movie_id(text))

    def test_superscript_digit_is_not_an_id(self):
        # "²".isdigit() 为真但 int("²") 会抛异常，所以判定必须用 isdecimal
        self.assertTrue("²".isdigit())
        self.assertIsNone(parse_movie_id("²"))

    def test_out_of_mysql_int_range_is_not_an_id(self):
        self.assertIsNone(parse_movie_id("99999999999"))


class ParseExplicitIdTests(unittest.TestCase):
    """`ID318` / `id318` 这类显式 ID 语法（纯函数）。"""

    def test_both_prefix_spellings_are_recognised(self):
        for text in ("id318", "ID318", "Id318", "  iD318  ", "id0"):
            with self.subTest(text=text):
                self.assertIsNotNone(parse_explicit_id(text))

    def test_separators_between_prefix_and_digits_are_not_recognised(self):
        # 前缀和数字之间多一个字符就不是 ID 语法了，会退回普通关键词
        for text in ("id 318", "id=318", "id:318", "ID：318", "id#318", "id-318", "id_318"):
            with self.subTest(text=text):
                self.assertIsNone(parse_explicit_id(text))

    def test_plain_number_is_not_explicit(self):
        # 不带 id 前缀的数字走宽匹配，不该被当成显式 ID
        self.assertIsNone(parse_explicit_id("318"))

    def test_missing_or_invalid_number(self):
        for text in ("", "id", "idabc", "id-abc", "idx318", "aid318", None):
            with self.subTest(text=text):
                self.assertIsNone(parse_explicit_id(text))

    def test_out_of_range_is_rejected(self):
        self.assertIsNone(parse_explicit_id("id99999999999"))


class MovieSearchRouteTests(unittest.TestCase):
    """两个列表接口的搜索行为都跑在临时 SQLite 库上。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="movie_search_")
        db_file = Path(cls._tmpdir.name) / "search.db"

        with sqlite_integer_pk_patch():
            _TestConfig.SQLALCHEMY_DATABASE_URI = "sqlite:///" + db_file.as_posix()
            cls.app = create_app(_TestConfig)
            with cls.app.app_context():
                db.create_all()
                for row in SEED:
                    db.session.add(Movie(**row))
                admin = User(username="search_admin", role="admin")
                admin.set_password("pw123456")
                db.session.add(admin)
                db.session.commit()
                token = create_access_token(identity=str(admin.id))

        cls.client = cls.app.test_client()
        cls.admin_headers = {"Authorization": f"Bearer {token}"}

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.app.app_context():
                db.session.remove()
                db.engine.dispose()
        finally:
            cls._tmpdir.cleanup()

    # ---------- 辅助 ----------

    def _search(self, q=None, **params):
        """打前台列表接口，返回 (movie_id 列表, total)。"""
        query = dict(params)
        if q is not None:
            query["q"] = q
        response = self.client.get("/api/movies/", query_string=query)
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        return [m["movie_id"] for m in body["movies"]], body["total"]

    def _admin_search(self, q):
        response = self.client.get(
            "/api/admin/movies", query_string={"q": q}, headers=self.admin_headers
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        return [m["movie_id"] for m in body["movies"]]

    # ---------- 四种输入 ----------

    def test_title_keyword_matches(self):
        ids, total = self._search("Toy")
        self.assertEqual(ids, [TITLE_HIT])
        self.assertEqual(total, 1)

    def test_overview_keyword_matches(self):
        ids, _ = self._search("公路")
        self.assertEqual(ids, [OVERVIEW_HIT])

    def test_id_keyword_matches_without_going_through_title(self):
        ids, _ = self._search(str(ID_HIT))
        self.assertIn(ID_HIT, ids)

    def test_null_overview_does_not_break_search(self):
        ids, _ = self._search("Gump")
        self.assertEqual(ids, [356])

    # ---------- ID318 / id318 显式 ID：只认 ID，一条不掺 ----------

    def test_explicit_id_returns_exactly_that_movie(self):
        # 两种写法：前缀大小写随意，数字紧挨着；前后空白会被 strip 掉
        for text in (f"id{ID_HIT}", f"ID{ID_HIT}", f"Id{ID_HIT}", f"  ID{ID_HIT}  "):
            with self.subTest(query=text):
                ids, total = self._search(text)
                # 9999 的简介里也含 "318"，但显式 ID 不该把它带出来
                self.assertEqual(ids, [ID_HIT])
                self.assertEqual(total, 1)

    def test_separator_forms_fall_back_to_keyword_search(self):
        # 中间带空格 / 等号 / 冒号的写法不当 ID 用，按普通关键词去搜片名与简介 → 什么也搜不到
        for text in (f"id {ID_HIT}", f"ID={ID_HIT}", f"id:{ID_HIT}"):
            with self.subTest(query=text):
                ids, total = self._search(text)
                self.assertEqual(ids, [])
                self.assertEqual(total, 0)

    def test_explicit_id_of_unknown_movie_is_empty(self):
        ids, total = self._search("id99999999")
        self.assertEqual(ids, [])
        self.assertEqual(total, 0)

    def test_explicit_prefix_without_number_falls_back_to_keyword(self):
        # "id" 后面没数字就不是 ID 语法，按普通关键词走 → 片名/简介里没有 "id" 这个词
        ids, total = self._search("id")
        self.assertEqual(ids, [])
        self.assertEqual(total, 0)

    # ---------- ID 命中排在结果最前 ----------

    def test_id_hit_ranks_first_even_when_others_are_more_popular(self):
        ids, _ = self._search(str(ID_HIT))
        # 9999 的简介里也含 "318"，且人气高得多（9000 vs 2）
        self.assertIn(OVERVIEW_HIT, ids, "简介里的数字也应该命中")
        self.assertEqual(ids[0], ID_HIT, "ID 精确命中的那条必须排第一")

    def test_id_hit_also_ranks_first_under_latest_sort(self):
        ids, _ = self._search(str(ID_HIT), sort="latest")
        self.assertEqual(ids[0], ID_HIT)

    # ---------- 热门榜门槛只在「无关键词浏览」时生效 ----------

    def test_cold_movie_is_reachable_by_keyword(self):
        # 318 只有 2 个人评过，浏览时进不了热门榜，但搜「救赎」必须能找到
        ids, total = self._search("救赎")
        self.assertEqual(ids, [ID_HIT])
        self.assertEqual(total, 1)

    def test_popular_browsing_keeps_the_rating_count_gate(self):
        ids, total = self._search(sort="popular", per_page=50)
        self.assertNotIn(ID_HIT, ids, "评分人数 2 的片不该进热门榜")
        self.assertEqual(total, len(SEED) - 1)

    # ---------- 边界：怪关键词不炸 ----------

    def test_huge_number_keyword_returns_empty(self):
        ids, total = self._search("99999999999")
        self.assertEqual(ids, [])
        self.assertEqual(total, 0)

    def test_superscript_keyword_returns_empty(self):
        ids, total = self._search("²")
        self.assertEqual(ids, [])
        self.assertEqual(total, 0)

    # ---------- 后台电影管理同规则 ----------

    def test_admin_search_accepts_id_and_overview(self):
        # 搜 "318" 同样会捎上「简介含 318」的 9999，两者都在结果里
        self.assertEqual(
            sorted(self._admin_search(str(ID_HIT))), [ID_HIT, OVERVIEW_HIT]
        )
        self.assertEqual(self._admin_search("公路"), [OVERVIEW_HIT])

    def test_admin_search_accepts_explicit_id(self):
        self.assertEqual(self._admin_search(f"id{ID_HIT}"), [ID_HIT])

    def test_admin_search_requires_admin(self):
        response = self.client.get("/api/admin/movies", query_string={"q": "318"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
