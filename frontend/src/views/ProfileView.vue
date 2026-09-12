<template>
  <div class="profile-page">
    <!-- 用户信息卡片 -->
    <el-card class="user-info-card">
      <div class="user-info-content">
        <div class="avatar-section">
          <el-avatar :size="100" :src="userInfo.avatar_url" class="user-avatar">
            <template #default>
              <span class="avatar-text">{{ userInfo.username?.charAt(0).toUpperCase() }}</span>
            </template>
          </el-avatar>
          <el-button size="small" @click="editDialogVisible = true" class="edit-btn">
            编辑资料
          </el-button>
        </div>

        <div class="info-section">
          <h2 class="username">{{ userInfo.username }}</h2>
          <div class="user-meta">
            <span class="user-id">ID: {{ userInfo.id }}</span>
            <span v-if="userInfo.created_at" class="join-date">
              注册于 {{ formatDate(userInfo.created_at) }}
            </span>
          </div>
          <p v-if="userInfo.bio" class="bio">{{ userInfo.bio }}</p>
          <p v-else class="bio-empty">这个人很懒，什么都没写</p>
        </div>
      </div>
    </el-card>

    <!-- 编辑资料对话框 -->
    <el-dialog v-model="editDialogVisible" title="编辑资料" width="500px">
      <el-form :model="editForm" label-width="80px">
        <el-form-item label="用户名">
          <el-input v-model="editForm.username" maxlength="50" show-word-limit />
        </el-form-item>
        <el-form-item label="头像URL">
          <el-input v-model="editForm.avatar_url" placeholder="https://example.com/avatar.jpg" />
          <div class="form-hint">支持外链图片，建议尺寸 200x200</div>
        </el-form-item>
        <el-form-item label="个人签名">
          <el-input
            v-model="editForm.bio"
            type="textarea"
            :rows="4"
            maxlength="500"
            show-word-limit
            placeholder="介绍一下自己吧"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="handleUpdateInfo" :loading="updating">保存</el-button>
      </template>
    </el-dialog>

    <el-card class="stats-card">
      <template #header>
        <div class="card-header">
          <span>观影统计</span>
        </div>
      </template>

      <div v-if="loading" class="loading">
        <el-icon class="is-loading"><loading /></el-icon>
        加载中...
      </div>

      <div v-else class="stats-grid">
        <div class="stat-item">
          <div class="stat-value">{{ stats.total_ratings }}</div>
          <div class="stat-label">已评分电影</div>
        </div>
        <div class="stat-item">
          <div class="stat-value">{{ stats.avg_rating }}</div>
          <div class="stat-label">平均评分</div>
        </div>
        <div class="stat-item">
          <div class="stat-value">{{ stats.total_comments }}</div>
          <div class="stat-label">发表评论</div>
        </div>
      </div>

      <div v-if="!loading" class="preferences">
        <div class="preference-section">
          <h4>我可能喜欢的类型</h4>
          <div v-if="stats.top_genres && stats.top_genres.length > 0" class="tag-list">
            <el-tag v-for="item in stats.top_genres" :key="item.genre" type="success">
              {{ item.genre }} ({{ item.count }})
            </el-tag>
          </div>
          <el-empty v-else description="暂无数据" :image-size="60" />
        </div>

        <div class="preference-section">
          <h4>我可能喜欢的导演</h4>
          <div v-if="stats.top_directors && stats.top_directors.length > 0" class="tag-list">
            <el-tag v-for="item in stats.top_directors" :key="item.director" type="warning">
              {{ item.director }} ({{ item.count }})
            </el-tag>
          </div>
          <el-empty v-else description="暂无数据" :image-size="60" />
        </div>

        <div class="preference-section">
          <h4>我的评分分布</h4>
          <div v-if="hasRatings" class="distribution-chart">
            <div v-for="(count, rating) in stats.rating_distribution" :key="rating" class="bar-item">
              <span class="bar-label">{{ rating }}星</span>
              <div class="bar-container">
                <div class="bar" :style="{ width: getBarWidth(count) + '%' }"></div>
                <span class="bar-count">{{ count }}</span>
              </div>
            </div>
          </div>
          <el-empty v-else description="暂无评分数据" :image-size="60" />
        </div>
      </div>
    </el-card>

    <el-tabs v-model="activeTab" class="content-tabs">
      <el-tab-pane label="我的评分" name="ratings">
        <div class="toolbar">
          <el-select v-model="ratingsSort" @change="loadRatings" placeholder="排序方式">
            <el-option label="最新评分" value="time_desc" />
            <el-option label="最早评分" value="time_asc" />
            <el-option label="评分最高" value="rating_desc" />
            <el-option label="评分最低" value="rating_asc" />
          </el-select>
        </div>

        <div v-if="ratingsLoading" class="loading">
          <el-icon class="is-loading"><loading /></el-icon>
          加载中...
        </div>

        <div v-else-if="ratings.items.length === 0" class="empty-state">
          <el-empty description="还没有评分记录" />
        </div>

        <div v-else class="ratings-list">
          <div v-for="item in ratings.items" :key="item.movie_id" class="rating-item">
            <router-link :to="`/movies/${item.movie_id}`" class="movie-link">
              <img
                v-if="item.poster_url"
                :src="item.poster_url"
                :alt="item.title"
                class="movie-poster"
              />
              <div v-else class="poster-placeholder">暂无海报</div>
            </router-link>

            <div class="rating-info">
              <router-link :to="`/movies/${item.movie_id}`" class="movie-title">
                {{ item.title }} <span v-if="item.year" class="year">({{ item.year }})</span>
              </router-link>
              <div class="movie-meta">
                <span v-if="item.genres" class="genres">{{ item.genres }}</span>
                <span v-if="item.director" class="director">导演: {{ item.director }}</span>
              </div>
              <div class="rating-detail">
                <el-rate :model-value="item.rating" disabled allow-half />
                <span class="rating-value">{{ item.rating }}</span>
                <span v-if="item.rated_at" class="rated-time">{{ formatTime(item.rated_at) }}</span>
              </div>
            </div>
          </div>
        </div>

        <el-pagination
          v-if="ratings.total > 0"
          v-model:current-page="ratingsPage"
          :page-size="pageSize"
          :total="ratings.total"
          layout="prev, pager, next, total"
          @current-change="loadRatings"
          class="pagination"
        />
      </el-tab-pane>

      <el-tab-pane label="我的评论" name="comments">
        <div v-if="commentsLoading" class="loading">
          <el-icon class="is-loading"><loading /></el-icon>
          加载中...
        </div>

        <div v-else-if="comments.items.length === 0" class="empty-state">
          <el-empty description="还没有发表评论" />
        </div>

        <div v-else class="comments-list">
          <div v-for="item in comments.items" :key="item.comment_id" class="comment-item">
            <router-link :to="`/movies/${item.movie_id}`" class="movie-link">
              <img
                v-if="item.poster_url"
                :src="item.poster_url"
                :alt="item.title"
                class="movie-poster"
              />
              <div v-else class="poster-placeholder">暂无海报</div>
            </router-link>

            <div class="comment-content">
              <router-link :to="`/movies/${item.movie_id}`" class="movie-title">
                {{ item.title }} <span v-if="item.year" class="year">({{ item.year }})</span>
              </router-link>
              <p class="comment-text">{{ item.content }}</p>
              <span v-if="item.created_at" class="comment-time">{{ formatTime(item.created_at) }}</span>
            </div>
          </div>
        </div>

        <el-pagination
          v-if="comments.total > 0"
          v-model:current-page="commentsPage"
          :page-size="pageSize"
          :total="comments.total"
          layout="prev, pager, next, total"
          @current-change="loadComments"
          class="pagination"
        />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import { getUserInfo, updateUserInfo, getUserStats, getUserRatings, getUserComments } from '@/api/profile'
