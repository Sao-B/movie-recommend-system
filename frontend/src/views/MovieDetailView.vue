<template>
  <div v-loading="loading">
    <template v-if="movie">
      <el-page-header @back="router.back()" style="margin-bottom:20px">
        <template #content>{{ movie.title }}</template>
      </el-page-header>

      <el-row :gutter="24">
        <!-- 左：信息 -->
        <el-col :span="16">
          <el-card>
            <el-descriptions :column="2" border>
              <el-descriptions-item label="年份">{{ movie.year || '—' }}</el-descriptions-item>
              <el-descriptions-item label="平均评分">
                <el-rate :model-value="(movie.avg_rating || 0) / 2" disabled show-score
                         score-template="{value}" />
                <span style="margin-left:8px; color:#999">（{{ movie.rating_count }} 人评分）</span>
              </el-descriptions-item>
              <el-descriptions-item label="类型" :span="2">
                <el-tag v-for="g in movie.genres" :key="g" style="margin-right:6px">{{ g }}</el-tag>
              </el-descriptions-item>
            </el-descriptions>
          </el-card>
        </el-col>

        <!-- 右：评分 -->
        <el-col :span="8">
          <el-card header="我的评分">
            <div style="text-align:center; padding:8px 0">
              <el-rate v-model="myRating" :max="5" allow-half show-text
                       :texts="['很差','较差','还行','推荐','强烈推荐']"
                       style="font-size:28px" />
              <el-button type="primary" style="margin-top:16px; width:100%"
                         :loading="submitting" :disabled="!myRating" @click="submitRating">
                提交评分
              </el-button>
            </div>
          </el-card>
        </el-col>
      </el-row>
    </template>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { moviesApi } from '@/api/movies'
import { ratingsApi } from '@/api/ratings'

const route     = useRoute()
const router    = useRouter()
const movie     = ref(null)
const loading   = ref(false)
const submitting= ref(false)
const myRating  = ref(0)

onMounted(async () => {
  loading.value = true
  try {
    const res = await moviesApi.detail(route.params.id)
    movie.value = res.data
  } finally {
    loading.value = false
  }
})

async function submitRating() {
  submitting.value = true
  try {
    // el-rate 最大5星，对应 rating 0.5~5.0
    await ratingsApi.submit({
      movie_id: Number(route.params.id),
      rating:   myRating.value,
    })
    ElMessage.success('评分成功！')
    // 刷新平均分
    const res = await moviesApi.detail(route.params.id)
    movie.value = res.data
  } finally {
    submitting.value = false
  }
}
</script>
