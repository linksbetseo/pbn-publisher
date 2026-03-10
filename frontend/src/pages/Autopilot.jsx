import { useState, useEffect, useRef } from 'react'
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
    setDomains(domRes.data.filter(d => d.active && d.wp_ok))
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

  return (
    <div className="p-8 max-w-7xl">
      <div className="flex items-center justify-between mb-2">
        <div>
          <h2 className="text-2xl font-bold text-gray-900">Autopilot PBN</h2>
          <p className="text-gray-500 text-sm mt-0.5">Automatyczne uzupełnianie treści na domenach PBN na podstawie Topical Map</p>
        </div>
        <button
          onClick={() => setShowAdd(!showAdd)}
          className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
        >
          + Dodaj domenę
        </button>
      </div>

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
                          ? `✓ ${entry.keyword} → ${entry.url}`
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
    </div>
  )
}
