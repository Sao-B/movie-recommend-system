# -*- coding: utf-8 -*-
"""ALS 模型在 ItemCF holdout 协议上的排序评估（不训练，只换打分函数）。

动机
====

现有的系统 ALS 模型（rank50 / reg0.1，按 RMSE 选参训练）只有 RMSE 成绩，
与 ItemCF 的 NDCG@20 / Recall@20 不可比。本脚本把**同一份已训练因子**放到
第五轮 holdout 评测的**同一冻结协议**下重算排序指标，产出可与 ItemCF 直接
同表比较的一行。**不训练任何新模型。**

协议（与 ``out/holdout/fullscale-20261005T095205Z`` 完全一致）：

1. 评测名单 = 冻结的 ``eval_users.txt``（2000 人，salt ``itemcf-fullscale-eval-v1:``），
   运行前重算 ``user_list_sha256`` 与 ``users.json`` 里的 ``eval_sha256`` 对拍；
2. 切分 = 复用 ``heldout_eval.RatingSession``（全体用户按个人时间 80/20，
   训练段取全体用户，留出段只保留评测用户）；重建后对拍冻结的
   ``split_holdout.csv`` 字节哈希，不一致即中止；
3. 相关集 = 留出段 ``rating >= 4.0``（与 ItemCF 同阈值）；已看 = 训练段全部评分；
4. 指标 = ``heldout_eval.per_user_metrics``（与 ItemCF 逐位同口径）。

打分（部署同款在线逻辑，见 ``backend/app/recommendation_core.py``）：

- 用户向量：训练段评分对该 ALS 的物品因子做**非负**最小二乘，正则强度
  ``reg_param * n_ratings``（MLlib 口径，``sqrt(lambda_eff) * I`` 增广后交给
  ``scipy.optimize.nnls``）。这里**复刻**该纯函数（本目录依赖纪律不 import
  backend），等价性由 ``tests/test_heldout_als_eval.py`` 的对拍用例锁定；
- 候选 = 因子表全量物品减去已看；排序 ``(-score, movieId)``；Top-20。

运行位置：VM（输入 ratings.csv 与 MySQL 因子都在那边）；本地只跑测试。

Python 3.6 兼容：无 f-string / dataclasses / walrus。
"""
import argparse
import csv
import json
import math
import os
import sys
import time

import numpy as np
from scipy.optimize import nnls

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf.heldout_eval import (  # noqa: E402
    CorrectnessError, HOLDOUT_CSV, POSITIVE_THRESHOLD, RatingSession, TOP_N,
    TRAIN_CSV, file_sha256, paired_bootstrap, per_user_metrics, read_user_list,
    user_list_sha256, write_csv_atomic, write_json_atomic,
)

#: 评估产出目录（在 holdout 产物目录旁边，不混进正式 run）。
DEFAULT_OUT_PARENT = os.path.join(
    PROJECT_ROOT, 'experiments', 'itemcf', 'out', 'holdout')


def solve_user_vector_explicit(item_factors, ratings, reg_param):
    """复刻 ``backend/app/recommendation_core.solve_user_vector``。

    为什么不复刻成"大概像"：部署侧在线 fold-in 解的是
    ``min ||F^T p - r||^2 + (reg * n_ratings) ||p||^2``（非负约束，
    ``sqrt(lambda_eff) * I`` 增广后交给 nnls）。评估必须与部署**逐位同法**，
    否则评估的不是系统真实给出的推荐。等价性由
    ``tests/test_heldout_als_eval.py::TestSolverEquivalence`` 对拍锁定。
    """
    factors = np.asarray(item_factors, dtype=float)
    values = np.asarray(ratings, dtype=float)
    if factors.ndim != 2 or values.ndim != 1:
        raise ValueError('item_factors must be 2-D and ratings must be 1-D')
    if factors.shape[0] != values.shape[0]:
        raise ValueError('item_factors and ratings row count mismatch')
    if factors.shape[0] == 0:
        raise ValueError('at least one rated item is required')
    if reg_param < 0:
        raise ValueError('reg_param must be nonnegative')
    rank = factors.shape[1]
    lambda_eff = float(reg_param) * factors.shape[0]
    regularizer = np.sqrt(lambda_eff) * np.eye(rank)
    augmented = np.vstack((factors, regularizer))
    target = np.concatenate((values, np.zeros(rank)))
    vector, _ = nnls(augmented, target)
    return vector


