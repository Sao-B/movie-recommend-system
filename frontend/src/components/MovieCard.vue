<template>
  <el-card class="movie-card" shadow="hover" @click="$emit('click')">
    <!-- 封面占位 -->
    <div class="poster">
      <span class="poster-year">{{ movie.year || '?' }}</span>
    </div>
    <div class="info">
      <div class="title" :title="movie.title">{{ movie.title }}</div>
      <div class="genres">
        <el-tag v-for="g in (movie.genres || []).slice(0, 3)" :key="g"
                size="small" style="margin-right:4px; margin-bottom:4px">
          {{ g }}
        </el-tag>
      </div>
      <div v-if="extra" class="extra">{{ extra }}</div>
    </div>
  </el-card>
</template>

<script setup>
defineProps({
  movie: { type: Object, required: true },
  extra: { type: String, default: '' },
})
defineEmits(['click'])
</script>

<style scoped>
.movie-card { cursor: pointer; height: 100%; }
.poster {
  height: 160px;
  background: linear-gradient(135deg, #667eea, #764ba2);
  border-radius: 4px;
  display: flex;
  align-items: center;
  justify-content: center;
}
.poster-year { color: #fff; font-size: 28px; font-weight: 700; opacity: .7; }
.info { padding: 10px 0 0; }
.title {
  font-weight: 600;
  font-size: 14px;
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
  margin-bottom: 6px;
}
.genres { min-height: 28px; }
.extra { color: #409eff; font-size: 12px; margin-top: 4px; }
</style>
