import { useId, useState, type ReactNode } from 'react'
import { motion } from 'motion/react'
import { ChevronDown, ChevronRight, Search, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { Badge, cx } from './ui'
import { Checkbox } from './ui/checkbox'
import { Popover, PopoverContent, PopoverTrigger } from './ui/popover'

/* ------------------------------------------------------------------ Page header */

export function PageHeader({
  title, sub, actions, crumbs, detail,
}: {
  title: string; sub?: string; actions?: ReactNode
  /** Breadcrumb trail for detail workspaces, e.g. [{ label: 'Contacts', to: '/contacts' }]. */
  crumbs?: { label: string; to: string }[]
  /** Detail pages use the one-step-down H1 size. */
  detail?: boolean
}) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        {crumbs && crumbs.length > 0 && (
          <nav aria-label="Breadcrumb" className="mb-1 flex items-center gap-1 text-xs text-dim">
            {crumbs.map((c) => (
              <span key={c.to} className="flex items-center gap-1">
                <Link to={c.to} className="rounded transition-colors hover:text-soft">{c.label}</Link>
                <ChevronRight size={12} aria-hidden />
              </span>
            ))}
            <span aria-current="page" className="truncate text-soft">{title}</span>
          </nav>
        )}
        <h1 className={cx('font-display font-bold tracking-tight', detail ? 'text-xl' : 'text-2xl')}>{title}</h1>
        {sub && <p className="mt-1 text-sm text-dim">{sub}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ Table */

export function Table({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div className={cx('overflow-x-auto', className)}>
      <table className="w-full border-collapse text-sm">{children}</table>
    </div>
  )
}
export function THead({ children }: { children: ReactNode }) {
  return (
    <thead>
      <tr className="border-b border-[var(--border)] text-left">{children}</tr>
    </thead>
  )
}
export function TH({ children, className }: { children?: ReactNode; className?: string }) {
  return (
    <th className={cx('whitespace-nowrap px-3 py-2.5 text-2xs font-semibold uppercase tracking-[0.1em] text-dim', className)}>
      {children}
    </th>
  )
}
export function TR({ children, className, onClick, current }: {
  children: ReactNode; className?: string; onClick?: () => void
  /** The row the user was last working on (e.g. the call they just came back
   *  from). Tinted, and exposed as aria-current so it is announced too. */
  current?: boolean
}) {
  return (
    <tr onClick={onClick}
      aria-current={current ? 'true' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } } : undefined}
      className={cx('border-b border-[var(--border)] transition-colors hover:bg-[var(--surface-2)]',
        onClick && 'cursor-pointer focus-within:bg-[var(--surface-2)]',
        current && 'bg-mint-400/10', className)}>
      {children}
    </tr>
  )
}
export function TD({ children, className, colSpan, title }: { children?: ReactNode; className?: string; colSpan?: number; title?: string }) {
  return <td colSpan={colSpan} title={title} className={cx('px-3 py-2.5 align-middle text-soft', className)}>{children}</td>
}

/* ------------------------------------------------------------------ Pagination */

/**
 * Numbered pagination.
 *
 * Was Prev/Next only, which is fine for browsing and useless for a list you
 * are working through: on 12 pages, reaching page 9 took eight clicks and
 * there was no way to see how far the list went or to come back to where you
 * were. Numbers make the size of the result visible and every page one click
 * away.
 *
 * The window is elided rather than unbounded — a 40-page list must not render
 * 40 buttons and wrap the toolbar. First and last are always reachable, the
 * current page always sits inside the window, and gaps render as an inert "…"
 * so the jump is legible instead of looking like missing pages.
 */
function pageWindow(page: number, pages: number, span = 1): (number | 'gap')[] {
  if (pages <= 7) return Array.from({ length: pages }, (_, i) => i + 1)
  const out: (number | 'gap')[] = [1]
  const from = Math.max(2, page - span)
  const to = Math.min(pages - 1, page + span)
  if (from > 2) out.push('gap')
  for (let i = from; i <= to; i++) out.push(i)
  if (to < pages - 1) out.push('gap')
  out.push(pages)
  return out
}

