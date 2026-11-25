/**
 * Conversation Analytics — the deep-dive companion to Intelligence.
 *
 * Intelligence answers "which call?"; this page answers "what is happening
 * across all of them, and is it getting better or worse?". So nothing here
 * repeats a per-call row: every panel is either a period-over-period movement,
 * a distribution, or a comparison between agents/segments.
 *
 * Reads /intel/analytics/conversation — SQL-side aggregation over
 * call_intelligence, windowed on WHEN THE CALL HAPPENED. The page it replaced
 * read the legacy calls.analysis blob with no date range at all, which is why
 * it could only ever show one all-time number per metric.
 *
 * Two things the filter row deliberately is NOT: a call picker, and a set of
 * per-card filters. It names a COHORT — period, agent, direction, language,
 * sentiment, lead band — once, above everything it scopes, and every panel
 * describes exactly that population.
 */
import { useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { Card, Button, SectionHead, EmptyState, SkeletonBlock, cx } from '../components/ui'
import { PageHeader, Table, THead, TH, TR, TD, AsyncState, Segmented, Select } from '../components/kit'
import {
  StackedColumns, TrendLine, ProportionBand, ActivityHeatmap, Legend,
  type Series, type ColumnDatum,
} from '../components/analyticsCharts'
import { CHART, TONE } from '../lib/chartColors'
import { num } from '../lib/format'
import { useUrlParam, useUrlPatch } from '../lib/hooks'
import {
  ArrowDownRight, ArrowUpRight, BarChart3, BrainCircuit, Clock, Flame, Minus,
  MessageSquareQuote, RefreshCw, Table2, TrendingUp, Users, X,
} from 'lucide-react'

type Counted = { label: string; count: number }
type Kpi = {
  analyzed: number; avg_lead: number | null; avg_risk: number | null
  avg_opportunity: number | null; avg_quality: number | null
  avg_satisfaction: number | null; avg_conversion: number | null
  avg_duration: number | null; total_duration: number | null
  hot: number; positive: number; neutral: number; negative: number
  low_confidence: number
  hot_rate: number | null; negative_rate: number | null; positive_rate: number | null
}
type Row = {
  day: string; calls: number; positive: number; neutral: number
  negative: number; hot: number; avg_lead: number | null; avg_duration: number | null
}
type AgentRow = {
  agent: string; calls: number; avg_lead: number | null; avg_quality: number | null
  avg_duration: number | null; hot: number; positive: number; negative: number
}
type Data = {
  days: number; agent: string | null
  filters: Record<string, string>
  current: Kpi; previous: Kpi
  series: Row[]
  lead_bands: { hot: number; warm: number; cold: number; unscored: number }
  agents: AgentRow[]
  heatmap: { dow: number; hour: number; calls: number; avg_lead: number | null }[]
  mix: { direction: string; language: string; calls: number; avg_lead: number | null }[]
  followups: { total: number; yes: number; unsure: number; completed: number; missed: number }
  funnel: {
    analyzed: number; reached: number; qualified: number
    committed: number; scheduled: number; won: number
  }
  commitments: { kind: string; n: number; closed: number }[]
  agent_options: string[]
  language_options: string[]
  direction_options: string[]
  topics: Counted[]; objections: Counted[]; product_interest: Counted[]
  pain_points: Counted[]; buying_signals: Counted[]; competitor_mentions: Counted[]
}

const n = (v: unknown): number => (typeof v === 'number' && !isNaN(v) ? v : 0)

const DAY_OPTIONS = [
  { value: '7', label: '7 days' }, { value: '30', label: '30 days' },
  { value: '90', label: '90 days' }, { value: '365', label: '1 year' },
]

const LEAD_BANDS = [
  { value: '', label: 'Any lead score' },
  { value: 'hot', label: 'Hot (70+)' },
  { value: 'warm', label: 'Warm (40–69)' },
  { value: 'cold', label: 'Cold (<40)' },
  { value: 'unscored', label: 'Unscored' },
]

type LensKey = 'topics' | 'objections' | 'buying_signals' | 'product_interest'
  | 'pain_points' | 'competitor_mentions'
const LENSES: { key: LensKey; label: string; color: string; empty: string }[] = [
  { key: 'topics', label: 'Topics', color: CHART.mint, empty: 'No topics extracted yet' },
  { key: 'objections', label: 'Objections', color: CHART.rose, empty: 'No objections recorded yet' },
  { key: 'buying_signals', label: 'Buying signals', color: CHART.amber, empty: 'No buying signals captured yet' },
  { key: 'product_interest', label: 'Products', color: CHART.indigo, empty: 'No product interest captured yet' },
  { key: 'pain_points', label: 'Pain points', color: CHART.rose, empty: 'No pain points captured yet' },
  { key: 'competitor_mentions', label: 'Competitors', color: CHART.neutral, empty: 'No competitors mentioned' },
]

const SENTIMENT: Series[] = [
  { key: 'positive', label: 'Positive', color: TONE.positive },
  { key: 'neutral', label: 'Neutral', color: TONE.neutral },
  { key: 'negative', label: 'Negative', color: TONE.negative },
]

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function dayLabel(iso: string) {
  const d = new Date(`${iso}T00:00:00`)
  return `${d.getDate()}/${d.getMonth() + 1}`
}
function dayFull(iso: string) {
  const d = new Date(`${iso}T00:00:00`)
  return d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' })
}
function mins(sec: number | null | undefined) {
  const s = n(sec)
  if (!s) return '—'
  return s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.round(s)}s`
}
function hourLabel(h: number) {
  const am = h < 12
  const hr = h % 12 === 0 ? 12 : h % 12
  return `${hr}${am ? 'am' : 'pm'}`
}

/** Signed change vs the previous equal-length period. `goodDown` inverts the
 *  color so "negative sentiment fell" reads as the win it is. */
function Delta({ now, prev, unit = '', goodDown = false }:
  { now: number | null; prev: number | null; unit?: string; goodDown?: boolean }) {
  if (now == null || prev == null || prev === 0) {
    return <span className="text-dim">no prior period</span>
  }
  const diff = now - prev
  const pct = Math.round((diff / Math.abs(prev)) * 100)
  if (pct === 0) {
    return <span className="inline-flex items-center gap-0.5 text-dim"><Minus size={11} /> flat</span>
  }
  const good = goodDown ? diff < 0 : diff > 0
  return (
    <span className={cx('inline-flex items-center gap-0.5 font-semibold',
      good ? 'text-mint-600 dark:text-mint-300' : 'text-rose-500 dark:text-rose-300')}>
      {diff > 0 ? <ArrowUpRight size={12} /> : <ArrowDownRight size={12} />}
      {Math.abs(pct)}%{unit}
    </span>
  )
}

/** Plain-text delta for the table view — the same number without the colour,
 *  because colour alone is not an accessible way to carry direction. */
function deltaText(now: number | null | undefined, prev: number | null | undefined): string {
  if (now == null || prev == null || prev === 0) return '—'
  const pct = Math.round(((now - prev) / Math.abs(prev)) * 100)
  return pct === 0 ? 'flat' : `${pct > 0 ? '+' : ''}${pct}%`
}

/** Metric tile: value, movement vs prior period, and the raw prior value. */
function Tile({ label, value, sub, delta, icon, tone = 'indigo' }: {
  label: string; value: string; sub?: string
  delta?: React.ReactNode; icon?: React.ReactNode
  tone?: 'mint' | 'indigo' | 'amber' | 'rose'
}) {
  const toneBg: Record<string, string> = {
    mint: 'bg-mint-400/15 text-mint-600 dark:text-mint-300',
    indigo: 'bg-indigo-500/12 text-indigo-500 dark:text-indigo-300',
    amber: 'bg-amber-400/15 text-amber-600 dark:text-amber-300',
    rose: 'bg-rose-500/12 text-rose-500 dark:text-rose-300',
  }
  return (
    <Card hover className="flex flex-col p-5">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="eyebrow truncate">{label}</div>
          {/* proportional figures — tabular-nums only in table columns */}
          <div className="metric mt-2 text-3xl leading-none sm:text-4xl">{value}</div>
        </div>
        {icon && (
          <div className={cx('grid h-9 w-9 shrink-0 place-items-center rounded-xl', toneBg[tone])}>
            {icon}
          </div>
        )}
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-x-2 text-xs">
        {delta}
        {sub && <span className="text-dim">{sub}</span>}
      </div>
    </Card>
  )
}

function BarRows({ items, color, empty, onPick }: {
  items: Counted[]; color: string; empty: string; onPick?: (label: string) => void
}) {
  if (!items?.length) return <div className="py-6 text-center text-sm text-dim">{empty}</div>
  const max = Math.max(...items.map((i) => i.count), 1)
  return (
    <div className="space-y-2.5">
      {items.map((it, i) => {
        const Row = onPick ? 'button' : 'div'
        return (
          <Row
            key={i}
            {...(onPick ? { type: 'button' as const, onClick: () => onPick(it.label) } : {})}
            className={cx('block w-full text-left', onPick && 'group rounded-lg')}
          >
            <div className="mb-1 flex items-start justify-between gap-2 text-xs">
              <span title={it.label}
                className={cx('line-clamp-2 leading-snug text-soft', onPick && 'group-hover:text-[var(--text)]')}>
                {it.label}
              </span>
              <span className="shrink-0 font-semibold tabular-nums text-dim">{num(it.count)}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-[var(--surface-2)]">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${(it.count / max) * 100}%`, background: color }} />
            </div>
          </Row>
        )
      })}
    </div>
  )
}

