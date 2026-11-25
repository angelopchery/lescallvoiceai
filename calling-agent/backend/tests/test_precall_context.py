"""Pre-call customer context (Customer Intelligence Hub read side).

Proves: the KNOWN CUSTOMER band renders only for returning customers with
context; set_customer_context prefills the no-re-ask name slot and honors
the per-agent memory.use_customer_context opt-out; an unknown caller's
prompt is byte-identical to before the feature; the bridge fetch returns
the digest + thin header from the precomputed column and caches it.
"""

from __future__ import annotations

import json

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401  (fixtures)


def make_router(memory_cfg=None, **over) -> ConversationRouter:
    d = {
        "agent_id": "t_precall",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en", "tts_voices": {}},
        "call_flow": {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            ],
        },
        "calendar": {"mock_slots": []},
        "response_bank": {"opening_greeting": "Hello {contact_name}!"},
    }
    if memory_cfg is not None:
        d["memory"] = memory_cfg
    d.update(over)
    return ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                              default_language="en")


CTX = {
    "contact_id": 7, "name": "Ramesh Shah", "calls_count": 4,
    "last_call_at": "2026-07-14 10:00:00+00:00", "last_outcome": "callback_requested",
    "open_followups": 1, "open_commitments": 2,
    "digest": "4 previous call(s). Interested in: 3BHK Skyline. "
              "Open promises: agent: send brochure (2026-07-15).",
}


def test_known_customer_band_renders():
    r = make_router()
    r.set_customer_context(CTX)
    prompt = r._build_system_prompt()
    assert "━━ KNOWN CUSTOMER ━━" in prompt
    assert "Ramesh Shah" in prompt and "4 previous call(s)" in prompt
    assert "3BHK Skyline" in prompt
    assert "Open promises: 2" in prompt and "Pending follow-ups: 1" in prompt
    assert "NEVER recite" in prompt


def test_unknown_caller_prompt_unchanged():
    baseline = make_router()._build_system_prompt()
    r = make_router()
    r.set_customer_context(None)
    r.set_customer_context({})
    assert r._build_system_prompt() == baseline
    assert "KNOWN CUSTOMER" not in baseline


def test_agent_flag_suppresses_context():
    r = make_router(memory_cfg={"use_customer_context": False})
    r.set_customer_context(CTX)
    prompt = r._build_system_prompt()
    assert "KNOWN CUSTOMER" not in prompt
    assert r.memory.call_state.customer_name in (None, "")


def test_known_name_prefills_and_personalizes_greeting():
    import asyncio

    r = make_router()
    r.set_customer_context(CTX)
    assert r.memory.call_state.customer_name == "Ramesh Shah"
    res = asyncio.run(r.generate_opening())
    assert "Ramesh Shah" in res.text  # {contact_name} placeholder resolved


def test_outbound_dial_name_wins_over_memory_name():
    r = make_router()
    r.set_call_context(direction="outbound", contact_name="Suresh")
    r.set_customer_context(CTX)
    assert r.outbound_contact_name == "Suresh"  # dial identity is authoritative


def test_bridge_fetch_reads_precomputed_digest(pg_test_db):
    import importlib.util
    from pathlib import Path

    from shared.db.database import get_db

    repo = Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "conversation_call_bridge_test", repo / "conversation_call_bridge.py")
    # Importing the full bridge pulls the audio stack; fetch the function via
    # a lightweight exec of just what we need instead.
    src = (repo / "conversation_call_bridge.py").read_text()
    ns: dict = {}
    import logging, time  # noqa: E401

    ns.update({"logging": logging, "time": time,
               "logger": logging.getLogger("test_bridge"),
               "INTEL_PRECALL_CONTEXT": True,
               "_customer_ctx_cache": {}, "_CUSTOMER_CTX_TTL_S": 300.0,
               "_CUSTOMER_CTX_CACHE_MAX": 512})
    start = src.index("def _fetch_customer_context")
    end = src.index("def _resolve_call_routing")
    exec(compile(src[start:end], "bridge_fetch", "exec"), ns)
    fetch = ns["_fetch_customer_context"]

    with get_db() as db:
        contact = db.execute(
            """INSERT INTO contacts (owner, phone, sig_digits, name)
               VALUES ('t@x.com', '+919899887766', '9899887766', 'Meera')
               ON CONFLICT (owner, phone) DO UPDATE SET name=EXCLUDED.name
               RETURNING id""").fetchone()
        db.execute(
            """INSERT INTO customer_memory (contact_id, owner, memory, calls_count,
                                            last_call_at, context_digest, digest_updated_at)
               VALUES (?, 't@x.com', '{}', 3, '2026-07-14T10:00:00Z',
                       '3 previous call(s). Interested in: 2BHK.', now())
               ON CONFLICT (contact_id) DO UPDATE SET
                   context_digest=EXCLUDED.context_digest,
                   calls_count=EXCLUDED.calls_count""",
            (contact["id"],))

    ctx = fetch("t@x.com", "09899887766")  # 0-prefix variant still matches
    assert ctx is not None
    assert ctx["name"] == "Meera" and ctx["calls_count"] == 3
    assert "2BHK" in ctx["digest"]

    assert fetch("t@x.com", "12345") is None          # unknown caller
    assert fetch("", "09899887766") is None            # no owner resolved
    # cache: second call must not touch the DB (poison the URL to prove it)
    import os
    old = os.environ.get("LESCALL_DB_URL")
    os.environ["LESCALL_DB_URL"] = "postgresql://invalid:1/none"
    try:
        ctx2 = fetch("t@x.com", "+919899887766")
        assert ctx2 == ctx
    finally:
        os.environ["LESCALL_DB_URL"] = old


