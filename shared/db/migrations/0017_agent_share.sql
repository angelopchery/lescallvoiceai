-- 0017_agent_share.sql — share an agent by link; the recipient IMPORTS a copy.
--
-- The product rule this schema exists to enforce: an imported agent has NO
-- relationship to the one it came from. It is a new row, a new slug, a new
-- agent_id, owned by the importer; either side can edit or delete theirs
-- without the other noticing. Nothing at call time ever reads these tables —
-- they are the sharing control plane, not a runtime join.
--
-- "The same agent cannot be shared by two users" is guaranteed by three facts
-- taken together, not by a column:
--   1. agents.slug is globally UNIQUE and each row carries exactly ONE owner,
--      so an agent record belongs to one account by construction.
--   2. POST /api/share verifies every slug in the family is owned by the
--      caller before minting a token, so nobody can share what they don't own.
--   3. idx_agent_shares_live below allows at most ONE live link per
--      (owner, family) — pressing Share twice returns the same token instead
--      of minting a second, separately-revocable link for the same agent.
-- An import mints a brand-new family, so two accounts never point at one row.

CREATE TABLE agent_shares (
    id           BIGSERIAL PRIMARY KEY,
    -- 256-bit URL-safe random. This token IS the credential — anyone holding
    -- it can read and copy the agent — so it must stay unguessable and must
    -- never be derived from the slug, the owner or the creation time.
    token        TEXT NOT NULL UNIQUE,
    owner        TEXT NOT NULL,          -- the sharer's email (tenant key)
    -- The agent FAMILY (base slug without -inbound/-outbound). A link carries
    -- whichever direction variants exist AT IMPORT TIME, because the plan cap
    -- counts families and the UI card is a family.
    family       TEXT NOT NULL,
    -- Display name captured when the link was made, so the sharer's list of
    -- links stays readable even after they rename the agent.
    label        TEXT,
    import_count INTEGER NOT NULL DEFAULT 0,
    -- NULL = never expires. Default is 30 days from creation (set in code, not
    -- as a column default, so the policy is visible where it is applied).
    expires_at   TIMESTAMPTZ,
    revoked_at   TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one LIVE link per agent per owner. Revoked rows are kept (the
-- import history references them) and drop out of the constraint.
CREATE UNIQUE INDEX idx_agent_shares_live
    ON agent_shares(owner, family) WHERE revoked_at IS NULL;
CREATE INDEX idx_agent_shares_owner ON agent_shares(owner);

-- Bookkeeping only: who imported which link, and what it became. Nothing
-- reads this to resolve an agent — it exists so the sharer can see how many
-- people took a copy, so a repeated open of the same link does not silently
-- mint a second copy, and so abuse is traceable.
CREATE TABLE agent_share_imports (
    id            BIGSERIAL PRIMARY KEY,
    share_id      BIGINT NOT NULL REFERENCES agent_shares(id) ON DELETE CASCADE,
    importer      TEXT NOT NULL,         -- the receiving tenant
    source_owner  TEXT NOT NULL,
    source_family TEXT NOT NULL,
    new_family    TEXT NOT NULL,         -- what it became in the importer's account
    new_slugs     TEXT NOT NULL,         -- JSON array of the created slugs
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One copy per person per link. A double-clicked Import button, a reloaded
-- tab, or a link opened twice must not quietly consume two agent slots. The
-- import handler deletes this row first when the earlier copy has since been
-- archived or deleted — re-importing after cleaning up is legitimate.
CREATE UNIQUE INDEX idx_share_imports_once
    ON agent_share_imports(share_id, importer);
CREATE INDEX idx_share_imports_importer ON agent_share_imports(importer);
