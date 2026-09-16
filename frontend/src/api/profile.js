import axios from 'axios'

const API_BASE = '/api/profile'

/**
 * 获取用户基本信息
 */
export async function getUserInfo() {
  const token = localStorage.getItem('token')
  const response = await axios.get(`${API_BASE}/info`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  return response.data
}

/**
 * 更新用户信息
 * @param {Object} data - { username?, avatar_url?, bio? }
 */
export async function updateUserInfo(data) {
  const token = localStorage.getItem('token')
  const response = await axios.put(`${API_BASE}/info`, data, {
    headers: { Authorization: `Bearer ${token}` }
  })
  return response.data
}

/**
 * 获取用户观影统计
 */
export async function getUserStats() {
  const token = localStorage.getItem('token')
  const response = await axios.get(`${API_BASE}/stats`, {
    headers: { Authorization: `Bearer ${token}` }
  })
  return response.data
}

/**
 * 获取用户评论列表
 * @param {number} page - 页码
 * @param {number} pageSize - 每页数量
 */
export async function getUserComments(page = 1, pageSize = 20) {
  const token = localStorage.getItem('token')
  const response = await axios.get(`${API_BASE}/comments`, {
    params: { page, page_size: pageSize },
    headers: { Authorization: `Bearer ${token}` }
  })
  return response.data
}
