/**
 * Reports — every generated report lives here, and this is where you make one.
 *
 * A report is an AI synthesis of the stored intelligence for a SET of calls.
 * That set used to be expressible only by hand-ticking rows on the Intelligence
 * page, which does not survive contact with "report on last month's outbound
 * calls" — so the builder here names the set with a filter instead, resolved
 * server-side by the same predicates the Intelligence list uses. Both paths
 * still land on the same POST /intel/reports.
 */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { Download, FileText, Plus, RefreshCw, Sparkles, Trash2 } from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { api } from '../api/client'
import { del } from '../api/mutate'
import { createReport, previewReport, type Report, type ReportFilters } from '../api/intel'
import { Button, IconButton, Spinner, Card, EmptyState, cx } from '../components/ui'
import {
  AsyncState, Field, Input, PageHeader, Pagination, Select, StatusBadge,
  Table, TD, TH, THead, TR, Textarea,
} from '../components/kit'
import { FullscreenModal, Modal } from '../components/Modal'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/confirm'
import { useDebounced, useUrlNumber } from '../lib/hooks'
import { fmtDateTime } from '../lib/format'

const LIMIT = 50

/** Export formats, in the order they appear on the reader's toolbar. */
const EXPORTS = [
  { fmt: 'docx', label: 'Word (.docx)' },
  { fmt: 'md', label: 'Markdown (.md)' },
  { fmt: 'txt', label: 'Text (.txt)' },
  { fmt: 'xlsx', label: 'Analysis (.xlsx)' },
] as const

const EMPTY_DRAFT = {
  title: '',
  focus: '',
  days: '30',
  search: '',
  sentiment: '',
  decision: '',
  direction: '',
  minLead: '0',
  limit: '200',
}
type Draft = typeof EMPTY_DRAFT

const draftToFilters = (d: Draft): ReportFilters => ({
  days: d.days === '0' ? 0 : Number(d.days),
  search: d.search.trim() || undefined,
  sentiment: d.sentiment || undefined,
  decision: d.decision || undefined,
  direction: d.direction || undefined,
  min_lead: Number(d.minLead) || 0,
  limit: Number(d.limit) || 200,
})

/* Minimal, dependency-free markdown renderer for report documents:
   headings, bold/italic, lists, tables, hr, paragraphs. */
function mdToHtml(md: string): string {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const inline = (s: string) => esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|\W)\*([^*\n]+)\*(?=\W|$)/g, '$1<em>$2</em>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
  const lines = md.split('\n')
  const out: string[] = []
  let list: string[] = []
  let table: string[][] = []
  const flushList = () => {
    if (list.length) { out.push('<ul>' + list.map((l) => `<li>${l}</li>`).join('') + '</ul>'); list = [] }
  }
  const flushTable = () => {
    if (table.length) {
      const [head, ...body] = table
      out.push('<div class="md-table"><table><thead><tr>'
        + head.map((c) => `<th>${c}</th>`).join('') + '</tr></thead><tbody>'
        + body.map((r) => '<tr>' + r.map((c) => `<td>${c}</td>`).join('') + '</tr>').join('')
        + '</tbody></table></div>')
      table = []
    }
  }
  for (const raw of lines) {
    const line = raw.trimEnd()
    const h = /^(#{1,4})\s+(.*)$/.exec(line)
    const li = /^\s*[-*]\s+(.*)$/.exec(line)
    const num = /^\s*\d+\.\s+(.*)$/.exec(line)
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const cells = line.trim().slice(1, -1).split('|').map((c) => inline(c.trim()))
      if (!cells.every((c) => /^:?-{2,}:?$/.test(c))) table.push(cells)
      continue
    }
    flushTable()
    if (h) { flushList(); out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); continue }
    if (li) { list.push(inline(li[1])); continue }
    if (num) { list.push(inline(num[1])); continue }
    flushList()
    if (/^---+$/.test(line)) { out.push('<hr/>'); continue }
    if (line.trim()) out.push(`<p>${inline(line)}</p>`)
  }
  flushList(); flushTable()
  return out.join('\n')
}

