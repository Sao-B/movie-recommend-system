-- ============================================================
-- 迁移脚本 013: 修掉四个列注释 + 两个表注释的乱码
-- 执行时间: 2026-09-18
-- 目的: 当年建库时用错连接字符集，中文注释被按 GBK 解了一遍存进库里，
--       现在读出来是「鐢ㄦ埛鍚」这种乱码，与 sql/schema.sql 里的注释对不上。
--       这里按 schema.sql 的原文重写这几个注释，**只改注释**。
-- 涉及:
--   users.username / users.role / users 表注释
--   movies.title  / movies.genres / movies 表注释
-- 注意: MODIFY COLUMN 会把整列定义重写一遍，类型 / 可空 / 默认值必须原样写回；
--       username 的 UNIQUE 是独立索引，不受 MODIFY 影响（执行后会再查一遍确认）。
-- ============================================================
USE movie_recommend;

ALTER TABLE users
  MODIFY COLUMN username VARCHAR(50) NOT NULL
    COMMENT '账号，唯一；注册时按 app/account_rules.py 的规则命名（3-20 位字母/数字/下划线/连字符），登录可用它或 id',
  MODIFY COLUMN role ENUM('user','admin') NOT NULL DEFAULT 'user'
    COMMENT '用户角色：user=普通用户, admin=管理员',
  COMMENT='系统注册用户表';

ALTER TABLE movies
  MODIFY COLUMN title VARCHAR(500) NOT NULL
    COMMENT '电影标题（含年份），如 "Toy Story (1995)"',
  MODIFY COLUMN genres VARCHAR(500) NULL
    COMMENT '类型，竖线分隔，如 "Action|Drama"',
  COMMENT='电影元数据表';

-- 验证
SELECT TABLE_NAME, TABLE_COMMENT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN ('users', 'movies');

SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND ((TABLE_NAME = 'users'  AND COLUMN_NAME IN ('username', 'role'))
    OR (TABLE_NAME = 'movies' AND COLUMN_NAME IN ('title', 'genres')));

-- 确认 username 的唯一索引还在
SELECT INDEX_NAME, NON_UNIQUE, COLUMN_NAME
FROM information_schema.STATISTICS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'username';
