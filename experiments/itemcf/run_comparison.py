# -*- coding: utf-8 -*-
"""ItemCF 完整对照实验驱动（计划书 4.1 / 6.4 / 10 / 11）。

一次运行产出**全部方法**的 ``summary_table.csv``：

- ItemCF 9 配置（k∈{20,50,100} × β∈{0,10,50}）：3 张邻居图 + 9 次评分，
  同一 β 下 k 从该 β 的 max_k 图取前缀（计划书 5.1）；
- MostPop / LikePop / BiasOnly 三个基线（复用 experiments 的纯函数）；
- ALS-ref（rank50_reg0.1）与 ALS-best-ranking（rank50_reg0.01）两条**真实复用**，
  先过协议校验（同一输入/切分/候选/用户序/Top-N/并列规则），再取逐用户明细重算。

串行执行：任意时刻最多一个计算进程。每张邻居图单独一个子进程（30 分钟超时），
崩溃/超时如实落盘；公共正确性错误（input/split/index/finiteness/evidence_cap/
cache/source）阻断后续比较（计划书 10.3）。

用法::

    python run_comparison.py --input /home/h/pilot_ratings.csv \
        --out /home/h/itemcf_out/<run_id> \
        --reuse-grid /home/h/pilot_out/grid/20261003T125319Z

Python 3.6 兼容（VM 上解释器为 3.6.8）。
"""
import argparse
import csv
import hashlib
import json
import os
import subprocess
import sys
import time

MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(MODULE_DIR))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from experiments.itemcf import run_itemcf as R  # noqa: E402
from experiments.itemcf import verify_results  # noqa: E402
from experiments.itemcf.evaluation import (  # noqa: E402
    BOOTSTRAP_REPEATS, BOOTSTRAP_SEED, POSITIVE_THRESHOLD, build_eval_context,
    candidate_universe_hash, fixed_case_users, paired_bootstrap, per_user_metrics,
    protocol_hashes, select_config, summarize, user_relevant_hash, user_seen_hash,
)
from experiments.pilot.data import temporal_split_three_way  # noqa: E402

#: 子进程超时（计划书 10.3：每子进程 30 分钟）。
CHILD_TIMEOUT = 1800

#: 邻居图最大 k（计划书 5.1：三张 β 图各存最多 100 邻居，k 从中取前缀）。
MAX_K = 100

NEIGHBOR_K = (20, 50, 100)
BETA = (0.0, 10.0, 50.0)


def log(message, path=None):
    stamp = time.strftime('%Y-%m-%dT%H:%M:%S')
    line = '[%s] %s' % (stamp, message)
    sys.stdout.write(line + '\n')
    sys.stdout.flush()
    if path:
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(line + '\n')


def _hash_file(path):
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 邻居图子进程（每 β 一张图，单独进程 + 超时）
# ---------------------------------------------------------------------------

def neighbor_graph_command(python, input_path, out, beta, block_size, max_k):
    return [python, os.path.join(MODULE_DIR, 'build_graph.py'),
            '--input', input_path, '--out', out, '--beta', repr(float(beta)),
            '--block-size', str(int(block_size)), '--max-k', str(int(max_k))]


def run_neighbor_graphs(python, input_path, out, beta_values, block_size,
                        timeout=CHILD_TIMEOUT, log_path=None):
    """串行构建每个 β 的邻居图；返回 ``{beta: {'status', 'path', 'seconds', ...}}``。"""
    graphs = {}
    graph_dir = os.path.join(out, 'neighbors')
    if not os.path.isdir(graph_dir):
        os.makedirs(graph_dir)
    for beta in beta_values:
        name = 'beta%g' % float(beta)
        started = time.time()
        command = neighbor_graph_command(python, input_path, graph_dir, beta,
                                         block_size, MAX_K)
        entry = {'beta': float(beta), 'name': name, 'command': command}
        try:
            code = subprocess.call(command, timeout=timeout)
        except subprocess.TimeoutExpired:
            code = -1
            entry['reason'] = 'timeout after %ss' % timeout
        entry['exit_code'] = code
        entry['seconds'] = time.time() - started
        entry['status'] = 'success' if code == 0 else 'failed'
        outputs = {'graph': os.path.join(graph_dir, '%s.npz' % name),
                   'mapping': os.path.join(graph_dir, '%s.mapping.json' % name),
                   'summary': os.path.join(graph_dir, '%s.summary.json' % name)}
        for key, path in sorted(outputs.items()):
            entry[key] = path if os.path.isfile(path) else None
        if entry['status'] == 'success' and not entry['graph']:
            entry['status'] = 'failed'
            entry['reason'] = 'exit 0 but no graph written'
        graphs[float(beta)] = entry
        log('neighbor graph %s: %s (%.1fs)' % (name, entry['status'],
                                               entry['seconds']), log_path)
    return graphs


