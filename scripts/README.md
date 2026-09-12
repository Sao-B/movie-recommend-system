# 工具脚本说明

本目录包含项目的辅助工具脚本。

## 凭据处理规范

**所有凭据一律从根目录 `config.yaml` 读取，禁止硬编码。**

`config.yaml` 已被 `.gitignore` / `.aiignore` 双重忽略：既不进版本库，也不进 AI 上下文。
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
2. `config.example.yaml` 补齐 `tmdb` / `vm` / `als.ml_user_offset`，与真实结构保持一致（值为占位符）；
3. `.gitignore` 中那几条无效规则已移除。

**仍需人工完成：轮换 MySQL 密码、虚拟机 SSH 密码、TMDB API Key。**

`f4dcba3` 尚未推送到 `origin`（`origin/master` 停在 `fe413df`），
轮换后历史中残留的旧值即为失效值，因此不再需要 `filter-repo` 重写历史。
若在轮换之前就 `git push`，这些仍有效的凭据会被公开。

## 现有工具

- `init_hdfs.sh` - 初始化 HDFS 并上传数据
- `run_training.sh` - 提交 Spark 训练作业
- `start_dev.sh` - 启动前后端开发服务器
- `start_backend.py` - 启动 Flask 后端

另有一组辅助脚本位于**项目根目录**（不在本目录）：
`check_db.py`、`fetch_posters.py`、`import_movie_stats.py`、`sync_movie_stats.py`。
