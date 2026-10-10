# -*- coding: utf-8 -*-
"""ItemCF 全量模型的**磁盘数组契约**与存取校验（计划书 §4 表格）。

本模块只负责"把图落成独立 NumPy 数组 + 元数据"和"读回来先验证再用"，
不做构图、不做打分、不读评分 CSV。构图在 :mod:`experiments.itemcf.full_train`，
深度核验在 :mod:`experiments.itemcf.verify_full_model`。

目录契约（``<model_dir>``）::

    item_ids.npy          int64   递增 movieId，长度 n_items
    indptr.npy            int64   每个来源的邻居区间边界，长度 n_items + 1
    target_indices.npy    int64   目标在 item_ids 中的**下标**，长度 n_edges
    similarities.npy      float64 与 target_indices 对齐的相似度，长度 n_edges
    cooccurrences.npy     int64   与 target_indices 对齐的共现计数，长度 n_edges
    n_like.npy            int64   对齐 item_ids 的好评人数（rating >= 阈值）
    n_all.npy             int64   对齐 item_ids 的全部评分人数
    model.json            schema_version=1 元数据（含文件哈希、计数、dtype、状态）

**索引语义（必须写清，否则读侧极易搞错）**：

- 数组用**从 0 开始的下标**，不是 movieId。``item_ids[pos]`` 才给出 movieId。
- 来源 ``pos`` 的邻居切片是 ``[indptr[pos], indptr[pos + 1])``；区间**左闭右开**。
- 没有邻居的来源 ``indptr[pos] == indptr[pos + 1]``，**保留空区间**（不省略、不共享
  哨兵），所以 ``indptr`` 长度恒为 ``n_items + 1`` 且 ``indptr[0] == 0``、
  ``indptr[-1] == n_edges``。
- 每条边的**来源**是 ``indptr`` 反查出来的来源下标，**不单独存**：这是 CSR 的
  紧凑之处，也意味着边的写入顺序必须按来源下标升序。:func:`save_model` 会校验
  这一点，不满足直接抛错（不静默重排——静默重排会掩盖上游 bug）。

**排序契约**：每个来源的邻居按 ``(-similarity, target_movieId)`` 升序排列，
即相似度从大到小、相似度**真正平票**时目标 movieId 从小到大。落盘顺序 =
内存顺序 = :func:`experiments.itemcf.core.build_neighbors` 的排序，三处一致。

**零邻居行为**：全零区间合法且必须是"真的没有邻居"，不是"没算"。零好评来源
（``n_like == 0``）按定义无相似度，因此必然是空区间；``n_all > 0`` 而
``n_like == 0`` 的来源因此会有空区间——这是正确结果，不是缺失。

**状态机**：``model.json['status']`` 只取

- ``build_incomplete``：构图尚未完成（缺块或未装配）。
- ``assembled_unverified``：数组已装配落盘，但尚未通过 :func:`validate_arrays` /
  :func:`verify_hashes`。
- ``validated_unpublished``：结构与哈希全部通过。**这个状态不代表线上 active**，
  只说明"文件自洽且可加载"。发布是另一件事，不在本模块范围。

**读取纪律**：:func:`load_model` 默认 ``mmap_mode='r'``——只映射，不整份读进内存；
``validate=True`` 时先查哈希再查结构，任一失败即抛 :class:`ModelIOError`，
**绝不返回半可信对象**。校验读哈希要顺序读全部字节（那是 IO，不是常驻内存），
所以大模型上"校验"这一步有明显磁盘成本，属于预期行为。

Python 3.6 兼容：无 f-string / dataclasses / walrus / ``from __future__ import
annotations``。依赖标准库 + NumPy。
"""
import hashlib
import json
import os
import re
import time

import numpy as np

#: 元数据 schema 版本（计划书 §4 固定为 1）。
SCHEMA_VERSION = 1

#: 模型目录里 7 个数组文件与 1 个元数据文件的固定名字。
ARRAY_FILES = (
    'item_ids.npy',
    'indptr.npy',
    'target_indices.npy',
    'similarities.npy',
    'cooccurrences.npy',
    'n_like.npy',
    'n_all.npy',
)
METADATA_FILE = 'model.json'

#: "正在构建"标记。``save()`` 在**删掉旧 model.json 之后、写数组之前**创建它，
#: 收尾时删除。它的存在意味着"目录里的数组可能正处于半写状态"（D2）。
#: 见 :meth:`NeighborModel.save` 的 docstring。
BUILDING_MARKER_FILE = '.building'

#: 每个数组的 dtype 契约（禁止 bool / uint8 计数——会溢出）。
ARRAY_DTYPES = {
    'item_ids.npy': np.dtype(np.int64),
    'indptr.npy': np.dtype(np.int64),
    'target_indices.npy': np.dtype(np.int64),
    'similarities.npy': np.dtype(np.float64),
    'cooccurrences.npy': np.dtype(np.int64),
    'n_like.npy': np.dtype(np.int64),
    'n_all.npy': np.dtype(np.int64),
}

#: 允许出现在元数据里的状态值（顺序即生命周期）。
STATUS_BUILD_INCOMPLETE = 'build_incomplete'
STATUS_ASSEMBLED_UNVERIFIED = 'assembled_unverified'
STATUS_VALIDATED_UNPUBLISHED = 'validated_unpublished'
#: **独立核验判定失败**后的状态。
#:
#: 为什么需要这个状态：``validated_unpublished`` 的凭据只有**自查**
#: （``validate_arrays`` 是装配它的同一模块，``verify_hashes`` 校验的是"刚写下的
#: 哈希等于刚记下的哈希"）。唯一从**原始评分独立重算**的是另一个 CLI
#: ``verify_full_model``，它**不被** model_io / full_train 读取。
#: 于是会出现产物互相矛盾：核验者判定模型复现不了输入，而 ``model.json``
#: 仍写着 ``validated_unpublished``，下游只读 ``model.json`` 就会采信一个
#: 已被它自己的核验者否定的模型。这个状态由 :func:`apply_verification_verdict`
#: 在核验**真的发现不符**时写入，使两侧口径一致。
STATUS_VERIFICATION_FAILED = 'verification_failed'
ALLOWED_STATUSES = (STATUS_BUILD_INCOMPLETE, STATUS_ASSEMBLED_UNVERIFIED,
                    STATUS_VALIDATED_UNPUBLISHED, STATUS_VERIFICATION_FAILED)

