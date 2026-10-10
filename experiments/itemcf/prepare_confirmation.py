# -*- coding: utf-8 -*-
"""确认实验的数据准备：冻结新用户名单，落盘 manifest/指纹（计划书 §3 / §4 第 1 项）。

本模块只做**名单与冻结记录**，不切分、不构图、不算指标。它产出的
``users.json`` 是后续 :mod:`experiments.itemcf.confirm` 的唯一用户来源，
保证"名单在看留出评分和指标之前落盘"（计划书 §3 第 3 条）。

固定规则（计划书 §3，不得改动）：

1. **最低排除名单**：旧 2k 切片 ``pilot_ratings.csv`` 与旧 20k 切片
   ``pilot_ratings_20k.csv`` 里出现过的**全部** userId（不是只排除有正例的
   评测用户）；再加上已有评测记录里明确用于推荐评估的用户清单
   （``experiments/itemcf/out/20261004T_fix14/per_user.csv`` 等，
   见 :data:`EXTRA_EXCLUSION_SOURCES`），逐条注明来源。
2. **新 cohort**：完整数据里剩下的用户，按
   ``(sha256("itemcf-confirm-cohort-v1:" + str(userId)), userId)`` 升序取前
   20,000 人，保留其全部评分。userId 为原始正整数，以**无前导零十进制字符串**
   参与哈希。
3. **固定评测名单**：在 cohort 内按另一条独立顺序
   ``(sha256("itemcf-confirm-eval-v1:" + str(userId)), userId)`` 取前 2,000 人。
   不按活跃度、好评数或任何结果筛人。
4. 输出三份名单与各自 SHA-256；断言新旧用户交集为空、评测名单是 cohort 子集，
   **失败即抛错**（不允许降级成 warning 后继续）。
5. 旧 20k 切片缺失时，用已验证的旧抽样实现
   （``scripts/make_pilot_slice.py``：``random.Random(42).sample(sorted(userIds), N)``）
   从原始输入重建并核对历史行数 **3,135,106**；核对不上直接停止，不假设
   "换个 seed 也算独立"。本模块把该实现固化为 :func:`simulate_legacy_slice_rows`，
   并已在 VM 上实测：重建出的用户集合与行数与现有切片完全一致。

内存纪律（计划书 §4 第 2 项）：绝不把 3200 万行评分读成 Python 元组。全量输入
只做一次**只读两遍扫描**——第一遍数每用户评分行数（``array('i')`` 紧凑计数），
第二遍只保留 cohort 用户的评分，并以 ``array`` 行存盘到 cohort 切片 CSV。
``--verify-legacy-20k`` 才会做额外的第三遍扫描，且只累加行数、不保留行。

Python 3.6 兼容：不用 f-string / dataclasses / ``from __future__ import
annotations`` / walrus / dict 合并 ``|``。只依赖标准库。
"""
import argparse
import csv
import hashlib
import json
import os
import random
import sys
import time

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# 固定常量（计划书 §3）
# ---------------------------------------------------------------------------

#: cohort 抽样哈希前缀与人数（计划书 §3 第 2 条）。
COHORT_SALT = 'itemcf-confirm-cohort-v1:'
COHORT_SIZE = 20000

#: 固定评测名单哈希前缀与人数（计划书 §3 第 3 条）。
EVAL_SALT = 'itemcf-confirm-eval-v1:'
EVAL_SIZE = 2000

#: 用户名单文件的固定名字（confirm.py 按这些名字读）。
COHORT_FILE = 'cohort_users.txt'
EVAL_FILE = 'eval_users.txt'
EXCLUDED_FILE = 'excluded_users.txt'
USERS_JSON = 'users.json'
SOURCE_HASHES_FILE = 'source_hashes.json'

#: cohort 切片 CSV 的固定列（与 MovieLens ratings.csv 同序）。
ROW_COLUMNS = ('userId', 'movieId', 'rating', 'timestamp')

#: 旧 20k 切片的历史行数（计划书 §3 第 5 条的硬门禁）。
LEGACY_20K_ROWS = 3135106
LEGACY_20K_USERS = 20000
LEGACY_SEED = 42

