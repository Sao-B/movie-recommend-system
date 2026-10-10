#!/usr/bin/env bash
# run_itemcf_training.sh — 提交 PySpark ItemCF 邻居图训练作业
# 用法：bash scripts/run_itemcf_training.sh [ratings.csv] [输出根目录]
#   默认输入：/home/h/movielens/ratings.csv（VM 上的 MovieLens 全量评分）
#   默认输出：/home/h/itemcf_out/itemcf_spark（实际写 <输出>/<run_id>/model）
#
# 兼容性验收（产出的模型目录，<out>/<run_id>/）：
#   1) 结构与哈希核验 + 抽样独立重算：
#      python -m experiments.itemcf.verify_full_model \
#          --run-dir <out>/<run_id> --input <ratings.csv>
#      （A 段结构/无自环/零好评空区间/ID 一致性/输入指纹按 model_io 契约落盘；
#        B 段用同一份评分独立重算，公式与 core.py 一致，预期逐位复现。）
#   2) 与 scipy 模型对拍：
#      experiments/itemcf/equiv_check.py 从原始评分独立重算 ItemCF；本作业与
#      scipy 版同公式同参数（k=100、beta=0、阈值 4.0），用 model_io.load_model
#      读入两边模型逐边比较，目标 ID/顺序/分数预期一致（容差 1e-12）。
#   3) 在线消费端直接加载：
#      backend/app/itemcf_service.py 的 ItemCFGraph(<out>/<run_id>/model)
#      按 model.json 的哈希清单 mmap 加载 7 个 npy——落盘走的是
#      experiments.itemcf.model_io.save，信任链天然兼容。

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SPARK_DIR="${SCRIPT_DIR}/../spark"
# 项目根加入 executor 的 Python worker 路径：worker 进程不继承 driver 的
# sys.path，闭包反序列化时需要 import utils.itemcf_math
PROJECT_ROOT="${SCRIPT_DIR}/.."

INPUT="${1:-/home/h/movielens/ratings.csv}"
# Spark 集群的 fs.defaultFS 指向 HDFS：裸本地绝对路径会被解释成 hdfs://，
# 这里给本地路径自动补 file:// 前缀（已是 URI 的不动）
case "$INPUT" in
  /*) INPUT="file://${INPUT}" ;;
esac
OUT="${2:-/home/h/itemcf_out/itemcf_spark}"

echo "==> 提交 Spark ItemCF 训练作业（input=${INPUT}, out=${OUT}）..."
spark-submit \
  --master local[*] \
  --conf "spark.executorEnv.PYTHONPATH=${PROJECT_ROOT}" \
  --driver-memory 4g \
  --executor-memory 4g \
  "${SPARK_DIR}/train_itemcf.py" \
  --input "${INPUT}" \
  --out "${OUT}"

echo "==> ItemCF 邻居图训练完成：${OUT}/<run_id>/model（7 npy + model.json）"
echo "==> 验收：python -m experiments.itemcf.verify_full_model --run-dir ${OUT}/<run_id> --input ${INPUT}"
