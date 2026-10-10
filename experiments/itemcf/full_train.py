# -*- coding: utf-8 -*-
"""ItemCF 全量训练入口：流式校验 → 磁盘 CSR → 分块**向量化**构图 → 模型装配。

对应计划书 §4（模型固定规则与产物契约）、§5.B（低内存精确构图）、§6（CLI）。

为什么是这个形状
================

上一轮 ``confirm.py`` 的实测瓶颈（Lead 在 VM 上逐块测过，见
``out/full/FEASIBILITY_ANALYSIS.md`` §2.2）：

===========================  ==========  ======
阶段                          前 269 块   占比
===========================  ==========  ======
稀疏乘积 ``X[:, s:stop].T @ X``   2.80 s    5%
**Python 逐条迭代 coo 元素**     **52.72 s**  **95%**
===========================  ==========  ======

``python / sparse ≈ 19x``。所以本模块的 top-k 提取**全部用 NumPy 按块向量化**，
禁止 ``for r, c, v in zip(coo.row, coo.col, coo.data)``。参考实现
:func:`reference_neighbors_python` 仍然保留——它是**测试用的独立慢路径**，
只在小数据上跑，用来证明向量化路径与逐条语义等价；它**不在任何生产路径上**。

上一轮另外两个错误，本轮显式避免：

1. **整张 Python dict 图**（``core.build_neighbors`` 返回 dict → ``by_source``
   dict → 逐条 ``array.append``）。本轮直接产出 NumPy 数组，落盘即 CSR。
2. **产物内 run_id 写成通用 ``'run'``**（因为取了 ``os.path.basename(out)``）。
   本轮 ``run_id`` 由 :func:`make_run_id` 生成、目录名即 run_id、
   ``model.json`` 里的 ``run_id`` / ``model_id`` 与之一致，并由
   :func:`assembled_ids` 集中产出，**三重一致是可测的不变式**。

内存纪律
========

31.6M 行评分不能读成 Python 四元组。流程是：

1. **第一遍扫描**：只累加每用户行数（``array('l')``）与每物品 ``n_all`` /
   ``n_like``（``array('q')``），同时建立 item 的 movieId→pos 映射
   （dict，约 8.8 万项）。还要记录每用户是否需要置位：用户是**流式分配**下标的，
   第 ``u`` 个新用户的下标就是 ``u``，所以只要在第一次见到某用户时记下
   (下标 → 输入行号)，就能在第二遍只对"该用户的第一行"做置位——**不需要
   全量 pair 集合**。
2. **第二遍扫描**：把好评 pair 落到一份**磁盘 mmap CSR**（行按用户下标，
   列已排序）。写之前按**磁盘归并排序**产出（不用 ``np.lexsort`` + 内存 concat：
   交叉块用户的第二段不可能与前面已 flush 的部分合并排序，除非全量驻留内存）。
3. 列块共现直接走这份 mmap：``X[:, s:stop].T @ X``。mmap 会增加 RSS，
   所以整程都必须在 :mod:`experiments.itemcf.resource_guard` 监督下跑。

数学口径（计划书 §4，不得改）
=============================

``X[u,i] = 1`` 当且仅当用户 u 给电影 i 的评分 ``>= positive_threshold``（默认 4.0）；
电影全集是全部合法评分出现过的电影（含零好评电影，它们的来源区间为空）：:

    n_like(i) = sum_u X[u,i]
    c(i,j)    = sum_u X[u,i] * X[u,j]
    s(i,j)    = c(i,j) / sqrt(n_like(i) * n_like(j))

- 对角线为 0；零共现或零好评支持度 → 相似度 0；**只存正相似邻居**；
- 每来源按 ``(-s, target_movieId)`` 保留最多 ``max_k``（默认 100）条；
- **不做双向补边、不热门补齐、不删冷门候选、不截断用户历史、不抽样共现**；
- 计数 ``int64``、相似度 ``float64``；**禁止 bool/uint8 共现计数**；
- 归一化用**全局 ``n_like``**。按用户批次独立归一化再拼图是错的——共现是跨块
  的量，必须在完整矩阵上算。

监督（计划书 §5.A）
===================

``resource_guard`` 是**硬依赖**：import 失败直接退出，**没有"无保护也能跑"的
回落分支**。上一轮的失败根源正是"采样由主计算流程调用"导致 492.5 秒零采样，
3 GiB 终止线在实际构图期无法触发。所以本轮的原则是：**没有监督就不许跑**。

CLI 按计划书 §6，默认启用监督、先预检后训练，**不提供默认绕过保护的开关**。

Python 3.6 兼容：无 f-string / dataclasses / walrus / ``from __future__ import
annotations`` / ``subprocess.run(capture_output=)`` / dict 合并 ``|``。
"""
import argparse
import array
import csv
import hashlib
import json
import os
import random
import sys
import time
import traceback

import numpy as np
from scipy import sparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import model_io as M  # noqa: E402
# ``conclusion.md`` 的渲染器（计划书 §6 要求的最后一项产物）。单独一个模块，
# 便于在训练结束后重生成（例如先跑 verify_full_model.py 再补结论）。
from experiments.itemcf import conclusion_writer as C  # noqa: E402

# 硬依赖：监督缺失即 ImportError，**没有回落分支**。
# 上一轮的失败根源正是"采样由主计算流程调用"导致 492.5 秒零采样、3 GiB 终止线
# 在构图期无法触发。所以本轮的原则是：**没有监督就不许跑**，宁可 import 失败。
from experiments.itemcf.resource_guard import (  # noqa: E402
    GuardedRun,
    ResourceGuard,
    STAGES,
)
from experiments.itemcf import resource_guard as _guard_mod  # noqa: E402

# ---------------------------------------------------------------------------
# 固定常量
# ---------------------------------------------------------------------------

#: 好评二值化阈值默认值（计划书 §4）。
POSITIVE_THRESHOLD = 4.0

#: 每来源最大邻居数（计划书固定 k = max_k = 100）。
MAX_K = 100

#: 共现收缩系数（计划书固定 beta = 0，即不收缩）。
BETA = 0.0

#: 默认列块大小（计划书 §6 示例）。
DEFAULT_BLOCK_SIZE = 128

#: 监督采样间隔（计划书 §5.A：每 2 秒）。
DEFAULT_MONITOR_INTERVAL = 2.0

#: 三段预算（计划书 §6 示例）。
DEFAULT_PREPARE_BUDGET_SECONDS = 3600.0
DEFAULT_PRECHECK_BUDGET_SECONDS = 1800.0
DEFAULT_TRAIN_BUDGET_SECONDS = 14400.0
DEFAULT_VERIFY_BUDGET_SECONDS = 1800.0

#: 输入扫描块行数（只为限制 dict 增长，不影响结果）。
DEFAULT_SCAN_ROWS = 1 << 20

#: 分块边文件写到第几个后 fsync + 更新块索引。
BLOCK_FLUSH_EVERY = 16

#: 环境预检门槛（计划书 §5.C）：可用磁盘至少 10 GiB，另留 2 GiB 余量。
MIN_FREE_DISK_BYTES = 10 * 1024 * 1024 * 1024
DISK_HEADROOM_BYTES = 2 * 1024 * 1024 * 1024

#: 合法评分必须是 0.5 的半星档且落在 [0.5, 5.0]。
RATING_MIN = 0.5
RATING_MAX = 5.0
RATING_STEP = 0.5

#: 边块文件与索引的固定名字。
BLOCK_INDEX_FILE = 'graph_blocks.json'
BLOCK_DIR_NAME = 'graph_blocks'
SUCCESS_BLOCKS_FILE = 'success_blocks.json'
FAILED_BLOCKS_FILE = 'failed_blocks.json'

#: 分块边文件的 schema 版本（防止把不同来源/参数的旧块拼进来）。
BLOCK_SCHEMA_VERSION = 1

#: run 状态取值。**复用 resource_guard 的常量**，不自造（计划书 §6 要求只取明确值）。
STATUS_RUNNING = _guard_mod.STATUS_RUNNING
STATUS_SUCCESS = _guard_mod.STATUS_SUCCESS
STATUS_RESOURCE_BLOCKED = _guard_mod.STATUS_RESOURCE_BLOCKED
STATUS_RESOURCE_FAILED = _guard_mod.STATUS_RESOURCE_FAILED
STATUS_MONITOR_FAILED = _guard_mod.STATUS_MONITOR_FAILED
STATUS_CORRECTNESS_FAILED = _guard_mod.STATUS_CORRECTNESS_FAILED

#: 阶段名直接用 guard 的契约值（prepare / precheck / train / verify）。
PHASES = STAGES

# 已删除：``WORKER_ARGV_ENV``。它曾声明为"父进程用环境变量把子进程身份传给
# worker"，但**从未接通**——`resource_guard.py` 全文不含该名字，本模块也没有
# 任何 ``os.environ[...]`` 读取点。留着一个死常量会让人以为那条通道存在。
# 子进程身份现在由 :func:`worker_argv` 的 ``out_dir`` / ``run_id`` 参数**显式**
# 传递（这是 CRITICAL-1/2 的修法），不需要环境变量。


class InputValidationError(Exception):
    """输入行非法或重复。**报告位置并停止**，不悄悄丢弃、不累加。"""

    def __init__(self, message, line_number=None, raw=None):
        self.line_number = line_number
        self.raw = raw
        detail = message
        if line_number is not None:
            detail = '%s (input line %d' % (message, line_number)
            if raw is not None:
                detail += ', raw=%r' % (raw,)
            detail += ')'
        Exception.__init__(self, detail)


class BudgetExceeded(Exception):
    """阶段预算耗尽。由调用方转成 ``resource_blocked`` / ``resource_failed``。"""


class CorrectnessError(Exception):
    """正确性检查失败（块装配不一致、哈希不符等）。"""


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def file_sha256(path, chunk_size=1 << 22):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def utc_stamp():
    return time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())


def make_run_id(now=None, token=None):
    """``YYYYMMDDTHHMMSSZ-<8 位小写十六进制>``；独占新目录用。"""
    stamp = now or utc_stamp()
    if token is None:
        token = '%08x' % random.SystemRandom().getrandbits(32)
    return '%s-%s' % (stamp, token)


def resolve_out_dir(out_arg, run_id):
    """把 ``--out`` 解析成"本轮独占的新目录"。

    规则（与计划书 §6 "``<run_id>`` 在执行时生成、独占新目录"一致）：

    - ``--out`` 末段**等于** run_id → 用它本身（调用方显式钉住了目录名）；
    - ``--out`` 末段**看起来像** run_id 但**不等于** run_id → **报错**（见下）；
    - 否则 → 建 ``<out>/<run_id>``。

    **为什么"形状像但值不同"要报错（MAJOR-3）**：
      这种情况意味着调用方同时给了互相矛盾的 ``--out`` 与 ``--run-id``。
      旧实现"直接用调用方的目录名"，于是产生**静默的 ID 分叉**：
        - ``run_status.json`` / ``manifest.json`` / ``model/model.json``
          里写的是编排层的 ``args.run_id``（RUNID_B）；
        - 而 ``assembled_ids()`` 从**目录名**推出 RUNID_A，
          worker 侧产物与 ``orchestrator_status.json`` 对不上。
      实测（真实端到端，exit 0）：两个 ID 并存于同一次运行，四个终态文件俱全、
      每个文件内部都自洽，**只有把 orchestrator_status.json 拿来对照才发现不一致**。
      这正是计划书 §5.D 要防的"父 run ID / model ID / 产物内 ID 必须一致"。

      修法：与其猜用户想要哪个，不如**拒绝**并说清怎么改。
      想钉目录名就只给 ``--out``（末段即 run_id），或者让两者一致。

    三种情况都拒绝复用已有目录：本轮的块索引会把"已存在的块"判为来源不符，
    与其让用户看到一堆"参数不符"，不如一开始就拒绝。
    """
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
                '--out basename %r already looks like a run_id but differs from '
                'the effective run_id %r. This would silently split the IDs '
                'between the directory name (which the worker derives run_id '
                'from) and the orchestrator (which uses --run-id). Pass only '
                '--out <...>/%s, or make --run-id match the --out basename.'
                % (tail, run_id, tail))
    if os.path.exists(target):
        raise RuntimeError('output directory already exists, refusing to reuse: %s'
                           % target)
    return target


def run_id_from_out(out_arg):
    """若 ``--out`` 的末段已是合法 run_id 形状，就用它当 run_id（保持目录名一致）。

    这样 ``--out /x/20261004T120000Z-deadbeef`` 会得到 run_id
    ``20261004T120000Z-deadbeef``，而不是在它下面再套一层新生成的名字——
    上轮"产物内 ID 与目录名不一致"的教训之一就是这种套娃。
    """
    tail = os.path.basename(os.path.abspath(out_arg).rstrip('\\/'))
    try:
        M.verify_id_format(tail, 'out basename')
        return tail
    except ValueError:
        return None


def bytes_to_gib(value):
    return float(value) / float(1 << 30)


def free_disk_bytes(path):
    """可用磁盘字节数（3.6 上用 ``shutil.disk_usage``，退回 ``os.statvfs``）。"""
    import shutil
    usage = getattr(shutil, 'disk_usage', None)
    if usage is not None:
        return int(usage(path).free)
    stat = os.statvfs(path)
    return int(stat.f_bavail * stat.f_frsize)


def set_blas_threads_to_one():
    """把 BLAS 相关线程数压到 1（计划书 §6）。

    **必须在 import numpy 之前**由启动脚本 ``export``；本函数是在进程内兜底：
    在已有 BLAS 运行时上改这些变量对已经初始化的库**不一定生效**，所以它只
    负责把变量设好并把实际值记进日志，真正生效仍靠 ``--blas-threads 1`` 对应的
    启动约定。这里不假装它一定能改成功。
    """
    names = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
             'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
    for name in names:
        os.environ[name] = '1'
    return dict((name, os.environ.get(name)) for name in names)


class RunLogger(object):
    """同时写 ``run.log`` 与 stdout 的行记录器（每行 flush，进程被杀也留痕）。"""

    def __init__(self, path):
        self.path = path
        self.handle = open(path, 'w', encoding='utf-8')

    def __call__(self, message):
        line = '[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message)
        self.handle.write(line + '\n')
        self.handle.flush()
        try:
            sys.stdout.write(line + '\n')
            sys.stdout.flush()
        except Exception:  # pragma: no cover - stdout 被重定向时也无所谓
            pass

    def close(self):
        try:
            self.handle.close()
        except Exception:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 输入校验（逐行，带行号，不静默）
# ---------------------------------------------------------------------------

def parse_rating_fields(parts, line_number, raw=None):
    """校验并解析**一条已切分的 CSV 记录**；返回 ``(userId, movieId, rating, timestamp)``。

    规则（计划书 §3）：

    - ``userId`` / ``movieId``：合法**正整数**，必须是十进制整数字面量
      （``"1.0"`` / ``"+1"`` / ``" 1"`` 一律拒绝，不做宽松解析）；
    - ``rating``：``0.5 <= r <= 5.0`` 且是 **0.5 的整数倍**（半星档）；
    - ``timestamp``：必须是**正整数**（负或零时间戳意味着数据损坏，拒绝比容忍好）；
    - 字段数不足或为空即报错。

    非法一律抛 :class:`InputValidationError` 并带行号与原始记录。
    """
    if len(parts) < 4:
        raise InputValidationError('row has %d fields, need 4'
                                   % len(parts), line_number, raw)
    user_raw = parts[0].strip()
    item_raw = parts[1].strip()
    rating_raw = parts[2].strip()
    stamp_raw = parts[3].strip()
    if not user_raw.isdigit():
        raise InputValidationError('userId must be decimal digits, got %r'
                                   % user_raw, line_number, raw)
    if not item_raw.isdigit():
        raise InputValidationError('movieId must be decimal digits, got %r'
                                   % item_raw, line_number, raw)
    user_id = int(user_raw, 10)
    item_id = int(item_raw, 10)
    if user_id <= 0:
        raise InputValidationError('userId must be a positive integer, got %r'
                                   % user_raw, line_number, raw)
    if item_id <= 0:
        raise InputValidationError('movieId must be a positive integer, got %r'
                                   % item_raw, line_number, raw)

    try:
        rating = float(rating_raw)
    except (TypeError, ValueError):
        raise InputValidationError('rating must be numeric, got %r' % rating_raw,
                                   line_number, raw)
    if not np.isfinite(rating):
        raise InputValidationError('rating must be finite, got %r' % rating_raw,
                                   line_number, raw)
    if rating < RATING_MIN or rating > RATING_MAX:
        raise InputValidationError(
            'rating %.6g out of range [%.1f, %.1f]' % (rating, RATING_MIN,
                                                       RATING_MAX),
            line_number, raw)
    # 半星档：rating / 0.5 必须是整数（容差 1e-9 只为吸收 "4.499999999999999"
    # 这类十进制表示，不是宽松放行——0.3 依然会被拒）。
    steps = rating / RATING_STEP
    if abs(steps - round(steps)) > 1e-9:
        raise InputValidationError(
            'rating %.17g is not a half-star step (0.5 granularity)' % rating,
            line_number, raw)

    if not stamp_raw.lstrip('-').isdigit():
        raise InputValidationError('timestamp must be an integer, got %r'
                                   % stamp_raw, line_number, raw)
    timestamp = int(stamp_raw, 10)
    if timestamp <= 0:
        raise InputValidationError('timestamp must be a positive integer, got %r'
                                   % stamp_raw, line_number, raw)
    return user_id, item_id, rating, timestamp


