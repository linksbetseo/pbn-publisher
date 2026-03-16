import { useState, useEffect } from 'react'
import { notifications } from '../api/client'
import { useToast } from '../components/Toast'

const GPT_MODELS = [
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', desc: 'Szybki i tani — dobry do wiekszosci artykulow' },
  { value: 'gpt-4.1-nano', label: 'GPT-4.1 Nano', desc: 'Najszybszy i najtanszy model' },
  { value: 'gpt-4.1-mini', label: 'GPT-4.1 Mini', desc: 'Dobry balans miedzy jakoscia a cena' },
  { value: 'gpt-4o', label: 'GPT-4o', desc: 'Wysoka jakosc, wyzszy koszt' },
  { value: 'gpt-4.1', label: 'GPT-4.1', desc: 'Najnowszy pelny model — najlepsza jakosc' },
  { value: 'gpt-4-turbo', label: 'GPT-4 Turbo', desc: 'Starszy model premium' },
  { value: 'o3-mini', label: 'o3-mini', desc: 'Model rozumowania — wolniejszy, dokladniejszy' },
]

const IMAGE_SOURCES = [
  { value: 'freepik_flux', label: 'Freepik Flux (AI)', desc: 'AI generowane — Flux Pro 1.1, najlepsza jakosc (rekomendowane)' },
  { value: 'freepik_zimage', label: 'Freepik Z-Image (AI)', desc: 'AI generowane — Turbo model (szybszy, dobra jakosc)' },
  { value: 'freepik_stock', label: 'Freepik Stock', desc: 'Zdjecia stockowe z Freepik (najszybsze)' },
  { value: 'gemini', label: 'Gemini (AI)', desc: 'Google Gemini — darmowe (preview), nieprzewidywalne' },
  { value: 'dalle', label: 'DALL-E 3 (AI)', desc: 'OpenAI DALL-E 3 — najwyzsza jakosc, ~$0.04/img' },
  { value: 'none', label: 'Brak obrazka', desc: 'Artykuly bez featured image' },
]

