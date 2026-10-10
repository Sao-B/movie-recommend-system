import axios from 'axios'
import { ElMessage } from 'element-plus'

const http = axios.create({
  baseURL: '/api',
  timeout: 15000,
})

// 后端冷启动（重启机器后第一次跑 start.bat）实测要 ~20s 才监听端口，而 vite 两秒就好了。
// 启动脚本现在会等 /api/health 通过再打开浏览器，这里再兜一层：连不上后端时
// 对 GET 退避重试两次，页面能自己缓过来，不至于停在空列表上等用户手动刷新。
const NETWORK_RETRY_TIMES = 2
const NETWORK_RETRY_DELAY_MS = 1000

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
  async (err) => {
    const config = err.config || {}
    // 没有 response 才是「连不上后端」；超时（ECONNABORTED）不重试——那是后端慢，不是没起来
    const isOffline = !err.response && err.code !== 'ECONNABORTED'
    const retries = config.__networkRetry || 0

    if (isOffline && (config.method || 'get').toLowerCase() === 'get'
        && retries < NETWORK_RETRY_TIMES && !config.__skipNetworkRetry) {
      config.__networkRetry = retries + 1
      await new Promise((resolve) => setTimeout(resolve, NETWORK_RETRY_DELAY_MS * (retries + 1)))
      return http(config)
    }

    const msg = err.response?.data?.msg || err.response?.data?.error
      || (isOffline ? '连不上后端服务，请稍后重试（后端可能还在启动）' : '请求失败，请重试')
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
