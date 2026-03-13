import { useState, useEffect } from 'react'
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import { ToastProvider } from './components/Toast'
import Dashboard from './pages/Dashboard'
import Projects from './pages/Projects'
import Publisher from './pages/Publisher'
import History from './pages/History'
import TopicalMap from './pages/TopicalMap'
import ContentWriter from './pages/ContentWriter'
import Autopilot from './pages/Autopilot'
import DomainHealth from './pages/DomainHealth'
import BulkPublish from './pages/BulkPublish'
import Analytics from './pages/Analytics'
import Settings from './pages/Settings'
import NewsPortals from './pages/NewsPortals'
import { hasAuthToken, setAuthCredentials, clearAuthCredentials, checkCredentials } from './api/client'
import api from './api/client'

function LoginPage({ onLogin }) {
  const [user, setUser] = useState('')
  const [pass, setPass] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  const submit = async (e) => {
    e.preventDefault()
    setLoading(true)
    setError('')
    try {
      await checkCredentials(user, pass)
      setAuthCredentials(user, pass)
      onLogin()
    } catch {
      setError('Nieprawidłowy login lub hasło')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
      <div className="bg-white dark:bg-gray-800 rounded-2xl shadow-lg border border-gray-100 dark:border-gray-700 p-8 w-full max-w-sm">
        <div className="text-center mb-6">
          <div className="text-3xl mb-2">🚀</div>
          <h1 className="text-xl font-bold text-gray-900 dark:text-white">PBN Publisher</h1>
          <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">Zaloguj się żeby kontynuować</p>
        </div>
        <form onSubmit={submit} className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Login</label>
            <input
              type="text" value={user} onChange={e => setUser(e.target.value)}
              autoFocus required
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          <div>
            <label className="block text-xs font-medium text-gray-600 dark:text-gray-400 mb-1">Hasło</label>
            <input
              type="password" value={pass} onChange={e => setPass(e.target.value)}
              required
              className="w-full border border-gray-200 dark:border-gray-600 rounded-lg px-3 py-2 text-sm bg-white dark:bg-gray-700 dark:text-white focus:outline-none focus:ring-2 focus:ring-blue-500"
            />
          </div>
          {error && <p className="text-red-600 dark:text-red-400 text-xs">{error}</p>}
          <button type="submit" disabled={loading}
            className="w-full py-2.5 bg-blue-600 text-white rounded-lg text-sm font-medium hover:bg-blue-700 disabled:opacity-50 transition-colors">
            {loading ? 'Loguję...' : 'Zaloguj się'}
          </button>
        </form>
      </div>
    </div>
  )
}

export default function App() {
  const [authed, setAuthed] = useState(hasAuthToken())
  // Only show spinner on first load when no token exists (need to check if password required)
  const [checking, setChecking] = useState(!hasAuthToken())

  useEffect(() => {
    // Only check if auth is needed when there's no token
    if (!hasAuthToken()) {
      api.get('/api/projects').then(() => {
        setAuthed(true)
        setChecking(false)
      }).catch(() => {
        setChecking(false)
      })
    } else {
      // Already have token — stop checking immediately, no extra request needed
      setChecking(false)
    }

    const onLogout = () => {
      setAuthed(false)
    }
    window.addEventListener('pbn_logout', onLogout)
    return () => window.removeEventListener('pbn_logout', onLogout)
  }, [])

  if (checking) {
    return (
      <div className="min-h-screen bg-gray-50 dark:bg-gray-900 flex items-center justify-center">
        <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
      </div>
    )
  }

  if (!authed) {
    return <LoginPage onLogin={() => setAuthed(true)} />
  }

  return (
    <BrowserRouter>
      <ToastProvider>
        <div className="flex h-screen overflow-hidden bg-gray-50 dark:bg-gray-900">
          <Sidebar />
          <main className="flex-1 overflow-y-auto">
            <Routes>
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<Dashboard />} />
              <Route path="/projects" element={<Projects />} />
              <Route path="/publisher" element={<Publisher />} />
              <Route path="/history" element={<History />} />
              <Route path="/topical-map" element={<TopicalMap />} />
              <Route path="/content-writer" element={<ContentWriter />} />
              <Route path="/autopilot" element={<Autopilot />} />
              <Route path="/domain-health" element={<DomainHealth />} />
              <Route path="/bulk-publish" element={<BulkPublish />} />
              <Route path="/analytics" element={<Analytics />} />
              <Route path="/news-portals" element={<NewsPortals />} />
              <Route path="/settings" element={<Settings />} />
            </Routes>
          </main>
        </div>
      </ToastProvider>
    </BrowserRouter>
  )
}
