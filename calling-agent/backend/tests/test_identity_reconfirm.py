"""Identity confirmation on an outbound call (RUNBOOK §73, 2026-10-12).

"No" / "wrong number" to "am I speaking with X?" is re-confirmed ONCE
("is this the wrong number, or the right one?"). The answer to that
decides the call:

  * a yes, an acknowledgement, or a repeated no → the apology line, and
    the call ends (hangup armed) — the owner's rule;
  * "it's me after all" / "the number is right" → the authored good-time
    line, exactly as a plain yes would have;
  * "he's not here" / "it's my husband's number" → the wrong-person line,
    and from then on the model may only arrange when the contact is
    reachable — no introduction, no pitch (call 966 pitched a stranger).

Offline: no LLM calls, no live agent YAML.
"""
from __future__ import annotations

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice import conversation as conv
from app.voice.conversation import ConversationRouter


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


IDENTITY = {"hi": "नमस्ते! क्या मेरी बात {contact_name} जी से हो रही है?",
            "en": "Hello! Am I speaking with {contact_name}?"}
GOOD_TIME = {"hi": "शुक्रिया! मैं आशा बोल रही हूँ TestCo से — अभी दो मिनट बात "
                   "करना ठीक रहेगा?",
             "en": "Thanks! It's Asha from TestCo — is now a good time?"}
WRONG_PERSON = {"hi": "अरे, माफ़ कीजिए — क्या {contact_name} जी अभी हैं, या उनसे "
                      "बात करने का कोई और समय ठीक रहेगा?",
                "en": "Oh, sorry — is {contact_name} around, or is there a "
                      "better time to reach them?"}


def make_router(lang="hi", reconfirm=None, close=None, contact="Vishal"):
    ob = {"identity_confirm_line": IDENTITY,
          "good_time_check_line": GOOD_TIME,
          "wrong_person_response": WRONG_PERSON,
          "wrong_number_patterns": ["wrong number", "galat number", "गलत नंबर"]}
    if reconfirm is not None:
        ob["wrong_number_reconfirm_line"] = reconfirm
    if close is not None:
        ob["wrong_number_close"] = close
    d = {
        "agent_id": "t_ident", "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo",
                     "pronouns": "she"},
        "languages": {"supported": ["hi", "en"], "default": lang,
                      "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "WARM_WELCOME", "order": 0, "goal": "confirm identity",
             "role": "greeting", "turns_until_advance": 2,
             "advance_when": {"max_turns": 2}},
            {"id": "INTRO", "order": 1, "goal": "introduce the offer",
             "role": "pitch", "turns_until_advance": 2},
            {"id": "DISCOVERY", "order": 2, "goal": "discover",
             "role": "discovery", "turns_until_advance": 99}]},
        "response_bank": {"opening_greeting": "hi"},
        "call_direction": "outbound",
        "outbound": ob,
    }
    agent = AgentDefinition.from_dict(d)
    r = ConversationRouter(agent_def=agent, default_language=lang)
    r.call_direction = "outbound"
    r.outbound_contact_name = contact
    r.memory.call_state.customer_name = contact
    r.state.language = lang
    # The identity beat has just been spoken.
    r.last_agent_response = IDENTITY[lang].replace("{contact_name}", contact)
    r._intro_delivered = True
    return r


def drive(r, text):
    async def _one():
        return [res async for res in r.process_stream(text)]
    return asyncio.run(_one())


def said(results) -> str:
    return " ".join(getattr(x, "text", "") or "" for x in results).strip()


# ── the first signal is re-confirmed, never closed on ───────────────────────

def test_a_bare_no_to_the_identity_question_is_reconfirmed():
    r = make_router()
    out = said(drive(r, "नहीं।"))
    assert "confirmation के लिए" in out and "ग़लत नंबर" in out
    assert r._wrong_number_pending == 1
    assert not r.state.is_closing and not r.hangup_pending
    assert r.state_mgr.extra_flags.get("wrong_number_suspected") is True


def test_a_wrong_number_phrase_is_reconfirmed_first():
    r = make_router()
    out = said(drive(r, "sorry wrong number lag gaya"))
    assert "confirmation के लिए" in out
    assert not r.state.is_closing
    assert r.call_outcome != "wrong_number"


def test_english_agents_get_the_english_question_and_apology():
    r = make_router(lang="en")
    assert "Just to confirm once more" in said(drive(r, "no"))
    out = said(drive(r, "yes"))
    assert "My apologies" in out
    assert r.state.is_closing and r.hangup_pending


