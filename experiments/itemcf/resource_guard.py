# -*- coding: utf-8 -*-
"""独立资源监督进程：采样、预算门禁、终止与状态落盘（计划书 §5.A）。

为什么单独一个模块
------------------
上一轮确认实验（``experiments/itemcf/confirm.py``）的资源保护**实质失效**：
采样由主计算流程在几个检查点手动调用，而构图是纯 Python 循环、全程不回调，
于是 ``resources.csv`` 只有 **8 个采样点**，其中有一段 **492.5 秒的缺口，
正好覆盖 493 秒的整个构图期**。后果是：

* 报告里的"峰值 RSS 325.9 MB"只是 8 点中的最大值，**不是峰值保证**；
* 3 GiB 的终止线在那 493 秒里**根本无法触发**。

因此本模块的核心契约是：**监督独立于工作进程**。监督线程按固定周期采样，
即使 worker 完全阻塞、不回调、不协作，采样照样按时进行，并且能在超限时
终止它。监督线程自身只做"读 /proc + 写 CSV + 检查阈值"，不做任何与 worker
共享的阻塞操作。

四类终止（任一命中即终止本轮拥有的进程组）
------------------------------------------
======================================  ==========================
工作进程树 RSS（采样值）                > ``rss_limit_bytes``（默认 3 GiB）
MemAvailable **连续** ``mem_floor_streak`` 秒  < ``mem_available_floor_bytes``（默认 256 MiB，连续 30 s）
剩余磁盘                                < ``disk_floor_bytes``（默认 2 GiB）
阶段 / 总预算超时                       >= 对应 ``*_budget_seconds``
======================================  ==========================

终止方式：先给进程组 TERM，``grace_seconds``（默认 10 s）内未退出再 KILL。
**只终止本轮拥有的进程组**（worker 以 ``start_new_session=True`` 启动，
即自己的会话与进程组），绝不按名字或全表扫描去杀别的服务。

用词纪律
--------
采样本身就是"每隔 ``interval`` 秒看一眼"，所以统计口径一律叫
**"采样最大值"（``rss_sample_max``）**，不叫"精确连续峰值"。
即使把 interval 调到 2 秒，两点之间仍是盲区。

监控失败语义
------------
监督自身抛异常、或连续 ``max_unmeasured_seconds``（默认 60 s）采不到任何
有效读数 → 记录 ``monitor_failed`` 并**终止工作**、返回
``resource_failed``，而不是继续无保护地跑。

状态口径（与计划书 §6 一致）
---------------------------
``running`` / ``success`` / ``resource_blocked`` / ``resource_failed`` /
``correctness_failed`` / ``monitor_failed``。

外部依赖：仅标准库；进程树枚举用 ``/proc``（Linux）。非 Linux 平台
``/proc`` 不存在，读不到 RSS/MemAvailable，此时监督会走"无法采样"路径。
测试通过 ``rss_reader`` / ``mem_reader`` / ``disk_reader`` 注入读数，
不依赖 ``/proc``，也绝不真的耗尽机器内存。

Python 3.6 兼容：不用 f-string、dataclasses、``from __future__``、walrus、
``subprocess.run(capture_output=)``、dict 合并 ``|``。
"""
import argparse
import csv
import json
import os
import signal
import subprocess
import sys
import threading
import time

# --- 默认阈值（计划书 §5.A 的硬性数字）--------------------------------------
RSS_LIMIT_BYTES = 3 * 1024 ** 3
MEM_AVAILABLE_FLOOR_BYTES = 256 * 1024 ** 2
MEM_AVAILABLE_STREAK_SECONDS = 30.0
DISK_FLOOR_BYTES = 2 * 1024 ** 3
RESOURCE_POLL_SECONDS = 2.0
TERM_GRACE_SECONDS = 10.0
STAGES = ('prepare', 'precheck', 'train', 'verify')
MAX_UNMEASURED_SECONDS = 60.0

# --- 终态（只取这些明确值）--------------------------------------------------
STATUS_RUNNING = 'running'
STATUS_SUCCESS = 'success'
STATUS_RESOURCE_BLOCKED = 'resource_blocked'
STATUS_RESOURCE_FAILED = 'resource_failed'
STATUS_MONITOR_FAILED = 'monitor_failed'
STATUS_CORRECTNESS_FAILED = 'correctness_failed'

#: 监督判定终止后落盘的原因分类，任务验收要求"失败原因"可读。
REASON_RSS = 'rss_limit'
REASON_MEM = 'mem_available'
REASON_DISK = 'disk'
REASON_STAGE_TIMEOUT = 'stage_timeout'
REASON_MONITOR_FAILED = 'monitor_failed'
REASON_NONE = None

CSV_FIELDS = ('ts', 'elapsed_s', 'stage', 'rss_bytes', 'rss_mb', 'pid_count',
              'pids', 'mem_available_bytes', 'mem_available_mb',
              'swap_used_bytes', 'swap_total_bytes', 'disk_free_bytes',
              'cpu_count', 'load_per_cpu', 'sample_age_s', 'note')

#: 每个 stage 的行都带上本次的累计计数；阶段推进时按阶段快照。
BUDGET_KEYS = {
    'prepare': 'prepare_budget_seconds',
    'precheck': 'precheck_budget_seconds',
    'train': 'train_budget_seconds',
    'verify': 'verify_budget_seconds',
}


# ---------------------------------------------------------------------------
# 真实读数（Linux /proc）。非 Linux 返回 None，由上层走"无法采样"路径。
# ---------------------------------------------------------------------------

