<template>
  <el-dropdown
    trigger="click"
    popper-class="tm-dropdown"
    @command="theme.setMode"
  >
    <button class="tm-btn" :title="MODES[theme.mode].name">
      <el-icon><component :is="MODES[theme.mode].icon" /></el-icon>
    </button>
    <template #dropdown>
      <el-dropdown-menu>
        <el-dropdown-item
          v-for="(m, key) in MODES"
          :key="key"
          :command="key"
          :class="{ 'is-current': key === theme.mode }"
          :title="m.name"
        >
          <el-icon><component :is="m.icon" /></el-icon>
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<script setup>
import { Moon, Monitor, Sunny } from '@element-plus/icons-vue'
import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()

const MODES = {
  light: { name: '日间', icon: Sunny },
  dark: { name: '夜间', icon: Moon },
  system: { name: '跟随系统', icon: Monitor },
}
</script>

<style scoped>
.tm-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 38px;
  height: 32px;
  border: 1px solid var(--app-border);
  border-radius: 8px;
  background: var(--app-bg-surface);
  color: var(--app-text-secondary);
  cursor: pointer;
  outline: none;
}

.tm-btn:hover {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
}
</style>

<style>
/* 下拉面板挂在 body 下，scoped 命不中，这里全局定制：纯图标、居中、当前模式高亮 */
.tm-dropdown .el-dropdown-menu__item {
  padding: 7px 12px;
  justify-content: center;
}

.tm-dropdown .el-dropdown-menu__item.is-current {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}
</style>
