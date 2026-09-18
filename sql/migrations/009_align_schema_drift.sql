-- ============================================================
-- 迁移脚本 009: 把旧库对齐到当前 schema.sql
-- 执行时间: 2026-09-16
-- 目的: 2026-09-16 之前的库与 schema.sql 之间存在若干漂移，本脚本一次性拉平，
--       使「沿迁移链升级上来的库」与「按 schema.sql 新建的库」结构完全一致。
--
-- 适用范围：**仅** 2026-09-16 之前的旧库。
--   线上库 movie_recommend 已于当天直接对齐完毕，**不需要执行本脚本**。
--   本脚本不可重复执行——索引/约束已存在或已不存在时会报错（1061 / 1091 / 1025 等），
--   这与 001 的行为一致，属预期。
--
-- 漂移清单（逐条对应下面的段落）:
--   1. movies.writers / countries / languages 仍是 TEXT / VARCHAR(200)
--      —— countries 实测最长 258 字符，会让 fetch_posters.py 撞 "Data too long"
--   2. movies 缺 idx_popularity（热门排序真正依赖的索引），却多了冗余的 idx_rating_count
--   3. users 多一个与唯一索引同列的普通索引 idx_username
--   4. movie_comments 复合索引名 idx_movie_created（统一为 idx_movie_time）
--   5. movie_comments 外键名 fk_comments_*（统一为 fk_comment_*）
--   6. recommendations 的 movie_id 索引名 fk_rec_movie（该表并没有外键，前缀误导；统一为 idx_rec_movie）
--   7. user_ratings.user_id 与 model_item_factors.model_version 的外键缺 ON DELETE CASCADE，
--      与 schema.sql 及文档描述的「用户/模型删除时级联清理」不一致
-- ============================================================
USE movie_recommend;

-- 1. 加宽 TMDB 富化列（与 fetch_posters.py 的 cut(limit=500) 对齐）
ALTER TABLE movies
  MODIFY writers   VARCHAR(500) COMMENT '编剧（多个用逗号分隔）',
  MODIFY countries VARCHAR(500) COMMENT '制片国家/地区',
  MODIFY languages VARCHAR(500) COMMENT '语言';

-- 2. movies 索引对齐：补上热门排序实际用的复合索引，去掉被它覆盖的冗余索引
ALTER TABLE movies
  ADD INDEX idx_popularity (rating_count DESC, avg_rating DESC) COMMENT '热门排序（首页与电影库默认排序）';
ALTER TABLE movies DROP INDEX idx_rating_count;

-- 3. users：username 的唯一索引由列级 UNIQUE 隐式创建，普通索引是多余的
ALTER TABLE users DROP INDEX idx_username;

-- 4 + 5. movie_comments：索引与外键改名（列与动作不变）
ALTER TABLE movie_comments RENAME INDEX idx_movie_created TO idx_movie_time;
ALTER TABLE movie_comments DROP FOREIGN KEY fk_comments_user;
ALTER TABLE movie_comments
  ADD CONSTRAINT fk_comment_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE movie_comments DROP FOREIGN KEY fk_comments_movie;
ALTER TABLE movie_comments
  ADD CONSTRAINT fk_comment_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id) ON DELETE CASCADE;

-- 6. recommendations：movie_id 上的索引改名（该表只按 ID 关联，没有外键约束）
ALTER TABLE recommendations RENAME INDEX fk_rec_movie TO idx_rec_movie;

-- 7. 外键补 ON DELETE CASCADE，与 schema.sql 及各文档的口径一致
ALTER TABLE user_ratings DROP FOREIGN KEY fk_user_ratings_user;
ALTER TABLE user_ratings
  ADD CONSTRAINT fk_user_ratings_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
ALTER TABLE model_item_factors DROP FOREIGN KEY fk_item_factors_version;
ALTER TABLE model_item_factors
  ADD CONSTRAINT fk_item_factors_version FOREIGN KEY (model_version)
    REFERENCES model_versions(version) ON DELETE CASCADE;

-- 验证：结构应与 schema.sql 完全一致
SHOW CREATE TABLE movies;
SHOW CREATE TABLE movie_comments;
SELECT TABLE_NAME, CONSTRAINT_NAME, DELETE_RULE
  FROM information_schema.REFERENTIAL_CONSTRAINTS
  WHERE CONSTRAINT_SCHEMA = DATABASE()
  ORDER BY TABLE_NAME;
