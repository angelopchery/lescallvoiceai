"""Native tool calling (deploy/PLAN-llm-tool-calling.md): accumulator
compat quirks, proposal parsing, and the deterministic execution gates.
Offline — no LLM calls (execution operates on pre-built proposals)."""

from types import SimpleNamespace

import pytest

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter
from app.voice.llm_tools import (
    ToolCallAccumulator,
    ToolProposal,
    build_tool_declarations,
    clean_slot_args,
    confirm_line,
    resolve_tools_mode,
)


def _delta(name=None, args=None, index=None):
    return SimpleNamespace(
        index=index, id=None,
        function=SimpleNamespace(name=name, arguments=args))


def make_router(slots=None, enable_tools="shadow") -> ConversationRouter:
    d = {
        "agent_id": "t_tools",
        "version": "1.0.0",
        "identity": {"agent_name": "Asha", "company_name": "TestCo"},
        "languages": {"supported": ["en", "hi"], "default": "en",
                      "tts_voices": {}},
        "call_flow": {"stages": [
            {"id": "S1", "order": 0, "goal": "one", "turns_until_advance": 8},
            {"id": "S2", "order": 1, "goal": "two", "turns_until_advance": 99},
        ]},
        "response_bank": {"opening_greeting": "hi"},
        "llm_config": {"enable_tools": enable_tools},
        "slots": slots or [],
    }
    agent = AgentDefinition.from_dict(d)
    return ConversationRouter(agent_def=agent, default_language="en")


# ── accumulator ──────────────────────────────────────────────────────────

def test_accumulator_splits_gemini_index_none_calls():
    """The compat shim streams index=None on every delta (observed live):
    two parallel calls must not fuse into one."""
    acc = ToolCallAccumulator()
    acc.add_delta([_delta(name="book_appointment")])
    acc.add_delta([_delta(args='{"time":"3pm",')])
    acc.add_delta([_delta(args='"date":"Tuesday"}')])
    acc.add_delta([_delta(name="record_customer_details")])
    acc.add_delta([_delta(args='{"name":"Ramesh"}')])
    props = acc.finish()
    assert [p.name for p in props] == ["book_appointment",
                                      "record_customer_details"]
    assert props[0].args == {"time": "3pm", "date": "Tuesday"}
    assert props[1].args == {"name": "Ramesh"}
    assert all(p.valid for p in props)


def test_accumulator_spec_compliant_indexed_calls():
    acc = ToolCallAccumulator()
    acc.add_delta([_delta(name="end_call", index=0)])
    acc.add_delta([_delta(name="flag_do_not_call", index=1)])
    acc.add_delta([_delta(args='{"reason":"done"}', index=0)])
    props = acc.finish()
    assert props[0].args == {"reason": "done"}
    assert props[1].args == {}


def test_accumulator_malformed_args_flagged_not_raised():
    acc = ToolCallAccumulator()
    acc.add_delta([_delta(name="book_appointment", args='{"date": nope')])
    props = acc.finish()
    assert len(props) == 1 and not props[0].valid


# ── helpers ──────────────────────────────────────────────────────────────

def test_clean_slot_args_filters_nulls():
    assert clean_slot_args({"budget": "50 Lakhs", "bhk": "null",
                            "loc": "  ", "tl": None, "n": 3}) == {
        "budget": "50 Lakhs", "n": "3"}


def test_confirm_line_formats_and_survives_missing_keys():
    line = confirm_line("book_appointment", "en",
                        {"date": "Tuesday", "time": "3pm"})
    assert "Tuesday" in line and "3pm" in line
    assert confirm_line("book_appointment", "hi", {})  # no KeyError


def test_declarations_use_agent_slots():
    decls = build_tool_declarations(["budget", "bhk"])
    rec = next(d for d in decls
               if d["function"]["name"] == "record_customer_details")
    assert set(rec["function"]["parameters"]["properties"]) == {"budget",
                                                                "bhk"}


