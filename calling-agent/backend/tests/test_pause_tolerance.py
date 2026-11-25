"""Turn-taking: a pause is not the end of a turn (2026-09-16).

Reported: "Whenever there is an unnatural pause in the conversation, the agent
does not respect it and keeps talking even when I am not finished… both
fragments of speech before and after should be one turn with one response."

Measured on the shubhangi calls the same day — 45 engine replies, 33 of which
took the idle fast path (zero hold), and 27 turns that then produced a
continuation the engine had to merge (17) or silently swallow (10 "stale
fragment absorbed, not answered": the caller spoke and got NO reply, because
the answer to the first half was already on the wire).

The live sequence this exists to stop, from call 871:

    05:32:46.015  [STT] 'yeah, so basically i work as an artificial
                        intelligence engineer.'          # 10 words, complete
    05:32:46.016  [ENGINE] Merge window skipped (idle)   # zero hold
    05:32:48.860  [TTS] Playing 'Got it, Mr. Piyush. What city are you
                        currently based in?'             # wrong question, and
                                                         # the caller was not done
    05:32:52.732  [ENGINE] Stale fragment absorbed, not answered:
                        "so i've had experience working on ai, machine
                         learning, and "                 # never answered

`_ends_with_continuation_cue` cannot catch this: every fragment is a
grammatically complete sentence. The signal is the SPEAKER, not the clause.
"""

from __future__ import annotations

import types

import pytest

from app.voice.pipeline import (
    TURN_CONTINUATION_HOLD_MS,
    TURN_NARRATIVE_SPEECH_MS,
    VoicePipeline,
    _ends_with_continuation_cue,
)


def _pipe(**over):
    """Minimal stand-in carrying the real pause-tolerance methods."""
    # Call started long ago: past the pickup phase unless a test says so.
    ns = types.SimpleNamespace(_caller_split_turns=0, _call_start_ts=0.0,
                               conversation=None)
    for k, v in over.items():
        setattr(ns, k, v)
    for name in ("_pause_may_continue", "_note_caller_split",
                 "_in_pickup_phase", "_tt_cfg", "_tt_num"):
        setattr(ns, name, types.MethodType(getattr(VoicePipeline, name), ns))
    return ns


def _t(text: str, duration_ms: float):
    return types.SimpleNamespace(text=text, duration_ms=duration_ms)


# ── The signal the old code had, and what it missed ────────────────────────

def test_a_dangling_cue_was_already_caught():
    assert _ends_with_continuation_cue("हम बेसिकली जो।") is True
    assert _ends_with_continuation_cue("i worked there and") is True


def test_the_live_fragment_reads_as_a_finished_sentence():
    """Why a grammar test could never have saved this turn."""
    assert _ends_with_continuation_cue(
        "yeah, so basically i work as an artificial intelligence engineer."
    ) is False


# ── Signal 1: a narrative-length segment ───────────────────────────────────

def test_a_long_segment_is_a_narrative_and_its_pause_is_held():
    p = _pipe()
    assert p._pause_may_continue(
        _t("so i have worked at another organization. i have worked in "
           "multiple startups and i have done three internships.", 8120)
    ) == "narrative"


def test_a_short_answer_keeps_the_fast_path():
    """A caller answering questions must not pay the hold — this is the
    latency the idle fast path was introduced for."""
    p = _pipe()
    assert p._pause_may_continue(_t("bangalore", 960)) == ""
    assert p._pause_may_continue(_t("yes, that's right", 1200)) == ""
    assert p._pause_may_continue(_t("3 lpa", 800)) == ""


def test_the_narrative_bar_sits_above_a_normal_answer():
    p = _pipe()
    assert p._pause_may_continue(
        _t("x", TURN_NARRATIVE_SPEECH_MS - 1)) == ""
    assert p._pause_may_continue(
        _t("x", TURN_NARRATIVE_SPEECH_MS)) == "narrative"


# ── Signal 2: this caller has already split once ───────────────────────────

