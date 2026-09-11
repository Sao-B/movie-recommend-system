-- ============================================================
-- 个性化电影推荐系统 — MySQL 建库建表 DDL
-- 执行方式: mysql -u root -p
--          SOURCE d:/Sundries/Paper/Project/sql/schema.sql;
-- ============================================================

SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS movie_recommend
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_unicode_ci;

USE movie_recommend;

-- ============================================================
-- 表 1: users - 系统注册用户
-- ============================================================
-- 作用：存储通过前端注册的应用用户，支持登录认证
--
-- 设计要点：
--   - id 为自增正整数，范围 [1, ∞)
--   - 与 MovieLens 用户隔离：MovieLens 用户 ID 在训练时取负，成为负整数
--   - 密码使用 bcrypt 加密存储
--
-- 关联表：
--   - user_ratings.user_id 外键引用 users.id
--   - recommendations.user_id 对应 users.id（无外键约束，因批量导入性能考虑）
--
-- 查询场景：
--   - 用户登录验证
--   - 获取系统用户列表（供训练脚本过滤）
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
  id           INT          NOT NULL AUTO_INCREMENT COMMENT '用户主键，自增正整数',
  username     VARCHAR(50)  NOT NULL UNIQUE         COMMENT '用户名，唯一',
  password_hash VARCHAR(255) NOT NULL                COMMENT 'bcrypt 哈希密码',
  created_at   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
  role         ENUM('user', 'admin') NOT NULL DEFAULT 'user' COMMENT '用户角色：user=普通用户, admin=管理员',
  PRIMARY KEY (id),
  INDEX idx_username (username),
  INDEX idx_user_role (role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统注册用户表';

-- ============================================================
-- 表 2: movies - 电影元数据
-- ============================================================
-- 作用：存储电影的基本信息，从 MovieLens movies.csv 预处理后导入
--
-- 设计要点：
--   - movie_id 使用 MovieLens 原始 ID，保持一致性
--   - title 包含年份信息，如 "Toy Story (1995)"
--   - genres 用竖线分隔，如 "Adventure|Animation|Children|Comedy|Fantasy"
--   - year 从 title 正则提取，便于按年份筛选
--
-- 数据来源：
--   - Spark 脚本 preprocess.py 从 HDFS 读取 movies.csv 并清洗后写入
--   - 约 86,000 条记录
--
-- 关联表：
--   - user_ratings.movie_id 引用 movies.movie_id
--   - recommendations.movie_id 引用 movies.movie_id
--   - model_item_factors.movie_id 引用 movies.movie_id
--
-- 查询场景：
--   - 前端电影列表展示（支持标题、类型、年份筛选）
--   - 推荐结果关联查询电影详情
-- ============================================================
CREATE TABLE IF NOT EXISTS movies (
  movie_id      INT          NOT NULL COMMENT 'MovieLens 原始电影 ID',
  title         VARCHAR(500) NOT NULL COMMENT '电影标题（含年份），如 "Toy Story (1995)"',
  genres        VARCHAR(500)          COMMENT '类型，竖线分隔，如 "Action|Drama"',
  year          SMALLINT              COMMENT '上映年份，从 title 正则解析',
  avg_rating    DECIMAL(3,2)          COMMENT '平均评分（0.00-5.00）',
  rating_count  INT          DEFAULT 0 COMMENT '评分人数',
  poster_url    VARCHAR(500)          COMMENT 'TMDB 海报 URL',
  overview      TEXT                  COMMENT '电影简介',
  director      VARCHAR(500)          COMMENT '导演',
  writers       TEXT                  COMMENT '编剧（多个用逗号分隔）',
  `cast`        TEXT                  COMMENT '主演（多个用逗号分隔）',
  countries     VARCHAR(200)          COMMENT '制片国家/地区',
  languages     VARCHAR(200)          COMMENT '语言',
  release_date  DATE                  COMMENT '上映日期',
  runtime       SMALLINT              COMMENT '片长（分钟）',
  PRIMARY KEY (movie_id),
  INDEX idx_genres (genres(100)),
  INDEX idx_year (year),
  INDEX idx_rating_count (rating_count)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='电影元数据表';

-- ============================================================
-- 表 3: ratings - 遗留评分表（已废弃）
-- ============================================================
-- 作用：保留历史评分数据，但不再由应用或训练任务读写
--
-- 废弃原因：
--   - 应用评分已迁移到 user_ratings 表
--   - 训练时直接从 HDFS 读取 MovieLens 评分，从 user_ratings 读取系统用户评分
--   - 保留此表是为了数据兼容性，可在确认无需后删除
--
-- 注意：
--   - 新代码不应操作此表
--   - 如需清理，可执行 DROP TABLE ratings;
-- ============================================================
CREATE TABLE IF NOT EXISTS ratings (
  id        BIGINT    NOT NULL AUTO_INCREMENT,
  user_id   INT       NOT NULL COMMENT '对应 users.id 或 MovieLens 用户 ID',
  movie_id  INT       NOT NULL COMMENT '对应 movies.movie_id',
  rating    FLOAT     NOT NULL COMMENT '评分值 0.5~5.0' CHECK (rating >= 0.5 AND rating <= 5.0),
  rated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_movie (user_id, movie_id),
  INDEX idx_user   (user_id),
  INDEX idx_movie  (movie_id),
  CONSTRAINT fk_ratings_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
  CONSTRAINT fk_ratings_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='遗留评分数据（已废弃，不再使用）';

-- ============================================================
-- 表 4: user_ratings - 系统用户评分
-- ============================================================
-- 作用：存储系统注册用户的评分记录，用于应用评分功能和 ALS 训练
--
-- 设计要点：
--   - 仅存储 users 表中真实注册用户的评分
--   - user_id 外键约束 users.id，保证数据完整性
--   - 唯一索引 uk_user_movie 保证用户对同一电影只能有一条评分记录
--   - 评分范围 [0.5, 5.0]，步长 0.5（与 MovieLens 一致）
--
-- 数据流向：
--   - 写入：前端用户提交评分 → Flask API → user_ratings
--   - 读取：
--     1. Flask API 查询用户历史评分
--     2. Spark 训练脚本读取并合并到训练集
--     3. Flask 在线推荐读取用户最新评分
--
-- 与 MovieLens 评分的区别：
--   - MovieLens 评分：3300 万条，存储在 HDFS，训练时读取并取负 user_id
--   - 系统用户评分：数量较少，存储在 MySQL，训练时直接使用 user_id
--
-- 查询场景：
--   - 前端展示用户评分历史
--   - 在线推荐计算用户向量（需要最新评分）
--   - 离线训练合并系统用户评分到训练集
-- ============================================================
CREATE TABLE IF NOT EXISTS user_ratings (
  id        BIGINT    NOT NULL AUTO_INCREMENT COMMENT '评分记录主键',
  user_id   INT       NOT NULL COMMENT '用户 ID，对应 users.id',
  movie_id  INT       NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  rating    FLOAT     NOT NULL COMMENT '评分值，范围 [0.5, 5.0]，步长 0.5' CHECK (rating >= 0.5 AND rating <= 5.0),
  rated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '评分时间',
  PRIMARY KEY (id),
  UNIQUE KEY uk_user_movie (user_id, movie_id) COMMENT '同一用户对同一电影只能评分一次',
  INDEX idx_user   (user_id)  COMMENT '按用户查询评分历史',
  INDEX idx_movie  (movie_id) COMMENT '按电影查询评分统计',
  CONSTRAINT fk_user_ratings_user FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE COMMENT '用户删除时级联删除其评分'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统注册用户评分表';

-- ============================================================
-- 表 5: model_versions - ALS 模型版本管理
-- ============================================================
-- 作用：管理 ALS 模型的生命周期，记录训练参数和状态
--
-- 模型版本命名规则：
--   - 格式：als-{timestamp}-{random_suffix}
--   - 示例：als-20260909T120000Z-abc123
--
-- 状态流转：
--   building → active → retired
--     |         |         |
--   训练中   当前生效   已废弃
--
-- 状态说明：
--   - building：模型正在训练，电影因子和推荐结果正在写入，不对外服务
--   - active：训练完成，在线推荐使用该版本的电影因子，批量推荐来自该版本
--   - retired：被新模型替换，保留一段时间后可清理
--
-- 设计要点：
--   - 同时只能有一个 active 模型
--   - 训练脚本在写入因子和推荐前创建 building 记录
--   - 写入完成后调用 activate_model_version() 将状态改为 active
--   - 旧的 active 模型自动变为 retired
--
-- 关联表：
--   - model_item_factors.model_version 外键引用 model_versions.version
--   - recommendations.model_version 关联 model_versions.version（无外键约束）
--
-- 查询场景：
--   - 在线推荐获取当前 active 模型版本
--   - 训练脚本检查模型状态
--   - 运维清理 retired 模型
-- ============================================================
CREATE TABLE IF NOT EXISTS model_versions (
  version       VARCHAR(64) NOT NULL COMMENT '模型版本号，格式 als-{timestamp}-{suffix}',
  `rank`        SMALLINT    NOT NULL COMMENT 'ALS 隐因子维度，如 50',
  reg_param     FLOAT       NOT NULL COMMENT 'ALS 正则化系数，如 0.1',
  status        ENUM('building', 'active', 'retired') NOT NULL DEFAULT 'building'
                COMMENT '模型状态：building=训练中, active=当前生效, retired=已废弃',
  generated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '模型创建时间',
  activated_at  TIMESTAMP   NULL     COMMENT '模型激活时间（状态变为 active 时记录）',
  PRIMARY KEY (version),
  INDEX idx_model_status (status, generated_at) COMMENT '按状态和时间查询模型'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ALS 模型版本管理表';

-- ============================================================
-- 表 6: model_item_factors - ALS 电影隐因子
-- ============================================================
-- 作用：存储 ALS 训练得到的电影隐向量，供在线推荐使用
--
-- 设计要点：
--   - 每个 (model_version, movie_id) 对应一个电影隐向量
--   - factors 存储为 JSON 数组字符串，如 "[0.12, -0.34, 0.56, ...]"
--   - 向量维度由 model_versions.rank 决定（如 rank=50，则向量长度为 50）
--
-- 数据来源：
--   - Spark 训练脚本 train_als.py 在模型训练完成后写入
--   - 提取 ALS model.itemFactors，转为 JSON 字符串后批量写入
--
-- 数据使用：
--   - Flask 在线推荐服务加载 active 模型的电影因子
--   - 用户新增评分后，固定电影因子，求解用户向量
--   - 计算用户向量与所有电影因子的内积，得到预测评分
--
-- 存储格式示例：
--   model_version: "als-20260909T120000Z-abc123"
--   movie_id: 1
--   factors: "[0.123, -0.456, 0.789, ..., 0.234]"  (50 维)
--
-- 性能优化：
--   - 主键 (model_version, movie_id) 保证查询效率
--   - Flask 服务启动时一次性加载 active 模型的所有因子到内存
--   - 缓存后避免频繁查询数据库
--
-- 查询场景：
--   - 在线推荐加载当前 active 模型的所有电影因子
--   - 训练完成后写入新模型的电影因子
-- ============================================================
CREATE TABLE IF NOT EXISTS model_item_factors (
  model_version VARCHAR(64) NOT NULL COMMENT '模型版本号，引用 model_versions.version',
  movie_id      INT         NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  factors       TEXT        NOT NULL COMMENT 'JSON 数组格式的电影隐向量，如 "[0.12, -0.34, ...]"',
  generated_at  TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '因子生成时间',
  PRIMARY KEY (model_version, movie_id),
  INDEX idx_factor_version (model_version) COMMENT '按模型版本查询所有电影因子',
  CONSTRAINT fk_item_factors_version FOREIGN KEY (model_version)
    REFERENCES model_versions(version)
    ON DELETE CASCADE COMMENT '模型删除时级联删除其电影因子'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ALS 电影隐因子表（供在线推荐使用）';

-- ============================================================
-- 表 7: recommendations - 批量推荐结果
-- ============================================================
-- 作用：存储 ALS 离线训练生成的批量推荐结果
--
-- 设计要点：
--   - 仅保存系统用户（users 表）的推荐，不保存 MovieLens 用户推荐
--   - 每个用户保存 Top-N（如 20）条推荐
--   - rank 字段表示推荐排名，1 为最推荐
--
-- 数据来源：
--   - Spark 训练脚本在模型训练完成后生成
--   - 调用 model.recommendForAllUsers(N)
--   - 过滤只保留 user_id > 0 的系统用户推荐
--
-- 数据使用：
--   - 用户无评分或评分很少时，返回批量推荐作为降级方案
--   - 用户有足够评分时，优先使用在线推荐（实时计算）
--
-- 推荐策略：
--   - 在线推荐优先：用户新增评分后，立即计算更新的推荐
--   - 批量推荐降级：用户无评分或在线推荐失败时，返回批量推荐
--
-- 数据更新：
--   - 每次训练生成新模型时，写入新版本的批量推荐
--   - 旧版本推荐可保留或清理（根据 model_version 状态）
--
-- 查询场景：
--   - 前端请求推荐时，Flask 优先计算在线推荐
--   - 在线推荐失败时，查询 recommendations 表返回批量推荐
--   - 查询条件：WHERE user_id = ? ORDER BY rank LIMIT 20
-- ============================================================
CREATE TABLE IF NOT EXISTS recommendations (
  id               BIGINT      NOT NULL AUTO_INCREMENT COMMENT '推荐记录主键',
  model_version    VARCHAR(64) NULL     COMMENT '生成推荐的模型版本号，引用 model_versions.version',
  user_id          INT         NOT NULL COMMENT '用户 ID，对应 users.id',
  movie_id         INT         NOT NULL COMMENT '推荐的电影 ID，对应 movies.movie_id',
  predicted_rating FLOAT       NOT NULL COMMENT 'ALS 预测评分',
  `rank`           SMALLINT    NOT NULL COMMENT '该用户的推荐排名，1=最推荐，2=次之，...',
  generated_at     TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '推荐批次生成时间',
  PRIMARY KEY (id),
  INDEX idx_model_user_rank (model_version, user_id, `rank`) COMMENT '按模型版本和用户查询推荐',
  INDEX idx_user_rank (user_id, `rank`) COMMENT '按用户查询 Top-N 推荐',
  INDEX idx_rec_movie (movie_id) COMMENT '按电影查询被推荐给哪些用户'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ALS 批量推荐结果表';

-- ============================================================
-- 表 8: movie_comments - 电影评论
-- ============================================================
-- 作用：存储用户对电影的文字评论
--
-- 设计要点：
--   - 用户可以对每部电影发表多条评论
--   - created_at 建立索引以支持按时间倒序查询
--   - 支持软删除（通过删除记录实现）
--
-- 数据流向：
--   - 写入：前端用户提交评论 → Flask API → movie_comments
--   - 读取：前端查询电影详情页的评论列表（分页）
--
-- 查询场景：
--   - 按电影 ID 分页查询评论列表
--   - 用户删除自己的评论
--   - 关联查询用户对该电影的评分（显示星级）
-- ============================================================
CREATE TABLE IF NOT EXISTS movie_comments (
  id          BIGINT    NOT NULL AUTO_INCREMENT COMMENT '评论主键',
  user_id     INT       NOT NULL COMMENT '用户 ID，对应 users.id',
  movie_id    INT       NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  content     TEXT      NOT NULL COMMENT '评论内容',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发表时间',
  updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  PRIMARY KEY (id),
  INDEX idx_movie_created (movie_id, created_at DESC) COMMENT '按电影查询评论，按时间倒序',
  INDEX idx_user (user_id) COMMENT '按用户查询评论',
  CONSTRAINT fk_comments_user FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE COMMENT '用户删除时级联删除其评论',
  CONSTRAINT fk_comments_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    ON DELETE CASCADE COMMENT '电影删除时级联删除其评论'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='电影评论表';
