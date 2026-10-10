# -*- coding: utf-8 -*-
"""ItemCF 实验报告生成（计划书第 11 节产物清单）。

从一次运行的产物目录生成：

- ``summary_table.csv``：**全部**方法/配置一行不落（含失败/阻塞项，状态与原因保留）；
- ``conclusion.md``：内嵌完整主表 + 简短结论 + 三张图的引用；
- 三张诊断图（matplotlib，可缺省）：每张都配同名源 CSV，图与源数据一一对应。

三张图（计划书 11 节末段，**最多三张**）：

1. ``fig1_ndcg_recall.*``：全部配置 + 基线的 NDCG@20 / Recall@20（含失败项标记）；
2. ``fig2_k_beta_diagnostics.*``：邻域 k 与 β 对**证据上限 / 实际 Recall / 满额率**的影响；
3. ``fig3_case_evidence.*``：固定案例的推荐支持度与解释证据。

设计约束（照抄计划书，不要"优化"掉）：

- 缺项一律写 ``N/A``，**不用 0 冒充缺失**；
- 不隐藏失败项、不隐藏短列表、不只保留获胜配置——图上失败项单独标注；
- 图的横轴/分组必须覆盖**全部**配置，即使某个配置没有数值。
- ``matplotlib`` 不可导入时不静默失败：图相关产物记为 ``N/A`` 并在结论里写明原因，
  主表与源 CSV 仍然产出（计划书"元数据无法取得时标 N/A"的同一条原则）。

Python 3.6 兼容；只用标准库 + （可选）matplotlib。
"""
import argparse
import csv
import json
import os
import sys

# 允许 ``python experiments/itemcf/report.py`` 这种按脚本路径的调用方式
# （包内是 ``experiments.itemcf.*`` 绝对导入，按路径跑时根目录不在 sys.path）。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

#: 报告层的缺失值占位符（计划书 11 节：缺项显示 N/A）。
NA = 'N/A'

#: summary_table.csv 的固定列序（与 run_itemcf._write_run_artifacts 落盘一致，再补报告列）。
BASE_COLUMNS = ['name', 'status', 'reason', 'ndcg@20', 'recall@20', 'rmse',
                'out_of_range_rate', 'code_id']

#: 画图时认为"没有数值"的终态（这些不是异常，是预算内的正常终态）。
NON_SUCCESS_STATES = ('failed', 'blocked', 'pending', 'reused_verified')

FIG1 = 'fig1_ndcg_recall'
FIG2 = 'fig2_k_beta_diagnostics'
FIG3 = 'fig3_case_evidence'


# ---------------------------------------------------------------------------
# 读取与格式化
# ---------------------------------------------------------------------------

def _read_csv(path):
    """读 CSV；文件不存在返回空列表（调用方按缺项处理，不抛错）。"""
    if not os.path.isfile(path):
        return []
    with open(path, newline='', encoding='utf-8') as handle:
        return list(csv.DictReader(handle))


def format_value(value, digits=6):
    """数值格式化：None/空串/非数值 → ``N/A``（绝不用 0 冒充缺失）。"""
    if value is None:
        return NA
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return NA
        try:
            value = float(text)
        except ValueError:
            return text
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number != number or number in (float('inf'), float('-inf')):
        return NA
    if number == int(number) and abs(number) < 1e15:
        return '%d' % int(number)
    return ('%.' + str(digits) + 'f') % number