/**
 * Conversion funnel. The stages are counted independently rather than as
 * strict subsets — a customer can commit without ever scoring 40 — so each bar
 * is drawn as a share of `reached` and the drop between bars is deliberately
 * NOT labelled as loss. Anything else would be a chart asserting causation the
 * data doesn't carry.
 */
const FUNNEL_STAGES: { key: keyof Data['funnel']; label: string; hint: string; color: string }[] = [
  { key: 'reached', label: 'Conversations', hint: 'connected and spoke', color: CHART.indigo },
  { key: 'qualified', label: 'Qualified', hint: 'lead score ≥ 40', color: CHART.mint },
  { key: 'committed', label: 'Committed', hint: 'agreed to something concrete', color: CHART.amber },
  { key: 'scheduled', label: 'Follow-up booked', hint: 'AI decided yes', color: CHART.mint },
  { key: 'won', label: 'Won', hint: "outcome marked 'success'", color: CHART.mint },
]

function Funnel({ f }: { f: Data['funnel'] }) {
  const base = Math.max(1, n(f?.reached))
  return (
    <div className="space-y-3">
      {FUNNEL_STAGES.map((s) => {
        const v = n(f?.[s.key])
        const pct = (v / base) * 100
        return (
          <div key={s.key}>
            <div className="mb-1 flex items-baseline justify-between gap-3 text-xs">
              <span className="text-soft">
                {s.label} <span className="text-dim">· {s.hint}</span>
              </span>
              <span className="shrink-0 tabular-nums text-dim">
                <span className="font-semibold text-[var(--text)]">{num(v)}</span>
                {' '}· {Math.round(pct)}%
              </span>
            </div>
            <div className="h-3 overflow-hidden rounded-full bg-[var(--surface-2)]">
              <div className="h-full rounded-full transition-all"
                style={{ width: `${Math.min(100, pct)}%`, background: s.color }} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function PageSkeleton() {
  return (
    <div className="space-y-6">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        {[0, 1, 2, 3].map((i) => <SkeletonBlock key={i} h={110} className="rounded-2xl" />)}
      </div>
      <SkeletonBlock h={300} className="rounded-2xl" />
      <div className="grid gap-4 lg:grid-cols-2">
        {[0, 1, 2, 3].map((i) => <SkeletonBlock key={i} h={230} className="rounded-2xl" />)}
      </div>
    </div>
  )
}

/** A table panel — the shape every table-view card shares. */
function TablePanel({ title, sub, children }:
  { title: string; sub?: string; children: React.ReactNode }) {
  return (
    <Card className="p-5">
      <SectionHead title={title} sub={sub} />
      <div className="mt-3 overflow-x-auto">{children}</div>
    </Card>
  )
}

export default function ConvAnalytics() {
  const nav = useNavigate()
  const doRefresh = useRefresh()
  const urlPatch = useUrlPatch()

  // Filters live in the URL: an analytics view someone spent five selects
  // building should survive a refresh and be sendable to a colleague.
  const [days, setDays] = useUrlParam('days', '30')
  const [agent, setAgent] = useUrlParam('agent')
  const [direction, setDirection] = useUrlParam('direction')
  const [language, setLanguage] = useUrlParam('language')
  const [sentiment, setSentiment] = useUrlParam('sentiment')
  const [leadBand, setLeadBand] = useUrlParam('band')
  const [view, setView] = useUrlParam('view', 'charts')
  const [lens, setLens] = useUrlParam('lens', 'topics')
  const activeLens = LENSES.find((l) => l.key === lens) ?? LENSES[0]
  const isTable = view === 'table'

  const hasFilters = !!(agent || direction || language || sentiment || leadBand)
  const resetFilters = () =>
    urlPatch({ agent: null, direction: null, language: null, sentiment: null, band: null })

  const qs = new URLSearchParams({ days })
  if (agent) qs.set('agent', agent)
  if (direction) qs.set('direction', direction)
  if (language) qs.set('language', language)
  if (sentiment) qs.set('sentiment', sentiment)
  if (leadBand) qs.set('lead_band', leadBand)
  const query = qs.toString()

  const { data, isLoading, isFetching, error } =
    useGet<Data>(['conv-analytics', query], `/intel/analytics/conversation?${query}`)

  const cur = data?.current
  const prev = data?.previous
  const series = useMemo(() => data?.series ?? [], [data])

  const columns: ColumnDatum[] = useMemo(
    () => series.map((r) => ({
      label: dayLabel(r.day),
      caption: dayFull(r.day),
      values: { positive: n(r.positive), neutral: n(r.neutral), negative: n(r.negative) },
    })),
    [series],
  )
  const leadTrend = useMemo(
    () => series.map((r) => ({ label: dayFull(r.day), value: r.avg_lead })),
    [series],
  )

  const bands = data?.lead_bands
  const analyzed = n(cur?.analyzed)

  /** Busiest slots, for the table twin of the heatmap. */
  const busiest = useMemo(
    () => [...(data?.heatmap ?? [])].sort((a, b) => b.calls - a.calls).slice(0, 20),
    [data],
  )

  // Drill-through: a topic/objection is only useful if you can get to the calls
  // behind it. Intelligence persists its free-text filter as ?search=, and the
  // window is carried across so a 90-day topic doesn't land on a 30-day list.
  const drill = (q: string) =>
    nav(`/intelligence?search=${encodeURIComponent(q)}&days=${days}`)

  /* One sentence naming the cohort every panel below describes. */
  const cohort = [
    `Last ${days} days`,
    agent && `agent ${agent}`,
    direction && direction,
    language && language.toUpperCase(),
    sentiment && `${sentiment} sentiment`,
    leadBand && (LEAD_BANDS.find((b) => b.value === leadBand)?.label ?? leadBand),
  ].filter(Boolean).join(' · ')

  return (
    <div className="space-y-6">
      <PageHeader
        title="Conversation Analytics"
        sub="What every conversation adds up to — movement, distribution, and which agent is actually converting."
        actions={
          <Button variant="ghost" onClick={() => doRefresh(['conv-analytics'])} disabled={isFetching}>
            <RefreshCw size={15} className={cx(isFetching && 'animate-spin')} /> Refresh
          </Button>
        }
      />

      {/* One filter row above everything it scopes — never per-card filters. */}
      <Card className="space-y-2 p-3">
        <div className="flex flex-wrap items-center gap-2">
          <Segmented options={DAY_OPTIONS} value={days} onChange={setDays} />
          <Select value={agent} onChange={(e) => setAgent(e.target.value)} aria-label="Filter by agent">
            <option value="">All agents</option>
            {(data?.agent_options ?? []).map((a) => <option key={a} value={a}>{a}</option>)}
          </Select>
          <Select value={direction} onChange={(e) => setDirection(e.target.value)}
            aria-label="Filter by call direction">
            <option value="">Inbound &amp; outbound</option>
            {(data?.direction_options ?? []).map((d) => (
              <option key={d} value={d}>{d[0].toUpperCase() + d.slice(1)} only</option>
            ))}
          </Select>
          <Select value={language} onChange={(e) => setLanguage(e.target.value)}
            aria-label="Filter by call language">
            <option value="">All languages</option>
            {(data?.language_options ?? []).map((l) => (
              <option key={l} value={l}>{l.toUpperCase()}</option>
            ))}
          </Select>
          <Select value={sentiment} onChange={(e) => setSentiment(e.target.value)}
            aria-label="Filter by sentiment">
            <option value="">Any sentiment</option>
            <option value="positive">Positive</option>
            <option value="neutral">Neutral</option>
            <option value="negative">Negative</option>
          </Select>
          <Select value={leadBand} onChange={(e) => setLeadBand(e.target.value)}
            aria-label="Filter by lead band">
            {LEAD_BANDS.map((b) => <option key={b.value} value={b.value}>{b.label}</option>)}
          </Select>
          {hasFilters && (
            <Button variant="ghost" size="sm" onClick={resetFilters}>
              <X size={13} /> Reset
            </Button>
          )}
          {/* A real view switch. This was a "Table view" toggle that appended
              two tables 4,000px below the click, which reads as doing nothing. */}
          <div className="ml-auto">
            <Segmented
              options={[{ value: 'charts', label: 'Charts' }, { value: 'table', label: 'Table' }]}
              value={view} onChange={setView} />
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-x-2 text-xs text-dim">
          <span>{cohort}</span>
          <span>· compared with the preceding {days} days</span>
          {isTable && <span>· every plotted figure in text form</span>}
        </div>
      </Card>

      {isLoading ? (
        <PageSkeleton />
      ) : (
        <AsyncState error={error} empty={false}>
          {analyzed === 0 ? (
            <Card className="p-5">
              <EmptyState
                icon={<BrainCircuit size={22} />}
                title="No analyzed calls match these filters"
                hint={hasFilters
                  ? 'Every completed call is analyzed automatically. Widen the date range, or clear a filter.'
                  : 'Every completed call is analyzed automatically. Widen the date range — nothing was analyzed in this window.'}
                action={
                  <div className="flex flex-wrap justify-center gap-2">
                    {hasFilters && <Button variant="soft" onClick={resetFilters}>Clear filters</Button>}
                    <Button variant="ghost" onClick={() => setDays('365')}>Show last year</Button>
                  </div>
                }
              />
            </Card>
          ) : (
            /* Hold the previous render at reduced opacity on refetch — no
               skeleton flash, no layout jump. */
            <div className={cx('space-y-6 transition-opacity', isFetching && 'opacity-60')}>
              {!isTable ? (
                <>
                  <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
                    <Tile label="Calls analyzed" value={num(analyzed)}
                      icon={<BrainCircuit size={17} />} tone="indigo"
                      delta={<Delta now={analyzed} prev={n(prev?.analyzed)} />}
                      sub={`${num(n(prev?.analyzed))} prior`} />
                    <Tile label="Hot lead rate" value={cur?.hot_rate != null ? `${cur.hot_rate}%` : '—'}
                      icon={<Flame size={17} />} tone="amber"
                      delta={<Delta now={cur?.hot_rate ?? null} prev={prev?.hot_rate ?? null} />}
                      sub={`${num(n(cur?.hot))} of ${num(analyzed)} calls`} />
                    <Tile label="Avg lead score" value={cur?.avg_lead != null ? String(cur.avg_lead) : '—'}
                      icon={<TrendingUp size={17} />} tone="mint"
                      delta={<Delta now={cur?.avg_lead ?? null} prev={prev?.avg_lead ?? null} />}
                      sub="out of 100" />
                    <Tile label="Negative sentiment"
                      value={cur?.negative_rate != null ? `${cur.negative_rate}%` : '—'}
                      icon={<MessageSquareQuote size={17} />} tone="rose"
                      delta={<Delta now={cur?.negative_rate ?? null} prev={prev?.negative_rate ?? null} goodDown />}
                      sub={`${num(n(cur?.negative))} calls`} />
                  </div>

                  {/* Conversion + what was actually agreed to. */}
                  <div className="grid gap-4 lg:grid-cols-[1.35fr_1fr]">
                    <Card className="p-5">
                      <SectionHead title="Conversion funnel"
                        sub="Each stage is a recorded fact, shown as a share of conversations" />
                      <div className="mt-4"><Funnel f={data!.funnel} /></div>
                    </Card>
                    <Card className="p-5">
                      <SectionHead title="What customers agreed to"
                        sub="Commitments the agent captured on the call" />
                      <div className="mt-4">
                        {(data?.commitments ?? []).length === 0 ? (
                          <div className="py-8 text-center text-sm text-dim">
                            No commitments captured in this range
                          </div>
                        ) : (
                          <BarRows
                            items={(data?.commitments ?? []).map((c) => ({
                              label: c.kind.replace(/_/g, ' '), count: c.n,
                            }))}
                            color={CHART.amber} empty="" />
                        )}
                      </div>
                    </Card>
                  </div>

                  {/* Volume + sentiment over time — the page's lead chart. */}
                  <Card className="p-5">
                    <SectionHead title="Call volume by sentiment"
                      sub={`Daily, last ${days} days — dated by when the call happened`} />
                    <div className="mt-4">
                      <StackedColumns data={columns} series={SENTIMENT} height={220} unit="calls" />
                    </div>
                  </Card>

                  <div className="grid gap-4 lg:grid-cols-2">
                    <Card className="p-5">
                      <SectionHead title="Average lead score" sub="Daily mean, 0–100" />
                      <div className="mt-4">
                        <TrendLine data={leadTrend} color={CHART.mint} height={140} unit="avg lead" />
                      </div>
                    </Card>

                    <Card className="p-5">
                      <SectionHead title="Lead temperature mix"
                        sub="Hot ≥ 70 · Warm 40–69 · Cold < 40" />
                      <div className="mt-4">
                        <ProportionBand
                          total={analyzed}
                          segments={[
                            { key: 'hot', label: 'Hot', value: n(bands?.hot), color: TONE.hot },
                            { key: 'warm', label: 'Warm', value: n(bands?.warm), color: CHART.indigo },
                            { key: 'cold', label: 'Cold', value: n(bands?.cold), color: CHART.neutral },
                          ]}
                        />
                      </div>
                      <div className="mt-5 grid grid-cols-3 gap-3 border-t border-[var(--border)] pt-4">
                        <div>
                          <div className="eyebrow">Avg talk time</div>
                          <div className="mt-1 text-lg font-semibold text-[var(--text)]">
                            {mins(cur?.avg_duration)}
                          </div>
                        </div>
                        <div>
                          <div className="eyebrow">Conversion prob.</div>
                          <div className="mt-1 text-lg font-semibold text-[var(--text)]">
                            {cur?.avg_conversion != null
                              ? `${Math.round(Number(cur.avg_conversion) * 100)}%` : '—'}
                          </div>
                        </div>
                        <div>
                          <div className="eyebrow">Follow-ups set</div>
                          <div className="mt-1 text-lg font-semibold text-[var(--text)]">
                            {num(n(data?.followups?.yes))}
                          </div>
                        </div>
                      </div>
                    </Card>
                  </div>

                  <Card className="p-5">
                    <SectionHead title="When calls happen"
                      sub="Volume by weekday and hour — where to point capacity" />
                    <div className="mt-4">
                      <ActivityHeatmap cells={data?.heatmap ?? []} color={CHART.indigo} />
                    </div>
                  </Card>

                  {/* One card, six lenses. These were six separate cards; at that
                      size the page was mostly bar lists and the signal drowned. */}
                  <Card className="p-5">
                    <SectionHead title="Voice of the customer"
                      sub="Extracted from every conversation — pick a lens; click any row for the calls behind it"
                      right={
                        <div className="flex flex-wrap gap-1">
                          {LENSES.map((l) => (
                            <button key={l.key} type="button" onClick={() => setLens(l.key)}
                              aria-pressed={lens === l.key}
                              className={cx('rounded-lg px-2.5 py-1 text-xs transition-colors',
                                lens === l.key
                                  ? 'bg-[var(--surface-2)] font-semibold text-[var(--text)]'
                                  : 'text-dim hover:text-soft')}>
                              {l.label}
                            </button>
                          ))}
                        </div>
                      } />
                    <div className="mt-4">
                      <BarRows items={data?.[activeLens.key] ?? []} color={activeLens.color}
                        empty={activeLens.empty} onPick={drill} />
                    </div>
                  </Card>
                </>
              ) : (
                /* ---------------------------------------- table view: every
                   panel above as text. It used to cover two of eight. */
                <>
                  <TablePanel title="Headline figures"
                    sub={`This period vs the preceding ${days} days`}>
                    <Table>
                      <THead>
                        <TH>Metric</TH>
                        <TH className="text-right">Now</TH>
                        <TH className="text-right">Prior</TH>
                        <TH className="text-right">Change</TH>
                      </THead>
                      <tbody>
                        {([
                          ['Calls analyzed', analyzed, n(prev?.analyzed)],
                          ['Hot lead rate (%)', cur?.hot_rate ?? null, prev?.hot_rate ?? null],
                          ['Positive rate (%)', cur?.positive_rate ?? null, prev?.positive_rate ?? null],
                          ['Negative rate (%)', cur?.negative_rate ?? null, prev?.negative_rate ?? null],
                          ['Avg lead score', cur?.avg_lead ?? null, prev?.avg_lead ?? null],
                          ['Avg opportunity', cur?.avg_opportunity ?? null, prev?.avg_opportunity ?? null],
                          ['Avg risk', cur?.avg_risk ?? null, prev?.avg_risk ?? null],
                          ['Avg quality', cur?.avg_quality ?? null, prev?.avg_quality ?? null],
                          ['Avg satisfaction', cur?.avg_satisfaction ?? null, prev?.avg_satisfaction ?? null],
                          ['Avg talk (seconds)', cur?.avg_duration ?? null, prev?.avg_duration ?? null],
                        ] as [string, number | null, number | null][]).map(([label, now, before]) => (
                          <TR key={label}>
                            <TD className="font-medium text-[var(--text)]">{label}</TD>
                            <TD className="text-right tabular-nums">{now ?? '—'}</TD>
                            <TD className="text-right tabular-nums">{before ?? '—'}</TD>
                            <TD className="text-right tabular-nums">{deltaText(now, before)}</TD>
                          </TR>
                        ))}
                      </tbody>
                    </Table>
                  </TablePanel>

                  <TablePanel title="Daily figures" sub="The same numbers the charts plot">
                    <Table>
                      <THead>
                        <TH>Date</TH>
                        <TH className="text-right">Calls</TH>
                        <TH className="text-right">Positive</TH>
                        <TH className="text-right">Neutral</TH>
                        <TH className="text-right">Negative</TH>
                        <TH className="text-right">Hot</TH>
                        <TH className="text-right">Avg lead</TH>
                        <TH className="text-right">Avg talk</TH>
                      </THead>
                      <tbody>
                        {series.filter((r) => r.calls > 0).map((r) => (
                          <TR key={r.day}>
                            <TD className="whitespace-nowrap font-medium text-[var(--text)]">{r.day}</TD>
                            <TD className="text-right tabular-nums">{num(r.calls)}</TD>
                            <TD className="text-right tabular-nums">{num(r.positive)}</TD>
                            <TD className="text-right tabular-nums">{num(r.neutral)}</TD>
                            <TD className="text-right tabular-nums">{num(r.negative)}</TD>
                            <TD className="text-right tabular-nums">{num(r.hot)}</TD>
                            <TD className="text-right tabular-nums">{r.avg_lead ?? '—'}</TD>
                            <TD className="text-right tabular-nums">{mins(r.avg_duration)}</TD>
                          </TR>
                        ))}
                      </tbody>
                    </Table>
                  </TablePanel>

                  <div className="grid gap-4 lg:grid-cols-2">
                    <TablePanel title="Conversion funnel"
                      sub="Counted independently — a stage can exceed the one below it">
                      <Table>
                        <THead>
                          <TH>Stage</TH>
                          <TH className="text-right">Calls</TH>
                          <TH className="text-right">Share</TH>
                        </THead>
                        <tbody>
                          {FUNNEL_STAGES.map((s) => {
                            const v = n(data?.funnel?.[s.key])
                            const base = Math.max(1, n(data?.funnel?.reached))
                            return (
                              <TR key={s.key}>
                                <TD>
                                  <span className="font-medium text-[var(--text)]">{s.label}</span>
                                  <span className="text-dim"> · {s.hint}</span>
                                </TD>
                                <TD className="text-right tabular-nums">{num(v)}</TD>
                                <TD className="text-right tabular-nums">{Math.round((v / base) * 100)}%</TD>
                              </TR>
                            )
                          })}
                        </tbody>
                      </Table>
                    </TablePanel>

                    <TablePanel title="Lead temperature mix" sub="Hot ≥ 70 · Warm 40–69 · Cold < 40">
                      <Table>
                        <THead>
                          <TH>Band</TH>
                          <TH className="text-right">Calls</TH>
                          <TH className="text-right">Share</TH>
                        </THead>
                        <tbody>
                          {([['Hot', bands?.hot], ['Warm', bands?.warm],
                            ['Cold', bands?.cold], ['Unscored', bands?.unscored]] as const).map(([label, v]) => (
                            <TR key={label}>
                              <TD className="font-medium text-[var(--text)]">{label}</TD>
                              <TD className="text-right tabular-nums">{num(n(v))}</TD>
                              <TD className="text-right tabular-nums">
                                {Math.round((n(v) / Math.max(1, analyzed)) * 100)}%
                              </TD>
                            </TR>
                          ))}
                        </tbody>
                      </Table>
                    </TablePanel>
                  </div>

                  <TablePanel title="What customers agreed to"
                    sub="Commitments the agent captured, and how many are closed">
                    {(data?.commitments ?? []).length === 0 ? (
                      <div className="py-6 text-center text-sm text-dim">
                        No commitments captured in this range
                      </div>
                    ) : (
                      <Table>
                        <THead>
                          <TH>Commitment</TH>
                          <TH className="text-right">Made</TH>
                          <TH className="text-right">Closed</TH>
                          <TH className="text-right">Open</TH>
                        </THead>
                        <tbody>
                          {(data?.commitments ?? []).map((c) => (
                            <TR key={c.kind}>
                              <TD className="font-medium capitalize text-[var(--text)]">
                                {c.kind.replace(/_/g, ' ')}
                              </TD>
                              <TD className="text-right tabular-nums">{num(c.n)}</TD>
                              <TD className="text-right tabular-nums">{num(c.closed)}</TD>
                              <TD className="text-right tabular-nums">{num(c.n - c.closed)}</TD>
                            </TR>
                          ))}
                        </tbody>
                      </Table>
                    )}
                  </TablePanel>

                  <TablePanel title="Busiest times"
                    sub="Weekday × hour, highest volume first — the heatmap as figures">
                    <Table>
                      <THead>
                        <TH>Day</TH>
                        <TH>Hour</TH>
                        <TH className="text-right">Calls</TH>
                        <TH className="text-right">Avg lead</TH>
                      </THead>
                      <tbody>
                        {busiest.map((c, i) => (
                          <TR key={`${c.dow}-${c.hour}-${i}`}>
                            <TD className="font-medium text-[var(--text)]">{DOW[c.dow] ?? c.dow}</TD>
                            <TD className="tabular-nums">{hourLabel(c.hour)}</TD>
                            <TD className="text-right tabular-nums">{num(c.calls)}</TD>
                            <TD className="text-right tabular-nums">{c.avg_lead ?? '—'}</TD>
                          </TR>
                        ))}
                      </tbody>
                    </Table>
                  </TablePanel>

                  <TablePanel title="Voice of the customer"
                    sub="Every lens, with the calls behind each row one click away">
                    <Table>
                      <THead>
                        <TH>Lens</TH>
                        <TH>Item</TH>
                        <TH className="text-right">Mentions</TH>
                      </THead>
                      <tbody>
                        {LENSES.flatMap((l) => (data?.[l.key] ?? []).map((it, i) => (
                          <TR key={`${l.key}-${i}`} onClick={() => drill(it.label)}>
                            <TD className="whitespace-nowrap text-dim">{l.label}</TD>
                            <TD className="max-w-[520px] text-[var(--text)]">{it.label}</TD>
                            <TD className="text-right tabular-nums">{num(it.count)}</TD>
                          </TR>
                        )))}
                      </tbody>
                    </Table>
                  </TablePanel>
                </>
              )}

              {/* Agent comparison — already text, so it is the same panel in
                  both views rather than a chart and a table that could differ. */}
              <Card className="p-5">
                <SectionHead title="Agent performance"
                  sub="Same window, side by side. Sorted by call volume."
                  right={<span className="text-xs text-dim">
                    {data?.agents?.length ?? 0} active
                  </span>} />
                <div className="mt-3 overflow-x-auto">
                  <Table>
                    <THead>
                      <TH>Agent</TH>
                      <TH className="text-right">Calls</TH>
                      <TH className="text-right">Avg lead</TH>
                      <TH className="text-right">Hot</TH>
                      <TH className="text-right">Avg talk</TH>
                      {isTable ? (
                        <>
                          <TH className="text-right">Positive</TH>
                          <TH className="text-right">Negative</TH>
                        </>
                      ) : (
                        <TH className="w-[180px]">Sentiment</TH>
                      )}
                    </THead>
                    <tbody>
                      {(data?.agents ?? []).map((a) => {
                        const other = Math.max(0, a.calls - a.positive - a.negative)
                        return (
                          <TR key={a.agent} onClick={() => setAgent(a.agent)}>
                            <TD className="max-w-[220px] truncate font-medium text-[var(--text)]"
                              title={a.agent}>{a.agent}</TD>
                            <TD className="text-right tabular-nums">{num(a.calls)}</TD>
                            <TD className="text-right tabular-nums">{a.avg_lead ?? '—'}</TD>
                            <TD className="text-right tabular-nums">{num(a.hot)}</TD>
                            <TD className="text-right tabular-nums">{mins(a.avg_duration)}</TD>
                            {isTable ? (
                              <>
                                <TD className="text-right tabular-nums">{num(a.positive)}</TD>
                                <TD className="text-right tabular-nums">{num(a.negative)}</TD>
                              </>
                            ) : (
                              <TD>
                                <div className="flex h-2 gap-[2px] overflow-hidden rounded-full">
                                  {[['positive', a.positive], ['neutral', other], ['negative', a.negative]]
                                    .map(([k, v]) => (v as number) > 0 && (
                                      <div key={k as string} title={`${k}: ${v}`}
                                        style={{ width: `${((v as number) / a.calls) * 100}%`,
                                          background: TONE[k as string] }} />
                                    ))}
                                </div>
                              </TD>
                            )}
                          </TR>
                        )
                      })}
                    </tbody>
                  </Table>
                </div>
                {!isTable && <div className="mt-3"><Legend series={SENTIMENT} /></div>}
              </Card>

              <Card className="p-5">
                <SectionHead title="Channel & language mix"
                  sub="Lead quality is rarely uniform across these" />
                <div className="mt-3 overflow-x-auto">
                  <Table>
                    <THead>
                      <TH>Direction</TH>
                      <TH>Language</TH>
                      <TH className="text-right">Calls</TH>
                      <TH className="text-right">Share</TH>
                      <TH className="text-right">Avg lead</TH>
                    </THead>
                    <tbody>
                      {(data?.mix ?? []).map((m, i) => (
                        <TR key={i} onClick={() => urlPatch({
                          direction: m.direction === 'unknown' ? null : m.direction,
                          language: m.language === 'unknown' ? null : m.language,
                        })}>
                          <TD className="capitalize">{m.direction}</TD>
                          <TD className="uppercase">{m.language}</TD>
                          <TD className="text-right tabular-nums">{num(m.calls)}</TD>
                          <TD className="text-right tabular-nums">
                            {Math.round((m.calls / analyzed) * 100)}%
                          </TD>
                          <TD className="text-right tabular-nums">{m.avg_lead ?? '—'}</TD>
                        </TR>
                      ))}
                    </tbody>
                  </Table>
                </div>
              </Card>

              <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-dim">
                <span className="inline-flex items-center gap-1">
                  <Clock size={12} /> {mins(cur?.total_duration)} of conversation analyzed
                </span>
                <span className="inline-flex items-center gap-1">
                  <Users size={12} /> {num(n(data?.followups?.total))} follow-up decisions
                  · {num(n(data?.followups?.missed))} missed
                </span>
                {n(cur?.low_confidence) > 0 && (
                  <span>{num(n(cur?.low_confidence))} call(s) analyzed with low confidence</span>
                )}
                <span className="ml-auto inline-flex items-center gap-1">
                  {isTable ? <Table2 size={12} /> : <BarChart3 size={12} />}
                  {isTable ? 'Table view' : 'Chart view'}
                </span>
              </div>
            </div>
          )}
        </AsyncState>
      )}
    </div>
  )
}
