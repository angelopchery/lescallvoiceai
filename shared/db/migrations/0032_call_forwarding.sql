-- 0032 — Conditional call forwarding (RUNBOOK §84).
--
-- Why
-- ---
-- A tenant's number has always been answered by an agent. Owners asked for
-- the other arrangement too: ring a phone of THEIRS first, and let the agent
-- take the call only when nobody picks up. The forward is a second leg that
-- FreeSWITCH places from the same number over the number's own trunk; the
-- person hears "Call on your <line> line, from a number ending 4 3 2 1. Press
-- 1 to accept." and is then connected straight to the caller.
--
-- forward_contacts   phones an owner may forward to. Indian numbers only, and
--                    unusable until verified: we ring the phone once and a
--                    voice reads a 4-digit code the owner types back. Without
--                    that, an account could make us ring a stranger.
-- phone_numbers.*    the per-number setting.
-- forwarded_calls    one row per forwarded call, built from FreeSWITCH's CDR by
--                    the workers (`python -m shared.call_forwarding --sync`).
--                    These calls never reach the AI, so they have no `calls`
--                    row and no usage_ledger row: forwarded minutes are counted
--                    HERE, separately from AI minutes, by the owner's decision.

CREATE TABLE IF NOT EXISTS forward_contacts (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    label              text        NOT NULL DEFAULT '',
    number             text        NOT NULL,             -- +91XXXXXXXXXX (never a LesCall number)
    sig_digits         text        NOT NULL,             -- last 10 digits
    verified_at        timestamptz,                      -- NULL = not usable yet
    verify_code_hash   text,                             -- sha256(contact id + code)
    verify_expires_at  timestamptz,
    verify_tries       integer     NOT NULL DEFAULT 0,   -- wrong codes for the current code
    verify_calls_day   date,                             -- day the counter below belongs to
    verify_calls_count integer     NOT NULL DEFAULT 0,   -- code calls placed that day
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, sig_digits)
);
CREATE INDEX IF NOT EXISTS idx_forward_contacts_owner ON forward_contacts(owner);

ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS forward_mode       text    NOT NULL DEFAULT 'agent';  -- agent | forward
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS forward_contact_id bigint  REFERENCES forward_contacts(id) ON DELETE SET NULL;
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS forward_ring_s     integer NOT NULL DEFAULT 25;       -- 10..45
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS forward_confirm    boolean NOT NULL DEFAULT true;     -- press 1 to accept
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS forward_fallback   text    NOT NULL DEFAULT 'agent';  -- agent | hangup
-- Spoken to the person in the press-1 prompt ("Call on your <name> line").
-- Deliberately NOT phone_numbers.label: that is a free-text admin note
-- ("restored 2026-09-23 for handover to …") and must never be read aloud.
ALTER TABLE phone_numbers ADD COLUMN IF NOT EXISTS forward_line_name  text    NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS forwarded_calls (
    id              bigserial PRIMARY KEY,
    owner           text,
    fs_uuid         text UNIQUE,      -- FreeSWITCH uuid of the caller's (inbound) leg
    number_id       bigint,
    did             text,             -- the number that was called (digits)
    caller          text,             -- who called it (digits, or 'anonymous')
    contact_id      bigint,
    contact_number  text,             -- the phone it was forwarded to, at sync time
    outcome         text,             -- connected | no_answer | busy | rejected | not_accepted | caller_hung_up | failed
    cause           text,             -- FreeSWITCH originate disposition / hangup cause
    fallback        text,             -- agent | hangup | none
    ring_s          integer,          -- caller waited before the person took the call
    talk_s          integer,          -- forwarded talk time (0 unless connected)
    started_at      timestamptz,
    answered_at     timestamptz,
    ended_at        timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_forwarded_calls_owner ON forwarded_calls(owner, started_at DESC);