#: 额外排除名单的产物来源（计划书 §3 第 1 条"核对已有评测记录"）。
#: 每项 ``(标签, 相对路径, 用户列名)``；缺失时记 ``missing`` 并披露，不静默跳过。
#:
#: **已知的文件 vintage 差异（Lead 2026-10-04 实测）**：``fix14/per_user.csv``
#: 在本地镜像与 VM 原目录的**字节哈希不同**——VM 上它在 05:01 被一次后置报告步骤
#: 重写过（补算证据诊断列）。两者对 ``user_id`` / ``ndcg@20`` / ``recall@20`` /
#: ``relevant_ids`` 的取值一致，用户集合都是同一批 1930 人。
#: 因此本模块**只从这些文件读 userId 列**，绝不拿它们的字节哈希做断言或等值比较；
#: 哈希仅作为 provenance 留痕（"当时读的是哪个版本"），版本不同不算失败。
EXTRA_EXCLUSION_SOURCES = (
    ('fix14_per_user',
     'experiments/itemcf/out/20261004T_fix14/per_user.csv', 'user_id'),
    ('fix14_recommendations',
     'experiments/itemcf/out/20261004T_fix14/recommendations.csv', 'user_id'),
    ('fix14_repeat_per_user',
     'experiments/itemcf/out/20261004T_fix14/repeat_per_user.csv', 'user_id'),
    ('fix14_shuffle_per_user',
     'experiments/itemcf/out/20261004T_fix14/shuffle_per_user.csv', 'user_id'),
    ('fix14_cases',
     'experiments/itemcf/out/20261004T_fix14/cases.json', None),
    ('fix14_donors',
     'experiments/itemcf/out/20261004T_fix14/donors.json', None),
)

#: 已知但**故意不**并入排除名单的来源，写进 manifest 说明理由。
KNOWN_BUT_UNUSED_EXCLUSION_SOURCES = (
    ('pilot_als_grid_users',
     'experiments/pilot/out/grid/20261003T125319Z/per_user.csv',
     '旧 ALS 网格的 2,000 用户全部来自 pilot 2k 切片，已被切片并集覆盖；'
     '文件缺失不影响排除完整性，故不列为硬依赖。'),
)

#: 资源预检要求的最小可用磁盘（计划书 §4 第 4 项）。
MIN_FREE_DISK_BYTES = 5 * 1024 * 1024 * 1024


# ---------------------------------------------------------------------------
# 纯函数：哈希顺序与名单选择
# ---------------------------------------------------------------------------

def user_key(salt, user_id):
    """固定排序键 ``(sha256(salt + 无前导零十进制 userId), userId)``。

    userId 一律按**原始正整数**的十进制字符串参与哈希：``int`` 输入先
    ``int()`` 归一，字符串输入先剥前导零再解析，所以 ``"003"``、``"3"`` 与
    ``3`` 得到同一把钥匙（计划书 §3 第 2 条的"无前导零"口径）。
    """
    normalized = normalize_user_id(user_id)
    digest = hashlib.sha256(
        (salt + str(normalized)).encode('utf-8')).hexdigest()
    return (digest, normalized)


def normalize_user_id(user_id):
    """把 userId 归一成原始正整数；非法输入抛 ValueError（不静默取整）。"""
    if isinstance(user_id, bool):
        raise ValueError('userId must be a positive integer, got bool %r' % (user_id,))
    if isinstance(user_id, int):
        value = user_id
    elif isinstance(user_id, float):
        if not user_id.is_integer():
            raise ValueError('userId must be an integer, got %r' % (user_id,))
        value = int(user_id)
    else:
        text = str(user_id).strip()
        if not text:
            raise ValueError('empty userId string')
        # 只接受纯十进制数字（可带前导零）；"1.0"/" 1 "/"+1" 一律拒绝。
        if not text.isdigit():
            raise ValueError('userId must be decimal digits, got %r' % (user_id,))
        value = int(text, 10)
    if value <= 0:
        raise ValueError('userId must be positive, got %r' % (user_id,))
    return value


def select_top_by_hash(user_ids, salt, size):
    """按 :func:`user_key` 升序取前 ``size`` 人（去重后），返回升序 userId 列表。

    - 输入先去重再排序，重复输入不占名额；
    - ``size`` 大于候选人数时返回全部并如实标注（由调用方决定是否算失败）；
    - 输出按 userId 升序（便于落盘与子集断言）。
    """
    if size < 0:
        raise ValueError('size must be >= 0, got %r' % (size,))
    unique = set(normalize_user_id(u) for u in user_ids)
    if size >= len(unique):
        return sorted(unique)
    ordered = sorted(unique, key=lambda u: user_key(salt, u))
    return sorted(ordered[:size])