# ── RUNBOOK §77: a primary-project agent is not shown other projects ────────
# customer_memory is per owner + contact and shared by every agent of that
# owner. Call 1047 (Green County agent) switched the pitch to Swarit on a
# ₹2.5 Cr budget; the memory then said "Interested in: Pramukh Swarit
# bungalows (primary)" and calls 1060/1061 opened with "you showed interest
# in our Pramukh Swarit bungalows". The band is scoped; the row is not.
GC = {"id": "pramukh-green-county", "name": "Pramukh Green County",
      "aliases": ["Green County"], "location": "Chharwada, Vapi",
      "configurations": ["3 BHK", "4 BHK"], "price_range": "about ₹78–85 lakh"}
SWARIT = {"id": "pramukh-swarit", "name": "Pramukh Swarit",
          "aliases": ["Swarit", "Swarit Bungalows"], "location": "New Chala, Vapi",
          "configurations": ["4 BHK", "5 BHK"], "price_range": "about ₹2.15 Cr"}
KB = {"known_projects": {}, "locations": ["Vapi"], "portfolio_prompt_text": "",
      "catalog": [GC, SWARIT], "match_rules": []}
DIGEST_1060 = (
    "1 previous call(s). Last intent: Book a site visit.. Prefers Hindi. "
    "Interested in: Pramukh Swarit bungalows (primary); investment. "
    "Open promises: customer: Customer will visit Pramukh Swarit on Wednesday "
    "14th October at 4 PM. (2026-10-09); agent: send the brochure (2026-10-09). "
    "Past objections: budget mismatch (Pramukh Green County). "
    "Decision makers: Shubhangi.")


def _primary_router(primary: bool = True) -> ConversationRouter:
    kb = dict(KB)
    if primary:
        kb["primary_project"] = {"id": "pramukh-green-county", "mode": "exclusive"}
    return make_router(knowledge_base=kb)


def _band(prompt: str) -> str:
    i = prompt.find("━━ KNOWN CUSTOMER ━━")
    assert i >= 0
    j = prompt.find("\n━━", i + 1)
    return prompt[i:j if j > 0 else None]


def test_scope_digest_drops_only_items_naming_other_projects():
    from app.voice.prompt_blocks import scope_digest_to_primary
    r = _primary_router()
    scoped, dropped = scope_digest_to_primary(DIGEST_1060, r._def.knowledge_base)
    assert "Swarit" not in scoped
    for kept in ("1 previous call(s).", "Last intent: Book a site visit..",
                 "Prefers Hindi.", "Interested in: investment.",
                 "Open promises: agent: send the brochure (2026-10-09).",
                 "Past objections: budget mismatch (Pramukh Green County).",
                 "Decision makers: Shubhangi."):
        assert kept in scoped, kept
    assert dropped == [
        "Pramukh Swarit bungalows (primary)",
        "customer: Customer will visit Pramukh Swarit on Wednesday 14th "
        "October at 4 PM. (2026-10-09)",
    ]


def test_known_customer_band_is_scoped_for_a_primary_project_agent():
    r = _primary_router()
    r.set_customer_context(dict(CTX, digest=DIGEST_1060))
    band = _band(r._build_system_prompt())
    assert "Swarit" not in band
    assert "▸ This call is about Pramukh Green County." in band
    assert "Interested in: investment." in band
    assert "Do NOT re-ask what you already know here" in band


def test_without_a_primary_the_band_is_byte_identical():
    from app.voice import prompt_blocks as pb
    a = _primary_router(primary=False)
    a.set_customer_context(dict(CTX, digest=DIGEST_1060))
    b = make_router()
    b.set_customer_context(dict(CTX, digest=DIGEST_1060))
    assert (pb.customer_context_block(a.customer_context, a._def)
            == pb.customer_context_block(b.customer_context))
    assert "Swarit" in "\n".join(pb.customer_context_block(a.customer_context, a._def))


def test_nothing_to_drop_leaves_the_digest_untouched():
    from app.voice.prompt_blocks import scope_digest_to_primary
    r = _primary_router()
    d = ("2 previous call(s). Interested in: 3 BHK; Pramukh Green County. "
         "Decision makers: Shubhangi.")
    assert scope_digest_to_primary(d, r._def.knowledge_base) == (d, [])
    assert scope_digest_to_primary("", r._def.knowledge_base) == ("", [])
