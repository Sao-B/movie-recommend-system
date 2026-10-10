"""Run the fixed 2,000-user grid serially; retain failures and resource samples.

This driver uses only the standard library and runs under VM Python 3.6.
Every child is a separate spark-submit process. Output directories are exclusive.
"""
import argparse
import csv
import hashlib
import json
import os
import shlex
import subprocess
import time


def resource_peaks(path):
    rss, swap = [], []
    if os.path.isfile(path):
        with open(path) as handle:
            for row in csv.DictReader(line for line in handle if not line.startswith('#')):
                rss.append(float(row['java_rss_mb']) + float(row['python_rss_mb']))
                swap.append(float(row['swap_used_mb']))
    return {'peak_user_java_python_rss_mb': max(rss) if rss else None,
            'peak_system_swap_mb': max(swap) if swap else None}


def experiment_commands(code, output, input_path, spark_submit):
    specs = [('mostpop', 'MostPop', 50, .1)]
    specs += [('rank%d_reg%s' % (rank, reg), 'ALS', rank, reg)
              for rank in (10, 30, 50) for reg in (.01, .1, 1.)]
    runs = []
    for name, model, rank, reg in specs:
        command = [spark_submit, '--master', 'local[*]', os.path.join(code, 'run_pilot.py'),
                   '--input', input_path, '--out', os.path.join(output, name),
                   '--rank', str(rank), '--reg', str(reg), '--iters', '10',
                   '--top-n', '20', '--seed', '42', '--num-slices', '16',
                   '--split', 'three-way', '--val-frac', '0.2', '--test-frac', '0.2',
                   '--models', model.lower()]
        runs.append(dict(name=name, model=model, rank=rank, reg=reg, command=command))
    return runs


def write_json(path, value):
    with open(path, 'w') as handle:
        json.dump(value, handle, indent=2, sort_keys=True, allow_nan=False)


def source_hashes(code):
    result = {}
    for filename in sorted(os.listdir(code)):
        if filename.endswith(('.py', '.sh')):
            with open(os.path.join(code, filename), 'rb') as handle:
                result[filename] = hashlib.sha256(handle.read()).hexdigest()
    return result


def execute(output, input_path, spark_submit='spark-submit'):
    os.makedirs(output)  # Refuse to overwrite a previous run, even an incomplete one.
    code = os.path.dirname(os.path.abspath(__file__))
    hashes = source_hashes(code)
    code_id = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    write_json(os.path.join(output, 'source_hashes.json'), hashes)
    runs = experiment_commands(code, output, input_path, spark_submit)
    reference_protocol = None
    blocked_reason = None
    env = os.environ.copy()
    env.update(PYSPARK_PYTHON='python3', PYSPARK_DRIVER_PYTHON='python3',
               OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
    for run in runs:
        run['code_id'] = code_id
        run['status'] = 'pending'
    status_file = os.path.join(output, 'run_status.json')
    write_json(status_file, runs)
    for run in runs:
        if blocked_reason:
            run.update(status='blocked', reason=blocked_reason)
            write_json(status_file, runs)
            continue
        if source_hashes(code) != hashes:
            blocked_reason = 'Source changed during grid; results cannot be mixed.'
            run.update(status='blocked', reason=blocked_reason)
            write_json(status_file, runs)
            continue
        print('START ' + run['name'], flush=True)
        resources = os.path.join(output, run['name'] + '_resources.csv')
        logfile = os.path.join(output, run['name'] + '.log')
        command = ['bash', os.path.join(code, 'vm_monitor.sh'), resources] + run['command']
        run.update(status='running', command_shell=' '.join(shlex.quote(x) for x in command),
                   started_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        write_json(status_file, runs)
        started = time.monotonic()
        with open(logfile, 'w') as log:
            rc = subprocess.call(command, stdout=log, stderr=subprocess.STDOUT, env=env)
        run.update(exit_code=rc, elapsed_seconds=time.monotonic() - started,
                   finished_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
        run.update(resource_peaks(resources))
        if source_hashes(code) != hashes:
            blocked_reason = 'Source changed during child execution; current result is invalid.'
        summary_path = os.path.join(output, run['name'], 'summary.json')
        summary_error = None
        if os.path.isfile(summary_path):
            try:
                with open(summary_path) as handle:
                    result = json.load(handle)
                protocol = result['protocol']
                if not isinstance(protocol, dict):
                    raise ValueError('Invalid protocol object')
                if result['status'] == 'success':
                    metrics = result['models'][run['model']]
                    for key in ('ndcg@20', 'recall@20', 'rmse', 'top_support_median'):
                        metrics[key]
                    metrics['validation_prediction_bounds']['rate']
                    metrics['top_prediction_bounds']['rate']
                run['result'] = result
                if reference_protocol is None:
                    reference_protocol = protocol
                if protocol != reference_protocol:
                    blocked_reason = 'Protocol differs from the MostPop baseline.'
                if result['status'] == 'objective_anomaly':
                    blocked_reason = 'Objective anomaly: investigate common solver before continuing.'
                run['status'] = 'success' if rc == 0 and result['status'] == 'success' and not blocked_reason else 'failed'
            except (OSError, ValueError, KeyError, TypeError) as exc:
                run['status'] = 'failed'
                summary_error = 'Invalid or incomplete child summary: %s' % exc
        else:
            run['status'] = 'failed'
            summary_error = 'Child did not produce summary.json.'
        if run['status'] == 'failed':
            with open(logfile) as handle:
                tail = handle.read()[-5000:]
            run['reason'] = blocked_reason or ((summary_error or 'Child process failed.') + '\n' + tail)
            # Shared protocol / numerical correctness failures invalidate subsequent runs.
            if any(token in tail for token in ('AssertionError', 'non-finite', 'objective_anomaly')):
                blocked_reason = 'Shared correctness failure; inspect ' + logfile
        write_json(status_file, runs)
        print('END %s status=%s seconds=%.1f' % (run['name'], run['status'], run['elapsed_seconds']), flush=True)
    write_table(output, runs)
    return runs


def write_table(output, runs):
    fields = ['model', 'rank', 'reg', 'ndcg@20', 'recall@20', 'rmse', 'validation_out_of_range',
              'top20_out_of_range', 'top_support_median', 'elapsed_seconds',
              'peak_user_java_python_rss_mb', 'peak_system_swap_mb', 'status', 'code_id', 'reason']
    with open(os.path.join(output, 'summary_table.csv'), 'w', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for run in runs:
            row = {key: run.get(key) for key in fields}
            if run['model'] == 'MostPop':
                row['rank'] = row['reg'] = None
            if run['status'] == 'success':
                metrics = run['result']['models'][run['model']]
                for key in ('ndcg@20', 'recall@20', 'rmse', 'top_support_median'):
                    row[key] = metrics[key]
                row['validation_out_of_range'] = metrics['validation_prediction_bounds']['rate']
                row['top20_out_of_range'] = metrics['top_prediction_bounds']['rate']
            writer.writerow(row)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', required=True)
    parser.add_argument('--input', default='hdfs:///movielens/pilot/ratings.csv')
    parser.add_argument('--spark-submit', default='spark-submit')
    args = parser.parse_args()
    execute(args.out, args.input, args.spark_submit)
