import { useRef, type CSSProperties, type ReactNode } from 'react'

export function cx(...c: (string | false | null | undefined)[]) {
  return c.filter(Boolean).join(' ')
}

export function Card({ className, children, hover, glow, style }: {
  className?: string; children: ReactNode; hover?: boolean; glow?: 'mint' | 'indigo'
  /** Escape hatch for entrance choreography (animation-delay) — not layout. */
  style?: CSSProperties
}) {
  const ref = useRef<HTMLDivElement>(null)
  // Hoverable cards get a pointer-tracking spotlight (CSS vars, no re-render).
  const onPointerMove = hover
    ? (e: React.PointerEvent<HTMLDivElement>) => {
        if (e.pointerType !== 'mouse' || !ref.current) return
        const r = ref.current.getBoundingClientRect()
        ref.current.style.setProperty('--spot-x', `${e.clientX - r.left}px`)
        ref.current.style.setProperty('--spot-y', `${e.clientY - r.top}px`)
      }
    : undefined
  return (
    <div ref={ref} onPointerMove={onPointerMove} style={style}
      className={cx('card', hover && 'card-hover', glow === 'mint' && 'shadow-glow-mint', glow === 'indigo' && 'shadow-glow-indigo', className)}>
      {children}
      {hover && <span aria-hidden className="card-spotlight" />}
    </div>
  )
}

export function Badge({ children, tone = 'neutral', soft }: { children: ReactNode; tone?: 'mint' | 'indigo' | 'amber' | 'rose' | 'neutral'; soft?: boolean }) {
  const tones: Record<string, string> = {
    mint: 'text-mint-700 dark:text-mint-300 bg-mint-400/15 ring-mint-400/30',
    indigo: 'text-indigo-600 dark:text-indigo-300 bg-indigo-500/12 ring-indigo-500/30',
    amber: 'text-amber-600 dark:text-amber-300 bg-amber-400/15 ring-amber-400/30',
    rose: 'text-rose-600 dark:text-rose-300 bg-rose-500/12 ring-rose-500/30',
    neutral: 'text-soft bg-[var(--surface-2)] ring-[var(--border)]',
  }
  return (
    <span className={cx('inline-flex max-w-full items-center gap-1 whitespace-nowrap rounded-full px-2.5 py-0.5 text-2xs font-semibold ring-1', tones[tone], soft && 'opacity-90')}>
      {children}
    </span>
  )
}

export function Button({
  children, onClick, variant = 'primary', size = 'md', className, disabled, title,
}: {
  children: ReactNode; onClick?: () => void; variant?: 'primary' | 'ghost' | 'danger' | 'soft'
  size?: 'sm' | 'md'; className?: string; disabled?: boolean; title?: string
}) {
  const variants: Record<string, string> = {
    primary: 'text-white bg-mint-600 hover:bg-mint-500 shadow-sm dark:bg-mint-500 dark:text-[#04150d] dark:hover:bg-mint-400',
    ghost: 'text-soft bg-[var(--surface-solid)] hover:bg-[var(--surface-2)] ring-1 ring-[var(--border)] hover:ring-[var(--border-strong)]',
    soft: 'text-indigo-600 dark:text-indigo-300 bg-indigo-500/10 hover:bg-indigo-500/15 ring-1 ring-indigo-500/20',
    danger: 'text-rose-600 dark:text-rose-300 bg-rose-500/10 hover:bg-rose-500/15 ring-1 ring-rose-500/20',
  }
  return (
    <button type="button" title={title} disabled={disabled} onClick={onClick}
      className={cx('inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-all active:scale-[.98] disabled:opacity-40 disabled:pointer-events-none',
        // Phones get a >=36px tap target; sm: restores today's exact desktop size.
        size === 'sm'
          ? 'min-h-[36px] px-2.5 py-1.5 text-xs sm:min-h-0'
          : 'min-h-[36px] px-3.5 py-2 text-sm sm:min-h-0',
        variants[variant], className)}>
      {children}
    </button>
  )
}

/** Square icon-only button with a mandatory accessible name. Replaces the
 *  hand-rolled `h-9 w-9 place-items-center` pattern (copy buttons, row actions). */
export function IconButton({
  label, onClick, children, className, disabled, active,
}: {
  /** Accessible name — becomes both aria-label and the tooltip. */
  label: string
  /** Receives the event so row-embedded actions can stopPropagation(). */
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void
  children: ReactNode
  className?: string; disabled?: boolean; active?: boolean
}) {
  return (
    <button type="button" aria-label={label} title={label} disabled={disabled} onClick={onClick}
      className={cx('inline-grid h-9 w-9 shrink-0 place-items-center rounded-md text-soft transition-all',
        'hover:bg-[var(--surface-2)] hover:text-[var(--text)] active:scale-[.96] disabled:opacity-40 disabled:pointer-events-none',
        active && 'bg-[var(--surface-2)] text-[var(--text)]', className)}>
      {children}
    </button>
  )
}

/** Pulsing "live" indicator — the one implementation of the ping-dot. */
export function LiveDot({ tone = 'mint', className }: { tone?: 'mint' | 'rose' | 'amber'; className?: string }) {
  const color = tone === 'rose' ? 'bg-rose-500' : tone === 'amber' ? 'bg-amber-500' : 'bg-mint-500'
  return (
    <span aria-hidden className={cx('relative inline-flex h-2 w-2', className)}>
      <span className={cx('absolute inline-flex h-full w-full animate-ping rounded-full opacity-60', color)} />
      <span className={cx('relative inline-flex h-2 w-2 rounded-full', color)} />
    </span>
  )
}

export function Spinner({ className }: { className?: string }) {
  return (
    <div className={cx('inline-block h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent opacity-60', className)} />
  )
}

export function EmptyState({ icon, title, hint, action }: { icon?: ReactNode; title: string; hint?: string; action?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-16 text-center">
      {icon && <div className="grid h-12 w-12 place-items-center rounded-2xl surface-2 text-dim">{icon}</div>}
      <div className="text-sm font-medium text-soft">{title}</div>
      {hint && <div className="max-w-xs text-xs text-dim">{hint}</div>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  )
}

export function SectionHead({ title, sub, right }: { title: string; sub?: string; right?: ReactNode }) {
  return (
    <div className="mb-4 flex flex-wrap items-end justify-between gap-3">
      <div className="min-w-0">
        <h2 className="font-display text-lg font-bold tracking-tight">{title}</h2>
        {sub && <p className="mt-0.5 text-sm text-dim">{sub}</p>}
      </div>
      {right}
    </div>
  )
}

export function SkeletonBlock({ h = 80, className }: { h?: number; className?: string }) {
  return <div className={cx('skeleton w-full', className)} style={{ height: h }} />
}
