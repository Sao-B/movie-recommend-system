# 后台管理系统实现计划

> **面向 AI 代理的工作者：** 必需子技能：使用 superpowers:subagent-driven-development（推荐）或 superpowers:executing-plans 逐任务实现此计划。步骤使用复选框（`- [ ]`）语法来跟踪进度。

**目标：** 为电影推荐系统创建管理员后台，支持用户管理、电影管理、评分管理、模型管理和统计数据展示。

**架构：** 在 User 模型添加 role 字段区分管理员和普通用户；后端创建 admin 蓝图提供管理 API，使用装饰器进行权限验证；前端创建独立的管理后台路由和页面，使用 Element Plus 组件构建 CRUD 界面。

**技术栈：** Flask + SQLAlchemy + JWT（后端），Vue 3 + Element Plus + Pinia（前端），MySQL 8.0

---

## 文件结构

### 数据库迁移
- `sql/migrations/002_add_user_role.sql` - 添加用户角色字段

### 后端文件
- 修改：`backend/app/models.py` - User 模型添加 role 字段和 is_admin() 方法
- 创建：`backend/app/routes/admin.py` - 管理 API 蓝图
- 修改：`backend/app/__init__.py` - 注册 admin 蓝图

### 前端文件
- 创建：`frontend/src/api/admin.js` - 管理 API 客户端
- 创建：`frontend/src/layouts/AdminLayout.vue` - 管理后台布局组件
- 创建：`frontend/src/views/admin/AdminDashboard.vue` - 仪表盘
- 创建：`frontend/src/views/admin/AdminUsers.vue` - 用户管理
- 创建：`frontend/src/views/admin/AdminMovies.vue` - 电影管理
- 创建：`frontend/src/views/admin/AdminRatings.vue` - 评分管理
- 创建：`frontend/src/views/admin/AdminModels.vue` - 模型管理
- 修改：`frontend/src/router/index.js` - 添加管理路由和权限守卫
- 修改：`frontend/src/stores/auth.js` - 存储用户角色信息
- 修改：`frontend/src/components/NavBar.vue` - 添加管理后台入口

---

## 任务 1：数据库迁移 - 添加用户角色

**文件：**
- 创建：`sql/migrations/002_add_user_role.sql`

- [ ] **步骤 1：创建迁移脚本**

```sql
-- 为 users 表添加 role 字段
USE movie_recommend;

ALTER TABLE users 
ADD COLUMN role ENUM('user', 'admin') DEFAULT 'user' 
COMMENT '用户角色：user=普通用户，admin=管理员';

CREATE INDEX idx_user_role ON users(role);

-- 创建第一个管理员账号（用户名：admin，密码：admin123）
-- 密码哈希使用 bcrypt，这里是 'admin123' 的哈希值
INSERT INTO users (username, password_hash, role, created_at) 
VALUES ('admin', 'scrypt:32768:8:1$0vQqF5xZGxAelqI2$46c3b5e9dd0f5e7c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c8e9c', 'admin', NOW())
ON DUPLICATE KEY UPDATE role = 'admin';

-- 注意：实际部署时，应通过后端 API 创建管理员并设置安全密码
```

- [ ] **步骤 2：执行迁移**

```bash
cd d:/Sundries/Paper/Project
mysql -u root -p"$MYSQL_PASSWORD" < sql/migrations/002_add_user_role.sql
```

预期：表结构成功修改，索引创建成功

- [ ] **步骤 3：验证迁移**

```bash
mysql -u root -p"$MYSQL_PASSWORD" -e "USE movie_recommend; DESC users;"
```

预期：输出包含 role 字段，类型为 enum('user','admin')

- [ ] **步骤 4：Commit**

```bash
git add sql/migrations/002_add_user_role.sql
git commit -m "db: add role field to users table for admin permissions"
```

---

## 任务 2：后端模型 - 扩展 User 模型

**文件：**
- 修改：`backend/app/models.py:11-26`

- [ ] **步骤 1：添加 role 字段到 User 模型**

在 `User` 类中，在 `created_at` 字段后添加：

```python
class User(db.Model):
    __tablename__ = "users"

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    username      = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at    = db.Column(db.DateTime, default=_utcnow)
    role          = db.Column(db.String(16), nullable=False, default='user', index=True)
```

- [ ] **步骤 2：添加 is_admin 方法**

在 `User` 类的 `to_dict` 方法前添加：

```python
    def is_admin(self) -> bool:
        """检查用户是否为管理员"""
        return self.role == 'admin'
```

- [ ] **步骤 3：更新 to_dict 方法包含 role**

修改 `to_dict` 方法：

```python
    def to_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "role": self.role,
            "created_at": self.created_at.isoformat() if self.created_at else None
        }
```

- [ ] **步骤 4：验证模型修改**

启动 Python shell 测试：

```bash
cd d:/Sundries/Paper/Project/backend
./venv/Scripts/python.exe -c "
from app.models import User
print('User model loaded successfully')
print('Has is_admin method:', hasattr(User, 'is_admin'))
"
```

预期：输出 "User model loaded successfully" 和 "Has is_admin method: True"

- [ ] **步骤 5：Commit**

```bash
git add backend/app/models.py
git commit -m "feat(backend): add role field and is_admin method to User model"
```

---

## 任务 3：后端权限装饰器

**文件：**
- 创建：`backend/app/routes/admin.py`

- [ ] **步骤 1：创建 admin.py 文件框架**

```python
from functools import wraps
from flask import Blueprint, request, jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from sqlalchemy import func
from ..db import db
from ..models import User, Movie, Rating, ModelVersion, Recommendation

admin_bp = Blueprint("admin", __name__)


def admin_required(fn):
    """管理员权限装饰器"""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = db.session.get(User, int(user_id))
        
        if not user or not user.is_admin():
            return jsonify({"msg": "需要管理员权限"}), 403
            
        return fn(*args, **kwargs)
    return wrapper
```

- [ ] **步骤 2：测试装饰器语法**

```bash
cd d:/Sundries/Paper/Project/backend
./venv/Scripts/python.exe -c "
import sys
sys.path.insert(0, '.')
from app.routes.admin import admin_required
print('admin_required decorator loaded successfully')
"
```

预期：输出 "admin_required decorator loaded successfully"

- [ ] **步骤 3：Commit**

```bash
git add backend/app/routes/admin.py
git commit -m "feat(backend): add admin_required decorator"
```

---

## 任务 4：后端 API - 统计数据接口

