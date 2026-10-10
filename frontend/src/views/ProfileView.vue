<template>
  <div class="profile-page">
    <!-- 用户信息卡片 -->
    <el-card class="user-info-card">
      <div class="user-info-content">
        <!-- 头像：点一下打开选择面板（内置 SVG 头像 + 恢复默认） -->
        <div class="avatar-section">
          <div class="avatar-clickable" @click="openAvatarPicker">
            <UserAvatar :src="userInfo.avatar_url" :name="displayName" :size="100" />
            <div class="avatar-mask">
              <el-icon><Camera /></el-icon>
              <span>更换头像</span>
            </div>
          </div>
        </div>

        <div class="info-section">
          <!-- 昵称：点击文本直接改，回车或「保存」落库。账号是登录凭据（注册时按规则定下），
               不在这里改，只在下面只读展示。 -->
          <div v-if="editingField === 'nickname'" class="edit-block">
            <el-input
              ref="nicknameInputRef"
              v-model="nicknameDraft"
              size="large"
              maxlength="50"
              show-word-limit
              placeholder="昵称"
              @keyup.enter="saveNickname"
              @keyup.esc="cancelEdit"
            />
            <div class="edit-block-actions">
              <el-button size="small" text @click="cancelEdit">取消</el-button>
              <el-button
                size="small"
                type="primary"
                :loading="savingField === 'nickname'"
                @click="saveNickname"
              >
                保存
              </el-button>
            </div>
          </div>
          <h2
            v-else
            class="username editable-text"
            tabindex="0"
            @click="startEdit('nickname')"
            @keyup.enter="startEdit('nickname')"
          >
            {{ displayName }}
          </h2>

          <div class="user-meta">
            <span class="user-id">账号：{{ userInfo.username }}</span>
            <span class="user-id">ID: {{ userInfo.id }}</span>
          </div>

          <!-- 个性签名：同昵称，点文本即改 -->
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
          <div
            v-else
            class="bio-line editable-text"
            tabindex="0"
            @click="startEdit('bio')"
            @keyup.enter="startEdit('bio')"
          >
            <p v-if="userInfo.bio" class="bio">{{ userInfo.bio }}</p>
            <p v-else class="bio-empty">这个人很懒，什么都没写</p>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 偏好设置：类型偏好 + 不感兴趣，一张卡集中管理（推荐结果由这两样共同决定） -->
    <el-card class="preferences-card">
      <template #header>
        <div class="card-header">
          <span>偏好设置</span>
        </div>
      </template>

      <div class="pref-section">
        <div class="pref-head">
          <h4>喜欢的电影类型</h4>
          <el-button type="primary" @click="genreDialogVisible = true">
            <el-icon><Edit /></el-icon>
            修改偏好
          </el-button>
        </div>
        <p class="pref-hint">没看过的新电影会优先按这些类型推荐（最多 5 个）</p>
        <div class="genres-tags">
          <el-tag
            v-for="g in (userInfo.favorite_genres || [])"
            :key="g"
            class="genre-chip"
          >{{ g }}</el-tag>
          <span v-if="!(userInfo.favorite_genres || []).length" class="genres-empty">
            还没选过，推荐会按全站热门来
          </span>
        </div>
      </div>

      <el-divider class="pref-divider" />

      <div class="pref-section">
        <div class="pref-head">
          <h4>不感兴趣的电影</h4>
          <span v-if="dislikes.length" class="pref-count">共 {{ dislikes.length }} 部</span>
        </div>
        <p class="pref-hint">标记过的电影不会出现在任何推荐里；取消标记后可能重新出现</p>
        <div v-loading="dislikesLoading">
          <div v-if="dislikes.length" class="dislike-list">
            <div v-for="d in dislikes" :key="d.movie_id" class="dislike-item">
              <router-link :to="`/movies/${d.movie_id}`" class="dislike-title">
                {{ dislikeTitle(d) }}<span v-if="d.year" class="year"> ({{ d.year }})</span>
              </router-link>
              <el-tag
                v-for="g in (d.genres || []).slice(0, 3)"
                :key="g"
                size="small"
                type="info"
              >{{ g }}</el-tag>
              <el-button text type="danger" size="small" class="dislike-remove" @click="removeDislike(d)">
                取消标记
              </el-button>
            </div>
          </div>
          <el-empty v-else description="没有标记过不感兴趣的电影" :image-size="60" />
        </div>
      </div>
    </el-card>

    <!-- 选类型：首次引导 / 个人主页编辑共用同一弹窗组件 -->
    <GenrePickerDialog
      :visible="genreDialogVisible"
      title="修改喜欢的电影类型"
      @close="genreDialogVisible = false"
      @saved="onGenresSaved"
    />

    <!-- 选头像：内置 SVG 头像网格 + 恢复默认，点哪个立刻保存并关窗 -->
    <el-dialog v-model="avatarPickerVisible" title="选择头像" width="440px">
      <AvatarPicker
        :presets="presetAvatars"
        :current="userInfo.avatar_url || ''"
        :saving="avatarSaving"
        @select="chooseAvatar"
        @reset="chooseAvatar(null)"
      />
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

        <div class="preference-section preference-section-wide">
          <h4>我的评分分布</h4>
          <!-- 没有评分时用等高容器占位，避免这一块塌下去、页面跳动 -->
          <div class="chart-area">
            <div v-if="hasRatings" class="distribution-chart">
              <div class="chart-y">
                <span
                  v-for="(tick, i) in yTicks"
                  :key="i"
                  :style="{ bottom: (tick / maxRatingCount) * 100 + '%' }"
                >{{ tick }}</span>
              </div>
              <div class="chart-plot">
                <span
                  v-for="(tick, i) in yTicks"
                  :key="'grid' + i"
                  class="grid-line"
                  :style="{ bottom: (tick / maxRatingCount) * 100 + '%' }"
                ></span>
                <div v-for="(count, rating) in stats.rating_distribution" :key="rating" class="bar-col">
                  <span class="bar" :style="{ height: getBarHeight(count) + '%' }"></span>
                  <span class="bar-count" :style="{ bottom: getBarHeight(count) + '%' }">{{ count }}</span>
                </div>
              </div>
              <div class="chart-x">
                <span
                  v-for="(count, rating) in stats.rating_distribution"
                  :key="'label' + rating"
                  class="bar-label"
                >{{ rating }}</span>
              </div>
            </div>
            <el-empty v-else description="暂无评分数据" :image-size="60" />
          </div>
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
          <!-- 作者头像：这里列的全是自己的评论，所以头像就是自己的资料（有图用图，没有回落首字符） -->
          <UserAvatar :src="userInfo.avatar_url" :name="displayName" :size="32" />

          <div class="comment-body">
            <!-- 编辑态：星级与正文一起改，点「保存」才落库。同一时刻只编辑一条 -->
            <CommentEditForm
              v-if="editingId === item.comment_id"
              v-model="draft"
              :rows="3"
              placeholder="修改你的评论"
              :saving="saving"
              @cancel="cancelEditComment"
              @save="saveEdit(item)"
            >
              <div class="edit-row">
                <span class="movie-title">{{ item.title }}</span>
                <el-rate v-model="draftRating" :max="5" allow-half show-score size="small" />
              </div>
            </CommentEditForm>

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
import { Camera, Edit } from '@element-plus/icons-vue'
import UserAvatar from '@/components/UserAvatar.vue'
import AvatarPicker from '@/components/AvatarPicker.vue'
import CommentEditForm from '@/components/CommentEditForm.vue'
import GenrePickerDialog from '@/components/GenrePickerDialog.vue'
import { getUserInfo, updateUserInfo, getPresetAvatars, getUserStats, getUserComments } from '@/api/profile'
import { apiDislikes } from '@/api/dislikes'
import { ElMessage, ElMessageBox } from 'element-plus'
import { apiComments } from '@/api/comments'
import { apiRatings } from '@/api/ratings'
import { useAuthStore } from '@/stores/auth'
import { formatRelative as formatTime } from '@/utils/format'