def read_meminfo():
    """读 ``/proc/meminfo``；返回 ``{'MemAvailable': bytes, ...}``。

    非 Linux（无 ``/proc/meminfo``）返回 ``{}``。
    """
    info = {}
    path = '/proc/meminfo'
    if not os.path.isfile(path):
        return info
    try:
        with open(path) as handle:
            for line in handle:
                parts = line.split(':')
                if len(parts) != 2:
                    continue
                key = parts[0].strip()
                values = parts[1].strip().split()
                if not values:
                    continue
                try:
                    number = int(values[0])
                except ValueError:
                    continue
                if len(values) > 1 and values[1] == 'kB':
                    number *= 1024
                info[key] = number
    except (IOError, OSError):
        return {}
    return info


def _iter_proc_stat():
    """产出 ``(pid, ppid)``；跳过内核线程与已消失的进程。"""
    if not os.path.isdir('/proc'):
        return
    try:
        names = os.listdir('/proc')
    except (IOError, OSError):
        return
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open('/proc/%s/stat' % name) as handle:
                # comm 可以含空格与括号，从最后一个 ')' 之后才是字段。
                fields = handle.read().rsplit(')', 1)[1].split()
            yield int(name), int(fields[1])
        except (IOError, OSError, IndexError, ValueError):
            continue


def read_rss_map():
    """返回 ``{pid: rss_bytes}``（``/proc/<pid>/statm`` 第 2 列 × 页大小）。

    非 Linux 返回 ``{}``。读不到的进程直接跳过（采样瞬间退出属正常）。
    """
    rss = {}
    if not os.path.isdir('/proc'):
        return rss
    try:
        page_size = os.sysconf('SC_PAGE_SIZE')
    except (ValueError, OSError, AttributeError):
        page_size = 4096
    for pid, _parent in _iter_proc_stat():
        try:
            with open('/proc/%d/statm' % pid) as handle:
                rss[pid] = int(handle.read().split()[1]) * page_size
        except (IOError, OSError, IndexError, ValueError):
            continue
    return rss


def process_tree_pids(root_pid):
    """返回 ``root_pid`` 及其**全部后代** PID（去重、按 PID 升序）。

    用父指针建树后自根遍历；只有从根可达的 PID 才算"本轮的进程"。
    """
    root_pid = int(root_pid)
    children = {}
    alive = set()
    for pid, parent in _iter_proc_stat():
        alive.add(pid)
        children.setdefault(parent, []).append(pid)
    if root_pid not in alive and not os.path.isdir('/proc'):
        return [root_pid]
    collected = []
    seen = set()
    stack = [root_pid]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        # 根自身即使 /proc 枚举不到（极短命）也要计入。
        if current == root_pid or current in alive:
            collected.append(current)
        stack.extend(children.get(current, ()))
    collected.sort()
    return collected


def process_tree_rss_bytes(root_pid, rss_map=None):
    """进程树 RSS 求和（字节）：遍历根及其后代，**同一 PID 只累加一次**。

    "去重"是契约的一部分：``/proc`` 枚举与父指针遍历在进程 fork 的瞬间
    可能出现同一个 PID 被走到两次，重复累加会把 RSS 虚报。所以这里用
    ``counted`` 集合去重，哪怕调用方传进来的 pid 列表本身有重复。

    返回 ``(total_bytes, pids)``；一个 PID 都读不到时返回 ``(None, [])``，
    以便上层区分"真的是 0"与"根本没采到"。``pids`` 是实际计入 RSS 的
    **去重后** PID 列表。
    """
    if rss_map is None:
        rss_map = read_rss_map()
    pids = process_tree_pids(root_pid)
    total = 0
    counted = []
    seen = set()
    for pid in pids:
        if pid in seen:
            continue
        value = rss_map.get(pid)
        if value is None:
            continue
        seen.add(pid)
        counted.append(pid)
        total += int(value)
    if not counted:
        return None, []
    counted.sort()
    return total, counted


def read_disk_free_bytes(path):
    """``path`` 所在文件系统剩余空间（字节）；失败返回 None。"""
    try:
        stat = os.statvfs(path)
    except (AttributeError, OSError):
        return None
    return stat.f_bavail * stat.f_frsize


# ---------------------------------------------------------------------------
# 终止：只对本轮拥有的进程组下手
# ---------------------------------------------------------------------------

def terminate_process_group(pgid, grace_seconds=TERM_GRACE_SECONDS,
                            log=None):
    """先 TERM 整组，``grace_seconds`` 内没退出再 KILL 整组。

    返回 ``{'term_sent': bool, 'kill_sent': bool, 'exited': bool,
    'waited_s': float}``。**只**对 ``pgid``（本轮 worker 自建的进程组）
    发信号，不涉及任何其他 PID。
    """
    result = {'term_sent': False, 'kill_sent': False, 'exited': False,
              'waited_s': 0.0, 'pgid': pgid, 'errors': []}
    if pgid is None:
        return result
    pgid = int(pgid)
    started = time.time()

    def _alive():
        return _group_alive(pgid)

    try:
        os.killpg(pgid, signal.SIGTERM)
        result['term_sent'] = True
    except (OSError, AttributeError) as error:
        result['errors'].append('TERM failed: %s' % error)
    deadline = started + float(grace_seconds)
    while time.time() < deadline:
        if not _alive():
            result['exited'] = True
            break
        time.sleep(0.1)
    if not result['exited'] and _alive():
        try:
            os.killpg(pgid, signal.SIGKILL)
            result['kill_sent'] = True
        except (OSError, AttributeError) as error:
            result['errors'].append('KILL failed: %s' % error)
        kill_deadline = time.time() + 5.0
        while time.time() < kill_deadline:
            if not _alive():
                result['exited'] = True
                break
            time.sleep(0.1)
    result['waited_s'] = round(time.time() - started, 2)
    if log is not None:
        log('terminate pgid=%s term=%s kill=%s exited=%s waited=%.2fs'
            % (pgid, result['term_sent'], result['kill_sent'],
               result['exited'], result['waited_s']))
    return result


