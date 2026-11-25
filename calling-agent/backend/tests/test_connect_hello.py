"""Connect hello-loop counters (2026-07-30).

Live calls looped at connect: bare "Hello" openings invited the caller's
"hello?", the reply arrived after ~2.5s of dead air, and the caller's next
hello barged in and killed the reply before its first WAV was flushed —
so the caller heard silence, said hello again, and both sides kept
resetting each other (full diagnosis: deploy/PLAN-connect-hello-loop.md).

This suite covers the platform counters:
- instant identity intro on the first bare hello (router fast path)
- the composed intro line (per language, gender-neutral, identity-safe)
- dead-air reprompt never parrots a bare "Hello"
- bare hellos never fill QA fields / slots / stage-turn budgets
- the pipeline's connect delivery guard (pure decision function)
- the connect-window STT floor and the empty-streak damper decision
"""

from __future__ import annotations

import time
import types

import pytest

from app.voice.conversation import ConversationRouter
from test_call_termination import drive, make_router


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def hello_router(opening: str = "Hello", connect: dict | None = None,
                 **over) -> ConversationRouter:
    """A router mid-call whose last utterance was the (possibly bare)
    opening, inside the early-hello window."""
    if connect is not None:
        cf = {
            "stages": [
                {"id": "S1", "order": 0, "goal": "one",
                 "turns_until_advance": 8},
                {"id": "S2", "order": 1, "goal": "two",
                 "turns_until_advance": 99},
            ],
            "connect": connect,
        }
        over.setdefault("call_flow", cf)
    r = make_router(**over)
    r.last_agent_response = opening
    r._opening_ts = time.monotonic()
    return r


# ── instant intro fast path ─────────────────────────────────────────────────

def test_intro_fastpath_on_bare_opening():
    r = hello_router("Hello")
    resp = drive(r, "hello")
    assert r._intro_delivered is True
    assert "TestCo" in resp                      # identity, not a parrot
    assert not r._is_bare_hello_text(resp)


def test_intro_fastpath_then_hello_is_a_presence_check():
    """After the intro a further hello is a presence check (RUNBOOK §63):
    "I can hear you" + the question — not the whole intro verbatim again —
    and a second one in a row says who we are."""
    r = hello_router("Hello")
    first = drive(r, "hello")
    second = drive(r, "hello")
    assert second.startswith("Yes, I can hear you.")
    assert second.endswith(first.split(". ")[-1])
    assert r._hello_replays == 0
    third = drive(r, "hello")
    assert "Asha" in third and "TestCo" in third


def test_intro_fastpath_agent_optout():
    r = hello_router("Hello", connect={"hello_fastpath": False})
    drive(r, "hello")
    assert r._intro_delivered is False


def test_intro_fastpath_env_optout(monkeypatch):
    import app.voice.conversation as conv_mod
    monkeypatch.setattr(conv_mod, "CONNECT_HELLO_FASTPATH", False)
    r = hello_router("Hello")
    drive(r, "hello")
    assert r._intro_delivered is False


def test_full_opening_still_replays_not_intro():
    """A real opening line keeps today's replay behaviour untouched."""
    opening = "Hi, am I speaking with Angelo from TestCo?"
    r = hello_router(opening)
    resp = drive(r, "hello")
    assert resp == opening
    assert r._intro_delivered is False


def test_fastpath_only_in_early_window():
    r = hello_router("Hello")
    r._opening_ts = time.monotonic() - 60.0      # window long expired
    drive(r, "hello")
    assert r._intro_delivered is False


def test_real_first_turn_unaffected():
    r = hello_router("Hello")
    drive(r, "I want to know about the two bhk pricing")
    assert r._intro_delivered is False


# ── the composed intro line ─────────────────────────────────────────────────

def test_intro_line_composes_identity():
    r = hello_router("Hello")
    line = r.connect_intro_line()
    assert "Asha" in line and "TestCo" in line


def test_intro_line_agent_same_as_company_not_doubled():
    r = hello_router(
        "Hello",
        identity={"agent_name": "TestCo", "company_name": "TestCo"})
    line = r.connect_intro_line()
    assert line.count("TestCo") == 1


def test_intro_line_hindi_when_language_hi():
    r = hello_router("Hello")
    r.state.language = "hi"
    line = r.connect_intro_line()
    assert "नमस्ते" in line and "TestCo" in line


def test_intro_line_prefers_full_configured_opening():
    r = hello_router("Hello")
    r._def.response_bank.opening_greeting = \
        "Hello! This is Asha from TestCo. How can I help?"
    assert r.connect_intro_line() == \
        "Hello! This is Asha from TestCo. How can I help?"


