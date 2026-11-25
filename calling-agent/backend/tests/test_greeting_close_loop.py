"""
Greeting/close re-ask loop regression suite (incident 2026-07-15, #2).

The user dictated closings per situation ({lang: {site_visit:…, thank_you:…}}),
but the engine's closing_responses is {lang: ONE string}. _lang_dict filtered
the nested dicts to {} and the write reported SUCCESS with
recorded_paths=[response_bank.closing_responses] while saving nothing — so
_done_greeting_close never passed and the interview re-asked the same final
question forever.

Covers: nested per-situation closings flatten to the thank-you line per
language (with an explanatory note); unusable shapes error repairably instead
of lying; emptied branches are pruned recursively so recorded_paths is honest.
"""
from __future__ import annotations

import asyncio
import copy
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
    return cop


def _guard(session):
    return {"user_turns": 3, "created_this_turn": False,
            "session": session, "user_message": "interview answer"}


def _fresh_session():
    return {"messages": [], "transcript": [], "meta": {}}


# The literal shape the model sent at turns 128/133 (trimmed).
_NESTED_CLOSINGS = {
    "en": {"site_visit": "That's wonderful! When would you like to come?",
           "whatsapp_number": "Is your WhatsApp number the same as this one?",
           "thank_you": "Thank you for your time. I'll send the details on "
                        "WhatsApp shortly. Have a wonderful day ahead!",
           "no_site_visit": "No worries at all. I'll share photos instead."},
    "hi": {"site_visit": "बहुत बढ़िया! आप कब आना चाहेंगे?",
           "thank_you": "धन्यवाद जी, आपने अपना समय दिया। आपका दिन शुभ रहे!"},
    "gu": {"site_visit": "ખૂબ સરસ! તમે ક્યારે આવવા ઇચ્છો છો?",
           "thank_you": "તમારો સમય આપવા બદલ આભાર. તમારો દિવસ શુભ રહે!"},
}


def _seed_langs_and_greeting(cop, session):
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "languages": {"supported": ["hi", "en", "gu"], "default": "hi"},
        "response_bank": {"opening_greeting": "Hey there, am I speaking with "
                                              "the property enquirer?"}}},
        _guard(session)))
    assert ok


def test_nested_closings_flatten_and_layer_closes(cop):
    session = _fresh_session()
    _seed_langs_and_greeting(cop, session)
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"closing_responses": copy.deepcopy(_NESTED_CLOSINGS)}}},
        _guard(session)))
    assert ok and any(p.startswith("response_bank.closing_responses")
                      for p in res["recorded_paths"])
    cr = session["interview"]["draft"]["response_bank"]["closing_responses"]
    # one STRING per language, and it is the thank-you (closing-most) line
    assert set(cr) == {"en", "hi", "gu"}
    assert all(isinstance(v, str) and v.strip() for v in cr.values())
    assert cr["en"].startswith("Thank you for your time")
    assert any("nested per-situation" in n for n in res["notes"])
    # the layer the loop was stuck on is now done
    from copilot_interview import _done_greeting_close
    done, missing = _done_greeting_close(session["interview"]["draft"])
    assert done, missing


def test_unusable_closing_shape_errors_repairably_not_fake_success(cop):
    session = _fresh_session()
    _seed_langs_and_greeting(cop, session)
    with pytest.raises(Exception) as ei:
        _run(cop._execute_tool("update_draft", {"changes": {
            "response_bank": {"closing_responses": {"en": ["a", "list"],
                                                    "hi": None}}}},
            _guard(session)))
    msg = str(ei.value)
    assert "closing_responses" in msg and "NOT saved" in msg
    assert "do NOT ask them to rephrase" in msg
    # nothing half-written into the draft
    rb = session["interview"]["draft"].get("response_bank") or {}
    assert "closing_responses" not in rb or rb["closing_responses"]


def test_emptied_branches_pruned_recursively(cop):
    session = _fresh_session()
    # a deep branch whose content all normalizes away must raise, not record
    with pytest.raises(Exception):
        _run(cop._execute_tool("update_draft", {"changes": {
            "response_bank": {"ai_disclosure_responses": {"en": ["nope"]}}}},
            _guard(session)))
    assert "response_bank" not in (session["interview"]["draft"].keys()
                                   if session.get("interview") else [])


def test_plain_lang_string_closings_unchanged(cop):
    session = _fresh_session()
    _seed_langs_and_greeting(cop, session)
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"closing_responses": {
            "hi": "धन्यवाद, आपका दिन शुभ रहे!",
            "en": "Thanks for your time, goodbye!",
            "gu": "આભાર, તમારો દિવસ શુભ રહે!"}}}},
        _guard(session)))
    assert ok
    cr = session["interview"]["draft"]["response_bank"]["closing_responses"]
    assert cr["en"] == "Thanks for your time, goodbye!"
    assert not any("nested" in n for n in res["notes"])