def _group_alive(pgid):
    """进程组里是否还有活着的成员（不发送任何信号）。"""
    try:
        os.killpg(pgid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, AttributeError):
        # Windows 等没有 killpg 的环境：退化为"按 roots 判活"由调用方处理。
        return False


# ---------------------------------------------------------------------------
# 监督主体
# ---------------------------------------------------------------------------

class ResourceGuard(object):
    """独立采样 + 预算门禁 + 终止。可注入读数以便在本地测试。

    :param int root_pid: 根进程（工作）PID；采样其**全部后代**。
    :param str csv_path: ``resources.csv`` 路径，每个采样点后 flush。
    :param str status_path: ``run_status.json`` 路径（可选）。
    :param callable log: ``log(message)`` 回调（可选）。
    :param callable rss_reader: ``(root_pid) -> (bytes_or_None, pids)``；
        默认走 ``/proc``。
    :param callable mem_reader: ``() -> dict``，键 ``MemAvailable`` /
        ``SwapTotal`` / ``SwapFree``（字节）；默认走 ``/proc/meminfo``。
    :param callable disk_reader: ``() -> bytes_or_None``；默认 statvfs(磁盘目录)。
    :param float interval: 采样周期，默认 2.0 s。
    :param float monitord_grace: 监督启动后允许首个有效采样的等待时间。

    ``start()`` 起监督线程，``stop()`` 收尾。线程只做采样与阈值判断，
    不等待 worker，因此 worker 阻塞不影响采样节奏。
    """

    def __init__(self, root_pid, csv_path=None, status_path=None, log=None,
                 interval=RESOURCE_POLL_SECONDS,
                 rss_limit_bytes=RSS_LIMIT_BYTES,
                 mem_available_floor_bytes=MEM_AVAILABLE_FLOOR_BYTES,
                 mem_floor_streak_seconds=MEM_AVAILABLE_STREAK_SECONDS,
                 disk_floor_bytes=DISK_FLOOR_BYTES,
                 disk_path=None,
                 grace_seconds=TERM_GRACE_SECONDS,
                 max_unmeasured_seconds=MAX_UNMEASURED_SECONDS,
                 rss_reader=None, mem_reader=None, disk_reader=None,
                 stage_budgets=None, total_budget_seconds=None,
                 clock=None):
        self.root_pid = int(root_pid)
        self.csv_path = csv_path
        self.status_path = status_path
        self.log = log if log is not None else (lambda _m: None)
        self.interval = float(interval)
        self.rss_limit_bytes = int(rss_limit_bytes)
        self.mem_available_floor_bytes = int(mem_available_floor_bytes)
        self.mem_floor_streak_seconds = float(mem_floor_streak_seconds)
        self.disk_floor_bytes = int(disk_floor_bytes)
        self.disk_path = _default_disk_path(disk_path, csv_path)
        self.grace_seconds = float(grace_seconds)
        self.max_unmeasured_seconds = float(max_unmeasured_seconds)
        self.rss_reader = rss_reader if rss_reader is not None else _default_rss_reader
        self.mem_reader = mem_reader if mem_reader is not None else read_meminfo
        self.disk_reader = (disk_reader if disk_reader is not None
                            else self._default_disk_reader())
        self.stage_budgets = {}
        for stage in STAGES:
            self.stage_budgets[stage] = _to_float(
                (stage_budgets or {}).get(stage), None)
        self.total_budget_seconds = _to_float(total_budget_seconds, None)
        self._clock = clock if clock is not None else time.time

        # 采样状态（只在监督线程里写；stop() 后由主线程读，加锁保护）。
        self._lock = threading.Lock()
        self.rows = []
        self.samples = 0
        self.first_sample_at = None
        self.last_valid_sample_at = None
        self.low_mem_since = None
        self.low_mem_observed_total = 0.0
        self.low_mem_recoveries = 0
        self.violation = None
        self.violation_reason = None
        self.monitor_error = None
        self.stage = STAGES[0]
        self.stage_started_at = self._clock()
        self.started_at = self.stage_started_at
        self.finished_at = None
        self.terminated = False
        self.termination = None
        #: 编排层在 worker 结束后设定的终态（未设时按 violation 推断）。
        self.final_status = None
        self._stop_event = threading.Event()
        self._thread = None
        self._write_error = None
        self._csv_header_written = False
        self._last_row = None

    # -- 生命周期 ---------------------------------------------------------

    def start(self):
        """启动后台监督线程（守护线程；不阻塞调用方）。"""
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self.run, name='resource-guard',
                                        kwargs={'standalone': False})
        self._thread.daemon = True
        self._thread.start()
        self.log('resource guard started: pid=%d interval=%.1fs '
                 'rss_limit=%.2fGiB mem_floor=%dMiB/%ss disk_floor=%.2fGiB'
                 % (self.root_pid, self.interval,
                    self.rss_limit_bytes / float(1 << 30),
                    self.mem_available_floor_bytes >> 20,
                    _fmt_num(self.mem_floor_streak_seconds),
                    self.disk_floor_bytes / float(1 << 30)))
        return self

    def stop(self, timeout=10.0):
        """停止监督线程并返回终态摘要 dict。"""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)
        return self.summary()

    def run(self, standalone=True):
        """监督循环：按 ``interval`` 采样，直到 stop / 命中终止条件。

        ``standalone=True`` 时循环结束后把状态落盘（独立进程用法）。
        """
        while not self._stop_event.is_set():
            cycle_started = self._clock()
            if not self._tick():
                break
            if standalone:
                self._write_status()
            # 用单调时钟算剩余等待，避免采样本身上升造成周期漂移。
            elapsed = self._clock() - cycle_started
            wait = self.interval - elapsed
            if wait > 0:
                self._stop_event.wait(wait)
        self.finished_at = self._clock()
        if standalone:
            self._write_status()
        return self.summary()

    # -- 单次采样 ---------------------------------------------------------

    def _tick(self):
        """采一次样、写一行、判一次门禁。返回 False 表示应停止监督。"""
        now = self._clock()
        row = None
        try:
            row = self._collect(now)
        except Exception as error:  # noqa: BLE001 - 监督自身失败不能静默
            self.monitor_error = '%s: %s' % (type(error).__name__, error)
            self.log('WARN monitor sampling error: %s' % self.monitor_error)
        if row is not None:
            self.rows.append(row)
            self.samples = len(self.rows)
            if self.first_sample_at is None:
                self.first_sample_at = now
            if row.get('rss_bytes') is not None:
                self.last_valid_sample_at = now
            self._last_row = row
            self._append_csv(row)
        reason = None
        if row is not None:
            reason = self._evaluate(row, now)
        if reason is None:
            # 监督失败路径：自身异常，或长时间采不到任何有效读数。
            reason = self._monitor_failure(now)
        if reason is not None:
            self._on_violation(reason)
            return False
        return True

    def _collect(self, now):
        """产生一行采样；只读，不做任何与 worker 共享的阻塞操作。"""
        rss_value = None
        pids = []
        try:
            measured = self.rss_reader(self.root_pid)
        except Exception as error:  # noqa: BLE001 - 读数失败记 note 继续
            measured = None
            self.log('WARN rss reader failed: %s' % error)
        if isinstance(measured, tuple) and len(measured) == 2:
            rss_value, pids = measured
        elif measured is not None:
            rss_value = measured
            pids = self._fallback_pids()
        mem = {}
        try:
            mem = self.mem_reader() or {}
        except Exception as error:  # noqa: BLE001
            self.log('WARN mem reader failed: %s' % error)
            mem = {}
        available = mem.get('MemAvailable')
        swap_total = mem.get('SwapTotal')
        swap_free = mem.get('SwapFree')
        swap_used = None
        if swap_total is not None and swap_free is not None:
            swap_used = int(swap_total) - int(swap_free)
        disk_free = None
        try:
            disk_free = self.disk_reader()
        except Exception as error:  # noqa: BLE001
            self.log('WARN disk reader failed: %s' % error)
        load = None
        try:
            load = os.getloadavg()[0] / float(os.cpu_count() or 1)
        except (OSError, AttributeError):
            load = None
        previous = self._last_row
        row = {
            'ts': int(self._wall_clock()),
            'elapsed_s': round(now - self.started_at, 2),
            'stage': self.stage,
            'stage_elapsed_s': round(now - self.stage_started_at, 2),
            'rss_bytes': int(rss_value) if rss_value is not None else None,
            'rss_mb': (round(rss_value / float(1 << 20), 1)
                       if rss_value is not None else None),
            'pid_count': len(pids),
            'pids': ','.join(str(p) for p in pids) if pids else '',
            'mem_available_bytes': available,
            'mem_available_mb': (round(available / float(1 << 20), 1)
                                 if available is not None else None),
            'swap_used_bytes': swap_used,
            'swap_total_bytes': swap_total,
            'disk_free_bytes': disk_free,
            'cpu_count': os.cpu_count(),
            'load_per_cpu': round(load, 3) if load is not None else None,
            'sample_age_s': (round(now - previous['_at'], 2) if previous else None),
            'note': '' if rss_value is not None else 'rss_unavailable',
            '_at': now,
        }
        row['low_mem'] = (available is not None
                          and available < self.mem_available_floor_bytes)
        row['pid_tree'] = list(pids)
        return row

    def _wall_clock(self):
        try:
            return time.time()
        except Exception:  # noqa: BLE001
            return 0

    def _default_disk_reader(self):
        path = self.disk_path
        return lambda: read_disk_free_bytes(path)

    def _fallback_pids(self):
        try:
            return process_tree_pids(self.root_pid)
        except Exception:  # noqa: BLE001
            return [self.root_pid]

    # -- 门禁 -------------------------------------------------------------

    def _evaluate(self, row, now):
        """四类终止规则。返回原因字符串或 None。"""
        rss = row.get('rss_bytes')
        if rss is not None and rss > self.rss_limit_bytes:
            return ('%s: process tree RSS %.2f GiB > %.2f GiB limit (sampled)'
                    % (REASON_RSS, rss / float(1 << 30),
                       self.rss_limit_bytes / float(1 << 30)))
        available = row.get('mem_available_bytes')
        if available is not None and available < self.mem_available_floor_bytes:
            if self.low_mem_since is None:
                self.low_mem_since = now
                self.log('WARN MemAvailable %d MiB < %d MiB floor; '
                         'streak timer starts'
                         % (available >> 20, self.mem_available_floor_bytes >> 20))
            else:
                held = now - self.low_mem_since
                self.low_mem_observed_total += max(0.0, row.get('sample_age_s') or 0.0)
                if held >= self.mem_floor_streak_seconds:
                    return ('%s: MemAvailable below %d MiB for %.1fs '
                            '(>= %.0fs)'
                            % (REASON_MEM, self.mem_available_floor_bytes >> 20,
                               held, self.mem_floor_streak_seconds))
        elif self.low_mem_since is not None:
            # 恢复：清零并重新计时（不能在恢复后把旧的累计时间接续使用）。
            recovered_after = now - self.low_mem_since
            self.low_mem_recoveries += 1
            self.low_mem_since = None
            self.log('MemAvailable recovered after %.1fs low; '
                     'streak timer reset' % recovered_after)
        disk_free = row.get('disk_free_bytes')
        if disk_free is not None and disk_free < self.disk_floor_bytes:
            return ('%s: free disk %.2f GiB < %.2f GiB floor on %s'
                    % (REASON_DISK, disk_free / float(1 << 30),
                       self.disk_floor_bytes / float(1 << 30), self.disk_path))
        budget = self.stage_budgets.get(self.stage)
        if budget is not None and now - self.stage_started_at >= budget:
            return ('%s: stage %r ran %.1fs >= %.0fs budget'
                    % (REASON_STAGE_TIMEOUT, self.stage,
                       now - self.stage_started_at, budget))
        if (self.total_budget_seconds is not None
                and now - self.started_at >= self.total_budget_seconds):
            return ('%s: total run %.1fs >= %.0fs budget'
                    % (REASON_STAGE_TIMEOUT, now - self.started_at,
                       self.total_budget_seconds))
        return None

    def _monitor_failure(self, now):
        """监督自身失败 / 长时间无法采样 → 也要停止工作。"""
        if self.monitor_error is not None and self._last_row is None:
            return '%s: supervisor could not sample at all (%s)' % (
                REASON_MONITOR_FAILED, self.monitor_error)
        reference = self.last_valid_sample_at
        if reference is None:
            reference = self.first_sample_at
        if reference is None:
            # 一次都没采到：从启动算起，超过 max_unmeasured 即失败。
            if now - self.started_at >= self.max_unmeasured_seconds:
                return ('%s: no sample obtained within %.0fs of start'
                        % (REASON_MONITOR_FAILED, self.max_unmeasured_seconds))
            return None
        if self._last_row is None or self._last_row.get('rss_bytes') is None:
            if now - reference >= self.max_unmeasured_seconds:
                return ('%s: no valid process-tree sample for %.0fs'
                        % (REASON_MONITOR_FAILED, self.max_unmeasured_seconds))
        return None

    def _on_violation(self, reason):
        self.violation = reason
        self.violation_reason = reason.split(':', 1)[0]
        self.terminated = True
        self.log('RESOURCE GUARD VIOLATION: %s' % reason)
        self._write_status()

    # -- 阶段与预算 -------------------------------------------------------

    def set_stage(self, stage):
        """切换到新阶段并重置该阶段计时（阶段超时按阶段算）。"""
        if stage not in STAGES:
            raise ValueError('unknown stage %r (expected one of %s)'
                             % (stage, ', '.join(STAGES)))
        self.stage = stage
        self.stage_started_at = self._clock()
        self.log('stage -> %s (budget=%s)'
                 % (stage, _fmt_num(self.stage_budgets.get(stage))))
        return stage

    # -- 落盘 -------------------------------------------------------------

    def _append_csv(self, row):
        """每行都 flush：即使随后进程被杀，CSV 也保留到最后一个采样点。

        这是对上一轮"最后才 dump 一次"的直接修正——上一轮的 8 行是
        事后一次性写出的，中途被杀就什么都没有。
        """
        if not self.csv_path:
            return
        try:
            folder = os.path.dirname(os.path.abspath(self.csv_path))
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            if not self._csv_header_written:
                exists = (os.path.isfile(self.csv_path)
                          and os.path.getsize(self.csv_path) > 0)
                mode = 'a' if exists else 'w'
            else:
                mode = 'a'
            with open(self.csv_path, mode, newline='') as handle:
                writer = csv.writer(handle)
                if not self._csv_header_written:
                    if mode == 'w':
                        writer.writerow(CSV_FIELDS)
                    self._csv_header_written = True
                writer.writerow([_csv_cell(row.get(name)) for name in CSV_FIELDS])
                handle.flush()
                os.fsync(handle.fileno())
        except (IOError, OSError, ValueError) as error:
            self._write_error = str(error)
            self.monitor_error = self.monitor_error or ('csv write failed: %s' % error)
            self.log('WARN could not append resources row: %s' % error)

    def write_status(self):
        """公开的状态落盘（独立进程用法）。"""
        return self._write_status()

    def _write_status(self):
        if not self.status_path:
            return None
        payload = self.summary()
        payload['rows_written'] = len(self.rows)
        tmp = self.status_path + '.tmp'
        try:
            folder = os.path.dirname(os.path.abspath(self.status_path))
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
            with open(tmp, 'w') as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.status_path)
        except (IOError, OSError, ValueError) as error:
            self.log('WARN could not write status: %s' % error)
            return None
        return self.status_path

    # -- 统计 -------------------------------------------------------------

    def sampled_max(self):
        """**采样最大值**（不是"精确连续峰值"）。

        因为采样是每隔 ``interval`` 秒看一眼，两次采样之间的峰值不会被
        记录；口径名称必须如实反映这一点。
        """
        rss = [r['rss_bytes'] for r in self.rows if r['rss_bytes'] is not None]
        mem = [r['mem_available_bytes'] for r in self.rows
               if r['mem_available_bytes'] is not None]
        disk = [r['disk_free_bytes'] for r in self.rows
                if r['disk_free_bytes'] is not None]
        ages = [r['sample_age_s'] for r in self.rows
                if r['sample_age_s'] is not None]
        peak = max(rss) if rss else None
        return {
            'sample_count': len(self.rows),
            'rss_sample_max_bytes': peak,
            'rss_sample_max_mb': (round(peak / float(1 << 20), 1)
                                  if peak is not None else None),
            'rss_sample_max_gi': (round(peak / float(1 << 30), 4)
                                  if peak is not None else None),
            'mem_available_sample_min_bytes': min(mem) if mem else None,
            'mem_available_sample_min_mb': (round(min(mem) / float(1 << 20), 1)
                                            if mem else None),
            'disk_free_sample_min_bytes': min(disk) if disk else None,
            'max_sample_gap_s': max(ages) if ages else None,
            'max_pid_count': max([r['pid_count'] for r in self.rows] or [0]),
            'unmeasured_samples': len([r for r in self.rows
                                       if r['rss_bytes'] is None]),
            'low_mem_recoveries': self.low_mem_recoveries,
        }

    #: 兼容旧调用点的别名；语义仍是"采样最大值"。
    peak = sampled_max

    def summary(self):
        """终态摘要（落盘 ``run_status.json`` 的主要内容）。"""
        stats = self.sampled_max()
        payload = {
            'status': self.status(),
            'root_pid': self.root_pid,
            'interval_s': self.interval,
            'stage': self.stage,
            'monitor_error': self.monitor_error,
            'violation': self.violation,
            'violation_reason': self.violation_reason,
            'terminated': self.terminated,
            'termination': self.termination,
            'csv_write_error': self._write_error,
            'limits': {
                'rss_limit_bytes': self.rss_limit_bytes,
                'mem_available_floor_bytes': self.mem_available_floor_bytes,
                'mem_floor_streak_seconds': self.mem_floor_streak_seconds,
                'disk_floor_bytes': self.disk_floor_bytes,
                'stage_budgets_seconds': dict(
                    (k, v) for k, v in self.stage_budgets.items() if v is not None),
                'total_budget_seconds': self.total_budget_seconds,
                'max_unmeasured_seconds': self.max_unmeasured_seconds,
            },
            'sampled_max': stats,
        }
        # 旧字段名（confirm.py 口径）保留为别名，方便对照旧产物。
        payload['peak_rss_bytes'] = stats['rss_sample_max_bytes']
        payload['peak_rss_mb'] = stats['rss_sample_max_mb']
        payload['samples'] = stats['sample_count']
        return payload

    def status(self):
        """只取明确状态值之一（计划书 §6 的状态口径）。

        映射（写在代码里以免被误读）：

        * RSS / MemAvailable / 磁盘超限 → ``resource_blocked``：机器资源不够，
          该规模在本机跑不动，属"硬性资源不足"；
        * 阶段 / 总预算超时 → ``resource_failed``：资源本身也许够，但这次
          没在预算内完成；
        * 监督自身失败 / 长时间采不到数 → ``monitor_failed``。
        """
        if self.violation is None:
            return self.final_status or STATUS_RUNNING
        if self.violation_reason == REASON_MONITOR_FAILED:
            return STATUS_MONITOR_FAILED
        if self.violation_reason == REASON_STAGE_TIMEOUT:
            return STATUS_RESOURCE_FAILED
        return STATUS_RESOURCE_BLOCKED