def verify_frozen_users(run_dir):
    """读冻结评测名单并与 ``users.json`` 的指纹对拍；返回有序 userId list。"""
    eval_users = read_user_list(os.path.join(run_dir, 'eval_users.txt'))
    with open(os.path.join(run_dir, 'users.json'), encoding='utf-8') as handle:
        users_meta = json.load(handle)
    expected = users_meta.get('eval_sha256')
    actual = user_list_sha256(eval_users)
    if expected and actual != expected:
        raise CorrectnessError(
            'eval user list sha256 mismatch: manifest=%s actual=%s'
            % (expected, actual))
    if int(users_meta.get('counts', {}).get('eval_users', -1)) != len(eval_users):
        raise CorrectnessError('users.json eval_users count != eval_users.txt lines')
    return eval_users


def rebuild_split_and_verify(input_path, work_dir, run_dir, eval_users,
                             test_frac=0.2, chunk_rows=200000, log=None):
    """用同一 ``RatingSession`` 重建切分，并对拍冻结的 ``split_holdout.csv``。

    字节级一致是本评估可信的基石：输入相同 + 切分器相同 => 留出段相同。
    """
    frozen_holdout = os.path.join(run_dir, HOLDOUT_CSV)
    if not os.path.isfile(frozen_holdout):
        raise CorrectnessError('frozen holdout csv not found: %s' % frozen_holdout)
    if not os.path.isdir(work_dir):
        os.makedirs(work_dir)
    session = RatingSession(input_path, work_dir, chunk_rows,
                            test_frac=test_frac, eval_users=eval_users,
                            keep_all_train=True)
    session.build(log=log)
    rebuilt = file_sha256(os.path.join(work_dir, HOLDOUT_CSV))
    frozen = file_sha256(frozen_holdout)
    if rebuilt != frozen:
        raise CorrectnessError(
            'rebuilt holdout csv differs from frozen one: rebuilt=%s frozen=%s '
            '(input file is not the one the protocol was frozen on?)'
            % (rebuilt, frozen))
    if log:
        log('holdout csv verified against frozen protocol (%s)' % frozen[:16])
    return session


def load_factors_from_mysql(host, port, user, password, database, model_version,
                            log=None):
    """从 MySQL 读物品因子；返回 ``(version, reg_param, movie_ids, matrix)``。

    ``movie_ids`` 升序 int64 数组；``matrix`` 形状 ``(n_items, rank)`` float64。
    ``model_version=None`` 时取 ``status='active'`` 的最新版本。
    """
    import pymysql

    conn = pymysql.connect(host=host, port=int(port), user=user,
                           password=password, database=database,
                           charset='utf8mb4')
    try:
        cur = conn.cursor()
        if model_version:
            cur.execute('SELECT version, reg_param, rank FROM model_versions '
                        'WHERE version = %s', (model_version,))
        else:
            cur.execute("SELECT version, reg_param, rank FROM model_versions "
                        "WHERE status = 'active' "
                        'ORDER BY activated_at DESC, generated_at DESC LIMIT 1')
        row = cur.fetchone()
        if row is None:
            raise CorrectnessError('no matching model_version in MySQL')
        version, reg_param, rank = row[0], float(row[1]), int(row[2])
        if log:
            log('model version=%s reg_param=%s rank=%d' % (version, reg_param, rank))
        cur.execute('SELECT movie_id, factors FROM model_item_factors '
                    'WHERE model_version = %s ORDER BY movie_id', (version,))
        movie_ids, rows = [], []
        for movie_id, payload in cur:
            values = json.loads(payload) if isinstance(payload, str) else list(payload)
            if len(values) != rank:
                raise CorrectnessError(
                    'factor rank mismatch for movie %s: %d != %d'
                    % (movie_id, len(values), rank))
            movie_ids.append(int(movie_id))
            rows.append(values)
    finally:
        conn.close()
    matrix = np.asarray(rows, dtype=np.float64)
    if matrix.shape[0] == 0:
        raise CorrectnessError('model_item_factors is empty for %s' % version)
    if not np.isfinite(matrix).all():
        raise CorrectnessError('factor matrix contains non-finite values')
    return version, reg_param, np.asarray(movie_ids, dtype=np.int64), matrix


