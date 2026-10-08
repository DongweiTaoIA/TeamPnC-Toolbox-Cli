import { useMemo, useState } from 'react'
import { apiUrl } from '../services/api'
import type { Page1Dto } from '../services/api'
import './Page1View.css'

interface Page1ViewProps {
  docId: number
  data: Page1Dto
}

/** Rendered page 1 with the fillable fields drawn on top, plus the field list. */
function Page1View({ docId, data }: Page1ViewProps) {
  const [hovered, setHovered] = useState<number | null>(null)
  const [filter, setFilter] = useState('')
  const size = data.size ?? { width: 612, height: 792 }

  const fields = useMemo(
    () => data.fields.filter(f => f.name.toLowerCase().includes(filter.toLowerCase())),
    [data.fields, filter],
  )

  const pct = (v: number, total: number) => `${(v / total) * 100}%`

  return (
    <div className="page1-view">
      <div className="page1-canvas" style={{ aspectRatio: `${size.width} / ${size.height}` }}>
        <img src={apiUrl(`/api/documents/${docId}/pages/1.png?zoom=2`)} alt="Page 1" />
        {fields.map(f => (
          <div
            key={f.seq}
            className={`field-box type-${f.type.toLowerCase()} ${hovered === f.seq ? 'hovered' : ''}`}
            style={{
              left: pct(f.x0, size.width),
              top: pct(f.y0, size.height),
              width: pct(f.x1 - f.x0, size.width),
              height: pct(f.y1 - f.y0, size.height),
            }}
            title={`${f.name} (${f.type})`}
            onMouseEnter={() => setHovered(f.seq)}
            onMouseLeave={() => setHovered(null)}
          />
        ))}
      </div>

      <div className="page1-fields card">
        <h3>{data.fields.length} fillable fields</h3>
        <input
          className="field-filter"
          placeholder="Filter fields…"
          value={filter}
          onChange={e => setFilter(e.target.value)}
        />
        <div className="field-list">
          <table className="data-table compact">
            <thead><tr><th>Name</th><th>Type</th><th className="num">Size</th></tr></thead>
            <tbody>
              {fields.map(f => (
                <tr
                  key={f.seq}
                  className={`clickable ${hovered === f.seq ? 'selected' : ''}`}
                  onMouseEnter={() => setHovered(f.seq)}
                  onMouseLeave={() => setHovered(null)}
                >
                  <td className="mono" style={{ fontSize: 12 }}>{f.name}</td>
                  <td><span className="badge">{f.type}</span></td>
                  <td className="num">{f.fontSize ? `${f.fontSize}pt` : 'auto'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  )
}

export default Page1View
