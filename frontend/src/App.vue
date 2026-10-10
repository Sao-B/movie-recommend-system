<template>
  <!-- 前后台共用同一根 NavBar，保证 header 风格完全一致 -->
  <NavBar />
  <router-view />

  <!-- 首次引导：登录后如果还没选过偏好类型（needs_onboarding），弹一次 -->
  <GenrePickerDialog
    :visible="showOnboarding"
    title="欢迎！选择你喜欢的电影类型"
    @close="showOnboarding = false"
  />
</template>

<script setup>
import { ref, watch } from 'vue'
import NavBar from '@/components/NavBar.vue'
import GenrePickerDialog from '@/components/GenrePickerDialog.vue'
import { useAuthStore } from '@/stores/auth'
import { getUserInfo } from '@/api/profile'

const auth = useAuthStore()
const showOnboarding = ref(false)

// 登录态变化（登录/刷新页面）时拉一次最新资料：
// needs_onboarding=true（favorite_genres 为 NULL）才弹窗；
// 跳过/保存后都会写库，之后不再弹。顺带把本地缓存的 user 刷新到最新。
watch(() => auth.token, async (token) => {
  if (!token) {
    showOnboarding.value = false
    return
  }
  try {
    const data = await getUserInfo()
    auth.updateUser(data)
    showOnboarding.value = !!data.needs_onboarding
  } catch { /* 拉不到就不弹，不打断浏览 */ }
}, { immediate: true })
</script>

<style>
* { box-sizing: border-box; }
body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: var(--app-bg-page); }
</style>
