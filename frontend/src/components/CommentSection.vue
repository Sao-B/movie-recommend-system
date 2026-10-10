<template>
  <div class="comment-section">
    <!-- 评分 + 影评：两者缺一不可，同一个「发表」按钮提交，需登录 -->
    <template v-if="auth.isLoggedIn">
      <div class="comment-form">
        <h4>影评 ({{ total }})</h4>

        <!-- 还没发表：显示名（昵称优先、回落账号）+ 星星在评论框上面（显示名在星星左边），评分与正文缺一不可 -->
        <template v-if="!hasPublished">
          <div class="rate-row">
            <!-- 录入区也亮出自己的头像：与影评卡片、顶栏同一套（有图用图，没图回落首字符） -->
            <UserAvatar :src="auth.user?.avatar_url" :name="auth.displayName" :size="44" />
            <!-- 名字在上（缩小一号）、星星紧贴其下，两行成一列立在头像旁；不再显示分值数字 -->
            <div class="rate-line">
              <span class="rate-user">{{ auth.displayName }}</span>
              <!-- 星星不再带「删除」：评分的撤销与影评绑定，走影评卡片上的「删除」 -->
              <el-rate
                :model-value="myRating"
                @update:model-value="$emit('update:my-rating', $event)"
                :max="5"
                allow-half
              />
            </div>
          </div>

          <el-input
            v-model="newComment"
            type="textarea"
            :rows="4"
            placeholder="写下你的观影感受（评分和影评需要同时填写才能发表）"
            maxlength="500"
            show-word-limit
          />
          <div class="comment-form-actions">
            <el-button
              type="primary"
              :loading="submitting"
              :disabled="!canSubmit"
              @click="submitAll"
            >
              发表
            </el-button>
          </div>

          <!-- 缺一半时明确说差什么，别让用户猜按钮为什么点不动 -->
          <el-alert
            v-if="submitHint"
            type="info"
            :closable="false"
            show-icon
            class="hint-alert"
          >
            {{ submitHint }}
          </el-alert>
        </template>

        <!-- 已发表：输入框的位置换成用户自己的影评（置顶展示），没有「修改」按钮。
             改正文或改评分都只能走卡片上的「编辑」——进编辑态后连星星一起改，
             点「保存」才落库；全页同时只许一条影评处于编辑态。 -->
        <template v-else>
          <CommentItem
            :comment="myComment"
            :movie-title="movieTitle"
            :edit-key="pinnedKey"
            :editing="editingKey === pinnedKey"
            :edit-locked="!!editingKey && editingKey !== pinnedKey"
            pinned
            @start-edit="onStartEdit"
            @cancel-edit="onCancelEdit"
            @saved="onSaved"
            @changed="reloadComments"
            @rating-removed="onRatingRemoved"
            @dirty="onItemDirty"
          />
          <!-- 置顶那条与下方时间线的分界 -->
          <el-divider class="pin-divider" />
        </template>
      </div>
    </template>

    <el-alert v-else type="info" :closable="false" style="margin-bottom: 16px">
      <router-link to="/login">登录</router-link> 后可评分和发表影评
    </el-alert>

    <div v-loading="loading" class="comment-body">
      <!-- 影评时间线（自己的那条也会出现在这里，删除/编辑与置顶那条同源，但编辑互斥） -->
      <el-empty
        v-if="!loading && comments.length === 0"
        description="暂无影评，快来发表第一条吧"
      />
      <div v-else class="comments">
        <CommentItem
          v-for="comment in comments"
          :key="comment.id"
          :comment="comment"
          :movie-title="movieTitle"
          :edit-key="'list:' + comment.id"
          :editing="editingKey === 'list:' + comment.id"
          :edit-locked="!!editingKey && editingKey !== 'list:' + comment.id"
          @start-edit="onStartEdit"
          @cancel-edit="onCancelEdit"
          @saved="onSaved"
          @changed="reloadComments"
          @rating-removed="onRatingRemoved"
          @dirty="onItemDirty"
        />
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
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { onBeforeRouteLeave } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { apiComments } from '@/api/comments'
import { apiRatings } from '@/api/ratings'
import CommentItem from '@/components/CommentItem.vue'
import UserAvatar from '@/components/UserAvatar.vue'

