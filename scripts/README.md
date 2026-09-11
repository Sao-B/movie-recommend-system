# 工具脚本说明

本目录包含项目的辅助工具脚本。

## 重要安全提示

⚠️ **凭据泄露警告**：以下脚本曾被提交到 git 历史中，包含硬编码的敏感信息：
- `check_db.py` - MySQL 密码
- `fetch_posters.py` - TMDB API Key
- `import_movie_stats.py` - MySQL 密码、SSH 密码
- `sync_movie_stats.py` - MySQL 密码、SSH 密码

这些文件已被移到 `.gitignore`，但**历史记录中仍然存在**。

### 建议的补救措施

1. **立即重置 TMDB API Key**（如果仍在使用）
2. **修改 MySQL 密码**
3. **修改 SSH 密码**
4. **清理 git 历史**（如果需要）：
   ```bash
   # 使用 git filter-branch 或 BFG Repo-Cleaner 清理历史
   git filter-branch --force --index-filter \
     "git rm --cached --ignore-unmatch check_db.py fetch_posters.py import_movie_stats.py sync_movie_stats.py" \
     --prune-empty --tag-name-filter cat -- --all
   ```

## 工具脚本使用

所有凭据应从 `config.yaml` 读取，示例：

```python
import yaml

with open('config.yaml', 'r', encoding='utf-8') as f:
    config = yaml.safe_load(f)

mysql_host = config['mysql']['host']
mysql_password = config['mysql']['password']
tmdb_api_key = config['tmdb']['api_key']
```

## 现有工具

- `init_hdfs.sh` - 初始化 HDFS 并上传数据
- `run_training.sh` - 提交 Spark 训练作业
- `start_dev.sh` - 启动前后端开发服务器
