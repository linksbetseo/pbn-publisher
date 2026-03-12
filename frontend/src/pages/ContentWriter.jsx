import { useState } from 'react'
import api from '../api/client'

const TONES = [
  { value: 'ekspert', label: 'Ekspert (autorytatywny)' },
  { value: 'przyjazny', label: 'Przyjazny (doradca)' },
  { value: 'formalny', label: 'Formalny (biznesowy)' },
  { value: 'poradnikowy', label: 'Poradnikowy (krok po kroku)' },
]

export default function ContentWriter() {
  const [form, setForm] = useState({
    keyword: '',
    client_domain: '',
    anchor_text: '',
    anchor_text2: '',
    anchor_url2: '',
    anchor_text3: '',
    anchor_url3: '',
    language: 'pl',
    tone_of_voice: 'ekspert',
    pillar_page_url: '',
    pillar_page_anchor: '',
    supporting_page_urls: '',
    custom_prompt: '',
    use_serp_scrape: true,
  })
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState('')
  const [copied, setCopied] = useState('')
  const [previewMode, setPreviewMode] = useState(false)

  const set = (field, value) => setForm(f => ({ ...f, [field]: value }))

  const generate = async () => {
    if (!form.keyword.trim() || !form.client_domain.trim() || !form.anchor_text.trim()) {
      setError('Wymagane: fraza kluczowa, domena klienta, anchor text')
      return
    }
    setLoading(true)
    setError('')
    setResult(null)
    try {
      const supporting_page_urls = form.supporting_page_urls
        .split('\n')
        .map(s => s.trim())
        .filter(Boolean)
      const resp = await api.post('/api/content-writer/generate', {
        keyword: form.keyword.trim(),
        client_domain: form.client_domain.trim(),
        anchor_text: form.anchor_text.trim(),
        anchor_text2: form.anchor_text2.trim(),
        anchor_url2: form.anchor_url2.trim(),
        anchor_text3: form.anchor_text3.trim(),
        anchor_url3: form.anchor_url3.trim(),
        language: form.language,
        tone_of_voice: form.tone_of_voice,
        pillar_page_url: form.pillar_page_url.trim(),
        pillar_page_anchor: form.pillar_page_anchor.trim(),
        supporting_page_urls,
        custom_prompt: form.custom_prompt.trim(),
        use_serp_scrape: form.use_serp_scrape,
      })
      setResult(resp.data)
    } catch (e) {
      setError(e.response?.data?.detail || e.message || 'Błąd generowania artykułu')
    } finally {
      setLoading(false)
    }
  }

  const copy = (text, key) => {
    navigator.clipboard.writeText(text).then(() => {
      setCopied(key)
      setTimeout(() => setCopied(''), 2000)
    })
  }

  const wordCount = result?.content
    ? result.content.replace(/<[^>]+>/g, ' ').split(/\s+/).filter(Boolean).length
    : 0

  return (
    <div className="p-8 max-w-7xl">
      <h2 className="text-2xl font-bold text-gray-900 mb-1">Content Writer</h2>
      <p className="text-gray-500 mb-6 text-sm">
        Generuje artykuł SEO na bazie analizy SERP top10 · H1 = bezpośrednia odpowiedź (AI Overview) · linkowanie wewnętrzne
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Form */}
        <div className="space-y-4">
          {/* Main settings */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
            <h3 className="font-semibold text-gray-800 mb-4 text-sm uppercase tracking-wide">Parametry artykułu</h3>
            <div className="space-y-3">
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  Fraza kluczowa <span className="text-red-500">*</span>
                </label>
                <input
                  value={form.keyword}
                  onChange={e => set('keyword', e.target.value)}
                  placeholder="np. kancelaria prawna Warszawa"
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
              </div>

              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Język</label>
                  <select
                    value={form.language}
                    onChange={e => set('language', e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    <option value="pl">Polski</option>
                    <option value="en">English</option>
                    <option value="de">Deutsch</option>
                  </select>
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Ton głosu</label>
                  <select
                    value={form.tone_of_voice}
                    onChange={e => set('tone_of_voice', e.target.value)}
                    className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                  >
                    {TONES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
                  </select>
                </div>
              </div>

              <div className="flex items-center gap-2">
                <input
                  id="serp"
                  type="checkbox"
                  checked={form.use_serp_scrape}
                  onChange={e => set('use_serp_scrape', e.target.checked)}
                  className="rounded border-gray-300 text-blue-600"
                />
                <label htmlFor="serp" className="text-sm text-gray-700">
                  Analizuj SERP top10 (DataForSEO) — lepsza jakość, wyższy koszt API
                </label>
              </div>
            </div>
          </div>

          {/* Links */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
            <h3 className="font-semibold text-gray-800 mb-4 text-sm uppercase tracking-wide">Linki do klienta</h3>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Domena klienta <span className="text-red-500">*</span></label>
                  <input value={form.client_domain} onChange={e => set('client_domain', e.target.value)} placeholder="https://klient.pl" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Anchor text <span className="text-red-500">*</span></label>
                  <input value={form.anchor_text} onChange={e => set('anchor_text', e.target.value)} placeholder="kancelaria prawna" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <input value={form.anchor_text2} onChange={e => set('anchor_text2', e.target.value)} placeholder="Anchor 2 (opcjonalny)" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
                <input value={form.anchor_url2} onChange={e => set('anchor_url2', e.target.value)} placeholder="URL 2 (opcjonalny)" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
              </div>
              <div className="grid grid-cols-2 gap-3">
                <input value={form.anchor_text3} onChange={e => set('anchor_text3', e.target.value)} placeholder="Anchor 3 (opcjonalny)" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
                <input value={form.anchor_url3} onChange={e => set('anchor_url3', e.target.value)} placeholder="URL 3 (opcjonalny)" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
              </div>
            </div>
          </div>

          {/* Internal linking */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
            <h3 className="font-semibold text-gray-800 mb-4 text-sm uppercase tracking-wide">Linkowanie wewnętrzne (PBN)</h3>
            <div className="space-y-3">
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">URL Pillar Page</label>
                  <input value={form.pillar_page_url} onChange={e => set('pillar_page_url', e.target.value)} placeholder="https://pbn-domena.pl/pilar-page/" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
                <div>
                  <label className="block text-sm font-medium text-gray-700 mb-1">Anchor Pillar Page</label>
                  <input value={form.pillar_page_anchor} onChange={e => set('pillar_page_anchor', e.target.value)} placeholder="poradnik prawny" className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500" />
                </div>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">Supporting Pages URLs (jedna na linię)</label>
                <textarea
                  value={form.supporting_page_urls}
                  onChange={e => set('supporting_page_urls', e.target.value)}
                  placeholder={'https://pbn-domena.pl/supporting-1/\nhttps://pbn-domena.pl/supporting-2/'}
                  rows={3}
                  className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
                />
              </div>
            </div>
          </div>

          {/* Custom prompt */}
          <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
            <h3 className="font-semibold text-gray-800 mb-3 text-sm uppercase tracking-wide">Dodatkowe instrukcje (opcjonalne)</h3>
            <textarea
              value={form.custom_prompt}
              onChange={e => set('custom_prompt', e.target.value)}
              placeholder="np. Skoncentruj się na aspektach prawnych dla firm. Unikaj tematu podatków."
              rows={3}
              className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 resize-none"
            />
          </div>

          <button
            onClick={generate}
            disabled={loading}
            className="w-full py-3 bg-blue-600 text-white rounded-xl text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 flex items-center justify-center gap-2"
          >
            {loading ? (
              <><span className="animate-spin inline-block w-4 h-4 border-2 border-white border-t-transparent rounded-full" />Generuję artykuł (30-60s)...</>
            ) : 'Generuj artykuł SEO'}
          </button>

          {error && (
            <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 text-red-700 text-sm">{error}</div>
          )}
        </div>

        {/* Result */}
        <div>
          {result ? (
            <div className="space-y-4">
              {/* Meta */}
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="font-semibold text-gray-800 text-sm uppercase tracking-wide">Meta dane</h3>
                  <span className="text-xs text-gray-500 bg-gray-100 px-2 py-1 rounded-full">{wordCount} słów</span>
                </div>
                <div className="space-y-3">
                  <div>
                    <div className="flex items-center justify-between mb-1">
                      <label className="text-xs font-medium text-gray-500 uppercase">Title ({result.title?.length || 0} znaków)</label>
                      <button onClick={() => copy(result.title, 'title')} className="text-xs text-blue-600 hover:underline">{copied === 'title' ? 'Skopiowano!' : 'Kopiuj'}</button>
                    </div>
                    <p className="text-sm text-gray-900 font-medium bg-gray-50 px-3 py-2 rounded">{result.title}</p>
                  </div>
                  {result.meta_description && (
                    <div>
                      <div className="flex items-center justify-between mb-1">
                        <label className="text-xs font-medium text-gray-500 uppercase">Meta description ({result.meta_description?.length || 0} znaków)</label>
                        <button onClick={() => copy(result.meta_description, 'meta')} className="text-xs text-blue-600 hover:underline">{copied === 'meta' ? 'Skopiowano!' : 'Kopiuj'}</button>
                      </div>
                      <p className="text-sm text-gray-700 bg-gray-50 px-3 py-2 rounded">{result.meta_description}</p>
                    </div>
                  )}
                  {(result.category || (result.tags && result.tags.length > 0)) && (
                    <div className="pt-2 border-t border-gray-100">
                      <label className="text-xs font-medium text-gray-500 uppercase block mb-2">Tagi i kategoria WP</label>
                      <div className="flex flex-wrap gap-2 items-center">
                        {result.category && (
                          <span className="inline-flex items-center gap-1 bg-blue-100 text-blue-800 text-xs font-semibold px-2 py-1 rounded-full">
                            <svg className="w-3 h-3" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z" /></svg>
                            {result.category}
                          </span>
                        )}
                        {result.tags?.map(tag => (
                          <span key={tag} className="bg-gray-100 text-gray-700 text-xs px-2 py-1 rounded-full">{tag}</span>
                        ))}
                        <button
                          onClick={() => copy([result.category, ...(result.tags || [])].filter(Boolean).join(', '), 'tags')}
                          className="text-xs text-blue-600 hover:underline ml-auto"
                        >
                          {copied === 'tags' ? 'Skopiowano!' : 'Kopiuj'}
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>

              {/* Content */}
              <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-5">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="font-semibold text-gray-800 text-sm uppercase tracking-wide">Treść artykułu</h3>
                  <div className="flex gap-2">
                    <button
                      onClick={() => setPreviewMode(!previewMode)}
                      className={`text-xs px-3 py-1 rounded-full border ${previewMode ? 'bg-gray-800 text-white border-gray-800' : 'text-gray-600 border-gray-300 hover:bg-gray-50'}`}
                    >
                      {previewMode ? 'HTML' : 'Podgląd'}
                    </button>
                    <button onClick={() => copy(result.content, 'content')} className="text-xs text-blue-600 hover:underline">{copied === 'content' ? 'Skopiowano!' : 'Kopiuj HTML'}</button>
                  </div>
                </div>
                {previewMode ? (
                  <div
                    className="prose prose-sm max-w-none text-gray-800 max-h-[600px] overflow-y-auto"
                    dangerouslySetInnerHTML={{ __html: result.content }}
                  />
                ) : (
                  <textarea
                    readOnly
                    value={result.content}
                    rows={20}
                    className="w-full border border-gray-200 rounded-lg px-3 py-2 text-xs font-mono text-gray-700 bg-gray-50 resize-none focus:outline-none"
                  />
                )}
              </div>
            </div>
          ) : (
            <div className="bg-white rounded-xl border border-gray-200 shadow-sm p-12 flex flex-col items-center justify-center text-center h-64">
              <svg className="w-12 h-12 text-gray-200 mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.5} d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
              </svg>
              <p className="text-gray-400 text-sm">Wypełnij formularz i kliknij "Generuj artykuł SEO"</p>
              <p className="text-gray-300 text-xs mt-1">Artykuł zostanie wygenerowany na podstawie analizy SERP top10</p>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
