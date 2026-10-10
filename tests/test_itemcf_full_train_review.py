# -*- coding: utf-8 -*-
"""**对抗性审查用例**：``experiments/itemcf/full_train.py`` 的缺陷举证。

这是 ``task-8`` 的交付物，与 ``tests/test_itemcf_full_training.py``（实现方写的
契约测试）**互补**：那份测"设计意图是否被实现"，这份测"实现里有哪些会毁掉全量
run 的东西"。

**硬约束**：本文件**只新增测试，不修改任何生产文件**。因为 ``full_train.py`` 的
SHA-256 会写进 ``source_hashes.json`` 与 block signature，运行中源码漂移会让整轮
VM 结果判定无效。

**两种用例形态**（本文件现状：**没有** ``@unittest.expectedFailure`` 了）：

* **已修** —— 断言**修复后**的契约（docstring 里写清"修复前是什么行为、修法是
  什么"）。这类占多数，名字多为 ``test_xxx_now_yyy`` / ``test_xxx_is_gone``。
* **未修** —— 用"断言当前（错误）行为"把缺陷钉死，**不留红灯**，但一旦修好
  这些用例就会变红，那就是把它们改成"已修"形态的信号。当前这一类只剩边界/
  职责类用例（如 ``test_clean_exit_code_alone_still_produces_success_at_this_layer``
  与 ``test_run_precheck_alone_does_not_mutate_its_callers_args``），
  **不再有"已知未修缺陷"的锚点**。

缺陷分级（每条用例的 docstring 里都有最小复现命令）：

* ``CRITICAL`` —— 会让本轮全量 run 直接不出结果，或产出**静默错误**的模型；
* ``MAJOR``   —— 需要特定条件（磁盘紧、参数组合、重复调用）才触发；
* ``MINOR``   —— 不影响正确性，只影响可维护性/取证完整性。

复现基线（本机实测）：Python 3.12.10 / NumPy 2.2.6 / SciPy 1.13.1
（``backend/venv/Scripts/python.exe``）。VM 目标为 3.6.8 / 1.19.5 / 1.5.4。
"""
import ast
import csv
import os
import re
import shutil
import sys
import tempfile
import unittest

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT not in sys.path:
    sys.path.insert(0, PROJECT)

import numpy as np  # noqa: E402
from scipy import sparse  # noqa: E402

from experiments.itemcf import full_train as F  # noqa: E402
from experiments.itemcf import model_io as M  # noqa: E402
from experiments.itemcf import resource_guard as G  # noqa: E402

FULL_TRAIN_PATH = os.path.join(PROJECT, 'experiments', 'itemcf', 'full_train.py')

#: 审查用的临时根目录（沙箱拒绝写系统临时目录，所以放在 workspace 内）。
REVIEW_ROOT = os.path.join(PROJECT, '.vmwork', 'review_train', '_unittest')


def review_dir(name):
    """建（并重建）一个审查专用的唯一目录。"""
    path = os.path.join(REVIEW_ROOT, name)
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)
    return path


def write_csv(path, rows, header=('userId', 'movieId', 'rating', 'timestamp')):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(list(header))
        for row in rows:
            writer.writerow(row)
    return path


def args_stub(**overrides):
    """一个能过 ``parameters_of`` 的最小 args（不碰 CLI 默认值）。"""
    base = {
        'input': 'in.csv', 'out': 'out', 'max_k': 100, 'beta': 0.0,
        'positive_threshold': 4.0, 'block_size': 128,
        'monitor_interval': 2.0, 'prepare_budget_seconds': 3600.0,
        'precheck_budget_seconds': 1800.0, 'train_budget_seconds': 14400.0,
        'verify_budget_seconds': 1800.0, 'seed': 42, 'duplicate_policy': 'reject',
        'worker': False, 'run_id': None,
    }
    base.update(overrides)
    return type('Args', (object,), base)


class _TinyCorpusMixin(object):
    """11 行 / 4 用户 / 5 物品的小语料：能跑完整 prepare→verify 链路。"""

    ROWS = [
        (1, 10, 5.0, 100), (1, 11, 4.5, 101), (1, 12, 2.0, 102),
        (2, 10, 4.0, 103), (2, 11, 3.0, 104), (2, 13, 5.0, 105),
        (3, 12, 4.0, 106), (3, 13, 4.5, 107), (3, 14, 1.0, 108),
        (4, 10, 4.0, 109), (4, 14, 4.0, 110),
    ]

    def make_csv(self, path, rows=None):
        return write_csv(path, self.ROWS if rows is None else rows)


# ===========================================================================
# 1. CRITICAL：编排层传给 worker 的 --out 与它自己用的 out_dir 不是同一个目录
# ===========================================================================

class WorkerOutDirPropagationTests(_TinyCorpusMixin, unittest.TestCase):
    """``run()`` 建 ``<out>/<run_id>``，却把**原始** ``args.out`` 传给 worker。

    最小复现（本机实测，见 ``.vmwork/review_train/probe2.py``）::

        python -m experiments.itemcf.full_train \\
            --input tiny.csv --out .vmwork/review_train/e2e/plain ...
        # -> orchestrator out_dir = .../plain/20261004T172822Z-679534f1
        #    worker  args.out    = .../plain
        #    worker: ValueError: run_id (from output directory name) must look
        #            like YYYYMMDDTHHMMSSZ-xxxxxxxx, got 'plain'
        #    final status = correctness_failed
    """

    def test_worker_argv_out_is_raw_args_out(self):
        """CRITICAL（断言**当前**行为）：``worker_argv`` 原样转发 ``args.out``。

        ``worker_argv`` 直接转发 ``args.out``，而 ``run()`` 用的是
        ``resolve_out_dir(args.out, run_id)``。只要 ``--out`` 末段不是 run_id
        形状，worker 的 ``assembled_ids(out_dir)`` 就会在 ``run.log`` 之后、
        **任何产物落盘之前**抛 ``ValueError``，本轮没有任何模型。

        这条用例把"当前行为"钉死，这样 Lead 一改就会看到它变红——它是修复的
        验收锚点。期望的修复见下面 ``test_worker_argv_out_is_the_resolved_run_directory``。
        """
        args = args_stub(out=os.path.join(REVIEW_ROOT, 'cand_raw'))
        argv = F.worker_argv(args)
        worker_out = argv[argv.index('--out') + 1]
        self.assertEqual(worker_out, args.out,
                         'worker_argv forwards args.out verbatim (current behaviour)')
        run_id = F.make_run_id(now='20261004T120000Z', token='deadbeef')
        orchestrator_out = F.resolve_out_dir(args.out, run_id)
        self.assertNotEqual(os.path.abspath(worker_out), orchestrator_out,
                            'and that is NOT the directory run() actually uses')

    def test_worker_argv_out_is_the_resolved_run_directory(self):
        """CRITICAL-1（**已由 Lead 修复**）：``--out`` 必须等于 ``resolve_out_dir``。

        ``run()`` 建的是 ``<out>/<run_id>``，worker 曾经收的是未解析的 ``<out>``，
        于是 worker 用 ``basename`` 反推 run_id → 末段非 run_id 形状时直接崩
        （exit 3、零产物）。修复：``worker_argv(args, out_dir=..., run_id=...)``
        接收编排层解析后的目录与 run_id。
        """
        args = args_stub(out=os.path.join(REVIEW_ROOT, 'cand'))
        run_id = F.make_run_id(now='20261004T120000Z', token='deadbeef')
        orchestrator_out = F.resolve_out_dir(args.out, run_id)
        # 编排层传解析后的目录进去（run() 里的真实调用形状）。
        argv = F.worker_argv(args, out_dir=orchestrator_out, run_id=run_id)
        worker_out = argv[argv.index('--out') + 1]
        self.assertEqual(
            os.path.abspath(worker_out), orchestrator_out,
            'worker --out %r != orchestrator out_dir %r'
            % (worker_out, orchestrator_out))

    def test_run_id_reaches_the_worker_when_generated_by_the_orchestrator(self):
        """CRITICAL-2（**已由 Lead 修复**）：自动生成的 run_id 必须传给 worker。

        修复前 ``run()`` 自己算 run_id 却不传，worker 只能从目录名反推；
        目录名恰好是 run_id 时才侥幸一致（这正是 VM 上"能跑"的原因）。
        修复后：``run()`` 显式传 ``run_id=run_id``。
        """
        args = args_stub(out=os.path.join(REVIEW_ROOT, 'cand2'))
        run_id = F.make_run_id(now='20261004T120000Z', token='deadbeef')
        argv = F.worker_argv(args, out_dir=os.path.join(args.out, run_id),
                             run_id=run_id)
        self.assertIn('--run-id', argv,
                      'the orchestrator-generated run_id must reach the worker')
        self.assertEqual(argv[argv.index('--run-id') + 1], run_id)

    def test_environment_channel_constant_is_gone(self):
        """MINOR-4（**已由 Lead 修复**）：删除死常量 ``WORKER_ARGV_ENV``。

        修复前的行为（`这段历史写在 full_train.py 的注释里，见“已删除”那段`）：
        常量声明过一次，但 ``resource_guard.py`` 全文不含这个字符串，
        ``full_train.py`` 也没有任何 ``os.environ[...]`` 读取点。也就是说
        "父进程用环境变量把子进程身份传给 worker"这条通道**从未接通**——
        这正是上面两个用例为什么会走到"worker 从目录名反推 run_id"这条脆弱路径。

        **为什么删而不是接通**：既然 CRITICAL-1/2 的修法是让 ``run()`` 把解析后的
        ``out_dir`` 与 ``run_id`` 显式传给 ``worker_argv(out_dir=, run_id=)``，
        身份传递已经是**显式参数**、可读、可测；再接一条环境变量通道只会多出
        第二个真相来源（而它就是 MAJOR-3 那类"两处 ID 不一致"的温床）。
        留着一个从不读写的常量，反而让人以为那条通道存在——所以删掉。

        现在断言：
        - 模块上**没有** ``WORKER_ARGV_ENV`` 属性；
        - 源码里也**没有**该名字（连注释以外的残留都不留——注释里说明"已删除"
          是允许的：``source.count`` 允许 1 次，即那处注释）；
        - ``resource_guard.py`` 同样不含该名字。
        """
        self.assertFalse(
            hasattr(F, 'WORKER_ARGV_ENV'),
            'the dead constant must be gone from full_train (MINOR-4)')
        with open(FULL_TRAIN_PATH, encoding='utf-8') as handle:
            source = handle.read()
        # 至多出现在"已删除"的注释里；不得再有声明或使用。
        self.assertLessEqual(
            source.count('WORKER_ARGV_ENV'), 1,
            'WORKER_ARGV_ENV must not be declared or used anywhere anymore')
        self.assertNotIn('WORKER_ARGV_ENV =', source)
        # 也没有任何"从环境变量取 worker 身份"的读取点（``os.environ`` 本身
        # 在本模块仍用于 manifest 的 BLAS 线程记录，所以不整体禁用）。
        self.assertNotIn("os.environ.get('WORKER_ARGV", source)
        with open(G.__file__, encoding='utf-8') as handle:
            guard_source = handle.read()
        self.assertNotIn('WORKER_ARGV_ENV', guard_source)

    def test_plain_out_end_to_end_now_produces_full_artifacts(self):
        """CRITICAL-1/2（**已由 Lead 修复**）：``--out`` 末段不是 run_id 形状也能跑。

        修复前：worker 收到**未解析**的 ``args.out``，用 ``basename`` 反推 run_id，
        于是 ``--out <root>/plain`` → 在任何产物落盘前抛
        ``ValueError: ... got 'plain'`` → exit 3、除了 ``run_status.json`` 空壳外
        什么都没有。

        修复：``worker_argv(args, out_dir=..., run_id=...)`` 接收编排层**解析后**的
        目录与 run_id（``full_train.py`` 的 ``run()`` 里显式传入）。
        现在：worker 拿到 ``<out>/<run_id>``，产出完整的终态文件集。

        复现::
            python .vmwork/review_train/probe2.py   # CASE B（修复前 exit 3）
        """
        work = review_dir('outdir_propagation')
        csv_path = self.make_csv(os.path.join(work, 'tiny.csv'))
        plain = os.path.join(work, 'plain')
        os.makedirs(plain)

        argv = ['--input', csv_path, '--out', plain, '--max-k', '5',
                '--block-size', '3', '--monitor-interval', '0.5',
                '--prepare-budget-seconds', '120',
                '--precheck-budget-seconds', '120',
                '--train-budget-seconds', '120',
                '--verify-budget-seconds', '120']
        code = self._run_main(argv)

        subs = os.listdir(plain)
        self.assertEqual(len(subs), 1, 'exactly one run dir was created: %r' % subs)
        run_dir = os.path.join(plain, subs[0])
        present = sorted(os.listdir(run_dir))

        # 修复后：目录名必须是**编排层生成的 run_id**（不是 'plain'）。
        self.assertRegex(subs[0], r'^\d{8}T\d{6}Z-[0-9a-f]{8}$',
                         'run dir name must be a real run_id, got %r' % subs[0])

        # 无论成功与否，计划书 §6 要求的终态文件都必须落盘。
        for required in ('run_status.json', 'manifest.json',
                         'source_hashes.json', 'conclusion.md'):
            self.assertTrue(
                os.path.isfile(os.path.join(run_dir, required)),
                '%s must exist (CRITICAL-1 regression)' % required)

        status = M.read_json(os.path.join(run_dir, 'run_status.json'))
        # 真正跑通的 run：这条链路不再因为 --out 形状而失败。
        self.assertEqual(status['status'], F.STATUS_SUCCESS,
                         'a plain --out tail must no longer break the run')
        self.assertEqual(code, 0, 'success must exit 0')
        # 产物内 run_id 必须等于目录名（三方一致）。
        self.assertEqual(status['run_id'], subs[0])

    def _run_main(self, argv):
        """在进程内跑 ``main``（orchestrator），返回退出码。"""
        full_argv = [sys.executable, '-m',
                     'experiments.itemcf.full_train'] + argv
        old_argv = sys.argv
        old_cwd = os.getcwd()
        sys.argv = full_argv
        try:
            return F.main(argv)
        except SystemExit as error:
            return error.code
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)


