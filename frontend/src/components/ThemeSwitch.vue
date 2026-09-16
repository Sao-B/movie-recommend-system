<template>
  <el-radio-group class="theme-switch" :model-value="theme.mode" size="small" @change="theme.setMode">
    <el-radio-button v-for="opt in options" :key="opt.value" :label="opt.value">
      <el-tooltip :content="opt.tip" placement="bottom">
        <span class="switch-item">
          <el-icon><component :is="opt.icon" /></el-icon>
        </span>
      </el-tooltip>
    </el-radio-button>
  </el-radio-group>
</template>

<script setup>
import { useThemeStore } from '@/stores/theme'

const theme = useThemeStore()

const options = [
  { value: 'light', icon: 'Sunny', tip: '日间模式' },
  { value: 'dark', icon: 'Moon', tip: '夜间模式' },
  { value: 'system', icon: 'Monitor', tip: '跟随系统' }
]
</script>

<style scoped>
.theme-switch {
  margin-right: 4px;
}

.switch-item {
  display: flex;
  align-items: center;
  font-size: 15px;
}
</style>
