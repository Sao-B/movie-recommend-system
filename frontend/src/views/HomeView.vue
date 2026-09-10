<template>
  <div class="page">
    <!-- 搜索 & 筛选栏 -->
    <el-card class="filter-bar" shadow="never">
      <el-row :gutter="12" align="middle">
        <el-col :span="8">
          <el-input v-model="filters.q" placeholder="搜索电影名称..."
            prefix-icon="Search" clearable @keyup.enter="doSearch" @clear="doSearch" />
        </el-col>
        <el-col :span="5">
          <el-select v-model="filters.genre" placeholder="类型" clearable @change="doSearch">
            <el-option v-for="g in genres" :key="g" :label="g" :value="g" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-input v-model.number="filters.year" placeholder="年份" clearable
            type="number" @keyup.enter="doSearch" @clear="doSearch" />
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="doSearch">搜索</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- 电影网格 -->
    <div v-loading="loading" class="grid-wrap">
      <el-empty v-if="!loading && movies.length === 0" description="没有找到相关电影" />
      <el-row v-else :gutter="16">
        <el-col v-for="m in movies" :key="m.movie_id" :xs="12" :sm="8" :md="6" :lg="4">
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
        layout="prev, pager, next, total"
        background
        @current-change="fetchMovies"
      />
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'
import { ElMessage } from 'element-plus'
import MovieCard from '@/components/MovieCard.vue'
import { apiMovies } from '@/api/movies'

const movies      = ref([])
const genres      = ref([])
const total       = ref(0)
const loading     = ref(false)
const currentPage = ref(1)
const perPage     = 24
const filters     = reactive({ q: '', genre: '', year: null })

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
  } catch (msg) {
    ElMessage.error(typeof msg === 'string' ? msg : '加载失败')
  } finally {
    loading.value = false
  }
}

async function fetchGenres() {
  try { genres.value = await apiMovies.genres() } catch (_) {}
}

function doSearch()     { currentPage.value = 1; fetchMovies() }
function resetFilters() { filters.q = ''; filters.genre = ''; filters.year = null; doSearch() }

onMounted(() => { fetchMovies(); fetchGenres() })
</script>

<style scoped>
.page       { max-width: 1200px; margin: 0 auto; padding: 20px; }
.filter-bar { margin-bottom: 20px; }
.grid-wrap  { min-height: 300px; }
.pagination { margin-top: 24px; display: flex; justify-content: center; }
</style>
