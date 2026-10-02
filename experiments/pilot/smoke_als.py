"""VM 冒烟：合成 3 用户 × 4 物品 × 12 条评分，跑 3 轮 ALS，打印损失。"""
from __future__ import print_function

from pyspark.sql import SparkSession

from als import fit_spark


def main():
    spark = SparkSession.builder.appName('pilot-smoke-als').getOrCreate()
    rows = [(1, 1, 5.0, 1), (1, 2, 4.0, 2), (1, 3, 2.0, 3), (1, 4, 1.0, 4),
            (2, 1, 4.0, 1), (2, 2, 5.0, 2), (2, 3, 3.0, 3), (2, 4, 1.0, 4),
            (3, 1, 2.0, 1), (3, 2, 1.0, 2), (3, 3, 4.0, 3), (3, 4, 5.0, 4)]
    _, losses = fit_spark(spark, rows, rank=4, reg=0.1, iters=3, seed=42,
                          num_slices=2, log=print)
    assert losses[-1] < losses[0], 'loss did not decrease: %s' % losses
    print('SMOKE OK losses=%s' % ['%.4f' % x for x in losses])
    spark.stop()


if __name__ == '__main__':
    main()
