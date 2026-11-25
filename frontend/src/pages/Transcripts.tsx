import { useState, useEffect, useRef } from 'react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { MessageSquare, Search, User, Bot, Phone, Copy, Check, FileSearch, RefreshCw, Sparkles } from 'lucide-react'
import { api } from '../api/client'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { Card, Badge, Button, Spinner, EmptyState, cx } from '../components/ui'
import { PageHeader, SearchInput, AsyncState, Field } from '../components/kit'
import { useDebounced, useUrlParam } from '../lib/hooks'
import { dur, parseTs } from '../lib/format'

/** The shared Button skin (ghost · sm · rounded-md) for a real router Link —
 *  ui.Button only renders <button>, and the cross-link to the intelligence
 *  workspace should be a link the browser can open in a new tab. */
const BUTTON_LINK_CLS =
  'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-md bg-[var(--surface-solid)] px-2.5 py-1.5 text-xs font-medium text-soft ring-1 ring-[var(--border)] transition-all hover:bg-[var(--surface-2)] hover:ring-[var(--border-strong)] active:scale-[.98] lg:min-h-0'

/* ------------------------------------------------------------------ Types */

interface Recording {
  folder: string
  call_id?: string
  caller?: string
  /** Saved phone-book name for `caller`, resolved server-side ('' if unknown). */
  contact_name?: string
  folder_dt?: string
}
interface RecordingsResp {
  recordings?: Recording[]
  total?: number
}
interface TranscriptMessage {
  speaker?: string
  text?: string
  timestamp?: string
}
interface TranscriptResp {
  folder?: string
  metadata?: Record<string, unknown>
  messages?: TranscriptMessage[]
}

/* ------------------------------------------------------------------ Helpers */

