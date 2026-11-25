/**
 * Follow-up call review — the human-in-the-loop gate before anyone is dialled.
 *
 * Why this screen exists, in one paragraph: on 2026-07-29 the platform rang a
 * real customer two days after a 44-second call on which nobody had promised
 * anything. The AI had recorded that call as `promised_by: 'agent_promised'`
 * with 0.9 confidence, and the dialer trusted it. Of the twenty rows armed
 * that morning, fifteen had no recorded promise at all, and several were calls
 * that had failed on OUR side — logged as agent promises with confidence 1.0.
 *
 * So this dialog is built on one rule: never restate the AI's conclusion
 * without showing the human what was actually said. Every row is labelled
 * either "recorded promise" (a verbatim quote from the call exists) or
 * "AI-inferred" (nothing was promised — the model proposed the call itself),
 * and the inferred ones are visually the loud case, because those are the ones
 * that ring strangers.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, Check, ChevronDown, ChevronRight, ExternalLink, Info,
  PhoneCall, PhoneOff, Quote, ShieldCheck,
} from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import type { FollowupReviewItem } from '../api/intel'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { post } from '../api/mutate'
import { Badge, Button, cx, IconButton, Spinner } from './ui'
import { AsyncState, ContactLabel, StatusBadge } from './kit'
import { FullscreenModal } from './Modal'
import { useToast } from './Toast'
import { useConfirm } from './confirm'
import { fmtDateTime } from '../lib/format'

type Props = { open: boolean; onClose: () => void }

const fmtWhen = (ts?: string | null) => (ts ? fmtDateTime(ts) : 'no time set')

/** "Agent said" / "Customer said" — whose words created the obligation. */
const SPEAKER: Record<string, string> = {
  agent: 'The agent said', customer: 'The customer said',
}

/** How the follow-up came to exist, in the operator's language rather than
 *  the pipeline's enum. `agent_promised` is stated as a claim, not a fact,
 *  because on inferred rows it demonstrably is one. */
const PROMISED_BY: Record<string, string> = {
  customer_requested: 'Customer asked us to call back',
  agent_promised: 'Recorded as an agent promise',
  internal: 'Internal decision — nobody asked for this call',
  external_dependency: 'Waiting on something outside the call',
}

