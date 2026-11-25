-- LesCall multi-tenant schema (docs/MULTITENANT_BYON_PLAN.md §1 + Part B Phase 0).
-- {PK} is substituted per dialect by shared/db/database.py:
--   SQLite:   INTEGER PRIMARY KEY AUTOINCREMENT
--   Postgres: BIGSERIAL PRIMARY KEY
-- Convention: `owner` holds the tenant's email; every tenant-facing query filters by it.
-- Phone columns keep the number as entered; `sig_digits` (last 10 digits, from
-- shared.phone.significant_digits) is the indexed canonical routing/lookup key.

-- Dashboard accounts (Google OAuth). owner columns elsewhere store the email.
CREATE TABLE IF NOT EXISTS users (
    id {PK},
    email TEXT NOT NULL UNIQUE,
    name TEXT,
    google_sub TEXT UNIQUE,
    picture TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login TIMESTAMP
);

-- An INDEX over the YAML agent registry, not a replacement for it.
-- slug = calling-agent/agents/<slug>/. Personas stay in agent.yaml; this row
-- exists so agents can be owner-scoped and referenced by routing.
CREATE TABLE IF NOT EXISTS agents (
    id {PK},
    slug TEXT NOT NULL UNIQUE,
    owner TEXT,
    display_name TEXT,
    is_active INTEGER DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- A SIP trunk/gateway (Plivo, Twilio, ...). Drives the generated FreeSWITCH
-- gateway XML and the Kamailio carrier ACL. provider_ips = comma-separated CIDRs.
CREATE TABLE IF NOT EXISTS trunks (
    id {PK},
    name TEXT NOT NULL UNIQUE,
    provider TEXT DEFAULT 'plivo',
    owner TEXT,
    sip_domain TEXT NOT NULL,
    proxy TEXT,
    username TEXT,
    password TEXT,
    register INTEGER DEFAULT 0,
    caller_id TEXT,
    provider_ips TEXT,
    transport TEXT DEFAULT 'udp',
    config_status TEXT DEFAULT 'pending',
    last_applied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- A DID bound to a trunk AND one agent PER DIRECTION. THE routing table.
-- inbound_agent_slug answers calls TO this DID; outbound_agent_slug speaks on
-- calls placed FROM it. They are independent bindings — an inbound receptionist
-- and an outbound sales agent are different agents with different prompts.
CREATE TABLE IF NOT EXISTS phone_numbers (
    id {PK},
    number TEXT NOT NULL UNIQUE,
    sig_digits TEXT,
    trunk_id INTEGER REFERENCES trunks(id) ON DELETE SET NULL,
    inbound_agent_slug TEXT,
    outbound_agent_slug TEXT,
    owner TEXT,
    direction TEXT DEFAULT 'both',
    label TEXT,
    -- verified=1 means the tenant proved control of this number (OTP call).
    -- Only a verified number may be used as an outbound caller ID.
    verified INTEGER DEFAULT 0,
    config_status TEXT DEFAULT 'pending',
    last_applied_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_phone_numbers_sig ON phone_numbers(sig_digits);

-- Outbound dialing bound to one agent slug + one gateway(trunk) + caller id.
CREATE TABLE IF NOT EXISTS campaigns (
    id {PK},
    owner TEXT,
    name TEXT NOT NULL,
    agent_slug TEXT,
    caller_id TEXT,
    gateway TEXT,
    status TEXT DEFAULT 'draft',
    max_concurrency INTEGER DEFAULT 1,
    retry_limit INTEGER DEFAULT 2,
    schedule TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Unified inbound+outbound call record; owner tags the tenant for dashboard scoping.
-- call_key = "<digits>_<YYYYMMDD>_<HHMMSS>" (recordings folder / call_analysis.json key).
-- analysis = the full analysis JSON blob (superset of summary).
CREATE TABLE IF NOT EXISTS calls (
    id {PK},
    conn_id TEXT UNIQUE,
    call_key TEXT UNIQUE,
    direction TEXT NOT NULL DEFAULT 'inbound',
    agent_slug TEXT,
    campaign_id INTEGER,
    phone TEXT,
    sig_digits TEXT,
    language TEXT,
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration_s REAL,
    recording_path TEXT,
    transcript TEXT,
    summary TEXT,
    analysis TEXT,
    owner TEXT,
    -- Recording delivery: download_token is a 256-bit random URL token — the
    -- ONLY unauthenticated way to reach a recording, so it must stay
    -- unguessable and never be derived from call_key/phone/date.
    download_token TEXT,
    recording_url TEXT,
    gcs_object TEXT,
    gcs_uploaded_at TIMESTAMP,
    follow_up TEXT,
    collected_data TEXT,
    -- Integer PK of the contacts row this call is bound to (resolved by
    -- owner+sig_digits at call end; unknown callers get a contact created).
    contact_id INTEGER,
    -- F8 per-call agent fidelity: which agent revision/sha served the call,
    -- the F6 preflight health marker ('ok' | 'load_failed' |
    -- 'preflight_failed:<stage>'), and LLM usage counters for the call.
    agent_revision INTEGER,
    agent_sha TEXT,
    agent_health TEXT,
    llm_models_used TEXT,
    llm_fallback_turns INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_calls_owner ON calls(owner);
CREATE INDEX IF NOT EXISTS idx_calls_sig ON calls(sig_digits);

-- ---------------------------------------------------------------------------
-- BILLING-GRADE minute metering (deploy/PLAN-minutes-docker-logging.md §1).
-- One immutable row per call, keyed by conn_id (== FreeSWITCH channel UUID).
-- billsec_final is the ONLY number billing may read; it is set by the
-- reconciler (CDR wins over the pipeline's own clock) and never changes after
-- status='reconciled' except via source='manual' corrections that MUST carry
-- a note. Rollups (usage_monthly) are recomputed from this table, never
-- incremented in place.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS usage_ledger (
    id {PK},
    conn_id     TEXT NOT NULL UNIQUE,
    fs_uuid     TEXT,
    owner       TEXT NOT NULL,
    agent_slug  TEXT,
    direction   TEXT,
    contact_id  INTEGER,
    phone       TEXT,
    answered_at TIMESTAMP,
    ended_at    TIMESTAMP,
    last_beat_at TIMESTAMP,
    billsec_pipeline INTEGER,
    billsec_cdr      INTEGER,
    billsec_final    INTEGER,
    source      TEXT,
    billable    INTEGER NOT NULL DEFAULT 1,
    status      TEXT NOT NULL DEFAULT 'open',
    note        TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_ledger_owner_time ON usage_ledger(owner, answered_at);
CREATE INDEX IF NOT EXISTS idx_ledger_status ON usage_ledger(status);

-- Derived rollup; REPLACEd wholesale per (owner, month) by the reconciler.
CREATE TABLE IF NOT EXISTS usage_monthly (
    id {PK},
    owner   TEXT NOT NULL,
    month   TEXT NOT NULL,          -- 'YYYY-MM' (UTC)
    seconds INTEGER NOT NULL DEFAULT 0,
    calls   INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner, month)
);

-- Semantic user-action audit trail ("track everything"): one row per
-- meaningful mutation, written by the dashboard endpoints.
CREATE TABLE IF NOT EXISTS audit_events (
    id {PK},
    owner  TEXT,
    action TEXT NOT NULL,
    target TEXT,
    meta   TEXT,                    -- JSON
    ip     TEXT,
    at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_audit_owner_at ON audit_events(owner, at);

-- Log-shipper bookkeeping: which local log files (at which mtime/size) have
-- already been uploaded to the logs bucket. Re-ship on change, overwrite in
-- place — the bucket always holds the latest full copy of each file.
CREATE TABLE IF NOT EXISTS shipped_logs (
    id {PK},
    path TEXT UNIQUE,
    mtime REAL,
    size INTEGER,
    shipped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Per-tenant contact groups for the phone-book panel. Contacts with
-- group_id NULL are the implicit "No group" bucket. Same group name may
-- exist under different owners.
CREATE TABLE IF NOT EXISTS contact_groups (
    id {PK},
    owner TEXT NOT NULL,
    name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner, name)
);

-- Part B Phase 3 target for dashboard/outbound_contacts.json.
-- extra = JSON for fields with no column (call_history, upload metadata, ...).
-- details = JSON dict of extra CSV columns captured at upload / manual entry.
-- Phone is unique PER OWNER only — the same number under two owners is two
-- independent contacts (multi-tenant phone books).
CREATE TABLE IF NOT EXISTS contacts (
    id {PK},
    legacy_id TEXT,
    owner TEXT,
    phone TEXT NOT NULL,
    sig_digits TEXT,
    name TEXT,
    notes TEXT,
    call_status TEXT,
    disposition TEXT,
    call_count INTEGER DEFAULT 0,
    last_called_at TIMESTAMP,
    call_notes TEXT,
    extra TEXT,
    added_at TIMESTAMP,
    gender TEXT,
    details TEXT,
    group_id INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner, phone)
);
CREATE INDEX IF NOT EXISTS idx_contacts_sig ON contacts(sig_digits);

-- Part B Phase 3/4 target for follow_ups.json / manual_followups.json.
-- ref = the JSON store's key (e.g. "call:<call_key>") so imports stay idempotent.
CREATE TABLE IF NOT EXISTS follow_ups (
    id {PK},
    owner TEXT,
    ref TEXT UNIQUE,
    contact_phone TEXT,
    due_at TIMESTAMP,
    note TEXT,
    status TEXT DEFAULT 'pending',
    priority TEXT,
    source TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Part B Phase 2: reliable to-send tray for external hand-offs (client ERP leads).
-- dedupe_key makes delivery idempotent: same lead enqueued twice = one row.
-- status: pending | sent | duplicate | failed
CREATE TABLE IF NOT EXISTS outbox (
    id {PK},
    owner TEXT,
    kind TEXT NOT NULL DEFAULT 'erp_lead',
    dedupe_key TEXT UNIQUE,
    payload TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    attempts INTEGER DEFAULT 0,
    next_attempt_at TIMESTAMP,
    last_error TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_attempt_at);

-- Generic per-tenant key/value store (JSON values). owner='' = platform-global.
-- Replaces minutes_quota.json, sarvam_credits.json, api_key_state.json.
CREATE TABLE IF NOT EXISTS settings (
    id {PK},
    owner TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (owner, key)
);

-- Scheduled follow-up auto-dials (replaces follow_up_automation.json).
-- legacy_id = the JSON store's uuid hex id, still the UI handle.
-- status: scheduled | calling | called | failed | cancelled
CREATE TABLE IF NOT EXISTS followup_automations (
    id {PK},
    legacy_id TEXT UNIQUE,
    owner TEXT,
    source_id TEXT,
    contact_name TEXT,
    contact_phone TEXT,
    past_call_context TEXT,
    call_aim TEXT,
    script_notes TEXT,
    scheduled_time TIMESTAMP,
    linked_contact_id TEXT,
    status TEXT NOT NULL DEFAULT 'scheduled',
    call_outcome TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    called_at TIMESTAMP
);

-- Dial→call correlation log (replaces call_link.json): one row per outbound
-- originate; the bridge matches an incoming pipeline call to the most recent
-- row for that phone within its matching window.
CREATE TABLE IF NOT EXISTS call_links (
    id {PK},
    owner TEXT,
    phone TEXT NOT NULL,
    sig_digits TEXT,
    contact_id TEXT,
    name TEXT,
    agent_slug TEXT,
    dialed_at TIMESTAMP NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_call_links_dialed ON call_links(dialed_at);

-- Appointment bookings (replaces per-agent bookings.json; keyed by agent slug).
CREATE TABLE IF NOT EXISTS bookings (
    id {PK},
    agent_slug TEXT NOT NULL,
    slot_day TEXT,
    slot_time TEXT,
    appointment_time TEXT,
    caller_name TEXT,
    caller_phone TEXT,
    conn_id TEXT,
    booked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_bookings_agent ON bookings(agent_slug);

-- Agent revision snapshots (AgentSpec v2 edit engine, plan D11/§5.1).
-- YAML stays the runtime source of truth; every write through
-- agent_definition/store.py snapshots the PRE-write YAML text here
-- (retention 50/slug) so any edit is diffable and reversible.
CREATE TABLE IF NOT EXISTS agent_revisions (
    id {PK},
    slug TEXT NOT NULL,
    owner TEXT,
    actor TEXT,
    reason TEXT,
    sha256 TEXT,
    yaml_text TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_agent_revisions_slug ON agent_revisions(slug, id);
