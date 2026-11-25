import { useState, type ReactNode } from 'react'
import { Check, Copy } from 'lucide-react'
import { IconButton } from './ui'

/** Copy-to-clipboard control. Confirms in place — a toast for "copied" is more
 *  interruption than the action deserves. */
export function CopyButton({ text, label }: { text: string; label: string }) {
  const [done, setDone] = useState(false)
  return (
    <IconButton label={done ? 'Copied' : label} onClick={async () => {
      try {
        await navigator.clipboard.writeText(text)
        setDone(true)
        setTimeout(() => setDone(false), 1600)
      } catch { /* clipboard blocked — the text is selectable anyway */ }
    }}>
      {done ? <Check size={14} className="text-mint-600 dark:text-mint-300" /> : <Copy size={14} />}
    </IconButton>
  )
}

/** A labelled, copyable code block.
 *
 *  `break-inside: avoid` matters here: this component is the main thing on the
 *  printable API reference, and a curl command split across a page break is
 *  worse than a short page. */
export function Snippet({ code, label, children }: {
  code: string
  label?: string
  /** Optional syntax-highlighted body; falls back to the raw `code` text. */
  children?: ReactNode
}) {
  return (
    <div className="break-inside-avoid overflow-hidden rounded-md bg-[var(--surface-2)] ring-1 ring-[var(--border)]">
      {label && (
        <div className="flex items-center justify-between gap-2 border-b border-[var(--border)] px-3 py-1.5">
          <span className="text-2xs font-semibold uppercase tracking-wide text-dim">{label}</span>
          <span data-print-hide><CopyButton text={code} label={`Copy the ${label} example`} /></span>
        </div>
      )}
      <pre className="overflow-x-auto px-3 py-2 text-xs leading-relaxed text-soft">
        <code>{children ?? code}</code>
      </pre>
    </div>
  )
}
