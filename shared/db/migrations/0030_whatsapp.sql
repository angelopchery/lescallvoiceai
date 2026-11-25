-- 0030 — WhatsApp follow-up messages (Integrations → WhatsApp).
--
-- What this is for
-- ----------------
-- Agents have told callers "I'll send you the details on WhatsApp" for months
-- and nothing ever sent them: the promise was recorded (a branch action flag,
-- a `whatsapp_number` slot, a `commitments.kind='whatsapp'` row) and then
-- nothing read it. These tables are the delivery half. A tenant connects
-- their own WhatsApp Business number, writes a PLAYBOOK ("when Ravi gets
-- consent on a call, send the Swarit brochure template"), and every call that
-- ends with the caller's agreement produces one SEND — sent automatically or
-- held for a person to approve, per playbook.
--
-- Consent is its own table, NOT contacts.disposition
-- --------------------------------------------------
-- disposition='rejected' means "asked for DND" AND "declined one ring"
-- (server.py sets it on a declined ring of an unclassified contact), so it
-- cannot answer "may we WhatsApp this person?". wa_consents is an append-only
-- log per phone number; the NEWEST row decides. A consent carries the call it
-- came from and the words that granted it, so "why did we message this
-- person" always has an answer in the caller's own words. A STOP reply is a
-- revoked row, and a later call where they agree again is a newer granted row.
--
-- One send per (playbook, call)
-- -----------------------------
-- The intelligence pipeline re-runs stages (retries, the sweeper's backfill of
-- a newly shipped stage). UNIQUE (playbook_id, call_id) makes "at most one
-- message per call per playbook" a database fact rather than a code path that
-- has to stay correct forever.
--
-- The access token is stored ENCRYPTED (Fernet, key from WA_TOKEN_KEY). The
-- repo is bind-mounted into every container, and a database dump is a thing
-- that gets copied around; a plaintext token in either is a WhatsApp account
-- anyone holding the copy can send from.

CREATE TABLE IF NOT EXISTS wa_accounts (
    owner              text PRIMARY KEY,
    waba_id            text,
    phone_number_id    text UNIQUE,
    display_phone      text,
    verified_name      text,
    token_enc          text,
    -- disconnected | connected | error
    status             text        NOT NULL DEFAULT 'disconnected',
    quality_rating     text,
    messaging_limit    text,
    last_error         text,
    connected_at       timestamptz,
    checked_at         timestamptz,
    -- Defaults for sending. They live here (one row per tenant, created on
    -- first read) rather than per playbook so "no messages after 8pm" is set
    -- once for the whole account.
    default_approval   text        NOT NULL DEFAULT 'manual',   -- manual | auto
    send_window_start  smallint    NOT NULL DEFAULT 9,          -- IST hour, inclusive
    send_window_end    smallint    NOT NULL DEFAULT 20,         -- IST hour, exclusive
    send_delay_min     integer     NOT NULL DEFAULT 2,
    daily_cap          integer     NOT NULL DEFAULT 500,
    approval_ttl_h     integer     NOT NULL DEFAULT 48,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);

-- Message templates as Meta knows them. Synced from the WABA (and created
-- through it); `components` is Meta's own JSON so nothing is lost in a
-- translation we would have to keep up to date.
CREATE TABLE IF NOT EXISTS wa_templates (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    name               text        NOT NULL,
    language           text        NOT NULL,
    category           text,
    status             text,           -- APPROVED | PENDING | REJECTED | PAUSED | DISABLED
    parameter_format   text,           -- NAMED | POSITIONAL
    meta_id            text,
    components         text,           -- JSON array, Meta's shape
    rejected_reason    text,
    synced_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, name, language)
);

