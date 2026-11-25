import { api } from './client'
import { post, put } from './mutate'

/* ───────────────────────── types ───────────────────────── */

export type PlanFeatures = {
  recordings: boolean
  summaries: boolean
  transcripts: boolean
  intelligence: boolean
  conversation_analytics: boolean
  advanced_analytics: boolean
  autocall: boolean
}

export type Plan = {
  code: string
  name: string
  tagline?: string | null
  /** Base price in paise, GST EXCLUSIVE. */
  price_paise: number
  /** What the customer is actually charged: base + 18% GST. */
  gross_paise: number
  period: 'monthly' | 'one_time'
  duration_days?: number | null
  included_minutes?: number | null
  max_agent_families?: number | null
  max_concurrent_calls?: number | null
  self_serve: boolean
  purchasable: boolean
  topup_allowed: boolean
  topup_pack_minutes?: number | null
  topup_rate_paise?: number | null
  features: PlanFeatures
  /**
   * False when the gross charge exceeds ₹15,000 — above that ceiling an RBI
   * additional-factor prompt fires on EVERY card/UPI auto-debit, so the
   * checkout copy recommends eMandate for these tiers.
   */
  afa_free: boolean
  already_used: boolean
}

export type GatewayStatus = {
  configured: boolean
  webhook_configured: boolean
  mode: string
  key_id: string
}

export type Subscription = {
  id: number
  plan_code: string
  kind: 'subscription' | 'one_time'
  status: string
  current_start?: string | null
  current_end?: string | null
  grace_until?: string | null
  cancel_at_cycle_end: number
  scheduled: number
  razorpay_subscription_id?: string | null
  razorpay_order_id?: string | null
}

export type Banner = { tone: string; text: string } | null

export type BillingSummary = {
  owner: string
  plan_code: string
  plan_name: string
  status: 'ok' | 'grace' | 'locked'
  minutes_limit: number | null
  minutes_included: number | null
  minutes_topup: number
  minutes_used: number
  minutes_remaining: number | null
  minutes_reserved: number
  calls_in_flight: number
  period_start: string | null
  period_end: string | null
  max_agent_families: number | null
  max_concurrent_calls: number | null
  copilot_turns_per_day: number | null
  /** Daily AI-assistant token allowance; null = unlimited. */
  copilot_tokens_per_day: number | null
  copilot_tokens_used: number
  copilot_tokens_remaining: number | null
  features: PlanFeatures
  agents_used: number | null
  subscription: Subscription | null
  pilot_used: boolean
  enforcement: boolean
  /**
   * The free plan's caps bind whether or not global billing `enforcement` is
   * on — they are anti-abuse limits, not plan tiering. Any UI that decides
   * whether a cap applies must read `enforcement || trial`; reading
   * `enforcement` alone hides a limit that is actually being enforced, and the
   * user meets it as an unexplained 402 on submit.
   */
  trial: boolean
  gateway: GatewayStatus
  unresolved_payments: number
  banner: Banner
}

export type Payment = {
  id: number
  created_at: string
  /** Operator-set invoice date for manual invoices; falls back to created_at. */
  issued_at?: string | null
  due_date?: string | null
  paid_at?: string | null
  /** 'razorpay' | 'manual' — manual invoices are collected offline. */
  source?: string
  /** UTR / cheque number, for reconciling against a bank statement. */
  payment_reference?: string | null
  purpose: string
  amount_paise: number
  base_paise: number
  tax_paise: number
  status: string
  method?: string | null
  receipt_number?: string | null
  credit_note_number?: string | null
  refunded_paise: number
  period_start?: string | null
  period_end?: string | null
}

export type Attempt = {
  id: number
  created_at: string
  updated_at: string
  kind: string
  intent: string
  plan_code?: string | null
  amount_paise: number
  status: string
  failure_step?: string | null
  failure_code?: string | null
  failure_reason?: string | null
  gateway_ref?: string | null
  reconciled_at?: string | null
  reconcile_note?: string | null
}

export type BillingProfile = {
  legal_name?: string
  gstin?: string
  state_code?: string
  state_name?: string
  address?: string
  email?: string
  phone?: string
}

export type Receipt = {
  number: string | null
  credit_note_number: string | null
  issued_at: string
  status: string
  method?: string | null
  gateway_payment_id?: string | null
  period_start?: string | null
  period_end?: string | null
  purpose: string
  supplier: Record<string, string>
  customer: Record<string, any>
  line_items: { description: string; sac: string; amount_paise: number }[]
  tax: {
    base_paise: number; tax_paise: number; total_paise: number; rate: number
    inter_state: boolean; place_of_supply: string; sac: string
    cgst_paise: number; sgst_paise: number; igst_paise: number
  }
  amount_paise: number
  refunded_paise: number
}

/** What POST /checkout returns — the SPA branches on `flow`. */
export type CheckoutSession = {
  ok: boolean
  flow: 'order' | 'subscription'
  order_id?: string
  subscription_id?: string
  key_id: string
  amount_paise: number
  currency: string
  plan?: string
  plan_name?: string
  purpose?: string
  minutes?: number
  packs?: number
  prefill: { name: string; email: string; contact: string }
  attempt_id: number
  prefer_emandate?: boolean
}

