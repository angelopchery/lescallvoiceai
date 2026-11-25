-- 0028 — a TOKEN budget for the copilot, and the columns the trial floor reads.
--
-- Two problems, one migration, because they are the same problem.
--
-- WHY A TOKEN BUDGET AND NOT JUST MORE TURNS
-- ------------------------------------------
-- `copilot_turns_per_day` counts USER MESSAGES. It has never counted work. One
-- chat turn runs a server-side agentic tool loop (_MAX_TOOL_ROUNDS = 20) and
-- each round is a full LLM request carrying the system prompt, the tool
-- schemas and the fitted history — ~11.5k input tokens by the primary model's
-- own budget, plus up to LLM_MAX_TOKENS out. So "20 turns" is anywhere between
-- ~250k and several million tokens depending entirely on how hard the user
-- pushes, and the cheap interpretation and the expensive one are the SAME
-- number to the old counter. A tenant who wants to burn the shared model quota
-- (COPILOT_LLM_TPM is shared with LIVE PHONE CALLS) does not need to exceed 20
-- messages to do it; they need to write 20 messages that each provoke a long
-- loop. That is what this column prices.
--
-- The turn cap stays. They fail in different directions — a token budget alone
-- permits 10,000 one-word messages, a turn budget alone permits 20 enormous
-- ones — so both are checked and the first to bite wins.
--
-- WHY THE SEEDED NUMBERS LOOK GENEROUS
-- ------------------------------------
-- These are ABUSE CEILINGS, not product allowances. Free is 150,000/day, which
-- measured against the primary model's own input budget is roughly a dozen
-- full tool loops — enough to build one agent end to end and revise it, and
-- not enough to sit in a retry loop all afternoon. Anything much tighter would
-- fire on a legitimate first-day build, and a limit that fires on real use
-- gets raised by whoever is on call rather than respected.
--
-- Enterprise is NULL = unlimited, the same convention every other cap uses.
ALTER TABLE plans       ADD COLUMN IF NOT EXISTS copilot_tokens_per_day BIGINT;
ALTER TABLE entitlements ADD COLUMN IF NOT EXISTS copilot_tokens_per_day BIGINT;

UPDATE plans SET copilot_tokens_per_day =    150000 WHERE code = 'free';
UPDATE plans SET copilot_tokens_per_day =   1500000 WHERE code = 'pilot';
UPDATE plans SET copilot_tokens_per_day =   3000000 WHERE code = 'basic';
UPDATE plans SET copilot_tokens_per_day =   8000000 WHERE code = 'growth';
UPDATE plans SET copilot_tokens_per_day =      NULL WHERE code = 'enterprise';

-- Existing entitlement rows are left NULL: resolve_entitlements() rebuilds a
-- row wholesale from its plan, and get_entitlements() falls back to the plan
-- column when the resolved row has none. Backfilling here would freeze today's
-- number onto every tenant and make the plans row decorative.

-- ---------------------------------------------------------------------------
-- The counter.
--
-- WHY NOT audit_events
-- --------------------
-- The turn cap counts rows in `audit_events` with action='copilot_turn'. That
-- works for turns because a turn IS one row. Tokens are a SUM, and summing a
-- JSON `meta` column over a day of rows means parsing text in the hot path of
-- every chat message — the check runs BEFORE the LLM call, so its latency is
-- latency the user sits through. One upserted row per owner per day is a
-- primary-key lookup instead.
--
-- WHY THE DAY IS A DATE COLUMN AND NOT A TIMESTAMP RANGE
-- ------------------------------------------------------
-- The budget resets at midnight IST, which is the convention _ist_day_bounds()
-- already established for the turn cap. Storing the IST calendar date the
-- writer computed keeps the read a `WHERE day = ?` equality instead of a range
-- scan, and — more importantly — makes the reset boundary the SAME boundary
-- for both caps. Two budgets that reset at different midnights is the kind of
-- detail that turns into a support ticket nobody can reproduce.
--
-- `requests` and `refusals` are here for the operator view: a tenant at the
-- ceiling with 12 requests is building, one at the ceiling with 900 is not.
CREATE TABLE IF NOT EXISTS copilot_token_usage (
    owner TEXT NOT NULL,
    day DATE NOT NULL,                     -- IST calendar date
    prompt_tokens BIGINT NOT NULL DEFAULT 0,
    completion_tokens BIGINT NOT NULL DEFAULT 0,
    total_tokens BIGINT NOT NULL DEFAULT 0,
    requests INTEGER NOT NULL DEFAULT 0,   -- LLM calls, not user messages
    refusals INTEGER NOT NULL DEFAULT 0,   -- times the budget said no
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (owner, day)
);

-- The operator query is "who is burning tokens today", not "what did this
-- tenant do in March".
CREATE INDEX IF NOT EXISTS idx_copilot_tokens_day
    ON copilot_token_usage(day DESC, total_tokens DESC);
