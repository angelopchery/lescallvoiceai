"""Regression gate for the 2026-09-08 Jai Mistry outbound call to Angelo P
Chery (RUNBOOK §35).

Two compounding defects, both pinned here:

1. The classic Agent Builder's Settings-tab save (`compileStages()` in
   frontend/src/api/agents.ts) rebuilt every stage from just {id, goal,
   turns_until_advance}, silently erasing the role/collect/advance_when/
   branches Copilot had correctly derived. One untouched Builder save wiped
   jai_mistry-outbound's wiring minutes after Copilot built it.

2. Even with wiring restored, the DISCOVERY stage's goal told the model to
   "advance to closing" after a caller said they were NOT focusing on lead
   generation — but advance_when required BOTH current_lead_gen AND
   agency_status to be filled, and the "no" path never asks about agency_status.
   The stage could only escape via max_turns, so the model was told to
   transition on a turn where it could not see the WRAP_UP stage's actual
   closing script, and it invented an off-script "anything else, or should
   we end the call?" line instead.

The fix: DISCOVERY branches to a dedicated NO_FOCUS_REASON stage the instant
the caller's answer classifies as `negation` (detect_intent, deterministic —
no LLM judgment needed for the transition itself), and NO_FOCUS_REASON
converges on WRAP_UP after exactly one turn. WRAP_UP's goal now opens by
telling the model to acknowledge whatever was just said (readable from CALL
STATE) before its scripted close — so the actual closing script is always in
view on the turn that needs it.

These tests exercise the state machine directly (branches/advance_when/
system-prompt content) — no live LLM call, matching the existing pattern in
test_recommend_before_schedule.py.
"""
from __future__ import annotations

import re
import pytest

from pathlib import Path

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter, detect_intent