#: 一旦进入就**不可**再回到 ``validated_unpublished`` 的状态（单调降级）。
#: 允许从 ``validated_unpublished`` 降到 ``verification_failed``，反之必须重跑
#: 整轮（重新装配 + 重新核验），不允许就地"洗白"。
TERMINAL_FAILURE_STATUSES = (STATUS_VERIFICATION_FAILED,)

#: run_id / model_id 的格式：``YYYYMMDDTHHMMSSZ-<8 位小写十六进制>``。
ID_PATTERN = re.compile(r'^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$')

#: 结构校验的分块大小（每块的目标边数上界）。
#:
#: 背景（D3）：``validate_arrays`` 的逐边检查原本一次性在整条边数组上开临时量，
#: tracemalloc 实测堆峰值 ≈ 边数组的 **9.89×**（250k 边 → 18.9 MB），
#: 全量 2000 万边外推约 **1.5 GB**。而它跑在 **3 GiB 上限**那条受监督路径上
#: （``full_train`` 的 verify 阶段），全量时很可能直接撞线被终止。
#:
#: 取 200 万边：峰值临时量约 200 MB 量级，配合 memmap 读入的数据页，
#: 离 3 GiB 有充足余量；同时块数不多（全量约 10 块），向量化效率不受影响。
DEFAULT_VALIDATE_CHUNK_EDGES = 2000000


class ModelIOError(Exception):
    """模型文件缺失、哈希不符或结构违约。**不吞、不降级**，直接中断加载。"""


# ---------------------------------------------------------------------------
# 小工具：哈希、原子写、JSON
# ---------------------------------------------------------------------------