# ---------------------------------------------------------------------------
# 进程监督：启动 worker + 监督 + 收尾
# ---------------------------------------------------------------------------

def _to_float(value, default):
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _fmt_num(value):
    if value is None:
        return 'none'
    try:
        return '%.0f' % float(value)
    except (TypeError, ValueError):
        return str(value)


def _default_disk_path(disk_path, csv_path):
    if disk_path:
        return disk_path
    if csv_path:
        folder = os.path.dirname(os.path.abspath(csv_path))
        if folder:
            return folder
    return os.getcwd()


def _csv_cell(value):
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return value


def _default_rss_reader(root_pid):
    return process_tree_rss_bytes(root_pid)


class GuardedRun(object):
    """启动受监督的 worker 子进程，并在超限时终止**本轮自己的进程组**。

    API::

        run = GuardedRun(['python', 'worker.py'], csv_path=..., ...)
        result = run.run(timeout_seconds=30, stages=(('train', 10),))
        # result = {'exit_code':..., 'status':..., 'reason':..., ...}

    worker 用 ``start_new_session=True``（POSIX）启动，因此它自成会话与进程组；
    终止只对 ``run.pgid`` 发信号，绝不波及其他服务。
    """

    def __init__(self, argv, csv_path=None, status_path=None, log=None,
                 interval=RESOURCE_POLL_SECONDS, env=None, cwd=None,
                 stage_budgets=None, total_budget_seconds=None,
                 **guard_kwargs):
        if not argv:
            raise ValueError('argv must not be empty')
        self.argv = list(argv)
        self.csv_path = csv_path
        self.status_path = status_path
        self.log = log if log is not None else (lambda _m: None)
        self.interval = float(interval)
        self.env = env
        self.cwd = cwd
        self.stage_budgets = dict(stage_budgets or {})
        self.total_budget_seconds = total_budget_seconds
        self.guard_kwargs = guard_kwargs
        self.process = None
        self.guard = None
        self.pgid = None
        self.terminated = False
        self.termination = None

    def _popen_kwargs(self):
        kwargs = {
            'stdout': subprocess.PIPE,
            'stderr': subprocess.STDOUT,
            'env': self.env,
        }
        if self.cwd:
            kwargs['cwd'] = self.cwd
        if os.name == 'posix':
            # 关键：独立会话/进程组，终止时才能只打自己那一组。
            kwargs['start_new_session'] = True
        return kwargs

    def start(self):
        """启动 worker（不等待），返回 Popen 对象。"""
        self.process = subprocess.Popen(self.argv, **self._popen_kwargs())
        self.pgid = _process_group_of(self.process)
        self.guard = ResourceGuard(
            self.process.pid,
            csv_path=self.csv_path,
            status_path=self.status_path,
            log=self.log,
            interval=self.interval,
            stage_budgets=self.stage_budgets,
            total_budget_seconds=self.total_budget_seconds,
            **self.guard_kwargs)
        self.guard.start()
        return self.process

    def run(self, timeout_seconds=None, poll_seconds=0.1,
            on_stage=None, stage_plan=None, stage='train'):
        """跑完 worker 并返回终态 dict。

        阶段与预算的语义（VM 上踩过一次坑，写清楚以免重演）：
        ``ResourceGuard`` 只对**当前阶段**判超时，所以"给了 ``train`` 的预算"
        不等于"现在就按 train 计时"。本方法必须显式把 guard 切到对应阶段，
        否则 worker 会一直停在默认的 ``prepare`` 上、``train`` 的预算永远
        不生效——那是"保护形同虚设"的另一种写法（实测后果：给 1.5 s 预算
        的阻塞 worker 跑了 36 分钟没被杀）。

        :param float timeout_seconds: 本轮时长上限；同时设为 ``stage`` 的阶段
            预算**和**总预算，任一命中都会终止 worker，调用方随手传一个超时
            就一定有效。
        :param str stage: ``timeout_seconds`` 归属的阶段名，默认 ``'train'``。
        :param callable on_stage: ``(guard) -> None``，worker 仍在跑时调用一次，
            供编排层推进阶段（``lambda g: g.set_stage('verify')``）。
        :param list stage_plan: ``[(stage, budget_seconds), ...]``；只取第一项
            作为起始阶段，后续阶段由编排层经 ``on_stage`` 显式推进。
        """
        stage_plan = list(stage_plan or [])
        if stage_plan:
            stage_budgets = dict(self.stage_budgets)
            for plan_stage, plan_budget in stage_plan:
                stage_budgets[plan_stage] = plan_budget
            self.stage_budgets = stage_budgets
        if timeout_seconds is not None:
            self.stage_budgets[stage] = float(timeout_seconds)
            # 总预算兜底：无论阶段怎么切，超过这个时长也一定停。
            self.total_budget_seconds = float(timeout_seconds)
        if self.process is None:
            self.start()
        # start() 之后 guard 才存在：把预算同步进去，并把阶段**切实**切到位。
        self.guard.stage_budgets = dict(self.stage_budgets)
        self.guard.total_budget_seconds = self.total_budget_seconds
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
            # 终止后仍等不到退出：进程组已 KILL，但 wait 超时。
            try:
                exit_code = self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                exit_code = None
        self._close_pipes()
        if self.guard is not None:
            self.guard.stop()
            result = self.result(exit_code)
            # 终态必须落盘：让 run_status.json 里的 status 与返回值一致，
            # 而不是停在 running（那会让后续自动继续规则误判本轮还在跑）。
            self.guard.final_status = result['status']
            self.guard._write_status()
            return result
        return self.result(exit_code)

    def _close_pipes(self):
        """关掉 worker 的 stdout 管道，避免句柄泄漏。

        注意：这里**不**读管道内容——worker 的输出由调用方按需处理，读取
        会引入"监督等 IO"的耦合（正是本模块要避免的）。如果 worker 写出
        超过管道缓冲的内容，阻塞会在**它自己**的写操作上发生，不影响监督
        采样；此时监督仍会按预算终止它。
        """
        if self.process is None:
            return
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (IOError, OSError):
                    pass

    def result(self, exit_code):
        stats = self.guard.sampled_max() if self.guard is not None else {}
        status = STATUS_SUCCESS
        reason = None
        if self.guard is not None and self.guard.violation is not None:
            status = self.guard.status()
            reason = self.guard.violation
        elif exit_code != 0:
            status = STATUS_CORRECTNESS_FAILED
            reason = 'worker exited with code %r' % (exit_code,)
        return {
            'exit_code': exit_code,
            'status': status,
            'reason': reason,
            'terminated': self.terminated,
            'termination': self.termination,
            'pgid': self.pgid,
            'root_pid': (self.process.pid if self.process is not None else None),
            'sampled_max': stats,
            'resource_violation': (self.guard.violation
                                   if self.guard is not None else None),
        }


