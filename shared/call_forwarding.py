"""Conditional call forwarding (RUNBOOK §84).

A number is either ANSWERED BY ITS AGENT (`forward_mode='agent'`, the default
and every number's behaviour before this module existed) or FORWARDED
(`forward_mode='forward'`): FreeSWITCH first rings one of the owner's own,
verified phones from that number, over the number's own trunk. The person
hears "Call on your <line> line, from a number ending 4 3 2 1. Press 1 to
accept." and, on 1, is bridged straight to the caller. If nobody takes the
call, the number's `forward_fallback` decides: the agent answers it exactly as
it would have before, or the call ends.

How it reaches FreeSWITCH
-------------------------
`public.xml` routes every inbound call with one catch-all (`plivo_inbound`,
`.*` → AI socket). The installer (`deploy/install_call_forwarding.sh`) adds ONE
line above it that includes `dialplan/public_forward/*.xml`. This module
writes one file there per number that is switched to forwarding, and deletes
it when the number goes back to agent mode. A number with no file therefore
takes exactly today's road — nothing else in the call path changes, and
deleting the file is the whole rollback for that number.

A forwarded call never reaches the AI pipeline, so it has no `calls` row and
no `usage_ledger` row. Its record is built afterwards from FreeSWITCH's CDR
(`sync_forwarded_calls`, run by the workers); forwarded minutes are counted
there, separately from AI minutes.

The loop guard
--------------
Every generated extension refuses a call whose caller ID IS the number itself.
Two things present that caller ID: our own forward coming back (the person's
SIM is set to forward missed calls to this same number), and the sentinel's
nightly loopback probe. Both must reach the agent, never be forwarded again —
the first would ring the person in a loop, the second would ring them at
08:30 every morning.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import logging
import os
import re
import secrets
import urllib.request
from pathlib import Path

from shared import freeswitch_config as fsc
from shared.phone import dialplan_expression, digits_only, significant_digits, to_e164

log = logging.getLogger("call_forwarding")

FORWARD_DIR = "public_forward"          # <FS_CONF_DIR>/dialplan/public_forward
PROMPT_DIR = "lescall_prompts"          # <FS_CONF_DIR>/lescall_prompts
PLACEHOLDER_FILE = "00_placeholder.xml"

RING_MIN_S, RING_MAX_S = 10, 45
DEFAULT_RING_S = 25
CONFIRM_READ_TIMEOUT_MS = 8000          # time to press 1 after the prompt
# One forwarded call can last at most this long. With India-only, verified
# phones there is no third party to defraud, so this — not a daily cap — is
# the backstop against a leg that never hangs up.
MAX_FORWARD_CALL_S = 3600
MAX_CONTACTS = 5
VERIFY_TTL_S = 600
VERIFY_CALLS_PER_DAY = 3
VERIFY_MAX_TRIES = 5
LINE_NAME_MAX = 40
DEFAULT_LINE_NAME = "LesCall"

MODES = ("agent", "forward")
FALLBACKS = ("agent", "hangup")

# Indian ringback (400+450 Hz cadence), generated while the person's phone
# rings — the caller's leg is not answered until they accept, so no minutes
# are counted on it and the caller hears an ordinary ring.
RINGBACK = "%(400,200,400,450);%(400,2000,400,450)"

PROMPT_SPEAKER = os.environ.get("FORWARD_PROMPT_SPEAKER", "shubh")
PROMPT_LANGUAGE = "en-IN"
PROMPT_MODEL = "bulbul:v3"
SARVAM_TTS_URL = "https://api.sarvam.ai/text-to-speech"

ENDING_TEXT = "from a number ending"
PRESS1_TEXT = "Press 1 to accept this call."
VERIFY_INTRO_TEXT = "This is LesCall. Your verification code is"
VERIFY_AGAIN_TEXT = "Once again, your code is"
VERIFY_BYE_TEXT = "Thank you. Goodbye."
_DIGIT_WORDS = ("zero", "one", "two", "three", "four",
                "five", "six", "seven", "eight", "nine")

# +91 and a 10-digit national number. Mobile vs landline is NOT decidable
# from the digits (Bangalore landlines 80xxxxxxxx overlap the 8-series
# mobiles), so the rule is "Indian", and the code call proves it is a real
# phone the owner can answer. Toll-free / premium (1800, 1860 …) are 11
# digits and never reach this shape.
_INDIAN_NUMBER = re.compile(r"^\+91[1-9]\d{9}$")
_LINE_NAME_OK = re.compile(r"^[A-Za-z0-9 .&'\-]*$")
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class ForwardingError(Exception):
    """A refusal the API turns into an HTTP error with this message."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def indian_number(raw) -> str:
    """`raw` as +91XXXXXXXXXX, or ForwardingError. Indian numbers only."""
    e164 = to_e164(str(raw or "").strip())
    if not _INDIAN_NUMBER.match(e164):
        raise ForwardingError(
            "Only Indian phone numbers can receive forwarded calls "
            "(+91 and a 10-digit number).")
    return e164


