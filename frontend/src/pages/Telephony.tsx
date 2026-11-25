import { Fragment, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  Plus, Trash2, UploadCloud, Info, Copy, PhoneCall, ChevronDown, ChevronRight,
  PlugZap, CheckCircle2, Circle, XCircle,
} from 'lucide-react'
import { api } from '../api/client'
import { post, patch, del } from '../api/mutate'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { useToast } from '../components/Toast'
import { Card, Button, Badge, IconButton, Spinner, EmptyState, cx } from '../components/ui'
import {
  PageHeader, Table, THead, TH, TR, TD, Select, Input, Field, AsyncState,
} from '../components/kit'
import { Modal } from '../components/Modal'
import { fmtDateTime } from '../lib/format'
import {
  ForwardPhonesCard, ForwardedCallsCard, NumberForwarding, useForwardPhones, type ForwardNumber,
} from '../components/CallForwarding'

type Trunk = {
  id: number; name: string; provider: string; sip_domain: string
  username?: string; caller_id?: string; provider_ips?: string
  config_status: string; last_applied_at?: string | null
}
type PhoneNumber = ForwardNumber & {
  id: number; number: string; sig_digits: string; trunk_id?: number | null
  inbound_agent_slug?: string | null; outbound_agent_slug?: string | null
  direction: string; label?: string
  config_status: string
}
/** Which of a number's two agent bindings a control writes to. */
type BindField = 'inbound_agent_slug' | 'outbound_agent_slug'
type ServerInfo = {
  sip_uri: string; public_ip: string; sip_port: number; transport: string
  rtp_range: string; notes: string[]
}
type Instructions = { trunk: string; sip_uri: string; rtp_range: string; steps: string[] }
/** Slice of the Settings profile we need: where test calls ring. */
type Profile = { phone?: string }
type AgentOption = { name: string; call_direction?: 'inbound' | 'outbound' }
type AgentsResponse = { agents: AgentOption[]; active?: string | null }
type ConfirmTarget = { kind: 'number'; n: PhoneNumber } | { kind: 'trunk'; t: Trunk }

function statusTone(s?: string): 'mint' | 'amber' | 'rose' | 'neutral' {
  if (s === 'applied') return 'mint'
  if (s === 'pending') return 'amber'
  if (s === 'error') return 'rose'
  return 'neutral'
}
const fmtDate = (dt?: string | null) => fmtDateTime(dt)
const digits = (s: string) => s.replace(/\D/g, '')
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1)

/* Agent <option>s for one direction slot. An agent built for the other
   direction is still selectable — the suffix is a hint, not a restriction. */
function AgentOptions({ agents, want }: { agents: AgentOption[]; want: 'inbound' | 'outbound' }) {
  return (
    <>
      {agents.map((a) => {
        const dir = a.call_direction === 'outbound' ? 'outbound' : 'inbound'
        return (
          <option key={a.name} value={a.name}>
            {a.name}{dir !== want ? ` (built for ${dir})` : ''}
          </option>
        )
      })}
    </>
  )
}

/* One copy-paste value the user has to put into their carrier console. */
function CopyValue({ label, value, hint, onCopy }: {
  label: string; value: string; hint?: string; onCopy: (v: string) => void
}) {
  // Labels can carry a "· context" suffix — the accessible name only needs the value's name.
  const shortLabel = label.split('·')[0].trim()
  return (
    <div className="flex flex-col gap-1 rounded-xl bg-[var(--surface-2)] p-3">
      <div className="text-2xs font-semibold uppercase tracking-wide text-dim">{label}</div>
      <div className="flex items-center gap-2">
        <code className="flex-1 truncate rounded-lg bg-[var(--surface)] px-2.5 py-1.5 text-sm font-semibold">{value}</code>
        <IconButton label={`Copy ${shortLabel}`} onClick={() => onCopy(value)}><Copy size={13} /></IconButton>
      </div>
      {hint && <div className="text-xs text-dim">{hint}</div>}
    </div>
  )
}

/* Step-number circle for the two-step wizard. */
function StepCircle({ n }: { n: number }) {
  return (
    <span className="grid h-6 w-6 place-items-center rounded-full bg-mint-400/15 text-xs font-bold text-mint-700 ring-1 ring-mint-400/30 dark:text-mint-300">
      {n}
    </span>
  )
}

const CONNECT_PHASES = ['Create trunk', 'Register number', 'Deploy to FreeSWITCH']
const PROVIDERS = ['plivo', 'twilio', 'telnyx', 'vonage', 'other']

const EMPTY_CONNECT = {
  provider: 'plivo', number: '', sip_domain: '',
  inbound_agent_slug: '', outbound_agent_slug: '',
  trunk_name: '', provider_ips: '', username: '', password: '',
}
const EMPTY_TRUNK = {
  name: '', provider: 'plivo', sip_domain: '', username: '', password: '',
  caller_id: '', provider_ips: '',
}

/**
 * What a Free account sees in place of the BYON setup flow.
 *
 * Deliberately shows the real shape of the thing being unlocked — carrier
 * trunk, your own DID, your agent answering it — rather than a bare
 * "upgrade required". Someone deciding whether to pay should be able to see
 * what they get.
 */
