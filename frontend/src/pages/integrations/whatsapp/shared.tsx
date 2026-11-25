import type { ReactNode } from 'react'
import { ExternalLink, FileText, Image as ImageIcon, Video } from 'lucide-react'
import { Badge, cx } from '../../../components/ui'
import type { WaPreview } from '../../../api/whatsapp'

/* ------------------------------------------------------------------ status */

const STATUS: Record<string, { label: string; tone: 'mint' | 'indigo' | 'amber' | 'rose' | 'neutral' }> = {
  pending_approval: { label: 'Awaiting approval', tone: 'amber' },
  queued: { label: 'Queued', tone: 'indigo' },
  sending: { label: 'Sending', tone: 'indigo' },
  sent: { label: 'Sent', tone: 'mint' },
  delivered: { label: 'Delivered', tone: 'mint' },
  read: { label: 'Read', tone: 'mint' },
  failed: { label: 'Failed', tone: 'rose' },
  blocked: { label: 'Blocked', tone: 'rose' },
  skipped: { label: 'Skipped', tone: 'neutral' },
  expired: { label: 'Expired', tone: 'neutral' },
}

export function WaStatusBadge({ status }: { status: string }) {
  const s = STATUS[status] ?? { label: status.replace(/_/g, ' '), tone: 'neutral' as const }
  return <Badge tone={s.tone}>{s.label}</Badge>
}

const TEMPLATE_TONE: Record<string, 'mint' | 'amber' | 'rose' | 'neutral'> = {
  APPROVED: 'mint', PENDING: 'amber', IN_APPEAL: 'amber', REJECTED: 'rose',
  PAUSED: 'rose', DISABLED: 'rose',
}

export function TemplateStatusBadge({ status }: { status: string | null }) {
  const s = (status || 'UNKNOWN').toUpperCase()
  const label = s === 'PENDING' ? 'In review' : s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, ' ')
  return <Badge tone={TEMPLATE_TONE[s] ?? 'neutral'}>{label}</Badge>
}

export const LANG_LABEL: Record<string, string> = {
  en: 'English', en_US: 'English (US)', en_GB: 'English (UK)', hi: 'Hindi', gu: 'Gujarati',
  mr: 'Marathi', ta: 'Tamil', te: 'Telugu', bn: 'Bengali', kn: 'Kannada', ml: 'Malayalam',
}
export const langLabel = (code?: string | null) => (code ? LANG_LABEL[code] ?? code : '—')

/* ------------------------------------------------------------------ bubble */

const MEDIA_ICON: Record<string, typeof FileText> = { document: FileText, image: ImageIcon, video: Video }

/**
 * The message roughly as it lands on the customer's phone — header, body,
 * footer, buttons. A value still missing shows as its raw {{variable}} so the
 * gap is visible instead of silently blank.
 */
