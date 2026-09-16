-- ============================================================
-- 迁移脚本 007: movie_comments 增加 edited_at
-- 执行时间: 2026-09-15
-- 目的: 用户可编辑自己的影评，详情页需要显示「上次修改于 …」
-- ============================================================
USE movie_recommend;

-- 为什么不复用 updated_at：
--   updated_at 是「行最后写入时间」，插入时就会被写成发表时刻，
--   用它判断「是否编辑过」会把所有影评都显示成已修改。
--   edited_at 只在用户真正改正文时写入，从未编辑过的行保持 NULL。
ALTER TABLE movie_comments
  ADD COLUMN edited_at TIMESTAMP NULL DEFAULT NULL
    COMMENT '用户手动编辑正文的时间；从未编辑过为 NULL（migrations/007 新增）'
    AFTER updated_at;

-- 验证
SHOW CREATE TABLE movie_comments;
