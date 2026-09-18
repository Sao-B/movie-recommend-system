<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="card-head">
        <span class="card-title">评分管理</span>
        <span class="hint">只读列表，异常打分可在此删除并重算推荐</span>
      </div>
    </template>

    <el-form class="search-bar" :inline="true">
      <el-form-item label="用户 ID">
        <el-input v-model="filters.user_id" placeholder="精确匹配" clearable style="width: 140px" @clear="handleSearch" @keyup.enter="handleSearch" />
      </el-form-item>
      <el-form-item label="电影 ID">
        <el-input v-model="filters.movie_id" placeholder="精确匹配" clearable style="width: 140px" @clear="handleSearch" @keyup.enter="handleSearch" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="handleSearch">搜索</el-button>
        <el-button @click="handleReset">重置</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="ratings" v-loading="loading" border stripe @sort-change="handleSortChange">
      <el-table-column prop="id" label="ID" width="100" sortable="custom" />
      <el-table-column label="用户" min-width="160" prop="username" sortable="custom">
        <template #default="{ row }">
          {{ row.username }} <span class="muted">#{{ row.user_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="电影" min-width="220" show-overflow-tooltip prop="movie_title" sortable="custom">
        <template #default="{ row }">
          {{ row.movie_title }} <span class="muted">#{{ row.movie_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="评分" width="110" prop="rating" sortable="custom">
        <template #default="{ row }">
          <el-tag size="small" type="warning">{{ row.rating }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="180" prop="rated_at" sortable="custom">
        <template #default="{ row }">{{ formatDateTime(row.rated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="100" fixed="right">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="没有找到评分记录" :image-size="80" />
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
      @current-change="loadRatings"
    />
  </el-card>
</template>

<script setup>
import { reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiAdmin } from '@/api/admin'
import { formatDateTime } from '@/utils/format'
import { useTableQuery } from '@/composables/useTableQuery'

const filters = reactive({ user_id: '', movie_id: '' })

// 分页 / 排序 / 加载骨架见 composables/useTableQuery.js
const {
  loading, rows: ratings, total, page, pageSize,
  load: loadRatings, search: handleSearch, handleSortChange, reloadAfterDelete,
} = useTableQuery(async ({ page, pageSize, sortProp, sortOrder }) => {
  const data = await apiAdmin.listRatings({
    page,
    per_page: pageSize,
    user_id: filters.user_id || undefined,
    movie_id: filters.movie_id || undefined,
    ...(sortProp ? { sort: sortProp, order: sortOrder } : {}),
  })
  return { rows: data.ratings, total: data.total }
})

function handleReset() {
  filters.user_id = ''
  filters.movie_id = ''
  handleSearch()
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除「${row.username}」对《${row.movie_title}》的 ${row.rating} 分？`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  await apiAdmin.deleteRating(row.id)
  ElMessage.success('已删除')
  reloadAfterDelete()
}

onMounted(loadRatings)
</script>

<style scoped>
.hint,
.muted {
  font-size: 13px;
  color: var(--app-text-muted);
}

.muted {
  font-size: 12px;
}
</style>
