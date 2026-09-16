"""auth 路由（注册 / 登录）的集成测试——临时 SQLite 库，不碰线上 MySQL。

覆盖：
- 注册：成功返回 token + 用户信息（不含密码哈希）、空白裁剪、四类入参校验、
  用户名重复 409、token 的 identity 是新用户 id 的字符串形式；
- 登录：正确密码 200、错误密码与不存在用户同样返回 401（不泄露用户是否存在）。

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

from app import create_app                                  # noqa: E402
from app.db import db                                       # noqa: E402
from app.models import User                                 # noqa: E402
from flask_jwt_extended import decode_token                 # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch    # noqa: E402


class _TestConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "auth-route-test-key-32bytes-long!!"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True


class AuthRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="auth_routes_")
        db_file = Path(cls._tmpdir.name) / "auth.db"
        with sqlite_integer_pk_patch():
            _TestConfig.SQLALCHEMY_DATABASE_URI = "sqlite:///" + db_file.as_posix()
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
            db.session.commit()

    # ---------- 注册 ----------

    def test_register_returns_token_and_user(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "alice", "password": "pw123456"},
        )
        self.assertEqual(response.status_code, 201)
        body = response.get_json()
        self.assertEqual(body["user"]["username"], "alice")
        self.assertNotIn("password_hash", body["user"])
        self.assertIn("token", body)

        with self.app.app_context():
            self.assertEqual(decode_token(body["token"])["sub"], str(body["user"]["id"]))

    def test_register_strips_surrounding_whitespace(self):
        response = self.client.post(
            "/api/auth/register",
            json={"username": "  bob  ", "password": "  pw123456  "},
        )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["user"]["username"], "bob")

    def test_register_rejects_missing_fields(self):
        for payload in ({}, {"username": "x"}, {"password": "x"}, {"username": "", "password": ""}):
            with self.subTest(payload=payload):
                response = self.client.post("/api/auth/register", json=payload)
                self.assertEqual(response.status_code, 400)

    def test_register_rejects_too_long_username(self):
        response = self.client.post(
            "/api/auth/register", json={"username": "u" * 51, "password": "pw123456"}
        )
        self.assertEqual(response.status_code, 400)

    def test_register_rejects_short_password(self):
        response = self.client.post(
            "/api/auth/register", json={"username": "carol", "password": "12345"}
        )
        self.assertEqual(response.status_code, 400)

    def test_register_rejects_too_long_password(self):
        response = self.client.post(
            "/api/auth/register", json={"username": "carol", "password": "p" * 129}
        )
        self.assertEqual(response.status_code, 400)

    def test_duplicate_username_returns_409(self):
        self.client.post(
            "/api/auth/register", json={"username": "dave", "password": "pw123456"}
        )
        response = self.client.post(
            "/api/auth/register", json={"username": "dave", "password": "pw654321"}
        )
        self.assertEqual(response.status_code, 409)

    # ---------- 登录 ----------

    def _register(self, username="erin", password="pw123456"):
        return self.client.post(
            "/api/auth/register", json={"username": username, "password": password}
        )

    def test_login_with_correct_password(self):
        self._register()
        response = self.client.post(
            "/api/auth/login", json={"username": "erin", "password": "pw123456"}
        )
        self.assertEqual(response.status_code, 200)
        body = response.get_json()
        self.assertEqual(body["user"]["username"], "erin")
        self.assertNotIn("password_hash", body["user"])
        self.assertIn("token", body)

    def test_login_with_wrong_password_returns_401(self):
        self._register()
        response = self.client.post(
            "/api/auth/login", json={"username": "erin", "password": "wrong!"}
        )
        self.assertEqual(response.status_code, 401)

    def test_unknown_user_and_wrong_password_share_the_same_message(self):
        """401 文案一致，避免通过报错信息枚举用户名。"""
        self._register()
        wrong_password = self.client.post(
            "/api/auth/login", json={"username": "erin", "password": "wrong!"}
        ).get_json()["msg"]
        unknown_user = self.client.post(
            "/api/auth/login", json={"username": "nobody", "password": "whatever"}
        ).get_json()["msg"]
        self.assertEqual(wrong_password, unknown_user)

    def test_login_with_legacy_unsupported_hash_is_401_not_500(self):
        """历史 bcrypt 哈希会让 check_password_hash 抛 ValueError，应按密码错误处理。"""
        with self.app.app_context():
            user = User(username="legacy_hash_user")
            user.password_hash = "$2b$12$notarealbcrypthashbutenoughlength0000000000000000000000"
            db.session.add(user)
            db.session.commit()

        response = self.client.post(
            "/api/auth/login", json={"username": "legacy_hash_user", "password": "anything"}
        )
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
