-- ============================================================
-- 迁移脚本 006: 创建 movie_rating_stats 表
-- 执行时间: 2026-09-15
-- 目的: 存放 MovieLens 全量评分的「五档分布」聚合结果。
--       库里的 movies 表只有 avg_rating / rating_count 两个标量，
--       没有分档计数；详情页「评分统计」的五档柱状图需要分档数据，
--       故从原始明细 data/raw/ratings.csv 离线聚合一次并落表。
--
-- 数据由 scripts/build_ml_rating_stats.py 写入（一次性，不需随训练重算）。
-- 页面展示时本表与本站 user_ratings 合并，不区分来源。
-- ============================================================

USE movie_recommend;

CREATE TABLE IF NOT EXISTS movie_rating_stats (
  movie_id     INT           NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  rating_count INT           NOT NULL COMMENT 'MovieLens 评分总条数',
  rating_sum   DECIMAL(14,1) NOT NULL COMMENT 'MovieLens 评分总和（0.5 粒度，1 位小数即精确），用于精确加权平均',
  cnt_1        INT           NOT NULL DEFAULT 0 COMMENT '1 星档条数',
  cnt_2        INT           NOT NULL DEFAULT 0 COMMENT '2 星档条数',
  cnt_3        INT           NOT NULL DEFAULT 0 COMMENT '3 星档条数',
  cnt_4        INT           NOT NULL DEFAULT 0 COMMENT '4 星档条数',
  cnt_5        INT           NOT NULL DEFAULT 0 COMMENT '5 星档条数',
  PRIMARY KEY (movie_id),
  -- 电影删除时级联删除其统计数据（FK 子句后不能写 COMMENT，否则 MySQL 8.0 报 1064）
  CONSTRAINT fk_mrs_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MovieLens 评分五档分布（离线聚合，与本站评分合并展示）';

-- 注意：本表依赖 movies 表的行存在（有外键）。spark/preprocess.py 重载 movies
-- 时用的是 TRUNCATE，外键会挡住它——这一点在引入 movie_comments 时就已经成立，
-- 本表没有让它变差；但重载 movies 后必须重跑 build_ml_rating_stats.py 重建本表。

-- 验证
SHOW CREATE TABLE movie_rating_stats;
