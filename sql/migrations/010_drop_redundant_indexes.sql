-- ============================================================
-- 010_drop_redundant_indexes.sql — 删掉被最左前缀覆盖的冗余索引
-- 适用：任何已建库（跑完即与 schema.sql 对齐）
-- ============================================================
-- 背景：
--   user_ratings       UNIQUE uk_user_movie (user_id, movie_id) 的最左前缀已覆盖 idx_user (user_id)
--   model_item_factors PRIMARY KEY (model_version, movie_id)    的最左前缀已覆盖 idx_factor_version (model_version)
--   同一条规矩此前已用于 movies（去掉 idx_rating_count）与 users（去掉 idx_username），
--   这两处是漏网的。两个索引对应列上的外键仍由上述主键/唯一键提供支撑，删掉不影响约束。
--
-- ⚠️ 不可重复执行：索引已不存在时会报 1091（Can't DROP ...; check that column/key exists），
--    这与 009 的行为一致，属预期。回滚就是把两条 INDEX 重新加回去。
-- ============================================================

USE movie_recommend;

ALTER TABLE user_ratings DROP INDEX idx_user;
ALTER TABLE model_item_factors DROP INDEX idx_factor_version;
