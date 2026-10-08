// Thin client for the Forms KB API (served by FastAPI, proxied by Vite under /api).

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? ''

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message)
  }
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v !== undefined && v !== '') qs.set(k, String(v))
  }
  const url = `${API_BASE}${path}${qs.toString() ? `?${qs}` : ''}`
  const res = await fetch(url)
  if (!res.ok) {
    let detail = res.statusText
    try {
      detail = (await res.json()).detail ?? detail
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, `${res.status}: ${detail}`)
  }
  return res.json() as Promise<T>
}

export const apiUrl = (path: string) => `${API_BASE}${path}`

// ---------------------------------------------------------------- DTOs

export interface Paged<T> {
  items: T[]
  totalCount: number
  page: number
  pageSize: number
  hasMore: boolean
}

export interface ProductStats {
  product: string
  documents: number
  inScope: number
  extracted: number
  fillable: number
}

export interface StatsDto {
  totals: {
    documents: number
    extracted: number
    blocks: number
    distinctTexts: number
    fields: number
    distinctFields: number
    terms: number
    assets: number
  }
  products: ProductStats[]
  blockTypes: { type: string; count: number }[]
  languages: { lang: string; count: number }[]
}

export interface ProductDto {
  code: string
  forms: number
}

export interface DocumentSummary {
  id: number
  product: string
  fileName: string
  family: string | null
  formNo: string | null
  variant: string | null
  revDate: string | null
  lang: 'E' | 'F' | null
  pageCount: number | null
  fieldCount: number
  isFillable: boolean
  coverage: number | null
  blocks: number
  terms: number
}

export interface RelatedDocument {
  id: number
  fileName: string
  lang: string | null
  revDate: string | null
  variant: string | null
  isExtracted: boolean
}

export interface DocumentDetails extends Omit<DocumentSummary, 'terms' | 'blocks'> {
  prefix: string | null
  pageSize: string | null
  fonts: string | null
  sizeKb: number | null
  notes: string | null
  isExtracted: boolean
  bodyFontSize: number | null
  linePitch: number | null
  indentStep: number | null
  pageBands: number[][] | null
  furniture: string[] | null
  terms: string[]
  related: RelatedDocument[]
}

export interface Run {
  text: string
  b?: boolean
  u?: boolean
  heavy?: boolean
  term?: boolean
}

export type BlockType =
  | 'region' | 'sectionTitle' | 'lead' | 'heading' | 'item' | 'paragraph' | 'definition' | 'table'

export interface Block {
  seq: number
  type: BlockType
  page: number | null
  columns: number | null
  level: number | null
  marker: string | null
  style: string | null
  term: Run[] | null
  runIn: Run[] | null
  runs: Run[] | null
  rows: string[][] | null
  xrefs: string[] | null
  textHash: string | null
}

export interface FormField {
  seq: number
  name: string
  type: string
  x0: number
  y0: number
  x1: number
  y1: number
  value: string | null
  options: string[] | null
  fontSize: number | null
  maxLen: number | null
}

export interface Page1Dto {
  size: { width: number; height: number } | null
  fields: FormField[]
  assets: { sha1: string; ext: string; x0: number; y0: number; x1: number; y1: number }[]
}

export interface TextEntry {
  textHash: string
  docs: number
  products: number
  type: string
  lang: string | null
  length: number
  productList: string[]
  text: string
}

export interface TextDocument {
  id: number
  product: string
  fileName: string
  formNo: string | null
  variant: string | null
  lang: string | null
  revDate: string | null
}

export interface FieldEntry {
  name: string
  docs: number
  products: number
  type: string
  mixedTypes: boolean
  productList: string[]
}

// ---------------------------------------------------------------- AI chat

export interface ChatSource {
  id: string
  documentId: number
  product: string
  formNo: string | null
  variant: string | null
  lang: string | null
  fileName: string
  type: string
  docs: number
  textHash: string
  text: string
}

export interface ChatStatus {
  available: boolean
  model: string
  models: string[]
}

export type ChatEvent =
  | { type: 'sources'; items: ChatSource[] }
  | { type: 'token'; text: string }
  | { type: 'done'; model: string }
  | { type: 'error'; message: string }

/** POST /api/chat and invoke onEvent for each NDJSON line as it streams in. */
export async function streamChat(
  messages: { role: 'user' | 'assistant'; content: string }[],
  documentId: number | null,
  onEvent: (e: ChatEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/chat`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ messages, documentId }),
    signal,
  })
  if (!res.ok || !res.body) throw new ApiError(res.status, `${res.status}: ${res.statusText}`)
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    let nl: number
    while ((nl = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, nl).trim()
      buffer = buffer.slice(nl + 1)
      if (line) onEvent(JSON.parse(line) as ChatEvent)
    }
  }
}

// ---------------------------------------------------------------- endpoints

export const api = {
  chatStatus: () => get<ChatStatus>('/api/chat/status'),
  stats: () => get<StatsDto>('/api/stats'),
  products: () => get<ProductDto[]>('/api/products'),
  documents: (p: {
    page: number
    pageSize: number
    product?: string
    lang?: string
    search?: string
    sortBy?: string
    sortDir?: 'asc' | 'desc'
  }) => get<Paged<DocumentSummary>>('/api/documents', p),
  document: (id: number) => get<DocumentDetails>(`/api/documents/${id}`),
  blocks: (id: number) => get<Block[]>(`/api/documents/${id}/blocks`),
  page1: (id: number) => get<Page1Dto>(`/api/documents/${id}/page1`),
  texts: (p: {
    page: number
    pageSize: number
    search?: string
    product?: string
    lang?: string
    type?: string
    minDocs?: number
  }) => get<Paged<TextEntry>>('/api/texts', p),
  textDocuments: (hash: string) => get<TextDocument[]>(`/api/texts/${hash}/documents`),
  fields: (p: { page: number; pageSize: number; search?: string; product?: string }) =>
    get<Paged<FieldEntry>>('/api/fields', p),
}
