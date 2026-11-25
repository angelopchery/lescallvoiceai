/**
 * Intelligence — the unified post-call dashboard. Every call is auto-analyzed
 * by the pipeline; this page is the source of truth: KPI band, a natural-
 * language semantic filter ("calls with complaints about product quality"),
 * structured filters, the master table, row selection → Generate Report or
 * Export to Excel.
 * Structured filters, page AND the active AI filter (`?ai=`) live in the URL.
 * The AI filter's judged rows and the ticked selection live in
 * `lib/intelBrowse`, because opening a call unmounts this page: held in
 * component state they were destroyed on every row click, and working through
 * a filtered list meant re-typing and re-running the filter once per call.
 *
 * The smart filter is VERIFIED: the server retrieves candidates, then has a
 * small model judge each one against an explicit requirement list and quote
 * the line that qualifies it. That changes what this page has to show — the
 * requirements it checked, how many calls it read, and the quoted evidence
 * per row — because an empty result is now a real answer ("none of the 103
 * calls we read match") rather than a failure to find anything.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useNavigationType, useSearchParams } from 'react-router-dom'
import { useLenis } from 'lenis/react'
import {
  BrainCircuit, CloudUpload, FileSpreadsheet, FileText, History, Quote,
  RefreshCw, ShieldCheck, Sparkles, X,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import {
  searchIntel, createReport, createExport, callIdsMatching, type CallExport,
  type IntelCallRow, type Verification,
} from '../api/intel'
import { Badge, Button, Card, EmptyState, Spinner } from '../components/ui'
import {
  AsyncState, ContactLabel, MultiSelect, PageHeader, Pagination, SearchInput,
  Segmented, Select, StatusBadge, Table, TD, TH, THead, TR, Field, Input,
} from '../components/kit'
import { ScoreCell } from '../components/insight'
import { Checkbox } from '@/components/ui/checkbox'
import { Modal } from '../components/Modal'
import { StatCard } from '../components/StatCard'
import { useToast } from '../components/Toast'
import { useDebounced, useUrlNumber, useUrlParam, useUrlPatch } from '../lib/hooks'
import { fmtDateTime, fmtTime } from '../lib/format'
import {
  consumeReturn, loadAiFilter, loadLastAiFilter, saveAiFilter, loadSelection,
  saveSelection, loadSequence, saveSequence, pageOf, positionOf,
  type AiFilterSnapshot,
} from '../lib/intelBrowse'

const LIMIT = 50
/** How many MATCHED calls an AI filter may return. The list below pages
 *  through them LIMIT at a time; this is not a page size. Sized to the
 *  server's candidate pool (INTEL_FILTER_MAX_CANDIDATES) so the only ceiling
 *  left is retrieval's, which the server reports via `pool_full`. */
const NL_MAX = 300
/** Server ceiling on one report (`_REPORT_MAX_CALLS`). Export's is higher, so
 *  a selection can be legal to export and too large to synthesize. */
const REPORT_MAX = 1000
/** A restored AI-filter result older than this says when it was taken. */
const STALE_AFTER_MS = 10 * 60_000
const DAYS_VALUES = ['7', '30', '90', '0'] as const
type Days = (typeof DAYS_VALUES)[number]

/* Slicing the raw string used to print the stored UTC clock verbatim; every
   call time on this page read hours off. fmtDateTime renders the instant in the
   viewer's zone — see lib/format.ts. */
const fmtWhen = (ts?: string | null) => fmtDateTime(ts)