const auth = useAuthStore()

// 用户基本信息（nickname 为空时显示回落账号，见 displayName）
const userInfo = ref({
  id: null,
  username: '',
  nickname: '',
  avatar_url: '',
  bio: '',
  created_at: ''
})

// 页面上显示的名字：昵称优先、回落账号，与后端 User.display_name 同一口径
const displayName = computed(() => userInfo.value.nickname || userInfo.value.username)

// 「点击即改」的两个字段：同一时刻只开一个编辑区
// nickname → 昵称输入框；bio → 签名文本域；null → 全部是只读文本
const editingField  = ref(null)
const savingField   = ref('')
const nicknameDraft = ref('')
const bioDraft      = ref('')
const nicknameInputRef = ref(null)

// 头像：内置 SVG 头像清单 + 选择面板的开关/保存态
const avatarPickerVisible = ref(false)

// 喜欢的类型：弹窗保存后后端返回新 userInfo（含 favorite_genres），直接替换
const genreDialogVisible = ref(false)
function onGenresSaved(list) {
  userInfo.value = { ...userInfo.value, favorite_genres: list, needs_onboarding: false }
  auth.updateUser({ favorite_genres: list, needs_onboarding: false })
  ElMessage.success('偏好类型已更新，推荐会参考它')
}

// 不感兴趣：GET /api/dislikes 的 items（带片名/年份/类型），列表可逐条取消标记
const dislikes        = ref([])
const dislikesLoading = ref(false)

