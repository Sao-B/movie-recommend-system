import axios from 'axios'

const http = axios.create({
  baseURL: '/api',
  timeout: 15000,
})

// 请求拦截：自动附带 JWT
http.interceptors.request.use((config) => {
  const token = localStorage.getItem('token')
  if (token) config.headers.Authorization = 'Bearer ' + token
  return config
})

// 响应拦截：统一取 data，401 跳转登录
http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    // 401 分两种情况：
    // 1. 登录接口本身返回 401（密码错误）→ 不跳转，让调用方处理
    // 2. 其他接口返回 401（token 过期）→ 清除凭据并跳转登录页
    if (err.response?.status === 401 && window.location.pathname !== '/login') {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      window.location.href = '/login'
    }
    return Promise.reject(err.response?.data?.msg || '请求失败，请重试')
  }
)

export default http
