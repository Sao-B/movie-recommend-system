-- ============================================================
-- 迁移脚本 005: 统一评分表 —— 应用评分迁移到 user_ratings
-- 执行时间: 2026-09-14
-- 目的: 修复应用写 legacy `ratings`、训练读 `user_ratings` 的数据分裂
-- ============================================================
--
-- 背景：
--   应用层（backend）此前经 SQLAlchemy 模型写 legacy `ratings` 表，
--   而 Spark 训练契约（utils/training_contract.py）固定读 `user_ratings`，
--   导致前端评分永远进不了训练集。
--   本次迁移把 `ratings` 中的评分合并进 `user_ratings`，
--   应用模型（backend/app/models.py 的 UserRating）已改指向 `user_ratings`。
--
-- 冲突策略：
--   两表均有 UNIQUE (user_id, movie_id)。若同一 (user_id, movie_id)
--   在两表都存在，保留 `user_ratings` 已有记录（不覆盖）。
--   实际迁移时 `ratings` 数据与 `user_ratings` 无重叠。
--
-- 幂等性：
--   可重复执行；已迁移过的行会被 WHERE NOT EXISTS 跳过。
--
-- 执行前请备份：
--   mysqldump -u root -p --single-transaction movie_recommend > movie_recommend_backup.sql
--
-- 执行后验证：
--   SELECT COUNT(*) FROM user_ratings;
--   SELECT COUNT(*) FROM ratings;   -- legacy 表保留，不再被应用读写
-- ============================================================

USE movie_recommend;

INSERT INTO user_ratings (user_id, movie_id, rating, rated_at)
SELECT r.user_id, r.movie_id, r.rating, r.rated_at
FROM ratings r
WHERE NOT EXISTS (
    SELECT 1 FROM user_ratings u
    WHERE u.user_id = r.user_id AND u.movie_id = r.movie_id
);
