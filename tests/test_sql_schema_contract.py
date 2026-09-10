from pathlib import Path
import re
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class SqlSchemaContractTests(unittest.TestCase):
    def test_model_version_rank_is_quoted_for_mysql8(self):
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
        migration = (
            PROJECT_ROOT / "sql" / "migrations" / "001_add_recommendation_model_tables.sql"
        ).read_text(encoding="utf-8")

        self.assertRegex(schema, r"(?m)^\s+`rank`\s+SMALLINT")
        self.assertRegex(migration, r"(?m)^\s+`rank`\s+SMALLINT")


if __name__ == "__main__":
    unittest.main()
