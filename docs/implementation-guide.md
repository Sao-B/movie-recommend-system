# 基于 Hadoop 生态的个性化电影推荐系统实现指南

> 本文档面向需要快速理解项目实现流程的读者，按实际开发顺序讲解关键步骤和技术要点。
> 文中所有代码片段均摘自项目源码（2026-09-14 核对），与实现一一对应。

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
- [附录](#附录)

---

## 一、项目概览

### 1.1 系统架构

本项目构建了一套完整的电影推荐系统，采用**离线训练 + 在线推荐**的两阶段架构：

```
[离线阶段]
MovieLens 32M 评分数据 (HDFS)          系统用户评分 (MySQL user_ratings)
              └────────────┬────────────┘
                           ↓
                    Spark ALS 训练
                           ↓
          电影隐因子 + 系统用户批量推荐
                           ↓
                      MySQL 存储

[在线阶段]
用户新增评分 → 固定电影因子求解用户向量 → 实时返回 Top-N
```

**核心设计思想：**
- **离线**：定期用全量数据训练非负显式反馈 ALS 模型，生成所有系统用户的批量推荐
- **在线**：用户提交新评分后，固定已训练的电影因子，用 NNLS 只求解当前用户向量，立即返回更新后的推荐
- **降级**：无评分的新用户使用离线批量推荐；在线计算失败时同样回退批量推荐

### 1.2 技术栈

| 层次 | 技术 | 作用 |
|------|------|------|
| 分布式存储 | Hadoop HDFS | 存储 MovieLens 原始评分与电影元数据 |
| 离线计算 | PySpark 3.5 + MLlib ALS | 协同过滤模型训练 |
| 关系数据库 | MySQL 8.0 | 存储用户、电影、评分、模型因子、推荐结果、评论 |
| 后端服务 | Flask 3 + SQLAlchemy 2 + JWT | RESTful API，认证、评分、评论、推荐、管理接口 |
| 前端界面 | Vue 3 + Vite + Element Plus + Pinia | 单页应用，电影浏览、评分、评论、推荐、管理后台 |

### 1.3 数据流向

```
MovieLens CSV 文件
    ↓ (hdfs dfs -put，scripts/init_hdfs.sh)
HDFS /movielens/
    ↓ (Spark JDBC 读 MySQL、读 HDFS)
数据预处理（movies 表）+ ALS 训练
    ↓ (JDBC append 写入)
MySQL (model_item_factors + recommendations)
    ↓ (SQLAlchemy 只读)
Flask RESTful API
    ↓ (Axios，Vite 代理 /api/*)
Vue 3 前端展示
```

---

## 二、环境搭建

> **说明：** 本项目的虚拟机集群搭建参照尚硅谷大数据教程的经典配置模式。文档按 3 节点完全分布式讲解搭建流程，但本项目实际运行的是**单机模式**（hadoop102），HDFS 副本数为 1——这是资源受限环境下的快速验证方案，生产环境建议按完全分布式部署。

### 2.1 集群规划

#### 2.1.1 为什么选择完全分布式

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
- Flask：8000（本机 Windows 禁止绑定 5000/5001，故统一改用 8000）
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
- **Node.js**：18+

> **本项目实际配置**：单机模式（hadoop102），HDFS 副本数 1。集群搭建流程保留作为论文中的部署方案说明。

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

# 配置 sudo 权限
vi /etc/sudoers
```

在 `root ALL=(ALL) ALL` 下一行添加：
```
h ALL=(ALL) NOPASSWD:ALL
```

**8. 创建工作目录**
```bash
su - h
mkdir /opt/module /opt/software
sudo chown h:h /opt/module /opt/software
```

#### 2.2.3 克隆虚拟机

**1. 创建模板快照**
- 关闭 hadoop102
- VMware → 右键 hadoop102 → 快照 → 拍摄快照 → 命名"模板机"

**2. 克隆 hadoop103**
```
右键 hadoop102 → 管理 → 克隆 → 完整克隆
虚拟机名称：hadoop103
```

启动后修改配置：
```bash
hostnamectl set-hostname hadoop103
vi /etc/sysconfig/network-scripts/ifcfg-ens33   # IPADDR 改为 192.168.10.103
systemctl restart network
```

**3. 同样方式克隆 hadoop104**（IP 改为 192.168.10.104）

#### 2.2.4 配置 SSH 免密登录

在 **hadoop102** 上：

```bash
ssh-keygen -t rsa          # 一路回车
ssh-copy-id hadoop102      # 分发公钥到所有节点（包括自己）
ssh-copy-id hadoop103
ssh-copy-id hadoop104
ssh hadoop103              # 验证免密登录
```

**为什么需要免密登录？**
- Hadoop 启动/停止脚本需要在各节点间执行命令
- HDFS、YARN 的进程间通信需要 SSH
- 集群管理脚本（如分发文件）需要免密

### 2.3 安装基础软件

以下操作在**所有节点**执行。

#### 2.3.1 安装 Java

```bash
# 方式一：yum 安装 OpenJDK
sudo yum install -y java-1.8.0-openjdk-devel

# 方式二：解压官方 JDK 到 /opt/module/jdk1.8.0_291
echo 'export JAVA_HOME=/opt/module/jdk1.8.0_291' >> ~/.bashrc
echo 'export PATH=$JAVA_HOME/bin:$PATH' >> ~/.bashrc
source ~/.bashrc

java -version
```

#### 2.3.2 安装 Hadoop（3.3.6）

在 **hadoop102** 上：

```bash
cd /opt/software
wget https://dlcdn.apache.org/hadoop/common/hadoop-3.3.6/hadoop-3.3.6.tar.gz
tar -xzf hadoop-3.3.6.tar.gz -C /opt/module/

echo 'export HADOOP_HOME=/opt/module/hadoop-3.3.6' >> ~/.bashrc
echo 'export PATH=$HADOOP_HOME/bin:$HADOOP_HOME/sbin:$PATH' >> ~/.bashrc
source ~/.bashrc
```

#### 2.3.3 配置 Hadoop

在 `/opt/module/hadoop-3.3.6/etc/hadoop/` 目录下修改：

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
    <value>1</value>  <!-- 单机模式，副本数为 1；完全分布式改为 2-3 -->
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
hadoop102
```

> **说明**：单机模式 workers 只写 hadoop102（或 localhost）。完全分布式改为 hadoop102/103/104 三行，并将配置分发到各节点。

#### 2.3.4 格式化 HDFS 并启动

在 **hadoop102** 上：

```bash
hdfs namenode -format    # 仅首次执行
start-dfs.sh
start-yarn.sh
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

echo 'export SPARK_HOME=/opt/module/spark-3.5.1-bin-hadoop3' >> ~/.bashrc
echo 'export PATH=$SPARK_HOME/bin:$SPARK_HOME/sbin:$PATH' >> ~/.bashrc
source ~/.bashrc

cd /opt/module/spark-3.5.1-bin-hadoop3/conf
cp spark-env.sh.template spark-env.sh
```

在 `spark-env.sh` 中添加：
```bash
export JAVA_HOME=/opt/module/jdk1.8.0_291
export HADOOP_CONF_DIR=/opt/module/hadoop-3.3.6/etc/hadoop
export SPARK_LOCAL_IP=192.168.10.102
```

**安装 MySQL JDBC 驱动**（Spark 通过 JDBC 读写 MySQL，必须预先提供）：
```bash
cd /opt/module/spark-3.5.1-bin-hadoop3/jars
wget https://repo1.maven.org/maven2/com/mysql/mysql-connector-j/8.0.33/mysql-connector-j-8.0.33.jar
```

验证安装：
```bash
spark-submit --version
```

### 2.5 安装 MySQL（仅 hadoop102）

```bash
wget https://dev.mysql.com/get/mysql80-community-release-el7-3.noarch.rpm
sudo rpm -ivh mysql80-community-release-el7-3.noarch.rpm
sudo yum install -y mysql-server

sudo systemctl start mysqld
sudo systemctl enable mysqld

# 获取临时密码
sudo grep 'temporary password' /var/log/mysqld.log

mysql -u root -p
# 登录后执行：
ALTER USER 'root'@'localhost' IDENTIFIED BY 'YourStrongPassword@123';
CREATE USER 'movie_app'@'%' IDENTIFIED BY 'MovieApp@2024';
GRANT ALL PRIVILEGES ON movie_recommend.* TO 'movie_app'@'%';
FLUSH PRIVILEGES;
EXIT;
```

**允许远程连接**（如需从开发机访问）：
```bash
sudo vi /etc/my.cnf     # [mysqld] 段添加 bind-address=0.0.0.0
sudo systemctl restart mysqld
```

### 2.6 安装 Python 环境（hadoop102）

```bash
# CentOS 7 系统自带 Python 版本较低，源码编译 Python 3.12
sudo yum install -y gcc openssl-devel bzip2-devel libffi-devel zlib-devel
cd /opt/software
wget https://www.python.org/ftp/python/3.12.0/Python-3.12.0.tgz
tar -xzf Python-3.12.0.tgz && cd Python-3.12.0
./configure --enable-optimizations --prefix=/opt/module/python3.12
make -j$(nproc)
sudo make altinstall
```

项目代码（Spark 作业、Flask）从项目根目录 `config.yaml` 读取全部配置，Spark 作业直接 `spark-submit` 运行，无需虚拟环境；Flask 后端建议用虚拟环境（见 7.4）。

### 2.7 Node.js 安装（可在开发机安装）

```bash
cd /opt/software
wget https://nodejs.org/dist/v18.18.0/node-v18.18.0-linux-x64.tar.xz
tar -xf node-v18.18.0-linux-x64.tar.xz -C /opt/module/
echo 'export PATH=/opt/module/node-v18.18.0-linux-x64/bin:$PATH' >> ~/.bashrc
source ~/.bashrc

node -v && npm -v
```

---

## 三、数据库设计与初始化

### 3.1 设计思路

本项目的核心设计决策是**用户 ID 隔离**、**评分表统一**和**模型版本管理**，数据库表结构围绕这三点展开。

#### 3.1.1 用户 ID 命名空间隔离

**问题：** MovieLens-32M 数据集包含 16 万真实用户（ID 1~162541），系统注册用户的 ID 如何避免冲突？

**解决方案（取负隔离）：**
- **系统用户**：MySQL `users` 表的自增 ID，正整数，范围 `[1, ∞)`
- **MovieLens 用户**：训练时原始 `userId` **取负**，负整数，范围约 `[-162541, -1]`

```
训练时的用户 ID 映射：
- 系统用户 123       → ALS 中仍为 123
- MovieLens 用户 456 → ALS 中为 -456
```

**好处：**
1. **永不冲突**：正数与负数天然隔离，无需任何偏移量配置
2. **查询简洁**：`user_id > 0` 即为系统用户
3. **易于理解**：负数直观表示外部数据源
4. **两类用户在同一个 ALS 模型中训练**，共享电影隐因子
5. 推荐结果经半连接过滤后只保留系统用户，不会污染应用数据

> 训练入口 `spark/utils/user_id_namespace.py` 的 `validate_user_id_namespace()` 在训练开始前校验系统用户 ID 为正整数，并打印两个命名空间的范围。

#### 3.1.2 评分表统一（user_ratings 为唯一事实来源）

| 表名 | 作用 | 数据来源 |
|------|------|----------|
| `user_ratings` | **系统用户评分（唯一事实来源）** | 前端提交，训练与在线推荐读取 |
| `ratings` | 遗留表，只读保留 | 历史数据，2026-09-14 起不再被任何代码读写 |
| MovieLens CSV（HDFS） | MovieLens 原始评分 | 离线训练读取 |

> **历史说明**：项目早期应用层误写 legacy `ratings` 表，导致"前端评分进不了训练集"的数据分裂。已通过迁移 `sql/migrations/005_merge_ratings_into_user_ratings.sql` 合并数据，`backend/app/models.py` 的评分模型（`UserRating`）统一指向 `user_ratings`，并由两端契约测试（`tests/test_training_contract.py` 锁训练侧、`tests/test_rating_table_contract.py` 锁应用侧）防止再次分裂。

**为什么 MovieLens 评分不进 MySQL？**
- MovieLens 数据量大（3300 万条），留在 HDFS 由 Spark 直接读取
- 系统用户评分需要实时写入，适合 MySQL
- 训练时通过 `unionByName` 合并两个数据源

#### 3.1.3 模型版本管理

每次训练生成一个新版本（如 `als-20260910T173512Z-1d3ea7bd40b0`），状态流转：

```
building → active → retired
  ↓          ↓         ↓
训练中    当前生效   已废弃
```

**核心表：**
- `model_versions`：版本元数据（rank、reg_param、状态）
- `model_item_factors`：电影隐因子（供在线推荐使用）
- `recommendations`：批量推荐结果（离线生成，带 model_version）

### 3.2 表结构详解

完整 DDL 见 [`sql/schema.sql`](../sql/schema.sql)。以下说明每个表的设计思路、字段含义和使用场景。

---

#### 3.2.1 `users` - 系统注册用户表

**作用：** 存储通过前端注册的应用用户，支持登录认证与管理后台权限控制。

**表结构（关键列）：**
```sql
CREATE TABLE users (
  id            INT PRIMARY KEY AUTO_INCREMENT COMMENT '用户主键，自增正整数',
  username      VARCHAR(50) UNIQUE NOT NULL    COMMENT '用户名，唯一',
  password_hash VARCHAR(255) NOT NULL          COMMENT 'Werkzeug 哈希密码',
  avatar_url    VARCHAR(500) NULL              COMMENT '用户头像 URL（迁移 004 新增）',
  bio           TEXT NULL                      COMMENT '个人签名（迁移 004 新增）',
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '注册时间',
  role          ENUM('user','admin') NOT NULL DEFAULT 'user' COMMENT '角色（迁移 001_add_user_role 新增）'
);
```

**设计要点：**
- **用户 ID 范围**：`id` 为自增正整数，范围 `[1, ∞)`，与取负后的 MovieLens 用户天然隔离
- **密码安全**：`werkzeug.security.generate_password_hash` 加密存储（pbkdf2），不存明文
- **角色字段**：`role` 支持 `admin`，管理后台接口经 `admin_required` 装饰器校验

**关联关系：**
- `user_ratings.user_id` 外键引用 `users.id`（ON DELETE CASCADE）
- `movie_comments.user_id` 外键引用 `users.id`（ON DELETE CASCADE）
- `recommendations.user_id` 对应 `users.id`（无外键约束，批量导入性能考虑）

---

#### 3.2.2 `movies` - 电影元数据表

**作用：** 存储电影基础信息与 TMDB 富化数据（海报、简介、演职员）。

**表结构（关键列）：**
```sql
CREATE TABLE movies (
  movie_id     INT PRIMARY KEY      COMMENT 'MovieLens 原始电影 ID',
  title        VARCHAR(500) NOT NULL COMMENT '标题（含年份），如 "Toy Story (1995)"',
  genres       VARCHAR(500)         COMMENT '竖线分隔，如 "Action|Drama"',
  year         SMALLINT             COMMENT '从 title 正则解析',
  -- MovieLens 统计列（import_movie_stats.py / sync_movie_stats.py 维护）
  avg_rating   DECIMAL(3,2)         COMMENT 'MovieLens 平均评分',
  rating_count INT DEFAULT 0        COMMENT 'MovieLens 评分数',
  -- TMDB 富化列（fetch_posters.py 抓取）
  poster_url   VARCHAR(500), overview TEXT, director VARCHAR(500),
  writers      TEXT, `cast` TEXT, countries VARCHAR(200),
  languages    VARCHAR(200), release_date DATE, runtime SMALLINT
);
```

**数据来源（三层）：**
1. `spark/preprocess.py`：从 HDFS 清洗 `movies.csv` 写入（约 8.7 万部）
2. `import_movie_stats.py` / `sync_movie_stats.py`：从 MovieLens 统计 CSV / 虚拟机同步 `avg_rating`、`rating_count`
3. `fetch_posters.py`：按 TMDB API 逐一回填海报、简介、导演、主演等富化列

**查询场景：**
1. 前端电影列表（按热门度排序，过滤评分人数 < 100 的小样本）：
   ```sql
   SELECT * FROM movies WHERE rating_count >= 100
   ORDER BY avg_rating DESC, rating_count DESC LIMIT 20;
   ```
2. 推荐结果关联电影详情：
   ```sql
   SELECT r.*, m.title, m.poster_url FROM recommendations r
   JOIN movies m ON r.movie_id = m.movie_id
   WHERE r.user_id = ? ORDER BY r.rank LIMIT 20;
   ```

---

#### 3.2.3 `ratings` - 遗留评分表（只读保留）

**作用：** 保留历史评分数据，**2026-09-14 起不再被应用或训练任务读写**。

**处理记录：**
1. 应用评分曾误写此表（详见 3.1.2 历史说明）
2. 数据已通过迁移 005 合并进 `user_ratings`
3. 测试期间产生的系统用户测试数据已清空；表结构保留仅为兼容
4. 如确认无需保留，可执行 `DROP TABLE ratings;`

---

#### 3.2.4 `user_ratings` - 系统用户评分表（唯一事实来源）

**作用：** 存储系统注册用户的评分记录，应用评分、ALS 训练、在线推荐共用。

**表结构：**
```sql
CREATE TABLE user_ratings (
  id        BIGINT PRIMARY KEY AUTO_INCREMENT COMMENT '评分记录主键',
  user_id   INT NOT NULL                      COMMENT '用户 ID，对应 users.id',
  movie_id  INT NOT NULL                      COMMENT '电影 ID，对应 movies.movie_id',
  rating    FLOAT NOT NULL CHECK (rating BETWEEN 0.5 AND 5.0)
            COMMENT '评分值，范围 [0.5, 5.0]',
  rated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '评分时间',
  UNIQUE KEY uk_user_movie (user_id, movie_id)
            COMMENT '同一用户对同一电影只保留最新一条评分',
  INDEX idx_user (user_id),
  INDEX idx_movie (movie_id),
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);
```

**数据流向：**

| 操作 | 方向 | 代码位置 |
|------|------|----------|
| **写入** | 前端 → Flask API → 此表 | `backend/app/routes/ratings.py`（`UserRating` 模型，重复评分更新 `rated_at`） |
| **读取 1** | Flask API ← 此表 | 用户评分历史 / 个人主页统计 |
| **读取 2** | Spark 训练 ← 此表 | `spark/utils/training_contract.py` 的 `build_system_ratings_query()` |
| **读取 3** | Flask 在线推荐 ← 此表 | `backend/app/recommendation_repository.py` 的 `get_user_ratings()` |

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
3. 离线训练读取系统用户评分（Spark JDBC 子查询）：
   ```sql
   (SELECT user_id AS userId, movie_id AS movieId, rating FROM user_ratings) AS t
   ```

---

#### 3.2.5 `model_versions` - ALS 模型版本管理表

**作用：** 管理 ALS 模型的生命周期，记录训练参数和状态。

**表结构：**
```sql
CREATE TABLE model_versions (
  version      VARCHAR(64) PRIMARY KEY
               COMMENT '模型版本号，格式 als-{UTC时间戳}-{12位hex}',
  `rank`       SMALLINT NOT NULL COMMENT 'ALS 隐因子维度，如 50（rank 是 MySQL 保留字，须反引号）',
  reg_param    FLOAT NOT NULL    COMMENT 'ALS 正则化系数，如 0.1',
  status       ENUM('building','active','retired') NOT NULL DEFAULT 'building',
  generated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  activated_at TIMESTAMP NULL,
  INDEX idx_model_status (status, generated_at)
);
```

**版本命名规则**（`spark/train_als.py` 的 `_new_model_version()`）：
- 格式：`als-{YYYYMMDDTHHMMSSZ}-{uuid4().hex[:12]}`
- 示例：`als-20260910T173512Z-1d3ea7bd40b0`

**状态流转与原子性：**

| 状态 | 含义 | 操作（`spark/utils/mysql_utils.py`） |
|------|------|------|
| **building** | 因子和推荐正在写入，不对外服务 | `create_model_version()` 创建 |
| **active** | 训练完成，在线推荐使用该版本的因子 | `activate_model_version()`：事务内先把旧 active 改 retired，再把本版本改 active |
| **retired** | 已替换或训练失败的半成品 | 新模型激活时旧模型自动转此状态；训练异常时 `retire_model_version()` 隐藏半成品 |

**设计要点：**
- **同时只能有一个 active 模型**；激活失败（目标不是 building 状态）会抛异常回滚
- **训练失败不破坏线上服务**：异常处理里 retire 掉 building 版本，旧 active 模型保持不动

**查询场景：**
```sql
-- 在线推荐获取当前活跃模型
SELECT * FROM model_versions WHERE status = 'active' LIMIT 1;
-- 运维清理 30 天前的旧模型（先删 recommendations 再删版本）
DELETE FROM recommendations WHERE model_version IN
  (SELECT version FROM model_versions WHERE status='retired'
   AND generated_at < DATE_SUB(NOW(), INTERVAL 30 DAY));
```

---

#### 3.2.6 `model_item_factors` - ALS 电影隐因子表

**作用：** 存储 ALS 训练得到的电影隐向量，供在线推荐使用。

**表结构：**
```sql
CREATE TABLE model_item_factors (
  model_version VARCHAR(64) NOT NULL COMMENT '引用 model_versions.version',
  movie_id      INT NOT NULL         COMMENT '对应 movies.movie_id',
  factors       TEXT NOT NULL        COMMENT 'JSON 数组格式的电影隐向量',
  generated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (model_version, movie_id),
  FOREIGN KEY (model_version) REFERENCES model_versions(version) ON DELETE CASCADE
);
```

**数据来源**（`train_als.py`）：
1. 提取 ALS `model.itemFactors`
2. `F.to_json(F.col("features"))` 直接转 JSON 字符串
3. JDBC `append` 批量写入

**数据使用**（Flask 在线推荐）：
- 按版本一次性加载全部因子到内存字典 `{movie_id: np.ndarray}`，带 `RLock` 线程安全缓存与 rank 校验（维度不符或含非有限值直接判失败走降级）
- 用户新增评分后，固定电影因子，NNLS 求解用户向量
- 用户向量与所有电影因子做矩阵乘法，得到预测评分

---

#### 3.2.7 `recommendations` - 批量推荐结果表

**作用：** 存储 ALS 离线训练生成的批量推荐结果。

**表结构（关键列）：**
```sql
CREATE TABLE recommendations (
  id               BIGINT PRIMARY KEY AUTO_INCREMENT,
  model_version    VARCHAR(64) NULL  COMMENT '生成推荐的模型版本号（无外键，批量导入性能考虑）',
  user_id          INT NOT NULL      COMMENT '只含系统用户',
  movie_id         INT NOT NULL,
  predicted_rating FLOAT NOT NULL    COMMENT 'ALS 预测评分',
  `rank`           SMALLINT NOT NULL COMMENT '推荐排名，1=最推荐',
  generated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  INDEX idx_model_user_rank (model_version, user_id, `rank`),
  INDEX idx_user_rank (user_id, `rank`)
);
```

**数据来源**（`train_als.py` + `spark/utils/training_contract.py`）：
```python
user_recs = model.recommendForAllUsers(int(als_cfg["top_n"]))   # 每用户 Top-20
recs_flat = user_recs.select(
    F.col("userId").alias("user_id"),
    F.posexplode(F.col("recommendations")).alias("pos", "rec"),
).select(
    "user_id",
    F.col("rec.movieId").cast("int").alias("movie_id"),
    F.col("rec.rating").cast("float").alias("predicted_rating"),
    (F.col("pos") + 1).cast("smallint").alias("rank"),
)
# 半连接过滤：只保留 users 表中真实存在的系统用户
recs_flat = filter_recommendations_to_system_users(recs_flat, registered_users, "user_id")
write_df_to_mysql(recs_flat, "recommendations", jdbc_url, ..., mode="append")
```

**推荐策略：**

| 场景 | 策略 | 数据来源 |
|------|------|----------|
| 用户有评分 | 在线推荐优先 | 实时求解用户向量 + 电影因子 |
| 用户无评分 | 批量推荐降级 | `recommendations` 表（按 active 版本） |
| 在线推荐失败 | 批量推荐兜底 | 同上 |

---

#### 3.2.8 `movie_comments` - 电影评论表

**作用：** 存储用户对电影的文字评论，支持电影详情页评论区与个人主页评论列表。

**表结构：**
```sql
CREATE TABLE movie_comments (
  id         BIGINT PRIMARY KEY AUTO_INCREMENT,
  user_id    INT NOT NULL COMMENT '引用 users.id，ON DELETE CASCADE',
  movie_id   INT NOT NULL COMMENT '引用 movies.movie_id，ON DELETE CASCADE',
  content    TEXT NOT NULL,
  created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  edited_at  TIMESTAMP NULL DEFAULT NULL COMMENT '用户编辑正文的时间；从未编辑为 NULL（migrations/007）',
  INDEX idx_movie_created (movie_id, created_at DESC),
  INDEX idx_user (user_id)
);
```

**关联查询**：评论区展示时 LEFT JOIN `user_ratings` 取作者此刻的星级（没有评分行则不显示星星，同时把这条影评判为「仅自己可见」），见 `backend/app/routes/comments.py`。

**展示态的星级一律只读，改评分要走「编辑 + 保存」**：`CommentItem.vue` 只用列表接口返回的 `comment.user_rating` 渲染用户名旁那颗星，展示态下它是 `disabled` 的——**谁都不能直接点着改**。点卡片上的「编辑」进入编辑态后，编辑区里另有一颗可点的星星，值先落在草稿 `draftRating` 上；点「保存」才按「先 `apiRatings.submit()`、再 `apiComments.update()`」落库，并 `emit('saved', editKey, ratingChanged)`。父组件收到后先收起编辑态、`reloadComments()`，评分也改了才再 `emit('rating-submitted')` 让详情页重拉统计——**不即时改**，界面上的星星与库里的记录始终一致。

**评分与影评成对发表（前端按钮 + 后端校验两道关）**：`canSubmit = myRating > 0 && newComment.trim()`，只有评分没写正文、或只写正文没打分，按钮都点不动，并在表单下方说明还差哪一半（`submitHint`）。`submitAll()` 因此固定为「先 `apiRatings.submit()`、再 `apiComments.create()`」——顺序不能反，因为 `create_comment` 会检查作者此刻有没有评分（`_star_rating(...) is None` → 400「请先给这部电影评分，评分和影评需要一起发表」），绕过前端直接发也留不下「没有评分的影评」。

**发表之后表单收起，原输入框的位置交给自己的影评，且没有「修改」按钮**：`hasPublished = !!myComment`，表单只在 `!hasPublished` 时渲染；已发表时那一块渲染的是置顶的 `CommentItem`（自己的影评 + 编辑/删除）。改正文走卡片上的「编辑」（就地编辑，`PUT /api/comments/<id>`；未改动正文即收起，不白刷 `edited_at`），改评分同样走这条「编辑 + 保存」（见上一条），不再有「点星星当场改」的捷径。因此 `submitAll()` 里只剩 `create()` 一种写法，`editing` / `startModify` / `cancelModify` / `isModify` 这套「把表单调回来再走 PUT」的分支已整体撤掉。另外全页同一时刻只许一条影评处于编辑态：互斥由父组件的 `editingKey`（`pinned:<id>` / `list:<id>`）统一裁决，**非当前编辑的那张卡所有操作都冻结**——「编辑」与「删除」两个按钮一起 `:disabled="editLocked"`（共用 `lockTitle`，悬停提示「已有另一条影评正在编辑，请先保存或取消」），整张卡片 `.comment-item.locked` 压暗到 0.55；按钮之外还有一层兜底：`remove()` 与 `onStartEdit()` 一进门就 `if (props.editLocked) return`，保证「一边改、一边删」在程序化调用下也触发不了；置顶展示块与时间线之间渲染一条 `<el-divider class="pin-divider" />`。

**删除只有一个入口，而且要先问「影评草稿要不要保留」**：详情页星星旁**不再**有独立的「删除」（评分不再单独撤销）。点影评卡片上的「删除」（置顶那条与时间线里自己那条是同一个 `CommentItem`，行为完全一致）会弹一次 `ElMessageBox.confirm`；作者此刻有评分时，题目就是「这条影评草稿要保留吗」，两个出口分别落到两个接口，而且**两种结果都会让评分消失**，所以之后都走 `reloadComments()` + `emit('rating-submitted')` 把列表与详情页统计（`my_rating` 归 null、星星清空）刷新一遍：

| 选择 | 请求 | 结果 |
|---|---|---|
| 保留影评 | `DELETE /api/ratings/<movie_id>` | 只删评分（`user_ratings` 那一行硬删掉），影评留在原地但退成「仅自己可见」，提示「评分已删除，这条影评现在仅你自己可见」；置顶卡片仍在 |
| 不保留 | `DELETE /api/comments/<id>?with_rating=1` | 两行在同一次 commit 里删掉，提示「影评与评分已删除」；置顶那块**变回评论输入框**（`myComment` 归 null → `hasPublished` 变假，输入框回来） |
| 本来就没评分 | `DELETE /api/comments/<id>` | 没有评分可删，只有简单问法「确定要删除这条影评吗？」 |

`distinguishCancelAndClose: true` 把「点 X / Esc」与「不保留」区分开：前者什么都不删。**这条路径不经过「发表」按钮，也从不把 0 当成评分值 POST 出去**（后端只收 0.5 ~ 5.0）；两个接口都是硬删除。因此 `el-rate` 绝不能开 `clearable`——那会让「点同一颗星」不弹确认就归零。也正因为删除自成一条链路，`submitAll()` 里不需要「待撤销」分支。

**未提交修改的离开提醒**：`CommentSection.vue` 用 `hasPendingChange` 汇总未提交状态——**表单展开时**（即还没发表，`!hasPublished`）评分与 `savedRating` 不等、或正文与库里那条不一样，另有某条影评正开着就地编辑且正文被改过（卡片通过 `@dirty` 上报）。命中时 `onBeforeRouteLeave` 弹 `ElMessageBox.confirm` 三选一：确定＝提交后离开、取消＝不提交直接离开、点 X / Esc＝留在本页；`beforeunload` 另外拦刷新和关标签页。置顶展示态什么都不改得动，天然没有待提交的修改；**删除也不在其中**——三种删除都在确认后立即落库，不是「待提交」状态。

**`@dirty` 上报时要连自己的 id 一起报**：`CommentItem` 报的是 `emit('dirty', props.comment.id, 标记)`，父组件只写 `@dirty="onItemDirty"`，**不要**在模板里写 `onItemDirty(myComment.id, $event)` 这种内联箭头。Vue 编译器会把内联箭头缓存成一个闭包、调用时才去读那个变量；而卡片被列表刷新卸载时，卸载钩子里正好会补报一次 `emit('dirty', false)`，此时置顶用的 `myComment` 已经是 `null`，取 `.id` 抛出的 TypeError 会把这一次 DOM 补丁**整个打断**——列表停在 loading、旧卡片留在页面上，只有已经赋值的数据（如 `total`）变了。现实中它的表现是「删完影评后卡片不消失、转圈不结束」，很容易被误判成接口没生效，所以特意记在这里。

**写入规则**：`content` 必填，空正文一律 400；**作者此刻没有评分也一律 400**（检查顺序：正文非空 → 电影存在 404 → 作者有评分 400）。详情页那个按钮本就要求「评分 + 正文」同时填齐，服务端再加一道，绕过前端（或旧版本客户端）也留不下「没有评分的影评」，评分与影评因此始终成对出现。同一用户对同一电影只保留一条影评（重复提交即更新正文，不追加），与 `user_ratings` 的「一人一片一条」一致；走「再次发表」和走 `PUT /api/comments/<id>` 编辑都算改动正文，都会写 `edited_at`。

**编辑**：`PUT /api/comments/<id>` 只允许本人（403=他人，404=不存在），正文必填（改成空请走 DELETE）。`created_at` 保持不变，`edited_at` 记录改动时间，前端据此在影评卡片底部显示「上次修改于 …」（与「发布于 `created_at`」并排在同一处，卡片右上角不再重复显示时间）；**不能用 `updated_at` 判断是否编辑过**——那一列在插入时就会被写成发表时刻。

**可见性由评分推导，表里不存状态位**：一条影评是否公开展示，取决于作者此刻有没有给这部电影打分（`user_ratings` 里有没有对应那一行）。作者没有评分行时（例如在个人中心「我的评分」里删掉了这部片子的评分，或在影评卡片上选了「保留影评」）→ 影评退化为「仅自己可见」，只有作者本人请求时才会出现在列表里；恢复评分（≥0.5）后同一条影评自动重新公开。**发布侧不允许产生这种状态**（作者没有评分就发不出影评，见上一条），所以「仅自己可见」只会由「删除 → 保留影评」这一条路径产生。

**删影评时可以选择连带删评分（`with_rating=1`）**：`DELETE /api/comments/<id>` 默认只删 `movie_comments` 那一行；带 `?with_rating=1` 时，后端在**同一次 commit** 里把当前用户对该电影的 `user_ratings` 行一并删掉，所以不会出现「影评没了、评分还留着」的半截状态。详情页「删除 → 不保留」走的就是这条；用户本来就没评分时该参数是空操作，不报错。权限与存在性判断（403=他人、404=不存在）都在删除任何一行**之前**完成，因此校验不通过时一行都不会被删掉。

#### 3.2.9 `movie_rating_stats` - MovieLens 评分五档分布表

**作用：** 存放 MovieLens 全量评分的五档（5/4/3/2/1 星）计数，供电影详情页「评分统计」渲染分布柱。`movies` 表只有 `avg_rating` / `rating_count` 两个标量，拿不到分档数据，所以单独离线聚合一张派生表。

**表结构：**
```sql
CREATE TABLE movie_rating_stats (
  movie_id     INT PRIMARY KEY COMMENT '引用 movies.movie_id，ON DELETE CASCADE',
  rating_count INT NOT NULL COMMENT 'MovieLens 评分总条数',
  rating_sum   DECIMAL(14,1) NOT NULL COMMENT '评分总和，0.5 粒度下 1 位小数即精确',
  cnt_1        INT NOT NULL DEFAULT 0,
  cnt_2        INT NOT NULL DEFAULT 0,
  cnt_3        INT NOT NULL DEFAULT 0,
  cnt_4        INT NOT NULL DEFAULT 0,
  cnt_5        INT NOT NULL DEFAULT 0
);
```

**数据来源**：`scripts/build_ml_rating_stats.py` 流式聚合 `data/raw/ratings.csv`（3200 万条）一次性写入，约 84,432 行，**不随 ALS 训练变化**。档位规则直接复用 `backend/rating_stats.py` 的 `rating_to_star_bucket`，保证与本站评分同一口径（4.5 归 5 档）。重载 `movies` 后必须重跑该脚本。

**读取方**：`GET /api/movies/<id>/rating-stats` 把本表与 `user_ratings` 合并后返回，页面上不区分来源；平均分取本表的 `rating_sum`，不用 `movies.avg_rating`（后者只有 2 位小数，做加权平均精度不够）。

### 3.3 执行建表与迁移

**新数据库**（一步到位，schema 已包含全部 9 张表）：
```bash
mysql -u root -p < sql/schema.sql
```

**已有数据库**按顺序执行增量迁移：
```bash
mysqldump -u root -p --single-transaction movie_recommend > backup.sql
mysql -u root -p movie_recommend < sql/migrations/001_add_user_role.sql
mysql -u root -p movie_recommend < sql/migrations/001_add_movie_metadata_fields.sql
mysql -u root -p movie_recommend < sql/migrations/001_add_recommendation_model_tables.sql
mysql -u root -p movie_recommend < sql/migrations/002_create_movie_comments_table.sql
mysql -u root -p movie_recommend < sql/migrations/004_add_user_profile_fields.sql
mysql -u root -p movie_recommend < sql/migrations/005_merge_ratings_into_user_ratings.sql
mysql -u root -p movie_recommend < sql/migrations/006_add_movie_rating_stats.sql
mysql -u root -p movie_recommend < sql/migrations/007_add_comment_edited_at.sql
```

> `006` 只建表不灌数据，建完必须跑 `scripts/build_ml_rating_stats.py`（约 40 秒），否则详情页「评分统计」里没有 MovieLens 的份，只剩本站评分。

> `007` 给 `movie_comments` 加 `edited_at`（影评「上次修改于」）。漏执行会让影评接口报表列不存在——模型会 select 该列。`ADD COLUMN` 不幂等，重跑报 1060。

> `003_rename_ratings_table.sql` 已废弃（方向与整体设计相反），保留仅为说明历史，不需要执行。

验证：
```sql
USE movie_recommend;
SHOW TABLES;
-- 应该看到：users, movies, ratings(legacy), user_ratings, movie_rating_stats,
--           movie_comments, model_versions, model_item_factors, recommendations

SELECT COUNT(*) FROM movies;
SELECT COUNT(*) FROM user_ratings;
SELECT COUNT(*) FROM model_versions WHERE status = 'active';
```

---

## 四、数据预处理实现

### 4.1 准备 MovieLens 数据

#### 4.1.1 下载数据集

访问 [MovieLens-32M](https://grouplens.org/datasets/movielens/32m/)，下载并解压到项目 `data/raw/`：
- `ratings.csv`：3300 万条评分记录（约 650MB）
- `movies.csv`：8.6 万部电影元数据

#### 4.1.2 上传到 HDFS

```bash
bash scripts/init_hdfs.sh
# 脚本行为：检查 data/raw/ 文件 → hdfs dfs -mkdir -p /movielens
#          → hdfs dfs -put -f 两个 CSV → hdfs dfs -ls 验证
```

### 4.2 电影元数据预处理

脚本：[`spark/preprocess.py`](../spark/preprocess.py)

**核心流程：**
1. 从 HDFS 读取 `movies.csv`
2. 用 Spark 原生 `regexp_extract`（非 Python UDF，性能更好）解析标题年份
3. `na.drop` 去无效行 → `dropDuplicates(["movie_id"])` 去重
4. JDBC 写入 MySQL `movies` 表

**关键代码：**
```python
movies = (
    movies_raw
    .select(
        F.col("movieId").cast("int").alias("movie_id"),
        F.col("title"),
        F.col("genres"),
        F.regexp_extract(F.col("title"), r'\((\d{4})\)\s*$', 1).cast("int").alias("year")
    )
    .na.drop(subset=["movie_id", "title"])
    .dropDuplicates(["movie_id"])
)
write_df_to_mysql(movies, "movies", jdbc_url, ..., mode="overwrite")
```

**执行命令：**
```bash
spark-submit --master local[*] spark/preprocess.py
```

**验证：**
```sql
SELECT COUNT(*) FROM movies;          -- 应接近 87000
SELECT * FROM movies WHERE year = 1995 LIMIT 5;
```

> **已知限制**：`mode="overwrite"` 底层是 TRUNCATE + 重写，而且只写入 `movie_id`、`title`、`genres`、`year` 四列。
>
> - **外键**：实测引用 `movies` 的外键有两条——`movie_comments.movie_id` 与 `movie_rating_stats.movie_id`（当前活库）。`user_ratings` 只对 `users.id` 建了外键，`model_item_factors`、`recommendations` 都没有指向 `movies` 的外键。MySQL 不允许 TRUNCATE 被外键引用的表，与引用表里有没有数据无关，所以只要 `movie_comments` 存在，再次执行预处理就会失败。（按 `sql/schema.sql` 的 DDL 全新建库时，legacy `ratings` 声明的 `movie_id` 与 `user_id` 两条外键会构成第三、第四条。）
> - **元数据丢失**：即使绕过外键，`movies` 上由 `import_movie_stats.py` / `sync_movie_stats.py` 写的 `avg_rating`、`rating_count`，以及 `fetch_posters.py` 回填的海报、简介、导演等富化列都会被清空；`movie_rating_stats` 依赖 `movies` 行存在，重载后必须重跑 `scripts/build_ml_rating_stats.py` 重建。
>
> **不要把这条命令当作可重复的生产刷新流程**，应改为 staging/upsert。

### 4.3 评分统计聚合（`ratings_clean.csv`）

脚本：[`scripts/build_ratings_clean.py`](../scripts/build_ratings_clean.py)

对 `data/raw/ratings.csv`（3200 万条明细）按 movieId 做单遍流式聚合，产出
`data/raw/ratings_clean.csv`（84,432 行 × 11 列：`rating_count` / `rating_mean` /
`rating_median` / `rating_std` / 极值 / `rating_sum` / `rating_range` / 四分位数）：

```bash
backend/venv/Scripts/python.exe scripts/build_ratings_clean.py --check   # 与现有产物逐单元格比对
backend/venv/Scripts/python.exe scripts/build_ratings_clean.py --force   # 重新生成
```

下游 [`scripts/import_movie_stats.py`](../scripts/import_movie_stats.py) 把
`rating_mean` / `rating_count` 两列回填 `movies` 表，支撑首页热门榜的贝叶斯加权
排序与电影卡片均分。它与详情页评分统计（`movie_rating_stats` 表，见 3.x 表说明与
`build_ml_rating_stats.py`）**同源、行数一致**，但粒度不同：本文件只服务 `movies`
两列回填，五档分布走 `build_ml_rating_stats.py`。注意单条评分的电影 `rating_std`
为空（样本标准差无定义）。

---

## 五、离线 ALS 训练实现

脚本：[`spark/train_als.py`](../spark/train_als.py)，启动：`bash scripts/run_training.sh`

### 5.1 训练流程（与代码一一对应）

```
 1. 读取 HDFS MovieLens 评分，userId cast 为 long
 2. 统计 MovieLens 最大用户 ID；查 MySQL users 表最大 ID
 3. validate_user_id_namespace 校验命名空间隔离
 4. MovieLens userId 取负（-userId → int）
 5. JDBC 子查询读取 user_ratings，并与 users 半连接（只留真实系统用户）
 6. MovieLens 评分按 train_ratio=0.8 随机划分（seed=42）
 7. combine_rating_sources：MovieLens 训练集 ∪ 系统用户全部评分
 8. 训练非负显式反馈 ALS
 9. 只用 MovieLens 测试集计算 RMSE（口径稳定，不受系统用户影响）
10. create_model_version：写入 building 版本记录
11. model.itemFactors → to_json → JDBC append 写 model_item_factors
12. recommendForAllUsers(top_n) → posexplode 展开 → 半连接过滤系统用户
    → JDBC append 写 recommendations
13. activate_model_version：原子发布（旧 active → retired，本版本 → active）
```

任一步骤失败：retire 掉 building 版本，旧 active 模型不受影响。

### 5.2 用户 ID 命名空间验证

```python
from utils.user_id_namespace import validate_user_id_namespace

# train_als.py 中：
max_ml_user_id = ratings_ml_raw.agg(F.max("ml_user_id")).first()[0]   # 162541
max_system_user_id = _read_max_system_user_id(spark, jdbc_url, mysql_cfg)  # SELECT MAX(id) FROM users
validate_user_id_namespace(max_system_user_id, max_ml_user_id)
# 输出：User ID namespace validated: system=[1, 6], MovieLens=[-162541, -1]
# 系统用户 ID 为负时抛 ValueError
```

### 5.3 数据合并

```python
# MovieLens 用户 ID 取负（正变负，与系统用户隔离）
ratings_ml = ratings_ml_raw.select(
    (-F.col("ml_user_id")).cast("int").alias("userId"),
    F.col("movieId"), F.col("rating"),
)

# 系统用户评分：JDBC 子查询 + left_semi 半连接 users 表
ratings_system = (
    read_df_from_mysql(spark, build_system_ratings_query(), jdbc_url, ...)
    .select(F.col("userId").cast("int"), F.col("movieId").cast("int"),
            F.col("rating").cast("float"))
    .na.drop()
    .join(registered_users.withColumnRenamed("user_id", "userId"), "userId", "left_semi")
)

# 8:2 划分 + 合并训练集
ml_train, ml_test = ratings_ml.randomSplit([0.8, 0.2], seed=42)
training_ratings = combine_rating_sources(ml_train, ratings_system).cache()
```

### 5.4 ALS 模型训练

```python
als = ALS(
    rank=50,                    # als.rank（config.yaml）
    maxIter=10,                 # als.max_iter
    regParam=0.1,               # als.reg_param
    userCol="userId",
    itemCol="movieId",
    ratingCol="rating",
    coldStartStrategy="drop",   # als.cold_start_strategy
    nonnegative=True,           # 非负约束（与在线求解一致）
    implicitPrefs=False         # 显式反馈
)
model = als.fit(training_ratings)

predictions = model.transform(ml_test)
rmse = RegressionEvaluator(metricName="rmse", labelCol="rating",
                           predictionCol="prediction").evaluate(predictions)
print(f"MovieLens test RMSE: {rmse:.4f}")
```

### 5.5 保存模型因子

```python
model_version = _new_model_version()   # als-{UTC时间戳}-{12位hex}
create_model_version(mysql_cfg, model_version, rank=50, reg_param=0.1)  # building

item_factors = model.itemFactors.select(
    F.lit(model_version).alias("model_version"),
    F.col("id").cast("int").alias("movie_id"),
    F.to_json(F.col("features")).alias("factors"),   # Spark 向量直接转 JSON
)
write_df_to_mysql(item_factors, "model_item_factors", jdbc_url, ..., mode="append")
```

### 5.6 生成推荐并过滤

```python
user_recs = model.recommendForAllUsers(int(als_cfg["top_n"]))   # Top-20
recs_flat = user_recs.select(
    F.col("userId").alias("user_id"),
    F.posexplode(F.col("recommendations")).alias("pos", "rec"),
).select(
    "user_id",
    F.col("rec.movieId").cast("int").alias("movie_id"),
    F.col("rec.rating").cast("float").alias("predicted_rating"),
    (F.col("pos") + 1).cast("smallint").alias("rank"),
)
recs_flat = filter_recommendations_to_system_users(recs_flat, registered_users, "user_id")
recs_flat = recs_flat.select("model_version", "user_id", "movie_id",
                             "predicted_rating", "rank") \
                     .withColumn("model_version", F.lit(model_version))
write_df_to_mysql(recs_flat, "recommendations", jdbc_url, ..., mode="append")
```

### 5.7 发布模型

```python
activate_model_version(mysql_cfg, model_version)
# 事务内：UPDATE ... SET status='retired' WHERE status='active' AND version<>{本版本}
#         UPDATE ... SET status='active', activated_at=UTC_TIMESTAMP() WHERE version={本版本} AND status='building'
# rowcount != 1 时抛 RuntimeError 回滚
```

### 5.8 执行训练

```bash
bash scripts/run_training.sh
# 等价于：
spark-submit --master local[*] --driver-memory 4g --executor-memory 4g spark/train_als.py
```

**训练前必须确认：**
- HDFS 评分路径存在；
- `user_ratings`、`users` 和三张模型表已创建；
- MySQL Connector/J 可被 Spark 加载；
- HDFS、MySQL 地址从 Spark 所在节点可达。

**预期输出（数量为示例）：**
```
Reading MovieLens ratings from hdfs://hadoop102:9000/movielens/ratings.csv ...
MovieLens ratings: 33,000,000; MovieLens user IDs negated
Application-user ratings: 12
Training ratings: 26,400,012; MovieLens test ratings: 6,599,992
MovieLens test RMSE: 0.8xxx
Published model als-20260910T173512Z-1d3ea7bd40b0: training=26,400,012,
  MovieLens=33,000,000, system=12, RMSE=0.8xxx, recommendations=120
```

> `Application-user ratings` 这一行来自 MySQL `user_ratings` 表（见 `spark/train_als.py`）。该表为空时此行输出 `0`——全新数据库或还没有任何用户评分时就是这个情况，训练本身仍会正常完成。这部分评分会计入下一行的 `Training ratings`，并只使用 `users` 表中的正 ID。

**验证：**
```sql
SELECT version, status, `rank`, reg_param, generated_at, activated_at
FROM model_versions ORDER BY generated_at DESC;

SELECT model_version, COUNT(*) FROM model_item_factors GROUP BY model_version;
SELECT model_version, COUNT(*) FROM recommendations GROUP BY model_version;
```

---

## 六、在线推荐服务实现

代码：[`backend/app/recommendation_core.py`](../backend/app/recommendation_core.py)、
[`backend/app/recommendation_service.py`](../backend/app/recommendation_service.py)、
[`backend/app/recommendation_repository.py`](../backend/app/recommendation_repository.py)

### 6.1 核心算法原理

**问题：** 用户新增评分后，如何立即生成更新的推荐，而不重新训练整个 ALS 模型？

**解决方案：** 固定已训练的电影隐因子矩阵 $Q$，只求解当前用户的隐向量 $p_u$。

#### ALS 简化回顾

协同过滤模型预测评分：$\hat{r}_{ui} = p_u^T q_i$

ALS 训练时交替固定 $P$ 或 $Q$。在线推荐时 $Q$ 已由离线训练产出（存在 `model_item_factors`），只需求解一个用户的 $p_u$。

#### 用户向量求解公式

给定用户 $u$ 的评分集合 $\{(i, r_{ui})\}$，求解：

$$p_u = \arg\min_{p_u \geq 0} \sum_{i \in I_u} (r_{ui} - p_u^T q_i)^2 + \lambda ||p_u||^2$$

带非负约束（与训练时 `nonnegative=True` 一致），用 `scipy.optimize.nnls` 求解。

### 6.2 核心实现（`recommendation_core.py`，纯函数、可单测）

```python
def solve_user_vector(item_factors, ratings, reg_param, nonnegative=True):
    """固定电影因子，求解用户向量（带 L2 正则的增广最小二乘）"""
    factors = np.asarray(item_factors, dtype=float)
    values = np.asarray(ratings, dtype=float)
    # ... 形状校验（2-D 因子、1-D 评分、行数一致、至少 1 条评分、reg_param>=0）

    rank = factors.shape[1]
    regularizer = np.sqrt(reg_param) * np.eye(rank)
    augmented_factors = np.vstack((factors, regularizer))
    augmented_ratings = np.concatenate((values, np.zeros(rank)))

    if nonnegative:
        vector, _ = nnls(augmented_factors, augmented_ratings)
        return vector
    # 无约束分支：正规方程
    lhs = factors.T @ factors + reg_param * np.eye(rank)
    rhs = factors.T @ values
    return np.linalg.solve(lhs, rhs)


def rank_unrated_items(movie_ids, item_factors, user_vector, rated_movie_ids, top_n):
    """矩阵乘法批量打分 → 过滤已评分 → 按分数降序（同分按 movie_id 升序）取 Top-N"""
    scores = factors @ vector
    candidates = [(int(mid), float(s)) for mid, s in zip(ids, scores)
                  if int(mid) not in rated_movie_ids]
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates[:top_n]
```

### 6.3 服务层实现（`recommendation_service.py` + `recommendation_repository.py`）

服务采用**依赖注入的仓储模式**，便于用假仓储单测（`tests/test_recommendation_service.py`）：

```python
class OnlineRecommendationService:
    """Calculate recommendations for one application user without retraining ALS."""

    def __init__(self, repository, top_n=20):
        self.repository = repository            # SqlAlchemyRecommendationRepository
        self.top_n = int(top_n)
        self._factor_cache = {}                 # {model_version: {movie_id: np.ndarray}}
        self._cache_lock = RLock()              # 多线程下防重复构建缓存

    def _load_factors(self, model):
        """按版本缓存电影因子；维度不等于 model.rank 或含非有限值 → ValueError → 降级"""
        ...

    def _online_recommendations(self, user_id, model, ratings):
        factors = self._load_factors(model)
        rating_by_movie = {int(r.movie_id): float(r.rating) for r in ratings}
        # 用户评分的电影必须全部存在于因子表，否则视为模型与数据不匹配 → 降级
        user_vector = solve_user_vector(
            np.asarray(rated_factors), np.asarray(rated_values),
            reg_param=float(model.reg_param), nonnegative=True,
        )
        ranked = rank_unrated_items(all_movie_ids, all_factors, user_vector,
                                    set(rated_movie_ids), self.top_n)
        # 补齐电影详情（批量 IN 查询，缺元数据同样视为异常 → 降级）
        ...

    def get_for_user(self, user_id):
        """入口：在线优先，批量兜底"""
        model = self.repository.get_active_model()          # status='active' 最新一条
        if model is None:
            return []                                       # 无 active 模型 → 空列表
        ratings = self.repository.get_user_ratings(user_id)
        if not ratings:
            return self._batch(user_id, str(model.version)) # 新用户 → 批量推荐
        try:
            return self._online_recommendations(user_id, model, ratings)
        except Exception:
            logger.exception("online recommendation failed for user_id=%s", user_id)
            return self._batch(user_id, str(model.version)) # 任何失败 → 批量推荐
```

仓储接口（`SqlAlchemyRecommendationRepository`）：`get_active_model()`、`get_user_ratings(user_id)`（读 `user_ratings`）、`get_model_factors(version)`、`get_batch_recommendations(user_id, version)`（joinedload 电影详情）、`get_movies(movie_ids)`。

服务在 `create_app()` 中构建为**应用级单例**，挂载到 `app.extensions["recommendation_service"]`，路由直接取用（见 7.3）。

### 6.4 降级策略总结

| 情况 | 行为 |
|------|------|
| 无 active 模型 | 返回空列表（前端显示空状态，属正常） |
| 用户无评分 | 返回当前 active 版本的批量推荐 |
| 因子缺失 / rank 不符 / JSON 损坏 / NNLS 异常 / 电影元数据缺失 | 记日志，回退批量推荐 |
| 批量推荐也没有 | 返回空列表 |

---

## 七、后端 API 实现

### 7.1 技术栈与依赖

Flask 3.0.3、Flask-SQLAlchemy 3.1.1（SQLAlchemy 2.0.40）、Flask-JWT-Extended 4.6.0、Flask-Cors 4.0.1、PyMySQL、NumPy + SciPy（在线求解）。完整清单见 `backend/requirements.txt`。

> JWT 有效期未显式配置，遵循 Flask-JWT-Extended 默认值。

### 7.2 项目结构

```
backend/
├── app.py                          # 入口：create_app(Config) + app.run
├── config.py                       # 从根目录 config.yaml 读配置（含 DB URI、CORS、ALS_TOP_N）
├── requirements.txt
└── app/
    ├── __init__.py                 # create_app：扩展初始化、服务单例、蓝图注册
    ├── db.py                       # SQLAlchemy 实例
    ├── models.py                   # User / Movie / UserRating / Recommendation /
    │                               # ModelVersion / ModelItemFactor / MovieComment
    ├── rating_stats.py             # 评分档位规则（纯函数，离线聚合脚本复用）
    ├── recommendation_core.py      # 纯函数：NNLS 求解 + Top-N 排序
    ├── recommendation_service.py   # OnlineRecommendationService（缓存 + 降级）
    ├── recommendation_repository.py# 推荐数据仓储
    └── routes/
        ├── auth.py                 # /api/auth/register /login
        ├── movies.py               # /api/movies 列表/详情/类型
        ├── ratings.py              # /api/ratings 提交/历史
        ├── recommendations.py      # /api/recommendations 推荐
        ├── comments.py             # /api/movies/<id>/comments 评论
        ├── profile.py              # /api/profile 个人主页
        └── admin.py                # /api/admin/* 管理后台（admin_required）
```

### 7.3 应用初始化（`app/__init__.py`）

```python
def create_app(config_obj=None):
    app = Flask(__name__)
    app.config.from_object(config_obj or Config)

    db.init_app(app)
    origins = app.config.get("CORS_ORIGINS") or ["http://localhost:5173"]
    CORS(app, origins=origins)          # 不开放 "*"
    JWTManager(app)

    # 在线推荐服务：应用级单例，避免多线程重复构建因子缓存
    app.extensions["recommendation_service"] = OnlineRecommendationService(
        SqlAlchemyRecommendationRepository(),
        top_n=app.config.get("ALS_TOP_N", 20),
    )

    # 蓝图注册（8 组路由）
    app.register_blueprint(auth_bp,      url_prefix="/api/auth")
    app.register_blueprint(movies_bp,    url_prefix="/api/movies")
    app.register_blueprint(rec_bp,       url_prefix="/api/recommendations")
    app.register_blueprint(ratings_bp,   url_prefix="/api/ratings")
    app.register_blueprint(admin_bp,     url_prefix="/api/admin")
    app.register_blueprint(comments_bp,  url_prefix="/api")
    app.register_blueprint(profile_bp,   url_prefix="/api/profile")
    return app
```

配置（`config.py`）全部来自根目录 `config.yaml`：`SQLALCHEMY_DATABASE_URI = mysql+pymysql://...`、`JWT_SECRET_KEY = flask.secret_key`、`CORS_ORIGINS = cors.origins`、`ALS_TOP_N = als.top_n`、`ALS_REG_PARAM = als.reg_param`。

### 7.4 关键路由实现

#### 认证（`routes/auth.py`）

```python
@auth_bp.post("/register")
def register():
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    # 校验：非空、用户名 ≤50、密码 6~128 位
    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "用户名已存在"}), 409
    user = User(username=username)
    user.set_password(password)
    db.session.add(user); db.session.commit()
    token = create_access_token(identity=str(user.id))   # identity 必须 str
    return jsonify({"token": token, "user": user.to_dict()}), 201
```

> 注意 `create_access_token(identity=str(user.id))`：JWT identity 须为字符串，路由内用 `int(get_jwt_identity())` 还原。

#### 评分（`routes/ratings.py`，写 `user_ratings` 表）

```python
@ratings_bp.post("/")
@jwt_required()
def submit_rating():
    user_id = int(get_jwt_identity())
    # 校验 movie_id/rating 必填、rating ∈ [0.5, 5.0]、电影存在（404）
    existing = UserRating.query.filter_by(user_id=user_id, movie_id=movie_id).first()
    if existing:                       # 重复评分 → 更新（uk_user_movie 唯一约束）
        existing.rating = rating
        existing.rated_at = datetime.now(timezone.utc)
        status_code = 200
    else:                              # 首次评分 → 插入
        db.session.add(UserRating(user_id=user_id, movie_id=int(movie_id), rating=rating))
        status_code = 201
    ...
```

#### 推荐（`routes/recommendations.py`）

```python
@rec_bp.get("/")
@jwt_required()
def get_recommendations():
    user_id = int(get_jwt_identity())
    service = current_app.extensions["recommendation_service"]   # 应用级单例
    recs = service.get_for_user(user_id)
    if not recs:
        return jsonify({"msg": "暂无推荐数据，请先评分或等待模型首次发布", "data": []})
    return jsonify({"user_id": user_id, "count": len(recs), "data": recs})
```

每条推荐结构：`{"rank": 1, "predicted_rating": 4.5123, "movie": {电影完整字段}}`。

#### API 总表

| 方法 | 路径 | 说明 | 鉴权 |
|---|---|---|---|
| `POST` | `/api/auth/register` | 注册并返回 JWT + 用户信息 | 否 |
| `POST` | `/api/auth/login` | 登录 | 否 |
| `GET` | `/api/movies/` | 分页、关键词、类型、年份筛选；sort=popular/latest/movie_id | 否 |
| `GET` | `/api/movies/<id>` | 电影详情 | 否 |
| `GET` | `/api/movies/<id>/rating-stats` | 评分统计（评分人数 + 平均分 + 五档分布 + 当前用户评分） | 可选 |
| `GET` | `/api/movies/genres` | 类型列表 | 否 |
| `POST` | `/api/movies/<id>/comments` | 发表影评（正文必填，**且要求作者已有评分**，否则 400；一人一片一条，重复提交即更新并记 `edited_at`） | 是 |
| `GET` | `/api/movies/<id>/comments` | 影评列表（附带作者星级；`my_comment` 单独返回当前用户那条用于置顶） | 可选 |
| `PUT` | `/api/comments/<id>` | 编辑自己的影评（正文必填；403=非本人，404=不存在） | 是 |
| `DELETE` | `/api/comments/<id>` | 删除自己的影评；带 `?with_rating=1` 时连同自己的评分一起删（影评卡片「删除 → 不保留」走这条，两行同一次提交） | 是 |
| `POST` | `/api/ratings/` | 新增或更新评分（取值范围 **0.5 ~ 5.0**，0 与其它越界值一律 400） | 是 |
| `DELETE` | `/api/ratings/<movie_id>` | 删除自己对该电影的评分（**只删评分**，影评留在原地退成「仅自己可见」；影评卡片「删除 → 保留影评」会调它） | 是 |
| `GET` | `/api/recommendations/` | 当前用户推荐（在线优先） | 是 |
| `GET/PUT` | `/api/profile/info` | 个人信息读取/修改（用户名、头像、签名） | 是 |
| `GET` | `/api/profile/stats` | 观影统计（评分分布、最爱类型/导演） | 是 |
| `GET` | `/api/profile/comments` | 我的评论（分页排序，附带我在该片评分的星级） | 是 |
| `GET` | `/api/admin/stats` | 系统统计（用户/电影/评分/推荐量、active 模型） | admin |
| `GET/POST/PATCH/DELETE` | `/api/admin/users[...]` | 用户管理 | admin |
| `GET/PATCH/DELETE` | `/api/admin/movies[...]` | 电影管理 | admin |
| `GET/DELETE` | `/api/admin/ratings[...]` | 评分管理 | admin |
| `GET` `/api/admin/models`；`POST .../activate`；`DELETE` | 模型查看/激活/删除（active 不可删） | admin |

### 7.5 启动后端

**Windows（开发机）**：项目根目录 `start.bat`（优先用 `backend/venv`，失效时回退系统 Python），或 `python scripts/start_backend.py`。

**Linux（服务器）**：
```bash
cd backend
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py     # 监听 config.yaml 的 flask.host:port
```

> `scripts/start_dev.sh`（Linux 联合启动）检查的是项目根 `venv`，与实际约定 `backend/venv` 不符，Linux 使用前需先修正脚本内 Python 路径（README 已知问题）。

---

## 八、前端实现

### 8.1 技术栈

Vue 3.4（Composition API）、Vite 5、Element Plus 2.7 + icons、Pinia 2.1、Vue Router 4.3、Axios 1.7。依赖见 `frontend/package.json`。

### 8.2 项目结构

```
frontend/src/
├── main.js / App.vue
├── router/index.js        # 路由 + 全局守卫（requiresAuth / requiresAdmin）
├── layouts/AdminLayout.vue
├── stores/
│   ├── auth.js            # token + user，含 isAdmin
│   └── user.js            # 未使用（遗留，待清理）
├── api/
│   ├── index.js           # Axios 实例：JWT 请求拦截、401 响应拦截
│   ├── auth.js / movies.js / ratings.js / recommendations.js
│   ├── comments.js / profile.js / admin.js
│   └── client.js          # 未使用（遗留，待清理）
├── views/
│   ├── HomeView.vue           # 电影浏览主页
│   ├── LoginView.vue / RegisterView.vue
│   ├── MovieDetailView.vue    # 详情 + 评分 + 评论区
│   ├── RecommendationsView.vue
│   ├── ProfileView.vue
│   └── admin/                 # AdminDashboard / AdminUsers /
│                              # AdminMovies / AdminRatings / AdminModels
└── components/
    ├── NavBar.vue / MovieCard.vue / CommentSection.vue / CommentItem.vue
    └── ThemeSwitch.vue        # 日间 / 夜间 / 跟随系统三档主题切换
```

### 8.3 路由与权限守卫（`router/index.js`）

```javascript
const routes = [
  { path: '/', component: HomeView },
  { path: '/login', component: LoginView },
  { path: '/register', component: RegisterView },
  { path: '/movies/:id', component: MovieDetailView },
  { path: '/recommendations', component: RecommendationsView, meta: { requiresAuth: true } },
  { path: '/profile', component: ProfileView, meta: { requiresAuth: true } },
  { path: '/admin', component: AdminLayout, redirect: '/admin/dashboard',
    meta: { requiresAuth: true, requiresAdmin: true },
    children: [ dashboard, users, movies, ratings, models ] },
]

router.beforeEach((to) => {
  const auth = useAuthStore()
  if (to.meta.requiresAuth && !auth.isLoggedIn) return { name: 'login', query: { redirect: to.fullPath } }
  if (to.meta.requiresAdmin && !auth.isAdmin)  return { name: 'home' }   // 静默重定向
  if ((to.name === 'login' || to.name === 'register') && auth.isLoggedIn) return { name: 'home' }
})
```

### 8.4 Axios 封装（`api/index.js`）

```javascript
const http = axios.create({ baseURL: '/api', timeout: 15000 })

// 请求拦截：自动附带 JWT
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = 'Bearer ' + token
  return config
})

// 响应拦截：统一取 data；401（非登录页）清凭据跳登录
http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    if (err.response?.status === 401 && window.location.pathname !== '/login') {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      window.location.href = '/login'
    }
    return Promise.reject(err.response?.data?.msg || '请求失败，请重试')
  }
)
```

各 API 模块（`auth.js`、`movies.js`、`ratings.js` 等）在 http 实例上按业务分组导出，如 `apiRatings.submit = (movie_id, rating) => http.post('/ratings/', { movie_id, rating })`。

### 8.5 认证 Store（`stores/auth.js`）

```javascript
export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user  = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin    = computed(() => user.value?.role === 'admin')

  async function login(username, password) {
    const data = await apiAuth.login(username, password)
    _persist(data.token, data.user)      // localStorage 持久化
    return data.user
  }
  // register / logout 同理
})
```

### 8.6 交互要点

- **电影浏览**（HomeView）：分页 + 名称/类型/年份筛选 + 排序（popular 过滤评分人数 < 100）；
- **电影详情**（MovieDetailView）：海报、简介、导演、演员、国家、语言、片长（剧情简介 18px）；评分统计显示**评分人数**（MovieLens 与本站合并后的 `count`）、平均分与五档分布；录入区里**用户名 + 半星评分控件在评论框上方**（0.5~5.0，控件不可清零，点同一颗星两次不会变 0），评分与影评**必须同时填写**才能点得动「发表」；发表后表单收起、原输入框的位置由自己的影评顶替（置顶），**没有「修改」按钮**——改正文和改评分都走卡片上的「编辑」（展示态的星星一律只读，本人他人的都点不动；在编辑区里改完星星要点「保存」才落库），同一时刻全页只许一条影评处于编辑态，置顶那条与时间线里同名那条互斥（其余卡片的操作——「编辑」和「删除」——全部禁用且整卡压暗）；置顶展示块与时间线之间有一条分割线；时间只在卡片底部显示一处（「发布于 …」+ 改过正文才有的「上次修改于 …」）；删除只有一个入口（影评卡片上的「删除」），确认框问「这条影评草稿要保留吗」——保留＝只删评分（`DELETE` 把 `user_ratings` 那一行硬删除，影评退成仅自己可见），不保留＝两行一起删、置顶那块变回输入框；评论区（时间倒序、分页、显示评论者星级）；改评分或影评未提交就离开会弹确认框；
- **推荐页**（RecommendationsView）：星级推荐指数（ALS 预测分归一化到 0.5~5.0）；无 active 模型时显示空状态（正常现象）；
- **管理后台**（/admin）：统计数据、用户/电影/评分/模型管理，非 admin 访问静默跳回首页。

### 8.7 Vite 配置（`vite.config.js`）

```javascript
export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } }
  }
})
```

启动：
```bash
cd frontend
npm install
npm run dev      # http://localhost:5173
```

---

## 九、运行与验证

### 9.1 启动顺序

**Linux 服务器（Hadoop/Spark/MySQL 节点）**：
```bash
start-dfs.sh && start-yarn.sh     # 1. Hadoop
sudo systemctl start mysqld       # 2. MySQL
bash scripts/run_training.sh      # 3. 离线训练（首次或周期性）
```

**开发机**：
```bat
:: Windows：项目根目录
start.bat                         :: 同时启动 Flask + Vue
```
```bash
# 或分别启动
python scripts/start_backend.py   # Flask, 127.0.0.1:8000
npm run dev --prefix frontend     # Vite, localhost:5173（/api/* 代理到 8000）
```

访问：`http://127.0.0.1:5173/`

### 9.2 功能验证清单

#### 数据预处理
```bash
hdfs dfs -ls /movielens/
mysql -u movie_app -p -e "SELECT COUNT(*) FROM movie_recommend.movies;"
```

#### 模型训练
```sql
SELECT version, status, `rank`, reg_param, activated_at
FROM model_versions ORDER BY generated_at DESC LIMIT 1;
SELECT COUNT(*) FROM model_item_factors WHERE model_version = '<版本号>';
SELECT COUNT(DISTINCT user_id) FROM recommendations WHERE model_version = '<版本号>';
```

#### 用户注册登录
1. 访问 `http://localhost:5173` → 注册 → 自动登录，右上角显示用户名

#### 电影浏览与评分
1. 首页电影列表分页、搜索 "Toy Story"、类型筛选 Action
2. 电影详情页提交 4.5 星评分 → 提示评分成功

#### 推荐链路（核心验收）
1. 评分 3 部电影 → 访问推荐页 → 触发在线求解，记录前 5 条
2. 再给 2 部新电影打高分 → 刷新推荐页 → 列表变化（同类型高分电影排名上升）
3. 新注册未评分用户 → 显示 active 模型的批量推荐

#### 在线/离线联动
1. 前端评分 → `user_ratings` 表新增记录
2. `bash scripts/run_training.sh` 重训 → 新评分进入训练集
3. 其他用户的批量推荐随新模型更新

### 9.3 常见问题排查

#### 问题 1：前端显示 "Total 0"
```bash
netstat -tuln | grep 8000                # Flask 是否监听
mysql -u movie_app -p -e "SELECT COUNT(*) FROM movie_recommend.movies;"
```

#### 问题 2：推荐页显示空
- 无 active 模型 → 正常状态（等训练发布）：
  ```sql
  SELECT * FROM model_versions WHERE status = 'active';
  ```
- active 模型存在但批量推荐为空 → 该用户不在 `recommendations` 的当前版本中，检查训练时用户是否已注册。

#### 问题 3：评分后推荐不变
后端日志应有 `online recommendation failed for user_id=...`（logger.exception）。
排查方向：
```sql
-- 用户评分的电影是否全部在因子表里
SELECT COUNT(*) FROM model_item_factors
WHERE model_version = '<active版本>' AND movie_id IN (
  SELECT movie_id FROM user_ratings WHERE user_id = <用户ID>);
```
因子缺失时服务自动降级批量推荐（设计行为），修复需重训。

#### 问题 4：Spark 作业失败
- HDFS 路径不存在 / MySQL 不可达 / JDBC 驱动缺失（$SPARK_HOME/jars/）
- 训练失败时 building 模型自动转 retired，旧 active 模型不受影响，可直接修复后重跑

#### 问题 5：preprocess 二次执行报外键错误
`mode="overwrite"` 与 `movies` 表上的外键冲突（见 4.2 已知限制），初始化后不要再对已有数据库跑 preprocess。

---

## 十、关键技术点总结

### 10.1 两阶段推荐架构的优势

| 维度 | 离线训练 | 在线推荐 |
|------|----------|----------|
| **延迟** | 小时级 | <100ms（80 万因子向量的矩阵乘法） |
| **数据新鲜度** | 周期性更新 | 每次评分实时反映 |
| **计算资源** | Spark 集群 | 单机 Flask + NumPy/SciPy |
| **覆盖范围** | 全量用户 | 单个用户 |

1. **用户体验好**：评分后立即看到更新的推荐
2. **资源高效**：不为每次评分启动 Spark
3. **口径一致**：在线求解与训练同为非负 + L2 正则，用户向量落在与电影因子同一空间

### 10.2 用户 ID 隔离设计的必要性

**不隔离的问题：**
- MovieLens 用户 ID（1~162541）与系统用户 ID 直接冲突
- `recommendForAllUsers` 会为 16 万 MovieLens 用户生成无意义推荐

**取负方案的优点：**
1. **命名空间天然隔离**：系统用户 `[1, +∞)`，MovieLens 用户 `[-162541, -1]`
2. **无需偏移量配置**：训练入口有显式校验（系统用户 ID 为负即报错）
3. **共享模型**：两类用户在同一 ALS 模型中训练，系统用户享受全量数据训练出的电影因子
4. **数据纯净**：推荐经半连接过滤只保留系统用户；MovieLens 用户不写入 `users`，推荐结果不回流为评分

### 10.3 模型版本管理的作用

- 训练耗时，需记录每次训练的参数和结果
- 在线推荐需要知道当前用哪个版本的因子（带版本缓存，模型切换自动失效重建）
- 原子发布：旧 active 转 retired 与新模型激活在同一事务，不存在"两个 active"或"零 active"的中间态
- 训练失败自动隐藏 building 版本，线上服务无感

### 10.4 降级策略的重要性

在线推荐可能因以下原因失败：因子缺失（评分的电影不在模型里）、因子维度/格式异常、数值计算异常、电影元数据缺失。

降级链：**在线计算 → 批量推荐（active 版本）→ 空列表**。保证推荐接口永不报错，只可能"暂时没有个性化结果"。

### 10.5 虚拟机集群的学习价值

1. **分布式存储**：HDFS NameNode/DataNode 架构、数据块复制
2. **资源调度**：YARN ResourceManager/NodeManager、容器分配
3. **网络通信**：主机名解析、SSH 免密的原理与作用
4. **配置管理**：统一配置文件多节点同步
5. **故障模拟**：关闭 DataNode 观察 HDFS 副本自愈

---

## 附录

### A. 配置文件示例

完整配置见 [`config.example.yaml`](../config.example.yaml)（`config.yaml` 为本地真实配置，已被 .gitignore / .aiignore 忽略）：

```yaml
hdfs:
  namenode: hdfs://localhost:9000    # 单机；集群改为 hdfs://hadoop102:9000
  ratings_path: /movielens/ratings.csv
  movies_path: /movielens/movies.csv

mysql:
  host: localhost                    # 集群改为 192.168.10.102
  port: 3306
  user: movie_app
  password: "change_me"
  database: movie_recommend
  charset: utf8mb4

als:
  rank: 50
  max_iter: 10
  reg_param: 0.1
  cold_start_strategy: drop
  train_ratio: 0.8
  top_n: 20
  # 注：MovieLens 用户隔离采用"取负"方案，无需偏移量配置

flask:
  host: "127.0.0.1"
  port: 8000
  debug: false
  secret_key: "replace_with_a_random_secret"

cors:
  origins: ["http://localhost:5173", "http://127.0.0.1:5173"]

tmdb:      # fetch_posters.py 电影富化
vm:        # sync_movie_stats.py 虚拟机统计同步（仅本地开发）
```

> 所有代码与脚本从 `config.yaml` 读配置，禁止硬编码凭据。

### B. 依赖清单

**Python（`backend/requirements.txt`）**
```
Flask==3.0.3
Flask-Cors==4.0.1
Flask-JWT-Extended==4.6.0
Flask-SQLAlchemy==3.1.1
SQLAlchemy==2.0.40
PyMySQL==1.1.1
Werkzeug==3.0.3
PyYAML==6.0.1
numpy==1.26.4
scipy==1.13.1
```

Spark 侧无需 pip 依赖（集群 spark-submit 提供 PySpark），另需 MySQL Connector/J 8.0.33 放入 `$SPARK_HOME/jars/`。

**Node.js（`frontend/package.json`）**
```json
{
  "dependencies": {
    "vue": "^3.4.21",
    "vue-router": "^4.3.2",
    "pinia": "^2.1.7",
    "element-plus": "^2.7.3",
    "@element-plus/icons-vue": "^2.3.1",
    "axios": "^1.7.2"
  },
  "devDependencies": {
    "@vitejs/plugin-vue": "^5.0.4",
    "vite": "^5.2.10"
  }
}
```

### C. 测试与静态验证

```bash
python -m unittest discover -s tests -v        # 140 个用例：训练契约、评分表契约、
                                               # 评分统计、影评全链路、详情页 UI 契约、
                                               # 在线推荐、模型生命周期、SQL 契约等
python -m compileall -q backend spark scripts  # 语法检查
npm run build --prefix frontend                # 前端构建
git diff --check                               # 空白错误
```

**尚未覆盖**（需真实环境）：Spark/HDFS/JDBC 端到端训练、Flask 路由集成测试、迁移 CI、前端浏览器自动化测试。

### D. 参考资源

- [Hadoop 官方文档](https://hadoop.apache.org/docs/stable/)
- [Spark MLlib ALS](https://spark.apache.org/docs/latest/ml-collaborative-filtering.html)
- [MovieLens 数据集](https://grouplens.org/datasets/movielens/)
- [Flask 文档](https://flask.palletsprojects.com/)
- [Vue 3 文档](https://cn.vuejs.org/)

---
