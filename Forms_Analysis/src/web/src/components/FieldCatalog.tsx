import { useCallback, useEffect, useState } from 'react'
import { Search, ChevronDown, X, Loader2, AlertTriangle } from 'lucide-react'
import { api } from '../services/api'
import type { FieldEntry, ProductDto } from '../services/api'
import { useDebounced, usePagedList } from '../hooks/usePagedList'

const PAGE_SIZE = 100

function FieldCatalog() {
  const [products, setProducts] = useState<ProductDto[]>([])
  const [search, setSearch] = useState('')
  const [product, setProduct] = useState('')
  const debouncedSearch = useDebounced(search)

  useEffect(() => {
    api.products().then(setProducts).catch(() => setProducts([]))
  }, [])

  const fetchPage = useCallback(
    (page: number) => api.fields({ page, pageSize: PAGE_SIZE, search: debouncedSearch, product }),
    [debouncedSearch, product],
  )
  const { items, totalCount, isLoading, isLoadingMore, error, sentinelRef } = usePagedList<FieldEntry>(fetchPage)
  const maxDocs = items.length ? items[0].docs : 1

  return (
    <div className="page">
      <div className="page-header">
        <h1>Page 1 Fields</h1>
        <p className="page-subtitle">Fillable field names used on page 1 across all forms — the building blocks for a generated page 1.</p>
      </div>

      <div className="toolbar">
        <div className="search-input-group">
          <Search size={14} className="search-icon" />
          <input placeholder="Search field name… (e.g. VIN, Dealer, Signature)" value={search} onChange={e => setSearch(e.target.value)} />
        </div>
        <div className="filter-group">
          <select value={product} onChange={e => setProduct(e.target.value)}>
            <option value="">All products</option>
            {products.map(p => <option key={p.code} value={p.code}>{p.code}</option>)}
          </select>
          <ChevronDown size={14} className="select-chevron" />
        </div>
        {(search || product) && (
          <button className="btn clear" onClick={() => { setSearch(''); setProduct('') }}><X size={14} /> Clear</button>
        )}
      </div>

      {error && <div className="error-banner">{error}</div>}
      {!isLoading && <div className="result-count">{totalCount.toLocaleString()} distinct field names</div>}

      {isLoading ? (
        <div className="loading"><Loader2 className="spin" size={18} /> Loading fields…</div>
      ) : items.length === 0 ? (
        <div className="empty">No fields match the filters.</div>
      ) : (
        <div className="table-container">
          <table className="data-table">
            <thead>
              <tr><th>Field name</th><th>Type</th><th>Products</th><th>Used in forms</th></tr>
            </thead>
            <tbody>
              {items.map(f => (
                <tr key={f.name}>
                  <td className="mono">{f.name}</td>
                  <td>
                    <span className="badge">{f.type}</span>
                    {f.mixedTypes && (
                      <span className="badge warning" title="Same name has different field types across forms" style={{ marginLeft: 4 }}>
                        <AlertTriangle size={10} /> mixed
                      </span>
                    )}
                  </td>
                  <td><div className="badge-list">{f.productList.map(p => <span key={p} className="badge product">{p}</span>)}</div></td>
                  <td style={{ minWidth: 220 }}>
                    <div className="bar-cell">
                      <div className="bar" style={{ width: `${(f.docs / maxDocs) * 160}px`, height: 8 }} />
                      <span>{f.docs}</span>
                    </div>
                  </td>
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

export default FieldCatalog
