# 增量迁移说明

**全新数据库不要执行本目录**，直接跑 `sql/schema.sql` 即可建出最终结构。
这里只服务「库里已有数据、不想重建」的存量库，按需挑单个文件执行：

```bash
mysql -u root -p movie_recommend < sql/migrations/005_merge_ratings_into_user_ratings.sql
```

## 文件一览

| 文件 | 作用 | 备注 |
|------|------|------|
| `001_add_recommendation_model_tables.sql` | 建 model_versions / model_item_factors / recommendations | 与 `001_add_user_role.sql` 撞号，见下方「编号冲突」 |
| `001_add_user_role.sql` | users 加 role 列 | 同上 |
| `002_create_movie_comments_table.sql` | 建 movie_comments | |
| `004_add_user_profile_fields.sql` | users 加头像 / 简介字段 | |
| `005_merge_ratings_into_user_ratings.sql` | 把 legacy `ratings` 数据并进 `user_ratings` | 评分表统一的关键一步 |
| `006_add_movie_rating_stats.sql` | 建 movie_rating_stats | |
| `007_add_comment_edited_at.sql` | movie_comments 加 edited_at | |
| `008_drop_legacy_ratings.sql` | `DROP TABLE IF EXISTS ratings` | 幂等 |
| `009_align_schema_drift.sql` | 把 2026-09-16 之前的旧库对齐到 `schema.sql`（加宽富化列、去冗余索引、统一约束名） | **不可重复执行**，已存在/不存在时报错属预期；线上库已对齐，**不需要**执行 |
| `010_drop_redundant_indexes.sql` | 删 `user_ratings.idx_user`、`model_item_factors.idx_factor_version` 两个被最左前缀覆盖的冗余索引 | **不可重复执行**（重跑报 1091）；线上库已于 2026-09-17 执行 |
| `011_add_user_nickname.sql` | users 加 `nickname`（昵称），并把存量用户的昵称初始化成账号名 | **不可重复执行**（重跑报 1060）；2026-09-18 已在线上库执行 |
| `012_update_users_avatar_comment.sql` | 只改 `users.avatar_url` 的列注释（头像来源变多：内置 SVG / 历史上传 / 外链 / NULL=默认） | 幂等（注释相同再跑等于重写一遍）；2026-09-18 已执行 |
| `013_fix_mojibake_comments.sql` | 修 4 个列注释 + 2 个表注释的乱码（`users.username` / `users.role` / `movies.title` / `movies.genres` / 两张表），按 `schema.sql` 原文重写 | 幂等；2026-09-18 已执行 |

> 两个**已废弃**的脚本已移入 [`archive/`](../../archive/)：`001_add_movie_metadata_fields.sql`
> （MariaDB 专有语法，MySQL 8 直接报错）与 `003_rename_ratings_table.sql`（重命名从未启用）。
> 它们只说明历史，留在本目录容易被误执行。

## 编号冲突（已知问题）

`001_add_recommendation_model_tables` / `001_add_user_role` 两个文件共用 `001` 前缀，
文件名排序不代表执行顺序。它们都是**已应用的存量脚本**，重命名会让 `README.md`、
`docs/implementation-guide.md` 里的引用失效，所以保持原样，仅在此登记。

新增迁移请从 `011_` 起编号。
