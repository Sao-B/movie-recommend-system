import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

const STORAGE_KEY = 'theme-mode'
const DARK_CLASS = 'dark'

// 三种模式：light / dark / system（跟随操作系统）
export const useThemeStore = defineStore('theme', () => {
  const media = window.matchMedia('(prefers-color-scheme: dark)')

  const mode = ref(localStorage.getItem(STORAGE_KEY) || 'system')
  const systemPrefersDark = ref(media.matches)

  const isDark = computed(() =>
    mode.value === 'system' ? systemPrefersDark.value : mode.value === 'dark'
  )

  // Element Plus 通过 <html class="dark"> 切换暗色变量，本站自定义色走 CSS 变量
  function apply() {
    const root = document.documentElement
    root.classList.toggle(DARK_CLASS, isDark.value)
    root.style.colorScheme = isDark.value ? 'dark' : 'light'
  }

  function setMode(next) {
    mode.value = next
    localStorage.setItem(STORAGE_KEY, next)
    apply()
  }

  media.addEventListener('change', (e) => {
    systemPrefersDark.value = e.matches
    apply()
  })

  apply()

  return { mode, isDark, setMode }
})