# These are ENGINE regression tests. They used to load the LIVE agent file
# (agents/jai_mistry-outbound/agent.yaml), which is gitignored and edited by
# its owner through the Copilot — so on 2026-09-18, when the owner rebuilt that
# agent into a different persona with a different flow, 13 tests went red with
# no code change anywhere. The definition they were written against is frozen
# here instead: the pre-rewrite snapshot taken from `agent_revisions`
# (outbound revision 20, inbound revision 13 — both 2026-09-11). Engine behaviour stays pinned; the owner
# stays free to change their agent.
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "jai_mistry_rev20"
SLUG = "jai_mistry-outbound"
YAML = FIXTURES / f"{SLUG}.yaml"


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Nothing here may reach the real model."""


def _router() -> ConversationRouter:
    agent = AgentDefinition.from_yaml(YAML)
    r = ConversationRouter(agent_def=agent, default_language="hi")
    r.set_call_context(direction="outbound", contact_name="Angelo P Chery",
                       contact_phone="918076562864")
    return r


# ---------------------------------------------------------------------------
# 1. The exact caller utterance from the call is classified as negation
# ---------------------------------------------------------------------------

def test_the_actual_caller_reply_classifies_as_negation():
    """'फोकस तो नहीं कर रहा' — turn 2 of the 09:51:30 call."""
    assert detect_intent("फोकस तो नहीं कर रहा", "DISCOVERY") == "negation"


# ---------------------------------------------------------------------------
# 2. DISCOVERY branches to NO_FOCUS_REASON on a negation turn
# ---------------------------------------------------------------------------

def test_discovery_branches_to_no_focus_reason_on_negation():
    r = _router()
    r.state.stage = "DISCOVERY"
    r.state.discovery_turns = 1

    fired = r._maybe_branch("negation")

    assert fired is True
    assert r.state.stage == "NO_FOCUS_REASON"
    assert r.state.discovery_turns == 0


def test_discovery_does_not_branch_on_affirmation():
    """Q1=YES must keep asking Q2 (agency status), not jump to the exit."""
    r = _router()
    r.state.stage = "DISCOVERY"

    fired = r._maybe_branch("affirmation")

    assert fired is False
    assert r.state.stage == "DISCOVERY"


def test_the_branch_only_fires_once_per_call():
    r = _router()
    r.state.stage = "DISCOVERY"
    assert r._maybe_branch("negation") is True
    assert r.state.stage == "NO_FOCUS_REASON"

    # A second, unrelated negation later in NO_FOCUS_REASON must not re-fire
    # DISCOVERY's (now-consumed) branch — there is nothing to re-enter here.
    r.state.discovery_turns = 1
    assert r._maybe_branch("negation") is False
    assert r.state.stage == "NO_FOCUS_REASON"


# ---------------------------------------------------------------------------
# 3. NO_FOCUS_REASON's goal is exactly the "ask why" line — nothing else
# ---------------------------------------------------------------------------

def test_no_focus_reason_stage_asks_why_and_nothing_more():
    r = _router()
    r.state.stage = "NO_FOCUS_REASON"
    prompt = r._build_system_prompt()

    assert "Koi particular wajah rahi ab tak ispe focus na karne ki" in prompt
    # The old bug: a stage goal telling the model to improvise a transition.
    assert "advance to closing" not in prompt.lower()


# ---------------------------------------------------------------------------
# 4. NO_FOCUS_REASON converges on WRAP_UP after exactly one turn, whatever
#    the caller said — this is what makes the REAL closing script visible on
#    the very turn that needs it, instead of a blind "wrap up" instruction.
# ---------------------------------------------------------------------------

def test_no_focus_reason_converges_on_wrap_up_once_the_reason_is_captured():
    """Converges via an explicit when_slot branch — NOT list-adjacency/
    max_turns. The stage sits ahead of AGENCY_CHALLENGES in the YAML; a
    plain max_turns escape would incorrectly land there (caught live by this
    test before the branch was added — RUNBOOK §35 follow-up)."""
    r = _router()
    r.state.stage = "NO_FOCUS_REASON"
    r.state.discovery_turns = 1
    r.state_mgr.set_externally("no_focus_reason", "budget issue", source="system")

    branched = r._maybe_branch("general")

    assert branched is True
    assert r.state.stage == "WRAP_UP"


def test_wrap_up_carries_the_real_closing_script_and_the_ack_rule():
    """WRAP_UP's own goal must reach the prompt whole.

    Deliberately derived from the live definition rather than pinned to a
    literal: the owner edits these exact sentences through Copilot (they were
    rewritten on 2026-09-08 and this assertion broke on a string the business
    had legitimately changed). What must never change is the WIRING — the
    stage's own script reaching the prompt, and the acknowledgment rule that
    keeps the model from opening the close cold."""
    r = _router()
    r.state.stage = "WRAP_UP"
    prompt = r._build_system_prompt()

    goal = next(s.goal for s in r._def.call_flow.stages if s.id == "WRAP_UP")
    # Every quoted verbatim line in the configured goal reaches the prompt —
    # AS THIS CALLER WILL HEAR IT (RUNBOOK §42): the goal is rendered per call,
    # so a "Sir" is held to the contact's gender (this contact has none, so it
    # goes) and first-person agreement to the voice. The wiring under test is
    # unchanged: the stage's own script, whole, in the prompt.
    scripted = re.findall(r"'([^']{25,})'", r._render_author_line(goal))
    assert scripted, "WRAP_UP goal carries no verbatim line to check"
    for line in scripted:
        assert line in prompt, f"WRAP_UP script line missing from prompt: {line[:40]!r}"
    # The acknowledgment rule (§35) — re-deleted by a Copilot goal rewrite on
    # 2026-09-08 and restored in §37.
    assert "acknowledge" in goal.lower()
    # §37: the goal must NOT tell the model to end the call itself. The
    # platform owns termination and asks the caller's permission first.
    assert "terminate the call" not in goal.lower()


# ---------------------------------------------------------------------------
# 5. A garbled/ambiguous reason (the ACTUAL transcript: "म्हटलं") still
#    converges cleanly — the fix does not depend on understanding the reason.
# ---------------------------------------------------------------------------

def test_garbled_reason_still_converges_on_wrap_up():
    """The actual transcript's reason turn ('म्हटलं') is unintelligible —
    the fix must not depend on understanding it. Capturing ANY text into
    no_focus_reason is enough to converge; classifying it is a separate,
    lower-stakes judgment call for WRAP_UP's own goal."""
    r = _router()
    r.state.stage = "NO_FOCUS_REASON"
    r.state.discovery_turns = 1
    intent = detect_intent("म्हटलं", "NO_FOCUS_REASON")
    r.state_mgr.set_externally("no_focus_reason", "म्हटलं", source="system")

    branched = r._maybe_branch(intent)

    assert branched is True
    assert r.state.stage == "WRAP_UP"


def test_no_focus_reason_does_not_converge_before_a_reason_is_captured():
    """The wildcard when_slot gate must not fire on entry — the slot is
    fresh/empty when the stage is entered, so the "ask why" line still gets
    spoken before anything converges."""
    r = _router()
    r.state.stage = "NO_FOCUS_REASON"
    r.state.discovery_turns = 0

    branched = r._maybe_branch("general")

    assert branched is False
    assert r.state.stage == "NO_FOCUS_REASON"
