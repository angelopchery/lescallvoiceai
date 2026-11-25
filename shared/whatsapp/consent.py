"""Did THIS call grant — or refuse — permission to WhatsApp the caller?

Agents ask for it three different ways today, so there are three ways in:

  call_promise     the `promise_whatsapp_media` branch action fired
                   (collected_data.flags.whatsapp_promised)
  call_slot        a slot whose name mentions WhatsApp was answered — yes,
                   or with a number (which then becomes the destination if
                   the playbook sends to "the number they gave")
  call_commitment  the post-call extractor recorded a `whatsapp` commitment
                   AND its quote actually occurs in the transcript — the same
                   quote gate the smart filter uses, so a model can assert a
                   promise but cannot invent one

A refusal beats all of them: a WhatsApp slot answered "no", or a DND request
anywhere on the call, means nothing is sent whatever else happened.

Pure functions over the call row + its commitments; no database access.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from shared.phone import digits_only, to_e164

_YES = {
    "yes", "y", "yeah", "yep", "sure", "ok", "okay", "haan", "han", "ha", "ji",
    "haan ji", "ha ji", "same", "same number", "this number", "yes this number",
    "true", "हाँ", "हां", "जी", "हाँ जी", "हां जी", "यही", "यही नंबर", "इसी नंबर",
    "હા", "હા જી",
}
_NO = {
    "no", "n", "nope", "nahi", "nahin", "false", "not on whatsapp",
    "no whatsapp", "नहीं", "नही", "ना", "ના", "નથી",
}


@dataclass
class Consent:
    granted: bool
    source: str = ""
    quote: str = ""
    given_phone: str = ""      # E.164, when the caller named a different number
    declined_reason: str = ""


def _collected(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("collected_data")
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _norm(value: Any) -> str:
    return re.sub(r"[\s.!।,]+", " ", str(value or "")).strip().lower()


def _utterance_for(cd: dict, field: str) -> str:
    """The caller's words that filled `field` (newest), for the evidence quote."""
    for entry in reversed(cd.get("audit_log") or []):
        if isinstance(entry, dict) and entry.get("field") == field and entry.get("utterance"):
            return str(entry["utterance"])[:400]
    return ""


def _whatsapp_slots(cd: dict) -> list[tuple[str, Any]]:
    state = cd.get("state") or {}
    return [(k, v) for k, v in state.items()
            if "whatsapp" in k.lower() and v not in (None, "")]


def _commitment_quote_ok(quote: str, transcript: str) -> bool:
    if not quote or not transcript:
        return False
    from shared.intelligence.extract import _normalize, quote_in_transcript, speech_runs

    return quote_in_transcript(quote, _normalize(transcript), speech_runs(transcript))


def detect(call: dict[str, Any], commitments: list[dict[str, Any]] | None = None) -> Consent | None:
    """Consent(granted=True/False) when the call said anything about it,
    None when WhatsApp never came up."""
    cd = _collected(call)
    flags = cd.get("flags") or {}

    if flags.get("dnd_requested"):
        return Consent(False, declined_reason="caller asked not to be contacted")

    given_phone = ""
    # RUNBOOK §72: the live-call share tracker records the other number the
    # caller gave for WhatsApp (digits) when they said it is not this one.
    alt = str(flags.get("whatsapp_alt_number") or "").strip()
    if len(digits_only(alt)) >= 10:
        given_phone = to_e164(digits_only(alt)) or ""
    slot_yes: tuple[str, str] | None = None
    for name, value in _whatsapp_slots(cd):
        text = _norm(value)
        digits = digits_only(str(value))
        if len(digits) >= 10:
            e164 = to_e164(digits)
            if e164:
                given_phone = e164
                slot_yes = (name, _utterance_for(cd, name) or str(value))
                continue
        if text in _NO or text.startswith(("no ", "nahi ", "नहीं ")):
            return Consent(False, source="call_slot", quote=_utterance_for(cd, name),
                           declined_reason="caller said no to WhatsApp on the call")
        if text in _YES or text.startswith(("yes", "haan", "हाँ", "हां")):
            slot_yes = (name, _utterance_for(cd, name) or str(value))

    commit_quote = ""
    transcript = call.get("transcript") or ""
    for c in commitments or []:
        if (c.get("kind") or "") != "whatsapp":
            continue
        q = (c.get("quote") or "").strip()
        if _commitment_quote_ok(q, transcript):
            commit_quote = q
            break

    if flags.get("whatsapp_promised"):
        return Consent(True, source="call_promise",
                       quote=commit_quote or (slot_yes[1] if slot_yes else ""),
                       given_phone=given_phone)
    if slot_yes:
        return Consent(True, source="call_slot", quote=slot_yes[1], given_phone=given_phone)
    if commit_quote:
        return Consent(True, source="call_commitment", quote=commit_quote)
    return None


SOURCE_LABELS = {
    "call_promise": "Agreed on the call",
    "call_slot": "Confirmed their WhatsApp number on the call",
    "call_commitment": "WhatsApp promised on the call",
    "reply_stop": "Replied STOP on WhatsApp",
    "manual": "Changed by a team member",
}
