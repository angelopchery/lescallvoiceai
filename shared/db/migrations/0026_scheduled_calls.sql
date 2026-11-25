-- 0026 — scheduled calls and scheduled campaigns.
--
-- "Call this person at 4pm" and "run this campaign tomorrow at 10" were the
-- two things the outbound surface could not express. Everything it could do
-- happened NOW: the dial button originates immediately, and a campaign runs
-- from the moment it is started. The only way to reach someone at a time they
-- asked for was to sit on the page until then.
--
-- Why a table and not a timer
-- ---------------------------
-- The campaign runner is an asyncio task inside lescall-api and its progress
-- lives in that process's memory, which is correct for a run you are watching
-- and useless for an intent that has to survive until tomorrow: lescall-api is
-- restarted on every deploy. An in-process timer would silently drop every
-- pending schedule each time we ship. The INTENT is durable state, so it is a
-- row; only the loop that notices due rows lives in the process, and it
-- rebuilds itself from this table on boot.
--
-- Why `status` is claimed, not read
-- ---------------------------------
-- The executor claims a row with a conditional UPDATE (pending -> running,
-- rowcount must be 1) rather than SELECT-then-act. Today lescall-api runs one
-- uvicorn worker, so nothing races; the day it runs two, "read the due rows
-- and dial them" would ring every scheduled contact twice. Claiming costs one
-- statement now and cannot be retrofitted after the incident.
--
-- Why a fired row is never re-run
-- -------------------------------
-- Terminal statuses (done/failed/missed/cancelled) are ends. A schedule that
-- failed is NOT retried automatically: the failure modes here are "no minutes",
-- "no trunk", "outside calling hours" — none of which a blind retry fixes, and
-- all of which would keep re-dialling a real person while the operator sleeps.
-- The row records why, and a human decides.
--
-- `scheduled_at` is timestamptz: the UI collects a wall-clock time in IST and
-- sends an absolute instant. Storing local time in a naive column is how a
-- schedule set for "4pm" fires at 10:30.

CREATE TABLE IF NOT EXISTS scheduled_calls (
    id             bigserial PRIMARY KEY,
    owner          text        NOT NULL,
    kind           text        NOT NULL,          -- 'call' | 'campaign'
    label          text,
    -- kind='call': who to ring. contact_handle is the string id the whole
    -- outbound surface keys on (legacy_id, or "db<pk>"); phone is a snapshot
    -- kept for the audit trail and for naming the row after the contact is
    -- deleted, never for dialling — the number is re-read at fire time.
    contact_handle text,
    phone          text,
    -- kind='campaign': the audience, as the same JSON body /campaign/start
    -- takes, so a scheduled campaign and a started one are the same request.
    audience       text,
    scheduled_at   timestamptz NOT NULL,
    status         text        NOT NULL DEFAULT 'pending',
    result         text,
    last_error     text,
    fired_at       timestamptz,
    created_by     text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

-- The executor's only query: due pending rows, oldest first.
CREATE INDEX IF NOT EXISTS idx_scheduled_calls_due
    ON scheduled_calls (status, scheduled_at);

-- The page's only query: this tenant's schedule, soonest first.
CREATE INDEX IF NOT EXISTS idx_scheduled_calls_owner
    ON scheduled_calls (owner, scheduled_at);
