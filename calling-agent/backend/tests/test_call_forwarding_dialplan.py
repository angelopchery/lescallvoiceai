"""Conditional call forwarding (RUNBOOK §84) — the generated FreeSWITCH XML and
the one-time installer. Pure: no database, no FreeSWITCH, no network.

The dialplan is the part a test call cannot cheaply re-check every time, so
its shape is pinned here: well-formed XML, the loop guard, the caller's last
four digits, the ring time, and a fallback IDENTICAL to the live catch-all.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from shared import call_forwarding as cf  # noqa: E402

NUMBER = {"id": 42, "number": "+912264231897", "forward_ring_s": 20,
          "forward_confirm": True, "forward_fallback": "agent"}
PHONE = "+919876543210"
PROMPTS = {"line": "/p/line.wav", "ending": "/p/ending.wav",
           "press1": "/p/press1.wav", "digits": "/p/d_x_"}
INSTALLER = _REPO / "deploy" / "install_call_forwarding.sh"
LIVE_PUBLIC = Path("/etc/freeswitch/dialplan/public.xml")


def _render(number=NUMBER, prompts=PROMPTS):
    return cf.render_forward_dialplan(number, PHONE, "acme_plivo-231897", prompts)


def _extensions(xml: str):
    return ET.fromstring(xml).findall("extension")


def _actions(ext):
    cond = ext.findall("condition")[-1]
    return [(a.get("application"), a.get("data") or "") for a in cond.findall("action")]


def test_the_file_is_well_formed_with_two_extensions_in_order():
    exts = _extensions(_render())
    assert [e.get("name") for e in exts] == ["lescall_forward_2264231897_cli",
                                             "lescall_forward_2264231897"]


def test_both_extensions_match_the_number_and_refuse_its_own_caller_id():
    for ext in _extensions(_render()):
        conds = ext.findall("condition")
        assert conds[0].get("field") == "destination_number"
        assert conds[0].get("expression") == r"^(\+?\d*?)?2264231897$"
        assert conds[1].get("field") == "caller_id_number"
        assert conds[1].get("expression") == r"^(?!(\+?\d*?)?2264231897$)"


def test_the_loop_guard_regex_lets_customers_through_and_stops_ourselves():
    import re
    guard = re.compile(r"^(?!(\+?\d*?)?2264231897$)")
    assert guard.match("+919811112222") and guard.match("anonymous")
    for own in ("+912264231897", "912264231897", "2264231897", "02264231897"):
        assert not guard.match(own), own


def test_the_prompt_reads_the_callers_last_four_digits_only_when_there_are_some():
    with_cli, without = _extensions(_render())
    assert with_cli.findall("condition")[-1].get("expression") == r"(\d)(\d)(\d)(\d)$"
    confirm = [d for a, d in _actions(with_cli) if d.startswith("group_confirm_file=")][0]
    assert "/p/line.wav" in confirm and "/p/ending.wav" in confirm
    assert "/p/d_x_$1.wav!/p/d_x_$2.wav!/p/d_x_$3.wav!/p/d_x_$4.wav" in confirm
    assert confirm.endswith("/p/press1.wav")
    plain = [d for a, d in _actions(without) if d.startswith("group_confirm_file=")][0]
    assert "$1" not in plain and "/p/ending.wav" not in plain


def test_the_bridge_rings_the_phone_from_the_number_over_its_own_trunk():
    acts = _actions(_extensions(_render())[0])
    bridge = [d for a, d in acts if a == "bridge"][0]
    assert bridge.endswith("}sofia/gateway/acme_plivo-231897/919876543210")
    assert "origination_caller_id_number=912264231897" in bridge
    assert "leg_timeout=20" in bridge and "ignore_early_media=true" in bridge
    assert f"sched_hangup +{cf.MAX_FORWARD_CALL_S} ALLOTTED_TIMEOUT" in bridge
    names = [d.split("=", 1)[0] for a, d in acts if a == "set"]
    for var in ("accountcode", "ringback", "hangup_after_bridge", "continue_on_fail",
                "group_confirm_key", "group_confirm_file", "group_confirm_read_timeout"):
        assert var in names, var
    assert ("set", "group_confirm_key=1") in acts
    assert ("set", "accountcode=lcfwd_42") in acts
    i_bridge = acts.index(("bridge", bridge))
    assert acts[i_bridge + 1] == ("set", "accountcode=lcfwd_42_${originate_disposition}")


def test_press_one_off_means_no_confirm_variables():
    xml = cf.render_forward_dialplan({**NUMBER, "forward_confirm": False},
                                     PHONE, "acme", None)
    assert "group_confirm" not in xml


def test_ring_time_is_clamped_even_if_the_row_is_out_of_range():
    for raw, want in ((3, cf.RING_MIN_S), (999, cf.RING_MAX_S)):
        xml = _render({**NUMBER, "forward_ring_s": raw})
        assert f"leg_timeout={want}" in xml


def test_hangup_fallback_ends_the_call_instead_of_the_agent():
    acts = _actions(_extensions(_render({**NUMBER, "forward_fallback": "hangup"}))[0])
    assert acts[-1] == ("hangup", "NO_ANSWER")
    assert not any(a == "socket" for a, _ in acts)


def test_agent_fallback_is_identical_to_the_live_catch_all():
    """A call the person did not take must reach the agent exactly as every
    call does today — compared against the real public.xml, not a copy."""
    if not LIVE_PUBLIC.exists():
        pytest.skip("no live public.xml on this machine")
    root = ET.fromstring(f"<root>{LIVE_PUBLIC.read_text().split('<include>', 1)[1].rsplit('</include>', 1)[0]}</root>")
    catch_all = [e for e in root.iter("extension") if e.get("name") == "plivo_inbound"][0]
    live = [(a.get("application"), a.get("data") or "")
            for a in catch_all.find("condition").findall("action")]
    acts = _actions(_extensions(_render())[0])
    assert acts[-len(live):] == live


def test_values_are_xml_escaped():
    xml = cf.render_forward_dialplan(NUMBER, PHONE, 'bad"name', PROMPTS)
    ET.fromstring(xml)                                 # still parses
    assert 'bad&quot;name' in xml


# ── installer ──────────────────────────────────────────────────────────────

def _run_installer(conf: Path, *args, reload_reply="+OK [Success]"):
    env = {**os.environ, "FWD_TEST": "1", "FS_DIR": str(conf),
           "FWD_TEST_RELOAD": reload_reply}
    return subprocess.run(["bash", str(INSTALLER), *args], env=env,
                          capture_output=True, text=True)


@pytest.fixture()
def conf(tmp_path):
    (tmp_path / "dialplan").mkdir()
    if LIVE_PUBLIC.exists():
        shutil.copy(LIVE_PUBLIC, tmp_path / "dialplan" / "public.xml")
    else:
        (tmp_path / "dialplan" / "public.xml").write_text(
            '<include>\n  <context name="public">\n\n<extension name="plivo_inbound">\n'
            '  <condition field="destination_number" expression=".*"/>\n</extension>\n'
            '  </context>\n</include>\n')
    return tmp_path


def test_installer_adds_one_include_directly_above_the_catch_all(conf):
    r = _run_installer(conf)
    assert r.returncode == 0, r.stderr
    text = (conf / "dialplan" / "public.xml").read_text()
    assert text.count("public_forward/*.xml") == 1
    lines = text.splitlines()
    inc = next(i for i, l in enumerate(lines) if "public_forward/*.xml" in l)
    nxt = next(i for i in range(inc + 1, len(lines)) if lines[i].strip())
    assert '<extension name="plivo_inbound">' in lines[nxt]
    assert (conf / "dialplan" / "public_forward" / "00_placeholder.xml").exists()
    assert cf.infrastructure_missing(str(conf)) == []
    ET.fromstring(text)                                # still valid XML


def test_installer_is_idempotent(conf):
    _run_installer(conf)
    first = (conf / "dialplan" / "public.xml").read_text()
    r = _run_installer(conf)
    assert r.returncode == 0 and "already present" in r.stdout
    assert (conf / "dialplan" / "public.xml").read_text() == first


def test_installer_restores_the_backup_if_freeswitch_rejects_it(conf):
    before = (conf / "dialplan" / "public.xml").read_text()
    r = _run_installer(conf, reload_reply="-ERR [Error parsing]")
    assert r.returncode != 0 and "restoring the backup" in r.stderr
    assert (conf / "dialplan" / "public.xml").read_text() == before


def test_uninstall_removes_the_include_and_nothing_else(conf):
    before = (conf / "dialplan" / "public.xml").read_text()
    _run_installer(conf)
    r = _run_installer(conf, "--uninstall")
    assert r.returncode == 0, r.stderr
    after = (conf / "dialplan" / "public.xml").read_text()
    assert "public_forward" not in after
    assert [l for l in after.splitlines() if l.strip()] == \
           [l for l in before.splitlines() if l.strip()]
