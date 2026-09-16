<template>
  <div class="comment-item" :class="{ pinned, locked: editLocked }">
    <div class="comment-header">
      <div class="user-info">
        <span class="username">{{ comment.username }}</span>
        <el-tag v-if="pinned" size="small" type="warning" effect="plain">我的影评</el-tag>
        <el-tag v-if="isHidden" size="small" type="info" effect="plain">仅自己可见</el-tag>
        <!-- 展示态的星级只读（取列表接口返回的 user_rating），谁都不能直接点着改：
             要改评分得先点「编辑」，在编辑区里改、点「保存」才落库。 -->
        <el-rate
          v-if="comment.user_rating && !editing"
          :model-value="comment.user_rating"
          disabled
          size="small"
          allow-half
          class="user-rating"
        />
      </div>
    </div>

    <!-- 编辑态：正文与评分都在这里改，点「保存」才一起提交 -->
    <div v-if="editing" class="edit-area">
      <div class="edit-rate-row">
        <span class="rate-user">{{ comment.username }}</span>
        <el-rate
          :model-value="draftRating"
          @update:model-value="draftRating = $event"
          :max="5"
          allow-half
          show-score
          size="small"
        />
      </div>
      <el-input
        v-model="draft"
        type="textarea"
        :rows="4"
        placeholder="修改你的影评"
        maxlength="500"
        show-word-limit
      />
      <div class="edit-actions">
        <el-button size="small" :disabled="saving" @click="onCancel">取消</el-button>
        <el-button
          size="small"
          type="primary"
          :loading="saving"
          :disabled="!draft.trim()"
          @click="saveEdit"
        >
          保存
        </el-button>
      </div>
    </div>

    <template v-else>
      <div class="comment-content">{{ comment.content }}</div>
      <!-- 时间只在底部出现这一处：发表时间恒为 created_at，真改过正文才有 edited_at -->
      <div class="comment-footer">
        <span class="posted-at">发布于 {{ formatTime(comment.created_at) }}</span>
        <span v-if="comment.edited_at" class="edited-at">
          上次修改于 {{ formatTime(comment.edited_at) }}
        </span>
        <div v-if="isMine" class="comment-actions">
          <el-button
            text
            type="primary"
            size="small"
            :disabled="editLocked"
            :title="lockTitle"
            @click="onStartEdit"
          >
            编辑
          </el-button>
          <el-button
            text
            type="danger"
            size="small"
            :disabled="editLocked"
            :title="lockTitle"
            @click="remove"
          >
            删除
          </el-button>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useAuthStore } from '@/stores/auth'
import { apiComments } from '@/api/comments'
import { apiRatings } from '@/api/ratings'
import { formatRelative as formatTime } from '@/utils/format'

const props = defineProps({
  comment: { type: Object, required: true },
  // 置顶卡片：多一个「我的影评」标签和高亮底色，与时间线里那条区分开
  pinned: { type: Boolean, default: false },
  // 确认框要用片名说清删的是哪一部；「连评分一起删」由后端按影评定位，不需要传 id
  movieTitle: { type: String, default: '' },
  // 这张卡片在页面上的唯一标识。置顶那条与时间线里自己那条是同一份数据、同一个 id，
  // 所以用「位置 + id」区分，两处才能互斥（见父组件的 editingKey）
  editKey: { type: String, required: true },
  // 是否处于编辑态。**由父组件持有**：全页同时只许一条影评在编辑
  editing: { type: Boolean, default: false },
  // 别处（另一张卡片）正在编辑 → 本卡片的操作（编辑、删除）全部禁用，任何行为都不触发
  editLocked: { type: Boolean, default: false }
})

const emit = defineEmits(['changed', 'dirty', 'rating-removed', 'start-edit', 'cancel-edit', 'saved'])

const auth = useAuthStore()

const draft = ref('')
const draftRating = ref(0)
const saving = ref(false)

const isMine = computed(() => auth.user?.id === props.comment.user_id)

// 别处正在编辑时，本卡片上所有操作（编辑、删除）一起禁用，并给出同一句悬停说明
const lockTitle = computed(() =>
  props.editLocked ? '已有另一条影评正在编辑，请先保存或取消' : ''
)

// 进入 / 退出编辑态时回填或清空草稿。编辑态由父组件驱动（互斥），所以这里跟着 prop 走。
watch(
  () => props.editing,
  (on) => {
    if (on) {
      draft.value = props.comment.content
      draftRating.value = props.comment.user_rating ?? 0
    } else {
      draft.value = ''
      draftRating.value = 0
    }
  },
  { immediate: true }
)

// 动了正文或评分却没点「保存」——把这条影评标成「有未提交的修改」，
// 由 CommentSection 在离开详情页前提醒用户。卸载时补报一次 false，
// 免得列表刷新把卡片换掉之后，脏标记还赖在父组件里。
//
// 报脏标记时**连自己的 editKey 一起报**，不让父组件在模板里写
// `onItemDirty(myComment.id, $event)`：那种内联箭头会被编译器缓存成一个闭包，
// 等卡片因列表刷新被卸载时 `myComment` 已经变成 null，取 `.id` 直接抛 TypeError，
// 把这一次 DOM 补丁整个打断——表现就是列表卡在 loading、卡片不刷新。
watch([() => props.editing, draft, draftRating], () => {
  const changed = props.editing && draftHasChange()
  emit('dirty', props.editKey, changed)
})
onBeforeUnmount(() => emit('dirty', props.editKey, false))

// 作者此刻没有评分（例如在个人中心删掉了）时，这条影评不再公开展示，只有作者自己还能看到
const isHidden = computed(() => props.comment.visible === false)

