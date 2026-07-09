<template>
  <el-header class="navbar">
    <div class="navbar-inner">
      <router-link to="/" class="brand">🎬 电影推荐系统</router-link>
      <div class="nav-right">
        <router-link to="/" class="nav-link">电影库</router-link>
        <router-link v-if="auth.isLoggedIn" to="/recommendations" class="nav-link">
          我的推荐
        </router-link>
        <template v-if="auth.isLoggedIn">
          <span class="username">{{ auth.user?.username }}</span>
          <el-button size="small" @click="handleLogout">退出</el-button>
        </template>
        <template v-else>
          <router-link to="/login"><el-button size="small">登录</el-button></router-link>
          <router-link to="/register"><el-button size="small" type="primary">注册</el-button></router-link>
        </template>
      </div>
    </div>
  </el-header>
</template>

<script setup>
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

const auth   = useAuthStore()
const router = useRouter()

function handleLogout() {
  auth.logout()
  ElMessage.success('已退出登录')
  router.push('/')
}
</script>

<style scoped>
.navbar {
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  position: sticky;
  top: 0;
  z-index: 100;
  height: 60px !important;
  padding: 0;
}
.navbar-inner {
  max-width: 1200px;
  margin: 0 auto;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
}
.brand {
  font-size: 18px;
  font-weight: 700;
  color: #409eff;
  text-decoration: none;
}
.nav-right {
  display: flex;
  align-items: center;
  gap: 16px;
}
.nav-link {
  color: #333;
  text-decoration: none;
  font-size: 14px;
}
.nav-link:hover { color: #409eff; }
.username { font-size: 14px; color: #666; }
</style>