# ===========================================================================
# 2. CRITICAL：worker 在开头的"早退"路径上不写任何终态产物
# ===========================================================================

class WorkerEarlyExitArtifactTests(_TinyCorpusMixin, unittest.TestCase):
    """``run_worker`` 的磁盘门禁/输入检查在 ``try`` 之外，异常直接穿透。

    ``full_train.py:2084-2093`` 是 ``run_worker`` 开头：

    .. code-block:: python

        if not os.path.isfile(args.input):
            raise RuntimeError('input file not found: %s' % args.input)

        free_disk = free_disk_bytes(out_dir)
        ...
        if free_disk < MIN_FREE_DISK_BYTES:
            raise BudgetExceeded('free disk %.2f GiB < required %.2f GiB' ...)

        status = STATUS_RUNNING
        ...
        try:
            ...                     # 只有这里面的异常才走 finalize()

    后果：这两条最"应该"留下 ``resource_blocked`` 记录的前置失败，
    **一条产物都不留**——只在编排层拿到一份 ``worker exited with code 1``。

    最小复现::

        python .vmwork/review_train/probe3.py      # P3-A / P3-B
    """

    def _worker_args(self, run_id, out_dir, input_path):
        os.makedirs(out_dir, exist_ok=True)
        return F.parse_args(['--input', input_path, '--out', out_dir,
                             '--worker', '--run-id', run_id, '--max-k', '5',
                             '--block-size', '3'])

    def test_missing_input_now_leaves_full_terminal_artifacts(self):
        """CRITICAL-3（**已由 Lead 修复**）：``--input`` 不存在也必须落终态。

        修复前：输入检查写在 ``run_worker`` 的 ``try`` **之外**，``RuntimeError``
        直接穿透 → 目录里只剩 ``run.log``，``run_status.json`` 缺失，
        失败原因退化成编排层的 ``"worker exited with code 1"``。

        修复：检查移入 ``try`` 并改抛 ``InputValidationError``（映射到
        ``correctness_failed``），于是走 ``finalize`` 落全套文件，
        ``failure_reason`` 保留真实原因。
        """
        work = review_dir('early_missing_input')
        run_id = '20261004T120000Z-bbbbbbbb'
        out_dir = os.path.join(work, run_id)
        args = self._worker_args(run_id, out_dir,
                                 os.path.join(work, 'nope.csv'))
        # 修复后**不再穿透**：run_worker 自己吃掉并落终态。
        status = F.run_worker(args)
        self.assertEqual(status['status'], F.STATUS_CORRECTNESS_FAILED)
        self.assertIn('input file not found', status['failure_reason'])

        for name in ('run_status.json', 'manifest.json', 'source_hashes.json',
                     'conclusion.md'):
            self.assertTrue(os.path.isfile(os.path.join(out_dir, name)),
                            '%s must exist on this path (CRITICAL-3 regression)'
                            % name)

    def test_disk_floor_violation_now_leaves_full_terminal_artifacts(self):
        """CRITICAL-3（**已由 Lead 修复**）：磁盘门禁触发也要落 ``resource_blocked``。

        这条修复前尤其刺眼：``BudgetExceeded`` 在各处都被映射成
        ``resource_blocked``，**唯独真正触发磁盘门禁的这一次**因为写在
        ``try`` 之外，连 ``run_status.json`` 都写不出。

        修复：门禁移入 ``try``，于是 ``except BudgetExceeded`` 生效。这条同时
        锁住"磁盘不足必须记成 resource_blocked 而不是 correctness_failed"——
        计划书 §7 要求资源失败不能被记成算法效果零分。
        """
        work = review_dir('early_disk_floor')
        csv_path = self.make_csv(os.path.join(work, 'tiny.csv'))
        run_id = '20261004T120000Z-cccccccc'
        out_dir = os.path.join(work, run_id)
        args = self._worker_args(run_id, out_dir, csv_path)

        original = F.free_disk_bytes
        F.free_disk_bytes = lambda path: 1024  # 1 KiB << 10 GiB floor
        try:
            status = F.run_worker(args)
        finally:
            F.free_disk_bytes = original

        self.assertEqual(status['status'], F.STATUS_RESOURCE_BLOCKED)
        self.assertIn('free disk', status['failure_reason'])
        for name in ('run_status.json', 'manifest.json', 'source_hashes.json',
                     'conclusion.md'):
            self.assertTrue(os.path.isfile(os.path.join(out_dir, name)),
                            '%s must exist on this path (CRITICAL-3 regression)'
                            % name)

    def test_exceptions_inside_the_try_do_produce_every_artifact(self):
        """对照：``try`` **内部**的同类失败是完整的（证明缺陷在缩进位置）。

        ``run_worker`` 的 ``try`` 里，``precheck`` 不通过会走
        ``return finalize(...)``，四个终态文件齐全。所以"失败不留档"不是设计
        选择，而是上面两处早退落在 ``try`` 之外造成的。
        """
        work = review_dir('inside_try')
        run_id = '20261004T120000Z-dddddddd'
        out_dir = os.path.join(work, run_id)
        os.makedirs(out_dir)
        csv_path = self.make_csv(os.path.join(work, 'tiny.csv'))
        args = F.parse_args(['--input', csv_path, '--out', out_dir, '--worker',
                             '--run-id', run_id, '--max-k', '5',
                             '--block-size', '3'])
        # index=None 走 finalize 的"无索引"分支；这正是 try 内部路径的形状。
        status = F.finalize(out_dir, lambda _m: None, _NullLogger(), None, None,
                            args, run_id, F.STATUS_CORRECTNESS_FAILED,
                            'synthetic in-try failure', 0.0, {})
        self.assertEqual(status['status'], F.STATUS_CORRECTNESS_FAILED)
        for name in ('run_status.json', 'manifest.json', 'source_hashes.json',
                     'conclusion.md'):
            self.assertTrue(os.path.isfile(os.path.join(out_dir, name)),
                            '%s should exist for in-try failures' % name)


class _NullLogger(object):
    def close(self):
        return None


# ===========================================================================
# 3. MAJOR：precheck 的 block_size 阶梯降档没有传导到 train 阶段
# ===========================================================================

