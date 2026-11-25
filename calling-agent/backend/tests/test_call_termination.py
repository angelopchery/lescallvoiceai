"""Call-termination hardening (2026-07-29).

Live calls were only ending when the caller said a literal "bye" — the
platform had no LLM end-call signal, no close on exhausted dead-air repair,
and nothing Copilot could configure. This suite covers the new
call_flow.termination policy end-to-end:

- wider deterministic farewell vocabulary + per-agent farewell_patterns
- "thanks" accepted as a close-permission confirm
- the [END_CALL] LLM signal: stripped before TTS, arms hangup_pending
- silence hangup + max-duration hard close (router side and the pipeline
  watchdog's pure decision functions)
- prompt rendering of END-CALL RULES
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter
from app.voice import prompt_blocks as pb


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def make_router(termination: dict | None = None, **over) -> ConversationRouter:
    cf: dict = {
        "stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
        ],
    }
    if termination is not None:
        cf["termination"] = termination
    d = {
        "agent_id": "t_term",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en", "tts_voices": {}},
        "call_flow": cf,
        "response_bank": {"opening_greeting": "hi"},
    }
    d.update(over)
    agent = AgentDefinition.from_dict(d)
    return ConversationRouter(agent_def=agent, default_language="en")


def drive(router: ConversationRouter, text: str) -> str:
    async def _one():
        out = []
        async for res in router.process_stream(text):
            out.append(res.text)
        return " ".join(t for t in out if t)

    return asyncio.run(_one())


# ── schema plumbing ─────────────────────────────────────────────────────────

def test_termination_survives_schema_roundtrip():
    r = make_router(termination={"llm_end_call": False,
                                 "end_conditions": ["once booked, end"],
                                 "max_call_minutes": 10})
    term = r._def.call_flow.termination
    assert term["llm_end_call"] is False
    assert term["end_conditions"] == ["once booked, end"]
    assert term["max_call_minutes"] == 10


def test_termination_defaults_to_empty_dict():
    r = make_router()
    assert r._def.call_flow.termination == {}
    assert r._termination_cfg() == {}


# ── wider farewell vocabulary ───────────────────────────────────────────────

def test_builtin_hangup_phrase_closes():
    """'main phone rakhta hoon' is an unambiguous 'I'm hanging up now'."""
    r = make_router()
    drive(r, "acha theek hai main phone rakhta hoon")
    drive(r, "haan")                        # close-confirm gate (2026-07-31)
    assert r.state.is_closing is True
    assert r.hangup_pending is True


def test_configured_farewell_pattern_closes():
    r = make_router(termination={"farewell_patterns": ["chalo rakhte hain"]})
    drive(r, "ok ji chalo rakhte hain")
    drive(r, "haan")
    assert r.hangup_pending is True


def test_unconfigured_phrase_does_not_close():
    r = make_router()
    drive(r, "ok ji chalo aage batao")
    assert r.hangup_pending is False


def test_thanks_confirms_close_permission():
    """'thank you' in reply to 'anything else?' means the caller is done."""
    r = make_router()
    r.last_agent_response = "Is there anything else I can help you with?"
    drive(r, "thank you so much")
    drive(r, "yes")
    assert r.hangup_pending is True


def test_thanks_mid_call_does_not_close():
    """Gratitude outside the permission-to-close context stays a thanks."""
    r = make_router()
    r.last_agent_response = "The site visit is booked for Sunday at 11 AM."
    resp = drive(r, "thank you so much")
    assert r.hangup_pending is False
    assert resp  # answered (gratitude shortcut), not closed


# ── LLM end-call signal ([END_CALL]) ────────────────────────────────────────

def test_marker_stripped_and_arms_signal():
    r = make_router()
    out = r._strip_end_call_marker("Thank you for your time, goodbye! [END_CALL]")
    assert out == "Thank you for your time, goodbye!"
    assert r._llm_end_call_seen is True


def test_marker_variants_stripped():
    r = make_router()
    for raw in ("Bye! [end_call]", "Bye! [ END CALL ]", "Bye! END_CALL"):
        r._llm_end_call_seen = False
        out = r._strip_end_call_marker(raw)
        assert "END" not in out.upper() and "[" not in out, raw
        assert r._llm_end_call_seen is True, raw


def test_plain_prose_end_call_not_eaten():
    """Natural 'end call' wording must survive — only the token is a signal."""
    r = make_router()
    out = r._strip_end_call_marker("You can end call recording any time.")
    assert out == "You can end call recording any time."
    assert r._llm_end_call_seen is False


def test_marker_stripped_but_ignored_when_disabled():
    r = make_router(termination={"llm_end_call": False})
    out = r._strip_end_call_marker("Goodbye! [END_CALL]")
    assert "[" not in out
    assert r._llm_end_call_seen is False


