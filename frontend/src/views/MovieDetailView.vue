<template>
  <div class="page" v-loading="loading">
    <template v-if="movie">
      <el-button text @click="$router.back()" class="back-btn">← 返回</el-button>
      <el-card class="detail-card">
        <div class="detail-layout">
          <div class="poster-section">
            <img v-if="movie.poster_url" :src="movie.poster_url" :alt="movie.title" class="detail-poster" />
            <div v-else class="detail-poster-placeholder">
              <el-icon :size="80"><Film /></el-icon>
            </div>
          </div>
          <div class="info-section">
            <h1 class="movie-title">{{ movie.title }}</h1>
            <div class="meta">
              <el-tag v-if="movie.year" type="info">{{ movie.year }}</el-tag>
              <el-tag v-for="g in movie.genres" :key="g" class="genre-tag">{{ g }}</el-tag>
            </div>

            <el-divider />

            <div class="movie-info">
              <div class="info-row" v-if="movie.director">
                <span class="info-label">导演：</span>
                <span class="info-value">{{ movie.director }}</span>
              </div>
              <div class="info-row" v-if="movie.writers">
                <span class="info-label">编剧：</span>
                <span class="info-value">{{ movie.writers }}</span>
              </div>
              <div class="info-row" v-if="movie.cast">
                <span class="info-label">主演：</span>
                <span class="info-value">{{ movie.cast }}</span>
              </div>
              <div class="info-row" v-if="movie.countries">
                <span class="info-label">制片地区：</span>
                <span class="info-value">{{ movie.countries }}</span>
              </div>
              <div class="info-row" v-if="movie.languages">
                <span class="info-label">语言：</span>
                <span class="info-value">{{ movie.languages }}</span>
              </div>
              <div class="info-row" v-if="movie.release_date">
                <span class="info-label">上映日期：</span>
                <span class="info-value">{{ movie.release_date }}</span>
              </div>
              <div class="info-row" v-if="movie.runtime">
                <span class="info-label">片长：</span>
                <span class="info-value">{{ movie.runtime }} 分钟</span>
              </div>
            </div>

            <el-divider />

            <div class="stat-item">
              <span class="stat-label">平均评分</span>
              <el-rate :model-value="movie.avg_rating" disabled show-score allow-half />
              <span class="stat-sub">（{{ movie.rating_count }} 人评价）</span>
            </div>
          </div>
        </div>

        <template v-if="movie.overview">
          <el-divider />
          <div class="overview-section">
            <h3>剧情简介</h3>
            <p class="overview-text">{{ movie.overview }}</p>
          </div>
        </template>

      </el-card>

      <!-- 评分与影评区 -->
      <el-card class="detail-card" style="margin-top: 20px">
        <CommentSection :movie-id="movie.movie_id" :my-rating="myRating" @update:my-rating="myRating = $event" @rating-submitted="onRatingSubmitted" />
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Film } from '@element-plus/icons-vue'
import { useAuthStore } from '@/stores/auth'
import { apiMovies } from '@/api/movies'
import CommentSection from '@/components/CommentSection.vue'

const route      = useRoute()
const auth       = useAuthStore()
const loading    = ref(true)
const movie      = ref(null)
const myRating   = ref(0)

onMounted(async () => {
  try {
    movie.value = await apiMovies.detail(route.params.id)
  } catch (msg) {
    ElMessage.error(typeof msg === 'string' ? msg : '加载失败')
  } finally {
    loading.value = false
  }
})

async function onRatingSubmitted() {
  // 评分提交后刷新电影平均分
  try {
    movie.value = await apiMovies.detail(route.params.id)
  } catch (msg) {
    ElMessage.error('刷新数据失败')
  }
}
</script>

<style scoped>
.page        { max-width: 900px; margin: 0 auto; padding: 20px; }
.back-btn    { margin-bottom: 12px; }
.movie-title { font-size: 24px; margin: 0 0 12px; }
.meta        { display: flex; flex-wrap: wrap; gap: 8px; }
.stat-item   { display: flex; align-items: center; gap: 8px; }
.stat-label  { font-weight: 600; min-width: 60px; }
.stat-sub    { color: #999; font-size: 13px; }

.detail-layout {
  display: flex;
  gap: 24px;
  margin-bottom: 16px;
}

.poster-section {
  flex-shrink: 0;
  width: 300px;
}

.detail-poster {
  width: 100%;
  height: auto;
  border-radius: 8px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.detail-poster-placeholder {
  width: 100%;
  aspect-ratio: 2/3;
  background: #f5f5f5;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: #dcdfe6;
}

.info-section {
  flex: 1;
  min-width: 0;
}

.movie-info {
  margin: 16px 0;
}

.info-row {
  display: flex;
  margin-bottom: 8px;
  line-height: 1.6;
}

.info-label {
  font-weight: 600;
  color: #606266;
  min-width: 80px;
  flex-shrink: 0;
}

.info-value {
  color: #303133;
  flex: 1;
}

.overview-section h3 {
  font-size: 18px;
  margin: 0 0 12px;
}

.overview-text {
  line-height: 1.8;
  color: #606266;
  margin: 0;
  white-space: pre-wrap;
}

/* 响应式布局 */
@media (max-width: 768px) {
  .page {
    padding: 12px;
  }

  .detail-layout {
    flex-direction: column;
    gap: 16px;
  }

  .poster-section {
    width: 100%;
    max-width: 250px;
    margin: 0 auto;
  }

  .movie-title {
    font-size: 20px;
  }

  .overview-section h3 {
    font-size: 16px;
  }
}

@media (min-width: 1200px) {
  .poster-section {
    width: 350px;
  }
}
</style>
