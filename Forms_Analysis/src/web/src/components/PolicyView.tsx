import type { ReactNode } from 'react'
import type { Block, Run } from '../services/api'
import './PolicyView.css'

interface PolicyViewProps {
  blocks: Block[]
  highlightTerms: boolean
  selectedSeq: number | null
  onSelect: (block: Block) => void
}

function renderRuns(runs: Run[] | null, keyPrefix: string): ReactNode {
  return (runs ?? []).map((r, i) => {
    let node: ReactNode = r.text
    if (r.u) node = <u>{node}</u>
    if (r.term) node = <strong className="pv-term">{node}</strong>
    else if (r.b) node = <strong>{node}</strong>
    // Runs are stored trimmed at style boundaries; restore the space between words.
    const prev = runs![i - 1]
    const needsSpace = i > 0 && prev && !/[\s(“"'’/–-]$/.test(prev.text) && !/^[\s.,;:)”"'’/–-]/.test(r.text)
    return <span key={`${keyPrefix}-${i}`}>{needsSpace ? ' ' : ''}{node}</span>
  })
}

/** Groups blocks into column regions and renders them like the printed policy pages. */
function PolicyView({ blocks, highlightTerms, selectedSeq, onSelect }: PolicyViewProps) {
  const regions: { columns: number; page: number | null; blocks: Block[] }[] = []
  for (const b of blocks) {
    if (b.type === 'region') {
      regions.push({ columns: b.columns ?? 1, page: b.page, blocks: [] })
    } else {
      if (regions.length === 0) regions.push({ columns: 1, page: b.page, blocks: [] })
      regions[regions.length - 1].blocks.push(b)
    }
  }

  const blockEl = (b: Block) => {
    const cls = `pv-block pv-${b.type} ${selectedSeq === b.seq ? 'selected' : ''}`
    const indent = { marginLeft: `${(b.level ?? 0) * 13.5}pt` }
    const click = () => onSelect(b)
    const key = `b-${b.seq}`
    switch (b.type) {
      case 'sectionTitle':
        return <h2 key={key} className={cls} onClick={click}>{renderRuns(b.runs, key)}</h2>
      case 'lead': {
        const allBold = (b.runs ?? []).every(r => r.b || !r.text.trim())
        return <p key={key} className={`${cls} ${allBold ? 'centered' : ''}`} onClick={click}>{renderRuns(b.runs, key)}</p>
      }
      case 'heading':
        return (
          <h3 key={key} className={cls} style={indent} onClick={click}>
            {b.marker && <span className="pv-marker">{b.marker}</span>}
            {renderRuns(b.runs, key)}
          </h3>
        )
      case 'definition':
        return (
          <p key={key} className={cls} onClick={click}>
            <strong className="pv-term">{renderRuns(b.term, `${key}t`)}</strong> – {renderRuns(b.runs, key)}
          </p>
        )
      case 'table':
        return (
          <table key={key} className={cls} style={indent} onClick={click}>
            <tbody>
              {(b.rows ?? []).map((row, i) => (
                <tr key={i}>{row.map((c, j) => i === 0 ? <th key={j}>{c}</th> : <td key={j}>{c}</td>)}</tr>
              ))}
            </tbody>
          </table>
        )
      default:
        return (
          <div key={key} className={cls} style={indent} onClick={click}>
            {b.marker && <span className="pv-marker">{b.marker}</span>}
            <span className="pv-text">
              {b.runIn && <strong>{renderRuns(b.runIn, `${key}r`)}</strong>}
              {b.runIn && ' '}
              {renderRuns(b.runs, key)}
            </span>
          </div>
        )
    }
  }

  return (
    <div className={`policy-view ${highlightTerms ? 'highlight-terms' : ''}`}>
      {regions.map((r, i) => (
        <section
          key={i}
          className={`pv-region cols-${r.columns}`}
          style={{ columnCount: r.columns }}
          data-page={r.page ?? undefined}
        >
          {r.blocks.map(blockEl)}
        </section>
      ))}
    </div>
  )
}

export default PolicyView