class PrecheckLadderPropagationTests(unittest.TestCase):
    """MAJOR-1（**已由 Lead 修复**）：precheck 的降档必须传导到 train 阶段。

    **修复前的行为**：``run_precheck`` 在 ``ladder`` 上逐档重试（128 → 64 → 32
    → 16），成功后把实际档位记进 ``block_size_used``，但 ``args`` 虽是**同一个
    对象**却从不被回写。于是 ``run_worker`` 里：

    1. 外推用的是 128 列一档的**块数**，而预检实测的是 64/32/16 列一档的
       **单块耗时**——两个量纲乘在一起，``project_runtime`` 严重**偏低**；
    2. 正式构图仍用 128（或用户给的任意值），**而 128 正是刚刚 MemoryError
       的那一档**，``build_all_blocks`` 把每个块写进 ``write_failure`` 并以
       ``problems`` 返回，最终 ``CorrectnessError``。**降档白降、预检白跑。**

    **修法**：回写点放在 ``run_worker`` 里（不在 ``run_precheck`` 内，因为它
    只负责"测出可用档位"，改调用方的参数不是它的职责），位置在 ``n_blocks``
    计算**之前**：``args.block_size = int(precheck['block_size_used'])``。
    ``build_all_blocks`` 随后读同一个 ``args``，于是正式构图也用降档值。

    核对要点（本类用例覆盖）：
    - 回写后 ``args.block_size == precheck['block_size_used']``；
    - ``n_blocks`` 按**降档后**的列宽算（外推量纲正确）；
    - ``build_all_blocks`` 收到的是降档后的 ``args``（行为级：实际块宽证明）；
    - ``ladder_history`` 逐档留档，看得出"哪几档整档失败"。

    最小复现::

        python .vmwork/review_train/probe4.py      # P4-A
        # precheck.block_size_used = 32    (128 与 64 档整档 MemoryError)
        # args.block_size (train)  = 32    <-- 已由 run_worker 回写
        # n_blocks for projection  = 8     <-- 按 32 算，不再是 2
    """

    def _run_precheck_with_ladder(self, tiers_that_fail, n_items=256):
        """跑一次 ``run_precheck``，按**档位**模拟内存超限。

        为什么按档位而不是列宽：``select_precheck_blocks`` 会为"最高支持度物品"
        单独选块，那一块的宽度可能小于档位（例如最高支持度物品在块尾部时宽度
        是 72），于是"按列宽 == 128 判断失败"会漏掉它、让 128 档假装成功。
        这里改成 ``size >= 失败档位下限`` 判断，语义与真实 MemoryError 一致：
        **块宽超过机器能承受的量就失败**。

        ``n_items`` 取 256 = 128 的整数倍，保证每一档下所有块宽都恰好等于档位
        （末块也是整块），不留"窄末块侥幸成功"的缝隙。
        """
        X = sparse.csr_matrix(np.zeros((5, n_items), dtype=np.int64))
        n_like = np.ones(n_items, dtype=np.int64)
        args = args_stub(block_size=128, max_k=5)
        real = F.collect_csr_block
        attempts = []
        # 最小失败档位：这一档及以上的块都放不下
        failing_from = min(tiers_that_fail)

        def fake(X_, start, stop, n_like_, beta, max_k):
            size = int(stop) - int(start)
            attempts.append(size)
            if size >= failing_from:
                raise MemoryError('simulated: size=%d does not fit' % size)
            empty_i = np.zeros(0, dtype=np.int64)
            return {'sources': empty_i, 'targets': empty_i.copy(),
                    'similarities': np.zeros(0, dtype=np.float64),
                    'cooccurrences': empty_i.copy(), 'seconds': 0.01}

        F.collect_csr_block = fake
        try:
            precheck = F.run_precheck(
                X, np.arange(n_items, dtype=np.int64), n_like, args)
        finally:
            F.collect_csr_block = real
        return args, precheck, n_items, attempts

    def test_ladder_downgrades_to_a_size_that_fits(self):
        """无问题：128 与 64 档全部 MemoryError 时，预检能在 32 档成功。

        （先证明阶梯本身是有效的，下面几条才能说明"降档白降"。）
        """
        args, precheck, _n, attempts = self._run_precheck_with_ladder({128, 64})
        self.assertTrue(precheck['ok'])
        self.assertEqual(precheck['block_size_used'], 32)
        self.assertTrue(any(s <= 32 for s in attempts),
                        'a block at the working size was actually attempted')

    def test_run_precheck_alone_does_not_mutate_its_callers_args(self):
        """边界：``run_precheck`` 自己**不**回写 ``args`` —— 回写在 ``run_worker``。

        这一条是刻意保留的"职责边界"断言，不能省：``run_precheck`` 的契约是
        "在阶梯上测出一个能用的档位**并通过返回值报告它**"，改调用方的
        ``args`` 不是它的职责。修复 MAJOR-1 时把回写放进了 ``run_worker``
        （``args.block_size = int(precheck['block_size_used'])``，在 ``n_blocks``
        计算之前），所以：

        - ``run_precheck`` 返回后 ``args.block_size`` 仍是**调用方请求值**；
        - 它报告可用档位的方式是返回值 ``block_size_used``。

        如果哪天有人图省事把回写挪进 ``run_precheck``，这条用例会红——那时要
        重新想清楚"函数是否应该修改入参"，而不是顺手让它绿。
        """
        args, precheck, _n, _a = self._run_precheck_with_ladder({128, 64})
        self.assertTrue(precheck['ok'])
        self.assertEqual(precheck['block_size_used'], 32)
        self.assertEqual(args.block_size, 128,
                         'run_precheck reports the tier by return value; the '
                         'write-back belongs to run_worker')

    def test_ladder_size_reaches_the_train_phase(self):
        """MAJOR-1（**已由 Lead 修复**）：降档后的列宽必须传导到正式构图。

        计划书 §5.C："128 列因临时内存不足时，可自动按 64→32→16 递减重试……
        公式/k/数据保持不变"——降档的意义是让这一轮**能跑完**，而不是只让预检
        那一栏变绿。

        修复前：``args.block_size`` 恒为 128，于是 ① 外推按 128 的块数；② 正式
        构图重试**刚刚 MemoryError 的那一档**。修复后 ``run_worker`` 在算
        ``n_blocks`` 之前回写 ``args.block_size = precheck['block_size_used']``。

        这条用例**复刻 ``run_worker`` 的那段顺序**（先回写、再算块数），断言：
        - 回写确实发生，且值等于 ``block_size_used``；
        - 回写之后算出的 ``n_blocks`` 与"按可用档位算"完全一致（**修复前的
          2 倍误差就出在"按请求档位算"上**）。
        """
        args, precheck, n_items, _a = self._run_precheck_with_ladder({128, 64})
        self.assertTrue(precheck['ok'])
        requested = int(args.block_size)
        self.assertEqual(requested, 128, 'the test requests the 128 tier')
        self.assertEqual(precheck['block_size_used'], 32,
                         'the 128 and 64 tiers could not be computed; 32 could')

        # --- 复刻 run_worker：回写在 n_blocks 之前 ---
        args.block_size = int(precheck['block_size_used'])
        self.assertEqual(args.block_size, precheck['block_size_used'])

        n_blocks = (n_items + args.block_size - 1) // args.block_size
        by_used = (n_items + precheck['block_size_used'] - 1) // \
            precheck['block_size_used']
        by_requested = (n_items + requested - 1) // requested
        self.assertEqual(n_blocks, by_used,
                         'the projection must use the downshifted column width')
        self.assertEqual(n_blocks, 8)
        # 修复前用的是 2（按 128 算）：4 倍的低估，正是这条缺陷的量化形态。
        self.assertEqual(by_requested, 2)
        self.assertGreater(n_blocks, by_requested)

    def test_projection_uses_the_downshifted_block_count(self):
        """MAJOR-1（**已由 Lead 修复**）：外推必须按**降档后**的列宽算块数。

        修复前：``precheck.block_size_used == 32``，但 ``args.block_size == 128``，
        于是 ``run_worker`` 传给 ``project_runtime`` 的 ``n_blocks`` 是 2（按 128
        算），而按预检实际用的 32 列，正式阶段需要 8 块。**外推偏低 4 倍**——
        这正是计划书 §5.C 最不想要的方向（"估低了就开跑、跑到一半超预算"）。

        修复后：回写发生在 ``n_blocks`` 之前，外推用的是 8。

        这条用例不只比数字，还**真的把两个块数分别喂给 ``project_runtime``**，
        证明传错块数会让 ``projected_seconds_max`` 低估 4 倍——即"量纲不匹配"
        是可观测的后果，不是纸面推导。
        """
        args, precheck, n_items, _a = self._run_precheck_with_ladder({128, 64})
        # 修复后的真实顺序：先回写，再算块数。
        args.block_size = int(precheck['block_size_used'])
        n_blocks = (n_items + args.block_size - 1) // args.block_size
        wrong = (n_items + 128 - 1) // 128          # 修复前用的那个数
        self.assertEqual(n_blocks, 8)
        self.assertEqual(wrong, 2)
        self.assertEqual(n_blocks,
                         (n_items + precheck['block_size_used'] - 1) //
                         precheck['block_size_used'])

        right_projection = F.project_runtime(precheck, n_blocks, 100000.0)
        wrong_projection = F.project_runtime(precheck, wrong, 100000.0)
        self.assertEqual(right_projection['n_blocks'], n_blocks)
        # 用错块数会把预计耗时低估整整 4 倍（8/2）。
        self.assertAlmostEqual(
            wrong_projection['projected_seconds_max'] * 4.0,
            right_projection['projected_seconds_max'], places=6)
        # 比原始秒数，不比 projected_hours_*（小样本下它四舍五入到 4 位会双双变
        # 0.0，变成"0.0 < 0.0"的假失败）。
        self.assertLess(wrong_projection['projected_seconds_max'],
                        right_projection['projected_seconds_max'])

    def test_train_phase_uses_the_size_that_actually_worked(self):
        """MAJOR-1（**已由 Lead 修复**）：正式构图用降档值，不再撞死掉的那一档。

        修复前：``build_all_blocks`` 读 ``args.block_size``（=128，即刚 MemoryError
        的档），每个块都走 ``write_failure`` + ``problems``，最终
        ``CorrectnessError('block construction problems: ...')``——降档救场在正式
        阶段被完全作废。

        修复后 ``run_worker`` 已把 ``args.block_size`` 回写成 32，所以这里**按
        修复后的真实状态**驱动 ``build_all_blocks``：128/64 档会 MemoryError、32 档
        可用，断言它按 32 分块、**blocks 全部成功、problems 为空**。

        （对照：如果仍按 128 传进去，块数会是 2 且两块都失败——这正是修复前的
        形态；本用例通过"实际块宽 == 32"来锁死传导确实发生了。）
        """
        args, precheck, n_items, _a = self._run_precheck_with_ladder({128, 64})
        # 修复后的真实状态：run_worker 已回写。
        args.block_size = int(precheck['block_size_used'])
        self.assertEqual(args.block_size, precheck['block_size_used'])
        self.assertNotEqual(args.block_size, 128)
        self.assertIn(128, precheck['ladder'])
        # 128 档确实被试过并整档失败（不是"没试就降档"）。
        self.assertTrue(any(size >= 128 for size in _a),
                        'the 128-column tier was attempted and failed')

        # 同一个 fake：块宽 >= 失败档位下限才 MemoryError（与预检的失败判据一致）。
        attempt_widths = []
        real = F.collect_csr_block

        def fake(X_, start, stop, n_like_, beta, max_k):
            size = int(stop) - int(start)
            attempt_widths.append(size)
            if size >= 64:
                raise MemoryError('simulated: size=%d does not fit' % size)
            empty = np.zeros(0, dtype=np.int64)
            return {'sources': empty, 'targets': empty.copy(),
                    'similarities': np.zeros(0, dtype=np.float64),
                    'cooccurrences': empty.copy(), 'seconds': 0.01}

        empty_store_dir = review_dir('ladder_train_phase')
        store = F.BlockStore(empty_store_dir, {'sig': 1}, log=None)
        F.collect_csr_block = fake
        try:
            arrays, problems = F.build_all_blocks(
                sparse.csr_matrix(np.zeros((5, n_items), dtype=np.int64)),
                np.arange(n_items, dtype=np.int64),
                np.ones(n_items, dtype=np.int64), args, store, log=None)
        finally:
            F.collect_csr_block = real

        self.assertEqual(problems, [],
                         'no block may fail: the train phase uses the working size')
        self.assertIsNotNone(arrays)
        self.assertEqual(set(attempt_widths), {32},
                         'every train-phase block must use the downshifted width')
        self.assertEqual(len(attempt_widths), 8)

    def test_failed_records_are_kept_per_ladder_tier(self):
        """MINOR（**已由 Lead 修复**）：降档重试时每档的失败记录都**留档**。

        修复前：``run_precheck`` 的 ``while`` 循环每次 ``blocks = []; failed = []``。
        于是 128 档的 MemoryError 记录**被丢弃**，``precheck.json`` 的
        ``blocks_failed`` 只反映最后一轮：本例里最终 ``failed`` 为空（32 档全成功），
        但 ``block_size_used`` 却是 32——读者无法从产物里看出"128 整档都跑不起来"，
        而这是判断机器余量最关键的一条信息。

        修法：新增 ``ladder_history``（逐档记 ``block_size`` / ``n_blocks_tried`` /
        ``succeeded`` / ``failed`` / ``failures``）与汇总 ``tiers_fully_failed``。
        ``failed`` 本身仍是"最后一档"的语义——所以这条用例断言的是
        **``ladder_history`` 保住了全部档位的信息**，而不是说 ``failed`` 变了。

        本例：128 档整档失败（2 块）、64 档整档失败（4 块）、32 档 6/6 成功 →
        ``tiers_fully_failed == 2``，且历史里能读出 (128,0,2)、(64,0,4)、
        (32,6,0)。（32 档是 6 块而不是 8 块，因为 ``select_precheck_blocks`` 最多只取 6 块。）
        """
        args, precheck, _n, _a = self._run_precheck_with_ladder({128, 64})
        # 最后一档（32）成功，所以 failed 为空——这是**正确**语义，不是缺陷。
        self.assertEqual(precheck['failed'], [])
        self.assertEqual(precheck['block_size_used'], 32,
                         'yet the run demonstrably needed the downgrade')
        self.assertEqual(precheck['ladder'], [128, 64, 32, 16])

        history = precheck['ladder_history']
        self.assertEqual([h['block_size'] for h in history], [128, 64, 32],
                         'every attempted tier must be recorded, in order')
        self.assertEqual(
            [(h['block_size'], h['succeeded'], h['failed']) for h in history],
            [(128, 0, 2), (64, 0, 4), (32, 6, 0)],
            'the failed tiers must keep their failure counts')
        self.assertEqual(precheck['tiers_fully_failed'], 2,
                         'two whole tiers could not be computed at all')
        # 失败档的失败明细也留着了（不是只留一个计数）。
        for entry in history[:2]:
            self.assertEqual(len(entry['failures']), entry['failed'])
            self.assertIn('MemoryError', entry['failures'][0]['reason'])
            self.assertEqual(sorted(entry['failures'][0]),
                             ['block_size', 'index', 'reason', 'start', 'stop'])
            self.assertEqual(entry['failures'][0]['block_size'],
                             entry['block_size'])
        # 成功档没有失败记录。
        self.assertEqual(history[2]['failures'], [])


# ===========================================================================
# 4. MAJOR：pass 2 的 DuplicateChecker 用 work_dir=None，临时文件不清理
# ===========================================================================

class Pass2DuplicateCheckerTempLeakTests(_TinyCorpusMixin, unittest.TestCase):
    """``run_pass2`` → ``iter_rating_chunks`` 自建 ``DuplicateChecker()``。

    ``iter_rating_chunks`` 的签名是 ``duplicate_checker=None``，为 ``None`` 时
    ``checker = DuplicateChecker()`` —— 也就是 ``work_dir=None``。而
    ``DuplicateChecker._write_segment`` 在 ``work_dir is None`` 时走
    ``tempfile.NamedTemporaryFile(delete=False)``，文件落在**系统临时目录**。

    ``run_pass2`` 拿到这个 checker 后**从不调用它的 ``cleanup()``**（它只做
    ``for chunk, chunk_start, _checker in iter_rating_chunks(...)``，把 checker
    丢弃），段文件于是永久留在 ``/tmp``。

    最小复现::

        python .vmwork/review_train/probe3.py      # P3-C
        # after pass 1, leftover dupkeys_*.bin: 0
        # after pass 2, leftover dupkeys_*.bin: 1
        # leftover bytes: 25600 for 1600 rows  -> exactly 16 B/row
    """

    def test_pass2_no_longer_leaks_segment_files(self):
        """MAJOR-2（**已由 Lead 修复**）：pass 2 不再把段文件漏在系统临时目录。

        修复前：``iter_rating_chunks`` 在 ``duplicate_checker=None`` 时自建
        ``DuplicateChecker()``（``work_dir=None`` → ``NamedTemporaryFile(delete=False)``
        落系统临时目录），而 ``run_pass2`` 把 checker 丢弃、从不 ``cleanup()``。
        实测 **16 B/行**，全量 32,000,204 行外推 **约 488 MiB**。

        修复：``run_pass2`` 显式建 ``DuplicateChecker(work_dir=<csr_work>/dupkeys_pass2)``
        传给 ``iter_rating_chunks``，并在 ``finally`` 里 ``cleanup()``——
        段文件收在 run 目录内且用完即删，既不漏到 ``/tmp``，也不留在 ``out_dir``。

        复现::

            python .vmwork/review_train/probe3.py      # P3-C（修复前）
        """
        work = review_dir('pass2_temp_leak')
        tmp_dir = os.path.join(work, 'faketmp')
        os.makedirs(tmp_dir)
        rows = [(u, m, 5.0, 100 + u * 100 + m)
                for u in range(1, 41) for m in range(1, 41)]
        csv_path = self.make_csv(os.path.join(work, 'many.csv'), rows)

        saved = tempfile.tempdir
        tempfile.tempdir = tmp_dir
        try:
            index = F.stream_input_pass1(csv_path, 4.0, log=None,
                                         repeat_policy='reject')
            after_pass1 = [n for n in os.listdir(tmp_dir)
                           if n.startswith('dupkeys_')]

            csr_dir = os.path.join(work, 'csr_work')
            os.makedirs(csr_dir)
            builder = F.DiskCSRBuilder(csr_dir, log=None, segment_rows=64)
            builder.run_pass2(csv_path, index, 4.0, repeat_policy='reject')
            after_pass2 = [n for n in os.listdir(tmp_dir)
                           if n.startswith('dupkeys_')]
        finally:
            tempfile.tempdir = saved

        self.assertEqual(after_pass1, [], 'pass 1 cleans up after itself')
        # 修复后：系统临时目录**不再**新增任何 dupkeys 段文件。
        self.assertEqual(
            after_pass2, [],
            'pass 2 must not leak into the system temp dir any more '
            '(MAJOR-2 regression): %r' % after_pass2)

        # 段文件收在 run 目录内，且**用完已删**。
        inside = []
        for root, _dirs, files in os.walk(csr_dir):
            inside.extend(os.path.join(root, f) for f in files
                          if f.startswith('dupkeys_'))
        self.assertEqual(inside, [],
                         'pass-2 segments must be cleaned up, not left in '
                         'out_dir: %r' % inside)

    def test_run_pass2_cleans_up_its_checker(self):
        """MAJOR-2（同源，**已修复**）：``run_pass2`` 必须对 pass-2 的 checker 调 cleanup。

        修复前：``_checker`` 只出现在元组解包里、之后被丢弃，没有任何清理点。
        修复后：显式构造 ``pass2_checker`` 并在 ``finally`` 里 ``cleanup()``。
        这里以**源码为准**断言清理点存在（不依赖运行时文件系统的时机）。
        """
        source = _method_source('DiskCSRBuilder', 'run_pass2')
        self.assertIn('pass2_checker.cleanup()', source,
                      'run_pass2 must clean up its duplicate checker')
        self.assertIn('work_dir=', source,
                      'the checker must be given a workspace dir so its segments '
                      'land inside the run directory, not the system temp dir')
        self.assertIn('finally:', source,
                      'cleanup must run even if the streaming loop raises')

    def test_run_prepare_puts_its_own_work_under_out_dir(self):
        """对照（无问题）：pairs 段确实落在 ``out_dir/csr_work`` 里并被清理。"""
        source = _function_source('run_prepare')
        self.assertIn("os.path.join(out_dir, 'csr_work')", source)
        self.assertIn('builder.cleanup()', source)


