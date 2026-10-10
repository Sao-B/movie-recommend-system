# -*- coding: utf-8 -*-
"""独立资源监督（``experiments/itemcf/resource_guard.py``）的契约测试。

本文件的重点是把上一轮的失效模式**变成回归测试**：

上一轮 ``confirm.py`` 的采样由主计算流程在检查点手动调用，构图是纯 Python
循环且不回调监控，实测 ``resources.csv`` 只有 8 个采样点、其中一段
**492.5 秒缺口正好覆盖 493 秒的整个构图期**，3 GiB 终止线在那段时间内
根本无法触发。因此这里第一个、也是最关键的用例是
``BlockingWorkerTests.test_blocking_worker_is_sampled_and_terminated``：
一个**启动后自己阻塞、绝不回调、绝不再说一个字**的 worker，必须仍然被
按 2 秒（测试里调成更短）节奏采样到，并按短测试超时被终止、且留下采样行。

其余用例用**可注入的资源读数**（``rss_reader`` / ``mem_reader`` /
``disk_reader``）验证阈值语义——不读真实 ``/proc``，也**绝不真的耗尽机器
内存**（用的都是 1 MiB 级的小阈值 + 假读数）。

覆盖清单：

* 阻塞且不回调的 worker 仍被定时采样并按时终止（上轮失效场景）；
* 采样节奏与"监督独立于 worker"（worker 阻塞期间采样数持续增长）；
* RSS 求和是**进程树**、去重、含子进程；
* MemAvailable **连续 30 秒**条件：不足时长不触发、足量触发、恢复后**重新计时**；
* 剩余磁盘下限；
* 阶段超时；
* 终止只打本轮进程组——**另一个无关进程不受影响**；
* 监督失败（读数一直拿不到）→ ``monitor_failed`` 且停止工作；
* 正常退出：exit_code 0 / ``success``、子进程被回收；
* 退出码、失败原因、``resources.csv`` 行数与字段、状态只取明确值；
* 用词纪律：统计字段叫"采样最大值"，不叫"精确连续峰值"。

运行：
    backend/venv/Scripts/python.exe -m unittest tests.test_itemcf_resource_guard -v
"""
import csv
import json
import os
import subprocess
import sys
import threading
import time
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

from experiments.itemcf import resource_guard as G  # noqa: E402

FIXTURE_ROOT = os.path.join(PROJECT, 'tests', '_itemcf_guard_tmp')
_BLOCKING_WORKER = os.path.join(FIXTURE_ROOT, 'blocking_worker.py')
_MIB = 1 << 20
_GIB = 1 << 30

_SEQ_LOCK = threading.Lock()
_SEQ = [0]


