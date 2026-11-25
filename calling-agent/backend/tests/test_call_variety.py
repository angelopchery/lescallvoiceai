"""Per-call variety, and three "is it listening?" repairs (RUNBOOK §51).

Reported 2026-09-18 on the Bhavna Patel agent, after two real test calls:
"it's the same script every time I call someone" and "it talks like a robot —
I don't feel like I am talking to a human".

Offline: no LLM calls.
"""

import asyncio

import pytest

from app.agent_definition.schema import AgentDefinition
from app.agent_definition.spoken_surfaces import resolve_spoken_surfaces
from app.voice.conversation import ConversationRouter
from app.voice.llm_tools import ToolProposal

OPENING = "Namaste, main Bhavna bol rahi hoon. Ek chhota sa sawal tha?"
VARIANTS = ["Hello ji, Bhavna this side. Ek minute hai aapke paas?",
            "Namaste! Bhavna bol rahi hoon — ek quick cheez poochni thi?"]
STYLES = ["Lead with curiosity about their trade.",
          "Be brisk — they are busy people."]


def _agent(**over) -> AgentDefinition:
    d = {
        "agent_id": "t_variety", "version": "1.0.0",
        "identity": {"agent_name": "Bhavna", "company_name": "TestCo"},
        # A female voice, or the engine (rightly) re-genders "rahi" → "raha":
        # an agent's first person follows its TTS voice (RUNBOOK §42).
        "languages": {"supported": ["hi", "en"], "default": "hi",
                      "tts_voices": {
                          "hi": {"model": "bulbul:v3", "speaker": "simran"},
                          "en": {"model": "bulbul:v3", "speaker": "simran"}}},
        "call_direction": "outbound",
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8}]},
        "response_bank": {"opening_greeting": OPENING},
        "outbound": {"opening_greeting": {"hi": OPENING}},
        "slots": [{"name": "customer_channel", "type": "text",
                   "question": {"hi": "Customers kaise milte hain?"}}],
    }
    for path, val in over.items():
        node = d
        keys = path.split(".")
        for k in keys[:-1]:
            node = node.setdefault(k, {})
        node[keys[-1]] = val
    return AgentDefinition.from_dict(d)


def _router(agent) -> ConversationRouter:
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.set_call_context(direction="outbound", contact_name="Alwyn",
                       contact_phone="9999999999", conn_id="t")
    return r


# ── schema ──────────────────────────────────────────────────────────────────

def test_new_fields_default_to_nothing():
    a = _agent()
    assert a.outbound.opening_variants == {}
    assert a.response_bank.opening_variants == {}
    assert a.personality.style_variants == []


def test_variants_survive_a_yaml_round_trip(tmp_path):
    a = _agent(**{"outbound.opening_variants": {"hi": VARIANTS},
                  "personality.style_variants": STYLES})
    path = tmp_path / "agent.yaml"
    a.to_yaml(path)
    b = AgentDefinition.from_yaml(path)
    assert b.outbound.opening_variants == {"hi": VARIANTS}
    assert b.personality.style_variants == STYLES


def test_variant_parsing_is_tolerant():
    a = _agent(**{"outbound.opening_variants":
                  {"hi": "just one line", "en": ["", "  ", None], "gu": 7}})
    assert a.outbound.opening_variants == {"hi": ["just one line"]}


# ── the opening changes from call to call ───────────────────────────────────

def test_without_variants_the_opening_is_the_authored_line():
    for _ in range(5):
        r = _router(_agent())
        assert asyncio.run(r.generate_opening()).text == OPENING


def test_openings_vary_across_calls_and_stay_inside_the_authored_pool():
    agent = _agent(**{"outbound.opening_variants": {"hi": VARIANTS}})
    heard = {asyncio.run(_router(agent).generate_opening()).text
             for _ in range(60)}
    assert heard == {OPENING, *VARIANTS}


def test_variants_of_another_language_are_never_borrowed():
    agent = _agent(**{"outbound.opening_variants": {"en": ["Hi there!"]}})
    for _ in range(10):
        assert asyncio.run(_router(agent).generate_opening()).text == OPENING


def test_a_hello_replays_the_opening_this_call_actually_used():
    agent = _agent(**{"outbound.opening_variants": {"hi": VARIANTS}})
    r = _router(agent)
    spoken = asyncio.run(r.generate_opening()).text
    assert r.connect_intro_line() == spoken


def test_previews_stay_deterministic():
    agent = _agent(**{"outbound.opening_variants": {"hi": VARIANTS}})
    lines = {resolve_spoken_surfaces(agent)["openings"]["hi"]["line"]
             for _ in range(6)}
    assert lines == {OPENING}


# ── the conversational angle changes from call to call ──────────────────────

def test_no_style_variants_means_no_band_at_all():
    assert "━━ THIS CALL ━━" not in _router(_agent())._build_system_prompt()


def test_one_style_is_held_for_the_whole_call_and_sits_in_the_per_call_band():
    agent = _agent(**{"personality.style_variants": STYLES})
    r = _router(agent)
    first = r._build_system_prompt()
    assert first.count("━━ THIS CALL ━━") == 1
    chosen = next(s for s in STYLES if s in first)
    r.state.turn_count = 5
    assert chosen in r._build_system_prompt()          # same call, same angle
    # after the static prefix, before the per-turn boundary
    assert first.index("━━ FIRM RULES ━━") < first.index("━━ THIS CALL ━━") \
        < first.index("━━ CALL STATE ━━")


def test_styles_vary_across_calls():
    agent = _agent(**{"personality.style_variants": STYLES})
    seen = set()
    for _ in range(40):
        p = _router(agent)._build_system_prompt()
        seen.add(next(s for s in STYLES if s in p))
    assert seen == set(STYLES)


