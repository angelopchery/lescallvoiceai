"""
Interview engine e2e (plan §11 P5 gate): mid-way info dump, one explicit skip,
one document upload/ingest, create REFUSED until every compulsory layer
closes, then create succeeds consuming the draft. Old sessions replay fine.

Drives the copilot tool layer directly (no LLM, no HTTP).
"""
from __future__ import annotations

import asyncio
import shutil
import sys

import pytest

from conftest import AGENTS_DIR, REPO_ROOT


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def cop(tmp_path, monkeypatch):
    monkeypatch.setenv("LESCALL_DB_URL", f"sqlite:///{tmp_path}/test.db")
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_builder_api as builder
    import agent_copilot_api as cop
    agents_dir = tmp_path / "agents"
    agents_dir.mkdir()
    monkeypatch.setattr(builder, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(cop, "_UPLOADS_DIR", tmp_path / "uploads")
    return cop, agents_dir, tmp_path


_DRAFT_DUMP = {
    # A mid-interview info dump covering several layers at once.
    "identity": {"agent_name": "Kavya", "company_name": "GreenLeaf Organics",
                 "role_description": "outbound organic-produce sales consultant"},
    "call_direction": "outbound",
    "call_flow": {
        "call_goal": "Pitch the weekly organic veggie box and book a trial delivery.",
        "stages": [
            {"id": "OPENING", "order": 0, "role": "confirm_person",
             "goal": "Confirm you reached the right person and check it is a good time to talk before anything else."},
            {"id": "PITCH", "order": 1, "role": "pitch",
             "goal": "Pitch the weekly organic box: farm-fresh, pesticide-free, delivered every Sunday. Lead with the free-trial offer."},
            {"id": "CLOSING", "order": 2, "role": "closing",
             "goal": "Thank them warmly by name and restate the agreed next step in one sentence."},
        ],
    },
    "languages": {"supported": ["hi", "en"], "default": "hi",
                  "tts_voices": {"hi": {"speaker": "simran"}, "en": {"speaker": "simran"}}},
    "personality": {"tone": "friendly", "energy": "high", "ai_disclosure": "admit"},
    "brand": {"trust_proofs": ["5000+ happy families", "certified organic farms"]},
    "slots": [
        {"name": "name", "priority": 10, "required": True,
         "question": {"hi": "आपका नाम क्या है?", "en": "May I know your name?"}},
        {"name": "delivery_area", "priority": 20, "required": True, "type": "text",
         "question": {"hi": "आप किस area में रहते हैं?", "en": "Which area do you live in?"}},
    ],
    "response_bank": {
        "opening_greeting": "नमस्ते! मैं Kavya बोल रही हूँ GreenLeaf Organics से।",
        "closing_responses": {"hi": "धन्यवाद! आपका दिन शुभ रहे।", "en": "Thank you! Have a great day."},
        "objection_playbook": [
            {"id": "price", "patterns": ["too expensive", "mehenga"],
             "answer": {"hi": "समझती हूँ — {trust_proofs}। एक trial box try कीजिए।",
                        "en": "I understand — {trust_proofs}. Try one trial box."}},
        ],
    },
    "guardrails": {"never_rules": ["quote delivery dates not on the schedule"],
                   "compliance": {"dnd_phrases": ["stop calling", "remove my number"]}},
    "calendar": {"mock_slots": [{"day": "Sunday", "time": "9:00 AM"}]},
}


def test_interview_flow_create_gate(cop):
    cop_mod, agents_dir, tmp = cop
    session: dict = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 4, "created_this_turn": False,
             "session": session, "user_message": "use defaults where needed — build it now", "sid": "s1"}

    # 1. Create with an empty draft → refused, naming open layers.
    with pytest.raises(ValueError, match="interview is not finished"):
        _run(cop_mod._execute_tool("create_agent", {
            "name": "greenleaf", "definition": {"identity": {"agent_name": "Kavya"}},
        }, guard))

    # 2. Info dump mid-way → update_draft mines it; progress is server-computed.
    res, _s, ok, mutated = _run(cop_mod._execute_tool(
        "update_draft", {"changes": _DRAFT_DUMP}, guard))
    assert ok and not mutated
    assert res["status"] == "draft_updated"
    open_layers = set(res["progress"]["open_layers"])
    # knowledge + scheduling-adjacent layers still open
    assert "knowledge_catalog" in open_layers

    # 3. Still refused (knowledge layer open).
    with pytest.raises(ValueError, match="interview is not finished"):
        _run(cop_mod._execute_tool("create_agent", {
            "name": "greenleaf", "definition": {}}, guard))

    # 4. Document upload satisfies the knowledge layer via ingest_document.
    updir = tmp / "uploads" / "s1"
    updir.mkdir(parents=True)
    (updir / "brochure.txt").write_text(
        "GreenLeaf Organics weekly box: seasonal vegetables, certified organic, "
        "₹499/week, free trial box for new families, delivery across Surat.",
        encoding="utf-8")

    # ingest_document calls the LLM extractor — stub it (no network in tests).
    import backend.app.documents.yaml_filler as yf

    def _fake_extract(text):
        return {"status": "ok", "missing": [], "fields": {
            "portfolio_prompt_text": ("Weekly organic vegetable box, ₹499/week, certified "
                                      "organic farms, free trial box for new families, "
                                      "delivery across Surat every Sunday morning."),
            "locations": ["Surat"],
            # The upgraded extractor emits catalog items whenever the doc
            # names products — a sales draft's knowledge gate requires them.
            "catalog": [{"id": "weekly-organic-box",
                         "name": "Weekly Organic Vegetable Box",
                         "price_range": "₹499/week",
                         "pitch_points": ["certified organic farms",
                                          "free trial box for new families"]}],
        }}
    orig = yf.extract_fields_from_text
    yf.extract_fields_from_text = _fake_extract
    try:
        res2, _s, ok2, _m = _run(cop_mod._execute_tool(
            "ingest_document", {"doc_id": "brochure.txt"}, guard))
    finally:
        yf.extract_fields_from_text = orig
    assert ok2 and res2["status"] == "ingested"
    assert "knowledge_catalog" not in set(res2["open_layers"])

    # 5. One explicit skip (post_call is optional; skip a compulsory one that
    #    may still be open — data capture per-language coverage is satisfied,
    #    so whatever remains gets answered or skipped).
    progress = cop_mod.interview_progress(session["interview"]["draft"],
                                          session["interview"]["skipped"])
    for open_id in list(progress["open_layers"]):
        guard.pop("skips_this_turn", None)  # each skip on its own turn
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": open_id,
            "user_words": "skip that, just use defaults"}, guard))
    progress = cop_mod.interview_progress(session["interview"]["draft"],
                                          session["interview"]["skipped"])
    assert progress["complete"]

    # 6. Create now succeeds and CONSUMES the draft (thin tool call).
    res3, _s, ok3, mutated3 = _run(cop_mod._execute_tool("create_agent", {
        "name": "greenleaf", "definition": {}}, guard))
    assert ok3 and mutated3 and res3["status"] == "created"
    assert session["interview"]["phase"] == "built"

    from backend.app.agent_definition.schema import AgentDefinition
    agent = AgentDefinition.from_yaml(agents_dir / res3["name"] / "agent.yaml")
    # Draft content survived even though the create call carried nothing:
    assert agent.identity.agent_name == "Kavya"
    assert agent.call_direction == "outbound"
    assert agent.call_flow.call_goal.startswith("Pitch the weekly organic")
    assert [s.id for s in agent.call_flow.stages] == ["OPENING", "PITCH", "CLOSING"]
    assert agent.brand.trust_proofs == ["5000+ happy families", "certified organic farms"]
    assert len(agent.slots) == 2
    assert agent.response_bank.objection_playbook
    assert "Surat" in agent.knowledge_base.locations
    assert agent.guardrails.compliance.get("dnd_phrases") == ["stop calling", "remove my number"]
    # provenance recorded confirmed vs derived (doc content = derived)
    prov = session["interview"]["provenance"]
    assert prov.get("identity.agent_name") == "confirmed"
    assert prov.get("knowledge_base.portfolio_prompt_text") == "derived"


