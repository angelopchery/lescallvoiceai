import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle, Check, CreditCard, Download, FileText, Loader2,
  RefreshCw, Sparkles,
} from 'lucide-react'
import { useGet, useRefresh } from '../api/hooks/useApi'
import {
  Attempt, BillingProfile, BillingSummary, CheckoutSession, Payment, Plan,
  cancelSubscription, changePlan, inr, loadCheckoutScript, openCheckout,
  reconcile, saveProfile, startCheckout,
} from '../api/billing'
import { Badge, Button, Card, Spinner } from '../components/ui'
import { AsyncState, Field, Input, PageHeader, Table, TD, TH, THead, TR } from '../components/kit'
import { useToast } from '../components/Toast'
import { useConfirm } from '../components/confirm'
import { fmtDateTime, parseTs } from '../lib/format'

/* ── usage meter ───────────────────────────────────────────────────────────
   Amber at 80%, rose at 100% — the same thresholds the Overview chip uses, so
   a user never sees "you're fine" on one page and a block on another. */
function meterTone(used: number, limit: number | null): 'mint' | 'amber' | 'rose' {
  if (limit == null) return 'mint'
  const pct = limit > 0 ? used / limit : 1
  return pct >= 1 ? 'rose' : pct >= 0.8 ? 'amber' : 'mint'
}

function Meter({ used, limit, label }: { used: number; limit: number | null; label: string }) {
  const tone = meterTone(used, limit)
  const pct = limit == null ? 0 : Math.min(100, limit > 0 ? (used / limit) * 100 : 100)
  const bar = tone === 'rose' ? 'bg-rose-500' : tone === 'amber' ? 'bg-amber-500' : 'bg-emerald-500'
  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-xs text-[var(--muted)]">{label}</span>
        <span className="text-sm font-semibold tabular-nums">
          {used.toLocaleString('en-IN')}
          <span className="text-[var(--muted)] font-normal">
            {' / '}{limit == null ? 'unlimited' : limit.toLocaleString('en-IN')}
          </span>
        </span>
      </div>
      {limit != null && (
        <div className="mt-1.5 h-2 w-full overflow-hidden rounded-full bg-[var(--ui-border)]">
          <div className={`h-full rounded-full transition-all ${bar}`} style={{ width: `${pct}%` }} />
        </div>
      )}
    </div>
  )
}

/* An invoice raised outside the gateway. `due` means we have extended credit —
   the plan is already active and the customer owes the balance. */
const PAYMENT_TONE: Record<string, 'mint' | 'amber' | 'rose' | 'neutral'> = {
  captured: 'mint', refunded: 'amber', due: 'amber', void: 'neutral', failed: 'rose',
}
const PAYMENT_LABEL: Record<string, string> = {
  captured: 'paid', refunded: 'refunded', due: 'payment due',
  void: 'cancelled', failed: 'failed',
}
const METHOD_LABEL: Record<string, string> = {
  cash: 'Cash', neft_rtgs: 'NEFT / RTGS', imps: 'IMPS', upi: 'UPI',
  cheque: 'Cheque', bank_transfer: 'Bank transfer', other: 'Other',
}

const BANNER_CLASS: Record<string, string> = {
  rose: 'border-rose-300 bg-rose-50 text-rose-900 dark:border-rose-900/60 dark:bg-rose-950/40 dark:text-rose-200',
  amber: 'border-amber-300 bg-amber-50 text-amber-900 dark:border-amber-900/60 dark:bg-amber-950/40 dark:text-amber-200',
  sky: 'border-sky-300 bg-sky-50 text-sky-900 dark:border-sky-900/60 dark:bg-sky-950/40 dark:text-sky-200',
  slate: 'border-[var(--ui-border)] bg-[var(--surface)] text-[var(--fg)]',
}

