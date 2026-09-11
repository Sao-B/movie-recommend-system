#!/usr/bin/env python3
"""从虚拟机 MySQL 同步电影评分统计到本地 MySQL"""
import pymysql
import paramiko

from config_loader import mysql_kwargs, vm_config

# 虚拟机 SSH / MySQL 配置（凭据从 config.yaml 读取）
_vm = vm_config()
VM_HOST = _vm['host']
VM_USER = _vm['ssh_user']
VM_PASSWORD = _vm['ssh_password']

VM_MYSQL_HOST = 'localhost'
VM_MYSQL_USER = _vm['mysql_user']
VM_MYSQL_PASSWORD = _vm['mysql_password']
VM_MYSQL_DB = _vm['mysql_database']

# 本地 MySQL 配置（凭据从 config.yaml 读取）
LOCAL_MYSQL = mysql_kwargs()

print("正在建立 SSH 连接...")
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect(VM_HOST, username=VM_USER, password=VM_PASSWORD)

# 创建 SSH 隧道
print("正在创建 SSH 隧道...")
transport = ssh.get_transport()
local_port = 13306
remote_bind_address = (VM_MYSQL_HOST, 3306)

# 直接通过 SSH 执行命令读取数据
print("正在从虚拟机 MySQL 读取统计数据...")
stdin, stdout, stderr = ssh.exec_command(
    f"mysql -u {VM_MYSQL_USER} -p{VM_MYSQL_PASSWORD} {VM_MYSQL_DB} "
    f"-e 'SELECT movie_id, avg_rating, rating_count FROM movie_stats_temp' "
    f"-B -N"
)

lines = stdout.read().decode('utf-8').strip().split('\n')
stats_data = []
for line in lines:
    if line.strip():
        parts = line.split('\t')
        if len(parts) == 3:
            movie_id, avg_rating, rating_count = parts
            stats_data.append((float(avg_rating), int(rating_count), int(movie_id)))

print(f"读取到 {len(stats_data)} 条统计数据")

# 连接本地 MySQL 并更新
print("正在连接本地 MySQL...")
local_conn = pymysql.connect(**LOCAL_MYSQL)

print("正在批量更新本地数据库...")
cursor = local_conn.cursor()

# 批量更新
batch_size = 1000
for i in range(0, len(stats_data), batch_size):
    batch = stats_data[i:i+batch_size]
    cursor.executemany(
        "UPDATE movies SET avg_rating = %s, rating_count = %s WHERE movie_id = %s",
        batch
    )
    local_conn.commit()
    print(f"已更新 {min(i+batch_size, len(stats_data))}/{len(stats_data)}")

# 验证更新结果
cursor.execute("SELECT COUNT(*) FROM movies WHERE rating_count > 0")
updated_count = cursor.fetchone()[0]
print(f"\n更新完成！有评分的电影数量: {updated_count}")

cursor.close()
local_conn.close()
ssh.close()
print("同步完成")