def test_intro_line_personalizes_a_placeholder_opening():
    """A placeholder must never be SPOKEN raw — but it must also never cost
    the business its own words.

    This used to reject any opening containing "{" and fall through to the
    platform's identity template, so precisely the businesses that wrote the
    most tailored opening got the generic line ("Hello! This is X from Y.
    Can you hear me clearly?" — the 2026-08-04 "all agents say the same
    thing" report). Substitute it, don't discard it.
    """
    r = hello_router("Hello", call_direction="outbound")
    r.outbound_contact_name = "Ramesh"
    r._def.response_bank.opening_greeting = \
        "Hello, am I speaking with {contact_name}?"
    line = r.connect_intro_line()
    assert line == "Hello, am I speaking with Ramesh?"
    assert "TestCo" not in line          # the template was NOT used


def test_intro_line_reads_the_outbound_field_on_outbound_agents():
    """An outbound agent's own opening lives in outbound.opening_greeting;
    reading only response_bank there skipped the business's words."""
    r = hello_router("Hello", call_direction="outbound",
                     outbound={"opening_greeting": {
                         "en": "Good day, calling from the TestCo desk about your enquiry."}},
                     response_bank={"opening_greeting": "Hello"})
    assert r.connect_intro_line() == \
        "Good day, calling from the TestCo desk about your enquiry."


def test_intro_line_prefers_substantive_last_response():
    r = hello_router("Hello")
    r.last_agent_response = "The site visit is booked for Sunday."
    assert r.connect_intro_line() == "The site visit is booked for Sunday."


# ── dead-air reprompt ───────────────────────────────────────────────────────

def test_reprompt_never_parrots_bare_hello():
    r = hello_router("Hello")
    text = r.generate_silence_reprompt()
    assert text
    assert not r._is_bare_hello_text(text)
    assert "TestCo" in text


def test_reprompt_unchanged_for_real_last_response():
    r = hello_router("What is your budget range?")
    r.state.turn_count = 0
    assert r.generate_silence_reprompt() == "What is your budget range?"


# ── pollution guards ────────────────────────────────────────────────────────

def test_hello_does_not_answer_qa_fields():
    r = hello_router("Hello")
    r.memory.qa_tracker._asked["name"] = 0
    drive(r, "hello hello")
    assert "name" not in r.memory.qa_tracker._answered
    drive(r, "my name is Rahul")
    assert "name" in r.memory.qa_tracker._answered


def test_scrub_hello_values_drops_hello_slots():
    r = hello_router("Hello")
    out = r._scrub_hello_values(
        {"name": "hello hello hello", "city": "Surat"})
    assert out == {"city": "Surat"}


def test_early_hello_does_not_burn_stage_turns():
    r = hello_router("Hello")
    r._hello_replays = 2          # replay cap spent → hellos reach the LLM
    r._intro_delivered = True
    before = r.state.discovery_turns
    drive(r, "hello")
    assert r.state.discovery_turns == before
    drive(r, "we are looking for a three bhk")
    assert r.state.discovery_turns == before + 1


# ── pipeline: connect delivery guard (pure decisions) ───────────────────────

def _guard_state(conv, **over):
    from app.voice.pipeline import VoicePipeline

    now = over.pop("now", 1000.0)
    ns = types.SimpleNamespace(
        conversation=conv,
        _tts_playing=True,
        _call_start_ts=now - 3.0,          # inside CONNECT_WINDOW_S
        _utt_protected=False,
        _utt_first_flush_ts=0.0,
        sink=types.SimpleNamespace(playback_busy_until=0.0),
    )
    for helper in ("_connect_barge_defer", "_connect_cfg", "_bare_hello",
                   "_min_speech_ms"):
        setattr(ns, helper, types.MethodType(
            getattr(VoicePipeline, helper), ns))
    for k, v in over.items():
        setattr(ns, k, v)
    return ns, now


@pytest.fixture
def _guard_enabled(monkeypatch):
    import app.voice.pipeline as pl

    monkeypatch.setattr(pl, "CONNECT_DELIVERY_GUARD", True)


def test_defer_when_nothing_flushed(_guard_enabled):
    ns, now = _guard_state(make_router())
    assert ns._connect_barge_defer(now) is True


def test_defer_records_first_flush_and_holds_briefly(_guard_enabled):
    ns, now = _guard_state(make_router())
    ns.sink.playback_busy_until = now + 2.0    # audio reached the clock
    assert ns._connect_barge_defer(now) is True
    assert ns._utt_first_flush_ts == now


