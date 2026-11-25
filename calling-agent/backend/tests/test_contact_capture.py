"""Deterministic contact capture (2026-07-30).

A live call stored the LLM's guess 'bell@gmail.com' (assembled from ASR
"bell www.gmail.com") as ground truth; the fill-only merge made it
immortal, the caller's correction to ben@gmail.com never reached state,
and the spelled retry was shredded into three separately-answered
fragments. Full diagnosis:
deploy/PLAN-language-fidelity-and-contact-capture.md.

Covers: the spoken-form normalizer corpus (B1 — seeded with the live
call's actual ASR outputs), validator gating + the LLM-write ban (B1/B4),
the engine capture flow through the router (B2/B3: payload → spelled
read-back → confirm / correction / unresolved cap), and the engine-level
slot provisioning that applies it to every agent (B6).
"""

from __future__ import annotations

import pytest

from app.voice.contact_capture import (looks_like_partial_email,
                                       normalize_spoken_email,
                                       spelled_readback, validate_email)
from test_call_termination import drive, make_router


@pytest.fixture(autouse=True)
def _no_network_llm(stub_llm):
    """Offline — see the stub_llm fixture in conftest."""


def email_router():
    """Router whose agent collects an email (mirrors the live LesCall
    agent: the slot is authored `type: text` — provisioning must infer)."""
    return make_router(slots=[
        {"name": "email", "priority": 100, "required": True, "type": "text",
         "merge_policy": "free", "question": {"en": "What is your email?"}},
    ])


# ── B1: spoken-form normalizer (live-call ASR corpus) ───────────────────────

@pytest.mark.parametrize("raw,want", [
    ("bell www.gmail.com", "bell@gmail.com"),          # ITN "www." artifact
    ("ben @gmail.com मैंने बोला।", "ben@gmail.com"),     # surrounding speech
    ("ben at gmail dot com", "ben@gmail.com"),
    ("b e n at gmail dot com", "ben@gmail.com"),
    ("C O N Y BN @ gmail.com", "conybn@gmail.com"),    # spelled-capital run
    ("angelo dot p at gmial dot con", "angelo.p@gmail.com"),  # snap + TLD
    ("tech at dhronai dot com", "tech@dhronai.com"),   # unknown domain kept
    ("rahul underscore 92 at yahoo dot co dot in", "rahul_92@yahoo.co.in"),
    ("my email is priya dot shah at outlook dot com",
     "priya.shah@outlook.com"),
    ("alvin anthony", None),
    ("उनका एड्रेस है।", None),
    ("cement manufacturing में काम", None),
])
def test_normalize_spoken_email(raw, want):
    assert normalize_spoken_email(raw) == want


def test_partial_email_detection():
    # the live call's actual fragments — all must buffer, not answer
    assert looks_like_partial_email("एल दब्यू वाय एल") is True
    assert looks_like_partial_email("c o n y bn @") is True
    assert looks_like_partial_email("email dot") is True
    # ordinary conversation must not
    assert looks_like_partial_email("cement manufacturing") is False
    assert looks_like_partial_email("haan theek hai") is False


def test_spelled_readback_shape():
    assert spelled_readback("ben@gmail.com") == "b. e. n. — at — gmail dot com"
    assert spelled_readback("no-at-sign") == "no-at-sign"


def test_validate_email():
    assert validate_email("ben@gmail.com") is True
    assert validate_email("bell www.gmail") is False
    assert validate_email("") is False


# ── B1/B4: state-layer gating ───────────────────────────────────────────────

def test_llm_may_not_write_email_slot():
    r = email_router()
    changes = r.state_mgr.merge_state({"email": "bell@gmail.com"},
                                      source="llm", utterance="x")
    assert changes == []
    assert r.state_mgr.state.get("email") is None


def test_invalid_email_rejected_from_any_source():
    r = email_router()
    changes = r.state_mgr.merge_state({"email": "bell www.gmail"},
                                      source="regex", utterance="x")
    assert changes == []
    assert r.state_mgr.state.get("email") is None


def test_deterministic_extraction_fills_email():
    """Volunteered mid-conversation email is captured by the typed
    extractor (regex-path, validator-clean) — no LLM involved."""
    r = email_router()
    changes = r.state_mgr.observe_user(
        "sure, it is priya dot shah at outlook dot com")
    assert r.state_mgr.state.get("email") == "priya.shah@outlook.com"
    # confirm-policy: captured value is pending, not ground truth
    assert "email" in r.state_mgr.detect_missing_slots()["pending"]


