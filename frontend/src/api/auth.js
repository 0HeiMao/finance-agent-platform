import http from './axios'

export const login = payload => http.post('/auth/login', payload)
export const register = payload => http.post('/auth/register', payload)
