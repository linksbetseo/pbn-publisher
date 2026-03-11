import { useState, useEffect } from 'react'
import { bulkPublish as bulkPublishApi, projects as projectsApi } from '../api/client'
import api from '../api/client'

const INDEXER_URL =
  'https://rocketindexer.com/api/index.php?token=544c8414cbd899cabf4816fd0800438c583b42377fff7faca8958ebbc54a88aa&endpoint=submit'

const IMAGE_SOURCES = [
  { value: 'none', label: 'Brak obrazka' },
  { value: 'freepik_stock', label: '📷 Freepik Stock' },
  { value: 'freepik_zimage', label: '⚡ Freepik Z-Image (generowanie)' },
  { value: 'freepik_flux', label: '🌊 Freepik Flux Pro 1.1 (generowanie)' },
  { value: 'dalle', label: '🎨 DALL-E 3' },
]

function TabImport({ projects }) {
  const [lines, setLines] = useState('')
  const [batchTag, setBatchTag] = useState('')
  const [projectId, setProjectId] = useState('')
  const [server, setServer] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)

  const handleImport = async () => {
    if (!lines.trim() || !batchTag.trim()) return
    setLoading(true)
    setResult(null)
    try {
      const resp = await bulkPublishApi.importDomains({
        lines,
        batch_tag: batchTag,
        project_id: projectId ? parseInt(projectId) : null,
        server,
      })
      setResult(resp.data)
    } catch (e) {
      setResult({ error: e.message })
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="space-y-5 max-w-2xl">
      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">
          Domeny (format: <code className="bg-gray-100 px-1 rounded">domena;login;haslo</code> lub <code className="bg-gray-100 px-1 rounded">domena;login;haslo;serwer</code>)
        </label>
        <textarea
          value={lines}
          onChange={(e) => setLines(e.target.value)}
          rows={10}
          placeholder={"example.com;admin;aplikacjahaslo\nblog2.pl;user;pass123;h20"}
          className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
        />
        <p className="text-xs text-gray-400 mt-1">Domeny już istniejące w bazie zostaną pominięte.</p>
      </div>

      <div className="grid grid-cols-2 gap-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Nazwa batcha *</label>
          <input
            type="text"
            value={batchTag}
            onChange={(e) => setBatchTag(e.target.value)}
            placeholder="np. Marzec 2026"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Domyślny serwer (opcjonalnie)</label>
          <input
            type="text"
            value={server}
            onChange={(e) => setServer(e.target.value)}
            placeholder="np. h20"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>
      </div>

      <div>
        <label className="block text-sm font-medium text-gray-700 mb-1">Projekt (opcjonalnie)</label>
        <select
          value={projectId}
          onChange={(e) => setProjectId(e.target.value)}
          className="border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          <option value="">— bez projektu —</option>
          {projects.map((p) => (
            <option key={p.id} value={p.id}>{p.name}</option>
          ))}
        </select>
      </div>

      <button
        onClick={handleImport}
        disabled={loading || !lines.trim() || !batchTag.trim()}
        className="px-6 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50"
      >
        {loading ? 'Importuję...' : 'Importuj domeny'}
      </button>

      {result && !result.error && (
        <div className="p-4 bg-green-50 border border-green-200 rounded-lg text-sm">
          <p className="font-semibold text-green-700">Import zakończony</p>
          <p className="text-green-600 mt-1">
            Dodano: <strong>{result.inserted}</strong> | Pominięto (duplikaty): <strong>{result.skipped}</strong>
          </p>
          {result.errors && result.errors.length > 0 && (
            <ul className="mt-2 text-red-600 text-xs list-disc ml-4">
              {result.errors.map((e, i) => <li key={i}>{e}</li>)}
            </ul>
          )}
        </div>
      )}
      {result?.error && (
        <div className="p-4 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
          Błąd: {result.error}
        </div>
      )}
    </div>
  )
}