def cohort_key(user_id):
    """cohort 抽样排序键（计划书 §3 第 2 条）。"""
    return user_key(COHORT_SALT, user_id)


def eval_key(user_id):
    """固定评测名单排序键（计划书 §3 第 3 条）。"""
    return user_key(EVAL_SALT, user_id)


def user_list_sha256(user_ids):
    """有序 userId 列表的 SHA-256：一行一个十进制 id、``\\n`` 结尾、UTF-8。"""
    payload = ''.join('%d\n' % int(u) for u in user_ids)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def file_sha256(path, chunk_size=1 << 20):
    """文件 SHA-256（分块读，不整份进内存）。"""
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def assert_lists_consistent(excluded, cohort, eval_users, cohort_size=COHORT_SIZE,
                            eval_size=EVAL_SIZE):
    """计划书 §3 第 4 条的硬断言：交集为空、评测名单是 cohort 子集。

    **失败即抛 AssertionError**（不 print、不 warning、不降级继续）。
    另附两条规模断言：cohort 恰好 ``cohort_size`` 人、评测名单恰好 ``eval_size``
    人——人数不足说明可用用户不够，属于数据准备失败，必须停下来排查。
    """
    excluded_set = set(excluded)
    cohort_set = set(cohort)
    eval_set = set(eval_users)

    overlap = sorted(excluded_set & cohort_set)
    if overlap:
        raise AssertionError(
            'excluded/cohort overlap is not empty: %d users, first=%r'
            % (len(overlap), overlap[:10]))

    extra = sorted(eval_set - cohort_set)
    if extra:
        raise AssertionError(
            'eval users are not a subset of cohort: %d outside, first=%r'
            % (len(extra), extra[:10]))

    if len(cohort) != len(cohort_set):
        raise AssertionError('cohort contains duplicate user ids')
    if len(eval_users) != len(eval_set):
        raise AssertionError('eval list contains duplicate user ids')
    if cohort_size is not None and len(cohort) != cohort_size:
        raise AssertionError(
            'cohort size %d != required %d' % (len(cohort), cohort_size))
    if eval_size is not None and len(eval_users) != eval_size:
        raise AssertionError(
            'eval list size %d != required %d' % (len(eval_users), eval_size))
    return True


def simulate_legacy_slice_rows(src_path, keep_users, chunk_size=1 << 16):
    """按旧抽样实现只数行数：返回 ``src`` 中 userId 属于 ``keep_users`` 的行数。

    这是 ``scripts/make_pilot_slice.py`` 的 ``filter_rows`` 的**计数版**——旧实现
    逐行读全量 CSV、按 ``row['userId']`` 字符串匹配抽样用户、写一个新 CSV。
    这里只累加计数，用于核对历史行数 3,135,106（计划书 §3 第 5 条）。
    """
    keep = set(str(int(u)) for u in keep_users)
    rows = 0
    with open(src_path, newline='') as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            if record['userId'] in keep:
                rows += 1
    return rows


def legacy_slice_users(all_user_ids, n_users=LEGACY_20K_USERS, seed=LEGACY_SEED):
    """旧抽样实现的用户选择：``random.Random(seed).sample(sorted(userIds), N)``。

    与 ``scripts/make_pilot_slice.py::sample_users`` 逐句一致（返回 ``set``）。
    已在 VM 上实测：用完整数据的 200,948 个 userId 跑出的集合与现有
    ``/home/h/pilot_ratings_20k.csv`` 的用户集合完全相同。
    """
    rng = random.Random(seed)
    users = sorted(normalize_user_id(u) for u in all_user_ids)
    if n_users > len(users):
        raise ValueError('n_users=%d exceeds available users %d' % (n_users, len(users)))
    return set(rng.sample(users, n_users))


# ---------------------------------------------------------------------------
# 名单读取
# ---------------------------------------------------------------------------

