#!/usr/bin/env bash
# init_hdfs.sh — 创建 HDFS 目录并上传 MovieLens 数据文件
# 使用前提：Hadoop 已启动（start-dfs.sh && start-yarn.sh）
# 用法：bash scripts/init_hdfs.sh

set -e

HDFS_DIR="/movielens"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOCAL_RAW="${SCRIPT_DIR}/../data/raw"

echo "==> 检查本地数据文件..."
if [ ! -f "${LOCAL_RAW}/ratings.csv" ] || [ ! -f "${LOCAL_RAW}/movies.csv" ]; then
  echo "错误：请先将 MovieLens-32M 解压到 data/raw/ 目录"
  echo "下载地址：https://grouplens.org/datasets/movielens/32m/"
  exit 1
fi

echo "==> 创建 HDFS 目录: ${HDFS_DIR}"
hdfs dfs -mkdir -p "${HDFS_DIR}"

echo "==> 上传 ratings.csv（约 650MB，请耐心等待）..."
hdfs dfs -put -f "${LOCAL_RAW}/ratings.csv" "${HDFS_DIR}/ratings.csv"

echo "==> 上传 movies.csv..."
hdfs dfs -put -f "${LOCAL_RAW}/movies.csv" "${HDFS_DIR}/movies.csv"

echo "==> 完成！HDFS 文件列表："
hdfs dfs -ls "${HDFS_DIR}"
