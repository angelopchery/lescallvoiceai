DROP INDEX IF EXISTS idx_call_turns_rag;
ALTER TABLE call_turns DROP COLUMN IF EXISTS rag_trace_id;
ALTER TABLE call_turns DROP COLUMN IF EXISTS rag_sources;
