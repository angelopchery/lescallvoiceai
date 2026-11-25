"""
Input-priority regime: the USER's words outrank the model's authorship.

Requirement (2026-07-15): "Any form of information that has been provided to
the copilot as input must always be preserved, edited and prioritized. The LLM
must only and only use its own free power when there is no input provided from
the user side."

Incident that motivated it (Kaapd build): the brief scripted four objection
answers; the model ALSO invented objection_responses ("I understand you are
busy…") that the user never wrote, and they landed in the live YAML.

The server ranks every leaf — confirmed (user typed) > derived (user's
document) > proposal (model proposed, user accepted) > authored (model's own
invention) — VERIFIES the rank against the user's actual words rather than
trusting the model's `source` claim, and refuses any write that would
overwrite better-sourced content.
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
    return cop


_USER_BRIEF = (
    "When they say they already have suppliers: \"That's perfectly fine — most "
    "of our long-term partners started while keeping their existing suppliers. "
    "One sample kit alongside what you have costs you nothing to compare.\" "
    "He never quotes prices or rate cards on the phone."
)


def _session_with_user_text(text: str) -> dict:
    return {"messages": [{"role": "user", "content": text}],
            "transcript": [], "meta": {}}


def _guard(session):
    return {"user_turns": 3, "created_this_turn": False,
            "session": session, "user_message": "answer", "sid": "s1"}


# ─── verification: the model's `source` claim is not authority ───────────────

def test_invented_content_is_ranked_authored_despite_user_claim(cop):
    session = _session_with_user_text(_USER_BRIEF)
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"objection_responses": {
            # the literal invention from the Kaapd incident — nowhere in the brief
            "en": ["I understand you are busy. I can call you back at a more "
                   "convenient time."]}}},
        "source": "user"}, _guard(session)))
    assert ok
    prov = session["interview"]["provenance"]
    assert prov["response_bank.objection_responses.en"] == "authored"
    assert any("YOUR draft copy" in n for n in res["notes"])


def test_user_wording_is_ranked_confirmed(cop):
    session = _session_with_user_text(_USER_BRIEF)
    _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"objection_playbook": [
            {"id": "have_suppliers", "patterns": ["already have suppliers"],
             "answer": {"en": "That's perfectly fine — most of our long-term "
                              "partners started while keeping their existing "
                              "suppliers. One sample kit alongside what you have "
                              "costs you nothing to compare."}}]}}},
        _guard(session)))
    prov = session["interview"]["provenance"]
    assert prov["response_bank.objection_playbook"] == "confirmed"


def test_short_scalars_are_structure_not_content(cop):
    """Enums/flags/names can't be word-matched against the user's prose — they
    must not be mislabelled as invented."""
    session = _session_with_user_text("make it outbound, admit being an AI")
    _run(cop._execute_tool("update_draft", {"changes": {
        "call_direction": "outbound",
        "personality": {"ai_disclosure": "admit", "tone": "friendly"},
        "identity": {"agent_name": "Karan"}}}, _guard(session)))
    prov = session["interview"]["provenance"]
    assert prov["call_direction"] == "confirmed"
    assert prov["personality.ai_disclosure"] == "confirmed"
    assert prov["identity.agent_name"] == "confirmed"


# ─── enforcement: lower rank never overwrites higher ─────────────────────────

def test_authored_copy_cannot_overwrite_user_wording(cop):
    session = _session_with_user_text(_USER_BRIEF)
    user_line = ("That's perfectly fine — most of our long-term partners started "
                 "while keeping their existing suppliers.")
    _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"closing_responses": {"en": user_line}}}},
        _guard(session)))
    assert session["interview"]["provenance"]["response_bank.closing_responses.en"] \
        == "confirmed"
    # the model now tries to "improve" it with copy of its own, alongside a
    # legitimate gap-fill (so the call partially succeeds and returns notes)
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"closing_responses": {
            "en": "Thanks so much for chatting with me today, it was a real "
                  "pleasure, do have a fantastic rest of your day!"}},
        "identity": {"agent_name": "Karan"}},
        "source": "user"}, _guard(session)))
    assert ok
    assert session["interview"]["draft"]["response_bank"]["closing_responses"]["en"] \
        == user_line                                   # the user's words stand
    assert session["interview"]["draft"]["identity"]["agent_name"] == "Karan"  # gap filled
    assert any("kept the wording you gave" in n for n in res["notes"])


def test_all_outranked_write_fails_loudly(cop):
    session = _session_with_user_text(_USER_BRIEF)
    line = "He never quotes prices or rate cards on the phone."
    _run(cop._execute_tool("update_draft", {"changes": {
        "guardrails": {"never_rules": [line]}}}, _guard(session)))
    with pytest.raises(Exception) as ei:
        _run(cop._execute_tool("update_draft", {"changes": {
            "guardrails": {"never_rules": [
                "Never share confidential internal pricing spreadsheets with "
                "anyone who happens to ask about them politely."]}}},
            _guard(session)))
    msg = str(ei.value)
    assert "Their input stands" in msg
    assert "do NOT retry the same content" in msg.lower() or "Do NOT retry" in msg
    assert session["interview"]["draft"]["guardrails"]["never_rules"] == [line]


def test_model_may_fill_gaps_freely(cop):
    """The whole point of the regime is that authorship is still allowed where
    the user gave nothing — only overwriting is refused."""
    session = _session_with_user_text("build me a fabric sales agent")
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "call_flow": {"stages": [
            {"id": "OPENING", "goal": "Greet the retailer warmly, confirm you are "
             "speaking with the owner and ask whether it is a good moment to talk "
             "before pitching anything at all."}]}}}, _guard(session)))
    assert ok
    assert session["interview"]["draft"]["call_flow"]["stages"]
    assert session["interview"]["provenance"]["call_flow.stages"] == "authored"


def test_user_can_always_change_their_own_mind(cop):
    """Input priority must never lock the USER out — a new user message is new
    user input and outranks the old value."""
    session = _session_with_user_text("closing line: Thank you for your time, "
                                      "have a wonderful day ahead!")
    _run(cop._execute_tool("update_draft", {"changes": {"response_bank": {
        "closing_responses": {"en": "Thank you for your time, have a wonderful "
                                    "day ahead!"}}}}, _guard(session)))
    # user speaks again with different wording
    session["messages"].append({"role": "user", "content":
        "change it to: Thanks so much, I will send the samples across today, "
        "stay in touch and take care!"})
    res, _s, ok, _m = _run(cop._execute_tool("update_draft", {"changes": {
        "response_bank": {"closing_responses": {
            "en": "Thanks so much, I will send the samples across today, stay "
                  "in touch and take care!"}}}}, _guard(session)))
    assert ok
    assert session["interview"]["draft"]["response_bank"]["closing_responses"]["en"] \
        .startswith("Thanks so much")


# ─── document content is user input too ──────────────────────────────────────

def test_document_content_outranks_authored_but_not_typed(cop):
    session = {"messages": [{"role": "user", "content": "here is the brief"}],
               "transcript": [], "meta": {}}
    iv = cop._interview_state(session)
    iv["input_texts"] = [_USER_BRIEF]
    # model authors a knowledge paragraph first
    _run(cop._execute_tool("update_draft", {"changes": {"knowledge_base": {
        "portfolio_prompt_text": "A company that sells assorted goods to a wide "
                                 "variety of customers across many regions."}}},
        _guard(session)))
    assert iv["provenance"]["knowledge_base.portfolio_prompt_text"] == "authored"
    # the document's own words replace it
    _ch, notes, _p, _c = cop._merge_extracted_into_draft(session, {
        "portfolio_prompt_text": "He never quotes prices or rate cards on the "
                                 "phone; one sample kit costs you nothing to compare."})
    assert iv["draft"]["knowledge_base"]["portfolio_prompt_text"].startswith(
        "He never quotes prices")
    assert iv["provenance"]["knowledge_base.portfolio_prompt_text"] == "derived"


def test_corpus_includes_typed_and_document_text(cop):
    session = {"messages": [{"role": "user", "content": "hello Kaapd"},
                            {"role": "assistant", "content": "assistant words only"}],
               "transcript": [], "meta": {}}
    cop._interview_state(session)["input_texts"] = ["polyester knitted fabrics"]
    corpus = cop._user_corpus(session)
    # words are \x00-tagged; phrases are plain
    assert "\x00kaapd" in corpus and "\x00polyester" in corpus
    assert "polyester knitted fabrics" in corpus            # phrase from the document
    assert "\x00assistant" not in corpus  # the model's own words are NOT input


# ─── platform defaults must not contradict the user's voice choice ───────────

def test_male_voice_fixes_feminine_platform_defaults(cop):
    """Kaapd audit: Karan spoke with shubh (male) yet carried pronouns 'she'
    and the feminine Hindi fallback. The VOICE is the user's deliberate
    choice; those are only defaults, so the defaults must yield."""
    merged = {
        "identity": {"agent_name": "Karan", "pronouns": "she"},
        "personality": {"pronouns": "she"},
        "languages": {"supported": ["hi", "en"], "default": "hi",
                      "tts_voices": {"hi": {"speaker": "shubh"},
                                     "en": {"speaker": "shubh"}}},
        "response_bank": {"opening_greeting": "नमस्ते जी!",
                          "timeout_fallbacks": {"hi": "मैं इस पर वापस आती हूँ।"}},
    }
    notes: list[str] = []
    cop._enforce_invariants(merged, notes)
    assert merged["identity"]["pronouns"] == "he"
    assert merged["personality"]["pronouns"] == "he"
    assert merged["response_bank"]["timeout_fallbacks"]["hi"] == "मैं इस पर वापस आता हूँ।"
    assert any("voice" in n for n in notes)


def test_female_voice_leaves_defaults_alone(cop):
    merged = {
        "identity": {"agent_name": "Ananya", "pronouns": "she"},
        "personality": {"pronouns": "she"},
        "languages": {"supported": ["hi"], "default": "hi",
                      "tts_voices": {"hi": {"speaker": "anushka"}}},
        "response_bank": {"opening_greeting": "नमस्ते जी!",
                          "timeout_fallbacks": {"hi": "मैं इस पर वापस आती हूँ।"}},
    }
    cop._enforce_invariants(merged, [])
    assert merged["identity"]["pronouns"] == "she"
    assert merged["response_bank"]["timeout_fallbacks"]["hi"] == "मैं इस पर वापस आती हूँ।"


def test_user_authored_lines_are_never_gender_rewritten(cop):
    """The gender fix only touches values that still ARE the platform default —
    a line the user wrote is never re-worded, whatever its grammar."""
    mine = "मैं आपको थोड़ी देर में वापस कॉल करती हूँ।"
    merged = {
        "identity": {"agent_name": "Karan", "pronouns": "he"},
        "languages": {"supported": ["hi"], "default": "hi",
                      "tts_voices": {"hi": {"speaker": "shubh"}}},
        "response_bank": {"opening_greeting": "नमस्ते!",
                          "timeout_fallbacks": {"hi": mine}},
    }
    cop._enforce_invariants(merged, [])
    assert merged["response_bank"]["timeout_fallbacks"]["hi"] == mine