def test_llm_end_call_signal_closes_after_stream():
    """The consume loop arms the platform hangup once the stream that
    carried [END_CALL] finishes — and the token never reaches TTS.
    confirm_before_end off here: the gated path has its own tests below."""
    r = make_router(termination={"confirm_before_end": False})
    text = r._strip_end_call_marker("It was lovely speaking with you. Goodbye! [END_CALL]")
    assert r._llm_end_call_seen is True

    async def _run():
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(text)
        q.put_nowait(None)
        out = []
        async for res in r._consume_llm_with_filler(q, asyncio.Event(), "farewell"):
            out.append(res.text)
        return out

    out = asyncio.run(_run())
    assert r.hangup_pending is True
    assert r.state.is_closing is True
    assert all("END_CALL" not in t for t in out)


def test_stale_signal_never_arms_a_later_turn():
    """A cancelled stream's leftover signal must not close the next turn."""
    r = make_router()
    r._llm_end_call_seen = True
    drive(r, "what is the price of the two bhk")
    assert r.hangup_pending is False


def test_caller_speech_cancels_llm_close():
    r = make_router()
    r.hangup_pending = True  # as armed by the LLM signal
    drive(r, "wait one more question about parking")
    assert r.hangup_pending is False


# ── silence hangup + max-duration (router side) ─────────────────────────────

def test_silence_goodbye_arms_hangup():
    r = make_router()
    text = r.generate_silence_goodbye()
    assert text
    assert r.hangup_pending is True
    assert r.call_outcome == "no_response"
    assert r.state_mgr.extra_flags.get("silence_hangup") is True


def test_silence_goodbye_cancelled_if_caller_returns():
    r = make_router()
    r.generate_silence_goodbye()
    drive(r, "hello haan main sun raha hoon")
    assert r.hangup_pending is False


def test_max_duration_close_is_hard():
    """The duration-cap goodbye is the one close caller speech can't cancel."""
    r = make_router()
    text = r.generate_timeout_goodbye()
    assert text
    assert r.hangup_pending is True
    resp = drive(r, "wait I have another question")
    assert resp == ""            # hard close: speech is not answered
    assert r.hangup_pending is True


# ── pipeline watchdog decisions (pure, on a stand-in state object) ──────────

def _pipeline_state(conv, **over):
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(
        _greeting_done=True, _tts_playing=False, _reprompt_count=0,
        conversation=conv,
        _current_generation_task=None,
        _stt_inflight=0, _user_speaking=False, _prev_speech=False,
        _stt_queue=asyncio.Queue(), _transcript_queue=asyncio.Queue(),
        _tts_queue=asyncio.Queue(),
        sink=types.SimpleNamespace(playback_busy_until=0.0),
        _tts_end_time=0.0, _last_voice_activity=0.0, _last_reprompt_ts=0.0,
        _last_heard_activity=0.0,
        _silence_hangup_fired=False, _maxdur_fired=False, _call_start_ts=0.0,
    )
    for helper in ("_pipeline_quiet", "_silence_anchor", "_termination_cfg",
                   "_silence_hangup_due", "_max_duration_due",
                   "_silence_reprompt_due"):
        setattr(ns, helper, types.MethodType(getattr(VoicePipeline, helper), ns))
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


@pytest.fixture
def _watchdog_enabled(monkeypatch):
    import app.voice.pipeline as pl

    monkeypatch.setattr(pl, "SILENCE_HANGUP_ENABLED", True)
    monkeypatch.setattr(pl, "SILENCE_REPROMPT_ENABLED", True)


def test_silence_hangup_fires_after_cap_and_grace(_watchdog_enabled):
    now = 1000.0
    ns = _pipeline_state(make_router(), _reprompt_count=2,
                         _tts_end_time=now - 30.0, _last_reprompt_ts=now - 30.0)
    assert ns._silence_hangup_due(now) is True


def test_silence_hangup_waits_for_reprompt_cap(_watchdog_enabled):
    now = 1000.0
    ns = _pipeline_state(make_router(), _reprompt_count=1,
                         _tts_end_time=now - 30.0)
    assert ns._silence_hangup_due(now) is False


def test_silence_hangup_respects_grace_window(_watchdog_enabled):
    now = 1000.0
    ns = _pipeline_state(make_router(), _reprompt_count=2,
                         _tts_end_time=now - 30.0, _last_reprompt_ts=now - 8.0)
    # last reprompt only 8s ago < SILENCE_REPROMPT_S(6) + grace(10)
    assert ns._silence_hangup_due(now) is False


def test_silence_hangup_skips_when_close_already_pending(_watchdog_enabled):
    now = 1000.0
    conv = make_router()
    conv.hangup_pending = True
    ns = _pipeline_state(conv, _reprompt_count=2, _tts_end_time=now - 30.0,
                         _last_reprompt_ts=now - 30.0)
    assert ns._silence_hangup_due(now) is False


