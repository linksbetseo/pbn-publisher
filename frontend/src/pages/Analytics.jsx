import { useState, useEffect } from 'react'
import { analytics } from '../api/client'
import { clients as clientsApi, domains as domainsApi } from '../api/client'
import { useToast } from '../components/Toast'

function Spinner() {
  return (
    <div className="flex items-center justify-center py-16">
      <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
    </div>
  )
}

function TabButton({ active, onClick, children }) {
  return (
    <button
      onClick={onClick}
      className={`px-5 py-2.5 text-sm font-medium rounded-lg transition-colors ${
        active
          ? 'bg-blue-600 text-white'
          : 'text-gray-600 dark:text-gray-300 hover:bg-gray-100 dark:hover:bg-gray-700'
      }`}
    >
      {children}
    </button>
  )
}

// ── Anchor Diversity Tab ──────────────────────────────────────

function AnchorDiversityTab() {
  const [clientDomains, setClientDomains] = useState([])
  const [selectedDomain, setSelectedDomain] = useState('')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const addToast = useToast()

  useEffect(() => {
    clientsApi.list().then(res => {
      const allDomains = []
      ;(res.data || []).forEach(c => {
        ;(c.domains || []).forEach(d => {
          if (!allDomains.includes(d.domain)) allDomains.push(d.domain)
        })
      })
      setClientDomains(allDomains)
      if (allDomains.length > 0) setSelectedDomain(allDomains[0])
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedDomain) return
    setLoading(true)
    analytics.anchorDiversity(selectedDomain)
      .then(res => setData(res.data))
      .catch(e => addToast(e.response?.data?.detail || 'Blad ladowania anchor diversity', 'error'))
      .finally(() => setLoading(false))
  }, [selectedDomain])

  if (loading) return <Spinner />

  return (
    <div className="space-y-6">
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Domena klienta</label>
        <select
          value={selectedDomain}
          onChange={e => setSelectedDomain(e.target.value)}
          className="border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 w-full max-w-md"
        >
          {clientDomains.map(d => (
            <option key={d} value={d}>{d}</option>
          ))}
        </select>
      </div>

      {data && (
        <>
          {/* Score card */}
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <div className="bg-blue-50 dark:bg-blue-900/30 rounded-xl p-5 border border-blue-100 dark:border-blue-800">
              <p className="text-xs font-medium text-blue-600 dark:text-blue-400 uppercase tracking-wider">Diversity Score</p>
              <p className="text-3xl font-bold text-blue-700 dark:text-blue-300 mt-1">{data.diversity_score ?? '—'}%</p>
            </div>
            <div className="bg-green-50 dark:bg-green-900/30 rounded-xl p-5 border border-green-100 dark:border-green-800">
              <p className="text-xs font-medium text-green-600 dark:text-green-400 uppercase tracking-wider">Unikalne kotwice</p>
              <p className="text-3xl font-bold text-green-700 dark:text-green-300 mt-1">{data.unique_anchors ?? '—'}</p>
            </div>
            <div className="bg-purple-50 dark:bg-purple-900/30 rounded-xl p-5 border border-purple-100 dark:border-purple-800">
              <p className="text-xs font-medium text-purple-600 dark:text-purple-400 uppercase tracking-wider">Linki</p>
              <p className="text-3xl font-bold text-purple-700 dark:text-purple-300 mt-1">{data.total_links ?? '—'}</p>
            </div>
          </div>

          {/* Warnings */}
          {data.warnings && data.warnings.length > 0 && (
            <div className="space-y-2">
              {data.warnings.map((w, i) => (
                <div key={i} className="bg-orange-50 dark:bg-orange-900/30 border border-orange-200 dark:border-orange-700 rounded-lg px-4 py-3 text-sm text-orange-700 dark:text-orange-300">
                  {w}
                </div>
              ))}
            </div>
          )}

          {/* Bar chart */}
          {data.distribution && data.distribution.length > 0 && (
            <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
              <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-4">Rozklad kotwic</h4>
              <div className="space-y-3">
                {data.distribution.map((item, i) => {
                  const maxPct = Math.max(...data.distribution.map(d => d.percentage || 0), 1)
                  const barW = ((item.percentage || 0) / maxPct) * 100
                  return (
                    <div key={i} className="flex items-center gap-3">
                      <span className="text-xs text-gray-600 dark:text-gray-400 w-40 truncate" title={item.anchor}>{item.anchor}</span>
                      <div className="flex-1 bg-gray-100 dark:bg-gray-700 rounded-full h-5 overflow-hidden">
                        <div
                          className="h-full bg-blue-500 rounded-full transition-all"
                          style={{ width: `${barW}%` }}
                        />
                      </div>
                      <span className="text-xs font-medium text-gray-700 dark:text-gray-300 w-14 text-right">
                        {item.percentage?.toFixed(1)}% ({item.count})
                      </span>
                    </div>
                  )
                })}
              </div>
            </div>
          )}
        </>
      )}

      {!data && !loading && (
        <p className="text-gray-400 dark:text-gray-500 text-sm">Wybierz domene klienta, aby zobaczyc analityke.</p>
      )}
    </div>
  )
}

// ── Link Velocity Tab ─────────────────────────────────────────

function LinkVelocityTab() {
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(true)
  const addToast = useToast()

  useEffect(() => {
    analytics.linkVelocity()
      .then(res => setData(res.data))
      .catch(e => addToast(e.response?.data?.detail || 'Blad ladowania link velocity', 'error'))
      .finally(() => setLoading(false))
  }, [])

  if (loading) return <Spinner />
  if (!data) return <p className="text-gray-400 text-sm">Brak danych.</p>

  const weeks = data.weeks || []
  const domains = data.domains || []
  const maxWeekly = Math.max(...weeks.map(w => w.count || 0), 1)

  return (
    <div className="space-y-6">
      {/* Spike warnings */}
      {data.warnings && data.warnings.length > 0 && (
        <div className="space-y-2">
          {data.warnings.map((w, i) => (
            <div key={i} className="bg-red-50 dark:bg-red-900/30 border border-red-200 dark:border-red-700 rounded-lg px-4 py-3 text-sm text-red-700 dark:text-red-300">
              {w}
            </div>
          ))}
        </div>
      )}

      {/* Weekly chart */}
      {weeks.length > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
          <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-4">Publikacje na tydzien</h4>
          <div className="space-y-2">
            {weeks.map((w, i) => (
              <div key={i} className="flex items-center gap-3">
                <span className="text-xs text-gray-500 dark:text-gray-400 w-24 font-mono">{w.week}</span>
                <div className="flex-1 bg-gray-100 dark:bg-gray-700 rounded-full h-6 overflow-hidden">
                  <div
                    className="h-full bg-green-500 rounded-full transition-all flex items-center justify-end pr-2"
                    style={{ width: `${((w.count || 0) / maxWeekly) * 100}%`, minWidth: w.count > 0 ? '24px' : '0' }}
                  >
                    {w.count > 0 && <span className="text-xs text-white font-medium">{w.count}</span>}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Per-domain velocity */}
      {domains.length > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
          <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-4">Predkosc publikacji per domena</h4>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider border-b border-gray-200 dark:border-gray-700">
                  <th className="pb-2 pr-4">Domena</th>
                  <th className="pb-2 pr-4">Ostatni tydzien</th>
                  <th className="pb-2 pr-4">Ostatni miesiac</th>
                  <th className="pb-2">Srednia/tydzien</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {domains.map((d, i) => (
                  <tr key={i} className="text-gray-700 dark:text-gray-300">
                    <td className="py-2 pr-4 font-medium truncate max-w-[200px]">{d.domain}</td>
                    <td className="py-2 pr-4">{d.last_week ?? '—'}</td>
                    <td className="py-2 pr-4">{d.last_month ?? '—'}</td>
                    <td className="py-2">{d.avg_per_week?.toFixed(1) ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}

// ── Content Uniqueness Tab ────────────────────────────────────

function ContentUniquenessTab() {
  const [domainList, setDomainList] = useState([])
  const [selectedDomain, setSelectedDomain] = useState('')
  const [data, setData] = useState(null)
  const [loading, setLoading] = useState(false)
  const addToast = useToast()

  useEffect(() => {
    domainsApi.list().then(res => {
      const list = res.data || []
      setDomainList(list)
      if (list.length > 0) setSelectedDomain(String(list[0].id))
    }).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedDomain) return
    setLoading(true)
    analytics.contentUniqueness(parseInt(selectedDomain))
      .then(res => setData(res.data))
      .catch(e => addToast(e.response?.data?.detail || 'Blad ladowania content uniqueness', 'error'))
      .finally(() => setLoading(false))
  }, [selectedDomain])

  if (loading) return <Spinner />

  return (
    <div className="space-y-6">
      <div>
        <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Domena PBN</label>
        <select
          value={selectedDomain}
          onChange={e => setSelectedDomain(e.target.value)}
          className="border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 w-full max-w-md"
        >
          {domainList.map(d => (
            <option key={d.id} value={d.id}>{d.domain}</option>
          ))}
        </select>
      </div>

      {data && (
        <>
          {/* Score */}
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div className="bg-emerald-50 dark:bg-emerald-900/30 rounded-xl p-5 border border-emerald-100 dark:border-emerald-800">
              <p className="text-xs font-medium text-emerald-600 dark:text-emerald-400 uppercase tracking-wider">Srednia unikalnosc</p>
              <p className="text-3xl font-bold text-emerald-700 dark:text-emerald-300 mt-1">{data.avg_uniqueness?.toFixed(1) ?? '—'}%</p>
            </div>
            <div className="bg-amber-50 dark:bg-amber-900/30 rounded-xl p-5 border border-amber-100 dark:border-amber-800">
              <p className="text-xs font-medium text-amber-600 dark:text-amber-400 uppercase tracking-wider">Artykuly analizowane</p>
              <p className="text-3xl font-bold text-amber-700 dark:text-amber-300 mt-1">{data.total_articles ?? '—'}</p>
            </div>
          </div>

          {/* Warnings */}
          {data.warnings && data.warnings.length > 0 && (
            <div className="space-y-2">
              {data.warnings.map((w, i) => (
                <div key={i} className="bg-orange-50 dark:bg-orange-900/30 border border-orange-200 dark:border-orange-700 rounded-lg px-4 py-3 text-sm text-orange-700 dark:text-orange-300">
                  {w}
                </div>
              ))}
            </div>
          )}

          {/* Similar pairs table */}
          {data.similar_pairs && data.similar_pairs.length > 0 && (
            <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
              <h4 className="text-sm font-semibold text-gray-800 dark:text-gray-200 mb-4">Podobne pary artykulow</h4>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="text-left text-xs text-gray-500 dark:text-gray-400 uppercase tracking-wider border-b border-gray-200 dark:border-gray-700">
                      <th className="pb-2 pr-4">Artykul A</th>
                      <th className="pb-2 pr-4">Artykul B</th>
                      <th className="pb-2">Podobienstwo</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                    {data.similar_pairs.map((pair, i) => {
                      const pct = pair.similarity ?? 0
                      const badgeColor = pct >= 80 ? 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
                        : pct >= 60 ? 'bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300'
                        : 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300'
                      return (
                        <tr key={i} className="text-gray-700 dark:text-gray-300">
                          <td className="py-2 pr-4 truncate max-w-[250px]" title={pair.title_a}>{pair.title_a}</td>
                          <td className="py-2 pr-4 truncate max-w-[250px]" title={pair.title_b}>{pair.title_b}</td>
                          <td className="py-2">
                            <span className={`px-2 py-0.5 rounded-full text-xs font-semibold ${badgeColor}`}>
                              {pct.toFixed(0)}%
                            </span>
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}

          {data.similar_pairs && data.similar_pairs.length === 0 && (
            <div className="bg-green-50 dark:bg-green-900/30 border border-green-200 dark:border-green-700 rounded-lg px-4 py-3 text-sm text-green-700 dark:text-green-300">
              Brak podejrzanych par - wszystkie artykuly sa unikalne.
            </div>
          )}
        </>
      )}
    </div>
  )
}

// ── Main Analytics Page ───────────────────────────────────────

export default function Analytics() {
  const [tab, setTab] = useState('anchor')

  return (
    <div className="p-8 max-w-6xl">
      <h2 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">Analityka</h2>
      <p className="text-gray-500 dark:text-gray-400 mb-6">Anchor diversity, link velocity i content uniqueness</p>

      {/* Tab navigation */}
      <div className="flex gap-2 mb-6 bg-gray-100 dark:bg-gray-800 rounded-xl p-1 w-fit">
        <TabButton active={tab === 'anchor'} onClick={() => setTab('anchor')}>
          Anchor Diversity
        </TabButton>
        <TabButton active={tab === 'velocity'} onClick={() => setTab('velocity')}>
          Link Velocity
        </TabButton>
        <TabButton active={tab === 'uniqueness'} onClick={() => setTab('uniqueness')}>
          Content Uniqueness
        </TabButton>
      </div>

      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        {tab === 'anchor' && <AnchorDiversityTab />}
        {tab === 'velocity' && <LinkVelocityTab />}
        {tab === 'uniqueness' && <ContentUniquenessTab />}
      </div>
    </div>
  )
}
