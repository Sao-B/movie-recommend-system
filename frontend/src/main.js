import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'
import 'element-plus/dist/index.css'
import 'element-plus/theme-chalk/dark/css-vars.css'
import '@/styles/theme.css'
import '@/styles/admin.css'
import App from './App.vue'
import router from './router'
import { useThemeStore } from '@/stores/theme'

const app = createApp(App)

// 临时调试：挂载失败的错误收集（排查 ThemeSwitch 白屏）
window.__errs = []
window.addEventListener('error', (e) => window.__errs.push('ERR: ' + ((e.error && e.error.stack) || e.message)))
window.addEventListener('unhandledrejection', (e) => window.__errs.push('REJ: ' + String(e.reason).slice(0, 300)))

for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
app.use(ElementPlus)

// 挂载前先落一次主题，避免刷新时闪一下日间皮肤
useThemeStore()

app.mount('#app')
