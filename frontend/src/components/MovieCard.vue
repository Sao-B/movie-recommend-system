<template>
  <el-card class="movie-card" shadow="hover" @click="$router.push('/movies/' + movie.movie_id)">
    <div class="movie-year" v-if="movie.year">{{ movie.year }}</div>
    <h4 class="movie-title">{{ movie.title }}</h4>
    <div class="movie-genres">
      <el-tag
        v-for="g in movie.genres?.slice(0, 3)"
        :key="g"
        size="small"
        type="info"
        class="genre-tag"
      >{{ g }}</el-tag>
    </div>
    <div v-if="predictedRating" class="predicted">
      预测评分：<strong>{{ predictedRating }}</strong>
    </div>
  </el-card>
</template>

<script setup>
defineProps({
  movie:          { type: Object, required: true },
  predictedRating:{ type: [Number, String], default: null },
})
</script>

<style scoped>
.movie-card {
  cursor: pointer;
  height: 160px;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  transition: transform .15s;
}
.movie-card:hover { transform: translateY(-3px); }
.movie-year  { font-size: 12px; color: #999; margin-bottom: 4px; }
.movie-title { font-size: 14px; margin: 0 0 8px; line-height: 1.4;
               display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }
.movie-genres { display: flex; flex-wrap: wrap; gap: 4px; }
.genre-tag   { font-size: 11px; }
.predicted   { font-size: 12px; color: #e6a23c; margin-top: 6px; }
</style>
