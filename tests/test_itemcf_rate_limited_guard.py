# -*- coding: utf-8 -*-
"""带留出评测专用监督（``experiments/itemcf/rate_limited_guard.py``）的契约测试。

本模块测的是**这一轮新引入的三种语义**，以及"没有退化"这件事：

1. **可刷新 deadline**：刷新之后不再触发阶段超时；**不刷新就按旧语义超时**
   —— 后者与前者同等重要，否则"放宽"会变成"取消"。
2. **显式心跳窗口**：声明了"接下来 T 秒不可测量"的窗口内不判
   ``monitor_failed``；窗口外照样判。**未声明时与现状完全一致**（60 s）。
3. **硬性终止线一条都没退化**：RSS > 3 GiB、``MemAvailable`` 连续 30 s < 256 MB、
   磁盘 < 2 GiB 仍然真正生效，且**优先级高于墙钟**——刷新 deadline 推不动它们。

另外还有一条**只有本模块才有**的边界：``rate_limited_guard`` 必须真的
**复用** ``resource_guard`` 的实现，而不是复制粘贴一整套。所以这里直接
断言身份相等（``R.process_tree_rss_bytes is G.process_tree_rss_bytes`` 等）。

读数全部走注入（``rss_reader`` / ``mem_reader`` / ``disk_reader`` + 假时钟），
**不读真实 /proc、不真的耗尽机器内存、不真的杀进程**。唯一用到真进程的地方是
心跳文件的读写（就是个临时文件）与一条端到端冒烟用例。

运行：
    backend/venv/Scripts/python.exe -m unittest tests.test_itemcf_rate_limited_guard -v
"""
import ast
import csv
import io
import json
import os
import sys
import threading
import time
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from experiments.itemcf import rate_limited_guard as R  # noqa: E402
from experiments.itemcf import resource_guard as G  # noqa: E402

FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_guard_tmp')
MODULE_PATH = os.path.join(PROJECT, 'experiments', 'itemcf',
                           'rate_limited_guard.py')
_MIB = 1 << 20
_GIB = 1 << 30

_MODULE_SOURCE = [None]

_SEQ_LOCK = threading.Lock()
_SEQ = [0]


def module_source():
    """读被测模块源码一次（编码显式 utf-8，绝不走 ANSI/CP936）。"""
    if _MODULE_SOURCE[0] is None:
        with io.open(MODULE_PATH, encoding='utf-8') as handle:
            _MODULE_SOURCE[0] = handle.read()
    return _MODULE_SOURCE[0]


def _strip_comments_and_docstrings(source):
    """剥掉注释与 docstring，只留**可执行代码**文本。

    扫描"有没有用某个 3.6 之后的 API"时必须先剥掉文档：被测模块的 docstring
    里**刻意**写着"不用 dataclasses / f-string / capture_output"，
    连文档里提一下都算违规的话，这条用例就等于自己判自己死。

    只剥**真正的 docstring**（``Module`` / ``ClassDef`` / ``FunctionDef`` 的
    第一个 ``Expr(Str)``），不碰其他字符串常量——那些是代码的一部分
    （例如 ``'%s: ...'`` 格式串，剥掉就把正向证据也一起剥没了）。
    """
    import ast as _ast
    tree = _ast.parse(source)
    lines = source.splitlines()
    drop = set()

    def _mark(node):
        body = getattr(node, 'body', None)
        if not body:
            return
        first = body[0]
        if not isinstance(first, _ast.Expr):
            return
        value = first.value
        if not (isinstance(value, _ast.Str)
                or (isinstance(value, _ast.Constant)
                    and isinstance(value.value, str))):
            return
        start = getattr(first, 'lineno', None)
        end = getattr(first, 'end_lineno', None)
        if start is None or end is None:
            return
        for lineno in range(start, end + 1):
            drop.add(lineno)

    _mark(tree)
    for node in _ast.walk(tree):
        if isinstance(node, (_ast.ClassDef, _ast.FunctionDef,
                             _ast.AsyncFunctionDef)):
            _mark(node)
    kept = [line for index, line in enumerate(lines, start=1)
            if index not in drop]
    text = '\n'.join(kept)
    # 再剥行内 ``#`` 注释（不处理字符串里的 # —— 本模块没有这种写法）。
    return '\n'.join(line.split('#', 1)[0] for line in text.splitlines())


def fixture_dir(prefix):
    """建唯一临时目录（沙箱下不做清理，与既有 itemcf 测试同策略）。

    ``time.time()*1000`` 在同一测试进程里会连续撞名（Windows 时钟分辨率约
    15 ms，一个用例跑完不到 1 ms），所以再叠一个单调序号并**重试**，
    绝不返回一个已被使用的目录。
    """
    for _attempt in range(1000):
        with _SEQ_LOCK:
            _SEQ[0] += 1
            seq = _SEQ[0]
        path = os.path.join(
            FIXTURE_ROOT,
            '%s-%d-%d-%d' % (prefix, int(time.time() * 1000), os.getpid(), seq))
        try:
            os.makedirs(path)
            return path
        except OSError:
            continue
    raise RuntimeError('could not create a unique fixture dir for %r under %s'
                       % (prefix, FIXTURE_ROOT))


