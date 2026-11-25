"""Per-agent turn-taking (call_flow.turn_taking, RUNBOOK §78) + the two
platform fixes found on call b5452b23 (Vedanta test, 2026-10-12).

What that call showed, in order:
  * the pitch ended "…एक site visit..." — the 400-character reply cap cut a
    Hindi reply mid-word because the truncator knew "." but not "।";
  * the caller's one-word "हाँ" nine seconds into a 25 s pitch was a TURN:
    the stage advanced and the next question queued behind the pitch;
  * "हाँ, अभी बात कर सकते हैं" (yes, we can talk now) wrote
    purchase_timeline=Immediate through the ask gate, because the keyword
    scan credited the good-time line ("कुछ समय पहले…") to any slot with
    "time" in its NAME — an enum, not a clock;
  * the doubts question was stacked behind replies that already ended on a
    question of their own.
"""

from __future__ import annotations

import asyncio
import types

import pytest

from app.agent_definition import controls
from app.agent_definition.schema import AgentDefinition
from app.voice import pipeline as pl
from app.voice.conversation import (
    ConversationRouter,
    MAX_RESPONSE_LENGTH,
    _validate_response,
)
from app.voice.pipeline import VoicePipeline


@pytest.fixture(autouse=True)
def _offline(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


# ── a pipeline stand-in carrying the real methods ───────────────────────────

def _conv(cfg: dict | None = None):
    return types.SimpleNamespace(
        _turn_taking_cfg=lambda: dict(cfg or {}),
        _is_bare_ack_text=ConversationRouter._is_bare_ack_text,
        _is_bare_hello_text=ConversationRouter._is_bare_hello_text,
        _is_greeting_only_text=ConversationRouter._is_greeting_only_text,
    )


def _pipe(cfg: dict | None = None, **over):
    ns = types.SimpleNamespace(
        _caller_split_turns=0, _call_start_ts=0.0,
        conversation=_conv(cfg),
        sink=types.SimpleNamespace(playback_busy_until=0.0),
        _tts_playing=False, _last_reply_tts_start=0.0,
        _bargein_deferred_ts=0.0,
    )
    for k, v in over.items():
        setattr(ns, k, v)
    for name in ("_pause_may_continue", "_note_caller_split",
                 "_in_pickup_phase", "_tt_cfg", "_tt_num", "_tt_flag",
                 "_pause_hold_s", "_agent_audio_remaining", "_bare_ack",
                 "_bare_hello", "_greeting_only", "_is_backchannel",
                 "_tail_cut_due", "_deferred_barge_override_due"):
        setattr(ns, name, types.MethodType(getattr(VoicePipeline, name), ns))
    return ns


def _t(text: str, duration_ms: float = 800.0, start: float = 0.0):
    return types.SimpleNamespace(text=text, duration_ms=duration_ms,
                                 speech_start_ts=start)


# ── pause_hold_ms: every pause is held, by this agent only ──────────────────

def test_without_the_block_a_short_answer_keeps_the_fast_path():
    p = _pipe()
    assert p._pause_may_continue(_t("4 bhk", 900)) == ""
    assert p._pause_hold_s() == pl.TURN_CONTINUATION_HOLD_MS / 1000.0


def test_the_agents_pause_hold_applies_to_every_pause():
    p = _pipe({"pause_hold_ms": 1500})
    assert p._pause_may_continue(_t("4 bhk", 900)) == "agent"
    assert p._pause_may_continue(_t("हाँ, मुझे Aristo में interest है", 2000)) == "agent"
    assert p._pause_hold_s() == 1.5


def test_a_hold_shorter_than_the_platforms_never_shortens_it():
    p = _pipe({"pause_hold_ms": 300})
    assert p._pause_hold_s() == pl.TURN_CONTINUATION_HOLD_MS / 1000.0


def test_a_bad_value_is_ignored():
    p = _pipe({"pause_hold_ms": "lots"})
    assert p._pause_may_continue(_t("4 bhk", 900)) == ""


# ── a bare acknowledgement ──────────────────────────────────────────────────

@pytest.mark.parametrize("text", [
    "हाँ", "हाँ।", "जी हाँ", "हम्म", "हम्म।", "ओके", "ठीक है", "अच्छा", "जी",
    "okay", "okay sir", "yes yes", "हाँ जी सर", "right", "सही है",
])
def test_these_are_acknowledgements(text):
    assert ConversationRouter._is_bare_ack_text(text) is True


@pytest.mark.parametrize("text", [
    "हाँ, मुझे 4 bhk चाहिए", "नहीं", "हाँ नहीं", "hello", "हेलो", "",
    "ठीक है, Wednesday को", "हाँ, अभी बात कर सकते हैं", "4 bhk",
    "जी हाँ सर बिल्कुल ठीक है",     # five tokens — a sentence, not a nod
])
def test_these_are_not(text):
    assert ConversationRouter._is_bare_ack_text(text) is False


# ── backchannel_guard: "हाँ" mid-pitch is not a turn ────────────────────────

def test_a_nod_while_the_pitch_still_plays_is_absorbed():
    """Call b5452b23, 02:34: "हाँ" with ~13 s of the pitch left to play."""
    p = _pipe({"backchannel_guard": True}, _last_reply_tts_start=100.0)
    p.sink.playback_busy_until = 122.0
    assert p._is_backchannel(_t("हाँ।", start=109.0)) is True


def test_the_same_nod_as_the_line_ends_is_an_answer():
    """0.4 s of audio left — they are answering a yes/no question."""
    p = _pipe({"backchannel_guard": True}, _last_reply_tts_start=100.0)
    p.sink.playback_busy_until = 109.4
    assert p._is_backchannel(_t("हाँ।", start=109.0)) is False


def test_real_words_are_never_a_backchannel():
    p = _pipe({"backchannel_guard": True}, _last_reply_tts_start=100.0)
    p.sink.playback_busy_until = 122.0
    assert p._is_backchannel(_t("हाँ, मुझे 4 bhk चाहिए", start=109.0)) is False


def test_a_nod_spoken_before_our_reply_started_is_not_ours_to_drop():
    p = _pipe({"backchannel_guard": True}, _last_reply_tts_start=110.0)
    p.sink.playback_busy_until = 122.0
    assert p._is_backchannel(_t("हाँ।", start=109.0)) is False


def test_the_guard_is_off_without_the_block():
    p = _pipe({}, _last_reply_tts_start=100.0)
    p.sink.playback_busy_until = 122.0
    assert p._is_backchannel(_t("हाँ।", start=109.0)) is False


def test_a_sink_without_a_playback_clock_never_absorbs():
    p = _pipe({"backchannel_guard": True}, _last_reply_tts_start=100.0,
              sink=types.SimpleNamespace())
    assert p._is_backchannel(_t("हाँ।", start=109.0)) is False


# ── tail_barge_in: real speech stops audio that is still playing ────────────

def test_real_speech_in_the_tail_cuts_it(monkeypatch):
    monkeypatch.setattr(pl.time, "monotonic", lambda: 110.0)
    p = _pipe({"tail_barge_in": True})
    p.sink.playback_busy_until = 118.0
    assert p._tail_cut_due(_t("मुझे 4 bhk चाहिए")) is True


def test_a_nod_or_a_hello_never_cuts(monkeypatch):
    monkeypatch.setattr(pl.time, "monotonic", lambda: 110.0)
    p = _pipe({"tail_barge_in": True})
    p.sink.playback_busy_until = 118.0
    assert p._tail_cut_due(_t("हाँ")) is False
    assert p._tail_cut_due(_t("हेलो")) is False


def test_nothing_to_cut_once_the_audio_is_nearly_over(monkeypatch):
    monkeypatch.setattr(pl.time, "monotonic", lambda: 110.0)
    p = _pipe({"tail_barge_in": True})
    p.sink.playback_busy_until = 110.5
    assert p._tail_cut_due(_t("मुझे 4 bhk चाहिए")) is False


def test_while_synthesis_is_live_the_barge_monitor_owns_it(monkeypatch):
    monkeypatch.setattr(pl.time, "monotonic", lambda: 110.0)
    p = _pipe({"tail_barge_in": True}, _tts_playing=True)
    p.sink.playback_busy_until = 118.0
    assert p._tail_cut_due(_t("मुझे 4 bhk चाहिए")) is False


# ── sustained_barge_ms: a deferred barge-in is carried out eventually ───────

def test_a_deferral_is_overridden_after_sustained_speech():
    p = _pipe({"sustained_barge_ms": 700}, _bargein_deferred_ts=50.0)
    assert p._deferred_barge_override_due(50.5) is False
    assert p._deferred_barge_override_due(50.7) is True


def test_no_override_by_default():
    p = _pipe({}, _bargein_deferred_ts=50.0)
    assert p._deferred_barge_override_due(60.0) is False


# ── the schema carries the block; the control surface knows its keys ────────

def test_from_dict_carries_turn_taking_and_the_keys_are_known():
    d = {"agent_id": "t", "version": "1.0.0",
         "identity": {"agent_name": "A", "company_name": "B"},
         "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {}},
         "call_flow": {"stages": [{"id": "S", "order": 0, "goal": "g"}],
                       "turn_taking": {"pause_hold_ms": 1500,
                                       "backchannel_guard": True}},
         "response_bank": {"opening_greeting": "hi"}}
    agent = AgentDefinition.from_dict(d)
    assert agent.call_flow.turn_taking == {"pause_hold_ms": 1500,
                                           "backchannel_guard": True}
    paths = ["call_flow.turn_taking.pause_hold_ms",
             "call_flow.turn_taking.max_merged_fragments",
             "call_flow.turn_taking.backchannel_guard",
             "call_flow.turn_taking.sustained_barge_ms",
             "call_flow.turn_taking.tail_barge_in"]
    assert controls.unknown_paths(paths) == []
    assert controls.unknown_paths(["call_flow.turn_taking.hold_forever"])


def test_an_agent_without_the_block_reads_empty():
    d = {"agent_id": "t", "version": "1.0.0",
         "identity": {"agent_name": "A", "company_name": "B"},
         "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {}},
         "call_flow": {"stages": [{"id": "S", "order": 0, "goal": "g"}]},
         "response_bank": {"opening_greeting": "hi"}}
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    assert r._turn_taking_cfg() == {}


# ── platform fix 1: the daṇḍa is a sentence end ─────────────────────────────

def test_a_long_hindi_reply_is_cut_at_a_sentence_not_mid_word():
    sentence = "इस project की amenities में आपको gymnasium और café मिल जाएगा।"
    text = " ".join([sentence] * 12)          # ~700 chars, "।" boundaries only
    assert len(text) > MAX_RESPONSE_LENGTH
    out = _validate_response(text, "hi")
    assert out.endswith("।")
    assert "..." not in out
    assert len(out) <= MAX_RESPONSE_LENGTH


def test_an_english_reply_still_cuts_at_its_full_stop():
    sentence = "The project has a gymnasium, a library and a cafe for residents."
    out = _validate_response(" ".join([sentence] * 10), "en")
    assert out.endswith(".")
    assert "..." not in out


def test_the_agents_own_cap_is_honoured_above_the_platform_floor():
    sentence = "इस project की amenities में आपको gymnasium और café मिल जाएगा।"
    text = " ".join([sentence] * 12)
    out = _validate_response(text, "hi", max_len=760)
    assert len(out) > MAX_RESPONSE_LENGTH
    assert out.endswith("।")


# ── platform fix 2: "time" in a slot's NAME is not a clock ──────────────────

def _router(slots):
    d = {"agent_id": "t_tt", "version": "1.0.0",
         "identity": {"agent_name": "Ravi", "company_name": "Pramukh"},
         "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                       "evidence_switch": False, "auto_switch": False},
         "call_flow": {"stages": [
             {"id": "S1", "order": 0, "goal": "one", "collect": [],
              "turns_until_advance": 9}]},
         "slots": list(slots),
         "response_bank": {"opening_greeting": "hi"}}
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.language = "hi"
    return r


def _slot(name, typ, enum=None, label=None):
    return {"name": name, "priority": 1, "required": True,
            "label": label or name, "question": {"hi": f"{name}?"},
            "type": typ, "enum_values": enum or [], "enum_synonyms": {},
            "extract_patterns": [], "depends_on": [], "merge_policy": "free",
            "confirm_readback": "", "max_asks": 2, "reask_question": {},
            "summary_known": "", "summary_unknown": "", "ask_gated": False}


GOOD_TIME = ("शुक्रिया! मैं रवि बोल रहा हूँ — आपने कुछ समय पहले हमारे project "
             "में interest दिखाया था, अभी दो मिनट बात करना ठीक रहेगा?")


def test_the_good_time_line_does_not_ask_an_enum_timeline():
    r = _router([_slot("purchase_timeline", "enum",
                       ["Immediate", "Within 6 months"], label="Timeline")])
    r._note_ask_signals(GOOD_TIME)
    assert r._last_asked_slot is None


def test_a_real_time_slot_is_still_credited_by_a_clock_word():
    r = _router([_slot("visit_time", "time", label="Visit time")])
    r._note_ask_signals("आप किस समय आना चाहेंगे?")
    assert r._last_asked_slot == "visit_time"


def test_a_free_text_slot_named_for_time_keeps_the_old_reading():
    r = _router([_slot("callback_time", "text", label="Callback time")])
    r._note_ask_signals("आपको किस समय call करूँ?")
    assert r._last_asked_slot == "callback_time"


# ── the script floor: no doubts question behind a question ──────────────────

Q_DOUBTS = ("क्या आपके पास इस property के बारे में और कोई सवाल या doubt है, "
            "जिसमें मैं आपकी मदद कर सकता हूँ?")


def _scripted_router():
    d = {"agent_id": "t_floor", "version": "1.0.0",
         "identity": {"agent_name": "Ravi", "company_name": "Pramukh"},
         "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                       "evidence_switch": False, "auto_switch": False},
         "call_flow": {"stages": [
             {"id": "KNOW_MORE", "order": 0, "goal": "answer", "collect": [],
              "turns_until_advance": 9,
              "script": {"after_answer": {"hi": Q_DOUBTS}}}]},
         "response_bank": {"opening_greeting": "hi"}}
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.language = "hi"
    r.state.stage = "KNOW_MORE"
    return r


def test_the_doubts_question_follows_an_answer():
    r = _scripted_router()
    assert Q_DOUBTS in r._script_followup(
        "Pramukh Aristo में आपको 3 BHK करीब ₹90 लाख में मिलता है।")


def test_but_never_a_reply_that_already_ends_on_a_question():
    """Call b5452b23, 03:22: "…आप अपना नया budget क्या रखना चाहते हैं? क्या
    आपके पास इस property के बारे में और कोई सवाल…" — two questions."""
    r = _scripted_router()
    assert r._script_followup(
        "बिल्कुल, बताइए — आप अपना नया budget क्या रखना चाहते हैं?") == ""


def test_the_floor_still_recognises_its_own_line():
    r = _scripted_router()
    assert r._script_followup("ज़रूर। " + Q_DOUBTS) == ""


# ── a bare "हम्म" to a scripted question: the question again, verbatim ─────

Q_FACTOR = ("आपके लिए property select करते वक़्त सबसे important factor क्या है — "
            "location, size, amenities, construction quality या investment value?")


def _ask_verbatim_router():
    slot = {"name": "decision_factor", "priority": 1, "required": True,
            "label": "What matters most", "question": {"hi": Q_FACTOR},
            "type": "enum", "enum_values": ["Location", "Size"],
            "enum_synonyms": {"Location": ["location", "लोकेशन"],
                              "Size": ["size", "साइज"]},
            "extract_patterns": [], "depends_on": [], "merge_policy": "free",
            "confirm_readback": "", "max_asks": 2, "reask_question": {},
            "summary_known": "", "summary_unknown": "", "ask_gated": True}
    d = {"agent_id": "t_reask", "version": "1.0.0",
         "identity": {"agent_name": "Ravi", "company_name": "Pramukh"},
         "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                       "evidence_switch": False, "auto_switch": False},
         "call_flow": {"stages": [
             {"id": "UNDERSTAND", "order": 0, "goal": "ask",
              "collect": ["decision_factor"], "turns_until_advance": 9,
              "advance_when": {"all_collect_filled": True, "max_turns": 9},
              "script": {"ask_verbatim": True}}]},
         "slots": [slot],
         "response_bank": {"opening_greeting": "hi"}}
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.language = "hi"
    r.state.stage = "UNDERSTAND"
    r.state.turn_count = 4
    r.state_mgr.turn_index = 4
    r.state_mgr.note_asked("decision_factor")
    r.last_agent_response = "बढ़िया। " + Q_FACTOR
    return r


def test_a_filler_gets_the_question_again_not_an_answer_to_it():
    r = _ask_verbatim_router()
    assert r._scripted_filler_reask("हम्म") == Q_FACTOR
    assert r._scripted_filler_reask("अच्छा") == Q_FACTOR


def test_an_answer_or_a_question_back_goes_to_the_model():
    r = _ask_verbatim_router()
    assert r._scripted_filler_reask("लोकेशन और साइज") == ""
    assert r._scripted_filler_reask("आप कौन से project से बोल रहे हैं?") == ""


def test_no_reask_once_the_slot_is_filled_or_the_line_asked_nothing():
    r = _ask_verbatim_router()
    r.state_mgr.state["decision_factor"] = "Location"
    assert r._scripted_filler_reask("हम्म") == ""
    r = _ask_verbatim_router()
    r.last_agent_response = "ठीक है, समझ गया।"
    assert r._scripted_filler_reask("हम्म") == ""


def test_the_reask_is_spoken_through_the_engine_and_counts_as_the_second_ask():
    r = _ask_verbatim_router()
    out = []

    async def _one():
        async for res in r.process_stream("हम्म"):
            if getattr(res, "text", ""):
                out.append(res.text)
    asyncio.run(_one())
    assert Q_FACTOR in " ".join(out)
    assert r.state_mgr.asked_count["decision_factor"] == 2


# ── a correction in Hindi letters is a correction ───────────────────────────

def _money_router():
    slot = {"name": "budget_range", "priority": 1, "required": True,
            "label": "Budget", "question": {"hi": "budget?"}, "type": "money",
            "enum_values": [], "enum_synonyms": {}, "extract_patterns": [],
            "depends_on": [], "merge_policy": "correction_gated",
            "confirm_readback": "", "max_asks": 2, "reask_question": {},
            "summary_known": "", "summary_unknown": "", "ask_gated": False}
    return _router([slot])


def test_a_stored_budget_changes_on_a_devanagari_change():
    """Sim on revision 10: "मेरा बजट चेंज करके 70 लाख से 90 लाख कर दो" left
    budget_range at '2 Crore'."""
    r = _money_router()
    sm = r.state_mgr
    sm.observe_user("मेरा एक से दो करोड़ का")
    assert sm.state["budget_range"] is not None
    before = sm.state["budget_range"]
    sm.observe_user("मेरा बजट चेंज करके 70 लाख से 90 लाख कर दो")
    assert sm.state["budget_range"] != before


def test_quoting_the_agents_price_is_not_a_correction():
    r = _money_router()
    sm = r.state_mgr
    sm.observe_user("मेरा एक से दो करोड़ का")
    before = sm.state["budget_range"]
    sm.observe_user("यह जो आपने price बोला 3 bhk का ₹70 लाख टू ₹85 लाख, यह किसका है?")
    assert sm.state["budget_range"] == before


# ── the audit transcript says what the engine heard ─────────────────────────

def _audit(tmp_path, monkeypatch):
    import call_audit
    monkeypatch.setattr(call_audit, "RECORDINGS_ROOT", str(tmp_path))
    s = call_audit.CallAuditSession("t-call", caller_number="919999999999")
    return s


def _lines(s):
    s._txt_file.flush()
    text = open(s._txt_path, encoding="utf-8").read()
    return [ln for ln in text.splitlines() if ln.startswith(("Caller:", "Agent:"))]


def test_the_pieces_of_a_split_turn_are_replaced_by_the_joined_text(tmp_path, monkeypatch):
    """Call b5452b23, 05:46: the file said "हाँ, मुझे प्रमुख परिष्टों में
    इंटरेस्ट है।"; the engine read the joined pass with "फोर बीएच के"."""
    s = _audit(tmp_path, monkeypatch)
    s.add_event("Agent", "क्या आपके पास कोई सवाल है?")
    s.add_event("Caller", "हाँ, मुझे प्रमुख परिष्टों में इंटरेस्ट है।", span=(1.0, 3.0))
    s.add_event("Caller", "और भी अच्छे।", span=(3.1, 4.0))
    ok = s.replace_last_events(
        "Caller", ["हाँ, मुझे प्रमुख परिष्टों में इंटरेस्ट है।", "और भी अच्छे।"],
        "हाँ, मुझे प्रमुख Aristo में इंटरेस्ट है, फोर बीएच के।", span=(1.0, 4.0))
    assert ok is True
    assert _lines(s) == ["Agent: क्या आपके पास कोई सवाल है?",
                         "Caller: हाँ, मुझे प्रमुख Aristo में इंटरेस्ट है, फोर बीएच के।"]
    assert "[spoke 1.00-4.00s]" in open(s._txt_path, encoding="utf-8").read()


def test_an_entry_written_since_makes_the_replacement_a_no_op(tmp_path, monkeypatch):
    s = _audit(tmp_path, monkeypatch)
    s.add_event("Caller", "पंजे")
    s.add_event("Caller", "करेक्ट, करेक्ट।")
    s.add_event("Agent", "ठीक है जी।")
    assert s.replace_last_events("Caller", ["पंजे", "करेक्ट, करेक्ट।"],
                                 "हां जी। करेक्ट करेक्ट।") is False
    assert _lines(s)[-1] == "Agent: ठीक है जी।"


def test_pieces_that_do_not_match_are_left_alone(tmp_path, monkeypatch):
    s = _audit(tmp_path, monkeypatch)
    s.add_event("Caller", "एक")
    s.add_event("Caller", "दो")
    assert s.replace_last_events("Caller", ["एक", "तीन"], "एक तीन") is False
    assert _lines(s) == ["Caller: एक", "Caller: दो"]


def test_a_retraction_and_a_replacement_keep_their_books_straight(tmp_path, monkeypatch):
    s = _audit(tmp_path, monkeypatch)
    s.add_event("Caller", "हाँ")
    s.add_event("Agent", "cancelled reply")
    assert s.drop_last_event("Agent", "cancelled reply") is True
    s.add_event("Caller", "4 bhk")
    assert s.replace_last_events("Caller", ["हाँ", "4 bhk"], "हाँ 4 bhk") is True
    assert _lines(s) == ["Caller: हाँ 4 bhk"]


# ── RUNBOOK §79: call 64629614 ──────────────────────────────────────────────

from app.voice.conversation import detect_intent
from app.voice import prompt_blocks as pb


def _invite_router():
    d = {"agent_id": "t_neg", "version": "1.0.0",
         "identity": {"agent_name": "Ravi", "company_name": "Pramukh Vedanta"},
         "languages": {"supported": ["hi"], "default": "hi", "tts_voices": {},
                       "evidence_switch": False, "auto_switch": False},
         "call_flow": {"decline_threshold": 2, "stages": [
             {"id": "INVITE_SITE_VISIT", "order": 0, "goal": "invite",
              "role": "scheduling", "collect": [], "turns_until_advance": 9}]},
         "knowledge_base": {"catalog": [
             {"id": "pramukh-vedanta", "name": "Pramukh Vedanta",
              "project_type": "residential", "location": "New Chala, Vapi"},
             {"id": "pramukh-green-county", "name": "Pramukh Green County",
              "project_type": "residential", "location": "Chharwada, Vapi"}]},
         "response_bank": {"opening_greeting": "hi"}}
    r = ConversationRouter(agent_def=AgentDefinition.from_dict(d),
                           default_language="hi")
    r.state.language = "hi"
    r.state.stage = "INVITE_SITE_VISIT"
    r.state.turn_count = 6
    r.last_agent_response = ("बिल्कुल सर, मैं कल यानी मंगलवार 13 October को "
                             "दोपहर 2 बजे की site visit fix कर देता हूँ।")
    return r


NEG_1 = "कल की मत करो, उसके बाद के बाद का कुछ करो।"
NEG_2 = "नहीं, मैं बोल रहा हूँ कल का मत करो, उसके अलावा कोई डेट का फिक्स कर लो।"


def test_the_two_sentences_still_read_as_objections_to_the_classifier():
    assert detect_intent(NEG_1) == "objection"
    assert detect_intent(NEG_2) == "objection"


def test_but_they_are_a_visit_negotiation_when_a_day_was_proposed():
    r = _invite_router()
    assert r._is_visit_negotiation(NEG_1) is True
    assert r._is_visit_negotiation(NEG_2) is True
    assert r._is_visit_negotiation("परसों शाम 5 बजे आ सकता हूँ") is True


def test_a_real_decline_or_a_callback_is_not_a_negotiation():
    r = _invite_router()
    assert r._is_visit_negotiation("नहीं भाई, मुझे नहीं आना") is False
    assert r._is_visit_negotiation("मुझे इंटरेस्ट नहीं है") is False
    assert r._is_visit_negotiation("10 मिनट बाद कॉल कर सकते हो?") is False


def test_nothing_on_the_table_means_the_objection_stands():
    r = _invite_router()
    r.state.stage = "INVITE_SITE_VISIT"
    r._def.call_flow.stages[0].role = "discovery"
    r.last_agent_response = "आपका budget क्या है?"
    assert r._is_visit_negotiation(NEG_1) is False


def test_two_negotiations_in_a_row_never_close_the_call():
    """The live call: strike one got the canned deferral, strike two the
    escalation handoff and a hangup."""
    r = _invite_router()
    for line in (NEG_1, NEG_2):
        drive(r, line)
    assert r.state.decline_count == 0
    assert r.hangup_pending is False
    assert r.state.is_closing is False
    assert r._visit_negotiation_turn >= 0
    assert any("NEGOTIATED" in l for l in r._visit_negotiation_band())


def drive(r, text):
    async def _one():
        return [res async for res in r.process_stream(text)]
    return asyncio.run(_one())


def test_because_is_not_why():
    assert detect_intent("इन्वेस्टमेंट वाली क्योंकि मैं इन्वेस्टमेंट के लिए देख रहा हूँ।") != "question"
    assert detect_intent("आप क्यों कॉल कर रहे हो?") == "question"


def test_the_project_the_caller_named_is_the_one_answered():
    r = _invite_router()
    r._turn_caller_text = "3 bhk, 4 bhk प्रमुख Vedanta में क्या-क्या मिलता है?"
    r.last_agent_response = "Pramukh Green County में 3 BHK का carpet area 1,242 sq ft है।"
    band = r._project_focus_band()
    assert any("named Pramukh Vedanta" in l for l in band)
    assert not any("Green County" in l for l in band)


def test_with_no_name_the_previous_lines_projects_are_the_topic():
    r = _invite_router()
    r._turn_caller_text = "दोनों का carpet area बता दो"
    r.last_agent_response = ("हमारे पास Pramukh Green County और Pramukh Vedanta "
                             "हैं जो इस budget में फिट बैठते हैं")
    band = r._project_focus_band()
    assert any("Pramukh Vedanta" in l and "Pramukh Green County" in l
               and "previous line" in l for l in band)


def test_no_project_anywhere_means_no_band():
    r = _invite_router()
    r._turn_caller_text = "ठीक है"
    r.last_agent_response = "आपका budget क्या है?"
    assert r._project_focus_band() == []


def test_the_floor_never_stacks_the_slot_question_behind_another_question():
    r = _ask_verbatim_router()
    assert r._script_followup(
        "Pramukh Vedanta उस बजट से ऊपर है, क्या आप दूसरे projects देखना चाहेंगे?") == ""
    assert Q_FACTOR in r._script_followup("ठीक है, समझ गया।")


def test_the_tail_cut_never_fires_on_a_pickup_greeting(monkeypatch):
    monkeypatch.setattr(pl.time, "monotonic", lambda: 110.0)
    p = _pipe({"tail_barge_in": True}, _call_start_ts=0.0)
    p.sink.playback_busy_until = 118.0
    assert p._tail_cut_due(_t("हेलो नमस्ते")) is False
    p._last_reply_was_onset = True
    assert p._tail_cut_due(_t("मुझे 4 bhk चाहिए")) is False
    p._last_reply_was_onset = False
    p._call_start_ts = 105.0            # 5 s into the call: connect window
    assert p._tail_cut_due(_t("मुझे 4 bhk चाहिए")) is False