**文件：**
- 修改：`backend/app/routes/admin.py`

- [ ] **步骤 1：添加统计数据 API**

在 `admin.py` 文件末尾添加：

```python
@admin_bp.get("/stats")
@admin_required
def get_stats():
    """获取系统统计数据"""
    user_count = db.session.query(func.count(User.id)).scalar() or 0
    movie_count = db.session.query(func.count(Movie.movie_id)).scalar() or 0
    rating_count = db.session.query(func.count(Rating.id)).scalar() or 0
    recommendation_count = db.session.query(func.count(Recommendation.id)).scalar() or 0
    
    # 获取活跃模型信息
    active_model = ModelVersion.query.filter_by(status='active').first()
    
    return jsonify({
        "user_count": user_count,
        "movie_count": movie_count,
        "rating_count": rating_count,
        "recommendation_count": recommendation_count,
        "active_model": {
            "version": active_model.version if active_model else None,
            "rank": active_model.rank if active_model else None,
            "reg_param": active_model.reg_param if active_model else None,
            "activated_at": active_model.activated_at.isoformat() if active_model and active_model.activated_at else None
        } if active_model else None
    })
```

- [ ] **步骤 2：Commit**

```bash
git add backend/app/routes/admin.py
git commit -m "feat(backend): add admin stats API endpoint"
```

---

## 任务 5：后端 API - 用户管理接口

**文件：**
- 修改：`backend/app/routes/admin.py`

- [ ] **步骤 1：添加用户列表 API**

```python
@admin_bp.get("/users")
@admin_required
def list_users():
    """获取用户列表 - 支持分页和搜索"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 20, type=int))
    q = request.args.get("q", "").strip()
    
    query = User.query
    if q:
        query = query.filter(User.username.ilike(f"%{q}%"))
    
    pagination = query.order_by(User.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return jsonify({
        "users": [u.to_dict() for u in pagination.items],
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })
```

- [ ] **步骤 2：添加创建用户 API**

```python
@admin_bp.post("/users")
@admin_required
def create_user():
    """创建用户 - 管理员可指定角色"""
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()
    role = data.get("role", "user")
    
    if not username or not password:
        return jsonify({"msg": "用户名和密码不能为空"}), 400
    if role not in ["user", "admin"]:
        return jsonify({"msg": "角色必须是 user 或 admin"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"msg": "用户名已存在"}), 409
    
    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    
    return jsonify(user.to_dict()), 201
```

- [ ] **步骤 3：添加更新用户 API**

```python
@admin_bp.patch("/users/<int:user_id>")
@admin_required
def update_user(user_id):
    """更新用户信息"""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"msg": "用户不存在"}), 404
    
    data = request.get_json(silent=True) or {}
    
    if "username" in data:
        new_username = data["username"].strip()
        if new_username and new_username != user.username:
            if User.query.filter_by(username=new_username).first():
                return jsonify({"msg": "用户名已存在"}), 409
            user.username = new_username
    
    if "role" in data:
        if data["role"] not in ["user", "admin"]:
            return jsonify({"msg": "角色必须是 user 或 admin"}), 400
        user.role = data["role"]
    
    if "password" in data and data["password"]:
        user.set_password(data["password"])
    
    db.session.commit()
    return jsonify(user.to_dict())
```

- [ ] **步骤 4：添加删除用户 API**

```python
@admin_bp.delete("/users/<int:user_id>")
@admin_required
def delete_user(user_id):
    """删除用户 - 级联删除评分"""
    user = db.session.get(User, user_id)
    if not user:
        return jsonify({"msg": "用户不存在"}), 404
    
    # 防止删除自己
    current_user_id = int(get_jwt_identity())
    if user_id == current_user_id:
        return jsonify({"msg": "不能删除当前登录的管理员"}), 400
    
    db.session.delete(user)
    db.session.commit()
    return jsonify({"msg": "删除成功"})
```

- [ ] **步骤 5：Commit**

```bash
git add backend/app/routes/admin.py
git commit -m "feat(backend): add user management APIs (CRUD)"
```

---

## 任务 6：后端 API - 电影管理接口

**文件：**
- 修改：`backend/app/routes/admin.py`

- [ ] **步骤 1：添加电影列表 API**

```python
@admin_bp.get("/movies")
@admin_required
def list_movies():
    """获取电影列表 - 支持分页和搜索"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 20, type=int))
    q = request.args.get("q", "").strip()
    
    query = Movie.query
    if q:
        query = query.filter(Movie.title.ilike(f"%{q}%"))
    
    pagination = query.order_by(Movie.movie_id).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    return jsonify({
        "movies": [m.to_dict() for m in pagination.items],
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })
```

- [ ] **步骤 2：添加更新电影 API**

```python
@admin_bp.patch("/movies/<int:movie_id>")
@admin_required
def update_movie(movie_id):
    """更新电影信息"""
    movie = db.session.get(Movie, movie_id)
    if not movie:
        return jsonify({"msg": "电影不存在"}), 404
    
    data = request.get_json(silent=True) or {}
    
    if "title" in data and data["title"]:
        movie.title = data["title"].strip()
    
    if "genres" in data:
        if isinstance(data["genres"], list):
            movie.genres = "|".join(data["genres"])
        elif isinstance(data["genres"], str):
            movie.genres = data["genres"]
    
    if "year" in data:
        movie.year = data["year"]
    
    db.session.commit()
    return jsonify(movie.to_dict())
```

- [ ] **步骤 3：添加删除电影 API**

```python
@admin_bp.delete("/movies/<int:movie_id>")
@admin_required
def delete_movie(movie_id):
    """删除电影 - 级联删除评分和推荐"""
    movie = db.session.get(Movie, movie_id)
    if not movie:
        return jsonify({"msg": "电影不存在"}), 404
    
    db.session.delete(movie)
    db.session.commit()
    return jsonify({"msg": "删除成功"})
```

- [ ] **步骤 4：Commit**

```bash
git add backend/app/routes/admin.py
git commit -m "feat(backend): add movie management APIs"
```

---

## 任务 7：后端 API - 评分和模型管理接口

**文件：**
- 修改：`backend/app/routes/admin.py`

- [ ] **步骤 1：添加评分列表 API**

