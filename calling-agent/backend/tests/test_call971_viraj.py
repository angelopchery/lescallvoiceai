"""Call 971 — Viraj's second test call (2026-09-26, RUNBOOK §61).

A four-minute Hindi survey call in which every question was eventually
answered and almost nothing the caller said reached the record intact:

* "हेलो, नमस्ते।" was heard THREE times before the agent said who it was —
  the early-hello repair replays the last line unless it is a bare "hello",
  and a greeting made of two greeting words is not a bare hello.
* "हाँ ठीक रहेगा" (consent to a two-minute chat) was stored as the caller's
  rating of the Mahotsav ('Okay') — the deterministic enum extractor read
  every vocabulary word anywhere in the call as a value.
* The rating stayed 'Okay' when he later said "it was good" three times, and
  the agent read the wrong value back to him.
* "बट मैं रहता तो दिल्ली में हूँ, लखनऊ कहाँ से आ गया?" — a question — was
  stored as relocate = 'No'.
* "नहीं, अभी तक नहीं आई" and "रोजगार की मदद चाहूंगा" were heard and never
  stored: a max_asks=1 slot left `missing` the moment its question was
  CHOSEN, and the extractor reads only `missing`.
* "हाँ हाँ हाँ बिल्कुल।" was rejected as a hallucination (repetition 0.75),
  the next 300 ms blip made it two empties, and the agent said "आपकी आवाज़
  स्पष्ट नहीं आई" to a man who had just said yes four times.
* A dead Whisper fallback (no credit) cost two 429s per rejected segment and
  delayed the transcript behind it by four seconds.
* The first stage (identity confirm) never ran: the caller's "जी नमस्ते"
  was an affirmation, so WARM_WELCOME advanced before it spoke.

Offline: no LLM, no network.
"""
from __future__ import annotations

import asyncio
import time
import types
from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter
from app.voice.pipeline import AudioSegment, VoicePipeline, _STT_FALLBACK_PHRASES
from app.voice.stt.base import STTResult
from app.voice.stt.router import validate_transcript_ex
from test_call_termination import drive, make_router
from test_connect_hello import hello_router
from test_stt_script_retry import AUDIO, FakeSTT, make_router as make_stt_router, run

FIXTURES = Path(__file__).parent / "fixtures"
FIXTURE = FIXTURES / "viraj_outbound_2026_09_26.yaml"


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


YES_NO = {
    "enum_values": ["Yes", "No"],
    "enum_synonyms": {
        "Yes": ["yes", "haan", "ji", "हाँ", "हां", "बिल्कुल", "मिल गई", "मिला"],
        "No": ["no", "nahi", "नहीं", "नहीं मिली", "ढूंढ रहा", "looking"],
    },
}
RATING = {
    "enum_values": ["Good", "Okay", "Not good"],
    "enum_synonyms": {
        "Good": ["good", "अच्छा", "बढ़िया", "गुड"],
        "Okay": ["okay", "ठीक", "ठीक-ठाक", "not bad", "नॉट बैड"],
        "Not good": ["bad", "बैड", "बेकार", "not good"],
    },
}
CITY = {
    "enum_values": ["Delhi", "Vapi", "Surat"],
    "enum_synonyms": {"Delhi": ["delhi", "दिल्ली"], "Vapi": ["vapi", "वापी"],
                      "Surat": ["surat", "सूरत"]},
}


