import { useState, useEffect } from 'react'
import { domains as domainsApi } from '../api/client'

export default function DomainSelector({ selected, onChange, clientId }) {
  const [domainList, setDomainList] = useState([])
  const [servers, setServers] = useState([])
  const [serverFilter, setServerFilter] = useState('all')
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [usedIds, setUsedIds] = useState([])
  const [hideUsed, setHideUsed] = useState(true)
  const [showOnlyOk, setShowOnlyOk] = useState(false)

  useEffect(() => {
    const fetches = [domainsApi.list({ active: 1 }), domainsApi.servers()]
    if (clientId) fetches.push(domainsApi.used(clientId))
    Promise.all(fetches)
      .then(([domsRes, srvRes, usedRes]) => {
        setDomainList(domsRes.data)
        setServers(srvRes.data)
        if (usedRes) setUsedIds(usedRes.data)
      })
      .finally(() => setLoading(false))
  }, [clientId])

  const filtered = domainList.filter((d) => {
    const matchServer = serverFilter === 'all' || d.server === serverFilter
    const matchSearch = !search || d.domain.toLowerCase().includes(search.toLowerCase())
    const matchUsed = !hideUsed || !usedIds.includes(d.id)
    const matchOk = !showOnlyOk || d.wp_ok === 1
    return matchServer && matchSearch && matchUsed && matchOk
  })

  const selectAll = () => onChange(filtered.map((d) => d.id))
  const deselectAll = () => onChange([])
  const toggleDomain = (id) => {
    if (selected.includes(id)) {
      onChange(selected.filter((s) => s !== id))
    } else {
      onChange([...selected, id])
    }
  }

  const serverColors = {
    h20: 'bg-blue-100 text-blue-700',
    h21: 'bg-green-100 text-green-700',
    h24: 'bg-purple-100 text-purple-700',
    h27: 'bg-yellow-100 text-yellow-700',
    ovh: 'bg-red-100 text-red-700',
  }
  const getBadgeColor = (s) => serverColors[s?.toLowerCase()] || 'bg-gray-100 text-gray-600'

  if (loading) {
    return (
      <div className="flex items-center justify-center h-40 text-gray-500">
        Ładowanie domen...
      </div>
    )
  }

  return (
    <div>
      <div className="flex flex-wrap gap-2 mb-3">
        <select
          value={serverFilter}
          onChange={(e) => setServerFilter(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="all">Wszystkie serwery</option>
          {servers.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
        <input
          type="text"
          placeholder="Szukaj domeny..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm flex-1 min-w-[150px] focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <button
          onClick={selectAll}
          className="px-3 py-2 text-sm bg-blue-600 text-white rounded-lg hover:bg-blue-700"
        >
          Zaznacz wszystkie
        </button>
        <button
          onClick={deselectAll}
          className="px-3 py-2 text-sm bg-gray-200 text-gray-700 rounded-lg hover:bg-gray-300"
        >
          Odznacz
        </button>
      </div>

      {/* Filters row */}
      <div className="flex gap-4 mb-2 text-sm">
        <label className="flex items-center gap-1.5 cursor-pointer text-gray-600">
          <input
            type="checkbox"
            checked={hideUsed}
            onChange={(e) => setHideUsed(e.target.checked)}
            className="rounded"
          />
          Ukryj już użyte ({usedIds.length})
        </label>
        <label className="flex items-center gap-1.5 cursor-pointer text-gray-600">
          <input
            type="checkbox"
            checked={showOnlyOk}
            onChange={(e) => setShowOnlyOk(e.target.checked)}
            className="rounded"
          />
          Tylko działające WP
        </label>
      </div>

      <div className="text-sm text-gray-500 mb-2">
        Zaznaczono: <span className="font-semibold text-blue-600">{selected.length}</span> z {filtered.length} widocznych
        <span className="ml-2 text-gray-400">({domainList.filter(d => d.wp_ok === 1).length} działa / {domainList.length} łącznie)</span>
      </div>

      <div className="border border-gray-200 rounded-lg overflow-y-auto max-h-72">
        {filtered.length === 0 ? (
          <div className="p-4 text-center text-gray-400 text-sm">Brak domen</div>
        ) : (
          <div className="divide-y divide-gray-100">
            {filtered.map((d) => {
              const isUsed = usedIds.includes(d.id)
              const wpOk = d.wp_ok === 1
              return (
                <label
                  key={d.id}
                  className={`flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-gray-50 ${
                    selected.includes(d.id) ? 'bg-blue-50' : ''
                  } ${isUsed ? 'opacity-60' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={selected.includes(d.id)}
                    onChange={() => toggleDomain(d.id)}
                    className="w-4 h-4 text-blue-600 rounded"
                  />
                  <span className="flex-1 text-sm font-medium text-gray-800">{d.domain}</span>
                  {isUsed && (
                    <span className="text-xs text-orange-500 font-medium">użyta</span>
                  )}
                  <span className={`text-xs font-semibold ${wpOk ? 'text-green-600' : 'text-red-400'}`}>
                    {wpOk ? '✓ WP' : '✗ WP'}
                  </span>
                  <span className={`text-xs px-2 py-0.5 rounded-full font-medium ${getBadgeColor(d.server)}`}>
                    {d.server || '—'}
                  </span>
                </label>
              )
            })}
          </div>
        )}
      </div>
    </div>
  )
}
