/**
 * Analytics chart primitives — stacked columns, a day×hour heatmap, and a
 * proportion band. Dependency-free HTML/SVG, same as components/charts.tsx.
 *
 * Fixed specs these all obey, so the page reads as one system:
 *   · marks are thin — columns cap at 24px and never fill their slot
 *   · a 2px gap IN THE SURFACE COLOR separates touching marks; nothing gets a
 *     border drawn around it to hold it apart
 *   · only the data-end is rounded (4px); the baseline end stays square
 *   · gridlines are solid hairlines one step off the surface, never dashed
 *   · text always wears a text token — identity comes from the swatch beside
 *     it, never from coloring the text itself
 *   · every chart has a hover tooltip AND a table-view twin, so no value is
 *     reachable only by pointing at it
 *
 * Colors arrive as props and must come from lib/chartColors — no hex here.
 */
import { useId, useState, type ReactNode } from 'react'
import { cx } from './ui'

export type Series = { key: string; label: string; color: string }

/** Round a max up to a clean axis top (1/2/5 × 10ⁿ). */
function niceMax(v: number): number {
  if (v <= 0) return 1
  const mag = Math.pow(10, Math.floor(Math.log10(v)))
  const norm = v / mag
  const step = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 5 ? 5 : 10
  return step * mag
}

