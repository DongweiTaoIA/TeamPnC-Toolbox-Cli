import { useEffect, useState } from 'react'
import { Routes, Route, Navigate, matchPath, useLocation } from 'react-router-dom'
import Sidebar from './components/Sidebar'
import Dashboard from './components/Dashboard'
import Forms from './components/Forms'
import FormDetail from './components/FormDetail'
import TextLibrary from './components/TextLibrary'
import FieldCatalog from './components/FieldCatalog'
import ChatArea from './components/ChatArea'
import type { ChatContext } from './components/ChatArea'
import { api } from './services/api'
import './App.css'

function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true)
  const [chatContext, setChatContext] = useState<ChatContext | null>(null)
  const location = useLocation()
  const formMatch = matchPath('/forms/:id', location.pathname)
  const formId = formMatch ? Number(formMatch.params.id) : null

  // The chat prefers the form currently on screen.
  useEffect(() => {
    if (!formId) {
      setChatContext(null)
      return
    }
    let cancelled = false
    api.document(formId)
      .then(d => {
        if (!cancelled) {
          setChatContext({ documentId: d.id, label: `${d.product} ${d.formNo ?? ''}${d.variant ?? ''} ${d.lang ?? ''}`.trim() })
        }
      })
      .catch(() => {
        if (!cancelled) setChatContext(null)
      })
    return () => {
      cancelled = true
    }
  }, [formId])

  return (
    <div className="app">
      <Sidebar isOpen={sidebarOpen} onToggle={() => setSidebarOpen(o => !o)} />
      <main className="main-content">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/forms" element={<Forms />} />
          <Route path="/forms/:id" element={<FormDetail />} />
          <Route path="/texts" element={<TextLibrary />} />
          <Route path="/fields" element={<FieldCatalog />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
      <ChatArea context={chatContext} />
    </div>
  )
}

export default App
