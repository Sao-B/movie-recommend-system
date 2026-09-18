"""评分与影评的端到端行为（自带临时 SQLite 库，不碰线上 MySQL）。

覆盖的链路：
- 影评：正文必填、**没有评分的影评不许发表**、一人一片一条（重复提交即更新）、
  列表附带作者星级、删除权限、编辑（记 edited_at）、可见性（作者没评分＝仅自己可见）；
- 评分：新增、覆盖（改分）、删除（``DELETE /api/ratings/<movie_id>``，个人中心用，
  以及详情页「删除 → 保留影评」那条），以及删掉评分后详情页回显（``my_rating``）归零。

详情页把评分与影评合成**一个动作、缺一不可**：按钮要两边都填齐才点得动，
提交顺序固定为「先 POST /api/ratings/ 再 POST /api/movies/<id>/comments」，
后端在 ``create_comment`` 里也要求作者此刻有评分（否则 400），保证成对出现。
因此「只评分不写影评」在库里是合法的（评分行一条，``empty_content_is_rejected`` 之外
不会再产生影评行），但反过来的「只写影评不评分」只能由下面这条路径产生：
作者在影评卡片上选「保留影评草稿」，评分行被删掉、影评退成「仅自己可见」——
可见性由「作者此刻有没有评分」推导，不存第二个状态位（见 ``_serialize``）。

删除方向（详情页只有影评卡片上的一个「删除」入口，置顶那条与时间线里自己那条同源）：
- 「保留影评草稿」＝``DELETE /api/ratings/<movie_id>``，只删评分，影评留在原地；
- 「不保留」＝``DELETE /api/comments/<id>?with_rating=1``，两行在同一次 commit 里删掉；
- 本来就没评分时＝``DELETE /api/comments/<id>``（不带参数），只删影评。

评分只接受 0.5 ~ 5.0：详情页的评分控件点不出 0 分，早先那套「打 0 分＝撤销评分」
的机制已按要求撤掉；星星旁也不再单独给「删除」，撤销评分一律走上面那条确认框。

环境适配说明：SQLite 只有 ``INTEGER PRIMARY KEY`` 才是 rowid 自增别名，模型的
``BigInteger`` 主键会报 ``NOT NULL constraint failed``。建表前用共享的
``tests/sqlite_support.py`` 补丁临时换成 ``Integer``，不改生产模型。
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
from app.models import Movie, MovieComment, User, UserRating      # noqa: E402
from flask_jwt_extended import create_access_token                # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch          # noqa: E402

TITLE = "Shawshank Redemption, The"
MOVIE_ID = 318
UNKNOWN_MOVIE_ID = 99999999
OTHER_MOVIE_ID = 1


class _TestConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "rating-comment-flow-test-key-32bytes"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True


class RatingCommentFlowTests(unittest.TestCase):
    """整条链路跑在临时 SQLite 库上。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="rating_comment_flow_")
        db_file = Path(cls._tmpdir.name) / "flow.db"

        with sqlite_integer_pk_patch():
            _TestConfig.SQLALCHEMY_DATABASE_URI = "sqlite:///" + db_file.as_posix()
            cls.app = create_app(_TestConfig)
            with cls.app.app_context():
                db.create_all()

        cls.client = cls.app.test_client()

        with cls.app.app_context():
            db.session.add(Movie(movie_id=MOVIE_ID, title=TITLE, year=1994))
            db.session.add(Movie(movie_id=OTHER_MOVIE_ID, title="Toy Story", year=1995))
            user = User(username="flow_user")
            user.set_password("pw123456")
            other = User(username="other_user")
            other.set_password("pw123456")
            db.session.add(user)
            db.session.add(other)
            db.session.commit()
            cls.user_id = user.id
            cls.other_user_id = other.id
            token = create_access_token(identity=str(cls.user_id))
            other_token = create_access_token(identity=str(cls.other_user_id))

        cls.headers = {"Authorization": f"Bearer {token}"}
        cls.other_headers = {"Authorization": f"Bearer {other_token}"}
        cls.comments_url = f"/api/movies/{MOVIE_ID}/comments"
        cls.stats_url = f"/api/movies/{MOVIE_ID}/rating-stats"
        cls.rating_url = "/api/ratings/"

    @classmethod
    def tearDownClass(cls):
        try:
            with cls.app.app_context():
                db.session.remove()
                db.engine.dispose()
        finally:
            cls._tmpdir.cleanup()

    def setUp(self):
        """每条用例从「没评分、没影评」的干净状态出发。"""
        with self.app.app_context():
            db.session.query(MovieComment).delete()
            db.session.query(UserRating).delete()
            db.session.commit()

    # ---------- 辅助 ----------

    def _rate(self, rating, headers=None, movie_id=MOVIE_ID):
        return self.client.post(
            self.rating_url,
            json={"movie_id": movie_id, "rating": rating},
            headers=self.headers if headers is None else headers,
        )

    def _unrate(self, headers=None, movie_id=MOVIE_ID):
        """只删评分（个人中心「我的评分」，以及详情页「删除 → 保留影评草稿」都走它）。

        注意：「不保留」那条路走 ``_delete_comment(..., with_rating=True)``，
        它在同一次 commit 里把影评也删掉。
        """
        return self.client.delete(
            f"/api/ratings/{movie_id}",
            headers=self.headers if headers is None else headers,
        )

    def _delete_comment(self, comment_id, with_rating=False, headers=None):
        """删影评；``with_rating=True`` 时后端连评分一起删（「不保留」走这条）。"""
        url = f"/api/comments/{comment_id}"
        if with_rating:
            url += "?with_rating=1"
        return self.client.delete(
            url, headers=self.headers if headers is None else headers
        )

    def _comment(self, content, headers=None):
        return self.client.post(
            self.comments_url, json={"content": content},
            headers=self.headers if headers is None else headers,
        )

    def _publish(self, content, rating=4.0):
        """按详情页的顺序成对发表：先评分、再影评。"""
        self._rate(rating)
        return self._comment(content)

    def _comments(self):
        with self.app.app_context():
            return [(row.id, row.content) for row in
                    MovieComment.query.order_by(MovieComment.id).all()]

    def _rating_rows(self):
        with self.app.app_context():
            return sorted((row.movie_id, row.rating) for row in UserRating.query.all())

    def _comment_row(self, comment_id):
        with self.app.app_context():
            return db.session.get(MovieComment, comment_id)

    def _list(self, headers=None):
        return self.client.get(
            self.comments_url, headers=self.headers if headers is None else headers
        ).get_json()

    def _stats(self, headers=None):
        return self.client.get(
            self.stats_url, headers=self.headers if headers is None else headers
        ).get_json()

    # ---------- 成对发表：没有评分就不许发影评 ----------

    def test_comment_without_a_rating_is_rejected(self):
        """「缺一不可」的服务端兜底：没给这部电影打分，影评发不出去。"""
        response = self._comment("只写影评不打分")
        self.assertEqual(response.status_code, 400)
        self.assertIn("评分", response.get_json()["msg"])
        self.assertEqual(self._comments(), [])
        self.assertEqual(self._rating_rows(), [])

    def test_comment_is_accepted_right_after_the_rating(self):
        """先评分、再发表影评——这就是详情页那次「发表」的两步。"""
        self.assertEqual(self._rate(4.0).status_code, 201)
        self.assertEqual(self._comment("成对发表").status_code, 201)
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 4.0)])
        self.assertEqual(len(self._comments()), 1)

    def test_rating_alone_is_still_a_legal_state(self):
        """只评分不写影评在库里是合法的（详情页按钮会拦住，但接口本身没有影评行）。"""
        self.assertEqual(self._rate(4.0).status_code, 201)
        self.assertEqual(self._comments(), [])

    # ---------- 影评：发表 ----------

    def test_empty_content_is_rejected(self):
        self.assertEqual(self._comment("").status_code, 400)
        self.assertEqual(self._comment("   \n  ").status_code, 400)
        self.assertEqual(self._comments(), [])

    def test_explicit_content_is_kept_verbatim(self):
        self.assertEqual(self._publish("  很好看  ").status_code, 201)
        self.assertEqual(len(self._comments()), 1)
        self.assertEqual(self._comments()[0][1], "很好看")

    def test_new_comment_has_no_edited_at(self):
        """刚发表的影评不该显示「上次修改于」——edited_at 必须为空。"""
        comment_id = self._publish("初版").get_json()["id"]
        self.assertIsNone(self._comment_row(comment_id).edited_at)

    def test_repeat_submit_updates_instead_of_appending(self):
        first_id = self._publish("第一版").get_json()["id"]
        response = self._comment("第二版")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._comments(), [(first_id, "第二版")])
        # 走「再次发表」这条路同样算修改了正文
        self.assertIsNotNone(self._comment_row(first_id).edited_at)

    def test_unknown_movie_returns_404(self):
        response = self.client.post(
            f"/api/movies/{UNKNOWN_MOVIE_ID}/comments",
            json={"content": "有正文"}, headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_anonymous_submit_is_rejected(self):
        self.assertEqual(self._comment("有正文", headers={}).status_code, 401)

    # ---------- 影评：列表 / 星级 / 可见性 ----------

    def test_list_returns_comment_with_author_rating(self):
        self._publish("好看", rating=4.0)
        body = self._list(headers={})
        self.assertEqual(body["total"], 1)
        self.assertEqual(body["comments"][0]["user_rating"], 4.0)
        self.assertEqual(body["comments"][0]["username"], "flow_user")
        self.assertTrue(body["comments"][0]["visible"])

    def test_list_carries_author_avatar_and_display_name(self):
        """卡片要带头像：作者没设头像时 avatar_url 为 None，前端回落成显示名首字符。"""
        self._publish("好看")
        card = self._list(headers={})["comments"][0]
        self.assertIn("avatar_url", card)
        self.assertIsNone(card["avatar_url"])
        self.assertEqual(card["display_name"], "flow_user")

        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.avatar_url = "/api/profile/avatar/avatar_1_deadbeef.png"
            db.session.commit()

        card = self._list(headers={})["comments"][0]
        self.assertEqual(card["avatar_url"], "/api/profile/avatar/avatar_1_deadbeef.png")

    def test_my_comment_is_null_without_a_comment_and_for_guests(self):
        self.assertIsNone(self._list()["my_comment"])
        self._publish("好看")
        self.assertIsNone(self._list(headers={})["my_comment"])
        self.assertIsNotNone(self._list()["my_comment"])

    def test_pinned_comment_also_stays_in_the_timeline(self):
        """自己的影评既置顶（my_comment）也留在时间线里。"""
        comment_id = self._publish("好看").get_json()["id"]
        body = self._list()
        self.assertEqual(body["my_comment"]["id"], comment_id)
        self.assertIn(comment_id, [c["id"] for c in body["comments"]])

    def test_owner_can_delete_comment_and_others_cannot(self):
        comment_id = self._publish("好看").get_json()["id"]
        self.assertEqual(
            self.client.delete(f"/api/comments/{comment_id}", headers={}).status_code, 401
        )
        self.assertEqual(
            self.client.delete(f"/api/comments/{comment_id}", headers=self.headers).status_code,
            200,
        )
        self.assertEqual(self._list()["total"], 0)

    # ---------- 影评：编辑 ----------

    def test_author_can_edit_own_comment(self):
        comment_id = self._publish("初版").get_json()["id"]
        response = self.client.put(
            f"/api/comments/{comment_id}", json={"content": " 改过的 "},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 200)
        row = self._comment_row(comment_id)
        self.assertEqual(row.content, "改过的")
        self.assertIsNotNone(row.edited_at)
        self.assertIsNotNone(response.get_json()["edited_at"])

    def test_edit_keeps_created_at(self):
        """编辑只动正文与 edited_at，发表时间不能被改。"""
        comment_id = self._publish("初版").get_json()["id"]
        created_before = self._comment_row(comment_id).created_at
        self.client.put(
            f"/api/comments/{comment_id}", json={"content": "改过"},
            headers=self.headers,
        )
        self.assertEqual(self._comment_row(comment_id).created_at, created_before)

    def test_edit_rejects_empty_content(self):
        comment_id = self._publish("初版").get_json()["id"]
        response = self.client.put(
            f"/api/comments/{comment_id}", json={"content": "   "},
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._comment_row(comment_id).content, "初版")

    def test_cannot_edit_or_delete_someone_elses_comment(self):
        comment_id = self._publish("我的").get_json()["id"]
        response = self.client.put(
            f"/api/comments/{comment_id}", json={"content": "篡改"},
            headers=self.other_headers,
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.delete(
                f"/api/comments/{comment_id}", headers=self.other_headers
            ).status_code,
            403,
        )
        self.assertEqual(self._comment_row(comment_id).content, "我的")

    def test_editing_missing_comment_returns_404(self):
        response = self.client.put(
            "/api/comments/99999999", json={"content": "改"}, headers=self.headers
        )
        self.assertEqual(response.status_code, 404)

    def test_anonymous_cannot_edit_comment(self):
        comment_id = self._publish("初版").get_json()["id"]
        response = self.client.put(
            f"/api/comments/{comment_id}", json={"content": "改"}, headers={}
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self._comment_row(comment_id).content, "初版")

    # ---------- 评分：新増 / 覆盖 / 撤销 / 删除 ----------

    def test_rating_only_submit_leaves_no_comment(self):
        """只提交评分（个人中心等场景）：库里不该出现任何影评行。"""
        self.assertEqual(self._rate(4.0).status_code, 201)
        self.assertEqual(self._comments(), [])
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 4.0)])

    def test_rating_can_be_created_then_overwritten(self):
        self.assertEqual(self._rate(2.0).status_code, 201)
        self.assertEqual(self._rate(5.0).status_code, 200)
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 5.0)])

    def test_rating_delete_removes_only_that_movie(self):
        self._rate(4.0)
        self._rate(3.0, movie_id=OTHER_MOVIE_ID)

        response = self.client.delete(f"/api/ratings/{MOVIE_ID}", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self._rating_rows(), [(OTHER_MOVIE_ID, 3.0)])

    def test_deleting_missing_rating_returns_404(self):
        self.assertEqual(
            self.client.delete(f"/api/ratings/{MOVIE_ID}", headers=self.headers).status_code,
            404,
        )
        self.assertEqual(
            self.client.delete(f"/api/ratings/{UNKNOWN_MOVIE_ID}", headers=self.headers).status_code,
            404,
        )

    def test_anonymous_cannot_delete_rating(self):
        self._rate(4.0)
        self.assertEqual(
            self.client.delete(f"/api/ratings/{MOVIE_ID}", headers={}).status_code, 401
        )
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 4.0)])

    def test_zero_rating_is_rejected(self):
        """0 分不再是「撤销」：接口直接拒掉，已有的评分也不受影响。"""
        self._rate(5.0)
        response = self._rate(0)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 5.0)])

    def test_rating_outside_half_to_five_is_rejected(self):
        for bad in (0, 0.1, 0.3, 0.49, -1, 5.5, 6):
            with self.subTest(rating=bad):
                self.assertEqual(self._rate(bad).status_code, 400)
        self.assertEqual(self._rating_rows(), [])

    def test_removing_a_rating_leaves_detail_page_unrated(self):
        self._rate(4.0)
        before = self._stats()
        self.assertEqual(before["my_rating"], 4.0)
        self.assertEqual(before["count"], 1)

        self._unrate()

        after = self._stats()
        self.assertIsNone(after["my_rating"])
        self.assertEqual(after["count"], 0)
        self.assertIsNone(after["average"])

    def test_keeping_the_draft_removes_only_the_rating(self):
        """「保留影评草稿」＝只删评分：影评留在库里，只是退成仅自己可见。"""
        self._publish("我很喜欢")
        self._unrate()

        self.assertEqual(self._rating_rows(), [])
        self.assertEqual(len(self._comments()), 1)

    # ---------- 删影评：默认只删影评，with_rating 连评分一起删 ----------

    def test_deleting_a_comment_keeps_the_rating_by_default(self):
        """不带 with_rating：只删影评，评分原地不动（本来就是没评分那条路的用法）。"""
        self._rate(4.0)
        comment_id = self._comment("我很喜欢").get_json()["id"]

        response = self._delete_comment(comment_id)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["with_rating"])
        self.assertEqual(self._comments(), [])
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 4.0)])

    def test_deleting_a_comment_can_take_the_rating_with_it(self):
        """带 with_rating（「不保留」）：影评与评分两行一起删，详情页回显也归零。"""
        self._publish("我很喜欢")
        comment_id = self._comments()[0][0]

        response = self._delete_comment(comment_id, with_rating=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["with_rating"])
        # 两行都删干净：user_ratings 不留 0 分记录，movie_comments 不留空正文
        self.assertEqual(self._comments(), [])
        self.assertEqual(self._rating_rows(), [])

        stats = self._stats()
        self.assertIsNone(stats["my_rating"])
        self.assertEqual(stats["count"], 0)

    def test_with_rating_is_a_noop_when_the_author_has_no_rating(self):
        """先选过「保留」（影评处于「仅自己可见」）：带 with_rating 也不报错，只删影评。"""
        self._publish("我先打了分")
        comment_id = self._comments()[0][0]
        self._unrate()

        response = self._delete_comment(comment_id, with_rating=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["with_rating"])
        self.assertEqual(self._comments(), [])
        self.assertEqual(self._rating_rows(), [])

    def test_cascade_removes_only_this_movie_and_this_user(self):
        """连带删除只作用在「本人 × 这部电影」那一行。"""
        self._publish("我很喜欢")
        comment_id = self._comments()[0][0]
        self._rate(3.0, movie_id=OTHER_MOVIE_ID)
        self._rate(5.0, headers=self.other_headers)  # 别人对同一部电影的评分

        self._delete_comment(comment_id, with_rating=True)

        self.assertEqual(
            self._rating_rows(), [(OTHER_MOVIE_ID, 3.0), (MOVIE_ID, 5.0)]
        )

    def test_cannot_delete_someone_elses_comment_with_rating(self):
        """403 发生在删任何一行之前：别人的影评、以及各人的评分都不许动。"""
        self._publish("我的影评")
        comment_id = self._comments()[0][0]
        self._rate(5.0, headers=self.other_headers)  # 别人的评分

        response = self._delete_comment(
            comment_id, with_rating=True, headers=self.other_headers
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self._comments()), 1)
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 4.0), (MOVIE_ID, 5.0)])

    def test_deleting_missing_comment_with_rating_touches_no_rating(self):
        """影评不存在时直接 404，不会先把评分删掉。"""
        self._rate(4.0)

        response = self._delete_comment(99999999, with_rating=True)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self._rating_rows(), [(MOVIE_ID, 4.0)])

    # ---------- 删掉评分 ⇄ 影评可见性 ----------

    def test_removing_the_rating_hides_the_comment_from_others(self):
        self._publish("我很喜欢")
        comment_id = self._comments()[0][0]

        # 有评分：对所有人公开
        self.assertEqual(self._list(headers={})["total"], 1)

        self._unrate()

        # 对别人：整条时间线里都看不到
        self.assertEqual(self._list(headers={})["total"], 0)
        self.assertEqual(self._list(headers={})["comments"], [])
        # 对自己：仍然可见，并且明确标出「仅自己可见」
        mine = self._list()
        self.assertEqual(mine["total"], 1)
        self.assertIn(comment_id, [c["id"] for c in mine["comments"]])
        self.assertFalse(mine["my_comment"]["visible"])
        self.assertIsNone(mine["my_comment"]["user_rating"])

    def test_comment_becomes_public_again_when_rating_restored(self):
        self._publish("我很喜欢")
        self._unrate()
        self.assertEqual(self._list(headers={})["total"], 0)

        # 恢复评分（0.5 星及以上）→ 同一条影评自动重新公开，不需要重新发表
        self._rate(3.0)

        others = self._list(headers={})["total"]
        self.assertEqual(others, 1)
        self.assertTrue(self._list()["my_comment"]["visible"])

    def test_hidden_comment_is_isolated_to_its_author(self):
        """别人的隐藏影评，我连「有一条评论」都不该知道。"""
        self._rate(4.0, headers=self.other_headers)
        self._comment("别人的影评", headers=self.other_headers)
        self._unrate(headers=self.other_headers)

        body = self._list()          # 我是 flow_user
        self.assertEqual(body["total"], 0)
        self.assertIsNone(body["my_comment"])


if __name__ == "__main__":
    unittest.main()
