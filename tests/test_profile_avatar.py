"""头像上传与读取路由的集成测试——临时 SQLite 库 + 临时上传目录，不碰线上 MySQL 与真实头像目录。

覆盖：
- 上传成功后 avatar_url 指向本服务的读取地址，文件确实落盘；
- 再传一次会删掉上一张本地图，不留孤儿文件；
- 读取路由无需鉴权即可取回图片；非法文件名（路径穿越 / 非本模块命名）一律 404；
- 扩展名、文件头、大小三类校验，以及未登录 401。

模型 BigInteger 主键在 SQLite 下需临时换成 Integer，见 ``sqlite_support.py``。
"""
import io
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
from app.models import User                                     # noqa: E402
from app.routes.profile import AVATAR_URL_PREFIX                 # noqa: E402
from app.routes.profile import PRESET_AVATAR_URL_PREFIX          # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch         # noqa: E402

# 1x1 透明 PNG，带真实文件头，用来过魔数校验
PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6300010000050001"
)
MAX_BYTES = 2 * 1024 * 1024


class _TestConfig:
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    JWT_SECRET_KEY = "profile-avatar-test-key-32bytes!!"
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
    CORS_ORIGINS = ["http://localhost:5173"]
    ALS_TOP_N = 20
    TESTING = True
    AVATAR_UPLOAD_DIR = None


class ProfileAvatarTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="profile_avatar_")
        tmp = Path(cls._tmpdir.name)
        _TestConfig.SQLALCHEMY_DATABASE_URI = "sqlite:///" + (tmp / "profile.db").as_posix()
        _TestConfig.AVATAR_UPLOAD_DIR = str(tmp / "avatars")
        with sqlite_integer_pk_patch():
            cls.app = create_app(_TestConfig)
            with cls.app.app_context():
                db.create_all()
        cls.client = cls.app.test_client()
        cls.upload_dir = Path(_TestConfig.AVATAR_UPLOAD_DIR)

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

        registered = self.client.post(
            "/api/auth/register", json={"username": "avatar_user", "password": "pw123456"}
        ).get_json()
        self.user_id = registered["user"]["id"]
        self.headers = {"Authorization": "Bearer " + registered["token"]}

    # ---------- 工具 ----------

    def _upload(self, content=PNG_BYTES, filename="me.png", content_type="image/png", headers=None):
        return self.client.post(
            "/api/profile/avatar",
            data={"file": (io.BytesIO(content), filename, content_type)},
            headers=self.headers if headers is None else headers,
            content_type="multipart/form-data",
        )

    @staticmethod
    def _filename_of(avatar_url):
        assert avatar_url.startswith(AVATAR_URL_PREFIX), avatar_url
        return avatar_url[len(AVATAR_URL_PREFIX):]

    # ---------- 上传 ----------

    def test_upload_writes_file_and_returns_local_url(self):
        response = self._upload()
        self.assertEqual(response.status_code, 200)

        avatar_url = response.get_json()["avatar_url"]
        self.assertTrue(avatar_url.startswith(AVATAR_URL_PREFIX))

        filename = self._filename_of(avatar_url)
        self.assertTrue(filename.startswith(f"avatar_{self.user_id}_"))
        self.assertEqual((self.upload_dir / filename).read_bytes(), PNG_BYTES)

    def test_upload_replaces_previous_local_avatar(self):
        first = self._upload().get_json()["avatar_url"]
        second = self._upload().get_json()["avatar_url"]

        self.assertNotEqual(first, second)
        self.assertFalse((self.upload_dir / self._filename_of(first)).exists())
        self.assertTrue((self.upload_dir / self._filename_of(second)).exists())

    def test_upload_keeps_external_avatar_url_untouched(self):
        """老数据里的外链头像（http 开头）不是本地文件，换头像时不该去删它。"""
        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            user.avatar_url = "https://example.com/old.jpg"
            db.session.commit()

        self.assertEqual(self._upload().status_code, 200)

    def test_upload_requires_login(self):
        self.assertEqual(self._upload(headers={}).status_code, 401)

    def test_upload_requires_file_field(self):
        response = self.client.post("/api/profile/avatar", data={}, headers=self.headers)
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_unsupported_extension(self):
        response = self._upload(filename="me.txt", content_type="text/plain")
        self.assertEqual(response.status_code, 400)

    def test_upload_rejects_content_that_is_not_an_image(self):
        """扩展名对、内容不对：靠文件头挡下来。"""
        self.assertEqual(self._upload(content=b"not an image at all").status_code, 400)

    def test_upload_rejects_oversized_file(self):
        before = set(self.upload_dir.glob("*")) if self.upload_dir.exists() else set()
        oversized = PNG_BYTES + b"\x00" * MAX_BYTES

        self.assertEqual(self._upload(content=oversized).status_code, 400)

        # 被拒的请求不在磁盘上留东西
        after = set(self.upload_dir.glob("*")) if self.upload_dir.exists() else set()
        self.assertEqual(before, after)

    # ---------- 读取 ----------

    def test_avatar_can_be_fetched_without_token(self):
        avatar_url = self._upload().get_json()["avatar_url"]

        response = self.client.get(avatar_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, PNG_BYTES)
        self.assertTrue(response.headers["Content-Type"].startswith("image/png"))
        response.close()  # send_file 的文件句柄要显式关掉，否则 unittest 会报 ResourceWarning

    def test_rejects_unknown_or_traversal_filenames(self):
        cases = [
            "../config.yaml",
            "avatar_1_short.png",
            "avatar_1_" + "f" * 32 + ".exe",
            "avatar_x_" + "f" * 32 + ".png",
        ]
        for filename in cases:
            with self.subTest(filename=filename):
                response = self.client.get("/api/profile/avatar/" + filename)
                self.assertEqual(response.status_code, 404)

    def test_missing_file_returns_404_not_500(self):
        response = self.client.get(AVATAR_URL_PREFIX + f"avatar_{self.user_id}_" + "f" * 32 + ".png")
        self.assertEqual(response.status_code, 404)

    # ---------- 内置 SVG 头像（界面上的换头像入口） ----------

    def test_preset_list_is_public_and_well_formed(self):
        response = self.client.get("/api/profile/avatars")   # 不带 token
        self.assertEqual(response.status_code, 200)

        presets = response.get_json()
        self.assertGreaterEqual(len(presets), 6)
        for preset in presets:
            with self.subTest(preset=preset):
                self.assertTrue(preset["id"])
                self.assertTrue(preset["name"])
                self.assertTrue(preset["url"].startswith(PRESET_AVATAR_URL_PREFIX))
                self.assertTrue(preset["url"].endswith(".svg"))

    def test_every_preset_in_the_list_has_a_real_file(self):
        """清单是代码里的常量，最容易出的错是「加了名字忘了放文件」——所以逐个取回来验。"""
        presets = self.client.get("/api/profile/avatars").get_json()
        for preset in presets:
            with self.subTest(url=preset["url"]):
                response = self.client.get(preset["url"])
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["Content-Type"].startswith("image/svg+xml"))
                self.assertTrue(response.data.startswith(b"<svg"))
                response.close()

    def test_preset_route_rejects_unknown_or_traversal_names(self):
        for name in ("../config.yaml", "film.txt", "Film.svg", "nope.svg", "film.svg.bak"):
            with self.subTest(name=name):
                self.assertEqual(
                    self.client.get(PRESET_AVATAR_URL_PREFIX + name).status_code, 404
                )

    def test_put_accepts_preset_url_and_reset(self):
        preset_url = self.client.get("/api/profile/avatars").get_json()[0]["url"]

        response = self.client.put(
            "/api/profile/info", json={"avatar_url": preset_url}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["avatar_url"], preset_url)

        # 恢复默认：置空，前端回落显示名首字符
        response = self.client.put(
            "/api/profile/info", json={"avatar_url": None}, headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.get_json()["avatar_url"])

    def test_put_rejects_avatars_outside_the_preset_list(self):
        for value in (
            "https://example.com/evil.png",                        # 外链
            AVATAR_URL_PREFIX + f"avatar_{self.user_id}_" + "f" * 32 + ".png",  # 上传路径
            "/api/profile/avatars/../../config.yaml",               # 想穿出去
        ):
            with self.subTest(value=value):
                response = self.client.put(
                    "/api/profile/info", json={"avatar_url": value}, headers=self.headers
                )
                self.assertEqual(response.status_code, 400)

    def test_switching_to_preset_deletes_the_uploaded_file(self):
        """换成内置头像后，历史上传的那张图不再被引用，磁盘上不该留孤儿文件。"""
        uploaded = self._upload().get_json()["avatar_url"]
        uploaded_file = self.upload_dir / self._filename_of(uploaded)
        self.assertTrue(uploaded_file.exists())

        preset_url = self.client.get("/api/profile/avatars").get_json()[0]["url"]
        self.client.put("/api/profile/info", json={"avatar_url": preset_url}, headers=self.headers)

        self.assertFalse(uploaded_file.exists())

    def test_reset_also_deletes_the_uploaded_file(self):
        uploaded = self._upload().get_json()["avatar_url"]
        uploaded_file = self.upload_dir / self._filename_of(uploaded)

        self.client.put("/api/profile/info", json={"avatar_url": None}, headers=self.headers)

        self.assertFalse(uploaded_file.exists())


if __name__ == "__main__":
    unittest.main()
