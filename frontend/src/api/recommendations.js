import http from './index'

export const apiRecommendations = {
  get: () => http.get('/recommendations/'),
}
