-- 0019: Public integration API — tenant-minted keys, an admin-owned access
-- policy (IP allowlist + rate limits), rate-limit windows and idempotency.
--
-- Why these tables and not a JSON store: the key hash is a credential, the IP
-- allowlist is a security boundary an operator sets per client, and the rate
-- windows are written on EVERY request. All three want real rows, real unique
-- constraints and real indexes.

-- ---------------------------------------------------------------------------
-- The keys themselves. The plaintext secret exists exactly once, in the
-- response that mints it; only its SHA-256 is stored, so a database dump
-- cannot be replayed against the API.
--
-- key_id is the PUBLIC half and travels in the token, which is what makes
-- verification a single indexed lookup instead of a scan-and-compare over
-- every hash in the table.
-- ---------------------------------------------------------------------------
CREATE TABLE api_keys (
    id            BIGSERIAL PRIMARY KEY,
    owner         TEXT NOT NULL,
    key_id        TEXT NOT NULL UNIQUE,
    secret_hash   TEXT NOT NULL,
    last4         TEXT NOT NULL DEFAULT '',   -- so a leaked key can be identified
    name          TEXT NOT NULL DEFAULT '',
    scopes        TEXT NOT NULL DEFAULT '[]', -- JSON [string]
    -- The tenant's OWN narrowing list. The enforced boundary is the operator's
    -- (api_access_policy.ip_allowlist); this one can only narrow it further.
    ip_allowlist  TEXT NOT NULL DEFAULT '[]', -- JSON [cidr]
    -- NULL = inherit the tenant policy. A per-key ceiling can only lower it.
    rate_per_min  INTEGER,
    rate_per_day  INTEGER,
    status        TEXT NOT NULL DEFAULT 'active',   -- active | revoked
    last_used_at  TIMESTAMPTZ,
    last_used_ip  TEXT,
    request_count BIGINT NOT NULL DEFAULT 0,
    expires_at    TIMESTAMPTZ,
    created_by    TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    revoked_at    TIMESTAMPTZ,
    revoked_by    TEXT
);
CREATE INDEX idx_api_keys_owner ON api_keys (owner, status);

-- ---------------------------------------------------------------------------
-- Per-tenant access policy. ADMIN-OWNED: tenants read it (so the Settings page
-- can show what they are actually allowed) but never write it, because it is
-- the surface that says which of our client's machines may talk to us and how
-- hard they may push.
--
-- ip_allowlist is a JSON list of objects, not bare strings:
--   [{"cidr": "203.0.113.9/32", "label": "Pramukh CRM prod",
--     "rate_per_min": 300, "rate_per_day": 100000}]
-- An empty list means "any IP" — the tenant's keys are the only credential.
-- A non-empty list is a hard boundary: an unlisted source IP is refused before
-- the key is even checked. Per-entry limits let one named client machine run
-- hot without raising the ceiling for everything else.
-- ---------------------------------------------------------------------------
CREATE TABLE api_access_policy (
    owner        TEXT PRIMARY KEY,
    enabled      INTEGER NOT NULL DEFAULT 1,
    ip_allowlist TEXT NOT NULL DEFAULT '[]',
    rate_per_min INTEGER NOT NULL DEFAULT 60,
    rate_per_day INTEGER NOT NULL DEFAULT 20000,
    max_keys     INTEGER NOT NULL DEFAULT 5,
    note         TEXT,
    updated_by   TEXT,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------------
-- Fixed-window counters — the rate limiter's state, and the usage figures the
-- Settings page and the admin console read.
--
-- In the database rather than in process memory on purpose: `docker restart
-- lescall-api` must not hand every caller a fresh budget, and the count an
-- operator is shown must be the same count the limiter enforced.
-- ---------------------------------------------------------------------------
CREATE TABLE api_rate_windows (
    key_id       TEXT NOT NULL,
    window_kind  TEXT NOT NULL,               -- minute | day
    window_start TIMESTAMPTZ NOT NULL,
    hits         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (key_id, window_kind, window_start)
);
CREATE INDEX idx_api_rate_windows_start ON api_rate_windows (window_start);

-- ---------------------------------------------------------------------------
-- Idempotency for writes. A CRM that times out and retries a contact push must
-- not create the row twice, so a repeated Idempotency-Key replays the stored
-- response instead of re-running the write.
--
-- request_hash pins the key to its payload: reusing one key for a DIFFERENT
-- body is a client bug that would otherwise return the wrong contact's id.
-- ---------------------------------------------------------------------------
CREATE TABLE api_idempotency (
    id           BIGSERIAL PRIMARY KEY,
    key_id       TEXT NOT NULL,
    idem_key     TEXT NOT NULL,
    endpoint     TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status_code  INTEGER NOT NULL,
    response     TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (key_id, idem_key)
);
CREATE INDEX idx_api_idempotency_created ON api_idempotency (created_at);