function TelephonyLocked() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Telephony"
        sub="Bring Your Own Number — connect your carrier and point a number at your agent"
      />
      <Card className="p-6">
        <div className="flex items-start gap-4">
          <div className="rounded-2xl bg-[var(--surface-2)] p-3">
            <PlugZap size={22} className="text-dim" />
          </div>
          <div className="min-w-0 flex-1">
            <h2 className="text-lg font-semibold">Connect your own number on a paid plan</h2>
            <p className="mt-1 max-w-2xl text-sm text-soft">
              Your account can build and test agents right now. To put one on a
              real phone line — your carrier, your number, your caller ID —
              you&rsquo;ll need a plan. Pilot is the usual starting point: seven
              days and 500 minutes to run real conversations with your own
              customers.
            </p>

            <ul className="mt-4 space-y-1.5 text-sm text-soft">
              {[
                'Register your SIP trunk (Plivo, Twilio, or any SIP carrier)',
                'Point one of your own numbers at an agent you built',
                'Inbound and outbound, English and Hindi',
                'A test call to your own phone before you go live',
              ].map((t) => (
                <li key={t} className="flex items-start gap-2">
                  <CheckCircle2 size={15} className="mt-0.5 shrink-0 text-emerald-500" />
                  <span>{t}</span>
                </li>
              ))}
            </ul>

            <div className="mt-5 flex flex-wrap gap-2">
              <Link to="/billing"><Button variant="primary">See plans</Button></Link>
              <Link to="/build/agents"><Button variant="ghost">Build an agent first</Button></Link>
            </div>
            <p className="mt-4 text-xs text-dim">
              Already on a paid plan and still seeing this? Reload the page, or
              contact us and we&rsquo;ll sort it out.
            </p>
          </div>
        </div>
      </Card>
    </div>
  )
}