def test_silence_hangup_per_agent_disable(_watchdog_enabled):
    now = 1000.0
    conv = make_router(termination={"silence_hangup": {"enabled": False}})
    ns = _pipeline_state(conv, _reprompt_count=2, _tts_end_time=now - 30.0,
                         _last_reprompt_ts=now - 30.0)
    assert ns._silence_hangup_due(now) is False


def test_max_duration_due_after_cap():
    now = 1000.0
    conv = make_router(termination={"max_call_minutes": 1})
    ns = _pipeline_state(conv, _call_start_ts=now - 90.0,
                         _tts_end_time=now - 7.0)
    assert ns._max_duration_due(now) is True


def test_max_duration_needs_a_configured_cap():
    now = 1000.0
    ns = _pipeline_state(make_router(), _call_start_ts=now - 9000.0,
                         _tts_end_time=now - 7.0)
    assert ns._max_duration_due(now) is False


def test_max_duration_waits_for_quiet_moment():
    now = 1000.0
    conv = make_router(termination={"max_call_minutes": 1})
    ns = _pipeline_state(conv, _call_start_ts=now - 90.0,
                         _tts_end_time=now - 7.0, _tts_playing=True)
    assert ns._max_duration_due(now) is False


def test_max_duration_fires_once():
    now = 1000.0
    conv = make_router(termination={"max_call_minutes": 1})
    ns = _pipeline_state(conv, _call_start_ts=now - 90.0,
                         _tts_end_time=now - 7.0, _maxdur_fired=True)
    assert ns._max_duration_due(now) is False


# ── prompt rendering ────────────────────────────────────────────────────────

def test_closure_block_teaches_the_token_by_default():
    r = make_router()
    joined = "\n".join(pb.call_closure_block(r._def, "en"))
    assert "[END_CALL]" in joined


def test_closure_block_renders_end_conditions():
    r = make_router(termination={
        "end_conditions": ["if the caller becomes abusive, end the call"]})
    joined = "\n".join(pb.call_closure_block(r._def, "en"))
    assert "END-CALL RULES" in joined
    assert "abusive" in joined


def test_closure_block_without_llm_end_call():
    r = make_router(termination={"llm_end_call": False})
    joined = "\n".join(pb.call_closure_block(r._def, "en"))
    assert "[END_CALL]" not in joined


# ── close-confirmation gate (2026-07-31) ────────────────────────────────────
# Live regression: STT heard "okay, by the way..." as "okay bye", the engine
# closed instantly, and the channel dropped while the caller was still
# talking. Every speech-triggered close now asks once before hanging up.

def test_misheard_goodbye_asks_instead_of_closing():
    r = make_router()
    out = drive(r, "okay bye")
    assert r.hangup_pending is False
    assert r.state.is_closing is False
    assert r._close_confirm_pending is True
    assert "end the call" in out.lower()    # "Alright then, shall I end the call here?"


def test_confirmed_goodbye_closes():
    r = make_router()
    drive(r, "okay bye")
    out = drive(r, "yes")
    assert r.hangup_pending is True
    assert r.state.is_closing is True
    assert r._close_confirm_pending is False
    # Owner-specified platform thank-you, not the agent's closing speech —
    # a goodbye may already have been spoken on the confirming turn.
    assert "thank you for talking" in out.lower()


def test_denied_close_keeps_the_call_alive():
    """'no, I didn't say that' must resume the conversation, not end it."""
    r = make_router()
    drive(r, "okay bye")
    drive(r, "no no")
    assert r.hangup_pending is False
    assert r.state.is_closing is False
    assert r._close_confirm_pending is False


def test_content_answer_to_the_confirm_is_not_a_yes():
    """The exact live turn: the caller answered with a request, not a
    goodbye — it must be handled as conversation."""
    r = make_router()
    drive(r, "okay bye")
    drive(r, "can you take my email address?")
    assert r.hangup_pending is False
    assert r._close_confirm_pending is False


def test_affirmation_inside_a_longer_sentence_is_not_a_yes():
    r = make_router()
    drive(r, "okay bye")
    drive(r, "yes but first can you tell me the price of the two bhk")
    assert r.hangup_pending is False


def test_confirm_gate_is_capped_so_a_real_goodbye_still_ends():
    """A caller who keeps saying bye is never trapped in a confirm loop."""
    r = make_router()
    for _ in range(r._CLOSE_CONFIRM_MAX):
        drive(r, "okay bye")
        drive(r, "actually one more thing about the location")
    drive(r, "okay bye")
    assert r.hangup_pending is True     # cap spent — closes directly


def test_confirm_gate_can_be_disabled_per_agent():
    r = make_router(termination={"confirm_before_end": False})
    drive(r, "okay bye")
    assert r.hangup_pending is True


