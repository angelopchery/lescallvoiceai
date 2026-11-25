"""
Full-coverage document/dump mining (PLAN-doc-dump-full-mining).

Requirement: a document or big pasted text that already answers interview
topics (objections WITH responses, call flow, closings, guardrails, persona,
slots, …) must close those topics so the Copilot never re-asks them — and
`derived` content must never overwrite what the user confirmed by hand.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from conftest import REPO_ROOT


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
    return cop, tmp_path


def _guard(session, sid="s1"):
    return {"user_turns": 3, "created_this_turn": False,
            "session": session, "user_message": "here is everything", "sid": sid}


def _fresh_session():
    return {"messages": [], "transcript": [], "meta": {}}


# What a rich all-sections document extracts to (the extractor's field names).
_ALL_SECTION_FIELDS = {
    "agent_name": "Vishal Patel",
    "company_name": "Pramukh Group",
    "role_description": "outbound real-estate agent calling interested buyers",
    "call_direction": "outbound",
    "call_goal": "Collect location, size and budget preferences and book a "
                 "site visit for the best-matching Pramukh project.",
    "stages": [
        {"id": "CONFIRM_IDENTITY", "goal": "Greet the caller by name, confirm "
         "you are speaking with the right person, then introduce yourself and "
         "ask if it is a good time to talk before continuing."},
        {"id": "DISCOVERY", "goal": "Ask for location, property size and budget "
         "preferences one at a time and store each answer before moving on."},
        {"id": "CLOSING", "goal": "Thank the caller warmly, confirm the WhatsApp "
         "number and wish them a great day before ending the call."}],
    "objection_playbook": [
        {"id": "too_expensive", "patterns": ["too expensive", "over budget"],
         "answer": {"en": "No worries — we can find options in your budget."}}],
    "closing_responses": {"en": "Thank you for your time — have a wonderful day!"},
    "opening_greeting": "Good {current_time}, am I speaking with {contact_name}?",
    "personality": {"tone": "friendly", "energy": "medium", "style": "conversational"},
    "languages": {"supported": ["en"], "default": "en"},
    "slots": [{"name": "location_preferences", "required": True,
               "question": {"en": "Which location do you prefer?"}},
              {"name": "budget_preferences", "required": True,
               "question": {"en": "What is your budget?"}}],
    "portfolio_prompt_text": "Pramukh Group builds premium residential projects "
                             "across Surat, Vapi and Silvassa. Flagship projects: "
                             "Pramukh Revanta (Vesu, 3BHK) and Pramukh Aura (Vapi).",
    # The upgraded extractor ALWAYS emits catalog items whenever the document
    # names products (a sales agent's knowledge gate requires them).
    "catalog": [
        {"id": "pramukh-revanta", "name": "Pramukh Revanta",
         "location": "Vesu", "configurations": ["3BHK"]},
        {"id": "pramukh-aura", "name": "Pramukh Aura", "location": "Vapi"}],
    "brand": {"credibility_markers": ["30+ years", "Zero stalled projects"]},
    "guardrails": {"never_rules": ["Never disparage other builders."]},
    "appointment_slots": [{"day": "Tomorrow", "time": "10:00 AM"}],
}


def test_all_section_fields_close_their_layers(cop):
    cop_mod, _tmp = cop
    session = _fresh_session()
    _ch, _notes, progress, closed = cop_mod._merge_extracted_into_draft(
        session, dict(_ALL_SECTION_FIELDS))
    done = {l["id"] for l in progress["layers"] if l["done"]}
    for layer in ("business_identity", "call_objective", "persona_voice",
                  "brand_trust", "call_flow", "data_capture",
                  "knowledge_catalog", "objections_branching",
                  "scheduling_followup", "guardrails_compliance",
                  "greeting_close"):
        assert layer in done, f"{layer} should be closed by the document"
    # the ONLY genuinely open question left is the voice (never in a document)
    assert progress["open_layers"] == ["languages"]
    assert len(closed) >= 10


def test_confirmed_answers_beat_document_content(cop):
    cop_mod, _tmp = cop
    session = _fresh_session()
    # user confirms the greeting by hand first
    _run(cop_mod._execute_tool("update_draft", {"changes": {
        "response_bank": {"opening_greeting": "USER'S OWN greeting line here"}}},
        _guard(session)))
    _ch, notes, _p, _c = cop_mod._merge_extracted_into_draft(
        session, {"opening_greeting": "document greeting that must lose",
                  "company_name": "Pramukh Group"})
    draft = session["interview"]["draft"]
    assert draft["response_bank"]["opening_greeting"] == "USER'S OWN greeting line here"
    assert draft["identity"]["company_name"] == "Pramukh Group"  # gap still filled
    assert any("confirmed answers" in n for n in notes)


def test_document_never_replaces_existing_flow(cop):
    cop_mod, _tmp = cop
    session = _fresh_session()
    stages = [{"id": f"S{i}", "goal": "A substantive goal sentence long enough "
               "to steer a real call through this specific stage."} for i in range(4)]
    _run(cop_mod._execute_tool("update_draft", {"changes": {
        "call_flow": {"stages": stages}}}, _guard(session)))
    _ch, notes, _p, _c = cop_mod._merge_extracted_into_draft(
        session, {"stages": [{"id": "DOC_STAGE", "goal": "One doc stage that "
                              "must not clobber the interview's flow at all."}]})
    ids = [s["id"] for s in session["interview"]["draft"]["call_flow"]["stages"]]
    assert len(ids) == 4 and "DOC_STAGE" not in ids
    # blocked either by confirmed-provenance protection or the flow guard
    assert any("kept" in n for n in notes)


def test_ingest_document_reports_closed_topics(cop, monkeypatch):
    cop_mod, tmp = cop
    session = _fresh_session()
    updir = tmp / "uploads" / "s1"
    updir.mkdir(parents=True)
    (updir / "playbook.txt").write_text("full playbook text", encoding="utf-8")
    import backend.app.documents.yaml_filler as yf
    monkeypatch.setattr(yf, "extract_fields_from_text",
                        lambda text: {"status": "ok", "missing": [],
                                      "fields": dict(_ALL_SECTION_FIELDS)})
    res, summary, ok, _m = _run(cop_mod._execute_tool(
        "ingest_document", {"doc_id": "playbook.txt"}, _guard(session)))
    assert ok and res["status"] == "ingested"
    assert res["open_layers"] == ["languages"]
    assert "Objections" in res["topics_closed"]
    assert "closed:" in summary


def test_chunk_merge_rules():
    from backend.app.documents.yaml_filler import _merge_chunk_fields
    base = {"locations": ["Surat"], "brand": {"credibility_markers": ["30+ years"]},
            "company_name": "Pramukh Group"}
    out = _merge_chunk_fields(base, {
        "locations": ["Surat", "Vapi"],                 # list extends, deduped
        "brand": {"trust_proofs": ["Zero stalled"],     # dict fills new keys
                  "credibility_markers": ["LOSES"]},    # existing dict key kept
        "company_name": "LOSES",                        # scalar first-wins
        "call_goal": "Book site visits."})              # new key lands
    assert out["locations"] == ["Surat", "Vapi"]
    assert out["brand"]["credibility_markers"] == ["30+ years"]
    assert out["brand"]["trust_proofs"] == ["Zero stalled"]
    assert out["company_name"] == "Pramukh Group"
    assert out["call_goal"] == "Book site visits."


# ─── the auto-miner trigger (Kaapd live test 2026-07-15: dump as 2nd msg) ────

def test_should_mine_dump_trigger(cop):
    cop_mod, _tmp = cop
    big = "x" * 2000
    # the live incident: dump pasted as the SECOND message, nothing recorded
    session = {"messages": [
        {"role": "user", "content": "I want to create a new voice agent."},
        {"role": "assistant", "content": "What is the name of your company?"},
        {"role": "user", "content": big}], "transcript": [], "meta": {}}
    assert cop_mod._should_mine_dump(session, big, [])
    # very first message of a fresh session
    assert cop_mod._should_mine_dump(
        {"messages": [], "transcript": [], "meta": {}}, big, [])
    # short message / attachment present → no
    assert not cop_mod._should_mine_dump(session, "short", [])
    assert not cop_mod._should_mine_dump(session, big, ["doc.txt"])
    # edit session (get_agent already used) → no
    edit_session = {"messages": [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_agent", "arguments": "{}"}}]}],
        "transcript": [], "meta": {}}
    assert not cop_mod._should_mine_dump(edit_session, big, [])
    # agent already built from this interview → no
    built = {"messages": [], "transcript": [], "meta": {},
             "interview": {"draft": {"identity": {}}, "skipped": [],
                           "provenance": {}, "phase": "built"}}
    assert not cop_mod._should_mine_dump(built, big, [])


def test_en_only_playbook_closes_objections_and_empty_lists_pruned(cop):
    cop_mod, _tmp = cop
    from copilot_interview import _done_objections
    session = _fresh_session()
    ch, _n, prog, _c = cop_mod._merge_extracted_into_draft(session, {
        "languages": {"supported": ["en", "hi"], "default": "hi"},
        "objection_playbook": [
            {"id": "have_suppliers", "patterns": ["already have suppliers"],
             "answer": {"en": "One sample kit costs nothing to compare."}}],
        "objection_responses": {"en": [], "hi": []},   # extractor hollow shell
    })
    rb = session["interview"]["draft"].get("response_bank") or {}
    assert "objection_responses" not in rb             # shells never land
    ok, missing = _done_objections(session["interview"]["draft"])
    assert ok, missing                                  # en-only answers close it
