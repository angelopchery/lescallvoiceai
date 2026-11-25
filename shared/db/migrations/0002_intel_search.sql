-- 0002: Hybrid semantic search — embedding chunks (pgvector) + lexical FTS.
--
-- Requires the vector extension (provisioned by deploy/pg_provision_pgvector.sh;
-- CREATE EXTENSION needs superuser so it does NOT happen here — this migration
-- fails loudly if the extension is missing, which is the desired signal).
--
-- Embeddings: gemini-embedding-001, 768-dim (Matryoshka truncation), cosine.
-- FTS: 'simple' config (no language stemming — transcripts are code-mixed
-- en/hi/gu; stemmers would mangle romanized Hindi). tsv is a stored generated
-- column so writers can't forget to maintain it.

CREATE TABLE call_embeddings (
    id          BIGSERIAL PRIMARY KEY,
    call_id     BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    owner       TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'chunk',   -- chunk|summary
    chunk_index INTEGER NOT NULL DEFAULT 0,
    content     TEXT NOT NULL,
    embedding   vector(768),
    tsv         tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (call_id, kind, chunk_index)
);

CREATE INDEX idx_call_embeddings_hnsw ON call_embeddings
    USING hnsw (embedding vector_cosine_ops);
CREATE INDEX idx_call_embeddings_tsv ON call_embeddings USING gin (tsv);
CREATE INDEX idx_call_embeddings_owner ON call_embeddings (owner, call_id);
CREATE INDEX idx_call_embeddings_trgm ON call_embeddings USING gin (content gin_trgm_ops);
