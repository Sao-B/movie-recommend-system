def write_df_to_mysql(df, table, jdbc_url, user, password, mode="overwrite"):
    """
    通过 JDBC 将 Spark DataFrame 写入 MySQL 表。

    参数
    ----
    df       : pyspark.sql.DataFrame
    table    : str   — 目标表名
    jdbc_url : str   — JDBC 连接串（含数据库名）
    user     : str
    password : str
    mode     : str   — 'overwrite' 或 'append'（overwrite 会先 truncate）
    """
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
        .option("truncate", "true")   # TRUNCATE 代替 DROP，保留表结构与索引
        .mode(mode)
        .save()
    )