def _process_group_of(process):
    """取 worker 的进程组 ID（POSIX）。"""
    if process is None:
        return None
    if os.name != 'posix':
        return None
    try:
        return os.getpgid(process.pid)
    except (OSError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# 独立进程入口：CLI
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='Independent resource supervisor: run a compute child under '
                    'sampling, budget gates and process-group termination '
                    '(plan section 5.A).')
    parser.add_argument('--out', help='产物目录（resources.csv / run_status.json）')
    parser.add_argument('--resources-csv', help='resources.csv 路径（覆盖 --out）')
    parser.add_argument('--interval', type=float, default=RESOURCE_POLL_SECONDS,
                        help='采样周期秒（默认 2）')
    parser.add_argument('--rss-limit-bytes', type=int, default=RSS_LIMIT_BYTES)
    parser.add_argument('--mem-floor-bytes', type=int,
                        default=MEM_AVAILABLE_FLOOR_BYTES)
    parser.add_argument('--mem-floor-streak-seconds', type=float,
                        default=MEM_AVAILABLE_STREAK_SECONDS)
    parser.add_argument('--disk-floor-bytes', type=int, default=DISK_FLOOR_BYTES)
    parser.add_argument('--disk-path', default=None)
    parser.add_argument('--grace-seconds', type=float, default=TERM_GRACE_SECONDS)
    parser.add_argument('--max-unmeasured-seconds', type=float,
                        default=MAX_UNMEASURED_SECONDS)
    parser.add_argument('--stage-budget', action='append', default=[],
                        metavar='STAGE=SECONDS',
                        help='阶段预算，可重复；STAGE 取 %s'
                             % '/'.join(STAGES))
    parser.add_argument('--total-budget-seconds', type=float, default=None)
    parser.add_argument('--precheck', action='store_true',
                        help='只做环境检查（磁盘/内存读数可用性），不启动 worker')
    parser.add_argument('worker', nargs=argparse.REMAINDER,
                        help='-- 之后的 worker 命令')
    args = parser.parse_args(argv)
    worker = list(args.worker)
    if worker and worker[0] == '--':
        worker = worker[1:]
    args.worker = worker
    return args


