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
 * @param {Object} data - { nickname?, bio?, avatar_url? }
 *   头像只接受内置清单里的地址，或用 null 表示「恢复默认」；账号不可改，后端会 400。
 */
export async function updateUserInfo(data) {
  return http.put('/profile/info', data)
}

/**
 * 内置 SVG 头像清单：[{ id, name, url }]，顺序即选择面板里的展示顺序。
 */
export async function getPresetAvatars() {
  return http.get('/profile/avatars')
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
