-- ============================================================
-- 迁移脚本 011: users 增加 nickname（昵称）
-- 执行时间: 2026-09-18
-- 目的: 把「账号」和「显示名」拆开——账号只用于登录与标识，注册时按命名规则
--       定下来不再改；个人主页能改的是昵称，任意字符、不唯一、可清空。
-- 背景: 之前 username 一个字段兼任两者，个人主页改的就是它，于是一旦允许中文昵称
--       就会把登录凭据改乱；拆出 nickname 后两边各归各位。
-- ============================================================
USE movie_recommend;

ALTER TABLE users
  ADD COLUMN nickname VARCHAR(50) NULL DEFAULT NULL
    COMMENT '昵称，展示用，任意字符、不唯一；为空时回落显示 username（migrations/011 新增）'
    AFTER bio;

-- 存量数据：昵称初始化成账号名，避免老用户显示名空掉
UPDATE users SET nickname = username WHERE nickname IS NULL;

-- 验证
SHOW CREATE TABLE users;
SELECT id, username, nickname FROM users;
