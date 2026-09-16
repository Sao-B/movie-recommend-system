import axios from 'axios'
import { ElMessage } from 'element-plus'

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

// 响应拦截：成功直接返回 data；失败统一弹提示并 reject 错误字符串。
// 全站错误 toast 只在这一处弹，页面里的 catch 只做收尾，不要再弹第二次。
http.interceptors.response.use(
  (res) => res.data,
  (err) => {
    const msg = err.response?.data?.msg || err.response?.data?.error || '请求失败，请重试'
    // 401 分两种情况：
    // 1. 登录接口本身返回 401（密码错误）→ 不跳转，交给登录页
    // 2. 其他接口返回 401（token 过期）→ 清除凭据并跳转登录页
    if (err.response?.status === 401 && window.location.pathname !== '/login') {
      localStorage.removeItem('token')
      localStorage.removeItem('user')
      window.location.href = '/login'
    } else {
      ElMessage.error(msg)
    }
    return Promise.reject(msg)
  }
)

export default http
