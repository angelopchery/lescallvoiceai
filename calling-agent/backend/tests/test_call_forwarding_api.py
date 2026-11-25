"""Conditional call forwarding (RUNBOOK §84) — API, DB and FreeSWITCH-file layer.

Endpoint functions are called directly against the lescall_test database, the
way test_admin_telephony.py does. FreeSWITCH is a tmp directory, the reload is
stubbed, and voice prompts are fake WAV bytes — nothing here places a call,
reaches Sarvam, or touches /etc/freeswitch.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.test_intel_queue import pg_test_db, _use_test_db  # noqa: F401 (fixtures)

_REPO = Path(__file__).resolve().parents[3]
for p in (str(_REPO / "dashboard"), str(_REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

import telephony_api as tapi  # noqa: E402
from shared import call_forwarding as cf  # noqa: E402
from shared import freeswitch_config as fsc  # noqa: E402

FAKE_WAV = b"RIFF" + b"\x00" * 4000


class _Req:
    def __init__(self, body: dict):
        self._body = body

    async def json(self):
        return self._body


@pytest.fixture()
def fs(tmp_path, monkeypatch):
    """A FreeSWITCH conf dir with the installer already run, a stubbed reload,
    and fake prompt synthesis. Returns (conf_dir, list_of_reloads, synth_calls)."""
    (tmp_path / "dialplan" / "public_forward").mkdir(parents=True)
    (tmp_path / "lescall_prompts").mkdir()
    (tmp_path / "dialplan" / "public.xml").write_text(
        '<X-PRE-PROCESS cmd="include" data="/etc/freeswitch/dialplan/public_forward/*.xml"/>\n'
        '<extension name="plivo_inbound"/>\n')
    monkeypatch.setattr(fsc, "FS_CONF_DIR", str(tmp_path))
    reloads, synth = [], []
    monkeypatch.setattr(cf, "_reloadxml", lambda: reloads.append(1) or {"ok": True, "output": "+OK"})
    monkeypatch.setattr(cf, "synthesize", lambda text: synth.append(text) or FAKE_WAV)
    return tmp_path, reloads, synth


@pytest.fixture()
def acct():
    """An owner and a stranger, each with an applied trunk and one number."""
    from shared.db.database import get_db
    tag = uuid.uuid4().hex[:8]
    owner, other = f"fwd-{tag}@example.test", f"other-{tag}@example.test"
    ids = {}
    with get_db() as db:
        rnd = f"{uuid.uuid4().int % 10**8:08d}"
        for who, did in ((owner, f"+9122{rnd}"), (other, f"+9180{rnd}")):
            t = db.execute(
                "INSERT INTO trunks (name, owner, sip_domain, config_status) "
                "VALUES (?,?,?,?) RETURNING id",
                (f"t_{who.split('@')[0]}", who, "x.zt.plivo.com", "applied")).fetchone()["id"]
            n = db.execute(
                "INSERT INTO phone_numbers (number, sig_digits, trunk_id, owner, direction, "
                "inbound_agent_slug, config_status) VALUES (?,?,?,?,?,?,?) RETURNING id",
                (did, did[-10:], t, who, "both", "recept-inbound", "applied")).fetchone()["id"]
            ids[who] = {"trunk": t, "number": n}
    yield {"owner": owner, "other": other, "ids": ids}
    with get_db() as db:
        for who in (owner, other):
            db.execute("DELETE FROM forwarded_calls WHERE owner=?", (who,))
            db.execute("DELETE FROM phone_numbers WHERE owner=?", (who,))
            db.execute("DELETE FROM forward_contacts WHERE owner=?", (who,))
            db.execute("DELETE FROM trunks WHERE owner=?", (who,))


# ── helpers ────────────────────────────────────────────────────────────────

def _add(owner, number="9876543210", label="My mobile"):
    return asyncio.run(tapi.create_forward_contact(
        _Req({"number": number, "label": label}), owner=owner))


def _verify(owner, contact_id, monkeypatch):
    monkeypatch.setattr(cf.secrets, "randbelow", lambda n: 1234)
    started = cf.start_verification(contact_id, owner)
    assert cf.check_verification(contact_id, owner, "1234") == {"verified": True}
    return started


def _patch(number_id, body, owner):
    return asyncio.run(tapi.update_number(number_id, _Req(body), owner=owner))


def _number_row(number_id):
    from shared.db.database import get_db
    with get_db() as db:
        return dict(db.execute("SELECT * FROM phone_numbers WHERE id=?", (number_id,)).fetchone())


def _forward_file(conf, number_id) -> Path:
    return conf / "dialplan" / "public_forward" / cf.forward_file_name(_number_row(number_id))


def _status(fn):
    with pytest.raises(HTTPException) as e:
        fn()
    return e.value.status_code


# ── phones: India-only, limits, privacy ────────────────────────────────────

def test_an_indian_number_is_stored_as_e164(acct, fs):
    out = _add(acct["owner"], "098765 43210")
    assert out["number"] == "+919876543210" and out["verified"] is False
    landline = _add(acct["owner"], "079-2325 1306")          # Ahmedabad landline
    assert landline["number"] == "+917923251306"


@pytest.mark.parametrize("bad", ["+14155550100", "+4420 7946 0958", "1800 123 4567",
                                 "+91 0123456789", "12345", ""])
def test_foreign_toll_free_and_junk_numbers_are_refused(acct, fs, bad):
    assert _status(lambda: _add(acct["owner"], bad)) == 400


def test_a_lescall_number_cannot_be_a_forward_phone(acct, fs):
    """Any account's LesCall number — forwarding to it would loop to an agent."""
    from shared.db.database import get_db
    with get_db() as db:
        theirs = db.execute("SELECT number FROM phone_numbers WHERE id=?",
                            (acct["ids"][acct["other"]]["number"],)).fetchone()["number"]
    assert _status(lambda: _add(acct["owner"], theirs)) == 400


