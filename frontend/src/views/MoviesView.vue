<template>
  <div>
    <!-- 筛选栏 -->
    <el-card style="margin-bottom:16px">
      <el-row :gutter="12" align="middle">
        <el-col :span="8">
          <el-input v-model="filters.q" placeholder="搜索电影名称…" clearable
                    :prefix-icon="Search" @keyup.enter="fetchMovies" />
        </el-col>
        <el-col :span="5">
          <el-select v-model="filters.genre" placeholder="选择类型" clearable style="width:100%">
            <el-option v-for="g in genres" :key="g" :label="g" :value="g" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-input v-model="filters.year" placeholder="年份" clearable type="number" />
        </el-col>
        <el-col :span="4">
          <el-button type="primary" :icon="Search" @click="fetchMovies">搜索</el-button>
          <el-button @click="resetFilters">重置</el-button>
        </el-col>
      </el-row>
    </el-card>

    <!-- 电影网格 -->
    <el-row v-loading="loading" :gutter="16">
      <el-col v-for="m in movies" :key="m.movie_id" :xs="24" :sm="12" :md="8" :lg="6" :xl="4"
              style="margin-bottom:16px">
        <MovieCard :movie="m" @click="router.push(`/movies/${m.movie_id}`)" />
      </el-col>
      <el-col v-if="!loading && movies.length === 0" :span="24">
        <el-empty description="没有找到相关电影" />
      </el-col>
    </el-row>

    <!-- 分页 -->
    <el-pagination v-if="total > 0" background layout="prev, pager, next, total"
                   :total="total" :page-size="perPage" :current-page="page"
                   style="margin-top:16px; justify-content:center; display:flex"
                   @current-change="onPageChange" />
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { Search } from '@element-plus/icons-vue'
import { moviesApi } from '@/api/movies'
import MovieCard from '@/components/MovieCard.vue'

const router  = useRouter()
const movies  = ref([])
const genres  = ref([])
const total   = ref(0)
const page    = ref(1)
const perPage = 20
const loading = ref(false)

const filters = ref({ q: '', genre: '', year: '' })

async function fetchMovies() {
  loading.value = true
  try {
    const params = { page: page.value, per_page: perPage, ...filters.value }
    // 去掉空值
    Object.keys(params).forEach(k => !params[k] && delete params[k])
    const res   = await moviesApi.list(params)
    movies.value = res.data.movies
    total.value  = res.data.total
  } finally {
    loading.value = false
  }
}

function resetFilters() {
  filters.value = { q: '', genre: '', year: '' }
  page.value = 1
  fetchMovies()
}

function onPageChange(p) {
  page.value = p
  fetchMovies()
}

onMounted(async () => {
  const res   = await moviesApi.genres()
  genres.value = res.data
  fetchMovies()
})
</script>