const props = defineProps({
  movieId: { type: Number, required: true },
  // 只用于「删除」确认框里的片名，让提示能说清删的是哪一部
  movieTitle: { type: String, default: '' },
  // 评分控件当前的值（表单态，用户可能已经改动但还没提交）
  myRating: { type: Number, default: 0 },
  // 服务端此刻记录的评分（0 表示没评过）。用来判断评分有没有改动：没改动就不写库，
  // 免得「只想改一句影评」把 rated_at 白刷一遍。
  savedRating: { type: Number, default: 0 }
})

const emit = defineEmits(['update:my-rating', 'rating-submitted'])

const auth = useAuthStore()

const comments = ref([])
const myComment = ref(null)
const total = ref(0)
const loading = ref(false)
const currentPage = ref(1)
const perPage = 10

const newComment = ref('')
const submitting = ref(false)

// 已经发表过（库里有一条自己的影评）。发表后详情页就不再显示那个输入框了——
// 那个位置被自己的影评顶上去（置顶展示），没有「修改」按钮：改正文与改评分都走
// 卡片上的「编辑」，进编辑态后连星星一起改，点「保存」才落库。
const hasPublished = computed(() => !!myComment.value)

// 评分和影评**必须同时存在**才能发表，缺一不可：
// 只有评分没写影评、或只写影评没打分，按钮都点不动（并在下面说明还差什么）。
const canSubmit = computed(
  () => props.myRating > 0 && !!newComment.value.trim()
)

// 只差一半时说清差哪一半；两边都齐了、或两边都空着（还没开始写）都不吭声
const submitHint = computed(() => {
  const hasText = !!newComment.value.trim()
  const hasRate = props.myRating > 0
  if (hasRate === hasText) return ''
  return hasRate
    ? '还差一句影评：评分和影评需要同时填写才能发表。'
    : '还差一个评分：评分和影评需要同时填写才能发表。'
})

// ---------- 编辑互斥 ----------
//
// 同一条影评在页面上会出现两次（置顶那块 + 时间线里自己那条），而时间线上还有别人的
// 影评。用户要求「只能同时一条评论在修改，时间线评论不可与置顶评论同时修改」，所以
// 编辑态由**父组件**用一个 editingKey 统一持有，卡片自己不存。key 形如
// `pinned:<id>` / `list:<id>`——同一个 id 也因为位置不同而互不冲突，谁先点谁进编辑态，
// 其余卡片的「编辑」按钮一律禁用（editLocked）。
const editingKey = ref('')
// 置顶那条自己的 key。放在 computed 里算，是为了不在模板里写任何读 myComment 的闭包
// （那种内联箭头会被编译器缓存，卡片卸载时 myComment 已变 null，取 .id 会抛错——见
// CommentItem 里 @dirty 那条说明）。
const pinnedKey = computed(() => (myComment.value ? 'pinned:' + myComment.value.id : ''))

function onStartEdit(key) {
  editingKey.value = key
}

function onCancelEdit() {
  editingKey.value = ''
}

// 保存成功：先收起编辑态，再刷列表；评分也改了就顺带让详情页重拉统计
async function onSaved(_key, ratingChanged) {
  editingKey.value = ''
  await reloadComments()
  if (ratingChanged) emit('rating-submitted')
}

// 影评卡片上的「删除」（置顶那条和时间线里自己那条走的是同一个组件）：
// 确认框里问「影评草稿要不要保留」，两边的结果都会让评分消失，所以都要重拉列表 +
// 重拉详情页统计（星星归零）。顺序与提交保持一致——先刷列表，再让详情页重拉统计。
async function onRatingRemoved() {
  editingKey.value = ''
  await reloadComments()
  emit('rating-submitted')
}

// 开着编辑区、且改过正文或评分的卡片 key（改了却不点「保存」也算未提交的修改）。
// key 由卡片自己（CommentItem）连同标记一起报上来，不在模板里写
// `onItemDirty(myComment.id, $event)`——那种内联闭包在卡片卸载时已经取不到
// myComment 了（见 CommentItem 里的说明）。
const dirtyKeys = ref(new Set())
function onItemDirty(key, dirty) {
  const next = new Set(dirtyKeys.value)
  if (dirty) next.add(key)
  else next.delete(key)
  dirtyKeys.value = next
}

// 有待提交的改动：表单展开着（即还没发表），且改了评分或写了正文；另有影评正开着编辑区。
// 置顶展示态什么都不改得动，所以那一态天然没有待提交的修改。
const hasPendingChange = computed(() => {
  if (dirtyKeys.value.size > 0) return true
  if (hasPublished.value) return false
  if (props.myRating !== props.savedRating) return true
  const content = newComment.value.trim()
  if (!content) return false
  return content !== (myComment.value?.content ?? '')
})

