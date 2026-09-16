<template>
  <!-- 顶栏复用前台的 NavBar（App.vue 全局渲染），这里只负责侧栏 + 内容区 -->
  <div class="admin-shell">
    <aside class="admin-side" :style="{ width: sideWidth + 'px' }">
      <el-menu :default-active="route.path" router>
        <el-menu-item index="/admin/dashboard">
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
          <el-icon><Cpu /></el-icon>
          <span>模型管理</span>
        </el-menu-item>
      </el-menu>

      <!-- 侧栏右缘 6px 是拖拽热区：悬停出现左右箭头指针，按住可调整宽度 -->
      <div
        class="side-resizer"
        :class="{ resizing: resizing }"
        title="拖拽调整侧栏宽度"
        @mousedown="startResize"
      ></div>
    </aside>

    <main class="admin-main">
      <router-view />
    </main>
  </div>
</template>

<script setup>
import { ref } from 'vue'
import { useRoute } from 'vue-router'
import { User, DataAnalysis, Film, Star, Cpu } from '@element-plus/icons-vue'

const route = useRoute()

const WIDTH_KEY = 'admin-side-width'
const MIN_WIDTH = 180
const MAX_WIDTH = 400

const sideWidth = ref(Number(localStorage.getItem(WIDTH_KEY)) || 220)
const resizing = ref(false)

function startResize(event) {
  event.preventDefault()
  const startX = event.clientX
  const startWidth = sideWidth.value
  resizing.value = true
  // 拖拽期间全局锁定指针样式并禁止选中文字，避免鼠标滑出热区后闪烁
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'

  const onMove = (e) => {
    sideWidth.value = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, startWidth + e.clientX - startX))
  }
  const onUp = () => {
    localStorage.setItem(WIDTH_KEY, String(sideWidth.value))
    resizing.value = false
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }

  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
</script>

<style scoped>
/* NavBar 高 60px 且 sticky，剩余高度给侧栏和内容区 */
.admin-shell {
  height: calc(100vh - 60px);
  display: flex;
}

.admin-side {
  position: relative;
  flex: none;
  background: var(--app-bg-surface);
  border-right: 1px solid var(--app-border);
  overflow-y: auto;
  overflow-x: hidden;
}

.admin-side :deep(.el-menu) {
  border-right: none;
  width: 100%;
}

/* 拖拽热区：默认透明，悬停/拖拽时亮起一条蓝线并显示 col-resize 指针 */
.side-resizer {
  position: absolute;
  top: 0;
  right: -3px;
  width: 6px;
  height: 100%;
  cursor: col-resize;
  z-index: 10;
}

.side-resizer:hover,
.side-resizer.resizing {
  background: rgba(64, 158, 255, 0.35);
}

.admin-main {
  flex: 1;
  min-width: 0;
  padding: 20px;
  overflow-y: auto;
  background: var(--app-bg-page);
}
</style>
