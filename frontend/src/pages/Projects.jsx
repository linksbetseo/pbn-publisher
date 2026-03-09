import { useState, useEffect } from 'react'
import { projects as projectsApi, clients as clientsApi } from '../api/client'

export default function Projects() {
  const [projectList, setProjectList] = useState([])
  const [clientList, setClientList] = useState([])
  const [selectedProject, setSelectedProject] = useState(null)
  const [selectedClient, setSelectedClient] = useState(null)
  const [newProject, setNewProject] = useState('')
  const [newClient, setNewClient] = useState('')
  const [newDomain, setNewDomain] = useState('')
  const [loading, setLoading] = useState(false)

  const loadProjects = async () => {
    const res = await projectsApi.list()
    setProjectList(res.data)
  }

  const loadClients = async (projectId) => {
    const res = await clientsApi.list(projectId)
    setClientList(res.data)
    setSelectedClient(null)
  }

  useEffect(() => { loadProjects() }, [])

  const handleSelectProject = (p) => {
    setSelectedProject(p)
    loadClients(p.id)
  }

  const handleAddProject = async () => {
    if (!newProject.trim()) return
    setLoading(true)
    await projectsApi.create(newProject.trim())
    setNewProject('')
    await loadProjects()
    setLoading(false)
  }

  const handleDeleteProject = async (id) => {
    if (!confirm('Usunąć projekt wraz z klientami?')) return
    await projectsApi.delete(id)
    if (selectedProject?.id === id) {
      setSelectedProject(null)
      setClientList([])
    }
    await loadProjects()
  }

  const handleAddClient = async () => {
    if (!newClient.trim() || !selectedProject) return
    setLoading(true)
    await clientsApi.create(selectedProject.id, newClient.trim())
    setNewClient('')
    await loadClients(selectedProject.id)
    setLoading(false)
  }

  const handleDeleteClient = async (id) => {
    if (!confirm('Usunąć klienta?')) return
    await clientsApi.delete(id)
    if (selectedClient?.id === id) setSelectedClient(null)
    await loadClients(selectedProject.id)
  }

  const handleAddDomain = async () => {
    if (!newDomain.trim() || !selectedClient) return
    setLoading(true)
    await clientsApi.addDomain(selectedClient.id, newDomain.trim())
    setNewDomain('')
    await loadClients(selectedProject.id)
    const updated = clientList.find(c => c.id === selectedClient.id)
    if (updated) setSelectedClient(updated)
    setLoading(false)
  }

  const handleDeleteDomain = async (clientId, domainId) => {
    await clientsApi.deleteDomain(clientId, domainId)
    await loadClients(selectedProject.id)
  }

  const currentClient = clientList.find(c => c.id === selectedClient?.id)

  return (
    <div className="p-8">
      <h2 className="text-2xl font-bold text-gray-900 mb-6">Projekty</h2>

      <div className="grid grid-cols-3 gap-6 h-[calc(100vh-160px)]">
        {/* Projects panel */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="p-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-700 mb-3">Projekty</h3>
            <div className="flex gap-2">
              <input
                type="text"
                placeholder="Nazwa projektu..."
                value={newProject}
                onChange={(e) => setNewProject(e.target.value)}
                onKeyDown={(e) => e.key === 'Enter' && handleAddProject()}
                className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <button
                onClick={handleAddProject}
                disabled={loading}
                className="px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
              >+</button>
            </div>
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {projectList.length === 0 && (
              <p className="text-center text-gray-400 text-sm mt-8">Brak projektów</p>
            )}
            {projectList.map((p) => (
              <div
                key={p.id}
                onClick={() => handleSelectProject(p)}
                className={`flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer mb-1 ${
                  selectedProject?.id === p.id
                    ? 'bg-blue-50 border border-blue-200'
                    : 'hover:bg-gray-50'
                }`}
              >
                <div>
                  <p className="text-sm font-medium text-gray-800">{p.name}</p>
                  <p className="text-xs text-gray-400">{p.client_count} klientów</p>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteProject(p.id) }}
                  className="text-red-400 hover:text-red-600 p-1"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Clients panel */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="p-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-700 mb-3">
              Klienci {selectedProject ? `— ${selectedProject.name}` : ''}
            </h3>
            {selectedProject && (
              <div className="flex gap-2">
                <input
                  type="text"
                  placeholder="Nazwa klienta..."
                  value={newClient}
                  onChange={(e) => setNewClient(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddClient()}
                  className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  onClick={handleAddClient}
                  disabled={loading}
                  className="px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
                >+</button>
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {!selectedProject && (
              <p className="text-center text-gray-400 text-sm mt-8">Wybierz projekt</p>
            )}
            {selectedProject && clientList.length === 0 && (
              <p className="text-center text-gray-400 text-sm mt-8">Brak klientów</p>
            )}
            {clientList.map((c) => (
              <div
                key={c.id}
                onClick={() => setSelectedClient(c)}
                className={`flex items-center justify-between px-3 py-2.5 rounded-lg cursor-pointer mb-1 ${
                  selectedClient?.id === c.id
                    ? 'bg-blue-50 border border-blue-200'
                    : 'hover:bg-gray-50'
                }`}
              >
                <div>
                  <p className="text-sm font-medium text-gray-800">{c.name}</p>
                  <p className="text-xs text-gray-400">{c.domains?.length || 0} domen</p>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); handleDeleteClient(c.id) }}
                  className="text-red-400 hover:text-red-600 p-1"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>

        {/* Domains panel */}
        <div className="bg-white rounded-xl border border-gray-200 shadow-sm flex flex-col">
          <div className="p-4 border-b border-gray-100">
            <h3 className="font-semibold text-gray-700 mb-3">
              Domeny klienta {currentClient ? `— ${currentClient.name}` : ''}
            </h3>
            {currentClient && (
              <div className="flex gap-2">
                <input
                  type="text"
                  placeholder="np. example.com"
                  value={newDomain}
                  onChange={(e) => setNewDomain(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleAddDomain()}
                  className="flex-1 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                />
                <button
                  onClick={handleAddDomain}
                  disabled={loading}
                  className="px-3 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
                >+</button>
              </div>
            )}
          </div>
          <div className="flex-1 overflow-y-auto p-2">
            {!currentClient && (
              <p className="text-center text-gray-400 text-sm mt-8">Wybierz klienta</p>
            )}
            {currentClient && (currentClient.domains || []).length === 0 && (
              <p className="text-center text-gray-400 text-sm mt-8">Brak domen</p>
            )}
            {(currentClient?.domains || []).map((d) => (
              <div key={d.id} className="flex items-center justify-between px-3 py-2.5 rounded-lg hover:bg-gray-50 mb-1">
                <span className="text-sm text-gray-700 font-medium">{d.domain}</span>
                <button
                  onClick={() => handleDeleteDomain(currentClient.id, d.id)}
                  className="text-red-400 hover:text-red-600 p-1"
                >
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                  </svg>
                </button>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}
