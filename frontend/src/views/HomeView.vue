<template>
  <el-container style="min-height: 100vh">
    <!-- 顶部导航 -->
    <el-header height="60px" style="background:#409eff; display:flex; align-items:center; justify-content:space-between; padding:0 24px">
      <div style="display:flex; align-items:center; gap:24px">
        <span style="color:#fff; font-size:18px; font-weight:700; cursor:pointer" @click="router.push('/')">🎬 电影推荐</span>
        <el-menu mode="horizontal" :ellipsis="false" background-color="#409eff"
                 text-color="#fff" active-text-color="#ffd04b"
                 :default-active="route.path" router style="border-bottom:none">
          <el-menu-item index="/">首页</el-menu-item>
          <el-menu-item index="/movies">电影库</el-menu-item>
          <el-menu-item index="/recommendations">我的推荐</el-menu-item>
        </el-menu>
      </div>
      <div style="display:flex; align-items:center; gap:12px; color:#fff">
        <span>{{ userStore.user?.username }}</span>
        <el-button size="small" @click="handleLogout">退出</el-button>
      </div>
    </el-header>

    <el-main style="background:#f5f7fa; padding:24px">
      <router-view />
    </el-main>
  </el-container>
</template>

<script setup>
import { useRouter, useRoute } from 'vue-router'
import { useUserStore } from '@/stores/user'

const router    = useRouter()
const route     = useRoute()
const userStore = useUserStore()

function handleLogout() {
  userStore.logout()
  router.push('/login')
}
</script>