class FakeClock(object):
    """可控单调时钟：不真等 30 秒就能验证"连续 30 秒"与各种 deadline。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


class StaticReadings(object):
    """可注入读数：RSS / 内存 / 磁盘都变成可控的常数（可随时改）。"""

    def __init__(self, rss=64 * _MIB, mem_available=2048 * _MIB,
                 disk_free=20 * _GIB, pids=None, rss_raises=False):
        self.rss = rss
        self.mem_available = mem_available
        self.disk_free = disk_free
        self.pids = list(pids) if pids else None
        self.rss_raises = rss_raises
        self.rss_calls = 0

    def rss_reader(self, root_pid):
        self.rss_calls += 1
        if self.rss_raises:
            raise RuntimeError('injected rss failure')
        pids = self.pids if self.pids is not None else [root_pid]
        return self.rss, pids

    def mem_reader(self):
        info = {}
        if self.mem_available is not None:
            info['MemAvailable'] = self.mem_available
        return info

    def disk_reader(self):
        return self.disk_free


def make_guard(tmp, clock=None, readings=None, mode=R.BUDGET_MODE_STAGE,
               csv_path=None, status_path=None, **kwargs):
    """构造一个带可控读数的 ``RefreshableGuard``（不启动线程，便于逐步 tick）。"""
    readings = readings or StaticReadings()
    guard = R.RefreshableGuard(
        root_pid=os.getpid(),
        budget_mode=mode,
        csv_path=(csv_path if csv_path is not None
                  else os.path.join(tmp, 'resources.csv')),
        status_path=status_path,
        rss_reader=readings.rss_reader,
        mem_reader=readings.mem_reader,
        disk_reader=readings.disk_reader,
        clock=clock or time.time,
        **kwargs)
    guard._readings = readings
    return guard


def tick(guard, clock):
    """走一遍**真正的**单次监督周期：采样 + 写行 + 判门禁。

    刻意调 ``_tick()`` 而不是 ``_evaluate(_collect())``：前者会把采样行写进
    ``guard.rows`` 与 ``resources.csv``，后者不会。用后者测出来的"没超时"
    与真实监督线程的行为并不等价（CSV 与统计口径都拿不到）。
    """
    ok = guard._tick()
    return None if ok else guard.violation


# ---------------------------------------------------------------------------
# 1. 必须复用 resource_guard，而不是复制粘贴
# ---------------------------------------------------------------------------

class ReuseContractTests(unittest.TestCase):
    """本模块的存在理由是"在 resource_guard 之上组合"，这一点必须可测。

    如果哪天有人为了图方便把 ``process_tree_rss_bytes`` 抄一份进来，下面这些
    身份断言会立刻变红——于是也就不会出现"两份读 /proc 的实现慢慢分家"。
    """

    def test_readers_and_termination_are_the_same_objects(self):
        for name in ('read_meminfo', 'process_tree_pids',
                     'process_tree_rss_bytes', 'read_disk_free_bytes',
                     'terminate_process_group'):
            self.assertIs(getattr(R, name), getattr(G, name),
                          '%s must be reused from resource_guard, not copied'
                          % name)
        self.assertEqual(R.CSV_FIELDS, G.CSV_FIELDS)

    def test_guard_classes_are_subclasses_of_the_originals(self):
        self.assertTrue(issubclass(R.RefreshableGuard, G.ResourceGuard))
        self.assertTrue(issubclass(R.RefreshableGuardedRun, G.GuardedRun))

    def test_status_and_reason_vocabulary_is_shared(self):
        """状态值必须**同一套**，否则调用方无法按统一口径写结论。"""
        for name in ('STATUS_RUNNING', 'STATUS_SUCCESS', 'STATUS_RESOURCE_BLOCKED',
                     'STATUS_RESOURCE_FAILED', 'STATUS_MONITOR_FAILED',
                     'STATUS_CORRECTNESS_FAILED', 'REASON_RSS', 'REASON_MEM',
                     'REASON_DISK', 'REASON_STAGE_TIMEOUT',
                     'REASON_MONITOR_FAILED'):
            self.assertEqual(getattr(R, name), getattr(G, name),
                             '%s diverged from resource_guard' % name)
        self.assertEqual(
            set([R.STATUS_SUCCESS, R.STATUS_RESOURCE_BLOCKED,
                 R.STATUS_RESOURCE_FAILED, R.STATUS_MONITOR_FAILED,
                 R.STATUS_CORRECTNESS_FAILED]),
            set(['success', 'resource_blocked', 'resource_failed',
                 'monitor_failed', 'correctness_failed']))

    def test_hard_limit_defaults_match_the_frozen_protocol(self):
        """三条硬线的默认数字必须还是冻结值（3 GiB / 256 MB / 30 s / 2 GiB）。"""
        self.assertEqual(R.RSS_LIMIT_BYTES, 3 * _GIB)
        self.assertEqual(R.MEM_AVAILABLE_FLOOR_BYTES, 256 * _MIB)
        self.assertEqual(R.MEM_AVAILABLE_STREAK_SECONDS, 30.0)
        self.assertEqual(R.DISK_FLOOR_BYTES, 2 * _GIB)
        self.assertEqual(R.RESOURCE_POLL_SECONDS, 2.0)
        self.assertEqual(R.MAX_UNMEASURED_SECONDS, 60.0)

    def test_module_does_not_reimplement_proc_readers(self):
        """源码层面：不得自己开 ``/proc/meminfo`` 或调 ``os.statvfs``。

        这两处一旦各写一份，"读数的口径"就会悄悄分家——正是本模块要避免的。
        """
        source = module_source()
        self.assertNotIn('/proc/meminfo', source)
        self.assertNotIn('statvfs', source)
        self.assertNotIn('killpg', source, 'termination must be reused')

    def test_no_existing_file_was_modified(self):
        """**只新增文件**：本模块不得 import 后改写 ``resource_guard`` 的任何常量。

        直接断言"导入本模块不会改变 resource_guard 的对象身份与常量"。
        """
        snapshot = {
            'RSS_LIMIT_BYTES': G.RSS_LIMIT_BYTES,
            'MAX_UNMEASURED_SECONDS': G.MAX_UNMEASURED_SECONDS,
            'CSV_FIELDS': G.CSV_FIELDS,
            'process_tree_rss_bytes': G.process_tree_rss_bytes,
            'ResourceGuard._evaluate': G.ResourceGuard._evaluate,
            'ResourceGuard._monitor_failure': G.ResourceGuard._monitor_failure,
        }
        # 重新导入一次（幂等），若本模块在 import 期做了猴子补丁就会露出来。
        import importlib
        importlib.reload(R)
        self.assertEqual(snapshot['RSS_LIMIT_BYTES'], G.RSS_LIMIT_BYTES)
        self.assertEqual(snapshot['MAX_UNMEASURED_SECONDS'],
                         G.MAX_UNMEASURED_SECONDS)
        self.assertEqual(snapshot['CSV_FIELDS'], G.CSV_FIELDS)
        self.assertIs(snapshot['process_tree_rss_bytes'],
                      G.process_tree_rss_bytes)
        self.assertIs(snapshot['ResourceGuard._evaluate'],
                      G.ResourceGuard._evaluate)
        self.assertIs(snapshot['ResourceGuard._monitor_failure'],
                      G.ResourceGuard._monitor_failure)


# ---------------------------------------------------------------------------
# 2. 可刷新 deadline —— 本轮的核心语义
# ---------------------------------------------------------------------------

class RefreshDeadlineTests(unittest.TestCase):
    """刷新 = "我还在按计划推进"，不是"取消墙钟线"。

    因此每个"刷新后不再超时"的用例，都配一条"不刷新仍按旧语义超时"的用例。
    """

    def test_stage_mode_without_refresh_still_times_out_like_before(self):
        """**不刷新时保持与现状一致**：这是最重要的一条，先钉死它。"""
        clock = FakeClock()
        tmp = fixture_dir('refresh-none')
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')
        self.assertIsNone(tick(guard, clock))
        clock.advance(9.0)
        self.assertIsNone(tick(guard, clock))
        clock.advance(2.0)
        reason = tick(guard, clock)
        self.assertIsNotNone(reason, '10 s stage budget must still fire')
        self.assertIn(R.REASON_STAGE_TIMEOUT, reason)
        self.assertIn('train', reason)

    def test_refresh_deadline_prevents_the_stage_timeout(self):
        """刷新之后同一阶段可以继续跑过旧预算——长任务的误杀被修掉了。"""
        clock = FakeClock()
        tmp = fixture_dir('refresh-hit')
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')
        clock.advance(9.0)
        self.assertIsNone(tick(guard, clock))

        guard.refresh_deadline('block=1')
        clock.advance(9.0)          # 距刷新 9 s，仍 < 10 s
        self.assertIsNone(tick(guard, clock),
                          'refreshed deadline must restart the stage clock')

        # 再刷两次，累计跑过 27 s —— 远超最初的 10 s 预算，仍不许超时。
        for index in range(2):
            guard.refresh_deadline('block=%d' % (index + 2))
            clock.advance(9.0)
            self.assertIsNone(tick(guard, clock))
        self.assertGreater(clock.now - guard.started_at, 27.0)
        self.assertIsNone(guard.violation)
        self.assertEqual(guard.refresh_count, 4)  # set_stage 一次 + 三次显式

    def test_refresh_deadline_does_not_extend_forever(self):
        """刷新只推基准，不放宽窗口：刷新之后照样会到期。"""
        clock = FakeClock()
        tmp = fixture_dir('refresh-window')
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')
        guard.refresh_deadline('block=1')
        clock.advance(11.0)
        reason = tick(guard, clock)
        self.assertIsNotNone(reason)
        self.assertIn(R.REASON_STAGE_TIMEOUT, reason)

    def test_refresh_log_records_every_refresh_with_its_reason(self):
        """事后必须能回答"是哪次刷新让这轮活下来的"。"""
        clock = FakeClock()
        tmp = fixture_dir('refresh-log')
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 100.0})
        guard.set_stage('train')
        guard.refresh_deadline('block=7')
        guard.refresh_deadline('scoring_done')
        self.assertEqual(guard.refresh_count, 3)
        reasons = [entry['reason'] for entry in guard.refresh_log]
        self.assertEqual(reasons, ['set_stage:train', 'block=7', 'scoring_done'])
        summary = guard.summary()
        self.assertEqual(summary['refresh_count'], 3)
        self.assertEqual(summary['last_refresh']['reason'], 'scoring_done')
        self.assertEqual(summary['budget_mode'], R.BUDGET_MODE_STAGE)
        # 刷新日志必须能落盘（json 可序列化），否则事后无从追溯。
        json.dumps(summary)

    def test_set_stage_also_refreshes_so_a_switch_never_times_out_instantly(self):
        """换阶段后不能用上一个阶段的旧基准立刻判超时（错位）。"""
        clock = FakeClock()
        tmp = fixture_dir('refresh-setstage')
        guard = make_guard(tmp, clock=clock,
                           stage_budgets={'prepare': 5.0, 'precheck': 5.0})
        guard.set_stage('prepare')
        clock.advance(4.9)
        self.assertIsNone(tick(guard, clock))
        guard.set_stage('precheck')
        self.assertIsNone(tick(guard, clock),
                          'set_stage must move the deadline baseline too')


class NoStageBudgetModeTests(unittest.TestCase):
    """``budget_mode='none'``：阶段墙钟**完全不参与**门禁。

    这是本轮"按列块循环构图、阶段边界不固定"的直接需求——阶段名仍然记录
    （可读性），但不再有"单阶段预算"这条路。
    """

    def test_none_mode_ignores_stage_budgets_entirely(self):
        clock = FakeClock()
        tmp = fixture_dir('none-mode')
        guard = make_guard(tmp, clock=clock, mode=R.BUDGET_MODE_NONE,
                           stage_budgets={'train': 1.0})
        guard.set_stage('train')
        # 即使配了 1 秒的阶段预算，'none' 模式下也必须无视它。
        for _ in range(20):
            clock.advance(60.0)
            self.assertIsNone(tick(guard, clock),
                              'stage budget must not apply in none mode')
        self.assertIsNone(guard.violation)
        self.assertGreaterEqual(clock.now - guard.started_at, 1200.0)

    def test_none_mode_still_enforces_the_total_budget(self):
        """总量预算仍是**唯一**的墙钟上限——否则等于没有墙钟线。"""
        clock = FakeClock()
        tmp = fixture_dir('none-total')
        guard = make_guard(tmp, clock=clock, mode=R.BUDGET_MODE_NONE,
                           total_budget_seconds=100.0)
        guard.set_stage('train')
        clock.advance(99.0)
        self.assertIsNone(tick(guard, clock))
        clock.advance(2.0)
        reason = tick(guard, clock)
        self.assertIsNotNone(reason)
        self.assertIn(R.REASON_STAGE_TIMEOUT, reason)
        self.assertIn('total budget', reason)

    def test_none_mode_with_no_budget_has_no_wall_clock_line(self):
        """明确"不设墙钟"时不许偷偷拿阶段预算兜底。"""
        clock = FakeClock()
        tmp = fixture_dir('none-nobudget')
        guard = make_guard(tmp, clock=clock, mode=R.BUDGET_MODE_NONE,
                           stage_budgets={'train': 0.5})
        guard.set_stage('train')
        clock.advance(100000.0)
        self.assertIsNone(tick(guard, clock))
        self.assertIsNone(guard.violation)

    def test_unknown_budget_mode_is_rejected(self):
        tmp = fixture_dir('none-bad')
        with self.assertRaises(ValueError):
            make_guard(tmp, mode='sometimes')

    def test_none_mode_status_maps_timeout_to_resource_failed(self):
        """状态映射与 resource_guard 同口径：超时 → resource_failed。"""
        clock = FakeClock()
        tmp = fixture_dir('none-status')
        guard = make_guard(tmp, clock=clock, mode=R.BUDGET_MODE_NONE,
                           total_budget_seconds=5.0)
        guard.set_stage('train')
        clock.advance(6.0)
        reason = tick(guard, clock)
        guard._on_violation(reason)
        self.assertEqual(guard.status(), R.STATUS_RESOURCE_FAILED)
        self.assertTrue(guard.terminated)


# ---------------------------------------------------------------------------
# 3. 显式心跳窗口（MAX_UNMEASURED_SECONDS 的替代语义）
# ---------------------------------------------------------------------------

class ExplicitHeartbeatWindowTests(unittest.TestCase):
    """未声明 → 与现状一致；声明了 T → 窗口是 T 的有限倍数，超时照样判死。"""

    def _unreadable(self, tmp, clock, **kwargs):
        readings = StaticReadings(rss_raises=True)
        return make_guard(tmp, clock=clock, readings=readings, **kwargs)

    def test_without_declaration_behaviour_is_unchanged(self):
        """**默认保守**：不声明就还是 60 秒，宁可停下也不放过。"""
        clock = FakeClock()
        tmp = fixture_dir('hb-default')
        guard = self._unreadable(tmp, clock)
        self.assertEqual(guard.max_unmeasured_seconds, 60.0)
        self.assertTrue(guard._tick())          # 第一次采不到
        self.assertIsNone(guard.violation)
        clock.advance(59.0)
        guard._tick()
        self.assertIsNone(guard.violation, '59 s < 60 s must not fail yet')
        clock.advance(2.0)                      # 累计 61 s
        guard._tick()
        self.assertIsNotNone(guard.violation)
        self.assertEqual(guard.violation_reason, R.REASON_MONITOR_FAILED)
        self.assertEqual(guard.status(), R.STATUS_MONITOR_FAILED)

    def test_declared_window_is_not_judged_inside_it(self):
        """声明"接下来 120 秒不可测量"→ 窗口 240 s 内不许判 monitor_failed。"""
        clock = FakeClock()
        tmp = fixture_dir('hb-declared')
        guard = self._unreadable(tmp, clock)
        guard._tick()                            # 基准：已经采不到
        guard.declare_unmeasured(120.0, reason='mmap warmup')
        for _ in range(6):
            clock.advance(20.0)                  # 累计 120 s == 声明窗
            self.assertTrue(guard._tick(),
                            'inside the declared window the loop must keep '
                            'running')
            self.assertIsNone(guard.violation)
        self.assertEqual(guard.status(), R.STATUS_RUNNING)

    def test_declared_window_still_expires(self):
        """窗口是**有限**的：超出 `T * HEARTBEAT_MAX_SILENCE_FACTOR` 必须判死。"""
        clock = FakeClock()
        tmp = fixture_dir('hb-expire')
        guard = self._unreadable(tmp, clock)
        guard._tick()
        guard.declare_unmeasured(100.0)          # 窗口 = 200 s
        clock.advance(199.0)
        self.assertTrue(guard._tick(), '199 s < 200 s window: still supervised')
        self.assertIsNone(guard.violation)
        # 窗口内报的是 declared；到点立刻判死。
        self.assertEqual(guard.unmeasured_window_seconds()[1], 'declared')
        clock.advance(2.0)                       # 201 s >= 200 s
        self.assertFalse(guard._tick())
        self.assertIsNotNone(guard.violation)
        self.assertEqual(guard.violation_reason, R.REASON_MONITOR_FAILED)
        self.assertIn('window=', guard.violation)

    def test_expiry_inside_the_window_names_the_declared_source(self):
        """在**声明窗内**触发判死时，原因里必须写明窗口与来源。

        这条是因为 `unmeasured_window_seconds` 有"过期即回落默认"的语义：
        一旦越过 `T * 系数`，来源就变回 ``default``。所以想验证
        ``source=declared`` 出现在原因里，必须在窗口内取值。

        构造关键：**先让读数失败一次、把"上次有效读数"留在足够久之前**，
        再声明长窗。这样"距上次有效读数"会比"距声明时刻"更早到窗口边界，
        于是判死发生在声明**尚未过期**的时刻，来源才会报 ``declared``。
        """
        clock = FakeClock()
        tmp = fixture_dir('hb-expire-src')
        guard = self._unreadable(tmp, clock)
        guard._tick()                               # 失败行进入 _last_row
        clock.advance(3000.0)                       # 距上次有效读数已是 3000 s
        guard.declare_unmeasured(2000.0)            # 声明窗 = 4000 s，从此刻起算
        self.assertEqual(guard.unmeasured_window_seconds(), (4000.0, 'declared'))
        clock.advance(1001.0)                       # 距上次有效读数 4001 s >= 4000 s
        self.assertEqual(guard.unmeasured_window_seconds()[1], 'declared',
                         'declaration must not have expired at this point')
        reason = guard._monitor_failure(clock())
        self.assertIsNotNone(reason)
        self.assertIn('window=4000s', reason)
        self.assertIn('source=declared', reason)

    def test_declared_window_does_not_permanently_relax_the_line(self):
        """声明窗过期后必须回落默认 60 s，不能一次声明永久放宽。"""
        clock = FakeClock()
        tmp = fixture_dir('hb-falls-back')
        guard = self._unreadable(tmp, clock)
        guard.declare_unmeasured(10.0)           # 窗口 = 20 s
        clock.advance(25.0)
        window, source = guard.unmeasured_window_seconds()
        self.assertEqual(window, 60.0,
                         'expired declaration must fall back to the default')
        self.assertEqual(source, 'default')

    def test_declare_unmeasured_requires_a_positive_number(self):
        tmp = fixture_dir('hb-bad')
        guard = make_guard(tmp)
        for bad in (0, -1.0, None, 'nope'):
            with self.assertRaises(ValueError):
                guard.declare_unmeasured(bad)

    def test_clear_unmeasured_restores_the_tight_default(self):
        tmp = fixture_dir('hb-clear')
        clock = FakeClock()
        guard = self._unreadable(tmp, clock)
        guard.declare_unmeasured(600.0)
        self.assertEqual(guard.unmeasured_window_seconds()[1], 'declared')
        guard.clear_unmeasured()
        self.assertEqual(guard.unmeasured_window_seconds(),
                         (60.0, 'default'))

    def test_declaration_also_refreshes_the_wall_clock(self):
        """一段**已知**的空窗不该被算成"任务卡住"（墙钟也要推）。"""
        clock = FakeClock()
        tmp = fixture_dir('hb-refresh')
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')                 # 刷新 #1
        clock.advance(9.0)
        guard.declare_unmeasured(20.0, reason='io stall')   # 刷新 #2
        self.assertEqual(guard.refresh_log[-1]['reason'], 'io stall')
        clock.advance(9.0)                       # 距刷新 9 s < 10 s
        self.assertIsNone(tick(guard, clock))
        # 不刷新的话，从 set_stage 起算早就超过 10 s —— 证明刷新真的生效了。
        self.assertGreaterEqual(clock.now - guard.stage_started_at, 18.0)

    def test_uncertain_window_is_visible_in_summary(self):
        tmp = fixture_dir('hb-summary')
        clock = FakeClock()
        guard = self._unreadable(tmp, clock)
        guard.declare_unmeasured(30.0)
        summary = guard.summary()
        self.assertEqual(summary['uncertain_seconds'], 30.0)
        # 窗口 = 30 * HEARTBEAT_MAX_SILENCE_FACTOR(2.0) = 60.0，
        # 恰好与默认 60.0 相等；取 max 后同名同值，来源按默认优先。
        self.assertEqual(summary['unmeasured_window_seconds'], 60.0)
        self.assertIn(summary['unmeasured_window_source'], ('default', 'declared'))
        json.dumps(summary)
        # limits 段必须带上新的模式字段，便于与旧产物对照。
        self.assertEqual(summary['limits']['budget_mode'], R.BUDGET_MODE_STAGE)
        self.assertEqual(summary['limits']['max_unmeasured_seconds'], 60.0)

    def test_a_declaration_larger_than_the_default_is_visible_as_declared(self):
        """声明的窗口**大于**默认 60 s 时，来源必须报 ``declared``（真的放宽了）。"""
        tmp = fixture_dir('hb-summary-big')
        clock = FakeClock()
        guard = self._unreadable(tmp, clock)
        guard.declare_unmeasured(600.0)
        summary = guard.summary()
        self.assertEqual(summary['uncertain_seconds'], 600.0)
        self.assertEqual(summary['unmeasured_window_seconds'], 1200.0)
        self.assertEqual(summary['unmeasured_window_source'], 'declared')
        json.dumps(summary)


# ---------------------------------------------------------------------------
# 4. 心跳文件通道（跨进程的显式心跳）
# ---------------------------------------------------------------------------

class HeartbeatFileTests(unittest.TestCase):

    def test_write_and_parse_round_trip(self):
        tmp = fixture_dir('hbfile')
        path = os.path.join(tmp, 'guard_heartbeat.txt')
        stamp = R.write_heartbeat_file(path, 'block=3')
        self.assertTrue(os.path.isfile(path))
        with io.open(path, encoding='utf-8') as handle:
            text = handle.read()
        # 文件里是 ``%.6f``，往返精度到微秒即可（不要拿 float 精确比）。
        self.assertAlmostEqual(R.parse_heartbeat_stamp(text), stamp, places=6)
        self.assertIn('block=3', text)
        # 原子写不留 .tmp。
        self.assertFalse(os.path.exists(path + '.tmp'))

    def test_parse_distinguishes_missing_from_zero(self):
        """``None``（没写过/写坏了）必须与 ``0.0``（很旧但合法）区分。

        混为一谈会让监督把坏文件当成"心跳极旧"，反复误触发。
        """
        self.assertIsNone(R.parse_heartbeat_stamp(None))
        self.assertIsNone(R.parse_heartbeat_stamp(''))
        self.assertIsNone(R.parse_heartbeat_stamp('garbage'))
        self.assertEqual(R.parse_heartbeat_stamp('0.000000\tn'), 0.0)
        self.assertEqual(R.parse_heartbeat_stamp('1234.5'), 1234.5)

    def test_heartbeat_writer_is_forgiving(self):
        """心跳写不出去不能把 worker 打死——只记错误。"""
        tmp = fixture_dir('hbwriter')
        # 路径指向一个**文件**下的子路径：makedirs 必失败。
        blocker = os.path.join(tmp, 'blocker')
        with open(blocker, 'w') as handle:
            handle.write('x')
        beat = R.HeartbeatWriter(os.path.join(blocker, 'nested', 'hb.txt'))
        self.assertIsNone(beat.beat('nope'))
        self.assertEqual(beat.count, 0)
        self.assertTrue(beat.errors)

    def test_refresh_from_heartbeat_ignores_missing_file(self):
        tmp = fixture_dir('hbrefresh-missing')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock)
        before = guard.refresh_count
        outcome = R.refresh_from_heartbeat(guard, os.path.join(tmp, 'nope.txt'))
        self.assertFalse(outcome['changed'])
        self.assertIsNone(outcome['stamp'])
        self.assertEqual(guard.refresh_count, before)

    def test_changed_heartbeat_refreshes_the_guard(self):
        tmp = fixture_dir('hbrefresh-changed')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')                 # 刷新 #1
        path = os.path.join(tmp, 'hb.txt')
        R.write_heartbeat_file(path, 'block=1', now=clock.now + 1.0)
        first = R.refresh_from_heartbeat(guard, path)
        self.assertTrue(first['changed'])
        self.assertTrue(first['interpreted_as_refresh'])
        self.assertEqual(guard.refresh_log[-1]['reason'], 'heartbeat')
        self.assertEqual(guard.refresh_count, 2)
        # 同一时间戳重复读**不算**新心跳（否则一次写盘会被反复刷新）。
        second = R.refresh_from_heartbeat(guard, path)
        self.assertFalse(second['changed'])
        self.assertEqual(guard.refresh_count, 2, 'stale stamp must not refresh')
        # 更新的时间戳才算新心跳。
        R.write_heartbeat_file(path, 'block=2', now=clock.now + 2.0)
        third = R.refresh_from_heartbeat(guard, path)
        self.assertTrue(third['changed'])
        self.assertEqual(guard.refresh_count, 3)

    def test_new_heartbeat_prevents_the_stage_timeout(self):
        """端到端语义：每块写一次心跳 → 单块预算不再误杀整轮。"""
        tmp = fixture_dir('hbrefresh-live')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')
        path = os.path.join(tmp, 'hb.txt')
        for block in range(6):
            R.write_heartbeat_file(path, 'block=%d' % block,
                                   now=clock.now + 1.0 + block)
            R.refresh_from_heartbeat(guard, path)
            clock.advance(9.0)
            self.assertIsNone(tick(guard, clock))
        self.assertIsNone(guard.violation)
        self.assertGreaterEqual(clock.now - guard.started_at, 54.0)

    def test_heartbeat_watchdog_reports_silence(self):
        """看门狗：块间**不刷新**超过 timeout 时必须报出来（不静默放行）。"""
        tmp = fixture_dir('hb-watchdog')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock)
        guard.started_at = clock()
        path = os.path.join(tmp, 'hb.txt')
        R.write_heartbeat_file(path, 'block=0', now=clock.now + 1.0)
        watcher = R.FileHeartbeat(path, interval=1.0, timeout_seconds=60.0)
        first = watcher.poll(guard)
        self.assertFalse(first['expired'])
        clock.advance(61.0)
        second = watcher.poll(guard)
        self.assertTrue(second['expired'])
        self.assertGreaterEqual(second['silence_s'], 60.0)
        self.assertIn('no heartbeat', second['errors'][-1])

    def test_watchdog_supervise_marks_monitor_failed_and_stops(self):
        """看门狗到点 → 判 ``monitor_failed`` 并停循环（不是继续无保护地跑）。"""
        tmp = fixture_dir('hb-watchdog-stop')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock)
        guard.started_at = clock()
        path = os.path.join(tmp, 'hb.txt')
        R.write_heartbeat_file(path, 'block=0', now=clock.now + 1.0)
        watcher = R.FileHeartbeat(path, interval=0.0, timeout_seconds=30.0)
        stop = threading.Event()
        # 用假时钟：每次 poll 前推进 40 s，第一次就该超窗退出。
        original_poll = watcher.poll

        def poll_and_advance(inner_guard):
            outcome = original_poll(inner_guard)
            clock.advance(40.0)
            return outcome

        watcher.poll = poll_and_advance
        summary = watcher.supervise(guard, stop)
        self.assertEqual(summary['status'], R.STATUS_MONITOR_FAILED)
        self.assertEqual(guard.violation_reason, R.REASON_MONITOR_FAILED)
        self.assertIn('no heartbeat', guard.violation)
    def test_watchdog_is_off_by_default(self):
        """默认不设看门狗：心跳只用来刷新，不主动判死（保守的一端）。"""
        tmp = fixture_dir('hb-watchdog-off')
        path = os.path.join(tmp, 'hb.txt')
        watcher = R.FileHeartbeat(path)
        self.assertIsNone(watcher.timeout_seconds)
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock)
        guard.started_at = clock()
        clock.advance(100000.0)
        self.assertFalse(watcher.poll(guard)['expired'])


# ---------------------------------------------------------------------------
# 5. 硬性终止线一条都没退化
# ---------------------------------------------------------------------------

class HardLimitStillEnforcedTests(unittest.TestCase):
    """上一轮花大力气修好的三条线，本模块**必须**仍然真正生效。

    全部用注入读数，**不真的耗尽内存、不真的杀进程**。
    """

    def test_rss_above_limit_triggers_resource_blocked(self):
        tmp = fixture_dir('rl-rss')
        readings = StaticReadings(rss=3 * _GIB + 1)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=3 * _GIB)
        reason = tick(guard, guard._clock)
        self.assertIsNotNone(reason)
        self.assertIn(R.REASON_RSS, reason)
        guard._on_violation(reason)
        self.assertEqual(guard.status(), R.STATUS_RESOURCE_BLOCKED)
        self.assertTrue(guard.terminated)

    def test_rss_exactly_at_limit_is_still_allowed(self):
        """边界保持严格 ``>``（与 resource_guard 一致，不得变成 ``>=``）。"""
        tmp = fixture_dir('rl-rss-edge')
        readings = StaticReadings(rss=3 * _GIB)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=3 * _GIB)
        self.assertIsNone(tick(guard, guard._clock))

    def test_rss_line_is_not_weakened_by_deadline_refreshes(self):
        """**关键**：刷新 deadline 推不动 RSS 线（刷新只影响墙钟）。"""
        tmp = fixture_dir('rl-rss-refresh')
        readings = StaticReadings(rss=4 * _GIB)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=3 * _GIB,
                           mode=R.BUDGET_MODE_NONE)
        for block in range(5):
            guard.refresh_deadline('block=%d' % block)
            reason = tick(guard, guard._clock)
            self.assertIsNotNone(
                reason, 'refresh_deadline must never mask an RSS breach')
            self.assertIn(R.REASON_RSS, reason)

    def test_resource_lines_outrank_the_wall_clock(self):
        """资源超限必须**先于**超时被报出，不能被一次刷新掩盖成超时。"""
        clock = FakeClock()
        tmp = fixture_dir('rl-order')
        readings = StaticReadings(rss=5 * _GIB, disk_free=1 * _GIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           rss_limit_bytes=3 * _GIB, disk_floor_bytes=2 * _GIB,
                           stage_budgets={'train': 1.0})
        guard.set_stage('train')
        clock.advance(100.0)          # 阶段预算也超了
        guard.refresh_deadline('block=0')
        clock.advance(100.0)
        reason = tick(guard, clock)
        self.assertIn(R.REASON_RSS, reason)
        self.assertNotIn(R.REASON_STAGE_TIMEOUT, reason)

    def test_disk_below_floor_triggers(self):
        tmp = fixture_dir('rl-disk')
        readings = StaticReadings(disk_free=1 * _GIB)
        guard = make_guard(tmp, readings=readings, disk_floor_bytes=2 * _GIB)
        reason = tick(guard, guard._clock)
        self.assertIsNotNone(reason)
        self.assertIn(R.REASON_DISK, reason)
        guard._on_violation(reason)
        self.assertEqual(guard.status(), R.STATUS_RESOURCE_BLOCKED)

    def test_disk_above_floor_is_fine(self):
        tmp = fixture_dir('rl-disk-ok')
        readings = StaticReadings(disk_free=36 * _GIB)
        guard = make_guard(tmp, readings=readings, disk_floor_bytes=2 * _GIB)
        self.assertIsNone(tick(guard, guard._clock))

    def test_disk_line_is_not_weakened_by_deadline_refreshes(self):
        tmp = fixture_dir('rl-disk-refresh')
        readings = StaticReadings(rss=8 * _MIB, disk_free=512 * _MIB)
        guard = make_guard(tmp, readings=readings, disk_floor_bytes=2 * _GIB,
                           mode=R.BUDGET_MODE_NONE)
        guard.refresh_deadline('block=0')
        reason = tick(guard, guard._clock)
        self.assertIn(R.REASON_DISK, reason)

    def test_mem_available_streak_shorter_than_30s_does_not_trigger(self):
        clock = FakeClock()
        tmp = fixture_dir('rl-mem-short')
        readings = StaticReadings(mem_available=64 * _MIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           mem_available_floor_bytes=256 * _MIB,
                           mem_floor_streak_seconds=30.0)
        self.assertIsNone(tick(guard, clock))
        clock.advance(29.0)
        self.assertIsNone(tick(guard, clock))
        self.assertIsNone(guard.violation)

    def test_mem_available_full_30s_streak_triggers(self):
        clock = FakeClock()
        tmp = fixture_dir('rl-mem-full')
        readings = StaticReadings(mem_available=64 * _MIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           mem_available_floor_bytes=256 * _MIB,
                           mem_floor_streak_seconds=30.0)
        tick(guard, clock)
        clock.advance(31.0)
        reason = tick(guard, clock)
        self.assertIsNotNone(reason)
        self.assertIn(R.REASON_MEM, reason)
        self.assertIn('30', reason)
        guard._on_violation(reason)
        self.assertEqual(guard.status(), R.STATUS_RESOURCE_BLOCKED)

    def test_mem_recovery_still_resets_the_streak(self):
        """恢复即清零重计时——这条语义必须逐字保持，不能被新代码碰坏。"""
        clock = FakeClock()
        tmp = fixture_dir('rl-mem-recover')
        readings = StaticReadings(mem_available=64 * _MIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           mem_available_floor_bytes=256 * _MIB,
                           mem_floor_streak_seconds=30.0)
        tick(guard, clock)
        clock.advance(20.0)
        self.assertIsNone(tick(guard, clock))

        readings.mem_available = 2048 * _MIB      # 恢复
        clock.advance(2.0)
        self.assertIsNone(tick(guard, clock))
        self.assertIsNone(guard.low_mem_since)
        self.assertEqual(guard.low_mem_recoveries, 1)

        readings.mem_available = 64 * _MIB        # 再次走低：从零计时
        clock.advance(2.0)
        self.assertIsNone(tick(guard, clock))
        clock.advance(20.0)                       # 新一轮 22 s < 30 s
        self.assertIsNone(tick(guard, clock))
        clock.advance(10.0)                       # 新一轮 32 s >= 30 s
        reason = tick(guard, clock)
        self.assertIsNotNone(reason)
        self.assertIn(R.REASON_MEM, reason)

    def test_all_three_hard_lines_are_still_configurable_and_defaulted(self):
        """三条线的默认值必须来自冻结协议，且能被显式调小用于测试。"""
        tmp = fixture_dir('rl-defaults')
        readings = StaticReadings(rss=2 * _MIB)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=_MIB)
        self.assertEqual(guard.rss_limit_bytes, _MIB)
        self.assertIn(R.REASON_RSS, tick(guard, guard._clock))
        default_guard = make_guard(tmp, readings=readings)
        self.assertEqual(default_guard.rss_limit_bytes, 3 * _GIB)
        self.assertEqual(default_guard.mem_available_floor_bytes, 256 * _MIB)
        self.assertEqual(default_guard.mem_floor_streak_seconds, 30.0)
        self.assertEqual(default_guard.disk_floor_bytes, 2 * _GIB)


# ---------------------------------------------------------------------------
# 6. 产物与摘要（调用方要照着这里读字段）
# ---------------------------------------------------------------------------

class ArtifactTests(unittest.TestCase):

    def test_resources_csv_keeps_the_shared_field_contract(self):
        tmp = fixture_dir('art-csv')
        csv_path = os.path.join(tmp, 'resources.csv')
        guard = make_guard(tmp, csv_path=csv_path)
        tick(guard, guard._clock)
        with io.open(csv_path, encoding='utf-8', newline='') as handle:
            header = next(iter(csv.reader(handle)))
        self.assertEqual(header, list(R.CSV_FIELDS))

    def test_status_json_is_written_with_the_new_fields(self):
        tmp = fixture_dir('art-status')
        status_path = os.path.join(tmp, 'guard_status.json')
        readings = StaticReadings(rss=2 * _MIB)
        guard = make_guard(tmp, status_path=status_path, readings=readings,
                           rss_limit_bytes=_MIB)
        guard._tick()
        self.assertTrue(os.path.isfile(status_path))
        with io.open(status_path, encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertEqual(payload['status'], R.STATUS_RESOURCE_BLOCKED)
        self.assertEqual(payload['violation_reason'], R.REASON_RSS)
        for key in ('budget_mode', 'refresh_count', 'refresh_log',
                    'unmeasured_window_seconds', 'unmeasured_window_source',
                    'sampled_max', 'limits'):
            self.assertIn(key, payload, 'missing summary field %r' % key)

    def test_summary_keeps_the_sampled_max_wording_discipline(self):
        """沿用用词纪律：叫"采样最大值"，绝不叫"精确连续峰值"。"""
        tmp = fixture_dir('art-wording')
        readings = StaticReadings(rss=130 * _MIB)
        guard = make_guard(tmp, readings=readings)
        tick(guard, guard._clock)
        stats = guard.sampled_max()
        self.assertEqual(stats['rss_sample_max_bytes'], 130 * _MIB)
        for key in stats:
            self.assertNotIn('exact', key.lower())
            self.assertNotIn('true_peak', key.lower())

# ---------------------------------------------------------------------------
# 7. Python 3.6 语法（VM 是 3.6.8，写错就白跑一轮全量）
# ---------------------------------------------------------------------------

class Python36SyntaxTests(unittest.TestCase):
    """本模块源码必须能被 Python 3.6 解析。

    这是**硬约束**：全量评测跑在 VM 的 Python 3.6.8 上，一个 f-string 就会让
    整轮 8 小时的运行在 import 阶段就死掉。判定用 AST（不看注释），
    与 ``test_itemcf_model_layer_review.py::test_python36_syntax_of_the_reviewed_modules``
    同一套写法。

    注意：注释/文档字符串里出现 "dataclasses"、"f-string" 这些**词**不算违规
    —— 它们只是声明"本模块不用"。这里检查的是真的没有用。
    """

    BANNED = ('f-string', 'walrus', 'annotation', '@dataclass')

    def _violations(self, tree, name):
        banned = []
        for node in ast.walk(tree):
            if hasattr(ast, 'NamedExpr') and isinstance(node, ast.NamedExpr):
                banned.append('%s: walrus at line %d' % (name, node.lineno))
            if isinstance(node, ast.JoinedStr):
                banned.append('%s: f-string at line %d' % (name, node.lineno))
            if isinstance(node, ast.AnnAssign):
                banned.append('%s: annotation at line %d' % (name, node.lineno))
            if isinstance(node, ast.ClassDef) and any(
                    'dataclass' in ast.dump(d) for d in node.decorator_list):
                banned.append('%s: @dataclass at line %d' % (name, node.lineno))
            if isinstance(node, ast.FormattedValue):
                banned.append('%s: f-string value at line %d'
                              % (name, node.lineno))
        return banned

    def test_rate_limited_guard_source_has_no_post_36_syntax(self):
        import ast as _ast
        tree = _ast.parse(module_source())
        violations = self._violations(tree, 'rate_limited_guard.py')
        self.assertEqual(violations, [])

    def test_this_test_module_is_also_py36_clean(self):
        """测试自己也得能在 3.6 上跑，否则 VM 上连用例都起不来。"""
        import ast as _ast
        with io.open(os.path.abspath(__file__), encoding='utf-8') as handle:
            tree = _ast.parse(handle.read())
        self.assertEqual(self._violations(tree, 'test_itemcf_rate_limited_guard.py'),
                         [])

    def test_guard_does_not_use_forbidden_runtime_apis(self):
        """源码里不许出现 3.6 之后的**运行时** API（不是语法，AST 抓不到）。

        只看**代码**：先剥掉 docstring 与注释。``capture_output`` 正是被
        文档声明为"本模块不用"的名字之一，如果连文档里的声明都算违规，
        这条用例会自己把自己判死。
        """
        code_only = _strip_comments_and_docstrings(module_source())
        for forbidden in ('capture_output', '__future__',
                          'functools.cached_property'):
            self.assertNotIn(forbidden, code_only,
                             'py36-incompatible API used: %r' % forbidden)
        # 正向证据：``subprocess.Popen`` 的用法是 3.6 就有的。
        self.assertIn('subprocess.Popen', code_only)

    def test_string_formatting_uses_percent_or_format(self):
        """正向证据：格式化确实走 ``%``（老写法），不是 f-string。

        f-string 的等价格式化就是 ``%``。这里只要求 ``%`` 出现在**可执行代码**
        里；不要求同时存在 ``.format(``——按需选一种老写法即可，
        硬要求两种反而是在测风格而不是测兼容性。
        """
        code_only = _strip_comments_and_docstrings(module_source())
        self.assertIn('%s', code_only)
        self.assertGreaterEqual(code_only.count('%'), 20,
                                'percent-formatting must be pervasive in real code')


# ---------------------------------------------------------------------------
# 8. 端到端：RefreshableGuardedRun 用真进程
# ---------------------------------------------------------------------------

class GuardedRunApiTests(unittest.TestCase):
    """``RefreshableGuardedRun`` 必须有与 ``GuardedRun`` 相近的 API 与**同一套状态值**。

    **平台纪律**：终止走 ``os.killpg``（进程组），Windows 上没有这个函数
    （``resource_guard._group_alive`` 因此退化成"判死为 False"）。
    所以"必须真的把 worker 杀掉"的用例在 POSIX 才跑，其余（API 形状、成功路径、
    退出码映射）在所有平台都跑。这与既有 ``test_itemcf_resource_guard.py``
    的处理完全一致。
    """

    def test_clean_worker_finishes_successfully(self):
        tmp = fixture_dir('run-clean')
        runner = R.RefreshableGuardedRun(
            [sys.executable, '-c', 'import time; time.sleep(0.5)'],
            csv_path=os.path.join(tmp, 'resources.csv'),
            status_path=os.path.join(tmp, 'guard_status.json'),
            interval=0.15, budget_mode=R.BUDGET_MODE_NONE,
            total_budget_seconds=30.0)
        result = runner.run()
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['status'], R.STATUS_SUCCESS)
        self.assertFalse(result['terminated'])
        self.assertIsNone(result['reason'])
        self.assertIsNotNone(runner.process.poll(), 'child must be reaped')
        # API 完整性：start/run/result/status/summary 都在。
        self.assertEqual(runner.status(), R.STATUS_SUCCESS)
        self.assertIn('budget_mode', runner.summary())
        self.assertEqual(runner.result(0)['refresh_count'],
                         runner.guard.refresh_count)

    def test_failing_worker_reports_correctness_failed(self):
        tmp = fixture_dir('run-fail')
        runner = R.RefreshableGuardedRun(
            [sys.executable, '-c', 'raise SystemExit(7)'],
            csv_path=os.path.join(tmp, 'resources.csv'), interval=0.15,
            budget_mode=R.BUDGET_MODE_NONE)
        result = runner.run()
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['status'], R.STATUS_CORRECTNESS_FAILED)
        self.assertIn('7', result['reason'])

    @unittest.skipIf(os.name != 'posix', 'process-group termination is POSIX-only')
    def test_run_timeout_is_still_enforced_when_asked(self):
        """即使放宽了语义，**显式要一个超时就一定有效**（父类的性质保留）。"""
        tmp = fixture_dir('run-timeout')
        script = os.path.join(tmp, 'never.py')
        with open(script, 'w') as handle:
            handle.write('import time\nwhile True:\n    time.sleep(1)\n')
        runner = R.RefreshableGuardedRun(
            [sys.executable, script],
            csv_path=os.path.join(tmp, 'resources.csv'),
            interval=0.15, grace_seconds=1.0,
            budget_mode=R.BUDGET_MODE_NONE)
        started = time.time()
        result = runner.run(timeout_seconds=1.5)
        self.assertLess(time.time() - started, 30.0,
                        'a 1.5 s budget must not let the worker run for minutes')
        self.assertNotEqual(result['status'], R.STATUS_SUCCESS)
        self.assertEqual(result['status'], R.STATUS_RESOURCE_FAILED)
        self.assertTrue(result['terminated'])
        self.assertIsNotNone(runner.process.poll())

    @unittest.skipIf(os.name != 'posix', 'process-group termination is POSIX-only')
    def test_hard_timeout_is_not_pushed_by_refreshes(self):
        """``hard_timeout_seconds`` 是**整轮**绝对上限，刷新推不动它。"""
        tmp = fixture_dir('run-hard')
        script = os.path.join(tmp, 'never2.py')
        with open(script, 'w') as handle:
            handle.write('import time\nwhile True:\n    time.sleep(1)\n')
        runner = R.RefreshableGuardedRun(
            [sys.executable, script],
            csv_path=os.path.join(tmp, 'resources.csv'),
            interval=0.15, grace_seconds=1.0,
            budget_mode=R.BUDGET_MODE_NONE)
        refreshed = [0]

        def keep_refreshing(guard):
            # 编排层每轮都把 deadline 往前推——按"可刷新"的口径这永远不超时。
            guard.refresh_deadline('keepalive')
            refreshed[0] += 1

        started = time.time()
        result = runner.run(poll_seconds=0.1, on_stage=keep_refreshing,
                            hard_timeout_seconds=2.0)
        self.assertLess(time.time() - started, 30.0,
                        'hard_timeout_seconds must terminate the worker')
        self.assertEqual(result['status'], R.STATUS_RESOURCE_FAILED)
        self.assertIn('hard wall-clock', result['reason'])
        self.assertTrue(result['terminated'])
        self.assertGreaterEqual(refreshed[0], 1)

    def test_worker_can_refresh_the_guard_by_writing_a_heartbeat_file(self):
        """跨进程心跳：worker 写文件 → 监督侧刷新 → 单块预算不误杀整轮。"""
        tmp = fixture_dir('run-heartbeat')
        hb_path = os.path.join(tmp, 'guard_heartbeat.txt')
        worker = os.path.join(tmp, 'beating_worker.py')
        # worker 每 0.4 s 写一次心跳，共 6 次（≈2.4 s），远大于 1 s 的"单块"预算。
        with open(worker, 'w') as handle:
            handle.write(
                'import sys, time\n'
                'sys.path.insert(0, %r)\n'
                'from experiments.itemcf.rate_limited_guard import '
                'HeartbeatWriter\n'
                'beat = HeartbeatWriter(%r)\n'
                'for i in range(6):\n'
                '    time.sleep(0.4)\n'
                '    beat.beat("block=%%d" %% i)\n'
                'sys.exit(0)\n' % (PROJECT, hb_path))
        runner = R.RefreshableGuardedRun(
            [sys.executable, worker],
            csv_path=os.path.join(tmp, 'resources.csv'),
            interval=0.15, grace_seconds=2.0,
            budget_mode=R.BUDGET_MODE_STAGE,
            heartbeat_path=hb_path, heartbeat_interval=0.2)
        result = runner.run(timeout_seconds=1.0, stage='train')
        self.assertEqual(result['status'], R.STATUS_SUCCESS,
                         'heartbeats must keep a long stage alive; got %r'
                         % (result['reason'],))
        self.assertFalse(result['terminated'])
        # worker 写了 6 次心跳、跑满 ≈2.4 s，远超过单块 1 s 的预算。
        # 它没有被杀，就证明刷新语义生效；刷新次数必然 >1（set_stage 那次之外
        # 至少还有心跳刷新）。监督线程按 0.2 s 轮询文件，未必抓到每一次中间态，
        # 所以这里只断言"心跳确实被观察到"，不钉死等于 6。
        self.assertGreater(result['refresh_count'], 1,
                           'expected at least one heartbeat-driven refresh')
        self.assertGreaterEqual(result['heartbeat_refreshes'], 1)

    def test_progress_heartbeat_file_flows_into_the_summary(self):
        """刷新次数必须出现在产物里，否则事后无法解释"这次为什么没被杀"。"""
        tmp = fixture_dir('run-hb-summary')
        hb_path = os.path.join(tmp, 'hb.txt')
        status_path = os.path.join(tmp, 'guard_status.json')
        worker = os.path.join(tmp, 'beating_worker2.py')
        with open(worker, 'w') as handle:
            handle.write(
                'import sys, time\n'
                'sys.path.insert(0, %r)\n'
                'from experiments.itemcf.rate_limited_guard import '
                'HeartbeatWriter\n'
                'beat = HeartbeatWriter(%r)\n'
                'for i in range(4):\n'
                '    beat.beat("b%%d" %% i)\n'
                '    time.sleep(0.3)\n'
                'sys.exit(0)\n' % (PROJECT, hb_path))
        runner = R.RefreshableGuardedRun(
            [sys.executable, worker],
            csv_path=os.path.join(tmp, 'resources.csv'),
            status_path=status_path, interval=0.15, grace_seconds=2.0,
            budget_mode=R.BUDGET_MODE_NONE, heartbeat_path=hb_path,
            heartbeat_interval=0.15)
        runner.run()
        with io.open(status_path, encoding='utf-8') as handle:
            payload = json.load(handle)
        self.assertEqual(payload['status'], R.STATUS_SUCCESS)
        self.assertGreater(payload['refresh_count'], 1,
                           'heartbeat refreshes must reach the status file')
        self.assertEqual(payload['budget_mode'], R.BUDGET_MODE_NONE)
        self.assertTrue(os.path.isfile(os.path.join(tmp, 'resources.csv')))


if __name__ == '__main__':
    unittest.main()