def test_provisioning_infers_email_type_from_text_slot():
    """The live LesCall agent's email slot is `type: text` — engine-level
    provisioning must upgrade it (B6) with confirm + spelled + cap."""
    r = email_router()
    spec = r.state_mgr.slots["email"]
    assert spec.type == "email"
    assert spec.merge_policy == "confirm"
    assert spec.confirm_readback == "spelled"
    assert spec.max_asks == 2


def test_authored_policy_respected():
    r = make_router(slots=[
        {"name": "email", "type": "email", "merge_policy": "correction_gated",
         "confirm_readback": "verbatim", "max_asks": 5},
    ])
    spec = r.state_mgr.slots["email"]
    assert spec.merge_policy == "correction_gated"
    assert spec.confirm_readback == "verbatim"
    assert spec.max_asks == 5


def test_phone_slots_keep_authored_policy():
    r = make_router(slots=[
        {"name": "confirm_contact_number", "type": "phone",
         "merge_policy": "free"},
    ])
    spec = r.state_mgr.slots["confirm_contact_number"]
    assert spec.merge_policy == "free"          # live flows must not stall
    assert spec.confirm_readback == "digit_groups_2"


# ── B2/B3: the capture flow through the router ──────────────────────────────

def arm(r):
    """The agent has asked for the email (ask-instruction bookkeeping)."""
    r.state_mgr.note_asked("email")
    r._refresh_capture_slot()
    assert r._capture_slot == "email"


def test_capture_payload_readback_confirm():
    r = email_router()
    arm(r)
    out = drive(r, "ben at gmail dot com")
    assert "b. e. n." in out
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert "email" in r.state_mgr.detect_missing_slots()["pending"]
    out2 = drive(r, "yes")
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert "email" not in r.state_mgr.detect_missing_slots()["pending"]
    assert "email" in r.state_mgr.detect_missing_slots()["filled"]


def test_capture_correction_rereads_new_value():
    """The bell@/ben@ deadlock: a correction while pending must re-enter
    read-back with the NEW value — never re-assert the old one."""
    r = email_router()
    arm(r)
    drive(r, "bell at gmail dot com")
    assert r.state_mgr.state["email"] == "bell@gmail.com"
    out = drive(r, "nahi, ben at gmail dot com maine bola")
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert "b. e. n." in out
    drive(r, "haan yes")
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert "email" in r.state_mgr.detect_missing_slots()["filled"]


def test_capture_rejection_reasks():
    r = email_router()
    arm(r)
    drive(r, "ben at gmail dot com")
    out = drive(r, "no")
    assert r.state_mgr.state.get("email") is None
    assert "email" in out.lower() or "ईमेल" in out


def test_capture_fragments_buffer_silently_then_assemble():
    r = email_router()
    arm(r)
    # fragment 1: agent must stay SILENT (the live call answered each
    # fragment separately and the attempt fell apart)
    out1 = drive(r, "b e n at")
    assert out1 == ""
    out2 = drive(r, "gmail dot com")
    assert r.state_mgr.state["email"] == "ben@gmail.com"
    assert "b. e. n." in out2


def test_capture_unresolved_after_cap_never_stores_guess():
    r = email_router()
    arm(r)
    drive(r, "ben at gmail dot com")
    drive(r, "no")                      # re-ask 1
    drive(r, "ben at gmail dot com")
    out = drive(r, "no")                # re-ask 2 → cap (max_asks=2)
    assert r.state_mgr.state.get("email") is None
    assert r.state_mgr.extra_flags.get("email_unresolved") is True
    assert "email" in r.state_mgr.detect_missing_slots()["skipped"]
    assert r._capture_slot is None
    # collect gates treat the skipped slot as satisfied — the call moves on
    assert r.state_mgr.collect_satisfied(["email"]) is True


def test_capture_unrelated_turn_falls_through():
    """A digression mid-capture routes normally (LLM), capture stays armed."""
    r = email_router()
    arm(r)
    out = drive(r, "which company are you calling from exactly?")
    assert out != ""                    # stubbed LLM answered
    assert r._capture_slot == "email"


def test_no_capture_for_agents_without_contact_slots():
    r = make_router()
    r._refresh_capture_slot()
    assert r._capture_slot is None


def test_auto_accept_disabled_for_email():
    """Two ignored turns must NOT silently promote an unconfirmed email."""
    r = email_router()
    arm(r)
    drive(r, "ben at gmail dot com")
    drive(r, "which company is this?")
    drive(r, "and where are you located?")
    drive(r, "okay tell me more")
    assert "email" in r.state_mgr.detect_missing_slots()["pending"]