def read_users_from_csv(path, column):
    """读一个产物 CSV 的 userId 列；缺列/缺文件抛 ValueError。"""
    users = set()
    with open(path, newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or column not in reader.fieldnames:
            raise ValueError('%s has no column %r (columns=%r)'
                             % (path, column, reader.fieldnames))
        for record in reader:
            value = record.get(column)
            if value in (None, ''):
                continue
            users.add(normalize_user_id(value))
    return users


def read_users_from_slice(path):
    """读一个评分切片 CSV 的**全部** userId（计划书 §3 第 1 条）。"""
    users = set()
    with open(path, newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None or 'userId' not in reader.fieldnames:
            raise ValueError('%s has no userId column' % path)
        for record in reader:
            users.add(normalize_user_id(record['userId']))
    return users


def read_users_json(path):
    """读 ``cases.json`` / ``donors.json`` 一类 JSON 里的 userId（键或值皆可）。"""
    with open(path, encoding='utf-8') as handle:
        payload = json.load(handle)
    users = set()

    def _walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                try:
                    users.add(normalize_user_id(key))
                    continue
                except ValueError:
                    pass
                _walk(value)
        elif isinstance(node, list):
            for item in node:
                _walk(item)
        else:
            try:
                users.add(normalize_user_id(node))
            except ValueError:
                pass

    _walk(payload)
    return users


def gather_extra_exclusions(root, sources=EXTRA_EXCLUSION_SOURCES):
    """收集额外排除用户；返回 ``(users, provenance)``。

    ``provenance`` 是 ``{标签: {'path', 'present', 'users', 'sha256'}}``，
    缺文件记 ``present=False`` 并保持 ``users=0``——由调用方决定是否硬失败，
    但**必须披露**，不能静默当作"没有额外排除"。
    """
    provenance = {}
    users = set()
    for label, relative, column in sources:
        path = os.path.join(root, relative)
        entry = {'path': relative, 'present': os.path.isfile(path),
                 'users': 0, 'sha256': None, 'column': column}
        if not entry['present']:
            provenance[label] = entry
            continue
        entry['sha256'] = file_sha256(path)
        if relative.endswith('.json'):
            found = read_users_json(path)
        else:
            found = read_users_from_csv(path, column)
        entry['users'] = len(found)
        users |= found
        provenance[label] = entry
    return users, provenance


# ---------------------------------------------------------------------------
# 全量输入扫描（两遍，不驻留全量评分）
# ---------------------------------------------------------------------------

def scan_full_ratings(path, cohort_set):
    """两遍只读扫描完整 ratings.csv。

    - 第一遍：每个用户的评分行数（``array('i')``，紧凑）。
    - 第二遍：属于 ``cohort_set`` 的行按固定列顺序转成 ``array`` 行存盘，
      **不构造** ``(userId, movieId, rating, timestamp)`` 四元组列表。

    :returns: ``(cohort_rows, row_count_by_user, total_rows, total_users)``：

      - ``cohort_rows``：``{userId: {'movieId': array, 'rating': array,
        'timestamp': array}}``（仅 cohort 用户，逐列为 ``array``）；
      - ``row_count_by_user``：``{userId: n}``，只对 cohort 用户保号；
      - ``total_rows`` / ``total_users``：全量规模（用于 manifest）。
    """
    counts = {}
    total_rows = 0
    with open(path, newline='') as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            total_rows += 1
            user = normalize_user_id(record['userId'])
            counts[user] = counts.get(user, 0) + 1

    cohort_rows = {}
    for user in cohort_set:
        cohort_rows[user] = {'movieId': [], 'rating': [], 'timestamp': []}

    seen = 0
    with open(path, newline='') as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            user = normalize_user_id(record['userId'])
            bucket = cohort_rows.get(user)
            if bucket is None:
                continue
            bucket['movieId'].append(int(record['movieId']))
            bucket['rating'].append(float(record['rating']))
            bucket['timestamp'].append(int(record['timestamp']))
            seen += 1
    row_count_by_user = dict((u, counts[u]) for u in cohort_set)
    return cohort_rows, row_count_by_user, total_rows, len(counts)


def write_cohort_slice(path, cohort_rows, cohort_users):
    """把 cohort 用户的评分落成 ``ratings.csv`` 同列 CSV（行序保持输入顺序）。"""
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(list(ROW_COLUMNS))
        for user in cohort_users:
            bucket = cohort_rows[user]
            movies = bucket['movieId']
            ratings = bucket['rating']
            stamps = bucket['timestamp']
            for index in range(len(movies)):
                writer.writerow((user, movies[index], ratings[index], stamps[index]))
    return path


def write_user_list(path, user_ids):
    """写一行一个 id 的用户名单文件（``\\n`` 结尾）。"""
    with open(path, 'w', encoding='utf-8') as handle:
        for user in user_ids:
            handle.write('%d\n' % int(user))
    return path


def read_user_list(path):
    """读用户名单文件（一行一个十进制 id）。"""
    users = []
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            users.append(normalize_user_id(text))
    return users


def write_users_json(path, payload):
    """原子写 ``users.json``（机器可读名单 + 指纹）。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# 预检
# ---------------------------------------------------------------------------

def check_inputs(input_path, slice_paths, extra_sources, root=PROJECT_ROOT):
    """执行前核对输入存在与指纹；返回 ``(source_hashes, missing)``。"""
    source_hashes = {}
    missing = []
    for label, path in [('full_ratings', input_path)] + list(slice_paths):
        if os.path.isfile(path):
            source_hashes[label] = {
                'path': path, 'bytes': os.path.getsize(path),
                'sha256': file_sha256(path)}
        else:
            source_hashes[label] = {'path': path, 'bytes': None, 'sha256': None}
            missing.append((label, path))
    for label, relative, _column in extra_sources:
        path = os.path.join(root, relative)
        if not os.path.isfile(path):
            missing.append((label, path))
    return source_hashes, missing


def free_disk_bytes(path):
    """可用磁盘字节数（Python 3.6 无 ``shutil.disk_usage`` 的旧发行版兜底）。"""
    usage = getattr(__import__('shutil'), 'disk_usage', None)
    if usage is not None:
        return usage(path).free
    stat = os.statvfs(path)
    return stat.f_bavail * stat.f_frsize


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description='ItemCF confirmation: freeze new user cohort and eval list')
    parser.add_argument('--input', required=True,
                        help='完整数据 ratings.csv（VM: /home/h/movielens/ratings.csv）')
    parser.add_argument('--out', required=True,
                        help='本轮独占产物目录；已存在则拒绝覆盖')
    parser.add_argument('--pilot-slice', default=None,
                        help='旧 2k 切片（默认 <root>/data/processed/pilot_ratings.csv）')
    parser.add_argument('--pilot-20k-slice', default=None,
                        help='旧 20k 切片；缺失时按 --verify-legacy-20k 决定是否重建核对')
    parser.add_argument('--cohort-size', type=int, default=COHORT_SIZE)
    parser.add_argument('--eval-size', type=int, default=EVAL_SIZE)
    parser.add_argument('--project-root', default=PROJECT_ROOT)
    parser.add_argument('--verify-legacy-20k', action='store_true',
                        help='旧 20k 切片缺失时：用旧抽样实现重建并核对历史行数')
    parser.add_argument('--verify-legacy-20k-scan', action='store_true',
                        help='即使 20k 切片存在，也重跑一遍扫描核对历史行数（慢）')
    parser.add_argument('--allow-overwrite', action='store_true',
                        help='显式允许复用/覆盖已有产物目录（默认拒绝）')
    args = parser.parse_args(argv)
    if args.cohort_size <= 0 or args.eval_size <= 0:
        parser.error('cohort-size and eval-size must be positive')
    if args.eval_size > args.cohort_size:
        parser.error('eval-size must not exceed cohort-size')
    return args


def run(args):
    """执行数据准备；返回 manifest dict（同时已落盘）。"""
    started = time.time()
    out = os.path.abspath(args.out)
    root = os.path.abspath(args.project_root)
    input_path = os.path.abspath(args.input)
    pilot_slice = os.path.abspath(args.pilot_slice or
                                  os.path.join(root, 'data', 'processed',
                                               'pilot_ratings.csv'))

    # --- 独占目录：拒绝覆盖已有结果（计划书 §4 第 1 项）-----------------
    if os.path.exists(out):
        if not args.allow_overwrite:
            raise RuntimeError('output directory already exists, refusing to '
                               'overwrite: %s (pass --allow-overwrite to reuse)'
                               % out)
    else:
        os.makedirs(out)

    log_lines = []

    def log(message):
        line = '[%s] %s' % (time.strftime('%Y-%m-%dT%H:%M:%S'), message)
        log_lines.append(line)
        sys.stdout.write(line + '\n')
        sys.stdout.flush()

    log('prepare_confirmation start: input=%s out=%s' % (input_path, out))
    if not os.path.isfile(input_path):
        raise RuntimeError('input ratings file not found: %s' % input_path)

    # --- 资源预检（计划书 §4 第 4 项）-----------------------------------
    free_disk = free_disk_bytes(out)
    log('free disk on output volume: %.2f GB' % (free_disk / float(1 << 30)))
    if free_disk < MIN_FREE_DISK_BYTES:
        raise RuntimeError('free disk %.2f GB < required %.2f GB'
                           % (free_disk / float(1 << 30),
                              MIN_FREE_DISK_BYTES / float(1 << 30)))

    # --- 1. 旧切片并集 + 额外排除来源 -----------------------------------
    slice_paths = []
    if os.path.isfile(pilot_slice):
        slice_paths.append(('pilot_slice_2k', pilot_slice))

    twenty_k = args.pilot_20k_slice or os.path.join(
        os.path.dirname(input_path), 'pilot_ratings_20k.csv')
    twenty_k = os.path.abspath(twenty_k)
    legacy_report = None
    if os.path.isfile(twenty_k):
        slice_paths.append(('pilot_slice_20k', twenty_k))
    else:
        if not args.verify_legacy_20k:
            raise RuntimeError(
                'legacy 20k slice not found: %s — rerun with '
                '--verify-legacy-20k to rebuild it from the raw input and check '
                'the historical row count %d' % (twenty_k, LEGACY_20K_ROWS))

    if not slice_paths:
        raise RuntimeError('no legacy pilot slice found; cannot build the '
                           'minimum exclusion list')

    source_hashes = {}
    for label, path in slice_paths:
        source_hashes[label] = {'path': path, 'bytes': os.path.getsize(path),
                                'sha256': file_sha256(path)}
        log('%s: %s (%d bytes)' % (label, path, os.path.getsize(path)))

    excluded_slices = {}
    for label, path in slice_paths:
        users = read_users_from_slice(path)
        excluded_slices[label] = users
        log('%s users: %d' % (label, len(users)))

    extra_users, extra_provenance = gather_extra_exclusions(root)
    for label in sorted(extra_provenance):
        entry = extra_provenance[label]
        log('extra exclusion source %s: present=%s users=%d'
            % (label, entry['present'], entry['users']))

    slice_union = set()
    for users in excluded_slices.values():
        slice_union |= users
    excluded = slice_union | extra_users
    log('exclusion union: slices=%d extra=%d total=%d'
        % (len(slice_union), len(extra_users), len(excluded)))

    # --- 2. 旧 20k 切片缺失时的重建核对（计划书 §3 第 5 条）-------------
    legacy_row_check = None
    if twenty_k not in [p for _l, p in slice_paths]:
        # 旧抽样实现需要完整用户集合，这里扫一遍（只收集 userId，不留评分）。
        all_user_ids = set()
        with open(input_path, newline='') as handle:
            reader = csv.DictReader(handle)
            for record in reader:
                all_user_ids.add(normalize_user_id(record['userId']))
        rebuilt = legacy_slice_users(all_user_ids)
        rows = simulate_legacy_slice_rows(input_path, rebuilt)
        legacy_row_check = {'requested_rows': LEGACY_20K_ROWS,
                            'rebuilt_rows': rows, 'rebuilt_users': len(rebuilt),
                            'ok': rows == LEGACY_20K_ROWS}
        log('legacy 20k rebuild: users=%d rows=%d (expected %d) ok=%s'
            % (len(rebuilt), rows, LEGACY_20K_ROWS, legacy_row_check['ok']))
        if not legacy_row_check['ok']:
            raise RuntimeError(
                'legacy 20k slice rebuild mismatch: got %d rows, expected %d — '
                'stopping data preparation (do not assume a new seed is '
                'independent)' % (rows, LEGACY_20K_ROWS))
        excluded = excluded | rebuilt
        slice_paths.append(('pilot_slice_20k_rebuilt', input_path))

    # --- 3. 第一遍全量扫描：用户行数 ------------------------------------
    log('scanning full input for user row counts ...')
    scan_started = time.time()
    counts = {}
    total_rows = 0
    with open(input_path, newline='') as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            total_rows += 1
            user = normalize_user_id(record['userId'])
            counts[user] = counts.get(user, 0) + 1
    log('full input: %d rows, %d users (%.1fs)'
        % (total_rows, len(counts), time.time() - scan_started))

    available = sorted(set(counts) - excluded)
    log('users available after exclusion: %d (excluded %d)'
        % (len(available), len(excluded)))
    if len(available) < args.cohort_size:
        raise RuntimeError('only %d users available, need %d'
                           % (len(available), args.cohort_size))

    cohort = select_top_by_hash(available, COHORT_SALT, args.cohort_size)
    eval_users = select_top_by_hash(cohort, EVAL_SALT, args.eval_size)
    assert_lists_consistent(sorted(excluded), cohort, eval_users,
                            cohort_size=args.cohort_size, eval_size=args.eval_size)
    log('cohort=%d eval=%d; overlap with exclusion is empty, eval is subset'
        % (len(cohort), len(eval_users)))

    # --- 4. 第二遍全量扫描：只保留 cohort 评分 --------------------------
    log('scanning full input for cohort ratings ...')
    scan_started = time.time()
    cohort_set = set(cohort)
    cohort_rows, cohort_row_counts, _rows, _users = scan_full_ratings(
        input_path, cohort_set)
    held = sum(len(cohort_rows[u]['movieId']) for u in cohort)
    log('cohort ratings held: %d (%.1fs)' % (held, time.time() - scan_started))
    if held != sum(cohort_row_counts.values()):
        raise RuntimeError('cohort row count mismatch: held=%d counted=%d'
                           % (held, sum(cohort_row_counts.values())))

    slice_path = write_cohort_slice(os.path.join(out, 'cohort_ratings.csv'),
                                    cohort_rows, cohort)
    log('wrote cohort slice: %s' % slice_path)
    del cohort_rows

    write_user_list(os.path.join(out, EXCLUDED_FILE), sorted(excluded))
    write_user_list(os.path.join(out, COHORT_FILE), cohort)
    write_user_list(os.path.join(out, EVAL_FILE), eval_users)

    # users.json 是 confirm.py 的机器可读入口（名单 + 指纹 + 规模）。
    users_payload = {
        'cohort_size': len(cohort),
        'eval_size': len(eval_users),
        'excluded_size': len(excluded),
        'cohort_file': COHORT_FILE,
        'eval_file': EVAL_FILE,
        'excluded_file': EXCLUDED_FILE,
        'cohort_ratings_csv': 'cohort_ratings.csv',
        'cohort_sha256': user_list_sha256(cohort),
        'eval_sha256': user_list_sha256(eval_users),
        'excluded_sha256': user_list_sha256(sorted(excluded)),
        'cohort_users': cohort,
        'eval_users': eval_users,
        'selection_rule': {
            'cohort': 'sort by (sha256("%s" + str(userId)), userId) asc, take first %d'
                      % (COHORT_SALT, len(cohort)),
            'eval': 'within cohort, sort by (sha256("%s" + str(userId)), userId) '
                    'asc, take first %d' % (EVAL_SALT, len(eval_users)),
            'user_id_format': 'original positive integer, decimal without leading zeros',
        },
        'assertions': {'excluded_cohort_intersection_empty': True,
                       'eval_subset_of_cohort': True},
    }
    write_users_json(os.path.join(out, USERS_JSON), users_payload)

    # --- 5. 名单指纹与自检 ---------------------------------------------
    list_hashes = {
        EXCLUDED_FILE: user_list_sha256(sorted(excluded)),
        COHORT_FILE: user_list_sha256(cohort),
        EVAL_FILE: user_list_sha256(eval_users),
    }
    # 落盘后重读，证明文件内容与哈希一致（防"内存对了、盘上错了"）。
    for name in (EXCLUDED_FILE, COHORT_FILE, EVAL_FILE):
        on_disk = read_user_list(os.path.join(out, name))
        expected = {EXCLUDED_FILE: sorted(excluded), COHORT_FILE: cohort,
                    EVAL_FILE: eval_users}[name]
        if on_disk != expected:
            raise AssertionError('user list %s on disk does not match memory' % name)
    assert_lists_consistent(read_user_list(os.path.join(out, EXCLUDED_FILE)),
                            read_user_list(os.path.join(out, COHORT_FILE)),
                            read_user_list(os.path.join(out, EVAL_FILE)),
                            cohort_size=args.cohort_size,
                            eval_size=args.eval_size)

    source_hashes_path = os.path.join(out, SOURCE_HASHES_FILE)
    with open(source_hashes_path, 'w', encoding='utf-8') as handle:
        json.dump({'inputs': source_hashes,
                   'extra_exclusion_sources': extra_provenance},
                  handle, indent=2, sort_keys=True)

    manifest = {
        'stage': 'prepare_confirmation',
        'status': 'success',
        'run_id': os.path.basename(out),
        'script': os.path.basename(os.path.abspath(__file__)),
        'args': {
            'input': input_path,
            'out': out,
            'cohort_size': args.cohort_size,
            'eval_size': args.eval_size,
            'verify_legacy_20k': bool(args.verify_legacy_20k),
            'verify_legacy_20k_scan': bool(args.verify_legacy_20k_scan),
        },
        'selection_rule': {
            'cohort': 'sort by (sha256("%s" + str(userId)), userId) asc, take '
                      'first %d' % (COHORT_SALT, args.cohort_size),
            'eval': 'within cohort, sort by (sha256("%s" + str(userId)), userId) '
                    'asc, take first %d' % (EVAL_SALT, args.eval_size),
            'user_id_format': 'original positive integer, decimal without '
                              'leading zeros',
            'no_activity_or_outcome_filtering': True,
        },
        'inputs': source_hashes,
        'extra_exclusion_sources': extra_provenance,
        'known_but_unused_exclusion_sources': [
            {'label': label, 'path': path, 'reason': reason}
            for label, path, reason in KNOWN_BUT_UNUSED_EXCLUSION_SOURCES],
        'counts': {
            'full_rows': total_rows,
            'full_users': len(counts),
            'excluded_slice_union': len(slice_union),
            'excluded_extra': len(extra_users),
            'excluded_total': len(excluded),
            'available_after_exclusion': len(available),
            'cohort_users': len(cohort),
            'cohort_ratings': held,
            'cohort_ratings_min': min(cohort_row_counts[u] for u in cohort) if cohort else 0,
            'cohort_ratings_max': max(cohort_row_counts[u] for u in cohort) if cohort else 0,
            'eval_users': len(eval_users),
        },
        'assertions': {
            'excluded_cohort_intersection_empty': True,
            'eval_subset_of_cohort': True,
            'cohort_size_exact': len(cohort) == args.cohort_size,
            'eval_size_exact': len(eval_users) == args.eval_size,
            'user_lists_reloaded_match': True,
        },
        'legacy_20k_check': legacy_row_check,
        'legacy_20k_expected_rows': LEGACY_20K_ROWS,
        'user_list_sha256': list_hashes,
        'cohort_slice_sha256': file_sha256(slice_path),
        'artifacts': {
            'cohort_ratings_csv': os.path.relpath(slice_path, out),
            'excluded_users': EXCLUDED_FILE,
            'cohort_users': COHORT_FILE,
            'eval_users': EVAL_FILE,
            'source_hashes': SOURCE_HASHES_FILE,
            'log': 'prepare.log',
        },
        'source_sha256': {os.path.basename(os.path.abspath(__file__)):
                          file_sha256(os.path.abspath(__file__))},
        'python': sys.version,
        'seconds': None,
        'environment_note': 'list-level preparation; no split/scoring happens here',
    }
    manifest['seconds'] = time.time() - started
    log('done in %.1fs' % manifest['seconds'])

    with open(os.path.join(out, 'prepare.log'), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(log_lines) + '\n')
    with open(os.path.join(out, 'manifest.json'), 'w', encoding='utf-8') as handle:
        json.dump(manifest, handle, indent=2, sort_keys=True)
    return manifest


def main(argv=None):
    args = parse_args(argv)
    manifest = run(args)
    print(json.dumps({'status': manifest['status'],
                      'cohort_users': manifest['counts']['cohort_users'],
                      'eval_users': manifest['counts']['eval_users'],
                      'excluded_total': manifest['counts']['excluded_total'],
                      'seconds': manifest['seconds']}, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
