import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiAuth } from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user  = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.role === 'admin')
  // 界面上显示的名字：昵称优先，没设昵称就回落账号（与后端 User.display_name 一个口径）
  const displayName = computed(() => user.value?.nickname || user.value?.username || '')

  function _persist(t, u) {
    token.value = t
    user.value  = u
    localStorage.setItem('token', t)
    localStorage.setItem('user', JSON.stringify(u))
  }

  function logout() {
    token.value = ''
    user.value  = null
    localStorage.removeItem('token')
    localStorage.removeItem('user')
  }

  // 个人主页改完昵称 / 头像后调用：同步 header 里的显示名，并写回 localStorage，
  // 否则刷新页面又会读回旧值
  function updateUser(patch) {
    if (!user.value) return
    _persist(token.value, { ...user.value, ...patch })
  }

  async function login(username, password) {
    const data = await apiAuth.login(username, password)
    _persist(data.token, data.user)
    return data.user
  }

  async function register(username, password) {
    const data = await apiAuth.register(username, password)
    _persist(data.token, data.user)
    return data.user
  }

  return { token, user, isLoggedIn, isAdmin, displayName, login, register, logout, updateUser }
})
