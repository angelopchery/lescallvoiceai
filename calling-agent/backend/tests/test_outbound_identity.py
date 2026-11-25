"""Who is on THIS call — outbound contact identity (RUNBOOK §33).

THE DEFECT
----------
An outbound leg carries no first-hand statement of who it reached: the
channel's caller id is OUR origination number, and FreeSWITCH fills
Caller-Destination-Number with the dialplan extension ("outbound_ai_connect"),
not the customer's number. So the bridge matched the call to a dial record by
TIMESTAMP and, when no phone matched — which on an outbound leg was always —
silently took `best`: the most recently logged dial.

During a campaign burst the most recent dial is somebody else. 44 calls across
two tenants greeted a real person by a stranger's name, and stamped that
stranger's phone and contact_id onto the call row. The reported case: a call to
Amrit Raj opened by asking for Harsh Agrawal.

THE FIX, in two halves
----------------------
1. The originate now carries `lescall_dest` — the dialled number, on the
   channel, where it cannot race with any other leg.
2. A dial record may only speak for the person on the call when its phone
   actually matches. A time-only match is still allowed to LABEL direction; it
   may never supply a name, a number, a contact, or a memory lookup.

The second half is what makes this safe even without the first: with no
verified match the agent asks who it is speaking to, which is a small
degradation. Greeting someone by a stranger's name is not.
"""
from __future__ import annotations

import datetime as dt
import sys

import pytest

from conftest import REPO_ROOT

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture()
def bridge():
    import conversation_call_bridge as mod

    return mod


def _entry(phone: str, name: str, age_s: float = 5.0, **kw) -> dict:
    dialed = (dt.datetime.now(dt.timezone.utc)
              - dt.timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"phone": phone, "name": name, "contact_id": kw.pop("contact_id", "1"),
            "agent_slug": "shubhangi-outbound", "dialed_at": dialed,
            "owner": "o@x", "context_note": None, "followup_id": None,
            "placed_by": "user", "callback_meta": None, **kw}


# The real burst: Amrit Raj dialled, Harsh Agrawal logged one second later.
AMRIT = "916202469148"
HARSH = "916268617072"


@pytest.fixture()
def burst():
    """Two dials seconds apart, newest last — the shape that caused §33."""
    return [_entry(AMRIT, "Amrit Raj", age_s=9, contact_id="917"),
            _entry(HARSH, "Harsh Agrawal", age_s=2, contact_id="961")]


class _FakeLinkFile:
    """Stands in for CALL_LINK_FILE. A real Path cannot be monkeypatched
    (its methods are read-only), and the point here is to drive the lookup
    over a fixed entry list, not to exercise the filesystem."""

    def __init__(self, entries):
        import json
        self._text = json.dumps(entries)

    def exists(self):
        return True

    def read_text(self, **kw):
        return self._text


def _lookup(bridge, monkeypatch, entries, caller="", dialed=""):
    """Drive the real lookup over a fixed entry list, via the file fallback."""
    monkeypatch.setattr(bridge, "CALL_LINK_FILE", _FakeLinkFile(entries))

    class _Boom:
        def __enter__(self): raise RuntimeError("force the file fallback")
        def __exit__(self, *a): return False

    monkeypatch.setattr("shared.db.database.get_db", lambda *a, **k: _Boom())
    return bridge._lookup_recent_outbound_dial(caller, dialed)


# ---------------------------------------------------------------- the fix ---

def test_the_dialled_number_selects_its_own_dial_record(bridge, burst, monkeypatch):
    """The whole point: matching on the number this leg dialled, not on the
    channel's caller id (which outbound is our own DID)."""
    got = _lookup(bridge, monkeypatch, burst, caller="918065953160", dialed=AMRIT)
    assert got is not None
    assert got["name"] == "Amrit Raj", (
        f"dialled {AMRIT} but matched {got['name']!r} — this is the §33 defect")
    assert got["_exact"] is True


def test_a_time_only_match_is_flagged_not_trusted(bridge, burst, monkeypatch):
    """With nothing to match on, the newest dial may still be returned as a
    direction HINT — but it must announce that it is not identified."""
    got = _lookup(bridge, monkeypatch, burst, caller="918065953160", dialed="")
    assert got is not None, "the direction hint is still useful"
    assert got["_exact"] is False, (
        "an unmatched record claiming to be exact is exactly how a stranger's "
        "name reached the agent")


