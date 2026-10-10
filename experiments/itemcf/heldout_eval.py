# -*- coding: utf-8 -*-
"""全量规模**留出评测**入口（计划书 `docs/superpowers/plans/2026-10-05-itemcf-fullscale-holdout.md`）。

本轮只回答一个问题：**在全量规模的训练数据下，ItemCF 的推荐优势是否仍然成立。**

固定：`k=100`、`beta=0`、`positive_threshold=4.0`、`top_n=20`。不调参、不训练 ALS、
不接线上、不改数据库。

与上一轮（20k cohort 的 ``confirm.py``）的三处**必须理解**的差别
==============================================================

1. **用户覆盖完全不同。** 上一轮只评测 2,000 人、且只给这 2,000 人造留出段；本轮
   全部用户都按个人时间 80/20 切分，训练图来自**全体用户的训练段**。
2. **评测方式从"逐用户打分循环"改成"精确命中矩阵"。** 20 万用户规模下逐用户走
   Python 打分循环不可行（上一轮 1,953 用户已花 24.8 s，2000 用户 × 全量图是分钟级，
   但整张图的邻居条数是上一轮的 3.7 倍）。这里用一次稀疏矩阵乘**精确**算出每个
   `(u, i) ∈ R_u` 是否有正贡献，数学上与 ``confirm.py::score_itemcf_user`` 等价
   （见 :func:`hit_matrix_columns` 的推导，以及 `tests/test_itemcf_holdout_eval.py`
   里对 300 用户图的逐用户对拍）。**只有"命中哪些正例"需要精确**；非命中位置的
   填充顺序对 Recall/NDCG 没有影响。
3. **监督换了实现。** 上一轮 `resource_guard` 的阶段墙钟预算在"单块耗时 4~7 分钟"
   的粒度上会误杀；本轮用 `rate_limited_guard`，保留全部资源终止线、去掉阶段墙钟
   上限，并支持每块显式刷新 deadline。

协议（三套，见计划书 §1）
========================

=========================  ==========================================  ==============
标识                        训练图                                      评测名单
=========================  ==========================================  ==============
``A``（主口径）             全部用户的训练段                            全部有效评测用户
``B``（敏感性）             只有 ``n_u >= 5`` 用户的训练段              同上（用户变少的那部分自然无测试段）
``C``（描述性对照）        同 A                                        只评测 ``n_u >= 5`` 的子集
=========================  ==========================================  ==============

`A` 是主结论；`B` 与 `C` 用来把"图变厚"和"名单变窄"两种效应分开。

内存纪律
========

31.6M 行评分由 :func:`experiments.itemcf.holdout_common.stream_input_pass1` 流式处理；
本模块只额外持有：

- ``eval_seen``（2,000 个 set，合计约 12 万项）、``eval_history``（2,000 个 int64 数组）；
- 全局物品计数 ``n_all`` / ``n_like``（8.4 万 × 8 字节 × 2）；
- 逐用户的**留出段**（约 2,000 × 9 项）与协议 B 的 ``n_like_B``（8.4 万 × 8 字节）；
- 构图中间量（由监督盯着，上一轮实测峰值约 1.3 GB）。

**不**持有全量评分行、**不**持有逐行用户下标副本（那是 ``full_train`` 第二遍落盘时的
内部缓冲，本模块不参与）。

Python 3.6 兼容：无 f-string / dataclasses / walrus / ``from __future__ import
annotations`` / dict 合并 ``|`` / positional-only 参数。
"""
import argparse
import array
import csv
import gc
import hashlib
import json
import math
import os
import random
import sys
import time

import numpy as np
from scipy import sparse

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.pilot.metrics import ndcg_at_k, recall_at_k  # noqa: E402

# ---------------------------------------------------------------------------
# 固定常量（协议冻结值；改任何一个都会改变结论）
# ---------------------------------------------------------------------------

#: 本轮的**唯一**口径：全部用户的训练段。
#: （计划初稿还设过一个"只保留 n_u >= 5 的用户"的敏感性口径，用来防"薄用户
#:  让训练图丢共现"。实测 ML-32M 已按 n_u >= 20 预筛，那里是恒等变换，
#:  已删除——见 `docs/superpowers/specs/2026-10-05-fullscale-holdout-protocol.md` §1。）
MODE_DEFAULT = 'default'

METHODS = ('ItemCF', 'MostPop', 'LikePop')

#: 好评二值化阈值（与上一轮、与全量训练完全一致）。
POSITIVE_THRESHOLD = 4.0
#: 冻结的邻居规模与收缩系数（不调参）。
MAX_K = 100
BETA = 0.0
TOP_N = 20
DEFAULT_BLOCK_SIZE = 128

#: 评测名单的新 salt —— 与前两轮名单**独立**。
EVAL_SALT = 'itemcf-fullscale-eval-v1:'
EVAL_SIZE = 2000
#: 兼容别名：`freeze_user_lists` 的签名里用 `cohort_size` 表示"可用用户池"，
#: 本轮不需要像上一轮那样先抽 20,000 人 cohort（评测名单直接从全量可用用户里取），
#: 所以这个参数只作留痕，不参与筛选。
COHORT_SIZE = 0

#: 配对 bootstrap（与上一轮同参数，便于对照）。
BOOTSTRAP_SEED = 42
BOOTSTRAP_REPEATS = 2000

#: 总量墙钟预算（8 小时）。由监督按**块**刷新 deadline，所以它是真正的总量上限，
#: 不是"单块墙钟"。
TOTAL_BUDGET_SECONDS = 8 * 3600
#: 监督采样间隔与资源线（与上一轮全量 run 相同口径）。
MONITOR_INTERVAL_SECONDS = 2.0
RSS_LIMIT_BYTES = 3 * 1024 ** 3
MEM_AVAILABLE_FLOOR_BYTES = 256 * 1024 ** 2
MEM_AVAILABLE_STREAK_SECONDS = 30.0

#: 切分中间件、CSR 中间件、块文件、缓存文件名。
TRAIN_CSV = 'split_train.csv'
HOLDOUT_CSV = 'split_holdout.csv'
CSR_WORK_DIR = 'csr_work'
BLOCK_DIR = 'graph_blocks'
BLOCK_INDEX = 'graph_blocks.json'
MODEL_DIR = 'model'
CACHE_DIR = 'holdout_cache'

#: 旧轮次名单（排除用）。相对项目根。
PRIOR_USER_LISTS = (
    ('confirm_20261004T100122Z',
     'experiments/itemcf/out/confirmation/20261004T100122Z/excluded_users.txt'),
    ('confirm_cohort',
     'experiments/itemcf/out/confirmation/20261004T100122Z/cohort_users.txt'),
    ('confirm_eval',
     'experiments/itemcf/out/confirmation/20261004T100122Z/eval_users.txt'),
    ('fix14_per_user',
     'experiments/itemcf/out/20261004T_fix14/per_user.csv'),
)

#: 判定规则（计划书 §5，先冻结后执行）。
VERDICT_SUPPORTED = 'advantage_holds_at_full_scale'
VERDICT_CROSSES_ZERO = 'insufficient_evidence_interval_crosses_zero'
VERDICT_METRICS_DISAGREE = 'insufficient_evidence_metrics_disagree'
VERDICT_NOT_REPRODUCED = 'advantage_not_reproduced'
VERDICT_FAILURE = 'resource_or_correctness_failure'


class ResourceError(Exception):
    """资源预算被突破（RSS / MemAvailable / 磁盘 / 总量墙钟）。"""


class CorrectnessError(Exception):
    """口径或结构被违反（切分守恒、哈希、索引、块签名）。"""


# ---------------------------------------------------------------------------
# 环境与通用工具
# ---------------------------------------------------------------------------

def set_blas_threads_to_one():
    """把 BLAS/OMP 线程数设为 1（VM 只有 2 核，多线程只会互相抢）。

    **必须在 import numpy 之副作用生效前调用才有意义**，所以调用方要在
    进程最前面（``main`` 的第一行）调；本函数同时把值写进环境并返回快照，
    以便产物里记录"这一轮确实是单线程"。
    """
    names = ('OMP_NUM_THREADS', 'OPENBLAS_NUM_THREADS', 'MKL_NUM_THREADS',
             'NUMEXPR_NUM_THREADS', 'VECLIB_MAXIMUM_THREADS')
    for name in names:
        os.environ[name] = '1'
    return dict((name, os.environ.get(name)) for name in names)


def file_sha256(path, chunk_size=1 << 22):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, payload, indent=2):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=indent, sort_keys=True,
                  default=_json_default)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _json_default(value):
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    raise TypeError('not JSON serializable: %r' % (type(value),))


def write_text(path, text):
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write(text)
    return path


