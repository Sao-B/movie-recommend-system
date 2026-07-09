<template>
  <div class="page" v-loading="loading">
    <template v-if="movie">
      <el-button text @click="$router.back()" class="back-btn">← 返回</el-button>
      <el-card class="detail-card">
        <h1 class="movie-title">{{ movie.title }}</h1>
        <div class="meta">
          <el-tag v-if="movie.year" type="info">{{ movie.year }}</el-tag>
          <el-tag v-for="g in movie.genres" :key="g" class="genre-tag">{{ g }}</el-tag>
        </div>

        <el-divider />

        <div class="stat-item">
          <span class="stat-label">平均评分</span>
          <el-rate :model-value="movie.avg_rating" disabled show-score allow-half />
          <span class="stat-sub">（{{ movie.rating_count }} 人评价）</span>
        </div>

        <el-divider v-if="auth.isLoggedIn" />

        <!-- 评分区域（需登录） -->
        <template v-if="auth.isLoggedIn">
          <h3>我的评分</h3>
          <div class="rate-area">
            <el-rate v-model="myRating" :max="5" allow-half show-score
              :texts="['很差','较差','还行','推荐','强烈推荐']" show-text />
            <el-button type="primary" :loading="submitting" :disabled="!myRating"
              @click="submitRating" style="margin-left:16px">提交评分</el-button>
          </div>
        </template>

        <el-alert v-else title="登录后可提交评分" type="info" show-icon :closable="false"
          style="margin-top:16px">
          <router-link to="/login">去登录</router-link>
        </el-alert>
      </el-card>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { apiMovies } from '@/api/movies'
import { apiRatings } from '@/api/ratings'

const route      = useRoute()
const auth       = useAuthStore()
const loading    = ref(true)
const submitting = ref(false)
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

async function submitRating() {
  if (!myRating.value) return
  submitting.value = true
  try {
    await apiRatings.submit(movie.value.movie_id, myRating.value)
    ElMessage.success('评分成功！感谢你的反馈')
    // 刷新平均分
    movie.value = await apiMovies.detail(route.params.id)
  } catch (msg) {
    ElMessage.error(typeof msg === 'string' ? msg : '评分失败')
  } finally {
    submitting.value = false
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
.rate-area   { display: flex; align-items: center; }
</style>
