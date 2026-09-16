<template>
  <div class="page">
    <h2 class="page-title">🎯 我的个性化推荐</h2>
    <p class="page-sub">基于 ALS 协同过滤，为你精选的 Top-20 电影</p>
    <div v-loading="loading">
      <el-empty v-if="!loading && recs.length === 0"
        description="暂无推荐数据，请先评几部电影，等待下次离线训练后即可获得推荐" />
      <el-row v-else :gutter="16">
        <el-col v-for="r in recs" :key="r.movie && r.movie.movie_id"
          :xs="12" :sm="8" :md="6" :lg="4">
          <MovieCard
            :movie="r.movie"
            :predicted-rating="r.predicted_rating && r.predicted_rating.toFixed(2)"
          />
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import MovieCard from '@/components/MovieCard.vue'
import { apiRecommendations } from '@/api/recommendations'

const recs    = ref([])
const loading = ref(true)

onMounted(async () => {
  try {
    const data = await apiRecommendations.get()
    recs.value = data.data || []
  } catch {} finally {
    loading.value = false
  }
})
</script>

<style scoped>
.page       { max-width: 1200px; margin: 0 auto; padding: 20px; }
.page-title { margin: 0 0 4px; }
.page-sub   { color: var(--app-text-muted); margin: 0 0 20px; font-size: 14px; }
</style>