def refuse_own_lines(db, number: str) -> None:
    """A LesCall number is never a forward target: the call would arrive back
    on the platform with our own caller ID, the loop guard would hand it to an
    agent, and the person would never be rung."""
    if db.execute("SELECT 1 FROM phone_numbers WHERE sig_digits=?",
                  (significant_digits(number),)).fetchone():
        raise ForwardingError(
            "That is a LesCall number. Forward to a phone a person answers.")


def clean_line_name(raw) -> str:
    name = re.sub(r"\s+", " ", str(raw or "")).strip()
    if len(name) > LINE_NAME_MAX:
        raise ForwardingError(f"The line name can be at most {LINE_NAME_MAX} characters.")
    if not _LINE_NAME_OK.match(name):
        raise ForwardingError(
            "The line name is read aloud, so use letters, numbers and spaces only.")
    return name


def clamp_ring(value) -> int:
    try:
        ring = int(value)
    except (TypeError, ValueError):
        raise ForwardingError("Ring time must be a whole number of seconds.")
    if not RING_MIN_S <= ring <= RING_MAX_S:
        raise ForwardingError(f"Ring time must be {RING_MIN_S}-{RING_MAX_S} seconds.")
    return ring


def mask(number: str) -> str:
    d = digits_only(number)
    return f"••••{d[-4:]}" if len(d) >= 4 else "••••"


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

def _base(conf_dir: str | None) -> Path:
    return Path(conf_dir or fsc.FS_CONF_DIR)


def forward_dir(conf_dir: str | None = None) -> Path:
    return _base(conf_dir) / "dialplan" / FORWARD_DIR


def prompt_dir(conf_dir: str | None = None) -> Path:
    return _base(conf_dir) / PROMPT_DIR


def forward_file_name(number: dict) -> str:
    sig = significant_digits(number["number"]) or digits_only(number["number"])
    return f"forward_{sig}.xml"


def infrastructure_missing(conf_dir: str | None = None) -> list[str]:
    """What the installer has not done yet. Empty = forwarding can go live.

    Checked before every write, because a file in a folder FreeSWITCH never
    reads would leave the dashboard saying "forwarding on" while every call
    still goes to the agent.
    """
    missing = []
    if not forward_dir(conf_dir).is_dir():
        missing.append(f"folder dialplan/{FORWARD_DIR}")
    if not prompt_dir(conf_dir).is_dir():
        missing.append(f"folder {PROMPT_DIR}")
    public_xml = _base(conf_dir) / "dialplan" / "public.xml"
    try:
        if f"{FORWARD_DIR}/*.xml" not in public_xml.read_text(encoding="utf-8"):
            missing.append("the include line in dialplan/public.xml")
    except OSError:
        missing.append("dialplan/public.xml (unreadable)")
    return missing


# ---------------------------------------------------------------------------
# voice prompts (Sarvam, cached as files FreeSWITCH plays)
# ---------------------------------------------------------------------------

def _voice_tag() -> str:
    return hashlib.sha1(
        f"{PROMPT_MODEL}|{PROMPT_SPEAKER}|{PROMPT_LANGUAGE}".encode()).hexdigest()[:8]


def prompt_file(text: str, conf_dir: str | None = None) -> Path:
    h = hashlib.sha1(text.encode()).hexdigest()[:16]
    return prompt_dir(conf_dir) / f"p_{_voice_tag()}_{h}.wav"


def digit_prefix(conf_dir: str | None = None) -> str:
    """Digit N is `<prefix>N.wav` — the dialplan appends $1..$4 at call time."""
    return str(prompt_dir(conf_dir) / f"d_{_voice_tag()}_")


