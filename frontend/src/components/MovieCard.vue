<template>
  <el-card class="movie-card" shadow="hover" @click="$router.push('/movies/' + movie.movie_id)">
    <div class="poster-container">
      <img
        v-if="movie.poster_url && !imageError"
        :src="movie.poster_url"
        :alt="movie.title"
        class="poster-img"
        @error="imageError = true"
      />
      <div v-else class="poster-placeholder">
        <el-icon><Film /></el-icon>
      </div>
    </div>
    <div class="content">
      <!-- 更多操作（横向三个点）：仅推荐页开启（dismissible）。
           "不感兴趣"是两段式：点一次只把菜单项点红成"确定吗？"（菜单不关），再点一次才 emit dislike。
           @click.stop 拦住卡片本身的跳转 -->
      <div v-if="dismissible" class="card-more" @click.stop>
        <el-dropdown
          ref="moreDropdown"
          trigger="click"
          placement="bottom-end"
          popper-class="movie-dislike-menu"
          :hide-on-click="false"
          @command="onMoreCommand"
          @visible-change="onMoreVisible"
        >
          <button class="more-btn" type="button" title="更多操作">
            <el-icon><MoreFilled /></el-icon>
          </button>
          <template #dropdown>
            <el-dropdown-menu>
              <el-dropdown-item
                command="dislike"
                class="dislike-item"
                :class="{ 'is-armed': dislikeArmed }"
              >
                <el-icon><CircleClose /></el-icon>
                {{ dislikeArmed ? '确定吗？' : '不感兴趣' }}
              </el-dropdown-item>
            </el-dropdown-menu>
          </template>
        </el-dropdown>
      </div>
      <div class="movie-year" v-if="movie.year">{{ movie.year }}</div>
      <h4 class="movie-title">{{ movie.title }}</h4>
      <div class="movie-genres">
        <el-tag
          v-for="g in movie.genres?.slice(0, 2)"
          :key="g"
          size="small"
          type="info"
          class="genre-tag"
        >{{ g }}</el-tag>
      </div>
      <div class="rating-info" v-if="movie.avg_rating">
        <el-rate
          :model-value="movie.avg_rating"
          disabled
          show-score
          :score-template="`${movie.avg_rating}`"
          size="small"
        />
      </div>
      <div v-if="predictedRating" class="predicted">
        <span class="recommend-label">推荐指数</span>
        <el-rate
          :model-value="normalizedRating"
          disabled
          allow-half
          size="small"
          class="recommend-stars"
        />
      </div>
      <div v-else-if="matchPercent" class="predicted">
        <span class="recommend-label">匹配度</span>
        <span class="match-percent">{{ matchPercent }}%</span>
      </div>
    </div>
  </el-card>
</template>

<script setup>
import { ref, computed } from 'vue'
import { Film, MoreFilled, CircleClose } from '@element-plus/icons-vue'

const props = defineProps({
  movie:          { type: Object, required: true },
  predictedRating:{ type: [Number, String], default: null },
  // ItemCF 等排序模型的匹配度（0-100 整数，相对本次推荐列表的最高分归一）。
  // 与 predictedRating 互斥：有 predictedRating 显示星级，否则显示匹配度。
  matchPercent:   { type: [Number, String], default: null },
  // 推荐页开启"不感兴趣"按钮（标记后该电影不再进入推荐）
  dismissible:    { type: Boolean, default: false },
})

const emit = defineEmits(['dislike'])

const imageError = ref(false)

// "不感兴趣"的两段式确认：false = 菜单项显示"不感兴趣"；true = 已点过一次，显示红色"确定吗？"
const dislikeArmed = ref(false)
const moreDropdown = ref(null)

// 卡片"⋯"菜单：目前只有"不感兴趣"一项；再扩展时在 commands 里加分支即可
function onMoreCommand(command) {
  if (command !== 'dislike') return
  if (!dislikeArmed.value) {
    dislikeArmed.value = true // 第一下只把菜单项点红，不动数据
    return
  }
  dislikeArmed.value = false
  emit('dislike') // 第二下才交回推荐页入库
  // 顺手收起菜单；即便这一步出岔子（无头环境里 popper 实例可能不全），标记动作也已经发出去了
  try { moreDropdown.value?.handleClose() } catch { /* 卡片随即被移除，菜单随之消失 */ }
}

// 菜单收起（点了别处 / 标记完成）→ 复位，避免红色确认态一直挂着
function onMoreVisible(visible) {
  if (!visible) dislikeArmed.value = false
}

// 将预测评分归一化到 0-5 范围（用于显示星星）
const normalizedRating = computed(() => {
  if (!props.predictedRating) return 0
  const rating = typeof props.predictedRating === 'string'
    ? parseFloat(props.predictedRating)
    : props.predictedRating
  // 截断到 0.5-5.0 范围
  return Math.max(0.5, Math.min(5.0, rating))
})