/* ───────────────────────── calls ───────────────────────── */

export const getPlans = () => api<{ plans: Plan[]; gateway: GatewayStatus }>('/billing/plans')
export const getSummary = () => api<BillingSummary>('/billing/summary')
export const getPayments = (limit = 25, offset = 0) =>
  api<{
    payments: Payment[]; total: number; limit: number; offset: number
    outstanding_paise: number; outstanding_count: number
  }>(`/billing/payments?limit=${limit}&offset=${offset}`)
export const getAttempts = (limit = 20) =>
  api<{ attempts: Attempt[] }>(`/billing/attempts?limit=${limit}`)
export const getReceipt = (paymentId: number) =>
  api<Receipt>(`/billing/receipts/${paymentId}`)
export const getProfile = () =>
  api<{ profile: BillingProfile; platform_ready: boolean }>('/billing/profile')
export const saveProfile = (p: BillingProfile) =>
  put<{ ok: boolean; profile: BillingProfile }>('/billing/profile', p)

export const startCheckout = (plan_code: string) =>
  post<CheckoutSession>('/billing/checkout', { plan_code })
export const startTopup = (packs: number) =>
  post<CheckoutSession>('/billing/topup', { packs })
export const verifyCheckout = (payload: Record<string, string>) =>
  post<{ ok: boolean; status: string; plan?: string; minutes?: number }>(
    '/billing/checkout/verify', payload)
export const cancelSubscription = () =>
  post<{ ok: boolean; status: string; active_until: string }>('/billing/cancel')
export const changePlan = (plan_code: string) =>
  post<CheckoutSession>('/billing/change', { plan_code })

/**
 * "I paid but nothing happened." Re-asks the gateway about every unresolved
 * attempt on this account. Idempotent, so pressing it twice is harmless.
 */
export const reconcile = () =>
  post<{ ok: boolean; message: string; checked: number; recovered: number }>(
    '/billing/reconcile')

/* ───────────────────────── helpers ───────────────────────── */

/** Paise → ₹ with Indian digit grouping. */
export function inr(paise: number | null | undefined, opts?: { paise?: boolean }): string {
  if (paise == null) return '—'
  const rupees = paise / 100
  return rupees.toLocaleString('en-IN', {
    style: 'currency', currency: 'INR',
    minimumFractionDigits: opts?.paise ? 2 : 0,
    maximumFractionDigits: opts?.paise ? 2 : 0,
  })
}

/**
 * Razorpay's checkout.js, loaded ONLY on the billing page.
 *
 * A global <script> in index.html would put a third-party script on every page
 * of the app — including the live-call monitor — for a file that is needed on
 * exactly one route. Resolves false when the network blocks it, so the caller
 * can say so instead of hanging on a modal that will never open.
 */
let checkoutPromise: Promise<boolean> | null = null
export function loadCheckoutScript(): Promise<boolean> {
  if ((window as any).Razorpay) return Promise.resolve(true)
  if (checkoutPromise) return checkoutPromise
  checkoutPromise = new Promise<boolean>((resolve) => {
    const s = document.createElement('script')
    s.src = 'https://checkout.razorpay.com/v1/checkout.js'
    s.async = true
    s.onload = () => resolve(true)
    s.onerror = () => { checkoutPromise = null; resolve(false) }
    document.body.appendChild(s)
  })
  return checkoutPromise
}

/**
 * Open the gateway modal for a checkout session and verify the result.
 *
 * The `order_id` vs `subscription_id` split is the whole reason `flow` exists:
 * a one-time Order and a recurring Subscription open the SAME modal with
 * different keys, and the callback returns different id fields, which the
 * server then verifies with two different HMAC field orders.
 */
export function openCheckout(
  session: CheckoutSession,
  opts: { onSuccess: (r: any) => void; onDismiss?: () => void; onError?: (e: any) => void },
): void {
  const Razorpay = (window as any).Razorpay
  if (!Razorpay) { opts.onError?.(new Error('checkout unavailable')); return }
  const base: Record<string, any> = {
    key: session.key_id,
    name: 'Namaskall',
    description: session.plan_name || session.purpose || 'Namaskall',
    prefill: session.prefill,
    theme: { color: '#059669' },
    modal: { ondismiss: () => opts.onDismiss?.() },
    handler: async (resp: any) => {
      try { opts.onSuccess(await verifyCheckout(resp)) }
      catch (e) { opts.onError?.(e) }
    },
  }
  if (session.flow === 'order') {
    base.order_id = session.order_id
    base.amount = session.amount_paise
    base.currency = session.currency
  } else {
    base.subscription_id = session.subscription_id
    // eMandate first for amounts above the AFA ceiling: a bank-rail mandate
    // renews silently, a card mandate demands approval every month.
    if (session.prefer_emandate) base.method = { emandate: true, card: true, upi: true }
  }
  new Razorpay(base).open()
}