def file_sha256(path, chunk_size=1 << 22):
    """文件 SHA-256（分块顺序读；内存只占一块）。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def write_json_atomic(path, payload, indent=2):
    """原子写 JSON：先写 ``<path>.tmp`` 再 ``os.replace``，读者永远看不到半份。

    ``os.replace`` 在同一文件系统上是原子的（POSIX rename / Windows
    MoveFileEx）。这意味着并发读者要么看到旧的完整文件，要么看到新的完整文件。
    """
    directory = os.path.dirname(os.path.abspath(path))
    if directory and not os.path.isdir(directory):
        os.makedirs(directory)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=indent, sort_keys=True,
                  default=_json_default)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_json(path):
    with open(path, encoding='utf-8') as handle:
        return json.load(handle)


def _json_default(value):
    """把 NumPy 标量/数组降成 JSON 原生类型（否则 ``json.dump`` 直接抛）。"""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def utc_now_iso():
    """UTC 时间字符串（秒级，``...Z``）；用于元数据的构建时间。"""
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def verify_id_format(value, name='id'):
    """校验 run_id / model_id 形状；不合规抛 ValueError。

    形状固定为 ``YYYYMMDDTHHMMSSZ-xxxxxxxx``（8 位小写十六进制）。上轮的 bug 是
    产物里写了通用 ``'run'``——那种值在这里会被直接拒绝，而不是"看起来也能用"。
    """
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise ValueError('%s must look like YYYYMMDDTHHMMSSZ-xxxxxxxx, got %r'
                         % (name, value))
    return value


# ---------------------------------------------------------------------------
# 图容器
# ---------------------------------------------------------------------------

class NeighborModel(object):
    """全量邻居模型的只读视图（CSR 形态 + 对齐统计）。

    构造时只做**形状**检查（长度一致、dtype 一致）；结构不变式（单调、
    无自环、排序、相似度范围）在 :func:`validate_arrays` 里查，因为那是
    O(n_edges) 的向量化扫描，应该由调用方显式决定何时付这个代价。
    """

    def __init__(self, item_ids, indptr, target_indices, similarities,
                 cooccurrences, n_like, n_all):
        self.item_ids = np.asarray(item_ids)
        self.indptr = np.asarray(indptr)
        self.target_indices = np.asarray(target_indices)
        self.similarities = np.asarray(similarities)
        self.cooccurrences = np.asarray(cooccurrences)
        self.n_like = np.asarray(n_like)
        self.n_all = np.asarray(n_all)
        self._check_shapes()

    def _check_shapes(self):
        n_items = len(self.item_ids)
        if len(self.indptr) != n_items + 1:
            raise ModelIOError('indptr length %d != n_items + 1 (%d)'
                               % (len(self.indptr), n_items + 1))
        if len(self.n_like) != n_items:
            raise ModelIOError('n_like length %d != n_items %d'
                               % (len(self.n_like), n_items))
        if len(self.n_all) != n_items:
            raise ModelIOError('n_all length %d != n_items %d'
                               % (len(self.n_all), n_items))
        if len(self.target_indices) != len(self.similarities):
            raise ModelIOError('target_indices length %d != similarities length %d'
                               % (len(self.target_indices), len(self.similarities)))
        if len(self.cooccurrences) != len(self.target_indices):
            raise ModelIOError('cooccurrences length %d != target_indices length %d'
                               % (len(self.cooccurrences), len(self.target_indices)))

    # -- 基本属性 --------------------------------------------------------

    @property
    def n_items(self):
        return int(len(self.item_ids))

    @property
    def n_edges(self):
        return int(len(self.target_indices))

    def n_sources(self):
        """有至少一条邻居的来源个数（空区间不计）。"""
        counts = np.diff(self.indptr)
        return int(np.count_nonzero(counts))

    def source_positions(self):
        """每个来源的邻居条数，长度 n_items（``array('q')`` 语义的 int64）。"""
        return np.diff(self.indptr)

    def id_to_pos(self):
        """``{movieId: 下标}``。**只在需要时显式调用**——全量约 8.8 万项，
        它是 Python dict，不要在建图热路径里顺手构造。"""
        return dict((int(v), p) for p, v in enumerate(self.item_ids))

    def edges_of_source(self, source_pos):
        """来源下标 ``source_pos`` 的 ``(target_indices, similarities, coocs)`` 切片。"""
        pos = int(source_pos)
        start = int(self.indptr[pos])
        stop = int(self.indptr[pos + 1])
        return (self.target_indices[start:stop], self.similarities[start:stop],
                self.cooccurrences[start:stop])

    # -- 落盘 / 读回 -----------------------------------------------------

    def save(self, model_dir, metadata, status=None):
        """把数组与元数据落盘；返回写好的元数据 dict。

        顺序很关键：**先移除旧元数据 → 写全部数组 → 算哈希 → 最后原子写 model.json**。

        为什么第一步要移除旧元数据（D2）：
          ``model.json`` 是"这份模型可信"的唯一凭据。数组是**就地逐个覆盖**的，
          一个数组就要写几百 MB，所以重写已有目录时存在一个真实窗口——
          新数组只写了一半，而**上一轮的 ``model.json`` 还躺在那里**，
          且它的哈希对应旧数组。此时裸 ``NeighborModel.load()`` 会读到
          "新旧混杂"的数组，而目录里没有任何"半写"标记。
          实测：覆盖一个数组后，旧 ``model.json`` 仍在、7 个数组齐全、
          无半写标记，裸 load() 直接读到被污染的数据。

          修复：写数组**之前**先删掉 ``model.json``（并写一个 ``.building`` 标记）。
          于是任何中途崩溃都表现为"缺元数据"——这是**安全失败**，
          :func:`load_model` 会直接拒绝，而不是拿半份文件当模型用。
          收尾时删掉标记、再原子写 ``model.json``。

        注意：这不等于"目录级事务"（数组仍是就地覆盖），但它把
        "**崩溃后仍有一个看起来可用的 model.json**"这个危险窗口关掉了——
        而那个窗口正是唯一会导致"静默读到坏数据"的路径。

        :param status: 覆盖 ``metadata['status']``；默认用调用方给的值，
            缺省为 :data:`STATUS_ASSEMBLED_UNVERIFIED`。
        """
        if not os.path.isdir(model_dir):
            os.makedirs(model_dir)
        # 1) 先让旧元数据失效：删掉它并留下"正在构建"的标记。
        stale_meta = os.path.join(model_dir, METADATA_FILE)
        if os.path.isfile(stale_meta):
            os.remove(stale_meta)
        write_json_atomic(os.path.join(model_dir, BUILDING_MARKER_FILE), {
            'started_at': utc_now_iso(),
            'note': ('arrays are being (re)written in place; a model.json must '
                     'not be trusted until this marker is gone'),
        })

        arrays = {
            'item_ids.npy': self.item_ids,
            'indptr.npy': self.indptr,
            'target_indices.npy': self.target_indices,
            'similarities.npy': self.similarities,
            'cooccurrences.npy': self.cooccurrences,
            'n_like.npy': self.n_like,
            'n_all.npy': self.n_all,
        }
        for name in ARRAY_FILES:
            arr = np.ascontiguousarray(arrays[name], dtype=ARRAY_DTYPES[name])
            np.save(os.path.join(model_dir, name), arr, allow_pickle=False)

        hashes = {}
        sizes = {}
        for name in ARRAY_FILES:
            path = os.path.join(model_dir, name)
            hashes[name] = file_sha256(path)
            sizes[name] = int(os.path.getsize(path))

        payload = dict(metadata or {})
        payload['schema_version'] = SCHEMA_VERSION
        payload['status'] = status or payload.get('status') or STATUS_ASSEMBLED_UNVERIFIED
        if payload['status'] not in ALLOWED_STATUSES:
            raise ValueError('unknown status %r; allowed=%r'
                             % (payload['status'], list(ALLOWED_STATUSES)))
        payload['created_at'] = payload.get('created_at') or utc_now_iso()
        payload['counts'] = {
            'n_items': self.n_items,
            'n_edges': self.n_edges,
            'n_sources_with_neighbors': self.n_sources(),
        }
        payload['dtypes'] = dict((name, str(ARRAY_DTYPES[name]))
                                 for name in ARRAY_FILES)
        payload['shapes'] = dict((name, [int(v) for v in arrays[name].shape])
                                 for name in ARRAY_FILES)
        payload['files'] = dict((name, {'sha256': hashes[name],
                                        'bytes': sizes[name]})
                                for name in ARRAY_FILES)
        # 返回值 = **写好的元数据 dict**（与 docstring 一致）。
        #
        # 曾经这里写的是 ``return write_json_atomic(...)``——那返回的是**路径字符串**，
        # 与 docstring 矛盾，并让调用方 ``run_worker`` 的
        # ``hash_ok = M.verify_hashes(model_dir, saved)`` 收到 str 而
        # ``meta.get('files')`` 抛 AttributeError（实测崩过，且因为 finalize 也崩，
        # 连失败记录都留不下）。调用方在 save() 之后要 ``saved['status'] = ...``、
        # ``saved['model_id'] = ...`` 再回传，因此这里**必须**返回可变的 dict。
        write_json_atomic(os.path.join(model_dir, METADATA_FILE), payload)
        # 元数据已就位 -> 撤掉"正在构建"标记。先写 model.json 再删标记：
        # 顺序反过来的话，删完标记到写 model.json 之间会有一个"无标记也无元数据"
        # 的窗口——那还好（安全失败），但先写元数据能让读者尽早看到完整状态。
        marker = os.path.join(model_dir, BUILDING_MARKER_FILE)
        if os.path.isfile(marker):
            os.remove(marker)
        return payload

    @classmethod
    def load(cls, model_dir, mmap_mode='r'):
        """只加载数组（不做哈希/结构校验）；要验证请用 :func:`load_model`。"""
        arrays = load_arrays(model_dir, mmap_mode=mmap_mode)
        return cls(**arrays)


def load_arrays(model_dir, mmap_mode='r'):
    """按契约 mmap 读 7 个数组；dtype 不符即抛 :class:`ModelIOError`。

    ``mmap_mode='r'`` 时返回的是 :class:`numpy.memmap`，**不把内容读进内存**；
    对全量模型（约 8.8 万物品、上千万边）这是唯一可接受的方式。注意 memmap
    仍会随访问把页计入 RSS，所以它只是"不一次性拷贝"，不是"零内存"。
    """
    arrays = {}
    for name in ARRAY_FILES:
        path = os.path.join(model_dir, name)
        if not os.path.isfile(path):
            raise ModelIOError('missing array file: %s' % path)
        try:
            arr = np.load(path, mmap_mode=mmap_mode, allow_pickle=False)
        except ValueError as error:
            raise ModelIOError('could not mmap %s: %s' % (path, error))
        expected = ARRAY_DTYPES[name]
        if arr.dtype != expected:
            raise ModelIOError('%s has dtype %s, expected %s'
                               % (path, arr.dtype, expected))
        arrays[name.replace('.npy', '')] = arr
    return arrays


def verify_hashes(model_dir, metadata=None):
    """逐文件重算 SHA-256 并与元数据比对；返回 ``{文件: 是否一致}``。

    读的是磁盘全量字节，所以大模型上这一步慢是正常的。缺文件、缺哈希记录
    都算不一致（``False``），不抛异常——由调用方汇总成一次失败报告。
    """
    meta = metadata if metadata is not None else read_json(
        os.path.join(model_dir, METADATA_FILE))
    recorded = meta.get('files') or {}
    results = {}
    for name in ARRAY_FILES:
        path = os.path.join(model_dir, name)
        expected = (recorded.get(name) or {}).get('sha256')
        if expected is None or not os.path.isfile(path):
            results[name] = False
            continue
        results[name] = file_sha256(path) == expected
    return results


# ---------------------------------------------------------------------------
# 结构校验（全部向量化，不逐边 Python 循环）
# ---------------------------------------------------------------------------

def _sorted_desc_with_id_tiebreak(targets, sims):
    """检查一个来源的邻居是否按 ``(-sim, target_movieId)`` 升序。

    相似度**真正平票**（``sims[pos] == sims[pos-1]``，精确相等）时才要求
    ``target_ids`` 升序。半开区间里"几乎相等但不精确相等"的浮点值按相似度
    判定即可——如果写成带容差的平票判定，会把"确实不同"的两条值也当成平票
    强行要求 ID 有序，那是在给上游排序施加比契约更强的约束。
    """
    if len(sims) <= 1:
        return True
    if np.any(sims[1:] > sims[:-1]):
        return False
    tied = sims[1:] == sims[:-1]
    if not np.any(tied):
        return True
    return bool(np.all(targets[1:][tied] > targets[:-1][tied]))


class _ChunkedCheckAccumulator(object):
    """分块校验的累加器：把逐块的计数与"首个命中位置"汇总起来。

    存在的理由是 D3（见 :func:`_check_edge_chunk` 的注释）：结构校验必须
    分块跑，才能让峰值内存与总边数脱钩。累加器把"跨块也要给对"的量收在这里：
    计数直接相加；"第一个命中的来源"取**出现最早的那块**（块按边下标升序遍历，
    所以首次非零即最终值）。
    """

    __slots__ = ('self_loops', 'first_self_loop_source', 'non_finite',
                 'non_positive_sims', 'above_one', 'max_similarity_seen',
                 'min_similarity_seen', 'non_positive_coocs', 'cooc_over_bound',
                 'first_over_bound_source', 'max_relative_gap', 'bad_desc',
                 'bad_tie', 'duplicate_target')

    def __init__(self):
        self.self_loops = 0
        self.first_self_loop_source = None
        self.non_finite = 0
        self.non_positive_sims = 0
        self.above_one = 0
        self.max_similarity_seen = None
        self.min_similarity_seen = None
        self.non_positive_coocs = 0
        self.cooc_over_bound = 0
        self.first_over_bound_source = None
        self.max_relative_gap = None
        self.bad_desc = 0
        self.bad_tie = 0
        self.duplicate_target = 0


def _iter_edge_chunks(indptr, counts, n_items, n_edges, chunk_edges):
    """按**来源边界**把边切成块，产出 ``(start_pos, stop_pos, e_start, e_stop)``。

    ``start_pos``/``stop_pos`` 是来源下标区间（左闭右开），
    ``e_start``/``e_stop`` 是对应的边下标区间。

    **块边界必须落在来源边界上**（不能按固定边数硬切）：排序/平票/重复目标的
    检查是"同一来源内的相邻对"，把来源拆到两块会漏掉跨块的那一对。
    按来源累积直到达到 ``chunk_edges`` 再切，天然满足这一点。
    """
    limit = int(chunk_edges)
    start_pos = 0
    e_start = 0
    while start_pos < n_items:
        # 往后吃来源，直到这一块的边数达到 limit（至少吃一个来源）。
        stop_pos = start_pos
        acc = 0
        while stop_pos < n_items and (acc == 0 or acc < limit):
            acc += int(counts[stop_pos])
            stop_pos += 1
        e_stop = e_start + acc
        if e_stop > n_edges:  # pragma: no cover - indptr 已验过一致性
            e_stop = n_edges
        yield start_pos, stop_pos, e_start, e_stop
        start_pos = stop_pos
        e_start = e_stop


def _check_edge_chunk(indptr, targets, sims, coocs, n_like, n_items, check_order,
                      e_start, e_stop, start_pos, stop_pos, acc,
                      bounds_checkable=True):
    """在 ``[e_start, e_stop)`` 这一段边上做第 3/4/6/7/8 项检查，累加进 ``acc``。

    **D3 的落点**：这里开的每个临时数组长度都是**块内边数**，不是总边数。
    原实现一次性开 ``edge_source``/``bound``/``denom``/``implied``/``diff``/
    ``scale``/``rel``（各 n_edges 长），tracemalloc 实测峰值 ≈ 边数组的 9.89×，
    全量 2000 万边约 1.5 GB —— 而这段跑在 3 GiB 上限的受监督路径上。

    ``start_pos``/``stop_pos`` 给出本块覆盖的来源区间，用于把命中的边映射回
    **来源下标**（与不分块时的回报值一致）。

    :param bounds_checkable: 全局判定"所有 target 都在 [0, n_items) 内"。
        只有为真时才做共现上界与相似度定义式复核——与旧实现的
        ``if np.any(targets >= 0) and np.all(targets < n_items) ...`` 等价。
    """
    if e_stop <= e_start:
        return
    t = np.asarray(targets[e_start:e_stop])
    s = np.asarray(sims[e_start:e_stop])
    c = np.asarray(coocs[e_start:e_stop])

    # 本块内"每条边的来源下标"。块边界落在来源边界上，所以只需本块的 counts。
    chunk_counts = np.diff(np.asarray(indptr[start_pos:stop_pos + 1]))
    source_of_edge = np.repeat(
        np.arange(start_pos, stop_pos, dtype=np.int64), chunk_counts)

    # 3. 无自环
    hit = source_of_edge == t
    if np.any(hit):
        acc.self_loops += int(np.count_nonzero(hit))
        if acc.first_self_loop_source is None:
            acc.first_self_loop_source = int(source_of_edge[np.argmax(hit)])

    # 6. 相似度范围
    if np.any(~np.isfinite(s)):
        acc.non_finite += int(np.count_nonzero(~np.isfinite(s)))
    else:
        acc.non_positive_sims += int(np.count_nonzero(s <= 0.0))
        acc.above_one += int(np.count_nonzero(s > 1.0 + 1e-12))
        if len(s):
            local_max = float(s.max())
            if acc.max_similarity_seen is None or local_max > acc.max_similarity_seen:
                acc.max_similarity_seen = local_max

    # 7. 共现为正整数且不超过两端 n_like
    acc.non_positive_coocs += int(np.count_nonzero(c <= 0))
    if bounds_checkable:
        bound = np.minimum(n_like[source_of_edge], n_like[t])
        over = c > bound
        if np.any(over):
            acc.cooc_over_bound += int(np.count_nonzero(over))
            if acc.first_over_bound_source is None:
                acc.first_over_bound_source = int(source_of_edge[np.argmax(over)])
        # 相似度的定义式复核：s = c / sqrt(n_i * n_j)，相对误差容差 1e-12。
        denom = np.sqrt(n_like[source_of_edge].astype(np.float64)
                        * n_like[t].astype(np.float64))
        with np.errstate(divide='ignore', invalid='ignore'):
            implied = c.astype(np.float64) / denom
        diff = np.abs(implied - s)
        scale = np.maximum(np.abs(implied), 1e-300)
        rel = diff / scale
        local_gap = float(rel.max()) if len(rel) else 0.0
        if acc.max_relative_gap is None or local_gap > acc.max_relative_gap:
            acc.max_relative_gap = local_gap
        del bound, over, denom, implied, diff, scale, rel

    # 4. 同来源内无重复目标 + 8. 排序
    if check_order and len(t) > 1:
        # 块内错位对：排除"本块每个来源的第一条"。因为块边界落在来源边界上，
        # 每个来源的第一条必然是本块内该来源的第一条，规则在块内仍成立。
        misplaced = np.ones(len(t), dtype=bool)
        offsets = np.concatenate((np.array([0], dtype=np.int64),
                                  np.cumsum(chunk_counts[:-1])))
        misplaced[offsets[chunk_counts > 0]] = False
        # misplaced[0] 恒为 True 的话说明来源 0 在本块且非空 —— 它的第一条
        # 不应参与错位比较，上面已被置 False。再做一次下标 0 的保护。
        prev_sim = np.concatenate(([1.0], s[:-1]))
        prev_target = np.concatenate(([-1], t[:-1]))
        bad_desc = misplaced & (s > prev_sim)
        acc.bad_desc += int(np.count_nonzero(bad_desc))
        tied = misplaced & (s == prev_sim)
        acc.bad_tie += int(np.count_nonzero(tied & (t <= prev_target)))
        acc.duplicate_target += int(np.count_nonzero(misplaced & (t == prev_target)))
        del misplaced, offsets, prev_sim, prev_target
    del t, s, c, chunk_counts, source_of_edge


def validate_arrays(model, max_k=None, positive_threshold=None,
                    check_order=True, check_reachability=True,
                    chunk_edges=DEFAULT_VALIDATE_CHUNK_EDGES):
    """结构不变式校验；返回 ``(problems, stats)``。

    ``problems`` 是**字符串列表**（空列表 = 全部通过）。逐项含义：

    1. ``indptr`` 非空、``indptr[0] == 0``、单调不减、``indptr[-1] == n_edges``。
    2. 下标范围：``target_indices`` 全部落在 ``[0, n_items)``。
    3. 无自环：``target_indices`` 不得等于所在来源的下标。
    4. 同来源内无重复目标。
    5. 每来源 ``<= max_k``（给了 ``max_k`` 时）。
    6. 相似度有限、``> 0``（只存正相似邻居）、``<= 1 + 1e-12``。
    7. 共现为正整数，且 ``<= min(n_like[source], n_like[target])``。
    8. 排序：每来源 ``(-similarity, target_movieId)`` 升序（``check_order``）。
    9. 可达性：``n_like == 0`` 的来源**必须**是空区间（``check_reachability``）。
    10. ``item_ids`` 严格递增（movieId 唯一且升序）。
    11. ``n_like <= n_all`` 且都非负。

    实现上**没有逐边 Python 循环**：逐来源检查用向量化，并按 ``chunk_edges``
    分块（块边界落在来源边界上），使峰值内存与**总边数脱钩**——
    理由见 :func:`_check_edge_chunk` 与 D3 记录。

    :param chunk_edges: 每块的目标边数上界。默认 200 万（峰值约 200 MB 量级，
        远低于 3 GiB 上限）。传 ``None`` 或 <= 0 表示不分块（逐字复现旧行为，
        只用于小模型的等价性对拍）。
    """
    """结构不变式校验；返回 ``(problems, stats)``。

    ``problems`` 是**字符串列表**（空列表 = 全部通过）。逐项含义：

    1. ``indptr`` 非空、``indptr[0] == 0``、单调不减、``indptr[-1] == n_edges``。
    2. 下标范围：``target_indices`` 全部落在 ``[0, n_items)``。
    3. 无自环：``target_indices`` 不得等于所在来源的下标。
    4. 同来源内无重复目标。
    5. 每来源 ``<= max_k``（给了 ``max_k`` 时）。
    6. 相似度有限、``> 0``（只存正相似邻居）、``<= 1 + 1e-12``。
    7. 共现为正整数，且 ``<= min(n_like[source], n_like[target])``。
    8. 排序：每来源 ``(-similarity, target_movieId)`` 升序（``check_order``）。
    9. 可达性：``n_like == 0`` 的来源**必须**是空区间（``check_reachability``）。
    10. ``item_ids`` 严格递增（movieId 唯一且升序）。
    11. ``n_like <= n_all`` 且都非负。

    实现上按"来源边界"向量化：用 ``np.repeat`` 把每条边映射到它的来源，
    再用 ``np.minimum.reduceat`` 之类的归约算每来源的最小/最大值。**没有
    逐边 Python 循环**，全量上千万边也能在秒级跑完。
    """
    problems = []
    stats = {}

    item_ids = np.asarray(model.item_ids)
    indptr = np.asarray(model.indptr)
    targets = np.asarray(model.target_indices)
    sims = np.asarray(model.similarities)
    coocs = np.asarray(model.cooccurrences)
    n_like = np.asarray(model.n_like)
    n_all = np.asarray(model.n_all)

    n_items = int(len(item_ids))
    n_edges = int(len(targets))
    stats['n_items'] = n_items
    stats['n_edges'] = n_edges
    stats['n_sources_with_neighbors'] = int(np.count_nonzero(np.diff(indptr))) \
        if n_items else 0

    # --- 10. item_ids 严格递增 -----------------------------------------
    if n_items and np.any(item_ids[1:] <= item_ids[:-1]):
        bad = int(np.count_nonzero(item_ids[1:] <= item_ids[:-1]))
        problems.append('item_ids not strictly increasing: %d adjacent pairs' % bad)
    if n_items and np.any(item_ids <= 0):
        problems.append('item_ids contains non-positive movieId: %d'
                        % int(np.count_nonzero(item_ids <= 0)))

    # --- 11. n_like / n_all 非负且 n_like <= n_all ----------------------
    if np.any(n_like < 0):
        problems.append('n_like has negative entries: %d'
                        % int(np.count_nonzero(n_like < 0)))
    if np.any(n_all < 0):
        problems.append('n_all has negative entries: %d'
                        % int(np.count_nonzero(n_all < 0)))
    over = n_like > n_all
    if np.any(over):
        problems.append('n_like > n_all for %d items (first pos=%d)'
                        % (int(np.count_nonzero(over)), int(np.argmax(over))))

    # --- 1. indptr 形状 -------------------------------------------------
    # 这一段**必须 fail-fast**：下面所有逐来源检查（``edge_source = repeat(arange, counts)``、
    # ``starts[nonempty]`` 索引等）都以「indptr 非降 且 indptr[-1] == n_edges」为前提。
    # 前提被破坏还继续往下走，得到的是**未分类崩溃**而不是"校验失败"：
    #   - counts 为负 -> np.repeat 抛 `ValueError: repeats may not contain negative values`
    #   - indptr[-1] < n_edges -> edge_source 偏短，随后 starts 索引越界 ->
    #     `IndexError: index N is out of bounds for axis 0 with size M`
    # 实测两种都会发生（review-model 报、Lead 复核），而且 **ModelIOError 不是
    # ValueError 子类**，调用方按 ModelIOError 捕获会漏掉。这与
    # ``load_model(validate=True)`` 的承诺（"任一失败即抛 ModelIOError，绝不返回
    # 半可信对象"）直接冲突。故一旦 indptr 自身矛盾就带着已收集的 problems 提前返回。
    indptr_broken = False
    if n_items and int(indptr[0]) != 0:
        problems.append('indptr[0] = %d, expected 0' % int(indptr[0]))
        indptr_broken = True
    if n_items and np.any(np.diff(indptr) < 0):
        problems.append('indptr is not non-decreasing')
        indptr_broken = True
    if n_items and int(indptr[-1]) != n_edges:
        problems.append('indptr[-1] = %d, expected n_edges = %d'
                        % (int(indptr[-1]), n_edges))
        indptr_broken = True

    counts = np.diff(indptr) if n_items else np.zeros(0, dtype=np.int64)
    if n_items and int(counts.sum()) != n_edges:
        problems.append('sum(indptr diffs) = %d, expected %d'
                        % (int(counts.sum()), n_edges))
        indptr_broken = True
    if max_k is not None and n_items and np.any(counts > int(max_k)):
        worst = int(counts.max())
        problems.append('a source has %d neighbors > max_k = %d' % (worst, int(max_k)))

    if indptr_broken:
        # indptr 不可信 -> "第 k 条边属于哪个来源"没有定义，提前返回而不是继续推到崩溃。
        stats['indptr_broken'] = True
        return problems, stats

    # --- 9. 零好评来源必须为空区间 --------------------------------------
    if check_reachability and n_items:
        zero_like_with_edges = (n_like == 0) & (counts > 0)
        if np.any(zero_like_with_edges):
            problems.append('n_like == 0 but non-empty neighbor interval for '
                            '%d source(s) (first pos=%d)'
                            % (int(np.count_nonzero(zero_like_with_edges)),
                               int(np.argmax(zero_like_with_edges))))

    # --- 全局统计（也用于验证报告）-------------------------------------
    if n_edges:
        stats['n_zero_like_sources'] = int(np.count_nonzero(n_like == 0))
        stats['n_empty_interval_sources'] = int(np.count_nonzero(counts == 0))
    else:
        stats['n_zero_like_sources'] = int(np.count_nonzero(n_like == 0)) \
            if n_items else 0
        stats['n_empty_interval_sources'] = n_items

    if n_edges == 0:
        return problems, stats

    # --- 2. 目标下标范围 -----------------------------------------------
    if np.any(targets < 0) or np.any(targets >= n_items):
        problems.append('target_indices out of range [0, %d): min=%d max=%d'
                        % (n_items, int(targets.min()), int(targets.max())))

    # --- 3-8. 逐来源检查（**分块**向量化）-------------------------------
    # 为什么要分块（D3）：
    #   这一段原本一次性在整条边数组上开临时量——`edge_source` / `bound` /
    #   `denom` / `implied` / `diff` / `scale` / `rel` 各自都是 n_edges 长。
    #   tracemalloc 实测堆峰值约为**边数组的 9.89×**：250k 边→18.9 MB，
    #   全量 2000 万边外推约 **1.5 GB**。而它跑在 **3 GiB 上限**那条被监督的
    #   路径上（`full_train` 的 verify 阶段），全量时很可能直接撞线被终止。
    #   分块后峰值只与 `chunk_edges` 有关，与总边数无关。
    # 语义**完全不变**：块边界落在来源边界（indptr）上，所以"每个来源的第一条
    # 不算错位对"这条规则在块内依然成立——来源不会被拆到两块，跨块也就无需
    # 额外处理。``chunk_edges=None`` 表示不分块（逐字复现旧行为，供对拍）。
    if chunk_edges is None or int(chunk_edges) <= 0:
        effective_chunk = n_edges
    else:
        effective_chunk = int(chunk_edges)
    checks = _ChunkedCheckAccumulator()
    bounds_checkable = bool(np.any(targets >= 0)
                            and np.all(targets < n_items)
                            and np.all(targets >= 0))
    for start_pos, stop_pos, e_start, e_stop in _iter_edge_chunks(
            indptr, counts, n_items, n_edges, effective_chunk):
        _check_edge_chunk(indptr, targets, sims, coocs, n_like, n_items,
                          check_order, e_start, e_stop, start_pos, stop_pos,
                          checks, bounds_checkable=bounds_checkable)

    # 统计：与原实现一致——**始终**按整条数组算 max/min（NaN 时即为 NaN）。
    stats['max_similarity'] = float(sims.max()) if len(sims) else None
    stats['min_similarity'] = float(sims.min()) if len(sims) else None
    if bounds_checkable and checks.max_relative_gap is not None:
        stats['max_relative_similarity_gap'] = checks.max_relative_gap

    # 汇总成与"不分块"时**逐字相同**的 problems（顺序也保持）。
    if checks.self_loops:
        problems.append('self loops: %d edge(s) (first source pos=%d)'
                        % (checks.self_loops, checks.first_self_loop_source))
    # **全局开关**：只要整体存在非有限相似度，就只报这一条、跳过下面两项
    # （与旧实现一致——旧实现用的是全局 `if np.any(~np.isfinite(sims))`）。
    if checks.non_finite:
        problems.append('non-finite similarities: %d' % checks.non_finite)
    else:
        if checks.non_positive_sims:
            problems.append('non-positive similarities stored: %d (only positive '
                            'neighbors may be saved)' % checks.non_positive_sims)
        if checks.above_one:
            problems.append('similarities above 1 + 1e-12: max=%.17g'
                            % checks.max_similarity_seen)
    if checks.non_positive_coocs:
        problems.append('non-positive cooccurrences: %d'
                        % checks.non_positive_coocs)
    if bounds_checkable and checks.cooc_over_bound:
        problems.append('cooccurrence exceeds min(n_like) on %d edge(s) '
                        '(first source pos=%d)'
                        % (checks.cooc_over_bound,
                           checks.first_over_bound_source))
    if bounds_checkable and checks.max_relative_gap > 1e-12:
        problems.append('similarity does not match c / sqrt(n_i*n_j) within '
                        '1e-12 relative: worst=%.3e' % checks.max_relative_gap)
    if check_order:
        if checks.bad_desc:
            problems.append('neighbors not sorted by descending similarity: '
                            '%d adjacent pair(s)' % checks.bad_desc)
        if checks.bad_tie:
            problems.append('similarity ties not broken by ascending '
                            'target_movieId: %d pair(s)' % checks.bad_tie)
        if checks.duplicate_target:
            problems.append('duplicate target within a source: %d pair(s)'
                            % checks.duplicate_target)
    return problems, stats


# ---------------------------------------------------------------------------
# 加载（验证后使用）
# ---------------------------------------------------------------------------

def load_model(model_dir, mmap_mode='r', validate=True, require_status=None,
               max_k=None):
    """读模型：默认**先验证哈希、再验证结构**，全部通过才返回。

    - ``validate=True``：哈希全部一致 + 结构不变式无问题，否则抛
      :class:`ModelIOError`。这是计划书 §4"验证结构和哈希后再使用"的落点。
    - ``require_status``：要求元数据状态**恰好**是给定值。发布/接入流程应传
      ``STATUS_VALIDATED_UNPUBLISHED``，从而拒绝加载未验证产物。
    - 返回 ``(model, metadata)``；``model`` 的数组是 memmap，随用随读。
    """
    meta_path = os.path.join(model_dir, METADATA_FILE)
    # D2：目录里有 `.building` 标记 = 数组可能正在被就地重写（半写状态）。
    # 即便 model.json 还在（陈旧），也必须拒绝——它记录的是**旧数组**的哈希，
    # 而磁盘上的数组可能已是新旧混杂。
    marker = os.path.join(model_dir, BUILDING_MARKER_FILE)
    if os.path.isfile(marker):
        raise ModelIOError(
            'model dir is marked as under construction (%s exists); the arrays '
            'may be half-written by an in-progress save()' % marker)
    if not os.path.isfile(meta_path):
        raise ModelIOError('missing metadata file: %s' % meta_path)
    metadata = read_json(meta_path)
    if int(metadata.get('schema_version', -1)) != SCHEMA_VERSION:
        raise ModelIOError('schema_version %r != %d'
                           % (metadata.get('schema_version'), SCHEMA_VERSION))
    status = metadata.get('status')
    if status not in ALLOWED_STATUSES:
        raise ModelIOError('unknown status %r in %s' % (status, meta_path))
    if require_status is not None and status != require_status:
        raise ModelIOError('status %r != required %r' % (status, require_status))

    if validate:
        hash_ok = verify_hashes(model_dir, metadata)
        bad = sorted(name for name, ok in hash_ok.items() if not ok)
        if bad:
            raise ModelIOError('hash mismatch or missing for: %s' % ', '.join(bad))

    model = NeighborModel.load(model_dir, mmap_mode=mmap_mode)

    if validate:
        # 本函数的契约是"任一失败即抛 ModelIOError"。但 ``validate_arrays`` 在
        # indptr 自相矛盾时仍可能触及裸 IndexError/ValueError（例如 `np.repeat`
        # 收到负 counts）。那种情况下调用方按 ModelIOError 捕获会**漏掉**，
        # 于是"校验失败"变成未分类崩溃 —— 与契约不符。
        # 这里把任何非 ModelIOError 的异常统一转成 ModelIOError，保留原始类型名
        # 便于排查，同时保证契约成立（ModelIOError 不是 ValueError 子类，
        # 所以不能只靠 except ValueError）。
        try:
            problems, _stats = validate_arrays(model, max_k=max_k)
        except ModelIOError:
            raise
        except Exception as error:  # noqa: BLE001 - 契约要求统一成 ModelIOError
            raise ModelIOError(
                'structural validation raised %s: %s'
                % (type(error).__name__, error))
        if problems:
            raise ModelIOError('structural validation failed: %s'
                               % '; '.join(problems))
    return model, metadata


def compute_model_id(run_id):
    """模型 ID 与 run ID 的**唯一**换算：``model-<run_id>``。

    上轮把产物内 ID 写成通用 ``'run'``（因为取了 ``os.path.basename(out)`` 的
    子目录名）。这里把换算集中到一个函数，并要求两个 ID 都是真值形状，
    让"父 run ID / model ID / 产物内 ID 三重一致"成为可测的不变式。
    """
    verify_id_format(run_id, 'run_id')
    return 'model-' + run_id


def build_metadata(run_id, source, parameters, extra=None):
    """装配 :func:`NeighborModel.save` 需要的元数据骨架。

    :param source: 来源指纹 dict（输入路径、字节数、SHA-256、行数等）。
    :param parameters: 构图参数（``max_k`` / ``beta`` / ``positive_threshold`` /
        ``block_size``）。
    """
    verify_id_format(run_id, 'run_id')
    payload = {
        'schema_version': SCHEMA_VERSION,
        'model_id': compute_model_id(run_id),
        'run_id': run_id,
        'status': STATUS_ASSEMBLED_UNVERIFIED,
        'created_at': utc_now_iso(),
        'source': source,
        'parameters': parameters,
    }
    if extra:
        payload.update(extra)
    return payload


def apply_verification_verdict(model_dir, verdict, report_path=None):
    """把**独立核验**的判定写回 ``model.json``，消除两侧口径不一致。

    :param verdict: 核验报告 dict（``verify_full_model`` 的产物），或布尔判定。
        传 dict 时按以下规则判定"核验不通过"：
          - ``verdict['pass']`` 为假；**或**
          - ``verdict['recompute_ran']`` 为假（一次都没重算 —— "没验"不等于"验过"）；
          - 或 ``checks/recompute`` 段存在 ``mismatch_count > 0``。
    :param report_path: 核验报告路径，记进元数据便于追溯。
    :returns: ``(status_before, status_after, changed)``。

    **只降级、不升级。** 这个函数**没有**把状态改成 ``validated_unpublished``
    的能力——那是装配阶段的职责。核验**通过**时它只补一条 ``independent_verification``
    记录，状态保持不变。

    **"没跑"与"跑了但不符"要分开处置**（这是关键设计点）：
      - 核验**真的发现不符**（``pass=False`` 且有过重算/有 mismatch）→
        降级为 :data:`STATUS_VERIFICATION_FAILED`。这是"产物自相矛盾"的情形。
      - 核验**根本没跑**（缺 ``verification.json``、``--skip-recompute``、
        输入不可用）→ **不降级**。因为"操作者拿错输入文件"不该把一份好模型判死；
        那种情形由 ``conclusion_writer`` 如实写"独立核验未执行/未通过"来处理。
    """
    meta_path = os.path.join(model_dir, METADATA_FILE)
    if not os.path.isfile(meta_path):
        raise ModelIOError('missing metadata file: %s' % meta_path)
    metadata = read_json(meta_path)
    before = metadata.get('status')

    if before in TERMINAL_FAILURE_STATUSES:
        # 已降级过：不再改动（单调，不洗白）。
        return before, before, False

    if isinstance(verdict, dict):
        checks = verdict.get('checks') or {}
        recompute = checks.get('recompute') or {}
        recompute_ran = bool(verdict.get('recompute_ran',
                                         (not recompute.get('skipped', False))
                                         and recompute.get('checked_sources')))
        mismatches = recompute.get('mismatch_count') or 0
        passed = bool(verdict.get('pass'))
        # ``n_like_from_input`` 的逐物品不符是**硬证据**：模型里存的 n_like 与
        # 输入算出来的对不上，说明模型**不是**由这份输入构建的。
        # 这一项与 recompute 是否运行无关——恰恰是它不符时 recompute 会被跳过
        # （见 verify_full_model：n_like 不符则逐来源重算"毫无意义"）。
        # 所以只看 recompute_ran 会漏掉这种"输入不符"的失败。
        nlike_problems = 0
        nlike = checks.get('n_like_from_input')
        if isinstance(nlike, dict):
            nlike_problems = int(nlike.get('mismatch_count') or 0)
        # 结构/自环/零好评那几项只要失败，也说明模型本身有问题。
        structural_failed = False
        for key in ('structure', 'no_self_loops', 'zero_like_empty_intervals',
                    'id_consistency'):
            sec = checks.get(key)
            if isinstance(sec, dict) and sec.get('pass') is False:
                structural_failed = True
                break
        # 降级条件：**有确定性的不符证据**（而不是"没跑"）。
        verified_and_failed = (not passed) and (
            recompute_ran or mismatches > 0 or nlike_problems > 0
            or structural_failed)
    else:
        verified_and_failed = not bool(verdict)
        recompute_ran = True
        nlike_problems = 0

    if verified_and_failed:
        metadata['status'] = STATUS_VERIFICATION_FAILED
        metadata['verification_verdict'] = {
            'status_before': before,
            'status_after': STATUS_VERIFICATION_FAILED,
            'at': utc_now_iso(),
            'report_path': report_path,
            'recompute_ran': bool(recompute_ran),
            'n_like_mismatches': int(nlike_problems),
            'reason': ('independent verification from the raw ratings did not '
                       'reproduce this model; the model must not be used'),
        }
        write_json_atomic(meta_path, metadata)
        return before, STATUS_VERIFICATION_FAILED, True

    # 核验通过（或没跑）：状态不动，只如实记录看到过什么。
    metadata['independent_verification'] = {
        'at': utc_now_iso(),
        'report_path': report_path,
        'passed': bool(isinstance(verdict, dict) and verdict.get('pass')),
        'recompute_ran': bool(recompute_ran),
        'note': ('recorded for traceability; the model status is granted by the '
                 'assembly stage, not here'),
    }
    write_json_atomic(meta_path, metadata)
    return before, before, False