function fmtDate(s: string | undefined | null): string {
  if (!s) return '—'
  const d = parseTs(s)
  if (!d) return String(s)
  return d.toLocaleString('en-IN', {
    day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

/** Message timestamps arrive in whatever shape the backend logged — show a clean time when parseable. */
function fmtMsgTime(s: string): string {
  const d = parseTs(s)
  if (!d) return s
  return d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

function isAgent(speaker: string | undefined): boolean {
  const s = (speaker || '').toLowerCase()
  return s.includes('agent') || s.includes('bot') || s.includes('assistant') || s.includes('ai')
}

/** Client-side script detection for bilingual calls (en / hi / gu). */
function scriptOf(text: string | undefined): 'hi' | 'gu' | null {
  const t = text || ''
  if (/[\u0900-\u097f]/.test(t)) return 'hi'
  if (/[\u0a80-\u0aff]/.test(t)) return 'gu'
  return null
}

function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/** Wrap case-insensitive keyword matches in <mark> so search intent survives into the thread. */
function highlightText(text: string, keyword: string): ReactNode {
  if (!keyword) return text
  const parts = text.split(new RegExp(`(${escapeRegExp(keyword)})`, 'gi'))
  if (parts.length === 1) return text
  return parts.map((part, i) =>
    i % 2 === 1 ? (
      <mark key={i} className="rounded-[4px] bg-amber-400/30 px-0.5 text-[var(--text)]">{part}</mark>
    ) : (
      part
    ),
  )
}

function fmtMetaValue(key: string, value: unknown): string {
  if (/duration/i.test(key) && Number.isFinite(Number(value))) return dur(Number(value))
  const s = String(value)
  // Raw ISO datetimes (start_time, end_time, …) read as machine noise — humanize them.
  if (/^\d{4}-\d{2}-\d{2}[T ]/.test(s)) {
    const d = parseTs(s)
    if (d) {
      return d.toLocaleString('en-IN', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', second: '2-digit' })
    }
  }
  return s.length > 48 ? `${s.slice(0, 48)}…` : s
}

/* ------------------------------------------------------------------ Transcript panel */

function TranscriptPanel({ recording, highlight }: { recording: Recording | null; highlight?: string }) {
  const folder = recording?.folder ?? null
  const [data, setData] = useState<TranscriptResp | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<unknown>(null)
  const [copied, setCopied] = useState(false)
  const firstMatchRef = useRef<HTMLDivElement | null>(null)
  const hl = (highlight || '').trim()

  useEffect(() => {
    if (!folder) {
      setData(null)
      setError(null)
      return
    }
    let cancelled = false
    setLoading(true)
    setError(null)
    api<TranscriptResp>(`/calls/recordings/${encodeURIComponent(folder)}/transcript`)
      .then((res) => { if (!cancelled) setData(res) })
      .catch((e) => { if (!cancelled) setError(e) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [folder])

  useEffect(() => { setCopied(false) }, [folder])

  // Once a searched transcript loads, jump to the first keyword hit.
  useEffect(() => {
    if (loading || !data || !hl) return
    firstMatchRef.current?.scrollIntoView({ block: 'center' })
  }, [loading, data, hl])

  if (!folder) {
    return (
      <div className="grid flex-1 place-items-center">
        <EmptyState
          icon={<MessageSquare size={20} />}
          title="Select a call to view its transcript"
          hint="Pick a recording from the list on the left."
        />
      </div>
    )
  }

  if (loading) {
    // Chat-shaped skeleton: mirrors the loaded thread so switching calls doesn't jolt the layout.
    return (
      <div className="space-y-3">
        {[0, 1, 2, 3, 4].map((i) => (
          <div key={i} className={cx('flex items-start gap-2.5', i % 2 === 1 && 'flex-row-reverse')}>
            <div className="skeleton h-8 w-8 shrink-0 rounded-xl" />
            <div className={cx('skeleton h-14 rounded-2xl', ['w-3/5', 'w-2/5', 'w-4/5', 'w-1/2', 'w-3/5'][i])} />
          </div>
        ))}
      </div>
    )
  }

  if (error) {
    return <AsyncState error={error}>{null}</AsyncState>
  }

  const messages = Array.isArray(data?.messages) ? (data?.messages ?? []) : []

  if (messages.length === 0) {
    return (
      <div className="grid flex-1 place-items-center">
        <EmptyState
          icon={<MessageSquare size={20} />}
          title="No transcript for this call"
        />
      </div>
    )
  }

  const firstMatchIdx = hl
    ? messages.findIndex((m) => (m?.text || '').toLowerCase().includes(hl.toLowerCase()))
    : -1

  const meta = Object.entries(data?.metadata ?? {}).filter(
    ([, v]) => (typeof v === 'string' && v !== '') || typeof v === 'number' || typeof v === 'boolean',
  )

  const copyTranscript = () => {
    const text = messages
      .map((m) => `${m?.speaker || (isAgent(m?.speaker) ? 'Agent' : 'Caller')}: ${m?.text ?? ''}`)
      .join('\n')
    navigator.clipboard?.writeText(text)
      .then(() => {
        setCopied(true)
        setTimeout(() => setCopied(false), 1500)
      })
      .catch(() => {})
  }

  return (
    <div className="flex min-h-0 flex-col lg:flex-1">
      {/* Call context header */}
      <div className="-mx-4 -mt-4 mb-3 shrink-0 border-b border-[var(--border)] px-4 py-3">
        <div className="flex flex-wrap items-center gap-2">
          <div className="min-w-0">
            <div className="truncate font-display text-base font-bold">
              {recording?.contact_name || recording?.caller || recording?.call_id || folder}
            </div>
            {recording?.contact_name && recording?.caller && (
              <div className="truncate font-mono text-2xs text-dim">{recording.caller}</div>
            )}
            <div className="text-xs tabular-nums text-dim">{fmtDate(recording?.folder_dt)}</div>
          </div>
          <div className="ml-auto flex flex-wrap items-center gap-2">
            <Badge>{messages.length} turns</Badge>
            <Button variant="ghost" size="sm" className="min-h-9 lg:min-h-0" onClick={copyTranscript} title="Copy full transcript to clipboard">
              {copied ? <><Check size={13} /> Copied</> : <><Copy size={13} /> Copy transcript</>}
            </Button>
            {/* folder IS the call_key — the same id Intelligence rows link with. */}
            <Link to={`/intelligence/${encodeURIComponent(folder)}`} className={BUTTON_LINK_CLS}>
              <Sparkles size={13} /> AI analysis →
            </Link>
          </div>
        </div>
        {meta.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {meta.map(([k, v]) => (
              <span key={k} title={`${k}: ${String(v)}`}>
                <Badge tone="neutral"><span className="max-w-[70vw] truncate sm:max-w-none">{k}: {fmtMetaValue(k, v)}</span></Badge>
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Thread — scrolls internally on desktop so the header stays put.
          Content column is capped: ~1000px chat bubbles are unreadable on wide screens. */}
      <div className="mx-auto min-h-0 w-full max-w-4xl flex-1 space-y-3 overflow-y-auto pr-1">
        {messages.map((m, i) => {
          const agent = isAgent(m?.speaker)
          const lang = scriptOf(m?.text)
          const rawSpeaker = (m?.speaker || '').trim()
          const showRaw = rawSpeaker !== '' && !['agent', 'caller', 'user'].includes(rawSpeaker.toLowerCase())
          return (
            <div
              key={i}
              ref={i === firstMatchIdx ? firstMatchRef : undefined}
              className={cx('flex items-start gap-2.5', !agent && 'flex-row-reverse')}
            >
              <div className={cx(
                'grid h-8 w-8 shrink-0 place-items-center rounded-xl ring-1',
                agent
                  ? 'bg-mint-400/15 text-mint-700 ring-mint-400/30 dark:text-mint-300'
                  : 'bg-indigo-500/12 text-indigo-600 ring-indigo-500/30 dark:text-indigo-300',
              )}>
                {agent ? <Bot size={15} /> : <User size={15} />}
              </div>
              <div className={cx(
                'w-fit min-w-0 max-w-[85%] rounded-2xl px-3.5 py-2.5 ring-1',
                agent
                  ? 'mr-auto rounded-tl-md bg-mint-400/10 ring-mint-400/20'
                  : 'ml-auto rounded-tr-md bg-indigo-500/10 ring-indigo-500/20',
              )}>
                <div className="mb-1 flex items-center justify-between gap-2">
                  <span className="flex min-w-0 items-center gap-1.5" title={rawSpeaker || undefined}>
                    <Badge tone={agent ? 'mint' : 'indigo'}>{agent ? 'Agent' : 'Caller'}</Badge>
                    {showRaw && <span className="min-w-0 truncate text-2xs text-dim">· {rawSpeaker}</span>}
                    {lang && <Badge tone="neutral">{lang.toUpperCase()}</Badge>}
                  </span>
                  {m?.timestamp && (
                    <span className="shrink-0 text-2xs tabular-nums text-dim" title={m.timestamp}>
                      {fmtMsgTime(m.timestamp)}
                    </span>
                  )}
                </div>
                <div
                  lang={lang ?? undefined}
                  className={cx(
                    'whitespace-pre-wrap break-words text-soft',
                    lang ? 'text-base leading-6' : 'text-sm',
                  )}
                >
                  {hl ? highlightText(m?.text ?? '', hl) : (m?.text ?? '')}
                </div>
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ Page */

export default function Transcripts() {
  const [callerFilter, setCallerFilter] = useState('')
  // Search + selected call live in the URL, so a refresh (or a shared link)
  // reopens the exact same thread. '' means "nothing selected".
  const [keyword, setKeyword] = useUrlParam('q')
  const [selected, setSelected] = useUrlParam('call')
  const refresh = useRefresh()
  const transcriptRef = useRef<HTMLDivElement | null>(null)
  const selectedRowRef = useRef<HTMLButtonElement | null>(null)

  // Debounced so the search endpoint fires once per pause, not per keystroke.
  const debouncedKeyword = useDebounced(keyword, 300).trim()

  const searchActive = debouncedKeyword.length >= 2

  const defaultQ = useGet<RecordingsResp>(
    ['recordings', 'list'],
    '/calls/recordings?limit=100&page=1',
    { enabled: !searchActive },
  )
  const searchQ = useGet<RecordingsResp>(
    ['transcripts', 'search', debouncedKeyword],
    `/calls/transcripts/search?q=${encodeURIComponent(debouncedKeyword)}`,
    { enabled: searchActive },
  )

  const activeQ = searchActive ? searchQ : defaultQ
  const allRecordings: Recording[] = Array.isArray(activeQ.data?.recordings)
    ? (activeQ.data?.recordings ?? [])
    : []

  // Client-side caller filter
  const filter = callerFilter.trim().toLowerCase()
  const recordings = filter
    ? allRecordings.filter((r) => (r?.caller || '').toLowerCase().includes(filter)
        || (r?.contact_name || '').toLowerCase().includes(filter))
    : allRecordings

  // Default-select first item when list loads / changes
  useEffect(() => {
    if (recordings.length === 0) return
    const stillPresent = selected !== '' && recordings.some((r) => r?.folder === selected)
    if (!stillPresent) {
      setSelected(recordings[0]?.folder ?? '')
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordings])

  // Keep the highlighted row visible inside the list's own scroll area
  useEffect(() => {
    selectedRowRef.current?.scrollIntoView({ block: 'nearest' })
  }, [selected])

  const selectedRec: Recording | null =
    recordings.find((r) => r?.folder === selected) ?? (selected ? { folder: selected } : null)

  return (
    <div className="space-y-6">
      <PageHeader
        title="Call Transcripts"
        sub="Browse & search conversation transcripts"
        actions={
          <Button variant="ghost" onClick={() => refresh(['recordings'], ['transcripts'])}>
            <RefreshCw size={14} /> Refresh
          </Button>
        }
      />

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        {/* LEFT — list + filters */}
        <Card className="flex flex-col gap-3 p-4 lg:sticky lg:top-4 lg:self-start">
          {/* Two different searches, two different anatomies: a plain list
              filter vs a content search with its own icon + real labels. */}
          <Field label="Filter calls">
            <SearchInput
              value={callerFilter}
              onChange={setCallerFilter}
              placeholder="Filter calls…"
              ariaLabel="Filter calls by phone or name"
            />
          </Field>
          <Field label="Search inside conversations">
            <SearchInput
              value={keyword}
              onChange={setKeyword}
              icon={<FileSearch size={15} />}
              placeholder="Search inside conversations…"
              ariaLabel="Search inside conversations"
            />
          </Field>

          <div className="flex items-center justify-between text-2xs text-dim">
            <span aria-live="polite">
              {searchActive
                ? `${recordings.length} matches for "${debouncedKeyword}"`
                : `${recordings.length} recent recordings`}
            </span>
            {activeQ.isFetching && <Spinner className="h-3.5 w-3.5" />}
          </div>

          <div className="-mr-1 max-h-[40vh] space-y-1 overflow-y-auto pr-1 lg:max-h-[calc(100vh-330px)]">
            <AsyncState loading={activeQ.isLoading} error={activeQ.error}>
              {recordings.length === 0 ? (
                searchActive ? (
                  <EmptyState
                    icon={<Search size={20} />}
                    title={`No transcripts mention "${debouncedKeyword}"`}
                    hint="Search looks inside the spoken conversation — try a shorter or different word."
                  />
                ) : filter ? (
                  <EmptyState
                    icon={<Search size={20} />}
                    title={`No recordings match "${callerFilter.trim()}"`}
                    hint="Try clearing the caller filter above."
                  />
                ) : (
                  <EmptyState
                    icon={<MessageSquare size={20} />}
                    title="No recordings yet"
                    hint="Transcripts appear here automatically after your agent takes its first call."
                  />
                )
              ) : (
                recordings.map((r, i) => {
                  const isSel = selected === r?.folder
                  return (
                    <button
                      key={r?.folder ?? i}
                      ref={isSel ? selectedRowRef : undefined}
                      aria-current={isSel ? 'true' : undefined}
                      onClick={() => {
                        setSelected(r?.folder ?? '')
                        if (window.innerWidth < 1024) {
                          transcriptRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
                        }
                      }}
                      className={cx(
                        'w-full rounded-md px-3 py-2.5 text-left transition ring-1',
                        isSel
                          ? 'bg-mint-400/12 ring-mint-400/40 shadow-[inset_2px_0_0_0_theme(colors.mint.400)]'
                          : 'bg-[var(--surface-2)] ring-[var(--border)] hover:bg-[var(--border)]',
                      )}
                    >
                      <div className="flex items-center gap-2">
                        <Phone size={13} className="shrink-0 text-dim" />
                        <span className="truncate text-sm font-semibold text-soft">
                          {r?.contact_name || r?.caller || r?.call_id || r?.folder || 'Unknown'}
                        </span>
                      </div>
                      <div className="mt-0.5 pl-5 text-2xs tabular-nums text-dim">
                        {r?.contact_name && r?.caller ? `${r.caller} · ` : ''}{fmtDate(r?.folder_dt)}
                      </div>
                    </button>
                  )
                })
              )}
            </AsyncState>
          </div>
        </Card>

        {/* RIGHT — transcript thread */}
        <div ref={transcriptRef} className="min-w-0">
          {/* min-h keeps this column visually balanced with the tall list beside it */}
          <Card className="p-4 lg:flex lg:min-h-[calc(100vh-170px)] lg:max-h-[calc(100vh-170px)] lg:flex-col">
            <TranscriptPanel recording={selectedRec} highlight={searchActive ? debouncedKeyword : ''} />
          </Card>
        </div>
      </div>
    </div>
  )
}
