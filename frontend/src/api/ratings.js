import client from './client'

export const ratingsApi = {
  submit: (data) => client.post('/ratings', data),
  mine:   ()     => client.get('/ratings/me'),
}