def test_one_split_earns_the_benefit_of_the_doubt_for_the_call():
    p = _pipe()
    short = _t("yeah, so basically i work as an ai engineer.", 2100)
    assert p._pause_may_continue(short) == ""      # first time, no evidence
    p._note_caller_split("tail arrived after the reply")
    assert p._pause_may_continue(short) == "splitter"


def test_split_memory_is_per_call_and_starts_empty():
    assert _pipe()._caller_split_turns == 0


@pytest.mark.parametrize("why", [
    "merged inside the window",
    "late continuation",
    "tail arrived after the reply",
    "rollback merge",
])
def test_every_merge_path_records_the_split(why):
    """All four are the same fact — the caller carried on through a pause we
    had already closed. Only counting some of them would leave the hold
    disarmed on exactly the callers who need it."""
    p = _pipe()
    p._note_caller_split(why)
    assert p._caller_split_turns == 1


def test_the_hold_is_longer_than_the_default_window_and_shorter_than_a_cue():
    """A dangling conjunction is a stronger signal than a speaker habit, so
    it keeps the longer hold; both beat the 550ms default."""
    from app.voice.pipeline import (
        MERGE_HOLD_CONTINUATION_MS, TRANSCRIPT_MERGE_WINDOW_MS,
    )
    assert TRANSCRIPT_MERGE_WINDOW_MS < TURN_CONTINUATION_HOLD_MS
    assert TURN_CONTINUATION_HOLD_MS <= MERGE_HOLD_CONTINUATION_MS


# ── The kill switch ────────────────────────────────────────────────────────

def test_the_feature_can_be_switched_off_wholesale(monkeypatch):
    import app.voice.pipeline as mod
    monkeypatch.setattr(mod, "TURN_PAUSE_TOLERANCE", False)
    p = _pipe(_caller_split_turns=3)
    assert p._pause_may_continue(_t("a long narrative", 9000)) == ""


def test_split_memory_alone_can_be_switched_off(monkeypatch):
    import app.voice.pipeline as mod
    monkeypatch.setattr(mod, "TURN_SPLIT_MEMORY", False)
    p = _pipe(_caller_split_turns=3)
    # The speaker habit stops counting…
    assert p._pause_may_continue(_t("short", 1000)) == ""
    # …but a narrative segment is still a narrative.
    assert p._pause_may_continue(_t("long", 9000)) == "narrative"


def test_note_split_is_inert_when_memory_is_off(monkeypatch):
    import app.voice.pipeline as mod
    monkeypatch.setattr(mod, "TURN_SPLIT_MEMORY", False)
    p = _pipe()
    p._note_caller_split("rollback merge")
    assert p._caller_split_turns == 0


# ── The live call, replayed ────────────────────────────────────────────────

def test_call_871_would_have_held_instead_of_interrupting():
    """The three turns that went wrong, in order.

    Turn 1 is the one the old code fast-pathed with zero hold. It is under
    the narrative bar, so on its own it still would — but the caller had
    ALREADY split twice by then (05:32:05 and 05:32:29 both logged
    [CONT-MERGE]), and that is what arms the hold.
    """
    p = _pipe()
    p._note_caller_split("rollback merge")        # 05:32:05 CONT-MERGE
    p._note_caller_split("rollback merge")        # 05:32:29 CONT-MERGE

    # 05:32:46 — the turn that was answered too early.
    assert p._pause_may_continue(
        _t("yeah, so basically i work as an artificial intelligence "
           "engineer.", 2260)) == "splitter"

    # 05:32:52 — the tail that was absorbed and never answered.
    assert p._pause_may_continue(
        _t("so i've had experience working on ai, machine learning, and my "
           "tech stack basically is python.", 6460)) == "narrative"

    # 05:33:08 — still held, still the same caller.
    assert p._pause_may_continue(
        _t("also, i have interned in all", 1900)) == "splitter"


def test_a_pramukh_style_call_never_arms_the_hold():
    """Short-answer flows keep today's latency end to end — no split ever
    happens, so nothing ever holds."""
    p = _pipe()
    for answer, ms in [("vapi", 700), ("3 bhk", 900), ("50 lakh", 1100),
                       ("yes", 500), ("tomorrow evening", 1400)]:
        assert p._pause_may_continue(_t(answer, ms)) == ""
    assert p._caller_split_turns == 0