# ── mode resolution ──────────────────────────────────────────────────────

def test_mode_env_kill_switch_overrides_agent(monkeypatch):
    r = make_router(enable_tools="on")
    assert resolve_tools_mode(r._def) == "on"
    monkeypatch.setenv("LLM_TOOLS_MODE", "off")
    assert resolve_tools_mode(r._def) == "off"


def test_mode_invalid_yaml_value_falls_back_to_shadow():
    r = make_router(enable_tools="banana")
    assert r._def.llm_config.enable_tools == "shadow"


# ── execution gates ──────────────────────────────────────────────────────

def _exec(router, name, args, had_text=False):
    router._pending_tool_proposals = [ToolProposal(name=name, args=args)]
    return router._execute_tool_proposals(had_text=had_text)


def test_shadow_mode_never_executes():
    r = make_router(enable_tools="shadow")
    lines = _exec(r, "flag_do_not_call", {})
    assert lines == []
    assert not r.state_mgr.extra_flags.get("dnd_requested")


def test_booking_gated_by_turn_count_then_executes():
    r = make_router(enable_tools="on")
    r._tools_mode = "on"
    cs = r.memory.call_state
    r.state.turn_count = 1
    _exec(r, "book_appointment", {"date": "Tuesday", "time": "3pm"})
    assert cs.appointment_date is None  # rejected: too early
    r.state.turn_count = 4
    lines = _exec(r, "book_appointment", {"date": "Tuesday", "time": "3pm"})
    assert cs.appointment_date == "Tuesday" and cs.appointment_time == "3pm"
    assert any("Tuesday" in ln for ln in lines)  # tool-only turn speaks
    # Fill-only: a second proposal can never overwrite the fixed slot.
    _exec(r, "book_appointment", {"date": "Friday", "time": "5pm"})
    assert cs.appointment_date == "Tuesday"


def test_booking_requires_both_args():
    r = make_router(enable_tools="on")
    r._tools_mode = "on"
    r.state.turn_count = 4
    _exec(r, "book_appointment", {"date": "Tuesday"})
    assert r.memory.call_state.appointment_date is None


def test_record_details_fill_only_merge():
    r = make_router(slots=[{"name": "budget"}, {"name": "bhk"}],
                    enable_tools="on")
    r._tools_mode = "on"
    lines = _exec(r, "record_customer_details",
                  {"budget": "50 Lakhs", "bogus_slot": "x"})
    assert r.state_mgr.state.get("budget") == "50 Lakhs"
    assert "bogus_slot" not in r.state_mgr.state
    assert lines  # tool-only turn acks
    # fill_only: cannot overwrite
    _exec(r, "record_customer_details", {"budget": "90 Lakhs"})
    assert r.state_mgr.state.get("budget") == "50 Lakhs"


def test_dnd_and_callback_and_end_call():
    r = make_router(enable_tools="on")
    r._tools_mode = "on"
    _exec(r, "flag_do_not_call", {})
    assert r.state_mgr.extra_flags.get("dnd_requested") is True
    lines = _exec(r, "request_callback", {"when": "tomorrow morning"})
    assert r.state_mgr.extra_flags.get("callback_requested") is True
    assert any("tomorrow" in ln for ln in lines)
    _exec(r, "end_call", {"reason": "done"})
    assert r._llm_end_call_seen is True


def test_no_confirmations_when_model_spoke():
    r = make_router(enable_tools="on")
    r._tools_mode = "on"
    lines = _exec(r, "flag_do_not_call", {}, had_text=True)
    assert lines == []
    assert r.state_mgr.extra_flags.get("dnd_requested") is True


def test_invalid_proposal_skipped():
    r = make_router(enable_tools="on")
    r._tools_mode = "on"
    r._pending_tool_proposals = [
        ToolProposal(name="book_appointment", args={}, valid=False)]
    assert r._execute_tool_proposals(had_text=False) == []
    assert r.memory.call_state.appointment_date is None
