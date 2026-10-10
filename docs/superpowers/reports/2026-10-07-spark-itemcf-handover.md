# 交接文档：Spark ItemCF 训练收尾 + 双引擎对拍

写于 2026-10-07 17:00 ｜ 接手对象：下一个智能体（无历史记忆，本文自包含）
项目：`D:\Sundries\Paper\Project`（分支 `codex/als-ranking-eval`，注意本地有**大量未提交改动**）
配套阅读：`AGENTS.md`（全量背景）、`docs/superpowers/reports/2026-10-06-stage-conclusion.md`（阶段结论）

---

## 0. 你接手后的第一件事（TL;DR，按顺序做）

1. **查训练状态**（在跑 / 完成 / 挂了，见 §2）；
2. **若完成且 `spark_watch3.log` 末尾 `verify exit=0`** → 结构验收已过，直接做 §4 的对拍与评估；
3. **若挂了** → `tail -40 /home/h/itemcf_spark_train.log` 看死因（§5 坑位清单有前四次失败的原因，别重复踩）；
4. 全部验收通过后，往 `AGENTS.md` 顶部加一条条目（格式照抄既有条目），并告知用户。

---

## 1. 背景（30 秒版）

毕业论文项目：基于 Hadoop 生态的电影推荐系统（MovieLens-32M）。在线选型算法为 ItemCF（邻居图 + 裸求和打分，NDCG@20=0.1321，holdout 协议详见 `AGENTS.md`）。现有 ItemCF 邻居图由 **scipy 单机版**训练（`experiments/itemcf/full_train.py`）。

**本任务目标**：用 **PySpark 重写 ItemCF 训练**并产出与 scipy 版**同一契约**的模型目录，然后做"双引擎对拍"（同算法同参数，两种引擎，产物逐边相等 + 同协议指标复现）——让选型算法的训练层也落在 Hadoop/Spark 上，契合论文主题。

## 2. 当前在飞状态（接手时先核实，可能已完成或又挂）

- **训练进程**：VM hadoop102 上，PID **44889**（python3 train_itemcf.py）+ 配套 java，16:21 启动，预计总时长 1~2 小时（32M 行评分 → 正样本自连接 → 共现 → Top-100 邻居图）；
- **run 目录**：`/home/h/itemcf_out/itemcf_spark/<run_id>/`（本次 run_id 形如 `20261007T*-********`，启动时间戳命名）；
- **训练日志**：`/home/h/itemcf_spark_train.log`（带 `[itemcf-spark]` 前缀的是作业自己的进度行；大量 INFO 是 Spark 引擎日志）；
- **自动验收器**：`/home/h/spark_watch3.sh`（以 PID 44889 为参数）已部署——训练一退出自动执行 `verify_full_model`（结构 + 哈希链 + 抽样独立重算），结果写 **`/home/h/spark_watch3.log`**；
- **状态检查命令**：
  ```bash
  ps aux | grep -c "[t]rain_itemcf"          # 0=结束 1+=在跑
  ls /home/h/itemcf_out/itemcf_spark/*/model/  # 有 7 个 npy + model.json = 训练成功落盘
  tail -20 /home/h/itemcf_spark_train.log     # 死了看这里
  tail -10 /home/h/spark_watch3.log           # watcher 验收结果
  ```

## 3. 环境（全部实测过）

