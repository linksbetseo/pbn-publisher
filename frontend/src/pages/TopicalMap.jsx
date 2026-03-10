import { useState } from 'react'
import api from '../api/client'

export default function TopicalMap() {
  const [seed, setSeed] = useState('')
  const [minVolume, setMinVolume] = useState(50)
  const [maxClusters, setMaxClusters] = useState(8)
  const [language, setLanguage] = useState('pl')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [expandedPillar, setExpandedPillar] = useState(null)

  const generate = async () => {
    if (!seed.trim()) return
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const resp = await api.post('/api/topical-map', {
        seed: seed.trim(),
        location_code: language === 'pl' ? 2616 : 2840,
        language_code: language,
        min_volume: minVolume,
        max_clusters: maxClusters,
      })
      setResult(resp.data)
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

  const totalKeywords = result?.pillars?.reduce((s, p) => s + p.supporting_keywords.length + 1, 0) || 0

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
            <label className="block text-sm font-medium text-gray-700 mb-1">Liczba klastrów</label>
            <input
              type="number"
              value={maxClusters}
              min={3}
              max={15}
              onChange={e => setMaxClusters(Number(e.target.value))}
              className="w-24 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
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

      {error && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-red-700 text-sm mb-6">{error}</div>
      )}

      {result && (
        <>
          {/* Summary */}
          <div className="flex gap-4 mb-6 flex-wrap">
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
          </div>

          {/* Pillars */}
          <div className="space-y-4">
            {result.pillars?.map((pillar, i) => (
              <div key={i} className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
                {/* Pillar header */}
                <div
                  className="flex items-center justify-between p-4 cursor-pointer hover:bg-gray-50"
                  onClick={() => setExpandedPillar(expandedPillar === i ? null : i)}
                >
                  <div className="flex items-center gap-3">
                    <span className="w-8 h-8 rounded-full bg-blue-100 text-blue-700 flex items-center justify-center text-sm font-bold">{i + 1}</span>
                    <div>
                      <div className="flex items-center gap-2">
                        <span className="font-semibold text-gray-900">{pillar.label}</span>
                        <span className="text-xs px-2 py-0.5 bg-blue-100 text-blue-700 rounded-full font-medium">PILLAR PAGE</span>
                      </div>
                      <div className="text-sm text-gray-500 mt-0.5">
                        Fraza główna: <span className="font-medium text-gray-700">{pillar.pillar_keyword}</span>
                        {' · '}vol. <span className="font-medium">{(pillar.pillar_volume || 0).toLocaleString()}</span>
                        {' · '}KD: <span className={`font-medium ${pillar.pillar_difficulty > 60 ? 'text-red-600' : pillar.pillar_difficulty > 30 ? 'text-yellow-600' : 'text-green-600'}`}>{Math.round(pillar.pillar_difficulty || 0)}</span>
                      </div>
                    </div>
                  </div>
                  <div className="flex items-center gap-4 text-sm text-gray-500">
                    <span className="hidden sm:block">{pillar.supporting_keywords?.length || 0} supporting pages</span>
                    <span className="hidden sm:block">∑ vol. {(pillar.total_volume || 0).toLocaleString()}</span>
                    <svg className={`w-5 h-5 transition-transform ${expandedPillar === i ? 'rotate-180' : ''}`} fill="none" stroke="currentColor" viewBox="0 0 24 24">
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </div>
                </div>

                {/* Supporting pages */}
                {expandedPillar === i && (
                  <div className="border-t border-gray-100 p-4">
                    <div className="flex items-center justify-between mb-3">
                      <h4 className="text-sm font-semibold text-gray-700">Supporting Pages ({pillar.supporting_keywords?.length || 0})</h4>
                      <button
                        onClick={() => copyKeywords(pillar.supporting_keywords)}
                        className="text-xs text-blue-600 hover:underline"
                      >
                        Kopiuj frazy
                      </button>
                    </div>
                    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-2">
                      {pillar.supporting_keywords?.map((sk, j) => (
                        <div key={j} className="flex items-center justify-between px-3 py-2 bg-gray-50 rounded-lg text-sm">
                          <span className="text-gray-700 truncate mr-2">{sk.keyword}</span>
                          <div className="flex items-center gap-2 shrink-0 text-xs text-gray-500">
                            <span>{(sk.search_volume || 0).toLocaleString()}</span>
                            <span className={`px-1.5 py-0.5 rounded ${sk.keyword_difficulty > 60 ? 'bg-red-100 text-red-600' : sk.keyword_difficulty > 30 ? 'bg-yellow-100 text-yellow-600' : 'bg-green-100 text-green-600'}`}>
                              {Math.round(sk.keyword_difficulty || 0)}
                            </span>
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
