import { useState, useEffect, useCallback } from 'react'
import { history as historyApi, clients as clientsApi } from '../api/client'
import api from '../api/client'
import { useToast } from '../components/Toast'

const STATUS_COLORS = {
  published: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  pending: 'bg-yellow-100 text-yellow-700',
}

export default function History() {
  const addToast = useToast()
  const [posts, setPosts] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [clients, setClients] = useState([])
  const [batchTags, setBatchTags] = useState([])
  const [stats, setStats] = useState({})
  const [loading, setLoading] = useState(true)
  const [clientFilter, setClientFilter] = useState('')
  const [statusFilter, setStatusFilter] = useState('')
  const [batchFilter, setBatchFilter] = useState('')
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [copied, setCopied] = useState(false)
  const [indexing, setIndexing] = useState(false)
  const [indexResult, setIndexResult] = useState(null)
  const LIMIT = 50

  const load = useCallback(async (newOffset = 0) => {
    setLoading(true)
    const params = { limit: LIMIT, offset: newOffset }
    if (clientFilter) params.client_id = clientFilter
    if (statusFilter) params.status = statusFilter
    if (batchFilter) params.batch_tag = batchFilter
    if (dateFrom) params.date_from = dateFrom
    if (dateTo) params.date_to = dateTo
    const [postsRes, statsRes, clientsRes, batchesRes] = await Promise.all([
      historyApi.list(params),
      historyApi.stats(),
      clientsApi.list(),
      historyApi.listBatchTags(),
    ])
    setPosts(postsRes.data.posts || [])
    setTotal(postsRes.data.total || 0)
    setOffset(newOffset)
    setStats(statsRes.data)
    setClients(clientsRes.data)
    setBatchTags(batchesRes.data || [])
    setLoading(false)
  }, [clientFilter, statusFilter, batchFilter, dateFrom, dateTo])

  useEffect(() => { load(0) }, [clientFilter, statusFilter, batchFilter, dateFrom, dateTo])

  const [retryingAll, setRetryingAll] = useState(false)
  const [retrying, setRetrying] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [preview, setPreview] = useState(null) // { id, title, content }
  const [previewLoading, setPreviewLoading] = useState(false)

  const openPreview = async (p) => {
    setPreview({ id: p.id, title: p.title, content: null })
    setPreviewLoading(true)
    try {
      const res = await historyApi.preview(p.id)
      setPreview({ id: p.id, title: p.title, content: res.data.content || '' })
    } catch {
      setPreview({ id: p.id, title: p.title, content: '<p>Brak podglądu — artykuł nie posiada zapisanej treści.</p>' })
    } finally {
      setPreviewLoading(false)
    }
  }

  const deletePost = async (id) => {
    if (!confirm('Usunąć ten wpis z historii?')) return
    await historyApi.delete(id)
    await load(offset)
  }

  const retryPost = async (id) => {
    setRetrying(id)
    try {
      await api.post(`/api/history/${id}/retry-status`)
      addToast('Post ponownie opublikowany!', 'success')
      await load(offset)
    } catch (e) {
      const msg = e?.response?.data?.detail || 'Błąd ponownej publikacji'
      addToast(msg, 'error')
    } finally {
      setRetrying(null)
    }
  }

  const copyAllUrls = () => {
    const urls = posts.filter(p => p.wp_post_url).map(p => p.wp_post_url).join('\n')
    navigator.clipboard.writeText(urls).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  const [exportingCsv, setExportingCsv] = useState(false)

  const exportCsv = async () => {
    setExportingCsv(true)
    try {
      // Fetch ALL records matching current filters (not just current page)
      const params = { limit: 5000, offset: 0 }
      if (clientFilter) params.client_id = clientFilter
      if (statusFilter) params.status = statusFilter
      if (batchFilter) params.batch_tag = batchFilter
      if (dateFrom) params.date_from = dateFrom
      if (dateTo) params.date_to = dateTo
      const res = await historyApi.list(params)
      const allPosts = res.data.posts || []
      const rows = [['Data', 'Klient', 'Domena klienta', 'Moja domena', 'Tytuł', 'Status', 'URL', 'Batch']]
      allPosts.forEach(p => {
        rows.push([
          p.created_at ? new Date(p.created_at).toLocaleString('pl-PL') : '',
          p.client_name || '',
          p.client_domain || '',
          p.my_domain || '',
          (p.title || '').replace(/"/g, '""'),
          p.status || '',
          p.wp_post_url || '',
          p.batch_tag || '',
        ])
      })
      const csv = rows.map(r => r.map(v => `"${v}"`).join(',')).join('\n')
      const blob = new Blob(['\uFEFF' + csv], { type: 'text/csv;charset=utf-8;' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `historia_${new Date().toISOString().slice(0, 10)}.csv`
      a.click()
      URL.revokeObjectURL(url)
    } finally {
      setExportingCsv(false)
    }
  }

  const submitToIndexer = async () => {
    const urls = posts.filter(p => p.wp_post_url).map(p => p.wp_post_url)
    if (urls.length === 0) return
    setIndexing(true)
    setIndexResult(null)
    try {
      const resp = await api.post('/api/history/indexer/submit', { urls })
      const data = resp.data
      if (data.success) {
        setIndexResult({ ok: true, msg: `Wysłano ${data.data?.submitted ?? urls.length} URL-i. Pozostałe kredyty: ${data.data?.credits_remaining ?? '?'}` })
      } else {
        setIndexResult({ ok: false, msg: data.message || 'Błąd API' })
      }
    } catch (e) {
      setIndexResult({ ok: false, msg: 'Błąd połączenia z indexerem' })
    } finally {
      setIndexing(false)
      setTimeout(() => setIndexResult(null), 5000)
    }
  }

  const formatDate = (str) => {
    if (!str) return '—'
    return new Date(str).toLocaleString('pl-PL', {
      day: '2-digit', month: '2-digit', year: 'numeric',
      hour: '2-digit', minute: '2-digit',
    })
  }

  const q = searchQuery.toLowerCase()
  const filteredPosts = q
    ? posts.filter(p =>
        (p.title || '').toLowerCase().includes(q) ||
        (p.wp_post_url || '').toLowerCase().includes(q) ||
        (p.client_domain || '').toLowerCase().includes(q) ||
        (p.my_domain || '').toLowerCase().includes(q)
      )
    : posts

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
      <div className="flex gap-3 mb-4 flex-wrap">
        <input
          type="text"
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          placeholder="Szukaj po tytule, URL, domenie..."
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 min-w-[220px]"
        />
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
        {batchTags.length > 0 && (
          <select
            value={batchFilter}
            onChange={(e) => setBatchFilter(e.target.value)}
            className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-purple-500"
          >
            <option value="">Wszystkie batche</option>
            {batchTags.map(tag => (
              <option key={tag} value={tag}>{tag}</option>
            ))}
          </select>
        )}
        <input
          type="date"
          value={dateFrom}
          onChange={e => setDateFrom(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          title="Data od"
        />
        <input
          type="date"
          value={dateTo}
          onChange={e => setDateTo(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          title="Data do"
        />
        {(dateFrom || dateTo) && (
          <button
            onClick={() => { setDateFrom(''); setDateTo('') }}
            className="px-3 py-2 text-xs text-gray-500 hover:text-gray-800 border border-gray-200 rounded-lg"
          >
            Wyczyść daty
          </button>
        )}
        <button
          onClick={() => load(offset)}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
        >
          Odśwież
        </button>
        {(stats.failed || 0) > 0 && (
          <button
            onClick={async () => {
              if (!confirm(`Ponowić publikację ${stats.failed} nieudanych postów?`)) return
              setRetryingAll(true)
              try {
                const res = await api.post('/api/history/retry-all-failed')
                addToast(`Ponowiono: ${res.data.succeeded} OK, ${res.data.failed} błędów z ${res.data.retried}`, res.data.succeeded > 0 ? 'success' : 'error')
                await load(offset)
              } catch (e) {
                addToast(e.response?.data?.detail || 'Błąd ponownej publikacji', 'error')
              } finally {
                setRetryingAll(false)
              }
            }}
            disabled={retryingAll}
            className="px-4 py-2 bg-red-600 text-white rounded-lg text-sm hover:bg-red-700 disabled:opacity-40 flex items-center gap-2"
          >
            {retryingAll ? (
              <><span className="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full" />Ponawiam...</>
            ) : `Retry all failed (${stats.failed})`}
          </button>
        )}
        <button
          onClick={copyAllUrls}
          disabled={posts.filter(p => p.wp_post_url).length === 0}
          className="px-4 py-2 bg-gray-700 text-white rounded-lg text-sm hover:bg-gray-800 disabled:opacity-40"
        >
          {copied ? 'Skopiowano!' : `Kopiuj URL-e (${posts.filter(p => p.wp_post_url).length})`}
        </button>
        <button
          onClick={exportCsv}
          disabled={exportingCsv || posts.length === 0}
          className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700 disabled:opacity-40 flex items-center gap-2"
        >
          {exportingCsv ? (
            <><span className="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full" />Eksportowanie...</>
          ) : 'Eksport CSV'}
        </button>
        <button
          onClick={submitToIndexer}
          disabled={indexing || posts.filter(p => p.wp_post_url).length === 0}
          className="px-4 py-2 bg-orange-500 text-white rounded-lg text-sm hover:bg-orange-600 disabled:opacity-40 flex items-center gap-2"
        >
          {indexing ? (
            <><span className="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full" />Wysyłanie...</>
          ) : (
            `Prześlij do indexera (${posts.filter(p => p.wp_post_url).length})`
          )}
        </button>
        {indexResult && (
          <span className={`px-3 py-2 rounded-lg text-sm font-medium ${indexResult.ok ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
            {indexResult.msg}
          </span>
        )}
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
        {loading ? (
          <div className="flex items-center justify-center h-40">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
          </div>
        ) : filteredPosts.length === 0 ? (
          <div className="flex items-center justify-center h-40 text-gray-400">
            Brak wyników{searchQuery ? ` dla "${searchQuery}"` : ''}
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
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Slow</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Status</th>
                  <th className="text-left px-4 py-3 font-semibold text-gray-600">Link</th>
                  <th className="px-4 py-3"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {filteredPosts.map((p) => (
                  <tr key={p.id} className="hover:bg-gray-50">
                    <td className="px-4 py-3 text-gray-500 whitespace-nowrap">{formatDate(p.created_at)}</td>
                    <td className="px-4 py-3 font-medium text-gray-800">{p.client_name || '—'}</td>
                    <td className="px-4 py-3 text-gray-600 max-w-[120px] truncate">{p.client_domain}</td>
                    <td className="px-4 py-3 text-gray-600 max-w-[120px] truncate">{p.my_domain || '—'}</td>
                    <td className="px-4 py-3 text-gray-800 max-w-[200px]">
                      <button
                        onClick={() => openPreview(p)}
                        className="truncate block text-left text-blue-700 hover:underline cursor-pointer"
                        title={p.title}
                      >
                        {p.title}
                      </button>
                    </td>
                    <td className="px-4 py-3 text-gray-500 text-xs tabular-nums">{p.word_count || '—'}</td>
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
                    <td className="px-4 py-3 flex items-center gap-2">
                      {p.status === 'failed' && (
                        <button
                          onClick={() => retryPost(p.id)}
                          disabled={retrying === p.id}
                          className="text-blue-500 hover:text-blue-700 text-xs disabled:opacity-40"
                          title="Ponów"
                        >
                          {retrying === p.id ? '...' : '↻'}
                        </button>
                      )}
                      <button
                        onClick={() => deletePost(p.id)}
                        className="text-red-400 hover:text-red-600 text-xs"
                        title="Usuń"
                      >
                        ✕
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <div className="flex items-center justify-between px-4 py-3 border-t border-gray-100 dark:border-gray-700 text-sm text-gray-600 dark:text-gray-400">
          <span>Strona {Math.floor(offset / LIMIT) + 1} z {Math.max(1, Math.ceil(total / LIMIT))} ({total} wpisow)</span>
          <div className="flex items-center gap-3">
            <button
              disabled={offset === 0}
              onClick={() => load(offset - LIMIT)}
              className="px-3 py-1.5 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-40"
            >
              ← Poprzednia
            </button>
            <span>{offset + 1}–{Math.min(offset + LIMIT, total)} / {total}</span>
            <button
              disabled={offset + LIMIT >= total}
              onClick={() => load(offset + LIMIT)}
              className="px-3 py-1.5 rounded border border-gray-300 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-40"
            >
              Następna →
            </button>
          </div>
        </div>
      </div>
      {preview && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={() => setPreview(null)}>
          <div
            className="bg-white rounded-xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col mx-4"
            onClick={e => e.stopPropagation()}
          >
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-200">
              <h3 className="font-semibold text-gray-900 text-lg truncate pr-4">{preview.title}</h3>
              <button onClick={() => setPreview(null)} className="text-gray-400 hover:text-gray-700 text-xl font-bold shrink-0">✕</button>
            </div>
            <div className="overflow-y-auto flex-1 px-6 py-4">
              {previewLoading ? (
                <div className="flex items-center justify-center h-40">
                  <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
                </div>
              ) : (
                <div
                  className="text-gray-800 text-sm leading-relaxed [&_h1]:text-2xl [&_h1]:font-bold [&_h1]:mb-3 [&_h2]:text-xl [&_h2]:font-semibold [&_h2]:mb-2 [&_h3]:text-lg [&_h3]:font-semibold [&_h3]:mb-2 [&_p]:mb-3 [&_ul]:list-disc [&_ul]:pl-5 [&_ul]:mb-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_ol]:mb-3 [&_li]:mb-1 [&_strong]:font-semibold [&_a]:text-blue-600 [&_a]:underline"
                  dangerouslySetInnerHTML={{ __html: preview.content || '' }}
                />
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
