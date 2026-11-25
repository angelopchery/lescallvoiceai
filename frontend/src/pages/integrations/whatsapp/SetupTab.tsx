import { useEffect, useState } from 'react'
import { CheckCircle2, Circle, KeyRound, Link2, RefreshCw, Unplug } from 'lucide-react'
import { Badge, Button, Card, SectionHead, cx } from '../../../components/ui'
import { Field, Input, Segmented, Select } from '../../../components/kit'
import { CopyButton } from '../../../components/copy'
import { useToast } from '../../../components/Toast'
import { useConfirm } from '../../../components/confirm'
import { useRefresh } from '../../../api/hooks/useApi'
import {
  connectInterakt, connectWa, disconnectWa, refreshWa, saveWaSettings, setWebhookSecret,
  type WaOverview, type WaProvider,
} from '../../../api/whatsapp'
import { fmtDateTime } from '../../../lib/format'
import { Hint } from './shared'

const QUALITY: Record<string, { label: string; tone: 'mint' | 'amber' | 'rose' }> = {
  GREEN: { label: 'High quality', tone: 'mint' },
  YELLOW: { label: 'Medium quality', tone: 'amber' },
  RED: { label: 'Low quality', tone: 'rose' },
}

const TIER: Record<string, string> = {
  TIER_50: '50 people / day', TIER_250: '250 people / day', TIER_1K: '1,000 people / day',
  TIER_10K: '10,000 people / day', TIER_100K: '100,000 people / day', TIER_UNLIMITED: 'Unlimited',
}

/** A connection exists when there is a status beyond "disconnected" — Meta
 *  accounts also carry a phone number id, an Interakt account only a key. */
export const isConnected = (a: WaOverview['account']) =>
  a.status !== 'disconnected' && (a.provider === 'interakt' || !!a.phone_number_id)

export function SetupTab({ data }: { data: WaOverview }) {
  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_360px]">
      <div className="space-y-5">
        <ConnectionCard data={data} />
        {isConnected(data.account) && data.account.provider === 'interakt' && <WebhookCard data={data} />}
        <RulesCard data={data} />
      </div>
      <div className="space-y-5">
        <HowItWorks data={data} />
        <PlatformCard data={data} />
      </div>
    </div>
  )
}

function useRun() {
  const toast = useToast()
  const refresh = useRefresh()
  const [busy, setBusy] = useState(false)
  async function run(fn: () => Promise<unknown>, ok: string) {
    setBusy(true)
    try {
      await fn()
      toast(ok, 'success')
      refresh(['wa'])
    } catch (e: any) {
      toast(e?.reason || e?.message || 'Something went wrong', 'error')
    } finally {
      setBusy(false)
    }
  }
  return { run, busy, toast }
}