# ── A repair probe is never a continuation ─────────────────────────────────

def test_a_bare_hello_never_pays_the_hold():
    """Even from a caller who splits everything else. "hello?" means they
    think the line is dead — holding adds the exact silence they are
    probing, which is why the fragment hold already exempts it."""
    from app.voice.pipeline import _is_self_contained_greeting
    for probe in ("hello", "hello?", "hello hello", "नमस्ते", "हेलो"):
        assert _is_self_contained_greeting(probe) is True


def test_a_real_utterance_is_not_mistaken_for_a_probe():
    from app.voice.pipeline import _is_self_contained_greeting
    assert _is_self_contained_greeting(
        "hello, this is piyush gupta") is False


# ── Delivery: one sentence, one audio file (2026-09-16) ────────────────────
#
# Live call 882: the agent's opening went out as FOUR playback files —
# "Thank you for confirming Mr." / "Piyush." / "This is Aakash from the Dhron
# AI hiring team." / "We received your application…" — because the stream
# chunker breaks after ANY full stop and {title} puts "Mr." in nearly every
# addressed turn. Each file carries the bridge's ~100ms pacing gap, and the
# 0.07s one was rejected by the sink validator and never heard at all.

import re as _re
from app.voice.conversation import _ABBREV_DOT_RE

_SENT_END = _re.compile(r'[.!?।]\s')


def _next_break(buf):
    pos = 0
    while True:
        m = _SENT_END.search(buf, pos)
        if m is None:
            return None
        if buf[m.start()] != "." or not _ABBREV_DOT_RE.search(buf[:m.start() + 1]):
            return m
        pos = m.end()


def _chunks(t):
    out, buf = [], t
    while True:
        m = _next_break(buf)
        if not m:
            break
        out.append(buf[:m.end()].strip())
        buf = buf[m.end():]
    if buf.strip():
        out.append(buf.strip())
    return out


def test_a_title_never_splits_the_name_off():
    assert _chunks("Thank you for confirming Mr. Piyush. This is Aakash.") == [
        "Thank you for confirming Mr. Piyush.", "This is Aakash."]


@pytest.mark.parametrize("text,first", [
    ("Right, Mrs. Sharma. What is your CTC?", "Right, Mrs. Sharma."),
    ("Noted, Dr. Rao. And your location?", "Noted, Dr. Rao."),
    ("We start at 10 a.m. sharp. Does that suit you?", "We start at 10 a.m. sharp."),
    ("That's Rs. 5 lakh. Correct?", "That's Rs. 5 lakh."),
])
def test_abbreviations_stay_with_their_sentence(text, first):
    assert _chunks(text)[0] == first


def test_real_sentence_boundaries_still_split():
    assert _chunks("That works. I'll note it down. Thanks!") == [
        "That works.", "I'll note it down.", "Thanks!"]


def test_a_short_fragment_is_padded_not_dropped():
    """_validate_wav rejects <100ms and the caller deleted the file, so those
    words were never spoken. Padding with silence is inaudible."""
    import asyncio, tempfile
    from app.audio_transport.telephony_sink import TelephonySink

    async def _go():
        with tempfile.TemporaryDirectory() as tmp:
            s = TelephonySink(sample_rate=8000, conn_id="t")
            s._tmp_dir, s._running = tmp, True
            await s.write_frame(b"\x11\x22" * 280)      # 70ms @ 8k
            await s.flush()
            assert s.wav_queue.qsize() == 1
    asyncio.run(_go())


def test_an_empty_buffer_is_still_not_written():
    import asyncio, tempfile
    from app.audio_transport.telephony_sink import TelephonySink

    async def _go():
        with tempfile.TemporaryDirectory() as tmp:
            s = TelephonySink(sample_rate=8000, conn_id="t")
            s._tmp_dir, s._running = tmp, True
            await s.flush()
            assert s.wav_queue.qsize() == 0
    asyncio.run(_go())