def test_the_same_phone_cannot_be_saved_twice(acct, fs):
    _add(acct["owner"], "9876543210")
    assert _status(lambda: _add(acct["owner"], "+91 98765 43210")) == 409


def test_an_account_can_save_at_most_five_phones(acct, fs):
    for i in range(cf.MAX_CONTACTS):
        _add(acct["owner"], f"98765432{i:02d}")
    assert _status(lambda: _add(acct["owner"], "9876543299")) == 409


def test_phones_are_private_to_their_account(acct, fs):
    mine = _add(acct["owner"])
    theirs = tapi.list_forward_contacts(owner=acct["other"])["contacts"]
    assert all(c["id"] != mine["id"] for c in theirs)


def test_the_list_never_exposes_the_code_hash(acct, fs, monkeypatch):
    c = _add(acct["owner"])
    monkeypatch.setattr(cf.secrets, "randbelow", lambda n: 1234)
    cf.start_verification(c["id"], acct["owner"])
    listed = tapi.list_forward_contacts(owner=acct["owner"])
    assert listed["server_ready"] is True
    assert all("verify_code_hash" not in row for row in listed["contacts"])


# ── verification ───────────────────────────────────────────────────────────

def test_verification_call_reads_the_code_from_the_owners_own_number(acct, fs, monkeypatch):
    c = _add(acct["owner"])
    monkeypatch.setattr(cf.secrets, "randbelow", lambda n: 4071)
    started = cf.start_verification(c["id"], acct["owner"])
    cmd = started["command"]
    assert cmd.startswith("originate {")
    assert f"sofia/gateway/t_{acct['owner'].split('@')[0]}/919876543210" in cmd
    digits = cf.digit_prefix()
    assert f"{digits}4.wav!{digits}0.wav!{digits}7.wav!{digits}1.wav" in cmd
    assert "accountcode=lcverify" in cmd and "&playback(file_string://" in cmd


