"""Operator control over numbers — and the two boundaries it must not cross.

An admin override exists precisely to do what a tenant cannot, so "the admin is
allowed" is not an answer to either question this file asks:

  * may a number sit on an account with no telephony entitlement? No — the
    number would answer calls the billing gate then refuses to meter.
  * may a number be bound to another account's agent? No — the inbound
    resolver TRUSTS the binding and never re-checks ownership, so that agent
    would answer in its own tenant's name and bill its own tenant's minutes.

FreeSWITCH is redirected at a temporary directory and its reload stubbed: these
tests are about the database and the rules, and a test that rewrote
/etc/freeswitch would be a test that takes production down.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401,E402

AGENTS_DIR = _REPO / "calling-agent" / "agents"


@pytest.fixture(autouse=True)
def fake_freeswitch(tmp_path, monkeypatch):
    """Point config generation at a tmp dir and stub the reload + ACL sync."""
    from shared import freeswitch_config as fsc
    from shared import kamailio_acl
    monkeypatch.setattr(fsc, "FS_CONF_DIR", str(tmp_path))
    monkeypatch.setattr(fsc, "_fs_reload",
                        lambda: {"reloadxml": {"ok": True, "output": "+OK", "via": "stub"}})
    monkeypatch.setattr(kamailio_acl, "sync_carrier_acl", lambda cidrs: {"stub": True})
    return tmp_path


@pytest.fixture()
def accounts():
    """Two tenants: `paid` is entitled to telephony, `free` is not."""
    from shared.billing.entitlements import resolve_entitlements
    from shared.db.database import get_db
    paid = f"paid-{uuid.uuid4().hex[:8]}@example.test"
    free = f"free-{uuid.uuid4().hex[:8]}@example.test"
    with get_db() as db:
        for who in (paid, free):
            db.execute("INSERT INTO users (email, name) VALUES (?,?) "
                       "ON CONFLICT (email) DO NOTHING", (who, "tel test"))
    for who, plan in ((paid, "growth"), (free, "free")):
        with get_db() as db:
            db.execute("INSERT INTO entitlements (owner, plan_code, overrides) "
                       "VALUES (?,?,?) ON CONFLICT (owner) DO UPDATE SET "
                       "overrides=excluded.overrides, plan_code=excluded.plan_code",
                       (who, plan, json.dumps({"plan_code": plan})))
        resolve_entitlements(who)
    yield {"paid": paid, "free": free}
    with get_db() as db:
        for who in (paid, free):
            db.execute("DELETE FROM phone_numbers WHERE owner=?", (who,))
            db.execute("DELETE FROM trunks WHERE owner=?", (who,))
            db.execute("DELETE FROM agents WHERE owner=?", (who,))
            db.execute("DELETE FROM entitlements WHERE owner=?", (who,))
            db.execute("DELETE FROM users WHERE email=?", (who,))


def _agent(owner: str, slug: str):
    """Claim a real on-disk agent for `owner` — the binding check needs both."""
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("INSERT INTO agents (slug, owner, display_name) VALUES (?,?,?) "
                   "ON CONFLICT (slug) DO UPDATE SET owner=excluded.owner",
                   (slug, owner, slug))


def _real_agent_slugs(n: int = 2) -> list[str]:
    slugs = sorted(p.name for p in AGENTS_DIR.iterdir()
                   if p.is_dir() and not p.name.startswith("_")
                   and (p / "agent.yaml").exists())
    if len(slugs) < n:
        pytest.skip("not enough agents on disk to exercise binding")
    return slugs[:n]


def _trunk(owner: str, suffix: str = "t1"):
    from shared.telephony_admin import create_trunk
    return create_trunk(owner, suffix, "example.zt.plivo.com",
                        caller_id="+91 80 0000 0000", actor="pytest")["trunk"]


def _number(owner: str, trunk_id: int, digits: str, **kw):
    from shared.telephony_admin import create_number
    return create_number(owner, digits, trunk_id, actor="pytest", **kw)["number"]


def _digits() -> str:
    return "+9180" + uuid.uuid4().int.__str__()[:8]


# ── entitlement boundary ────────────────────────────────────────────────────

def test_a_trunk_cannot_be_created_for_an_unentitled_account(accounts):
    from shared.telephony_admin import TelephonyAdminError, create_trunk
    with pytest.raises(TelephonyAdminError, match="does not include telephony"):
        create_trunk(accounts["free"], "t1", "example.zt.plivo.com")


def test_a_number_cannot_be_added_to_an_unentitled_account(accounts):
    from shared.telephony_admin import TelephonyAdminError, create_number
    trunk = _trunk(accounts["paid"])
    with pytest.raises(TelephonyAdminError, match="does not include telephony"):
        create_number(accounts["free"], _digits(), trunk["id"])


def test_a_locked_account_cannot_take_a_number(accounts):
    from shared.db.database import get_db
    from shared.telephony_admin import TelephonyAdminError, create_trunk
    with get_db() as db:
        db.execute("UPDATE entitlements SET status='locked', lock_reason=? WHERE owner=?",
                   ("abuse", accounts["paid"]))
    with pytest.raises(TelephonyAdminError, match="locked"):
        create_trunk(accounts["paid"], "t2", "example.zt.plivo.com")


def test_connect_re_checks_entitlement_at_the_moment_it_goes_live(accounts):
    """The row may have been created while the plan was still paid."""
    from shared.db.database import get_db
    from shared.telephony_admin import TelephonyAdminError, connect_number
    trunk = _trunk(accounts["paid"])
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    num = _number(accounts["paid"], trunk["id"], _digits(), inbound_agent_slug=slug)
    with get_db() as db:                                   # plan lapses
        db.execute("UPDATE entitlements SET telephony=0 WHERE owner=?", (accounts["paid"],))
    with pytest.raises(TelephonyAdminError, match="does not include telephony"):
        connect_number(num["id"])


# ── ownership boundary ──────────────────────────────────────────────────────

def test_a_number_cannot_be_bound_to_another_accounts_agent(accounts):
    from shared.telephony_admin import TelephonyAdminError, bind_number
    mine, theirs = _real_agent_slugs(2)
    _agent(accounts["paid"], mine)
    _agent(accounts["free"], theirs)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), inbound_agent_slug=mine)

    with pytest.raises(TelephonyAdminError, match="belongs to"):
        bind_number(num["id"], inbound_agent_slug=theirs)


def test_adding_a_number_with_a_foreign_agent_is_refused(accounts):
    from shared.telephony_admin import TelephonyAdminError, create_number
    _, theirs = _real_agent_slugs(2)
    _agent(accounts["free"], theirs)
    trunk = _trunk(accounts["paid"])
    with pytest.raises(TelephonyAdminError, match="belongs to"):
        create_number(accounts["paid"], _digits(), trunk["id"],
                      inbound_agent_slug=theirs)


def test_binding_to_the_accounts_own_agent_is_allowed(accounts):
    from shared.telephony_admin import bind_number, number_or_404
    mine, other_mine = _real_agent_slugs(2)
    _agent(accounts["paid"], mine)
    _agent(accounts["paid"], other_mine)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), inbound_agent_slug=mine)

    bind_number(num["id"], inbound_agent_slug=other_mine, outbound_agent_slug=mine)
    after = number_or_404(num["id"])
    assert after["inbound_agent_slug"] == other_mine
    assert after["outbound_agent_slug"] == mine


def test_an_unknown_agent_slug_is_refused(accounts):
    from shared.telephony_admin import TelephonyAdminError, bind_number
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), inbound_agent_slug=slug)
    with pytest.raises(TelephonyAdminError, match="no agent.yaml"):
        bind_number(num["id"], inbound_agent_slug="no-such-agent-anywhere")


def test_a_number_may_not_ride_another_accounts_trunk(accounts):
    """Egress uses that gateway's carrier credentials — the wrong customer
    would be billed and the caller ID would not be authorised."""
    from shared.telephony_admin import TelephonyAdminError, create_number
    from shared.db.database import get_db
    other = _trunk(accounts["paid"], "shared")
    with get_db() as db:                                    # entitle the second account
        db.execute("UPDATE entitlements SET telephony=1 WHERE owner=?", (accounts["free"],))
    with pytest.raises(TelephonyAdminError, match="belongs to"):
        create_number(accounts["free"], _digits(), other["id"])


def test_the_same_number_cannot_be_registered_twice(accounts):
    from shared.telephony_admin import TelephonyAdminError, create_number
    trunk = _trunk(accounts["paid"])
    digits = _digits()
    _number(accounts["paid"], trunk["id"], digits)
    with pytest.raises(TelephonyAdminError, match="already registered"):
        create_number(accounts["paid"], digits, trunk["id"])


# ── connect / disconnect / remove ───────────────────────────────────────────

def test_connect_writes_the_dialplan_and_disconnect_deletes_it(accounts, fake_freeswitch):
    from shared.telephony_admin import (connect_number, disconnect_number,
                                        number_or_404)
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), inbound_agent_slug=slug)

    res = connect_number(num["id"])
    assert res["status"] == "applied"
    dialplans = list((fake_freeswitch / "dialplan" / "public").glob("ai_inbound_*.xml"))
    assert len(dialplans) == 1
    assert number_or_404(num["id"])["config_status"] == "applied"

    disconnect_number(num["id"])
    assert not list((fake_freeswitch / "dialplan" / "public").glob("ai_inbound_*.xml"))
    after = number_or_404(num["id"])
    assert after["config_status"] == "disconnected"
    # The setup survives, so reconnecting is one click.
    assert after["inbound_agent_slug"] == slug and after["trunk_id"] == trunk["id"]


def test_connect_refuses_a_number_with_no_agent(accounts):
    """A connected DID with no binding answers with the neutral fallback —
    which reads to the caller as the wrong company, not as a misconfiguration."""
    from shared.telephony_admin import TelephonyAdminError, connect_number
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits())
    with pytest.raises(TelephonyAdminError, match="no agent bound"):
        connect_number(num["id"])


def test_remove_deletes_the_dialplan_too(accounts, fake_freeswitch):
    """Deleting only the row leaves FreeSWITCH still answering the DID."""
    from shared.telephony_admin import connect_number, remove_number
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), inbound_agent_slug=slug)
    connect_number(num["id"])
    assert list((fake_freeswitch / "dialplan" / "public").glob("ai_inbound_*.xml"))

    remove_number(num["id"])
    assert not list((fake_freeswitch / "dialplan" / "public").glob("ai_inbound_*.xml"))
    from shared.db.database import get_db
    with get_db() as db:
        assert db.execute("SELECT 1 FROM phone_numbers WHERE id=?",
                          (num["id"],)).fetchone() is None


def test_a_trunk_still_carrying_a_number_cannot_be_deleted(accounts):
    from shared.telephony_admin import TelephonyAdminError, delete_trunk
    trunk = _trunk(accounts["paid"])
    _number(accounts["paid"], trunk["id"], _digits())
    with pytest.raises(TelephonyAdminError, match="still use this trunk"):
        delete_trunk(trunk["id"])


# ── transfer ────────────────────────────────────────────────────────────────

def _entitle(owner: str):
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE entitlements SET telephony=1 WHERE owner=?", (owner,))


def test_transfer_moves_the_number_its_trunk_and_its_agents(accounts):
    """All three, or the number can receive calls and never place one:
    outbound checks the TRUNK's owner and the AGENT's owner as well."""
    from shared.db.database import get_db
    from shared.telephony_admin import number_or_404, transfer_number
    src, dst = accounts["paid"], accounts["free"]
    _entitle(dst)
    a_in, a_out = _real_agent_slugs(2)
    _agent(src, a_in)
    _agent(src, a_out)
    trunk = _trunk(src)
    num = _number(src, trunk["id"], _digits(), inbound_agent_slug=a_in,
                  outbound_agent_slug=a_out)

    out = transfer_number(num["id"], dst, actor="pytest", reason="handover")
    assert number_or_404(num["id"])["owner"] == dst
    assert set(out["agents"]) == {a_in, a_out}
    with get_db() as db:
        assert db.execute("SELECT owner FROM trunks WHERE id=?",
                          (trunk["id"],)).fetchone()["owner"] == dst
        for slug in (a_in, a_out):
            assert db.execute("SELECT owner FROM agents WHERE slug=?",
                              (slug,)).fetchone()["owner"] == dst


def test_transfer_renames_the_trunk_to_the_new_owner(accounts):
    """The trunk name is the FreeSWITCH gateway name AND is shown to the
    tenant — leaving it puts the previous owner's email in their panel."""
    from shared.telephony_admin import transfer_number
    src, dst = accounts["paid"], accounts["free"]
    _entitle(dst)
    trunk = _trunk(src, "plivo-1234")
    num = _number(src, trunk["id"], _digits())

    out = transfer_number(num["id"], dst, actor="pytest", reason="handover")
    assert out["trunk"]["was"] == trunk["name"]
    assert out["trunk"]["now"].endswith("_plivo-1234")
    assert out["trunk"]["now"] != trunk["name"]
    assert dst.split("@")[0] in out["trunk"]["now"]


