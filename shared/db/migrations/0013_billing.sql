-- 0013_billing.sql — plans, subscriptions, entitlements, payments, receipts.
-- deploy/PLAN-razorpay-subscriptions.md §4.1.
--
-- Two doctrines make the rest of the layer readable:
--   * `subscriptions` is HISTORY — a local mirror of the gateway's truth,
--     one row per purchase, never deleted.
--   * `entitlements` is the RESOLVED PRESENT — exactly one row per owner,
--     rebuilt wholesale by resolve_entitlements(). It is the only table the
--     hot path (dial admission, agent-cap checks, feature gates) reads.
--
-- Style follows 0001-0012: bare CREATE TABLE (the version tracker makes
-- IF NOT EXISTS pointless and it would mask half-applied state), TIMESTAMPTZ,
-- real foreign keys, `owner TEXT` = normalized email, JSON stored as TEXT.
--
-- This migration MUST NOT reference agents.archived_at: that column is added
-- by the boot-time MIGRATIONS list in shared/db/database.py, which migrate.py
-- never runs, so the ordering is not guaranteed.

-- ---------------------------------------------------------------------------
-- Catalogue
-- ---------------------------------------------------------------------------
CREATE TABLE plans (
    code TEXT PRIMARY KEY,                 -- free|pilot|basic|growth|enterprise
    name TEXT NOT NULL,
    tagline TEXT,
    -- BASE price, GST EXCLUSIVE. The gateway is charged price_paise * 1.18;
    -- the tax invoice shows base + CGST/SGST or IGST. Storing the base (not
    -- the gross) keeps the receipt arithmetic exact in one direction only.
    price_paise BIGINT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'INR',
    period TEXT NOT NULL DEFAULT 'monthly',          -- monthly | one_time
    duration_days INTEGER,                           -- 7 for pilot; NULL = calendar cycle
    price_includes_gst INTEGER NOT NULL DEFAULT 0,   -- 0 = prices are ex-GST

    included_minutes INTEGER,              -- NULL = unlimited
    max_agent_families INTEGER,            -- NULL = unlimited
    max_concurrent_calls INTEGER,          -- NULL = unlimited
    copilot_turns_per_day INTEGER,         -- NULL = unlimited

    -- Feature flags. One column per marketed capability, so the admin plane
    -- can grant a capability to one Enterprise tenant without a code change,
    -- and so E12 can gate at the API from a single entitlements row.
    recordings_enabled INTEGER NOT NULL DEFAULT 0,
    summaries_enabled INTEGER NOT NULL DEFAULT 0,
    transcripts_enabled INTEGER NOT NULL DEFAULT 0,
    intelligence_enabled INTEGER NOT NULL DEFAULT 0,
    conversation_analytics INTEGER NOT NULL DEFAULT 0,
    advanced_analytics INTEGER NOT NULL DEFAULT 0,
    autocall_enabled INTEGER NOT NULL DEFAULT 0,

    -- Additional minutes. NOT SOLD (owner decision, 2026-08-01): running out of
    -- minutes offers exactly one route — upgrade the plan. The columns and the
    -- minute_topups table stay because the rail is built and tested, and a
    -- future decision to sell packs is then a data change (set topup_allowed=1
    -- and a rate) rather than a schema migration. Seeded 0 everywhere.
    topup_allowed INTEGER NOT NULL DEFAULT 0,
    topup_pack_minutes INTEGER,
    topup_rate_paise BIGINT,               -- per MINUTE, base, ex-GST

    self_serve INTEGER NOT NULL DEFAULT 1, -- 0 = sales-assisted (Enterprise)
    -- Gateway plan ids are per-mode because test and live are separate
    -- catalogues. Populated by shared.billing.plans_bootstrap; NULL until an
    -- account exists. Recurring tiers only — one-time tiers need no Plan.
    razorpay_plan_id_test TEXT,
    razorpay_plan_id_live TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Purchases (both billing models in one table)
--   kind='subscription' -> Basic/Growth, recurring, razorpay_subscription_id
--   kind='one_time'     -> Pilot 7-day window, razorpay_order_id, no mandate
-- ---------------------------------------------------------------------------
CREATE TABLE subscriptions (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    plan_code TEXT NOT NULL REFERENCES plans(code),
    kind TEXT NOT NULL DEFAULT 'subscription',
    status TEXT NOT NULL DEFAULT 'created',
    -- recurring: created|authenticated|active|pending|halted|paused|cancelled|completed|expired
    -- one_time:  created|active|completed|expired
    scheduled INTEGER NOT NULL DEFAULT 0,  -- authenticated with a FUTURE start
    source TEXT NOT NULL DEFAULT 'razorpay',   -- razorpay | manual (Enterprise)
    razorpay_subscription_id TEXT UNIQUE,
    razorpay_order_id TEXT UNIQUE,
    razorpay_mode TEXT,                    -- test | live, as of creation
    current_start TIMESTAMPTZ,
    current_end TIMESTAMPTZ,
    charge_at TIMESTAMPTZ,                 -- drives the webhook-silence alarm
    cancel_at_cycle_end INTEGER NOT NULL DEFAULT 0,
    verified_at TIMESTAMPTZ,               -- checkout/verify may fire ONCE
    grace_until TIMESTAMPTZ,               -- set when a payment fails
    meta TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_subscriptions_owner ON subscriptions(owner);
CREATE INDEX idx_subscriptions_status ON subscriptions(status, current_end);

-- At most ONE current recurring subscription per tenant.
-- Excludes `scheduled` rows so the §6.4 upgrade flow works: a future-dated
-- subscription is authenticated while the outgoing one is still active, and
-- both rows must be allowed to exist at once.
-- Excludes kind='one_time' so a Pilot tenant can buy Basic/Growth mid-trial.
CREATE UNIQUE INDEX uq_subscriptions_owner_current ON subscriptions(owner)
    WHERE kind = 'subscription' AND scheduled = 0
      AND status IN ('authenticated','active','pending','halted','paused');

-- One active Pilot window at a time. Belt-and-braces; pilot_claims is the
-- real once-ever rule.
CREATE UNIQUE INDEX uq_subscriptions_owner_pilot_active ON subscriptions(owner)
    WHERE kind = 'one_time' AND status = 'active';

-- ---------------------------------------------------------------------------
-- Pilot-once. Claimed AT CHECKOUT, in the same transaction as the `created`
-- subscription row.
--
-- Enforcing this with a partial index over `subscriptions` does not work: any
-- such index has to exclude `created` rows (otherwise an abandoned checkout
-- bars the tenant forever), which means two concurrent checkouts both create
-- REAL gateway objects and the constraint only fires later, inside the
-- webhook — after the customer has been charged, with no local record to
-- refund against. A dedicated table claimed up front makes the race impossible.
--
-- Append-only: an admin reset writes released_by/released_at, never DELETE, so
-- the audit trail of who re-granted a trial survives.
-- ---------------------------------------------------------------------------
CREATE TABLE pilot_claims (
    owner TEXT PRIMARY KEY,
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    razorpay_contact TEXT,                 -- scarcer identity than an email
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_by TEXT,
    released_at TIMESTAMPTZ,
    release_reason TEXT
);
-- A verified phone number is harder to mint than a Google account, so it is
-- the second axis of the once-ever rule. Partial: most claims have no contact.
CREATE UNIQUE INDEX uq_pilot_claims_contact ON pilot_claims(razorpay_contact)
    WHERE razorpay_contact IS NOT NULL AND released_at IS NULL;

-- ---------------------------------------------------------------------------
-- The resolved present. Exactly one row per owner; rebuilt wholesale.
-- ---------------------------------------------------------------------------
CREATE TABLE entitlements (
    owner TEXT PRIMARY KEY,
    plan_code TEXT NOT NULL DEFAULT 'free' REFERENCES plans(code),
    source_subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,

    minutes_limit INTEGER,                 -- NULL = unlimited; INCLUDES top-ups
    minutes_included INTEGER,              -- plan component, for display
    minutes_topup INTEGER NOT NULL DEFAULT 0,
    max_agent_families INTEGER,
    max_concurrent_calls INTEGER,
    copilot_turns_per_day INTEGER,

    recordings INTEGER NOT NULL DEFAULT 0,
    summaries INTEGER NOT NULL DEFAULT 0,
    transcripts INTEGER NOT NULL DEFAULT 0,
    intelligence INTEGER NOT NULL DEFAULT 0,
    conversation_analytics INTEGER NOT NULL DEFAULT 0,
    advanced_analytics INTEGER NOT NULL DEFAULT 0,
    autocall INTEGER NOT NULL DEFAULT 0,

    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'ok',     -- ok | grace | locked
    lock_reason TEXT,
    overrides TEXT,                        -- JSON: per-tenant admin overrides
    notes TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

-- ---------------------------------------------------------------------------
-- In-flight dial admissions.
--
-- Admission has to be a RESERVATION, not a read. `outbound_dial` returns
-- immediately and originates in a background task, and the duplicate guard is
-- keyed per phone — so N parallel POSTs to N different contacts all pass the
-- same "remaining >= 1" check in the same instant. The overshoot is unbounded,
-- not one call.
--
-- Reservations live in their own table rather than as a counter column so
-- they EXPIRE. A counter that is incremented at dial and decremented at
-- ledger-close leaks permanently every time a call crashes between the two,
-- and a leaked counter silently shrinks a paying tenant's quota forever with
-- no way to tell it apart from real usage. Here, a crashed call's row simply
-- ages out; the sync pass sweeps the corpses.
-- ---------------------------------------------------------------------------
CREATE TABLE billing_reservations (
    ref TEXT PRIMARY KEY,                  -- conn_id once known, else dial:<uuid>
    owner TEXT NOT NULL,
    minutes INTEGER NOT NULL DEFAULT 0,
    origin TEXT,                           -- dial|campaign|autodial|automation|test|inbound
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX idx_reservations_owner ON billing_reservations(owner, expires_at);
CREATE INDEX idx_reservations_expiry ON billing_reservations(expires_at);

-- ---------------------------------------------------------------------------
-- Money actually taken
-- ---------------------------------------------------------------------------
CREATE TABLE payments (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'razorpay',   -- razorpay | manual
    purpose TEXT NOT NULL DEFAULT 'subscription',  -- subscription|pilot|topup|manual
    razorpay_payment_id TEXT UNIQUE,
    razorpay_order_id TEXT,
    razorpay_invoice_id TEXT,
    -- GROSS amount actually charged (base + tax), in paise, as the gateway
    -- reports it. The base/tax split lives in the receipt fields below.
    amount_paise BIGINT NOT NULL,
    base_paise BIGINT NOT NULL DEFAULT 0,
    tax_paise BIGINT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'INR',
    status TEXT NOT NULL,                  -- captured | failed | refunded
    method TEXT,                           -- card | upi | netbanking | emandate
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    receipt_number TEXT UNIQUE,
    -- The customer's GST profile AS OF issue. An invoice is a legal document:
    -- a later edit to the tenant's billing profile must not silently rewrite
    -- one that has already been issued.
    billing_snapshot TEXT,
    refunded_paise BIGINT NOT NULL DEFAULT 0,
    refunded_at TIMESTAMPTZ,
    credit_note_number TEXT UNIQUE,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_payments_owner_time ON payments(owner, created_at);

-- GST requires a consecutive serial per financial year. A naive MAX(seq)+1
-- races between the two processes that insert payments (the api webhook and
-- the admin plane's manual entry), and a gap or a duplicate in an invoice
-- series is an audit finding. Allocation is SELECT ... FOR UPDATE on this row.
CREATE TABLE receipt_counters (
    fy TEXT PRIMARY KEY,                   -- '25-26'
    last_seq INTEGER NOT NULL DEFAULT 0,
    last_credit_seq INTEGER NOT NULL DEFAULT 0
);

-- ---------------------------------------------------------------------------
-- Additional minutes (§6.9)
-- ---------------------------------------------------------------------------
CREATE TABLE minute_topups (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    payment_id BIGINT REFERENCES payments(id) ON DELETE SET NULL,
    razorpay_order_id TEXT UNIQUE,
    minutes INTEGER NOT NULL,
    amount_paise BIGINT NOT NULL,          -- base, ex-GST
    status TEXT NOT NULL DEFAULT 'created',  -- created|active|expired|refunded
    -- Copied from the subscription cycle at purchase: packs top up THIS cycle
    -- and do not roll over.
    period_start TIMESTAMPTZ,
    period_end TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_topups_owner_period ON minute_topups(owner, period_end);
CREATE INDEX idx_topups_active ON minute_topups(owner, status);

-- ---------------------------------------------------------------------------
-- Gateway event log. Payloads are stored verbatim so a mis-handled event can
-- be replayed — the gateway dedupes its own retries and will never resend one
-- we have already acknowledged.
-- ---------------------------------------------------------------------------
CREATE TABLE billing_events (
    id BIGSERIAL PRIMARY KEY,
    -- NOT NULL is load-bearing: a nullable UNIQUE column lets unlimited NULL
    -- rows through, which defeats the dedupe this table exists for.
    event_id TEXT NOT NULL UNIQUE,
    event_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    signature_ok INTEGER NOT NULL DEFAULT 1,
    owner TEXT,                            -- resolved locally, never trusted from `notes`
    processed_at TIMESTAMPTZ,
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_billing_events_unprocessed ON billing_events(received_at)
    WHERE processed_at IS NULL;
CREATE INDEX idx_billing_events_type ON billing_events(event_type, received_at);

-- ---------------------------------------------------------------------------
-- Transaction log. One row per checkout ATTEMPT, from the moment the tenant
-- presses Pay — including the attempts that never become money.
--
-- `payments` only ever records success, so on its own it cannot answer the
-- three questions that actually get asked in support:
--   "I paid and nothing happened"   -> an attempt with no resolved_payment_id
--   "why did my card fail"          -> failure_code / failure_reason / step
--   "did we double-charge them"     -> two captured attempts, one gateway_ref
-- The reconcile pass in shared/billing/sync.py walks this table.
-- ---------------------------------------------------------------------------
CREATE TABLE payment_attempts (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    plan_code TEXT REFERENCES plans(code),
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    topup_id BIGINT REFERENCES minute_topups(id) ON DELETE SET NULL,
    kind TEXT NOT NULL,                    -- subscription | one_time | topup
    intent TEXT NOT NULL DEFAULT 'new',    -- new | change | topup | retry
    gateway TEXT NOT NULL DEFAULT 'razorpay',
    gateway_mode TEXT,                     -- test | live
    gateway_ref TEXT,                      -- order_id or subscription_id
    amount_paise BIGINT NOT NULL DEFAULT 0,   -- gross, what we asked for
    status TEXT NOT NULL DEFAULT 'initiated',
    -- initiated       -> row written before the gateway call
    -- gateway_created -> gateway accepted, customer is at the checkout modal
    -- gateway_error   -> gateway refused to create the order/subscription
    -- authorized      -> customer completed, signature verified
    -- captured        -> money confirmed (verify fast-path OR webhook)
    -- failed          -> customer's payment failed
    -- abandoned       -> never completed, aged out by sync
    -- reconciled      -> captured late, recovered by the reconcile pass
    failure_step TEXT,                     -- create|checkout|verify|webhook|reconcile
    failure_code TEXT,
    failure_reason TEXT,
    request_ip TEXT,
    user_agent TEXT,
    resolved_payment_id BIGINT REFERENCES payments(id) ON DELETE SET NULL,
    reconciled_at TIMESTAMPTZ,
    reconcile_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_attempts_owner_time ON payment_attempts(owner, created_at DESC);
CREATE INDEX idx_attempts_ref ON payment_attempts(gateway_ref);
-- The recovery worklist: everything that started and never resolved.
CREATE INDEX idx_attempts_open ON payment_attempts(created_at)
    WHERE status IN ('initiated','gateway_created','authorized');

-- ---------------------------------------------------------------------------
-- Seed: the five plans.
--
-- Prices are GST-EXCLUSIVE and come from the published pricing page
-- (2026-07-31). Both recurring tiers charge more than Rs 15,000 gross
-- (Basic 17,698.82 / Growth 58,998.82), which is above the RBI AFA-free
-- e-mandate ceiling — so card/UPI-Autopay mandates will demand a customer
-- action every month and eMandate/eNACH (no per-debit AFA up to Rs 99,999)
-- is the method to prefer at checkout. See RUNBOOK §18.
--
-- Free is not marketed; it is the default state of any signed-up account.
-- ---------------------------------------------------------------------------
INSERT INTO plans (
    code, name, tagline, price_paise, period, duration_days,
    included_minutes, max_agent_families, max_concurrent_calls,
    copilot_turns_per_day,
    recordings_enabled, summaries_enabled, transcripts_enabled,
    intelligence_enabled, conversation_analytics, advanced_analytics,
    autocall_enabled,
    topup_allowed, topup_pack_minutes, topup_rate_paise,
    self_serve, sort
) VALUES
-- code       name          tagline
--   price      period       days  mins  fam  conc  copilot
--   rec sum trn int conv adv auto   topup pack rate   self sort
('free', 'Free', 'The default state of a new account',
    0,       'monthly',   NULL,   30,   1,   1,   20,
    0,  0,  0,  0,  0,  0,  0,        0, NULL, NULL,     0,  0),

('pilot', 'Pilot', 'Test LesCall with your own business',
    299900,  'one_time',     7,  500,   3,   5,  200,
    1,  1,  0,  1,  0,  0,  0,        0, NULL, NULL,     1, 10),

('basic', 'Basic', 'Start automating customer conversations',
    1499900, 'monthly',   NULL, 3000,  10,  10,  300,
    1,  0,  0,  0,  0,  0,  0,        0, NULL, NULL,     1, 20),

('growth', 'Growth', 'Scale conversations without scaling teams',
    4999900, 'monthly',   NULL, 8000,  25,  25,  500,
    1,  1,  1,  1,  1,  1,  1,        0, NULL, NULL,     1, 30),

('enterprise', 'Enterprise', 'Built around your business',
    0,       'monthly',   NULL, NULL, NULL, NULL, NULL,
    1,  1,  1,  1,  1,  1,  1,        0, NULL, NULL,     0, 40);

-- ---------------------------------------------------------------------------
-- Seed: explicit Enterprise entitlements for the staff tenants.
--
-- Without these three rows, flipping enforcement on drops accounts that
-- currently run 11 and 3 agents to Free (30 minutes, 1 agent) and blocks them
-- instantly. §4.1's discovery query MUST return zero rows before P6.
-- ---------------------------------------------------------------------------
INSERT INTO entitlements (
    owner, plan_code, minutes_limit, minutes_included,
    max_agent_families, max_concurrent_calls, copilot_turns_per_day,
    recordings, summaries, transcripts, intelligence,
    conversation_analytics, advanced_analytics, autocall,
    status, updated_by, notes
) VALUES
('tech@dhronai.com',        'enterprise', NULL, NULL, NULL, NULL, NULL,
    1,1,1,1,1,1,1, 'ok', 'migration:0013', 'founding account — unlimited'),
('angelo@dhronai.com',      'enterprise', NULL, NULL, NULL, NULL, NULL,
    1,1,1,1,1,1,1, 'ok', 'migration:0013', 'staff account — unlimited'),
('alwynantonyben@dhronai.com','enterprise', NULL, NULL, NULL, NULL, NULL,
    1,1,1,1,1,1,1, 'ok', 'migration:0013', 'staff account — unlimited')
ON CONFLICT (owner) DO NOTHING;

-- ---------------------------------------------------------------------------
-- Enforcement switch. Lives in the DB, NOT in env: the inbound gate (E6) runs
-- inside the runtime container, which would never receive an api-only env
-- flag — inbound enforcement would have been silently dead forever.
-- Shipped OFF. P6 turns it on.
-- ---------------------------------------------------------------------------
INSERT INTO settings (owner, key, value, updated_at)
VALUES ('', 'billing_enforce', '{"enabled": false}', now())
ON CONFLICT (owner, key) DO NOTHING;
