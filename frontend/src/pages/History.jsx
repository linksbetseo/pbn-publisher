import { useState, useEffect } from 'react'
import { history as historyApi, clients as clientsApi } from '../api/client'

const STATUS_COLORS = {
  published: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  pending: 'bg-yellow-100 text-yellow-700',
}

export default function History() {
  const [posts, setPosts] = useState([])
  const [clients, setClients] = useState([])
  const [stats, setStats] = useState({})
  const [loading, setLoading] = useState(true)
  const [clientFilter, setClientFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [copied, setCopied] = useState(false)

  const load = async () => {
    setLoading(true)
    const params = {}
    if (clientFilter) params.client_id = clientFilter
    if (statusFilter) params.status = statusFilter
    const [postsRes, statsRes, clientsRes] = await Promise.all([
      historyApi.list(params),
      historyApi.stats(),
      clientsApi.list(),
    ])
    setPosts(postsRes.data)
    setStats(statsRes.data)
    setClients(clientsRes.data)
    setLoading(false)
  }

  useEffect(() => { load() }, [clientFilter, statusFilter])

  const copyAllUrls = () => {
    const urls = posts.filter(p => p.wp_post_url).map(p => p.wp_post_url).join('\n')
    navigator.clipboard.writeText(urls).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  const formatDate = (str) => {
    if (!str) return '—'
    return new Date(str).toLocaleString('pl-PL', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  return (
    <div className="p-8">
      <h2 className="text-2xl font-bold text-gray-900 mb-2">Historia publikacji</h2>
      <p className="text-gray-500 mb-6">Log wszystkich opublikowanych artykułów</p>

      {/* Stats */}
      <div className="flex gap-4 mb-6">
        {[
          { label: 'Opublikowane', key: 'published', color: 'text-green-600 bg-green-50' },
          { label: 'Błędy', key: 'failed', color: 'text-red-600 bg-red-50' },
          { label: 'Oczekujące', key: 'pending', color: 'text-yellow-600 bg-yellow-50' },
        ].map(s => (
          <div key={s.key} className={`px-4 py-2 rounded-lg text-sm font-medium ${s.color}`}>
            {s.label}: <strong>{stats[s.key] || 0}</strong>
          </div>
        ))}
        <div className="px-4 py-2 rounded-lg text-sm font-medium text-gray-600 bg-gray-100">
          Łącznie: <strong>{Object.values(stats).reduce((a, b) => a + b, 0)}</strong>
        </div>
      </div>

      {/* Filters */}
      <div className="flex gap-3 mb-4">
        <select
          value={clientFilter}
          onChange={(e) => setClientFilter(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Wszyscy klienci</option>
          {clients.map(c => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">Wszystkie statusy</option>
          <option value="published">Opublikowane</option>
          <option value="failed">Błędy</option>
          <option value="pending">Oczekujące</option>
        </select>
        <button
          onClick={load}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
        >
          Odśwież
        </button>
        <button
          onClick={copyAllUrls}
          disabled={posts.filter(p => p.wp_post_url).length === 0}
          className="px-4 py-2 bg-gray-700 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-40"
        >
          {copied ? 'Skopiowano!' : `Kopiuj URL-e (${posts.filter(p => p.wp_post_url).length})`}
        </button>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-40">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
          </div>
        ) : posts.length === 0 ? (
          <div className="flex items-center justify-center h-40 text-gray-400">
            Brak wyników
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-200">
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Data</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Klient</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Domena klienta</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Moja domena</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Tytuł</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Status</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Link</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {posts.map((p) => (
                  <tr key={p.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 text-gray-500 whitespace-nowrap">{formatDate(p.created_at)}</td>
                    <td className="px-4 py-3 font-medium text-gray-800">{p.client_name || '—'}</td>
                    <td className="px-4 py-3 text-gray-600 max-w-[120px] truncate">{p.client_domain}</td>
                    <td className="px-4 py-3 text-gray-600 max-w-[120px] truncate">{p.my_domain || '—'}</td>
                    <td className="px-4 py-3 text-gray-800 max-w-[200px]">
                      <span className="truncate block" title={p.title}>{p.title}</span>
                    </td>
                    <td className="px-4 py-3">
                      <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${STATUS_COLORS[p.status] || 'bg-gray-100 text-gray-600'}`}>
                        {p.status}
                      </span>
                    </td>
                    <td className="px-4 py-3">
                      {p.wp_post_url ? (
                        <a
                          href={p.wp_post_url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-blue-600 hover:underline text-xs"
                        >
                          Zobacz post
                        </a>
                      ) : (
                        <span className="text-gray-300 text-xs">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  )
}
