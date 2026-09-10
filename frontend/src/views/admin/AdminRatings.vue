<template>
  <div class="admin-ratings">
    <el-card>
      <template #header>
        <span>评分管理</span>
      </template>

      <el-form :inline="true" :model="searchForm" class="search-form">
        <el-form-item label="用户ID">
          <el-input v-model="searchForm.user_id" placeholder="请输入用户ID" clearable />
        </el-form-item>
        <el-form-item label="电影ID">
          <el-input v-model="searchForm.movie_id" placeholder="请输入电影ID" clearable />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">搜索</el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>

      <el-table :data="ratings" v-loading="loading" border stripe>
        <el-table-column prop="id" label="ID" width="100" />
        <el-table-column prop="user_id" label="用户ID" width="100" />
        <el-table-column prop="username" label="用户名" width="150" />
        <el-table-column prop="movie_id" label="电影ID" width="100" />
        <el-table-column prop="movie_title" label="电影标题" min-width="200" />
        <el-table-column prop="rating" label="评分" width="100">
          <template #default="{ row }">
            <el-tag type="warning">{{ row.rating }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="timestamp" label="时间" width="180">
          <template #default="{ row }">
            {{ formatTime(row.timestamp) }}
          </template>
        </el-table-column>
        <el-table-column label="操作" width="120">
          <template #default="{ row }">
            <el-button size="small" type="danger" @click="handleDelete(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <el-pagination
        v-model:current-page="pagination.page"
        v-model:page-size="pagination.page_size"
        :total="pagination.total"
        :page-sizes="[10, 20, 50, 100]"
        layout="total, sizes, prev, pager, next, jumper"
        @size-change="loadRatings"
        @current-change="loadRatings"
        style="margin-top: 20px; justify-content: center;"
      />
    </el-card>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { apiAdmin } from '@/api/admin'
import { ElMessage, ElMessageBox } from 'element-plus'

const loading = ref(false)
const ratings = ref([])
const pagination = reactive({
  page: 1,
  page_size: 10,
  total: 0
})

const searchForm = reactive({
  user_id: '',
  movie_id: ''
})

const loadRatings = async () => {
  loading.value = true
  try {
    const params = {
      page: pagination.page,
      page_size: pagination.page_size
    }
    if (searchForm.user_id) params.user_id = searchForm.user_id
    if (searchForm.movie_id) params.movie_id = searchForm.movie_id

    const data = await apiAdmin.listRatings(params)
    ratings.value = data.ratings
    pagination.total = data.total
  } catch (error) {
    ElMessage.error('加载评分列表失败')
  } finally {
    loading.value = false
  }
}

const handleSearch = () => {
  pagination.page = 1
  loadRatings()
}

const handleReset = () => {
  searchForm.user_id = ''
  searchForm.movie_id = ''
  pagination.page = 1
  loadRatings()
}

const handleDelete = async (row) => {
  try {
    await ElMessageBox.confirm(
      `确定删除此评分记录吗？用户: ${row.username}, 电影: ${row.movie_title}`,
      '警告',
      {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      }
    )
    await apiAdmin.deleteRating(row.id)
    ElMessage.success('删除成功')
    loadRatings()
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
  loadRatings()
})
</script>

<style scoped>
.admin-ratings {
  width: 100%;
}

.search-form {
  margin-bottom: 20px;
}
</style>