function ConnectionCard({ data }: { data: WaOverview }) {
  const a = data.account
  const confirm = useConfirm()
  const { run, busy, toast } = useRun()
  const [provider, setProvider] = useState<WaProvider>(a.provider === 'meta' && a.waba_id ? 'meta' : 'interakt')
  // Meta form
  const [waba, setWaba] = useState('')
  const [pnid, setPnid] = useState('')
  const [token, setToken] = useState('')
  // Interakt form
  const [apiKey, setApiKey] = useState('')
  const [phone, setPhone] = useState('')
  const [secret, setSecret] = useState('')

  if (!isConnected(a)) {
    return (
      <Card className="p-5">
        <SectionHead title="Connect your WhatsApp Business number"
          sub="Messages go out from your own verified business number, under your business name." />
        {!data.platform.token_key_ready && (
          <Hint tone="amber" className="mb-4">
            The platform has not been given its token-encryption key yet, so a number cannot be
            connected. Ask the Namaskall team to finish WhatsApp setup — everything else on this
            page already works.
          </Hint>
        )}
        <div className="mb-4">
          <div className="mb-1 text-xs font-semibold text-soft">Where is your WhatsApp Business number?</div>
          <Segmented value={provider} onChange={(v) => setProvider(v)}
            options={[{ value: 'interakt', label: 'Interakt' }, { value: 'meta', label: 'Meta Cloud API' }]} />
          <p className="mt-1.5 text-xs text-dim">
            {provider === 'interakt'
              ? 'Your number is managed in Interakt (interakt.ai). One API key connects it; templates are created and approved in Interakt and added here.'
              : 'Your number is on your own Meta WhatsApp Business account. Connect it with the IDs and a permanent token from Meta’s WhatsApp Manager.'}
          </p>
        </div>

        {provider === 'interakt' ? (
          <>
            <Field label="Interakt API key">
              <Input value={apiKey} onChange={(e) => setApiKey(e.target.value)} type="password"
                autoComplete="off" placeholder="Paste the Secret Key from Interakt" />
            </Field>
            <div className="mt-3 grid gap-3 sm:grid-cols-2">
              <Field label="Business WhatsApp number (optional, for display)">
                <Input value={phone} onChange={(e) => setPhone(e.target.value)} inputMode="tel"
                  placeholder="+91 98xxx xxxxx" />
              </Field>
              <Field label="Webhook secret (optional, for delivery receipts)">
                <Input value={secret} onChange={(e) => setSecret(e.target.value)} type="password"
                  autoComplete="off" placeholder="Any secret you also paste into Interakt" />
              </Field>
            </div>
            <p className="mt-2 text-xs text-dim">
              In Interakt: Settings → Developer Settings → <span className="font-semibold">Secret Key</span>.
              The key is verified with a read-only request, stored encrypted and never shown again.
              Receipts (sent / delivered / read) and replies arrive once the webhook is set up — the
              next card explains it.
            </p>
            <div className="mt-4 flex justify-end">
              <Button disabled={busy || apiKey.trim().length < 16 || !data.platform.token_key_ready}
                onClick={() => run(async () => {
                  await connectInterakt({ api_key: apiKey, display_phone: phone, webhook_secret: secret })
                  setApiKey(''); setSecret('')
                }, 'Interakt connected — now add your approved template')}>
                <KeyRound size={15} /> Verify and connect
              </Button>
            </div>
          </>
        ) : (
          <>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="WhatsApp Business Account ID">
                <Input value={waba} onChange={(e) => setWaba(e.target.value.trim())} inputMode="numeric"
                  placeholder="e.g. 102290129340398" />
              </Field>
              <Field label="Phone number ID">
                <Input value={pnid} onChange={(e) => setPnid(e.target.value.trim())} inputMode="numeric"
                  placeholder="e.g. 106540352242922" />
              </Field>
            </div>
            <div className="mt-3">
              <Field label="Permanent access token">
                <Input value={token} onChange={(e) => setToken(e.target.value)} type="password"
                  autoComplete="off" placeholder="EAAG…" />
              </Field>
            </div>
            <p className="mt-2 text-xs text-dim">
              Find both IDs in Meta’s WhatsApp Manager → API setup. Create the token for a system user
              with the <span className="font-mono">whatsapp_business_messaging</span> and{' '}
              <span className="font-mono">whatsapp_business_management</span> permissions. It is stored
              encrypted and never shown again.
            </p>
            <div className="mt-4 flex justify-end">
              <Button disabled={busy || !waba || !pnid || token.length < 20 || !data.platform.token_key_ready}
                onClick={() => run(async () => {
                  const r = await connectWa({ waba_id: waba, phone_number_id: pnid, access_token: token })
                  setToken('')
                  if (r.webhook_error) toast(`Connected, but replies and receipts will not arrive: ${r.webhook_error}`, 'warn')
                }, 'WhatsApp number connected')}>
                <Link2 size={15} /> Verify and connect
              </Button>
            </div>
          </>
        )}
      </Card>
    )
  }

  const q = QUALITY[(a.quality_rating || '').toUpperCase()]
  const interakt = a.provider === 'interakt'
  return (
    <Card className="p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <span className={cx('inline-block h-2 w-2 rounded-full', a.status === 'connected' ? 'bg-mint-500' : 'bg-rose-500')} />
            <span className="text-sm font-semibold">{a.status === 'connected' ? 'Connected' : 'Needs attention'}</span>
            <Badge tone="neutral">{interakt ? 'via Interakt' : 'Meta Cloud API'}</Badge>
          </div>
          <div className="mt-2 font-display text-2xl font-bold tracking-tight">
            {a.display_phone || (interakt ? 'Interakt account' : a.phone_number_id)}
          </div>
          <div className="text-sm text-dim">
            {interakt ? 'API key verified · stored encrypted' : (a.verified_name || 'Business name pending review')}
          </div>
        </div>
        <div className="flex gap-2">
          <Button variant="ghost" size="sm" disabled={busy}
            onClick={() => run(refreshWa, interakt ? 'API key re-verified' : 'Number status and templates refreshed')}>
            <RefreshCw size={14} /> {interakt ? 'Re-verify key' : 'Refresh'}
          </Button>
          <Button variant="danger" size="sm" disabled={busy}
            onClick={async () => {
              if (await confirm({
                title: interakt ? 'Disconnect Interakt?' : 'Disconnect this WhatsApp number?',
                description: 'Queued messages stay queued and nothing new is sent until it is connected again. Playbooks and templates are kept.',
                confirmLabel: 'Disconnect', destructive: true,
              })) run(disconnectWa, 'WhatsApp disconnected')
            }}>
            <Unplug size={14} /> Disconnect
          </Button>
        </div>
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        {q && <Badge tone={q.tone}>{q.label}</Badge>}
        {a.messaging_limit && <Badge tone="neutral">Limit: {TIER[a.messaging_limit] ?? a.messaging_limit}</Badge>}
        <Badge tone="neutral">{data.templates} template{data.templates === 1 ? '' : 's'}</Badge>
        {a.checked_at && <span className="self-center text-2xs text-dim">Checked {fmtDateTime(a.checked_at)}</span>}
      </div>
      {a.last_error && <Hint tone="rose" className="mt-4">{a.last_error}</Hint>}
    </Card>
  )
}

