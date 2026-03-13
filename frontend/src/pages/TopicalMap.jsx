import { useState, useEffect, useRef, useCallback } from 'react'
import api from '../api/client'

const INTENT_COLORS = {
  informational: 'bg-blue-100 text-blue-700',
  commercial: 'bg-orange-100 text-orange-700',
  transactional: 'bg-green-100 text-green-700',
  navigational: 'bg-gray-100 text-gray-600',
}

const TREND_ICONS = { rising: '↑', declining: '↓', new: '★', stable: '' }
const TREND_COLORS = { rising: 'text-green-600', declining: 'text-red-500', new: 'text-purple-600', stable: '' }

const RATING_PL = {
  excellent: 'Doskonały', good: 'Dobry', fair: 'Średni', weak: 'Słaby',
  tight: 'Ścisły', moderate: 'Umiarkowany', wide: 'Szeroki', drifting: 'Dryfujący',
  comprehensive: 'Pełne', developing: 'Rozwijające', sparse: 'Rzadkie',
}

// FIX #22: Force-directed graph visualization (pure Canvas, no extra deps)
function ForceGraph({ nodes: rawNodes, links: rawLinks }) {
  const canvasRef = useRef(null)
  const stateRef = useRef({ nodes: [], links: [], dragging: null, offset: { x: 0, y: 0 } })

  useEffect(() => {
    if (!rawNodes?.length || !canvasRef.current) return
    const canvas = canvasRef.current
    const ctx = canvas.getContext('2d')
    const W = canvas.width = canvas.parentElement.offsetWidth
    const H = canvas.height = 420
    const dpr = window.devicePixelRatio || 1
    canvas.width = W * dpr
    canvas.height = H * dpr
    canvas.style.width = W + 'px'
    canvas.style.height = H + 'px'
    ctx.scale(dpr, dpr)

    // Init positions
    const idMap = {}
    const nodes = rawNodes.map((n, i) => {
      const angle = (i / rawNodes.length) * Math.PI * 2
      const r = n.type === 'seed' ? 0 : n.type === 'pillar' ? 120 : 200 + Math.random() * 60
      const obj = { ...n, x: W / 2 + Math.cos(angle) * r, y: H / 2 + Math.sin(angle) * r, vx: 0, vy: 0 }
      idMap[n.id] = obj
      return obj
    })
    const links = rawLinks.filter(l => idMap[l.source] && idMap[l.target]).map(l => ({
      ...l, source: idMap[l.source], target: idMap[l.target]
    }))
    stateRef.current = { nodes, links, dragging: null, offset: { x: 0, y: 0 } }

    let animFrame
    const simulate = () => {
      // Repulsion
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          let dx = nodes[j].x - nodes[i].x
          let dy = nodes[j].y - nodes[i].y
          let dist = Math.sqrt(dx * dx + dy * dy) || 1
          let force = 800 / (dist * dist)
          let fx = (dx / dist) * force
          let fy = (dy / dist) * force
          nodes[i].vx -= fx; nodes[i].vy -= fy
          nodes[j].vx += fx; nodes[j].vy += fy
        }
      }
      // Attraction along links
      for (const l of links) {
        let dx = l.target.x - l.source.x
        let dy = l.target.y - l.source.y
        let dist = Math.sqrt(dx * dx + dy * dy) || 1
        let force = (dist - 80) * 0.005 * (l.strength || 1)
        let fx = (dx / dist) * force
        let fy = (dy / dist) * force
        l.source.vx += fx; l.source.vy += fy
        l.target.vx -= fx; l.target.vy -= fy
      }
      // Center gravity
      for (const n of nodes) {
        n.vx += (W / 2 - n.x) * 0.001
        n.vy += (H / 2 - n.y) * 0.001
        n.vx *= 0.9; n.vy *= 0.9
        if (stateRef.current.dragging !== n) {
          n.x += n.vx; n.y += n.vy
        }
        n.x = Math.max(20, Math.min(W - 20, n.x))
        n.y = Math.max(20, Math.min(H - 20, n.y))
      }
      // Draw
      ctx.clearRect(0, 0, W, H)
      // Links
      for (const l of links) {
        ctx.beginPath()
        ctx.moveTo(l.source.x, l.source.y)
        ctx.lineTo(l.target.x, l.target.y)
        ctx.strokeStyle = l.type === 'cross_pillar' ? 'rgba(99,102,241,0.3)' : 'rgba(156,163,175,0.25)'
        ctx.lineWidth = l.type === 'cross_pillar' ? 1.5 : 0.8
        ctx.stroke()
      }
      // Nodes
      for (const n of nodes) {
        const r = n.type === 'seed' ? 12 : n.type === 'pillar' ? 8 : 4
        ctx.beginPath()
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2)
        ctx.fillStyle = n.color || '#4285f4'
        ctx.fill()
        if (n.type !== 'supporting') {
          ctx.font = n.type === 'seed' ? 'bold 11px sans-serif' : '10px sans-serif'
          ctx.fillStyle = '#374151'
          ctx.textAlign = 'center'
          const label = n.label.length > 22 ? n.label.slice(0, 20) + '...' : n.label
          ctx.fillText(label, n.x, n.y + r + 12)
        }
      }
      animFrame = requestAnimationFrame(simulate)
    }
    simulate()

    // Drag support
    const getNode = (ex, ey) => {
      const rect = canvas.getBoundingClientRect()
      const mx = ex - rect.left, my = ey - rect.top
      for (const n of nodes) {
        const r = n.type === 'seed' ? 14 : n.type === 'pillar' ? 10 : 6
        if (Math.hypot(n.x - mx, n.y - my) < r) return n
      }
      return null
    }
    const onDown = (e) => {
      const n = getNode(e.clientX, e.clientY)
      if (n) { stateRef.current.dragging = n; canvas.style.cursor = 'grabbing' }
    }
    const onMove = (e) => {
      const d = stateRef.current.dragging
      if (d) {
        const rect = canvas.getBoundingClientRect()
        d.x = e.clientX - rect.left; d.y = e.clientY - rect.top
        d.vx = 0; d.vy = 0
      }
    }
    const onUp = () => { stateRef.current.dragging = null; canvas.style.cursor = 'default' }
    canvas.addEventListener('mousedown', onDown)
    canvas.addEventListener('mousemove', onMove)
    canvas.addEventListener('mouseup', onUp)
    canvas.addEventListener('mouseleave', onUp)

    return () => {
      cancelAnimationFrame(animFrame)
      canvas.removeEventListener('mousedown', onDown)
      canvas.removeEventListener('mousemove', onMove)
      canvas.removeEventListener('mouseup', onUp)
      canvas.removeEventListener('mouseleave', onUp)
    }
  }, [rawNodes, rawLinks])

  return (
    <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4 mb-6">
      <h3 className="text-sm font-semibold text-gray-700 mb-3">Mapa tematyczna (graf)</h3>
      <div className="text-xs text-gray-400 mb-2">Przeciągnij węzły aby eksplorować strukturę klastrów</div>
      <canvas ref={canvasRef} className="w-full rounded-lg bg-gray-50 border border-gray-100" style={{ height: 420 }} />
      <div className="flex gap-4 mt-2 text-xs text-gray-500">
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-full bg-[#1a2332] inline-block" /> Seed</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-full bg-[#1a73e8] inline-block" /> Pillar</span>
        <span className="flex items-center gap-1"><span className="w-3 h-3 rounded-full bg-[#4285f4] inline-block" /> Supporting</span>
      </div>
    </div>
  )
}

