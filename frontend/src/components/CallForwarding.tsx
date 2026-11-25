/* Conditional call forwarding (RUNBOOK §84) — the Telephony page's three pieces:
 *
 *   ForwardPhonesCard   the owner's own phones (add → code call → verify)
 *   NumberForwarding    per number: "My agent answers" | "Ring my phone first"
 *   ForwardedCallsCard  what happened to forwarded calls + this month's minutes
 *
 * The server is the authority on every rule (Indian numbers, verified phones,
 * 10–45 s ring, line-name characters); these controls only shape the request
 * and show the server's own sentence when it refuses. */
import { useState } from 'react'
import { PhoneForwarded, PhoneCall, Trash2, ShieldCheck, Plus } from 'lucide-react'
import { post, patch, del } from '../api/mutate'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { useToast } from './Toast'
import { useConfirm } from './confirm'
import { Card, Button, Badge, IconButton, Spinner, cx } from './ui'
import { Table, THead, TH, TR, TD, Select, Input, Field, AsyncState, Segmented } from './kit'
import { fmtDateTime } from '../lib/format'

export type ForwardPhone = {
  id: number; label: string; number: string; verified: boolean; verified_at?: string | null
}
type PhonesResp = { contacts: ForwardPhone[]; max: number; server_ready: boolean; server_missing: string[] }

export type ForwardNumber = {
  id: number; number: string
  forward_mode?: 'agent' | 'forward'; forward_contact_id?: number | null
  forward_ring_s?: number; forward_confirm?: boolean
  forward_fallback?: 'agent' | 'hangup'; forward_line_name?: string
}
type ForwardingResult = { state: 'on' | 'off' | 'error'; reason?: string | null; phone?: string }

type ForwardedCall = {
  id: number; line?: string | null; did: string; caller: string; contact_number?: string | null
  outcome: string; cause?: string | null; fallback?: string | null
  ring_s?: number | null; talk_s?: number | null; started_at?: string | null
}
type CallsResp = {
  calls: ForwardedCall[]
  summary: { month: string; calls: number; connected: number; talk_s: number; minutes: number }
}

const PHONES_KEY = ['telephony', 'forward-contacts']
const CALLS_KEY = ['telephony', 'forwarded-calls']
const RING_CHOICES = [10, 15, 20, 25, 30, 35, 40, 45]

const masked = (n?: string | null) => {
  const d = (n || '').replace(/\D/g, '')
  return d.length >= 4 ? `••••${d.slice(-4)}` : '—'
}
const phoneName = (p: ForwardPhone) => `${p.label ? `${p.label} · ` : ''}${p.number}`

export function useForwardPhones() {
  return useGet<PhonesResp>(PHONES_KEY, '/forward-contacts')
}

/* ------------------------------------------------------------------ phones */

