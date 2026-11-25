"""Call hearing + capture hardening (2026-07-31).

Live call 918076562864_20260731_052423: the streaming-STT layer silently
destroyed real caller speech (plausibility scored stream-stitched text
against one VAD chunk; barge-in invalidated fed audio; late segments were
dropped as "stale"), and the email capture armed five turns after the ask,
fought the LLM for ownership, then discarded the confirmed address at
call end. Full diagnosis: deploy/PLAN-call-hearing-and-capture-hardening.md.

Covers Part H (hearing: continuation hold, हेलो allowlist, condensed
watchdog replay) and Part C (ask-time arming, engine-owned readbacks,
token-based bare confirm with read-back context, capture-scoped extraction
hygiene, script-aware echo guard, answer-slot overwrite).

The streaming-only half of Part H (H1 covered-seconds plausibility, H2
barge keep-audio, H3 late-segment stash, H4 consumed-episode empties) was
removed on 2026-07-31 together with the WebSocket STT provider it
mitigated — see RUNBOOK §24.
"""

from __future__ import annotations

import asyncio
import time
import types

import pytest

from app.voice.conversation import (_AFFIRM_TOKENS, _NEG_TOKENS,
                                    _all_tokens_in)
from test_call_termination import drive, make_router


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def email_router(**extra_slots):
    slots = [
        {"name": "email", "priority": 100, "required": True, "type": "text",
         "merge_policy": "free", "question": {"en": "What is your email?"}},
    ]
    if extra_slots.get("with_time"):
        slots.append({"name": "preferred_time", "priority": 90,
                      "required": True, "type": "time",
                      "question": {"en": "What time works?"}})
    return make_router(slots=slots)


# ── H: hearing ──────────────────────────────────────────────────────────────

def test_batch_flush_drains_queues_without_stream_hooks():
    """The barge-in queue flush drains the three pipeline queues and bumps
    the delivery epoch. H1-H4 (covered-seconds plausibility, barge
    keep-audio, late-segment stash, consumed-episode empties) were
    streaming-only mitigations and were removed with the streaming
    provider on 2026-07-31 — this pins that the flush no longer reaches
    for any stream handle."""
    import app.voice.pipeline as pl

    ns = types.SimpleNamespace(
        _delivery_epoch=0,
        _tts_queue=asyncio.Queue(),
        _stt_queue=asyncio.Queue(),
        _transcript_queue=asyncio.Queue(),
        # Batch STT prefetch (3f36209): the flush also cancels prefetched
        # transcription tasks — part of the batch path, not a stream hook.
        _stt_ready_queue=asyncio.Queue(),
        _stt_inflight=0,
    )
    ns._tts_queue.put_nowait("stale")
    ns._transcript_queue.put_nowait("stale")
    ns._flush_pipeline_queues = types.MethodType(
        pl.VoicePipeline._flush_pipeline_queues, ns)

    ns._flush_pipeline_queues()

    assert ns._delivery_epoch == 1
    assert ns._tts_queue.empty() and ns._transcript_queue.empty()


@pytest.mark.parametrize("text,held", [
    ("हम बेसिकली जो।", True),           # THE live incident utterance
    ("मैं बता रहा था कि", True),
    ("i was going to say and", True),
    ("થોડું કામ છે અને", True),
    ("हम काम करते हैं।", False),
    ("ठीक है।", False),
    ("i need pricing details", False),
    ("", False),
])
def test_continuation_cue_detection(text, held):
    from app.voice.pipeline import _ends_with_continuation_cue
    assert _ends_with_continuation_cue(text) is held


def test_helo_variants_in_single_word_allowlist():
    from app.voice.pipeline import _SINGLE_WORD_GREETINGS
    for w in ("हेलो", "हैलो", "हलो", "hello", "હેલો", "હલો"):
        assert w in _SINGLE_WORD_GREETINGS


def test_watchdog_replay_condenses_to_question():
    """H7: a long two-sentence replay condenses to the final question."""
    r = make_router()
    r.state.turn_count = 3
    long_reply = ("सही है, यानी अभी सब कुछ आपके भाई ही संभालते हैं। "
                  "तो फिर, जब कभी बहुत ज़्यादा कॉल्स एक साथ आ जाती हैं, "
                  "तो क्या वो सब कुछ मैनेज कर पाते हैं?")
    r.last_agent_response = long_reply
    out = r.generate_silence_reprompt()
    assert "मैनेज कर पाते हैं" in out
    assert "भाई ही संभालते" not in out