def test_the_newest_dial_never_becomes_the_identity(bridge, burst, monkeypatch):
    """The regression, stated as the user reported it."""
    got = _lookup(bridge, monkeypatch, burst, caller="918065953160", dialed=AMRIT)
    assert got["name"] != "Harsh Agrawal"


# --------------------------------------------------------- the phone guard ---

@pytest.mark.parametrize("caller,dialed,phone,want", [
    ("918065953160", AMRIT, AMRIT, True),      # outbound: dialled number matches
    ("918065953160", AMRIT, HARSH, False),     # outbound: the burst neighbour
    (AMRIT, "", AMRIT, True),                  # inbound: caller id matches
    ("918065953160", "", AMRIT, False),        # nothing to match on
    ("", "", AMRIT, False),                    # no numbers at all
    ("918065953160", "9122" + AMRIT, AMRIT, True),   # trunk prefix on the dial string
])
def test_phone_guard_uses_whichever_number_is_the_customer(
        bridge, caller, dialed, phone, want):
    assert bridge._dial_matches_phone(caller, {"phone": phone}, dialed) is want


def test_phone_guard_is_false_without_a_record(bridge):
    assert bridge._dial_matches_phone("919999999999", None, "919999999999") is False


# ------------------------------------------------------- the customer number ---

def test_outbound_customer_is_the_dialled_number_not_our_caller_id(bridge):
    """calls.phone and the memory lookup both key on this."""
    got = bridge._customer_number("outbound", "918065953160", AMRIT, None)
    assert bridge._sig10(got) == bridge._sig10(AMRIT)


def test_outbound_prefers_a_verified_records_normalized_form(bridge):
    """The dial string can carry a trunk prefix; the record holds the form the
    contacts table is keyed on."""
    got = bridge._customer_number("outbound", "918065953160", "9122" + AMRIT,
                                  {"phone": "+91 62024 69148"})
    assert got == "+91 62024 69148"


def test_an_unverified_record_never_supplies_the_customer_number(bridge):
    """Passing None for identity_dial is the caller's contract; this pins that
    the fallback is the DIALLED number and never the neighbour's."""
    got = bridge._customer_number("outbound", "918065953160", AMRIT, None)
    assert bridge._sig10(got) != bridge._sig10(HARSH)


def test_inbound_customer_is_the_caller_id(bridge):
    assert bridge._customer_number("inbound", AMRIT, "918065953160", None) == AMRIT


# ------------------------------------------------------------- the routing ---

def test_routing_withholds_identity_when_the_dial_is_someone_elses(
        bridge, monkeypatch):
    """End of the chain: `identity_dial` is what reaches set_call_context(),
    and it must be empty when the window's dial is not this call."""
    stale = {**_entry(HARSH, "Harsh Agrawal"), "_exact": False}
    monkeypatch.setattr(bridge, "_lookup_recent_outbound_dial",
                        lambda *a, **k: stale)
    monkeypatch.setattr(bridge, "resolve_agent_slug", lambda **k: "shubhangi-outbound")
    monkeypatch.setattr(bridge, "_lookup_agent_owner", lambda s: "o@x")
    monkeypatch.setattr(bridge, "_did_owner", lambda d: "o@x")
    monkeypatch.setattr(bridge, "INTEL_PRECALL_CONTEXT", False)

    out = bridge._resolve_call_routing("c1", caller_number="918065953160",
                                       did="", dialed_number=AMRIT)
    assert out["direction"] == "outbound", "the hint may still label direction"
    assert out["identity_dial"] is None, (
        "a dial for a different number must not name the person on this call")
    assert out["dial"] is stale, "the loose hint is still available for labelling"


def test_routing_passes_identity_through_on_a_real_match(bridge, monkeypatch):
    mine = {**_entry(AMRIT, "Amrit Raj"), "_exact": True}
    monkeypatch.setattr(bridge, "_lookup_recent_outbound_dial",
                        lambda *a, **k: mine)
    monkeypatch.setattr(bridge, "resolve_agent_slug", lambda **k: "shubhangi-outbound")
    monkeypatch.setattr(bridge, "_lookup_agent_owner", lambda s: "o@x")
    monkeypatch.setattr(bridge, "_did_owner", lambda d: "o@x")
    monkeypatch.setattr(bridge, "INTEL_PRECALL_CONTEXT", False)

    out = bridge._resolve_call_routing("c2", caller_number="918065953160",
                                       did="", dialed_number=AMRIT)
    assert out["identity_dial"] is not None
    assert out["identity_dial"]["name"] == "Amrit Raj"
