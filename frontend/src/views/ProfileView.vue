<template>
  <div class="profile-page">
    <!-- 用户信息卡片 -->
    <el-card class="user-info-card">
      <div class="user-info-content">
        <!-- 头像：点一下选本地图片，选完立刻上传 -->
        <div class="avatar-section">
          <el-tooltip
            content="点击更换头像"
            placement="bottom"
            :show-after="200"
            :disabled="avatarUploading"
          >
            <div
              class="avatar-clickable"
              :class="{ 'is-uploading': avatarUploading }"
              @click="openAvatarPicker"
            >
              <el-avatar :size="100" :src="userInfo.avatar_url" class="user-avatar">
                <template #default>
                  <span class="avatar-text">{{ userInfo.username?.charAt(0).toUpperCase() }}</span>
                </template>
              </el-avatar>
              <div class="avatar-mask">
                <el-icon><Camera /></el-icon>
                <span>{{ avatarUploading ? '上传中…' : '更换头像' }}</span>
              </div>
            </div>
          </el-tooltip>
          <!-- 真正的文件选择器，藏起来由头像代点 -->
          <input
            ref="avatarInputRef"
            class="avatar-file-input"
            type="file"
            accept="image/png,image/jpeg,image/gif,image/webp"
            @change="handleAvatarChange"
          />
        </div>

        <div class="info-section">
          <!-- 用户名：点击文本直接改，回车或「保存」落库 -->
          <div v-if="editingField === 'username'" class="edit-block">
            <el-input
              ref="usernameInputRef"
              v-model="usernameDraft"
              size="large"
              maxlength="50"
              show-word-limit
              placeholder="用户名"
              @keyup.enter="saveUsername"
              @keyup.esc="cancelEdit"
            />
            <div class="edit-block-actions">
              <el-button size="small" text @click="cancelEdit">取消</el-button>
              <el-button
                size="small"
                type="primary"
                :loading="savingField === 'username'"
                @click="saveUsername"
              >
                保存
              </el-button>
            </div>
          </div>
          <el-tooltip v-else content="点击编辑用户名" placement="top" :show-after="200">
            <h2
              class="username editable-text"
              tabindex="0"
              @click="startEdit('username')"
              @keyup.enter="startEdit('username')"
            >
              {{ userInfo.username }}
            </h2>
          </el-tooltip>

          <div class="user-meta">
            <span class="user-id">ID: {{ userInfo.id }}</span>
            <span v-if="userInfo.created_at" class="join-date">
              注册于 {{ formatDate(userInfo.created_at) }}
            </span>
          </div>

          <!-- 个性签名：同用户名，点文本即改 -->
          <div v-if="editingField === 'bio'" class="edit-block">
            <el-input
              v-model="bioDraft"
              type="textarea"
              :rows="3"
              maxlength="500"
              show-word-limit
              placeholder="介绍一下自己吧"
              @keyup.esc="cancelEdit"
            />
            <div class="edit-block-actions">
              <el-button size="small" text @click="cancelEdit">取消</el-button>
              <el-button
                size="small"
                type="primary"
                :loading="savingField === 'bio'"
                @click="saveBio"
              >
                保存
              </el-button>
            </div>
          </div>
          <el-tooltip v-else content="点击编辑个性签名" placement="top" :show-after="200">
            <div
              class="bio-line editable-text"
              tabindex="0"
              @click="startEdit('bio')"
              @keyup.enter="startEdit('bio')"
            >
              <p v-if="userInfo.bio" class="bio">{{ userInfo.bio }}</p>
              <p v-else class="bio-empty">这个人很懒，什么都没写</p>
            </div>
          </el-tooltip>
        </div>
      </div>
    </el-card>

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
              <el-button size="small" @click="cancelEditComment">取消</el-button>
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
              <el-button text type="primary" size="small" @click="startEditComment(item)">编辑</el-button>
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
import { ref, computed, nextTick, onMounted } from 'vue'
import { Camera } from '@element-plus/icons-vue'
import { getUserInfo, updateUserInfo, uploadAvatar, getUserStats, getUserComments } from '@/api/profile'
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

// 「点击即改」的两个字段：同一时刻只开一个编辑区
// username → 用户名输入框；bio → 签名文本域；null → 全部是只读文本
const editingField  = ref(null)
const savingField   = ref('')
const usernameDraft = ref('')
const bioDraft      = ref('')
const usernameInputRef = ref(null)

// 头像上传
const avatarInputRef  = ref(null)
const avatarUploading = ref(false)

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

// ---------- 用户资料：点击文本直接编辑 ----------

