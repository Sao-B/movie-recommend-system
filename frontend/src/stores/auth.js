import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiAuth } from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user  = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  const isLoggedIn = computed(() => !!token.value)
  const isAdmin = computed(() => user.value?.role === 'admin')

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

  // 个人主页改完用户名 / 头像后调用：同步 header 里的显示名，并写回 localStorage，
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

  return { token, user, isLoggedIn, isAdmin, login, register, logout, updateUser }
})
