import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  Search, Bell, Timer, HelpCircle, Sun, Moon, PanelLeft, Menu, CornerDownLeft,
  CheckCircle2, XCircle, Keyboard, MessageCircle, CheckCheck,
} from 'lucide-react'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useTheme } from '../theme/ThemeProvider'
import { HealthPill, type Service } from '../components/HealthPill'
import { cx } from '../components/ui'
import { clockTime, num, parseTs } from '../lib/format'
import { NAV } from '../lib/nav'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { patch, post } from '../api/mutate'

/** Flattened list of navigable pages for the quick-jump search. */
function usePages() {
  return useMemo(() => {
    const out: { to: string; label: string; group: string; icon: any }[] = []
    for (const g of NAV) {
      for (const i of g.items) {
        out.push({ to: i.to, label: i.label, group: g.title, icon: i.icon })
      }
    }
    return out
  }, [])
}

const IS_MAC = typeof navigator !== 'undefined' && /Mac/i.test(navigator.platform)

const ICON_BTN = 'grid h-8 w-8 place-items-center rounded-md text-dim transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]'

/** Quiet icon-only utility button (sidebar toggle, theme…). */
function IconBtn({ onClick, label, className, children }: {
  onClick?: () => void; label: string; className?: string; children: React.ReactNode
}) {
  return (
    <button type="button" onClick={onClick} aria-label={label} title={label}
      className={cx(ICON_BTN, className)}>
      {children}
    </button>
  )
}

type Notif = {
  id: number; kind: string; severity?: string | null
  title: string; body?: string | null; link?: string | null
  read_at?: string | null; created_at?: string
}

