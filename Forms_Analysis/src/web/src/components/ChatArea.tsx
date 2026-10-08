import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Send, Sparkles, MessageSquare, MessageSquarePlus, ChevronRight, RefreshCw, Loader2, Square, FileText, AlertTriangle,
} from 'lucide-react'
import { api, streamChat } from '../services/api'
import type { ChatSource, ChatStatus } from '../services/api'
import './ChatArea.css'

interface Message {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: Date
  sources?: ChatSource[]
  isStreaming?: boolean
  isError?: boolean
}

export interface ChatContext {
  documentId: number
  label: string
}

interface ChatAreaProps {
  context: ChatContext | null
}

const WIDTH_KEY = 'chatArea.width'
const OPEN_KEY = 'chatArea.open'
const MIN_W = 320
const MAX_W = 900
const DEFAULT_W = 400

const GENERAL_PROMPTS = [
  'Which forms cover towing and what are the limits?',
  'How is a cancellation refund calculated?',
  'What does "Mechanical Breakdown" mean?',
]
const FORM_PROMPTS = [
  'Summarize the exclusions of this form',
  'What are the deductible rules?',
  'Which benefits are included without deductible?',
]

let nextId = 0
const newId = () => `m${Date.now()}-${nextId++}`

/** Inline markdown-lite: **bold** and [S1] source tags, rendered as React nodes (no raw HTML). */
function renderInline(text: string, sources: ChatSource[] | undefined, key: string): ReactNode[] {
  const parts = text.split(/(\*\*[^*]+\*\*|\[S\d+\])/g)
  return parts.map((p, i) => {
    const k = `${key}-${i}`
    if (p.startsWith('**') && p.endsWith('**') && p.length > 4) return <strong key={k}>{p.slice(2, -2)}</strong>
    const m = /^\[(S\d+)\]$/.exec(p)
    if (m) {
      const src = sources?.find(s => s.id === m[1])
      return src ? (
        <Link key={k} to={`/forms/${src.documentId}`} className="cite" title={`${src.product} ${src.formNo ?? ''} ${src.lang ?? ''}: ${src.text}`}>
          {m[1]}
        </Link>
      ) : <span key={k} className="cite muted">{m[1]}</span>
    }
    return <span key={k}>{p}</span>
  })
}

function renderMessage(content: string, sources?: ChatSource[]): ReactNode {
  const lines = content.split('\n')
  const out: ReactNode[] = []
  let bullets: ReactNode[] = []
  const flush = (i: number) => {
    if (bullets.length) out.push(<ul key={`ul-${i}`}>{bullets}</ul>)
    bullets = []
  }
  lines.forEach((line, i) => {
    const b = /^\s*(?:[-*•]|\d+\.)\s+(.*)$/.exec(line)
    if (b) {
      bullets.push(<li key={`li-${i}`}>{renderInline(b[1], sources, `l${i}`)}</li>)
      return
    }
    flush(i)
    if (line.trim()) out.push(<p key={`p-${i}`}>{renderInline(line, sources, `p${i}`)}</p>)
  })
  flush(lines.length)
  return out
}

function readNumber(key: string, fallback: number) {
  const v = Number(window.localStorage.getItem(key))
  return Number.isFinite(v) && v > 0 ? Math.min(MAX_W, Math.max(MIN_W, v)) : fallback
}