export function Pagination({
  page, pages, total, onPage,
}: { page: number; pages: number; total?: number; onPage: (p: number) => void }) {
  if (pages <= 1 && !total) return null
  const last = Math.max(1, pages)
  const current = Math.min(Math.max(1, page), last)
  const step = 'min-h-[36px] min-w-[36px] rounded-md px-2.5 py-1.5 text-xs font-semibold '
    + 'ring-1 ring-[var(--border)] transition hover:bg-[var(--surface-2)] '
    + 'disabled:opacity-40 disabled:hover:bg-transparent sm:min-h-0'
  return (
    <nav className="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs text-dim"
      aria-label="Pagination">
      <div>{total != null ? `${total.toLocaleString('en-IN')} total` : ''}</div>
      <div className="flex flex-wrap items-center gap-1">
        <button disabled={current <= 1} onClick={() => onPage(current - 1)}
          className={step} aria-label="Previous page">Prev</button>
        {pageWindow(current, last).map((p, i) => (
          p === 'gap' ? (
            <span key={`gap${i}`} className="px-1 tabular-nums text-dim"
              aria-hidden="true">…</span>
          ) : (
            <button key={p} onClick={() => onPage(p)}
              aria-current={p === current ? 'page' : undefined}
              aria-label={`Page ${p}`}
              className={cx(step, 'tabular-nums',
                p === current
                  ? 'bg-mint-500/15 text-[var(--text)] ring-mint-400/40'
                  : 'text-soft')}>
              {p}
            </button>
          )
        ))}
        <button disabled={current >= last} onClick={() => onPage(current + 1)}
          className={step} aria-label="Next page">Next</button>
      </div>
    </nav>
  )
}

/* ------------------------------------------------------------------ Contact label */

/**
 * A phone number, shown as the person it belongs to.
 *
 * Whenever a number is in the tenant's phone book, the saved NAME is the
 * headline and the number sits underneath in small mono type. Unknown numbers
 * stay the headline themselves — a lone "—" would hide the one identifying
 * detail we do have. Use this anywhere a caller/callee is rendered so the app
 * reads as people rather than digits.
 */
export function ContactLabel({ name, phone, className }: {
  name?: string | null; phone?: string | null; className?: string
}) {
  const n = (name ?? '').trim()
  const p = (phone ?? '').trim()
  if (!n && !p) return <span className="text-dim">—</span>
  if (!n) return <span className={cx('font-mono text-sm text-[var(--text)]', className)}>{p}</span>
  return (
    <div className={cx('min-w-0 leading-tight', className)}>
      <div className="truncate font-medium text-[var(--text)]" title={n}>{n}</div>
      {p && <div className="truncate font-mono text-2xs text-dim" title={p}>{p}</div>}
    </div>
  )
}

/* ------------------------------------------------------------------ Status badge */

type Tone = 'mint' | 'indigo' | 'amber' | 'rose' | 'neutral'

const STATUS_TONE: Record<string, Tone> = {
  // call status
  answered: 'mint', completed: 'mint', called: 'mint', interested: 'mint', site_visit_booked: 'mint',
  no_answer: 'amber', busy: 'amber', missed: 'amber', pending: 'amber', scheduled: 'amber',
  to_contact_later: 'amber', in_doubt: 'amber', inquiry: 'indigo', calling: 'indigo', dialing: 'indigo',
  failed: 'rose', declined: 'rose', invalid_number: 'rose', rejected: 'rose', cancelled: 'rose', down: 'rose', dismissed: 'neutral',
  not_called: 'neutral', not_classified: 'neutral',
  // Connected but nobody actually spoke (silence / noise / voicemail / instant
  // hang-up). Amber, not mint: the leg answered, the conversation did not.
  empty_call: 'amber',
  // call direction + runtime states
  inbound: 'indigo', outbound: 'mint', up: 'mint', live: 'mint', running: 'mint', error: 'rose', stopped: 'neutral',
  // priority
  high: 'rose', medium: 'amber', low: 'indigo',
  // sentiment / lead
  positive: 'mint', hot: 'rose', warm: 'amber', cold: 'indigo', negative: 'rose', neutral: 'neutral',
  healthy: 'mint', degraded: 'amber',
  // intelligence layer: follow-up decisions, statuses, urgency, reports
  yes: 'mint', no: 'neutral', unsure: 'amber', review: 'amber',
  archived: 'neutral', waiting_customer: 'amber', escalated: 'rose',
  urgent: 'rose', critical: 'rose', queued: 'amber', done: 'mint',
  agent: 'indigo', customer: 'mint',
}

export function StatusBadge({ value }: { value?: string | null }) {
  if (!value) return <span className="text-dim">—</span>
  const key = String(value).toLowerCase()
  const tone = STATUS_TONE[key] ?? 'neutral'
  return <Badge tone={tone}>{String(value).replace(/_/g, ' ')}</Badge>
}

/* ------------------------------------------------------------------ Search input */

