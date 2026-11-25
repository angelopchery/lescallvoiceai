-- 0018: which uploaded document backed what the agent said, per turn.
--
-- The document RAG places up to RAG_FINAL_TOP_K chunks of a tenant's uploaded
-- documents into the prompt of a turn. Until now the only record of WHICH
-- chunks was a line in a rotating JSONL trace file, so the question a business
-- actually asks — "the agent quoted a price on Tuesday's call, where did that
-- come from?" — was answerable for about as long as the log survived.
--
-- rag_sources is the citation list the retrieval pipeline built for that turn:
-- [{label, document_id, chunk_id, score}]. It is evidence the model was GIVEN,
-- not evidence it claims to have used — that distinction is what makes it an
-- audit record rather than a model self-report, and it is why the list is
-- assembled from the retrieval result and never from the generated text.
--
-- rag_trace_id joins the turn to its full retrieval trace (stage latencies,
-- scores, cache tier, abstention reason) in credit_log/rag-<date>.jsonl while
-- that file still exists.
--
-- Both are nullable: turns on agents with no documents, filler turns, and every
-- turn recorded before this migration legitimately have neither.
--
-- Canonical query (documents cited on a given call):
--   SELECT t.turn_index, s->>'document_id' AS doc, s->>'chunk_id' AS chunk
--     FROM call_turns t,
--          LATERAL jsonb_array_elements(t.rag_sources) s
--    WHERE t.call_id = $1
--    ORDER BY t.turn_index;

ALTER TABLE call_turns ADD COLUMN rag_sources  JSONB;
ALTER TABLE call_turns ADD COLUMN rag_trace_id TEXT;

-- Partial index: only a minority of turns carry evidence (documents are opt-in
-- per agent), so indexing the whole table to find them wastes most of the index.
CREATE INDEX idx_call_turns_rag ON call_turns (call_id)
    WHERE rag_sources IS NOT NULL;
