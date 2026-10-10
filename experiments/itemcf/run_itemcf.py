# -*- coding: utf-8 -*-
"""ItemCF 实验运行编排（计划书 5、10 节）：输入读取、模式解析、构图/评分、缓存校验、
状态与产物写出。

模式（计划书 4.1 / 10.2）：

- ``grid``：3×3 参数网格（k∈{20,50,100} × β∈{0,10,50}），共享一次共现计算，
  每 β 一张 max_k 邻居图，k 从该 β 图取前缀。
- ``repeat``：选定配置在**新进程**里从训练评分独立重建（不读第一次图缓存），
  与首次结果对拍（计划书 8 节）。
- ``shuffle``：固定选定邻居图，用错配训练历史打分（seed=42 循环移位 donor）。

失败处理（计划书 10.3）：单配置失败记 ``failed`` 并保留原因/退出码/日志；共享
构图失败时依赖该图的配置记 ``blocked``；输入/切分/索引/有限性/证据上限/缓存归属/
代码哈希不一致是公共正确性问题，立即暂停后续比较；摘要临时文件写完后原子替换。

Python 3.6 兼容。SciPy 缺失时构图阶段明确报错（``blocked``），不自动安装。
"""
import argparse
import csv
import hashlib
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))

# 计划书 10.2 的调用方式是 ``spark-submit ... experiments/itemcf/run_itemcf.py``，
# 即**按脚本路径**执行；而本模块内部用 ``experiments.itemcf.*`` 绝对导入。
# 直接按路径运行时项目根不在 sys.path 上，会 ModuleNotFoundError。
# 这里补一次根目录，让"按脚本跑"和"-m"两种入口都能用（幂等，不影响包导入）。
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import baselines as itemcf_baselines  # noqa: E402
from experiments.itemcf import baselines  # noqa: E402  (测试与调用方按此名取纯函数)
from experiments.itemcf.evaluation import (  # noqa: E402
    BOOTSTRAP_REPEATS, BOOTSTRAP_SEED, DEFAULT_K_VALUES, POSITIVE_THRESHOLD,
    build_eval_context, candidate_universe_hash, evidence_from_history,
    fixed_case_users, paired_bootstrap, per_user_metrics, protocol_hashes,
    select_config, summarize, user_relevant_hash, user_seen_hash,
)

#: 计划书 10.2 的固定 CLI 默认值。
GRID_DEFAULTS = {
    'block_size': 128,
    'positive_threshold': 4.0,
    'test_frac': 0.2,
    'val_frac': 0.2,
    'top_n': 20,
    'seed': 42,
}

#: 公共正确性问题类别（出现即阻断后续比较，计划书 10.3）。
PUBLIC_ERROR_KINDS = ('input', 'split', 'index', 'finiteness', 'evidence_cap',
                      'cache', 'source')


# ---------------------------------------------------------------------------
# 输入与校验
# ---------------------------------------------------------------------------

def load_rows_csv(path):
    """读 ``(userId, movieId, rating, timestamp)`` CSV（本地/导出的 HDFS 副本）。"""
    rows = []
    with open(path, newline='') as handle:
        reader = csv.DictReader(handle)
        for record in reader:
            rows.append((int(record['userId']), int(record['movieId']),
                         float(record['rating']), int(record['timestamp'])))
    return rows


def load_rows_spark(spark, path):
    """HDFS 输入读取（Spark 负责 I/O，ItemCF 相似度在 driver 分块计算）。"""
    frame = spark.read.csv(path, header=True).select('userId', 'movieId', 'rating', 'timestamp')
    return [(int(r.userId), int(r.movieId), float(r.rating), int(r.timestamp))
            for r in frame.collect()]


def validate_rows(rows):
    """计划书 2.1：有限、0.5–5.0 半分档、(userId, movieId) 唯一、ID/时间戳合法。"""
    seen = set()
    for index, row in enumerate(rows):
        try:
            user, item, rating, ts = row
        except (TypeError, ValueError):
            raise ValueError('row %d must be (userId, movieId, rating, timestamp)' % index)
        if int(user) != user or int(item) != item or int(ts) != ts:
            raise ValueError('row %d has non-integer id/timestamp: %r' % (index, row))
        if int(user) <= 0 or int(item) <= 0 or int(ts) < 0:
            raise ValueError('row %d has illegal id/timestamp: %r' % (index, row))
        rating = float(rating)
        if rating != rating or rating in (float('inf'), float('-inf')):
            raise ValueError('row %d has non-finite rating: %r' % (index, row))
        if rating < 0.5 or rating > 5.0 or abs(rating * 2 - round(rating * 2)) > 1e-9:
            raise ValueError('row %d rating outside 0.5..5.0 half steps: %r' % (index, row))
        key = (int(user), int(item))
        if key in seen:
            raise ValueError('duplicate (userId, movieId) pair: %r' % (key,))
        seen.add(key)
    return True


# ---------------------------------------------------------------------------
# 源码哈希与缓存身份
# ---------------------------------------------------------------------------

def source_hashes(paths):
    """子进程开始前/退出后核验的源码哈希（计划书 10.3）。"""
    out = {}
    for path in paths:
        with open(path, 'rb') as handle:
            out[os.path.basename(path)] = hashlib.sha256(handle.read()).hexdigest()
    return out


def itemcf_source_paths():
    return [os.path.join(MODULE_DIR, name) for name in sorted(os.listdir(MODULE_DIR))
            if name.endswith('.py')]