-- Files a playbook attaches (the brochure). Kept on disk under data/whatsapp;
-- Meta's media id is cached because an uploaded id is valid for 30 days and
-- re-uploading a 5 MB PDF for every send is waste.
CREATE TABLE IF NOT EXISTS wa_media (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    filename           text        NOT NULL,
    mime               text        NOT NULL,
    size_bytes         bigint      NOT NULL,
    path               text        NOT NULL,
    meta_media_id      text,
    meta_uploaded_at   timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_media_owner ON wa_media (owner);

CREATE TABLE IF NOT EXISTS wa_playbooks (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    name               text        NOT NULL,
    enabled            boolean     NOT NULL DEFAULT false,
    -- JSON array of agent slugs. Never empty when enabled: "every agent" is
    -- not a default anyone should get by forgetting to pick one.
    agent_slugs        text        NOT NULL DEFAULT '[]',
    require_booking    boolean     NOT NULL DEFAULT false,
    recipient          text        NOT NULL DEFAULT 'dialled',   -- dialled | given
    template_name      text,
    -- JSON {"body.greeting": {"value": "{title} {contact_name}", "fallback": "there"}, ...}
    variables          text        NOT NULL DEFAULT '{}',
    media_id           bigint,
    link_url           text,
    approval_mode      text        NOT NULL DEFAULT 'manual',    -- manual | auto
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_playbooks_owner ON wa_playbooks (owner);

CREATE TABLE IF NOT EXISTS wa_consents (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    phone              text        NOT NULL,      -- E.164
    contact_id         bigint,
    state              text        NOT NULL,      -- granted | revoked
    source             text        NOT NULL,      -- call_promise | call_slot | call_commitment | reply_stop | manual
    call_id            bigint,
    quote              text,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_consents_phone ON wa_consents (owner, phone, id DESC);
-- One consent row per call: the stage re-runs, the evidence does not change.
CREATE UNIQUE INDEX IF NOT EXISTS uq_wa_consents_call
    ON wa_consents (owner, call_id) WHERE call_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS wa_sends (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    playbook_id        bigint      NOT NULL,
    call_id            bigint,
    contact_id         bigint,
    consent_id         bigint,
    to_phone           text        NOT NULL,
    template_name      text        NOT NULL,
    language           text,
    -- JSON {"body.greeting": "Mr. Rakesh Shah", ...} — the values as rendered
    -- when the send was created, and as edited by the approver. What is here
    -- is exactly what goes out.
    params             text        NOT NULL DEFAULT '{}',
    -- pending_approval | queued | sending | sent | delivered | read |
    -- failed | skipped | expired | blocked
    status             text        NOT NULL,
    reason             text,
    not_before         timestamptz,
    expires_at         timestamptz,
    approved_by        text,
    approved_at        timestamptz,
    wamid              text UNIQUE,
    error_code         text,
    error              text,
    attempts           integer     NOT NULL DEFAULT 0,
    pricing_category   text,
    sent_at            timestamptz,
    delivered_at       timestamptz,
    read_at            timestamptz,
    failed_at          timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (playbook_id, call_id)
);
CREATE INDEX IF NOT EXISTS idx_wa_sends_owner ON wa_sends (owner, id DESC);
CREATE INDEX IF NOT EXISTS idx_wa_sends_due ON wa_sends (status, not_before);

-- Tracked links: the form/brochure URL a message carries goes through
-- /r/<token> so "clicked" is visible, not only "read".
CREATE TABLE IF NOT EXISTS wa_links (
    token              text PRIMARY KEY,
    owner              text        NOT NULL,
    send_id            bigint,
    url                text        NOT NULL,
    clicks             integer     NOT NULL DEFAULT 0,
    first_clicked_at   timestamptz,
    last_clicked_at    timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_links_send ON wa_links (send_id);

-- What customers write back.
CREATE TABLE IF NOT EXISTS wa_inbound (
    id                 bigserial PRIMARY KEY,
    owner              text        NOT NULL,
    phone              text        NOT NULL,
    contact_id         bigint,
    wamid              text UNIQUE,
    kind               text,
    body               text,
    is_stop            boolean     NOT NULL DEFAULT false,
    received_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_wa_inbound_owner ON wa_inbound (owner, id DESC);

-- Raw webhook log, deduped. Kept so a processing bug can be fixed and the
-- events replayed, the same reason billing_webhook_events exists.
CREATE TABLE IF NOT EXISTS wa_webhook_events (
    id                 bigserial PRIMARY KEY,
    event_key          text UNIQUE NOT NULL,
    payload            text        NOT NULL,
    received_at        timestamptz NOT NULL DEFAULT now(),
    processed_at       timestamptz,
    error              text
);
