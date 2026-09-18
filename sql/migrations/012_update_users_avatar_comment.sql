-- ============================================================
-- 迁移脚本 012: users.avatar_url 的列注释跟着「内置 SVG 头像」改写
-- 执行时间: 2026-09-18
-- 目的: 头像来源变多了（内置 SVG / 历史上传 / 早期外链 / NULL＝默认），
--       列注释要与 schema.sql 说的一致：只改 COMMENT，不动类型与可空性。
-- 说明: MySQL 没有单独的「改注释」语法，只能 MODIFY COLUMN 把整列定义重写一遍，
--       所以 VARCHAR(500) NULL 必须原样写回来，漏一个就会顺手改掉列定义。
-- ============================================================
USE movie_recommend;

ALTER TABLE users
  MODIFY COLUMN avatar_url VARCHAR(500) NULL
    COMMENT '头像地址：内置 SVG /api/profile/avatars/<名>.svg、历史上传 /api/profile/avatar/<文件名> 或早期外链；NULL＝默认头像（显示名首字符）';

-- 验证（应能看到上面这段注释，且类型仍是 varchar(500)、可空）
SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'users' AND COLUMN_NAME = 'avatar_url';