export default function Intelligence() {
  const nav = useNavigate()
  const toast = useToast()
  const refresh = useRefresh()

  // Structured filters — URL-persisted (refresh/share keeps them).
  const [page, setPage] = useUrlNumber('page', 1)
  const [search, setSearch] = useUrlParam('search')
  const [rawDays, setDays] = useUrlParam('days', '30')
  const days: Days = (DAYS_VALUES as readonly string[]).includes(rawDays) ? (rawDays as Days) : '30'
  const [sentiment, setSentiment] = useUrlParam('sentiment')
  const [decision, setDecision] = useUrlParam('decision')
  const [minLead, setMinLead] = useUrlParam('minLead', '0')
  const [agent, setAgent] = useUrlParam('agent')
  // WHICH AI filter is active. The URL owns it, so Back, a refresh and a
  // pasted link all reopen the same filtered view.
  const [aiParam] = useUrlParam('ai')
  const patchUrl = useUrlPatch()
  const [params, setParams] = useSearchParams()
  const navType = useNavigationType()
  const lenis = useLenis()

  const debSearch = useDebounced(search, 300)
  const hasFilters = Boolean(search) || sentiment !== '' || decision !== ''
    || minLead !== '0' || days !== '30' || agent !== ''

  // Filter changes reset the page. Done as an effect (not a second setter call
  // in the same handler) because react-router setSearchParams calls clobber
  // each other within one tick; skip the first run so deep links keep ?page=.
  const firstRun = useRef(true)
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return }
    if (page !== 1) setPage(1)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [debSearch, days, sentiment, decision, minLead, agent])

  function resetFilters() {
    setParams((prev) => {
      const next = new URLSearchParams(prev)
      for (const k of ['search', 'days', 'sentiment', 'decision', 'minLead', 'agent', 'page']) next.delete(k)
      return next
    }, { replace: true })
  }

  // Natural-language semantic filter (server-side hybrid retrieval). `nl` is
  // one completed pass, restored from lib/intelBrowse when this page remounts
  // — it is NOT re-run on Back. `nlQuery` is only the text box's draft.
  const [nl, setNl] = useState<AiFilterSnapshot | null>(() => loadAiFilter(aiParam, agent))
  const [nlQuery, setNlQuery] = useState(aiParam)
  const [nlBusy, setNlBusy] = useState(false)
  const nlSeq = useRef(0)
  const nlInflight = useRef('')
  const nlRows: IntelCallRow[] | null = nl?.rows ?? null
  const nlActive = nl?.query ?? ''
  const nlApplied = nl?.applied ?? null
  const nlVerify: Verification | null = nl?.verification ?? null
  const nlVerified = nl?.verified ?? false
  const nlDegraded = nl?.degraded ?? false
  /** The URL names a filter whose rows are not here yet (a pasted link, or a
   *  tab whose stored result is gone). The table must wait for it rather than
   *  flash the unfiltered list under a filter the URL says is active. */
  const nlPending = Boolean(aiParam) && !nl

  /** The call last opened from this list — marked in the table so a return
   *  answers "where was I?" without the user counting rows. */
  const [lastViewed, setLastViewed] = useState<string | null>(
    () => loadSequence()?.lastViewed ?? null)

  const [selectAllBusy, setSelectAllBusy] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  const [reportTitle, setReportTitle] = useState('')
  const [reportBusy, setReportBusy] = useState(false)
  const [exportOpen, setExportOpen] = useState(false)
  const [exportTitle, setExportTitle] = useState('')
  const [exportBusy, setExportBusy] = useState(false)
  const [exportDone, setExportDone] = useState<CallExport | null>(null)

  const qs = useMemo(() => {
    const p = new URLSearchParams({ page: String(page), limit: String(LIMIT) })
    if (debSearch) p.set('search', debSearch)
    if (days !== '0') p.set('days', days)
    if (sentiment) p.set('sentiment', sentiment)
    if (decision) p.set('decision', decision)
    if (minLead !== '0') p.set('min_lead', minLead)
    if (agent) p.set('agent', agent)
    return p.toString()
  }, [page, debSearch, days, sentiment, decision, minLead, agent])

  /** The same predicates WITHOUT page/limit — what "all matches" resolves to
   *  on the server. Kept separate from `qs` so the ids call can never inherit
   *  the page window that caused the 50-row ceiling. */
  const filterQs = useMemo(() => {
    const p = new URLSearchParams()
    if (debSearch) p.set('search', debSearch)
    if (days !== '0') p.set('days', days)
    if (sentiment) p.set('sentiment', sentiment)
    if (decision) p.set('decision', decision)
    if (minLead !== '0') p.set('min_lead', minLead)
    if (agent) p.set('agent', agent)
    return p.toString()
  }, [debSearch, days, sentiment, decision, minLead, agent])

  // A selection belongs to the filter that produced it. Ticks already survived
  // a filter change, which was survivable while "all" meant at most one page —
  // with "Select all N matches" a stale 246-row selection would quietly export
  // the PREVIOUS filter's calls. Paging does not clear it: `filterQs` has no
  // page in it, which is the point of keeping it separate from `qs`.
  //
  // It is also RESTORED under that same key. Ticks were component state, so
  // opening one call to check it before exporting wiped every tick made so far.
  const selectionKey = aiParam ? `ai:${aiParam}|${agent}` : `list:${filterQs}`
  const [selected, setSelected] = useState<Set<number>>(
    () => new Set(loadSelection(selectionKey)?.ids ?? []))
  const [selectionCapped, setSelectionCapped] = useState(
    () => loadSelection(selectionKey)?.capped ?? false)
  const selKeyRef = useRef(selectionKey)
  // Order matters: persist runs BEFORE the clear below. On a key change it
  // sees the ref still holding the old key and skips, so the previous
  // filter's ticks are never written under the new filter's key.
  useEffect(() => {
    if (selKeyRef.current !== selectionKey) return
    saveSelection({ key: selectionKey, ids: [...selected], capped: selectionCapped })
  }, [selected, selectionCapped, selectionKey])
  useEffect(() => {
    if (selKeyRef.current === selectionKey) return
    selKeyRef.current = selectionKey
    setSelected(new Set())
    setSelectionCapped(false)
  }, [selectionKey])

  const list = useGet<{ items: IntelCallRow[]; total: number }>(
    ['intel-calls', qs], `/intel/calls?${qs}`, { refetchInterval: 30_000 })
  const analytics = useGet<any>(['intel-analytics', days, agent],
    `/intel/analytics?days=${days === '0' ? 365 : days}`
    + (agent ? `&agent=${encodeURIComponent(agent)}` : ''))
  const status = useGet<any>(['intel-status'], '/intel/status',
    { refetchInterval: 30_000 })

  /** Run a judged pass. `fromUrl` = the URL already names this filter (a
   *  pasted link, a lost snapshot, an agent-scope change) rather than the
   *  user pressing the button. */
  async function runNlSearch(fromUrl?: string) {
    const q = (fromUrl ?? nlQuery).trim()
    if (!q) return
    const scope = agent
    const flight = `${q}|${scope}`
    // StrictMode double-fires the restoring effect in dev; one pass reads up
    // to 300 calls with a model, so an identical request in flight is reused.
    if (fromUrl != null && nlInflight.current === flight) return
    nlInflight.current = flight
    const seq = ++nlSeq.current
    setNlBusy(true)
    try {
      const res = await searchIntel(q, NL_MAX, scope)
      if (seq !== nlSeq.current) return
      const snap: AiFilterSnapshot = {
        query: q, agent: scope, rows: res.items,
        applied: res.applied ?? null,
        verification: res.verification ?? null,
        verified: Boolean(res.verified), degraded: res.degraded,
        at: Date.now(),
      }
      saveAiFilter(snap)
      // A fresh pass is a fresh list: the previous walk-through no longer
      // describes it, and its "you were here" row may not even be in it.
      saveSequence(null)
      setLastViewed(null)
      setNl(snap)
      setSelected(new Set()); setSelectionCapped(false)
      // One navigation. Two setters in a tick clobber each other (lib/hooks).
      patchUrl({ ai: q, page: null })
    } catch (e: any) {
      toast(`Search failed: ${e?.message ?? e}`, 'error')
      // A filter the URL names but that cannot be produced must not leave the
      // table waiting forever. The phrase stays in the box for a retry.
      if (fromUrl != null) patchUrl({ ai: null, page: null })
    } finally {
      if (nlInflight.current === flight) nlInflight.current = ''
      if (seq === nlSeq.current) setNlBusy(false)
    }
  }

  function clearNl() {
    saveAiFilter(null)
    saveSequence(null)
    setLastViewed(null)
    setNl(null); setNlQuery('')
    // Selection clears itself: `selectionKey` changes with `ai`.
    // Both lists page through the same `page`, so leaving it on 3 would drop
    // the user onto page 3 of the unfiltered list.
    patchUrl({ ai: null, page: null })
  }

  // The URL is the source of truth for WHICH filter is active; make `nl`
  // agree with it. Covers: Back/Forward across a filter change, a pasted
  // link, a refresh in a tab whose stored result is gone, and the agent
  // dropdown changing under an active filter (its rows were judged for the
  // old scope, while every other panel on this page already followed the
  // new one).
  useEffect(() => {
    if (!aiParam) {
      if (nl) setNl(null)
      return
    }
    if (nl && nl.query === aiParam && nl.agent === agent) return
    setNlQuery(aiParam)
    const stored = loadAiFilter(aiParam, agent)
    if (stored) { setNl(stored); return }
    void runNlSearch(aiParam)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [aiParam, agent])

  /** A finished filter from earlier in this tab that the URL no longer names
   *  — the user left via the sidebar and came back to a bare /intelligence.
   *  Offered, never applied: a sidebar click should open the default view. */
  const resumable = useMemo(
    () => (aiParam ? null : loadLastAiFilter()),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [aiParam, nl])

  // AI-filter results arrive as one complete set and are paged CLIENT-side.
  // They used to be capped at 50 and rendered as a single page, which made a
  // 98-call answer look like a 50-call one — and, because Export/Report can
  // only ever send the ticked rows, made every workbook built from such a
  // filter short by the difference.
  const total = nlRows ? nlRows.length : list.data?.total ?? 0
  const pages = Math.max(1, Math.ceil(total / LIMIT))
  const rows: IntelCallRow[] = nlRows
    ? nlRows.slice((page - 1) * LIMIT, page * LIMIT)
    : list.data?.items ?? []

  /** Open a call AND hand the call page this list's order, so it can step to
   *  the next one without a trip back here. An AI filter is already the whole
   *  result; the structured list is one server page of it, which the call
   *  page extends from `filterQs` when the user walks past either end. */
  function openCall(r: IntelCallRow) {
    if (!r.call_key) return
    const here = new URLSearchParams(params)
    here.delete('page')
    // Keys stay index-aligned with the rows ('' for a row with no key) so a
    // position maps to the same page on both sides.
    const keysOf = (rs: IntelCallRow[]) => rs.map((x) => x.call_key ?? '')
    saveSequence(nlRows
      ? {
        keys: keysOf(nlRows), offset: 0, total: nlRows.length, pageSize: LIMIT,
        listSearch: here.toString(), filterQs: null,
        label: `“${nlActive}”`, lastViewed: r.call_key,
      }
      : {
        keys: keysOf(rows), offset: (page - 1) * LIMIT, total, pageSize: LIMIT,
        listSearch: here.toString(), filterQs,
        label: hasFilters ? 'the filtered calls' : 'all calls',
        lastViewed: r.call_key,
      })
    nav(`/intelligence/${r.call_key}`)
  }

  // Is this mount the user coming back from a call opened from THIS list?
  // Decided once, from the flag the call page set — not from the navigation
  // type, which is POP for a refresh too (see CallSequence.pendingReturn).
  const [returning] = useState(() => {
    const seq = loadSequence()
    if (!seq?.pendingReturn || !seq.lastViewed) return false
    const here = new URLSearchParams(params)
    here.delete('page')
    return here.toString() === seq.listSearch       // else: a different list
  })

  // Back leaves the URL on the page the user LEFT from, but Previous/Next may
  // have carried them onto another. Land on the page holding the call they
  // were last reading. Only for Back/Forward: the breadcrumb's URL already
  // names the right page, and a sidebar click is a fresh visit to page 1.
  const returned = useRef(false)
  useEffect(() => {
    if (returned.current || !returning) return
    if (nlPending) return                       // rows not here yet
    returned.current = true
    const seq = loadSequence()
    consumeReturn()
    if (!seq?.lastViewed || navType !== 'POP') return
    const pos = nlRows
      ? nlRows.findIndex((x) => x.call_key === seq.lastViewed)
      : positionOf(seq, seq.lastViewed)
    if (pos >= 0 && pageOf(seq, pos) !== page) setPage(pageOf(seq, pos))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nlPending, returning, navType])

  // …and bring that row into view. Deferred a frame: AppShell's effect resets
  // scroll to the top on every route change and, being the parent's, runs
  // after this one.
  const scrolled = useRef(false)
  const lastViewedOnPage = Boolean(
    lastViewed && rows.some((x) => x.call_key === lastViewed))
  useEffect(() => {
    if (scrolled.current || !returning || !lastViewedOnPage) return
    const t = window.setTimeout(() => {
      // Flagged here, not before the timer: a re-run (Lenis arriving a render
      // late) cancels the pending timer, and must be allowed to set a new one.
      scrolled.current = true
      const el = document.querySelector<HTMLElement>('tr[aria-current="true"]')
      if (!el) return
      if (lenis) lenis.scrollTo(el, { offset: -160, immediate: true, force: true })
      else el.scrollIntoView({ block: 'center' })
    }, 80)
    return () => window.clearTimeout(t)
  }, [lastViewedOnPage, returning, lenis])

  function toggle(id: number) {
    setSelected((s) => {
      const n = new Set(s)
      n.has(id) ? n.delete(id) : n.add(id)
      return n
    })
  }
  const allChecked = rows.length > 0 && rows.every((r) => selected.has(r.call_id))
  // Every row the filter matched, across pages. Export and Report can only send
  // ticked ids, so without this a 98-call answer could only be exported by
  // paging through it and ticking each page in turn.
  //
  // This used to exist for AI-filter results ONLY, because those arrive as one
  // complete client-side set. A STRUCTURED filter is server-paged at 50, so it
  // had no way to say "all of them" at all — the ceiling the AI filter shed on
  // 2026-08-29 was still fully in place on the ordinary list. The ids now come
  // from /intel/calls/ids, which returns the whole matching set and no rows.
  const everyMatchSelected = Boolean(
    total > 0 && (nlRows
      ? nlRows.every((r) => selected.has(r.call_id))
      : selected.size >= total))

  async function toggleSelectAll() {
    if (everyMatchSelected) {
      setSelected(new Set()); setSelectionCapped(false); return
    }
    if (nlRows) { setSelected(new Set(nlRows.map((r) => r.call_id))); return }
    setSelectAllBusy(true)
    try {
      const res = await callIdsMatching(filterQs)
      setSelected(new Set(res.call_ids))
      setSelectionCapped(res.capped)
      if (res.capped) {
        toast(`Selected ${res.count} calls — this filter matches more than the `
          + `${res.max} one export can hold. Narrow it to cover the rest.`, 'error')
      }
    } catch (e: any) {
      toast(`Could not select every match: ${e?.message ?? e}`, 'error')
    } finally {
      setSelectAllBusy(false)
    }
  }

  async function generateReport() {
    setReportBusy(true)
    try {
      const res = await createReport({
        title: reportTitle || undefined,
        query_prompt: nlActive || search || undefined,
        call_ids: [...selected],
      })
      toast(`Report #${res.id} queued — it will appear in Reports shortly`, 'success')
      setReportOpen(false)
      setReportTitle('')
      setSelected(new Set()); setSelectionCapped(false)
      refresh(['reports'])
    } catch (e: any) {
      toast(`Report failed: ${e?.message ?? e}`, 'error')
    } finally {
      setReportBusy(false)
    }
  }

  async function runExport() {
    setExportBusy(true)
    try {
      const res = await createExport({
        title: exportTitle || undefined,
        // The phrase the judge was given, which is what the verdicts are
        // stored under — `nlActive` still contains any "...using agent X"
        // constraint the server lifted out, and looking evidence up by it
        // returned nothing, shipping the workbook unverified. The server
        // falls back too; sending the right value keeps it from having to.
        query: nlApplied?.query || nlActive || search || undefined,
        call_ids: [...selected],
        criterion: nlVerify?.criterion ?? undefined,
      })
      setExportDone(res)
      toast(res.storage === 'gcs'
        ? `Workbook built and stored in Google Cloud (${res.call_count} calls)`
        : `Workbook built (${res.call_count} calls) — cloud upload failed, `
          + 'the download link still works', res.storage === 'gcs' ? 'success' : 'error')
    } catch (e: any) {
      toast(`Export failed: ${e?.message ?? e}`, 'error')
    } finally {
      setExportBusy(false)
    }
  }

  function closeExport() {
    setExportOpen(false); setExportDone(null); setExportTitle('')
  }

  const sum = analytics.data?.summary

  const agentOptions: string[] = analytics.data?.agent_options ?? []
  // `agent` stays a CSV in the URL so old single-agent links keep working.
  const agents = useMemo(
    () => agent.split(',').map((a) => a.trim()).filter(Boolean), [agent])
  const cov = status.data?.coverage

  return (
    <div className="space-y-6">
      <PageHeader
        title="Intelligence"
        sub="Every call is analyzed automatically — no manual step. Filter in plain language, inspect, act."
        actions={
          <>
            {total > rows.length && (
              <Button variant="ghost" size="sm" onClick={toggleSelectAll}
                disabled={selectAllBusy}>
                {selectAllBusy ? <Spinner className="h-3.5 w-3.5" /> : null}
                {everyMatchSelected
                  ? 'Clear selection'
                  : `Select all ${total} matches`}
              </Button>
            )}
            {selected.size > 0 && (
              <>
                <Button variant="ghost" onClick={() => setExportOpen(true)}>
                  <FileSpreadsheet size={14} /> Export to Excel ({selected.size})
                </Button>
                <Button onClick={() => setReportOpen(true)}>
                  <FileText size={14} /> Generate report ({selected.size})
                </Button>
              </>
            )}
            <Button variant="ghost" onClick={() => refresh(['intel-calls'], ['intel-analytics'], ['intel-status'])}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow">Agent</span>
        <MultiSelect
          options={agentOptions}
          value={agents}
          onChange={(next) => setAgent(next.join(','))}
          allLabel="All agents"
          label="Filter every panel by agent"
          summary={(n) => `${n} agents`}
          className="w-56" />
        {agents.length > 0 && (
          <Button variant="ghost" size="sm" onClick={() => setAgent('')}>
            <X size={13} /> All agents
          </Button>
        )}
        <span className="ml-auto text-xs text-dim">
          {agents.length === 0
            ? 'Showing every agent on the account'
            : `Scores, topics, trends and calls below are ${
                agents.length === 1 ? agents[0] : `${agents.join(', ')}`} only`}
        </span>
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <StatCard label="Calls analyzed" value={sum?.analyzed ?? '—'}
          sub={cov ? `${cov.analyzed}/${cov.calls} lifetime coverage` : undefined}
          icon={<BrainCircuit size={18} />} iconTone="mint" />
        <StatCard label="Hot leads" value={sum?.hot_leads ?? '—'}
          sub="lead score ≥ 70" iconTone="rose"
          icon={<Sparkles size={18} />} />
        <StatCard label="Avg lead score" value={sum?.avg_lead ?? '—'}
          radial={sum?.avg_lead != null ? { value: sum.avg_lead, max: 100 } : undefined} />
        <StatCard label="Negative calls" value={sum?.negative ?? '—'}
          sub={sum ? `${sum.positive} positive · ${sum.neutral} neutral` : undefined}
          iconTone="amber" icon={<X size={18} />} />
      </div>

      <Card className="p-5">
        <div className="flex flex-col gap-2 sm:flex-row">
          <SearchInput
            value={nlQuery}
            onChange={setNlQuery}
            onEnter={runNlSearch}
            icon={<Sparkles size={15} />}
            placeholder='Describe the calls you want — e.g. "3BHK flat enquiries" or "complaints about product quality"'
            ariaLabel="Semantic call search"
            className="flex-1"
          />
          <Button onClick={runNlSearch} disabled={nlBusy || !nlQuery.trim()}>
            {nlBusy ? <Spinner /> : <Sparkles size={14} />} Smart filter
          </Button>
        </div>

        {nlRows ? (
          <div className="mt-3 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-xs text-dim">
                <span className="font-semibold tabular-nums text-soft">{nlRows.length}</span>
                {' '}call(s) matching “{nlActive}”
                {nlApplied?.agent && ` · agent ${nlApplied.agent}`}
                {nlApplied && nlApplied.query !== nlActive
                  && ` · searched for “${nlApplied.query}”`}
                {nlDegraded && ' · semantic engine unavailable — keyword results only'}
                {/* A restored result is a snapshot: calls that ended since are
                    not in it. Say when it was taken instead of re-running a
                    judged pass — and reshuffling the list — behind the user. */}
                {nl && Date.now() - nl.at > STALE_AFTER_MS
                  && ` · as of ${fmtTime(nl.at)} — press Smart filter to include newer calls`}
              </span>
              <Button variant="ghost" size="sm" onClick={clearNl}>
                <X size={13} /> Clear AI filter
              </Button>
            </div>

            {/* What the filter actually checked. Without this the user cannot
                tell a precise empty result from a broken search — which is the
                whole difference this feature makes. */}
            {nlVerified && nlVerify?.criterion ? (
              <div className="rounded-2xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge tone="mint" soft>
                    <ShieldCheck size={12} /> AI-verified
                  </Badge>
                  <span className="text-xs text-dim">
                    read{' '}
                    <span className="tabular-nums text-soft">{nlVerify.examined}</span>
                    {' '}of{' '}
                    <span className="tabular-nums text-soft">{nlVerify.candidates}</span>
                    {' '}candidate call(s) ·{' '}
                    <span className="tabular-nums text-soft">{nlVerify.matched}</span>
                    {' '}matched
                    {nlVerify.cached > 0 && ` · ${nlVerify.cached} from cache`}
                    {nlVerify.model && ` · ${nlVerify.model}`}
                  </span>
                </div>
                {nlVerify.criterion.restated && (
                  <p className="mt-2 text-xs text-soft">
                    Read as: {nlVerify.criterion.restated}
                  </p>
                )}
                {nlVerify.criterion.requirements.length > 0 && (
                  <ul className="mt-1.5 space-y-0.5">
                    {nlVerify.criterion.requirements.map((r, i) => (
                      <li key={i} className="text-2xs text-dim">
                        <span className="text-mint-600 dark:text-mint-400">✓</span> every result had to: {r}
                      </li>
                    ))}
                  </ul>
                )}
                {nlVerify.truncated && (
                  <p className="mt-2 text-2xs text-amber-700 dark:text-amber-300">
                    The time budget ran out before every candidate was read —
                    narrow the filter (pick an agent, or add a detail) to be sure
                    nothing was missed.
                  </p>
                )}
                {nlVerify.pool_full && (
                  <p className="mt-2 text-2xs text-amber-700 dark:text-amber-300">
                    Search filled its candidate pool ({nlVerify.candidates}), so
                    this may not be every match — treat {nlVerify.matched} as a
                    floor. Narrow the filter (pick an agent, or add a detail) to
                    see the rest.
                  </p>
                )}
              </div>
            ) : nlVerify?.enabled ? (
              <p className="text-2xs text-amber-700 dark:text-amber-300">
                The verifier could not run — these are similarity matches, not
                checked ones. Treat them as a starting point.
              </p>
            ) : null}
          </div>
        ) : nlPending ? (
          <p className="mt-3 flex items-center gap-2 text-xs text-dim" aria-live="polite">
            <Spinner className="h-3.5 w-3.5" />
            Reading calls against “{aiParam}” — each candidate is checked in
            full, so this can take up to a minute.
          </p>
        ) : (
          <div className="mt-3 flex flex-wrap items-center gap-2">
            {resumable && (
              <Button variant="soft" size="sm" className="max-w-full"
                onClick={() => patchUrl({
                  ai: resumable.query, agent: resumable.agent || null, page: null,
                })}>
                <History size={13} className="shrink-0" />
                <span className="truncate">
                  Resume “{resumable.query}” · {resumable.rows.length} call(s)
                </span>
              </Button>
            )}
            <SearchInput value={search} className="w-56"
              onChange={setSearch}
              placeholder="Phone / summary…" ariaLabel="Filter calls" />
            <Segmented
              options={[{ value: '7', label: '7d' }, { value: '30', label: '30d' },
                { value: '90', label: '90d' }, { value: '0', label: 'All' }]}
              value={days} onChange={(v) => setDays(v)} />
            <Select value={sentiment} onChange={(e) => setSentiment(e.target.value)}
              aria-label="Sentiment filter">
              <option value="">Any sentiment</option>
              <option value="positive">Positive</option>
              <option value="neutral">Neutral</option>
              <option value="negative">Negative</option>
            </Select>
            <Select value={decision} onChange={(e) => setDecision(e.target.value)}
              aria-label="Follow-up filter">
              <option value="">Any follow-up</option>
              <option value="yes">Follow-up: yes</option>
              <option value="no">Follow-up: no</option>
              <option value="unsure">Needs review</option>
            </Select>
            <Select value={minLead} onChange={(e) => setMinLead(e.target.value)}
              aria-label="Lead score filter">
              <option value="0">Any lead score</option>
              <option value="40">Lead ≥ 40</option>
              <option value="70">Lead ≥ 70 (hot)</option>
            </Select>
            {hasFilters && (
              <Button variant="ghost" size="sm" onClick={resetFilters}>
                <X size={13} /> Reset filters
              </Button>
            )}
            {!list.isLoading && (
              <span className="ml-auto text-xs tabular-nums text-dim">
                {total.toLocaleString('en-IN')} result{total === 1 ? '' : 's'}
              </span>
            )}
          </div>
        )}

        <div className="mt-4">
          <AsyncState loading={nlPending || (list.isLoading && !nlRows)}
            error={nlRows || nlPending ? undefined : list.error}>
            {rows.length === 0 ? (
              <EmptyState icon={<BrainCircuit size={22} />}
                title={nlRows
                  ? (nlVerified
                    ? `No call matches — ${nlVerify?.examined ?? 0} were read in full`
                    : 'No calls match the AI filter')
                  : hasFilters ? 'No analyzed calls match these filters'
                    : 'No calls analyzed yet'}
                hint={nlRows
                  ? (nlVerified
                    /* Say what was checked and that zero is the answer. A
                       verified empty result is information; presenting it as
                       "try rephrasing" would push the user back toward the
                       loose matching this filter exists to replace. */
                    ? `Every candidate was read against your filter and none satisfied it${
                      nlVerify?.criterion?.restated
                        ? ` — “${nlVerify.criterion.restated}”` : ''
                    }. That is the answer, not a miss. Rephrase only if you meant something different.`
                    : 'Try rephrasing — or clear the AI filter to browse all analyzed calls.')
                  : hasFilters
                    ? 'Widen the date range or clear a filter — new calls appear here automatically after they end.'
                    : 'Completed calls enter the intelligence pipeline automatically.'}
                action={nlRows
                  ? <Button variant="ghost" size="sm" onClick={clearNl}><X size={13} /> Clear AI filter</Button>
                  : hasFilters
                    ? <Button variant="ghost" size="sm" onClick={resetFilters}><X size={13} /> Reset filters</Button>
                    : undefined}
              />
            ) : (
              <>
                <Table>
                  <THead>
                    <TH className="w-8">
                      <Checkbox aria-label="Select all on this page"
                        checked={allChecked}
                        onCheckedChange={() => setSelected((prev) => {
                          // Adds/removes only THIS page. Replacing the whole
                          // set (as this did) silently dropped every tick made
                          // on another page, which matters now that a filter
                          // can return more rows than one page holds.
                          const n = new Set(prev)
                          for (const r of rows) {
                            allChecked ? n.delete(r.call_id) : n.add(r.call_id)
                          }
                          return n
                        })} />
                    </TH>
                    <TH>Customer</TH>
                    <TH>Date</TH>
                    <TH>Agent</TH>
                    <TH>Intent</TH>
                    <TH>Sentiment</TH>
                    <TH>Lead</TH>
                    <TH>Opp</TH>
                    <TH>Risk</TH>
                    <TH>Follow-up</TH>
                    <TH>Summary</TH>
                  </THead>
                  <tbody>
                    {rows.map((r) => (
                      <TR key={r.call_id}
                        current={Boolean(lastViewed) && r.call_key === lastViewed}
                        onClick={() => openCall(r)}>
                        <TD className="w-8">
                          <span onClick={(e) => e.stopPropagation()}
                            onKeyDown={(e) => e.stopPropagation()}>
                            <Checkbox aria-label={`Select call ${r.call_key}`}
                              checked={selected.has(r.call_id)}
                              onCheckedChange={() => toggle(r.call_id)} />
                          </span>
                        </TD>
                        <TD>
                          <ContactLabel name={r.contact_name} phone={r.phone} />
                          <div className="text-2xs text-dim">
                            {r.direction} · {Math.round(r.duration_s ?? 0)}s
                            {lastViewed && r.call_key === lastViewed && (
                              <span className="font-semibold text-mint-600 dark:text-mint-400">
                                {' '}· last opened
                              </span>
                            )}
                          </div>
                        </TD>
                        <TD className="whitespace-nowrap tabular-nums">{fmtWhen(r.started_at)}</TD>
                        <TD className="max-w-[110px] truncate" title={r.agent_slug ?? ''}>{r.agent_slug ?? '—'}</TD>
                        <TD className="max-w-[160px] truncate" title={r.customer_intent ?? ''}>
                          {r.customer_intent ?? '—'}
                        </TD>
                        <TD><StatusBadge value={r.sentiment} /></TD>
                        <TD><ScoreCell score={r.lead_score} /></TD>
                        <TD><ScoreCell score={r.opportunity_score} /></TD>
                        <TD><ScoreCell score={r.risk_score} invert /></TD>
                        <TD>
                          {r.followup_decision === 'yes'
                            ? <StatusBadge value={r.followup_status ?? 'scheduled'} />
                            : r.followup_decision === 'unsure'
                              ? <StatusBadge value="review" />
                              : <span className="text-dim">no</span>}
                        </TD>
                        <TD className="max-w-[320px]">
                          {r.match === 'verified' && r.match_evidence ? (
                            /* The words that qualified this row. Shown instead
                               of the summary because "why is this here?" is the
                               question a filtered list has to answer. */
                            <>
                              <span className="flex items-start gap-1 text-xs text-soft">
                                <Quote size={11} className="mt-0.5 shrink-0 text-mint-600 dark:text-mint-400" />
                                <span className="line-clamp-2">{r.match_evidence}</span>
                              </span>
                              {r.match_reason && (
                                <span className="mt-0.5 line-clamp-1 text-2xs text-dim"
                                  title={r.match_reason}>
                                  {r.match_reason}
                                </span>
                              )}
                            </>
                          ) : (
                            <span className="line-clamp-2 text-xs text-dim">
                              {r.snippet ?? r.summary ?? '—'}
                            </span>
                          )}
                        </TD>
                      </TR>
                    ))}
                  </tbody>
                </Table>
                <Pagination page={page} pages={pages} total={total} onPage={setPage} />
              </>
            )}
          </AsyncState>
        </div>
      </Card>

      <Modal open={exportOpen} onClose={closeExport}
        title={exportDone
          ? 'Workbook ready'
          : `Export ${selected.size} call(s) to Excel`}
        footer={exportDone
          ? <Button onClick={closeExport}>Done</Button>
          : (
            <>
              <Button variant="ghost" onClick={closeExport}>Cancel</Button>
              <Button onClick={runExport} disabled={exportBusy}>
                {exportBusy ? <Spinner /> : <CloudUpload size={14} />} Build &amp; upload
              </Button>
            </>
          )}>
        {!exportDone && selectionCapped && (
          <p className="mb-3 rounded-xl bg-amber-500/10 px-3 py-2 text-xs text-amber-700 ring-1 ring-amber-500/30 dark:text-amber-300">
            This filter matches more calls than one workbook can hold, so the
            selection is the newest {selected.size}. Narrow the date range to
            cover the rest.
          </p>
        )}
        {exportDone ? (
          <div className="space-y-3">
            <p className="text-sm text-soft">
              <span className="font-semibold">{exportDone.filename}</span> —{' '}
              {exportDone.call_count} call(s), {exportDone.number_count
                ?? exportDone.contact_count} number(s),{' '}
              {Math.max(1, Math.round((exportDone.size_bytes ?? 0) / 1024))} KB
            </p>
            {exportDone.storage === 'gcs' ? (
              <div className="rounded-2xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
                <p className="text-xs text-dim">In Google Cloud Storage:</p>
                <p className="mt-1 break-all font-mono text-2xs text-soft">
                  {exportDone.gcs_uri}
                </p>
                {exportDone.gcs_console_url && (
                  <a className="mt-2 inline-block text-xs font-medium text-mint-600 hover:underline dark:text-mint-300"
                    href={exportDone.gcs_console_url}
                    target="_blank" rel="noreferrer">
                    Open in the Cloud console
                  </a>
                )}
              </div>
            ) : (
              <p className="text-xs text-amber-700 dark:text-amber-300">
                The cloud upload failed{exportDone.upload_error
                  ? `: ${exportDone.upload_error}` : ''}. The file is on the
                server and the link below works — retry the export once storage
                is reachable to get it into the bucket.
              </p>
            )}
            <div>
              <a className="text-sm font-medium text-mint-600 hover:underline dark:text-mint-300"
                href={exportDone.url} target="_blank" rel="noreferrer">
                Download the workbook
              </a>
              <p className="mt-1 text-2xs text-dim">
                This link carries its own credential, so it opens for anyone you
                send it to — no Namaskall login needed. Sheets: Numbers, Contacts,
                Calls, Insights, Transcripts
                {nlVerified ? ', Evidence' : ''}, Commitments.
              </p>
            </div>
          </div>
        ) : (
          <div className="space-y-3">
            <Field label="File name">
              <Input value={exportTitle} onChange={(e) => setExportTitle(e.target.value)}
                placeholder={nlActive ? `Calls — ${nlActive}` : 'Call selection'} />
            </Field>
            <p className="text-xs text-dim">
              One workbook, one sheet per question: the de-duplicated
              <strong className="text-soft"> Numbers </strong> to dial, the
              <strong className="text-soft"> Contacts </strong> behind them, every
              <strong className="text-soft"> Call </strong> with its scores, the
              extracted <strong className="text-soft">Insights</strong>, the full
              <strong className="text-soft"> Transcripts </strong> and every open
              <strong className="text-soft"> Commitment</strong>.
              {nlVerified && ' Because this selection came from a verified AI '
                + 'filter, an Evidence sheet carries the quoted line that put '
                + 'each call in the list.'}
            </p>
            <p className="text-xs text-dim">
              It is stored in your Google Cloud Storage bucket and served back
              through a private link.
            </p>
          </div>
        )}
      </Modal>

      <Modal open={reportOpen} onClose={() => setReportOpen(false)}
        title={`Generate report from ${selected.size} call(s)`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setReportOpen(false)}>Cancel</Button>
            <Button onClick={generateReport}
              disabled={reportBusy || selected.size > REPORT_MAX}>
              {reportBusy ? <Spinner /> : <FileText size={14} />} Generate
            </Button>
          </>
        }>
        <div className="space-y-3">
          {selected.size > REPORT_MAX && (
            <p className="rounded-xl bg-amber-500/10 px-3 py-2 text-xs text-amber-700 ring-1 ring-amber-500/30 dark:text-amber-300">
              A report can synthesize at most {REPORT_MAX} calls and{' '}
              {selected.size} are selected. Export to Excel takes the whole
              selection, or narrow the filter and report on it in parts.
            </p>
          )}
          <Field label="Report title">
            <Input value={reportTitle} onChange={(e) => setReportTitle(e.target.value)}
              placeholder={nlActive ? `Report: ${nlActive}` : 'Call intelligence report'} />
          </Field>
          <p className="text-xs text-dim">
            The AI synthesizes the selected calls&apos; stored intelligence into a
            structured report (executive summary, findings, recommended actions).
            It runs in the background — you&apos;ll get a notification and it appears
            under Reports.
          </p>
        </div>
      </Modal>
    </div>
  )
}
