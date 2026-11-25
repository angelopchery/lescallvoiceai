"""Platform-enforced agent invariants (2026-08-04).

Each case here was a real live-call defect that reached production because
the write path accepted it and reported success. The platform now refuses the
write instead of relying on a human — or the copilot — to notice.

Companion runtime guarantees (asserted at the bottom): the engine must also
survive a bad definition that predates enforcement, because agents on disk
were written before these gates existed.
"""
from __future__ import annotations

import sys

import pytest

from conftest import REPO_ROOT


@pytest.fixture(scope="module")
def cop():
    dash = str(REPO_ROOT / "dashboard")
    if dash not in sys.path:
        sys.path.insert(0, dash)
    import agent_copilot_api as c
    return c


# ── write-time refusals ─────────────────────────────────────────────────────

def test_bare_scalars_in_object_list_are_refused(cop):
    """Both hardik twins lost five objection answers this way: the copilot
    sent {"objection_playbook": [3,4,5,6,7,8,9]}, the server answered
    {"status":"updated"} and the normalizer emptied the field."""
    v = cop._platform_invariants(
        {"response_bank": {"objection_playbook": [3, 4, 5]}})
    assert v and "list of objects" in v[0]


def test_orphaned_stage_collect_is_refused(cop):
    """A stage collecting an undefined slot used to advance vacuously, which
    silently deleted hardik's whole EXISTING_COVERAGE qualification."""
    v = cop._platform_invariants({
        "slots": [{"name": "real_slot"}],
        "call_flow": {"stages": [{"id": "S1", "collect": ["ghost_slot"]}]},
    })
    assert v and "not a defined slot" in v[0]


def test_invented_placeholder_in_spoken_text_is_refused(cop):
    """academic_counsellor opened every call saying the literal characters
    "{{student_name}}" — only three tokens are ever substituted."""
    v = cop._platform_invariants(
        {"response_bank": {"opening_greeting": "hello {{student_name}}"}})
    assert v and "read aloud" in v[0]


@pytest.mark.parametrize("payload", [
    {"personality": {"custom_instructions": "You are a real person."}},
    {"guardrails": {"never_rules": ["never admit you are an AI"]}},
    {"personality": {"custom_instructions": "Say you are not a bot."}},
])
def test_instructions_to_claim_humanity_are_refused(cop, payload):
    v = cop._platform_invariants(payload)
    assert v, payload
    assert any("ai_disclosure" in p for p in v)


# ── must NOT over-block ─────────────────────────────────────────────────────

def test_supported_tokens_and_wellformed_lists_pass(cop):
    assert cop._platform_invariants({
        "slots": [{"name": "city"}],
        "call_flow": {"stages": [{"id": "S1", "collect": ["city"]}]},
        "response_bank": {
            "opening_greeting": "Good {current_time}, am I speaking with {contact_name}?",
            "objection_playbook": [
                {"id": "x", "patterns": ["p"], "answer": {"en": "a"}}],
        },
    }) == []


def test_slots_prompt_template_is_not_flagged(cop):
    """response_bank.slots_prompt is a documented {slots} template, not an
    invented token."""
    assert cop._platform_invariants(
        {"response_bank": {"slots_prompt": {"en": "I have {slots}."}}}) == []


def test_every_live_agent_passes(cop):
    """Enforcement must never edit-lock an agent that already exists."""
    import glob
    import os

    import yaml
    failing = {}
    for f in glob.glob(str(REPO_ROOT / "calling-agent/agents/*/agent.yaml")):
        v = cop._platform_invariants(yaml.safe_load(open(f)) or {})
        if v:
            failing[os.path.basename(os.path.dirname(f))] = v[0]
    assert not failing, failing


# ── runtime guarantees (defence in depth for YAML written before the gates) ──

def test_engine_never_speaks_an_unresolved_placeholder():
    from app.voice.conversation import strip_unresolved_tokens
    out = strip_unresolved_tokens(
        "am I speaking with the parent or guardian of {{student_name}}?")
    assert "{" not in out and "student_name" not in out
    # a supported token has already been substituted by this point, and
    # ordinary text must survive untouched
    assert strip_unresolved_tokens("Good morning, Angelo!") == "Good morning, Angelo!"


def test_unknown_collect_name_holds_the_gate_instead_of_advancing():
    from app.voice.state_manager import ConversationStateManager, SlotSpec
    sm = ConversationStateManager([SlotSpec(name="city")])
    # Unknown name must NOT count as satisfied (it used to `continue`).
    assert sm.collect_satisfied(["ghost"]) is False
    assert sm.collect_satisfied(["city"]) is False      # defined but unfilled
    sm.set_externally("city", "Surat", source="test")
    assert sm.collect_satisfied(["city"]) is True