function ago(iso?: string): string {
  const at = parseTs(iso)
  if (!at) return ''
  const s = Math.max(0, (Date.now() - at.getTime()) / 1000)
  if (s < 60) return 'now'
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h`
  return `${Math.floor(s / 86400)}d`
}

function severityDot(severity?: string | null): string {
  const s = (severity ?? '').toLowerCase()
  if (s === 'critical' || s === 'error' || s === 'urgent') return 'bg-rose-500'
  if (s === 'warning' || s === 'warn') return 'bg-amber-500'
  return 'bg-indigo-400'
}

export function Topbar({ title, onToggleSidebar, onOpenMobileNav, services, overall }: {
  title: string; onToggleSidebar: () => void; onOpenMobileNav?: () => void
  services: Service[]; overall: 'healthy' | 'degraded' | 'down'
}) {
  const { theme, toggle } = useTheme()
  const navigate = useNavigate()
  const pages = usePages()
  const refresh = useRefresh()
  // `/usage/minutes` now also carries the tenant's entitlement view, from the
  // same numbers the dial gates enforce — so the pill shows used / limit
  // rather than a bare running total that says nothing about what is left.
  const minutes = useGet<{
    minutes_month?: number
    minutes_used_period?: number | null
    minutes_limit?: number | null
    minutes_remaining?: number | null
  }>(['usage-minutes'], '/usage/minutes', { refetchInterval: 60000 })
  const notifs = useGet<{ items: Notif[]; unread: number }>(
    ['intel', 'notifications'], '/intel/notifications?limit=20', { refetchInterval: 30000 })
  const [now, setNow] = useState(clockTime())
  const [q, setQ] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [hi, setHi] = useState(0)
  const inputRef = useRef<HTMLInputElement>(null)

  useEffect(() => { const t = setInterval(() => setNow(clockTime()), 1000); return () => clearInterval(t) }, [])

  // "/" or Cmd/Ctrl+K focuses the quick-jump search from anywhere.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      const tag = (e.target as HTMLElement)?.tagName
      const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (e.target as HTMLElement)?.isContentEditable
      if ((e.key === '/' && !typing) || (e.key.toLowerCase() === 'k' && (e.metaKey || e.ctrlKey))) {
        e.preventDefault()
        inputRef.current?.focus()
      }
    }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [])

  const matches = useMemo(() => {
    const needle = q.trim().toLowerCase()
    if (!needle) return []
    return pages.filter((p) => p.label.toLowerCase().includes(needle) || p.group.toLowerCase().includes(needle)).slice(0, 7)
  }, [q, pages])

  // Empty query browses the full page list; typing narrows it.
  const visible = q.trim() ? matches : pages

  const go = (to: string) => {
    navigate(to)
    setQ(''); setSearchOpen(false); inputRef.current?.blur()
  }

  const downServices = services.filter((s) => !s.ok)
  const unread = notifs.data?.unread ?? 0
  const items = notifs.data?.items ?? []
  const attention = unread > 0 || downServices.length > 0

  const openNotification = async (n: Notif) => {
    if (!n.read_at) {
      try { await patch(`/intel/notifications/${n.id}/read`) } catch { /* non-fatal */ }
      refresh(['intel', 'notifications'])
    }
    if (n.link) navigate(n.link)
  }

  const markAllRead = async () => {
    try { await post('/intel/notifications/read-all') } catch { /* non-fatal */ }
    refresh(['intel', 'notifications'])
  }

  const resultRow = (m: { to: string; label: string; group: string; icon: any }, i: number) => (
    <button key={m.to} type="button"
      onMouseDown={(e) => { e.preventDefault(); go(m.to) }}
      onMouseEnter={() => setHi(i)}
      className={cx('flex w-full items-center gap-2.5 rounded-md px-3 py-2 text-left text-sm transition-colors',
        i === hi ? 'bg-[var(--surface-2)] text-[var(--text)]' : 'text-soft')}>
      <m.icon size={15} className="shrink-0 text-dim" />
      <span className="font-medium">{m.label}</span>
      <span className="text-2xs text-dim">{m.group}</span>
      {i === hi && <CornerDownLeft size={13} className="ml-auto shrink-0 text-dim" />}
    </button>
  )

  return (
    <header className="sticky top-0 z-20 -mx-4 mb-6 border-b border-[var(--border)] bg-[var(--surface-solid)] px-4 sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
      <div className="flex h-14 items-center gap-3">
        {/* Mobile: open the nav drawer. Desktop: collapse the rail. */}
        <IconBtn onClick={onOpenMobileNav} label="Open navigation" className="lg:hidden"><Menu size={17} /></IconBtn>
        <IconBtn onClick={onToggleSidebar} label="Toggle sidebar" className="hidden lg:grid"><PanelLeft size={16} /></IconBtn>
        <h1 className="truncate text-sm font-semibold tracking-tight">{title}</h1>
        <HealthPill services={services} overall={overall} />

        {/* Quick-jump */}
        <div className="relative ml-2 hidden w-full max-w-sm md:block">
          <Search size={14} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-dim" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => { setQ(e.target.value); setSearchOpen(true); setHi(0) }}
            onFocus={() => { setSearchOpen(true); setHi(0) }}
            onBlur={() => setTimeout(() => setSearchOpen(false), 120)}
            onKeyDown={(e) => {
              if (e.key === 'Escape') { setQ(''); setSearchOpen(false); inputRef.current?.blur() }
              if (e.key === 'ArrowDown') { e.preventDefault(); setHi((i) => Math.min(i + 1, visible.length - 1)) }
              if (e.key === 'ArrowUp') { e.preventDefault(); setHi((i) => Math.max(i - 1, 0)) }
              if (e.key === 'Enter' && visible[hi]) go(visible[hi].to)
            }}
            placeholder="Jump to a panel…"
            aria-label="Jump to a panel"
            className="h-8 w-full rounded-md bg-[var(--surface-2)] pl-9 pr-14 text-sm text-[var(--text)] outline-none ring-1 ring-transparent transition placeholder:text-dim focus:bg-[var(--surface-solid)] focus:ring-[var(--border-strong)]"
          />
          <kbd className="pointer-events-none absolute right-2.5 top-1/2 hidden -translate-y-1/2 rounded border border-[var(--border)] bg-[var(--surface-solid)] px-1.5 py-0.5 text-2xs font-medium text-dim lg:inline-flex">
            {IS_MAC ? '⌘K' : 'Ctrl K'}
          </kbd>
          {searchOpen && (
            <div data-lenis-prevent className="card absolute left-0 top-10 z-40 max-h-72 w-full overflow-y-auto p-1.5 shadow-glass-lg animate-fade-up">
              {q.trim() ? (
                <>
                  {matches.length === 0 && (
                    <div className="px-3 py-2.5 text-sm text-dim">No panel matches “{q}”</div>
                  )}
                  {matches.map((m, i) => resultRow(m, i))}
                </>
              ) : (
                // Browse mode: the whole IA, grouped — a teaching moment on first focus.
                Array.from(new Set(pages.map((p) => p.group))).map((g) => (
                  <div key={g}>
                    <div className="eyebrow px-3 pb-1 pt-2">{g}</div>
                    {pages.filter((p) => p.group === g).map((m) => resultRow(m, pages.indexOf(m)))}
                  </div>
                ))
              )}
            </div>
          )}
        </div>

        <div className="ml-auto flex items-center gap-1.5">
          {/* Call-minutes usage against the plan allowance (owner-scoped).
              Amber at 80%, rose at 100% — the same thresholds the Billing page
              and the Overview chip use, so the whole app agrees about when a
              tenant is close to being blocked. Links to /billing because that
              is the only place the number can be acted on. */}
          {(() => {
            const used = minutes.data?.minutes_used_period ?? minutes.data?.minutes_month ?? null
            const limit = minutes.data?.minutes_limit ?? null
            const pct = limit != null && limit > 0 && used != null ? used / limit : 0
            const tone = limit == null ? '' : pct >= 1
              ? 'text-rose-600 ring-rose-300 dark:text-rose-300 dark:ring-rose-900'
              : pct >= 0.8
                ? 'text-amber-600 ring-amber-300 dark:text-amber-300 dark:ring-amber-900'
                : ''
            const label = limit == null
              ? `Call minutes used${used != null ? `: ${num(used)}` : ''}`
              : `${num(used ?? 0)} of ${num(limit)} minutes used this period`
            return (
              <Link
                to="/billing"
                title={label}
                aria-label={label}
                className={cx(
                  'flex h-8 shrink-0 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium text-soft ring-1 ring-[var(--border)] hover:bg-[var(--surface)]',
                  tone,
                )}
              >
                <Timer size={13} className={tone ? '' : 'text-dim'} />
                <span className="tabular-nums">
                  {used == null ? '— min'
                    : limit == null ? `${num(used)} min`
                    : `${num(used)}/${num(limit)}`}
                </span>
                <span className="hidden text-dim sm:inline">
                  {limit == null ? 'this month' : 'min'}
                </span>
              </Link>
            )
          })()}
          <span className="hidden px-1.5 text-xs font-medium tabular-nums text-dim md:block" title="Live clock">{now}</span>

          {/* Notification center — real intelligence notifications + service health */}
          <Popover>
            <PopoverTrigger aria-label="Notifications" title="Notifications" className={cx(ICON_BTN, 'relative')}>
              <Bell size={16} />
              {attention && (
                unread > 0 ? (
                  <span className="absolute -right-0.5 -top-0.5 grid min-w-[16px] place-items-center rounded-full bg-rose-500 px-1 py-px text-2xs font-bold leading-none text-white">
                    {unread > 9 ? '9+' : unread}
                  </span>
                ) : (
                  <span className="absolute right-1.5 top-1.5 h-1.5 w-1.5 rounded-full bg-rose-500" />
                )
              )}
            </PopoverTrigger>
            <PopoverContent align="end" sideOffset={8} className="card w-80 rounded-2xl border-0 p-2 shadow-glass-lg">
              <div className="flex items-center justify-between px-2 pb-2 pt-1">
                <span className="eyebrow">Notifications</span>
                {unread > 0 && (
                  <button type="button" onClick={markAllRead}
                    className="flex items-center gap-1 rounded px-1.5 py-0.5 text-2xs font-semibold text-indigo-600 transition-colors hover:bg-[var(--surface-2)] dark:text-indigo-300">
                    <CheckCheck size={12} /> Mark all read
                  </button>
                )}
              </div>
              {downServices.map((s) => (
                <div key={s.name} className="flex items-center gap-2 rounded-md px-2 py-2 text-sm text-soft">
                  <XCircle size={15} className="shrink-0 text-rose-500 dark:text-rose-300" />
                  <span><span className="font-medium">{s.name}</span> is down</span>
                </div>
              ))}
              {items.length === 0 && downServices.length === 0 && (
                <div className="flex items-center gap-2 rounded-md px-2 py-2 text-sm text-soft">
                  <CheckCircle2 size={15} className="shrink-0 text-mint-600 dark:text-mint-400" />
                  All clear — nothing needs your attention.
                </div>
              )}
              {items.length > 0 && (
                <div data-lenis-prevent className="max-h-80 overflow-y-auto">
                  {items.map((n) => (
                    <button key={n.id} type="button" onClick={() => openNotification(n)}
                      className={cx('flex w-full items-start gap-2.5 rounded-md px-2 py-2 text-left transition-colors hover:bg-[var(--surface-2)]',
                        n.read_at && 'opacity-60')}>
                      <span aria-hidden className={cx('mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full', n.read_at ? 'bg-[var(--border-strong)]' : severityDot(n.severity))} />
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-sm font-medium">{n.title}</span>
                        {n.body && <span className="mt-0.5 line-clamp-2 block text-xs text-dim">{n.body}</span>}
                      </span>
                      <span className="shrink-0 pt-0.5 text-2xs tabular-nums text-dim">{ago(n.created_at)}</span>
                    </button>
                  ))}
                </div>
              )}
            </PopoverContent>
          </Popover>

          <Popover>
            <PopoverTrigger aria-label="Help" title="Help" className={cx(ICON_BTN, 'hidden sm:grid')}>
              <HelpCircle size={16} />
            </PopoverTrigger>
            <PopoverContent align="end" sideOffset={8} className="card w-72 rounded-2xl border-0 p-2 shadow-glass-lg">
              <div className="eyebrow px-2 pb-2 pt-1">Help</div>
              <div className="flex items-start gap-2 rounded-md px-2 py-2 text-sm text-soft">
                <MessageCircle size={15} className="mt-0.5 shrink-0 text-indigo-500 dark:text-indigo-300" />
                <span>Ask the <button type="button" onClick={() => navigate('/copilot')} className="font-medium text-indigo-600 hover:underline dark:text-indigo-300">Copilot</button> anything — it can create and edit agents for you.</span>
              </div>
              <div className="flex items-start gap-2 rounded-md px-2 py-2 text-sm text-soft">
                <Keyboard size={15} className="mt-0.5 shrink-0 text-dim" />
                <span>Press <kbd className="rounded border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 text-2xs font-medium">/</kbd> or <kbd className="rounded border border-[var(--border)] bg-[var(--surface-2)] px-1.5 py-0.5 text-2xs font-medium">{IS_MAC ? '⌘K' : 'Ctrl K'}</kbd> to jump to any panel.</span>
              </div>
              <div className="flex items-start gap-2 rounded-md px-2 py-2 text-sm text-soft">
                <HelpCircle size={15} className="mt-0.5 shrink-0 text-dim" />
                <span>Need a human? Write to <a href="mailto:tech@dhronai.com" className="font-medium text-indigo-600 hover:underline dark:text-indigo-300">tech@dhronai.com</a>.</span>
              </div>
            </PopoverContent>
          </Popover>

          <IconBtn onClick={toggle} label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}>
            {theme === 'dark' ? <Sun size={16} /> : <Moon size={16} />}
          </IconBtn>
        </div>
      </div>
    </header>
  )
}
