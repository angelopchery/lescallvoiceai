/**
 * Follow-ups — the management dashboard for the decision engine's output.
 * Buckets: Today / Upcoming / Overdue / Needs review / Done / Archived (no).
 * Every row: complete, reschedule, cancel, reassign, notes — with audit trail.
 */
import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Check, ExternalLink, Info, Pencil, PhoneCall, Plus, RefreshCw, ShieldAlert,
  X, Zap,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { patch, post } from '../api/mutate'
import type { Followup } from '../api/intel'
import { Badge, Button, Card, cx, IconButton, Spinner } from '../components/ui'
import {
  AsyncState, ContactLabel, Field, Input, PageHeader, Pagination, Segmented, Select,
  StatusBadge, Table, TD, TH, THead, TR, Textarea,
} from '../components/kit'
import { Modal } from '../components/Modal'
import { FollowupReview, ReviewCallsButton } from '../components/FollowupReview'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/confirm'
import { useUrlNumber, useUrlParam } from '../lib/hooks'
import { fmtDateTime, parseTs } from '../lib/format'

const LIMIT = 50
type Bucket = 'today' | 'upcoming' | 'overdue' | 'review' | 'done' | 'archived'

/* One empty state per bucket — "Nothing in this bucket" told the user nothing
   about whether that was good news (no overdue) or a gap (nothing scheduled). */
const EMPTY_LABEL: Record<Bucket, string> = {
  today: 'Nothing due today. Follow-ups appear here automatically when the AI detects a promise or pending action on a call.',
  upcoming: 'No follow-ups scheduled ahead. Anything the AI schedules for a future date lands here.',
  overdue: 'Nothing overdue — every scheduled follow-up is still in its window.',
  review: 'Nothing needs review. Low-confidence AI decisions surface here for a human call.',
  done: 'No completed follow-ups yet.',
  archived: 'No archived no-follow-up decisions yet — these are calls the AI judged needed no chase.',
}

/* Which bucket to open on when the user hasn't picked one — most urgent first.
   `archived` and `done` are never auto-selected: they are the biggest buckets
   by row count but contain nothing to act on. */
const PRIORITY_ORDER: Bucket[] = ['today', 'overdue', 'review', 'upcoming']

/** Instant -> the "YYYY-MM-DDTHH:mm" a datetime-local input wants.
 *
 *  Rendered in IST on purpose: the save paths below write the picked wall clock
 *  back with an explicit +05:30, so the picker has to show the same zone or the
 *  round-trip is lossy. It used to slice the stored UTC clock straight into the
 *  field, which meant re-saving an untouched follow-up moved it 5h30m earlier. */
