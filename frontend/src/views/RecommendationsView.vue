<template>
  <div>
    <el-card header="🎯 专属推荐" style="margin-bottom:16px">
      <el-alert v-if="!loading && recs.length === 0"
                title="暂无推荐数据"
                description="请先前往【电影库】评分几部电影，等待系统离线训练后即可获得个性化推荐。"
                type="info" show-icon :closable="false" />
    </el-card>

    <el-row v-loading="loading" :gutter="16">
      <el-col v-for="item in recs" :key="item.movie.movie_id"
              :xs="24" :sm="12" :md="8" :lg="6" :xl="4"
              style="margin-bottom:16px">
        <MovieCard :movie="item.movie" :extra="`预测评分 ${item.predicted_rating}`"
                   @click="router.push(`/movies/${item.movie.movie_id}`)" />
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { recommendationsApi } from '@/api/recommendations'
import MovieCard from '@/components/MovieCard.vue'

const router  = useRouter()
const recs    = ref([])
const loading = ref(false)

onMounted(async () => {
  loading.value = true
  try {
    const res = await recommendationsApi.mine()
    recs.value = res.data.data || []
  } finally {
    loading.value = false
  }
})
</script>