def _survey_router(direction: str = "outbound", **over) -> ConversationRouter:
    d = {
        "agent_id": "t_971", "version": "1.0.0",
        "identity": {"agent_name": "Viraj", "company_name": "Rojgar Seva"},
        "languages": {"supported": ["hi", "en"], "default": "hi", "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "HELLO", "order": 0, "goal": "confirm identity", "role": "greeting",
             "collect": [], "advance_when": {"intent": "affirmation", "max_turns": 2}},
            {"id": "ASK", "order": 1, "goal": "ask", "role": "discovery",
             "collect": ["got_job", "company_contacted", "future_help"],
             "advance_when": {"all_collect_filled": True, "max_turns": 9}},
            {"id": "BYE", "order": 2, "goal": "bye", "role": "closing",
             "turns_until_advance": 99},
        ]},
        "response_bank": {"opening_greeting": "हेलो?",
                          "lang_fallbacks": {"hi": "माफ़ कीजिए, आवाज़ साफ़ नहीं आई — एक बार फिर बताइए?"}},
        "call_direction": direction,
        "slots": [
            {"name": "got_job", "type": "boolean", "priority": 1, "required": True,
             "label": "Found a job (Yes/No)", "max_asks": 2,
             "question": {"hi": "job मिल गई?"}, **YES_NO},
            {"name": "company_contacted", "type": "boolean", "priority": 2,
             "required": True, "label": "Company contacted (Yes/No)", "max_asks": 1,
             "question": {"hi": "किसी company ने contact किया?"}, **YES_NO},
            {"name": "experience", "type": "enum", "priority": 3, "required": True,
             "label": "Experience (Good / Okay / Not good)", "max_asks": 1,
             "question": {"hi": "experience कैसा रहा?"}, **RATING},
            {"name": "city", "type": "enum", "priority": 4, "required": True,
             "label": "Preferred city", "question": {"hi": "किस शहर में?"}, **CITY},
            {"name": "future_help", "type": "text", "priority": 5, "required": True,
             "label": "Help wanted", "max_asks": 1,
             "question": {"hi": "किस तरह की मदद चाहेंगे?"}},
            {"name": "issues", "type": "text", "priority": 6, "required": True,
             "label": "Issues faced", "max_asks": 1,
             "question": {"hi": "कोई दिक्कत आई?"}},
        ],
    }
    d.update(over)
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d), default_language="hi")
    return r


# ── 1. a greeting-only opening carries nothing worth replaying ──────────────

OUTBOUND = {"identity_confirm_line": {
    "hi": "नमस्ते! क्या मेरी बात {contact_name} जी से हो रही है?",
    "en": "Hello! Am I speaking with {contact_name}?"}}


def _viraj_like(opening: str) -> ConversationRouter:
    r = hello_router(opening, call_direction="outbound", outbound=OUTBOUND)
    r.set_call_context(direction="outbound", contact_name="Alwyn",
                       contact_phone="917042340227")
    r.state.language = "hi"
    return r


@pytest.mark.parametrize("opening", ["हेलो, नमस्ते।", "Hello, good afternoon.",
                                     "नमस्ते!", "Hello?"])
def test_greeting_only_opening_gets_the_identity_line_not_a_replay(opening):
    r = _viraj_like(opening)
    resp = drive(r, "hello")
    assert r._hello_replays == 0, "the opening was replayed"
    assert r._intro_delivered is True
    assert "Alwyn" in resp and "हो रही है" in resp


@pytest.mark.parametrize("first_words", ["जी नमस्ते।", "नमस्ते", "hello namaste",
                                         "haan hello"])
def test_a_greeting_only_first_reply_also_gets_the_identity_line(first_words):
    r = _viraj_like("हेलो, नमस्ते।")
    resp = drive(r, first_words)
    assert r._intro_delivered is True
    assert "Alwyn" in resp


def test_a_real_opening_sentence_is_still_replayed_on_a_bare_hello():
    opening = "नमस्ते! मैं Viraj बोल रहा हूँ, क्या मेरी बात Alwyn जी से हो रही है?"
    r = _viraj_like(opening)
    resp = drive(r, "hello")
    assert r._hello_replays == 1
    assert resp == opening


def test_greeting_only_is_strict_about_content():
    f = ConversationRouter._is_greeting_only_text
    assert f("हेलो, नमस्ते।") and f("Hello, good morning!") and f("जी नमस्ते")
    assert not f("नमस्ते, मैं Viraj बोल रहा हूँ")
    assert not f("hello, who is this?")
    assert not f("")


def test_the_onset_intro_fires_for_a_greeting_only_opening():
    r = _viraj_like("हेलो, नमस्ते।")
    assert "Alwyn" in r.connect_onset_line()


# ── 2. an everyday word is a value only as the answer to its own question ──

def test_consent_to_talk_is_not_a_rating():
    r = _survey_router()
    r.state_mgr.observe_user("हाँ ठीक रहेगा।")
    assert r.state_mgr.state["experience"] is None
    r.state_mgr.observe_user("अच्छा, तो बताइए")
    assert r.state_mgr.state["experience"] is None


