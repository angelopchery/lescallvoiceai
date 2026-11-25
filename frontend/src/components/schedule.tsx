/**
 * Scheduled calls and campaigns — ONE dialog and ONE list, shared by Contacts
 * and Outbound.
 *
 * Both pages can schedule, and the rules an operator has to understand are the
 * same on both: the calling window, what "missed" means, that a schedule is
 * cancellable until it fires. A second copy of this dialog is a second place
 * for those explanations to drift out of step with `shared/scheduling.py`,
 * which is the thing that actually enforces them.
 *
 * The server owns every rule. This component does not pre-validate the time
 * against the calling window — it shows the window, and renders the server's
 * refusal verbatim when it comes. A client-side copy of the window would be a
 * second definition of business hours, and the first one to go stale.
 */
import { useEffect, useState } from 'react'
import { CalendarClock, X } from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { post, del } from '../api/mutate'
import { Badge, Button, EmptyState, IconButton, Spinner } from './ui'
import { Field, Input, Table, THead, TH, TR, TD, AsyncState } from './kit'
import { Modal } from './Modal'
import { useToast } from './Toast'
import { useConfirm } from './confirm'

export type Schedule = {
  id: number
  kind: 'call' | 'campaign'
  label?: string | null
  contact_handle?: string | null
  contact_name?: string
  contact_phone?: string
  contact_missing?: boolean
  scheduled_at: string
  status: 'pending' | 'running' | 'done' | 'failed' | 'missed' | 'cancelled'
  status_text?: string
  last_error?: string | null
  will_dial?: number
}

type SchedulesResp = { schedules: Schedule[]; hours: { start: number; end: number } }

const TONE: Record<Schedule['status'], 'mint' | 'indigo' | 'amber' | 'rose' | 'neutral'> = {
  pending: 'indigo', running: 'mint', done: 'mint',
  failed: 'rose', missed: 'amber', cancelled: 'neutral',
}

/** IST wall-clock, which is what every operator on this platform thinks in and
 *  what the calling window is defined in. Rendering the browser's local zone
 *  would show a Gulf-based operator a time the server will refuse. */
export function fmtSlot(iso?: string | null): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleString('en-IN', {
    weekday: 'short', day: '2-digit', month: 'short',
    hour: '2-digit', minute: '2-digit', hour12: false,
    timeZone: 'Asia/Kolkata',
  }) + ' IST'
}

/** `<input type="datetime-local">` gives a naive wall-clock string. The server
 *  reads a naive time as IST, so send it through unchanged rather than letting
 *  `new Date()` reinterpret it in the browser's zone — that is how a schedule
 *  set for 4pm fires at 10:30. */
function localValueToPayload(v: string): string {
  return v
}

/** Default slot: the next round hour at least an hour out, clamped into the
 *  morning of the window so the picker opens on a time the server accepts. */
