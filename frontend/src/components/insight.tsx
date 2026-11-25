/**
 * Shared composites for the INSIGHT family (Intelligence, IntelligenceCall,
 * ContactHub, ConvAnalytics, FollowUps, Reports). One implementation of the
 * score/chip/commitment/timeline patterns that used to be duplicated between
 * IntelligenceCall and ContactHub — one color law (chartColors.scoreColor),
 * one anatomy per pattern.
 */
import type { ReactNode } from 'react'
import { Badge, cx } from './ui'
import { StatusBadge } from './kit'
import { CountingNumber } from './motion'
import { RadialRing } from './charts'
import { scoreColor } from '../lib/chartColors'
import type { Commitment } from '../api/intel'
import { fmtDateTime } from '../lib/format'

export const SCORE_LEGEND = '70+ strong · 40-69 medium · <40 weak'

/** Colored numeric score for table cells — one threshold law for L/O/R columns.
 *  `invert` for risk-style scores where low is good. */
export function ScoreCell({ score, invert }: { score?: number | null; invert?: boolean }) {
  if (score == null) return <span className="text-dim">—</span>
  return (
    <span
      title={SCORE_LEGEND}
      className="font-semibold tabular-nums"
      style={{ color: scoreColor(score, invert) }}>
      {score}
    </span>
  )
}

/** The exec-summary ring cluster shared by IntelligenceCall and ContactHub.
 *  Rings take their color from scoreColor (invert for risk-style scores);
 *  trailing children (sentiment badges, sparkline…) flow in the same row. */
export function ScoreRingRow({ items, children }: {
  items: { label: string; value?: number | null; invert?: boolean }[]
  children?: ReactNode
}) {
  return (
    <div className="mt-4 flex flex-wrap items-center gap-5">
      {items.map((it) => (
        <span key={it.label} title={SCORE_LEGEND}>
          <RadialRing value={it.value ?? 0} max={100} label={it.label}
            color={scoreColor(it.value ?? 0, it.invert)} />
        </span>
      ))}
      {children}
    </div>
  )
}

/** Flex-wrapped Badge list for extracted signal strings (topics, objections…). */
export function Chips({ items, tone = 'neutral' }: {
  items?: string[]
  tone?: 'mint' | 'indigo' | 'amber' | 'rose' | 'neutral'
}) {
  if (!items?.length) return <span className="text-xs text-dim">None detected</span>
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((t, i) => <Badge key={i} tone={tone}>{t}</Badge>)}
    </div>
  )
}

/** Accessible toggle-chip filter group (single-select, aria-pressed). */
export function FilterChips({ options, value, onChange }: {
  options: { value: string; label: string }[]
  value: string
  onChange: (v: string) => void
}) {
  return (
    <div role="group" className="flex flex-wrap gap-1.5">
      {options.map((o) => (
        <button key={o.value} type="button"
          aria-pressed={value === o.value}
          onClick={() => onChange(o.value)}
          className={cx('rounded-full px-3 py-1 text-xs font-medium ring-1 transition',
            value === o.value
              ? 'bg-mint-400/15 text-mint-600 ring-mint-400/40 dark:text-mint-300'
              : 'text-dim ring-[var(--border)] hover:text-soft')}>
          {o.label}
        </button>
      ))}
    </div>
  )
}

/** Commitment tile (renders an <li> — wrap in <ul className="space-y-2.5">). */
export function CommitmentCard({ c }: { c: Commitment }) {
  return (
    <li className="rounded-xl bg-[var(--surface-2)] p-3 text-xs">
      <div className="mb-1 flex flex-wrap items-center gap-1.5">
        <StatusBadge value={c.made_by} />
        <Badge tone="indigo">{c.kind.replace(/_/g, ' ')}</Badge>
        {c.is_vague ? <Badge tone="amber">vague</Badge> : null}
        {c.clarified ? <Badge tone="mint">clarified in-call</Badge> : null}
      </div>
      <div className="text-soft">{c.description}</div>
      {c.quote && <div className="mt-1 italic text-dim">“{c.quote}”</div>}
      {(c.deadline_at || c.deadline_text) && (
        <div className="mt-1 text-dim">
          Due: {c.deadline_at ? fmtDateTime(c.deadline_at) : c.deadline_text}
        </div>
      )}
    </li>
  )
}

export type TimelineEvent = {
  id: string | number
  title: ReactNode
  /** Badges / timestamp shown inline after the title. */
  meta?: ReactNode
  detail?: ReactNode
  /** Icon inside the timeline dot; omit for the plain small dot. */
  icon?: ReactNode
  tone?: 'rose' | 'neutral'
  /** Original event, for renderDetail callbacks. */
  raw?: unknown
}

/** The border-l timeline shared by ContactHub (events) and IntelligenceCall
 *  (key moments). `renderDetail` appends per-event footer content (links…). */
export function TimelineList({ events, renderDetail }: {
  events: TimelineEvent[]
  renderDetail?: (e: TimelineEvent) => ReactNode
}) {
  return (
    <ol className="relative space-y-3 border-l border-[var(--border)] pl-5">
      {events.map((e) => (
        <li key={e.id} className="relative">
          {e.icon ? (
            <span className={cx(
              'absolute -left-[27px] top-0.5 grid h-5 w-5 place-items-center rounded-full ring-2 ring-[var(--surface)]',
              e.tone === 'rose' ? 'bg-rose-500/15 text-rose-500' : 'bg-[var(--surface-2)] text-dim')}>
              {e.icon}
            </span>
          ) : (
            <span className="absolute -left-[26px] top-1 h-2.5 w-2.5 rounded-full bg-mint-500" />
          )}
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-sm font-semibold">{e.title}</span>
            {e.meta}
          </div>
          {e.detail && <div className="mt-0.5 text-xs text-dim">{e.detail}</div>}
          {renderDetail?.(e)}
        </li>
      ))}
    </ol>
  )
}

/** Small metric tile (ContactHub lifetime stats and friends). */
export function StatTile({ label, value, hint }: {
  label: string; value: ReactNode; hint?: string
}) {
  return (
    <div>
      <div className="metric text-2xl font-bold">
        {typeof value === 'number' ? <CountingNumber value={value} /> : value}
      </div>
      <div className="text-xs text-dim">{label}</div>
      {hint && <div className="text-2xs text-dim">{hint}</div>}
    </div>
  )
}
