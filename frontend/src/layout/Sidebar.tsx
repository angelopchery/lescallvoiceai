import { useEffect, useState } from 'react'
import { NavLink } from 'react-router-dom'
import { motion } from 'motion/react'
import { LogOut, Search } from 'lucide-react'
import { NAV } from '../lib/nav'
import { BrandMark } from '../components/BrandMark'
import { cx } from '../components/ui'
import { useMe } from '../api/hooks/useMe'
import { useGet } from '../api/hooks/useApi'

export function Sidebar({ collapsed, liveCount, mobileOpen, onCloseMobile }: {
  collapsed: boolean; liveCount: number; mobileOpen?: boolean; onCloseMobile?: () => void
}) {
  const { data: me } = useMe()
  // WhatsApp messages waiting for a person to approve them. Polled slowly: it
  // changes when a call ends, not second by second.
  const waPending = useGet<{ pending: number }>(
    ['wa', 'pending-count'], '/integrations/whatsapp/pending-count',
    { refetchInterval: 60_000 }).data?.pending ?? 0
  const [filter, setFilter] = useState('')   // mobile-drawer quick filter
  const needle = filter.trim().toLowerCase()
  const groups = NAV
    .map((g) => ({
      ...g,
      items: g.items.filter((it) => !needle || it.label.toLowerCase().includes(needle)),
    }))
    .filter((g) => g.items.length > 0)
  const email = me?.email || ''
  const displayName = me?.name || (email ? email.split('@')[0] : 'Account')
  const initial = (me?.name || email || 'A').trim().charAt(0).toUpperCase() || 'A'

  // Escape closes the mobile drawer.
  useEffect(() => {
    if (!mobileOpen) return
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onCloseMobile?.() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [mobileOpen, onCloseMobile])

  return (
    <>
      {mobileOpen && (
        <div className="fixed inset-0 z-40 bg-zinc-950/30 lg:hidden" onClick={onCloseMobile} aria-hidden="true" />
      )}
      <aside
        aria-label="Main navigation"
        className={cx(
          'fixed z-50 flex flex-col transition-all duration-300',
          // Mobile: solid off-canvas drawer, docked to the edge.
          'inset-y-0 left-0 w-[264px] border-r border-[var(--border)] bg-[var(--surface-solid)]',
          // Desktop: detached glass card floating inside the viewport.
          // lg:overflow-hidden clips the full-bleed dividers to the radius.
          'lg:glass lg:inset-y-3 lg:left-3 lg:rounded-3xl lg:border lg:overflow-hidden',
          collapsed ? 'lg:w-[76px]' : 'lg:w-[244px]',
          mobileOpen ? 'translate-x-0 shadow-glass-lg' : '-translate-x-full',
          'lg:translate-x-0',
        )}>
        {/* Brand */}
        <div className={cx('flex h-14 shrink-0 items-center border-b border-[var(--border)] px-4', collapsed && 'lg:justify-center lg:px-0')}>
          <BrandMark collapsed={collapsed} />
        </div>

        {/* Touch users have no "/" shortcut — give the drawer its own quick filter. */}
        <div className="relative mt-3 px-3 lg:hidden">
          <Search size={14} className="pointer-events-none absolute left-6 top-1/2 -translate-y-1/2 text-dim" />
          <input
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
            placeholder="Find a panel…"
            aria-label="Find a panel"
            className="w-full rounded-md bg-[var(--surface-2)] py-2 pl-9 pr-3 text-sm text-[var(--text)] outline-none ring-1 ring-[var(--border)] transition placeholder:text-dim focus:ring-mint-500/40"
          />
        </div>

        <nav data-lenis-prevent className="mt-3 flex-1 space-y-4 overflow-y-auto px-3 pb-2">
          {groups.map((group, gi) => (
            <div key={group.title}>
              <div className={cx('eyebrow px-2 pb-1.5', collapsed && 'lg:hidden')}>{group.title}</div>
              {collapsed && gi > 0 && <div className="mx-2 mb-2 hidden border-t border-[var(--border)] lg:block" />}
              <div className="space-y-px">
                {group.items.map((item) => (
                  <NavLink key={item.to} to={item.to} end={item.to === '/'}
                    title={collapsed ? item.label : undefined}
                    className={({ isActive }) => cx(
                      'group relative flex items-center gap-2.5 rounded-md px-2 py-[7px] text-sm font-medium transition-colors',
                      collapsed && 'lg:justify-center lg:px-0',
                      isActive
                        ? 'text-[var(--text)]'
                        : 'text-soft hover:bg-[var(--surface-2)] hover:text-[var(--text)]',
                    )}>
                    {({ isActive }) => (
                      <>
                        {/* One shared layoutId: the active pill glides between
                            items instead of blinking off/on. */}
                        {isActive && (
                          <motion.span
                            layoutId="sidebar-active-pill"
                            aria-hidden
                            className="absolute inset-0 rounded-md bg-[var(--surface-2)]"
                            transition={{ type: 'spring', stiffness: 420, damping: 34 }}>
                            <span className="absolute left-0 top-1/2 h-4 w-0.5 -translate-y-1/2 rounded-full bg-mint-500" />
                          </motion.span>
                        )}
                        <span className="relative shrink-0">
                          <item.icon size={16} strokeWidth={2} className={cx('shrink-0', isActive ? 'text-mint-600 dark:text-mint-400' : 'text-dim group-hover:text-soft')} />
                          {/* Collapsed rail still shows the live-call count as a dot badge */}
                          {collapsed && item.badge === 'live' && liveCount > 0 && (
                            <span aria-label={`${liveCount} live ${liveCount === 1 ? 'call' : 'calls'}`}
                              className="absolute -right-2 -top-1.5 hidden h-[15px] min-w-[15px] items-center justify-center rounded-full bg-rose-500 px-1 text-2xs font-bold leading-none text-white ring-2 ring-[var(--bg)] lg:inline-flex">{liveCount}</span>
                          )}
                        </span>
                        <span className={cx('relative truncate', collapsed && 'lg:hidden')}>{item.label}</span>
                        {/* Approvals are work waiting, not an alarm: a quiet amber
                            count, no pulse. Collapsed, it becomes a dot. */}
                        {item.badge === 'wa' && waPending > 0 && (
                          <span aria-label={`${waPending} WhatsApp ${waPending === 1 ? 'message' : 'messages'} waiting for approval`}
                            className={cx('relative ml-auto inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-amber-500 px-1 text-2xs font-semibold text-white',
                              collapsed && 'lg:absolute lg:right-2 lg:top-1 lg:h-2 lg:min-w-0 lg:w-2 lg:p-0')}>
                            <span className={cx(collapsed && 'lg:hidden')}>{waPending}</span>
                          </span>
                        )}
                        {item.badge === 'live' && liveCount > 0 && (
                          <span className={cx('relative ml-auto inline-flex', collapsed && 'lg:hidden')}>
                            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-rose-400 opacity-40" />
                            <span className="relative inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-full bg-rose-500 px-1 text-2xs font-semibold text-white">{liveCount}</span>
                          </span>
                        )}
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
          {/* Scroll cue: hints that more nav items exist below on short viewports */}
          <div aria-hidden="true" className="pointer-events-none sticky bottom-0 -mt-6 h-6 bg-gradient-to-t from-[var(--surface-solid)] to-transparent lg:from-[var(--surface)]" />
        </nav>

        {/* Identity */}
        <div className={cx('flex shrink-0 items-center gap-2.5 border-t border-[var(--border)] px-3 py-3', collapsed && 'lg:flex-col lg:gap-1.5 lg:px-1.5')}
          title={collapsed ? `${displayName}${email ? ` — ${email}` : ''}` : undefined}>
          {me?.picture ? (
            <img src={me.picture} alt="" referrerPolicy="no-referrer"
              className="h-8 w-8 shrink-0 rounded-full object-cover ring-1 ring-[var(--border)]" />
          ) : (
            <div className="grid h-8 w-8 shrink-0 place-items-center rounded-full bg-indigo-600 text-xs font-semibold text-white">{initial}</div>
          )}
          <div className={cx('min-w-0 flex-1 leading-tight', collapsed && 'lg:hidden')}>
            <div className="truncate text-xs font-medium">{displayName}</div>
            <div className="truncate text-2xs text-dim">{email || 'signed in'}</div>
          </div>
          {me?.mode === 'multi-tenant' && (
            // Stays visible in the collapsed rail — signing out must never require expanding the sidebar.
            <a href="/auth/logout" title="Sign out" aria-label="Sign out"
              className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-dim transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]">
              <LogOut size={14} />
            </a>
          )}
        </div>
      </aside>
    </>
  )
}