def iter_rating_chunks(input_path, chunk_rows, log=None, repeat_policy='reject',
                       duplicate_checker=None):
    """分块迭代输入 CSV，**逐行校验**；产出 ``(rows, start_line)``。

    ``rows`` 是 ``(userId, movieId, rating, timestamp)`` 的 list，长度至多
    ``chunk_rows``；``start_line`` 是块内第一行的 1-based 行号（表头算第 1 行）。

    **用 ``csv.reader`` 而不是 ``csv.DictReader``**（Lead 在 VM 全量 32,000,204 行上
    实测：DictReader 162.3 s vs reader 20.8 s，**7.8x**；手写字节解析 71.5 s，
    反而比 reader 慢 3.4x——C 实现的 CSV 层快过 Python 层逐字段 ``int()``）。
    这里必须按**列位置**取值，所以先读表头确认列名顺序，再把索引解析出来。

    唯一性检查委托给 ``duplicate_checker``（:class:`DuplicateChecker`）：
    **不用 Python ``set`` of tuple**（全量约 1.9 GB，会撞 3 GiB 监督红线），
    而是把 ``(userId, movieId)`` 打包成 int64、按段排序后查重。
    """
    if duplicate_checker is None:
        duplicate_checker = DuplicateChecker()
    with open(input_path, newline='', encoding='utf-8') as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if header is None:
            raise InputValidationError('input has no header row', 1, None)
        columns = dict((name.strip(), pos) for pos, name in enumerate(header))
        missing = [name for name in ('userId', 'movieId', 'rating', 'timestamp')
                   if name not in columns]
        if missing:
            raise InputValidationError('input header missing columns: %s'
                                       % ', '.join(missing), 1, header)
        u_col, i_col, r_col, t_col = (columns['userId'], columns['movieId'],
                                      columns['rating'], columns['timestamp'])

        chunk = []
        chunk_start = 2  # 表头占第 1 行
        line_number = 1
        for parts in reader:
            line_number += 1
            if len(parts) < 4:
                raise InputValidationError('row has %d fields, need 4'
                                           % len(parts), line_number, parts)
            parsed = parse_rating_fields(
                [parts[u_col], parts[i_col], parts[r_col], parts[t_col]],
                line_number, parts)
            duplicate_checker.add(parsed[0], parsed[1], line_number)
            chunk.append(parsed)
            if len(chunk) >= chunk_rows:
                duplicate_checker.end_segment()
                if repeat_policy == 'reject':
                    duplicate_checker.raise_if_duplicate(line_number)
                yield chunk, chunk_start, duplicate_checker
                chunk = []
                chunk_start = line_number + 1
        if chunk:
            duplicate_checker.end_segment()
            if repeat_policy == 'reject':
                duplicate_checker.raise_if_duplicate(line_number)
            yield chunk, chunk_start, duplicate_checker


def read_input_relevant_rows(input_path, log=None, repeat_policy='reject'):
    """校验输入并返回**与构图有关的全部行**（小输入 / 对拍用）。

    返回 ``(rows, duplicate_report)``。全量 31.6M 行**必须**走
    :func:`stream_input_pass1` 两遍流式路径，不要调这个。
    """
    rows = []
    checker = DuplicateChecker()
    for chunk, _start, checker in iter_rating_chunks(
            input_path, DEFAULT_SCAN_ROWS, log=log,
            repeat_policy=repeat_policy, duplicate_checker=checker):
        rows.extend(chunk)
    return rows, checker.report()


# ---------------------------------------------------------------------------
# (userId, movieId) 唯一性：packed int64 key + 分段排序（不用 tuple set）
# ---------------------------------------------------------------------------

#: packed key 的进制。要求 ``userId`` 与 ``movieId`` 都 ``< 10**KEY_RADIX_DIGITS``。
KEY_RADIX_DIGITS = 7
KEY_RADIX = 10 ** KEY_RADIX_DIGITS

#: 每段的 packed key 条数：4M 条 int64 = 32 MB 内存，排序峰值约 3 倍。
DUPLICATE_SEGMENT_ROWS = 4 << 20


class DuplicateChecker(object):
    """``(userId, movieId)`` 唯一性检查：packed int64 key + 分段排序。

    为什么不用 Python ``set``：全量 32,000,204 条 tuple 的实测外推约 **1.9 GB**，
    再加上 CSR 与索引很容易撞上 3 GiB 监督红线。Lead 在 VM 上实测
    **numpy int64 packed key + sort 的峰值是 0.73 GB**（数组本身 256 MB），
    安全得多。

    做法：

    1. 每行算 ``key = userId * 10**7 + movieId``，断言两项都 ``< 10**7``
       （**显式校验，不默默截断**——超界就用 :data:`KEY_RADIX_DIGITS` 说明并报错，
       而不是换个 packing 让键撞上）；
    2. key 与行号都进 ``array('q')``；积满一段就对 key 排序、查**段内**重复；
    3. 段与段之间可能重复（同一 pair 分处两段），所以每段排序后把
       ``(min_key, max_key)`` 记录下来；**最终一次跨段核对**用一份"段边界摘要"+
       对重叠段重新加载比对的保守策略——但为了简单且可靠，这里采用：
       每段排序后**保留全部 key 的副本**是不行的（那就是全量驻留），所以改为
       **把每段排序结果写临时文件**，最后由 :meth:`finalize` 做一次
       k 路归并查重。峰值 = 一段的内存 + 归并游标。

    这样唯一性检查的内存与"唯一 pair 数"无关，只与段大小有关。
    """

    def __init__(self, work_dir=None, segment_rows=DUPLICATE_SEGMENT_ROWS,
                 log=None):
        self.work_dir = work_dir
        self.segment_rows = int(segment_rows)
        self.log = log
        self.keys = array.array('q')
        self.lines = array.array('q')
        self.segments = []          # [(path, count)]
        self.n_rows = 0
        self.duplicates = []        # 明细（行号 + pair），最多保留 1000 条
        self.n_duplicates = 0
        #: 已登记过的 ``(key, line_a, line_b)``，用于让 :meth:`_record_duplicate` 幂等
        #: （段内查重与跨段归并会看到同一处重复，不去重会重复计数）。
        self._seen_duplicates = set()
        self.max_user_id = 0
        self.max_movie_id = 0
        self.seconds = None
        self._started = time.time()
        self._segment_open = False
        self._segment_index = 0

    def add(self, user_id, item_id, line_number):
        """登记一行（校验 packing 边界）。"""
        if user_id >= KEY_RADIX or item_id >= KEY_RADIX:
            raise InputValidationError(
                'packing key would overflow: userId=%d movieId=%d must both be '
                '< %d (10**%d). Refusing to silently truncate or collide keys.'
                % (user_id, item_id, KEY_RADIX, KEY_RADIX_DIGITS),
                line_number, None)
        if user_id > self.max_user_id:
            self.max_user_id = user_id
        if item_id > self.max_movie_id:
            self.max_movie_id = item_id
        self.keys.append(user_id * KEY_RADIX + item_id)
        self.lines.append(line_number)
        self.n_rows += 1
        self._segment_open = True
        if len(self.keys) >= self.segment_rows:
            self.end_segment()

    def end_segment(self):
        """把当前段排序落盘（临时文件），释放内存。"""
        if not self._segment_open or not self.keys:
            return
        keys = np.frombuffer(self.keys, dtype=np.int64)
        lines = np.frombuffer(self.lines, dtype=np.int64)
        order = np.argsort(keys, kind='stable')
        keys_sorted = keys[order]
        lines_sorted = lines[order]
        # 段内重复（同一段里同一个 key 出现多次）。
        if len(keys_sorted) > 1:
            same = keys_sorted[1:] == keys_sorted[:-1]
            if np.any(same):
                positions = np.flatnonzero(same)
                for pos in positions[:1000]:
                    self._record_duplicate(int(keys_sorted[pos]),
                                           int(lines_sorted[pos]),
                                           int(lines_sorted[pos + 1]))
        path = self._write_segment(keys_sorted, lines_sorted)
        self.segments.append((path, len(keys_sorted)))
        del keys, lines, order, keys_sorted, lines_sorted
        del self.keys[:]
        del self.lines[:]
        self._segment_open = False
        self._segment_index += 1

    def _write_segment(self, keys_sorted, lines_sorted):
        if self.work_dir is None:
            import tempfile
            handle = tempfile.NamedTemporaryFile(
                prefix='dupkeys_', suffix='.bin', delete=False)
            path = handle.name
            handle.close()
        else:
            if not os.path.isdir(self.work_dir):
                os.makedirs(self.work_dir)
            path = os.path.join(self.work_dir,
                                'dupkeys_%05d.bin' % self._segment_index)
        with open(path, 'wb') as handle:
            keys_sorted.tofile(handle)
            lines_sorted.tofile(handle)
        return path

    def raise_if_duplicate(self, line_number):
        """``repeat_policy='reject'`` 时在每段结束后立刻报错（报告位置并停止）。"""
        self.end_segment()
        if self.n_duplicates:
            first = self.duplicates[0]
            pair = (first['userId'], first['movieId'])
            raise InputValidationError(
                'duplicate (userId, movieId) pair %r at input lines %s (and %d '
                'more duplicate occurrences). Duplicates are never dropped or '
                'accumulated silently.' % (pair, first['lines'],
                                           self.n_duplicates - 1),
                first['lines'][0], None)

    def _record_duplicate(self, key, line_a, line_b):
        """登记一个重复出现（同一 key 的相邻两行）。

        **必须幂等**：同一处重复会被两条路径看到——``end_segment`` 的段内查重
        （``keys_sorted[pos] == keys_sorted[pos+1]``）与 ``finalize`` 的跨段 k 路归并
        （``key == previous_key``）。段内重复在归并时仍然相邻，于是**同一对会被记两次**：
        实测 ``segment_rows=2``、``(1,10)`` 在段内出现 2 次时 ``n_duplicates`` 为 2
        而单段时只有 1，``duplicate_rows`` 里出现两条 ``lines:[1,2]`` 完全相同的记录。

        这里用 ``(key, line_a, line_b)`` 去重，保证"一处重复只计一次"。
        ``n_duplicates`` 的语义因此是**重复出现的处数**（重复对数），不是"被检测到的次数"。
        """
        marker = (int(key), min(int(line_a), int(line_b)), max(int(line_a), int(line_b)))
        if marker in self._seen_duplicates:
            return
        self._seen_duplicates.add(marker)
        self.n_duplicates += 1
        if len(self.duplicates) < 1000:
            self.duplicates.append({
                'userId': key // KEY_RADIX,
                'movieId': key % KEY_RADIX,
                'lines': [line_a, line_b],
            })

    def finalize(self):
        """跨段归并查重（段内查重已在 :meth:`end_segment` 做过）。"""
        self.end_segment()
        if len(self.segments) > 1:
            import heapq
            iterators = [self._segment_iterator(path) for path, _n in self.segments]
            previous_key = None
            previous_line = None
            for key, line in heapq.merge(*iterators):
                if key == previous_key:
                    self._record_duplicate(int(key), int(previous_line), int(line))
                previous_key = key
                previous_line = line
        self.seconds = time.time() - self._started
        return self

    @staticmethod
    def _segment_iterator(path, chunk_rows=1 << 19):
        """按段文件布局 ``[n 个 key(int64)][n 个 line(int64)]`` 流式产出 ``(key, line)``。

        **不要**用 ``np.fromfile(handle, count=chunk_rows)`` 直接读 key：``count`` 是
        "最多读这么多，读到 EOF 为止"，它**不会停在 key 块末尾**。于是当某段的 key 数
        小于 ``chunk_rows`` 时，会把后面的 line 块也读进 keys，紧接着
        ``lines = fromfile(count=keys.size)`` 读到空，
        ``keys[pos]``/``lines[pos]`` 抛 ``IndexError: index 0 is out of bounds ...``。

        实测复现（本文件的历史缺陷）：``segment_rows=2`` 的 4 行输入 -> 2 段、每段 2 个
        key -> ``finalize()`` 直接 IndexError。32,000,204 行的全量之所以没爆，只是
        因为尾段 2,640,076 ≥ 524288，**属于数据巧合而非正确性**（换行数或给小切片用就会崩）。

        修法：先从文件字节数算出本段 key 数 ``n = size // 16``（每项 int64 = 8 字节，
        key/line 各占一半），再据此精确读，读满即止。
        """
        size = os.path.getsize(path)
        if size % 16 != 0:
            raise ValueError('segment file %s has non-multiple-of-16 size %d'
                             % (path, size))
        n_keys = size // 16
        with open(path, 'rb') as handle:
            remaining = n_keys
            while remaining > 0:
                take = min(chunk_rows, remaining)
                keys = np.fromfile(handle, dtype=np.int64, count=take)
                if keys.size != take:
                    raise ValueError('segment file %s truncated: wanted %d keys, got %d'
                                     % (path, take, keys.size))
                lines = np.fromfile(handle, dtype=np.int64, count=take)
                if lines.size != take:
                    raise ValueError('segment file %s truncated: wanted %d lines, got %d'
                                     % (path, take, lines.size))
                for pos in range(keys.size):
                    yield int(keys[pos]), int(lines[pos])
                remaining -= take

    def cleanup(self):
        for path, _count in self.segments:
            try:
                os.remove(path)
            except OSError:  # pragma: no cover
                pass
        self.segments = []

    def report(self):
        return {
            'rows_checked': self.n_rows,
            'duplicate_occurrences': self.n_duplicates,
            'duplicate_rows': self.duplicates[:100],
            'duplicate_rows_truncated': max(0, self.n_duplicates - len(self.duplicates)),
            'segments': len(self.segments),
            'segment_rows': self.segment_rows,
            'packing': {'radix_digits': KEY_RADIX_DIGITS, 'radix': KEY_RADIX,
                        'max_user_id': self.max_user_id,
                        'max_movie_id': self.max_movie_id,
                        'keys_unique': self.n_duplicates == 0},
            'seconds': self.seconds,
            'method': 'numpy int64 packed key + per-segment argsort + k-way merge '
                      '(no Python set of tuples)',
        }


# ---------------------------------------------------------------------------
# 两遍流式扫描：索引 + 磁盘 CSR
# ---------------------------------------------------------------------------

