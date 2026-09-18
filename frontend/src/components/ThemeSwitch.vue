<template>
  <el-dropdown
    trigger="click"
    placement="bottom-end"
    popper-class="tm-dropdown"
    :hide-on-click="true"
    :show-timeout="0"
    :hide-timeout="120"
    @command="theme.setMode"
    @visible-change="panelOpen = $event"
  >
    <button
      class="tm-btn"
      :class="{ 'is-open': panelOpen }"
      :title="`主题：${MODES[theme.mode].name}`"
      :aria-label="`切换主题，当前${MODES[theme.mode].name}`"
    >
      <el-icon class="tm-btn-icon"><component :is="MODES[theme.mode].icon" /></el-icon>
    </button>

    <template #dropdown>
      <el-dropdown-menu class="tm-menu">
        <el-dropdown-item
          v-for="(m, key) in MODES"
          :key="key"
          :command="key"
          :class="{ 'is-current': key === theme.mode }"
        >
          <span class="tm-item">
            <el-icon class="tm-item-icon"><component :is="m.icon" /></el-icon>
            <span class="tm-item-name">{{ m.name }}</span>
            <!-- 当前模式打勾：图标按钮本身看不出选中的是哪一档 -->
            <el-icon v-if="key === theme.mode" class="tm-item-check"><Check /></el-icon>
          </span>
        </el-dropdown-item>
      </el-dropdown-menu>
    </template>
  </el-dropdown>
</template>

<script setup>
import { ref } from 'vue'
import { Check, Moon, Monitor, Sunny } from '@element-plus/icons-vue'
import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()
const panelOpen = ref(false)

const MODES = {
  light:  { name: '日间',   icon: Sunny },
  dark:   { name: '夜间',   icon: Moon },
  system: { name: '跟随系统', icon: Monitor },
}
</script>

<style scoped>
.tm-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 40px;
  height: 34px;
  padding: 0;
  border: 1px solid var(--app-border);
  border-radius: 10px;
  background: var(--app-bg-surface);
  color: var(--app-text-secondary);
  cursor: pointer;
  outline: none;
  transition: color 0.2s, border-color 0.2s, background-color 0.2s, box-shadow 0.2s;
}

.tm-btn:hover {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}

/* 面板打开时按钮保持高亮，形成「一直按着」的连贯感 */
.tm-btn.is-open {
  border-color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  box-shadow: 0 0 0 3px var(--el-color-primary-light-9);
}

.tm-btn-icon {
  font-size: 17px;
  transition: transform 0.25s ease;
}

.tm-btn:hover .tm-btn-icon,
.tm-btn.is-open .tm-btn-icon {
  transform: scale(1.08) rotate(-8deg);
}

.tm-btn:focus-visible {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 3px var(--el-color-primary-light-9);
}
</style>

<style>
/* 下拉面板挂在 body 下（teleport），scoped 命不中，这里全局定制。
   面板与条目都走 Element Plus 变量，日间 / 夜间自动跟随。 */
.tm-dropdown.el-popper {
  border-radius: 12px;
  border-color: var(--el-border-color-lighter);
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.14);
}

.tm-dropdown .el-dropdown-menu {
  padding: 6px;
  border-radius: 12px;
  background: transparent;
}

.tm-dropdown .el-dropdown-menu__item {
  padding: 0;
  margin: 2px 0;
  border-radius: 8px;
  color: var(--el-text-color-regular);
  transition: background-color 0.18s, color 0.18s;
}

.tm-dropdown .el-dropdown-menu__item:not(.is-disabled):hover,
.tm-dropdown .el-dropdown-menu__item:not(.is-disabled):focus {
  background: var(--el-fill-color);
  color: var(--el-text-color-primary);
}

/* 当前档位：淡主色底 + 主色文字 + 打勾 */
.tm-dropdown .el-dropdown-menu__item.is-current,
.tm-dropdown .el-dropdown-menu__item.is-current:not(.is-disabled):hover {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
}

.tm-dropdown .tm-item {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-width: 132px;
  padding: 8px 10px;
  font-size: 13px;
}

.tm-dropdown .tm-item-icon {
  font-size: 16px;
}

.tm-dropdown .tm-item-name {
  flex: 1;
  text-align: left;
}

.tm-dropdown .el-dropdown-menu__item.is-current .tm-item-name {
  font-weight: 500;
}

.tm-dropdown .tm-item-check {
  font-size: 14px;
  color: var(--el-color-primary);
}
</style>
