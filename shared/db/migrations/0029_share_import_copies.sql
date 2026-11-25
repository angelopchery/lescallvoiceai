-- 0029 — a share link can be imported MORE THAN ONCE, and a copy can be
-- brought up to date from it.
--
-- WHAT WAS WRONG
-- --------------
-- 0017 allowed exactly one row per (share_id, importer) and the import handler
-- refused outright while any slug of that copy was still live. That is right
-- for a double-clicked button and wrong for the way sharing is actually used:
-- someone builds an agent FOR another account, keeps improving it, and sends
-- the same link again. The recipient got "You already imported this agent" and
-- a link to the OLD version, with no way forward. The workaround they reached
-- for — archive the old one — only works if they archive BOTH directions; the
-- first real case archived one half and stayed locked out.
--
-- So the recipient now chooses: REPLACE a copy they already hold with the
-- shared version, or import it again as a NEW agent. Several copies of one
-- link in one account is therefore a legitimate state and needs a row each —
-- "replace" has to know which of my agents came from this link, and a single
-- re-pointed row would silently forget every copy but the newest.
--
-- WHY copy_no AND NOT JUST DROPPING THE UNIQUE INDEX
-- --------------------------------------------------
-- The unique index was also the concurrency guard: two simultaneous imports
-- converge on one copy because only one INSERT can win. Dropping it would hand
-- that back. Each import claims the NEXT copy_no instead, so two racing
-- requests both reach for the same number and exactly one gets it. Numbers are
-- never reused — rows for archived copies stay, which also stops a
-- create/archive loop from resetting the per-day import ceiling (the old
-- handler deleted the stale row, and the count with it).
ALTER TABLE agent_share_imports
    ADD COLUMN IF NOT EXISTS copy_no INTEGER NOT NULL DEFAULT 1;

-- When this copy was last replaced with the shared version, and how often.
-- NULL / 0 = still the bytes that were originally imported (plus whatever the
-- recipient edited since). Shown in the import dialog so "your copy is from
-- 9 Sep, the shared agent changed on 19 Sep" is a fact rather than a guess.
ALTER TABLE agent_share_imports
    ADD COLUMN IF NOT EXISTS synced_at TIMESTAMPTZ;
ALTER TABLE agent_share_imports
    ADD COLUMN IF NOT EXISTS sync_count INTEGER NOT NULL DEFAULT 0;

DROP INDEX IF EXISTS idx_share_imports_once;
CREATE UNIQUE INDEX IF NOT EXISTS idx_share_imports_copy
    ON agent_share_imports(share_id, importer, copy_no);