function ChatArea({ context }: ChatAreaProps) {
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isOpen, setIsOpen] = useState(() => window.localStorage.getItem(OPEN_KEY) !== '0')
  const [width, setWidth] = useState(() => readNumber(WIDTH_KEY, DEFAULT_W))
  const [status, setStatus] = useState<ChatStatus | null>(null)
  const [isBusy, setIsBusy] = useState(false)
  const abortRef = useRef<AbortController | null>(null)
  const endRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    api.chatStatus().then(setStatus).catch(() => setStatus({ available: false, model: '', models: [] }))
  }, [])

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  useEffect(() => {
    window.localStorage.setItem(WIDTH_KEY, String(width))
  }, [width])

  useEffect(() => {
    window.localStorage.setItem(OPEN_KEY, isOpen ? '1' : '0')
  }, [isOpen])

  const startResize = (e: React.MouseEvent) => {
    e.preventDefault()
    const startX = e.clientX
    const startW = width
    document.body.style.cursor = 'col-resize'
    document.body.style.userSelect = 'none'
    const move = (ev: MouseEvent) => setWidth(Math.min(MAX_W, Math.max(MIN_W, startW + startX - ev.clientX)))
    const up = () => {
      document.body.style.cursor = ''
      document.body.style.userSelect = ''
      window.removeEventListener('mousemove', move)
      window.removeEventListener('mouseup', up)
    }
    window.addEventListener('mousemove', move)
    window.addEventListener('mouseup', up)
  }

  const send = async (text: string, history: Message[] = messages) => {
    const question = text.trim()
    if (!question || isBusy) return
    const user: Message = { id: newId(), role: 'user', content: question, timestamp: new Date() }
    const reply: Message = { id: newId(), role: 'assistant', content: '', timestamp: new Date(), isStreaming: true }
    const convo = [...history, user]
    setMessages([...convo, reply])
    setInput('')
    if (textareaRef.current) textareaRef.current.style.height = 'auto'
    setIsBusy(true)

    const update = (fn: (m: Message) => Message) =>
      setMessages(prev => prev.map(m => (m.id === reply.id ? fn(m) : m)))
    const controller = new AbortController()
    abortRef.current = controller
    try {
      await streamChat(
        convo.filter(m => !m.isError).map(m => ({ role: m.role, content: m.content })),
        context?.documentId ?? null,
        evt => {
          if (evt.type === 'sources') update(m => ({ ...m, sources: evt.items }))
          else if (evt.type === 'token') update(m => ({ ...m, content: m.content + evt.text }))
          else if (evt.type === 'error') update(m => ({ ...m, content: evt.message, isError: true }))
        },
        controller.signal,
      )
    } catch (err) {
      if (!controller.signal.aborted) {
        update(m => ({ ...m, content: err instanceof Error ? err.message : String(err), isError: true }))
      }
    } finally {
      update(m => ({ ...m, isStreaming: false, content: m.content || (controller.signal.aborted ? '(stopped)' : m.content) }))
      setIsBusy(false)
      abortRef.current = null
    }
  }

  const retry = (index: number) => {
    const lastUser = [...messages.slice(0, index)].reverse().find(m => m.role === 'user')
    if (!lastUser) return
    const cut = messages.indexOf(lastUser)
    send(lastUser.content, messages.slice(0, cut))
  }

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send(input)
    }
  }

  if (!isOpen) {
    return (
      <div className="chat-area collapsed">
        <button className="chat-expand-btn" onClick={() => setIsOpen(true)} title="Open AI assistant">
          <MessageSquare size={20} />
        </button>
      </div>
    )
  }

  const prompts = context ? FORM_PROMPTS : GENERAL_PROMPTS

  return (
    <div className="chat-area" style={{ width, minWidth: width, maxWidth: width }}>
      <div className="chat-resize-handle" onMouseDown={startResize} title="Drag to resize" role="separator" aria-orientation="vertical" />

      <div className="chat-header">
        <div className="chat-title">
          <Sparkles size={20} className="sparkle-icon" />
          Forms Assistant
        </div>
        <div className="chat-header-right">
          <button className="chat-icon-btn" onClick={() => { abortRef.current?.abort(); setMessages([]) }} title="New chat" disabled={!messages.length}>
            <MessageSquarePlus size={18} />
          </button>
          <button className="chat-icon-btn" onClick={() => setIsOpen(false)} title="Collapse">
            <ChevronRight size={20} />
          </button>
        </div>
      </div>

      <div className="chat-status-bar">
        {status === null ? (
          <span className="muted">Checking model…</span>
        ) : status.available ? (
          <span className="badge success">{status.model} · local</span>
        ) : (
          <span className="badge warning" title={`Installed: ${status.models.join(', ') || 'none'}`}>
            <AlertTriangle size={10} /> model {status.model || 'unknown'} not available
          </span>
        )}
        {context && (
          <span className="badge context-badge" title="Answers prefer this form">
            <FileText size={10} /> {context.label}
          </span>
        )}
      </div>

      <div className="messages-container">
        {messages.length === 0 ? (
          <div className="welcome-screen">
            <div className="welcome-icon"><Sparkles size={34} /></div>
            <h2>Ask the forms knowledge base</h2>
            <p>
              Answers come from the extracted policy text, with sources you can open.
              {context ? ` Questions prefer the form you are viewing (${context.label}).` : ''}
            </p>
            <div className="quick-actions">
              {prompts.map(p => (
                <button key={p} className="quick-action-btn" onClick={() => send(p)}>{p}</button>
              ))}
            </div>
          </div>
        ) : (
          <div className="messages-list">
            {messages.map((m, i) => (
              <div key={m.id} className={`message ${m.role}${m.isError ? ' error' : ''}`}>
                <div className="message-avatar">{m.role === 'user' ? 'U' : '✦'}</div>
                <div className="message-content-wrapper">
                  <div className="message-sender">
                    {m.role === 'user' ? 'You' : 'Assistant'}
                    <span className="message-time">{m.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
                  </div>
                  {m.isStreaming && !m.content ? (
                    <div className="message-bubble loading-bubble">
                      <Loader2 size={16} className="spin" />
                      <span>{m.sources ? `Reading ${m.sources.length} sources…` : 'Searching the knowledge base…'}</span>
                    </div>
                  ) : m.isError ? (
                    <div className="message-bubble error-bubble">
                      <span>{m.content}</span>
                      <button className="retry-btn" onClick={() => retry(i)} disabled={isBusy}><RefreshCw size={14} /> Retry</button>
                    </div>
                  ) : (
                    <div className="message-bubble">
                      {m.role === 'user' ? m.content : renderMessage(m.content, m.sources)}
                      {m.isStreaming && <span className="cursor">▍</span>}
                    </div>
                  )}
                  {m.role === 'assistant' && m.sources && m.sources.length > 0 && !m.isStreaming && (
                    <details className="sources">
                      <summary>{m.sources.length} sources</summary>
                      <ul>
                        {m.sources.map(s => (
                          <li key={s.id}>
                            <Link to={`/forms/${s.documentId}`} title={s.text}>
                              <span className="cite">{s.id}</span>
                              <span className="badge product">{s.product}</span>
                              <span className="form-num">{s.formNo}{s.variant ?? ''}</span>
                              <span className="muted">{s.lang} · {s.type}{s.docs > 1 ? ` · in ${s.docs} forms` : ''}</span>
                            </Link>
                          </li>
                        ))}
                      </ul>
                    </details>
                  )}
                </div>
              </div>
            ))}
            <div ref={endRef} />
          </div>
        )}
      </div>

      <div className="input-area">
        <form onSubmit={e => { e.preventDefault(); send(input) }}>
          <div className="input-wrapper">
            <textarea
              ref={textareaRef}
              className="message-input"
              value={input}
              onChange={e => {
                setInput(e.target.value)
                e.target.style.height = 'auto'
                e.target.style.height = `${Math.min(e.target.scrollHeight, 200)}px`
              }}
              onKeyDown={onKeyDown}
              placeholder={context ? `Ask about ${context.label} or any form…` : 'Ask about any clause, benefit or form…'}
              rows={1}
              maxLength={4000}
            />
            {isBusy ? (
              <button type="button" className="send-btn stop" onClick={() => abortRef.current?.abort()} title="Stop">
                <Square size={14} />
              </button>
            ) : (
              <button type="submit" className="send-btn" disabled={!input.trim()} title="Send">
                <Send size={16} />
              </button>
            )}
          </div>
        </form>
        <div className="chat-footer-text">Local AI · answers can be wrong, check the sources</div>
      </div>
    </div>
  )
}

export default ChatArea
