import http from './index'

export const apiComments = {
  // 获取电影评论列表
  list(movieId, params = {}) {
    return http.get(`/movies/${movieId}/comments`, { params })
  },

  // 发表评论
  create(movieId, content) {
    return http.post(`/movies/${movieId}/comments`, { content })
  },

  // 删除评论
  delete(commentId) {
    return http.delete(`/comments/${commentId}`)
  }
}