def test_skip_layer_requires_words_and_valid_id(cop):
    cop_mod, _a, _t = cop
    session: dict = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 2, "session": session, "user_message": "skip", "sid": "s2"}
    with pytest.raises(ValueError, match="No interview layer"):
        _run(cop_mod._execute_tool("skip_layer",
                                   {"layer_id": "nope", "user_words": "skip"}, guard))
    with pytest.raises(ValueError, match="user's own words"):
        _run(cop_mod._execute_tool("skip_layer",
                                   {"layer_id": "brand_trust", "user_words": ""}, guard))


def test_old_session_replay_unaffected(cop):
    """Sessions without interview state keep loading (lazy setdefault)."""
    cop_mod, _a, _t = cop
    session = {"messages": [{"role": "user", "content": "hi"}],
               "transcript": [], "meta": {"owner": "x@y.com"}}
    iv = cop_mod._interview_state(session)
    assert iv["phase"] == "idle" and iv["draft"] == {}
    # System prompt renders without an interview block for idle sessions.
    prompt = cop_mod._system_prompt(session)
    assert "INTERVIEW STATE (server-tracked" not in prompt
    # ... and includes it once a draft exists.
    iv["draft"] = {"identity": {"company_name": "X"}}
    iv["phase"] = "interviewing"
    prompt2 = cop_mod._system_prompt(session)
    assert "INTERVIEW STATE (server-tracked" in prompt2
    assert "NEXT: ask exactly this ONE question" in prompt2