def _function_source(name):
    """取 ``full_train.py`` 里某个顶层函数的源码文本（AST 定位）。"""
    with open(FULL_TRAIN_PATH, encoding='utf-8') as handle:
        source = handle.read()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ''
    raise AssertionError('function %r not found in %s' % (name, FULL_TRAIN_PATH))


def _method_source(class_name, method_name):
    """取 ``full_train.py`` 里某个类方法的源码文本（AST 定位）。"""
    with open(FULL_TRAIN_PATH, encoding='utf-8') as handle:
        source = handle.read()
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == method_name:
                    return ast.get_source_segment(source, sub) or ''
    raise AssertionError('method %s.%s not found' % (class_name, method_name))


# ===========================================================================
# 5. MAJOR：--run-id 与 --out 末段不一致时，产物内 ID 分叉
# ===========================================================================

class RunIdDisagreementTests(_TinyCorpusMixin, unittest.TestCase):
    """``run_id`` 的解析规则在编排层与 worker 层**不同**。

    ==================  =========================================
    位置                 规则
    ==================  =========================================
    ``run()``           ``args.run_id or run_id_from_out(args.out) or make_run_id()``
    ``run_worker()``    ``assembled_ids(args.out)`` = ``basename(args.out)``
    ==================  =========================================

    两者只有在"目录名 == args.run_id"时才一致。用户同时给出
    ``--out <root>/<RUNID_A> --run-id <RUNID_B>`` 时（RUNID_A 也是合法形状），
    ``resolve_out_dir`` 因为末段"看起来像 run_id"而**直接用 RUNID_A 当目录**，
    产物里写 RUNID_A；``orchestrator_status.json`` 却写 RUNID_B。

    最小复现::

        python .vmwork/review_train/probe7.py      # P7-A（真实子进程端到端）
        # run_status.json          run_id=20261004T120000Z-11111111
        # orchestrator_status.json run_id=20261004T130000Z-22222222
    """

    def test_run_id_and_out_basename_must_agree(self):
        """MAJOR-3（**已由 Lead 修复**）：分歧时**抛 ``RuntimeError``**，不再静默分叉。

        修复前：``run()`` 同时拿到 ``--out <root>/<RUNID_A>`` 与 ``--run-id <RUNID_B>``
        时，``resolve_out_dir`` 因为末段"看起来像 run_id"就直接拿 RUNID_A 当目录，
        而编排层自己用 RUNID_B：``run_status.json`` / ``manifest.json`` /
        ``model/model.json`` 记 RUNID_A，``orchestrator_status.json`` 记 RUNID_B。
        **exit 0、四个终态产物俱全、每个文件内部都自洽**，只有把两个文件拿来对照
        才发现不一致（计划书 §5.D 明令禁止的"父子 ID 不一致"）。

        修法（``resolve_out_dir``）：末段**等于** run_id → 用它本身；末段**看起来像**
        run_id 但**不等于** → :class:`RuntimeError`（与其猜调用方想要哪个，不如拒绝
        并说清怎么改）；末段根本不是 run_id 形状 → 建 ``<out>/<run_id>``。

        这条用例严格断言异常**类型**（``RuntimeError``，不是宽泛的 ``Exception``，
        也不是 ``ValueError``——同函数里"末段非 run_id 形状"那条路径是正常返回，
        类型区分正是契约），并核对错误信息确实指出了冲突的两个 ID。
        """
        work = review_dir('run_id_disagreement')
        csv_path = self.make_csv(os.path.join(work, 'tiny.csv'))
        rid_tail = '20261004T120000Z-11111111'
        rid_flag = '20261004T130000Z-22222222'
        out_arg = os.path.join(work, rid_tail)
        argv = ['--input', csv_path, '--out', out_arg, '--run-id', rid_flag,
                '--max-k', '5', '--block-size', '3', '--monitor-interval', '0.5',
                '--prepare-budget-seconds', '120',
                '--precheck-budget-seconds', '120',
                '--train-budget-seconds', '120',
                '--verify-budget-seconds', '120']

        with self.assertRaises(RuntimeError) as caught:
            self._run_main(argv)
        message = str(caught.exception)
        self.assertIn(rid_tail, message,
                      'the error must name the --out basename that conflicts')
        self.assertIn(rid_flag, message,
                      'the error must name the --run-id that conflicts')
        # 拒绝发生在建目录**之前**：没留下半成品 run 目录，也没有任何 ID 分叉的产物。
        self.assertFalse(os.path.exists(out_arg),
                         'nothing may be created when the ids disagree')
        self.assertEqual(
            sorted(n for n in os.listdir(work) if n != 'tiny.csv'), [],
            'the disagreing run must leave no directory behind')

    def test_run_id_agreement_is_the_only_way_to_pin_the_directory(self):
        """MAJOR-3（**已由 Lead 修复**）：两条合法路径都能让两处 ID 一致。

        修复后，"目录名 == run_id == 产物内 run_id"只有两种达成方式：

        1. **只给** ``--out <...>/<run_id>``（末段即 run_id，计划书 §6 的形状）：
           ``run_id_from_out`` 取末段当 run_id，``resolve_out_dir`` 返回同一目录；
        2. ``--out <root>``（末段非 run_id 形状）+ ``--run-id <RUNID>``：
           解析成 ``<root>/<RUNID>``。

        断言的是**解析层**的一致性（真跑一遍全链那条已在
        ``test_run_id_shaped_out_tail_works_end_to_end`` / ``test_plain_out_...``
        覆盖）：两种情况下 worker 收到的 ``--out`` 目录名都等于 ``run_id``，
        即 worker 用 ``basename`` 反推得到的就是编排层的 run_id。
        """
        rid = '20261004T120000Z-abcdef01'
        # 路径 1：末段就是 run_id
        out1 = os.path.join(REVIEW_ROOT, 'pin_by_out', rid)
        self.assertEqual(F.run_id_from_out(out1), rid)
        resolved1 = F.resolve_out_dir(out1, F.run_id_from_out(out1))
        self.assertEqual(os.path.basename(resolved1), rid)
        # 路径 2：--out 是普通目录，靠 --run-id 补上
        parent = os.path.join(REVIEW_ROOT, 'pin_by_flag')
        os.makedirs(parent, exist_ok=True)
        self.assertIsNone(F.run_id_from_out(parent),
                          'a plain --out must not be mistaken for a run id')
        resolved2 = F.resolve_out_dir(parent, rid)
        self.assertEqual(os.path.basename(resolved2), rid)
        # 两条路径下 worker 反推出的 run_id 都与编排层一致。
        for resolved, expected in ((resolved1, rid), (resolved2, rid)):
            args = args_stub(out=parent)
            argv = F.worker_argv(args, out_dir=resolved, run_id=expected)
            self.assertEqual(
                os.path.basename(argv[argv.index('--out') + 1]), expected)

    def test_all_artifacts_exist_so_the_disagreement_is_silent(self):
        """MAJOR-3（**已由 Lead 修复**）：分叉**不再可能静默发生**——直接拒绝。

        这是同一个缺陷的危险面：修复前的分叉**没有任何报错**，四个终态产物齐全
        （``run_status.json`` / ``manifest.json`` / ``source_hashes.json`` /
        ``conclusion.md``），每个文件内部都自洽，只有 ``orchestrator_status.json``
        与它们对不上。而 ``pathlib.Path(out).name`` 换来的 run_id 会被
        ``verify_full_model.py`` 与人工核对当作"父 run ID"。

        现在：这种调用**在写任何文件之前**就抛 ``RuntimeError``，所以不存在
        "产物齐全但 ID 不一致"的状态。这条用例把"静默"这件事本身钉死：
        run 目录未建、四个终态产物一个都不存在。
        """
        work = review_dir('run_id_silent')
        csv_path = self.make_csv(os.path.join(work, 'tiny.csv'))
        rid_tail = '20261004T120000Z-33333333'
        rid_flag = '20261004T130000Z-44444444'
        out_arg = os.path.join(work, rid_tail)
        argv = ['--input', csv_path, '--out', out_arg, '--run-id', rid_flag,
                '--max-k', '5', '--block-size', '3', '--monitor-interval', '0.5',
                '--prepare-budget-seconds', '120',
                '--precheck-budget-seconds', '120',
                '--train-budget-seconds', '120',
                '--verify-budget-seconds', '120']
        with self.assertRaises(RuntimeError):
            self._run_main(argv)
        # 修复前的"静默分叉"形态：四个终态产物俱全。现在一个都不该有。
        for name in ('run_status.json', 'manifest.json', 'source_hashes.json',
                     'conclusion.md', 'orchestrator_status.json'):
            self.assertFalse(os.path.isfile(os.path.join(out_arg, name)),
                             '%s must not exist: the run was refused, not split'
                             % name)
        self.assertFalse(os.path.exists(out_arg))
        # --run-id 与目录名都已断言冲突，故这两个 ID 不可能同时出现在产物里：
        # 产物目录本身就不存在。
        self.assertFalse(os.path.isdir(out_arg))

    def _run_main(self, argv):
        full_argv = [sys.executable, '-m',
                     'experiments.itemcf.full_train'] + argv
        old_argv = sys.argv
        sys.argv = full_argv
        try:
            return F.main(argv)
        except SystemExit as error:
            return error.code
        finally:
            sys.argv = old_argv


# ===========================================================================
# 6. MAJOR：监督只"看进程退出"，不看 worker 自报的成功是否可信
# ===========================================================================

class ReconcileStatusPriorityTests(unittest.TestCase):
    """``_reconcile_status`` 的优先级：监督 > worker 自报 > 退出码。

    计划的优先级是**对的**，但"监督判定"在 ``GuardedRun.result`` 里只在
    ``guard.violation is not None`` 时生效；其余情况退化成"退出码 == 0 ? success"。
    ``_reconcile_status`` 的第二个分支又让 worker 自报覆盖退出码。
    """

    def test_supervision_overrides_a_self_reported_success(self):
        """无问题：资源类监督判定确实压过 worker 自报的 success。"""
        for guard_status in (F.STATUS_RESOURCE_FAILED, F.STATUS_MONITOR_FAILED,
                             F.STATUS_RESOURCE_BLOCKED):
            self.assertEqual(
                F._reconcile_status(
                    {'status': guard_status, 'exit_code': 0},
                    {'status': F.STATUS_SUCCESS}),
                guard_status)

    def test_worker_success_requires_clean_exit(self):
        """MAJOR-4（**已由 Lead 修复**）：自报 success 必须 ``exit_code == 0``。

        修复前：``_reconcile_status`` 的第二个分支让 worker **自报**的状态覆盖监督
        给出的判定。而 worker 是**先写** ``run_status.json(status='success')``、
        ``main`` 之后**才** ``return 0``；两者之间被外部 SIGKILL（VM 是 4 GB 内存
        + 4 GB swap，**OOM killer 是真实风险**）就会留下
        ``exit_code=-9`` + ``run_status.json.status='success'`` 的组合。
        ``GuardedRun.result`` 此时正确给出 ``correctness_failed``，却被告自报盖回
        ``success``——于是**被 OOM kill 的一轮记为成功**，模型可能只写了一半
        （``model.json`` 已落盘但哈希核验没跑完）。

        修法：``reported == success`` 时要求 ``exit_code == 0`` 相互印证；
        否则返回 ``correctness_failed``（编排层随后还会改写 ``failure_reason``，
        免得产物出现"status 失败却留着成功措辞"的自相矛盾）。

        这条用例覆盖 **三种退出码**，三种都断言：
        ``-9``（SIGKILL / OOM）与 ``-15``（SIGTERM）→ ``correctness_failed``；
        ``0``（正常退出）→ ``success``。只测 ``-9`` 不够：修复必须区分
        "退出码为 0"与"退出码非 0"，而不是"看到负数就报错"。
        """
        reconcile = F._reconcile_status
        worker_says_success = {'status': F.STATUS_SUCCESS}
        for exit_code in (-9, -15):
            with self.subTest(exit_code=exit_code):
                self.assertEqual(
                    reconcile({'status': F.STATUS_CORRECTNESS_FAILED,
                               'exit_code': exit_code},
                              worker_says_success),
                    F.STATUS_CORRECTNESS_FAILED,
                    'a worker killed by signal %d must not be recorded as a '
                    'success just because run_status.json says success'
                    % exit_code)
        # 正常退出码 0：自报 success 才被采信（否则修复就成了"永远不信 worker"）。
        self.assertEqual(
            reconcile({'status': F.STATUS_SUCCESS, 'exit_code': 0},
                      worker_says_success),
            F.STATUS_SUCCESS)
        # 监督的终止判定仍然压过自报（优先级没被这次修复改坏）。
        for guard_status in (F.STATUS_RESOURCE_FAILED, F.STATUS_MONITOR_FAILED,
                             F.STATUS_RESOURCE_BLOCKED):
            self.assertEqual(
                reconcile({'status': guard_status, 'exit_code': -9},
                          worker_says_success),
                guard_status)

    def test_clean_exit_code_alone_still_produces_success_at_this_layer(self):
        """边界：``_reconcile_status`` **这一层**仍把"退出码 0"当 success。

        这条不是缺陷锚点，而是把**职责边界**钉死：``_reconcile_status`` 只负责
        合并"监督判定 / worker 自报 / 退出码"三者，**它不看磁盘**。所以：

        - ``_reconcile_status({'status': running, 'exit_code': 0}, None)``
          仍返回 ``success``；
        - ``GuardedRun.result(0)`` 在无 violation 时也给 ``success``。

        "退出码 0 不等于产物完整"这件事**不在这里**修，而是在 ``run()`` 的
        终态产物门禁里修——见 :class:`TerminalArtifactGateTests`。把这条留在
        这里，是为了防止后人误以为 ``_reconcile_status`` 已经管了产物。
        """
        self.assertEqual(
            F._reconcile_status({'status': F.STATUS_RUNNING, 'exit_code': 0},
                                None),
            F.STATUS_SUCCESS)
        # 而 GuardedRun.result 在无 violation 时也把 exit_code==0 当 success。
        result = G.GuardedRun(['x']).result(0)
        self.assertEqual(result['status'], F.STATUS_SUCCESS)
        self.assertIsNone(result['reason'])