```python
@admin_bp.get("/ratings")
@admin_required
def list_ratings():
    """获取评分列表 - 支持按用户和电影筛选"""
    page = max(1, request.args.get("page", 1, type=int))
    per_page = min(100, request.args.get("per_page", 20, type=int))
    user_id = request.args.get("user_id", type=int)
    movie_id = request.args.get("movie_id", type=int)
    
    query = Rating.query
    if user_id:
        query = query.filter(Rating.user_id == user_id)
    if movie_id:
        query = query.filter(Rating.movie_id == movie_id)
    
    pagination = query.order_by(Rating.rated_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    
    # 丰富数据，包含用户名和电影标题
    items = []
    for rating in pagination.items:
        item = rating.to_dict()
        user = db.session.get(User, rating.user_id)
        item["username"] = user.username if user else None
        item["movie_title"] = rating.movie.title if rating.movie else None
        items.append(item)
    
    return jsonify({
        "ratings": items,
        "total": pagination.total,
        "page": page,
        "per_page": per_page,
        "pages": pagination.pages
    })
```

- [ ] **步骤 2：添加删除评分 API**

```python
@admin_bp.delete("/ratings/<int:rating_id>")
@admin_required
def delete_rating(rating_id):
    """删除评分"""
    rating = db.session.get(Rating, rating_id)
    if not rating:
        return jsonify({"msg": "评分不存在"}), 404
    
    db.session.delete(rating)
    db.session.commit()
    return jsonify({"msg": "删除成功"})
```

- [ ] **步骤 3：添加模型列表 API**

```python
@admin_bp.get("/models")
@admin_required
def list_models():
    """获取所有模型版本"""
    models = ModelVersion.query.order_by(ModelVersion.generated_at.desc()).all()
    
    return jsonify({
        "models": [{
            "version": m.version,
            "rank": m.rank,
            "reg_param": m.reg_param,
            "status": m.status,
            "generated_at": m.generated_at.isoformat() if m.generated_at else None,
            "activated_at": m.activated_at.isoformat() if m.activated_at else None
        } for m in models]
    })
```

- [ ] **步骤 4：添加激活模型 API**

```python
@admin_bp.post("/models/<string:version>/activate")
@admin_required
def activate_model(version):
    """激活指定模型版本"""
    model = db.session.get(ModelVersion, version)
    if not model:
        return jsonify({"msg": "模型不存在"}), 404
    
    # 将其他 active 模型设为 retired
    ModelVersion.query.filter_by(status='active').update({"status": "retired"})
    
    # 激活当前模型
    model.status = 'active'
    model.activated_at = func.now()
    db.session.commit()
    
    return jsonify({"msg": "模型已激活", "version": version})
```

- [ ] **步骤 5：添加删除模型 API**

```python
@admin_bp.delete("/models/<string:version>")
@admin_required
def delete_model(version):
    """删除模型 - 级联删除因子和推荐"""
    model = db.session.get(ModelVersion, version)
    if not model:
        return jsonify({"msg": "模型不存在"}), 404
    
    if model.status == 'active':
        return jsonify({"msg": "不能删除活跃模型，请先激活其他模型"}), 400
    
    db.session.delete(model)
    db.session.commit()
    return jsonify({"msg": "删除成功"})
```

- [ ] **步骤 6：Commit**

```bash
git add backend/app/routes/admin.py
git commit -m "feat(backend): add rating and model management APIs"
```

---

## 任务 8：注册 Admin 蓝图

**文件：**
- 修改：`backend/app/__init__.py`

- [ ] **步骤 1：导入 admin 蓝图**

在文件顶部的导入部分，在其他 blueprint 导入后添加：

```python
from .routes.admin import admin_bp
```

- [ ] **步骤 2：注册 admin 蓝图**

在蓝图注册部分（`app.register_blueprint` 调用处）添加：

```python
    app.register_blueprint(admin_bp, url_prefix="/api/admin")
```

- [ ] **步骤 3：测试后端 API 可访问性**

启动后端服务，测试管理 API：

```bash
# 先启动后端（如果未启动）
cd d:/Sundries/Paper/Project/backend
./venv/Scripts/python.exe ../scripts/start_backend.py
```

在另一个终端测试（需要先登录获取 token）：

```bash
curl http://localhost:5000/api/admin/stats
```

预期：返回 401 或 403 错误（因为未登录/非管理员）

- [ ] **步骤 4：Commit**

```bash
git add backend/app/__init__.py
git commit -m "feat(backend): register admin blueprint"
```

---

## 任务 9：前端 API 客户端

**文件：**
- 创建：`frontend/src/api/admin.js`

- [ ] **步骤 1：创建 admin API 客户端**

```javascript
import client from './client'

export const apiAdmin = {
  // 统计数据
  getStats() {
    return client.get('/admin/stats').then(r => r.data)
  },

  // 用户管理
  listUsers(params) {
    return client.get('/admin/users', { params }).then(r => r.data)
  },
  createUser(data) {
    return client.post('/admin/users', data).then(r => r.data)
  },
  updateUser(userId, data) {
    return client.patch(`/admin/users/${userId}`, data).then(r => r.data)
  },
  deleteUser(userId) {
    return client.delete(`/admin/users/${userId}`).then(r => r.data)
  },

  // 电影管理
  listMovies(params) {
    return client.get('/admin/movies', { params }).then(r => r.data)
  },
  updateMovie(movieId, data) {
    return client.patch(`/admin/movies/${movieId}`, data).then(r => r.data)
  },
  deleteMovie(movieId) {
    return client.delete(`/admin/movies/${movieId}`).then(r => r.data)
  },

  // 评分管理
  listRatings(params) {
    return client.get('/admin/ratings', { params }).then(r => r.data)
  },
  deleteRating(ratingId) {
    return client.delete(`/admin/ratings/${ratingId}`).then(r => r.data)
  },

  // 模型管理
  listModels() {
    return client.get('/admin/models').then(r => r.data)
  },
  activateModel(version) {
    return client.post(`/admin/models/${version}/activate`).then(r => r.data)
  },
  deleteModel(version) {
    return client.delete(`/admin/models/${version}`).then(r => r.data)
  }
}
```

- [ ] **步骤 2：验证语法**

```bash
cd d:/Sundries/Paper/Project/frontend
npm run build 2>&1 | head -20
```