def test_transfer_to_an_unentitled_account_is_refused(accounts):
    from shared.telephony_admin import TelephonyAdminError, transfer_number
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits())
    with pytest.raises(TelephonyAdminError, match="does not include telephony"):
        transfer_number(num["id"], accounts["free"], actor="pytest")


def test_transfer_refuses_to_take_a_trunk_other_numbers_still_use(accounts):
    from shared.telephony_admin import TelephonyAdminError, transfer_number
    src, dst = accounts["paid"], accounts["free"]
    _entitle(dst)
    trunk = _trunk(src)
    keep = _number(src, trunk["id"], _digits())
    move = _number(src, trunk["id"], _digits())
    assert keep["id"] != move["id"]
    with pytest.raises(TelephonyAdminError, match="other number"):
        transfer_number(move["id"], dst, actor="pytest")


def test_transfer_can_leave_the_trunk_behind_when_asked(accounts):
    from shared.db.database import get_db
    from shared.telephony_admin import transfer_number
    src, dst = accounts["paid"], accounts["free"]
    _entitle(dst)
    trunk = _trunk(src)
    _number(src, trunk["id"], _digits())
    move = _number(src, trunk["id"], _digits())

    out = transfer_number(move["id"], dst, move_trunk=False, actor="pytest")
    assert out["trunk"] is None
    with get_db() as db:
        assert db.execute("SELECT owner FROM trunks WHERE id=?",
                          (trunk["id"],)).fetchone()["owner"] == src