def test_the_same_word_is_the_answer_when_the_question_was_asked():
    r = _survey_router()
    r.state_mgr.observe_user("hello")                 # turn 1
    r.state_mgr.note_asked("experience")              # the directive chose it
    r.state_mgr.observe_user("ठीक-ठाक था")            # turn 2 — the answer
    assert r.state_mgr.state["experience"] == "Okay"


def test_the_engines_own_reading_of_its_question_counts_too():
    r = _survey_router()
    r.state_mgr.observe_user("hello")
    r.state_mgr.observe_user("बहुत अच्छा लगा", asked="experience")
    assert r.state_mgr.state["experience"] == "Good"


def test_an_ask_noted_before_the_call_started_never_counts():
    r = _survey_router()
    r.state_mgr.note_asked("experience")              # a warm-up prompt build
    r.state_mgr.observe_user("हाँ ठीक रहेगा")          # the caller's first words
    assert r.state_mgr.state["experience"] is None


def test_a_distinctive_word_is_taken_wherever_it_is_volunteered():
    r = _survey_router()
    r.state_mgr.observe_user("मैं दिल्ली में रहता हूँ")
    assert r.state_mgr.state["city"] == "Delhi"


def test_the_longest_alias_wins_at_a_position():
    r = _survey_router()
    r.state_mgr.observe_user("hello")
    r.state_mgr.observe_user("इट वाज़ नॉट बैड", asked="experience")
    assert r.state_mgr.state["experience"] == "Okay"      # not "Not good"


def test_word_boundaries_are_script_aware():
    from app.voice.state_manager import _word_boundary_ok
    s = "जॉब इधर मिली नहीं है"
    i = s.index("मिल")
    assert not _word_boundary_ok(s, i, i + 3)          # "मिल" inside "मिली"
    assert _word_boundary_ok("मिल गई", 0, 3)


@pytest.mark.parametrize("said,expected", [
    ("नहीं, अभी तक नहीं आई है, पहला कॉल आ रहा है अभी मुझे।", "No"),
    ("हाँ, बिल्कुल बिल्कुल।", "Yes"),
    ("हाँ हाँ, एक company ने किया था", "Yes"),
])
def test_hindi_yes_no_fill_a_boolean_when_it_was_asked(said, expected):
    r = _survey_router()
    r.state_mgr.observe_user("hello")
    r.state_mgr.observe_user(said, asked="company_contacted")
    assert r.state_mgr.state["company_contacted"] == expected


def test_a_stray_hindi_yes_does_not_fill_an_unasked_boolean():
    r = _survey_router()
    r.state_mgr.observe_user("hello")
    r.state_mgr.note_asked("got_job")
    r.state_mgr.observe_user("हाँ, बिल्कुल")           # answers got_job only
    assert r.state_mgr.state["got_job"] == "Yes"
    assert r.state_mgr.state["company_contacted"] is None


def test_the_answer_through_the_vocabulary_beats_a_bare_yes():
    r = _survey_router()
    r.state_mgr.observe_user("hello")
    r.state_mgr.observe_user("हाँ… मतलब अभी ढूंढ रहा हूँ", asked="got_job")
    assert r.state_mgr.state["got_job"] == "No"


# ── 3. the ask budget settles on the caller's turn, not on the choice ──────

def test_the_last_permitted_ask_keeps_the_slot_listening():
    r = _survey_router()
    sm = r.state_mgr
    sm.observe_user("hello")                          # turn 1
    sm.note_asked("future_help")                      # max_asks=1 — last ask
    gaps = sm.detect_missing_slots()
    assert "future_help" not in gaps["missing"]       # never asked again
    assert "future_help" in gaps["awaiting"]          # but still listened for
    assert "future_help" not in gaps["skipped"]
    assert sm.collect_satisfied(["future_help"])      # and no stage is held
    sm.observe_user("रोजगार सेवा से रोजगार की मदद चाहूंगा ना।")   # turn 2
    assert "future_help" in sm.detect_missing_slots()["awaiting"]
    # …which is what lets the background extractor store the answer.
    sm.merge_state({"future_help": "job placement help"}, source="llm",
                   utterance="रोजगार की मदद", fill_only=True)
    assert sm.state["future_help"] == "job placement help"
    assert "future_help" not in sm.detect_missing_slots()["awaiting"]