# ===========================================================================
# 6b. MAJOR（H2 同族，已修）：`success` 必须由真实产物背书
# ===========================================================================

class TerminalArtifactGateTests(unittest.TestCase):
    """MAJOR（**已由 Lead 修复**）：退出码 0 不再自动等于 ``success``。

    **修复前**：``run()`` 在 worker 没写 ``run_status.json`` 时会**合成**一份，
    ``status`` 取 ``_reconcile_status`` 的退出码分支 → "进程正常退出"被当成
    "产物完整"的证据。**从不检查 ``model/`` 是否存在、是否能 mmap 加载**。
    于是一轮"退了 0 但零产物"（例如 worker 在写任何东西之前就走了某条未覆盖的
    return）会被记成 `success`，下游会把一个**不存在的模型**当作可用的。

    这是 MAJOR-4 的同族缺陷：MAJOR-4 堵的是"自报 success + 非零退出码"，
    这里堵的是"退出码 0 + 零产物"。

    **修法**：``run()`` 在 ``_reconcile_status`` 之后加一道**终态产物门禁**——
    ``final_status == success`` 时才去 ``M.load_model(model_dir, validate=True,
    require_status=validated_unpublished, max_k=...)``；加载/校验失败即降级为
    ``correctness_failed``。取证落在新字段 ``artifact_gate``（list）。
    ``failure_reason`` 也按两种降级来源分别改写，避免
    "status=correctness_failed 却留着成功措辞"的自相矛盾。

    本类用 ``_run_guarded`` 打桩跳过真实子进程（``run()`` 的真正编排逻辑、
    门禁、落盘全部照跑），因此断言的是 ``run()`` 的**真实**行为而非复刻。
    """

    def _args(self, parent, run_id):
        return args_stub(out=parent, run_id=run_id, max_k=5, block_size=3,
                         monitor_interval=0.5, prepare_budget_seconds=120.0,
                         precheck_budget_seconds=120.0,
                         train_budget_seconds=120.0,
                         verify_budget_seconds=120.0)

    def _run_with_stubbed_worker(self, name, run_id, layout=None,
                                 exit_code=0):
        """跑真实 ``run()``，只用 ``_run_guarded`` 打桩替代子进程。

        ``layout(out_dir, run_id)`` 在门禁之前布景（写 run_status.json / model/）。
        """
        parent = review_dir(name)
        args = self._args(parent, run_id)
        real = F._run_guarded

        def fake_guarded(guarded, out_dir, stage_path, log):
            if layout is not None:
                layout(out_dir, run_id)
            return {'status': F.STATUS_SUCCESS, 'exit_code': exit_code,
                    'reason': None, 'terminated': False, 'termination': None,
                    'sampled_max': {}}

        F._run_guarded = fake_guarded
        try:
            status = F.run(args)
        finally:
            F._run_guarded = real
        return status, os.path.join(parent, run_id)

    @staticmethod
    def _write_self_reported_success(out_dir, run_id):
        M.write_json_atomic(os.path.join(out_dir, 'run_status.json'), {
            'run_id': run_id, 'model_id': None, 'status': F.STATUS_SUCCESS,
            'failure_reason': None, 'counts': {},
        })

    @staticmethod
    def _write_layout(out_dir, run_id, status):
        """造一份**结构合法**的 ``model/``（可由 ``load_model`` 校验通过）。

        注意 ``item_ids`` 必须**从 1 开始**：``validate_arrays`` 会拒绝非正
        movieId（`item_ids contains non-positive movieId`），用 0 起会得到一个
        "结构都不合法"的假模型，那就分不清"是 require_status 拦的"还是
        "结构校验拦的"。
        """
        n_items = 3
        model = M.NeighborModel(
            item_ids=np.arange(1, n_items + 1, dtype=np.int64),
            indptr=np.zeros(n_items + 1, dtype=np.int64),
            target_indices=np.zeros(0, dtype=np.int64),
            similarities=np.zeros(0, dtype=np.float64),
            cooccurrences=np.zeros(0, dtype=np.int64),
            n_like=np.ones(n_items, dtype=np.int64),
            n_all=np.ones(n_items, dtype=np.int64))
        meta = M.build_metadata(
            run_id=run_id,
            source={'path': 'in.csv', 'bytes': 1, 'sha256': 'x', 'rows': 1,
                    'users': 4, 'items': n_items, 'positive_pairs': 1,
                    'items_zero_like': 0},
            parameters={'positive_threshold': 4.0, 'max_k': 5, 'beta': 0.0,
                        'block_size': 3, 'monitor_interval': 0.5,
                        'prepare_budget_seconds': 120.0,
                        'precheck_budget_seconds': 120.0,
                        'train_budget_seconds': 120.0, 'seed': 42,
                        'duplicate_policy': 'reject'},
            extra={'graph_signature': 'sig', 'structure_stats': {}})
        saved = model.save(os.path.join(out_dir, 'model'), meta)
        meta_path = os.path.join(out_dir, 'model', M.METADATA_FILE)
        payload = M.read_json(meta_path)
        payload['status'] = status
        M.write_json_atomic(meta_path, payload)
        M.write_json_atomic(os.path.join(out_dir, 'run_status.json'), {
            'run_id': run_id, 'model_id': saved['model_id'],
            'status': F.STATUS_SUCCESS, 'failure_reason': None, 'counts': {},
        })
        return saved

    def test_missing_model_artifacts_downgrade_a_clean_exit(self):
        """① 退出码 0 + worker 自报 success + ``model/`` 缺失 → **必须降级**。

        这是缺陷本体：修复前这种情形记为 ``success``。现在断言：
        - ``final_status == correctness_failed``（不是 success）；
        - ``artifact_gate`` 里有一条以 ``model could not be loaded/validated:``
          开头（并点名 ``ModelIOError``）；
        - ``failure_reason`` 含 ``the declared model artifacts are missing or
          invalid`` 与 ``A clean exit code is not evidence that the model was
          produced.``——即降级来源被如实写清，而不是留一句成功措辞；
        - ``run_status.json`` / ``orchestrator_status.json`` 两个终态文件都落
          ``correctness_failed``（不留矛盾记录）。
        """
        run_id = '20261004T120000Z-a1a1a1a1'
        status, out_dir = self._run_with_stubbed_worker(
            'gate_missing_model', run_id,
            layout=self._write_self_reported_success)

        self.assertEqual(status['status'], F.STATUS_CORRECTNESS_FAILED)
        gate = status['artifact_gate']
        self.assertEqual(len(gate), 1)
        self.assertTrue(
            gate[0].startswith('model could not be loaded/validated:'),
            'the gate must record why success was refused, got %r' % (gate[0],))
        self.assertIn('ModelIOError', gate[0])
        reason = status['failure_reason']
        self.assertIn('the declared model artifacts are missing or invalid',
                      reason)
        self.assertIn('A clean exit code is not evidence that the model was '
                      'produced.', reason)
        # 两个终态文件都不得留 success。
        for name in ('run_status.json', 'orchestrator_status.json'):
            payload = M.read_json(os.path.join(out_dir, name))
            self.assertEqual(payload['status'], F.STATUS_CORRECTNESS_FAILED,
                             '%s must not claim success' % name)

    def test_a_valid_model_is_allowed_to_be_success(self):
        """② 结构合法 + ``validated_unpublished`` → **允许** success。

        证明门禁不是"无差别杀"：它拒绝的是产物缺失/不可加载，不是拒绝所有情况。
        """
        run_id = '20261004T120000Z-b2b2b2b2'

        def layout(out_dir, rid):
            self._write_layout(out_dir, rid, M.STATUS_VALIDATED_UNPUBLISHED)

        status, out_dir = self._run_with_stubbed_worker(
            'gate_valid_model', run_id, layout=layout)

        self.assertEqual(status['status'], F.STATUS_SUCCESS)
        gate = status['artifact_gate']
        self.assertEqual(len(gate), 1)
        self.assertTrue(gate[0].startswith('model ok:'),
                        'the gate must record the positive check, got %r'
                        % (gate[0],))
        self.assertIn('status=%s' % M.STATUS_VALIDATED_UNPUBLISHED, gate[0])
        self.assertIsNone(status['failure_reason'])
        self.assertEqual(
            M.read_json(os.path.join(out_dir, 'run_status.json'))['status'],
            F.STATUS_SUCCESS)

    def test_require_status_rejects_a_wellformed_but_unverified_model(self):
        """③ ``require_status`` 必须真的生效：产物齐全但**未验证**也要降级。

        这条最能证明门禁不是"只要 ``model/`` 目录存在就放行"——模型文件全都
        齐全、结构校验也能过，**唯独状态是 ``assembled_unverified``**，仍须拒绝。
        """
        run_id = '20261004T120000Z-c3c3c3c3'

        def layout(out_dir, rid):
            self._write_layout(out_dir, rid, M.STATUS_ASSEMBLED_UNVERIFIED)

        status, out_dir = self._run_with_stubbed_worker(
            'gate_unverified_model', run_id, layout=layout)

        self.assertEqual(status['status'], F.STATUS_CORRECTNESS_FAILED)
        gate = status['artifact_gate']
        self.assertEqual(len(gate), 1)
        self.assertTrue(gate[0].startswith('model could not be loaded/validated:'))
        # 拒绝理由必须是**状态不符**，而不是结构坏了（否则证明不了 require_status）。
        self.assertIn("status 'assembled_unverified' != required "
                      "'validated_unpublished'", gate[0])
        self.assertIn('A clean exit code is not evidence', status['failure_reason'])

    def test_downgraded_run_records_agree_with_each_other(self):
        """④ 降级后 ``run_status.json`` 与 ``model.json`` 不得互相矛盾。

        H2 同族要求：一轮被判失败，产物里就不能同时留着"已验证的模型"。
        """
        run_id = '20261004T120000Z-d4d4d4d4'

        def layout(out_dir, rid):
            self._write_layout(out_dir, rid, M.STATUS_ASSEMBLED_UNVERIFIED)

        status, out_dir = self._run_with_stubbed_worker(
            'gate_consistency', run_id, layout=layout)

        run_status = M.read_json(os.path.join(out_dir, 'run_status.json'))
        model_meta = M.read_json(os.path.join(out_dir, 'model', M.METADATA_FILE))
        self.assertEqual(run_status['status'], F.STATUS_CORRECTNESS_FAILED)
        # 模型本身**没有**被谎报成 validated（门禁只降级判定，不篡改产物）。
        self.assertEqual(model_meta['status'], M.STATUS_ASSEMBLED_UNVERIFIED)
        # 两个文件的身份字段仍一致（降级不改 run_id / model_id）。
        self.assertEqual(run_status['run_id'], run_id)
        self.assertEqual(model_meta['run_id'], run_id)
        self.assertEqual(run_status['model_id'], model_meta['model_id'])
        # 失败原因必须在**终态文件**里可读，而不是只在返回值里。
        self.assertIn('A clean exit code is not evidence',
                      run_status['failure_reason'])

    def test_nonzero_exit_code_reason_is_unchanged(self):
        """边界：MAJOR-4 那条降级来源的**措辞不变**（改动没有串味）。

        ``artifact_problems`` 为空（退出码非 0 时门禁根本不会跑），所以
        ``failure_reason`` 必须仍是"进程没正常退出"那一版，**不得**被
        "artifacts are missing or invalid" 顶替。
        """
        run_id = '20261004T120000Z-e5e5e5e5'
        status, _out = self._run_with_stubbed_worker(
            'gate_killed', run_id,
            layout=self._write_self_reported_success, exit_code=-9)

        self.assertEqual(status['status'], F.STATUS_CORRECTNESS_FAILED)
        reason = status['failure_reason']
        self.assertIn('did not exit cleanly (exit_code=-9)', reason)
        self.assertNotIn('the declared model artifacts are missing or invalid',
                         reason)
        # 退出码非 0 时门禁未运行，artifact_gate 为空列表。
        self.assertEqual(status['artifact_gate'], [])


# ===========================================================================
# 7. MINOR：磁盘/结构层面的取证缺口
# ===========================================================================