export function ForwardPhonesCard() {
  const toast = useToast()
  const confirm = useConfirm()
  const refresh = useRefresh()
  const phonesQ = useForwardPhones()
  const phones = phonesQ.data?.contacts ?? []
  const max = phonesQ.data?.max ?? 5
  const serverReady = phonesQ.data?.server_ready !== false

  const [form, setForm] = useState({ label: '', number: '' })
  const [busy, setBusy] = useState<string | null>(null)
  const [codeFor, setCodeFor] = useState<number | null>(null)
  const [code, setCode] = useState('')

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key)
    try { await fn() } catch (e: any) { toast(e.message || String(e), 'error') }
    finally { setBusy(null) }
  }

  const add = () => run('add', async () => {
    const out = await post<ForwardPhone>('/forward-contacts', form)
    setForm({ label: '', number: '' })
    toast(`${out.number} added — now verify it with a code call`, 'success')
    refresh(PHONES_KEY)
  })

  const callWithCode = (p: ForwardPhone) => run(`call-${p.id}`, async () => {
    const out = await post<{ calling: string; calls_left_today: number }>(
      `/forward-contacts/${p.id}/verify-call`)
    setCodeFor(p.id)
    setCode('')
    toast(`Calling ${out.calling} — answer it and note the 4-digit code (${out.calls_left_today} more calls today)`, 'success')
  })

  const verify = (p: ForwardPhone) => run(`verify-${p.id}`, async () => {
    await post(`/forward-contacts/${p.id}/verify`, { code })
    setCodeFor(null)
    setCode('')
    toast(`${p.number} verified — you can now forward calls to it`, 'success')
    refresh(PHONES_KEY)
  })

  const remove = async (p: ForwardPhone) => {
    const ok = await confirm({
      title: `Remove ${p.number}?`,
      description: 'Any number set to ring this phone goes back to its agent answering every call.',
      destructive: true, confirmLabel: 'Remove phone',
    })
    if (!ok) return
    run(`del-${p.id}`, async () => {
      const out = await del<{ numbers_back_to_agent: number[] }>(`/forward-contacts/${p.id}`)
      toast(out.numbers_back_to_agent.length
        ? `Removed — ${out.numbers_back_to_agent.length} number(s) are back on their agent`
        : 'Phone removed', 'success')
      refresh(['telephony'])
    })
  }

  const canAdd = form.number.replace(/\D/g, '').length >= 10 && phones.length < max

  return (
    <Card className="p-5">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 text-sm font-semibold">
            <PhoneForwarded size={15} className="text-indigo-500" /> Phones I can forward calls to
          </div>
          <div className="text-xs text-dim">
            Your numbers can ring one of these phones <b>before</b> your agent answers. Indian numbers
            only. Each phone is checked once: we call it and a voice reads a 4-digit code.
          </div>
        </div>
        <span className="text-xs tabular-nums text-dim">{phones.length}/{max} phones</span>
      </div>

      {!serverReady && (
        <div className="mb-3 rounded-xl bg-amber-500/8 px-3 py-2.5 text-xs text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
          Call forwarding is still being switched on for our phone system. You can add phones now;
          the code call and forwarding start working once that is finished.
        </div>
      )}

      <AsyncState loading={phonesQ.isLoading} error={phonesQ.error} variant="rows" rows={2}>
        {phones.length > 0 && (
          <ul className="mb-3 divide-y divide-[var(--border)] rounded-xl ring-1 ring-[var(--border)]">
            {phones.map((p) => (
              <li key={p.id} className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-center sm:justify-between">
                <div className="flex min-w-0 flex-wrap items-center gap-2">
                  <span className="truncate text-sm font-medium">{phoneName(p)}</span>
                  {p.verified
                    ? <Badge tone="mint" soft><ShieldCheck size={11} /> verified</Badge>
                    : <Badge tone="amber" soft>not verified yet</Badge>}
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  {!p.verified && codeFor !== p.id && (
                    <Button size="sm" variant="soft" disabled={busy === `call-${p.id}` || !serverReady}
                      onClick={() => callWithCode(p)}>
                      {busy === `call-${p.id}` ? <Spinner /> : <PhoneCall size={13} />} Call me with a code
                    </Button>
                  )}
                  {!p.verified && codeFor === p.id && (
                    <>
                      <Input inputMode="numeric" maxLength={4} placeholder="4-digit code" value={code}
                        aria-label={`Code read out on the call to ${p.number}`}
                        onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 4))}
                        className="!w-32 tabular-nums" />
                      <Button size="sm" disabled={code.length !== 4 || busy === `verify-${p.id}`}
                        onClick={() => verify(p)}>
                        {busy === `verify-${p.id}` ? <Spinner /> : <ShieldCheck size={13} />} Verify
                      </Button>
                      <Button size="sm" variant="ghost" disabled={busy === `call-${p.id}`}
                        onClick={() => callWithCode(p)}>Call again</Button>
                    </>
                  )}
                  <IconButton label={`Remove ${p.number}`} onClick={() => remove(p)}>
                    {busy === `del-${p.id}` ? <Spinner /> : <Trash2 size={13} />}
                  </IconButton>
                </div>
              </li>
            ))}
          </ul>
        )}
      </AsyncState>

      {phones.length < max && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
          <Field label="Name (optional)">
            <Input placeholder="My mobile" maxLength={60} value={form.label}
              onChange={(e) => setForm({ ...form, label: e.target.value })} />
          </Field>
          <Field label="Phone number">
            <Input placeholder="+91 98765 43210" inputMode="tel" value={form.number}
              onChange={(e) => setForm({ ...form, number: e.target.value })} />
          </Field>
          <Button disabled={!canAdd || busy === 'add'} onClick={add}>
            {busy === 'add' ? <Spinner /> : <Plus size={14} />} Add phone
          </Button>
        </div>
      )}
    </Card>
  )
}

/* ------------------------------------------------------------- per number */

