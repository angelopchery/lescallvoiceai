-- 0006: Customer Intelligence Hub — pre-call context digest, unified contact
-- timeline, and cross-owner contact grants.
--
-- context_digest is the write-time-precomputed prompt digest ("hot cache"
-- layer of the pre-call context architecture): the digest pipeline stage
-- renders it after every memory merge so the live-call bridge needs only one
-- indexed read, no assembly, no LLM.

ALTER TABLE customer_memory ADD COLUMN context_digest TEXT;
ALTER TABLE customer_memory ADD COLUMN digest_updated_at TIMESTAMPTZ;

-- Append-only per-contact timeline. Written by the pipeline timeline stage
-- (sweeper backfills history), the dial paths, and contact/follow-up CRUD.
-- Nothing is ever manually linked; payload is a small JSON render summary,
-- never a transcript.
CREATE TABLE contact_events (
    id          BIGSERIAL PRIMARY KEY,
    contact_id  BIGINT NOT NULL,
    owner       TEXT NOT NULL,
    kind        TEXT NOT NULL,            -- call.inbound|call.outbound|call.missed|
                                          -- dial.attempted|call.analyzed|
                                          -- followup.created|followup.completed|
                                          -- commitment.made|memory.updated|
                                          -- note.added|contact.updated|automation.fired
    ref_table   TEXT,
    ref_id      BIGINT,                   -- soft ref to calls/followups/commitments/...
    occurred_at TIMESTAMPTZ NOT NULL,
    payload     TEXT,                     -- small JSON summary for rendering
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_contact_events_contact ON contact_events (contact_id, occurred_at DESC);
CREATE INDEX idx_contact_events_owner ON contact_events (owner, occurred_at DESC);
-- Idempotent emitters: one event per (kind, referenced row). Ref-less events
-- (notes, ad-hoc updates) are exempt by the partial predicate.
CREATE UNIQUE INDEX uq_contact_events_ref ON contact_events (kind, ref_table, ref_id)
    WHERE ref_table IS NOT NULL AND ref_id IS NOT NULL;

-- Cross-owner delegation under the flattened-roles doctrine: no admin —
-- only the owning tenant grants/revokes, enforced in the /api/intel/contacts
-- handlers. Grantees get the same read (or edit) path, never ownership.
CREATE TABLE contact_grants (
    id            BIGSERIAL PRIMARY KEY,
    contact_id    BIGINT NOT NULL,
    owner         TEXT NOT NULL,            -- granting tenant (contact's owner)
    grantee_email TEXT NOT NULL,
    access        TEXT NOT NULL DEFAULT 'read',  -- read|edit
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (contact_id, grantee_email)
);
CREATE INDEX idx_contact_grants_grantee ON contact_grants (grantee_email);

-- Pre-call hot path: the bridge's second query counts open follow-ups and
-- commitments per contact via scalar subqueries — both need contact indexes.
CREATE INDEX idx_followups_contact ON followups (contact_id);
CREATE INDEX idx_commitments_contact ON commitments (contact_id);
