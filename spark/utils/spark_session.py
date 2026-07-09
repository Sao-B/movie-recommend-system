import os
import yaml


def load_config(config_path=None):
    """从项目根目录读取 config.yaml。"""
    if config_path is None:
        config_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "config.yaml"
        )
    with open(os.path.abspath(config_path), "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def get_spark_session(app_name="MovieRecommend", config=None):
    """
    创建并返回 (SparkSession, config_dict, jdbc_url)。

    注意：MySQL JDBC driver (mysql-connector-java-*.jar) 需预先放到
    $SPARK_HOME/jars/ 目录，或通过 spark-submit --jars 传入。
    """
    # 延迟导入，避免在无 PySpark 环境下 import 报错
    from pyspark.sql import SparkSession

    if config is None:
        config = load_config()

    hdfs_namenode = config["hdfs"]["namenode"]
    mysql_cfg = config["mysql"]
    jdbc_url = (
        f"jdbc:mysql://{mysql_cfg['host']}:{mysql_cfg['port']}"
        f"/{mysql_cfg['database']}"
        f"?useSSL=false&characterEncoding=utf8&serverTimezone=Asia/Shanghai"
    )

    spark = (
        SparkSession.builder
        .appName(app_name)
        .config("spark.hadoop.fs.defaultFS", hdfs_namenode)
        .config("spark.sql.shuffle.partitions", "8")   # 单机调小分区数
        .getOrCreate()
    )
    spark.sparkContext.setLogLevel("WARN")
    return spark, config, jdbc_url