def test_authored_lines_win_over_the_built_ins():
    r = make_router(reconfirm={"hi": "ज़रा फिर से बताइए — ग़लत नंबर है क्या?"},
                    close={"hi": "माफ़ी, नमस्ते — आपका दिन अच्छा रहे।"})
    assert "ज़रा फिर से बताइए" in said(drive(r, "नहीं"))
    assert "आपका दिन अच्छा रहे" in said(drive(r, "हाँ"))


# ── what the answer to the re-confirmation does ─────────────────────────────

@pytest.mark.parametrize("answer", [
    "हाँ", "हाँ जी", "जी", "ठीक है", "सही", "हाँ सही है", "बिल्कुल", "ok",
    "yes", "नहीं", "नहीं गलत नंबर है", "galat number hai", "जी रॉन्ग नंबर।",
    "मैं वो नहीं हूँ",
])
def test_a_yes_an_acknowledgement_or_a_repeated_no_closes_with_the_apology(answer):
    r = make_router()
    drive(r, "नहीं")
    out = said(drive(r, answer))
    assert "तकलीफ़ के लिए माफ़" in out, answer
    assert r.state.is_closing and r.hangup_pending
    assert r.call_outcome == "wrong_number"
    assert r.state_mgr.extra_flags.get("wrong_number") is True
    assert r._wrong_number_pending == 0


@pytest.mark.parametrize("answer", [
    "नहीं नहीं, मैं ही बोल रहा हूँ", "मैं ही हूँ", "सही नंबर है", "नंबर सही है",
    "no no it's me", "yes speaking", "haan main hi bol raha hoon",
    "हाँ Vishal बोल रहा हूँ", "arre Vishal hi hoon",
])
def test_its_me_after_all_resumes_with_the_good_time_line(answer):
    r = make_router()
    drive(r, "नहीं")
    out = said(drive(r, answer))
    assert "अभी दो मिनट" in out, answer          # the authored second beat
    assert not r.state.is_closing and not r.hangup_pending
    assert r._wrong_number_pending == 0
    assert r._good_time_spoken is True
    assert "wrong_number_suspected" not in r.state_mgr.extra_flags


@pytest.mark.parametrize("answer", [
    "वो अभी नहीं हैं", "वो बाहर गए हैं", "ये मेरे पति का नंबर है",
    "he's not here right now", "it's my brother's number", "बाद में कॉल करना",
])
def test_right_number_other_person_gets_the_wrong_person_line_and_no_pitch(answer):
    r = make_router()
    drive(r, "नहीं")
    out = said(drive(r, answer))
    # outbound.wrong_person_response, in whichever language the caller's
    # answer carried the (bilingual) test agent into
    assert "Vishal" in out and ("अभी हैं" in out or "around" in out), answer
    assert not r.state.is_closing
    assert r._identity_other is True
    assert r.state_mgr.extra_flags.get("wrong_person") is True
    # From here the model is held to arranging a callback — on every turn.
    prompt = r._build_system_prompt()
    assert "━━ IDENTITY NOT CONFIRMED ━━" in prompt
    assert "NOT Vishal" in prompt
    drive(r, "हाँ बताइए क्या बात है")
    assert "━━ IDENTITY NOT CONFIRMED ━━" in r._build_system_prompt()
    assert r._wrong_number_pending == 0


def test_an_unclear_answer_is_asked_once_more_then_the_call_is_closed():
    r = make_router()
    drive(r, "नहीं")
    out = said(drive(r, "हम्म क्या मतलब"))
    assert "confirmation के लिए" in out
    assert r._wrong_number_pending == 2 and not r.state.is_closing
    out = said(drive(r, "अरे कुछ समझ नहीं आया"))
    assert "तकलीफ़ के लिए माफ़" in out
    assert r.state.is_closing and r.call_outcome == "wrong_number"


def test_who_is_this_while_pending_gets_the_identity_and_the_question_again():
    r = make_router()
    drive(r, "नहीं")
    out = said(drive(r, "कौन बोल रहा है?"))
    assert "TestCo" in out
    assert "confirmation के लिए" in out
    assert r._wrong_number_pending == 1 and not r.state.is_closing


# ── nothing fires outside the identity beat ─────────────────────────────────