# ── server-verified consent gates (2026-07-23 copilot audit) ────────────────
# Two "don't ask me anything" messages used to mass-skip 9 layers and ship a
# fully fabricated agent; a brochure paste triggered an unsolicited create;
# one skip cited BROCHURE text as the user's words. These pin the gates.

def test_create_refused_without_explicit_build_instruction(cop):
    cop_mod, _agents_dir, _tmp = cop
    session = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 4, "created_this_turn": False, "session": session,
             "user_message": "here is our brochure text about the clinic",
             "sid": "sg1"}
    with pytest.raises(ValueError, match="Held until the user says yes"):
        _run(cop_mod._execute_tool("create_agent",
                                   {"name": "gate_test", "definition": {}},
                                   guard))


def test_long_paste_with_incidental_verb_is_not_consent(cop):
    cop_mod, _agents_dir, _tmp = cop
    session = {"messages": [], "transcript": [], "meta": {}}
    # 1500+ chars containing "build" far from any agent-noun — a paste, not
    # an instruction.
    paste = ("we build lasting smiles for families. " * 60)
    guard = {"user_turns": 4, "created_this_turn": False, "session": session,
             "user_message": paste, "sid": "sg2"}
    with pytest.raises(ValueError, match="Held until the user says yes"):
        _run(cop_mod._execute_tool("create_agent",
                                   {"name": "gate_test2", "definition": {}},
                                   guard))


def test_skip_refused_when_words_are_not_the_users(cop):
    cop_mod, _agents_dir, _tmp = cop
    session = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 3, "session": session,
             "user_message": "tell me about pricing", "sid": "sg3"}
    with pytest.raises(ValueError, match="don't appear in anything the user"):
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": "business_identity",
            "user_words": "patients should share their name"}, guard))


def test_skip_capped_at_three_per_turn(cop):
    cop_mod, _agents_dir, _tmp = cop
    session = {"messages": [], "transcript": [], "meta": {}}
    guard = {"user_turns": 3, "session": session,
             "user_message": "skip everything, use defaults", "sid": "sg4"}
    layers = [l.id for l in cop_mod.INTERVIEW_LAYERS][:4]
    for lid in layers[:3]:
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": lid, "user_words": "skip everything"}, guard))
    with pytest.raises(ValueError, match="3 layers already skipped"):
        _run(cop_mod._execute_tool("skip_layer", {
            "layer_id": layers[3], "user_words": "skip everything"}, guard))