function startEdit(field) {
  if (savingField.value) return
  editingField.value = field
  if (field === 'username') {
    usernameDraft.value = userInfo.value.username || ''
    nextTick(() => usernameInputRef.value?.focus())
  } else {
    bioDraft.value = userInfo.value.bio || ''
  }
}

function cancelEdit() {
  editingField.value = null
  usernameDraft.value = ''
  bioDraft.value = ''
}

async function saveUsername() {
  const username = usernameDraft.value.trim()
  if (!username) {
    ElMessage.warning('用户名不能为空')
    return
  }
  // 没改动就别白跑一次请求（后端也会因为重名把自己判成冲突）
  if (username === userInfo.value.username) {
    cancelEdit()
    return
  }

  savingField.value = 'username'
  try {
    const updated = await updateUserInfo({ username })
    userInfo.value = updated
    auth.updateUser({ username: updated.username })  // header 里的显示名跟着变
    ElMessage.success('用户名已更新')
    cancelEdit()
  } catch {
    // 错误提示由 axios 拦截器统一弹出（重名、超长都在这里返回 400）
  } finally {
    savingField.value = ''
  }
}

async function saveBio() {
  const bio = bioDraft.value
  if (bio === (userInfo.value.bio || '')) {
    cancelEdit()
    return
  }

  savingField.value = 'bio'
  try {
    const updated = await updateUserInfo({ bio })
    userInfo.value = updated
    ElMessage.success('个性签名已更新')
    cancelEdit()
  } catch {
    // 同上
  } finally {
    savingField.value = ''
  }
}

// ---------- 头像：点头像选文件并上传 ----------

function openAvatarPicker() {
  if (avatarUploading.value) return
  avatarInputRef.value?.click()
}

async function handleAvatarChange(event) {
  const input = event.target
  const file = input.files && input.files[0]
  // 先清空 value，否则连着选同一个文件不会再触发 change
  input.value = ''
  if (!file) return

  // 前端先挡一道，省一次白跑的请求；后端同样会校验扩展名、文件头与大小
  if (!file.type.startsWith('image/')) {
    ElMessage.warning('请选择图片文件')
    return
  }
  if (file.size > 2 * 1024 * 1024) {
    ElMessage.warning('图片不能超过 2MB')
    return
  }

  avatarUploading.value = true
  try {
    const updated = await uploadAvatar(file)
    userInfo.value = updated
    auth.updateUser({ avatar_url: updated.avatar_url })
    ElMessage.success('头像已更新')
  } catch {
    // 错误提示由 axios 拦截器统一弹出
  } finally {
    avatarUploading.value = false
  }
}

// ---------- 观影统计 / 评论 ----------

function startEditComment(item) {
  editingId.value = item.comment_id
  draft.value = item.content
  draftRating.value = item.user_rating ?? 0
}

function cancelEditComment() {
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
  } catch {
    // 错误提示由 axios 拦截器统一弹出
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
    cancelEditComment()
    return
  }

  saving.value = true
  try {
    if (ratingChanged) await apiRatings.submit(item.movie_id, rating)
    if (contentChanged) await apiComments.update(item.comment_id, content)
    ElMessage.success(
      ratingChanged && contentChanged ? '评分与评论已更新' : ratingChanged ? '评分已更新' : '评论已更新'
    )
    cancelEditComment()
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

/* 头像本身就是「更换头像」的按钮：悬停盖一层遮罩提示可点 */
.avatar-clickable {
  position: relative;
  width: 100px;
  height: 100px;
  border-radius: 50%;
  overflow: hidden;
  cursor: pointer;
}

.avatar-mask {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  background: rgba(0, 0, 0, 0.55);
  color: #fff;
  font-size: 12px;
  opacity: 0;
  transition: opacity 0.2s;
}

.avatar-clickable:hover .avatar-mask { opacity: 1; }

/* 上传中常显遮罩，顺带挡住重复点击 */
.avatar-clickable.is-uploading { cursor: default; }
.avatar-clickable.is-uploading .avatar-mask { opacity: 1; }

/* 文件选择器由头像代点，本体不出现 */
.avatar-file-input { display: none; }

.user-avatar {
  border: 3px solid var(--app-border-light);
}

.avatar-text {
  font-size: 36px;
  font-weight: bold;
  color: #409eff;
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

/* 可点击编辑的文本：悬停时描一圈浅色边框，提示「这里能改」 */
.editable-text {
  cursor: pointer;
  border-radius: 4px;
  transition: color 0.2s, background-color 0.2s, box-shadow 0.2s;
}

.editable-text:hover,
.editable-text:focus-visible {
  color: #409eff;
  background: var(--app-fill);
  box-shadow: 0 0 0 4px var(--app-fill);
  outline: none;
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

/* 编辑态：输入框在上，取消/保存在右下 */
.edit-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.edit-block-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
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
