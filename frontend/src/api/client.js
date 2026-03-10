import axios from 'axios'

const BASE_URL = import.meta.env.VITE_API_URL || (
  import.meta.env.DEV ? 'http://localhost:8001' : ''
)

const api = axios.create({
  baseURL: BASE_URL,
  timeout: 120000,
})

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
  used: (client_id) => api.get('/api/domains/used', { params: { client_id } }),
}

export const publish = {
  generate: (data) => api.post('/api/publish/generate', data),
}

export const history = {
  list: (params = {}) => api.get('/api/history', { params }),
  stats: () => api.get('/api/history/stats'),
  delete: (id) => api.delete(`/api/history/${id}`),
}

export const dashboard = {
  stats: async () => {
    const today = new Date().toISOString().slice(0, 10)
    const [domainsRes, historyStatsRes, todayRes, clientsRes, autopilotRes] = await Promise.all([
      api.get('/api/domains'),
      api.get('/api/history/stats'),
      api.get('/api/history', { params: { limit: 500, offset: 0, status: 'published' } }),
      api.get('/api/clients'),
      api.get('/api/autopilot/stats').catch(() => ({ data: {} })),
    ])
    const todayPosts = (todayRes.data.posts || []).filter(p =>
      p.created_at && p.created_at.startsWith(today)
    ).length
    return {
      total_domains: domainsRes.data.length,
      total_clients: clientsRes.data.length,
      total_published: historyStatsRes.data.published || 0,
      posts_today: todayPosts,
      pending_keywords: autopilotRes.data.pending_keywords || 0,
      active_schedules: autopilotRes.data.active_schedules || 0,
    }
  }
}

export default api

export const topicalMap = {
  generate: (data) => api.post('/api/topical-map', data),
}

export const contentWriter = {
  generate: (data) => api.post('/api/content-writer/generate', data),
}
