import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Check, Copy, Download, FileAudio, Link2, RefreshCw, SearchX, Sparkles } from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { Badge, Button, Card, EmptyState, IconButton, Spinner, cx } from '../components/ui'
import { PageHeader, Table, THead, TH, TR, TD, Pagination, SearchInput, AsyncState, ContactLabel } from '../components/kit'
import { AudioPlayer } from '../components/AudioPlayer'
import { useDebounced, useUrlNumber, useUrlParam, useUrlPatch } from '../lib/hooks'
import { fmtDayLabel, fmtFull, fmtTime, parseTs } from '../lib/format'

/** The shared Button skin (ghost · sm · rounded-md) as a class string, for the
 *  two link-shaped actions below — ui.Button only renders a <button>, but a
 *  download must stay an <a> and a cross-link should be a real router Link. */
const BUTTON_LINK_CLS =
  'inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-md bg-[var(--surface-solid)] px-2.5 py-1.5 text-xs font-medium text-soft ring-1 ring-[var(--border)] transition-all hover:bg-[var(--surface-2)] hover:ring-[var(--border-strong)] active:scale-[.98] sm:min-h-0'

type Recording = {
  folder: string
  call_id: string | null
  caller: string
  /** Saved phone-book name for `caller`, resolved server-side ('' if unknown). */
  contact_name?: string
  folder_dt: string
  /** Public tokenized download link (unguessable, shareable without login). */
  download_url?: string | null
  gcs_uploaded?: boolean
}

type RecordingsResponse = {
  recordings: Recording[]
  total: number
  page: number
  limit: number
  pages: number
}

/* Day labelling and instant parsing are shared with every other call surface —
   see lib/format.ts for why folder_dt cannot go straight into `new Date()`. */

