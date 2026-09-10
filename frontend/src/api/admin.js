import client from './client'

export const apiAdmin = {
  // 统计数据
  getStats() {
    return client.get('/admin/stats').then(r => r.data)
  },

  // 用户管理
  listUsers(params) {
    return client.get('/admin/users', { params }).then(r => r.data)
  },
  createUser(data) {
    return client.post('/admin/users', data).then(r => r.data)
  },
  updateUser(userId, data) {
    return client.patch(`/admin/users/${userId}`, data).then(r => r.data)
  },
  deleteUser(userId) {
    return client.delete(`/admin/users/${userId}`).then(r => r.data)
  },

  // 电影管理
  listMovies(params) {
    return client.get('/admin/movies', { params }).then(r => r.data)
  },
  updateMovie(movieId, data) {
    return client.patch(`/admin/movies/${movieId}`, data).then(r => r.data)
  },
  deleteMovie(movieId) {
    return client.delete(`/admin/movies/${movieId}`).then(r => r.data)
  },

  // 评分管理
  listRatings(params) {
    return client.get('/admin/ratings', { params }).then(r => r.data)
  },
  deleteRating(ratingId) {
    return client.delete(`/admin/ratings/${ratingId}`).then(r => r.data)
  },

  // 模型管理
  listModels() {
    return client.get('/admin/models').then(r => r.data)
  },
  activateModel(version) {
    return client.post(`/admin/models/${version}/activate`).then(r => r.data)
  },
  deleteModel(version) {
    return client.delete(`/admin/models/${version}`).then(r => r.data)
  }
}
