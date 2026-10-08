import axios from 'axios'

const http = axios.create({ baseURL: import.meta.env.VITE_API_BASE || '/api/v1', timeout: 15000 })
http.interceptors.request.use(config => {
  const token = localStorage.getItem('access_token')
  if (token) config.headers.Authorization = `Bearer ${token}`
  return config
})
http.interceptors.response.use(response => response, error => {
  const isAuthRequest = /^\/auth\/(login|register)$/.test(error.config?.url || '')
  if (error.response?.status === 401 && !isAuthRequest) {
    localStorage.removeItem('access_token')
    window.location.assign('/login')
  }
  return Promise.reject(error)
})
export default http