export default function Telephony() {
  const toast = useToast()
  const refresh = useRefresh()
  const trunksQ = useGet<{ trunks: Trunk[] }>(['telephony', 'trunks'], '/trunks')
  const numbersQ = useGet<{ numbers: PhoneNumber[] }>(['telephony', 'numbers'], '/numbers')
  const infoQ = useGet<ServerInfo>(['telephony', 'server-info'], '/telephony/server-info')
  const agentsQ = useGet<AgentsResponse>(['agents'], '/agents')
  // Conditional call forwarding (RUNBOOK §84): the owner's own phones.
  const fwdQ = useForwardPhones()
  const fwdPhones = fwdQ.data?.contacts ?? []
  const fwdReady = fwdQ.data?.server_ready !== false
  // Same source Settings edits — shows where "Test call" actually rings.
  const profileQ = useGet<Profile>(['profile'], '/auth/profile')
  const testPhone = profileQ.data?.phone ? `+${profileQ.data.phone}` : ''
  // Paid-plan gate. The server refuses the mutations regardless (402); this
  // just avoids walking someone through a five-field carrier form that is
  // going to be rejected at the end.
  const billingQ = useGet<{ features?: { telephony?: boolean } }>(
    ['billing', 'summary'], '/billing/summary')
  // Undefined while loading, or if /billing is unavailable — default to
  // allowing, so a billing outage never hides a working panel from a paying
  // customer. The server is the authority either way.
  const telephonyAllowed = billingQ.data?.features?.telephony !== false

  const [connectForm, setConnectForm] = useState({ ...EMPTY_CONNECT })
  const [showCreds, setShowCreds] = useState(false)
  const [showNetDetails, setShowNetDetails] = useState(false)
  const [showAdvanced, setShowAdvanced] = useState(false)
  const [trunkModal, setTrunkModal] = useState(false)
  const [trunkForm, setTrunkForm] = useState({ ...EMPTY_TRUNK })
  const [instructions, setInstructions] = useState<Instructions | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [connectPhase, setConnectPhase] = useState(0)
  const [connectError, setConnectError] = useState<string | null>(null)
  const [touched, setTouched] = useState<{ number?: boolean; sip_domain?: boolean }>({})
  const [confirmTarget, setConfirmTarget] = useState<ConfirmTarget | null>(null)
  const [collapsedChecklists, setCollapsedChecklists] = useState<Set<number>>(new Set())
  // keyed by `${field}-${number id}` so each direction gets its own tick
  const [justSaved, setJustSaved] = useState<string | null>(null)

  const agentList = agentsQ.data?.agents ?? []
  const agentSlugs = agentList.map((a) => a.name)
  const trunks = trunksQ.data?.trunks ?? []
  const numbers = numbersQ.data?.numbers ?? []
  const info = infoQ.data

  const copy = (text: string) => {
    navigator.clipboard?.writeText(text)
    toast('Copied to clipboard', 'info')
  }

  const run = async (key: string, fn: () => Promise<void>) => {
    setBusy(key)
    try { await fn() } catch (e: any) { toast(e.message || String(e), 'error') }
    finally { setBusy(null) }
  }

  /* Ring the account owner's phone (Settings → phone) FROM this number, with
     its bound agent live — the end-to-end "does my number actually work" check. */
  const testCall = (n: PhoneNumber) => run(`test-${n.id}`, async () => {
    const res = await post<any>(`/numbers/${n.id}/test-call`)
    toast(res.message || `Calling your phone from ${n.number} now — pick up to talk to your agent.`, 'success')
  })

  /* -------- the one-shot connect flow: trunk -> number -> apply -------- */
  const numberValid = digits(connectForm.number).length >= 7
  const domainValid = connectForm.sip_domain.trim().length > 3
  const canConnect = numberValid && domainValid

  const connect = () => run('connect', async () => {
    setConnectError(null)
    setConnectPhase(0)
    const num = connectForm.number.trim()
    const name = (connectForm.trunk_name.trim()
      || `${connectForm.provider}-${digits(num).slice(-6)}`).replace(/[^A-Za-z0-9_-]/g, '')

    let trunkId: number
    try {
      const t = await post<any>('/trunks', {
        name,
        provider: connectForm.provider,
        sip_domain: connectForm.sip_domain.trim(),
        provider_ips: connectForm.provider_ips.trim(),
        username: connectForm.username.trim(),
        password: connectForm.password,
        caller_id: num,
      })
      trunkId = t.id
    } catch (e: any) {
      setConnectError(`The trunk could not be created (${e.message}). Nothing was changed — fix the details and press Connect again.`)
      throw new Error(`Trunk: ${e.message}`)
    }

    setConnectPhase(1)
    try {
      await post<any>('/numbers', {
        number: num,
        trunk_id: trunkId,
        inbound_agent_slug: connectForm.inbound_agent_slug || undefined,
        // left alone → not sent; the backend mirrors the inbound choice
        outbound_agent_slug: connectForm.outbound_agent_slug || undefined,
        label: 'connected via panel',
      })
    } catch (e: any) {
      refresh(['telephony'])
      setShowAdvanced(true)
      setConnectError(`Trunk "${name}" was created, but the number failed to register (${e.message}). Fix and retry, or manage the trunk under Advanced below.`)
      throw new Error(`Number: ${e.message} — the trunk "${name}" was created and is kept (see Advanced below)`)
    }

    setConnectPhase(2)
    const res = await post<any>(`/trunks/${trunkId}/apply`)
    if (res.status !== 'applied') {
      setShowAdvanced(true)
      setConnectError(`Trunk "${name}" and ${num} are registered, but the FreeSWITCH deploy reported "${res.status}" — press Apply on the trunk under Advanced to retry.`)
    } else {
      setConnectPhase(0)
    }
    setConnectForm({ ...EMPTY_CONNECT })
    setTouched({})
    refresh(['telephony'])
    if (res.status === 'applied') {
      const inSlug = connectForm.inbound_agent_slug
      const outSlug = connectForm.outbound_agent_slug || inSlug
      const routedTo = !inSlug ? ''
        : outSlug === inSlug ? ` to ${inSlug}`
          : ` — ${inSlug} answers, ${outSlug} calls out`
      toast(`${num} is connected and routed${routedTo} 🎉`, 'success')
    } else {
      toast(`Number registered, but the FreeSWITCH deploy reported "${res.status}" — press Apply on the trunk (Advanced) to retry`, 'error')
    }
  })

  /* ------------------------------- numbers ------------------------------ */
  const rebind = (n: PhoneNumber, field: BindField, slug: string) => {
    const key = `${field}-${n.id}`
    return run(`rebind-${key}`, async () => {
      await patch(`/numbers/${n.id}`, { [field]: slug })
      const which = field === 'inbound_agent_slug' ? 'incoming calls' : 'outgoing calls'
      toast(`${n.number} ${which} → ${slug || '(none)'} — effective on the next call`, 'success')
      setJustSaved(key)
      setTimeout(() => setJustSaved((cur) => (cur === key ? null : cur)), 1500)
      refresh(['telephony'])
    })
  }
  const deleteNumberNow = (n: PhoneNumber) => {
    run(`del-num-${n.id}`, async () => {
      await del(`/numbers/${n.id}`)
      toast('Number removed', 'success')
      refresh(['telephony'])
    })
  }

  /* --------------------------- advanced: trunks ------------------------- */
  const applyTrunk = (id: number) => run(`apply-${id}`, async () => {
    const res = await post<any>(`/trunks/${id}/apply`)
    toast(res.status === 'applied' ? 'FreeSWITCH config generated + reloaded'
      : `Apply finished with status: ${res.status}`,
      res.status === 'applied' ? 'success' : 'error')
    refresh(['telephony'])
  })
  const deleteTrunkNow = (t: Trunk) => {
    run(`del-trunk-${t.id}`, async () => {
      await del(`/trunks/${t.id}`)
      toast('Trunk deleted', 'success')
      refresh(['telephony'])
    })
  }
  const confirmDelete = () => {
    if (!confirmTarget) return
    const target = confirmTarget
    setConfirmTarget(null)
    if (target.kind === 'number') deleteNumberNow(target.n)
    else deleteTrunkNow(target.t)
  }
  const trunkConfirmNumbers = confirmTarget?.kind === 'trunk'
    ? numbers.filter((n) => n.trunk_id === confirmTarget.t.id)
    : []
  const showInstructions = (id: number) => run(`instr-${id}`, async () => {
    setInstructions(await api<Instructions>(`/trunks/${id}/instructions`))
  })
  const createTrunk = () => run('trunk-create', async () => {
    const res = await post<any>('/trunks', trunkForm)
    toast(`Trunk ${res.name} created`, 'success')
    setTrunkModal(false)
    setTrunkForm({ ...EMPTY_TRUNK })
    refresh(['telephony'])
  })

  const toggleChecklist = (id: number) => setCollapsedChecklists((prev) => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    return next
  })

  // Connecting a real number is a paid capability. The panel stays VISIBLE and
  // readable — a Free user should be able to see exactly what they would be
  // setting up — but every control that would write is replaced by an upgrade
  // path. Hiding the page instead would make the product look like it cannot
  // do this at all.
  if (!telephonyAllowed) return <TelephonyLocked />

  return (
    <div className="space-y-6">
      <PageHeader
        title="Telephony"
        sub="Bring Your Own Number — two steps: configure your carrier, then connect your number"
      />

      {/* ---------------- STEP 1: carrier console values ---------------- */}
      <Card className="p-5">
        <div className="mb-1 flex items-center gap-2">
          <StepCircle n={1} />
          <span className="text-base font-semibold">In your carrier console (e.g. Plivo Zentrunk)</span>
        </div>
        <div className="mb-3 space-y-1 sm:pl-8">
          <p className="text-xs text-dim">
            <b className="text-soft">a.</b> Create an <b>inbound (origination)</b> trunk and an <b>outbound (termination)</b> trunk.
          </p>
          <p className="text-xs text-dim">
            <b className="text-soft">b.</b> Paste these two values <b>into</b> your carrier where it asks for them —
            then <b className="text-soft">c.</b> copy the termination SIP domain back <b>from</b> your outbound trunk:
          </p>
        </div>
        <AsyncState loading={infoQ.isLoading} error={infoQ.error} variant="text" rows={5}>
          {info && (
            <>
              <div className="grid grid-cols-1 gap-3 sm:pl-8 md:grid-cols-3">
                <CopyValue
                  label="Origination URI  ·  goes on your INBOUND trunk"
                  value={info.sip_uri.replace(/^sip:/, '')}
                  hint="Where your carrier sends incoming calls (Plivo: Zentrunk → Inbound trunk → Origination URI)"
                  onCopy={copy}
                />
                <CopyValue
                  label="IP ACL entry  ·  allow this server"
                  value={`${info.public_ip}/32`}
                  hint="Add to your carrier's IP Access Control List so our calls are accepted (Plivo: Zentrunk → IP ACL)"
                  onCopy={copy}
                />
                <div className="flex flex-col gap-1 rounded-xl bg-indigo-500/8 p-3 ring-1 ring-indigo-500/20">
                  <div className="text-2xs font-semibold uppercase tracking-wide text-indigo-600 dark:text-indigo-300">
                    Termination SIP domain · copy FROM your outbound trunk
                  </div>
                  {/* Placeholder pattern, not a copyable value — deliberately not chip-styled. */}
                  <span className="px-0.5 py-1.5 font-mono text-sm text-dim">xxxxxxxx.zt.plivo.com</span>
                  <div className="flex flex-wrap items-center justify-between gap-1">
                    <span className="text-xs text-dim">Find it on your outbound (termination) trunk</span>
                    <span className="shrink-0 rounded-full bg-indigo-500/12 px-2 py-0.5 text-2xs font-semibold text-indigo-600 ring-1 ring-indigo-500/20 dark:text-indigo-300">
                      paste in step 2 ↓
                    </span>
                  </div>
                </div>
              </div>
              <div className="mt-3 sm:pl-8">
                <button className="flex items-center gap-1 text-xs font-medium text-dim hover:text-soft"
                  aria-expanded={showNetDetails} aria-controls="net-details-panel"
                  onClick={() => setShowNetDetails(!showNetDetails)}>
                  {showNetDetails ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                  More network details (port, transport, RTP range)
                </button>
                {showNetDetails && (
                  <div id="net-details-panel" className="mt-2 space-y-2">
                    <div className="grid grid-cols-1 gap-3 md:grid-cols-3">
                      <CopyValue label="SIP port" value={String(info.sip_port)} onCopy={copy} />
                      <CopyValue label="Transport" value={info.transport} onCopy={copy} />
                      <CopyValue label="RTP media range" value={info.rtp_range}
                        hint="Open these UDP ports toward us on any firewall" onCopy={copy} />
                    </div>
                    {info.notes?.length > 0 && (
                      <ul className="list-disc space-y-0.5 pl-4 text-xs text-dim">
                        {info.notes.map((note, i) => <li key={i}>{note}</li>)}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            </>
          )}
        </AsyncState>
      </Card>

      {/* ---------------- STEP 2: one-shot connect form ----------------- */}
      <Card className="p-5">
        <div className="mb-1 flex items-center gap-2">
          <StepCircle n={2} />
          <span className="text-base font-semibold">Connect your number</span>
        </div>
        <p className="mb-3 text-xs text-dim sm:pl-8">
          One go: this registers the trunk + number, deploys the phone-system config, and routes calls to your agent.
        </p>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 sm:pl-8 lg:grid-cols-4">
          <Field label="Provider">
            <Select value={connectForm.provider}
              onChange={(e) => setConnectForm({ ...connectForm, provider: e.target.value })}>
              {PROVIDERS.map((p) => <option key={p} value={p}>{cap(p)}</option>)}
            </Select>
          </Field>
          <Field label="Your number">
            <Input placeholder="+918065953160" value={connectForm.number}
              onChange={(e) => setConnectForm({ ...connectForm, number: e.target.value })}
              onBlur={() => setTouched((t) => ({ ...t, number: true }))}
              className={touched.number && !numberValid ? '!ring-rose-500/40' : undefined} />
            {touched.number && !numberValid && (
              <span className="mt-1 block text-2xs text-rose-500">Include the country code, e.g. +918065953160</span>
            )}
          </Field>
          <Field label="Termination SIP domain (outbound trunk)">
            <Input placeholder="xxxxxxxx.zt.plivo.com" value={connectForm.sip_domain}
              onChange={(e) => setConnectForm({ ...connectForm, sip_domain: e.target.value.replace(/^sip:/i, '') })}
              onBlur={() => setTouched((t) => ({ ...t, sip_domain: true }))}
              className={touched.sip_domain && !domainValid ? '!ring-rose-500/40' : undefined} />
            {touched.sip_domain && !domainValid && (
              <span className="mt-1 block text-2xs text-rose-500">Should look like xxxxxxxx.zt.plivo.com (no sip: prefix)</span>
            )}
          </Field>
          <Field label="Agent that answers incoming calls">
            <Select value={connectForm.inbound_agent_slug}
              onChange={(e) => setConnectForm({ ...connectForm, inbound_agent_slug: e.target.value })}>
              <option value="">(choose later)</option>
              <AgentOptions agents={agentList} want="inbound" />
            </Select>
            {agentSlugs.length === 0 && !agentsQ.isLoading && (
              <span className="mt-1 block text-2xs text-dim">
                No agents yet —{' '}
                <Link to="/build/agents/new" className="font-semibold text-mint-600 hover:underline dark:text-mint-300">create one first →</Link>
                {' '}(you can still connect the number now and bind later)
              </span>
            )}
          </Field>
          <Field label="Agent that speaks on outgoing calls">
            <Select value={connectForm.outbound_agent_slug}
              onChange={(e) => setConnectForm({ ...connectForm, outbound_agent_slug: e.target.value })}>
              <option value="">(same as incoming)</option>
              <AgentOptions agents={agentList} want="outbound" />
            </Select>
            <span className="mt-1 block text-2xs text-dim">
              Optional — leave as is and outgoing calls use the same agent as incoming ones.
            </span>
          </Field>
        </div>
        <div className="mt-2 sm:pl-8">
          <button className="flex items-center gap-1 text-xs font-medium text-dim hover:text-soft"
            aria-expanded={showCreds} aria-controls="trunk-creds-panel"
            onClick={() => setShowCreds(!showCreds)}>
            {showCreds ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
            Optional: trunk credentials &amp; carrier signaling IPs
          </button>
          {showCreds && (
            <div id="trunk-creds-panel" className="mt-2 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
              <Field label="Username (blank = IP auth)">
                <Input value={connectForm.username}
                  onChange={(e) => setConnectForm({ ...connectForm, username: e.target.value })} />
              </Field>
              <Field label="Password">
                <Input type="password" value={connectForm.password}
                  onChange={(e) => setConnectForm({ ...connectForm, password: e.target.value })} />
              </Field>
              <Field label="Carrier signaling IPs (CIDRs)">
                <Input placeholder="1.2.3.0/24, 5.6.7.8" value={connectForm.provider_ips}
                  onChange={(e) => setConnectForm({ ...connectForm, provider_ips: e.target.value })} />
              </Field>
              <Field label="Trunk name (auto if blank)">
                <Input placeholder="plivo-953160" value={connectForm.trunk_name}
                  onChange={(e) => setConnectForm({ ...connectForm, trunk_name: e.target.value })} />
              </Field>
            </div>
          )}
        </div>
        <div className="mt-4 flex flex-wrap items-center gap-3 sm:pl-8">
          <Button disabled={!canConnect || busy === 'connect'} onClick={connect}>
            {busy === 'connect' ? <Spinner /> : <PlugZap size={15} />} Connect number
          </Button>
          {!canConnect && busy !== 'connect' && !connectError && (
            <span className="text-xs text-dim">
              Enter your number (with country code) and the termination SIP domain from step 1
            </span>
          )}
          {(busy === 'connect' || connectError) && (
            <div className="flex flex-wrap items-center gap-3">
              {CONNECT_PHASES.map((label, i) => {
                const done = i < connectPhase
                const failed = !!connectError && i === connectPhase
                const current = !connectError && i === connectPhase
                return (
                  <span key={label} className={cx('flex items-center gap-1.5 text-xs',
                    failed ? 'font-medium text-rose-600 dark:text-rose-300'
                      : current ? 'font-medium text-[var(--text)]' : 'text-dim')}>
                    {done ? <CheckCircle2 size={14} className="shrink-0 text-mint-500" />
                      : failed ? <XCircle size={14} className="shrink-0 text-rose-500" />
                        : current ? <Spinner className="h-3 w-3 shrink-0" />
                          : <Circle size={14} className="shrink-0 text-[var(--border)]" />}
                    {label}
                  </span>
                )
              })}
            </div>
          )}
        </div>
        {connectError && (
          <div role="alert"
            className="mt-3 rounded-xl bg-rose-500/10 px-3 py-2.5 text-sm text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300 sm:ml-8">
            {connectError}
          </div>
        )}
      </Card>

      {/* ------------------------- your numbers ------------------------- */}
      <Card className="p-5">
        <div className="mb-3">
          <div className="text-sm font-semibold">Your numbers</div>
          <div className="text-xs text-dim">
            Bind <b>your agents</b> to a number — one for the calls it answers, one for the calls it
            places — and apply its trunk. Use <b>Test call</b> to ring your own phone and hear your
            agent exactly as callers do.{' '}
            {testPhone ? (
              <>Test calls ring <b className="tabular-nums text-soft">{testPhone}</b>{' '}
                (<Link to="/settings" className="font-semibold text-indigo-600 hover:underline dark:text-indigo-300">change in Settings →</Link>)
              </>
            ) : (
              <Link to="/settings" className="font-semibold text-indigo-600 hover:underline dark:text-indigo-300">
                Set your phone in Settings →
              </Link>
            )}
          </div>
        </div>
        <AsyncState loading={numbersQ.isLoading} error={numbersQ.error} variant="cards" rows={3}>
          {numbers.length === 0 ? (
            <EmptyState icon={<PhoneCall size={20} />} title="No numbers connected yet"
              hint="Complete steps 1 and 2 above to connect your first number" />
          ) : (
            <Table>
              <THead>
                <TH>Number</TH><TH>Agents</TH><TH>Trunk</TH><TH>Config</TH><TH></TH>
              </THead>
              <tbody>
                {numbers.map((n) => {
                  const hasInbound = !!n.inbound_agent_slug
                  const hasOutbound = !!n.outbound_agent_slug
                  // Fully configured only once BOTH directions are bound.
                  const hasAgent = hasInbound && hasOutbound
                  const applied = n.config_status === 'applied'
                  const ownsAgent = agentSlugs.length > 0
                  const callable = hasAgent && applied
                  // Ordered readiness steps; the first incomplete one is "next".
                  const steps = [
                    { done: ownsAgent, label: 'Create your own agent',
                      hint: 'Build an agent in Copilot or Create Agent', action: 'agent' },
                    { done: hasAgent,
                      label: 'Bind an agent to both directions of this number',
                      hint: hasInbound && !hasOutbound
                        ? 'Incoming is set — pick one under "Speaks on outgoing calls"'
                        : !hasInbound && hasOutbound
                          ? 'Outgoing is set — pick one under "Answers incoming calls"'
                          : 'Pick one for each direction in the Agents column',
                      action: null },
                    { done: applied, label: 'Apply the trunk',
                      hint: 'Advanced → Apply generates the phone-system config', action: null },
                  ]
                  const nextIdx = steps.findIndex((s) => !s.done)
                  const doneCount = steps.filter((s) => s.done).length
                  const collapsed = collapsedChecklists.has(n.id)
                  return (
                  <Fragment key={n.id}>
                  <TR>
                    <TD className="font-semibold">
                      {n.number}
                      {callable && (
                        <span className="ml-2"><Badge tone="mint" soft><CheckCircle2 size={11} /> ready</Badge></span>
                      )}
                      {!callable && (
                        <span className="ml-2"><Badge tone="amber" soft>{doneCount}/3 setup</Badge></span>
                      )}
                    </TD>
                    <TD>
                      <div className="flex flex-col gap-2">
                        {([
                          { field: 'inbound_agent_slug', want: 'inbound', label: 'Answers incoming calls', bound: hasInbound },
                          { field: 'outbound_agent_slug', want: 'outbound', label: 'Speaks on outgoing calls', bound: hasOutbound },
                        ] as { field: BindField; want: 'inbound' | 'outbound'; label: string; bound: boolean }[]).map((slot) => {
                          const key = `${slot.field}-${n.id}`
                          const working = busy === `rebind-${key}`
                          return (
                            <label key={slot.field} className="flex flex-col gap-1">
                              <span className="text-2xs font-semibold uppercase tracking-wide text-dim">{slot.label}</span>
                              <span className="inline-flex items-center gap-2">
                                <Select value={n[slot.field] || ''}
                                  disabled={working}
                                  onChange={(e) => rebind(n, slot.field, e.target.value)}
                                  className={slot.bound ? '' : 'ring-1 ring-amber-500/50'}>
                                  <option value="">(pick your agent)</option>
                                  <AgentOptions agents={agentList} want={slot.want} />
                                </Select>
                                {working && <Spinner className="h-3 w-3" />}
                                {!working && justSaved === key && (
                                  <CheckCircle2 size={14} className="text-mint-500 animate-fade-up" />
                                )}
                              </span>
                            </label>
                          )
                        })}
                      </div>
                    </TD>
                    <TD className="text-dim">{trunks.find((t) => t.id === n.trunk_id)?.name || '—'}</TD>
                    <TD>
                      <span className="inline-flex items-center gap-1.5">
                        {applied && <CheckCircle2 size={14} className="text-mint-500" />}
                        <Badge tone={statusTone(n.config_status)} soft>{applied ? 'live' : n.config_status}</Badge>
                      </span>
                    </TD>
                    <TD>
                      <div className="flex items-center justify-end gap-2">
                        <Button size="sm" variant="soft" title={callable
                            ? `Ring your phone from ${n.number} with "${n.outbound_agent_slug}" live`
                            : 'Finish the setup steps first'}
                          disabled={!callable || busy === `test-${n.id}`}
                          onClick={() => testCall(n)}>
                          {busy === `test-${n.id}` ? <Spinner /> : <PhoneCall size={13} />} Test call
                        </Button>
                        <Button size="sm" variant="danger" title={`Disconnect ${n.number}`}
                          disabled={busy === `del-num-${n.id}`}
                          onClick={() => setConfirmTarget({ kind: 'number', n })}>
                          {busy === `del-num-${n.id}` ? <Spinner /> : <Trash2 size={13} />}
                        </Button>
                      </div>
                    </TD>
                  </TR>
                  {applied && (
                    <TR>
                      <TD colSpan={5} className="!py-2">
                        <div className="sticky left-0 max-w-[calc(100vw-6rem)]">
                          <NumberForwarding n={n} phones={fwdPhones} serverReady={fwdReady} />
                        </div>
                      </TD>
                    </TR>
                  )}
                  {!callable && (
                    <TR>
                      <TD colSpan={5} className="!py-2">
                        <div className="sticky left-0 max-w-[calc(100vw-6rem)] rounded-xl bg-amber-500/8 px-3 py-2.5 ring-1 ring-amber-500/20">
                          <div className="mb-1.5 flex items-center justify-between gap-2">
                            <div className="text-xs font-semibold text-amber-700 dark:text-amber-300">
                              To make calls from {n.number}, finish these steps:
                            </div>
                            <button onClick={() => toggleChecklist(n.id)}
                              title={collapsed ? 'Expand checklist' : 'Collapse checklist'}
                              aria-label={collapsed ? 'Expand checklist' : 'Collapse checklist'}
                              aria-expanded={!collapsed}
                              className="shrink-0 rounded-md p-2 -m-1 text-amber-700 hover:bg-amber-500/10 dark:text-amber-300">
                              {collapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                            </button>
                          </div>
                          {collapsed ? (
                            <div className="text-xs text-dim">
                              {nextIdx >= 0
                                ? <>Next: <span className="font-medium text-[var(--text)]">{steps[nextIdx].label}</span> — {steps[nextIdx].hint}</>
                                : 'All steps done'}
                            </div>
                          ) : (
                            <>
                              <ol className="flex flex-col gap-1">
                                {steps.map((s, i) => (
                                  <li key={i} className={cx('flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm',
                                    s.done ? 'text-dim' : i === nextIdx ? 'font-medium text-[var(--text)]' : 'text-dim')}>
                                    {s.done
                                      ? <CheckCircle2 size={14} className="shrink-0 text-mint-500" />
                                      : <Circle size={14} className={cx('shrink-0', i === nextIdx ? 'text-amber-500' : 'text-[var(--border)]')} />}
                                    <span className={s.done ? 'line-through opacity-70' : ''}>{i + 1}. {s.label}</span>
                                    {!s.done && i === nextIdx && (
                                      <span className="text-2xs text-dim">— {s.hint}</span>
                                    )}
                                    {!s.done && i === nextIdx && s.action === 'agent' && (
                                      <Link to="/build/agents/new" className="ml-1 text-xs font-semibold text-mint-600 hover:underline dark:text-mint-300">Create agent →</Link>
                                    )}
                                  </li>
                                ))}
                              </ol>
                              <div className="mt-1.5 text-2xs text-dim">
                                Your caller ID is authorised by your own carrier (the call goes out over your trunk) — no separate number verification needed.
                              </div>
                            </>
                          )}
                        </div>
                      </TD>
                    </TR>
                  )}
                  </Fragment>
                )})}
              </tbody>
            </Table>
          )}
        </AsyncState>
      </Card>

      {/* ------------------- call forwarding (RUNBOOK §84) ------------------- */}
      {numbers.length > 0 && <ForwardPhonesCard />}
      {numbers.length > 0 && <ForwardedCallsCard />}

      {/* --------------------------- advanced --------------------------- */}
      <button className="flex items-center gap-1.5 text-xs font-medium text-dim hover:text-soft"
        aria-expanded={showAdvanced} aria-controls="advanced-trunks-panel"
        onClick={() => setShowAdvanced(!showAdvanced)}>
        {showAdvanced ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        Advanced: trunks {trunks.length > 0 && `(${trunks.length})`}
      </button>
      {showAdvanced && (
        <div id="advanced-trunks-panel">
        <Card className={cx('p-5')}>
          <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
            <div className="text-sm font-semibold">Trunks (carrier connections)</div>
            <Button size="sm" variant="ghost" onClick={() => setTrunkModal(true)}><Plus size={14} /> Add trunk only</Button>
          </div>
          <AsyncState loading={trunksQ.isLoading} error={trunksQ.error}>
            {trunks.length === 0 ? (
              <EmptyState icon={<PlugZap size={20} />} title="No trunks yet"
                hint="The Connect flow above creates one for you automatically" />
            ) : (
              <Table>
                <THead>
                  <TH>Name</TH><TH>Provider</TH><TH>SIP domain</TH><TH>Provider IPs</TH>
                  <TH>Status</TH><TH>Last applied</TH><TH></TH>
                </THead>
                <tbody>
                  {trunks.map((t) => (
                    <TR key={t.id}>
                      <TD className="font-medium">{t.name}</TD>
                      <TD>{t.provider}</TD>
                      <TD><code className="text-xs">{t.sip_domain}</code></TD>
                      <TD className="max-w-[180px] truncate" title={t.provider_ips}>{t.provider_ips || '—'}</TD>
                      <TD><Badge tone={statusTone(t.config_status)} soft>{t.config_status}</Badge></TD>
                      <TD className="text-dim">{fmtDate(t.last_applied_at)}</TD>
                      <TD>
                        <div className="flex items-center justify-end gap-1.5">
                          <Button size="sm" variant="soft" disabled={busy === `apply-${t.id}`} onClick={() => applyTrunk(t.id)}>
                            {busy === `apply-${t.id}` ? <Spinner /> : <UploadCloud size={13} />} Apply
                          </Button>
                          <Button size="sm" variant="ghost" disabled={busy === `instr-${t.id}`}
                            onClick={() => showInstructions(t.id)} title="Carrier setup steps">
                            {busy === `instr-${t.id}` ? <Spinner /> : <Info size={13} />}
                          </Button>
                          <Button size="sm" variant="danger" title={`Delete trunk ${t.name}`}
                            disabled={busy === `del-trunk-${t.id}`}
                            onClick={() => setConfirmTarget({ kind: 'trunk', t })}>
                            {busy === `del-trunk-${t.id}` ? <Spinner /> : <Trash2 size={13} />}
                          </Button>
                        </div>
                      </TD>
                    </TR>
                  ))}
                </tbody>
              </Table>
            )}
          </AsyncState>
        </Card>
        </div>
      )}

      {/* Confirm disconnect / delete modal */}
      <Modal open={!!confirmTarget} onClose={() => setConfirmTarget(null)}
        title={confirmTarget?.kind === 'number' ? 'Disconnect number?' : 'Delete trunk?'}
        footer={<>
          <Button variant="ghost" onClick={() => setConfirmTarget(null)}>Cancel</Button>
          <Button variant="danger" onClick={confirmDelete}>
            <Trash2 size={13} /> {confirmTarget?.kind === 'number' ? 'Disconnect' : 'Delete trunk'}
          </Button>
        </>}>
        {confirmTarget?.kind === 'number' && (
          <p className="text-sm text-soft">
            Inbound calls to <b>{confirmTarget.n.number}</b> will stop being answered, and outbound calls
            from it will fail. You can reconnect it later via the Connect flow above.
          </p>
        )}
        {confirmTarget?.kind === 'trunk' && (
          <div className="space-y-3 text-sm text-soft">
            <p>
              The carrier connection <b>{confirmTarget.t.name}</b> ({confirmTarget.t.sip_domain}) will be
              removed from the phone system.
            </p>
            {trunkConfirmNumbers.length > 0 && (
              <div className="rounded-xl bg-amber-500/8 px-3 py-2.5 text-sm text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
                {trunkConfirmNumbers.length === 1
                  ? <>1 number still routes through this trunk: <b>{trunkConfirmNumbers[0].number}</b> — calls on it will stop working.</>
                  : <>{trunkConfirmNumbers.length} numbers still route through this trunk: <b>{trunkConfirmNumbers.map((n) => n.number).join(', ')}</b> — calls on them will stop working.</>}
              </div>
            )}
          </div>
        )}
      </Modal>

      {/* Add-trunk-only modal (advanced) */}
      <Modal open={trunkModal} onClose={() => setTrunkModal(false)} title="Add trunk" wide
        footer={<>
          <Button variant="ghost" onClick={() => setTrunkModal(false)}>Cancel</Button>
          <Button disabled={busy === 'trunk-create' || !trunkForm.name || !trunkForm.sip_domain} onClick={createTrunk}>
            {busy === 'trunk-create' ? <Spinner /> : <Plus size={14} />} Create
          </Button>
        </>}>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          <Field label="Name"><Input placeholder="myplivo" value={trunkForm.name}
            onChange={(e) => setTrunkForm({ ...trunkForm, name: e.target.value })} /></Field>
          <Field label="Provider">
            <Select value={trunkForm.provider} onChange={(e) => setTrunkForm({ ...trunkForm, provider: e.target.value })}>
              {PROVIDERS.map((p) => <option key={p} value={p}>{cap(p)}</option>)}
            </Select>
          </Field>
          <Field label="SIP domain"><Input placeholder="xxxx.zt.plivo.com" value={trunkForm.sip_domain}
            onChange={(e) => setTrunkForm({ ...trunkForm, sip_domain: e.target.value })} /></Field>
          <Field label="Caller ID"><Input placeholder="+91..." value={trunkForm.caller_id}
            onChange={(e) => setTrunkForm({ ...trunkForm, caller_id: e.target.value })} /></Field>
          <Field label="Username (blank = IP auth)"><Input value={trunkForm.username}
            onChange={(e) => setTrunkForm({ ...trunkForm, username: e.target.value })} /></Field>
          <Field label="Password"><Input type="password" value={trunkForm.password}
            onChange={(e) => setTrunkForm({ ...trunkForm, password: e.target.value })} /></Field>
          <div className="sm:col-span-2">
            <Field label="Provider signaling IPs (CIDRs, comma-separated)">
              <Input placeholder="1.2.3.0/24, 5.6.7.8" value={trunkForm.provider_ips}
                onChange={(e) => setTrunkForm({ ...trunkForm, provider_ips: e.target.value })} />
            </Field>
          </div>
        </div>
      </Modal>

      {/* Carrier instructions modal */}
      <Modal open={!!instructions} onClose={() => setInstructions(null)} title="Carrier setup" wide>
        {instructions && (
          <div className="space-y-3 text-sm">
            <div>Trunk:{' '}
              <code className="break-all rounded bg-[var(--surface-2)] px-1.5 py-0.5">{instructions.trunk}</code>
              <IconButton label="Copy trunk name" className="ml-0.5 align-middle"
                onClick={() => copy(instructions.trunk)}><Copy size={13} /></IconButton>
            </div>
            <div>Origination URI:{' '}
              <code className="break-all rounded bg-[var(--surface-2)] px-1.5 py-0.5">{instructions.sip_uri}</code>
              <IconButton label="Copy origination URI" className="ml-0.5 align-middle"
                onClick={() => copy(instructions.sip_uri)}><Copy size={13} /></IconButton>
            </div>
            {instructions.rtp_range && (
              <div>RTP media range:{' '}
                <code className="break-all rounded bg-[var(--surface-2)] px-1.5 py-0.5">{instructions.rtp_range}</code>
                <IconButton label="Copy RTP media range" className="ml-0.5 align-middle"
                  onClick={() => copy(instructions.rtp_range)}><Copy size={13} /></IconButton>
              </div>
            )}
            <ol className="list-decimal space-y-1.5 pl-5">
              {instructions.steps.map((s, i) => <li key={i}>{s}</li>)}
            </ol>
          </div>
        )}
      </Modal>
    </div>
  )
}
