-- 0023 — verified smart filter + call-selection exports (2026-08-25)
--
-- intel_filter_verdicts caches one LLM membership decision per (call, query).
-- The natural-language filter judges every candidate call against an explicit
-- requirement list; without this table, re-running the same filter (or paging
-- it, or exporting from it) would pay for the same judgements again. The cache
-- key carries the RUBRIC VERSION and the MODEL, so changing either retires the
-- old verdicts instead of silently outliving the rubric that produced them,
-- and context_sha retires a verdict whose call was re-analysed underneath it.
CREATE TABLE IF NOT EXISTS intel_filter_verdicts (
    id           bigserial PRIMARY KEY,
    owner        text   NOT NULL,
    call_id      bigint NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    -- sha256(normalised query | rubric version | model): the identity of the
    -- QUESTION, so two users asking the same thing share one answer.
    query_hash   text   NOT NULL,
    query        text   NOT NULL,
    model        text,
    -- sha256 of the exact text the judge saw. A re-analysed or re-transcribed
    -- call changes this and the cached verdict stops applying.
    context_sha  text   NOT NULL,
    is_match     boolean NOT NULL,
    confidence   real,
    evidence     text,
    reason       text,
    checks       text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, call_id, query_hash, context_sha)
);

CREATE INDEX IF NOT EXISTS idx_filter_verdicts_lookup
    ON intel_filter_verdicts (owner, query_hash);

-- call_exports is one generated .xlsx workbook: which calls it covers, where
-- the object lives in GCS, and the unguessable token that serves it back.
-- Same doctrine as call recordings (RUNBOOK §6b): the bucket stays private and
-- the 256-bit token IS the credential, so nothing here depends on being able
-- to mint a service-account key — an org policy blocks that.
CREATE TABLE IF NOT EXISTS call_exports (
    id              bigserial PRIMARY KEY,
    owner           text NOT NULL,
    title           text,
    -- The filter that produced the selection, kept for provenance: a workbook
    -- that cannot say what question it answers is an orphan two weeks later.
    query           text,
    call_ids        text NOT NULL DEFAULT '[]',
    call_count      integer NOT NULL DEFAULT 0,
    contact_count   integer NOT NULL DEFAULT 0,
    filename        text NOT NULL,
    size_bytes      bigint,
    sha256          text,
    -- 'gcs' once the object is in the bucket; 'local' when the upload failed
    -- and the file is being served off the VM. Never silently one or the other.
    storage         text NOT NULL DEFAULT 'local',
    gcs_bucket      text,
    gcs_object      text,
    gcs_uploaded_at timestamptz,
    local_path      text,
    upload_error    text,
    download_token  text NOT NULL,
    created_by      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    deleted_at      timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_call_exports_token
    ON call_exports (download_token);
CREATE INDEX IF NOT EXISTS idx_call_exports_owner
    ON call_exports (owner, created_at DESC);