def _parse_stage_budgets(items):
    budgets = {}
    for item in items or []:
        if '=' not in item:
            raise ValueError('--stage-budget expects STAGE=SECONDS, got %r' % item)
        stage, value = item.split('=', 1)
        stage = stage.strip()
        if stage not in STAGES:
            raise ValueError('unknown stage %r (expected one of %s)'
                             % (stage, ', '.join(STAGES)))
        budgets[stage] = float(value)
    return budgets


def precheck(disk_path=None, log=None):
    """只读预检：磁盘 / 内存读数是否可用（不启动 worker）。"""
    log = log if log is not None else (lambda _m: None)
    mem = read_meminfo()
    disk_path = disk_path or os.getcwd()
    free = read_disk_free_bytes(disk_path)
    rss_map = read_rss_map()
    report = {
        'platform': sys.platform,
        'pid': os.getpid(),
        'meminfo_available': 'MemAvailable' in mem,
        'mem_available_bytes': mem.get('MemAvailable'),
        'swap_total_bytes': mem.get('SwapTotal'),
        'swap_free_bytes': mem.get('SwapFree'),
        'disk_path': disk_path,
        'disk_free_bytes': free,
        'proc_readable': bool(rss_map),
        'proc_pid_count': len(rss_map),
        'self_tree_rss_bytes': process_tree_rss_bytes(os.getpid(), rss_map)[0],
    }
    report['ok'] = bool(report['proc_readable'] and report['meminfo_available'])
    log('precheck: %s' % json.dumps(report, sort_keys=True))
    return report


