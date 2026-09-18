import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url))
    }
  },
  server: {
    port: 5173,
    // 开发环境代理：前端请求 /api/* 自动转发到 Flask
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true
      }
    }
  },
  // 组件测试（vitest 复用这份配置）：测试文件放在 src/**/__tests__/*.spec.js
  test: {
    environment: 'jsdom',
    include: ['src/**/__tests__/*.spec.js']
  }
})