def test_no_defer_once_enough_played(_guard_enabled):
    ns, now = _guard_state(make_router(), _utt_first_flush_ts=1000.0 - 1.5)
    assert ns._connect_barge_defer(now) is False


def test_no_defer_outside_window_unprotected(_guard_enabled, monkeypatch):
    """Legacy scope (BARGE_GUARD_ALL_REPLIES=false): connect window only."""
    import app.voice.pipeline as _pl
    monkeypatch.setattr(_pl, "BARGE_GUARD_ALL_REPLIES", False)
    ns, now = _guard_state(make_router(), _call_start_ts=1000.0 - 60.0)
    assert ns._connect_barge_defer(now) is False


def test_unheard_reply_deferred_any_time(_guard_enabled, monkeypatch):
    """RUNBOOK §70 (call 1033): a reply the caller has not heard yet is
    protected at ANY point of the call, not only in the connect window."""
    import app.voice.pipeline as _pl
    monkeypatch.setattr(_pl, "BARGE_GUARD_ALL_REPLIES", True)
    ns, now = _guard_state(make_router(), _call_start_ts=1000.0 - 60.0)
    assert ns._connect_barge_defer(now) is True


def test_defer_for_protected_utterance_any_age(_guard_enabled):
    """Replays / intros / reprompts stay guarded beyond the window."""
    ns, now = _guard_state(make_router(), _call_start_ts=1000.0 - 60.0,
                           _utt_protected=True)
    assert ns._connect_barge_defer(now) is True


def test_no_defer_when_not_playing(_guard_enabled):
    ns, now = _guard_state(make_router(), _tts_playing=False)
    assert ns._connect_barge_defer(now) is False


def test_no_defer_when_agent_opts_out(_guard_enabled):
    conv = make_router()
    conv._def.call_flow.connect = {"delivery_guard": False}
    ns, now = _guard_state(conv)
    assert ns._connect_barge_defer(now) is False


def test_no_defer_when_env_disabled():
    ns, now = _guard_state(make_router())
    import app.voice.pipeline as pl
    if pl.CONNECT_DELIVERY_GUARD:      # pragma: no cover — env-dependent
        pytest.skip("CONNECT_DELIVERY_GUARD enabled in this environment")
    assert ns._connect_barge_defer(now) is False


# ── pipeline: connect STT floor + empty-streak damper ───────────────────────

def test_min_speech_lowered_in_connect_window():
    from app.voice.pipeline import (CONNECT_MIN_SPEECH_MS, MIN_SPEECH_MS,
                                    VoicePipeline)

    ns = types.SimpleNamespace(_call_start_ts=1000.0 - 3.0)
    ns._min_speech_ms = types.MethodType(VoicePipeline._min_speech_ms, ns)
    assert ns._min_speech_ms(1000.0) == min(CONNECT_MIN_SPEECH_MS,
                                            MIN_SPEECH_MS)
    ns._call_start_ts = 1000.0 - 60.0
    assert ns._min_speech_ms(1000.0) == MIN_SPEECH_MS


def test_empty_streak_damper_speaks_only_second(monkeypatch):
    import app.voice.pipeline as pl

    monkeypatch.setattr(pl, "EMPTY_STREAK_DAMPER", True)
    speaks = [n for n in range(1, 5)
              if pl.VoicePipeline._empty_fallback_speaks(n)]
    assert speaks == [2]
    monkeypatch.setattr(pl, "EMPTY_STREAK_DAMPER", False)
    assert all(pl.VoicePipeline._empty_fallback_speaks(n)
               for n in range(1, 5))


# ── pipeline: hello turns never merge into chains ───────────────────────────

def test_bare_hello_delegate():
    from app.voice.pipeline import VoicePipeline

    ns = types.SimpleNamespace(conversation=make_router())
    ns._bare_hello = types.MethodType(VoicePipeline._bare_hello, ns)
    assert ns._bare_hello("hello") is True
    assert ns._bare_hello("hello haan hello") is True
    assert ns._bare_hello("hello I need help") is False
    ns.conversation = None
    assert ns._bare_hello("hello") is False


# ── the SECOND BEAT is settable: call_flow.connect.intro_line ───────────────
#
# The gap this closes (2026-08-04): since the 07-30 opening doctrine the
# agent says a short "Hello", waits, and then speaks a second line the
# platform composed from identity.agent_name + identity.company_name. There
# was no field for it, so an owner asking five times across three agents for
# "Hello, wait, THEN say <exact sentence>" got platform boilerplate every
# time. intro_line is that field.

def test_intro_line_config_wins_over_composed():
    r = hello_router("Hello", connect={
        "intro_line": {"en": "Thanks for picking up, this is Asha."}})
    resp = drive(r, "hello")
    assert "Thanks for picking up, this is Asha." in resp
    assert "TestCo" not in resp          # the composed intro was bypassed