def test_an_unanswered_last_ask_is_skipped_after_the_callers_turn():
    r = _survey_router()
    sm = r.state_mgr
    sm.observe_user("hello")
    sm.note_asked("issues")
    sm.observe_user("hmm")                            # the answer turn — nothing
    assert "issues" in sm.detect_missing_slots()["awaiting"]
    sm.observe_user("okay")                           # one more turn
    gaps = sm.detect_missing_slots()
    assert "issues" in gaps["skipped"] and "issues" not in gaps["awaiting"]
    assert "issues" in sm.to_dict()["skipped"]


def test_the_record_lists_a_still_awaiting_slot_as_skipped_at_call_end():
    r = _survey_router()
    sm = r.state_mgr
    sm.observe_user("hello")
    sm.note_asked("issues")
    assert "issues" in sm.to_dict()["skipped"]


def test_the_extractor_key_list_includes_the_awaiting_slot(monkeypatch):
    r = _survey_router()
    r._slot_extract_enabled = True
    r._api_key = "x"
    seen: dict = {}

    async def _fake_extract(text, missing):
        seen["missing"] = list(missing)

    monkeypatch.setattr(r, "_llm_extract_slots", _fake_extract)
    r.state_mgr.observe_user("hello")
    r.state_mgr.note_asked("future_help")
    r.state_mgr.observe_user("रोजगार सेवा से रोजगार की मदद चाहूंगा ना।")

    async def _go():
        r._spawn_slot_extractor("रोजगार सेवा से रोजगार की मदद चाहूंगा ना।")
        await asyncio.sleep(0)
        if r._slot_extract_task:
            await r._slot_extract_task
    asyncio.run(_go())
    assert "future_help" in seen["missing"]


def test_unnote_asked_restores_the_budget_this_turn_only():
    r = _survey_router()
    sm = r.state_mgr
    sm.observe_user("hello")
    sm.note_asked("issues")
    assert sm.asked_count["issues"] == 1
    sm.unnote_asked("issues")
    assert sm.asked_count["issues"] == 0
    assert "issues" in sm.detect_missing_slots()["missing"]
    sm.note_asked("issues")
    sm.observe_user("next turn")
    sm.unnote_asked("issues")                         # too late — no effect
    assert sm.asked_count["issues"] == 1


def test_the_budget_goes_back_when_the_agent_asked_something_else():
    r = _survey_router()
    sm = r.state_mgr
    sm.observe_user("hello")
    sm.note_asked("issues")                           # the directive's choice
    r._last_asked_slot = "experience"                 # what the line asked
    r._reconcile_ask_budget()
    assert sm.asked_count.get("issues", 0) == 0
    assert "issues" in sm.detect_missing_slots()["missing"]


def test_the_budget_stays_spent_when_the_agent_asked_the_suggested_question():
    r = _survey_router()
    sm = r.state_mgr
    sm.observe_user("hello")
    sm.note_asked("issues")
    r._last_asked_slot = "issues"
    r._reconcile_ask_budget()
    assert sm.asked_count["issues"] == 1


def test_qa_tracker_reports_the_field_its_last_line_asked():
    from app.voice.memory import QATracker
    qa = QATracker({"issues": ["कोई दिक्कत"], "experience": ["कैसा रहा"]})
    qa.observe_agent_turn("महोत्सव आपके लिए कैसा रहा?", 3)
    assert qa.last_asked_field == "experience"
    qa.observe_agent_turn("ठीक है, समझ गया।", 4)
    assert qa.last_asked_field is None


# ── 4. a question never answers a yes/no ────────────────────────────────────

def test_a_callers_question_never_writes_a_yes_no():
    r = _survey_router()
    out = r._drop_yes_no_from_question_turn(
        {"company_contacted": "No", "city": "Delhi", "future_help": "Lucknow? Delhi"},
        "बट मैं रहता तो दिल्ली में हूँ, लखनऊ कहाँ से आ गया?", "llm")
    assert "company_contacted" not in out and "city" not in out
    assert out["future_help"] == "Lucknow? Delhi"    # free text keeps the content