async function loadDislikes() {
  dislikesLoading.value = true
  try {
    const data = await apiDislikes.list()
    dislikes.value = data.items || []
  } catch {
    // 错误提示由 axios 拦截器统一弹出
  } finally {
    dislikesLoading.value = false
  }
}

async function removeDislike(item) {
  try {
    await ElMessageBox.confirm(
      `取消后《${dislikeTitle(item)}》可能重新出现在推荐里。确定吗？`,
      '取消不感兴趣',
      { confirmButtonText: '取消标记', cancelButtonText: '再想想', type: 'warning' }
    )
  } catch {
    return // 用户取消
  }
  try {
    await apiDislikes.remove(item.movie_id)
    dislikes.value = dislikes.value.filter((x) => x.movie_id !== item.movie_id)
    ElMessage.success('已取消标记，这部电影可能重新出现在推荐里')
  } catch {
    // 同上
  }
}

// MovieLens 片名自带「 (1994)」年份尾巴，展示时去掉再统一补一次，避免出现两个年份
function dislikeTitle(item) {
  if (!item.title) return `电影 ${item.movie_id}`
  return item.title.replace(/\s*\(\d{4}\)\s*$/, '')
}
const presetAvatars       = ref([])
const avatarSaving        = ref(false)

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

// ---------- 用户资料：点击文本直接编辑（昵称 / 签名；账号不可改） ----------

function startEdit(field) {
  if (savingField.value) return
  editingField.value = field
  if (field === 'nickname') {
    nicknameDraft.value = userInfo.value.nickname || userInfo.value.username || ''
    nextTick(() => nicknameInputRef.value?.focus())
  } else {
    bioDraft.value = userInfo.value.bio || ''
  }
}

function cancelEdit() {
  editingField.value = null
  nicknameDraft.value = ''
  bioDraft.value = ''
}

