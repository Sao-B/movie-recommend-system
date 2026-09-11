<template>
  <div class="comment-section">
    <h3>评分与影评</h3>

    <!-- 评分区域（需登录） -->
    <template v-if="auth.isLoggedIn">
      <div class="rating-section">
        <h4>我的评分</h4>
        <div class="rate-area">
          <el-rate
            :model-value="myRating"
            @update:model-value="$emit('update:my-rating', $event)"
            :max="5"
            allow-half
            show-score
            :texts="['很差','较差','还行','推荐','强烈推荐']"
            show-text
          />
          <el-button
            type="primary"
            :loading="ratingSubmitting"
            :disabled="!myRating"
            @click="submitRating"
            style="margin-left: 16px"
          >
            提交评分
          </el-button>
        </div>
      </div>

      <el-divider />

      <!-- 发表评论 -->
      <div class="comment-form">
        <h4>影评 ({{ total }})</h4>
        <el-input
          v-model="newComment"
          type="textarea"
          :rows="4"
          placeholder="写下你的观影感受..."
          maxlength="500"
          show-word-limit
        />
        <el-button
          type="primary"
          :loading="commentSubmitting"
          :disabled="!newComment.trim()"
          @click="submitComment"
          style="margin-top: 12px"
        >
          发表评论
        </el-button>
      </div>
    </template>

    <el-alert v-else type="info" :closable="false" style="margin-bottom: 16px">
      <router-link to="/login">登录</router-link> 后可评分和发表评论
    </el-alert>

    <!-- 评论列表 -->
    <div v-loading="loading" class="comment-list">
      <el-empty v-if="!loading && comments.length === 0" description="暂无评论，快来发表第一条吧" />
      <div v-else class="comments">
        <div v-for="comment in comments" :key="comment.id" class="comment-item">
          <div class="comment-header">
            <div class="user-info">
              <span class="username">{{ comment.username }}</span>
              <el-rate
                v-if="comment.user_rating"
                :model-value="comment.user_rating"
                disabled
                size="small"
                allow-half
                class="user-rating"
              />
            </div>
            <span class="time">{{ formatTime(comment.created_at) }}</span>
          </div>
          <div class="comment-content">{{ comment.content }}</div>
          <div v-if="auth.user?.id === comment.user_id" class="comment-actions">
            <el-button
              text
              type="danger"
              size="small"
              @click="deleteComment(comment.id)"
            >
              删除
            </el-button>
          </div>
        </div>
      </div>

      <!-- 分页 -->
      <div v-if="total > perPage" class="comment-pagination">
        <el-pagination
          v-model:current-page="currentPage"
          :page-size="perPage"
          :total="total"
          layout="prev, pager, next"
          small
          background
          @current-change="fetchComments"
        />
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { apiComments } from '@/api/comments'
import { apiRatings } from '@/api/ratings'

const props = defineProps({
  movieId: { type: Number, required: true },
  myRating: { type: Number, default: 0 }
})

const emit = defineEmits(['update:my-rating', 'rating-submitted'])

const auth = useAuthStore()

const comments = ref([])
const total = ref(0)
const loading = ref(false)
const currentPage = ref(1)
const perPage = 10

const newComment = ref('')
const commentSubmitting = ref(false)
const ratingSubmitting = ref(false)

async function fetchComments() {
  loading.value = true
  try {
    const data = await apiComments.list(props.movieId, {
      page: currentPage.value,
      per_page: perPage
    })
    comments.value = data.comments
    total.value = data.total
  } catch (msg) {
    ElMessage.error(typeof msg === 'string' ? msg : '加载评论失败')
  } finally {
    loading.value = false
  }
}

async function submitRating() {
  if (!props.myRating) return

  ratingSubmitting.value = true
  try {
    await apiRatings.submit(props.movieId, props.myRating)
    ElMessage.success('评分成功！感谢你的反馈')
    emit('rating-submitted')
  } catch (msg) {
    ElMessage.error(typeof msg === 'string' ? msg : '评分失败')
  } finally {
    ratingSubmitting.value = false
  }
}

async function submitComment() {
  if (!newComment.value.trim()) return

  commentSubmitting.value = true
  try {
    await apiComments.create(props.movieId, newComment.value.trim())
    ElMessage.success('评论发表成功')
    newComment.value = ''
    currentPage.value = 1
    await fetchComments()
  } catch (msg) {
    ElMessage.error(typeof msg === 'string' ? msg : '发表失败')
  } finally {
    commentSubmitting.value = false
  }
}

async function deleteComment(commentId) {
  try {
    await ElMessageBox.confirm('确定要删除这条评论吗？', '提示', {
      confirmButtonText: '确定',
      cancelButtonText: '取消',
      type: 'warning'
    })

    await apiComments.delete(commentId)
    ElMessage.success('删除成功')
    await fetchComments()
  } catch (error) {
    if (error !== 'cancel') {
      ElMessage.error('删除失败')
    }
  }
}

function formatTime(isoString) {
  if (!isoString) return ''
  const date = new Date(isoString)
  const now = new Date()
  const diff = now - date

  // 1分钟内
  if (diff < 60000) return '刚刚'
  // 1小时内
  if (diff < 3600000) return `${Math.floor(diff / 60000)} 分钟前`
  // 1天内
  if (diff < 86400000) return `${Math.floor(diff / 3600000)} 小时前`
  // 7天内
  if (diff < 604800000) return `${Math.floor(diff / 86400000)} 天前`

  // 否则显示日期
  return date.toLocaleDateString('zh-CN')
}

onMounted(() => {
  fetchComments()
})
</script>

<style scoped>
.comment-section h3 {
  font-size: 18px;
  margin: 0 0 16px;
}

.comment-section h4 {
  font-size: 16px;
  margin: 0 0 12px;
  color: #303133;
}

.rating-section {
  margin-bottom: 20px;
}

.rate-area {
  display: flex;
  align-items: center;
}

.comment-form {
  margin-bottom: 24px;
}

.comment-list {
  min-height: 200px;
}

.comments {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.comment-item {
  padding: 16px;
  background: #f9f9f9;
  border-radius: 8px;
}

.comment-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 12px;
}

.username {
  font-weight: 600;
  color: #303133;
}

.user-rating {
  font-size: 14px;
}

.time {
  font-size: 12px;
  color: #909399;
}

.comment-content {
  color: #606266;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.comment-actions {
  margin-top: 8px;
  text-align: right;
}

.comment-pagination {
  margin-top: 24px;
  display: flex;
  justify-content: center;
}
</style>
