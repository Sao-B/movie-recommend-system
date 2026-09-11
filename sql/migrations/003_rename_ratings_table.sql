-- ============================================================
-- 迁移脚本 003: 确保评分表名统一为 ratings
-- 执行时间: 2024-01-XX
-- 目的: 统一评分表名，避免 user_ratings 和 ratings 混淆
-- ============================================================

USE movie_recommend;

-- 检查是否存在 user_ratings 表
SET @table_exists = (
  SELECT COUNT(*)
  FROM information_schema.TABLES
  WHERE TABLE_SCHEMA = 'movie_recommend'
  AND TABLE_NAME = 'user_ratings'
);

-- 如果存在 user_ratings 且不存在 ratings，则重命名
-- 注意：此脚本需要根据实际情况手动调整
--
-- 选项 A: 如果当前使用 user_ratings，保持不变
-- 选项 B: 如果当前使用 ratings，且 user_ratings 不存在，无需操作
-- 选项 C: 如果两个表都存在，需要手动合并数据

-- 查看当前表结构
SELECT TABLE_NAME, TABLE_ROWS
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = 'movie_recommend'
AND TABLE_NAME IN ('ratings', 'user_ratings');

-- 如果需要重命名，取消下面注释：
-- RENAME TABLE user_ratings TO ratings;
