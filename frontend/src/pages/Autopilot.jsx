import { useState, useEffect, useRef, useCallback } from 'react'
import api from '../api/client'

const STATUS_COLOR = {
  pending: 'bg-yellow-100 text-yellow-700',
  published: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  cannibal_risk: 'bg-orange-100 text-orange-700',
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
  const [customPrompt, setCustomPrompt] = useState('')
  const [runLimit, setRunLimit] = useState(1)

  const [csvImporting, setCsvImporting] = useState(false)
  const csvInputRef = useRef(null)

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

  const handleCsvImport = async (e) => {
    const file = e.target.files[0]
    if (!file) return
    setCsvImporting(true)
    try {
      const text = await file.text()
      const lines = text.split('\n').map(l => l.trim()).filter(Boolean)
      if (lines.length < 2) { log('CSV jest pusty', 'err'); return }
      // Detect delimiter
      const delim = lines[0].includes(';') ? ';' : ','
      const headers = lines[0].split(delim).map(h => h.replace(/^\uFEFF/, '').trim().toLowerCase())
      const col = (name, aliases = []) => {
        const idx = [name, ...aliases].map(a => headers.indexOf(a)).find(i => i >= 0)
        return idx !== undefined ? idx : -1
      }
      const iDomain = col('domena', ['domain'])
      const iLogin = col('login wp', ['login_wp', 'wp_login', 'login'])
      const iPass = col('haslo aplikacji', ['password', 'wp_pass', 'pass', 'haslo'])
      const iServer = col('serwer', ['server'])
      if (iDomain < 0 || iLogin < 0 || iPass < 0) {
        log('CSV musi mieć kolumny: Domena, Login WP, Haslo Aplikacji', 'err'); return
      }
      const items = []
      for (const line of lines.slice(1)) {
        const cols = line.split(delim).map(c => c.trim().replace(/^"|"$/g, ''))
        const domain = cols[iDomain]
        if (!domain) continue
        items.push({
          domain,
          wp_login: cols[iLogin] || '',
          wp_pass: cols[iPass] || '',
          server: iServer >= 0 ? (cols[iServer] || '') : '',
          active: 1,
        })
      }
      if (items.length === 0) { log('Brak danych w CSV', 'err'); return }
      const res = await api.post('/api/domains/bulk-import', items)
      log(`✓ Zaimportowano ${res.data.inserted} domen (pominięto ${res.data.skipped} duplikatów)`, 'ok')
      await load()
    } catch (err) {
      log(`✗ Błąd importu: ${err.response?.data?.detail || err.message}`, 'err')
    } finally {
      setCsvImporting(false)
      e.target.value = ''
    }
  }

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
        custom_prompt: customPrompt,
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
            {tab === 'domains' && (
              <>
                <input
                  ref={csvInputRef}
                  type="file"
                  accept=".csv"
                  className="hidden"
                  onChange={handleCsvImport}
                />
                <button
                  onClick={() => csvInputRef.current?.click()}
                  disabled={csvImporting}
                  className="px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 disabled:opacity-50 flex items-center gap-1"
                >
                  {csvImporting ? (
                    <><span className="animate-spin inline-block w-3 h-3 border-2 border-white border-t-transparent rounded-full" />Importuję...</>
                  ) : '↑ Import CSV'}
                </button>
              </>
            )}
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
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Custom prompt (opcjonalnie)</label>
                  <textarea value={customPrompt} onChange={e => setCustomPrompt(e.target.value)}
                    rows={3}
                    placeholder="Dodatkowe wskazówki dla AI, np. styl, ton, wymagania..."
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none" />
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
  const [cannibModal, setCannibModal] = useState(null) // { scheduleId, data } | null
  const [cannibLoading, setCannibLoading] = useState({})
  const [newForm, setNewForm] = useState({
    my_domain_id: '',
    seed_keyword: '',
    posts_per_day: 1,
    language: 'pl',
    min_volume: 10,
    client_domain: '',
    anchor_text: '',
    image_source: 'freepik_stock',
    custom_prompt: '',
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
      setNewForm({ my_domain_id: '', seed_keyword: '', posts_per_day: 1, language: 'pl', min_volume: 10, client_domain: '', anchor_text: '', image_source: 'freepik_stock', custom_prompt: '' })
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
      setRunLog(l => ({ ...l, [id]: [{ status: 'info', keyword: '—', error: `Mapa gotowa: ${res.data.pillars} klastrów, ${res.data.total_keywords} fraz` }] }))
      await load()
    } catch (e) {
      setRunLog(l => ({ ...l, [id]: [{ status: 'failed', keyword: '—', error: 'Błąd mapy: ' + (e.response?.data?.detail || e.message) }] }))
    } finally {
      setGeneratingMap(g => ({ ...g, [id]: false }))
    }
  }

  const checkCannibalization = async (id) => {
    setCannibLoading(c => ({ ...c, [id]: true }))
    try {
      const res = await api.get(`/api/autopilot/schedules/${id}/cannibalization`)
      setCannibModal({ scheduleId: id, data: res.data })
    } catch {
      // silent
    } finally {
      setCannibLoading(c => ({ ...c, [id]: false }))
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

  const _pollJob = async (id, job_id) => {
    let lastCount = 0
    while (true) {
      await new Promise(r => setTimeout(r, 3000))
      const statusRes = await api.get(`/api/autopilot/schedules/${id}/run-status/${job_id}`)
      const data = statusRes.data
      const entries = data.results || []
      if (entries.length > lastCount) {
        setRunLog(l => ({ ...l, [id]: entries }))
        lastCount = entries.length
        setTimeout(() => {
          const el = logRefs.current[id]
          if (el) el.scrollTop = el.scrollHeight
        }, 50)
      }
      if (data.done) {
        if (data.error) {
          setRunLog(l => ({ ...l, [id]: [...(data.results || []), { status: 'failed', keyword: '—', error: data.error }] }))
        } else {
          const finalEntries = data.results || []
          if (finalEntries.length === 0 && data.message) {
            finalEntries.push({ status: 'info', keyword: '—', error: data.message })
          }
          setRunLog(l => ({ ...l, [id]: [...finalEntries, { done: true, published: data.published, failed: data.failed }] }))
        }
        break
      }
    }
  }

  const runNow = async (sched, limitOverride = null) => {
    const id = sched.id
    setRunning(r => ({ ...r, [id]: true }))
    setRunLog(l => ({ ...l, [id]: [] }))

    const limit = limitOverride || sched.posts_per_day
    try {
      const res = await api.post(`/api/autopilot/schedules/${id}/run`, { schedule_id: id, limit })
      const { job_id } = res.data
      if (!job_id) throw new Error('Brak job_id w odpowiedzi')
      await _pollJob(id, job_id)
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

  const retryAllFailed = async (sched) => {
    const id = sched.id
    const failed_count = keywords[id]?.filter(k => k.status === 'failed').length || 0
    if (failed_count === 0) { alert('Brak failed keywords do ponowienia.'); return }
    if (!confirm(`Ponowić ${failed_count} failed keywords dla ${sched.domain}?`)) return
    setRunning(r => ({ ...r, [id]: true }))
    try {
      const res = await api.post(`/api/autopilot/schedules/${id}/retry-failed`)
      const { job_id, reset } = res.data
      if (job_id) {
        setRunLog(l => ({ ...l, [id]: [{ status: 'info', keyword: '—', error: `Zresetowano ${reset} failed → pending. Uruchamiam...` }] }))
        await _pollJob(id, job_id)
        await load()
        if (expandedId === id) await loadKeywords(id, kwFilter[id] || '')
      }
    } catch (e) {
      alert('Błąd retry-all: ' + (e.response?.data?.detail || e.message))
    } finally {
      setRunning(r => ({ ...r, [id]: false }))
    }
  }

  const deleteSchedule = async (id) => {
    if (!confirm('Usunąć harmonogram i wszystkie frazy?')) return
    await api.delete(`/api/autopilot/schedules/${id}`)
    await load()
  }

  const exportCsv = (id) => {
    const BASE = import.meta.env.VITE_API_URL || ''
    const token = localStorage.getItem('pbn_auth_token')
    const headers = token ? `&_auth=${encodeURIComponent(token)}` : ''
    // Use fetch + blob to keep auth header
    fetch(`${BASE}/api/autopilot/schedules/${id}/export-csv`, {
      headers: token ? { Authorization: `Basic ${token}` } : {},
    }).then(r => r.blob()).then(blob => {
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `keywords_${id}.csv`
      a.click()
      URL.revokeObjectURL(url)
    }).catch(e => alert('Błąd eksportu CSV: ' + e.message))
  }

  const updatePpd = async (id, val) => {
    await api.patch(`/api/autopilot/schedules/${id}`, { posts_per_day: Number(val) })
    await load()
  }

  const updateImageSource = async (id, val) => {
    await api.patch(`/api/autopilot/schedules/${id}`, { image_source: val })
    await load()
  }

  const updateCustomPrompt = async (id, val) => {
    await api.patch(`/api/autopilot/schedules/${id}`, { custom_prompt: val })
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
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Źródło zdjęć</label>
              <select value={newForm.image_source} onChange={e => set('image_source', e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                <option value="freepik_stock">📷 Freepik Stock (wyszukiwanie)</option>
                <option value="freepik_zimage">⚡ Freepik Z-Image (generowanie)</option>
                <option value="freepik_flux">🌊 Freepik Flux Pro 1.1 (generowanie)</option>
                <option value="dalle">🎨 DALL-E 3 (~$0.04/zdjęcie)</option>
                <option value="none">🚫 Bez zdjęcia</option>
              </select>
            </div>
            <div className="sm:col-span-2 lg:col-span-3">
              <label className="block text-xs font-medium text-gray-600 mb-1">Custom prompt (opcjonalnie)</label>
              <textarea value={newForm.custom_prompt} onChange={e => set('custom_prompt', e.target.value)}
                rows={2}
                placeholder="Dodatkowe wskazówki dla AI — styl, ton, wymagania branżowe..."
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none" />
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

                {/* Image source */}
                <div className="flex items-center gap-1 shrink-0">
                  <select
                    defaultValue={sched.image_source || 'freepik_stock'}
                    onChange={e => updateImageSource(sched.id, e.target.value)}
                    className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                    title="Źródło zdjęć"
                  >
                    <option value="freepik_stock">📷 Stock</option>
                    <option value="freepik_zimage">⚡ Z-Image</option>
                    <option value="freepik_flux">🌊 Flux</option>
                    <option value="dalle">🎨 DALL-E</option>
                    <option value="none">🚫 Brak</option>
                  </select>
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
                      {keywords[sched.id]?.some(k => k.status === 'failed') && (
                        <button
                          onClick={() => retryAllFailed(sched)}
                          disabled={running[sched.id]}
                          title="Ponów wszystkie failed keywords"
                          className="px-2 py-1.5 text-red-600 border border-red-200 rounded-lg text-xs hover:bg-red-50 disabled:opacity-50"
                        >
                          ↺ Retry failed
                        </button>
                      )}
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
                      <button
                        onClick={() => checkCannibalization(sched.id)}
                        disabled={cannibLoading[sched.id]}
                        title="Sprawdź kanibalizację fraz na tej domenie"
                        className="px-2 py-1.5 text-orange-600 border border-orange-200 rounded-lg text-xs hover:bg-orange-50 disabled:opacity-50"
                      >
                        {cannibLoading[sched.id] ? '...' : '⚠ Kanibalizacja'}
                      </button>
                    </>
                  )}
                  <button
                    onClick={() => toggleExpand(sched.id)}
                    className="px-2 py-1.5 text-gray-500 border border-gray-300 rounded-lg text-xs hover:bg-gray-50"
                  >
                    {expandedId === sched.id ? '▲ Frazy' : '▼ Frazy'}
                  </button>
                  {sched.map_generated && (
                    <button
                      onClick={() => exportCsv(sched.id)}
                      title="Eksportuj keyword map do CSV"
                      className="px-2 py-1.5 text-green-600 border border-green-200 rounded-lg text-xs hover:bg-green-50"
                    >
                      ↓ CSV
                    </button>
                  )}
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
                  {/* Custom prompt for this schedule */}
                  <div className="mb-3">
                    <label className="block text-xs font-medium text-gray-600 mb-1">Custom prompt (opcjonalnie)</label>
                    <textarea
                      defaultValue={sched.custom_prompt || ''}
                      onBlur={e => updateCustomPrompt(sched.id, e.target.value)}
                      rows={2}
                      placeholder="Dodatkowe wskazówki dla AI — styl, ton, wymagania branżowe..."
                      className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                    />
                  </div>
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

      {/* Cannibalization modal */}
      {cannibModal && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setCannibModal(null)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[80vh] flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
              <div>
                <h3 className="font-bold text-gray-900">Kanibalizacja fraz</h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  {cannibModal.data.collision_groups} grup kolidujących · {cannibModal.data.total_keywords} fraz łącznie
                </p>
              </div>
              <button onClick={() => setCannibModal(null)} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
            </div>
            <div className="overflow-y-auto flex-1 px-6 py-4 space-y-3">
              {cannibModal.data.collisions.length === 0 ? (
                <div className="text-center py-8 text-gray-400 text-sm">Brak wykrytych kolizji</div>
              ) : cannibModal.data.collisions.map((group, i) => (
                <div key={i} className="border border-orange-100 rounded-xl p-4 bg-orange-50">
                  <div className="flex items-center justify-between mb-2">
                    <span className="font-semibold text-orange-800 text-sm">"{group.stem}..."</span>
                    <div className="flex gap-2 text-xs">
                      <span className="bg-green-100 text-green-700 px-2 py-0.5 rounded-full">{group.published} opublikowane</span>
                      <span className="bg-yellow-100 text-yellow-700 px-2 py-0.5 rounded-full">{group.pending} oczekujące</span>
                    </div>
                  </div>
                  <div className="space-y-1">
                    {group.keywords.map(kw => (
                      <div key={kw.id} className="flex items-center gap-2 text-xs">
                        <span className={`w-2 h-2 rounded-full shrink-0 ${kw.status === 'published' ? 'bg-green-500' : kw.status === 'pending' ? 'bg-yellow-400' : 'bg-gray-300'}`} />
                        <span className="text-gray-800 font-medium flex-1 truncate">{kw.keyword}</span>
                        <span className="text-gray-400 uppercase">{kw.type}</span>
                        {kw.url ? (
                          <a href={kw.url} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline shrink-0">↗</a>
                        ) : <span className="text-gray-300">—</span>}
                      </div>
                    ))}
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
