# 后台管理系统实现计划

## 一、功能设计

### 1. 管理员权限系统
**方案：** 在现有 users 表基础上添加角色字段，实现简单的 RBAC

**数据库变更：**
```sql
ALTER TABLE users ADD COLUMN role ENUM('user', 'admin') DEFAULT 'user' COMMENT '用户角色';
CREATE INDEX idx_user_role ON users(role);
```

**认证策略：**
- 管理员账号通过数据库直接创建（手动 UPDATE role='admin'）
- 后台 API 路由使用装饰器检查 role='admin'
- 前端根据用户角色显示/隐藏管理入口

---

### 2. 后端 API 设计

创建新的蓝图 `backend/app/routes/admin.py`，包含以下接口：

#### 2.1 统计数据
```
GET /api/admin/stats
返回：用户总数、电影总数、评分总数、推荐总数、活跃模型版本
```

#### 2.2 用户管理
```
GET    /api/admin/users?page=1&per_page=20&q=username
POST   /api/admin/users          # 创建用户（可指定角色）
PATCH  /api/admin/users/<id>     # 修改用户信息（用户名、角色）
DELETE /api/admin/users/<id>     # 删除用户（级联删除评分）
```

#### 2.3 电影管理
```
GET    /api/admin/movies?page=1&per_page=20&q=title
PATCH  /api/admin/movies/<id>    # 修改电影信息（标题、类型、年份）
DELETE /api/admin/movies/<id>    # 删除电影（级联删除评分、推荐）
```

#### 2.4 评分管理
```
GET    /api/admin/ratings?page=1&per_page=20&user_id=&movie_id=
DELETE /api/admin/ratings/<id>   # 删除评分
```

#### 2.5 模型管理
```
GET    /api/admin/models         # 列出所有模型版本
POST   /api/admin/models/<version>/activate  # 激活指定模型
DELETE /api/admin/models/<version>            # 删除模型（含因子和推荐）
```

#### 2.6 推荐管理
```
GET /api/admin/recommendations/stats  # 推荐统计（各模型覆盖用户数）
```

---

### 3. 前端页面设计

#### 3.1 路由结构
```
/admin                        # 管理后台根路由（重定向到 /admin/dashboard）
/admin/dashboard              # 仪表盘（统计数据）
/admin/users                  # 用户管理
/admin/movies                 # 电影管理
/admin/ratings                # 评分管理
/admin/models                 # 模型管理
```

#### 3.2 页面布局
- **左侧导航菜单**：Dashboard、用户、电影、评分、模型
- **顶部面包屑**：显示当前位置
- **内容区**：表格 + 分页 + 操作按钮

#### 3.3 组件复用
- 使用 Element Plus 的 `el-table`、`el-pagination`、`el-dialog`
- 表格操作列：编辑、删除按钮
- 对话框：新增/编辑表单
- 确认提示：删除操作使用 `ElMessageBox.confirm`

---

## 二、实现步骤

### Phase 1: 数据库和后端基础
1. ✅ **迁移脚本**：添加 role 字段到 users 表
2. ✅ **权限装饰器**：创建 `@admin_required` 装饰器检查 JWT 中的 role
3. ✅ **Admin 模型方法**：在 models.py 中为 User 添加 `is_admin()` 方法
4. ✅ **Admin 蓝图**：创建 `admin.py`，实现所有管理 API

### Phase 2: 前端路由和布局
5. ✅ **路由配置**：在 `router/index.js` 添加 `/admin/*` 路由，meta 标记 `requiresAdmin: true`
6. ✅ **路由守卫**：检查用户角色，非管理员禁止访问
7. ✅ **AdminLayout 组件**：左侧菜单 + 右侧内容区的布局组件
8. ✅ **导航菜单**：顶部导航栏添加"管理后台"入口（仅管理员可见）

### Phase 3: 管理页面实现
9. ✅ **Dashboard 页面**：卡片展示统计数据
10. ✅ **用户管理页面**：表格 + CRUD 操作
11. ✅ **电影管理页面**：表格 + 编辑/删除
12. ✅ **评分管理页面**：表格 + 删除
13. ✅ **模型管理页面**：表格 + 激活/删除操作

### Phase 4: API 集成和测试
14. ✅ **API 客户端**：在 `frontend/src/api/admin.js` 封装所有管理 API
15. ✅ **前后端联调**：测试所有 CRUD 操作
16. ✅ **错误处理**：统一错误提示和加载状态