def main(argv=None):
    """独立监督进程入口。

    用法::

        python -m experiments.itemcf.resource_guard \\
          --out <run_dir> --interval 2 \\
          --stage-budget prepare=3600 --stage-budget train=14400 \\
          -- <worker 命令...>

    退出码：worker 正常退出 → 0；资源超限（rss/mem/disk）→ 3；阶段或总预算
    超时 → 6；worker 自身非零退出 → 4；监控失败 → 5；用法/预检失败 → 2。
    """
    args = parse_args(argv)
    def log(message):
        sys.stdout.write('[%s] %s\n' % (time.strftime('%Y-%m-%dT%H:%M:%S'),
                                        message))
        sys.stdout.flush()

    try:
        budgets = _parse_stage_budgets(args.stage_budget)
    except ValueError as error:
        sys.stderr.write('%s\n' % error)
        return 2
    csv_path = args.resources_csv
    if csv_path is None and args.out:
        csv_path = os.path.join(args.out, 'resources.csv')
    status_path = os.path.join(args.out, 'run_status.json') if args.out else None
    if args.precheck:
        report = precheck(args.disk_path, log=log)
        if status_path:
            _dump_json(status_path, report)
        return 0 if report['ok'] else 2
    if not args.worker:
        sys.stderr.write('no worker command given; use -- <cmd...>\n')
        return 2
    runner = GuardedRun(args.worker, csv_path=csv_path, status_path=status_path,
                        log=log, interval=args.interval,
                        stage_budgets=budgets,
                        total_budget_seconds=args.total_budget_seconds,
                        rss_limit_bytes=args.rss_limit_bytes,
                        mem_available_floor_bytes=args.mem_floor_bytes,
                        mem_floor_streak_seconds=args.mem_floor_streak_seconds,
                        disk_floor_bytes=args.disk_floor_bytes,
                        disk_path=args.disk_path,
                        grace_seconds=args.grace_seconds,
                        max_unmeasured_seconds=args.max_unmeasured_seconds)
    result = runner.run()
    log('RESULT %s' % json.dumps(
        dict((k, v) for k, v in result.items() if k != 'termination'),
        sort_keys=True))
    if result['status'] == STATUS_SUCCESS:
        return 0
    if result['status'] == STATUS_MONITOR_FAILED:
        return 5
    if result['status'] == STATUS_RESOURCE_BLOCKED:
        return 3
    if result['status'] == STATUS_RESOURCE_FAILED:
        # 区分"预算超时"与"worker 自己失败"：前者 guard 有 violation。
        return 6 if result.get('resource_violation') else 4
    return 4


def _dump_json(path, payload):
    folder = os.path.dirname(os.path.abspath(path))
    if folder and not os.path.isdir(folder):
        os.makedirs(folder)
    tmp = path + '.tmp'
    with open(tmp, 'w') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


if __name__ == '__main__':
    sys.exit(main())