def test_transfer_preview_reports_the_whole_consequence(accounts):
    from shared.telephony_admin import transfer_preview
    src, dst = accounts["paid"], accounts["free"]
    slug = _real_agent_slugs(1)[0]
    _agent(src, slug)
    trunk = _trunk(src)
    num = _number(src, trunk["id"], _digits(), inbound_agent_slug=slug)

    p = transfer_preview(num["id"], dst)
    assert p["from"] == src and p["to"] == dst
    assert p["trunk"]["can_move"] is True
    assert [a["slug"] for a in p["bound_agents"]] == [slug]
    assert p["eligibility"]["ok"] is False          # dst is still on Free


def test_transferring_to_the_same_account_is_refused(accounts):
    from shared.telephony_admin import TelephonyAdminError, transfer_number
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits())
    with pytest.raises(TelephonyAdminError, match="already belongs"):
        transfer_number(num["id"], accounts["paid"], actor="pytest")


# ── reads the console depends on ────────────────────────────────────────────

def test_tenant_agents_lists_only_this_accounts_claimed_agents(accounts):
    from shared.telephony_admin import tenant_agents
    mine, theirs = _real_agent_slugs(2)
    _agent(accounts["paid"], mine)
    _agent(accounts["free"], theirs)
    slugs = [a["slug"] for a in tenant_agents(accounts["paid"])]
    assert mine in slugs and theirs not in slugs


