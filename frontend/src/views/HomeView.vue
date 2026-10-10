<template>
  <div class="page">
    <!-- 搜索 & 筛选栏（内容居中） -->
    <el-card class="filter-bar" shadow="never">
      <div class="filter-inner">
        <el-input v-model="filters.q" class="fi fi-q" placeholder="搜索片名 / 简介 / ID"
          :prefix-icon="Search" clearable @keyup.enter="doSearch" @clear="doSearch" />
        <el-select v-model="filters.genre" class="fi" placeholder="类型" clearable @change="doSearch">
          <el-option v-for="g in genres" :key="g" :label="g" :value="g" />
        </el-select>
        <el-input v-model.number="filters.year" class="fi fi-year" placeholder="年份" clearable
          type="number" @keyup.enter="doSearch" @clear="doSearch" />
        <div class="fi-btns">
          <el-button type="primary" @click="doSearch">搜索</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </div>
      </div>
    </el-card>

    <!-- 电影网格 -->
    <div v-loading="loading" class="grid-wrap">
      <el-empty v-if="!loading && movies.length === 0" description="没有找到相关电影" />
      <el-row v-else :gutter="16" class="movie-grid">
        <el-col v-for="m in movies" :key="m.movie_id" :xs="12" :sm="8" :md="6" :lg="4" class="movie-col">
          <MovieCard :movie="m" />
        </el-col>
      </el-row>
    </div>

    <!-- 分页 -->
    <div class="pagination">
      <el-pagination
        v-model:current-page="currentPage"
        :page-size="perPage"
        :total="total"
        layout="prev, pager, next, jumper, total"
        background
        @current-change="fetchMovies"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import MovieCard from '@/components/MovieCard.vue'
import { apiMovies } from '@/api/movies'

const route = useRoute()
const router = useRouter()

const movies      = ref([])
const genres      = ref([])
const total       = ref(0)
const loading     = ref(false)
const currentPage = ref(1)
const perPage     = 24
const filters     = reactive({ q: '', genre: '', year: null })

// 从 URL 查询参数恢复状态
function restoreStateFromQuery() {
  currentPage.value = parseInt(route.query.page) || 1
  filters.q = route.query.q || ''
  filters.genre = route.query.genre || ''
  filters.year = route.query.year ? parseInt(route.query.year) : null
}

// 将状态同步到 URL
function syncStateToQuery() {
  const query = { page: currentPage.value }
  if (filters.q) query.q = filters.q
  if (filters.genre) query.genre = filters.genre
  if (filters.year) query.year = filters.year
  router.replace({ query })
}

async function fetchMovies() {
  loading.value = true
  try {
    const params = {
      page: currentPage.value,
      per_page: perPage,
      sort: 'popular'  // 默认按热门排序
    }
    if (filters.q)     params.q     = filters.q
    if (filters.genre) params.genre = filters.genre
    if (filters.year)  params.year  = filters.year
    const data   = await apiMovies.list(params)
    movies.value = data.movies
    total.value  = data.total
    // 同步状态到 URL
    syncStateToQuery()
  } catch {} finally {
    loading.value = false
  }
}

async function fetchGenres() {
  try { genres.value = await apiMovies.genres() } catch (_) {}
}

function doSearch()     { currentPage.value = 1; fetchMovies() }
function resetFilters() { filters.q = ''; filters.genre = ''; filters.year = null; doSearch() }

onMounted(() => {
  // 先恢复状态再加载数据
  restoreStateFromQuery()
  fetchMovies()
  fetchGenres()
})

// 监听 URL 变化（浏览器前进/后退）
watch(() => route.query, () => {
  restoreStateFromQuery()
  fetchMovies()
}, { deep: true })
</script>

<style scoped>
.page       { max-width: 1200px; margin: 0 auto; padding: 20px; }
.filter-bar { margin-bottom: 20px; }
.filter-inner {
  display: flex;
  justify-content: center;
  align-items: center;
  flex-wrap: wrap;
  gap: 12px;
}
/* 注意顺序：.fi 是通用默认，必须写在 .fi-q/.fi-year 之前，否则同优先级下会覆盖具体宽度 */
.fi      { width: 190px; }
.fi-q    { width: 380px; }
.fi-year { width: 130px; }
.fi-btns { display: flex; }
.grid-wrap  { min-height: 300px; }
.movie-grid { display: flex; flex-wrap: wrap; }
.movie-col {
  display: flex;
  margin-bottom: 16px;
  min-height: 380px;
}
.movie-col > * {
  width: 100%;
}
.pagination { margin-top: 24px; display: flex; justify-content: center; }
</style>
