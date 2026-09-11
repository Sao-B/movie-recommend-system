<template>
  <el-container class="admin-layout">
    <el-aside width="200px">
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
      <el-main>
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { DataAnalysis, User, Film, Star, Setting, Management } from '@element-plus/icons-vue'

const route = useRoute()
const activeMenu = computed(() => route.path)
</script>

<style scoped>
.admin-layout {
  height: 100vh;
}

.el-aside {
  background-color: #545c64;
}

.el-menu {
  border-right: none !important;
}

.el-main {
  background-color: #f5f5f5;
  padding: 20px;
}
</style>
