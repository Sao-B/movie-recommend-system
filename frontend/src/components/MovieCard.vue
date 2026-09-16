<template>
  <el-card class="movie-card" shadow="hover" @click="$router.push('/movies/' + movie.movie_id)">
    <div class="poster-container">
      <img
        v-if="movie.poster_url && !imageError"
        :src="movie.poster_url"
        :alt="movie.title"
        class="poster-img"
        @error="imageError = true"
      />
      <div v-else class="poster-placeholder">
        <el-icon><Film /></el-icon>
      </div>
    </div>
    <div class="content">
      <div class="movie-year" v-if="movie.year">{{ movie.year }}</div>
      <h4 class="movie-title">{{ movie.title }}</h4>
      <div class="movie-genres">
        <el-tag
          v-for="g in movie.genres?.slice(0, 2)"
          :key="g"
          size="small"
          type="info"
          class="genre-tag"
        >{{ g }}</el-tag>
      </div>
      <div class="rating-info" v-if="movie.avg_rating">
        <el-rate
          :model-value="movie.avg_rating"
          disabled
          show-score
          :score-template="`${movie.avg_rating}`"
          size="small"
        />
      </div>
      <div v-if="predictedRating" class="predicted">
        <span class="recommend-label">推荐指数</span>
        <el-rate
          :model-value="normalizedRating"
          disabled
          allow-half
          size="small"
          class="recommend-stars"
        />
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed } from 'vue'
import { Film } from '@element-plus/icons-vue'

const props = defineProps({
  movie:          { type: Object, required: true },
  predictedRating:{ type: [Number, String], default: null },
})

const imageError = ref(false)

// 将预测评分归一化到 0-5 范围（用于显示星星）
const normalizedRating = computed(() => {
  if (!props.predictedRating) return 0
  const rating = typeof props.predictedRating === 'string'
    ? parseFloat(props.predictedRating)
    : props.predictedRating
  // 截断到 0.5-5.0 范围
  return Math.max(0.5, Math.min(5.0, rating))
})

</script>

<style scoped>
.movie-card {
  cursor: pointer;
  height: 100%;
  display: flex;
  flex-direction: column;
  transition: transform .15s;
  padding: 0;
}

.movie-card :deep(.el-card__body) {
  padding: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.movie-card:hover { transform: translateY(-3px); }

.poster-container {
  width: 100%;
  height: 240px;
  background: var(--app-fill-light);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex-shrink: 0;
  position: relative;
}

.poster-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 响应式适配 */
@media (max-width: 768px) {
  .poster-container {
    height: 180px;
  }
}

@media (min-width: 1200px) {
  .poster-container {
    height: 280px;
  }
}

.poster-placeholder {
  font-size: 48px;
  color: var(--app-text-muted);
  display: flex;
  align-items: center;
  justify-content: center;
}

.content {
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 12px;
  min-height: 0;
}

.movie-year  { font-size: 12px; color: var(--app-text-muted); margin-bottom: 4px; flex-shrink: 0; }
.movie-title {
  font-size: 14px;
  font-weight: 500;
  margin: 0 0 8px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
  flex-shrink: 0;
}
.movie-genres {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
  flex-shrink: 0;
}
.genre-tag   { font-size: 11px; }
.rating-info {
  font-size: 12px;
  margin-top: auto;
  flex-shrink: 0;
}
.rating-info :deep(.el-rate) {
  height: 20px;
}
.predicted {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  flex-shrink: 0;
}

.recommend-label {
  font-size: 12px;
  color: #e6a23c;
  font-weight: 500;
}

.recommend-stars :deep(.el-rate) {
  height: 18px;
}

.recommend-stars :deep(.el-rate__icon) {
  font-size: 14px;
}
</style>
