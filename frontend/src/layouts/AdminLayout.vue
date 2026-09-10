<template>
  <el-container class="admin-layout">
    <el-aside width="200px">
      <div class="logo">
        <el-icon><Management /></el-icon>
        <span>管理后台</span>
      </div>
      <el-menu
        :default-active="activeMenu"
        router
        background-color="#545c64"
        text-color="#fff"
        active-text-color="#ffd04b"
      >
        <el-menu-item index="/admin">
          <el-icon><DataAnalysis /></el-icon>
          <span>仪表盘</span>
        </el-menu-item>
        <el-menu-item index="/admin/users">
          <el-icon><User /></el-icon>
          <span>用户管理</span>
        </el-menu-item>
        <el-menu-item index="/admin/movies">
          <el-icon><Film /></el-icon>
          <span>电影管理</span>
        </el-menu-item>
        <el-menu-item index="/admin/ratings">
          <el-icon><Star /></el-icon>
          <span>评分管理</span>
        </el-menu-item>
        <el-menu-item index="/admin/models">
          <el-icon><Setting /></el-icon>
          <span>模型管理</span>
        </el-menu-item>
      </el-menu>
    </el-aside>
    <el-container>
      <el-header>
        <h2>电影推荐系统 - 管理后台</h2>
        <div class="user-info">
          <span>管理员: {{ username }}</span>
          <el-button link @click="router.push('/')">返回前台</el-button>
          <el-divider direction="vertical" />
          <el-button type="danger" link @click="handleLogout">退出登录</el-button>
        </div>
      </el-header>
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { DataAnalysis, User, Film, Star, Setting, Management } from '@element-plus/icons-vue'

const router = useRouter()
const route = useRoute()
const authStore = useAuthStore()

const username = computed(() => authStore.user?.username || 'Admin')
const activeMenu = computed(() => route.path)

const handleLogout = () => {
  authStore.logout()
  router.push('/login')
}
</script>

<style scoped>
.admin-layout {
  height: 100vh;
}

.el-aside {
  background-color: #545c64;
}

.logo {
  height: 60px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
  font-weight: bold;
  color: #fff;
  gap: 8px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.1);
}

.el-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background-color: #fff;
  border-bottom: 1px solid #e6e6e6;
  padding: 0 20px;
}

.el-header h2 {
  margin: 0;
  font-size: 20px;
}

.user-info {
  display: flex;
  align-items: center;
  gap: 10px;
}

.el-main {
  background-color: #f5f5f5;
  padding: 20px;
}
</style>
