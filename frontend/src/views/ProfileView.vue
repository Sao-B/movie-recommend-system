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

    <el-card class="comments-card">
      <template #header>
        <div class="comments-head">
          <span>我的评论</span>
          <span v-if="comments.total" class="count-hint">共 {{ comments.total }} 条</span>
        </div>
      </template>

      <div v-if="commentsLoading" class="loading">
        <el-icon class="is-loading"><loading /></el-icon>
        加载中...
      </div>

      <div v-else-if="comments.items.length === 0" class="empty-state">
        <el-empty description="还没有发表过评论" :image-size="80" />
      </div>

      <div v-else class="comment-list">
        <div v-for="item in comments.items" :key="item.comment_id" class="comment-item">
          <!-- 编辑态：星级与正文一起改，点「保存」才落库。同一时刻只编辑一条 -->
          <template v-if="editingId === item.comment_id">
            <div class="edit-row">
              <span class="movie-title">{{ item.title }}</span>
              <el-rate v-model="draftRating" :max="5" allow-half show-score size="small" />
            </div>
            <el-input
              v-model="draft"
              type="textarea"
              :rows="3"
              maxlength="500"
              show-word-limit
              placeholder="修改你的评论"
            />
            <div class="edit-actions">
              <el-button size="small" @click="cancelEdit">取消</el-button>
              <el-button
                size="small"
                type="primary"
                :loading="saving"
                :disabled="!draft.trim()"
                @click="saveEdit(item)"
              >
                保存
              </el-button>
            </div>
          </template>

          <template v-else>
            <div class="comment-head">
              <router-link :to="`/movies/${item.movie_id}`" class="movie-title">
                {{ item.title }}<span v-if="item.year" class="year"> ({{ item.year }})</span>
              </router-link>
              <!-- 评分与影评是一起发表的，星级直接挂在这张评论卡上（评分被单独删掉则不显示） -->
              <span v-if="item.user_rating" class="comment-score">
                <el-rate :model-value="item.user_rating" disabled allow-half size="small" />
                <span class="score-value">{{ item.user_rating }}</span>
              </span>
            </div>

            <div class="comment-text">{{ item.content }}</div>

            <div class="comment-foot">
              <span class="comment-time">{{ formatTime(item.created_at) }}</span>
              <el-button text type="primary" size="small" @click="startEdit(item)">编辑</el-button>
              <el-button text type="danger" size="small" @click="handleDeleteComment(item)">
                删除
              </el-button>
            </div>
          </template>
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
    </el-card>
  </div>
</template>

<script setup>
import { ref, computed, onMounted } from 'vue'
import { Loading } from '@element-plus/icons-vue'
import { getUserInfo, updateUserInfo, getUserStats, getUserComments } from '@/api/profile'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiComments } from '@/api/comments'
import { apiRatings } from '@/api/ratings'
import { useAuthStore } from '@/stores/auth'
import { formatRelative as formatTime } from '@/utils/format'

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

const pageSize = 20

// 评论列表
const commentsLoading = ref(false)
const commentsPage = ref(1)
const comments = ref({
  items: [],
  total: 0
})

// 正在编辑的那一条（同一时刻只许一条）及其草稿
const editingId = ref(null)
const draft = ref('')
const draftRating = ref(0)
const saving = ref(false)

function startEdit(item) {
  editingId.value = item.comment_id
  draft.value = item.content
  draftRating.value = item.user_rating ?? 0
}