def test_confirm_question_follows_the_call_language():
    r = make_router()
    r.state.language = "hi"
    out = drive(r, "theek hai bye")
    assert r.hangup_pending is False
    assert "कॉल कट" in out    # "ठीक है, तो फिर मैं कॉल कट कर दूँ?"
    out2 = drive(r, "haan")
    assert r.hangup_pending is True
    assert "धन्यवाद" in out2  # "बात करने के लिए धन्यवाद, आपका दिन शुभ हो।"


def test_end_call_marker_on_a_question_is_ignored():
    """The live bug: the model stapled [END_CALL] onto 'is there anything
    else I can help you with today?' and the call was cut as the caller
    answered. A turn that asks something is not a goodbye."""
    r = make_router()
    text = r._strip_end_call_marker(
        "I understand. Is there anything else I can help you with today? [END_CALL]")
    assert r._llm_end_call_seen is True

    async def _run():
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(text)
        q.put_nowait(None)
        out = []
        async for res in r._consume_llm_with_filler(q, asyncio.Event(), "question"):
            out.append(res.text)
        return out

    out = asyncio.run(_run())
    assert r.hangup_pending is False
    assert r._close_confirm_pending is False    # not even a confirm — no close
    assert all("END_CALL" not in t for t in out)


def test_llm_goodbye_is_confirmed_before_closing():
    r = make_router()
    text = r._strip_end_call_marker("It was lovely speaking with you. Goodbye! [END_CALL]")

    async def _run():
        q: asyncio.Queue = asyncio.Queue()
        q.put_nowait(text)
        q.put_nowait(None)
        out = []
        async for res in r._consume_llm_with_filler(q, asyncio.Event(), "farewell"):
            out.append(res.text)
        return out

    out = asyncio.run(_run())
    joined = " ".join(out)
    assert r.hangup_pending is False
    assert r._close_confirm_pending is True
    assert "end the call" in joined.lower()
    final = drive(r, "yes")
    assert r.hangup_pending is True
    assert "thank you for talking" in final.lower()


def test_dnd_still_closes_immediately():
    """Compliance closes are NOT gated — re-asking someone who said 'stop
    calling me' is worse than the mishearing risk."""
    r = make_router(guardrails={"compliance": {"dnd_phrases": ["stop calling me"]}})
    r.state.stage = "S2"
    drive(r, "please stop calling me")
    assert r.hangup_pending is True
    assert r._close_confirm_pending is False


def test_silence_hangup_still_closes_immediately():
    """Nobody is on the line to answer a confirmation question."""
    r = make_router()
    assert r.generate_silence_goodbye()
    assert r.hangup_pending is True
    assert r._close_confirm_pending is False


def test_max_duration_close_still_hard():
    r = make_router(termination={"max_call_minutes": 5})
    assert r.generate_timeout_goodbye()
    assert r.hangup_pending is True
    assert r._hangup_hard is True
    assert r._close_confirm_pending is False


def test_closure_block_mentions_the_platform_confirm():
    r = make_router()
    joined = "\n".join(pb.call_closure_block(r._def, "en"))
    assert "asks the caller a question" in joined
    assert "mishear" in joined.lower()
    # The platform speaks the final thank-you after the confirm, so the LLM
    # is told to keep its own goodbye to one sentence (double-farewell fix).
    assert "ONE short sentence" in joined


# ── dead-air repair must not talk over a decided close ──────────────────────
# Live call 6ad891a4 (2026-08-25): 10s after the agent spoke "Thank you for
# your time, Angelo, and have a wonderful day!" the repair arm asked "Sorry,
# I couldn't hear you. Is it alright if we close the call now?" — a fifth
# close question after the farewell. _silence_hangup_due has refused to fire
# into a pending close since 2026-07-29; the reprompt arm never got the guard.

def test_reprompt_skips_when_a_close_is_pending(_watchdog_enabled):
    now = 1000.0
    conv = make_router()
    conv.hangup_pending = True
    ns = _pipeline_state(conv, _tts_end_time=now - 30.0)
    assert ns._silence_reprompt_due(now) is False


def test_reprompt_skips_once_the_router_is_closing(_watchdog_enabled):
    """hangup_pending can be cleared while the goodbye is still draining —
    is_closing is the durable statement that the call is wrapping up."""
    now = 1000.0
    conv = make_router()
    conv.state.is_closing = True
    ns = _pipeline_state(conv, _tts_end_time=now - 30.0)
    assert ns._silence_reprompt_due(now) is False


def test_reprompt_still_fires_on_ordinary_dead_air(_watchdog_enabled):
    """The guard must not disable repair on a live call — this is the case
    the reprompt exists for."""
    now = 1000.0
    ns = _pipeline_state(make_router(), _tts_end_time=now - 30.0)
    assert ns._silence_reprompt_due(now) is True

