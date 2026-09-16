-- ============================================================
-- 008_drop_legacy_ratings.sql — 删除遗留 ratings 表
-- 适用：2026-09-14 之前创建的存量数据库
-- 全新建库直接执行 schema.sql 即可，本迁移无需执行
-- ============================================================
-- 背景：
--   - 自迁移 005 起，评分的唯一事实来源是 user_ratings；
--   - ratings 表自 2026-09-14 起不再被应用或训练任务读写，
--     数据已由 005_merge_ratings_into_user_ratings.sql 合并；
--   - 长期保留只会让全新建库多出两条指向 movies/users 的无意义外键
--     （preprocess.py 的 TRUNCATE 流程旧文档曾多次被它绊住）。
-- 幂等：DROP TABLE IF EXISTS，可重复执行。
-- ============================================================

USE movie_recommend;

DROP TABLE IF EXISTS ratings;
