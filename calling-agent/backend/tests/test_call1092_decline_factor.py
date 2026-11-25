"""Call 1092 (2026-10-13, Blue Stone test, RUNBOOK §81) — three engine
capabilities, all opt-in:

  * `stages[].script.entry_line` — a stage a branch enters speaks its line
    verbatim with no model turn. "अभी तो मैं नहीं आ सकती" jumped to the
    close stage and the model said only the pre-close line; the owner's
    "okay, I'll share the details on WhatsApp" was dropped;
  * a slot's `reask_question` is what the ask_verbatim floor speaks on the
    second ask, and a reply carrying either line asked it;
  * `languages.stt_enum_recheck` — a short answer to an enum question that
    names no value under the STT language lock is re-read once without it
    ("Amenities" came back "हमें देखिए" and "नि टी" under hi-IN).

Offline: no LLM calls (stub_llm).
"""
from __future__ import annotations

import asyncio
import types

import numpy as np
import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter
from app.voice.pipeline import Transcript, VoicePipeline


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


L_PRECLOSE = ("ठीक है जी, धन्यवाद। जाने से पहले क्या मैं आपकी और किसी चीज़ में "
              "मदद कर सकता हूँ?")
L_DECLINED = ("ठीक है जी, कोई बात नहीं — मैं details WhatsApp पर share कर देता "
              "हूँ। जाने से पहले क्या मैं आपकी और किसी चीज़ में मदद कर सकता हूँ?")
L_BYE = "Thank you, आपका दिन शुभ हो।"
L_INVITE = ("इसलिए मेरा suggestion है कि आप एक 20 से 30 minute site visit कर "
            "लीजिए। Saturday evening या Sunday morning में से क्या convenient "
            "रहेगा?")
Q_FACTOR = ("आपके लिए property select करते वक़्त सबसे important factor क्या है "
            "— location, size, amenities, construction quality या investment "
            "value?")
Q_FACTOR_REASK = ("माफ़ कीजिए, ठीक से सुनाई नहीं दिया — location, size, "
                  "amenities, construction quality या investment value?")
FACTOR_SYN = {"Location": ["location", "लोकेशन"],
              "Amenities": ["amenities", "अमेनिटीज़", "मैनिटीज"]}


def _slot(name, prio, enum, syn, q, reask=None, max_asks=2):
    return {"name": name, "priority": prio, "required": True, "label": name,
            "question": {"hi": q}, "type": "enum",
            "enum_values": enum, "enum_synonyms": syn, "extract_patterns": [],
            "depends_on": [], "merge_policy": "free", "confirm_readback": "",
            "max_asks": max_asks, "reask_question": reask or {},
            "summary_known": "", "summary_unknown": "", "ask_gated": True}


def make_router(stages, slots, stage, languages=None):
    d = {
        "agent_id": "t_1092", "version": "1.0.0",
        "identity": {"agent_name": "Prashant",
                     "company_name": "Pramukh Blue Stone"},
        "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                      "evidence_switch": False, "auto_switch": False,
                      **(languages or {})},
        "call_flow": {"stages": stages,
                      "termination": {"confirm_before_end": False,
                                      "pre_close_question": {"hi": L_PRECLOSE}}},
        "slots": list(slots),
        "response_bank": {"opening_greeting": "hi",
                          "closing_responses": {"hi": L_BYE}},
    }
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.language = "hi"
    r.state.turn_count = 9
    r.state.stage = stage
    return r


def drive(r, text):
    async def _one():
        return [res async for res in r.process_stream(text)]
    return asyncio.run(_one())


def said(results) -> str:
    return " ".join(getattr(x, "text", "") or "" for x in results).strip()


# ── a declined visit: the WhatsApp line, then the close ────────────────────

