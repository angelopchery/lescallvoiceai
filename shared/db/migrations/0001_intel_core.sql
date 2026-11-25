-- 0001: Intelligence layer core — job queue, per-call intelligence record,
-- commitments, action items, follow-ups, notifications.
--
-- Conventions carried over from the platform (see shared/db/schema.sql):
--   * owner TEXT = tenant email on every tenant-facing row
--   * JSON payloads stored as TEXT (readers json.loads; _TextLoader keeps
--     timestamps as strings)
-- New discipline introduced here (deliberately better than legacy):
--   * real foreign keys to calls(id) with ON DELETE CASCADE
--   * BIGINT contact references (contacts.id is bigint)
--   * TIMESTAMPTZ timestamps

-- ---------------------------------------------------------------------------
-- Job queue: the event backbone of the post-call pipeline.
-- Claim protocol: UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED).
-- ---------------------------------------------------------------------------
CREATE TABLE intel_jobs (
    id              BIGSERIAL PRIMARY KEY,
    kind            TEXT NOT NULL,               -- call.process | report.generate | automation.run | followup.sweep
    owner           TEXT NOT NULL DEFAULT '',
    call_id         BIGINT REFERENCES calls(id) ON DELETE CASCADE,
    dedupe_key      TEXT UNIQUE,                 -- e.g. 'call.process:<conn_id>'
    payload         TEXT NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|cancelled
    attempts        INTEGER NOT NULL DEFAULT 0,
    max_attempts    INTEGER NOT NULL DEFAULT 5,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_by      TEXT,
    claimed_at      TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX idx_intel_jobs_ready ON intel_jobs (status, next_attempt_at);
CREATE INDEX idx_intel_jobs_call ON intel_jobs (call_id);

-- ---------------------------------------------------------------------------
-- One intelligence record per call. Scalar columns for everything the
-- dashboard filters/sorts on; JSON TEXT for the deep detail lists.
-- ---------------------------------------------------------------------------
CREATE TABLE call_intelligence (
    id                     BIGSERIAL PRIMARY KEY,
    call_id                BIGINT NOT NULL UNIQUE REFERENCES calls(id) ON DELETE CASCADE,
    owner                  TEXT NOT NULL,
    contact_id             BIGINT,
    agent_slug             TEXT,
    schema_version         INTEGER NOT NULL DEFAULT 1,
    model                  TEXT,
    stages                 TEXT NOT NULL DEFAULT '{}',  -- per-stage checkpoint map
    processed_at           TIMESTAMPTZ,
    -- executive summary
    summary                TEXT,
    outcome_summary        TEXT,
    customer_intent        TEXT,
    conversation_quality   REAL,        -- 0..1
    conversion_probability REAL,        -- 0..1
    satisfaction           REAL,        -- 0..1
    urgency                TEXT,        -- low|medium|high|critical
    confidence             REAL,        -- 0..1 extraction confidence
    low_confidence         INTEGER NOT NULL DEFAULT 0,
    -- scores 0..100
    lead_score             INTEGER,
    opportunity_score      INTEGER,
    risk_score             INTEGER,
    compliance_score       INTEGER,
    sentiment              TEXT,        -- positive|neutral|negative
    sentiment_journey      TEXT,        -- JSON [{phase, sentiment, note}]
    -- intelligence detail: JSON lists of {text, quote, turn_ref?}
    topics                 TEXT,
    pain_points            TEXT,
    objections             TEXT,
    buying_signals         TEXT,
    competitor_mentions    TEXT,
    product_interest       TEXT,
    budget_discussion      TEXT,
    timeline_discussion    TEXT,
    decision_makers        TEXT,
    compliance_issues      TEXT,
    escalations            TEXT,
    risk_indicators        TEXT,
    agent_performance      TEXT,        -- JSON {score, strengths[], improvements[]}
    key_moments            TEXT,        -- JSON timeline [{at_ms?, label, detail}]
    recommendations        TEXT,        -- JSON {next_actions[], emails[], documents[], crm_updates[]}
    entities               TEXT,        -- JSON {names[], orgs[], amounts[], dates[], places[]}
    tags                   TEXT,        -- JSON [string]
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_ci_owner_processed ON call_intelligence (owner, processed_at);
CREATE INDEX idx_ci_owner_lead ON call_intelligence (owner, lead_score);
CREATE INDEX idx_ci_owner_sentiment ON call_intelligence (owner, sentiment);
CREATE INDEX idx_ci_contact ON call_intelligence (contact_id);

-- ---------------------------------------------------------------------------
-- Every promise made on any call, normalized and queryable.
-- ---------------------------------------------------------------------------
CREATE TABLE commitments (
    id              BIGSERIAL PRIMARY KEY,
    call_id         BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    owner           TEXT NOT NULL,
    contact_id      BIGINT,
    made_by         TEXT NOT NULL,               -- agent|customer
    kind            TEXT NOT NULL,               -- callback|send_document|whatsapp|meeting|quote|payment|visit|other
    description     TEXT NOT NULL,
    quote           TEXT,                        -- supporting transcript quote
    deadline_text   TEXT,                        -- raw phrase ("next week", "kal")
    deadline_at     TIMESTAMPTZ,                 -- resolved concrete time (nullable)
    is_vague        INTEGER NOT NULL DEFAULT 0,
    clarified       INTEGER NOT NULL DEFAULT 0,
    clarified_value TEXT,
    source          TEXT NOT NULL DEFAULT 'post_call_llm',  -- in_call|post_call_llm
    confidence      REAL,
    status          TEXT NOT NULL DEFAULT 'open',           -- open|fulfilled|cancelled
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_commitments_owner ON commitments (owner, created_at);
CREATE INDEX idx_commitments_call ON commitments (call_id);
CREATE INDEX idx_commitments_open ON commitments (owner, status) WHERE status = 'open';

-- ---------------------------------------------------------------------------
-- Action items extracted per call (editable, assignable, trackable).
-- ---------------------------------------------------------------------------
CREATE TABLE action_items (
    id          BIGSERIAL PRIMARY KEY,
    call_id     BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    owner       TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',    -- open|in_progress|done|dismissed
    priority    TEXT NOT NULL DEFAULT 'medium',  -- low|medium|high|urgent
    assigned_to TEXT,
    due_at      TIMESTAMPTZ,
    source      TEXT NOT NULL DEFAULT 'ai',      -- ai|manual
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_action_items_owner ON action_items (owner, status);
CREATE INDEX idx_action_items_call ON action_items (call_id);

-- ---------------------------------------------------------------------------
-- Follow-ups v2: the decision engine's output, including explicit NO decisions
-- (the explainability record). Supersedes legacy follow_ups/followup_automations
-- for new writes; legacy tables are left untouched for rollback.
-- ---------------------------------------------------------------------------
CREATE TABLE followups (
    id            BIGSERIAL PRIMARY KEY,
    owner         TEXT NOT NULL,
    call_id       BIGINT REFERENCES calls(id) ON DELETE CASCADE,
    contact_id    BIGINT,
    commitment_id BIGINT REFERENCES commitments(id) ON DELETE SET NULL,
    decision      TEXT NOT NULL,                 -- yes|no|unsure
    confidence    REAL,
    explanation   TEXT,                          -- why the engine decided this
    reason        TEXT,                          -- why a follow-up is needed
    summary       TEXT,
    followup_type TEXT,                          -- callback|send_info|meeting|visit|payment|other
    promised_by   TEXT,                          -- customer_requested|agent_promised|internal|external_dependency
    scheduled_at  TIMESTAMPTZ,
    timezone      TEXT NOT NULL DEFAULT 'Asia/Kolkata',
    window_start  TIMESTAMPTZ,
    window_end    TIMESTAMPTZ,
    auto_scheduled INTEGER NOT NULL DEFAULT 0,   -- 1 = AI picked the concrete time
    priority      TEXT NOT NULL DEFAULT 'medium',
    status        TEXT NOT NULL DEFAULT 'pending', -- pending|scheduled|completed|cancelled|waiting_customer|escalated|missed|archived
    assigned_to   TEXT,
    reminder      TEXT,                          -- JSON {offsets_min:[...], channels:[...]}
    source        TEXT NOT NULL DEFAULT 'pipeline', -- pipeline|in_call|manual
    notes         TEXT NOT NULL DEFAULT '[]',    -- JSON [{at, by, text}]
    audit         TEXT NOT NULL DEFAULT '[]',    -- JSON [{at, by, action, detail}]
    deleted_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at  TIMESTAMPTZ
);
CREATE INDEX idx_followups_owner_status ON followups (owner, status);
CREATE INDEX idx_followups_owner_sched ON followups (owner, scheduled_at);
CREATE INDEX idx_followups_call ON followups (call_id);
CREATE INDEX idx_followups_due ON followups (status, scheduled_at)
    WHERE status IN ('pending', 'scheduled');

-- ---------------------------------------------------------------------------
-- In-app notifications (bell + SSE stream).
-- ---------------------------------------------------------------------------
CREATE TABLE notifications (
    id         BIGSERIAL PRIMARY KEY,
    owner      TEXT NOT NULL,
    kind       TEXT NOT NULL,                    -- call.analyzed|followup.due|automation.fired|report.ready|...
    severity   TEXT NOT NULL DEFAULT 'info',     -- info|warn|critical
    title      TEXT NOT NULL,
    body       TEXT,
    link       TEXT,                             -- SPA route, e.g. /intelligence/<call_key>
    ref        TEXT,                             -- opaque reference (call_key, followup id, ...)
    read_at    TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_notifications_owner ON notifications (owner, created_at);
CREATE INDEX idx_notifications_unread ON notifications (owner) WHERE read_at IS NULL;
