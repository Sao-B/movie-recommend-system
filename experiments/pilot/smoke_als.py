"""VM 冒烟：合成 3 用户 × 4 物品 × 12 条评分，跑 3 轮 ALS，打印损失。

以 `spark-submit --master 'local[*]' smoke_als.py` 提交。脚本自己把 als.py
注册进 SparkContext（addPyFile），因此不依赖 cwd / driver 的 sys.path——
worker 反序列化 mapPartitions 闭包时要按模块名重新导入 als，若只靠裸
`from als import fit_spark`，在 local[*] 之外（yarn / cluster / 换工作目录）
会 ModuleNotFoundError。

结果行带唯一前缀 `[SMOKE_RESULT]`，并用退出码表成败：Spark 会把大量日志
刷在 stdout 上，成功标记常被淹没，只 grep 结果行 / 看退出码才可靠。
"""
from __future__ import print_function

import os
import sys

from pyspark.sql import SparkSession

_HERE = os.path.dirname(os.path.abspath(__file__))
# 让 driver 侧也能 import als：直接 `python smoke_als.py`（不走 spark-submit 的
# 脚本目录注入）时，sys.path[0] 未必是本目录。
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from als import fit_spark

_ALS_PATH = os.path.join(_HERE, 'als.py')


def main():
    spark = SparkSession.builder.appName('pilot-smoke-als').getOrCreate()
    # K1：把 als.py 分发给每个 executor，使 worker 能按模块名重导入 als。
    spark.sparkContext.addPyFile(_ALS_PATH)

    rows = [(1, 1, 5.0, 1), (1, 2, 4.0, 2), (1, 3, 2.0, 3), (1, 4, 1.0, 4),
            (2, 1, 4.0, 1), (2, 2, 5.0, 2), (2, 3, 3.0, 3), (2, 4, 1.0, 4),
            (3, 1, 2.0, 1), (3, 2, 1.0, 2), (3, 3, 4.0, 3), (3, 4, 5.0, 4)]
    _, losses = fit_spark(spark, rows, rank=4, reg=0.1, iters=3, seed=42,
                          num_slices=2, log=print)
    try:
        # 严格单调下降：任何一轮不降都算失败（只看首末会漏掉「先降后升」的坏算法）。
        assert all(losses[i + 1] < losses[i] for i in range(len(losses) - 1)), losses
        # 黄金值对拍：该管线已被证明确定性（同值跨分区数 / 跨重复运行一致）。
        # 黄金值取自基线实现的确定性输出（0.397950 / 0.228600 / 0.194750），
        # 容差 1e-3 只容忍浮点末位差异；将来若有意变更算法，必须同步更新
        # 黄金值并说明原因，不能为了让断言变绿而放宽容差。
        assert abs(losses[-1] - 0.194750) < 1e-3, losses
        # 保留原有首末比较作为兜底（对判别力无增益，但失败信息最直观）。
        assert losses[-1] < losses[0], losses
    except AssertionError as exc:
        print('[SMOKE_RESULT] FAIL %s' % exc)
        spark.stop()
        return 1

    print('[SMOKE_RESULT] OK losses=%s' % ['%.4f' % x for x in losses])
    spark.stop()
    return 0


if __name__ == '__main__':
    sys.exit(main())
