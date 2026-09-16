#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地工作区代码同步到虚拟机（覆盖前自动备份 VM 旧副本）。

用法:
    python scripts/sync_code_to_vm.py            # 自动在 VM 上 find 定位项目目录
    python scripts/sync_code_to_vm.py /home/h/Project   # 指定 VM 上的项目路径

打包内容 = git 跟踪文件 + 未跟踪但未忽略的文件，排除 venv/node_modules/
.git/__pycache__ 等。VM 上解压覆盖，旧副本备份为 <项目>_backup_<时间戳>.tar.gz。
"""
import io
import os
import subprocess
import sys
import tarfile
import time

import paramiko

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config_loader import vm_config  # noqa: E402

EXCLUDE_DIRS = {".git", "venv", "node_modules", "__pycache__", ".workbuddy", "logs", "dist"}
EXCLUDE_FILES = {"backend.log", "poster_fetch.log", "poster_fetch_full.log", "nohup.out"}
EXCLUDE_EXT = {".log", ".pyc"}


def local_changed_files():
    """git 跟踪文件 + 工作区未忽略文件（含未跟踪新文件）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=root, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    keep = []
    for f in files:
        parts = f.split("/")
        if any(p in EXCLUDE_DIRS for p in parts[:-1] + [os.path.dirname(f)]):
            continue
        name = parts[-1]
        if name in EXCLUDE_FILES or os.path.splitext(name)[1] in EXCLUDE_EXT:
            continue
        keep.append(f)
    return root, sorted(set(keep))


def main():
    vm = vm_config()
    root, files = local_changed_files()
    print(f"打包 {len(files)} 个文件...")

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in files:
            p = os.path.join(root, f)
            if os.path.isfile(p):
                tar.add(p, arcname=f)
    payload = buf.getvalue()
    print(f"压缩包大小: {len(payload) / 1024 / 1024:.1f} MB")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(vm["host"], username=vm["ssh_user"], password=vm["ssh_password"], timeout=10)

    def run(cmd, timeout=60):
        _, out, err = ssh.exec_command(cmd, timeout=timeout)
        o = out.read().decode("utf-8", errors="replace").strip()
        e = err.read().decode("utf-8", errors="replace").strip()
        return o, e

    if len(sys.argv) > 1:
        target = sys.argv[1].rstrip("/")
    else:
        o, _ = run("find ~ -maxdepth 3 -name train_als.py 2>/dev/null | head -1")
        if not o:
            print("VM 上没找到 train_als.py，请手动指定项目路径：python scripts/sync_code_to_vm.py <路径>")
            ssh.close()
            sys.exit(1)
        target = os.path.dirname(o)
    print(f"VM 目标目录: {target}")

    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup = f"{target}_backup_{stamp}.tar.gz"
    run(f"tar czf '{backup}' -C '{target}' . 2>/dev/null; echo ok")
    print(f"VM 旧副本已备份: {backup}")

    sftp = ssh.open_sftp()
    remote_tmp = f"/tmp/sync_{stamp}.tar.gz"
    with sftp.open(remote_tmp, "wb") as rf:
        rf.write(payload)
    print("上传完成，解压覆盖...")

    o, e = run(f"tar xzf '{remote_tmp}' -C '{target}' && rm '{remote_tmp}' && echo OK")
    if "OK" not in o:
        print(f"解压失败: {e}")
        ssh.close()
        sys.exit(1)

    o, _ = run(f"grep -c 'left_semi' '{target}/spark/train_als.py' ; head -3 '{target}/spark/train_als.py'")
    print("VM 侧验证（train_als.py 应含新代码）:")
    print(" ", o.replace("\n", "\n  "))
    print("\n同步完成。")
    ssh.close()


if __name__ == "__main__":
    main()