class StreamingIndex(object):
    """第一遍扫描的产物：用户/物品下标 + 计数 + 逐用户置位行号。

    - ``item_ids``：``list`` of int，**全局物品全集**（升序，含零好评物品）；
    - ``item_index``：``{movieId: pos}``（约 8.8 万项）；
    - ``n_all`` / ``n_like``：``array('q')``，对齐 ``item_ids``；
    - ``user_rows``：``array('q')``，每用户总行数（下标即用户下标）；
    - ``user_like_rows``：``array('q')``，每用户好评行数；
    - ``new_user_positions``：``dict {user_pos: 1-based line number}``，即每个用户
      **第一次**出现的那一行；第二遍据此决定"这一行要不要置 1"。
    - ``input_rows`` / ``duplicate_rows``：输入规模与重复行明细（如实记录）。
    """

    def __init__(self):
        self.item_ids = []
        self.item_index = {}
        self.n_all = array.array('q')
        self.n_like = array.array('q')
        self.user_rows = array.array('q')
        self.user_like_rows = array.array('q')
        self.new_user_positions = {}
        self.user_ids = []
        self.input_rows = 0
        self.duplicate_rows = []
        self.n_duplicates = 0
        self.max_user_id = 0
        self.max_movie_id = 0
        self.seconds = None
        self._line_to_user = None
        self._duplicate_checker = None
        #: **逐行**的用户下标（``array('l')``，下标 = 0-based 行序，即行号 - 2）。
        #: 这是第二遍精确还原用户归属的依据，**不依赖"输入按 userId 分组"**。
        #: 全量 3,200 万行：Windows 上 ``array('l')`` 是 32 位 = 128 MB；
        #: Linux 上是 64 位 = 256 MB。这个大小是可接受的，而且它是"精确"的
        #: 代价——比起猜用户归属导致共现表被静默污染，这 128/256 MB 便宜得多。
        self.line_users = array.array('l')

    @property
    def n_items(self):
        return len(self.item_ids)

    @property
    def n_users(self):
        return len(self.user_ids)

    def positive_pairs(self):
        """好评 pair 总数 = ``sum(n_like)``（第二遍要写的三元组条数）。"""
        return int(sum(self.n_like))

    def line_to_user_index(self):
        """``{1-based 行号: 用户下标}``，含该用户**每一行**的行号。

        第二遍扫描靠它把输入行映射回用户下标。**注意**：早期版本只记录每个用户
        **第一次**出现的行号，并据此认为"只有首行才置位"——那是错的：用户的
        **首行往往是一个低分**，于是该用户所有非首行的高分都会被丢掉（实测 60
        个用户里 32 个首行是低分，正例从 176 掉到 98）。正确做法是**每一行**
        都映射到用户，由 :class:`DiskCSRBuilder` 按 ``(user, item)`` 去重。

        实现上不额外占内存：``new_user_positions`` 里存的是"首行行号 → 下标"，
        而一个用户连续的行号区间可以由它的**行数**推出来——但输入不保证用户成组，
        所以这里改为在第二遍扫描时**顺带记录**（见 ``run_pass2`` 的
        ``user_of_line`` 缓冲），而不是预先构造一张 3200 万项的 dict。
        """
        if self._line_to_user is None:
            self._line_to_user = dict(
                (line, pos) for pos, line in self.new_user_positions.items())
        return self._line_to_user

    def coverage(self):
        return {
            'input_rows': self.input_rows,
            'users': self.n_users,
            'items': self.n_items,
            'items_with_positive': int(sum(1 for v in self.n_like if v > 0)),
            'items_zero_like': int(sum(1 for v in self.n_like if v == 0)),
            'positive_pairs': self.positive_pairs(),
            'total_ratings': int(sum(self.n_all)),
        }

    def duplicate_report(self):
        """唯一性检查结果（含行号明细）。重复必须可见，不能被吞掉。

        注意属性名：``StreamingIndex`` 存的是 ``duplicate_rows``（由
        ``DuplicateChecker.duplicates`` 在第二遍开头拷入，见本文件 ``index.duplicate_rows
        = list(checker.duplicates)``）；``self.duplicates`` 是 ``DuplicateChecker`` 自己的
        字段，两个类都有同义字段是历史命名，**不要**在 ``StreamingIndex`` 里引用前者——
        那会 AttributeError 并让整个 run 在 finalize 阶段失败（实测崩过一次）。
        """
        duplicates = self.duplicate_rows or []
        return {
            'rows_checked': self.input_rows,
            'duplicate_occurrences': self.n_duplicates,
            'duplicate_rows_sample': duplicates[:100],
            'duplicate_rows_truncated': max(0, self.n_duplicates - len(duplicates)),
            'packing': {'radix_digits': KEY_RADIX_DIGITS, 'radix': KEY_RADIX,
                        'max_user_id': self.max_user_id,
                        'max_movie_id': self.max_movie_id},
            'method': 'numpy int64 packed key + per-segment argsort + k-way merge '
                      '(no Python set of tuples)',
            'pass': self.n_duplicates == 0,
        }


def stream_input_pass1(input_path, positive_threshold=POSITIVE_THRESHOLD,
                       log=None, repeat_policy='reject'):
    """第一遍：流式建立用户/物品下标与计数，**不保留评分行**。

    用户下标是**流式分配**的（第 k 个新用户 → 下标 k-1），与 ``sorted(user_ids)``
    的编号不同——这是刻意的：排序需要全量用户集，流式分配不需要。图本身与用户
    编号无关（共现只看"同一用户",不看下标大小），但模型里不存用户维度，所以
    没有下游依赖这个编号。**唯一需要注意的地方**：跨输入块的交错用户在这里被
    正确归并到同一个下标，所以"同用户跨块"不会算错——这正是计划书 §5.B 第 1 条
    要的。
    """
    started = time.time()
    index = StreamingIndex()
    item_index = index.item_index
    n_all = index.n_all
    n_like = index.n_like
    user_rows = index.user_rows
    user_like_rows = index.user_like_rows
    new_positions = index.new_user_positions
    user_ids = index.user_ids
    user_index = {}
    line_users = index.line_users

    if log:
        log('pass 1: streaming scan of %s (positive_threshold=%r, policy=%s)'
            % (input_path, positive_threshold, repeat_policy))

    for chunk, chunk_start, checker in iter_rating_chunks(
            input_path, DEFAULT_SCAN_ROWS, log=log, repeat_policy=repeat_policy):
        index._duplicate_checker = checker
        for offset, (user_id, item_id, rating, _ts) in enumerate(chunk):
            line_number = chunk_start + offset
            pos = user_index.get(user_id)
            if pos is None:
                pos = len(user_ids)
                user_index[user_id] = pos
                user_ids.append(user_id)
                user_rows.append(0)
                user_like_rows.append(0)
                # 每个用户第一次出现的 1-based 行号：第二遍靠它置位。
                new_positions[pos] = line_number
            user_rows[pos] += 1
            item_pos = item_index.get(item_id)
            if item_pos is None:
                item_pos = len(index.item_ids)
                item_index[item_id] = item_pos
                index.item_ids.append(item_id)
                n_all.append(0)
                n_like.append(0)
            n_all[item_pos] += 1
            if rating >= positive_threshold:
                n_like[item_pos] += 1
                user_like_rows[pos] += 1
            # 逐行记录用户下标：第二遍靠它精确还原每行的用户（不假设分组）。
            line_users.append(pos)
        index.input_rows += len(chunk)
        if log and index.input_rows % (8 * DEFAULT_SCAN_ROWS) < len(chunk):
            log('pass 1: %d rows scanned' % index.input_rows)

    index.seconds = time.time() - started
    # item_ids 必须是**升序**的 movieId（计划书 §4：``model/item_ids.npy`` 是
    # "递增 movieId"；模型契约与 core.py 的口径都依赖它，相似度平票也要按
    # movieId 而不是下标来判定）。
    #
    # 第一遍为了不给每个物品建 dict 而按**首次出现顺序**编号，而 MovieLens 的
    # ratings.csv 并不按物品分组，所以这个顺序是乱的。这里在**第二遍之前**
    # 把物品编号归一成升序：这样 pass 2 写 CSR 列、以及下游所有代码用的都是
    # 同一个（升序）编号，不需要再做一次列重映射。代价是一次
    # O(n_items log n) 排序 + 两个计数数组的搬运，n_items 约 8.8 万，可忽略。
    _sort_items_ascending(index, n_all, n_like)
    checker = index._duplicate_checker
    if checker is not None:
        # 唯一性检查的段文件还没归并完（reject 策略下早在重复那一步就停了），
        # 这里补一次 finalize，把跨段重复也查出来。
        checker.finalize()
        index.duplicate_rows = list(checker.duplicates)
        index.n_duplicates = checker.n_duplicates
        index.max_user_id = checker.max_user_id
        index.max_movie_id = checker.max_movie_id
        checker.cleanup()
    if log:
        coverage = index.coverage()
        log('pass 1 done in %.1fs: rows=%d users=%d items=%d pos_pairs=%d '
            'zero_like_items=%d duplicates=%d'
            % (index.seconds, coverage['input_rows'], coverage['users'],
               coverage['items'], coverage['positive_pairs'],
               coverage['items_zero_like'], index.n_duplicates))
        del coverage
    return index


def _sort_items_ascending(index, n_all, n_like):
    """把 ``index`` 的物品编号从"首次出现顺序"归一成 **升序 movieId**。

    就地改写 ``index.item_ids`` / ``index.item_index`` / ``index.n_all`` /
    ``index.n_like``。**必须在第二遍扫描之前调用**——第二遍用 ``item_index``
    写 CSR 的列，如果之后才改编号，CSR 的列就与 ``item_ids`` 对不上了。
    """
    if not index.item_ids:
        return index
    order = np.argsort(np.asarray(index.item_ids, dtype=np.int64), kind='stable')
    old_ids = list(index.item_ids)
    old_all = list(n_all)
    old_like = list(n_like)
    index.item_ids = [old_ids[int(p)] for p in order]
    index.n_all = array.array('q', [old_all[int(p)] for p in order])
    index.n_like = array.array('q', [old_like[int(p)] for p in order])
    index.item_index = dict((mid, pos)
                            for pos, mid in enumerate(index.item_ids))
    index.item_id_order = [int(p) for p in order]
    return index
    """给一个块里的每一行确定用户下标；返回 ``{行号: 用户下标}``。

    这是**兜底路径**，只用于 ``index.line_users`` 不可用的小输入/测试场景。
    正式全量路径走 :meth:`DiskCSRBuilder.run_pass2` 的 ``line_users`` 数组，
    它不依赖任何"用户成组"假设。

    规则（不猜）：行号命中首行映射 → 用它；否则沿用块内上一行（同一用户连续的
    情形）；都不满足则抛 :class:`CorrectnessError`——**猜错用户归属会直接污染
    共现计数，而这种错误在图上看不出来**，所以宁可停下。
    """
    mapping = {}
    last_pos = None
    last_line = None
    for offset in range(len(chunk)):
        line_number = chunk_start + offset
        pos = first_line_to_user.get(line_number)
        if pos is None:
            if last_pos is not None and last_line == line_number - 1:
                pos = last_pos
            else:
                raise CorrectnessError(
                    'input line %d cannot be attributed to a user: it is not a '
                    'user\'s first line and it is not contiguous with the '
                    'previous line inside the same chunk. Use the full-line '
                    'user array path (StreamingIndex.line_users) instead of '
                    'guessing.' % line_number)
        mapping[line_number] = pos
        last_pos = pos
        last_line = line_number
    return mapping


class DiskCSRBuilder(object):
    """第二遍：把好评 pair 落成一份**磁盘 mmap CSR**（用户 x 物品，int64）。

    为什么不能内存 concat
    ---------------------

    ``X[u, i] = 1`` 当且仅当该行是用户 ``u`` 的**第一次**出现且评分达阈值。
    于是每个用户的置位行在输入里最多出现一次，但**输入顺序不保证用户成组**
    （MovieLens 事实上是，计划书明确不假定它成立）。用流式下标分配时，一个
    用户的两次出现可能相隔整份文件；CSR 要求行内列**升序**，所以按到达顺序
    累积的三元组需要排序，而"边扫边写"做不到——除非全量驻留内存。

    解法：**分段落盘 + 磁盘归并**。每积满 ``segment_rows`` 条三元组就按
    ``(user_pos, item_pos)`` 排序、写成一段 ``.bin``（int64 三元组）。全部段写完
    后做 k 路归并（``heapq.merge``，只持有 k 个当前元素），按行聚合出最终的
    ``indptr`` / ``indices`` / ``data``。峰值内存 = 一段的大小 + k 个游标。
    """

    def __init__(self, work_dir, log=None, segment_rows=4 << 20, buffer_rows=1 << 20):
        self.work_dir = work_dir
        self.log = log
        self.segment_rows = int(segment_rows)
        self.buffer_rows = int(buffer_rows)
        self.segments = []
        self.n_pairs_written = 0
        self.n_segments = 0
        self.seconds = None

    def _segment_path(self, index):
        return os.path.join(self.work_dir, 'pairs_%05d.bin' % index)

    # -- 第二遍主体 ------------------------------------------------------

    def run_pass2(self, input_path, index, positive_threshold=POSITIVE_THRESHOLD,
                  repeat_policy='reject'):
        """流式读第二遍，把好评写成分段三元组 ``(user_pos, item_pos, 1)``。

        **置位规则**：``X[u, i] = 1`` 当且仅当用户 u 给电影 i 的评分达阈值。
        ``(userId, movieId)`` 的唯一性已由第一遍保证（reject 策略下重复直接抛错
        停止），所以每条达阈值的行**恰好**对应一个 1，直接写即可。

        **曾经踩过的坑（务必不要再改回去）**：早期版本只对"用户第一次出现的
        那一行"置位，理由是"避免同一个 (user,item) 写两次"。但那条判据完全错位
        ——用户的**首行往往是一个低分**，于是该用户所有非首行的高分都被丢掉。
        实测 60 个用户的小样本上，32 个用户首行是低分，正例从 176 掉到 98，
        而且第一遍的 ``sum(n_like)`` 十字校验会立刻抓住这个不一致。去重要按
        ``(user, item)`` 做，不是按"用户首行"做。

        用户归属：每行必须精确映射回用户下标（共现依赖它）。首行映射来自
        :meth:`StreamingIndex.line_to_user_index`，其余行按"同一用户的行在输入里
        通常连续"填充——但**不假设连续**：遇到首行就重设当前用户，遇到无法归属的
        行直接抛错，而不是猜一个。
        """
        started = time.time()
        user_buffer = array.array('q')
        item_buffer = array.array('q')
        data_buffer = array.array('q')
        item_index = index.item_index
        # reject 策略下 (user, item) 已唯一，不需要去重集合；dedupe 策略才需要。
        seen_pairs = set() if repeat_policy != 'reject' else None

        def flush_segment():
            if not data_buffer:
                return
            path = self._segment_path(self.n_segments)
            triples = np.empty((len(data_buffer), 3), dtype=np.int64)
            triples[:, 0] = np.frombuffer(user_buffer, dtype=np.int64)
            triples[:, 1] = np.frombuffer(item_buffer, dtype=np.int64)
            triples[:, 2] = np.frombuffer(data_buffer, dtype=np.int64)
            triples = triples[np.lexsort((triples[:, 1], triples[:, 0]))]
            triples.tofile(path)
            self.segments.append(path)
            self.n_pairs_written += int(triples.shape[0])
            self.n_segments += 1
            if self.log:
                self.log('pass 2: wrote segment %d (%d triples)'
                         % (self.n_segments - 1, int(triples.shape[0])))
            del triples, user_buffer[:], item_buffer[:], data_buffer[:]

        # **第二遍的唯一性检查器必须有自己的磁盘目录，且用完要清。**
        # `iter_rating_chunks` 在 `duplicate_checker=None` 时自建 `DuplicateChecker()`，
        # 而 `work_dir=None` 会让段文件落到**系统临时目录**
        # (`tempfile.NamedTemporaryFile(delete=False)`)。第二遍只负责取出好评、
        # 唯一性已由第一遍保证，所以这个 checker 是**纯副作用**，而且从没人调
        # 它的 `cleanup()`。
        # 实测（review-train 报、Lead 复核）：1600 行残留 25,600 B，精确 16 B/行；
        # 全量 32,000,204 行外推 **488.28 MiB** 漏在 /tmp 不回收。
        # `estimate_disk_bytes` 与磁盘门禁看的是 `out_dir` 所在卷，**看不到这份**。
        dup_work_dir = os.path.join(self.work_dir, 'dupkeys_pass2')
        pass2_checker = DuplicateChecker(work_dir=dup_work_dir,
                                         segment_rows=self.segment_rows,
                                         log=self.log)
        # MINOR 修复：pass 1 逐行累加的 ``index.user_like_rows`` 原本**从来没人读**——
        # 是个死数据，意味着"每用户好评数"在两遍之间**没有任何交叉校验**，
        # 只有全局 `sum(n_like)` vs nnz 这一条粗校验。
        # 这里按用户统计 pass 2 实际写入的条数并与 pass 1 对比：
        # 逐用户一致才说明"置位规则"在两遍里是同一套（例如"只对用户首行置位"
        # 那类 bug 会在这里暴露，而全局总数可能仍然对得上）。
        # 用 numpy 数组而不是 dict：n_users 量级（20 万）下内存可忽略，
        # 且计数不进 Python 对象、不受 3 GiB 红线影响。
        pass2_per_user = np.zeros(int(index.n_users), dtype=np.int64)
        try:
            for chunk, chunk_start, _checker in iter_rating_chunks(
                    input_path, DEFAULT_SCAN_ROWS, log=self.log,
                    repeat_policy=repeat_policy,
                    duplicate_checker=pass2_checker):
                # 第二遍的块与第一遍用同一个 chunk 大小（DEFAULT_SCAN_ROWS），所以
                # `chunk_start` 在两遍里对同一条输入行是同一个值；据此可以用
                # `chunk_start - 2 + offset` 索引 `index.line_users`（行号 2 = 下标 0）。
                base = chunk_start - 2
                for offset in range(len(chunk)):
                    _user_id, item_id, rating, _ts = chunk[offset]
                    if rating < positive_threshold:
                        continue
                    pos = index.line_users[base + offset]
                    item_pos = item_index[item_id]
                    if seen_pairs is not None:
                        key = (pos, item_pos)
                        if key in seen_pairs:
                            continue
                        seen_pairs.add(key)
                    user_buffer.append(pos)
                    item_buffer.append(item_pos)
                    data_buffer.append(1)
                    pass2_per_user[pos] += 1
                    if len(data_buffer) >= self.segment_rows:
                        flush_segment()
        finally:
            # 段文件必须回收：否则全量会在 out_dir 下留下约 488 MiB 的
            # dupkeys_pass2_*.bin（与系统 /tmp 的版本同量级）。
            pass2_checker.cleanup()
        flush_segment()
        # 逐用户交叉校验（只在不做 dedupe 时严格成立：dedupe 策略会主动丢弃
        # 重复对，pass 2 的计数**小于**pass 1 是预期的）。
        self.per_user_check = None
        if repeat_policy == 'reject':
            expected = np.asarray(index.user_like_rows, dtype=np.int64)
            if expected.shape[0] != pass2_per_user.shape[0]:  # pragma: no cover
                raise CorrectnessError(
                    'per-user like-count length mismatch: pass1 has %d users, '
                    'pass2 counted %d' % (expected.shape[0],
                                          pass2_per_user.shape[0]))
            disagree = int(np.count_nonzero(expected != pass2_per_user))
            self.per_user_check = {
                'users': int(expected.shape[0]),
                'disagreeing_users': disagree,
                'pass1_total': int(expected.sum()),
                'pass2_total': int(pass2_per_user.sum()),
            }
            if disagree:
                first = int(np.argmax(expected != pass2_per_user))
                raise CorrectnessError(
                    'per-user like-count disagrees between pass 1 and pass 2 for '
                    '%d user(s) (first user_pos=%d: pass1=%d pass2=%d); the '
                    'positive-matrix rule is not the same in both passes'
                    % (disagree, first, int(expected[first]),
                       int(pass2_per_user[first])))
            if self.log:
                self.log('pass 2 per-user cross-check OK: %d users, %d likes'
                         % (int(expected.shape[0]), int(expected.sum())))
        self.seconds = time.time() - started
        return self

    # -- 磁盘归并 → CSR --------------------------------------------------

    def merge_to_csr(self, n_users, n_items):
        """k 路归并所有段，产出 ``(indptr, indices, data)``（int64，列升序）。

        归并键是 ``(user_pos, item_pos)``。同 ``(user, item)`` 理论上有且仅有一条
        （置位逻辑保证），但仍然显式做一次去重：如果出现重复，那说明"用户第一次
        出现"的判据被破坏了，**这是 bug 而不是可容忍的噪声**——所以这里抛
        :class:`CorrectnessError`，而不是静默求和（求和会把二值矩阵变成计数矩阵）。
        """
        handles = [open(path, 'rb') for path in self.segments]
        try:
            iterators = [self._segment_iterator(handle) for handle in handles]
            import heapq
            merged = heapq.merge(*iterators)

            indptr = np.zeros(n_users + 1, dtype=np.int64)
            indices = array.array('q')
            row_lengths = np.zeros(n_users, dtype=np.int64)
            previous = None
            n_written = 0
            for user_pos, item_pos, value in merged:
                key = (user_pos, item_pos)
                if key == previous:
                    raise CorrectnessError(
                        'duplicate (user_pos, item_pos) %r in the positive matrix; '
                        'the "first occurrence of a user" rule was violated '
                        '(value=%r)' % (key, value))
                previous = key
                row_lengths[user_pos] += 1
                indices.append(item_pos)
                n_written += 1
            indptr[1:] = np.cumsum(row_lengths)
        finally:
            for handle in handles:
                handle.close()
        if self.log:
            self.log('pass 2 merge: %d pairs -> CSR rows=%d nnz=%d'
                     % (self.n_pairs_written, n_users, n_written))
        return indptr, indices, n_written

    @staticmethod
    def _segment_iterator(handle, chunk_rows=1 << 20):
        """把一个段文件读成 ``(user_pos, item_pos, 1)`` 的生成器（分块读）。"""
        while True:
            block = np.fromfile(handle, dtype=np.int64, count=chunk_rows * 3)
            if block.size == 0:
                return
            block = block.reshape((-1, 3))
            for row in block:
                yield int(row[0]), int(row[1]), int(row[2])

    def cleanup(self):
        for path in self.segments:
            try:
                os.remove(path)
            except OSError:  # pragma: no cover
                pass
        self.segments = []