def test_watchdog_replay_keeps_short_question_whole():
    r = make_router()
    r.state.turn_count = 3
    r.last_agent_response = "What is your email?"
    out = r.generate_silence_reprompt()
    assert "What is your email?" in out


# ── C1: ask-time arming ─────────────────────────────────────────────────────

def test_llm_email_ask_arms_capture():
    """The live agent's ask came from the LLM/stage goal — note_asked never
    fired and capture stayed dark through five spelling turns."""
    r = email_router()
    r._store_last_response(
        "क्या आप मुझे अपना ईमेल एड्रेस बता सकते हैं ताकि मैं जानकारी भेज सकूँ?")
    r._refresh_capture_slot()
    assert r._capture_slot == "email"
    assert r._capture_awaiting_payload is True
    assert r.capture_expecting_input() is True


def test_capture_stays_armed_after_rejection():
    """Live: the rejection cleared pending and capture RELEASED, sending the
    caller's next spelling fragment to the LLM ('spell it slowly')."""
    r = email_router()
    r._store_last_response("Could you tell me your email please?")
    r._refresh_capture_slot()
    drive(r, "ben at gmail dot com")
    drive(r, "no")
    r._refresh_capture_slot()
    assert r._capture_slot == "email"          # still armed for the retry
    out = drive(r, "b e l at gmail dot com")
    assert "b. e. l." in out                   # engine, not LLM, handled it


def test_non_ask_response_does_not_arm():
    r = email_router()
    r._store_last_response("We provide voice automation for businesses.")
    r._refresh_capture_slot()
    assert r._capture_slot is None


# ── C3: confirm semantics ───────────────────────────────────────────────────

def test_emphatic_hindi_confirm():
    """'हाँ सही है सही है।' (5 tokens) failed the old ≤3-words bar."""
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    drive(r, "ben at gmail dot com")
    drive(r, "हाँ सही है सही है।")
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert "email" in r.state_mgr.detect_missing_slots()["filled"]


def test_all_tokens_helper():
    assert _all_tokens_in("हाँ सही है सही है।", _AFFIRM_TOKENS) is True
    assert _all_tokens_in("नहीं नहीं।", _NEG_TOKENS) is True
    assert _all_tokens_in("okay tell me more", _AFFIRM_TOKENS) is False
    assert _all_tokens_in("सही है?", _AFFIRM_TOKENS) is False   # question


def test_bare_negation_without_readback_context_does_not_discard():
    """Live: 'नहीं नहीं।' answering 'any more questions?' six turns after
    the last readback DISCARDED the email and spoke the unresolved line."""
    r = email_router()
    r.state_mgr.observe_user("it is priya dot shah at outlook dot com")
    assert "email" in r.state_mgr.detect_missing_slots()["pending"]
    r._refresh_capture_slot()
    assert r._capture_slot == "email"
    assert r._capture_awaiting_confirm is False   # nothing was read back
    drive(r, "नहीं नहीं।")
    # value survives, still pending — NOT discarded, NOT unresolved
    assert r.state_mgr.state["email"] == "priya.shah@outlook.com"
    assert "email" in r.state_mgr.detect_missing_slots()["pending"]
    assert not r.state_mgr.extra_flags.get("email_unresolved")


def test_confirmed_email_immune_to_later_negation():
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    drive(r, "ben at gmail dot com")
    drive(r, "yes")
    assert "email" in r.state_mgr.detect_missing_slots()["filled"]
    drive(r, "नहीं नहीं।")                        # "any more questions?" — no
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert not r.state_mgr.extra_flags.get("email_unresolved")


# ── C2: engine ownership ────────────────────────────────────────────────────

def test_llm_may_not_speak_an_email_while_capturing():
    """Live: the LLM read back four assemblies it could not write; the
    caller confirmed a value state never held."""
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    drive(r, "ben at gmail dot com")              # pending = ben@gmail.com
    out = r._scrub_llm_email(
        "मैं इसे कंफर्म कर लेता हूँ: anjali.panan@gmail.com, क्या यह सही है?")
    assert "anjali.panan@gmail.com" not in out
    assert "b. e. n. — at — gmail dot com" in out


def test_llm_email_scrub_without_pending_uses_generic():
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    out = r._scrub_llm_email("I have noted angelo.ann@gmail.com for you.")
    assert "angelo.ann@gmail.com" not in out


def test_llm_email_untouched_when_not_capturing():
    r = make_router()
    text = "You can reach us at support@lescall.com anytime."
    assert r._scrub_llm_email(text) == text


