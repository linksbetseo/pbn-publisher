import { useState, useEffect, useRef } from 'react'
import { internalLinks, domains } from '../api/client'
import { useToast } from '../components/Toast'

export default function InternalLinks() {
  const addToast = useToast()
  const [domainList, setDomainList] = useState([])
  const [selectedDomain, setSelectedDomain] = useState('')
  const [lang, setLang] = useState('pl')
  const [jobs, setJobs] = useState([])
  const [activeJob, setActiveJob] = useState(null)
  const [suggestions, setSuggestions] = useState([])
  const [statusFilter, setStatusFilter] = useState('pending')
  const [selected, setSelected] = useState(new Set())
  const [analyzing, setAnalyzing] = useState(false)
  const [applying, setApplying] = useState(false)
  const [loading, setLoading] = useState(false)
  const pollRef = useRef(null)

  // Load domains
  useEffect(() => {
    domains.list({ active: 1 }).then(r => {
      setDomainList(r.data.domains || r.data || [])
    }).catch(() => {})
  }, [])

  // Load jobs when domain changes
  useEffect(() => {
    if (!selectedDomain) { setJobs([]); setActiveJob(null); setSuggestions([]); return }
    loadJobs(selectedDomain)
  }, [selectedDomain])

  // Poll active job status
  useEffect(() => {
    if (pollRef.current) clearInterval(pollRef.current)
    if (!activeJob || !['pending', 'clustering', 'analyzing'].includes(activeJob.status)) return
    pollRef.current = setInterval(async () => {
      try {
        const r = await internalLinks.getJobStatus(activeJob.id)
        setActiveJob(prev => ({ ...prev, ...r.data }))
        if (!['pending', 'clustering', 'analyzing'].includes(r.data.status)) {
          clearInterval(pollRef.current)
          setAnalyzing(false)
          // Reload suggestions
          loadSuggestions(activeJob.id, statusFilter)
          addToast(r.data.status === 'done'
            ? `Analiza zakończona — ${r.data.suggestions_total} propozycji`
            : `Błąd analizy: ${r.data.error || 'nieznany'}`,
            r.data.status === 'done' ? 'success' : 'error'
          )
        }
      } catch {}
    }, 3000)
    return () => clearInterval(pollRef.current)
  }, [activeJob?.id, activeJob?.status])

  async function loadJobs(domainId) {
    try {
      const r = await internalLinks.listJobs(domainId)
      const jobList = r.data || []
      setJobs(jobList)
      if (jobList.length > 0) {
        const latest = jobList[0]
        setActiveJob(latest)
        if (latest.status === 'done') {
          loadSuggestions(latest.id, statusFilter)
        }
        if (['pending', 'clustering', 'analyzing'].includes(latest.status)) {
          setAnalyzing(true)
        }
      } else {
        setActiveJob(null)
        setSuggestions([])
      }
    } catch {}
  }

  async function loadSuggestions(jobId, filter) {
    setLoading(true)
    try {
      const r = await internalLinks.getJob(jobId, filter)
      setSuggestions(r.data.suggestions || [])
      setSelected(new Set())
    } catch {
      setSuggestions([])
    } finally {
      setLoading(false)
    }
  }

  async function handleAnalyze() {
    if (!selectedDomain) return addToast('Wybierz domenę', 'error')
    setAnalyzing(true)
    setSuggestions([])
    setSelected(new Set())
    try {
      const r = await internalLinks.analyze({ my_domain_id: parseInt(selectedDomain), lang })
      setActiveJob(r.data)
      setJobs(prev => [r.data, ...prev])
      addToast('Analiza uruchomiona...', 'success')
    } catch (e) {
      console.error('[InternalLinks] analyze error:', e.response?.data || e.message)
      setAnalyzing(false)
      const msg = e.response?.data?.detail || e.response?.data?.message || e.message || 'Błąd uruchamiania analizy'
      addToast(msg, 'error')
    }
  }

  async function handleApproveSelected() {
    if (!selected.size) return
    try {
      await internalLinks.approve([...selected])
      addToast(`Zatwierdzono ${selected.size} propozycji`, 'success')
      loadSuggestions(activeJob.id, statusFilter)
    } catch {
      addToast('Błąd zatwierdzania', 'error')
    }
  }

  async function handleRejectSelected() {
    if (!selected.size) return
    try {
      await internalLinks.reject([...selected])
      addToast(`Odrzucono ${selected.size} propozycji`, 'success')
      loadSuggestions(activeJob.id, statusFilter)
    } catch {
      addToast('Błąd odrzucania', 'error')
    }
  }

  async function handleApplyApproved() {
    // Get all approved suggestion IDs
    try {
      const r = await internalLinks.getJob(activeJob.id, 'approved')
      const approvedIds = (r.data.suggestions || []).map(s => s.id)
      if (!approvedIds.length) return addToast('Brak zatwierdzonych propozycji', 'error')
      setApplying(true)
      const res = await internalLinks.apply(approvedIds)
      const d = res.data
      addToast(`Wstawiono ${d.applied} linków${d.failed > 0 ? `, ${d.failed} błędów` : ''}`, d.failed > 0 ? 'warning' : 'success')
      loadSuggestions(activeJob.id, statusFilter)
      // Refresh job
      const jr = await internalLinks.getJobStatus(activeJob.id)
      setActiveJob(prev => ({ ...prev, ...jr.data }))
    } catch (e) {
      addToast(e.response?.data?.detail || 'Błąd wstawiania linków', 'error')
    } finally {
      setApplying(false)
    }
  }

  function toggleSelect(id) {
    setSelected(prev => {
      const n = new Set(prev)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  }

  function toggleAll() {
    if (selected.size === suggestions.length) {
      setSelected(new Set())
    } else {
      setSelected(new Set(suggestions.map(s => s.id)))
    }
  }

  const statusLabel = {
    pending: 'Oczekujące',
    approved: 'Zatwierdzone',
    rejected: 'Odrzucone',
    applied: 'Wstawione',
    all: 'Wszystkie',
  }

  const statusColor = {
    done: 'text-green-600 dark:text-green-400',
    pending: 'text-yellow-600 dark:text-yellow-400',
    clustering: 'text-blue-600 dark:text-blue-400',
    analyzing: 'text-blue-600 dark:text-blue-400',
    error: 'text-red-600 dark:text-red-400',
  }

  const isRunning = activeJob && ['pending', 'clustering', 'analyzing'].includes(activeJob.status)

  // Group suggestions by source article
  const grouped = suggestions.reduce((acc, s) => {
    const key = s.source_post_id
    if (!acc[key]) acc[key] = { title: s.source_title, url: s.source_url, items: [] }
    acc[key].items.push(s)
    return acc
  }, {})

  return (
    <div className="p-6 max-w-5xl mx-auto">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">Linkowanie Wewnętrzne</h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Semantyczna analiza i wstawianie linków wewnętrznych do opublikowanych artykułów
        </p>
      </div>

      {/* Config panel */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 p-5 mb-6">
        <div className="flex flex-wrap gap-4 items-end">
          <div className="flex-1 min-w-48">
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Domena</label>
            <select
              value={selectedDomain}
              onChange={e => setSelectedDomain(e.target.value)}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="">— wybierz domenę —</option>
              {domainList.map(d => (
                <option key={d.id} value={d.id}>{d.domain}</option>
              ))}
            </select>
          </div>
          <div className="w-32">
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Język</label>
            <select
              value={lang}
              onChange={e => setLang(e.target.value)}
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              <option value="pl">Polski</option>
              <option value="en">English</option>
              <option value="de">Deutsch</option>
              <option value="fr">Français</option>
            </select>
          </div>
          <button
            onClick={handleAnalyze}
            disabled={!selectedDomain || analyzing}
            className="px-5 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors flex items-center gap-2"
          >
            {analyzing ? (
              <>
                <svg className="w-4 h-4 animate-spin" fill="none" viewBox="0 0 24 24">
                  <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                  <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                </svg>
                Analizuję...
              </>
            ) : 'Analizuj domenę'}
          </button>
        </div>

        {/* Job status bar */}
        {activeJob && (
          <div className="mt-4 pt-4 border-t border-gray-100 dark:border-gray-700 flex flex-wrap gap-6 text-sm">
            <div>
              <span className="text-gray-500 dark:text-gray-400">Status: </span>
              <span className={`font-medium ${statusColor[activeJob.status] || 'text-gray-700 dark:text-gray-300'}`}>
                {activeJob.status === 'clustering' ? 'Klasteryzacja tematyczna...'
                  : activeJob.status === 'analyzing' ? 'Szukam miejsc na linki...'
                  : activeJob.status === 'pending' ? 'Pobieranie artykułów...'
                  : activeJob.status === 'done' ? 'Gotowe'
                  : activeJob.status === 'error' ? `Błąd: ${activeJob.error}`
                  : activeJob.status}
              </span>
            </div>
            {activeJob.posts_total > 0 && (
              <div>
                <span className="text-gray-500 dark:text-gray-400">Artykułów: </span>
                <span className="font-medium text-gray-900 dark:text-white">{activeJob.posts_total}</span>
              </div>
            )}
            {activeJob.suggestions_total > 0 && (
              <div>
                <span className="text-gray-500 dark:text-gray-400">Propozycji: </span>
                <span className="font-medium text-gray-900 dark:text-white">{activeJob.suggestions_total}</span>
              </div>
            )}
            {activeJob.applied > 0 && (
              <div>
                <span className="text-gray-500 dark:text-gray-400">Wstawionych: </span>
                <span className="font-medium text-green-600 dark:text-green-400">{activeJob.applied}</span>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Suggestions panel */}
      {activeJob?.status === 'done' && (
        <>
          {/* Filter tabs + actions */}
          <div className="flex flex-wrap items-center justify-between gap-3 mb-4">
            <div className="flex gap-1">
              {['pending', 'approved', 'applied', 'rejected', 'all'].map(f => (
                <button
                  key={f}
                  onClick={() => { setStatusFilter(f); loadSuggestions(activeJob.id, f) }}
                  className={`px-3 py-1.5 rounded-lg text-xs font-medium transition-colors ${
                    statusFilter === f
                      ? 'bg-blue-600 text-white'
                      : 'bg-gray-100 dark:bg-gray-700 text-gray-600 dark:text-gray-300 hover:bg-gray-200 dark:hover:bg-gray-600'
                  }`}
                >
                  {statusLabel[f]}
                </button>
              ))}
            </div>
            <div className="flex gap-2">
              {statusFilter === 'pending' && selected.size > 0 && (
                <>
                  <button
                    onClick={handleApproveSelected}
                    className="px-3 py-1.5 bg-green-600 text-white rounded-lg text-xs font-medium hover:bg-green-700 transition-colors"
                  >
                    Zatwierdź ({selected.size})
                  </button>
                  <button
                    onClick={handleRejectSelected}
                    className="px-3 py-1.5 bg-red-500 text-white rounded-lg text-xs font-medium hover:bg-red-600 transition-colors"
                  >
                    Odrzuć ({selected.size})
                  </button>
                </>
              )}
              {statusFilter === 'approved' && (
                <button
                  onClick={handleApplyApproved}
                  disabled={applying}
                  className="px-4 py-1.5 bg-purple-600 text-white rounded-lg text-xs font-medium hover:bg-purple-700 disabled:opacity-50 transition-colors flex items-center gap-1.5"
                >
                  {applying ? (
                    <>
                      <svg className="w-3 h-3 animate-spin" fill="none" viewBox="0 0 24 24">
                        <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                        <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
                      </svg>
                      Wstawiam...
                    </>
                  ) : 'Wstaw linki do WP'}
                </button>
              )}
            </div>
          </div>

          {loading ? (
            <div className="flex justify-center py-12">
              <svg className="w-8 h-8 animate-spin text-blue-600" fill="none" viewBox="0 0 24 24">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v8H4z" />
              </svg>
            </div>
          ) : suggestions.length === 0 ? (
            <div className="text-center py-12 text-gray-500 dark:text-gray-400">
              Brak propozycji w kategorii "{statusLabel[statusFilter]}"
            </div>
          ) : (
            <>
              {/* Select all bar */}
              {statusFilter === 'pending' && (
                <div className="flex items-center gap-3 mb-3 px-1">
                  <input
                    type="checkbox"
                    checked={selected.size === suggestions.length && suggestions.length > 0}
                    onChange={toggleAll}
                    className="rounded"
                  />
                  <span className="text-xs text-gray-500 dark:text-gray-400">
                    Zaznacz wszystkie ({suggestions.length})
                  </span>
                </div>
              )}

              {/* Grouped by source article */}
              <div className="space-y-4">
                {Object.values(grouped).map(group => (
                  <div key={group.url}
                    className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 overflow-hidden">
                    {/* Article header */}
                    <div className="px-5 py-3 bg-gray-50 dark:bg-gray-750 border-b border-gray-200 dark:border-gray-700 flex items-center justify-between">
                      <div>
                        <span className="text-sm font-semibold text-gray-900 dark:text-white">{group.title}</span>
                        <a href={group.url} target="_blank" rel="noopener noreferrer"
                          className="ml-2 text-xs text-blue-500 hover:underline">{group.url}</a>
                      </div>
                      <span className="text-xs text-gray-400">{group.items.length} propozycji</span>
                    </div>

                    {/* Suggestions */}
                    <div className="divide-y divide-gray-100 dark:divide-gray-700">
                      {group.items.map(s => (
                        <div key={s.id} className={`px-5 py-4 flex gap-4 items-start ${
                          selected.has(s.id) ? 'bg-blue-50 dark:bg-blue-900/20' : ''
                        }`}>
                          {statusFilter === 'pending' && (
                            <input
                              type="checkbox"
                              checked={selected.has(s.id)}
                              onChange={() => toggleSelect(s.id)}
                              className="mt-1 rounded flex-shrink-0"
                            />
                          )}
                          <div className="flex-1 min-w-0">
                            {/* Context preview */}
                            <div className="text-sm text-gray-600 dark:text-gray-300 mb-2 leading-relaxed">
                              {s.context_before && (
                                <span className="text-gray-400 dark:text-gray-500">...{s.context_before} </span>
                              )}
                              <span className="bg-yellow-100 dark:bg-yellow-900/40 text-yellow-800 dark:text-yellow-300 font-medium px-1 rounded">
                                {s.anchor_text}
                              </span>
                              {s.context_after && (
                                <span className="text-gray-400 dark:text-gray-500"> {s.context_after}...</span>
                              )}
                            </div>
                            {/* Arrow → target */}
                            <div className="flex items-center gap-2 text-xs">
                              <svg className="w-4 h-4 text-gray-400 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5m0 0l-5 5m5-5H6" />
                              </svg>
                              <span className="text-gray-500 dark:text-gray-400">Link do:</span>
                              <a href={s.target_url} target="_blank" rel="noopener noreferrer"
                                className="text-blue-600 dark:text-blue-400 hover:underline font-medium truncate max-w-xs">
                                {s.target_title}
                              </a>
                            </div>
                          </div>
                          {/* Status badge */}
                          <div className="flex-shrink-0">
                            {s.status === 'applied' && (
                              <span className="inline-flex items-center gap-1 text-xs bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400 px-2 py-0.5 rounded-full">
                                <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 20 20"><path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" /></svg>
                                Wstawiono
                              </span>
                            )}
                            {s.status === 'approved' && (
                              <span className="text-xs bg-blue-100 dark:bg-blue-900/30 text-blue-700 dark:text-blue-400 px-2 py-0.5 rounded-full">
                                Zatwierdzone
                              </span>
                            )}
                            {s.status === 'rejected' && (
                              <span className="text-xs bg-gray-100 dark:bg-gray-700 text-gray-500 dark:text-gray-400 px-2 py-0.5 rounded-full">
                                Odrzucone
                              </span>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      )}
    </div>
  )
}
