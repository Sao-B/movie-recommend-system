def read_df_from_mysql(spark, query_or_table, jdbc_url, user, password):
    """
    通过 JDBC 从 MySQL 读取数据，返回 Spark DataFrame。

    参数
    ----
    spark           : SparkSession
    query_or_table  : str — 表名（如 'ratings'）或子查询
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