export default function Billing() {
  const summary = useGet<BillingSummary>(['billing', 'summary'], '/billing/summary')
  const plansQ = useGet<{ plans: Plan[] }>(['billing', 'plans'], '/billing/plans')
  const paymentsQ = useGet<{
    payments: Payment[]; total: number
    outstanding_paise: number; outstanding_count: number
  }>(['billing', 'payments'], '/billing/payments?limit=25')
  const attemptsQ = useGet<{ attempts: Attempt[] }>(['billing', 'attempts'], '/billing/attempts?limit=10')
  const profileQ = useGet<{ profile: BillingProfile; platform_ready: boolean }>(
    ['billing', 'profile'], '/billing/profile')

  const refresh = useRefresh()
  const toast = useToast()
  const confirm = useConfirm()
  const [busy, setBusy] = useState('')
  const s = summary.data

  const refreshAll = () => {
    refresh(['billing', 'summary']); refresh(['billing', 'payments'])
    refresh(['billing', 'plans']); refresh(['billing', 'attempts'])
  }

  /* Poll /summary briefly after a purchase: the plan flips when the WEBHOOK
     lands, which is usually a second or two after the modal closes — not at
     the moment `verify` returns. Without this the page shows the old plan and
     the user presses Buy again. */
  const [pollUntil, setPollUntil] = useState(0)
  useEffect(() => {
    if (!pollUntil) return
    const id = setInterval(() => {
      if (Date.now() > pollUntil) { setPollUntil(0); return }
      refresh(['billing', 'summary'])
    }, 2000)
    return () => clearInterval(id)
  }, [pollUntil, refresh])

  const runCheckout = async (session: CheckoutSession, what: string) => {
    if (!(await loadCheckoutScript())) {
      toast('Could not load the payment window. Check your connection or any '
        + 'ad blocker, then try again.', 'error')
      return
    }
    openCheckout(session, {
      onSuccess: (r) => {
        toast(r?.status === 'activated' ? `${what} activated.`
          : 'Payment received — finishing up.', 'success')
        setPollUntil(Date.now() + 30_000)
        refreshAll()
      },
      onDismiss: () => toast('Payment window closed. Nothing has been charged.', 'info'),
      onError: (e: any) => toast(
        e?.reason || e?.message || 'We could not confirm that payment. If money '
        + 'has left your account, use "I paid but nothing happened" below.', 'error'),
    })
  }

  const buy = async (plan: Plan) => {
    setBusy(plan.code)
    try {
      const hasPlan = s?.subscription && ['active', 'authenticated', 'pending'].includes(s.subscription.status)
      const session = hasPlan && plan.period === 'monthly'
        ? await changePlan(plan.code)
        : await startCheckout(plan.code)
      await runCheckout(session, plan.name)
    } catch (e: any) {
      toast(e?.reason || e?.message || 'Could not start checkout', 'error')
    } finally { setBusy('') }
  }

  const doCancel = async () => {
    const ok = await confirm({
      title: 'Cancel subscription?',
      description: 'Your plan stays active until the end of the current billing '
        + 'period, then the account moves to Free. Your agents and call history '
        + 'are kept.',
      confirmLabel: 'Cancel subscription', destructive: true,
    })
    if (!ok) return
    setBusy('cancel')
    try {
      const r = await cancelSubscription()
      toast(`Cancelled. Active until ${(r.active_until || '').slice(0, 10)}.`, 'success')
      refreshAll()
    } catch (e: any) { toast(e?.reason || e?.message || 'Could not cancel', 'error') }
    finally { setBusy('') }
  }

  const doReconcile = async () => {
    setBusy('reconcile')
    try {
      const r = await reconcile()
      toast(r.message, r.recovered ? 'success' : 'info')
      refreshAll()
    } catch (e: any) { toast(e?.reason || e?.message || 'Could not recheck', 'error') }
    finally { setBusy('') }
  }

  const trial = s?.subscription?.kind === 'one_time' && s.subscription.status === 'active'
  const daysLeft = useMemo(() => {
    const end = s?.subscription?.current_end
    const at = parseTs(end)
    if (!at) return null
    return Math.max(0, Math.ceil((at.getTime() - Date.now()) / 86_400_000))
  }, [s?.subscription?.current_end])

  return (
    <div className="space-y-5">
      <PageHeader
        title="Billing"
        sub="Your plan, minutes and invoices."
        actions={
          <Button variant="ghost" onClick={refreshAll}>
            <RefreshCw className="h-4 w-4" /> Refresh
          </Button>
        }
      />

      {/* Gateway not configured yet — say so plainly rather than letting the
          user press Buy and get a 503. */}
      {s && !s.gateway.configured && (
        <div className={`rounded-2xl border px-4 py-3 text-sm ${BANNER_CLASS.amber}`}>
          <AlertTriangle className="mr-2 inline h-4 w-4" />
          Online payments are not switched on yet. Your current plan and minutes
          work normally — contact us to arrange a plan.
        </div>
      )}

      {!!paymentsQ.data?.outstanding_paise && (
        <div className={`flex flex-wrap items-center gap-3 rounded-2xl border px-4 py-3 text-sm ${BANNER_CLASS.amber}`}>
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span className="flex-1">
            <b>{inr(paymentsQ.data.outstanding_paise, { paise: true })}</b> is
            outstanding across {paymentsQ.data.outstanding_count} invoice
            {paymentsQ.data.outstanding_count === 1 ? '' : 's'}. Your plan is
            active — please settle the balance with your account manager.
          </span>
        </div>
      )}

      {s?.banner && (
        <div className={`rounded-2xl border px-4 py-3 text-sm ${BANNER_CLASS[s.banner.tone] || BANNER_CLASS.slate}`}>
          {s.banner.text}
        </div>
      )}

      {/* Unresolved payment recovery. The single most common billing support
          ticket is "I paid and nothing happened" — a button beats an email. */}
      {s && s.unresolved_payments > 0 && (
        <div className={`flex flex-wrap items-center gap-3 rounded-2xl border px-4 py-3 text-sm ${BANNER_CLASS.amber}`}>
          <AlertTriangle className="h-4 w-4 shrink-0" />
          <span className="flex-1">
            You have {s.unresolved_payments} payment{s.unresolved_payments === 1 ? '' : 's'} we
            have not been able to confirm. If money left your account, we can recheck now.
          </span>
          <Button onClick={doReconcile} disabled={busy === 'reconcile'}>
            {busy === 'reconcile' ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
            I paid but nothing happened
          </Button>
        </div>
      )}

      <AsyncState loading={summary.isLoading} error={summary.error} variant="cards" rows={2}>
        {s && (
          <div className="grid gap-4 lg:grid-cols-3">
            {/* Current plan + usage */}
            <Card className="p-5 lg:col-span-2">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div>
                  <div className="flex items-center gap-2">
                    <h2 className="text-xl font-semibold">{s.plan_name}</h2>
                    {s.status !== 'ok' && (
                      <Badge tone={s.status === 'locked' ? 'rose' : 'amber'}>{s.status}</Badge>
                    )}
                    {trial && <Badge tone="indigo">Trial</Badge>}
                  </div>
                  <p className="mt-1 text-xs text-[var(--muted)]">
                    {s.period_start?.slice(0, 10)} — {s.period_end?.slice(0, 10)}
                    {trial && daysLeft != null && ` · ${daysLeft} day${daysLeft === 1 ? '' : 's'} left`}
                  </p>
                </div>
                {s.subscription && ['active', 'authenticated', 'pending'].includes(s.subscription.status)
                  && !s.subscription.cancel_at_cycle_end && s.subscription.kind === 'subscription' && (
                  <Button variant="ghost" onClick={doCancel} disabled={busy === 'cancel'}>Cancel plan</Button>
                )}
              </div>

              <div className="mt-5 grid gap-4 sm:grid-cols-2">
                <Meter used={s.minutes_used} limit={s.minutes_limit} label="Minutes this period" />
                <Meter used={s.agents_used ?? 0} limit={s.max_agent_families} label="Agents" />
                {/* The AI-assistant allowance is spent by the Copilot's
                    server-side tool loop, so a user has no way to feel it
                    being consumed — which makes a meter the only honest way to
                    show it. Without one, the first sign of the budget is a
                    refusal mid-build. Resets at midnight IST, not with the
                    billing period, so it says so. */}
                {s.copilot_tokens_per_day != null && (
                  <Meter used={s.copilot_tokens_used ?? 0}
                         limit={s.copilot_tokens_per_day}
                         label="AI assistant today (resets midnight IST)" />
                )}
              </div>

              {/* Additional minutes are not sold. Running out offers exactly
                  one route, so that is what the page says — no top-up control,
                  and no copy implying one exists. */}
              {s.minutes_limit != null && s.minutes_remaining === 0 && (
                <div className="mt-4 flex flex-wrap items-center justify-between gap-3 border-t border-[var(--ui-border)] pt-4">
                  <span className="text-sm">
                    You have used this period&rsquo;s minutes.{' '}
                    <span className="text-[var(--muted)]">
                      Your allowance resets on {s.period_end?.slice(0, 10)}.
                    </span>
                  </span>
                  <Button onClick={() => document.getElementById('plans')?.scrollIntoView({ behavior: 'smooth' })}>
                    Upgrade plan
                  </Button>
                </div>
              )}
            </Card>

            {/* What the plan includes */}
            <Card className="p-5">
              <h3 className="text-sm font-semibold">Included in {s.plan_name}</h3>
              <ul className="mt-3 space-y-1.5 text-sm">
                {([
                  ['Call recordings', s.features.recordings],
                  ['AI summaries', s.features.summaries],
                  ['Call transcripts', s.features.transcripts],
                  ['Intelligence dashboard', s.features.intelligence],
                  ['Conversation analytics', s.features.conversation_analytics],
                  ['Advanced analytics', s.features.advanced_analytics],
                  ['Automated follow-up calls', s.features.autocall],
                ] as [string, boolean][]).map(([label, on]) => (
                  <li key={label} className={on ? '' : 'text-[var(--muted)]'}>
                    <Check className={`mr-2 inline h-3.5 w-3.5 ${on ? 'text-emerald-500' : 'opacity-20'}`} />
                    {label}
                  </li>
                ))}
              </ul>
              <p className="mt-4 text-xs text-[var(--muted)]">
                Up to {s.max_concurrent_calls ?? '∞'} simultaneous calls.
              </p>
            </Card>
          </div>
        )}
      </AsyncState>

      {/* Plans */}
      <Card className="p-5" >
        <h2 id="plans" className="text-lg font-semibold">Plans</h2>
        <p className="mt-1 text-xs text-[var(--muted)]">All prices exclude GST (18% added at checkout).</p>
        <AsyncState loading={plansQ.isLoading} error={plansQ.error} variant="cards" rows={3}>
          <div className="mt-4 grid gap-4 md:grid-cols-2 xl:grid-cols-4">
            {plansQ.data?.plans.filter((p) => p.code !== 'free').map((p) => {
              const current = p.code === s?.plan_code
              return (
                <div key={p.code}
                     className={`rounded-2xl border p-4 ${current
                       ? 'border-emerald-400 bg-emerald-50/40 dark:bg-emerald-950/20'
                       : 'border-[var(--ui-border)]'}`}>
                  <div className="flex items-center justify-between">
                    <h3 className="font-semibold">{p.name}</h3>
                    {current && <Badge tone="mint">Current</Badge>}
                  </div>
                  <p className="mt-1 min-h-[2.5rem] text-xs text-[var(--muted)]">{p.tagline}</p>
                  <p className="mt-2 text-2xl font-semibold">
                    {p.code === 'enterprise' ? 'Custom' : inr(p.price_paise)}
                    {p.period === 'monthly' && p.code !== 'enterprise' && (
                      <span className="text-sm font-normal text-[var(--muted)]">/mo</span>
                    )}
                  </p>
                  {p.code !== 'enterprise' && (
                    <p className="text-2xs text-[var(--muted)]">
                      {inr(p.gross_paise)} incl. GST
                      {p.period === 'one_time' && ` · ${p.duration_days} days`}
                    </p>
                  )}
                  <p className="mt-3 text-sm">
                    {p.included_minutes == null ? 'Custom minutes'
                      : `${p.included_minutes.toLocaleString('en-IN')} minutes`}
                    {p.max_agent_families != null && ` · ${p.max_agent_families} agents`}
                  </p>

                  {/* RBI additional-factor warning. Above ₹15,000 gross the
                      bank asks the customer to approve EVERY monthly debit on
                      a card or UPI mandate — saying so up front is much better
                      than a surprise every renewal. */}
                  {p.period === 'monthly' && !p.afa_free && p.code !== 'enterprise' && (
                    <p className="mt-2 text-2xs text-[var(--muted)]">
                      Above ₹15,000 per charge — we recommend bank e-mandate at
                      checkout so renewals are automatic.
                    </p>
                  )}

                  <div className="mt-4">
                    {p.code === 'enterprise' ? (
                      <Button variant="ghost" className="w-full"
                              onClick={() => { window.location.href = 'mailto:tech@dhronai.com?subject=Namaskall Enterprise' }}>
                        Contact us
                      </Button>
                    ) : p.already_used ? (
                      <Button className="w-full" disabled>Already used</Button>
                    ) : current ? (
                      <Button className="w-full" disabled>Your plan</Button>
                    ) : (
                      <Button className="w-full" onClick={() => buy(p)}
                              disabled={!!busy || !s?.gateway.configured}>
                        {busy === p.code ? <Loader2 className="h-4 w-4 animate-spin" />
                          : p.period === 'one_time' ? <Sparkles className="h-4 w-4" />
                          : <CreditCard className="h-4 w-4" />}
                        {p.period === 'one_time' ? 'Start Pilot' : 'Choose'}
                      </Button>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </AsyncState>
      </Card>

      <BillingProfileCard data={profileQ.data} loading={profileQ.isLoading}
                          error={profileQ.error} onSaved={() => refresh(['billing', 'profile'])} />

      {/* Payment history */}
      <Card className="p-5">
        <h2 className="text-lg font-semibold">Payment history</h2>
        <AsyncState loading={paymentsQ.isLoading} error={paymentsQ.error}
                    empty={!paymentsQ.data?.payments.length}
                    emptyLabel="No payments yet — invoices appear here after your first purchase.">
          <div className="mt-3 overflow-x-auto">
            <Table>
              <THead>
                <TH>Date</TH><TH>For</TH><TH className="text-right">Amount</TH>
                <TH>Status</TH><TH>Invoice</TH><TH />
              </THead>
              <tbody>
                {paymentsQ.data?.payments.map((p) => (
                  <TR key={p.id}>
                    <TD>{(p.issued_at || p.created_at || '').slice(0, 10)}</TD>
                    <TD className="capitalize">
                      {p.purpose}
                      {p.source === 'manual' && (
                        <span className="block text-2xs text-[var(--muted)]">
                          {p.method ? METHOD_LABEL[p.method] || p.method : 'invoiced'}
                          {p.payment_reference ? ` · ${p.payment_reference}` : ''}
                        </span>
                      )}
                    </TD>
                    <TD className="text-right tabular-nums">{inr(p.amount_paise, { paise: true })}</TD>
                    <TD>
                      <Badge tone={PAYMENT_TONE[p.status] || 'rose'}>
                        {PAYMENT_LABEL[p.status] || p.status}
                      </Badge>
                      {p.status === 'due' && p.due_date && (
                        <span className="block text-2xs text-[var(--muted)]">
                          due {p.due_date.slice(0, 10)}
                        </span>
                      )}
                    </TD>
                    <TD className="font-mono text-2xs">
                      {p.receipt_number || '—'}
                      {p.credit_note_number && (
                        <span className="block text-[var(--muted)]">CN {p.credit_note_number}</span>
                      )}
                    </TD>
                    <TD>
                      {p.receipt_number && (
                        <Button variant="ghost"
                                onClick={() => window.open(`/billing/receipt/${p.id}`, '_blank')}>
                          <Download className="h-3.5 w-3.5" /> Invoice
                        </Button>
                      )}
                    </TD>
                  </TR>
                ))}
              </tbody>
            </Table>
          </div>
        </AsyncState>
      </Card>

      {/* Attempts — including the ones that never became money. This is what a
          support conversation actually needs, so the user can see it too. */}
      {!!attemptsQ.data?.attempts.filter((a) => a.status !== 'captured').length && (
        <Card className="p-5">
          <h2 className="text-lg font-semibold">Recent payment attempts</h2>
          <p className="mt-1 text-xs text-[var(--muted)]">
            Attempts that did not complete. Nothing here has been charged.
          </p>
          <div className="mt-3 overflow-x-auto">
            <Table>
              <THead><TH>When</TH><TH>Plan</TH><TH className="text-right">Amount</TH><TH>Outcome</TH></THead>
              <tbody>
                {attemptsQ.data.attempts.filter((a) => a.status !== 'captured').map((a) => (
                  <TR key={a.id}>
                    <TD>{fmtDateTime(a.created_at)}</TD>
                    <TD>{a.plan_code || a.kind}</TD>
                    <TD className="text-right tabular-nums">{inr(a.amount_paise)}</TD>
                    <TD>
                      <Badge tone={a.status === 'failed' ? 'rose' : 'neutral'}>{a.status}</Badge>
                      {a.failure_reason && (
                        <span className="ml-2 text-2xs text-[var(--muted)]">{a.failure_reason}</span>
                      )}
                    </TD>
                  </TR>
                ))}
              </tbody>
            </Table>
          </div>
        </Card>
      )}
    </div>
  )
}

/* ── GST billing profile ─────────────────────────────────────────────────── */

function BillingProfileCard({ data, loading, error, onSaved }: {
  data?: { profile: BillingProfile; platform_ready: boolean }
  loading?: boolean; error?: unknown; onSaved: () => void
}) {
  const toast = useToast()
  const [form, setForm] = useState<BillingProfile>({})
  const [seeded, setSeeded] = useState(false)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    if (!data || seeded) return
    setForm(data.profile || {})
    setSeeded(true)
  }, [data, seeded])

  const save = async () => {
    setSaving(true)
    try {
      const r = await saveProfile(form)
      setForm(r.profile)
      toast('Billing details saved.', 'success')
      onSaved()
    } catch (e: any) {
      toast(e?.reason || e?.message || 'Could not save', 'error')
    } finally { setSaving(false) }
  }

  return (
    <Card className="p-5">
      <h2 className="flex items-center gap-2 text-lg font-semibold">
        <FileText className="h-4 w-4" /> Billing details
      </h2>
      <p className="mt-1 text-xs text-[var(--muted)]">
        Used on your GST tax invoice. Add your GSTIN to claim input credit —
        the place of supply is taken from it automatically.
      </p>
      <AsyncState loading={loading} error={error} variant="rows" rows={2}>
        <div className="mt-4 grid gap-3 sm:grid-cols-2">
          <Field label="Registered business name">
            <Input value={form.legal_name || ''}
                   onChange={(e) => setForm({ ...form, legal_name: e.target.value })}
                   placeholder="Acme Private Limited" />
          </Field>
          <Field label="GSTIN (optional)">
            <Input value={form.gstin || ''}
                   onChange={(e) => setForm({ ...form, gstin: e.target.value.toUpperCase() })}
                   placeholder="24AAACC1206D1ZM" maxLength={15} className="font-mono" />
          </Field>
          <Field label="Billing address">
            <Input value={form.address || ''}
                   onChange={(e) => setForm({ ...form, address: e.target.value })} />
          </Field>
          <Field label="Billing contact phone">
            <Input value={form.phone || ''}
                   onChange={(e) => setForm({ ...form, phone: e.target.value })} />
          </Field>
        </div>
        {form.state_name && (
          <p className="mt-2 text-xs text-[var(--muted)]">
            Place of supply: {form.state_name} (from your GSTIN)
          </p>
        )}
        <div className="mt-4">
          <Button onClick={save} disabled={saving}>
            {saving ? <Spinner className="h-4 w-4" /> : <Check className="h-4 w-4" />} Save
          </Button>
        </div>
      </AsyncState>
    </Card>
  )
}
