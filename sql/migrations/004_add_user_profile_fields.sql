-- ============================================================
-- 为 users 表添加个人资料字段
-- ============================================================

USE movie_recommend;

ALTER TABLE users
  ADD COLUMN avatar_url VARCHAR(500) NULL COMMENT '用户头像 URL' AFTER password_hash,
  ADD COLUMN bio TEXT NULL COMMENT '个人签名/简介' AFTER avatar_url;