预期：无语法错误

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/api/admin.js
git commit -m "feat(frontend): add admin API client"
```

---

## 任务 10：更新认证存储 - 保存用户角色

**文件：**
- 修改：`frontend/src/stores/auth.js`

- [ ] **步骤 1：查看当前 auth store 结构**

```bash
cat frontend/src/stores/auth.js
```

- [ ] **步骤 2：确保 user 对象包含 role 字段**

在 `login` 和 `register` 方法中，确保保存完整的 user 对象（包含 role）：

确认以下代码存在且正确保存 user：

```javascript
// 在 login 函数中
const { token, user } = await apiAuth.login(username, password)
this.token = token
this.user = user  // 确保 user 包含 role 字段
localStorage.setItem('token', token)
localStorage.setItem('user', JSON.stringify(user))
```

如果代码已正确，无需修改。如果缺少 user 存储，添加上述代码。

- [ ] **步骤 3：添加 isAdmin 计算属性**

在 `useAuthStore` 中添加：

```javascript
export const useAuthStore = defineStore('auth', () => {
  // ... 现有代码 ...

  const isAdmin = computed(() => user.value?.role === 'admin')

  return {
    // ... 现有返回值 ...
    isAdmin
  }
})
```

- [ ] **步骤 4：Commit**

```bash
git add frontend/src/stores/auth.js
git commit -m "feat(frontend): add isAdmin computed property to auth store"
```

---

## 任务 11：管理后台布局组件

**文件：**
- 创建：`frontend/src/layouts/AdminLayout.vue`

- [ ] **步骤 1：创建 AdminLayout 组件**

```vue
<template>
  <el-container class="admin-layout">
    <el-aside width="200px" class="admin-sidebar">
      <div class="logo">
        <el-icon><Management /></el-icon>
        <span>管理后台</span>
      </div>
      <el-menu
        :default-active="$route.path"
        router
        class="admin-menu"
      >
        <el-menu-item index="/admin/dashboard">
          <el-icon><DataLine /></el-icon>
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="/admin/users">
          <el-icon><User /></el-icon>
          <span>用户管理</span>
        </el-menu-item>
        <el-menu-item index="/admin/movies">
          <el-icon><Film /></el-icon>
          <span>电影管理</span>
        </el-menu-item>
        <el-menu-item index="/admin/ratings">
          <el-icon><Star /></el-icon>
          <span>评分管理</span>
        </el-menu-item>
        <el-menu-item index="/admin/models">
          <el-icon><Setting /></el-icon>
          <span>模型管理</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header class="admin-header">
        <el-breadcrumb separator="/">
          <el-breadcrumb-item :to="{ path: '/admin/dashboard' }">管理后台</el-breadcrumb-item>
          <el-breadcrumb-item v-if="breadcrumbTitle">{{ breadcrumbTitle }}</el-breadcrumb-item>
        </el-breadcrumb>
        <div>
          <el-button type="primary" text @click="goHome">返回前台</el-button>
        </div>
      </el-header>
      <el-main class="admin-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Management, DataLine, User, Film, Star, Setting } from '@element-plus/icons-vue'

const route = useRoute()
const router = useRouter()

const breadcrumbTitle = computed(() => {
  const map = {
    '/admin/dashboard': '仪表盘',
    '/admin/users': '用户管理',
    '/admin/movies': '电影管理',
    '/admin/ratings': '评分管理',
    '/admin/models': '模型管理'
  }
  return map[route.path]
})

function goHome() {
  router.push('/')
}
</script>

<style scoped>
.admin-layout {
  height: 100vh;
}

