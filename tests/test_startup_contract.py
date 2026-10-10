"""启动入口契约。

历史教训：`scripts/start_bg.ps1` 曾把后端入口写成相对路径 `scripts\\start_backend.py`，
而 `Start-Process -WorkingDirectory` 是 `backend`，python 按 CWD 解析成
`backend\\scripts\\start_backend.py`（该文件不存在）——前端能起、后端起不来，
而当时这条测试只断言 `start.bat` 文本里出现过这个字符串，完全没发现。
所以这里盯的是「入口以项目根为基准传绝对路径」，不是某个文件里有没有那句话。

第二条是「先等后端就绪，再开浏览器」（2026-09-18 补）：后端冷启动实测 ~20s 才监听端口，
早期只等前端端口的做法会让首页那次 `/api/movies/` 直撞代理的 ECONNREFUSED 而白掉。
"""
import re
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))
sys.path.insert(0, str(PROJECT_ROOT))

from app import create_app                                      # noqa: E402
from app.db import db                                          # noqa: E402
from tests.sqlite_support import sqlite_integer_pk_patch        # noqa: E402


class StartupContractTests(unittest.TestCase):
    def test_backend_entrypoint_exists(self):
        self.assertTrue((PROJECT_ROOT / "scripts" / "start_backend.py").is_file())

    def test_launcher_passes_project_root_based_entrypoint(self):
        launcher = (PROJECT_ROOT / "scripts" / "start_bg.ps1").read_text(encoding="utf-8")

        self.assertIn("Join-Path $projectRoot 'scripts\\start_backend.py'", launcher)


class LauncherWaitsForBackendTests(unittest.TestCase):
    """启动脚本必须先等后端就绪（并预热首页查询），再打开浏览器。"""

    @classmethod
    def setUpClass(cls):
        cls.launcher = (PROJECT_ROOT / "scripts" / "start_bg.ps1").read_text(encoding="utf-8")

    def test_health_probe_targets_the_configured_flask_port(self):
        self.assertIn('$healthUrl = "http://127.0.0.1:$flaskPort/api/health"', self.launcher)

    def test_browser_opens_only_after_the_backend_is_ready(self):
        wait_at = self.launcher.index("Test-BackendReady")
        open_at = self.launcher.index("Start-Process $frontUrl")
        self.assertLess(wait_at, open_at, "开浏览器必须排在等后端就绪之后")

    def test_warmup_request_matches_the_home_page_query(self):
        """预热的那次查询必须与首页首次请求一致，否则预热的是别的路径。"""
        home = (PROJECT_ROOT / "frontend" / "src" / "views" / "HomeView.vue").read_text(encoding="utf-8")
        per_page = re.search(r"const perPage\s*=\s*(\d+)", home).group(1)
        sort = re.search(r"sort:\s*'(\w+)'", home).group(1)

        self.assertIn(f"/api/movies/?page=1&per_page={per_page}&sort={sort}", self.launcher)


class HealthProbeTests(unittest.TestCase):
    """`GET /api/health`——启动脚本等的就是它，必须不鉴权、且带一句库检查。"""

    @classmethod
    def setUpClass(cls):
        cls._tmpdir = tempfile.TemporaryDirectory(prefix="startup_health_")

        class _Config:
            SQLALCHEMY_TRACK_MODIFICATIONS = False
            JWT_SECRET_KEY = "startup-contract-test-key-32bytes!"
            JWT_ACCESS_TOKEN_EXPIRES = timedelta(minutes=15)
            CORS_ORIGINS = ["http://localhost:5173"]
            ALS_TOP_N = 20
            TESTING = True

        _Config.SQLALCHEMY_DATABASE_URI = (
            "sqlite:///" + (Path(cls._tmpdir.name) / "health.db").as_posix()
        )
        with sqlite_integer_pk_patch():
            cls.app = create_app(_Config)
            with cls.app.app_context():
                db.create_all()
        cls.client = cls.app.test_client()

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.engine.dispose()
        cls._tmpdir.cleanup()

    def test_health_is_ok_without_a_token(self):
        response = self.client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok", "database": "up"})


if __name__ == "__main__":
    unittest.main()
