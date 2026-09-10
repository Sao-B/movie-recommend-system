# 基于 Hadoop 生态的个性化电影推荐系统实现指南

> 本文档面向需要快速理解项目实现流程的读者，按实际开发顺序讲解关键步骤和技术要点。

## 目录

- [一、项目概览](#一项目概览)
- [二、环境搭建](#二环境搭建)
- [三、数据库设计与初始化](#三数据库设计与初始化)
- [四、数据预处理实现](#四数据预处理实现)
- [五、离线 ALS 训练实现](#五离线-als-训练实现)
- [六、在线推荐服务实现](#六在线推荐服务实现)
- [七、后端 API 实现](#七后端-api-实现)
- [八、前端实现](#八前端实现)
- [九、运行与验证](#九运行与验证)
- [十、关键技术点总结](#十关键技术点总结)

---

## 一、项目概览

### 1.1 系统架构

本项目构建了一套完整的电影推荐系统，采用**离线训练 + 在线推荐**的两阶段架构：

```
[离线阶段]
MovieLens 32M 评分数据 (HDFS)
         +
系统用户评分 (MySQL)
         ↓
    Spark ALS 训练
         ↓
  电影隐因子 + 批量推荐结果
         ↓
      MySQL 存储

[在线阶段]
用户新增评分 → 固定电影因子求解用户向量 → 实时推荐
```

**核心设计思想：**
- **离线**：定期（如每周）用全量数据训练 ALS 模型，生成所有系统用户的批量推荐
- **在线**：用户提交新评分后，固定已训练的电影因子，只求解当前用户向量，立即返回更新后的推荐

### 1.2 技术栈

| 层次 | 技术 | 作用 |
|------|------|------|
| 分布式存储 | Hadoop HDFS | 存储 MovieLens 原始评分数据 |
| 离线计算 | PySpark + MLlib ALS | 分布式协同过滤模型训练 |
| 关系数据库 | MySQL 8.0 | 存储用户、电影、评分、模型因子、推荐结果 |
| 后端服务 | Flask + SQLAlchemy | RESTful API，提供认证、评分、推荐接口 |
| 前端界面 | Vue 3 + Element Plus | 单页应用，电影浏览、评分、推荐展示 |

### 1.3 数据流向

```
MovieLens CSV 文件
    ↓ (上传)
HDFS /movielens/
    ↓ (Spark 读取)
数据预处理 + ALS 训练
    ↓ (JDBC 写入)
MySQL (电影因子 + 推荐结果)
    ↓ (Flask 读取)
后端 API
    ↓ (Axios 调用)
Vue 前端展示
```

---

## 二、环境搭建

> **说明：** 本项目的虚拟机集群搭建采用尚硅谷大数据教程的经典配置模式，使用完全分布式部署，便于理解 Hadoop 生态的分布式特性。

### 2.1 集群规划

#### 2.1.1 为什么选择完全分布式

**完全分布式 vs 伪分布式：**

| 模式 | 节点数 | HDFS 副本 | 适用场景 |
|------|--------|-----------|----------|
| 伪分布式 | 1 台 | 只能为 1 | 功能验证，无法体现分布式特性 |
| 完全分布式 | 3+ 台 | 可设为 2-3 | 接近生产环境，可演示容错和负载均衡 |

**完全分布式的优势：**
1. **真实的数据分片**：HDFS 数据块分布在不同节点，可观察副本机制
2. **容错演示**：关闭一个 DataNode，数据仍可访问
3. **资源调度**：YARN 可以在多个 NodeManager 间分配任务
4. **生产就绪**：配置方式与企业部署一致

#### 2.1.2 集群角色分配

参照尚硅谷教程的经典 3 节点规划：

| 节点 | IP | 角色 | 进程 |
|------|-----|------|------|
| hadoop102 | 192.168.10.102 | 主节点 | NameNode、ResourceManager、MySQL、Spark |
| hadoop103 | 192.168.10.103 | 从节点 1 | DataNode、NodeManager |
| hadoop104 | 192.168.10.104 | 从节点 2 | DataNode、NodeManager、SecondaryNameNode |

**端口规划：**
- HDFS NameNode Web UI：9870
- YARN ResourceManager Web UI：8088
- MySQL：3306
- Flask：5000
- Vue Dev Server：5173

### 2.2 虚拟机准备

#### 2.2.1 软件版本

- **虚拟化软件**：VMware Workstation Pro 17 / VirtualBox 7
- **操作系统**：CentOS Linux 7 (Core)
- **Java**：JDK 1.8.0_291
- **Hadoop**：3.3.6
- **Spark**：3.5.1
- **MySQL**：8.0
- **Python**：3.12

> **本项目实际配置**：虽然文档介绍完全分布式集群搭建流程（hadoop102/103/104），但本项目实际使用的是**单机模式**（hadoop102），HDFS 副本数设为 1。这是为了在资源有限的开发环境下快速验证功能。生产环境建议按完全分布式部署。

#### 2.2.2 模板机制作（hadoop102）

**1. 创建虚拟机**
```
VMware → 新建虚拟机 → 典型
ISO 镜像：CentOS-7-x86_64-Minimal-2009.iso
虚拟机名称：hadoop102
磁盘大小：50GB，单个文件
内存：4GB
处理器：2 核
网络适配器：NAT 模式
```

**2. 安装 CentOS**
```
安装界面选择：
- 语言：中文（简体）/ English
- 软件选择：最小安装
- 网络：打开以太网，配置静态 IP
- 主机名：hadoop102
- root 密码：设置复杂密码
```

**3. 配置静态 IP**
```bash
vi /etc/sysconfig/network-scripts/ifcfg-ens33
```

修改为：
```bash
TYPE=Ethernet
BOOTPROTO=static            # 静态 IP
NAME=ens33
DEVICE=ens33
ONBOOT=yes                  # 开机启动

IPADDR=192.168.10.102       # IP 地址
NETMASK=255.255.255.0       # 子网掩码
GATEWAY=192.168.10.2        # 网关（NAT 模式默认）
DNS1=8.8.8.8
```

重启网络：
```bash
systemctl restart network
ip addr  # 验证 IP
```

**4. 配置主机名映射**
```bash
vi /etc/hosts
```

添加：
```
192.168.10.102 hadoop102
192.168.10.103 hadoop103
192.168.10.104 hadoop104
```

**5. 关闭防火墙（开发环境）**
```bash
systemctl stop firewalld
systemctl disable firewalld
systemctl status firewalld  # 验证已关闭
```

**6. 关闭 SELinux**
```bash
vi /etc/selinux/config
```

修改为：
```
SELINUX=disabled
```

重启生效：
```bash
reboot
```

**7. 创建普通用户**
```bash
useradd h
passwd h
# 输入密码两次

# 配置 sudo 权限
vi /etc/sudoers
```

在 `root ALL=(ALL) ALL` 下一行添加：
```
h ALL=(ALL) NOPASSWD:ALL
```

**8. 创建工作目录**
```bash
# 切换到 h 用户
su - h

# 创建软件存放目录
mkdir /opt/module  # 安装路径
mkdir /opt/software  # 安装包存放
sudo chown h:h /opt/module /opt/software
```

#### 2.2.3 克隆虚拟机

**1. 创建模板快照**
- 关闭 hadoop102
- VMware → 右键 hadoop102 → 快照 → 拍摄快照 → 命名"模板机"

**2. 克隆 hadoop103**
```
右键 hadoop102 → 管理 → 克隆
克隆类型：完整克隆
虚拟机名称：hadoop103
```

启动后修改配置：
```bash
# 修改主机名
hostnamectl set-hostname hadoop103

# 修改 IP
vi /etc/sysconfig/network-scripts/ifcfg-ens33
# 将 IPADDR 改为 192.168.10.103

# 重启网络
systemctl restart network
```

**3. 同样方式克隆 hadoop104**（IP 改为 192.168.10.104）

#### 2.2.4 配置 SSH 免密登录

在 **hadoop102** 上：

```bash
# 生成密钥对
ssh-keygen -t rsa
# 一路回车，不设密码

# 分发公钥到所有节点（包括自己）
ssh-copy-id hadoop102
ssh-copy-id hadoop103
ssh-copy-id hadoop104

# 验证免密登录
ssh hadoop103
exit
```

**为什么需要免密登录？**
- Hadoop 启动/停止脚本需要在各节点间执行命令
- HDFS、YARN 的进程间通信需要 SSH
- 集群管理脚本（如分发文件）需要免密

### 2.3 安装基础软件

以下操作在**所有节点**执行（可以写成脚本批量执行）：

#### 2.3.1 安装 Java

```bash
sudo apt update
sudo apt install -y openjdk-8-jdk

# 配置环境变量
echo 'export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64' >> ~/.bashrc
echo 'export PATH=$JAVA_HOME/bin:$PATH' >> ~/.bashrc
source ~/.bashrc

# 验证
java -version
```

#### 2.3.2 安装 Hadoop（3.3.6）

在 **hadoop102 节点**下载并配置：

```bash
cd /opt/software
wget https://dlcdn.apache.org/hadoop/common/hadoop-3.3.6/hadoop-3.3.6.tar.gz
tar -xzf hadoop-3.3.6.tar.gz -C /opt/module/

# 配置环境变量
echo 'export HADOOP_HOME=/opt/module/hadoop-3.3.6' >> ~/.bashrc
echo 'export PATH=$HADOOP_HOME/bin:$HADOOP_HOME/sbin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

#### 2.3.3 配置 Hadoop

在 `/opt/module/hadoop-3.3.6/etc/hadoop/` 目录下修改以下文件：

**1. `hadoop-env.sh`**
```bash
export JAVA_HOME=/opt/module/jdk1.8.0_291
```

**2. `core-site.xml`**
```xml
<configuration>
  <property>
    <name>fs.defaultFS</name>
    <value>hdfs://hadoop102:9000</value>
  </property>
  <property>
    <name>hadoop.tmp.dir</name>
    <value>/opt/module/hadoop-3.3.6/data</value>
  </property>
</configuration>
```

**3. `hdfs-site.xml`**
```xml
<configuration>
  <property>
    <name>dfs.replication</name>
    <value>1</value>  <!-- 单机模式，副本数为 1 -->
  </property>
  <property>
    <name>dfs.permissions.enabled</name>
    <value>false</value>  <!-- 关闭权限检查，便于开发 -->
  </property>
</configuration>
```

**4. `mapred-site.xml`**
```xml
<configuration>
  <property>
    <name>mapreduce.framework.name</name>
    <value>yarn</value>
  </property>
</configuration>
```

**5. `yarn-site.xml`**
```xml
<configuration>
  <property>
    <name>yarn.resourcemanager.hostname</name>
    <value>hadoop102</value>
  </property>
  <property>
    <name>yarn.nodemanager.aux-services</name>
    <value>mapreduce_shuffle</value>
  </property>
</configuration>
```

**6. `workers` 文件**
```
localhost
```

> **说明**：本项目使用单机模式，所有 Hadoop 组件运行在 hadoop102 上。如需完全分布式，修改 `workers` 为 `hadoop103` 和 `hadoop104`，并将配置分发到各节点。

#### 2.3.4 格式化 HDFS 并启动

在 **hadoop102** 上：

```bash
# 格式化 NameNode（仅首次执行）
hdfs namenode -format

# 启动 HDFS
start-dfs.sh

# 启动 YARN
start-yarn.sh

# 验证进程
jps
# 应该看到：NameNode、DataNode、SecondaryNameNode、ResourceManager、NodeManager
```

访问 Web UI：
- HDFS：`http://192.168.10.102:9870`
- YARN：`http://192.168.10.102:8088`

### 2.4 安装 Spark（3.5.1）

在 **hadoop102** 上：

```bash
cd /opt/software
wget https://dlcdn.apache.org/spark/spark-3.5.1/spark-3.5.1-bin-hadoop3.tgz
tar -xzf spark-3.5.1-bin-hadoop3.tgz -C /opt/module/

# 配置环境变量
echo 'export SPARK_HOME=/opt/module/spark-3.5.1-bin-hadoop3' >> ~/.bashrc
echo 'export PATH=$SPARK_HOME/bin:$SPARK_HOME/sbin:$PATH' >> ~/.bashrc
source ~/.bashrc

# 配置 Spark
cd /opt/module/spark-3.5.1-bin-hadoop3/conf
cp spark-env.sh.template spark-env.sh
vi spark-env.sh
```

在 `spark-env.sh` 中添加：
```bash
export JAVA_HOME=/opt/module/jdk1.8.0_291
export HADOOP_CONF_DIR=/opt/module/hadoop-3.3.6/etc/hadoop
export SPARK_LOCAL_IP=192.168.10.102
```

**安装 MySQL JDBC 驱动**（用于 Spark 读写 MySQL）：
```bash
cd /opt/module/spark-3.5.1-bin-hadoop3/jars
wget https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.0.33/mysql-connector-j-8.0.33.jar
```

验证安装：
```bash
spark-shell --version
```

### 2.5 安装 MySQL（仅 hadoop102）

```bash
# 安装 MySQL 8.0
sudo yum install -y wget
wget https://dev.mysql.com/get/mysql80-community-release-el7-3.noarch.rpm
sudo rpm -ivh mysql80-community-release-el7-3.noarch.rpm
sudo yum install -y mysql-server

# 启动 MySQL
sudo systemctl start mysqld
sudo systemctl enable mysqld

# 获取临时密码
sudo grep 'temporary password' /var/log/mysqld.log

# 登录并修改密码
mysql -u root -p
# 输入临时密码后执行：
ALTER USER 'root'@'localhost' IDENTIFIED BY 'YourStrongPassword@123';

# 创建应用用户
CREATE USER 'movie_app'@'%' IDENTIFIED BY 'MovieApp@2024';
GRANT ALL PRIVILEGES ON movie_recommend.* TO 'movie_app'@'%';
FLUSH PRIVILEGES;
EXIT;
```

**允许远程连接**（如需从开发机访问）：
```bash
sudo vi /etc/my.cnf
```

添加：
```ini
[mysqld]
bind-address=0.0.0.0
```

重启 MySQL：
```bash
sudo systemctl restart mysqld
```

### 2.6 安装 Python 环境（hadoop102）

```bash
# CentOS 7 默认 Python 版本较低，使用 Python 3.12
sudo yum install -y gcc openssl-devel bzip2-devel libffi-devel zlib-devel
cd /opt/software
wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz
tar -xzf Python-3.12.0.tgz
cd Python-3.12.0
./configure --enable-optimizations --prefix=/opt/module/python3.12
make -j$(nproc)
sudo make altinstall

# 创建软链接
sudo ln -s /opt/module/python3.12/bin/python3.12 /usr/bin/python3.12
sudo ln -s /opt/module/python3.12/bin/pip3.12 /usr/bin/pip3.12

# 验证
python3.12 --version
```

### 2.7 Node.js 安装（可在开发机安装）

如果前端在 hadoop102 上开发：

```bash
cd /opt/software
wget https://nodejs.org/dist/v18.18.0/node-v18.18.0-linux-x64.tar.xz
tar -xf node-v18.18.0-linux-x64.tar.xz -C /opt/module/
echo 'export PATH=/opt/module/node-v18.18.0-linux-x64/bin:$PATH' >> ~/.bashrc
source ~/.bashrc

# 验证
node -v
npm -v
```
cd /opt/spark/conf
cp spark-env.sh.template spark-env.sh
vim spark-env.sh
```

添加：
```bash
export JAVA_HOME=/usr/lib/jvm/java-8-openjdk-amd64
export HADOOP_CONF_DIR=/opt/hadoop/etc/hadoop
export SPARK_MASTER_HOST=hadoop-master
```

**安装 MySQL JDBC 驱动**（用于 Spark 读写 MySQL）：
```bash
cd /opt/spark/jars
wget https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.0.33/mysql-connector-j-8.0.33.jar
```

### 2.5 安装 MySQL（仅 Master 节点）

```bash
sudo apt install -y mysql-server

# 启动并设置开机自启
sudo systemctl start mysql
sudo systemctl enable mysql

# 安全配置
sudo mysql_secure_installation

# 允许远程连接
sudo vim /etc/mysql/mysql.conf.d/mysqld.cnf
# 将 bind-address = 127.0.0.1 改为 0.0.0.0

sudo systemctl restart mysql
```

### 2.6 安装 Python 环境（Master 节点）

```bash
sudo apt install -y python3.12 python3.12-venv python3-pip

# 创建项目目录
mkdir -p ~/movie-recommend
cd ~/movie-recommend

# 创建虚拟环境
python3.12 -m venv backend/venv
source backend/venv/bin/activate

# 安装 Python 依赖（稍后根据 requirements.txt 安装）
```

### 2.7 安装 Node.js（开发机或 Master）

```bash
curl -fsSL https://deb.nodesource.com/setup_18.x | sudo -E bash -
sudo apt install -y nodejs

# 验证
node -v
npm -v
```

---

## 三、数据库设计与初始化

### 3.1 设计思路

本项目的核心设计决策是**用户 ID 隔离**和**两阶段推荐**，数据库表结构围绕这两点展开。

#### 3.1.1 用户 ID 命名空间隔离

**问题：** MovieLens-32M 数据集包含 16 万真实用户的评分，系统注册用户的 ID 如何避免冲突？

**解决方案：**
- **系统用户**：MySQL `users` 表的自增 ID，正整数，范围 [1, ∞)
- **MovieLens 用户**：原始 `userId` 取负，负整数，范围约 [-162541, -1]

```
训练时的用户 ID 映射：
- 系统用户 123       → ALS 中仍为 123
- MovieLens 用户 456 → ALS 中为 -456
```

**好处：**
1. **永不冲突**：系统用户 ID 为正数，MovieLens 用户 ID 为负数，天然隔离
2. **查询简洁**：`WHERE user_id > 0` 即可过滤系统用户
3. **易于理解**：负数直观表示外部数据源
4. **两类用户在同一个 ALS 模型中训练**，共享电影隐因子
5. 推荐结果可以只保留系统用户，不会污染应用数据

#### 3.1.2 评分表分离

| 表名 | 作用 | 数据来源 |
|------|------|----------|
| `ratings` | 遗留表，不再使用 | 历史数据 |
| `user_ratings` | 系统用户评分 | 前端提交 + 训练读取 |
| MovieLens CSV（HDFS） | MovieLens 原始评分 | 离线训练读取 |

**为什么分离？**
- MovieLens 数据量大（3300 万条），不应导入 MySQL
- 系统用户评分需要实时写入，适合 MySQL
- 训练时合并两个数据源

#### 3.1.3 模型版本管理

每次训练生成一个新版本（如 `als-20260909T120000Z-abc123`），状态流转：

```
building → active → retired
  ↓          ↓         ↓
训练中    当前生效   已废弃
```

**核心表：**
- `model_versions`：版本元数据（rank、reg_param、状态）
- `model_item_factors`：电影隐因子（供在线推荐使用）
- `recommendations`：批量推荐结果（离线生成）

### 3.2 表结构详解

完整 DDL 见 [`sql/schema.sql`](../sql/schema.sql)。以下详细说明每个表的设计思路、字段含义和使用场景。

---

#### 3.2.1 `users` - 系统注册用户表

**作用：** 存储通过前端注册的应用用户，支持登录认证。

**表结构：**
```sql
CREATE TABLE users (
  id           INT PRIMARY KEY AUTO_INCREMENT COMMENT '用户主键，自增正整数',
  username     VARCHAR(50) UNIQUE NOT NULL    COMMENT '用户名，唯一',
  password_hash VARCHAR(255) NOT NULL          COMMENT 'bcrypt 哈希密码',
  created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间'
);
```

**设计要点：**
- **用户 ID 范围**：`id` 为自增正整数，范围 `[1, ∞)`
- **与 MovieLens 用户隔离**：MovieLens 用户 ID 在训练时取负，成为负整数 `[-162541, -1]`，与系统用户完全隔离
- **密码安全**：使用 bcrypt 算法加密存储，不存储明文密码

**关联关系：**
- `user_ratings.user_id` 外键引用 `users.id`
- `recommendations.user_id` 对应 `users.id`（无外键约束，批量导入性能考虑）

**查询场景：**
1. 用户登录验证：`SELECT * FROM users WHERE username = ?`
2. 训练脚本获取系统用户列表：`SELECT id FROM users`（用于过滤推荐结果）

---

#### 3.2.2 `movies` - 电影元数据表

**作用：** 存储电影的基本信息，从 MovieLens `movies.csv` 预处理后导入。

**表结构：**
```sql
CREATE TABLE movies (
  movie_id  INT PRIMARY KEY           COMMENT 'MovieLens 原始电影 ID',
  title     VARCHAR(500) NOT NULL     COMMENT '电影标题（含年份），如 "Toy Story (1995)"',
  genres    VARCHAR(500)              COMMENT '类型，竖线分隔，如 "Action|Drama"',
  year      SMALLINT                  COMMENT '上映年份，从 title 正则解析',
  INDEX idx_genres (genres(100))
);
```

**字段说明：**
- **movie_id**：使用 MovieLens 原始 ID，保持数据一致性
- **title**：包含年份信息，格式如 `"Toy Story (1995)"`
- **genres**：用竖线 `|` 分隔的类型列表，如 `"Adventure|Animation|Children|Comedy|Fantasy"`
- **year**：通过正则表达式 `\((\d{4})\)` 从 `title` 中提取

**数据来源：**
- Spark 脚本 `preprocess.py` 从 HDFS 读取 `movies.csv`
- 清洗数据（去重、提取年份、过滤无效行）后批量写入 MySQL
- 约 **86,000** 条电影记录

**关联关系：**
- `user_ratings.movie_id` 引用 `movies.movie_id`
- `recommendations.movie_id` 引用 `movies.movie_id`
- `model_item_factors.movie_id` 引用 `movies.movie_id`

**查询场景：**
1. 前端电影列表展示：
   ```sql
   SELECT * FROM movies 
   WHERE title LIKE '%Toy%' AND year >= 1990 
   ORDER BY year DESC LIMIT 20;
   ```
2. 推荐结果关联电影详情：
   ```sql
   SELECT r.*, m.title, m.genres 
   FROM recommendations r 
   JOIN movies m ON r.movie_id = m.movie_id 
   WHERE r.user_id = ? ORDER BY r.rank LIMIT 10;
   ```

---

#### 3.2.3 `ratings` - 遗留评分表（已废弃）

**作用：** 保留历史评分数据，但**不再由应用或训练任务读写**。

**废弃原因：**
1. 应用评分已迁移到 `user_ratings` 表
2. 训练时直接从 HDFS 读取 MovieLens 评分，从 `user_ratings` 读取系统用户评分
3. 保留此表仅为数据兼容性，确认无需后可删除

**注意事项：**
- ⚠️ 新代码不应操作此表
- 如需清理，可执行 `DROP TABLE ratings;`

---

#### 3.2.4 `user_ratings` - 系统用户评分表

**作用：** 存储系统注册用户的评分记录，用于应用评分功能和 ALS 训练。

**表结构：**
```sql
CREATE TABLE user_ratings (
  id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '评分记录主键',
  user_id   INT NOT NULL                      COMMENT '用户 ID，对应 users.id',
  movie_id  INT NOT NULL                      COMMENT '电影 ID，对应 movies.movie_id',
  rating    FLOAT NOT NULL CHECK (rating BETWEEN 0.5 AND 5.0) 
            COMMENT '评分值，范围 [0.5, 5.0]，步长 0.5',
  rated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '评分时间',
  UNIQUE KEY uk_user_movie (user_id, movie_id) 
            COMMENT '同一用户对同一电影只能评分一次',
  INDEX idx_user (user_id)   COMMENT '按用户查询评分历史',
  INDEX idx_movie (movie_id) COMMENT '按电影查询评分统计',
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

**设计要点：**
- **唯一性约束**：`uk_user_movie` 保证用户对同一电影只能有一条评分记录
- **评分范围**：`[0.5, 5.0]`，步长 0.5，与 MovieLens 数据集一致
- **外键约束**：`user_id` 引用 `users.id`，用户删除时级联删除其评分
- **只存储系统用户**：不包含 MovieLens 用户的评分

**数据流向：**

| 操作 | 方向 | 说明 |
|------|------|------|
| **写入** | 前端 → Flask API → MySQL | 用户提交评分后写入此表 |
| **读取 1** | Flask API ← MySQL | 查询用户历史评分，前端展示 |
| **读取 2** | Spark 训练 ← MySQL | 读取并合并到训练集 |
| **读取 3** | Flask 在线推荐 ← MySQL | 读取用户最新评分，计算用户向量 |

**与 MovieLens 评分的区别：**

| 维度 | MovieLens 评分 | 系统用户评分 |
|------|---------------|-------------|
| **数量** | 3300 万条 | 数量较少（随系统用户增长） |
| **存储** | HDFS `ratings.csv` | MySQL `user_ratings` 表 |
| **用户 ID** | 训练时取负，范围 `[-162541, -1]` | 直接使用 `users.id`，范围 `[1, ∞)` |
| **用途** | 离线训练、测试集 RMSE 评估 | 应用评分、训练、在线推荐 |

**查询场景：**
1. 前端展示用户评分历史：
   ```sql
   SELECT ur.*, m.title FROM user_ratings ur
   JOIN movies m ON ur.movie_id = m.movie_id
   WHERE ur.user_id = ? ORDER BY ur.rated_at DESC;
   ```
2. 在线推荐计算用户向量：
   ```sql
   SELECT movie_id, rating FROM user_ratings WHERE user_id = ?;
   ```
3. 离线训练读取系统用户评分：
   ```python
   # Spark 通过 JDBC 读取
   ratings_system = spark.read.jdbc(
       url=jdbc_url, table="user_ratings", properties={...}
   )
   ```

---

#### 3.2.5 `model_versions` - ALS 模型版本管理表

**作用：** 管理 ALS 模型的生命周期，记录训练参数和状态。

**表结构：**
```sql
CREATE TABLE model_versions (
  version      VARCHAR(64) PRIMARY KEY 
               COMMENT '模型版本号，格式 als-{timestamp}-{suffix}',
  `rank`       SMALLINT NOT NULL COMMENT 'ALS 隐因子维度，如 50',
  reg_param    FLOAT NOT NULL    COMMENT 'ALS 正则化系数，如 0.1',
  status       ENUM('building', 'active', 'retired') DEFAULT 'building'
               COMMENT '模型状态：building=训练中, active=当前生效, retired=已废弃',
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '模型创建时间',
  activated_at TIMESTAMP NULL     COMMENT '模型激活时间（状态变为 active 时记录）',
  INDEX idx_model_status (status, generated_at)
);
```

**版本命名规则：**
- 格式：`als-{timestamp}-{random_suffix}`
- 示例：`als-20260909T120000Z-abc123`
- `timestamp`：UTC 时间，格式 `YYYYMMDDTHHMMSSZ`
- `random_suffix`：6 位随机字符串，避免冲突

**状态流转：**
```
building → active → retired
   |         |         |
训练中   当前生效   已废弃
```

**状态说明：**

| 状态 | 含义 | 操作 |
|------|------|------|
| **building** | 模型正在训练，电影因子和推荐结果正在写入 | 训练脚本创建版本记录后立即设为 building |
| **active** | 训练完成，在线推荐使用该版本的电影因子 | 写入完成后调用 `activate_model_version()` |
| **retired** | 被新模型替换，保留一段时间后可清理 | 新模型激活时，旧 active 模型自动变为 retired |

**设计要点：**
- **同时只能有一个 active 模型**
- 训练脚本流程：
  1. 创建 `building` 记录
  2. 写入 `model_item_factors`
  3. 写入 `recommendations`
  4. 调用 `activate_model_version()` 变为 `active`
- 新模型激活时，自动将旧 `active` 模型改为 `retired`

**关联关系：**
- `model_item_factors.model_version` 外键引用 `model_versions.version`
- `recommendations.model_version` 关联（无外键约束）

**查询场景：**
1. 在线推荐获取当前活跃模型：
   ```sql
   SELECT version FROM model_versions WHERE status = 'active' LIMIT 1;
   ```
2. 训练脚本检查模型状态：
   ```sql
   SELECT status FROM model_versions WHERE version = ?;
   ```
3. 运维清理旧模型：
   ```sql
   DELETE FROM model_versions 
   WHERE status = 'retired' AND generated_at < DATE_SUB(NOW(), INTERVAL 30 DAY);
   ```

---

#### 3.2.6 `model_item_factors` - ALS 电影隐因子表

**作用：** 存储 ALS 训练得到的电影隐向量，供在线推荐使用。

**表结构：**
```sql
CREATE TABLE model_item_factors (
  model_version VARCHAR(64) NOT NULL COMMENT '模型版本号，引用 model_versions.version',
  movie_id      INT NOT NULL         COMMENT '电影 ID，对应 movies.movie_id',
  factors       TEXT NOT NULL        COMMENT 'JSON 数组格式的电影隐向量',
  generated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '因子生成时间',
  PRIMARY KEY (model_version, movie_id),
  INDEX idx_factor_version (model_version),
  FOREIGN KEY (model_version) REFERENCES model_versions(version) ON DELETE CASCADE
);
```

**数据格式说明：**
- **factors 字段**：存储为 JSON 数组字符串，如 `"[0.12, -0.34, 0.56, ...]"`
- **向量维度**：由 `model_versions.rank` 决定（如 `rank=50`，则向量长度为 50）
- **每个 (model_version, movie_id)** 对应一个电影隐向量

**数据来源：**
1. Spark 训练脚本 `train_als.py` 在模型训练完成后生成
2. 提取 ALS `model.itemFactors`
3. 转换为 JSON 字符串：`[float(f) for f in vector]` → JSON
4. 批量写入 MySQL

**数据使用：**
```python
# Flask 在线推荐服务加载电影因子
active_version = get_active_model_version()
item_factors = load_item_factors(active_version)  # 从 MySQL 加载

# 用户新增评分后，固定电影因子，求解用户向量
user_vector = solve_user_vector(user_ratings, item_factors, rank)

# 计算用户向量与所有电影因子的内积，得到预测评分
predictions = user_vector @ item_factors.T
```

**存储格式示例：**
```
model_version: "als-20260909T120000Z-abc123"
movie_id: 1
factors: "[0.123, -0.456, 0.789, 0.234, ..., -0.111]"  (50 维)
```

**性能优化：**
- **缓存策略**：Flask 服务启动时一次性加载 `active` 模型的所有电影因子到内存
- **避免频繁查询**：内存缓存后，推荐请求无需访问数据库
- **主键索引**：`(model_version, movie_id)` 保证查询效率

**查询场景：**
1. 在线推荐加载活跃模型的所有电影因子：
   ```sql
   SELECT movie_id, factors FROM model_item_factors 
   WHERE model_version = (SELECT version FROM model_versions WHERE status = 'active');
   ```
2. 训练完成后写入新模型的电影因子：
   ```python
   item_factors_df.write.jdbc(
       url=jdbc_url, table="model_item_factors", mode="append"
   )
   ```

---

#### 3.2.7 `recommendations` - 批量推荐结果表

**作用：** 存储 ALS 离线训练生成的批量推荐结果。

**表结构：**
```sql
CREATE TABLE recommendations (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '推荐记录主键',
  model_version    VARCHAR(64) NULL  COMMENT '生成推荐的模型版本号',
  user_id          INT NOT NULL      COMMENT '用户 ID，对应 users.id',
  movie_id         INT NOT NULL      COMMENT '推荐的电影 ID，对应 movies.movie_id',
  predicted_rating FLOAT NOT NULL    COMMENT 'ALS 预测评分',
  `rank`           SMALLINT NOT NULL COMMENT '该用户的推荐排名，1=最推荐，2=次之，...',
  generated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '推荐批次生成时间',
  INDEX idx_model_user_rank (model_version, user_id, `rank`),
  INDEX idx_user_rank (user_id, `rank`),
  INDEX idx_rec_movie (movie_id)
);
```

**设计要点：**
- **只保存系统用户推荐**：不保存 MovieLens 用户（`user_id < 0`）的推荐
- **Top-N 推荐**：每个用户保存 Top-N（如 20）条推荐
- **rank 字段**：表示推荐排名，`1` 为最推荐，`2` 为次之，依此类推

**数据来源：**
```python
# Spark 训练脚本生成推荐
user_recs = model.recommendForAllUsers(20)  # 为每个用户生成 20 条推荐

# 过滤只保留系统用户（user_id > 0）
system_user_recs = user_recs.filter(F.col("userId") > 0)

# 写入 MySQL
system_user_recs.write.jdbc(
    url=jdbc_url, table="recommendations", mode="append"
)
```

**推荐策略：**

| 场景 | 策略 | 数据来源 |
|------|------|----------|
| 用户有足够评分（≥5 条） | 在线推荐优先 | 实时计算用户向量 + 电影因子 |
| 用户无评分或评分很少 | 批量推荐降级 | 读取 `recommendations` 表 |
| 在线推荐失败 | 批量推荐兜底 | 读取 `recommendations` 表 |

**数据更新：**
- 每次训练生成新模型时，写入新版本的批量推荐
- 旧版本推荐可保留或清理（根据 `model_version` 的状态）
- 建议保留最近 2-3 个版本的推荐

**查询场景：**
1. 前端请求推荐（降级方案）：
   ```sql
   SELECT r.movie_id, r.predicted_rating, m.title, m.genres 
   FROM recommendations r
   JOIN movies m ON r.movie_id = m.movie_id
   WHERE r.user_id = ? 
   ORDER BY r.rank 
   LIMIT 20;
   ```
2. 按模型版本查询推荐：
   ```sql
   SELECT * FROM recommendations 
   WHERE model_version = ? AND user_id = ? 
   ORDER BY rank;
   ```
3. 统计推荐覆盖率：
   ```sql
   SELECT COUNT(DISTINCT user_id) AS covered_users 
   FROM recommendations 
   WHERE model_version = ?;
   ```

---

### 3.3 执行建表

将项目文件传输到 Master 节点：
```bash
# 在开发机上
scp -r /path/to/Project hadoop@hadoop-master:~/movie-recommend/
```

在 Master 节点执行：
```bash
mysql -u root -p < ~/movie-recommend/sql/schema.sql
```

验证：
```bash
mysql -u root -p
```

```sql
USE movie_recommend;
SHOW TABLES;
-- 应该看到：users, movies, user_ratings, model_versions, model_item_factors, recommendations

DESCRIBE user_ratings;
```

---

## 四、数据预处理实现

### 4.1 准备 MovieLens 数据

#### 4.1.1 下载数据集

访问 [MovieLens-32M](https://grouplens.org/datasets/movielens/32m/)，下载并解压：
- `ratings.csv`：3300 万条评分记录
- `movies.csv`：8.6 万部电影元数据

#### 4.1.2 上传到 HDFS

在 Master 节点：
```bash
# 创建 HDFS 目录
hdfs dfs -mkdir -p /movielens

# 上传文件（假设已传输到 Master 的 ~/data/raw/）
hdfs dfs -put ~/data/raw/ratings.csv /movielens/
hdfs dfs -put ~/data/raw/movies.csv /movielens/

# 验证
hdfs dfs -ls /movielens/
hdfs dfs -du -h /movielens/
```

### 4.2 电影元数据预处理

脚本：[`spark/preprocess.py`](../spark/preprocess.py)

**核心功能：**
1. 从 HDFS 读取 `movies.csv`
2. 解析电影标题中的年份（如 `Toy Story (1995)` → `year=1995`）
3. 去除无效行，按 `movie_id` 去重
4. 通过 JDBC 写入 MySQL `movies` 表

**关键代码片段：**
```python
from pyspark.sql import functions as F

# 读取 HDFS
movies_raw = spark.read.csv(hdfs_cfg["movies_path"], header=True, inferSchema=True)

# 解析年份
movies = movies_raw.withColumn(
    "year",
    F.regexp_extract(F.col("title"), r"\((\d{4})\)", 1).cast("int")
).select(
    F.col("movieId").cast("int").alias("movie_id"),
    F.col("title"),
    F.col("genres"),
    F.col("year")
).na.drop(subset=["movie_id", "title"])

# 去重
movies = movies.dropDuplicates(["movie_id"])

# 写入 MySQL
movies.write.jdbc(
    url=jdbc_url,
    table="movies",
    mode="overwrite",
    properties={"user": mysql_user, "password": mysql_password}
)
```

**执行命令：**
```bash
cd ~/movie-recommend
spark-submit \
  --master local[*] \
  --driver-memory 4g \
  spark/preprocess.py
```

**验证：**
```sql
SELECT COUNT(*) FROM movies;
-- 应该接近 86000

SELECT * FROM movies WHERE year = 1995 LIMIT 5;
```

---

## 五、离线 ALS 训练实现

脚本：[`spark/train_als.py`](../spark/train_als.py)

### 5.1 训练流程

```
1. 读取 MovieLens 评分（HDFS）+ 将用户 ID 取负
2. 读取系统用户评分（MySQL user_ratings）
3. 验证用户 ID 命名空间（系统用户 ID 为正）
4. MovieLens 数据按 8:2 划分训练集和测试集
5. 系统用户评分全部加入训练集
6. 训练 ALS 模型
7. 用 MovieLens 测试集计算 RMSE
8. 保存电影隐因子到 model_item_factors
9. 生成全量用户推荐，只保留系统用户
10. 发布模型（状态改为 active）
```

### 5.2 用户 ID 命名空间验证

```python
from spark.utils.user_id_namespace import validate_user_id_namespace

max_system_user_id = 150  # 从 MySQL 查询
max_ml_user_id = 162541   # 从 HDFS 统计

validate_user_id_namespace(
    max_system_user_id=max_system_user_id,
    max_movielens_user_id=max_ml_user_id
)
# 验证：max_system_user_id >= 0（系统用户 ID 必须为正）
# MovieLens 用户 ID 取负后范围：[-162541, -1]
# 系统用户 ID 范围：[1, ∞)，两者不冲突
```

### 5.3 数据合并

```python
# MovieLens 评分取负
ratings_ml = ratings_ml_raw.select(
    (-F.col("ml_user_id")).cast("int").alias("userId"),
    F.col("movieId"),
    F.col("rating")
)

# 系统用户评分（只保留真实注册用户）
ratings_system = (
    read_df_from_mysql(spark, "user_ratings", ...)
    .join(registered_users, "userId", "left_semi")
)

# 合并训练集
ml_train, ml_test = ratings_ml.randomSplit([0.8, 0.2], seed=42)
training_ratings = ml_train.union(ratings_system).cache()
```

### 5.4 ALS 模型训练

```python
from pyspark.ml.recommendation import ALS

als = ALS(
    rank=50,                    # 隐因子维度
    maxIter=10,                 # 迭代次数
    regParam=0.1,               # 正则化系数
    userCol="userId",
    itemCol="movieId",
    ratingCol="rating",
    coldStartStrategy="drop",   # 冷启动处理
    nonnegative=True,           # 非负约束
    implicitPrefs=False         # 显式反馈
)

model = als.fit(training_ratings)

# 评估
predictions = model.transform(ml_test)
evaluator = RegressionEvaluator(metricName="rmse", labelCol="rating", predictionCol="prediction")
rmse = evaluator.evaluate(predictions)
print(f"RMSE: {rmse:.4f}")  # 目标 < 0.85
```

### 5.5 保存模型因子

```python
model_version = "als-20260909T120000Z-abc123"

# 创建模型版本记录
create_model_version(mysql_cfg, model_version, rank=50, reg_param=0.1)

# 提取电影隐因子
item_factors = model.itemFactors.select(
    F.lit(model_version).alias("model_version"),
    F.col("id").cast("int").alias("movie_id"),
    F.to_json(F.col("features")).alias("factors")  # 转为 JSON 字符串
)

# 写入 MySQL
write_df_to_mysql(item_factors, "model_item_factors", jdbc_url, ...)
```

### 5.6 生成推荐并过滤

```python
# 为所有用户生成 Top-20 推荐
user_recs = model.recommendForAllUsers(20)

# 展开推荐列表
recs_flat = user_recs.select(
    F.col("userId").alias("user_id"),
    F.posexplode(F.col("recommendations")).alias("pos", "rec")
).select(
    "user_id",
    F.col("rec.movieId").cast("int").alias("movie_id"),
    F.col("rec.rating").cast("float").alias("predicted_rating"),
    (F.col("pos") + 1).cast("smallint").alias("rank")
)

# 只保留系统用户的推荐
recs_flat = recs_flat.join(
    registered_users.withColumnRenamed("id", "user_id"),
    "user_id",
    "left_semi"
).withColumn("model_version", F.lit(model_version))

# 写入 MySQL
write_df_to_mysql(recs_flat, "recommendations", jdbc_url, ...)
```

### 5.7 发布模型

```python
# 将模型状态从 building 改为 active
activate_model_version(mysql_cfg, model_version)
# 同时将旧的 active 模型改为 retired
```

### 5.8 执行训练

```bash
cd ~/movie-recommend

spark-submit \
  --master local[*] \
  --driver-memory 8g \
  --executor-memory 4g \
  --conf spark.sql.shuffle.partitions=200 \
  spark/train_als.py
```

**预期输出：**
```
Reading MovieLens ratings from hdfs://hadoop-master:9000/movielens/ratings.csv ...
MovieLens ratings: 33,000,000; user offset: 10,000,000
Application-user ratings: 120
Training ratings: 26,400,120; MovieLens test ratings: 6,599,880
MovieLens test RMSE: 0.8234
Published model als-20260909T120000Z-abc123: 
  training=26,400,120, MovieLens=33,000,000, system=120, 
  RMSE=0.8234, recommendations=2,400
```

**验证：**
```sql
SELECT version, status, `rank`, reg_param, generated_at 
FROM model_versions 
ORDER BY generated_at DESC 
LIMIT 1;

SELECT COUNT(*) FROM model_item_factors WHERE model_version = 'als-20260909T120000Z-abc123';
-- 应该接近电影总数

SELECT user_id, movie_id, predicted_rating, `rank`
FROM recommendations
WHERE model_version = 'als-20260909T120000Z-abc123' AND user_id = 1
ORDER BY `rank`
LIMIT 5;
```

---

## 六、在线推荐服务实现

脚本：[`backend/recommendation_core.py`](../backend/recommendation_core.py)、[`backend/recommendation_service.py`](../backend/recommendation_service.py)

### 6.1 核心算法原理

**问题：** 用户新增评分后，如何立即生成更新的推荐，而不重新训练整个 ALS 模型？

**解决方案：** 固定已训练的电影隐因子矩阵 $Q$，只求解当前用户的隐向量 $p_u$。

#### ALS 简化回顾

协同过滤模型预测评分：
$$\hat{r}_{ui} = p_u^T q_i$$

其中：
- $p_u$ 是用户 $u$ 的隐向量（50 维）
- $q_i$ 是电影 $i$ 的隐向量（50 维）

ALS 训练时交替固定 $P$ 或 $Q$，求解另一个矩阵。在线推荐时，我们已经有了 $Q$（电影因子），只需求解一个用户的 $p_u$。

#### 用户向量求解公式

给定用户 $u$ 的评分集合 $\{(i, r_{ui})\}$，求解：
$$p_u = \arg\min_{p_u \geq 0} \sum_{i \in I_u} (r_{ui} - p_u^T q_i)^2 + \lambda ||p_u||^2$$

这是一个带非负约束的最小二乘问题（NNLS），使用 `scipy.optimize.nnls` 求解。

### 6.2 代码实现

#### 求解用户向量（`recommendation_core.py`）

```python
import numpy as np
from scipy.optimize import nnls

def solve_user_vector(
    item_factors: np.ndarray,  # (n_items, rank)
    ratings: np.ndarray,       # (n_items,)
    reg_param: float,
    nonnegative: bool = True
) -> np.ndarray:
    """固定电影因子，求解用户向量"""
    rank = item_factors.shape[1]
    
    # 增广矩阵，加入正则化项
    regularizer = np.sqrt(reg_param) * np.eye(rank)
    augmented_factors = np.vstack((item_factors, regularizer))
    augmented_ratings = np.concatenate((ratings, np.zeros(rank)))
    
    # 非负最小二乘
    if nonnegative:
        vector, _ = nnls(augmented_factors, augmented_ratings)
        return vector
    
    # 普通最小二乘
    lhs = item_factors.T @ item_factors + reg_param * np.eye(rank)
    rhs = item_factors.T @ ratings
    return np.linalg.solve(lhs, rhs)
```

#### 排序未评分电影

```python
def rank_unrated_items(
    movie_ids: Iterable[int],
    item_factors: np.ndarray,
    user_vector: np.ndarray,
    rated_movie_ids: set[int],
    top_n: int
) -> list[tuple[int, float]]:
    """计算预测分，过滤已评分，返回 Top-N"""
    scores = item_factors @ user_vector  # (n_items,)
    
    candidates = [
        (int(movie_id), float(score))
        for movie_id, score in zip(movie_ids, scores)
        if int(movie_id) not in rated_movie_ids
    ]
    
    candidates.sort(key=lambda x: (-x[1], x[0]))  # 按分数降序，ID 升序
    return candidates[:top_n]
```

### 6.3 服务层实现（`recommendation_service.py`）

```python
class RecommendationService:
    def __init__(self, db, config):
        self.db = db
        self.top_n = config["als"]["top_n"]
        self.reg_param = config["als"]["reg_param"]
        self._cached_model_version = None
        self._cached_factors = None  # {movie_id: np.array}
    
    def get_recommendations(self, user_id: int) -> list[dict]:
        """获取用户推荐（在线计算优先，回退批量推荐）"""
        # 1. 获取用户评分
        user_ratings = self.db.session.query(UserRating).filter_by(user_id=user_id).all()
        
        if not user_ratings:
            # 新用户，返回批量推荐
            return self._get_batch_recommendations(user_id)
        
        # 2. 加载当前 active 模型的电影因子
        active_version = self._get_active_model_version()
        if not active_version:
            return []
        
        try:
            factors_map = self._load_item_factors(active_version)
            
            # 3. 准备用户评分数据
            rated_movie_ids = {r.movie_id for r in user_ratings}
            rated_items = [
                (r.movie_id, r.rating) 
                for r in user_ratings 
                if r.movie_id in factors_map
            ]
            
            if not rated_items:
                return self._get_batch_recommendations(user_id)
            
            movie_ids, ratings = zip(*rated_items)
            item_factors = np.array([factors_map[mid] for mid in movie_ids])
            ratings_array = np.array(ratings)
            
            # 4. 求解用户向量
            user_vector = solve_user_vector(
                item_factors, ratings_array, self.reg_param, nonnegative=True
            )
            
            # 5. 排序未评分电影
            all_movie_ids = list(factors_map.keys())
            all_factors = np.array([factors_map[mid] for mid in all_movie_ids])
            
            recommendations = rank_unrated_items(
                all_movie_ids, all_factors, user_vector, rated_movie_ids, self.top_n
            )
            
            # 6. 查询电影详情
            return self._enrich_with_movie_details(recommendations)
        
        except Exception as e:
            print(f"Online recommendation failed: {e}")
            return self._get_batch_recommendations(user_id)
    
    def _load_item_factors(self, model_version: str) -> dict:
        """加载电影因子，带缓存"""
        if self._cached_model_version == model_version:
            return self._cached_factors
        
        factors = self.db.session.query(ModelItemFactor).filter_by(
            model_version=model_version
        ).all()
        
        self._cached_factors = {
            f.movie_id: np.array(json.loads(f.factors))
            for f in factors
        }
        self._cached_model_version = model_version
        return self._cached_factors
```

### 6.4 降级策略

在线推荐失败时（因子缺失、计算异常），自动回退到批量推荐：

```python
def _get_batch_recommendations(self, user_id: int) -> list[dict]:
    """从 recommendations 表读取批量推荐"""
    recs = self.db.session.query(Recommendation).filter_by(
        user_id=user_id
    ).order_by(Recommendation.rank).limit(self.top_n).all()
    
    return self._enrich_with_movie_details([
        (r.movie_id, r.predicted_rating) for r in recs
    ])
```

---

## 七、后端 API 实现

### 7.1 技术栈

- **Web 框架**：Flask 3.0
- **ORM**：SQLAlchemy 2.0
- **认证**：Flask-JWT-Extended（JWT 令牌）
- **密码加密**：bcrypt

### 7.2 项目结构

```
backend/
├── app.py                    # Flask 应用入口
├── config.py                 # 加载 config.yaml
├── requirements.txt
├── recommendation_core.py    # 在线推荐算法
├── recommendation_service.py # 推荐服务封装
└── app/
    ├── __init__.py          # 创建 Flask app
    ├── db.py                # SQLAlchemy 初始化
    ├── models.py            # ORM 模型
    └── routes/
        ├── auth.py          # 注册/登录
        ├── movies.py        # 电影查询
        ├── ratings.py       # 评分提交
        └── recommendations.py  # 推荐接口
```

### 7.3 核心实现

#### 应用初始化（`app/__init__.py`）

```python
from flask import Flask
from flask_jwt_extended import JWTManager
from flask_cors import CORS

def create_app(config):
    app = Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = f"mysql+pymysql://{config['mysql']['user']}:{config['mysql']['password']}@{config['mysql']['host']}/{config['mysql']['database']}"
    app.config['JWT_SECRET_KEY'] = config['flask']['secret_key']
    
    CORS(app)
    JWTManager(app)
    
    from app.db import db
    db.init_app(app)
    
    # 注册路由
    from app.routes import auth, movies, ratings, recommendations
    app.register_blueprint(auth.bp, url_prefix='/api/auth')
    app.register_blueprint(movies.bp, url_prefix='/api/movies')
    app.register_blueprint(ratings.bp, url_prefix='/api/ratings')
    app.register_blueprint(recommendations.bp, url_prefix='/api/recommendations')
    
    return app
```

#### ORM 模型（`app/models.py`）

```python
from app.db import db
from werkzeug.security import generate_password_hash, check_password_hash

class User(db.Model):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    
    def set_password(self, password):
        self.password_hash = generate_password_hash(password)
    
    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

class Movie(db.Model):
    __tablename__ = 'movies'
    movie_id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    genres = db.Column(db.String(500))
    year = db.Column(db.SmallInteger)

class UserRating(db.Model):
    __tablename__ = 'user_ratings'
    id = db.Column(db.BigInteger, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    movie_id = db.Column(db.Integer, nullable=False)
    rating = db.Column(db.Float, nullable=False)
    rated_at = db.Column(db.DateTime, server_default=db.func.now())
    
    __table_args__ = (db.UniqueConstraint('user_id', 'movie_id', name='uk_user_movie'),)
```

#### 认证路由（`app/routes/auth.py`）

```python
from flask import Blueprint, request, jsonify
from flask_jwt_extended import create_access_token
from app.models import User
from app.db import db

bp = Blueprint('auth', __name__)

@bp.route('/register', methods=['POST'])
def register():
    data = request.json
    username = data.get('username')
    password = data.get('password')
    
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username already exists"}), 400
    
    user = User(username=username)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    token = create_access_token(identity=user.id)
    return jsonify({"token": token, "username": username})

@bp.route('/login', methods=['POST'])
def login():
    data = request.json
    user = User.query.filter_by(username=data.get('username')).first()
    
    if not user or not user.check_password(data.get('password')):
        return jsonify({"error": "Invalid credentials"}), 401
    
    token = create_access_token(identity=user.id)
    return jsonify({"token": token, "username": user.username})
```

#### 评分路由（`app/routes/ratings.py`）

```python
from flask import Blueprint, request, jsonify
from flask_jwt_extended import jwt_required, get_jwt_identity
from app.models import UserRating
from app.db import db

bp = Blueprint('ratings', __name__)

@bp.route('/', methods=['POST'])
@jwt_required()
def submit_rating():
    user_id = get_jwt_identity()
    data = request.json
    movie_id = data.get('movie_id')
    rating = data.get('rating')
    
    if not (0.5 <= rating <= 5.0):
        return jsonify({"error": "Rating must be between 0.5 and 5.0"}), 400
    
    # 更新或插入
    existing = UserRating.query.filter_by(user_id=user_id, movie_id=movie_id).first()
    if existing:
        existing.rating = rating
        existing.rated_at = db.func.now()
    else:
        new_rating = UserRating(user_id=user_id, movie_id=movie_id, rating=rating)
        db.session.add(new_rating)
    
    db.session.commit()
    return jsonify({"message": "Rating submitted"})

@bp.route('/me', methods=['GET'])
@jwt_required()
def get_my_ratings():
    user_id = get_jwt_identity()
    ratings = UserRating.query.filter_by(user_id=user_id).order_by(
        UserRating.rated_at.desc()
    ).all()
    
    return jsonify([{
        "movie_id": r.movie_id,
        "rating": r.rating,
        "rated_at": r.rated_at.isoformat()
    } for r in ratings])
```

#### 推荐路由（`app/routes/recommendations.py`）

```python
from flask import Blueprint, jsonify, current_app
from flask_jwt_extended import jwt_required, get_jwt_identity

bp = Blueprint('recommendations', __name__)

@bp.route('/', methods=['GET'])
@jwt_required()
def get_recommendations():
    user_id = get_jwt_identity()
    service = current_app.recommendation_service
    
    recommendations = service.get_recommendations(user_id)
    return jsonify(recommendations)
```

### 7.4 启动后端

```bash
cd ~/movie-recommend/backend
source venv/bin/activate
pip install -r requirements.txt

python app.py
```

访问：`http://192.168.100.10:5000`

---

## 八、前端实现

### 8.1 技术栈

- **框架**：Vue 3（Composition API）
- **构建工具**：Vite 5
- **UI 组件库**：Element Plus
- **状态管理**：Pinia
- **路由**：Vue Router
- **HTTP 客户端**：Axios

### 8.2 项目结构

```
frontend/
├── package.json
├── vite.config.js
├── index.html
└── src/
    ├── main.js
    ├── App.vue
    ├── router/index.js
    ├── stores/
    │   └── auth.js       # 认证状态
    ├── api/
    │   └── index.js      # Axios 封装
    ├── views/
    │   ├── LoginView.vue
    │   ├── HomeView.vue
    │   ├── MovieDetailView.vue
    │   └── RecommendationsView.vue
    └── components/
        ├── MovieCard.vue
        └── RatingStars.vue
```

### 8.3 核心实现

#### Axios 封装（`src/api/index.js`）

```javascript
import axios from 'axios'
import { ElMessage } from 'element-plus'
import router from '@/router'

const api = axios.create({
  baseURL: '/api',  // Vite 会代理到 http://localhost:5000
  timeout: 10000
})

// 请求拦截器：附加 JWT
api.interceptors.request.use(config => {
  const token = localStorage.getItem('token')
  if (token) {
    config.headers.Authorization = `Bearer ${token}`
  }
  return config
})

// 响应拦截器：处理 401
api.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401) {
      localStorage.removeItem('token')
      router.push('/login')
      ElMessage.error('Please login')
    }
    return Promise.reject(error)
  }
)

export default {
  auth: {
    login: (username, password) => api.post('/auth/login', { username, password }),
    register: (username, password) => api.post('/auth/register', { username, password })
  },
  movies: {
    list: (params) => api.get('/movies', { params }),
    detail: (id) => api.get(`/movies/${id}`)
  },
  ratings: {
    submit: (movie_id, rating) => api.post('/ratings', { movie_id, rating }),
    myRatings: () => api.get('/ratings/me')
  },
  recommendations: {
    get: () => api.get('/recommendations')
  }
}
```

#### 认证 Store（`src/stores/auth.js`）

```javascript
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || null)
  const username = ref(localStorage.getItem('username') || null)
  
  const isAuthenticated = computed(() => !!token.value)
  
  function setAuth(newToken, newUsername) {
    token.value = newToken
    username.value = newUsername
    localStorage.setItem('token', newToken)
    localStorage.setItem('username', newUsername)
  }
  
  function clearAuth() {
    token.value = null
    username.value = null
    localStorage.removeItem('token')
    localStorage.removeItem('username')
  }
  
  return { token, username, isAuthenticated, setAuth, clearAuth }
})
```

#### 电影列表页（`src/views/HomeView.vue`）

```vue
<template>
  <div class="home">
    <el-form :inline="true">
      <el-form-item label="搜索">
        <el-input v-model="keyword" placeholder="电影名称" @keyup.enter="search" />
      </el-form-item>
      <el-form-item label="类型">
        <el-select v-model="genre" placeholder="全部" clearable @change="search">
          <el-option v-for="g in genres" :key="g" :label="g" :value="g" />
        </el-select>
      </el-form-item>
      <el-form-item label="年份">
        <el-input-number v-model="year" :min="1900" :max="2024" @change="search" />
      </el-form-item>
      <el-button type="primary" @click="search">搜索</el-button>
    </el-form>
    
    <el-row :gutter="20">
      <el-col v-for="movie in movies" :key="movie.movie_id" :xs="24" :sm="12" :md="8" :lg="6">
        <MovieCard :movie="movie" @rate="handleRate" />
      </el-col>
    </el-row>
    
    <el-pagination
      v-model:current-page="page"
      :page-size="pageSize"
      :total="total"
      @current-change="loadMovies"
    />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import api from '@/api'
import MovieCard from '@/components/MovieCard.vue'

const movies = ref([])
const page = ref(1)
const pageSize = 20
const total = ref(0)
const keyword = ref('')
const genre = ref('')
const year = ref(null)
const genres = ref([])

async function loadMovies() {
  const params = {
    page: page.value,
    page_size: pageSize,
    keyword: keyword.value,
    genre: genre.value,
    year: year.value
  }
  const { data } = await api.movies.list(params)
  movies.value = data.movies
  total.value = data.total
}

async function loadGenres() {
  const { data } = await api.get('/movies/genres')
  genres.value = data
}

function search() {
  page.value = 1
  loadMovies()
}

async function handleRate(movieId, rating) {
  await api.ratings.submit(movieId, rating)
  ElMessage.success('Rating submitted')
}

onMounted(() => {
  loadMovies()
  loadGenres()
})
</script>
```

#### 推荐页（`src/views/RecommendationsView.vue`）

```vue
<template>
  <div class="recommendations">
    <h2>我的个性化推荐</h2>
    
    <el-empty v-if="recommendations.length === 0" description="暂无推荐，请先评分几部电影" />
    
    <el-row v-else :gutter="20">
      <el-col v-for="(rec, index) in recommendations" :key="rec.movie_id" :xs="24" :sm="12" :md="8" :lg="6">
        <el-card shadow="hover">
          <div class="rank-badge">{{ index + 1 }}</div>
          <h3>{{ rec.title }}</h3>
          <p>预测评分: {{ rec.predicted_rating.toFixed(2) }}</p>
          <p>类型: {{ rec.genres }}</p>
          <el-button @click="viewDetail(rec.movie_id)">查看详情</el-button>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import api from '@/api'

const router = useRouter()
const recommendations = ref([])

async function loadRecommendations() {
  const { data } = await api.recommendations.get()
  recommendations.value = data
}

function viewDetail(movieId) {
  router.push(`/movie/${movieId}`)
}

onMounted(loadRecommendations)
</script>

<style scoped>
.rank-badge {
  position: absolute;
  top: 10px;
  right: 10px;
  background: #409EFF;
  color: white;
  width: 30px;
  height: 30px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-weight: bold;
}
</style>
```

### 8.4 Vite 配置（`vite.config.js`）

```javascript
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import path from 'path'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src')
    }
  },
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://192.168.100.10:5000',
        changeOrigin: true
      }
    }
  }
})
```

### 8.5 启动前端

```bash
cd ~/movie-recommend/frontend
npm install
npm run dev
```

访问：`http://localhost:5173`

---

## 九、运行与验证

### 9.1 启动顺序

#### 在 Master 节点

1. **启动 Hadoop**
   ```bash
   start-dfs.sh
   start-yarn.sh
   jps  # 确认 NameNode、ResourceManager 运行
   ```

2. **启动 MySQL**
   ```bash
   sudo systemctl start mysql
   ```

3. **启动后端**
   ```bash
   cd ~/movie-recommend/backend
   source venv/bin/activate
   python app.py
   ```

#### 在开发机或 Master

4. **启动前端**
   ```bash
   cd ~/movie-recommend/frontend
   npm run dev
   ```

### 9.2 功能验证清单

#### ✅ 数据预处理
```bash
# HDFS 数据已上传
hdfs dfs -ls /movielens/

# 电影元数据已导入
mysql -u root -p -e "SELECT COUNT(*) FROM movie_recommend.movies;"
```

#### ✅ 模型训练
```sql
-- 查看最新模型
SELECT version, status, `rank`, reg_param FROM model_versions ORDER BY generated_at DESC LIMIT 1;

-- 电影因子数量
SELECT COUNT(*) FROM model_item_factors WHERE model_version = '<版本号>';

-- 推荐数量
SELECT COUNT(DISTINCT user_id) FROM recommendations WHERE model_version = '<版本号>';
```

#### ✅ 用户注册登录
1. 访问 `http://localhost:5173`
2. 点击"注册" → 输入用户名密码 → 提交
3. 自动跳转到首页，右上角显示用户名

#### ✅ 电影浏览
1. 首页显示电影列表，带分页
2. 搜索框输入"Toy Story" → 点击搜索 → 显示相关电影
3. 类型筛选：选择"Action" → 只显示动作片

#### ✅ 评分提交
1. 点击某部电影的"评分"按钮
2. 选择星级（如 4.5 星）→ 提交
3. 提示"评分成功"

#### ✅ 推荐展示
1. 点击顶部菜单"我的推荐"
2. 如果已评分：显示个性化推荐列表（Top 20）
3. 如果未评分：显示空状态提示

#### ✅ 在线推荐更新
1. 评分 3 部电影后，访问推荐页 → 记录前 5 个推荐
2. 再评分 2 部新电影（给高分）
3. 刷新推荐页 → 推荐列表应该变化（高分电影类似的作品排名上升）

### 9.3 常见问题排查

#### 问题 1：前端显示"Total 0"
**原因**：后端未启动或数据库连接失败

**排查步骤：**
```bash
# 检查后端是否监听 5000 端口
netstat -tuln | grep 5000

# 检查 MySQL 连接
mysql -u movie_app -p movie_recommend -e "SELECT COUNT(*) FROM movies;"

# 查看后端日志
# 终端应该有 Flask 日志输出
```

#### 问题 2：推荐页显示空
**原因**：没有 active 模型或该用户没有批量推荐

**解决方案：**
```sql
-- 检查是否有 active 模型
SELECT * FROM model_versions WHERE status = 'active';

-- 如果没有，重新运行训练
cd ~/movie-recommend
spark-submit --master local[*] spark/train_als.py
```

#### 问题 3：评分后推荐不变
**原因**：在线推荐计算失败，回退到了批量推荐

**排查：**
```python
# 后端日志应该有 "Online recommendation failed: ..." 信息
# 检查 model_item_factors 表是否有当前 active 模型的数据
```

#### 问题 4：Spark 作业失败
**可能原因：**
- HDFS 路径不存在
- MySQL 连接失败
- 内存不足

**解决方案：**
```bash
# 查看 Spark 日志
ls ~/movie-recommend/spark-logs/

# 增加内存
spark-submit --driver-memory 8g --executor-memory 4g ...

# 验证 MySQL 从 Spark 可达
spark-shell
scala> val df = spark.read.jdbc("jdbc:mysql://hadoop-master:3306/movie_recommend", "movies", ...)
```

---

## 十、关键技术点总结

### 10.1 两阶段推荐架构的优势

| 维度 | 离线训练 | 在线推荐 |
|------|----------|----------|
| **延迟** | 数小时 | <100ms |
| **数据新鲜度** | 周期性更新 | 实时反馈 |
| **计算资源** | Spark 集群 | 单机 Flask |
| **覆盖范围** | 全量用户 | 单个用户 |

**好处：**
1. **用户体验好**：评分后立即看到更新的推荐，无需等待下次训练
2. **资源高效**：不用为每次评分启动 Spark 作业
3. **模型稳定**：全量训练保证电影因子质量，在线计算不会产生偏差

### 10.2 用户 ID 隔离设计的必要性

**不隔离的问题：**
- MovieLens 用户 ID 可能与系统用户 ID 冲突
- 推荐结果会混入 MovieLens 用户的推荐（无意义）
- 无法区分评分来源

**隔离方案的优点：**
1. **命名空间清晰**：系统用户 [1, 1000万)，MovieLens 用户 [1000万, 1016万)
2. **数据纯净**：只为系统用户生成推荐，不污染应用数据
3. **可扩展**：未来可以接入其他数据源（如豆瓣），分配新的偏移量

### 10.3 模型版本管理的作用

**为什么需要版本管理？**
- 训练是耗时操作，需要记录每次训练的参数和结果
- 在线推荐需要知道当前使用哪个模型的因子
- 模型切换时，旧模型的推荐和因子不能立即删除（可能需要回滚）

**状态流转：**
```
building → active → retired
```

- **building**：训练中，因子和推荐正在写入，不对外服务
- **active**：训练完成，在线推荐使用该版本的因子，批量推荐来自该版本
- **retired**：被新模型替换，保留一段时间后可清理

### 10.4 降级策略的重要性

在线推荐可能因以下原因失败：
- 电影因子缺失（新上映的电影）
- 用户评分的电影因子全部缺失
- 数值计算异常（如评分全为 0）

**降级方案：**
1. 优先使用在线计算
2. 失败时回退到批量推荐
3. 批量推荐也没有时返回空列表

**好处：** 保证服务可用性，不会因为个别异常导致整个推荐接口报错。

### 10.5 虚拟机集群的学习价值

通过搭建虚拟机集群，可以深入理解：
1. **分布式存储**：HDFS 的 NameNode/DataNode 架构，数据块复制机制
2. **资源调度**：YARN 的 ResourceManager/NodeManager，容器分配
3. **网络通信**：节点间如何通过主机名互相访问，SSH 免密的作用
4. **配置管理**：统一配置文件在多节点间的同步
5. **故障模拟**：关闭一个 DataNode，HDFS 如何自动恢复副本

这些知识在云上部署大数据平台时同样适用。

---

## 附录

### A. 配置文件示例

完整配置见 [`config.example.yaml`](../config.example.yaml)，关键参数说明：

```yaml
hdfs:
  namenode: hdfs://hadoop-master:9000
  ratings_path: /movielens/ratings.csv
  movies_path: /movielens/movies.csv

mysql:
  host: hadoop-master
  port: 3306
  user: movie_app
  password: your_password
  database: movie_recommend

als:
  rank: 50                  # 隐因子维度
  max_iter: 10              # ALS 迭代次数
  reg_param: 0.1            # 正则化系数
  train_ratio: 0.8          # 训练集比例
  top_n: 20                 # 推荐数量
  ml_user_offset: 10000000  # MovieLens 用户 ID 偏移量

flask:
  host: 0.0.0.0
  port: 5000
  debug: false
  secret_key: random_secret_key_here
```

### B. 依赖清单

**Python（`backend/requirements.txt`）**
```
Flask==3.0.0
Flask-JWT-Extended==4.6.0
Flask-CORS==4.0.0
Flask-SQLAlchemy==3.1.1
PyMySQL==1.1.0
bcrypt==4.1.2
numpy==1.26.3
scipy==1.12.0
pyspark==3.5.0
PyYAML==6.0.1
```

**Node.js（`frontend/package.json`）**
```json
{
  "dependencies": {
    "vue": "^3.4.0",
    "vue-router": "^4.2.5",
    "pinia": "^2.1.7",
    "element-plus": "^2.5.0",
    "axios": "^1.6.5"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.0",
    "vite": "^5.0.0"
  }
}
```

### C. 参考资源

- [Hadoop 官方文档](https://hadoop.apache.org/docs/stable/)
- [Spark MLlib ALS](https://spark.apache.org/docs/latest/ml-collaborative-filtering.html)
- [MovieLens 数据集](https://grouplens.org/datasets/movielens/)
- [Flask 文档](https://flask.palletsprojects.com/)
- [Vue 3 文档](https://cn.vuejs.org/)

---

**文档版本：** v1.0  
**更新时间：** 2026-09-09  
**作者：** [你的名字]