def test_a_no_to_any_other_question_is_not_a_wrong_number():
    r = make_router()
    r.last_agent_response = "आप कहाँ से बात कर रहे हैं — Vapi में ही हैं?"
    drive(r, "नहीं")
    assert r._wrong_number_pending == 0
    assert not r.state.is_closing


def test_a_wrong_number_phrase_mid_call_is_still_reconfirmed():
    r = make_router()
    r.last_agent_response = "आप कहाँ से बात कर रहे हैं?"
    r.state.stage = "DISCOVERY"
    assert "confirmation के लिए" in said(drive(r, "अरे ये तो गलत नंबर है"))
    assert r._wrong_number_pending == 1


def test_feature_switch_restores_the_immediate_close(monkeypatch):
    monkeypatch.setattr(conv, "WRONG_NUMBER_RECONFIRM", False)
    r = make_router()
    out = said(drive(r, "you have the wrong number"))
    assert r.state.is_closing and r.hangup_pending
    assert "तकलीफ़ के लिए माफ़" in out or "My apologies" in out
    r2 = make_router()
    drive(r2, "नहीं")                          # a bare no goes to the model
    assert r2._wrong_number_pending == 0 and not r2.state.is_closing


def test_rollback_of_the_reconfirm_turn_forgets_the_pending_ask():
    """A continuation merge re-runs the turn with the caller's full words;
    the provisional ask must not survive as pending state."""
    r = make_router()
    r._continuation_enabled = True
    r.begin_turn_transaction("नहीं")         # what the pipeline does per turn
    drive(r, "नहीं")
    assert r._wrong_number_pending == 1 and r.has_pending_turn()
    r.rollback_pending_turn()
    assert r._wrong_number_pending == 0


# ── the vocabulary ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "नहीं", "no", "नहीं भाई", "जी नहीं", "नहीं नहीं", "no, sorry.", "ना।",
    "मैं वो नहीं हूँ", "galat number", "nahi ji", "no sir", "नो नो",
])
def test_identity_no_vocabulary_accepts(text):
    assert conv._is_identity_no(text)


@pytest.mark.parametrize("text", [
    "कौन?", "नहीं सुनाई दिया", "हाँ जी", "नहीं मैं बिज़ी हूँ",
    "no, i want properties in vapi", "हाँ नहीं", "नहीं, कौन बोल रहा है",
    "no inquiries", "", "नहीं नहीं 4 bhk नहीं 3 bhk",
])
def test_identity_no_vocabulary_rejects(text):
    assert not conv._is_identity_no(text)


@pytest.mark.parametrize("text", [
    "wrong number", "ये गलत नंबर है", "galat number lag gaya", "जी रॉन्ग नंबर",
    "is naam ka koi nahi", "no one by that name here", "ખોટો નંબર",
    "you've got the wrong number",
])
def test_wrong_number_phrases(text):
    assert conv._WRONG_NUMBER_RE.search(text.lower()), text


# ── "don't call me again" ends the call on that turn (Aristo, 2026-10-11) ───

@pytest.mark.parametrize("text", [
    "नहीं, dont call me again",
    "मैं अरिशा नहीं हूँ, मुझे दोबारा कॉल मत करना",
    "ये गलत नंबर है, दुबारा फोन मत करना",
    "आगे से कॉल मत करना मुझे",
    "फिर से फोन मत करना",
])
def test_dont_call_again_closes_at_once_and_is_never_reconfirmed(text):
    r = make_router()
    out = said(drive(r, text))
    assert "confirmation के लिए" not in out
    assert r.hangup_pending and r.state.is_closing
    assert r._wrong_number_pending == 0
    assert r.state_mgr.extra_flags.get("dnd_requested") is True
    assert r.call_outcome == "dnd"
    assert "माफ़" in out


def test_dont_call_again_during_the_reconfirmation_closes_too():
    r = make_router()
    drive(r, "नहीं")
    assert r._wrong_number_pending == 1
    out = said(drive(r, "मैंने कहा ना, दोबारा कॉल मत करो"))
    assert r.hangup_pending and r._wrong_number_pending == 0
    assert "confirmation के लिए" not in out


@pytest.mark.parametrize("text", [
    "अभी कॉल मत करना, शाम को करना",
    "आज फोन मत करो",
    "call me tomorrow",
    "नहीं, अभी नहीं देखना",
])
def test_scheduling_talk_is_not_a_do_not_call(text):
    assert conv._detect_dnd_request(text) is False