// 草稿与库里那条比，到底改了什么。0 不是合法评分（后端只收 0.5 ~ 5.0），不当成「改了」
function draftHasChange() {
  const rating = Number(draftRating.value)
  const ratingChanged = rating > 0 && rating !== (props.comment.user_rating ?? 0)
  const contentChanged = draft.value.trim() !== props.comment.content
  return { ratingChanged, contentChanged }
}

function onStartEdit() {
  if (props.editLocked) return
  emit('start-edit', props.editKey)
}

function onCancel() {
  emit('cancel-edit')
}

async function saveEdit() {
  const content = draft.value.trim()
  if (!content) {
    ElMessage.warning('影评内容不能为空')
    return
  }

  const { ratingChanged, contentChanged } = draftHasChange()
  if (!ratingChanged && !contentChanged) {
    // 点开却什么都没改：只收起编辑区，不去白刷一次 edited_at
    emit('cancel-edit')
    return
  }

  saving.value = true
  try {
    // 评分与影评都是「点了保存才落库」：先落评分、再落影评（顺序与发表时一致）
    if (ratingChanged) {
      await apiRatings.submit(props.comment.movie_id, Number(draftRating.value))
    }
    if (contentChanged) {
      await apiComments.update(props.comment.id, content)
    }
    ElMessage.success(
      ratingChanged && contentChanged
        ? '评分与影评已更新'
        : ratingChanged
          ? '评分已更新'
          : '影评已更新'
    )
    emit('saved', props.editKey, ratingChanged)
  } catch {} finally {
    saving.value = false
  }
}

// 作者此刻有没有给这部电影打分（列表接口把评分接在 user_rating 上，没评过是 null）。
// 有评分时确认框要多问一句「影评草稿要不要留着」，所以这个值决定走哪套问法。
const hasRating = computed(() => props.comment.user_rating > 0)

// 删除。评分与影评是一体发表的（缺一不可，见 CommentSection 的 canSubmit），
// 所以这里先问一句「影评草稿要不要保留」：
// - 保留：只删评分（``DELETE /api/ratings/<movie_id>``），影评留在原地，
//   但从此只对作者自己可见；重新评分（>= 0.5）即自动重新公开。
// - 不保留：评分和影评一起删（``DELETE /api/comments/<id>?with_rating=1``，
//   两行在同一次 commit 里删掉），详情页置顶那块随即变回评论输入框。
// 本来就没有评分时（例如上次选了「保留」）没有评分可删，直接问要不要删影评。
async function remove() {
  // 别处正在编辑时本卡片一律不响应，避免「一边改、一边删」把数据改乱
  if (props.editLocked) return

  // 先把两个事实定下来，免得中途 props 变化导致话说一套、做一套
  const name = props.movieTitle ? `《${props.movieTitle}》` : '这部电影'
  const movieId = props.comment.movie_id
  const commentId = props.comment.id

  if (!hasRating.value) {
    try {
      await ElMessageBox.confirm('确定要删除这条影评吗？', '提示', {
        confirmButtonText: '确定',
        cancelButtonText: '取消',
        type: 'warning'
      })
    } catch {
      return
    }

    try {
      await apiComments.delete(commentId)
      ElMessage.success('影评已删除')
      // 没有评分要撤，只要重拉列表：置顶那块由「有影评」变回「输入框」
      emit('changed')
    } catch {}
    return
  }

  let keepDraft
  try {
    await ElMessageBox.confirm(
      `确定删除对${name}的评分吗？这条影评草稿要保留吗？\n` +
        `· 保留：影评留在原地，但只对你自己可见（重新评分即自动公开）；\n` +
        `· 不保留：评分和影评一起删除。`,
      '提示',
      {
        confirmButtonText: '保留影评',
        cancelButtonText: '不保留',
        distinguishCancelAndClose: true,
        type: 'warning'
      }
    )
    keepDraft = true
  } catch (action) {
    if (action !== 'cancel') return // 点 X / Esc：什么都没删
    keepDraft = false
  }

  try {
    if (keepDraft) {
      await apiRatings.remove(movieId)
      ElMessage.success('评分已删除，这条影评现在仅你自己可见')
    } else {
      await apiComments.delete(commentId, true)
      ElMessage.success('影评与评分已删除')
    }
    // 两种情况评分都没了 → 详情页的星星与统计要归零，交给自己人（CommentSection）
    // 重拉列表和统计；「不保留」时列表里那条也没了，置顶随即退回输入框
    emit('rating-removed')
  } catch {}
}
</script>

<style scoped>
.comment-item {
  padding: 16px;
  background: var(--app-fill-light);
  border-radius: 8px;
}

/* 置顶的「我的影评」用暖色底 + 描边区分（颜色走主题变量，夜间是深琥珀） */
.comment-item.pinned {
  background: var(--app-pin-bg);
  border: 1px solid var(--app-pin-border);
}

/* 别处正在编辑：整张卡片压暗，表示这一条现在动不了 */
.comment-item.locked {
  opacity: 0.55;
}

.comment-header {
  display: flex;
  align-items: center;
  margin-bottom: 8px;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.username {
  font-weight: 600;
  color: var(--app-text);
}

.user-rating {
  font-size: 14px;
}

.comment-content {
  color: var(--app-text-secondary);
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.comment-footer {
  margin-top: 8px;
  display: flex;
  align-items: center;
  gap: 12px;
}

/* 「发布于」与「上次修改于」并排在底部左侧，操作按钮靠右 */
.posted-at,
.edited-at {
  font-size: 12px;
  color: var(--app-text-muted);
}

.comment-actions {
  margin-left: auto;
}

/* 编辑区：用户名 + 可点的星星在上，正文框在下 */
.edit-rate-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.rate-user {
  font-weight: 600;
  color: var(--app-text);
}

.edit-actions {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
</style>
