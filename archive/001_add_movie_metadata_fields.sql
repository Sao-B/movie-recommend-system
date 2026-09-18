-- ============================================================
-- 迁移脚本 001: 为 movies 表添加扩展字段
-- 执行时间: 2024-01-XX
-- 目的: 支持电影详情页展示更丰富的信息和海报
--
-- 注（2026-09-16）：本脚本写入的 writers TEXT、countries/languages VARCHAR(200)
-- 以及 idx_rating_count 已在 migrations/009 中修正（加宽到 VARCHAR(500)、去掉冗余索引）。
-- 此处保持历史原样不改——迁移链的最终结构与 schema.sql 的一致性由 009 负责。
-- ============================================================

USE movie_recommend;

-- 添加新字段
ALTER TABLE movies
  ADD COLUMN IF NOT EXISTS avg_rating DECIMAL(3,2) COMMENT '平均评分（0.00-5.00）',
  ADD COLUMN IF NOT EXISTS rating_count INT DEFAULT 0 COMMENT '评分人数',
  ADD COLUMN IF NOT EXISTS poster_url VARCHAR(500) COMMENT 'TMDB 海报 URL',
  ADD COLUMN IF NOT EXISTS overview TEXT COMMENT '电影简介',
  ADD COLUMN IF NOT EXISTS director VARCHAR(500) COMMENT '导演',
  ADD COLUMN IF NOT EXISTS writers TEXT COMMENT '编剧（多个用逗号分隔）',
  ADD COLUMN IF NOT EXISTS `cast` TEXT COMMENT '主演（多个用逗号分隔）',
  ADD COLUMN IF NOT EXISTS countries VARCHAR(200) COMMENT '制片国家/地区',
  ADD COLUMN IF NOT EXISTS languages VARCHAR(200) COMMENT '语言',
  ADD COLUMN IF NOT EXISTS release_date DATE COMMENT '上映日期',
  ADD COLUMN IF NOT EXISTS runtime SMALLINT COMMENT '片长（分钟）';

-- 添加索引
ALTER TABLE movies
  ADD INDEX IF NOT EXISTS idx_year (year),
  ADD INDEX IF NOT EXISTS idx_rating_count (rating_count);

-- 验证
SELECT COUNT(*) as total_movies,
       COUNT(poster_url) as movies_with_poster,
       COUNT(overview) as movies_with_overview
FROM movies;