def write_csv_atomic(path, fieldnames, rows):
    tmp = path + '.tmp'
    with open(tmp, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_user_list(path):
    """读一份"每行一个正整数"的名单；空行忽略，返回 list（保持文件顺序）。"""
    users = []
    with open(path, newline='') as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            users.append(int(text))
    return users


def read_user_column(path, column):
    """从 CSV 里读一列 userId（旧轮次的 ``per_user.csv`` 等）。"""
    users = set()
    with open(path, newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        if column not in (reader.fieldnames or []):
            raise CorrectnessError('column %r not in %s' % (column, path))
        for record in reader:
            value = (record.get(column) or '').strip()
            if value:
                users.add(int(value))
    return users


def user_key(salt, user_id):
    """固定排序键 ``(sha256(salt + 无前导零十进制 userId), userId)``。

    **绝不用 Python 的 ``hash()``**（有随机化，跨进程不可复现）。
    """
    text = '%s%d' % (salt, int(user_id))
    return (hashlib.sha256(text.encode('utf-8')).hexdigest(), int(user_id))


def select_top_by_hash(user_ids, salt, size):
    """按 :func:`user_key` 升序取前 ``size`` 人；返回 list（已按该顺序）。"""
    if size < 0:
        raise ValueError('size must be >= 0')
    ranked = sorted(user_ids, key=lambda u: user_key(salt, u))
    return [int(u) for u in ranked[:int(size)]]


def user_list_sha256(user_ids):
    """名单指纹：按给定顺序、每行一个十进制 userId、LF 结尾。"""
    payload = ''.join('%d\n' % int(u) for u in user_ids)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


class RunLogger(object):
    def __init__(self, path):
        self.path = path
        self.handle = open(path, 'w', encoding='utf-8')

    def __call__(self, message):
        line = '[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message)
        self.handle.write(line + '\n')
        self.handle.flush()
        sys.stdout.write(line + '\n')
        sys.stdout.flush()

    def close(self):
        try:
            self.handle.close()
        except Exception:  # noqa: BLE001
            pass


class DeadlineRefresher(object):
    """向监督汇报"我还活着"，并周期性刷新总量 deadline。

    设计要点（本轮真正要修的东西）：

    - 上一轮 ``resource_guard`` 的阶段预算在"单块耗时 4~7 分钟"的粒度上会误杀
      正常的长任务；本类把墙钟预算的**唯一**用途定为"总量上限"，并在每个块结束
      时把 deadline 往后推 ``TOTAL_BUDGET_SECONDS``，于是它永远不会因为
      "某一阶段慢"而提前终止；
    - 同时它充当**显式心跳**：每次刷新都带一个"我预计下一个块要跑 T 秒"的声明，
      监督在 T + 余量的窗口内不判 ``monitor_failed``。
    - 在非 Linux（本地开发机）上 ``rate_limited_guard`` 不可用，此时本类退化为
      空操作，**不**伪装成"已经受监督"（产物里 ``monitor`` 字段会如实写
      ``unavailable``）。
    """

    def __init__(self, heartbeat=None, budget_seconds=TOTAL_BUDGET_SECONDS,
                 log=None, enabled=True):
        self.heartbeat = heartbeat
        self.budget_seconds = float(budget_seconds)
        self.log = log
        self.enabled = bool(enabled and heartbeat is not None)
        self.refreshes = 0

    def refresh(self, expected_seconds, note=''):
        self.refreshes += 1
        if not self.enabled:
            return
        try:
            self.heartbeat(expected_seconds, note)
            self.heartbeat.set_total_budget(self.budget_seconds)
        except Exception as error:  # noqa: BLE001 - 监督接口失败不能掩盖主流程
            if self.log:
                self.log('WARN heartbeat failed: %s' % error)

    def describe(self):
        return {'enabled': self.enabled, 'refreshes': self.refreshes,
                'total_budget_seconds': self.budget_seconds}


# ---------------------------------------------------------------------------
# 用户名单：排除旧轮次 + 哈希选人
# ---------------------------------------------------------------------------

def load_prior_exclusions(root=PROJECT_ROOT):
    """读全部旧轮次名单的并集；返回 ``(users, provenance)``。

    缺失的文件**不静默跳过**：记 ``present=False`` 并保留在 provenance 里，
    由调用方决定是否致命（本函数不判死，因为"某个旧产物不在本地"是可接受的，
    但必须如实披露，否则"排除了历史实验用户"这句话就不成立）。
    """
    users = set()
    provenance = {}
    for label, relative in PRIOR_USER_LISTS:
        path = os.path.join(root, relative)
        entry = {'path': relative, 'present': os.path.isfile(path)}
        if entry['present']:
            try:
                if path.endswith('.csv'):
                    found = read_user_column(path, 'user_id')
                else:
                    found = set(read_user_list(path))
                entry['users'] = len(found)
                users |= found
            except Exception as error:  # noqa: BLE001 - 来源损坏要留痕
                entry['present'] = False
                entry['error'] = '%s: %s' % (type(error).__name__, error)
        provenance[label] = entry
    return users, provenance


def freeze_eval_users(available_users, size=EVAL_SIZE, salt=EVAL_SALT):
    """从可用用户里按哈希冻结评测名单（**不看任何指标**）。"""
    return select_top_by_hash(available_users, salt, size)


class RatingSession(object):
    """磁盘会话：**全体用户**的训练段 + 评测用户的留出段。

    读**完整输入** ``ratings.csv``（不读切片），按用户攒行后切分：

    - 训练段：``keep_all_train=True``（默认，也是本轮唯一采用的模式）时
      **全部用户**的训练段都落 ``split_train.csv``——这是"全量训练数据"的落点，
      也是共现分子与 ``n_like`` 分母同口径的前提；
    - 留出段：只保留**评测用户**的（其余丢弃，避免写 600 万行无用数据）。

    **为什么训练段必须全量**（第一版在这里犯过错）：如果只把评测用户的训练段
    写进训练矩阵，那么共现的分子只统计约 2,000 人，而分母（``n_like``）来自
    全部 200,948 人——两者口径不一致，相似度被系统性压小，实测导致 ItemCF 的
    NDCG/Recall **双双变成 0.0**（详见 :func:`run_evaluation` 的 docstring）。

    切分完全复用 :func:`experiments.pilot.data.temporal_split` 的取整规则
    （``n_test = max(1, int(n * test_frac))``，``n < 2`` 时整体进训练段），
    排序键也是同一套 ``(timestamp, movieId, 原始行序)``。

    内存纪律：按用户攒行，只在**确认该用户不会再出现**时才切分落盘。
    MovieLens 的 ``ratings.csv`` 事实上按 userId 分组，但本类不假定它——
    一旦已落盘的用户再次出现就直接抛错，而不是拼出一个错误的切分。
    """

    def __init__(self, input_path, out_dir, chunk_rows, test_frac=0.2,
                 eval_users=None, keep_all_train=True):
        self.input_path = input_path
        self.chunk_rows = int(chunk_rows)
        self.test_frac = float(test_frac)
        self.eval_users = set(int(u) for u in (eval_users or []))
        self.keep_all_train = bool(keep_all_train)
        self.train_path = os.path.join(out_dir, TRAIN_CSV)
        self.holdout_path = os.path.join(out_dir, HOLDOUT_CSV)
        self.counts = {
            'input_rows': 0, 'users': 0,
            'train_rows': 0, 'holdout_rows': 0,
            'users_with_train': 0, 'users_with_holdout': 0,
            'users_empty_train': 0, 'users_empty_holdout': 0,
            'dropped_holdout_rows': 0,
        }
        self.empty_train_users = []
        self.seconds = None

    def build(self, log=None):
        started = time.time()
        if not os.path.isfile(self.input_path):
            raise CorrectnessError('input not found: %s' % self.input_path)
        handle_train = open(self.train_path, 'w', newline='')
        handle_holdout = open(self.holdout_path, 'w', newline='')
        writer_train = csv.writer(handle_train)
        writer_holdout = csv.writer(handle_holdout)
        closed = set()
        pending_user = None
        pending = []
        try:
            with open(self.input_path, newline='') as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
                if header is None or header[0] != 'userId':
                    raise CorrectnessError(
                        'unexpected input header: %r (expected userId,movieId,'
                        'rating,timestamp)' % (header,))
                for record in reader:
                    self.counts['input_rows'] += 1
                    user = int(record[0])
                    if user != pending_user:
                        if pending_user is not None:
                            self._flush_user(pending_user, pending, writer_train,
                                             writer_holdout)
                            closed.add(pending_user)
                        if user in closed:
                            raise CorrectnessError(
                                'input is not grouped by userId: %d reappeared '
                                'after its split was written' % user)
                        pending_user = user
                        pending = []
                    pending.append((int(record[0]), int(record[1]),
                                    float(record[2]), int(record[3])))
            if pending_user is not None:
                self._flush_user(pending_user, pending, writer_train,
                                 writer_holdout)
                closed.add(pending_user)
        finally:
            handle_train.close()
            handle_holdout.close()
        self.counts['users'] = len(closed)
        self.seconds = time.time() - started
        if log:
            log('session built: input_rows=%d users=%d train_rows=%d '
                'holdout_rows=%d (dropped=%d) users_with_train=%d '
                'users_with_holdout=%d empty_train=%d empty_holdout=%d (%.1fs)'
                % (self.counts['input_rows'], self.counts['users'],
                   self.counts['train_rows'], self.counts['holdout_rows'],
                   self.counts['dropped_holdout_rows'],
                   self.counts['users_with_train'],
                   self.counts['users_with_holdout'],
                   self.counts['users_empty_train'],
                   self.counts['users_empty_holdout'], self.seconds))
        return self

    def _flush_user(self, user, rows, writer_train, writer_holdout):
        # 与 temporal_split 完全相同的排序键：(timestamp, movieId, 原始行序)
        entries = list(enumerate(rows))
        entries.sort(key=lambda pair: (pair[1][3], pair[1][1], pair[0]))
        n = len(entries)
        if n < 2:
            train_n, test_n = n, 0
        else:
            test_n = max(1, int(n * self.test_frac))
            if n - test_n < 1:
                test_n = n - 1
            train_n = n - test_n
        if train_n == 0:
            self.counts['users_empty_train'] += 1
            self.empty_train_users.append(user)
        else:
            self.counts['users_with_train'] += 1
        if self.keep_all_train:
            for _index, row in entries[:train_n]:
                writer_train.writerow(row)
                self.counts['train_rows'] += 1
        elif user in self.eval_users:
            for _index, row in entries[:train_n]:
                writer_train.writerow(row)
                self.counts['train_rows'] += 1
        holdout_rows = entries[train_n:]
        if user in self.eval_users:
            if holdout_rows:
                self.counts['users_with_holdout'] += 1
            else:
                self.counts['users_empty_holdout'] += 1
            for _index, row in holdout_rows:
                writer_holdout.writerow(row)
                self.counts['holdout_rows'] += 1
        else:
            self.counts['dropped_holdout_rows'] += len(holdout_rows)

    def iter_train(self, chunk_rows=None):
        size = int(chunk_rows or self.chunk_rows)
        chunk = []
        with open(self.train_path, newline='') as handle:
            for record in csv.reader(handle):
                chunk.append((int(record[0]), int(record[1]),
                              float(record[2]), int(record[3])))
                if len(chunk) >= size:
                    yield chunk
                    chunk = []
        if chunk:
            yield chunk

    def iter_holdout(self, chunk_rows=None):
        size = int(chunk_rows or self.chunk_rows)
        chunk = []
        with open(self.holdout_path, newline='') as handle:
            for record in csv.reader(handle):
                chunk.append((int(record[0]), int(record[1]),
                              float(record[2]), int(record[3])))
                if len(chunk) >= size:
                    yield chunk
                    chunk = []
        if chunk:
            yield chunk


# ---------------------------------------------------------------------------
# 命中矩阵（本轮评测的核心）
# ---------------------------------------------------------------------------

def locate_in_rows(indices, indptr, rows, cols):
    """在 CSR 的每一行内查找列 ``cols`` 的**扁平下标**；找不到返回 -1。

    等价于逐行 ``searchsorted(indices[indptr[r]:indptr[r+1]], c)``，
    但向量化实现（2,000 行 × 每行数百列的规模下，逐行 Python 循环不可接受）。

    **两次失败的写法（务必不要再改回去，两种都实测出错）**：

    1. 直接对**整条** ``indices`` 调 ``np.searchsorted(indices, cols)``，再用
       ``raw - indptr[rows]`` 校正。错在 ``np.searchsorted`` 要求数组**全局有序**，
       而 CSR 的 ``indices`` 只在**行内**有序、跨行回绕（下一行又从小列号开始）。
       实测 3×6 的手工掩码得到 ``[-1,-1,-1,3,4,-1]``，行 0 的两条命中全丢。
    2. 给每行加一个"行偏移"把整条数组抬成全局单调再二分。错在**行偏移用的
       ``indptr`` 是各行的起始位置**，而行与行之间可能**没有空隙**
       （某行为空时 ``indptr[r] == indptr[r+1]``），于是相邻行的键发生**并列**，
       二分会把"属于别行"的位置判成命中。实测 2 行的手工掩码得到
       ``[0, -1, -1, 2, 4]``，第 4 项（列 21，本属行 0）被误判成行 1 的命中。

    正确做法：**逐行做二分**（每行一个独立的搜索区间），但用
    ``np.searchsorted`` 的非标量 ``sorter`` 参数做不到；这里改用**分组**
    的写法——把 (行, 列) 查询按行分组，对每一行在它自己的区间上二分。分组数
    等于**本块出现过的用户数**（最多 2,000），每组的二分是 NumPy 向量化的，
    所以总代价仍是一次 Python 级的行循环，而不是 12 万次 Python 级的列循环。

    :param indices: CSR 的 ``indices``（行内升序）。
    :param indptr: CSR 的 ``indptr``。
    :param rows: 查询的用户/行下标（int64 数组，可重复）。
    :param cols: 查询的列下标（int64 数组，与 ``rows`` 对齐）。
    :returns: 与 ``cols`` 等长的扁平下标，找不到为 -1。
    """
    indices = np.asarray(indices)
    indptr = np.asarray(indptr)
    rows = np.asarray(rows, dtype=np.int64)
    cols = np.asarray(cols, dtype=np.int64)
    found = np.full(cols.shape[0], -1, dtype=np.int64)
    if cols.shape[0] == 0 or indices.size == 0:
        return found
    # 按行分组：order 把同一行的查询聚在一起，group 给出每组在 order 里的切片。
    order = np.argsort(rows, kind='stable')
    sorted_rows = rows[order]
    boundaries = np.flatnonzero(np.diff(sorted_rows)) + 1
    group_starts = np.concatenate([[0], boundaries])
    group_stops = np.concatenate([boundaries, [sorted_rows.shape[0]]])
    for group_start, group_stop in zip(group_starts.tolist(),
                                       group_stops.tolist()):
        query_index = order[group_start:group_stop]
        row = int(sorted_rows[group_start])
        if row < 0 or row + 1 >= indptr.shape[0]:
            continue
        begin = int(indptr[row])
        end = int(indptr[row + 1])
        if end <= begin:
            continue
        segment = indices[begin:end]
        target = cols[query_index]
        at = np.searchsorted(segment, target)
        # searchsorted 只保证插入位置，"列值真的相等"必须再核一次。
        inside = at < segment.shape[0]
        hit = np.zeros(target.shape[0], dtype=bool)
        if np.any(inside):
            where = np.flatnonzero(inside)
            hit[where] = segment[at[where]] == target[where]
        if np.any(hit):
            found[query_index[hit]] = begin + at[hit]
    return found


def build_cooccurrence(A, item_ids, max_k, block_size, n_like, log=None,
                       deadline=None):
    """构造**截断后**的共现相似度表 ``C``（来源 j → 目标 i），稀疏 CSR。

    为什么必须自己算而不是用 Gram
    -----------------------------

    打分式子是 ::

        score(u, i) = Σ_{j ∈ H_u} c_ij · 1[i ∈ N_k(j)]

    展开成矩阵就是 ``A @ C``，其中 ``C[j, i] = s_ji``（``j != i``，且 ``i``
    在 ``j`` 的邻居截断后仍在表内）。

    **Gram 恒等式在本项目里是错的，有实测反例**：``(A@A.T - diag) @ A`` 看起来
    等于 ``A @ (A.T@A)``，但清掉的是 ``G[v,v] = |H_v ∩ H_v|``，而需要清的是
    ``c_ii``。反例（``.vmwork/probe_grambug.py``）：user 212 的 ``H_u = {20}``、
    item 21 ∈ H_u 且**不是任何来源的邻居**——正确分数 0，Gram 路线给出 **27**
    （= ``|H_20 ∩ H_21|``）。换句话说 Gram 把"用户已评过的电影"通过
    ``|H_u ∩ H_i|`` 又算回了分数里。**不要再走 Gram 路线。**

    截断同样不可省：``confirm.py`` 的图只保留每个来源的前 ``max_k`` 个邻居
    （实测全量模型 5,425,546 条边 / 55,174 个有邻居的来源，平均约 98 条，
    **大量来源恰好被截到 100**）。不截断会把"第 101 名之后的邻居"也算进分数，
    命中集合被放大（合成图上实测每个用户多出几十条虚假命中）。

    ``n_like`` **必须**是「全量训练段」的好评人数（**不是** ``A`` 的列和）
    ---------------------------------------------------------------------

    ``A`` 只含**评测用户**（约 2,000 行），而相似度的分母
    ``sqrt(n_like(j)·n_like(i))`` 必须用**全库每一列**的好评人数——这正是
    ``core.build_neighbors`` 的口径（它拿到的矩阵覆盖全体用户）。

    用 ``A`` 的列和当分母是一个**真实犯过的错**：「评测用户的列和」远小于
    「全库列和」，于是相似度被系统性放大（实测某条边 **0.707 vs 正确 0.5**）。
    它对 Top-20 **排序**无害（每行都乘同一个因子），但会让产物里的相似度不可比、
    并让**独立核验器的抽样对拍必然失败**——那正是这个错被发现的方式。
    因此本函数把它设为**必填参数**，不给默认值。

    :param A: ``(n_users, n_items)`` CSR（int64），训练段好评 0/1。
    :param item_ids: 全部物品的 movieId（升序），长度 = ``n_items``。
    :param max_k: 每个来源保留的邻居上限（本协议冻结为 100）。
    :param block_size: 列块大小；按块算 ``c = A_b.T @ A`` 再逐块截断落表。
    :param n_like: 长度 = ``len(item_ids)`` 的 int64 数组，**全量训练段**好评人数
        （来自 ``stream_input_pass1`` 的 ``index.n_like``）。
    :returns: ``(C, stats)``。``C`` 是 ``(n_items, n_items)`` CSR（float64），
        已去对角、每行至多 ``max_k`` 条。
    """
    n_items = len(item_ids)
    A = A.tocsr().astype(np.int64)
    if n_like is None:
        raise CorrectnessError(
            'build_cooccurrence requires the GLOBAL n_like (from the full '
            'training corpus). Deriving it from A is wrong: A holds only the '
            'evaluation users, so column sums are far below the corpus counts '
            'and every similarity is inflated by a per-column factor. Pass '
            'index.n_like from stream_input_pass1.')
    n_like = np.asarray(n_like, dtype=np.int64)
    if n_like.shape[0] != n_items:
        raise CorrectnessError('n_like length %d != n_items %d'
                               % (n_like.shape[0], n_items))
    block_size = int(block_size)
    started = time.time()
    rows_all = []
    cols_all = []
    vals_all = []
    for start in range(0, n_items, block_size):
        stop = min(start + block_size, n_items)
        block = A[:, start:stop]
        coo = (block.T @ A).tocoo()          # 行=块内来源, 列=全库目标
        if coo.nnz == 0:
            continue
        src_pos = coo.row.astype(np.int64) + start
        tgt_pos = coo.col.astype(np.int64)
        cooc = coo.data.astype(np.int64)
        keep = src_pos != tgt_pos
        if not np.any(keep):
            continue
        src_pos = src_pos[keep]
        tgt_pos = tgt_pos[keep]
        cooc = cooc[keep]
        n_src = n_like[src_pos]
        n_tgt = n_like[tgt_pos]
        keep = (cooc > 0) & (n_src > 0) & (n_tgt > 0)
        if not np.any(keep):
            continue
        src_pos = src_pos[keep]
        tgt_pos = tgt_pos[keep]
        cooc = cooc[keep]
        n_src = n_src[keep]
        n_tgt = n_tgt[keep]
        # 相似度必须用**整数先转浮点**再乘，避免 int64 溢出。
        sim = cooc.astype(np.float64) / np.sqrt(
            n_src.astype(np.float64) * n_tgt.astype(np.float64))
        # 按 (来源升序, -相似度, 目标 movieId 升序) 排一次，再按来源分组取前 k。
        order = np.lexsort((tgt_pos, -sim, src_pos))
        src_pos = src_pos[order]
        tgt_pos = tgt_pos[order]
        sim = sim[order]
        del order
        if max_k is not None and max_k > 0:
            changes = np.empty(src_pos.shape, dtype=bool)
            changes[0] = True
            changes[1:] = src_pos[1:] != src_pos[:-1]
            group_first = np.flatnonzero(changes)
            rank = (np.arange(src_pos.shape[0], dtype=np.int64)
                    - np.repeat(group_first,
                                np.diff(np.concatenate(
                                    [group_first, [src_pos.shape[0]]]))))
            keep = rank < int(max_k)
            src_pos = src_pos[keep]
            tgt_pos = tgt_pos[keep]
            sim = sim[keep]
        rows_all.append(src_pos)
        cols_all.append(tgt_pos)
        vals_all.append(sim)
        if deadline is not None:
            deadline.refresh(60.0, 'cooccurrence %d' % stop)
        if log and (start // block_size) % 32 == 0:
            log('cooccurrence: items %d/%d edges=%d (%.1fs)'
                % (stop, n_items, sum(len(v) for v in rows_all),
                   time.time() - started))
    if rows_all:
        rows = np.concatenate(rows_all)
        cols = np.concatenate(cols_all)
        vals = np.concatenate(vals_all)
    else:
        rows = np.zeros(0, dtype=np.int64)
        cols = np.zeros(0, dtype=np.int64)
        vals = np.zeros(0, dtype=np.float64)
    del rows_all, cols_all, vals_all
    C = sparse.csr_matrix((vals, (rows, cols)), shape=(n_items, n_items))
    stats = {'edges': int(C.nnz), 'items': int(n_items),
             'seconds': time.time() - started, 'max_k': int(max_k or 0)}
    return C, stats


def score_support(A, C, shape):
    """精确命中：``score(u, i) > 0 ⟺ (A @ C)[u, i] > 0``。

    ``C`` 已由 :func:`build_cooccurrence` 截断并去对角，所以这一次乘法就是
    ``confirm.py::score_itemcf_user`` 的**精确**等价式（有对拍用例钉死）。

    :returns: ``(user_pos, item_pos)``——命中坐标，``item_pos`` 是**位置下标**
        （``item_ids[pos]`` 才是 movieId）。同一对只出现一次。
    """
    n_items = int(shape[1])
    support = (A @ C).tocsr()
    coo = support.tocoo()
    if coo.nnz == 0:
        return (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
    user_pos = coo.row.astype(np.int64)
    item_pos = coo.col.astype(np.int64)
    key = user_pos * np.int64(n_items) + item_pos
    unique_key = np.unique(key)
    del key
    return (unique_key // np.int64(n_items), unique_key % np.int64(n_items))


def score_support_by_block(A, C, n_items, block_size, log=None, deadline=None,
                           on_block=None):
    """分块算命中，供调用方边算边消费（避免把所有块同时留在内存里）。

    ``on_block(chunk_start, chunk_stop, hits)`` 可选回调。传了回调时**不**累积
    返回值（返回空数组），调用方自行处理命中。
    """
    started = time.time()
    parts = []
    total = 0
    for start in range(0, n_items, int(block_size)):
        stop = min(start + int(block_size), n_items)
        sub = C[:, start:stop]
        support = (A @ sub).tocsr()
        coo = support.tocoo()
        if coo.nnz:
            user_pos = coo.row.astype(np.int64)
            item_pos = coo.col.astype(np.int64) + start
            key = user_pos * np.int64(n_items) + item_pos
            unique_key = np.unique(key)
            hits = (unique_key // np.int64(n_items),
                    unique_key % np.int64(n_items))
        else:
            hits = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
        total += int(hits[0].shape[0])
        if on_block is not None:
            on_block(start, stop, hits)
        else:
            parts.append(hits)
        if deadline is not None:
            deadline.refresh(60.0, 'score %d' % stop)
        if log and (start // int(block_size)) % 64 == 0:
            log('score support: items %d/%d hits=%d (%.1fs)'
                % (stop, n_items, total, time.time() - started))
    if on_block is not None:
        accumulated = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
    elif parts:
        accumulated = (np.concatenate([p[0] for p in parts]),
                       np.concatenate([p[1] for p in parts]))
    else:
        accumulated = (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.int64))
    del parts
    return accumulated, {'hits': total, 'seconds': time.time() - started}


def support_set(hits, n_items):
    """把 ``(user_pos, item_pos)`` 命中数组转成 ``{user_pos: set(item_pos)}``。

    只用于**小规模**对拍与产物落盘；全量 2,000 用户的命中集合约 3 万条，
    dict 形式完全可接受。
    """
    out = {}
    for p, i in zip(hits[0].tolist(), hits[1].tolist()):
        out.setdefault(p, set()).add(i)
    return out


def filter_hits_by_mask(hits, key_to_pos, candidate_mask, n_items):
    """把 :func:`hits_for_columns` 的输出按**某个方法自己的候选掩码**筛选。

    :param hits: ``(user_pos, item_pos)``（见 :func:`hits_for_columns`）。
    :param candidate_mask: ``(n_users, n_items)`` CSR，逐用户的候选池
        （``data`` 非零即"是候选"）。传 ``None`` 表示不做筛选（ItemCF 的全候选）。
    :returns: 筛选后的 ``(user_pos, item_pos)``，形状可能与输入不同。
    """
    user_pos, item_pos = hits
    if candidate_mask is None or user_pos.shape[0] == 0:
        return hits
    pos = locate_in_rows(candidate_mask.indices, candidate_mask.indptr,
                         user_pos, item_pos)
    keep = pos >= 0
    return (user_pos[keep], item_pos[keep])


def exclude_seen_from_hits(hits, seen_positions, n_items):
    """从 ItemCF 的命中里排除"该用户训练段已经评过"的物品。

    ``seen_positions``：``{用户下标: set(物品下标)}``。

    **为什么必须显式排除**：``A[u, i] > 0`` 说明 ``i`` 在 ``Seen_u`` 里；当 ``i``
    在共现表里也有行（即 ``i`` 是来源之一）时，``(A @ C)[u, i]`` **可能非零**，
    于是"已评过的电影"会被当成命中。这在 ``confirm.py::score_itemcf_user`` 里由
    ``if target in seen: continue`` 排除。
    """
    user_pos, item_pos = hits
    if user_pos.shape[0] == 0:
        return hits
    keep = np.ones(user_pos.shape[0], dtype=bool)
    for p in np.unique(user_pos):
        seen = seen_positions.get(int(p))
        if not seen:
            continue
        mask = user_pos == p
        rows = np.flatnonzero(mask)
        keep[rows] = np.asarray([int(item_pos[r]) not in seen for r in rows],
                                dtype=bool)
    return (user_pos[keep], item_pos[keep])


def build_candidate_mask(row_specs, shape, key_to_pos, item_ids):
    """按"每个用户一行、每行一批候选"构造候选掩码 CSR。

    ``row_specs[u]`` 是一个可迭代对象，元素是**该方法的候选物品 movieId**——
    调用方按方法分别传：

    - ``MostPop`` → ``confirm.top_n_excluding(order_all, seen[u], 20)``
    - ``LikePop`` → ``confirm.top_n_excluding(order_like, seen[u], 20)``
    - ``ItemCF``  → 传 ``None``（全候选，不做掩码筛选；只需显式排除 ``Seen_u``）

    **为什么必须传"精确的候选集合"而不是"全库排序前缀"**（两次真实错误）：

    1. 早先传的是全库排序的前 ``|Seen_u| + 20`` 名。那是候选集的**超集**
       （数学上足够，因为前 ``|Seen_u|+20`` 名里至多 ``|Seen_u|`` 个是 seen 的），
       但会把**永远不会被返回**的电影也标成候选。
    2. 更严重的一次：把这个"20+20 并集"套在所有三个方法头上。三个方法的候选池
       **本来就不同**——人气基线只有 20 个候选，ItemCF 有全库候选。套错以后
       ItemCF 的命中被砍到 0（300 用户实测）。

    同一用户内的重复候选会被**累加**（``data`` 取值 1 或 2），表示"该电影在这个
    用户的候选池里出现几次"——同名次电影（``n_all == n_like``）在两个基线里同时
    出现时才是 2。

    :returns: ``(n_users, n_items)`` CSR（int64，行内列升序、无重复）。
    """
    indptr = np.zeros(len(row_specs) + 1, dtype=np.int64)
    indices_parts = []
    data_parts = []
    for u, items in enumerate(row_specs):
        if items is None:
            indptr[u + 1] = indptr[u]
            continue
        items = np.asarray(list(items), dtype=np.int64)
        if items.size == 0:
            indptr[u + 1] = indptr[u]
            continue
        columns = np.asarray([key_to_pos[int(i)] for i in items],
                             dtype=np.int64)
        columns.sort()
        unique, counts = np.unique(columns, return_counts=True)
        indices_parts.append(unique)
        data_parts.append(counts.astype(np.int64))
        indptr[u + 1] = indptr[u] + unique.shape[0]
    indices = np.concatenate(indices_parts) if indices_parts else \
        np.zeros(0, dtype=np.int64)
    data = np.concatenate(data_parts) if data_parts else np.zeros(0,
                                                                 dtype=np.int64)
    return sparse.csr_matrix((data, indices, indptr), shape=shape, dtype=np.int64)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------

def per_user_metrics(recommendations, relevants, k=TOP_N):
    """逐用户 ``recall@k`` / ``ndcg@k``（复用 pilot 的指标函数，口径唯一）。"""
    if len(recommendations) != len(relevants):
        raise ValueError('recommendations and relevants length mismatch')
    recalls = []
    ndcgs = []
    for index, (rec, rel) in enumerate(zip(recommendations, relevants)):
        rel_set = set(rel)
        if not rel_set:
            raise ValueError('empty relevant set for row %d' % index)
        rec_list = list(rec)
        if len(rec_list) != len(set(rec_list)):
            raise ValueError('duplicate recommendations for row %d' % index)
        recalls.append(recall_at_k(rec_list, rel_set, k) or 0.0)
        ndcgs.append(ndcg_at_k(rec_list, rel_set, k) or 0.0)
    return recalls, ndcgs


def paired_bootstrap(values_a, values_b, seed=BOOTSTRAP_SEED,
                     repeats=BOOTSTRAP_REPEATS):
    """配对 bootstrap：同用户索引有放回抽样，均值差 + 95% percentile 区间。

    逐用户差值预先算好（2000 次 × 2000 用户的浮点累加，约 0.6 s），
    与上一轮 ``confirm.py::paired_bootstrap`` 同一算法与同一 seed。
    """
    if len(values_a) != len(values_b):
        raise ValueError('paired bootstrap requires equal-length per-user values')
    if not values_a:
        raise ValueError('paired bootstrap requires non-empty values')
    diffs = [float(a) - float(b) for a, b in zip(values_a, values_b)]
    n = len(diffs)
    mean_diff = sum(diffs) / float(n)
    rng = random.Random(seed)
    samples = []
    for _ in range(repeats):
        total = 0.0
        for _ in range(n):
            total += diffs[rng.randrange(n)]
        samples.append(total / n)
    ordered = sorted(samples)
    low = ordered[int(math.floor(0.025 * (repeats - 1)))]
    high = ordered[int(math.ceil(0.975 * (repeats - 1)))]
    baseline_mean = sum(float(v) for v in values_b) / float(n)
    return {
        'mean_difference': mean_diff,
        'ci95': [low, high],
        'relative_difference': (mean_diff / baseline_mean) if baseline_mean else None,
        'excludes_zero': bool(low > 0.0 or high < 0.0),
        'seed': seed, 'repeats': repeats, 'users': n,
    }


def verdict_detail(metrics, bootstrap):
    """把判定依据逐条摊开（供产物与结论使用，避免只有一句结论）。

    :returns: dict，含 ``stronger_baseline``、逐基线的 ``beats`` 与区间下界、
    以及最终 ``verdict``。
    """
    baselines = [name for name in ('MostPop', 'LikePop') if name in metrics]
    itemcf = metrics.get('ItemCF') or {}
    stronger = None
    for name in baselines:
        if stronger is None or \
                metrics[name]['ndcg@20'] > metrics[stronger]['ndcg@20']:
            stronger = name
    detail = {'stronger_baseline': stronger, 'baselines': {}}
    for name in baselines:
        entry = {'ndcg_beats': bool(itemcf.get('ndcg@20', 0.0)
                                    > metrics[name]['ndcg@20']),
                 'recall_beats': bool(itemcf.get('recall@20', 0.0)
                                      > metrics[name]['recall@20']),
                 'ndcg_difference': (itemcf.get('ndcg@20', 0.0)
                                     - metrics[name]['ndcg@20']),
                 'recall_difference': (itemcf.get('recall@20', 0.0)
                                       - metrics[name]['recall@20'])}
        stats = bootstrap.get('ItemCF_vs_%s' % name) or {}
        for key in ('ndcg@20', 'recall@20'):
            block = stats.get(key) or {}
            ci = block.get('ci95') or [None, None]
            entry['%s_ci95' % key] = ci
            entry['%s_excludes_zero' % key] = bool(block.get('excludes_zero'))
            entry['%s_relative' % key] = block.get('relative_difference')
        detail['baselines'][name] = entry
    if stronger:
        detail['stronger_ndcg_ci95'] = \
            detail['baselines'][stronger].get('ndcg@20_ci95')
        detail['stronger_recall_ci95'] = \
            detail['baselines'][stronger].get('recall@20_ci95')
    return detail


def verdict_of(metrics, bootstrap):
    """按**预先冻结**的规则判定（协议 §5）。不按结果调整规则。

    规则（与上一轮同构，只把"全量规模"写进结论措辞里）：

    1. NDCG 与 Recall 均超过**两个**基线，且对**较强基线**的 NDCG 与 Recall
       区间下界都 > 0 → :data:`VERDICT_SUPPORTED`；
    2. 点估计全面领先但某个区间跨零 → :data:`VERDICT_CROSSES_ZERO`（证据不足）；
    3. 有任一指标没超过任一基线 → :data:`VERDICT_NOT_REPRODUCED`。
    """
    if not metrics or 'ItemCF' not in metrics:
        return VERDICT_FAILURE
    baselines = [name for name in ('MostPop', 'LikePop') if name in metrics]
    if not baselines:
        return VERDICT_FAILURE
    stronger = None
    for name in baselines:
        if stronger is None or metrics[name]['ndcg@20'] > metrics[stronger]['ndcg@20']:
            stronger = name
    itemcf = metrics['ItemCF']
    ndcg_beats = all(itemcf['ndcg@20'] > metrics[name]['ndcg@20']
                     for name in baselines)
    recall_beats = all(itemcf['recall@20'] > metrics[name]['recall@20']
                       for name in baselines)
    if not (ndcg_beats and recall_beats):
        return VERDICT_NOT_REPRODUCED
    ndcg_block = (bootstrap.get('ItemCF_vs_%s' % stronger, {})
                  .get('ndcg@20') or {})
    recall_block = (bootstrap.get('ItemCF_vs_%s' % stronger, {})
                    .get('recall@20') or {})
    ndcg_lower = (ndcg_block.get('ci95') or [None, None])[0]
    recall_lower = (recall_block.get('ci95') or [None, None])[0]
    if ndcg_lower is None or recall_lower is None:
        return VERDICT_CROSSES_ZERO
    if ndcg_lower > 0.0 and recall_lower > 0.0:
        return VERDICT_SUPPORTED
    return VERDICT_CROSSES_ZERO


def per_user_recommendations(users, item_ids, hits_map, scores_map, seen_pos,
                             top_n=TOP_N):
    """把命中集合整理成每个方法的 Top-20 推荐列表（ItemCF 侧）。

    ``ItemCF``：命中集合（排除 ``Seen_u``）内按精确分数 `(-score, movieId)` 排序，
    **只返回分数 > 0 的条目**，不做热门或零分补齐（沿用既有口径，也是
    "满额率 < 1" 的来源）。人气基线的推荐由调用方直接按 ``top_n_excluding`` 得到。

    :param users: 评测用户的 **位置下标**列表（顺序即输出顺序）。
    :param hits_map: ``{用户位置: set(物品位置)}``，ItemCF 命中（未排除 seen）。
    :param scores_map: ``{用户位置: {物品位置: 精确分数}}``，用于排序。
    :param seen_pos: ``{用户位置: set(物品位置)}``，训练段已评电影。
    :returns: ``{用户位置: [movieId, ...]}``。
    """
    out = {}
    for p in users:
        seen = seen_pos.get(p, set())
        scored = []
        for pos, score in (scores_map.get(p) or {}).items():
            if score <= 0.0 or pos in seen:
                continue
            scored.append((pos, score))
        scored.sort(key=lambda row: (-row[1], item_ids[row[0]]))
        out[p] = [item_ids[pos] for pos, _score in scored[:top_n]]
    return out


def top_n_popularity(order_pos, seen, top_n=TOP_N):
    """按已排好序的**位置下标**列表排除 ``seen`` 取前 ``top_n``。

    与 ``confirm.top_n_excluding`` 同语义（后者吃 movieId，这里吃位置），
    人气基线的候选池就是这一段前缀的最前面 ``top_n`` 个。
    """
    picked = []
    for pos in order_pos:
        if pos in seen:
            continue
        picked.append(pos)
        if len(picked) >= top_n:
            break
    return picked


def popularity_orders(n_all, n_like, n_items):
    """MostPop / LikePop 的全库排序（位置下标），平票按 movieId 升序。

    返回 ``(order_all, order_like)``，元素是位置下标。排序键与
    ``confirm.most_popular_order`` / ``like_popular_order`` 一致
    （后者按 movieId 排，而 ``item_ids`` 升序，所以两者等价）。
    """
    positions = np.arange(n_items, dtype=np.int64)
    order_all = sorted(positions.tolist(),
                       key=lambda p: (-int(n_all[p]), p))
    order_like = sorted(positions.tolist(),
                        key=lambda p: (-int(n_like[p]), p))
    return order_all, order_like


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def freeze_user_lists(input_path, out_dir, project_root=PROJECT_ROOT,
                      cohort_size=COHORT_SIZE, eval_size=EVAL_SIZE,
                      prior_exclusions=None, log=None, deadline=None,
                      dump_slice=False):
    """冻结评测名单与切片（**先落盘、后算指标**）。

    步骤：

    1. 排除名单 = 旧轮次全部名单的并集（``load_prior_exclusions``）∪ 调用方显式
       传入的 ``prior_exclusions``；
    2. 扫一遍输入，得每用户行数与全量物品计数（``stream_input_pass1``）；
    3. 可用用户 = 全部用户 − 排除名单；按 ``sha256(EVAL_SALT + userId)`` 升序取
       前 ``eval_size`` 人作为**评测名单**；
    4. 只看评测用户的评分写一份**保序切片** ``slice_ratings.csv``；
    5. 落盘 ``users.json``（名单 + 哈希 + 规模）、``eval_users.txt``、
       ``excluded_users.txt``。

    :returns: ``(slice_path, users_payload, index)``；``index`` 是全量扫描的
        ``StreamingIndex``（物品全集、``n_all``、``n_like`` 都在里面）。
    """
    from experiments.itemcf.holdout_common import stream_input_pass1

    started = time.time()
    prior_users, provenance = load_prior_exclusions(project_root)
    excluded = set(prior_users)
    if prior_exclusions:
        excluded |= set(int(u) for u in prior_exclusions)
    if log:
        log('prior exclusions: %d user(s) from %d source(s)'
            % (len(excluded), len(provenance)))

    if deadline is not None:
        deadline.refresh(600.0, 'pass1')
    index = stream_input_pass1(input_path, POSITIVE_THRESHOLD, log=log)
    if deadline is not None:
        deadline.refresh(600.0, 'pass1 done')

    available = [int(u) for u in index.user_ids if int(u) not in excluded]
    eval_users = freeze_eval_users(available, eval_size, EVAL_SALT)
    if not eval_users:
        raise CorrectnessError('no evaluable users after exclusion')
    eval_set = set(eval_users)
    if log:
        log('users: total=%d excluded=%d available=%d eval=%d'
            % (len(index.user_ids), len(excluded), len(available),
               len(eval_users)))

    # 注意：本函数**不再**写评测用户切片（slice_ratings.csv）。
    # 训练段必须来自**全体用户**（见 `RatingSession` 的 docstring），所以切片
    # 对主流程已经没用；留一份只评测用户的中间件既费时（约 2 分钟 I/O）
    # 又容易让人误以为构图用的是它。切片只在 `--dump-slice` 时落盘供排查。
    slice_path = None
    kept = 0
    if dump_slice:
        slice_path = os.path.join(out_dir, 'slice_ratings.csv')
        with open(input_path, newline='') as src, \
                open(slice_path, 'w', newline='') as dst:
            reader = csv.reader(src)
            writer = csv.writer(dst)
            header = next(reader, None)
            writer.writerow(header or ['userId', 'movieId', 'rating',
                                       'timestamp'])
            for record in reader:
                if int(record[0]) in eval_set:
                    writer.writerow(record[:4])
                    kept += 1
        if log:
            log('debug slice written: %s rows=%d' % (slice_path, kept))

    payload = {
        'eval_size': len(eval_users),
        'excluded_size': len(excluded),
        'eval_salt': EVAL_SALT,
        'eval_users': eval_users,
        'eval_sha256': user_list_sha256(eval_users),
        'excluded_sha256': user_list_sha256(sorted(excluded)),
        'selection_rule': {
            'eval': 'available users sorted by (sha256("%s" + str(userId)), '
                    'userId) ascending, take first %d' % (EVAL_SALT, eval_size),
            'user_id_format': 'original positive integer, decimal without '
                              'leading zeros',
            'no_activity_or_outcome_filtering': True,
        },
        'input': {'path': os.path.abspath(input_path),
                  'bytes': os.path.getsize(input_path),
                  'sha256': file_sha256(input_path),
                  'rows': index.input_rows,
                  'users': index.n_users,
                  'items': index.n_items},
        'counts': {'total_users': int(len(index.user_ids)),
                   'excluded_users': int(len(excluded)),
                   'available_users': int(len(available)),
                   'eval_users': int(len(eval_users)),
                   'slice_rows': int(kept)},
        'prior_exclusions': provenance,
        'slice_sha256': (file_sha256(slice_path) if slice_path else None),
        'seconds': time.time() - started,
        'environment_note': 'list freezing only; no metric is computed here',
    }
    write_user_list_file(os.path.join(out_dir, 'eval_users.txt'), eval_users)
    write_user_list_file(os.path.join(out_dir, 'excluded_users.txt'),
                         sorted(excluded))
    write_json_atomic(os.path.join(out_dir, 'users.json'), payload)
    if log:
        log('user lists frozen: %s (%d eval users)'
            % (os.path.join(out_dir, 'users.json'), len(eval_users)))
    return slice_path, payload, index


def write_user_list_file(path, user_ids):
    with open(path, 'w', encoding='utf-8', newline='') as handle:
        for user in user_ids:
            handle.write('%d\n' % int(user))
    return path


def build_graph_matrix(split_dir, item_index, log=None, deadline=None,
                       chunk_rows=1 << 18):
    """构造**全体用户**训练段的好评矩阵（本轮"全量训练数据"的落点）。

    ``split_train.csv`` 是**全部**用户（200,948）的训练段，所以这里扫出来的
    就是全量共现的分子。返回的 ``A_graph`` 的行下标是**流式分配**的
    （第 k 个新用户 → 行 k-1），本函数不保留用户维度，因为下游只把它喂给
    ``A_graph.T @ A_graph`` 这类统计。

    **为什么要单独一遍**：``RatingSession.iter_train`` 会再读一次同一个文件；
    本函数与它读的是同一份磁盘中间件，所以两边看到的数据完全一致。

    内存纪律：只累积 COO 三元组（约 1,277 万条 × 8 字节 × 3 ≈ 306 MB），
    最后一次性建 CSR。这是全量共现**必须**付的成本（分子就是这么大的稀疏矩阵）。
    """
    started = time.time()
    user_index = {}
    r_list = array.array('q')
    c_list = array.array('q')
    rows_seen = 0
    with open(os.path.join(split_dir, TRAIN_CSV), newline='') as handle:
        reader = csv.reader(handle)
        for record in reader:
            rows_seen += 1
            user = int(record[0])
            pos = user_index.get(user)
            if pos is None:
                pos = len(user_index)
                user_index[user] = pos
            if float(record[2]) >= POSITIVE_THRESHOLD:
                item_p = item_index.get(int(record[1]))
                if item_p is None:
                    raise CorrectnessError(
                        'train item %d missing from the global item index'
                        % int(record[1]))
                r_list.append(pos)
                c_list.append(item_p)
    n_users = len(user_index)
    n_items = len(item_index)
    A = sparse.csr_matrix(
        (np.ones(len(r_list), dtype=np.int64),
         (np.frombuffer(r_list, dtype=np.int64).copy(),
          np.frombuffer(c_list, dtype=np.int64).copy())),
        shape=(max(1, n_users), n_items), dtype=np.int64)
    if log:
        log('graph matrix: users=%d items=%d nnz=%d train_rows=%d (%.1fs)'
            % (n_users, n_items, int(A.nnz), rows_seen,
               time.time() - started))
    del r_list, c_list, user_index
    gc.collect()
    return A, {'graph_users': n_users, 'graph_nnz': int(A.nnz),
               'graph_train_rows': rows_seen,
               'seconds': time.time() - started}


def counts_from_split(split_dir, item_index, log=None):
    """从 ``split_train.csv``（**全量训练段**）算每部电影的 ``n_all`` / ``n_like``。

    **为什么不能用全量 ``ratings.csv`` 的计数**：共现的分子来自训练段，
    分母（``n_like``）也必须来自训练段，否则两者口径不一致。用全量计数会把
    留出段的好评也算进分母，相似度被系统性压小——独立核验器的抽样对拍
    会立刻抓到（实测正是如此）。

    **为什么也不用 ``A_graph`` 的列和**：``A_graph`` 只含**好评**（0/1），
    它的列和就是 ``n_like``，但 ``n_all``（MostPop 用）需要**全部**评分。
    两者用同一次扫描得到，保证一致。

    :returns: ``(n_all, n_like)``，均为长度 = ``len(item_index)`` 的 int64 数组。
    """
    n_items = len(item_index)
    n_all = np.zeros(n_items, dtype=np.int64)
    n_like = np.zeros(n_items, dtype=np.int64)
    rows = 0
    with open(os.path.join(split_dir, TRAIN_CSV), newline='') as handle:
        for record in csv.reader(handle):
            rows += 1
            pos = item_index.get(int(record[1]))
            if pos is None:
                raise CorrectnessError(
                    'train item %d missing from the global item index'
                    % int(record[1]))
            n_all[pos] += 1
            if float(record[2]) >= POSITIVE_THRESHOLD:
                n_like[pos] += 1
    if log:
        log('counts from TRAIN split: rows=%d items=%d positive_pairs=%d'
            % (rows, n_items, int(n_like.sum())))
    return n_all, n_like


def build_eval_matrix(split_dir, eval_users, item_index, log=None):
    """读切分好的训练/留出段，构造评测用户侧的全部评测输入。

    :returns: ``(A, seen_pos, history_pos, relevants_pos, stats)``：

    - ``A``：``(n_eval, n_items)`` CSR（int64），评测用户**训练段好评** 0/1；
    - ``seen_pos``：``{用户位置: set(物品位置)}``，训练段全部评分（**含低分**）；
    - ``relevants_pos``：``{用户位置: set(物品位置)}``，留出段好评（= ``R_u``）。
    """
    user_index = {}
    user_ids = []
    seen_pos, relevants_pos = {}, {}
    r_list, c_list = [], []
    stats = {'train_rows': 0, 'holdout_rows': 0, 'train_users': 0,
             'evaluable_users': 0, 'dropped_holdout_rows': 0}

    def pos_of(user):
        p = user_index.get(user)
        if p is None:
            p = len(user_ids)
            user_index[user] = p
            user_ids.append(user)
            seen_pos[p] = set()
            relevants_pos[p] = set()
        return p

    with open(os.path.join(split_dir, TRAIN_CSV), newline='') as handle:
        for record in csv.reader(handle):
            user = int(record[0])
            if user not in eval_users:
                continue
            stats['train_rows'] += 1
            p = pos_of(user)
            item_p = item_index.get(int(record[1]))
            if item_p is None:
                raise CorrectnessError(
                    'train item %d missing from the global item index'
                    % int(record[1]))
            seen_pos[p].add(item_p)
            if float(record[2]) >= POSITIVE_THRESHOLD:
                r_list.append(p)
                c_list.append(item_p)
    with open(os.path.join(split_dir, HOLDOUT_CSV), newline='') as handle:
        for record in csv.reader(handle):
            user = int(record[0])
            if user not in eval_users:
                stats['dropped_holdout_rows'] += 1
                continue
            stats['holdout_rows'] += 1
            p = pos_of(user)
            if float(record[2]) >= POSITIVE_THRESHOLD:
                relevants_pos[p].add(item_index[int(record[1])])
    stats['train_users'] = len(user_ids)
    stats['evaluable_users'] = sum(1 for p in relevants_pos
                                   if relevants_pos[p])
    shape = (max(1, len(user_ids)), len(item_index))
    A = sparse.csr_matrix((np.ones(len(r_list), dtype=np.int64),
                           (np.asarray(r_list, dtype=np.int64),
                            np.asarray(c_list, dtype=np.int64))),
                          shape=shape, dtype=np.int64)
    if log:
        log('eval matrix: users=%d A.nnz=%d train_rows=%d holdout_rows=%d '
            'evaluable=%d' % (len(user_ids), int(A.nnz), stats['train_rows'],
                              stats['holdout_rows'], stats['evaluable_users']))
    return A, seen_pos, relevants_pos, stats


def run_evaluation(out_dir, A_eval, A_graph, item_ids, seen_pos, relevants_pos,
                   n_all, n_like, block_size, user_ids=None, log=None,
                   deadline=None, model_dir=None):
    """本轮的主计算：全量共现 → 精确分数 → 三个方法的指标。

    **两个矩阵，别混用**（第一版在这里犯过错，见 docstring 底部）：

    - ``A_graph``：**全体用户**的训练段好评（约 200,948 × 84,432）。
      共现 ``C`` 由它构建，所以**分子（共现）与分母（``n_like``）同口径**，
      都是"全量训练段"的统计。这就是"全量规模的训练数据"。
    - ``A_eval``：**只看评测用户**的训练段好评（约 2,000 × 84,432）。
      打分 ``support = A_eval @ C`` 只用它的行——打分只需评测用户的历史。

    为什么不能只用一个矩阵
    ----------------------

    第一版让 ``A`` 同时承担两个角色（只含 2,000 评测用户），却拿**全库**
    ``n_like`` 归一化。那是**分子分母不同口径**：全库 ``n_like`` 可达 4 万，
    而 2,000 人之间的共现最多几十，相似度被压到 ``1e-4`` 量级，top-100 截断
    后所有用户只能拿到同一批高 support 电影。实测后果是 ItemCF 的
    NDCG@20 与 Recall@20 **双双变成 0.0**、目录覆盖率只剩 **202** 部电影。

    那不是"优势消失"，是**口径写错**——所以本函数把两个矩阵显式分开命名，
    并且要求 :func:`build_cooccurrence` 的 ``n_like`` 与 ``A_graph`` 同源。

    :param user_ids: 位置下标 → 真实 userId（按 ``A_eval`` 的行序）；
        只用于逐用户产物的 ``user_id`` 列，取值不影响任何指标。
    :returns: ``(metrics, bootstrap, per_user_rows, rec_rows, extra)``
    """
    started = time.time()
    n_graph_users, n_items = A_graph.shape
    if A_eval.shape[1] != n_items:
        raise CorrectnessError('A_eval and A_graph disagree on n_items: %d vs %d'
                               % (A_eval.shape[1], n_items))
    order_all, order_like = popularity_orders(n_all, n_like, n_items)
    if log:
        log('popularity orders ready (items=%d); graph matrix %d x %d nnz=%d'
            % (n_items, n_graph_users, n_items, int(A_graph.nnz)))

    # C 由**全量训练段**构建：分子是全体用户的共现，分母是同一批用户的 n_like。
    # --model-dir 给了就用外部图顶替（别的引擎产出的同一张图），下游打分
    # 代码一行不动——因为下游只做 A @ C，而 C 只是 (row, col, value) 三元组。
    if model_dir is not None:
        C, cstats = load_cooccurrence_from_model_dir(model_dir, item_ids,
                                                     log=log)
    else:
        C, cstats = build_cooccurrence(A_graph, item_ids, MAX_K, block_size,
                                       np.asarray(n_like, dtype=np.int64),
                                       log=log, deadline=deadline)
    if log:
        log('cooccurrence built: edges=%d (%.1fs)'
            % (cstats['edges'], cstats['seconds']))
    if deadline is not None:
        deadline.refresh(900.0, 'score')

    # 落盘共现矩阵：**独立核验器**（verify_holdout_eval.py）要拿它做抽样对拍。
    # 存成 3 个 npy 而不是 npz 的压缩容器，核验器可以直接 mmap 读。
    np.save(os.path.join(out_dir, 'cooccurrence_indptr.npy'),
            np.asarray(C.indptr, dtype=np.int64), allow_pickle=False)
    np.save(os.path.join(out_dir, 'cooccurrence_indices.npy'),
            np.asarray(C.indices, dtype=np.int64), allow_pickle=False)
    np.save(os.path.join(out_dir, 'cooccurrence_similarities.npy'),
            np.asarray(C.data, dtype=np.float64), allow_pickle=False)
    if log:
        log('cooccurrence dumped (indptr/indices/similarities)')

    # 打分只取评测用户的行。
    A = A_eval
    support = (A @ C).tocsr()
    del C
    gc.collect()
    if log:
        log('support computed: nnz=%d (%.1fs)' % (int(support.nnz),
                                                  time.time() - started))

    evaluable = sorted(p for p in seen_pos if relevants_pos.get(p))
    skipped_no_positive = sorted(p for p in seen_pos
                                 if not relevants_pos.get(p))

    per_method = dict((name, {}) for name in METHODS)
    for p in evaluable:
        seen = seen_pos[p]
        start, stop = support.indptr[p], support.indptr[p + 1]
        cols = support.indices[start:stop]
        vals = support.data[start:stop]
        keep = np.asarray([int(c) not in seen for c in cols.tolist()],
                          dtype=bool)
        cols = cols[keep]
        vals = vals[keep]
        order = np.lexsort((item_ids[cols], -vals))
        picked = cols[order[:TOP_N]].tolist()
        per_method['ItemCF'][p] = [item_ids[pos] for pos in picked]
        per_method['MostPop'][p] = [
            item_ids[pos] for pos in top_n_popularity(order_all, seen, TOP_N)]
        per_method['LikePop'][p] = [
            item_ids[pos] for pos in top_n_popularity(order_like, seen, TOP_N)]
    if log:
        log('recommendations built for %d users (%.1fs)'
            % (len(evaluable), time.time() - started))

    relevants = [set(item_ids[pos] for pos in relevants_pos[p])
                 for p in evaluable]
    metrics = {}
    per_user_metric = {}
    for name in METHODS:
        recs = [per_method[name][p] for p in evaluable]
        recalls, ndcgs = per_user_metrics(recs, relevants, TOP_N)
        per_user_metric[name] = {'recall': recalls, 'ndcg': ndcgs,
                                 'recs': recs}
        metrics[name] = {
            'ndcg@20': sum(ndcgs) / len(ndcgs),
            'recall@20': sum(recalls) / len(recalls),
            'users_scored': len(evaluable),
            'full_list_rate': (sum(1 for r in recs if len(r) >= TOP_N)
                               / float(len(recs))),
            'empty_list_rate': (sum(1 for r in recs if not r) / float(len(recs))),
            'returned_count': sum(len(r) for r in recs),
            'catalog_coverage': (len(set(i for r in recs for i in r))
                                 / float(n_items)),
        }
        if log:
            log('%-8s ndcg@20=%.9f recall@20=%.9f full=%.4f cov=%.6f'
                % (name, metrics[name]['ndcg@20'], metrics[name]['recall@20'],
                   metrics[name]['full_list_rate'],
                   metrics[name]['catalog_coverage']))

    bootstrap = {}
    for baseline in ('MostPop', 'LikePop'):
        bootstrap['ItemCF_vs_%s' % baseline] = {
            'ndcg@20': paired_bootstrap(per_user_metric['ItemCF']['ndcg'],
                                        per_user_metric[baseline]['ndcg']),
            'recall@20': paired_bootstrap(per_user_metric['ItemCF']['recall'],
                                          per_user_metric[baseline]['recall']),
        }
    if log:
        log('bootstrap done (seed=%d repeats=%d)'
            % (BOOTSTRAP_SEED, BOOTSTRAP_REPEATS))

    def user_id_of(index_pos):
        if user_ids and index_pos < len(user_ids):
            return int(user_ids[index_pos])
        return index_pos

    per_user_rows = []
    for index, p in enumerate(evaluable):
        row = {'user_id': user_id_of(p),
               'relevant_count': len(relevants_pos[p]),
               'candidate_count': n_items - len(seen_pos[p]),
               'seen_count': len(seen_pos[p]),
               'ItemCF_returned': len(per_user_metric['ItemCF']['recs'][index]),
               'MostPop_returned': len(per_user_metric['MostPop']['recs'][index]),
               'LikePop_returned': len(per_user_metric['LikePop']['recs'][index])}
        for name in METHODS:
            row['%s_ndcg@20' % name] = per_user_metric[name]['ndcg'][index]
            row['%s_recall@20' % name] = per_user_metric[name]['recall'][index]
        per_user_rows.append(row)
    rec_rows = []
    for index, p in enumerate(evaluable):
        for name in METHODS:
            for rank, item in enumerate(per_user_metric[name]['recs'][index], 1):
                rec_rows.append({'user_id': per_user_rows[index]['user_id'],
                                 'method': name, 'rank': rank, 'movie_id': item})
    extra = {'evaluable_users': len(evaluable),
             'skipped_no_positive': len(skipped_no_positive),
             'cooccurrence': cstats,
             'support_nnz': int(support.nnz),
             'seconds': time.time() - started}
    return metrics, bootstrap, per_user_rows, rec_rows, extra


def load_cooccurrence_from_model_dir(model_dir, item_ids, log=None):
    """从**已落盘的模型目录**读共现表，替代 :func:`build_cooccurrence` 的现算结果。

    用途：让同一份 holdout 协议能跑在**别的训练引擎产出的图**上。典型场景是
    Spark 版 ItemCF（``spark/train_itemcf.py`` 产出 ``engine: pyspark`` 的
    ``model/`` 目录）与本脚本现算的 scipy 版图逐边一致，从而"同协议同指标"
    可被实测复现，而不只是由"两个模型逐边全等"间接推出。

    **返回的 ``C`` 与 :func:`build_cooccurrence` 的返回同型同义**：来源
    ``j`` 的邻居区间是 ``[indptr[j], indptr[j+1])``，``indices`` 存**目标
    下标**、``data`` 存相似度——因为下游只做 ``A @ C``（``C`` 是"来源行 ×
    目标列"的转置视图），矩阵乘法只认 ``(row, col, value)`` 三元组，谁算的
    它不关心。

    :param model_dir: 模型目录（7 个 npy + model.json，契约见
        ``experiments.itemcf.model_io`` 的模块 docstring）。
    :param item_ids: **本评测口径**的物品顺序（本脚本 ``item_ids`` 列表）。
    :returns: ``(C, stats)``，``C`` 是 ``scipy.sparse.csr_matrix``。
    :raises CorrectnessError: 物品集合与 ``item_ids`` 不一致时——
        下标语义全靠两边同序，错位会产出**看似正常但全错**的图。
    """
    started = time.time()
    from experiments.itemcf import model_io

    arrays = model_io.load_arrays(model_dir, mmap_mode='r')
    model_item_ids = np.asarray(arrays['item_ids'])
    if len(model_item_ids) != len(item_ids) or \
            not np.array_equal(model_item_ids,
                               np.asarray(item_ids, dtype=np.int64)):
        raise CorrectnessError(
            'model dir item_ids differ from the evaluation item order '
            '(model %d items, eval %d items); positional semantics would be '
            'silently wrong'
            % (len(model_item_ids), len(item_ids)))
    indptr = np.asarray(arrays['indptr'], dtype=np.int64)
    indices = np.asarray(arrays['target_indices'], dtype=np.int32)
    data = np.asarray(arrays['similarities'], dtype=np.float64)
    coocs = np.asarray(arrays['cooccurrences'], dtype=np.int64)

    C = sparse.csr_matrix((data, indices, indptr),
                          shape=(len(item_ids), len(item_ids)))
    # 键名**必须与 build_cooccurrence 一致**：下游 1428 行的进度日志与
    # extra['cooccurrence']（落进 run_status.json，verify_holdout_eval 会读）
    # 都按 'edges' / 'seconds' 取值。返回另一套名字会在写日志时 KeyError——
    # 那正是这个入口第一版的失败方式（图已正确加载却崩在下一行）。
    stats = {
        'source': 'model_dir',
        'model_dir': os.path.abspath(model_dir),
        # 'edges'/'seconds' 是与现算路径对齐的别名；'n_edges' 保留是因为
        # 本函数内部用它来构造上面那行日志，两者同一个值。
        'edges': int(C.nnz),
        'n_edges': int(C.nnz),
        'n_sources_with_neighbors': int(np.count_nonzero(np.diff(indptr))),
        # 'items' / 'max_k' 与现算路径对齐：产物里记录的是"这张图覆盖多少物品、
        # 每来源截断到多少"，缺了它们，独立核验器 verify_holdout_eval 读
        # extra['cooccurrence'] 时会拿不到这两个口径。
        'items': int(len(item_ids)),
        'max_k': int(MAX_K),
        'min_similarity': float(data.min()) if C.nnz else None,
        'max_similarity': float(data.max()) if C.nnz else None,
        # 外部图的**加载**耗时（本函数从 mmap 到构造 csr_matrix 的时间）。
        # 必须是 float：下游 run_evaluation 的进度日志用 %.1f 格式化它，
        # 给 None 会 TypeError（第一版正是这么崩的——为了"不编数"而诚实，
        # 结果比编一个数更糟）。加载本来就有成本，测出来是事实。
        'seconds': float(time.time() - started),
    }
    if log:
        log('cooccurrence loaded from model dir: edges=%d sources=%d (%.1fs) %s'
            % (stats['edges'], stats['n_sources_with_neighbors'],
               stats['seconds'], stats['model_dir']))
    return C, stats


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog='python -m experiments.itemcf.heldout_eval',
        description='Full-scale held-out evaluation: ItemCF vs MostPop vs '
                    'LikePop on the full training corpus.')
    parser.add_argument('--input', required=True,
                        help='完整评分 CSV（VM: /home/h/movielens/ratings.csv）')
    parser.add_argument('--out', required=True,
                        help='产物目录；已存在则拒绝覆盖（保证 run 独占）')
    parser.add_argument('--eval-size', type=int, default=EVAL_SIZE)
    parser.add_argument('--block-size', type=int, default=DEFAULT_BLOCK_SIZE)
    parser.add_argument('--chunk-rows', type=int, default=1 << 18)
    parser.add_argument('--max-k', type=int, default=MAX_K)
    parser.add_argument('--top-n', type=int, default=TOP_N)
    parser.add_argument('--test-frac', type=float, default=0.2)
    parser.add_argument('--budget-seconds', type=float,
                        default=TOTAL_BUDGET_SECONDS)
    parser.add_argument('--monitor-interval', type=float,
                        default=MONITOR_INTERVAL_SECONDS)
    parser.add_argument('--project-root', default=PROJECT_ROOT)
    parser.add_argument('--workers', type=int, default=1,
                        help='并发 worker 数；本轮固定为 1（内存与正确性都要求串行）')
    parser.add_argument('--allow-overwrite', action='store_true')
    parser.add_argument('--skip-user-freeze', action='store_true',
                        help='复用 `--out` 里已冻结的 users.json（续跑用）')
    parser.add_argument('--model-dir', default=None,
                        help='从已落盘的模型目录读共现图，**不**在本脚本里现算。'
                             '用于"同协议同指标"在别的训练引擎产物上复现（如 '
                             'Spark 版 train_itemcf.py 的输出）。目录契约见 '
                             'experiments.itemcf.model_io。省略则现算。')
    args = parser.parse_args(argv)
    if args.max_k <= 0 or args.top_n <= 0:
        parser.error('--max-k and --top-n must be positive')
    if args.block_size < 1:
        parser.error('--block-size must be >= 1')
    if not (0.0 < args.test_frac < 1.0):
        parser.error('--test-frac must be in (0, 1)')
    if args.workers != 1:
        parser.error('--workers must be 1: the graph build and scoring are '
                     'memory-bound and must run serially')
    return args


def run(args):
    """本轮入口：冻结名单 → 切分 → 构图 → 精确命中 → 指标 → 产物。"""
    started = time.time()
    out = os.path.abspath(args.out)
    if os.path.exists(out) and not args.allow_overwrite:
        raise RuntimeError('output directory already exists, refusing to '
                           'overwrite: %s' % out)
    if not os.path.isdir(out):
        os.makedirs(out)
    logger = RunLogger(os.path.join(out, 'run.log'))
    log = logger
    log('holdout evaluation start: input=%s out=%s' % (args.input, out))
    log('frozen config: max_k=%d beta=%r top_n=%d positive_threshold=%r '
        'block_size=%d test_frac=%r'
        % (args.max_k, BETA, args.top_n, POSITIVE_THRESHOLD, args.block_size,
           args.test_frac))
    blas_env = set_blas_threads_to_one()
    log('BLAS thread env: %r' % (blas_env,))
    deadline = DeadlineRefresher(log=log, enabled=False)

    try:
        # --- 1. 冻结名单（必须在算任何指标之前落盘）----------------------
        users_path = os.path.join(out, 'users.json')
        if args.skip_user_freeze and os.path.isfile(users_path):
            users_payload = json.load(open(users_path, encoding='utf-8'))
            index = None
            log('reusing frozen user lists (%d eval users)'
                % users_payload['eval_size'])
        else:
            _slice, users_payload, index = freeze_user_lists(
                args.input, out, project_root=args.project_root,
                eval_size=args.eval_size, log=log, deadline=deadline)
        eval_users = set(int(u) for u in users_payload['eval_users'])

        # --- 2. 切分（**全部用户** → 全量训练段 + 评测用户的留出段）----
        # 关键：训练段必须是**全体用户**的（200,948 人），否则共现的分子与
        # 全库 n_like 分母不同口径；留出段只保留评测用户（其余丢弃，省盘）。
        session = RatingSession(args.input, out, args.chunk_rows,
                                args.test_frac, eval_users=eval_users,
                                keep_all_train=True)
        session.build(log=log)
        log('split counts: %r' % (session.counts,))

        # --- 3. 全量物品索引 + **训练段**的 n_all / n_like -----------------
        # 关键：n_like 必须与共现的分子**同口径**，即都来自**训练段**
        # （25.7M 行），不能用全量 32M 行的计数——那会把留出段的好评也算进
        # 分母，与分子不匹配（实测后果：独立核验器的抽样对拍系统性失败，
        # 见 .vmwork/holdout_eval_verified_contract.md §2.9）。
        if index is None:
            from experiments.itemcf.holdout_common import stream_input_pass1
            index = stream_input_pass1(args.input, POSITIVE_THRESHOLD, log=log)
        item_ids = np.asarray(index.item_ids, dtype=np.int64)
        item_index = dict((int(m), p) for p, m in enumerate(item_ids.tolist()))
        log('global index: items=%d positive_pairs(all rows)=%d'
            % (len(item_ids), index.positive_pairs()))
        n_all, n_like = counts_from_split(out, item_index, log=log)

        # --- 4. 两个矩阵：全量图矩阵 + 评测用户矩阵 ----------------------
        if deadline is not None:
            deadline.refresh(3600.0, 'graph matrix')
        A_graph, gstats = build_graph_matrix(out, item_index, log=log,
                                             deadline=deadline)
        A_eval, seen_pos, relevants_pos, mstats = build_eval_matrix(
            out, eval_users, item_index, log=log)
        # A_eval 的行序 = 评测用户在训练段里的首次出现顺序；产物里的 user_id 需要它。
        user_ids = _user_ids_from_matrix(out, eval_users)

        # --- 5. 主计算 ---------------------------------------------------
        metrics, bootstrap, per_user_rows, rec_rows, extra = run_evaluation(
            out, A_eval, A_graph, item_ids, seen_pos, relevants_pos, n_all,
            n_like, args.block_size, user_ids=user_ids, log=log,
            deadline=deadline, model_dir=args.model_dir)
        extra['graph_matrix'] = gstats
    except BaseException as error:  # noqa: BLE001 - 失败也要落盘原因
        log('FAILURE: %s: %s' % (type(error).__name__, error))
        write_json_atomic(os.path.join(out, 'run_status.json'), {
            'status': 'failed',
            'failure_reason': '%s: %s' % (type(error).__name__, error),
            'seconds': time.time() - started,
        })
        logger.close()
        raise

    log('writing artifacts ...')
    verdict = verdict_of(metrics, bootstrap)
    write_csv_atomic(os.path.join(out, 'per_user.csv'),
                     list(per_user_rows[0].keys()) if per_user_rows else
                     ['user_id'], per_user_rows)
    write_csv_atomic(os.path.join(out, 'recommendations.csv'),
                     ['user_id', 'method', 'rank', 'movie_id'], rec_rows)
    write_json_atomic(os.path.join(out, 'bootstrap.json'), bootstrap)
    summary_rows = []
    for name in METHODS:
        row = {'method': name}
        row.update(metrics[name])
        summary_rows.append(row)
    write_csv_atomic(os.path.join(out, 'summary_table.csv'),
                     list(summary_rows[0].keys()), summary_rows)
    source_hashes = {}
    for name in ('heldout_eval.py', 'holdout_common.py', 'rate_limited_guard.py'):
        path = os.path.join(MODULE_DIR, name)
        if os.path.isfile(path):
            source_hashes[name] = file_sha256(path)
    run_status = {
        'status': 'success',
        'run_id': os.path.basename(out),
        'mode': MODE_DEFAULT,
        'config': {'max_k': args.max_k, 'beta': BETA, 'top_n': args.top_n,
                   'positive_threshold': POSITIVE_THRESHOLD,
                   'block_size': args.block_size,
                   'test_frac': args.test_frac,
                   'eval_size': len(eval_users),
                   # None = 本脚本现算共现图；给绝对路径 = 用外部模型目录的图
                   # （"同协议同指标"跨引擎复现时，产物必须能自证用的是哪张图）。
                   'model_dir': (os.path.abspath(args.model_dir)
                                 if args.model_dir else None)},
        'methods': list(METHODS),
        'metrics': metrics,
        'bootstrap': bootstrap,
        'verdict': verdict,
        'split_counts': session.counts,
        'eval_stats': mstats,
        'extra': extra,
        'blas_env': blas_env,
        'source_hashes': source_hashes,
        'seconds': time.time() - started,
    }
    write_json_atomic(os.path.join(out, 'run_status.json'), run_status)
    write_json_atomic(os.path.join(out, 'verification.json'), build_self_check(
        metrics, bootstrap, per_user_rows, rec_rows, extra, eval_users))
    write_text(os.path.join(out, 'conclusion.md'),
               render_conclusion(metrics, bootstrap, verdict, run_status))
    logger.close()
    return run_status


def _user_ids_from_matrix(split_dir, eval_users):
    """按训练段的出现顺序还原"位置下标 → userId"，与 ``build_eval_matrix`` 一致。"""
    order = []
    seen = set()
    with open(os.path.join(split_dir, TRAIN_CSV), newline='') as handle:
        for record in csv.reader(handle):
            user = int(record[0])
            if user in eval_users and user not in seen:
                seen.add(user)
                order.append(user)
    return order


def build_self_check(metrics, bootstrap, per_user_rows, rec_rows, extra,
                     eval_users):
    """本模块**自查**（不是独立核验；独立核验由 ``verify_holdout_eval.py`` 做）。"""
    duplicates = 0
    per_user_seen = {}
    for row in rec_rows:
        key = (row['user_id'], row['method'])
        bucket = per_user_seen.setdefault(key, set())
        if row['movie_id'] in bucket:
            duplicates += 1
        bucket.add(row['movie_id'])
    return {
        'status': 'self_check_only',
        'note': ('this file is a self-check produced by the same module that '
                 'computed the metrics; it is NOT independent verification. '
                 'Independent verification is verify_holdout_eval.py.'),
        'duplicate_recommendations': duplicates,
        'evaluable_users': extra['evaluable_users'],
        'skipped_no_positive': extra['skipped_no_positive'],
        'support_nnz': extra['support_nnz'],
        'cooccurrence_edges': extra['cooccurrence']['edges'],
        'metrics': metrics,
        'bootstrap': bootstrap,
        'eval_users_requested': len(eval_users),
    }


def render_conclusion(metrics, bootstrap, verdict, run_status):
    """渲染 ``conclusion.md``（含主表、区间、判定与限制）。"""
    lines = []
    lines.append('# 全量规模留出评测结论')
    lines.append('')
    lines.append('run_id：**%s** ｜ 口径：全部用户的时间序 80/20 切分'
                 % run_status['run_id'])
    lines.append('')
    lines.append('## 1. 主表')
    lines.append('')
    lines.append('| method | NDCG@20 | Recall@20 | 有效用户数 | 满额率 | '
                 '目录覆盖率 |')
    lines.append('|---|---:|---:|---:|---:|---:|')
    for name in METHODS:
        m = metrics[name]
        lines.append('| %s | %.6f | %.6f | %d | %.4f | %.6f |'
                     % (name, m['ndcg@20'], m['recall@20'], m['users_scored'],
                        m['full_list_rate'], m['catalog_coverage']))
    lines.append('')
    lines.append('## 2. 配对差值区间（seed=%d、%d 次、95%% percentile）'
                 % (BOOTSTRAP_SEED, BOOTSTRAP_REPEATS))
    lines.append('')
    lines.append('| 对比 | 指标 | 绝对差 | 相对差 | 95% CI | 排除 0 |')
    lines.append('|---|---|---:|---:|---|---|')
    for baseline in ('MostPop', 'LikePop'):
        entry = bootstrap.get('ItemCF_vs_%s' % baseline, {})
        for key in ('ndcg@20', 'recall@20'):
            stat = entry.get(key) or {}
            ci = stat.get('ci95') or [0.0, 0.0]
            lines.append('| %s | %s | %+.6f | %+.2f%% | [%+.6f, %+.6f] | %s |'
                         % (baseline, key, stat.get('mean_difference', 0.0),
                            100.0 * (stat.get('relative_difference') or 0.0),
                            ci[0], ci[1],
                            '是' if stat.get('excludes_zero') else '否'))
    lines.append('')
    lines.append('## 3. 判定（按预先冻结的规则）')
    lines.append('')
    lines.append('`verdict = %s`' % verdict)
    lines.append('')
    lines.append('判定规则（协议 §5，执行前冻结）：NDCG 与 Recall 均超过两个基线，'
                 '且对**较强基线**的 NDCG 与 Recall 区间下界都 > 0。')
    lines.append('')
    lines.append('## 4. 证据分层（**不要混为一谈**）')
    lines.append('')
    lines.append('| 层 | 产物 | 强度 |')
    lines.append('|---|---|---|')
    lines.append('| 自查 | `verification.json`（`status=self_check_only`）| '
                 '弱：由**算指标的同一个模块**产出 |')
    lines.append('| **独立核验** | `verification_independent.json` | '
                 '**强**：由 `verify_holdout_eval.py` 从**原始评分重算**，'
                 '不 import 本模块 |')
    lines.append('')
    lines.append('**本文件（`conclusion.md`）只陈述指标与判定，不声称"已通过独立核验"。** '
                 '独立核验是否跑过、是否通过，以 `verification_independent.json` 的 '
                 '`pass` 字段为准。')
    lines.append('')
    lines.append('## 5. 限制')
    lines.append('')
    lines.append('- 这是"排除了已识别的历史实验用户之后的全量规模留出评测"，')
    lines.append('  **不是严格盲测**：完整 MovieLens 曾用于生产 ALS 训练。')
    lines.append('- 区间只反映**固定训练图下**的用户层面不确定性。')
    lines.append('- 与前两轮的绝对指标**不可直接比较**（人群、规模、名单同时变）。')
    lines.append('- 评测名单有 %d 人，其中 %d 人有效（其余测试段无正例，计零不剔除）；'
                 % (run_status.get('config', {}).get('eval_size', 0),
                    run_status.get('extra', {}).get('evaluable_users', 0)))
    lines.append('  **不是"全体用户都被评测"**。')
    lines.append('')
    return '\n'.join(lines)


def main(argv=None):
    args = parse_args(argv)
    try:
        status = run(args)
    except KeyboardInterrupt:
        sys.stderr.write('interrupted\n')
        return 130
    print(json.dumps({'status': status['status'],
                      'verdict': status['verdict'],
                      'metrics': status['metrics'],
                      'seconds': status['seconds']}, sort_keys=True,
                     default=_json_default))
    return 0


if __name__ == '__main__':
    sys.exit(main())


__all__ = [
    'MODE_DEFAULT', 'METHODS', 'EVAL_SALT', 'EVAL_SIZE',
    'POSITIVE_THRESHOLD', 'MAX_K', 'BETA', 'TOP_N',
    'ResourceError', 'CorrectnessError', 'RatingSession',
    'build_cooccurrence', 'score_support', 'score_support_by_block',
    'support_set', 'filter_hits_by_mask', 'exclude_seen_from_hits',
    'build_candidate_mask', 'locate_in_rows', 'per_user_metrics',
    'paired_bootstrap', 'verdict_of', 'freeze_eval_users',
    'load_prior_exclusions', 'select_top_by_hash', 'user_list_sha256',
    'user_key', 'set_blas_threads_to_one', 'RunLogger', 'DeadlineRefresher',
    'freeze_user_lists', 'build_eval_matrix', 'run_evaluation',
    'per_user_recommendations', 'top_n_popularity', 'popularity_orders',
    'write_user_list_file', 'parse_args', 'run', 'main', 'build_self_check',
    'render_conclusion',
]
