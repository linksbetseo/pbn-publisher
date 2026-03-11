import { useState, useEffect } from 'react'
import { dashboard, history as historyApi } from '../api/client'

function StatCard({ label, value, color, icon }) {
  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6">
      <div className="flex items-center justify-between">
        <div>
          <p className="text-sm text-gray-500 font-medium">{label}</p>
          <p className={`text-3xl font-bold mt-1 ${color}`}>{value}</p>
        </div>
        <div className={`w-12 h-12 rounded-full flex items-center justify-center ${color.replace('text-', 'bg-').replace('-600', '-100')}`}>
          {icon}
        </div>
      </div>
    </div>
  )
}

function PublicationChart({ posts }) {
  // Build last 14 days count
  const days = []
  for (let i = 13; i >= 0; i--) {
    const d = new Date()
    d.setDate(d.getDate() - i)
    days.push(d.toISOString().slice(0, 10))
  }
  const counts = {}
  days.forEach(d => { counts[d] = 0 })
  posts.forEach(p => {
    const day = (p.created_at || '').slice(0, 10)
    if (counts[day] !== undefined) counts[day]++
  })
  const values = days.map(d => counts[d])
  const max = Math.max(...values, 1)

  return (
    <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6">
      <h3 className="text-base font-semibold text-gray-800 mb-4">Publikacje — ostatnie 14 dni</h3>
      <div className="flex items-end gap-1.5 h-28">
        {days.map((day, i) => (
          <div key={day} className="flex-1 flex flex-col items-center gap-1">
            <div
              className="w-full bg-blue-500 rounded-t transition-all"
              style={{ height: `${Math.round((values[i] / max) * 96)}px`, minHeight: values[i] > 0 ? '4px' : '0' }}
              title={`${day}: ${values[i]} postów`}
            />
            {i % 2 === 0 && (
              <span className="text-gray-400 text-[9px] rotate-45 origin-left whitespace-nowrap">
                {day.slice(5)}
              </span>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}

export default function Dashboard() {
  const [stats, setStats] = useState(null)
  const [recentPosts, setRecentPosts] = useState([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    Promise.all([
      dashboard.stats(),
      historyApi.list({ limit: 500, offset: 0, status: 'published' }),
    ])
      .then(([s, p]) => {
        setStats(s)
        setRecentPosts(p.data.posts || [])
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [])

  return (
    <div className="p-8">
      <h2 className="text-2xl font-bold text-gray-900 mb-2">Dashboard</h2>
      <p className="text-gray-500 mb-8">Przegląd systemu PBN Publisher</p>

      {loading ? (
        <div className="flex items-center justify-center h-40">
          <div className="animate-spin rounded-full h-8 w-8 border-b-2 border-blue-600" />
        </div>
      ) : (
        <>
          <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4 mb-8">
            <StatCard label="Moje domeny" value={stats?.total_domains ?? 0} color="text-blue-600"
              icon={<svg className="w-6 h-6 text-blue-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M21 12a9 9 0 01-9 9m9-9a9 9 0 00-9-9m9 9H3m9 9a9 9 0 01-9-9m9 9c1.657 0 3-4.03 3-9s-1.343-9-3-9m0 18c-1.657 0-3-4.03-3-9s1.343-9 3-9m-9 9a9 9 0 019-9" /></svg>}
            />
            <StatCard label="Opublikowane" value={stats?.total_published ?? 0} color="text-green-600"
              icon={<svg className="w-6 h-6 text-green-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z" /></svg>}
            />
            <StatCard label="Dziś" value={stats?.posts_today ?? 0} color="text-purple-600"
              icon={<svg className="w-6 h-6 text-purple-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" /></svg>}
            />
            <StatCard label="Klienci" value={stats?.total_clients ?? 0} color="text-orange-600"
              icon={<svg className="w-6 h-6 text-orange-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0z" /></svg>}
            />
            <StatCard label="Autopilot aktywny" value={stats?.active_schedules ?? 0} color="text-cyan-600"
              icon={<svg className="w-6 h-6 text-cyan-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>}
            />
            <StatCard label="Frazy w kolejce" value={stats?.pending_keywords ?? 0} color="text-yellow-600"
              icon={<svg className="w-6 h-6 text-yellow-600" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 10h16M4 14h16M4 18h16" /></svg>}
            />
          </div>

          <div className="mb-8">
            <PublicationChart posts={recentPosts} />
          </div>

          <div className="bg-white rounded-xl shadow-sm border border-gray-100 p-6">
            <h3 className="text-lg font-semibold text-gray-800 mb-4">Jak zacząć</h3>
            <ol className="space-y-3">
              {[
                'Przejdź do <strong>Projekty</strong> i dodaj projekt, klienta oraz domeny klienta',
                'Przejdź do <strong>Publikuj</strong>, wybierz projekt → klienta → domenę docelową',
                'Wpisz temat artykułu i wybierz swoje domeny PBN',
                'Wygeneruj treść i zdjęcie, a następnie opublikuj na wybranych domenach',
                'Sprawdź wyniki w <strong>Historia</strong>',
              ].map((step, i) => (
                <li key={i} className="flex items-start gap-3">
                  <span className="flex-shrink-0 w-6 h-6 rounded-full bg-blue-600 text-white text-xs flex items-center justify-center font-bold">
                    {i + 1}
                  </span>
                  <span className="text-gray-600 text-sm" dangerouslySetInnerHTML={{ __html: step }} />
                </li>
              ))}
            </ol>
          </div>
        </>
      )}
    </div>
  )
}
