import { createContext, useCallback, useContext, useRef, useState, type ReactNode } from 'react'
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Input } from './kit'
import { cx } from './ui'

export type ConfirmOptions = {
  title: string
  description?: string
  confirmLabel?: string
  cancelLabel?: string
  /** Styles the confirm action rose and defaults the label to "Delete". */
  destructive?: boolean
}

export type PromptOptions = {
  title: string
  description?: string
  placeholder?: string
  initial?: string
  confirmLabel?: string
  /** Return an error string to block submission, or null/undefined to allow. */
  validate?: (value: string) => string | null | undefined
}

type ConfirmFn = (opts: ConfirmOptions) => Promise<boolean>
type PromptFn = (opts: PromptOptions) => Promise<string | null>

const ConfirmCtx = createContext<{ confirm: ConfirmFn; prompt: PromptFn } | null>(null)

/** App-wide replacement for window.confirm / window.prompt — focus-managed,
 *  styled, and awaitable: `if (await confirm({ title: 'Delete?' })) …` */
export function useConfirm(): ConfirmFn {
  const ctx = useContext(ConfirmCtx)
  if (!ctx) throw new Error('useConfirm requires <ConfirmProvider>')
  return ctx.confirm
}

export function usePrompt(): PromptFn {
  const ctx = useContext(ConfirmCtx)
  if (!ctx) throw new Error('usePrompt requires <ConfirmProvider>')
  return ctx.prompt
}

type Pending =
  | { kind: 'confirm'; opts: ConfirmOptions; resolve: (v: boolean) => void }
  | { kind: 'prompt'; opts: PromptOptions; resolve: (v: string | null) => void }

export function ConfirmProvider({ children }: { children: ReactNode }) {
  const [pending, setPending] = useState<Pending | null>(null)
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  // Ref mirror so late Radix onOpenChange(false) after resolve can't double-fire.
  const pendingRef = useRef<Pending | null>(null)

  const settle = useCallback((result: boolean | string | null) => {
    const p = pendingRef.current
    pendingRef.current = null
    setPending(null)
    if (!p) return
    if (p.kind === 'confirm') p.resolve(Boolean(result))
    else p.resolve(typeof result === 'string' ? result : null)
  }, [])

  const confirm = useCallback<ConfirmFn>((opts) => new Promise((resolve) => {
    const p: Pending = { kind: 'confirm', opts, resolve }
    pendingRef.current = p
    setPending(p)
  }), [])

  const prompt = useCallback<PromptFn>((opts) => new Promise((resolve) => {
    const p: Pending = { kind: 'prompt', opts, resolve }
    pendingRef.current = p
    setValue(opts.initial ?? '')
    setError(null)
    setPending(p)
  }), [])

  const submitPrompt = () => {
    if (!pending || pending.kind !== 'prompt') return
    const err = pending.opts.validate?.(value)
    if (err) { setError(err); return }
    settle(value)
  }

  const opts = pending?.opts
  const destructive = pending?.kind === 'confirm' && (pending.opts.destructive ?? false)

  return (
    <ConfirmCtx.Provider value={{ confirm, prompt }}>
      {children}
      <AlertDialog open={pending != null} onOpenChange={(open) => { if (!open) settle(null) }}>
        <AlertDialogContent className="card max-w-sm border-0 p-5">
          <AlertDialogHeader>
            <AlertDialogTitle className="font-display text-base font-bold tracking-tight">
              {opts?.title}
            </AlertDialogTitle>
            {opts?.description && (
              <AlertDialogDescription className="text-sm text-dim">
                {opts.description}
              </AlertDialogDescription>
            )}
          </AlertDialogHeader>
          {pending?.kind === 'prompt' && (
            <div className="mt-1">
              <Input
                autoFocus
                value={value}
                placeholder={pending.opts.placeholder}
                onChange={(e) => { setValue(e.target.value); setError(null) }}
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submitPrompt() } }}
                aria-invalid={error ? true : undefined}
                className={cx(error && '!ring-rose-500/40')}
              />
              {error && <p role="alert" className="mt-1.5 text-xs font-medium text-rose-600 dark:text-rose-300">{error}</p>}
            </div>
          )}
          <AlertDialogFooter className="mt-2">
            <AlertDialogCancel
              onClick={() => settle(null)}
              className="min-h-[36px] rounded-md border-0 bg-[var(--surface-2)] px-3.5 text-sm font-medium text-soft ring-1 ring-[var(--border)] hover:bg-[var(--surface-2)] hover:ring-[var(--border-strong)] sm:min-h-0">
              {opts && 'cancelLabel' in opts && opts.cancelLabel ? opts.cancelLabel : 'Cancel'}
            </AlertDialogCancel>
            {pending?.kind === 'confirm' ? (
              <AlertDialogAction
                onClick={() => settle(true)}
                className={cx('min-h-[36px] rounded-md px-3.5 text-sm font-medium sm:min-h-0',
                  destructive
                    ? 'bg-rose-600 text-white hover:bg-rose-500'
                    : 'bg-mint-600 text-white hover:bg-mint-500 dark:bg-mint-500 dark:text-[#04150d] dark:hover:bg-mint-400')}>
                {pending.opts.confirmLabel ?? (destructive ? 'Delete' : 'Confirm')}
              </AlertDialogAction>
            ) : (
              <AlertDialogAction
                onClick={(e) => { e.preventDefault(); submitPrompt() }}
                className="min-h-[36px] rounded-md bg-mint-600 px-3.5 text-sm font-medium text-white hover:bg-mint-500 dark:bg-mint-500 dark:text-[#04150d] dark:hover:bg-mint-400 sm:min-h-0">
                {pending?.opts.confirmLabel ?? 'Save'}
              </AlertDialogAction>
            )}
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </ConfirmCtx.Provider>
  )
}
