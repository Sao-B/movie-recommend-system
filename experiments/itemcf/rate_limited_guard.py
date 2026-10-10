# -*- coding: utf-8 -*-
"""带留出评测专用监督：**保留全部硬性终止线**，把墙钟预算改成"可显式刷新"。

为什么不是改 ``resource_guard.py``
==================================

``resource_guard`` 已被 533 个用例与上一轮全量 run 依赖（``full_train.py``
对它做模块级硬 import，``source_hashes_of_modules()`` 还把它的哈希写进模型
指纹）。**动它 = 破坏可复现性**。所以本模块只做"在它之上组合/子类化"：

* 采样、读 ``/proc``、进程树 RSS、磁盘读数、终止进程组、CSV 字段、
  状态取值 —— 全部**直接复用** ``resource_guard`` 的实现，不复制粘贴；
* 只替换两处**语义**：阶段 deadline 的可刷新性、以及"不可测量"的判定口径。

本模块不 import ``resource_guard`` 之外的项目模块（零额外依赖）。

上一轮真正的失败模式（本次要修的）
==================================

1. **阶段墙钟预算把长任务误杀。** ``resource_guard`` 的阶段超时是
   ``now - stage_started_at >= budget``，而阶段切换只能靠 ``set_stage()``
   —— 它**顺带把该阶段计时清零**。全量留出评测的流程是"按列块循环构图，
   中间穿插打分"，阶段边界不是固定的四段：一个列块 4~7 分钟、几百个块，
   任何"单阶段预算"都会在中途炸掉。协议要求的是**总量 8 小时**上限，
   不是"每块 4~7 分钟就必须有进展"。

2. **``MAX_UNMEASURED_SECONDS = 60.0`` 会误杀。** 它的原意是"监督连续 60 秒
   采不到有效读数就判 ``monitor_failed`` 并杀掉 worker，宁可停下也不放过"。
   但在 worker 经历一段**已知的、可预期的**不可测量窗口时（例如 mmap 大数组
   时 RSS 读数抖动、进程正处于 fork/exec 过渡、或一次长 GC），这条会把正常
   工作判成监督失败。

修法：**显式心跳（explicit heartbeat）**。调用方在进入那段窗口前**主动声明**
"我接下来会有 T 秒不可测量"，监督在这段声明的时间窗内**不判** ``monitor_failed``，
超出声明窗（含宽限）才判。默认行为**与现状一致**：不声明就按原始的 60 秒口径，
**宁可停下也不放过**。

四条终止线为什么在这里仍然真正生效
==================================

==============================  ============================================
终止线                           本模块的处理
==============================  ============================================
进程树 RSS > 3 GiB               **原样继承** ``_evaluate`` 的判定，且在
                                 "无阶段预算"模式下它是**唯一的** RSS 出口，
                                 不受任何 deadline 刷新影响
MemAvailable 连续 30 s < 256 MB  **原样继承**，含"恢复即清零重计时"语义
剩余磁盘 < 2 GiB                 **原样继承**
阶段 / 总预算超时                改为**可显式刷新**；总量预算仍是硬上限
==============================  ============================================

关键点：**刷新 deadline 只影响墙钟那一行，不影响前三条。** 前三条是
"机器扛不住"的硬性证据，与"任务做了多久"无关，所以调用方刷新多少次都不会
削弱它们。本模块还把这三条的门禁顺序保持为 RSS → 内存 → 磁盘 → 墙钟，
即**资源超限永远先于超时被报出**，不会被一次刷新掩盖。

状态口径完全沿用 ``resource_guard``（不得自造）
==============================================

``running`` / ``success`` / ``resource_blocked`` / ``resource_failed`` /
``correctness_failed`` / ``monitor_failed``，映射规则也一致：

* RSS / MemAvailable / 磁盘 → ``resource_blocked``（机器不够，硬性资源不足）；
* 阶段 / 总预算超时 → ``resource_failed``（资源也许够，但没在预算内跑完）；
* 监督自身失败 / 超出声明窗仍采不到数 → ``monitor_failed``。

两种预算模式
============

``budget_mode``：

* ``'stage'``（默认）—— **与 ``resource_guard`` 逐字节同语义**。阶段超时按
  ``stage_started_at`` 起算，``stage_budget_seconds`` 到期即报
  ``resource_failed``。既有的四段流程与既有测试口径不受影响。
* ``'none'``（本轮全量评测用）—— **阶段墙钟不参与门禁**。阶段名仍然记录进
  ``resources.csv``（可读性），但不再有"单阶段预算"这条路。若设置了
  ``total_budget_seconds``，它就是**唯一**的墙钟上限（真正的 8 小时上限）。

``RefreshableGuard`` 在**两种模式下**都提供 ``refresh_deadline()``：
它把墙钟计时的基准推到"现在"，于是总预算是一段**滑动窗口**式的上限
（"距上次刷新不超过 N 秒"）。对本轮来说这正是想要的：每完成一个列块刷新一次，
"总预算"就表达成"**两个块之间**不允许超过 N 秒"，而总量上限由
``budget_mode='none'`` + 不刷新（或用 ``set_total_budget`` 收紧）来兜底。

``RefreshableGuardedRun`` 在 worker 与监督之间开了一个**文件心跳通道**：
worker（或它的编排层）往某个文件写一行，父进程侧监督线程按采样周期读它，
读到变化就自动刷新 deadline 并续心跳。这样"每块刷新一次"不需要 worker
自己 import 监控代码——**监督仍然独立于 worker**，是 ``resource_guard``
最核心的契约，不能退化成"worker 主动回调监督"。

默认保守
========

每个可调的放松项，默认值都是**最保守**的那一端：

* ``budget_mode`` 默认 ``'stage'``（旧语义）；
* ``refresh_deadline()`` 默认**不被调用**（不刷新就按旧语义超时）；
* ``declare_unmeasured(T)`` 默认**不被调用**（不声明就按原始 60 秒判
  ``monitor_failed``）；
* ``heartbeat_*`` 默认全 ``None``（没有心跳通道 = 没有放松）。

Python 3.6 兼容：不用 f-string、dataclasses、walrus、
``from __future__ import annotations``、dict 合并 ``|``、
``subprocess.run(capture_output=)``。只用 ``%`` / ``str.format``。
"""
import os
import sys
import time

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 直接复用上一轮已经跑通的实现——**这是本模块存在的理由**，不是复制粘贴。
# 采样（read_meminfo / read_rss_map）、进程树（process_tree_pids /
# process_tree_rss_bytes）、磁盘（read_disk_free_bytes）、终止
# （terminate_process_group）、CSV 字段（CSV_FIELDS）、状态常量
# （STATUS_*）与原因常量（REASON_*）全部来自这里。
from experiments.itemcf.resource_guard import (  # noqa: E402
    CSV_FIELDS,
    DISK_FLOOR_BYTES,
    MAX_UNMEASURED_SECONDS,
    MEM_AVAILABLE_FLOOR_BYTES,
    MEM_AVAILABLE_STREAK_SECONDS,
    REASON_DISK,
    REASON_MEM,
    REASON_MONITOR_FAILED,
    REASON_RSS,
    REASON_STAGE_TIMEOUT,
    RESOURCE_POLL_SECONDS,
    RSS_LIMIT_BYTES,
    STAGES,
    STATUS_CORRECTNESS_FAILED,
    STATUS_MONITOR_FAILED,
    STATUS_RESOURCE_BLOCKED,
    STATUS_RESOURCE_FAILED,
    STATUS_RUNNING,
    STATUS_SUCCESS,
    TERM_GRACE_SECONDS,
    GuardedRun,
    ResourceGuard,
    process_tree_pids,
    process_tree_rss_bytes,
    read_disk_free_bytes,
    read_meminfo,
    terminate_process_group,
)