# ── test call ───────────────────────────────────────────────────────────────

def test_originate_carries_the_dialled_number(accounts):
    """RUNBOOK §33: without lescall_dest on the channel the bridge falls back to
    'the most recently logged dial', which during a burst is somebody else —
    44 calls greeted a real person by a stranger's name. A second hand-written
    originate builder is how that comes back on one surface only."""
    from shared.fs_originate import originate_cmd
    cmd = originate_cmd("919876543210", agent_slug="a1",
                        caller_id="918031339140", gateway_name="gw1")
    assert "lescall_dest=919876543210" in cmd
    assert "lescall_agent_slug=a1" in cmd
    assert "origination_caller_id_number=918031339140" in cmd
    assert "sofia/gateway/gw1/919876543210" in cmd


def test_a_ten_digit_number_is_dialled_with_its_country_code(accounts):
    """2026-09-09: a phone-book row held "7359800826". FreeSWITCH INVITEd the
    gateway with exactly that, the carrier replied SIP 404 and the leg hung up
    UNALLOCATED_NUMBER — which the operator saw only as "declined", four times
    in two minutes. The number is national-format Indian; the country code is
    the whole difference between a call and a 404."""
    from shared.fs_originate import originate_cmd
    cmd = originate_cmd("7359800826", agent_slug="a1",
                        caller_id="918031339140", gateway_name="gw1")
    assert "sofia/gateway/gw1/917359800826" in cmd
    assert "lescall_dest=917359800826" in cmd


