import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertTriangle, ArrowDownLeft, ArrowUpRight, BookOpen, KeyRound, Plus,
  ShieldCheck, Sparkles, Trash2,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import { del, post } from '../api/mutate'
import { Badge, Button, Card, IconButton, SectionHead, Spinner } from '../components/ui'
import { AsyncState, Field, Input, PageHeader, Table, TD, TH, THead, TR, Textarea } from '../components/kit'
import {
  Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle,
} from '../components/ui/dialog'
import { CopyButton, Snippet } from '../components/copy'
import { useConfirm } from '../components/confirm'
import { useToast } from '../components/Toast'

/**
 * The integration hub — keys, limits, and the way in to the reference.
 *
 * This used to be a tab inside Settings with the entire API manual stacked
 * underneath it. Two things were wrong with that: a customer's engineer had to
 * go through an *account* page to reach the thing they work in all day, and the
 * reference had nowhere to breathe, so it read as a wall.
 *
 * The page opens with the two directions data actually moves, because that is
 * the product's whole proposition and it is answerable with live numbers from
 * this account rather than a marketing sentence.
 */

type ApiKey = {
  id: number
  key_id: string
  name: string
  masked: string
  scopes: string[]
  ip_allowlist: { cidr: string; label?: string }[]
  status: string
  expired: boolean
  last_used_at: string | null
  last_used_ip: string | null
  request_count: number
  usage: { minute: number; day: number }
  token?: string
}

type Payload = {
  available: boolean
  reason?: string
  keys: ApiKey[]
  scopes: Record<string, string>
  default_scopes: string[]
  base_url: string
  plan_allowed: boolean
  plan_required: string
  upgrade_url: string
  policy: {
    enabled: boolean
    rate_per_min: number
    rate_per_day: number
    max_keys: number
    ip_restricted: boolean
    ip_allowlist: { cidr: string; label?: string }[]
    note: string
  }
  usage_by_day: { day: string; hits: number }[]
  active_count: number
}

const EXPIRY_CHOICES = [
  { value: '', label: 'Never expires' },
  { value: '90', label: '90 days' },
  { value: '365', label: '1 year' },
]

function when(value: string | null) {
  if (!value) return 'Never used'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString(undefined, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })
}

/** The two directions data moves. This is the page's thesis, so it carries real
 *  counts from this account rather than illustrative ones. */
