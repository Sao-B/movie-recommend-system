# archive —— 已废弃、但可能还有参考价值的历史件

这里**不是**代码目录：任何程序、脚本、测试都不依赖本目录下的文件。
收录标准只有一条：**已经退出使用，但留着还能说明"当时为什么这么做"**。

| 文件 | 原路径 | 为什么废弃 |
|------|--------|-----------|
| `2026-01-09-admin-system-plan.md` | `docs/superpowers/plans/2026-01-09-admin-system.md` | 后台管理系统的实施计划（2235 行、带 `- [ ]` 复选框），功能已全部落地，计划本身不再维护 |
| `admin-system-design-draft.md` | `.claude/plans/admin-system.md` | 同一功能的早期设计稿（API 设计 + 页面布局参考），已被上面那份完整计划取代 |
| `001_add_movie_metadata_fields.sql` | `sql/migrations/001_add_movie_metadata_fields.sql` | 用了 MariaDB 专有的 `ADD COLUMN/INDEX IF NOT EXISTS`，在 MySQL 8 上**直接语法报错**；最终效果已由 `sql/schema.sql` + `009_align_schema_drift.sql` 覆盖 |
| `003_rename_ratings_table.sql` | `sql/migrations/003_rename_ratings_table.sql` | 重命名语句从未启用，方向与最终方案（`user_ratings` 为唯一事实来源）相反 |

**现行有效的迁移脚本仍在 `sql/migrations/`**，见该目录的 `README.md`；不要从这里执行任何 SQL。

什么时候可以整个删掉本目录：论文与答辩不再需要引用这些设计过程时。归档不代表"有价值"，只代表"删了会损失一点可追溯性"。