def test_progress_query_answered_from_state():
    """'नोट कर लिया आपने?' — deterministic status readback, never the LLM."""
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    drive(r, "ben at gmail dot com")
    out = drive(r, "नोट कर लिया आपने?")
    assert "b. e. n." in out
    assert r._capture_awaiting_confirm is True
    drive(r, "हाँ सही है")
    assert "email" in r.state_mgr.detect_missing_slots()["filled"]


def test_capture_prompt_doctrine_injected():
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    prompt = r._build_system_prompt()
    assert "EMAIL CAPTURE (ENGINE-OWNED)" in prompt
    assert "speak slowly" in prompt
    assert "EMAIL CAPTURE" not in make_router()._build_system_prompt()


# ── C4: expectation-scoped affordances ──────────────────────────────────────

def test_expecting_input_only_in_spelling_context():
    r = email_router()
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    assert r.capture_expecting_input() is True       # ask just played
    # An LLM digression answer resets the expectation (armed but idle —
    # the live call paid a 2s merge-window tax on five booking turns).
    r._store_last_response("We integrate with Zoom and Google Meet.")
    assert r._capture_slot == "email"                # refresh not run yet
    assert r.capture_expecting_input() is False


# ── C5/C6: extraction hygiene + answer overwrite ────────────────────────────

def test_extractor_skipped_while_spelling():
    """Live: the caller spelling 'angelo' became name='Panj Loa'."""
    r = email_router()
    r._slot_extract_enabled = True
    r._api_key = "test"
    r._store_last_response("What is your email?")
    r._refresh_capture_slot()
    r._spawn_slot_extractor("पंज लो आ डॉट अनन एट द रेट gmail.com")
    assert r._slot_extract_task is None


def test_answer_source_overwrites_filled_slot():
    """Live: preferred_time='Now' was immortal; the booked 'आज 4 बजे'
    never landed."""
    r = email_router(with_time=True)
    r.state_mgr.merge_state({"preferred_time": "Evening"}, source="llm")
    assert r.state_mgr.state["preferred_time"] == "Evening"
    # fill-only llm write cannot update it…
    r.state_mgr.merge_state({"preferred_time": "4 PM"}, source="llm",
                            fill_only=True)
    assert r.state_mgr.state["preferred_time"] == "Evening"
    # …but a direct answer to the agent's question can.
    changes = r.state_mgr.merge_state({"preferred_time": "4 PM"},
                                      source="answer", fill_only=False)
    assert changes and r.state_mgr.state["preferred_time"] == "4 PM"


def test_answer_source_still_banned_for_contact_slots():
    r = email_router()
    r.state_mgr.merge_state({"email": "guess@gmail.com"}, source="answer",
                            fill_only=False)
    assert r.state_mgr.state.get("email") is None


def test_asked_slot_detection_from_agent_question():
    r = email_router(with_time=True)
    r._note_ask_signals("आप आज कितने बजे फ्री रहेंगे?")
    assert r._last_asked_slot == "preferred_time"
    r._note_ask_signals("ठीक है, धन्यवाद।")
    assert r._last_asked_slot is None


# ── C7: normalizer upgrades (live-call ASR corpus) ──────────────────────────

@pytest.mark.parametrize("raw,want", [
    # spelled Devanagari letters + at-the-rate + a garbled @-token: the
    # pre-@ material must join the local part (live: only knaan@ survived)
    ("नहीं नहीं नहीं ए एन जी ई एल ओ। डॉट। पी ए। knaan@gmail.com",
     "angelo.pa.knaan@gmail.com"),
    ("मैं बताता हूँ anjali डॉट। पी ए एन ए एन। @gmail.com",
     "anjali.panan@gmail.com"),
    ("naam at the rate gmail dot com", "naam@gmail.com"),
    ("ben एट द रेट gmail.com", "ben@gmail.com"),
    ("ben एट दी रेट gmail dot com", "ben@gmail.com"),
    # ordinary words never join a local part
    ("my email is priya dot shah at outlook dot com",
     "priya.shah@outlook.com"),
])
def test_normalizer_live_corpus_round2(raw, want):
    from app.voice.contact_capture import normalize_spoken_email
    assert normalize_spoken_email(raw) == want


@pytest.mark.parametrize("raw", [
    "आंदलो। डॉट।",                 # partials from the live call — all must
    "5 लोड। पी ए एन। एन ए एन।",      # buffer silently, never answer
    "मनन एट द रेट।",
    "हां, कोई बात नहीं। angelo a n g e l o",
])
def test_live_fragments_detected_as_partial(raw):
    from app.voice.contact_capture import looks_like_partial_email
    assert looks_like_partial_email(raw) is True