# ── listening ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "तो मतलब कैसे मिल रहा है मतलब?", "matlab kya?", "what do you mean?",
])
def test_a_question_is_never_stored_as_the_answer(monkeypatch, said):
    """Call 888: the caller asked what the question MEANT, the sentence was
    stored as customer_channel, and the agent replied "Samajh gayi" and moved
    on to its next question."""
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    r = _router(_agent())
    r.state.turn_count = 2
    r._pending_tool_proposals = [ToolProposal(
        name="record_customer_details", args={"customer_channel": said})]
    r._execute_tool_proposals(had_text=True)
    assert r.state_mgr.state.get("customer_channel") is None


def test_a_reprompt_repeats_the_question_not_the_acknowledgement():
    """Call 888: "माफ़ कीजिए, मुझे आपकी आवाज़ नहीं आई। Samajh gayi, Alwyn.
    Waise, aapka business kis cheez ka hai?" — the 59-character reply was
    under the old 90-character floor, so its acknowledgement was replayed."""
    r = _router(_agent())
    r.state.turn_count = 2
    r.last_agent_response = ("Samajh gayi, Alwyn. "
                             "Waise, aapka business kis cheez ka hai?")
    said = r.generate_silence_reprompt()
    assert "Samajh gayi" not in said
    assert said.endswith("Waise, aapka business kis cheez ka hai?")


def test_a_single_sentence_reprompt_is_unchanged():
    r = _router(_agent())
    r.state.turn_count = 2
    r.last_agent_response = "Aapka business kis cheez ka hai?"
    assert r.generate_silence_reprompt().endswith(
        "Aapka business kis cheez ka hai?")


# ── a "hello?" after a real opening gets the owner's second beat ────────────

def _say(router, text):
    async def go():
        return [r.text async for r in router.process_stream(text) if r.text]
    return asyncio.run(go())


def test_early_hello_speaks_the_second_beat_not_a_replay(stub_llm):
    second = "जी, भावना बोल रही हूँ — आवाज़ आ रही है ना?"
    agent = _agent(**{"call_flow.connect": {"intro_line": {"hi": second}}})
    r = _router(agent)
    asyncio.run(r.generate_opening())
    assert _say(r, "hello") == [second]
    # a second "hello?" repeats what was last said — never the opening again
    assert _say(r, "hello") == [second]


def test_early_hello_without_a_second_beat_still_replays(stub_llm):
    r = _router(_agent())
    opening = asyncio.run(r.generate_opening()).text
    assert _say(r, "hello") == [opening]


# ── no record tool on the spoken path (llm_config.record_details_on_main_call)

def test_record_tool_can_be_taken_off_the_spoken_path():
    from app.voice.llm_tools import build_tool_declarations
    names = lambda decls: [d["function"]["name"] for d in decls]
    assert "record_customer_details" in names(build_tool_declarations(["a"]))
    off = names(build_tool_declarations(["a"], include_record=False))
    assert "record_customer_details" not in off
    assert {"book_appointment", "request_callback", "flag_do_not_call",
            "end_call"} <= set(off)


def test_the_switch_defaults_to_todays_behaviour():
    assert _agent().llm_config.record_details_on_main_call is True
    assert _router(_agent())._record_on_main_call() is True
    off = _agent(**{"llm_config.record_details_on_main_call": False})
    assert _router(off)._record_on_main_call() is False


def test_a_booking_also_fills_the_agents_time_slot(monkeypatch):
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    monkeypatch.setenv("PROMPT_NOW_OVERRIDE", "2026-09-18T13:00:00")
    d_slots = [{"name": "consultation_time", "type": "time", "required": False,
                "question": {"hi": "कौन सा दिन और time?"}}]
    r = _router(_agent(slots=d_slots))
    r._write_calendar_booking = lambda *_a, **_k: None
    r.state.turn_count = 4
    r._pending_tool_proposals = [ToolProposal(
        name="book_appointment", args={"date": "2026-09-21", "time": "11:00"})]
    r._execute_tool_proposals(had_text=True)
    assert r.state_mgr.state.get("consultation_time") == "2026-09-21 at 11:00"


def test_an_optional_slot_is_read_only_when_it_was_just_asked(monkeypatch):
    """The meeting time is optional on purpose (never guessed from an early
    sentence) — but the turn after the agent ASKS for it, the answer is on the
    table and the background extractor is the only writer left."""
    monkeypatch.setenv("LLM_TOOLS_MODE", "on")
    slots = [{"name": "customer_channel", "type": "text",
              "question": {"hi": "Customers कैसे मिलते हैं?"}},
             {"name": "consultation_time", "type": "time", "required": False,
              "label": "Consultation time",
              "question": {"hi": "कौन सा दिन और time ठीक रहेगा?"}}]
    agent = _agent(slots=slots, **{"llm_config.record_details_on_main_call": False})
    r = _router(agent)
    r._api_key = "x"
    r.state_mgr.set_externally("customer_channel", "Referrals", source="system")
    seen = {}

    async def fake_extract(text, missing):
        seen["missing"] = list(missing)
    r._llm_extract_slots = fake_extract

    async def go(asked):
        r._last_asked_slot = asked
        seen.clear()
        r._spawn_slot_extractor("सोमवार ग्यारह बजे ठीक रहेगा")
        t = r._slot_extract_task
        if t is not None and seen == {}:
            await t
        return seen.get("missing")

    assert asyncio.run(go(None)) is None                       # not asked → not read
    assert asyncio.run(go("consultation_time")) == ["consultation_time"]
