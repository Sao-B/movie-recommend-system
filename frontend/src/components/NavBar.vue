<template>
  <el-header class="navbar">
    <div class="navbar-inner">
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
          <el-dropdown
            @command="handleCommand"
            trigger="hover"
            @visible-change="dropdownVisible = $event"
          >
            <span class="username-dropdown">
              {{ auth.user?.username }}
              <el-icon
                class="el-icon--right arrow-icon"
                :class="{
                  'rotate-cw': dropdownVisible && rotateDirection === 'clockwise',
                  'rotate-ccw': dropdownVisible && rotateDirection === 'counterclockwise',
                  'rotate-back-cw': !dropdownVisible && rotateDirection === 'counterclockwise',
                  'rotate-back-ccw': !dropdownVisible && rotateDirection === 'clockwise'
                }"
              >
                <arrow-down />
              </el-icon>
            </span>
            <template #dropdown>
              <el-dropdown-menu>
                <el-dropdown-item command="profile">
                  <el-icon><User /></el-icon>
                  个人主页
                </el-dropdown-item>
                <el-dropdown-item command="settings">
                  <el-icon><Setting /></el-icon>
                  设置
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
import { ref, watch } from 'vue'
import { useAuthStore } from '@/stores/auth'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { ArrowDown, User, Setting, SwitchButton } from '@element-plus/icons-vue'

const auth   = useAuthStore()
const router = useRouter()
const dropdownVisible = ref(false)
const rotateDirection = ref('clockwise') // 'clockwise' 或 'counterclockwise'

// 监听下拉菜单打开，随机决定旋转方向
watch(dropdownVisible, (isVisible) => {
  if (isVisible) {
    rotateDirection.value = Math.random() > 0.5 ? 'clockwise' : 'counterclockwise'
  }
})

function handleCommand(command) {
  switch (command) {
    case 'profile':
      ElMessage.info('个人主页功能开发中')
      // router.push('/profile')
      break
    case 'settings':
      ElMessage.info('设置功能开发中')
      // router.push('/settings')
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
.admin-link {
  color: #e6a23c;
  font-weight: 500;
}
.admin-link:hover { color: #f0a020; }

.username-dropdown {
  font-size: 14px;
  color: #333;
  cursor: pointer;
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 12px;
  border-radius: 4px;
  transition: background-color 0.2s;
}

.username-dropdown:hover {
  background-color: #f5f7fa;
  color: #409eff;
}

.username-dropdown .arrow-icon {
  transform-origin: center;
}

/* 展开：顺时针旋转 */
.username-dropdown .arrow-icon.rotate-cw {
  animation: rotateCW 0.3s ease forwards;
}

/* 展开：逆时针旋转 */
.username-dropdown .arrow-icon.rotate-ccw {
  animation: rotateCCW 0.3s ease forwards;
}

/* 收起：顺时针旋转回去（原来是逆时针展开的） */
.username-dropdown .arrow-icon.rotate-back-cw {
  animation: rotateBackCW 0.3s ease forwards;
}

/* 收起：逆时针旋转回去（原来是顺时针展开的） */
.username-dropdown .arrow-icon.rotate-back-ccw {
  animation: rotateBackCCW 0.3s ease forwards;
}

@keyframes rotateCW {
  from { transform: rotate(0deg); }
  to { transform: rotate(180deg); }
}

@keyframes rotateCCW {
  from { transform: rotate(0deg); }
  to { transform: rotate(-180deg); }
}

@keyframes rotateBackCW {
  from { transform: rotate(-180deg); }
  to { transform: rotate(0deg); }
}

@keyframes rotateBackCCW {
  from { transform: rotate(180deg); }
  to { transform: rotate(0deg); }
}
</style>
