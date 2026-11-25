# PLAN — Razorpay Subscriptions, Tiered Plans & Localhost-only Super-Admin

Status: **DRAFT for approval** · Date: 2026-07-25 · **Revision 4 — final tier structure**
Basis: 6-agent codebase recon + live Razorpay/RBI research + 3-agent adversarial verify
(repo-accuracy / security red-team / completeness). 55 review findings folded in; every
repo claim below was opened and checked in the verify pass. Nothing is implemented yet.

Revision history — read §1 for the live tier definitions, not these:
· r1 initial · r2 post adversarial review (blockers fixed, P-1 added)
· r3 Pilot re-modelled as a 7-day one-time Order rather than a subscription
· **r4 final pricing page applied: Pro retired, Basic added, Pilot 500 min / 7 days,
  autocall + transcripts become Growth-tier features, GST-exclusive, minute top-ups (§6.9)**

---

## 0. Read this first — the plan changed shape

The review found that **billing enforcement is not the platform's weakest link**. Three
paths let anyone place calls or burn a tenant's quota *without ever touching an HTTP
endpoint this plan gates*. Building a money layer on top of them would produce quotas that
look enforced and aren't. So **Phase P-1 (security prerequisites) now comes before any
billing code** — details in §16, summary in §0.2.

### 0.1 What we're building (unchanged in substance)

Five plans (Free / Pilot / Basic / Growth / Enterprise) in a new `plans` table. **Two
Razorpay rails**: Subscriptions for the monthly tiers (Basic, Growth), and one-time Orders
for the 7-day Pilot and for minute top-ups. Enterprise is set manually from a **separate
super-admin service reachable only over a UNIX socket / 127.0.0.1** (SSH tunnel, never
nginx). Enforcement sits at **twelve** server-side choke points (E1–E12: six dial origins,
agent creation, autocall staging, three LLM surfaces, and the per-tier feature flags),
all reading one `entitlements` row per tenant. The SPA gets a lazy `/billing` page with
checkout, trial countdown, top-ups, payment history and printable GST receipts. Everything
ships behind an enforcement switch, so the platform behaves exactly as today until flipped.

### 0.2 Live findings that predate this plan (verified today, need your decision)

| | Finding | Verified evidence | Exposure |
|---|---|---|---|
| **S1** | **FreeSWITCH ESL open with the vendor default password.** `event_socket.conf.xml`: `listen-ip="::"`, `password="ClueCon"`, `apply-inbound-acl` **commented out**; `ss` shows `*:8021 LISTEN` | read the file + `ss -tlnp` | Anyone who reaches 8021 can `originate` unlimited calls on your Plivo trunk (toll fraud), attributed to nobody. **No evidence of intrusion** — FS logs show only legitimate `sofia/external/…` calls, no foreign ESL connections. Internet reachability depends on GCP VPC rules **I could not read** (`gcloud` auth expired) — GCP defaults to deny-all ingress, so this is *probably* blocked from outside, but it is unconditionally exploitable from any local process or host-network container |
| **S2** | **Media WebSocket unauthenticated with caller-chosen agent.** `ws_audio_receiver.py` listens `0.0.0.0:9001`; `handle_connection` parses `<uuid>/<caller>/<did>/<campaign>/<slug>` and `resolve_agent_slug` accepts any slug that merely *exists on disk* — no ownership check | `ss` + code read | Whoever reaches 9001 runs full AI sessions billed to any tenant they name (quota theft + your Sarvam/LLM spend), bypassing every billing gate |
| **S3** | **Signups are already open.** `calling-agent/.env:71` = `LESCALL_ALLOWED_EMAILS=*` (loaded by `auth.py` at import). `users` contains two unaffiliated Google accounts: `saptaswa.sen@bscdsh.christuniversity.in` (Jul 23) and `amalvpillai5@gmail.com` (**today**) | `grep` + `psql` | Any Google account can sign in right now with no quota, no agent cap, and campaign dialing on your shared trunk. **Both accounts created nothing** — zero agents, calls, contacts, ledger rows (checked). My earlier draft wrongly said signups were closed |
| **S4** | **Tenant-reachable SSRF.** `/api/intel/automations` (allowlisted) accepts a `webhook` action whose URL is unvalidated; `automation.py::_webhook` POSTs it from a host-network container → can hit `127.0.0.1:8090` and the GCP metadata IP | code read | The planned admin plane's bearer token is therefore **the only wall**, not defense-in-depth (§7.2 rewritten accordingly) |
| **S5** | **Email header injection.** `automation.py::_email` pipes unvalidated `params["to"]` into `sendmail -t` with concatenated headers | code read | VM becomes a relay for convincing "LesCall payment failed, re-enter card" mail from your own IP — directly undermines billing notifications |

**My recommendation:** fix S1–S3 this week regardless of whether you proceed with billing
(S1/S2 are ~1 hour of config + a bind change; S3 is one env line). S4/S5 land in P-1.
None of them are billing features — they're the floor billing has to stand on.

### 0.3 Decisions I need from you

**Resolved 2026-07-25** (folded into §1, no longer questions): tiers are
Free → Pilot → Basic → Growth → Enterprise (**Pro is retired**); Pilot = 7 days / 500 min,
one-time, never repeatable; Basic 3,000 min/mo; Growth 8,000 min/mo; **prices are GST
EXCLUSIVE**; autocall is **Growth+** (and Pilot); paid tiers can buy **additional
minutes** (§6.9).

| # | Question | Default if you don't answer |
|---|----------|------------------------------|
| **D1** 🔴 | **Prices for Pilot / Basic / Growth are missing from the final spec** — Razorpay Plans cannot be created without them, and once created they are **immutable forever**. Prior numbers no longer map cleanly: old "Growth ₹8,999 / 3,000 min" is now **Basic**, and the new Growth (8,000 min) sits between the retired ₹8,999 and ₹49,000 | Proposal, all **+18% GST at checkout**: Pilot **₹2,999** (₹6.00/min) · Basic **₹8,999** (₹3.00/min) · Growth **₹19,999** (₹2.50/min, keeps the volume discount the retired Pro had) |
| **D2** 🔴 | **Agent caps per tier are unspecified.** The copy says "Create & customize your own agent" (Pilot, singular) and lists "multiple agents" as an *Enterprise* differentiator, which would imply Basic/Growth are single-agent — surely not intended at ₹9k-20k | Free **1** · Pilot **3** · Basic **10** · Growth **25** · Enterprise unlimited |
| **D3** 🔴 | **Additional-minutes pricing** ("+ Additional minutes available" on Basic and Growth) — pack size and price | **1,000-minute packs**, priced at the tier's per-minute rate +20% (Basic ₹3,600, Growth ₹3,000), bought as one-time Orders, valid to period end, non-refundable, no auto-top-up in v1 |
| D4 | Free 30 min — per month or lifetime? | **Per IST calendar month** |
| D5 | At 100% minutes (before any top-up) | **Hard block** new calls + "buy more minutes" CTA |
| **D6** | **Existing tenants.** `angelo@dhronai.com` (11 agents) and `alwynantonyben@dhronai.com` (3) would drop to Free at flip-on and immediately block | Seed **both + founder as Enterprise/unlimited** |
| D7 | S3: close signups until enforcement ships, or leave open? | **Close now**, reopen at P7 |
| D8 | Refund policy (ingestion is built either way — §6.3) | Case-by-case, manual |
| D9 | **Agents over cap after a downgrade** (Pilot→Free, Growth→Basic) | **Keep every agent working; block only new creates until under cap.** Deleting or disabling customer work on expiry is hostile; the minute quota is the real constraint |
| D10 | Does **Basic** really lack automated follow-up calls? Your copy lists it under Pilot and under "Everything in Basic, **plus**" for Growth — so Basic = no autocall. Odd only because the cheap trial has it | **Yes — autocall is Growth+ (and Pilot).** Trial shows the full product; Basic is the entry tier |
| D11 | **Transcripts + AI summaries**: Pilot says "recordings & AI summaries", Basic says "Call recordings" only, Growth adds "Summaries & transcripts" | **Recordings all paid tiers; summaries+transcripts Growth+ and Pilot** |

---

## 1. Tier matrix

Final structure, 2026-07-25. **Pro is retired; Basic is new.** Free is not marketed on the
pricing page — it's the default state of any signed-up account.

