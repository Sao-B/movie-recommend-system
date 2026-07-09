import client from './client'

export const moviesApi = {
  list:   (params) => client.get('/movies', { params }),
  detail: (id)     => client.get(`/movies/${id}`),
  genres: ()       => client.get('/movies/genres'),
}
