import { useState, useEffect } from 'react'
import { NavLink } from 'react-router-dom'
import api from '../api/client'

const navGroups = [
  {
    label: 'Główne',
    items: [
      {
        to: '/dashboard',
        label: 'Dashboard',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" />
          </svg>
        ),
      },
      {
        to: '/projects',
        label: 'Projekty',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M19 11H5m14 0a2 2 0 012 2v6a2 2 0 01-2 2H5a2 2 0 01-2-2v-6a2 2 0 012-2m14 0V9a2 2 0 00-2-2M5 11V9a2 2 0 012-2m0 0V5a2 2 0 012-2h6a2 2 0 012 2v2M7 7h10" />
          </svg>
        ),
      },
    ],
  },
  {
    label: 'SEO & Content',
    items: [
      {
        to: '/topical-map',
        label: 'Topical Map',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 20l-5.447-2.724A1 1 0 013 16.382V5.618a1 1 0 011.447-.894L9 7m0 13l6-3m-6 3V7m6 10l4.553 2.276A1 1 0 0021 18.382V7.618a1 1 0 00-.553-.894L15 4m0 13V4m0 0L9 7" />
          </svg>
        ),
      },
      {
        to: '/domain-health',
        label: 'Zdrowie Domen',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
        ),
      },
      {
        to: '/autopilot',
        label: 'Autopilot',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M13 10V3L4 14h7v7l9-11h-7z" />
          </svg>
        ),
      },
      {
        to: '/news-portals',
        label: 'News Portals',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M19 20H5a2 2 0 01-2-2V6a2 2 0 012-2h10a2 2 0 012 2v1m2 13a2 2 0 01-2-2V7m2 13a2 2 0 002-2V9a2 2 0 00-2-2h-2m-4-3H9M7 16h6M7 8h6v4H7V8z" />
          </svg>
        ),
      },
      {
        to: '/content-writer',
        label: 'Content Writer',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M11 5H6a2 2 0 00-2 2v11a2 2 0 002 2h11a2 2 0 002-2v-5m-1.414-9.414a2 2 0 112.828 2.828L11.828 15H9v-2.828l8.586-8.586z" />
          </svg>
        ),
      },
      {
        to: '/link-checker',
        label: 'Link Checker',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M13.828 10.172a4 4 0 00-5.656 0l-4 4a4 4 0 105.656 5.656l1.102-1.101m-.758-4.899a4 4 0 005.656 0l4-4a4 4 0 00-5.656-5.656l-1.1 1.1" />
          </svg>
        ),
      },
      {
        to: '/analytics',
        label: 'Analityka',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" />
          </svg>
        ),
      },
    ],
  },
  {
    label: 'Publikowanie',
    items: [
      {
        to: '/publisher',
        label: 'Publikuj',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M12 19l9 2-9-18-9 18 9-2zm0 0v-8" />
          </svg>
        ),
      },
      {
        to: '/bulk-publish',
        label: 'Publikuj Ręcznie',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M4 16v1a3 3 0 003 3h10a3 3 0 003-3v-1m-4-8l-4-4m0 0L8 8m4-4v12" />
          </svg>
        ),
      },
      {
        to: '/history',
        label: 'Historia',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2" />
          </svg>
        ),
      },
    ],
  },
  {
    label: 'System',
    items: [
      {
        to: '/settings',
        label: 'Ustawienia',
        icon: (
          <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
              d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        ),
      },
    ],
  },
]

