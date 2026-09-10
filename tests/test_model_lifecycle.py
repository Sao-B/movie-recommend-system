import sys
from pathlib import Path
import types
import unittest
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "spark"))

from utils.mysql_utils import activate_model_version, retire_model_version  # noqa: E402


class FakeCursor:
    def __init__(self, statements):
        self.statements = statements
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def execute(self, sql, params):
        self.statements.append((sql, params))


class FakeConnection:
    def __init__(self):
        self.statements = []
        self.committed = False
        self.rolled_back = False

    def cursor(self):
        return FakeCursor(self.statements)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


class ModelLifecycleTests(unittest.TestCase):
    MYSQL_CONFIG = {
        "host": "localhost",
        "port": 3306,
        "user": "test",
        "password": "test",
        "database": "movie_recommend",
    }

    def setUp(self):
        self.connection = FakeConnection()
        fake_pymysql = types.SimpleNamespace(
            connect=lambda **kwargs: self.connection
        )
        self.pymysql_patch = patch.dict(sys.modules, {"pymysql": fake_pymysql})
        self.pymysql_patch.start()
        self.addCleanup(self.pymysql_patch.stop)

    def test_activation_retires_old_active_before_publishing_building_model(self):
        activate_model_version(self.MYSQL_CONFIG, "v2")

        self.assertEqual(len(self.connection.statements), 2)
        self.assertIn("WHERE status = 'active'", self.connection.statements[0][0])
        self.assertIn("status = 'building'", self.connection.statements[1][0])
        self.assertTrue(self.connection.committed)

    def test_failed_model_cleanup_only_retires_building_version(self):
        retire_model_version(self.MYSQL_CONFIG, "v2")

        self.assertEqual(len(self.connection.statements), 1)
        self.assertIn("status = 'building'", self.connection.statements[0][0])
        self.assertNotIn("status = 'active'", self.connection.statements[0][0])
        self.assertTrue(self.connection.committed)


if __name__ == "__main__":
    unittest.main()
