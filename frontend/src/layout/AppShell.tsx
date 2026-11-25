import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { useLenis } from 'lenis/react'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'
import { NAV } from '../lib/nav'
import { useHealth } from '../api/hooks/useDashboard'
import { cx } from '../components/ui'
import type { ReactNode } from 'react'

// Longest-prefix match so sub-routes (/build/agents/new, /build/agents/:name)
// inherit their parent panel's title instead of falling back to "Overview".
function titleFor(path: string) {
  // Pages that are deliberately absent from the sidebar still need a name in
  // the tab title and the topbar.
  if (path.startsWith('/developers/reference')) return 'API reference'
  let best = ''
  let label = 'Overview'
  for (const g of NAV) for (const i of g.items) {
    if (path === i.to || (i.to !== '/' && path.startsWith(i.to + '/'))) {
      if (i.to.length > best.length) { best = i.to; label = i.label }
    }
  }
  return label
}

export function AppShell({ children }: { children: ReactNode }) {
  const [collapsed, setCollapsed] = useState(false)
  const [mobileOpen, setMobileOpen] = useState(false)
  const { pathname } = useLocation()
  const { services, overall, liveCount } = useHealth()
  const lenis = useLenis()

  // Navigating closes the mobile drawer.
  useEffect(() => { setMobileOpen(false) }, [pathname])

  // Publish the sidebar state to <html> so shell-scoped overlays (the live
  // transcript) can resolve --shell-nav-w in CSS and follow the sidebar when
  // it collapses — they render in a portal on <body>, far outside this tree.
  useEffect(() => {
    document.documentElement.dataset.nav = collapsed ? 'collapsed' : 'expanded'
  }, [collapsed])

  // Each panel opens at its top — an inherited scroll offset on a fresh page
  // reads as a broken load. Immediate (not smoothed): the content under the
  // scrollbar is already new, so easing would animate the wrong page.
  useEffect(() => {
    if (lenis) lenis.scrollTo(0, { immediate: true, force: true })
    else window.scrollTo(0, 0)
  }, [pathname, lenis])

  // Tab title tracks the active panel (helps users with several tabs open).
  useEffect(() => { document.title = `${titleFor(pathname)} · namaskall` }, [pathname])

  return (
    <div className="min-h-screen bg-[var(--bg)] bg-mesh-light dark:bg-mesh-dark">
      <Sidebar collapsed={collapsed} liveCount={liveCount} mobileOpen={mobileOpen} onCloseMobile={() => setMobileOpen(false)} />
      <div className={cx('transition-all duration-300', collapsed ? 'lg:pl-[92px]' : 'lg:pl-[260px]')}>
        {/* Full-bleed: panels use the whole browser width (user request) */}
        <div className="px-4 pb-12 sm:px-6 lg:px-8">
          <Topbar
            title={titleFor(pathname)}
            onToggleSidebar={() => setCollapsed((c) => !c)}
            onOpenMobileNav={() => setMobileOpen(true)}
            services={services}
            overall={overall}
          />
          {/* key remounts main per route so the entrance animation replays */}
          <main key={pathname} className="animate-fade-up">{children}</main>
        </div>
      </div>
    </div>
  )
}
