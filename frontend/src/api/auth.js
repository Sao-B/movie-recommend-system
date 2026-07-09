import client from './client'

export const authApi = {
  login:    (data) => client.post('/auth/login', data),
  register: (data) => client.post('/auth/register', data),
}