| 项 | 值 |
|---|---|
| VM ssh | `config.yaml` 的 `vm` 段（host/user/password），用 paramiko 连 |
| spark-submit | PATH 已有（`/opt/module/spark-3.5.1-bin-hadoop3/bin`），Spark 3.5.1，`--master local[*]` |
| 训练用 venv | `/home/h/venvs/itemcf/bin/python`（numpy 1.19.5；**注意 spark-submit 用的是系统 python3**，不是这个 venv） |
| 输入数据 | `/home/h/movielens/ratings.csv`（ML-32M 全量 32M 行） |
| VM 代码目录 | `/home/h/itemcf_holdout_code/`（experiments/itemcf/*.py 已全量同步 28 个、spark/、scripts/、**utils/itemcf_math.py 有一份拷贝在项目根 utils/**——worker 的 sys.path 指向项目根） |
| scipy 参照模型 | `/home/h/itemcf_full/20261004T195616Z-d535c178/model`（5,425,546 边） |
| 产出 | `/home/h/itemcf_out/itemcf_spark/<run_id>/model/`（7 npy + model.json） |

## 4. 训练成功后的验收三连（按顺序，全过才算完成）

1. **verify_full_model**（watcher 已自动跑，也可手动）：
   ```bash
   cd /home/h/itemcf_holdout_code && export PYTHONPATH=/home/h/itemcf_holdout_code
   /home/h/venvs/itemcf/bin/python experiments/itemcf/verify_full_model.py \
     --run-dir <run_dir> --input /home/h/movielens/ratings.csv
   ```
2. **双引擎对拍**（⚠️ **2026-10-07 更正：原文档写的"用 `equiv_check.py` 逐边比较两个模型目录"是错的**。该脚本的 argparse 只有 `--input / --baseline-dir / --out`，**没有 `--model-dir`**；它的机制是"用 `core.py` 从原始评分独立重算一遍，再跟一个 holdout 基线目录的冻结哈希 + per-user 推荐逐用户比"，**不能比较两个已存在的模型目录**。
   真正的逐边对拍需要**新写一个 CSR 比较脚本**：`model_io.load_arrays`（`mmap_mode='r'`）分别读入 Spark 产物与 scipy 参照目录，逐数组比对 `item_ids` / `indptr` / `target_indices` / `similarities` / `cooccurrences` / `n_like` / `n_all`。参照目录 `/home/h/itemcf_full/20261004T195616Z-d535c178/model`（5,425,546 边）已有 `model.json` 与 7 个 npy，契约见 `model_io.py` 的模块 docstring。预期 1e-12 容差内全等。
3. **同协议指标复现**（⚠️ **2026-10-07 更正：原文档写的 `--holdout-run-dir` / `--model-dir` 两个参数在 `heldout_eval.py` 里都不存在**。实际参数是 `--input / --out / --eval-size / --block-size / --chunk-rows / --max-k / --top-n / --test-frac / --budget-seconds / --monitor-interval / --project-root / --workers / --allow-overwrite / --skip-user-freeze`；其中 `--workers` 强制为 1。
   关键限制：它的图是**在脚本内自己重建的**（`build_graph_matrix`，`heldout_eval.py:1223`），**没有"从外部模型目录加载图"的入口**。所以"用 Spark 产物替换 scipy 图重跑评估"按原文命令走不通，需要改造：要么给 `heldout_eval.py` 加一个"外部图目录"入口，要么改走 `score_from_graph.py` / `experiments/itemcf/core.py` 的打分入口去读 Spark 产物。**这一步尚未做。**

全部通过 → **双引擎叙事闭环**（同一算法、scipy 单机 vs Spark 集群、同协议同指标），往 AGENTS.md 写条目，向用户汇报。

## 5. 坑位清单（前几次失败的完整原因——别重复踩）

| # | 现象 | 根因 | 已修/修复方式 |
|---|---|---|---|
| 1 | `PATH_NOT_FOUND: hdfs://hadoop102:9000/...` | 集群 `fs.defaultFS` 把本地路径劫持成 HDFS | runner 自动给本地路径加 `file://` 前缀（已修） |
| 2 | `$'\r': 未找到命令` | Windows CRLF 行尾 | 上传前转 LF（已修；**今后每次从 Windows 传脚本到 VM 都要防这个**） |
| 3 | worker `ModuleNotFoundError: No module named 'utils'`（两次！） | ① executor Python worker 不继承 driver 的 sys.path，cloudpickle 反序列化闭包时按引用 import `utils.itemcf_math`；② 就算 sys.path 对了，`itemcf_math.py` 也必须在**项目根/utils/** 下（曾只放在 spark/utils/ 下） | ① runner 加 `--conf spark.executorEnv.PYTHONPATH=/home/h/itemcf_holdout_code`；② 分区函数体内**局部** `from utils import itemcf_math`（局部 import 不被 cloudpickle 按引用捕获）；③ 项目根/utils/ 放一份 itemcf_math.py（已放） |
| 4 | watcher 误报/验证失败 | watcher 用 ps 判活误判 + `python -m experiments.itemcf.verify_full_model` 需要 PYTHONPATH 且模块曾未同步 | 已改 PID 键控 + 全量模块同步（如再失败，检查 experiments/__init__.py 是否存在，py3 命名空间包通常可省） |
| 5 | 本地脚本嵌套引号炸裂 | bash heredoc + paramiko 内嵌引号 | **所有 VM 侧多行逻辑写成 .py/.sh 文件用 sftp 上传执行**，别用内联 -c |
| 6 | **内核 OOM killer 杀掉 driver**（2026-10-07 17:48:50，PID 44889/44989，共跑 42 分钟） | **资源配置 vs 数据规模不匹配**，与代码逻辑无关。`make_edge_partition_fn`（`train_itemcf.py:287`）把整个分区的候选边攒进纯 Python dict `{src: ([tgt...], [c...])}` 才做 Top-100 截断，内存 **∝ 分区行数**（约 72B/边）。VM 只有 2 核 4G，`shuffle.partitions=8` 时每分区 3560 万行 → dict 2.56GB，2 核并发峰值 ~5.1GB > 4GB 物理内存 | **已修**：`spark.sql.shuffle.partitions` 由 `8` 改为 `32`（`train_itemcf.py` 的 `main()`），每分区 dict 降到 0.64GB、2 并发峰值 ~1.3GB，4G 机器有余量。**分区数只影响内存、不影响结果**——`repartition(N,'src')` 保证同一 src 的边不跨分区，Top-K 截断不跨分区。**别改回 8，也别加核**（加核 = 更多 task 各持一份 dict，是乘法）。诊断特征：日志末尾 `BlockManager: Putting block rdd_XXX failed ... Connection reset` 是**后果不是原因**，真因在 `dmesg -T \| grep -i 'out of memory'`；`free -m` 的 swap 会在死亡前十几分钟被吃满 |

## 6. 本地代码与测试状态

- 新增：`spark/train_itemcf.py`（566+行）、`spark/utils/itemcf_math.py`（260 行，纯 NumPy 数学核心）、`scripts/run_itemcf_training.sh`、`tests/test_spark_itemcf_core.py`（16 测试）；
- 测试：`backend/venv/Scripts/python.exe -m unittest tests.test_spark_itemcf_core tests.test_itemcf_online` → **26/26 绿**（数学核心与 `core.py` 在 β×k 六组合下全图逐位相等）；
- **上一轮修复后未重跑本地测试**：`train_itemcf.py` 的分区函数改过（局部 import），本地测试仍绿（刚验过），但建议你再跑一遍确认；
- 本地还有一个 16:21 之前的等价修复已在 VM 上验证（worker import 通过）。

## 7. 更大上下文：今天已完成的其他工作（都在 AGENTS.md）

- V4 三路融合已接入线上（`?model=fusion`，第三个推荐开关，admin 实测科幻画像 20/20 命中）；
- 两轮打分消融（裸求和最优 + 设计缺陷修正后 V4 融合精度不降反微升 0.1352≥0.1343）；
- 类型标签数量 vs 小众占比的对照实验（T0 85% → 满标签 10%，类型可控性与多样性权衡的定量数据）；
- 中差评 fold-in 敏感性实验（3 条 1 分差评 → 90% 榜单换血，fold-in 对负目标高度敏感）；
- admin 当前状态：18 部小众片 5 分 + 3 部热门片 1 分、类型标签空（实验遗留状态，可按需恢复）。

## 8. 本任务完成后的剩余待办（不是你的任务，仅背景）

- 代码提交（两分支大量未提交改动）；
- 论文正文（素材全齐：主表、ALS 对比、消融表、混合模型、双引擎对拍）；
- （可选）探索列/类型路由产品化。
