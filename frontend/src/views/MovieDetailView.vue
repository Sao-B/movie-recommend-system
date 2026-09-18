<template>
  <div class="page" v-loading="loading">
    <template v-if="movie">
      <el-button text @click="$router.back()" class="back-btn">← 返回</el-button>

      <!-- 基本信息 -->
      <el-card class="detail-card">
        <div class="detail-layout">
          <div class="poster-section">
            <img v-if="movie.poster_url" :src="movie.poster_url" :alt="movie.title" class="detail-poster" />
            <div v-else class="detail-poster-placeholder">
              <el-icon :size="80"><Film /></el-icon>
            </div>
          </div>

          <div class="head-section">
            <h1 class="movie-title">{{ movie.title }}</h1>
            <!-- 浅灰小字标出 ID，便于在搜索框里用 ID318 这种写法精确查片 -->
            <div class="movie-id">电影 ID：{{ movie.movie_id }}</div>
            <div class="meta">
              <el-tag v-if="movie.year" type="info">{{ movie.year }}</el-tag>
              <el-tag v-for="g in movie.genres" :key="g" class="genre-tag">{{ g }}</el-tag>
            </div>

            <template v-if="movie.overview">
              <el-divider />
              <h3 class="section-label">剧情简介</h3>
              <p class="overview-text">{{ movie.overview }}</p>
            </template>
          </div>
        </div>

        <template v-if="hasDetails">
          <el-divider />
          <div class="movie-details">
            <div class="detail-row" v-if="movie.release_date">
              <span class="detail-label">上映日期：</span>
              <span class="detail-value">{{ movie.release_date }}</span>
            </div>
            <div class="detail-row" v-if="movie.runtime">
              <span class="detail-label">片长：</span>
              <span class="detail-value">{{ movie.runtime }} 分钟</span>
            </div>

            <div class="detail-row" v-if="movie.director">
              <span class="detail-label">导演：</span>
              <span class="detail-value">{{ movie.director }}</span>
            </div>
            <div class="detail-row" v-if="movie.countries">
              <span class="detail-label">制片地区：</span>
              <span class="detail-value">{{ movie.countries }}</span>
            </div>

            <div class="detail-row" v-if="movie.writers">
              <span class="detail-label">编剧：</span>
              <span class="detail-value">{{ movie.writers }}</span>
            </div>
            <div class="detail-row" v-if="movie.languages">
              <span class="detail-label">语言：</span>
              <span class="detail-value">{{ movie.languages }}</span>
            </div>

            <div class="detail-row full" v-if="movie.cast">
              <span class="detail-label">主演：</span>
              <span class="detail-value">{{ movie.cast }}</span>
            </div>
          </div>
        </template>
      </el-card>

      <!-- 评分与影评 -->
      <el-card class="detail-card comment-card">
        <h3 class="card-title">评分与影评</h3>

        <RatingStats
          :count="stats.count"
          :average="stats.average"
          :distribution="stats.distribution"
          :loading="statsLoading"
        />

        <el-divider />

        <CommentSection
          :movie-id="movie.movie_id"
          :movie-title="movie.title"
          :my-rating="myRating"
          :saved-rating="savedRating"
          @update:my-rating="myRating = $event"
          @rating-submitted="onRatingSubmitted"
        />
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { Film } from '@element-plus/icons-vue'
import { apiMovies } from '@/api/movies'
import RatingStats from '@/components/RatingStats.vue'
import CommentSection from '@/components/CommentSection.vue'

const route       = useRoute()
const loading     = ref(true)
const movie       = ref(null)
// myRating 是评分控件的表单态（用户改动后立即反映）；savedRating 是服务端此刻的值，
// 两者分开是为了判断「评分到底改没改」——没改就不写库，免得只改一句影评时把
// rated_at 白刷一遍。删除不经过这两个值：影评卡片上的「删除」（置顶那条与时间线里
// 自己那条同源）在确认框里问「影评草稿要不要保留」，保留＝只删评分、不保留＝两行一起删，
// 之后照旧靠 onRatingSubmitted 重拉统计，把 myRating / savedRating 一起归零。
const myRating    = ref(0)
const savedRating = ref(0)

const stats        = ref({ count: 0, average: null, distribution: [] })
const statsLoading = ref(false)

const hasDetails = computed(() => {
  const m = movie.value
  return !!(m && (m.release_date || m.runtime || m.director ||
                  m.countries || m.writers || m.languages || m.cast))
})

async function loadMovie() {
  try {
    movie.value = await apiMovies.detail(route.params.id)
  } catch {}
}

async function loadStats() {
  statsLoading.value = true
  try {
    const data = await apiMovies.ratingStats(route.params.id)
    stats.value = {
      count:        data.count ?? 0,
      average:      data.average ?? null,
      distribution: data.distribution ?? []
    }
    // 没有评分时 my_rating 为 null，必须把控件重置成「未评分」（值 0），
    // 否则页面上还留着上一次的星星（旧写法 `if (data.my_rating != null)` 有这个问题）
    myRating.value = data.my_rating ?? 0
    savedRating.value = data.my_rating ?? 0
  } catch (msg) {
    // 评分统计属于附加信息，失败时静默降级，不打断详情页
    stats.value = { count: 0, average: null, distribution: [] }
  } finally {
    statsLoading.value = false
  }
}

onMounted(async () => {
  await Promise.all([loadMovie(), loadStats()])
  loading.value = false
})

async function onRatingSubmitted() {
  // 提交评分后，平均分与各档分布会变化，重新拉取统计
  await loadStats()
}
</script>

<style scoped>
.page        { max-width: 1200px; margin: 0 auto; padding: 20px; }
.back-btn    { margin-bottom: 12px; }
.movie-title { font-size: 26px; margin: 0 0 6px; line-height: 1.3; }
.movie-id    { font-size: 13px; margin: 0 0 12px; color: var(--app-text-muted); }
.meta        { display: flex; flex-wrap: wrap; gap: 8px; }

.comment-card {
  margin-top: 20px;
}

.card-title {
  font-size: 18px;
  font-weight: 600;
  margin: 0 0 18px;
  color: var(--app-text);
}

.section-label {
  font-size: 20px;
  font-weight: 600;
  color: var(--app-text);
  margin: 0 0 8px;
}

.detail-layout {
  display: flex;
  gap: 28px;
}

.poster-section {
  flex-shrink: 0;
  width: 320px;
}

.detail-poster {
  display: block;
  width: 100%;
  height: auto;
  border-radius: 6px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
}

.detail-poster-placeholder {
  width: 100%;
  aspect-ratio: 2/3;
  background: var(--app-fill-light);
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 6px;
  color: var(--app-text-muted);
}

.head-section {
  flex: 1;
  min-width: 0;
}

.overview-text {
  color: var(--app-text);
  margin: 0;
  font-size: 18px;
  line-height: 1.9;
  white-space: pre-wrap;
}

/* 详情信息：两列网格 */
.movie-details {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 9px 40px;
}

.detail-row {
  display: flex;
  min-width: 0;
  line-height: 1.6;
}

.detail-row.full {
  grid-column: 1 / -1;
}

.detail-label {
  flex-shrink: 0;
  width: 84px;
  color: var(--app-text-muted);
}

.detail-value {
  color: var(--app-text);
  min-width: 0;
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

  .movie-details {
    grid-template-columns: minmax(0, 1fr);
    gap: 9px 0;
  }

  .section-label {
    font-size: 16px;
  }
}
</style>
