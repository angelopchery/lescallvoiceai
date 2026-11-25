import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import { AnimatePresence, motion } from 'motion/react'
import { CheckCircle2, AlertTriangle, Info, XCircle, X } from 'lucide-react'
import { cx } from './ui'

type ToastType = 'success' | 'error' | 'info' | 'warn'
type Toast = { id: number; text: string; type: ToastType }

const ToastCtx = createContext<(text: string, type?: ToastType) => void>(() => {})

export function useToast() {
  return useContext(ToastCtx)
}

const ICONS = { success: CheckCircle2, error: XCircle, warn: AlertTriangle, info: Info }
const TONES: Record<ToastType, string> = {
  success: 'text-mint-600 dark:text-mint-300 ring-mint-400/30',
  error: 'text-rose-500 dark:text-rose-300 ring-rose-500/30',
  warn: 'text-amber-600 dark:text-amber-300 ring-amber-400/30',
  info: 'text-indigo-500 dark:text-indigo-300 ring-indigo-500/30',
}

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])
  const seq = useRef(0)
  const timers = useRef(new Map<number, ReturnType<typeof setTimeout>>())

  const dismissLater = useCallback((id: number, ms: number) => {
    timers.current.set(id, setTimeout(() => {
      timers.current.delete(id)
      setToasts((t) => t.filter((x) => x.id !== id))
    }, ms))
  }, [])

  const push = useCallback((text: string, type: ToastType = 'info') => {
    const id = ++seq.current
    setToasts((t) => [...t, { id, text, type }])
    // Errors deserve more reading time than confirmations.
    dismissLater(id, type === 'error' ? 8000 : 5000)
  }, [dismissLater])

  // Hovering pauses auto-dismiss; leaving restarts a short countdown.
  const pause = useCallback((id: number) => {
    const t = timers.current.get(id)
    if (t) { clearTimeout(t); timers.current.delete(id) }
  }, [])
  const resume = useCallback((id: number) => { dismissLater(id, 2000) }, [dismissLater])

  return (
    <ToastCtx.Provider value={push}>
      {children}
      <div role="status" aria-live="polite" className="pointer-events-none fixed bottom-4 right-4 z-[100] flex w-[min(92vw,360px)] flex-col gap-2">
        {/* layout + AnimatePresence: new toasts spring in, dismissed ones fade
            out, and the survivors glide into the freed space. */}
        <AnimatePresence initial={false}>
          {toasts.map((t) => {
            const Icon = ICONS[t.type]
            return (
              <motion.div key={t.id} layout
                initial={{ opacity: 0, y: 16, scale: 0.97 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={{ opacity: 0, y: 8, scale: 0.97 }}
                transition={{ type: 'spring', stiffness: 480, damping: 38 }}
                onMouseEnter={() => pause(t.id)}
                onMouseLeave={() => resume(t.id)}
                className={cx('glass pointer-events-auto flex items-start gap-2.5 rounded-xl px-3.5 py-3 text-sm font-medium shadow-glass-lg ring-1', TONES[t.type])}>
                <Icon size={16} className="mt-0.5 shrink-0" />
                <span className="flex-1 text-[var(--text)]">{t.text}</span>
                <button onClick={() => setToasts((all) => all.filter((x) => x.id !== t.id))} aria-label="Dismiss"
                  className="grid h-5 w-5 shrink-0 place-items-center rounded-md text-dim transition hover:bg-[var(--surface-2)] hover:text-[var(--text)]">
                  <X size={13} />
                </button>
              </motion.div>
            )
          })}
        </AnimatePresence>
      </div>
    </ToastCtx.Provider>
  )
}
