"""个人资料（``GET/PUT /api/profile/info``）的集成测试——临时 SQLite 库，不碰线上 MySQL。

这一块的核心约定是**账号与昵称分开**：

- 账号 ``username``：注册时按命名规则定下，登录凭据，**接口上不可改**（带它就 400）；
- 昵称 ``nickname``：个人主页能改的那个字段，任意字符、不唯一、可清空；
- 界面上显示的名字 = ``display_name`` = 昵称 or 账号（没设昵称就回落账号）。

模型 BigInteger 主键在 SQLite 下需临时换成 Integer，见 ``sqlite_support.py``。
"""
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app                                      # noqa: E402
from app.db import db                                           # noqa: E402
from app.models import Movie, User                               # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch         # noqa: E402

ACCOUNT = "info_user"
MOVIE_ID = 318


class _TestConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "profile-info-test-key-32bytes!!"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True


class ProfileInfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="profile_info_")
        _TestConfig.SQLALCHEMY_DATABASE_URI = (
            "sqlite:///" + (Path(cls._tmpdir.name) / "profile.db").as_posix()
        )
        with sqlite_integer_pk_patch():
            cls.app = create_app(_TestConfig)
            with cls.app.app_context():
                db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls._tmpdir.cleanup()

    def setUp(self):
        with self.app.app_context():
            db.session.query(User).delete()
            # 影评用例要用到，造一部片子（评分外键指向 movies）
            if db.session.get(Movie, MOVIE_ID) is None:
                db.session.add(Movie(movie_id=MOVIE_ID, title="Shawshank Redemption, The"))
            db.session.commit()

        registered = self.client.post(
            "/api/auth/register", json={"username": ACCOUNT, "password": "pw123456"}
        ).get_json()
        self.user_id = registered["user"]["id"]
        self.headers = {"Authorization": "Bearer " + registered["token"]}

    def _get_info(self):
        response = self.client.get("/api/profile/info", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        return response.get_json()

    def _put_info(self, payload, headers=None):
        return self.client.put(
            "/api/profile/info", json=payload,
            headers=self.headers if headers is None else headers,
        )

    def _account_in_db(self):
        with self.app.app_context():
            return db.session.get(User, self.user_id).username

    # ---------- 读 ----------

    def test_info_returns_account_and_nickname(self):
        info = self._get_info()
        self.assertEqual(info["username"], ACCOUNT)
        self.assertEqual(info["nickname"], ACCOUNT)     # 注册时初始化为账号
        self.assertEqual(info["display_name"], ACCOUNT)
        self.assertEqual(info["id"], self.user_id)

    def test_info_requires_login(self):
        self.assertEqual(
            self.client.get("/api/profile/info", headers={}).status_code, 401
        )

    # ---------- 改昵称 ----------

    def test_nickname_accepts_any_characters(self):
        """昵称不受账号那套字符限制——中文、空格、符号都可以。"""
        for nickname in ("张三", "Andy Dufresne", "电影爱好者 🎬", "me@home"):
            with self.subTest(nickname=nickname):
                response = self._put_info({"nickname": nickname, "bio": "签名也一起改"})
                self.assertEqual(response.status_code, 200)
                body = response.get_json()
                self.assertEqual(body["nickname"], nickname)
                self.assertEqual(body["display_name"], nickname)
                self.assertEqual(body["bio"], "签名也一起改")
                # 账号始终没被动过
                self.assertEqual(body["username"], ACCOUNT)
                self.assertEqual(self._account_in_db(), ACCOUNT)

    def test_clearing_nickname_falls_back_to_account(self):
        self._put_info({"nickname": "临时昵称"})
        response = self._put_info({"nickname": "   "})   # 空白 = 清空

        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertIsNone(body["nickname"])
        self.assertEqual(body["display_name"], ACCOUNT)

    def test_too_long_nickname_is_rejected(self):
        response = self._put_info({"nickname": "昵" * 51})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._get_info()["nickname"], ACCOUNT)   # 没被改

    # ---------- 账号不可改 ----------

    def test_username_cannot_be_changed(self):
        response = self._put_info({"username": "brand_new_name"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._account_in_db(), ACCOUNT)

    def test_username_rejected_even_when_sent_with_valid_nickname(self):
        """老前端会连 username 一起发过来：整体拒绝，不能只改一半。"""
        response = self._put_info({"username": ACCOUNT, "nickname": "新昵称"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self._get_info()["nickname"], ACCOUNT)

    # ---------- 昵称会出现在对外的署名里 ----------

    def test_comment_serialisation_exposes_display_name(self):
        """影评署名跟着昵称走（前端用 display_name 渲染）。"""
        self._put_info({"nickname": "署名测试"})
        rated = self.client.post(
            "/api/ratings/", json={"movie_id": MOVIE_ID, "rating": 4.5}, headers=self.headers
        )
        self.assertEqual(rated.status_code, 201)   # 新建评分返回 201

        created = self.client.post(
            f"/api/movies/{MOVIE_ID}/comments", json={"content": "正文"},
            headers=self.headers,
        )
        self.assertEqual(created.status_code, 201)
        body = created.get_json()
        self.assertEqual(body["display_name"], "署名测试")
        self.assertEqual(body["username"], ACCOUNT)


if __name__ == "__main__":
    unittest.main()
