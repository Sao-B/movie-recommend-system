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
-- 作用：存储通过前端注册的应用用户，支持登录认证与管理后台权限控制
--
-- 设计要点：
--   - id 为自增正整数，范围 [1, ∞)
--   - 与 MovieLens 用户隔离：MovieLens 用户 ID 在训练时取负，成为负整数
--   - 密码用 werkzeug.security.generate_password_hash 存储，校验用 check_password_hash
--     （werkzeug 3.x 的默认算法是 scrypt，已移除 bcrypt 支持）
--     注意：migrations/001_add_user_role.sql 插入的默认 admin 哈希必须由
--     generate_password_hash 生成；若写成 bcrypt 会抛 ValueError、登录返回 500
--
-- 关联表：
--   - user_ratings.user_id 外键引用 users.id（ON DELETE CASCADE）
--   - movie_comments.user_id 外键引用 users.id（ON DELETE CASCADE）
--   - recommendations.user_id 对应 users.id（无外键约束，因批量导入性能考虑）
--
-- 查询场景：
--   - 用户登录验证
--   - 获取系统用户列表（供训练脚本过滤）
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
  id           INT          NOT NULL AUTO_INCREMENT COMMENT '用户主键，自增正整数',
  username     VARCHAR(50)  NOT NULL UNIQUE         COMMENT '用户名，唯一',
  password_hash VARCHAR(255) NOT NULL                COMMENT '密码哈希（werkzeug generate_password_hash / pbkdf2）',
  avatar_url   VARCHAR(500)          NULL            COMMENT '用户头像 URL（migrations/004 新增）',
  bio          TEXT                  NULL            COMMENT '个人签名/简介（migrations/004 新增）',
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
--   - Spark 脚本 preprocess.py 从 HDFS 读取 movies.csv，清洗后经 movies_staging
--     暂存、upsert 写入基础四列（movie_id / title / genres / year），可重复执行
--   - import_movie_stats.py、sync_movie_stats.py 回填 avg_rating、rating_count
--   - fetch_posters.py 按 TMDB API 回填 poster_url、overview、director、writers 等富化列
--   - 当前约 87,585 条记录
--
-- 关联表（外键约束）：
--   - movie_comments.movie_id 外键引用 movies.movie_id（ON DELETE CASCADE）
--   - movie_rating_stats.movie_id 外键引用 movies.movie_id（ON DELETE CASCADE）
-- 仅按 ID 关联、没有外键约束：
--   - user_ratings.movie_id、model_item_factors.movie_id、recommendations.movie_id
--     （批量导入与查询性能考虑，不建外键，需由写入方保证 ID 有效）
-- 注意：preprocess.py 不再 TRUNCATE 本表（2026-09-16 起改走 movies_staging
--       暂存 + upsert），外键不会再挡住预处理刷新。
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
-- 表 3: user_ratings - 系统用户评分
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
  -- 用户删除时级联删除其评分（注：MySQL 不支持在 FOREIGN KEY 子句后写 COMMENT，故用普通注释）
  CONSTRAINT fk_user_ratings_user FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统注册用户评分表';

-- ============================================================
-- 表 4: model_versions - ALS 模型版本管理
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
-- 表 5: model_item_factors - ALS 电影隐因子
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
  -- 模型删除时级联删除其电影因子（FK 子句后不能写 COMMENT）
  CONSTRAINT fk_item_factors_version FOREIGN KEY (model_version)
    REFERENCES model_versions(version)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='ALS 电影隐因子表（供在线推荐使用）';

