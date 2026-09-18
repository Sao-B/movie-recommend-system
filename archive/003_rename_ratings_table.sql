-- ============================================================
-- 迁移脚本 003（已废弃，被 005 取代）
-- ============================================================
-- 原意图是“统一评分表名为 ratings”，与 001（应用评分迁移到
-- user_ratings）和 005（合并 ratings 数据进 user_ratings）的方向相反，
-- 且原脚本为半成品（RENAME 语句被注释，仅含只读查询）。
--
-- 2026-09-14 起评分表统一方向定为：user_ratings 为唯一事实来源。
-- 请执行 005_merge_ratings_into_user_ratings.sql。
-- 本脚本保留仅为说明历史，不需要执行。
-- ============================================================

USE movie_recommend;

SELECT TABLE_NAME, TABLE_ROWS
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'movie_recommend'
AND TABLE_NAME IN ('ratings', 'user_ratings');