def cache_identity(input_md5, rows, config, item_ids):
    """邻居图缓存归属：输入指纹 + 行数 + 配置 + 索引（形状相同不是匹配依据）。"""
    payload = {'input_md5': input_md5, 'rows': rows, 'config': config,
               'item_ids': list(item_ids)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()


def cache_matches(stored, current):
    """缓存归属一致才允许复用；不一致按公共正确性问题阻断（计划书 10.3）。"""
    return stored == current


# ---------------------------------------------------------------------------
# 摘要原子写 / 校验读
# ---------------------------------------------------------------------------

def write_summary_atomic(path, payload):
    """摘要临时文件写完后原子替换（计划书 10.3）。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def read_summary_checked(path):
    """读摘要并校验结构；损坏/截断/缺 status 一律 ValueError（不静默接受）。"""
    with open(path, encoding='utf-8') as handle:
        try:
            payload = json.load(handle)
        except ValueError:
            raise ValueError('corrupt or truncated summary: %s' % path)
    if not isinstance(payload, dict) or 'status' not in payload:
        raise ValueError('summary missing status field: %s' % path)
    return payload


# ---------------------------------------------------------------------------
# CLI（计划书 10.2）
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description='ItemCF independent experiment runner')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--mode', choices=('grid', 'repeat', 'shuffle'), default='grid')
    parser.add_argument('--neighbor-k', nargs='+', type=int, default=[20, 50, 100])
    parser.add_argument('--beta', nargs='+', type=float, default=[0, 10, 50])
    parser.add_argument('--block-size', type=int, default=GRID_DEFAULTS['block_size'])
    parser.add_argument('--positive-threshold', type=float,
                        default=GRID_DEFAULTS['positive_threshold'])
    parser.add_argument('--test-frac', type=float, default=GRID_DEFAULTS['test_frac'])
    parser.add_argument('--val-frac', type=float, default=GRID_DEFAULTS['val_frac'])
    parser.add_argument('--top-n', type=int, default=GRID_DEFAULTS['top_n'])
    parser.add_argument('--seed', type=int, default=GRID_DEFAULTS['seed'])
    parser.add_argument('--selected-config', default=None,
                        help='repeat/shuffle 必需：指向已冻结配置 JSON')
    args = parser.parse_args(argv)
    if args.mode in ('repeat', 'shuffle') and not args.selected_config:
        parser.error('--selected-config is required for mode=%s' % args.mode)
    if args.mode == 'grid' and args.selected_config:
        parser.error('--selected-config is only for repeat/shuffle')
    return args


def config_name(neighbor_k, beta):
    """配置目录名：**唯一**命名处，调用方与子进程必须共用。

    历史上 :func:`build_commands` 用 ``'k%d_beta%s' % (k, b)`` 命名，而调用方传入的
    ``beta`` 可能是 ``0``（int）也可能是 ``0.0``（argparse ``type=float`` 解析结果），
    两者格式化结果不同：``k20_beta0`` 与 ``k20_beta0.0``。于是汇总层按一个名字找目录、
    子进程按另一个名字写目录，"退出码 0 但产物全空"就成了一种静默失败。
    命名必须只留一处，且对 int/float 同值给出同一结果。
    """
    value = float(beta)
    text = '%g' % value  # 0.0 -> '0'，10.0 -> '10'，0.5 -> '0.5'
    return 'k%d_beta%s' % (int(neighbor_k), text)


def build_commands(spark_submit, input_path, out, mode='grid',
                   neighbor_k=(20, 50, 100), beta=(0, 10, 50),
                   selected_config=None, block_size=GRID_DEFAULTS['block_size'],
                   top_n=GRID_DEFAULTS['top_n'], seed=GRID_DEFAULTS['seed']):
    """生成子进程命令清单（每个配置一条独立命令，失败互不牵连）。"""
    commands = []
    pairs = ([(k, b) for k in neighbor_k for b in beta] if mode == 'grid'
             else [(neighbor_k[0], beta[0])])
    for k, b in pairs:
        # β 统一按 argparse 的 type=float 口径写成浮点字面量，保证子进程重新解析后
        # 的 (k, beta) 与这里的配置名一一对应（'0' 会被解析成 0.0）。
        command = [spark_submit, os.path.join(MODULE_DIR, 'run_itemcf.py'),
                   '--input', input_path, '--out', out,
                   '--mode', mode, '--neighbor-k', str(int(k)),
                   '--beta', repr(float(b)),
                   '--block-size', str(block_size), '--top-n', str(int(top_n)),
                   '--seed', str(int(seed))]
        if selected_config:
            command += ['--selected-config', selected_config]
        commands.append({'name': config_name(k, b), 'neighbor_k': k, 'beta': b,
                         'mode': mode, 'command': command})
    return commands


# ---------------------------------------------------------------------------
# 产物累积器
# ---------------------------------------------------------------------------

class TopRecommendationAccumulator(object):
    """按 (-score, movie_id) 保留 top_n，附证据字段（计划书 3.3 / 11）。"""

    def __init__(self, top_n, case_users=(), contributions=None):
        self.top_n = top_n
        self.case_users = set(case_users)
        self.contributions = contributions or {}
        self.items = []

    def add(self, movie_id, score, evidence=None, user_id=None):
        record = dict(evidence or {})
        record['movie_id'] = int(movie_id)
        record['score'] = float(score)
        if 'contrib_sum' not in record:
            record['contrib_sum'] = float(score)
        if user_id is not None:
            record['user_id'] = int(user_id)
        user_key = record.get('user_id')
        if user_key in self.case_users:
            contribs = self.contributions.get(user_key, {}).get(int(movie_id))
            if contribs is not None:
                record['contributions'] = contribs
        self.items.append((record['movie_id'], record['score'], record))
        self.items.sort(key=lambda t: (-t[1], t[0]))
        del self.items[self.top_n:]

    def top(self):
        return list(self.items)


# ---------------------------------------------------------------------------
# repeat 对拍与 shuffle donor（计划书第 8 节）
# ---------------------------------------------------------------------------

def run_repeat_comparison(neighbors_a, neighbors_b, recs_a, recs_b,
                          users_a, users_b, metrics_a, metrics_b, tolerance=1e-9):
    """独立重建一致性：邻居 ID/排序、推荐 ID/排序、用户列表完全一致，浮点差 <= 1e-9。"""
    def graph_order(graph):
        return dict((source, [edge[0] for edge in edges])
                    for source, edges in graph.items())

    def rec_order(recs):
        return dict((user, [row[0] for row in rows]) for user, rows in recs.items())

    neighbor_order_equal = graph_order(neighbors_a) == graph_order(neighbors_b)
    recommendation_order_equal = rec_order(recs_a) == rec_order(recs_b)
    users_equal = list(users_a) == list(users_b)

    max_gap = 0.0
    for source, edges in neighbors_a.items():
        other = dict((e[0], e) for e in neighbors_b.get(source, ()))
        for edge in edges:
            twin = other.get(edge[0])
            if twin is None:
                max_gap = float('inf')
                continue
            max_gap = max(max_gap, abs(float(edge[1]) - float(twin[1])))
    for user, rows in recs_a.items():
        other = dict((r[0], r) for r in recs_b.get(user, ()))
        for row in rows:
            twin = other.get(row[0])
            if twin is None:
                max_gap = float('inf')
                continue
            max_gap = max(max_gap, abs(float(row[1]) - float(twin[1])))
    metric_diff = 0.0
    for key in metrics_a:
        if key in metrics_b and metrics_a[key] is not None and metrics_b[key] is not None:
            metric_diff = max(metric_diff, abs(float(metrics_a[key]) - float(metrics_b[key])))

    consistent = (neighbor_order_equal and recommendation_order_equal and users_equal
                  and max_gap <= tolerance and metric_diff <= tolerance)
    return {'consistent': consistent,
            'neighbor_order_equal': neighbor_order_equal,
            'recommendation_order_equal': recommendation_order_equal,
            'users_equal': users_equal,
            'max_abs_float_diff': max_gap,
            'max_abs_metric_diff': metric_diff,
            'tolerance': tolerance}


def build_shuffle_donors(user_ids, seed=42):
    """seed=42 把训练用户排成随机序列后循环移一位：错配是无不动点置换。

    每位目标用户恰好分配另一位用户的历史，每个历史恰好用一次（计划书 8 节）。
    """
    users = sorted(user_ids)
    if len(users) < 2:
        raise ValueError('shuffle needs at least 2 users')
    pool = list(users)
    random.Random(seed).shuffle(pool)
    return dict((user, pool[(index + 1) % len(pool)])
                for index, user in enumerate(pool))


def shuffled_histories(histories, donors):
    """按 donor 映射取错配历史：``shuffled[u] = H_{donor[u]}``。"""
    out = {}
    for user, donor in donors.items():
        out[user] = set(histories[donor])
    return out


# ---------------------------------------------------------------------------
# 复用产物（计划书 4.2 / 11）
# ---------------------------------------------------------------------------

def reused_als_protocol(folder):
    """读旧 ALS 网格产物 summary.json 的 protocol，核对切分/候选/用户口径。"""
    with open(os.path.join(folder, 'summary.json'), encoding='utf-8') as handle:
        payload = json.load(handle)
    return payload['protocol']


def _row_user(row):
    """取用户列；真实 ALS 产物叫 ``user``，本仓库 ItemCF 产物叫 ``user_id``。

    注意**不能**写 ``row.get('user', row['user_id'])``——``dict.get`` 的默认值是
    立即求值的，缺 ``user_id`` 时就地 KeyError（这个坑真踩过一次）。
    """
    value = row.get('user')
    if value in (None, ''):
        value = row.get('user_id')
    if value in (None, ''):
        raise ValueError('row has neither user nor user_id: %r' % (row,))
    return int(value)


def load_reused_top(folder):
    """读旧产物 top_recommendations.csv → ``{user: [(movie_id, score), ...]}``。

    真实产物的列名是 ``user`` / ``movie_id`` / ``raw_prediction``（不是 ``user_id`` /
    ``raw_score``），两个名字都接受，免得换一份产物就读不出来。
    """
    path = os.path.join(folder, 'top_recommendations.csv')
    out = {}
    with open(path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            user = _row_user(row)
            raw = row.get('raw_prediction')
            if raw is None or raw == '':
                raw = row.get('raw_score')
            out.setdefault(user, []).append(
                (int(row['movie_id']), float(raw) if raw not in (None, '') else None))
    for user in out:
        # 并列规则与计划书一致：分数降序、平票 movieId 升序（原产物 rank 列已排好，
        # 但重排一次保证不依赖上游是否保序）。
        out[user].sort(key=lambda t: (-(t[1] if t[1] is not None else 0.0), t[0]))
    return out


def load_reused_als_users(folder):
    """读 ``per_user.csv`` 的有序用户清单（复用产物的评测用户序，计划书 4.2）。"""
    path = os.path.join(folder, 'per_user.csv')
    users = []
    with open(path, newline='', encoding='utf-8') as handle:
        for row in csv.DictReader(handle):
            users.append(_row_user(row))
    return users


# ---------------------------------------------------------------------------
# 对照方法入口（计划书 4.1：一张表覆盖全部方法）
# ---------------------------------------------------------------------------

#: 需要报 RMSE / 双越界率的方法（其余显式 N/A，计划书 4.1）。
RMSE_METHODS = ('BiasOnly', 'ALS-ref', 'ALS-best-ranking')

#: 对照方法固定清单与顺序（计划书 4.1）。
#: ``kind='reused_als'`` 的方法不由 :func:`evaluate_comparison_method` 求值，
#: 必须走 :func:`load_reused_als` 读磁盘产物并先过协议校验。
COMPARISON_METHODS = (
    {'name': 'MostPop', 'kind': 'baseline', 'config': 'MostPop',
     'reports_rmse': False,
     'description': '训练段 n_all(i) 降序、平票 movieId 升序（复用 pilot most_popular）'},
    {'name': 'LikePop', 'kind': 'baseline', 'config': 'LikePop',
     'reports_rmse': False,
     'description': '训练段 n_like(i)（rating>=4 人数）降序、平票 movieId 升序'},
    {'name': 'BiasOnly', 'kind': 'baseline', 'config': 'BiasOnly(lambda=0.1,rounds=10)',
     'reports_rmse': True,
     'description': 'mu + b_u + b_i，lambda=0.1、10 轮、按观测数正则（计划书 4.3）'},
    {'name': 'ALS-ref', 'kind': 'reused_als', 'config': 'ALS(rank=50,reg=0.1)',
     'reports_rmse': True, 'run_id': '20261003T125319Z',
     'reuse_folder': 'rank50_reg0.1',
     'description': '复用 rank50_reg0.1 真实产物；协议校验后逐用户重算（reused_verified）'},
    {'name': 'ALS-best-ranking', 'kind': 'reused_als', 'config': 'ALS(rank=50,reg=0.01)',
     'reports_rmse': True, 'run_id': '20261003T125319Z',
     'reuse_folder': 'rank50_reg0.01',
     'description': '复用 rank50_reg0.01；同一验证集选出的最佳排序配置，非盲测'},
)

#: 复用 ALS 的两条方法名（协议校验/用户序核对对它们必须生效）。
ALS_REUSE_METHODS = tuple(m['name'] for m in COMPARISON_METHODS
                          if m['kind'] == 'reused_als')


def comparison_methods():
    """对照方法清单（固定顺序；返回副本，调用方改不动本模块的常量）。"""
    return [dict(method) for method in COMPARISON_METHODS]


def comparison_method_spec(name):
    """按名字取方法规格；未知名字 KeyError（不静默返回 None）。"""
    for method in COMPARISON_METHODS:
        if method['name'] == name:
            return dict(method)
    raise KeyError('unknown comparison method: %r' % (name,))


def _seen_lists(context):
    return [context.seen.get(u, set()) for u in context.eval_users]


def _relevant_lists(context):
    return [context.relevant[u] for u in context.eval_users]


def _metric_block(recommendations, relevants, users, context, top_n, evidence=None):
    """逐用户指标 + 聚合诊断（走 evaluation 的纯函数，不另写一套口径）。

    :param evidence: 可选 ``{序号: E_u}``。**只有 ItemCF 类方法**才有"有邻域证据
      的候选"这个概念（``E_u = {i in C_u : score(u,i) > 0}``）；MostPop / LikePop /
      BiasOnly / ALS 是对**全部候选**排序，没有证据集，必须保持 ``N/A``——
      给它们硬塞一个证据集会把"证据上限"变成恒等于 1 的无意义数字。
      所以默认 ``None``，由调用方按方法语义显式传入。
    """
    metrics = per_user_metrics(recommendations, relevants, k_values=DEFAULT_K_VALUES,
                               evidence=evidence)
    summary = summarize(recommendations, relevants, users, k_values=DEFAULT_K_VALUES,
                        top_n=top_n, item_universe=context.item_ids,
                        n_all=context.n_all,
                        candidates=dict(enumerate(
                            [context.candidates.get(u, set()) for u in users])),
                        evidence=evidence)
    block = {
        'users_scored': metrics['users_scored'],
        'ndcg@20': (sum(metrics['ndcg@20']) / len(metrics['ndcg@20'])
                    if metrics['ndcg@20'] else None),
        'recall@20': (sum(metrics['recall@20']) / len(metrics['recall@20'])
                      if metrics['recall@20'] else None),
        'ndcg@5': (sum(metrics['ndcg@5']) / len(metrics['ndcg@5'])
                   if metrics['ndcg@5'] else None),
        'ndcg@10': (sum(metrics['ndcg@10']) / len(metrics['ndcg@10'])
                    if metrics['ndcg@10'] else None),
        'recall@5': (sum(metrics['recall@5']) / len(metrics['recall@5'])
                     if metrics['recall@5'] else None),
        'recall@10': (sum(metrics['recall@10']) / len(metrics['recall@10'])
                      if metrics['recall@10'] else None),
        'full_list_rate': summary['full_list_rate'],
        'fill_rate': summary['fill_rate'],
        'empty_list_rate': summary['empty_list_rate'],
        'catalog_coverage': summary['catalog_coverage'],
        'candidate_coverage': summary['candidate_coverage'],
        'recommended_popularity_mean': summary['recommended_popularity_mean'],
        'recommended_popularity_p50': summary['recommended_popularity_p50'],
        'support_median': summary['recommended_popularity_p50'],
        'low_support_rate': summary['low_support_rate'],
        'returned_count': summary['returned_count'],
        # 计划书 §6.2 证据诊断：仅在调用方给出 E_u 时有值，否则一律 None（N/A）。
        'evidence_positive_coverage': summary['evidence_positive_coverage'],
        'evidence_max_recall@20': summary['evidence_max_recall@20'],
        'evidence_count_p10': summary['evidence_count_p10'],
        'evidence_count_p50': summary['evidence_count_p50'],
        'evidence_count_p90': summary['evidence_count_p90'],
        'evidence_count_zero_users': summary['evidence_count_zero_users'],
    }
    per_user = []
    for index, user in enumerate(users):
        per_user.append({
            'user_id': user,
            'relevant_count': len(relevants[index]),
            'relevant_ids': '|'.join(str(i) for i in sorted(relevants[index])),
            # 候选数与 ItemCF 子进程的 per_user 行同字段（score_from_graph.py），
            # 便于独立核验统一按候选/相关集重算，不因方法不同而缺列。
            'candidate_count': len(context.candidates.get(user, ())),
            'returned_count': metrics['returned'][index],
            'ndcg@20': metrics['ndcg@20'][index],
            'recall@20': metrics['recall@20'][index],
            'ndcg@10': metrics['ndcg@10'][index],
            'recall@10': metrics['recall@10'][index],
            'ndcg@5': metrics['ndcg@5'][index],
            'recall@5': metrics['recall@5'][index],
            'history_len': context.history_len.get(user, 0),
        })
    return block, per_user


def evaluate_comparison_method(spec, context, top_n=GRID_DEFAULTS['top_n']):
    """在给定评测上下文里跑一个对照方法；返回逐用户指标 + 聚合诊断。

    ``spec`` 是 :func:`comparison_method_spec` 的返回；``spec['kind']`` 为
    ``reused_als`` 时**不能**在此求值（要读磁盘产物），调用方须走
    :func:`load_reused_als`——这里显式报错，避免"ALS 悄悄变成空推荐"的静默失败。

    所有方法共用同一份 ``context``（同一候选/同一用户序/同一 Top-N），所以主表
    的对照差值是可比的（计划书 4.1）。
    """
    kind = spec.get('kind')
    if kind == 'reused_als':
        raise ValueError('reused_als method %r must be loaded via load_reused_als()'
                         % spec.get('name'))
    users = list(context.eval_users)
    relevants = _relevant_lists(context)
    seen_lists = _seen_lists(context)
    name = spec['name']

    if name == 'MostPop':
        order = itemcf_baselines.most_popular_order(context.train, len(context.item_ids))
        recommendations = [[i for i in order if i not in seen][:top_n]
                           for seen in seen_lists]
        metrics, per_user = _metric_block(recommendations, relevants, users,
                                          context, top_n)
        metrics['rmse'] = None
        metrics['out_of_range_rate'] = None
    elif name == 'LikePop':
        ranked = itemcf_baselines.like_popular(
            context.train, dict((u, context.seen.get(u, set())) for u in users), top_n)
        recommendations = [[item for item, _count in ranked.get(u, [])] for u in users]
        metrics, per_user = _metric_block(recommendations, relevants, users,
                                          context, top_n)
        metrics['rmse'] = None
        metrics['out_of_range_rate'] = None
    elif name == 'BiasOnly':
        model, objectives = itemcf_baselines.fit_bias_only(
            context.train, return_objectives=True)
        recommendations = []
        for user, seen in zip(users, seen_lists):
            ranked = itemcf_baselines.bias_only_ranking(
                model, user, seen, context.item_ids, top_n)
            recommendations.append([item for item, _score in ranked])
        metrics, per_user = _metric_block(recommendations, relevants, users,
                                          context, top_n)
        bounds = _bias_bounds(model, context)
        metrics.update(bounds)
        metrics['objective_values'] = objectives
        metrics['bias_evidence'] = itemcf_baselines.bias_only_evidence(model)
    else:
        raise KeyError('unknown comparison method: %r' % (name,))

    metrics['method'] = name
    metrics['config'] = spec['config']
    metrics['status'] = 'success'
    metrics['description'] = spec['description']
    return {'method': name, 'config': spec['config'], 'status': 'success',
            'kind': kind, 'metrics': metrics, 'per_user': per_user,
            'recommendations': recommendations,
            'users': users, 'description': spec['description']}


def _bias_bounds(model, context, threshold=0.5, ceiling=5.0):
    """BiasOnly 的评分量纲诊断：验证段 RMSE + 验证/Top-N 的双越界率。

    BiasOnly 的 ``mu + b_u + b_i`` 是唯一带 1–5 量纲的基线，所以能按计划书
    4.1 报 RMSE 与越界率；ItemCF / 各类流行度基线是排序分数，一律 N/A。
    """
    mu, bu, bi = model['mu'], model['bu'], model['bi']
    errors, count = 0.0, 0
    for row in context.val:
        user, item, rating = int(row[0]), int(row[1]), float(row[2])
        prediction = mu + bu.get(user, 0.0) + bi.get(item, 0.0)
        errors += (prediction - rating) ** 2
        count += 1
    rmse = (errors / count) ** 0.5 if count else None
    out_of_range, total = 0, 0
    for user in context.eval_users:
        for item in context.candidates.get(user, ()):
            prediction = mu + bu.get(user, 0.0) + bi.get(item, 0.0)
            total += 1
            if prediction < threshold or prediction > ceiling:
                out_of_range += 1
    return {'rmse': rmse,
            'validation_rows': count,
            'out_of_range_count': out_of_range,
            'out_of_range_total': total,
            'out_of_range_rate': (out_of_range / float(total)) if total else None}


#: 协议核对的字段 → 旧 ALS 产物里可能的名字（按顺序取第一个存在的）。
#: 旧产物的 schema 与本仓库 ItemCF 产物不同，实测口径：
#:   train_rows / validation_rows(不是 val_rows) / rows(三段总数) / candidate_items
#:   / users_scored / relevant_threshold / evaluation_segment，
#: 而 top_n 在 ``config`` 里（不在 ``protocol``）。
PROTOCOL_FIELD_ALIASES = (
    ('train_rows', ('train_rows',)),
    ('val_rows', ('val_rows', 'validation_rows', 'evaluation_rows')),
    ('rows', ('rows',)),
    ('candidate_items', ('candidate_items',)),
    ('users_scored', ('users_scored',)),
    ('top_n', ('top_n',)),
    ('relevant_threshold', ('relevant_threshold',)),
    ('evaluation_segment', ('evaluation_segment',)),
)


def resolve_protocol_field(payload_protocol, payload_config, field):
    """按别名在 protocol（再回落 config）里找字段值；取不到返回 None。"""
    aliases = dict(PROTOCOL_FIELD_ALIASES).get(field, (field,))
    for name in aliases:
        if name in payload_protocol and payload_protocol[name] is not None:
            return payload_protocol[name]
    for name in aliases:
        if name in payload_config and payload_config[name] is not None:
            return payload_config[name]
    return None


def verify_als_protocol(stored, expected, stored_config=None):
    """核对复用 ALS 产物的协议与我们当前口径一致（计划书 4.2）。

    只认可比字段；任何一项不符就是公共正确性问题（``public:input``），必须让后续
    比较停下来，而不是"凑合用"——同一份 CSV 换了切分/候选/用户序，指标就不可比。
    ``stored`` 是旧产物的 ``protocol``；旧产物把 ``top_n`` 放在 ``config`` 里，
    故由 ``stored_config`` 回落取值。
    """
    mismatches = []
    resolved = {}
    for field, _aliases in PROTOCOL_FIELD_ALIASES:
        value = resolve_protocol_field(stored or {}, stored_config or {}, field)
        resolved[field] = value
        if value is None:
            mismatches.append('missing protocol field: %s' % field)
            continue
        ours = (expected or {}).get(field)
        if ours is None:
            continue
        try:
            same = float(value) == float(ours)
        except (TypeError, ValueError):
            same = value == ours
        if not same:
            mismatches.append('%s: reused=%r != current=%r' % (field, value, ours))
    return {'ok': not mismatches, 'mismatches': mismatches,
            'checked': list(f for f, _ in PROTOCOL_FIELD_ALIASES),
            'resolved': resolved}


def expected_protocol(context, top_n=GRID_DEFAULTS['top_n'], segment='validation',
                      total_rows=None):
    """当前这次运行的口径（与 ``verify_als_protocol`` 的 expected 对齐）。

    ``rows`` 是**三段切分的总行数**，不是 train+val：旧 ALS 产物的 ``protocol.rows``
    实测为 304944（= 196614 训练 + 48138 验证 + 60192 保留测试），与 PRECHECK 的
    "评分行数 304944"同一口径。这里必须跟着同一口径，否则复用会被误判成协议不符。

    评测上下文刻意不含测试段（防泄漏），所以总行数由调用方以 ``total_rows`` 显式传入
    （通常是 ``len(train) + len(val) + len(test)``）；不传则记 None，表示该字段无法核对。
    """
    if total_rows is not None:
        rows = int(total_rows)
    else:
        rows = None
    return {
        'train_rows': len(context.train),
        'val_rows': len(context.val),
        'rows': rows,
        'candidate_items': len(context.item_ids),
        'users_scored': len(context.eval_users),
        'top_n': int(top_n),
        'relevant_threshold': POSITIVE_THRESHOLD,
        'evaluation_segment': segment,
    }


def load_reused_als(folder, context=None, expected=None, run_id=None,
                    top_n=GRID_DEFAULTS['top_n'], total_rows=None):
    """载入并**校验**一份复用的 ALS 产物，返回逐用户重算的指标块。

    - ``run_id``：未显式给出时从 ``summary.json`` 的 ``protocol['run_id']`` 取，
      再回落成目录名（计划书 11：保留原 run_id 以便追溯）。
    - ``context`` 给出时必须通过 :func:`verify_als_protocol`，否则抛
      ``RuntimeError('public:input: ...')``——由 :func:`run_grid` 归类成
      ``input`` 并阻断后续比较。
    - 指标一律由 ``top_recommendations.csv`` 的逐用户明细**重算**；``summary.json``
      里声明值只作为 ``summary_declared_*`` 留痕，不用来填主表。
    """
    summary_path = os.path.join(folder, 'summary.json')
    with open(summary_path, encoding='utf-8') as handle:
        payload = json.load(handle)
    protocol = payload.get('protocol') or {}
    stored_config = payload.get('config') or {}
    declared_model = ((payload.get('models') or {}).get('ALS')
                      or (payload.get('models') or {}).get('als') or {})
    resolved_run_id = (run_id or protocol.get('run_id')
                       or os.path.basename(os.path.normpath(folder)))

    if context is not None:
        reference = expected or expected_protocol(context, top_n=top_n,
                                                  total_rows=total_rows)
        report = verify_als_protocol(protocol, reference, stored_config)
        if not report['ok']:
            raise RuntimeError('public:input: reused ALS protocol mismatch in %s: %s'
                               % (folder, '; '.join(report['mismatches'])))

    top = load_reused_top(folder)
    stored_users = load_reused_als_users(folder)
    users = list(context.eval_users) if context is not None else list(stored_users)
    # 用户序必须与本次评测一致；对不上就是协议问题，不能靠"取交集"糊过去。
    if context is not None and users != stored_users:
        raise RuntimeError('public:input: reused ALS user order mismatch in %s '
                           '(%d vs %d users)' % (folder, len(users), len(stored_users)))

    recommendations, relevants, seen_lists = [], [], []
    missing = []
    for user in users:
        rows = top.get(user)
        if rows is None:
            missing.append(user)
            rows = []
        recommendations.append([item for item, _score in rows][:top_n])
        if context is not None:
            relevants.append(context.relevant.get(user, set()))
            seen_lists.append(context.seen.get(user, set()))
    if missing:
        raise RuntimeError('public:input: reused ALS missing users in '
                           'top_recommendations.csv: %s'
                           % ','.join(str(u) for u in missing[:5]))

    result = {'method': 'ALS', 'status': 'reused_verified', 'run_id': resolved_run_id,
              'source_dir': os.path.abspath(folder), 'protocol': protocol,
              'users': users, 'recommendations': recommendations,
              'summary_declared_ndcg@20': declared_model.get('ndcg@20'),
              'summary_declared_recall@20': declared_model.get('recall@20')}
    if context is None:
        result['metrics'] = None
        result['per_user'] = None
        return result

    metrics, per_user = _metric_block(recommendations, relevants, users, context, top_n)
    for index, user in enumerate(users):
        # 复用产物自报的逐用户值一并留痕，便于发现"重算 vs 自报"的系统性偏差。
        per_user[index]['seen_count'] = len(seen_lists[index])
    bounds = declared_model.get('top_prediction_bounds') or {}
    metrics['rmse'] = declared_model.get('rmse')
    metrics['out_of_range_rate'] = (declared_model.get('out_of_range_rate')
                                    if declared_model.get('out_of_range_rate') is not None
                                    else bounds.get('rate'))
    metrics['reused'] = True
    metrics['run_id'] = resolved_run_id
    metrics['status'] = 'reused_verified'
    result['metrics'] = metrics
    result['per_user'] = per_user
    return result



# ---------------------------------------------------------------------------
# 子进程桩（供失败处理测试与无 core 依赖的合成管线）
# ---------------------------------------------------------------------------

def _iter_rows(part):
    """兼容 ``(userId, movieId, rating, ts)`` 行与 ``(原行号, 行)`` 二元组。"""
    for entry in part:
        if len(entry) == 2 and len(entry[1]) == 4:
            yield (int(entry[1][0]), int(entry[1][1]), float(entry[1][2]), int(entry[1][3]))
        else:
            yield (int(entry[0]), int(entry[1]), float(entry[2]), int(entry[3]))


def _stub_write(out, config, train, val, ctx, status='success', reason=None):
    """按真实产物 schema 写一个配置目录（stub 图 = 训练好评二分图的一跳邻居）。

    产物列名与 :func:`_write_run_artifacts` 落盘的 ``per_user.csv`` /
    ``recommendations.csv`` 保持一致（``user_id`` / ``rank`` / ``movie_id`` /
    ``raw_score`` ...），否则合成管线测试会拿到与真实运行不同的 schema，
    独立核验 :mod:`experiments.itemcf.verify_results` 也就无从重算。
    """
    folder = os.path.join(out, config['name'])
    if not os.path.isdir(folder):
        os.makedirs(folder)
    # 注意：temporal_split_three_way 返回的是 (原行号, 行) 二元组（它按索引切分再
    # 取出原行），不是 ``(userId, movieId, rating, timestamp)`` 四元组。直接按四元组
    # 解包会静默抛 ValueError，被 run_grid 记成失败并退化成空产物。
    history = {}
    for user, item, rating, _ts in _iter_rows(train):
        if rating >= POSITIVE_THRESHOLD:
            history.setdefault(user, set()).add(item)
    graph = {}
    for user, sources in sorted(history.items()):
        for source in sorted(sources):
            for other_user, other_sources in sorted(history.items()):
                if other_user == user:
                    continue
                for target in sorted(other_sources):
                    if target == source:
                        continue
                    key = (source, target)
                    if key not in graph:
                        graph[key] = [target, 0.5, 1]
    per_user_rows = []
    rec_rows = []
    from experiments.pilot.metrics import ndcg_at_k, recall_at_k
    for index, user in enumerate(ctx.eval_users):
        seen = ctx.seen.get(user, set())
        sources = history.get(user, set())
        scores, contributions = {}, {}
        for source in sorted(sources):
            for (src, target), edge in sorted(graph.items()):
                if src != source or target in seen or target in sources:
                    continue
                scores[target] = scores.get(target, 0.0) + edge[1]
                contributions.setdefault(target, []).append((source, edge[1]))
        # 无证据候选不入列表（计划书 3.3：零分电影不按 ID 填满）；短列表合法。
        evidence = sorted(scores)
        ranked = sorted(scores.items(), key=lambda t: (-t[1], t[0]))[:config['top_n']]
        recs = [item for item, _s in ranked]
        rel = ctx.relevant[user]
        row = {'user_id': user, 'relevant_count': len(rel),
               'candidate_count': len(ctx.candidates.get(user, ())),
               'evidence_count': len(evidence),
               'returned_count': len(recs),
               'recall@20': recall_at_k(recs, rel, 20) or 0.0,
               'ndcg@20': ndcg_at_k(recs, rel, 20) or 0.0,
               'recall@5': recall_at_k(recs, rel, 5) or 0.0,
               'ndcg@5': ndcg_at_k(recs, rel, 5) or 0.0,
               'recall@10': recall_at_k(recs, rel, 10) or 0.0,
               'ndcg@10': ndcg_at_k(recs, rel, 10) or 0.0,
               'evidence_max_recall@20': (min(20, len(set(rel) & set(evidence)))
                                          / float(len(rel)) if rel else None),
               'evidence_ratio': (len(set(rel) & set(evidence)) / float(len(rel))
                                  if rel else None),
               'history_len': len(sources),
               'relevant_ids': '|'.join(str(i) for i in sorted(rel))}
        per_user_rows.append(row)
        for rank, (item, score) in enumerate(ranked, 1):
            edges = contributions.get(item, [])
            best_source, best_value = (edges[0][0], edges[0][1]) if edges else ('', '')
            rec_rows.append({'user_id': user, 'model': config['name'], 'rank': rank,
                             'movie_id': item, 'raw_score': score,
                             'n_all': ctx.n_all.get(item, 0),
                             'n_like': ctx.n_like.get(item, 0),
                             'evidence_neighbor_count': len(edges),
                             'top_source_movie_id': best_source,
                             'top_source_c': '',
                             'contrib_sum': sum(value for _s, value in edges),
                             'top_source_n_like': ctx.n_like.get(best_source, '')})
    model_summary = {'ndcg@20': (sum(r['ndcg@20'] for r in per_user_rows) / len(per_user_rows)
                                 if per_user_rows else None),
                     'recall@20': (sum(r['recall@20'] for r in per_user_rows)
                                   / len(per_user_rows) if per_user_rows else None),
                     'users_scored': len(per_user_rows),
                     'rmse': None, 'out_of_range_rate': None}
    summary = {'status': status, 'reason': reason, 'config': config,
               'models': {config['name']: model_summary},
               'per_user': per_user_rows, 'recommendations': rec_rows}
    write_summary_atomic(os.path.join(folder, 'summary.json'), summary)
    return summary


def child_failing_stub(argv=None):
    """子进程失败桩：退出码 1。"""
    sys.stderr.write('stub child failed on purpose\n')
    return 1


def child_graph_failure_stub(argv=None):
    """共享构图失败桩：公共错误 kind=cache（图不可用）。"""
    raise RuntimeError('public:cache:shared graph build failed')


def child_protocol_error_stub(argv=None):
    """公共协议错误桩：kind=input。"""
    raise RuntimeError('public:input:protocol mismatch')


def child_source_change_stub(argv=None):
    """运行中源码变化桩：kind=source。"""
    raise RuntimeError('public:source:code changed during run')


def child_slow_stub(argv=None):
    """超时桩。"""
    time.sleep(30)
    return 0


def child_slow_stub_argv(argv):
    """超时桩（真实 argv 契约：接受子进程命令行，与 ``subprocess.call`` 同形）。

    注意与 :func:`child_slow_stub` 的签名差异：``run_grid`` 走 ``child=None`` 分支时
    用 ``subprocess.call``（超时抛 ``subprocess.TimeoutExpired``），而注入桩的分支是
    **直接调用** ``child(argv)``，不会抛 ``TimeoutExpired``——桩一律 ``return 0``
    会被记成成功。所以这个桩必须自己实现超时语义：睡眠时长与 ``run_grid`` 的
    ``timeout`` 无关，由调用方传入的 ``timeout`` 决定是否超时。
    """
    time.sleep(30)
    return 0


def _command_tail(command):
    """去掉解释器/脚本前缀，只留参数（命令形如 [python, script.py, --input ...]）。"""
    tail = list(command)
    while tail and not str(tail[0]).startswith('--'):
        tail.pop(0)
    return tail


def child_stub_pipeline(argv=None):
    """合成管线桩：读真实 CSV、真实切分、写全部产物（不含 core 图）。"""
    args = parse_args(_command_tail(argv) if argv is not None else sys.argv[1:])
    rows = load_rows_csv(args.input)
    validate_rows(rows)
    from experiments.pilot.data import temporal_split_three_way
    train, val, _test = temporal_split_three_way(rows, args.test_frac, args.val_frac)
    ctx = build_eval_context(train, val)
    config = {'name': config_name(args.neighbor_k[0], args.beta[0]),
              'k': args.neighbor_k[0], 'beta': args.beta[0], 'top_n': args.top_n,
              'seed': args.seed, 'block_size': args.block_size}
    _stub_write(args.out, config, train, val, ctx)
    return 0


# ---------------------------------------------------------------------------
# 主编排
# ---------------------------------------------------------------------------

def _classify_child_error(text):
    marker = 'public:'
    if marker in text:
        tail = text[text.index(marker) + len(marker):].split(':', 1)[0].strip()
        if tail in PUBLIC_ERROR_KINDS:
            return tail
    return None


def run_grid(input_path, out, mode='grid', neighbor_k=(20, 50, 100), beta=(0, 10, 50),
             block_size=GRID_DEFAULTS['block_size'], top_n=GRID_DEFAULTS['top_n'],
             seed=GRID_DEFAULTS['seed'], child=None, timeout=1800,
             code_paths=None, log_path=None):
    """按配置串行执行子进程并落 run_status.json；公共错误立即阻断后续比较。

    :param child: 子进程入口（测试注入桩）；默认用 ``subprocess.call`` 跑
      ``python experiments/itemcf/run_itemcf.py ...``。
    :returns: ``{'configs': [...], 'shared_graph_status', 'public_error'}``。
    """
    if not input_path:
        raise ValueError('input path is required')
    commands = build_commands(sys.executable, input_path, out, mode,
                              neighbor_k, beta, block_size=block_size,
                              top_n=top_n, seed=seed)
    code_paths = code_paths or itemcf_source_paths()
    code_before = source_hashes(code_paths)
    code_id = hashlib.sha256(json.dumps(code_before, sort_keys=True).encode('utf-8')).hexdigest()
    entries = []
    public_error = None
    shared_graph_status = 'success'
    for spec in commands:
        if public_error is not None:
            entries.append({'name': spec['name'], 'status': 'blocked',
                            'reason': 'blocked by public error: %s' % public_error,
                            'exit_code': None, 'code_id': code_id})
            continue
        started = time.time()
        exit_code, reason, kind = None, None, None
        try:
            if child is None:
                exit_code = subprocess.call(spec['command'], timeout=timeout)
                if exit_code != 0:
                    reason = 'child exit code %s' % exit_code
            else:
                # 注入桩也受同一超时约束：桩是同步调用，不像 subprocess.call 会抛
                # TimeoutExpired，所以用工作线程 + join(timeout) 复刻同一语义。
                state = {}

                def _invoke(spec=spec, state=state):
                    try:
                        state['exit_code'] = child(_command_tail(spec['command']))
                    except BaseException as error:  # noqa: BLE001 - 交给主线程分类
                        state['error'] = error

                worker = threading.Thread(target=_invoke)
                worker.daemon = True
                worker.start()
                worker.join(timeout)
                if worker.is_alive():
                    # 桩线程无法强杀，但它已 daemon 化，不会阻塞进程退出；
                    # 与 subprocess.call 一样只记超时，后续配置照常执行/阻断。
                    raise subprocess.TimeoutExpired(spec['command'], timeout)
                if 'error' in state:
                    raise state['error']
                exit_code = state.get('exit_code')
                if exit_code != 0:
                    reason = 'child exit code %s' % exit_code
        except subprocess.TimeoutExpired:
            exit_code, reason = -1, 'timeout after %ss' % timeout
        except RuntimeError as error:
            kind = _classify_child_error(str(error))
            reason = 'child raised: %s' % error
            exit_code = 2
        except Exception as error:  # noqa: BLE001 - 失败原因必须落盘
            exit_code, reason = -1, 'child launch error: %s' % error
        code_after = source_hashes(code_paths)
        if code_after != code_before:
            kind = 'source'
            reason = 'Source changed during run; results invalid (public:source)'
            public_error = 'source'
        elif kind is not None:
            public_error = kind
        status = 'success' if exit_code == 0 and kind is None else 'failed'
        if kind in ('cache', 'source', 'input', 'split', 'index', 'finiteness', 'evidence_cap'):
            status = 'failed'
        entries.append({'name': spec['name'], 'status': status, 'reason': reason,
                        'exit_code': exit_code, 'code_id': code_id,
                        'mode': spec['mode'], 'neighbor_k': spec['neighbor_k'],
                        'beta': spec['beta'], 'started': started,
                        'finished': time.time(), 'duration': time.time() - started})
        if kind == 'cache':
            shared_graph_status = 'failed'
    write_summary_atomic(os.path.join(out, 'run_status.json'), entries)
    _write_run_artifacts(out, entries, input_path, mode, top_n)
    if log_path:
        with open(log_path, 'w', encoding='utf-8') as handle:
            for entry in entries:
                handle.write('%s %s %s\n' % (entry['name'], entry['status'], entry['reason']))
    return {'configs': entries, 'shared_graph_status': shared_graph_status,
            'public_error': public_error, 'code_id': code_id}


def _write_run_artifacts(out, entries, input_path, mode, top_n):
    """落 manifest / 汇总表 / 逐用户 / 推荐明细（缺项置 None，报告层显示 N/A）。"""
    summary_rows, per_user_rows, rec_rows = [], [], []
    for entry in entries:
        folder = os.path.join(out, entry['name'])
        summary_path = os.path.join(folder, 'summary.json')
        if entry['status'] == 'success' and not os.path.isfile(summary_path):
            # "退出码 0 但没有产物"是矛盾状态。只在汇总里写空行会让这类联调错位
            # （命名不一致、子进程写错目录）静默通过，所以必须显式改判为失败。
            entry['status'] = 'failed'
            entry['reason'] = 'success exit code but no summary.json written'
        if entry['status'] != 'success' or not os.path.isfile(summary_path):
            summary_rows.append({'name': entry['name'], 'status': entry['status'],
                                 'reason': entry.get('reason'), 'ndcg@20': None,
                                 'recall@20': None, 'rmse': None,
                                 'out_of_range_rate': None,
                                 'code_id': entry['code_id']})
            continue
        try:
            payload = read_summary_checked(summary_path)
        except ValueError as error:
            entry['status'] = 'failed'
            entry['reason'] = str(error)
            summary_rows.append({'name': entry['name'], 'status': 'failed',
                                 'reason': str(error), 'ndcg@20': None,
                                 'recall@20': None, 'rmse': None,
                                 'out_of_range_rate': None,
                                 'code_id': entry['code_id']})
            continue
        model = (payload.get('models') or {}).get(entry['name'], {})
        summary_rows.append({'name': entry['name'], 'status': entry['status'],
                             'reason': entry.get('reason'),
                             'ndcg@20': model.get('ndcg@20'),
                             'recall@20': model.get('recall@20'),
                             'rmse': model.get('rmse'),
                             'out_of_range_rate': model.get('out_of_range_rate'),
                             'code_id': entry['code_id']})
        for row in payload.get('per_user', []):
            record = dict(row)
            record['model'] = entry['name']
            per_user_rows.append(record)
        for row in payload.get('recommendations', []):
            record = dict(row)
            record['model'] = entry['name']
            rec_rows.append(record)
    _write_csv(os.path.join(out, 'summary_table.csv'), summary_rows,
               ['name', 'status', 'reason', 'ndcg@20', 'recall@20', 'rmse',
                'out_of_range_rate', 'code_id'])
    _write_csv(os.path.join(out, 'per_user.csv'), per_user_rows, None)
    _write_csv(os.path.join(out, 'recommendations.csv'), rec_rows, None)
    write_summary_atomic(os.path.join(out, 'manifest.json'), {
        'input': input_path, 'mode': mode, 'top_n': top_n,
        'code_id': entries[0]['code_id'] if entries else None,
        'configs': [e['name'] for e in entries]})


def _write_csv(path, rows, fields):
    if not rows and not fields:
        with open(path, 'w', newline='', encoding='utf-8') as handle:
            csv.writer(handle).writerow(['empty'])
        return
    fields = fields or sorted({key for row in rows for key in row})
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


# ---------------------------------------------------------------------------
# summary_table.csv（计划书 4.1 / 11：全部方法一张表）
# ---------------------------------------------------------------------------

#: summary_table.csv 的固定列序。
SUMMARY_TABLE_COLUMNS = ['method', 'config', 'status', 'reason',
                         'ndcg@20', 'recall@20', 'delta_ndcg_vs_mostpop',
                         'delta_recall_vs_mostpop', 'ndcg@10', 'recall@10',
                         'full_list_rate', 'fill_rate', 'catalog_coverage',
                         'candidate_coverage', 'support_median',
                         'recommended_popularity_mean', 'low_support_rate',
                         'users_scored', 'returned_count', 'cost_seconds',
                         'rmse', 'out_of_range_rate', 'code_id', 'run_id',
                         'notes']


def add_comparison_columns(rows, baseline='MostPop'):
    """补对照列：相对 MostPop 的 NDCG/Recall 差值。

    基准行差值为 0.0（同一次运行的同一 MostPop 行，不是 0 冒充缺失）；
    基准缺失或基准失败时全部记 None（N/A），**不编造 0**。
    """
    base = None
    for row in rows:
        if row.get('method') == baseline or row.get('config') == baseline:
            if row.get('status') == 'success':
                base = row
            break
    out = []
    for row in rows:
        record = dict(row)
        delta_ndcg = delta_recall = None
        if base is not None:
            if base.get('ndcg@20') is not None and record.get('ndcg@20') is not None:
                delta_ndcg = float(record['ndcg@20']) - float(base['ndcg@20'])
            if base.get('recall@20') is not None and record.get('recall@20') is not None:
                delta_recall = float(record['recall@20']) - float(base['recall@20'])
        record['delta_ndcg_vs_mostpop'] = delta_ndcg
        record['delta_recall_vs_mostpop'] = delta_recall
        # 只有 BiasOnly / ALS 报 RMSE 与双越界率，其余一律 N/A（计划书 4.1）。
        if record.get('method') not in RMSE_METHODS:
            record['rmse'] = None
            record['out_of_range_rate'] = None
        record.setdefault('notes', None)
        out.append(record)
    return out


def write_summary_table(path, rows, columns=None):
    """写 ``summary_table.csv``；缺项写空串（报告层渲染成 N/A），不用 0 冒充。"""
    rows = add_comparison_columns(rows)
    columns = columns or SUMMARY_TABLE_COLUMNS
    extra = [c for row in rows for c in row if c not in columns]
    ordered = list(columns)
    for column in sorted(set(extra)):
        if column not in ordered:
            ordered.append(column)
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=ordered, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(dict((k, '' if v is None else v) for k, v in row.items()))
    return path


def comparison_row(result, spec=None, code_id=None, cost_seconds=None):
    """把 :func:`evaluate_comparison_method` / :func:`load_reused_als` 的结果转成表行。"""
    metrics = result.get('metrics') or {}
    name = (spec or {}).get('name') or result.get('method')
    return {
        'method': name,
        'config': (spec or {}).get('config') or result.get('config') or name,
        'status': result.get('status'),
        'reason': result.get('reason'),
        'ndcg@20': metrics.get('ndcg@20'),
        'recall@20': metrics.get('recall@20'),
        'ndcg@10': metrics.get('ndcg@10'),
        'recall@10': metrics.get('recall@10'),
        'full_list_rate': metrics.get('full_list_rate'),
        'fill_rate': metrics.get('fill_rate'),
        'catalog_coverage': metrics.get('catalog_coverage'),
        'candidate_coverage': metrics.get('candidate_coverage'),
        'support_median': metrics.get('support_median'),
        'recommended_popularity_mean': metrics.get('recommended_popularity_mean'),
        'low_support_rate': metrics.get('low_support_rate'),
        'users_scored': metrics.get('users_scored'),
        'returned_count': metrics.get('returned_count'),
        'evidence_positive_coverage': metrics.get('evidence_positive_coverage'),
        'evidence_max_recall@20': metrics.get('evidence_max_recall@20'),
        'evidence_count_p50': metrics.get('evidence_count_p50'),
        'cost_seconds': cost_seconds,
        'rmse': metrics.get('rmse'),
        'out_of_range_rate': metrics.get('out_of_range_rate'),
        'code_id': code_id,
        'run_id': result.get('run_id'),
        'notes': (spec or {}).get('description'),
    }



def main(argv=None):
    args = parse_args(argv)
    if os.path.isdir(args.out) and os.listdir(args.out):
        raise ValueError('refusing to overwrite existing artifacts: %s' % args.out)
    if not os.path.isdir(args.out):
        os.makedirs(args.out)
    return run_grid(args.input, args.out, args.mode, args.neighbor_k, args.beta,
                    block_size=args.block_size, top_n=args.top_n, seed=args.seed)


if __name__ == '__main__':
    sys.exit(main())