def test_a_wrong_code_counts_and_the_right_one_verifies(acct, fs, monkeypatch):
    c = _add(acct["owner"])
    monkeypatch.setattr(cf.secrets, "randbelow", lambda n: 1234)
    cf.start_verification(c["id"], acct["owner"])
    with pytest.raises(cf.ForwardingError, match="not right"):
        cf.check_verification(c["id"], acct["owner"], "9999")
    assert cf.check_verification(c["id"], acct["owner"], "1234") == {"verified": True}
    assert tapi.list_forward_contacts(owner=acct["owner"])["contacts"][0]["verified"]


def test_five_wrong_codes_lock_the_code(acct, fs, monkeypatch):
    c = _add(acct["owner"])
    monkeypatch.setattr(cf.secrets, "randbelow", lambda n: 1234)
    cf.start_verification(c["id"], acct["owner"])
    for _ in range(cf.VERIFY_MAX_TRIES):
        with pytest.raises(cf.ForwardingError):
            cf.check_verification(c["id"], acct["owner"], "0000")
    with pytest.raises(cf.ForwardingError) as e:
        cf.check_verification(c["id"], acct["owner"], "1234")
    assert e.value.status == 429


def test_an_expired_code_is_refused(acct, fs, monkeypatch):
    from shared.db.database import get_db
    c = _add(acct["owner"])
    monkeypatch.setattr(cf.secrets, "randbelow", lambda n: 1234)
    cf.start_verification(c["id"], acct["owner"])
    with get_db() as db:
        db.execute("UPDATE forward_contacts SET verify_expires_at=now() - interval '1 minute' "
                   "WHERE id=?", (c["id"],))
    with pytest.raises(cf.ForwardingError) as e:
        cf.check_verification(c["id"], acct["owner"], "1234")
    assert e.value.status == 410


def test_at_most_three_code_calls_a_day(acct, fs):
    c = _add(acct["owner"])
    for _ in range(cf.VERIFY_CALLS_PER_DAY):
        cf.start_verification(c["id"], acct["owner"])
    with pytest.raises(cf.ForwardingError) as e:
        cf.start_verification(c["id"], acct["owner"])
    assert e.value.status == 429


def test_another_account_cannot_verify_my_phone(acct, fs):
    c = _add(acct["owner"])
    with pytest.raises(cf.ForwardingError) as e:
        cf.start_verification(c["id"], acct["other"])
    assert e.value.status == 404


def test_a_failed_prompt_does_not_burn_a_code_call(acct, fs, monkeypatch):
    c = _add(acct["owner"])

    def boom(text):
        raise RuntimeError("sarvam down")
    monkeypatch.setattr(cf, "synthesize", boom)
    with pytest.raises(cf.ForwardingError):
        cf.start_verification(c["id"], acct["owner"])
    monkeypatch.setattr(cf, "synthesize", lambda text: FAKE_WAV)
    for _ in range(cf.VERIFY_CALLS_PER_DAY):      # all three still available
        cf.start_verification(c["id"], acct["owner"])


# ── switching a number to forwarding ───────────────────────────────────────

def test_forwarding_needs_a_verified_phone(acct, fs):
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    assert _status(lambda: _patch(nid, {"forward_mode": "forward"}, acct["owner"])) == 400
    assert _status(lambda: _patch(nid, {"forward_mode": "forward",
                                        "forward_contact_id": c["id"]}, acct["owner"])) == 400


def test_a_number_cannot_forward_to_another_accounts_phone(acct, fs, monkeypatch):
    nid = acct["ids"][acct["owner"]]["number"]
    theirs = _add(acct["other"], "9000000001")
    _verify(acct["other"], theirs["id"], monkeypatch)
    assert _status(lambda: _patch(nid, {"forward_mode": "forward",
                                        "forward_contact_id": theirs["id"]},
                                  acct["owner"])) == 404


def test_turning_forwarding_on_writes_the_file_and_off_deletes_it(acct, fs, monkeypatch):
    conf, reloads, _ = fs
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)

    out = _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"],
                       "forward_line_name": "Pramukh"}, acct["owner"])
    assert out["forwarding"]["state"] == "on"
    path = _forward_file(conf, nid)
    xml = path.read_text()
    assert "sofia/gateway/t_fwd-" in xml and "919876543210" in xml
    assert reloads, "FreeSWITCH must be reloaded after writing the file"

    out = _patch(nid, {"forward_mode": "agent"}, acct["owner"])
    assert out["forwarding"]["state"] == "off" and not path.exists()


