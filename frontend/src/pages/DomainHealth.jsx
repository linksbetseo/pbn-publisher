import { useState, useEffect, useCallback, useRef } from 'react'
import api from '../api/client'
import { useToast } from '../components/Toast'

const PAGE_SIZE = 50

function ScoreBadge({ score }) {
  const map = {
    good:     { label: 'Dobry',    cls: 'bg-green-100 text-green-700' },
    medium:   { label: 'Średni',   cls: 'bg-yellow-100 text-yellow-700' },
    weak:     { label: 'Słaby',    cls: 'bg-red-100 text-red-700' },
    critical: { label: 'Krytyczny',cls: 'bg-red-200 text-red-800 font-bold' },
  }
  const { label, cls } = map[score] || { label: score || '—', cls: 'bg-gray-100 text-gray-600' }
  return <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${cls}`}>{label}</span>
}

function ExpiryBadge({ days, date }) {
  if (days === null || days === undefined)
    return <span className="text-gray-400 text-xs">—</span>
  let cls = 'bg-green-100 text-green-700'
  if (days < 0)  cls = 'bg-red-200 text-red-800'
  else if (days < 14) cls = 'bg-red-100 text-red-700'
  else if (days < 60) cls = 'bg-yellow-100 text-yellow-700'
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${cls}`} title={date || ''}>
      {days < 0 ? 'WYGASŁA' : `${days}d`}
    </span>
  )
}

function WpBadge({ ok }) {
  if (ok === null || ok === undefined)
    return <span className="text-gray-300 text-xs">—</span>
  return ok
    ? <span className="inline-flex items-center gap-1 text-green-600 text-xs font-medium"><span className="w-2 h-2 rounded-full bg-green-500 inline-block" />OK</span>
    : <span className="inline-flex items-center gap-1 text-red-500 text-xs font-medium"><span className="w-2 h-2 rounded-full bg-red-400 inline-block" />Błąd</span>
}

function StatCard({ label, value, sub, color, onClick, active }) {
  return (
    <div
      onClick={onClick}
      className={`bg-white rounded-xl border shadow-sm p-4 ${onClick ? 'cursor-pointer hover:border-blue-300' : ''} ${active ? 'border-blue-400 ring-1 ring-blue-300' : 'border-gray-100'}`}
    >
      <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">{label}</p>
      <p className={`text-2xl font-bold mt-1 ${color}`}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-1">{sub}</p>}
    </div>
  )
}

// ── Progress bar ──────────────────────────────────────────────────────────────