function EvidenceBlock({ item }: { item: FollowupReviewItem }) {
  const [openTail, setOpenTail] = useState(false)
  const ev = item.evidence
  const quoted = ev.strength === 'quoted'

  return (
    <div className="mt-3 space-y-2">
      {quoted ? (
        <>
          <div className="flex items-center gap-1.5 text-xs font-medium text-emerald-700 dark:text-emerald-300">
            <ShieldCheck size={13} aria-hidden />
            Recorded promise on the call
          </div>
          <ul className="space-y-2">
            {ev.commitments.map((c, i) => (
              <li key={i}
                className="rounded-md border border-[var(--ui-border)] bg-[var(--surface-2)] p-2.5">
                <div className="text-2xs uppercase tracking-wide text-dim">
                  {SPEAKER[c.made_by] ?? c.made_by}
                  {c.deadline_text ? ` · ${c.deadline_text}` : ''}
                </div>
                {c.quote && (
                  <blockquote className="mt-1 flex gap-1.5 text-sm text-[var(--text)]">
                    <Quote size={12} className="mt-1 shrink-0 text-dim" aria-hidden />
                    <span className="italic">“{c.quote}”</span>
                  </blockquote>
                )}
                <div className="mt-1 text-xs text-dim">{c.description}</div>
              </li>
            ))}
          </ul>
        </>
      ) : (
        /* The important case. Nothing was promised — say so plainly, because
           the row's own `promised_by` field may still claim otherwise. */
        <div className="rounded-md border border-amber-300/60 bg-amber-50 p-2.5 text-xs
                        dark:border-amber-400/30 dark:bg-amber-400/10">
          <div className="flex items-center gap-1.5 font-medium text-amber-800 dark:text-amber-200">
            <AlertTriangle size={13} aria-hidden />
            No promise was recorded on this call
          </div>
          <p className="mt-1 text-amber-900/80 dark:text-amber-100/70">
            Nothing in the transcript was extracted as a commitment. The AI
            proposed this call on its own reasoning
            {item.promised_by === 'agent_promised'
              ? ' — despite labelling it an agent promise' : ''}.
            Read the call before approving.
          </p>
        </div>
      )}

      {ev.transcript_tail.length > 0 && (
        <div>
          <button type="button" onClick={() => setOpenTail((v) => !v)}
            className="flex items-center gap-1 text-2xs text-dim transition-colors hover:text-[var(--text)]">
            {openTail ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            {openTail ? 'Hide' : 'Show'} how the call ended
            ({ev.transcript_tail.length} turns)
          </button>
          {openTail && (
            <div data-lenis-prevent
              className="mt-1.5 max-h-56 space-y-1.5 overflow-y-auto rounded-md
                         border border-[var(--ui-border)] bg-[var(--surface-2)] p-2.5">
              {ev.transcript_tail.map((t, i) => (
                <div key={i} className="text-xs">
                  <span className="text-2xs tabular-nums text-dim">{t.at}</span>{' '}
                  <span className="font-medium text-[var(--text)]">{t.speaker}:</span>{' '}
                  <span className="text-dim">{t.text}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export function FollowupReview({ open, onClose }: Props) {
  const nav = useNavigate()
  const toast = useToast()
  const refresh = useRefresh()
  const confirm = useConfirm()
  const [picked, setPicked] = useState<Set<number>>(new Set())
  const [busy, setBusy] = useState(false)
  const [rowBusy, setRowBusy] = useState<number | null>(null)

  // Declined rows are hidden by default but must stay reachable: this dialog
  // is the only screen that can approve, so without the toggle a decline is a
  // one-way door and changing your mind is impossible.
  const [showDeclined, setShowDeclined] = useState(false)

  const q = useGet<{
    items: FollowupReviewItem[]; total: number; shown: number
    approval_ttl_hours: number; max_attempts_per_approval: number
  }>(
    ['followups', 'review-queue', showDeclined],
    `/intel/followups/review-queue${showDeclined ? '?include_declined=1' : ''}`,
    { enabled: open })

  const items = useMemo(() => q.data?.items ?? [], [q.data])
  const ttl = q.data?.approval_ttl_hours ?? 72
  const maxAttempts = q.data?.max_attempts_per_approval ?? 3

  // Selection is reset whenever the dialog opens, and pruned when the queue
  // changes underneath it — approving a row must never leave its id behind to
  // be re-submitted by a later "approve selected".
  useEffect(() => { if (open) setPicked(new Set()) }, [open])
  useEffect(() => {
    setPicked((prev) => {
      const live = new Set(items.map((i) => i.id))
      const next = new Set([...prev].filter((id) => live.has(id)))
      return next.size === prev.size ? prev : next
    })
  }, [items])

  // Select-all deliberately skips rows already declined: sweeping them back in
  // would silently undo a decision someone made on purpose.
  const callable = items.filter((i) => !i.dnd && i.approval_state !== 'declined')
  const toggle = (id: number) => setPicked((p) => {
    const n = new Set(p)
    if (n.has(id)) n.delete(id); else n.add(id)
    return n
  })

  async function approve(ids: number[]) {
    if (!ids.length) return
    const names = ids.length === 1
      ? (items.find((i) => i.id === ids[0])?.contact_name
         || items.find((i) => i.id === ids[0])?.dial_phone || 'this contact')
      : `${ids.length} people`
    const ok = await confirm({
      title: ids.length === 1 ? `Approve calling ${names}?` : `Approve ${names}?`,
      description:
        `The platform may place ${ids.length === 1 ? 'this call' : 'these calls'} `
        + `automatically at the scheduled time, within calling hours `
        + `(10:00–19:00 IST, Mon–Sat), retrying up to ${maxAttempts} times if `
        + `nobody answers. The approval expires after ${ttl} hours and is `
        + `recorded against your account.`,
      confirmLabel: ids.length === 1 ? 'Approve this call' : `Approve ${ids.length}`,
    })
    if (!ok) return
    ids.length === 1 ? setRowBusy(ids[0]) : setBusy(true)
    try {
      const res = ids.length === 1
        ? await post(`/intel/followups/${ids[0]}/approve`, { mode: 'auto' })
        : await post('/intel/followups/approve', { ids, mode: 'auto' })
      const skipped = (res as any)?.skipped ?? 0
      toast(skipped
        ? `Approved ${(res as any).count}. Skipped ${skipped} (contact asked not to be called).`
        : ids.length === 1 ? 'Approved — this call may now be placed'
          : `Approved ${(res as any).count} calls`, 'success')
      refresh(['followups'])
    } catch (e: any) {
      toast(`Could not approve: ${e?.message ?? e}`, 'error')
    } finally { setBusy(false); setRowBusy(null) }
  }

  async function decline(item: FollowupReviewItem) {
    const ok = await confirm({
      title: `Don't call ${item.contact_name || item.dial_phone || 'this contact'}?`,
      description:
        'The follow-up stays on your list and you can still call by hand — it '
        + 'just loses permission to dial itself.',
      confirmLabel: "Don't call",
      cancelLabel: 'Back',
      destructive: true,
    })
    if (!ok) return
    setRowBusy(item.id)
    try {
      await post(`/intel/followups/${item.id}/decline`, {})
      toast('Marked do-not-auto-call', 'success')
      refresh(['followups'])
    } catch (e: any) {
      toast(`Failed: ${e?.message ?? e}`, 'error')
    } finally { setRowBusy(null) }
  }

  const pending = items.filter((i) => i.approval_state === 'pending').length
  const inferred = items.filter((i) => i.approval_state === 'pending'
    && i.evidence.strength === 'inferred').length

  return (
    <FullscreenModal open={open} onClose={onClose} scope="shell"
      title="Review follow-up calls"
      headerExtra={
        <div className="flex items-center gap-3">
          {pending > 0 && (
            <span className="text-xs text-dim">
              {pending} awaiting your decision
              {inferred > 0 ? ` · ${inferred} with no recorded promise` : ''}
            </span>
          )}
          <label className="flex items-center gap-1.5 text-xs text-dim">
            <input type="checkbox" className="h-3.5 w-3.5 accent-[var(--accent)]"
              checked={showDeclined}
              onChange={(e) => setShowDeclined(e.target.checked)} />
            Show declined
          </label>
        </div>
      }
      footer={
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label className="flex items-center gap-2 text-xs text-dim">
            <input type="checkbox"
              className="h-3.5 w-3.5 accent-[var(--accent)]"
              checked={callable.length > 0 && picked.size === callable.length}
              onChange={(e) => setPicked(e.target.checked
                ? new Set(callable.map((i) => i.id)) : new Set())} />
            Select all {callable.length > 0 ? `(${callable.length})` : ''}
          </label>
          <div className="flex items-center gap-2">
            <Button variant="ghost" onClick={onClose}>Close</Button>
            <Button disabled={!picked.size || busy}
              onClick={() => { void approve([...picked]) }}>
              {busy ? <Spinner /> : <Check size={14} />}
              Approve {picked.size || ''} for calling
            </Button>
          </div>
        </div>
      }>
      <div data-lenis-prevent className="flex-1 overflow-y-auto px-4 py-4 sm:px-6">
        <p className="mb-4 flex items-start gap-1.5 text-xs text-dim">
          <Info size={13} className="mt-0.5 shrink-0" aria-hidden />
          Nothing here has been called, and nothing can be called until you
          approve it. Approvals last {ttl} hours and are recorded against your
          account.
        </p>
        <AsyncState loading={q.isLoading} error={q.error} empty={items.length === 0}
          variant="cards" rows={3}
          emptyLabel="No follow-up calls are waiting for approval. New ones appear here after a call is analysed — the platform never dials before you say so.">
          <ul className="space-y-3">
            {items.map((item) => {
              const chosen = picked.has(item.id)
              return (
                <li key={item.id}
                  className={cx('rounded-2xl border p-4 transition-colors',
                    chosen
                      ? 'border-[var(--accent)] bg-[var(--accent)]/[0.04]'
                      : 'border-[var(--ui-border)] bg-[var(--surface)]')}>
                  <div className="flex items-start gap-3">
                    <input type="checkbox" aria-label={`Select ${item.contact_name || item.dial_phone || 'contact'}`}
                      className="mt-1 h-4 w-4 shrink-0 accent-[var(--accent)]"
                      disabled={!!item.dnd} checked={chosen}
                      onChange={() => toggle(item.id)} />
                    <div className="min-w-0 flex-1">
                      <div className="flex flex-wrap items-center gap-2">
                        {item.contact_name || item.dial_phone
                          ? <ContactLabel name={item.contact_name} phone={item.dial_phone} />
                          : <span className="font-medium text-dim">Unknown contact</span>}
                        {item.dnd && (
                          <Badge tone="rose" soft>asked not to be called</Badge>
                        )}
                        {item.approval_state === 'declined' && (
                          <Badge tone="neutral" soft>you declined this</Badge>
                        )}
                        {item.evidence.strength === 'inferred'
                          ? <Badge tone="amber" soft>AI-inferred</Badge>
                          : <Badge tone="mint" soft>promise on record</Badge>}
                        <StatusBadge value={item.priority} />
                      </div>

                      <div className="mt-1 text-xs text-dim">
                        Would call {fmtWhen(item.scheduled_at)}
                        {item.auto_scheduled ? ' (time picked by AI)' : ' (time the customer gave)'}
                        {item.followup_type ? ` · ${item.followup_type.replace(/_/g, ' ')}` : ''}
                        {item.promised_by
                          ? ` · ${PROMISED_BY[item.promised_by] ?? item.promised_by}` : ''}
                      </div>

                      <p className="mt-2 text-sm text-[var(--text)]">
                        {item.reason || item.explanation || 'No reason recorded.'}
                      </p>

                      <EvidenceBlock item={item} />

                      <div className="mt-3 flex flex-wrap items-center gap-2">
                        <Button size="sm" disabled={!!item.dnd || rowBusy === item.id}
                          onClick={() => { void approve([item.id]) }}>
                          {rowBusy === item.id ? <Spinner /> : <Check size={13} />}
                          {item.approval_state === 'declined' ? 'Approve anyway' : 'Approve'}
                        </Button>
                        {item.approval_state !== 'declined' && (
                          <Button size="sm" variant="ghost" disabled={rowBusy === item.id}
                            onClick={() => { void decline(item) }}>
                            <PhoneOff size={13} /> Don't call
                          </Button>
                        )}
                        {item.call_key && (
                          <IconButton
                            label={`Open the call this follow-up came from`}
                            onClick={() => { onClose(); nav(`/intelligence/${item.call_key}`) }}>
                            <ExternalLink size={14} />
                          </IconButton>
                        )}
                      </div>
                    </div>
                  </div>
                </li>
              )
            })}
          </ul>
        </AsyncState>
      </div>
    </FullscreenModal>
  )
}

/** Header button + count badge that opens the review dialog. */
export function ReviewCallsButton({ onClick, count }: { onClick: () => void; count: number }) {
  return (
    <Button onClick={onClick} variant={count > 0 ? 'primary' : 'ghost'}>
      <PhoneCall size={14} />
      Review &amp; approve calls{count > 0 ? ` (${count})` : ''}
    </Button>
  )
}