async function fetchComments() {
  loading.value = true
  try {
    const data = await apiComments.list(props.movieId, {
      page: currentPage.value,
      per_page: perPage
    })
    comments.value = data.comments ?? []
    myComment.value = data.my_comment ?? null
    total.value = data.total ?? 0
  } catch {} finally {
    loading.value = false
  }
}

async function reloadComments() {
  // 写完影评回到第一页，免得停在超出范围的页码上
  currentPage.value = 1
  await fetchComments()
}

// 评分与影评合成一个动作，缺一不可：先落评分，再落影评（后端同样要求「有评分的影评」，
// 顺序不能反）。表单只会在「还没发表」时展开，所以这里只有新建（POST）一种写法。
// 返回「是否真的提交成功」，供离开页面前的提醒决定放不放行。
async function submitAll() {
  const content = newComment.value.trim()
  // 按钮在「缺一」时是禁用的，这里是兜底（离开提醒也走同一条规则）
  if (props.myRating <= 0 || !content) {
    ElMessage.warning('评分和影评需要同时填写才能发表')
    return false
  }

  submitting.value = true
  try {
    // 评分没变就不写库，免得白刷一次 rated_at；0 不是合法评分（后端只收 0.5 ~ 5.0），
    // 上面已经挡掉了。
    if (props.myRating !== props.savedRating) {
      await apiRatings.submit(props.movieId, props.myRating)
    }
    await apiComments.create(props.movieId, content)

    ElMessage.success('评分与影评已发表')

    // 刷新列表：评论条上的星级用的是列表接口返回的值，所以星星是「点发表之后」
    // 才更新。随后再让详情页重拉评分统计。
    await reloadComments()
    newComment.value = ''
    emit('rating-submitted')
    return true
  } catch {
    return false
  } finally {
    submitting.value = false
  }
}

// 离开详情页前提醒：改了评分或影评却没点「发表」/「保存」，别让用户以为已经存下了。
// 三个出口：确定＝提交后离开；取消＝不提交直接离开；点 X / Esc＝留在本页。
onBeforeRouteLeave(async () => {
  if (!hasPendingChange.value) return true

  try {
    await ElMessageBox.confirm(
      '你对这部电影的评分和影评还没提交，要提交后再离开吗？',
      '有未提交的修改',
      {
        confirmButtonText: '提交并离开',
        cancelButtonText: '不提交，直接离开',
        distinguishCancelAndClose: true,
        type: 'warning'
      }
    )
  } catch (action) {
    return action === 'cancel'
  }

  return await submitAll()
})

// 刷新 / 关标签页也拦一下（浏览器不允许自定义文案，只会弹它默认那句）
function handleBeforeUnload(event) {
  if (!hasPendingChange.value) return
  event.preventDefault()
  event.returnValue = ''
}

onMounted(() => {
  window.addEventListener('beforeunload', handleBeforeUnload)
  fetchComments()
})

onUnmounted(() => {
  window.removeEventListener('beforeunload', handleBeforeUnload)
})
</script>

<style scoped>
.comment-section h4 {
  font-size: 16px;
  margin: 0 0 12px;
  color: var(--app-text);
}

.comment-form {
  margin-bottom: 24px;
}

/* 星星那一行：头像在左，名字（缩小一号）在上、星星紧贴其下，两行成一列与头像垂直居中 */
.rate-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

/* 名字在上、星星在下；el-rate 自带 32px 固定盒高（图标只有 18px），放开成 auto
   免得两行之间被空盒撑出一截 */
.rate-line {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 2px;
}

.rate-line :deep(.el-rate) {
  height: auto;
}

.rate-user {
  font-size: 14px;
  font-weight: 600;
  color: var(--app-text);
}

/* 「发表」按钮右对齐 */
.comment-form-actions {
  margin-top: 12px;
  display: flex;
  justify-content: flex-end;
}

/* 置顶那条与下方时间线之间的分界 */
.pin-divider {
  margin: 14px 0 4px;
}

.hint-alert {
  margin-top: 12px;
}

.comment-body {
  min-height: 200px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.comments {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

.comment-pagination {
  margin-top: 8px;
  display: flex;
  justify-content: center;
}
</style>