__all__ = [
    # 复用自 resource_guard 的读数 / 进程树 / 终止 / 产物契约
    'read_meminfo', 'process_tree_pids', 'process_tree_rss_bytes',
    'read_disk_free_bytes', 'terminate_process_group', 'CSV_FIELDS',
    # 默认阈值与采样契约
    'RSS_LIMIT_BYTES', 'MEM_AVAILABLE_FLOOR_BYTES',
    'MEM_AVAILABLE_STREAK_SECONDS', 'DISK_FLOOR_BYTES',
    'RESOURCE_POLL_SECONDS', 'TERM_GRACE_SECONDS', 'STAGES',
    'MAX_UNMEASURED_SECONDS',
    # 状态与原因常量（与 resource_guard 同一套取值）
    'STATUS_RUNNING', 'STATUS_SUCCESS', 'STATUS_RESOURCE_BLOCKED',
    'STATUS_RESOURCE_FAILED', 'STATUS_MONITOR_FAILED',
    'STATUS_CORRECTNESS_FAILED',
    'REASON_RSS', 'REASON_MEM', 'REASON_DISK', 'REASON_STAGE_TIMEOUT',
    'REASON_MONITOR_FAILED',
    # 本模块新增
    'BUDGET_MODE_STAGE', 'BUDGET_MODE_NONE', 'BUDGET_MODES',
    'HEARTBEAT_MAX_SILENCE_FACTOR', 'WATCHER_INTERVAL_SECONDS',
    'RefreshableGuard', 'RefreshableGuardedRun', 'FileHeartbeat',
    'HeartbeatWriter', 'parse_heartbeat_stamp', 'write_heartbeat_file',
    'refresh_from_heartbeat',
]

# ---------------------------------------------------------------------------
# 预算模式
# ---------------------------------------------------------------------------

#: 与 ``resource_guard`` 逐字节同语义：阶段墙钟到期即 ``resource_failed``。
BUDGET_MODE_STAGE = 'stage'

#: 阶段墙钟**不参与门禁**；总量预算（若设置）是唯一墙钟上限。
BUDGET_MODE_NONE = 'none'

BUDGET_MODES = (BUDGET_MODE_STAGE, BUDGET_MODE_NONE)

#: 监督线程检查心跳文件的周期（秒）。只读一个小文件，代价可忽略。
WATCHER_INTERVAL_SECONDS = 1.0

#: ``uncertain_seconds`` 的宽松系数。声明的"一段不可测量时间"本身也是估计值，
#: 硬卡在声明长度上会把"估短了 1 秒"判成监督失败。所以容忍
#: ``T * 该系数`` 的窗口（默认 2 倍）。**这是唯一的宽松量**，且只作用于
#: 显式声明过的窗口；未声明时 `_monitor_failure` 走原始 60 秒口径，分毫不松。
HEARTBEAT_MAX_SILENCE_FACTOR = 2.0


