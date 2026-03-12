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
  { value: 'freepik_stock', label: 'Freepik Stock', desc: 'Zdjecia stockowe z Freepik (najszybsze)' },
  { value: 'freepik_zimage', label: 'Freepik Z-Image', desc: 'AI generowane — Turbo model (szybki, dobra jakosc)' },
  { value: 'freepik_flux', label: 'Freepik Flux', desc: 'AI generowane — Flux model (wolniejszy, lepsza jakosc)' },
  { value: 'dalle', label: 'DALL-E 3', desc: 'OpenAI DALL-E 3 — najwyzsza jakosc, najdrozszy' },
  { value: 'none', label: 'Brak obrazka', desc: 'Artykuly bez featured image' },
]

export default function Settings() {
  const [botToken, setBotToken] = useState('')
  const [chatId, setChatId] = useState('')
  const [configured, setConfigured] = useState(false)
  const [gptModel, setGptModel] = useState('gpt-4o-mini')
  const [imageSource, setImageSource] = useState('freepik_stock')
  const [encryptionKeySet, setEncryptionKeySet] = useState(false)
  const [apiKeysStatus, setApiKeysStatus] = useState({})
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [savingModel, setSavingModel] = useState(false)
  const [savingImage, setSavingImage] = useState(false)
  const [testing, setTesting] = useState(false)
  const addToast = useToast()

  useEffect(() => {
    notifications.getSettings()
      .then(res => {
        const s = res.data || {}
        if (s.telegram_bot_token_masked) setBotToken(s.telegram_bot_token_masked)
        if (s.telegram_chat_id) setChatId(s.telegram_chat_id)
        setConfigured(!!s.telegram_configured)
        if (s.gpt_model) setGptModel(s.gpt_model)
        if (s.default_image_source) setImageSource(s.default_image_source)
        setEncryptionKeySet(!!s.encryption_key_set)
        if (s.api_keys_status) setApiKeysStatus(s.api_keys_status)
      })
      .catch(() => {})
      .finally(() => setLoading(false))
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
    </div>
  )
}