# ---------------------------------------------------------------------------
# 向量化构图核心（本模块的重点）
# ---------------------------------------------------------------------------

def build_block_edges(X, block_start, block_stop, n_like, beta, max_k,
                      item_ids=None):
    """单个**列块**的共现 + 向量化 top-k；返回按来源排好序的边数组。

    :param X: 全局好评 CSR（用户 x 物品），``int64``。**必须在完整 X 上算**，
        不能先切列再传进来——``n_like`` 归一化用的是全局计数。
    :param block_start, block_stop: 本块的来源列区间，左闭右开。
    :param n_like: 全局好评人数（长度 = X 列数），``int64``。
    :returns: ``(sources, targets, sims, coocs)``:

        - ``sources`` / ``targets``：**全局物品下标**，``int64``；
        - ``sims``：``float64``；``coocs``：``int64``（**不是 uint8/bool**）；
        - 已按 ``(source 升序, -sim, target_movieId 升序)`` 排好；
        - 只含正相似邻居，每来源至多 ``max_k`` 条。

    实现步骤（全部 NumPy，无逐边 Python 循环）：

    1. ``C = X[:, s:stop].T @ X`` → 稀疏 ``(block_width, n_items)``，int64。
       这是 scipy 的 C 实现，5% 的耗时在这里。
    2. ``C = C.tocoo()``，取 ``row``（块内来源偏移）、``col``（全局目标下标）、
       ``data``（共现 ``c_ij``）。
    3. **掩码过滤**（向量化）：去对角线、``c <= 0``、任一端 ``n_like <= 0``。
    4. ``sim = c / sqrt(n_like[j] * n_like[i]) * shrink``；校验有限且 ``<= 1+1e-12``。
    5. **排序**：以 ``(source, -sim, target_movieId)`` 为键做一次 ``np.lexsort``。
       等价于逐来源 ``sorted(key=(-sim, target_movieId))``，但只有一次排序。
    6. **每来源取前 ``max_k``**：``np.argsort`` 换成"组内名次"的技巧——
       排序后按来源求组内序号（``arange - repeat(starts, counts)``），
       再掩码 ``rank < max_k``。仍是纯 NumPy。
    7. 丢弃零条目的来源（它们会由 ``indptr`` 表达成空区间）。

    ``beta > 0`` 时收缩系数为 ``c / (c + beta)``，**先收缩再取 top-k**（不能先按
    原始余弦取 top-k 再收缩——收缩会改变邻居顺序，计划书 §4 与 ``core.py`` 一致）。
    """
    stop = int(block_stop)
    start = int(block_start)
    if stop <= start:
        empty_i = np.zeros(0, dtype=np.int64)
        return empty_i, empty_i.copy(), np.zeros(0, dtype=np.float64), empty_i.copy()

    block = X[:, start:stop]
    coo = (block.T @ X).tocoo()
    if coo.nnz == 0:
        empty_i = np.zeros(0, dtype=np.int64)
        return empty_i, empty_i.copy(), np.zeros(0, dtype=np.float64), empty_i.copy()

    source_pos = coo.row.astype(np.int64) + start   # 全局来源下标
    target_pos = coo.col.astype(np.int64)           # 全局目标下标
    cooc = coo.data
    if cooc.dtype != np.int64:
        # 禁止静默降级成 uint8/bool：计数一律 int64。
        cooc = cooc.astype(np.int64)

    n_source = n_like[source_pos]
    n_target = n_like[target_pos]
    # 3. 掩码：对角线 / 非正共现 / 任一端无好评。三条都用布尔数组一次算完。
    keep = (source_pos != target_pos) & (cooc > 0) & (n_source > 0) & (n_target > 0)
    if not np.any(keep):
        empty_i = np.zeros(0, dtype=np.int64)
        return empty_i, empty_i.copy(), np.zeros(0, dtype=np.float64), empty_i.copy()
    if not np.all(keep):
        source_pos = source_pos[keep]
        target_pos = target_pos[keep]
        cooc = cooc[keep]
        n_source = n_source[keep]
        n_target = n_target[keep]
    del keep

    # 4. 相似度（float64；整数先转浮点，避免 int64 乘积溢出）。
    sim = cooc.astype(np.float64) / np.sqrt(
        n_source.astype(np.float64) * n_target.astype(np.float64))
    if beta > 0.0:
        shrink = cooc.astype(np.float64) / (cooc.astype(np.float64) + float(beta))
        sim = sim * shrink
    if np.any(~np.isfinite(sim)):
        raise CorrectnessError('non-finite similarity produced in block [%d, %d)'
                               % (start, stop))
    if np.any(sim > 1.0 + 1e-12):
        raise CorrectnessError('similarity above 1 + 1e-12 in block [%d, %d): '
                               'max=%.17g' % (start, stop, float(sim.max())))

    # 5. 一次 lexsort 得到 "source 升序 → 相似度降序 → 平票 target movieId 升序"。
    #    **np.lexsort 的最后一个键是主键**，且各键都按升序排。所以键序列要从
    #    最次要写到最主要：先 target_pos（次次），再 -sim，最后 source_pos（主）。
    #    `-sim` 是为了让"升序排 -sim"等价于"降序排 sim"。
    #    这里曾经把顺序写反（把 source 放最前），结果源内的邻居按相似度**升序**
    #    排列——测试里有显式的"相似度必须降序"断言兜底，别把它删掉。
    order = np.lexsort((target_pos, -sim, source_pos))
    source_pos = source_pos[order]
    target_pos = target_pos[order]
    cooc = cooc[order]
    sim = sim[order]
    del order

    # 6. 每来源取前 max_k：排序后组内名次 = 全局位置 - 该来源首条位置。
    if max_k is not None and max_k >= 0:
        # 组边界：source_pos 已升序，变化点即新来源。
        changes = np.empty(source_pos.shape, dtype=bool)
        changes[0] = True
        changes[1:] = source_pos[1:] != source_pos[:-1]
        group_start_index = np.cumsum(changes) - 1
        starts = np.flatnonzero(changes)
        group_first = starts[group_start_index]
        rank = np.arange(source_pos.shape[0], dtype=np.int64) - group_first
        keep = rank < int(max_k)
        # 相似度必须为正（只存正相似邻居）。sim > 0 恒成立，因为 c > 0 且 n > 0，
        # 但 beta 收缩后仍 > 0；这里显式断言而不是静默截断。
        keep &= sim > 0.0
        if not np.all(keep):
            source_pos = source_pos[keep]
            target_pos = target_pos[keep]
            cooc = cooc[keep]
            sim = sim[keep]
        del changes, starts, group_first, rank, keep
    else:
        pos_mask = sim > 0.0
        if not np.all(pos_mask):
            source_pos = source_pos[pos_mask]
            target_pos = target_pos[pos_mask]
            cooc = cooc[pos_mask]
            sim = sim[pos_mask]
        del pos_mask

    del block, coo
    return source_pos, target_pos, sim, cooc


def reference_neighbors_python(X, item_ids, beta, max_k, block_size=128,
                               positive_threshold=POSITIVE_THRESHOLD):
    """**测试用**的逐条 Python 参考实现（独立慢路径）。

    这是被替换掉的那种写法——逐条 ``zip(coo.row, coo.col, coo.data)``，语义就是
    ``core.build_neighbors``。它**只**用于测试：证明 :func:`build_block_edges`
    的向量化路径与逐条语义逐边等价，并作为加速比测量的基线。
    生产路径**不调用**这个函数。

    :returns: ``{source_movieId: [(target_movieId, sim, c), ...]}``，
        已按 ``(-sim, target_movieId)`` 排序并截断。
    """
    n_items = len(item_ids)
    graph = {}
    if n_items == 0 or max_k == 0:
        return graph
    X = X.tocsr().astype(np.int64)
    n_like = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)
    for start in range(0, n_items, block_size):
        stop = min(start + block_size, n_items)
        coo = (X[:, start:stop].T @ X).tocoo()
        buckets = {}
        for r, c, val in zip(coo.row, coo.col, coo.data):
            j = start + int(r)
            i = int(c)
            if j == i:
                continue
            c_ij = int(val)
            if c_ij <= 0:
                continue
            nj = int(n_like[j])
            ni = int(n_like[i])
            if nj <= 0 or ni <= 0:
                continue
            sim = c_ij / float(np.sqrt(nj * ni))
            if beta > 0.0:
                sim = sim * (c_ij / float(c_ij + beta))
            buckets.setdefault(j, []).append((i, sim, c_ij))
        for j in sorted(buckets):
            ordered = sorted(buckets[j], key=lambda e: (-e[1], item_ids[e[0]]))
            kept = []
            for i, sim, c_ij in ordered:
                if sim <= 0.0 or len(kept) >= max_k:
                    break
                kept.append((item_ids[i], float(sim), c_ij))
            graph[item_ids[j]] = kept
        del coo, buckets
    return graph


def build_graph_arrays(X, item_ids, beta=BETA, max_k=MAX_K,
                       block_size=DEFAULT_BLOCK_SIZE, log=None):
    """小规模全图向量化构图，返回**边数组**（不落盘）。

    这是给测试与小数据用的便捷入口：直接走 :func:`build_block_edges` 逐块展开，
    不做块文件落盘（正式全量路径用的是 :func:`build_all_blocks` + :class:`BlockStore`）。
    两条路径共用同一个 :func:`build_block_edges`，所以口径必然一致。

    返回 ``{'sources','targets','similarities','cooccurrences','indptr','n_like'}``。
    """
    n_items = len(item_ids)
    if block_size < 1:
        raise ValueError('block_size must be >= 1, got %r' % (block_size,))
    n_like = np.asarray(X.sum(axis=0)).ravel().astype(np.int64)
    parts = {'sources': [], 'targets': [], 'similarities': [],
             'cooccurrences': []}
    for start in range(0, n_items, block_size):
        stop = min(start + block_size, n_items)
        sources, targets, sims, coocs = build_block_edges(
            X, start, stop, n_like, beta, max_k)
        parts['sources'].append(sources)
        parts['targets'].append(targets)
        parts['similarities'].append(sims)
        parts['cooccurrences'].append(coocs)

    def _cat(chunks, dtype):
        if not chunks:
            return np.zeros(0, dtype=dtype)
        if len(chunks) == 1:
            return np.ascontiguousarray(chunks[0], dtype=dtype)
        return np.concatenate(chunks)

    arrays = dict((name, _cat(chunks, dtype)) for name, dtype, chunks in (
        ('sources', np.int64, parts['sources']),
        ('targets', np.int64, parts['targets']),
        ('similarities', np.float64, parts['similarities']),
        ('cooccurrences', np.int64, parts['cooccurrences']),
    ))
    del parts
    if len(arrays['sources']):
        counts = np.bincount(arrays['sources'], minlength=n_items)
        indptr = np.zeros(n_items + 1, dtype=np.int64)
        indptr[1:] = np.cumsum(counts.astype(np.int64))
    else:
        indptr = np.zeros(n_items + 1, dtype=np.int64)
    arrays['indptr'] = indptr
    arrays['n_like'] = n_like
    if log:
        log('graph built in memory: edges=%d' % len(arrays['sources']))
    return arrays


# ---------------------------------------------------------------------------
# 图边按块落盘（验收 + 失败留档；禁止拼入不同来源/参数的旧块）
# ---------------------------------------------------------------------------