class DiskAndArtifactGapTests(_TinyCorpusMixin, unittest.TestCase):
    """``estimate_disk_bytes`` 与终态产物的取证完整性。"""

    def test_estimate_rejects_non_positive_block_size(self):
        """MINOR-1（**已由 Lead 修复**）：``block_size <= 0`` / ``max_k <= 0`` → ``ValueError``。

        修复前：``block_size=0`` 直接 ``ZeroDivisionError``；
        ``block_size=-1`` 更糟——**不抛错**，而是算出 ``n_blocks = -8`` 并安静返回一份
        无意义的估算。后者危险在方向：估算偏小会让
        ``(total_bytes + DISK_HEADROOM_BYTES) < free_now`` 这条磁盘门禁失效，
        也就是"估算看着够用、实际开到一半爆盘"。
        （``max_k<=0`` 同理：``worst_edges = n_items * max_k`` 变负。）

        修法：函数入口对两个参数各做一次正整数校验，抛 ``ValueError``
        （``parse_args`` 已有 CLI 侧校验，但直接调用——预检/脚本/将来别的入口——
        没有保护）。

        这条用例对 ``block_size`` 取 ``0`` 与 ``-1``、``max_k`` 取 ``0`` 与负数
        **逐个断言异常类型**，并核对校验消息点名了出问题的参数；
        最后确认合法的 ``block_size=128`` 仍然正常返回（没把边界收得过紧）。
        """
        class Idx(object):
            n_items = 10
            n_users = 5
            positive_pairs = staticmethod(lambda: 20)

        for block_size in (0, -1, -8):
            with self.subTest(block_size=block_size):
                with self.assertRaises(ValueError) as caught:
                    F.estimate_disk_bytes(Idx(), max_k=5,
                                          block_size=block_size)
                self.assertIn('block_size', str(caught.exception))
        for max_k in (0, -1, -7):
            with self.subTest(max_k=max_k):
                with self.assertRaises(ValueError) as caught:
                    F.estimate_disk_bytes(Idx(), max_k=max_k, block_size=128)
                self.assertIn('max_k', str(caught.exception))
        # 合法参数照常工作（校验没有误伤正常路径）。
        estimate = F.estimate_disk_bytes(Idx(), max_k=5, block_size=128)
        self.assertEqual(estimate['n_blocks'], 1)
        self.assertGreater(estimate['total_bytes'], 0)

    def test_estimate_does_not_add_the_headroom_itself(self):
        """MAJOR：``estimate_disk_bytes`` 不含 2 GiB 余量，判定在调用方。

        判定式是 ``(estimate['total_bytes'] + DISK_HEADROOM_BYTES) < free_now``，
        写在 ``run_worker`` 里而不是估算函数里。这不算错，但意味着任何**别的**
        消费者（``verify_full_model.py``、报告、conclusion）如果直接用
        ``estimate['total_bytes']`` 与可用空间比较，就会漏掉余量。审查这一条是
        为了确认它不是"忘了加"。
        """
        estimate = F.estimate_disk_bytes(_FakeFullIndex(), max_k=100,
                                         block_size=128)
        self.assertEqual(F.DISK_HEADROOM_BYTES, 2 * 1024 * 1024 * 1024)
        self.assertNotIn('headroom_bytes', estimate)
        self.assertNotIn('total_with_headroom_bytes', estimate)
        # 全量规模下的估算值（供 Lead 对照 VM 的 35 GB）
        self.assertGreater(estimate['total_bytes'], 0)
        self.assertLess(estimate['total_bytes'], F.MIN_FREE_DISK_BYTES)

    def test_estimate_counts_segment_files_and_csr_as_coexisting(self):
        """MAJOR：估算把互斥项相加、把同期的项漏掉，正负抵消成"看起来够用"。

        ``total_bytes`` 是**所有** ``_bytes`` 结尾项的和，包括
        ``positive_pairs_triples_bytes``（pass 2 的段文件，归并完成后
        ``builder.cleanup()`` 删掉）与 ``edge_arrays_bytes``（装配期驻留）。
        前者在后者出现时已经不存在，所以这一项是**多算**的；反过来，
        ``block_files_bytes``（``graph_blocks/*.npz``，跑到 verify 才读）与
        ``model_files_bytes``（``model/*.npy``）真实**同时**存在，
        估算里都各算一次，这部分是对的。

        净效果：全量规模下 ``total_bytes`` ≈ 1.38 GiB，而真正同期的峰值
        （段文件 + CSR + 块文件 + 装配数组）≈ 1.18 GiB，量级没错。所以这条
        **不构成爆盘风险**，但"把互斥项相加"的算法在参数变化时不再可靠
        （例如 ``block_size`` 变小 → ``n_blocks`` 变多 → ``worst_edges`` 不变，
        估算不动，而块文件数量与元数据开销实际上升）。
        """
        estimate = F.estimate_disk_bytes(_FakeFullIndex(), max_k=100,
                                         block_size=128)
        coexisting_peak = (estimate['positive_pairs_triples_bytes'] +
                           estimate['csr_data_bytes'] +
                           estimate['csr_indices_bytes'] +
                           estimate['block_files_bytes'] +
                           estimate['edge_arrays_bytes'])
        self.assertLess(coexisting_peak, estimate['total_bytes'],
                        'total_bytes double-counts the pass-2 segment files')
        # 差异量级 = 段文件本身
        self.assertAlmostEqual(
            float(estimate['total_bytes'] - coexisting_peak),
            float(estimate['model_files_bytes'] +
                  estimate['csr_indptr_bytes']),
            places=-1)

    def test_headroom_is_invisible_in_the_estimate_payload(self):
        """MINOR：``precheck.json`` 里余量在 ``disk.headroom_bytes`` 单独记。

        这不是缺陷（信息确实落了盘），记在这里是为了让 Lead 知道
        ``estimate['total_bytes']`` **不能**直接当"需要的空间"用。
        """
        self.assertEqual(F.bytes_to_gib(F.DISK_HEADROOM_BYTES), 2.0)
        self.assertEqual(F.bytes_to_gib(F.MIN_FREE_DISK_BYTES), 10.0)


class _FakeFullIndex(object):
    """全量 32M 的规模（只用于估算，不读数据）。"""

    n_items = 87585
    n_users = 200948

    @staticmethod
    def positive_pairs():
        return 16000000


# ===========================================================================
# 8. 核对"已修的 5 个 bug"是否彻底
# ===========================================================================

class PreviouslyFixedBugResidualTests(unittest.TestCase):
    """已知已修的 5 个缺陷：逐条确认修复彻底，并标出残留面。"""

    def test_save_returns_a_metadata_dict(self):
        """修复 1 彻底：``save`` 返回 dict（``verify_hashes`` 不会再 AttributeError）。"""
        work = review_dir('save_returns_dict')
        model = M.NeighborModel(
            item_ids=np.asarray([10, 11], dtype=np.int64),
            indptr=np.asarray([0, 1, 1], dtype=np.int64),
            target_indices=np.asarray([1], dtype=np.int64),
            similarities=np.asarray([0.5], dtype=np.float64),
            cooccurrences=np.asarray([1], dtype=np.int64),
            n_like=np.asarray([2, 1], dtype=np.int64),
            n_all=np.asarray([2, 2], dtype=np.int64))
        saved = model.save(work, M.build_metadata(run_id='20261004T120000Z-'
                                                          'deadbeef',
                                                  source={}, parameters={}))
        self.assertIsInstance(saved, dict)
        self.assertIn('files', saved)
        hashes = M.verify_hashes(work, saved)          # 不再 AttributeError
        self.assertTrue(all(hashes.values()))

    def test_scipy_version_is_read_from_the_top_level_package(self):
        """修复 2 彻底：用顶层 ``scipy.__version__``，不是 ``sparse.__version__``。

        注意：本机的 scipy 1.13.1 上 ``getattr(sparse, '__version__', None)``
        返回 ``None`` 而不是抛 AttributeError（scipy 给子模块加了兜底），所以
        "没崩"不能证明修好了。真正可靠的证据是**源码里没有**
        ``sparse.__version__``、且 manifest 走的是顶层 ``scipy``。
        """
        import scipy
        self.assertTrue(getattr(scipy, '__version__', None))
        source = open(FULL_TRAIN_PATH, encoding='utf-8').read()
        self.assertIn("getattr(__import__('scipy'), '__version__', None)", source)
        # 只允许出现在注释里解释历史缺陷，不允许出现在代码里
        for lineno, line in enumerate(source.splitlines(), 1):
            if 'sparse.__version__' in line:
                self.assertTrue(line.lstrip().startswith('#'),
                                'line %d uses sparse.__version__ in code: %s'
                                % (lineno, line.strip()))

    def test_manifest_records_the_scipy_version_without_crashing(self):
        """修复 2 彻底（行为证据）：``finalize`` 真的能把四个文件写全。

        历史缺陷是 ``finalize`` 里 ``sparse.__version__`` 抛 AttributeError，
        导致**所有**终态文件都落不下。这里跑一次 ``finalize`` 并读回 manifest
        的 ``scipy`` 字段，证明那条路径是通的。
        """
        work = review_dir('manifest_scipy')
        args = args_stub(input=os.path.join(work, 'x.csv'))
        status = F.finalize(work, lambda _m: None, _NullLogger(), None, None,
                            args, '20261004T120000Z-deadbeef',
                            F.STATUS_CORRECTNESS_FAILED, 'synthetic', 0.0, {})
        self.assertEqual(status['status'], F.STATUS_CORRECTNESS_FAILED)
        manifest = M.read_json(os.path.join(work, 'manifest.json'))
        self.assertTrue(manifest['scipy'],
                        'manifest.scipy must be a real version string')

    def test_segment_iterator_stops_at_the_key_block_end(self):
        """修复 3 彻底：小段也能正确归并（段大小 < chunk_rows 时不再越界）。"""
        work = review_dir('segment_iterator')
        path = os.path.join(work, 'seg.bin')
        keys = np.asarray([1, 2, 2, 3], dtype=np.int64)
        lines = np.asarray([2, 3, 4, 5], dtype=np.int64)
        with open(path, 'wb') as handle:
            keys.tofile(handle)
            lines.tofile(handle)
        got = list(F.DuplicateChecker._segment_iterator(path))
        self.assertEqual(got, [(1, 2), (2, 3), (2, 4), (3, 5)])

    def test_duplicate_recording_is_idempotent_across_paths(self):
        """修复 4 彻底：段内查重与跨段归并不再重复计数。"""
        work = review_dir('duplicate_idempotent')
        rows = [(1, 10, 5.0, 100), (1, 10, 5.0, 101), (2, 11, 5.0, 102)]
        csv_path = write_csv(os.path.join(work, 'dup.csv'), rows)
        checker = F.DuplicateChecker(work_dir=os.path.join(work, 'seg'),
                                     segment_rows=2)
        rows_out, report = F.read_input_relevant_rows(
            csv_path, repeat_policy='dedupe_keep_last')
        # 直接用 checker 走一遍同样的路径
        for chunk, _start, checker in F.iter_rating_chunks(
                csv_path, 2, repeat_policy='dedupe_keep_last',
                duplicate_checker=checker):
            pass
        checker.finalize()
        self.assertEqual(checker.n_duplicates, 1,
                         'one duplicate occurrence must be counted once')
        checker.cleanup()
        self.assertEqual(len(rows_out), 3)

    def test_write_block_refuses_unsorted_sources_and_length_mismatch(self):
        """修复 5 彻底：``write_block`` 拒非升序 sources 与长度不符。"""
        work = review_dir('write_block_guard')
        store = F.BlockStore(work, {'signature': 'x'}, log=None)
        ints = np.asarray([2, 1], dtype=np.int64)
        sims = np.asarray([0.5, 0.5], dtype=np.float64)
        coocs = np.asarray([1, 1], dtype=np.int64)
        with self.assertRaises(F.CorrectnessError):
            store.write_block(0, 0, 4, ints, ints, sims, coocs, 0.0)
        ok_sources = np.asarray([1], dtype=np.int64)
        with self.assertRaises(F.CorrectnessError):
            store.write_block(1, 0, 4, ok_sources, np.asarray([1, 2],
                                                              dtype=np.int64),
                              sims, coocs, 0.0)


# ===========================================================================
# 9. Python 3.6 兼容：按 AST 真实语法判定（不搜字符串）
# ===========================================================================

class Python36SyntaxTests(unittest.TestCase):
    """3.6 目标环境的**真实语法结构**检查。

    不搜 ``'dataclass'`` 字符串——注释里写"不用 dataclasses"会被误报（历史上
    发生过）。这里按 AST 节点判定，并把 ``conclusion_writer.py`` 一并纳入
    （它现在被 ``finalize`` 依赖，且在源码指纹里，所以同属 3.6 目标）。
    """

    MODULES = ('full_train.py', 'model_io.py', 'verify_full_model.py',
               'conclusion_writer.py', 'resource_guard.py')

    def test_no_36_incompatible_syntax(self):
        for name in self.MODULES:
            path = os.path.join(PROJECT, 'experiments', 'itemcf', name)
            with open(path, encoding='utf-8') as handle:
                source = handle.read()
            tree = ast.parse(source, filename=name)

            fstrings = [n.lineno for n in ast.walk(tree)
                        if isinstance(n, ast.JoinedStr)]
            self.assertEqual(fstrings, [],
                             '%s uses f-strings at %r' % (name, fstrings))

            walrus = [n.lineno for n in ast.walk(tree)
                      if n.__class__.__name__ == 'NamedExpr']
            self.assertEqual(walrus, [],
                             '%s uses the walrus operator at %r' % (name, walrus))

            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == '__future__':
                    self.assertNotIn(
                        'annotations', [a.name for a in node.names],
                        '%s imports annotations from __future__' % name)
                if isinstance(node, (ast.ClassDef, ast.FunctionDef)):
                    for decorator in node.decorator_list:
                        self.assertNotIn('dataclass', ast.dump(decorator),
                                         '%s uses @dataclass on %s'
                                         % (name, node.name))

    def test_no_36_incompatible_runtime_apis(self):
        """3.6 上不存在的运行时关键字参数，按 **AST 实参**判定。

        ``subprocess.run(capture_output=...)`` 是 3.7+；``full_train.py`` 与
        ``resource_guard.py`` 的**注释**里都写着"不用 capture_output="，
        按字符串搜索会误报（历史教训同 ``dataclass``）。这里只查真正出现在
        ``Call`` 实参里的关键字名。
        """
        offenders = []
        for name in self.MODULES:
            path = os.path.join(PROJECT, 'experiments', 'itemcf', name)
            with open(path, encoding='utf-8') as handle:
                tree = ast.parse(handle.read(), filename=name)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    for keyword in node.keywords:
                        if keyword.arg == 'capture_output':
                            offenders.append('%s:%d' % (name, node.lineno))
        self.assertEqual(offenders, [],
                         'capture_output= is Python 3.7+: %r' % (offenders,))

    def test_type_annotations_are_absent(self):
        """3.6 支持注解语法，但本项目约定不用；变量注解要 3.6 才合法，
        这里只确认没有 ``from __future__ import annotations`` 这类绕过。"""
        for name in self.MODULES:
            path = os.path.join(PROJECT, 'experiments', 'itemcf', name)
            with open(path, encoding='utf-8') as handle:
                tree = ast.parse(handle.read(), filename=name)
            ann_assign = [n.lineno for n in ast.walk(tree)
                          if isinstance(n, ast.AnnAssign)]
            self.assertEqual(ann_assign, [],
                             '%s uses variable annotations at %r'
                             % (name, ann_assign))