async function saveNickname() {
  const nickname = nicknameDraft.value.trim()
  // 空昵称是合法的：后端存 NULL，显示回落账号
  if (nickname === (userInfo.value.nickname || '')) {
    cancelEdit()
    return
  }

  savingField.value = 'nickname'
  try {
    const updated = await updateUserInfo({ nickname })
    userInfo.value = updated
    auth.updateUser({ nickname: updated.nickname })  // header 里的显示名跟着变
    ElMessage.success('昵称已更新')
    cancelEdit()
  } catch {
    // 错误提示由 axios 拦截器统一弹出（超长在这里返回 400）
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

// ---------- 头像：内置 SVG 头像，点一下选中即保存 ----------

function openAvatarPicker() {
  loadPresetAvatars()
  avatarPickerVisible.value = true
}

async function loadPresetAvatars() {
  // 清单是静态的，只在没拿到时请求一次；失败静默（面板会空着，不打断页面）
  if (presetAvatars.value.length) return
  try {
    presetAvatars.value = await getPresetAvatars()
  } catch {}
}

/**
 * 选中一个内置头像（url 为字符串）或恢复默认（url 为 null）。
 * 后端只接受内置清单里的地址或 null，其它值会 400。
 */
async function chooseAvatar(url) {
  if (avatarSaving.value) return
  if ((url || '') === (userInfo.value.avatar_url || '')) {
    avatarPickerVisible.value = false
    return
  }

  avatarSaving.value = true
  try {
    const updated = await updateUserInfo({ avatar_url: url })
    userInfo.value = updated
    auth.updateUser({ avatar_url: updated.avatar_url })  // 顶栏头像跟着变
    avatarPickerVisible.value = false
    ElMessage.success(url ? '头像已更新' : '已恢复默认头像')
  } catch {
    // 错误提示由 axios 拦截器统一弹出
  } finally {
    avatarSaving.value = false
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

// 纵轴刻度：0 到最高的那一档，最多 5 条；数据少时去重，避免刻度重复
const yTicks = computed(() => {
  const max = maxRatingCount.value
  return [...new Set([0, 1, 2, 3, 4].map((i) => Math.round((max * i) / 4)))]
})

function getBarHeight(count) {
  return (count / maxRatingCount.value) * 100
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
  loadDislikes()   // 与资料加载互不依赖，并行拉
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

.user-avatar {
  border: 3px solid var(--app-border-light);
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

/* 类型 / 导演各占一半、分散到整行两端（固定两列，免得宽屏下挤出空的第三列）；
   评分分布独占一整行，图撑满该行 */
.preferences {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 24px;
}

@media (max-width: 760px) {
  .preferences {
    grid-template-columns: 1fr;
  }
}

.preference-section-wide {
  grid-column: 1 / -1;
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

/* 十档并排的柱 + 纵轴 + 水平网格线；柱高 = 条数 / 最多的那一档。
   撑满「评分分布」那一行：10 列等宽、柱子占列宽 60%，左右不留大片空白 */
.distribution-chart {
  /* 父层是 flex 居中容器，没有这行 width 的话本块会收缩到内容宽度（图会缩成一小条居中） */
  width: 90%;
  display: grid;
  grid-template-columns: 30px 1fr;
  column-gap: 10px;
}

/* 图与「暂无评分数据」共用这一层：没数据时也把同样的高度占住，页面不跳 */
.chart-area {
  min-height: 300px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.chart-y {
  grid-column: 1;
  grid-row: 1;
  position: relative;
  height: 300px;
  font-size: 12px;
  color: var(--app-text-muted);
  text-align: right;
}

.chart-y span {
  position: absolute;
  right: 0;
  transform: translateY(50%);
}

.chart-plot {
  grid-column: 2;
  grid-row: 1;
  position: relative;
  height: 300px;
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  gap: 8px;
  border-left: 1px solid var(--app-border-light);
  border-bottom: 1px solid var(--app-border-light);
}

.grid-line {
  position: absolute;
  left: 0;
  right: 0;
  border-top: 1px dashed var(--app-border-light);
}

.bar-col {
  position: relative;
  height: 100%;
}

.bar {
  position: absolute;
  bottom: 0;
  left: 50%;
  transform: translateX(-50%);
  width: 60%;
  max-width: 64px;
  background: linear-gradient(180deg, #79bbff, #409eff);
  border-radius: 3px 3px 0 0;
  transition: height 0.3s ease;
  min-height: 2px;
}

.bar-count {
  position: absolute;
  left: 50%;
  transform: translateX(-50%);
  margin-bottom: 4px;
  font-size: 12px;
  line-height: 1;
  color: var(--app-text-muted);
}

.chart-x {
  grid-column: 2;
  grid-row: 2;
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: 1fr;
  gap: 8px;
  margin-top: 8px;
}

.bar-label {
  font-size: 12px;
  color: var(--app-text-muted);
  text-align: center;
  white-space: nowrap;
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

/* 与详情页的评论卡同一套观感：浅填充底 + 圆角，颜色全部走主题变量。
   左侧作者头像 + 右侧内容两栏；min-width: 0 让长正文能正常换行、不把卡片撑破 */
.comment-item {
  display: flex;
  gap: 12px;
  align-items: flex-start;
  padding: 16px;
  background: var(--app-fill);
  border-radius: 8px;
}

.comment-body {
  flex: 1;
  min-width: 0;
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

/* 偏好设置卡：类型偏好 + 不感兴趣集中管理，插在资料卡与观影统计之间 */
.preferences-card {
  margin-bottom: 20px;
}

.pref-section h4 {
  margin: 0;
  font-size: 16px;
  color: var(--app-text);
}

/* 标题在左、操作按钮/计数在右 */
.pref-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.pref-hint {
  margin: 6px 0 12px;
  font-size: 13px;
  color: var(--app-text-muted);
}

.pref-count {
  font-size: 13px;
  color: var(--app-text-muted);
}

.pref-divider {
  margin: 20px 0;
}

.genres-tags {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.genres-empty {
  font-size: 14px;
  color: var(--app-text-muted);
  font-style: italic;
}

.genre-chip { margin-right: 2px; }

/* 不感兴趣列表：片名 + 类型标签 + 取消标记一行一条 */
.dislike-list {
  display: grid;
  gap: 8px;
}

.dislike-item {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
  padding: 10px 12px;
  background: var(--app-fill);
  border-radius: 8px;
}

.dislike-title {
  font-size: 14px;
  font-weight: 500;
  color: var(--app-text);
  text-decoration: none;
}

.dislike-title:hover {
  color: #409eff;
}

.dislike-title .year {
  color: var(--app-text-muted);
  font-weight: 400;
  font-size: 13px;
}

.dislike-remove {
  margin-left: auto;
}
</style>
