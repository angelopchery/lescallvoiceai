/**
 * Share an agent, and import a shared one — the two ends of one feature.
 *
 * They live in one module because they describe the same contract from either
 * side, and the sentence that has to stay true on both is the same:
 *
 *     An imported agent is a COPY. It gets its own name in the recipient's
 *     account, and from that moment the two are unrelated — editing one never
 *     changes the other, and deleting one never touches the other.
 *
 * The UI says that out loud in both dialogs rather than leaving people to
 * discover it, because the wrong guess ("they're editing MY agent") is the one
 * that stops someone from sharing at all.
 */
import { useEffect, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import {
  AlertTriangle, ArrowRight, Check, Copy, CopyPlus, Download, FileText,
  GitBranch, Languages, Link2, PhoneIncoming, PhoneOutgoing, RefreshCw, Share2,
  Trash2, Users,
} from 'lucide-react'
import { Badge, Button, Spinner, cx } from './ui'
import { Modal } from './Modal'
import { useToast } from './Toast'
import { useConfirm } from './confirm'
import { useRefresh } from '../api/hooks/useApi'
import { fmtDate, parseTs } from '../lib/format'
import {
  createShareLink, getShareLink, importShare, previewShare, revokeShareLink,
  type ImportResult, type ShareCopy, type ShareLink, type SharePreview,
} from '../api/share'

const errText = (e: any) => String(e?.reason || e?.message || e || 'Something went wrong')

function DirectionBadge({ direction }: { direction: string }) {
  const outbound = direction === 'outbound'
  return (
    <Badge tone={outbound ? 'indigo' : 'mint'}>
      {outbound ? <PhoneOutgoing size={11} /> : <PhoneIncoming size={11} />}
      {outbound ? 'outbound' : 'inbound'}
    </Badge>
  )
}

function formatExpiry(iso: string | null): string {
  const at = parseTs(iso)
  if (!at) return 'never expires'
  const days = Math.round((at.getTime() - Date.now()) / 86_400_000)
  if (days < 0) return 'expired'
  if (days === 0) return 'expires today'
  return `expires in ${days} day${days === 1 ? '' : 's'}`
}

/* ═════════════════════════════════════ sharer ═════════════════════════════ */

export function ShareAgentModal({
  slug, title, open, onClose,
}: {
  slug: string
  title?: string
  open: boolean
  onClose: () => void
}) {
  const toast = useToast()
  const confirm = useConfirm()
  const [link, setLink] = useState<ShareLink | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [copied, setCopied] = useState(false)
  const [error, setError] = useState('')

  // Load the EXISTING link first rather than minting on open: opening the
  // dialog to check whether you had already shared something must not create
  // a link (and a second one to revoke) as a side effect.
  useEffect(() => {
    if (!open || !slug) return
    let live = true
    setLoading(true); setError(''); setCopied(false)
    getShareLink(slug)
      .then((r) => { if (live) setLink(r.link) })
      .catch((e) => { if (live) setError(errText(e)) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [open, slug])

  async function create() {
    setBusy(true); setError('')
    try {
      const r = await createShareLink(slug)
      setLink(r.link)
      await copy(r.link.url)
    } catch (e: any) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url)
      setCopied(true)
      toast('Share link copied', 'success')
      setTimeout(() => setCopied(false), 2200)
    } catch {
      // Clipboard is blocked outside a secure context or without permission —
      // the input below is selectable, so say that instead of failing silently.
      toast('Could not copy automatically — select the link and copy it', 'info')
    }
  }

  async function revoke() {
    if (!link) return
    const ok = await confirm({
      title: 'Revoke this share link?',
      description: link.import_count > 0
        ? `The link stops working immediately. The ${link.import_count} ${link.import_count === 1 ? 'person who has' : 'people who have'} already imported this agent keep their own copy — those copies are independent and cannot be taken back.`
        : 'The link stops working immediately. Nobody has imported this agent yet.',
      confirmLabel: 'Revoke link',
      destructive: true,
    })
    if (!ok) return
    setBusy(true)
    try {
      await revokeShareLink(link.token)
      setLink(null)
      toast('Share link revoked', 'success')
    } catch (e: any) {
      setError(errText(e))
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null

  return (
    <Modal open onClose={onClose} title={`Share “${title || slug}”`} wide>
      <p className="text-sm text-soft">
        Anyone with this link can import a <span className="font-semibold">copy</span> of this
        agent into their own Namaskall account. Their copy is a separate agent from
        the moment they import it — your edits never reach it, and theirs never
        reach yours.
      </p>

      <ul className="mt-3 space-y-1.5 text-xs text-dim">
        <li>· Both the inbound and outbound versions travel together, and count as one agent against their plan.</li>
        <li>· Uploaded knowledge-base documents are copied too.</li>
        <li>· Your phone-number connections and your booked appointments are <span className="font-semibold">not</span> shared.</li>
      </ul>

      {error && (
        <div role="alert" className="mt-4 flex items-start gap-2 rounded-xl bg-rose-500/10 px-3.5 py-3 text-xs text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      <div className="mt-4">
        {loading ? (
          <div className="flex items-center gap-2 text-sm text-dim">
            <Spinner className="h-4 w-4" /> Checking for an existing link…
          </div>
        ) : link ? (
          <div className="space-y-3">
            <div className="flex flex-wrap items-center gap-2">
              <input
                readOnly
                value={link.url}
                aria-label="Share link"
                onFocus={(e) => e.currentTarget.select()}
                className="min-w-0 flex-1 rounded-md bg-[var(--surface-2)] px-3 py-2 font-mono text-xs text-soft ring-1 ring-[var(--border)] focus:outline-none focus:ring-2 focus:ring-[var(--accent)]"
              />
              <Button variant="primary" size="sm" onClick={() => copy(link.url)}>
                {copied ? <Check size={14} /> : <Copy size={14} />}
                {copied ? 'Copied' : 'Copy'}
              </Button>
            </div>
            <div className="flex flex-wrap items-center gap-2 text-xs text-dim">
              <Badge tone="neutral"><Users size={11} /> {link.import_count} imported</Badge>
              <Badge tone={link.expires_at ? 'amber' : 'neutral'}>
                <Link2 size={11} /> {formatExpiry(link.expires_at)}
              </Badge>
              <Button variant="ghost" size="sm" className="ml-auto" disabled={busy}
                      onClick={() => { void revoke() }}>
                {busy ? <Spinner className="h-3 w-3" /> : <Trash2 size={13} />} Revoke
              </Button>
            </div>
          </div>
        ) : (
          <Button variant="primary" disabled={busy} onClick={() => { void create() }}>
            {busy ? <Spinner className="h-3.5 w-3.5" /> : <Share2 size={15} />}
            Create share link
          </Button>
        )}
      </div>
    </Modal>
  )
}

/* ════════════════════════════════════ recipient ═══════════════════════════ */

/** What the primary button will do. `replace:<family>` names the copy, because
    an account can hold several copies of one link. */
type Choice = 'new' | `replace:${string}`

/** A selectable option that is a real radio underneath, so arrow keys, focus
    and screen readers work without any of it being re-implemented. */
function ChoiceCard({
  checked, disabled, onSelect, icon, title, children,
}: {
  checked: boolean
  disabled?: boolean
  onSelect: () => void
  icon: ReactNode
  title: ReactNode
  children: ReactNode
}) {
  return (
    <label
      className={cx(
        'flex items-start gap-3 rounded-2xl px-3.5 py-3 ring-1 transition-colors',
        // The radio itself is sr-only, so its focus outline is a 1px dot.
        // Keyboard focus has to be drawn on the card.
        'has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-offset-2 has-[:focus-visible]:outline-[var(--accent-2)]',
        disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
        checked
          // NOT bg-[var(--accent)]/10: --accent is a hex, Tailwind v3 cannot
          // alpha it, and the class compiles to nothing at all.
          ? 'bg-mint-400/10 ring-2 ring-[var(--accent)]'
          : 'ring-[var(--border)] hover:bg-[var(--surface-2)]',
      )}
    >
      <input
        type="radio"
        name="import-choice"
        className="sr-only"
        checked={checked}
        disabled={disabled}
        onChange={onSelect}
      />
      <span className={cx('mt-0.5 shrink-0', checked ? 'text-[var(--accent)]' : 'text-dim')}>
        {icon}
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-semibold text-soft">{title}</span>
        <span className="mt-0.5 block text-xs text-dim">{children}</span>
      </span>
    </label>
  )
}

/** Directions the shared agent has that this copy no longer does — they are
    created from the shared version on replace, and the dialog has to say so
    BEFORE the click, not in the toast after it. */
function missingDirections(preview: SharePreview, copy: ShareCopy): string[] {
  return preview.agent.variants
    .map((v) => v.direction)
    .filter((d) => !copy.slugs.some((s) => s.endsWith(`-${d}`)))
}

/**
 * The popup a share link lands on. Mounted by the Agents page whenever the URL
 * carries `?import=<token>`; it clears that parameter on close so a refresh
 * does not reopen a dialog the user already dismissed.
 *
 * Opening a link you have ALREADY imported is the normal case, not an error:
 * agents are shared by people who are still improving them. So it offers a
 * choice — replace the copy you hold, or take another — rather than pointing
 * at the old version and stopping.
 */
export function ImportAgentModal({
  token, open, onClose,
}: {
  token: string
  open: boolean
  onClose: () => void
}) {
  const nav = useNavigate()
  const toast = useToast()
  const confirm = useConfirm()
  const refresh = useRefresh()
  const [preview, setPreview] = useState<SharePreview | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [done, setDone] = useState<ImportResult | null>(null)
  const [choice, setChoice] = useState<Choice>('new')

  const adopt = (p: SharePreview) => {
    setPreview(p)
    // Replace is the default when there is something to replace: it is why
    // someone re-opens a link, and it is the option that cannot fill the
    // account with near-identical agents.
    setChoice(p.copies.length ? `replace:${p.copies[p.copies.length - 1].family}` : 'new')
  }

  useEffect(() => {
    if (!open || !token) return
    let live = true
    setLoading(true); setError(''); setPreview(null); setDone(null)
    previewShare(token)
      .then((p) => { if (live) adopt(p) })
      .catch((e) => { if (live) setError(errText(e)) })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [open, token])

  const a = preview?.agent
  const copies = preview?.copies ?? []
  const hasCopy = copies.length > 0
  const atCap = preview?.code === 'cap_agents'
  const target = choice.startsWith('replace:') ? choice.slice('replace:'.length) : ''
  const targetCopy = copies.find((c) => c.family === target)
  const sharer = preview?.shared_by.name || preview?.shared_by.email || 'the sharer'

  async function run() {
    if (!preview) return
    if (targetCopy) {
      const back = missingDirections(preview, targetCopy)
      const ok = await confirm({
        title: `Replace “${targetCopy.family}”?`,
        description:
          `Its script, stages, voice and shared knowledge are overwritten with the version from ${sharer}, including any edits you made. `
          + 'The version you have now can be restored from the agent’s History tab. '
          + 'Your phone numbers, bookings, call history and the documents you uploaded yourself are not touched.'
          + (back.length ? ` The ${back.join(' and ')} version is added back from the shared agent.` : ''),
        confirmLabel: 'Replace',
        destructive: true,
      })
      if (!ok) return
    }
    setBusy(true); setError('')
    try {
      const r = await importShare(
        token,
        targetCopy ? { mode: 'replace', target: targetCopy.family }
          : hasCopy ? { mode: 'new' } : undefined,
      )
      setDone(r)
      refresh(['agents'], ['billing', 'summary'])
      toast(r.status === 'replaced' ? `Replaced “${r.family}”` : `Imported as “${r.primary}”`, 'success')
    } catch (e: any) {
      setError(errText(e))
      // A refusal may have changed what the preview should say (a concurrent
      // import filled the last plan slot, the copy was archived in another
      // tab), so re-read it.
      previewShare(token).then(adopt).catch(() => {})
    } finally {
      setBusy(false)
    }
  }

  if (!open) return null

  // With a copy in hand the cap only disables ONE option, so the footer keeps
  // its action; with none, the cap is the whole story and the way out is plans.
  const blocked = !hasCopy && !preview?.can_import
  const replaced = done?.status === 'replaced'

  return (
    <Modal
      open
      onClose={onClose}
      title={done ? (replaced ? 'Agent replaced' : 'Agent imported')
        : hasCopy ? 'You already have this agent' : 'Import this agent?'}
      wide
      footer={
        done ? (
          <>
            <Button variant="ghost" onClick={onClose}>Stay here</Button>
            <Button variant="primary" onClick={() => { onClose(); nav(`/build/agents/${done.primary}`) }}>
              Open it <ArrowRight size={14} />
            </Button>
          </>
        ) : (
          <>
            <Button variant="ghost" onClick={onClose} disabled={busy}>Cancel</Button>
            {blocked && atCap ? (
              <Link to="/billing" onClick={onClose}>
                <Button variant="primary">See plans <ArrowRight size={14} /></Button>
              </Link>
            ) : (
              <Button variant="primary" disabled={busy || loading || !preview || blocked}
                      onClick={() => { void run() }}>
                {busy ? <Spinner className="h-3.5 w-3.5" />
                  : targetCopy ? <RefreshCw size={15} /> : <Download size={15} />}
                {targetCopy ? 'Replace my copy' : hasCopy ? 'Import as a new agent' : 'Import to my account'}
              </Button>
            )}
          </>
        )
      }
    >
      {loading ? (
        <div className="flex items-center gap-2 py-6 text-sm text-dim">
          <Spinner className="h-4 w-4" /> Loading the shared agent…
        </div>
      ) : done ? (
        <div className="space-y-3">
          {replaced ? (
            <>
              <p className="text-sm text-soft">
                <code className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-2xs">{done.family}</code>{' '}
                now runs the version {sharer} shared. It takes effect on the next call.
                {!!done.added?.length && ` Added from the shared agent: ${done.added.join(', ')}.`}
              </p>
              <p className="text-xs text-dim">
                It is still entirely yours, and still a one-off copy — later
                changes on their side will not reach it unless you replace it
                again. The version you had is in the agent’s History tab.
                {done.documents?.failed && ' Its documents could not be updated and were left as they were.'}
              </p>
            </>
          ) : (
            <>
              <p className="text-sm text-soft">
                “{a?.agent_name || done.family}” is now in your account as{' '}
                <code className="rounded bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-2xs">{done.family}</code>.
                It is entirely yours — edit it freely; the person who shared it will
                never see your changes.
              </p>
              <p className="text-xs text-dim">
                It is not connected to any phone number yet. Open it, review the
                opening line, then Activate it on one of your numbers.
              </p>
            </>
          )}
        </div>
      ) : !a ? (
        <div role="alert" className="flex items-start gap-2 rounded-xl bg-rose-500/10 px-3.5 py-3 text-sm text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" />
          <span>{error || 'This share link could not be loaded.'}</span>
        </div>
      ) : (
        <div className="space-y-4">
          <div>
            <div className="font-display text-xl font-bold tracking-tight">
              {a.agent_name || 'Untitled agent'}
            </div>
            <div className="text-sm text-dim">{a.company || '—'}</div>
            <div className="mt-1 text-xs text-dim">
              Shared by {preview?.shared_by.name
                ? `${preview.shared_by.name} (${preview.shared_by.email})`
                : preview?.shared_by.email}
              {preview?.source_updated_at && ` · last changed ${fmtDate(preview.source_updated_at)}`}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-1.5">
            {a.variants.map((v) => <DirectionBadge key={v.slug} direction={v.direction} />)}
            {a.languages.map((l) => (
              <Badge key={l} tone="neutral"><Languages size={11} /> {l}</Badge>
            ))}
            <span title={a.stages.join(' → ')} className="inline-flex">
              <Badge tone="neutral"><GitBranch size={11} /> {a.stages.length} stages</Badge>
            </span>
            {a.documents > 0 && (
              <Badge tone="neutral">
                <FileText size={11} /> {a.documents} document{a.documents === 1 ? '' : 's'}
              </Badge>
            )}
          </div>

          {/* A failed attempt keeps the choice on screen — the error explains
              what happened, the options are how to try again. */}
          {error && (
            <div role="alert" className="flex items-start gap-2 rounded-xl bg-rose-500/10 px-3.5 py-3 text-sm text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
              <AlertTriangle size={15} className="mt-0.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          {hasCopy ? (
            <div role="radiogroup" aria-label="What to do with this shared agent" className="space-y-2">
              {copies.map((c) => {
                const back = preview ? missingDirections(preview, c) : []
                return (
                  <ChoiceCard
                    key={c.family}
                    checked={choice === `replace:${c.family}`}
                    onSelect={() => setChoice(`replace:${c.family}`)}
                    icon={<RefreshCw size={16} />}
                    title={<>Replace “{c.family}” with this version</>}
                  >
                    Your copy from {fmtDate(c.replaced_at || c.imported_at)} keeps its
                    name, phone numbers, bookings and call history; its script,
                    voice and shared knowledge are overwritten.
                    {back.length > 0 && ` The ${back.join(' and ')} version comes back from the shared agent.`}
                    {' '}
                    <Link to={`/build/agents/${c.primary}`} onClick={onClose}
                          className="font-semibold underline">
                      Open it
                    </Link>
                  </ChoiceCard>
                )
              })}
              <ChoiceCard
                checked={choice === 'new'}
                disabled={!preview?.can_import}
                onSelect={() => setChoice('new')}
                icon={<CopyPlus size={16} />}
                title="Import as a new agent"
              >
                {preview?.can_import ? (
                  <>A second, separate copy under a new name. {copies.length === 1 ? `“${copies[0].family}” is` : 'Your existing copies are'} left exactly as {copies.length === 1 ? 'it is' : 'they are'}.</>
                ) : (
                  <>
                    {preview?.reason}
                    {atCap && preview?.billing?.agents_limit != null && (
                      <> Using {preview.billing.agents_used}/{preview.billing.agents_limit} agents —{' '}
                        <Link to="/billing" onClick={onClose} className="font-semibold underline">see plans</Link>.
                      </>
                    )}
                  </>
                )}
              </ChoiceCard>
            </div>
          ) : (
            <div className="rounded-xl bg-indigo-500/10 px-3.5 py-3 text-xs text-soft ring-1 ring-indigo-500/20">
              You get your own copy under a new name. It is independent from the
              original: your changes stay yours, and nothing you do here is
              visible to the person who shared it.
              {a.variants.length > 1 && ' Both directions arrive together and count as one agent on your plan.'}
            </div>
          )}

          {!hasCopy && !preview?.can_import && preview?.reason && (
            <div
              role="alert"
              className={cx(
                'flex items-start gap-2 rounded-xl px-3.5 py-3 text-sm ring-1',
                atCap
                  ? 'bg-amber-500/10 text-amber-700 ring-amber-500/20 dark:text-amber-300'
                  : 'bg-rose-500/10 text-rose-600 ring-rose-500/20 dark:text-rose-300',
              )}
            >
              <AlertTriangle size={15} className="mt-0.5 shrink-0" />
              <div>
                <div>{preview.reason}</div>
                {atCap && preview.billing?.agents_limit != null && (
                  <div className="mt-1 text-xs opacity-90">
                    Using {preview.billing.agents_used}/{preview.billing.agents_limit} agents.
                    Archive one to free a slot, or upgrade your plan.
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
