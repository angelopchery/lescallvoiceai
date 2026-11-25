-- 0003: Automation engine — rules (versioned) + execution runs with step logs.

CREATE TABLE automation_rules (
    id          BIGSERIAL PRIMARY KEY,
    owner       TEXT NOT NULL,
    name        TEXT NOT NULL,
    description TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    trigger     TEXT NOT NULL,                -- call.analyzed|followup.created|followup.overdue|sentiment.negative|...
    conditions  TEXT NOT NULL DEFAULT '{}',   -- JSON tree {all:[...]}|{any:[...]} of {field, op, value}
    actions     TEXT NOT NULL DEFAULT '[]',   -- JSON ordered [{type, params, delay_s}]
    version     INTEGER NOT NULL DEFAULT 1,
    created_by  TEXT,
    deleted_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_automation_rules_owner ON automation_rules (owner);
CREATE INDEX idx_automation_rules_trigger ON automation_rules (trigger)
    WHERE enabled = 1 AND deleted_at IS NULL;

-- Snapshot on every edit (the agent_revisions pattern; retention enforced in code).
CREATE TABLE automation_rule_versions (
    id         BIGSERIAL PRIMARY KEY,
    rule_id    BIGINT NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
    version    INTEGER NOT NULL,
    owner      TEXT NOT NULL,
    actor      TEXT,
    definition TEXT NOT NULL,                 -- full JSON snapshot of the rule
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (rule_id, version)
);

CREATE TABLE automation_runs (
    id              BIGSERIAL PRIMARY KEY,
    rule_id         BIGINT NOT NULL REFERENCES automation_rules(id) ON DELETE CASCADE,
    rule_version    INTEGER NOT NULL,
    owner           TEXT NOT NULL,
    trigger_event   TEXT NOT NULL DEFAULT '{}',  -- JSON: the event that fired the rule
    call_id         BIGINT REFERENCES calls(id) ON DELETE SET NULL,
    dedupe_key      TEXT UNIQUE,                 -- '<rule_id>:<event>:<call_id>' loop guard
    status          TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|cancelled
    steps           TEXT NOT NULL DEFAULT '[]',  -- JSON per-action log [{type, started, finished, ok, result}]
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ
);
CREATE INDEX idx_automation_runs_rule ON automation_runs (rule_id, created_at);
CREATE INDEX idx_automation_runs_owner ON automation_runs (owner, created_at);
CREATE INDEX idx_automation_runs_ready ON automation_runs (status, next_attempt_at);