function cancelEdit() {
  editingId.value = null
  draft.value = ''
  draftRating.value = 0
}

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
  } catch {
    // 错误提示由 axios 拦截器统一弹出
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
  } catch {
    // 错误提示由 axios 拦截器统一弹出
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

// 保存编辑：与详情页影评卡一致，先落评分、再落正文，都点了保存才提交
async function saveEdit(item) {
  const content = draft.value.trim()
  if (!content) {
    ElMessage.warning('评论内容不能为空')
    return
  }

  // 0 不是合法评分（后端只收 0.5 ~ 5.0），不当成「改了」
  const rating = Number(draftRating.value)
  const ratingChanged = rating > 0 && rating !== (item.user_rating ?? 0)
  const contentChanged = content !== item.content

  // 什么都没改：只收起编辑区，不去白刷一次 edited_at
  if (!ratingChanged && !contentChanged) {
    cancelEdit()
    return
  }

  saving.value = true
  try {
    if (ratingChanged) await apiRatings.submit(item.movie_id, rating)
    if (contentChanged) await apiComments.update(item.comment_id, content)
    ElMessage.success(
      ratingChanged && contentChanged ? '评分与评论已更新' : ratingChanged ? '评分已更新' : '评论已更新'
    )
    cancelEdit()
    // 评论时间与观影统计都会变，一起刷新
    await Promise.all([loadComments(), loadStats()])
  } catch {} finally {
    saving.value = false
  }
}

// 删除评论。评分与影评是一起发表的，这里连同评分一起删（后端在同一次提交里删两行），
// 删完观影统计里的「已评分电影 / 平均评分 / 分布」也会变，所以一起刷新。
async function handleDeleteComment(item) {
  try {
    await ElMessageBox.confirm(
      `删除《${item.title}》的评论会连同你对它的评分一起删除，确定吗？`,
      '提示',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return // 用户取消
  }

  try {
    await apiComments.delete(item.comment_id, true)
    ElMessage.success('评论与评分已删除')
    // 删掉的是当前页最后一条时，回退一页再刷新，避免停在空页
    if (comments.value.items.length === 1 && commentsPage.value > 1) {
      commentsPage.value -= 1
    }
    await Promise.all([loadComments(), loadStats()])
  } catch {}
}

onMounted(async () => {
  await loadUserInfo()
  await loadStats()
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
  border: 3px solid var(--app-border-light);
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
  color: var(--app-text);
}

.user-meta {
  display: flex;
  gap: 16px;
  font-size: 14px;
  color: var(--app-text-muted);
}

.user-id {
  font-family: monospace;
}

.bio {
  margin: 8px 0 0 0;
  font-size: 15px;
  color: var(--app-text-secondary);
  line-height: 1.6;
}

.bio-empty {
  margin: 8px 0 0 0;
  font-size: 14px;
  color: var(--app-text-muted);
  font-style: italic;
}

.form-hint {
  font-size: 12px;
  color: var(--app-text-muted);
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
  color: var(--app-text-muted);
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

.preferences {
  display: grid;
  gap: 24px;
}

.preference-section h4 {
  margin: 0 0 12px 0;
  color: var(--app-text);
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
  color: var(--app-text-secondary);
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
  color: var(--app-text-secondary);
  min-width: 30px;
}

/* 这一块原来写死 background:white，夜间模式下是块白斑，现改为跟随主题的表面色 */
.comments-card {
  background: var(--app-bg-surface);
}

.comments-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  font-size: 16px;
  font-weight: 500;
  color: var(--app-text);
}

.count-hint {
  font-size: 13px;
  font-weight: 400;
  color: var(--app-text-muted);
}

.empty-state {
  padding: 40px 0;
}

.comment-list {
  display: grid;
  gap: 12px;
}

/* 与详情页的评论卡同一套观感：浅填充底 + 圆角，颜色全部走主题变量 */
.comment-item {
  padding: 16px;
  background: var(--app-fill);
  border-radius: 8px;
}

.comment-head {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.movie-title {
  font-size: 15px;
  font-weight: 500;
  color: var(--app-text);
  text-decoration: none;
}

.movie-title:hover {
  color: #409eff;
}

.year {
  color: var(--app-text-muted);
  font-weight: 400;
}

.comment-score {
  display: inline-flex;
  align-items: center;
  gap: 8px;
}

.score-value {
  font-size: 13px;
  font-weight: 500;
  color: #e6a23c;
}

.comment-text {
  margin: 0;
  font-size: 14px;
  color: var(--app-text-secondary);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

/* 编辑态：片名 + 可点的星星在上，正文框在下 */
.edit-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 10px;
}

.edit-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}

.comment-foot {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 12px;
}

.comment-time {
  font-size: 12px;
  color: var(--app-text-muted);
  margin-right: auto;
}

.pagination {
  margin-top: 20px;
  justify-content: center;
}
</style>