function SnapshotProgress({ progress, onDone }) {
  if (!progress || !progress.running) return null
  const pct = progress.pct || 0
  return (
    <div className="mb-4 px-4 py-3 bg-blue-50 border border-blue-200 rounded-xl">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs font-semibold text-blue-700">
          Snapshot w toku — {progress.done}/{progress.total} domen ({pct}%)
        </span>
        <span className="text-xs text-blue-500 animate-pulse">● działa</span>
      </div>
      <div className="w-full bg-blue-100 rounded-full h-2">
        <div
          className="bg-blue-500 h-2 rounded-full transition-all duration-500"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

// ── Historia zakładka ─────────────────────────────────────────────────────────

function SnapshotsTab() {
  const [snapshots, setSnapshots] = useState([])
  const [loading, setLoading] = useState(false)
  const [snapping, setSnapping] = useState(false)
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('traffic')
  const [sortDir, setSortDir] = useState('desc')

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const SortTh = ({ col, label }) => {
    const active = sortKey === col
    const arrow = active ? (sortDir === 'asc' ? ' ↑' : ' ↓') : ' ⇅'
    return (
      <th
        onClick={() => toggleSort(col)}
        className={`px-4 py-2 text-left text-xs font-semibold cursor-pointer select-none ${active ? 'text-blue-600' : 'text-gray-500 hover:text-gray-700'}`}
      >
        {label}<span className="opacity-60">{arrow}</span>
      </th>
    )
  }

  const load = async () => {
    setLoading(true)
    try {
      const res = await api.get('/api/health/snapshots', { params: { limit: 500 } })
      setSnapshots(res.data)
    } catch {
      // silent — UI shows empty state
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const triggerSnapshot = async () => {
    setSnapping(true)
    try {
      await api.post('/api/health/snapshot')
      setTimeout(load, 5000)
    } catch {
      // silent
    } finally {
      setSnapping(false)
    }
  }

  const filtered = snapshots.filter(s =>
    !search || s.domain.toLowerCase().includes(search.toLowerCase())
  )

  const byDate = filtered.reduce((acc, s) => {
    const date = s.snapped_at?.slice(0, 10) || 'unknown'
    if (!acc[date]) acc[date] = []
    acc[date].push(s)
    return acc
  }, {})

  const sortRows = (rows) => [...rows].sort((a, b) => {
    let av, bv
    if (sortKey === 'traffic') { av = a.traffic || 0; bv = b.traffic || 0 }
    else if (sortKey === 'keywords') { av = a.keywords || 0; bv = b.keywords || 0 }
    else if (sortKey === 'expiry') { av = a.days_to_expiry ?? 9999; bv = b.days_to_expiry ?? 9999 }
    else if (sortKey === 'health') { av = a.health_score || 0; bv = b.health_score || 0 }
    else if (sortKey === 'dr') { av = a.dr || 0; bv = b.dr || 0 }
    else { av = a.domain; bv = b.domain }
    if (typeof av === 'string') return sortDir === 'asc' ? av.localeCompare(bv) : bv.localeCompare(av)
    return sortDir === 'asc' ? av - bv : bv - av
  })

  return (
    <div>
      <div className="flex items-center justify-between mb-4">
        <div className="flex gap-3 items-center">
          <input
            type="text"
            placeholder="Szukaj domeny..."
            value={search}
            onChange={e => setSearch(e.target.value)}
            className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          <span className="text-sm text-gray-400">{snapshots.length} rekordów</span>
        </div>
        <button
          onClick={triggerSnapshot}
          disabled={snapping}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
        >
          {snapping ? (
            <><span className="animate-spin">⟳</span> Snapshot w toku...</>
          ) : (
            <><span>📸</span> Zrób snapshot teraz</>
          )}
        </button>
      </div>

      {loading ? (
        <div className="flex items-center justify-center h-40 text-gray-400">
          <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600 mr-2" />
          Ładowanie historii...
        </div>
      ) : Object.keys(byDate).length === 0 ? (
        <div className="text-center py-16 text-gray-400">
          <p className="text-4xl mb-3">📊</p>
          <p className="font-medium">Brak snapshotów</p>
          <p className="text-sm mt-1">Cron uruchamia się co poniedziałek o 03:00 UTC,<br/>lub kliknij "Zrób snapshot teraz"</p>
        </div>
      ) : (
        Object.entries(byDate)
          .sort(([a], [b]) => b.localeCompare(a))
          .map(([date, rows]) => (
            <div key={date} className="mb-6">
              <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wide mb-2 px-1">
                {date} — {rows.length} domen
              </h4>
              <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-x-auto">
                <table className="w-full text-sm">
                  <thead className="border-b border-gray-100 bg-gray-50">
                    <tr>
                      <SortTh col="domain" label="Domena" />
                      <SortTh col="traffic" label="Ruch" />
                      <SortTh col="keywords" label="Słowa kl." />
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">WP</th>
                      <SortTh col="expiry" label="Wygasa" />
                      <SortTh col="health" label="Zdrowie" />
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50">
                    {sortRows(rows).map(s => (
                      <tr key={s.id} className="hover:bg-gray-50">
                        <td className="px-4 py-2 text-blue-600 font-medium text-xs">{s.domain.replace(/^https?:\/\//, '')}</td>
                        <td className="px-4 py-2 text-gray-700">{s.traffic?.toLocaleString() || 0}</td>
                        <td className="px-4 py-2 text-gray-700">{s.keywords?.toLocaleString() || 0}</td>
                        <td className="px-4 py-2"><WpBadge ok={s.wp_ok} /></td>
                        <td className="px-4 py-2"><ExpiryBadge days={s.days_to_expiry} date={s.expiry_date} /></td>
                        <td className="px-4 py-2"><ScoreBadge score={s.health_score} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ))
      )}
    </div>
  )
}

// ── HTTP Auth Modal ───────────────────────────────────────────────────────────

function HttpAuthModal({ domain, onClose }) {
  const addToast = useToast()
  const [user, setUser] = useState('')
  const [pass, setPass] = useState('')
  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)

  const save = async () => {
    setSaving(true)
    try {
      await api.patch(`/api/domains/${domain.id}`, { http_user: user, http_pass: pass })
      setSaved(true)
      setTimeout(onClose, 800)
    } catch (e) {
      addToast('Błąd zapisu: ' + (e.response?.data?.detail || e.message), 'error')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl p-6 w-96" onClick={e => e.stopPropagation()}>
        <h3 className="text-base font-semibold text-gray-900 mb-1">Basic Auth (htpasswd)</h3>
        <p className="text-xs text-gray-500 mb-4">{domain.domain}</p>
        <div className="space-y-3">
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Login HTTP</label>
            <input value={user} onChange={e => setUser(e.target.value)}
              placeholder="użytkownik"
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 mb-1">Hasło HTTP</label>
            <input type="password" value={pass} onChange={e => setPass(e.target.value)}
              placeholder="hasło"
              className="w-full border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
          </div>
          <p className="text-xs text-gray-400">Zostaw puste jeśli domena nie ma Basic Auth.</p>
        </div>
        <div className="flex justify-end gap-2 mt-5">
          <button onClick={onClose} className="px-4 py-2 text-sm text-gray-600 hover:text-gray-900">Anuluj</button>
          <button onClick={save} disabled={saving}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50">
            {saved ? '✓ Zapisano' : saving ? 'Zapisuję...' : 'Zapisz'}
          </button>
        </div>
      </div>
    </div>
  )
}

// ── Live zakładka ─────────────────────────────────────────────────────────────

function LiveTab() {
  const [domains, setDomains] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [liveChecking, setLiveChecking] = useState({})
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('traffic')
  const [sortDir, setSortDir] = useState('desc')
  const [filter, setFilter] = useState('all')
  const [authModal, setAuthModal] = useState(null)
  const [wpTesting, setWpTesting] = useState({})
  const [wpTestResult, setWpTestResult] = useState(null)
  const [snapping, setSnapping] = useState(false)
  const [snapMsg, setSnapMsg] = useState('')
  const [progress, setProgress] = useState(null)
  const pollRef = useRef(null)

  const liveCheck = useCallback(async (domainId) => {
    setLiveChecking(prev => ({ ...prev, [domainId]: true }))
    try {
      const res = await api.get(`/api/health/${domainId}`)
      setDomains(prev => prev.map(d => d.id === domainId ? { ...res.data, from_snapshot: false } : d))
    } catch {
      // silent
    } finally {
      setLiveChecking(prev => ({ ...prev, [domainId]: false }))
    }
  }, [])

  const load = useCallback(async (off = 0, append = false) => {
    setLoading(true)
    try {
      const res = await api.get('/api/health', { params: { limit: PAGE_SIZE, offset: off } })
      setTotal(res.data.total)
      setDomains(prev => append ? [...prev, ...res.data.domains] : res.data.domains)
      setOffset(off + PAGE_SIZE)
      if (res.data.snapshot_progress) {
        setProgress(res.data.snapshot_progress)
      }
    } catch {
      // silent
    } finally {
      setLoading(false)
    }
  }, [])

  // Poll snapshot progress
  const startPolling = useCallback(() => {
    if (pollRef.current) return
    pollRef.current = setInterval(async () => {
      try {
        const res = await api.get('/api/health/snapshot-progress')
        setProgress(res.data)
        if (!res.data.running) {
          // Snapshot finished — reload data
          clearInterval(pollRef.current)
          pollRef.current = null
          setSnapping(false)
          setSnapMsg('Snapshot zakończony! Dane zaktualizowane.')
          await load(0)
          setTimeout(() => setSnapMsg(''), 4000)
        }
      } catch (e) {
        clearInterval(pollRef.current)
        pollRef.current = null
      }
    }, 4000)
  }, [load])

  useEffect(() => {
    load(0)
    // Check if snapshot already running
    api.get('/api/health/snapshot-progress').then(res => {
      if (res.data.running) {
        setProgress(res.data)
        setSnapping(true)
        startPolling()
      }
    }).catch(() => {})
    return () => {
      if (pollRef.current) clearInterval(pollRef.current)
    }
  }, [load, startPolling])

  const triggerSnapshot = async () => {
    if (snapping) return
    setSnapping(true)
    setSnapMsg('Snapshot uruchomiony — zbieranie danych...')
    try {
      const res = await api.post('/api/health/snapshot')
      if (res.data.already_running) {
        setSnapMsg('Snapshot już działa w tle...')
      }
      startPolling()
    } catch (e) {
      setSnapMsg('Błąd: ' + (e.response?.data?.detail || e.message))
      setSnapping(false)
    }
  }

  const triggerQuickSnapshot = async () => {
    if (snapping) return
    setSnapping(true)
    setSnapMsg('Szybki snapshot — DataForSEO + WP (~2-3 min)...')
    try {
      const res = await api.post('/api/health/snapshot-quick')
      if (res.data.already_running) {
        setSnapMsg('Snapshot już działa w tle...')
      }
      startPolling()
    } catch (e) {
      setSnapMsg('Błąd: ' + (e.response?.data?.detail || e.message))
      setSnapping(false)
    }
  }

  const handleSort = (key) => {
    if (sortKey === key) setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    else { setSortKey(key); setSortDir('desc') }
  }

  const filtered = domains
    .filter(d => {
      if (search && !d.domain.toLowerCase().includes(search.toLowerCase())) return false
      if (filter === 'good') return d.health_score === 'good'
      if (filter === 'medium') return d.health_score === 'medium'
      if (filter === 'weak') return d.health_score === 'weak'
      if (filter === 'critical') return d.health_score === 'critical'
      if (filter === 'expiring') return d.days_to_expiry !== null && d.days_to_expiry < 60
      if (filter === 'wp_err') return !d.wp_ok
      if (filter === 'no_snap') return !d.from_snapshot
      return true
    })
    .sort((a, b) => {
      const av = a[sortKey] ?? -1
      const bv = b[sortKey] ?? -1
      return sortDir === 'asc' ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1)
    })

  const good = domains.filter(d => d.health_score === 'good').length
  const medium = domains.filter(d => d.health_score === 'medium').length
  const weak = domains.filter(d => d.health_score === 'weak').length
  const critical = domains.filter(d => d.health_score === 'critical').length
  const expiring = domains.filter(d => d.days_to_expiry !== null && d.days_to_expiry < 60).length
  const wpErrors = domains.filter(d => !d.wp_ok).length
  const noSnap = domains.filter(d => !d.from_snapshot).length
  const avgTraffic = domains.length
    ? Math.round(domains.reduce((s, d) => s + (d.traffic || 0), 0) / domains.length)
    : 0
  const lastSnap = domains.find(d => d.snapped_at)?.snapped_at

  const SortIcon = ({ k }) => sortKey !== k
    ? <span className="text-gray-300 ml-1">↕</span>
    : <span className="ml-1">{sortDir === 'asc' ? '↑' : '↓'}</span>

  const thCls = "px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide cursor-pointer hover:text-gray-800 select-none"

  return (
    <div>
      {authModal && <HttpAuthModal domain={authModal} onClose={() => setAuthModal(null)} />}
      {wpTestResult && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={() => setWpTestResult(null)}>
          <div className="bg-white rounded-xl shadow-xl p-6 w-96" onClick={e => e.stopPropagation()}>
            <h3 className="text-base font-semibold text-gray-900 mb-1">Test WP Connection</h3>
            <p className="text-xs text-gray-500 mb-4">{wpTestResult.domain}</p>
            <div className={`p-4 rounded-lg text-sm ${wpTestResult.ok ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'}`}>
              <p className="font-semibold mb-1">{wpTestResult.ok ? 'Połączenie OK' : 'Błąd połączenia'}</p>
              <p>{wpTestResult.message}</p>
              {wpTestResult.status_code > 0 && <p className="text-xs mt-1 opacity-70">HTTP {wpTestResult.status_code}</p>}
            </div>
            <button onClick={() => setWpTestResult(null)} className="mt-4 w-full py-2 bg-gray-100 text-gray-700 rounded-lg text-sm font-medium hover:bg-gray-200">Zamknij</button>
          </div>
        </div>
      )}

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-8 gap-2 mb-5">
        <StatCard label="Wszystkie" value={total} color="text-gray-800" />
        <StatCard label="Dobre" value={good} color="text-green-600" sub="ruch≥500 lub kw≥200"
          onClick={() => setFilter(f => f === 'good' ? 'all' : 'good')} active={filter === 'good'} />
        <StatCard label="Średnie" value={medium} color="text-yellow-600"
          onClick={() => setFilter(f => f === 'medium' ? 'all' : 'medium')} active={filter === 'medium'} />
        <StatCard label="Słabe" value={weak} color="text-red-600"
          onClick={() => setFilter(f => f === 'weak' ? 'all' : 'weak')} active={filter === 'weak'} />
        <StatCard label="Krytyczne" value={critical} color="text-red-800" sub="wygasa <7d"
          onClick={() => setFilter(f => f === 'critical' ? 'all' : 'critical')} active={filter === 'critical'} />
        <StatCard label="Wygasające" value={expiring} color="text-orange-600" sub="<60 dni"
          onClick={() => setFilter(f => f === 'expiring' ? 'all' : 'expiring')} active={filter === 'expiring'} />
        <StatCard label="WP błąd" value={wpErrors} color="text-red-500"
          onClick={() => setFilter(f => f === 'wp_err' ? 'all' : 'wp_err')} active={filter === 'wp_err'} />
        <StatCard label="Bez snapsh." value={noSnap} color="text-gray-400"
          onClick={() => setFilter(f => f === 'no_snap' ? 'all' : 'no_snap')} active={filter === 'no_snap'} />
      </div>

      {/* Progress bar */}
      <SnapshotProgress progress={progress} />

      {/* Toolbar */}
      <div className="flex items-center justify-between mb-3">
        <div className="text-xs text-gray-400">
          Śr. ruch: {avgTraffic.toLocaleString()} / mies. · {domains.length} z {total} załadowanych
          {lastSnap && (
            <span className="ml-3 text-gray-300">
              · Snapshot: {lastSnap.slice(0, 16).replace('T', ' ')} UTC
            </span>
          )}
          {!lastSnap && domains.length > 0 && (
            <span className="ml-3 text-orange-400 font-medium">
              · Brak snapshotu — kliknij Odśwież
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => {
              const token = localStorage.getItem('pbn_token') || btoa(`${localStorage.getItem('pbn_user')||''}:${localStorage.getItem('pbn_pass')||''}`)
              fetch('/api/health/export-csv', { headers: { Authorization: `Basic ${token}` } })
                .then(r => r.blob())
                .then(blob => {
                  const a = document.createElement('a')
                  a.href = URL.createObjectURL(blob)
                  a.download = `domain_health_${new Date().toISOString().slice(0,10)}.csv`
                  a.click()
                })
            }}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-100 text-gray-700 rounded-lg text-xs font-medium hover:bg-gray-200 transition-colors"
            title="Eksportuj do CSV"
          >
            ↓ CSV
          </button>
          <button
            onClick={triggerQuickSnapshot}
            disabled={snapping}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 disabled:opacity-60 transition-colors"
            title="Szybki snapshot: DataForSEO + WP (bez WHOIS, ~2-3 min)"
          >
            {snapping ? <><span className="animate-spin inline-block">⟳</span> W toku...</> : '⚡ Szybki snapshot'}
          </button>
          <button
            onClick={triggerSnapshot}
            disabled={snapping}
            className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 disabled:opacity-60 transition-colors"
            title="Pełny snapshot: WHOIS + DataForSEO + WP + DR (~10-15 min)"
          >
            {snapping
              ? <><span className="animate-spin inline-block">⟳</span> W toku...</>
              : '📸 Pełny snapshot'}
          </button>
        </div>
      </div>

      {snapMsg && (
        <div className="mb-3 px-4 py-2 bg-blue-50 border border-blue-200 rounded-lg text-xs text-blue-700">
          {snapMsg}
        </div>
      )}

      {/* Search + filter */}
      <div className="flex flex-wrap gap-3 mb-4">
        <input
          type="text"
          placeholder="Szukaj domeny..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <div className="flex flex-wrap gap-1 bg-gray-100 rounded-lg p-1">
          {[
            { k: 'all', label: 'Wszystkie' },
            { k: 'good', label: 'Dobre' },
            { k: 'medium', label: 'Średnie' },
            { k: 'weak', label: 'Słabe' },
            { k: 'expiring', label: 'Wygasające' },
            { k: 'wp_err', label: 'WP błąd' },
            { k: 'no_snap', label: 'Bez danych' },
          ].map(({ k, label }) => (
            <button key={k} onClick={() => setFilter(k)}
              className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                filter === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'
              }`}>{label}</button>
          ))}
        </div>
      </div>

      {/* Table */}
      <div className="bg-white rounded-xl border border-gray-100 shadow-sm overflow-x-auto">
        {loading && domains.length === 0 ? (
          <div className="flex items-center justify-center h-48 text-gray-400">
            <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600 mr-3" />
            Pobieranie danych...
          </div>
        ) : (
          <table className="w-full text-sm">
            <thead className="border-b border-gray-100">
              <tr>
                <th className={thCls} onClick={() => handleSort('domain')}>Domena <SortIcon k="domain" /></th>
                <th className={thCls} onClick={() => handleSort('server')}>Serwer <SortIcon k="server" /></th>
                <th className={thCls} onClick={() => handleSort('traffic')}>Ruch org. <SortIcon k="traffic" /></th>
                <th className={thCls} onClick={() => handleSort('keywords')}>Słowa kl. <SortIcon k="keywords" /></th>
                <th className={thCls} onClick={() => handleSort('days_to_expiry')}>Wygasa <SortIcon k="days_to_expiry" /></th>
                <th className={thCls}>WP</th>
                <th className={thCls} onClick={() => handleSort('health_score')}>Zdrowie <SortIcon k="health_score" /></th>
                <th className="px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide">Auth</th>
                <th className="px-4 py-3"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {filtered.map(d => (
                <tr key={d.id} className={`hover:bg-gray-50 transition-colors ${d.health_score === 'critical' ? 'bg-red-50' : ''}`}>
                  <td className="px-4 py-3">
                    <a href={`https://${d.domain.replace(/^https?:\/\//, '')}`}
                      target="_blank" rel="noopener noreferrer"
                      className="text-blue-600 hover:underline font-medium text-xs">
                      {d.domain.replace(/^https?:\/\//, '')}
                    </a>
                    {!d.from_snapshot && (
                      <span className="ml-2 text-gray-300 text-xs" title="Brak snapshotu">●</span>
                    )}
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{d.server || '—'}</td>
                  <td className="px-4 py-3 font-medium">
                    {d.traffic > 0
                      ? <span className={d.traffic >= 500 ? 'text-green-600' : d.traffic >= 50 ? 'text-yellow-600' : 'text-gray-500'}>
                          {d.traffic.toLocaleString()}
                        </span>
                      : <span className="text-gray-300">0</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {d.keywords > 0 ? d.keywords.toLocaleString() : <span className="text-gray-300">0</span>}
                  </td>
                  <td className="px-4 py-3"><ExpiryBadge days={d.days_to_expiry} date={d.expiry_date} /></td>
                  <td className="px-4 py-3"><WpBadge ok={d.wp_ok} /></td>
                  <td className="px-4 py-3"><ScoreBadge score={d.health_score} /></td>
                  <td className="px-4 py-3 flex items-center gap-1">
                    <button onClick={() => setAuthModal(d)}
                      title="Ustaw Basic Auth (htpasswd)"
                      className="text-gray-400 hover:text-blue-600 text-sm px-2 py-0.5 rounded hover:bg-blue-50 transition-colors">
                      🔑
                    </button>
                    <button
                      onClick={async () => {
                        setWpTesting(prev => ({ ...prev, [d.id]: true }))
                        try {
                          const res = await api.post(`/api/health/${d.id}/test-wp`)
                          setWpTestResult(res.data)
                        } catch (e) {
                          setWpTestResult({ ok: false, domain: d.domain, message: e.response?.data?.detail || e.message, status_code: 0 })
                        } finally {
                          setWpTesting(prev => ({ ...prev, [d.id]: false }))
                        }
                      }}
                      disabled={wpTesting[d.id]}
                      title="Test WP connection"
                      className="text-gray-400 hover:text-green-600 text-xs px-2 py-0.5 rounded hover:bg-green-50 transition-colors disabled:opacity-40"
                    >
                      {wpTesting[d.id] ? '⟳' : 'WP'}
                    </button>
                  </td>
                  <td className="px-4 py-3">
                    <button
                      onClick={() => liveCheck(d.id)}
                      disabled={liveChecking[d.id]}
                      title="Sprawdź live teraz"
                      className="text-gray-400 hover:text-green-600 text-xs px-2 py-0.5 rounded hover:bg-green-50 transition-colors disabled:opacity-40"
                    >
                      {liveChecking[d.id] ? '⟳' : d.from_snapshot ? '↻' : '✓'}
                    </button>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && !loading && (
                <tr><td colSpan={9} className="px-4 py-12 text-center text-gray-400">Brak domen</td></tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {domains.length < total && (
        <div className="mt-4 text-center">
          <button onClick={() => load(offset, true)} disabled={loading}
            className="px-6 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg text-sm font-medium disabled:opacity-50">
            {loading ? 'Ładowanie...' : `Załaduj więcej (${total - domains.length} pozostałych)`}
          </button>
        </div>
      )}
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function DomainHealth() {
  const [tab, setTab] = useState('live')

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold text-gray-900">Zdrowie Domen</h2>
          <p className="text-gray-500 text-sm mt-1">Monitoring ruchu organicznego, słów kluczowych, WP i wygaśnięcia</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-1 mb-6 w-fit">
        {[
          { k: 'live', label: 'Przegląd domen' },
          { k: 'history', label: 'Historia snapshotów' },
        ].map(({ k, label }) => (
          <button key={k} onClick={() => setTab(k)}
            className={`px-5 py-2 rounded-md text-sm font-medium transition-colors ${
              tab === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'
            }`}>{label}</button>
        ))}
      </div>

      {tab === 'live' ? <LiveTab /> : <SnapshotsTab />}
    </div>
  )
}