function TabPublish() {
  const [batches, setBatches] = useState([])
  const [selectedBatch, setSelectedBatch] = useState('')
  const [batchDomains, setBatchDomains] = useState([])
  const [selectedIds, setSelectedIds] = useState([])
  const [keyword, setKeyword] = useState('')
  const [anchor, setAnchor] = useState('')
  const [clientDomain, setClientDomain] = useState('')
  const [anchor2, setAnchor2] = useState('')
  const [url2, setUrl2] = useState('')
  const [anchor3, setAnchor3] = useState('')
  const [url3, setUrl3] = useState('')
  const [language, setLanguage] = useState('pl')
  const [imageSource, setImageSource] = useState('none')
  const [publishing, setPublishing] = useState(false)
  const [results, setResults] = useState([])
  const [progress, setProgress] = useState({ done: 0, total: 0 })
  const [indexing, setIndexing] = useState({})
  const [indexResults, setIndexResults] = useState({})

  useEffect(() => {
    bulkPublishApi.listBatches().then((r) => setBatches(r.data)).catch(() => {})
  }, [])

  useEffect(() => {
    if (!selectedBatch) {
      setBatchDomains([])
      setSelectedIds([])
      return
    }
    bulkPublishApi.listByBatch(selectedBatch).then((r) => {
      setBatchDomains(r.data)
      setSelectedIds(r.data.map((d) => d.id))
    }).catch(() => {})
  }, [selectedBatch])

  const toggleDomain = (id) => {
    setSelectedIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    )
  }
  const selectAll = () => setSelectedIds(batchDomains.map((d) => d.id))
  const deselectAll = () => setSelectedIds([])

  const handlePublish = async () => {
    if (!selectedIds.length || !keyword.trim()) return
    setPublishing(true)
    setResults([])
    setProgress({ done: 0, total: selectedIds.length })

    try {
      const resp = await api.post('/api/publish/post', {
        title: keyword,
        content: '',
        my_domain_ids: selectedIds,
        client_domain: clientDomain,
        topic: keyword,
        anchor_text: anchor || keyword,
        anchor_text2: anchor2,
        anchor_url2: url2,
        anchor_text3: anchor3,
        anchor_url3: url3,
        language,
        unique_per_domain: true,
        batch_tag: selectedBatch,
        image_source: imageSource,
      })
      const res = resp.data
      setResults(res.results || [])
      setProgress({ done: selectedIds.length, total: selectedIds.length })
    } catch (e) {
      setResults([{ domain: '—', status: 'failed', error: e.message, url: '' }])
    } finally {
      setPublishing(false)
    }
  }

  const sendToIndexer = async (url, key) => {
    setIndexing((prev) => ({ ...prev, [key]: true }))
    setIndexResults((prev) => ({ ...prev, [key]: null }))
    try {
      const resp = await fetch(INDEXER_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ urls: [url] }),
      })
      const data = await resp.json()
      if (data.success) {
        setIndexResults((prev) => ({ ...prev, [key]: { ok: true, msg: 'Wysłano!' } }))
      } else {
        setIndexResults((prev) => ({ ...prev, [key]: { ok: false, msg: data.message || 'Błąd API' } }))
      }
    } catch {
      setIndexResults((prev) => ({ ...prev, [key]: { ok: false, msg: 'Błąd połączenia' } }))
    } finally {
      setIndexing((prev) => ({ ...prev, [key]: false }))
    }
  }

  return (
    <div className="space-y-5">
      {/* Batch selector */}
      <div className="grid grid-cols-2 gap-4 max-w-2xl">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Batch</label>
          <select
            value={selectedBatch}
            onChange={(e) => setSelectedBatch(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="">— wybierz batch —</option>
            {batches.map((b) => (
              <option key={b.batch_tag} value={b.batch_tag}>
                {b.batch_tag} ({b.count} domen{b.project_name ? ` · ${b.project_name}` : ''})
              </option>
            ))}
          </select>
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">Język</label>
          <select
            value={language}
            onChange={(e) => setLanguage(e.target.value)}
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          >
            <option value="pl">Polski</option>
            <option value="en">English</option>
          </select>
        </div>
      </div>

      {/* Domain list */}
      {batchDomains.length > 0 && (
        <div>
          <div className="flex items-center gap-3 mb-2">
            <span className="text-sm font-medium text-gray-700">
              Domeny ({selectedIds.length}/{batchDomains.length} zaznaczonych)
            </span>
            <button onClick={selectAll} className="text-xs text-blue-600 hover:underline">Zaznacz wszystkie</button>
            <button onClick={deselectAll} className="text-xs text-gray-500 hover:underline">Odznacz</button>
          </div>
          <div className="border border-gray-200 rounded-lg overflow-y-auto max-h-48">
            <div className="divide-y divide-gray-100">
              {batchDomains.map((d) => (
                <label
                  key={d.id}
                  className={`flex items-center gap-3 px-4 py-2 cursor-pointer hover:bg-gray-50 text-sm ${selectedIds.includes(d.id) ? 'bg-blue-50' : ''}`}
                >
                  <input
                    type="checkbox"
                    checked={selectedIds.includes(d.id)}
                    onChange={() => toggleDomain(d.id)}
                    className="w-4 h-4 text-blue-600 rounded"
                  />
                  <span className="flex-1 font-medium text-gray-800">{d.domain}</span>
                  <span className={`text-xs font-semibold ${d.wp_ok === 1 ? 'text-green-600' : 'text-gray-400'}`}>
                    {d.wp_ok === 1 ? '✓ WP' : '✗ WP'}
                  </span>
                  {d.server && (
                    <span className="text-xs px-2 py-0.5 rounded-full bg-gray-100 text-gray-600">{d.server}</span>
                  )}
                </label>
              ))}
            </div>
          </div>
        </div>
      )}

      {/* Publish settings */}
      <div className="space-y-4 max-w-2xl">
        {/* Row 1: keyword + language */}
        <div className="grid grid-cols-2 gap-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Keyword / Temat *</label>
            <input
              type="text"
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="np. hydraulik warszawa"
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Źródło obrazka</label>
            <select
              value={imageSource}
              onChange={(e) => setImageSource(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            >
              {IMAGE_SOURCES.map((s) => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
          </div>
        </div>

        {/* Links section */}
        <div className="border border-gray-200 rounded-lg p-4 space-y-3 bg-gray-50">
          <p className="text-xs font-semibold text-gray-500 uppercase tracking-wide">Linki w artykule (max 3)</p>

          {/* Link 1 */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">URL 1 (domena klienta)</label>
              <input
                type="text"
                value={clientDomain}
                onChange={(e) => setClientDomain(e.target.value)}
                placeholder="https://firma.pl"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Anchor 1</label>
              <input
                type="text"
                value={anchor}
                onChange={(e) => setAnchor(e.target.value)}
                placeholder="(domyślnie = keyword)"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              />
            </div>
          </div>

          {/* Link 2 */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">URL 2 (opcjonalny)</label>
              <input
                type="text"
                value={url2}
                onChange={(e) => setUrl2(e.target.value)}
                placeholder="https://drugi-link.pl"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Anchor 2</label>
              <input
                type="text"
                value={anchor2}
                onChange={(e) => setAnchor2(e.target.value)}
                placeholder="tekst anchora"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              />
            </div>
          </div>

          {/* Link 3 */}
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">URL 3 (opcjonalny)</label>
              <input
                type="text"
                value={url3}
                onChange={(e) => setUrl3(e.target.value)}
                placeholder="https://trzeci-link.pl"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-gray-600 mb-1">Anchor 3</label>
              <input
                type="text"
                value={anchor3}
                onChange={(e) => setAnchor3(e.target.value)}
                placeholder="tekst anchora"
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 bg-white"
              />
            </div>
          </div>
        </div>
      </div>

      <button
        onClick={handlePublish}
        disabled={publishing || !selectedIds.length || !keyword.trim()}
        className="px-6 py-2.5 bg-green-600 text-white rounded-lg text-sm font-medium hover:bg-green-700 disabled:opacity-50 flex items-center gap-2"
      >
        {publishing ? (
          <>
            <span className="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full" />
            Publikuję... ({progress.done}/{progress.total})
          </>
        ) : (
          `Publikuj na wybranych (${selectedIds.length})`
        )}
      </button>

      {/* Results table */}
      {results.length > 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm overflow-hidden">
          <div className="px-4 py-3 border-b border-gray-100 flex items-center gap-4 text-sm">
            <span className="font-semibold text-gray-700">Wyniki publikacji</span>
            <span className="text-green-600 font-medium">
              OK: {results.filter((r) => r.status === 'published').length}
            </span>
            <span className="text-red-500 font-medium">
              Błędy: {results.filter((r) => r.status === 'failed').length}
            </span>
          </div>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="bg-gray-50 border-b border-gray-100">
                  <th className="text-left px-4 py-2 font-semibold text-gray-600">Domena</th>
                  <th className="text-left px-4 py-2 font-semibold text-gray-600">Status</th>
                  <th className="text-left px-4 py-2 font-semibold text-gray-600">URL</th>
                  <th className="px-4 py-2"></th>
                </tr>
              </thead>
              <tbody className="divide-y divide-gray-100">
                {results.map((r, i) => (
                  <tr key={i} className="hover:bg-gray-50">
                    <td className="px-4 py-2 font-medium text-gray-800">{r.domain}</td>
                    <td className="px-4 py-2">
                      <span
                        className={`inline-block px-2 py-0.5 rounded-full text-xs font-medium ${
                          r.status === 'published'
                            ? 'bg-green-100 text-green-700'
                            : 'bg-red-100 text-red-700'
                        }`}
                      >
                        {r.status === 'published' ? 'Opublikowano' : 'Błąd'}
                      </span>
                      {r.error && (
                        <span className="text-xs text-red-500 ml-2" title={r.error}>
                          {r.error.slice(0, 60)}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {r.url ? (
                        <a
                          href={r.url}
                          target="_blank"
                          rel="noreferrer"
                          className="text-blue-600 hover:underline text-xs"
                        >
                          {r.url.slice(0, 60)}…
                        </a>
                      ) : (
                        <span className="text-gray-300 text-xs">—</span>
                      )}
                    </td>
                    <td className="px-4 py-2">
                      {r.url && (
                        <button
                          onClick={() => sendToIndexer(r.url, `${i}-${r.domain}`)}
                          disabled={indexing[`${i}-${r.domain}`]}
                          className="px-3 py-1 bg-orange-500 text-white rounded text-xs hover:bg-orange-600 disabled:opacity-40"
                        >
                          {indexing[`${i}-${r.domain}`]
                            ? '...'
                            : indexResults[`${i}-${r.domain}`]?.ok
                            ? 'Wysłano!'
                            : 'Indexer'}
                        </button>
                      )}
                    </td>
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

export default function BulkPublish() {
  const [tab, setTab] = useState('import')
  const [projects, setProjects] = useState([])

  useEffect(() => {
    projectsApi.list().then((r) => setProjects(r.data)).catch(() => {})
  }, [])

  return (
    <div className="p-8">
      <h2 className="text-2xl font-bold text-gray-900 mb-1">Publikuj Ręcznie</h2>
      <p className="text-gray-500 mb-6">Importuj domeny w paczkach i publikuj artykuły hurtowo</p>

      {/* Tabs */}
      <div className="flex gap-1 mb-6 border-b border-gray-200">
        {[
          { key: 'import', label: 'Importuj domeny' },
          { key: 'publish', label: 'Publikuj' },
        ].map((t) => (
          <button
            key={t.key}
            onClick={() => setTab(t.key)}
            className={`px-5 py-2.5 text-sm font-medium rounded-t-lg transition-colors ${
              tab === t.key
                ? 'bg-white border border-b-white border-gray-200 text-blue-600 -mb-px'
                : 'text-gray-500 hover:text-gray-700'
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'import' && <TabImport projects={projects} />}
      {tab === 'publish' && <TabPublish />}
    </div>
  )
}
