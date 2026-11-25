"""Call 909 (Vikrant → Ravi, Pramukh, 2026-09-24; RUNBOOK §55.5): the end of
the call was a loop. The visit was fixed at 11:54:21 and the agent then said
"Saturday 26 September, 5 PM, confirmed" on five consecutive turns; it opened
or closed eleven of fourteen replies with the caller's name; the model tried
to end the call three times and the engine ignored it every time; and after
a long pitch the dead-air repair replayed the whole pitch behind "I couldn't
hear you" — twice.

Four small guards, all offline:
  * visit_confirmed_block — once a booking has been read back, don't again;
  * name_fatigue_block    — a name said in the last replies is not said now;
  * build_tool_declarations(include_book=False) — the booking tool is
    withdrawn once it can only be refused, so the closing turns are text
    turns and the goodbye is heard by the engine;
  * the silence re-prompt asks "are you still there?" instead of replaying
    a line longer than 120 characters.
"""

from __future__ import annotations

from app.voice import prompt_blocks as pb
from app.voice.llm_tools import build_tool_declarations
from app.voice.memory import ConversationMemory


# ---------------------------------------------------------------------------
# visit_confirmed_block
# ---------------------------------------------------------------------------

READBACK_HI = "बहुत बढ़िया, तो इस शनिवार यानी 26 सितंबर को शाम 5:00 बजे का समय पक्का रहा।"
READBACK_EN = "Lovely — Saturday the 26th at 5 pm it is."


def test_no_band_before_the_first_readback():
    lines = ["Shall we say Saturday evening, or Sunday morning?"]
    assert pb.visit_confirmed_block("2026-09-26", "17:00", lines) == []


def test_band_after_a_hindi_readback():
    text = "\n".join(pb.visit_confirmed_block("2026-09-26", "17:00", [READBACK_HI]))
    assert "━━ VISIT ALREADY CONFIRMED ━━" in text
    assert "Do NOT say the day, date or time again" in text
    assert "end the call with [END_CALL]" in text


def test_band_after_an_english_readback_and_without_llm_end():
    text = "\n".join(pb.visit_confirmed_block("2026-09-26", "17:00", [READBACK_EN],
                                             llm_end=False))
    assert "VISIT ALREADY CONFIRMED" in text
    assert "[END_CALL]" not in text


def test_a_line_with_only_the_day_number_is_not_a_readback():
    """'26' alone could be anything (a price, an age); both tokens are needed."""
    assert pb.visit_confirmed_block("2026-09-26", "17:00",
                                    ["We have 26 bungalows left."]) == []
    assert pb.visit_confirmed_block("2026-09-26", "17:00",
                                    ["Around 5 pm the light is lovely there."]) == []


def test_only_the_last_three_lines_count():
    old = [READBACK_HI, "ok", "sure", "great"]
    assert pb.visit_confirmed_block("2026-09-26", "17:00", old) == []


def test_no_booking_no_band():
    assert pb.visit_confirmed_block(None, None, [READBACK_HI]) == []
    assert pb.visit_confirmed_block("2026-09-26", "", [READBACK_HI]) == []


# ---------------------------------------------------------------------------
# name_fatigue_block
# ---------------------------------------------------------------------------

def test_latin_name_twice_in_three_lines_triggers():
    lines = ["Sure, Alvin.", "What size are you after?", "Got it, Alvin — and when?"]
    text = "\n".join(pb.name_fatigue_block("Alvin Antony", lines))
    assert "━━ THEIR NAME ━━" in text
    assert "Do NOT say it this turn" in text


def test_devanagari_vocative_is_matched_by_shape():
    """The model transliterates the name; the string compare cannot see
    'अल्विं' in 'Alvin'. The '<name> जी,' opener and ', <name> जी।' closer are
    matched by shape (call 909: eleven of fourteen replies)."""
    lines = ["अल्विं जी, आपसे बात करके अच्छा लगा।",
             "यह घर आप ख़ुद रहने के लिए देख रहे हैं?",
             "बिल्कुल, शनिवार को मिलते हैं, अल्विं जी।"]
    assert pb.name_fatigue_block("Alvin", lines)


def test_once_is_fine():
    lines = ["Sure, Alvin.", "What size?", "And when are you looking to move?"]
    assert pb.name_fatigue_block("Alvin", lines) == []


def test_no_name_no_band():
    assert pb.name_fatigue_block(None, ["Alvin, Alvin, Alvin"]) == []
    assert pb.name_fatigue_block("Alvin", []) == []


def test_ji_inside_a_sentence_is_not_a_vocative():
    lines = ["हाँ जी, बिल्कुल।", "जी, समझ गया।", "ठीक है।"]
    assert pb.name_fatigue_block("Alvin", lines) == []


# ---------------------------------------------------------------------------
# memory helper
# ---------------------------------------------------------------------------

def test_recent_assistant_texts_returns_agent_lines_oldest_first():
    m = ConversationMemory()
    m.add_user("hi"); m.add_assistant("one")
    m.add_user("x"); m.add_assistant("two")
    m.add_user("y"); m.add_assistant("three")
    m.add_user("z"); m.add_assistant("four")
    assert m.recent_assistant_texts(3) == ["two", "three", "four"]
    assert m.recent_assistant_texts(0) == []


# ---------------------------------------------------------------------------
# tool roster
# ---------------------------------------------------------------------------

def test_booking_tool_is_withdrawn_on_request():
    names = lambda ds: [d["function"]["name"] for d in ds]
    full = names(build_tool_declarations(["budget_range"], {}))
    assert "book_appointment" in full and "record_customer_details" in full
    no_book = names(build_tool_declarations(["budget_range"], {}, include_book=False))
    assert "book_appointment" not in no_book
    assert "record_customer_details" in no_book
    neither = names(build_tool_declarations(["budget_range"], {},
                                            include_record=False, include_book=False))
    assert "book_appointment" not in neither and "record_customer_details" not in neither
    # the rest of the roster is untouched
    assert set(full) - {"book_appointment", "record_customer_details"} == set(neither)


# ---------------------------------------------------------------------------
# dead-air repair after a long line
# ---------------------------------------------------------------------------

def test_silence_reprompt_does_not_replay_a_long_pitch(stub_llm):
    from app.agent_definition.schema import AgentDefinition
    from app.voice.conversation import ConversationRouter, _STILL_THERE
    agent = AgentDefinition.from_dict({
        "agent_id": "t_reprompt", "version": "1.0.0",
        "identity": {"agent_name": "Ravi", "company_name": "Pramukh Group"},
        "languages": {"supported": ["hi", "en"], "default": "hi", "tts_voices": {}},
        "call_flow": {"stages": []},
        "knowledge_base": {"known_projects": {}, "locations": [], "portfolio_prompt_text": ""},
        "response_bank": {"opening_greeting": "Hello"},
        "slots": [],
    })
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.state.language = "hi"
    r.state.turn_count = 3
    pitch = ("Vapi में flats के अलावा अगर आप चाहें तो New Chala में हमारे flagship "
             "Pramukh Swarit bungalows, जो 4 और 5 BHK ready-to-move हैं, उनके बारे में "
             "भी बता सकता हूँ, क्या आप उनके बारे में जानना चाहेंगे?")
    assert len(pitch) > 120
    r._store_last_response(pitch)
    text = r._generate_silence_reprompt_raw()
    assert _STILL_THERE["hi"] in text
    assert "Swarit" not in text
    # a short question is still repeated, as before
    r._store_last_response("आप कितने लोग हैं?")
    assert "आप कितने लोग हैं?" in r._generate_silence_reprompt_raw()