def test_a_yes_that_leads_the_sentence_survives_a_rhetorical_tail():
    r = _survey_router()
    data = {"company_contacted": "Yes"}
    assert r._drop_yes_no_from_question_turn(data, "हाँ, क्यों नहीं?", "llm") == data
    assert r._drop_yes_no_from_question_turn(data, "हाँ बिल्कुल।", "llm") == data


def test_the_tool_door_applies_the_same_guard(monkeypatch):
    from app.voice.llm_tools import ToolProposal
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    r = _survey_router()
    r._tools_mode = "on"
    r.memory.add_user("कहाँ किसके बाहर?")
    r._pending_tool_proposals = [ToolProposal(
        name="record_customer_details", args={"company_contacted": "No"})]
    r._execute_tool_proposals(had_text=True)
    assert r.state_mgr.state["company_contacted"] is None


# ── 5. an outbound first stage speaks before an affirmation can end it ─────

def _first_stage_router(direction: str) -> ConversationRouter:
    r = _survey_router(direction=direction)
    r.state.stage = "HELLO"
    r.state.last_intent = "affirmation"
    r.state.discovery_turns = 0
    return r


def test_outbound_first_stage_holds_on_the_callers_first_affirmation():
    r = _first_stage_router("outbound")
    r._maybe_advance_stage()
    assert r.state.stage == "HELLO"


def test_it_advances_once_the_stage_has_spoken_or_the_intro_was_delivered():
    r = _first_stage_router("outbound")
    r._intro_delivered = True
    r._maybe_advance_stage()
    assert r.state.stage == "ASK"
    r = _first_stage_router("outbound")
    r.state.discovery_turns = 1
    r._maybe_advance_stage()
    assert r.state.stage == "ASK"


def test_inbound_agents_keep_advancing_immediately():
    r = _first_stage_router("inbound")
    r._maybe_advance_stage()
    assert r.state.stage == "ASK"


# ── 6. STT: emphatic yes, dead fallback, Marathi misfire ────────────────────

def test_a_repeated_affirmation_is_collapsed_not_rejected():
    text, reason = validate_transcript_ex("हाँ हाँ हाँ बिल्कुल।")
    assert reason is None
    assert "बिल्कुल" in text and text.count("हाँ") == 1
    text, reason = validate_transcript_ex("yes yes yes yes correct")   # 4/5 = 0.8
    assert reason is None and text == "yes correct"


def test_pure_repetition_and_digit_only_company_are_still_rejected():
    assert validate_transcript_ex("okay okay okay okay") == ("", "repetition")
    assert validate_transcript_ex("yes yes yes 42")[0] == ""
    assert validate_transcript_ex("na na na na na na")[0] == ""


def test_a_quota_error_disables_the_fallback_for_the_cooldown():
    import httpx
    quota = Exception("Error code: 429 - {'error': {'message': 'You have no credits "
                      "remaining.', 'type': 'insufficient_quota', "
                      "'code': 'credit_balance_exhausted'}}")
    # Primary hangs (timeout) → fallback → quota error. Twice.
    router, primary, fallback = make_stt_router(
        [httpx.ReadTimeout("hung"), httpx.ReadTimeout("hung")],
        [quota, quota, quota, quota])
    router._hedge_delay = 0.0
    first = run(router.transcribe(AUDIO, language="hi", default_language="hi"))
    assert first.text == ""
    assert fallback.calls == ["hi"]                 # ONE attempt, no retry
    assert router.fallback_available() is False
    run(router.transcribe(AUDIO, language="hi", default_language="hi"))
    assert fallback.calls == ["hi"]                 # not even attempted
    router._fallback_disabled_until = time.monotonic() - 1
    assert router.fallback_available() is True


def test_an_ordinary_fallback_error_does_not_open_the_cooldown():
    import httpx
    router, primary, fallback = make_stt_router(
        [httpx.ReadTimeout("hung")], [Exception("boom"), Exception("boom")])
    router._hedge_delay = 0.0
    run(router.transcribe(AUDIO, language="hi", default_language="hi"))
    assert router.fallback_available() is True
    assert len(fallback.calls) == 2                  # attempt + retry, as before