def decline_router(entry_line=True):
    declined = [{"when_slot": {"name": "site_visit_interest", "equals": v},
                 "goto": "DECLINED_CLOSE", "max_takes": 1, "actions": []}
                for v in ("Maybe later", "No")]
    script = {"after_answer": {"hi": L_PRECLOSE},
              "unless": [L_BYE, L_DECLINED]}
    if entry_line:
        script["entry_line"] = {"hi": L_DECLINED}
    stages = [
        {"id": "INVITE", "order": 0, "role": "scheduling", "goal": "invite",
         "collect": [], "advance_when": {"max_turns": 4},
         "turns_until_advance": 4, "branches": declined},
        {"id": "CONFIRM_AND_CLOSE", "order": 1, "role": "confirmation",
         "goal": "close", "collect": [], "advance_when": {},
         "turns_until_advance": 99},
        {"id": "DECLINED_CLOSE", "order": 2, "role": "confirmation",
         "goal": "declined", "collect": [], "advance_when": {},
         "turns_until_advance": 99, "script": script},
    ]
    visit = _slot("site_visit_interest", 1, ["Yes", "Maybe later", "No"], {},
                  "visit?")
    visit["required"] = False
    r = make_router(stages, [visit], "INVITE")
    r.last_agent_response = L_INVITE
    return r


def test_a_declined_visit_speaks_the_whatsapp_line_with_no_model(stub_llm):
    r = decline_router()
    out = said(drive(r, "अभी तो मैं नहीं आ सकती।"))
    assert r.state.stage == "DECLINED_CLOSE"
    assert out == L_DECLINED
    assert stub_llm not in out
    assert r.last_agent_response == L_DECLINED
    # Its pre-close half is the close permission: "नहीं" ends the call.
    drive(r, "नहीं।")
    assert r.hangup_pending is True


def test_a_question_with_the_decline_goes_to_the_model(stub_llm):
    r = decline_router()
    out = said(drive(r, "अभी नहीं आ सकती, price क्या है?"))
    assert r.state.stage == "DECLINED_CLOSE"
    assert out != L_DECLINED


def test_without_an_entry_line_the_model_answers_as_before(stub_llm):
    r = decline_router(entry_line=False)
    out = said(drive(r, "अभी तो मैं नहीं आ सकती।"))
    assert r.state.stage == "DECLINED_CLOSE"
    assert stub_llm in out


# ── the second ask admits it did not hear ─────────────────────────────────

def factor_router(languages=None, reask=True):
    stages = [{"id": "REQ", "order": 0, "role": "discovery", "goal": "g",
               "collect": ["decision_factor"],
               "advance_when": {"all_collect_filled": True, "max_turns": 9},
               "turns_until_advance": 9, "script": {"ask_verbatim": True}},
              {"id": "PITCH", "order": 1, "role": "recommend", "goal": "p",
               "collect": [], "advance_when": {"max_turns": 9},
               "turns_until_advance": 9}]
    slot = _slot("decision_factor", 1,
                 ["Location", "Size", "Amenities", "Construction quality",
                  "Investment value"], FACTOR_SYN, Q_FACTOR,
                 reask={"hi": Q_FACTOR_REASK} if reask else None, max_asks=3)
    return make_router(stages, [slot], "REQ", languages=languages)


def test_the_second_ask_speaks_the_reask_line():
    r = factor_router()
    sm = r.state_mgr
    sm.note_asked("decision_factor")
    assert r._script_followup("ठीक है।") == Q_FACTOR
    sm.turn_index += 1
    sm.note_asked("decision_factor")
    assert r._script_followup("ठीक है।") == Q_FACTOR_REASK
    # A reply that already carries either line is not followed by the other.
    assert r._script_followup(Q_FACTOR_REASK) == ""
    assert r._script_followup("जी, " + Q_FACTOR) == ""


def test_without_a_reask_line_the_question_repeats_as_before():
    r = factor_router(reask=False)
    sm = r.state_mgr
    sm.note_asked("decision_factor")
    sm.turn_index += 1
    sm.note_asked("decision_factor")
    assert r._script_followup("ठीक है।") == Q_FACTOR


