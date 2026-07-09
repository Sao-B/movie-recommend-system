-- ============================================================
-- 个性化电影推荐系统 — MySQL 建库建表 DDL
-- 执行方式: mysql -u root -p
--          SOURCE d:/Sundries/Paper/Project/sql/schema.sql;
-- ============================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS movie_recommend
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE movie_recommend;

-- ----------------------------------------------------------
-- 系统用户表（前端注册/登录）
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
  id           INT          NOT NULL AUTO_INCREMENT,
  username     VARCHAR(50)  NOT NULL UNIQUE COMMENT '用户名',
  password_hash VARCHAR(255) NOT NULL       COMMENT 'bcrypt 哈希',
  created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统用户';

-- ----------------------------------------------------------
-- 电影元数据表（从 movies.csv 导入）
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS movies (
  movie_id  INT          NOT NULL COMMENT 'MovieLens 原始 ID',
  title     VARCHAR(500) NOT NULL COMMENT '电影标题（含年份）',
  genres    VARCHAR(500)          COMMENT '类型，竖线分隔，如 Action|Drama',
  year      SMALLINT              COMMENT '上映年份，从 title 中解析',
  PRIMARY KEY (movie_id),
  INDEX idx_genres (genres(100))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='电影元数据';

-- ----------------------------------------------------------
-- 评分表（MovieLens 原始评分 + 用户在前端提交的实时评分）
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS ratings (
  id        BIGINT    NOT NULL AUTO_INCREMENT,
  user_id   INT       NOT NULL COMMENT '对应 users.id 或 MovieLens 用户 ID',
  movie_id  INT       NOT NULL COMMENT '对应 movies.movie_id',
  rating    FLOAT     NOT NULL COMMENT '评分值 0.5~5.0',
  rated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_movie (user_id, movie_id),
  INDEX idx_user   (user_id),
  INDEX idx_movie  (movie_id),
  INDEX idx_movie_ref (movie_id)   -- 无外键约束，由应用层保证一致性
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户评分';

-- ----------------------------------------------------------
-- 推荐结果表（ALS 离线训练输出，Spark 批量写入）
-- ----------------------------------------------------------
CREATE TABLE IF NOT EXISTS recommendations (
  id               BIGINT    NOT NULL AUTO_INCREMENT,
  user_id          INT       NOT NULL COMMENT 'users.id',
  movie_id         INT       NOT NULL COMMENT 'movies.movie_id',
  predicted_rating FLOAT     NOT NULL COMMENT 'ALS 预测评分',
  `rank`           SMALLINT  NOT NULL COMMENT '该用户下的推荐排名，1 最高',
  generated_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '批次时间',
  PRIMARY KEY (id),
  INDEX idx_user_rank (user_id, `rank`),
  INDEX idx_rec_movie (movie_id)   -- 无外键约束，由应用层保证一致性
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ALS 推荐结果';
