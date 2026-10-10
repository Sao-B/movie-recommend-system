# -*- coding: utf-8 -*-
"""在 holdout 协议的训练段上训练一份对照 ALS，产出因子 jsonl（不发布、不进系统）。

为什么有这个脚本
================

系统管线（spark/train_als.py）用**哈希切分**（md5(userId:movieId) 逐条 80/20）
训练、以 RMSE 自检；而 holdout 评测协议是**按时间**切分（每用户时间后 20% 为
留出）。两种切分独立，若拿管线模型去评时间留出段，留出正例约 80% 已进过训练
集——**泄漏**，排序指标虚高不可用。

本脚本用 holdout 协议**同一份**训练段（``split_train.csv``，由
``heldout_eval.RatingSession`` 产出）训练一份参数与系统管线完全一致的 ALS
（rank50 / reg0.1 / maxIter10 / nonnegative），把物品因子落成 jsonl：
``{"movie_id": <int>, "factors": [<float>...]}``，供
``heldout_als_eval.py --factors-jsonl`` 在同一协议下算 NDCG@20 / Recall@20。

它**不写 MySQL、不发布版本、不生成系统推荐**——这是一个实验对照物。

运行（VM）::

    spark-submit --master 'local[*]' --driver-memory 4g \
      experiments/itemcf/train_als_holdout.py \
      --train-csv /home/h/itemcf_holdout/fullscale-20261005T095205Z/split_train.csv \
      --out /home/h/itemcf_holdout/als_factors

Python 3.6 兼容（VM driver 为 3.6.8）：无 f-string / dataclasses / walrus。
"""
import argparse
import hashlib
import json
import os
import sys
import time

from pyspark.ml.recommendation import ALS
from pyspark.sql import functions as F

FACTORS_JSONL = 'factors.jsonl'
META_JSON = 'meta.json'


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Train a protocol-consistent ALS on the holdout train segment')
    parser.add_argument('--train-csv', required=True,
                        help='holdout 协议的训练段（RatingSession 产出的 '
                             'split_train.csv，无表头，列 = userId,movieId,'
                             'rating,timestamp）')
    parser.add_argument('--out', required=True, help='因子与 meta 的输出目录')
    parser.add_argument('--rank', type=int, default=50)
    parser.add_argument('--reg', type=float, default=0.1)
    parser.add_argument('--max-iter', type=int, default=10)
    parser.add_argument('--seed', type=int, default=None,
                        help='ALS 随机种子（缺省不设，与系统管线一致）')
    return parser.parse_args(argv)


def file_sha256(path, chunk_size=1 << 22):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv=None):
    args = parse_args(argv)
    started = time.time()
    if not os.path.isfile(args.train_csv):
        raise SystemExit('train csv not found: %s' % args.train_csv)
    if os.path.isdir(args.out) and os.listdir(args.out):
        raise SystemExit('refusing to overwrite non-empty out dir: %s' % args.out)
    os.makedirs(args.out)

    input_sha = file_sha256(args.train_csv)
    print('[als-holdout] input sha256=%s' % input_sha)
    sys.stdout.flush()

    from pyspark.sql import SparkSession
    spark = SparkSession.builder.appName('ALS-Holdout-Protocol').getOrCreate()
    try:
        # VM 的默认 FS 是 HDFS（fs.defaultFS=hdfs://hadoop102:9000），
        # 本地文件必须显式加 file:// 前缀；sha256 仍走本地路径。
        spark_csv_path = 'file://' + os.path.abspath(args.train_csv)
        ratings = (
            spark.read.csv(spark_csv_path)
            .select(
                F.col('_c0').cast('int').alias('userId'),
                F.col('_c1').cast('int').alias('movieId'),
                F.col('_c2').cast('float').alias('rating'),
            )
            .na.drop()
        )
        count = ratings.count()
        print('[als-holdout] training ratings: %d' % count)
        sys.stdout.flush()

        als = ALS(
            rank=args.rank,
            maxIter=args.max_iter,
            regParam=args.reg,
            userCol='userId',
            itemCol='movieId',
            ratingCol='rating',
            coldStartStrategy='drop',
            nonnegative=True,
            implicitPrefs=False,
            seed=args.seed,
        )
        model = als.fit(ratings)

        item_factors = model.itemFactors.select(
            F.col('id').cast('int').alias('movie_id'),
            F.col('features').alias('factors'),
        )
        rows = item_factors.collect()
        print('[als-holdout] item factors collected: %d' % len(rows))
        sys.stdout.flush()

        factors_path = os.path.join(args.out, FACTORS_JSONL)
        with open(factors_path, 'w', encoding='utf-8') as handle:
            for row in rows:
                # row['factors'] 是 DenseVector，直接迭代出 float——
                # 不用 pyspark.ml.functions.vector_to_array（它带
                # `from __future__ import annotations`，VM 的 3.6.8 import 即炸）。
                handle.write(json.dumps({
                    'movie_id': int(row['movie_id']),
                    'factors': [float(v) for v in row['factors']],
                }) + '\n')

        meta = {
            'kind': 'als_holdout_protocol',
            'train_csv': os.path.abspath(args.train_csv),
            'train_csv_sha256': input_sha,
            'train_rows': count,
            'rank': args.rank,
            'reg_param': args.reg,
            'max_iter': args.max_iter,
            'seed': args.seed,
            'n_items': len(rows),
            'seconds': time.time() - started,
        }
        with open(os.path.join(args.out, META_JSON), 'w', encoding='utf-8') as handle:
            json.dump(meta, handle, indent=2, sort_keys=True)
        print('[als-holdout] done: %d factors in %.1fs -> %s'
              % (len(rows), time.time() - started, factors_path))
    finally:
        spark.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