def test_the_line_name_is_spoken_not_the_admin_label(acct, fs, monkeypatch):
    _, _, synth = fs
    nid = acct["ids"][acct["owner"]]["number"]
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE phone_numbers SET label='restored for handover to x@y' WHERE id=?", (nid,))
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)
    _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"],
                 "forward_line_name": "Pramukh Vedanta"}, acct["owner"])
    assert "Call on your Pramukh Vedanta line," in synth
    assert not any("handover" in t for t in synth)


def test_an_empty_line_name_falls_back_to_lescall(acct, fs, monkeypatch):
    _, _, synth = fs
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)
    _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"]}, acct["owner"])
    assert "Call on your LesCall line," in synth


@pytest.mark.parametrize("body", [
    {"forward_ring_s": 5}, {"forward_ring_s": 46}, {"forward_ring_s": "abc"},
    {"forward_fallback": "voicemail"}, {"forward_mode": "sometimes"},
    {"forward_line_name": "x" * 41}, {"forward_line_name": "<script>"},
])
def test_bad_settings_are_refused(acct, fs, body):
    nid = acct["ids"][acct["owner"]]["number"]
    assert _status(lambda: _patch(nid, body, acct["owner"])) == 400


def test_the_setting_saves_but_reports_when_the_server_is_not_ready(acct, fs, monkeypatch):
    conf, _, _ = fs
    (conf / "dialplan" / "public.xml").write_text('<extension name="plivo_inbound"/>\n')
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE forward_contacts SET verified_at=now() WHERE id=?", (c["id"],))
    out = _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"]}, acct["owner"])
    assert out["forward_mode"] == "forward"
    assert out["forwarding"]["state"] == "error"
    assert "not switched on for this server" in out["forwarding"]["reason"]
    assert not _forward_file(conf, nid).exists()


def test_deleting_the_phone_puts_the_number_back_on_its_agent(acct, fs, monkeypatch):
    conf, _, _ = fs
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)
    _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"]}, acct["owner"])
    assert _forward_file(conf, nid).exists()
    out = tapi.delete_forward_contact(c["id"], owner=acct["owner"])
    assert out["numbers_back_to_agent"] == [nid]
    row = _number_row(nid)
    assert row["forward_mode"] == "agent" and row["forward_contact_id"] is None
    assert not _forward_file(conf, nid).exists()


def test_deleting_the_number_removes_its_forward_file(acct, fs, monkeypatch):
    conf, _, _ = fs
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)
    _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"]}, acct["owner"])
    path = _forward_file(conf, nid)
    assert path.exists()
    tapi.delete_number(nid, owner=acct["owner"])
    assert not path.exists()


def test_a_disconnected_number_does_not_forward(acct, fs, monkeypatch):
    conf, _, _ = fs
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)
    _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"]}, acct["owner"])
    path = _forward_file(conf, nid)
    from shared.db.database import get_db
    with get_db() as db:
        db.execute("UPDATE phone_numbers SET config_status='disconnected' WHERE id=?", (nid,))
    assert cf.sync_number(nid)["reason"] == "the number is disconnected"
    assert not path.exists()


def test_handover_stops_forwarding_to_the_previous_owners_phone(acct, fs, monkeypatch):
    """transfer_number's own reset, exercised directly on the row + file."""
    conf, _, _ = fs
    nid = acct["ids"][acct["owner"]]["number"]
    c = _add(acct["owner"])
    _verify(acct["owner"], c["id"], monkeypatch)
    _patch(nid, {"forward_mode": "forward", "forward_contact_id": c["id"]}, acct["owner"])
    from shared.db.database import get_db
    with get_db() as db:   # what the new owner would inherit without the reset
        db.execute("UPDATE phone_numbers SET owner=? WHERE id=?", (acct["other"], nid))
    # Even before transfer_number's reset runs, the contact-owner check refuses it.
    assert cf.sync_number(nid)["reason"] == "the phone belongs to another account"
    assert not _forward_file(conf, nid).exists()
    # And transfer_number resets the row in the same transaction as the move.
    src = (_REPO / "shared" / "telephony_admin.py").read_text()
    assert "forward_contact_id=NULL WHERE id=?" in src


