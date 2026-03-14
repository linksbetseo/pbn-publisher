import { useState, useEffect, useRef, useCallback } from 'react'
import api from '../api/client'
import { useToast } from '../components/Toast'
import { sanitizeHtml } from '../sanitize'

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

function Spinner({ className = 'w-5 h-5' }) {
  return (
    <svg className={`animate-spin ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

// ── Bulk Tab ──────────────────────────────────────────────────────────────────

function BulkTab() {
  const addToast = useToast()
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
  const [customPrompt, setCustomPrompt] = useState('')
  const [runLimit, setRunLimit] = useState(1)
  const [autoSeed, setAutoSeed] = useState(true)

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

  // Build domain→schedule lookup (domain may have multiple schedules)
  const schedByDomain = {}
  schedules.forEach(s => {
    const did = s.my_domain_id
    if (!schedByDomain[did]) schedByDomain[did] = []
    schedByDomain[did].push(s)
  })

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
    if (!selectedList.length) return addToast('Zaznacz domeny', 'warning')
    if (!autoSeed && !seedKw.trim()) return addToast('Wpisz frazę seed lub włącz auto-seed', 'warning')
    setRunning(true)
    setActionLog([])

    if (autoSeed) {
      log(`AI dobiera seed keywords dla ${selectedList.length} domen... (to może potrwać kilka minut)`)
      try {
        const res = await api.post('/api/autopilot/bulk-create-auto', {
          domain_ids: selectedList,
          posts_per_day: Number(ppd),
          language: lang,
          min_volume: Number(minVol),
          custom_prompt: customPrompt,
        }, { timeout: 600000 })
        res.data.results?.forEach(r => {
          if (r.error) log(`  ✗ ${r.domain}: ${r.error}`, 'err')
          else log(`  ✓ ${r.domain}: seed "${r.seed_keyword}"`, 'ok')
        })
        log(`✓ Utworzono: ${res.data.created}, pominięto: ${res.data.skipped}, błędy: ${res.data.errors}`, 'ok')
        await load()
        setSelected(new Set())
      } catch (e) {
        log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
      }
    } else {
      log(`Tworzę harmonogramy dla ${selectedList.length} domen z seed "${seedKw}"...`)
      try {
        const res = await api.post('/api/autopilot/bulk-create', {
          domain_ids: selectedList,
          seed_keyword: seedKw,
          posts_per_day: Number(ppd),
          language: lang,
          min_volume: Number(minVol),
          custom_prompt: customPrompt,
        })
        log(`✓ Utworzono: ${res.data.created}, pominięto: ${res.data.skipped}, błędy: ${res.data.errors}`, 'ok')
        await load()
        setSelected(new Set())
      } catch (e) {
        log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
      }
    }
    setRunning(false)
  }

  const bulkGenerateMaps = async () => {
    if (!selectedList.length) return addToast('Zaznacz harmonogramy', 'warning')
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
    if (!selectedList.length) return addToast('Zaznacz harmonogramy', 'warning')
    setRunning(true)
    setActionLog([])
    log(`Uruchamiam publikację dla ${selectedList.length} harmonogramów (${runLimit} artykuł/domena)...`)
    try {
      const res = await api.post('/api/autopilot/bulk-run', { schedule_ids: selectedList, limit: Number(runLimit) })
      const { job_id } = res.data
      if (!job_id) throw new Error('Brak job_id w odpowiedzi')
      log(`Job ${job_id.slice(0, 8)}... uruchomiony w tle`)
      // Poll status every 5s
      let lastResultsLen = 0
      while (true) {
        await new Promise(r => setTimeout(r, 5000))
        const st = await api.get(`/api/autopilot/bulk-run-status/${job_id}`)
        const data = st.data
        const results = data.results || []
        // Log new results as they come in
        if (results.length > lastResultsLen) {
          results.slice(lastResultsLen).forEach(r => {
            if (r.skipped) log(`  ⚠ ${r.domain}: pominięto (${r.reason})`, 'warn')
            else if (r.error) log(`  ✗ ${r.domain || r.schedule_id}: ${r.error}`, 'err')
            else log(`  ✓ ${r.domain}: ${r.published} pub, ${r.failed} err`, r.failed > 0 ? 'warn' : 'ok')
          })
          lastResultsLen = results.length
        }
        if (data.done) {
          if (data.error) {
            log(`✗ Błąd: ${data.error}`, 'err')
          } else {
            log(`✓ Zakończono: ${data.published || 0} opublikowanych, ${data.failed || 0} błędów`, 'ok')
          }
          break
        }
      }
      await load()
    } catch (e) {
      log(`✗ Błąd: ${e.response?.data?.detail || e.message}`, 'err')
    } finally {
      setRunning(false)
    }
  }

  const bulkSetPpd = async () => {
    if (!selectedList.length) return addToast('Zaznacz harmonogramy', 'warning')
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
                    {tab === 'domains' && <th className="px-3 py-2 text-left font-semibold text-gray-500 uppercase tracking-wide">Harmonogram</th>}
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
                      {tab === 'domains' && (
                        <td className="px-3 py-2">
                          {schedByDomain[item.id] ? (
                            <div className="space-y-0.5">
                              {schedByDomain[item.id].map(sc => (
                                <div key={sc.id} className="flex items-center gap-1.5 flex-wrap">
                                  <span className="inline-block px-1.5 py-0.5 bg-purple-100 text-purple-700 rounded text-[10px] font-medium max-w-[100px] truncate" title={sc.seed_keyword}>
                                    {sc.seed_keyword}
                                  </span>
                                  <span className="text-[10px] text-gray-400">
                                    {sc.published_count || 0}/{sc.total_keywords || 0}
                                  </span>
                                  {sc.map_generated
                                    ? <span className="text-green-500 text-[10px]" title="Mapa wygenerowana">&#10003;</span>
                                    : <span className="text-orange-400 text-[10px]" title="Brak mapy">&#9679;</span>}
                                </div>
                              ))}
                            </div>
                          ) : (
                            <span className="text-gray-300 text-[10px]">—</span>
                          )}
                        </td>
                      )}
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
                    <tr><td colSpan={6} className="px-3 py-8 text-center text-gray-400">Brak wyników</td></tr>
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

            <div className="bg-blue-50 rounded-lg p-3 mb-2 border border-blue-100">
              <label className="flex items-center gap-2 text-xs font-semibold text-blue-800 cursor-pointer">
                <input type="checkbox" checked={autoSeed} onChange={e => setAutoSeed(e.target.checked)}
                  className="rounded border-blue-300 text-blue-600" />
                Auto-Seed z DataForSEO
              </label>
              {autoSeed ? (
                <p className="text-xs text-blue-600 mt-1.5">
                  System przeanalizuje każdą domenę przez DataForSEO API — automatycznie dobierze najlepszą niszę i seed keyword na podstawie istniejących rankingów i tematyki domeny.
                </p>
              ) : (
                <p className="text-xs text-gray-400 mt-1.5">
                  Wyłączony — musisz podać seed keyword ręcznie.
                </p>
              )}
            </div>

            {tab === 'domains' && (
              <>
                {!autoSeed && (
                  <div>
                    <label className="block text-xs font-medium text-gray-500 mb-1">Fraza seed *</label>
                    <input value={seedKw} onChange={e => setSeedKw(e.target.value)}
                      placeholder="np. prawo pracy"
                      className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                  </div>
                )}
                <div>
                  <label className="block text-xs font-medium text-gray-500 mb-1">Custom prompt (opcjonalnie)</label>
                  <textarea value={customPrompt} onChange={e => setCustomPrompt(e.target.value)}
                    rows={2}
                    placeholder="Dodatkowe wskazówki dla AI..."
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
                  {running ? <><Spinner className="w-4 h-4 inline mr-1" /> Tworzę...</> : `+ Utwórz harmonogramy (${selectedList.length})`}
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

function AutopilotLogs() {
  const [logs, setLogs] = useState([])
  const [loading, setLoading] = useState(true)
  const [search, setSearch] = useState('')

  useEffect(() => {
    setLoading(true)
    api.get('/api/autopilot/logs').then(r => setLogs(r.data)).catch(() => {}).finally(() => setLoading(false))
  }, [])

  const filtered = search
    ? logs.filter(l => l.keyword?.toLowerCase().includes(search.toLowerCase()) || l.domain?.toLowerCase().includes(search.toLowerCase()))
    : logs

  return (
    <div>
      <div className="flex items-center gap-3 mb-4">
        <input
          type="text"
          placeholder="Szukaj po keyword lub domenie..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-64 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <span className="text-sm text-gray-400">{filtered.length} wpisów</span>
      </div>
      {loading ? (
        <div className="flex items-center justify-center h-40 text-gray-400">
          <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 mr-2" />Ładowanie...
        </div>
      ) : filtered.length === 0 ? (
        <div className="text-center py-12 text-gray-400">Brak logów</div>
      ) : (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="border-b border-gray-100 bg-gray-50">
              <tr>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Data</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Domena</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Seed</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Keyword</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Status</th>
                <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">URL</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {filtered.map(l => (
                <tr key={l.id} className="hover:bg-gray-50">
                  <td className="px-4 py-2 text-gray-500 text-xs whitespace-nowrap">
                    {l.published_at ? new Date(l.published_at).toLocaleString('pl-PL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}
                  </td>
                  <td className="px-4 py-2 text-gray-700 text-xs font-medium">{l.domain}</td>
                  <td className="px-4 py-2 text-purple-600 text-xs">{l.seed_keyword}</td>
                  <td className="px-4 py-2 text-gray-800 text-xs max-w-[200px] truncate" title={l.keyword}>{l.keyword}</td>
                  <td className="px-4 py-2">
                    <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${l.status === 'published' ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-700'}`}>
                      {l.status}
                    </span>
                  </td>
                  <td className="px-4 py-2">
                    {l.wp_post_url ? (
                      <a href={l.wp_post_url} target="_blank" rel="noreferrer" className="text-blue-600 hover:underline text-xs">link</a>
                    ) : <span className="text-gray-300 text-xs">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  )
}

export default function Autopilot() {
  const addToast = useToast()
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
  const [kwTypeFilter, setKwTypeFilter] = useState({})
  const [cannibModal, setCannibModal] = useState(null) // { scheduleId, data } | null
  const [cannibLoading, setCannibLoading] = useState({})
  const [siteMetrics, setSiteMetrics] = useState({})
  const [previewModal, setPreviewModal] = useState(null) // { keyword, title, content, excerpt } | null
  const [previewLoading, setPreviewLoading] = useState({})
  const [toneGenerating, setToneGenerating] = useState({})
  const [toneModal, setToneModal] = useState(null) // { scheduleId, tone, isGenerated } | null
  const [toneEditing, setToneEditing] = useState('')
  const [newForm, setNewForm] = useState({
    my_domain_id: '',
    seed_keyword: '',
    posts_per_day: 1,
    language: 'pl',
    min_volume: 10,
    min_coherence: 0,
    image_source: 'freepik_flux',
    custom_prompt: '',
    tone_of_voice: 'ekspert',
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
    // Auto-load site metrics for schedules with map
    schRes.data.filter(s => s.map_generated).forEach(s => {
      api.get(`/api/autopilot/schedules/${s.id}/site-metrics`).then(r => {
        if (r.data.site_metrics && Object.keys(r.data.site_metrics).length)
          setSiteMetrics(m => ({ ...m, [s.id]: r.data.site_metrics }))
      }).catch(() => {})
    })
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
        min_coherence: Number(newForm.min_coherence),
      })
      setShowAdd(false)
      setNewForm({ my_domain_id: '', seed_keyword: '', posts_per_day: 1, language: 'pl', min_volume: 10, min_coherence: 0, image_source: 'freepik_flux', custom_prompt: '', tone_of_voice: 'ekspert' })
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
      if (res.data.site_metrics) setSiteMetrics(m => ({ ...m, [id]: res.data.site_metrics }))
      setRunLog(l => ({ ...l, [id]: [{ status: 'info', keyword: '—', error: `Mapa gotowa: ${res.data.pillars} klastrów, ${res.data.total_keywords} fraz` }] }))
      await load()
    } catch (e) {
      setRunLog(l => ({ ...l, [id]: [{ status: 'failed', keyword: '—', error: 'Błąd mapy: ' + (e.response?.data?.detail || e.message) }] }))
    } finally {
      setGeneratingMap(g => ({ ...g, [id]: false }))
    }
  }

  const fetchMetrics = async (id, recalc = false) => {
    try {
      if (recalc) await api.post(`/api/autopilot/schedules/${id}/recalculate-coherence`)
      const res = await api.get(`/api/autopilot/schedules/${id}/site-metrics`)
      if (res.data.site_metrics) setSiteMetrics(m => ({ ...m, [id]: res.data.site_metrics }))
    } catch {
      // silent
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

  const pending_count = (id) => keywords[id]?.filter(k => k.status === 'pending').length ?? '?'

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

  const runAll = async (sched) => {
    const id = sched.id
    const total = pending_count(id)
    if (total === '?' || total === 0) { addToast('Brak pending keywords do opublikowania.', 'warning'); return }
    const estMinutes = Math.ceil(total * 3)
    if (!confirm(
      `⚠️ UWAGA: Opublikować WSZYSTKIE ${total} artykułów dla ${sched.domain}?\n\n` +
      `Szacowany czas: ~${estMinutes} min (${Math.ceil(estMinutes/60)} godz.)\n` +
      `Koszt: ${total * 10} kredytów\n\n` +
      `Tej operacji nie można cofnąć. Kontynuować?`
    )) return
    setRunning(r => ({ ...r, [id]: true }))
    setRunLog(l => ({ ...l, [id]: [] }))
    try {
      const res = await api.post(`/api/autopilot/schedules/${id}/run-all`)
      const { job_id, total_pending, message } = res.data
      if (!job_id) {
        setRunLog(l => ({ ...l, [id]: [{ status: 'info', keyword: '—', error: message || 'Brak pending keywords' }] }))
        return
      }
      setRunLog(l => ({ ...l, [id]: [{ status: 'info', keyword: '—', error: `Uruchamiam ${total_pending} keywords...` }] }))
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
      addToast('Błąd retry: ' + (e.response?.data?.detail || e.message), 'error')
    }
  }

  const previewKeyword = async (kwId) => {
    setPreviewLoading(l => ({ ...l, [kwId]: true }))
    try {
      const res = await api.post(`/api/autopilot/keywords/${kwId}/preview`)
      setPreviewModal(res.data)
    } catch (e) {
      addToast('Błąd podglądu: ' + (e.response?.data?.detail || e.message), 'error')
    } finally {
      setPreviewLoading(l => ({ ...l, [kwId]: false }))
    }
  }

  const retryAllFailed = async (sched) => {
    const id = sched.id
    const failed_count = keywords[id]?.filter(k => k.status === 'failed').length || 0
    if (failed_count === 0) { addToast('Brak failed keywords do ponowienia.', 'warning'); return }
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
      addToast('Błąd retry-all: ' + (e.response?.data?.detail || e.message), 'error')
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
    }).catch(e => addToast('Błąd eksportu CSV: ' + e.message, 'error'))
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

  const updateMinCoherence = async (id, val) => {
    await api.patch(`/api/autopilot/schedules/${id}`, { min_coherence: Number(val) })
    await load()
  }

  const updateTone = async (id, val) => {
    await api.patch(`/api/autopilot/schedules/${id}`, { tone_of_voice: val })
    await load()
  }

  const generateTone = async (id) => {
    setToneGenerating(g => ({ ...g, [id]: true }))
    try {
      await api.post(`/api/autopilot/schedules/${id}/generate-tone`)
      addToast('Generowanie Tone of Voice rozpoczęte...', 'info')
      // Poll for completion (tone changes from preset to long text)
      let attempts = 0
      const poll = setInterval(async () => {
        attempts++
        try {
          const res = await api.get(`/api/autopilot/schedules/${id}/tone`)
          if (res.data.is_generated || attempts > 60) {
            clearInterval(poll)
            setToneGenerating(g => ({ ...g, [id]: false }))
            if (res.data.is_generated) {
              addToast('Tone of Voice wygenerowany!', 'success')
            }
            await load()
          }
        } catch { /* ignore poll errors */ }
      }, 5000)
    } catch (e) {
      addToast('Błąd generowania tone: ' + (e.response?.data?.detail || e.message), 'error')
      setToneGenerating(g => ({ ...g, [id]: false }))
    }
  }

  const viewTone = async (id) => {
    try {
      const res = await api.get(`/api/autopilot/schedules/${id}/tone`)
      setToneModal({ scheduleId: id, tone: res.data.tone_of_voice || '', isGenerated: res.data.is_generated })
      setToneEditing(res.data.tone_of_voice || '')
    } catch (e) {
      addToast('Błąd pobierania tone', 'error')
    }
  }

  const saveToneEdit = async () => {
    if (!toneModal) return
    await api.patch(`/api/autopilot/schedules/${toneModal.scheduleId}`, { tone_of_voice: toneEditing })
    addToast('Tone of Voice zapisany', 'success')
    setToneModal(null)
    await load()
  }

  const set = (f, v) => setNewForm(n => ({ ...n, [f]: v }))
  const fmt = (s) => s ? new Date(s).toLocaleString('pl-PL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'

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
        {[['schedules', 'Harmonogramy'], ['bulk', '⚡ Bulk Setup'], ['logs', 'Logi']].map(([k, label]) => (
          <button key={k} onClick={() => setMainTab(k)}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${mainTab === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>
            {label}
          </button>
        ))}
        <button
          onClick={() => {
            const BASE = import.meta.env.VITE_API_URL || ''
            const token = localStorage.getItem('pbn_auth_token')
            fetch(`${BASE}/api/autopilot/export-csv`, {
              headers: token ? { Authorization: `Basic ${token}` } : {},
            }).then(r => r.blob()).then(blob => {
              const a = document.createElement('a')
              a.href = URL.createObjectURL(blob)
              a.download = 'autopilot_all_keywords.csv'
              a.click()
            }).catch(e => addToast('Błąd: ' + e.message, 'error'))
          }}
          className="ml-2 px-3 py-2 text-xs text-gray-600 hover:text-blue-600 hover:bg-white rounded-md transition-colors"
          title="Eksportuj wszystkie frazy do CSV"
        >
          ↓ CSV
        </button>
      </div>

      {mainTab === 'bulk' && <BulkTab />}
      {mainTab === 'logs' && <AutopilotLogs />}
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
              <label className="block text-xs font-medium text-gray-600 mb-1" title="SiteRadius filter: odrzuca frazy zbyt odległe od seed. 0=brak filtracji, 0.1=łagodny, 0.3=umiarkowany, 0.5=surowy">
                Min. spójność (SiteRadius) <span className="text-gray-400 font-normal">0–0.5</span>
              </label>
              <select value={newForm.min_coherence} onChange={e => set('min_coherence', e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                <option value="0">0 — brak filtracji (max frazy)</option>
                <option value="0.1">0.1 — łagodny (~-10% fraz)</option>
                <option value="0.2">0.2 — lekki (~-20% fraz)</option>
                <option value="0.3">0.3 — umiarkowany (~-30% fraz)</option>
                <option value="0.5">0.5 — surowy (~-50% fraz)</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Źródło zdjęć</label>
              <select value={newForm.image_source} onChange={e => set('image_source', e.target.value)} className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                <option value="freepik_flux">🌊 Flux Pro 1.1 (AI — rekomendowane)</option>
                <option value="freepik_zimage">⚡ Z-Image Turbo (AI — szybsze)</option>
                <option value="freepik_stock">📷 Freepik Stock (wyszukiwanie)</option>
                <option value="gemini">🤖 Gemini (AI — darmowe)</option>
                <option value="dalle">🎨 DALL-E 3 (AI — ~$0.04/zdjęcie)</option>
                <option value="none">🚫 Bez zdjęcia</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Ton artykułu</label>
              <div className="text-xs text-gray-500 border border-gray-200 rounded-lg px-3 py-2 bg-gray-50">
                Tone of Voice zostanie wygenerowany automatycznie przez AI po dodaniu domeny. Kliknij "Generuj Tone" w harmonogramie.
              </div>
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
              {/* Row 1: Toggle + Domain info */}
              <div className="flex items-center gap-3 px-4 pt-4 pb-2">
                <button
                  onClick={() => toggleActive(sched)}
                  className={`w-10 h-6 rounded-full transition-colors flex-shrink-0 ${sched.active ? 'bg-green-500' : 'bg-gray-300'}`}
                  title={sched.active ? 'Aktywny — kliknij żeby wstrzymać' : 'Wstrzymany — kliknij żeby aktywować'}
                >
                  <div className={`w-4 h-4 rounded-full bg-white shadow mx-1 transition-transform ${sched.active ? 'translate-x-4' : 'translate-x-0'}`} />
                </button>
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
                  </div>
                  {siteMetrics[sched.id] && (
                    <div className="flex gap-3 mt-1 flex-wrap text-xs">
                      <span title="SiteFocus: koncentracja semantyczna klastrów (wyżej = lepiej)" className={`px-2 py-0.5 rounded-full font-medium ${siteMetrics[sched.id].focus_rating === 'excellent' ? 'bg-green-100 text-green-700' : siteMetrics[sched.id].focus_rating === 'good' ? 'bg-blue-100 text-blue-700' : siteMetrics[sched.id].focus_rating === 'fair' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-600'}`}>
                        Focus: {(siteMetrics[sched.id].site_focus * 100).toFixed(0)}% ({siteMetrics[sched.id].focus_rating})
                      </span>
                      <span title="SiteRadius: dryf semantyczny od rdzenia (niżej = lepiej)" className={`px-2 py-0.5 rounded-full font-medium ${siteMetrics[sched.id].radius_rating === 'tight' ? 'bg-green-100 text-green-700' : siteMetrics[sched.id].radius_rating === 'moderate' ? 'bg-blue-100 text-blue-700' : siteMetrics[sched.id].radius_rating === 'wide' ? 'bg-yellow-100 text-yellow-700' : 'bg-red-100 text-red-600'}`}>
                        Radius: {(siteMetrics[sched.id].site_radius * 100).toFixed(0)}% ({siteMetrics[sched.id].radius_rating})
                      </span>
                      <span className="text-gray-400">{siteMetrics[sched.id].total_articles} artykułów</span>
                    </div>
                  )}
                </div>
                <button
                  onClick={() => deleteSchedule(sched.id)}
                  className="px-2 py-1.5 text-red-500 border border-red-200 rounded-lg text-xs hover:bg-red-50 shrink-0"
                  title="Usuń harmonogram"
                >
                  ✕
                </button>
              </div>

              {/* Row 2: Settings + Actions */}
              <div className="flex items-center gap-2 px-4 pb-3 flex-wrap">
                {/* Settings */}
                <div className="flex items-center gap-1">
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
                <select
                  defaultValue={sched.min_coherence ?? 0}
                  onChange={e => updateMinCoherence(sched.id, e.target.value)}
                  className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                  title="SiteRadius filter: min. spójność frazy z seed keyword (0=brak, 0.5=surowy)"
                >
                  <option value="0">Spójność: 0</option>
                  <option value="0.1">Spójność: 0.1</option>
                  <option value="0.2">Spójność: 0.2</option>
                  <option value="0.3">Spójność: 0.3</option>
                  <option value="0.5">Spójność: 0.5</option>
                </select>
                <select
                  defaultValue={sched.image_source || 'freepik_flux'}
                  onChange={e => updateImageSource(sched.id, e.target.value)}
                  className="border border-gray-300 rounded px-2 py-1 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500"
                  title="Źródło zdjęć (AI-generowane)"
                >
                  <option value="freepik_flux">🌊 Flux</option>
                  <option value="freepik_zimage">⚡ Z-Image</option>
                  <option value="freepik_stock">📷 Stock</option>
                  <option value="gemini">🤖 Gemini</option>
                  <option value="dalle">🎨 DALL-E</option>
                  <option value="none">🚫 Brak</option>
                </select>
                <div className="flex items-center gap-1">
                  {sched.tone_of_voice && !['ekspert','przyjazny','formalny','poradnikowy'].includes(sched.tone_of_voice) ? (
                    <button
                      onClick={() => viewTone(sched.id)}
                      className="px-2 py-1 bg-green-100 text-green-700 border border-green-300 rounded text-xs font-medium hover:bg-green-200"
                      title="Tone of Voice wygenerowany — kliknij aby zobaczyć/edytować"
                    >
                      Tone AI
                    </button>
                  ) : (
                    <button
                      onClick={() => generateTone(sched.id)}
                      disabled={toneGenerating[sched.id]}
                      className="px-2 py-1 bg-amber-100 text-amber-700 border border-amber-300 rounded text-xs font-medium hover:bg-amber-200 disabled:opacity-50 flex items-center gap-1"
                      title="Wygeneruj unikalny Tone of Voice (AI) dla tej domeny"
                    >
                      {toneGenerating[sched.id] ? (
                        <><span className="animate-spin inline-block w-3 h-3 border-2 border-amber-600 border-t-transparent rounded-full" />Generuję...</>
                      ) : 'Generuj Tone'}
                    </button>
                  )}
                </div>

                <div className="w-px h-5 bg-gray-200 mx-1" />

                {/* Actions */}
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
                        onClick={() => runAll(sched)}
                        disabled={running[sched.id]}
                        title={`Opublikuj wszystkie pending frazy (${pending_count(sched.id)})`}
                        className="px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 disabled:opacity-50 flex items-center gap-1"
                      >
                        {running[sched.id] ? '...' : `▶▶ Wszystkie (${pending_count(sched.id)})`}
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
                        onClick={() => fetchMetrics(sched.id, true)}
                        title="Przelicz coherence i sprawdź SiteFocus / SiteRadius"
                        className="px-2 py-1.5 text-indigo-600 border border-indigo-200 rounded-lg text-xs hover:bg-indigo-50 disabled:opacity-50"
                      >
                        📊 Metryki
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
                    <span className="text-gray-300 mx-1">|</span>
                    {['pillar', 'supporting'].map(t => {
                      const kws = keywords[sched.id] || []
                      const count = kws.filter(k => k.keyword_type === t).length
                      return (
                        <button
                          key={t}
                          onClick={() => {
                            setKwTypeFilter(f => ({ ...f, [sched.id]: kwTypeFilter[sched.id] === t ? '' : t }))
                          }}
                          className={`text-xs px-2 py-1 rounded ${kwTypeFilter[sched.id] === t
                            ? (t === 'pillar' ? 'bg-blue-600 text-white' : 'bg-gray-700 text-white')
                            : (t === 'pillar' ? 'bg-blue-50 text-blue-600 hover:bg-blue-100' : 'bg-gray-50 text-gray-600 hover:bg-gray-100')
                          }`}
                        >
                          {t === 'pillar' ? 'Pillar' : 'Supporting'} ({count})
                        </button>
                      )
                    })}
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
                          {keywords[sched.id].filter(kw => !kwTypeFilter[sched.id] || kw.keyword_type === kwTypeFilter[sched.id]).map(kw => (
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
                              <td className="py-1.5 flex gap-1">
                                {kw.status === 'pending' && (
                                  <button
                                    onClick={() => previewKeyword(kw.id)}
                                    disabled={previewLoading[kw.id]}
                                    className="text-xs px-2 py-0.5 bg-blue-50 text-blue-600 rounded hover:bg-blue-100 disabled:opacity-50"
                                  >{previewLoading[kw.id] ? '...' : 'Podgląd'}</button>
                                )}
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

      {/* Preview modal */}
      {/* Tone of Voice Modal */}
      {toneModal && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setToneModal(null)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-2xl max-h-[85vh] flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
              <div>
                <h3 className="font-bold text-gray-900">Tone of Voice</h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  {toneModal.isGenerated ? 'Wygenerowany przez AI — możesz edytować' : 'Preset — wygeneruj unikalny tone'}
                </p>
              </div>
              <button onClick={() => setToneModal(null)} className="text-gray-400 hover:text-gray-600 text-xl">&times;</button>
            </div>
            <div className="flex-1 overflow-auto p-6">
              <textarea
                value={toneEditing}
                onChange={e => setToneEditing(e.target.value)}
                rows={16}
                className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none font-mono leading-relaxed"
                placeholder="Tone of Voice instrukcja..."
              />
            </div>
            <div className="flex items-center justify-between px-6 py-4 border-t border-gray-100">
              <button
                onClick={() => { generateTone(toneModal.scheduleId); setToneModal(null) }}
                className="px-4 py-2 bg-amber-100 text-amber-700 border border-amber-300 rounded-lg text-sm font-medium hover:bg-amber-200"
              >
                Wygeneruj ponownie
              </button>
              <div className="flex gap-2">
                <button onClick={() => setToneModal(null)} className="px-4 py-2 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50">Anuluj</button>
                <button onClick={saveToneEdit} className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">Zapisz</button>
              </div>
            </div>
          </div>
        </div>
      )}

      {previewModal && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setPreviewModal(null)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
              <div>
                <h3 className="font-bold text-gray-900">Podgląd artykułu</h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  Fraza: <strong>{previewModal.keyword}</strong> · Tytuł: {previewModal.title}
                </p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => {
                    navigator.clipboard.writeText(previewModal.content)
                  }}
                  className="px-3 py-1.5 text-xs bg-blue-100 text-blue-700 rounded-lg hover:bg-blue-200"
                >Kopiuj HTML</button>
                <button onClick={() => setPreviewModal(null)} className="text-gray-400 hover:text-gray-600 text-xl leading-none">×</button>
              </div>
            </div>
            <div className="overflow-y-auto flex-1 px-6 py-4">
              {previewModal.excerpt && (
                <div className="mb-4 p-3 bg-gray-50 rounded-lg">
                  <span className="text-xs font-medium text-gray-500">Meta description:</span>
                  <p className="text-sm text-gray-700 mt-1">{previewModal.excerpt}</p>
                </div>
              )}
              {previewModal.lsi_tags?.length > 0 && (
                <div className="mb-4 flex gap-1 flex-wrap">
                  {previewModal.lsi_tags.map((t, i) => (
                    <span key={i} className="px-2 py-0.5 bg-gray-100 text-gray-600 rounded-full text-xs">{t}</span>
                  ))}
                </div>
              )}
              <div
                className="prose prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: sanitizeHtml(previewModal.content) }}
              />
            </div>
          </div>
        </div>
      )}

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