| | Free | **Pilot** | **Basic** | **Growth** ★ | Enterprise |
|---|---|---|---|---|---|
| Positioning | default | Test LesCall with your own business | Start automating customer conversations | Scale conversations without scaling teams | Built around your business |
| Price (ex-GST) | 0 | **₹2,999 once (D1)** | **₹8,999/mo (D1)** | **₹19,999/mo (D1)** | custom |
| **Billing model** | — | **one-time, 7 days** | monthly recurring | monthly recurring | manual (admin) |
| Minutes | 30/month | **500 for the 7 days** | **3,000**/month | **8,000**/month | custom |
| Additional minutes | ✗ | ✗ | ✓ (§6.9) | ✓ (§6.9) | custom |
| Agents (families¹) | 1 | 3 (D2) | 10 (D2) | 25 (D2) | unlimited |
| Inbound + outbound | ✓ | ✓ | ✓ | ✓ | ✓ |
| English + Hindi | ✓ | ✓ | ✓ | ✓ | + custom languages |
| Activity dashboard | ✓ | ✓ | ✓ | ✓ | ✓ |
| Intelligence dashboard | ✗ | ✓ | ✓ | ✓ | ✓ |
| Call recordings | ✗ | ✓ | ✓ | ✓ | ✓ |
| **Summaries + transcripts** | ✗ | ✓ | **✗ (D11)** | ✓ | ✓ |
| **Automated follow-up calls** | ✗ | ✓ | **✗ (D10)** | ✓ | ✓ |
| Lead qualification + booking | ✗ | ✓ | ✓ | ✓ | ✓ |
| Advanced analytics | ✗ | ✗ | ✗ | ✓ | ✓ |
| CRM / ERP / workflow | ✗ | ✗ | ✗ | ✗ | ✓ (bespoke, not a flag) |
| Concurrent calls² | 1 | 5 | 10 | 25 | custom |
| Copilot turns/day² | 20 | 200 | 300 | 500 | unlimited |
| Purchase | — | **Razorpay Order, once ever** | Razorpay Subscription | Razorpay Subscription | manual |

² Concurrency and Copilot budgets aren't marketed — they exist because without them one
tenant can hold unbounded parallel legs or drain the shared LLM quota and break paying
tenants' live calls (§7). Set them generously; they're abuse ceilings, not product limits.

