import { Fragment, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import {
  Check, ChevronRight, Copy, PhoneIncoming, PhoneOutgoing, RefreshCw, SearchX,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { Badge, Button, Card, EmptyState, IconButton, Spinner, cx } from '../components/ui'
import {
  PageHeader, Table, THead, TH, TR, TD, Pagination,
  StatusBadge, SearchInput, Segmented, AsyncState, ContactLabel,
} from '../components/kit'
import { useDebounced, useUrlNumber, useUrlParam, useUrlPatch } from '../lib/hooks'
import { CALL_STATUS_FILTER } from '../lib/callStatus'
import { inr, dur, num, parseTs } from '../lib/format'

type Call = {
  call_id?: string
  start_time?: string
  end_time?: string
  duration_seconds?: number
  est_stt_cost?: number
  est_tts_cost?: number
  est_total_cost?: number
  call_type?: 'inbound' | 'outbound'
  contact_name?: string
  contact_phone?: string
  status?: 'answered' | 'no_answer' | 'busy' | 'declined' | 'invalid_number' | 'failed'
}

type HistoryResp = {
  calls?: Call[]
  total?: number
  page?: number
  limit?: number
  pages?: number
}

const fmtWhen = (s?: string): string => {
  const d = parseTs(s)
  return d ? d.toLocaleString('en-IN') : '—'
}

const fmtTime = (s?: string): string => {
  const d = parseTs(s)
  return d ? d.toLocaleTimeString('en-IN', { hour: '2-digit', minute: '2-digit' }) : '—'
}

/** react-router Link wearing the shared ghost-Button skin (sm size) — ui.Button
 *  only renders <button>, and cross-links to other views should be real links. */
function LinkButton({ to, children }: { to: string; children: ReactNode }) {
  return (
    <Link
      to={to}
      onClick={(e) => e.stopPropagation()}
      className="inline-flex min-h-[36px] items-center justify-center gap-1.5 rounded-md bg-[var(--surface-solid)] px-2.5 py-1.5 text-xs font-medium text-soft ring-1 ring-[var(--border)] transition-all hover:bg-[var(--surface-2)] hover:ring-[var(--border-strong)] active:scale-[.98] sm:min-h-0"
    >
      {children}
    </Link>
  )
}

const dayLabel = (s?: string): string => {
  const d = parseTs(s)
  if (!d) return 'Unknown date'
  const today = new Date()
  const yesterday = new Date()
  yesterday.setDate(today.getDate() - 1)
  if (d.toDateString() === today.toDateString()) return 'Today'
  if (d.toDateString() === yesterday.toDateString()) return 'Yesterday'
  return d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' })
}

export default function Activity() {
  // Filters + page live in the URL — refresh/back/share keep the exact view.
  const [page, setPage] = useUrlNumber('page', 1)
  const [search, setSearch] = useUrlParam('q')
  const [days, setDays] = useUrlNumber('days', 7)
  const [callType, setCallType] = useUrlParam('type', 'all')
  const [status, setStatus] = useUrlParam('status', 'all')
  const [open, setOpen] = useState<string | null>(null)
  const [copied, setCopied] = useState<string | null>(null)
  const refresh = useRefresh()

  // Debounced so we fire one request per pause, not one per keystroke.
  const debouncedSearch = useDebounced(search, 300)

  // Filter + page reset must be ONE navigation. Written as two calls
  // (setDays/setCallType/setStatus then setPage) the page reset recomputed the
  // URL from pre-update state and threw the filter change away — which is why
  // clicking these pills appeared to do nothing at all.
  const patch = useUrlPatch()

  const path =
    `/calls/history?page=${page}&limit=50` +
    `&search=${encodeURIComponent(debouncedSearch)}` +
    `&days=${days}&call_type=${callType}&status=${status}`

  const { data, isLoading, isFetching, error } = useGet<HistoryResp>(
    ['history', page, debouncedSearch, days, callType, status],
    path,
  )

  // Keep the previous rows on screen while a new page/filter loads so a
  // refetch reads as a soft dim instead of a table teardown.
  const lastDataRef = useRef<HistoryResp | undefined>(undefined)
  if (data) lastDataRef.current = data
  const view = data ?? lastDataRef.current

  const calls = view?.calls ?? []
  const showSkeleton = isLoading && !view
  const refreshing = isFetching && !showSkeleton

  const filtersActive = !!search || callType !== 'all' || status !== 'all' || days !== 7
  // Five setters in one tick had the same clobber problem — only the last
  // survived, so "Reset filters" left most filters exactly where they were.
  const clearFilters = () =>
    patch({ q: null, type: null, status: null, days: null, page: null })

  const copyId = (id: string) => {
    navigator.clipboard.writeText(id).then(() => {
      setCopied(id)
      window.setTimeout(() => setCopied((cur) => (cur === id ? null : cur)), 1500)
    }).catch(() => { /* clipboard unavailable */ })
  }

  // Group consecutive rows by calendar day for scannable date dividers.
  const groups: { key: string; label: string; items: Call[] }[] = []
  for (const c of calls) {
    const d = parseTs(c?.start_time)
    const key = d ? d.toDateString() : 'unknown'
    const last = groups[groups.length - 1]
    if (last && last.key === key) last.items.push(c)
    else groups.push({ key, label: dayLabel(c?.start_time), items: [c] })
  }

  const head = (
    <THead>
      <TH className="w-8"><span className="sr-only">Expand row</span></TH>
      <TH>Date &amp; Time</TH>
      <TH>Contact</TH>
      <TH>Type</TH>
      <TH>Duration</TH>
      <TH>Status</TH>
      <TH>Call ID</TH>
      <TH className="text-right">
        <span
          title="Estimated STT + TTS spend for this call"
          className="cursor-help underline decoration-dotted decoration-[var(--border-strong)] underline-offset-2"
        >
          Est. Cost
        </span>
      </TH>
    </THead>
  )

  return (
    <div className="space-y-6">
      <PageHeader
        title="Call Activity"
        sub="Every call your agents placed and answered"
        actions={
          <Button variant="ghost" size="sm" onClick={() => refresh(['history'])}>
            {refreshing ? <Spinner className="h-3.5 w-3.5" /> : <RefreshCw size={14} />}
            Refresh
          </Button>
        }
      />

      <Card className="p-5">
        <div className="mb-4 flex flex-wrap items-center gap-3">
          <SearchInput
            value={search}
            onChange={(v) => patch({ q: v, page: null })}
            placeholder="Search call id or contact"
            className="min-w-[220px] flex-1"
          />
          {/* One idiom for all three filters: Segmented pill groups. */}
          <div className="max-w-full overflow-x-auto [scrollbar-width:none]">
            <Segmented
              options={[
                { value: '1', label: 'Today' },
                { value: '7', label: '7 days' },
                { value: '30', label: '30 days' },
                { value: '90', label: '90 days' },
              ]}
              value={String(days)}
              onChange={(v) => patch({ days: v, page: null })}
            />
          </div>
          <Segmented
            options={[
              { value: 'all', label: 'All types' },
              { value: 'inbound', label: 'Inbound' },
              { value: 'outbound', label: 'Outbound' },
            ]}
            value={callType}
            onChange={(v) => patch({ type: v === 'all' ? null : v, page: null })}
          />
          <div className="max-w-full overflow-x-auto [scrollbar-width:none]">
            <Segmented
              options={CALL_STATUS_FILTER.map((o) => ({ value: o.value, label: o.label }))}
              value={status}
              onChange={(v) => patch({ status: v === 'all' ? null : v, page: null })}
            />
          </div>
        </div>

        {!showSkeleton && !error && (
          <div aria-live="polite" className="mb-2 flex items-center gap-2 text-xs text-dim">
            <span>{num(view?.total ?? 0)} calls · {days === 1 ? 'today' : `last ${days} days`}</span>
            {refreshing && <Spinner className="h-3 w-3" />}
          </div>
        )}

        <AsyncState loading={showSkeleton} error={error}>
        {calls.length === 0 ? (
          filtersActive ? (
            <EmptyState
              icon={<SearchX size={22} />}
              title="No calls match these filters"
              hint="Try a wider date range or a different status."
              action={<Button variant="ghost" size="sm" onClick={clearFilters}>Reset filters</Button>}
            />
          ) : (
            <EmptyState
              icon={<PhoneIncoming size={22} />}
              title="No calls yet"
              hint="Calls will appear here the moment your agent answers an inbound call or a campaign starts dialing."
            />
          )
        ) : (
          <div className={cx('transition-opacity', refreshing && 'opacity-60')}>
            <Table>
              {head}
              <tbody>
                {groups.map((g, gi) => (
                  <Fragment key={`${g.key}-${gi}`}>
                    <tr className="border-b border-[var(--border)]">
                      <td colSpan={8} className="bg-[var(--surface-2)] px-3 py-1.5">
                        <span className="eyebrow sticky left-3 inline-block">{g.label}</span>
                      </td>
                    </tr>
                    {g.items.map((c, i) => {
                      const rid = c?.call_id ?? `row-${gi}-${i}`
                      const isOpen = open === rid
                      return (
                        <Fragment key={rid}>
                          <TR className="group" onClick={() => setOpen(isOpen ? null : rid)}>
                            <TD className="w-8 pr-0">
                              {/* Stays a native button: this is a disclosure, and it must carry
                                  aria-expanded, which IconButton's props don't pass through.
                                  Classes mirror IconButton's anatomy exactly. */}
                              <button
                                type="button"
                                aria-label={isOpen ? 'Collapse call details' : 'Expand call details'}
                                aria-expanded={isOpen}
                                onClick={(e) => { e.stopPropagation(); setOpen(isOpen ? null : rid) }}
                                className="-my-2.5 inline-grid h-9 w-9 shrink-0 place-items-center rounded-md text-dim transition-all hover:bg-[var(--surface-2)] hover:text-soft active:scale-[.96]"
                              >
                                <ChevronRight size={14} className={cx('transition-transform', isOpen && 'rotate-90')} />
                              </button>
                            </TD>
                            <TD className="whitespace-nowrap text-[var(--text)]" title={fmtWhen(c?.start_time)}>
                              {fmtTime(c?.start_time)}
                            </TD>
                            <TD><ContactLabel name={c?.contact_name} phone={c?.contact_phone} /></TD>
                            <TD>
                              {c?.call_type
                                ? (
                                  <Badge tone={c.call_type === 'inbound' ? 'indigo' : 'mint'}>
                                    {c.call_type === 'inbound' ? <PhoneIncoming size={11} /> : <PhoneOutgoing size={11} />}
                                    {c.call_type}
                                  </Badge>
                                )
                                : <span className="text-dim">—</span>}
                            </TD>
                            <TD className="tabular-nums">{dur(c?.duration_seconds)}</TD>
                            <TD><StatusBadge value={c?.status} /></TD>
                            <TD>
                              <div className="flex items-center gap-1.5">
                                <span title={c?.call_id} className="block max-w-[140px] truncate font-mono text-2xs text-dim">
                                  {c?.call_id ?? '—'}
                                </span>
                                {c?.call_id && (
                                  <span onClick={(e) => e.stopPropagation()}>
                                    <IconButton
                                      label="Copy call ID"
                                      onClick={() => copyId(c.call_id!)}
                                      className="-my-2.5 focus-visible:opacity-100 md:opacity-0 md:group-hover:opacity-100"
                                    >
                                      {copied === c.call_id ? <Check size={12} className="text-mint-500" /> : <Copy size={12} />}
                                    </IconButton>
                                  </span>
                                )}
                              </div>
                            </TD>
                            <TD
                              className="text-right tabular-nums text-[var(--text)]"
                              title={`STT ${c?.est_stt_cost != null ? inr(c.est_stt_cost, 2) : '—'} · TTS ${c?.est_tts_cost != null ? inr(c.est_tts_cost, 2) : '—'}`}
                            >
                              {c?.est_total_cost != null ? inr(c.est_total_cost, 2) : <span className="text-dim">—</span>}
                            </TD>
                          </TR>
                          {isOpen && (
                            <tr className="border-b border-[var(--border)]">
                              <TD colSpan={8} className="bg-[var(--surface-2)]">
                                <div className="sticky left-0 grid max-w-[calc(100vw-5rem)] grid-cols-2 gap-4 py-1 sm:max-w-none sm:grid-cols-4">
                                  <div>
                                    <div className="eyebrow">Started</div>
                                    <div className="mt-0.5 text-sm text-[var(--text)]">{fmtWhen(c?.start_time)}</div>
                                  </div>
                                  <div>
                                    <div className="eyebrow">Ended</div>
                                    <div className="mt-0.5 text-sm text-[var(--text)]">{fmtWhen(c?.end_time)}</div>
                                  </div>
                                  <div>
                                    <div className="eyebrow">STT cost</div>
                                    <div className="mt-0.5 text-sm tabular-nums text-[var(--text)]">
                                      {c?.est_stt_cost != null ? inr(c.est_stt_cost, 2) : '—'}
                                    </div>
                                  </div>
                                  <div>
                                    <div className="eyebrow">TTS cost</div>
                                    <div className="mt-0.5 text-sm tabular-nums text-[var(--text)]">
                                      {c?.est_tts_cost != null ? inr(c.est_tts_cost, 2) : '—'}
                                    </div>
                                  </div>
                                  <div className="col-span-2 sm:col-span-4">
                                    <div className="eyebrow">Call ID</div>
                                    <div className="mt-0.5 break-all font-mono text-xs text-soft">{c?.call_id ?? '—'}</div>
                                  </div>
                                  {c?.call_id && (
                                    <div className="col-span-2 flex flex-wrap items-center gap-2 sm:col-span-4">
                                      {/* The intel workspace resolves call_key OR conn_id — history's
                                          call_id IS the conn_id, so this link lands on the right call. */}
                                      <LinkButton to={`/intelligence/${encodeURIComponent(c.call_id)}`}>AI analysis →</LinkButton>
                                      <LinkButton to={`/recordings?q=${encodeURIComponent(c.call_id)}`}>Recording →</LinkButton>
                                    </div>
                                  )}
                                </div>
                              </TD>
                            </tr>
                          )}
                        </Fragment>
                      )
                    })}
                  </Fragment>
                ))}
              </tbody>
            </Table>
          </div>
        )}
        </AsyncState>

        <Pagination
          page={view?.page ?? page}
          pages={view?.pages ?? 1}
          total={view?.total}
          onPage={setPage}
        />
      </Card>
    </div>
  )
}
