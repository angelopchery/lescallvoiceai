"""
Native LLM tool calling for live calls — deploy/PLAN-llm-tool-calling.md.

Three modes, resolved per agent (`llm_config.enable_tools`) with a global
env kill switch (`LLM_TOOLS_MODE=off|shadow|on` overrides every agent):

- off:    nothing changes.
- shadow: the SPOKEN path is byte-identical (no tools on the main call —
          the P0 probe showed Gemini's compat shim returns tool calls
          INSTEAD of text, which would create silent turns). Instead the
          background slot extractor declares the tool roster: its
          record_customer_details proposals merge fill-only exactly like
          the old json_object extractor, and ACTION proposals are logged
          (`[TOOLS] shadow …`) for the P1 agreement metric.
- on:     tools are declared on the main streaming call; text still
          streams first; proposals execute at end-of-stream through the
          deterministic gates in ConversationRouter._execute_tool_proposals
          (fill-only slots, stage legality, turn-transaction deferral).

Compat quirk handled here (verified live 2026-07-29): the Gemini
OpenAI-compat shim streams `tool_calls` deltas with `index=None`, so
parallel calls fuse under index-keyed accumulation. ToolCallAccumulator
starts a new call whenever a delta carries a function name instead.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"
_MODES = (MODE_OFF, MODE_SHADOW, MODE_ON)


def resolve_tools_mode(agent_def) -> str:
    """Env kill switch first, then the per-agent YAML flag."""
    env = os.environ.get("LLM_TOOLS_MODE", "").strip().lower()
    if env in _MODES:
        return env
    mode = str(getattr(agent_def.llm_config, "enable_tools", MODE_SHADOW) or "").lower()
    return mode if mode in _MODES else MODE_SHADOW


_SLOT_HINT_MAX_CHARS = 160


def build_tool_declarations(slot_names: list[str],
                            slot_hints: dict[str, str] | None = None,
                            include_record: bool = True,
                            include_book: bool = True) -> list[dict]:
    """OpenAI tools schema. Minimal on purpose — every description line is
    prompt tokens on every turn (P0 measured ~flat TTFT with this roster).

    record_customer_details takes the AGENT'S slot names, so the runtime
    stays domain-free: a real-estate agent exposes budget/bhk, a clinic
    exposes symptom/insurer, purely from its YAML slots.

    `slot_hints` says what each slot MEANS, in the business's own words (its
    question, else its label). A bare name makes the model guess from the
    name, and it fills whatever is still empty: measured 2026-09-18, a caller
    saying "mostly references" had BOTH customer_channel and business_type
    recorded as 'Referrals', and the real business ("furniture showroom", one
    turn later) was then refused by the fill-only merge. The json extractor
    was given these hints on 2026-09-16 for exactly this reason; the tool
    roster — the path production actually runs — never was.
    """
    hints = slot_hints or {}
    slot_props: dict[str, dict] = {}
    for name in slot_names[:12]:
        prop: dict = {"type": "string"}
        hint = " ".join(str(hints.get(name) or "").split())
        if hint:
            prop["description"] = hint[:_SLOT_HINT_MAX_CHARS]
        slot_props[name] = prop
    decls = _all_tool_declarations(slot_props)
    # include_record=False: see LLMConfig.record_details_on_main_call.
    # include_book=False: the visit is already fixed or the call is closing —
    # every book_appointment the model issues now is refused by the legality
    # gate, and a refused tool-only turn costs a text re-ask whose goodbye
    # was then ignored: call 909 (2026-09-24, RUNBOOK §55.5) re-proposed the
    # same booking on FIVE consecutive closing turns and could not hang up.
    drop: set[str] = set()
    if not include_record:
        drop.add("record_customer_details")
    if not include_book:
        drop.add("book_appointment")
    return [d for d in decls if d["function"]["name"] not in drop]


def _all_tool_declarations(slot_props: dict) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": "record_customer_details",
            "description": "Record details the customer just stated.",
            "parameters": {"type": "object", "properties": slot_props},
        }},
        {"type": "function", "function": {
            "name": "book_appointment",
            "description": "Book the visit/appointment the customer agreed to.",
            "parameters": {"type": "object", "properties": {
                "date": {"type": "string"},
                "time": {"type": "string"}},
                "required": ["date", "time"]},
        }},
        {"type": "function", "function": {
            "name": "request_callback",
            "description": "Customer asked to be called back later.",
            "parameters": {"type": "object", "properties": {
                "when": {"type": "string"},
                "reason": {"type": "string"}},
                "required": ["when"]},
        }},
        {"type": "function", "function": {
            "name": "flag_do_not_call",
            "description": "Customer asked never to be called again.",
            "parameters": {"type": "object", "properties": {}},
        }},
        {"type": "function", "function": {
            "name": "end_call",
            "description": "The conversation is complete and should end.",
            "parameters": {"type": "object", "properties": {
                "reason": {"type": "string"}}},
        }},
    ]


@dataclass
class ToolProposal:
    name: str
    args: dict
    raw_args: str = ""
    valid: bool = True


@dataclass
class ToolCallAccumulator:
    """Reassembles streamed tool_calls deltas into proposals.

    Handles both spec-compliant streams (distinct `index` per call) and the
    Gemini shim (`index=None` on every delta): a delta carrying a function
    NAME always begins a new call; argument fragments append to the most
    recent call.
    """
    _calls: list[dict] = field(default_factory=list)

    def add_delta(self, tool_calls) -> None:
        for tc in tool_calls or []:
            fn = getattr(tc, "function", None)
            name = getattr(fn, "name", None) if fn else None
            args = getattr(fn, "arguments", None) if fn else None
            idx = getattr(tc, "index", None)
            if name:
                self._calls.append({"name": name, "args": args or "", "index": idx})
                continue
            target = None
            if idx is not None:
                for c in reversed(self._calls):
                    if c["index"] == idx:
                        target = c
                        break
            if target is None and self._calls:
                target = self._calls[-1]
            if target is not None and args:
                target["args"] += args

    @property
    def has_calls(self) -> bool:
        return bool(self._calls)

    def finish(self) -> list[ToolProposal]:
        out: list[ToolProposal] = []
        for c in self._calls:
            raw = c["args"].strip() or "{}"
            try:
                parsed = json.loads(raw)
                if not isinstance(parsed, dict):
                    raise ValueError("arguments not an object")
                out.append(ToolProposal(name=c["name"], args=parsed, raw_args=raw))
            except (ValueError, json.JSONDecodeError) as exc:
                # Visible failure — the whole point of tools over regex.
                logger.warning("[TOOLS] malformed arguments for %s: %r (%s)",
                               c["name"], raw[:200], exc)
                out.append(ToolProposal(name=c["name"], args={}, raw_args=raw,
                                        valid=False))
        self._calls.clear()
        return out


# What a model writes into a field when it has nothing to write. Stored, each
# of these reads as an ANSWER: measured 2026-09-18, the model recorded
# consultation_time='unscheduled' while merely OFFERING a call, a when_slot
# gate took that as "the caller named a time", and the conversation jumped to
# its closing stage; 'Pending' and 'pending confirmation' followed on the next
# two runs. A placeholder must leave the slot empty so it is still asked.
PLACEHOLDER_VALUES = frozenset({
    "null", "none", "unknown", "n/a", "nil", "-", "--", "tbd", "tba",
    "pending", "pending confirmation", "unscheduled", "not scheduled",
    "not yet scheduled", "not provided", "not specified", "not mentioned",
    "not stated", "not given", "not available", "unspecified", "undecided",
    "not decided", "to be decided", "to be confirmed", "no answer",
    "not answered", "not applicable",
    # A ROLE is not a name. Live 2026-09-24 (calls 903/904, Prakhar): the
    # tool wrote caller_name='caller' and 'Caller' on the turn the caller
    # had NOT given a name; the slot read as filled, the agent stopped
    # asking, and "Welcome, Baki!" went to a person whose name it never
    # actually had. Same for the honorific a model reaches for when it has
    # nothing else.
    "caller", "the caller", "customer", "the customer", "client",
    "the client", "user", "the user", "unknown caller", "anonymous",
    "sir", "madam", "ma'am", "maam", "no name", "unknown name",
})

# "Go ahead" is not an answer. The commonest first words on an outbound call
# are "हाँ जी बताओ" / "haan boliye" — yes, tell me. They carry no information,
# but said straight after a question they were stored as that question's
# answer (customer_channel='haan ji batao'), which then suppressed the real
# question. Yes/no words are deliberately NOT here: "No" is a real value for
# "are you happy with your leads?".
GO_AHEAD_TOKENS = frozenset({
    "batao", "bataiye", "bataiyega", "bataye", "batayein", "bolo", "boliye",
    "boliyega", "bole", "bolie", "kahiye", "kahie", "kaho",
    "बताओ", "बताइए", "बताइये", "बताएं", "बताएँ", "बताइएगा", "बोलो", "बोलिए",
    "बोलिये", "बोले", "बोलें", "कहिए", "कहिये", "कहो",
    "બોલો", "કહો", "બતાવો",
})


# Words that ride along with a go-ahead without adding information: "हाँ
# जल्दी बताओ थोड़ा" (yes, tell me quickly) was stored as
# customer_channel='abhi jaldi batao thoda' — the verb was recognised, the
# adverbs around it were not. Only ever consulted TOGETHER with a go-ahead
# verb, so "abhi" or "kya" on their own never make a value disappear.
GO_AHEAD_FILLERS = frozenset({
    "jaldi", "jldi", "jaldi se", "thoda", "thora", "zara", "jara", "abhi",
    "fatafat", "aap", "aage", "phir", "kya", "kuch",
    "जल्दी", "थोड़ा", "थोडा", "ज़रा", "जरा", "अभी", "फटाफट", "आप", "आगे",
    "फिर", "क्या", "कुछ",
})


def is_placeholder_value(value) -> bool:
    return " ".join(str(value or "").lower().split()).strip(" .") in PLACEHOLDER_VALUES


def clean_slot_args(args: dict) -> dict:
    """Same null-filtering discipline as the legacy json extractor."""
    return {k: str(v).strip() for k, v in args.items()
            if isinstance(v, (str, int, float)) and str(v).strip()
            and not is_placeholder_value(v)}


# Deterministic spoken confirmations for tool-only turns ("on" mode). The
# P0 probe showed the shim returns tool calls INSTEAD of text — without
# these, an executed action would be followed by dead air. Kept canned on
# purpose (platform doctrine: determinism beats generation for
# confirmations; these also hit the TTS PCM cache).
CONFIRM_LINES: dict[str, dict[str, str]] = {
    "book_appointment": {
        "en": "Done — I have noted {date} at {time}.",
        "hi": "जी, {date} को {time} नोट कर लिया।",
        "gu": "સરસ, {date} ના રોજ {time} નોંધી લીધું.",
    },
    "request_callback": {
        "en": "Sure — we will call you back {when}.",
        "hi": "ठीक है, हम आपको {when} कॉल करेंगे।",
        "gu": "ચોક્કસ, અમે તમને {when} કૉલ કરીશું.",
    },
    "flag_do_not_call": {
        "en": "Understood — we will not call you again.",
        "hi": "समझ गया, हम आपको दोबारा कॉल नहीं करेंगे।",
        "gu": "સમજાઈ ગયું, અમે તમને ફરી કૉલ નહીં કરીએ.",
    },
    "record_customer_details": {
        "en": "Noted, thank you.",
        "hi": "जी, नोट कर लिया।",
        "gu": "જી, નોંધી લીધું.",
    },
}


_MONTHS = ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December")


def _speakable_when(value: str) -> str:
    """Machine date/time → words a TTS voice can say.

    The model hands book_appointment ISO strings, and this canned line read
    them to the caller verbatim: "जी, 2025-09-19 को 16:00 नोट कर लिया।"
    (measured 2026-09-18). Anything that is not ISO passes through untouched —
    "Tuesday" and "3pm" were already speakable.
    """
    import re as _re
    v = str(value or "").strip()
    m = _re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", v)
    if m and 1 <= int(m[2]) <= 12:
        return f"{int(m[3])} {_MONTHS[int(m[2]) - 1]}"
    m = _re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)(?::[0-5]\d)?", v)
    if m:
        h, mins = int(m[1]), m[2]
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12} {suffix}" if mins == "00" else f"{h12}:{mins} {suffix}"
    return v


def confirm_line(name: str, lang: str, args: dict) -> str:
    lines = CONFIRM_LINES.get(name) or {}
    template = lines.get((lang or "en")[:2]) or lines.get("en") or ""
    try:
        return template.format(**{k: _speakable_when(v) if k in ("date", "time")
                                  else str(v) for k, v in args.items()})
    except (KeyError, IndexError):
        return template.replace("{date}", "").replace("{time}", "").replace(
            "{when}", "").strip(" —-")
