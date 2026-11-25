"""
v2 stage-machine / state-manager tests over the plan §8 seven-step fixture.

Everything here drives the engine deterministically (no LLM): branches,
advance_when gates, branch actions, merge policies, dependency gates,
read-back, max_asks, callback policy, DND, playbook matching, outbound
prompt substitution.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "seven_step_agent.yaml"


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Makes the module docstring's "no LLM" claim actually true: three tests
    here (callback policy, DND, wrong-number) reached a turn that falls through
    to the model and were opening real network connections."""


def _router() -> ConversationRouter:
    agent = AgentDefinition.from_yaml(FIXTURE)
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r._available_calendar_slots = lambda: list(r._def.calendar.mock_slots or [])
    r.set_call_context(direction="outbound", contact_name="Ramesh Bhai",
                       contact_phone="9876543210")
    return r


def test_fixture_loads_and_prompts():
    r = _router()
    r.state.stage = "RECOMMEND"
    prompt = r._build_system_prompt()
    assert "━━ CALL MISSION ━━" in prompt
    assert "Qualify the contact and book a confirmed site visit" in prompt
    assert "नमस्ते, क्या मेरी बात Ramesh Bhai जी से हो रही है?" in prompt  # outbound line
    assert "Trust proofs" in prompt
    assert "30 years in business" in prompt
    # Playbook guidance replaces the built-in 4 lines, with {trust_proofs} substituted
    assert "'Price too high' → acknowledge, cite 30 years in business; zero stalled projects" in prompt
    assert "'Send brochure'" not in prompt


def test_seven_step_happy_path_trace():
    """OPENING → 3 discovery gates → RECOMMEND → NEXT_STEP → CLOSING."""
    r = _router()
    r.state.stage = "OPENING"

    # Step 1-2: "yes it's me, good time" → advance_when intent=affirmation min_turns=1
    r.state.discovery_turns = 1
    r.state.last_intent = "affirmation"
    r._maybe_advance_stage()
    assert r.state.stage == "DISCOVER_LOCATION"

    # Step 3: location gate — not filled, no advance
    r.state.last_intent = "general"
    r._maybe_advance_stage()
    assert r.state.stage == "DISCOVER_LOCATION"
    r.state_mgr.observe_user("Vapi me dekh rahe hain")
    assert r.state_mgr.state["location"] == "Vapi"
    r._maybe_advance_stage()
    assert r.state.stage == "DISCOVER_CONFIG"

    # Step 4: bhk (enum with synonyms)
    r.state_mgr.observe_user("two bhk chahiye")
    assert r.state_mgr.state["bhk"] == "2BHK"
    r._maybe_advance_stage()
    assert r.state.stage == "DISCOVER_BUDGET"

    # Step 5: budget (money)
    r.state_mgr.observe_user("budget 30 lakh tak hai")
    assert r.state_mgr.state["budget"] == "30 Lakhs"
    r._maybe_advance_stage()
    assert r.state.stage == "RECOMMEND"

    # Match rules fire in the recommend stage prompt
    prompt = r._build_system_prompt()
    assert "MATCH RULES" in prompt
    assert "Recommend: Sahaj" in prompt

    # Step 7c: interest → NEXT_STEP via branch
    assert r._maybe_branch("affirmation") is True
    assert r.state.stage == "NEXT_STEP"

    # Phone capture (type: phone, merge_policy: confirm → pending first)
    r.state_mgr.observe_user("mera number 98765 43210 hai")
    gaps = r.state_mgr.detect_missing_slots()
    assert "phone" in gaps["pending"]
    # Caller confirms → slot settles; read-back instruction appears
    r.state_mgr.set_externally("phone", r.state_mgr.state["phone"], source="system")
    prompt = r._build_system_prompt()
    assert "98 76 54 32 10" in prompt  # digit_groups_2 read-back
    # advance_when slots:[phone] met
    r._maybe_advance_stage()
    assert r.state.stage == "CLOSING"


def test_rejection_branch_actions():
    """Step 7b: rejection → HANDLE_REJECTION; 'yes to WhatsApp' → NEXT_STEP
    with capture_phone + promise_whatsapp_media recorded."""
    r = _router()
    r.state.stage = "RECOMMEND"
    assert r._maybe_branch("negation") is True
    assert r.state.stage == "HANDLE_REJECTION"
    assert r._maybe_branch("affirmation") is True
    assert r.state.stage == "NEXT_STEP"
    assert r.state_mgr.extra_flags.get("whatsapp_promised") is True
    assert r.state_mgr.extra_flags.get("whatsapp_assets") == ["photos", "videos"]
    # capture_phone made the optional phone slot required → it's now missing
    assert "phone" in r.state_mgr.detect_missing_slots()["missing"]
    # ... and lands in the collected_data snapshot
    assert r.state_mgr.to_dict()["flags"]["whatsapp_promised"] is True


def test_branch_max_takes():
    r = _router()
    r.state.stage = "RECOMMEND"
    assert r._maybe_branch("question") is True     # take 1 → ALTERNATIVES
    r.state.stage = "RECOMMEND"
    assert r._maybe_branch("question") is True     # take 2
    r.state.stage = "RECOMMEND"
    assert r._maybe_branch("question") is False    # max_takes=2 exhausted


