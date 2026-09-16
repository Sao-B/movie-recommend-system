<template>
  <el-card class="page-card" shadow="never">
    <template #header>
      <div class="card-head">
        <span class="card-title">电影管理</span>
      </div>
    </template>

    <el-form class="search-bar" :inline="true">
      <el-form-item label="标题">
        <el-input v-model="filters.title" placeholder="支持模糊搜索" clearable style="width: 220px" @clear="handleSearch" @keyup.enter="handleSearch" />
      </el-form-item>
      <el-form-item label="类型">
        <el-select v-model="filters.genres" placeholder="全部类型" clearable filterable style="width: 150px" @change="handleSearch">
          <el-option v-for="genre in GENRES" :key="genre" :label="genre" :value="genre" />
        </el-select>
      </el-form-item>
      <el-form-item label="年份">
        <el-select v-model="filters.year" placeholder="全部年份" clearable filterable style="width: 120px" @change="handleSearch">
          <el-option v-for="y in YEARS" :key="y" :label="y" :value="y" />
        </el-select>
      </el-form-item>
      <el-form-item>
        <el-button type="primary" @click="handleSearch">搜索</el-button>
        <el-button @click="handleReset">重置</el-button>
      </el-form-item>
    </el-form>

    <el-table :data="movies" v-loading="loading" border stripe @sort-change="handleSortChange">
      <el-table-column prop="movie_id" label="ID" width="90" sortable="custom" />
      <el-table-column prop="title" label="标题" min-width="220" sortable="custom" show-overflow-tooltip />
      <el-table-column label="类型" min-width="180" show-overflow-tooltip prop="genres" sortable="custom">
        <template #default="{ row }">{{ joinGenres(row.genres) }}</template>
      </el-table-column>
      <el-table-column prop="year" label="年份" width="90" sortable="custom" />
      <el-table-column label="均分" width="90" prop="avg_rating" sortable="custom">
        <template #default="{ row }">{{ row.avg_rating ?? '-' }}</template>
      </el-table-column>
      <el-table-column label="评分人数" width="120" prop="rating_count" sortable="custom">
        <template #default="{ row }">{{ row.rating_count ?? 0 }}</template>
      </el-table-column>
      <el-table-column label="操作" width="160" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
          <el-button link type="danger" size="small" @click="handleDelete(row)">删除</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="没有找到电影" :image-size="80" />
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
      @current-change="loadMovies"
    />
  </el-card>

  <el-dialog v-model="dialogVisible" title="编辑电影" width="440px">
    <el-form ref="formRef" :model="form" :rules="rules" label-width="70px">
      <el-form-item label="ID">
        <el-input :model-value="form.movie_id" disabled />
      </el-form-item>
      <el-form-item label="标题" prop="title">
        <el-input v-model="form.title" />
      </el-form-item>
      <el-form-item label="类型" prop="genres">
        <el-input v-model="form.genres" placeholder="多个类型用 | 分隔，如 Comedy|Romance" />
      </el-form-item>
      <el-form-item label="年份" prop="year">
        <el-input-number v-model="form.year" :min="1900" :max="2100" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="dialogVisible = false">取消</el-button>
      <el-button type="primary" @click="handleSubmit">保存</el-button>
    </template>
  </el-dialog>
</template>

<script setup>
import { reactive, ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const movies = ref([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const filters = reactive({ title: '', genres: '', year: '' })
const sortProp = ref('')
const sortOrder = ref('')

// MovieLens 的 19 种标准类型；数据库里的 genres 就是竖线拼接的这套词
const GENRES = [
  'Action', 'Adventure', 'Animation', 'Children', 'Comedy', 'Crime',
  'Documentary', 'Drama', 'Fantasy', 'Film-Noir', 'Horror', 'IMAX',
  'Musical', 'Mystery', 'Romance', 'Sci-Fi', 'Thriller', 'War', 'Western'
]

// 年份下拉从 2026 倒序到 1896（首部电影诞生年）
const YEARS = Array.from({ length: 2026 - 1896 + 1 }, (_, i) => 2026 - i)

const dialogVisible = ref(false)
const formRef = ref(null)
const form = reactive({ movie_id: null, title: '', genres: '', year: null })

const rules = {
  title: [{ required: true, message: '请输入标题', trigger: 'blur' }]
}

function joinGenres(genres) {
  return Array.isArray(genres) ? genres.join(' / ') : genres || '-'
}

async function loadMovies() {
  loading.value = true
  try {
    // 后端分页参数叫 per_page，标题关键词叫 q
    const data = await apiAdmin.listMovies({
      page: page.value,
      per_page: pageSize.value,
      q: filters.title,
      genres: filters.genres,
      year: filters.year || undefined,
      ...(sortProp.value ? { sort: sortProp.value, order: sortOrder.value } : {})
    })
    movies.value = data.movies
    total.value = data.total
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  page.value = 1
  loadMovies()
}

// 表头点击切换排序；order 为空表示取消排序，回到默认
function handleSortChange({ prop, order }) {
  if (order) {
    sortProp.value = prop
    sortOrder.value = order === 'ascending' ? 'asc' : 'desc'
  } else {
    sortProp.value = ''
    sortOrder.value = ''
  }
  handleSearch()
}

function handleReset() {
  filters.title = ''
  filters.genres = ''
  filters.year = ''
  handleSearch()
}

function openEdit(row) {
  Object.assign(form, {
    movie_id: row.movie_id,
    title: row.title,
    genres: Array.isArray(row.genres) ? row.genres.join('|') : row.genres || '',
    year: row.year
  })
  dialogVisible.value = true
}

async function handleSubmit() {
  const valid = await formRef.value.validate().catch(() => false)
  if (!valid) return

  await apiAdmin.updateMovie(form.movie_id, { title: form.title, genres: form.genres, year: form.year })
  ElMessage.success('已保存')
  dialogVisible.value = false
  loadMovies()
}

async function handleDelete(row) {
  try {
    await ElMessageBox.confirm(
      `删除《${row.title}》会连带清理它的评分与评论记录。确定继续？`,
      '删除确认',
      { type: 'warning', confirmButtonText: '删除', cancelButtonText: '取消' }
    )
  } catch {
    return
  }

  await apiAdmin.deleteMovie(row.movie_id)
  ElMessage.success('已删除')
  const lastPage = Math.max(1, Math.ceil((total.value - 1) / pageSize.value))
  if (page.value > lastPage) page.value = lastPage
  loadMovies()
}

onMounted(loadMovies)
</script>
