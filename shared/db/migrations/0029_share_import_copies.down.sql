-- Rollback 0029. Imported agents are untouched — they are ordinary owned
-- agents and carry no reference to this table.
--
-- The old one-row-per-(share, importer) index cannot be rebuilt while an
-- account holds several copies of one link, so the extra rows go first: the
-- NEWEST copy keeps its bookkeeping row, which is the one the old handler
-- would have been tracking.
DELETE FROM agent_share_imports a
 USING agent_share_imports b
 WHERE a.share_id = b.share_id
   AND a.importer = b.importer
   AND a.copy_no  < b.copy_no;

DROP INDEX IF EXISTS idx_share_imports_copy;
CREATE UNIQUE INDEX IF NOT EXISTS idx_share_imports_once
    ON agent_share_imports(share_id, importer);

ALTER TABLE agent_share_imports DROP COLUMN IF EXISTS sync_count;
ALTER TABLE agent_share_imports DROP COLUMN IF EXISTS synced_at;
ALTER TABLE agent_share_imports DROP COLUMN IF EXISTS copy_no;
