-- ============================================================
-- 迁移脚本 002: 创建 movie_comments 表
-- 执行时间: 2024-01-XX
-- 目的: 支持用户对电影发表评论
--
-- 注（2026-09-16）：本脚本建立的 idx_movie_created 与 fk_comments_* 已在
-- migrations/009 中分别改名为 idx_movie_time 与 fk_comment_*（列与动作不变）。
-- 此处保持历史原样不改。
-- ============================================================

USE movie_recommend;

CREATE TABLE IF NOT EXISTS movie_comments (
  id          BIGINT    NOT NULL AUTO_INCREMENT COMMENT '评论主键',
  user_id     INT       NOT NULL COMMENT '用户 ID，对应 users.id',
  movie_id    INT       NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  content     TEXT      NOT NULL COMMENT '评论内容',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发表时间',
  updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  INDEX idx_movie_created (movie_id, created_at DESC) COMMENT '按电影查询评论，按时间倒序',
  INDEX idx_user (user_id) COMMENT '按用户查询评论',
  -- 用户删除时级联删除其评论（FK 子句后不能写 COMMENT，否则 MySQL 8.0 报 1064）
  CONSTRAINT fk_comments_user FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE,
  -- 电影删除时级联删除其评论
  CONSTRAINT fk_comments_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='电影评论表';

-- 验证
SHOW CREATE TABLE movie_comments;