import { ElMessage } from 'element-plus'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()

// 用户基本信息
const userInfo = ref({
  id: null,
  username: '',
  avatar_url: '',
  bio: '',
  created_at: ''
})

const editDialogVisible = ref(false)
const updating = ref(false)
const editForm = ref({
  username: '',
  avatar_url: '',
  bio: ''
})

const loading = ref(true)
const stats = ref({
  total_ratings: 0,
  avg_rating: 0,
  total_comments: 0,
  rating_distribution: {},
  top_genres: [],
  top_directors: []
})

const activeTab = ref('ratings')
const pageSize = 20

// 评分列表
const ratingsLoading = ref(false)
const ratingsPage = ref(1)
const ratingsSort = ref('time_desc')
const ratings = ref({
  items: [],
  total: 0
})

// 评论列表
const commentsLoading = ref(false)
const commentsPage = ref(1)
const comments = ref({
  items: [],
  total: 0
})

const hasRatings = computed(() => {
  return stats.value.total_ratings > 0
})

const maxRatingCount = computed(() => {
  if (!stats.value.rating_distribution) return 1
  return Math.max(...Object.values(stats.value.rating_distribution), 1)
})

function getBarWidth(count) {
  return (count / maxRatingCount.value) * 100
}

function formatTime(isoString) {
  const date = new Date(isoString)
  const now = new Date()
  const diff = now - date

  const minutes = Math.floor(diff / 60000)
  const hours = Math.floor(diff / 3600000)
  const days = Math.floor(diff / 86400000)

  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes}分钟前`
  if (hours < 24) return `${hours}小时前`
  if (days < 7) return `${days}天前`

  return date.toLocaleDateString('zh-CN')
}

function formatDate(isoString) {
  const date = new Date(isoString)
  const options = {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone
  }
  return date.toLocaleDateString('zh-CN', options)
}

async function loadUserInfo() {
  try {
    userInfo.value = await getUserInfo()
    // 初始化编辑表单
    editForm.value = {
      username: userInfo.value.username,
      avatar_url: userInfo.value.avatar_url || '',
      bio: userInfo.value.bio || ''
    }
  } catch (error) {
    console.error('加载用户信息失败:', error)
    ElMessage.error('加载用户信息失败')
  }
}

async function handleUpdateInfo() {
  try {
    updating.value = true
    const updated = await updateUserInfo(editForm.value)
    userInfo.value = updated
    editDialogVisible.value = false
    ElMessage.success('资料更新成功')

    // 更新全局 auth store 中的用户名
    if (auth.user) {
      auth.user.username = updated.username
    }
  } catch (error) {
    console.error('更新用户信息失败:', error)
    ElMessage.error(error.response?.data?.error || '更新失败')
  } finally {
    updating.value = false
  }
}

async function loadStats() {
  try {
    loading.value = true
    stats.value = await getUserStats()
  } catch (error) {
    console.error('加载统计数据失败:', error)
  } finally {
    loading.value = false
  }
}

async function loadRatings() {
  try {
    ratingsLoading.value = true
    ratings.value = await getUserRatings(ratingsPage.value, pageSize, ratingsSort.value)
  } catch (error) {
    console.error('加载评分列表失败:', error)
  } finally {
    ratingsLoading.value = false
  }
}

async function loadComments() {
  try {
    commentsLoading.value = true
    comments.value = await getUserComments(commentsPage.value, pageSize)
  } catch (error) {
    console.error('加载评论列表失败:', error)
  } finally {
    commentsLoading.value = false
  }
}

onMounted(async () => {
  await loadUserInfo()
  await loadStats()
  await loadRatings()
  await loadComments()
})
</script>

<style scoped>
.profile-page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 20px;
}

.user-info-card {
  margin-bottom: 20px;
}

.user-info-content {
  display: flex;
  gap: 24px;
}

.avatar-section {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}

.user-avatar {
  border: 3px solid #f0f0f0;
}

.avatar-text {
  font-size: 36px;
  font-weight: bold;
  color: #409eff;
}

.edit-btn {
  width: 100%;
}

.info-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.username {
  margin: 0;
  font-size: 24px;
  color: #303133;
}

.user-meta {
  display: flex;
  gap: 16px;
  font-size: 14px;
  color: #909399;
}

.user-id {
  font-family: monospace;
}

.bio {
  margin: 8px 0 0 0;
  font-size: 15px;
  color: #606266;
  line-height: 1.6;
}

.bio-empty {
  margin: 8px 0 0 0;
  font-size: 14px;
  color: #c0c4cc;
  font-style: italic;
}

.form-hint {
  font-size: 12px;
  color: #909399;
  margin-top: 4px;
}

.stats-card {
  margin-bottom: 20px;
}

.card-header {
  font-size: 18px;
  font-weight: bold;
}

.loading {
  text-align: center;
  padding: 40px;
  color: #909399;
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 20px;
  margin-bottom: 30px;
}

.stat-item {
  text-align: center;
  padding: 20px;
  background: #f5f7fa;
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
  color: #606266;
}

.preferences {
  display: grid;
  gap: 24px;
}

.preference-section h4 {
  margin: 0 0 12px 0;
  color: #303133;
  font-size: 16px;
}

.tag-list {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.distribution-chart {
  display: grid;
  gap: 8px;
}

.bar-item {
  display: grid;
  grid-template-columns: 60px 1fr;
  align-items: center;
  gap: 12px;
}

.bar-label {
  font-size: 14px;
  color: #606266;
  text-align: right;
}

.bar-container {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
}

.bar {
  height: 24px;
  background: linear-gradient(90deg, #409eff, #79bbff);
  border-radius: 4px;
  transition: width 0.3s ease;
  min-width: 2px;
}

.bar-count {
  font-size: 14px;
  color: #606266;
  min-width: 30px;
}

.content-tabs {
  background: white;
  padding: 20px;
  border-radius: 8px;
}

.toolbar {
  margin-bottom: 20px;
}

.empty-state {
  padding: 40px 0;
}

.ratings-list,
.comments-list {
  display: grid;
  gap: 16px;
}

.rating-item,
.comment-item {
  display: flex;
  gap: 16px;
  padding: 16px;
  background: #f5f7fa;
  border-radius: 8px;
  transition: background 0.2s;
}

.rating-item:hover,
.comment-item:hover {
  background: #e8eaf0;
}

.movie-link {
  flex-shrink: 0;
}

.movie-poster {
  width: 80px;
  height: 120px;
  object-fit: cover;
  border-radius: 4px;
}

.poster-placeholder {
  width: 80px;
  height: 120px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #dcdfe6;
  border-radius: 4px;
  font-size: 12px;
  color: #909399;
}

.rating-info,
.comment-content {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.movie-title {
  font-size: 16px;
  font-weight: 500;
  color: #303133;
  text-decoration: none;
}

.movie-title:hover {
  color: #409eff;
}

.year {
  color: #909399;
  font-weight: normal;
}

.movie-meta {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 14px;
  color: #606266;
}

.rating-detail {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: auto;
}

.rating-value {
  font-size: 18px;
  font-weight: bold;
  color: #409eff;
}

.rated-time,
.comment-time {
  font-size: 12px;
  color: #909399;
  margin-left: auto;
}

.comment-text {
  margin: 0;
  font-size: 14px;
  color: #606266;
  line-height: 1.6;
}

.pagination {
  margin-top: 20px;
  justify-content: center;
}
</style>