export default function Sidebar() {
  const [expiringCount, setExpiringCount] = useState(0)
  const [failedKeywords, setFailedKeywords] = useState(0)
  const [failedPosts, setFailedPosts] = useState(0)
  const [dismissed, setDismissed] = useState(() => {
    try { return JSON.parse(localStorage.getItem('pbn_dismissed_badges') || '{}') } catch { return {} }
  })
  const [dark, setDark] = useState(() => localStorage.getItem('pbn_dark') === '1')

  const dismiss = (key, e) => {
    e.preventDefault()
    e.stopPropagation()
    const next = { ...dismissed, [key]: Date.now() }
    setDismissed(next)
    localStorage.setItem('pbn_dismissed_badges', JSON.stringify(next))
  }

  useEffect(() => {
    document.documentElement.classList.toggle('dark', dark)
    localStorage.setItem('pbn_dark', dark ? '1' : '0')
  }, [dark])

  useEffect(() => {
    api.get('/api/health', { params: { limit: 500, offset: 0 } })
      .then(res => {
        const count = (res.data.domains || []).filter(d =>
          d.days_to_expiry !== null && d.days_to_expiry !== undefined && d.days_to_expiry < 30
        ).length
        if (count !== expiringCount) {
          // Reset dismiss when count changes (new data)
          const prev = dismissed['health']
          if (prev && count > 0) {
            const next = { ...dismissed }
            delete next['health']
            setDismissed(next)
            localStorage.setItem('pbn_dismissed_badges', JSON.stringify(next))
          }
        }
        setExpiringCount(count)
      })
      .catch(() => {})
    api.get('/api/autopilot/stats')
      .then(res => {
        const f = res.data.failed_keywords || 0
        if (f !== failedKeywords && dismissed['autopilot']) {
          const next = { ...dismissed }
          delete next['autopilot']
          setDismissed(next)
          localStorage.setItem('pbn_dismissed_badges', JSON.stringify(next))
        }
        setFailedKeywords(f)
      })
      .catch(() => {})
    api.get('/api/history/failed-count')
      .then(res => {
        const c = res.data.count || 0
        if (c !== failedPosts && dismissed['history']) {
          const next = { ...dismissed }
          delete next['history']
          setDismissed(next)
          localStorage.setItem('pbn_dismissed_badges', JSON.stringify(next))
        }
        setFailedPosts(c)
      })
      .catch(() => {})
  }, [])

  return (
    <div className="w-64 h-full flex flex-col" style={{ backgroundColor: '#1e293b' }}>
      <div className="p-6 border-b border-slate-700">
        <h1 className="text-white text-xl font-bold">PBN Publisher</h1>
        <p className="text-slate-400 text-xs mt-1">SEO Publishing Tool</p>
      </div>
      <nav className="flex-1 p-4 space-y-5 overflow-y-auto">
        {navGroups.map((group) => (
          <div key={group.label}>
            <p className="text-slate-500 text-xs uppercase font-semibold tracking-wider mb-2 px-4">{group.label}</p>
            <div className="space-y-1">
              {group.items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) =>
                    `flex items-center gap-3 px-4 py-3 rounded-lg text-sm font-medium transition-colors ${
                      isActive
                        ? 'bg-blue-600 text-white'
                        : 'text-slate-300 hover:bg-slate-700 hover:text-white'
                    }`
                  }
                >
                  {item.icon}
                  <span className="flex-1">{item.label}</span>
                  {item.to === '/domain-health' && expiringCount > 0 && !dismissed['health'] && (
                    <span className="relative group">
                      <span className="bg-orange-500 text-white text-xs font-bold rounded-full w-5 h-5 flex items-center justify-center">
                        {expiringCount > 9 ? '9+' : expiringCount}
                      </span>
                      <button onClick={(e) => dismiss('health', e)}
                        className="absolute -top-1.5 -right-1.5 bg-slate-600 text-white rounded-full w-3.5 h-3.5 text-[8px] leading-none items-center justify-center hidden group-hover:flex hover:bg-slate-500"
                        title="Ukryj">×</button>
                    </span>
                  )}
                  {item.to === '/autopilot' && failedKeywords > 0 && !dismissed['autopilot'] && (
                    <span className="relative group">
                      <span className="bg-red-500 text-white text-xs font-bold rounded-full w-5 h-5 flex items-center justify-center">
                        {failedKeywords > 9 ? '9+' : failedKeywords}
                      </span>
                      <button onClick={(e) => dismiss('autopilot', e)}
                        className="absolute -top-1.5 -right-1.5 bg-slate-600 text-white rounded-full w-3.5 h-3.5 text-[8px] leading-none items-center justify-center hidden group-hover:flex hover:bg-slate-500"
                        title="Ukryj">×</button>
                    </span>
                  )}
                  {item.to === '/history' && failedPosts > 0 && !dismissed['history'] && (
                    <span className="relative group">
                      <span className="bg-red-500 text-white text-xs font-bold rounded-full w-5 h-5 flex items-center justify-center">
                        {failedPosts > 9 ? '9+' : failedPosts}
                      </span>
                      <button onClick={(e) => dismiss('history', e)}
                        className="absolute -top-1.5 -right-1.5 bg-slate-600 text-white rounded-full w-3.5 h-3.5 text-[8px] leading-none items-center justify-center hidden group-hover:flex hover:bg-slate-500"
                        title="Ukryj">×</button>
                    </span>
                  )}
                </NavLink>
              ))}
            </div>
          </div>
        ))}
      </nav>
      <div className="p-4 border-t border-slate-700 flex items-center justify-between">
        <p className="text-slate-500 text-xs">v2.1.0</p>
        <button
          onClick={() => setDark(d => !d)}
          className="text-slate-400 hover:text-white transition-colors p-1 rounded"
          title={dark ? 'Tryb jasny' : 'Tryb ciemny'}
        >
          {dark ? (
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 3v1m0 16v1m9-9h-1M4 12H3m15.364 6.364l-.707-.707M6.343 6.343l-.707-.707m12.728 0l-.707.707M6.343 17.657l-.707.707M16 12a4 4 0 11-8 0 4 4 0 018 0z" />
            </svg>
          ) : (
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M20.354 15.354A9 9 0 018.646 3.646 9.003 9.003 0 0012 21a9.003 9.003 0 008.354-5.646z" />
            </svg>
          )}
        </button>
      </div>
    </div>
  )
}
