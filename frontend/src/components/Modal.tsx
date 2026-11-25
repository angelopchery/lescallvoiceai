import { useEffect, useRef, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { AnimatePresence, motion } from 'motion/react'
import { X } from 'lucide-react'
import { cx } from './ui'

/**
 * Every overlay renders into <body>, never in place.
 *
 * `position: fixed` is only viewport-relative while no ancestor establishes a
 * containing block — and any ancestor with a transform does. The app shell's
 * <main> carries `animate-fade-up`, whose `both` fill-mode leaves
 * `transform: translateY(0)` applied forever after the entrance animation, so
 * an inline `fixed inset-0` dialog resolved against the padded content column
 * (offset by the sidebar, clipped to content height) instead of the browser
 * window. Portalling to <body> is the fix, and it keeps working no matter what
 * transform/filter/backdrop a future page wrapper introduces.
 */
function Portal({ children }: { children: ReactNode }) {
  if (typeof document === 'undefined') return null
  return createPortal(children, document.body)
}

function useEsc(onClose: () => void) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
}

/** Lock body scroll while an overlay is open. */
function useScrollLock(open: boolean) {
  useEffect(() => {
    if (!open) return
    const prev = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => { document.body.style.overflow = prev }
  }, [open])
}

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'

/**
 * Move focus into the dialog on open, keep Tab cycling inside it, and hand
 * focus back to the opener on close — without this, Tab walks the page
 * hidden behind the backdrop.
 *
 * `trap: false` keeps the focus handoff but lets Tab leave: a shell-scoped
 * overlay leaves the sidebar and topbar visible and clickable, so caging the
 * keyboard inside it while the mouse can reach the nav would be a lie.
 */
function useFocusTrap(open: boolean, trap = true) {
  const panelRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const opener = document.activeElement as HTMLElement | null
    const raf = requestAnimationFrame(() => {
      panelRef.current?.querySelector<HTMLElement>(FOCUSABLE)?.focus()
    })
    const h = (e: KeyboardEvent) => {
      if (!trap || e.key !== 'Tab' || !panelRef.current) return
      const items = Array.from(panelRef.current.querySelectorAll<HTMLElement>(FOCUSABLE))
        .filter((el) => el.offsetParent !== null)
      if (!items.length) return
      const first = items[0], last = items[items.length - 1]
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus() }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus() }
    }
    window.addEventListener('keydown', h)
    return () => {
      cancelAnimationFrame(raf)
      window.removeEventListener('keydown', h)
      opener?.focus?.()
    }
  }, [open, trap])
  return panelRef
}

const backdropAnim = {
  initial: { opacity: 0 },
  animate: { opacity: 1 },
  exit: { opacity: 0 },
  transition: { duration: 0.2 },
} as const

/** Centered modal dialog. */
export function Modal({
  open, onClose, title, children, footer, wide,
}: { open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; footer?: ReactNode; wide?: boolean }) {
  useEsc(onClose)
  useScrollLock(open)
  const panelRef = useFocusTrap(open)
  return (
    <Portal>
    <AnimatePresence>
      {open && (
        <div role="dialog" aria-modal="true" aria-labelledby={title ? 'modal-title' : undefined}>
          <motion.div {...backdropAnim} className="fixed inset-0 z-[80] bg-zinc-950/30" aria-hidden="true" />
          {/* Centering wrapper — clicks on it (i.e. outside the panel) close,
              same as the backdrop it covers. */}
          <div className="fixed inset-0 z-[90] grid place-items-center p-4" onClick={onClose}>
            <motion.div
              ref={panelRef}
              onClick={(e) => e.stopPropagation()}
              initial={{ opacity: 0, y: 14, scale: 0.97 }}
              animate={{ opacity: 1, y: 0, scale: 1 }}
              exit={{ opacity: 0, y: 8, scale: 0.98 }}
              transition={{ type: 'spring', stiffness: 380, damping: 32 }}
              className={cx('card w-full p-5 shadow-glass-lg', wide ? 'max-w-2xl' : 'max-w-md')}>
              {title && (
                <div className="mb-3 flex items-center justify-between gap-3">
                  <h3 id="modal-title" className="font-display text-lg font-semibold tracking-tight">{title}</h3>
                  <button onClick={onClose} aria-label="Close dialog" className="grid h-8 w-8 place-items-center rounded-lg text-dim transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]"><X size={16} /></button>
                </div>
              )}
              <div data-lenis-prevent className="max-h-[70vh] overflow-y-auto">{children}</div>
              {footer && <div className="mt-4 flex justify-end gap-2">{footer}</div>}
            </motion.div>
          </div>
        </div>
      )}
    </AnimatePresence>
    </Portal>
  )
}