def as_float(value):
    """可转数值则返回 float，否则 None（用于画图，不做单位换算）。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float('inf'), float('-inf')):
        return None
    return number


def parse_config_name(name):
    """从 ``k20_beta0`` 解析 ``(k, beta)``；解析不出返回 ``(None, None)``。"""
    if not name or not name.startswith('k') or '_beta' not in name:
        return None, None
    head, _, tail = name.partition('_beta')
    try:
        return int(head[1:]), float(tail)
    except ValueError:
        return None, None


# ---------------------------------------------------------------------------
# 表：全部方法/配置，一行不落
# ---------------------------------------------------------------------------

def load_summary_rows(run_dir):
    """读 ``summary_table.csv``；缺文件时回退到 ``run_status.json`` 的终态清单。

    回退很重要：运行中途失败时可能还没有 summary_table.csv，但 run_status.json
    已经记下了每个配置的终态。报告必须能展示"全部配置都有终态"，而不是空表。
    """
    rows = _read_csv(os.path.join(run_dir, 'summary_table.csv'))
    if rows:
        return rows
    status_path = os.path.join(run_dir, 'run_status.json')
    if not os.path.isfile(status_path):
        return []
    with open(status_path, encoding='utf-8') as handle:
        entries = json.load(handle)
    fallback = []
    for entry in entries:
        row = dict((column, None) for column in BASE_COLUMNS)
        row['name'] = entry.get('name')
        row['status'] = entry.get('status')
        row['reason'] = entry.get('reason')
        row['code_id'] = entry.get('code_id')
        fallback.append(row)
    return fallback


def build_main_table(rows, extra_metrics=None):
    """主表：每个方法/配置一行，保留状态与失败原因。

    :param extra_metrics: 可选 ``{name: {列名: 值}}``，用于补充满额率/覆盖率等
      逐配置诊断列（来自各配置目录的 summary.json）。
    :returns: ``(columns, table_rows)``；``table_rows`` 的单元格已是字符串，
      缺项为 ``N/A``。
    """
    extra_metrics = extra_metrics or {}
    dynamic = []
    for name in sorted(extra_metrics):
        for column in extra_metrics[name]:
            if column not in dynamic and column not in BASE_COLUMNS:
                dynamic.append(column)
    columns = list(BASE_COLUMNS) + dynamic
    table_rows = []
    for row in rows:
        name = row.get('name') or ''
        out = {}
        for column in columns:
            if column in ('name', 'status', 'reason', 'code_id'):
                value = row.get(column)
                out[column] = NA if value in (None, '') else str(value)
            else:
                value = row.get(column)
                # ``N/A`` 哨兵必须与 None/空串同等对待：报告层是**幂等重跑**的，
                # 上一次跑会把缺项写成字面量 ``'N/A'`` 落进 summary_table.csv。
                # 只判 None/空串的话，重跑时这个非空字符串会被当成"真值"直接采用，
                # 逐配置 summary.json 里的真数据永远补不进来——主表于是永久
                # 钉死在 N/A 上（图 2 同款哨兵 bug 的表侧版本）。
                if value == NA:
                    value = None
                if (value is None or (isinstance(value, str) and not value.strip())) \
                        and name in extra_metrics:
                    # 不用 ``or`` 回落：0.0 是合法值，``a or b`` 会把真 0 换成别的来源。
                    value = extra_metrics[name].get(column)
                out[column] = format_value(value)
        table_rows.append(out)
    return columns, table_rows


def load_per_config_metrics(run_dir, names):
    """从各配置目录的 summary.json 收集逐配置诊断列（满额率/覆盖率等）。

    只在字段真实存在时收录；缺失的配置不出现在返回 dict 里，主表对该配置显示
    ``N/A``——不替它编造 0。

    两个落盘位置都要找：``run_itemcf.run_grid`` 写 ``<run_dir>/<name>/summary.json``，
    而 ``run_comparison`` 写 ``<run_dir>/configs/<name>/summary.json``。只认前者会让
    完整对照实验的 ItemCF 诊断列（满额率/覆盖率/支持度）整列显示 N/A。
    """
    wanted = ['full_list_rate', 'empty_list_rate', 'fill_rate',
              'candidate_coverage', 'evidence_positive_coverage',
              'evidence_max_recall@20', 'evidence_count_p10',
              'evidence_count_p50', 'evidence_count_p90',
              'evidence_count_zero_users', 'catalog_coverage',
              'recommended_popularity_mean', 'low_support_rate',
              'personalization_jaccard', 'users_scored']
    out = {}
    for name in names:
        payload = None
        for candidate in (os.path.join(run_dir, name, 'summary.json'),
                          os.path.join(run_dir, 'configs', name, 'summary.json')):
            if not os.path.isfile(candidate):
                continue
            try:
                with open(candidate, encoding='utf-8') as handle:
                    payload = json.load(handle)
            except ValueError:
                payload = None
            if payload is not None:
                break
        if payload is None:
            continue
        metrics = {}
        model = (payload.get('models') or {}).get(name, {})
        for column in wanted:
            if column in model:
                metrics[column] = model[column]
            elif column in payload:
                metrics[column] = payload[column]
        if metrics:
            out[name] = metrics
    return out


def markdown_table(columns, rows):
    """把主表渲染成 Markdown 表格（计划书：Markdown 内嵌主表保证可审阅）。"""
    if not rows:
        return '_（无配置终态可展示）_\n'
    lines = ['| ' + ' | '.join(columns) + ' |',
             '|' + '|'.join(['---'] * len(columns)) + '|']
    for row in rows:
        cells = []
        for column in columns:
            cell = str(row.get(column, NA)).replace('|', '\\|')
            cells.append(cell)
        lines.append('| ' + ' | '.join(cells) + ' |')
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# 源数据 CSV（每张图都配一份）
# ---------------------------------------------------------------------------

def _write_rows(path, columns, rows):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return path


def figure1_source(run_dir, rows):
    """图 1 源数据：全部配置 + 基线的 NDCG@20 / Recall@20。"""
    columns = ['name', 'kind', 'k', 'beta', 'status', 'ndcg@20', 'recall@20', 'reason']
    out_rows = []
    for row in rows:
        name = row.get('name') or ''
        k, beta = parse_config_name(name)
        out_rows.append({
            'name': name,
            'kind': 'itemcf' if k is not None else 'baseline',
            'k': k if k is not None else NA,
            'beta': beta if beta is not None else NA,
            'status': row.get('status') or NA,
            'ndcg@20': format_value(row.get('ndcg@20')),
            'recall@20': format_value(row.get('recall@20')),
            'reason': row.get('reason') or NA,
        })
    return _write_rows(os.path.join(run_dir, FIG1 + '.csv'), columns, out_rows)


def figure2_source(run_dir, rows):
    """图 2 源数据：k 与 β 对证据上限 / 实际 Recall / 满额率的影响。"""
    columns = ['name', 'k', 'beta', 'status', 'evidence_max_recall@20',
               'recall@20', 'full_list_rate', 'empty_list_rate', 'evidence_count_p50']
    out_rows = []
    for row in rows:
        name = row.get('name') or ''
        k, beta = parse_config_name(name)
        metrics = load_per_config_metrics(run_dir, [name]).get(name, {})

        def pick(column):
            value = row.get(column)
            # 主表里的缺项已经被格式化成字符串 ``'N/A'``（不是 None/空串），
            # 只判 None/空串会把它当成"真实取值"直接返回，于是逐配置 summary.json
            # 里的真值永远取不到——图的源数据整列 N/A。这里必须把哨兵一并当缺项。
            if value is None or value == NA:
                value = None
            if value is None or (isinstance(value, str) and not value.strip()):
                value = metrics.get(column)
            return value

        out_rows.append({
            'name': name,
            'k': k if k is not None else NA,
            'beta': beta if beta is not None else NA,
            'status': row.get('status') or NA,
            'evidence_max_recall@20': format_value(pick('evidence_max_recall@20')),
            'recall@20': format_value(pick('recall@20')),
            'full_list_rate': format_value(pick('full_list_rate')),
            'empty_list_rate': format_value(pick('empty_list_rate')),
            'evidence_count_p50': format_value(pick('evidence_count_p50')),
        })
    # 同一 β 下按 k 排序，便于看图上的单调趋势
    out_rows.sort(key=lambda r: (str(r['beta']), str(r['k']), str(r['name'])))
    return _write_rows(os.path.join(run_dir, FIG2 + '.csv'), columns, out_rows)


def load_cases(run_dir):
    """读 ``cases.json``；缺失/损坏返回 None（调用方写 N/A）。"""
    path = os.path.join(run_dir, 'cases.json')
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except ValueError:
        return None


def figure3_source(run_dir, model_name=None):
    """图 3 源数据：固定案例的推荐支持度与解释证据。

    ``cases.json`` 的结构是 ``{user_id: {...}}`` 或 ``{'cases': [...]}``，两种都接受；
    取不到案例时返回空表（报告写 N/A），不伪造数据。
    """
    columns = ['user_id', 'rank', 'movie_id', 'raw_score', 'n_all', 'n_like',
               'evidence_neighbor_count', 'top_source_movie_id', 'top_source_c']
    cases = load_cases(run_dir)
    out_rows = []
    if cases:
        items = cases.get('cases') if isinstance(cases, dict) else cases
        if isinstance(items, dict):
            items = [dict(v, user_id=v.get('user_id', key)) for key, v in items.items()]
        for case in items or []:
            user = case.get('user_id', case.get('user'))
            recs = case.get('recommendations') or case.get('recs') or []
            for rank, rec in enumerate(recs, 1):
                out_rows.append({
                    'user_id': user if user is not None else NA,
                    'rank': rank,
                    'movie_id': rec.get('movie_id', NA),
                    'raw_score': format_value(rec.get('raw_score', rec.get('score'))),
                    'n_all': rec.get('n_all', NA),
                    'n_like': rec.get('n_like', NA),
                    'evidence_neighbor_count': rec.get('evidence_neighbor_count', NA),
                    'top_source_movie_id': rec.get('top_source_movie_id', NA),
                    'top_source_c': rec.get('top_source_c', NA),
                })
    return _write_rows(os.path.join(run_dir, FIG3 + '.csv'), columns, out_rows)


# ---------------------------------------------------------------------------
# 画图（可选依赖）
# ---------------------------------------------------------------------------

def _setup_matplotlib():
    """返回 matplotlib.pyplot 或 None（不可用时报告记 N/A，不静默跳过）。"""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        return plt
    except Exception:  # noqa: BLE001 - 缺失/后端不可用都按 N/A 处理
        return None


def _bar_labels(axis, bars, values, fontsize=6):
    """在柱顶写数值；数值之间错开高度，避免相邻标签互相压住。"""
    for index, (bar, value) in enumerate(zip(bars, values)):
        if value is None:
            continue
        axis.text(bar.get_x() + bar.get_width() / 2.0, bar.get_height(),
                  '%.4f' % value, ha='center',
                  va='bottom' if index % 2 == 0 else 'top',
                  fontsize=fontsize)


def _legend_if_any(axis, **kwargs):
    """有带标签的 artist 才调 legend()，否则 matplotlib 会打 UserWarning。"""
    handles, labels = axis.get_legend_handles_labels()
    if handles and any(not label.startswith('_') for label in labels):
        axis.legend(**kwargs)


def draw_figure1(plt, run_dir, rows):
    """图 1：全部配置 + 基线的 NDCG/Recall 分组柱状图（成功项带数值标注）。"""
    names = [row.get('name') or '' for row in rows]
    ndcgs = [as_float(row.get('ndcg@20')) for row in rows]
    recalls = [as_float(row.get('recall@20')) for row in rows]
    if not names:
        return None
    fig, axis = plt.subplots(figsize=(max(8.0, 0.9 * len(names)), 4.8))
    positions = list(range(len(names)))
    width = 0.38
    # 缺数值的位置高度取 0（图上不画柱子），但在状态标注里明确指出是失败/阻塞。
    ndcg_vals = [v if v is not None else 0.0 for v in ndcgs]
    recall_vals = [v if v is not None else 0.0 for v in recalls]
    bars1 = axis.bar([p - width / 2.0 for p in positions], ndcg_vals, width,
                     label='NDCG@20')
    bars2 = axis.bar([p + width / 2.0 for p in positions], recall_vals, width,
                     label='Recall@20')
    _bar_labels(axis, bars1, ndcgs)
    _bar_labels(axis, bars2, recalls)
    for position, row, value in zip(positions, rows, ndcgs):
        status = (row.get('status') or '').strip()
        if value is None:
            axis.text(position, 0.0, status or NA, ha='center', va='bottom',
                      fontsize=7, rotation=90, color='crimson')
    axis.set_xticks(positions)
    axis.set_xticklabels(names, rotation=45, ha='right', fontsize=8)
    axis.set_ylabel('metric value')
    axis.set_title('ItemCF grid vs baselines: NDCG@20 / Recall@20 (all configs)')
    _legend_if_any(axis, fontsize=8)
    axis.grid(axis='y', alpha=0.3)
    # 排序指标非负：不要让 matplotlib 自动把 y 轴拉到 0 以下（否则 0 值柱
    # 看起来像"负分"，也会把 0 与 0.08 的差别压扁）。
    observed = [v for v in ndcg_vals + recall_vals if v is not None]
    top = max(observed) if observed else 1.0
    axis.set_ylim(0.0, top * 1.25 if top > 0 else 1.0)
    fig.tight_layout()
    path = os.path.join(run_dir, FIG1 + '.png')
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def draw_figure2(plt, run_dir, rows):
    """图 2：k 与 β 对证据上限 / 实际 Recall / 满额率 / 证据强度的影响（每 β 一条线）。

    **必须用图源 CSV 而不是主表行**：主表 ``summary_table.csv`` 把缺项写成字符串
    ``'N/A'`` 哨兵（不是 None），``as_float('N/A')`` 返回 None，于是证据上限、满额率
    这些"主表里是 N/A、真值在逐配置 summary.json"的列会被静默丢成空面板——
    图 2 曾经就是这样画出一片空白。``figure2_source`` 已经做过哨兵回落，所以这里
    以它落盘的 CSV 为唯一数据来源，保证**图上看到的就是源 CSV 里的数**。
    """
    source = os.path.join(run_dir, FIG2 + '.csv')
    rows = _read_csv(source) or rows
    points = []
    for row in rows:
        name = row.get('name') or ''
        k, beta = parse_config_name(name)
        if k is None:
            continue
        points.append((k, beta, name,
                       as_float(row.get('evidence_max_recall@20')),
                       as_float(row.get('recall@20')),
                       as_float(row.get('full_list_rate')),
                       as_float(row.get('evidence_count_p50'))))
    betas = sorted(set(p[1] for p in points))
    if not points or not betas:
        return None
    ks = sorted(set(p[0] for p in points))
    # 四个面板：证据上限 / 实际 Recall / 满额率 / 证据强度（|E_u| 中位数）。
    # 计划书 §11 要求图 2 展示"证据上限 / 实际 Recall / 满额率"；证据候选数
    # 也是 §6.2 的诊断项，单列一个面板才能回答 §1 问题 4（候选不够还是排序不好）。
    panels = [('evidence_max_recall@20', 'evidence cap (upper bound)'),
              ('recall@20', 'actual Recall@20'),
              ('full_list_rate', 'full-list rate'),
              ('evidence_count_p50', 'evidence count p50')]
    fig, axes = plt.subplots(1, len(panels), figsize=(4.4 * len(panels), 4.2))
    for axis, (field, title) in zip(axes, panels):
        for beta in betas:
            subset = dict((p[0], p) for p in points if p[1] == beta)
            xs, ys = [], []
            for k in ks:
                if k not in subset:
                    continue
                value = {'evidence_max_recall@20': subset[k][3],
                         'recall@20': subset[k][4],
                         'full_list_rate': subset[k][5],
                         'evidence_count_p50': subset[k][6]}[field]
                if value is None:
                    continue  # 缺项不连线，缺口留给源 CSV 说明
                xs.append(k)
                ys.append(value)
            if xs:
                axis.plot(xs, ys, marker='o', label='beta=%g' % beta)
        axis.set_xlabel('neighbor k')
        axis.set_title(title, fontsize=10)
        axis.set_xticks(ks)
        axis.grid(alpha=0.3)
        # 排序指标与比率都非负，统一从 0 起画，避免视觉上夸大差异。
        axis.set_ylim(bottom=0.0)
        _legend_if_any(axis, fontsize=8)
    fig.suptitle('k / beta effect on evidence cap, Recall, full-list rate '
                 'and evidence strength', fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.94))
    path = os.path.join(run_dir, FIG2 + '.png')
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


def draw_figure3(plt, run_dir, cases_path=None):
    """图 3：固定案例的推荐支持度（n_all）与解释证据（证据邻居数）。"""
    source = cases_path or os.path.join(run_dir, FIG3 + '.csv')
    rows = _read_csv(source)
    if not rows:
        return None
    # 只取每个用户的前若干条，避免图上过密；排序即推荐名次。
    grouped = {}
    for row in rows:
        grouped.setdefault(str(row.get('user_id')), []).append(row)
    users = sorted(grouped)
    fig, axes = plt.subplots(1, 2, figsize=(11.0, 4.6))
    for axis, field, title in ((axes[0], 'n_all', 'recommended popularity n_all'),
                               (axes[1], 'evidence_neighbor_count',
                                'evidence neighbors per recommendation')):
        width = 0.8 / max(1, len(users))
        for offset, user in enumerate(users):
            subset = grouped[user]
            xs = [int(r['rank']) for r in subset if r.get('rank')]
            # 缺项按 0 高度画（图上留白），不连线也不编造数值；源 CSV 保留 N/A。
            ys = [as_float(r.get(field)) if as_float(r.get(field)) is not None else 0.0
                  for r in subset]
            axis.bar([x + offset * width for x in xs], ys, width,
                     label='user %s' % user)
        axis.set_xlabel('rank in recommendation list')
        axis.set_title(title, fontsize=10)
        axis.grid(axis='y', alpha=0.3)
        _legend_if_any(axis, fontsize=7)
    fig.suptitle('Fixed cases: recommendation support and explanation evidence',
                 fontsize=11)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    path = os.path.join(run_dir, FIG3 + '.png')
    fig.savefig(path, dpi=130)
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# 结论
# ---------------------------------------------------------------------------

def load_diagnostics(run_dir):
    """读本轮三个诊断产物（缺一个就记 None，报告显式写 N/A，不编数字）。"""
    out = {'bootstrap': None, 'repeat': None, 'shuffle': None}
    for key, name in (('bootstrap', 'bootstrap.json'), ('repeat', 'repeat.json'),
                      ('shuffle', 'shuffle.json')):
        path = os.path.join(run_dir, name)
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as handle:
                    out[key] = json.load(handle)
            except ValueError:
                out[key] = None
    return out


#: 计划书 §12 的效果判断分支。
def effect_verdict(best_row, baselines):
    """按计划书 §12 给出效果判断。

    :param best_row: 选定的 ItemCF 配置行。
    :param baselines: ``{方法名: 行}``。
    :returns: ``(情况编号, 结论文本, 是否宣告上线)``。

    情况 1：NDCG、Recall **双不超**人气基线 -> 不值得上线，报告差距。
    情况 2：NDCG、Recall **双超**人气基线 -> **值得进入独立数据确认**，
            **不宣布上线**（同一验证集选参与评估，尚未独立确认）。
    """
    mostpop = baselines.get('MostPop')
    if best_row is None:
        return (None, '没有任何成功配置可供判断（计划书 §12：只能给工程结论，'
                      '不能用失败指标否定算法）。', False)
    if mostpop is None or as_float(mostpop.get('ndcg@20')) is None:
        return (None, '人气基线未产出，无法做 §12 的效果判断。', False)
    best_ndcg = as_float(best_row.get('ndcg@20'))
    best_recall = as_float(best_row.get('recall@20'))
    base_ndcg = as_float(mostpop.get('ndcg@20'))
    base_recall = as_float(mostpop.get('recall@20'))
    if best_ndcg is None or best_recall is None:
        return (None, '选定配置缺少 NDCG/Recall，无法做 §12 的效果判断。', False)
    beats_ndcg = best_ndcg > base_ndcg
    beats_recall = best_recall > base_recall
    if beats_ndcg and beats_recall:
        return (2, 'NDCG@20 与 Recall@20 **双超**人气基线 MostPop，'
                   '属于计划书 §12 的**情况 2**：结论是**值得进入独立数据确认**'
                   '（用未参与选参的独立数据/盲测复核），**不宣布上线**。', False)
    if not beats_ndcg and not beats_recall:
        return (1, 'NDCG@20 与 Recall@20 **均未超过**人气基线 MostPop，'
                   '属于计划书 §12 的**情况 1**：不值得上线，如实报告差距。', False)
    return (3, 'NDCG@20 与 Recall@20 只有一项超过人气基线（%s 超、%s 不超），'
               '属于混合情形：不能按"双超"结论收尾，需如实说明取舍。'
               % ('NDCG' if beats_ndcg else 'Recall',
                  'Recall' if beats_ndcg else 'NDCG'), False)


def build_conclusion(run_dir, rows, figures, source_csvs, notes=None, extra=None):
    """生成简短结论文本：状态分布、最佳配置、失败项、图的可用性、§12 效果判断。

    :param extra: 可选 ``{name: {列名: 值}}``（逐配置 summary.json 的诊断列，通常来自
      :func:`load_per_config_metrics`）。证据上限/证据候选数只在逐配置 ``summary.json``
      里，主表行仅在被补列后才带这些字段，所以这里必须能回落进去取。
      不传时按 ``rows`` 的 ``name`` 就地重新收集一次，保证单独调用也能出数据。
    """
    if extra is None:
        extra = load_per_config_metrics(
            run_dir, [r.get('name') for r in rows if r.get('name')])
    total = len(rows)
    by_status = {}
    for row in rows:
        status = (row.get('status') or NA)
        by_status[status] = by_status.get(status, 0) + 1
    successful = [r for r in rows if (r.get('status') or '') == 'success']
    ranked = sorted([r for r in successful if as_float(r.get('ndcg@20')) is not None],
                    key=lambda r: as_float(r.get('ndcg@20')), reverse=True)
    lines = []
    lines.append('## 结论')
    lines.append('')
    lines.append('- 配置/方法总数：%d；终态分布：%s。'
                 % (total, ', '.join('%s=%d' % (k, v) for k, v in sorted(by_status.items()))
                    or NA))
    if ranked:
        best = ranked[0]
        lines.append('- 按 NDCG@20 排序的最佳成功配置：**%s**（NDCG@20=%s，Recall@20=%s）。'
                     % (best.get('name'), format_value(best.get('ndcg@20')),
                        format_value(best.get('recall@20'))))
        lines.append('- 该结论是在同一验证集上选出的探索性结果，**不构成独立显著性验证**'
                     '（计划书 6.4）；绝对差、配对区间与短列表代价见 paired_differences.csv / '
                     'bootstrap.json。')
    else:
        lines.append('- 没有任何配置产出可比较的 NDCG@20：本轮只能给出工程结论，'
                     '不能用失败指标否定算法（计划书 12 节）。')
    failures = [r for r in rows if (r.get('status') or '') in ('failed', 'blocked')]
    lines.append('- 失败/阻塞项 %d 个：%s。'
                 % (len(failures),
                    ', '.join('%s(%s)' % (r.get('name'), r.get('status'))
                              for r in failures) or '无'))
    lines.append('- 三张图各自配有同名源 CSV；图上失败项与缺失值不隐藏，缺项显示为 %s。'
                 % NA)
    for index, path in enumerate(figures, 1):
        if path:
            lines.append('  - 图 %d：`%s`（源：`%s`）'
                         % (index, os.path.basename(path),
                            os.path.basename(source_csvs[index - 1])))
        else:
            lines.append('  - 图 %d：%s（matplotlib 不可用或源数据为空；源 CSV：`%s`）'
                         % (index, NA, os.path.basename(source_csvs[index - 1])))
    lines.append('- **分数含义**：ItemCF 的排序分数不是预测评分，其 RMSE / 越界率记 %s。'
                 % NA)

    # ---- 计划书 §6.2 / §12：证据上限 vs 实际 Recall（回答 §1 问题 4）----
    lines.append('')
    lines.append('## 证据上限与实际 Recall（回答计划书 §1 问题 4）')
    lines.append('')
    lines.append('- **问题 4**："推荐失败是找不到相关候选，还是候选排序不好？"'
                 '判据是**证据集 Recall@20 上限**'
                 '（`mean_u min(20,|R_u∩E_u|)/|R_u|`，计划书 §6.2）与实际 Recall@20 的差距。')
    evidence_rows = []
    for row in rows:
        name = row.get('name') or ''
        k, beta = parse_config_name(name)
        if k is None or (row.get('status') or '') != 'success':
            continue
        metrics = extra.get(name, {})
        cap = as_float(row.get('evidence_max_recall@20'))
        if cap is None:
            cap = as_float(metrics.get('evidence_max_recall@20'))
        actual = as_float(row.get('recall@20'))
        if actual is None:
            actual = as_float(metrics.get('recall@20'))
        median = as_float(row.get('evidence_count_p50'))
        if median is None:
            median = as_float(metrics.get('evidence_count_p50'))
        evidence_rows.append((name, k, beta, cap, actual, median))
    if evidence_rows:
        lines.append('')
        lines.append('| 配置 | k | beta | 证据上限 Recall@20 | 实际 Recall@20 | '
                     '差距（上限−实际） | 达成率（实际/上限） | |E_u| 中位数 |')
        lines.append('|---|---:|---:|---:|---:|---:|---:|---:|')
        for name, k, beta, cap, actual, median in evidence_rows:
            gap = (cap - actual) if (cap is not None and actual is not None) else None
            ratio = (actual / cap) if (cap not in (None, 0.0) and actual is not None) else None
            lines.append('| %s | %d | %g | %s | %s | %s | %s | %s |'
                         % (name, k, beta, format_value(cap), format_value(actual),
                            format_value(gap), format_value(ratio),
                            format_value(median)))
        caps = [c for _n, _k, _b, c, _a, _m in evidence_rows if c is not None]
        acts = [a for _n, _k, _b, _c, a, _m in evidence_rows if a is not None]
        if caps and acts:
            best_cap = max(caps)
            lines.append('')
            lines.append('- 证据上限（全部配置最大值）**%s**；实际 Recall@20 最大 **%s**。'
                         % (format_value(best_cap), format_value(max(acts))))
            # 用最大上限配置的达成率判断：接近 0 -> 候选不够；接近 1 且仍低 -> 排序受限
            paired = [(c, a) for _n, _k, _b, c, a, _m in evidence_rows
                      if c is not None and a is not None]
            if paired:
                cap_top, act_top = max(paired, key=lambda t: t[0])
                ratio_top = (act_top / cap_top) if cap_top > 0 else None
                if ratio_top is not None and ratio_top < 0.5:
                    lines.append('- 在证据上限最高的配置上，实际 Recall 只达到上限的 **%s**'
                                 '（< 50%%）：**瓶颈主要在排序/打分**——邻域已经能找到'
                                 '足够多的相关候选，但 Top-20 没能把它们排上来'
                                 '（计划书 §12 情况 5）。'
                                 % format_value(ratio_top))
                elif ratio_top is not None:
                    lines.append('- 在证据上限最高的配置上，实际 Recall 达到上限的 **%s**'
                                 '：**瓶颈主要在候选覆盖**——即使排序器完美，'
                                 '`|R_u∩E_u|` 就把 Recall 封在 %s'
                                 '（计划书 §12 情况 4）。'
                                 % (format_value(ratio_top), format_value(cap_top)))
                lines.append('- 上限与实际 Recall 都是**共同评测用户**上的先逐人算再求均值；'
                             '两者都不剔除无证据用户（计划书 §6.1）。')
    else:
        lines.append('- 证据上限：%s（本轮没有成功配置带出 evidence 诊断）。' % NA)

    # ---- 计划书 §12 效果判断 ----
    # 基线行按 ``name`` 认，不能只看 ``method``：``summary_table.csv`` 的列是
    # ``name/status/...``（没有 ``method`` 列），只查 ``method`` 会让基线"看不见"，
    # §12 判断直接退化成"人气基线未产出"。
    baselines = {}
    for row in rows:
        for key in (row.get('method'), row.get('name'), row.get('config')):
            if key in ('MostPop', 'LikePop'):
                baselines[key] = row
    best_row = ranked[0] if ranked else None
    case, verdict, launch = effect_verdict(best_row, baselines)
    lines.append('')
    lines.append('## 效果判断（计划书 §12）')
    lines.append('')
    if case == 2 and best_row is not None:
        mostpop = baselines.get('MostPop')
        lines.append('- 选定配置 **%s**：NDCG@20=%s、Recall@20=%s；'
                     'MostPop：NDCG@20=%s、Recall@20=%s。'
                     % (best_row.get('name'), format_value(best_row.get('ndcg@20')),
                        format_value(best_row.get('recall@20')),
                        format_value(mostpop.get('ndcg@20')),
                        format_value(mostpop.get('recall@20'))))
        lines.append('- **判断：命中计划书 §12 情况 2（NDCG@20 与 Recall@20 双超人气基线）。**')
        lines.append('- **结论：值得进入独立数据确认，不宣布上线。**'
                     '本次比较在**同一验证集**上完成选参与评估，且为单次运行，'
                     '尚不能替代独立数据上的确认。')
    lines.append('- %s' % verdict)

    # ---- 诊断产物（bootstrap / repeat / shuffle） ----
    diag = load_diagnostics(run_dir)
    lines.append('')
    lines.append('## 诊断与不确定性')
    lines.append('')
    boot = diag['bootstrap']
    if boot:
        lines.append('- **配对 bootstrap**（seed=%s，%s 次，同用户索引配对，'
                     'percentile 95%% 区间）：区间基于**同一验证集选参之后**的探索性'
                     '比较，区间不排除 0 **不构成**已做独立显著性验证。'
                     % (boot.get('seed'), boot.get('repeats')))
        lines.append('')
        lines.append('| 比较 | NDCG 均值差 | NDCG 95% CI | 是否排除 0 | '
                     'Recall 均值差 | Recall 95% CI | 是否排除 0 |')
        lines.append('|---|---:|---|---|---|---|---|')
        for key in sorted(boot.get('comparisons') or {}):
            item = boot['comparisons'][key]
            nd, rc = item.get('ndcg@20') or {}, item.get('recall@20') or {}
            lines.append('| %s | %s | [%s, %s] | %s | %s | [%s, %s] | %s |'
                         % (key, format_value(nd.get('mean_difference')),
                            format_value(nd.get('ci95_low')),
                            format_value(nd.get('ci95_high')),
                            '是' if item.get('excludes_zero_ndcg') else '否',
                            format_value(rc.get('mean_difference')),
                            format_value(rc.get('ci95_low')),
                            format_value(rc.get('ci95_high')),
                            '是' if item.get('excludes_zero_recall') else '否'))
    else:
        lines.append('- **配对 bootstrap**：%s（未产出 bootstrap.json）。' % NA)

    repeat = diag['repeat']
    if repeat:
        lines.append('- **repeat（另启进程、不读邻居图缓存、重新构图+评分）**：'
                     '邻居 ID/排序一致=%s；推荐 ID/排序一致=%s；用户列表一致=%s；'
                     '浮点绝对差=%s；指标绝对差=%s（容差 %s）→ **一致=%s**。'
                     % (repeat.get('neighbor_order_equal'),
                        repeat.get('recommendation_order_equal'),
                        repeat.get('users_equal'),
                        format_value(repeat.get('max_abs_float_diff')),
                        format_value(repeat.get('max_abs_metric_diff')),
                        repeat.get('tolerance'), repeat.get('consistent')))
    else:
        lines.append('- **repeat**：%s（未产出 repeat.json）。' % NA)

    shuffle = diag['shuffle']
    if shuffle:
        nd = shuffle.get('ndcg@20') or {}
        rc = shuffle.get('recall@20') or {}
        lines.append('- **history-shuffle（seed=%s，错配历史）**：真实 NDCG@20=%s '
                     'vs 错配 %s；真实 Recall@20=%s vs 错配 %s；'
                     '配对差 NDCG=%s（CI [%s, %s]）、Recall=%s（CI [%s, %s]）。'
                     % (shuffle.get('seed'), format_value(shuffle.get('real_ndcg@20')),
                        format_value(shuffle.get('shuffled_ndcg@20')),
                        format_value(shuffle.get('real_recall@20')),
                        format_value(shuffle.get('shuffled_recall@20')),
                        format_value(nd.get('mean_difference')),
                        format_value(nd.get('ci95_low')),
                        format_value(nd.get('ci95_high')),
                        format_value(rc.get('mean_difference')),
                        format_value(rc.get('ci95_low')),
                        format_value(rc.get('ci95_high'))))
        fell = (as_float(nd.get('mean_difference')) or 0.0) > 0
        lines.append('  - 错配历史后指标**%s**（真实 > 错配：%s）；'
                     'seed 固定为 %s，**不因结果重选 seed**。'
                     % ('下降' if fell else '未下降（如实报告）', fell,
                        shuffle.get('seed')))
    else:
        lines.append('- **history-shuffle**：%s（未产出 shuffle.json）。' % NA)

    if notes:
        lines.append('')
        for note in notes:
            lines.append('- %s' % note)
    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_report(run_dir, make_figures=True):
    """生成全部报告产物；返回摘要 dict（同时供测试断言）。

    产物：``summary_table.csv``、``conclusion.md``、三张图 + 三份图源 CSV。
    ``matplotlib`` 缺失时图记 ``None``，其余产物照常产出。
    """
    run_dir = os.path.abspath(run_dir)
    rows = load_summary_rows(run_dir)
    names = [r.get('name') for r in rows if r.get('name')]
    extra = load_per_config_metrics(run_dir, names)

    # 主表补充诊断列（来自逐配置 summary.json）
    columns, table_rows = build_main_table(rows, extra)
    # 图 2/图 3 需要的字段一并带进主表，便于 audit
    for row, source in zip(table_rows, rows):
        name = source.get('name') or ''
        metrics = extra.get(name, {})
        for column in ('full_list_rate', 'empty_list_rate', 'fill_rate',
                       'candidate_coverage', 'evidence_positive_coverage',
                       'evidence_max_recall@20', 'evidence_count_p50',
                       'evidence_count_zero_users', 'catalog_coverage',
                       'recommended_popularity_mean', 'low_support_rate',
                       'personalization_jaccard'):
            if column not in columns:
                columns.append(column)
            if column not in row:
                row[column] = format_value(metrics.get(column))
    _write_rows(os.path.join(run_dir, 'summary_table.csv'), columns, table_rows)

    source1 = figure1_source(run_dir, rows)
    source2 = figure2_source(run_dir, rows)
    source3 = figure3_source(run_dir)

    figures = [None, None, None]
    if make_figures:
        plt = _setup_matplotlib()
        if plt is not None:
            figures[0] = draw_figure1(plt, run_dir, rows)
            figures[1] = draw_figure2(plt, run_dir, rows)
            figures[2] = draw_figure3(plt, run_dir, source3)

    notes = []
    if make_figures and figures[0] is None:
        notes.append('matplotlib 不可用，三张诊断图记为 %s；图源 CSV 与主表已正常产出。'
                     % NA)
    head = ['# ItemCF 实验结果报告', '',
            '运行目录：`%s`' % run_dir, '',
            '## 主表（全部配置/基线，含失败项）', '',
            markdown_table(columns, table_rows)]
    conclusion = build_conclusion(run_dir, rows, figures, [source1, source2, source3],
                                  notes, extra=extra)
    path = os.path.join(run_dir, 'conclusion.md')
    with open(path, 'w', encoding='utf-8') as handle:
        handle.write('\n'.join(head) + '\n')
        handle.write(conclusion)
    return {'run_dir': run_dir, 'rows': len(rows), 'summary_table': 'summary_table.csv',
            'conclusion': path, 'figures': figures,
            'source_csvs': [source1, source2, source3], 'notes': notes}


def main(argv=None):
    parser = argparse.ArgumentParser(description='ItemCF experiment report builder')
    parser.add_argument('--run-dir', required=True, help='一次运行的产物目录')
    parser.add_argument('--no-figures', action='store_true',
                        help='只产主表与图源 CSV，跳过 matplotlib 绘图')
    args = parser.parse_args(argv)
    summary = build_report(args.run_dir, make_figures=not args.no_figures)
    print(json.dumps({'rows': summary['rows'], 'figures': summary['figures'],
                      'conclusion': summary['conclusion']}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    sys.exit(main())