# ── forwarded-call records (CDR → forwarded_calls) ─────────────────────────

def _cdr_line(uuid_, acct_code, billsec, duration, cause, caller="+919811112222",
              dest="+912264231897", start="2026-11-05 10:00:00", answer=""):
    cells = ["", caller, dest, "public", start, answer, "2026-11-05 10:01:00",
             str(duration), str(billsec), cause, uuid_, "", acct_code, "PCMU", "PCMU"]
    return ",".join(f'"{c}"' for c in cells) + "\n"


def test_cdr_rows_classify_by_accountcode():
    base = {"caller_id_number": "+919811112222", "destination_number": "+912264231897",
            "uuid": "u", "duration": "40"}
    connected = cf.classify_cdr_row({**base, "accountcode": "lcfwd_7", "billsec": "30",
                                     "hangup_cause": "NORMAL_CLEARING"})
    assert connected["outcome"] == "connected" and connected["talk_s"] == 30
    assert connected["ring_s"] == 10 and connected["number_id"] == 7
    hung = cf.classify_cdr_row({**base, "accountcode": "lcfwd_7", "billsec": "0",
                                "hangup_cause": "ORIGINATOR_CANCEL"})
    assert hung["outcome"] == "caller_hung_up"
    to_agent = cf.classify_cdr_row({**base, "accountcode": "lcfwd_7_NO_ANSWER",
                                    "billsec": "12", "hangup_cause": "NORMAL_CLEARING"})
    assert (to_agent["outcome"], to_agent["fallback"], to_agent["talk_s"]) == ("no_answer", "agent", 0)
    busy = cf.classify_cdr_row({**base, "accountcode": "lcfwd_7_USER_BUSY",
                                "billsec": "0", "hangup_cause": "NO_ANSWER"}, "hangup")
    assert (busy["outcome"], busy["fallback"]) == ("busy", "hangup")
    odd = cf.classify_cdr_row({**base, "accountcode": "lcfwd_7_WEIRD_THING",
                               "billsec": "0", "hangup_cause": "X"})
    assert odd["outcome"] == "failed" and odd["cause"] == "WEIRD_THING"
    assert cf.classify_cdr_row({**base, "accountcode": "", "billsec": "9"}) is None
    assert cf.classify_cdr_row({**base, "accountcode": "lcverify", "billsec": "9"}) is None


def test_sync_builds_records_once_and_counts_minutes_separately(acct, fs, tmp_path):
    nid = acct["ids"][acct["owner"]]["number"]
    cdr = tmp_path / "Master.csv"
    u1, u2, u3 = (f"fwd-test-{uuid.uuid4()}" for _ in range(3))
    cdr.write_text(
        _cdr_line(u1, f"lcfwd_{nid}", 61, 70, "NORMAL_CLEARING", answer="2026-11-05 10:00:09")
        + _cdr_line(u2, f"lcfwd_{nid}_NO_ANSWER", 30, 55, "NORMAL_CLEARING")
        + _cdr_line(u3, "", 99, 99, "NORMAL_CLEARING"))
    assert cf.sync_forwarded_calls(str(cdr))["inserted"] == 2
    assert cf.sync_forwarded_calls(str(cdr))["inserted"] == 0          # idempotent
    calls = tapi.list_forwarded_calls(owner=acct["owner"])
    assert {c["outcome"] for c in calls["calls"]} == {"connected", "no_answer"}
    summary = cf.month_summary(acct["owner"], "2026-11")
    # Only the CONNECTED call is a forwarded minute; 61 s rounds up per call.
    assert (summary["connected"], summary["talk_s"], summary["minutes"]) == (1, 61, 2)
