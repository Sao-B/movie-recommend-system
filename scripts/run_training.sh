#!/usr/bin/env bash
# run_training.sh — 提交 PySpark ALS 训练作业
# 用法：bash scripts/run_training.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPARK_DIR="${SCRIPT_DIR}/../spark"

echo "==> 提交 Spark ALS 训练作业..."
spark-submit \
  --master local[*] \
  --driver-memory 4g \
  --executor-memory 4g \
  "${SPARK_DIR}/train_als.py"

echo "==> 训练完成，推荐结果已写入 MySQL recommendations 表"