function FlowLane({ dir, title, detail, examples }: {
  dir: 'in' | 'out'; title: string; detail: string; examples: string[]
}) {
  const Icon = dir === 'in' ? ArrowDownLeft : ArrowUpRight
  const tint = dir === 'in'
    ? 'text-indigo-600 dark:text-indigo-300 bg-indigo-500/10 ring-indigo-500/20'
    : 'text-mint-700 dark:text-mint-300 bg-mint-500/10 ring-mint-500/20'
  return (
    <div className="flex gap-3.5">
      <div className={`grid h-9 w-9 shrink-0 place-items-center rounded-full ring-1 ${tint}`}>
        <Icon size={16} />
      </div>
      <div className="min-w-0">
        <div className="text-sm font-semibold">{title}</div>
        <p className="mt-0.5 text-xs text-dim">{detail}</p>
        <div className="mt-2 flex flex-wrap gap-1">
          {examples.map((e) => (
            <span key={e} className="rounded-full bg-[var(--surface-2)] px-2 py-0.5 font-mono text-2xs text-soft">
              {e}
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}

export default function Developers() {
  const { data, isLoading, error } = useGet<Payload>(['apikeys'], '/apikeys')
  const refresh = useRefresh()
  const toast = useToast()
  const confirm = useConfirm()

  const [creating, setCreating] = useState(false)
  const [busy, setBusy] = useState(false)
  const [name, setName] = useState('')
  const [expiry, setExpiry] = useState('')
  const [ips, setIps] = useState('')
  const [scopes, setScopes] = useState<string[]>([])
  const [minted, setMinted] = useState<ApiKey | null>(null)

  const base = data?.base_url || ''
  const live = useMemo(() => (data?.keys || []).filter((k) => k.status === 'active'), [data])
  const atCap = !!data && live.length >= (data.policy?.max_keys ?? 0)
  // True only once platform billing enforcement is on; invisible until then.
  const planLocked = !!data && data.available && data.plan_allowed === false
  const todayCalls = data?.usage_by_day?.[0]?.hits ?? 0

  const openCreate = () => {
    setName(''); setExpiry(''); setIps('')
    setScopes(data?.default_scopes || [])
    setCreating(true)
  }

  const create = async () => {
    setBusy(true)
    try {
      const key = await post<ApiKey>('/apikeys', {
        name: name.trim() || 'API key',
        scopes,
        expires_in_days: expiry ? Number(expiry) : null,
        ip_allowlist: ips.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
      })
      setCreating(false)
      setMinted(key)
      refresh(['apikeys'])
    } catch (e: any) {
      toast(e.reason || e.message || 'Could not create the key', 'error')
    } finally {
      setBusy(false)
    }
  }

  const revoke = async (key: ApiKey) => {
    const ok = await confirm({
      title: `Revoke “${key.name}”?`,
      description: 'Any system using this key stops working immediately. Keys cannot be '
        + 'restored — issue a new one and update the integration first.',
      destructive: true,
      confirmLabel: 'Revoke key',
    })
    if (!ok) return
    try {
      await del(`/apikeys/${key.id}`)
      toast(`“${key.name}” revoked`, 'success')
      refresh(['apikeys'])
    } catch (e: any) {
      toast(e.reason || e.message || 'Could not revoke the key', 'error')
    }
  }

  const sampleToken = minted?.token || 'lk_live_YOUR_KEY'

  return (
    <div className="space-y-5">
      <PageHeader
        title="Developers"
        sub="Connect Namaskall to the systems your team already works in"
        actions={
          <Link to="/developers/reference">
            <Button variant="ghost"><BookOpen size={14} /> API reference</Button>
          </Link>
        }
      />

      <AsyncState loading={isLoading} error={error} variant="rows" rows={5}>
        {data && !data.available ? (
          <Card className="p-5">
            <div className="flex items-start gap-3">
              <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-500" />
              <div>
                <div className="text-sm font-semibold">The API is not available on this instance</div>
                <p className="mt-1 text-xs text-dim">{data.reason}</p>
              </div>
            </div>
          </Card>
        ) : data ? (
          <>
            {/* ------------------------------------------------ the thesis */}
            <Card className="p-5">
              <div className="grid gap-6 sm:grid-cols-2">
                <FlowLane
                  dir="in" title="Your systems push in"
                  detail="Leads from a web form, a CRM sync, a nightly export — one at a time or 500 in a request."
                  examples={['POST /v1/contacts', 'POST /v1/contacts/bulk']}
                />
                <FlowLane
                  dir="out" title="Namaskall hands back"
                  detail="Every call the agent handled: what was said, what the AI made of it, and the audio."
                  examples={['GET /v1/calls', 'summary + scores', 'transcript', 'recording']}
                />
              </div>
              <div className="mt-5 flex flex-wrap items-center gap-x-6 gap-y-2 border-t border-[var(--border)] pt-3.5 text-xs">
                <span className="text-dim">Base URL</span>
                <span className="flex items-center gap-1 font-mono">
                  {base}/v1 <CopyButton text={`${base}/v1`} label="Copy the base URL" />
                </span>
                <span className="text-dim">·</span>
                <span className="text-dim">
                  <span className="font-semibold text-[var(--text)] tabular-nums">{live.length}</span> active
                  {live.length === 1 ? ' key' : ' keys'}
                </span>
                <span className="text-dim">
                  <span className="font-semibold text-[var(--text)] tabular-nums">{todayCalls.toLocaleString()}</span> requests today
                </span>
              </div>
            </Card>

            {planLocked && (
              <Card className="p-4 ring-1 ring-indigo-500/20">
                <div className="flex flex-wrap items-center gap-3">
                  <Sparkles size={16} className="shrink-0 text-indigo-500 dark:text-indigo-300" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold">
                      The API is included from {data.plan_required.replace(/^./, (c) => c.toUpperCase())}
                    </div>
                    <p className="mt-0.5 text-xs text-dim">
                      Upgrade to push contacts in and export call data. Existing keys keep
                      working until your current period ends.
                    </p>
                  </div>
                  <Link to={data.upgrade_url || '/billing'}><Button>View plans</Button></Link>
                </div>
              </Card>
            )}

            {/* --------------------------------------------------- the keys */}
            <Card className="p-5">
              <SectionHead
                title="Keys"
                sub={`${live.length} of ${data.policy.max_keys} in use · the secret is shown once, when you create it`}
                right={
                  <Button onClick={openCreate}
                    disabled={atCap || planLocked || !data.policy.enabled}
                    title={planLocked ? `Included from ${data.plan_required}`
                      : atCap ? 'Revoke a key before creating another' : undefined}>
                    <Plus size={14} /> Create key
                  </Button>
                }
              />

              {!data.policy.enabled && (
                <div className="mb-3 flex items-start gap-2 rounded-md bg-rose-500/10 px-3 py-2 text-xs text-rose-600 ring-1 ring-rose-500/20 dark:text-rose-300">
                  <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                  <span>API access is switched off for this account. Existing keys are
                    refused until it is re-enabled — contact support.</span>
                </div>
              )}

              {data.keys.length === 0 ? (
                <div className="rounded-2xl border border-dashed border-[var(--border)] px-4 py-10 text-center">
                  <KeyRound size={20} className="mx-auto mb-2 text-dim" />
                  <div className="text-sm font-semibold">Create your first key</div>
                  <p className="mx-auto mt-1 max-w-sm text-xs text-dim">
                    A key is how your CRM identifies itself. Give each system its own —
                    that way you can turn one off without touching the others.
                  </p>
                  {!planLocked && data.policy.enabled && (
                    <Button className="mt-4" onClick={openCreate}><Plus size={14} /> Create key</Button>
                  )}
                </div>
              ) : (
                <Table>
                  <THead>
                    <TH>Key</TH><TH>Permissions</TH><TH>Last used</TH>
                    <TH className="text-right">Requests</TH><TH /></THead>
                  <tbody>
                    {data.keys.map((k) => (
                      <TR key={k.id}>
                        <TD>
                          <div className="flex items-center gap-2">
                            <span className="font-semibold">{k.name}</span>
                            {k.status !== 'active'
                              ? <Badge tone="rose" soft>revoked</Badge>
                              : k.expired
                                ? <Badge tone="amber" soft>expired</Badge>
                                : <Badge tone="mint" soft>active</Badge>}
                          </div>
                          <div className="mt-0.5 font-mono text-2xs text-dim">{k.masked}</div>
                          {k.ip_allowlist.length > 0 && (
                            <div className="mt-0.5 text-2xs text-dim">
                              only from {k.ip_allowlist.map((i) => i.cidr).join(', ')}
                            </div>
                          )}
                        </TD>
                        <TD>
                          <div className="flex flex-wrap gap-1">
                            {k.scopes.map((s) => (
                              <span key={s} className="rounded-full bg-[var(--surface-2)] px-1.5 py-0.5 font-mono text-2xs text-soft">
                                {s}
                              </span>
                            ))}
                          </div>
                        </TD>
                        <TD>
                          <div className="text-xs">{when(k.last_used_at)}</div>
                          {k.last_used_ip && <div className="text-2xs text-dim">{k.last_used_ip}</div>}
                        </TD>
                        <TD className="text-right tabular-nums">
                          <div className="text-xs">{k.request_count.toLocaleString()}</div>
                          <div className="text-2xs text-dim">{k.usage.day} today</div>
                        </TD>
                        <TD className="text-right">
                          {k.status === 'active' && (
                            <IconButton label={`Revoke ${k.name}`} onClick={() => revoke(k)}>
                              <Trash2 size={14} className="text-rose-500" />
                            </IconButton>
                          )}
                        </TD>
                      </TR>
                    ))}
                  </tbody>
                </Table>
              )}
            </Card>

            {/* ------------------------------------------------- start here */}
            <Card className="p-5">
              <SectionHead title="Start here"
                sub="Three requests that prove the connection, in order" />
              <div className="grid gap-3 lg:grid-cols-3">
                <Snippet label="1 · Check the key works" code={`curl ${base}/v1/me \\
  -H "Authorization: Bearer ${sampleToken}"`} />
                <Snippet label="2 · Push a contact" code={`curl -X POST ${base}/v1/contacts \\
  -H "Authorization: Bearer ${sampleToken}" \\
  -H "Content-Type: application/json" \\
  -d '{"phone":"+919876543210","name":"Ramesh Patel"}'`} />
                <Snippet label="3 · Pull new calls" code={`curl "${base}/v1/calls?order=asc&limit=100" \\
  -H "Authorization: Bearer ${sampleToken}"
# keep next_cursor, pass it back next time`} />
              </div>
            </Card>

            {/* ------------------------------------------------ limits + use */}
            <div className="grid gap-4 lg:grid-cols-2">
              <Card className="p-5">
                <SectionHead title="Your limits" sub="Set by Namaskall for this account" />
                <dl className="space-y-2.5 text-sm">
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="text-dim">Requests</dt>
                    <dd className="tabular-nums">
                      {data.policy.rate_per_min.toLocaleString()}/min ·{' '}
                      {data.policy.rate_per_day.toLocaleString()}/day
                    </dd>
                  </div>
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="text-dim">Shared across</dt>
                    <dd>all keys on this account</dd>
                  </div>
                  <div className="flex items-baseline justify-between gap-3">
                    <dt className="text-dim">Allowed from</dt>
                    <dd className="text-right">
                      {data.policy.ip_restricted ? (
                        <div className="space-y-0.5">
                          {data.policy.ip_allowlist.map((i) => (
                            <div key={i.cidr} className="font-mono text-xs">
                              {i.cidr}
                              {i.label ? <span className="ml-1 font-sans text-dim">{i.label}</span> : null}
                            </div>
                          ))}
                        </div>
                      ) : <span className="text-dim">any address</span>}
                    </dd>
                  </div>
                </dl>
                {data.policy.note && (
                  <p className="mt-3 flex items-start gap-2 rounded-md bg-[var(--surface-2)] px-3 py-2 text-xs text-soft">
                    <ShieldCheck size={13} className="mt-0.5 shrink-0 text-mint-600 dark:text-mint-300" />
                    {data.policy.note}
                  </p>
                )}
                {data.policy.ip_restricted && (
                  <p className="mt-2 text-xs text-dim">
                    Requests from any other address are refused. Ask us to add a server —
                    <code className="mx-1">GET /v1/me</code> reports the address we see.
                  </p>
                )}
              </Card>

              <Card className="p-5">
                <SectionHead title="Requests" sub="Last 14 days, all keys" />
                {data.usage_by_day.length === 0 ? (
                  <p className="py-8 text-center text-xs text-dim">
                    Nothing yet. Your first request will show up here.
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {data.usage_by_day.map((d) => {
                      const peak = Math.max(...data.usage_by_day.map((x) => x.hits), 1)
                      return (
                        <li key={d.day} className="flex items-center gap-2 text-xs">
                          <span className="w-20 shrink-0 text-dim">{d.day}</span>
                          <span className="h-1.5 flex-1 overflow-hidden rounded-full bg-[var(--surface-2)]">
                            <span className="block h-full rounded-full bg-mint-500"
                              style={{ width: `${Math.round((d.hits / peak) * 100)}%` }} />
                          </span>
                          <span className="w-14 shrink-0 text-right tabular-nums">
                            {d.hits.toLocaleString()}
                          </span>
                        </li>
                      )
                    })}
                  </ul>
                )}
              </Card>
            </div>

            {/* -------------------------------------------------- reference */}
            <Card className="p-5">
              <div className="flex flex-wrap items-center justify-between gap-4">
                <div>
                  <div className="font-display text-base font-bold tracking-tight">
                    The full reference
                  </div>
                  <p className="mt-1 max-w-xl text-sm text-soft">
                    Every endpoint and field, the sync loop, and what each error means —
                    with this account’s own limits filled in. Printable, so you can send
                    it to whoever is building the integration.
                  </p>
                </div>
                <Link to="/developers/reference">
                  <Button><BookOpen size={14} /> Open the reference</Button>
                </Link>
              </div>
            </Card>
          </>
        ) : null}
      </AsyncState>

      {/* ----------------------------------------------------- create modal */}
      <Dialog open={creating} onOpenChange={(o) => { if (!o) setCreating(false) }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader><DialogTitle>Create a key</DialogTitle></DialogHeader>
          <div className="space-y-4 px-1">
            <Field label="Which system will use this key?">
              <Input value={name} onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Salesforce production" autoFocus />
            </Field>

            <div>
              <div className="mb-1.5 text-xs font-medium text-dim">
                What it may do — grant only what that system needs
              </div>
              <div className="space-y-1">
                {Object.entries(data?.scopes || {}).map(([scope, description]) => (
                  <label key={scope}
                    className="flex cursor-pointer items-start gap-2.5 rounded-md px-2 py-1.5 hover:bg-[var(--surface-2)]">
                    <input type="checkbox" className="mt-0.5 accent-current"
                      checked={scopes.includes(scope)}
                      onChange={(e) => setScopes((prev) => (
                        e.target.checked ? [...prev, scope] : prev.filter((s) => s !== scope)
                      ))} />
                    <span>
                      <span className="text-sm">{description}</span>
                      <span className="block font-mono text-2xs text-dim">{scope}</span>
                    </span>
                  </label>
                ))}
              </div>
            </div>

            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="Expiry">
                <select value={expiry} onChange={(e) => setExpiry(e.target.value)}
                  className="w-full rounded-md bg-[var(--surface-2)] px-3 py-2 text-sm ring-1 ring-[var(--border)]">
                  {EXPIRY_CHOICES.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
                </select>
              </Field>
              <Field label="Restrict to these IPs (optional)">
                <Textarea rows={2} value={ips} onChange={(e) => setIps(e.target.value)}
                  placeholder="203.0.113.9" />
              </Field>
            </div>
            <p className="text-xs text-dim">
              One address or CIDR block per line. This narrows the key further than your
              account’s own restriction — it cannot widen it.
            </p>
          </div>
          <DialogFooter className="gap-2">
            <Button variant="ghost" onClick={() => setCreating(false)}>Cancel</Button>
            <Button onClick={create} disabled={busy || scopes.length === 0}>
              {busy ? <Spinner className="h-3.5 w-3.5" /> : <KeyRound size={14} />}
              {busy ? 'Creating…' : 'Create key'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* ------------------------------------------------ reveal-once modal */}
      <Dialog open={!!minted} onOpenChange={(o) => { if (!o) setMinted(null) }}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader><DialogTitle>Copy your key now</DialogTitle></DialogHeader>
          <div className="space-y-3 px-1">
            <div className="flex items-start gap-2 rounded-md bg-amber-500/10 px-3 py-2 text-xs text-amber-700 ring-1 ring-amber-500/20 dark:text-amber-300">
              <AlertTriangle size={13} className="mt-0.5 shrink-0" />
              <span>This is the only time the full key is shown. We store a hash, not the
                key itself — if you lose it, create a new one and revoke this.</span>
            </div>
            <div className="flex items-center gap-2 rounded-md bg-[var(--surface-2)] p-2 ring-1 ring-[var(--border)]">
              <code className="min-w-0 flex-1 break-all font-mono text-xs">{minted?.token}</code>
              <CopyButton text={minted?.token || ''} label="Copy the API key" />
            </div>
            <p className="text-xs text-dim">
              Send it on every request as
              <code className="mx-1">Authorization: Bearer …</code>
              (or <code className="mx-1">X-API-Key</code> if your system cannot set that header).
            </p>
          </div>
          <DialogFooter>
            <Button onClick={() => setMinted(null)}>I’ve stored it safely</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