def load_factors_from_jsonl(path, model_version=None, log=None):
    """测试/离线备用：从 jsonl（每行 ``{"movie_id":..,"factors":[..]}``）读因子。"""
    movie_ids, rows = [], []
    rank = None
    version = model_version or 'jsonl'
    with open(path, encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            values = [float(v) for v in record['factors']]
            if rank is None:
                rank = len(values)
            elif len(values) != rank:
                raise CorrectnessError('factor rank mismatch in %s' % path)
            movie_ids.append(int(record['movie_id']))
            rows.append(values)
    order = np.argsort(np.asarray(movie_ids, dtype=np.int64), kind='mergesort')
    movie_ids = np.asarray(movie_ids, dtype=np.int64)[order]
    matrix = np.asarray(rows, dtype=np.float64)[order]
    reg_param = 0.1
    return version, reg_param, movie_ids, matrix


def collect_user_segments(train_path, holdout_path, eval_users,
                          factor_movie_ids, log=None):
    """流式收集评测用户的训练段评分与留出段相关集。

    :returns: ``(train_by_user, relevant_by_user, support_by_movie)``；
        训练段只保留因子表里存在的电影；相关集 = 留出段 ``rating >= 4.0``；
        ``support_by_movie`` = 训练段好评（>=4.0）人数，供候选门槛用。
    """
    factor_set = set(int(v) for v in factor_movie_ids)
    eval_set = set(int(u) for u in eval_users)
    train_by_user = {}
    support_by_movie = {}
    with open(train_path, newline='') as handle:
        reader = csv.reader(handle)
        for record in reader:
            user = int(record[0])
            movie_id = int(record[1])
            if float(record[2]) >= POSITIVE_THRESHOLD:
                support_by_movie[movie_id] = support_by_movie.get(movie_id, 0) + 1
            if user not in eval_set:
                continue
            if movie_id not in factor_set:
                continue
            train_by_user.setdefault(user, []).append(
                (movie_id, float(record[2])))
    relevant_by_user = {}
    with open(holdout_path, newline='') as handle:
        reader = csv.reader(handle)
        for record in reader:
            user = int(record[0])
            if user not in eval_set:
                continue
            if float(record[2]) >= POSITIVE_THRESHOLD:
                relevant_by_user.setdefault(user, set()).add(int(record[1]))
    if log:
        log('eval users with train ratings: %d; with holdout positives: %d'
            % (len(train_by_user), len(relevant_by_user)))
    return train_by_user, relevant_by_user, support_by_movie


def rank_for_user(user_vector, factor_movie_ids, factor_matrix, seen, top_n,
                  allowed=None):
    """部署同款排序：点积全量打分、排除已看、``(-score, movieId)`` Top-N。

    ``allowed``：候选 movieId 集合（None = 不限制）；用于候选支持度门槛。
    """
    scores = factor_matrix @ user_vector
    candidates = [
        (int(movie_id), float(score))
        for movie_id, score in zip(factor_movie_ids.tolist(), scores.tolist())
        if int(movie_id) not in seen
        and (allowed is None or int(movie_id) in allowed)
    ]
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates[:top_n]


def evaluate(eval_users, train_by_user, relevant_by_user,
             factor_movie_ids, factor_matrix, reg_param, top_n=TOP_N,
             log=None, allowed=None):
    """逐用户求解 + 排序；返回 ``(recommendations, relevants, scored_users)``。

    留出段无正例的用户跳过（与 holdout 轮口径一致，计入 ``users_skipped``）。
    """
    factor_pos = {}
    for pos, movie_id in enumerate(factor_movie_ids.tolist()):
        factor_pos[int(movie_id)] = pos
    recommendations, relevants, scored_users = [], [], []
    skipped_no_relevant = 0
    skipped_no_train = 0
    for user in eval_users:
        relevant = relevant_by_user.get(user)
        if not relevant:
            skipped_no_relevant += 1
            continue
        ratings = train_by_user.get(user)
        if not ratings:
            skipped_no_train += 1
            continue
        rows = [factor_pos[m] for m, _r in ratings if m in factor_pos]
        values = [r for m, r in ratings if m in factor_pos]
        factors = factor_matrix[rows]
        vector = solve_user_vector_explicit(factors, np.asarray(values), reg_param)
        seen = set(m for m, _r in ratings)
        ranked = rank_for_user(vector, factor_movie_ids, factor_matrix,
                               seen, top_n, allowed=allowed)
        recommendations.append([movie_id for movie_id, _s in ranked])
        relevants.append(relevant)
        scored_users.append(user)
        if log and len(scored_users) % 500 == 0:
            log('scored %d users' % len(scored_users))
    return recommendations, relevants, scored_users, skipped_no_relevant, \
        skipped_no_train


def load_compare_per_user(path):
    """读 ItemCF holdout 的 ``per_user.csv``，按 userId 对齐逐用户指标。

    列名兼容两种风格：单方法表用裸名（``ndcg@20``/``recall@20``），
    全量留出轮的多方法表带方法前缀（``ItemCF_ndcg@20``/``ItemCF_recall@20``）。
    """
    result = {}
    with open(path, newline='', encoding='utf-8') as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        recall_col = ('ItemCF_recall@20' if 'ItemCF_recall@20' in columns
                      else 'recall@20')
        ndcg_col = ('ItemCF_ndcg@20' if 'ItemCF_ndcg@20' in columns
                    else 'ndcg@20')
        for col in (recall_col, ndcg_col):
            if col not in columns:
                raise CorrectnessError(
                    'neither ItemCF_ prefixed nor bare %s column in %s '
                    '(has: %s)' % (col, path, columns))
        for record in reader:
            user = int(record['user_id'])
            result[user] = (float(record[recall_col]),
                            float(record[ndcg_col]))
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='ALS ranking evaluation under the ItemCF holdout protocol')
    parser.add_argument('--input', required=True,
                        help='ratings.csv（userId 分组，与冻结协议同源）')
    parser.add_argument('--holdout-run-dir', required=True,
                        help='冻结协议目录（eval_users.txt / users.json / '
                             'split_holdout.csv）')
    parser.add_argument('--out', required=True)
    parser.add_argument('--factors-jsonl', default=None,
                        help='离线因子来源（jsonl）；缺省读 MySQL')
    parser.add_argument('--model-version', default=None)
    parser.add_argument('--config-yaml', default=os.path.join(
        PROJECT_ROOT, 'config.yaml'),
        help='MySQL 凭据来源；--profile 选择用哪一段')
    parser.add_argument('--profile', default='vm', choices=['vm', 'mysql'],
                        help="vm = config.yaml 的 vm 段（VM 本机跑用 localhost），"
                             'mysql = 主库段')
    parser.add_argument('--mysql-host', default=None)
    parser.add_argument('--top-n', type=int, default=TOP_N)
    parser.add_argument('--chunk-rows', type=int, default=200000)
    parser.add_argument('--compare-per-user', default=None,
                        help='ItemCF holdout per_user.csv（做配对 bootstrap）')
    parser.add_argument('--limit-users', type=int, default=None,
                        help='只评前 N 个评测用户（冒烟用；会写 smoke 标记）')
    parser.add_argument('--min-movie-support', type=int, default=0,
                        help='候选电影好评人数门槛（训练段 rating>=4 人数），'
                             '0 = 关闭。对应系统 2026-09-20 的可辨识性门槛。')
    args = parser.parse_args(argv)

    started = time.time()
    run_dir = os.path.abspath(args.holdout_run_dir)
    out_dir = os.path.abspath(args.out)
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise SystemExit('refusing to overwrite non-empty out dir: %s' % out_dir)
    os.makedirs(out_dir)

    def log(message):
        print('[als-eval] %s' % message)
        sys.stdout.flush()

    eval_users = verify_frozen_users(run_dir)
    log('frozen eval users: %d' % len(eval_users))
    if args.limit_users:
        eval_users = eval_users[:args.limit_users]
        log('smoke mode: limit to first %d users' % len(eval_users))

    work_dir = os.path.join(out_dir, 'split_work')
    os.makedirs(work_dir)
    session = rebuild_split_and_verify(args.input, work_dir, run_dir,
                                       eval_users, chunk_rows=args.chunk_rows,
                                       log=log)
    train_path = os.path.join(work_dir, TRAIN_CSV)
    holdout_path = os.path.join(work_dir, HOLDOUT_CSV)

    if args.factors_jsonl:
        version, reg_param, movie_ids, matrix = load_factors_from_jsonl(
            args.factors_jsonl, model_version=args.model_version, log=log)
    else:
        import yaml

        cfg = yaml.safe_load(open(args.config_yaml, encoding='utf-8'))
        section = cfg['vm'] if args.profile == 'vm' else cfg['mysql']
        host = args.mysql_host or ('localhost' if args.profile == 'vm'
                                   else section['host'])
        version, reg_param, movie_ids, matrix = load_factors_from_mysql(
            host, section.get('port', 3306), section['user'],
            section['password'], section['database'],
            args.model_version, log=log)

    train_by_user, relevant_by_user, support_by_movie = collect_user_segments(
        train_path, holdout_path, eval_users, movie_ids, log=log)

    allowed = None
    if args.min_movie_support > 0:
        allowed = set(m for m, n in support_by_movie.items()
                      if n >= args.min_movie_support)
        log('candidate gate: support>=%d keeps %d of %d items'
            % (args.min_movie_support, len(allowed), len(movie_ids)))

    recommendations, relevants, scored_users, skip_rel, skip_train = evaluate(
        eval_users, train_by_user, relevant_by_user, movie_ids, matrix,
        reg_param, top_n=args.top_n, log=log, allowed=allowed)
    log('scored users: %d (skipped: no-relevant=%d no-train=%d)'
        % (len(scored_users), skip_rel, skip_train))

    recalls, ndcgs = per_user_metrics(recommendations, relevants, k=args.top_n)
    summary = {
        'method': 'ALS-ranking-eval(%s)' % version,
        'model_version': version,
        'reg_param': reg_param,
        'protocol': {
            'holdout_run_dir': run_dir,
            'eval_sha256': user_list_sha256(eval_users),
            'holdout_csv_sha256': file_sha256(holdout_path),
            'frozen_holdout_sha256': file_sha256(
                os.path.join(run_dir, HOLDOUT_CSV)),
        },
        'top_n': args.top_n,
        'min_movie_support': args.min_movie_support,
        'candidates_allowed': (len(allowed) if allowed is not None else None),
        'users_scored': len(scored_users),
        'users_skipped_no_relevant': skip_rel,
        'users_skipped_no_train': skip_train,
        'recall@20': sum(recalls) / float(len(recalls)) if recalls else None,
        'ndcg@20': sum(ndcgs) / float(len(ndcgs)) if ndcgs else None,
        'smoke': bool(args.limit_users),
        'seconds': time.time() - started,
    }

    per_user_rows = []
    for index, user in enumerate(scored_users):
        per_user_rows.append({
            'user_id': user,
            'ndcg@20': ndcgs[index],
            'recall@20': recalls[index],
            'relevant_count': len(relevants[index]),
            'returned_count': len(recommendations[index]),
        })
    write_csv_atomic(
        os.path.join(out_dir, 'per_user.csv'),
        ['user_id', 'ndcg@20', 'recall@20', 'relevant_count', 'returned_count'],
        per_user_rows)

    rec_rows = []
    for index, user in enumerate(scored_users):
        for rank_, movie_id in enumerate(recommendations[index], start=1):
            rec_rows.append({'user_id': user, 'rank': rank_,
                             'movie_id': movie_id})
    write_csv_atomic(os.path.join(out_dir, 'recommendations.csv'),
                     ['user_id', 'rank', 'movie_id'], rec_rows)

    if args.compare_per_user:
        baseline = load_compare_per_user(args.compare_per_user)
        pairs = [(index, user) for index, user in enumerate(scored_users)
                 if user in baseline]
        if pairs:
            als_ndcg = [ndcgs[index] for index, _u in pairs]
            cf_ndcg = [baseline[user][1] for _i, user in pairs]
            als_recall = [recalls[index] for index, _u in pairs]
            cf_recall = [baseline[user][0] for _i, user in pairs]
            summary['paired_vs_itemcf'] = {
                'users': len(pairs),
                'ndcg@20': paired_bootstrap(als_ndcg, cf_ndcg),
                'recall@20': paired_bootstrap(als_recall, cf_recall),
            }

    write_json_atomic(os.path.join(out_dir, 'summary.json'), summary)
    write_json_atomic(os.path.join(out_dir, 'run_status.json'), {
        'status': 'success', 'seconds': summary['seconds'],
        'model_version': version, 'users_scored': len(scored_users),
    })
    log('summary: ndcg@20=%.6f recall@20=%.6f users=%d (%.1fs)'
        % (summary['ndcg@20'], summary['recall@20'],
           summary['users_scored'], summary['seconds']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
