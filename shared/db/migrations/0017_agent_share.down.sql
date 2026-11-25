-- Rollback 0017. Agents created BY an import are untouched: they are ordinary
-- owned agents with their own slugs and carry no reference to these tables.
-- Only the links and the import bookkeeping go away.
DROP INDEX IF EXISTS idx_share_imports_importer;
DROP INDEX IF EXISTS idx_share_imports_once;
DROP TABLE IF EXISTS agent_share_imports;
DROP INDEX IF EXISTS idx_agent_shares_owner;
DROP INDEX IF EXISTS idx_agent_shares_live;
DROP TABLE IF EXISTS agent_shares;
