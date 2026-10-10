# -*- coding: utf-8 -*-
"""本轮（全量规模留出评测）**只读复用**的口径模块。

为什么单独一个文件
==================

本轮的评测入口 :mod:`experiments.itemcf.heldout_eval` 不复刻"两遍扫描 + 磁盘 CSR +
分块向量化构图"——那正是上一轮出过 10 个缺陷的地方，重写只会引入新的分歧。
它复用的三样东西是**已通过 533 个用例与全量实跑**的：

1. :func:`experiments.itemcf.full_train.stream_input_pass1` —— 流式索引（用户下标、
   ``item_ids`` 升序、``n_all`` / ``n_like``）；
2. :func:`experiments.itemcf.full_train.run_prepare` —— 两遍扫描落磁盘 mmap CSR；
3. :func:`experiments.itemcf.full_train.build_block_edges` —— 列块共现 + 向量化 top-k。

**本项目所有跨模块 import 都以模块级方式写**（``full_train`` 对 ``resource_guard``
就是硬 import，那是上一轮刻意的设计：没有监督就不许跑）。本模块遵循同一风格，
不用裸 ``importlib`` 去绕开它——读到这个文件的人应该一眼看清依赖是什么。

对拍测试也从这个模块取 :func:`build_block_edges`，于是"主入口用的实现"与
"对拍用的实现"**是同一份**，不存在"两套向量化实现互相印证"的假独立。

本模块**不含**评测逻辑、不含资源监控、不读用户名单、不写任何产物。
它也不改变 ``full_train`` 的行为——上一轮 ``d535c178`` 那份模型的
``source_hashes`` 指纹因此不受影响（有测试断言）。

Python 3.6 兼容：无 f-string / dataclasses / walrus / ``from __future__ import
annotations``。
"""
import os
import sys

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

#: 复用的三个数据面函数（成员名 = full_train 里的原名）。
from experiments.itemcf.full_train import (  # noqa: E402
    POSITIVE_THRESHOLD,
    build_block_edges,
    run_prepare,
    stream_input_pass1,
)

__all__ = ['POSITIVE_THRESHOLD', 'build_block_edges', 'run_prepare',
           'stream_input_pass1', 'ArgStub']


class ArgStub(object):
    """``run_prepare`` 需要的参数对象（它只读这几个字段）。

    ``run_prepare`` 的签名是为 CLI 设计的，直接吃 ``argparse.Namespace``。
    这里给一个显式的最小替身，避免调用方为了四个字段去伪造整个 Namespace。
    """

    __slots__ = ('positive_threshold', 'duplicate_policy', 'max_k', 'beta',
                 'block_size')

    def __init__(self, positive_threshold=POSITIVE_THRESHOLD,
                 duplicate_policy='reject', max_k=100, beta=0.0,
                 block_size=128):
        self.positive_threshold = float(positive_threshold)
        self.duplicate_policy = str(duplicate_policy)
        self.max_k = int(max_k)
        self.beta = float(beta)
        self.block_size = int(block_size)

    def as_dict(self):
        return {'positive_threshold': self.positive_threshold,
                'duplicate_policy': self.duplicate_policy,
                'max_k': self.max_k, 'beta': self.beta,
                'block_size': self.block_size}
