import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import Dashboard from './pages/Dashboard'
import Projects from './pages/Projects'
import Publisher from './pages/Publisher'
import History from './pages/History'
import TopicalMap from './pages/TopicalMap'
import ContentWriter from './pages/ContentWriter'
import Autopilot from './pages/Autopilot'
import DomainHealth from './pages/DomainHealth'

export default function App() {
  return (
    <BrowserRouter>
      <div className="flex h-screen overflow-hidden bg-gray-50">
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
          </Routes>
        </main>
      </div>
    </BrowserRouter>
  )
}
