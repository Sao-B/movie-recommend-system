import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { apiAuth } from '@/api/auth'

export const useAuthStore = defineStore('auth', () => {
  const token = ref(localStorage.getItem('token') || '')
  const user  = ref(JSON.parse(localStorage.getItem('user') || 'null'))

  const isLoggedIn = computed(() => !!token.value)

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

  return { token, user, isLoggedIn, login, register, logout }
})
