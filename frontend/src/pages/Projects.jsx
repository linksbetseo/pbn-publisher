import { useState, useEffect } from 'react'
import api, { projects as projectsApi, clients as clientsApi } from '../api/client'
import { useToast } from '../components/Toast'
import { sanitizeHtml } from '../sanitize'

function Spinner({ className = 'w-5 h-5' }) {
  return (
    <svg className={`animate-spin ${className}`} fill="none" viewBox="0 0 24 24">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
    </svg>
  )
}

// ── Domain Workspace ────────────────────────────────────────────────────────

function DomainWorkspace({ domainId, domainName, onBack }) {
  const addToast = useToast()
  const [tab, setTab] = useState('map') // map | tone | articles | export
  const [domainInfo, setDomainInfo] = useState(null)
  const [keywords, setKeywords] = useState([])
  const [articles, setArticles] = useState([])
  const [loading, setLoading] = useState(true)
  const [generating, setGenerating] = useState(false)
  const [toneGenerating, setToneGenerating] = useState(false)
  const [articleGenerating, setArticleGenerating] = useState(false)

  // Map generation form
  const [seedKw, setSeedKw] = useState('')
  const [lang, setLang] = useState('pl')
  const [minVol, setMinVol] = useState(10)
  const [siteDesc, setSiteDesc] = useState('')
  const [mapLog, setMapLog] = useState(null) // { seeds: [...], inserted, pillars } | null

  // Article generation
  const [selectedKws, setSelectedKws] = useState(new Set())
  const [customPrompt, setCustomPrompt] = useState('')

  // Tone modal
  const [toneText, setToneText] = useState('')
  const [toneEditing, setToneEditing] = useState(false)

  // Article preview
  const [previewArticle, setPreviewArticle] = useState(null)

  // Site metrics
  const [siteMetrics, setSiteMetrics] = useState(null)

  const loadAll = async () => {
    setLoading(true)
    try {
      const [infoRes, kwRes, artRes] = await Promise.all([
        api.get(`/api/client-content/domains/${domainId}`),
        api.get(`/api/client-content/domains/${domainId}/keywords`),
        api.get(`/api/client-content/domains/${domainId}/articles`),
      ])
      setDomainInfo(infoRes.data)
      setKeywords(kwRes.data)
      setArticles(artRes.data)
      if (infoRes.data.seed_keyword) setSeedKw(infoRes.data.seed_keyword)
      if (infoRes.data.language) setLang(infoRes.data.language)
      if (infoRes.data.tone_of_voice) setToneText(infoRes.data.tone_of_voice)
      if (infoRes.data.site_description) setSiteDesc(infoRes.data.site_description)

      // Load metrics if map exists
      if (infoRes.data.map_generated) {
        api.get(`/api/client-content/domains/${domainId}/site-metrics`).then(r => {
          if (r.data.site_metrics) setSiteMetrics(r.data.site_metrics)
        }).catch(() => {})
      }
    } catch (e) {
      addToast('Blad ladowania danych domeny', 'error')
    }
    setLoading(false)
  }

  useEffect(() => { loadAll() }, [domainId])

  const generateMap = async () => {
    if (!seedKw.trim()) return addToast('Wpisz seed keyword', 'warning')
    setGenerating(true)
    setMapLog(null)
    try {
      // Save site description if provided (used for GPT relevance filtering)
      if (siteDesc.trim()) {
        await api.patch(`/api/client-content/domains/${domainId}`, { site_description: siteDesc.trim() }).catch(() => {})
      }
      const res = await api.post(`/api/client-content/domains/${domainId}/generate-map`, {
        seed_keyword: seedKw.trim(),
        language: lang,
        min_volume: Number(minVol),
      }, { timeout: 300000 })
      const data = res.data
      setMapLog({ seeds: data.seeds || [], inserted: data.inserted, pillars: data.pillars })
      if (data.seeds && data.seeds.length > 1) {
        const breakdown = data.seeds.map(s => `${s.seed}: ${s.keywords} fraz`).join(', ')
        addToast(`Topical Map: ${data.inserted} fraz, ${data.pillars} klastrow (${breakdown})`, 'success')
      } else {
        addToast(`Topical Map: ${data.inserted} fraz, ${data.pillars} klastrow`, 'success')
      }
      await loadAll()
    } catch (e) {
      addToast('Blad generowania mapy: ' + (e.response?.data?.detail || e.message), 'error')
    }
    setGenerating(false)
  }

  const generateTone = async () => {
    setToneGenerating(true)
    try {
      const res = await api.post(`/api/client-content/domains/${domainId}/generate-tone`, {}, { timeout: 300000 })
      setToneText(res.data.tone_of_voice)
      addToast('Tone of Voice wygenerowany!', 'success')
      await loadAll()
    } catch (e) {
      addToast('Blad: ' + (e.response?.data?.detail || e.message), 'error')
    }
    setToneGenerating(false)
  }

  const saveTone = async () => {
    await api.patch(`/api/client-content/domains/${domainId}`, { tone_of_voice: toneText })
    addToast('Tone zapisany', 'success')
    setToneEditing(false)
  }

  const toggleKw = (id) => setSelectedKws(s => {
    const n = new Set(s)
    n.has(id) ? n.delete(id) : n.add(id)
    return n
  })

  const toggleAllPending = () => {
    const pending = keywords.filter(k => k.status === 'pending').map(k => k.id)
    if (pending.every(id => selectedKws.has(id))) {
      setSelectedKws(s => { const n = new Set(s); pending.forEach(id => n.delete(id)); return n })
    } else {
      setSelectedKws(s => { const n = new Set(s); pending.forEach(id => n.add(id)); return n })
    }
  }

  const generateArticles = async () => {
    if (!selectedKws.size) return addToast('Zaznacz frazy', 'warning')
    setArticleGenerating(true)
    try {
      const res = await api.post(`/api/client-content/domains/${domainId}/generate-articles`, {
        keyword_ids: [...selectedKws],
        custom_prompt: customPrompt,
        language: lang,
      }, { timeout: 600000 })
      addToast(`Wygenerowano ${res.data.generated} artykulow, ${res.data.failed} bledow`, res.data.failed ? 'warning' : 'success')
      setSelectedKws(new Set())
      await loadAll()
      setTab('articles')
    } catch (e) {
      addToast('Blad: ' + (e.response?.data?.detail || e.message), 'error')
    }
    setArticleGenerating(false)
  }

  const viewArticle = async (id) => {
    const res = await api.get(`/api/client-content/articles/${id}`)
    setPreviewArticle(res.data)
  }

  const deleteArticle = async (id) => {
    await api.delete(`/api/client-content/articles/${id}`)
    await loadAll()
  }

  const exportCsv = () => {
    const BASE = import.meta.env.VITE_API_URL || ''
    const token = localStorage.getItem('pbn_auth_token')
    fetch(`${BASE}/api/client-content/domains/${domainId}/export-csv`, {
      headers: token ? { Authorization: `Basic ${token}` } : {},
    }).then(r => r.blob()).then(blob => {
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `content_${domainName}.csv`
      a.click()
    })
  }

  const exportHtml = () => {
    const BASE = import.meta.env.VITE_API_URL || ''
    const token = localStorage.getItem('pbn_auth_token')
    fetch(`${BASE}/api/client-content/domains/${domainId}/export-html`, {
      headers: token ? { Authorization: `Basic ${token}` } : {},
    }).then(r => {
      if (!r.ok) throw new Error('Export failed')
      return r.blob()
    }).then(blob => {
      const a = document.createElement('a')
      a.href = URL.createObjectURL(blob)
      a.download = `content_${domainName}.zip`
      a.click()
    }).catch(e => addToast(e.message, 'error'))
  }

  if (loading) return <div className="flex items-center justify-center h-40"><Spinner /></div>

  const pendingKws = keywords.filter(k => k.status === 'pending')
  const generatedKws = keywords.filter(k => k.status === 'generated')

  return (
    <div>
      {/* Header */}
      <div className="flex items-center gap-3 mb-4">
        <button onClick={onBack} className="px-3 py-1.5 border border-gray-300 rounded-lg text-sm text-gray-600 hover:bg-gray-50">
          ← Wróć
        </button>
        <div>
          <h3 className="text-lg font-bold text-gray-900">{domainName}</h3>
          <div className="flex gap-3 text-xs text-gray-500">
            <span>Frazy: {keywords.length}</span>
            <span>Artykuly: {articles.length}</span>
            {domainInfo?.seed_keyword && <span>Seed: {domainInfo.seed_keyword}</span>}
          </div>
        </div>
        {siteMetrics && (
          <div className="flex gap-2 ml-auto text-xs">
            <span className={`px-2 py-0.5 rounded-full font-medium ${siteMetrics.focus_rating === 'excellent' ? 'bg-green-100 text-green-700' : siteMetrics.focus_rating === 'good' ? 'bg-blue-100 text-blue-700' : 'bg-yellow-100 text-yellow-700'}`}>
              Focus: {(siteMetrics.site_focus * 100).toFixed(0)}%
            </span>
            <span className={`px-2 py-0.5 rounded-full font-medium ${siteMetrics.radius_rating === 'tight' ? 'bg-green-100 text-green-700' : siteMetrics.radius_rating === 'moderate' ? 'bg-blue-100 text-blue-700' : 'bg-yellow-100 text-yellow-700'}`}>
              Radius: {(siteMetrics.site_radius * 100).toFixed(0)}%
            </span>
          </div>
        )}
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-1 mb-5 w-fit">
        {[['map', 'Topical Map'], ['tone', 'Tone of Voice'], ['articles', `Artykuly (${articles.length})`], ['export', 'Eksport']].map(([k, label]) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-4 py-2 rounded-md text-sm font-medium transition-colors ${tab === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'}`}>
            {label}
          </button>
        ))}
      </div>

      {/* ── TAB: Topical Map ── */}
      {tab === 'map' && (
        <div className="space-y-4">
          {/* Generate form */}
          <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
            <h4 className="font-semibold text-gray-800 mb-3">Generuj Topical Map</h4>
            <div className="grid grid-cols-4 gap-3">
              <div className="col-span-2">
                <label className="block text-xs font-medium text-gray-500 mb-1">Seed keyword *</label>
                <input value={seedKw} onChange={e => setSeedKw(e.target.value)}
                  placeholder="np. pokemony, pokemon karty, pokemon go (po przecinku = więcej fraz)"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">Min. volume</label>
                <input type="number" min={0} value={minVol} onChange={e => setMinVol(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
              </div>
              <div>
                <label className="block text-xs font-medium text-gray-500 mb-1">Język</label>
                <select value={lang} onChange={e => setLang(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500">
                  <option value="pl">Polski</option>
                  <option value="en">English</option>
                </select>
              </div>
            </div>
            <div className="mt-3">
              <label className="block text-xs font-medium text-gray-500 mb-1">
                Opis biznesu <span className="text-gray-400">(opcjonalnie — auto-generowany z homepage jeśli puste)</span>
              </label>
              <textarea value={siteDesc} onChange={e => setSiteDesc(e.target.value)}
                placeholder="np. Sklep z kamieniami naturalnymi — granit, marmur, kwarc. Sprzedaż hurtowa i detaliczna materiałów budowlanych i dekoracyjnych."
                rows={2}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none" />
              {siteDesc && (
                <p className="text-xs text-green-600 mt-1">Opis biznesu pomoże odfiltrować nietrafne frazy z topical map</p>
              )}
            </div>
            <button onClick={generateMap} disabled={generating}
              className="mt-3 px-5 py-2 bg-purple-600 text-white rounded-lg text-sm font-medium hover:bg-purple-700 disabled:opacity-50 flex items-center gap-2">
              {generating ? <><Spinner className="w-4 h-4" /> Generuję mapę...</> : 'Generuj Topical Map'}
            </button>
            {seedKw.includes(',') && !generating && (
              <p className="mt-2 text-xs text-purple-600">
                {seedKw.split(',').filter(s => s.trim()).length} seedów — wyniki zostaną połączone i zdeduplikowane
              </p>
            )}
          </div>

          {/* Loader overlay during generation */}
          {generating && (
            <div className="bg-purple-50 border border-purple-200 rounded-xl p-6 text-center">
              <Spinner className="w-8 h-8 text-purple-600 mx-auto mb-3" />
              <p className="text-sm font-medium text-purple-800">Generuję Topical Map...</p>
              <p className="text-xs text-purple-500 mt-1">
                {seedKw.includes(',')
                  ? `Pobieram dane z DataForSEO dla ${seedKw.split(',').filter(s => s.trim()).length} seedów i łączę wyniki`
                  : 'Pobieram frazy z DataForSEO i tworzę klastry tematyczne'}
              </p>
              <p className="text-xs text-purple-400 mt-2">To może potrwać 30-120 sekund...</p>
            </div>
          )}

          {/* Seed log after generation */}
          {mapLog && mapLog.seeds && mapLog.seeds.length > 1 && !generating && (
            <div className="bg-green-50 border border-green-200 rounded-xl p-4">
              <div className="flex items-center justify-between mb-2">
                <h5 className="text-sm font-semibold text-green-800">Wyniki generowania (multi-seed)</h5>
                <button onClick={() => setMapLog(null)} className="text-green-400 hover:text-green-600 text-xs">zamknij</button>
              </div>
              <div className="grid grid-cols-2 gap-2 text-xs">
                {mapLog.seeds.map((s, i) => (
                  <div key={i} className="flex items-center gap-2 bg-white rounded-lg px-3 py-2">
                    <span className="font-medium text-green-700">{s.seed}</span>
                    <span className="text-gray-400">→</span>
                    <span className="text-gray-600">{s.pillars} klastrow, {s.keywords} fraz</span>
                  </div>
                ))}
              </div>
              <p className="text-xs text-green-600 mt-2 font-medium">
                Razem po deduplikacji: {mapLog.inserted} fraz, {mapLog.pillars} klastrow
              </p>
            </div>
          )}

          {/* Keywords list */}
          {keywords.length > 0 && (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
              <div className="flex items-center justify-between px-4 py-3 border-b border-gray-100">
                <div className="flex items-center gap-3">
                  <h4 className="font-semibold text-gray-800">Frazy ({keywords.length})</h4>
                  <span className="text-xs text-gray-400">
                    {pendingKws.length} oczekujących, {generatedKws.length} z artykułem
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <button onClick={toggleAllPending}
                    className="px-3 py-1.5 border border-gray-300 rounded-lg text-xs text-gray-600 hover:bg-gray-50">
                    {pendingKws.every(k => selectedKws.has(k.id)) ? 'Odznacz' : 'Zaznacz'} pending ({pendingKws.length})
                  </button>
                  {selectedKws.size > 0 && (
                    <button onClick={generateArticles} disabled={articleGenerating}
                      className="px-4 py-1.5 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 disabled:opacity-50 flex items-center gap-1">
                      {articleGenerating ? <><Spinner className="w-3 h-3" /> Generuję...</> : `Generuj artykuly (${selectedKws.size})`}
                    </button>
                  )}
                </div>
              </div>
              {selectedKws.size > 0 && (
                <div className="px-4 py-2 bg-blue-50 border-b border-blue-100">
                  <label className="block text-xs font-medium text-blue-700 mb-1">Custom prompt (opcjonalnie)</label>
                  <input value={customPrompt} onChange={e => setCustomPrompt(e.target.value)}
                    placeholder="Dodatkowe wskazówki dla AI..."
                    className="w-full border border-blue-200 rounded px-3 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
              )}
              <div className="max-h-[450px] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-gray-50 border-b border-gray-100">
                    <tr>
                      <th className="px-3 py-2 w-8"></th>
                      <th className="px-3 py-2 text-left font-semibold text-gray-500">Keyword</th>
                      <th className="px-3 py-2 text-left font-semibold text-gray-500">Typ</th>
                      <th className="px-3 py-2 text-left font-semibold text-gray-500">Klaster</th>
                      <th className="px-3 py-2 text-right font-semibold text-gray-500">Vol.</th>
                      <th className="px-3 py-2 text-right font-semibold text-gray-500">KD</th>
                      <th className="px-3 py-2 text-right font-semibold text-gray-500">Coh.</th>
                      <th className="px-3 py-2 text-center font-semibold text-gray-500">Status</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50">
                    {keywords.map(kw => (
                      <tr key={kw.id} className={`hover:bg-gray-50 ${selectedKws.has(kw.id) ? 'bg-blue-50' : ''}`}>
                        <td className="px-3 py-2">
                          <input type="checkbox" checked={selectedKws.has(kw.id)} onChange={() => toggleKw(kw.id)}
                            disabled={kw.status === 'generated'}
                            className="rounded border-gray-300" />
                        </td>
                        <td className="px-3 py-2 font-medium text-gray-800">{kw.keyword}</td>
                        <td className="px-3 py-2">
                          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${kw.keyword_type === 'pillar' ? 'bg-blue-100 text-blue-700' : 'bg-gray-100 text-gray-600'}`}>
                            {kw.keyword_type}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-gray-500 max-w-[150px] truncate" title={kw.pillar_label}>{kw.pillar_label}</td>
                        <td className="px-3 py-2 text-right text-gray-600">{kw.search_volume}</td>
                        <td className="px-3 py-2 text-right text-gray-600">{kw.keyword_difficulty?.toFixed(0)}</td>
                        <td className="px-3 py-2 text-right text-gray-600">{(kw.coherence * 100).toFixed(0)}%</td>
                        <td className="px-3 py-2 text-center">
                          <span className={`px-2 py-0.5 rounded-full text-xs font-medium ${kw.status === 'generated' ? 'bg-green-100 text-green-700' : 'bg-yellow-100 text-yellow-700'}`}>
                            {kw.status}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── TAB: Tone of Voice ── */}
      {tab === 'tone' && (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5 space-y-4">
          <div className="flex items-center justify-between">
            <h4 className="font-semibold text-gray-800">Tone of Voice</h4>
            <div className="flex gap-2">
              <button onClick={generateTone} disabled={toneGenerating}
                className="px-4 py-2 bg-amber-600 text-white rounded-lg text-sm font-medium hover:bg-amber-700 disabled:opacity-50 flex items-center gap-2">
                {toneGenerating ? <><Spinner className="w-4 h-4" /> Generuję...</> : toneText ? 'Wygeneruj ponownie' : 'Generuj Tone of Voice'}
              </button>
              {toneEditing && (
                <button onClick={saveTone}
                  className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700">
                  Zapisz
                </button>
              )}
            </div>
          </div>
          {toneText ? (
            <div>
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs text-green-600 font-medium">Tone wygenerowany</span>
                <button onClick={() => setToneEditing(!toneEditing)}
                  className="text-xs text-blue-600 hover:underline">
                  {toneEditing ? 'Anuluj edycje' : 'Edytuj'}
                </button>
              </div>
              {toneEditing ? (
                <textarea value={toneText} onChange={e => setToneText(e.target.value)}
                  rows={16}
                  className="w-full border border-gray-300 rounded-lg px-4 py-3 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none font-mono leading-relaxed" />
              ) : (
                <div className="bg-gray-50 rounded-lg p-4 text-sm text-gray-700 whitespace-pre-wrap leading-relaxed max-h-[500px] overflow-y-auto">
                  {toneText}
                </div>
              )}
            </div>
          ) : (
            <div className="text-center py-12 text-gray-400">
              <p className="text-sm">Brak Tone of Voice — kliknij "Generuj" aby AI utworzył unikalny ton dla tej domeny.</p>
              <p className="text-xs mt-1">Pipeline: analiza treści → profil klienta → SERP → konkurencja → generacja tonu</p>
            </div>
          )}
        </div>
      )}

      {/* ── TAB: Articles ── */}
      {tab === 'articles' && (
        <div className="space-y-4">
          {articles.length === 0 ? (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-8 text-center text-gray-400">
              <p className="text-sm">Brak artykułów — przejdz do Topical Map, zaznacz frazy i wygeneruj.</p>
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-hidden">
              <div className="px-4 py-3 border-b border-gray-100 flex items-center justify-between">
                <h4 className="font-semibold text-gray-800">Artykuły ({articles.length})</h4>
              </div>
              <div className="max-h-[500px] overflow-y-auto">
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-gray-50 border-b border-gray-100">
                    <tr>
                      <th className="px-3 py-2 text-left font-semibold text-gray-500">Keyword</th>
                      <th className="px-3 py-2 text-left font-semibold text-gray-500">Tytuł</th>
                      <th className="px-3 py-2 text-center font-semibold text-gray-500">Status</th>
                      <th className="px-3 py-2 text-left font-semibold text-gray-500">Data</th>
                      <th className="px-3 py-2 w-24"></th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50">
                    {articles.map(a => (
                      <tr key={a.id} className="hover:bg-gray-50">
                        <td className="px-3 py-2 text-gray-600">{a.keyword}</td>
                        <td className="px-3 py-2 font-medium text-gray-800 max-w-[300px] truncate" title={a.title}>{a.title}</td>
                        <td className="px-3 py-2 text-center">
                          <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-blue-100 text-blue-700">
                            {a.status}
                          </span>
                        </td>
                        <td className="px-3 py-2 text-gray-500 whitespace-nowrap">
                          {a.created_at ? new Date(a.created_at).toLocaleString('pl-PL', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }) : '—'}
                        </td>
                        <td className="px-3 py-2">
                          <div className="flex gap-1">
                            <button onClick={() => viewArticle(a.id)}
                              className="px-2 py-1 text-blue-600 border border-blue-200 rounded text-xs hover:bg-blue-50">
                              Podglad
                            </button>
                            <button onClick={() => deleteArticle(a.id)}
                              className="px-2 py-1 text-red-500 border border-red-200 rounded text-xs hover:bg-red-50">
                              Usun
                            </button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}

      {/* ── TAB: Export ── */}
      {tab === 'export' && (
        <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5 space-y-4">
          <h4 className="font-semibold text-gray-800">Eksport artykułów</h4>
          <p className="text-sm text-gray-500">
            Eksportuj wygenerowane artykuły do przeglądu przez klienta. {articles.length} artykułów gotowych.
          </p>
          <div className="flex gap-3">
            <button onClick={exportCsv} disabled={!articles.length}
              className="px-5 py-2.5 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50">
              Eksport CSV
            </button>
            <button onClick={exportHtml} disabled={!articles.length}
              className="px-5 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
              Eksport HTML (ZIP)
            </button>
          </div>
          {!articles.length && (
            <p className="text-xs text-gray-400">Wygeneruj artykuły w zakładce Topical Map aby móc je eksportować.</p>
          )}
        </div>
      )}

      {/* Article preview modal */}
      {previewArticle && (
        <div className="fixed inset-0 bg-black/40 z-50 flex items-center justify-center p-4" onClick={() => setPreviewArticle(null)}>
          <div className="bg-white rounded-2xl shadow-2xl w-full max-w-4xl max-h-[90vh] flex flex-col" onClick={e => e.stopPropagation()}>
            <div className="flex items-center justify-between px-6 py-4 border-b border-gray-100">
              <div>
                <h3 className="font-bold text-gray-900">Podgląd artykuły</h3>
                <p className="text-xs text-gray-500 mt-0.5">
                  Fraza: <strong>{previewArticle.keyword}</strong>
                </p>
              </div>
              <button onClick={() => setPreviewArticle(null)} className="text-gray-400 hover:text-gray-600 text-xl">&times;</button>
            </div>
            <div className="flex-1 overflow-auto p-6">
              <h1 className="text-2xl font-bold text-gray-900 mb-4">{previewArticle.title}</h1>
              <div
                className="prose prose-sm max-w-none"
                dangerouslySetInnerHTML={{ __html: sanitizeHtml(previewArticle.content) }}
              />
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Main Projects Component ─────────────────────────────────────────────────

export default function Projects() {
  const addToast = useToast()
  const [projectList, setProjectList] = useState([])
  const [clientList, setClientList] = useState([])
  const [selectedProject, setSelectedProject] = useState(null)
  const [selectedClient, setSelectedClient] = useState(null)
  const [newProject, setNewProject] = useState('')
  const [newClient, setNewClient] = useState('')
  const [newDomain, setNewDomain] = useState('')
  const [loading, setLoading] = useState(false)

  // Domain workspace
  const [activeDomain, setActiveDomain] = useState(null) // { id, domain }

  const loadProjects = async () => {
    const res = await projectsApi.list()
    setProjectList(res.data)
  }

  const loadClients = async (projectId) => {
    const res = await clientsApi.list(projectId)
    setClientList(res.data)
    setSelectedClient(null)
  }

  useEffect(() => { loadProjects() }, [])

  const handleSelectProject = (p) => {
    setSelectedProject(p)
    loadClients(p.id)
    setActiveDomain(null)
  }

  const handleAddProject = async () => {
    if (!newProject.trim()) return
    setLoading(true)
    await projectsApi.create(newProject.trim())
    setNewProject('')
    await loadProjects()
    setLoading(false)
  }

  const handleDeleteProject = async (id) => {
    if (!confirm('Usunąć projekt wraz z klientami?')) return
    await projectsApi.delete(id)
    if (selectedProject?.id === id) {
      setSelectedProject(null)
      setClientList([])
    }
    await loadProjects()
  }

  const handleAddClient = async () => {
    if (!newClient.trim() || !selectedProject) return
    setLoading(true)
    await clientsApi.create(selectedProject.id, newClient.trim())
    setNewClient('')
    await loadClients(selectedProject.id)
    setLoading(false)
  }

  const handleDeleteClient = async (id) => {
    if (!confirm('Usunąć klienta?')) return
    await clientsApi.delete(id)
    if (selectedClient?.id === id) setSelectedClient(null)
    await loadClients(selectedProject.id)
  }

  const handleAddDomain = async () => {
    if (!newDomain.trim() || !selectedClient) return
    setLoading(true)
    await clientsApi.addDomain(selectedClient.id, newDomain.trim())
    setNewDomain('')
    await loadClients(selectedProject.id)
    setLoading(false)
  }

  const handleDeleteDomain = async (clientId, domainId) => {
    await clientsApi.deleteDomain(clientId, domainId)
    await loadClients(selectedProject.id)
  }

  const currentClient = clientList.find(c => c.id === selectedClient?.id)

  // If domain workspace is active, show it fullscreen
  if (activeDomain) {
    return (
      <div className="p-8 max-w-7xl">
        <DomainWorkspace
          domainId={activeDomain.id}
          domainName={activeDomain.domain}
          onBack={() => setActiveDomain(null)}
        />
      </div>
    )
  }

  return (
    <div className="p-8">
      <h2 className="text-2xl font-bold text-gray-900 mb-6">Projekty</h2>

      <div className="grid grid-cols-3 gap-6 h-[calc(100vh-160px)]">
        {/* Projects panel */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="p-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-700 mb-3">Projekty</h3>
            <div className="flex gap-2">
              <input
                type="text"
                placeholder="Nazwa projektu..."
                value={newProject}
                onChange={(e) => setNewProject(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAddProject()}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                onClick={handleAddProject}
                disabled={loading}
                className="px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
              >+</button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {projectList.length === 0 && (
              <p className="text-center text-gray-400 text-sm mt-8">Brak projektów</p>
            )}
            {projectList.map((p) => (
              <div
                key={p.id}
                onClick={() => handleSelectProject(p)}
                className={`flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer mb-1 ${
                  selectedProject?.id === p.id
                    ? 'bg-blue-50 border border-blue-200'
                    : 'hover:bg-gray-50'
                }`}
              >
                <div>
                  <p className="text-sm font-medium text-gray-800">{p.name}</p>
                  <p className="text-xs text-gray-400">{p.client_count} klientów</p>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteProject(p.id) }}
                  className="text-red-400 hover:text-red-600 p-1"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Clients panel */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="p-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-700 mb-3">
              Klienci {selectedProject ? `— ${selectedProject.name}` : ''}
            </h3>
            {selectedProject && (
              <div className="flex gap-2">
                <input
                  type="text"
                  placeholder="Nazwa klienta..."
                  value={newClient}
                  onChange={(e) => setNewClient(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddClient()}
                  className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  onClick={handleAddClient}
                  disabled={loading}
                  className="px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
                >+</button>
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {!selectedProject && (
              <p className="text-center text-gray-400 text-sm mt-8">Wybierz projekt</p>
            )}
            {selectedProject && clientList.length === 0 && (
              <p className="text-center text-gray-400 text-sm mt-8">Brak klientów</p>
            )}
            {clientList.map((c) => (
              <div
                key={c.id}
                onClick={() => setSelectedClient(c)}
                className={`flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer mb-1 ${
                  selectedClient?.id === c.id
                    ? 'bg-blue-50 border border-blue-200'
                    : 'hover:bg-gray-50'
                }`}
              >
                <div>
                  <p className="text-sm font-medium text-gray-800">{c.name}</p>
                  <p className="text-xs text-gray-400">{c.domains?.length || 0} domen</p>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteClient(c.id) }}
                  className="text-red-400 hover:text-red-600 p-1"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Domains panel */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="p-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-700 mb-3">
              Domeny klienta {currentClient ? `— ${currentClient.name}` : ''}
            </h3>
            {currentClient && (
              <div className="flex gap-2">
                <input
                  type="text"
                  placeholder="np. example.com"
                  value={newDomain}
                  onChange={(e) => setNewDomain(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddDomain()}
                  className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  onClick={handleAddDomain}
                  disabled={loading}
                  className="px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
                >+</button>
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {!currentClient && (
              <p className="text-center text-gray-400 text-sm mt-8">Wybierz klienta</p>
            )}
            {currentClient && (currentClient.domains || []).length === 0 && (
              <p className="text-center text-gray-400 text-sm mt-8">Brak domen</p>
            )}
            {(currentClient?.domains || []).map((d) => (
              <div key={d.id} className="flex items-center justify-between px-3 py-2.5 rounded-lg hover:bg-gray-50 mb-1">
                <button
                  onClick={() => setActiveDomain(d)}
                  className="text-sm text-blue-600 font-medium hover:underline text-left"
                >
                  {d.domain}
                </button>
                <div className="flex gap-1 items-center">
                  <button
                    onClick={() => setActiveDomain(d)}
                    className="px-2 py-1 text-xs text-blue-600 border border-blue-200 rounded hover:bg-blue-50"
                  >
                    Content
                  </button>
                  <button
                    onClick={() => handleDeleteDomain(currentClient.id, d.id)}
                    className="text-red-400 hover:text-red-600 p-1"
                  >
                    <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                    </svg>
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