export default function Recordings() {
  // Search + page live in the URL — deep links like /recordings?q=<call id>
  // (from Activity's cross-link) land directly on the filtered view.
  const [page, setPage] = useUrlNumber('page', 1)
  const [search] = useUrlParam('q')
  const [copiedId, setCopiedId] = useState<string | null>(null)
  const copyTimer = useRef<number | null>(null)
  const refresh = useRefresh()

  // Debounce keystrokes so the query (and the table) doesn't churn per character.
  const debounced = useDebounced(search, 300)
  // ONE navigation for both params. As two calls (setSearch then setPage) the
  // page reset recomputed the URL from pre-update state and dropped the letter
  // just typed — the box looked like it refused input.
  const patch = useUrlPatch()
  const changeSearch = (v: string) => patch({ q: v, page: null })

  useEffect(() => () => {
    if (copyTimer.current) window.clearTimeout(copyTimer.current)
  }, [])

  const { data, isLoading, isFetching, error } = useGet<RecordingsResponse>(
    ['recordings', page, debounced],
    `/calls/recordings?page=${page}&limit=50&search=${encodeURIComponent(debounced)}`,
  )

  // Keep the previous page's rows on screen (dimmed) while the next fetch runs,
  // instead of tearing the table down into skeletons on every page/search change.
  const [prev, setPrev] = useState<RecordingsResponse | null>(null)
  useEffect(() => {
    if (data) setPrev(data)
  }, [data])
  const view = data ?? prev ?? undefined
  const refreshing = isLoading && view != null

  const recordings = Array.isArray(view?.recordings) ? view!.recordings : []
  const total = view?.total ?? 0
  const pages = view?.pages ?? 1

  const copyCallId = (id: string) => {
    navigator.clipboard?.writeText(id).then(() => {
      setCopiedId(id)
      if (copyTimer.current) window.clearTimeout(copyTimer.current)
      copyTimer.current = window.setTimeout(() => setCopiedId(null), 1200)
    }).catch(() => {})
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Recordings"
        sub="Call audio & metadata"
        actions={
          <>
            {total > 0 && <Badge tone="neutral">{total.toLocaleString('en-IN')} recordings</Badge>}
            <Button variant="ghost" onClick={() => refresh(['recordings'])}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </>
        }
      />

      <Card className="p-5">
        <div className="mb-4 flex items-center gap-3">
          <SearchInput
            value={search}
            onChange={changeSearch}
            placeholder="Search by contact / number"
            className="w-full max-w-sm"
          />
          {isFetching && view != null && <Spinner className="h-4 w-4 shrink-0" />}
        </div>

        <AsyncState loading={isLoading && view == null} error={error}>
          {recordings.length === 0 ? (
            debounced ? (
              <EmptyState
                icon={<SearchX size={20} />}
                title={`No recordings match "${debounced}"`}
                hint="Try a shorter name or the last digits of the number."
                action={
                  <Button variant="ghost" size="sm" onClick={() => changeSearch('')}>
                    Clear search
                  </Button>
                }
              />
            ) : (
              <EmptyState
                icon={<FileAudio size={20} />}
                title="No recordings yet"
                hint="Audio is captured automatically for every completed call — recordings will appear here after your agent takes its first call."
              />
            )
          ) : (
            <div className={cx('transition-opacity', refreshing && 'pointer-events-none opacity-60')}>
              <Table>
                <THead>
                  <TH>Contact</TH>
                  <TH>Date</TH>
                  <TH>Call ID</TH>
                  <TH>Play</TH>
                  <TH>Actions</TH>
                </THead>
                <tbody>
                  {recordings.map((r) => {
                    const folder = r?.folder ?? ''
                    const enc = encodeURIComponent(folder)
                    const audioUrl = `/api/calls/recordings/${enc}/audio`
                    // Prefer the public tokenized link (works without a
                    // session and survives local-file cleanup after GCS sync).
                    const downloadUrl = r?.download_url || `${audioUrl}?download=1`
                    const d = parseTs(r?.folder_dt)
                    return (
                      <TR key={folder || r?.call_id || Math.random()} className="group">
                        <TD><ContactLabel name={r?.contact_name} phone={r?.caller} /></TD>
                        <TD className="whitespace-nowrap tabular-nums" title={d ? fmtFull(d) : undefined}>
                          {d ? (
                            <>
                              <span className="text-[var(--text)]">{fmtDayLabel(d)}</span>{' '}
                              <span className="text-dim">{fmtTime(d)}</span>
                            </>
                          ) : (
                            <span className="text-dim">{r?.folder_dt || '—'}</span>
                          )}
                        </TD>
                        <TD>
                          <div className="flex items-center gap-1.5">
                            <span
                              className="max-w-[160px] truncate font-mono text-xs text-dim"
                              title={r?.call_id ?? ''}
                            >
                              {r?.call_id || '—'}
                            </span>
                            {r?.call_id && (
                              <IconButton
                                label="Copy call ID"
                                onClick={() => copyCallId(r.call_id!)}
                                className={cx(
                                  '-my-2.5 md:opacity-0 md:focus-visible:opacity-100 md:group-hover:opacity-100',
                                  copiedId === r.call_id && 'md:opacity-100',
                                )}
                              >
                                {copiedId === r.call_id ? (
                                  <Check size={12} className="text-mint-500" />
                                ) : (
                                  <Copy size={12} />
                                )}
                              </IconButton>
                            )}
                          </div>
                        </TD>
                        <TD>
                          <AudioPlayer src={audioUrl} caller={r?.caller || undefined} />
                        </TD>
                        <TD>
                          <div className="flex items-center gap-1.5">
                            <a href={downloadUrl} download className={BUTTON_LINK_CLS}>
                              <Download size={14} /> Download
                            </a>
                            {r?.download_url && (
                              <IconButton
                                label="Copy public download link"
                                onClick={() => copyCallId(r.download_url!)}
                              >
                                {copiedId === r.download_url ? (
                                  <Check size={14} className="text-mint-500" />
                                ) : (
                                  <Link2 size={14} />
                                )}
                              </IconButton>
                            )}
                            {/* folder IS the call_key — the same id Intelligence rows link with. */}
                            {folder && (
                              <Link to={`/intelligence/${encodeURIComponent(folder)}`} className={BUTTON_LINK_CLS}>
                                <Sparkles size={13} /> AI analysis →
                              </Link>
                            )}
                          </div>
                        </TD>
                      </TR>
                    )
                  })}
                </tbody>
              </Table>
            </div>
          )}
        </AsyncState>

        <Pagination page={page} pages={pages} total={total} onPage={setPage} />
      </Card>
    </div>
  )
}