.admin-sidebar {
  background: #304156;
  color: #fff;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: bold;
  gap: 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.admin-menu {
  border: none;
  background: transparent;
}

.admin-header {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}

.admin-main {
  background: #f0f2f5;
  padding: 20px;
}
</style>
```

- [ ] **步骤 2：验证组件语法**

```bash
cd d:/Sundries/Paper/Project/frontend
npm run build 2>&1 | grep -i error | head -10
```

预期：无错误

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/layouts/AdminLayout.vue
git commit -m "feat(frontend): add admin layout component"
```

---

## 任务 12：管理后台 - 仪表盘页面

**文件：**
- 创建：`frontend/src/views/admin/AdminDashboard.vue`

- [ ] **步骤 1：创建 AdminDashboard 组件**

```vue
<template>
  <div class="admin-dashboard">
    <el-row :gutter="20" v-loading="loading">
      <el-col :span="6">
        <el-card class="stat-card">
          <el-statistic title="用户总数" :value="stats.user_count">
            <template #prefix>
              <el-icon><User /></el-icon>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card">
          <el-statistic title="电影总数" :value="stats.movie_count">
            <template #prefix>
              <el-icon><Film /></el-icon>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card">
          <el-statistic title="评分总数" :value="stats.rating_count">
            <template #prefix>
              <el-icon><Star /></el-icon>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card class="stat-card">
          <el-statistic title="推荐总数" :value="stats.recommendation_count">
            <template #prefix>
              <el-icon><DataLine /></el-icon>
            </template>
          </el-statistic>
        </el-card>
      </el-col>
    </el-row>

    <el-card class="model-card" v-if="stats.active_model">
      <template #header>
        <div class="card-header">
          <span>活跃推荐模型</span>
        </div>
      </template>
      <el-descriptions :column="2" border>
        <el-descriptions-item label="模型版本">{{ stats.active_model.version }}</el-descriptions-item>
        <el-descriptions-item label="隐因子维度">{{ stats.active_model.rank }}</el-descriptions-item>
        <el-descriptions-item label="正则化系数">{{ stats.active_model.reg_param }}</el-descriptions-item>
        <el-descriptions-item label="激活时间">{{ formatTime(stats.active_model.activated_at) }}</el-descriptions-item>
      </el-descriptions>
    </el-card>
    
    <el-empty v-else description="暂无活跃模型" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import { User, Film, Star, DataLine } from '@element-plus/icons-vue'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const stats = ref({
  user_count: 0,
  movie_count: 0,
  rating_count: 0,
  recommendation_count: 0,
  active_model: null
})

function formatTime(isoString) {
  if (!isoString) return '-'
  return new Date(isoString).toLocaleString('zh-CN')
}

async function fetchStats() {
  loading.value = true
  try {
    stats.value = await apiAdmin.getStats()
  } catch (err) {
    ElMessage.error('加载统计数据失败')
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  fetchStats()
})
</script>

<style scoped>
.admin-dashboard {
  max-width: 1200px;
}

.stat-card {
  margin-bottom: 20px;
}

.model-card {
  margin-top: 20px;
}

.card-header {
  font-weight: bold;
}
</style>
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/views/admin/AdminDashboard.vue
git commit -m "feat(frontend): add admin dashboard page"
```

---

## 任务 13：管理后台 - 用户管理页面

**文件：**
- 创建：`frontend/src/views/admin/AdminUsers.vue`

- [ ] **步骤 1：创建 AdminUsers 组件（第一部分：模板和数据）**

```vue
<template>
  <div class="admin-users">
    <el-card shadow="never">
      <template #header>
        <div class="card-header">
          <span>用户管理</span>
          <el-button type="primary" @click="showCreateDialog">新增用户</el-button>
        </div>
      </template>

      <!-- 搜索栏 -->
      <el-row :gutter="12" class="search-bar">
        <el-col :span="8">
          <el-input
            v-model="searchQuery"
            placeholder="搜索用户名..."
            clearable
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="handleSearch">搜索</el-button>
        </el-col>
      </el-row>

      <!-- 表格 -->
      <el-table :data="users" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="username" label="用户名" />
        <el-table-column prop="role" label="角色" width="120">
          <template #default="{ row }">
            <el-tag :type="row.role === 'admin' ? 'danger' : 'info'">
              {{ row.role === 'admin' ? '管理员' : '普通用户' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="注册时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="showEditDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="perPage"
          :total="total"
          layout="prev, pager, next, total"
          background
          @current-change="fetchUsers"
        />
      </div>
    </el-card>

    <!-- 新增/编辑对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="dialogMode === 'create' ? '新增用户' : '编辑用户'"
      width="500px"
    >
      <el-form :model="formData" :rules="formRules" ref="formRef" label-width="100px">
        <el-form-item label="用户名" prop="username">
          <el-input v-model="formData.username" />
        </el-form-item>
        <el-form-item label="密码" :prop="dialogMode === 'create' ? 'password' : ''">
          <el-input
            v-model="formData.password"
            type="password"
            :placeholder="dialogMode === 'edit' ? '留空表示不修改' : ''"
          />
        </el-form-item>
        <el-form-item label="角色" prop="role">
          <el-select v-model="formData.role">
            <el-option label="普通用户" value="user" />
            <el-option label="管理员" value="admin" />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>
```

- [ ] **步骤 2：创建 AdminUsers 组件（第二部分：脚本）**

```vue
<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const submitting = ref(false)
const users = ref([])
const total = ref(0)
const currentPage = ref(1)
const perPage = 20
const searchQuery = ref('')

const dialogVisible = ref(false)
const dialogMode = ref('create') // 'create' or 'edit'
const formRef = ref(null)
const formData = reactive({
  id: null,
  username: '',
  password: '',
  role: 'user'
})

const formRules = {
  username: [{ required: true, message: '请输入用户名', trigger: 'blur' }],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, message: '密码至少6位', trigger: 'blur' }
  ],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}

function formatTime(isoString) {
  if (!isoString) return '-'
  return new Date(isoString).toLocaleString('zh-CN')
}

async function fetchUsers() {
  loading.value = true
  try {
    const params = { page: currentPage.value, per_page: perPage }
    if (searchQuery.value) params.q = searchQuery.value
    const data = await apiAdmin.listUsers(params)
    users.value = data.users
    total.value = data.total
  } catch (err) {
    ElMessage.error('加载用户列表失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  currentPage.value = 1
  fetchUsers()
}

function showCreateDialog() {
  dialogMode.value = 'create'
  formData.id = null
  formData.username = ''
  formData.password = ''
  formData.role = 'user'
  dialogVisible.value = true
}

function showEditDialog(row) {
  dialogMode.value = 'edit'
  formData.id = row.id
  formData.username = row.username
  formData.password = ''
  formData.role = row.role
  dialogVisible.value = true
}

async function handleSubmit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  submitting.value = true
  try {
    if (dialogMode.value === 'create') {
      await apiAdmin.createUser(formData)
      ElMessage.success('创建成功')
    } else {
      const payload = { username: formData.username, role: formData.role }
      if (formData.password) payload.password = formData.password
      await apiAdmin.updateUser(formData.id, payload)
      ElMessage.success('更新成功')
    }
    dialogVisible.value = false
    fetchUsers()
  } catch (err) {
    // 错误已在 client.js 中处理
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除用户 "${row.username}" 吗？`, '警告', {
      type: 'warning'
    })
    await apiAdmin.deleteUser(row.id)
    ElMessage.success('删除成功')
    fetchUsers()
  } catch (err) {
    if (err !== 'cancel') {
      // 错误已在 client.js 中处理
    }
  }
}

onMounted(() => {
  fetchUsers()
})
</script>

<style scoped>
.card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.search-bar {
  margin-bottom: 20px;
}

.pagination {
  margin-top: 20px;
  display: flex;
  justify-content: center;
}
</style>
```

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/views/admin/AdminUsers.vue
git commit -m "feat(frontend): add admin users management page"
```

---

## 任务 14：管理后台 - 电影管理页面

**文件：**
- 创建：`frontend/src/views/admin/AdminMovies.vue`

- [ ] **步骤 1：创建 AdminMovies 组件**