**Pilot** is a one-time, paid 7-day run — effectively a trial of Growth (it carries
autocall, summaries and the Intelligence dashboard). 500 minutes inside a 7-day window.
When it ends the tenant drops to Free and can buy **Basic or Growth**; Pilot never returns
— `pilot_claims` (§4.1) enforces once-ever, independent of what they buy next. Your copy
promises exactly this ("A one-time starting point — once you move to a monthly plan, Pilot
can't be repeated"), so the rule must be airtight rather than best-effort.

Modelling Pilot as a **one-time Razorpay Order rather than a Subscription**: no mandate,
therefore no e-mandate authorization, no RBI pre-debit notification, no AFA exposure, and
every payment method works (card / UPI / netbanking / wallet). A `total_count:1` weekly
subscription would mint a mandate that never charges again — friction plus a dangling
mandate to cancel. The same Orders rail carries **additional-minute packs** (§6.9).
**Watch the HMAC trap in §5.1: Orders and Subscriptions use opposite field order, and this
plan contains both flows.**

**Prices are GST-exclusive** (confirmed): Razorpay charges `price × 1.18`, the receipt
shows base + CGST/SGST or IGST, and `plans.price_paise` stores the **base**. At the
proposed D1 numbers the Razorpay amounts are Pilot **353,882** paise, Basic **1,061,882**,
Growth **2,359,882** — all safely under the ₹15,000 AFA-free ceiling **except Growth**
(₹23,599 gross), which therefore needs the §11.4 treatment.

This is why Pilot uses Razorpay's **Orders API + standard Checkout**, not Subscriptions:
a one-time charge needs no mandate, so there is no e-mandate authorization, no RBI
pre-debit notification machinery, no AFA exposure, and every payment method works
(card / UPI / netbanking / wallet). Modelling a 7-day trial as a `total_count:1` weekly
subscription would create a mandate that never charges again — pure friction. **Watch the
HMAC trap in §5.1: Orders and Subscriptions use opposite field order, and this plan now
has both flows.**

¹ **Family = twin pair.** One "create" makes `<base>-inbound` + `<base>-outbound`
(`agent_family()`, `agent_builder_api.py:186`). Caps count families — "1 agent" on Free
means one working pair. Archiving frees the slot (§5.4).
² **New, added by review:** without a concurrency cap a Free tenant with 30 minutes can
hold hundreds of parallel legs (§7 note); without a Copilot cap one tenant can drain the
shared LLM quota and break paying tenants' live calls (E9–E11).

Minutes use the existing rounding law — `display_minutes()` in `shared/usage_metering.py`
(`BILLING_ROUNDING=per_call_ceil`). Quota math MUST call that same function or quota and
invoice diverge.

---

## 2. What already exists (verified)

- **Metering is done and trustworthy**: bridge opens/heartbeats/closes `usage_ledger`
  rows; `lescall-workers` reconciles against FreeSWITCH CDR every 300s;
  `billsec_final` = "the only number invoicing may read"; `usage_monthly` rollup.
- **No quota exists.** Overview's `minutes_quota` is a single platform-global value in
  `settings(owner='')`; its setter is 403-blocked under OAuth. Nothing checks minutes at
  dial time.
- **Tenant identity = raw email string** in `owner TEXT` on every table; `users.id` is
  never referenced. Billing tables key on `owner` — **but the email is not normalized**
  (`allowed_emails()` lowercases only for comparison; `current_owner()` returns the raw
  claim), so mixed-case would mint a second tenant with a fresh quota. §5.5 fixes this.
- **Sessions are stateless HMAC cookies** (7-day TTL, no server store) → entitlements must
  be read from the DB per request; never cached in the cookie.
- **Roles are flattened** (`is_admin()` → `False`; deny-by-default `_TENANT_API_PREFIXES`
  allowlist). Any prefix added there is visible to *every* tenant → the admin plane cannot
  live in the main app. That doctrine is why §7 is a separate service.
- **The whole auth wall is conditional on `oauth_enabled()`**, which reads
  `calling-agent/.env` through a best-effort `try/except: pass` dotenv load. Lose that file
  and the API silently becomes an unauthenticated single-tenant app **whose implicit
  identity is the founder** — the account D7 makes unlimited. §16 P-1 adds a boot assertion.
- **Migrations**: versioned PG-only `shared/db/migrations/NNNN_*.sql` via
  `docker exec -w /home/angelo/lescall lescall-workers python -m shared.db.migrate`;
  **next free = 0010**; each needs a `.down.sql`. Core-table ALTERs go in the boot
  `MIGRATIONS` list in `shared/db/database.py`. **Timestamps come back as strings** —
  coerce with `as_dt()`.
- **No Razorpay/plan/subscription code exists anywhere.**

---

## 3. Architecture

```
  Tenant browser ── https://agent.lescall.com ── nginx ── /api → :8080 lescall-api
                                                        └─ /    → :8081 SPA (auth_request wall)
  Razorpay ──────── POST /api/billing/webhook/razorpay   (HMAC signature IS the auth)

  lescall-api (:8080)                          lescall-workers
  ├─ dashboard/billing_api.py  /api/billing/*  ├─ shared/billing/sync.py  ← own supervised
  ├─ shared/billing/entitlements.py            │   process, NOT the 5-min tick (§10)
  ├─ shared/billing/razorpay_client.py         └─ (existing metering/intel workers)
  └─ enforcement at E1-E12 (§7)

  NEW lescall-admin ── uvicorn on a UNIX SOCKET /run/lescall-admin.sock (+ optional
  127.0.0.1:8090) ── no nginx vhost, no DNS ── bearer token required ── ssh -L tunnel
```

Principles:

1. **`subscriptions` = history** (mirror of Razorpay truth); **`entitlements` = resolved
   present**, one row per owner, the only thing the hot path reads. Rebuilt wholesale by
   `resolve_entitlements(owner)` — same doctrine as `usage_monthly`.
2. **Webhooks are authoritative**; the checkout `verify` call is an optimistic fast-path
   that may only advance a `created` subscription (§6.2 — otherwise it's a replay exploit).
3. **Webhook handlers trust only the local `subscriptions` row** matched on
   `razorpay_subscription_id`. `notes` is untrusted display data. An unknown subscription
   id records the event and alarms — it never creates an entitlement.
4. **Enforcement state lives in the DB** (`settings` key `billing_enforce`), not env —
   E5 runs in the *runtime* container, which would never receive a `.billing_env` flag
   (review blocker: inbound enforcement would have been silently dead forever).

---

## 4. Database changes

### 4.1 `shared/db/migrations/0010_billing.sql` (+ `.down.sql`)

Style matches 0001-0009: bare `CREATE TABLE` (the version tracker makes `IF NOT EXISTS`
unnecessary and it masks half-applied state), TIMESTAMPTZ, real FKs, `owner TEXT` = email,
JSON as TEXT. **This migration must not reference `agents.archived_at`** (that column
arrives via the boot list, which `migrate.py` never runs — ordering trap, §5.4).

```sql
CREATE TABLE plans (
    code TEXT PRIMARY KEY,                 -- free|pilot|growth|pro|enterprise
    name TEXT NOT NULL,
    price_paise BIGINT NOT NULL DEFAULT 0,
    currency TEXT NOT NULL DEFAULT 'INR',
    period TEXT NOT NULL DEFAULT 'monthly',          -- monthly | one_time
    duration_days INTEGER,                           -- 7 for pilot; NULL = calendar cycle
    price_includes_gst INTEGER NOT NULL DEFAULT 1,   -- D6
    included_minutes INTEGER,              -- NULL = unlimited
    max_agent_families INTEGER,
    max_concurrent_calls INTEGER,
    copilot_turns_per_day INTEGER,
    -- Feature flags (§1 matrix). One column per marketed capability so the admin
    -- plane can grant them per-tenant for Enterprise without code changes.
    autocall_enabled INTEGER NOT NULL DEFAULT 0,        -- Growth+ and Pilot (D10)
    intelligence_enabled INTEGER NOT NULL DEFAULT 0,    -- all paid tiers
    recordings_enabled INTEGER NOT NULL DEFAULT 0,      -- all paid tiers
    transcripts_enabled INTEGER NOT NULL DEFAULT 0,     -- Growth+ and Pilot (D11)
    advanced_analytics INTEGER NOT NULL DEFAULT 0,      -- Growth+
    topup_allowed INTEGER NOT NULL DEFAULT 0,           -- Basic/Growth (§6.9)
    topup_pack_minutes INTEGER,                         -- 1000
    topup_pack_paise BIGINT,                            -- base, ex-GST (D3)
    self_serve INTEGER NOT NULL DEFAULT 1,
    razorpay_plan_id_test TEXT,
    razorpay_plan_id_live TEXT,
    sort INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Holds BOTH billing models so a tenant's history is one table and
-- resolve_entitlements has one place to look:
--   kind='subscription' → Basic/Growth (recurring, razorpay_subscription_id)
--   kind='one_time'     → Pilot 7-day trial (razorpay_order_id, no mandate)
CREATE TABLE subscriptions (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    plan_code TEXT NOT NULL REFERENCES plans(code),
    kind TEXT NOT NULL DEFAULT 'subscription',   -- subscription | one_time
    status TEXT NOT NULL DEFAULT 'created',
    -- recurring: created|authenticated|active|pending|halted|paused|cancelled|completed|expired
    -- one_time:  created|active|completed        (completed = the 7 days elapsed)
    scheduled INTEGER NOT NULL DEFAULT 0,  -- authenticated with a FUTURE start (upgrade)
    source TEXT NOT NULL DEFAULT 'razorpay',
    razorpay_subscription_id TEXT UNIQUE,
    razorpay_order_id TEXT UNIQUE,         -- pilot only
    razorpay_mode TEXT,
    current_start TIMESTAMPTZ,
    current_end TIMESTAMPTZ,
    charge_at TIMESTAMPTZ,                 -- for the webhook-silence alarm (§10.5)
    cancel_at_cycle_end INTEGER NOT NULL DEFAULT 0,
    verified_at TIMESTAMPTZ,               -- checkout/verify may fire once (§6.2)
    meta TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_subscriptions_owner ON subscriptions(owner);
-- One CURRENT RECURRING subscription per tenant. Excludes `scheduled` rows so the
-- §6.4 upgrade flow (future-dated sub authenticated while the old one is still
-- active) is possible — the review caught these two as mutually exclusive. Also
-- excludes kind='one_time', so a Pilot tenant can buy Basic/Growth mid-trial.
CREATE UNIQUE INDEX uq_subscriptions_owner_current ON subscriptions(owner)
    WHERE kind = 'subscription' AND scheduled = 0
      AND status IN ('authenticated','active','pending','halted','paused');
-- One active Pilot window at a time (belt-and-braces; pilot_claims is the real rule).
CREATE UNIQUE INDEX uq_subscriptions_owner_pilot_active ON subscriptions(owner)
    WHERE kind = 'one_time' AND status = 'active';

-- Pilot-once lives in its own table, claimed AT CHECKOUT inside the same
-- transaction as the `created` row. The old design enforced it via a partial
-- index that excluded `created`, so two concurrent checkouts both created real
-- Razorpay subscriptions and the constraint fired inside the *webhook* — after
-- the customer was charged, with no local record. Append-only: admin reset
-- writes released_by, never DELETEs (history + audit survive).
CREATE TABLE pilot_claims (
    owner TEXT PRIMARY KEY,
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    razorpay_contact TEXT,                 -- scarcer identity than email (§5.5)
    claimed_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    released_by TEXT, released_at TIMESTAMPTZ, release_reason TEXT
);

CREATE TABLE entitlements (
    owner TEXT PRIMARY KEY,
    plan_code TEXT NOT NULL DEFAULT 'free' REFERENCES plans(code),
    minutes_limit INTEGER,                 -- NULL = unlimited
    max_agent_families INTEGER,
    max_concurrent_calls INTEGER,
    copilot_turns_per_day INTEGER,
    autocall INTEGER NOT NULL DEFAULT 0,
    minutes_reserved INTEGER NOT NULL DEFAULT 0,  -- in-flight admissions (§7 note)
    period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'ok',     -- ok|grace|locked  (locked is WIRED, §8.3)
    overrides TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by TEXT
);

CREATE TABLE payments (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    source TEXT NOT NULL DEFAULT 'razorpay',
    razorpay_payment_id TEXT UNIQUE,
    razorpay_invoice_id TEXT,
    amount_paise BIGINT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'INR',
    status TEXT NOT NULL,                  -- captured|failed|refunded
    method TEXT,
    period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,
    receipt_number TEXT UNIQUE,
    billing_snapshot TEXT,                 -- customer GST profile AS OF issue (§6.5)
    refunded_paise BIGINT NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_payments_owner_time ON payments(owner, created_at);

-- GST needs a consecutive serial per financial year; a naive MAX+1 races across
-- the two processes that insert payments (api webhook + admin manual entry).
CREATE TABLE receipt_counters (fy TEXT PRIMARY KEY, last_seq INTEGER NOT NULL DEFAULT 0);

CREATE TABLE billing_events (
    id BIGSERIAL PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,         -- NOT NULL: nullable UNIQUE lets
    event_type TEXT NOT NULL,              -- unlimited NULL rows through and
    payload TEXT NOT NULL,                 -- defeats dedupe entirely
    processed_at TIMESTAMPTZ,
    error TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_billing_events_unprocessed ON billing_events(received_at)
    WHERE processed_at IS NULL;
```

Seed: the 5 plan rows (§1) + **explicit Enterprise entitlements for all three existing
staff tenants** (D6): `tech@dhronai.com`, `angelo@dhronai.com`, `alwynantonyben@dhronai.com`.
Razorpay plan-id columns stay NULL until §11.2's bootstrap.

**Discovery query to run before P6** (the review found the first draft's "founder only"
seed would have blocked two real, active tenants):

```sql
SELECT DISTINCT owner FROM (
  SELECT email AS owner FROM users UNION SELECT owner FROM agents
  UNION SELECT owner FROM usage_ledger UNION SELECT owner FROM calls) t
WHERE owner <> '' AND owner NOT IN (SELECT owner FROM entitlements);
-- MUST return zero rows before enforcement is enabled.
```

### 4.2 Boot-time ALTERs (append to `MIGRATIONS` in `shared/db/database.py`)

```
ALTER TABLE agents ADD COLUMN archived_at TIMESTAMP
ALTER TABLE usage_ledger ADD COLUMN entitlement_check TEXT
```
(`TIMESTAMP` matches every existing entry in that list; the loader stringifies either way.
`entitlement_check` records `ok|error|unattributed` so fail-open minutes are recoverable.)

### 4.3 What deliberately does NOT change
`users` gets no plan column. Customer GST details live in per-owner `settings`
(`key='billing_profile'`). The legacy global `minutes_quota` setting and
`GET/POST /api/minutes/quota` are **retired**.

### 4.4 Backfill script (NOT in the migration)
`shared/billing/backfill_archived.py`, run in P0 **after** the new image is up (so the boot
ALTER has applied): stamps `archived_at` on `agents` rows whose folder now sits under
`calling-agent/agents/_archived/`, and deletes owned rows whose folder is missing entirely
(orphans that would otherwise consume a cap slot forever — see §7 E6).

---

## 5. Backend modules

### 5.1 `shared/billing/`

| File | Contents |
|---|---|
| `entitlements.py` | `billing_owner(routing)` — **the one owner-resolution function** used by both enforcement and metering (§5.3) · `get_entitlements(owner)` · `resolve_entitlements(owner)` · `minutes_used(owner, start, end)` via `display_minutes()` over `COALESCE(billsec_final, billsec_pipeline)` `WHERE billable=1` (open calls count — else a tenant gets up to 24h of free overshoot during CDR wait) · `check_dial_allowed(owner, expected_minutes)` → reserves (§7 note) · `release_reservation(conn_id)` · `check_agent_create(owner, family)` · `check_copilot_turn(owner)` · `autocall_allowed(owner)` · `enforcement_enabled()` (DB `settings`, cached 30s) · **period rule, three cases**: recurring = the subscription cycle; **Pilot = a fixed window `[activated_at, activated_at + 7 days)` from `plans.duration_days`, not a calendar month** — 8,000 minutes is the whole-window pool, not per-month; Free/Enterprise = IST calendar month (`Asia/Kolkata`, `as_dt` coercion everywhere) |
| `razorpay_client.py` | httpx wrapper, **explicit 10s total timeout**. Recurring: `create_plan`, `create_subscription`, `fetch_subscription`, `patch_subscription`, `cancel_subscription`, `fetch_invoices`. One-time (Pilot): `create_order(amount_paise, receipt, notes)`, `fetch_payment`. **Two signature verifiers — opposite field order, this is the single easiest way to break both flows:**<br>`verify_subscription_signature` = `HMAC(payment_id + "\|" + subscription_id)` — **payment id first**<br>`verify_order_signature` = `HMAC(order_id + "\|" + payment_id)` — **order id first**<br>both constant-time against the id **stored locally**, never the echoed one. Plus `verify_webhook_signature(raw_bytes, header_sig)` with the per-mode webhook secret |
| `receipts.py` | FY serial via `receipt_counters` + `SELECT … FOR UPDATE` in the payment transaction; FY from `created_at` in IST (`LC-25-26-00001`); GST math per D6; **GSTIN validated (15-char regex + mod-36 check digit) and `state_code` derived from its first two digits — never accepted separately**; no/invalid GSTIN → B2C, place of supply = platform state; snapshot written to `payments.billing_snapshot` so later profile edits can't rewrite an issued invoice |
| `sync.py` | §10's supervised pass |
| `plans_bootstrap.py` | idempotent Razorpay Plan creation per mode |
| `backfill_archived.py` | §4.4 |

Self-disables on SQLite (versioned tables are PG-only), like the intelligence layer.

### 5.2 Router `dashboard/billing_api.py` — §6. Add `"/api/billing"` to
`_TENANT_API_PREFIXES`; every endpoint `Depends(auth.current_owner)`. Mount with an
explicit boot log line — the existing try/except mount pattern would otherwise let a
broken billing router vanish silently.

### 5.3 One owner-resolution function (review blocker)

Enforcement and metering would have resolved the tenant differently:
`_resolve_call_routing` returns `owner=""` for any slug with no `agents` row (it even
swallows DB errors into `""`), while `_metering_owner()` falls back to
`LESCALL_DEFAULT_OWNER` — the account D7 makes **unlimited**. Net effect: such calls check
quota against an empty-string tenant whose usage never accumulates, and bill minutes to an
unlimited account. A self-serve free-minutes switch.

Fix: `billing_owner(routing)` is the single source for both. **An unattributable call is
refused, not defaulted** — answer with the neutral prompt, hang up, write a `billable=0`
ledger row stamped `entitlement_check='unattributed'`, and raise a critical notification.
Unattributed usage must never land in an unlimited entitlement.

### 5.4 Agent-count correctness
`DELETE /api/agents/{name}` archives the folder but leaves the `agents` row owned — a naive
`COUNT(*)` counts dead agents forever. Fix: `archive_agent` sets `archived_at`; all counting
(cap check *and* `/api/billing/summary`) filters `archived_at IS NULL`; `_assert_slug_free`
treats an archived row as claimable.

### 5.5 Identity normalization
Lowercase + NFC-normalize the email in `_upsert_user`, `make_session_cookie` and
`current_owner`; one-time data fix for existing mixed-case rows. Bind the pilot claim to
the Razorpay customer's verified contact number as well as the email
(`pilot_claims.razorpay_contact`). Multi-Google-account Free abuse is **accepted-with-limits**
(low minutes, 1 agent, no autocall, per-IP signup rate limit), not prevented — stated
plainly rather than pretended away.

---

## 6. Tenant API (`/api/billing`)

### 6.1 Endpoints
| Endpoint | Behavior |
|---|---|
| `GET /summary` | plan, entitlements, period, minutes used/remaining (same numbers enforcement uses), agent families used/cap, concurrency + copilot allowances, subscription status, renewal date, pilot-used flag, tenant-visible state banner (§6.6) |
| `GET /plans` | active self-serve plans (never hardcoded in the SPA) |
| `POST /checkout` | `{plan_code}` → **branches on `plans.period`**.<br>**Pilot (one_time)**: refuse if `pilot_claims` has an unreleased row **or** the tenant already holds/has held any paid plan (trial is for newcomers); `create_order(299900, receipt=…, notes={owner,'pilot'})`; insert `subscriptions(kind='one_time', status='created')` **and** claim `pilot_claims` in the same transaction; return `{order_id, key_id, prefill, plan}`.<br>**Basic/Growth (subscription)**: refuse if a current recurring sub exists (→ §6.4); **cancel a lingering halted/paused own-sub first** (else the unique index blocks re-subscribe forever — §6.7); `create_subscription`; return `{subscription_id, …}`. An active Pilot does **not** block this — buying mid-trial is allowed (§6.8).<br>Both: one `created` row per owner max (reuse/expire the previous), rate-limited 5/hour/owner + global daily ceiling with alarm, `prefill` built **strictly from the `users` row + owner phone in `settings`** — never from the request body |
| `POST /checkout/verify` | **strict state transition** for both flows: only when the stored row is `status='created'`, `owner` matches the session and `verified_at IS NULL`; else `200 {"status":"already_processed"}` and change nothing. (Otherwise a tenant replays their own old signature forever to resurrect a dead row and re-grant a paid plan.) Picks the verifier by `kind` — **order vs subscription HMAC field order differs** — and checks against the **locally stored** id. On a verified Pilot: stamp `current_start=now`, `current_end=now+7d`, `status='active'`, resolve entitlements |
| `POST /cancel` | `cancel_at_cycle_end=true` + local flag (catch "already in final cycle") |
| `POST /change` | §6.4 |
| `GET /payments` | history, paginated, with `receipt_number` and refund state |
| `GET /receipts/{payment_id}` | owner-scoped (foreign id → 404) |
| `GET/PUT /profile` | `billing_profile` KV; GSTIN validated server-side (§5.1) |

**Refusal shape** — 402 carrying *both* dialects, because `outbound_dial` answers
`{"ok": false, "reason": …}` and the Outbound UI reads `reason`; a bare `detail` body
renders as a generic failure and nudges users toward the Force-call button:
```json
{"ok": false, "reason": "<human message>", "detail": "<same>",
 "code": "quota_minutes|cap_agents|quota_copilot|cap_concurrency|plan_required|account_locked",
 "upgrade": "/billing"}
```
Never contains `duplicate: true` (that's the 409 duplicate-dial shape — `?force=1` must not
bypass billing). `frontend/src/api/client.ts` gains `err.code`/`err.upgrade` (today it only
attaches `err.status` and folds `detail` into the message).

### 6.2 Webhook `POST /api/billing/webhook/razorpay`
- Add the **exact path** to `_OPEN_PREFIXES` — and change that gate to support exact
  matches (`_OPEN_EXACT`, compared with `==`): it currently uses `startswith`, which
  short-circuits both the session requirement *and* the tenancy gate, so a future
  `…/razorpay-test` route would be silently unauthenticated.
- **Reject on `Content-Length` before reading** (cap 256 KB → 413), stream-read with a hard
  cap rather than `await request.body()`. Unbounded buffering on an unauthenticated,
  un-rate-limited endpoint in the same process that serves every dashboard and dial request
  is an OOM lever on a 2-core VM. Also: nginx `limit_req` on that location,
  `client_max_body_size 256k`, restrict to Razorpay's published source ranges.
- Verify HMAC over raw bytes → `INSERT … ON CONFLICT (event_id) DO NOTHING`; conflict →
  already processed → `200`. Missing `X-Razorpay-Event-Id` → 400 + alarm.
- **Handlers may never raise after the event is marked seen** (Razorpay dedupes its own
  retry, so a raise strands a real payment). Upsert only; on any impossible conflict record
  `error`, leave `processed_at` NULL, cancel+flag the duplicate subscription at Razorpay,
  raise a critical notification.
- `subscription.charged` is idempotent independent of the dedupe row: allocate the receipt
  number **only when the payments INSERT actually inserted**
  (`ON CONFLICT (razorpay_payment_id) DO NOTHING RETURNING id`); derive `current_end` from
  the payload rather than incrementing; verify `payment.amount == plans.price_paise`.

### 6.3 Events handled
**One-time / Pilot**: `payment.captured` and `order.paid` — match on `razorpay_order_id`,
upsert the `payments` row (idempotent on `razorpay_payment_id`), allocate the receipt,
open the 7-day window (`status='active'`), resolve entitlements. Webhook is authoritative:
if `verify` never fired (user closed the tab after paying) the window still opens here.
**Recurring**: `subscription.authenticated` (set `scheduled=1` if `start_at` is future) · `activated` ·
`charged` (money + receipt + notification) · `pending` (retrying banner) · `halted`
(→ `status='grace'`, 72h, urgent notification) · `cancelled|completed|paused|resumed|updated` ·
`payment.failed` (dunning row) · **`refund.processed`/`payment.refunded` → flip
`payments.status='refunded'`, set `refunded_paise`, surface in history, flag that a GST
credit note is needed** (the first draft subscribed to no refund events while its own
go-live drill refunded a payment) · `invoice.paid` → explicit no-op ack (subscribed but
unhandled events would otherwise sit unprocessed forever and trip the alarm).

### 6.4 Plan changes
Razorpay `PATCH /subscriptions/:id` works **only for card-token subs; UPI-Autopay and
eMandate subs cannot be updated at all**, and Razorpay never auto-prorates. v1, uniform:
current sub gets `cancel_at_cycle_end=true`; new sub created with `start_at = current_end`
and `scheduled=1` (excluded from the unique index — this is why §4.1 has that column);
entitlements switch on its `activated`. Cancel-to-Free = cycle-end downshift by sync.
Out of v1: mid-cycle proration, card-only in-place `PATCH` upgrades.

### 6.5 Receipts
Ours is the **GST tax invoice** (Razorpay's auto-invoice is only a payment receipt — no
customer GSTIN, SAC, or CGST/SGST split, which B2B tiers at ₹8,999/₹19,999 need). SAC
998314; CGST+SGST vs IGST from the GSTIN-derived state code; snapshot frozen at issue.

### 6.6 Subscription state × what the tenant sees (was undefined for half the states)
| Sub status | entitlements | `/billing` banner |
|---|---|---|
| created | unchanged | "Complete payment to activate" |
| authenticated (scheduled) | current plan | "Switching to X on {date}" |
| active | plan | renewal date |
| pending | plan (kept) | amber "Payment retrying — update method" |
| halted | `grace` 72h | rose "Payment failed — plan ends {date}" |
| paused | `grace` | "Subscription paused" |
| cancelled (cycle-end) | plan till `current_end` | "Ends {date}, then Free" |
| completed | Free | "Subscription completed — resubscribe" |
| expired | Free | "Authorization expired — try again" |
| **Pilot active** | Growth-level features, 500-min pool | "Trial — {n} days and {m} minutes left" + Basic/Growth CTAs from day 1 |
| **Pilot last 48h** *(or <20% minutes left)* | unchanged | amber "Trial ends {date} — pick a plan to keep your agents running" (+ notification at T-48h and T-6h) |
| **Pilot completed** | Free (30 min, 1 agent; existing agents keep working — D9) | "Trial ended — choose Basic or Growth" ; Pilot card gone for good |
| Enterprise past `period_end` | `grace` N days → **`locked`** (never silently Free — don't strip a custom config) | "Renewal due — contact us" |

### 6.7 The halted trap (review blocker)
Grace expiry downgraded entitlements but left the row `halted` — which sits inside the
uniqueness index, so the tenant could **never check out any plan again**, and §11.4 says
halted is the *expected monthly path* for card-based Growth if D1 keeps it above the AFA
ceiling. Fix: on grace expiry sync calls
`cancel_subscription` (immediate) so the row becomes `cancelled` and frees the index;
checkout also cancels a lingering halted/paused own-sub defensively. Tested end-to-end
(§12).

### 6.8 Pilot lifecycle & downgrade policy
- **Expiry is time-based, not event-based** — no webhook announces it. Sync (§10) closes
  any `kind='one_time'` row whose `current_end` has passed → `status='completed'` →
  `resolve_entitlements` → Free. **This must also be enforced on read**: `get_entitlements()`
  treats an elapsed window as expired even if sync hasn't ticked yet, so a lapsed trial can
  never keep dialing in the gap between passes.
- **Exhausting 8,000 minutes before day 7** behaves like any quota exhaustion (hard block
  per D5 + upgrade CTA); the window itself keeps running.
- **Buying Basic/Growth mid-trial** is allowed and encouraged: the new subscription supersedes
  the trial when it activates, the pilot row closes early (`completed`), and no refund is
  due — one-time purchase, stated plainly in the checkout copy.
- **Downgrade never destroys work (D9)**: agents over the new cap keep running and keep
  taking calls; only *new* creates are refused until the tenant is under cap. Applies to
  Pilot→Free (3→1) and Growth→Basic alike. Autocall, by contrast, **does** stop
  immediately on a plan without it (E3 dial-time gate + E8 staging gate).
- **Once ever**: `pilot_claims` survives expiry, cancellation and re-signup with the same
  email, and is claimed **at checkout, not at payment** — so parallel Pilot checkouts can't
  burn two windows.

### 6.9 Additional minutes ("+ Additional minutes available")
Basic and Growth can buy top-up packs mid-cycle. This reuses the **Pilot rail**, which is
the main reason that rail is worth building: a pack is a one-time Razorpay **Order**, not a
subscription change — no mandate, no proration, no plan migration, instant effect.

```sql
CREATE TABLE minute_topups (
    id BIGSERIAL PRIMARY KEY,
    owner TEXT NOT NULL,
    subscription_id BIGINT REFERENCES subscriptions(id) ON DELETE SET NULL,
    payment_id BIGINT REFERENCES payments(id) ON DELETE SET NULL,
    razorpay_order_id TEXT UNIQUE,
    minutes INTEGER NOT NULL,
    amount_paise BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',   -- created|active|expired|refunded
    period_start TIMESTAMPTZ, period_end TIMESTAMPTZ,  -- mirrors the cycle it belongs to
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_topups_owner_period ON minute_topups(owner, period_end);
```

- `POST /api/billing/topup` `{packs: n}` → Order → same verify/webhook path as Pilot;
  `status='active'` on capture.
- **`minutes_limit` becomes `plan.included_minutes + SUM(active topups for this period)`**
  — computed inside `get_entitlements()`, so every enforcement point (E1–E6) and the
  Overview/Topbar meters pick it up with no extra code.
- **Packs expire with the cycle** (`period_end` copied from the subscription at purchase) —
  they do not roll over; the checkout copy must say so plainly. On plan change the
  remaining packs follow the tenant to the new cycle only if the change is an upgrade;
  state this in the copy either way.
- Refunds flow through the same `payments` path (`status='refunded'` → topup `refunded` →
  entitlements recomputed).
- Not offered on Free (no card on file) or Pilot (a fixed trial — upgrade instead); the
  100%-quota block (D5) shows "Buy more minutes" only where `plans.topup_allowed`.

---

## 7. Enforcement — E1…E12

All read `shared.billing.entitlements` and no-op when `enforcement_enabled()` is false.
**Gate rule for §12:** every call site of `_fs_esl_exec(_fs_originate_cmd(...))` must be
preceded by an entitlement check — today `server.py:3648, 3866, 4218, 4287, 4389` plus
`shared/sentinel.py:497`.

| # | Origin | Where | Note |
|---|---|---|---|
| E1 | Manual dial | `server.py::outbound_dial` (~4165) | after the duplicate guard, before `_outbound_identity` |
| **E2** | **Campaign start** | `server.py::campaign_start` (**4323**) + inside `_run()` before each `_record_dial` (~4389) | **The review found this sixth origin missing entirely.** It loops every matching contact, has **no duplicate guard**, and takes `delay_seconds` from the body (0 accepted) — a Free tenant could dial 5,000 contacts with the 30-min quota never consulted. Also clamp `delay_seconds ≥ 2`, cap `total` by remaining minutes, stop the campaign with reason `quota_minutes` surfaced in `/campaign/status` |
| E3 | Auto-dialer | `server.py::_autodial_once` (~3762) | beside the existing `placed_today` cap; skip owner if `not autocall_allowed`. `shared/intelligence/autodial.py` stays a pure predicate — pass `quota_ok`/`autocall_ok` in, add matching transient reason codes so `explain()` stays truthful |
| E4 | Automation callback | `server.py` ~3645 | before `_record_dial` |
| E5 | Test call | `server.py::number_test_call` (~4235) | meters identically |
| E6 | **Inbound** | `conversation_call_bridge.py::_resolve_call_routing` (370) / `create` (641) | owner via `billing_owner()` (§5.3); exhausted → refusal prompt + hangup + `billable=0`. **Fail-open is bounded, not blind**: use a short-TTL cached entitlement on DB error; still bill (`billable=1`, `entitlement_check='error'`); after 5 consecutive fail-opens per owner refuse + critical notification; counter on `/api/health` for the sentinel. (Unbounded fail-open + `max_connections=30` and no pooler = a tenant who exhausts connections gets free calls for everyone, benefiting from the outage they caused.) |
| E7 | Agent creation | **pre-check in `create_agent` / `create_twin` / copilot's `create_agent` tool, before any filesystem write**, with `_claim_slug` as backstop | The first draft put it only in `_claim_slug`. Verified problems: `write_agent_dir` wraps it in `except Exception` → re-raises **500**, so the SPA's 402 handling never fires; the copilot wraps it into a `ValueError` string; and the per-slug claim inside the twin loop means the inbound slug's row is inserted, the outbound raises, and rollback does `rmtree` **without deleting the claimed row** — leaving an owned, folder-less row that eats a cap slot forever and 404s on archive. Fixes: `BillingRefusal(HTTPException)` re-raised untouched (`if isinstance(exc, HTTPException): raise`) in both handlers; rollback also `DELETE FROM agents WHERE slug=? AND owner=?`; wrap count+claim in `pg_advisory_xact_lock(hashtext(owner))` (a bare count-then-write is TOCTOU — two concurrent creates both see `0 < 1`); also gate the restore-shaped paths `rollback/{rev_id}` (673) and `apply-yaml` (870), which bypass `_claim_slug` |
| E8 | Autocall staging | `shared/intelligence/followup.py::stage_followup` (~385) | only stamp `mode='auto'` when `autocall_allowed`; E3 is the safety net for rows stamped before a downgrade |
| **E9** | **Copilot chat** | `agent_copilot_api.py::copilot_chat` (4752) | **New.** LLM spend is unmetered today; per project memory `COPILOT_LLM_TPM` and the daily model quota are **shared with live calls**, so one Free tenant can degrade paying tenants' phone calls at zero cost. Daily turn budget + rate limit; `code="quota_copilot"` |
| **E10** | **Copilot try-agent** | `agent_copilot_api.py` (5194) | spins a real `ConversationRouter`, up to 30 turns per request |
| **E11** | **Doc extraction** | `agent_builder_api.py` (752, 856) | LLM extraction on tenant-uploaded files |
| **E12** | **Feature flags** (new — the tiers now differ by capability, not just volume) | one dependency on the `/api/intel` router + per-route checks | `intelligence_enabled` gates the `/api/intel` prefix (Free only); `transcripts_enabled` gates `/api/calls/transcripts` + summary fields in intel responses (Basic excluded, D11); `recordings_enabled` gates `/api/calls/recordings` + the tokenized download links; `advanced_analytics` gates `/api/intel/analytics/conversation`. **Gate at the API, not just the SPA nav** — hiding a nav item is not enforcement. Post-call *processing* still runs for every tier (it feeds follow-ups and the founder's own analytics); only tenant-facing exposure is gated, so an upgrade retroactively reveals history rather than leaving a gap |

**Concurrency (review major).** `outbound_dial` returns immediately and originates in
`asyncio.create_task`, and the duplicate guard is keyed *per phone* — so N parallel POSTs to
N different contacts all pass a `remaining=1` check in the same instant. Overshoot is
**unbounded, not single-call**. Fix: admission is a **reservation**, not a read — bump
`entitlements.minutes_reserved` in the same transaction as the check, release at ledger
close, refuse when `used + reserved >= limit`; plus the per-plan `max_concurrent_calls` cap
(§1) at every dial origin and E6, and a per-owner rate limit on dial/campaign endpoints
(there is no rate limiting anywhere in the codebase today).

`/api/overview` and `/api/usage/minutes` switch to entitlement-derived per-tenant numbers
(the SPA's `OverviewT` already types `{total_minutes, remaining_minutes}`).

---

## 8. Super-admin plane

### 8.1 Placement
`dashboard/admin_app.py`, own FastAPI app. **Primary bind: a UNIX domain socket**
(`uvicorn --uds /run/lescall-admin.sock`) — an HTTP-URL SSRF (S4) cannot reach a UDS at
all; optional `127.0.0.1:8090` (verified free; avoid 9002 `STT_FORWARD_PORT`, 8082 FS ws,
8000 CLI console) for convenience, off by default. Compose service `admin` (container
`lescall-admin`), shared image, `network_mode: host`, DB pool ≤2 (`max_connections=30`).
**No nginx vhost, no DNS, no GCP firewall opening.** Access:
`ssh -L 8090:127.0.0.1:8090 angelo@35.207.225.237` (or forward the socket). Boot assertion:
refuse to start if bound to anything but the socket/loopback.

### 8.2 Defense in depth — honest version
1. **Socket/loopback bind.** There is **no host firewall** (`iptables ACCEPT`, no ufw); GCP
   VPC is the only outer wall and unreadable from the VM. Loopback is shared with every
   host-network container.
2. **Bearer token, mandatory.** S4 (tenant-reachable SSRF from a host-network container)
   means the token is **the only wall**, not a second one. Therefore: refuse to start if
   `ADMIN_TOKEN` is missing or <32 bytes (no "unset = open" path); accept it **only** from
   the `Authorization` header; per-operator tokens with expiry; log every accepted request
   with source. Independently **fix the SSRF**: allowlist `_webhook` URLs to https + public
   IPs, block RFC1918/loopback/link-local after DNS resolution, cap redirects.
3. **Secrets must leave the bind mount.** Every app container mounts the whole repo as
   uid 1000 — the owner of `deploy/.pg_env`. So `.admin_env`/`.billing_env` under the repo
   would be readable by the api *and* runtime *and* workers regardless of `env_file`,
   making both "runtime never sees payment secrets" and "the token means on-the-box-plus-
   secret" false. **New invariant: secrets live in `/etc/lescall/{admin,billing}.env`,
   root-owned 0600, referenced by `env_file:` and mounted into nothing. Never put a secret
   under /home/angelo/lescall.**
4. Cookie-free → CSRF-immune. Audit every mutation to `audit_events` (`admin.*`) with
   before/after + mandatory reason; **also log reads** (path + owner queried + source) —
   if a token leaks, reads are what an attacker uses first.
5. Nothing admin-shaped is mounted on :8080 or shipped in the SPA bundle.
6. Razorpay keys stay in env (Sarvam-keys doctrine: payment credentials are platform infra,
   never DB rows, never per-tenant, never in a UI).

### 8.3 Capabilities (`/admin/api`, server-rendered Jinja UI — no React, no build step)
Tenants list/detail (users ⋈ entitlements ⋈ live sub ⋈ month usage ⋈ agent count) ·
`POST /tenants/{owner}/plan` (set/remove/change any tier incl. Enterprise custom values;
`force:true` required while a Razorpay sub is live) · `POST /tenants/{owner}/manual-payment`
(Enterprise: inserts `payments(source='manual')`, allocates receipt, extends period) ·
`POST /tenants/{owner}/subscription/cancel` · **`POST /tenants/{owner}/lock` / `/unlock`**
(wires `entitlements.status='locked'` — it was a dead enum value, and "manage users" needs a
way to suspend an abusive account now that signup is open) · `POST /tenants/{owner}/pilot-reset`
(**append-only**: writes `released_by`/`reason` ≥10 chars, never DELETEs; requires
`confirm_owner` equal to the path owner so a body-less SSRF POST can't trigger it) ·
`GET /billing-events`, `/payments`, `/health`.

### 8.4 Ops
RUNBOOK **§15 Super-admin plane** (tunnel, token rotation — remember *compose restart ≠ env
apply*, audit queries) and **§16 Billing ops** (webhook-secret rotation and its two-secret
window, `plans_bootstrap` immutability rules, live-key cutover, dunning drill, refund +
credit-note procedure). Sentinel watches `lescall-admin` at severity=warn (admin down never
blocks tenant traffic).

---

## 9. Frontend

Lazy `/billing` route in the **Account** nav group; `frontend/src/api/billing.ts` mirroring
`api/agents.ts`; all I/O through `api/client.ts` + `api/mutate.ts`. Cards: current plan +
usage meters (amber ≥80%, rose at 100%) + state banner (§6.6); plans grid from
`GET /plans` (Pilot marked "already used" when consumed; Enterprise = contact card);
payment history via kit `Table` + `Pagination` with `inr()`; billing profile form.
**Trial state is its own card while Pilot is active**: days-and-minutes-left countdown
(both can end it — whichever comes first), a Growth-features note, and Basic/Growth CTAs visible from
day 1; it turns amber in the final 48h. `checkout.js` loaded **only on this page** (dynamic
script tag + load-failure toast) → `POST /checkout` → Razorpay modal (opened with
`order_id` for Pilot and top-ups, `subscription_id` for Basic/Growth — **the SPA branches on what
`/checkout` returned**) → `POST /checkout/verify` → poll `GET /summary`.
Receipts: lazy print view (`window.print()` → PDF; works on phone/iPad).

Gates in existing UI: `AgentsList` New-Agent button disabled-with-reason at cap (+ note
that archiving frees a slot); `CreateAgent` renders an upgrade panel on 402 (using the new
`err.code`); Copilot needs no frontend gate (server-side E7/E9 text arrives as the chat
reply); Topbar minutes pill becomes `used / limit` and links to `/billing`; Overview gains
an upgrade chip at ≥80%. Quota/payment/grace/renewal alerts reuse
`/api/intel/notifications` with `link:'/billing'` — **zero Topbar changes needed**.
Design-system rules apply as written in CLAUDE.md (closed type scale, radius law,
`AsyncState`, `useConfirm`, `IconButton` labels, shadcn@2 only).

---

## 10. Workers — sync & dunning

**Its own supervised background process** inside `lescall-workers`, exactly like the intel
worker — **not** the 5-minute tick. (The first draft added it to the tick while noting that
sentinel restarts the container if `reconcile:` disappears for 20 min; N live Razorpay
calls in a sequential loop makes tick duration a function of Razorpay latency and
subscriber count → sentinel restart → kills the intel worker mid-job → restart loop, with
billing reconciliation stopped.) 15-min interval, 10s per-call timeout, 120s wall-clock
budget per pass, batched by staleness.

Passes: (1) reconcile non-terminal subs against Razorpay; (2) grace expiry → **cancel at
Razorpay** (§6.7) → Free + notification; (3) cycle-end downshifts; (4) stale `created` rows
→ `expired`; **(4b) Pilot window expiry** — `kind='one_time'` rows past `current_end` →
`completed` → Free + "trial ended" notification, plus the T-48h/T-6h heads-up notifications
and a **Pilot-burn alarm at 80% of its 500 minutes** (so a trial that will convert — or
churn — is visible early); **(4c) top-up expiry** — `minute_topups` past `period_end` →
`expired` → entitlements recomputed; (5) **webhook-silence alarm** — active sub whose `charge_at` passed >36h with
no `billing_events` row (catches Razorpay's auto-disable-after-24h-of-failures);
(6) **errored-event alarm + reprocess** — `billing_events WHERE error IS NOT NULL AND
processed_at IS NULL` older than 15 min (payloads are stored verbatim, so replay is
possible; Razorpay will never retry a deduped event); (7) Enterprise `period_end` expiry →
grace → locked; (8) D-2 renewal heads-up. **Alarms go to the workers logline that
sentinel/GCS logging already watch, not only the in-app bell** — nobody sees a bell dot.

---

## 11. Razorpay setup guide

### 11.1 Account
KYC-verified account → enable **Subscriptions** product → Account Settings → **GST
details** (platform GSTIN) → enable cards, **UPI Autopay**, and **eMandate/eNACH**
(eMandate matters for Growth, §11.4).

### 11.2 Test mode
1. Test API keys → `/etc/lescall/billing.env`.
2. `python -m shared.billing.plans_bootstrap` (mode=test) creates **only the two recurring
   Plans** (Basic and Growth) at `price_paise × 1.18` since prices are GST-exclusive, and
   stores ids in `plans`. **Razorpay Plans are
   immutable forever** — a price change means a new plan_id + subscriber migration, so
   triple-check before live. **Pilot needs no Plan**: it's an Order created per purchase,
   so its price lives solely in `plans.pilot.price_paise` and can be changed freely.
3. Webhook → `https://agent.lescall.com/api/billing/webhook/razorpay` with its **own test
   secret** (≠ API secret; live secret differs again); events: all `subscription.*`,
   `payment.failed`, `refund.processed`, `invoice.paid`.
4. Drills — **run both flows, they share almost no code path**:
   - **Pilot (Order)**: checkout with a test card and with `success@razorpay` UPI → verify
     the 7-day window opens → force-expire by moving `current_end` back in the DB → confirm
     the drop to Free, that existing agents still work (D9) and that Pilot never reappears.
   - **Recurring (Subscription)**: checkout → Dashboard **"Charge this Now"** to force
     success **and** failure → walk `pending → halted → grace → Free → re-subscribe`
     deliberately (§6.7). Test card tokens live only **3 days** — simulate promptly.

### 11.3 Go-live
Live keys → `plans_bootstrap` live → live webhook (new secret) →
`docker compose up -d api workers admin` (**`up -d`, not `restart`** — env changes) →
`RAZORPAY_MODE=live` with enforcement still off → one real ₹2,999 Pilot purchase on a staff
account to prove the loop (refund via dashboard — refund ingestion is wired, §6.3) → then
flip enforcement (§16 P6). Settlements T+2 working days; fees ≈2% PG + ~0.9-1% recurring —
confirm contracted rates with Razorpay sales.

### 11.4 ⚠ The ₹15,000 AFA ceiling now lands on **Growth** — a pricing decision, not just a technical one
RBI's E-Mandate Framework 2026 keeps AFA-free auto-debit at **₹15,000 per transaction**
for general merchants (the ₹1 lakh band covers only MF / insurance / credit-card bills —
SaaS is excluded). **The ceiling applies to the gross amount charged, GST included.**

With prices GST-exclusive, that puts the break-even at **₹12,711 ex-GST** (× 1.18 =
₹14,999):

| Tier | Ex-GST | Gross charged | Auto-debit behaviour |
|---|---|---|---|
| Pilot ₹2,999 | — | ₹3,538 | **N/A — one-time Order, no mandate at all** |
| Basic ₹8,999 | | ₹10,619 | ✅ Silent monthly debit (customer just gets the mandated ≥24h pre-debit notice) |
| **Growth ₹19,999 (proposed)** | | **₹23,599** | ⚠ **Above the ceiling.** Every month the bank sends a 72h AFA link; if the customer ignores it → `pending` → 3 daily retries → `halted` |

Your "Most Popular" tier is the one that would need a customer action every single month.
Three ways out, pick one in D1:

1. **Price Growth at ≤ ₹12,711 ex-GST** (e.g. ₹12,499 → ₹14,749 gross) — fully silent
   renewals, best conversion and retention, lowest support load. Costs revenue per seat.
2. **Keep ₹19,999 and push eMandate/eNACH** — Razorpay's eMandate (bank-account debit on
   the NACH rail) supports up to ₹99,999/debit with **no per-transaction AFA**; make it the
   default method on Growth checkout, keep cards/UPI available with explicit "you'll
   approve each month" copy.
3. **Keep ₹19,999 on cards/UPI and accept monthly AFA** — then dunning is not an edge case,
   it's the expected monthly path, and §6.7 (the halted trap) becomes load-bearing.

My recommendation: **(2)** — it preserves the price and the silent renewal, at the cost of
a slightly heavier first-time authorization. Whichever you choose, the plan already handles
`pending → halted → grace → Free → re-subscribe` end-to-end.

### 11.5 Env
`/etc/lescall/billing.env` (root 0600, **outside the repo**): `RAZORPAY_MODE`,
`RAZORPAY_KEY_ID` (public — ships to checkout), `RAZORPAY_KEY_SECRET`,
`RAZORPAY_WEBHOOK_SECRET`, `BILLING_GRACE_HOURS=72`, `PLATFORM_GSTIN`,
`PLATFORM_LEGAL_NAME`, `PLATFORM_ADDRESS`. `/etc/lescall/admin.env`: `ADMIN_TOKEN` only.
Attached to api + workers + admin. **Enforcement state is a DB setting, not env** (§3.4).

---

## 12. Testing

- **Unit**: **both HMAC field orders — an order signature must FAIL subscription
  verification and vice versa** (the single easiest way to break this) · webhook raw-body
  HMAC (incl. tampered body) · **Pilot window math**: opens on verify/webhook, expires
  exactly 7×24h later, `get_entitlements` treats an elapsed window as expired *without*
  waiting for sync, DST-free IST handling · **`plans.pilot` caps equal `plans.pro` caps**
  (so a future Growth change can't silently leave the trial behind) · **top-up math**: pack
  adds to the period pool, expires with the cycle, refund removes it · **feature flags E12
  enforced at the API, not just hidden in nav** · pilot mid-trial upgrade
  closes the one-time row · **downgrade keeps over-cap agents callable but refuses new
  creates** (D9) ·
  entitlement matrix (plan × override × status) · IST period math + string-timestamp
  coercion · `display_minutes` parity between quota check and `/api/usage/minutes` ·
  **scheduled-upgrade dual-row vs the unique index** (would have caught the §4.1/§6.4
  blocker) · pilot claim race (two concurrent checkouts → one wins, one Razorpay sub) ·
  agent-family counting (twin = 1, archived excluded, legacy slugs) · **two concurrent
  creates against cap=1** · reservation math · receipt FY rollover (Mar 31 → Apr 1 IST) +
  concurrent allocation · GST matrix (intra-state, inter-state, B2C no-profile) + GSTIN
  checksum · refund ingestion · `_OPEN_EXACT` (`/api/billing/webhook/razorpayX` and
  `/summary` both 401 without a session). Use the `stub_llm` fixture; billing tests never
  touch live Razorpay.
- **Integration** (test mode): **Pilot order → 7-day window → expiry → Free → Growth
  purchase → Pilot never offered again** · checkout happy path · Charge-this-Now success/failure ·
  **halted → grace → Free → re-subscribe round trip** · cancel-at-cycle-end ·
  upgrade-at-cycle-end · webhook replay (same event twice → one effect) · out-of-order
  (charged before activated) · Enterprise manual payment → receipt → period extension →
  expiry → grace → locked.
- **Enforcement**: E1–E12 with Free/Pilot/Basic/exhausted/locked tenants · **every feature
  flag denied at the API, not merely hidden in nav** · E6 refusal leaves
  `billable=0` · 402 never carries `duplicate:true` and the SPA offers no force-retry for
  `quota_*`/`cap_*` · **repo-wide grep gate**: every `_fs_esl_exec(_fs_originate_cmd(...))`
  site is preceded by a check · `enforcement_enabled()` is True **inside the runtime
  container** after the flip (exposed on `/api/health` so the sentinel can alarm on
  disagreement).
- **Admin plane**: 401 without token · refuses to boot with a weak/absent token · bound to
  socket/loopback only (`ss -tlnp`) · **SSRF from the app cannot reach it** · every mutation
  audited · pilot-reset needs `confirm_owner`.
- **Network acceptance (from an off-VM host)**: `nc -z <ip> 8021 9000 9001 8080 8090` — all
  must fail.
- **Acceptance floor**: `test_kaapd_e2e.py` stays green — billing must never break call
  flow; specifically E6 fail-open on DB errors.

---

## 13. Open items
**Blocking the build: D1 prices · D2 agent caps · D3 top-up pricing** (§0.3) — Razorpay
Plans are immutable once created, so D1 must be settled before `plans_bootstrap` runs in
live mode. Non-blocking: D4–D11 · mid-cycle proration (v1: none) · `billing_events` payload
retention (suggest 24 months) · SEZ/zero-rated customers (v1: "contact us" on the profile
form) · receipt-number global sequence discloses transaction volume (GST wants a
consecutive series — accept, or scope per-owner if your auditor agrees) · whether
Enterprise gets platform receipts for manual payments (plan says yes) · whether top-up
packs survive a mid-cycle upgrade (§6.9 says yes on upgrade only — confirm the copy).

## 14. Future work
Card-only in-place upgrades with addon proration · **auto-top-up when minutes run low**
(the pack rail makes this a small addition, and it removes the hard-block cliff) · annual
plans (would also sidestep Growth's AFA problem entirely) · usage-based overage instead of
packs · self-serve Enterprise invoices · coupons (`offer_id` plumbing already in checkout)
· plan analytics in the admin plane · per-tier concurrency tuning once real load is known.

---

## 15. Effort estimate (rough, engineer-days)
P-1 security 1.5 · DB+entitlements 2 · Razorpay client+webhook 3 (two flows: Orders +
Subscriptions + top-up packs) · checkout+verify+topups 2.5 · enforcement E1–E12 3.5
(feature flags added) · admin plane 2.5 · SPA billing+receipts+trial UI+topups 4 ·
sync/dunning/pilot+topup expiry 2 · tests 4 · docs/rollout 1 → **≈26 days**, ~5 weeks with
review and drills.

---

## 16. Rollout

| Phase | Contents | Gate |
|---|---|---|
| **P-1** | **Security prerequisites, before any billing code.** S1: ESL — 32-byte random password (update `shared/fs_esl.py` + socket_server consumers), `listen-ip=127.0.0.1`, re-enable `apply-inbound-acl=loopback.auto`. S2: bind `ws_audio_receiver`/`socket_server` to 127.0.0.1 (FreeSWITCH is same-host) or require a shared-secret path segment, **and enforce slug↔owner ownership in `resolve_agent_slug`**. S3: decide D8. S4: SSRF allowlist. S5: `sendmail` argv + CR/LF stripping. Plus: explicit GCP VPC ingress rules (22/80/443 + carrier SIP only), `sites-enabled/lescall` catch-all → `return 444`, `LESCALL_REQUIRE_AUTH=true` boot assertion (fail-closed instead of silently becoming an unauthenticated single-tenant app as the founder), secrets moved to `/etc/lescall/` | off-VM `nc` probe fails on 8021/9000/9001/8080; auth-mode assertion live; **no billing work starts until this passes** |
| P0 | Migration 0010 + boot ALTERs + `backfill_archived.py` (after image is up) + `shared/billing` skeleton + enforcement setting = off | `migrate --status` clean; zero behavior change |
| P1 | Entitlements lib + per-tenant `/usage/minutes` and `/overview` (display only) + identity normalization (§5.5) | staff tenants show correct numbers |
| P2 | Webhook + `billing_events` + test keys + `plans_bootstrap` (test) + nginx limits | test events land; dedupe + replay verified |
| P3 | Checkout + verify + `/billing` page + history + receipts | full test-mode purchase on a staff account |
| P4 | E1–E12 **dark-launch** (log-only for 48h) + reservations + concurrency caps + feature flags | zero false blocks in logs; grep gate green |
| P5 | Admin plane + RUNBOOK §15/§16 | tunnel-only proven; SSRF-cannot-reach test green |
| P6 | **Enforcement on.** Run the §4.1 discovery query first — every pre-existing tenant must have an explicit entitlements row (D6). Then invert the flag's default: enforcement ON unless explicitly `false` + `BILLING_ENFORCE_ACK=<date+reason>`; log resolved state at boot in all four services; sentinel warns if any reports disabled | discovery query returns zero rows; staff unaffected; a test Free account hits every gate |
| P7 | Go-live §11.3 + reopen signups (`LESCALL_ALLOWED_EMAILS=*`) | first real signup lands on Free with 30 min / 1 agent |
| P8 | Dunning drills, webhook-silence + errored-event alarms verified, first Enterprise manual payment | — |

Rollback: every phase is env-flag or additive DB (0010 has `.down.sql`); enforcement
reverts by flipping the DB setting.