</script>

<style scoped>
.movie-card {
  cursor: pointer;
  height: 100%;
  display: flex;
  flex-direction: column;
  transition: transform .15s;
  padding: 0;
}

.movie-card :deep(.el-card__body) {
  padding: 0;
  height: 100%;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.movie-card:hover { transform: translateY(-3px); }

.poster-container {
  width: 100%;
  height: 240px;
  background: var(--app-fill-light);
  display: flex;
  align-items: center;
  justify-content: center;
  overflow: hidden;
  flex-shrink: 0;
  position: relative;
}

.poster-img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

/* 响应式适配 */
@media (max-width: 768px) {
  .poster-container {
    height: 180px;
  }
}

@media (min-width: 1200px) {
  .poster-container {
    height: 280px;
  }
}

.poster-placeholder {
  font-size: 48px;
  color: var(--app-text-muted);
  display: flex;
  align-items: center;
  justify-content: center;
}

/* 卡片"⋯"菜单按钮：内容区右上角（与年份行齐平），常驻显示、悬停加深。
   菜单面板 teleport 到 body，点菜单项不会触发卡片跳转 */
.card-more {
  position: absolute;
  top: 8px;
  right: 10px;
  z-index: 2;
  line-height: 1;
}

/* 实心三点、无底色，直接融进卡片；悬停时染上主题强调色 */
.more-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  padding: 0;
  border: none;
  border-radius: 50%;
  cursor: pointer;
  font-size: 16px;
  color: var(--app-text-secondary);
  background: transparent;
  transition: color 0.15s, transform 0.15s;
}
.more-btn:hover {
  color: var(--el-color-primary);
  transform: scale(1.1);
}

/* "不感兴趣"菜单项的红色见文件末尾那段**非 scoped** 样式：
   菜单面板被 teleport 到 body，菜单项又是 Element Plus 内部子组件渲染的 li，
   这里的 scoped 属性追不进去（data-v 只落在直接子组件的根节点上） */

.content {
  position: relative;
  flex: 1;
  display: flex;
  flex-direction: column;
  padding: 12px;
  min-height: 0;
}

.movie-year  { font-size: 12px; color: var(--app-text-muted); margin-bottom: 4px; flex-shrink: 0; }
.movie-title {
  font-size: 14px;
  font-weight: 500;
  margin: 0 0 8px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
  flex-shrink: 0;
}
.movie-genres {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 8px;
  flex-shrink: 0;
}
.genre-tag   { font-size: 11px; }
.rating-info {
  font-size: 12px;
  margin-top: auto;
  flex-shrink: 0;
}
.rating-info :deep(.el-rate) {
  height: 20px;
}
.predicted {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
  flex-shrink: 0;
}

.recommend-label {
  font-size: 12px;
  color: #e6a23c;
  font-weight: 500;
}

.match-percent {
  font-size: 12px;
  color: var(--app-text-primary);
  font-weight: 500;
}

.recommend-stars :deep(.el-rate) {
  height: 18px;
}

.recommend-stars :deep(.el-rate__icon) {
  font-size: 14px;
}
</style>

<!-- 上面的 scoped 样式够不到下拉菜单：面板被 Element Plus teleport 到 body，菜单项 li 也是
     EP 内部子组件渲染的，data-v 属性只落在直接子组件的根节点上。所以这段必须是全局样式，
     用 el-dropdown 的 popper-class（movie-dislike-menu）把作用范围锁在推荐卡片的这个菜单里。 -->
<style>
.movie-dislike-menu .el-dropdown-menu__item {
  color: var(--el-color-danger);
}
.movie-dislike-menu .el-dropdown-menu__item:not(.is-disabled):hover {
  color: var(--el-color-danger);
  background-color: rgba(245, 108, 108, 0.16);
}
/* 菜单项是 tabindex="-1" 的 li，点完第一下（"确定吗？"态）Chrome 会把它**留在 :focus 上**，
   而 Element Plus 把 :focus 当 :hover 处理。若直接写 :focus{transparent}，会和 :hover 同优先级、
   且排在后面，导致"武装态下即便悬停也变透明"——正是这次回退的根因。
   正确做法：只在「聚焦但没悬停」时压透明；只要鼠标还悬停着（含点完第一下那一下），
   就保留淡红底色，既不残留也不误伤悬停态。 */
.movie-dislike-menu .el-dropdown-menu__item:not(.is-disabled):focus:not(:hover) {
  color: var(--el-color-danger);
  background-color: transparent;
}
.movie-dislike-menu .el-dropdown-menu__item.is-armed {
  font-weight: 700;
  color: var(--el-color-danger);
}
</style>