function WebhookCard({ data }: { data: WaOverview }) {
  const a = data.account
  const { run, busy } = useRun()
  const [secret, setSecret] = useState('')
  const [editing, setEditing] = useState(!a.has_webhook_secret)
  return (
    <Card className="p-5">
      <SectionHead title="Delivery receipts and replies"
        sub="Interakt tells us when a message is sent, delivered, read or fails, and when a customer replies or says STOP." />
      <ol className="space-y-3 text-sm text-soft">
        <li className="flex gap-2">
          <span className="font-semibold text-dim">1.</span>
          <div className="min-w-0 flex-1">
            In Interakt open <span className="font-semibold">Settings → Developer Settings → Webhooks</span>, switch
            webhooks on and paste this URL:
            <div className="mt-1.5 flex items-center gap-2">
              <code className="min-w-0 flex-1 truncate rounded-md bg-[var(--surface-2)] px-2 py-1.5 text-xs ring-1 ring-[var(--border)]">{a.webhook_url}</code>
              {a.webhook_url && <CopyButton text={a.webhook_url} label="Copy webhook URL" />}
            </div>
          </div>
        </li>
        <li className="flex gap-2">
          <span className="font-semibold text-dim">2.</span>
          <div className="min-w-0 flex-1">
            Type a secret there and the same one here. Receipts are refused until the two match.
            {a.has_webhook_secret && !editing ? (
              <div className="mt-1.5 flex items-center gap-2">
                <Badge tone="mint">Secret set</Badge>
                <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>Replace</Button>
              </div>
            ) : (
              <div className="mt-1.5 flex flex-wrap items-center gap-2">
                <Input className="max-w-xs" type="password" autoComplete="off" value={secret}
                  onChange={(e) => setSecret(e.target.value)} placeholder="Webhook secret" />
                <Button size="sm" disabled={busy || secret.trim().length < 6}
                  onClick={() => run(async () => { await setWebhookSecret(secret); setSecret(''); setEditing(false) }, 'Webhook secret saved')}>
                  Save secret
                </Button>
                {a.has_webhook_secret && <Button variant="ghost" size="sm" onClick={() => setEditing(false)}>Cancel</Button>}
              </div>
            )}
          </div>
        </li>
        <li className="flex gap-2">
          <span className="font-semibold text-dim">3.</span>
          <div>Tick the <span className="font-mono text-xs">message_api_*</span> events and <span className="font-mono text-xs">message_received</span>. Messages send fine without this step — you just will not see delivered / read.</div>
        </li>
      </ol>
    </Card>
  )
}