export function MessageBubble({ preview, attachment, empty }: {
  preview: WaPreview | null; attachment?: string | null; empty?: ReactNode
}) {
  if (!preview) {
    return (
      <div className="rounded-2xl bg-[var(--surface-2)] p-4 text-center text-xs text-dim ring-1 ring-[var(--border)]">
        {empty ?? 'Pick a template to see the message.'}
      </div>
    )
  }
  const Icon = preview.header_media ? MEDIA_ICON[preview.header_media] ?? FileText : null
  return (
    <div className="rounded-2xl bg-[var(--surface-2)] p-3 ring-1 ring-[var(--border)]">
      <div className="max-w-sm overflow-hidden rounded-xl rounded-tl-sm bg-[var(--surface-solid)] shadow-sm ring-1 ring-mint-400/25">
        {Icon && (
          <div className="flex items-center gap-2 border-b border-[var(--border)] bg-mint-400/10 px-3 py-2.5 text-xs font-medium text-soft">
            <Icon size={16} className="shrink-0 text-mint-600 dark:text-mint-400" />
            <span className="truncate">{attachment || `Attachment (${preview.header_media})`}</span>
          </div>
        )}
        <div className="space-y-1.5 px-3 py-2.5">
          {preview.header && <div className="text-sm font-semibold text-[var(--text)]">{preview.header}</div>}
          <p className="whitespace-pre-wrap break-words text-sm leading-relaxed text-[var(--text)]">
            <Highlighted text={preview.body} />
          </p>
          {preview.footer && <div className="text-2xs text-dim">{preview.footer}</div>}
        </div>
        {preview.buttons.length > 0 && (
          <div className="divide-y divide-[var(--border)] border-t border-[var(--border)]">
            {preview.buttons.map((b, i) => (
              <div key={i} className="flex items-center justify-center gap-1.5 px-3 py-2 text-xs font-semibold text-indigo-600 dark:text-indigo-300"
                title={b.url ?? undefined}>
                {b.type === 'URL' && <ExternalLink size={12} />}
                {b.text}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  )
}

/** Marks a value that is still an unfilled {{variable}}. */
function Highlighted({ text }: { text: string }) {
  const parts = text.split(/(\{\{[A-Za-z0-9_]+\}\})/g)
  return (
    <>
      {parts.map((p, i) => /^\{\{[A-Za-z0-9_]+\}\}$/.test(p)
        ? <span key={i} className="rounded bg-amber-400/20 px-0.5 font-mono text-xs text-amber-700 dark:text-amber-300">{p}</span>
        : <span key={i}>{p}</span>)}
    </>
  )
}

/* ------------------------------------------------------------------ tokens */

export const TOKEN_HELP: { token: string; hint: string }[] = [
  { token: '{greeting}', hint: '“Mr. Rakesh Shah” — title only when the contact’s gender is on file' },
  { token: '{contact_name}', hint: 'Full name from the phone book or the call' },
  { token: '{first_name}', hint: 'First word of the name' },
  { token: '{title}', hint: 'Mr./Mrs. — empty when gender is unknown' },
  { token: '{honorific}', hint: 'Sir/Ma’am — empty when gender is unknown' },
  { token: '{project_name}', hint: 'The project or product the call settled on' },
  { token: '{visit_date}', hint: 'Booked visit day' },
  { token: '{visit_time}', hint: 'Booked visit time' },
  { token: '{agent_name}', hint: 'Your agent’s name' },
  { token: '{company_name}', hint: 'Your company name from the agent' },
  { token: '{link}', hint: 'The playbook’s tracked link' },
  { token: '{slot:company_name}', hint: 'Any detail the agent collected, by field name' },
]

/** Clickable placeholder chips that append to a field. */
export function TokenChips({ onPick, tokens = TOKEN_HELP }: {
  onPick: (token: string) => void; tokens?: { token: string; hint: string }[]
}) {
  return (
    <div className="flex flex-wrap gap-1">
      {tokens.map((t) => (
        <button key={t.token} type="button" title={t.hint} onClick={() => onPick(t.token)}
          className="rounded-full bg-[var(--surface-2)] px-2 py-0.5 font-mono text-2xs text-soft ring-1 ring-[var(--border)] transition hover:text-[var(--text)] hover:ring-mint-400/40">
          {t.token}
        </button>
      ))}
    </div>
  )
}

export function Hint({ children, tone = 'neutral', className }: {
  children: ReactNode; tone?: 'neutral' | 'amber' | 'rose' | 'mint'; className?: string
}) {
  const tones = {
    neutral: 'bg-[var(--surface-2)] text-soft ring-[var(--border)]',
    amber: 'bg-amber-400/10 text-amber-700 ring-amber-400/25 dark:text-amber-300',
    rose: 'bg-rose-500/10 text-rose-600 ring-rose-500/20 dark:text-rose-300',
    mint: 'bg-mint-400/10 text-mint-700 ring-mint-400/25 dark:text-mint-300',
  }
  return <div className={cx('rounded-xl px-3.5 py-2.5 text-xs leading-relaxed ring-1', tones[tone], className)}>{children}</div>
}