```vue
<template>
  <div class="admin-movies">
    <el-card shadow="never">
      <template #header>
        <span>电影管理</span>
      </template>

      <!-- 搜索栏 -->
      <el-row :gutter="12" class="search-bar">
        <el-col :span="8">
          <el-input
            v-model="searchQuery"
            placeholder="搜索电影标题..."
            clearable
            @keyup.enter="handleSearch"
            @clear="handleSearch"
          >
            <template #prefix>
              <el-icon><Search /></el-icon>
            </template>
          </el-input>
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="handleSearch">搜索</el-button>
        </el-col>
      </el-row>

      <!-- 表格 -->
      <el-table :data="movies" v-loading="loading" stripe>
        <el-table-column prop="movie_id" label="ID" width="80" />
        <el-table-column prop="title" label="标题" min-width="300" />
        <el-table-column prop="genres" label="类型" width="200">
          <template #default="{ row }">
            <el-tag v-for="g in row.genres.slice(0, 2)" :key="g" size="small" class="genre-tag">
              {{ g }}
            </el-tag>
            <span v-if="row.genres.length > 2">...</span>
          </template>
        </el-table-column>
        <el-table-column prop="year" label="年份" width="100" />
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="showEditDialog(row)">编辑</el-button>
            <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="perPage"
          :total="total"
          layout="prev, pager, next, total"
          background
          @current-change="fetchMovies"
        />
      </div>
    </el-card>

    <!-- 编辑对话框 -->
    <el-dialog v-model="dialogVisible" title="编辑电影" width="500px">
      <el-form :model="formData" ref="formRef" label-width="100px">
        <el-form-item label="标题">
          <el-input v-model="formData.title" />
        </el-form-item>
        <el-form-item label="类型">
          <el-input v-model="formData.genresText" placeholder="用竖线分隔，如：Action|Drama" />
        </el-form-item>
        <el-form-item label="年份">
          <el-input-number v-model="formData.year" :min="1800" :max="2100" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleSubmit" :loading="submitting">确定</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Search } from '@element-plus/icons-vue'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const submitting = ref(false)
const movies = ref([])
const total = ref(0)
const currentPage = ref(1)
const perPage = 20
const searchQuery = ref('')

const dialogVisible = ref(false)
const formRef = ref(null)
const formData = reactive({
  movie_id: null,
  title: '',
  genresText: '',
  year: null
})

async function fetchMovies() {
  loading.value = true
  try {
    const params = { page: currentPage.value, per_page: perPage }
    if (searchQuery.value) params.q = searchQuery.value
    const data = await apiAdmin.listMovies(params)
    movies.value = data.movies
    total.value = data.total
  } catch (err) {
    ElMessage.error('加载电影列表失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  currentPage.value = 1
  fetchMovies()
}

function showEditDialog(row) {
  formData.movie_id = row.movie_id
  formData.title = row.title
  formData.genresText = row.genres.join('|')
  formData.year = row.year
  dialogVisible.value = true
}

async function handleSubmit() {
  submitting.value = true
  try {
    const payload = {
      title: formData.title,
      genres: formData.genresText,
      year: formData.year
    }
    await apiAdmin.updateMovie(formData.movie_id, payload)
    ElMessage.success('更新成功')
    dialogVisible.value = false
    fetchMovies()
  } catch (err) {
    // 错误已在 client.js 中处理
  } finally {
    submitting.value = false
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除电影 "${row.title}" 吗？这将同时删除相关的评分和推荐。`,
      '警告',
      { type: 'warning' }
    )
    await apiAdmin.deleteMovie(row.movie_id)
    ElMessage.success('删除成功')
    fetchMovies()
  } catch (err) {
    if (err !== 'cancel') {
      // 错误已在 client.js 中处理
    }
  }
}

onMounted(() => {
  fetchMovies()
})
</script>

<style scoped>
.search-bar {
  margin-bottom: 20px;
}

.genre-tag {
  margin-right: 4px;
}

.pagination {
  margin-top: 20px;
  display: flex;
  justify-content: center;
}
</style>
```

- [ ] **步骤 2：Commit**

```bash
git add frontend/src/views/admin/AdminMovies.vue
git commit -m "feat(frontend): add admin movies management page"
```

---

## 任务 15：管理后台 - 评分和模型管理页面

**文件：**
- 创建：`frontend/src/views/admin/AdminRatings.vue`
- 创建：`frontend/src/views/admin/AdminModels.vue`

- [ ] **步骤 1：创建 AdminRatings 组件**

```vue
<template>
  <div class="admin-ratings">
    <el-card shadow="never">
      <template #header>
        <span>评分管理</span>
      </template>

      <!-- 筛选栏 -->
      <el-row :gutter="12" class="filter-bar">
        <el-col :span="6">
          <el-input-number
            v-model="filterUserId"
            placeholder="用户ID"
            :min="0"
            controls-position="right"
            style="width: 100%"
          />
        </el-col>
        <el-col :span="6">
          <el-input-number
            v-model="filterMovieId"
            placeholder="电影ID"
            :min="0"
            controls-position="right"
            style="width: 100%"
          />
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="handleFilter">筛选</el-button>
          <el-button @click="resetFilter">重置</el-button>
        </el-col>
      </el-row>

      <!-- 表格 -->
      <el-table :data="ratings" v-loading="loading" stripe>
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="user_id" label="用户ID" width="100" />
        <el-table-column prop="username" label="用户名" width="150" />
        <el-table-column prop="movie_id" label="电影ID" width="100" />
        <el-table-column prop="movie_title" label="电影标题" min-width="300" show-overflow-tooltip />
        <el-table-column prop="rating" label="评分" width="80" />
        <el-table-column prop="rated_at" label="评分时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.rated_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="perPage"
          :total="total"
          layout="prev, pager, next, total"
          background
          @current-change="fetchRatings"
        />
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const ratings = ref([])
const total = ref(0)
const currentPage = ref(1)
const perPage = 20
const filterUserId = ref(null)
const filterMovieId = ref(null)

function formatTime(isoString) {
  if (!isoString) return '-'
  return new Date(isoString).toLocaleString('zh-CN')
}

async function fetchRatings() {
  loading.value = true
  try {
    const params = { page: currentPage.value, per_page: perPage }
    if (filterUserId.value) params.user_id = filterUserId.value
    if (filterMovieId.value) params.movie_id = filterMovieId.value
    const data = await apiAdmin.listRatings(params)
    ratings.value = data.ratings
    total.value = data.total
  } catch (err) {
    ElMessage.error('加载评分列表失败')
  } finally {
    loading.value = false
  }
}

function handleFilter() {
  currentPage.value = 1
  fetchRatings()
}

function resetFilter() {
  filterUserId.value = null
  filterMovieId.value = null
  handleFilter()
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(`确定删除此评分吗？`, '警告', { type: 'warning' })
    await apiAdmin.deleteRating(row.id)
    ElMessage.success('删除成功')
    fetchRatings()
  } catch (err) {
    if (err !== 'cancel') {
      // 错误已在 client.js 中处理
    }
  }
}

onMounted(() => {
  fetchRatings()
})
</script>

<style scoped>
.filter-bar {
  margin-bottom: 20px;
}