class BlockStore(object):
    """把每个列块的边写成独立文件，并维护一份**可审计**的块索引。

    索引里每个块记录：来源区间、``edges``、``seconds``、``source_signature``
    （输入指纹 + 参数 + 源码哈希的合并摘要）、``dtypes``、``status``。

    写入前会**校验签名**：若索引里已有同 ``index`` 的块而签名不同，直接
    :class:`CorrectnessError`——这就是"禁止拼入来源/参数/源码不同的旧块"的
    落点。默认不实现复杂自动续跑：发现任何已有块就报错要求换新目录。
    """

    def __init__(self, out_dir, signature, log=None):
        self.out_dir = out_dir
        self.block_dir = os.path.join(out_dir, BLOCK_DIR_NAME)
        self.index_path = os.path.join(out_dir, BLOCK_INDEX_FILE)
        self.signature = dict(signature)
        self.log = log
        self.records = {}
        self._last_flush = 0
        if not os.path.isdir(self.block_dir):
            os.makedirs(self.block_dir)

    def _check_existing(self):
        if os.path.isfile(self.index_path):
            raise CorrectnessError(
                'block index already exists at %s; this run directory must be '
                'exclusive. Old blocks from a different run/source/parameters are '
                'never merged.' % self.index_path)

    def block_path(self, index):
        return os.path.join(self.block_dir, 'block_%06d.npz' % int(index))

    def write_block(self, index, start, stop, sources, targets, sims, coocs,
                    seconds):
        """校验 dtype/范围后落一个块文件；返回记录 dict。"""
        if sources.dtype != np.int64 or targets.dtype != np.int64:
            raise CorrectnessError('block %d: source/target dtypes must be int64'
                                   % index)
        if coocs.dtype != np.int64:
            raise CorrectnessError('block %d: cooccurrence dtype must be int64, '
                                   'got %s (bool/uint8 would overflow)'
                                   % (index, coocs.dtype))
        if sims.dtype != np.float64:
            raise CorrectnessError('block %d: similarity dtype must be float64, '
                                   'got %s' % (index, sims.dtype))
        # 来源必须升序：CSR 的「边顺序即 indptr 区间」语义以此为前提。
        # 这里在**写盘前**就拒，而不是等 load_blocks/arrays_to_model 才发现——
        # 坏块一旦落盘就会以「problems」形式存在，等装配阶段才报错会浪费整轮构图，
        # 而且掩盖了"块边界算错"的真正上游原因（与 arrays_to_model 的口径一致，
        # 两处都显式拒，不静默重排）。
        if len(sources) and np.any(sources[1:] < sources[:-1]):
            raise CorrectnessError('block %d: sources are not sorted ascending; '
                                   'CSR indptr semantics require source order'
                                   % index)
        if len(sources) and len(targets) != len(sources):
            raise CorrectnessError('block %d: targets length %d != sources length %d'
                                   % (index, len(targets), len(sources)))
        path = self.block_path(index)
        np.savez(path, sources=sources, targets=targets, similarities=sims,
                 cooccurrences=coocs, schema_version=np.int64(BLOCK_SCHEMA_VERSION))
        record = {
            'index': int(index),
            'start': int(start),
            'stop': int(stop),
            'edges': int(len(sources)),
            'seconds': round(float(seconds), 6),
            'status': 'success',
            'dtypes': {'sources': str(sources.dtype),
                       'targets': str(targets.dtype),
                       'similarities': str(sims.dtype),
                       'cooccurrences': str(coocs.dtype)},
            'file': os.path.relpath(path, self.out_dir),
            'signature': self.signature,
        }
        self.records[int(index)] = record
        if len(self.records) - self._last_flush >= BLOCK_FLUSH_EVERY:
            self.flush()
        return record

    def write_failure(self, index, start, stop, reason, seconds):
        """失败块也要留档（计划书 §5.B：失败块与成功块都有记录）。"""
        record = {
            'index': int(index), 'start': int(start), 'stop': int(stop),
            'edges': 0, 'seconds': round(float(seconds), 6),
            'status': 'failed', 'reason': str(reason), 'file': None,
            'signature': self.signature,
        }
        self.records[int(index)] = record
        return record

    def flush(self):
        payload = {
            'schema_version': BLOCK_SCHEMA_VERSION,
            'signature': self.signature,
            'blocks_written': sum(1 for r in self.records.values()
                                  if r['status'] == 'success'),
            'blocks_failed': sum(1 for r in self.records.values()
                                 if r['status'] == 'failed'),
            'blocks': [self.records[k] for k in sorted(self.records)],
        }
        M.write_json_atomic(self.index_path, payload)
        # 成功/失败分列两份便于验收（内容同源，不重复计算）。
        M.write_json_atomic(os.path.join(self.out_dir, SUCCESS_BLOCKS_FILE),
                            dict((str(k), self.records[k]['edges'])
                                 for k in sorted(self.records)
                                 if self.records[k]['status'] == 'success'))
        M.write_json_atomic(os.path.join(self.out_dir, FAILED_BLOCKS_FILE),
                            [self.records[k] for k in sorted(self.records)
                             if self.records[k]['status'] == 'failed'])
        self._last_flush = len(self.records)
        return self.index_path

    def load_blocks(self):
        """读回全部成功块并做**完整性校验**（签名、dtype、范围、块内排序）。

        返回 ``(edge_arrays, problems)``；``problems`` 非空即说明块文件不可信，
        调用方应判定 ``correctness_failed`` 而不是"凑合装配"。
        """
        problems = []
        payload = M.read_json(self.index_path)
        if payload.get('signature') != self.signature:
            problems.append('block index signature mismatch: %r != current %r'
                            % (payload.get('signature'), self.signature))
        sources_parts, targets_parts = [], []
        sims_parts, coocs_parts = [], []
        for record in payload.get('blocks') or []:
            if record['status'] != 'success':
                problems.append('block %d is marked %s'
                                % (record['index'], record['status']))
                continue
            path = os.path.join(self.out_dir, record['file'])
            if not os.path.isfile(path):
                problems.append('block %d file missing: %s'
                                % (record['index'], record['file']))
                continue
            data = np.load(path, allow_pickle=False)
            s = data['sources']
            t = data['targets']
            sim = data['similarities']
            c = data['cooccurrences']
            if s.dtype != np.int64 or t.dtype != np.int64 or c.dtype != np.int64 \
                    or sim.dtype != np.float64:
                problems.append('block %d dtype mismatch: %s/%s/%s/%s'
                                % (record['index'], s.dtype, t.dtype, sim.dtype,
                                   c.dtype))
                continue
            if int(data['schema_version']) != BLOCK_SCHEMA_VERSION:
                problems.append('block %d schema_version %r != %d'
                                % (record['index'], int(data['schema_version']),
                                   BLOCK_SCHEMA_VERSION))
                continue
            if len(s) != int(record['edges']):
                problems.append('block %d edge count %d != recorded %d'
                                % (record['index'], len(s), int(record['edges'])))
                continue
            if len(s) and np.any(s < int(record['start'])) or \
                    (len(s) and np.any(s >= int(record['stop']))):
                problems.append('block %d has sources outside [%d, %d)'
                                % (record['index'], record['start'], record['stop']))
                continue
            if len(s) and np.any(s[1:] < s[:-1]):
                problems.append('block %d sources are not sorted ascending'
                                % record['index'])
                continue
            sources_parts.append(s)
            targets_parts.append(t)
            sims_parts.append(sim)
            coocs_parts.append(c)
        if problems:
            return None, problems
        arrays = {
            'sources': (np.concatenate(sources_parts) if sources_parts
                        else np.zeros(0, dtype=np.int64)),
            'targets': (np.concatenate(targets_parts) if targets_parts
                        else np.zeros(0, dtype=np.int64)),
            'similarities': (np.concatenate(sims_parts) if sims_parts
                             else np.zeros(0, dtype=np.float64)),
            'cooccurrences': (np.concatenate(coocs_parts) if coocs_parts
                              else np.zeros(0, dtype=np.int64)),
        }
        return arrays, problems


def arrays_to_model(arrays, item_ids, n_like, n_all):
    """把边数组装配成 :class:`model_io.NeighborModel`（含 indptr 计算与一致性校验）。

    关键校验：``sources`` 必须**按来源升序**，否则 CSR 的"边顺序即 indptr 区间"
    语义不成立。这里不静默重排——静默重排会掩盖上游块边界算错。
    """
    sources = np.asarray(arrays['sources'], dtype=np.int64)
    targets = np.asarray(arrays['targets'], dtype=np.int64)
    sims = np.asarray(arrays['similarities'], dtype=np.float64)
    coocs = np.asarray(arrays['cooccurrences'], dtype=np.int64)
    n_items = len(item_ids)
    if len(sources) and np.any(sources[1:] < sources[:-1]):
        raise CorrectnessError('edge sources are not sorted ascending; CSR '
                               'indptr semantics require blocks to be written in '
                               'source order')
    if len(sources) and int(sources[-1]) >= n_items:
        raise CorrectnessError('edge source index %d out of range (n_items=%d)'
                               % (int(sources[-1]), n_items))
    counts = np.bincount(sources, minlength=n_items) if len(sources) \
        else np.zeros(n_items, dtype=np.int64)
    indptr = np.zeros(n_items + 1, dtype=np.int64)
    indptr[1:] = np.cumsum(counts.astype(np.int64))
    return M.NeighborModel(item_ids=item_ids, indptr=indptr, target_indices=targets,
                           similarities=sims, cooccurrences=coocs,
                           n_like=n_like, n_all=n_all)


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

