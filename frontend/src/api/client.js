import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_URL || (
  import.meta.env.DEV ? 'http://localhost:8001' : ''
)

const _authInterceptor = config => {
  const token = localStorage.getItem('pbn_auth_token')
  if (token) {
    config.headers = config.headers || {}
    config.headers['Authorization'] = `Basic ${token}`
  }
  return config
}

const api = axios.create({
  baseURL: BASE_URL,
  timeout: 30000, // 30s default for normal requests
})

// Long-running axios instance for article generation / bulk publish (up to 10 min)
export const apiLong = axios.create({
  baseURL: BASE_URL,
  timeout: 600000,
})

// Inject saved Basic Auth token on every request
api.interceptors.request.use(_authInterceptor)
apiLong.interceptors.request.use(_authInterceptor)

api.interceptors.response.use(
  res => res,
  err => Promise.reject(err)
)
apiLong.interceptors.response.use(
  res => res,
  err => Promise.reject(err)
)

export default api

export function setAuthCredentials(user, password) {
  const token = btoa(`${user}:${password}`)
  localStorage.setItem('pbn_auth_token', token)
}

export function clearAuthCredentials() {
  localStorage.removeItem('pbn_auth_token')
}

export function hasAuthToken() {
  return !!localStorage.getItem('pbn_auth_token')
}

// Login check — won't trigger logout interceptor
export function checkCredentials(user, password) {
  const token = btoa(`${user}:${password}`)
  return axios.get(`${BASE_URL}/api/projects`, {
    headers: { Authorization: `Basic ${token}` },
    timeout: 10000,
    _isLoginCheck: true,
  })
}

export const projects = {
  list: () => api.get('/api/projects'),
  create: (name) => api.post('/api/projects', { name }),
  delete: (id) => api.delete(`/api/projects/${id}`),
}

export const clients = {
  list: (project_id) => api.get('/api/clients', { params: { project_id } }),
  create: (project_id, name) => api.post('/api/clients', { project_id, name }),
  delete: (id) => api.delete(`/api/clients/${id}`),
  addDomain: (client_id, domain) =>
    api.post(`/api/clients/${client_id}/domains`, { domain }),
  deleteDomain: (client_id, domain_id) =>
    api.delete(`/api/clients/${client_id}/domains/${domain_id}`),
}

export const domains = {
  list: (params = {}) => api.get('/api/domains', { params }),
  servers: () => api.get('/api/domains/servers'),
  toggle: (id, active) => api.patch(`/api/domains/${id}`, { active }),
  patch: (id, data) => api.patch(`/api/domains/${id}`, data),
  delete: (id) => api.delete(`/api/domains/${id}`),
  bulkDelete: (domainNames) => api.delete('/api/domains/bulk-delete', { data: domainNames }),
  used: (client_id) => api.get('/api/domains/used', { params: { client_id } }),
}

export const bulkPublish = {
  importDomains: (data) => api.post('/api/domains/batch-import', data),
  listBatches: () => api.get('/api/domains/batches'),
  listByBatch: (batch_tag) => api.get('/api/domains', { params: { batch_tag } }),
}

export const publish = {
  generate: (data) => apiLong.post('/api/publish/generate', data),
  post: (data) => apiLong.post('/api/publish/post', data),
  postAsync: (data) => apiLong.post('/api/publish/post-async', data),
  checkDuplicate: (topic, domainId) => api.get('/api/publish/check-duplicate', { params: { topic, domain_id: domainId } }),
  pingSitemap: (data) => api.post('/api/publish/ping-sitemap', data),
  domainPosts: (domainId, limit = 20) => api.get(`/api/publish/domain-posts/${domainId}`, { params: { limit } }),
}

export const history = {
  list: (params = {}) => api.get('/api/history', { params }),
  stats: () => api.get('/api/history/stats'),
  delete: (id) => api.delete(`/api/history/${id}`),
  deleteBatch: (batchTag) => api.delete(`/api/history/batch/${encodeURIComponent(batchTag)}`),
  listBatchTags: () => api.get('/api/history/batches'),
  preview: (id) => api.get(`/api/history/${id}/preview`),
  domainStats: () => api.get('/api/history/domain-stats'),
  retryPost: (id) => api.post(`/api/history/${id}/retry-status`),
  retryAllFailed: () => api.post('/api/history/retry-all-failed'),
  failedCount: () => api.get('/api/history/failed-count'),
}

export const dashboard = {
  stats: () => api.get('/api/dashboard/stats').then(r => r.data),
}

export const topicalMap = {
  generate: (data) => apiLong.post('/api/topical-map', data),
}

export const contentWriter = {
  generate: (data) => apiLong.post('/api/content-writer/generate', data),
  serpCacheInfo: (keyword) => api.get('/api/content-writer/serp-cache-info', { params: { keyword } }),
  sendToAutopilot: (data) => api.post('/api/content-writer/send-to-autopilot', data),
}

export const analytics = {
  anchorDiversity: (clientDomain) => api.get('/api/analytics/anchor-diversity', { params: { client_domain: clientDomain } }),
  linkVelocity: () => api.get('/api/analytics/link-velocity'),
  contentUniqueness: (domainId) => api.get('/api/analytics/content-uniqueness', { params: { my_domain_id: domainId } }),
}

export const deindex = {
  scan: () => apiLong.post('/api/deindex/scan'),
  results: () => api.get('/api/deindex/results'),
}

export const notifications = {
  telegramTest: () => api.get('/api/notifications/telegram-test'),
  getSettings: () => api.get('/api/notifications/settings'),
  saveTelegram: (data) => api.post('/api/notifications/telegram-config', data),
  saveGptModel: (model) => api.post('/api/notifications/gpt-model', { model }),
  saveImageSource: (image_source) => api.post('/api/notifications/default-image-source', { image_source }),
  saveNotifyPrefs: (prefs) => api.post('/api/notifications/notify-prefs', prefs),
}