export function SearchInput({
  value, onChange, placeholder = 'Search…', className, ariaLabel, onEnter, icon,
}: {
  value: string; onChange: (v: string) => void; placeholder?: string; className?: string
  ariaLabel?: string
  /** Fires on Enter (NL/semantic searches that submit rather than filter live). */
  onEnter?: () => void
  /** Swap the leading magnifier for another icon (e.g. Sparkles for AI search). */
  icon?: ReactNode
}) {
  return (
    <div className={cx('relative', className)}>
      <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-dim">
        {icon ?? <Search size={15} />}
      </span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={onEnter ? (e) => { if (e.key === 'Enter') { e.preventDefault(); onEnter() } } : undefined}
        placeholder={placeholder}
        aria-label={ariaLabel ?? placeholder}
        className="w-full rounded-md bg-[var(--surface-2)] py-2 pl-9 pr-8 text-sm text-[var(--text)] outline-none ring-1 ring-[var(--border)] transition placeholder:text-dim focus:ring-mint-400/40"
      />
      {value && (
        <button type="button" aria-label="Clear search" onClick={() => onChange('')}
          className="absolute right-2 top-1/2 grid h-6 w-6 -translate-y-1/2 place-items-center rounded text-dim transition-colors hover:text-soft">
          <X size={13} />
        </button>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ Form inputs */

export function Input(props: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <input {...props}
      className={cx('w-full rounded-md bg-[var(--surface-2)] px-3 py-2 text-sm text-[var(--text)] outline-none ring-1 ring-[var(--border)] transition placeholder:text-dim focus:ring-mint-400/40', props.className)} />
  )
}
export function Textarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return (
    <textarea {...props}
      className={cx('w-full rounded-md bg-[var(--surface-2)] px-3 py-2 text-sm text-[var(--text)] outline-none ring-1 ring-[var(--border)] transition placeholder:text-dim focus:ring-mint-400/40', props.className)} />
  )
}
export function Select(props: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select {...props}
      className={cx('rounded-md bg-[var(--surface-2)] px-3 py-2 text-sm font-medium text-[var(--text)] outline-none ring-1 ring-[var(--border)] transition focus:ring-mint-400/40', props.className)} />
  )
}
export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-semibold text-soft">{label}</span>
      {children}
    </label>
  )
}

/* ------------------------------------------------------------------ Multi-select */

/**
 * Pick several options at once, as checkboxes in a popover.
 *
 * A native <Select> can only hold ONE agent, so "how did shubhangi AND maya do"
 * meant running the page twice and adding the numbers up by hand. Values are
 * carried as a comma-separated string because that is what the URL and the API
 * already speak (`?agent=a,b`), which keeps deep links and the single-agent
 * form working unchanged.
 *
 * `summary` is what the trigger says: the caller names the thing being picked,
 * so the button reads "3 agents" rather than a truncated list nobody can parse.
 */
export function MultiSelect({
  options, value, onChange, allLabel, label, summary, className,
}: {
  options: string[]
  value: string[]
  onChange: (next: string[]) => void
  allLabel: string
  label: string
  summary?: (n: number) => string
  className?: string
}) {
  const [open, setOpen] = useState(false)
  const chosen = new Set(value)
  const text = value.length === 0
    ? allLabel
    : value.length === 1
      ? value[0]
      : summary ? summary(value.length) : `${value.length} selected`
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button type="button" aria-label={label}
          className={cx('flex min-h-[38px] items-center justify-between gap-2 rounded-md',
            'bg-[var(--surface-2)] px-3 py-2 text-sm font-medium text-[var(--text)]',
            'outline-none ring-1 ring-[var(--border)] transition focus:ring-mint-400/40',
            className)}>
          <span className="truncate">{text}</span>
          <ChevronDown size={14} className="shrink-0 text-dim" />
        </button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-64 p-0">
        <div className="flex items-center justify-between gap-2 border-b border-[var(--ui-border)] px-3 py-2">
          <span className="text-xs font-semibold text-soft">{label}</span>
          {value.length > 0 && (
            <button type="button" onClick={() => onChange([])}
              className="text-2xs font-semibold text-mint-600 hover:underline dark:text-mint-400">
              Clear
            </button>
          )}
        </div>
        {options.length === 0 ? (
          <p className="px-3 py-4 text-xs text-dim">Nothing to choose from yet.</p>
        ) : (
          <div className="max-h-64 overflow-y-auto py-1">
            {options.map((o) => (
              <label key={o}
                className="flex cursor-pointer items-center gap-2.5 px-3 py-1.5 text-sm text-[var(--text)] transition hover:bg-[var(--surface-2)]">
                <Checkbox checked={chosen.has(o)}
                  onCheckedChange={() => onChange(chosen.has(o)
                    ? value.filter((v) => v !== o)
                    : [...value, o])} />
                <span className="truncate" title={o}>{o}</span>
              </label>
            ))}
          </div>
        )}
      </PopoverContent>
    </Popover>
  )
}

