import { useState, useEffect, useCallback } from 'react'
import api from '../api/client'

const TABS = ['Portale', 'Kolejka Review', 'Opublikowane', 'Statystyki']

// ── Helpers ──────────────────────────────────────────────────────────────────

function Spinner({ className = 'w-5 h-5' }) {
  return (
    <svg className={`animate-spin ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

function Badge({ children, color = 'blue' }) {
  const map = {
    blue: 'bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-300',
    green: 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-300',
    yellow: 'bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-300',
    red: 'bg-red-100 dark:bg-red-900/30 text-red-700 dark:text-red-300',
    gray: 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300',
    purple: 'bg-purple-100 dark:bg-purple-900/30 text-purple-700 dark:text-purple-300',
    orange: 'bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-300',
  }
  return (
    <span className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${map[color] || map.gray}`}>
      {children}
    </span>
  )
}

function ConfirmDialog({ title, message, onConfirm, onCancel }) {
  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-100 dark:border-gray-700 p-6 w-full max-w-sm">
        <h3 className="text-lg font-semibold text-gray-900 dark:text-white mb-2">{title}</h3>
        <p className="text-sm text-gray-600 dark:text-gray-400 mb-6">{message}</p>
        <div className="flex justify-end gap-3">
          <button onClick={onCancel}
            className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors">
            Anuluj
          </button>
          <button onClick={onConfirm}
            className="px-4 py-2 text-sm bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors">
            Usuń
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Niche color mapping ──────────────────────────────────────────────────────

function nicheColor(niche) {
  if (!niche) return 'gray'
  const n = niche.toLowerCase()
  if (n.includes('polity')) return 'red'
  if (n.includes('sport')) return 'green'
  if (n.includes('tech')) return 'blue'
  if (n.includes('biznes') || n.includes('finans')) return 'orange'
  if (n.includes('kultu') || n.includes('rozryw')) return 'purple'
  return 'gray'
}

// ── Modal: Add/Edit Portal ───────────────────────────────────────────────────

function PortalFormModal({ portal, domains, onSave, onClose }) {
  const isEdit = !!portal?.id
  const [form, setForm] = useState({
    name: portal?.name || '',
    my_domain_id: portal?.my_domain_id || '',
    niche: portal?.niche || '',
    language: portal?.language || 'pl',
    editorial_prompt: portal?.editorial_prompt || '',
    posts_per_day: portal?.posts_per_day ?? 5,
    check_interval_min: portal?.check_interval_min ?? 30,
    auto_publish: portal?.auto_publish ?? false,
  })
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const handleChange = (key, value) => setForm(f => ({ ...f, [key]: value }))

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!form.name.trim()) { setError('Nazwa portalu jest wymagana'); return }
    setSaving(true)
    setError('')
    try {
      const payload = {
        ...form,
        name: form.name.trim(),
        niche: form.niche.trim(),
        editorial_prompt: form.editorial_prompt.trim(),
        my_domain_id: form.my_domain_id ? Number(form.my_domain_id) : null,
        posts_per_day: Number(form.posts_per_day) || 5,
        check_interval_min: Number(form.check_interval_min) || 30,
      }
      if (isEdit) {
        await api.put(`/api/news-portals/${portal.id}`, payload)
      } else {
        await api.post('/api/news-portals/', payload)
      }
      onSave()
    } catch (err) {
      setError(err.response?.data?.detail || 'Błąd zapisu portalu')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-100 dark:border-gray-700 w-full max-w-lg max-h-[90vh] overflow-y-auto">
        <div className="p-6 border-b border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">
            {isEdit ? 'Edytuj portal' : 'Dodaj portal'}
          </h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>
        <form onSubmit={handleSubmit} className="p-6 space-y-4">
          {/* Name */}
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Nazwa portalu</label>
            <input type="text" value={form.name} onChange={e => handleChange('name', e.target.value)}
              placeholder="np. Wiadomości Techniczne"
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>

          {/* Domain */}
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Domena</label>
            <select value={form.my_domain_id} onChange={e => handleChange('my_domain_id', e.target.value)}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500">
              <option value="">-- Wybierz domenę --</option>
              {domains.map(d => (
                <option key={d.id} value={d.id}>{d.domain}</option>
              ))}
            </select>
          </div>

          {/* Niche + Language row */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Nisza</label>
              <input type="text" value={form.niche} onChange={e => handleChange('niche', e.target.value)}
                placeholder="np. polityka, sport, technologia"
                className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Język</label>
              <select value={form.language} onChange={e => handleChange('language', e.target.value)}
                className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500">
                <option value="pl">Polski</option>
                <option value="en">English</option>
              </select>
            </div>
          </div>

          {/* Posts per day + Check interval */}
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Artykułów / dzień</label>
              <input type="number" min="1" max="50" value={form.posts_per_day}
                onChange={e => handleChange('posts_per_day', e.target.value)}
                className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Interwał sprawdzania (min)</label>
              <input type="number" min="5" max="1440" value={form.check_interval_min}
                onChange={e => handleChange('check_interval_min', e.target.value)}
                className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
          </div>

          {/* Editorial prompt */}
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Linia redakcyjna / System prompt</label>
            <textarea rows={4} value={form.editorial_prompt}
              onChange={e => handleChange('editorial_prompt', e.target.value)}
              placeholder="Opisz ton, styl i zakres tematyczny portalu..."
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none" />
          </div>

          {/* Auto publish */}
          <label className="flex items-center gap-2 cursor-pointer">
            <input type="checkbox" checked={form.auto_publish}
              onChange={e => handleChange('auto_publish', e.target.checked)}
              className="w-4 h-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500" />
            <span className="text-sm text-gray-700 dark:text-gray-300">Auto-publikacja (bez review)</span>
          </label>

          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}

          <div className="flex justify-end gap-3 pt-2">
            <button type="button" onClick={onClose}
              className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors">
              Anuluj
            </button>
            <button type="submit" disabled={saving}
              className="px-4 py-2 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors disabled:opacity-50 flex items-center gap-2">
              {saving && <Spinner className="w-4 h-4" />}
              {isEdit ? 'Zapisz zmiany' : 'Utwórz portal'}
            </button>
          </div>
        </form>
      </div>
    </div>
  )
}

// ── Modal: Sources ───────────────────────────────────────────────────────────

