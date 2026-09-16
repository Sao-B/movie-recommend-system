<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="card-head">
        <span class="card-title">模型管理</span>
        <span class="hint">同一时刻只有一个模型参与线上推荐</span>
      </div>
    </template>

    <el-table :data="models" v-loading="loading" border stripe>
      <el-table-column prop="version" label="版本" min-width="160" sortable />
      <el-table-column prop="rank" label="隐因子数 (rank)" width="150" sortable />
      <el-table-column prop="reg_param" label="正则系数" width="110" sortable />
      <el-table-column label="状态" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="row.status === 'active' ? 'success' : 'info'">
            {{ row.status === 'active' ? '生效中' : '已下线' }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="训练时间" width="180" prop="generated_at" sortable>
        <template #default="{ row }">{{ formatDateTime(row.generated_at) }}</template>
      </el-table-column>
      <el-table-column label="激活时间" width="180" prop="activated_at" sortable>
        <template #default="{ row }">{{ formatDateTime(row.activated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="success" size="small" :disabled="row.status === 'active'" @click="handleActivate(row)">
            设为生效
          </el-button>
          <el-button link type="danger" size="small" :disabled="row.status === 'active'" @click="handleDelete(row)">
            删除
          </el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="还没有训练好的模型" :image-size="80" />
      </template>
    </el-table>
  </el-card>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiAdmin } from '@/api/admin'
import { formatDateTime } from '@/utils/format'

const loading = ref(false)
const models = ref([])

async function loadModels() {
  loading.value = true
  try {
    const data = await apiAdmin.listModels()
    models.value = data.models
  } finally {
    loading.value = false
  }
}

async function handleActivate(row) {
  try {
    await ElMessageBox.confirm(
      `切换到模型「${row.version}」后，当前生效模型会自动下线。`,
      '切换确认',
      { type: 'info', confirmButtonText: '切换', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  await apiAdmin.activateModel(row.version)
  ElMessage.success('已切换')
  loadModels()
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除模型「${row.version}」及其隐因子矩阵？该操作不可恢复。`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  await apiAdmin.deleteModel(row.version)
  ElMessage.success('已删除')
  loadModels()
}

onMounted(loadModels)
</script>

<style scoped>
.hint {
  font-size: 13px;
  color: var(--app-text-muted);
}
</style>
