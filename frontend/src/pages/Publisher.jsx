import { useState, useEffect, useRef } from 'react'
import { projects as projectsApi, clients as clientsApi, publish as publishApi } from '../api/client'
import { useToast } from '../components/Toast'
import DomainSelector from '../components/DomainSelector'
import { sanitizeHtml } from '../sanitize'

const STEPS = ['Konfiguracja', 'Wybór domen', 'Generowanie', 'Publikacja']

export default function Publisher() {
  const [step, setStep] = useState(0)
  const [projectList, setProjectList] = useState([])
  const [clientList, setClientList] = useState([])
  const [selectedProject, setSelectedProject] = useState('')
  const [selectedClient, setSelectedClient] = useState('')
  const [selectedClientDomain, setSelectedClientDomain] = useState('')
  const [topic, setTopic] = useState('')
  const [anchorText, setAnchorText] = useState('')
  const [anchorText2, setAnchorText2] = useState('')
  const [anchorUrl2, setAnchorUrl2] = useState('')
  const [anchorText3, setAnchorText3] = useState('')
  const [anchorUrl3, setAnchorUrl3] = useState('')
  const [customPrompt, setCustomPrompt] = useState('')
  const [useCustomPrompt, setUseCustomPrompt] = useState(false)
  const [language, setLanguage] = useState('pl')
  const [toneOfVoice, setToneOfVoice] = useState('ekspert')
  const [useSerpScrape, setUseSerpScrape] = useState(true)
  const [selectedDomainIds, setSelectedDomainIds] = useState([])
  const [generating, setGenerating] = useState(false)
  const [genProgress, setGenProgress] = useState(null)
  const [generated, setGenerated] = useState(null)
  const [editTitle, setEditTitle] = useState('')
  const [editContent, setEditContent] = useState('')
  const [editingContent, setEditingContent] = useState(false)
  const [regeneratingImage, setRegeneratingImage] = useState(false)
  const [staticImageB64, setStaticImageB64] = useState(null)
  const [publishResults, setPublishResults] = useState([])
  const [publishing, setPublishing] = useState(false)
  const [publishDone, setPublishDone] = useState(false)
  const [error, setError] = useState('')
  const [pingSitemap, setPingSitemap] = useState({})
  const [dripDelay, setDripDelay] = useState(true)
  const addToast = useToast()
  const fileInputRef = useRef(null)

  useEffect(() => {
    projectsApi.list().then(r => setProjectList(r.data))
  }, [])

  const handleProjectChange = async (pid) => {
    setSelectedProject(pid)
    setSelectedClient('')
    setSelectedClientDomain('')
    if (pid) {
      const res = await clientsApi.list(parseInt(pid))
      setClientList(res.data)
    } else {
      setClientList([])
    }
  }

  const currentClient = clientList.find(c => c.id === parseInt(selectedClient))
  const clientDomains = currentClient?.domains || []

  const canProceedStep0 = selectedProject && selectedClient && selectedClientDomain && topic.trim() && anchorText.trim()
  const canProceedStep1 = selectedDomainIds.length > 0

  const handleGenerate = async () => {
    setGenerating(true)
    setError('')
    const steps = [
      { label: 'Analiza SERP top10...', pct: 15 },
      { label: 'Generowanie artykulu (GPT)...', pct: 50 },
      { label: 'Generowanie obrazka...', pct: 80 },
      { label: 'Finalizacja...', pct: 95 },
    ]
    let stepIdx = 0
    setGenProgress({ label: steps[0].label, pct: steps[0].pct })
    const progressTimer = setInterval(() => {
      stepIdx++
      if (stepIdx < steps.length) {
        setGenProgress({ label: steps[stepIdx].label, pct: steps[stepIdx].pct })
      }
    }, useSerpScrape ? 12000 : 5000)
    try {
      const res = await publishApi.generate({
        topic,
        client_domain: selectedClientDomain,
        anchor_text: anchorText,
        language,
        anchor_text2: anchorText2,
        anchor_url2: anchorUrl2,
        anchor_text3: anchorText3,
        anchor_url3: anchorUrl3,
        custom_prompt: useCustomPrompt ? customPrompt : '',
        tone_of_voice: toneOfVoice,
        use_serp_scrape: useSerpScrape,
      })
      setGenerated(res.data)
      setEditTitle(res.data.title)
      setEditContent(res.data.content)
      setStep(3)
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Błąd generowania')
    } finally {
      clearInterval(progressTimer)
      setGenProgress(null)
      setGenerating(false)
    }
  }

  const handleRegenerateImage = async () => {
    setRegeneratingImage(true)
    try {
      const apiBase = import.meta.env.DEV ? 'http://localhost:8001' : ''
      const authToken = localStorage.getItem('pbn_auth_token')
      const imgHeaders = { 'Content-Type': 'application/json' }
      if (authToken) imgHeaders['Authorization'] = `Basic ${authToken}`
      const res = await fetch(`${apiBase}/api/publish/regenerate-image`, {
        method: 'POST',
        headers: imgHeaders,
        body: JSON.stringify({ topic, static_image_b64: staticImageB64 || null }),
      })
      const data = await res.json()
      if (data.image_b64) {
        setGenerated(prev => ({ ...prev, image_b64: data.image_b64 }))
        setStaticImageB64(null)
      }
    } catch (e) {
      setError('Błąd regeneracji obrazka')
    } finally {
      setRegeneratingImage(false)
    }
  }

  const handleStaticImageUpload = (e) => {
    const file = e.target.files[0]
    if (!file) return
    const reader = new FileReader()
    reader.onload = (ev) => {
      const b64 = ev.target.result.split(',')[1]
      setStaticImageB64(b64)
      setGenerated(prev => ({ ...prev, image_b64: b64 }))
    }
    reader.readAsDataURL(file)
  }

  const handlePublish = async () => {
    if (!generated) return
    setPublishing(true)
    setPublishResults([])
    setPublishDone(false)
    setError('')

    const body = {
      title: editTitle,
      content: editContent,
      image_b64: generated.image_b64,
      my_domain_ids: selectedDomainIds,
      client_id: parseInt(selectedClient),
      client_domain: selectedClientDomain,
      topic,
      anchor_text: anchorText,
      anchor_text2: anchorText2,
      anchor_url2: anchorUrl2,
      anchor_text3: anchorText3,
      anchor_url3: anchorUrl3,
      custom_prompt: useCustomPrompt ? customPrompt : '',
      language,
      unique_per_domain: true,
      drip_delay: dripDelay,
    }

    try {
      // Use async job + SSE to avoid browser timeout on large domain sets
      const startResp = await publishApi.postAsync(body)
      const { job_id, total } = startResp.data
      if (!job_id) throw new Error('Brak job_id z serwera')

      const BASE = import.meta.env.DEV ? 'http://localhost:8001' : ''
      const token = localStorage.getItem('pbn_auth_token')
      const sseUrl = `${BASE}/api/publish/post-progress/${job_id}` + (token ? `?_auth=${encodeURIComponent(token)}` : '')
      const evtSource = new EventSource(sseUrl)

      await new Promise((resolve) => {
        evtSource.onmessage = (e) => {
          try {
            const data = JSON.parse(e.data)
            if (data.latest) {
              setPublishResults(prev => [...prev, data.latest])
            }
            if (data.finished) {
              setPublishDone(true)
              evtSource.close()
              resolve()
            }
          } catch {}
        }
        evtSource.onerror = () => {
          evtSource.close()
          setPublishDone(true)
          resolve()
        }
      })
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Błąd publikacji')
    } finally {
      setPublishing(false)
    }
  }

  const resetAll = () => {
    setStep(0)
    setGenerated(null)
    setEditTitle('')
    setEditContent('')
    setEditingContent(false)
    setStaticImageB64(null)
    setPublishResults([])
    setPublishDone(false)
    setError('')
    setTopic('')
    setAnchorText('')
    setAnchorText2('')
    setAnchorUrl2('')
    setAnchorText3('')
    setAnchorUrl3('')
    setCustomPrompt('')
    setSelectedDomainIds([])
  }

  return (
    <div className="p-8 max-w-5xl">
      <h2 className="text-2xl font-bold text-gray-900 mb-2">Publikuj artykuł</h2>
      <p className="text-gray-500 mb-6">Wygeneruj i opublikuj SEO artykuł na domenach PBN</p>

      {/* Step indicator */}
      <div className="flex items-center mb-8">
        {STEPS.map((s, i) => (
          <div key={i} className="flex items-center">
            <div className={`flex items-center justify-center w-8 h-8 rounded-full text-sm font-bold ${
              i <= step ? 'bg-blue-600 text-white' : 'bg-gray-200 text-gray-500'
            }`}>{i + 1}</div>
            <span className={`ml-2 text-sm font-medium ${i <= step ? 'text-blue-600' : 'text-gray-400'}`}>{s}</span>
            {i < STEPS.length - 1 && (
              <div className={`mx-3 h-0.5 w-12 ${i < step ? 'bg-blue-600' : 'bg-gray-200'}`} />
            )}
          </div>
        ))}
      </div>

      {error && (
        <div className="mb-4 p-4 bg-red-50 border border-red-200 rounded-lg text-red-700 text-sm">{error}</div>
      )}

      {/* Step 0: Config */}
      {step === 0 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6 space-y-5">
          <h3 className="font-semibold text-gray-800 text-lg">Krok 1 — Konfiguracja</h3>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Projekt</label>
              <select
                value={selectedProject}
                onChange={(e) => handleProjectChange(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="">— wybierz projekt —</option>
                {projectList.map(p => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Klient</label>
              <select
                value={selectedClient}
                onChange={(e) => { setSelectedClient(e.target.value); setSelectedClientDomain('') }}
                disabled={!selectedProject}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
              >
                <option value="">— wybierz klienta —</option>
                {clientList.map(c => (
                  <option key={c.id} value={c.id}>{c.name}</option>
                ))}
              </select>
            </div>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Domena klienta (kotwica)</label>
            <select
              value={selectedClientDomain}
              onChange={(e) => setSelectedClientDomain(e.target.value)}
              disabled={!selectedClient}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:opacity-50"
            >
              <option value="">— wybierz domenę klienta —</option>
              {clientDomains.map(d => (
                <option key={d.id} value={d.domain}>{d.domain}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Temat artykułu</label>
            <input
              type="text"
              placeholder="np. Najlepsze okna dachowe do domu jednorodzinnego"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>

          {/* Anchors */}
          <div className="space-y-3">
            <p className="text-sm font-medium text-gray-700">Kotwice (anchor text)</p>

            {/* Anchor 1 — obowiązkowy */}
            <div className="flex gap-2 items-center">
              <span className="text-xs font-semibold text-blue-600 w-6">1*</span>
              <input
                type="text"
                placeholder="Tekst kotwicy (obowiązkowy)"
                value={anchorText}
                onChange={(e) => setAnchorText(e.target.value)}
                className="flex-1 border border-blue-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <span className="text-xs text-gray-400 w-16">→ {selectedClientDomain || 'domena klienta'}</span>
            </div>

            {/* Anchor 2 — opcjonalny */}
            <div className="flex gap-2 items-center">
              <span className="text-xs font-semibold text-gray-400 w-6">2</span>
              <input
                type="text"
                placeholder="Tekst kotwicy (opcjonalny)"
                value={anchorText2}
                onChange={(e) => setAnchorText2(e.target.value)}
                className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <input
                type="text"
                placeholder="URL docelowy"
                value={anchorUrl2}
                onChange={(e) => setAnchorUrl2(e.target.value)}
                className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>

            {/* Anchor 3 — opcjonalny */}
            <div className="flex gap-2 items-center">
              <span className="text-xs font-semibold text-gray-400 w-6">3</span>
              <input
                type="text"
                placeholder="Tekst kotwicy (opcjonalny)"
                value={anchorText3}
                onChange={(e) => setAnchorText3(e.target.value)}
                className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <input
                type="text"
                placeholder="URL docelowy"
                value={anchorUrl3}
                onChange={(e) => setAnchorUrl3(e.target.value)}
                className="flex-1 border border-gray-200 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            </div>
          </div>

          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">Ton głosu</label>
              <select
                value={toneOfVoice}
                onChange={(e) => setToneOfVoice(e.target.value)}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              >
                <option value="ekspert">Ekspert (autorytatywny)</option>
                <option value="przyjazny">Przyjazny (doradca)</option>
                <option value="formalny">Formalny (biznesowy)</option>
                <option value="poradnikowy">Poradnikowy (krok po kroku)</option>
              </select>
            </div>
            <div className="flex items-end pb-1">
              <label className="flex items-center gap-2 text-sm text-gray-700 cursor-pointer">
                <input
                  type="checkbox"
                  checked={useSerpScrape}
                  onChange={(e) => setUseSerpScrape(e.target.checked)}
                  className="rounded border-gray-300 text-blue-600"
                />
                Analizuj SERP top10 (DataForSEO)
              </label>
            </div>
          </div>

          {/* Custom prompt */}
          <div>
            <label className="flex items-center gap-2 text-sm font-medium text-gray-700 mb-2 cursor-pointer">
              <input
                type="checkbox"
                checked={useCustomPrompt}
                onChange={(e) => setUseCustomPrompt(e.target.checked)}
                className="rounded"
              />
              Dodatkowe instrukcje do prompta
            </label>
            {useCustomPrompt && (
              <textarea
                placeholder="np. Pisz w tonie eksperckim, użyj danych statystycznych, nie pisz o konkurencji..."
                value={customPrompt}
                onChange={(e) => setCustomPrompt(e.target.value)}
                rows={3}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            )}
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

          <button
            onClick={() => setStep(1)}
            disabled={!canProceedStep0}
            className="w-full py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Dalej — Wybierz domeny PBN →
          </button>
        </div>
      )}

      {/* Step 1: Domain selection */}
      {step === 1 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
          <h3 className="font-semibold text-gray-800 text-lg mb-4">Krok 2 — Wybierz domeny PBN</h3>
          <DomainSelector selected={selectedDomainIds} onChange={setSelectedDomainIds} clientId={selectedClient ? parseInt(selectedClient) : null} />
          <div className="flex gap-3 mt-5">
            <button
              onClick={() => setStep(0)}
              className="px-6 py-3 border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-50"
            >
              ← Wstecz
            </button>
            <button
              onClick={() => setStep(2)}
              disabled={!canProceedStep1}
              className="flex-1 py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 disabled:opacity-40"
            >
              Dalej — Generuj treść →
            </button>
          </div>
        </div>
      )}

      {/* Step 2: Generate */}
      {step === 2 && (
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-6">
          <h3 className="font-semibold text-gray-800 text-lg mb-4">Krok 3 — Generowanie treści</h3>
          <div className="bg-gray-50 rounded-lg p-4 mb-5 space-y-2 text-sm text-gray-600">
            <p><strong>Temat:</strong> {topic}</p>
            <p><strong>Domena klienta:</strong> {selectedClientDomain}</p>
            <p><strong>Kotwica 1:</strong> {anchorText}</p>
            {anchorText2 && <p><strong>Kotwica 2:</strong> {anchorText2} → {anchorUrl2}</p>}
            {anchorText3 && <p><strong>Kotwica 3:</strong> {anchorText3} → {anchorUrl3}</p>}
            {useCustomPrompt && customPrompt && <p><strong>Prompt:</strong> {customPrompt.slice(0, 80)}{customPrompt.length > 80 ? '...' : ''}</p>}
            <p><strong>Język:</strong> {language === 'pl' ? 'Polski' : 'English'}</p>
            <p><strong>Liczba domen PBN:</strong> {selectedDomainIds.length}</p>
          </div>

          {generating ? (
            <div className="flex flex-col items-center justify-center py-12">
              <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-blue-600 mb-4" />
              <p className="text-gray-600 font-medium">Generowanie artykułu i zdjęcia...</p>
              <p className="text-gray-400 text-sm mt-1">Może potrwać 20-40 sekund</p>
              {genProgress && (
                <div className="w-full max-w-md mt-4">
                  <div className="flex items-center justify-between mb-1">
                    <span className="text-xs font-medium text-gray-600">{genProgress.label}</span>
                    <span className="text-xs text-gray-400">{genProgress.pct}%</span>
                  </div>
                  <div className="w-full bg-gray-200 rounded-full h-2">
                    <div className="bg-blue-600 h-2 rounded-full transition-all duration-1000 ease-out" style={{ width: `${genProgress.pct}%` }} />
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="flex gap-3">
              <button
                onClick={() => setStep(1)}
                className="px-6 py-3 border border-gray-300 text-gray-700 rounded-lg font-medium hover:bg-gray-50"
              >
                ← Wstecz
              </button>
              <button
                onClick={handleGenerate}
                className="flex-1 py-3 bg-blue-600 text-white rounded-lg font-semibold hover:bg-blue-700 text-lg"
              >
                Generuj treść i zdjęcie
              </button>
            </div>
          )}
        </div>
      )}

      {/* Step 3: Edit + Publish */}
      {step === 3 && generated && (
        <div className="space-y-4">
          {/* Image row */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
            <div className="flex gap-4 items-start">
              {/* Image preview */}
              <div className="relative w-48 h-32 flex-shrink-0 rounded-lg overflow-hidden bg-gray-100">
                {generated.image_b64 ? (
                  <img
                    src={`data:image/jpeg;base64,${generated.image_b64}`}
                    alt="Featured"
                    className="w-full h-full object-cover"
                  />
                ) : (
                  <div className="w-full h-full flex items-center justify-center text-gray-300 text-xs">Brak obrazka</div>
                )}
                {regeneratingImage && (
                  <div className="absolute inset-0 bg-white/70 flex items-center justify-center">
                    <div className="animate-spin rounded-full h-6 w-6 border-b-2 border-blue-600" />
                  </div>
                )}
              </div>

              {/* Image controls */}
              <div className="flex-1 space-y-2">
                <p className="text-sm font-medium text-gray-700">Obrazek artykułu</p>
                <div className="flex gap-2 flex-wrap">
                  <button
                    onClick={handleRegenerateImage}
                    disabled={regeneratingImage}
                    className="px-3 py-1.5 bg-blue-600 text-white rounded-lg text-xs font-medium hover:bg-blue-700 disabled:opacity-40"
                  >
                    {regeneratingImage ? 'Generuję...' : 'Regeneruj obrazek (AI)'}
                  </button>
                  <button
                    onClick={() => fileInputRef.current?.click()}
                    className="px-3 py-1.5 border border-gray-300 text-gray-700 rounded-lg text-xs font-medium hover:bg-gray-50"
                  >
                    Wgraj foto statyczną
                  </button>
                  {staticImageB64 && (
                    <button
                      onClick={handleRegenerateImage}
                      disabled={regeneratingImage}
                      className="px-3 py-1.5 bg-purple-600 text-white rounded-lg text-xs font-medium hover:bg-purple-700 disabled:opacity-40"
                    >
                      Opisz i wygeneruj z foto
                    </button>
                  )}
                </div>
                {staticImageB64 && (
                  <p className="text-xs text-purple-600">Foto wgrana — kliknij "Opisz i wygeneruj z foto" żeby AI opisał screena i wygenerował obrazek</p>
                )}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept="image/*"
                  className="hidden"
                  onChange={handleStaticImageUpload}
                />
              </div>
            </div>
          </div>

          {/* Content edit */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4 space-y-3">
            <div className="flex items-center justify-between">
              <p className="text-sm font-medium text-gray-700">Tytuł i treść</p>
              <button
                onClick={() => setEditingContent(v => !v)}
                className="text-xs text-blue-600 hover:underline"
              >
                {editingContent ? 'Podgląd' : 'Edytuj treść'}
              </button>
            </div>

            <input
              type="text"
              value={editTitle}
              onChange={(e) => setEditTitle(e.target.value)}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm font-semibold focus:outline-none focus:ring-2 focus:ring-blue-500"
              placeholder="Tytuł artykułu"
            />

            {editingContent ? (
              <textarea
                value={editContent}
                onChange={(e) => setEditContent(e.target.value)}
                rows={16}
                className="w-full border border-gray-300 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
            ) : (
              <div
                className="prose prose-sm max-w-none text-gray-700 border border-gray-100 rounded-lg p-3 max-h-64 overflow-y-auto text-sm"
                dangerouslySetInnerHTML={{ __html: sanitizeHtml(editContent) }}
              />
            )}
          </div>

          {/* Publish panel */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-4">
            <h3 className="font-semibold text-gray-800 mb-3">Publikacja na {selectedDomainIds.length} domenach</h3>

            {!publishing && !publishDone && (
              <div className="space-y-3">
                <p className="text-sm text-gray-600">
                  Link kotwiczny prowadzi do <strong>{selectedClientDomain}</strong>.
                </p>
                <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="checkbox"
                    checked={dripDelay}
                    onChange={e => setDripDelay(e.target.checked)}
                    className="rounded border-gray-300 text-blue-600"
                  />
                  Drip publishing (losowe odstepy miedzy domenami)
                </label>
                <div className="flex gap-2">
                  <button
                    onClick={() => { setGenerated(null); setStep(2) }}
                    className="px-4 py-2 border border-gray-300 text-gray-600 rounded-lg text-sm hover:bg-gray-50"
                  >
                    Regeneruj treść
                  </button>
                  <button
                    onClick={handlePublish}
                    className="flex-1 py-2 bg-green-600 text-white rounded-lg font-semibold hover:bg-green-700"
                  >
                    Publikuj na {selectedDomainIds.length} domenach
                  </button>
                </div>
              </div>
            )}

            {(publishing || publishResults.length > 0) && (
              <div>
                <div className="flex items-center gap-2 mb-3">
                  {publishing && <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-blue-600" />}
                  <span className="text-sm font-medium text-gray-700">
                    {publishDone ? 'Zakończono!' : `Publikowanie... ${publishResults.length}/${selectedDomainIds.length}`}
                  </span>
                </div>
                <div className="space-y-2 max-h-64 overflow-y-auto">
                  {publishResults.map((r, i) => (
                    <div key={i} className={`flex items-center gap-2 p-2 rounded-lg text-sm ${
                      r.status === 'published' ? 'bg-green-50' : 'bg-red-50'
                    }`}>
                      <span>{r.status === 'published' ? '✓' : '✗'}</span>
                      <span className="flex-1 font-medium truncate">{r.domain}</span>
                      {r.url && (
                        <a href={r.url} target="_blank" rel="noreferrer"
                          className="text-blue-600 hover:underline text-xs">
                          Zobacz
                        </a>
                      )}
                      {r.error && (
                        <span className="text-red-500 text-xs truncate max-w-[100px]" title={r.error}>
                          {r.error.slice(0, 30)}
                        </span>
                      )}
                    </div>
                  ))}
                </div>

                {publishDone && (
                  <div className="mt-4 pt-3 border-t border-gray-100">
                    <p className="text-sm text-gray-600 mb-3">
                      Sukces: <strong className="text-green-600">{publishResults.filter(r => r.status === 'published').length}</strong> |
                      Błędy: <strong className="text-red-600">{publishResults.filter(r => r.status !== 'published').length}</strong>
                    </p>
                    <div className="flex gap-2 mb-2">
                      <button
                        onClick={async () => {
                          const domains = [...new Set(publishResults.filter(r => r.status === 'published').map(r => r.domain))]
                          setPingSitemap({ loading: true })
                          let ok = 0
                          for (const dom of domains) {
                            try {
                              await publishApi.pingSitemap({ domain: dom })
                              ok++
                            } catch {}
                          }
                          setPingSitemap({ loading: false })
                          addToast(`Sitemap ping wysłany do ${ok}/${domains.length} domen`, 'success')
                        }}
                        disabled={pingSitemap.loading}
                        className="flex-1 py-2 bg-purple-600 text-white rounded-lg text-sm font-medium hover:bg-purple-700 disabled:opacity-50"
                      >
                        {pingSitemap.loading ? 'Pingowanie...' : 'Ping Sitemap (Google + Bing)'}
                      </button>
                      <button
                        onClick={resetAll}
                        className="flex-1 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700"
                      >
                        Nowa publikacja
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
