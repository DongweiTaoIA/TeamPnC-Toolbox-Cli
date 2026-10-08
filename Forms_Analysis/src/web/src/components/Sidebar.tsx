import { useState } from 'react'
import { NavLink } from 'react-router-dom'
import {
  ChevronLeft, ChevronDown, LayoutDashboard, FileText, Library, TextCursorInput,
  BookOpen, Sparkles, PenSquare,
} from 'lucide-react'
import './Sidebar.css'

interface NavItem {
  to: string
  label: string
  icon: React.ReactNode
  disabled?: boolean
}

interface NavSection {
  id: string
  label: string
  icon: React.ReactNode
  badge?: string
  items: NavItem[]
}

const navSections: NavSection[] = [
  {
    id: 'knowledge',
    label: 'Knowledge Base',
    icon: <BookOpen size={14} />,
    items: [
      { to: '/forms', label: 'Forms', icon: <FileText size={16} /> },
      { to: '/texts', label: 'Text Library', icon: <Library size={16} /> },
      { to: '/fields', label: 'Page 1 Fields', icon: <TextCursorInput size={16} /> },
    ],
  },
  {
    id: 'authoring',
    label: 'Authoring',
    icon: <Sparkles size={14} />,
    badge: 'Soon',
    items: [
      { to: '/drafts', label: 'AI Drafts', icon: <PenSquare size={16} />, disabled: true },
    ],
  },
]

interface SidebarProps {
  isOpen: boolean
  onToggle: () => void
}

function Sidebar({ isOpen, onToggle }: SidebarProps) {
  const [collapsedSections, setCollapsedSections] = useState<Set<string>>(new Set())

  const toggleSection = (id: string) => {
    setCollapsedSections(prev => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <aside className={`sidebar ${isOpen ? 'open' : 'collapsed'}`}>
      <div className="sidebar-header">
        {isOpen && (
          <div className="logo">
            <span className="logo-text">Forms Studio</span>
          </div>
        )}
        <button className="collapse-btn" onClick={onToggle} title={isOpen ? 'Collapse sidebar' : 'Expand sidebar'}>
          {isOpen ? <ChevronLeft size={20} /> : <ChevronDown size={20} style={{ transform: 'rotate(-90deg)' }} />}
        </button>
      </div>

      <NavLink to="/" end className={({ isActive }) => `home-btn ${isActive ? 'active' : ''}`} title="Dashboard">
        <LayoutDashboard size={20} />
        {isOpen && <span>Dashboard</span>}
      </NavLink>

      <nav className="nav-list">
        {navSections.map(section => {
          const isCollapsed = collapsedSections.has(section.id)
          return (
            <div key={section.id} className="nav-section">
              {isOpen && (
                <div className="nav-section-header" onClick={() => toggleSection(section.id)}>
                  <span className="nav-section-label">
                    <span className="section-icon">{section.icon}</span>
                    {section.label}
                  </span>
                  <div className="nav-section-meta">
                    {section.badge && <span className="nav-section-badge">{section.badge}</span>}
                    <ChevronDown size={14} className={`nav-section-chevron ${isCollapsed ? 'collapsed' : ''}`} />
                  </div>
                </div>
              )}
              {(isOpen ? !isCollapsed : true) && (
                <div className="nav-section-items">
                  {section.items.map(item =>
                    item.disabled ? (
                      <span key={item.to} className="nav-item disabled" title={`${item.label} (coming soon)`}>
                        {item.icon}
                        {isOpen && item.label}
                      </span>
                    ) : (
                      <NavLink
                        key={item.to}
                        to={item.to}
                        className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}
                        title={item.label}
                      >
                        {item.icon}
                        {isOpen && item.label}
                      </NavLink>
                    ),
                  )}
                </div>
              )}
            </div>
          )
        })}
      </nav>

      {isOpen && <div className="sidebar-footer">Local demo · FormsKB</div>}
    </aside>
  )
}

export default Sidebar
