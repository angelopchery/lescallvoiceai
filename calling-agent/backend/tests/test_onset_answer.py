"""Ravi's "long pause after identity confirmation" (RUNBOOK §65).

Calls 994 / 997 / 998 / 1008: the connect intro ("क्या मेरी बात X जी से हो
रही है?") fired on the caller's pickup hello, and the caller's "हां जी" that
answered it was glued onto that hello and absorbed as a greeting — 6-8 s of
silence, then the identity question again. And every confirmation that did
get through paid a 1.1-2.3 s LLM turn to regenerate the owner's own
`good_time_check_line`.
"""

from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter, _is_identity_yes
from app.voice.pipeline import VoicePipeline
from test_call_termination import drive

RAVI_OUT = Path(__file__).parent / "fixtures" / "ravi_outbound_2026_09_24.yaml"


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """No network LLM — see conftest."""


def ravi(monkeypatch=None) -> ConversationRouter:
    """Ravi mid-connect: opened with "हेलो, नमस्ते।", the caller started
    speaking, and the pipeline spoke the identity line on that onset."""
    r = ConversationRouter(agent_def=AgentDefinition.from_yaml(RAVI_OUT),
                           default_language="hi")
    r._continuation_enabled = True
    r.state.language = "hi"
    r.outbound_contact_name = "Angelo P Chery"
    r.last_agent_response = "हेलो, नमस्ते।"
    r._opening_ts = time.monotonic()
    line = r.connect_onset_line()
    assert "क्या मेरी बात" in line
    r.note_connect_intro_spoken(line)
    return r


# ── the yes vocabulary ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "हां जी हां जी।", "हाँ।", "हाँ जी", "जी।", "yes", "Yes, yes.",
    "हाँ मैं ही बोल रहा हूँ", "हाँ बोलिए।", "हेलो, हाँ जी, बताओ।",
    "yes speaking", "હા બોલો", "हेलो। हां जी हां जी।",
])
def test_plain_confirmations(text):
    assert _is_identity_yes(text)


@pytest.mark.parametrize("text", [
    "हेलो", "हेलो जी", "नमस्कार", "कौन बोल रहा है?", "नहीं", "जी नहीं।",
    "हाँ Jignesh बोल रहा हूँ", "हाँ पर अभी busy हूँ", "wrong number",
    "आप कौन बोल रहे हैं?", "",
])
def test_not_plain_confirmations(text):
    assert not _is_identity_yes(text)


# ── the router ──────────────────────────────────────────────────────────────

def test_pickup_hello_is_absorbed_without_a_trace():
    r = ravi()
    assert drive(r, "हेलो।") == ""
    assert not r.has_pending_turn()          # nothing to glue the yes onto
    assert r.state.turn_count == 0


def test_yes_after_the_intro_gets_the_authored_line_at_once():
    """Call 1008, fixed: hello absorbed, then the yes is answered — with the
    owner's words, no model turn."""
    r = ravi()
    drive(r, "हेलो।")
    r.end_connect_onset_absorb()
    resp = drive(r, "हां जी हां जी।")
    assert "मैं रवि बोल रहा हूँ Pramukh Group से" in resp
    assert "दो मिनट" in resp
    assert r._good_time_spoken
    assert r.state.stage != "GREETING"


def test_merged_hello_plus_yes_is_not_absorbed_once_answered():
    """Call 994: the pipeline merged "hello" + "जी हां". Once the caller is
    answering the intro, the absorb window is closed."""
    r = ravi()
    r.end_connect_onset_absorb()
    resp = drive(r, "hello जी हां।")
    assert "Pramukh Group" in resp


def test_absorb_still_eats_a_pickup_hello_before_the_intro_ends():
    r = ravi()
    assert drive(r, "नमस्कार") == ""


def test_who_is_this_is_not_the_fast_path():
    r = ravi()
    r.end_connect_onset_absorb()
    drive(r, "कौन बोल रहा है?")
    assert not r._good_time_spoken


def test_no_is_not_the_fast_path():
    r = ravi()
    r.end_connect_onset_absorb()
    drive(r, "जी नहीं।")
    assert not r._good_time_spoken


def test_bare_hello_after_the_intro_is_a_presence_check_not_a_yes():
    """Call 997: a hello after the intro finished means they did not hear it."""
    r = ravi()
    r.end_connect_onset_absorb()
    resp = drive(r, "hello")
    assert "क्या मेरी बात" in resp
    assert not r._good_time_spoken


def test_fast_path_only_once():
    r = ravi()
    r.end_connect_onset_absorb()
    drive(r, "हाँ जी")
    r.last_agent_response = "नमस्ते! क्या मेरी बात Angelo P Chery जी से हो रही है?"
    assert r._good_time_fastpath_line("हाँ जी", "affirmation") == ""


def test_agent_opt_out(monkeypatch):
    r = ravi()
    r._def.call_flow.connect = {"good_time_fastpath": False}
    r.end_connect_onset_absorb()
    assert r._good_time_fastpath_line("हाँ जी", "affirmation") == ""


def test_needs_the_identity_question_to_have_been_asked():
    r = ravi()
    r.last_agent_response = "और आप कहाँ से बात कर रहे हैं?"
    assert r._good_time_fastpath_line("हाँ जी", "affirmation") == ""


def test_reask_after_dead_air_still_counts_as_asked():
    r = ravi()
    r.last_agent_response = ("माफ़ कीजिए, मुझे आपकी आवाज़ नहीं आई। क्या मेरी बात "
                             "Angelo P Chery जी से हो रही है?")
    assert "Pramukh Group" in r._good_time_fastpath_line("हाँ जी", "general")


# ── the pipeline's pickup decisions ─────────────────────────────────────────

def _pipe(**over):
    ns = types.SimpleNamespace(
        _onset_play_end_ts=0.0, _caller_split_turns=0,
        _call_start_ts=time.monotonic(),
        conversation=types.SimpleNamespace(
            state=types.SimpleNamespace(turn_count=0)))
    for k, v in over.items():
        setattr(ns, k, v)
    for name in ("_onset_reaction", "_in_pickup_phase", "_note_caller_split"):
        setattr(ns, name, types.MethodType(getattr(VoicePipeline, name), ns))
    return ns


def test_speech_ending_after_the_intro_is_a_reaction():
    now = time.monotonic()
    p = _pipe(_onset_play_end_ts=now)
    assert p._onset_reaction(types.SimpleNamespace(speech_end_ts=now + 1.0))
    # The pickup hello ended while the intro was still playing.
    assert not p._onset_reaction(types.SimpleNamespace(speech_end_ts=now - 2.0))


def test_no_reaction_before_any_onset_intro():
    p = _pipe()
    assert not p._onset_reaction(
        types.SimpleNamespace(speech_end_ts=time.monotonic()))


def test_pickup_splits_do_not_arm_the_hold():
    """998 / 999 / 1008: a hello over the intro + the yes after it armed a
    900ms hold on every later turn."""
    p = _pipe()
    p._note_caller_split("rollback merge")
    assert p._caller_split_turns == 0
    p.conversation.state.turn_count = 1          # a real turn has happened
    p._note_caller_split("merged inside the window")
    assert p._caller_split_turns == 1