function defaultSlot(startHour: number): string {
  const d = new Date()
  d.setMinutes(0, 0, 0)
  d.setHours(d.getHours() + 1)
  if (d.getHours() < startHour) d.setHours(startHour)
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`
}

export type ScheduleTarget =
  | { kind: 'call'; contactId: string; name?: string; phone?: string }
  | { kind: 'campaign'; audience: Record<string, unknown>; summary?: string }

/**
 * The dialog. `target` decides what is being scheduled; `null` closes it.
 */
export function ScheduleDialog({
  target, onClose, hours,
}: {
  target: ScheduleTarget | null
  onClose: () => void
  hours?: { start: number; end: number }
}) {
  const toast = useToast()
  const refresh = useRefresh()
  const startHour = hours?.start ?? 10
  const [when, setWhen] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    if (target) { setWhen(defaultSlot(startHour)); setLabel(''); setError('') }
  }, [target, startHour])

  const save = async () => {
    if (!target || !when) return
    setBusy(true)
    setError('')
    try {
      const body: Record<string, unknown> = {
        kind: target.kind, when: localValueToPayload(when), label: label.trim(),
      }
      if (target.kind === 'call') body.contact_id = target.contactId
      else body.audience = target.audience
      await post('/outbound/schedule', body)
      toast(target.kind === 'call' ? 'Call scheduled' : 'Campaign scheduled', 'success')
      refresh(['schedules'])
      onClose()
    } catch (e: unknown) {
      // The server's refusal is the useful sentence — it names the calling
      // window and the next slot that would work. Shown in the dialog rather
      // than as a toast so the operator can read it while fixing the time.
      setError(String((e as Error)?.message ?? e).replace(/^\d+\s*/, ''))
    } finally {
      setBusy(false)
    }
  }

  const who = target?.kind === 'call'
    ? (target.name || target.phone || 'this contact')
    : (target?.summary || 'this audience')

  return (
    <Modal
      open={!!target}
      onClose={onClose}
      title={target?.kind === 'campaign' ? 'Schedule campaign' : 'Schedule call'}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>Cancel</Button>
          <Button onClick={save} disabled={busy || !when}>
            {busy ? <Spinner className="h-4 w-4" /> : null} Schedule
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <p className="text-sm text-soft">
          {target?.kind === 'campaign'
            ? <>The campaign will start automatically at this time, dialling <strong>{who}</strong>.</>
            : <>The agent will call <strong>{who}</strong> at this time.</>}
        </p>
        <Field label="Date and time (IST)">
          <Input type="datetime-local" value={when}
            onChange={(e) => setWhen(e.target.value)} />
        </Field>
        <Field label="Note (optional)">
          <Input value={label} onChange={(e) => setLabel(e.target.value)}
            placeholder={target?.kind === 'campaign' ? 'Morning follow-up run' : 'Asked us to call after lunch'} />
        </Field>
        {error ? (
          <p className="rounded-md bg-rose-500/10 px-3 py-2 text-sm text-rose-400">{error}</p>
        ) : (
          <p className="text-xs text-dim">
            Calls go out between {String(hours?.start ?? 10).padStart(2, '0')}:00 and{' '}
            {String(hours?.end ?? 19).padStart(2, '0')}:00 IST, Monday to Saturday.
            You can cancel any time before it runs.
          </p>
        )}
      </div>
    </Modal>
  )
}

/** Load the tenant's schedule. Exported so a page can read `hours` for the
 *  dialog without a second request. */
export function useSchedules() {
  return useGet<SchedulesResp>(['schedules'], '/outbound/schedule')
}

/**
 * The list. Pending items first (soonest at the top), then what already
 * happened — a schedule nobody can see is a schedule nobody trusts.
 */
export function ScheduleList({ compact }: { compact?: boolean }) {
  const q = useSchedules()
  const toast = useToast()
  const refresh = useRefresh()
  const confirm = useConfirm()
  const rows = q.data?.schedules ?? []

  const cancel = async (s: Schedule) => {
    const who = s.kind === 'campaign'
      ? (s.label || 'this campaign')
      : (s.contact_name || s.contact_phone || 'this contact')
    if (!(await confirm({
      title: 'Cancel this schedule?',
      description: `The ${s.kind === 'campaign' ? 'campaign' : 'call'} to ${who} at ${fmtSlot(s.scheduled_at)} will not run.`,
      destructive: true,
    }))) return
    try {
      await del(`/outbound/schedule/${s.id}`)
      toast('Schedule cancelled', 'success')
      refresh(['schedules'])
    } catch (e: unknown) {
      toast(`Could not cancel: ${String((e as Error)?.message ?? e)}`, 'error')
    }
  }

  return (
    <AsyncState loading={q.isLoading} error={q.error} variant="rows" rows={3}>
      {rows.length === 0 ? (
        <EmptyState
          icon={<CalendarClock size={20} />}
          title="Nothing scheduled yet"
          hint="Use Schedule on a contact to set a time for its call, or schedule a campaign to run a whole list at a fixed hour."
        />
      ) : (
      <Table>
        <THead>
          <TH>When</TH>
          <TH>What</TH>
          {!compact && <TH>Note</TH>}
          <TH>Status</TH>
          <TH className="text-right">Actions</TH>
        </THead>
        <tbody>
          {rows.map((s) => (
            <TR key={s.id}>
              <TD className="whitespace-nowrap text-sm tabular-nums">{fmtSlot(s.scheduled_at)}</TD>
              <TD>
                {s.kind === 'campaign' ? (
                  <span className="text-sm">Campaign</span>
                ) : (
                  <span className="text-sm">
                    {s.contact_name || s.contact_phone || '—'}
                    {s.contact_missing && (
                      <span className="ml-2 text-xs text-amber-400">contact deleted</span>
                    )}
                  </span>
                )}
              </TD>
              {!compact && (
                <TD className="max-w-[240px]">
                  <span className="block truncate text-xs text-dim" title={s.label || ''}>
                    {s.label || '—'}
                  </span>
                </TD>
              )}
              <TD>
                <span title={s.status_text || ''}>
                  <Badge tone={TONE[s.status] ?? 'neutral'} soft>{s.status}</Badge>
                </span>
                {(s.status === 'failed' || s.status === 'missed') && s.status_text && (
                  <p className="mt-1 max-w-[280px] text-xs text-dim">{s.status_text}</p>
                )}
              </TD>
              <TD className="text-right">
                {s.status === 'pending' ? (
                  <IconButton
                    label={`Cancel the ${s.kind === 'campaign' ? 'campaign' : 'call'} scheduled for ${s.contact_name || s.contact_phone || fmtSlot(s.scheduled_at)}`}
                    onClick={() => cancel(s)}
                  >
                    <X size={14} />
                  </IconButton>
                ) : (
                  <span className="text-xs text-dim">—</span>
                )}
              </TD>
            </TR>
          ))}
        </tbody>
      </Table>
      )}
    </AsyncState>
  )
}
