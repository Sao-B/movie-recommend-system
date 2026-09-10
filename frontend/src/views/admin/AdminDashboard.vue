<template>
  <div class="admin-dashboard">
    <h2>系统概览</h2>
    <el-row :gutter="20" class="stats-row">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-item">
            <el-icon class="stat-icon user"><User /></el-icon>
            <div class="stat-content">
              <div class="stat-value">{{ stats.total_users || 0 }}</div>
              <div class="stat-label">总用户数</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-item">
            <el-icon class="stat-icon movie"><Film /></el-icon>
            <div class="stat-content">
              <div class="stat-value">{{ stats.total_movies || 0 }}</div>
              <div class="stat-label">总电影数</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-item">
            <el-icon class="stat-icon rating"><Star /></el-icon>
            <div class="stat-content">
              <div class="stat-value">{{ stats.total_ratings || 0 }}</div>
              <div class="stat-label">总评分数</div>
            </div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-item">
            <el-icon class="stat-icon model"><Setting /></el-icon>
            <div class="stat-content">
              <div class="stat-value">{{ stats.active_model_version || 'N/A' }}</div>
              <div class="stat-label">当前模型版本</div>
            </div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="20" style="margin-top: 20px;">
      <el-col :span="12">
        <el-card>
          <template #header>
            <span>最近评分</span>
          </template>
          <div v-if="stats.recent_ratings && stats.recent_ratings.length">
            <div v-for="rating in stats.recent_ratings" :key="rating.id" class="recent-item">
              <span>{{ rating.username }}</span> 给 <span>{{ rating.movie_title }}</span> 评分 <el-tag size="small">{{ rating.rating }}</el-tag>
            </div>
          </div>
          <el-empty v-else description="暂无评分" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card>
          <template #header>
            <span>热门电影 Top 10</span>
          </template>
          <div v-if="stats.top_movies && stats.top_movies.length">
            <div v-for="movie in stats.top_movies" :key="movie.movie_id" class="recent-item">
              <span>{{ movie.title }}</span>
              <el-tag size="small" type="success">{{ movie.rating_count }} 评分</el-tag>
            </div>
          </div>
          <el-empty v-else description="暂无数据" />
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'
import { apiAdmin } from '@/api/admin'
import { ElMessage } from 'element-plus'
import { User, Film, Star, Setting } from '@element-plus/icons-vue'

const stats = ref({})

const loadStats = async () => {
  try {
    stats.value = await apiAdmin.getStats()
  } catch (error) {
    ElMessage.error('加载统计数据失败')
  }
}

onMounted(() => {
  loadStats()
})
</script>

<style scoped>
.admin-dashboard h2 {
  margin-bottom: 20px;
}

.stats-row {
  margin-bottom: 20px;
}

.stat-item {
  display: flex;
  align-items: center;
  gap: 15px;
}

.stat-icon {
  font-size: 40px;
}

.stat-icon.user {
  color: #409eff;
}

.stat-icon.movie {
  color: #67c23a;
}

.stat-icon.rating {
  color: #e6a23c;
}

.stat-icon.model {
  color: #f56c6c;
}

.stat-content {
  flex: 1;
}

.stat-value {
  font-size: 28px;
  font-weight: bold;
  margin-bottom: 5px;
}

.stat-label {
  color: #909399;
  font-size: 14px;
}

.recent-item {
  padding: 10px 0;
  border-bottom: 1px solid #f0f0f0;
  display: flex;
  justify-content: space-between;
  align-items: center;
}

.recent-item:last-child {
  border-bottom: none;
}
</style>