def fixture_dir(prefix):
    """建唯一临时目录（沙箱下不做清理，与既有 itemcf 测试同策略）。

    ``time.time()*1000`` 在同一测试进程里会连续撞名（Windows 时钟分辨率约
    15 ms，一个用例跑完不到 1 ms），所以再叠一个单调序号并**重试**，
    绝不返回一个已被使用的目录——复用目录会让"落盘产物"读到上一轮遗留文件。
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


def write_blocking_worker():
    """写一个**阻塞且不回调监控**的 worker。

    这正是上一轮失效场景的复刻：它既不采样、也不响应任何"上报进度"的
    约定，只是纯 Python 死循环（甚至 sleep 也不调——用一个忙等循环，
    确保它连"至少让出 GIL"都不做）。唯一的正常退出路径是被信号杀掉。
    """
    os.makedirs(FIXTURE_ROOT, exist_ok=True)
    with open(_BLOCKING_WORKER, 'w') as handle:
        handle.write(
            'import sys, time\n'
            '# 忙等：不 sleep、不打印、不读环境变量、不与监控有任何交互。\n'
            'deadline = time.time() + 3600\n'
            'x = 0\n'
            'while time.time() < deadline:\n'
            '    x = (x * 31 + 7) % 1000003\n'
            'sys.exit(0)\n')
    return _BLOCKING_WORKER


def read_csv_rows(path):
    with open(path, newline='') as handle:
        return list(csv.DictReader(handle))


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class StaticReadings(object):
    """可注入的读数：把 RSS / 内存 / 磁盘都变成可控的常数序列。"""

    def __init__(self, rss=None, mem_available=None, disk_free=None,
                 swap_total=None, swap_free=None, pids=None, rss_raises=False):
        self.rss = rss
        self.mem_available = mem_available
        self.disk_free = disk_free
        self.swap_total = swap_total
        self.swap_free = swap_free
        self.pids = list(pids) if pids else None
        self.rss_raises = rss_raises
        self.rss_calls = 0
        self.mem_calls = 0

    def rss_reader(self, root_pid):
        self.rss_calls += 1
        if self.rss_raises:
            raise RuntimeError('injected rss failure')
        pids = self.pids if self.pids is not None else [root_pid]
        return self.rss, pids

    def mem_reader(self):
        self.mem_calls += 1
        info = {}
        if self.mem_available is not None:
            info['MemAvailable'] = self.mem_available
        if self.swap_total is not None:
            info['SwapTotal'] = self.swap_total
        if self.swap_free is not None:
            info['SwapFree'] = self.swap_free
        return info

    def disk_reader(self):
        return self.disk_free


class FakeClock(object):
    """可控单调时钟：不真等 30 秒就能验证"连续 30 秒"条件。"""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += float(seconds)


def make_guard(tmp, clock=None, **kwargs):
    """构造一个带可控读数的 guard（不启动线程，便于逐步 tick）。"""
    readings = kwargs.pop('readings', None) or StaticReadings(
        rss=64 * _MIB, mem_available=2048 * _MIB, disk_free=20 * _GIB)
    guard = G.ResourceGuard(
        root_pid=os.getpid(),
        csv_path=kwargs.pop('csv_path', os.path.join(tmp, 'resources.csv')),
        status_path=kwargs.pop('status_path', None),
        rss_reader=readings.rss_reader,
        mem_reader=readings.mem_reader,
        disk_reader=readings.disk_reader,
        clock=clock or time.time,
        **kwargs)
    guard._readings = readings
    return guard


# ---------------------------------------------------------------------------
# 1. 关键回归：阻塞且不回调的 worker 仍被采样并终止（上轮失效场景）
# ---------------------------------------------------------------------------

@unittest.skipIf(os.name != 'posix', 'process-group termination is POSIX-only')
class BlockingWorkerTests(unittest.TestCase):
    """上轮 493 秒零采样的直接回归测试。"""

    def test_blocking_worker_is_sampled_and_terminated(self):
        """worker 完全阻塞、绝无回调，仍必须：被采样 + 被按时终止 + 留有 CSV 行。"""
        worker = write_blocking_worker()
        tmp = fixture_dir('blocking')
        csv_path = os.path.join(tmp, 'resources.csv')
        runner = G.GuardedRun(
            [sys.executable, worker],
            csv_path=csv_path,
            interval=0.2,                 # 测试里缩短周期，语义与 2 s 相同
            stage_budgets={'train': 1.5},  # 短测试阶段预算
            grace_seconds=3.0)
        started = time.time()
        result = runner.run()
        elapsed = time.time() - started

        # 被终止，且不是靠 worker 自己退出。
        self.assertEqual(result['status'], G.STATUS_RESOURCE_FAILED)
        self.assertTrue(result['terminated'])
        self.assertIn(G.REASON_STAGE_TIMEOUT, result['reason'])
        self.assertLess(elapsed, 25.0, 'termination must be prompt')
        self.assertFalse(_pid_alive(runner.process.pid), 'worker must be dead')

        # 关键断言：worker 阻塞期间**照样**产生了多个采样点。
        rows = read_csv_rows(csv_path)
        self.assertGreaterEqual(
            len(rows), 4,
            'blocking worker must still be sampled repeatedly, got %d rows'
            % len(rows))
        self.assertEqual(result['sampled_max']['sample_count'], len(rows))
        # 时间跨度覆盖了 worker 的真实存活期（证明不是只在开头采了几点）。
        spans = [float(r['elapsed_s']) for r in rows]
        self.assertGreater(max(spans), 0.5)

    def test_worker_with_no_monitor_callbacks_still_produces_pid_tree_rows(self):
        """采样行必须带上被测进程树的 PID（根 + 后代），而不是空值。"""
        worker = write_blocking_worker()
        tmp = fixture_dir('blocking-pids')
        csv_path = os.path.join(tmp, 'resources.csv')
        runner = G.GuardedRun([sys.executable, worker], csv_path=csv_path,
                              interval=0.2, stage_budgets={'train': 1.0},
                              grace_seconds=3.0)
        result = runner.run()
        rows = read_csv_rows(csv_path)
        self.assertTrue(rows)
        pids = set()
        for row in rows:
            if row['pids']:
                pids.update(int(p) for p in row['pids'].split(',') if p)
        self.assertIn(runner.process.pid, pids,
                      'root worker pid must appear in sampled pid tree')
        self.assertGreaterEqual(result['sampled_max']['max_pid_count'], 1)


@unittest.skipIf(os.name != 'posix', 'process-group termination is POSIX-only')
class BudgetMustActuallyApplyTests(unittest.TestCase):
    """回归：**给了预算就必须真的生效**。

    这是 VM 上实测到的真实缺陷（修之前）：

    ``GuardedRun`` 把 ``stage_budgets={'train': 1.5}`` 传给了 guard，但
    guard 的当前阶段仍是默认的 ``prepare``，而超时只在**当前阶段**上判定。
    于是 1.5 秒的阶段预算根本没有生效的时机，那个"阻塞且不回调"的 worker
    **跑了 36 分钟没被终止**，``resources.csv`` 攒到 10625 行。故障表现与
    上一轮的 492 秒零采样相反，但本质相同：**保护看起来配上了，实际没生效**。

    所以这里把三件事都钉住：阶段确实被切到位、预算确实被判、总预算兜底有效。
    """

    def _stubborn_runner(self, tmp, **kwargs):
        """一个永不退出、且忽略 TERM 的 worker（需要 KILL 才能收场）。"""
        script = os.path.join(tmp, 'never_exits.py')
        with open(script, 'w') as handle:
            handle.write('import time\n'
                         'while True:\n'
                         '    time.sleep(1)\n')
        return G.GuardedRun([sys.executable, script],
                            csv_path=os.path.join(tmp, 'resources.csv'),
                            interval=0.2, grace_seconds=1.0, **kwargs)

    def test_timeout_seconds_sets_the_stage_the_guard_actually_judges(self):
        """timeout_seconds 必须落到 guard 的**当前阶段**上（不是空转）。"""
        tmp = fixture_dir('budget-stage')
        runner = self._stubborn_runner(tmp)
        try:
            runner.start()
            # 只跑到"预算已配置、阶段已切"的时点，不等 worker 结束。
            self.assertFalse(runner.process.poll())
            runner.guard.stage_budgets = dict(runner.stage_budgets)
            runner.guard.set_stage('train')
            self.assertEqual(runner.guard.stage, 'train')
            self.assertIn('train', runner.guard.stage_budgets)
        finally:
            G.terminate_process_group(runner.pgid, 1.0)
            runner.process.wait()
            runner.guard.stop()

    def test_run_with_timeout_terminates_a_worker_that_never_exits(self):
        """端到端：worker 永不退出 + timeout_seconds=2 → 必须被终止并返回。"""
        tmp = fixture_dir('budget-e2e')
        runner = self._stubborn_runner(tmp)
        started = time.time()
        result = runner.run(timeout_seconds=2.0)
        elapsed = time.time() - started
        self.assertLess(elapsed, 30.0,
                        'a 2 s budget must not let the worker run for minutes '
                        '(this is the bug that produced 10625 rows on the VM)')
        self.assertNotEqual(result['status'], G.STATUS_SUCCESS)
        self.assertTrue(result['terminated'], 'worker must be terminated')
        self.assertIn('budget', result['reason'])
        self.assertIsNotNone(runner.process.poll(), 'child must be reaped')

    def test_stage_is_advanced_by_run_not_left_at_default(self):
        """run() 之后 guard.stage 不能还停在默认的 prepare。"""
        tmp = fixture_dir('budget-default-stage')
        runner = self._stubborn_runner(tmp)
        try:
            result = runner.run(timeout_seconds=1.5)
            self.assertEqual(result['status'], G.STATUS_RESOURCE_FAILED)
            self.assertEqual(runner.guard.stage, 'train')
            self.assertIn(G.REASON_STAGE_TIMEOUT, result['reason'])
        finally:
            if runner.process.poll() is None:
                G.terminate_process_group(runner.pgid, 1.0)
                runner.process.wait()

    def test_stage_plan_first_entry_becomes_the_active_stage(self):
        tmp = fixture_dir('budget-plan')
        runner = self._stubborn_runner(tmp)
        try:
            result = runner.run(stage_plan=[('precheck', 1.5)])
            self.assertEqual(runner.guard.stage, 'precheck')
            self.assertEqual(result['status'], G.STATUS_RESOURCE_FAILED)
            self.assertIn('precheck', result['reason'])
        finally:
            if runner.process.poll() is None:
                G.terminate_process_group(runner.pgid, 1.0)
                runner.process.wait()

    def test_total_budget_stops_a_worker_even_without_stage_budget(self):
        """总预算兜底：没有任何阶段预算时也必须能停。"""
        tmp = fixture_dir('budget-total')
        runner = self._stubborn_runner(tmp)
        try:
            result = runner.run(timeout_seconds=1.5)
            self.assertNotEqual(result['status'], G.STATUS_SUCCESS)
            self.assertTrue(result['terminated'])
        finally:
            if runner.process.poll() is None:
                G.terminate_process_group(runner.pgid, 1.0)
                runner.process.wait()

    def test_guard_created_without_budget_still_judges_current_stage(self):
        """直接构造 guard（不走 GuardedRun）时，set_stage + 预算也要生效。"""
        tmp = fixture_dir('budget-direct')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock)
        guard.stage_budgets = {'prepare': 5.0}
        guard.set_stage('prepare')
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        clock.advance(6.0)
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIsNotNone(reason)
        self.assertIn(G.REASON_STAGE_TIMEOUT, reason)


def _pid_alive(pid):
    if os.name == 'posix':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    # Windows 退路（本用例在 POSIX 才跑，这里只为可读性）。
    output = subprocess.Popen(['tasklist', '/FI', 'PID eq %d' % pid],
                              stdout=subprocess.PIPE).communicate()[0]
    return str(pid) in output.decode('utf-8', 'replace')


# ---------------------------------------------------------------------------
# 2. 采样节奏 / 监督独立于 worker
# ---------------------------------------------------------------------------

class SamplingCadenceTests(unittest.TestCase):

    def test_samples_accumulate_while_worker_is_blocked(self):
        """监督线程在 worker 阻塞期间持续采样（不等待 worker）。"""
        worker = write_blocking_worker()
        tmp = fixture_dir('cadence')
        if os.name != 'posix':
            self.skipTest('needs a real child process')
        process = subprocess.Popen([sys.executable, worker],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL,
                                   start_new_session=True)
        csv_path = os.path.join(tmp, 'resources.csv')
        guard = G.ResourceGuard(process.pid, csv_path=csv_path, interval=0.15,
                                max_unmeasured_seconds=30.0)
        guard.start()
        try:
            self.assertTrue(
                wait_for(lambda: len(guard.rows) >= 4, timeout=10.0),
                'expected repeated samples while the worker was blocked')
            counts_a = len(guard.rows)
            time.sleep(0.5)
            counts_b = len(guard.rows)
            self.assertGreater(counts_b, counts_a,
                               'sampling must continue over time')
        finally:
            G.terminate_process_group(os.getpgid(process.pid), 2.0)
            process.wait()
            guard.stop()
        rows = read_csv_rows(csv_path)
        self.assertGreaterEqual(len(rows), 4)

    def test_csv_is_flushed_continuously_not_dumped_at_the_end(self):
        """每个采样点都落盘：读文件内容就能看到已写入的行（不必等结束）。"""
        tmp = fixture_dir('flush')
        guard = make_guard(tmp, interval=0.05)
        guard.start()
        try:
            self.assertTrue(wait_for(lambda: len(guard.rows) >= 3, timeout=5.0))
            # 不 stop()，直接读盘——上轮就是"最后才 dump 一次"。
            rows = read_csv_rows(guard.csv_path)
        finally:
            guard.stop()
        self.assertGreaterEqual(len(rows), 3,
                                'rows must be on disk before the guard stops')

    def test_interval_defaults_to_two_seconds(self):
        """计划书要求 2 秒采样；默认值必须是 2.0。"""
        self.assertEqual(G.RESOURCE_POLL_SECONDS, 2.0)
        tmp = fixture_dir('interval-default')
        guard = make_guard(tmp)
        self.assertEqual(guard.interval, 2.0)


# ---------------------------------------------------------------------------
# 3. 进程树 RSS 汇总
# ---------------------------------------------------------------------------

class ProcessTreeTests(unittest.TestCase):

    def test_tree_rss_sums_root_and_descendants(self):
        """去重 PID 后**求和**：根 100 MiB + 两个子进程 = 总量。"""
        rss_map = {10: 100 * _MIB, 11: 40 * _MIB, 12: 25 * _MIB, 99: 9999 * _MIB}
        fake_tree = {10: [10, 11, 12]}
        original = G.process_tree_pids
        G.process_tree_pids = lambda root: list(fake_tree[int(root)])
        try:
            total, pids = G.process_tree_rss_bytes(10, rss_map)
        finally:
            G.process_tree_pids = original
        self.assertEqual(total, 165 * _MIB)
        self.assertEqual(sorted(pids), [10, 11, 12])
        # 无关进程（99）绝不能被算进来。
        self.assertNotIn(99, pids)

    def test_duplicate_pids_are_counted_once(self):
        """同一 PID 在树里出现多次只累加一次（去重后求和）。"""
        rss_map = {10: 50 * _MIB}
        original = G.process_tree_pids
        G.process_tree_pids = lambda root: [10, 10, 10]
        try:
            total, pids = G.process_tree_rss_bytes(10, rss_map)
        finally:
            G.process_tree_pids = original
        self.assertEqual(total, 50 * _MIB, 'RSS must not be double-counted')
        self.assertEqual(pids, [10], 'reported pid list must be deduplicated')
        self.assertEqual(len(pids), len(set(pids)))

    @unittest.skipIf(not os.path.isdir('/proc'), '/proc is Linux-only')
    def test_real_proc_tree_includes_a_spawned_child(self):
        """真实 /proc 路径：父 + 派生的子进程都要被枚举到。"""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(10)'])
        try:
            pids = G.process_tree_pids(os.getpid())
            self.assertIn(os.getpid(), pids)
            self.assertIn(child.pid, pids)
            total, counted = G.process_tree_rss_bytes(os.getpid())
            self.assertIsNotNone(total)
            self.assertGreater(total, 0)
            self.assertIn(child.pid, counted)
        finally:
            child.kill()
            child.wait()

    def test_unreadable_tree_returns_none_not_zero(self):
        """一个 PID 都读不到 → None（区分"真的 0"与"没采到"）。"""
        original = G.process_tree_pids
        G.process_tree_pids = lambda root: [12345]
        try:
            total, pids = G.process_tree_rss_bytes(12345, {})
        finally:
            G.process_tree_pids = original
        self.assertIsNone(total)
        self.assertEqual(pids, [])


# ---------------------------------------------------------------------------
# 4. MemAvailable 连续条件 / 恢复后重计时
# ---------------------------------------------------------------------------

class MemoryStreakTests(unittest.TestCase):
    """用注入读数 + 假时钟验证 30 秒**连续**语义，不真耗尽内存。"""

    def _guard(self, low_mib, streak=30.0, floor_mib=256, step=2.0):
        tmp = fixture_dir('mem')
        clock = FakeClock()
        readings = StaticReadings(rss=64 * _MIB,
                                  mem_available=low_mib * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           mem_available_floor_bytes=floor_mib * _MIB,
                           mem_floor_streak_seconds=streak, interval=step)
        return guard, clock

    def test_low_memory_shorter_than_streak_does_not_trigger(self):
        guard, clock = self._guard(low_mib=64)
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        clock.advance(10.0)
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        clock.advance(19.0)  # 累计 29 s < 30 s
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        self.assertIsNone(guard.violation)

    def test_low_memory_for_full_streak_triggers(self):
        guard, clock = self._guard(low_mib=64)
        guard._evaluate(guard._collect(clock()), clock())
        clock.advance(31.0)
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIsNotNone(reason)
        self.assertIn(G.REASON_MEM, reason)
        self.assertIn('30', reason)  # 报出连续时长/阈值

    def test_recovery_resets_the_streak_timer(self):
        """中间恢复一次 → 之前的累计必须清零，不能接续计算。"""
        guard, clock = self._guard(low_mib=64)
        guard._evaluate(guard._collect(clock()), clock())
        clock.advance(20.0)
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))

        # 恢复：MemAvailable 回到阈值以上。
        guard._readings.mem_available = 2048 * _MIB
        clock.advance(2.0)
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        self.assertIsNone(guard.low_mem_since)
        self.assertEqual(guard.low_mem_recoveries, 1)

        # 再次走低：必须从零开始计时（20 s + 2 s 的旧累计不得残留）。
        guard._readings.mem_available = 64 * _MIB
        clock.advance(2.0)
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        clock.advance(20.0)  # 新一轮才 22 s < 30 s
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()),
                          'recovered streak must restart from zero')
        clock.advance(10.0)  # 新一轮 32 s >= 30 s
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIsNotNone(reason)
        self.assertIn(G.REASON_MEM, reason)

    def test_memory_above_floor_never_triggers(self):
        guard, clock = self._guard(low_mib=512)
        for _ in range(40):
            clock.advance(2.0)
            self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        self.assertIsNone(guard.violation)

    def test_swap_is_recorded(self):
        tmp = fixture_dir('swap')
        readings = StaticReadings(rss=8 * _MIB, mem_available=1024 * _MIB,
                                  disk_free=20 * _GIB,
                                  swap_total=4095 * _MIB, swap_free=1000 * _MIB)
        guard = make_guard(tmp, readings=readings)
        row = guard._collect(time.time())
        self.assertEqual(row['swap_total_bytes'], 4095 * _MIB)
        self.assertEqual(row['swap_used_bytes'], 3095 * _MIB)


# ---------------------------------------------------------------------------
# 5. RSS / 磁盘 / 阶段超时
# ---------------------------------------------------------------------------

class ThresholdTests(unittest.TestCase):

    def test_rss_above_limit_triggers_and_names_the_reason(self):
        tmp = fixture_dir('rss')
        readings = StaticReadings(rss=3 * _GIB + 1, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=3 * _GIB)
        clock = time.time
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIsNotNone(reason)
        self.assertIn(G.REASON_RSS, reason)
        self.assertIn('GiB', reason)

    def test_rss_exactly_at_limit_is_allowed(self):
        """边界：等于上限不算超（规则是严格 `>`）。"""
        tmp = fixture_dir('rss-edge')
        readings = StaticReadings(rss=3 * _GIB, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=3 * _GIB)
        clock = time.time
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))

    def test_small_threshold_used_in_tests_not_real_memory(self):
        """用小阈值（1 MiB）证明逻辑，不动真内存。"""
        tmp = fixture_dir('small-rss')
        readings = StaticReadings(rss=2 * _MIB, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, readings=readings, rss_limit_bytes=_MIB)
        clock = time.time
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIn(G.REASON_RSS, reason)

    def test_disk_below_floor_triggers(self):
        tmp = fixture_dir('disk')
        readings = StaticReadings(rss=8 * _MIB, mem_available=2048 * _MIB,
                                  disk_free=1 * _GIB)
        guard = make_guard(tmp, readings=readings, disk_floor_bytes=2 * _GIB)
        clock = time.time
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIsNotNone(reason)
        self.assertIn(G.REASON_DISK, reason)

    def test_disk_above_floor_is_fine(self):
        tmp = fixture_dir('disk-ok')
        readings = StaticReadings(rss=8 * _MIB, mem_available=2048 * _MIB,
                                  disk_free=36 * _GIB)
        guard = make_guard(tmp, readings=readings, disk_floor_bytes=2 * _GIB)
        clock = time.time
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))

    def test_stage_timeout_triggers_with_fake_clock(self):
        tmp = fixture_dir('stage')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock, stage_budgets={'train': 10.0})
        guard.set_stage('train')
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        clock.advance(11.0)
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIsNotNone(reason)
        self.assertIn(G.REASON_STAGE_TIMEOUT, reason)
        self.assertIn('train', reason)

    def test_stage_switch_resets_stage_timer(self):
        """换阶段要重置该阶段计时，不能把上个阶段的时间算进来。"""
        tmp = fixture_dir('stage-switch')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock,
                           stage_budgets={'prepare': 10.0, 'train': 10.0})
        guard.set_stage('prepare')
        clock.advance(9.0)
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        guard.set_stage('train')
        clock.advance(9.0)   # prepare 的 9 s 不能计入 train
        self.assertIsNone(guard._evaluate(guard._collect(clock()), clock()))
        clock.advance(2.0)
        reason = guard._evaluate(guard._collect(clock()), clock())
        self.assertIn(G.REASON_STAGE_TIMEOUT, reason)

    def test_unknown_stage_is_rejected(self):
        tmp = fixture_dir('stage-unknown')
        guard = make_guard(tmp)
        with self.assertRaises(ValueError):
            guard.set_stage('nope')


# ---------------------------------------------------------------------------
# 6. 监控失败 → 停止工作（不是继续无保护运行）
# ---------------------------------------------------------------------------

class MonitorFailureTests(unittest.TestCase):

    def test_persistent_unreadable_rss_marks_monitor_failed(self):
        tmp = fixture_dir('mon-fail')
        clock = FakeClock()
        readings = StaticReadings(rss_raises=True, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           max_unmeasured_seconds=10.0)
        guard._tick()                      # 第一次尝试：采不到
        self.assertIsNone(guard.violation)
        clock.advance(12.0)
        guard._tick()
        self.assertIsNotNone(guard.violation)
        self.assertEqual(guard.violation_reason, G.REASON_MONITOR_FAILED)
        self.assertEqual(guard.status(), G.STATUS_MONITOR_FAILED)
        self.assertTrue(guard.terminated)

    def test_monitor_failure_stops_the_supervision_loop(self):
        tmp = fixture_dir('mon-stop')
        clock = FakeClock()
        readings = StaticReadings(rss_raises=True, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           max_unmeasured_seconds=5.0, interval=1.0)
        guard.started_at = clock()
        # 用假时钟推进：第一次 tick 采集失败，之后一直失败。
        guard._tick()
        clock.advance(6.0)
        self.assertFalse(guard._tick(), 'loop must stop on monitor failure')

    def test_out_of_band_memory_reading_none_is_unmeasured(self):
        """MemAvailable 读不到时不能当成 0（否则会误报内存耗尽）。"""
        tmp = fixture_dir('mem-none')
        readings = StaticReadings(rss=8 * _MIB, mem_available=None,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, readings=readings)
        clock = time.time
        row = guard._collect(clock())
        self.assertIsNone(row['mem_available_bytes'])
        self.assertIsNone(guard._evaluate(row, clock()))


# ---------------------------------------------------------------------------
# 7. 终止：只打本轮进程组，无关进程不受影响
# ---------------------------------------------------------------------------

@unittest.skipIf(os.name != 'posix', 'process-group termination is POSIX-only')
class TerminationScopeTests(unittest.TestCase):

    def test_termination_does_not_touch_an_unrelated_process(self):
        """无关的旁观进程必须在终止前后都活着（绝不误杀其他服务）。"""
        bystander = subprocess.Popen([sys.executable, '-c',
                                      'import time; time.sleep(60)'])
        worker = write_blocking_worker()
        tmp = fixture_dir('scope')
        runner = G.GuardedRun([sys.executable, worker],
                              csv_path=os.path.join(tmp, 'resources.csv'),
                              interval=0.2, stage_budgets={'train': 1.0},
                              grace_seconds=3.0)
        try:
            result = runner.run()
            self.assertTrue(result['terminated'])
            self.assertTrue(bystander.poll() is None,
                            'unrelated process must survive termination')
            self.assertFalse(_pid_alive(runner.process.pid))
        finally:
            bystander.kill()
            bystander.wait()

    def test_termination_targets_only_our_process_group(self):
        """终止用的是本轮 worker 自己的 pgid（独立会话）。"""
        worker = write_blocking_worker()
        tmp = fixture_dir('pgid')
        runner = G.GuardedRun([sys.executable, worker],
                              csv_path=os.path.join(tmp, 'resources.csv'),
                              interval=0.2, stage_budgets={'train': 1.0},
                              grace_seconds=3.0)
        result = runner.run()
        self.assertIsNotNone(result['pgid'])
        self.assertEqual(result['pgid'], runner.process.pid,
                         'start_new_session makes the worker its own group leader')
        self.assertNotEqual(result['pgid'], os.getpgrp(),
                            'must never be the supervisor group')
        self.assertGreaterEqual(result['sampled_max']['sample_count'], 2)

    def test_graceful_term_is_preferred_over_kill(self):
        """先 TERM：worker 自己处理掉 TERM 时不应升级到 KILL。"""
        script = os.path.join(FIXTURE_ROOT, 'term_worker.py')
        os.makedirs(FIXTURE_ROOT, exist_ok=True)
        with open(script, 'w') as handle:
            handle.write('import time\n'
                         'try:\n'
                         '    time.sleep(60)\n'
                         'except KeyboardInterrupt:\n'
                         '    pass\n')
        process = subprocess.Popen([sys.executable, script],
                                   start_new_session=True)
        try:
            time.sleep(0.4)
            outcome = G.terminate_process_group(os.getpgid(process.pid),
                                                grace_seconds=5.0)
            self.assertTrue(outcome['term_sent'])
            self.assertTrue(outcome['exited'])
            self.assertFalse(outcome['kill_sent'],
                             'TERM was enough; KILL must not be needed')
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_stubborn_worker_is_killed_after_grace(self):
        """TRM 无效时，grace 过后必须升级 KILL。"""
        script = os.path.join(FIXTURE_ROOT, 'stubborn_worker.py')
        os.makedirs(FIXTURE_ROOT, exist_ok=True)
        with open(script, 'w') as handle:
            handle.write('import signal, time\n'
                         'signal.signal(signal.SIGTERM, signal.SIG_IGN)\n'
                         'time.sleep(120)\n')
        process = subprocess.Popen([sys.executable, script],
                                   start_new_session=True)
        try:
            time.sleep(0.5)
            outcome = G.terminate_process_group(os.getpgid(process.pid),
                                                grace_seconds=1.0)
            self.assertTrue(outcome['term_sent'])
            self.assertTrue(outcome['kill_sent'])
            self.assertTrue(outcome['exited'])
            self.assertFalse(_pid_alive(process.pid))
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()

    def test_terminate_with_none_pgid_is_a_noop(self):
        outcome = G.terminate_process_group(None, grace_seconds=0.1)
        self.assertFalse(outcome['term_sent'])
        self.assertFalse(outcome['exited'])


# ---------------------------------------------------------------------------
# 8. 终态：正常退出、退出码、子进程清理、状态取值
# ---------------------------------------------------------------------------

class OutcomeTests(unittest.TestCase):

    def test_clean_worker_finishes_successfully(self):
        tmp = fixture_dir('clean')
        csv_path = os.path.join(tmp, 'resources.csv')
        runner = G.GuardedRun(
            [sys.executable, '-c', 'import time; time.sleep(0.6)'],
            csv_path=csv_path, status_path=os.path.join(tmp, 'run_status.json'),
            interval=0.15)
        result = runner.run()
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual(result['status'], G.STATUS_SUCCESS)
        self.assertIsNone(result['reason'])
        self.assertFalse(result['terminated'])
        self.assertIsNotNone(runner.process.poll(), 'child must be reaped')
        self.assertGreaterEqual(result['sampled_max']['sample_count'], 1)

    def test_failing_worker_reports_nonzero_exit_code(self):
        tmp = fixture_dir('fail')
        runner = G.GuardedRun([sys.executable, '-c', 'raise SystemExit(7)'],
                              csv_path=os.path.join(tmp, 'resources.csv'),
                              interval=0.15)
        result = runner.run()
        self.assertEqual(result['exit_code'], 7)
        self.assertEqual(result['status'], G.STATUS_CORRECTNESS_FAILED)
        self.assertIn('7', result['reason'])

    def test_status_values_are_from_the_agreed_vocabulary(self):
        allowed = set([G.STATUS_RUNNING, G.STATUS_SUCCESS,
                       G.STATUS_RESOURCE_BLOCKED, G.STATUS_RESOURCE_FAILED,
                       G.STATUS_CORRECTNESS_FAILED, G.STATUS_MONITOR_FAILED])
        self.assertEqual(allowed, set(['running', 'success', 'resource_blocked',
                                       'resource_failed', 'correctness_failed',
                                       'monitor_failed']))
        tmp = fixture_dir('vocab')
        guard = make_guard(tmp)
        self.assertEqual(guard.status(), G.STATUS_RUNNING)
        self.assertIn(guard.status(), allowed)

    def test_status_maps_hard_limits_to_blocked_and_timeout_to_failed(self):
        tmp = fixture_dir('status-map')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock)
        guard.violation = 'x'
        guard.violation_reason = G.REASON_RSS
        self.assertEqual(guard.status(), G.STATUS_RESOURCE_BLOCKED)
        guard.violation_reason = G.REASON_MEM
        self.assertEqual(guard.status(), G.STATUS_RESOURCE_BLOCKED)
        guard.violation_reason = G.REASON_DISK
        self.assertEqual(guard.status(), G.STATUS_RESOURCE_BLOCKED)
        guard.violation_reason = G.REASON_STAGE_TIMEOUT
        self.assertEqual(guard.status(), G.STATUS_RESOURCE_FAILED)
        guard.violation_reason = G.REASON_MONITOR_FAILED
        self.assertEqual(guard.status(), G.STATUS_MONITOR_FAILED)


# ---------------------------------------------------------------------------
# 9. 产物：resources.csv 字段 / run_status.json
# ---------------------------------------------------------------------------

class ArtifactTests(unittest.TestCase):

    def test_resources_csv_has_required_columns_and_rows(self):
        tmp = fixture_dir('csv')
        csv_path = os.path.join(tmp, 'resources.csv')
        guard = make_guard(tmp, csv_path=csv_path, interval=0.05)
        guard.start()
        try:
            self.assertTrue(wait_for(lambda: len(guard.rows) >= 3, timeout=5.0))
        finally:
            guard.stop()
        rows = read_csv_rows(csv_path)
        self.assertGreaterEqual(len(rows), 3)
        for column in ('ts', 'elapsed_s', 'stage', 'rss_bytes',
                       'mem_available_bytes', 'swap_used_bytes',
                       'disk_free_bytes', 'pid_count', 'pids'):
            self.assertIn(column, rows[0], 'missing column %r' % column)

    def test_stage_is_recorded_and_changes_with_set_stage(self):
        tmp = fixture_dir('csv-stage')
        csv_path = os.path.join(tmp, 'resources.csv')
        guard = make_guard(tmp, csv_path=csv_path, interval=0.05)
        guard.set_stage('precheck')
        guard.start()
        try:
            self.assertTrue(wait_for(lambda: len(guard.rows) >= 2, timeout=5.0))
        finally:
            guard.stop()
        rows = read_csv_rows(csv_path)
        self.assertTrue(all(r['stage'] == 'precheck' for r in rows))

    def test_status_json_is_written_with_reason(self):
        tmp = fixture_dir('status-json')
        status_path = os.path.join(tmp, 'run_status.json')
        csv_path = os.path.join(tmp, 'resources.csv')
        readings = StaticReadings(rss=2 * _MIB, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, csv_path=csv_path, status_path=status_path,
                           readings=readings, rss_limit_bytes=_MIB)
        guard._tick()
        self.assertTrue(os.path.isfile(status_path))
        with open(status_path) as handle:
            payload = json.load(handle)
        self.assertEqual(payload['status'], G.STATUS_RESOURCE_BLOCKED)
        self.assertIn(G.REASON_RSS, payload['violation'])
        self.assertEqual(payload['violation_reason'], G.REASON_RSS)
        self.assertIn('limits', payload)
        self.assertIn('sampled_max', payload)

    def test_no_temp_file_left_behind_for_status(self):
        tmp = fixture_dir('status-tmp')
        status_path = os.path.join(tmp, 'run_status.json')
        guard = make_guard(tmp, status_path=status_path)
        guard._tick()
        guard._write_status()
        self.assertFalse(os.path.exists(status_path + '.tmp'))


# ---------------------------------------------------------------------------
# 10. 用词纪律：采样最大值 ≠ 精确连续峰值
# ---------------------------------------------------------------------------

class WordingDisciplineTests(unittest.TestCase):

    def test_summary_reports_sampled_max_not_exact_peak(self):
        tmp = fixture_dir('wording')
        readings = StaticReadings(rss=130 * _MIB, mem_available=1024 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, readings=readings)
        clock = time.time
        guard._tick()
        stats = guard.sampled_max()
        self.assertIn('rss_sample_max_bytes', stats)
        self.assertIn('rss_sample_max_mb', stats)
        self.assertEqual(stats['rss_sample_max_bytes'], 130 * _MIB)
        # 绝不允许出现"精确连续峰值"式的字段名。
        for key in stats:
            self.assertNotIn('exact', key.lower())
            self.assertNotIn('true_peak', key.lower())
        self.assertFalse(hasattr(guard, 'exact_peak'))

    def test_max_sample_gap_is_reported(self):
        """采样间隔统计要在产物里可见（用来证明"没有 492 秒缺口"）。"""
        tmp = fixture_dir('gap')
        clock = FakeClock()
        guard = make_guard(tmp, clock=clock, interval=2.0)
        for _ in range(3):
            guard._tick()
            clock.advance(2.0)
        stats = guard.sampled_max()
        self.assertIsNotNone(stats['max_sample_gap_s'])
        self.assertLessEqual(stats['max_sample_gap_s'], 5.0)

    def test_unmeasured_samples_are_counted(self):
        tmp = fixture_dir('unmeasured')
        clock = FakeClock()
        readings = StaticReadings(rss_raises=True, mem_available=2048 * _MIB,
                                  disk_free=20 * _GIB)
        guard = make_guard(tmp, clock=clock, readings=readings,
                           max_unmeasured_seconds=1000.0)
        guard._tick()
        guard._tick()
        stats = guard.sampled_max()
        self.assertEqual(stats['unmeasured_samples'], 2)
        self.assertIsNone(stats['rss_sample_max_bytes'])


# ---------------------------------------------------------------------------
# 11. CLI 契约
# ---------------------------------------------------------------------------

class CliTests(unittest.TestCase):

    def test_parse_args_splits_stage_budgets(self):
        args = G.parse_args(['--out', 'x', '--stage-budget', 'prepare=3600',
                             '--stage-budget', 'train=14400',
                             '--', 'python', 'w.py'])
        self.assertEqual(G._parse_stage_budgets(args.stage_budget),
                         {'prepare': 3600.0, 'train': 14400.0})
        self.assertEqual(args.worker, ['python', 'w.py'])

    def test_parse_args_rejects_unknown_stage(self):
        with self.assertRaises(ValueError):
            G._parse_stage_budgets(['nope=10'])

    def test_interval_flag_defaults_to_two(self):
        args = G.parse_args(['--out', 'x'])
        self.assertEqual(args.interval, 2.0)

    def test_cli_requires_a_worker_command(self):
        self.assertEqual(G.main(['--out', 'x']), 2)

    def test_cli_smoke_runs_a_worker_and_writes_artifacts(self):
        """真跑一次 CLI（本地）：worker 正常退出 → 0 + 产物齐全。"""
        tmp = fixture_dir('cli')
        code = ('from experiments.itemcf import resource_guard as G; '
                'import sys; sys.exit(G.main(['
                '"--out", %r, "--interval", "0.2", '
                '"--", %r, "-c", "import time; time.sleep(0.5)"]))'
                % (tmp, sys.executable))
        completed = subprocess.run([sys.executable, '-c', code], cwd=PROJECT)
        self.assertEqual(completed.returncode, 0)
        self.assertTrue(os.path.isfile(os.path.join(tmp, 'resources.csv')))
        status_path = os.path.join(tmp, 'run_status.json')
        self.assertTrue(os.path.isfile(status_path))
        with open(status_path) as handle:
            payload = json.load(handle)
        self.assertEqual(payload['status'], G.STATUS_SUCCESS)

    @unittest.skipIf(os.name != 'posix', 'POSIX-only')
    def test_cli_kills_a_blocking_worker_and_exits_nonzero(self):
        """CLI 全程：阻塞 worker + 短阶段预算 → 非零退出 + 有采样 + 已终止。"""
        tmp = fixture_dir('cli-block')
        worker = write_blocking_worker()
        code = ('from experiments.itemcf import resource_guard as G; '
                'import sys; sys.exit(G.main(['
                '"--out", %r, "--interval", "0.2", '
                '"--stage-budget", "train=1.5", "--grace-seconds", "2", '
                '"--", %r, %r]))'
                % (tmp, sys.executable, worker))
        completed = subprocess.run([sys.executable, '-c', code], cwd=PROJECT)
        self.assertNotEqual(completed.returncode, 0)
        rows = read_csv_rows(os.path.join(tmp, 'resources.csv'))
        self.assertGreaterEqual(len(rows), 3,
                                'blocking worker must be sampled repeatedly')
        with open(os.path.join(tmp, 'run_status.json')) as handle:
            payload = json.load(handle)
        self.assertEqual(payload['status'], G.STATUS_RESOURCE_FAILED)
        self.assertTrue(payload['terminated'])

    def test_precheck_is_read_only_and_reports_platform(self):
        report = G.precheck()
        self.assertIn('platform', report)
        self.assertIn('disk_free_bytes', report)
        self.assertIn('meminfo_available', report)
        # 本地 Windows：/proc 不存在，ok 应为 False，且不得抛异常。
        if not os.path.isdir('/proc'):
            self.assertFalse(report['ok'])
            self.assertFalse(report['proc_readable'])


if __name__ == '__main__':
    unittest.main()