function fmt(n: number): string {
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n % 1000 === 0 ? 0 : 1)}k`
  return String(n)
}

/* ------------------------------------------------------------------ Tooltip */

function Tip({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <div
      role="tooltip"
      className={cx(
        'pointer-events-none absolute z-20 min-w-[128px] rounded-xl px-3 py-2 text-xs shadow-lg',
        'bg-[var(--surface-solid)] ring-1 ring-[var(--border-strong)]',
        className,
      )}
    >
      {children}
    </div>
  )
}

/* --------------------------------------------------------- Stacked columns */

export type ColumnDatum = { label: string; caption?: string; values: Record<string, number> }

/**
 * Stacked column chart over time. One y-scale only — a second measure gets its
 * own chart rather than a right-hand axis, which would let an arbitrary scale
 * alignment invent a correlation the data doesn't contain.
 */
export function StackedColumns({
  data, series, height = 200, unit = '', emptyText = 'No data in this range',
}: {
  data: ColumnDatum[]
  series: Series[]
  height?: number
  unit?: string
  emptyText?: string
}) {
  const [hover, setHover] = useState<number | null>(null)
  if (!data.length) {
    return <div className="py-10 text-center text-sm text-dim">{emptyText}</div>
  }

  const totals = data.map((d) => series.reduce((s, k) => s + (d.values[k.key] || 0), 0))
  const top = niceMax(Math.max(...totals, 1))
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => Math.round(top * f))

  // Thin out x labels so they never collide; always keep the last one.
  const stride = Math.max(1, Math.ceil(data.length / 12))

  return (
    <div>
      <div className="flex gap-2" style={{ height }}>
        {/* y axis */}
        <div className="flex w-9 shrink-0 flex-col-reverse justify-between pb-5 text-right">
          {ticks.map((t) => (
            <span key={t} className="text-2xs tabular-nums leading-none text-dim">{fmt(t)}</span>
          ))}
        </div>

        <div className="relative min-w-0 flex-1">
          {/* recessive hairline grid */}
          <div className="absolute inset-x-0 top-0 bottom-5" aria-hidden>
            {ticks.map((t, i) => (
              <div
                key={t}
                className="absolute inset-x-0 border-t"
                style={{ bottom: `${(i / (ticks.length - 1)) * 100}%`, borderColor: 'var(--border)' }}
              />
            ))}
          </div>

          <div className="absolute inset-0 flex items-end gap-[3px]">
            {data.map((d, i) => {
              const total = totals[i]
              const isHover = hover === i
              return (
                <div
                  key={i}
                  className="group relative flex h-full min-w-0 flex-1 flex-col justify-end"
                  onMouseEnter={() => setHover(i)}
                  onMouseLeave={() => setHover((h) => (h === i ? null : h))}
                  onFocus={() => setHover(i)}
                  onBlur={() => setHover((h) => (h === i ? null : h))}
                  tabIndex={0}
                  role="figure"
                  aria-label={`${d.label}: ${total} ${unit || 'total'}`}
                >
                  {/* hit target spans the full column height, not just the bar */}
                  <div className="flex flex-1 flex-col justify-end pb-5">
                    <div
                      className="mx-auto flex w-full max-w-[24px] flex-col-reverse"
                      style={{ height: `${(total / top) * 100}%` }}
                    >
                      {series.map((s, si) => {
                        const v = d.values[s.key] || 0
                        if (v <= 0) return null
                        const isTop = !series
                          .slice(si + 1)
                          .some((rest) => (d.values[rest.key] || 0) > 0)
                        return (
                          <div
                            key={s.key}
                            style={{
                              height: `${(v / total) * 100}%`,
                              background: s.color,
                              // 2px of surface between segments — the gap does
                              // the separating, so no stroke is needed.
                              marginTop: 2,
                              borderTopLeftRadius: isTop ? 4 : 0,
                              borderTopRightRadius: isTop ? 4 : 0,
                              opacity: hover == null || isHover ? 1 : 0.45,
                              transition: 'opacity .15s',
                            }}
                          />
                        )
                      })}
                    </div>
                  </div>

                  <div className="absolute inset-x-0 bottom-0 truncate text-center text-2xs leading-5 text-dim">
                    {i % stride === 0 || i === data.length - 1 ? d.label : ''}
                  </div>

                  {isHover && (
                    <Tip className={cx('bottom-8', i > data.length / 2 ? 'right-0' : 'left-0')}>
                      <div className="font-semibold text-[var(--text)]">{d.caption ?? d.label}</div>
                      <div className="mt-1.5 space-y-1">
                        {series.map((s) => (
                          <div key={s.key} className="flex items-center gap-2 whitespace-nowrap">
                            <span
                              className="h-2 w-2 shrink-0 rounded-full"
                              style={{ background: s.color }}
                            />
                            <span className="text-dim">{s.label}</span>
                            <span className="ml-auto font-semibold tabular-nums text-soft">
                              {d.values[s.key] || 0}
                            </span>
                          </div>
                        ))}
                        <div className="mt-1 flex gap-2 border-t border-[var(--border)] pt-1">
                          <span className="text-dim">Total</span>
                          <span className="ml-auto font-semibold tabular-nums text-[var(--text)]">
                            {total}
                          </span>
                        </div>
                      </div>
                    </Tip>
                  )}
                </div>
              )
            })}
          </div>
        </div>
      </div>

      <Legend series={series} />
    </div>
  )
}

/* ------------------------------------------------------------------ Legend */

export function Legend({ series, counts }: { series: Series[]; counts?: Record<string, number> }) {
  return (
    <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-1.5">
      {series.map((s) => (
        <span key={s.key} className="flex items-center gap-1.5 text-xs">
          <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: s.color }} />
          <span className="text-soft">{s.label}</span>
          {counts && (
            <span className="font-semibold tabular-nums text-dim">{counts[s.key] ?? 0}</span>
          )}
        </span>
      ))}
    </div>
  )
}

/* ------------------------------------------------------------- Trend line */

/** Single-measure line over the same x domain as StackedColumns. One series,
 *  so per spec it carries no legend box — the card title names it. */
export function TrendLine({
  data, color, height = 120, unit = '',
}: {
  data: { label: string; value: number | null }[]
  color: string
  height?: number
  unit?: string
}) {
  const id = useId().replace(/:/g, '')
  const [hover, setHover] = useState<number | null>(null)
  const pts = data.filter((d) => d.value != null) as { label: string; value: number }[]
  if (pts.length < 2) {
    return <div className="py-10 text-center text-sm text-dim">Not enough data to trend</div>
  }
  const top = niceMax(Math.max(...pts.map((p) => p.value)))
  const W = 100
  const step = W / (data.length - 1 || 1)
  const y = (v: number) => height - (v / top) * (height - 10) - 5

  let d = ''
  data.forEach((p, i) => {
    if (p.value == null) return
    d += `${d ? 'L' : 'M'}${(i * step).toFixed(2)},${y(p.value).toFixed(2)} `
  })

  return (
    <div>
      <div className="flex gap-2">
        <div className="flex w-9 shrink-0 flex-col-reverse justify-between text-right" style={{ height }}>
          {[0, 0.5, 1].map((f) => (
            <span key={f} className="text-2xs tabular-nums leading-none text-dim">
              {fmt(Math.round(top * f))}
            </span>
          ))}
        </div>
        <div className="relative min-w-0 flex-1" style={{ height }}>
          <div className="absolute inset-0" aria-hidden>
            {[0, 0.5, 1].map((f) => (
              <div key={f} className="absolute inset-x-0 border-t"
                style={{ bottom: `${f * 100}%`, borderColor: 'var(--border)' }} />
            ))}
          </div>
          <svg viewBox={`0 0 ${W} ${height}`} preserveAspectRatio="none"
            className="absolute inset-0 h-full w-full">
            <defs>
              <linearGradient id={`tl${id}`} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity="0.16" />
                <stop offset="100%" stopColor={color} stopOpacity="0" />
              </linearGradient>
            </defs>
            <path d={`${d} L${W},${height} L0,${height} Z`} fill={`url(#tl${id})`} />
            <path d={d} fill="none" stroke={color} strokeWidth={2} strokeLinejoin="round"
              strokeLinecap="round" vectorEffect="non-scaling-stroke" />
          </svg>

          {/* hover columns: full-height hit targets, never pinpoint */}
          <div className="absolute inset-0 flex">
            {data.map((p, i) => (
              <div key={i} className="min-w-0 flex-1"
                onMouseEnter={() => setHover(i)}
                onMouseLeave={() => setHover((h) => (h === i ? null : h))} />
            ))}
          </div>

          {hover != null && data[hover]?.value != null && (
            <>
              <div className="pointer-events-none absolute top-0 bottom-0 border-l"
                style={{ left: `${(hover / (data.length - 1)) * 100}%`, borderColor: 'var(--border-strong)' }} />
              <div
                className="pointer-events-none absolute h-2 w-2 rounded-full"
                style={{
                  left: `calc(${(hover / (data.length - 1)) * 100}% - 4px)`,
                  top: y(data[hover].value as number) - 4,
                  background: color,
                  boxShadow: '0 0 0 2px var(--surface-solid)',
                }}
              />
              <Tip className={cx('top-0', hover > data.length / 2 ? 'right-0' : 'left-0')}>
                <div className="font-semibold text-[var(--text)]">{data[hover].label}</div>
                <div className="mt-1 tabular-nums text-soft">
                  {data[hover].value}{unit && <span className="text-dim"> {unit}</span>}
                </div>
              </Tip>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------ Proportion band */

/** Horizontal 100% band — part-to-whole for a handful of ordered classes.
 *  Labels render inside a segment only when they measurably fit; otherwise the
 *  legend and the table carry them (never clipped, never overflowing). */
export function ProportionBand({
  segments, total, height = 34,
}: {
  segments: { key: string; label: string; value: number; color: string }[]
  total: number
  height?: number
}) {
  const [hover, setHover] = useState<string | null>(null)
  const sum = total || segments.reduce((s, x) => s + x.value, 0)
  if (!sum) return <div className="py-6 text-center text-sm text-dim">Nothing scored yet</div>
  return (
    <div>
      <div className="relative flex gap-[2px] overflow-hidden" style={{ height }}>
        {segments.map((s) => {
          const pct = (s.value / sum) * 100
          if (pct <= 0) return null
          // ~7px per character plus padding — below that the text would be
          // clipped by its own segment, so it drops to the legend/tooltip.
          const fits = pct > 12
          return (
            <div
              key={s.key}
              className="relative grid min-w-0 place-items-center rounded-[4px] transition-opacity"
              style={{
                width: `${pct}%`,
                background: s.color,
                opacity: hover == null || hover === s.key ? 1 : 0.45,
              }}
              onMouseEnter={() => setHover(s.key)}
              onMouseLeave={() => setHover((h) => (h === s.key ? null : h))}
              role="figure"
              aria-label={`${s.label}: ${s.value} of ${sum}`}
            >
              {fits && (
                <span className="px-1 text-2xs font-semibold tabular-nums text-white mix-blend-luminosity">
                  {Math.round(pct)}%
                </span>
              )}
            </div>
          )
        })}
      </div>
      <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1.5">
        {segments.map((s) => (
          <span key={s.key} className="flex items-center gap-1.5 text-xs">
            <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ background: s.color }} />
            <span className="text-soft">{s.label}</span>
            <span className="font-semibold tabular-nums text-dim">
              {s.value} · {Math.round((s.value / sum) * 100)}%
            </span>
          </span>
        ))}
      </div>
    </div>
  )
}

/* ---------------------------------------------------------------- Heatmap */

const DOW = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

/**
 * Day-of-week × hour volume. Sequential magnitude, so it is ONE hue stepped by
 * opacity — never a rainbow — and it ships a scale legend plus a tooltip
 * because a color-only continuous scale is unreadable on its own.
 */
export function ActivityHeatmap({
  cells, color, hourFrom = 6, hourTo = 22,
}: {
  cells: { dow: number; hour: number; calls: number; avg_lead: number | null }[]
  color: string
  hourFrom?: number
  hourTo?: number
}) {
  const [hover, setHover] = useState<string | null>(null)
  const byKey = new Map(cells.map((c) => [`${c.dow}-${c.hour}`, c]))
  const max = Math.max(1, ...cells.map((c) => c.calls))
  const hours: number[] = []
  for (let h = hourFrom; h <= hourTo; h++) hours.push(h)

  if (!cells.length) {
    return <div className="py-10 text-center text-sm text-dim">No calls in this range</div>
  }

  return (
    <div>
      <div className="overflow-x-auto pb-1">
        <div className="min-w-[520px]">
          <div className="flex gap-[2px] pl-9">
            {hours.map((h) => (
              <div key={h} className="min-w-0 flex-1 text-center text-2xs leading-4 text-dim">
                {h % 3 === 0 ? h : ''}
              </div>
            ))}
          </div>
          {DOW.map((label, dow) => (
            <div key={dow} className="flex items-center gap-[2px]">
              <div className="w-9 shrink-0 pr-1 text-right text-2xs text-dim">{label}</div>
              {hours.map((h) => {
                const key = `${dow}-${h}`
                const c = byKey.get(key)
                const v = c?.calls ?? 0
                return (
                  <div
                    key={h}
                    className="relative min-w-0 flex-1"
                    onMouseEnter={() => setHover(key)}
                    onMouseLeave={() => setHover((x) => (x === key ? null : x))}
                  >
                    <div
                      className="h-5 rounded-[3px]"
                      style={{
                        background: v ? color : 'var(--surface-2)',
                        // one hue, light→dark by density; floor at .15 so a
                        // single call is still visibly a call
                        opacity: v ? 0.15 + (v / max) * 0.85 : 1,
                      }}
                      role="figure"
                      aria-label={`${label} ${h}:00 — ${v} calls`}
                    />
                    {hover === key && v > 0 && (
                      <Tip className={cx('bottom-7', h > (hourFrom + hourTo) / 2 ? 'right-0' : 'left-0')}>
                        <div className="whitespace-nowrap font-semibold text-[var(--text)]">
                          {label} {String(h).padStart(2, '0')}:00
                        </div>
                        <div className="mt-1 whitespace-nowrap text-dim">
                          <span className="font-semibold tabular-nums text-soft">{v}</span> calls
                          {c?.avg_lead != null && (
                            <> · lead <span className="font-semibold tabular-nums text-soft">{c.avg_lead}</span></>
                          )}
                        </div>
                      </Tip>
                    )}
                  </div>
                )
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="mt-3 flex items-center gap-2 text-2xs text-dim">
        <span>Fewer</span>
        {[0.15, 0.35, 0.55, 0.75, 1].map((o) => (
          <span key={o} className="h-3 w-5 rounded-[3px]" style={{ background: color, opacity: o }} />
        ))}
        <span>More</span>
        <span className="ml-auto tabular-nums">peak {max} calls/hr</span>
      </div>
    </div>
  )
}
