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

    def test_comment_visibility_is_derived_not_stored(self):
        """可见性由评分推导，movie_comments 不存任何可见性状态位。

        （原实现是断言 schema.sql 的注释里出现「仅自己可见」这种中文短语——注释改了测试就红，
        而真加了 is_visible 列反而不会红。改成直接看建表列。）
        """
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
        comments = re.search(
            r"CREATE TABLE IF NOT EXISTS movie_comments \((.*?)\n\)", schema, re.S
        ).group(1)

        self.assertNotRegex(
            comments,
            r"(?mi)^\s+`?(is_visible|visible|hidden|is_hidden|visibility)`?\s",
            "可见性应由是否有评分推导，不应作为字段存储",
        )


    def test_movies_rich_columns_are_wide_enough_for_fetch_posters(self):
        """movies 的富化列宽必须容得下 fetch_posters.py 的截断长度。

        countries 实测就有 258 字符的取值，而 schema.sql 曾把它写成 VARCHAR(200)，
        按那份建表脚本新建的库会直接撞 "Data too long"（正是 cut() 要防的崩溃）。
        这里锁死「脚本的截断上限 <= 列宽」这条跨文件契约。
        """
        fetcher = (PROJECT_ROOT / "scripts" / "fetch_posters.py").read_text(encoding="utf-8")
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")

        m = re.search(r"def cut\(value, limit=(\d+)\)", fetcher)
        self.assertIsNotNone(m, "fetch_posters.py 的 cut() 默认 limit 找不到")
        limit = int(m.group(1))

        cut_cols = re.search(r"CUT_COLS = \(([^)]*)\)", fetcher)
        self.assertIsNotNone(cut_cols, "fetch_posters.py 的 CUT_COLS 找不到")
        names = [a or b for a, b in re.findall(r"'(\w+)'|\"(\w+)\"", cut_cols.group(1))]
        self.assertEqual(len(names), 5, f"CUT_COLS 应恰好 5 列，实际 {names}")

        movies = re.search(
            r"CREATE TABLE IF NOT EXISTS movies \((.*?)\n\)", schema, re.S
        ).group(1)
        for col in names:
            cm = re.search(rf"(?m)^\s+`?{col}`?\s+VARCHAR\((\d+)\)", movies)
            self.assertIsNotNone(cm, f"movies.{col} 未声明为 VARCHAR")
            self.assertGreaterEqual(
                int(cm.group(1)), limit,
                f"movies.{col} 宽度小于 fetch_posters.py 的截断上限 {limit}",
            )

    def test_fetch_posters_keeps_progress_in_the_database(self):
        """抓取进度不落任何表——待办与失败记录就是 movies 里缺列的行。

        脚本曾用临时队列记录进度（tmp_search_queue → tmp_movie_fetch_queue），
        2026-09-16 取消：数据库本身就是进度，一条 SQL 即可查出没抓到的条目。
        别再把队列表加回来。
        """
        fetcher = (PROJECT_ROOT / "scripts" / "fetch_posters.py").read_text(encoding="utf-8")

        self.assertNotIn("CREATE TABLE", fetcher, "fetch_posters.py 不应再建任何表")
        self.assertNotRegex(
            fetcher, r"QUEUE_TABLE|run_queue", "抓取进度队列机制已取消，不要再加回来"
        )
        self.assertRegex(
            fetcher,
            r"COALESCE\(poster_url, ''\) = '' OR overview IS NULL OR director IS NULL",
            "待办/失败记录必须直接由 movies 的缺列状态给出",
        )

    def test_movies_indexes_match_the_real_query_patterns(self):
        """movies 的索引只应有 idx_genres / idx_year / idx_popularity。

        idx_rating_count 被 idx_popularity 的最左前缀覆盖，属冗余，不要再长回来。
        （idx_popularity 服务的是热门榜的 `rating_count >= 100` 过滤；真实排序是贝叶斯加权
        表达式 `rating_count/(rating_count+m)*avg_rating+...`，索引不参与排序。）
        """
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
        movies = re.search(
            r"CREATE TABLE IF NOT EXISTS movies \((.*?)\n\)", schema, re.S
        ).group(1)

        names = set(re.findall(r"(?m)^\s+INDEX\s+(\w+)", movies))
        self.assertEqual(names, {"idx_genres", "idx_year", "idx_popularity"})
        self.assertIn("idx_popularity (rating_count DESC, avg_rating DESC)", movies)
        self.assertNotIn("INDEX idx_rating_count", movies)

    def test_users_has_no_redundant_index_on_username(self):
        """username 的唯一索引由列级 UNIQUE 隐式创建（名为 username），不要再另建同列普通索引。"""
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")
        users = re.search(
            r"CREATE TABLE IF NOT EXISTS users \((.*?)\n\)", schema, re.S
        ).group(1)

        self.assertIn("UNIQUE", users)
        self.assertNotIn("INDEX idx_username", users)

    def test_all_foreign_keys_cascade(self):
        """外键动作统一为 ON DELETE CASCADE（与 backend/app/routes/admin.py 的注释口径一致）。"""
        schema = (PROJECT_ROOT / "sql" / "schema.sql").read_text(encoding="utf-8")

        fks = re.findall(r"FOREIGN KEY\s*\(([^)]*)\)\s*REFERENCES\s+\w+\s*\([^)]*\)\s*ON DELETE (\w+)", schema)
        self.assertEqual(len(fks), 5, f"schema.sql 应有 5 条带 ON DELETE 的外键，实际 {fks}")
        self.assertEqual({rule.upper() for _, rule in fks}, {"CASCADE"})
        self.assertNotIn("ON DELETE RESTRICT", schema)
        self.assertNotIn("ON DELETE SET NULL", schema)


if __name__ == "__main__":
    unittest.main()