function RulesCard({ data }: { data: WaOverview }) {
  const a = data.account
  const toast = useToast()
  const refresh = useRefresh()
  const [f, setF] = useState(() => pick(a))
  useEffect(() => { setF(pick(a)) }, [a.default_approval, a.send_window_start, a.send_window_end,
    a.send_delay_min, a.daily_cap, a.approval_ttl_h]) // eslint-disable-line react-hooks/exhaustive-deps
  const dirty = JSON.stringify(f) !== JSON.stringify(pick(a))
  const hours = Array.from({ length: 25 }, (_, h) => h)
  const [busy, setBusy] = useState(false)

  return (
    <Card className="p-5">
      <SectionHead title="Sending rules" sub="Apply to every playbook on this account." />
      <div className="space-y-4">
        <div>
          <div className="mb-1 text-xs font-semibold text-soft">New playbooks start as</div>
          <Segmented value={f.default_approval} onChange={(v) => setF({ ...f, default_approval: v })}
            options={[{ value: 'manual', label: 'Manual review' }, { value: 'auto', label: 'Automatic' }]} />
          <p className="mt-1.5 text-xs text-dim">Each playbook can still choose for itself.</p>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="Send from (IST)">
            <Select className="w-full" value={f.send_window_start}
              onChange={(e) => setF({ ...f, send_window_start: Number(e.target.value) })}>
              {hours.slice(0, 24).map((h) => <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>)}
            </Select>
          </Field>
          <Field label="Until (IST)">
            <Select className="w-full" value={f.send_window_end}
              onChange={(e) => setF({ ...f, send_window_end: Number(e.target.value) })}>
              {hours.slice(1).map((h) => <option key={h} value={h}>{String(h).padStart(2, '0')}:00</option>)}
            </Select>
          </Field>
          <Field label="Wait after the call (min)">
            <Input type="number" min={0} max={1440} value={f.send_delay_min}
              onChange={(e) => setF({ ...f, send_delay_min: Number(e.target.value) })} />
          </Field>
          <Field label="Most messages per day">
            <Input type="number" min={1} value={f.daily_cap}
              onChange={(e) => setF({ ...f, daily_cap: Number(e.target.value) })} />
          </Field>
        </div>
        <div className="max-w-xs">
          <Field label="Unapproved messages expire after (hours)">
            <Input type="number" min={1} max={168} value={f.approval_ttl_h}
              onChange={(e) => setF({ ...f, approval_ttl_h: Number(e.target.value) })} />
          </Field>
        </div>
        <p className="text-xs text-dim">
          A call that ends outside these hours sends at the next opening. A brochure approved two
          days late is worse than none, so messages nobody approves in time expire.
        </p>
      </div>
      <div className="mt-4 flex justify-end">
        <Button disabled={!dirty || busy || f.send_window_end <= f.send_window_start}
          onClick={async () => {
            setBusy(true)
            try {
              await saveWaSettings(f)
              toast('Sending rules saved', 'success')
              refresh(['wa'])
            } catch (e: any) {
              toast(e?.reason || e?.message, 'error')
            } finally {
              setBusy(false)
            }
          }}>Save rules</Button>
      </div>
    </Card>
  )
}

