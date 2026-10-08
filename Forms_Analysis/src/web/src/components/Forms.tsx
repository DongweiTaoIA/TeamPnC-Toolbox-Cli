import { useCallback, useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Search, ChevronDown, X, Loader2, ArrowUpDown, ArrowUp, ArrowDown } from 'lucide-react'
import { api } from '../services/api'
import type { DocumentSummary, ProductDto } from '../services/api'
import { useDebounced, usePagedList } from '../hooks/usePagedList'

type SortField = 'fileName' | 'product' | 'formNo' | 'revDate' | 'lang' | 'pageCount' | 'fieldCount' | 'blocks'

const PAGE_SIZE = 50

function Forms() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const [products, setProducts] = useState<ProductDto[]>([])
  const [search, setSearch] = useState(params.get('search') ?? '')
  const product = params.get('product') ?? ''
  const lang = params.get('lang') ?? ''
  const sortBy = (params.get('sortBy') as SortField) ?? 'revDate'
  const sortDir = (params.get('sortDir') as 'asc' | 'desc') ?? 'desc'
  const debouncedSearch = useDebounced(search)

  useEffect(() => {
    api.products().then(setProducts).catch(() => setProducts([]))
  }, [])

  const setParam = (key: string, value: string) => {
    setParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set(key, value)
      else next.delete(key)
      return next
    }, { replace: true })
  }

  useEffect(() => {
    setParam('search', debouncedSearch)
  }, [debouncedSearch])

  const fetchPage = useCallback(
    (page: number) => api.documents({ page, pageSize: PAGE_SIZE, product, lang, search: debouncedSearch, sortBy, sortDir }),
    [product, lang, debouncedSearch, sortBy, sortDir],
  )
  const { items, totalCount, isLoading, isLoadingMore, error, sentinelRef } = usePagedList<DocumentSummary>(fetchPage)

  const toggleSort = (field: SortField) => {
    setParams(prev => {
      const next = new URLSearchParams(prev)
      next.set('sortBy', field)
      next.set('sortDir', sortBy === field && sortDir === 'asc' ? 'desc' : 'asc')
      return next
    }, { replace: true })
  }

  const sortIcon = (field: SortField) => {
    if (sortBy !== field) return <ArrowUpDown size={12} className="sort-icon inactive" />
    return sortDir === 'asc' ? <ArrowUp size={12} className="sort-icon active" /> : <ArrowDown size={12} className="sort-icon active" />
  }

  const th = (field: SortField, label: string, numeric = false) => (
    <th className={`sortable ${numeric ? 'num' : ''}`} onClick={() => toggleSort(field)}>
      {label}{sortIcon(field)}
    </th>
  )

  const hasFilters = !!(product || lang || search)

  return (
    <div className="page">
      <div className="page-header">
        <div className="page-title-row">
          <div>
            <h1>Forms</h1>
            <p className="page-subtitle">Current revisions of each form, extracted into page-1 layout and policy text.</p>
          </div>
        </div>
      </div>

      <div className="toolbar">
        <div className="search-input-group">
          <Search size={14} className="search-icon" />
          <input
            type="text"
            placeholder="Search by file name, form number or variant…"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
        </div>
        <div className="filter-group">
          <select value={product} onChange={e => setParam('product', e.target.value)}>
            <option value="">All products</option>
            {products.map(p => <option key={p.code} value={p.code}>{p.code} ({p.forms})</option>)}
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        <div className="filter-group">
          <select value={lang} onChange={e => setParam('lang', e.target.value)}>
            <option value="">All languages</option>
            <option value="E">English</option>
            <option value="F">French</option>
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        {hasFilters && (
          <button className="btn clear" onClick={() => { setSearch(''); setParams({}, { replace: true }) }}>
            <X size={14} /> Clear
          </button>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {!isLoading && <div className="result-count">{totalCount.toLocaleString()} forms</div>}

      {isLoading ? (
        <div className="loading"><Loader2 className="spin" size={18} /> Loading forms…</div>
      ) : items.length === 0 ? (
        <div className="empty">No forms match the filters.</div>
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr>
                {th('formNo', 'Form')}
                {th('product', 'Product')}
                {th('fileName', 'File')}
                {th('revDate', 'Revision')}
                {th('lang', 'Lang')}
                {th('pageCount', 'Pages', true)}
                {th('fieldCount', 'Fields', true)}
                {th('blocks', 'Text blocks', true)}
                <th className="num">Terms</th>
              </tr>
            </thead>
            <tbody>
              {items.map(d => (
                <tr key={d.id} className="clickable" onClick={() => navigate(`/forms/${d.id}`)}>
                  <td style={{ whiteSpace: 'nowrap' }}>
                    <span className="form-num">{d.formNo}</span>
                    {d.variant && <span className="badge" style={{ marginLeft: 6 }}>{d.variant}</span>}
                  </td>
                  <td><span className="badge product">{d.product}</span></td>
                  <td className="mono" style={{ fontSize: 12 }}>{d.fileName}</td>
                  <td>{d.revDate?.slice(0, 7) ?? '—'}</td>
                  <td>{d.lang && <span className={`badge lang-${d.lang}`}>{d.lang}</span>}</td>
                  <td className="num">{d.pageCount}</td>
                  <td className="num">{d.fieldCount}</td>
                  <td className="num">{d.blocks || <span className="badge warning">page 1 only</span>}</td>
                  <td className="num">{d.terms}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <div ref={sentinelRef} className="sentinel" />
      {isLoadingMore && <div className="loading"><Loader2 className="spin" size={16} /> Loading more…</div>}
    </div>
  )
}

export default Forms