def _as_float(value, default=None):
    """``resource_guard._to_float`` 的本地等价物（不 import 私有名）。"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value, default=None):
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# 显式心跳：文件通道
# ---------------------------------------------------------------------------

def write_heartbeat_file(path, note='', now=None):
    """写一行心跳时间戳（原子替换），返回写入的 ``float`` 时间戳。

    这是**worker 侧**唯一需要知道的接口：往文件里写一个递增的数字。
    之所以用文件而不是队列/信号：worker 与监督可能是不同进程、不同用户，
    文件是两者都能碰的最小公分母，且**监督侧读不到文件也不会崩**。

    写入用 ``<path>.tmp`` + ``os.replace``，避免监督读到半行。
    """
    stamped = float(time.time() if now is None else now)
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = path + '.tmp'
    with open(tmp, 'w') as handle:
        handle.write('%.6f\t%s\n' % (stamped, note))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return stamped


def parse_heartbeat_stamp(text):
    """从心跳文件内容解析时间戳；解析不出来返回 ``None``（不抛异常）。

    ``None`` 与 ``0.0`` 必须区分开：前者是"还没写过/写坏了"，
    后者是一个合法的（很旧的）时间戳。把坏内容当成 0 会让监督误判成
    "心跳极旧"从而反复触发；当成 ``None`` 则走"从未收到心跳"的保守路径。
    """
    if text is None:
        return None
    line = str(text).strip()
    if not line:
        return None
    head = line.split('\t', 1)[0].split()[0] if line.split() else ''
    if not head:
        return None
    try:
        return float(head)
    except (TypeError, ValueError):
        return None


def refresh_from_heartbeat(guard, path):
    """读一次心跳文件，变化则刷新 ``guard`` 的 deadline。

    返回 ``{'stamp': float_or_None, 'changed': bool, 'interpreted_as_refresh':
    bool, 'age_s': float_or_None}``。**这个函数不抛异常**：心跳文件缺失/
    损坏只意味着"没有新的心跳"，绝不意味着监督失败——监督失败由
    ``_monitor_failure`` 按显式声明的窗口判定。
    """
    outcome = {'stamp': None, 'changed': False,
               'interpreted_as_refresh': False, 'age_s': None}
    if guard is None or not path:
        return outcome
    try:
        with open(path) as handle:
            text = handle.read()
    except (IOError, OSError):
        return outcome
    stamp = parse_heartbeat_stamp(text)
    if stamp is None:
        return outcome
    now = guard._clock()
    outcome['stamp'] = stamp
    # 心跳时间戳来自 ``time.time()``，而 guard 的时钟可能是**假时钟**
    # （测试里是 FakeClock，从 1000.0 起算）。只有当心跳戳确实比
    # "本 guard 见过的时间"更大时才算刷新，否则假时钟场景下会被
    # 一次陈旧的心跳反复刷新成未来时间。
    last = getattr(guard, '_last_heartbeat_stamp', None)
    if last is not None and stamp <= last:
        outcome['age_s'] = round(now - guard.started_at, 2)
        return outcome
    guard._last_heartbeat_stamp = stamp
    guard.refresh_deadline(reason='heartbeat')
    outcome['changed'] = True
    outcome['interpreted_as_refresh'] = True
    age = _as_float(getattr(guard, '_last_refresh_at', None))
    outcome['age_s'] = round(now - age, 2) if age is not None else None
    return outcome


class HeartbeatWriter(object):
    """worker 侧的续心跳助手：``beat()`` 一次等于"我还活着"。

    ::

        beat = HeartbeatWriter(os.path.join(out_dir, 'guard_heartbeat.txt'))
        for block in blocks:
            build_block(block)
            beat.beat('block=%d' % block_index)   # ← 每个列块一次

    ``beat()`` 失败（磁盘满、权限）只吞掉并记在 ``self.errors`` 里，
    **绝不因为心跳写不出去就把 worker 打死**——worker 的失败应该由它自己的
    业务逻辑决定，监督侧会按 60 秒/声明窗口的保守口径处理。
    """

    def __init__(self, path, log=None):
        if not path:
            raise ValueError('heartbeat path must not be empty')
        self.path = path
        self.log = log if log is not None else (lambda _m: None)
        self.count = 0
        self.errors = []
        self.last_stamp = None

    def beat(self, note='', now=None):
        """写一次心跳；返回时间戳或 ``None``（写失败）。不抛异常。"""
        try:
            self.last_stamp = write_heartbeat_file(self.path, note, now=now)
            self.count += 1
            return self.last_stamp
        except (IOError, OSError, ValueError) as error:
            self.errors.append(str(error))
            self.log('WARN heartbeat write failed: %s' % error)
            return None


class FileHeartbeat(object):
    """监督侧的**心跳观察者**：把文件变化翻译成"刷新 deadline"。

    与 :class:`HeartbeatWriter` 配对。两者可以跨进程、跨用户，只共享一个文件。

    :param str path: 心跳文件路径。
    :param float interval: 轮询周期，默认 1.0 s。
    :param float timeout_seconds: ``set_timeout()`` 后自动开启的看门狗窗口：
        超过这么久没有**新**心跳，就把 ``guard`` 的 ``violation`` 置为
        ``monitor_failed`` 并停止监督循环。``None``（默认）＝ 不设看门狗，
        即"心跳只用来刷新，不主动判死"（此时判死仍由
        ``declare_unmeasured`` 的显式窗口负责）。

    **为什么要有 timeout_seconds**：刷新本身会掩盖"任务卡死"。
    每块刷新一次的语义是"两块之间不超过 N 秒"，如果没有看门狗，
    一个卡在第一个块里的 worker 会永远不被墙钟杀死（RSS/内存/磁盘三条仍是
    硬线，但"没算完"这件事没人报）。所以全量评测必须同时配
    ``timeout_seconds = 单块墙钟上限``：块内正常跑 → 有看门狗；
    块间刷新 → 看门狗重置。
    """

    def __init__(self, path, interval=WATCHER_INTERVAL_SECONDS,
                 timeout_seconds=None, log=None):
        self.path = path
        self.interval = float(interval)
        self.timeout_seconds = _as_float(timeout_seconds)
        self.log = log if log is not None else (lambda _m: None)
        self.last_stamp = None
        self.last_change_at = None
        self.refreshes = 0
        self.errors = []

    def set_timeout(self, timeout_seconds):
        """设置/清除看门狗窗口（秒）；``None`` 表示不判死。"""
        self.timeout_seconds = _as_float(timeout_seconds)
        return self.timeout_seconds

    def poll(self, guard):
        """读一次心跳并按需刷新 ``guard``；返回 ``refresh_from_heartbeat`` 的结果。

        若 ``timeout_seconds`` 已设置且自上次**变化**起的静默超过它，
        返回的 dict 里 ``'expired'=True``，并给出 ``'silence_s'``。
        **不在这里下手**——置 violation / 停循环由 :meth:`supervise` 统一做，
        这样单独调用 :meth:`poll` 的调用方不会被暗地里停掉。
        """
        now = guard._clock() if guard is not None else time.time()
        outcome = refresh_from_heartbeat(guard, self.path)
        # ``refresh_from_heartbeat`` 的返回契约里**没有** ``errors``；
        # 本方法自己开一个列表再往外报（``supervise`` 读它）。
        outcome['errors'] = []
        outcome['silence_s'] = None
        outcome['expired'] = False
        if outcome['changed']:
            if self.last_stamp is not None and outcome['stamp'] is not None:
                if outcome['stamp'] > self.last_stamp:
                    self.last_change_at = now
                    self.refreshes += 1
            else:
                self.last_change_at = now
                self.refreshes += 1
            self.last_stamp = outcome['stamp']
        if self.timeout_seconds is not None:
            reference = self.last_change_at
            if reference is None:
                reference = getattr(guard, 'started_at', now) if guard else now
            silence = now - reference
            outcome['silence_s'] = round(silence, 2)
            if silence >= self.timeout_seconds:
                outcome['expired'] = True
                outcome['errors'].append(
                    'no heartbeat for %.1fs (>= %.0fs watchdog)'
                    % (silence, self.timeout_seconds))
        return outcome

    def supervise(self, guard, stop_event, standalone=False,
                  watcher_interval=None, poll_seconds=None):
        """轮询循环：有变化就刷新，静默超窗就判 ``monitor_failed`` 并停止。

        :param float watcher_interval: 检查心跳的周期（默认 1.0 s）。
        :param float poll_seconds: 顺手也跑 guard 自己的采样循环的周期；
            为 ``None``（默认）时**只做心跳观察**，采样循环由调用方另行驱动
            （``RefreshableGuardedRun`` 就是"心跳观察 + guard 自带采样线程"
            两条并行）。传了值则本方法自己也调 ``guard._tick()``。

        返回终态摘要 dict。
        """
        interval = float(watcher_interval or WATCHER_INTERVAL_SECONDS)
        while not stop_event.is_set():
            started = time.time()
            if poll_seconds is not None:
                try:
                    if not guard._tick():
                        break
                except Exception as error:  # noqa: BLE001 - 监督不能崩在这里
                    guard.monitor_error = '%s: %s' % (type(error).__name__, error)
                    self.log('WARN guard tick failed: %s' % guard.monitor_error)
            outcome = self.poll(guard)
            if outcome['expired'] and guard.violation is None:
                reason = ('%s: %s' % (REASON_MONITOR_FAILED,
                                      outcome['errors'][-1]))
                guard._on_violation(reason)
                break
            if guard.violation is not None:
                break
            elapsed = time.time() - started
            wait = interval - elapsed
            if wait > 0:
                stop_event.wait(wait)
        if standalone:
            guard._write_status()
        return guard.summary()


# ---------------------------------------------------------------------------
# 可刷新 deadline 的监督
# ---------------------------------------------------------------------------

class RefreshableGuard(ResourceGuard):
    """``ResourceGuard`` + 可显式刷新的墙钟 deadline + 显式心跳窗口。

    继承而不重写：**采样、进程树 RSS、内存连续时长、磁盘下限、CSV/状态落盘、
    终止进程组全部沿用父类**。本类只覆写两处判定语义
    （:meth:`_evaluate` 的墙钟那一行、:meth:`_monitor_failure`），
    并在 ``summary()`` 里补上本类新增的可观测字段。

    :param str budget_mode: ``'stage'``（默认，旧语义）或 ``'none'``
        （阶段墙钟不参与门禁）。
    :param float stage_budget_seconds: 便捷写法，等价于给 ``STAGES`` 里每个
        阶段设同一份预算（需显式指定阶段名时仍用 ``stage_budgets``）。
    :param float uncertain_seconds: **显式声明**的"接下来会有 T 秒不可测量"。
        它同时做两件事：① 刷新墙钟 deadline（避免把可预期的不可测量窗口
        算成"任务卡住"）；② 把这一轮 ``monitor_failed`` 的窗口从默认 60 s
        放宽到 ``T * HEARTBEAT_MAX_SILENCE_FACTOR``。默认 ``None`` ＝ 未声明
        ＝ **与现状完全一致**（60 s，宁可停下也不放过）。
    :param str reason_note: 刷新原因标签，会写入 ``refresh_log``，便于事后
        回答"到底是哪次刷新让这轮活下来的"。

    其余参数与 ``ResourceGuard`` 同名同义（``rss_limit_bytes``、
    ``mem_available_floor_bytes``、``mem_floor_streak_seconds``、
    ``disk_floor_bytes``、``grace_seconds``、``max_unmeasured_seconds``、
    ``rss_reader`` / ``mem_reader`` / ``disk_reader`` / ``clock`` …），
    全部透传，**阈值默认值也一致**，所以"沿用已修好的口径"是字面意义的。
    """

    def __init__(self, root_pid, budget_mode=BUDGET_MODE_STAGE,
                 stage_budget_seconds=None, uncertain_seconds=None,
                 heartbeat_timeout_seconds=None, rss_limit_bytes=RSS_LIMIT_BYTES,
                 mem_available_floor_bytes=MEM_AVAILABLE_FLOOR_BYTES,
                 mem_floor_streak_seconds=MEM_AVAILABLE_STREAK_SECONDS,
                 disk_floor_bytes=DISK_FLOOR_BYTES, **kwargs):
        if budget_mode not in BUDGET_MODES:
            raise ValueError('unknown budget_mode %r (expected one of %s)'
                             % (budget_mode, ', '.join(BUDGET_MODES)))
        # 硬性终止线的阈值在这里显式落到父类上，而不是靠"忘了传就等于默认"
        # ——本模块的契约是"这三条必须真正生效"，值得在代码里写死。
        super(RefreshableGuard, self).__init__(
            root_pid,
            rss_limit_bytes=rss_limit_bytes,
            mem_available_floor_bytes=mem_available_floor_bytes,
            mem_floor_streak_seconds=mem_floor_streak_seconds,
            disk_floor_bytes=disk_floor_bytes,
            **kwargs)
        self.budget_mode = budget_mode
        if stage_budget_seconds is not None:
            for _stage in STAGES:
                self.stage_budgets[_stage] = float(stage_budget_seconds)
        # 墙钟基准。与父类的 ``stage_started_at`` 分开记：
        # 父类那个是"本阶段从何时起算"，这个是"墙钟门禁从何时起算"，
        # 显式刷新只推后者，前者的可观测语义（CSV 里的 stage_elapsed_s）不变。
        self.deadline_started_at = self._clock()
        self.deadline_budget_seconds = _as_float(self.total_budget_seconds)
        self.refresh_count = 0
        self.refresh_log = []
        self.uncertain_seconds = _as_float(uncertain_seconds)
        self.uncertain_declared_at = None
        self.heartbeat_timeout_seconds = _as_float(heartbeat_timeout_seconds)
        self._last_heartbeat_stamp = None
        #: 最近一次刷新时间戳（``refresh_from_heartbeat`` 用它报 ``age_s``）。
        self._last_refresh_at = None

    # -- 刷新 -------------------------------------------------------------

    def refresh_deadline(self, reason=None, extend_budget_seconds=None):
        """把墙钟计时基准推到**现在**：一个"我还在按计划推进"的显式声明。

        三种用法，语义各不相同，都写进 ``refresh_log`` 以免事后混淆：

        1. **推进常规阶段**（``reason='block_done'``）：按默认的
           ``budget_mode`` 走。``'stage'`` 模式下，这次刷新让当前阶段的墙钟
           重新计时（等价于父类的 ``set_stage`` 但**不改阶段名、不动
           ``stage_started_at`` 的 CSV 语义**）。
        2. **完成一个列块**（``refresh_deadline('block=%d' % i)``）：
           同上。全量评测里每块调一次，于是"单阶段预算"不再误杀长任务。
        3. **声明一段不可测量窗口**（``declare_unmeasured(T)`` 内部调用）：
           把 deadline 推到"现在 + T"，因为调用方**明确知道**自己接下来 T 秒
           不产出可测量进展。这不是"放宽总量"，而是"把已知的空窗如实登记"。

        :param str reason: 刷新原因（进 ``refresh_log``，也进 ``summary``）。
        :param float extend_budget_seconds: 只用于第 3 种用法；把它折进
            ``deadline_budget_seconds``，使"距基准不超过 N 秒"这条仍然成立。
        :return: 刷新后的 ``{'count', 'at', 'reason', 'budget_seconds'}``。
        """
        now = self._clock()
        self.deadline_started_at = now
        self._last_refresh_at = now
        self.refresh_count += 1
        budget = _as_float(extend_budget_seconds)
        if budget is not None:
            self.deadline_budget_seconds = budget
        entry = {'count': self.refresh_count, 'at': now,
                 'reason': reason or 'manual',
                 'budget_seconds': self.deadline_budget_seconds}
        self.refresh_log.append(entry)
        self.log('guard: deadline refreshed (#%d, reason=%s, '
                 'budget=%s)' % (self.refresh_count, entry['reason'],
                                 entry['budget_seconds']))
        return entry

    def declare_unmeasured(self, seconds, reason=None):
        """**显式心跳**：声明"接下来 T 秒不可测量"，在这段窗口内不判失败。

        这正是对 ``MAX_UNMEASURED_SECONDS = 60.0`` 误杀问题的修法：

        * **未声明** → 走父类 ``_monitor_failure`` 的原始口径（默认 60 秒），
          宁可停下也不放过；
        * **声明了 T** → 窗口是 ``T * HEARTBEAT_MAX_SILENCE_FACTOR``，
          超时照样判 ``monitor_failed``（**不是无限豁免**）；
        * 声明同时刷新墙钟 deadline：一段**已知**的空窗不该被算成"任务卡住"。

        实现上有两个**必须的**细节，写在这里以免被后来的改动破坏：

        1. ``uncertain_declared_at`` 在 ``refresh_deadline()`` **之后**才赋值。
          （``refresh_deadline`` 不再清它，但顺序保持"先推基准、后起窗口"更直观。）
        2. **不把 T 折进 ``deadline_budget_seconds``。** 早先的写法用
          ``extend_budget_seconds=seconds``，那会让总预算变成 T 本身——于是
          "声明 120 秒不可测量"的下一采样点（已过 120 秒）就被自己的总预算
          杀掉，声明等于自毁。``deadline_budget_seconds`` 现在**只由**
          ``set_total_budget`` / 构造参数决定；T 只影响不可测量窗口。

        :param float seconds: 预计的不可测量时长（必须 > 0）。
        :param str reason: 记入 ``refresh_log`` 的原因。
        """
        value = _as_float(seconds)
        if value is None or value <= 0:
            raise ValueError('declare_unmeasured needs a positive seconds, '
                             'got %r' % (seconds,))
        self.refresh_deadline(reason=reason or 'declare_unmeasured')
        self.uncertain_seconds = value
        self.uncertain_declared_at = self._clock()
        self.log('guard: declared %.1fs unmeasured window (window=%.1fs)'
                 % (value, value * HEARTBEAT_MAX_SILENCE_FACTOR))
        return value

    def clear_unmeasured(self):
        """撤销未过期的不可测量声明（下一段恢复常规口径）。"""
        self.uncertain_seconds = None
        self.uncertain_declared_at = None
        return None

    def unmeasured_window_seconds(self, now=None):
        """当前生效的"多久没读数才算监督失败"窗口（秒）。

        返回 ``(window_seconds, source)``：``source`` 取 ``'default'`` /
        ``'declared'`` / ``'watchdog'``，写进 ``summary``，
        这样事后能一眼看出"这次没被杀，是因为谁把窗口放宽了"。

        **``'declared'`` 优先于数值大小。** 声明的窗口若**小于**默认 60 s，
        取 max 之后数值仍是 60，但来源必须报 ``'declared'``——否则调用方
        会以为"声明没生效"，而实际上它是生效了的、只是比默认更严。
        """
        moment = self._clock() if now is None else now
        window = float(self.max_unmeasured_seconds)
        source = 'default'
        if self.uncertain_seconds and self.uncertain_declared_at is not None:
            declared = self.uncertain_seconds * HEARTBEAT_MAX_SILENCE_FACTOR
            # 声明窗只在自己还没过期时有效：`T*系数` 过去之后必须回落默认值，
            # 否则一次 `declare_unmeasured(60)` 会永久放宽这条线。
            if moment - self.uncertain_declared_at <= declared:
                if declared >= window:
                    window, source = declared, 'declared'
                else:
                    source = 'declared'
        if self.heartbeat_timeout_seconds is not None:
            if self.heartbeat_timeout_seconds > window:
                window = float(self.heartbeat_timeout_seconds)
                source = 'watchdog'
        return window, source

    # -- 门禁 -------------------------------------------------------------

    def _evaluate(self, row, now):
        """四条终止线，顺序：RSS → 内存 → 磁盘 → 墙钟。

        **资源三条一律调父类原实现**（含内存恢复清零重计时、磁盘严格下限、
        RSS 严格 ``>``），保证"上一轮花大力气修好的不能被这次扩展弄退化"。
        只有墙钟那一行是本类自己的。
        """
        reason = self._resource_reason(row, now)
        if reason is not None:
            return reason
        return self._clock_reason(now)

    def _resource_reason(self, row, now):
        """把父类 ``_evaluate`` 的资源三条摘出来单独调。

        为什么要"摘出来"而不是整段抄：调用父类时必须把墙钟那两条**中和掉**，
        否则父类会先报阶段超时、把本类"阶段墙钟不参与门禁"的语义盖掉。
        做法是临时把 ``stage_budgets`` / ``total_budget_seconds`` 换成空，
        调完立刻还原（本函数在监督线程里被调用，guard 的门禁判定是单线程的，
        不存在与 ``set_stage`` 并发改写的问题）。
        """
        saved_budgets = self.stage_budgets
        saved_total = self.total_budget_seconds
        self.stage_budgets = {}
        self.total_budget_seconds = None
        try:
            return ResourceGuard._evaluate(self, row, now)
        finally:
            self.stage_budgets = saved_budgets
            self.total_budget_seconds = saved_total

    def _clock_reason(self, now):
        """墙钟门禁：**只有它能被 ``refresh_deadline`` 推动**。

        * ``budget_mode='none'`` → 不看阶段预算，只看总预算（若设置）。
        * ``budget_mode='stage'`` → 先看**当前阶段**的预算（基准
          ``deadline_started_at``，与父类的 ``stage_started_at`` 同初值、
          刷新后分离），再看总预算。
        """
        if self.budget_mode == BUDGET_MODE_STAGE:
            budget = self.stage_budgets.get(self.stage)
            if budget is not None and now - self.deadline_started_at >= budget:
                return ('%s: stage %r ran %.1fs >= %.0fs budget '
                        '(since last deadline refresh #%d)'
                        % (REASON_STAGE_TIMEOUT, self.stage,
                           now - self.deadline_started_at, budget,
                           self.refresh_count))
        total = self.deadline_budget_seconds
        if total is None:
            total = _as_float(self.total_budget_seconds)
        if total is not None and now - self.deadline_started_at >= total:
            return ('%s: run %.1fs >= %.0fs total budget '
                    '(since last deadline refresh #%d)'
                    % (REASON_STAGE_TIMEOUT, now - self.deadline_started_at,
                       total, self.refresh_count))
        return None

    def _monitor_failure(self, now):
        """监督失败判定：显式声明过的不可测量窗口内**不判**，超出才判。

        与父类的**唯一**差别是"窗口有多长"：窗口来自
        :meth:`unmeasured_window_seconds`（受显式声明影响），而父类固定用
        ``max_unmeasured_seconds``。判定结构（先看自身异常、再看有没有任何有效
        读数、最后看距上次有效读数多久）**逐条保持**——那套结构是上一轮修出来
        的、且有回归用例钉住，不能顺手改。

        特别地，"当前是否采不到读数"仍然看 ``_last_row``：父类 ``_tick`` 在
        采不到 RSS 时**照样**把那一行写进 ``_last_row``（字段为 ``None``），
        所以这个判断在"先成功、后持续失败"的序列上依然成立。这里曾一度想
        "修正"它，实测证明父类行为正确，故保持原样。
        """
        if self.monitor_error is not None and self._last_row is None:
            return '%s: supervisor could not sample at all (%s)' % (
                REASON_MONITOR_FAILED, self.monitor_error)
        window, source = self.unmeasured_window_seconds(now)
        reference = self.last_valid_sample_at
        if reference is None:
            reference = self.first_sample_at
        if reference is None:
            if now - self.started_at >= window:
                return ('%s: no sample obtained within %.0fs of start '
                        '(window=%s)' % (REASON_MONITOR_FAILED, window, source))
            return None
        if self._last_row is None or self._last_row.get('rss_bytes') is None:
            if now - reference >= window:
                return ('%s: no valid process-tree sample for %.0fs '
                        '(window=%.0fs, source=%s)'
                        % (REASON_MONITOR_FAILED, now - reference, window,
                           source))
        return None

    # -- 阶段 -------------------------------------------------------------

    def set_stage(self, stage):
        """换阶段：沿用父类语义（`stage_started_at` 清零），**并同时刷新墙钟**。

        父类的 ``set_stage`` 顺带重置了阶段计时；本类多推一下 deadline 基准，
        否则 ``budget_mode='stage'`` 下会出现"刚换完阶段就立刻用旧基准判超时"
        的错位。阶段名与 ``stage_started_at`` 的行为**不变**，所以
        ``resources.csv`` 的 ``stage`` / ``stage_elapsed_s`` 两列口径不动。
        """
        stage = ResourceGuard.set_stage(self, stage)
        self.refresh_deadline(reason='set_stage:%s' % stage)
        return stage

    def set_total_budget(self, seconds):
        """在运行中设置/收紧总量墙钟上限（秒）；``None`` 表示取消。"""
        self.total_budget_seconds = _as_float(seconds)
        self.deadline_budget_seconds = self.total_budget_seconds
        return self.deadline_budget_seconds

    # -- 统计 -------------------------------------------------------------

    def summary(self):
        """父类摘要 + 本类新增的可观测字段。

        新增字段都是**为了回答"这次为什么活下来了/为什么被杀"**：
        ``budget_mode``、``refresh_count``、``last_refresh``、
        ``unmeasured_window_seconds`` / ``unmeasured_window_source``、
        ``uncertain_seconds``。父类字段一个不动，便于与既有产物对照。
        """
        payload = ResourceGuard.summary(self)
        window, source = self.unmeasured_window_seconds()
        payload['budget_mode'] = self.budget_mode
        payload['refresh_count'] = self.refresh_count
        payload['refresh_log'] = [dict(entry) for entry in self.refresh_log]
        payload['last_refresh'] = (dict(self.refresh_log[-1])
                                   if self.refresh_log else None)
        payload['deadline_started_at'] = self.deadline_started_at
        payload['deadline_budget_seconds'] = self.deadline_budget_seconds
        payload['uncertain_seconds'] = self.uncertain_seconds
        payload['unmeasured_window_seconds'] = window
        payload['unmeasured_window_source'] = source
        payload['limits']['budget_mode'] = self.budget_mode
        return payload


# ---------------------------------------------------------------------------
# 进程监督：起 worker + 监督 + 收尾
# ---------------------------------------------------------------------------

def _spawn_worker(argv, env=None, cwd=None):
    """起 worker 子进程（POSIX 下 ``start_new_session=True``，自成进程组）。

    与 ``GuardedRun._popen_kwargs`` 同一口径：**终止只能打本轮自己的进程组**，
    绝不波及其他服务。这里显式重复一次而不用父类方法，是因为
    ``RefreshableGuardedRun`` 需要拿到 ``pgid`` 并在同一处决定是否开监督线程。
    """
    import subprocess
    kwargs = {'stdout': subprocess.PIPE, 'stderr': subprocess.STDOUT,
              'env': env}
    if cwd:
        kwargs['cwd'] = cwd
    if os.name == 'posix':
        kwargs['start_new_session'] = True
    return subprocess.Popen(argv, **kwargs)


def _pgid_of(process):
    if process is None or os.name != 'posix':
        return None
    try:
        return os.getpgid(process.pid)
    except (OSError, AttributeError):
        return None


class RefreshableGuardedRun(GuardedRun):
    """与 ``GuardedRun`` 同 API（``start`` / ``run`` / ``result`` / ``status``
    / ``summary``），状态值也**完全一致**，只是墙钟预算换成可刷新语义。

    ::

        run = RefreshableGuardedRun(
            argv, csv_path=..., status_path=...,
            budget_mode='none',              # 阶段墙钟不参与门禁
            total_budget_seconds=8 * 3600,   # 真正的总量上限
            heartbeat_path=os.path.join(out_dir, 'guard_heartbeat.txt'),
            heartbeat_timeout_seconds=None)  # 不设看门狗 = 不因静止判死
        run.start()
        ...
        for block in blocks:
            build_block(block)               # worker 侧写心跳
            # 父进程侧无需做任何事：监督线程读心跳 → 自动刷新 deadline

    **两种刷新通道，二选一或并用**：

    * ``heartbeat_path`` —— 监督线程按周期读文件，读到新时间戳就自动刷新。
      这是跨进程用法（本轮评测），不需要 worker import 任何监控代码；
    * ``run.guard.refresh_deadline('block=%d' % i)`` —— 编排层与监督同进程时
      直接调（补跑/调试用）。

    与父类一样，``run(timeout_seconds=...)`` 会把该值同时设为阶段预算与总预算；
    **本类额外保证**：即使 ``budget_mode='stage'``，``timeout_seconds`` 也只是
    "距上次刷新的最大间隔"，不是"整轮生命周期的硬上限"——那正是本轮要修掉的
    误杀。想要"整轮硬上限"，用 ``budget_mode='none'`` + 不刷新的总预算。
    """

    def __init__(self, argv, csv_path=None, status_path=None, log=None,
                 interval=RESOURCE_POLL_SECONDS, env=None, cwd=None,
                 stage_budgets=None, total_budget_seconds=None,
                 budget_mode=BUDGET_MODE_STAGE, stage_budget_seconds=None,
                 uncertain_seconds=None, heartbeat_path=None,
                 heartbeat_watchdog_seconds=None,
                 heartbeat_interval=WATCHER_INTERVAL_SECONDS,
                 guard=None, **guard_kwargs):
        GuardedRun.__init__(
            self, argv, csv_path=csv_path, status_path=status_path, log=log,
            interval=interval, env=env, cwd=cwd, stage_budgets=stage_budgets,
            total_budget_seconds=total_budget_seconds, **guard_kwargs)
        self.budget_mode = budget_mode
        self.stage_budget_seconds = _as_float(stage_budget_seconds)
        self.uncertain_seconds = _as_float(uncertain_seconds)
        self.heartbeat_path = heartbeat_path
        self.heartbeat_watchdog_seconds = _as_float(heartbeat_watchdog_seconds)
        self.heartbeat_interval = float(heartbeat_interval)
        #: 可注入的现成 guard（测试用假进程树/假读数时走这里）。
        self._injected_guard = guard
        self.heartbeat = None
        self.heartbeat_refreshes = 0
        self._stop_event = None
        self._watch_thread = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self):
        """启动 worker、建 guard、起监督线程与（可选）心跳观察线程。"""
        import threading
        self.process = _spawn_worker(self.argv, env=self.env, cwd=self.cwd)
        self.pgid = _pgid_of(self.process)
        guard = self._injected_guard
        if guard is None:
            guard = RefreshableGuard(
                self.process.pid,
                csv_path=self.csv_path,
                status_path=self.status_path,
                log=self.log,
                interval=self.interval,
                budget_mode=self.budget_mode,
                stage_budget_seconds=self.stage_budget_seconds,
                uncertain_seconds=self.uncertain_seconds,
                heartbeat_timeout_seconds=self.heartbeat_watchdog_seconds,
                stage_budgets=self.stage_budgets,
                total_budget_seconds=self.total_budget_seconds)
        else:
            # 注入的 guard 也要带上本轮的预算设置（它的读数由测试接管）。
            guard.budget_mode = self.budget_mode
            guard.log = self.log
            if self.stage_budget_seconds is not None:
                for stage in STAGES:
                    guard.stage_budgets[stage] = self.stage_budget_seconds
            if self.total_budget_seconds is not None:
                guard.set_total_budget(self.total_budget_seconds)
        self.guard = guard
        guard.start()
        if self.heartbeat_path:
            self.heartbeat = FileHeartbeat(
                self.heartbeat_path, interval=self.heartbeat_interval,
                timeout_seconds=self.heartbeat_watchdog_seconds, log=self.log)
            self._stop_event = threading.Event()
            self._watch_thread = threading.Thread(
                target=self.heartbeat.supervise,
                name='heartbeat-watcher',
                kwargs={'guard': guard, 'stop_event': self._stop_event})
            self._watch_thread.daemon = True
            self._watch_thread.start()
        return self.process

    def _sync_guard_budget(self, stage_budgets, total_budget_seconds):
        """把预算同步进 guard（``run()`` 在 ``start()`` 之前被调用时的补丁）。"""
        if self.guard is None:
            return
        self.guard.stage_budgets = dict(stage_budgets)
        self.guard.total_budget_seconds = _as_float(total_budget_seconds)
        self.guard.deadline_budget_seconds = self.guard.total_budget_seconds

    def run(self, timeout_seconds=None, poll_seconds=0.1, on_stage=None,
            stage_plan=None, stage='train', hard_timeout_seconds=None,
            total_budget_seconds=None):
        """跑完 worker 并返回终态 dict（状态取值同 ``GuardedRun``）。

        :param float timeout_seconds: **距上次 deadline 刷新的最大间隔**。
            ``'stage'`` 模式下落到当前阶段的预算上；``'none'`` 模式下落到总预算
            （此时每次刷新都重置这个窗口）。父类"随手传一个超时就一定有效"
            的性质保留：无论哪种模式，传了它就会被判。
        :param float total_budget_seconds: 覆盖构造时的总量预算。
        :param float hard_timeout_seconds: **整轮绝对上限**（不被刷新推动）。
            与 ``timeout_seconds`` 是两把不同的尺子，正是本轮要区分的东西：
            "单块不许超过 10 分钟"（可刷新）与"整轮不许超过 8 小时"（不可刷新）。
            实现方式是父类既有的 ``stage_started_at``：它只在 ``set_stage`` 时
            清零，而 :meth:`RefreshableGuard.refresh_deadline` **不碰它**。
        :param callable on_stage: ``(guard) -> None``，worker 仍在跑时调用一次。
        :param list stage_plan: ``[(stage, budget_seconds), ...]``，只取第一项
            作为起始阶段。
        :param str stage: ``timeout_seconds`` 归属的阶段名（``'stage'`` 模式）。
        """
        import subprocess
        stage_plan = list(stage_plan or [])
        if stage_plan:
            budgets = dict(self.stage_budgets)
            for plan_stage, plan_budget in stage_plan:
                budgets[plan_stage] = plan_budget
            self.stage_budgets = budgets
        if timeout_seconds is not None:
            if self.budget_mode == BUDGET_MODE_STAGE:
                self.stage_budgets[stage] = float(timeout_seconds)
            # 两种模式都把总预算设上：`'none'` 模式下它是唯一墙钟出口
            # （否则"阶段不看、总量也不看"就等于没有墙钟线了）。
            self.total_budget_seconds = float(timeout_seconds)
        if total_budget_seconds is not None:
            self.total_budget_seconds = float(total_budget_seconds)
        if self.process is None:
            self.start()
        self._sync_guard_budget(self.stage_budgets, self.total_budget_seconds)
        if hard_timeout_seconds is not None:
            self.guard.hard_deadline_seconds = float(hard_timeout_seconds)
        self.guard.set_stage(stage_plan[0][0] if stage_plan else stage)
        exit_code = None
        while True:
            try:
                exit_code = self.process.wait(timeout=poll_seconds)
                break
            except subprocess.TimeoutExpired:
                if on_stage is not None:
                    try:
                        on_stage(self.guard)
                    except Exception as error:  # noqa: BLE001
                        self.log('WARN on_stage failed: %s' % error)
                    on_stage = None
                if self._hard_deadline_hit():
                    self.guard._on_violation(
                        '%s: hard wall-clock limit %.0fs reached (not '
                        'refreshable)' % (REASON_STAGE_TIMEOUT,
                                          self.guard.hard_deadline_seconds))
                if self.guard.violation is not None:
                    self.terminated = True
                    self.termination = terminate_process_group(
                        self.pgid, self.guard.grace_seconds, log=self.log)
                    try:
                        exit_code = self.process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        exit_code = None
                    break
        if exit_code is None:
            try:
                exit_code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = None
        self._close_pipes()
        if self.guard is not None:
            self._stop_watcher()
            self.guard.stop()
            result = self.result(exit_code)
            self.guard.final_status = result['status']
            self.guard._write_status()
            return result
        return self.result(exit_code)

    def _hard_deadline_hit(self):
        """整轮绝对上限是否已到（只看 ``stage_started_at``，刷新推不动它）。"""
        guard = self.guard
        limit = _as_float(getattr(guard, 'hard_deadline_seconds', None))
        if limit is None:
            return False
        return guard._clock() - guard.stage_started_at >= limit

    def _stop_watcher(self):
        if self._stop_event is not None:
            self._stop_event.set()
        thread = self._watch_thread
        if thread is not None and thread is not threading_current():
            thread.join(5.0)
        if self.heartbeat is not None:
            self.heartbeat_refreshes = self.heartbeat.refreshes
        self._watch_thread = None

    # -- 统计 -------------------------------------------------------------

    def status(self):
        """终态（未结束时为 ``running``），取值与 ``resource_guard`` 一致。"""
        if self.guard is None:
            return STATUS_RUNNING
        return self.guard.status()

    def summary(self):
        """guard 摘要；没有 guard 时返回最小可用结构（不抛异常）。"""
        if self.guard is None:
            return {'status': STATUS_RUNNING, 'root_pid': None,
                    'violation': None, 'refresh_count': 0}
        return self.guard.summary()

    def result(self, exit_code):
        """与 ``GuardedRun.result`` 同结构，额外带上心跳刷新计数。

        ``status`` 的映射规则**完全不改**：guard 有 violation 就用 guard 的
        状态，否则看退出码（非 0 → ``correctness_failed``），否则 ``success``。
        """
        payload = GuardedRun.result(self, exit_code)
        payload['budget_mode'] = self.budget_mode
        payload['refresh_count'] = (self.guard.refresh_count
                                    if self.guard is not None else 0)
        payload['heartbeat_refreshes'] = self.heartbeat_refreshes
        payload['heartbeat_path'] = self.heartbeat_path
        return payload


def threading_current():
    """``threading.current_thread()`` 的薄封装（避免在本模块顶部 import threading）。"""
    import threading
    return threading.current_thread()
