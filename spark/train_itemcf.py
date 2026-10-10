# -*- coding: utf-8 -*-
"""PySpark 版 ItemCF 邻居图训练作业（论文：基于 Hadoop 的个性化电影推荐系统）。

用 Spark 重写 ``experiments/itemcf/full_train.py`` 的 scipy 版 ItemCF 训练，
产出与现有模型契约**完全兼容**的模型目录（7 个 npy + model.json），使选型
算法（ItemCF）也能在 Hadoop/Spark 生态上训练。

数学口径与权威实现逐位一致
==========================

算法部分（共现计数 / 相似度公式 / Top-K 规则 / β 参数）不在本模块实现，
全部抽到 :mod:`utils.itemcf_math`（纯 NumPy，可独立测试），其与
``experiments/itemcf/core.py`` 的逐位一致性由 ``tests/test_spark_itemcf_core.py``
用 ``core.build_neighbors`` 对拍验证。本模块只做分布式数据准备，
逐分区调用纯函数。

数据流（读什么 -> 几步变换 -> 写什么）
======================================

1. 读 ratings CSV（header，按列名取 userId/movieId/rating/timestamp——与
   ``full_train.iter_rating_chunks`` 相同的按名列取值方式）；
2. 行级校验，**拒绝而非静默丢弃**（与 ``full_train.parse_rating_fields``
   同口径）：userId/movieId 为十进制正整数字面量、rating ∈ [0.5, 5.0] 且
   半星档、timestamp 为正整数；发现非法行直接失败；
3. ``(userId, movieId)`` 唯一性检查：duplicate_policy 固定为 reject——
   发现重复直接失败（``full_train`` 默认即 reject；dedupe_keep_last 明确
   "正式全量训练不得使用"，故不提供）；
4. 统计 ``n_all``（全部评分人数）/ ``n_like``（ polarity 集合人数：pos =
   rating >= positive_threshold，neg = rating <= negative_threshold）、
   item 全集（含零证据物品，数值升序）；
5. 正样本（rating >= 阈值）自连接（同 userId，a.movieId < b.movieId）→
   无序对计数 → 双向展开，得到共现 ``c_ij``（整数计数，精确；
   ``c_ij >= 1`` 蕴含两端 ``n_like >= 1``，零好评物品天然无共现边）；
6. 按 source 重分区（同一来源的全部候选边落在同一分区）后逐分区调用
   ``utils.itemcf_math.edges_from_cooccurrence``：相似度 +
   每来源 Top-K（k=100，排序键 = 相似度降序 + movieId 升序，
   先收缩再取 top-k）；
7. driver 侧流式收集（toLocalIterator）装配 CSR（movieId -> 下标、
   全局排序、indptr），交 ``experiments.itemcf.model_io`` 落盘：
   ``<out>/<run_id>/model/`` 下 7 个 npy + model.json（哈希链、状态机、
   结构校验全部复用 model_io 的实现，不复制代码），另写 run_status.json。

兼容性验收（产出的模型目录）
============================

- ``python -m experiments.itemcf.verify_full_model --run-dir <out>/<run_id>
  --input <ratings.csv>``：A 段结构不变式 / 无自环 / 零好评空区间 /
  ID 一致性 / 输入指纹均按 model_io 契约落盘，预期通过；B 段抽样独立重算
  使用同一份评分与同一公式（verify_full_model 的相似度是独立 NumPy 实现，
  与 core.py 同式），预期逐位复现（容差 1e-12）。
- ``experiments/itemcf/equiv_check.py``：对拍器从原始评分独立重算 ItemCF
  并与基线对拍；本作业模型与 scipy 版同公式同参数，用
  ``model_io.load_model`` 读入两边产物逐边比较（目标 ID / 顺序 / 分数）
  预期一致，浮点分数对拍容差 1e-12（同为 float64 IEEE-754 同序求值，
  实测差异应为 0）。
- ``backend/app/itemcf_service.py`` 的 ``ItemCFGraph(model_dir)`` 可直接
  加载：它只按 model.json 声明的数组契约 mmap 加载并校验 SHA-256，
  而落盘走的是 model_io.save，天然满足其信任链。

与 scipy 版的实现层差异（数学无差异）
======================================

- 共现计数由 Spark join + groupBy 完成（整数，精确），scipy 版由稀疏矩阵
  乘完成（同为整数计数）——数值无差异；
- 相似度是同一条 float64 公式的同序求值（无求和步骤），逐位相等；
- ``full_train`` 的 ``block_size``（列块大小）与 ``KEY_RADIX``（去重
  packing 的进制，要求 id < 10^7）是 scipy/单机实现细节，不影响产物
  契约，Spark 作业不引入；``--min-support`` 在 full_train 中不存在，
  故本作业也不提供。

Spark 作业文件 Python 3.6 兼容：无 f-string / walrus / dataclass。
"""
import argparse
import array
import hashlib
import json
import os
import random
import sys
import time