-- ============================================================
-- 表 6: recommendations - 批量推荐结果
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
-- 表 7: movie_comments - 电影评论
-- ============================================================
-- 作用：存储用户对电影的文字评论
--
-- 设计要点：
--   - **同一用户对同一电影只保留一条影评**：再次发表即更新正文，不追加新行
--     （与 user_ratings 的「一人一片一条」一致，避免反复点按钮堆出重复评论）。
--     该规则由应用层保证（backend/app/routes/comments.py），表上没有唯一索引。
--   - created_at 建立索引以支持按时间倒序查询
--   - edited_at 只在用户真正改过正文时写入。**不要用 updated_at 判断「是否编辑过」**：
--     那一列在插入时就会被写成发表时刻，用它做判据会把所有影评都显示成已修改。
--   - 删除为物理删除（DELETE /api/comments/<id>），没有软删除标记
--   - **可见性不存字段**：一条影评是否公开展示，取决于作者此刻有没有给这部电影
--     打分（user_ratings 里有没有对应那一行）。作者没有评分行时（例如在个人中心
--     「我的评分」里删掉了），他的影评随之「仅自己可见」；重新评分（>= 0.5）后
--     自动重新公开。
--     从评分推导可见性是单一事实来源，不会出现两份状态互相打架。
--
-- 数据流向：
--   - 写入：前端用户提交影评 → Flask API（POST 发表/更新、PUT 编辑）→ movie_comments
--   - 读取：前端查询电影详情页的影评列表（分页；另单独取当前用户那条用于置顶展示）
--
-- 查询场景：
--   - 按电影 ID 分页查询影评列表（LEFT JOIN user_ratings 过滤出公开的影评）
--   - 取当前用户在该电影下的影评（置顶卡片 + 编辑）
--   - 用户编辑或删除自己的影评
--   - 关联查询作者对该电影的评分（显示星级）
-- ============================================================
CREATE TABLE IF NOT EXISTS movie_comments (
  id          BIGINT    NOT NULL AUTO_INCREMENT COMMENT '评论主键',
  user_id     INT       NOT NULL COMMENT '用户 ID，对应 users.id',
  movie_id    INT       NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  content     TEXT      NOT NULL COMMENT '评论内容',
  created_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '发表时间',
  updated_at  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '行最后写入时间（插入时即等于发表时间，别用它判断是否编辑过）',
  edited_at   TIMESTAMP NULL     DEFAULT NULL COMMENT '用户手动编辑正文的时间；从未编辑过为 NULL（migrations/007 新增）',
  PRIMARY KEY (id),
  INDEX idx_movie_created (movie_id, created_at DESC) COMMENT '按电影查询评论，按时间倒序',
  INDEX idx_user (user_id) COMMENT '按用户查询评论',
  -- 用户删除时级联删除其评论（FK 子句后不能写 COMMENT）
  CONSTRAINT fk_comments_user FOREIGN KEY (user_id) REFERENCES users(id)
    ON DELETE CASCADE,
  -- 电影删除时级联删除其评论
  CONSTRAINT fk_comments_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='电影评论表';

-- ============================================================
-- 表 8: movie_rating_stats - MovieLens 评分五档分布（离线聚合）
-- ============================================================
-- 作用：存放 MovieLens 全量评分的五档分布，供详情页「评分统计」渲染分布柱
--
-- 为什么需要：
--   - movies 表只有 avg_rating / rating_count 两个标量，没有分档计数
--   - 详情页要显示 5/4/3/2/1 星各占多少，必须另有分档数据
--
-- 数据来源：
--   - scripts/build_ml_rating_stats.py 流式聚合 data/raw/ratings.csv（3200 万条）
--     一次性写入，不需随 ALS 训练重算；档位规则与 backend/app/rating_stats.py 的
--     rating_to_star_bucket 完全一致（四舍五入，4.5 归 5 档）
--   - 当前约 84,432 行（= MovieLens 中有评分的电影数）
--
-- 数据使用：
--   - GET /api/movies/<id>/rating-stats 把本表与 user_ratings 合并后返回，
--     页面上不区分来源
--   - rating_sum 用于与本站评分一起做精确加权平均（avg_rating 只有 2 位小数）
--
-- 注意：本表依赖 movies 行存在（有外键）。重载 movies 后必须重跑
--       build_ml_rating_stats.py 重建本表。
-- ============================================================
CREATE TABLE IF NOT EXISTS movie_rating_stats (
  movie_id     INT           NOT NULL COMMENT '电影 ID，对应 movies.movie_id',
  rating_count INT           NOT NULL COMMENT 'MovieLens 评分总条数',
  rating_sum   DECIMAL(14,1) NOT NULL COMMENT 'MovieLens 评分总和（0.5 粒度，1 位小数即精确）',
  cnt_1        INT           NOT NULL DEFAULT 0 COMMENT '1 星档条数',
  cnt_2        INT           NOT NULL DEFAULT 0 COMMENT '2 星档条数',
  cnt_3        INT           NOT NULL DEFAULT 0 COMMENT '3 星档条数',
  cnt_4        INT           NOT NULL DEFAULT 0 COMMENT '4 星档条数',
  cnt_5        INT           NOT NULL DEFAULT 0 COMMENT '5 星档条数',
  PRIMARY KEY (movie_id),
  CONSTRAINT fk_mrs_movie FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
    ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='MovieLens 评分五档分布（离线聚合，与本站评分合并展示）';
