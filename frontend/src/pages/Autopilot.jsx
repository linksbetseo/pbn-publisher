import { useState, useEffect, useRef, useCallback } from 'react'
import api from '../api/client'

const STATUS_COLOR = {
  pending: 'bg-yellow-100 text-yellow-700',
  published: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
}

const KW_TYPE_COLOR = {
  pillar: 'bg-blue-100 text-blue-700',
  supporting: 'bg-gray-100 text-gray-600',
}

// ── Bulk Tab ──────────────────────────────────────────────────────────────────

function BulkTab() {
  const [allDomains, setAllDomains] = useState([])
  const [schedules, setSchedules] = useState([])
  const [selected, setSelected] = useState(new Set())
  const [serverFilter, setServerFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [tab, setTab] = useState('domains') // domains | schedules
  const [loading, setLoading] = useState(true)
  const [actionLog, setActionLog] = useState([])
  const [running, setRunning] = useState(false)

  // Shared settings
  const [seedKw, setSeedKw] = useState('')
  const [ppd, setPpd] = useState(1)
  const [lang, setLang] = useState('pl')
  const [minVol, setMinVol] = useState(10)
  const [clientDomain, setClientDomain] = useState('')
  const [anchorText, setAnchorText] = useState('')
  const [runLimit, setRunLimit] = useState(1)

  const log = (msg, type = 'info') => setActionLog(l => [...l, { msg, type, ts: new Date().toLocaleTimeString('pl-PL') }])

  const load = useCallback(async () => {
    setLoading(true)
    const [domRes, schRes] = await Promise.all([
      api.get('/api/domains', { params: { active: 1 } }),
      api.get('/api/autopilot/schedules'),
    ])
    setAllDomains(domRes.data)
    setSchedules(schRes.data)
    setLoading(false)
  }, [])

  useEffect(() => { load() }, [load])

  const servers = ['all', ...new Set(allDomains.map(d => d.server).filter(Boolean))]

  const filteredDomains = allDomains.filter(d => {
    if (serverFilter !== 'all' && d.server !== serverFilter) return false
    if (search && !d.domain.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const filteredSchedules = schedules.filter(s => {
    if (search && !s.domain.toLowerCase().includes(search.toLowerCase())) return false
    return true
  })

  const toggleAll = () => {
    const items = tab === 'domains' ? filteredDomains : filteredSchedules
    const ids = items.map(i => i.id)
    if (ids.every(id => selected.has(id))) {
      setSelected(s => { const n = new Set(s); ids.forEach(id => n.delete(id)); return n })
    } else {
      setSelected(s => { const n = new Set(s); ids.forEach(id => n.add(id)); return n })
    }
  }

  const toggle = (id) => setSelected(s => {
    const n = new Set(s)
    n.has(id) ? n.delete(id) : n.add(id)
    return n
  })

  const selectedList = [...selected]

  // ── Actions ──

  const bulkCreateSchedules = async () => {
    if (!selectedList.length) return alert('Zaznacz domeny')
    if (!seedKw.trim()) return alert('Wpisz frazę seed')
    setRunning(true)
    setActionLog([])
    log(`Tworzę harmonogramy dla ${selectedList.length} domen...`)
    try {
      const res = await api.post('/api/autopilot/bulk-create', {
        domain_ids: selectedList,
        seed_keyword: seedKw,
        posts_per_day: Number(ppd),
        language: lang,
        min_volume: Number(minVol),
        client_domain: clientDomain,
        anchor_text: anchorText,
      })
      log(`✓ Utworzono: ${res.data.created}, pominięto: ${res.data.skipped}, błędy: ${res.data.errors}`, 'ok')
      await load()
      setSelected(new Set())
    } catch (e) {
      log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
    } finally {
      setRunning(false)
    }
  }

  const bulkGenerateMaps = async () => {
    if (!selectedList.length) return alert('Zaznacz harmonogramy')
    setRunning(true)
    setActionLog([])
    log(`Generuję Topical Map dla ${selectedList.length} harmonogramów... (może potrwać kilka minut)`)
    try {
      const res = await api.post('/api/autopilot/bulk-generate-maps', { schedule_ids: selectedList }, { timeout: 600000 })
      log(`✓ OK: ${res.data.ok}/${res.data.processed}`, 'ok')
      res.data.results.forEach(r => {
        if (r.error) log(`  ✗ ${r.domain || r.schedule_id}: ${r.error}`, 'err')
        else log(`  ✓ ${r.domain}: ${r.inserted} nowych fraz (${r.pillars} klastrów)`, 'ok')
      })
      await load()
    } catch (e) {
      log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
    } finally {
      setRunning(false)
    }
  }

  const bulkRun = async () => {
    if (!selectedList.length) return alert('Zaznacz harmonogramy')
    setRunning(true)
    setActionLog([])
    log(`Uruchamiam publikację dla ${selectedList.length} harmonogramów (${runLimit} artykuł/domena)...`)
    try {
      const res = await api.post('/api/autopilot/bulk-run', { schedule_ids: selectedList, limit: Number(runLimit) }, { timeout: 600000 })
      log(`✓ Łącznie: ${res.data.total_published} opublikowanych, ${res.data.total_failed} błędów`, 'ok')
      res.data.results.forEach(r => {
        if (r.skipped) log(`  ⚠ ${r.domain}: pominięto (${r.reason})`, 'warn')
        else if (r.error) log(`  ✗ ${r.domain}: ${r.error}`, 'err')
        else log(`  ✓ ${r.domain}: ${r.published} pub, ${r.failed} err`, r.failed > 0 ? 'warn' : 'ok')
      })
      await load()
    } catch (e) {
      log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
    } finally {
      setRunning(false)
    }
  }

  const bulkSetPpd = async () => {
    if (!selectedList.length) return alert('Zaznacz harmonogramy')
    setRunning(true)
    try {
      await api.post('/api/autopilot/bulk-set-ppd', { schedule_ids: selectedList, limit: Number(ppd) })
      log(`✓ Ustawiono ${ppd} postów/dzień dla ${selectedList.length} harmonogramów`, 'ok')
      await load()
    } catch (e) {
      log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
    } finally {
      setRunning(false)
    }
  }

  const logColor = { ok: 'text-green-400', err: 'text-red-400', warn: 'text-yellow-300', info: 'text-gray-300' }

  if (loading) return <div className="flex items-center justify-center h-40"><div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" /></div>

  return (
    <div className="space-y-5">
      {/* Sub-tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-1 w-fit">
        {[['domains', `Domeny (${allDomains.length})`], ['schedules', `Harmonogramy (${schedules.length})`]].map(([k, label]) => (
          <button key={k} onClick={() => { setTab(k); setSelected(new Set()) }}
            className={`px-4 py-1.5 rounded-md text-xs font-medium transition-colors ${tab === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>
            {label}
          </button>
        ))}
      </div>

      <div className="flex gap-4">
        {/* Left: list */}
        <div className="flex-1 min-w-0">
          {/* Filters */}
          <div className="flex gap-2 mb-3 flex-wrap">
            <input value={search} onChange={e => setSearch(e.target.value)}
              placeholder="Szukaj domeny..."
              className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm w-48 focus:outline-none focus:ring-2 focus:ring-blue-500" />
            {tab === 'domains' && (
              <select value={serverFilter} onChange={e => setServerFilter(e.target.value)}
                className="border border-gray-200 rounded-lg px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                {servers.map(s => <option key={s} value={s}>{s === 'all' ? 'Wszystkie serwery' : s}</option>)}
              </select>
            )}
            <button onClick={toggleAll}
              className="px-3 py-1.5 border border-gray-300 rounded-lg text-xs font-medium text-gray-600 hover:bg-gray-50">
              {(tab === 'domains' ? filteredDomains : filteredSchedules).every(i => selected.has(i.id))
                ? 'Odznacz wszystkie' : 'Zaznacz wszystkie'}
            </button>
            <span className="px-3 py-1.5 bg-blue-50 text-blue-700 rounded-lg text-xs font-medium">
              Zaznaczono: {selectedList.length}
            </span>
          </div>

          {/* Table */}
          <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
            <div className="max-h-[420px] overflow-y-auto">
              <table className="w-full text-xs">
                <thead className="sticky top-0 bg-gray-50 border-b border-gray-100 z-10">
                  <tr>
                    <th className="px-3 py-2 w-8">
                      <input type="checkbox"
                        checked={(tab === 'domains' ? filteredDomains : filteredSchedules).length > 0 &&
                          (tab === 'domains' ? filteredDomains : filteredSchedules).every(i => selected.has(i.id))}
                        onChange={toggleAll}
                        className="rounded" />
                    </th>
                    <th className="px-3 py-2 text-left font-semibold text-gray-500 uppercase tracking-wide">Domena</th>
                    {tab === 'domains' && <th className="px-3 py-2 text-left font-semibold text-gray-500 uppercase tracking-wide">Serwer</th>}
                    {tab === 'schedules' && <th className="px-3 py-2 text-left font-semibold text-gray-500 uppercase tracking-wide">Seed</th>}
                    {tab === 'schedules' && <th className="px-3 py-2 text-left font-semibold text-gray-500 uppercase tracking-wide">Frazy</th>}
                    {tab === 'schedules' && <th className="px-3 py-2 text-left font-semibold text-gray-500 uppercase tracking-wide">Mapa</th>}
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-50">
                  {(tab === 'domains' ? filteredDomains : filteredSchedules).map(item => (
                    <tr key={item.id} onClick={() => toggle(item.id)}
                      className={`cursor-pointer hover:bg-blue-50 transition-colors ${selected.has(item.id) ? 'bg-blue-50' : ''}`}>
                      <td className="px-3 py-2">
                        <input type="checkbox" checked={selected.has(item.id)} onChange={() => toggle(item.id)}
                          onClick={e => e.stopPropagation()} className="rounded" />
                      </td>
                      <td className="px-3 py-2 font-medium text-gray-800 max-w-[200px] truncate">{item.domain}</td>
                      {tab === 'domains' && <td className="px-3 py-2 text-gray-500">{item.server || '—'}</td>}
                      {tab === 'schedules' && <td className="px-3 py-2 text-purple-700 max-w-[140px] truncate">{item.seed_keyword}</td>}
                      {tab === 'schedules' && (
                        <td className="px-3 py-2 text-gray-600">
                          {item.published_count || 0}/{item.total_keywords || 0}
                        </td>
                      )}
                      {tab === 'schedules' && (
                        <td className="px-3 py-2">
                          {item.map_generated
                            ? <span className="text-green-600 font-medium">✓</span>
                            : <span className="text-orange-500">brak</span>}
                        </td>
                      )}
                    </tr>
                  ))}
                  {(tab === 'domains' ? filteredDomains : filteredSchedules).length === 0 && (
                    <tr><td colSpan={5} className="px-3 py-8 text-center text-gray-400">Brak wyników</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        {/* Right: actions panel */}
        <div className="w-72 shrink-0 space-y-4">
          {/* Settings */}
          <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-4 space-y-3">
            <h3 className="text-sm font-semibold text-gray-800">Ustawienia</h3>

            {tab === 'domains' && (
              <>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Fraza seed *</label>
                  <input value={seedKw} onChange={e => setSeedKw(e.target.value)}
                    placeholder="np. prawo pracy"
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Domena klienta</label>
                  <input value={clientDomain} onChange={e => setClientDomain(e.target.value)}
                    placeholder="https://klient.pl"
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Anchor text klienta</label>
                  <input value={anchorText} onChange={e => setAnchorText(e.target.value)}
                    placeholder="usługi prawne"
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <label className="block text-xs font-medium text-gray-500 mb-1">Posty/dzień</label>
                    <input type="number" min={1} max={20} value={ppd} onChange={e => setPpd(e.target.value)}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                  </div>
                  <div>
                    <label className="block text-xs font-medium text-gray-500 mb-1">Min. vol.</label>
                    <input type="number" min={0} value={minVol} onChange={e => setMinVol(e.target.value)}
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                  </div>
                </div>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Język</label>
                  <select value={lang} onChange={e => setLang(e.target.value)}
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500">
                    <option value="pl">Polski</option>
                    <option value="en">English</option>
                  </select>
                </div>
                <button onClick={bulkCreateSchedules} disabled={running || !selectedList.length}
                  className="w-full py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
                  {running ? '⟳ Tworzę...' : `+ Utwórz harmonogramy (${selectedList.length})`}
                </button>
              </>
            )}

            {tab === 'schedules' && (
              <>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Artykuły/uruchomienie</label>
                  <input type="number" min={1} max={50} value={runLimit} onChange={e => setRunLimit(e.target.value)}
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
                <button onClick={bulkGenerateMaps} disabled={running || !selectedList.length}
                  className="w-full py-2 bg-purple-600 text-white rounded-lg text-sm font-medium hover:bg-purple-700 disabled:opacity-50">
                  {running ? '⟳ Generuję...' : `↻ Generuj mapy (${selectedList.length})`}
                </button>
                <button onClick={bulkRun} disabled={running || !selectedList.length}
                  className="w-full py-2 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50">
                  {running ? '⟳ Publikuję...' : `▶ Uruchom publikację (${selectedList.length})`}
                </button>
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Ustaw posty/dzień</label>
                  <div className="flex gap-2">
                    <input type="number" min={1} max={20} value={ppd} onChange={e => setPpd(e.target.value)}
                      className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                    <button onClick={bulkSetPpd} disabled={running || !selectedList.length}
                      className="px-3 py-2 bg-gray-700 text-white rounded-lg text-xs font-medium hover:bg-gray-800 disabled:opacity-50">
                      Ustaw
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>

          {/* Log */}
          {actionLog.length > 0 && (
            <div className="bg-gray-900 rounded-xl p-3 max-h-60 overflow-y-auto">
              <p className="text-xs text-gray-500 mb-2 font-medium">Log operacji</p>
              <div className="space-y-0.5 font-mono text-xs">
                {actionLog.map((e, i) => (
                  <div key={i} className={logColor[e.type]}>
                    <span className="text-gray-600">{e.ts} </span>{e.msg}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Main component ─────────────────────────────────────────────────────────────

export default function Autopilot() {
  const [schedules, setSchedules] = useState([])
  const [domains, setDomains] = useState([])
  const [stats, setStats] = useState({})
  const [loading, setLoading] = useState(true)
  const [showAdd, setShowAdd] = useState(false)
  const [expandedId, setExpandedId] = useState(null)
  const [keywords, setKeywords] = useState({})
  const [kwFilter, setKwFilter] = useState({})
  const [runLog, setRunLog] = useState({})
  const [running, setRunning] = useState({})
  const [generatingMap, setGeneratingMap] = useState({})
  const [syncingCats, setSyncingCats] = useState({})
  const [catResults, setCatResults] = useState({})
  const [newForm, setNewForm] = useState({
    my_domain_id: '',
    seed_keyword: '',
    posts_per_day: 1,
    language: 'pl',
    min_volume: 10,
    client_domain: '',
    anchor_text: '',
  })
  const [addError, setAddError] = useState('')
  const [adding, setAdding] = useState(false)
  const logRefs = useRef({})

  const load = async () => {
    setLoading(true)
    const [schRes, domRes, stRes] = await Promise.all([
      api.get('/api/autopilot/schedules'),
      api.get('/api/domains'),
      api.get('/api/autopilot/stats'),
    ])
    setSchedules(schRes.data)
    setDomains(domRes.data.filter(d => d.active && d.wp_ok !== 0))
    setStats(stRes.data)
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const addSchedule = async () => {
    if (!newForm.my_domain_id || !newForm.seed_keyword.trim()) {
      setAddError('Wybierz domenę i wpisz frazę seed')
      return
    }
    setAdding(true)
    setAddError('')
    try {
      await api.post('/api/autopilot/schedules', {
        ...newForm,
        my_domain_id: Number(newForm.my_domain_id),
        posts_per_day: Number(newForm.posts_per_day),
        min_volume: Number(newForm.min_volume),
      })
      setShowAdd(false)
      setNewForm({ my_domain_id: '', seed_keyword: '', posts_per_day: 1, language: 'pl', min_volume: 10, client_domain: '', anchor_text: '' })
      await load()
    } catch (e) {
      setAddError(e.response?.data?.detail || e.message)
    } finally {
      setAdding(false)
    }
  }

  const generateMap = async (id) => {
    setGeneratingMap(g => ({ ...g, [id]: true }))
    try {
      const res = await api.post(`/api/autopilot/schedules/${id}/generate-map`)
      alert(`Wygenerowano mapę: ${res.data.pillars} klastrów, ${res.data.total_keywords} fraz`)
      await load()
    } catch (e) {
      alert('Błąd generowania mapy: ' + (e.response?.data?.detail || e.message))
    } finally {
      setGeneratingMap(g => ({ ...g, [id]: false }))
    }
  }

  const loadKeywords = async (id, status = '') => {
    const params = status ? { status } : {}
    const res = await api.get(`/api/autopilot/schedules/${id}/keywords`, { params })
    setKeywords(k => ({ ...k, [id]: res.data }))
    setKwFilter(f => ({ ...f, [id]: status }))
  }

  const toggleExpand = async (id) => {
    if (expandedId === id) {
      setExpandedId(null)
    } else {
      setExpandedId(id)
      if (!keywords[id]) await loadKeywords(id)
    }
  }

  const runNow = async (sched, limitOverride = null) => {
    const id = sched.id
    setRunning(r => ({ ...r, [id]: true }))
    setRunLog(l => ({ ...l, [id]: [] }))

    const limit = limitOverride || sched.posts_per_day
    try {
      const res = await api.post(`/api/autopilot/schedules/${id}/run`, { schedule_id: id, limit })
      const data = res.data
      // Wyświetl każdy wynik
      const entries = data.results || []
      if (entries.length === 0 && data.message) {
        entries.push({ status: 'info', keyword: '—', error: data.message })
      }
      setRunLog(l => ({ ...l, [id]: [...entries, { done: true, published: data.published, failed: data.failed }] }))
      setTimeout(() => {
        const el = logRefs.current[id]
        if (el) el.scrollTop = el.scrollHeight
      }, 50)
      await load()
      if (expandedId === id) await loadKeywords(id, kwFilter[id] || '')
    } catch (e) {
      const err = e.response?.data?.detail || e.message || 'Błąd połączenia'
      setRunLog(l => ({ ...l, [id]: [{ status: 'failed', keyword: '—', error: err }] }))
    } finally {
      setRunning(r => ({ ...r, [id]: false }))
    }
  }

  const syncCategories = async (sched) => {
    const id = sched.id
    setSyncingCats(s => ({ ...s, [id]: true }))
    setCatResults(r => ({ ...r, [id]: null }))
    try {
      const res = await api.post(`/api/autopilot/schedules/${id}/sync-categories`)
      setCatResults(r => ({ ...r, [id]: res.data }))
      setTimeout(() => setCatResults(r => ({ ...r, [id]: null })), 8000)
    } catch (e) {
      setCatResults(r => ({ ...r, [id]: { error: e.response?.data?.detail || e.message } }))
    } finally {
      setSyncingCats(s => ({ ...s, [id]: false }))
    }
  }

  const toggleActive = async (sched) => {
    await api.patch(`/api/autopilot/schedules/${sched.id}`, { active: sched.active ? 0 : 1 })
    await load()
  }

  const retryKeyword = async (kwId, schedId) => {
    try {
      await api.post(`/api/autopilot/keywords/${kwId}/retry`)
      await loadKeywords(schedId, kwFilter[schedId] || '')
    } catch (e) {
      alert('Błąd retry: ' + (e.response?.data?.detail || e.message))
    }
  }

  const deleteSchedule = async (id) => {
    if (!confirm('Usunąć harmonogram i wszystkie frazy?')) return
    await api.delete(`/api/autopilot/schedules/${id}`)
    await load()
  }

  const updatePpd = async (id, val) => {
    await api.patch(`/api/autopilot/schedules/${id}`, { posts_per_day: Number(val) })
    await load()
  }

  const set = (f, v) => setNewForm(n => ({ ...n, [f]: v }))
  const fmt = (s) => s ? new Date(s).toLocaleString('pl-PL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'
  const pending_count = (id) => keywords[id]?.filter(k => k.status === 'pending').length ?? '?'

  const [mainTab, setMainTab] = useState('schedules')

  return (
    <div className="p-8 max-w-7xl">
      <div className="flex items-center justify-between mb-2">
        <div>
          <h2 className="text-2xl font-bold text-gray-900">Autopilot PBN</h2>
          <p className="text-gray-500 text-sm mt-0.5">Automatyczne uzupełnianie treści na domenach PBN na podstawie Topical Map</p>
        </div>
        {mainTab === 'schedules' && (
          <button
            onClick={() => setShowAdd(!showAdd)}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
          >
            + Dodaj domenę
          </button>
        )}
      </div>

      {/* Main tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-1 mb-5 w-fit">
        {[['schedules', 'Harmonogramy'], ['bulk', '⚡ Bulk Setup']].map(([k, label]) => (
          <button key={k} onClick={() => setMainTab(k)}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${mainTab === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>
            {label}
          </button>
        ))}
      </div>

      {mainTab === 'bulk' && <BulkTab />}
      {mainTab === 'schedules' && <>


      {/* Global stats */}
      <div className="flex gap-3 mt-4 mb-6 flex-wrap">
        {[
          { label: 'Harmonogramów', val: stats.total_schedules || 0, color: 'text-gray-700 bg-gray-100' },
          { label: 'Aktywnych', val: stats.active_schedules || 0, color: 'text-blue-700 bg-blue-50' },
          { label: 'Fraz w kolejce', val: stats.pending_keywords || 0, color: 'text-yellow-700 bg-yellow-50' },
          { label: 'Opublikowanych', val: stats.published_keywords || 0, color: 'text-green-700 bg-green-50' },
        ].map(s => (
          <div key={s.label} className={`px-4 py-2 rounded-lg text-sm font-medium ${s.color}`}>
            {s.label}: <strong>{s.val}</strong>
          </div>
        ))}
      </div>

      {/* Add form */}
      {showAdd && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5 mb-6">
          <h3 className="font-semibold text-gray-800 mb-4">Nowy harmonogram</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Domena PBN (wp_ok)</label>
              <select
                value={newForm.my_domain_id}
                onChange={e => set('my_domain_id', e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="">— wybierz domenę —</option>
                {domains.map(d => (
                  <option key={d.id} value={d.id}>{d.domain} ({d.server})</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Fraza seed (temat domeny)</label>
              <input
                value={newForm.seed_keyword}
                onChange={e => set('seed_keyword', e.target.value)}
                placeholder="np. prawo pracy, ubezpieczenia"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Posty dziennie</label>
              <input
                type="number"
                value={newForm.posts_per_day}
                min={1}
                max={20}
                onChange={e => set('posts_per_day', e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Język</label>
              <select value={newForm.language} onChange={e => set('language', e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                <option value="pl">Polski</option>
                <option value="en">English</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Min. wolumen frazy</label>
              <input type="number" value={newForm.min_volume} onChange={e => set('min_volume', e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Domena klienta (link w artykule)</label>
              <input value={newForm.client_domain} onChange={e => set('client_domain', e.target.value)} placeholder="https://klient.pl (opcjonalne)" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Anchor text klienta</label>
              <input value={newForm.anchor_text} onChange={e => set('anchor_text', e.target.value)} placeholder="usługi prawne (opcjonalne)" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
            </div>
          </div>
          {addError && <p className="text-red-600 text-xs mt-2">{addError}</p>}
          <div className="flex gap-3 mt-4">
            <button onClick={addSchedule} disabled={adding} className="px-5 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
              {adding ? 'Dodaję...' : 'Dodaj harmonogram'}
            </button>
            <button onClick={() => setShowAdd(false)} className="px-5 py-2 text-gray-600 border border-gray-300 rounded-lg text-sm hover:bg-gray-50">Anuluj</button>
          </div>
        </div>
      )}

      {/* Schedules list */}
      {loading ? (
        <div className="flex items-center justify-center h-40">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
        </div>
      ) : schedules.length === 0 ? (
        <div className="bg-white rounded-xl border border-gray-200 p-16 text-center text-gray-400">
          <p className="text-lg">Brak harmonogramów</p>
          <p className="text-sm mt-1">Dodaj domenę PBN żeby zacząć automatyczne publikowanie</p>
        </div>
      ) : (
        <div className="space-y-3">
          {schedules.map(sched => (
            <div key={sched.id} className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
              {/* Header row */}
              <div className="flex items-center gap-3 p-4">
                {/* Active toggle */}
                <button
                  onClick={() => toggleActive(sched)}
                  className={`w-10 h-6 rounded-full transition-colors flex-shrink-0 ${sched.active ? 'bg-green-500' : 'bg-gray-300'}`}
                  title={sched.active ? 'Aktywny — kliknij żeby wstrzymać' : 'Wstrzymany — kliknij żeby aktywować'}
                >
                  <div className={`w-4 h-4 rounded-full bg-white shadow mx-1 transition-transform ${sched.active ? 'translate-x-4' : 'translate-x-0'}`} />
                </button>

                {/* Domain + seed */}
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-gray-900 text-sm">{sched.domain}</span>
                    <span className="text-xs px-2 py-0.5 bg-purple-100 text-purple-700 rounded-full">{sched.seed_keyword}</span>
                    {!sched.map_generated && (
                      <span className="text-xs px-2 py-0.5 bg-orange-100 text-orange-700 rounded-full">Brak mapy</span>
                    )}
                  </div>
                  <div className="text-xs text-gray-500 mt-0.5 flex gap-3 flex-wrap">
                    <span>Frazy: <strong>{sched.published_count || 0}</strong>/{sched.total_keywords || 0} opublikowanych</span>
                    <span>Ostatni run: {fmt(sched.last_run_at)}</span>
                    <span>Język: {sched.language}</span>
                    {sched.client_domain && <span>→ {sched.client_domain}</span>}
                  </div>
                </div>

                {/* Posts per day */}
                <div className="flex items-center gap-1 shrink-0">
                  <span className="text-xs text-gray-500">dziennie:</span>
                  <input
                    type="number"
                    defaultValue={sched.posts_per_day}
                    min={1}
                    max={20}
                    onBlur={e => updatePpd(sched.id, e.target.value)}
                    className="w-14 border border-gray-300 rounded px-2 py-1 text-sm text-center focus:outline-none focus:ring-2 focus:ring-blue-500"
                  />
                </div>

                {/* Actions */}
                <div className="flex items-center gap-2 shrink-0">
                  {!sched.map_generated ? (
                    <button
                      onClick={() => generateMap(sched.id)}
                      disabled={generatingMap[sched.id]}
                      className="px-3 py-1.5 bg-purple-600 text-white rounded-lg text-xs font-medium hover:bg-purple-700 disabled:opacity-50 flex items-center gap-1"
                    >
                      {generatingMap[sched.id] ? (
                        <><span className="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full" />Generuję...</>
                      ) : 'Generuj mapę'}
                    </button>
                  ) : (
                    <>
                      <button
                        onClick={() => runNow(sched)}
                        disabled={running[sched.id]}
                        className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 disabled:opacity-50 flex items-center gap-1"
                      >
                        {running[sched.id] ? (
                          <><span className="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full" />Publikuję...</>
                        ) : `▶ Uruchom (${sched.posts_per_day})`}
                      </button>
                      <button
                        onClick={() => generateMap(sched.id)}
                        disabled={generatingMap[sched.id]}
                        title="Odśwież mapę tematyczną"
                        className="px-2 py-1.5 text-gray-500 border border-gray-300 rounded-lg text-xs hover:bg-gray-50 disabled:opacity-50"
                      >
                        {generatingMap[sched.id] ? '...' : '↻ Mapa'}
                      </button>
                      <button
                        onClick={() => syncCategories(sched)}
                        disabled={syncingCats[sched.id]}
                        title="Utwórz kategorie WP z klastrów topical map"
                        className="px-2 py-1.5 text-purple-600 border border-purple-200 rounded-lg text-xs hover:bg-purple-50 disabled:opacity-50"
                      >
                        {syncingCats[sched.id] ? '...' : '⚡ Kategorie WP'}
                      </button>
                    </>
                  )}
                  <button
                    onClick={() => toggleExpand(sched.id)}
                    className="px-2 py-1.5 text-gray-500 border border-gray-300 rounded-lg text-xs hover:bg-gray-50"
                  >
                    {expandedId === sched.id ? '▲ Frazy' : '▼ Frazy'}
                  </button>
                  <button
                    onClick={() => deleteSchedule(sched.id)}
                    className="px-2 py-1.5 text-red-500 border border-red-200 rounded-lg text-xs hover:bg-red-50"
                  >
                    ✕
                  </button>
                </div>
              </div>

              {/* Category sync result */}
              {catResults[sched.id] && (
                <div className={`border-t px-4 py-2 text-xs ${catResults[sched.id].error ? 'bg-red-50 text-red-700' : 'bg-purple-50'}`}>
                  {catResults[sched.id].error ? (
                    <span>Błąd: {catResults[sched.id].error}</span>
                  ) : (
                    <div className="flex flex-wrap gap-2 items-center">
                      <span className="font-medium text-purple-700">
                        ✓ Zsynchronizowano {catResults[sched.id].synced}/{catResults[sched.id].total} kategorii WP:
                      </span>
                      {catResults[sched.id].categories?.map((c, i) => (
                        <span key={i} className={`px-2 py-0.5 rounded-full text-xs font-medium ${c.ok ? 'bg-purple-100 text-purple-700' : 'bg-red-100 text-red-600'}`}>
                          {c.ok ? '✓' : '✗'} {c.label} {c.wp_category_id ? `(ID: ${c.wp_category_id})` : ''}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Run log */}
              {runLog[sched.id] && runLog[sched.id].length > 0 && (
                <div className="border-t border-gray-100 bg-gray-900 px-4 py-3">
                  <div
                    ref={el => logRefs.current[sched.id] = el}
                    className="font-mono text-xs space-y-0.5 max-h-32 overflow-y-auto"
                  >
                    {runLog[sched.id].map((entry, i) => (
                      <div key={i} className={
                        entry.done ? 'text-green-400' :
                        entry.status === 'published' ? 'text-green-300' :
                        entry.status === 'failed' ? 'text-red-400' :
                        entry.status === 'generating' ? 'text-yellow-300' :
                        'text-gray-300'
                      }>
                        {entry.done
                          ? `✓ Gotowe: ${entry.published} opublikowanych, ${entry.failed} błędów`
                          : entry.status === 'published'
                          ? `✓ ${entry.keyword} → ${entry.url} [img: ${entry.image || '?'}]`
                          : entry.status === 'failed'
                          ? `✗ ${entry.keyword}: ${entry.error}`
                          : entry.status === 'generating'
                          ? `⏳ Generuję: ${entry.keyword}`
                          : entry.status === 'publishing'
                          ? `📤 Publikuję: ${entry.title}`
                          : JSON.stringify(entry)
                        }
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* Keywords panel */}
              {expandedId === sched.id && (
                <div className="border-t border-gray-100 p-4">
                  <div className="flex items-center gap-3 mb-3 flex-wrap">
                    <h4 className="text-sm font-semibold text-gray-700">Kolejka fraz</h4>
                    {['', 'pending', 'published', 'failed'].map(s => (
                      <button
                        key={s}
                        onClick={() => loadKeywords(sched.id, s)}
                        className={`text-xs px-2 py-1 rounded ${kwFilter[sched.id] === s ? 'bg-blue-600 text-white' : 'bg-gray-100 text-gray-600 hover:bg-gray-200'}`}
                      >
                        {s || 'Wszystkie'} ({
                          s === '' ? (sched.total_keywords || 0) :
                          s === 'pending' ? ((sched.total_keywords || 0) - (sched.published_count || 0)) :
                          s === 'published' ? (sched.published_count || 0) : '?'
                        })
                      </button>
                    ))}
                  </div>
                  {!keywords[sched.id] ? (
                    <p className="text-sm text-gray-400">Ładowanie...</p>
                  ) : keywords[sched.id].length === 0 ? (
                    <p className="text-sm text-gray-400">Brak fraz w tej kategorii</p>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full text-xs">
                        <thead>
                          <tr className="text-left text-gray-500 border-b border-gray-100">
                            <th className="pb-1 pr-3 font-medium">Fraza</th>
                            <th className="pb-1 pr-3 font-medium">Klaster</th>
                            <th className="pb-1 pr-3 font-medium">Typ</th>
                            <th className="pb-1 pr-3 font-medium">Vol.</th>
                            <th className="pb-1 pr-3 font-medium">KD</th>
                            <th className="pb-1 pr-3 font-medium">Status</th>
                            <th className="pb-1 font-medium">Link</th>
                            <th className="pb-1 font-medium"></th>
                          </tr>
                        </thead>
                        <tbody className="divide-y divide-gray-50">
                          {keywords[sched.id].map(kw => (
                            <tr key={kw.id} className="hover:bg-gray-50">
                              <td className="py-1.5 pr-3 font-medium text-gray-800 max-w-[180px] truncate">{kw.keyword}</td>
                              <td className="py-1.5 pr-3 text-gray-500 max-w-[120px] truncate">{kw.pillar_label || '—'}</td>
                              <td className="py-1.5 pr-3">
                                <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${KW_TYPE_COLOR[kw.keyword_type] || 'bg-gray-100 text-gray-600'}`}>
                                  {kw.keyword_type}
                                </span>
                              </td>
                              <td className="py-1.5 pr-3 text-gray-600">{(kw.search_volume || 0).toLocaleString()}</td>
                              <td className="py-1.5 pr-3">
                                <span className={`${kw.keyword_difficulty > 60 ? 'text-red-600' : kw.keyword_difficulty > 30 ? 'text-yellow-600' : 'text-green-600'} font-medium`}>
                                  {Math.round(kw.keyword_difficulty || 0)}
                                </span>
                              </td>
                              <td className="py-1.5 pr-3">
                                <span className={`px-1.5 py-0.5 rounded-full text-xs font-medium ${STATUS_COLOR[kw.status] || 'bg-gray-100 text-gray-600'}`}>
                                  {kw.status}
                                </span>
                              </td>
                              <td className="py-1.5">
                                {kw.wp_post_url ? (
                                  <a href={kw.wp_post_url} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline">link</a>
                                ) : '—'}
                              </td>
                              <td className="py-1.5">
                                {kw.status === 'failed' && (
                                  <button
                                    onClick={() => retryKeyword(kw.id, sched.id)}
                                    className="text-xs px-2 py-0.5 bg-orange-100 text-orange-700 rounded hover:bg-orange-200"
                                  >↺ retry</button>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      </>}
    </div>
  )
}
