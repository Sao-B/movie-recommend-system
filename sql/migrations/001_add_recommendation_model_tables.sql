-- 非破坏性迁移：创建应用评分和 ALS 模型服务表。
-- 执行前请备份数据库。本脚本不会复制或删除遗留 ratings 数据。

USE movie_recommend;

CREATE TABLE IF NOT EXISTS user_ratings (
  id        BIGINT    NOT NULL AUTO_INCREMENT,
  user_id   INT       NOT NULL,
  movie_id  INT       NOT NULL,
  rating    FLOAT     NOT NULL CHECK (rating >= 0.5 AND rating <= 5.0),
  rated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_movie (user_id, movie_id),
  INDEX idx_user (user_id),
  INDEX idx_movie (movie_id),
  CONSTRAINT fk_user_ratings_user FOREIGN KEY (user_id) REFERENCES users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS model_versions (
  version       VARCHAR(64) NOT NULL,
  `rank`        SMALLINT    NOT NULL,
  reg_param     FLOAT       NOT NULL,
  status        ENUM('building', 'active', 'retired') NOT NULL DEFAULT 'building',
  generated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  activated_at  TIMESTAMP   NULL,
  PRIMARY KEY (version),
  INDEX idx_model_status (status, generated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS model_item_factors (
  model_version VARCHAR(64) NOT NULL,
  movie_id      INT         NOT NULL,
  factors       TEXT        NOT NULL,
  generated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (model_version, movie_id),
  INDEX idx_factor_version (model_version),
  CONSTRAINT fk_item_factors_version FOREIGN KEY (model_version)
    REFERENCES model_versions(version)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Existing recommendation rows keep a NULL model version. The checks make
-- this migration safe to re-run after an interrupted deployment.
SET @has_model_version_column = (
  SELECT COUNT(*)
  FROM information_schema.columns
  WHERE table_schema = DATABASE()
    AND table_name = 'recommendations'
    AND column_name = 'model_version'
);
SET @sql = IF(
  @has_model_version_column = 0,
  'ALTER TABLE recommendations ADD COLUMN model_version VARCHAR(64) NULL COMMENT ''generated model version'' AFTER id',
  'SELECT 1'
);
PREPARE migration_stmt FROM @sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;

SET @has_model_version_index = (
  SELECT COUNT(*)
  FROM information_schema.statistics
  WHERE table_schema = DATABASE()
    AND table_name = 'recommendations'
    AND index_name = 'idx_model_user_rank'
);
SET @sql = IF(
  @has_model_version_index = 0,
  'ALTER TABLE recommendations ADD INDEX idx_model_user_rank (model_version, user_id, `rank`)',
  'SELECT 1'
);
PREPARE migration_stmt FROM @sql;
EXECUTE migration_stmt;
DEALLOCATE PREPARE migration_stmt;