.pagination {
  margin-top: 20px;
  display: flex;
  justify-content: center;
}
</style>
```

- [ ] **步骤 2：创建 AdminModels 组件**

```vue
<template>
  <div class="admin-models">
    <el-card shadow="never">
      <template #header>
        <span>模型管理</span>
      </template>

      <!-- 表格 -->
      <el-table :data="models" v-loading="loading" stripe>
        <el-table-column prop="version" label="版本号" min-width="250" />
        <el-table-column prop="rank" label="隐因子维度" width="120" />
        <el-table-column prop="reg_param" label="正则化系数" width="120" />
        <el-table-column prop="status" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)">
              {{ statusText(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="generated_at" label="生成时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.generated_at) }}
          </template>
        </el-table-column>
        <el-table-column prop="activated_at" label="激活时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.activated_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button
              v-if="row.status !== 'active'"
              size="small"
              type="success"
              @click="handleActivate(row)"
            >
              激活
            </el-button>
            <el-button
              v-if="row.status !== 'active'"
              size="small"
              type="danger"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
            <el-tag v-else type="success">当前生效</el-tag>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const models = ref([])

function formatTime(isoString) {
  if (!isoString) return '-'
  return new Date(isoString).toLocaleString('zh-CN')
}

function statusType(status) {
  const map = { building: 'warning', active: 'success', retired: 'info' }
  return map[status] || 'info'
}

function statusText(status) {
  const map = { building: '构建中', active: '已激活', retired: '已废弃' }
  return map[status] || status
}

async function fetchModels() {
  loading.value = true
  try {
    const data = await apiAdmin.listModels()
    models.value = data.models
  } catch (err) {
    ElMessage.error('加载模型列表失败')
  } finally {
    loading.value = false
  }
}

async function handleActivate(row) {
  try {
    await ElMessageBox.confirm(
      `确定激活模型 "${row.version}" 吗？当前活跃模型将被废弃。`,
      '确认',
      { type: 'warning' }
    )
    await apiAdmin.activateModel(row.version)
    ElMessage.success('激活成功')
    fetchModels()
  } catch (err) {
    if (err !== 'cancel') {
      // 错误已在 client.js 中处理
    }
  }
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `确定删除模型 "${row.version}" 吗？这将同时删除模型因子和批量推荐数据。`,
      '警告',
      { type: 'warning' }
    )
    await apiAdmin.deleteModel(row.version)
    ElMessage.success('删除成功')
    fetchModels()
  } catch (err) {
    if (err !== 'cancel') {
      // 错误已在 client.js 中处理
    }
  }
}

onMounted(() => {
  fetchModels()
})
</script>
```

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/views/admin/AdminRatings.vue frontend/src/views/admin/AdminModels.vue
git commit -m "feat(frontend): add admin ratings and models management pages"
```

---

## 任务 16：配置管理后台路由

**文件：**
- 修改：`frontend/src/router/index.js`

- [ ] **步骤 1：导入管理布局和页面组件**

在文件顶部添加导入：

```javascript
import AdminLayout from '@/layouts/AdminLayout.vue'
```

- [ ] **步骤 2：添加管理路由配置**

在 `routes` 数组中添加管理路由：

```javascript
const routes = [
  // ... 现有路由 ...
  {
    path: '/admin',
    component: AdminLayout,
    meta: { requiresAuth: true, requiresAdmin: true },
    redirect: '/admin/dashboard',
    children: [
      {
        path: 'dashboard',
        name: 'admin-dashboard',
        component: () => import('@/views/admin/AdminDashboard.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'users',
        name: 'admin-users',
        component: () => import('@/views/admin/AdminUsers.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'movies',
        name: 'admin-movies',
        component: () => import('@/views/admin/AdminMovies.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'ratings',
        name: 'admin-ratings',
        component: () => import('@/views/admin/AdminRatings.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      },
      {
        path: 'models',
        name: 'admin-models',
        component: () => import('@/views/admin/AdminModels.vue'),
        meta: { requiresAuth: true, requiresAdmin: true }
      }
    ]
  }
]
```

- [ ] **步骤 3：更新路由守卫检查管理员权限**

修改 `router.beforeEach` 添加管理员权限检查：

```javascript
router.beforeEach((to) => {
  const auth = useAuthStore()
  
  // 检查需要登录的路由
  if (to.meta.requiresAuth && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  
  // 检查需要管理员权限的路由
  if (to.meta.requiresAdmin && !auth.isAdmin) {
    ElMessage.error('需要管理员权限')
    return { name: 'home' }
  }
  
  // 已登录用户访问登录/注册页面，重定向到首页
  if ((to.name === 'login' || to.name === 'register') && auth.isLoggedIn) {
    return { name: 'home' }
  }
})
```

注意：需要在文件顶部导入 ElMessage：

```javascript
import { ElMessage } from 'element-plus'
```

- [ ] **步骤 4：验证路由配置**

```bash
cd d:/Sundries/Paper/Project/frontend
npm run build 2>&1 | grep -i error | head -10
```

预期：无错误

- [ ] **步骤 5：Commit**

```bash
git add frontend/src/router/index.js
git commit -m "feat(frontend): add admin routes and permission guards"
```

---

## 任务 17：添加管理后台入口

**文件：**
- 修改：`frontend/src/components/NavBar.vue`

- [ ] **步骤 1：查看 NavBar 组件结构**

```bash
cat frontend/src/components/NavBar.vue | head -50
```

- [ ] **步骤 2：在导航栏添加管理后台链接**

在用户已登录的菜单项中，添加管理后台入口（仅管理员可见）。

找到导航栏的用户菜单部分，添加：

```vue
<!-- 在用户下拉菜单中添加 -->
<el-menu-item v-if="auth.isAdmin" index="/admin/dashboard">
  <el-icon><Setting /></el-icon>
  <span>管理后台</span>
</el-menu-item>
```

如果是下拉菜单形式，则：

```vue
<el-dropdown-item v-if="auth.isAdmin" @click="router.push('/admin/dashboard')">
  管理后台
</el-dropdown-item>
```

确保导入必要的图标和依赖：

```javascript
import { Setting } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
```

- [ ] **步骤 3：Commit**

```bash
git add frontend/src/components/NavBar.vue
git commit -m "feat(frontend): add admin panel entry in navbar"
```

---

## 任务 18：端到端测试 - 创建管理员账号

**文件：**
- 测试脚本

- [ ] **步骤 1：通过数据库创建管理员账号**

```bash
mysql -u root -p"$MYSQL_PASSWORD" -e "
USE movie_recommend;
-- 先尝试注册一个新用户（通过后端API），然后将其升级为管理员
-- 或者直接在数据库中设置现有用户为管理员
UPDATE users SET role = 'admin' WHERE username = 'admin';
SELECT id, username, role FROM users WHERE role = 'admin';
"
```

预期：显示管理员用户信息

- [ ] **步骤 2：启动前后端服务**

```bash
cd d:/Sundries/Paper/Project
start.bat
```

预期：Flask 和 Vue 开发服务器都启动

- [ ] **步骤 3：登录管理员账号**

1. 打开浏览器访问 `http://localhost:5173`
2. 使用管理员账号登录（username: admin）
3. 检查导航栏是否显示"管理后台"入口

预期：管理员可见管理后台入口

- [ ] **步骤 4：访问管理后台仪表盘**

点击"管理后台"链接，访问 `/admin/dashboard`

预期：
- 显示统计数据卡片（用户数、电影数、评分数、推荐数）
- 显示活跃模型信息
- 左侧显示管理菜单

- [ ] **步骤 5：测试用户管理功能**

1. 点击"用户管理"菜单
2. 尝试搜索用户
3. 点击"新增用户"创建一个测试用户
4. 编辑用户角色
5. 删除测试用户

预期：所有 CRUD 操作正常工作

- [ ] **步骤 6：测试权限控制**

1. 退出登录
2. 使用普通用户账号登录
3. 尝试访问 `/admin/dashboard`

预期：被拒绝访问，提示"需要管理员权限"，重定向到首页

- [ ] **步骤 7：Commit 测试通过标记**

```bash
git add .
git commit -m "test: verify admin system end-to-end functionality"
```

---

## 任务 19：文档更新

**文件：**
- 修改：`README.md`
- 创建：`docs/admin-guide.md`

- [ ] **步骤 1：创建管理员使用指南**

```markdown
# 管理后台使用指南

## 访问管理后台

1. 使用管理员账号登录系统
2. 点击导航栏右上角的"管理后台"链接
3. 或直接访问 `http://localhost:5173/admin/dashboard`

## 功能说明

### 仪表盘
- 查看系统统计数据：用户数、电影数、评分数、推荐数
- 查看当前活跃的推荐模型信息

### 用户管理
- **列表查看**：分页浏览所有用户
- **搜索**：按用户名搜索
- **创建用户**：管理员可创建新用户并指定角色
- **编辑用户**：修改用户名、角色、密码
- **删除用户**：删除用户（会级联删除其评分）

### 电影管理
- **列表查看**：分页浏览所有电影
- **搜索**：按电影标题搜索
- **编辑电影**：修改电影标题、类型、年份
- **删除电影**：删除电影（会级联删除相关评分和推荐）

### 评分管理
- **列表查看**：分页浏览所有评分
- **筛选**：按用户ID或电影ID筛选
- **删除评分**：删除指定评分记录

### 模型管理
- **列表查看**：查看所有 ALS 模型版本
- **激活模型**：切换活跃模型（用于在线推荐）
- **删除模型**：删除废弃模型（会级联删除模型因子和批量推荐）

## 权限说明

- 只有 `role='admin'` 的用户可以访问管理后台
- 普通用户访问管理路由会被拦截并提示权限不足
- 管理员不能删除自己的账号

## 创建管理员

### 方法 1：数据库直接设置
```sql
USE movie_recommend;
UPDATE users SET role = 'admin' WHERE username = 'your_username';
```

### 方法 2：通过管理后台创建
已有管理员可通过"用户管理"页面创建新管理员，选择角色为"管理员"。

## 安全建议

1. 管理员账号应使用强密码
2. 定期审查管理员账号列表
3. 删除操作不可恢复，请谨慎操作
4. 生产环境建议添加操作日志审计功能
```

保存到 `docs/admin-guide.md`

- [ ] **步骤 2：更新 README.md**

在 README.md 中添加管理后台相关说明：

```markdown
## 管理后台

系统提供管理后台用于系统管理和数据维护。

### 访问方式
- URL: `http://localhost:5173/admin/dashboard`
- 需要管理员权限

### 创建管理员
```bash
mysql -u root -p -e "USE movie_recommend; UPDATE users SET role='admin' WHERE username='your_username';"
```

详细使用说明见 [管理后台使用指南](docs/admin-guide.md)
```

- [ ] **步骤 3：Commit**

```bash
git add README.md docs/admin-guide.md
git commit -m "docs: add admin system documentation"
```

---

## 任务 20：最终验证和优化

**文件：**
- 全项目

- [ ] **步骤 1：完整功能测试清单**

依次测试以下功能：

1. ✅ 管理员登录
2. ✅ 仪表盘数据正确显示
3. ✅ 用户管理 CRUD 全流程
4. ✅ 电影管理编辑和删除
5. ✅ 评分管理筛选和删除
6. ✅ 模型管理激活和删除
7. ✅ 普通用户权限隔离
8. ✅ 导航栏入口显示逻辑

- [ ] **步骤 2：性能检查**

```bash
# 检查后端 API 响应时间
curl -w "@curl-format.txt" -o /dev/null -s http://localhost:5000/api/admin/stats

# 检查前端构建大小
cd d:/Sundries/Paper/Project/frontend
npm run build
ls -lh dist/assets/*.js
```

预期：管理页面资源合理，无明显性能问题

- [ ] **步骤 3：代码质量检查**

```bash
# 后端代码检查
cd d:/Sundries/Paper/Project/backend
./venv/Scripts/python.exe -m pylint app/routes/admin.py 2>&1 | grep -E "(rated|error)" | head -20

# 前端代码检查
cd d:/Sundries/Paper/Project/frontend
npm run lint 2>&1 | grep -i error | head -20
```

预期：无严重错误

- [ ] **步骤 4：最终 Commit**

```bash
git add .
git commit -m "feat: complete admin system implementation

- Add role field to User model
- Implement admin APIs for user/movie/rating/model management
- Create admin layout and pages (dashboard, users, movies, ratings, models)
- Add permission guards and route protection
- Update documentation

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 完成检查清单

- [ ] 数据库迁移成功，role 字段已添加
- [ ] 后端所有管理 API 正常工作
- [ ] 前端所有管理页面正常显示
- [ ] 权限控制正常（管理员可访问，普通用户被拦截）
- [ ] CRUD 操作全部测试通过
- [ ] 文档已更新
- [ ] 代码已提交到 Git

## 后续优化建议

1. **操作日志**：记录管理员操作到日志表，便于审计
2. **批量操作**：支持批量删除用户、电影、评分
3. **数据导出**：支持导出 Excel/CSV 格式的数据
4. **图表统计**：在仪表盘添加图表展示（评分趋势、用户增长等）
5. **角色细分**：扩展为更细粒度的权限系统（如只读管理员）
6. **API 限流**：为管理 API 添加速率限制，防止滥用
7. **前端优化**：表格虚拟滚动，处理大量数据时的性能优化
