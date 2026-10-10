<template>
  <el-header class="navbar">
    <div class="navbar-inner" :class="{ 'is-admin': isAdminRoute }">
      <router-link to="/" class="brand">🎬 电影推荐系统</router-link>
      <div class="nav-right">
        <router-link to="/" class="nav-link">电影库</router-link>
        <router-link v-if="auth.isLoggedIn" to="/recommendations" class="nav-link">
          我的推荐
        </router-link>
        <router-link v-if="auth.isAdmin" to="/admin" class="nav-link admin-link">
          管理后台
        </router-link>
        <template v-if="auth.isLoggedIn">
          <ThemeSwitch />
          <el-dropdown @command="handleCommand" trigger="hover">
            <span class="username-dropdown">
              <!-- 头像与个人主页同一套：无头像图时显示昵称首字符；显示名一律用 displayName（昵称优先、回落账号） -->
              <UserAvatar :src="auth.user?.avatar_url" :name="auth.displayName" :size="28" />
              {{ auth.displayName }}
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <el-icon><User /></el-icon>
                  个人主页
                </el-dropdown-item>
                <el-dropdown-item command="logout" divided>
                  <el-icon><SwitchButton /></el-icon>
                  退出登录
                </el-dropdown-item>
              </el-dropdown-menu>
            </template>
          </el-dropdown>
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
import { computed } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { User, SwitchButton } from '@element-plus/icons-vue'
import ThemeSwitch from '@/components/ThemeSwitch.vue'
import UserAvatar from '@/components/UserAvatar.vue'

const auth   = useAuthStore()
const router = useRouter()
const route  = useRoute()
// 管理后台是全宽双栏布局，header 内容不再收进 1200px 容器，避免与侧栏错位
const isAdminRoute = computed(() => route.path.startsWith('/admin'))

function handleCommand(command) {
  switch (command) {
    case 'profile':
      router.push('/profile')
      break
    case 'logout':
      handleLogout()
      break
  }
}

function handleLogout() {
  auth.logout()
  ElMessage.success('已退出登录')
  router.push('/')
}
</script>

<style scoped>
.navbar {
  background: var(--app-bg-surface);
  border-bottom: 1px solid var(--app-border);
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

/* 后台双栏全宽，header 内容与侧栏左对齐 */
.navbar-inner.is-admin {
  max-width: none;
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
  color: var(--app-text);
  text-decoration: none;
  font-size: 14px;
}
.nav-link:hover { color: #409eff; }
.admin-link {
  color: #e6a23c;
  font-weight: 500;
}
.admin-link:hover { color: #f0a020; }

/* 头像 + 显示名，本身就是下拉的触发区；不再另挂一个小箭头图标 */
.username-dropdown {
  font-size: 14px;
  color: var(--app-text);
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  border-radius: 999px;
  transition: background-color 0.2s;
}

.username-dropdown:hover {
  background-color: var(--app-fill);
  color: #409eff;
}
</style>
