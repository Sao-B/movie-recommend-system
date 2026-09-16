<template>
  <el-card class="page-card stats-card" shadow="never" v-loading="loading">
    <template #header>
      <div class="card-head">
        <span class="card-title">系统概览</span>
      </div>
    </template>

    <!-- 统计块：与个人主页「观影统计」同一套观感（填充底 + 居中蓝色大数字） -->
    <div class="stats-grid">
      <div class="stat-item">
        <div class="stat-value">{{ fmt(stats.user_count) }}</div>
        <div class="stat-label">注册用户</div>
      </div>
      <div class="stat-item">
        <div class="stat-value">{{ fmt(stats.movie_count) }}</div>
        <div class="stat-label">电影总数</div>
      </div>
      <div class="stat-item">
        <div class="stat-value">{{ fmt(stats.rating_count) }}</div>
        <div class="stat-label">评分记录</div>
      </div>
      <div class="stat-item">
        <div class="stat-value">{{ fmt(stats.recommendation_count) }}</div>
        <div class="stat-label">推荐结果</div>
      </div>
    </div>

    <div class="overview-grid">
      <div class="preference-section">
        <h4>最近评分</h4>
        <div v-if="recentRatings.length" class="list">
          <div v-for="item in recentRatings" :key="item.id" class="list-row">
            <span class="list-text">{{ item.username }} 给《{{ item.movie_title }}》</span>
            <el-tag size="small">{{ item.rating }} 分</el-tag>
          </div>
        </div>
        <el-empty v-else description="暂无评分数据" :image-size="80" />
      </div>

      <div class="preference-section">
        <h4>热门电影 Top 10</h4>
        <div v-if="topMovies.length" class="list">
          <div v-for="(movie, index) in topMovies" :key="movie.movie_id" class="list-row">
            <span class="list-text">
              <span class="rank">{{ index + 1 }}</span>
              {{ movie.title }}
            </span>
            <el-tag size="small" type="info">{{ fmt(movie.rating_count) }} 条评分</el-tag>
          </div>
        </div>
        <el-empty v-else description="暂无数据" :image-size="80" />
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { computed, ref, onMounted } from 'vue'
import { apiAdmin } from '@/api/admin'

const loading = ref(false)
const stats = ref({})

const recentRatings = computed(() => stats.value.recent_ratings ?? [])
const topMovies = computed(() => stats.value.top_movies ?? [])

function fmt(value) {
  return (value ?? 0).toLocaleString('zh-CN')
}

async function loadStats() {
  loading.value = true
  try {
    // 失败提示由 axios 拦截器统一处理，这里不再重复弹
    stats.value = await apiAdmin.getStats()
  } finally {
    loading.value = false
  }
}

onMounted(loadStats)
</script>

<style scoped>
.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 20px;
  margin-bottom: 24px;
}

.stat-item {
  text-align: center;
  padding: 20px;
  background: var(--app-fill);
  border-radius: 8px;
}

.stat-value {
  font-size: 32px;
  font-weight: bold;
  color: #409eff;
  margin-bottom: 8px;
}

.stat-label {
  font-size: 14px;
  color: var(--app-text-secondary);
}

.overview-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 24px;
}

@media (max-width: 900px) {
  .overview-grid {
    grid-template-columns: 1fr;
  }
}

.preference-section h4 {
  margin: 0 0 12px;
  font-size: 16px;
  color: var(--app-text);
}

.list-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 0;
  border-bottom: 1px solid var(--app-border-light);
}

.list-row:last-child {
  border-bottom: none;
}

.list-text {
  font-size: 14px;
  color: var(--app-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.rank {
  display: inline-block;
  width: 20px;
  color: var(--app-text-muted);
  font-size: 13px;
}
</style>
