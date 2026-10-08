import { useEffect, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  ArrowLeft, FileText, LayoutTemplate, Info, FileSearch, Loader2, Highlighter, ZoomIn, ZoomOut, X,
} from 'lucide-react'
import { api, apiUrl } from '../services/api'
import type { Block, DocumentDetails, Page1Dto, TextDocument } from '../services/api'
import PolicyView from './PolicyView'
import Page1View from './Page1View'
import './FormDetail.css'

type Tab = 'policy' | 'page1' | 'pdf' | 'info'

function FormDetail() {
  const { id } = useParams()
  const docId = Number(id)
  const navigate = useNavigate()
  const [doc, setDoc] = useState<DocumentDetails | null>(null)
  const [blocks, setBlocks] = useState<Block[] | null>(null)
  const [page1, setPage1] = useState<Page1Dto | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [tab, setTab] = useState<Tab>('policy')
  const [highlightTerms, setHighlightTerms] = useState(true)
  const [zoom, setZoom] = useState(1.2)
  const [selected, setSelected] = useState<Block | null>(null)
  const [usage, setUsage] = useState<TextDocument[] | null>(null)

  useEffect(() => {
    setDoc(null)
    setBlocks(null)
    setPage1(null)
    setSelected(null)
    setError(null)
    Promise.all([api.document(docId), api.blocks(docId), api.page1(docId)])
      .then(([d, b, p]) => {
        setDoc(d)
        setBlocks(b)
        setPage1(p)
        setTab(b.length > 0 ? 'policy' : 'page1')
      })
      .catch(e => setError(String(e.message ?? e)))
  }, [docId])

  useEffect(() => {
    setUsage(null)
    if (selected?.textHash) api.textDocuments(selected.textHash).then(setUsage).catch(() => setUsage([]))
  }, [selected])

  if (error) return <div className="page"><div className="error-banner">{error}</div></div>
  if (!doc || !blocks || !page1) {
    return <div className="page"><div className="loading"><Loader2 className="spin" size={18} /> Loading form…</div></div>
  }

  const contentBlocks = blocks.filter(b => b.type !== 'region')

  return (
    <div className="page form-detail">
      <button className="back-btn" onClick={() => navigate(-1)}><ArrowLeft size={14} /> Back</button>

      <div className="page-header">
        <div className="page-title-row">
          <div>
            <h1>
              <span className="form-num">{doc.formNo}</span>
              {doc.variant && <span className="badge" style={{ marginLeft: 8 }}>{doc.variant}</span>}
              <span className="badge product" style={{ marginLeft: 8 }}>{doc.product}</span>
              {doc.lang && <span className={`badge lang-${doc.lang}`} style={{ marginLeft: 6 }}>{doc.lang === 'E' ? 'English' : 'French'}</span>}
            </h1>
            <p className="page-subtitle">
              {doc.fileName} · revision {doc.revDate?.slice(0, 7) ?? '—'} · {doc.pageCount} pages · {doc.fieldCount} fields ·{' '}
              {contentBlocks.length} text blocks · {doc.terms.length} defined terms
            </p>
          </div>
          <a className="btn" href={apiUrl(`/api/documents/${doc.id}/pdf`)} target="_blank" rel="noreferrer">
            <FileSearch size={14} /> Open PDF
          </a>
        </div>
        {doc.related.length > 0 && (
          <div className="related">
            <span className="related-label">Other versions:</span>
            {doc.related.slice(0, 12).map(r => (
              r.isExtracted ? (
                <Link key={r.id} to={`/forms/${r.id}`} className="badge related-link" title={r.fileName}>
                  {r.revDate?.slice(0, 7)} {r.variant} {r.lang}
                </Link>
              ) : (
                <span key={r.id} className="badge muted" title={`${r.fileName} (not extracted)`}>
                  {r.revDate?.slice(0, 7)} {r.variant} {r.lang}
                </span>
              )
            ))}
          </div>
        )}
      </div>

      <div className="tabs">
        <button className={`tab ${tab === 'policy' ? 'active' : ''}`} onClick={() => setTab('policy')} disabled={!contentBlocks.length}>
          <FileText size={14} /> Policy text
        </button>
        <button className={`tab ${tab === 'page1' ? 'active' : ''}`} onClick={() => setTab('page1')} disabled={!page1.fields.length}>
          <LayoutTemplate size={14} /> Page 1
        </button>
        <button className={`tab ${tab === 'pdf' ? 'active' : ''}`} onClick={() => setTab('pdf')}>
          <FileSearch size={14} /> Original PDF
        </button>
        <button className={`tab ${tab === 'info' ? 'active' : ''}`} onClick={() => setTab('info')}>
          <Info size={14} /> Details
        </button>
      </div>

      {tab === 'policy' && (
        <div className="policy-layout">
          <div className="policy-main">
            <div className="policy-toolbar">
              <button className={`btn ${highlightTerms ? 'toggled' : ''}`} onClick={() => setHighlightTerms(h => !h)}>
                <Highlighter size={14} /> Defined terms
              </button>
              <button className="btn" onClick={() => setZoom(z => Math.max(0.8, +(z - 0.2).toFixed(1)))}><ZoomOut size={14} /></button>
              <span className="zoom-label">{Math.round(zoom * 100)}%</span>
              <button className="btn" onClick={() => setZoom(z => Math.min(2.4, +(z + 0.2).toFixed(1)))}><ZoomIn size={14} /></button>
              <span className="hint">Click a block to see where the same text is used.</span>
            </div>
            <div className="policy-scroll" style={{ zoom }}>
              <PolicyView blocks={blocks} highlightTerms={highlightTerms} selectedSeq={selected?.seq ?? null} onSelect={setSelected} />
            </div>
          </div>

          {selected && (
            <aside className="block-panel card">
              <div className="block-panel-head">
                <h3>Block #{selected.seq}</h3>
                <button className="icon-btn" onClick={() => setSelected(null)} title="Close"><X size={14} /></button>
              </div>
              <dl className="kv">
                <dt>Type</dt><dd>{selected.type}{selected.marker ? ` · ${selected.marker}` : ''}{selected.style ? ` (${selected.style})` : ''}</dd>
                <dt>Page</dt><dd>{selected.page}</dd>
                {selected.xrefs && <><dt>References</dt><dd>{selected.xrefs.join(', ')}</dd></>}
              </dl>
              <h3 className="mt">Same text used in</h3>
              {usage === null ? (
                <div className="loading small"><Loader2 className="spin" size={14} /></div>
              ) : (
                <>
                  <p className="usage-count">{usage.length} form{usage.length === 1 ? '' : 's'}</p>
                  <ul className="usage-list">
                    {usage.slice(0, 50).map(u => (
                      <li key={u.id}>
                        <Link to={`/forms/${u.id}`}>
                          <span className="badge product">{u.product}</span> <span className="form-num">{u.formNo}</span>{' '}
                          {u.variant} {u.lang} <span className="muted-text">{u.revDate?.slice(0, 7)}</span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                  {selected.textHash && (
                    <Link className="btn mt" to={`/texts?hash=${selected.textHash}`}>Open in Text Library</Link>
                  )}
                </>
              )}
            </aside>
          )}
        </div>
      )}

      {tab === 'page1' && <Page1View docId={doc.id} data={page1} />}

      {tab === 'pdf' && (
        <iframe className="pdf-frame" title="Original PDF" src={apiUrl(`/api/documents/${doc.id}/pdf`)} />
      )}

      {tab === 'info' && (
        <div className="info-grid">
          <div className="card">
            <h3>Form</h3>
            <dl className="kv">
              <dt>File</dt><dd className="mono">{doc.fileName}</dd>
              <dt>Family</dt><dd>{doc.family ?? '—'}</dd>
              <dt>Form number</dt><dd>{doc.formNo}</dd>
              <dt>Variant</dt><dd>{doc.variant || '—'}</dd>
              <dt>Revision</dt><dd>{doc.revDate ?? '—'}</dd>
              <dt>Page size</dt><dd>{doc.pageSize}</dd>
              <dt>File size</dt><dd>{doc.sizeKb} KB</dd>
              {doc.notes && <><dt>Notes</dt><dd>{doc.notes}</dd></>}
            </dl>
          </div>
          <div className="card">
            <h3>Layout &amp; style</h3>
            <dl className="kv">
              <dt>Body font size</dt><dd>{doc.bodyFontSize ?? '—'} pt</dd>
              <dt>Line pitch</dt><dd>{doc.linePitch ?? '—'} pt</dd>
              <dt>Indent step</dt><dd>{doc.indentStep ?? '—'} pt</dd>
              <dt>Text coverage</dt><dd>{doc.coverage ? `${(doc.coverage * 100).toFixed(2)}%` : '—'}</dd>
              <dt>Columns by page</dt>
              <dd>{doc.pageBands?.map((b, i) => <div key={i}>p{i + (page1.fields.length ? 2 : 1)}: {b.join(' → ')}</div>) ?? '—'}</dd>
              <dt>Fonts</dt><dd className="fonts">{doc.fonts?.split(';').join(', ')}</dd>
              <dt>Footer text</dt><dd>{doc.furniture?.join(' · ') || '—'}</dd>
            </dl>
          </div>
          <div className="card">
            <h3>Defined terms ({doc.terms.length})</h3>
            <div className="badge-list">
              {doc.terms.map(t => <span key={t} className="badge">{t}</span>)}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

export default FormDetail