/* ------------------------------------------------------------------ Segmented control */

export function Segmented<T extends string>({
  options, value, onChange,
}: { options: { value: T; label: string }[]; value: T; onChange: (v: T) => void }) {
  // Per-instance layoutId — two Segmented controls on one page must not
  // trade thumbs with each other.
  const id = useId()
  return (
    <div className="inline-flex max-w-full overflow-x-auto rounded-xl bg-[var(--surface-2)] p-1 ring-1 ring-[var(--border)]">
      {options.map((o) => (
        <button key={o.value} onClick={() => onChange(o.value)} aria-pressed={value === o.value}
          className={cx('relative min-h-[36px] shrink-0 whitespace-nowrap rounded-md px-3 py-1.5 text-xs font-semibold transition-colors sm:min-h-0',
            value === o.value ? 'text-[var(--text)]' : 'text-dim hover:text-soft')}>
          {value === o.value && (
            <motion.span layoutId={`seg-thumb-${id}`} aria-hidden
              className="absolute inset-0 rounded-md bg-[var(--surface-solid)] shadow-sm"
              transition={{ type: 'spring', stiffness: 500, damping: 40 }} />
          )}
          <span className="relative">{o.label}</span>
        </button>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ Horizontal bar list (analytics) */

export function BarList({
  items, color = 'var(--accent)', empty = 'No data', wrap,
}: {
  items: { label: string; value: number }[]; color?: string; empty?: string
  /** Wrap long sentence labels onto two lines instead of truncating. */
  wrap?: boolean
}) {
  if (!items.length) return <div className="py-6 text-center text-sm text-dim">{empty}</div>
  const max = Math.max(...items.map((i) => i.value), 1)
  return (
    <div className="space-y-2.5">
      {items.map((it, i) => (
        <div key={i}>
          <div className={cx('mb-1 flex justify-between gap-2 text-xs', wrap ? 'items-start' : 'items-center')}>
            <span title={it.label} className={cx('text-soft', wrap ? 'line-clamp-2 leading-snug' : 'truncate')}>{it.label}</span>
            <span className="shrink-0 font-semibold tabular-nums text-dim">{it.value.toLocaleString('en-IN')}</span>
          </div>
          <div className="h-2 overflow-hidden rounded-full bg-[var(--surface-2)]">
            {/* Grows on first paint, then springs between values on refetch.
                Delay is capped so long lists don't tail off forever. */}
            <motion.div className="h-full rounded-full"
              initial={{ width: 0 }}
              animate={{ width: `${(it.value / max) * 100}%` }}
              transition={{ duration: 0.7, delay: Math.min(i * 0.05, 0.4), ease: [0.22, 1, 0.36, 1] }}
              style={{ background: color }} />
          </div>
        </div>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------------ Async boundary */

export function AsyncState({
  loading, error, empty, emptyLabel = 'Nothing here yet', children,
  variant = 'rows', rows = 4, skeleton,
}: {
  loading?: boolean; error?: unknown; empty?: boolean; emptyLabel?: string; children: ReactNode
  /** Shape of the loading placeholder — match it to what actually lands. */
  variant?: 'rows' | 'cards' | 'text'
  /** How many placeholder rows/cards to draw. */
  rows?: number
  /** Full override when the real layout is too specific for a variant. */
  skeleton?: ReactNode
}) {
  if (loading) {
    if (skeleton) return <>{skeleton}</>
    if (variant === 'cards') {
      return (
        <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: rows }, (_, i) => <div key={i} className="skeleton h-28 w-full rounded-2xl" />)}
        </div>
      )
    }
    if (variant === 'text') {
      return (
        <div className="space-y-2 py-2">
          {Array.from({ length: rows }, (_, i) => (
            <div key={i} className="skeleton h-4" style={{ width: `${[100, 92, 96, 74][i % 4]}%` }} />
          ))}
        </div>
      )
    }
    return (
      <div className="space-y-2 py-2">
        {Array.from({ length: rows }, (_, i) => <div key={i} className="skeleton h-11 w-full" />)}
      </div>
    )
  }
  if (error) {
    return <div role="alert" className="rounded-xl bg-rose-500/10 px-4 py-3 text-sm font-medium text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">Failed to load: {String((error as Error)?.message ?? error)}</div>
  }
  if (empty) {
    return <div className="py-12 text-center text-sm text-dim">{emptyLabel}</div>
  }
  return <>{children}</>
}
