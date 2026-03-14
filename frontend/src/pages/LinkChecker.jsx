import React, { useState, useEffect, useCallback } from 'react'
import api from '../api/client'

const Badge = ({ color, children }) => {
  const colors = {
    green: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-300',
    red: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-300',
    blue: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-300',
    yellow: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-300',
    gray: 'bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300',
    orange: 'bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-300',
    purple: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-300',
  }
  return (
    <span className={`inline-flex items-center px-2 py-0.5 rounded text-xs font-medium ${colors[color] || colors.gray}`}>
      {children}
    </span>
  )
}

const locationLabels = {
  menu: 'Menu / Nav',
  footer: 'Footer',
  sidebar: 'Sidebar',
  article: 'Artykul',
  error: 'Blad',
}

const locationColors = {
  menu: 'purple',
  footer: 'orange',
  sidebar: 'yellow',
  article: 'green',
  error: 'red',
}

export default function LinkChecker() {
  const [targetDomain, setTargetDomain] = useState('')
  const [batchTag, setBatchTag] = useState('')
  const [batches, setBatches] = useState([])
  const [scanning, setScanning] = useState(false)
  const [currentScanId, setCurrentScanId] = useState(null)
  const [progress, setProgress] = useState(null)
  const [results, setResults] = useState(null)
  const [recentScans, setRecentScans] = useState([])
  const [expandedDomain, setExpandedDomain] = useState(null)
  const [filterMode, setFilterMode] = useState('all') // all, with_links, without_links

  // Load batches and recent scans on mount
  useEffect(() => {
    api.get('/api/domains/batches').then(r => setBatches(r.data)).catch(() => {})
    api.get('/api/link-checker/scans').then(r => setRecentScans(r.data)).catch(() => {})
  }, [])

  // Poll progress while scanning
  useEffect(() => {
    if (!currentScanId || !scanning) return
    const iv = setInterval(async () => {
      try {
        const res = await api.get(`/api/link-checker/status/${currentScanId}`)
        setProgress(res.data)
        if (res.data.status === 'done' || res.data.status === 'error') {
          setScanning(false)
          clearInterval(iv)
          // Load results
          const rr = await api.get(`/api/link-checker/results/${currentScanId}`)
          setResults(rr.data)
          // Refresh scan list
          api.get('/api/link-checker/scans').then(r => setRecentScans(r.data)).catch(() => {})
        }
      } catch {
        // ignore
      }
    }, 2000)
    return () => clearInterval(iv)
  }, [currentScanId, scanning])

  const startScan = async () => {
    if (!targetDomain.trim()) return
    setScanning(true)
    setResults(null)
    setProgress(null)
    try {
      const payload = { target_domain: targetDomain.trim() }
      if (batchTag) payload.batch_tag = batchTag
      const res = await api.post('/api/link-checker/scan', payload)
      setCurrentScanId(res.data.scan_id)
    } catch (err) {
      setScanning(false)
      alert(err.response?.data?.detail || 'Blad startu skanowania')
    }
  }

  const loadResults = async (scanId) => {
    try {
      const res = await api.get(`/api/link-checker/results/${scanId}`)
      setResults(res.data)
      setCurrentScanId(scanId)
    } catch {
      alert('Nie udalo sie wczytac wynikow')
    }
  }

  const deleteScan = async (scanId) => {
    if (!confirm('Usunac wyniki tego skanu?')) return
    try {
      await api.delete(`/api/link-checker/scan/${scanId}`)
      setRecentScans(prev => prev.filter(s => s.scan_id !== scanId))
      if (currentScanId === scanId) {
        setResults(null)
        setCurrentScanId(null)
      }
    } catch {
      alert('Blad usuwania')
    }
  }

  const filteredDomains = results?.domains?.filter(d => {
    if (filterMode === 'with_links') return d.has_link
    if (filterMode === 'without_links') return !d.has_link
    return true
  }) || []

  return (
    <div className="p-6 max-w-7xl mx-auto space-y-6">
      {/* Header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Link Checker</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Sprawdz czy na Twoich domenach PBN sa linki do wybranej domeny docelowej
        </p>
      </div>

      {/* Scan Form */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
        <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">Nowy skan</h2>
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex-1 min-w-[200px]">
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
              Domena docelowa (szukamy linkow do...)
            </label>
            <input
              type="text"
              value={targetDomain}
              onChange={e => setTargetDomain(e.target.value)}
              placeholder="np. apteczka24.pl"
              disabled={scanning}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div className="min-w-[180px]">
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">
              Paczka domen (opcjonalnie)
            </label>
            <select
              value={batchTag}
              onChange={e => setBatchTag(e.target.value)}
              disabled={scanning}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">Wszystkie aktywne</option>
              {batches.map(b => (
                <option key={b.batch_tag} value={b.batch_tag}>
                  {b.batch_tag} ({b.count} domen)
                </option>
              ))}
            </select>
          </div>
          <button
            onClick={startScan}
            disabled={scanning || !targetDomain.trim()}
            className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors whitespace-nowrap"
          >
            {scanning ? 'Skanowanie...' : 'Rozpocznij skan'}
          </button>
        </div>

        {/* Progress bar */}
        {scanning && progress && (
          <div className="mt-4 space-y-2">
            <div className="flex justify-between text-xs text-gray-500 dark:text-gray-400">
              <span>Domeny: {progress.domains_done} / {progress.domains_total}</span>
              <span>Strony: {progress.pages_checked} / {progress.pages_total || '...'}</span>
              <span>{progress.elapsed_sec}s</span>
            </div>
            <div className="w-full bg-gray-200 dark:bg-gray-700 rounded-full h-2">
              <div
                className="bg-blue-600 h-2 rounded-full transition-all duration-500"
                style={{ width: `${progress.domains_total ? (progress.domains_done / progress.domains_total * 100) : 0}%` }}
              />
            </div>
            {progress.errors_count > 0 && (
              <p className="text-xs text-red-500">{progress.errors_count} bledow</p>
            )}
          </div>
        )}
      </div>

      {/* Recent Scans */}
      {recentScans.length > 0 && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-3">Ostatnie skany</h2>
          <div className="space-y-2">
            {recentScans.map(s => (
              <div key={s.scan_id} className="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors">
                <div className="flex items-center gap-3">
                  <span className="text-sm font-medium text-gray-900 dark:text-white">{s.target_domain}</span>
                  <Badge color="blue">{s.domains_count} domen</Badge>
                  <Badge color={s.links_found > 0 ? 'green' : 'gray'}>{s.links_found || 0} linkow</Badge>
                  <span className="text-xs text-gray-400">{s.started_at}</span>
                </div>
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => loadResults(s.scan_id)}
                    className="text-xs text-blue-600 hover:text-blue-800 dark:text-blue-400 font-medium"
                  >
                    Zobacz
                  </button>
                  <button
                    onClick={() => deleteScan(s.scan_id)}
                    className="text-xs text-red-500 hover:text-red-700 font-medium"
                  >
                    Usun
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Results */}
      {results && (
        <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-6">
          <div className="flex flex-wrap items-center justify-between gap-4 mb-4">
            <div>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white">
                Wyniki: <span className="text-blue-600">{results.scan_id?.split('_')[0]}</span>
              </h2>
              <p className="text-xs text-gray-500 dark:text-gray-400 mt-1">
                {results.total_domains} domen &middot;{' '}
                <span className="text-green-600 font-medium">{results.with_links} z linkami</span> &middot;{' '}
                <span className="text-red-500 font-medium">{results.without_links} bez linkow</span>
              </p>
            </div>
            <div className="flex gap-2 items-center">
              <div className="flex gap-1">
                {['all', 'with_links', 'without_links'].map(mode => (
                  <button
                    key={mode}
                    onClick={() => setFilterMode(mode)}
                    className={`px-3 py-1.5 text-xs rounded-lg font-medium transition-colors ${
                      filterMode === mode
                        ? 'bg-blue-600 text-white'
                        : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                    }`}
                  >
                    {mode === 'all' ? 'Wszystkie' : mode === 'with_links' ? 'Z linkami' : 'Bez linkow'}
                  </button>
                ))}
              </div>
              <button
                onClick={async () => {
                  try {
                    const res = await api.get(`/api/link-checker/export/${results.scan_id}`, { responseType: 'blob' })
                    const url = URL.createObjectURL(new Blob([res.data], { type: 'text/csv' }))
                    const a = document.createElement('a')
                    a.href = url
                    const cd = res.headers['content-disposition'] || ''
                    const match = cd.match(/filename="?([^"]+)"?/)
                    a.download = match ? match[1] : `link_check_${results.scan_id}.csv`
                    a.click()
                    URL.revokeObjectURL(url)
                  } catch { alert('Blad eksportu') }
                }}
                className="px-3 py-1.5 text-xs rounded-lg font-medium bg-green-600 text-white hover:bg-green-700 transition-colors flex items-center gap-1"
              >
                <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" />
                </svg>
                Export CSV
              </button>
            </div>
          </div>

          {/* Results table */}
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-gray-200 dark:border-gray-700">
                  <th className="text-left py-2 px-3 text-xs font-medium text-gray-500 dark:text-gray-400">Domena</th>
                  <th className="text-left py-2 px-3 text-xs font-medium text-gray-500 dark:text-gray-400">Status</th>
                  <th className="text-left py-2 px-3 text-xs font-medium text-gray-500 dark:text-gray-400">Lokalizacja</th>
                  <th className="text-left py-2 px-3 text-xs font-medium text-gray-500 dark:text-gray-400">Anchor</th>
                  <th className="text-left py-2 px-3 text-xs font-medium text-gray-500 dark:text-gray-400">Akcje</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100 dark:divide-gray-700">
                {filteredDomains.map(d => (
                  <React.Fragment key={d.domain}>
                    <tr className="hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors">
                      <td className="py-2.5 px-3">
                        <span className="font-medium text-gray-900 dark:text-white">{d.domain}</span>
                      </td>
                      <td className="py-2.5 px-3">
                        {d.has_link ? (
                          <Badge color="green">Link znaleziony ({d.links.length})</Badge>
                        ) : (
                          <Badge color="red">Brak linka</Badge>
                        )}
                      </td>
                      <td className="py-2.5 px-3">
                        {d.has_link ? (
                          <div className="flex flex-wrap gap-1">
                            {[...new Set(d.links.map(l => l.location))].map(loc => (
                              <Badge key={loc} color={locationColors[loc] || 'gray'}>
                                {locationLabels[loc] || loc}
                              </Badge>
                            ))}
                          </div>
                        ) : (
                          <span className="text-gray-400">-</span>
                        )}
                      </td>
                      <td className="py-2.5 px-3 max-w-[200px] truncate text-gray-600 dark:text-gray-400">
                        {d.has_link ? d.links[0]?.anchor_text || '-' : '-'}
                      </td>
                      <td className="py-2.5 px-3">
                        {d.has_link && d.links.length > 0 && (
                          <button
                            onClick={() => setExpandedDomain(expandedDomain === d.domain ? null : d.domain)}
                            className="text-xs text-blue-600 hover:text-blue-800 dark:text-blue-400 font-medium"
                          >
                            {expandedDomain === d.domain ? 'Zwiń' : 'Szczegoly'}
                          </button>
                        )}
                      </td>
                    </tr>
                    {/* Expanded details */}
                    {expandedDomain === d.domain && d.links.map((link, idx) => (
                      <tr key={`${d.domain}-${idx}`} className="bg-gray-50 dark:bg-gray-700/30">
                        <td colSpan={5} className="py-3 px-6">
                          <div className="space-y-2 text-xs">
                            <div className="flex flex-wrap gap-4">
                              <div>
                                <span className="text-gray-500 dark:text-gray-400">Strona:</span>{' '}
                                <a href={link.page_url} target="_blank" rel="noopener noreferrer"
                                   className="text-blue-600 hover:underline break-all">
                                  {link.page_url}
                                </a>
                              </div>
                              <div>
                                <span className="text-gray-500 dark:text-gray-400">Lokalizacja:</span>{' '}
                                <Badge color={locationColors[link.location] || 'gray'}>
                                  {locationLabels[link.location] || link.location}
                                </Badge>
                              </div>
                            </div>
                            <div>
                              <span className="text-gray-500 dark:text-gray-400">Link URL:</span>{' '}
                              <a href={link.link_url} target="_blank" rel="noopener noreferrer"
                                 className="text-blue-600 hover:underline break-all">
                                {link.link_url}
                              </a>
                            </div>
                            <div>
                              <span className="text-gray-500 dark:text-gray-400">Anchor:</span>{' '}
                              <span className="text-gray-900 dark:text-white font-medium">{link.anchor_text || '(brak)'}</span>
                            </div>
                            {link.excerpt && (
                              <div className="mt-1 p-2 bg-white dark:bg-gray-800 rounded border border-gray-200 dark:border-gray-600 text-gray-700 dark:text-gray-300">
                                <span className="text-gray-500 dark:text-gray-400 block mb-1">Fragment:</span>
                                {link.excerpt}
                              </div>
                            )}
                          </div>
                        </td>
                      </tr>
                    ))}
                  </React.Fragment>
                ))}
                {filteredDomains.length === 0 && (
                  <tr>
                    <td colSpan={5} className="py-8 text-center text-gray-400">
                      Brak wynikow dla wybranego filtra
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  )
}
