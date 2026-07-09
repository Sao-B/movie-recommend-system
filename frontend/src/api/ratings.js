import http from './index'

export const apiRatings = {
  submit: (movie_id, rating) => http.post('/ratings/',  { movie_id, rating }),
  mine:   ()                 => http.get('/ratings/me'),
}