/**
 * Large dialog for content that deserves the room — e.g. a live transcript.
 *
 * `scope="viewport"` (default) covers the entire browser window.
 * `scope="shell"` fills only the content region: the sidebar and topbar stay
 * visible AND clickable, so you can still see where you are and navigate away
 * without closing the panel. The insets come from --shell-nav-w/--shell-top-h
 * (see index.css), which track the sidebar's collapsed state on their own.
 *
 * Viewport height is `100dvh`, not `100vh`: on mobile the URL bar and toolbar
 * are part of `100vh`, so the last line of a transcript (and any footer) ends
 * up hidden under browser chrome. `dvh` tracks the actually-visible viewport.
 */
export function FullscreenModal({
  open, onClose, title, children, headerExtra, footer, scope = 'viewport',
}: {
  open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode
  headerExtra?: ReactNode; footer?: ReactNode; scope?: 'viewport' | 'shell'
}) {
  const shell = scope === 'shell'
  useEsc(onClose)
  useScrollLock(open)
  const panelRef = useFocusTrap(open, !shell)
  return (
    <Portal>
    <AnimatePresence>
      {open && (
        <div role="dialog" aria-modal={shell ? undefined : true} aria-labelledby={title ? 'fullscreen-modal-title' : undefined}>
          {/* Shell scope dims only the content region — a scrim over the nav
              would read as "disabled" for chrome that still works. It also
              sits under the sidebar (z-50) and topbar menus (z-40). */}
          <motion.div
            {...backdropAnim}
            aria-hidden="true"
            className={cx(
              'fixed bg-zinc-950/40',
              shell
                ? 'bottom-0 right-0 z-[30] left-[var(--shell-nav-w)] top-[var(--shell-top-h)] transition-[left] duration-300'
                : 'inset-0 z-[80]',
            )}
          />
          <motion.div
            ref={panelRef}
            initial={{ opacity: 0, scale: 0.985 }}
            animate={{ opacity: 1, scale: 1 }}
            exit={{ opacity: 0, scale: 0.99 }}
            transition={{ duration: 0.22, ease: [0.22, 1, 0.36, 1] }}
            className={cx(
              'fixed flex flex-col overflow-hidden bg-[var(--surface-solid)] shadow-glass-lg',
              shell
                ? 'bottom-0 right-0 z-[31] left-[var(--shell-nav-w)] top-[var(--shell-top-h)] transition-[left] duration-300 lg:rounded-tl-2xl lg:border-l lg:border-t lg:border-[var(--border)]'
                : 'inset-x-0 top-0 z-[90] h-[100dvh]',
            )}
          >
            {/* Header stays put while the body scrolls — on a long call you
                always keep the number, timer and controls in view. */}
            <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-[var(--border)] px-4 py-3 sm:px-6">
              <h3 id="fullscreen-modal-title" className="min-w-0 flex-1 truncate font-display text-lg font-bold tracking-tight sm:text-xl">
                {title}
              </h3>
              {headerExtra}
              <button onClick={onClose} aria-label="Close dialog" title="Close (Esc)"
                className="grid h-9 w-9 shrink-0 place-items-center rounded-lg text-dim transition-colors hover:bg-[var(--surface-2)] hover:text-[var(--text)]">
                <X size={18} />
              </button>
            </div>
            {children}
            {footer && (
              <div className="shrink-0 border-t border-[var(--border)] px-4 py-3 sm:px-6">{footer}</div>
            )}
          </motion.div>
        </div>
      )}
    </AnimatePresence>
    </Portal>
  )
}

/** Right-side slide-over panel (used for transcripts / detail views). */
export function SlideOver({
  open, onClose, title, children, headerExtra,
}: { open: boolean; onClose: () => void; title?: ReactNode; children: ReactNode; headerExtra?: ReactNode }) {
  useEsc(onClose)
  useScrollLock(open)
  const panelRef = useFocusTrap(open)
  return (
    <Portal>
    <AnimatePresence>
      {open && (
        <div className="fixed inset-0 z-[80]" role="dialog" aria-modal="true" aria-labelledby={title ? 'slideover-title' : undefined}>
          <motion.div {...backdropAnim} className="absolute inset-0 bg-zinc-950/30" onClick={onClose} aria-hidden="true" />
          <motion.div
            ref={panelRef}
            initial={{ x: '100%' }}
            animate={{ x: 0 }}
            exit={{ x: '100%' }}
            transition={{ type: 'spring', stiffness: 360, damping: 40 }}
            className="absolute right-0 top-0 flex h-full w-[min(94vw,560px)] flex-col bg-[var(--surface-solid)] shadow-glass-lg">
            <div className="flex items-center justify-between gap-3 border-b border-[var(--border)] px-5 py-4">
              <div className="min-w-0 flex-1">
                <h3 id="slideover-title" className="truncate font-display text-lg font-bold tracking-tight">{title}</h3>
              </div>
              {headerExtra}
              <button onClick={onClose} aria-label="Close panel" className="grid h-8 w-8 shrink-0 place-items-center rounded-lg text-dim hover:bg-[var(--surface-2)]"><X size={16} /></button>
            </div>
            <div data-lenis-prevent className="flex-1 overflow-y-auto p-5">{children}</div>
          </motion.div>
        </div>
      )}
    </AnimatePresence>
    </Portal>
  )
}
