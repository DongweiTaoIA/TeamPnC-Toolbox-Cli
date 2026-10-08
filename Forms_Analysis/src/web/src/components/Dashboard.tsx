import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import { FileText, Library, TextCursorInput, Layers, BookMarked, Image, Loader2 } from 'lucide-react'
import { api } from '../services/api'
import type { StatsDto } from '../services/api'
import './Dashboard.css'

const fmt = (n: number) => n.toLocaleString('en-CA')

function Dashboard() {
  const [stats, setStats] = useState<StatsDto | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    api.stats().then(setStats).catch(e => setError(String(e.message ?? e)))
  }, [])

  if (error) return <div className="page"><div className="error-banner">{error}</div></div>
  if (!stats) return <div className="page"><div className="loading"><Loader2 className="spin" size={18} /> Loading…</div></div>

  const t = stats.totals
  const maxDocs = Math.max(...stats.products.map(p => p.documents))
  const maxBlock = Math.max(...stats.blockTypes.map(b => b.count))

  const metrics = [
    { label: 'Forms extracted', value: t.extracted, sub: `of ${fmt(t.documents)} PDFs`, icon: <FileText size={18} />, to: '/forms' },
    { label: 'Content blocks', value: t.blocks, sub: `${fmt(t.distinctTexts)} distinct texts`, icon: <Layers size={18} />, to: '/texts' },
    { label: 'Page 1 fields', value: t.fields, sub: `${fmt(t.distinctFields)} distinct names`, icon: <TextCursorInput size={18} />, to: '/fields' },
    { label: 'Defined terms', value: t.terms, sub: 'bold terms in Definitions', icon: <BookMarked size={18} />, to: '/forms' },
    { label: 'Images / logos', value: t.assets, sub: 'distinct page-1 images', icon: <Image size={18} />, to: '/forms' },
  ]

  return (
    <div className="page dashboard">
      <div className="page-header">
        <h1>Forms Knowledge Base</h1>
        <p className="page-subtitle">Contract PDFs broken down into page-1 layouts and policy text, ready for reuse.</p>
      </div>

      <div className="metric-grid">
        {metrics.map(m => (
          <Link key={m.label} to={m.to} className="metric-card">
            <div className="metric-icon">{m.icon}</div>
            <div className="metric-value">{fmt(m.value)}</div>
            <div className="metric-label">{m.label}</div>
            <div className="metric-sub">{m.sub}</div>
          </Link>
        ))}
      </div>

      <div className="dashboard-grid">
        <div className="card">
          <h3>Forms by product</h3>
          <table className="data-table compact">
            <thead>
              <tr><th>Product</th><th className="num">PDFs</th><th className="num">In scope</th><th>Extracted</th></tr>
            </thead>
            <tbody>
              {stats.products.map(p => (
                <tr key={p.product}>
                  <td>
                    {p.extracted > 0
                      ? <Link to={`/forms?product=${p.product}`} className="badge product">{p.product}</Link>
                      : <span className="badge">{p.product}</span>}
                  </td>
                  <td className="num">{fmt(p.documents)}</td>
                  <td className="num">{fmt(p.inScope)}</td>
                  <td>
                    <div className="bar-cell">
                      <div className="bar" style={{ width: `${(p.extracted / maxDocs) * 100}%` }} />
                      <span>{fmt(p.extracted)}</span>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        <div className="card">
          <h3>Policy text structure</h3>
          {stats.blockTypes.map(b => (
            <div key={b.type} className="bar-row">
              <span className="bar-label">{b.type}</span>
              <div className="bar-track"><div className="bar" style={{ width: `${(b.count / maxBlock) * 100}%` }} /></div>
              <span className="bar-value">{fmt(b.count)}</span>
            </div>
          ))}
          <h3 className="mt">Languages</h3>
          <div className="badge-list">
            {stats.languages.map(l => (
              <span key={l.lang} className={`badge lang-${l.lang}`}>
                {l.lang === 'E' ? 'English' : l.lang === 'F' ? 'French' : 'Unknown'} · {fmt(l.count)}
              </span>
            ))}
          </div>
          <Link to="/texts" className="btn primary mt">
            <Library size={14} /> Explore reused text
          </Link>
        </div>
      </div>
    </div>
  )
}

export default Dashboard
