# -*- coding: utf-8 -*-
"""生成全量训练的 ``conclusion.md``（计划书 §6 产物清单的最后一项）。

计划书 §6 对 ``conclusion.md`` 的要求是"**一页左右**"，写清：

1. 全部评分是否纳入；
2. 用户 / 电影 / 好评 / 边数；
3. 阶段耗时；
4. **采样最大 RSS**（不是"峰值"——见下）；
5. 覆盖缺口；
6. 是否可接入；
7. **并未进行新的泛化效果评估**。

## 为什么单独一个模块

``full_train.py`` 已经有 2600 行。把"读产物 + 渲染 Markdown"拆出来，
一是让渲染逻辑能被单元测试直接喂造好的 dict（不必真跑一轮全量），
二是让 ``conclusion.md`` 可以在训练**结束后**单独重生成
（例如先跑了 ``verify_full_model.py``，再把核验结论补进结论文件）。

## 用词纪律（计划书 §5.A 明确要求）

监督每 2 秒采样一次，记到的是**采样最大值**，**不是精确连续峰值**——
两次采样之间的瞬时高峰可能被漏掉。所以本文件里一律写
"sampled maximum"，并且显式写出采样间隔，不让读者误以为它是峰值保证。
上一轮就是因为把 8 个采样点的最大值当成峰值保证而出了错。

Python 3.6 兼容；只依赖标准库。
"""
import json
import os

#: 状态 → 中文说明（只允许计划书 §6 列出的明确值）。
STATUS_LABELS = {
    # 注意措辞：`success` 只表示**运行成功且模型文件自洽**，
    # **不代表**独立核验已通过（那是 `verification.json` 的事，见第 6/7 节）。
    'success': '运行成功（模型文件自洽、状态 `validated_unpublished`；'
               '**独立核验另见第 6 节**）',
    'resource_blocked': '资源不足，未开跑（有据可查地停止，不是算法效果零分）',
    'resource_failed': '运行中触发资源保护被终止（不是算法效果零分）',
    'monitor_failed': '监督自身失败，已停止工作（拒绝无保护继续运行）',
    'correctness_failed': '正确性检查失败（结果不可信，模型不标记成功）',
    'verification_failed': '**独立核验失败**：从原始评分重算与模型不符，'
                           '模型已被降级、**不可使用**',
    'running': '仍在运行',
}


