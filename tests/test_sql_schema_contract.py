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

    def test_comment_edited_at_exists_in_model_schema_and_migration(self):
        """影评的 edited_at 必须三处一致：模型、schema.sql、迁移 007。

        「上次修改于」这个展示依赖它，任何一处漏了都会让部署出来的库与代码对不上。
        """
        model = (PROJECT_ROOT / "backend" / "app" / "models.py").read_text(encoding="utf-8")
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
        migration = (
            PROJECT_ROOT / "sql" / "migrations" / "007_add_comment_edited_at.sql"
        ).read_text(encoding="utf-8")

        self.assertIn("edited_at", model)
        self.assertRegex(schema, r"(?m)^\s+edited_at\s+TIMESTAMP\s+NULL")
        self.assertIn("ADD COLUMN edited_at", migration)

    def test_schema_documents_visibility_derived_from_rating(self):
        """可见性从评分推导、不存状态位——这条设计必须写在建表脚本里。"""
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
        self.assertIn("仅自己可见", schema)
        self.assertIn("可见性不存字段", schema)
        # 「打 0 分＝撤销」已经撤掉，别再留在建表注释里误导人
        self.assertNotIn("撤销评分", schema)


if __name__ == "__main__":
    unittest.main()
