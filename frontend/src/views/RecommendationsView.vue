<template>
  <div class="page">
    <h2 class="page-title">🎯 我的个性化推荐</h2>
    <p class="page-sub">{{ subtitles[currentModel] }}</p>
    <!-- 模型写在网址里（/recommendations/als|itemcf|fusion）：切换按钮 = 换 URL，刷新、开新标签、分享链接都停在同一路推荐上 -->
    <el-radio-group :model-value="currentModel" class="model-switch" @change="switchModel">
      <el-radio-button value="als">ALS 协同过滤</el-radio-button>
      <el-radio-button value="itemcf">ItemCF 相似度</el-radio-button>
      <el-radio-button value="fusion">混合融合</el-radio-button>
    </el-radio-group>
    <div v-loading="loading">
      <el-empty v-if="!loading && recs.length === 0"
        :description="emptyTexts[currentModel]" />
      <el-row v-else :gutter="16">
        <el-col v-for="r in recs" :key="currentModel + '-' + (r.movie && r.movie.movie_id)"
          :xs="12" :sm="8" :md="6" :lg="4">
          <MovieCard
            :movie="r.movie"
            :predicted-rating="r.predicted_rating && r.predicted_rating.toFixed(2)"
            :match-percent="matchPercent(r)"
            dismissible
            @dislike="onDislike(r)"
          />
        </el-col>
      </el-row>
    </div>
  </div>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import MovieCard from '@/components/MovieCard.vue'
import { apiRecommendations } from '@/api/recommendations'
import { apiDislikes } from '@/api/dislikes'

const MODELS = ['als', 'itemcf', 'fusion']

const route  = useRoute()
const router = useRouter()

const recs    = ref([])
const loading = ref(true)

// 当前模型以网址为唯一来源；网址里不是这三个值时回落 als（后端也按 als 处理）
const currentModel = computed(() => (MODELS.includes(route.params.model) ? route.params.model : 'als'))

const subtitles = {
  als:    '基于 ALS 协同过滤，为你精选的 Top-20 电影',
  itemcf: '基于 ItemCF 电影相似度图（k=100 全量模型），与你好评电影最相似的作品',
  fusion: '混合融合：ItemCF 共现 + 门控 ALS 隐因子 + 你的类型偏好，三路信号归一融合',
}
const emptyTexts = {
  als:    '暂无推荐数据，请先评几部电影，等待下次离线训练后即可获得推荐',
  itemcf: '暂无 ItemCF 推荐：请先给至少一部电影打 4 分及以上好评',
  fusion: '暂无融合推荐：请先给至少一部电影打 4 分及以上好评',
}

// 点按钮 = 跳转到该模型的网址，列表由下面的 watch 重新拉取
function switchModel(model) {
  if (model === currentModel.value) return
  router.push({ name: 'recommendation-model', params: { model } })
}

// ItemCF 的 score 是排序分（无 5 星上限），相对列表最高分归一成 0-100 的匹配度
function matchPercent(item) {
  if (item.score == null || !recs.value.length) return null
  const max = Math.max(...recs.value.map((x) => x.score ?? 0))
  if (!(max > 0)) return null
  return Math.max(1, Math.round((item.score / max) * 100))
}

async function load() {
  loading.value = true
  try {
    const data = await apiRecommendations.get(currentModel.value)
    recs.value = data.data || []
  } catch {} finally {
    loading.value = false
  }
}

// 不感兴趣：确认动作已经在卡片菜单里完成（点一下变红"确定吗？"、再点一下才走到这里），
// 这里不再弹确认框，直接入库（所有推荐链路硬排除）+ 立即从当前列表移除
async function onDislike(item) {
  const movieId = item.movie && item.movie.movie_id
  if (!movieId) return
  try {
    await apiDislikes.add(movieId)
    recs.value = recs.value.filter((x) => x.movie?.movie_id !== movieId)
    ElMessage.success('已标记不感兴趣，不再推荐（可在个人主页「偏好设置」里取消）')
  } catch { /* 失败提示由 axios 拦截器统一弹 */ }
}

// 首次进入和每次切换网址都重新拉取
watch(currentModel, load, { immediate: true })
</script>

<style scoped>
.page        { max-width: 1200px; margin: 0 auto; padding: 20px; }
.page-title  { margin: 0 0 4px; }
.page-sub    { color: var(--app-text-muted); margin: 0 0 12px; font-size: 14px; }
.model-switch{ margin-bottom: 16px; }
</style>