def available_ks(graph_entry):
    """该 β 图上实际可用的 k 列表：``summary.json`` 里的 max_k 之内取请求的 k。"""
    if not graph_entry or graph_entry.get('status') != 'success':
        return []
    summary_path = graph_entry.get('summary')
    if not summary_path or not os.path.isfile(summary_path):
        return []
    with open(summary_path, encoding='utf-8') as handle:
        payload = json.load(handle)
    max_k = int(payload.get('max_k') or MAX_K)
    return [k for k in NEIGHBOR_K if k <= max_k]


# ---------------------------------------------------------------------------
# 9 次评分（每个 (k, beta) 一个子进程，串行）
# ---------------------------------------------------------------------------

def run_itemcf_configs(python, input_path, out, graphs, block_size, top_n, seed,
                       timeout=CHILD_TIMEOUT, log_path=None):
    """对每张可用的 β 图跑它的各个 k 前缀；返回结果行 + 逐用户/推荐明细。"""
    rows, per_user_rows, rec_rows = [], [], []
    for beta in sorted(graphs):
        entry = graphs[beta]
        for k in available_ks(entry):
            name = R.config_name(k, beta)
            started = time.time()
            command = [python, os.path.join(MODULE_DIR, 'score_from_graph.py'),
                       '--input', input_path, '--out', out, '--graph', entry['graph'],
                       '--mapping', entry['mapping'], '--name', name,
                       '--neighbor-k', str(int(k)), '--beta', repr(float(beta)),
                       '--top-n', str(int(top_n)), '--seed', str(int(seed))]
            record = {'method': 'ItemCF', 'config': name, 'k': int(k),
                      'beta': float(beta), 'name': name}
            try:
                code = subprocess.call(command, timeout=timeout)
                reason = None if code == 0 else 'child exit code %s' % code
            except subprocess.TimeoutExpired:
                code, reason = -1, 'timeout after %ss' % timeout
            seconds = time.time() - started
            folder = os.path.join(out, 'configs', name)
            summary_path = os.path.join(folder, 'summary.json')
            status = 'success' if code == 0 else 'failed'
            payload = None
            if status == 'success':
                if os.path.isfile(summary_path):
                    with open(summary_path, encoding='utf-8') as handle:
                        payload = json.load(handle)
                else:
                    status = 'failed'
                    reason = 'exit 0 but no summary.json written'
            record.update({'status': status, 'reason': reason,
                           'cost_seconds': seconds, 'exit_code': code,
                           'folder': folder})
            if payload is not None:
                model = (payload.get('models') or {}).get(name, {})
                for column in ('ndcg@20', 'recall@20', 'ndcg@10', 'recall@10',
                               'full_list_rate', 'fill_rate', 'catalog_coverage',
                               'candidate_coverage', 'support_median',
                               'recommended_popularity_mean', 'low_support_rate',
                               'users_scored', 'returned_count'):
                    record[column] = model.get(column)
                record['rmse'] = None
                record['out_of_range_rate'] = None
                record['notes'] = 'ItemCF k=%d beta=%g（邻居图前缀）' % (k, beta)
                for row in payload.get('per_user', []):
                    item = dict(row)
                    item['model'] = name
                    per_user_rows.append(item)
                for row in payload.get('recommendations', []):
                    item = dict(row)
                    item['model'] = name
                    rec_rows.append(item)
            rows.append(record)
            log('itemcf %s: %s (%.1fs)%s' % (
                name, status, seconds, '' if not reason else ' -- ' + str(reason)),
                log_path)
    return rows, per_user_rows, rec_rows


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(input_path, out, reuse_grid=None, python=None, block_size=128,
        top_n=20, seed=42, test_frac=0.2, val_frac=0.2, timeout=CHILD_TIMEOUT,
        skip_graphs=False):
    python = python or sys.executable
    if not os.path.isdir(out):
        os.makedirs(out)
    log_path = os.path.join(out, 'run.log')
    # ``run_status.json`` 按既有契约是**逐配置条目列表**（verify_results 与报告层都
    # 按 list 解析），所以驱动进程自身的进度状态另写 ``driver_state.json``，
    # 不要挤掉那份列表——否则独立核验会直接 TypeError。
    state_path = os.path.join(out, 'driver_state.json')
    entries_path = os.path.join(out, 'run_status.json')
    started_wall = time.time()

    def write_status(state, extra=None):
        payload = {'status': state, 'started': started_wall,
                   'updated': time.time(), 'pid': os.getpid(),
                   'input': input_path, 'out': out}
        if extra:
            payload.update(extra)
        R.write_summary_atomic(state_path, payload)

    write_status('running', {'stage': 'load_input'})
    log('loading input %s' % input_path, log_path)
    rows = R.load_rows_csv(input_path)
    R.validate_rows(rows)
    input_md5 = _hash_file(input_path)
    log('rows=%d md5=%s' % (len(rows), input_md5), log_path)

    write_status('running', {'stage': 'split'})
    train, val, test = temporal_split_three_way(rows, test_frac, val_frac)
    ctx = build_eval_context(train, val)
    total_rows = len(train) + len(val) + len(test)
    expected = R.expected_protocol(ctx, top_n=top_n, total_rows=total_rows)
    log('split train=%d val=%d test=%d I_train=%d eval_users=%d'
        % (len(train), len(val), len(test), len(ctx.item_ids), len(ctx.eval_users)),
        log_path)

    code_paths = R.itemcf_source_paths()
    code_id = hashlib.sha256(
        json.dumps(R.source_hashes(code_paths), sort_keys=True)
        .encode('utf-8')).hexdigest()

    # ---- ItemCF 邻居图 + 9 配置 ----
    write_status('running', {'stage': 'neighbor_graphs'})
    if skip_graphs:
        graphs = {}
        merged_path = os.path.join(out, 'neighbors', 'graphs.json')
        if os.path.isfile(merged_path):
            with open(merged_path, encoding='utf-8') as handle:
                cached = json.load(handle)
            graphs = dict((float(k), v) for k, v in cached.items())
    else:
        graphs = run_neighbor_graphs(python, input_path, out, BETA, block_size,
                                     timeout, log_path)
        with open(os.path.join(out, 'neighbors', 'graphs.json'), 'w',
                  encoding='utf-8') as handle:
            json.dump(dict((('%g' % k), v) for k, v in graphs.items()), handle,
                      indent=2, sort_keys=True)

    write_status('running', {'stage': 'itemcf_configs'})
    itemcf_rows, per_user_rows, rec_rows = run_itemcf_configs(
        python, input_path, out, graphs, block_size, top_n, seed, timeout, log_path)

    # ---- 对照方法（与 ItemCF 用同一 context / 同一 Top-N） ----
    write_status('running', {'stage': 'comparison_methods'})
    comparison_rows = []
    for spec in R.comparison_methods():
        started = time.time()
        try:
            if spec['kind'] == 'reused_als':
                folder = os.path.join(reuse_grid or '', spec.get('reuse_folder', ''))
                result = R.load_reused_als(folder, context=ctx,
                                           run_id=spec.get('run_id'),
                                           top_n=top_n, total_rows=total_rows)
                result['method'] = spec['name']
            else:
                result = R.evaluate_comparison_method(spec, ctx, top_n=top_n)
            record = R.comparison_row(result, spec, code_id=code_id,
                                      cost_seconds=time.time() - started)
            record['k'] = None
            record['beta'] = None
            record['name'] = spec['name']
            comparison_rows.append(record)
            log('comparison %s: %s (%.1fs) ndcg@20=%s' % (
                spec['name'], record['status'], record['cost_seconds'],
                record['ndcg@20']), log_path)
            # 计划书 §11：recommendations.csv 必须覆盖**全部方法**，逐条 Top-N 落盘，
            # 非 ItemCF 方法的解释字段记 N/A（空串），否则基线的指标无法被独立重算。
            # 这里对 reused_als 与本地求值的基线**一视同仁**——早先只有 reused_als
            # 分支落盘，MostPop/LikePop/BiasOnly 的逐条推荐整批丢失（11/14 方法），
            # 正是"基线指标没有独立核验"的根因。
            for row in result.get('per_user') or []:
                item = dict(row)
                item['model'] = spec['name']
                per_user_rows.append(item)
            for user, recs in zip(result.get('users') or [],
                                  result.get('recommendations') or []):
                for rank, movie in enumerate(recs, 1):
                    rec_rows.append({'user_id': user, 'model': spec['name'],
                                     'rank': rank, 'movie_id': movie,
                                     'raw_score': '', 'n_all': '',
                                     'n_like': '', 'evidence_neighbor_count': '',
                                     'top_source_movie_id': '',
                                     'top_source_c': '', 'contrib_sum': '',
                                     'top_source_n_like': ''})
        except RuntimeError as error:
            # 复用协议不符等公共正确性问题：如实记失败并**阻断后续比较**。
            record = {'method': spec['name'], 'config': spec['config'],
                      'name': spec['name'], 'status': 'blocked',
                      'reason': str(error), 'k': None, 'beta': None,
                      'cost_seconds': time.time() - started,
                      'code_id': code_id}
            comparison_rows.append(record)
            log('comparison %s: BLOCKED %s' % (spec['name'], error), log_path)
            for later in R.comparison_methods():
                if later['name'] == spec['name']:
                    break
            break
        except Exception as error:  # noqa: BLE001 - 失败原因必须落盘
            record = {'method': spec['name'], 'config': spec['config'],
                      'name': spec['name'], 'status': 'failed',
                      'reason': '%s: %s' % (type(error).__name__, error),
                      'k': None, 'beta': None,
                      'cost_seconds': time.time() - started,
                      'code_id': code_id}
            comparison_rows.append(record)
            log('comparison %s: FAILED %s' % (spec['name'], error), log_path)

    # ---- 汇总表：对照方法在前，ItemCF 9 配置在后 ----
    all_rows = comparison_rows + itemcf_rows
    R.write_summary_table(os.path.join(out, 'summary_table.csv'), all_rows)
    R._write_csv(os.path.join(out, 'per_user.csv'), per_user_rows, None)
    R._write_csv(os.path.join(out, 'recommendations.csv'), rec_rows, None)

    # run_status.json = 逐配置终态列表（与 run_grid / verify_results / 报告层同一契约）
    entries = []
    for row in all_rows:
        entries.append({
            'name': row.get('name') or row.get('config'),
            'method': row.get('method'),
            'status': row.get('status'),
            'reason': row.get('reason'),
            'exit_code': row.get('exit_code'),
            'cost_seconds': row.get('cost_seconds'),
            'code_id': row.get('code_id'),
            'run_id': row.get('run_id'),
            'neighbor_k': row.get('k'),
            'beta': row.get('beta'),
            'ndcg@20': row.get('ndcg@20'),
            'recall@20': row.get('recall@20'),
        })
    R.write_summary_atomic(entries_path, entries)

    # ---- 选参（计划书 6.4：只从 ItemCF 9 配置里选，规则 NDCG→Recall→小 k→小 β） ----
    candidates = [{'k': r['k'], 'beta': r['beta'],
                   'ndcg@20': r.get('ndcg@20'), 'recall@20': r.get('recall@20'),
                   'status': r['status']}
                  for r in itemcf_rows if r.get('ndcg@20') is not None]
    winner = select_config(candidates) if candidates else None
    selected = {'rule': 'NDCG@20 desc -> Recall@20 desc -> smaller k -> smaller beta '
                        '(tie tolerance 1e-12)',
                'scope': 'ItemCF 9 configs only (plan 6.4)',
                'selected': winner,
                'candidates': candidates,
                'baselines_not_selectable': [r['method'] for r in comparison_rows]}
    R.write_summary_atomic(os.path.join(out, 'selected_config.json'), selected)

    # ---- 固定案例（计划书 6.3）----
    cases = fixed_case_users(ctx.history_len)
    case_payload = {'case_users': dict((k, list(v)) for k, v in cases.items())}
    if winner is not None:
        name = R.config_name(winner['k'], winner['beta'])
        picked = []
        for layer, users in cases.items():
            for user in users:
                recs = [r for r in rec_rows
                        if r.get('model') == name and r.get('user_id') == user]
                recs.sort(key=lambda r: int(r['rank']))
                picked.append({'user_id': user, 'layer': layer, 'model': name,
                               'recommendations': recs})
        case_payload['selected_model'] = name
        case_payload['cases'] = picked
    R.write_summary_atomic(os.path.join(out, 'cases.json'), case_payload)

    # ---- PRECHECK.md（三段切分 + MostPop 硬门槛在本轮的复现） ----
    mostpop = None
    for row in comparison_rows:
        if row['method'] == 'MostPop':
            mostpop = row
    precheck_lines = [
        '# ItemCF 完整对照实验 PRECHECK',
        '',
        '- 输入：`%s`；MD5：`%s`；行数：%d' % (input_path, input_md5, len(rows)),
        '- 解释器：`%s`' % python,
        '- 三段切分：训练 / 验证 / 保留测试 = %d / %d / %d'
        % (len(train), len(val), len(test)),
        '- `|I_train|` = %d；评测用户 = %d；排除（无验证正例）= %d'
        % (len(ctx.item_ids), len(ctx.eval_users), len(ctx.excluded_no_positive)),
        '- code_id：`%s`' % code_id,
        '',
        '| 指标 | 本轮 MostPop | PRECHECK 记录 | 判定 |',
        '|---|---:|---:|---|',
    ]
    if mostpop and mostpop.get('ndcg@20') is not None:
        precheck_lines.append('| NDCG@20 | %.18f | 0.079767365068241017 | %s |'
                              % (mostpop['ndcg@20'],
                                 'PASS' if abs(mostpop['ndcg@20']
                                               - 0.079767365068241017) <= 1e-15
                                 else 'CHECK'))
        precheck_lines.append('| Recall@20 | %.18f | 0.10663327838454675 | %s |'
                              % (mostpop['recall@20'],
                                 'PASS' if abs(mostpop['recall@20']
                                               - 0.10663327838454675) <= 1e-15
                                 else 'CHECK'))
    else:
        precheck_lines.append('| NDCG@20 | N/A | 0.079767365068241017 | MostPop 未产出 |')
        precheck_lines.append('| Recall@20 | N/A | 0.10663327838454675 | MostPop 未产出 |')
    with open(os.path.join(out, 'PRECHECK.md'), 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(precheck_lines) + '\n')

    elapsed = time.time() - started_wall
    statuses = {}
    for row in all_rows:
        statuses[row['status']] = statuses.get(row['status'], 0) + 1
    final = {'status': 'completed', 'stage': 'done', 'elapsed_seconds': elapsed,
             'status_counts': statuses, 'code_id': code_id,
             'selected_config': winner, 'total_rows': len(all_rows)}
    write_status('completed', final)
    log('DONE in %.1fs; statuses=%s; selected=%s'
        % (elapsed, statuses, winner), log_path)
    return final


def main(argv=None):
    parser = argparse.ArgumentParser(description='ItemCF full comparison experiment')
    parser.add_argument('--input', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--reuse-grid', default=None,
                        help='旧 ALS 网格产物根目录（含 rank50_reg0.1 等子目录）')
    parser.add_argument('--python', default=None)
    parser.add_argument('--block-size', type=int, default=128)
    parser.add_argument('--top-n', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    parser.add_argument('--timeout', type=int, default=CHILD_TIMEOUT)
    parser.add_argument('--skip-graphs', action='store_true',
                        help='复用已有 neighbors/graphs.json（中断续跑）')
    args = parser.parse_args(argv)
    result = run(args.input, args.out, reuse_grid=args.reuse_grid,
                 python=args.python, block_size=args.block_size, top_n=args.top_n,
                 seed=args.seed, timeout=args.timeout, skip_graphs=args.skip_graphs)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
