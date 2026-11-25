-- 0004: Customer long-term memory + generated reports.

CREATE TABLE customer_memory (
    id           BIGSERIAL PRIMARY KEY,
    contact_id   BIGINT NOT NULL UNIQUE,
    owner        TEXT NOT NULL,
    memory       TEXT NOT NULL DEFAULT '{}',
    -- JSON: {preferences[], promises[], objections[], purchases[],
    --        followup_history[], communication_style, decision_makers[],
    --        risk_level, relationship_notes[]} — each list entry carries
    --        {text, call_id, date} provenance
    calls_count  INTEGER NOT NULL DEFAULT 0,
    last_call_at TIMESTAMPTZ,
    version      INTEGER NOT NULL DEFAULT 1,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_customer_memory_owner ON customer_memory (owner);

CREATE TABLE reports (
    id           BIGSERIAL PRIMARY KEY,
    owner        TEXT NOT NULL,
    title        TEXT NOT NULL,
    query_prompt TEXT,                          -- the natural-language filter that selected the calls
    filters      TEXT NOT NULL DEFAULT '{}',    -- JSON structured filters used
    call_ids     TEXT NOT NULL DEFAULT '[]',    -- JSON [bigint]
    call_count   INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL DEFAULT 'queued',-- queued|running|done|failed
    document     TEXT,                          -- rendered markdown
    sections     TEXT NOT NULL DEFAULT '{}',    -- JSON structured sections
    model        TEXT,
    error        TEXT,
    created_by   TEXT,
    deleted_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);
CREATE INDEX idx_reports_owner ON reports (owner, created_at);