function toLocalInput(ts?: string | null) {
  const d = parseTs(ts)
  if (!d) return ''
  const p: Record<string, string> = {}
  for (const part of new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Kolkata', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
  }).formatToParts(d)) p[part.type] = part.value
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`
}

/** Who a row is about — icon buttons need a name, not four identical "Edit"s. */
const who = (f: Followup) => f.contact_name || f.phone || 'this contact'

export default function FollowUps() {
  const nav = useNavigate()
  const toast = useToast()
  const refresh = useRefresh()
  const confirm = useConfirm()
  // Bucket + page live in the URL so an "overdue" list can be linked and
  // shared. A hand-edited/stale ?bucket= falls back rather than 404-ing.
  const [bucketParam, setBucket] = useUrlParam('bucket', '')
  const bucket: Bucket = bucketParam in EMPTY_LABEL ? (bucketParam as Bucket) : 'today'
  const [page, setPage] = useUrlNumber('page', 1)

  const q = useGet<{ items: Followup[]; total: number; counts: Record<string, number> }>(
    ['followups', bucket, page],
    `/intel/followups?bucket=${bucket}&page=${page}&limit=${LIMIT}`,
    { refetchInterval: 60_000 })

  // Count for the header badge. `total` counts only rows still awaiting a
  // decision, so it stays truthful even when the dialog is showing declined
  // rows too (which it fetches under its own key).
  const [reviewing, setReviewing] = useState(false)
  const reviewQ = useGet<{ items: unknown[]; total: number }>(
    ['followups', 'review-queue'], '/intel/followups/review-queue',
    { refetchInterval: 60_000 })
  const awaiting = reviewQ.data?.total ?? 0

  // Land on a bucket that actually has work. "Today" is empty on most days —
  // opening there showed an empty table with no rows and therefore no action
  // buttons, which reads as "the page is broken" rather than "nothing is due".
  // Only applies when the user hasn't chosen a bucket themselves (no ?bucket=),
  // and only once, so it never fights a deliberate click.
  const autoPicked = useRef(false)
  useEffect(() => {
    if (autoPicked.current || bucketParam) return
    const counts = q.data?.counts
    if (!counts) return
    autoPicked.current = true
    const firstWithWork = PRIORITY_ORDER.find((b) => (counts[b] ?? 0) > 0)
    if (firstWithWork && firstWithWork !== bucket) setBucket(firstWithWork)
  }, [q.data, bucketParam, bucket, setBucket])

  const [editing, setEditing] = useState<Followup | null>(null)
  const [editWhen, setEditWhen] = useState('')
  const [editPriority, setEditPriority] = useState('medium')
  const [editAssignee, setEditAssignee] = useState('')
  const [editNote, setEditNote] = useState('')
  const [editMode, setEditMode] = useState<'manual' | 'auto'>('manual')
  const [busy, setBusy] = useState(false)
  const [callingId, setCallingId] = useState<number | null>(null)

  const [creating, setCreating] = useState(false)
  const [newReason, setNewReason] = useState('')
  const [newWhen, setNewWhen] = useState('')

  function openEdit(f: Followup) {
    setEditing(f)
    setEditWhen(toLocalInput(f.scheduled_at))
    setEditPriority(f.priority)
    setEditAssignee(f.assigned_to ?? '')
    setEditMode(f.mode === 'auto' ? 'auto' : 'manual')
    setEditNote('')
  }

  async function act(f: Followup, body: Record<string, unknown>, msg: string) {
    try {
      await patch(`/intel/followups/${f.id}`, body)
      toast(msg, 'success')
      refresh(['followups'])
    } catch (e: any) {
      toast(`Failed: ${e?.message ?? e}`, 'error')
    }
  }

  /**
   * Place the follow-up call now.
   *
   * Nothing in the platform dials a follow-up on its own — the pipeline decides
   * one is needed and schedules a time, and the sweeper marks it `missed` an
   * hour later. This is the manual close of that loop, and it deliberately
   * reuses POST /outbound/dial/{handle} rather than re-implementing dialling:
   * that path owns the duplicate-dial guard, the caller-ID/trunk identity, and
   * the post-call contact update.
   */
  async function callNow(f: Followup) {
    if (!f.contact_handle) {
      toast('This follow-up has no linked contact to call', 'error')
      return
    }
    const ok = await confirm({
      title: `Call ${who(f)} now?`,
      description: `This places a real outbound call to ${f.dial_phone ?? 'the contact'} using your registered number and its outbound agent.`,
      confirmLabel: 'Call now',
    })
    if (!ok) return
    setCallingId(f.id)
    try {
      // Record the authorisation before dialling, not after. Clicking this
      // button IS the human-in-the-loop step, so it should leave the same
      // "approved_by <person>, at <time>" trace on the row that the review
      // dialog does — otherwise the audit trail can answer "who allowed this
      // call?" for auto-dialled rows but not for hand-dialled ones. Non-fatal:
      // a stamping failure must not block a call the user just confirmed.
      try {
        await post(`/intel/followups/${f.id}/approve`, { mode: 'manual' })
      } catch { /* audit stamp is best-effort; the confirm above is the gate */ }
      await post(`/outbound/dial/${encodeURIComponent(f.contact_handle)}`)
      toast(`Calling ${who(f)} — it rings in 15-30s`, 'success')
      // Record the attempt on the follow-up. Not marked completed: the call may
      // not connect, and claiming success here would be a lie in the audit log.
      await act(f, { note: `Outbound call placed to ${f.dial_phone ?? 'contact'}` },
                'Attempt logged')
    } catch (e: any) {
      const msg = String(e?.message ?? e)
      toast(msg.includes('409')
        ? 'A call to this contact was placed moments ago and may still be connecting.'
        : `Could not place the call: ${msg}`, 'error')
    } finally {
      setCallingId(null)
    }
  }

  async function cancelFollowup(f: Followup) {
    const ok = await confirm({
      title: 'Cancel this follow-up?',
      description: `${who(f)} will drop off the follow-up list. The call and its AI reasoning are kept — only the scheduled action is cancelled.`,
      confirmLabel: 'Cancel follow-up',
      cancelLabel: 'Keep it',
      destructive: true,
    })
    if (ok) await act(f, { status: 'cancelled' }, 'Cancelled')
  }

  async function saveEdit() {
    if (!editing) return
    setBusy(true)
    try {
      await patch(`/intel/followups/${editing.id}`, {
        scheduled_at: editWhen ? editWhen.replace('T', ' ') + ':00+05:30' : undefined,
        priority: editPriority,
        assigned_to: editAssignee || undefined,
        status: editWhen ? 'scheduled' : undefined,
        mode: editMode !== (editing.mode === 'auto' ? 'auto' : 'manual')
          ? editMode : undefined,
        note: editNote || undefined,
      })
      toast('Follow-up updated', 'success')
      setEditing(null)
      refresh(['followups'])
    } catch (e: any) {
      toast(`Failed: ${e?.message ?? e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  async function createManual() {
    if (!newReason.trim()) return
    setBusy(true)
    try {
      await post('/intel/followups', {
        reason: newReason,
        scheduled_at: newWhen ? newWhen.replace('T', ' ') + ':00+05:30' : undefined,
      })
      toast('Follow-up created', 'success')
      setCreating(false); setNewReason(''); setNewWhen('')
      refresh(['followups'])
    } catch (e: any) {
      toast(`Failed: ${e?.message ?? e}`, 'error')
    } finally {
      setBusy(false)
    }
  }

  const counts = q.data?.counts ?? {}
  const items = q.data?.items ?? []
  const pages = Math.max(1, Math.ceil((q.data?.total ?? 0) / LIMIT))
  const label = (b: Bucket, name: string) =>
    counts[b] ? `${name} (${counts[b]})` : name

  // An empty bucket should say where the work went, not just that this one is
  // empty — otherwise a tenant with 40 overdue items sees a blank Today tab and
  // concludes the panel is broken.
  const BUCKET_NAMES: Record<Bucket, string> = {
    today: 'Today', upcoming: 'Upcoming', overdue: 'Overdue',
    review: 'Needs review', done: 'Done', archived: 'No follow-up',
  }
  const elsewhere = (PRIORITY_ORDER.filter((b) => b !== bucket && (counts[b] ?? 0) > 0)
    .map((b) => `${BUCKET_NAMES[b]} (${counts[b]})`))
  const emptyLabel = EMPTY_LABEL[bucket]
    + (elsewhere.length ? ` Waiting elsewhere: ${elsewhere.join(' · ')}.` : '')

  return (
    <div className="space-y-6">
      <PageHeader title="Follow-ups"
        sub="AI-decided after every call, with the reasoning attached. Deciding and scheduling are automatic — calling is not: every follow-up call needs your approval first, whether you place it or the platform does."
        actions={
          <>
            <ReviewCallsButton count={awaiting} onClick={() => setReviewing(true)} />
            <Button variant="ghost" onClick={() => setCreating(true)}>
              <Plus size={14} /> New follow-up
            </Button>
            <Button variant="ghost" onClick={() => refresh(['followups'])}>
              <RefreshCw size={14} /> Refresh
            </Button>
          </>
        } />

      {awaiting > 0 && (
        <Card className="flex flex-wrap items-center gap-3 border-amber-300/60 p-4
                         dark:border-amber-400/30">
          <ShieldAlert size={16} className="shrink-0 text-amber-600 dark:text-amber-300"
            aria-hidden />
          <p className="min-w-0 flex-1 text-sm text-[var(--text)]">
            {awaiting} follow-up {awaiting === 1 ? 'call is' : 'calls are'} waiting
            for your approval. Nothing is dialled until you review
            {awaiting === 1 ? ' it' : ' them'}.
          </p>
          <Button size="sm" onClick={() => setReviewing(true)}>Review now</Button>
        </Card>
      )}

      <Card className="p-5">
        <Segmented<Bucket>
          options={[
            { value: 'today', label: label('today', 'Today') },
            { value: 'upcoming', label: label('upcoming', 'Upcoming') },
            { value: 'overdue', label: label('overdue', 'Overdue') },
            { value: 'review', label: label('review', 'Needs review') },
            { value: 'done', label: label('done', 'Done') },
            { value: 'archived', label: label('archived', 'No follow-up') },
          ]}
          value={bucket} onChange={(b) => { setBucket(b); setPage(1) }} />

        <div className="mt-4">
          <AsyncState loading={q.isLoading} error={q.error} empty={items.length === 0}
            emptyLabel={emptyLabel}>
            <Table>
              <THead>
                <TH>Customer</TH>
                <TH>When</TH>
                <TH>Type</TH>
                <TH>Priority</TH>
                <TH>Status</TH>
                <TH>Why</TH>
                <TH className="text-right">Actions</TH>
              </THead>
              <tbody>
                {items.map((f) => (
                  <TR key={f.id}>
                    <TD>
                      <ContactLabel name={f.contact_name} phone={f.dial_phone || f.phone} />
                      <div className="text-2xs text-dim">
                        {f.source === 'manual' ? 'manual' : `AI · conf ${(f.confidence ?? 0).toFixed(2)}`}
                      </div>
                    </TD>
                    <TD className="whitespace-nowrap tabular-nums">
                      {fmtDateTime(f.scheduled_at)}
                      {f.auto_scheduled ? (
                        <div className="text-2xs text-dim">AI-picked</div>
                      ) : null}
                    </TD>
                    <TD>{f.followup_type?.replace(/_/g, ' ') ?? '—'}</TD>
                    <TD><StatusBadge value={f.priority} /></TD>
                    <TD>
                      <div className="flex flex-wrap items-center gap-1">
                        <StatusBadge value={f.status} />
                        {/* `auto` on its own stopped meaning "the platform
                            will call this" in 0011: a named human must also
                            have approved the row. A badge that said "auto" on
                            an unapproved row is precisely the false
                            reassurance that let an unapproved call go out, so
                            this reports the gate, not the preference. */}
                        {f.mode === 'auto' && f.approval_state === 'approved' && (
                          <Badge tone="indigo" soft>
                            <Zap size={10} /> auto-call approved
                          </Badge>
                        )}
                        {f.approval_state === 'pending'
                          && !['completed', 'cancelled', 'archived'].includes(f.status) && (
                          <Badge tone="amber" soft>
                            <ShieldAlert size={10} /> needs approval
                          </Badge>
                        )}
                        {f.approval_state === 'declined' && (
                          <Badge tone="neutral" soft>do not auto-call</Badge>
                        )}
                      </div>
                    </TD>
                    <TD className="max-w-[320px]">
                      <span className="line-clamp-2 text-xs text-dim"
                        title={f.explanation ?? ''}>
                        {f.reason ?? f.explanation ?? '—'}
                      </span>
                      {/* Why the auto-dialer has not called this yet. Without
                          it a held row is indistinguishable from a forgotten
                          one — which is how 44 follow-ups quietly aged into
                          'missed' before the dialer existed. */}
                      {f.last_attempt_error && (
                        <span className={cx('mt-1 flex items-start gap-1 text-2xs',
                          f.autodial_blocked_at
                            ? 'text-amber-600 dark:text-amber-300' : 'text-dim')}>
                          <Info size={11} className="mt-0.5 shrink-0" aria-hidden />
                          {f.last_attempt_error}
                        </span>
                      )}
                      {!f.last_attempt_error && (f.attempts ?? 0) > 0 && (
                        <span className="mt-1 block text-2xs text-dim">
                          Auto-called {f.attempts}×
                          {f.last_attempt_at
                            ? ` · last ${fmtDateTime(f.last_attempt_at)}` : ''}
                          {f.execution_outcome ? ` · ${f.execution_outcome.replace(/_/g, ' ')}` : ''}
                          {f.next_attempt_at && !['completed', 'cancelled'].includes(f.status)
                            ? ` · next try ${fmtDateTime(f.next_attempt_at)}` : ''}
                        </span>
                      )}
                    </TD>
                    <TD className="text-right">
                      <div className="inline-flex items-center gap-0.5">
                        {f.call_key && (
                          <IconButton label={`Open the call for ${who(f)}`}
                            onClick={() => nav(`/intelligence/${f.call_key}`)}>
                            <ExternalLink size={14} />
                          </IconButton>
                        )}
                        {!['completed', 'cancelled', 'archived'].includes(f.status) && (
                          <>
                            {f.contact_handle && (
                              <IconButton
                                label={`Call ${who(f)} now on ${f.dial_phone ?? 'their number'}`}
                                disabled={callingId === f.id}
                                onClick={() => { void callNow(f) }}>
                                {callingId === f.id
                                  ? <Spinner className="h-3.5 w-3.5" />
                                  : <PhoneCall size={14} />}
                              </IconButton>
                            )}
                            <IconButton label={`Edit or reschedule the follow-up for ${who(f)}`}
                              onClick={() => openEdit(f)}>
                              <Pencil size={14} />
                            </IconButton>
                            <IconButton label={`Mark the follow-up for ${who(f)} completed`}
                              onClick={() => act(f, { status: 'completed' }, 'Marked completed')}>
                              <Check size={14} />
                            </IconButton>
                            <IconButton label={`Cancel the follow-up for ${who(f)}`}
                              onClick={() => cancelFollowup(f)}>
                              <X size={14} />
                            </IconButton>
                          </>
                        )}
                      </div>
                    </TD>
                  </TR>
                ))}
              </tbody>
            </Table>
            <Pagination page={page} pages={pages} total={q.data?.total} onPage={setPage} />
          </AsyncState>
        </div>
      </Card>

      {/* Edit / reschedule */}
      <Modal open={!!editing} onClose={() => setEditing(null)}
        title={`Edit follow-up${editing?.phone ? ` — ${editing.phone}` : ''}`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditing(null)}>Cancel</Button>
            <Button onClick={saveEdit} disabled={busy}>
              {busy ? <Spinner /> : <Check size={14} />} Save
            </Button>
          </>
        }>
        <div className="space-y-3">
          <Field label="Scheduled (IST)">
            <Input type="datetime-local" value={editWhen}
              onChange={(e) => setEditWhen(e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-3">
            <Field label="Priority">
              <Select value={editPriority} className="w-full"
                onChange={(e) => setEditPriority(e.target.value)}>
                <option value="low">Low</option>
                <option value="medium">Medium</option>
                <option value="high">High</option>
                <option value="urgent">Urgent</option>
              </Select>
            </Field>
            <Field label="Assign to">
              <Input value={editAssignee} placeholder="email"
                onChange={(e) => setEditAssignee(e.target.value)} />
            </Field>
          </div>
          <Field label="Execution">
            <Segmented<'manual' | 'auto'>
              value={editMode}
              onChange={setEditMode}
              options={[
                { value: 'manual', label: 'Manual — I call' },
                { value: 'auto', label: 'Auto — platform calls' },
              ]} />
            <p className="mt-1.5 text-2xs text-dim">
              {editMode === 'auto'
                ? 'Lets the platform place this call at the scheduled time (10:00–19:00 IST, Mon–Sat, retry with backoff), but only once you have approved it — this switch on its own never dials anyone. The agent opens the call remembering the previous conversation and this follow-up’s reason.'
                : 'Stays on your list — use the call button when you’re ready.'}
            </p>
            {editMode === 'auto' && editing?.approval_state !== 'approved' && (
              <p className="mt-1 text-2xs text-amber-600 dark:text-amber-300">
                Still needs approval — open “Review &amp; approve calls” to
                authorise it after saving.
              </p>
            )}
            {editing?.autodial_blocked_at && editMode === 'auto' && (
              <p className="mt-1 text-2xs text-amber-600 dark:text-amber-300">
                Auto-calling was previously stopped ({editing.last_attempt_error ?? 'blocked'}).
                Saving re-arms it.
              </p>
            )}
          </Field>
          <Field label="Add note">
            <Textarea rows={2} value={editNote}
              onChange={(e) => setEditNote(e.target.value)} />
          </Field>
          {editing && editing.notes.length > 0 && (
            <div>
              <div className="eyebrow mb-1">Notes</div>
              <ul className="max-h-28 space-y-1 overflow-y-auto text-xs text-dim">
                {editing.notes.map((n, i) => (
                  <li key={i}>{fmtDateTime(n.at)} — {n.text}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </Modal>

      {/* Manual create */}
      <Modal open={creating} onClose={() => setCreating(false)} title="New manual follow-up"
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreating(false)}>Cancel</Button>
            <Button onClick={createManual} disabled={busy || !newReason.trim()}>
              {busy ? <Spinner /> : <Plus size={14} />} Create
            </Button>
          </>
        }>
        <div className="space-y-3">
          <Field label="Reason">
            <Textarea rows={2} value={newReason}
              onChange={(e) => setNewReason(e.target.value)}
              placeholder="e.g. Call back with revised quotation" />
          </Field>
          <Field label="Scheduled (IST)">
            <Input type="datetime-local" value={newWhen}
              onChange={(e) => setNewWhen(e.target.value)} />
          </Field>
        </div>
      </Modal>

      <FollowupReview open={reviewing} onClose={() => setReviewing(false)} />
    </div>
  )
}