import numpy as np

_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(_MODULE_DIR)
for _path in (_MODULE_DIR, PROJECT_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from utils import itemcf_math  # noqa: E402
from experiments.itemcf import model_io as M  # noqa: E402

#: 好评二值化阈值（与 full_train.POSITIVE_THRESHOLD 一致）。
POSITIVE_THRESHOLD = 4.0
#: 差评二值化阈值（polarity=neg 用；EDA 差评阈值探索的定稿值）。
NEGATIVE_THRESHOLD = 2.0

#: 每来源最大邻居数（与 full_train.MAX_K 一致）。
MAX_K = 100

#: 共现收缩系数（与 full_train.BETA 一致，0 即不收缩）。
BETA = 0.0

#: 合法评分范围（与 full_train 一致）：半星档 [0.5, 5.0]。
RATING_MIN = 0.5
RATING_MAX = 5.0
RATING_STEP = 0.5


def log(message):
    """行日志（照搬 run_training 系的 stdout 风格，带时间戳）。"""
    print('[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message))


def make_run_id():
    """``YYYYMMDDTHHMMSSZ-<8 位小写十六进制>``（与 full_train.make_run_id 同型）。"""
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    token = '%08x' % random.SystemRandom().getrandbits(32)
    return '%s-%s' % (stamp, token)


def resolve_out_dir(out_arg, run_id):
    """把 ``--out`` 解析成本轮独占新目录（与 full_train.resolve_out_dir 同规则）。"""
    out = os.path.abspath(out_arg)
    tail = os.path.basename(out.rstrip('\\/'))
    if tail == run_id:
        target = out
    else:
        try:
            M.verify_id_format(tail, 'out basename')
        except ValueError:
            target = os.path.join(out, run_id)
        else:
            raise RuntimeError(
                '--out basename %r already looks like a run_id but differs '
                'from the effective run_id %r. Pass --out <...>/%s or make '
                'them match.' % (tail, run_id, tail))
    if os.path.exists(target):
        raise RuntimeError(
            'output directory already exists, refusing to reuse: %s' % target)
    return target


def file_sha256(path, chunk_size=1 << 22):
    """文件 SHA-256（分块读）；路径不可本地读时返回 None。"""
    if not os.path.isfile(path):
        return None
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def parse_args(argv=None):
    """CLI：与 full_train 实际存在的参数对齐（无 --min-support，见模块 docstring）。"""
    parser = argparse.ArgumentParser(
        prog='spark-submit spark/train_itemcf.py',
        description='PySpark ItemCF neighbor-graph training, model directory '
                    'contract-compatible with experiments/itemcf.')
    parser.add_argument('--input', required=True,
                        help='完整评分 CSV（VM: /home/h/movielens/ratings.csv）')
    parser.add_argument('--out', required=True,
                        help='产物根目录；实际写 <out>/<run_id>/model')
    parser.add_argument('--top-k', type=int, default=MAX_K,
                        help='每来源最大邻居数（默认 100，即 full_train 的 max_k）')
    parser.add_argument('--beta', type=float, default=BETA,
                        help='共现收缩系数（默认 0，与 full_train 相同）')
    parser.add_argument('--positive-threshold', type=float,
                        default=POSITIVE_THRESHOLD,
                        help='好评二值化阈值（默认 4.0，与 full_train 相同）')
    parser.add_argument('--polarity', choices=('pos', 'neg'), default='pos',
                        help='证据极性：pos=好评共现图（默认，部署正向路）；'
                             'neg=差评共现图（rating <= negative_threshold，'
                             'V5 负反馈罚项的证据来源）')
    parser.add_argument('--negative-threshold', type=float,
                        default=NEGATIVE_THRESHOLD,
                        help='差评二值化阈值（默认 2.0，仅 polarity=neg 生效；'
                             'EDA：<=2.0 覆盖 66%% 物品且误伤可控）')
    parser.add_argument('--run-id', default=None,
                        help='显式指定 run_id（默认 UTC 时间 + 随机短标识）')
    parser.add_argument('--edge-mode', choices=('streaming', 'dict'),
                        default='streaming',
                        help='逐分区的边计算策略（默认 streaming）。'
                             'streaming=内存 O(单源候选数)，全量数据唯一可跑通的'
                             '选项；dict=老实现，内存 ∝ 分区行数，全量会被内核 '
                             'OOM killer 杀掉，仅用于小规模对拍两条路径等价')
    args = parser.parse_args(argv)

    if args.top_k <= 0:
        parser.error('--top-k must be positive')
    if not np.isfinite(args.beta) or args.beta < 0.0:
        parser.error('--beta must be finite and >= 0')
    if not np.isfinite(args.positive_threshold):
        parser.error('--positive-threshold must be finite')
    if not np.isfinite(args.negative_threshold):
        parser.error('--negative-threshold must be finite')
    if args.run_id is not None:
        M.verify_id_format(args.run_id, '--run-id')
    return args


# ---------------------------------------------------------------------------
# Spark 层：数据准备（校验 / 去重 / 共现），数学部分全部委托 itemcf_math
# ---------------------------------------------------------------------------

def read_and_validate_ratings(spark, input_path, log_fn=log):
    """读 CSV 并按 full_train.parse_rating_fields 的口径校验；违规直接失败。

    校验用 Spark 原生表达式（rlike / 范围 / 半星档），**统计违规行数并 raise**，
    不静默丢弃——与 full_train 的"报告位置并停止"同一原则（Spark 侧丢失了
    行号定位，改为给出违规计数与列分布）。
    """
    from pyspark.sql import functions as F

    raw = spark.read.csv(input_path, header=True)
    header = raw.columns
    missing = [name for name in ('userId', 'movieId', 'rating', 'timestamp')
               if name not in [c.strip() for c in header]]
    if missing:
        raise ValueError('input header missing columns: %s (got %r)'
                         % (', '.join(missing), header))

    user_bad = F.col('userId').isNull() | \
        ~F.col('userId').rlike('^[0-9]+$')
    movie_bad = F.col('movieId').isNull() | \
        ~F.col('movieId').rlike('^[0-9]+$')
    rating_num = F.col('rating').cast('double')
    rating_bad = F.col('rating').isNull() | F.isnan(rating_num) | \
        (rating_num < F.lit(RATING_MIN)) | (rating_num > F.lit(RATING_MAX)) | \
        (F.abs(rating_num / F.lit(RATING_STEP)
               - F.round(rating_num / F.lit(RATING_STEP))) > F.lit(1e-9))
    stamp_bad = F.col('timestamp').isNull() | \
        ~F.col('timestamp').rlike('^-?[0-9]+$') | \
        (F.col('timestamp').cast('long') <= F.lit(0))
    violation = user_bad | movie_bad | rating_bad | stamp_bad

    n_bad = raw.where(violation).count()
    if n_bad:
        raise ValueError(
            'input validation failed: %d invalid rating row(s) '
            '(userId/movieId must be positive decimal integers, rating in '
            '[0.5, 5.0] on half-star steps, timestamp a positive integer). '
            'Policy is reject, not silently drop.' % n_bad)

    ratings = raw.select(
        F.col('userId').cast('long').alias('userId'),
        F.col('movieId').cast('long').alias('movieId'),
        F.col('rating').cast('double').alias('rating'),
        F.col('timestamp').cast('long').alias('timestamp'),
    )
    log_fn('input rows validated OK')
    return ratings


def reject_duplicate_pairs(ratings, log_fn=log):
    """ ``(userId, movieId)`` 唯一性：重复即失败（duplicate_policy=reject）。"""
    from pyspark.sql import functions as F

    dup = (ratings.groupBy('userId', 'movieId')
                  .agg(F.count(F.lit(1)).alias('n'))
                  .where(F.col('n') > 1)
                  .persist())
    n_dup = dup.count()
    if n_dup:
        examples = dup.limit(5).collect()
        detail = ', '.join('(userId=%s, movieId=%s, rows=%s)'
                           % (r['userId'], r['movieId'], r['n'])
                           for r in examples)
        raise ValueError('duplicate (userId, movieId) pairs detected: %d pair '
                         'type(s), e.g. %s. duplicate_policy=reject (same as '
                         'full_train default); rerun with deduplicated input.'
                         % (n_dup, detail))
    dup.unpersist()
    log_fn('duplicate check OK (reject policy)')


def build_cooccurrence(spark, positives, log_fn=log):
    """正样本 -> 有序对共现 DataFrame ``(src, tgt, c)``（双向都覆盖）。"""
    from pyspark.sql import functions as F

    a = positives.alias('a')
    b = positives.alias('b')
    joined = a.join(
        b,
        (F.col('a.userId') == F.col('b.userId'))
        & (F.col('a.movieId') < F.col('b.movieId')),
    )
    # 无序对计数一次，再双向展开：c_ij 对称，每方向恰好计一次（整数，精确）。
    pairs = (joined.groupBy(F.col('a.movieId').alias('src'),
                            F.col('b.movieId').alias('tgt'))
                   .agg(F.count(F.lit(1)).alias('c'))
                   .persist())
    cooc = pairs.select('src', 'tgt', 'c').unionByName(
        pairs.select(F.col('tgt').alias('src'),
                     F.col('src').alias('tgt'),
                     F.col('c')))
    n_pairs = pairs.count()
    log_fn('cooccurrence: %d unordered positive pair type(s)' % n_pairs)
    return cooc


def make_edge_partition_fn(n_like_broadcast, beta, max_k, log_fn=log):
    """生成逐分区的边计算闭包：数学部分全部委托 itemcf_math（可测试的纯函数）。"""

    def _partition_edges(iterator):
        # executor worker 是独立 Python 进程，不继承 driver 的 sys.path——
        # 分区函数内部必须自行补路径（local[*] 模式下 worker 与 driver 同机，
        # PROJECT_ROOT 路径有效；集群模式需把项目目录加入 executorEnv.PYTHONPATH）
        import sys as _sys
        if PROJECT_ROOT not in _sys.path:
            _sys.path.insert(0, PROJECT_ROOT)
        from utils import itemcf_math as _math   # 局部 import：避免闭包按引用携带模块
        groups = {}
        for row in iterator:
            src = int(row['src'])
            tgt = int(row['tgt'])
            c = int(row['c'])
            if c <= 0 or src == tgt:
                continue
            entry = groups.get(src)
            if entry is None:
                entry = ([], [])
                groups[src] = entry
            entry[0].append(tgt)
            entry[1].append(c)

        n_like = n_like_broadcast.value
        for src in sorted(groups):
            tgts, cs = groups[src]
            if n_like.get(src, 0) <= 0:
                continue  # 与 core 一致：零好评来源相似度按 0，无边。
            kept = [(t, c) for t, c in zip(tgts, cs) if n_like.get(t, 0) > 0]
            if not kept:
                continue
            tgt_arr = np.fromiter((t for t, _ in kept), dtype=np.int64,
                                  count=len(kept))
            c_arr = np.fromiter((c for _, c in kept), dtype=np.int64,
                                count=len(kept))
            src_arr = np.full(tgt_arr.shape, src, dtype=np.int64)
            n_src = np.full(tgt_arr.shape, int(n_like[src]), dtype=np.int64)
            n_tgt = np.fromiter((int(n_like[int(t)]) for t in tgt_arr),
                                dtype=np.int64, count=tgt_arr.shape[0])
            sims = _math.similarity_from_counts(c_arr, n_src, n_tgt, beta)
            s, t, sm, c = _math.rank_and_truncate(
                src_arr, tgt_arr, sims, c_arr, max_k)
            for pos in range(len(s)):
                yield (int(s[pos]), int(t[pos]), float(sm[pos]), int(c[pos]))

    return _partition_edges


def make_streaming_edge_partition_fn(n_like_broadcast, beta, max_k,
                                     log_fn=log):
    """**流式**版逐分区边计算：内存 O(单源候选数)，与总边数无关。

    与 :func:`make_edge_partition_fn`（下称 dict 版）**数学完全等价**，
    区别只在内存策略：

    - dict 版把整个分区的候选行攒进 ``groups = {src: ([tgt...], [c...])}``，
      内存 **∝ 分区行数**（约 72B/边）。全量 32M 行时每分区 890 万行
      （``shuffle.partitions=32``）就是 0.64GB，2 核并发 1.3GB——4G 机器
      在 8 分区下会直接被内核 OOM killer 杀掉（见坑位清单第 6 条）。
    - 流式版**只保留当前正在处理的一个 src** 的候选，攒够或遇到下一个 src 就
      立刻算完 Top-K 并 yield 出去。峰值 = 单源候选数上界
      （= 物品总数 84,432）x 约 72B = **约 6MB**，与分区大小和总边数都无关。

    **正确性依赖两件事，缺一不可**：

    1. **同一 src 的全部候选行必在同一分区且连续**。前者由
       ``repartition(N, 'src')``（按 src 做 hash 分区）保证；后者由本函数
       **读入时按 src 有序**这个前提保证 —— 所以调用方**必须**在
       ``mapPartitions`` 之前加 ``sortWithinPartitions('src')``。
       若没有这个 sort，流式版会漏掉"同一 src 后续又来一行"的情况，
       静默产出**少边**的图（不报错，只少边）——这是本函数最危险的依赖。
       :func:`build_graph_edges` 负责加这个 sort。
    2. **每个 src 只 flush 一次**。用 ``current_src`` 记住当前来源，src 一变
       就把攒的那批算完并清空；循环结束后再 flush 最后一个。

    为什么不直接用 ``itertools.groupby``：它按 key 分组，但 key 变化时要
    **先拿到新 key 的第一行**才知道边界，边界那行必须留到下一轮；
    ``groupby`` 的语义天然处理不了"单行组"（它要求分组非空且要前瞻），
    在流式迭代器上写出来容易错。用显式哨兵（``current_src is None``）更直白。

    :param n_like_broadcast: ``{movieId: 好评人数}`` 的广播变量。
    :param beta: 共现收缩系数（校验与用法同 dict 版）。
    :param max_k: 每来源 Top-K。
    :returns: 逐分区边生成器，产出 ``(src, tgt, sim, c)``，与 dict 版逐条相同。

    **实现结构说明**：``yield`` 只能出现在**内层** ``_partition_edges`` 里。
    若把它写在外层（本函数体），整个函数就成了生成器函数，
    ``return _partition_edges`` 返回的是"生成器对象"而不是"函数"，
    调用方 ``mapPartitions(fn)`` 会拿到一个不可调用的东西
    （``TypeError: 'generator' object is not callable``）。
    所以这里与 :func:`make_edge_partition_fn` 保持同构：外层只做准备并
    ``return`` 内层闭包，``yield`` 全在内层。
    """
    import sys as _sys
    if PROJECT_ROOT not in _sys.path:
        _sys.path.insert(0, PROJECT_ROOT)
    from utils import itemcf_math as _math   # 局部 import：避免闭包按引用携带模块

    n_like = n_like_broadcast.value

    def _partition_edges(iterator):
        # 单源的候选缓冲。刻意用 list 而不是 array.array：这里的量级是"一个源的
        # 候选数"（上界 84,432），list 的 append 最快，且峰值只有几 MB。
        buf_tgt = []
        buf_c = []
        current_src = None

        def _flush(src):
            """把 ``buf_*`` 里的候选算成 Top-K 边并 yield 出去。"""
            if src is None or not buf_tgt:
                return
            n_src_like = int(n_like.get(src, 0))
            if n_src_like > 0:
                # 零好评目标按定义与 dict 版 / core 一致：无相似度、不落盘。
                kept = [(t, c) for t, c in zip(buf_tgt, buf_c)
                        if n_like.get(t, 0) > 0]
                if kept:
                    tgt_arr = np.fromiter((t for t, _ in kept), dtype=np.int64,
                                          count=len(kept))
                    c_arr = np.fromiter((c for _, c in kept), dtype=np.int64,
                                        count=len(kept))
                    src_arr = np.full(tgt_arr.shape, int(src), dtype=np.int64)
                    n_src = np.full(tgt_arr.shape, n_src_like, dtype=np.int64)
                    n_tgt = np.fromiter((int(n_like[int(t)]) for t in tgt_arr),
                                        dtype=np.int64, count=tgt_arr.shape[0])
                    sims = _math.similarity_from_counts(c_arr, n_src, n_tgt,
                                                        beta)
                    s, t, sm, c = _math.rank_and_truncate(
                        src_arr, tgt_arr, sims, c_arr, max_k)
                    for pos in range(len(s)):
                        yield (int(s[pos]), int(t[pos]), float(sm[pos]),
                               int(c[pos]))

        for row in iterator:
            src = int(row['src'])
            tgt = int(row['tgt'])
            c = int(row['c'])
            if c <= 0 or src == tgt:
                continue
            if src != current_src:
                # 换源：先把上一源算完（生成器，调用方消费时才真正执行），再起新源。
                for edge in _flush(current_src):
                    yield edge
                del buf_tgt[:]
                del buf_c[:]
                current_src = src
            buf_tgt.append(tgt)
            buf_c.append(c)

        # 最后一个源（循环外 flush：否则它永远不会遇到"下一个 src"）
        for edge in _flush(current_src):
            yield edge

    return _partition_edges


def build_graph_edges(spark, cooc, n_like_map, beta, max_k, log_fn=log,
                      streaming=True):
    """按 source 重分区后逐分区调用纯函数；返回缓存好的边 DataFrame。

    :param streaming: ``True``（默认）走 :func:`make_streaming_edge_partition_fn`
        （内存 O(单源候选数)）；``False`` 走 dict 版（内存 ∝ 分区行数）。
        两者数学等价，只在能跑通与跑不动上有区别——保留 dict 版是为了
        **对拍**：小规模数据集上两条路径产出必须逐边相等（见
        ``tests/test_spark_itemcf_core.py``），流式版依赖 sort 的前提也就
        有了回归保护。
    """
    from pyspark.sql import functions as F
    n_like_broadcast = spark.sparkContext.broadcast(dict(n_like_map))
    if streaming:
        partition_fn = make_streaming_edge_partition_fn(
            n_like_broadcast, beta, max_k, log_fn=log_fn)
    else:
        partition_fn = make_edge_partition_fn(
            n_like_broadcast, beta, max_k, log_fn=log_fn)
    # 同一 source 的全部候选行经 hash 重分区落在同一分区，Top-K 不跨分区截断。
    #
    # sortWithinPartitions('src') 是**流式版的硬前提**：
    # 流式分区函数靠"读入时 src 有序"来判断一个源何时结束（见
    # make_streaming_edge_partition_fn 的 docstring）。缺了它，同一 src 的
    # 候选会被切成多段、每段各自做一次 Top-K，产出**少边**的图且不报错。
    # 代价是每个分区内多一次排序（外部排序，会 spill），换来内存与总量解耦。
    # sortWithinPartitions 是 DataFrame API（RDD 没有这个方法）：在重分区后
    # 的 DF 上做分区内排序，再转 RDD 喂给流式分区函数
    cooc_sorted = (cooc
                   .repartition(cooc.rdd.getNumPartitions(), 'src')
                   .sortWithinPartitions(F.col('src')))
    edges_rdd = cooc_sorted.rdd.mapPartitions(partition_fn)
    edges = spark.createDataFrame(edges_rdd,
                                  ['src', 'tgt', 'sim', 'c']).persist()
    n_edges = edges.count()
    log_fn('neighbor edges after top-k: %d%s'
           % (n_edges, ' (streaming)' if streaming else ' (dict)'))
    return edges



# ---------------------------------------------------------------------------
# driver 侧装配 + model_io 落盘
# ---------------------------------------------------------------------------

def collect_edges_to_arrays(edges):
    """流式收集边到 driver（toLocalIterator，峰值内存 = 一个分区）。"""
    src_buf = array.array('q')
    tgt_buf = array.array('q')
    sim_buf = array.array('d')
    c_buf = array.array('q')
    for row in edges.toLocalIterator():
        src_buf.append(int(row['src']))
        tgt_buf.append(int(row['tgt']))
        sim_buf.append(float(row['sim']))
        c_buf.append(int(row['c']))
    arrays = {
        'sources': np.frombuffer(src_buf, dtype=np.int64),
        'targets': np.frombuffer(tgt_buf, dtype=np.int64),
        'sims': np.frombuffer(sim_buf, dtype=np.float64),
        'coocs': np.frombuffer(c_buf, dtype=np.int64),
    }
    for name in ('sources', 'targets', 'sims', 'coocs'):
        arrays[name] = np.array(arrays[name])  # 拷贝成可写数组
    return arrays


def assemble_model_arrays(edge_arrays, item_ids):
    """movieId 边 -> 模型契约数组（下标 CSR，全局按契约排序）。"""
    item_index = dict((int(item), pos) for pos, item in enumerate(item_ids))
    n_edges = len(edge_arrays['sources'])
    src_pos = np.fromiter((item_index[int(s)] for s in edge_arrays['sources']),
                          dtype=np.int64, count=n_edges)
    tgt_pos = np.fromiter((item_index[int(t)] for t in edge_arrays['targets']),
                          dtype=np.int64, count=n_edges)
    sims = np.asarray(edge_arrays['sims'], dtype=np.float64)
    coocs = np.asarray(edge_arrays['coocs'], dtype=np.int64)

    # 全局排序键 = (source 升序, -sim, target movieId 升序)。item_ids 数值升序
    # 使得"target 下标升序"与"target movieId 升序"等价；分区内的顺序本已正确，
    # 这里整体重排一次保证 CSR 的来源区间语义，且不改变契约序。
    order = np.lexsort((tgt_pos, -sims, src_pos))
    src_pos = src_pos[order]
    tgt_pos = tgt_pos[order]
    sims = sims[order]
    coocs = coocs[order]

    indptr = itemcf_math.build_indptr(src_pos, len(item_ids))
    return {
        'item_ids': np.asarray(item_ids, dtype=np.int64),
        'indptr': indptr,
        'target_indices': tgt_pos,
        'similarities': sims,
        'cooccurrences': coocs,
    }


def run_job(spark, args, log_fn=log):
    """主流程：读 -> 校验 -> 共现 -> 边 -> 装配 -> 落盘。"""
    from pyspark.sql import functions as F

    run_id = args.run_id or make_run_id()
    model_id = M.compute_model_id(run_id)
    out_dir = resolve_out_dir(args.out, run_id)
    os.makedirs(out_dir)
    log_fn('run_id=%s model_id=%s input=%s out=%s'
           % (run_id, model_id, args.input, out_dir))

    ratings = read_and_validate_ratings(spark, args.input, log_fn=log_fn)
    ratings = ratings.persist()
    reject_duplicate_pairs(ratings, log_fn=log_fn)

    n_input_rows = ratings.count()
    n_users = ratings.select('userId').distinct().count()
    if args.polarity == 'neg':
        evidence = ratings.where(
            F.col('rating') <= float(args.negative_threshold)
        ).select('userId', 'movieId').persist()
    else:
        evidence = ratings.where(
            F.col('rating') >= float(args.positive_threshold)
        ).select('userId', 'movieId').persist()
    n_positive_rows = evidence.count()
    log_fn('rows=%d users=%d polarity=%s evidence_rows=%d (threshold=%r)'
           % (n_input_rows, n_users, args.polarity, n_positive_rows,
              args.negative_threshold if args.polarity == 'neg'
              else args.positive_threshold))

    item_ids = [int(r[0]) for r in
                ratings.select('movieId').distinct()
                       .orderBy('movieId').collect()]
    n_items = len(item_ids)
    if n_items == 0:
        raise ValueError('input has no valid ratings; cannot build a model')

    n_all_map = dict((int(r['movieId']), int(r['n_all']))
                     for r in ratings.groupBy('movieId')
                                     .agg(F.count(F.lit(1)).alias('n_all'))
                                     .collect())
    n_like_map = dict((int(r['movieId']), int(r['n_like']))
                      for r in evidence.groupBy('movieId')
                                       .agg(F.count(F.lit(1))
                                            .alias('n_like')).collect())
    n_zero_like = sum(1 for item in item_ids if item not in n_like_map)
    log_fn('items=%d (zero-like=%d)' % (n_items, n_zero_like))

    cooc = build_cooccurrence(spark, evidence, log_fn=log_fn)
    edges = build_graph_edges(spark, cooc, n_like_map, args.beta, args.top_k,
                              log_fn=log_fn,
                              streaming=(args.edge_mode == 'streaming'))

    edge_arrays = collect_edges_to_arrays(edges)
    graph_arrays = assemble_model_arrays(edge_arrays, item_ids)
    n_edges = len(graph_arrays['target_indices'])
    log_fn('assembled CSR: items=%d edges=%d' % (n_items, n_edges))

    n_like = np.fromiter((n_like_map.get(item, 0) for item in item_ids),
                         dtype=np.int64, count=n_items)
    n_all = np.fromiter((n_all_map.get(item, 0) for item in item_ids),
                        dtype=np.int64, count=n_items)
    model = M.NeighborModel(
        item_ids=graph_arrays['item_ids'],
        indptr=graph_arrays['indptr'],
        target_indices=graph_arrays['target_indices'],
        similarities=graph_arrays['similarities'],
        cooccurrences=graph_arrays['cooccurrences'],
        n_like=n_like,
        n_all=n_all,
    )
    problems, struct_stats = M.validate_arrays(model, max_k=args.top_k)
    if problems:
        raise ValueError('structural validation failed: %s' % '; '.join(problems))

    input_sha = file_sha256(args.input)
    signature_payload = {
        'input_sha256': input_sha,
        'input_rows': int(n_input_rows),
        'n_items': int(n_items),
        'n_users': int(n_users),
        'polarity': args.polarity,
        'positive_threshold': float(args.positive_threshold),
        'negative_threshold': float(args.negative_threshold),
        'max_k': int(args.top_k),
        'beta': float(args.beta),
        'engine': 'pyspark',
    }
    graph_signature = hashlib.sha256(
        json.dumps(signature_payload, sort_keys=True).encode('utf-8')).hexdigest()

    metadata = M.build_metadata(
        run_id=run_id,
        source={
            'path': os.path.abspath(args.input),
            'sha256': input_sha,
            'rows': int(n_input_rows),
            'users': int(n_users),
            'items': int(n_items),
            'evidence_rows': int(n_positive_rows),
            'items_zero_like': int(n_zero_like),
            'engine': 'pyspark',
        },
        parameters={
            'positive_threshold': float(args.positive_threshold),
            'max_k': int(args.top_k),
            'beta': float(args.beta),
            'engine': 'pyspark',
        },
        extra={'graph_signature': graph_signature,
               'structure_stats': struct_stats},
    )
    model_dir = os.path.join(out_dir, 'model')
    saved = model.save(model_dir, metadata)
    hash_ok = M.verify_hashes(model_dir, saved)
    bad = sorted(name for name, ok in hash_ok.items() if not ok)
    if bad:
        raise ValueError('model file hash mismatch after write: %s'
                         % ', '.join(bad))
    # 全部检查通过才升级状态（与 full_train 相同的状态机；不代表线上 active）。
    saved['status'] = M.STATUS_VALIDATED_UNPUBLISHED
    saved['validation'] = {
        'hash_verified': True,
        'structure_problems': [],
        'structure_stats': struct_stats,
        'validated_at': M.utc_now_iso(),
        'note': 'validated_unpublished means the files are self-consistent '
                'and loadable. It does NOT mean the model is active online.',
    }
    saved['run_id'] = run_id
    saved['model_id'] = model_id
    M.write_json_atomic(os.path.join(model_dir, M.METADATA_FILE), saved)

    # 最小 run_status.json：让 experiments.itemcf.verify_full_model --run-dir
    # 能直接找到 run_id / model_id / 输入路径。
    M.write_json_atomic(os.path.join(out_dir, 'run_status.json'), {
        'status': 'success',
        'run_id': run_id,
        'model_id': model_id,
        'input': os.path.abspath(args.input),
        'engine': 'pyspark',
        'polarity': args.polarity,
        'counts': {
            'input_rows': int(n_input_rows),
            'users': int(n_users),
            'items': int(n_items),
            'edges': int(n_edges),
            # 键名保留 positive_pairs 以兼容 verify_full_model 的读取；
            # polarity=neg 时语义为"差评证据行数"
            'positive_pairs': int(n_positive_rows),
        },
        'parameters': metadata['parameters'],
        'model_dir': 'model',
    })
    log_fn('done: model=%s edges=%d status=%s'
           % (model_dir, n_edges, saved['status']))
    return {'run_id': run_id, 'model_id': model_id, 'model_dir': model_dir,
            'n_edges': n_edges}


def main(argv=None):
    args = parse_args(argv)
    # 延迟导入：本地无 PySpark 时仍可 import 本模块做静态检查/复用工具函数。
    from pyspark.sql import SparkSession

    spark = (SparkSession.builder
             .appName('ItemCF-Training')
             # 分区数 = 32，不是随手填的：build_graph_edges 的
             # make_edge_partition_fn 把整个分区的候选边攒进一个纯 Python
             # dict（{src: ([tgt...], [c...])}）才做 Top-100 截断，内存与
             # **分区内的行数**成正比（约 72B/边）。
             #
             # 实测（2026-10-07，VM 2 核 4G，全量 32M 行）：共现 distinct 无序对
             # 142,418,942 条，对称展开后 2.85 亿行进 mapPartitions。
             #   partitions=8  -> 每分区 3560 万行 -> dict 2.56 GB/分区，
             #                    2 核并发峰值 ~5.1 GB > 4 GB 物理内存
             #                    => 内核 OOM killer 在 17:48:50 杀掉 driver
             #   partitions=32 -> 每分区 890 万行 -> dict 0.64 GB/分区，
             #                    2 核并发峰值 ~1.3 GB，4G 机器有余量
             #
             # 分区数**只影响内存，不影响结果**：repartition(N, 'src') 保证同一
             # src 的全部候选边落在同一分区，Top-K 截断不跨分区，结果与分区数无关
             # （N >= 1 即等，见 tests/test_spark_itemcf_core.py）。
             # 别再改回 8。
             .config('spark.sql.shuffle.partitions', '32')
             .getOrCreate())
    spark.sparkContext.setLogLevel('WARN')
    try:
        run_job(spark, args)
    finally:
        spark.stop()


if __name__ == '__main__':
    main()
