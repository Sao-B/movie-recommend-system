<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="card-head">
        <span class="card-title">用户管理</span>
        <el-button type="primary" size="small" @click="openCreate">新增用户</el-button>
      </div>
    </template>

    <el-form class="search-bar" :inline="true">
      <el-form-item label="账号">
        <el-input v-model="keyword" placeholder="支持模糊搜索" clearable style="width: 220px" @clear="handleSearch" @keyup.enter="handleSearch" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="handleSearch">搜索</el-button>
        <el-button @click="handleReset">重置</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="users" v-loading="loading" border stripe @sort-change="handleSortChange">
      <el-table-column prop="id" label="ID" width="80" sortable="custom" />
      <el-table-column prop="username" label="账号" min-width="160" sortable="custom" />
      <el-table-column label="角色" width="120">
        <template #default="{ row }">
          <el-tag :type="row.role === 'admin' ? 'danger' : 'info'" size="small">
            {{ row.role === 'admin' ? '管理员' : '普通用户' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="注册时间" width="180" prop="created_at" sortable="custom">
        <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="没有找到用户" :image-size="80" />
      </template>
    </el-table>

    <el-pagination
      class="pager"
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[10, 20, 50]"
      layout="total, sizes, prev, pager, next"
      @size-change="handleSearch"
      @current-change="loadUsers"
    />
  </el-card>

  <el-dialog v-model="dialogVisible" :title="isEdit ? '编辑用户' : '新增用户'" width="440px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="80px">
      <el-form-item label="账号" prop="username">
        <el-input v-model="form.username" :disabled="isEdit" placeholder="3-20 位，字母 / 数字 / _ / -" />
      </el-form-item>
      <el-form-item label="密码" prop="password">
        <el-input v-model="form.password" type="password" show-password :placeholder="isEdit ? '留空表示不修改密码' : ''" />
      </el-form-item>
      <el-form-item label="角色" prop="role">
        <el-select v-model="form.role">
          <el-option label="普通用户" value="user" />
          <el-option label="管理员" value="admin" />
        </el-select>
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { computed, reactive, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiAdmin } from '@/api/admin'
import { formatDateTime } from '@/utils/format'
import { useTableQuery } from '@/composables/useTableQuery'

const keyword = ref('')

// 分页 / 排序 / 加载骨架见 composables/useTableQuery.js
const {
  loading, rows: users, total, page, pageSize,
  load: loadUsers, search: handleSearch, handleSortChange, reloadAfterDelete,
} = useTableQuery(async ({ page, pageSize, sortProp, sortOrder }) => {
  const data = await apiAdmin.listUsers({
    page,
    per_page: pageSize,
    q: keyword.value,
    ...(sortProp ? { sort: sortProp, order: sortOrder } : {}),
  })
  return { rows: data.users, total: data.total }
})

const dialogVisible = ref(false)
const isEdit = ref(false)
const formRef = ref(null)
const form = reactive({ id: null, username: '', password: '', role: 'user' })

// 编辑时密码可选，新增时必填
const rules = computed(() => ({
  // 账号规则与注册一致（后端 app/account_rules.py）；编辑态下账号框是灰的，不校验
  username: isEdit.value
    ? []
    : [
        { required: true, message: '请输入账号', trigger: 'blur' },
        { pattern: /^[A-Za-z0-9_-]{3,20}$/, message: '账号 3-20 位，只能包含字母、数字、下划线、连字符', trigger: 'blur' },
      ],
  password: isEdit.value ? [] : [{ required: true, message: '请输入密码', trigger: 'blur' }, { min: 6, message: '至少 6 个字符', trigger: 'blur' }],
  role: [{ required: true, message: '请选择角色', trigger: 'change' }]
}))

function handleReset() {
  keyword.value = ''
  handleSearch()
}

function openCreate() {
  isEdit.value = false
  Object.assign(form, { id: null, username: '', password: '', role: 'user' })
  dialogVisible.value = true
}

function openEdit(row) {
  isEdit.value = true
  Object.assign(form, { id: row.id, username: row.username, password: '', role: row.role })
  dialogVisible.value = true
}

async function handleSubmit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  if (isEdit.value) {
    const payload = { username: form.username, role: form.role }
    if (form.password) payload.password = form.password
    await apiAdmin.updateUser(form.id, payload)
    ElMessage.success('已保存')
  } else {
    await apiAdmin.createUser({ username: form.username, password: form.password, role: form.role })
    ElMessage.success('已创建')
  }
  dialogVisible.value = false
  loadUsers()
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除用户「${row.username}」后，其评分与评论也会一并清理。确定继续？`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  await apiAdmin.deleteUser(row.id)
  ElMessage.success('已删除')
  reloadAfterDelete()
}

onMounted(loadUsers)
</script>
