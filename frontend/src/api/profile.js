import http from './index'

// 个人主页接口。统一走 ./index 的 axios 实例：JWT 自动附带，
// 失败提示由拦截器统一弹，这里的调用方 catch 只做收尾、不再弹第二次。

/**
 * 获取用户基本信息
 */
export async function getUserInfo() {
  return http.get('/profile/info')
}

/**
 * 更新用户信息
 * @param {Object} data - { username?, bio? }（头像走 uploadAvatar，不从这里改）
 */
export async function updateUserInfo(data) {
  return http.put('/profile/info', data)
}

/**
 * 上传头像（multipart/form-data，字段名 file）。
 * 成功后返回更新后的用户信息，avatar_url 指向本服务的读取地址。
 * @param {File} file - 本地图片文件
 */
export async function uploadAvatar(file) {
  const form = new FormData()
  form.append('file', file)
  return http.post('/profile/avatar', form)
}

/**
 * 获取用户观影统计
 */
export async function getUserStats() {
  return http.get('/profile/stats')
}

/**
 * 获取用户评论列表
 * @param {number} page - 页码
 * @param {number} pageSize - 每页数量
 */
export async function getUserComments(page = 1, pageSize = 20) {
  return http.get('/profile/comments', { params: { page, page_size: pageSize } })
}
