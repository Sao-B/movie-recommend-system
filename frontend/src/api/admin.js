import http from './index'

export const apiAdmin = {
  // 统计数据
  getStats: () => http.get('/admin/stats'),

  // 用户管理
  listUsers: (params) => http.get('/admin/users', { params }),
  createUser: (data) => http.post('/admin/users', data),
  updateUser: (userId, data) => http.patch(`/admin/users/${userId}`, data),
  deleteUser: (userId) => http.delete(`/admin/users/${userId}`),

  // 电影管理
  listMovies: (params) => http.get('/admin/movies', { params }),
  updateMovie: (movieId, data) => http.patch(`/admin/movies/${movieId}`, data),
  deleteMovie: (movieId) => http.delete(`/admin/movies/${movieId}`),

  // 评分管理
  listRatings: (params) => http.get('/admin/ratings', { params }),
  deleteRating: (ratingId) => http.delete(`/admin/ratings/${ratingId}`),

  // 模型管理
  listModels: () => http.get('/admin/models'),
  activateModel: (version) => http.post(`/admin/models/${version}/activate`),
  deleteModel: (version) => http.delete(`/admin/models/${version}`),
}