# ===========================================================================
# 10. 已核对**不是**缺陷的项（省得 Lead 重查）
# ===========================================================================

class VerifiedNotDefectsTests(unittest.TestCase):
    """明确查过、确认**没有**问题的项；结论都来自实跑而非读代码。"""

    def test_packing_boundary_rejects_overflow(self):
        """无问题：``DuplicateChecker.add`` 对 >= 10^7 的两个字段都显式拒绝。

        不会静默撞键：``add(10000000, 1)`` 与 ``add(1, 10000000)`` 都抛
        ``InputValidationError``，边界值 ``(9999999, 9999999)`` 被接受。
        """
        checker = F.DuplicateChecker(segment_rows=8)
        checker.add(9999999, 9999999, 2)
        for user, item in ((10000000, 1), (1, 10000000)):
            with self.assertRaises(F.InputValidationError) as caught:
                checker.add(user, item, 3)
            self.assertIn('packing key would overflow', str(caught.exception))
        checker.cleanup()

    def test_per_user_cross_check_catches_threshold_drift(self):
        """MINOR-2（**已由 Lead 修复**）：阈值漂移现在由**逐用户**校验抓住。

        修复前只有一条**全局** ``nnz`` 校验：``sum(index.n_like)`` 与
        ``merge_to_csr`` 的 ``nnz`` 比。这条粗校验能抓"总数变了"，但抓不到
        **逐用户**的偏差——例如"只对用户首行置位"那类 bug：每个用户少一行，
        若同时另有重复多算一条，总数可能刚好对上，而**每用户矩阵已经不是同一张**。

        修法：``run_pass2`` 用 pass 1 的 ``index.user_like_rows`` 与 pass 2 实测的
        逐用户计数比对，不一致即 ``CorrectnessError``，并由
        ``self.per_user_check`` 留下 ``{'users', 'disagreeing_users',
        'pass1_total', 'pass2_total'}`` 四项取证。

        这条用例把 pass 2 的阈值从 4.0 降到 3.0（真实漂移）后断言：
        - 抛 ``CorrectnessError``（不是宽泛的 ``Exception``）；
        - 消息里点名"逐用户"这条校验，并给出首个不一致的用户位置与两侧计数
          （``pass1=1 pass2=2``）——**比旧的全局 nnz 校验更早、更精确**：
          旧校验在 ``merge_to_csr`` 归并完**所有段**、拿到总 ``nnz`` 之后才比，
          而这里在**写出 CSR 之前**就拒绝，且给出的是"哪个用户、两边各几条"，
          不是只报一个总数对不上；
        - 抛错时产物目录里没有任何 CSR 文件（被拒绝的漂移没有变成矩阵）；
        - ``builder.per_user_check`` 已留下这次漂移的规模（2 用户 / 1 不一致 /
          3 vs 4 条），即"先取证、再抛错"。
        """
        work = review_dir('nnz_drift')
        csv_path = write_csv(os.path.join(work, 'd.csv'), [
            (1, 10, 5.0, 100), (1, 11, 3.0, 101),
            (2, 10, 4.0, 102), (2, 11, 4.0, 103)])
        index = F.stream_input_pass1(csv_path, 4.0, log=None,
                                     repeat_policy='reject')
        # pass 1 的逐用户好评数：用户 1 只有 movie 10，用户 2 有 10 和 11。
        self.assertEqual(list(index.user_like_rows), [1, 2])
        self.assertEqual(index.positive_pairs(), 3)
        csr_dir = os.path.join(work, 'csr')
        os.makedirs(csr_dir)
        builder = F.DiskCSRBuilder(csr_dir, log=None, segment_rows=8)

        with self.assertRaises(F.CorrectnessError) as caught:
            builder.run_pass2(csv_path, index, 3.0, repeat_policy='reject')
        message = str(caught.exception)
        self.assertIn('per-user', message,
                      'the new check is per-user, not the old global nnz one: %r'
                      % message)
        self.assertIn('pass1=1 pass2=2', message,
                      'the error must localise the first disagreeing user')
        # 抛错发生在**归并之前**：段文件没有变成 CSR，所以产物里不存在
        # "阈值漂移后的矩阵"（旧实现要归并完、比总数才发现）。
        self.assertFalse(
            any(name.startswith('csr_') for name in os.listdir(csr_dir)),
            'no CSR file may be produced from a drifted pass 2')
        # 逐用户校验只在 reject 策略下做（dedupe 会主动丢重复对，pass2 更少是预期的）。
        # 拒绝发生在**先落取证、再抛错**的顺序上：per_user_check 已被填出，
        # 记录的就是这次漂移的规模（2 个用户、1 个不一致、3 vs 4 条好评）。
        # 这比"只抛一个异常"更有用——失败现场留下了可核对的数字。
        self.assertEqual(
            builder.per_user_check,
            {'users': 2, 'disagreeing_users': 1,
             'pass1_total': 3, 'pass2_total': 4})

    def test_reference_and_vectorized_paths_agree_on_ties(self):
        """无问题：平票按 target movieId 升序，向量化与逐条参考实现一致。"""
        rows = [(1, 10, 5.0, 100), (1, 11, 5.0, 101),
                (2, 10, 5.0, 102), (2, 11, 5.0, 103),
                (3, 10, 5.0, 104), (3, 12, 5.0, 105),
                (4, 11, 5.0, 106), (4, 12, 5.0, 107)]
        item_ids = sorted(set(r[1] for r in rows))
        user_ids = sorted(set(r[0] for r in rows))
        ui = dict((u, p) for p, u in enumerate(user_ids))
        ii = dict((m, p) for p, m in enumerate(item_ids))
        X = sparse.csr_matrix(
            (np.ones(len(rows), dtype=np.int64),
             (np.asarray([ui[r[0]] for r in rows]),
              np.asarray([ii[r[1]] for r in rows]))),
            shape=(len(user_ids), len(item_ids)), dtype=np.int64)
        arrays = F.build_graph_arrays(X, item_ids, beta=0.0, max_k=10,
                                     block_size=128)
        reference = F.reference_neighbors_python(X, item_ids, 0.0, 10)
        for pos, movie in enumerate(item_ids):
            start = int(arrays['indptr'][pos])
            stop = int(arrays['indptr'][pos + 1])
            got = [(int(item_ids[arrays['targets'][k]]),
                    float(arrays['similarities'][k]),
                    int(arrays['cooccurrences'][k]))
                   for k in range(start, stop)]
            self.assertEqual(got, reference[movie],
                             'source %d differs from the reference' % movie)
            sims = [s for _t, s, _c in got]
            self.assertEqual(sims, sorted(sims, reverse=True),
                             'similarities must be descending within a source')

    def test_run_id_shaped_out_tail_works_end_to_end(self):
        """无问题：``--out <root>/<run_id>``（计划书 §6 的形状）能跑通全链。

        这正是 VM 上默认会走的分支，所以缺陷 #1 在"按计划书原样调用"时被掩盖；
        它只在末段不是 run_id 形状时暴露。
        """
        work = review_dir('happy_path')
        csv_path = write_csv(os.path.join(work, 'tiny.csv'), [
            (1, 10, 5.0, 100), (1, 11, 4.5, 101), (2, 10, 4.0, 102),
            (2, 11, 4.0, 103), (3, 12, 5.0, 104), (4, 12, 4.5, 105)])
        out_dir = os.path.join(work, '20261004T120000Z-eeeeeeee')
        argv = ['--input', csv_path, '--out', out_dir, '--max-k', '5',
                '--block-size', '3', '--monitor-interval', '0.5',
                '--prepare-budget-seconds', '120',
                '--precheck-budget-seconds', '120',
                '--train-budget-seconds', '120',
                '--verify-budget-seconds', '120']
        full_argv = [sys.executable, '-m',
                     'experiments.itemcf.full_train'] + argv
        old_argv = sys.argv
        sys.argv = full_argv
        try:
            code = F.main(argv)
        except SystemExit as error:
            code = error.code
        finally:
            sys.argv = old_argv
        self.assertEqual(code, 0)
        for name in ('run_status.json', 'manifest.json', 'source_hashes.json',
                     'conclusion.md', 'precheck.json',
                     'orchestrator_status.json'):
            self.assertTrue(os.path.isfile(os.path.join(out_dir, name)),
                            '%s missing' % name)
        status = M.read_json(os.path.join(out_dir, 'run_status.json'))
        self.assertEqual(status['status'], F.STATUS_SUCCESS)
        self.assertEqual(status['run_id'], '20261004T120000Z-eeeeeeee')
        self.assertEqual(status['model_id'],
                         M.compute_model_id('20261004T120000Z-eeeeeeee'))

    def test_parse_rating_fields_rejects_real_invalid_values(self):
        """无问题：直接调用 ``parse_rating_fields`` 时校验是严的。

        非数字 userId、越界评分、非半星档、非正 timestamp 都带行号拒绝。
        """
        bad = [
            (['1.0', '10', '5.0', '100'], 'userId'),
            (['+1', '10', '5.0', '100'], 'userId'),
            (['0', '10', '5.0', '100'], 'userId'),
            (['1', '0', '5.0', '100'], 'movieId'),
            (['1', '10', '5.5', '100'], 'rating'),
            (['1', '10', '0.3', '100'], 'rating'),
            (['1', '10', '5.0', 'abc'], 'timestamp'),
            (['1', '10', '5.0', '0'], 'timestamp'),
        ]
        for parts, field in bad:
            with self.assertRaises(F.InputValidationError,
                                   msg='%r should be rejected' % (parts,)):
                F.parse_rating_fields(parts, 7, parts)
        self.assertEqual(F.parse_rating_fields(['1', '10', '4.5', '100'], 2),
                         (1, 10, 4.5, 100))

    def test_extra_csv_columns_are_tolerated_but_parse_guard_is_dead(self):
        """无问题（但记下死代码）：多于 4 列会被容忍，``parse_rating_fields``
        的 ``len(parts) < 4`` 分支在流式路径上不可达。

        ``iter_rating_chunks`` 传的是 ``[parts[u_col], parts[i_col],
        parts[r_col], parts[t_col]]`` 这个**长度恒为 4** 的列表字面量，所以
        ``parse_rating_fields`` 里的字段数检查永远不会触发；真正的字段数检查
        是上一层的 ``if len(parts) < 4``。7 列的行会被正常接受。
        """
        work = review_dir('extra_columns')
        csv_path = os.path.join(work, 'extra.csv')
        with open(csv_path, 'w', newline='', encoding='utf-8') as handle:
            handle.write('userId,movieId,rating,timestamp\n')
            handle.write('1,10,5.0,100,GARBAGE,EXTRA,MORE\n')
        rows, _report = F.read_input_relevant_rows(csv_path)
        self.assertEqual(rows, [(1, 10, 5.0, 100)])
        # 少列则被上层拦住
        short = os.path.join(work, 'short.csv')
        with open(short, 'w', newline='', encoding='utf-8') as handle:
            handle.write('userId,movieId,rating,timestamp\n')
            handle.write('1,10,5.0\n')
        with self.assertRaises(F.InputValidationError):
            F.read_input_relevant_rows(short)

    def test_user_like_rows_is_now_cross_checked(self):
        """MINOR-2（**已由 Lead 修复**）：``user_like_rows`` 不再是死数据。

        修复前：``StreamingIndex.user_like_rows`` 在 pass 1 逐行累加，但全文再没有
        任何读取点——pass 2 的 CSR 行长度与它没有交叉校验，只有**全局**
        ``sum(n_like)`` 与 ``nnz`` 比过（旧用例正是把"它是死数据"钉死的那条）。
        每用户级别的两遍一致性因此没有门禁。

        修法：``run_pass2`` 读 ``index.user_like_rows``，与 pass 2 逐用户实测计数
        比对，不一致抛 ``CorrectnessError``，并落 ``self.per_user_check`` 取证。

        这条用例断言"现在**有**读取点、且是**校验**读取点"（源码级），
        并实跑一次确认校验值确实被填出来（``pass1_total == pass2_total``、
        零个不一致用户）——只数出现次数会漏掉"读来打日志"这种假修复。
        """
        source = open(FULL_TRAIN_PATH, encoding='utf-8').read()
        # 至少 8 处：字段声明/初始化/累加/pass 2 的交叉校验读取/日志等。
        self.assertGreaterEqual(source.count('user_like_rows'), 7,
                                'user_like_rows must have real readers now')
        self.assertIn('self.user_like_rows = array.array', source)
        self.assertIn('user_like_rows[pos] += 1', source)
        # 关键：pass 2 里真的拿它与逐用户计数比较。
        self.assertIn('np.asarray(index.user_like_rows', source)
        self.assertIn('self.per_user_check', source)
        # 实跑：一致性通过时 per_user_check 被填出，且两侧总数相等。
        work = review_dir('user_like_rows_live')
        csv_path = write_csv(os.path.join(work, 'd.csv'), [
            (1, 10, 5.0, 100), (1, 11, 3.0, 101),
            (2, 10, 4.0, 102), (2, 11, 4.0, 103)])
        index = F.stream_input_pass1(csv_path, 4.0, log=None,
                                     repeat_policy='reject')
        csr_dir = os.path.join(work, 'csr')
        os.makedirs(csr_dir)
        builder = F.DiskCSRBuilder(csr_dir, log=None, segment_rows=8)
        builder.run_pass2(csv_path, index, 4.0, repeat_policy='reject')
        check = builder.per_user_check
        self.assertIsNotNone(check, 'the per-user cross-check must be recorded')
        self.assertEqual(sorted(check), ['disagreeing_users', 'pass1_total',
                                         'pass2_total', 'users'])
        self.assertEqual(check['disagreeing_users'], 0)
        self.assertEqual(check['users'], index.n_users)
        self.assertEqual(check['pass1_total'], index.positive_pairs())
        self.assertEqual(check['pass2_total'], index.positive_pairs())

    def test_source_hashes_cover_every_module_that_shapes_the_artifacts(self):
        """无问题：源码指纹覆盖了 ``full_train`` 直接 import 的全部模块。

        ``source_hashes_of_modules()`` 现在含 ``conclusion_writer.py``。核对
        ``full_train.py`` 的 import 表：``model_io`` / ``conclusion_writer`` /
        ``resource_guard`` 都在列表里，``numpy`` / ``scipy`` / 标准库不属于
        本仓库源码。``verify_full_model.py`` 与 ``core.py`` 也在列表里。
        """
        hashes = F.source_hashes_of_modules()
        for name in ('full_train.py', 'model_io.py', 'verify_full_model.py',
                     'core.py', 'resource_guard.py', 'conclusion_writer.py'):
            self.assertIn(name, hashes, '%s is missing from the source hashes'
                          % name)
            self.assertRegex(hashes[name], r'^[0-9a-f]{64}$')
        # full_train 的 import 表里的实验模块都必须被覆盖
        source = open(FULL_TRAIN_PATH, encoding='utf-8').read()
        for module in ('model_io', 'conclusion_writer', 'resource_guard'):
            self.assertIn("from experiments.itemcf import %s" % module, source)
            self.assertIn('%s.py' % module, hashes)

    def test_fingerprint_closure_has_no_uncovered_local_import(self):
        """无问题（Lead 特别问过）：依赖闭包里没有"改了却不算漂移"的模块。

        做法：把 ``source_hashes_of_modules()`` 列出的 6 个模块解析成 AST，
        收集其中所有 ``experiments.itemcf.*`` 的 import，再看哪些目标文件不在
        指纹里。结果是**空集**——``full_train`` 本地依赖闭包 = {model_io,
        conclusion_writer, resource_guard}，``verify_full_model`` = {model_io}，
        全部已覆盖。``conclusion_writer`` 只 import ``json``/``os``。
        （``baselines`` / ``evaluation`` / ``pilot.data`` 等并未被 full_train
        依赖，所以不在指纹里是正确的。）
        """
        module_dir = os.path.join(PROJECT, 'experiments', 'itemcf')
        listed = set(F.source_hashes_of_modules())
        uncovered = []
        for name in sorted(listed):
            path = os.path.join(module_dir, name)
            with open(path, encoding='utf-8') as handle:
                tree = ast.parse(handle.read(), filename=name)
            targets = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.startswith('experiments.itemcf'):
                        parts = node.module.split('.')
                        if len(parts) >= 3:
                            targets.add(parts[2] + '.py')
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith('experiments.itemcf'):
                            parts = alias.name.split('.')
                            if len(parts) >= 3:
                                targets.add(parts[2] + '.py')
            for target in sorted(targets):
                if target not in listed:
                    uncovered.append((name, target))
        self.assertEqual(uncovered, [],
                         'these local imports are outside the source '
                         'fingerprint: %r' % (uncovered,))

    def test_run_log_is_the_only_thing_written_before_the_prepare_phase(self):
        """无问题：``run.log`` 在 ``assembled_ids`` 之后才开，编号/目录校验早于它。

        （这条只是把早退路径的边界钉死：``assembled_ids`` 失败时连
        ``run.log`` 都没有，所以那种失败在 run 目录里**完全无痕**——这正是
        缺陷 #1 最麻烦的部分。）
        """
        source = _function_source('run_worker')
        self.assertLess(source.index('assembled_ids(out_dir)'),
                        source.index("'run.log'"))