def test_intro_line_accepts_a_bare_string():
    r = hello_router("Hello", connect={"intro_line": "One line, no map."})
    assert "One line, no map." in drive(r, "hello")


def test_intro_line_per_language():
    r = hello_router("Hello", connect={
        "intro_line": {"en": "English line.", "hi": "हिंदी लाइन।"}})
    r.state.language = "hi"
    assert "हिंदी लाइन।" in drive(r, "hello")


def test_intro_line_substitutes_contact_name():
    r = hello_router("Hello", connect={
        "intro_line": {"en": "Am I speaking to {contact_name}?"}},
        call_direction="outbound")
    r.outbound_contact_name = "Ramesh"
    resp = drive(r, "hello")
    assert "Ramesh" in resp and "{" not in resp


def test_intro_line_absent_is_todays_behaviour():
    """The backward-compat pin: every existing agent has connect={}."""
    plain = drive(hello_router("Hello"), "hello")
    empty = drive(hello_router("Hello", connect={}), "hello")
    assert plain == empty
    assert "TestCo" in plain


def test_intro_line_bare_hello_is_rejected():
    """A configured 'Hello' would re-open the parrot loop this path kills."""
    r = hello_router("Hello", connect={"intro_line": {"en": "Hello"}})
    resp = drive(r, "hello")
    assert "TestCo" in resp              # fell through to the composed intro


def test_intro_line_outranks_a_long_opening():
    """response_bank.opening_greeting is tier 2 and is live on real agents
    with long openings — it must not shadow what the owner dictated."""
    r = hello_router("Hello", connect={"intro_line": {"en": "Dictated line."}},
                     response_bank={"opening_greeting":
                                    "Hello and welcome to TestCo, how may I help?"})
    assert "Dictated line." in drive(r, "hello")


def test_intro_line_drives_the_dead_air_reprompt():
    r = hello_router("Hello", connect={"intro_line": {"en": "Dictated line."}})
    r.state.turn_count = 0
    assert "Dictated line." in r.generate_silence_reprompt()


# ── the business's OWN identity line is the second beat (2026-08-04) ────────
#
# Live call b6c5d398 (Prakhar / Pramukh group, outbound): the caller said
# "hello" and heard "Hello! This is Prakhar from Pramukh group. Can you hear
# me clearly?" — the platform template — while the owner's own line sat in
# outbound.identity_confirm_line, which the engine only ever fed to the LLM
# as prompt context. The owner's words arrived a turn late, if at all.

def test_outbound_identity_line_is_spoken_as_the_second_beat():
    r = hello_router("Hello", call_direction="outbound",
                     outbound={"opening_greeting": {"en": "Hello?"},
                               "identity_confirm_line": {
                                   "en": "Hope you're having a very good "
                                         "{current_time}, just wanted to make "
                                         "sure if I am speaking to {contact_name}?"}})
    r.outbound_contact_name = "Angelo"
    resp = drive(r, "hello")
    assert "just wanted to make sure if I am speaking to Angelo?" in resp
    assert "Can you hear me clearly" not in resp    # template NOT used
    assert "{" not in resp


def test_outbound_identity_line_is_per_language():
    r = hello_router("Hello", call_direction="outbound",
                     outbound={"opening_greeting": {"en": "Hello?"},
                               "identity_confirm_line": {
                                   "en": "Am I speaking to {contact_name}?",
                                   "hi": "क्या मैं {contact_name} जी से बात कर रहा हूँ?"}})
    r.outbound_contact_name = "Angelo"
    r.state.language = "hi"
    assert "क्या मैं Angelo जी से बात कर रहा हूँ?" in drive(r, "hello")


def test_intro_line_outranks_the_identity_line():
    """An explicitly dictated second beat wins over the identity line."""
    r = hello_router("Hello", call_direction="outbound",
                     connect={"intro_line": {"en": "Dictated second line."}},
                     outbound={"opening_greeting": {"en": "Hello?"},
                               "identity_confirm_line": {
                                   "en": "Am I speaking to {contact_name}?"}})
    assert "Dictated second line." in drive(r, "hello")


def test_inbound_agents_ignore_the_identity_line():
    """Nobody to confirm on an inbound call — it must not leak there."""
    r = hello_router("Hello", call_direction="inbound",
                     outbound={"identity_confirm_line": {
                         "en": "Am I speaking to {contact_name}?"}})
    resp = drive(r, "hello")
    assert "Am I speaking to" not in resp
    assert "TestCo" in resp
