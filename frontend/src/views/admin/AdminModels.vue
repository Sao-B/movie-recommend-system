<template>
  <div class="admin-models">
    <el-card>
      <template #header>
        <span>模型管理</span>
      </template>

      <el-table :data="models" v-loading="loading" border stripe>
        <el-table-column prop="version" label="版本" width="150" />
        <el-table-column prop="rank" label="Rank" width="100" />
        <el-table-column prop="max_iter" label="最大迭代" width="120" />
        <el-table-column prop="reg_param" label="正则化系数" width="120" />
        <el-table-column prop="rmse" label="RMSE" width="120">
          <template #default="{ row }">
            <el-tag :type="row.rmse < 0.85 ? 'success' : 'warning'">
              {{ row.rmse.toFixed(4) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="is_active" label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="row.is_active ? 'success' : 'info'">
              {{ row.is_active ? '激活' : '未激活' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.created_at) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="200">
          <template #default="{ row }">
            <el-button
              size="small"
              type="success"
              :disabled="row.is_active"
              @click="handleActivate(row)"
            >
              激活
            </el-button>
            <el-button
              size="small"
              type="danger"
              :disabled="row.is_active"
              @click="handleDelete(row)"
            >
              删除
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="model-info">
        <el-alert
          title="说明"
          type="info"
          :closable="false"
          style="margin-top: 20px;"
        >
          <p>• 激活的模型将用于生成推荐结果</p>
          <p>• 只能有一个激活的模型</p>
          <p>• RMSE 越低表示模型预测越准确（目标 < 0.85）</p>
          <p>• 激活的模型不能删除</p>
        </el-alert>
      </div>
    </el-card>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiAdmin } from '@/api/admin'
import { ElMessage, ElMessageBox } from 'element-plus'

const loading = ref(false)
const models = ref([])

const loadModels = async () => {
  loading.value = true
  try {
    const data = await apiAdmin.listModels()
    models.value = data.models
  } catch (error) {
    ElMessage.error('加载模型列表失败')
  } finally {
    loading.value = false
  }
}

const handleActivate = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定激活模型版本 "${row.version}" 吗？这将停用当前激活的模型。`,
      '确认',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'info'
      }
    )
    await apiAdmin.activateModel(row.version)
    ElMessage.success('激活成功')
    loadModels()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('激活失败')
    }
  }
}

const handleDelete = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定删除模型版本 "${row.version}" 吗？此操作不可恢复。`,
      '警告',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    await apiAdmin.deleteModel(row.version)
    ElMessage.success('删除成功')
    loadModels()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

const formatTime = (isoString) => {
  if (!isoString) return '-'
  return new Date(isoString).toLocaleString('zh-CN')
}

onMounted(() => {
  loadModels()
})
</script>

<style scoped>
.admin-models {
  width: 100%;
}

.model-info p {
  margin: 5px 0;
  font-size: 14px;
}
</style>
