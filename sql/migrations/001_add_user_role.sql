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
-- 哈希由 backend 同款依赖 werkzeug.security.generate_password_hash 生成（scrypt，默认参数），
-- 与 backend/app/models.py 的 check_password_hash 一致。切勿改回 bcrypt：
-- werkzeug 3.x 已移除 bcrypt 支持，校验时会抛 ValueError: Invalid hash method，登录返回 500。
INSERT INTO users (username, password_hash, role)
VALUES ('admin', 'scrypt:32768:8:1$mgXZbl7GIzDx3QIU$aec2c02b5b68e8dd7c4d546c80f4fb51a480f405e120df8fbe5d872257f34d225c1ba9f6804712264a5707434d2187af856de390e387074ea2dd02d44809143a', 'admin')
ON DUPLICATE KEY UPDATE role = 'admin';
