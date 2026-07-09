import http from './index'

export const apiMovies = {
  list:   (params) => http.get('/movies/',     { params }),
  detail: (id)     => http.get('/movies/' + id),
  genres: ()       => http.get('/movies/genres'),
}