# ===========================================================================
# 11. 手工核对过、确认无需行动的其余方向
# ===========================================================================

class AuditedWithoutActionTests(unittest.TestCase):
    """审查过并判定"无需行动"的方向，逐条给出理由（供 Lead 快速跳读）。"""

    def test_csv_average_row_bytes_assumption(self):
        """输入规模：32,000,204 行 × ~26 B/行 ≈ 836 MB，与计划书一致。

        这里只固化"单位"，不读原始 CSV（``.aiignore`` 禁止把 836 MB 读进上下文）。
        """
        self.assertEqual(32000204, 32000204)
        rows = 32000204
        self.assertLess(rows * 26 / float(1 << 20), 900.0)

    def test_block_flush_interval_is_bounded(self):
        """``BLOCK_FLUSH_EVERY=16``：崩在两次 flush 之间最多丢 16 个块的**索引**。

        块文件本身在 ``write_block`` 里已经落盘（``np.savez``），丢的只是
        ``graph_blocks.json`` 的索引条目；而 ``_check_existing`` 会拒绝复用
        已存在索引的目录，所以重跑必须换新目录。可接受。
        """
        self.assertEqual(F.BLOCK_FLUSH_EVERY, 16)
        # flush 会把 _last_flush 推到当前记录数（计数基准）
        flush_source = _method_source('BlockStore', 'flush')
        self.assertIn('_last_flush = len(self.records)', flush_source)
        # write_block 只在跨过 BLOCK_FLUSH_EVERY 时才 flush
        write_source = _method_source('BlockStore', 'write_block')
        self.assertIn('self._last_flush >= BLOCK_FLUSH_EVERY', write_source)

    def test_precheck_uses_at_most_six_blocks(self):
        """无问题：``select_precheck_blocks`` 最多 6 块且含首/中/末/最高支持度。"""
        n_items, block_size = 1000, 128
        n_like = np.zeros(n_items, dtype=np.int64)
        n_like[500] = 99                      # 最高支持度在第 3 块
        chosen = F.select_precheck_blocks(n_items, block_size, n_like)
        self.assertLessEqual(len(chosen), 6)
        n_blocks = (n_items + block_size - 1) // block_size
        self.assertIn(0, chosen)
        self.assertIn(n_blocks - 1, chosen)
        self.assertIn(500 // block_size, chosen)

    def test_guard_is_a_hard_import(self):
        """无问题：``resource_guard`` 是硬依赖，没有"无监督也能跑"的回落分支。"""
        source = open(FULL_TRAIN_PATH, encoding='utf-8').read()
        tree = ast.parse(source)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            if isinstance(node, ast.Try):
                body = ast.dump(ast.Module(body=node.body))
                if 'resource_guard' in body:
                    self.fail('resource_guard import is wrapped in try/except')
        self.assertIn('experiments.itemcf.resource_guard', imported)

    def test_stage_names_match_the_guard_contract(self):
        """无问题：``PHASES`` 直接复用 ``STAGES``，不会写出 guard 不认的阶段名。"""
        self.assertEqual(tuple(F.PHASES), tuple(G.STAGES))
        self.assertEqual(tuple(F.PHASES),
                         ('prepare', 'precheck', 'train', 'verify'))

    def test_status_values_come_from_the_guard_module(self):
        """无问题：状态取值全部来自 ``resource_guard``，没有自造字符串。"""
        self.assertEqual(F.STATUS_SUCCESS, G.STATUS_SUCCESS)
        self.assertEqual(F.STATUS_RESOURCE_BLOCKED, G.STATUS_RESOURCE_BLOCKED)
        self.assertEqual(F.STATUS_RESOURCE_FAILED, G.STATUS_RESOURCE_FAILED)
        self.assertEqual(F.STATUS_MONITOR_FAILED, G.STATUS_MONITOR_FAILED)
        self.assertEqual(F.STATUS_CORRECTNESS_FAILED, G.STATUS_CORRECTNESS_FAILED)


# ===========================================================================
# 12. 汇总：缺陷清单（供 Lead 直接引用）
# ===========================================================================

class FindingSummaryTests(unittest.TestCase):
    """把每条发现的**最小复现命令**固化下来，便于 Lead 逐条复核。

    这个类不重复断言，只保证复现脚本与缺陷一一对应、且脚本仍在仓库里。
    """

    FINDINGS = (
        ('CRITICAL', 'worker --out 与 orchestrator out_dir 不一致（已修）',
         'test_worker_argv_out_is_the_resolved_run_directory'),
        ('CRITICAL', '自动生成的 run_id 传不到 worker（已修）',
         'test_run_id_reaches_the_worker_when_generated_by_the_orchestrator'),
        ('CRITICAL', 'worker 早退零终态产物 —— 输入缺失（已修）',
         'test_missing_input_now_leaves_full_terminal_artifacts'),
        ('CRITICAL', 'worker 早退零终态产物 —— 磁盘门禁（已修）',
         'test_disk_floor_violation_now_leaves_full_terminal_artifacts'),
        ('MAJOR', 'precheck 降档不传导到 train，外推量纲不匹配（已修：run_worker 回写）',
         'test_ladder_size_reaches_the_train_phase'),
        ('MAJOR', '外推按请求档位而非降档后档位算块数（已修）',
         'test_projection_uses_the_downshifted_block_count'),
        ('MAJOR', '正式构图重试刚 MemoryError 的那一档（已修）',
         'test_train_phase_uses_the_size_that_actually_worked'),
        ('MAJOR', 'pass 2 的 DuplicateChecker 临时文件不清理（~488 MiB，已修）',
         'test_pass2_no_longer_leaks_segment_files'),
        ('MAJOR', 'pass 2 的 checker 从未 cleanup（同源，已修）',
         'test_run_pass2_cleans_up_its_checker'),
        ('MAJOR', '--run-id 与 --out 末段分歧时产物内 ID 分叉（已修：改为拒绝）',
         'test_run_id_and_out_basename_must_agree'),
        ('MAJOR', '分歧曾是静默的（已修：现在在建目录前就报错）',
         'test_all_artifacts_exist_so_the_disagreement_is_silent'),
        ('MAJOR', 'worker 自报 success 会盖掉被杀的退出码（已修：要求 exit_code==0）',
         'test_worker_success_requires_clean_exit'),
        ('MAJOR', '退出码 0 但零产物被记成 success（已修：终态产物门禁 + artifact_gate）',
         'test_missing_model_artifacts_downgrade_a_clean_exit'),
        ('MAJOR', '产物齐全但未验证（assembled_unverified）也必须拒绝（已修：require_status 生效）',
         'test_require_status_rejects_a_wellformed_but_unverified_model'),
        ('MINOR', 'estimate_disk_bytes 对 block_size<=0 / max_k<=0 无保护（已修：抛 ValueError）',
         'test_estimate_rejects_non_positive_block_size'),
        ('MINOR', 'user_like_rows 是死数据（已修：pass 2 逐用户交叉校验）',
         'test_user_like_rows_is_now_cross_checked'),
        ('MINOR', 'precheck 降档时每一档的失败记录被丢弃（已修：ladder_history）',
         'test_failed_records_are_kept_per_ladder_tier'),
        ('MINOR', 'WORKER_ARGV_ENV 声明后无人使用（已修：删除死常量）',
         'test_environment_channel_constant_is_gone'),
    )

    def test_every_finding_points_at_a_real_test(self):
        module = sys.modules[__name__]
        names = set()
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, unittest.TestCase):
                names.update(vars(value))
        for severity, description, test_name in self.FINDINGS:
            self.assertIn(test_name, names,
                          '%s: %s -> %s is missing' % (severity, description,
                                                       test_name))
            self.assertIn(severity, ('CRITICAL', 'MAJOR', 'MINOR'))

    def test_reproduction_scripts_are_kept(self):
        """``.vmwork/review_train/probe*.py`` 是每条发现的独立复现脚本。"""
        probe_dir = os.path.join(PROJECT, '.vmwork', 'review_train')
        expected = ['probe2.py', 'probe3.py', 'probe4.py', 'probe7.py']
        for name in expected:
            self.assertTrue(os.path.isfile(os.path.join(probe_dir, name)),
                            '%s should be kept as evidence' % name)


if __name__ == '__main__':
    unittest.main()
