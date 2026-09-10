from pathlib import Path
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class StartupContractTests(unittest.TestCase):
    def test_windows_launcher_uses_resilient_backend_entrypoint(self):
        launcher = (PROJECT_ROOT / "start.bat").read_text(encoding="utf-8")

        self.assertIn("scripts\\start_backend.py", launcher)
        self.assertIn("if errorlevel 1", launcher.lower())

    def test_backend_entrypoint_exists(self):
        self.assertTrue((PROJECT_ROOT / "scripts" / "start_backend.py").is_file())


if __name__ == "__main__":
    unittest.main()