export default function Reports() {
  const toast = useToast()
  const nav = useNavigate()
  const refresh = useRefresh()
  const confirm = useConfirm()
  const [params, setParams] = useSearchParams()
  const [page, setPage] = useUrlNumber('page', 1)
  const [open, setOpen] = useState<Report | null>(null)
  // Which row is being fetched — a single global flag spun every row at once.
  const [openingId, setOpeningId] = useState<number | null>(null)

  // builder
  const [newOpen, setNewOpen] = useState(false)
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT)
  const [creating, setCreating] = useState(false)
  const [matches, setMatches] = useState<number | null>(null)
  const [counting, setCounting] = useState(false)

  const anyRunning = (items: Report[] | undefined) =>
    (items ?? []).some((r) => r.status === 'queued' || r.status === 'running')

  const q = useGet<{ items: Report[]; total: number }>(
    ['reports', page], `/intel/reports?page=${page}&limit=${LIMIT}`,
    { refetchInterval: 10_000 })

  async function openReport(id: number) {
    setOpeningId(id)
    try {
      const rep = await api<Report>(`/intel/reports/${id}`)
      setOpen(rep)
    } catch (e: any) {
      toast(`Failed to open: ${e?.message ?? e}`, 'error')
    } finally {
      setOpeningId(null)
    }
  }

  // deep link: /reports?open=<id> (used by the "report ready" notification)
  useEffect(() => {
    const id = params.get('open')
    if (id) {
      openReport(Number(id))
      params.delete('open')
      setParams(params, { replace: true })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /* ------------------------------------------------------------- builder */

  const filters = useMemo(() => draftToFilters(draft), [draft])
  // Debounced on the whole filter, not just the text box: every control here
  // changes which calls are in scope, and generation is a background LLM job —
  // "0 calls match" has to be visible while choosing, not ten minutes later.
  const filterKey = useDebounced(JSON.stringify(filters), 350)
  useEffect(() => {
    if (!newOpen) return
    let alive = true
    setCounting(true)
    previewReport(JSON.parse(filterKey))
      .then((r) => { if (alive) setMatches(r.call_count) })
      .catch(() => { if (alive) setMatches(null) })
      .finally(() => { if (alive) setCounting(false) })
    return () => { alive = false }
  }, [filterKey, newOpen])

  async function generate() {
    setCreating(true)
    try {
      const res = await createReport({
        title: draft.title.trim() || undefined,
        query_prompt: draft.focus.trim() || undefined,
        filters,
      })
      toast(`Report #${res.id} queued — it will appear here shortly`, 'success')
      setNewOpen(false)
      setDraft(EMPTY_DRAFT)
      refresh(['reports'])
    } catch (e: any) {
      toast(`Report failed: ${e?.message ?? e}`, 'error')
    } finally {
      setCreating(false)
    }
  }

  /* ------------------------------------------------------------- exports */

  /** Every export is built server-side — a watermarked .docx, a formatted
   *  .xlsx and the provenance-stamped .md/.txt cannot be produced from a Blob
   *  in the browser. Same-origin link, so the session cookie authenticates. */
  function download(rep: Report, fmt: string) {
    const a = document.createElement('a')
    a.href = `/api/intel/reports/${rep.id}/export.${fmt}`
    a.rel = 'noopener'
    a.click()
  }

  async function remove(rep: Report) {
    const ok = await confirm({
      title: 'Delete report?',
      description: `“${rep.title}” will be removed permanently. The calls it was built from are not affected.`,
      confirmLabel: 'Delete report',
      destructive: true,
    })
    if (!ok) return
    try {
      await del(`/intel/reports/${rep.id}`)
      toast('Report deleted', 'success')
      refresh(['reports'])
    } catch (e: any) {
      toast(`Failed: ${e?.message ?? e}`, 'error')
    }
  }

  const html = useMemo(() => (open?.document ? mdToHtml(open.document) : ''), [open])
  const items = q.data?.items ?? []
  const pages = Math.max(1, Math.ceil((q.data?.total ?? 0) / LIMIT))

  return (
    <div className="space-y-6">
      <PageHeader title="Reports"
        sub="AI-synthesized documents built from a set of calls."
        actions={
          <>
            <Button onClick={() => setNewOpen(true)}>
              <Plus size={14} /> New report
            </Button>
            <Button variant="ghost" onClick={() => refresh(['reports'])}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </>
        } />

      <Card className="p-5">
        <AsyncState loading={q.isLoading} error={q.error}>
          {items.length === 0 ? (
            <EmptyState
              icon={<FileText size={20} />}
              title="No reports yet"
              hint="Describe a set of calls — a date range, sentiment, follow-up state — and the AI writes an executive summary, findings and recommended actions from their stored intelligence."
              action={<Button onClick={() => setNewOpen(true)}><Plus size={15} /> New report</Button>}
            />
          ) : (
            <>
              <Table>
                <THead>
                  <TH>Title</TH>
                  <TH>Covers</TH>
                  <TH>Calls</TH>
                  <TH>Status</TH>
                  <TH>Created</TH>
                  <TH className="text-right">Actions</TH>
                </THead>
                {/* Only finished reports open — a queued row must not offer a
                    pointer cursor and a focus stop that lead nowhere. */}
                <tbody>
                  {items.map((r) => (
                    <TR key={r.id}
                      onClick={r.status === 'done' ? () => openReport(r.id) : undefined}>
                      <TD className="font-medium text-[var(--text)]">{r.title}</TD>
                      <TD className="max-w-[240px] truncate text-dim"
                        title={r.query_prompt ?? ''}>{r.query_prompt || '—'}</TD>
                      <TD className="tabular-nums">{r.call_count}</TD>
                      <TD>
                        <span className="inline-flex items-center gap-1.5">
                          <StatusBadge value={r.status} />
                          {(r.status === 'queued' || r.status === 'running') && <Spinner className="h-3 w-3" />}
                        </span>
                        {r.error && <div className="mt-0.5 max-w-[220px] truncate text-2xs text-rose-500"
                          title={r.error}>{r.error}</div>}
                      </TD>
                      <TD className="whitespace-nowrap tabular-nums">
                        {fmtDateTime(r.created_at)}
                      </TD>
                      <TD className="text-right">
                        {/* The row itself opens the report; actions must not double-fire. */}
                        <div className="inline-flex gap-1" onClick={(e) => e.stopPropagation()}>
                          {r.status === 'done' && (
                            <IconButton label={`Open “${r.title}”`}
                              onClick={() => openReport(r.id)}>
                              {openingId === r.id ? <Spinner className="h-3.5 w-3.5" /> : <FileText size={14} />}
                            </IconButton>
                          )}
                          <IconButton label={`Delete “${r.title}”`} onClick={() => remove(r)}>
                            <Trash2 size={14} />
                          </IconButton>
                        </div>
                      </TD>
                    </TR>
                  ))}
                </tbody>
              </Table>
              <Pagination page={page} pages={pages} total={q.data?.total} onPage={setPage} />
            </>
          )}
        </AsyncState>
        {anyRunning(items) && (
          <div className="mt-3 text-xs text-dim">
            Reports generate in the background — this list refreshes automatically.
          </div>
        )}
      </Card>

      {/* ---------------------------------------------------------- builder */}
      <Modal open={newOpen} onClose={() => setNewOpen(false)} wide
        title="New report"
        footer={
          <>
            <Button variant="ghost" onClick={() => setNewOpen(false)}>Cancel</Button>
            <Button onClick={generate} disabled={creating || matches === 0}>
              {creating ? <Spinner /> : <Sparkles size={14} />} Generate
            </Button>
          </>
        }>
        <div className="space-y-4">
          <Field label="Report title">
            <Input value={draft.title} onChange={(e) => setDraft({ ...draft, title: e.target.value })}
              placeholder="Call intelligence report" />
          </Field>

          <div>
            <div className="eyebrow mb-2">Which calls</div>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Period">
                <Select value={draft.days} onChange={(e) => setDraft({ ...draft, days: e.target.value })}>
                  <option value="7">Last 7 days</option>
                  <option value="30">Last 30 days</option>
                  <option value="90">Last 90 days</option>
                  <option value="180">Last 6 months</option>
                  <option value="365">Last 12 months</option>
                  <option value="0">All time</option>
                </Select>
              </Field>
              <Field label="Direction">
                <Select value={draft.direction} onChange={(e) => setDraft({ ...draft, direction: e.target.value })}>
                  <option value="">Inbound & outbound</option>
                  <option value="inbound">Inbound only</option>
                  <option value="outbound">Outbound only</option>
                </Select>
              </Field>
              <Field label="Sentiment">
                <Select value={draft.sentiment} onChange={(e) => setDraft({ ...draft, sentiment: e.target.value })}>
                  <option value="">Any sentiment</option>
                  <option value="positive">Positive</option>
                  <option value="neutral">Neutral</option>
                  <option value="negative">Negative</option>
                </Select>
              </Field>
              <Field label="Follow-up">
                <Select value={draft.decision} onChange={(e) => setDraft({ ...draft, decision: e.target.value })}>
                  <option value="">Any follow-up state</option>
                  <option value="yes">Follow-up: yes</option>
                  <option value="no">Follow-up: no</option>
                  <option value="unsure">Needs review</option>
                </Select>
              </Field>
              <Field label="Lead score">
                <Select value={draft.minLead} onChange={(e) => setDraft({ ...draft, minLead: e.target.value })}>
                  <option value="0">Any lead score</option>
                  <option value="40">Lead ≥ 40</option>
                  <option value="70">Lead ≥ 70 (hot)</option>
                </Select>
              </Field>
              <Field label="Most calls to include">
                <Select value={draft.limit} onChange={(e) => setDraft({ ...draft, limit: e.target.value })}>
                  <option value="50">50 (fastest)</option>
                  <option value="200">200</option>
                  <option value="500">500</option>
                  <option value="1000">1000 (maximum)</option>
                </Select>
              </Field>
            </div>
            <div className="mt-3">
              <Field label="Contains (phone, name or summary text)">
                <Input value={draft.search} onChange={(e) => setDraft({ ...draft, search: e.target.value })}
                  placeholder="Optional — e.g. a project name or a number" />
              </Field>
            </div>

            {/* The count is the server's own resolution of this filter. */}
            <div className="mt-3 flex items-center gap-2 rounded-xl bg-[var(--surface-2)] px-3 py-2 text-xs">
              {counting ? (
                <><Spinner className="h-3 w-3" /> <span className="text-dim">Counting matching calls…</span></>
              ) : matches == null ? (
                <span className="text-dim">Could not count matching calls — Generate will still validate.</span>
              ) : matches === 0 ? (
                <span className="font-medium text-amber-600 dark:text-amber-300">
                  No analyzed calls match — widen the period or clear a filter.
                </span>
              ) : (
                <span>
                  <span className="font-semibold tabular-nums text-[var(--text)]">{matches}</span>
                  <span className="text-dim"> analyzed call{matches === 1 ? '' : 's'} will be synthesized</span>
                </span>
              )}
              <button type="button" onClick={() => { setNewOpen(false); nav('/intelligence') }}
                className={cx('ml-auto shrink-0 text-xs font-medium text-dim underline-offset-2 hover:text-soft hover:underline')}>
                Pick individual calls instead
              </button>
            </div>
          </div>

          <Field label="Focus (optional)">
            <Textarea rows={3} value={draft.focus}
              onChange={(e) => setDraft({ ...draft, focus: e.target.value })}
              placeholder="What should the report concentrate on? e.g. “Why site-visit bookings dropped, and which objections came up most.”" />
          </Field>

          <p className="text-xs text-dim">
            The AI synthesizes the selected calls&apos; stored intelligence into a
            structured document (executive summary, findings, recommended
            actions). It runs in the background — you&apos;ll get a notification and
            it appears in the list above, exportable as Word, Markdown, text or
            an analysis workbook.
          </p>
        </div>
      </Modal>

      {/* ---------------------------------------------------------- reader */}
      <FullscreenModal open={!!open} onClose={() => setOpen(null)} scope="shell"
        title={open?.title}
        headerExtra={open ? (
          <div className="flex flex-wrap items-center gap-2">
            {EXPORTS.map((e) => (
              <Button key={e.fmt} variant="ghost" size="sm" onClick={() => download(open, e.fmt)}>
                <Download size={13} /> {e.label}
              </Button>
            ))}
          </div>
        ) : undefined}>
        <div className="report-doc mx-auto max-w-3xl px-1 py-2"
          dangerouslySetInnerHTML={{ __html: html }} />
      </FullscreenModal>
    </div>
  )
}
