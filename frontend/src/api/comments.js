import http from './index'

export const apiComments = {
  // 获取影评列表：comments 是公开时间线，my_comment 是当前用户自己的那条
  //（即使因作者没有评分而仅自己可见也会返回，供详情页置顶展示）
  list(movieId, params = {}) {
    return http.get(`/movies/${movieId}/comments`, { params })
  },

  // 发表影评（正文必填；同一用户对同一电影只保留一条，重复提交即更新）
  create(movieId, content) {
    return http.post(`/movies/${movieId}/comments`, { content })
  },

  // 编辑自己的影评（正文必填；后端会记 edited_at，用于显示「上次修改于」）
  update(commentId, content) {
    return http.put(`/comments/${commentId}`, { content })
  },

  // 删除影评。withRating 为 true 时后端在同一次提交里把该用户对这部电影的评分
  // 一起删掉（详情页影评卡片的「删除」走这条，确认框里已讲明评分会一并删除）
  delete(commentId, withRating = false) {
    return http.delete(`/comments/${commentId}`, {
      params: withRating ? { with_rating: 1 } : {}
    })
  }
}