def test_the_dialled_number_on_the_channel_matches_the_number_dialled(accounts):
    """lescall_dest is what the bridge matches a contact's identity against
    (RUNBOOK §33). If normalization reached the dial string but not the channel
    variable, an outbound leg would carry a destination it never called."""
    from shared.fs_originate import originate_cmd
    for raw in ("7359800826", "07359800826", "+91 73598-00826", "917359800826"):
        cmd = originate_cmd(raw, gateway_name="gw1")
        assert "lescall_dest=917359800826" in cmd, raw
        assert "sofia/gateway/gw1/917359800826 " in cmd, raw


def test_normalizing_never_makes_a_dialable_string_undialable(accounts):
    """A destination `to_e164` cannot place keeps its own digits. This function
    is here to repair numbers, and must not be the reason a short internal
    extension or an already-international number stops connecting."""
    from shared.fs_originate import dialable_destination
    assert dialable_destination("1234") == "1234"          # too short for E.164
    assert dialable_destination("442071838750") == "442071838750"   # UK, 12 digits
    assert dialable_destination("+1 415 555 0132") == "14155550132"
    assert dialable_destination("") == ""


def test_test_call_needs_a_real_destination(accounts):
    from shared.telephony_admin import TelephonyAdminError, test_call
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), outbound_agent_slug=slug)
    for bad in ("", "123", "not a number"):
        with pytest.raises(TelephonyAdminError, match="with country code"):
            test_call(num["id"], bad)


def test_test_call_refuses_until_the_trunk_is_applied(accounts):
    """Its gateway does not exist in FreeSWITCH yet, so there is nothing to
    dial through — a clearer refusal than an ESL error."""
    from shared.telephony_admin import TelephonyAdminError, test_call
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])          # created 'pending', never applied
    num = _number(accounts["paid"], trunk["id"], _digits(), outbound_agent_slug=slug)
    with pytest.raises(TelephonyAdminError, match="Press Connect"):
        test_call(num["id"], "919876543210")


def test_test_call_refuses_a_number_with_no_agent_of_its_own(accounts):
    from shared.telephony_admin import TelephonyAdminError, connect_number, test_call
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), outbound_agent_slug=slug)
    connect_number(num["id"])
    from shared.db.database import get_db
    with get_db() as db:                      # the agent leaves the account
        db.execute("UPDATE agents SET owner=? WHERE slug=?", (accounts["free"], slug))
    with pytest.raises(TelephonyAdminError, match="belongs to"):
        test_call(num["id"], "919876543210")


def test_test_call_refuses_an_unentitled_account(accounts):
    from shared.db.database import get_db
    from shared.telephony_admin import TelephonyAdminError, connect_number, test_call
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), outbound_agent_slug=slug)
    connect_number(num["id"])
    with get_db() as db:
        db.execute("UPDATE entitlements SET telephony=0 WHERE owner=?", (accounts["paid"],))
    with pytest.raises(TelephonyAdminError, match="does not include telephony"):
        test_call(num["id"], "919876543210")


def test_test_call_never_leaves_a_quota_reservation_behind(accounts, monkeypatch):
    """A refused or unanswered test must not hold minutes against the tenant."""
    from shared.db.database import get_db
    from shared.telephony_admin import connect_number, test_call
    import shared.fs_esl as fs_esl
    slug = _real_agent_slugs(1)[0]
    _agent(accounts["paid"], slug)
    trunk = _trunk(accounts["paid"])
    num = _number(accounts["paid"], trunk["id"], _digits(), outbound_agent_slug=slug)
    connect_number(num["id"])

    seen = {}
    monkeypatch.setattr(fs_esl, "fs_api",
                        lambda cmd, timeout=15: seen.setdefault("cmd", cmd) or "-ERR NO_ANSWER")
    out = test_call(num["id"], "919876543210", actor="pytest")
    assert out["ok"] is False
    assert "lescall_dest=919876543210" in seen["cmd"]
    assert f"sofia/gateway/{trunk['name']}/" in seen["cmd"]
    with get_db() as db:
        held = db.execute("SELECT COUNT(*) AS n FROM billing_reservations WHERE owner=?",
                          (accounts["paid"],)).fetchone()["n"]
    assert held == 0