def _read_json(path):
    """读 JSON；缺失返回 None（结论文件要能在部分产物缺失时仍生成）。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding='utf-8') as handle:
            return json.load(handle)
    except ValueError:
        return None


def _fmt(value, digits=6, dash='N/A'):
    if value is None:
        return dash
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, float):
        return ('%.' + str(digits) + 'f') % value
    return str(value)


def _fmt_int(value, dash='N/A'):
    if value is None:
        return dash
    try:
        return '{:,}'.format(int(value))
    except (TypeError, ValueError):
        return dash


def _verification_summary(vf):
    """把 ``verify_full_model`` 的 ``verification.json`` 归一成结论要用的几个数。

    真实 schema 是**嵌套**在 ``checks`` 下的（实测）：

    - ``checks/recompute/{checked_sources, mismatch_count, pass}``
    - ``checks/no_self_loops/{self_loops, pass}``
    - ``checks/zero_like_empty_intervals/{empty_with_zero_like, ...}``
    - ``checks/structure/{pass, stats}``
    - ``checks/n_like_from_input/{mismatch_count, pass}``
    - ``checks/id_consistency/pass``

    不能去顶层找 ``self_loops`` 之类的键——那会渲染成 N/A（我第一版就写错了）。
    """
    if not vf:
        return None
    checks = vf.get('checks') or {}
    rec = checks.get('recompute') or {}
    loops = checks.get('no_self_loops') or {}
    zero = checks.get('zero_like_empty_intervals') or {}
    struct = checks.get('structure') or {}
    nlike = checks.get('n_like_from_input') or {}
    ident = checks.get('id_consistency') or {}
    return {
        'pass': vf.get('pass'),
        # ``recompute_ran``：独立核验**是否真的跑了**。这是与 ``pass`` 不同的量——
        # ``verify_full_model`` 曾经在"跳过重算"时也报 pass=True（已修为 pass=False），
        # 但结论文件仍应**独立判断**一次，不能只信顶层 pass：
        # 顶层 pass 可能来自老版本的产物文件（磁盘上已有的旧 verification.json）。
        'recompute_ran': bool(
            (not rec.get('skipped', False)) and rec.get('checked_sources')),
        'recompute_skipped': rec.get('skipped'),
        'checked_sources': rec.get('checked_sources'),
        'source_mismatches': rec.get('mismatch_count'),
        'boundary_sources': rec.get('boundary_sources'),
        'high_support_sources': rec.get('high_support_sources'),
        'low_support_sources': rec.get('low_support_sources'),
        'self_loops': loops.get('self_loops'),
        'self_loops_pass': loops.get('pass'),
        'zero_like_empty': zero.get('empty_with_zero_like'),
        'zero_like_empty_bad': zero.get('empty_without_zero_like'),
        'structure_pass': struct.get('pass'),
        'n_like_mismatches': nlike.get('mismatch_count'),
        'id_consistency_pass': ident.get('pass'),
        'failures': vf.get('failures') or [],
        'finished_at': vf.get('finished_at'),
    }


def collect(run_dir, model_dir=None, verification=None, precheck=None,
            run_status=None, manifest=None, guard_status=None):
    """把产物读成一个 dict（缺失的键为 None，不抛错）。

    :param verification: 已读好的 ``verification.json``（可选；调用方可先跑
        ``verify_full_model.py`` 再生成结论，把核验结果并进来）。
    """
    run_dir = os.path.abspath(run_dir)
    if model_dir is None:
        model_dir = os.path.join(run_dir, 'model')
    run_status = run_status if run_status is not None else _read_json(
        os.path.join(run_dir, 'run_status.json'))
    manifest = manifest if manifest is not None else _read_json(
        os.path.join(run_dir, 'manifest.json'))
    precheck = precheck if precheck is not None else _read_json(
        os.path.join(run_dir, 'precheck.json'))
    verification = verification if verification is not None else _read_json(
        os.path.join(run_dir, 'verification.json'))
    guard_status = guard_status if guard_status is not None else _read_json(
        os.path.join(run_dir, 'guard_status.json'))
    model_meta = _read_json(os.path.join(model_dir, 'model.json'))
    return {
        'run_dir': run_dir,
        'model_dir': model_dir,
        'run_status': run_status,
        'manifest': manifest,
        'precheck': precheck,
        'verification': verification,
        'verification_summary': _verification_summary(verification),
        'guard_status': guard_status,
        'model_meta': model_meta,
    }


def render(data):
    """把 :func:`collect` 的结果渲染成 ``conclusion.md`` 文本。"""
    rs = data.get('run_status') or {}
    mf = data.get('manifest') or {}
    pk = data.get('precheck') or {}
    vf = data.get('verification') or {}
    mm = data.get('model_meta') or {}

    run_id = rs.get('run_id') or mf.get('run_id') or '(unknown)'
    model_id = rs.get('model_id') or mf.get('model_id') or '(none)'
    status = rs.get('status') or mf.get('status') or '(unknown)'
    counts = rs.get('counts') or mf.get('counts') or {}
    params = rs.get('config') or mf.get('parameters') or {}
    phases = rs.get('phase_seconds') or {}
    sampled = rs.get('sampled_max') or {}
    inp = (pk.get('input') or mf.get('input') or {})

    # 输入指纹在不同产物里分别是 dict（precheck.input / manifest.input）
    # 或裸字符串（manifest.input 在旧版里是路径字符串）。
    if not isinstance(inp, dict):
        inp = {'path': inp}

    lines = []
    add = lines.append

    add('# ItemCF 全量训练结论')
    add('')
    add('run_id：`%s` ｜ model_id：`%s`' % (run_id, model_id))
    add('')
    add('| 项 | 值 |')
    add('|---|---|')
    add('| 状态 | `%s` — %s |'
        % (status, STATUS_LABELS.get(status, '（未在计划书状态表内的值）')))
    if rs.get('failure_reason'):
        add('| 失败原因 | %s |' % rs['failure_reason'])
    add('| 输入 | `%s` |' % (inp.get('path') or 'N/A'))
    add('| 输入 SHA-256 | `%s` |' % (inp.get('sha256') or 'N/A'))
    add('| 参数 | k=%s, beta=%s, 好评阈值=%s, block_size=%s |'
        % (_fmt(params.get('max_k'), 0, '?'), _fmt(params.get('beta'), 0, '?'),
           _fmt(params.get('positive_threshold'), 0, '?'),
           _fmt(params.get('block_size'), 0, '?')))
    add('')

    # ---- 全部评分是否纳入 ----
    add('## 1. 全部评分是否纳入')
    add('')
    add('- 本轮读取的输入文件：`%s`（SHA-256 `%s`）。'
        % (inp.get('path') or 'N/A', inp.get('sha256') or 'N/A'))
    rows = counts.get('input_rows') or inp.get('rows')
    # 三种情形都要给出**明确说法**，不能有"沉默"的分支：
    #   - rows 已知且 == 完整的 32,000,204 -> 正面确认；
    #   - rows 已知但不是那个数 -> 警告"不是全量"；
    #   - rows **未知** -> 也必须警告。曾经的写法是
    #     `... if rows is not None else ''`，于是 rows 缺失时整条警告变成**一个空行**，
    #     规模那一行显示 N/A 却没有任何提醒 —— 读者会以为"没说就是没问题"。
    if rows == 32000204:
        add('- 该文件是**完整** MovieLens-32M 评分文件（32,000,204 行 / 200,948 用户）。')
    elif rows is None:
        add('- ⚠️ **无法确认输入规模**：产物里没有记录读取行数（`input_rows`）。'
            '无法判断本轮是全量还是子集，**不要把本轮结果当作全量结论**。')
    else:
        add('- ⚠️ 该输入**不是**完整的 MovieLens-32M（完整文件应为 32,000,204 行）；'
            '本轮规模见下。**不要把本轮结果当作全量结论。**')
    add('- 读取行数：**%s**；其中被判为好评（rating >= %s）的 `(user, movie)` 对：**%s**。'
        % (_fmt_int(rows),
           _fmt(params.get('positive_threshold'), 1, '4.0'),
           _fmt_int(counts.get('positive_pairs') or inp.get('positive_pairs'))))
    add('- **未做留出**：全量模型使用所有有效历史评分，因此**没有测试段**；'
        '这也意味着本轮**不产生**任何泛化指标（见第 8 节）。')
    add('- 重复/非法行的处理：`%s`。重复必须报告位置并停止，不得静默丢弃或累加。'
        % (params.get('duplicate_policy') or 'reject'))
    dup = rs.get('duplicate_check') or {}
    if dup:
        add('- 唯一性检查：检查行数 %s，重复出现 **%s** 次。'
            % (_fmt_int(dup.get('rows_checked')),
               _fmt_int(dup.get('duplicate_occurrences'))))
    add('')

    # ---- 规模 ----
    add('## 2. 规模（用户 / 电影 / 好评 / 边）')
    add('')
    add('| 项 | 值 |')
    add('|---|---|')
    add('| 用户数 | %s |' % _fmt_int(counts.get('users')))
    add('| 电影数（合法评分中出现过的全部） | %s |' % _fmt_int(counts.get('items')))
    add('| 其中好评支持度为 0 的电影 | %s |' % _fmt_int(counts.get('items_zero_like')))
    add('| 好评 `(user, movie)` 对 | %s |' % _fmt_int(counts.get('positive_pairs')))
    add('| 邻居边数 | %s |' % _fmt_int(counts.get('graph_edges')))
    add('| 有邻居的来源电影数 | %s |' % _fmt_int(counts.get('graph_sources')))
    if mm:
        mc = mm.get('counts') or {}
        add('| 模型内边数（model.json） | %s |' % _fmt_int(mc.get('n_edges')))
        add('| 模型状态 | `%s` |' % (mm.get('status') or 'N/A'))
    add('')
    add('每来源最多 %s 个邻居；只存**正相似度**邻居，不做双向补边、不热门补齐；'
        '计数 int64、相似度 float64。'
        % _fmt(params.get('max_k'), 0, '100'))
    add('')

    # ---- 阶段耗时 ----
    add('## 3. 阶段耗时')
    add('')
    if phases:
        add('| 阶段 | 秒 |')
        add('|---|---:|')
        for name in ('prepare', 'precheck', 'train', 'verify'):
            if name in phases:
                add('| %s | %s |' % (name, _fmt(phases.get(name), 1)))
        add('| **合计** | %s |' % _fmt(rs.get('seconds'), 1))
    else:
        add('（未记录阶段耗时）')
    add('')
    proj = pk.get('projection') or {}
    if proj:
        add('- 预检外推：均值 %s h、最大值界 %s h（含装配预留 %s s）。'
            % (_fmt(proj.get('projected_hours_mean'), 3),
               _fmt(proj.get('projected_hours_max'), 3),
               _fmt(proj.get('assembly_reserve_seconds'), 1)))
        add('- 预检与正式阶段共用同一个 `collect_csr_block`，'
            '所以预检测的就是正式要付的时间。')
    add('')

    # ---- 资源 ----
    add('## 4. 资源（**采样最大值**，不是峰值）')
    add('')
    if sampled:
        add('- 采样点数：%s；采样间隔：%s s；**最大采样间隔**：%s s。'
            % (_fmt_int(sampled.get('sample_count')),
               _fmt(params.get('monitor_interval'), 1, '2.0'),
               _fmt(sampled.get('max_sample_gap_s'), 2)))
        add('- 进程树 RSS **采样最大值**：%s MB（%s GiB）。'
            % (_fmt(sampled.get('rss_sample_max_mb'), 1),
               _fmt(sampled.get('rss_sample_max_gi'), 3)))
        add('- MemAvailable **采样最小值**：%s MB。'
            % _fmt(sampled.get('mem_available_sample_min_mb'), 1))
        add('- 磁盘可用**采样最小值**：%s MB。'
            % _fmt(sampled.get('disk_free_sample_min_mb'), 1))
        if sampled.get('unmeasured_samples'):
            add('- ⚠️ 未能测量的采样点：%s（这些时刻没有有效读数）。'
                % _fmt_int(sampled.get('unmeasured_samples')))
        add('')
        add('> **这是采样最大值，不是精确连续峰值。** 监督按固定间隔采样，'
            '两次采样之间的瞬时高峰可能未被记录。判定资源是否安全时不要把它'
            '当作峰值保证——上一轮实验就是因为把 8 个采样点的最大值当成峰值保证'
            '而得出错误结论。')
    else:
        add('（未记录采样数据）')
    add('')

    # ---- 覆盖缺口 ----
    add('## 5. 覆盖缺口')
    add('')
    gaps = []
    if counts.get('items_zero_like'):
        gaps.append('有 %s 部电影好评支持度为 0，它们作为**来源**时没有任何邻居'
                    '（这是定义使然，不是漏算）。'
                    % _fmt_int(counts.get('items_zero_like')))
    if counts.get('items') and counts.get('graph_sources'):
        silent = int(counts['items']) - int(counts['graph_sources'])
        gaps.append('有 %s 部电影没有任何邻居边（占 %.2f%%）。'
                    % (_fmt_int(silent), 100.0 * silent / float(counts['items'])))
    if pk.get('blocks_failed'):
        gaps.append('预检有 %s 个块失败：%s'
                    % (len(pk['blocks_failed']), pk['blocks_failed']))
    if not gaps:
        gaps.append('未发现覆盖缺口。')
    for g in gaps:
        add('- %s' % g)
    add('')

    # ---- 正确性核验 ----
    add('## 6. 正确性核验')
    add('')
    vs = data.get('verification_summary')
    if vs:
        add('- 独立核验总体：**%s**'
            % ('通过' if vs.get('pass') else '**未通过**'))
        add('- 抽样来源独立重算：从**原始评分**重算 %s 个来源，'
            '**不符 %s 处**（其中高支持度 %s、低支持度 %s、并列边界 %s）。'
            % (_fmt_int(vs.get('checked_sources')),
               _fmt_int(vs.get('source_mismatches')),
               _fmt_int(vs.get('high_support_sources')),
               _fmt_int(vs.get('low_support_sources')),
               _fmt_int(vs.get('boundary_sources'))))
        add('- 结构检查：%s；自环 %s；零好评来源却带邻居 %s；'
            'n_like 与输入不一致 %s 处；产物身份一致 %s。'
            % ('通过' if vs.get('structure_pass') else '**未通过**',
               _fmt_int(vs.get('self_loops')),
               _fmt_int(vs.get('zero_like_empty_bad')),
               _fmt_int(vs.get('n_like_mismatches')),
               _fmt(vs.get('id_consistency_pass'))))
        if vs.get('failures'):
            add('- ⚠️ 失败项：%s' % vs['failures'])
    else:
        add('- ⚠️ **未找到 `verification.json`**：本轮尚未执行独立核验'
            '（`experiments.itemcf.verify_full_model`）。'
            '**在核验通过前不要把模型视为可用。**')
    add('')
    add('- 等价性证据（旧 2k / fix14 的 k=100、beta=0）：新实现与冻结基准'
        '**逐用户推荐 ID/次序完全一致**、分数差 ≤1e-9、'
        'NDCG/Recall 差 ≤1e-10，且与 block_size（16/64/128）无关。'
        '该对拍只证明**实现等价**，不计为新的推荐效果实验。')
    add('')

    # ---- 是否可接入 ----
    add('## 7. 是否可接入（本轮结论）')
    add('')
    # 两件事必须**分开陈述**，不能混成一句话：
    #   (a) 文件自洽（哈希+结构，由构图层自查）—— 这决定 `validated_unpublished`；
    #   (b) 独立核验（从**原始评分**重算抽样来源）—— 这决定"是否真的复现了输入"。
    # 曾经的措辞是"自洽、可 mmap 加载、**已通过独立核验**"，把 (a) 说成了 (b)。
    # 而 `validated_unpublished` 的凭据链其实**只含自查**：`verify_hashes` 校验的是
    # "刚写下的哈希等于刚写下的哈希"（恒真），`validate_arrays` 又是同一个模块
    # 装配出来的数组自己查自己；独立核验是另一个 CLI（`verify_full_model`），
    # 其产物**不被** model_io / full_train 读取。所以只凭 (a) 不能宣称 (b)。
    self_consistent = (status == 'success'
                       and mm.get('status') == 'validated_unpublished')
    # H2：核验**真的发现不符**时，`model.json` 会被降级为 `verification_failed`
    # （由 `model_io.apply_verification_verdict` 写入）。这种模型必须被明确标成
    # **不可使用**，而不是继续按"文件自洽"来陈述。
    verification_downgraded = (mm.get('status') == 'verification_failed'
                               or status == 'verification_failed')
    independently_verified = bool(vs and vs.get('pass')
                                  and vs.get('recompute_ran', True)
                                  and vs.get('checked_sources'))
    if verification_downgraded:
        add('- 🛑 **模型已被独立核验判定为不可用**：`model.json` 的状态是 '
            '`verification_failed`。从原始评分重算的抽样来源与模型**不符**，'
            '因此它**不能被视为可信产物，也不得接入**。')
        add('- 处置：整轮重跑（重新装配 + 重新核验）。**不允许就地改回 '
            '`validated_unpublished`** —— 那等于把核验者的否定洗掉。')
        # 两个状态字段说的是**不同的事**，必须点明，否则读者会以为产物自相矛盾：
        #   run_status.json 描述"那次运行跑得怎么样"（装配阶段结束时的判定）；
        #   model.json 描述"这份模型现在可不可信"（可被事后核验推翻）。
        # 事后核验失败时前者仍是 success，这是**正确**的；真正的结论以 model.json 为准。
        if status == 'success':
            add('- ⚠️ 注意 `run_status.json` 里仍是 `success`：它描述的是'
                '**运行过程**（装配阶段跑完且自查通过），而本模型是在**事后**'
                '独立核验时被推翻的。两者不矛盾——**以 `model.json` 的状态为准**。')
    if self_consistent:
        add('- 模型文件**自洽**：哈希一致、结构检查通过、可 mmap 加载，'
            '状态为 `validated_unpublished`。')
        add('  ⚠️ 这句**只说明文件自洽**：该状态的凭据是"写盘后重算的哈希与'
            '写盘时一致"加上"装配模块自查结构"，**里面没有独立核验**。')
    if independently_verified:
        add('- **独立核验已通过**：从原始评分重算了 %s 个来源，'
            '**不符 %s 处**（见第 6 节）。'
            % (_fmt_int(vs.get('checked_sources')),
               _fmt_int(vs.get('source_mismatches'))))
    elif self_consistent:
        add('- ⚠️ **独立核验未通过或未执行**：因此**本模型不能视为"已验证复现输入"**。'
            '重跑 `python -m experiments.itemcf.verify_full_model --run-dir <dir> '
            '--input <原始评分>` 并确认 `pass=true` 之后再看接入。')
    if self_consistent and independently_verified and not verification_downgraded:
        add('- **`validated_unpublished` 不代表线上 active**：本轮没有做'
            '加载到 MySQL/文件服务、模型版本切换或在线推荐接入。')
        add('- 建议下一步（**本轮不做**）：设计模型加载、版本切换与在线打分，'
            '并评估全量规模下的在线查询开销。')
    else:
        add('- **本轮不满足接入条件。** 原因见第 1 节的状态与第 6 节的核验结果；'
            '若为资源问题，按计划书 §7 报告瓶颈位置与最小工程调整建议，'
            '**不能用资源失败否定 ItemCF，也不能改成小样本结果冒充全量**。')
    add('')

    # ---- 明确声明 ----
    add('## 8. 本轮**没有**做的事（必须如实声明）')
    add('')
    add('- **没有进行新的泛化效果评估**：全量模型使用所有历史评分、不做留出，'
        '因此**不在训练数据上报告 NDCG/Recall**。论文效果引用既有的探索实验与'
        '新用户群体确认实验（`experiments/itemcf/out/confirmation/`）。')
    add('- 没有继续调 ALS、没有搜索 ItemCF 参数、没有做融合。')
    add('- 没有修改在线服务、数据库表或推荐页面。')
    add('- 没有实现 genres 冷启动调查。')
    add('- 全量效果是否提升**不属于本轮结论**。')
    add('')

    return '\n'.join(lines) + '\n'


def write_conclusion(run_dir, out_path=None, verification=None):
    """读产物并写 ``<run_dir>/conclusion.md``；返回写出的路径。"""
    data = collect(run_dir, verification=verification)
    text = render(data)
    if out_path is None:
        out_path = os.path.join(os.path.abspath(run_dir), 'conclusion.md')
    tmp = out_path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, out_path)
    return out_path
