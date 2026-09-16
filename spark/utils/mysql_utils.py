def read_df_from_mysql(spark, query_or_table, jdbc_url, user, password):
    """
    通过 JDBC 从 MySQL 读取数据，返回 Spark DataFrame。

    参数
    ----
    spark           : SparkSession
    query_or_table  : str — 表名（如 'user_ratings'）或子查询
                      传入子查询时格式须为 '(SELECT ...) AS t'
    jdbc_url        : str — JDBC 连接串（含数据库名）
    user            : str
    password        : str

    返回
    ----
    pyspark.sql.DataFrame
    """
    return (
        spark.read
        .format("jdbc")
        .option("url", jdbc_url)
        .option("dbtable", query_or_table)
        .option("user", user)
        .option("password", password)
        .option("driver", "com.mysql.cj.jdbc.Driver")
        .option("fetchsize", 10000)
        .load()
    )


def write_df_to_mysql(df, table, jdbc_url, user, password, mode="overwrite"):
    """Write a Spark DataFrame through the MySQL JDBC driver."""
    (
        df.write
        .format("jdbc")
        .option("url", jdbc_url)
        .option("dbtable", table)
        .option("user", user)
        .option("password", password)
        .option("driver", "com.mysql.cj.jdbc.Driver")
        .option("batchsize", 10000)
        .option("isolationLevel", "READ_COMMITTED")
        .option("truncate", "true")
        .mode(mode)
        .save()
    )


# 与 sql/schema.sql 中 movies 的四列基础定义保持一致（不含富化/统计列）。
# staging 是运行时暂存表，不进 schema.sql，由 preprocess.py 每次运行前重建。
_MOVIES_STAGING_DDL = """
CREATE TABLE movies_staging (
  movie_id INT PRIMARY KEY,
  title    VARCHAR(500) NOT NULL,
  genres   VARCHAR(500),
  year     SMALLINT
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
"""


def reset_movies_staging(mysql_cfg):
    """Drop and recreate the movies staging table used by preprocess.py."""
    connection = _connect(mysql_cfg)
    try:
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE IF EXISTS movies_staging")
            cursor.execute(_MOVIES_STAGING_DDL)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def upsert_movies_from_staging(mysql_cfg):
    """把 movies_staging 的清洗结果 upsert 进 movies。

    - 新电影插入；
    - 已有电影只更新 title / genres / year，
      不动 import_movie_stats.py、fetch_posters.py 维护的统计与富化列；
    - staging 中已消失的电影**保留不删**：movie_comments、movie_rating_stats
      对 movies.movie_id 有外键引用，删除会失败，只返回 id 列表由调用方告警。

    返回 (affected_rows, orphan_movie_ids)。
    """
    connection = _connect(mysql_cfg)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO movies (movie_id, title, genres, year)
                SELECT movie_id, title, genres, year FROM movies_staging
                ON DUPLICATE KEY UPDATE
                    title  = VALUES(title),
                    genres = VALUES(genres),
                    year   = VALUES(year)
                """
            )
            affected = cursor.rowcount
            cursor.execute(
                """
                SELECT m.movie_id FROM movies m
                LEFT JOIN movies_staging s ON s.movie_id = m.movie_id
                WHERE s.movie_id IS NULL
                """
            )
            orphans = [row[0] for row in cursor.fetchall()]
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    return affected, orphans


def _connect(mysql_cfg):
    import pymysql

    return pymysql.connect(
        host=mysql_cfg["host"],
        port=int(mysql_cfg["port"]),
        user=mysql_cfg["user"],
        password=mysql_cfg["password"],
        database=mysql_cfg["database"],
        charset=mysql_cfg.get("charset", "utf8mb4"),
        autocommit=False,
    )


def create_model_version(mysql_cfg, version, rank, reg_param):
    """Create a model in building state before factors are persisted."""
    connection = _connect(mysql_cfg)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_versions (version, `rank`, reg_param, status)
                VALUES (%s, %s, %s, 'building')
                """,
                (version, int(rank), float(reg_param)),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def activate_model_version(mysql_cfg, version):
    """Atomically retire the old active model and publish a building model."""
    connection = _connect(mysql_cfg)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE model_versions
                SET status = 'retired'
                WHERE status = 'active' AND version <> %s
                """,
                (version,),
            )
            cursor.execute(
                """
                UPDATE model_versions
                SET status = 'active', activated_at = UTC_TIMESTAMP()
                WHERE version = %s AND status = 'building'
                """,
                (version,),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(f"model version is not building: {version}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def cleanup_stale_model_data(mysql_cfg, keep_version, tables=("recommendations", "model_item_factors")):
    """Delete recommendations / item-factor rows that belong to other model versions.

    每次训练都是 append 写入， activate 新版本后，旧版本（含历史失败残留的
    retired 版本）数据不再被任何代码读取，留着只会让表无限膨胀。
    表名是本模块内的固定常量，不存在注入风险。
    """
    connection = _connect(mysql_cfg)
    try:
        with connection.cursor() as cursor:
            for table in tables:
                cursor.execute(
                    f"DELETE FROM {table} WHERE model_version <> %s",
                    (keep_version,),
                )
                print(f"Cleaned {cursor.rowcount:,} stale rows from {table}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def retire_model_version(mysql_cfg, version):
    """Hide a partially written model while preserving any active model."""
    connection = _connect(mysql_cfg)
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE model_versions
                SET status = 'retired'
                WHERE version = %s AND status = 'building'
                """,
                (version,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