export function NumberForwarding({ n, phones, serverReady }: {
  n: ForwardNumber; phones: ForwardPhone[]; serverReady: boolean
}) {
  const toast = useToast()
  const refresh = useRefresh()
  const verified = phones.filter((p) => p.verified)
  const saved = {
    mode: (n.forward_mode ?? 'agent') as 'agent' | 'forward',
    contact: n.forward_contact_id ?? null,
    ring: n.forward_ring_s ?? 25,
    confirm: n.forward_confirm ?? true,
    fallback: (n.forward_fallback ?? 'agent') as 'agent' | 'hangup',
    line: n.forward_line_name ?? '',
  }
  const [draft, setDraft] = useState(saved)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<ForwardingResult | null>(null)
  // The phone list can load after this row: in forward mode an unset contact
  // means "the first verified phone", resolved here, never stored as a change.
  const contactNow = draft.contact ?? verified[0]?.id ?? null
  const dirty = draft.mode !== saved.mode || (draft.mode === 'forward' && (
    contactNow !== saved.contact || draft.ring !== saved.ring || draft.confirm !== saved.confirm
    || draft.fallback !== saved.fallback || draft.line.trim() !== saved.line))

  const savedPhone = phones.find((p) => p.id === n.forward_contact_id)
  const liveNow = saved.mode === 'forward' && serverReady && !!savedPhone?.verified

  const save = async () => {
    setBusy(true)
    try {
      const body = draft.mode === 'agent'
        ? { forward_mode: 'agent' }
        : {
            forward_mode: 'forward', forward_contact_id: contactNow,
            forward_ring_s: draft.ring, forward_confirm: draft.confirm,
            forward_fallback: draft.fallback, forward_line_name: draft.line,
          }
      const out = await patch<{ forwarding?: ForwardingResult }>(`/numbers/${n.id}`, body)
      const fw = out.forwarding ?? null
      setResult(fw)
      if (fw?.state === 'error') toast(`Saved, but not live yet: ${fw.reason}`, 'error')
      else if (fw?.state === 'on') toast(`${n.number} now rings ${fw.phone} first`, 'success')
      else toast(`${n.number}: your agent answers every call`, 'success')
      refresh(['telephony'])
    } catch (e: any) {
      toast(e.message || String(e), 'error')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl bg-[var(--surface-2)] px-3 py-3 ring-1 ring-[var(--border)]">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="text-xs font-semibold text-soft">When someone calls {n.number}:</span>
        <Segmented<'agent' | 'forward'>
          options={[{ value: 'agent', label: 'My agent answers' },
                    { value: 'forward', label: 'Ring my phone first' }]}
          value={draft.mode}
          onChange={(mode) => setDraft({ ...draft, mode })} />
        {!dirty && saved.mode === 'forward' && (
          liveNow
            ? <Badge tone="mint" soft><PhoneForwarded size={11} /> rings {masked(savedPhone?.number)} first</Badge>
            : <Badge tone="amber" soft>saved — not live yet</Badge>
        )}
      </div>

      {draft.mode === 'forward' && (
        <div className="mt-3 space-y-3">
          {verified.length === 0 ? (
            <p className="text-xs text-amber-700 dark:text-amber-300">
              Add and verify a phone in “Phones I can forward calls to” below first.
            </p>
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Phone to ring">
                <Select className="w-full" value={contactNow ?? ''}
                  onChange={(e) => setDraft({ ...draft, contact: Number(e.target.value) || null })}>
                  {verified.map((p) => <option key={p.id} value={p.id}>{phoneName(p)}</option>)}
                </Select>
              </Field>
              <Field label="Ring for">
                <Select className="w-full" value={draft.ring}
                  onChange={(e) => setDraft({ ...draft, ring: Number(e.target.value) })}>
                  {RING_CHOICES.map((s) => <option key={s} value={s}>{s} seconds</option>)}
                </Select>
              </Field>
              <Field label="If I don't answer">
                <Select className="w-full" value={draft.fallback}
                  onChange={(e) => setDraft({ ...draft, fallback: e.target.value as 'agent' | 'hangup' })}>
                  <option value="agent">My agent answers</option>
                  <option value="hangup">Hang up</option>
                </Select>
              </Field>
              <Field label="Line name (read to you)">
                <Input placeholder="Namaskall" maxLength={40} value={draft.line}
                  onChange={(e) => setDraft({ ...draft, line: e.target.value })} />
              </Field>
            </div>
          )}
          {verified.length > 0 && (
            <>
              <label className="flex items-start gap-2 text-xs text-soft">
                <input type="checkbox" className="mt-0.5" checked={draft.confirm}
                  onChange={(e) => setDraft({ ...draft, confirm: e.target.checked })} />
                <span>
                  <b>I must press 1 to accept</b> — you will hear “Call on your {draft.line.trim() || 'Namaskall'} line,
                  from a number ending 4 3 2 1. Press 1 to accept this call.”
                  {!draft.confirm && (
                    <span className="mt-0.5 block text-amber-700 dark:text-amber-300">
                      Without it, if your phone is off its voicemail would “answer” and the caller would be
                      connected to your voicemail instead of your agent.
                    </span>
                  )}
                </span>
              </label>
              <p className="text-2xs text-dim">
                Your phone will show <b>{n.number}</b>, not the caller’s number. Talk time is billed on two
                phone legs and counted as forwarded minutes, separately from AI minutes. Don’t set your
                phone to forward its missed calls to {n.number} while this is on.
              </p>
            </>
          )}
        </div>
      )}

      {(dirty || result) && (
        <div className="mt-3 flex flex-wrap items-center gap-3">
          {dirty && (
            <Button size="sm" disabled={busy || (draft.mode === 'forward' && !contactNow)} onClick={save}>
              {busy ? <Spinner /> : null} Save
            </Button>
          )}
          {dirty && <Button size="sm" variant="ghost" onClick={() => { setDraft(saved); setResult(null) }}>Cancel</Button>}
          {!dirty && result?.state === 'error' && (
            <span className="text-xs text-amber-700 dark:text-amber-300">{result.reason}</span>
          )}
        </div>
      )}
    </div>
  )
}

/* ---------------------------------------------------------- call history */

const OUTCOME: Record<string, { label: string; tone: 'mint' | 'amber' | 'rose' | 'neutral' }> = {
  connected: { label: 'You answered', tone: 'mint' },
  no_answer: { label: 'Not answered', tone: 'amber' },
  busy: { label: 'Busy', tone: 'amber' },
  rejected: { label: 'Declined', tone: 'amber' },
  not_accepted: { label: 'Answered, 1 not pressed', tone: 'amber' },
  caller_hung_up: { label: 'Caller hung up', tone: 'neutral' },
  failed: { label: 'Did not ring', tone: 'rose' },
}

const fmtSecs = (s?: number | null) => {
  const v = s ?? 0
  return v >= 60 ? `${Math.floor(v / 60)}m ${v % 60}s` : `${v}s`
}

export function ForwardedCallsCard() {
  const callsQ = useGet<CallsResp>(CALLS_KEY, '/forwarded-calls')
  const calls = callsQ.data?.calls ?? []
  const sum = callsQ.data?.summary

  return (
    <Card className="p-5">
      <div className="mb-3 flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-semibold">Forwarded calls</div>
          <div className="text-xs text-dim">
            Calls that rang your phone first. They appear a few minutes after they end. Forwarded
            minutes are counted separately from AI minutes.
          </div>
        </div>
        {sum && (
          <div className="flex flex-wrap gap-4 text-xs text-dim">
            <span><b className="text-base tabular-nums text-[var(--text)]">{sum.calls}</b> forwarded this month</span>
            <span><b className="text-base tabular-nums text-[var(--text)]">{sum.connected}</b> answered by you</span>
            <span><b className="text-base tabular-nums text-[var(--text)]">{sum.minutes}</b> forwarded min</span>
          </div>
        )}
      </div>
      <AsyncState loading={callsQ.isLoading} error={callsQ.error} variant="rows" rows={3}
        empty={calls.length === 0}
        emptyLabel="No forwarded calls yet — switch a number to “Ring my phone first” above.">
        <Table>
          <THead><TH>When</TH><TH>Caller</TH><TH>Line</TH><TH>Result</TH><TH>Talk</TH></THead>
          <tbody>
            {calls.map((c) => {
              const o = OUTCOME[c.outcome] ?? { label: c.outcome, tone: 'neutral' as const }
              return (
                <TR key={c.id}>
                  <TD className="whitespace-nowrap text-dim">{fmtDateTime(c.started_at)}</TD>
                  <TD className="tabular-nums">{c.caller === 'anonymous' ? 'Hidden number' : `+${c.caller}`}</TD>
                  <TD className="tabular-nums text-dim">{c.line || `+${c.did}`}</TD>
                  <TD>
                    <span className="inline-flex flex-wrap items-center gap-1.5">
                      <Badge tone={o.tone} soft>{o.label}</Badge>
                      {c.fallback === 'agent' && <span className={cx('text-2xs text-dim')}>→ agent answered</span>}
                    </span>
                  </TD>
                  <TD className="tabular-nums">{c.outcome === 'connected' ? fmtSecs(c.talk_s) : '—'}</TD>
                </TR>
              )
            })}
          </tbody>
        </Table>
      </AsyncState>
    </Card>
  )
}