def test_a_marathi_misdetection_gets_the_forced_language_retry():
    router, primary, fallback = make_stt_router([
        STTResult(text="हम्म, कोण उत्तर आहे हे?", confidence=1.0, detected_language="mr-IN"),
        STTResult(text="हाँ, कौन बोल रहा है?", confidence=1.0, detected_language="hi-IN"),
    ])
    result = run(router.transcribe(AUDIO, language="unknown", default_language="hi"))
    assert result.text == "हाँ, कौन बोल रहा है?"
    assert primary.calls == ["unknown", "hi"]
    assert fallback.calls == []
    assert router.script_recovered_count == 1


def test_a_marathi_reject_without_a_language_hint_is_still_dropped():
    router, primary, fallback = make_stt_router([
        STTResult(text="हम्म, कोण उत्तर आहे हे?", confidence=1.0, detected_language="mr-IN"),
    ])
    result = run(router.transcribe(AUDIO, language="unknown", default_language=None))
    assert result.text == ""
    assert primary.calls == ["unknown"]


# ── 7. the pipeline's "couldn't hear you" line ─────────────────────────────

def test_the_fallback_phrase_is_the_agents_own_when_it_wrote_one():
    r = _survey_router()
    ns = types.SimpleNamespace(conversation=r)
    own = VoicePipeline._stt_fallback_phrase(ns, "hi")
    assert own == "माफ़ कीजिए, आवाज़ साफ़ नहीं आई — एक बार फिर बताइए?"
    assert VoicePipeline._stt_fallback_phrase(ns, "en") == _STT_FALLBACK_PHRASES["en"]
    assert VoicePipeline._stt_fallback_phrase(
        types.SimpleNamespace(conversation=None), "hi") == _STT_FALLBACK_PHRASES["hi"]


def test_the_fallback_gate_reads_real_speech_not_the_buffered_span():
    blip = AudioSegment(audio=b"", duration_ms=860.0, timestamp=0.0, speech_ms=300.0)
    assert VoicePipeline._segment_speech_ms(blip) == 300.0
    unmeasured = AudioSegment(audio=b"", duration_ms=860.0, timestamp=0.0)
    assert VoicePipeline._segment_speech_ms(unmeasured) == 860.0


# ── 8. the agent as rebuilt (frozen snapshot, never the live YAML) ─────────

def _agent() -> AgentDefinition:
    return AgentDefinition.from_yaml(FIXTURE)


def test_the_relocation_question_names_no_city():
    a = _agent()
    slot = next(s for s in a.slots if s.name == "relocate_outside_lucknow")
    for lang in ("hi", "en"):
        q = slot.question[lang].lower()
        assert "लखनऊ" not in q and "lucknow" not in q
    goal = next(s for s in a.call_flow.stages if s.id == "NOT_EMPLOYED").goal
    assert "another city" in goal and "never assume" in goal


def test_the_feedback_question_is_open_and_the_register_quotes_no_phrase():
    a = _agent()
    slot = next(s for s in a.slots if s.name == "mahotsav_experience")
    assert "—" not in slot.question["hi"] and "या" not in slot.question["hi"]
    assert "note कर लिया" not in a.languages.register_notes["hi"]
    goal = next(s for s in a.call_flow.stages if s.id == "FEEDBACK").goal
    assert "never a menu" in goal and "face value" in goal


def test_no_close_confirm_and_still_searching_is_optional():
    a = _agent()
    assert a.call_flow.termination.get("confirm_before_end") is False
    still = next(s for s in a.slots if s.name == "still_searching")
    assert still.required is False
    not_employed = next(s for s in a.call_flow.stages if s.id == "NOT_EMPLOYED")
    assert "still_searching" not in not_employed.collect


def test_the_rebuilt_agent_never_parrots_its_greeting():
    r = ConversationRouter(agent_def=_agent(), default_language="hi")
    r.set_call_context(direction="outbound", contact_name="Alwyn",
                       contact_phone="917042340227")
    r.last_agent_response = "हेलो, नमस्ते।"
    r._opening_ts = time.monotonic()
    resp = drive(r, "hello")
    assert r._hello_replays == 0 and "Alwyn" in resp
