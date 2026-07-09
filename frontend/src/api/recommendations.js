import client from './client'

export const recommendationsApi = {
  mine: () => client.get('/recommendations'),
}
