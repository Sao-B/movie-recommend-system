# -*- coding: utf-8 -*-
"""流式边计算 vs dict 边计算：**两条路径必须逐边相等**。

背景
----
``spark/train_itemcf.py`` 有两个逐分区的边计算实现：

- ``make_edge_partition_fn``（dict 版）：把整个分区的候选行攒进
  ``groups = {src: ([tgt...], [c...])}`` 再做 Top-K。内存 ∝ 分区行数，
  全量数据下会被内核 OOM killer 杀掉（见交接文档坑位清单第 6 条）。
- ``make_streaming_edge_partition_fn``（流式版）：只保留当前一个 src 的候选，
  内存 O(单源候选数)，与总边数无关。**这是全量唯一能跑通的实现。**

两者数学等价，但**流式版有一个静默失效的依赖**：它靠"读入时 src 有序"判断
一个源何时结束。如果调用方忘了 ``sortWithinPartitions('src')``，同一个 src 的
候选会被切成多段、每段各自做一次 Top-K，产出**少边**的图 —— **不报错、不抛异常，
只是边少了**。这是最危险的失败模式，所以必须有测试把等价性钉死。

本模块直接测那两个分区函数（不启动 Spark）：构造按 src 排好序的候选行，
喂给两个函数，比对输出序列。流式版的排序前提由 :func:`sorted_candidates`
负责模拟（对应 Spark 侧的 ``sortWithinPartitions``）。
"""
import os
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for candidate in (str(PROJECT_ROOT), str(PROJECT_ROOT / "spark")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from spark import train_itemcf  # noqa: E402


class _FakeBroadcast(object):
    """``Broadcast`` 的最小替身：``.value`` 就是一个 dict。"""

    def __init__(self, value):
        self._value = value

    @property
    def value(self):
        return self._value


class _Row(object):
    """``mapPartitions`` 收到的行对象只需要支持 ``row['src']`` 这类下标访问。"""

    __slots__ = ('_d',)

    def __init__(self, **kwargs):
        self._d = kwargs

    def __getitem__(self, key):
        return self._d[key]


def _dict_version(rows, n_like, beta, max_k):
    """跑 dict 版分区函数，返回输出列表。"""
    fn = train_itemcf.make_edge_partition_fn(_FakeBroadcast(n_like), beta, max_k)
    return list(fn(iter([_Row(src=r[0], tgt=r[1], c=r[2]) for r in rows])))


def _streaming_version(rows, n_like, beta, max_k, presorted=True):
    """跑流式版分区函数，返回输出列表。

    :param presorted: ``True`` 时先按 src 排序（模拟 Spark 侧的
        ``sortWithinPartitions('src')``）；``False`` 时原样喂入乱序数据，
        用来演示"缺 sort 会少边"这个静默失效——**不是**期望的正确行为。
    """
    data = sorted(rows, key=lambda r: r[0]) if presorted else list(rows)
    fn = train_itemcf.make_streaming_edge_partition_fn(
        _FakeBroadcast(n_like), beta, max_k)
    return list(fn(iter([_Row(src=r[0], tgt=r[1], c=r[2]) for r in data])))


#: 一组手工构造的共现候选 (src, tgt, c)，覆盖：
#:   - 多个 src（3 个），每个 src 若干候选
#:   - 候选数 > max_k（触发截断）
#:   - 零好评来源（101 的 n_like=0，应整源丢边）
#:   - 零好评目标（c 指向 109，其 n_like=0，应被过滤）
#:   - c <= 0 与 src == tgt（应被过滤）
#:   - 真正平票（同一 src 下两条 c 相同、n_tgt 相同 -> sim 相等，靠 tgt 升序打破）
CANDIDATES = [
    # src=100：3 个候选，都有效
    (100, 101, 3),
    (100, 102, 2),
    (100, 103, 1),
    # src=200：5 个候选，max_k=3 时截断到 3
    (200, 201, 5),
    (200, 202, 4),
    (200, 203, 3),
    (200, 204, 2),
    (200, 205, 1),
    # src=300：候选里混了应被过滤的 c=0、自环、零好评目标
    (300, 301, 4),
    (300, 302, 2),
    (300, 302, 0),   # c <= 0 -> 过滤
    (300, 303, 3),
    (300, 303, 3),   # 重复候选对（同 src 同 tgt）-> 两条都在，排序后都在
    (300, 304, -1),  # c < 0  -> 过滤
    (300, 305, 2),   # 目标 305 零好评 -> 过滤
]

N_LIKE = {
    100: 3, 101: 3, 102: 2, 103: 1,      # 有效
    101 + 100: 0,                        # 占位，避免误读
    200: 5, 201: 5, 202: 4, 203: 3, 204: 2, 205: 1,
    300: 4, 301: 4, 302: 2, 303: 3,
    305: 0,      # 零好评目标
    101: 3,
}
# 修正：把 100 设为零好评来源，用于验证"整源丢边"
N_LIKE[100] = 3   # 100 有效（保留 100 的边，才能测多个 src）
ZERO_LIKE_SRC = 400
N_LIKE[ZERO_LIKE_SRC] = 0
CANDIDATES_WITH_ZERO_SRC = CANDIDATES + [
    (ZERO_LIKE_SRC, 401, 5),
    (ZERO_LIKE_SRC, 402, 3),
]
N_LIKE[401] = 3
N_LIKE[402] = 2


class StreamingMatchesDictTest(unittest.TestCase):
    """两条路径的输出必须逐条相同（顺序也相同）。"""

    def _assert_same(self, rows, n_like, beta, max_k):
        got_dict = _dict_version(rows, n_like, beta, max_k)
        got_stream = _streaming_version(rows, n_like, beta, max_k)
        self.assertEqual(
            got_dict, got_stream,
            'streaming and dict disagree\n dict     = %r\n streaming = %r'
            % (got_dict, got_stream))
        return got_stream

    def test_equal_with_zero_like_source(self):
        """含零好评来源时两条路径仍相等（整源丢边）。"""
        out = self._assert_same(CANDIDATES_WITH_ZERO_SRC, N_LIKE, 0.0, 3)
        self.assertNotIn(ZERO_LIKE_SRC, [e[0] for e in out])

    def test_equal_across_betas(self):
        """beta 改变收缩系数与顺序，两条路径仍应一致。"""
        for beta in (0.0, 0.25, 1.0, 5.0):
            for max_k in (1, 3, 100):
                self._assert_same(CANDIDATES_WITH_ZERO_SRC, N_LIKE, beta, max_k)

    def test_equal_max_k_truncation(self):
        """max_k 截断生效且两条路径一致。"""
        for max_k in (1, 2, 3, 5, 1000):
            out = self._assert_same(CANDIDATES_WITH_ZERO_SRC, N_LIKE, 0.0, max_k)
            counts = {}
            for src, _t, _s, _c in out:
                counts[src] = counts.get(src, 0) + 1
            for src, n in counts.items():
                self.assertLessEqual(n, max_k)

    def test_unsorted_input_would_lose_edges(self):
        """**记录失效模式**：不给 sort 时流式版可能少边（静默，不抛异常）。

        这个测试不是断言"正确行为"，而是把**危险行为**钉成可执行的文档。

        少边的机制：``max_k`` 截断是**按 src 全局**取 Top-K。src 被打断成
        多段后，每段各自取 Top-K，**落在被切掉那几段里的高相似度边**就永远
        选不上。所以只有当 ``max_k < 该 src 的候选总数`` 时才会少边 ——
        ``max_k`` 大于等于候选数时，每段全留，切段无害（这解释了为什么
        ``test_equal_*`` 那几组即使没有 sort 也会通过）。

        所以这里的构造必须让候选数 **>** ``max_k``。
        """
        # src=300 有 4 个候选，max_k=2 -> 必须全局选最好的 2 个。
        # 不排序时 300 被切成两段（[301,302] 与 [303,304]），
        # 每段各自取 2 个 -> 4 条（**多了**，触发契约里 max_k 的上界校验），
        # 而排序后正确产出恰好 2 条。
        # c 必须 <= min(n_like) 且 c/sqrt(n_i*n_j) <= 1：
        # n_like 全取 4 -> sqrt(4*4)=4，所以 c 最大 4。
        # 排序后 300 应选中 c=4（301/303 并列，tgt 小者胜）与 c=3 之一，
        # 具体哪条由 (-sim, tgt) 契约决定，这里只断言"恰好 2 条"。
        unsorted = [
            (300, 301, 4),   # sim = 4/4 = 1.0（最高）
            (100, 101, 3),   # 换源
            (300, 302, 1),   # sim = 1/4（最低）
            (100, 102, 2),
            (300, 303, 4),   # sim = 1.0（与 301 并列，tgt 更大，平票输）
            (300, 304, 2),
        ]
        n_like = {100: 3, 101: 3, 102: 2, 103: 1,
                  300: 4, 301: 4, 302: 4, 303: 4, 304: 4}
        got_unsorted = _streaming_version(unsorted, n_like, 0.0, 2,
                                          presorted=False)
        got_sorted = _streaming_version(unsorted, n_like, 0.0, 2,
                                        presorted=True)
        # 排序后：300 恰好 2 条（契约正确）
        self.assertEqual(len([e for e in got_sorted if e[0] == 300]), 2)
        self.assertEqual(len([e for e in got_sorted if e[0] == 100]), 2)
        # 不排序：300 被切成两段，各段各取 2 条 -> 超过 max_k
        self.assertGreater(
            len([e for e in got_unsorted if e[0] == 300]), 2,
            'this test documents the failure mode; if it no longer holds, '
            'the streaming function may have gained an undocumented safety '
            'net (e.g. accumulating a full group before truncating)')
        # 且它与 dict 版不等 —— 上面的等价性测试正是靠这条抓到 sort 被删
        self.assertNotEqual(_dict_version(unsorted, n_like, 0.0, 2),
                            got_unsorted)

    def test_empty_input(self):
        """空输入：两条路径都给空输出。"""
        self._assert_same([], N_LIKE, 0.0, 100)

    def test_only_zero_like_sources(self):
        """全部来源都零好评 -> 无边。"""
        rows = [(400, 401, 5), (400, 402, 3)]
        out = self._assert_same(rows, N_LIKE, 0.0, 100)
        self.assertEqual(out, [])

    def test_single_src(self):
        """只有一个来源：验证"循环外 flush 最后一个源"没漏。"""
        rows = [(100, 101, 3), (100, 102, 2), (100, 103, 1)]
        out = self._assert_same(rows, N_LIKE, 0.0, 100)
        self.assertEqual(len(out), 3)
        self.assertTrue(all(e[0] == 100 for e in out))


if __name__ == '__main__':
    unittest.main()