# ── the enum-answer re-check ──────────────────────────────────────────────

RECHECK = {"stt_forced_language": "hi-IN", "stt_enum_recheck": True}


def asked_factor(languages):
    r = factor_router(languages=languages)
    r.state_mgr.note_asked("decision_factor")
    r.last_agent_response = "जी। " + Q_FACTOR
    return r


def test_recheck_targets_a_garbled_answer_to_the_enum_question():
    r = asked_factor(RECHECK)
    assert r.stt_recheck_slot("हमें देखिए।") == "decision_factor"
    assert r.stt_recheck_slot("नि टी") == "decision_factor"


@pytest.mark.parametrize("text", [
    "Amenities",                 # already a value
    "अमेनिटीज़",                   # the Devanagari spelling
    "कितने floors हैं?",          # a question
    "मुझे पहले ये बताइए कि project कब तक ready होगा",   # long, not an answer
])
def test_recheck_leaves_values_questions_and_sentences_alone(text):
    assert asked_factor(RECHECK).stt_recheck_slot(text) == ""


@pytest.mark.parametrize("languages", [
    {"stt_forced_language": "hi-IN"},                 # not opted in
    {"stt_enum_recheck": True},                       # STT not locked
])
def test_recheck_is_opt_in_and_only_under_a_language_lock(languages):
    assert asked_factor(languages).stt_recheck_slot("हमें देखिए।") == ""


def test_recheck_needs_the_question_to_have_just_been_asked():
    r = asked_factor(RECHECK)
    r.last_agent_response = "Pramukh Blue Stone New Chala में है।"
    assert r.stt_recheck_slot("हमें देखिए।") == ""
    r = asked_factor(RECHECK)
    r.state_mgr.set_externally("decision_factor", "Location")
    assert r.stt_recheck_slot("हमें देखिए।") == ""


class _STT:
    def __init__(self, text):
        self.text, self.calls = text, []

    async def transcribe(self, audio, sample_rate, language,
                         default_language=None, **kw):
        self.calls.append(language)
        return types.SimpleNamespace(text=self.text, confidence=None,
                                     source="primary")


def _pipe(conv, reread):
    ns = types.SimpleNamespace(
        stt=_STT(reread), _stt_lang_pin="", _stt_accepts_hint=False,
        conversation=conv, _last_tts_end=0.0,
        sink=types.SimpleNamespace(playback_busy_until=0.0))
    for name in ("_retx_remember", "_retx_pieces", "_enum_recheck",
                 "_retx_close_window"):
        setattr(ns, name, types.MethodType(getattr(VoicePipeline, name), ns))
    return ns


def _heard(p, text, start=200.0, end=201.2):
    pcm = (np.ones(int(16000 * (end - start)), dtype=np.int16) * 500).tobytes()
    p._retx_remember(types.SimpleNamespace(
        audio=pcm, duration_ms=(end - start) * 1000, timestamp=end + 0.2,
        speech_start_ts=start, speech_end_ts=end, shadow=False), text)
    return Transcript(text=text, duration_ms=(end - start) * 1000,
                      timestamp=end + 0.3, speech_start_ts=start,
                      speech_end_ts=end)


def test_the_reread_replaces_the_text_only_when_it_names_a_value():
    p = _pipe(asked_factor(RECHECK), "Amenities")
    out = asyncio.run(p._enum_recheck(_heard(p, "हमें देखिए।")))
    assert out.text == "Amenities"
    assert p.stt.calls == ["unknown"]          # no language lock

    p = _pipe(asked_factor(RECHECK), "Hum dekhiye")
    tr = _heard(p, "हमें देखिए।")
    assert asyncio.run(p._enum_recheck(tr)) is tr


def test_no_reread_when_the_transcript_already_names_a_value():
    p = _pipe(asked_factor(RECHECK), "Amenities")
    tr = _heard(p, "लोकेशन")
    assert asyncio.run(p._enum_recheck(tr)) is tr
    assert p.stt.calls == []
