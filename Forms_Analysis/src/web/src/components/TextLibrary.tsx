import { useCallback, useEffect, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { Search, ChevronDown, ChevronRight, X, Loader2 } from 'lucide-react'
import { api } from '../services/api'
import type { ProductDto, TextDocument, TextEntry } from '../services/api'
import { useDebounced, usePagedList } from '../hooks/usePagedList'
import './TextLibrary.css'

const PAGE_SIZE = 50

function TextLibrary() {
  const [params, setParams] = useSearchParams()
  const [products, setProducts] = useState<ProductDto[]>([])
  const [search, setSearch] = useState('')
  const [product, setProduct] = useState('')
  const [lang, setLang] = useState('')
  const [type, setType] = useState('')
  const [minDocs, setMinDocs] = useState(2)
  const [expanded, setExpanded] = useState<string | null>(params.get('hash'))
  const [docs, setDocs] = useState<Record<string, TextDocument[]>>({})
  const debouncedSearch = useDebounced(search)

  useEffect(() => {
    api.products().then(setProducts).catch(() => setProducts([]))
  }, [])

  const fetchPage = useCallback(
    (page: number) => api.texts({ page, pageSize: PAGE_SIZE, search: debouncedSearch, product, lang, type, minDocs }),
    [debouncedSearch, product, lang, type, minDocs],
  )
  const { items, totalCount, isLoading, isLoadingMore, error, sentinelRef } = usePagedList<TextEntry>(fetchPage)

  const toggle = (hash: string) => {
    const next = expanded === hash ? null : hash
    setExpanded(next)
    setParams(next ? { hash: next } : {}, { replace: true })
  }

  useEffect(() => {
    if (expanded && !docs[expanded]) {
      api.textDocuments(expanded)
        .then(d => setDocs(prev => ({ ...prev, [expanded]: d })))
        .catch(() => setDocs(prev => ({ ...prev, [expanded]: [] })))
    }
  }, [expanded, docs])

  const hasFilters = !!(search || product || lang || type || minDocs !== 2)

  return (
    <div className="page">
      <div className="page-header">
        <h1>Text Library</h1>
        <p className="page-subtitle">
          Every distinct paragraph, clause and definition across all forms, ranked by how many forms use exactly the same text.
        </p>
      </div>

      <div className="toolbar">
        <div className="search-input-group">
          <Search size={14} className="search-icon" />
          <input placeholder="Search text… (e.g. towing, cancellation, Lienholder)" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div className="filter-group">
          <select value={product} onChange={e => setProduct(e.target.value)}>
            <option value="">All products</option>
            {products.map(p => <option key={p.code} value={p.code}>{p.code}</option>)}
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        <div className="filter-group">
          <select value={lang} onChange={e => setLang(e.target.value)}>
            <option value="">All languages</option>
            <option value="E">English</option>
            <option value="F">French</option>
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        <div className="filter-group">
          <select value={type} onChange={e => setType(e.target.value)}>
            <option value="">All block types</option>
            <option value="item">Items</option>
            <option value="paragraph">Paragraphs</option>
            <option value="definition">Definitions</option>
            <option value="lead">Leads</option>
            <option value="heading">Headings</option>
            <option value="table">Tables</option>
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        <div className="filter-group">
          <select value={minDocs} onChange={e => setMinDocs(Number(e.target.value))}>
            <option value={1}>Used in any number of forms</option>
            <option value={2}>Shared by 2+ forms</option>
            <option value={10}>Shared by 10+ forms</option>
            <option value={50}>Shared by 50+ forms</option>
            <option value={100}>Shared by 100+ forms</option>
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        {hasFilters && (
          <button className="btn clear" onClick={() => { setSearch(''); setProduct(''); setLang(''); setType(''); setMinDocs(2) }}>
            <X size={14} /> Clear
          </button>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {!isLoading && <div className="result-count">{totalCount.toLocaleString()} distinct texts</div>}

      {isLoading ? (
        <div className="loading"><Loader2 className="spin" size={18} /> Loading texts…</div>
      ) : items.length === 0 ? (
        <div className="empty">No texts match the filters.</div>
      ) : (
        <div className="text-list">
          {items.map(t => {
            const open = expanded === t.textHash
            return (
              <div key={t.textHash} className={`text-card ${open ? 'open' : ''}`}>
                <div className="text-card-head" onClick={() => toggle(t.textHash)}>
                  {open ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
                  <div className="text-body">
                    <p className={open ? '' : 'clamp'}>{t.text}</p>
                    <div className="text-meta">
                      <span className="badge">{t.type}</span>
                      {t.lang && <span className={`badge lang-${t.lang}`}>{t.lang}</span>}
                      {t.productList.map(p => <span key={p} className="badge product">{p}</span>)}
                      <span className="muted-text">{t.length} chars</span>
                    </div>
                  </div>
                  <div className="reuse">
                    <span className="reuse-count">{t.docs}</span>
                    <span className="reuse-label">form{t.docs === 1 ? '' : 's'}</span>
                  </div>
                </div>
                {open && (
                  <div className="text-docs">
                    {!docs[t.textHash] ? (
                      <div className="loading small"><Loader2 className="spin" size={14} /></div>
                    ) : (
                      <div className="doc-chips">
                        {docs[t.textHash].map(d => (
                          <Link key={d.id} to={`/forms/${d.id}`} className="doc-chip" title={d.fileName}>
                            <span className="badge product">{d.product}</span>
                            <span className="form-num">{d.formNo}</span>
                            {d.variant && <span>{d.variant}</span>}
                            <span className="muted-text">{d.lang} {d.revDate?.slice(0, 7)}</span>
                          </Link>
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}
      <div ref={sentinelRef} className="sentinel" />
      {isLoadingMore && <div className="loading"><Loader2 className="spin" size={16} /> Loading more…</div>}
    </div>
  )
}

export default TextLibrary
