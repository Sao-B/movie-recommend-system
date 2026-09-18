"""启动入口契约。

历史教训：`scripts/start_bg.ps1` 曾把后端入口写成相对路径 `scripts\\start_backend.py`，
而 `Start-Process -WorkingDirectory` 是 `backend`，python 按 CWD 解析成
`backend\\scripts\\start_backend.py`（该文件不存在）——前端能起、后端起不来，
而当时这条测试只断言 `start.bat` 文本里出现过这个字符串，完全没发现。
所以这里盯的是「入口以项目根为基准传绝对路径」，不是某个文件里有没有那句话。
"""
from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StartupContractTests(unittest.TestCase):
    def test_backend_entrypoint_exists(self):
        self.assertTrue((PROJECT_ROOT / "scripts" / "start_backend.py").is_file())

    def test_launcher_passes_project_root_based_entrypoint(self):
        launcher = (PROJECT_ROOT / "scripts" / "start_bg.ps1").read_text(encoding="utf-8")

        self.assertIn("Join-Path $projectRoot 'scripts\\start_backend.py'", launcher)


if __name__ == "__main__":
    unittest.main()
