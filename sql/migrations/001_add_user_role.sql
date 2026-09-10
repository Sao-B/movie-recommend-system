-- ============================================================
-- 迁移脚本 001: 添加用户角色字段
-- 执行方式: mysql -u root -p movie_recommend < sql/migrations/001_add_user_role.sql
-- ============================================================

USE movie_recommend;

-- 添加用户角色字段
ALTER TABLE users
ADD COLUMN role ENUM('user', 'admin') NOT NULL DEFAULT 'user'
COMMENT '用户角色: user=普通用户, admin=管理员';

-- 添加角色索引以优化权限检查查询
CREATE INDEX idx_user_role ON users(role);

-- 创建默认管理员账号 (密码: admin123)
-- bcrypt hash 使用 rounds=12 生成
INSERT INTO users (username, password_hash, role)
VALUES ('admin', '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY5GyYqgXzb4vSC', 'admin')
ON DUPLICATE KEY UPDATE role = 'admin';
