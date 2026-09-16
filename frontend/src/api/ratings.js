import http from './index'

export const apiRatings = {
  submit: (movie_id, rating) => http.post('/ratings/',  { movie_id, rating }),
  // 评分是「一人一片一条」，用 movie_id 定位即可
  remove: (movie_id)         => http.delete('/ratings/' + movie_id),
}
