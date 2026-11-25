import { useEffect, useState } from 'react'
import { Activity, ChevronDown, CheckCircle2, XCircle } from 'lucide-react'
import { cx } from './ui'

export type Service = { name: string; ok: boolean }

export function HealthPill({ services, overall }: { services: Service[]; overall: 'healthy' | 'degraded' | 'down' }) {
  const [open, setOpen] = useState(false)
  const up = services.filter((s) => s.ok).length
  const tone = overall === 'healthy' ? 'mint' : overall === 'degraded' ? 'amber' : 'rose'
  const toneCls: Record<string, string> = {
    mint: 'text-mint-600 dark:text-mint-300 bg-mint-400/12 ring-mint-400/30',
    amber: 'text-amber-600 dark:text-amber-300 bg-amber-400/15 ring-amber-400/30',
    rose: 'text-rose-500 dark:text-rose-300 bg-rose-500/12 ring-rose-500/30',
  }
  // Escape closes the popover (backdrop click already does).
  useEffect(() => {
    if (!open) return
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [open])
  return (
    <div className="relative">
      <button onClick={() => setOpen((o) => !o)} aria-haspopup="true" aria-expanded={open}
        className={cx('inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-2xs font-medium ring-1 transition', toneCls[tone])}>
        <span className="relative flex h-2 w-2">
          <span className={cx('absolute inline-flex h-full w-full rounded-full opacity-70 animate-ping', tone === 'mint' ? 'bg-mint-400' : tone === 'amber' ? 'bg-amber-400' : 'bg-rose-400')} />
          <span className={cx('relative inline-flex h-2 w-2 rounded-full', tone === 'mint' ? 'bg-mint-500' : tone === 'amber' ? 'bg-amber-500' : 'bg-rose-500')} />
        </span>
        <Activity size={14} />
        <span className="hidden sm:inline">System {overall === 'healthy' ? 'Healthy' : overall === 'degraded' ? 'Degraded' : 'Down'}</span>
        <span className="opacity-60">{up}/{services.length}</span>
        <ChevronDown size={14} className={cx('transition', open && 'rotate-180')} />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-30" onClick={() => setOpen(false)} />
          <div className="card absolute right-0 z-40 mt-2 w-64 p-2 animate-fade-up">
            <div className="px-2 pb-2 pt-1 eyebrow">Service Status</div>
            {services.map((s) => (
              <div key={s.name} className="flex items-center justify-between rounded-lg px-2 py-1.5 text-sm hover:bg-[var(--surface-2)]">
                <span className="text-soft">{s.name}</span>
                {s.ok
                  ? <span className="inline-flex items-center gap-1 text-mint-600 dark:text-mint-300"><CheckCircle2 size={14} />Online</span>
                  : <span className="inline-flex items-center gap-1 text-rose-500 dark:text-rose-300"><XCircle size={14} />Down</span>}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
