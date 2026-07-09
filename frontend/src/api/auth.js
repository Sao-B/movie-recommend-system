import http from './index'

export const apiAuth = {
  login:    (username, password) => http.post('/auth/login',    { username, password }),
  register: (username, password) => http.post('/auth/register', { username, password }),
}