export default function Settings() {
  const [botToken, setBotToken] = useState('')
  const [chatId, setChatId] = useState('')
  const [configured, setConfigured] = useState(false)
  const [gptModel, setGptModel] = useState('gpt-4o-mini')
  const [imageSource, setImageSource] = useState('freepik_flux')
  const [encryptionKeySet, setEncryptionKeySet] = useState(false)
  const [apiKeysStatus, setApiKeysStatus] = useState({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [savingModel, setSavingModel] = useState(false)
  const [savingImage, setSavingImage] = useState(false)
  const [testing, setTesting] = useState(false)
  // Custom LLM
  const [customLlm, setCustomLlm] = useState({ enabled: false, base_url: '', model: '', api_key: '', api_key_masked: '', api_key_set: false, max_tokens: 0, serp_chars: 0 })
  const [savingCustomLlm, setSavingCustomLlm] = useState(false)
  const [testingCustomLlm, setTestingCustomLlm] = useState(false)
  const [customLlmTestResult, setCustomLlmTestResult] = useState(null)
  const [notifyPrefs, setNotifyPrefs] = useState({
    notify_autopilot_done: true,
    notify_bulk_publish_done: true,
    notify_news_approve: true,
    notify_news_generate: true,
    notify_health_snapshot: true,
    notify_errors: true,
  })
  const [savingPrefs, setSavingPrefs] = useState(false)
  const addToast = useToast()

  useEffect(() => {
    Promise.all([
      notifications.getSettings(),
      notifications.getCustomLlm(),
    ]).then(([settingsRes, llmRes]) => {
      const s = settingsRes.data || {}
      if (s.telegram_bot_token_masked) setBotToken(s.telegram_bot_token_masked)
      if (s.telegram_chat_id) setChatId(s.telegram_chat_id)
      setConfigured(!!s.telegram_configured)
      if (s.gpt_model) setGptModel(s.gpt_model)
      if (s.default_image_source) setImageSource(s.default_image_source)
      setEncryptionKeySet(!!s.encryption_key_set)
      if (s.api_keys_status) setApiKeysStatus(s.api_keys_status)
      if (s.notify_prefs) setNotifyPrefs(prev => ({ ...prev, ...s.notify_prefs }))
      const l = llmRes.data || {}
      setCustomLlm({
        enabled: !!l.enabled,
        base_url: l.base_url || '',
        model: l.model || '',
        api_key: l.api_key_masked || '',
        api_key_masked: l.api_key_masked || '',
        api_key_set: !!l.api_key_set,
        max_tokens: l.max_tokens || 0,
        serp_chars: l.serp_chars || 0,
      })
    }).catch(() => {}).finally(() => setLoading(false))
  }, [])

  const handleSaveTelegram = async () => {
    if (!botToken.trim() || !chatId.trim()) {
      addToast('Uzupelnij oba pola', 'warning')
      return
    }
    // Don't send masked token back
    if (botToken.includes('...')) {
      addToast('Wpisz pelny token bota (nie zamaskowany)', 'warning')
      return
    }
    setSaving(true)
    try {
      await notifications.saveTelegram({ bot_token: botToken.trim(), chat_id: chatId.trim() })
      setConfigured(true)
      addToast('Konfiguracja Telegram zapisana', 'success')
    } catch (e) {
      addToast(e.response?.data?.detail || 'Blad zapisu konfiguracji', 'error')
    } finally {
      setSaving(false)
    }
  }

  const handleTest = async () => {
    setTesting(true)
    try {
      await notifications.telegramTest()
      addToast('Wiadomosc testowa wyslana na Telegram', 'success')
    } catch (e) {
      addToast(e.response?.data?.detail || 'Blad wysylania wiadomosci testowej', 'error')
    } finally {
      setTesting(false)
    }
  }

  const handleSaveCustomLlm = async () => {
    setSavingCustomLlm(true)
    try {
      const payload = {
        enabled: customLlm.enabled,
        base_url: customLlm.base_url.trim(),
        model: customLlm.model.trim(),
        api_key: customLlm.api_key,
        max_tokens: parseInt(customLlm.max_tokens) || 0,
        serp_chars: parseInt(customLlm.serp_chars) || 0,
      }
      await notifications.saveCustomLlm(payload)
      addToast(customLlm.enabled ? 'Własny LLM aktywny — artykuły będą generowane przez Twój endpoint' : 'Własny LLM wyłączony — używam standardowego OpenAI', 'success')
    } catch (e) {
      addToast(e.response?.data?.detail || 'Błąd zapisu', 'error')
    } finally {
      setSavingCustomLlm(false)
    }
  }

  const handleTestCustomLlm = async () => {
    setTestingCustomLlm(true)
    setCustomLlmTestResult(null)
    try {
      const res = await notifications.testCustomLlm()
      setCustomLlmTestResult(res.data)
    } catch (e) {
      setCustomLlmTestResult({ ok: false, error: e.response?.data?.detail || 'Błąd połączenia' })
    } finally {
      setTestingCustomLlm(false)
    }
  }

  const handleSaveImageSource = async () => {
    setSavingImage(true)
    try {
      await notifications.saveImageSource(imageSource)
      addToast(`Domyslne zrodlo obrazkow: ${IMAGE_SOURCES.find(s => s.value === imageSource)?.label}`, 'success')
    } catch (e) {
      addToast(e.response?.data?.detail || 'Blad zapisu', 'error')
    } finally {
      setSavingImage(false)
    }
  }

  const handleSaveNotifyPrefs = async () => {
    setSavingPrefs(true)
    try {
      await notifications.saveNotifyPrefs(notifyPrefs)
      addToast('Preferencje powiadomien zapisane', 'success')
    } catch (e) {
      addToast(e.response?.data?.detail || 'Blad zapisu preferencji', 'error')
    } finally {
      setSavingPrefs(false)
    }
  }

  const handleSaveModel = async () => {
    setSavingModel(true)
    try {
      await notifications.saveGptModel(gptModel)
      addToast(`Model zmieniony na ${gptModel} — nowe artykuly beda generowane tym modelem`, 'success')
    } catch (e) {
      addToast(e.response?.data?.detail || 'Blad zapisu modelu', 'error')
    } finally {
      setSavingModel(false)
    }
  }

  if (loading) {
    return (
      <div className="p-8 max-w-3xl">
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white mb-2">Ustawienia</h2>
        <div className="flex items-center justify-center py-16">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
        </div>
      </div>
    )
  }

  return (
    <div className="p-8 max-w-3xl space-y-6">
      <div>
        <h2 className="text-2xl font-bold text-gray-900 dark:text-white mb-1">Ustawienia</h2>
        <p className="text-gray-500 dark:text-gray-400">Konfiguracja systemu, modelu AI i powiadomien</p>
      </div>

      {/* Encryption Status */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="flex items-center justify-between mb-4">
          <div>
            <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Szyfrowanie hasel WP</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">AES-256 Fernet encryption dla hasel WordPress</p>
          </div>
          <span className={`px-3 py-1 rounded-full text-xs font-semibold ${
            encryptionKeySet
              ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
              : 'bg-yellow-100 text-yellow-700 dark:bg-yellow-900/50 dark:text-yellow-300'
          }`}>
            {encryptionKeySet ? 'Klucz ustawiony' : 'Klucz tymczasowy'}
          </span>
        </div>

        {encryptionKeySet ? (
          <div className="bg-green-50 dark:bg-green-900/20 border border-green-200 dark:border-green-800 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <svg className="w-5 h-5 text-green-600 dark:text-green-400 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
              </svg>
              <div>
                <p className="text-sm font-medium text-green-800 dark:text-green-300">PBN_ENCRYPTION_KEY jest ustawiony w .env</p>
                <p className="text-xs text-green-600 dark:text-green-400 mt-1">Hasla WordPress sa szyfrowane i bezpieczne miedzy restartami serwera.</p>
              </div>
            </div>
          </div>
        ) : (
          <div className="bg-yellow-50 dark:bg-yellow-900/20 border border-yellow-200 dark:border-yellow-800 rounded-lg p-4">
            <div className="flex items-start gap-3">
              <svg className="w-5 h-5 text-yellow-600 dark:text-yellow-400 mt-0.5 flex-shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" />
              </svg>
              <div>
                <p className="text-sm font-medium text-yellow-800 dark:text-yellow-300">PBN_ENCRYPTION_KEY nie jest ustawiony</p>
                <p className="text-xs text-yellow-600 dark:text-yellow-400 mt-1">
                  Serwer uzywa tymczasowego klucza. Hasla zaszyfrowane teraz beda nieczytelne po restarcie.
                  Sprawdz logi serwera — klucz zostal tam wydrukowany. Dodaj go do pliku .env:
                </p>
                <code className="block mt-2 text-xs bg-yellow-100 dark:bg-yellow-900/40 px-3 py-2 rounded font-mono text-yellow-800 dark:text-yellow-300">
                  PBN_ENCRYPTION_KEY=twoj_klucz_z_logow_serwera
                </code>
              </div>
            </div>
          </div>
        )}
      </div>

      {/* API Keys Status */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="mb-4">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Klucze API</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Status kluczy w zmiennych srodowiskowych (.env)</p>
        </div>
        <div className="space-y-3">
          {[
            { key: 'openai', label: 'OpenAI', desc: 'OPENAI_API_KEY — generowanie artykulow + DALL-E' },
            { key: 'freepik', label: 'Freepik', desc: 'FREEPIK_API_KEY — obrazki AI (Z-Image, Flux, Stock)' },
            { key: 'dataforseo', label: 'DataForSEO', desc: 'DATAFORSEO_LOGIN + PASSWORD — dane SERP' },
            { key: 'rocket_indexer', label: 'Rocket Indexer', desc: 'ROCKET_INDEXER_TOKEN — indeksowanie Google' },
            { key: 'telegram_indexer', label: 'Telegram Indexer', desc: 'TELEGRAM_INDEXER_TOKEN — link-indexing-bot.com' },
          ].map(item => (
            <div key={item.key} className="flex items-center justify-between p-3 rounded-lg border border-gray-200 dark:border-gray-600">
              <div>
                <span className="text-sm font-medium text-gray-900 dark:text-white">{item.label}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400">{item.desc}</p>
              </div>
              <span className={`px-2.5 py-0.5 rounded-full text-xs font-semibold ${
                apiKeysStatus[item.key]
                  ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
                  : 'bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300'
              }`}>
                {apiKeysStatus[item.key] ? 'Skonfigurowany' : 'Brak'}
              </span>
            </div>
          ))}
        </div>
      </div>

      {/* GPT Model Selection */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Model AI do generowania</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Wybierz model OpenAI uzyty przy tworzeniu artykulow</p>
          </div>
          <span className="px-3 py-1 rounded-full text-xs font-semibold bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300">
            {gptModel}
          </span>
        </div>

        <div className="space-y-2 mb-4">
          {GPT_MODELS.map(m => (
            <label key={m.value}
              className={`flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                gptModel === m.value
                  ? 'border-blue-500 bg-blue-50 dark:bg-blue-900/20 dark:border-blue-400'
                  : 'border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700'
              }`}
            >
              <input
                type="radio"
                name="gpt_model"
                value={m.value}
                checked={gptModel === m.value}
                onChange={() => setGptModel(m.value)}
                className="w-4 h-4 text-blue-600"
              />
              <div className="flex-1 min-w-0">
                <span className="text-sm font-medium text-gray-900 dark:text-white">{m.label}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400">{m.desc}</p>
              </div>
            </label>
          ))}
        </div>

        <button
          onClick={handleSaveModel}
          disabled={savingModel}
          className="px-6 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {savingModel ? 'Zapisywanie...' : 'Zapisz model'}
        </button>
      </div>

      {/* Custom LLM */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Własny LLM (OpenAI-compatible)</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">
              LM Studio, Groq, Ollama, llama.cpp lub dowolny serwer zgodny z OpenAI API
            </p>
          </div>
          <label className="flex items-center gap-2 cursor-pointer">
            <div
              onClick={() => setCustomLlm(prev => ({ ...prev, enabled: !prev.enabled }))}
              className={`relative w-11 h-6 rounded-full transition-colors cursor-pointer ${customLlm.enabled ? 'bg-blue-600' : 'bg-gray-300 dark:bg-gray-600'}`}
            >
              <span className={`absolute top-0.5 left-0.5 w-5 h-5 bg-white rounded-full shadow transition-transform ${customLlm.enabled ? 'translate-x-5' : ''}`} />
            </div>
            <span className={`text-xs font-semibold ${customLlm.enabled ? 'text-blue-600' : 'text-gray-400'}`}>
              {customLlm.enabled ? 'Aktywny' : 'Wyłączony'}
            </span>
          </label>
        </div>

        {customLlm.enabled && (
          <div className="mb-4 p-3 bg-blue-50 dark:bg-blue-900/20 border border-blue-200 dark:border-blue-800 rounded-lg text-xs text-blue-700 dark:text-blue-300">
            Wszystkie artykuły będą generowane przez Twój endpoint zamiast OpenAI. Upewnij się, że serwer jest dostępny.
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Server URL <span className="text-gray-400 font-normal">(bez /v1)</span>
            </label>
            <input
              type="text"
              value={customLlm.base_url}
              onChange={e => setCustomLlm(prev => ({ ...prev, base_url: e.target.value }))}
              placeholder="http://localhost:1234  lub  https://api.groq.com/openai"
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono"
            />
            <p className="text-xs text-gray-400 mt-1">LM Studio: <code>http://localhost:1234</code> · Groq: <code>https://api.groq.com/openai</code> · Ollama: <code>http://localhost:11434/v1</code></p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Nazwa modelu</label>
            <input
              type="text"
              value={customLlm.model}
              onChange={e => setCustomLlm(prev => ({ ...prev, model: e.target.value }))}
              placeholder="llama-3.1-8b-instruct  lub  llama3-70b-8192  lub  mistral"
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono"
            />
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              API Key <span className="text-gray-400 font-normal">(opcjonalny — LM Studio nie wymaga)</span>
            </label>
            <input
              type="password"
              value={customLlm.api_key}
              onChange={e => setCustomLlm(prev => ({ ...prev, api_key: e.target.value }))}
              placeholder={customLlm.api_key_set ? '••••••••••••••••' : 'Zostaw puste jeśli niepotrzebny'}
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500 font-mono"
            />
            {customLlm.api_key_set && (
              <p className="text-xs text-gray-400 mt-1">Klucz zapisany. Wpisz nowy żeby zmienić.</p>
            )}
          </div>
        </div>

        <div className="grid grid-cols-2 gap-4 mt-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Max tokens odpowiedzi <span className="text-gray-400 font-normal">(0 = auto)</span>
            </label>
            <input
              type="number"
              min="0"
              value={customLlm.max_tokens}
              onChange={e => setCustomLlm(prev => ({ ...prev, max_tokens: parseInt(e.target.value) || 0 }))}
              placeholder="0 = auto (2500 małe / 6000 duże modele)"
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
              Kontekst SERP (znaki) <span className="text-gray-400 font-normal">(0 = auto)</span>
            </label>
            <input
              type="number"
              min="0"
              value={customLlm.serp_chars}
              onChange={e => setCustomLlm(prev => ({ ...prev, serp_chars: parseInt(e.target.value) || 0 }))}
              placeholder="0 = auto (1200 małe / pełny duże)"
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
        </div>

        {customLlmTestResult && (
          <div className={`mt-4 p-3 rounded-lg text-sm ${customLlmTestResult.ok ? 'bg-green-50 dark:bg-green-900/20 border border-green-200 text-green-700 dark:text-green-300' : 'bg-red-50 dark:bg-red-900/20 border border-red-200 text-red-700 dark:text-red-300'}`}>
            {customLlmTestResult.ok
              ? <>✓ Połączenie OK · Model: <strong>{customLlmTestResult.model}</strong> · Odpowiedź: <em>{customLlmTestResult.response}</em></>
              : <>✗ Błąd: {customLlmTestResult.error}</>
            }
          </div>
        )}

        <div className="flex gap-3 mt-5">
          <button
            onClick={handleSaveCustomLlm}
            disabled={savingCustomLlm}
            className="px-6 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
          >
            {savingCustomLlm ? 'Zapisywanie...' : 'Zapisz konfigurację'}
          </button>
          <button
            onClick={handleTestCustomLlm}
            disabled={testingCustomLlm || !customLlm.base_url || !customLlm.model}
            className="px-6 py-2.5 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 rounded-lg text-sm font-medium hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50 transition-colors"
          >
            {testingCustomLlm ? 'Testowanie...' : 'Testuj połączenie'}
          </button>
        </div>
      </div>

      {/* Default Image Source */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Domyslne zrodlo obrazkow</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Provider obrazkow dla nowych artykulow (mozna nadpisac per schedule w autopilocie)</p>
          </div>
          <span className="px-3 py-1 rounded-full text-xs font-semibold bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300">
            {IMAGE_SOURCES.find(s => s.value === imageSource)?.label || imageSource}
          </span>
        </div>

        <div className="space-y-2 mb-4">
          {IMAGE_SOURCES.map(s => (
            <label key={s.value}
              className={`flex items-center gap-3 p-3 rounded-lg border cursor-pointer transition-colors ${
                imageSource === s.value
                  ? 'border-purple-500 bg-purple-50 dark:bg-purple-900/20 dark:border-purple-400'
                  : 'border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700'
              }`}
            >
              <input
                type="radio"
                name="image_source"
                value={s.value}
                checked={imageSource === s.value}
                onChange={() => setImageSource(s.value)}
                className="w-4 h-4 text-purple-600"
              />
              <div className="flex-1 min-w-0">
                <span className="text-sm font-medium text-gray-900 dark:text-white">{s.label}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400">{s.desc}</p>
              </div>
            </label>
          ))}
        </div>

        <button
          onClick={handleSaveImageSource}
          disabled={savingImage}
          className="px-6 py-2.5 bg-purple-600 text-white rounded-lg text-sm font-semibold hover:bg-purple-700 disabled:opacity-50 transition-colors"
        >
          {savingImage ? 'Zapisywanie...' : 'Zapisz zrodlo obrazkow'}
        </button>
      </div>

      {/* Telegram Notifications */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="flex items-center justify-between mb-5">
          <div>
            <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Powiadomienia Telegram</h3>
            <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Otrzymuj alerty o publikacjach i bledach</p>
          </div>
          <span className={`px-3 py-1 rounded-full text-xs font-semibold ${
            configured
              ? 'bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300'
              : 'bg-gray-100 text-gray-500 dark:bg-gray-700 dark:text-gray-400'
          }`}>
            {configured ? 'Skonfigurowany' : 'Nieskonfigurowany'}
          </span>
        </div>

        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Bot Token</label>
            <input
              type="text"
              value={botToken}
              onChange={e => setBotToken(e.target.value)}
              placeholder="123456789:ABCdefGHIjklmNOPqrstUVWxyz"
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">Uzyskaj token od @BotFather na Telegramie</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">Chat ID</label>
            <input
              type="text"
              value={chatId}
              onChange={e => setChatId(e.target.value)}
              placeholder="-1001234567890"
              className="w-full border border-gray-300 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
            <p className="text-xs text-gray-400 dark:text-gray-500 mt-1">ID czatu lub grupy docelowej</p>
          </div>

          <div className="flex gap-3 pt-2">
            <button
              onClick={handleSaveTelegram}
              disabled={saving}
              className="px-6 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
            >
              {saving ? 'Zapisywanie...' : 'Zapisz konfiguracje'}
            </button>
            <button
              onClick={handleTest}
              disabled={testing || !configured}
              className="px-6 py-2.5 border border-gray-300 dark:border-gray-600 text-gray-700 dark:text-gray-300 rounded-lg text-sm font-medium hover:bg-gray-50 dark:hover:bg-gray-700 disabled:opacity-50 transition-colors"
            >
              {testing ? 'Wysylanie...' : 'Wyslij test'}
            </button>
          </div>
        </div>
      </div>

      {/* Notification Preferences */}
      <div className="bg-white dark:bg-gray-800 rounded-xl border border-gray-200 dark:border-gray-700 shadow-sm p-6">
        <div className="mb-5">
          <h3 className="text-lg font-semibold text-gray-800 dark:text-gray-200">Rodzaje powiadomien Telegram</h3>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-0.5">Wybierz o czym chcesz otrzymywac powiadomienia</p>
        </div>

        <div className="space-y-3 mb-5">
          {[
            { key: 'notify_autopilot_done', label: 'Autopilot zakonczony', desc: 'Po zakonczeniu dziennego autopilota — podsumowanie publikacji' },
            { key: 'notify_bulk_publish_done', label: 'Publikacja zbiorcza', desc: 'Po zakonczeniu ręcznej publikacji zbiorczej' },
            { key: 'notify_news_approve', label: 'News Portal — publikacja', desc: 'Po zatwierdzeniu i opublikowaniu artykulu z news portalu' },
            { key: 'notify_news_generate', label: 'News Portal — generowanie', desc: 'Po wygenerowaniu nowych artykulow AI z news portalu' },
            { key: 'notify_health_snapshot', label: 'Health snapshot zakonczony', desc: 'Po zakonczeniu skanowania zdrowia domen' },
            { key: 'notify_errors', label: 'Bledy i ostrzezenia', desc: 'Krytyczne bledy publikacji, problemy z API, nieudane operacje' },
          ].map(item => (
            <label key={item.key}
              className="flex items-start gap-3 p-3 rounded-lg border border-gray-200 dark:border-gray-600 hover:bg-gray-50 dark:hover:bg-gray-700 cursor-pointer transition-colors"
            >
              <input
                type="checkbox"
                checked={!!notifyPrefs[item.key]}
                onChange={e => setNotifyPrefs(prev => ({ ...prev, [item.key]: e.target.checked }))}
                className="w-4 h-4 text-blue-600 rounded mt-0.5"
              />
              <div className="flex-1 min-w-0">
                <span className="text-sm font-medium text-gray-900 dark:text-white">{item.label}</span>
                <p className="text-xs text-gray-500 dark:text-gray-400">{item.desc}</p>
              </div>
            </label>
          ))}
        </div>

        <button
          onClick={handleSaveNotifyPrefs}
          disabled={savingPrefs || !configured}
          className="px-6 py-2.5 bg-blue-600 text-white rounded-lg text-sm font-semibold hover:bg-blue-700 disabled:opacity-50 transition-colors"
        >
          {savingPrefs ? 'Zapisywanie...' : 'Zapisz preferencje'}
        </button>
        {!configured && (
          <p className="text-xs text-gray-400 dark:text-gray-500 mt-2">Najpierw skonfiguruj Telegram powyzej</p>
        )}
      </div>
    </div>
  )
}
