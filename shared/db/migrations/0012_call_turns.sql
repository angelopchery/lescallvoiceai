-- 0012: turn-level call traces.
--
-- Per-turn latency components (STT wall time, LLM first-chunk/total,
-- speech_end→engine, speech_end→first_audio) and quality signals
-- (derived STT confidence, barge-in, filler) were computed on every turn
-- but existed only as INFO log lines and a transient EventHub payload —
-- rotated away, never queryable. "Did median turn latency regress this
-- week?" required hand-joining logs.
--
-- The pipeline now collects one record per caller turn in memory
-- (VoicePipeline.turn_traces) and the bridge batch-inserts them at
-- teardown, after — and transactionally independent of — the calls row.
--
-- Canonical query (median turn latency, last 7 days, real turns only):
--   SELECT percentile_cont(0.5) WITHIN GROUP
--            (ORDER BY speech_end_to_first_audio_ms) AS median_ms
--   FROM call_turns t JOIN calls c ON c.id = t.call_id
--   WHERE c.started_at >= (now() - interval '7 days')::text
--     AND NOT t.is_filler AND t.speech_end_to_first_audio_ms IS NOT NULL;
-- (calls.started_at is TEXT-ish ISO — the ::text comparison keeps the
--  existing string-timestamp convention; see database.py _TextLoader.)

CREATE TABLE call_turns (
    id          BIGSERIAL PRIMARY KEY,
    call_id     BIGINT NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
    turn_index  INTEGER NOT NULL,
    ts          TIMESTAMPTZ,          -- when the caller's turn hit the engine
    stage       TEXT,                 -- conversation stage at the time
    intent      TEXT,                 -- engine-classified intent of the reply
    language    TEXT,
    user_chars  INTEGER,
    agent_chars INTEGER,
    stt_latency_ms               REAL,
    stt_confidence               REAL, -- derived score (0012+), NULL = unknown
    speech_end_to_engine_ms      REAL, -- endpoint gate + STT + merge + queue
    llm_first_chunk_ms           REAL,
    llm_total_ms                 REAL,
    speech_end_to_first_audio_ms REAL, -- the number latency work optimizes
    is_filler   BOOLEAN NOT NULL DEFAULT FALSE,
    barged_in   BOOLEAN NOT NULL DEFAULT FALSE, -- caller interrupted this reply
    UNIQUE (call_id, turn_index)      -- reconnect upserts re-run the insert
);

CREATE INDEX idx_call_turns_call ON call_turns(call_id);

-- Window queries (analytics doctrine: window on started_at) had no index.
CREATE INDEX idx_calls_started ON calls(started_at);
