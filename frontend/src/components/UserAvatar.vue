<template>
  <!-- 有头像图就用图；没有（或图挂了）就回落到显示名（昵称优先、回落账号）首字符，全站统一这一套 -->
  <el-avatar
    :size="size"
    :src="src || undefined"
    class="user-avatar"
    :style="{ fontSize: Math.round(size * 0.42) + 'px' }"
  >
    {{ initial }}
  </el-avatar>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  src:  { type: String, default: '' },
  name: { type: String, default: '' },
  size: { type: Number, default: 36 },
})

// 默认头像＝显示名（昵称优先、回落账号）首字符：中文取第一个字，英文首字母大写；还没加载出来时给个问号占位
const initial = computed(() => {
  const name = (props.name || '').trim()
  return name ? name.charAt(0).toUpperCase() : '?'
})
</script>

<style scoped>
/* 图片加载失败时 el-avatar 会自动换成 default 插槽，所以这层底色只对「首字符」可见 */
.user-avatar {
  flex: none;
  font-weight: 700;
  color: #409eff;
  background: var(--app-fill);
  user-select: none;
}
</style>