function pick(a: WaOverview['account']) {
  return {
    default_approval: a.default_approval, send_window_start: a.send_window_start,
    send_window_end: a.send_window_end, send_delay_min: a.send_delay_min,
    daily_cap: a.daily_cap, approval_ttl_h: a.approval_ttl_h,
  }
}

function Step({ done, title, children }: { done: boolean; title: string; children: React.ReactNode }) {
  return (
    <li className="flex gap-3">
      {done
        ? <CheckCircle2 size={18} className="mt-0.5 shrink-0 text-mint-600 dark:text-mint-400" />
        : <Circle size={18} className="mt-0.5 shrink-0 text-dim" />}
      <div>
        <div className="text-sm font-semibold">{title}</div>
        <div className="text-xs text-dim">{children}</div>
      </div>
    </li>
  )
}

function HowItWorks({ data }: { data: WaOverview }) {
  const connected = isConnected(data.account)
  const interakt = data.account.provider === 'interakt'
  return (
    <Card className="p-5">
      <SectionHead title="How it works" />
      <ol className="space-y-4">
        <Step done={connected} title="1. Connect your number">
          {interakt ? 'Your Interakt API key connects the WhatsApp Business number managed there.' : 'Your verified WhatsApp Business number sends the messages.'}
        </Step>
        <Step done={data.templates > 0} title="2. Get a template approved">
          {interakt
            ? 'Create the message in Interakt → Templates and wait for Meta’s approval, then add it in the Templates tab with the same name and text.'
            : 'WhatsApp only lets a business message someone first with a message Meta has approved.'}
        </Step>
        <Step done={data.playbooks > 0} title="3. Write a playbook">
          Pick the agents, the template, the brochure for each project, and how names fill in.
        </Step>
        <Step done={(data.counts.sent ?? 0) + (data.counts.delivered ?? 0) + (data.counts.read ?? 0) > 0}
          title="4. Callers who agree get it">
          When a caller says yes on the call, the message is queued. It sends automatically, or
          waits in Approvals.
        </Step>
      </ol>
    </Card>
  )
}

function PlatformCard({ data }: { data: WaOverview }) {
  const p = data.platform
  const a = data.account
  const interakt = a.provider === 'interakt'
  const rows: [boolean, string][] = [
    [p.token_key_ready, 'Keys and tokens can be stored encrypted'],
    [interakt ? a.has_webhook_secret : p.webhook_ready, 'Delivery receipts, replies and STOP are received'],
    [p.send_enabled, 'Sending is switched on for the platform'],
  ]
  return (
    <Card className="p-5">
      <SectionHead title="Platform" sub="Set up by Namaskall." />
      <ul className="space-y-2">
        {rows.map(([ok, label]) => (
          <li key={label} className="flex items-center gap-2 text-sm">
            {ok ? <CheckCircle2 size={15} className="text-mint-600 dark:text-mint-400" /> : <Circle size={15} className="text-dim" />}
            <span className={ok ? 'text-soft' : 'text-dim'}>{label}</span>
          </li>
        ))}
        {p.canary && <li><Badge tone="amber">Test mode — only test numbers receive messages</Badge></li>}
      </ul>
      {!interakt && (
        <div className="mt-4">
          <div className="mb-1 text-xs font-semibold text-soft">Webhook URL (for Meta)</div>
          <div className="flex items-center gap-2">
            <code className="min-w-0 flex-1 truncate rounded-md bg-[var(--surface-2)] px-2 py-1.5 text-xs ring-1 ring-[var(--border)]">{p.webhook_url}</code>
            <CopyButton text={p.webhook_url} label="Copy webhook URL" />
          </div>
        </div>
      )}
    </Card>
  )
}
