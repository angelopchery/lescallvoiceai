"""Post-call intelligence layer.

Modules:
    config   — env-driven configuration (LLM endpoints, models, worker tuning)
    queue    — intel_jobs Postgres job queue (enqueue / claim / complete / fail)
    worker   — long-lived pipeline worker process (lescall-workers container)
    extract  — stage 1: LLM intelligence extraction
    embed    — stage 2: embeddings + FTS indexing
    followup — stage 3: follow-up decision engine
    memory   — stage 4: customer memory merge
    automation — stage 5: rule evaluation + action execution
    notify   — stage 6: notifications

The pipeline is Postgres-only (pgvector + SKIP LOCKED); on the sqlite dev
fallback every entry point degrades to a no-op so the CLI voice pipeline is
unaffected.
"""
