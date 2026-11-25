-- 0024 — cache the filter PLAN, not just the verdicts (2026-08-25)
--
-- 0023 keyed intel_filter_verdicts on a hash that included the planner's
-- requirement TEXT. The planner is an LLM: asked the same phrase twice at
-- temperature 0 it still writes "Calls where the caller agrees or confirms
-- they have relocated to Vapi" one run and "Calls where the caller confirms
-- they are relocating to Vapi" the next. Different text, different hash, so
-- the verdict cache measured 0 hits on a verbatim repeat of the same filter —
-- every re-run, every page refresh, paid for all 100+ judgements again.
--
-- The fix is to make the PLAN itself stable: one row per (owner, question),
-- reused for as long as the rubric and model are unchanged. The verdict key
-- then derives from the question alone, and the same filter run twice is free.
CREATE TABLE IF NOT EXISTS intel_filter_plans (
    id             bigserial PRIMARY KEY,
    owner          text NOT NULL,
    -- sha256(normalised query | rubric version | model) — the same value the
    -- verdict rows use, so a plan and its verdicts retire together.
    query_hash     text NOT NULL,
    query          text NOT NULL,
    model          text,
    rubric_version text,
    criterion      text NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    used_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (owner, query_hash)
);
