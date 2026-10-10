-- ============================================================
-- 用户类型偏好 + 不感兴趣（014）
-- favorite_genres：JSON 数组字符串，如 ["Sci-Fi","Action"]。
--   NULL  = 从未选过（触发首次弹窗）
--   []    = 明确跳过过（不再弹窗）
-- user_dislikes：推荐页"不感兴趣"标记，硬排除出后续推荐。
-- ============================================================

USE movie_recommend;

ALTER TABLE users
  ADD COLUMN favorite_genres VARCHAR(255) NULL
  COMMENT '用户自选偏好类型（JSON 数组，最多 5 个；NULL=未选过，[]=已跳过）' AFTER nickname;

CREATE TABLE user_dislikes (
  id         BIGINT       NOT NULL AUTO_INCREMENT,
  user_id    INT          NOT NULL COMMENT '系统用户 ID',
  movie_id   INT          NOT NULL COMMENT '电影 ID',
  created_at DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '标记时间',
  PRIMARY KEY (id),
  UNIQUE KEY uq_user_movie (user_id, movie_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='用户标记不感兴趣的电影（推荐硬排除）';