export default function TopicalMap() {
  const [seed, setSeed] = useState('')
  const [minVolume, setMinVolume] = useState(50)
  const [maxClusters, setMaxClusters] = useState(8)
  const [language, setLanguage] = useState('pl')
  const [minCoherence, setMinCoherence] = useState(0)
  const [forceRefresh, setForceRefresh] = useState(false)
  const [competitorDomain, setCompetitorDomain] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  // FIX #24: Set instead of single value — allows multiple expanded
  const [expandedPillars, setExpandedPillars] = useState(new Set())
  const [showGraph, setShowGraph] = useState(false)
  const [domains, setDomains] = useState([])
  const [importDomain, setImportDomain] = useState('')
  const [importing, setImporting] = useState(false)
  const [importMsg, setImportMsg] = useState('')

  useEffect(() => {
    api.get('/api/domains').then(r => setDomains(r.data || [])).catch(() => {})
  }, [])

  const togglePillar = (i) => {
    setExpandedPillars(prev => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i); else next.add(i)
      return next
    })
  }

  const generate = async () => {
    if (!seed.trim()) return
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const resp = await api.post('/api/topical-map', {
        seed: seed.trim(),
        location_code: language === 'pl' ? 2616 : language === 'de' ? 2276 : 2840,
        language_code: language,
        min_volume: minVolume,
        max_clusters: maxClusters,
        force_refresh: forceRefresh,
        min_coherence: minCoherence,
        competitor_domain: competitorDomain.trim(),
      })
      setResult(resp.data)
      setForceRefresh(false)
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Błąd generowania mapy')
    } finally {
      setLoading(false)
    }
  }

  const copyKeywords = (keywords) => {
    const text = keywords.map(k => k.keyword || k).join('\n')
    navigator.clipboard.writeText(text)
  }

  // FIX #23: CSV export includes coherence, intent, CPC, trend for all rows
  const exportCsv = () => {
    if (!result?.pillars) return
    const rows = [['Pillar', 'Keyword', 'Type', 'Volume', 'KD', 'Intent', 'CPC', 'Coherence', 'Trend', 'Ranking', 'Position']]
    result.pillars.forEach(p => {
      rows.push([p.label, p.pillar_keyword, 'pillar', p.pillar_volume || 0, Math.round(p.pillar_difficulty || 0), p.pillar_intent || '', p.pillar_cpc || '', p.pillar_coherence || '', p.pillar_trend || '', p.pillar_already_ranking ? 'yes' : '', p.pillar_current_position || ''])
      p.supporting_keywords?.forEach(sk => {
        rows.push([p.label, sk.keyword, 'supporting', sk.search_volume || 0, Math.round(sk.keyword_difficulty || 0), sk.intent || '', sk.cpc || '', sk.coherence || '', sk.trend || '', sk.already_ranking ? 'yes' : '', sk.current_position || ''])
      })
    })
    const csv = rows.map(r => r.map(c => `"${String(c).replace(/"/g, '""')}"`).join(',')).join('\n')
    const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `topical-map_${result.seed}_${new Date().toISOString().slice(0,10)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const exportJson = () => {
    if (!result) return
    const blob = new Blob([JSON.stringify(result, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `topical-map_${result.seed}_${new Date().toISOString().slice(0,10)}.json`
    a.click()
    URL.revokeObjectURL(url)
  }

  const sendToAutopilot = async () => {
    if (!importDomain || !result?.pillars) return
    setImporting(true)
    setImportMsg('')
    try {
      const resp = await api.post('/api/autopilot/import-map', {
        my_domain_id: Number(importDomain),
        seed_keyword: result.seed || seed,
        pillars: result.pillars,
        language,
      })
      setImportMsg(`Zaimportowano ${resp.data.inserted} fraz (schedule #${resp.data.schedule_id})`)
    } catch (e) {
      setImportMsg('Błąd: ' + (e.response?.data?.detail || e.message))
    } finally {
      setImporting(false)
    }
  }

  const totalKeywords = result?.pillars?.reduce((s, p) => s + p.supporting_keywords.length + 1, 0) || 0
  const sm = result?.site_metrics

  return (
    <div className="p-8 max-w-7xl">
      <h2 className="text-2xl font-bold text-gray-900 mb-1">Topical Map</h2>
      <p className="text-gray-500 mb-6 text-sm">
        Buduje strukturę Pillar Pages + Supporting Pages na podstawie analizy DataForSEO
      </p>

      {/* Config */}
      <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 mb-6">
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex-1 min-w-[260px]">
            <label className="block text-sm font-medium text-gray-700 mb-1">Fraza główna (seed)</label>
            <input
              value={seed}
              onChange={e => setSeed(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && generate()}
              placeholder="np. kancelaria prawna, ubezpieczenia"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Język</label>
            <select
              value={language}
              onChange={e => setLanguage(e.target.value)}
              className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="pl">Polski</option>
              <option value="en">English</option>
              <option value="de">Deutsch</option>
            </select>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Min. wolumen</label>
            <input
              type="number"
              value={minVolume}
              onChange={e => setMinVolume(Number(e.target.value))}
              className="w-24 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Klastrów</label>
            <input
              type="number"
              value={maxClusters}
              min={2}
              max={15}
              onChange={e => setMaxClusters(Number(e.target.value))}
              className="w-20 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          {/* FIX #26: min_coherence slider */}
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Filtr spójności <span className="text-xs text-gray-400">{minCoherence > 0 ? minCoherence.toFixed(1) : 'wył.'}</span>
            </label>
            <input
              type="range"
              min={0}
              max={0.5}
              step={0.05}
              value={minCoherence}
              onChange={e => setMinCoherence(Number(e.target.value))}
              className="w-24"
            />
          </div>
          {/* FIX #17: competitor domain for cannibalization check */}
          <div className="min-w-[180px]">
            <label className="block text-sm font-medium text-gray-700 mb-1">Domena konkur.</label>
            <input
              value={competitorDomain}
              onChange={e => setCompetitorDomain(e.target.value)}
              placeholder="np. example.com"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
            />
          </div>
          <div className="flex items-center gap-2">
            {/* FIX #25: force refresh checkbox */}
            <label className="flex items-center gap-1.5 cursor-pointer text-xs text-gray-500">
              <input type="checkbox" checked={forceRefresh} onChange={e => setForceRefresh(e.target.checked)} className="rounded" />
              Odśwież
            </label>
          </div>
          <button
            onClick={generate}
            disabled={loading || !seed.trim()}
            className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 flex items-center gap-2"
          >
            {loading ? (
              <><span className="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full" />Generuję...</>
            ) : 'Generuj mapę'}
          </button>
        </div>
      </div>

      {loading && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-8 mb-6 flex flex-col items-center justify-center">
          <div className="animate-spin rounded-full h-12 w-12 border-4 border-blue-200 border-t-blue-600 mb-4" />
          <p className="text-lg font-semibold text-gray-800 mb-1">Generuję mapę tematyczną...</p>
          <p className="text-sm text-gray-500 text-center max-w-md">
            Pobieram słowa kluczowe z DataForSEO, analizuję wolumeny i klastry tematyczne. To może potrwać 15-60 sekund.
          </p>
        </div>
      )}

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-red-700 text-sm mb-6">{error}</div>
      )}

      {!loading && result && (
        <>
          {/* Summary + Export */}
          <div className="flex gap-4 mb-6 flex-wrap items-center">
            <div className="px-4 py-2 bg-blue-50 rounded-lg text-sm font-medium text-blue-700">
              Seed: <strong>{result.seed}</strong>
            </div>
            <div className="px-4 py-2 bg-green-50 rounded-lg text-sm font-medium text-green-700">
              Pillar pages: <strong>{result.pillars?.length || 0}</strong>
            </div>
            <div className="px-4 py-2 bg-purple-50 rounded-lg text-sm font-medium text-purple-700">
              Supporting pages: <strong>{totalKeywords}</strong>
            </div>
            <div className="px-4 py-2 bg-gray-100 rounded-lg text-sm font-medium text-gray-600">
              Wszystkich fraz: <strong>{result.total_keywords || 0}</strong>
            </div>
            <div className="ml-auto flex gap-2">
              <button onClick={exportCsv} className="px-3 py-2 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700">
                CSV
              </button>
              <button onClick={exportJson} className="px-3 py-2 bg-gray-600 text-white rounded-lg text-xs font-medium hover:bg-gray-700">
                JSON
              </button>
              <button onClick={() => copyKeywords(result.pillars?.flatMap(p => [{ keyword: p.pillar_keyword }, ...p.supporting_keywords]) || [])} className="px-3 py-2 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700">
                Kopiuj wszystkie
              </button>
            </div>
          </div>

          {/* Send to Autopilot */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4 mb-6">
            <h3 className="text-sm font-semibold text-gray-700 mb-3">Wyślij do Autopilota</h3>
            <div className="flex gap-3 items-end flex-wrap">
              <div className="flex-1 min-w-[200px]">
                <label className="block text-xs text-gray-500 mb-1">Domena PBN</label>
                <select
                  value={importDomain}
                  onChange={e => setImportDomain(e.target.value)}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-orange-500"
                >
                  <option value="">-- wybierz domenę --</option>
                  {domains.filter(d => d.active).map(d => (
                    <option key={d.id} value={d.id}>{d.domain}</option>
                  ))}
                </select>
              </div>
              <button
                onClick={sendToAutopilot}
                disabled={!importDomain || importing}
                className="px-4 py-2 bg-orange-600 text-white rounded-lg text-sm font-medium hover:bg-orange-700 disabled:opacity-50 flex items-center gap-2"
              >
                {importing ? 'Importuję...' : 'Wyślij do Autopilota'}
              </button>
              {importMsg && (
                <span className={`text-sm ${importMsg.startsWith('Błąd') ? 'text-red-600' : 'text-green-600'}`}>{importMsg}</span>
              )}
            </div>
          </div>

          {/* FIX #21: Site Metrics — show ALL returned metrics */}
          {sm && (
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4 mb-6">
              <h3 className="text-sm font-semibold text-gray-700 mb-3">Metryki spójności tematycznej</h3>
              <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
                <div>
                  <span className="text-xs text-gray-500">SiteFocus</span>
                  <div className="text-lg font-bold text-blue-700">{(sm.site_focus * 100).toFixed(0)}%</div>
                  <p className="text-xs text-gray-400">{RATING_PL[sm.focus_rating] || sm.focus_rating}</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">SiteRadius</span>
                  <div className="text-lg font-bold text-purple-700">{(sm.site_radius * 100).toFixed(0)}%</div>
                  <p className="text-xs text-gray-400">{RATING_PL[sm.radius_rating] || sm.radius_rating}</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Pokrycie</span>
                  <div className="text-lg font-bold text-green-700">{sm.coverage}</div>
                  <p className="text-xs text-gray-400">klastrów</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Artykuły</span>
                  <div className="text-lg font-bold text-gray-700">{sm.total_articles}</div>
                  <p className="text-xs text-gray-400">łącznie do napisania</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Kompletność</span>
                  <div className="text-lg font-bold text-amber-600">{(sm.avg_cluster_completeness * 100).toFixed(0)}%</div>
                  <p className="text-xs text-gray-400">{RATING_PL[sm.completeness_rating] || sm.completeness_rating}</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Tempo</span>
                  <div className="text-lg font-bold text-indigo-600">{sm.recommended_weekly_velocity}/tydz.</div>
                  <p className="text-xs text-gray-400">rekomendowane</p>
                </div>
              </div>
            </div>
          )}

          {/* FIX #17: Competitor / Cannibalization Analysis */}
          {result.competitor_analysis && (
            <div className="bg-white rounded-xl border border-orange-200 shadow-sm p-4 mb-6">
              <h3 className="text-sm font-semibold text-orange-700 mb-3">Analiza kanibalizacji — {result.competitor_analysis.domain}</h3>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
                <div>
                  <span className="text-xs text-gray-500">Już rankuje</span>
                  <div className="text-lg font-bold text-orange-700">{result.competitor_analysis.total_already_ranking}</div>
                  <p className="text-xs text-gray-400">z {result.competitor_analysis.total_checked} fraz</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">W TOP 10</span>
                  <div className="text-lg font-bold text-red-600">{result.competitor_analysis.in_top10}</div>
                  <p className="text-xs text-gray-400">silna pozycja</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Pokrycie</span>
                  <div className="text-lg font-bold text-amber-600">{result.competitor_analysis.coverage_pct}%</div>
                  <p className="text-xs text-gray-400">fraz pokrytych</p>
                </div>
                <div>
                  <span className="text-xs text-gray-500">Nowe okazje</span>
                  <div className="text-lg font-bold text-green-700">{result.competitor_analysis.total_checked - result.competitor_analysis.total_already_ranking}</div>
                  <p className="text-xs text-gray-400">fraz niepokrytych</p>
                </div>
              </div>
            </div>
          )}

          {/* FIX #22: Force Graph Toggle + Visualization */}
          {result.nodes?.length > 0 && (
            <div className="mb-4">
              <button
                onClick={() => setShowGraph(prev => !prev)}
                className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm font-medium hover:bg-indigo-700"
              >
                {showGraph ? 'Ukryj graf' : 'Pokaż graf tematyczny'}
              </button>
            </div>
          )}
          {showGraph && result.nodes?.length > 0 && (
            <ForceGraph nodes={result.nodes} links={result.links} />
          )}

          {/* Pillars */}
          <div className="space-y-4">
            {result.pillars?.map((pillar, i) => (
              <div key={i} className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                {/* Pillar header */}
                <div
                  className="flex items-center justify-between p-4 cursor-pointer hover:bg-gray-50"
                  onClick={() => togglePillar(i)}
                >
                  <div className="flex items-center gap-3">
                    <span className="w-8 h-8 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center text-sm font-bold">{i + 1}</span>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-gray-900">{pillar.label}</span>
                        <span className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded-full font-medium">PILLAR</span>
                        {/* FIX #19: show intent badge */}
                        {pillar.pillar_intent && (
                          <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${INTENT_COLORS[pillar.pillar_intent] || 'bg-gray-100 text-gray-600'}`}>
                            {pillar.pillar_intent}
                          </span>
                        )}
                        {/* FIX #14: trend indicator */}
                        {pillar.pillar_trend && pillar.pillar_trend !== 'stable' && (
                          <span className={`text-xs font-bold ${TREND_COLORS[pillar.pillar_trend] || ''}`}>
                            {TREND_ICONS[pillar.pillar_trend]}
                          </span>
                        )}
                      </div>
                      <div className="text-sm text-gray-500 mt-0.5">
                        <span className="font-medium text-gray-700">{pillar.pillar_keyword}</span>
                        {' · '}vol. <span className="font-medium">{(pillar.pillar_volume || 0).toLocaleString()}</span>
                        {' · '}KD: <span className={`font-medium ${pillar.pillar_difficulty > 60 ? 'text-red-600' : pillar.pillar_difficulty > 30 ? 'text-yellow-600' : 'text-green-600'}`}>{Math.round(pillar.pillar_difficulty || 0)}</span>
                        {/* FIX #20: show CPC */}
                        {pillar.pillar_cpc > 0 && (
                          <>{' · '}<span className="text-emerald-600 font-medium">${pillar.pillar_cpc.toFixed(2)}</span></>
                        )}
                        {' · '}avg KD: <span className="font-medium text-gray-600">{Math.round(pillar.avg_difficulty || 0)}</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-4 text-sm text-gray-500">
                    <span className="hidden sm:block">{pillar.supporting_keywords?.length || 0} supporting</span>
                    <span className="hidden sm:block">vol. {(pillar.total_volume || 0).toLocaleString()}</span>
                    {pillar.content_gap?.quick_wins > 0 && (
                      <span className="hidden sm:block text-xs px-2 py-0.5 bg-emerald-100 text-emerald-700 rounded-full">
                        {pillar.content_gap.quick_wins} quick wins
                      </span>
                    )}
                    {pillar.cannibalization_risk > 0 && (
                      <span className="hidden sm:block text-xs px-2 py-0.5 bg-orange-100 text-orange-700 rounded-full">
                        {pillar.cannibalization_risk} ranking
                      </span>
                    )}
                    <svg className={`w-5 h-5 transition-transform ${expandedPillars.has(i) ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </div>

                {/* Supporting pages + Related pillars — FIX #24: uses Set */}
                {expandedPillars.has(i) && (
                  <div className="border-t border-gray-100 p-4">
                    {/* FIX #18: Related pillars — use pillar_index, add label */}
                    {pillar.related_pillars && pillar.related_pillars.length > 0 && (
                      <div className="mb-4 p-3 bg-indigo-50 rounded-lg">
                        <h5 className="text-xs font-semibold text-indigo-700 mb-2">Powiązane klastry (interlinking)</h5>
                        <div className="flex flex-wrap gap-2">
                          {pillar.related_pillars.map((rp, ri) => (
                            <button
                              key={ri}
                              onClick={(e) => { e.stopPropagation(); togglePillar(rp.pillar_index) }}
                              className="px-3 py-1 bg-indigo-100 text-indigo-700 rounded-full text-xs font-medium hover:bg-indigo-200 transition-colors"
                            >
                              {rp.label || rp.pillar_keyword} <span className="text-indigo-400">({(rp.similarity * 100).toFixed(0)}%)</span>
                            </button>
                          ))}
                        </div>
                      </div>
                    )}

                    {/* Content gap summary */}
                    {pillar.content_gap && (
                      <div className="flex gap-3 mb-3 text-xs">
                        <span className="px-2 py-1 bg-gray-100 rounded">Subtopics: {pillar.content_gap.total_subtopics}</span>
                        <span className="px-2 py-1 bg-amber-50 text-amber-700 rounded">High vol gaps: {pillar.content_gap.high_volume_gaps}</span>
                        <span className="px-2 py-1 bg-green-50 text-green-700 rounded">Low KD: {pillar.content_gap.low_kd_opportunities}</span>
                        <span className="px-2 py-1 bg-emerald-50 text-emerald-700 rounded">Quick wins: {pillar.content_gap.quick_wins}</span>
                      </div>
                    )}

                    <div className="flex items-center justify-between mb-3">
                      <h4 className="text-sm font-semibold text-gray-700">Supporting Pages ({pillar.supporting_keywords?.length || 0})</h4>
                      <button
                        onClick={() => copyKeywords(pillar.supporting_keywords)}
                        className="text-xs text-blue-600 hover:underline"
                      >
                        Kopiuj frazy
                      </button>
                    </div>
                    {/* FIX #19/#20: show intent badge and CPC for supporting */}
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                      {pillar.supporting_keywords?.map((sk, j) => (
                        <div key={j} className="flex items-center justify-between px-3 py-2 bg-gray-50 rounded-lg text-sm">
                          <div className="flex items-center gap-1.5 truncate mr-2">
                            {sk.trend && sk.trend !== 'stable' && (
                              <span className={`text-xs font-bold ${TREND_COLORS[sk.trend] || ''}`}>{TREND_ICONS[sk.trend]}</span>
                            )}
                            <span className="text-gray-700 truncate">{sk.keyword}</span>
                          </div>
                          <div className="flex items-center gap-1.5 shrink-0 text-xs text-gray-500">
                            <span>{(sk.search_volume || 0).toLocaleString()}</span>
                            <span className={`px-1.5 py-0.5 rounded ${sk.keyword_difficulty > 60 ? 'bg-red-100 text-red-600' : sk.keyword_difficulty > 30 ? 'bg-yellow-100 text-yellow-600' : 'bg-green-100 text-green-600'}`}>
                              {Math.round(sk.keyword_difficulty || 0)}
                            </span>
                            {sk.intent && (
                              <span className={`px-1 py-0.5 rounded text-[10px] ${INTENT_COLORS[sk.intent] || 'bg-gray-100 text-gray-500'}`}>
                                {sk.intent?.charAt(0).toUpperCase()}
                              </span>
                            )}
                            {sk.cpc > 0 && (
                              <span className="text-emerald-600">${sk.cpc.toFixed(2)}</span>
                            )}
                            {sk.already_ranking && (
                              <span className={`px-1 py-0.5 rounded text-[10px] font-medium ${sk.current_position <= 10 ? 'bg-red-100 text-red-600' : sk.current_position <= 30 ? 'bg-orange-100 text-orange-600' : 'bg-yellow-100 text-yellow-600'}`}>
                                #{sk.current_position}
                              </span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
