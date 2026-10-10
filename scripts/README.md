# 工具脚本说明

本目录包含项目的辅助工具脚本。

## 凭据处理规范

**所有凭据一律从根目录 `config.yaml` 读取，禁止硬编码。**

`config.yaml` 已被 `.gitignore` 忽略：不进版本库，只在本机留存，需要时直接读。
新环境请复制 `config.example.yaml` 为 `config.yaml` 并填入真实值。

项目根目录提供统一读取入口 `config_loader.py`：

```python
from config_loader import mysql_kwargs, tmdb_config, vm_config

conn = pymysql.connect(**mysql_kwargs())   # MySQL 连接参数
key  = tmdb_config()['api_key']            # TMDB API Key
vm   = vm_config()                         # 虚拟机 SSH / MySQL
```

缺少字段时会抛出 `ConfigError` 并指明是哪个段的哪个字段，不会静默使用空值。

## 历史事件：硬编码凭据（代码侧已处理）

早期提交 `f4dcba3` 曾把 4 个辅助脚本连同硬编码凭据一起提交：

| 脚本 | 曾硬编码 |
|------|----------|
| `check_db.py` | MySQL 账号密码 |
| `fetch_posters.py` | MySQL 账号密码、TMDB API Key |
| `import_movie_stats.py` | MySQL 账号密码 |
| `sync_movie_stats.py` | MySQL 账号密码、虚拟机 SSH 密码 |

当时的补救只往 `.gitignore` 加了规则。但**这 4 个文件已被 git 跟踪，而 `gitignore` 对已跟踪文件不生效**，
所以凭据始终留在索引里，规则从未起过作用。现已完成：

1. 凭据全部迁入 `config.yaml`，4 个脚本改为经 `config_loader.py` 读取；
2. `config.example.yaml` 补齐 `tmdb` / `vm` 段，与真实结构保持一致（值为占位符）；
3. `.gitignore` 中那几条无效规则已移除。

**仍需人工完成：轮换 MySQL 密码、虚拟机 SSH 密码、TMDB API Key。**

`f4dcba3` 尚未推送到 `origin`（`origin/master` 停在 `fe413df`），
轮换后历史中残留的旧值即为失效值，因此不再需要 `filter-repo` 重写历史。
若在轮换之前就 `git push`，这些仍有效的凭据会被公开。

## 现有工具

- `init_hdfs.sh` - 初始化 HDFS 并上传数据
- `run_training.sh` - 提交 Spark 训练作业
- `start_dev.sh` - 启动前后端开发服务器（Linux，前台）
- `start_backend.py` - 启动 Flask 后端
- `start_bg.ps1` - Windows 后台启动 Flask + Vite（隐藏窗口，日志/PID 落 `logs\`；`start.bat` 调它）。
  它做四件事：① 先停掉上一批进程（按 `logs\*.pid`，再按端口 8000/5173 兜底）② 起前后端
  ③ **等后端就绪再开浏览器**：轮询 `GET /api/health`（不鉴权，内含一次 `SELECT 1`），通过后把首页
  那次查询（`/api/movies/?page=1&per_page=24&sort=popular`）打热，再等前端端口，最后打开浏览器
  ④ 以 `-WatchMinutes` / `-WatchPids` 参数再拉起自己当看门狗，到点自动停掉这两个进程。
  **看门狗就是同一份脚本**，所以项目里没有停止脚本；
  想提前停就结束 python/node，或再跑一次 `start.bat`（等于重启）。

  **为什么 ③ 要等后端（2026-09-18 修的）**：vite 两秒就绪，而 Flask **冷启动（重启机器后第一次）
  实测 ~20s 才监听端口**（热启动只要 ~1.3s，差的是操作系统第一次把 python 与一堆 DLL 读进内存）。
  早先「只等前端端口」的做法会让首页那次 `/api/movies/` 直撞代理的 `ECONNREFUSED`
  （`logs\vue.err.log` 里可以查到），列表就一直空着，得手动刷新。现在后端就绪与预热都会打印出来：
  `Backend ready in Xs` / `Warmed up home page query (+Xs)`。
  前端侧另有一层兜底：`frontend/src/api/index.js` 遇到「连不上后端」会对 GET 退避重试两次。
  ⚠️ `start_bg.ps1` 与 `start.bat` 都刻意写成**纯 ASCII**，别再往里加中文注释：
  `start.bat` 用 Windows PowerShell 5.1 跑（无 BOM 的 UTF-8 会被按 ANSI/GBK 解码，
  中文注释会把 `param()` 块解坏、直接解析报错），`cmd.exe` 按 OEM 代码页读 `.bat`
  （UTF-8 中文的 `rem` 行会被当成命令执行，实测导致过 `start.bat` 递归调用）。
  中文说明统一放本文件。
- `build_ratings_clean.py` - 从 `data/raw/ratings.csv` 聚合出 `ratings_clean.csv`（`--check` 与现有产物比对，不写出）
- `build_ml_rating_stats.py` - 聚合五档评分分布写入 `movie_rating_stats` 表
- `import_movie_stats.py` - 把 `ratings_clean.csv` 的均值/评分人数回填 `movies` 表
- `check_db.py` - 打印各表行数与当前 active 模型（2026-09-17 从根目录移入）
- `fetch_posters.py` - TMDB 富化回填（海报/简介/演职员；2026-09-17 从根目录移入）

（`sync_movie_stats.py` 已于 2026-09-16 删除：它读虚拟机上一张并不存在的 `movie_stats_temp` 表，
职责早已被 `scripts/import_movie_stats.py` 取代。）

其中 `fetch_posters.py` 有两个运行前提：
- **本机 `127.0.0.1:65532` 代理必须在线**——TMDB 走裸 socket 的 `CONNECT` 隧道
  （单隧道代理会拒绝 urllib3/requests 的握手，所以刻意不用 requests）
- 建议用 `backend/venv` 解释器运行；该脚本只依赖 `pymysql` + `PyYAML`，不依赖 requests

它不建任何进度/队列表：**待补清单和上次抓取失败的记录是同一件事**，就是 `movies` 里缺列的行，
直接查库即可（脚本每次运行结束也会打印这条 SQL）：

```sql
SELECT movie_id, title, year FROM movies
WHERE COALESCE(poster_url, '') = '' OR overview IS NULL OR director IS NULL;
```