function SourcesModal({ portal, onClose, onUpdated }) {
  const [sources, setSources] = useState([])
  const [loading, setLoading] = useState(true)
  const [newName, setNewName] = useState('')
  const [newUrl, setNewUrl] = useState('')
  const [newType, setNewType] = useState('rss')
  const [adding, setAdding] = useState(false)
  const [toggling, setToggling] = useState(null)
  const [error, setError] = useState('')
  // RSS Catalog state
  const [showCatalog, setShowCatalog] = useState(false)
  const [catalog, setCatalog] = useState([])
  const [catalogLoading, setCatalogLoading] = useState(false)
  const [catalogCategory, setCatalogCategory] = useState('all')
  const [catalogSelected, setCatalogSelected] = useState([])
  const [bulkAdding, setBulkAdding] = useState(false)

  const loadSources = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.get(`/api/news-portals/${portal.id}/sources`)
      setSources(res.data || [])
    } catch {
      setSources([])
    } finally {
      setLoading(false)
    }
  }, [portal.id])

  useEffect(() => { loadSources() }, [loadSources])

  const loadCatalog = useCallback(async () => {
    setCatalogLoading(true)
    try {
      const res = await api.get('/api/news-portals/rss-catalog')
      setCatalog(res.data || [])
    } catch {
      setCatalog([])
    } finally {
      setCatalogLoading(false)
    }
  }, [])

  const handleOpenCatalog = () => {
    setShowCatalog(true)
    if (catalog.length === 0) loadCatalog()
  }

  const handleAdd = async () => {
    if (!newUrl.trim()) { setError('URL jest wymagany'); return }
    setAdding(true)
    setError('')
    try {
      await api.post(`/api/news-portals/${portal.id}/sources`, {
        name: newName.trim() || newUrl.trim(),
        url: newUrl.trim(),
        source_type: newType,
      })
      setNewName('')
      setNewUrl('')
      setNewType('rss')
      await loadSources()
      onUpdated()
    } catch (err) {
      setError(err.response?.data?.detail || 'Błąd dodawania źródła')
    } finally {
      setAdding(false)
    }
  }

  const handleDelete = async (sourceId) => {
    try {
      await api.delete(`/api/news-portals/sources/${sourceId}`)
      await loadSources()
      onUpdated()
    } catch {}
  }

  const handleToggle = async (source) => {
    setToggling(source.id)
    try {
      await api.put(`/api/news-portals/sources/${source.id}`, { active: !source.active })
      await loadSources()
      onUpdated()
    } catch {}
    setToggling(null)
  }

  const toggleCatalogFeed = (feed) => {
    setCatalogSelected(prev => {
      const exists = prev.find(f => f.url === feed.url)
      if (exists) return prev.filter(f => f.url !== feed.url)
      return [...prev, feed]
    })
  }

  const selectAllFromPortal = (portalFeeds) => {
    setCatalogSelected(prev => {
      const existingUrls = new Set(prev.map(f => f.url))
      const newFeeds = portalFeeds.filter(f => !existingUrls.has(f.url))
      return [...prev, ...newFeeds]
    })
  }

  const handleBulkAdd = async () => {
    if (catalogSelected.length === 0) return
    setBulkAdding(true)
    setError('')
    try {
      const res = await api.post(`/api/news-portals/${portal.id}/sources/bulk-add`, catalogSelected)
      setCatalogSelected([])
      setShowCatalog(false)
      await loadSources()
      onUpdated()
    } catch (err) {
      setError(err.response?.data?.detail || 'Błąd dodawania źródeł')
    } finally {
      setBulkAdding(false)
    }
  }

  const existingUrls = new Set(sources.map(s => s.url))

  const catalogCategories = ['all', ...new Set(catalog.map(c => c.category))]
  const filteredCatalog = catalogCategory === 'all' ? catalog : catalog.filter(c => c.category === catalogCategory)

  const categoryLabels = {
    'all': 'Wszystkie', 'ogólne': 'Ogólne', 'wiadomości': 'Wiadomości',
    'technologia': 'Technologia', 'finanse': 'Finanse', 'biznes': 'Biznes',
    'sport': 'Sport', 'zdrowie': 'Zdrowie', 'motoryzacja': 'Motoryzacja',
    'budownictwo': 'Budownictwo', 'kultura': 'Kultura', 'international': 'Międzynarodowe',
  }

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-100 dark:border-gray-700 w-full max-w-3xl max-h-[85vh] flex flex-col">
        <div className="p-6 border-b border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <div>
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Źródła — {portal.name}</h3>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">{sources.length} źródeł</p>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={handleOpenCatalog}
              className={`px-3 py-1.5 text-xs font-medium rounded-lg transition-colors flex items-center gap-1.5 ${
                showCatalog
                  ? 'bg-orange-100 dark:bg-orange-900/30 text-orange-700 dark:text-orange-300'
                  : 'bg-orange-500 hover:bg-orange-600 text-white'
              }`}>
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z" />
              </svg>
              {showCatalog ? 'Ukryj katalog' : 'Katalog RSS'}
            </button>
            <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
              <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          {/* RSS Catalog Browser */}
          {showCatalog && (
            <div className="mb-6 border border-orange-200 dark:border-orange-800 rounded-xl overflow-hidden">
              <div className="bg-orange-50 dark:bg-orange-900/20 p-4">
                <div className="flex items-center justify-between mb-3">
                  <p className="text-sm font-semibold text-orange-800 dark:text-orange-300">Katalog portali RSS — kliknij żeby dodać</p>
                  {catalogSelected.length > 0 && (
                    <button onClick={handleBulkAdd} disabled={bulkAdding}
                      className="px-3 py-1.5 bg-green-600 hover:bg-green-700 text-white rounded-lg text-xs font-medium transition-colors disabled:opacity-50 flex items-center gap-1">
                      {bulkAdding ? <Spinner className="w-3 h-3" /> : null}
                      Dodaj zaznaczone ({catalogSelected.length})
                    </button>
                  )}
                </div>
                {/* Category pills */}
                <div className="flex flex-wrap gap-1.5">
                  {catalogCategories.map(cat => (
                    <button key={cat} onClick={() => setCatalogCategory(cat)}
                      className={`px-2.5 py-1 rounded-full text-xs font-medium transition-colors ${
                        catalogCategory === cat
                          ? 'bg-orange-500 text-white'
                          : 'bg-white dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-orange-100 dark:hover:bg-gray-600'
                      }`}>
                      {categoryLabels[cat] || cat}
                    </button>
                  ))}
                </div>
              </div>
              <div className="max-h-72 overflow-y-auto">
                {catalogLoading ? (
                  <div className="flex justify-center py-6"><Spinner /></div>
                ) : filteredCatalog.length === 0 ? (
                  <p className="text-center text-gray-400 text-sm py-6">Brak portali w tej kategorii</p>
                ) : (
                  <div className="divide-y divide-gray-100 dark:divide-gray-700">
                    {filteredCatalog.map((portalItem, idx) => (
                      <div key={idx} className="p-3">
                        <div className="flex items-center justify-between mb-2">
                          <div className="flex items-center gap-2">
                            <span className="text-sm font-semibold text-gray-800 dark:text-gray-200">{portalItem.portal}</span>
                            <Badge color="gray">{categoryLabels[portalItem.category] || portalItem.category}</Badge>
                          </div>
                          <button onClick={() => selectAllFromPortal(portalItem.feeds)}
                            className="text-xs text-orange-600 dark:text-orange-400 hover:underline">
                            Zaznacz wszystkie
                          </button>
                        </div>
                        <div className="flex flex-wrap gap-1.5">
                          {portalItem.feeds.map((feed, fi) => {
                            const alreadyAdded = existingUrls.has(feed.url)
                            const isSelected = catalogSelected.some(f => f.url === feed.url)
                            return (
                              <button key={fi}
                                onClick={() => !alreadyAdded && toggleCatalogFeed(feed)}
                                disabled={alreadyAdded}
                                className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all ${
                                  alreadyAdded
                                    ? 'bg-green-100 dark:bg-green-900/30 text-green-600 dark:text-green-400 cursor-default opacity-60'
                                    : isSelected
                                      ? 'bg-orange-500 text-white ring-2 ring-orange-300'
                                      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-orange-100 dark:hover:bg-orange-800/30'
                                }`}>
                                {alreadyAdded ? '✓ ' : isSelected ? '● ' : ''}{feed.name}
                              </button>
                            )
                          })}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )}

          {/* Existing sources list */}
          {loading ? (
            <div className="flex justify-center py-8"><Spinner /></div>
          ) : sources.length === 0 && !showCatalog ? (
            <p className="text-center text-gray-400 dark:text-gray-500 text-sm py-8">Brak źródeł. Kliknij "Katalog RSS" żeby dodać z listy portali.</p>
          ) : sources.length === 0 ? null : (
            <div className="space-y-2">
              {sources.map(s => (
                <div key={s.id} className="flex items-center gap-3 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                  <div className="min-w-0 flex-1">
                    <p className="text-sm font-medium text-gray-800 dark:text-gray-200 truncate">{s.name}</p>
                    <p className="text-xs text-gray-400 dark:text-gray-500 truncate">{s.url}</p>
                  </div>
                  <Badge color={s.source_type === 'rss' ? 'blue' : 'purple'}>{s.source_type}</Badge>
                  <button
                    onClick={() => handleToggle(s)}
                    disabled={toggling === s.id}
                    className={`relative w-10 h-5 rounded-full transition-colors ${s.active ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'}`}>
                    <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform shadow-sm ${s.active ? 'translate-x-5' : ''}`} />
                  </button>
                  <button onClick={() => handleDelete(s.id)}
                    className="text-red-400 hover:text-red-600 p-1 flex-shrink-0">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                    </svg>
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Add source form */}
        <div className="p-6 border-t border-gray-100 dark:border-gray-700">
          <p className="text-xs font-medium text-gray-600 dark:text-gray-400 uppercase mb-3">Dodaj źródło ręcznie</p>
          <div className="grid grid-cols-12 gap-2">
            <input type="text" placeholder="Nazwa (opcjonalnie)" value={newName}
              onChange={e => setNewName(e.target.value)}
              className="col-span-3 border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            <input type="url" placeholder="https://..." value={newUrl}
              onChange={e => setNewUrl(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && handleAdd()}
              className="col-span-5 border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
            <select value={newType} onChange={e => setNewType(e.target.value)}
              className="col-span-2 border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500">
              <option value="rss">RSS</option>
              <option value="web">Web</option>
            </select>
            <button onClick={handleAdd} disabled={adding}
              className="col-span-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors disabled:opacity-50 flex items-center justify-center gap-1">
              {adding ? <Spinner className="w-4 h-4" /> : (
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
                </svg>
              )}
              Dodaj
            </button>
          </div>
          {error && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{error}</p>}
        </div>
      </div>
    </div>
  )
}

// ── Modal: Preview Draft ─────────────────────────────────────────────────────

function PreviewDraftModal({ draft, onClose, onApprove, onReject, onEdit }) {
  const [approving, setApproving] = useState(false)
  const [rejecting, setRejecting] = useState(false)

  const handleApprove = async () => {
    setApproving(true)
    await onApprove(draft.id)
    setApproving(false)
  }

  const handleReject = async () => {
    setRejecting(true)
    await onReject(draft.id)
    setRejecting(false)
  }

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-100 dark:border-gray-700 w-full max-w-3xl max-h-[90vh] flex flex-col">
        <div className="p-6 border-b border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <div className="min-w-0 flex-1 mr-4">
            <h3 className="text-lg font-semibold text-gray-900 dark:text-white truncate">{draft.title}</h3>
            <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
              Portal: {draft.portal_name || '—'} | Utworzono: {draft.created_at?.slice(0, 16).replace('T', ' ') || '—'}
            </p>
          </div>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 flex-shrink-0">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6">
          {/* Source URLs */}
          {(() => {
            let urls = draft.source_urls
            if (typeof urls === 'string') { try { urls = JSON.parse(urls) } catch { urls = [] } }
            if (!Array.isArray(urls) || urls.length === 0) return null
            return (
              <div className="mb-4 p-3 bg-gray-50 dark:bg-gray-700/50 rounded-lg">
                <p className="text-xs font-medium text-gray-500 dark:text-gray-400 uppercase mb-2">Źródła</p>
                <div className="space-y-1">
                  {urls.map((url, i) => (
                    <a key={i} href={url} target="_blank" rel="noopener noreferrer"
                      className="block text-xs text-blue-600 dark:text-blue-400 hover:underline truncate">{url}</a>
                  ))}
                </div>
              </div>
            )
          })()}

          {/* Content */}
          <div className="prose prose-sm dark:prose-invert max-w-none"
            dangerouslySetInnerHTML={{ __html: (draft.content || '<p class="text-gray-400">Brak treści</p>').replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, '') }} />
        </div>

        <div className="p-6 border-t border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <button onClick={onClose}
            className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors">
            Zamknij
          </button>
          <div className="flex gap-2">
            <button onClick={handleReject} disabled={rejecting}
              className="px-4 py-2 text-sm bg-red-600 hover:bg-red-700 text-white rounded-lg transition-colors disabled:opacity-50 flex items-center gap-1">
              {rejecting && <Spinner className="w-4 h-4" />}
              Odrzuć
            </button>
            <button onClick={() => onEdit(draft)}
              className="px-4 py-2 text-sm bg-gray-600 hover:bg-gray-700 text-white rounded-lg transition-colors">
              Edytuj
            </button>
            <button onClick={handleApprove} disabled={approving}
              className="px-4 py-2 text-sm bg-green-600 hover:bg-green-700 text-white rounded-lg transition-colors disabled:opacity-50 flex items-center gap-1">
              {approving && <Spinner className="w-4 h-4" />}
              Zatwierdź i publikuj
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}

// ── Modal: Edit Draft ────────────────────────────────────────────────────────

function EditDraftModal({ draft, onClose, onSaved }) {
  const [title, setTitle] = useState(draft.title || '')
  const [content, setContent] = useState(draft.content || '')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')

  const handleSave = async () => {
    if (!title.trim()) { setError('Tytuł jest wymagany'); return }
    setSaving(true)
    setError('')
    try {
      await api.put(`/api/news-portals/drafts/${draft.id}`, { title: title.trim(), content })
      onSaved()
    } catch (err) {
      setError(err.response?.data?.detail || 'Błąd zapisu')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm z-50 flex items-center justify-center p-4">
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-100 dark:border-gray-700 w-full max-w-3xl max-h-[90vh] flex flex-col">
        <div className="p-6 border-b border-gray-100 dark:border-gray-700 flex items-center justify-between">
          <h3 className="text-lg font-semibold text-gray-900 dark:text-white">Edytuj artykuł</h3>
          <button onClick={onClose} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300">
            <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-6 space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Tytuł</label>
            <input type="text" value={title} onChange={e => setTitle(e.target.value)}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Treść (HTML)</label>
            <textarea rows={16} value={content} onChange={e => setContent(e.target.value)}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none font-mono" />
          </div>
          {error && <p className="text-sm text-red-600 dark:text-red-400">{error}</p>}
        </div>

        <div className="p-6 border-t border-gray-100 dark:border-gray-700 flex justify-end gap-3">
          <button onClick={onClose}
            className="px-4 py-2 text-sm text-gray-600 dark:text-gray-400 hover:bg-gray-100 dark:hover:bg-gray-700 rounded-lg transition-colors">
            Anuluj
          </button>
          <button onClick={handleSave} disabled={saving}
            className="px-4 py-2 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors disabled:opacity-50 flex items-center gap-2">
            {saving && <Spinner className="w-4 h-4" />}
            Zapisz
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Tab: Portale ─────────────────────────────────────────────────────────────

function PortalsTab({ portals, domains, loading, onRefresh, onOpenForm, onOpenSources, onViewPublished }) {
  const [fetchingId, setFetchingId] = useState(null)
  const [generatingId, setGeneratingId] = useState(null)
  const [confirmDelete, setConfirmDelete] = useState(null)

  const handleFetch = async (portalId) => {
    setFetchingId(portalId)
    try {
      await api.post(`/api/news-portals/${portalId}/fetch`)
      onRefresh()
    } catch {}
    setFetchingId(null)
  }

  const [autoResult, setAutoResult] = useState(null)

  const handleAutoGenerate = async (portalId) => {
    setGeneratingId(portalId)
    setAutoResult(null)
    try {
      const res = await api.post(`/api/news-portals/${portalId}/auto-generate`)
      setAutoResult({ portalId, ...res.data })
      onRefresh()
    } catch (err) {
      setAutoResult({ portalId, error: err.response?.data?.detail || 'Błąd generowania' })
    }
    setGeneratingId(null)
  }

  const handleDelete = async (portalId) => {
    try {
      await api.delete(`/api/news-portals/${portalId}`)
      setConfirmDelete(null)
      onRefresh()
    } catch {}
  }

  const handleToggle = async (portal) => {
    try {
      await api.put(`/api/news-portals/${portal.id}`, { active: !portal.active })
      onRefresh()
    } catch {}
  }

  if (loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="w-8 h-8 text-blue-600" />
      </div>
    )
  }

  if (portals.length === 0) {
    return (
      <div className="text-center py-16">
        <svg className="w-16 h-16 mx-auto text-gray-300 dark:text-gray-600 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z" />
        </svg>
        <p className="text-gray-500 dark:text-gray-400 mb-2">Brak portali.</p>
        <p className="text-gray-400 dark:text-gray-500 text-sm mb-4">Dodaj pierwszy portal newsowy!</p>
        <button onClick={() => onOpenForm(null)}
          className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm transition-colors">
          Dodaj portal
        </button>
      </div>
    )
  }

  const getDomainName = (domainId) => {
    const d = domains.find(d => d.id === domainId)
    return d?.domain || '—'
  }

  return (
    <>
      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {portals.map(p => (
          <div key={p.id} className="bg-white dark:bg-gray-800 rounded-2xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
            {/* Card header */}
            <div className="p-5">
              <div className="flex items-start justify-between mb-3">
                <div className="min-w-0 flex-1">
                  <h4 className="text-base font-semibold text-gray-900 dark:text-white truncate">{p.name}</h4>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-0.5 truncate">
                    {getDomainName(p.my_domain_id)}
                  </p>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0 ml-2">
                  {p.niche && <Badge color={nicheColor(p.niche)}>{p.niche}</Badge>}
                  <button
                    onClick={() => handleToggle(p)}
                    className={`relative w-10 h-5 rounded-full transition-colors ${p.active ? 'bg-green-500' : 'bg-gray-300 dark:bg-gray-600'}`}>
                    <span className={`absolute top-0.5 left-0.5 w-4 h-4 bg-white rounded-full transition-transform shadow-sm ${p.active ? 'translate-x-5' : ''}`} />
                  </button>
                </div>
              </div>

              {/* Stats row */}
              <div className="grid grid-cols-3 gap-2 mb-4">
                <div className="bg-gray-50 dark:bg-gray-700/50 rounded-lg p-2 text-center">
                  <p className="text-lg font-bold text-gray-900 dark:text-white">{p.source_count ?? 0}</p>
                  <p className="text-[10px] text-gray-400 dark:text-gray-500 uppercase">Źródła</p>
                </div>
                <div className="bg-yellow-50 dark:bg-yellow-900/20 rounded-lg p-2 text-center">
                  <p className="text-lg font-bold text-yellow-600 dark:text-yellow-400">{p.pending_count ?? 0}</p>
                  <p className="text-[10px] text-gray-400 dark:text-gray-500 uppercase">Oczekujące</p>
                </div>
                <div className="bg-green-50 dark:bg-green-900/20 rounded-lg p-2 text-center cursor-pointer hover:bg-green-100 dark:hover:bg-green-900/40 transition-colors"
                  onClick={() => (p.published_count ?? 0) > 0 && onViewPublished && onViewPublished(p.id)}
                  title={`${p.published_count ?? 0} opublikowanych — kliknij by zobaczyć linki`}>
                  <p className="text-lg font-bold text-green-600 dark:text-green-400">{p.published_count ?? 0}</p>
                  <p className="text-[10px] text-gray-400 dark:text-gray-500 uppercase">Opublikowane</p>
                </div>
              </div>

              {/* Extra info */}
              <div className="flex items-center gap-3 text-xs text-gray-400 dark:text-gray-500 mb-4">
                <span>{p.posts_per_day ?? 5} art./dzień</span>
                <span className="w-1 h-1 bg-gray-300 dark:bg-gray-600 rounded-full" />
                <span>co {p.check_interval_min ?? 30} min</span>
                <span className="w-1 h-1 bg-gray-300 dark:bg-gray-600 rounded-full" />
                <span>{p.language === 'en' ? 'EN' : 'PL'}</span>
                {p.auto_publish && (
                  <>
                    <span className="w-1 h-1 bg-gray-300 dark:bg-gray-600 rounded-full" />
                    <Badge color="green">Auto</Badge>
                  </>
                )}
              </div>
            </div>

            {/* Card actions */}
            <div className="px-5 py-3 border-t border-gray-100 dark:border-gray-700 flex items-center gap-1 flex-wrap">
              <button onClick={() => onOpenForm(p)} title="Edytuj"
                className="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-lg transition-colors">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                </svg>
              </button>
              <button onClick={() => onOpenSources(p)} title="Źródła"
                className="p-2 text-gray-400 hover:text-purple-600 hover:bg-purple-50 dark:hover:bg-purple-900/20 rounded-lg transition-colors">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
                </svg>
              </button>
              <button onClick={() => handleFetch(p.id)} disabled={fetchingId === p.id} title="Pobierz newsy"
                className="p-2 text-gray-400 hover:text-cyan-600 hover:bg-cyan-50 dark:hover:bg-cyan-900/20 rounded-lg transition-colors disabled:opacity-50">
                {fetchingId === p.id ? <Spinner className="w-4 h-4" /> : (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
                  </svg>
                )}
              </button>
              <button onClick={() => handleAutoGenerate(p.id)} disabled={generatingId === p.id} title="Pobierz i generuj artykuły"
                className="p-2 text-gray-400 hover:text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20 rounded-lg transition-colors disabled:opacity-50">
                {generatingId === p.id ? <Spinner className="w-4 h-4" /> : (
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" />
                  </svg>
                )}
              </button>
              <div className="flex-1" />
              <button onClick={() => setConfirmDelete(p.id)} title="Usuń"
                className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors">
                <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                </svg>
              </button>
            </div>
          </div>
        ))}
      </div>

      {/* Auto-generate result banner */}
      {autoResult && (
        <div className={`mt-4 p-4 rounded-xl border ${autoResult.error ? 'bg-red-50 dark:bg-red-900/20 border-red-200 dark:border-red-800' : 'bg-green-50 dark:bg-green-900/20 border-green-200 dark:border-green-800'}`}>
          <div className="flex items-center justify-between">
            <div>
              {autoResult.error ? (
                <p className="text-sm text-red-700 dark:text-red-300">{autoResult.error}</p>
              ) : (
                <>
                  <p className="text-sm font-medium text-green-800 dark:text-green-300">
                    Pobrano {autoResult.fetch?.new_items ?? 0} nowych wiadomości, utworzono {autoResult.fetch?.new_clusters ?? 0} klastrów, wygenerowano {autoResult.generated ?? 0} artykułów
                  </p>
                  {autoResult.fetch?.errors && autoResult.fetch.errors.length > 0 && (
                    <p className="text-xs text-yellow-600 dark:text-yellow-400 mt-1">
                      Ostrzeżenia RSS: {autoResult.fetch.errors.join(', ')}
                    </p>
                  )}
                </>
              )}
            </div>
            <button onClick={() => setAutoResult(null)} className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-300 ml-3">
              <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
              </svg>
            </button>
          </div>
        </div>
      )}

      {confirmDelete && (
        <ConfirmDialog
          title="Usuń portal"
          message="Czy na pewno chcesz usunąć ten portal? Zostaną usunięte wszystkie źródła i wersje robocze."
          onConfirm={() => handleDelete(confirmDelete)}
          onCancel={() => setConfirmDelete(null)}
        />
      )}
    </>
  )
}

// ── Tab: Kolejka Review ──────────────────────────────────────────────────────

function ReviewQueueTab({ portals }) {
  const [drafts, setDrafts] = useState([])
  const [loading, setLoading] = useState(true)
  const [portalFilter, setPortalFilter] = useState('')
  const [previewDraft, setPreviewDraft] = useState(null)
  const [editDraft, setEditDraft] = useState(null)
  const [publishedUrls, setPublishedUrls] = useState([])

  const loadDrafts = useCallback(async () => {
    setLoading(true)
    try {
      const promises = (portals || []).map(p =>
        api.get(`/api/news-portals/${p.id}/drafts`).then(res => {
          const items = res.data?.items || (Array.isArray(res.data) ? res.data : [])
          return items.map(d => ({ ...d, portal_name: p.name, portal_id: p.id }))
        }).catch(() => [])
      )
      const results = await Promise.all(promises)
      const all = results.flat().sort((a, b) => (b.created_at || '').localeCompare(a.created_at || ''))
      setDrafts(all)
    } catch {
      setDrafts([])
    } finally {
      setLoading(false)
    }
  }, [portals])

  useEffect(() => { loadDrafts() }, [loadDrafts])

  const handleApprove = async (draftId) => {
    try {
      const res = await api.post(`/api/news-portals/drafts/${draftId}/approve`)
      const url = res.data?.wp_post_url
      if (url) {
        setPublishedUrls(prev => [{ url, title: res.data?.title || drafts.find(d => d.id === draftId)?.title || '', id: draftId }, ...prev])
      }
      setPreviewDraft(null)
      await loadDrafts()
    } catch {}
  }

  const handleReject = async (draftId) => {
    try {
      await api.post(`/api/news-portals/drafts/${draftId}/reject`)
      setPreviewDraft(null)
      await loadDrafts()
    } catch {}
  }

  const filtered = portalFilter
    ? drafts.filter(d => String(d.portal_id) === portalFilter)
    : drafts

  const pendingDrafts = filtered.filter(d => d.status === 'pending' || !d.status)

  if (loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="w-8 h-8 text-blue-600" />
      </div>
    )
  }

  return (
    <>
      {/* Filter bar */}
      <div className="flex items-center gap-4 mb-6">
        <div>
          <select value={portalFilter} onChange={e => setPortalFilter(e.target.value)}
            className="border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500">
            <option value="">Wszystkie portale</option>
            {(portals || []).map(p => (
              <option key={p.id} value={p.id}>{p.name}</option>
            ))}
          </select>
        </div>
        <span className="text-sm text-gray-500 dark:text-gray-400">
          {pendingDrafts.length} {pendingDrafts.length === 1 ? 'artykuł oczekuje' : 'artykułów oczekuje'}
        </span>
      </div>

      {/* Recently published URLs */}
      {publishedUrls.length > 0 && (
        <div className="mb-4 bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-xl p-4">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-sm font-semibold text-green-700 dark:text-green-300">Opublikowane linki</h4>
            <button onClick={() => setPublishedUrls([])} className="text-xs text-green-600 dark:text-green-400 hover:underline">Wyczyść</button>
          </div>
          <div className="space-y-1">
            {publishedUrls.map(pu => (
              <div key={pu.id} className="flex items-center gap-2 text-sm">
                <svg className="w-4 h-4 text-green-500 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                </svg>
                <span className="text-gray-700 dark:text-gray-300 truncate max-w-xs">{pu.title}</span>
                <a href={pu.url} target="_blank" rel="noopener noreferrer"
                  className="text-blue-600 dark:text-blue-400 hover:underline text-xs truncate">
                  {pu.url}
                </a>
              </div>
            ))}
          </div>
        </div>
      )}

      {pendingDrafts.length === 0 ? (
        <div className="text-center py-16">
          <svg className="w-16 h-16 mx-auto text-gray-300 dark:text-gray-600 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          <p className="text-gray-500 dark:text-gray-400">Brak artykułów do przeglądu.</p>
          <p className="text-gray-400 dark:text-gray-500 text-sm mt-1">Wszystko zatwierdzone!</p>
        </div>
      ) : (
        <div className="space-y-3">
          {pendingDrafts.map(d => (
            <div key={d.id} className="bg-white dark:bg-gray-800 rounded-2xl shadow-sm border border-gray-100 dark:border-gray-700 p-5">
              <div className="flex items-start justify-between gap-4">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 mb-1">
                    <Badge color="blue">{d.portal_name}</Badge>
                    {d.source_count && (
                      <span className="text-xs text-gray-400 dark:text-gray-500">{d.source_count} źródeł</span>
                    )}
                  </div>
                  <h4 className="text-sm font-semibold text-gray-900 dark:text-white mb-1 line-clamp-2">{d.title || 'Bez tytułu'}</h4>
                  <p className="text-xs text-gray-500 dark:text-gray-400 line-clamp-2">
                    {d.excerpt || (d.content ? d.content.replace(/<[^>]*>/g, '').slice(0, 200) : '—')}
                  </p>
                  <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">
                    {d.created_at?.slice(0, 16).replace('T', ' ') || '—'}
                  </p>
                </div>

                <div className="flex items-center gap-1 flex-shrink-0">
                  <button onClick={() => setPreviewDraft(d)} title="Podgląd"
                    className="p-2 text-gray-400 hover:text-blue-600 hover:bg-blue-50 dark:hover:bg-blue-900/20 rounded-lg transition-colors">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2.458 12C3.732 7.943 7.523 5 12 5c4.478 0 8.268 2.943 9.542 7-1.274 4.057-5.064 7-9.542 7-4.477 0-8.268-2.943-9.542-7z" />
                    </svg>
                  </button>
                  <button onClick={() => handleApprove(d.id)} title="Zatwierdź"
                    className="p-2 text-gray-400 hover:text-green-600 hover:bg-green-50 dark:hover:bg-green-900/20 rounded-lg transition-colors">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
                    </svg>
                  </button>
                  <button onClick={() => setEditDraft(d)} title="Edytuj"
                    className="p-2 text-gray-400 hover:text-yellow-600 hover:bg-yellow-50 dark:hover:bg-yellow-900/20 rounded-lg transition-colors">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
                    </svg>
                  </button>
                  <button onClick={() => handleReject(d.id)} title="Odrzuć"
                    className="p-2 text-gray-400 hover:text-red-600 hover:bg-red-50 dark:hover:bg-red-900/20 rounded-lg transition-colors">
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {previewDraft && (
        <PreviewDraftModal
          draft={previewDraft}
          onClose={() => setPreviewDraft(null)}
          onApprove={handleApprove}
          onReject={handleReject}
          onEdit={(d) => { setPreviewDraft(null); setEditDraft(d) }}
        />
      )}

      {editDraft && (
        <EditDraftModal
          draft={editDraft}
          onClose={() => setEditDraft(null)}
          onSaved={() => { setEditDraft(null); loadDrafts() }}
        />
      )}
    </>
  )
}

// ── Tab: Opublikowane ────────────────────────────────────────────────────────

function PublishedTab({ portals, initialFilter = '' }) {
  const [items, setItems] = useState([])
  const [loading, setLoading] = useState(true)
  const [portalFilter, setPortalFilter] = useState(initialFilter)
  const [selected, setSelected] = useState(new Set())
  const [indexing, setIndexing] = useState(false)
  const [indexResult, setIndexResult] = useState(null)
  const [page, setPage] = useState(1)
  const [totalPages, setTotalPages] = useState(1)
  const [total, setTotal] = useState(0)

  const loadPublished = useCallback(async () => {
    setLoading(true)
    try {
      const params = { page, per_page: 50 }
      if (portalFilter) params.portal_id = portalFilter
      const res = await api.get('/api/news-portals/published-urls', { params })
      setItems(res.data?.items || [])
      setTotalPages(res.data?.pages || 1)
      setTotal(res.data?.total || 0)
    } catch {
      setItems([])
    } finally {
      setLoading(false)
    }
  }, [page, portalFilter])

  useEffect(() => { loadPublished() }, [loadPublished])
  useEffect(() => { if (initialFilter) { setPortalFilter(initialFilter); setPage(1) } }, [initialFilter])

  const toggleSelect = (id) => {
    setSelected(prev => {
      const next = new Set(prev)
      next.has(id) ? next.delete(id) : next.add(id)
      return next
    })
  }

  const toggleAll = () => {
    if (selected.size === items.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(items.map(i => i.id)))
    }
  }

  const handleIndex = async () => {
    const urls = items.filter(i => selected.has(i.id)).map(i => i.wp_post_url).filter(Boolean)
    if (!urls.length) return
    setIndexing(true)
    setIndexResult(null)
    try {
      const res = await api.post('/api/history/indexer/submit', { urls })
      setIndexResult(res.data)
    } catch (e) {
      setIndexResult({ success: false, message: e.message })
    } finally {
      setIndexing(false)
    }
  }

  if (loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="w-8 h-8 text-blue-600" />
      </div>
    )
  }

  return (
    <>
      <div className="flex items-center gap-4 mb-4 flex-wrap">
        <select value={portalFilter} onChange={e => { setPortalFilter(e.target.value); setPage(1) }}
          className="border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500">
          <option value="">Wszystkie portale</option>
          {(portals || []).map(p => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
        <span className="text-sm text-gray-500 dark:text-gray-400">{total} opublikowanych</span>
        {selected.size > 0 && (
          <button onClick={handleIndex} disabled={indexing}
            className="ml-auto px-4 py-2 text-sm bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors disabled:opacity-50 flex items-center gap-2">
            {indexing && <Spinner className="w-4 h-4" />}
            Indeksuj zaznaczone ({selected.size})
          </button>
        )}
      </div>

      {indexResult && (
        <div className={`mb-4 p-3 rounded-lg text-sm ${indexResult.success !== false ? 'bg-green-50 dark:bg-green-900/20 text-green-700 dark:text-green-300' : 'bg-red-50 dark:bg-red-900/20 text-red-700 dark:text-red-300'}`}>
          {indexResult.success !== false ? 'Wysłano do indeksacji!' : `Błąd: ${indexResult.message || 'Nieznany błąd'}`}
          {indexResult.submitted && <span className="ml-2">({indexResult.submitted} URL-i)</span>}
        </div>
      )}

      {items.length === 0 ? (
        <div className="text-center py-16">
          <p className="text-gray-500 dark:text-gray-400">Brak opublikowanych artykułów.</p>
        </div>
      ) : (
        <>
          <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-sm border border-gray-100 dark:border-gray-700 overflow-hidden">
            <table className="w-full text-sm">
              <thead className="bg-gray-50 dark:bg-gray-700/50">
                <tr>
                  <th className="px-4 py-3 text-left">
                    <input type="checkbox" checked={selected.size === items.length && items.length > 0}
                      onChange={toggleAll} className="rounded border-gray-300 dark:border-gray-600" />
                  </th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Tytuł</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Portal</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">URL</th>
                  <th className="px-4 py-3 text-left text-xs font-medium text-gray-500 dark:text-gray-400 uppercase">Data</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {items.map(item => (
                  <tr key={item.id} className="hover:bg-gray-50 dark:hover:bg-gray-700/30 transition-colors">
                    <td className="px-4 py-3">
                      <input type="checkbox" checked={selected.has(item.id)}
                        onChange={() => toggleSelect(item.id)} className="rounded border-gray-300 dark:border-gray-600" />
                    </td>
                    <td className="px-4 py-3 text-gray-900 dark:text-white font-medium max-w-xs truncate">{item.title}</td>
                    <td className="px-4 py-3"><Badge color="blue">{item.portal_name || '—'}</Badge></td>
                    <td className="px-4 py-3 max-w-sm">
                      <a href={item.wp_post_url} target="_blank" rel="noopener noreferrer"
                        className="text-blue-600 dark:text-blue-400 hover:underline text-xs truncate block">
                        {item.wp_post_url}
                      </a>
                    </td>
                    <td className="px-4 py-3 text-xs text-gray-500 dark:text-gray-400 whitespace-nowrap">
                      {item.published_at?.slice(0, 16).replace('T', ' ') || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {totalPages > 1 && (
            <div className="flex justify-center gap-2 mt-4">
              <button onClick={() => setPage(p => Math.max(1, p - 1))} disabled={page <= 1}
                className="px-3 py-1 text-sm border border-gray-200 dark:border-gray-600 rounded-lg disabled:opacity-40 hover:bg-gray-50 dark:hover:bg-gray-700">
                &laquo; Poprzednia
              </button>
              <span className="px-3 py-1 text-sm text-gray-500 dark:text-gray-400">
                {page} / {totalPages}
              </span>
              <button onClick={() => setPage(p => Math.min(totalPages, p + 1))} disabled={page >= totalPages}
                className="px-3 py-1 text-sm border border-gray-200 dark:border-gray-600 rounded-lg disabled:opacity-40 hover:bg-gray-50 dark:hover:bg-gray-700">
                Następna &raquo;
              </button>
            </div>
          )}
        </>
      )}
    </>
  )
}


// ── Tab: Statystyki ──────────────────────────────────────────────────────────

function StatsTab() {
  const [stats, setStats] = useState(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api.get('/api/news-portals/stats')
      .then(res => setStats(res.data))
      .catch(() => setStats(null))
      .finally(() => setLoading(false))
  }, [])

  if (loading) {
    return (
      <div className="flex justify-center py-16">
        <Spinner className="w-8 h-8 text-blue-600" />
      </div>
    )
  }

  if (!stats) {
    return (
      <div className="text-center py-16">
        <p className="text-gray-500 dark:text-gray-400">Nie udało się wczytać statystyk.</p>
      </div>
    )
  }

  const cards = [
    {
      label: 'Portale',
      value: stats.total_portals ?? 0,
      color: 'text-blue-600',
      bg: 'bg-blue-50 dark:bg-blue-900/20',
      icon: 'M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z',
    },
    {
      label: 'Źródła',
      value: stats.total_sources ?? 0,
      color: 'text-purple-600',
      bg: 'bg-purple-50 dark:bg-purple-900/20',
      icon: 'M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1',
    },
    {
      label: 'Oczekujące',
      value: stats.pending_drafts ?? 0,
      color: 'text-yellow-600',
      bg: 'bg-yellow-50 dark:bg-yellow-900/20',
      icon: 'M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z',
    },
    {
      label: 'Opublikowane dzisiaj',
      value: stats.published_today ?? 0,
      color: 'text-green-600',
      bg: 'bg-green-50 dark:bg-green-900/20',
      icon: 'M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z',
    },
    {
      label: 'Opublikowane łącznie',
      value: stats.published_total ?? 0,
      color: 'text-emerald-600',
      bg: 'bg-emerald-50 dark:bg-emerald-900/20',
      icon: 'M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z',
    },
  ]

  return (
    <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4">
      {cards.map(c => (
        <div key={c.label} className="bg-white dark:bg-gray-800 rounded-2xl shadow-sm border border-gray-100 dark:border-gray-700 p-6">
          <div className="flex items-center justify-between mb-3">
            <div className={`w-10 h-10 rounded-full flex items-center justify-center ${c.bg}`}>
              <svg className={`w-5 h-5 ${c.color}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d={c.icon} />
              </svg>
            </div>
          </div>
          <p className={`text-3xl font-bold ${c.color}`}>{c.value}</p>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">{c.label}</p>
        </div>
      ))}
    </div>
  )
}

// ── Main Page ────────────────────────────────────────────────────────────────

export default function NewsPortals() {
  const [activeTab, setActiveTab] = useState(0)
  const [portals, setPortals] = useState([])
  const [domains, setDomains] = useState([])
  const [loading, setLoading] = useState(true)
  const [publishedPortalFilter, setPublishedPortalFilter] = useState('')

  // Modal states
  const [showPortalForm, setShowPortalForm] = useState(false)
  const [editingPortal, setEditingPortal] = useState(null)
  const [sourcesPortal, setSourcesPortal] = useState(null)

  const loadPortals = useCallback(async () => {
    setLoading(true)
    try {
      const res = await api.get('/api/news-portals/')
      setPortals(res.data || [])
    } catch {
      setPortals([])
    } finally {
      setLoading(false)
    }
  }, [])

  const loadDomains = useCallback(async () => {
    try {
      const res = await api.get('/api/domains')
      const list = Array.isArray(res.data) ? res.data : (res.data?.domains || res.data || [])
      setDomains(list)
    } catch {
      setDomains([])
    }
  }, [])

  useEffect(() => {
    loadPortals()
    loadDomains()
  }, [loadPortals, loadDomains])

  const handleOpenForm = (portal) => {
    setEditingPortal(portal)
    setShowPortalForm(true)
  }

  const handleCloseForm = () => {
    setShowPortalForm(false)
    setEditingPortal(null)
  }

  const handleSaveForm = () => {
    handleCloseForm()
    loadPortals()
  }

  const handleOpenSources = (portal) => {
    setSourcesPortal(portal)
  }

  const pendingTotal = portals.reduce((sum, p) => sum + (p.pending_count || 0), 0)

  return (
    <div className="p-6">
      {/* Header */}
      <div className="flex items-start justify-between mb-6">
        <div className="flex items-center gap-3">
          <div className="w-10 h-10 rounded-full bg-blue-100 dark:bg-blue-900/30 flex items-center justify-center">
            <svg className="w-5 h-5 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z" />
            </svg>
          </div>
          <div>
            <h2 className="text-2xl font-bold text-gray-900 dark:text-white">News Portals</h2>
            <p className="text-sm text-gray-500 dark:text-gray-400">Zarządzanie portalami newsowymi</p>
          </div>
        </div>
        <button onClick={() => handleOpenForm(null)}
          className="px-4 py-2.5 bg-blue-600 hover:bg-blue-700 text-white rounded-lg text-sm font-medium transition-colors flex items-center gap-2">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 4v16m8-8H4" />
          </svg>
          Dodaj Portal
        </button>
      </div>

      {/* Tabs */}
      <div className="flex items-center gap-1 mb-6 border-b border-gray-200 dark:border-gray-700">
        {TABS.map((tab, i) => (
          <button key={tab} onClick={() => setActiveTab(i)}
            className={`px-4 py-3 text-sm font-medium border-b-2 transition-colors -mb-px ${
              activeTab === i
                ? 'border-blue-600 text-blue-600 dark:text-blue-400'
                : 'border-transparent text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-300'
            }`}>
            {tab}
            {i === 1 && pendingTotal > 0 && (
              <span className="ml-2 bg-yellow-100 dark:bg-yellow-900/30 text-yellow-700 dark:text-yellow-400 text-xs font-bold px-1.5 py-0.5 rounded-full">
                {pendingTotal}
              </span>
            )}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === 0 && (
        <PortalsTab
          portals={portals}
          domains={domains}
          loading={loading}
          onRefresh={loadPortals}
          onOpenForm={handleOpenForm}
          onOpenSources={handleOpenSources}
          onViewPublished={(portalId) => { setPublishedPortalFilter(String(portalId)); setActiveTab(2) }}
        />
      )}
      {activeTab === 1 && (
        <ReviewQueueTab portals={portals} />
      )}
      {activeTab === 2 && (
        <PublishedTab portals={portals} initialFilter={publishedPortalFilter} />
      )}
      {activeTab === 3 && (
        <StatsTab />
      )}

      {/* Portal form modal */}
      {showPortalForm && (
        <PortalFormModal
          portal={editingPortal}
          domains={domains}
          onSave={handleSaveForm}
          onClose={handleCloseForm}
        />
      )}

      {/* Sources modal */}
      {sourcesPortal && (
        <SourcesModal
          portal={sourcesPortal}
          onClose={() => setSourcesPortal(null)}
          onUpdated={loadPortals}
        />
      )}
    </div>
  )
}
