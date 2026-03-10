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
  if (days === null || days === undefined) {
    return <span className="text-gray-400 text-xs">—</span>
  }
  let cls = 'bg-green-100 text-green-700'
  if (days < 14) cls = 'bg-red-100 text-red-700'
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
    : <span className="inline-flex items-center gap-1 text-red-500 text-xs font-medium"><span className="w-2 h-2 rounded-full bg-red-500 inline-block" />Błąd</span>
}

function StatCard({ label, value, sub, color }) {
  return (
    <div className="bg-white rounded-xl border border-gray-100 shadow-sm p-5">
      <p className="text-xs text-gray-500 font-medium uppercase tracking-wide">{label}</p>
      <p className={`text-3xl font-bold mt-1 ${color}`}>{value}</p>
      {sub && <p className="text-xs text-gray-400 mt-1">{sub}</p>}
    </div>
  )
}

export default function DomainHealth() {
  const [domains, setDomains] = useState([])
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [loading, setLoading] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [search, setSearch] = useState('')
  const [sortKey, setSortKey] = useState('dr')
  const [sortDir, setSortDir] = useState('desc')
  const [filter, setFilter] = useState('all') // all | good | medium | weak | expiring | wp_err

  const load = useCallback(async (off = 0, append = false) => {
    setLoading(true)
    try {
      const res = await api.get('/api/health', { params: { limit: PAGE_SIZE, offset: off } })
      const data = res.data
      setTotal(data.total)
      setDomains(prev => append ? [...prev, ...data.domains] : data.domains)
      setOffset(off + PAGE_SIZE)
    } catch (e) {
      console.error(e)
    } finally {
      setLoading(false)
      setRefreshing(false)
    }
  }, [])

  useEffect(() => { load(0) }, [load])

  const refresh = () => {
    setRefreshing(true)
    setOffset(0)
    load(0, false)
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
      if (filter === 'expiring') return d.days_to_expiry !== null && d.days_to_expiry < 60
      if (filter === 'wp_err') return !d.wp_ok
      return true
    })
    .sort((a, b) => {
      const av = a[sortKey] ?? -1
      const bv = b[sortKey] ?? -1
      return sortDir === 'asc' ? (av > bv ? 1 : -1) : (av < bv ? 1 : -1)
    })

  // Summary stats
  const good = domains.filter(d => d.health_score === 'good').length
  const medium = domains.filter(d => d.health_score === 'medium').length
  const weak = domains.filter(d => d.health_score === 'weak').length
  const expiring = domains.filter(d => d.days_to_expiry !== null && d.days_to_expiry < 60).length
  const wpErrors = domains.filter(d => !d.wp_ok).length
  const avgTraffic = domains.length ? Math.round(domains.reduce((s, d) => s + (d.traffic || 0), 0) / domains.length) : 0
  const avgDr = domains.length ? Math.round(domains.reduce((s, d) => s + (d.dr || 0), 0) / domains.length) : 0

  const SortIcon = ({ k }) => {
    if (sortKey !== k) return <span className="text-gray-300 ml-1">↕</span>
    return <span className="ml-1">{sortDir === 'asc' ? '↑' : '↓'}</span>
  }

  const thCls = "px-4 py-3 text-left text-xs font-semibold text-gray-500 uppercase tracking-wide cursor-pointer hover:text-gray-800 select-none"

  return (
    <div className="p-6">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h2 className="text-2xl font-bold text-gray-900">Zdrowie Domen</h2>
          <p className="text-gray-500 text-sm mt-1">
            {domains.length} z {total} domen załadowanych
          </p>
        </div>
        <button
          onClick={refresh}
          disabled={refreshing || loading}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
        >
          <svg className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
          </svg>
          Odśwież
        </button>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 sm:grid-cols-4 lg:grid-cols-7 gap-3 mb-6">
        <StatCard label="Wszystkie" value={total} color="text-gray-800" />
        <StatCard label="Dobre" value={good} color="text-green-600" sub="DR≥20, ruch≥100" />
        <StatCard label="Średnie" value={medium} color="text-yellow-600" />
        <StatCard label="Słabe" value={weak} color="text-red-600" />
        <StatCard label="Wygasające" value={expiring} color="text-orange-600" sub="<60 dni" />
        <StatCard label="WP błąd" value={wpErrors} color="text-red-500" />
        <StatCard label="Śr. DR" value={avgDr} color="text-blue-600" sub={`Śr. ruch: ${avgTraffic}`} />
      </div>

      {/* Filters */}
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
            <button
              key={k}
              onClick={() => setFilter(k)}
              className={`px-3 py-1 rounded-md text-xs font-medium transition-colors ${
                filter === k ? 'bg-white text-blue-600 shadow-sm' : 'text-gray-500 hover:text-gray-700'
              }`}
            >
              {label}
            </button>
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
                <th className={thCls} onClick={() => handleSort('domain')}>
                  Domena <SortIcon k="domain" />
                </th>
                <th className={thCls} onClick={() => handleSort('server')}>
                  Serwer <SortIcon k="server" />
                </th>
                <th className={thCls} onClick={() => handleSort('dr')}>
                  DR <SortIcon k="dr" />
                </th>
                <th className={thCls} onClick={() => handleSort('traffic')}>
                  Ruch org. <SortIcon k="traffic" />
                </th>
                <th className={thCls} onClick={() => handleSort('keywords')}>
                  Słowa kl. <SortIcon k="keywords" />
                </th>
                <th className={thCls} onClick={() => handleSort('days_to_expiry')}>
                  Wygasa <SortIcon k="days_to_expiry" />
                </th>
                <th className={thCls}>WP</th>
                <th className={thCls} onClick={() => handleSort('health_score')}>
                  Zdrowie <SortIcon k="health_score" />
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-50">
              {filtered.map(d => (
                <tr key={d.id} className="hover:bg-gray-50 transition-colors">
                  <td className="px-4 py-3">
                    <a
                      href={`https://${d.domain.replace(/^https?:\/\//, '')}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="text-blue-600 hover:underline font-medium"
                    >
                      {d.domain.replace(/^https?:\/\//, '')}
                    </a>
                  </td>
                  <td className="px-4 py-3 text-gray-500 text-xs">{d.server || '—'}</td>
                  <td className="px-4 py-3">
                    <span className={`font-bold ${d.dr >= 20 ? 'text-green-600' : d.dr >= 10 ? 'text-yellow-600' : 'text-red-500'}`}>
                      {d.dr || 0}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {d.traffic > 0 ? d.traffic.toLocaleString() : <span className="text-gray-300">0</span>}
                  </td>
                  <td className="px-4 py-3 text-gray-700">
                    {d.keywords > 0 ? d.keywords.toLocaleString() : <span className="text-gray-300">0</span>}
                  </td>
                  <td className="px-4 py-3">
                    <ExpiryBadge days={d.days_to_expiry} date={d.expiry_date} />
                  </td>
                  <td className="px-4 py-3">
                    <WpBadge ok={d.wp_ok} />
                  </td>
                  <td className="px-4 py-3">
                    <ScoreBadge score={d.health_score} />
                  </td>
                </tr>
              ))}
              {filtered.length === 0 && !loading && (
                <tr>
                  <td colSpan={8} className="px-4 py-12 text-center text-gray-400">
                    Brak domen spełniających kryteria
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </div>

      {/* Load more */}
      {domains.length < total && (
        <div className="mt-4 text-center">
          <button
            onClick={() => load(offset, true)}
            disabled={loading}
            className="px-6 py-2 bg-gray-100 hover:bg-gray-200 text-gray-700 rounded-lg text-sm font-medium disabled:opacity-50"
          >
            {loading ? 'Ładowanie...' : `Załaduj więcej (${total - domains.length} pozostałych)`}
          </button>
        </div>
      )}
    </div>
  )
}
