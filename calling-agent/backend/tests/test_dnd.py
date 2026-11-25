"""In-call "stop calling me" → DND (2026-07-23, autodial open item #1).

Detection is deterministic and platform-wide (conversation._detect_dnd_request);
enforcement rides collected_data.flags into the intel pipeline: decide() forces
NO over everything, and _apply_dnd sets disposition='rejected' + cancels open
follow-ups. The autodial guardrail for disposition='rejected' already exists
and is never overridden.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from app.voice.conversation import _detect_dnd_request  # noqa: E402
from shared.intelligence.followup import decide  # noqa: E402


# ── detection: must catch the opt-out ───────────────────────────────────────

@pytest.mark.parametrize("text", [
    "please don't call me again",
    "do not call me anymore",
    "never call me",
    "stop calling me",
    "stop phoning this number please",
    "remove my number from your system",
    "take me off your list",
    "unsubscribe",
    "dobara call mat karna bhai",
    "phir kabhi phone mat karna",
    "call karna band karo",
    "mera number hata do",
    "दोबारा कॉल मत करना",
    "फिर कभी फोन मत करना",
    "कॉल करना बंद करो",
    "मेरा नंबर हटा दो",
    "ફરી ફોન ન કરતા",
    "મારો નંબર કાઢી નાખો",
])
def test_dnd_detected(text):
    assert _detect_dnd_request(text) is True


# ── detection: scheduling talk must NEVER trip it ───────────────────────────

@pytest.mark.parametrize("text", [
    "call me later",
    "don't call me today, call tomorrow",
    "abhi call mat karna, sham ko karna",
    "please call me after 8 pm",
    "I am busy right now, call me next week",
    "अभी कॉल मत करना, कल करना",
    "call me at the office number instead",
    "no",           # bare rejection ≠ DND
    "not interested",
])
def test_dnd_not_tripped_by_scheduling(text):
    assert _detect_dnd_request(text) is False


# ── decision: DND outranks everything ───────────────────────────────────────

def _call_with_flags(flags: dict) -> dict:
    import json
    return {"id": 1, "owner": "t@x.com", "contact_id": 5,
            "started_at": "2026-07-23 10:00:00",
            "collected_data": json.dumps({"flags": flags})}


def test_decide_dnd_forces_no_over_clarified_commitment():
    call = _call_with_flags({"dnd_requested": True,
                             "dnd_quote": "stop calling me"})
    # even a clarified in-call commitment with a deadline loses
    commitments = [{"source": "in_call", "clarified": 1, "id": 9,
                    "deadline_at": "2026-07-24T11:00:00+05:30",
                    "kind": "callback", "made_by": "customer",
                    "description": "call back tomorrow"}]
    row = decide(call, {"decision": "yes"}, commitments)
    assert row["decision"] == "no"
    assert row["scheduled_at"] is None
    assert "DND" in row["explanation"]
    assert row["reason"] == "stop calling me"


def test_decide_without_dnd_still_honors_commitment():
    call = _call_with_flags({})
    commitments = [{"source": "in_call", "clarified": 1, "id": 9,
                    "deadline_at": "2026-07-24T11:00:00+05:30",
                    "kind": "callback", "made_by": "customer",
                    "description": "call back tomorrow"}]
    row = decide(call, {}, commitments)
    assert row["decision"] == "yes"