def _sarvam_key() -> str:
    try:
        from backend.app.voice.key_rotator import current_key   # dashboard path
        key = current_key()
        if key:
            return key
    except Exception:
        pass
    key = os.environ.get("SARVAM_API_KEY", "").strip()
    if key:
        return key
    env = Path(__file__).resolve().parents[1] / "calling-agent" / ".env"
    try:
        m = re.search(r"^SARVAM_API_KEY=(\S+)", env.read_text(encoding="utf-8"), re.M)
        return m.group(1) if m else ""
    except OSError:
        return ""


def synthesize(text: str) -> bytes:
    """One WAV from Sarvam's REST endpoint. Raises on any failure."""
    key = _sarvam_key()
    if not key:
        raise ForwardingError("The voice service key is not configured.", status=503)
    req = urllib.request.Request(
        SARVAM_TTS_URL,
        data=json.dumps({
            "text": text,
            "target_language_code": PROMPT_LANGUAGE,
            "speaker": PROMPT_SPEAKER,
            "model": PROMPT_MODEL,
            "pace": 1.0,
            "speech_sample_rate": 16000,
        }).encode(),
        headers={"api-subscription-key": key, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = json.loads(resp.read())
    return base64.b64decode(body["audios"][0])


def _ensure_file(path: Path, text: str) -> Path:
    if path.exists() and path.stat().st_size > 1000:
        return path
    try:
        wav = synthesize(text)
    except ForwardingError:
        raise
    except Exception as exc:
        log.warning("prompt synthesis failed for %r: %r", text, exc)
        raise ForwardingError(
            "Could not prepare the voice prompt just now. Please try again.",
            status=503)
    if len(wav) <= 1000:
        raise ForwardingError("The voice service returned an empty prompt.", status=503)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(wav)
    try:
        tmp.chmod(0o664)
    except PermissionError:
        pass
    tmp.replace(path)
    return path


def ensure_digits(conf_dir: str | None = None) -> str:
    prefix = digit_prefix(conf_dir)
    for n, word in enumerate(_DIGIT_WORDS):
        _ensure_file(Path(f"{prefix}{n}.wav"), word)
    return prefix


def line_text(line_name: str) -> str:
    return f"Call on your {line_name or DEFAULT_LINE_NAME} line,"


def ensure_confirm_prompts(line_name: str, conf_dir: str | None = None) -> dict:
    return {
        "line": str(_ensure_file(prompt_file(line_text(line_name), conf_dir),
                                 line_text(line_name))),
        "ending": str(_ensure_file(prompt_file(ENDING_TEXT, conf_dir), ENDING_TEXT)),
        "press1": str(_ensure_file(prompt_file(PRESS1_TEXT, conf_dir), PRESS1_TEXT)),
        "digits": ensure_digits(conf_dir),
    }


# ---------------------------------------------------------------------------
# the dialplan (pure rendering — unit-tested)
# ---------------------------------------------------------------------------

def _ai_fallback_actions() -> list[tuple[str, str]]:
    """Byte-for-byte the actions of public.xml's plivo_inbound catch-all, so a
    call the person did not take reaches the agent exactly as it always has
    (same DID on the channel → same agent, same caller memory)."""
    return [
        ("answer", ""),
        ("sleep", "1000"),
        ("record_session", "/tmp/dialplan_test.wav"),
        ("socket", f"{fsc.AI_SOCKET} async full"),
    ]


def _forward_actions(number: dict, contact_number: str, trunk_name: str,
                     confirm_file: str | None) -> list[tuple[str, str]]:
    from shared.fs_originate import dialable_destination

    nid = int(number["id"])
    ring = int(number.get("forward_ring_s") or DEFAULT_RING_S)
    ring = max(RING_MIN_S, min(RING_MAX_S, ring))
    did_digits = digits_only(number["number"])
    dest = dialable_destination(contact_number)
    acts: list[tuple[str, str]] = [
        ("set", f"lescall_forward={nid}"),
        # The CDR row of this (caller's) leg carries the accountcode, which is
        # how sync_forwarded_calls finds forwarded calls and their outcome.
        ("set", f"accountcode=lcfwd_{nid}"),
        ("set", f"ringback={RINGBACK}"),
        ("set", "hangup_after_bridge=true"),
        ("set", "continue_on_fail=true"),
    ]
    if confirm_file:
        acts += [
            ("set", "group_confirm_key=1"),
            ("set", f"group_confirm_file={confirm_file}"),
            ("set", f"group_confirm_read_timeout={CONFIRM_READ_TIMEOUT_MS}"),
        ]
    dial_vars = [
        f"origination_caller_id_number={did_digits}",
        "ignore_early_media=true",
        f"leg_timeout={ring}",
        f"execute_on_answer='sched_hangup +{MAX_FORWARD_CALL_S} ALLOTTED_TIMEOUT'",
    ]
    acts.append(("bridge",
                 "{" + ",".join(dial_vars) + "}" + f"sofia/gateway/{trunk_name}/{dest}"))
    # Reached only when the person did not take the call (a connected call
    # ends at hangup_after_bridge). Record why, then fall back.
    acts.append(("set", f"accountcode=lcfwd_{nid}_${{originate_disposition}}"))
    if (number.get("forward_fallback") or "agent") == "hangup":
        acts.append(("hangup", "NO_ANSWER"))
    else:
        acts += _ai_fallback_actions()
    return acts


def _extension(name: str, conditions: list[tuple[str, str]],
               actions: list[tuple[str, str]]) -> str:
    lines = [f'  <extension name="{fsc._attr(name)}">']
    for field, expr in conditions[:-1]:
        lines.append(f'    <condition field="{fsc._attr(field)}" '
                     f'expression="{fsc._attr(expr)}"/>')
    field, expr = conditions[-1]
    lines.append(f'    <condition field="{fsc._attr(field)}" '
                 f'expression="{fsc._attr(expr)}">')
    for app, data in actions:
        if data:
            lines.append(f'      <action application="{fsc._attr(app)}" '
                         f'data="{fsc._attr(data)}"/>')
        else:
            lines.append(f'      <action application="{fsc._attr(app)}"/>')
    lines.append("    </condition>")
    lines.append("  </extension>")
    return "\n".join(lines)


def render_forward_dialplan(number: dict, contact_number: str, trunk_name: str,
                            prompts: dict | None) -> str:
    """dialplan/public_forward/forward_<did>.xml for one forwarding number.

    Two extensions, tried in order:
      1. the caller has a number → the prompt reads its last four digits;
      2. no usable caller ID → the same forward with the shorter prompt.
    Both refuse a call presenting this number as its caller ID (the loop guard
    in the module docstring); such a call falls through to plivo_inbound.

    `prompts` is None when press-1 is off.
    """
    sig = significant_digits(number["number"]) or digits_only(number["number"])
    dest_cond = ("destination_number", dialplan_expression(number["number"]))
    guard_cond = ("caller_id_number", rf"^(?!(\+?\d*?)?{sig}$)")

    confirm_cli = confirm_plain = None
    if prompts:
        dp = prompts["digits"]
        confirm_cli = "file_string://" + "!".join([
            prompts["line"], "silence_stream://200", prompts["ending"],
            f"{dp}$1.wav", f"{dp}$2.wav", f"{dp}$3.wav", f"{dp}$4.wav",
            "silence_stream://300", prompts["press1"],
        ])
        confirm_plain = "file_string://" + "!".join([
            prompts["line"], "silence_stream://300", prompts["press1"],
        ])

    header = (
        f"<!-- LesCall call forwarding: number id {int(number['id'])} "
        f"({mask(number['number'])}) -> phone {mask(contact_number)}. "
        f"Generated by shared/call_forwarding.py; edits are overwritten. -->"
    )
    with_cli = _extension(
        f"lescall_forward_{sig}_cli",
        [dest_cond, guard_cond, ("caller_id_number", r"(\d)(\d)(\d)(\d)$")],
        _forward_actions(number, contact_number, trunk_name, confirm_cli))
    without_cli = _extension(
        f"lescall_forward_{sig}",
        [dest_cond, guard_cond],
        _forward_actions(number, contact_number, trunk_name, confirm_plain))
    return "<include>\n" + header + "\n" + with_cli + "\n" + without_cli + "\n</include>\n"


# ---------------------------------------------------------------------------
# applying a number's setting to FreeSWITCH
# ---------------------------------------------------------------------------

def _reloadxml() -> dict:
    try:
        from shared.fs_esl import fs_api
        body = fs_api("reloadxml", timeout=15).strip()
        return {"ok": not body.startswith("-ERR"), "output": body[:200]}
    except Exception as exc:
        return {"ok": False, "output": repr(exc)[:200]}


def _load(db, number_id: int) -> dict | None:
    row = db.execute(
        "SELECT p.*, t.name AS trunk_name, t.owner AS trunk_owner, "
        "t.config_status AS trunk_status, "
        "c.number AS contact_number, c.verified_at AS contact_verified_at, "
        "c.owner AS contact_owner "
        "FROM phone_numbers p "
        "LEFT JOIN trunks t ON t.id = p.trunk_id "
        "LEFT JOIN forward_contacts c ON c.id = p.forward_contact_id "
        "WHERE p.id=?", (int(number_id),)).fetchone()
    return dict(row) if row else None


def _off_reason(n: dict) -> str | None:
    """Why this number must NOT forward right now, or None if it may."""
    if (n.get("forward_mode") or "agent") != "forward":
        return "agent mode"
    if not n.get("forward_contact_id") or not n.get("contact_number"):
        return "no phone selected"
    if (n.get("contact_owner") or "") != (n.get("owner") or ""):
        return "the phone belongs to another account"
    if not n.get("contact_verified_at"):
        return "the phone is not verified"
    if not n.get("trunk_name") or (n.get("trunk_owner") or "") != (n.get("owner") or ""):
        return "the number has no trunk of its own"
    if n.get("trunk_status") != "applied":
        return "the number's trunk is not applied"
    if n.get("config_status") == "disconnected":
        return "the number is disconnected"
    return None


def remove_forward_file(number: dict, conf_dir: str | None = None,
                        reload: bool = True) -> dict:
    path = forward_dir(conf_dir) / forward_file_name(number)
    existed = path.exists()
    if existed:
        path.unlink()
    out = {"removed": str(path), "existed": existed}
    if existed and reload:
        out["reload"] = _reloadxml()
    return out


def sync_number(number_id: int, conf_dir: str | None = None,
                reload: bool = True) -> dict:
    """Make FreeSWITCH match the database for one number. Idempotent.

    Returns {"state": "on"|"off", "reason": ...}. Raises ForwardingError when
    the number SHOULD forward but cannot (installer not run, prompt synthesis
    failed) — the caller must tell the owner, never report "on".
    """
    from shared.db.database import get_db
    with get_db() as db:
        n = _load(db, number_id)
    if not n:
        return {"state": "off", "reason": "number not found"}
    reason = _off_reason(n)
    if reason:
        removed = remove_forward_file(n, conf_dir, reload=reload)
        return {"state": "off", "reason": reason, "file_removed": removed["existed"]}

    missing = infrastructure_missing(conf_dir)
    if missing:
        raise ForwardingError(
            "Call forwarding is not switched on for this server yet ("
            + ", ".join(missing) + "). Your setting is saved and will start "
            "working once it is.", status=503)
    prompts = (ensure_confirm_prompts(n.get("forward_line_name") or "", conf_dir)
               if n.get("forward_confirm", True) else None)
    xml = render_forward_dialplan(n, n["contact_number"], n["trunk_name"], prompts)
    path = forward_dir(conf_dir) / forward_file_name(n)
    if not (path.exists() and path.read_text(encoding="utf-8") == xml):
        fsc._write_shared(path, xml)
        if reload:
            r = _reloadxml()
            if not r["ok"]:
                raise ForwardingError(
                    "The phone system did not accept the change "
                    f"({r['output']}). Forwarding is NOT live.", status=503)
    return {"state": "on", "reason": None, "phone": mask(n["contact_number"])}


def sync_numbers(number_ids, conf_dir: str | None = None) -> dict:
    """sync_number for several numbers; never raises (used after Apply etc.)."""
    out = {}
    for nid in number_ids:
        try:
            out[int(nid)] = sync_number(int(nid), conf_dir)
        except ForwardingError as exc:
            out[int(nid)] = {"state": "error", "reason": str(exc)}
        except Exception as exc:                        # pragma: no cover
            log.exception("forward sync failed for number %s", nid)
            out[int(nid)] = {"state": "error", "reason": repr(exc)}
    return out


# ---------------------------------------------------------------------------
# phone verification (a call that reads a 4-digit code)
# ---------------------------------------------------------------------------

def _code_hash(contact_id: int, code: str) -> str:
    return hashlib.sha256(f"lescall-forward:{int(contact_id)}:{code}".encode()).hexdigest()


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _as_dt(value) -> dt.datetime | None:
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def _caller_identity(db, owner: str) -> dict:
    """The owner's own number + trunk to place the code call from."""
    row = db.execute(
        "SELECT p.number, t.name AS trunk_name FROM phone_numbers p "
        "JOIN trunks t ON t.id = p.trunk_id "
        "WHERE p.owner=? AND t.owner=? AND t.config_status='applied' "
        "AND COALESCE(p.config_status,'') <> 'disconnected' "
        "ORDER BY p.id LIMIT 1", (owner, owner)).fetchone()
    if not row:
        raise ForwardingError(
            "Connect one of your numbers first — the code call is placed from it.")
    return dict(row)


def verification_command(caller_number: str, trunk_name: str, dest: str,
                         code: str, conf_dir: str | None = None) -> str:
    """The originate that rings `dest` and reads `code` twice."""
    from shared.fs_originate import dialable_destination
    prefix = digit_prefix(conf_dir)
    digits = [f"{prefix}{c}.wav" for c in code]
    intro = str(prompt_file(VERIFY_INTRO_TEXT, conf_dir))
    again = str(prompt_file(VERIFY_AGAIN_TEXT, conf_dir))
    bye = str(prompt_file(VERIFY_BYE_TEXT, conf_dir))
    play = "!".join(["silence_stream://800", intro, *digits, "silence_stream://700",
                     again, *digits, "silence_stream://500", bye])
    chan = ",".join([
        f"origination_caller_id_number={digits_only(caller_number)}",
        "ignore_early_media=true",
        "originate_timeout=30",
        "accountcode=lcverify",
    ])
    return ("originate {" + chan + "}"
            f"sofia/gateway/{trunk_name}/{dialable_destination(dest)} "
            f"&playback(file_string://{play})")


def start_verification(contact_id: int, owner: str,
                       conf_dir: str | None = None) -> dict:
    """Issue a fresh code and return the originate to place.

    Placing the call is the caller's job (`place_call`), so the API can run it
    in the background: an originate blocks until the phone answers.
    """
    from shared.db.database import get_db
    missing = [m for m in infrastructure_missing(conf_dir) if PROMPT_DIR in m]
    if missing:
        raise ForwardingError(
            "Call forwarding is not switched on for this server yet.", status=503)
    today = dt.datetime.now(_IST).date()
    with get_db() as db:
        c = db.execute("SELECT * FROM forward_contacts WHERE id=? AND owner=?",
                       (int(contact_id), owner)).fetchone()
        if not c:
            raise ForwardingError("Phone not found.", status=404)
        c = dict(c)
        if c.get("verified_at"):
            raise ForwardingError("This phone is already verified.", status=409)
        day = c.get("verify_calls_day")
        count = c.get("verify_calls_count") or 0
        if str(day or "") != today.isoformat():
            count = 0
        if count >= VERIFY_CALLS_PER_DAY:
            raise ForwardingError(
                f"We have already called this phone {VERIFY_CALLS_PER_DAY} times "
                "today. Please try again tomorrow.", status=429)
        ident = _caller_identity(db, owner)

    # Prompts before the code is issued: a synthesis failure must not burn
    # one of the day's three attempts.
    ensure_digits(conf_dir)
    for text in (VERIFY_INTRO_TEXT, VERIFY_AGAIN_TEXT, VERIFY_BYE_TEXT):
        _ensure_file(prompt_file(text, conf_dir), text)

    code = f"{secrets.randbelow(10000):04d}"
    with get_db() as db:
        db.execute(
            "UPDATE forward_contacts SET verify_code_hash=?, verify_expires_at=?, "
            "verify_tries=0, verify_calls_day=?, verify_calls_count=?, "
            "updated_at=now() WHERE id=? AND owner=?",
            (_code_hash(contact_id, code),
             (_now() + dt.timedelta(seconds=VERIFY_TTL_S)).isoformat(),
             today.isoformat(), count + 1, int(contact_id), owner))
    cmd = verification_command(ident["number"], ident["trunk_name"], c["number"],
                               code, conf_dir)
    log.info("forward verification call for %s to %s (attempt %d today)",
             owner, mask(c["number"]), count + 1)
    return {"command": cmd, "phone": mask(c["number"]),
            "expires_in_s": VERIFY_TTL_S,
            "calls_left_today": VERIFY_CALLS_PER_DAY - count - 1}


def place_call(command: str) -> str:
    """Run an originate; returns FreeSWITCH's reply (never raises)."""
    try:
        from shared.fs_esl import fs_api
        return fs_api(command, timeout=45).strip()[:200]
    except Exception as exc:
        log.warning("forward verification originate failed: %r", exc)
        return f"-ERR {exc!r}"[:200]


def check_verification(contact_id: int, owner: str, code: str) -> dict:
    from shared.db.database import get_db
    code = re.sub(r"\D", "", str(code or ""))
    with get_db() as db:
        c = db.execute("SELECT * FROM forward_contacts WHERE id=? AND owner=?",
                       (int(contact_id), owner)).fetchone()
        if not c:
            raise ForwardingError("Phone not found.", status=404)
        c = dict(c)
    if c.get("verified_at"):
        return {"verified": True}
    expires = _as_dt(c.get("verify_expires_at"))
    if not c.get("verify_code_hash") or not expires or expires < _now():
        raise ForwardingError("That code has expired. Ask for a new call.", status=410)
    if (c.get("verify_tries") or 0) >= VERIFY_MAX_TRIES:
        raise ForwardingError("Too many wrong codes. Ask for a new call.", status=429)
    if len(code) == 4 and secrets.compare_digest(
            _code_hash(contact_id, code), c["verify_code_hash"]):
        with get_db() as db:
            db.execute(
                "UPDATE forward_contacts SET verified_at=now(), verify_code_hash=NULL, "
                "verify_expires_at=NULL, verify_tries=0, updated_at=now() "
                "WHERE id=? AND owner=?", (int(contact_id), owner))
        log.info("forward phone %s verified for %s", mask(c["number"]), owner)
        return {"verified": True}
    # Committed BEFORE raising: get_db rolls back on an exception inside it.
    with get_db() as db:
        db.execute("UPDATE forward_contacts SET verify_tries=verify_tries+1, "
                   "updated_at=now() WHERE id=? AND owner=?", (int(contact_id), owner))
    left = VERIFY_MAX_TRIES - (c.get("verify_tries") or 0) - 1
    raise ForwardingError(
        f"That code is not right. {left} tr{'y' if left == 1 else 'ies'} left."
        if left > 0 else "That code is not right. Ask for a new call.")


# ---------------------------------------------------------------------------
# forwarded-call records from the CDR (workers)
# ---------------------------------------------------------------------------

_ACCT_RE = re.compile(r"^lcfwd_(\d+)(?:_([A-Z_]+))?$")
_DISPOSITION_OUTCOME = {
    "NO_ANSWER": "no_answer",
    "NO_USER_RESPONSE": "no_answer",
    "USER_BUSY": "busy",
    "CALL_REJECTED": "rejected",
    "USER_NOT_REGISTERED": "failed",
    "SUBSCRIBER_ABSENT": "no_answer",
}


def classify_cdr_row(row: dict, fallback_setting: str = "agent") -> dict | None:
    """One CDR row (dict keyed like usage_metering._CDR_COLS) → a
    forwarded_calls record, or None if it is not a forwarded call."""
    m = _ACCT_RE.match((row.get("accountcode") or "").strip())
    if not m:
        return None
    nid, disposition = int(m.group(1)), m.group(2)
    try:
        billsec = int(row.get("billsec") or 0)
        duration = int(row.get("duration") or 0)
    except ValueError:
        billsec = duration = 0
    cause = (row.get("hangup_cause") or "").strip()
    rec = {
        "number_id": nid,
        "fs_uuid": (row.get("uuid") or "").strip(),
        "did": digits_only(row.get("destination_number")),
        "caller": digits_only(row.get("caller_id_number")) or "anonymous",
        "started_at": (row.get("start_stamp") or "").strip() or None,
        "answered_at": (row.get("answer_stamp") or "").strip() or None,
        "ended_at": (row.get("end_stamp") or "").strip() or None,
        "ring_s": max(0, duration - billsec),
    }
    if disposition is None:
        # The bridge ended the call: either the person took it, or the caller
        # hung up while it rang (the rest of the dialplan never ran).
        if billsec > 0:
            rec.update(outcome="connected", talk_s=billsec, fallback="none", cause=cause)
        else:
            rec.update(outcome="caller_hung_up" if cause == "ORIGINATOR_CANCEL" else "failed",
                       talk_s=0, fallback="none", cause=cause)
    else:
        # A disposition we have not mapped (e.g. the person answered but did not
        # press 1 — its exact code is confirmed in the first live test) stays
        # "failed" with the raw code in `cause`, never a guessed label.
        rec.update(outcome=_DISPOSITION_OUTCOME.get(disposition, "failed"),
                   talk_s=0, cause=disposition,
                   fallback=("agent" if billsec > 0 else
                             ("hangup" if fallback_setting == "hangup" else "none")))
    return rec


def _cdr_rows(path: str):
    import csv
    from shared.usage_metering import _CDR_COLS
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        for cells in csv.reader(f):
            if len(cells) < len(_CDR_COLS):
                continue
            row = dict(zip(_CDR_COLS, cells))
            if row.get("accountcode", "").startswith("lcfwd_"):
                yield row


def sync_forwarded_calls(cdr_path: str | None = None) -> dict:
    """Insert a forwarded_calls row for every forwarded call in the CDR.
    Idempotent (fs_uuid is unique). Quietly does nothing before migration 0032."""
    from shared.db.database import get_db
    from shared.usage_metering import CDR_PATH
    path = cdr_path or CDR_PATH
    stats = {"seen": 0, "inserted": 0}
    try:
        rows = list(_cdr_rows(path))
    except FileNotFoundError:
        return {**stats, "note": "no CDR file"}
    if not rows:
        return stats
    try:
        with get_db() as db:
            known = {r["fs_uuid"] for r in db.execute(
                "SELECT fs_uuid FROM forwarded_calls WHERE fs_uuid = ANY(?)",
                ([r["uuid"] for r in rows],)).fetchall()}
            numbers = {int(r["id"]): dict(r) for r in db.execute(
                "SELECT p.id, p.owner, p.forward_fallback, p.forward_contact_id, "
                "c.number AS contact_number FROM phone_numbers p "
                "LEFT JOIN forward_contacts c ON c.id = p.forward_contact_id").fetchall()}
    except Exception as exc:
        if "does not exist" in str(exc):
            return {**stats, "note": "migration 0032 not applied"}
        raise
    for row in rows:
        stats["seen"] += 1
        if row["uuid"] in known:
            continue
        num = numbers.get(int(_ACCT_RE.match(row["accountcode"]).group(1)), {})
        rec = classify_cdr_row(row, num.get("forward_fallback") or "agent")
        if not rec or not rec["fs_uuid"]:
            continue
        with get_db() as db:
            db.execute(
                "INSERT INTO forwarded_calls (owner, fs_uuid, number_id, did, caller, "
                "contact_id, contact_number, outcome, cause, fallback, ring_s, talk_s, "
                "started_at, answered_at, ended_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT (fs_uuid) DO NOTHING",
                (num.get("owner"), rec["fs_uuid"], rec["number_id"], rec["did"],
                 rec["caller"], num.get("forward_contact_id"),
                 digits_only(num.get("contact_number")) or None,
                 rec["outcome"], rec["cause"], rec["fallback"], rec["ring_s"],
                 rec["talk_s"], rec["started_at"], rec["answered_at"], rec["ended_at"]))
        stats["inserted"] += 1
    return stats


def month_summary(owner: str, month: str | None = None) -> dict:
    """Forwarded calls this month — reported SEPARATELY from AI minutes."""
    from shared.db.database import get_db
    from shared.usage_metering import display_minutes
    month = month or dt.datetime.now(_IST).strftime("%Y-%m")
    with get_db() as db:
        rows = [dict(r) for r in db.execute(
            "SELECT outcome, talk_s FROM forwarded_calls WHERE owner=? "
            "AND to_char(started_at, 'YYYY-MM')=?", (owner, month)).fetchall()]
    connected = [int(r["talk_s"] or 0) for r in rows if r["outcome"] == "connected"]
    return {"month": month, "calls": len(rows), "connected": len(connected),
            "talk_s": sum(connected), "minutes": display_minutes(connected)}


def main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sync", action="store_true",
                    help="build forwarded_calls rows from the CDR")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.sync:
        print("forwarding:", json.dumps(sync_forwarded_calls()))


if __name__ == "__main__":
    main()