def select_precheck_blocks(n_items, block_size, n_like):
    """计划书 §5.C：最多六块——首 / 中 / 末 + 好评支持度最高物品所在块 +
    两个按固定哈希选出的不同块。

    :returns: 排序去重后的块下标列表（可能少于 6）。
    """
    n_blocks = (n_items + block_size - 1) // block_size
    if n_blocks == 0:
        return []
    chosen = set()
    chosen.add(0)
    chosen.add((n_blocks - 1) // 2)
    chosen.add(n_blocks - 1)
    if n_items:
        peak_item = int(np.argmax(n_like))
        chosen.add(int(peak_item) // block_size)
    salt = 0
    while len(chosen) < 6 and salt < 1000:
        digest = hashlib.sha256(('itemcf-precheck-block-v1:%d' % salt)
                                .encode('utf-8')).hexdigest()
        chosen.add(int(digest[:12], 16) % n_blocks)
        salt += 1
        if len(chosen) >= min(6, n_blocks):
            break
    return sorted(chosen)


def estimate_disk_bytes(index, max_k, block_size):
    """估算中间件 + 产物的磁盘需求（计划书 §5.C 要求"估算 + 2 GiB 余量"）。

    MINOR 修复：``block_size <= 0`` 时直接抛错。旧实现会 ``ZeroDivisionError``
    （0）或算出**负数**块数（-1），后者更危险——磁盘估算会偏小，
    让"估算 + 2 GiB 余量 < 可用空间"这条门槛失效。CLI 已在校验，
    但直接调用（脚本/测试）没有保护，这里补齐。
    """
    if int(block_size) <= 0:
        raise ValueError('block_size must be a positive integer, got %r'
                         % (block_size,))
    if int(max_k) <= 0:
        raise ValueError('max_k must be a positive integer, got %r' % (max_k,))
    n_items = index.n_items
    n_users = index.n_users
    pairs = index.positive_pairs()
    n_blocks = (n_items + block_size - 1) // block_size
    worst_edges = min(pairs, n_items * max_k)
    estimate = {
        'positive_pairs_triples_bytes': pairs * 24,     # 磁盘归并的段文件
        'csr_data_bytes': pairs * 8,
        'csr_indices_bytes': pairs * 8,
        'csr_indptr_bytes': (n_users + 1) * 8,
        'edge_arrays_bytes': worst_edges * 32,          # 4 源/tgt/sim/c 各 8 字节
        'block_files_bytes': worst_edges * 32,          # 块文件与装配数组同量级
        'model_files_bytes': worst_edges * 32 + (n_items + 1) * 8 * 4,
        'n_blocks': n_blocks,
        'worst_case_edges': int(worst_edges),
    }
    estimate['total_bytes'] = int(sum(v for k, v in estimate.items()
                                      if k.endswith('_bytes')))
    return estimate


#: 预检时如果列块导致内存超限，按计划书 §5.C 自动逐级递减：128 → 64 → 32 → 16，
#: **最多一次逐级尝试**；若基本 CSR 本身超限，减列块无效，直接停止。
PRECHECK_BLOCK_LADDER = (128, 64, 32, 16)


def project_runtime(precheck, n_blocks, budget_seconds):
    """用预检各块耗时 × 预计块数给出**粗估范围**。

    计划书 §5.C 与 Lead 的实测都指出：**块难度差异极大**（前 12 块是物品 ID 最小
    = 最热门电影的块，coo_nnz 53 万~73 万，而全部 269 块平均只有 46,772，
    密度差 12.4 倍）。用"前几块"外推会高估 2.28 倍（Lead 实测：外推 1120 s vs
    实际 492.5 s）。因此：

    - 这里用**预检选出的六块**（首/中/末/最高支持度/两个哈希块）做样本，
      覆盖了难度分布的两端，而不是只用热门块；
    - 给出 **mean 与 max 两个界**，并明确写出"这不是可靠运行时上界"；
    - 判定用 ``max`` 界（保守），避免"估低了就开跑、跑到一半超预算"。
    """
    samples = [b['seconds'] for b in precheck['blocks'] if b.get('seconds') is not None]
    if not samples:
        return {
            'samples': 0, 'n_blocks': int(n_blocks),
            'mean_block_seconds': None, 'max_block_seconds': None,
            'projected_seconds_mean': None, 'projected_seconds_max': None,
            'projected_hours_mean': None, 'projected_hours_max': None,
            'budget_seconds': float(budget_seconds),
            'within_budget': False,
            'note': 'no precheck samples were collected; cannot project',
        }
    mean_s = float(sum(samples)) / float(len(samples))
    max_s = float(max(samples))
    projected_mean = mean_s * int(n_blocks)
    projected_max = max_s * int(n_blocks)
    # 装配与核验的预留：计划书 §5.C 要求计入。
    assembly_reserve = 0.15 * projected_max
    return {
        'samples': len(samples),
        'n_blocks': int(n_blocks),
        'mean_block_seconds': round(mean_s, 6),
        'max_block_seconds': round(max_s, 6),
        'min_block_seconds': round(float(min(samples)), 6),
        'projected_seconds_mean': round(projected_mean, 3),
        'projected_seconds_max': round(projected_max, 3),
        'projected_hours_mean': round(projected_mean / 3600.0, 4),
        'projected_hours_max': round(projected_max / 3600.0, 4),
        'assembly_reserve_seconds': round(assembly_reserve, 3),
        'budget_seconds': float(budget_seconds),
        'within_budget': bool(projected_max + assembly_reserve
                              <= float(budget_seconds)),
        'note': ('mean and max are both reported. The max bound plus a 15% '
                 'assembly/hash reserve is what gates the decision. Block '
                 'difficulty varies by more than an order of magnitude, so this '
                 'is a rough range, NOT a reliable runtime upper bound.'),
    }


def collect_csr_block(X, start, stop, n_like, beta, max_k):
    """构造一个列块并返回边数组 + 本块耗时（供预检与正式构图共用）。

    **共用同一个函数是关键**：预检测的就是正式阶段要跑的那段代码。如果预检用
    另一份实现，那测出来的时间不能代表正式阶段。
    """
    began = time.time()
    sources, targets, sims, coocs = build_block_edges(
        X, start, stop, n_like, beta, max_k)
    return {
        'sources': sources, 'targets': targets, 'similarities': sims,
        'cooccurrences': coocs, 'seconds': time.time() - began,
    }


def run_precheck(X, item_ids, n_like, args, log=None):
    """计划书 §5.C：在**完整矩阵**上实测最多六块，给出外推与结论。

    **不读取任何推荐效果指标**——这里只测真实共现、top-k 与写盘耗时。

    128 列因临时内存不足时可以按 :data:`PRECHECK_BLOCK_LADDER` 自动逐级递减
    重试，**每次记录失败**，最多一轮。若连基本 CSR 都已经不可用，递减没用；
    本函数通过 ``X.nnz`` 与矩阵规模判断这一点并直接给出 ``ok=False``。
    """
    n_items = len(item_ids)
    block_size = args.block_size
    chosen = select_precheck_blocks(n_items, block_size, n_like)
    blocks = []
    failed = []
    # 阶梯每一档的失败都要**留档**（MINOR 修复）。
    # 旧实现在每档开头 `failed = []`，于是降档成功后产物里只看得到
    # "最终成功那一档"的失败记录，**看不出 128 整档都跑不起来**——
    # 而这恰恰是"为什么最终用了 32"的唯一证据。现在按档累积。
    ladder_history = []
    ladder_index = 0
    ladder = [block_size] + [v for v in PRECHECK_BLOCK_LADDER if v < block_size]
    while ladder_index < len(ladder):
        size = ladder[ladder_index]
        blocks = []
        failed = []
        effective = select_precheck_blocks(n_items, size, n_like)
        for index in effective:
            start = index * size
            stop = min(start + size, n_items)
            if start >= n_items:
                continue
            try:
                result = collect_csr_block(X, start, stop, n_like, args.beta,
                                           args.max_k)
            except MemoryError as error:
                failed.append({'index': index, 'start': start, 'stop': stop,
                               'block_size': size,
                               'reason': 'MemoryError: %s' % error})
                if log:
                    log('precheck: block %d (size=%d) failed with MemoryError'
                        % (index, size))
                continue
            record = {
                'index': index, 'start': start, 'stop': stop,
                'block_size': size,
                'edges': int(len(result['sources'])),
                'seconds': round(result['seconds'], 6),
            }
            blocks.append(record)
            if log:
                log('precheck block %d [%d, %d) size=%d: %.3fs edges=%d'
                    % (index, start, stop, size, result['seconds'],
                       record['edges']))
            del result
        # 每档都记进历史（MINOR 修复）：产物里要看得出"哪几档试过、各自结果如何"。
        ladder_history.append({
            'block_size': size,
            'n_blocks_tried': len(effective),
            'succeeded': len(blocks),
            'failed': len(failed),
            'failures': failed,
        })
        if blocks:
            break
        # 全部失败 → 按阶梯降一档重试（最多一轮逐级尝试）。
        if ladder_index + 1 >= len(ladder):
            break
        if log:
            log('precheck: all %d block(s) failed at block_size=%d; retrying at %d'
                % (len(effective), size, ladder[ladder_index + 1]))
        ladder_index += 1
    ok = bool(blocks)
    reason = None
    if not ok:
        reason = ('no precheck block could be computed even at the smallest '
                  'block size %d; reducing the column block does not help, so '
                  'the full positive CSR itself does not fit this machine'
                  % ladder[-1])
    # 失败档数（用于产物里如实说明降档过程）。
    n_fully_failed_tiers = sum(1 for h in ladder_history if h['succeeded'] == 0)
    return {
        'blocks': blocks,
        'failed': failed,
        'ok': ok,
        'reason': reason,
        'block_size_used': (blocks[0]['block_size'] if blocks else None),
        'ladder': list(ladder),
        'ladder_history': ladder_history,
        'tiers_fully_failed': n_fully_failed_tiers,
    }


def write_csr_arrays(work_dir, indptr_users, indices_buffer, n_users, n_items,
                     log=None):
    """把磁盘 CSR 落成 3 个 npy 文件（mmap 用），返回描述 dict。

    存成 npy 而不是 scipy 的 ``.npz``：``np.load(mmap_mode='r')`` 能直接映射，
    而 ``npz`` 是 zip 容器、必须解压才能读，那就等于整份进内存。
    """
    if not os.path.isdir(work_dir):
        os.makedirs(work_dir)
    paths = {}
    arrays = {
        'indptr': np.asarray(indptr_users, dtype=np.int64),
        'indices': np.frombuffer(indices_buffer, dtype=np.int64).copy()
        if isinstance(indices_buffer, array.array) else
        np.asarray(indices_buffer, dtype=np.int64),
        'shape': np.asarray([n_users, n_items], dtype=np.int64),
    }
    for name, values in arrays.items():
        path = os.path.join(work_dir, 'csr_%s.npy' % name)
        np.save(path, values, allow_pickle=False)
        paths[name] = path
    if log:
        log('CSR written: shape=(%d, %d) nnz=%d files=%s'
            % (n_users, n_items, len(arrays['indices']), sorted(paths)))
    return paths


def open_csr(paths_or_dir, index, log=None):
    """mmap 打开磁盘 CSR，返回 ``scipy.sparse.csr_matrix``（int64）。

    ``csr_matrix`` 会持有一份对 mmap 数组的引用（不是拷贝），所以 ``X`` 本身
    很小；真正的内存开销出现在 ``X[:, s:stop]`` 切片与 ``.T @ X`` 的临时量上——
    那部分才是监督要盯的。
    """
    if isinstance(paths_or_dir, dict):
        paths = paths_or_dir
    else:
        paths = dict((name, os.path.join(paths_or_dir, 'csr_%s.npy' % name))
                     for name in ('indptr', 'indices', 'shape'))
    for name in ('indptr', 'indices', 'shape'):
        if not os.path.isfile(paths[name]):
            raise CorrectnessError('missing CSR file: %s' % paths[name])
    indptr = np.load(paths['indptr'], mmap_mode='r')
    indices = np.load(paths['indices'], mmap_mode='r')
    shape = np.load(paths['shape'])
    if tuple(int(v) for v in shape) != (index.n_users, index.n_items):
        raise CorrectnessError('CSR shape %r != expected (%d, %d)'
                               % (tuple(int(v) for v in shape), index.n_users,
                                  index.n_items))
    if int(indptr[-1]) != int(indices.shape[0]):
        raise CorrectnessError('CSR indptr[-1] = %d != nnz = %d'
                               % (int(indptr[-1]), int(indices.shape[0])))
    matrix = sparse.csr_matrix(
        (np.ones(int(indices.shape[0]), dtype=np.int64), indices, indptr),
        shape=(int(shape[0]), int(shape[1])), dtype=np.int64)
    if log:
        log('CSR opened via mmap: shape=%r nnz=%d' % (matrix.shape, matrix.nnz))
    return matrix


def load_csr_for_blocks(csr_path, index):
    """正式构图前重新 mmap 打开 CSR（与预检同一份文件，保证口径一致）。"""
    return open_csr(csr_path, index)


def build_all_blocks(X, item_ids, n_like, args, store, log=None):
    """正式阶段：逐列块构图、落块文件、返回交错排好的边数组。

    与预检共用 :func:`collect_csr_block`，所以"预检测出来的时间"就是"正式阶段
    要付的时间"。块按 ``source`` 下标升序处理，因此拼起来的 ``sources`` 天然
    升序，满足 CSR 的 ``indptr`` 语义（:func:`arrays_to_model` 会复核）。
    """
    n_items = len(item_ids)
    block_size = args.block_size
    n_blocks = (n_items + block_size - 1) // block_size
    parts = {'sources': [], 'targets': [], 'similarities': [],
             'cooccurrences': []}
    problems = []
    for index in range(n_blocks):
        start = index * block_size
        stop = min(start + block_size, n_items)
        try:
            result = collect_csr_block(X, start, stop, n_like, args.beta,
                                       args.max_k)
        except MemoryError as error:
            store.write_failure(index, start, stop,
                                'MemoryError: %s' % error, 0.0)
            problems.append('block %d raised MemoryError: %s' % (index, error))
            continue
        except Exception as error:  # noqa: BLE001 - 失败块也要留档
            store.write_failure(index, start, stop,
                                '%s: %s' % (type(error).__name__, error), 0.0)
            problems.append('block %d failed: %s: %s'
                            % (index, type(error).__name__, error))
            continue
        store.write_block(index, start, stop, result['sources'],
                          result['targets'], result['similarities'],
                          result['cooccurrences'], result['seconds'])
        parts['sources'].append(result['sources'])
        parts['targets'].append(result['targets'])
        parts['similarities'].append(result['similarities'])
        parts['cooccurrences'].append(result['cooccurrences'])
        if log and (index + 1) % 32 == 0:
            log('train blocks %d/%d done' % (index + 1, n_blocks))
        del result
    store.flush()
    if problems:
        return None, problems

    def _cat(chunks, dtype):
        if not chunks:
            return np.zeros(0, dtype=dtype)
        if len(chunks) == 1:
            return np.ascontiguousarray(chunks[0], dtype=dtype)
        return np.concatenate(chunks)

    arrays = dict((name, _cat(chunks, dtype)) for name, dtype, chunks in (
        ('sources', np.int64, parts['sources']),
        ('targets', np.int64, parts['targets']),
        ('similarities', np.float64, parts['similarities']),
        ('cooccurrences', np.int64, parts['cooccurrences']),
    ))
    del parts
    return arrays, []


# ---------------------------------------------------------------------------
# 结果契约
# ---------------------------------------------------------------------------

def assembled_ids(out_dir):
    """从产物目录名推出 ``(run_id, model_id)``，并要求两者都是真值形状。

    上轮把 ``run_id`` 写成通用 ``'run'``（``os.path.basename(out)`` 取了子目录
    名）。这里的做法相反：**目录名就是 run_id**，格式由 :data:`model_io.ID_PATTERN`
    约束，``model_id`` 由 :func:`model_io.compute_model_id` 唯一换算。
    """
    run_id = os.path.basename(os.path.abspath(out_dir))
    M.verify_id_format(run_id, 'run_id (from output directory name)')
    return run_id, M.compute_model_id(run_id)


def source_signature(index, args, source_sha256):
    """块的来源签名：输入指纹 + 构图参数 + 源码哈希。

    这是"禁止拼入来源/参数/源码不同的旧块"的执行依据——签名不同就是不同实验。
    """
    payload = {
        'input_sha256': source_sha256,
        'input_rows': index.input_rows,
        'n_items': index.n_items,
        'n_users': index.n_users,
        'positive_threshold': float(args.positive_threshold),
        'max_k': int(args.max_k),
        'beta': float(args.beta),
        'block_size': int(args.block_size),
        'source_sha256': source_hashes_of_modules(),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8'))
    payload['signature'] = digest.hexdigest()
    return payload


def source_hashes_of_modules():
    """本 run 依赖的源码文件哈希（用于签名与"源码未漂移"核对）。

    必须涵盖**所有会影响产物内容的模块**。``conclusion_writer.py`` 是后加的
    依赖（``finalize`` 用它渲染 ``conclusion.md``），漏掉它的话：改了结论渲染器
    而源码指纹不变，"源码未漂移"的核对就会漏过这次改动。
    """
    names = ('full_train.py', 'model_io.py', 'verify_full_model.py', 'core.py',
             'resource_guard.py', 'conclusion_writer.py')
    hashes = {}
    for name in names:
        path = os.path.join(MODULE_DIR, name)
        if os.path.isfile(path):
            hashes[name] = file_sha256(path)
    return hashes


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m experiments.itemcf.full_train',
        description='ItemCF full-training run: supervised, precheck-first, '
                    'vectorized block construction (no bypass switch).')
    parser.add_argument('--input', required=True,
                        help='完整评分 CSV（VM: /home/h/movielens/ratings.csv）')
    parser.add_argument('--out', required=True,
                        help='产物目录；末段等于 run_id 时直接用，否则建 <out>/<run_id>')
    parser.add_argument('--max-k', type=int, default=MAX_K,
                        help='每来源最大邻居数（默认 100）')
    parser.add_argument('--beta', type=float, default=BETA,
                        help='共现收缩系数（默认 0）')
    parser.add_argument('--positive-threshold', type=float,
                        default=POSITIVE_THRESHOLD,
                        help='好评二值化阈值（默认 4.0）')
    parser.add_argument('--block-size', type=int, default=DEFAULT_BLOCK_SIZE,
                        help='列块大小（默认 128）')
    parser.add_argument('--monitor-interval', type=float,
                        default=DEFAULT_MONITOR_INTERVAL,
                        help='监督采样间隔秒（默认 2.0）')
    parser.add_argument('--prepare-budget-seconds', type=float,
                        default=DEFAULT_PREPARE_BUDGET_SECONDS)
    parser.add_argument('--precheck-budget-seconds', type=float,
                        default=DEFAULT_PRECHECK_BUDGET_SECONDS)
    parser.add_argument('--train-budget-seconds', type=float,
                        default=DEFAULT_TRAIN_BUDGET_SECONDS)
    parser.add_argument('--verify-budget-seconds', type=float,
                        default=DEFAULT_VERIFY_BUDGET_SECONDS)
    parser.add_argument('--worker', action='store_true',
                        help='内部使用：当前进程是被 GuardedRun 监督的 worker，'
                             '自己不再起监督。不要手动传这个开关——手工调用本模块 '
                             '永远走编排层（受监督）。')
    parser.add_argument('--seed', type=int, default=42,
                        help='预检块选择与抽样核验的固定 seed')
    parser.add_argument('--run-id', default=None,
                        help='显式指定 run_id（默认 UTC 时间 + 随机短标识）')
    parser.add_argument('--duplicate-policy',
                        choices=('reject', 'dedupe_keep_last'),
                        default='reject',
                        help='重复 (userId,movieId) 的处理；默认 reject（计划书要求 '
                             '报告位置并停止）。dedupe_keep_last 只为等价性对拍复现 '
                             '旧口径，正式全量训练不得使用。')
    args = parser.parse_args(argv)

    if args.max_k <= 0:
        parser.error('--max-k must be positive')
    if args.block_size < 1:
        parser.error('--block-size must be >= 1')
    if args.beta < 0.0 or not np.isfinite(args.beta):
        parser.error('--beta must be finite and >= 0')
    if not np.isfinite(args.positive_threshold):
        parser.error('--positive-threshold must be finite')
    if args.monitor_interval <= 0:
        parser.error('--monitor-interval must be positive')
    for name in ('prepare_budget_seconds', 'precheck_budget_seconds',
                 'train_budget_seconds', 'verify_budget_seconds'):
        if getattr(args, name) <= 0:
            parser.error('--%s must be positive' % name.replace('_', '-'))
    if args.run_id is not None:
        M.verify_id_format(args.run_id, '--run-id')
    return args


def run_prepare(input_path, index, out_dir, args, log=None):
    """整理两遍扫描：第二遍落磁盘段 → 归并成 CSR → 写 npy。

    **两遍都必须带唯一性检查**：第一遍用 reject 策略（重复即停），第二遍复用
    同一套 :class:`DuplicateChecker`（它只在 ``finalize`` 后给出结论，所以第二遍
    的重查是"再确认一次"而不是主判据——主判据在第一遍就已经停下流程了）。

    :returns: ``(csr_paths, duplicate_report, prepare_seconds)``
    """
    started = time.time()
    work = os.path.join(out_dir, 'csr_work')
    if not os.path.isdir(work):
        os.makedirs(work)

    log and log('pass 2: streaming positive pairs to disk segments')
    builder = DiskCSRBuilder(work, log=log)
    builder.run_pass2(input_path, index,
                      positive_threshold=args.positive_threshold,
                      repeat_policy=args.duplicate_policy)
    log and log('pass 2: merging %d segment(s) on disk' % builder.n_segments)
    indptr_users, indices_buffer, nnz = builder.merge_to_csr(index.n_users,
                                                             index.n_items)
    expected = index.positive_pairs()
    if int(nnz) != int(expected):
        raise CorrectnessError(
            'nnz mismatch: CSR holds %d positive ratings but pass 1 counted %d. '
            'The two passes disagree, so the matrix cannot be trusted.'
            % (int(nnz), int(expected)))
    paths = write_csr_arrays(work, indptr_users, indices_buffer, index.n_users,
                             index.n_items, log=log)
    builder.cleanup()
    return paths, time.time() - started


def run_worker(args, log=None):
    """**受监督的 worker 主体**：真正做计算的这一段，跑在被监督的子进程里。

    为什么要有"worker + 编排层"这一层拆分
    -------------------------------------

    计划书 §5.A 的核心契约是**监督独立于工作进程**：上一轮把采样写进主计算流程，
    结果构图 493 秒里一个采样点都没有，3 GiB 终止线形同虚设。所以本模块必须让
    构图跑在一个**独立的子进程**里，由 :class:`GuardedRun` 在父进程侧起
    :class:`ResourceGuard` 采样并随时终止该子进程所在的进程组。

    本函数就是那个子进程的入口：它自己**不**起监督，只负责在阶段切换时调用
    ``guard.set_stage``（通过 :func:`_notify_stage` 向父进程写阶段文件），
    其余全部工作（校验、两遍扫描、CSR、列块共现、装配、校验、落盘）都在这里。

    阶段推进靠一个**阶段文件**（``<out_dir>/stage.txt``）而不是进程内状态：
    父进程的 guard 按文件内容判阶段超时。这样即使 worker 卡在某一段，
    父进程仍能按上一段名判定超时并终止它。
    """
    started = time.time()
    out_dir = os.path.abspath(args.out)
    run_id, model_id = assembled_ids(out_dir)

    logger = RunLogger(os.path.join(out_dir, 'run.log'))
    log = logger
    log('worker start: run_id=%s model_id=%s input=%s out=%s'
        % (run_id, model_id, args.input, out_dir))
    log('blas threads: %r' % (set_blas_threads_to_one(),))
    log('fixed rules: positive_threshold=%r max_k=%d beta=%r block_size=%d '
        'duplicate_policy=%s'
        % (args.positive_threshold, args.max_k, args.beta, args.block_size,
           args.duplicate_policy))

    status = STATUS_RUNNING
    failure_reason = None
    index = None
    artifacts = {}
    phase_seconds = {}
    struct_stats = None
    precheck_payload = None
    metadata = None
    try:
        # ---------------- 阶段 0：环境门禁（必须在 try 内）--------------
        # 这两项检查曾经写在 `try` **之外**：`raise` 直接穿透 `run_worker`，
        # 于是 `finalize` 从不执行，目录里只剩 worker_console.log，
        # **run_status.json / manifest.json / source_hashes.json / conclusion.md
        # 全部缺失**，失败原因退化成编排层的通用 "worker exited with code 1"。
        # 实测：`--input` 指向不存在文件 -> 只有 run.log。
        # 最讽刺的是磁盘门禁：`BudgetExceeded` 映射成 resource_blocked 的分支
        # 在 try 内（下面 `except BudgetExceeded`），唯独真正触发磁盘门禁的
        # 这一次因为写在 try 外，连终态文件都留不下。
        # 计划书 §6 要求 run_status.json 等**始终**能落盘，所以移进来。
        if not os.path.isfile(args.input):
            raise InputValidationError('input file not found: %s' % args.input)

        free_disk = free_disk_bytes(out_dir)
        log('free disk on output volume: %.2f GiB (floor %.2f GiB)'
            % (bytes_to_gib(free_disk), bytes_to_gib(MIN_FREE_DISK_BYTES)))
        if free_disk < MIN_FREE_DISK_BYTES:
            raise BudgetExceeded('free disk %.2f GiB < required %.2f GiB'
                                 % (bytes_to_gib(free_disk),
                                    bytes_to_gib(MIN_FREE_DISK_BYTES)))
        # ---------------- 阶段 1：prepare（两遍扫描 + 磁盘 CSR）---------
        _notify_stage(out_dir, 'prepare')
        prepare_started = time.time()
        input_sha256 = file_sha256(args.input)
        log('phase prepare: input sha256=%s' % input_sha256)
        log('phase prepare: pass 1 (streaming index, chunk=%d rows)'
            % DEFAULT_SCAN_ROWS)
        index = stream_input_pass1(args.input, args.positive_threshold, log=log,
                                   repeat_policy=args.duplicate_policy)
        log('pass 1 done in %.1fs: rows=%d users=%d items=%d pos_pairs=%d'
            % (index.seconds, index.input_rows, index.n_users, index.n_items,
               index.positive_pairs()))

        signature = source_signature(index, args, input_sha256)
        artifacts['graph_signature'] = signature['signature']

        log('phase prepare: pass 2 (disk-segment CSR)')
        csr_paths, pass2_seconds = run_prepare(args.input, index, out_dir, args,
                                               log=log)
        nnz = int(index.positive_pairs())
        phase_seconds['prepare'] = time.time() - prepare_started
        log('phase prepare done in %.1fs (pass2=%.1fs nnz=%d)'
            % (phase_seconds['prepare'], pass2_seconds, nnz))

        # ---------------- 阶段 2：precheck（完整矩阵上实测若干块）-------
        _notify_stage(out_dir, 'precheck')
        precheck_started = time.time()
        X = open_csr(csr_paths, index, log=log)
        n_like = np.asarray(index.n_like, dtype=np.int64).copy()
        n_all = np.asarray(index.n_all, dtype=np.int64).copy()
        item_ids = np.asarray(index.item_ids, dtype=np.int64)
        if len(n_like) != index.n_items or len(n_all) != index.n_items:
            raise CorrectnessError('n_like/n_all length mismatch: %d/%d vs %d'
                                   % (len(n_like), len(n_all), index.n_items))
        log('full positive matrix loaded: shape=%r nnz=%d'
            % ((index.n_users, index.n_items), int(X.nnz)))

        disk_estimate = estimate_disk_bytes(index, args.max_k, args.block_size)
        free_now = free_disk_bytes(out_dir)
        disk_ok = (disk_estimate['total_bytes'] + DISK_HEADROOM_BYTES) < free_now
        log('disk estimate: %.2f GiB + headroom %.2f GiB vs free %.2f GiB -> %s'
            % (bytes_to_gib(disk_estimate['total_bytes']),
               bytes_to_gib(DISK_HEADROOM_BYTES), bytes_to_gib(free_now),
               'ok' if disk_ok else 'INSUFFICIENT'))

        # 记住**请求的**块大小，用于在产物里如实记录"是否发生过降档"
        # （`parameters_of` 读的是 `args.block_size`，降档后它已是实际值；
        #  不留下请求值就无法从产物看出降档发生过）。
        requested_block_size = int(args.block_size)
        precheck = run_precheck(X, item_ids, n_like, args, log=log)
        phase_seconds['precheck'] = time.time() - precheck_started
        log('phase precheck done in %.1fs' % phase_seconds['precheck'])

        # **阶梯降档必须传导到 train 阶段**。
        # `run_precheck` 在 128 列 MemoryError 时会按 64→32→16 递减并记录
        # `block_size_used`，但曾经**从不回写** `args.block_size`。后果有两个：
        #   ① 外推偏低：`n_blocks` 按 128 算，而实际要按 32 跑（块数差 3.5 倍），
        #      于是 `project_runtime` 严重低估正式阶段耗时；
        #   ② 正式构图仍用 128 —— **正是刚刚 MemoryError 的那一档**，
        #      `build_all_blocks` 会整块走 `write_failure` → `CorrectnessError`。
        #      等于降档白降、预检白跑。
        # 这里把实测可用的块大小落回 `args.block_size`，让外推与正式构图都用它。
        effective_block_size = precheck['block_size_used']
        if effective_block_size and int(effective_block_size) != int(args.block_size):
            log('precheck: downshifted block_size %d -> %d; train will use the '
                'downshifted value (otherwise it would retry the size that just '
                'ran out of memory)'
                % (int(args.block_size), int(effective_block_size)))
            args.block_size = int(effective_block_size)

        n_blocks = (index.n_items + args.block_size - 1) // args.block_size
        projection = project_runtime(precheck, n_blocks,
                                     args.train_budget_seconds)
        continue_run = bool(disk_ok and precheck['ok']
                            and projection['within_budget'])
        precheck_payload = {
            'input': describe_input(args.input, index, input_sha256),
            'matrix': {'rows': index.n_users, 'cols': index.n_items,
                       'nnz': int(nnz), 'dtype': 'int64'},
            'disk': {'free_bytes': free_now, 'estimate': disk_estimate,
                     'headroom_bytes': DISK_HEADROOM_BYTES, 'ok': disk_ok},
            'blocks': precheck['blocks'],
            'blocks_failed': precheck['failed'],
            'block_size_used': precheck['block_size_used'],
            'block_size_requested': int(requested_block_size),
            'block_size_downshifted': bool(
                effective_block_size
                and int(effective_block_size) != int(requested_block_size)),
            'ladder_history': precheck.get('ladder_history'),
            'tiers_fully_failed': precheck.get('tiers_fully_failed'),
            'projection': projection,
            'extrapolation_caveat': (
                'Projections multiply sampled per-block times by block count. '
                'Block difficulty varies strongly (co-occurrence nnz per block '
                'ranges by orders of magnitude: the first blocks are the most '
                'popular movies and are ~12x denser than the average block), so '
                'the mean/max projections are a rough range and NOT a reliable '
                'runtime upper bound.'),
            'continue': continue_run,
            'reason': precheck['reason'] or (None if continue_run
                                             else 'disk estimate or runtime '
                                                  'projection failed'),
            'seconds': phase_seconds['precheck'],
        }
        M.write_json_atomic(os.path.join(out_dir, 'precheck.json'),
                            precheck_payload)
        artifacts['precheck_json'] = 'precheck.json'
        log('precheck: %s' % json.dumps(
            {'blocks': len(precheck['blocks']), 'failed': len(precheck['failed']),
             'mean_s': projection['mean_block_seconds'],
             'max_s': projection['max_block_seconds'],
             'projected_mean_hours': projection['projected_hours_mean'],
             'projected_max_hours': projection['projected_hours_max'],
             'within_budget': projection['within_budget'], 'disk_ok': disk_ok},
            sort_keys=True))
        del X

        # 计划书 §5.C：通过则**直接继续**正式构图，不要求用户再次确认。
        if not continue_run:
            status = STATUS_RESOURCE_BLOCKED
            failure_reason = ('precheck did not pass: disk_ok=%s precheck_ok=%s '
                              'within_budget=%s; see precheck.json'
                              % (disk_ok, precheck['ok'],
                                 projection['within_budget']))
            log('RESOURCE BLOCKED: %s' % failure_reason)
            return finalize(out_dir, log, logger, None, index, args, run_id,
                            status, failure_reason, started, artifacts,
                            precheck_payload=precheck_payload,
                            phase_seconds=phase_seconds)

        # ---------------- 阶段 3：train（正式分块构图）------------------
        _notify_stage(out_dir, 'train')
        train_started = time.time()
        staged = load_csr_for_blocks(csr_paths, index)
        store = BlockStore(out_dir, signature, log=log)
        store._check_existing()
        arrays, problems = build_all_blocks(staged, item_ids, n_like, args,
                                            store, log=log)
        if problems:
            raise CorrectnessError('block construction problems: %s'
                                   % '; '.join(problems))
        del staged
        log('phase train done in %.1fs (edges=%d)'
            % (time.time() - train_started, len(arrays['sources'])))

        # ---------------- 阶段 4：verify（装配 + 校验 + 落盘）-----------
        _notify_stage(out_dir, 'verify')
        model_arrays, reload_problems = store.load_blocks()
        if reload_problems:
            raise CorrectnessError('reloaded block files are inconsistent: %s'
                                   % '; '.join(reload_problems))
        model = arrays_to_model(model_arrays, item_ids, n_like, n_all)
        del model_arrays, arrays
        struct_problems, struct_stats = M.validate_arrays(model, max_k=args.max_k)
        if struct_problems:
            raise CorrectnessError('structural validation failed: %s'
                                   % '; '.join(struct_problems))

        model_dir = os.path.join(out_dir, 'model')
        metadata = M.build_metadata(
            run_id=run_id,
            source=describe_input(args.input, index, input_sha256),
            parameters=parameters_of(args),
            extra={'graph_signature': signature['signature'],
                   'structure_stats': struct_stats})
        saved = model.save(model_dir, metadata)
        hash_ok = M.verify_hashes(model_dir, saved)
        bad = sorted(name for name, ok in hash_ok.items() if not ok)
        if bad:
            raise CorrectnessError('model file hash mismatch after write: %s'
                                   % ', '.join(bad))
        # 只有全部检查通过才升级状态；**这不代表线上 active**。
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
        # 升级状态后重新 mmap 加载一次：证明磁盘上的东西真的能被"验证后使用"。
        reloaded_model, reloaded_meta = M.load_model(
            model_dir, validate=True,
            require_status=M.STATUS_VALIDATED_UNPUBLISHED, max_k=args.max_k)
        if reloaded_meta['run_id'] != run_id or \
                reloaded_meta['model_id'] != model_id:
            raise CorrectnessError(
                'artifact id mismatch: run_id=%r model_id=%r vs expected %r/%r'
                % (reloaded_meta['run_id'], reloaded_meta['model_id'], run_id,
                   model_id))
        del reloaded_model
        artifacts['model_dir'] = 'model'
        artifacts['model_metadata'] = os.path.join('model', M.METADATA_FILE)
        phase_seconds['train'] = time.time() - train_started
        status = STATUS_SUCCESS
        return finalize(out_dir, log, logger, None, index, args, run_id, status,
                        failure_reason, started, artifacts,
                        precheck_payload=precheck_payload,
                        model_dir=model_dir, metadata=saved,
                        phase_seconds=phase_seconds,
                        struct_stats=struct_stats)

    except BudgetExceeded as error:
        status = STATUS_RESOURCE_BLOCKED
        failure_reason = str(error)
        log('RESOURCE BLOCKED: %s' % failure_reason)
    except (CorrectnessError, M.ModelIOError) as error:
        status = STATUS_CORRECTNESS_FAILED
        failure_reason = '%s: %s' % (type(error).__name__, error)
        log('CORRECTNESS FAILURE: %s' % failure_reason)
        log('traceback:\n%s' % traceback.format_exc())
        status = STATUS_CORRECTNESS_FAILED
    except InputValidationError as error:
        status = STATUS_CORRECTNESS_FAILED
        failure_reason = 'InputValidationError: %s' % error
        log('INPUT VALIDATION FAILURE: %s' % failure_reason)
        log('traceback:\n%s' % traceback.format_exc())
    except BaseException as error:  # noqa: BLE001 - 未预期错误也必须留档
        import traceback as _tb
        status = STATUS_CORRECTNESS_FAILED
        failure_reason = '%s: %s' % (type(error).__name__, error)
        log('UNEXPECTED FAILURE: %s' % failure_reason)
        log('traceback:\n%s' % _tb.format_exc())

    return finalize(out_dir, log, logger, None, index, args, run_id, status,
                    failure_reason, started, artifacts,
                    precheck_payload=precheck_payload,
                    phase_seconds=phase_seconds, struct_stats=struct_stats)


def _notify_stage(out_dir, stage):
    """把当前阶段写进 ``stage.txt``（原子写），父进程的 guard 据此判阶段超时。

    用文件而不是 IPC：worker 可能卡死在任何一段，父进程必须**不依赖 worker
    配合**就能读到"它跑到哪了"。这正是上一轮"采样被主流程阻塞"教训的延伸。
    """
    if stage not in PHASES:
        raise ValueError('unknown stage %r; allowed=%r' % (stage, list(PHASES)))
    M.write_json_atomic(os.path.join(out_dir, 'stage.txt'),
                        {'stage': stage, 'at': M.utc_now_iso()}, indent=None)
    return stage


def read_stage(out_dir):
    """读父进程侧的阶段文件；缺失或非法返回 None（不猜）。"""
    path = os.path.join(out_dir, 'stage.txt')
    if not os.path.isfile(path):
        return None
    try:
        payload = M.read_json(path)
    except ValueError:
        return None
    stage = payload.get('stage')
    return stage if stage in PHASES else None


def run(args):
    """**编排层**：起受监督的 worker 子进程，等它跑完，落盘终态。

    这是计划书 §6 的入口。它自己**不**做计算——计算全在
    :func:`run_worker`（子进程）。这样监督进程与工作进程分离，worker 无论
    怎样阻塞都不会让采样停摆。

    **没有绕过监督的开关**：本函数不存在 ``--no-guard`` 之类的参数，
    ``resource_guard`` 也是硬 import。
    """
    started = time.time()
    # run_id 的优先级：显式 --run-id > --out 末段已是 run_id 形状 > 新生成。
    # 这样"目录名 == run_id == 产物内 run_id"是三处一致的，不会套娃。
    run_id = args.run_id or run_id_from_out(args.out) or make_run_id()
    out_dir = resolve_out_dir(args.out, run_id)
    os.makedirs(out_dir)
    stage_path = os.path.join(out_dir, 'stage.txt')

    logger = RunLogger(os.path.join(out_dir, 'orchestrator.log'))
    log = logger
    log('orchestrator: run_id=%s out=%s' % (run_id, out_dir))
    log('orchestrator: spawning supervised worker (interval=%.1fs)'
        % args.monitor_interval)

    stage_budgets = {
        'prepare': float(args.prepare_budget_seconds),
        'precheck': float(args.precheck_budget_seconds),
        'train': float(args.train_budget_seconds),
        'verify': float(args.verify_budget_seconds),
    }
    argv = worker_argv(args, out_dir=out_dir, run_id=run_id)
    log('orchestrator: worker argv=%r' % (argv,))

    guarded = GuardedRun(
        argv,
        csv_path=os.path.join(out_dir, 'resources.csv'),
        status_path=os.path.join(out_dir, 'guard_status.json'),
        log=log,
        interval=args.monitor_interval,
        stage_budgets=stage_budgets,
    )
    guarded.start()
    # worker 一启动就先算 prepare（它自己会写 stage.txt），这里先把 guard 的阶段
    # 设成 prepare，避免"还没读到 stage.txt"的空窗期不受阶段预算约束。
    guarded.guard.set_stage('prepare')
    result = _run_guarded(guarded, out_dir, stage_path, log)
    guard_result = result
    log('orchestrator: worker finished: exit_code=%r status=%s reason=%r'
        % (guard_result.get('exit_code'), guard_result.get('status'),
           guard_result.get('reason')))

    # 读 worker 自己写的 run_status.json（如果它跑到了写状态那一步）。
    run_status_path = os.path.join(out_dir, 'run_status.json')
    worker_status = None
    if os.path.isfile(run_status_path):
        try:
            worker_status = M.read_json(run_status_path)
        except ValueError:
            worker_status = None

    final_status = _reconcile_status(guard_result, worker_status)

    # **`success` 必须由真实产物背书**，不能只靠"进程退出码 0"。
    # 缺陷同族于 MAJOR-4：`_reconcile_status` 在 worker 没写 run_status.json 时
    # 会"合成一份"并采信退出码 0；但它**从不检查 `model/` 是否真的存在、
    # 是否可加载**。于是"进程正常退出但零产物"（例如 worker 在写任何东西之前
    # 就走了某条未覆盖的 return）会被记成 `success`。
    # 这里补一道终态门禁：只有 `model/model.json` 存在、状态是
    # `validated_unpublished`、且能真正 mmap 加载并通过校验，才允许 success。
    artifact_problems = []
    if final_status == STATUS_SUCCESS:
        model_dir = os.path.join(out_dir, 'model')
        try:
            reloaded, reloaded_meta = M.load_model(
                model_dir, validate=True,
                require_status=M.STATUS_VALIDATED_UNPUBLISHED,
                max_k=args.max_k)
            del reloaded
            artifact_problems.append(
                'model ok: status=%s n_items=%s'
                % (reloaded_meta.get('status'),
                   (reloaded_meta.get('counts') or {}).get('n_items')))
        except Exception as error:  # noqa: BLE001 - 任何失败都必须降级
            final_status = STATUS_CORRECTNESS_FAILED
            artifact_problems.append(
                'model could not be loaded/validated: %s: %s'
                % (type(error).__name__, error))
            log('orchestrator: DOWNGRADE success -> %s: %s'
                % (final_status, artifact_problems[-1]))

    # 若发生了降级，必须改写 failure_reason，
    # 否则产物自相矛盾：status=correctness_failed 却留着 worker 的成功措辞。
    # 两种降级来源都要覆盖：
    #   (a) 自报 success 但退出码非 0（MAJOR-4，被 SIGKILL）；
    #   (b) 退出码 0 但**产物不存在/不可加载**（终态门禁，见上）。
    # 旧实现只处理 (a)，如果 (b) 的 failure_reason 不改，会留下
    # "status=correctness_failed + reason=空/success 措辞"的矛盾记录。
    _exit_code = guard_result.get('exit_code')
    if (final_status != STATUS_SUCCESS
            and worker_status is not None
            and worker_status.get('status') == STATUS_SUCCESS):
        if artifact_problems and artifact_problems[-1].startswith('model could'):
            reason = ('orchestrator downgraded a self-reported success: the '
                      'declared model artifacts are missing or invalid (%s). '
                      'A clean exit code is not evidence that the model was '
                      'produced.' % artifact_problems[-1])
        else:
            reason = (
                'orchestrator downgraded a self-reported success: the worker '
                'process did not exit cleanly (exit_code=%r). A non-zero/negative '
                'exit code after run_status.json was written means the process was '
                'killed (e.g. OOM) in the window between writing status and '
                'returning 0, so the self-reported success is not trustworthy.'
                % (_exit_code,))
        worker_status['failure_reason'] = reason
        worker_status['reported_status'] = STATUS_SUCCESS
        log('orchestrator: DOWNGRADE %s -> %s (exit_code=%r)'
            % (STATUS_SUCCESS, final_status, _exit_code))
    if worker_status is None:
        worker_status = {
            'run_id': run_id,
            'model_id': None,
            'status': final_status,
            'failure_reason': guard_result.get('reason') or 'worker did not '
                                                            'write run_status.json',
            'config': parameters_of(args),
            'counts': {},
            'phase_seconds': {},
            'seconds': time.time() - started,
            'out_dir': out_dir,
        }
    worker_status['status'] = final_status
    worker_status['artifact_gate'] = artifact_problems
    worker_status['guard_result'] = {
        'status': guard_result.get('status'),
        'reason': guard_result.get('reason'),
        'terminated': guard_result.get('terminated'),
        'termination': guard_result.get('termination'),
        'exit_code': guard_result.get('exit_code'),
    }
    worker_status['sampled_max'] = guard_result.get('sampled_max')
    worker_status['resource_note'] = (
        'sampled maximum (every %.1fs), not a verified continuous peak'
        % args.monitor_interval)
    M.write_json_atomic(run_status_path, worker_status)
    M.write_json_atomic(os.path.join(out_dir, 'orchestrator_status.json'), {
        'run_id': run_id,
        'model_id': M.compute_model_id(run_id),
        'status': final_status,
        'guard_result': guard_result,
        'seconds': time.time() - started,
        'worker_argv': argv,
        'stage_budgets': stage_budgets,
    })
    log('orchestrator: final status=%s' % final_status)
    logger.close()
    return worker_status


def _run_guarded(guarded, out_dir, stage_path, log):
    """等待被监督的 worker 结束，同时把 stage.txt 的变化同步给 guard。

    阶段推进由 worker 写文件、父进程轮询转发——父进程**不阻塞在 worker 上**，
    所以 guard 的采样线程始终能跑。

    **必须持续抽干 worker 的 stdout/stderr**：``GuardedRun`` 用
    ``subprocess.PIPE`` 起子进程，管道缓冲区满了以后 worker 会**永久阻塞在
    write 上**。worker 的每一行输出都会先写进 ``run.log``，但 traceback 是
    Python 直接写 stderr 的，不走日志——不抽干就既看不到真正的错误原因，
    又会把 worker 卡死（而且因为它在阻塞、RSS 不动，监督也未必触发）。
    这里把子进程输出转存到 ``worker_console.log``。
    """
    import subprocess
    import threading

    console_path = os.path.join(out_dir, 'worker_console.log')
    console = open(console_path, 'w', encoding='utf-8', errors='replace')

    def drain():
        try:
            stream = guarded.process.stdout
            if stream is None:
                return
            for raw in iter(stream.readline, b''):
                try:
                    text = raw.decode('utf-8', 'replace')
                except AttributeError:  # pragma: no cover - 已经是 str 时
                    text = raw
                console.write(text)
                console.flush()
        except Exception:  # noqa: BLE001 - 抽干失败不能影响主流程
            pass

    pump = threading.Thread(target=drain)
    pump.daemon = True
    pump.start()

    exit_code = None
    last_stage = None
    try:
        while True:
            try:
                exit_code = guarded.process.wait(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                current = read_stage(out_dir)
                if current is not None and current != last_stage:
                    try:
                        guarded.guard.set_stage(current)
                        log('orchestrator: stage -> %s' % current)
                    except Exception as error:  # noqa: BLE001
                        log('orchestrator: WARN set_stage(%r) failed: %s'
                            % (current, error))
                    last_stage = current
                if guarded.guard.violation is not None:
                    guarded.terminated = True
                    guarded.termination = _guard_mod.terminate_process_group(
                        guarded.pgid, guarded.guard.grace_seconds, log=log)
                    try:
                        exit_code = guarded.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                    break
        if exit_code is None:
            try:
                exit_code = guarded.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = None
        if guarded.guard is not None:
            guarded.guard.stop()
    finally:
        pump.join(timeout=5)
        try:
            console.close()
        except Exception:  # pragma: no cover
            pass
    return guarded.result(exit_code)


def _reconcile_status(guard_result, worker_status):
    """把"监督的判定"与"worker 自报的状态"合成一个终态。

    优先级：监督的终止判定 > worker 自报 > 退出码。

    **但"worker 自报 success"有一个硬前提：它必须真的正常退出（exit_code == 0）。**

    为什么需要这条：worker 是**先写** ``run_status.json(status=success)``、
    ``main`` 之后**才** ``return 0``。这两步之间如果被外部信号打死
    （VM 是 4 GB 内存 + 4 GB swap，**OOM killer 是真实风险**），就会留下
    ``exit_code=-9`` 而 ``run_status.json`` 写着 ``success``。
    曾经的实现直接采信自报状态，于是 **OOM kill 被记成成功**。
    实测：

        _reconcile_status({'status': 'correctness_failed', 'exit_code': -9},
                          {'status': 'success'})  ->  'success'   # 错

    现在：退出码非 0（或为负 = 被信号杀死）时，自报的 success 一律不采信，
    降级为 correctness_failed 并在 ``failure_reason`` 里说明。
    """
    guard_status = (guard_result or {}).get('status')
    if guard_status in (STATUS_RESOURCE_FAILED, STATUS_MONITOR_FAILED,
                        STATUS_RESOURCE_BLOCKED):
        return guard_status
    exit_code = (guard_result or {}).get('exit_code')
    if worker_status is not None:
        reported = worker_status.get('status')
        # 自报 success 必须与"正常退出"相互印证；否则不采信。
        if reported == STATUS_SUCCESS:
            if exit_code == 0:
                return reported
            # 自报成功但进程没正常退出 —— 以失败论，绝不当成成功。
            return STATUS_CORRECTNESS_FAILED
        if reported in (STATUS_RESOURCE_BLOCKED, STATUS_CORRECTNESS_FAILED):
            return reported
    return STATUS_SUCCESS if exit_code == 0 else STATUS_CORRECTNESS_FAILED


def worker_argv(args, out_dir=None, run_id=None):
    """构造 worker 子进程的命令行（保留全部构图参数 + ``--worker``）。

    :param out_dir: **编排层解析后的**产物目录。必须显式传，不能用 ``args.out``：
        编排层建的是 ``<out>/<run_id>``，而 worker 用 ``basename(out_dir)`` 反推
        run_id（见 :func:`assembled_ids`）。若把未解析的 ``args.out`` 原样转发，
        只要 ``--out`` 末段不是 run_id 形状（例如 ``--out /home/h/itemcf_full``），
        worker 就会在**任何产物落盘之前**抛
        ``ValueError: run_id ... got 'plain'``，整轮无模型、无终态文件。
        实测（真实子进程）：``--out <root>/plain`` → exit 3、零产物。
        计划书 §6 的示例恰好写成 ``--out /home/h/itemcf_full/<run_id>``
        （末段就是 run_id），**把这个 bug 掩盖住了**，所以只有那一种写法能跑。
    :param run_id: 编排层解析出的 run_id。自动生成时（``make_run_id()``）
        ``args.run_id`` 是 None，必须把它显式传给 worker，否则 worker 只能从
        目录名猜——两者一旦不同就是 CRITICAL 级的 ID 分叉。
    """
    if out_dir is None:
        out_dir = args.out
    if run_id is None:
        run_id = args.run_id
    argv = [sys.executable, '-m', 'experiments.itemcf.full_train',
            '--input', args.input, '--out', out_dir,
            '--max-k', str(args.max_k), '--beta', repr(args.beta),
            '--positive-threshold', repr(args.positive_threshold),
            '--block-size', str(args.block_size),
            '--monitor-interval', repr(args.monitor_interval),
            '--prepare-budget-seconds', repr(args.prepare_budget_seconds),
            '--precheck-budget-seconds', repr(args.precheck_budget_seconds),
            '--train-budget-seconds', repr(args.train_budget_seconds),
            '--verify-budget-seconds', repr(args.verify_budget_seconds),
            '--seed', str(args.seed),
            '--run-id', run_id or '',
            '--duplicate-policy', args.duplicate_policy,
            '--worker']
    if run_id:
        argv[argv.index('--run-id') + 1] = run_id
    else:
        argv.remove('--run-id')
        argv.remove('')
    return argv


def finalize(out_dir, log, logger, guard, index, args, run_id, status,
             failure_reason, started, artifacts, precheck_payload=None,
             model_dir=None, metadata=None, phase_seconds=None,
             struct_stats=None):
    """worker 侧落盘 run_status.json / manifest.json / source_hashes.json。

    ``guard`` 在 worker 里通常是 ``None``——监督跑在**父进程**，worker 不该有
    guard 对象。采样最大值由编排层在 worker 退出后补进 ``sampled_max`` 字段。
    """
    counts = index.coverage() if index is not None else {}
    counts['graph_edges'] = int(metadata.get('counts', {}).get('n_edges', 0)) \
        if metadata else None
    counts['graph_sources'] = int(
        metadata.get('counts', {}).get('n_sources_with_neighbors', 0)) \
        if metadata else None

    source_hashes = source_hashes_of_modules()
    input_sha256 = None
    try:
        input_sha256 = file_sha256(args.input)
    except (OSError, IOError):  # pragma: no cover
        pass

    run_status = {
        'status': status,
        'failure_reason': failure_reason,
        'run_id': run_id,
        'model_id': M.compute_model_id(run_id) if model_dir else None,
        'out_dir': out_dir,
        'config': parameters_of(args),
        'counts': counts,
        'phase_seconds': phase_seconds or {},
        'seconds': time.time() - started,
        'artifacts': artifacts,
        'supervision': {
            'mode': 'separate_process',
            'guard_owner': 'parent orchestrator (GuardedRun + ResourceGuard)',
            'stage_file': 'stage.txt',
            'note': 'the worker holds no guard object; sampling happens in the '
                    'parent process so it cannot be blocked by this process',
        },
    }
    if index is not None:
        run_status['duplicate_check'] = index.duplicate_report()
    M.write_json_atomic(os.path.join(out_dir, 'run_status.json'), run_status)
    M.write_json_atomic(os.path.join(out_dir, 'source_hashes.json'),
                        {'sources': source_hashes,
                         'input_sha256': input_sha256})
    manifest = {
        'stage': 'full_train',
        'run_id': run_id,
        'model_id': M.compute_model_id(run_id),
        'status': status,
        'command': sys.argv,
        'input': os.path.abspath(args.input),
        'input_sha256': input_sha256,
        'parameters': parameters_of(args),
        'source_hashes': source_hashes,
        'python': sys.version,
        'numpy': np.__version__,
        # 注意：``scipy.sparse`` 子模块**没有** ``__version__``（写成
        # ``sparse.__version__`` 会 AttributeError，实测在本地上崩过一次）。版本在
        # 顶层 ``scipy`` 包上；用 getattr 兜底，兼容 VM 的 scipy 1.5.4 与本地 1.13.1。
        'scipy': getattr(__import__('scipy'), '__version__', None),
        'blas_thread_env': dict((k, os.environ.get(k)) for k in
                                ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS',
                                 'MKL_NUM_THREADS')),
        'counts': counts,
        'duplicate_check': index.duplicate_report() if index is not None else None,
        'structure_stats': struct_stats,
        'precheck': precheck_payload,
        'seconds': time.time() - started,
        'artifacts': artifacts,
    }
    M.write_json_atomic(os.path.join(out_dir, 'manifest.json'), manifest)

    # ``conclusion.md``（计划书 §6 产物的最后一项）：一页左右的结论摘要。
    # 放在**所有终态**都走到的 finalize 里，所以 success / resource_blocked /
    # correctness_failed 都会留下结论——失败时也要能一眼看出"为什么没成"，
    # 而不是只留一堆 json。渲染失败不得影响终态落盘（它是摘要，不是事实来源）。
    try:
        conclusion_path = C.write_conclusion(out_dir)
        artifacts['conclusion_md'] = os.path.relpath(conclusion_path, out_dir)
        log('conclusion.md written')
    except Exception as error:  # noqa: BLE001 - 摘要失败不能拖垮终态落盘
        log('WARN could not write conclusion.md: %s: %s'
            % (type(error).__name__, error))

    log('run_status.json written: status=%s failure_reason=%r'
        % (status, failure_reason))
    logger.close()
    return run_status


def parameters_of(args):
    return {
        'positive_threshold': float(args.positive_threshold),
        'max_k': int(args.max_k),
        'beta': float(args.beta),
        'block_size': int(args.block_size),
        'monitor_interval': float(args.monitor_interval),
        'prepare_budget_seconds': float(args.prepare_budget_seconds),
        'precheck_budget_seconds': float(args.precheck_budget_seconds),
        'train_budget_seconds': float(args.train_budget_seconds),
        'seed': int(args.seed),
        'duplicate_policy': args.duplicate_policy,
    }


def describe_input(path, index, sha256=None):
    """输入指纹（含 SHA-256）。``sha256`` 由调用方算一次后传入，不重复全量读。"""
    return {
        'path': os.path.abspath(path),
        'bytes': os.path.getsize(path),
        'sha256': sha256,
        'rows': index.input_rows if index is not None else None,
        'users': index.n_users if index is not None else None,
        'items': index.n_items if index is not None else None,
        'positive_pairs': index.positive_pairs() if index is not None else None,
        'items_zero_like': (int(sum(1 for v in index.n_like if v == 0))
                            if index is not None else None),
    }


def main(argv=None):
    args = parse_args(argv)
    if args.worker:
        # 被 GuardedRun 监督的子进程入口：只做计算，不起监督。
        status = run_worker(args)
    else:
        # 正常入口：编排层起受监督的 worker。**没有绕过监督的开关**。
        status = run(args)
    print(json.dumps({
        'status': status['status'],
        'run_id': status['run_id'],
        'model_id': status['model_id'],
        'failure_reason': status['failure_reason'],
        'counts': {
            'input_rows': (status.get('counts') or {}).get('input_rows'),
            'users': (status.get('counts') or {}).get('users'),
            'items': (status.get('counts') or {}).get('items'),
            'graph_edges': (status.get('counts') or {}).get('graph_edges'),
        },
        'sampled_max': status.get('sampled_max'),
    }, sort_keys=True, default=M._json_default))
    return 0 if status['status'] == STATUS_SUCCESS else 3


if __name__ == '__main__':
    sys.exit(main())