def test_dependency_gates():
    """budget depends_on [location, bhk] — blocked until both fill, but
    volunteered values still stored."""
    r = _router()
    gaps = r.state_mgr.detect_missing_slots()
    assert "budget" in gaps["blocked"]
    assert "budget" not in gaps["missing"]
    assert gaps["missing"][0] == "location"  # priority order, deps met
    # Volunteered budget fills opportunistically despite the gate
    r.state_mgr.observe_user("50 lakh ka budget hai")
    assert r.state_mgr.state["budget"] == "50 Lakhs"


def test_correction_gated_merge():
    r = _router()
    r.state_mgr.observe_user("budget 30 lakh")
    assert r.state_mgr.state["budget"] == "30 Lakhs"
    # Plain new mention without a correction marker → rejected
    r.state_mgr.observe_user("50 lakh")
    assert r.state_mgr.state["budget"] == "30 Lakhs"
    # Explicit correction → accepted
    r.state_mgr.observe_user("actually make it 50 lakh")
    assert r.state_mgr.state["budget"] == "50 Lakhs"


def test_max_asks_skip():
    r = _router()
    r.state_mgr.observe_user("Vapi")
    r.state_mgr.observe_user("2 bhk")
    # budget now missing (deps met); asking it on max_asks=2 DISTINCT turns
    # skips it. Asks are counted once per turn_index — prompt rebuilds within
    # a turn used to double-count and burn the budget without the agent ever
    # voicing a question (2026-07-15 fix).
    r.state_mgr.note_asked("budget")
    r.state_mgr.note_asked("budget")   # same turn — must NOT count again
    assert r.state_mgr.asked_count["budget"] == 1
    r.state_mgr.observe_user("hmm let me think")   # next turn
    r.state_mgr.note_asked("budget")
    gaps = r.state_mgr.detect_missing_slots()
    # The LAST permitted ask has just been chosen: the slot is no longer
    # askable and already counts as satisfied — but it is not `skipped` yet.
    # It stays `awaiting` (an extraction target) until the caller has had a
    # turn to answer (RUNBOOK §61, call 971: the answers to every max_asks=1
    # question were heard and never stored because the slot left `missing`
    # the moment the question was chosen).
    assert "budget" not in gaps["missing"]
    assert "budget" in gaps["awaiting"]
    assert "budget" not in gaps["skipped"]
    assert r.state_mgr.collect_satisfied(["budget"]) is True
    r.state_mgr.observe_user("no idea really")     # the caller's answer turn
    assert "budget" in r.state_mgr.detect_missing_slots()["awaiting"]
    r.state_mgr.observe_user("what else")           # one more turn, still unanswered
    gaps = r.state_mgr.detect_missing_slots()
    assert "budget" in gaps["skipped"]
    assert "budget" not in gaps["awaiting"]
    assert "budget" not in gaps["missing"]
    # skipped counts as satisfied for advancement
    assert r.state_mgr.collect_satisfied(["budget"]) is True


def test_callback_policy_no_calendar_booking():
    import asyncio
    r = _router()
    r.state.stage = "RECOMMEND"
    booked = []
    r._record_calendar_booking = lambda t: booked.append(t)
    async def _run():
        return [res async for res in r.process_stream("abhi busy hoon, kal call karna")]
    results = asyncio.run(_run())
    assert results and results[0].is_shortcut
    assert r.state_mgr.extra_flags.get("callback_requested") is True
    assert r.state_mgr.state.get("callback_time") == "Tomorrow"
    assert not booked                      # NEVER books a calendar slot
    assert r.state.decline_count == 0      # counts_as_decline: false
    assert not r.state.is_closing


def test_dnd_immediate_end():
    import asyncio
    r = _router()
    r.state.stage = "RECOMMEND"
    async def _run():
        return [res async for res in r.process_stream("stop calling me")]
    results = asyncio.run(_run())
    assert r.state.is_closing
    assert r.state_mgr.extra_flags.get("dnd_requested") is True
    assert r.call_outcome == "dnd"


def test_wrong_number_close():
    """RUNBOOK §73: the wrong-number phrase is re-confirmed once; the
    caller's yes to that closes the call with the authored line."""
    import asyncio
    r = _router()
    r.state.stage = "OPENING"
    async def _run(text):
        return [res async for res in r.process_stream(text)]
    results = asyncio.run(_run("sorry wrong number lag gaya"))
    assert not r.state.is_closing
    assert r._wrong_number_pending == 1
    assert results and "confirmation" in results[0].text
    results = asyncio.run(_run("haan"))
    assert r.state.is_closing
    assert r.call_outcome == "wrong_number"
    assert results and "गलत नंबर" in results[0].text


def test_objection_playbook_deterministic():
    r = _router()
    resp = r._pick_objection_response("this is too expensive for me")
    assert "30 years in business; zero stalled projects" in resp


def test_decline_threshold_wired():
    r = _router()
    assert (r._def.call_flow.decline_threshold or 2) == 2