---

## 三、技术细节

### 3.1 权限装饰器实现
```python
# backend/app/routes/admin.py
from functools import wraps
from flask import jsonify
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from ..models import User

def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        verify_jwt_in_request()
        user_id = get_jwt_identity()
        user = User.query.get(int(user_id))
        if not user or user.role != 'admin':
            return jsonify({"msg": "需要管理员权限"}), 403
        return fn(*args, **kwargs)
    return wrapper
```

### 3.2 前端路由守卫
```javascript
// frontend/src/router/index.js
router.beforeEach((to) => {
  const auth = useAuthStore()
  
  // 检查管理员路由
  if (to.meta.requiresAdmin) {
    if (!auth.isLoggedIn) {
      return { name: 'login', query: { redirect: to.fullPath } }
    }
    if (auth.user?.role !== 'admin') {
      ElMessage.error('需要管理员权限')
      return { name: 'home' }
    }
  }
  
  // 原有认证检查...
})
```

### 3.3 AdminLayout 布局
```vue
<template>
  <el-container class="admin-layout">
    <el-aside width="200px">
      <el-menu :default-active="$route.path" router>
        <el-menu-item index="/admin/dashboard">仪表盘</el-menu-item>
        <el-menu-item index="/admin/users">用户管理</el-menu-item>
        <el-menu-item index="/admin/movies">电影管理</el-menu-item>
        <el-menu-item index="/admin/ratings">评分管理</el-menu-item>
        <el-menu-item index="/admin/models">模型管理</el-menu-item>
      </el-menu>
    </el-aside>
    <el-main>
      <router-view />
    </el-main>
  </el-container>
</template>
```

---

## 四、文件清单

### 新增文件
```
backend/
  app/routes/admin.py                    # 管理 API 蓝图
  
frontend/src/
  api/admin.js                           # 管理 API 客户端
  views/admin/
    AdminDashboard.vue                   # 仪表盘
    AdminUsers.vue                       # 用户管理
    AdminMovies.vue                      # 电影管理
    AdminRatings.vue                     # 评分管理
    AdminModels.vue                      # 模型管理
  layouts/AdminLayout.vue                # 管理后台布局
  
sql/migrations/
  002_add_user_role.sql                  # 添加角色字段的迁移脚本
```

### 修改文件
```
backend/app/__init__.py                  # 注册 admin_bp
backend/app/models.py                    # User 模型添加 role 字段和方法
frontend/src/router/index.js             # 添加管理路由和守卫
frontend/src/stores/auth.js              # 存储用户角色信息
frontend/src/components/NavBar.vue       # 添加管理入口链接
```

---

## 五、安全考虑

1. **SQL 注入防护**：使用 SQLAlchemy ORM，避免拼接 SQL
2. **XSS 防护**：Element Plus 自动转义，用户输入不直接渲染 HTML
3. **CSRF 防护**：JWT 存储在 localStorage，不使用 Cookie
4. **权限验证**：每个管理 API 都使用 `@admin_required` 装饰器
5. **级联删除**：删除用户/电影时级联删除关联数据，保持数据一致性
6. **操作日志**：（可选）记录管理员操作到日志表

---

## 六、UI 设计参考

### Dashboard 卡片
```
┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
│   用户总数   │  │  电影总数   │  │  评分总数   │  │  推荐总数   │
│    1,234    │  │   87,585    │  │   45,678    │  │  123,456    │
└─────────────┘  └─────────────┘  └─────────────┘  └─────────────┘

┌──────────────────────────────────────────────────────────────────┐
│ 活跃模型: als-20260909T120000Z-abc123                            │
│ 隐因子维度: 50 | 正则化系数: 0.1 | 激活时间: 2026-09-09 12:00:00  │
└──────────────────────────────────────────────────────────────────┘
```

### 表格操作列
```
操作: [编辑] [删除]
```

---

## 七、预期效果

完成后，管理员可以：
- 📊 查看系统统计数据
- 👥 管理用户（创建、编辑、删除、设置角色）
- 🎬 管理电影（编辑信息、删除电影）
- ⭐ 管理评分（查看、删除）
- 🤖 管理模型（查看版本、激活模型、删除旧模型）

普通用户：
- 不可见管理后台入口
- 访问管理路由时被拦截并提示权限不足
