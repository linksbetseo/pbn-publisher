import { useState, useEffect, useCallback } from 'react'
import api from '../api/client'

const PAGE_SIZE = 50

function ScoreBadge({ score }) {
  const map = {
    good:   { label: 'Dobry',   cls: 'bg-green-100 text-green-700' },
    medium: { label: 'Średni',  cls: 'bg-yellow-100 text-yellow-700' },
    weak:   { label: 'Słaby',   cls: 'bg-red-100 text-red-700' },
  }
  const { label, cls } = map[score] || { label: score, cls: 'bg-gray-100 text-gray-600' }
  return <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${cls}`}>{label}</span>
}

function ExpiryBadge({ days, date }) {
  if (days === null || days === undefined)
    return <span className="text-gray-400 text-xs">—</span>
  let cls = 'bg-green-100 text-green-700'
  if (days < 0) cls = 'bg-red-200 text-red-800'
  else if (days < 14) cls = 'bg-red-100 text-red-700'
  else if (days < 60) cls = 'bg-yellow-100 text-yellow-700'
  return (
    <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${cls}`} title={date || ''}>
      {days < 0 ? 'WYGASŁA' : `${days}d`}
    </span>
  )
}

function WpBadge({ ok }) {
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

// ── Historia zakładka ─────────────────────────────────────────────────────────

function SnapshotsTab() {
  const [snapshots, setSnapshots] = useState([])
  const [loading, setLoading] = useState(false)
  const [snapping, setSnapping] = useState(false)
  const [search, setSearch] = useState('')

  const load = async () => {
    setLoading(true)
    try {
      const res = await api.get('/api/health/snapshots', { params: { limit: 500 } })
      setSnapshots(res.data)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [])

  const triggerSnapshot = async () => {
    setSnapping(true)
    try {
      await api.post('/api/health/snapshot')
      setTimeout(load, 3000) // reload after 3s
    } catch (e) {
      console.error(e)
    } finally {
      setSnapping(false)
    }
  }

  const filtered = snapshots.filter(s =>
    !search || s.domain.toLowerCase().includes(search.toLowerCase())
  )

  // Group by snapped_at date for display
  const byDate = filtered.reduce((acc, s) => {
    const date = s.snapped_at?.slice(0, 10) || 'unknown'
    if (!acc[date]) acc[date] = []
    acc[date].push(s)
    return acc
  }, {})

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
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Domena</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Ruch</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Słowa kl.</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">WP</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Wygasa</th>
                      <th className="px-4 py-2 text-left text-xs font-semibold text-gray-500">Zdrowie</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-50">
                    {rows.map(s => (
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

// ── Live zakładka ─────────────────────────────────────────────────────────────

function HttpAuthModal({ domain, onClose }) {
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
      alert('Błąd zapisu: ' + (e.response?.data?.detail || e.message))
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

function LiveTab() {
  const [domains, setDomains] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('traffic')
  const [sortDir, setSortDir] = useState('desc')
  const [filter, setFilter] = useState('all')
  const [authModal, setAuthModal] = useState(null)

  const load = useCallback(async (off = 0, append = false) => {
    setLoading(true)
    try {
      const res = await api.get('/api/health', { params: { limit: PAGE_SIZE, offset: off } })
      setTotal(res.data.total)
      setDomains(prev => append ? [...prev, ...res.data.domains] : res.data.domains)
      setOffset(off + PAGE_SIZE)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { load(0) }, [load])

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
      if (filter === 'expiring') return d.days_to_expiry !== null && d.days_to_expiry < 60
      if (filter === 'wp_err') return !d.wp_ok
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
  const expiring = domains.filter(d => d.days_to_expiry !== null && d.days_to_expiry < 60).length
  const wpErrors = domains.filter(d => !d.wp_ok).length
  const avgTraffic = domains.length ? Math.round(domains.reduce((s, d) => s + (d.traffic || 0), 0) / domains.length) : 0

  const SortIcon = ({ k }) => sortKey !== k
    ? <span className="text-gray-300 ml-1">↕</span>
    : <span className="ml-1">{sortDir === 'asc' ? '↑' : '↓'}</span>

  const thCls = "px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide cursor-pointer hover:text-gray-800 select-none"

  return (
    <div>
      {authModal && <HttpAuthModal domain={authModal} onClose={() => setAuthModal(null)} />}
      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3 mb-5">
        <StatCard label="Wszystkie" value={total} color="text-gray-800" />
        <StatCard label="Dobre" value={good} color="text-green-600" sub="ruch≥500 lub kw≥200"
          onClick={() => setFilter(f => f === 'good' ? 'all' : 'good')} active={filter === 'good'} />
        <StatCard label="Średnie" value={medium} color="text-yellow-600"
          onClick={() => setFilter(f => f === 'medium' ? 'all' : 'medium')} active={filter === 'medium'} />
        <StatCard label="Słabe" value={weak} color="text-red-600"
          onClick={() => setFilter(f => f === 'weak' ? 'all' : 'weak')} active={filter === 'weak'} />
        <StatCard label="Wygasające" value={expiring} color="text-orange-600" sub="<60 dni"
          onClick={() => setFilter(f => f === 'expiring' ? 'all' : 'expiring')} active={filter === 'expiring'} />
        <StatCard label="WP błąd" value={wpErrors} color="text-red-500"
          onClick={() => setFilter(f => f === 'wp_err' ? 'all' : 'wp_err')} active={filter === 'wp_err'} />
      </div>
      <div className="text-xs text-gray-400 mb-4">Śr. ruch: {avgTraffic.toLocaleString()} / mies. · {domains.length} z {total} załadowanych</div>

      {/* Search + filter */}
      <div className="flex flex-wrap gap-3 mb-4">
        <input
          type="text"
          placeholder="Szukaj domeny..."
          value={search}
          onChange={e => setSearch(e.target.value)}
          className="border border-gray-200 rounded-lg px-3 py-2 text-sm w-56 focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <div className="flex gap-1 bg-gray-100 rounded-lg p-1">
          {[
            { k: 'all', label: 'Wszystkie' },
            { k: 'good', label: 'Dobre' },
            { k: 'medium', label: 'Średnie' },
            { k: 'weak', label: 'Słabe' },
            { k: 'expiring', label: 'Wygasające' },
            { k: 'wp_err', label: 'WP błąd' },
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
            Pobieranie danych... (może chwilę potrwać)
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
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {filtered.map(d => (
                <tr key={d.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3">
                    <a href={`https://${d.domain.replace(/^https?:\/\//, '')}`}
                      target="_blank" rel="noopener noreferrer"
                      className="text-blue-600 hover:underline font-medium text-xs">
                      {d.domain.replace(/^https?:\/\//, '')}
                    </a>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{d.server || '—'}</td>
                  <td className="px-4 py-3 font-medium">
                    {d.traffic > 0
                      ? <span className={d.traffic >= 500 ? 'text-green-600' : d.traffic >= 50 ? 'text-yellow-600' : 'text-gray-500'}>{d.traffic.toLocaleString()}</span>
                      : <span className="text-gray-300">0</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {d.keywords > 0 ? d.keywords.toLocaleString() : <span className="text-gray-300">0</span>}
                  </td>
                  <td className="px-4 py-3"><ExpiryBadge days={d.days_to_expiry} date={d.expiry_date} /></td>
                  <td className="px-4 py-3"><WpBadge ok={d.wp_ok} /></td>
                  <td className="px-4 py-3"><ScoreBadge score={d.health_score} /></td>
                  <td className="px-4 py-3">
                    <button onClick={() => setAuthModal(d)}
                      title="Ustaw Basic Auth (htpasswd)"
                      className="text-gray-400 hover:text-blue-600 text-sm px-2 py-0.5 rounded hover:bg-blue-50 transition-colors">
                      🔑
                    </button>
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && !loading && (
                <tr><td colSpan={8} className="px-4 py-12 text-center text-gray-400">Brak domen</td></tr>
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
          <p className="text-gray-500 text-sm mt-1">Monitoring ruchu, słów kluczowych, WP i wygaśnięcia domen</p>
        </div>
      </div>

      {/* Tabs */}
      <div className="flex gap-1 bg-gray-100 rounded-lg p-1 mb-6 w-fit">
        {[
          { k: 'live', label: 'Live check' },
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
