"""
Pramukh Group — proactive outbound calling agent.

Drives conversation through a structured appointment booking flow:
  GREETING -> INTEREST_CONFIRM -> (INTEREST_RECONFIRM) -> LOCATION
  -> PROPERTY_TYPE -> SCHEDULING -> CONFIRMATION -> CLOSING

Agent goes first with bilingual greeting ("Hello, Namaste!").
User's language is detected from their response and mirrored.
Interest is confirmed before advancing to the booking flow.
Uses parallel LLM execution, filler responses, and deterministic
shortcuts for low latency.
"""

from __future__ import annotations

import json
import asyncio
import os
import re
import time
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from openai import AsyncOpenAI

from .memory import ConversationMemory, BookingData, CallState
from . import prompt_cache as _prompt_cache

# Ask the provider to append a usage block to the stream. Until 2026-08-11 the
# platform recorded ZERO LLM tokens, so half the cost of a call was
# unmeasurable — this is what makes it measurable. Two behaviours it drags in,
# both handled below: the usage chunk arrives with an EMPTY `choices` list
# (indexing it unguarded raises IndexError mid-turn), and a provider that
# rejects the parameter must not be allowed to fail the turn.
# ENABLE_LLM_USAGE_TELEMETRY=false restores the previous blind behaviour.
_LLM_USAGE_TELEMETRY = (
    os.environ.get("ENABLE_LLM_USAGE_TELEMETRY", "true").lower() == "true")


def _is_param_rejection(exc: Exception) -> bool:
    """True only when the provider rejected the REQUEST SHAPE, not the request.

    The distinction is load-bearing. `stream_options` is an optional telemetry
    ask, so an unsupported-parameter error should disable it and retry — but a
    429, a timeout or a 5xx says nothing about parameter support, and treating
    one as such disables cost telemetry for the life of the process on the
    first quota blip. That is exactly what happened on the first live call
    after deploy (2026-08-11): the free tier's 20-requests-per-DAY cap was
    already spent, every turn 429'd, and the first one switched telemetry off
    permanently — so a call that made 7 LLM requests recorded none.

    Deliberately narrow: only a 400/422 whose message actually names the
    parameter counts. Anything unrecognised is NOT a param rejection, so the
    request takes the normal fallback path with telemetry left intact.
    """
    status = getattr(exc, "status_code", None)
    if status not in (400, 422):
        return False
    msg = str(exc).lower()
    return ("stream_options" in msg or "stream options" in msg
            or "include_usage" in msg)


def _credit_tracker():
    """The credit tracker singleton, or None if unavailable.

    Imported lazily and defensively: credit_tracker reaches for `shared/` via a
    sys.path insert that only holds in the deployed layouts, and a conversation
    engine that cannot be constructed because a COST LEDGER failed to import
    would be a self-inflicted outage. Cost telemetry is strictly optional to
    the call path.
    """
    try:
        from .credit_tracker import SarvamCreditTracker
        return SarvamCreditTracker.get()
    except Exception:  # pragma: no cover - import-environment dependent
        return None
from .stt.post_processor import match_location, match_bhk
from ..agent_definition import AgentDefinition, get_pramukh_agent

# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------

STAGE_GREETING = "GREETING"
STAGE_DISCOVERY = "DISCOVERY"
STAGE_PROJECT_MATCH = "PROJECT_MATCH"
STAGE_INVITE_VISIT = "INVITE_VISIT"
STAGE_CONFIRMATION = "CONFIRMATION"
STAGE_CLOSING = "CLOSING"

_STAGE_ORDER = {
    STAGE_GREETING: 0,
    STAGE_DISCOVERY: 1,
    STAGE_PROJECT_MATCH: 2,
    STAGE_INVITE_VISIT: 3,
    STAGE_CONFIRMATION: 4,
    STAGE_CLOSING: 5,
}

from .stage_roles import derive_stage_role as _derive_stage_role
from .llm_tools import GO_AHEAD_TOKENS as _GO_AHEAD_TOKENS
from .llm_tools import GO_AHEAD_FILLERS as _GO_AHEAD_FILLERS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_RESPONSE_LENGTH = 400
LLM_TIMEOUT = 5.0
LLM_MAX_TOKENS = 160
# Lowered 2.0 → 1.0s: the LLM's first sentence typically lands at ~1.8s, so a
# 2.0s gate almost never fired and the caller heard dead air. At 1.0s a short
# acknowledgment ("One moment…") plays ~1s after the caller stops, masking the
# LLM round-trip and cutting *perceived* turn latency without touching the
# turn-end wait or the model itself.
LLM_STREAM_TIMEOUT_S = 7.0
INTENT_CLASSIFY_TIMEOUT_S = 1.0

# First-turn ambiguous-language handling: the LLM classify round-trip used to
# run SEQUENTIALLY before the main LLM on the caller's very first utterance.
# Default off = first turn trusts STT's detection (already mirrored into
# state.language by the pipeline) / agent default; later turns keep the LLM.
LANG_LLM_CLASSIFY_FIRST_TURN = os.environ.get(
    "LANG_LLM_CLASSIFY_FIRST_TURN", "false").lower() == "true"

logger = logging.getLogger(__name__)

_MARKDOWN_RE = re.compile(r"[*#`~_\[\]()>|]")
# A size/configuration token is never a product name, however often owners
# and the Copilot doc-miner park one in knowledge_base.known_projects /
# catalog. Guards the project lock and the recommendation gate: "3 BHK" once
# latched as the caller's chosen "project" for a whole live call.
_CONFIG_TOKEN_RE = re.compile(
    r"^\s*\d+(?:\.\d+)?\s*(?:bhk|rk|bed|beds|bedroom|bedrooms)\s*$",
    re.IGNORECASE)
# LLM end-of-call signal (call_flow.termination.llm_end_call): the model
# appends [END_CALL] after its goodbye line. Must be stripped BEFORE
# _validate_response — the markdown filter would mangle it into spoken
# "ENDCALL". Bracketed form is case-insensitive; the bare form must be
# upper-case so natural "end call" prose is never eaten.
_END_CALL_RE = re.compile(r"(?i:\[\s*end[\s_-]*call\s*\])|\bEND[_-]?CALL\b")
_EMOJI_RE = re.compile(
    "[\U0001f600-\U0001f64f\U0001f300-\U0001f5ff\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff\U00002702-\U000027b0\U0001fa00-\U0001fa6f"
    "\U0001fa70-\U0001faff\U00002600-\U000026ff]+",
    re.UNICODE,
)

# Gujarati copula markers in Devanagari (STT sometimes writes Gujarati in
# Devanagari when language_code=hi-IN). These copulas are exclusively Gujarati
# and NEVER appear as word-stems in Hindi.
#
# Boundary rule (2026-07-30 incident): Python's \w EXCLUDES combining marks
# (matras, virama), so `(?<!\w)` fired MID-WORD after a matra — everyday
# Hindi "अच्छे" matched marker "छे" (the virama ् before छ read as a word
# edge) and one such hit flipped a live Hindi call to Gujarati. A marker is
# a word only when not touching ANY Devanagari codepoint, marks included.
_DEVA_BLOCK = "ऀ-ॿ"
_STRONG_GU_DEVA_RE = re.compile(
    rf"(?<![{_DEVA_BLOCK}])(?:छे|छो|छु|छुं|छीए|केम|तमे|नथी|हसे|जोइए)"
    rf"(?![{_DEVA_BLOCK}])"
)

# Weak (ambiguous) Gujarati-in-Devanagari markers — 2+ boundary-clean hits
# required. Same Devanagari-block boundaries as the strong tier: the old
# substring scan matched "हती" inside Hindi "चाहती" etc. Longer alternatives
# listed before their prefixes ("जोईए" before "जोई") for clarity — the regex
# engine's backtracking would resolve them anyway.
_WEAK_GU_DEVA_RE = re.compile(
    rf"(?<![{_DEVA_BLOCK}])(?:"
    r"जोईए|जोइये|जोवुं|जोवा|जोई|"
    r"माने|तमने|अमने|अमारा|तमारा|"
    r"बरोबर|सारुं|सारु|माजामा|"
    r"केमछो|केम छो|"
    r"शुं|क्यां|आवजो|हतु|हती|अत्यारे|"
    r"बतावो|जणावो"
    rf")(?![{_DEVA_BLOCK}])"
)


@dataclass
class EngineResult:
    text: str
    is_shortcut: bool
    intent: str


class ConversationState:
    def __init__(self) -> None:
        self.last_intent: str | None = None
        self.turn_count: int = 0
        self.is_closing: bool = False
        self.language: str = "hi"
        self.language_history: list[str] = []
        self.stage: str = STAGE_GREETING
        self.lang_confidence: float = 1.0
        self.lang_source: str = "default"
        self.filler_used: bool = False
        self.llm_cancelled: bool = False
        self.parallel_execution: bool = False
        self.objection_handled: bool = False
        self.language_locked: bool = False
        self.language_lock_turns: int = 0
        self.decline_count: int = 0
        # Discovery tracking (inbound flow)
        self.discovery_turns: int = 0
        self.purpose: str | None = None          # "self_use" / "investment" / "exploring"
        self.project_recommended: str | None = None  # project name mentioned to caller




# Universal objection/deferral phrases — fallback when an agent defines no
# objection_patterns of its own (English + romanized Hindi core set). Without
# this, agents with an empty YAML list silently lose objection handling.
_DEFAULT_OBJECTION_PATTERNS = (
    "not interested", "busy right now", "call later", "call me later",
    "call back later", "don't need", "dont need", "no time", "don't want",
    "stop calling", "not looking", "no need", "wrong number",
    "remove my number", "talk later", "later please", "maybe later",
    "some other time", "another time", "not right now",
    "baad mein baat", "baad me baat", "phir baat", "phir baat karenge",
    "abhi nahi", "abhi nai", "abhi busy", "fursat nahi",
    # Native script (RUNBOOK §37). Every phrase above is Latin, so a Hindi or
    # Gujarati caller declining in their own language matched NOTHING and the
    # decline was never counted. _DECLINE_INDIC_RE below catches the
    # word-order variants; these fixed phrases are here so an owner reading
    # the pattern list in the builder SEES that both scripts are covered, and
    # so objection_patterns_mode 'extend' hands them to every agent.
    "इंटरेस्ट नहीं", "इंटरेस्टेड नहीं", "दिलचस्पी नहीं", "रुचि नहीं",
    "ज़रूरत नहीं", "जरूरत नहीं", "नहीं चाहिए", "मत कीजिए", "मत करो",
    "अभी नहीं", "अभी बिज़ी", "अभी व्यस्त", "समय नहीं", "टाइम नहीं",
    "बाद में बात", "फिर बात", "बाद में कॉल",
    "રસ નથી", "જરૂર નથી", "નથી જોઈતું", "અત્યારે નહીં", "અત્યારે નથી",
    "સમય નથી", "પછી વાત", "પછી કૉલ",
)


# "Stop calling me" — the compliance opt-out, distinct from an objection or a
# "call me later" preference. Every pattern requires the never/again/stop/
# remove semantics, so scheduling talk ("don't call me today", "abhi call mat
# karna", "call me after 8") never trips it. Pure + module-level: unit-tested
# against both directions in tests.
_DND_PATTERNS = tuple(re.compile(p, re.I) for p in (
    r"\b(?:do\s*n[o']?t|never)\s+(?:call|phone|contact)\s+me\s+(?:again|any\s*more|ever)",
    r"\bdo\s*n[o']?t\s+(?:call|phone|ring)\s+(?:me\s+)?(?:again|any\s*more|back)\b",
    r"\bnever\s+(?:call|phone|contact)\s+me\b",
    r"\bstop\s+(?:calling|phoning|contacting)\b",
    r"\b(?:remove|delete|take)\s+(?:my|me)\s+(?:number|off)\b",
    r"\btake\s+me\s+off\s+(?:your|the|this)\s+list\b",
    r"\bunsubscribe\b",
    r"\bdo\s+not\s+disturb\b",
    # Hindi (romanized): dobara/phir kabhi + call/phone + mat/na, band karo
    r"\b(?:dobara|dubara|phir\s+kabhi)\s+(?:call|phone|फ़ोन|फोन|कॉल)\s*(?:mat|na|नहीं|मत)",
    r"\b(?:call|phone)\s+karna\s+band\s+kar",
    r"\bmera\s+number\s+(?:hata|delete|nikal)",
    # Hindi (Devanagari)
    r"(?:दोबारा|दुबारा|दुबारा|फिर\s*से|आगे\s*से|अब\s*कभी|दुबारा\s*से)\s*"
    r"(?:मुझे\s*)?(?:कॉल|फोन|फ़ोन|कॉल\s*मत)\s*(?:मत|न(?:हीं)?\s*कर)",
    r"फिर\s*कभी\s*(?:कॉल|फोन|फ़ोन)\s*(?:मत|न)",
    r"(?:कॉल|फोन|फ़ोन)\s*(?:मत|न(?:हीं)?)\s*(?:करना|करो|करिए|कीजिए)\s*"
    r"(?:दोबारा|दुबारा|फिर\s*से|आगे\s*से|कभी)",
    r"मुझे\s*(?:बार\s*बार\s*)?परेशान\s*मत",
    r"(?:कॉल|फोन|फ़ोन)\s*करना\s*बंद\s*कर",
    r"मेरा\s*नंबर\s*(?:हटा|निकाल|डिलीट)",
    # Gujarati
    r"ફરી\s*(?:ફોન|કૉલ|કોલ)\s*(?:ન\s*કર|કરશો\s*નહીં|ના\s*કર)",
    r"મારો\s*નંબર\s*(?:કાઢી|હટાવ|ડિલીટ)",
))


def _detect_dnd_request(text: str) -> bool:
    """True when the caller asked us to stop contacting them — ever."""
    t = (text or "").strip()
    if len(t) < 8:  # too short to carry the semantics; avoids noise tokens
        return False
    return any(p.search(t) for p in _DND_PATTERNS)


# ── Declines in the caller's OWN script (RUNBOOK §37) ──────────────────────
# _DEFAULT_OBJECTION_PATTERNS above is 100% Latin — "not interested", "abhi
# nahi", "baad mein baat". A Hindi or Gujarati caller declining in their own
# language matched NONE of it, so `decline_count` never incremented,
# `decline_threshold` was never reached, and the decline farewell / DND path
# never ran. Measured 2026-09-08: "मुझे इंटरेस्ट नहीं है" and "अभी बात नहीं
# कर सकता" both classified `negation`, and "बाद में बात करते हैं" classified
# `general` — the agent kept pitching a caller who had already said no, then
# said goodbye twice on the way out.
#
# Word-window regexes rather than fixed phrases, because Hindi word order
# moves freely: the live caller said "इंटरेस्टेड हूं नहीं", not the dictionary
# "इंटरेस्टेड नहीं हूं". Every alternative REQUIRES an interest / need / time
# word next to the negation, so a plain "no" answer to a qualification
# question ("नहीं, हमारे पास एम्प्लॉइज नहीं है") stays a `negation` and does
# not count as a decline.
_DECLINE_INDIC_RE = re.compile(
    # interest ↔ negation, either order
    r"(?:इंटरेस्ट|इन्ट्रेस्ट|दिलचस्पी|रुचि|इच्छा)[^\n]{0,15}(?:नहीं|नही|नहि)"
    r"|(?:नहीं|नही|नहि)[^\n]{0,15}(?:इंटरेस्ट|इन्ट्रेस्ट|दिलचस्पी|रुचि|इच्छा)"
    # need / time / availability + negation
    r"|(?:ज़रूरत|जरूरत|जरुरत|आवश्यकता|समय|टाइम|टाईम|फुर्सत|फ़ुर्सत)"
    r"[^\n]{0,12}(?:नहीं|नही|नहि)"
    # "can't talk (right now)"
    r"|बात[^\n]{0,10}(?:नहीं|नही)[^\n]{0,10}(?:कर|हो)"
    r"|(?:नहीं|नही)[^\n]{0,10}चाहिए"
    # deferral — "let's talk later / call later"
    r"|(?:बाद\s*में|बाद\s*मे|फिर\s*कभी|अगली\s*बार)[^\n]{0,12}"
    r"(?:बात|कॉल|call|फ़ोन|फोन)"
    # Gujarati
    r"|(?:રસ|જરૂર|સમય|ટાઇમ|ટાઈમ|ઈચ્છા)[^\n]{0,12}(?:નથી|નહીં|નહિ)"
    r"|(?:નથી|નહીં|નહિ)[^\n]{0,12}(?:રસ|જરૂર)"
    r"|(?:પછી|બીજી\s*વાર)[^\n]{0,12}(?:વાત|કૉલ|કોલ)"
)

# Indic letters (Devanagari + Gujarati). Used as a hand-rolled word boundary:
# `\b` cannot separate two Indic words because every one of these code points
# is a `\w`, so a bare `"हाँ" in text` test is a SUBSTRING test.
_INDIC_LETTER = "ऀ-ॿ઀-૿"
# Affirmation words that must match as WHOLE words. "हाँ" (yes) is a substring
# of कहाँ (where), यहाँ (here), वहाँ (there) and जहाँ (wherever); "जी" of जीवन
# and राजी. The old `any(w in lower ...)` test therefore read every Hindi
# "where are you calling from?" as a YES — measured live on 2026-09-08
# (RUNBOOK §37): it made a stage's `when_intent: negation` branch unreachable
# and the agent re-asked the same question three times.  Longest alternatives
# first so "हाँजी" is preferred over "हाँ".
_AFFIRM_INDIC_RE = re.compile(
    rf"(?<![{_INDIC_LETTER}])"
    r"(?:हाँजी|हांजी|હાજી|हाँ|हां|ठीक|जी|હા|બરાબર|જી)"
    rf"(?![{_INDIC_LETTER}])"
)


# "No time TO DO SOMETHING" is a complaint about the caller's business, not a
# request to end the call. Every time/need alternative above fires on a time
# word next to a negation, which is right for "मेरे पास टाइम नहीं है" and wrong
# for the single most useful sentence a discovery call can hear. Measured
# 2026-09-18 on a marketing-agency agent: asked for their biggest challenge,
# the prospect said "फॉलो अप करने का टाइम नहीं मिलता" — no time to follow up
# with leads, exactly what the agency sells — and was answered with the
# canned deferral "no problem, what time would be convenient for you?", with
# decline_count bumped one step closer to a hang-up.
#
# Shape, not vocabulary, separates the two: time FOR AN ACTIVITY ("…ने का /
# के लिए टाइम", "no time to follow up") versus time, full stop. The activity
# phrase is removed before the decline checks run, so any OTHER decline in the
# same sentence ("…और इंटरेस्ट भी नहीं है") still counts. When the activity is
# the call itself — "बात करने का टाइम नहीं", "no time to talk" — it is a real
# deferral and is left alone.
_ACTIVITY_TIME_RE = re.compile(
    r"\S+ने\s+(?:का|के\s+लिए|को)\s+(?:टाइम|टाईम|समय|फुर्सत|फ़ुर्सत|वक्त|वक़्त)"
    r"|\b\w+ne\s+(?:ka|ke\s+liye|ko)\s+(?:time|samay|fursat|waqt|vakt)\b"
    r"|\bno\s+time\s+(?:to|for)\s+(?!talk|speak|chat|discuss|this|that|it\b|"
    r"you|a\s+call|the\s+call|calls?\b|now\b|any)\w+",
    re.IGNORECASE)
_CALL_ACTIVITY_RE = re.compile(
    r"बात|बोल|सुन|कॉल|फोन|फ़ोन"
    r"|\b(?:baat|bol\w*|sun\w*|call\w*|phone|talk\w*|speak\w*|chat\w*)\b",
    re.IGNORECASE)


def _strip_activity_time_complaints(text: str) -> str:
    """`text` without its "no time to <do something in my business>" phrases."""
    def _sub(m: "re.Match") -> str:
        window = text[max(0, m.start() - 14):m.end()]
        return m.group(0) if _CALL_ACTIVITY_RE.search(window) else " "
    return _ACTIVITY_TIME_RE.sub(_sub, text or "")


def detect_intent(
    text: str,
    stage: str = STAGE_GREETING,
    *,
    location_aliases: frozenset[str] = frozenset(),
    objection_patterns: list[str] | tuple[()] = (),
    time_patterns: list[str] | tuple[()] = (),
) -> str:
    """Classify user input into a domain intent. Fast and deterministic."""
    if not text or not text.strip():
        return "general"

    lower = text.lower().strip()

    # Objection — highest priority. Decided on the text WITHOUT its "no time
    # to <do something>" phrases (see _strip_activity_time_complaints); every
    # other intent below still reads the caller's full words.
    _obj_text = _strip_activity_time_complaints(text)
    _obj_lower = _obj_text.lower().strip()
    if any(p in _obj_lower for p in (objection_patterns or _DEFAULT_OBJECTION_PATTERNS)):
        return "objection"
    if any(w in _obj_lower for w in ["दिलचस्पी नहीं", "बिज़ी", "ज़रूरत नहीं",
                                      "રસ નથી", "વ્યસ્ત", "જરૂર નથી"]):
        return "objection"
    if _DECLINE_INDIC_RE.search(_obj_text):
        return "objection"
    # Deferral patterns — clear "later / not now" in Devanagari and Gujarati script.
    # Keep this narrow to avoid false positives.
    if any(w in _obj_text for w in [
        "अभी नहीं", "अभी नही", "अभी बिज़ी", "अभी busy",
        "फिर बात करेंगे",
        "અત્યારે નહીં", "અત્યારે નથી",
    ]):
        return "objection"

    # Language switch request — highest priority after objection.
    # Catches romanized, Devanagari, and Gujarati script requests.
    _lang_switch_phrases = [
        "hindi me", "hindi mai", "hindi mein", "hindi boliye", "hindi bolo",
        "hindi me baat", "hindi mai baat", "hindi mein baat",
        # Gujarati postposition "ma" (in) — "hindi ma", "english ma"
        "hindi ma", "hindi maa", "hindi ma vaat", "hindi ma baat",
        "hindi ma bol", "hindi ma karo",
        "english ma", "english maa", "english ma vaat", "english ma baat",
        "english me", "english mai", "english mein", "english boliye", "english bolo",
        "english me baat", "english mai baat", "english mein baat",
        "gujarati ma", "gujarati maa", "gujarati mai", "gujarati mein", "gujarati boliye", "gujarati bolo",
        "gujarati ma vaat", "gujarati mai baat", "gujarati ma baat",
        "gu ma bolo", "gu mein bolo", "gu ma baat", "gu maa bolo",
        "bolo gujarati", "bol gujarati", "bolav gujarati",
        "speak hindi", "speak english", "speak gujarati",
        "talk in hindi", "talk in english", "talk in gujarati",
        "speak in hindi", "speak in english", "speak in gujarati",
        "switch to hindi", "switch to english", "switch to gujarati",
        "in english", "in hindi", "in gujarati",
        # Common short asks — "english please", "english only", etc.
        "english please", "hindi please", "gujarati please",
        "only english", "only hindi", "only gujarati",
        "english only", "hindi only", "gujarati only",
        "can you speak english", "can you speak hindi", "can you speak gujarati",
        "can you talk english", "can you talk hindi", "can you talk gujarati",
        "could you speak english", "could you speak hindi", "could you speak gujarati",
        "talk english", "talk hindi", "talk gujarati",
        # Romanized question forms — "kya aap hindi mein baat kar sakte hain"
        "kya aap hindi", "kya aap english", "kya aap gujarati",
        "kya hindi", "kya english", "kya gujarati",
        # "vaat" = Gujarati for "talk/speech" — covers "hindi ma vaat karvi che"
        "hindi vaat", "english vaat", "gujarati vaat",
        "hindi ma vaat", "english ma vaat",
    ]
    if any(p in lower for p in _lang_switch_phrases):
        return "language_switch"
    # Standalone language name as the whole message — "gujarati", "hindi", "english"
    if re.match(r"^\s*(gujarati|hindi|english)\s*[?.!]?\s*$", lower):
        return "language_switch"
    # Devanagari script — all spellings of language names + में/बोलो/बात
    _lang_switch_devanagari = [
        "हिंदी में", "हिन्दी में", "हिंदी बोल", "हिन्दी बोल",
        "हिंदी में बात", "हिन्दी में बात",
        "अंग्रेज़ी में", "अंग्रेजी में", "इंग्लिश में",
        "अंग्रेज़ी बोल", "अंग्रेजी बोल", "इंग्लिश बोल",
        "अंग्रेज़ी में बात", "अंग्रेजी में बात", "इंग्लिश में बात",
        "गुजराती में", "गुजराती बोल", "गुजराती में बात",
        "गुजराती में बोलो", "हिंदी में बोलो",
        # Gujarati-syntax "ma" postposition in Devanagari — hi-IN STT
        # renders "Gujarati ma vaat karo" as "गुजराती मा वात करो".
        "गुजराती मा", "गुजराती माँ",
        "हिंदी मा", "हिंदी माँ",
        "इंग्लिश मा", "इंग्लिश माँ",
    ]
    if any(p in text for p in _lang_switch_devanagari):
        return "language_switch"
    # Broader Devanagari catch-all: language name + any postposition or verb.
    # Catches STT variants that spell the postposition differently.
    if re.search(
        r"(?:\u0917\u0941\u091C\u0930\u093E\u0924\u0940|\u0939\u093F\u0902\u0926\u0940|\u0939\u093F\u0928\u094D\u0926\u0940|\u0905\u0902\u0917\u094D\u0930\u0947\u091C\u0940|\u0906\u0928\u094D\u0917\u094D\u0930\u0947\u091C\u0940|\u0907\u0902\u0917\u094D\u0932\u093F\u0936)"
        r"[^\n]{0,12}"
        r"(?:\u092E\u093E|\u092E\u093E\u0901|\u092E\u0947\u0902|\u092E\u0947|\u092E\u0947\u0901|\u0935\u093E\u0924|\u092C\u093E\u0924|\u092C\u094B\u0932|\u0915\u0930\u094B|kariye|karo)",
        text,
    ):
        return "language_switch"
    # Gujarati script — comprehensive list including common colloquial forms
    if any(w in text for w in [
        "ગુજરાતીમાં", "ગુજરાતી માં", "અંગ્રેજીમાં", "હિન્દીમાં",
        "ઇંગ્લિશમાં", "ગુજરાતી બોલ", "ગુજ બોલ",
        "અંગ્રેજી બોલ", "હિન્દી બોલ", "ઇંગ્લિશ બોલ",
        "ગુજ માં", "ગુ માં",
        "ગુજ મા", "ગુ મા", "ગુજરાતી મા",
    ]):
        return "language_switch"
    # Catch-all: any mention of a language name with "baat/bol/bolo" nearby
    if re.search(r"(?:english|hindi|gujarati|इंग्लिश|अंग्रेज़ी|अंग्रेजी|हिंदी|हिन्दी|गुजराती|ગુજરાતી|અંગ્રેજી|હિન્દી|ઇંગ્લિશ).{0,15}(?:बात|बोल|bol|baat|bolo|boliye|vaat|vat|વાત|બોલ)", lower):
        return "language_switch"
    if re.search(r"(?:बात|बोल|bol|baat|bolo|boliye|vaat|vat|વાત|બોલ).{0,15}(?:english|hindi|gujarati|इंग्लिश|अंग्रेज़ी|अंग्रेजी|हिंदी|हिन्दी|गुजराती|ગુજરાતી|અંગ્રેજી|હિન્દી|ઇંગ્લિશ)", lower):
        return "language_switch"

    # Two language names co-mentioned within a 30-char window — strong signal
    # the user is talking about switching ("english instead of hindi", "switch
    # from hindi to english", "hindi or english", etc.).
    if re.search(
        r"(english|hindi|gujarati).{1,30}(english|hindi|gujarati)",
        lower,
    ):
        return "language_switch"

    # Negation / restriction patterns — user implicitly asks to switch by
    # saying "I don't know X" / "X नहीं आती" / "સિર્ફ Y" / "only Y". These
    # must be classified as language_switch so the deterministic handler
    # responds (instead of the LLM, which sometimes hallucinates refusals
    # like "मैं हिंदी में ही बात करूँगा"). The destination language is
    # resolved by _extract_requested_language's last-position fallback.
    _lang_neg_lat = r"(?:english|hindi|gujarati|inglish|angrezi)"
    _lang_neg_dev = r"(?:इंग्लिश|अंग्रेज़ी|अंग्रेजी|हिंदी|हिन्दी|गुजराती)"
    _lang_neg_guj = r"(?:ગુજરાતી|અંગ્રેજી|હિન્દી|ઇંગ્લિશ)"
    _lang_neg_any = f"(?:{_lang_neg_lat[3:-1]}|{_lang_neg_dev[3:-1]}|{_lang_neg_guj[3:-1]})"
    # English: "don't know/speak X", "can't speak X", "only know/speak X"
    if re.search(
        rf"(?:don'?t|do not|can'?t|cannot|only|just)\s+(?:know|speak|talk|understand)\s+{_lang_neg_lat}",
        lower,
    ):
        return "language_switch"
    # Negation word near a language name (either order, ~25 char window).
    # Covers Devanagari नहीं, Gujarati નથી, romanized nahi/nahin.
    _neg_words = r"(?:नहीं|नहि|नही|न्ही|નથી|નહિ|નહી|નહીં|nahi|nahin|nai)"
    if re.search(rf"{_lang_neg_any}[^\n]{{0,25}}{_neg_words}", text, re.IGNORECASE):
        return "language_switch"
    if re.search(rf"{_neg_words}[^\n]{{0,25}}{_lang_neg_any}", text, re.IGNORECASE):
        return "language_switch"
    # Restriction word ("only" in various scripts) near a language name.
    _only_words = r"(?:सिर्फ|केवल|खाली|बस|ફક્ત|માત્ર|સિર્ફ|બસ|ખાલી|sirf|sirff|fakt|fakta|kewal|khali)"
    if re.search(rf"{_only_words}[^\n]{{0,20}}{_lang_neg_any}", text, re.IGNORECASE):
        return "language_switch"
    if re.search(rf"{_lang_neg_any}[^\n]{{0,20}}{_only_words}", text, re.IGNORECASE):
        return "language_switch"

    # Location selection
    loc = match_location(lower)
    if loc:
        return "location_selection"
    if location_aliases and any(w in lower for w in location_aliases):
        return "location_selection"

    # BHK / property type
    bhk = match_bhk(lower)
    if bhk:
        return "property_type"
    if re.search(r"\d\s*bhk|\d\s*bed", lower, re.IGNORECASE):
        return "property_type"

    # Time preference
    if any(p in lower for p in time_patterns):
        return "time_preference"

    # Affirmation
    # Checked FIRST among the yes/no family: a "no" used as a discourse
    # marker ("no no, we can talk right now") must not read as a refusal.
    if _GO_AHEAD_RE.search(lower):
        return "affirmation"
    if re.search(r"\b(yes|yeah|yep|sure|okay|ok|haan|ji|theek|ha|barobar)\b", lower):
        return "affirmation"
    if _AFFIRM_INDIC_RE.search(text):
        return "affirmation"

    # Identity question — "who is this", "kon bol raha hai", etc.
    # Highest priority among questions — gets a fast deterministic response
    _identity_phrases = [
        "who is this", "who are you", "who is calling", "who is speaking",
        "who am i speaking", "who am i talking",
        "which company", "what company", "where are you calling from",
        "where you calling from", "kahan se", "kis taraf se",
        "kon hai", "kaun hai", "kon bol", "kaun bol", "kon baat", "kaun baat",
        "aap kon", "aap kaun", "ap kon", "ap kaun", "tum kon", "tum kaun",
        "ye kon", "ye kaun", "yeh kon", "yeh kaun",
        "tame kon", "tame kaun", "aa kon", "aa kaun",
        "kon che", "kaun che", "kon bole che", "kon baat kar",
        "kidhar se", "kahan se bol",
    ]
    # Also check Devanagari/Gujarati script identity questions
    if any(w in lower for w in [
        "कौन है", "कौन बोल", "कौन बात", "आप कौन", "ये कौन", "कहां से",
        "કોણ છે", "કોણ બોલ", "કોણ વાત", "તમે કોણ", "ક્યાંથી",
    ]):
        return "identity_question"
    if any(p in lower for p in _identity_phrases):
        return "identity_question"

    # Question — check before negation so कितना isn't caught by ना substring
    if lower.endswith("?") or re.search(r"\b(what|how|when|where|why|which|who|kitna|kab|kahan|kaun|kya|kyun|kon)\b", lower):
        return "question"
    # "क्योंकि" (because) is not "क्यों" (why): "इन्वेस्टमेंट वाली क्योंकि मैं
    # इन्वेस्टमेंट के लिए देख रहा हूँ" read as a question and the answer it
    # carried was refused (RUNBOOK §79).
    _lower_q = lower.replace("क्योंकि", " ").replace("क्योकि", " ")
    if any(w in _lower_q for w in ["कौन", "क्या", "कैसे", "कब", "कहां", "कितना", "क्यों",
                                     "કોણ", "શું", "કેવી", "ક્યારે", "ક્યાં", "કેટલા", "કેમ"]):
        return "question"
    # Common caller questions without explicit question words
    _caller_questions = [
        "is this", "are you", "tell me about",
        "can you tell", "i want to know", "details", "more information",
    ]
    if any(p in lower for p in _caller_questions):
        return "question"

    # Negation
    if re.search(r"\b(no|nope|nah|nahi|na)\b", lower):
        return "negation"
    if any(w in lower for w in ["नहीं", "ના", "નહીં"]):
        return "negation"
    if re.search(r"(?:^|\s)ना(?:\s|$)", lower):
        return "negation"

    # Farewell — only when the goodbye actually ends the turn. "bye philips
    # lightning" is the caller naming their employer, not hanging up.
    if (re.search(r"\b(bye|goodbye|see you|alvida|aavjo)\b", lower)
            and _farewell_is_terminal(lower)):
        return "farewell"
    if any(w in lower for w in ["अलविदा", "फिर मिलते", "આવજો", "ફરી મળીશું"]):
        return "farewell"

    # Greeting — includes Gujarati/Hindi common greetings
    if re.search(r"\b(hello|helo|hellow|hi|hey|namaste|namaskar|kem cho|kemcho|hallo|halo|bol|bolo|suno|suniye|boliye)\b", lower):
        return "greeting"
    if any(w in lower for w in ["नमस्ते", "નમસ્તે", "હેલો", "કેમ છો", "बोलो", "बोल", "सुनिए", "हेलो"]):
        return "greeting"

    # Gratitude
    if re.search(r"\b(thank|thanks|dhanyavaad|shukriya|aabhar)\b", lower):
        return "gratitude"
    if any(w in lower for w in ["धन्यवाद", "शुक्रिया", "આભાર", "थैंक", "થેંક"]):
        return "gratitude"

    return "general"


def detect_language(text: str) -> str:
    if not text or not text.strip():
        return "en"
    if any("઀" <= c <= "૿" for c in text):
        return "gu"
    if any("ऀ" <= c <= "ॿ" for c in text):
        return "hi"
    return "en"


# ---------------------------------------------------------------------------
# Transliteration patterns for language detection v2
# ---------------------------------------------------------------------------

_HINDI_PATTERNS = [
    "hai", "tha", "thi", "hoga", "kya", "kyun", "kaise",
    "karna", "chahiye", "hona", "mujhe", "tum", "aap",
    "nahi", "accha", "theek", "namaste", "dhanyavaad",
    "shukriya", "haan", "dekhna", "dikhao", "samay",
    "subah", "dopahar", "shaam", "kal", "parso", "aaj",
    "baat", "raha", "rahi", "batao", "bolo", "boliye",
    "kaun", "kon", "kahan", "kidhar", "suniye", "bhai",
]

_GUJARATI_PATTERNS = [
    "che", "chu", "chhe", "cho", "chho", "chie", "chiye",
    "hatu", "hase", "hati", "hoy",
    "shu", "kem", "kemcho", "kem cho",
    "tame", "hu", "pan", "tamne", "mane", "amne", "amara", "tamara",
    "majama", "barobar", "saru", "sarun",
    "savare", "bapore", "saanje", "aaje", "kaale",
    "joie", "joiye", "jova", "joi", "jovun",
    "aavjo", "aavo", "jao",
    "saheb", "mari", "tari",
    "batavo", "janavo", "bolo",
    "karo", "karso", "kariye",
    "atyare", "pachhi", "pela",
    "nathi", "nahi", "haji",
]

# ── Romanized-Indic markers for the TTS ACCENT decision only ───────────────
# Deliberately a THIRD list, not an extension of the two above: those also
# feed the caller-side evidence language switcher, so widening them would
# change which language a CALLER is heard in. This one is consulted by
# tts_language_for() alone — on a call already established as hi/gu, for text
# the AGENT is about to speak — so its only power is choosing which Sarvam
# pronunciation frontend says a line that is already ours.
#
# Why it exists: businesses dictate Hinglish scripts in Latin script ("Aapke
# time ke liye thank you"), and _HINDI_PATTERNS carries content words
# (kya, baat, kaun) but none of the function words that actually make a
# Hinglish sentence Hinglish. On 2026-09-08, 4 of the 10 scripted lines of a
# live Hindi agent scored zero and were spoken by the ENGLISH frontend —
# including its wrong-number close and its busy-callback line (RUNBOOK §37).
# Every entry must be a token that is NOT an English word: "is", "us", "me",
# "the", "hi", "sir", "problem" are all excluded on purpose.
_ROMAN_INDIC_TTS_MARKERS = frozenset({
    # pronouns / possessives
    "aap", "aapka", "aapke", "aapki", "aapko", "aapse", "aapne", "aapko",
    "main", "mai", "mera", "meri", "mere", "mujhe", "mujhse", "maine",
    "hum", "ham", "hamara", "hamare", "hamari", "humein", "hamein",
    "tum", "tumhara", "unka", "unke", "uska", "uske", "iska", "iske",
    "apna", "apne", "apni", "ye", "yeh", "woh", "wo",
    # postpositions / connectives
    "ka", "ke", "ki", "ko", "se", "mein", "liye", "par", "tak", "aur",
    "toh", "phir", "lekin", "magar", "agar", "ya", "jo", "bhi",
    # verbs / auxiliaries
    "hai", "hain", "tha", "thi", "thay", "hoon", "hun", "hu", "hoga", "hogi",
    "honge", "kar", "karna", "karke", "karta", "karte", "karti", "kiya",
    "kiye", "sakta", "sakte", "sakti", "chahta", "chahte", "chahti",
    "chahiye", "raha", "rahi", "rahe", "rahega", "rahegi", "gaya", "gayi",
    "gaye", "dena", "dene", "deta", "dete", "lena", "lete", "leta",
    "samajh", "samjha", "samajhta", "bataiye", "batao", "bolo", "boliye",
    "suniye", "kijiye", "kijiyega", "karenge", "karunga", "karoge",
    # quantifiers / adverbs / discourse
    "koi", "kuch", "sab", "sabhi", "abhi", "bas", "ek", "thoda", "bahut",
    "bohot", "boht", "zyada", "jyada", "sirf", "kaafi", "zaroor", "zarur",
    "bilkul", "achha", "accha", "acha", "achhe", "badhiya", "theek", "thik",
    # question words
    "kya", "kyun", "kyu", "kaise", "kab", "kahan", "kaun", "kaunsa",
    "kaunsi", "konsa", "kitna", "kitne",
    # courtesy / nouns that carry the register
    "shukriya", "dhanyavaad", "dhanyawad", "namaste", "maaf", "kripya",
    "wajah", "waqt", "samay", "paas", "saath", "sath", "baat", "nahi",
    "nahin", "haan", "shayad", "galat",
})


def _roman_indic_marker_score(text: str) -> int:
    """Romanized Hindi/Gujarati markers in `text` (TTS accent decision only)."""
    words = {w.strip(_SCORER_STRIP_CHARS) for w in text.lower().split()}
    words.discard("")
    return len(words & _ROMAN_INDIC_TTS_MARKERS)

# ── A2 (2026-07-30): evidence-density language switching ────────────────────
# The response language switches on per-word EVIDENCE inside the utterance,
# not on a single word or a single Sarvam label. Every word casts one vote
# (en/hi/gu) or none; names, digits, spelled letters, emails, unknown Latin
# tokens and bare acknowledgements are NEUTRAL. See _evidence_switch_decision.
LANG_EVIDENCE_SWITCH = os.environ.get(
    "LANG_EVIDENCE_SWITCH", "true").lower() == "true"
# Words an utterance must carry before UNANIMOUS evidence (density 1.0, zero
# competing votes) is enough to switch on its own. Guards against a single
# stray token flipping a call while still following a caller who has clearly
# changed language in a full sentence.
LANG_UNANIMOUS_MIN_WORDS = int(
    os.environ.get("LANG_UNANIMOUS_MIN_WORDS", "3"))

# Indic script characters (Devanagari + Gujarati) are UNFAKEABLE language
# evidence: no provider hallucinates Devanagari on English speech. A
# fallback/recovered transcript that carries them may vote in the evidence
# switcher — without this, a hedge-heavy stretch (Whisper winning most
# turns with Latin text, which never votes) starves the switcher and the
# call sticks in the agent's default language (2026-08-03 live incident:
# Hindi caller answered in English for 90 seconds).
_INDIC_EVIDENCE_RE = re.compile("[\u0900-\u097F\u0A80-\u0AFF]")

# English *function* words are the only Latin tokens that vote en — a name
# ("alvin anthony") or brand string must never read as an English switch.
# Deliberately absent: to/me/in/us/do/he/may (all common romanized-Hindi
# homographs: तो/में/उस/दो/है/मैं), and every bare ack (see the neutral set).
_EN_FUNCTION_WORDS = frozenset({
    "i", "you", "we", "they", "she", "it", "my", "your", "our", "their",
    "him", "her", "am", "is", "are", "was", "were", "does", "did",
    "have", "has", "had", "will", "would", "can", "could", "should",
    "shall", "must", "the", "this", "that", "these", "those", "there",
    "what", "when", "where", "which", "who", "how", "why", "not",
    "want", "need", "tell", "know", "speak", "talk", "send", "give",
    "help", "please", "about", "with", "from", "for", "and", "but",
    "or", "if", "because", "of", "on", "at", "im", "i'm", "don't",
    "dont", "can't", "cant",
})

# Bare acknowledgements/greetings carry no language: hi/gu speakers say
# "yes yes" freely and en speakers say "haan" back — none of these may vote.
_SCORER_ACK_NEUTRAL = frozenset({
    "yes", "no", "ok", "okay", "yeah", "yep", "hmm", "hn", "huh",
    "hello", "hallo", "hi", "hey", "bye", "thanks", "thank",
    "haan", "haanji", "hanji", "ha", "ji", "jee", "na", "nahi", "nahin",
    "theek", "thik", "accha", "acha", "achha", "arre", "are", "oh",
})

_LANG_CLASSIFY_PROMPT = (
    "Classify the dominant language of this text.\n"
    "Options: English, Hindi, Gujarati\n"
    "The input may be transliterated (romanized) or mixed-language.\n"
    'Text: "{text}"\n'
    "Return ONLY one word: English, Hindi, or Gujarati."
)

_LANG_NAME_TO_CODE = {"english": "en", "hindi": "hi", "gujarati": "gu"}

# ── B2/B3 (2026-07-30): deterministic contact capture ───────────────────────
# When the engine has asked for an email, the caller's spelled/spoken reply
# is buffered, assembled and read back deterministically — the LLM never
# invents, confirms, or stores these values.
CAPTURE_MODE_ENV = os.environ.get("CAPTURE_MODE", "true").lower() == "true"

_CAPTURE_READBACK = {
    "en": "Let me confirm your email: {spelled}. Is that correct?",
    "hi": "मैं आपका ईमेल कन्फर्म कर लेता हूँ: {spelled} — क्या यह सही है?",
    "gu": "હું તમારું ઈમેલ કન્ફર્મ કરી લઉં: {spelled} — બરાબર છે?",
}
_CAPTURE_REASK = {
    "en": ("Sorry, I couldn't catch the full email. Please say it once "
           "more in one go — for example: name, at, gmail, dot com."),
    "hi": ("माफ़ कीजिए, पूरा ईमेल समझ नहीं आया। कृपया एक बार में पूरा "
           "बोलिए — जैसे: name, at, gmail, dot com."),
    "gu": ("માફ કરશો, પૂરું ઈમેલ સમજાયું નહીં. કૃપા કરીને એક જ વારમાં "
           "આખું બોલો — જેમ કે: name, at, gmail, dot com."),
}
_CAPTURE_CONFIRMED = {
    "en": "Perfect, I've noted it.",
    "hi": "बहुत बढ़िया, मैंने नोट कर लिया।",
    "gu": "સરસ, મેં નોંધી લીધું.",
}
_CAPTURE_UNRESOLVED = {
    "en": ("No problem — let's not worry about the email right now; "
           "our team will sort it out with you later."),
    "hi": ("कोई बात नहीं — ईमेल अभी रहने देते हैं; हमारी टीम बाद में "
           "आपसे ले लेगी।"),
    "gu": ("કંઈ વાંધો નહીં — ઈમેલ અત્યારે રહેવા દઈએ; અમારી ટીમ પછીથી "
           "તમારી પાસેથી લઈ લેશે."),
}
# C2 (2026-07-31): progress queries mid-capture ("नोट कर लिया आपने?",
# "कितना नोट किया?") are answered deterministically — the LLM once claimed
# "noted" for a value that was never in state.
_CAPTURE_PROGRESS_PENDING = {
    "en": "So far I have: {spelled}. Is that correct?",
    "hi": "अभी तक मैंने यह नोट किया है: {spelled} — क्या यह सही है?",
    "gu": "અત્યાર સુધી મેં આ નોંધ્યું છે: {spelled} — બરાબર છે?",
}
_CAPTURE_PROGRESS_PARTIAL = {
    "en": "I'm noting it down — please continue with the full email.",
    "hi": "जी, मैं नोट कर रहा हूँ — पूरा ईमेल बता दीजिए।",
    "gu": "જી, હું નોંધી રહ્યો છું — પૂરું ઈમેલ કહી દો.",
}

# C3: a confirmation/rejection is "bare" when EVERY token is an
# affirmation/negation word — an emphatic Hindi yes ("हाँ सही है सही है।",
# 5 tokens) failed the old ≤3-words bar and fell to the LLM.
_AFFIRM_TOKENS = frozenset({
    "yes", "yeah", "yep", "ok", "okay", "sure", "correct", "right", "perfect",
    "haan", "han", "ha", "haanji", "hanji", "ji", "sahi", "theek", "thik",
    "bilkul", "barobar", "बिल्कुल", "हाँ", "हां", "जी", "सही", "है", "हैं",
    "ठीक", "एकदम", "બરાબર", "હા", "જી", "સાચું", "એકદમ", "ચોક્કસ",
})
_NEG_TOKENS = frozenset({
    "no", "nope", "wrong", "nahi", "nahin", "nai", "na", "galat",
    "नहीं", "नहि", "ना", "नो", "गलत", "ના", "નહીં", "ખોટું",
})
# Politeness that wraps an answer without changing it: "ji ji ji okay sir
# thank you" is a yes. Live 2026-09-24 (call 934, RUNBOOK §56): that exact
# reply to "shall I cut the call?" scored "not a goodbye", the model then
# answered in ENGLISH (the same seven Latin tokens flipped the language
# switch) and the call needed a second [END_CALL] to end.
_POLITE_TOKENS = frozenset({
    "sir", "madam", "maam", "ma'am", "thank", "thanks", "thankyou",
    "thank-you", "you", "dhanyavad", "dhanyawad", "dhanyavaad", "shukriya",
    "bhai", "bhaiya", "bhaiyya", "sahab", "saheb", "sahib", "ji", "jee",
    "धन्यवाद", "शुक्रिया", "सर", "मैडम", "भाई", "भैया", "साहब", "आभार",
    "થેંક્યુ", "આભાર", "સર", "ભાઈ", "સાહેબ",
})
# Tokens that carry NO language evidence on their own — the same words in
# a Hindi, Gujarati or English mouth. The ASCII multi-word English switch
# counts only the words that are not in this set (call 934: "ji ji ji okay
# sir thank you" was seven "English" words and flipped an all-Hindi call
# to English on its last turn — and stamped the DB row `language=en`).
_LANG_NEUTRAL_TOKENS = _POLITE_TOKENS | _AFFIRM_TOKENS | _NEG_TOKENS | frozenset({
    "hello", "hi", "hey", "bye", "byee", "hmm", "hm", "hmmm", "oh", "ah",
    "arre", "are", "arey", "bas", "acha", "achha", "accha", "haanji",
    "hanji", "hanji", "okay", "ok", "okk", "yes", "yeah",
})
# What a listener says while the other person is still talking (RUNBOOK
# §78): an acknowledgement, not a turn. Core words carry it; politeness and
# the copula may ride along ("जी हाँ सर", "ठीक है", "okay thank you").
_ACK_CORE = (_AFFIRM_TOKENS - {"है", "हैं"}) | frozenset({
    "hmm", "hm", "hmmm", "mm", "mhm", "हम्म", "हम", "हूँ", "हूं", "ओके",
    "okk", "acha", "achha", "accha", "achcha", "अच्छा", "haa", "theek",
    "thik", "ठीक", "right", "sure", "हाँजी", "हांजी", "ok", "okay", "yes",
    "yeah", "बढ़िया", "badhiya",
})
_ACK_FILLER = _POLITE_TOKENS | frozenset({"है", "हैं", "hai", "सर", "sir"})


# Real-estate / money / unit tokens a Hindi or Gujarati caller says in Latin
# letters without speaking a word of English: "3 bhk", "90 lakh", "1200 sq
# ft". Call 1059 (2026-10-11, RUNBOOK §73): the answer "3 bhk" to "कितने
# BHK?" counted as TWO English words and flipped a Hindi-only call to
# English for the rest of the turn.
_LANG_DOMAIN_NEUTRAL_TOKENS = frozenset({
    "bhk", "rk", "sq", "sqft", "sq.ft", "ft", "feet", "lakh", "lakhs", "lac",
    "lacs", "cr", "crore", "crores", "rs", "inr", "k", "am", "pm", "kg",
    "km", "kms", "no", "nos", "flat", "plot", "villa", "bungalow", "shop",
    "office", "ready", "possession", "carpet", "builtup", "built-up",
})


def _language_content_words(text: str) -> int:
    """Words in an ASCII turn that could actually be English — politeness,
    bare yes/no, interjections, digits and unit/domain tokens excluded.
    What the English switch counts."""
    words = [w.strip(_SCORER_STRIP_CHARS) for w in (text or "").lower().split()]
    return sum(1 for w in words
               if w and w not in _LANG_NEUTRAL_TOKENS
               and w not in _LANG_DOMAIN_NEUTRAL_TOKENS
               and not w.replace(".", "").replace(",", "").isdigit())


# Full stops that do NOT end a sentence. Used by the LLM stream chunker so a
# title never splits a name off into its own audio file ("Mr." | "Piyush.").
_ABBREV_DOT_RE = re.compile(
    r"(?:^|[\s(\"'])(?:mr|mrs|ms|dr|prof|sr|jr|st|vs|etc|no|rs|inc|ltd|pvt|"
    r"approx|dept|est|min|max|a\.m|p\.m|e\.g|i\.e|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)\.$",
    re.IGNORECASE)

# A leading "no" that is a DISCOURSE MARKER, not a refusal: "no no, we can
# talk right now", "no problem, go ahead", "no no I'm fine with that". Very
# common in Indian English and it reads as a flat negation to a word-level
# classifier. Live 2026-09-16 (call 883): the caller said "no, no, i think we
# can talk about it right now" to "is now a good time?", was scored
# `negation`, and the availability branch jumped the call five stages
# straight to the close — the entire screening never happened.
#
# Safe to check BEFORE negation because a genuine deferral ("call me later",
# "talk later", "not right now", "busy") is already returned as `objection`
# at the top of detect_intent, before anything here runs.
_GO_AHEAD_RE = re.compile(
    r"\b(?:"
    r"we can (?:talk|do|start|go|carry|discuss|proceed|continue)"
    r"|you can (?:go ahead|start|ask|continue|proceed|carry)"
    r"|go ahead|carry on|please (?:continue|proceed|go on|do|ask)"
    r"|no problem|no worries|not an issue|no issues"
    r"|that'?s fine|thats fine|it'?s fine|i'?m fine|i am fine"
    r"|fine with (?:that|it|this)|okay with (?:that|it|this)"
    r"|i'?m free|i am free|i'?m available|i am available"
    r"|let'?s (?:do|start|go|talk)"
    r")\b", re.IGNORECASE)

# C2: email-shaped strings inside LLM speech (unanchored, case-lax).
_EMAIL_ANY_RE = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._+\-]*@[A-Za-z0-9][A-Za-z0-9\-]*"
    r"(?:\.[A-Za-z0-9\-]+)+")
_EMAIL_GENERIC = {"en": "your email", "hi": "आपका ईमेल", "gu": "તમારું ઈમેલ"}


# A plain "yes, it's me" to "Am I speaking with X?" (RUNBOOK §65). Every token
# must come from this set, and at least one from _IDENTITY_YES_CORE. A bare
# "जी" beside a hello is not enough — "हेलो जी?" is still checking the line.
_IDENTITY_YES_CORE = frozenset({
    "yes", "yeah", "yep", "yup", "yess", "ok", "okay", "sure", "speaking",
    "haan", "han", "haa", "ha", "haanji", "hanji", "ji", "jee",
    "हाँ", "हां", "हा", "हाँजी", "हांजी", "जी", "ओके", "बोलिए", "बोलिये",
    "बोलो", "बताइए", "बताइये", "बताओ", "बोल", "બોલો", "બોલું", "હા", "જી",
    "હાજી", "bolo", "boliye", "bataiye", "batao",
})
_IDENTITY_YES_WEAK = frozenset({"ji", "jee", "जी", "જી"})
_STRONG_YES = frozenset({
    "yes", "yeah", "yep", "yup", "haan", "han", "haa", "haanji", "hanji",
    "हाँ", "हां", "हाँजी", "हांजी", "હા", "હાજી", "speaking",
})
_IDENTITY_YES_TOKENS = _IDENTITY_YES_CORE | frozenset({
    "this", "is", "it's", "its", "me", "i", "am", "him", "her", "he", "she",
    "sir", "madam", "maam", "ma'am", "right", "correct", "bilkul", "sahi",
    "मैं", "मै", "ही", "रहा", "रही", "हूँ", "हूं", "हु", "हूँ।", "है", "सही",
    "बिल्कुल", "सर", "भाई", "साहब", "हेलो", "हैलो", "हलो", "hello", "helo",
    "नमस्ते", "नमस्कार", "namaste", "namaskar", "हું", "છું", "સર", "હેલો",
    "main", "hi", "hoon", "hun", "raha", "rahi",
})


# ── RUNBOOK §73 (2026-10-12): identity confirmation on an outbound call ────
# "No" / "wrong number" to "am I speaking with X?" is RE-CONFIRMED once —
# STT mishears, and a wrong number pitched is worse than one extra question.
# The caller's answer to the re-confirmation then decides: an acknowledgement
# or a repeated no closes the call with an apology; "it's me" / "right
# number" resumes with the authored good-time line; "he's not here" / "it's
# my husband's number" keeps the call open ONLY to arrange when the contact
# is reachable. Before this, a bare "नहीं" went to the model with a
# wrong-person script line and the next turn pitched anyway (call 966).
WRONG_NUMBER_RECONFIRM = os.environ.get(
    "WRONG_NUMBER_RECONFIRM", "true").lower() == "true"

# Built-in re-confirmation question (outbound.wrong_number_reconfirm_line
# overrides per agent). The Hindi line is the owner's own wording.
_WRONG_NUMBER_RECONFIRM_DEFAULT = {
    "hi": "क्या आप बस एक बार confirmation के लिए वापस बता सकते हैं कि ये ग़लत "
          "नंबर है या सही?",
    "en": "Just to confirm once more — is this the wrong number, or the "
          "right one?",
    "gu": "બસ એક વાર confirmation માટે ફરી કહેશો — આ ખોટો નંબર છે કે સાચો?",
}
# Built-in apology + close when the wrong number is confirmed
# (outbound.wrong_number_close overrides per agent).
_WRONG_NUMBER_CLOSE_DEFAULT = {
    "hi": "तकलीफ़ के लिए माफ़ कीजिएगा, आपका दिन शुभ हो।",
    "en": "My apologies for the trouble — have a good day.",
    "gu": "તકલીફ માટે માફ કરશો, તમારો દિવસ શુભ રહે.",
}

# "Wrong number" in every script the callers speak — matched anywhere in the
# turn, like the per-agent wrong_number_patterns it extends.
_WRONG_NUMBER_RE = re.compile(
    r"wrong\s*(?:number|no\.?|num)\b"
    r"|galat\s*(?:number|no\.?|num|नंबर|नम्बर)"
    r"|(?:गलत|ग़लत|ग़लत)\s*(?:नंबर|नम्बर|number)"
    r"|(?:रॉन्ग|रोंग|रौंग|राँग|रांग)\s*(?:नंबर|नम्बर|number)"
    r"|(?:नंबर|नम्बर|number)\s*(?:गलत|ग़लत|galat|wrong|ग़लत)"
    r"|ખોટો\s*નંબર|નંબર\s*ખોટો"
    r"|no\s*one\s*(?:by|with|of|called)\s*(?:that|this)\s*name"
    r"|(?:is|us|इस)\s*(?:naam|नाम)\s*(?:ka|का|के|की)\s*(?:koi|कोई)\s*(?:nahi|नहीं|नही)"
    r"|मैं\s*(?:वो|वह|ये|यह)\s*(?:नहीं|नही)\s*(?:हूँ|हूं|हु)"
    r"|i'?m\s*not\s*(?:him|her|them|that\s*person)"
    r"|you'?ve\s*(?:got\s*)?the\s*wrong"
    r"|(?:number|नंबर|नम्बर)\s*(?:lag|लग)\s*(?:gaya|गया)",
    re.IGNORECASE)

# A plain "no, not me" to the identity question: every token from this set
# and at least one core NO. "कौन?" (who?), "नहीं सुनाई दिया" (couldn't hear)
# and "नहीं मैं बिज़ी हूँ" carry a token outside it and do not match.
_IDENTITY_NO_CORE = frozenset({
    "no", "nope", "nahi", "nahin", "nai", "na", "नहीं", "नही", "नहि", "ना",
    "नो", "galat", "गलत", "ग़लत", "wrong", "ખોટો", "ખોટું", "ના", "નહીં", "નથી",
    "nhi", "nope",
})
_IDENTITY_NO_TOKENS = _IDENTITY_NO_CORE | frozenset({
    "ji", "jee", "जी", "sir", "सर", "madam", "maam", "ma'am", "मैडम", "bhai",
    "भाई", "hello", "हेलो", "हैलो", "hi", "mai", "main", "mein", "मैं", "मै",
    "woh", "vo", "wo", "वो", "वह", "ye", "yeh", "ये", "यह", "hoon", "hun",
    "hu", "हूँ", "हूं", "हु", "number", "नंबर", "नम्बर", "hai", "है", "hain",
    "हैं", "sorry", "सॉरी", "माफ़", "माफ", "कीजिए", "कीजिये", "karo", "kijiye",
    "arre", "are", "arey", "अरे", "not", "me", "it's", "its", "this", "is",
    "the", "person", "koi", "कोई", "aur", "और", "इस", "naam", "नाम", "ka",
    "का", "se", "से", "baat", "बात", "kar", "कर", "रहे", "रहा", "रही", "ho",
    "हो", "लग", "गया", "lag", "gaya", "bilkul", "बिल्कुल", "yaar", "यार",
    "bhaiya", "भैया", "sahab", "साहब", "nahi", "that", "him", "her",
    "speaking", "here", "मैंने", "nahin", "to", "तो", "ही", "hi", "aap",
    "आप", "aapne", "आपने", "tum", "तुम", "तुमने", "mere", "मेरे", "mera",
    "मेरा", "मेरी", "meri", "pas", "paas", "पास", "nam", "naam", "નંબર",
    "હું", "નથી", "જી", "સર", "ભાઈ",
})


def _is_identity_no(text: str) -> bool:
    """A bare "no, that's not me / wrong number" to the identity question."""
    t = text or ""
    if "?" in t or _has_strong_yes(t):
        return False
    toks = re.sub(r"[^0-9a-z'ऀ-ॣ०-ॿ઀-૿]+", " ",
                  t.lower()).split()
    if not toks or len(toks) > 10:
        return False
    if not all(tok in _IDENTITY_NO_TOKENS for tok in toks):
        return False
    return any(tok in _IDENTITY_NO_CORE for tok in toks)


# The re-confirmation answers that do NOT mean "wrong number".
# "It's me after all / the number is right":
_RIGHT_NUMBER_ME_RE = re.compile(
    r"मैं\s*ही|मै\s*ही|ही\s*(?:हूँ|हूं|हु|बोल)|बोल\s*रह[ाी]\s*(?:हूँ|हूं|हु)"
    r"|(?:सही|ठीक|correct|right)\s*(?:नंबर|नम्बर|number)"
    r"|(?:नंबर|नम्बर|number)\s*(?:सही|ठीक|right|correct|मेरा)"
    r"|मेरा\s*ही\s*(?:नंबर|नम्बर|number)"
    r"|\b(?:right|correct)\s*number\b|number\s*is\s*(?:right|correct|mine)"
    r"|\bit'?s\s*me\b|\bthis\s*is\s*me\b|\bthat'?s\s*me\b|\bspeaking\b"
    r"|\bi\s*am\s*(?:him|her|the\s*one|the\s*person)"
    r"|\b(?:yes|haan|han),?\s*(?:it'?s|this\s*is|i\s*am|main|mai)\b"
    r"|\bmai+n?\s*hi\s*(?:hoon|hun|hu|bol)|\bsahi\s*(?:number|no)\b"
    r"|\bnumber\s*sahi\b|\bmera\s*hi\s*number"
    r"|હું\s*જ|બોલું\s*છું|સાચો\s*નંબર|નંબર\s*સાચો|મારો\s*જ\s*નંબર",
    re.IGNORECASE)
# "Right number, but the person you want is someone else / not here":
_RIGHT_NUMBER_OTHER_RE = re.compile(
    r"(?:वो|वह|उनका|उनकी|उन्हें|उनको|उनसे)\s*(?:\S+\s*){0,3}?"
    r"(?:नहीं|नही|बाहर|busy|बिज़ी|बिजी|व्यस्त|available|उपलब्ध|सो\s*रह)"
    r"|(?:नहीं|नही)\s*(?:हैं|है)\s*(?:अभी|यहाँ|यहां|घर)?"
    r"|अभी\s*(?:नहीं|नही)\s*(?:हैं|है)\b"
    r"|बाहर\s*(?:गए|गई|गये|हैं|है|हुए)"
    r"|(?:पति|पत्नी|भाई|बेटा|बेटे|बेटी|पापा|पिता|मम्मी|माँ|माता|दोस्त|"
    r"husband|wife|brother|son|daughter|father|mother|friend|boss|मालिक|"
    r"सर|saheb|साहब|भैया|दीदी|बहन)\s*(?:का|की|के|'s|ka|ki|ke)?\s*"
    r"(?:नंबर|नम्बर|number|phone|फोन|फ़ोन)"
    r"|(?:उनका|उसका|his|her|their|unka|uska)\s*(?:नंबर|नम्बर|number|phone|फोन)"
    r"|\b(?:he|she|they)\s*(?:is|are|'s|'re)?\s*(?:not|n'?t)\s*"
    r"(?:here|home|in|around|available|there|free)"
    r"|\bnot\s*(?:here|at\s*home|available|around|reachable)\b"
    r"|\b(?:he|she)\s*(?:is|'s)\s*(?:out|away|busy|sleeping|driving)"
    r"|\bcall\s*(?:later|back|after|again)\b|\bबाद\s*में\s*(?:कॉल|फोन|फ़ोन|call)"
    r"|\bgive\s*(?:him|her|them)\s*the\s*phone|\bi'?ll\s*(?:pass|give|hand)"
    r"|\bthey'?re\s*not\s*(?:here|home|around|in)"
    r"|(?:તે|તેઓ|એ)\s*(?:નથી|બહાર)|નથી\s*અત્યારે|(?:પતિ|પત્ની|ભાઈ)\s*નો\s*નંબર",
    re.IGNORECASE)

# Gratitude, in the call's language (response_bank.localized_responses
# still wins). The English pair used to be the only built-in.
_GRATITUDE_DEFAULTS = {
    "en": ["You're welcome!", "Happy to help!"],
    "hi": ["कोई बात नहीं!", "जी, ख़ुशी हुई मदद करके।"],
    "gu": ["કોઈ વાંધો નહીં!", "જી, મદદ કરીને આનંદ થયો."],
}

# ── RUNBOOK §73: a declined site visit stands for the rest of the call ─────
# "I can't come now" was answered with the scripted "photos and videos only
# show so much…" invitation again, and again — six of seven Pramukh test
# calls on 2026-10-11 (1057, 1058, 1059, 1060, 1061, 1063); one caller asked
# "why do you keep saying this?". The decline is detected here, recorded on
# the `site_visit_interest` slot when the agent has one, and from then on a
# per-turn band (prompt_blocks.visit_declined_block) tells the model to send
# the details on WhatsApp and never invite again. "अभी नहीं" is ALSO a
# universal objection pattern (the canned "I won't take much time — when
# should I call?" deferral); a decline aimed at the visit is not a decline
# of the call and is kept away from that branch.
#
# STRONG forms stand on their own; WEAK forms ("not now", "later", a bare
# "no", "I'll let you know") count only when the agent's previous line
# invited a visit.
_VISIT_DECLINE_STRONG_RE = re.compile(
    # Hindi — "won't come / won't see / can't come"
    r"(?:नहीं|नही|नहि)\s*(?:आना|आऊँगी|आऊंगी|आऊँगा|आऊंगा|आऊँ|आऊं|"
    r"आ\s*(?:सकती|सकता|सकते|सकूंगी|सकूँगी|सकूंगा|सकूँगा|पाऊँगी|पाऊंगी|पाऊँगा|"
    r"पाऊंगा|पाएंगे|पाएँगे|पाऊँ|पाऊं)|देखना|देखने|देखूंगी|देखूँगी|देखूंगा|देखूँगा|"
    r"जा\s*(?:सकती|सकता|सकते|पाऊँगी|पाऊंगी|पाऊँगा|पाऊंगा)|visit|विज़िट|विजिट)"
    r"|(?:आना|आने|आ\s*पाना|देखना|देखने|visit|विज़िट|विजिट)\s*(?:का\s*|के\s*लिए\s*|"
    r"को\s*)?(?:अभी\s*|तो\s*|फ़िलहाल\s*|फिलहाल\s*)?(?:नहीं|नही|मुश्किल|"
    r"possible\s*नहीं|पॉसिबल\s*नहीं)"
    r"|(?:अभी|अभी\s*तो|फ़िलहाल|फिलहाल|अब)\s*(?:तो\s*)?(?:नहीं|नही)\s*"
    r"(?:आ(?:\s*(?:सक|पा)|ऊ|ए|ना|ने)|देख|visit|विज़िट|विजिट|जा\s*(?:सक|पा))"
    r"|(?:आ|आने|देखने)\s*(?:नहीं|नही)\s*(?:हो\s*पाएगा|हो\s*पायेगा|हो\s*पाएगी|"
    r"पाऊँगी|पाऊंगी|पाऊँगा|पाऊंगा|सकते|सकता|सकती|सकूंगी|सकूँगा)"
    r"|बाद\s*में\s*(?:शायद\s*)?(?:आ|देख|visit|विज़िट|विजिट)"
    r"|(?:कंफर्टेबल|कम्फर्टेबल|comfortable)\s*(?:नहीं|नही)\s*(?:हूँ|हूं|हु)?"
    r"\s*(?:\S+\s*){0,3}?(?:visit|विज़िट|विजिट|आने|देखने)"
    r"|(?:visit|विज़िट|विजिट|site)\s*(?:के\s*लिए\s*)?(?:\S+\s*){0,2}?"
    r"(?:कंफर्टेबल|कम्फर्टेबल|comfortable)\s*(?:नहीं|नही)"
    # Hinglish
    r"|\bnahi+\s*(?:aa(?:unga|ungi|unge|\s*sak\w*|\s*pa\w*)|aana|dekh\w*|"
    r"visit|jaa?\s*sak\w*)"
    r"|\b(?:abhi|filhal|fil\s*hal)\s*(?:to\s*)?nahi+\s*(?:aa|dekh|visit|jaa?)"
    r"|\bbaad\s*me(?:in)?\s*(?:shayad\s*)?(?:aa|dekh|visit)"
    r"|\b(?:aana|visit|dekhna)\s*(?:abhi\s*)?(?:nahi+|mushkil)"
    # English
    r"|\b(?:can'?t|cannot|won'?t|will\s*not|not\s*able\s*to|unable\s*to|"
    r"don'?t\s*want\s*to|not\s*(?:going\s*to|gonna)|no\s*(?:time|chance)\s*to)"
    r"\s*(?:be\s*able\s*to\s*|really\s*|possibly\s*)?"
    r"(?:come|visit|make\s*it|see\s*it|be\s*there|travel|drop\s*by)"
    r"|\bno\s*visit\b|\bnot\s*(?:interested\s*in\s*)?(?:visiting|coming)\b"
    r"|\bnot\s*now\b[^\n]{0,20}\b(?:visit|come|site)"
    # Gujarati — the person coming / seeing, not "the voice isn't coming"
    r"|(?:નહીં|નથી|નહિ)\s*(?:આવ(?:ી\s*શક|ીશ|વા|ું|ીએ|ી\s*શકું)|જો(?:વા|ઈશ|ઈ\s*શક)|visit)"
    r"|(?:આવ(?:ી\s*શક|વા|ું|ીશ)|જોવા)[^\n]{0,8}(?:નહીં|નથી|નહિ)"
    r"|અત્યારે\s*(?:નહીં|નથી)\s*(?:આવ|જો)",
    re.IGNORECASE)
# A concrete alternative day or time beside the "can't" is a NEGOTIATION
# ("सुबह नहीं आ सकते" → afternoon then; "can't make it Saturday, only after
# 5") — the scheduling flow handles it; it is not a declined visit.
# Moving the visit, not refusing it (RUNBOOK §79): "not that day, another".
_VISIT_NEGOTIATION_CUE_RE = re.compile(
    r"मत\s*(?:करो|कर|रखो|रख)|(?:कोई|किसी)\s*(?:और|दूसर\S*)|दूसर(?:ा|े|ी)\s*(?:दिन|डेट|"
    r"date|तारीख|टाइम|time)|अलावा|उसके\s*बाद|बाद\s*(?:का|की|के)|बदल|change|"
    r"\b(?:another|some\s*other|different|other)\s*(?:day|date|time|slot)\b|"
    r"\bnot\s*(?:tomorrow|today|that\s*day|then)\b|\bmat\s*(?:karo|kar|rakho)\b|"
    r"\b(?:koi|kisi)\s*(?:aur|dusr\w*)\b|\balawa\b",
    re.IGNORECASE)
_VISIT_ALT_TIME_RE = re.compile(
    r"बजे|baje|दोपहर|शाम|सुबह|सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|"
    r"रविवार|संडे|सनडे|सैटरडे|सैटर्डे|वीकेंड|परसों|अगले\s*(?:हफ्ते|हफ़्ते|महीने|"
    r"सप्ताह)|(?<![\u0900-\u097F])कल(?![\u0900-\u097F])"
    r"|\b(?:evening|morning|afternoon|noon|tonight|tomorrow|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday|weekend|next\s*(?:week|month)|"
    r"after\s*\d|at\s*\d|\d\s*(?:am|pm|o'?clock)|kal|parso|subah|shaam|"
    r"dopahar|raat)\b"
    r"|સવારે|બપોરે|સાંજે|કાલે|રવિવાર|શનિવાર|સોમવાર|મંગળવાર|બુધવાર|ગુરુવાર|"
    r"શુક્રવાર|સન્ડે|સંડે|મન્ડે|ફ્રાઇડે|સેટરડે|શનિ"
    r"|मंडे|ट्यूज़डे|ट्यूसडे|वेडनेसडे|थर्सडे|फ्राइडे|फ़्राइडे|शनि"
    r"|तारीख|तारीक|तारिख|\bdate\b"
    r"|\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|"
    r"oct|nov|dec)\w*|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
    r"\w*\s*\d{1,2}\b"
    r"|\d{1,2}\s*(?:st|nd|rd|th)?\s*(?:जनवरी|फरवरी|मार्च|अप्रैल|मई|जून|जुलाई|"
    r"अगस्त|सितंबर|सितम्बर|अक्टूबर|ऑक्टोबर|ऑक्टूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर)"
    r"|(?:जनवरी|फरवरी|मार्च|अप्रैल|जून|जुलाई|अगस्त|सितंबर|सितम्बर|अक्टूबर|ऑक्टोबर|"
    r"ऑक्टूबर|नवंबर|नवम्बर|दिसंबर|दिसम्बर)",
    re.IGNORECASE)
# WEAK forms — only when the previous agent line invited a visit, and only
# an explicit deferral ("not now", "later", "I'll let you know", "tell me
# first", "I'm not free / comfortable") or a bare short refusal. A "no"
# that carries OTHER content ("नहीं, 4 BHK नहीं — 3 BHK", "no, I want
# Vapi", "Blue Stone भी तो देखना है") is a correction or a request, never a
# declined visit — the corpus sweep behind RUNBOOK §73 found 30 of those
# for a looser rule.
_VISIT_WEAK_DEFER_RE = re.compile(
    r"(?<![\u0900-\u097F])(?:अभी|अभी\s*तो|फ़िलहाल|फिलहाल)\s*(?:तो\s*)?(?:नहीं|नही)"
    r"(?![\u0900-\u097F])"
    r"|बाद\s*में|फिर\s*(?:कभी|बताऊ|बताउ|बताती|बताता|बता\s*द|देख)"
    r"|सोच\s*(?:के|कर|कर\s*के|लूँ|लूं)"
    r"|(?:बताऊँगी|बताऊंगी|बताऊँगा|बताऊंगा|बताती\s*हूँ|बताती\s*हूं|बताता\s*हूँ|"
    r"बताता\s*हूं|बता\s*दूँगी|बता\s*दूंगी|बता\s*दूँगा|बता\s*दूंगा)"
    r"|(?:कंफर्टेबल|कम्फर्टेबल|comfortable|फ्री|free|फुर्सत|फ़ुर्सत)\s*(?:नहीं|नही|nahi+)"
    r"|\b(?:abhi|filhal)\s*(?:to\s*)?nahi+\b|\bbaad\s*me(?:in)?\b"
    r"|\bsoch\s*(?:ke|kar|lun|loon)\b|\bbata(?:ungi|unga|unge|\s*dungi|\s*dunga)\b"
    r"|\bnot\s*(?:now|right\s*now|yet|at\s*the\s*moment|today|this\s*week|free)\b"
    r"|\b(?:later|maybe|perhaps)\b|\bsome\s*other\s*(?:time|day)"
    r"|\bwill\s*(?:let\s*you\s*know|see|think|tell\s*you|get\s*back)"
    r"|\bthink\s*about\s*it|\blet\s*me\s*think"
    r"|અત્યારે\s*(?:નહીં|નથી)|પછી\s*(?:કહીશ|જોઈશ|વાત)|વિચારી(?:ને|શ)",
    re.IGNORECASE)
# "Tell me about it first" — not a decline: the caller wants the answer
# before any invitation. One turn of "answer, don't invite" (no slot, no
# stage exit); the invitation may follow later, once (calls 1059, 1063,
# 1068: "पहले थोड़ी बताओ तो सही" was answered with the photos speech).
_VISIT_ANSWER_FIRST_RE = re.compile(
    r"(?:उससे\s*|उसके\s*)?पहले\s*(?:\S+\s*){0,7}?(?:बता|बोल|समझा|जानकारी|डिटेल|detail)"
    r"|\b(?:uss?e\s*)?pe?hle\s*(?:\S+\s*){0,7}?(?:bata|bol|samjha|detail)"
    r"|\bfirst\s*(?:\S+\s*){0,4}?(?:tell|explain|send|share|details?)\b"
    r"|\btell\s*me\s*(?:\S+\s*){0,4}?first\b|\bbefore\s*that\b"
    r"|પહેલા\s*(?:\S+\s*){0,6}?(?:કહો|જણાવો|કહેશો|બતાવો)",
    re.IGNORECASE)
# A bare refusal: every token a no / politeness / "now" word, five at most.
_VISIT_BARE_NO_TOKENS = _NEG_TOKENS | _POLITE_TOKENS | frozenset({
    "अभी", "abhi", "तो", "to", "ji", "जी", "now", "not", "right", "yet",
    "भाई", "सर", "मैडम", "madam", "sir", "bhai", "no", "नहीं", "nahi",
    "nope", "नो", "ना", "na", "अरे", "arre", "arey", "यार", "yaar",
})
# A "later / maybe" cue makes the decline "Maybe later"; otherwise "No".
_VISIT_LATER_CUE_RE = re.compile(
    r"बाद|अभी|फिर|शायद|सोच|बताऊ|बताउ|बताती|बताता|बता\s*दूँ|बता\s*दूं|अगले|पहले|इस\s*हफ्ते|"
    r"इस\s*हफ़्ते|next|first|this\s*week|\b(?:right\s*)?now\b|"
    r"\b(?:later|maybe|perhaps|abhi|baad|shayad|soch|bata|think|not\s*now|"
    r"not\s*yet|some\s*other)\b|પછી|અત્યારે|વિચારી",
    re.IGNORECASE)
# What an agent line looks like when it invites a visit — what the WEAK
# forms need in the previous agent turn.
_VISIT_INVITE_CUE_RE = re.compile(
    r"आकर\s*देख|देखने\s*(?:आ|के\s*लिए|चल)|आना\s*(?:चाह|पसंद|ठीक)|आ\s*सकत|"
    r"कब\s*आ|वीकेंड|weekend|weekday|site\s*visit|visit\s*(?:रख|कर|plan|fix|"
    r"ke|के)|विज़िट|विजिट|चलकर|जाकर\s*देख|आइए|आ\s*जाइए|दिखा\s*(?:दूँ|दूं|देंगे|"
    r"देता|देती|दूंगा|दूँगा|दूंगी|दूँगी)|आने\s*का|आना\s*(?:चाहेंगी|चाहेंगे|चाहोगे)"
    r"|\bcome\s*(?:and|over|down|by|to|this|next|on)\b|\bvisit\b|"
    r"\bsee\s*(?:it|the\s*site|the\s*project)\s*(?:in\s*person|yourself)"
    r"|\bdrop\s*by\b|\bwalk\s*(?:you\s*)?through\b"
    r"|જોવા\s*આવ|આવીને\s*જો|મુલાકાત",
    re.IGNORECASE)
# A decline of the CALL (not of the visit) keeps today's objection path.
_CALL_LEVEL_DECLINE_RE = re.compile(
    r"(?:इंटरेस्ट|इन्ट्रेस्ट|दिलचस्पी|रुचि|ज़रूरत|जरूरत|जरुरत)[^\n]{0,15}(?:नहीं|नही)"
    r"|(?:नहीं|नही)[^\n]{0,15}(?:इंटरेस्ट|इन्ट्रेस्ट|दिलचस्पी|रुचि|ज़रूरत|जरूरत)"
    r"|\bnot\s*interested\b|\bno\s*need\b|\bdon'?t\s*need\b"
    r"|\b(?:interest|zaroorat|zarurat)\s*nahi+\b|\bnahi+\s*(?:chahiye|interest)"
    r"|(?:बात|baat)\s*(?:नहीं|नही|nahi+)\s*(?:कर|kar)"
    r"|(?:फोन|फ़ोन|कॉल|phone|call)\s*(?:रखो|रख|काटो|काट|kaato|kato|rakho|rakh)"
    r"|રસ\s*નથી|જરૂર\s*નથી",
    re.IGNORECASE)


def _norm_letters(text: str) -> str:
    """Lower-cased letters/digits only (Devanagari + Gujarati kept whole) —
    for 'does the last line contain this authored line?' checks."""
    return re.sub(r"[^0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+", "",
                  (text or "").lower())


def _has_strong_yes(text: str) -> bool:
    """Carries a yes that means yes — not the bare "जी" of "हेलो जी?"."""
    toks = re.sub(r"[^0-9a-z'\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+", " ",
                  (text or "").lower()).split()
    return any(t in _STRONG_YES for t in toks)


def _is_identity_yes(text: str) -> bool:
    if "?" in (text or ""):
        return False
    toks = re.sub(r"[^0-9a-z'\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+", " ",
                  (text or "").lower()).split()
    if not toks or len(toks) > 8:
        return False
    if not all(t in _IDENTITY_YES_TOKENS for t in toks):
        return False
    core = [t for t in toks if t in _IDENTITY_YES_CORE]
    if not core:
        return False
    has_hello = any(t in _HELLO_CORE for t in toks)
    if has_hello and all(t in _IDENTITY_YES_WEAK for t in core):
        return False
    return True


def _all_tokens_in(text: str, vocab: frozenset) -> bool:
    """True when every token of the turn is from `vocab` — a repetition-proof
    bare yes/no ("हाँ सही है सही है।") regardless of length."""
    if "?" in text:
        return False
    toks = [w.strip(".,!;:।॥\"'") for w in text.lower().split()]
    toks = [w for w in toks if w]
    return bool(toks) and all(w in vocab for w in toks)


# ── Is the goodbye the POINT of the turn, or a word inside an answer? ───────
# "bye" is a homophone of by / buy / bhai, and STT mishears ordinary words
# into it. Live 2026-09-15: the agent asked "which organization are you
# currently working with?", the caller answered "Signify, Philips Lighting",
# Sarvam transcribed "bye philips lightning" — and the caller was asked
# "alright then, shall I end the call here?" mid-answer. The corpus carries
# the same shape at call 704 ("by the time of interview" → "bye at the time
# of interview").
#
# What separates every real goodbye in 762 calls from those two is POSITION,
# not vocabulary: a goodbye ends the sentence ("okay thank you, bye", "that's
# it for now, bye", "bye bye, thank you"). A farewell word with two or more
# content words AFTER it is a mis-transcription of something else.
#
# Tokens below may follow a goodbye without making it something else —
# politeness, acknowledgement and function words only. NEVER add a noun here:
# a noun after "bye" is exactly the signal this test exists to read.
_CLOSING_FILLER_TOKENS = frozenset({
    "a", "an", "the", "and", "so", "then", "at", "of", "for", "to", "in",
    "on", "it", "is", "was", "that", "this", "now", "here", "there",
    "ok", "okay", "okey", "alright", "right", "well", "oh", "ah", "uh", "um",
    "yeah", "yes", "yep", "no", "nope", "not",
    "thanks", "thank", "you", "thankyou", "thaank", "much", "very",
    "have", "good", "great", "nice", "lovely", "day", "night", "one",
    "sir", "madam", "maam", "ma'am", "mam", "ji", "bhai", "dear",
    "haan", "han", "ha", "hanji", "haanji", "theek", "thik", "accha", "acha",
    "chalo", "bas", "bs", "fir", "phir", "ab", "aur", "koi", "kuch", "nahi",
    "nahin", "na", "aap", "aapka", "aapko", "apna", "hi", "hello",
    "હા", "જી", "બસ", "ઠીક", "સારું", "આભાર", "તમે",
    "हाँ", "हां", "जी", "बस", "ठीक", "अच्छा", "चलो", "धन्यवाद", "शुक्रिया",
    "आपका", "आपको", "और", "फिर", "अब", "नहीं", "ना",
})
# Single-word goodbyes — the ambiguous vocabulary the position test guards.
# Multi-word phrases (_FAREWELL_EXTRA, per-agent farewell_patterns) are
# deliberately unambiguous and are NOT subject to it.
_FAREWELL_WORD_RE = re.compile(r"\b(bye|goodbye|alvida|aavjo)\b", re.IGNORECASE)
_FAREWELL_SCRIPT_WORDS = ("अलविदा", "फिर मिलते", "આવજો", "ફરી મળીશું")
# How many content words may follow the last goodbye and still leave it a
# goodbye ("bye sir", "bye na"). Two or more is an answer, not a farewell.
_FAREWELL_TRAILING_CONTENT_MAX = 1


def _farewell_is_terminal(text: str) -> bool:
    """True when a goodbye word in `text` actually ENDS the utterance.

    Returns False when the goodbye is followed by content — the shape a
    mis-transcription takes. Callers must already know a farewell word is
    present; this only decides whether it is the point of the turn.
    """
    lower = (text or "").lower()
    # Script forms are unambiguous (no homophone in Latin STT output) and are
    # short farewell phrases in practice — accept them wherever they appear.
    if any(w in lower for w in _FAREWELL_SCRIPT_WORDS):
        return True
    toks = [w.strip(".,!;:।॥\"'?()") for w in lower.split()]
    toks = [w for w in toks if w]
    last_farewell = -1
    for i, tok in enumerate(toks):
        # Match on the token so "buh-bye" and "bye-bye" count as goodbyes,
        # not as content words that happen to contain one.
        if _FAREWELL_WORD_RE.search(tok):
            last_farewell = i
    if last_farewell < 0:
        return False
    trailing_content = [
        t for t in toks[last_farewell + 1:]
        if t not in _CLOSING_FILLER_TOKENS and not _FAREWELL_WORD_RE.search(t)
    ]
    return len(trailing_content) <= _FAREWELL_TRAILING_CONTENT_MAX


# Words that can make up a whole acknowledgement without carrying any
# information of their own. A turn built only from these cannot be the answer
# to a location / salary / skills question, whatever an extractor thinks.
_ACK_ONLY_TOKENS = _AFFIRM_TOKENS | _NEG_TOKENS | frozenset({
    "we", "i", "you", "it", "that", "this", "so", "well", "now", "just",
    "can", "could", "will", "would", "do", "did", "am", "is", "are", "was",
    "think", "guess", "mean", "please", "go", "ahead", "on", "fine", "good",
    "great", "alright", "sir", "madam", "maam", "ma'am", "hmm", "uh", "um",
    "oh", "ya", "yea", "yup", "and", "but", "a", "an", "the", "to", "of",
    "for", "then", "okey", "sure", "absolutely", "definitely", "course",
    "hi", "hello", "hey", "thanks", "thank", "हेलो", "हैलो", "हलो",
    # "तो" / "toh" glue these together: "हाँ तो बताओ", "toh boliye".
    "तो", "toh", "to",
}) | _GO_AHEAD_TOKENS


def _ack_tokens(text: str) -> list[str]:
    toks = [w.strip(".,!;:।॥\"'?()-") for w in (text or "").lower().split()]
    return [w for w in toks if w]


def _is_ack_only(text: str) -> bool:
    """True when every word of the turn is an acknowledgement/function word."""
    toks = _ack_tokens(text)
    if not toks or any(ch.isdigit() for ch in text or ""):
        return False
    return all(w in _ACK_ONLY_TOKENS for w in toks) or _is_go_ahead_value(text)


def _is_go_ahead_value(value) -> bool:
    """Is this extracted VALUE just the caller saying "go on, tell me"?

    Every word an acknowledgement AND at least one of them a go-ahead verb
    ("haan ji batao", "हाँ बोलिए"). Such a phrase is never a datum, for any
    slot of any type — unlike a bare yes/no, which is a perfectly good answer
    to "are you happy with your leads?" and is therefore left alone. The
    caller's words arrive here both as heard (Devanagari) and as the model
    transliterates them into a tool argument (Latin), so both are covered.
    """
    toks = _ack_tokens(str(value or ""))
    return (bool(toks)
            and any(w in _GO_AHEAD_TOKENS for w in toks)
            and all(w in _ACK_ONLY_TOKENS or w in _GO_AHEAD_FILLERS
                    for w in toks))


def _drop_non_values(data: dict, where: str = "") -> dict:
    """Extracted slot values minus the ones that are not values at all —
    a go-ahead ("haan ji batao") or a placeholder ("unscheduled"). Applied at
    every door an LLM-extracted value comes through; see llm_tools."""
    from .llm_tools import is_placeholder_value
    # …nor is a QUESTION. Live 2026-09-18 (call 888): asked how customers find
    # him, the caller said "तो मतलब कैसे मिल रहा है मतलब?" — what do you mean? —
    # and that sentence was stored as customer_channel. With the slot "known"
    # the agent answered "Samajh gayi" to a question it had not understood and
    # moved on. STT punctuates, so the mark is there to read.
    kept = {k: v for k, v in (data or {}).items()
            if not is_placeholder_value(v) and not _is_go_ahead_value(v)
            and "?" not in str(v) and "？" not in str(v)}
    dropped = sorted(set(data or {}) - set(kept))
    if dropped:
        logger.info("[STATE] %sdropped non-values for %s — a go-ahead, a "
                    "placeholder or a question is not an answer",
                    f"{where}: " if where else "",
                    ", ".join(f"{k}={data[k]!r}" for k in dropped))
    return kept


def _short_turn(text: str) -> bool:
    return "?" not in text and len(text.split()) <= 3


def _pattern_score(text: str, patterns: list[str]) -> int:
    """How many romanized markers of a language this text carries.

    Punctuation is stripped from every token, exactly as
    `stt_mirror_consistent` already does. Without it "koi problem nahi." did
    not count as Hindi because the token was "nahi." — and the caller heard
    the agent's own scripted Hindi line read out by Sarvam's ENGLISH
    pronunciation frontend (live 2026-09-08, RUNBOOK §37)."""
    words = {w.strip(_SCORER_STRIP_CHARS) for w in text.split()}
    words.discard("")
    return sum(1 for p in patterns if p in words)


# A2 scorer vote sets: romanized words that appear in BOTH pattern lists
# ("nahi", "bolo", …) are ambiguous → neutral; ack-tier words are neutral
# regardless of which list carries them.
_HI_ROMAN_VOTE_WORDS = (
    frozenset(_HINDI_PATTERNS) - frozenset(_GUJARATI_PATTERNS)
    - _SCORER_ACK_NEUTRAL)
_GU_ROMAN_VOTE_WORDS = (
    frozenset(_GUJARATI_PATTERNS) - frozenset(_HINDI_PATTERNS)
    - _SCORER_ACK_NEUTRAL)
_SCORER_STRIP_CHARS = ".,!?;:।॥()[]{}'\"`-–—…"


_SLOT_HOUR_RE = re.compile(r"(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)


def _slot_hour_24(time_str: str) -> int:
    """Parse a slot string like '10:00 AM' into 24-hour integer (10, 14, etc)."""
    m = _SLOT_HOUR_RE.match(time_str)
    if not m:
        return -1
    h = int(m.group(1))
    period = m.group(3).upper()
    if period == "PM" and h < 12:
        h += 12
    elif period == "AM" and h == 12:
        h = 0
    return h


def _slot_period(time_str: str) -> str:
    """Classify a slot time as morning / afternoon / evening."""
    h = _slot_hour_24(time_str)
    if 0 <= h < 12:
        return "morning"
    if h < 17:
        return "afternoon"
    return "evening"


# --- Call repair (platform rules — every agent, every call) ---------------
# Early-call "hello?" window: a bare hello inside this many seconds of the
# greeting means the caller didn't hear us — replay the last utterance
# instead of chatting. Tunable via env; see ConversationRouter._is_early_hello.
EARLY_HELLO_WINDOW_S = float(os.environ.get("CALL_REPAIR_HELLO_WINDOW_S", "25"))
# Scripts no Sarvam voice can read (RUNBOOK §63): Arabic, Arabic Supplement,
# Extended-A and the presentation forms.
_FOREIGN_SCRIPT_RE = re.compile(
    "[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]+")

# "Can you hear me?" in the languages we speak — a question the presence
# repair must not prefix with "I can hear you" (RUNBOOK §63).
_HEARING_CHECK_RE = re.compile(
    r"can you hear|hear me|आवाज़? (?:साफ़ |साफ )?आ रही|અવાજ (?:બરાબર )?આવે",
    re.IGNORECASE)

# How long a call screener / hold announcement may hold the line before the
# normal dead-air repair resumes (RUNBOOK §63). iOS rings the owner for
# roughly 20-30s after the screener line.
MACHINE_WAIT_MAX_S = float(os.environ.get("MACHINE_WAIT_MAX_S", "60"))
# Ceiling on one WHOLE hold / screener episode, measured from its FIRST
# machine line (RUNBOOK §83). A hold announcement repeats every ~45 s and
# MACHINE_WAIT_MAX_S was re-armed by every repeat: call 917265832771
# (2026-10-02) sat on hold for 8.5 minutes while the agent pitched into each
# garbled fragment. Past the ceiling the call is closed in silence — a
# person who parked a sales call this long is not coming back to it. The
# corpus's real hold episodes that a person returned from were all under
# 40 s. Per-agent override: call_flow.connect.hold_max_s.
MACHINE_HOLD_MAX_S = float(os.environ.get("MACHINE_HOLD_MAX_S", "90"))
MACHINE_SCREENER_MAX_S = float(os.environ.get("MACHINE_SCREENER_MAX_S", "120"))
# Kill switch for the §83 immediate close: false → a machine close drains the
# agent's queued audio before the channel drops, exactly as §63 did.
MACHINE_CLOSE_IMMEDIATE = (
    os.environ.get("MACHINE_CLOSE_IMMEDIATE", "true").lower() == "true")
# What a person says coming BACK from a hold, beyond a greeting or a yes:
# "sorry, go ahead", "haan bolo", "ready to speak" (call 916378305026). Any
# one of these words lets a short line through to the model; the hold
# announcement's own words never appear in it. Erring this way costs one
# reply to a garbled fragment; erring the other way ignores a person.
_HOLD_RETURN_CUES = frozenset({
    "sorry", "ahead", "ready", "speak", "listening", "bolo", "boliye",
    "bolie", "batao", "bataiye", "batayein", "sunna", "suniye",
    "सॉरी", "बोलो", "बोलिए", "बोलिये", "बताओ", "बताइए", "बताइये", "सुनिए",
    "સોરી", "બોલો", "કહો", "બતાવો",
})
# Connect hello fast path: when the opening line was itself just "Hello" (so
# there is nothing worth replaying), answer the caller's early bare "hello?"
# with an instant identity intro instead of a 1.2-1.5s LLM turn. Per-agent
# opt-out via call_flow.connect.hello_fastpath.
CONNECT_HELLO_FASTPATH = (
    os.environ.get("CONNECT_HELLO_FASTPATH", "true").lower() == "true")
# Outbound: the caller's plain "yes, it's me" to the identity question gets the
# owner's authored `outbound.good_time_check_line` at once, instead of a 1.1-
# 2.3 s LLM turn that regenerated (and reworded) the same line (RUNBOOK §65).
# Per-agent opt-out: call_flow.connect.good_time_fastpath: false.
GOOD_TIME_FASTPATH = (
    os.environ.get("GOOD_TIME_FASTPATH", "true").lower() == "true")
# ...and its onset-triggered twin (2026-08-17). The fast path above still has
# to WAIT for a transcript: endpoint silence, then STT, then the engine. On
# call ffbdc517 that chain never completed for five consecutive hellos and the
# caller sat in 14s of dead air. The intro does not actually depend on what
# the caller said — inside the connect window the first sustained speech IS
# the cue — so the pipeline fires it on VAD onset and the transcript that
# follows is absorbed here rather than answered twice.
CONNECT_ONSET_ABSORB_S = float(
    os.environ.get("CONNECT_ONSET_ABSORB_S", "8.0"))
# Hello words across the platform's scripts (en / Devanagari / Gujarati).
_HELLO_CORE = {"hello", "hallo", "helo", "hullo", "hellow",
               "हेलो", "हैलो", "हलो", "हॅलो", "હેલો", "હલો", "હૅલો"}
# Filler that may ride along with a bare hello without changing its meaning.
_HELLO_FILLER = {"haan", "haa", "han", "ha", "ji", "are", "arey", "arre",
                 "हां", "हाँ", "जी", "अरे", "હા", "જી", "અરે"}
# Greeting words beyond hello — what an opening or a first reply is made of
# when it says nothing yet (_is_greeting_only_text).
_GREETING_CORE = {"namaste", "namaskar", "namaskaar", "salaam", "salam",
                  "kemcho", "morning", "afternoon", "evening",
                  "नमस्ते", "नमस्कार", "सलाम", "નમસ્તે", "નમસ્કાર", "કેમ"}
_GREETING_FILLER = {"good", "very", "a", "to", "you", "sir", "madam", "maam",
                    "hii", "hi", "hey", "cho", "छो", "छे", "जी", "सर",
                    "मैडम", "સર", "મેડમ"}
# Dead-air reprompt phrases (used with the last question re-asked after them).
_COULDNT_HEAR = {
    "en": "Sorry, I couldn't hear you.",
    "hi": "माफ़ कीजिए, मुझे आपकी आवाज़ नहीं आई।",
    "gu": "માફ કરશો, મને તમારો અવાજ સંભળાયો નહીં.",
}
_STILL_THERE = {
    "en": "Are you still there?",
    "hi": "क्या आप लाइन पर हैं?",
    "gu": "તમે લાઇન પર છો?",
}
_SAY_AGAIN = {
    "en": "Could you please say that again?",
    "hi": "कृपया दोबारा बताइए।",
    "gu": "કૃપા કરીને ફરી કહેશો?",
}

# Text-less tool turn repair (2026-08-04). When the model answers with a tool
# call and no sentence, ask it once more with the tools withdrawn so the
# caller hears the agent's own words instead of a canned confirmation. Only
# fires on turns that would otherwise be canned-or-silent, so the latency
# cost is paid exactly where the alternative is worse.
_TOOL_TEXT_REISSUE = (
    os.environ.get("TOOL_TEXT_REISSUE", "true").lower() == "true")
try:
    _TOOL_TEXT_REISSUE_TIMEOUT_S = float(
        os.environ.get("TOOL_TEXT_REISSUE_TIMEOUT_S", "2.5"))
except ValueError:
    _TOOL_TEXT_REISSUE_TIMEOUT_S = 2.5
# Absolute floor when a tool ran, produced no canned line, and the re-ask
# missed. Agents override via response_bank.tool_ack_fallbacks.
_TOOL_ACK_FALLBACK = {
    "en": "Got it, thank you — what else can I help you with?",
    "hi": "जी, समझ गया — और कुछ बताइए, मैं कैसे मदद कर सकता हूँ?",
    "gu": "જી, સમજાઈ ગયું — બીજું કંઈ મદદ કરી શકું?",
}
# Placeholder date/time words the model substitutes for a real slot. Read
# back verbatim by the booking confirmation, they produce lines like
# "Done — I have noted today at now." A booking needs a concrete slot.
_UNBOOKABLE_WHEN = {
    "now", "asap", "today", "tomorrow", "later", "soon", "anytime",
    "any time", "tbd", "n/a", "na", "none", "unknown", "unspecified",
    "sometime", "whenever", "immediately", "right now", "अभी", "आज", "कल",
    "બહુ જલ્દી", "હમણાં", "આજે", "કાલે",
}


def _is_bookable_when(date: str, tm: str) -> bool:
    """A booking slot must name a time, not a vague adverb.

    `date` may legitimately be relative ("tomorrow", "Monday") — the calendar
    layer resolves those. `time` may not: without a clock reading there is
    nothing to confirm and nothing to put in a diary.
    """
    t = (tm or "").strip().lower()
    if not t or t in _UNBOOKABLE_WHEN:
        return False
    # Accept anything carrying a digit (3, 3pm, 15:30, ३) or a spelled-out
    # clock word; reject bare adverbs.
    if any(ch.isdigit() for ch in t):
        return True
    return any(w in t for w in ("o'clock", "oclock", "noon", "midnight",
                                "बजे", "વાગ્યે"))

# Clock-hour words by script, for "did the caller actually say this hour?"
# (RUNBOOK §76). डेढ़ / ढाई are 1:30 / 2:30 — the hour is what matters here.
_HOUR_WORDS: dict[int, tuple[str, ...]] = {
    1: ("one", "ek", "एक", "એક", "डेढ़", "dedh", "દોઢ"),
    2: ("two", "do", "दो", "બે", "ढाई", "dhai", "અઢી"),
    3: ("three", "teen", "tin", "तीन", "ત્રણ"),
    4: ("four", "char", "chaar", "चार", "ચાર"),
    5: ("five", "paanch", "panch", "पांच", "पाँच", "પાંચ"),
    6: ("six", "chhe", "che", "chah", "छह", "छः", "छे", "છ"),
    7: ("seven", "saat", "सात", "સાત"),
    8: ("eight", "aath", "आठ", "આઠ"),
    9: ("nine", "nau", "नौ", "નવ"),
    10: ("ten", "das", "दस", "દસ"),
    11: ("eleven", "gyarah", "gyaarah", "ग्यारह", "અગિયાર"),
    12: ("twelve", "barah", "baarah", "बारह", "બાર"),
}
_INDIC_DIGITS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯", "01234567890123456789")
_YES_TOKENS_FOR_HOUR = {"yes", "haan", "han", "ha", "ji", "ok", "okay", "theek",
                        "thik", "sure", "done", "हाँ", "हां", "जी", "ठीक", "हा",
                        "બરાબર", "હા", "ઠીક"}


def _hours_of(tm: str) -> set[int]:
    """The clock hours a tool `time` could mean — "4pm" → {4, 16}, "16:30" →
    {16, 4}, "11:00" → {11, 23}. Empty when there is no hour in it."""
    m = re.search(r"(\d{1,2})", (tm or "").translate(_INDIC_DIGITS))
    if not m:
        return set()
    h = int(m.group(1))
    if not 0 <= h <= 23:
        return set()
    out = {h}
    if h > 12:
        out.add(h - 12)
    elif 1 <= h <= 11:
        out.add(h + 12)
    elif h == 0:
        out.add(12)
    return out


_PRICE_ASK_RE = re.compile(
    r"\bprice\b|pricing|\bcost\b|\brate\b|कीमत|क़ीमत|दाम|भाव|प्राइस|रेट|"
    r"कितने\s*(?:का|में)|कितना\s*(?:है|होगा|पड़ेगा|लगेगा)|approx|approximate|"
    r"अप्रॉक्स|अप्रोक्स|अप्रोक्सीमेट|लगभग\s*कितन|ballpark|idea\s*(?:दो|do|de)|"
    r"કિંમત|ભાવ|કેટલા\s*(?:નો|માં)", re.IGNORECASE)
_DAY_OR_PERIOD_RE = re.compile(
    r"(?<![0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])"
    r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|"
    r"weekday|tomorrow|morning|afternoon|evening|noon|"
    r"सोमवार|मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|इतवार|मंडे|संडे|सन्डे|"
    r"सैटरडे|वीकेंड|विकेंड|कल|परसों|परसो|सुबह|दोपहर|शाम|"
    r"સોમવાર|મંગળવાર|બુધવાર|ગુરુવાર|શુક્રવાર|શનિવાર|રવિવાર|કાલે|સવારે|બપોરે|સાંજે)"
    r"(?![0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])", re.IGNORECASE)


def _has_clock_hour(text: str) -> bool:
    """A digit, or any clock-hour word, in the caller's words."""
    t = (text or "").lower().translate(_INDIC_DIGITS)
    if any(ch.isdigit() for ch in t):
        return True
    return _hour_said_in(t, set(range(1, 13)))


def _hour_said_in(text: str, hours: set[int]) -> bool:
    """One of `hours` appears in `text` as a digit or a clock word, with
    script-aware edges (never `\b` around Indic text — RUNBOOK §42)."""
    t = (text or "").lower().translate(_INDIC_DIGITS)
    nb, na = r"(?<![0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])", \
             r"(?![0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])"
    for h in hours:
        if re.search(nb + str(h) + r"(?![0-9])", t):
            return True
        for w in _HOUR_WORDS.get(h if h <= 12 else h - 12, ()):
            if re.search(nb + re.escape(w) + na, t):
                return True
    return False


# Every caller is in India and the host runs UTC, so IST is never the local
# zone — resolve it explicitly wherever the caller can hear the result.
_IST = timezone(timedelta(hours=5, minutes=30))


_ISO_DATE_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})\s*$")


def _is_past_iso_date(date: str) -> bool:
    """True only for a well-formed ISO date that is before today (IST).
    Relative words ("tomorrow", "Monday") are not ISO and never match — the
    calendar layer resolves those. Reads _now_ist(), the same clock the
    ━━ RIGHT NOW ━━ band shows the model, so the two cannot disagree."""
    m = _ISO_DATE_RE.match(date or "")
    if not m:
        return False
    try:
        d = datetime(int(m[1]), int(m[2]), int(m[3])).date()
    except ValueError:
        return False
    return d < _now_ist().date()


# How far ahead a corrected-year date may land and still be read as "the
# model meant this month/day, this year". A site visit or a callback is
# booked days to a few weeks out; a year-shifted date that lands within this
# window was a wrong YEAR, one that does not was genuinely in the past.
_WRONG_YEAR_WINDOW_DAYS = 90


def _roll_forward_wrong_year(date: str) -> str:
    """A past ISO date whose month/day fall within the next few weeks once
    the year is corrected is a WRONG-YEAR date, not a past one.

    Live 2026-09-24 (call 904): the caller agreed to "tomorrow, 3 o'clock";
    the model passed book_appointment date='2025-09-25' — its training-era
    year — four times in a row, and each was refused as already past. The
    caller was told the visit was locked in, nothing was stored, and the
    stage machine, never seeing a booking, wandered back into the recommend
    stage and pitched a Surat project. ━━ RIGHT NOW ━━ already states the
    year; the model still gets it wrong, and a refusal the caller cannot hear
    fixes nothing. Returns the input untouched when it is not ISO, not in the
    past, or does not land inside the window once shifted by one or two
    years.
    """
    m = _ISO_DATE_RE.match(date or "")
    if not m:
        return date
    try:
        d = datetime(int(m[1]), int(m[2]), int(m[3])).date()
    except ValueError:
        return date
    today = _now_ist().date()
    if d >= today:
        return date
    for years in (1, 2):
        try:
            cand = d.replace(year=d.year + years)
        except ValueError:      # 29 February
            continue
        if today <= cand <= today + timedelta(days=_WRONG_YEAR_WINDOW_DAYS):
            logger.info("[TOOLS] book_appointment date %r is in the past by "
                        "a whole year — read as %s", date, cand.isoformat())
            return cand.isoformat()
    return date

# Localized time-of-day words for the {current_time} greeting placeholder.
_TIME_OF_DAY_WORDS = {
    "en": {"morning": "morning", "afternoon": "afternoon", "evening": "evening"},
    "hi": {"morning": "सुबह", "afternoon": "दोपहर", "evening": "शाम"},
    "gu": {"morning": "સવાર", "afternoon": "બપોર", "evening": "સાંજ"},
}


def _now_ist() -> datetime:
    """Wall-clock now in IST — every caller is in India, the host runs UTC.

    THE clock for anything the caller can hear: the time-of-day greeting word
    and the ━━ RIGHT NOW ━━ prompt band both read it, so they can never
    disagree about what day it is.

    PROMPT_NOW_OVERRIDE (ISO-8601, read as IST when it carries no offset)
    freezes it. The golden-prompt harness needs that: a prompt carrying the
    live clock changes every minute and no byte-identity fixture could hold.
    Unset in production — an unparseable value warns and falls back to the
    real clock rather than pinning the agent to a wrong date.
    """
    override = os.environ.get("PROMPT_NOW_OVERRIDE", "").strip()
    if override:
        try:
            dt = datetime.fromisoformat(override)
            return dt.astimezone(_IST) if dt.tzinfo else dt.replace(tzinfo=_IST)
        except ValueError:
            logger.warning(
                "[PROMPT] PROMPT_NOW_OVERRIDE=%r is not ISO-8601 — using the "
                "real clock", override)
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        return datetime.now(_IST)


def _time_of_day_word(greeting: str = "") -> str:
    """Morning/afternoon/evening right now in IST (callers are in India).

    If the surrounding greeting text is written in Devanagari or Gujarati
    script, return the word in that script so TTS doesn't code-switch
    mid-greeting.
    """
    now = _now_ist()
    period = "morning" if now.hour < 12 else ("afternoon" if now.hour < 17 else "evening")
    lang = "en"
    if re.search(r"[ऀ-ॿ]", greeting):
        lang = "hi"
    elif re.search(r"[઀-૿]", greeting):
        lang = "gu"
    return _TIME_OF_DAY_WORDS[lang][period]


# Gendered address — the words, the preview sentinels and the lookups live in
# address.py (RUNBOOK §42), shared with the prompt builder, the spoken-surface
# preview and the Copilot so none of them can disagree about what a caller
# hears. The names below are kept because tests and callers import them here.
from .address import (  # noqa: E402
    FEMALE_NAME_PREFIX_EN as _FEMALE_NAME_PREFIX_EN,
    HONORIFIC_PREVIEW_GENDER, HONORIFIC_PREVIEW_WORD, HONORIFIC_WORDS as _HONORIFIC_WORDS,
    TITLE_PREVIEW_WORD, TITLE_WORDS as _TITLE_WORDS,
    gendered_word as _gendered_word, honorific_word as _honorific_word,
    title_word as _title_word,
)
from . import address as _address  # noqa: E402
from . import machine_line as _machine  # noqa: E402
from . import ai_disclosure as _ai_disclosure  # noqa: E402


# ---------------------------------------------------------------------------
_RESPONSE_FALLBACK = "Sorry, could you repeat that?"

# ---------------------------------------------------------------------------
# Repeat-request detection patterns (multilingual)
# ---------------------------------------------------------------------------

_REPEAT_DETECT_PATTERNS = [
    "repeat", "say again", "say that again", "once more", "pardon",
    "dobara bolo", "dobara", "dubara", "phir se bolo", "phir se", "phir kaho",
    "fari bol", "fari bolsho", "fari janavo",
    "फिर बोलो", "फिर से", "दोबारा", "दोबारा बोलो", "दुबारा", "एक बार फिर",
    "ફરી બોલ", "ફરી",
    # "the same thing (in Hindi)" — live call 2026-07-28 looped the canned
    # language ack three turns because none of these matched. "सेम ची" is a
    # prefix of "सेम चीज़" in both the combining-nukta and precomposed forms.
    "same thing", "same cheez", "sem cheez", "सेम ची", "वही बात", "वही बता",
    "એ જ વાત",
]

# Tokens that make up a *bare* language-switch request — language names,
# switch directives, postpositions, and politeness/filler across the three
# focus languages (en/hi/gu) and their scripts. _lang_switch_has_content()
# strips these; whatever survives is a real ask the caller wants answered,
# not just a request to change language.
_LANG_SWITCH_STOP_TOKENS = frozenset([
    # language names (incl. common suffixed/agglutinated forms)
    "english", "hindi", "gujarati", "angrezi", "angreji", "inglish",
    "इंग्लिश", "अंग्रेज़ी", "अंग्रेजी", "हिंदी", "हिन्दी", "गुजराती",
    "हिंदीमें", "गुजरातीमें",
    "ગુજરાતી", "અંગ્રેજી", "હિન્દી", "ઇંગ્લિશ",
    "ગુજરાતીમાં", "હિન્દીમાં", "અંગ્રેજીમાં", "ઇંગ્લિશમાં",
    # switch directives / verbs
    "speak", "talk", "say", "tell", "switch", "continue", "reply", "use",
    "change", "know", "understand",
    "bolo", "boliye", "bol", "bola", "bolna", "baat", "vaat", "vat",
    "karo", "kariye", "kare", "karna", "kar",
    "batao", "bataiye", "bata", "batana", "samajh", "aata", "aati",
    "बोलो", "बोलिए", "बोल", "बोलें", "बोलना", "बात",
    "बताओ", "बताइए", "बताना", "बता",
    "करो", "करिए", "कीजिए", "करें", "कर", "समझ", "आता", "आती",
    "વાત", "બોલો", "બોલ", "બોલવું", "કરો", "કહો", "જણાવો", "આવડે", "આવડતું",
    # postpositions / auxiliaries
    "in", "to", "me", "mai", "mein", "ma", "maa", "mei", "se", "par", "pe",
    "ki", "ka", "ke", "ko", "hai", "hain", "ho", "hu", "hun", "che", "chhe",
    "में", "मैं", "मा", "माँ", "से", "को", "का", "की", "के",
    "है", "हैं", "हूँ", "हूं", "हो",
    "માં", "મા", "ને", "નું", "ની", "નો", "થી", "છે", "છું",
    # politeness / fillers / pronouns / negation / restriction
    "please", "pls", "plz", "kya", "kyaa", "aap", "ap", "tum", "tu",
    "i", "you", "we", "can", "could", "would", "will", "do", "does",
    "don't", "dont", "can't", "cant", "cannot", "not", "no", "yes",
    "ok", "okay", "na", "ji", "sir", "madam", "bhai", "hello", "hi",
    "haan", "ha", "arre", "are", "ek", "ye", "yeh", "wo", "woh",
    "the", "a", "an", "this", "that", "it", "only", "just", "now", "abhi",
    "zara", "thoda", "mujhe", "muje", "mujhko", "hume", "humein",
    "mera", "meri", "apna", "nahi", "nahin", "nai",
    "sirf", "fakt", "kewal", "khali", "bas",
    "जी", "ना", "नहीं", "नही", "हाँ", "हां", "एक", "ये", "यह", "वो", "वह",
    "अभी", "जरा", "थोड़ा", "मुझे", "मुझको", "हमें", "मेरा", "मेरी", "अरे",
    "क्या", "आप", "तुम", "कृपया", "सिर्फ", "केवल", "खाली", "बस",
    "હા", "ના", "નથી", "નહીં", "નહિ", "મને", "હું", "તમે", "જરા", "એક",
    "ફક્ત", "માત્ર", "બસ", "ખાલી", "પ્લીઝ", "જી",
])



# ---------------------------------------------------------------------------
# Response validation
# ---------------------------------------------------------------------------


_UNRESOLVED_TOKEN_RE = re.compile(
    r"\$\{[^{}]{0,60}\}|\{\{[^{}]{0,60}\}\}|\{[A-Za-z_][A-Za-z0-9_ .\-]{0,58}\}")


def _tidy_removed_token_seams(text: str) -> str:
    """Close the gap a removed placeholder leaves in a spoken line.

    Shared by the two paths that delete a token rather than replace it: the
    unresolved-token backstop below, and a {honorific} on a contact with no
    gender. Without this, "Good morning {honorific}, am I speaking with X?"
    would be spoken as "Good morning , am I speaking with X?" — the comma
    audibly detached — and "the guardian of {student_name}?" as "the guardian
    of?".
    """
    # One implementation, shared with the address rules (RUNBOOK §42) — the
    # two used to drift: this copy turned "Craft Idea. {honorific}, kya" into
    # "Craft Idea., kya".
    return _address.tidy_seams(text)


def strip_unresolved_tokens(text: str, where: str = "") -> str:
    """Last line of defence: never SPEAK a template placeholder.

    The engine substitutes a fixed set of tokens ({contact_name},
    {customer_name}, {current_time}, {honorific} and tolerated syntax
    variants). Anything
    else an agent author or the copilot invents survives to TTS and is read
    aloud, braces and all — academic_counsellor opened every call with
    "am I speaking with the parent or guardian of {{student_name}}?".

    Config can always be wrong; a caller hearing curly braces is never
    acceptable, so this strips whatever is left AFTER substitution and logs
    loudly enough to find the offending field.
    """
    if not text or "{" not in text:
        return text
    found = _UNRESOLVED_TOKEN_RE.findall(text)
    if not found:
        return text
    cleaned = _tidy_removed_token_seams(_UNRESOLVED_TOKEN_RE.sub("", text))
    logger.error(
        "[TOKEN] unresolved placeholder(s) %s in %s — stripped before speaking. "
        "Only {contact_name}/{customer_name}/{current_time}/{honorific} are "
        "substituted.",
        found[:3], where or "agent text")
    return cleaned.strip()


# Quote characters a stage goal uses to DELIMIT a verbatim line. They are
# punctuation belonging to the instruction, never words the caller should
# hear — but SCRIPT FIDELITY (RUNBOOK §36) tells the model to reproduce a
# quoted line word for word, and on 2026-09-08 it duly reproduced the quote
# marks too: the call transcript reads
#   'Aapke time ke liye thank you. Have a great day.'
# with the apostrophes spoken as part of the utterance (RUNBOOK §37).
_SCRIPT_QUOTE_CHARS = "'\"‘’“”«»"
# A quote sitting right after sentence-ending punctuation and before a space
# or the end is closing a quoted line, never doing work inside a word. The
# apostrophe in "that's" fails both halves of this test, which is the whole
# point — an earlier attempt that just looked for the next quote character
# cut "'Achha Sir, that's good to hear.'" in half.
_SCRIPT_QUOTE_CLOSE_RE = re.compile(
    rf"(?<=[.?!।॥])[{_SCRIPT_QUOTE_CHARS}](?=\s|$)")


def _strip_script_quotes(text: str) -> str:
    """Drop quote marks that merely DELIMIT a reproduced script line.

    Responses are streamed clause by clause, so the opening and closing marks
    of one quoted line routinely arrive in different chunks — this runs on
    each chunk and must therefore handle an unpaired mark. Quotes doing real
    work inside a sentence (an apostrophe, a name read back) are left alone.
    """
    t = (text or "").strip()
    if not t:
        return t
    t = _SCRIPT_QUOTE_CLOSE_RE.sub("", t)
    # A spoken sentence never opens with a quote mark, and never ends on a
    # bare one either.
    t = t.lstrip(_SCRIPT_QUOTE_CHARS).strip()
    t = t.rstrip(_SCRIPT_QUOTE_CHARS).strip()
    return t


def _validate_response(text: str, expected_lang: str = "en",
                       max_len: int = MAX_RESPONSE_LENGTH) -> str:
    if not text or not text.strip():
        return _RESPONSE_FALLBACK
    # llm_config.max_response_length is finally wired (D8); values below the
    # historical effective constant are clamped up to it — the sub-400 values
    # in old YAMLs were never in effect and must not start truncating now.
    max_len = max(int(max_len or MAX_RESPONSE_LENGTH), MAX_RESPONSE_LENGTH)
    cleaned = _MARKDOWN_RE.sub("", text)
    cleaned = _EMOJI_RE.sub("", cleaned).strip()
    cleaned = strip_unresolved_tokens(cleaned, "llm_response")
    cleaned = _strip_script_quotes(cleaned)
    if len(cleaned) > max_len:
        truncated = cleaned[:max_len]
        # The daṇḍa is a Hindi full stop. Without it every over-long Hindi
        # reply was sliced mid-word at the cap and spoken with a trailing
        # "..." (call b5452b23: the pitch ended "…एक site visit...").
        last_sentence = max(truncated.rfind("."), truncated.rfind("!"),
                            truncated.rfind("?"), truncated.rfind("।"),
                            truncated.rfind("॥"))
        if last_sentence > max_len // 2:
            cleaned = truncated[: last_sentence + 1]
        else:
            cleaned = truncated.rstrip() + "..."
    if not cleaned:
        return _RESPONSE_FALLBACK
    return cleaned


# ===================================================================
# ConversationRouter — Pramukh Group Proactive Agent
# ===================================================================


class ConversationRouter:
    """
    Proactive outbound calling agent for Pramukh Group.

    Drives the conversation through:
      GREETING -> INTEREST_CONFIRM -> (INTEREST_RECONFIRM) -> LOCATION
      -> PROPERTY_TYPE -> SCHEDULING -> CONFIRMATION -> CLOSING.

    Agent speaks first with a bilingual greeting. User's language is
    detected from their first response and mirrored throughout.
    Extracts structured booking data at each stage. Falls back to LLM
    for natural responses and question handling.
    """

    # Class-level warmup flag: LLM API connection warms at most once per
    # process regardless of how many calls arrive (Issue 6).
    _warmup_done_flag: bool = False
    # Per-agent, not per-process: a document index belongs to one agent, so a
    # process-wide flag would warm at most one of them.
    _rag_warmed_agents: set = set()

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
        default_language: str = "hi",
        agent_def: AgentDefinition | None = None,
        call_id_provider: "Callable[[], str | None] | None" = None,
    ) -> None:
        # Agent definition drives all domain-specific behaviour. It MUST be
        # explicit: the old `agent_def or get_pramukh_agent()` fallback meant a
        # failed tenant-agent load silently answered the call as Vishal Patel
        # from Pramukh Group. Callers that want Pramukh (CLI default) pass
        # get_pramukh_agent() themselves; the telephony bridge substitutes the
        # neutral fallback agent on load failure.
        if agent_def is None:
            raise ValueError(
                "ConversationRouter requires an explicit agent_def — pass "
                "get_pramukh_agent() for the CLI default or "
                "get_neutral_fallback_agent() for a degraded call."
            )
        self._def: AgentDefinition = agent_def
        # LLM provider is swappable via env WITHOUT code changes — set all three
        # to move off US-hosted OpenAI onto a fast in-region endpoint (e.g. Gemini
        # OpenAI-compat) and cut ~1s of trans-Pacific latency per turn:
        #   LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
        #   LLM_API_KEY=<key>   LLM_MODEL=gemini-2.0-flash-lite
        # Defaults preserve the previous behaviour (OpenAI, gpt-4o-mini).
        self._api_key = (
            api_key
            or os.environ.get("LLM_API_KEY", "").strip()
            or os.environ.get("OPENAI_API_KEY", "")
        )
        self._base_url = os.environ.get("LLM_BASE_URL", "").strip() or None
        self._model = model or os.environ.get("LLM_MODEL", "").strip() or self._def.llm_config.model
        # Gemini 3.x models "think" by default and thinking tokens count against
        # max_tokens, so short-budget calls (warmup max_tokens=1, language
        # classify max_tokens=5) come back empty. LLM_REASONING_EFFORT=none
        # disables it. Primary client only — the OpenAI fallback (gpt-4o-mini)
        # rejects the param.
        _reff = os.environ.get("LLM_REASONING_EFFORT", "").strip()
        self._llm_extra: dict = {"reasoning_effort": _reff} if _reff else {}
        # Cost telemetry. Same call_id the TTS/STT trackers use, so one call's
        # speech and token spend land on the same ledger row; None in contexts
        # that have no call (tests, CLI single-turn) and the tracker no-ops.
        self._call_id_provider = call_id_provider
        # Set False by the first provider that rejects stream_options, so the
        # retry-without-usage path is taken once and then remembered for the
        # life of the process instead of costing a rejected request per turn.
        self._llm_usage_supported: bool = _LLM_USAGE_TELEMETRY
        # Model provenance, once per call. LLM_MODEL/LLM_REASONING_EFFORT live
        # in a gitignored .env read at process start, so a model change leaves
        # no commit and no record — the 08-01 bump had to be reconstructed
        # from tool-call id alphabets weeks later. One line makes every future
        # "it got worse after…" answerable from the log itself.
        logger.info("[LLM-CONFIG] model=%s effort=%s base_url=%s "
                    "max_tokens=%s tools=%s",
                    self._model, _reff or "(unset)",
                    self._base_url or "(default)",
                    self._def.llm_config.max_tokens,
                    os.environ.get("LLM_TOOLS_MODE", "(agent)"))
        self._timeout = timeout if timeout is not None else self._def.llm_config.timeout_s
        self._client: AsyncOpenAI | None = None
        _qa_signals = self._derive_qa_field_signals()
        self.memory = ConversationMemory(field_signals=_qa_signals)
        self.memory.spoken_filter = self._finalize_spoken
        self.state = ConversationState()
        # Seeded from the agent's languages.default by the bridge/run.py; an
        # unseeded session used to start hardcoded-Hindi, so English agents
        # opened with hi-IN accents (PLAN-tts-accent-consistency.md P0).
        # Codes outside the conversational trio keep the legacy "hi" seed —
        # fillers/responses are only authored for en/hi/gu.
        self.state.language = (default_language
                               if default_language in ("en", "hi", "gu")
                               else "hi")
        self._response_counter = 0
        self._lang_cache: dict[str, tuple[str, float, str]] = {}
        self._filler_counter = 0
        self._speculative_yes: asyncio.Task | None = None
        self._speculative_no: asyncio.Task | None = None
        self._last_nudge_stage: str | None = None
        # Last-response memory (feature 3)
        self.last_agent_response: str = ""
        self.last_agent_language: str = ""
        # Call-repair state (platform rules, all agents): early-call "hello?"
        # replay window anchor + replay counter, see _is_early_hello().
        self._opening_ts: float = 0.0
        self._hello_replays: int = 0
        # Connect hello fast path: the composed identity intro fires at most
        # once; after that the normal replay branch repeats it.
        self._intro_delivered: bool = False
        # The authored good-time beat has been spoken (once per call).
        self._good_time_spoken: bool = False
        # RUNBOOK §73 — identity confirmation on an outbound call. A "no" /
        # "wrong number" to "am I speaking with X?" is RE-CONFIRMED once
        # before the call is closed (asks spoken so far; 0 = not pending),
        # and "it's someone else" is remembered so the pitch never follows.
        self._wrong_number_pending: int = 0
        self._identity_other: bool = False
        # RUNBOOK §73 — the caller declined a site visit on this turn index
        # (-1 = never). From then on the per-turn ━━ VISIT DECLINED ━━ band
        # keeps the model from inviting again.
        self._visit_declined_turn: int = -1
        # RUNBOOK §79: the turn the caller last negotiated the visit day/time
        self._visit_negotiation_turn: int = -1
        self._visit_answer_first_turn: int = -1
        # Set when the PIPELINE spoke the intro on speech onset. Until it
        # expires, bare hellos are the utterance that already got answered —
        # absorb them instead of replaying at them (see CONNECT_ONSET_ABSORB_S).
        self._connect_onset_absorb_until: float = 0.0
        # Machine on the caller's side (RUNBOOK §63, machine_line.py): the
        # kind we are waiting through (screener / hold), since when, and the
        # language the call had before a machine spoke — a screener's English
        # is not the customer's language. `_human_spoke` ends the window in
        # which voicemail / screener wording can mean a machine at all.
        self._machine_wait_kind: str | None = None
        self._machine_wait_since: float = 0.0
        self._machine_lang_before: str | None = None
        self._screener_answered: bool = False
        self._human_spoke: bool = False
        # §83: when the CURRENT hold/screener episode began (its first machine
        # line) — the ceiling is on the episode, not on the gap since the last
        # announcement — and whether its timeout already closed the call.
        self._machine_wait_started: float = 0.0
        self._machine_timeout_fired: bool = False
        # A machine close stops the agent's audio NOW and hangs up without
        # draining it (RUNBOOK §83): the opening and the onset intro were
        # spoken into a mailbox, and finishing them only bills TTS. False for
        # the one close that SPEAKS first (an authored voicemail message).
        self.hangup_immediate: bool = False
        # Consecutive "hello?" turns mid-call (presence checks), see
        # _presence_check_reply. Reset by any turn with content.
        self._presence_checks: int = 0
        # Name-capture confirmation mode (feature 4)
        self._name_pending: str | None = None
        self._name_confirmed: bool = False
        self._name_pending_turn: int = 0   # turn index when _name_pending was set
        self._rag_context: str = ""        # populated from documents before each LLM call
        self._rag_citations: list = []     # structured evidence for the turn record
        self._rag_trace_id: str = ""       # correlates the turn with its RAG trace
        # A2 evidence-density switching state (2026-07-30): Tier-2 pending
        # target + oscillation-guard anchor. Tier-0 inputs: the STT source of
        # the current turn (set by the pipeline before process_stream —
        # Whisper/script-retry recoveries are mishear-prone and never vote)
        # and the contact-capture slot (B2 — spelled emails/names never vote).
        self._pending_lang_switch: str | None = None
        self._last_evidence_switch_turn: int = -10
        self._turn_stt_source: str = "primary"
        self._capture_slot: str | None = None   # B2 capture mode (contact slots)
        self._capture_buf: list[str] = []
        self._capture_buf_ts: float = 0.0
        self._capture_fallback_said: set[str] = set()
        # C1 (2026-07-31): armed the moment the AGENT asks for the email —
        # note_asked only fires on scheduler-driven asks, so an LLM/stage-goal
        # ask left capture dark through five spelling turns on a live call.
        self._capture_asked_slot: str | None = None
        # C3/C4: what the engine's most recent utterance makes it expect.
        # awaiting_payload → the ask/re-ask just played (spelling next);
        # awaiting_confirm → a read-back just played (bare yes/no next).
        # Both cleared by _store_last_response and re-set by the yield sites,
        # so an intervening LLM digression always resets the expectation.
        self._capture_awaiting_payload: bool = False
        self._capture_awaiting_confirm: bool = False
        # C6: the non-contact slot the agent's last utterance asked about —
        # the caller's direct answer may overwrite a filled value ("अभी
        # बात कर लो" filled preferred_time='Now'; the booked "आज 4 बजे"
        # could then never land).
        self._last_asked_slot: str | None = None

        # Call context — direction defaults to the agent's configured type;
        # the telephony bridge overrides per call via set_call_context()
        # (e.g. an outbound dial carries the dialed contact's name/number).
        self.call_direction: str = (getattr(self._def, "call_direction", "") or "inbound").lower()
        self.outbound_contact_name: str = ""
        self.outbound_contact_phone: str = ""
        # "male" / "female" from the contact record, or "" — the {honorific}
        # placeholder and the CONTACT prompt line are the only readers, and
        # both treat "" as "do not address them by a gendered title".
        self.outbound_contact_gender: str = ""
        self.conn_id: str = ""
        # Callback context — why this (outbound) call is being made, carried
        # from the original call's notes (call_links.context_note). Rendered
        # as a WHY-YOU-ARE-CALLING-BACK prompt block when non-empty.
        self.callback_context: str = ""
        # Structured metadata for a platform-scheduled follow-up call
        # (call_links.callback_meta, stamped by the auto-dial executor):
        # {reason, topic, requested_when, source_call_at}. When present, the
        # opening line references the earlier conversation instead of the
        # cold outbound greeting.
        self.callback_meta: dict = {}
        # Agent-initiated hangup (platform rule, all agents): set ONLY on the
        # truly-final close paths (farewell exchange, wrong number, DND /
        # decline threshold, LLM-failure graceful close, close-permission
        # confirm). The telephony bridge polls this after the final utterance
        # drains and tears the channel down. NOT the same as state.is_closing,
        # which also fires on booking-confirmed while the conversation is
        # still live.
        self.hangup_pending: bool = False
        # Hard close (max-duration cap / business-mandated cut): caller speech
        # does NOT cancel a hard-armed hangup — the platform is ending the
        # call regardless. Every consented-close path leaves this False.
        self._hangup_hard: bool = False
        # Set while streaming when the LLM appended the [END_CALL] token to
        # its goodbye (call_flow.termination.llm_end_call, default on). The
        # token itself is stripped before TTS ever sees it.
        self._llm_end_call_seen: bool = False
        # Close-confirmation gate (platform rule, all agents —
        # call_flow.termination.confirm_before_end, default on). A misheard
        # "bye" used to close the call outright; now every speech-triggered
        # close asks one short confirmation question first and only arms
        # hangup_pending after the caller says yes. Counted so a caller who
        # really is leaving is never trapped in a confirm loop.
        self._close_confirm_pending: bool = False
        self._close_confirm_count: int = 0
        # True once the caller has actually RESPONDED to a close question.
        # Asking a second time is how the 2026-09-08 loop happened: a reply
        # the gate could not read as a yes dropped the gate, the LLM re-emitted
        # [END_CALL] with the identical farewell, and the gate armed again —
        # so the caller heard the same goodbye three times (RUNBOOK §37).
        self._close_confirm_answered: bool = False
        # Native tool calling (deploy/PLAN-llm-tool-calling.md). Mode is
        # per-agent llm_config.enable_tools with the LLM_TOOLS_MODE env kill
        # switch; proposals are accumulated by _call_llm_stream and executed
        # (or shadow-logged) at end-of-stream in _consume_llm_with_filler.
        from .llm_tools import resolve_tools_mode
        self._tools_mode: str = resolve_tools_mode(self._def)
        self._pending_tool_proposals: list = []
        if self._tools_mode != "off":
            logger.info("[TOOLS] mode=%s agent=%s", self._tools_mode,
                        self._def.agent_id)
        # Persistent customer memory (returning caller), set by the bridge via
        # set_customer_context(). Empty dict = unknown caller = no band.
        self.customer_context: dict = {}
        # v2 branch bookkeeping: per-call take counters (loop prevention) and
        # deterministic side effects queued by branch actions.
        self._branch_takes: dict = {}
        self._slot_confirm_turn: dict[str, int] = {}
        self._whatsapp_promised: bool = False
        self._callback_requested: str = ""
        self.call_outcome: str = ""
        # v3 in-call commitment clarification (flag-gated per agent via
        # follow_up.clarify_commitments.enabled; PLAN-intelligence-layer §2.8).
        # Typed commitments ride extra_flags["commitments"] → collected_data →
        # the post-call pipeline, which schedules clarified ones precisely.
        self._commitments: list[dict] = []
        self._clarify_asked: bool = False
        self._pending_clarify: dict | None = None
        # While > 0, _extract_time_preference is suppressed: the answer to
        # "what day works?" must refine the commitment, NOT create a calendar
        # booking (phantom-booking doctrine).
        self._suppress_time_match_turns: int = 0

        # Conversation State Management layer — per-call structured memory:
        # extraction, merge, audit trail, missing-slot detection, and the
        # single authoritative state block injected into every system prompt.
        from .state_manager import build_state_manager
        self.state_mgr = build_state_manager(self._def, language=default_language)
        # RUNBOOK §68 — budget-fit suggestion + caller-confirmed pitch switch.
        # None unless knowledge_base.primary_project.budget_switch is set.
        self._pitch = None
        try:
            from . import pitch_switch as _ps
            if _ps.enabled(self._def):
                _pid = str(self._def.knowledge_base.primary_project.get("id")
                           ).strip().lower()
                self._pitch = _ps.PitchSwitchState(base_id=_pid, active_id=_pid)
        except Exception:
            logger.exception("[PITCH] init failed — budget switch disabled")
            self._pitch = None
        # RUNBOOK §72 — WhatsApp sharing tracker: the project under
        # discussion, the projects the caller agreed to receive, the number.
        # None unless follow_up.whatsapp_promise.track_projects is set.
        self._share = None
        try:
            from . import share_tracker as _st
            if _st.enabled(self._def):
                self._share = _st.ShareState()
        except Exception:
            logger.exception("[SHARE] init failed — tracker disabled")
            self._share = None
        # Fallback LLM (lazy; see _get_fallback_client).
        self._fallback_client: AsyncOpenAI | None = None
        self._fallback_model: str = "gpt-4o-mini"
        # Per-call fidelity record: which model(s) actually spoke, and how
        # many turns rode the fallback. Persisted into the calls table by the
        # telephony bridge — the historical log holds 666 silent mid-call
        # switches to the US-hosted fallback with only a WARNING line.
        self.llm_models_used: list[str] = [self._model]
        self.llm_fallback_turns: int = 0
        # Filler frequency governors (llm_config; 0/0 = uncapped = today).
        self._fillers_played: int = 0
        self._last_filler_turn: int = -999
        # Consecutive-LLM-failure streak (env LLM_FAILURE_STREAK_LIMIT,
        # 0/unset = off = today's endless canned-fallback behaviour).
        self._llm_failure_streak: int = 0
        # One-turn LLM directive armed by the language-switch handler when the
        # caller's switch request carries a real ask (or names the language
        # already active) — consumed by _call_llm_stream, cleared every turn.
        self._turn_lang_directive: str | None = None
        # Async LLM slot-extractor bookkeeping (strong ref; latest turn only).
        self._slot_extract_task: asyncio.Task | None = None
        self._slot_extract_enabled = (
            os.environ.get("ENABLE_LLM_SLOT_EXTRACTOR", "true").lower() != "false"
        )
        # Turn transaction (continuation merge): snapshot of pre-turn state,
        # open from process_stream entry until commit or rollback. See
        # turn_transaction.py — new per-turn-mutable fields must be added to
        # its capture surface.
        self._continuation_enabled = (
            os.environ.get("ENABLE_CONTINUATION_MERGE", "false").lower() == "true"
        )
        self._pending_turn = None
        self._deferred_bookings: list[str] = []
        self._price_asks = 0               # ━━ PRICE ━━ band (opt-in)
        self._price_ask_pending = False
        # What this call has already written to the calendar. The CallState →
        # booking sync used to write a SECOND row — the bare time, no date —
        # one turn after the tool had written the real one (call 1072:
        # rows "2026-10-18 at 11:00" and "11:00").
        self._calendar_booked: str = ""
        # (turn_count, "unusable"|"refused") of the last book_appointment the
        # gates REFUSED — drives the ━━ NOT BOOKED ━━ band (F4).
        self._booking_refused: tuple[int, str] | None = None

        if not self._api_key:
            logger.warning("[CONV] OPENAI_API_KEY not set -- LLM calls will fail")

    def _get_client(self) -> AsyncOpenAI:
        if self._client is None:
            _kwargs = {"api_key": self._api_key, "timeout": self._timeout}
            if self._base_url:
                _kwargs["base_url"] = self._base_url
            self._client = AsyncOpenAI(**_kwargs)
        return self._client

    def _get_fallback_client(self) -> AsyncOpenAI | None:
        """Secondary LLM used when the primary fails (quota/429/auth/network).

        Config: LLM_FALLBACK_BASE_URL / LLM_FALLBACK_MODEL / LLM_FALLBACK_API_KEY.
        Defaults to OpenAI gpt-4o-mini on OPENAI_API_KEY — always funded here,
        slower (US) but a slow answer beats a canned 'please repeat'. Returns
        None when no usable fallback exists or it would duplicate the primary.
        """
        if self._fallback_client is not None:
            return self._fallback_client
        fb_key = (os.environ.get("LLM_FALLBACK_API_KEY", "").strip()
                  or os.environ.get("OPENAI_API_KEY", "").strip())
        fb_base = os.environ.get("LLM_FALLBACK_BASE_URL", "").strip() or None
        self._fallback_model = (os.environ.get("LLM_FALLBACK_MODEL", "").strip()
                                or "gpt-4o-mini")
        if not fb_key:
            return None
        if fb_base == self._base_url and self._fallback_model == self._model:
            return None  # identical to primary — nothing to fall back to
        _kwargs = {"api_key": fb_key, "timeout": self._timeout}
        if fb_base:
            _kwargs["base_url"] = fb_base
        self._fallback_client = AsyncOpenAI(**_kwargs)
        return self._fallback_client

    # ------------------------------------------------------------------
    # Call context (direction + outbound contact identity)
    # ------------------------------------------------------------------

    def set_call_context(
        self,
        direction: str | None = None,
        contact_name: str = "",
        contact_phone: str = "",
        conn_id: str = "",
        contact_gender: str = "",
    ) -> None:
        """Per-call override set by the telephony bridge before the greeting.

        For outbound dials the dashboard logs the contact's name/number at
        dial time (dashboard/call_link.json); the bridge passes them here so
        the agent can greet the contact by name and confirm identity.

        `contact_gender` is whatever `contacts.gender` holds for this person —
        usually nothing. It is optional at every link of the chain and no
        caller is required to supply it; an absent value means the agent
        simply never uses a gendered title."""
        if direction:
            self.call_direction = direction.lower()
        if contact_name:
            self.outbound_contact_name = contact_name.strip()
            # Prefill so missing-slot detection never re-asks for the name;
            # the prompt still instructs an identity confirmation first.
            if not self.memory.call_state.customer_name:
                self.memory.call_state.customer_name = self.outbound_contact_name
            # CallState is only half the story: the agent's own name COLLECT
            # slot is a separate store, and leaving it empty is what let a
            # mis-heard token overwrite a known contact (see seed_identity).
            self._seed_identity_slots(self.outbound_contact_name)
        if contact_phone:
            self.outbound_contact_phone = contact_phone.strip()
        if contact_gender:
            self.outbound_contact_gender = contact_gender.strip().lower()
        if conn_id:
            self.conn_id = conn_id
        logger.info("[CONTEXT] direction=%s contact=%r honorific=%r",
                    self.call_direction, self.outbound_contact_name or None,
                    _honorific_word(self.outbound_contact_gender) or None)
        # The caller is known now — make sure this agent's context cache is
        # warm before the first LLM turn (RUNBOOK §62). Background, no-op
        # when the cache is already live.
        self.prewarm_context_cache()

    def _seed_identity_slots(self, name: str) -> None:
        """Hand a KNOWN contact name to the slot layer, before the greeting.

        Never fatal: an agent with no name slot, or a state manager not built
        yet, must not cost the call. Also marks the legacy name flow satisfied
        so the confirm-the-name loop doesn't re-ask for something the CRM
        already told us and the caller confirmed on the opening line."""
        name = (name or "").strip()
        if not name:
            return
        try:
            sm = getattr(self, "state_mgr", None)
            if sm is None:
                return
            sm.seed_identity(name, source="crm")
            # The legacy booking/name path has its own confirmation loop; a
            # known contact is already past it.
            if not self.memory.booking.user_name:
                self.memory.booking.user_name = name
                self.memory.context.user_name = name
            self._name_confirmed = True
        except Exception:
            logger.exception("[IDENTITY] seeding failed (continuing)")

    def set_callback_context(self, context_note: str,
                             meta: dict | None = None) -> None:
        """Context for a scheduled callback (why we're calling back) —
        rendered as a WHY-YOU-ARE-CALLING-BACK block in the system prompt.
        `meta` (call_links.callback_meta, from the auto-dial executor) makes
        the OPENING line itself reference the earlier call."""
        self.callback_context = (context_note or "").strip()
        if isinstance(meta, dict) and meta:
            self.callback_meta = dict(meta)

    def set_customer_context(self, ctx: dict | None) -> None:
        """Persistent customer memory for a RETURNING caller, pre-fetched by
        the telephony bridge (customer_memory.context_digest + thin header).
        Rendered as the ━━ KNOWN CUSTOMER ━━ prompt band; the known name also
        prefills the no-re-ask slot and the {contact_name} greeting
        placeholder, and the known gender (if any) the {honorific} one.
        Per-agent opt-out: memory.use_customer_context: false."""
        if not ctx:
            return
        if not (getattr(self._def, "memory", None) or {}).get(
                "use_customer_context", True):
            logger.info("[CONTEXT] customer context suppressed by agent flag")
            return
        self.customer_context = dict(ctx)
        # RUNBOOK §77: a primary-project agent is not shown history items
        # that name another catalogue project. Logged once per call here;
        # the band re-scopes on every render.
        try:
            from . import prompt_blocks as _pb
            _, _dropped = _pb.scope_digest_to_primary(
                str(ctx.get("digest") or ""), self._def.knowledge_base)
            if _dropped:
                logger.info("[CONTEXT] %d memory item(s) naming another "
                            "project withheld from the prompt: %s",
                            len(_dropped), " | ".join(d[:70] for d in _dropped))
        except Exception:
            logger.debug("[CONTEXT] digest scoping failed", exc_info=True)
        # A known caller's phone book entry may carry a gender; an INBOUND call
        # has no dial record, so this is the only place the honorific can come
        # from. Never overrides a dial-time value — that one is the verified
        # identity for this leg (RUNBOOK §33).
        if not self.outbound_contact_gender:
            self.outbound_contact_gender = (ctx.get("gender") or "").strip().lower()
        name = (ctx.get("name") or "").strip()
        if name and not self.outbound_contact_name:
            self.outbound_contact_name = name
            if not self.memory.call_state.customer_name:
                self.memory.call_state.customer_name = name
            self._seed_identity_slots(name)
        logger.info("[CONTEXT] known customer: name=%r calls=%s digest=%dch",
                    name or None, ctx.get("calls_count"),
                    len(ctx.get("digest") or ""))

    def _stage_def(self, stage_id: str | None = None):
        """StageDefinition for a stage id (default: the current stage)."""
        sid = stage_id if stage_id is not None else self.state.stage
        return next((s for s in self._def.call_flow.stages if s.id == sid), None)

    def _stage_role(self, stage_id: str | None = None) -> str:
        """Semantic role of a stage: explicit StageDefinition.role wins,
        else derived from the legacy id (v2 role shim)."""
        sd = self._stage_def(stage_id)
        explicit = (getattr(sd, "role", "") or "") if sd else ""
        if explicit:
            return explicit
        sid = stage_id if stage_id is not None else self.state.stage
        return _derive_stage_role(sid)

    # ------------------------------------------------------------------
    # Calendar availability + booking persistence
    # ------------------------------------------------------------------

    def _agent_folder(self) -> str:
        try:
            from ..documents.store import get_folder_by_agent_id
            return get_folder_by_agent_id(self._def.agent_id) or ""
        except Exception:
            return ""

    def _available_calendar_slots(self) -> list:
        """Configured mock_slots minus slots already booked in bookings.json."""
        try:
            from ..agent_definition.calendar_store import available_slots
            return available_slots(self._def, self._agent_folder())
        except Exception:
            return list(self._def.calendar.mock_slots or [])

    def _books_appointments(self) -> bool:
        """False when the business said this agent never books
        (`calendar.type: none`). Until 2026-09-28 only the prompt honoured
        that value: the book_appointment tool was still offered, and a
        time word in any answer ("start next month", "tomorrow 4 pm" for a
        TEAM callback) was written to the diary by the legacy extractor —
        Dechcept's Nisha, whose first rule is "never schedule a meeting",
        booked two in one simulated call."""
        cal = getattr(self._def, "calendar", None)
        return str(getattr(cal, "type", "mock") or "mock").lower() != "none"

    def _record_calendar_booking(self, appointment_time: str) -> None:
        """Persist a booked slot so later calls see it as taken. Best-effort.

        While a turn transaction is open (continuation merge), the write is
        DEFERRED to commit_pending_turn(): a continuation may roll this turn
        back, and add_booking has no undo. memory.booking is set immediately
        either way, so the in-call flow never re-offers slots.
        """
        self._calendar_booked = appointment_time
        if self._continuation_enabled and self._pending_turn is not None:
            self._deferred_bookings.append(appointment_time)
            logger.info(
                "[CALENDAR] Booking %r deferred until turn commit "
                "(continuation window open)", appointment_time,
            )
            return
        self._write_calendar_booking(appointment_time)

    def _write_calendar_booking(self, appointment_time: str) -> None:
        try:
            from ..agent_definition.calendar_store import add_booking
            folder = self._agent_folder()
            if not folder:
                return
            slot_day, _, slot_time = appointment_time.partition(" at ")
            b = self.memory.booking
            cs = self.memory.call_state
            add_booking(
                folder,
                slot_day=slot_day.strip(),
                slot_time=slot_time.strip(),
                appointment_time=appointment_time,
                caller_name=b.user_name or cs.customer_name or self.outbound_contact_name,
                caller_phone=cs.customer_phone or self.outbound_contact_phone,
                conn_id=self.conn_id,
            )
        except Exception as exc:
            logger.warning("[CALENDAR] booking persistence failed: %r", exc)

    # ------------------------------------------------------------------
    # Turn transactions (continuation merge)
    # ------------------------------------------------------------------
    # The pipeline drives these: begin_turn_transaction at every
    # process_stream entry; rollback_pending_turn when a continuation of the
    # previous utterance arrives; commit_pending_turn when the turn is final
    # (next distinct turn, window expiry, closing shortcut, pipeline stop).

    def begin_turn_transaction(self, text: str) -> None:
        if not self._continuation_enabled:
            return
        if self._pending_turn is not None:
            self.commit_pending_turn()
        try:
            from .turn_transaction import capture
            self._pending_turn = capture(self, text)
        except Exception:
            logger.exception("[TURN-TXN] capture failed — turn not supersedable")
            self._pending_turn = None

    def has_pending_turn(self) -> bool:
        return self._pending_turn is not None

    def rollback_pending_turn(self) -> str | None:
        """Rewind the provisional turn; returns its user text for merging."""
        snap = self._pending_turn
        if snap is None:
            return None
        self._pending_turn = None
        # A stale extractor completion must not land after the restore.
        task = self._slot_extract_task
        if task is not None and not task.done():
            task.cancel()
        try:
            from .turn_transaction import restore
            restore(self, snap)
        except Exception:
            logger.exception("[TURN-TXN] restore failed — keeping current state")
            return None
        return snap.user_text

    def commit_pending_turn(self) -> None:
        self._pending_turn = None
        if self._deferred_bookings:
            pending, self._deferred_bookings = self._deferred_bookings, []
            for t in pending:
                self._write_calendar_booking(t)
                logger.info("[TURN-TXN] Committed deferred booking %r", t)

    # ------------------------------------------------------------------
    # Opening greeting (outbound call starts here)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Call repair (platform rules — every agent, every call)
    # ------------------------------------------------------------------

    @staticmethod
    def _is_bare_hello_text(text: str) -> bool:
        """Is this utterance nothing but hello(s) plus filler, in any supported
        script? Script-safe tokenisation, no time window."""
        # NOT [\W_]: re's \W matches Indic combining vowel signs (category
        # Mn), which would shred "हेलो" into bare consonants. Keep the
        # Devanagari + Gujarati blocks whole.
        toks = re.sub(r"[^0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+", " ",
                      (text or "").lower()).split()
        if not toks or len(toks) > 4:
            return False
        if not any(t in _HELLO_CORE for t in toks):
            return False
        return all(t in _HELLO_CORE or t in _HELLO_FILLER for t in toks)

    @staticmethod
    def _is_greeting_only_text(text: str) -> bool:
        """Is this utterance nothing but greeting words — a hello, a namaste,
        a "good morning", with or without filler?

        The AGENT-side test for "is there anything worth replaying?": an
        opening of "हेलो, नमस्ते।" carries exactly as little as "Hello?", so
        the caller's answering "hello" must get the identity line, not the
        opening again. Live 2026-09-26 (call 971): the caller heard
        "हेलो, नमस्ते।" three times before the agent said who it was, because
        only a bare HELLO was recognised as content-free. Also the
        CALLER-side test for the connect fast path — "जी नमस्ते" answering a
        greeting is contact established, and the identity beat is due."""
        toks = re.sub(r"[^0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+", " ",
                      (text or "").lower()).split()
        if not toks or len(toks) > 6:
            return False
        if not any(t in _HELLO_CORE or t in _GREETING_CORE for t in toks):
            return False
        return all(t in _HELLO_CORE or t in _GREETING_CORE
                   or t in _HELLO_FILLER or t in _GREETING_FILLER
                   for t in toks)

    def _is_early_greeting(self, text: str) -> bool:
        """A greeting-only caller utterance inside the connect window — the
        cue for the identity intro (fast path), never for a replay."""
        if self._opening_ts <= 0.0:
            return False
        if time.monotonic() - self._opening_ts > EARLY_HELLO_WINDOW_S:
            return False
        return self._is_greeting_only_text(text)

    def connect_onset_line(self) -> str:
        """Finalized for address and self-gender (RUNBOOK §42) — see
        _connect_onset_line_raw for what is said and when."""
        return self._finalize_spoken(self._connect_onset_line_raw())

    def _connect_onset_line_raw(self) -> str:
        """The intro to speak the instant the caller starts talking, or "".

        Pure query — the pipeline calls this from the VAD frame path, so it
        must not mutate anything. Only inside the connect window, only before
        any real turn, only when the opening was itself a bare hello (there is
        nothing worth replaying), and honouring the same per-agent opt-out as
        the transcript-driven fast path."""
        if not CONNECT_HELLO_FASTPATH:
            return ""
        if self._connect_cfg().get("hello_fastpath") is False:
            return ""
        if self._intro_delivered or self.state.turn_count > 0:
            return ""
        if self._opening_ts <= 0.0:
            return ""
        if time.monotonic() - self._opening_ts > EARLY_HELLO_WINDOW_S:
            return ""
        # Same precondition as the transcript path: only when there is nothing
        # worth replaying. If the agent opened with a real sentence the caller
        # may simply have missed it, and replay (not intro) is the repair.
        if self.last_agent_response and not self._is_greeting_only_text(
                self.last_agent_response):
            return ""
        try:
            return self.connect_intro_line() or ""
        except Exception:
            logger.exception("[CONNECT] onset intro build failed")
            return ""

    def note_connect_intro_spoken(self, text: str) -> None:
        """Record an intro the PIPELINE spoke on onset, as if it had come
        from process_stream: memory, last-response and the once-only latch,
        plus the window in which the triggering hello is absorbed."""
        if not text:
            return
        self._intro_delivered = True
        self._connect_onset_absorb_until = (
            time.monotonic() + CONNECT_ONSET_ABSORB_S)
        self.memory.add_assistant(text, language=self.state.language)
        self._store_last_response(text)

    def end_connect_onset_absorb(self) -> None:
        """The caller has heard the onset intro and is answering it: nothing
        from here on is the pickup hello (RUNBOOK §65). Called by the
        pipeline, which alone knows when the intro finished playing."""
        if self._connect_onset_absorb_until > 0.0:
            self._connect_onset_absorb_until = 0.0
            logger.info("[CONNECT] Onset absorb closed — caller is answering "
                        "the intro")

    def _is_early_hello(self, text: str) -> bool:
        """True when this utterance is a bare "hello?" (any supported script,
        optionally repeated / with filler) inside the early-call window.

        On a phone call that pattern means the caller did not hear what we
        just said — the right response is to say it again, not to treat
        "hello" as a conversational turn."""
        if self._opening_ts <= 0.0:
            return False
        if time.monotonic() - self._opening_ts > EARLY_HELLO_WINDOW_S:
            return False
        return self._is_bare_hello_text(text)

    def generate_silence_reprompt(self) -> str:
        """Finalized for address and self-gender (RUNBOOK §42) — see
        _generate_silence_reprompt_raw for what is said and when."""
        return self._finalize_spoken(self._generate_silence_reprompt_raw())

    def _generate_silence_reprompt_raw(self) -> str:
        """Dead-air repair: the caller has been silent well past their turn —
        most likely STT missed their reply (or they never heard us). Say we
        couldn't hear them and repeat the question; if they have not spoken
        at all yet, replay the last utterance plain (saying "I couldn't hear
        you" to someone who said nothing would be wrong). Returns "" when
        there is nothing sensible to say. Called by the pipeline watchdog."""
        # Leave-message voicemail whose greeting never said "at the tone":
        # the mailbox has gone quiet, which is the tone. Speak and close.
        if self._machine_wait_kind == _machine.VOICEMAIL:
            msg = self._voicemail_message()
            self._arm_machine_close(_machine.VOICEMAIL, immediate=False)
            return msg
        last = (self.last_agent_response or "").strip()
        if not last:
            return ""
        # A pending close question is the ONE thing worth repeating here, and
        # the ONLY thing: last_agent_response also carries the farewell that
        # was spoken alongside it, and replaying "…thank you, have a great
        # day. Shall I cut the call?" after an apology reads as a malfunction
        # (live 2026-09-08 — the caller heard that whole line three times).
        # No "I couldn't hear you" prefix either: we are asking them to
        # answer a yes/no question, not to repeat themselves.
        if self._close_confirm_pending:
            _q = self._close_confirm_question()
            self.memory.add_assistant(_q, language=self.state.language)
            return _q
        # Never parrot a bare "Hello" back into more dead air — replay the
        # full identity intro instead (live incident: "Hello" … 12s silence
        # … "Hello" → caller hung up).
        if self._is_greeting_only_text(last):
            last = self.connect_intro_line()
            self._store_last_response(last)
        if self.state.turn_count == 0:
            text = last
        else:
            phrase = _COULDNT_HEAR.get(self.state.language, _COULDNT_HEAR["en"])
            # A bare acknowledgement is a REACTION to the caller, not a
            # prompt — replaying it produces the nonsense observed live on
            # 08-03: "Sorry, I couldn't hear you. Noted, thank you." Re-ask
            # the last thing the agent actually asked instead.
            if not self._invites_a_reply(last):
                _q = self._last_question_asked(answered_stop=True)
                last = _q or ""
                if not last:
                    # Our last line asked nothing and every earlier question
                    # was answered: the caller has nothing to repeat. Ask
                    # whether they are there — not "say that again".
                    _t = f"{phrase} {_STILL_THERE.get(self.state.language, _STILL_THERE['en'])}"
                    self.memory.add_assistant(_t, language=self.state.language)
                    return _t
            # H7: replaying a long multi-sentence answer verbatim reads as a
            # malfunction (live: 166 chars ≈ 13s of TTS after an apology).
            # Condense to the final sentence — that is the question.
            # No length floor any more (was > 90): a SHORT reply is replayed
            # with its acknowledgement too, and that is the most machine-like
            # line a caller can hear — live 2026-09-18 (call 888): "माफ़ कीजिए,
            # मुझे आपकी आवाज़ नहीं आई। Samajh gayi, Alwyn. Waise, aapka business
            # kis cheez ka hai?". Only the question is worth saying twice.
            _sents = [s.strip() for s in
                      re.split(r"(?<=[।?.!])\s+", last) if s.strip()]
            if (len(_sents) > 1 and len(_sents[-1]) >= 15
                    and (len(last) > 90 or self._invites_a_reply(_sents[-1]))):
                last = _sents[-1]
            # A long single sentence cannot be condensed, and replaying a
            # 150-char pitch after "I couldn't hear you" is the most
            # machine-like thing the caller hears (call 909, 2026-09-24: the
            # whole Swarit pitch, twice, the second time over the caller's
            # own voice). A caller silent after a long line has most likely
            # heard it and is thinking — ask whether they are there.
            if len(last) <= 120:
                text = f"{phrase} {last}"
            else:
                text = f"{phrase} {_STILL_THERE.get(self.state.language, _STILL_THERE['en'])}"
        # Record the reprompt so the LLM knows it happened — but do NOT
        # overwrite last_agent_response: a second reprompt must repeat the
        # original question, not compound "couldn't hear you" prefixes.
        self.memory.add_assistant(text, language=self.state.language)
        return text

    # Question marks across the platform's scripts, plus the Devanagari
    # danda-free interrogatives that carry no '?' in practice.
    _REPLY_INVITE_RE = re.compile(
        r"\?|？|कीजिए|बताइए|बताएं|बोलिए|चाहिए|कहिए|शेयर|"
        r"કહેશો|જણાવો|બતાવો|જોઈએ", re.IGNORECASE)

    def _invites_a_reply(self, text: str) -> bool:
        """Did this utterance ask the caller for something?

        Used by dead-air repair: an utterance that asked nothing is not worth
        replaying after "I couldn't hear you" — the caller has nothing to
        answer.
        """
        t = (text or "").strip()
        if not t:
            return False
        return bool(self._REPLY_INVITE_RE.search(t))

    _PRESENCE_ACK = {
        "en": "Yes, I can hear you.",
        "hi": "जी, आपकी आवाज़ आ रही है।",
        "gu": "હા, તમારો અવાજ આવે છે.",
    }

    def _presence_check_reply(self, text: str) -> str:
        """The line for a caller's bare "hello?" mid-call, or "" to route
        the turn normally. 1st in a row: we are here + the question we
        asked. 2nd: who we are + can you hear me. Then the model has it."""
        if not self._is_greeting_only_text(text):
            self._presence_checks = 0
            return ""
        if self.state.is_closing or not (self.last_agent_response or "").strip():
            return ""
        # "hello जी हां" to a question we asked is its answer, not a line
        # check (call 994, RUNBOOK §65): हां/yes are hello-filler only when
        # nothing was asked.
        if (_has_strong_yes(text)
                and (self.last_agent_response or "").rstrip().endswith("?")):
            self._presence_checks = 0
            return ""
        self._presence_checks += 1
        lang = self.state.language if self.state.language in ("en", "hi", "gu") \
            else "en"
        if self._presence_checks == 1:
            q = self._last_question_sentence()
            if not q:
                return ""
            # "I can hear you — can you hear me?" is nonsense: when what we
            # asked WAS the hearing check, ask it again, plainly.
            line = q if _HEARING_CHECK_RE.search(q) \
                else f"{self._PRESENCE_ACK[lang]} {q}"
            logger.info("[REPAIR] Mid-call hello — present + re-ask: %r",
                        line[:80])
            return line
        if self._presence_checks == 2:
            line = self._composed_intro_line()
            logger.info("[REPAIR] Second hello in a row — identity + hearing "
                        "check: %r", line[:80])
            return line
        return ""

    _SENTENCE_END_RE = re.compile(r"[?？!।]+|\.(?=\s)")
    _ABBREV = frozenset({"mr", "mrs", "ms", "dr", "st", "jr", "sr", "no"})

    @classmethod
    def _split_sentences(cls, body: str) -> list[str]:
        """Sentence split that keeps "Mr. Rajiv" in one piece."""
        parts: list[str] = []
        start = 0
        for m in cls._SENTENCE_END_RE.finditer(body):
            if m.group() == ".":
                prev = re.findall(r"[A-Za-z]+$", body[start:m.start()])
                if prev and prev[0].lower() in cls._ABBREV:
                    continue
            parts.append(body[start:m.end()].strip())
            start = m.end()
        parts.append(body[start:].strip())
        return [p for p in parts if p]

    def _last_question_sentence(self) -> str:
        """The QUESTION inside the last reply-inviting agent line — the
        sentence to repeat, not the acknowledgement or the intro around it."""
        body = self._last_question_asked()
        if not body:
            return ""
        parts = self._split_sentences(body)
        for part in reversed(parts):
            if "?" in part or "？" in part:
                # "जी, निशा … — आवाज़ आ रही है ना? बस ये पूछना था कि …?" →
                # the last question, without the hearing check before it.
                return part
        return parts[-1] if parts else body

    def _ai_disclosure_reply(self, text: str) -> str:
        """The honest answer to "are you an AI?" (RUNBOOK §66), in order:

        1. the owner's objection-playbook answer for this wording, if it
           admits being an AI — their words, exactly as before;
        2. `response_bank.ai_disclosure_responses` — the owner's line;
        3. the platform line ("Yes, I'm Ravi, an AI voice assistant calling
           on behalf of Pramukh Group.").
        2 and 3 then repeat the question the caller was answering, so the
        call carries on instead of stalling on the disclosure."""
        own = self._match_objection_playbook(text)
        if own and _ai_disclosure.admits_ai(own):
            return own
        resp = ""
        try:
            resp = str(self._lang_response(
                self._def.response_bank.ai_disclosure_responses) or "").strip()
            resp = strip_unresolved_tokens(resp, "ai_disclosure_response")
        except Exception:
            resp = ""
        if not resp or not _ai_disclosure.admits_ai(resp):
            ident = self._def.identity
            resp = _ai_disclosure.default_disclosure(
                self.state.language, getattr(ident, "agent_name", ""),
                getattr(ident, "company_name", ""))
        q = self._last_question_sentence()
        if q and _norm_letters(q) not in _norm_letters(resp):
            resp = f"{resp} {q}"
        return resp

    def _refund_turn_ask(self) -> None:
        """A deterministic repair turn repeated an old question; the prompt
        built in parallel may already have spent a slot's ask budget on a
        question nobody voiced."""
        try:
            sm = self.state_mgr
            noted = sm.last_noted_ask
            if noted and sm._asked_turn.get(noted) == sm.turn_index:
                sm.unnote_asked(noted)
        except Exception:
            logger.exception("[STATE] ask refund failed (continuing)")

    _BARE_ACK_WORDS = frozenset({
        "ok", "okay", "okk", "hmm", "hm", "mm", "haan", "han", "ha", "haa",
        "ji", "yes", "yeah", "ya", "accha", "acha", "achha", "theek", "thik",
        "hai", "right", "fine", "sure",
        "हाँ", "हां", "जी", "ओके", "अच्छा", "ठीक", "है", "हम्म", "हम",
        "હા", "જી", "ઓકે", "સારું", "બરાબર",
    })

    def _is_bare_ack(self, text: str) -> bool:
        toks = re.sub(r"[^0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+",
                      " ", (text or "").lower()).split()
        return bool(toks) and len(toks) <= 4 and all(
            t in self._BARE_ACK_WORDS for t in toks)

    def _is_question_only(self, text: str) -> bool:
        """Every sentence of the turn is a question — the caller asked, they
        did not answer."""
        parts = self._split_sentences((text or "").strip())
        return bool(parts) and all(p.rstrip().endswith(("?", "？"))
                                   for p in parts)

    # The caller did not follow / wants it again / is asking what we mean.
    # RUNBOOK §69 (call 1032): "कौन था सर?", "किसकी बात कर रहा हूँ? कहाँ रहने
    # की बात कर रहा हूँ?" were read as answers, the question's one ask was
    # spent, and the agent said "समझ गया" and moved to the next question.
    _CONFUSED_RE = re.compile(
        r"(?:समझ(?:ा|ी|े)? नहीं|समझ नहीं आया|समझ में नहीं|क्या मतलब|मतलब\?|"
        r"किस(?:की|के|का)? बात|कौन सा|कौनसा|कौन से|कौन था|फिर से|दोबारा|"
        r"एक बार और|क्या बोला|क्या कहा|सुनाई नहीं|"
        r"samjha nahi|samjhi nahi|samajh nahi|kya matlab|matlab\?|kis(?:ki|ke|ka)? baat|"
        r"kaun ?sa|kaunse|kaun tha|phir se|fir se|dobara|kya bola|kya kaha|"
        r"sunai nahi|didn'?t (?:get|understand|catch)|don'?t understand|"
        r"not clear|what do you mean|which one|which project|come again|"
        r"pardon|sorry\?|say (?:that |it )?again|repeat|"
        r"સમજાયું નહીં|સમજ ના પડી|શું કહ્યું|કયું|કયો|ફરીથી|ફરી કહો)",
        re.IGNORECASE)

    def _non_answer_kind(self, text: str) -> str:
        """'hello' | 'confused' | 'question' when this caller turn is NOT an
        answer to what we asked; '' when it may be one."""
        t = (text or "").strip()
        if not t:
            return ""
        try:
            if self._is_greeting_only_text(t) or self._is_bare_hello_text(t):
                return "hello"
        except Exception:
            pass
        if self._CONFUSED_RE.search(t):
            return "confused"
        if self._is_question_only(t):
            return "question"
        return ""

    _GRATITUDE_WORDS = frozenset({
        "thank", "thanks", "thankyou", "you", "so", "very", "much", "a", "lot",
        "ok", "okay", "ji", "sir", "madam", "maam", "dhanyavaad", "dhanyavad",
        "shukriya", "aabhar", "welcome",
        "धन्यवाद", "शुक्रिया", "थैंक", "थैंक्स", "यू", "जी", "बहुत", "सर",
        "मैडम", "ओके", "आभार", "આભાર", "થેંક", "થેંક્સ", "યુ", "જી", "ખૂબ",
    })

    def _is_gratitude_only(self, text: str) -> bool:
        toks = re.sub(r"[^0-9a-z\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+",
                      " ", (text or "").lower()).split()
        return bool(toks) and all(t in self._GRATITUDE_WORDS for t in toks)

    def _last_question_asked(self, answered_stop: bool = False) -> str:
        """Most recent agent utterance that actually asked the caller
        something, scanning back through the conversation window.

        `answered_stop` (dead-air repair): stop at the first CALLER turn —
        a question the caller has already answered is not re-asked. Call
        1059 (2026-10-11, RUNBOOK §73): the caller declined a visit, agreed
        to WhatsApp details, the agent confirmed (a statement), the line
        went quiet — and the repair replayed the visit invitation from two
        turns earlier, the very line the caller had just refused."""
        try:
            for turn in reversed(self.memory.get_history_for_llm()):
                if turn.get("role") == "user" and answered_stop:
                    # A bare filler ("hmm", "haan", "ok") answered nothing —
                    # the question before it is still the open one.
                    _u = [w.strip(".,!?।॥") for w in
                          (turn.get("content") or "").lower().split()]
                    if _u and all(w in self._BARE_ACK_WORDS for w in _u):
                        continue
                    return ""
                if turn.get("role") != "assistant":
                    continue
                body = (turn.get("content") or "").strip()
                if not body or self._is_greeting_only_text(body):
                    continue
                if self._invites_a_reply(body):
                    return body if len(body) <= 220 else ""
        except Exception:
            logger.exception("[REPAIR] last-question scan failed")
        return ""

    _SILENCE_GOODBYE = {
        "en": "It seems I'm not able to reach you right now, so I'll let you "
              "go. Thank you for your time — have a great day!",
        "hi": "लगता है अभी आपकी आवाज़ नहीं आ रही है, तो अभी कॉल यहीं रखते हैं। "
              "आपके समय के लिए धन्यवाद, आपका दिन शुभ हो!",
        "gu": "લાગે છે કે અત્યારે તમારો અવાજ નથી આવી રહ્યો, તો હું કૉલ અહીં મૂકું છું. "
              "તમારા સમય માટે આભાર, તમારો દિવસ શુભ રહે!",
    }

    def generate_silence_goodbye(self) -> str:
        """Finalized for address and self-gender (RUNBOOK §42) — see
        _generate_silence_goodbye_raw for what is said and when."""
        return self._finalize_spoken(self._generate_silence_goodbye_raw())

    def _generate_silence_goodbye_raw(self) -> str:
        """Silence hangup (termination.silence_hangup): every dead-air repair
        attempt is spent and the caller still isn't there — say one polite
        goodbye and arm the platform hangup instead of holding the channel
        open forever. The caller speaking again still cancels the hangup at
        the top of process_stream. Called by the pipeline watchdog."""
        lang = (self.state.language or "en")[:2]
        text = (self._lang_response(self._def.response_bank.closing_responses,
                                    lang=lang)
                or self._SILENCE_GOODBYE.get(lang, self._SILENCE_GOODBYE["en"]))
        self.state.is_closing = True
        self.commit_pending_turn()
        self.state.stage = STAGE_CLOSING
        self.state_mgr.extra_flags["silence_hangup"] = True
        self.call_outcome = self.call_outcome or "no_response"
        self.hangup_pending = True
        self.memory.add_assistant(text, language=self.state.language)
        logger.info("[HANGUP] silence hangup — caller unreachable, closing")
        return text

    _MAXDUR_GOODBYE = {
        "en": "I've taken a lot of your time already, so let me not hold you "
              "any longer. Thank you so much for speaking with me — have a "
              "great day!",
        "hi": "मैंने आपका काफ़ी समय ले लिया है, इसलिए अब आपको और नहीं रोकेंगे। "
              "बात करने के लिए बहुत धन्यवाद — आपका दिन शुभ हो!",
        "gu": "મેં તમારો ઘણો સમય લઈ લીધો છે, એટલે હવે તમને વધુ નહીં રોકું. "
              "વાત કરવા બદલ ખૂબ આભાર — તમારો દિવસ શુભ રહે!",
    }

    def generate_timeout_goodbye(self) -> str:
        """Finalized for address and self-gender (RUNBOOK §42) — see
        _generate_timeout_goodbye_raw for what is said and when."""
        return self._finalize_spoken(self._generate_timeout_goodbye_raw())

    def _generate_timeout_goodbye_raw(self) -> str:
        """Hard duration cap (termination.max_call_minutes): deliver one
        wrap-up line and arm a HARD hangup — this is the single close the
        caller's next utterance does not cancel, because the business set an
        absolute ceiling on call length. Called by the pipeline watchdog."""
        lang = (self.state.language or "en")[:2]
        text = self._MAXDUR_GOODBYE.get(lang, self._MAXDUR_GOODBYE["en"])
        self.state.is_closing = True
        self.commit_pending_turn()
        self.state.stage = STAGE_CLOSING
        self.state_mgr.extra_flags["max_duration_close"] = True
        self.call_outcome = self.call_outcome or "max_duration"
        self.hangup_pending = True
        self._hangup_hard = True
        self.memory.add_assistant(text, language=self.state.language)
        logger.info("[HANGUP] max-call-duration reached — hard close")
        return text

    def arm_dead_media_close(self) -> None:
        """Media-layer close: the caller's audio channel has flatlined, so
        there is nobody to say goodbye to and every TTS chunk sent into it is
        pure waste. Unlike every other close this speaks NO line — a farewell
        into a dead socket is precisely the behaviour it replaces (call
        02819104 re-asked twice and delivered a full farewell across 53s to a
        channel that had been dead since second 64).

        HARD close: caller speech cannot cancel it, because by definition none
        is arriving. Called by the pipeline's silence watchdog."""
        self.state.is_closing = True
        self.commit_pending_turn()
        self.state.stage = STAGE_CLOSING
        self.state_mgr.extra_flags["dead_media_close"] = True
        self.call_outcome = self.call_outcome or "dead_media"
        self.hangup_pending = True
        self._hangup_hard = True
        logger.info("[HANGUP] dead media — hard close, no farewell spoken")

    # ------------------------------------------------------------------
    # Machine on the caller's side (RUNBOOK §63, machine_line.py)
    # ------------------------------------------------------------------

    # What we say to a call screener: who we are and who we are calling for,
    # nothing the owner reading the screen would not want relayed. Owners
    # override it with call_flow.connect.screening_line ({lang: line}).
    _SCREENER_LINES = {
        ("en", True, True): "Hi, this is {agent} from {company}, calling for "
                            "{contact_name} regarding your business. I'll stay "
                            "on the line.",
        ("en", True, False): "Hi, this is {agent} from {company}, calling "
                             "regarding your business. I'll stay on the line.",
        ("en", False, True): "Hi, this is {company}, calling for {contact_name} "
                             "regarding your business. I'll stay on the line.",
        ("en", False, False): "Hi, this is {company}, calling regarding your "
                              "business. I'll stay on the line.",
        ("hi", True, True): "नमस्ते, मैं {agent}, {company} से। {contact_name} जी "
                            "के लिए कॉल है, उनके business के बारे में। मैं लाइन पर हूँ।",
        ("hi", True, False): "नमस्ते, मैं {agent}, {company} से। आपके business "
                             "के बारे में कॉल है। मैं लाइन पर हूँ।",
        ("hi", False, True): "नमस्ते, ये {company} की ओर से {contact_name} जी के "
                             "लिए कॉल है, उनके business के बारे में।",
        ("hi", False, False): "नमस्ते, ये {company} की ओर से आपके business के "
                              "बारे में कॉल है।",
        ("gu", True, True): "નમસ્તે, હું {agent}, {company} તરફથી. {contact_name} "
                            "માટે કૉલ છે, તેમના business વિશે. હું લાઇન પર છું.",
        ("gu", True, False): "નમસ્તે, હું {agent}, {company} તરફથી. તમારા "
                             "business વિશે કૉલ છે. હું લાઇન પર છું.",
        ("gu", False, True): "નમસ્તે, આ {company} તરફથી {contact_name} માટે "
                             "કૉલ છે, તેમના business વિશે.",
        ("gu", False, False): "નમસ્તે, આ {company} તરફથી તમારા business વિશે "
                              "કૉલ છે.",
    }

    @staticmethod
    def _machine_text_lang(text: str) -> str:
        if re.search(r"[\u0A80-\u0AFF]", text or ""):
            return "gu"
        if re.search(r"[\u0900-\u097F]", text or ""):
            return "hi"
        return "en"

    def _screener_reply(self, screener_text: str) -> str:
        """Identity + purpose for a call screener, in the SCREENER's language
        (iOS and Google screen in English whatever the customer speaks)."""
        lang = self._machine_text_lang(screener_text)
        raw = self._connect_cfg().get("screening_line")
        if isinstance(raw, str):
            raw = {"en": raw}
        if isinstance(raw, dict) and raw:
            line = self._personalize(
                str(self._lang_response(raw, lang=lang) or "").strip(),
                "call_flow.connect.screening_line")
            if len(line) >= 10:
                return line
        ident = self._def.identity
        agent = (ident.agent_name or "").strip()
        company = (ident.company_name or "").strip() or agent or "our team"
        has_agent = bool(agent) and agent.lower() not in company.lower()
        has_name = bool((self.outbound_contact_name or "").strip())
        tmpl = self._SCREENER_LINES[(lang, has_agent, has_name)]
        line = tmpl.replace("{agent}", agent).replace("{company}", company)
        return self._personalize(line, "machine.screener_reply")

    def _voicemail_cfg(self) -> dict:
        """call_flow.connect.voicemail: {action: hangup|leave_message,
        message: {lang: line}}. Default hangup — a mailbox is never pitched."""
        cfg = self._connect_cfg().get("voicemail")
        return cfg if isinstance(cfg, dict) else {}

    def _voicemail_message(self) -> str:
        cfg = self._voicemail_cfg()
        if str(cfg.get("action") or "hangup").strip().lower() != "leave_message":
            return ""
        raw = cfg.get("message")
        if isinstance(raw, str):
            raw = {"en": raw}
        if not isinstance(raw, dict) or not raw:
            return ""
        lang = self._machine_lang_before or self.state.language
        return self._personalize(
            str(self._lang_response(raw, lang=lang) or "").strip(),
            "call_flow.connect.voicemail.message")

    def awaiting_machine_handoff(self, now: float | None = None) -> bool:
        """True while a screener or a hold announcement has the line and we
        are waiting for a person — dead air is EXPECTED, so the pipeline's
        "Sorry, I couldn't hear you" repair and silence hangup stand down
        (call 919106094098: the repair was spoken into an iPhone screener).
        Bounded by MACHINE_WAIT_MAX_S so a screen nobody answers still ends."""
        if self._machine_wait_kind not in _machine.WAITING_KINDS:
            return False
        now = time.monotonic() if now is None else now
        if now - self._machine_wait_since >= MACHINE_WAIT_MAX_S:
            return False
        if self._machine_wait_started > 0.0 and \
                now - self._machine_wait_started >= self._machine_episode_max_s():
            return False
        return True

    def _machine_episode_max_s(self) -> float:
        """How long one hold / screener episode may run in total (§83)."""
        if self._machine_wait_kind == _machine.HOLD:
            raw = self._connect_cfg().get("hold_max_s")
            try:
                v = float(raw) if raw is not None else MACHINE_HOLD_MAX_S
            except (TypeError, ValueError):
                v = MACHINE_HOLD_MAX_S
            return v if v > 0 else MACHINE_HOLD_MAX_S
        return MACHINE_SCREENER_MAX_S

    def machine_wait_expired(self, now: float | None = None) -> str | None:
        """The WAITING kind (hold / screener) whose episode has ended with no
        person coming through — the announcement stopped MACHINE_WAIT_MAX_S
        ago, or the whole episode ran past its ceiling. Reported once per
        episode; the pipeline watchdog closes the call on it (RUNBOOK §83).
        Before §83 an expired wait fell back to "Sorry, I couldn't hear you"
        twice and a goodbye — thirty more seconds of TTS into a recording."""
        kind = self._machine_wait_kind
        if kind not in _machine.WAITING_KINDS or self._machine_timeout_fired:
            return None
        if self.hangup_pending or self._machine_wait_started <= 0.0:
            return None
        now = time.monotonic() if now is None else now
        if now - self._machine_wait_since >= MACHINE_WAIT_MAX_S:
            return kind
        if now - self._machine_wait_started >= self._machine_episode_max_s():
            return kind
        return None

    def machine_audio_close_allowed(self) -> bool:
        """May a confirmed mailbox BEEP end this call? Only on an outbound leg
        before any PERSON has spoken (every caller line so far was a machine's
        or there was none) — a beep after a human line is a sound in their
        room, and an inbound caller's phone is never a mailbox (§83)."""
        return (self.call_direction == "outbound" and not self._human_spoke
                and not self.hangup_pending)

    def arm_machine_timeout_close(self, kind: str) -> None:
        """A hold / screener episode ran its course with no person: close in
        silence. Called by the pipeline watchdog (§83)."""
        self._machine_timeout_fired = True
        self.call_outcome = self.call_outcome or f"{kind}_timeout"
        self._arm_machine_close(kind)

    def arm_machine_audio_close(self, evidence: str) -> None:
        """The audio layer confirmed a mailbox beep on an outbound leg before
        anyone spoke: a recording is about to start, and nothing the agent
        says belongs on it (§83)."""
        self.state_mgr.extra_flags["machine_evidence"] = evidence
        self._arm_machine_close(_machine.VOICEMAIL)

    def _arm_machine_close(self, kind: str, immediate: bool = True) -> None:
        """A mailbox or a network announcement: HARD close — its next
        sentence must not cancel the hangup the way a person's would.
        `immediate` (the default) also tells the pipeline to cut whatever the
        agent is saying and the bridge to hang up without draining it (§83);
        a close that speaks an authored voicemail message first passes
        False so the message is delivered before the channel drops."""
        self.state.is_closing = True
        self.commit_pending_turn()
        self.state.stage = STAGE_CLOSING
        self.state_mgr.extra_flags["machine_answered"] = kind
        self.call_outcome = self.call_outcome or kind
        self.hangup_pending = True
        self._hangup_hard = True
        self.hangup_immediate = bool(immediate) and MACHINE_CLOSE_IMMEDIATE
        self._machine_wait_kind = None
        self._machine_wait_started = 0.0
        logger.info("[MACHINE] %s — hard close%s", kind,
                    " (immediate)" if immediate else "")

    def _machine_close(self, kind: str, text: str):
        """VOICEMAIL / UNREACHABLE. Returns the EngineResult to speak (an
        authored voicemail message) or None (close in silence)."""
        msg = self._voicemail_message() if kind == _machine.VOICEMAIL else ""
        if msg:
            if not _machine.has_record_cue(text):
                # The greeting is still playing. Wait for "at the tone…"; if
                # it never comes, the dead-air watchdog leaves the message
                # (_generate_silence_reprompt_raw).
                self._machine_wait_kind = _machine.VOICEMAIL
                self._machine_wait_since = time.monotonic()
                self.commit_pending_turn()
                logger.info("[MACHINE] voicemail greeting — waiting for the "
                            "record cue before leaving the message")
                return None
            self._arm_machine_close(kind, immediate=False)
            logger.info("[MACHINE] leaving voicemail message: %r", msg[:60])
            return EngineResult(text=msg, is_shortcut=True, intent="voicemail")
        self._arm_machine_close(kind)
        return None

    def _machine_line_gate(self, text: str):
        """Decide a turn that may be a MACHINE talking, before anything else
        reads it. Returns:
          * None          — handled; say nothing,
          * EngineResult  — handled; speak exactly this,
          * str           — a PERSON's words (possibly with a machine prefix
                            removed); route them as a normal turn.
        Machine turns never touch memory, slots, QA, language or the stage.
        """
        kind, human = _machine.split(text)
        now = time.monotonic()
        waiting = self.awaiting_machine_handoff(now)

        # Screener / voicemail / network wording means a machine only before a
        # person has spoken (or while we already wait on one). Mid-call it is a
        # person talking ABOUT voicemail. A hold announcement can arrive any
        # time, and silence is the right answer to one from a person too.
        if kind is not None and self._human_spoke and not waiting \
                and kind != _machine.HOLD:
            logger.info("[MACHINE] %s wording mid-conversation — a person "
                        "talking, not a machine: %r", kind, text[:60])
            return text

        if kind is not None:
            logger.info("[MACHINE] %s line: %r (person also said: %r)",
                        kind, text[:80], human[:40])
            if self._machine_lang_before is None:
                self._machine_lang_before = self.state.language
            if kind in _machine.CLOSING_KINDS:
                return self._machine_close(kind, text)
            if self._machine_wait_kind is None or self._machine_wait_started <= 0.0:
                self._machine_wait_started = now
            if kind == _machine.SCREENER or self._machine_wait_kind is None:
                self._machine_wait_kind = kind
            self._machine_wait_since = now
            # A REPEATED announcement never extends the episode (§83): once
            # the whole hold / screen has run past its ceiling, close.
            _expired = None if human else self.machine_wait_expired(now)
            if _expired:
                logger.info("[MACHINE] %s episode has run %.0fs with no "
                            "person — closing", _expired,
                            now - self._machine_wait_started)
                self.arm_machine_timeout_close(_expired)
                return None
            if human:
                # "…please stay on the line. Hello?" — the person is here.
                return self._machine_pickup(human)
            self.commit_pending_turn()
            if kind == _machine.SCREENER and not self._screener_answered:
                self._screener_answered = True
                line = self._screener_reply(text)
                logger.info("[MACHINE] answering the call screener: %r",
                            line[:80])
                return EngineResult(text=line, is_shortcut=True,
                                    intent="screener")
            logger.info("[MACHINE] %s — staying silent", kind)
            return None

        if waiting:
            if _machine.is_residue(self._machine_wait_kind, text) or (
                    self._machine_wait_kind == _machine.HOLD
                    and not self._looks_like_hold_pickup(text)):
                # "thanks" / "please" — the screener's own courtesy line,
                # split by STT (answering it "You're welcome!" is what the
                # engine did before §63); "the person you are speaking with
                # has put" / "i'm better off" — the hold announcement in
                # pieces, each of which earned a fresh pitch before §83.
                self._machine_wait_since = now
                self.commit_pending_turn()
                logger.info("[MACHINE] %s still talking (%r) — silent",
                            self._machine_wait_kind, text[:40])
                return None
            return self._machine_pickup(text)

        if self._machine_wait_kind == _machine.VOICEMAIL:
            # Leave-message mode and the mailbox said something without a
            # record cue: keep waiting.
            self.commit_pending_turn()
            return None

        if self._machine_wait_kind is not None:
            # The wait expired (MACHINE_WAIT_MAX_S) — whatever this is, it is
            # the call now.
            self._machine_wait_kind = None
            self._machine_wait_started = 0.0
        self._human_spoke = True
        return text

    def _looks_like_hold_pickup(self, text: str) -> bool:
        """After a hold announcement, is this a PERSON back on the line?

        A greeting, an acknowledgement, a question, a hearing check, any
        affirmative / polite word, or four or more words. STT renders the
        tail of the announcement as two- and three-word garbage ("i'm better
        off", "come back, but", "ही स्टाइल बनाईन"), and before §83 each
        piece reached the model as the caller's turn and got another pitch
        (call 917265832771: seventeen of them over 8.5 minutes)."""
        t = (text or "").strip()
        if not t:
            return False
        if self._is_greeting_only_text(t) or self._is_bare_ack_text(t):
            return True
        if "?" in t or _HEARING_CHECK_RE.search(t):
            return True
        toks = _machine._WORD_RE.findall(t.lower())
        if any(w in _AFFIRM_TOKENS or w in _HELLO_CORE or w in _GREETING_CORE
               or w in _POLITE_TOKENS or w in _ACK_CORE
               or w in _HOLD_RETURN_CUES for w in toks):
            return True
        return len(toks) >= 4

    def _machine_pickup(self, text: str):
        """The first words of a PERSON after a screener or a hold."""
        was = self._machine_wait_kind
        self._machine_wait_kind = None
        self._machine_wait_started = 0.0
        self._machine_timeout_fired = False
        self._human_spoke = True
        if self._machine_lang_before and was == _machine.SCREENER:
            # The screener's English pinned nothing about THIS person.
            self.state.language = self._machine_lang_before
        if was == _machine.SCREENER and self._is_greeting_only_text(text):
            # They picked up and said hello: they have heard NOTHING — not the
            # opening, not what we told the screener. Open the call again.
            self._intro_delivered = True
            line = self.connect_intro_line()
            if line:
                logger.info("[MACHINE] person picked up after the screener "
                            "— re-opening: %r", line[:80])
                self.memory.add_assistant(line, language=self.state.language)
                self._store_last_response(line)
                self.commit_pending_turn()
                return EngineResult(text=line, is_shortcut=True,
                                    intent="connect_intro")
        logger.info("[MACHINE] person on the line after %s: %r", was, text[:60])
        return text

    def _callback_opening(self) -> str | None:
        """Opening line for a platform-scheduled follow-up call: warmly
        reference the earlier conversation and the caller's own callback
        request, then ask if now is a good time. Deterministic (no LLM on the
        latency-critical opening); language = the agent's opening language."""
        meta = self.callback_meta or {}
        if not meta:
            return None
        lang = (self.state.language or "en")[:2]
        name = (self.outbound_contact_name or "").strip()
        agent = (getattr(self._def.identity, "agent_name", "") or "").strip()
        company = (getattr(self._def.identity, "company_name", "") or "").strip()
        # How long ago the original call was, for a natural time reference.
        when_key = "recent"
        src = str(meta.get("source_call_at") or "")
        try:
            src_dt = datetime.fromisoformat(src.replace("Z", "+00:00"))
            if src_dt.tzinfo is None:
                src_dt = src_dt.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
            now_ist = datetime.now(timezone(timedelta(hours=5, minutes=30)))
            age = now_ist - src_dt
            if age <= timedelta(hours=3):
                when_key = "just_now"
            elif src_dt.astimezone(now_ist.tzinfo).date() == now_ist.date():
                when_key = "today"
            elif (now_ist.date() - src_dt.astimezone(now_ist.tzinfo).date()).days == 1:
                when_key = "yesterday"
            else:
                when_key = "recent"
        except (ValueError, TypeError):
            pass
        when = {
            "en": {"just_now": "a little while ago", "today": "earlier today",
                   "yesterday": "yesterday", "recent": "a few days ago"},
            "hi": {"just_now": "abhi thodi der pehle", "today": "aaj",
                   "yesterday": "kal", "recent": "kuch din pehle"},
            "gu": {"just_now": "hamna thodi var pehla", "today": "aaje",
                   "yesterday": "gai kale", "recent": "thoda divas pehla"},
        }.get(lang, {}).get(when_key) or {"just_now": "a little while ago",
                                          "today": "earlier today",
                                          "yesterday": "yesterday",
                                          "recent": "a few days ago"}[when_key]
        topic = str(meta.get("topic") or "").strip().rstrip(".")
        if len(topic) > 90:  # keep the opening breath-sized
            topic = ""
        who = f" {name}" if name else ""
        ident = " and ".join(x for x in (agent,) if x)
        frm = f" from {company}" if company else ""
        if lang == "hi":
            about = f" {topic} ke baare mein" if topic else ""
            return (f"Namaste{who}! Main {ident or 'aapka agent'}"
                    f"{' ' + company + ' se' if company else ''}. "
                    f"Humari {when} baat hui thi{about}, aur aapne mujhe call "
                    f"karne ko kaha tha — isliye aapko call kiya. "
                    f"Kya abhi baat karna theek rahega?")
        if lang == "gu":
            about = f" {topic} vishe" if topic else ""
            return (f"Namaste{who}! Hu {ident or 'tamaro agent'}"
                    f"{' ' + company + ' thi' if company else ''}. "
                    f"Aapni {when} vaat thai hati{about}, ane tame mane call "
                    f"karva kahyu hatu — etle call karyo chhe. "
                    f"Su atyare vaat kari shakiye?")
        about = f" about {topic}" if topic else ""
        return (f"Hello{who}! This is {ident or 'your assistant'}{frm}. "
                f"We spoke {when}{about}, and you'd asked me to give you a "
                f"call back — so here I am. Is now a good time to talk?")

    # Placeholder syntaxes the engine resolves in any authored spoken line —
    # {contact_name}, {current_time}, {honorific}, {title}, plus the variants
    # LLM editors keep inventing (${contact_name}, {{contact_name}}, [name],
    # <contact>, {Sir/Ma'am}, {Mr/Mrs}). A raw template token must NEVER be
    # spoken to a caller. Defined once in address.py; the prompt builder
    # resolves the same set in the author text it quotes to the model.
    _CONTACT_PH_RE = _address.CONTACT_PH_RE
    _TIME_PH_RE = _address.TIME_PH_RE
    _HONORIFIC_PH_RE = _address.HONORIFIC_PH_RE
    _TITLE_PH_RE = _address.TITLE_PH_RE

    def _substitute_tokens(self, text: str) -> str:
        """Resolve the placeholders an authored spoken line may carry.

        Substitution ONLY — no unresolved-token backstop. That split matters:
        `_lang_response` runs this over every authored line in the agent, and
        some of those carry tokens THIS function does not own and a later step
        does (`objection_playbook` answers are handed `{trust_proofs}` after
        the fact). Stripping here would eat them. `_personalize` is the
        substitute-then-strip pair for the surfaces that are finished lines.
        """
        if not text or "{" not in text and "[" not in text and "<" not in text \
                and "$" not in text:
            return text
        dropped = False
        if self._TIME_PH_RE.search(text):
            _tod = _time_of_day_word(text)
            text = self._TIME_PH_RE.sub(lambda _m: _tod, text)
        if self._HONORIFIC_PH_RE.search(text):
            # "" for an unknown gender — the token is removed and the seam it
            # leaves ("Good morning , am I") is tidied below, so the same
            # authored line works for a contact list that carries gender and
            # one that does not. That is the whole optionality contract.
            _hon = _honorific_word(self.outbound_contact_gender, text)
            text = self._HONORIFIC_PH_RE.sub(lambda _m: _hon, text)
            dropped = dropped or not _hon
        if self._TITLE_PH_RE.search(text):
            _ttl = _title_word(self.outbound_contact_gender, text)
            text = self._TITLE_PH_RE.sub(lambda _m: _ttl, text)
            dropped = dropped or not _ttl
        if dropped:
            text = _tidy_removed_token_seams(text)
        if self._CONTACT_PH_RE.search(text):
            # With no name on file the old fallback spoke the literal word
            # "sir/ma'am" in its place — "Am I speaking to sir/ma'am?" reached
            # real callers 29+ times, and it is gendered address to a contact
            # with no gender. Now a clause that only asked WHO they are is
            # dropped and the token simply goes everywhere else (RUNBOOK §42).
            # Last, so the identity-question test sees the finished line.
            text = _address.resolve_contact_name(
                text, self.outbound_contact_name)
        return text

    def _personalize(self, text: str, field: str) -> str:
        """Substitute, then refuse to speak whatever is still in braces.

        Used by the surfaces that hand their result straight to TTS — the
        opening greeting, the connect intro line, the identity-confirm beat —
        so a placeholder behaves identically wherever the owner types it and
        an invented token is never read aloud.
        """
        if not text:
            return text
        return self._finalize_spoken(
            strip_unresolved_tokens(self._substitute_tokens(text), field))

    # ── who is addressed, and who is speaking (RUNBOOK §42) ────────────────

    def _finalize_spoken(self, text: str) -> str:
        """The last word on every line the caller hears.

        Two rules, applied to authored, canned and model-written text alike:

          * the CALLER is addressed by their contact record — Sir/Ma'am and
            Mr./Mrs. in the right gender when it is known, and NO gendered
            address at all when it is not. A script that hardcoded "Sir", a
            placeholder, and a model that improvised "Mrs. Paul" are all held
            to the same rule;
          * the AGENT speaks in the gender of the voice the business chose —
            "kar rahi hoon" for simran, "kar raha hoon" for shubh — whatever
            the script was written in.

        Runs on process_stream output, the opening, every watchdog line and
        (through ConversationMemory.spoken_filter) the history the model reads
        back, so what is recorded is what was said. Never raises into a call.
        """
        if not text or not isinstance(text, str):
            return text
        # No voice we use can read Perso-Arabic script, and the model slips
        # into it mid-Gujarati/Hindi ("word of mouth થી જ زیادہ customers",
        # 3 live calls to 2026-09-28). Drop the run rather than speak noise.
        if _FOREIGN_SCRIPT_RE.search(text):
            cleaned = re.sub(r"[ \t]{2,}", " ", _FOREIGN_SCRIPT_RE.sub("", text))
            logger.warning("[SCRIPT] dropped Arabic-script text from a spoken "
                           "line: %r", text[:80])
            text = cleaned
            if not text.strip():
                return text
        try:
            out = _address.enforce_address(
                text, self.outbound_contact_gender, self.outbound_contact_name,
                self._protected_names())
            return _address.regender_self(out, self._self_gender_for(out))
        except Exception:
            logger.exception("[ADDRESS] finalize failed — speaking the line as is")
            return text

    def _render_author_line(self, text: str) -> str:
        """An owner line the PROMPT quotes (stage goal, outbound.* lines),
        rendered for this caller and this voice — see
        address.render_author_line."""
        if not text:
            return text
        try:
            return _address.render_author_line(
                text, contact_name=self.outbound_contact_name,
                contact_gender=self.outbound_contact_gender,
                agent_gender=self._self_gender_for(text),
                protected=self._protected_names())
        except Exception:
            logger.exception("[ADDRESS] author-line render failed")
            return text

    def _self_gender_for(self, text: str = "", lang: str | None = None) -> str:
        """The agent's own gender for a line — from the voice that will SPEAK
        it: the line's script picks hi/gu, otherwise the call's language."""
        if lang is None:
            _s = _address.script_of(text or "")
            lang = _s if _s != "en" else (self.state.language or "")
        cache = self.__dict__.setdefault("_self_gender_cache", {})
        if lang not in cache:
            cache[lang] = _address.agent_gender(self._def, lang)
        return cache[lang]

    def _protected_names(self) -> frozenset:
        """Third parties this agent's own text names with a title ("Mr.
        Umang, our CEO") — never re-gendered to the caller, never stripped."""
        cached = self.__dict__.get("_protected_names_cache")
        if cached is not None:
            return cached
        texts: list[str] = []

        def _walk(x) -> None:
            if isinstance(x, str):
                texts.append(x)
            elif isinstance(x, dict):
                for v in x.values():
                    _walk(v)
            elif isinstance(x, (list, tuple)):
                for v in x:
                    _walk(v)
            elif hasattr(x, "__dataclass_fields__"):
                for f in x.__dataclass_fields__:
                    _walk(getattr(x, f, None))

        try:
            _walk(self._def)
        except Exception:
            logger.exception("[ADDRESS] protected-name scan failed")
        names = _address.protected_names(texts)
        self.__dict__["_protected_names_cache"] = names
        return names

    async def generate_opening(self) -> EngineResult:
        """Inbound warm welcome — agent answers the call.
        Bilingual so caller can respond in their preferred language."""
        greeting = self._def.response_bank.opening_greeting
        # D6: outbound agents may carry a per-language outbound greeting —
        # preferred when this call is outbound and the field is non-empty.
        if self.call_direction == "outbound":
            _ob_greeting = self._lang_response(self._def.outbound.opening_greeting)
            if _ob_greeting:
                greeting = _ob_greeting
            # Platform rule (all agents): a scheduled follow-up call must OPEN
            # by referencing the earlier conversation and the caller's own
            # request — never with the cold outbound pitch. Built from the
            # structured callback_meta the auto-dial executor stamped on the
            # dial record; falls back to the normal greeting when absent.
            _cb_greeting = self._callback_opening()
            if _cb_greeting:
                greeting = _cb_greeting
            else:
                greeting = self._pick_opening(
                    greeting, self._def.outbound.opening_variants)
        else:
            greeting = self._pick_opening(
                greeting, self._def.response_bank.opening_variants)
        greeting = self._personalize(greeting, "opening_greeting")
        if not (greeting or "").strip():
            # The authored opening only asked who the caller is, and this
            # contact has no name on file — open with who WE are instead of
            # silence (the pipeline would otherwise substitute "Sorry, could
            # you repeat that?" as the first thing a caller ever hears).
            greeting = self._finalize_spoken(self._composed_intro_line())
            logger.info("[OPENING] no name for an identity-question opening "
                        "— composed intro instead")
        self.memory.add_assistant(greeting, language=self.state.language)
        self.state.stage = STAGE_GREETING
        # Store for last-response memory (feature 3)
        self.last_agent_response = greeting
        self.last_agent_language = self.state.language
        # Anchor for the early-call "hello?" repair window.
        self._opening_ts = time.monotonic()
        self.memory.qa_tracker.observe_agent_turn(greeting, self.memory.turn_index)
        logger.info("[OPENING] Inbound welcome delivered")
        return EngineResult(text=greeting, is_shortcut=True, intent="greeting")

    # ── per-call variety (RUNBOOK §51) ───────────────────────────────────
    # An owner test-calling their own agent hears the same opening sentence
    # and the same second question on every call, because both are fixed text
    # and the prompt is identical from call to call. Two per-CALL choices fix
    # that without touching a word of any existing agent: which authored
    # opening to speak, and which conversational angle to take. Both are
    # decided once and held for the call — the opening because a "hello?"
    # replays it, the angle because it sits in the prompt's per-call band.
    # `vary_per_call = False` (previews, boot check) always takes the first.

    vary_per_call: bool = True

    def _pick_opening(self, authored: str, variants: dict) -> str:
        """The authored opening, or — when the agent carries alternatives for
        this language — one of them, chosen at random for this call."""
        lang = (self.state.language or "en")[:2]
        pool = [str(v) for v in ((variants or {}).get(lang) or []) if str(v).strip()]
        if not pool or not self.vary_per_call:
            return authored
        if (authored or "").strip():
            pool = [authored] + pool
        import random as _random
        choice = _random.choice(pool)
        logger.info("[OPENING] variant %d of %d chosen for this call",
                    pool.index(choice) + 1, len(pool))
        return choice

    def _call_style_lines(self) -> list[str]:
        """━━ THIS CALL ━━ — one of personality.style_variants, fixed for the
        call. Empty list = no band at all (every pre-existing agent)."""
        variants = list(getattr(self._def.personality, "style_variants", None) or [])
        if not variants:
            return []
        if getattr(self, "_call_style", None) is None:
            import random as _random
            self._call_style = (_random.choice(variants) if self.vary_per_call
                                else variants[0])
            logger.info("[STYLE] this call: %s", self._call_style[:90])
        return [
            "",
            "━━ THIS CALL ━━",
            "No two calls should sound alike. For THIS call, take this angle — "
            "it shapes HOW you say things, never what you are allowed to say:",
            f"▸ {self._call_style}",
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def process(self, text: str) -> EngineResult:
        parts: list[str] = []
        intent = "general"
        is_shortcut = False
        async for result in self.process_stream(text):
            parts.append(result.text)
            intent = result.intent
            is_shortcut = result.is_shortcut
        return EngineResult(text=" ".join(parts), is_shortcut=is_shortcut, intent=intent)

    # RUNBOOK §70 (call 1033): asked "where are you calling from?", the caller
    # said "Vapi में" three times and STT returned "अभी मैं", "बाबी में" and
    # "I like it." A short city name is the hardest thing to hear on a phone
    # line. Sound-alikes are corrected ONLY when the agent's last line asked a
    # place question, the reply is a few words, and the city is one this
    # agent's knowledge base names — "अभी मैं" anywhere else is left alone.
    _CITY_SOUNDALIKES = {
        "vapi": ("अभी", "बाबी", "बापी", "वाबी", "वापी", "वापि", "आपी", "वैपी",
                 "वापिस", "बापु", "वाप्पी", "abhi", "baabi", "babi", "bapi",
                 "wapi", "vappi", "vaapi", "આપી", "વાપી", "બાપી"),
    }
    _CITY_TAIL = r"(?:\s*(?:में|मैं|मे|से|ही|में ही|से ही|me|mai|main|mein|se|hi|માં|થી))?"

    def _contextual_city_fix(self, text: str) -> str:
        try:
            slot = str(getattr(self, "_last_asked_slot", "") or "").lower()
            if not any(k in slot for k in ("calling_from", "city", "location",
                                           "where", "place", "area")):
                return text
            t = (text or "").strip()
            if not t or len(t.split()) > 4:
                return t or text
            cities = [str(c).strip() for c in
                      (getattr(self._def.knowledge_base, "locations", None) or [])]
            for city in cities:
                alikes = self._CITY_SOUNDALIKES.get(city.lower())
                if not alikes:
                    continue
                alt = "|".join(sorted(map(re.escape, alikes), key=len, reverse=True))
                rx = re.compile(rf"^\s*(?:{alt})(?=$|[\s,।.!?]){self._CITY_TAIL}"
                                r"\s*[,।.!?]*\s*$", re.IGNORECASE)
                m = rx.match(t)
                if m:
                    first = t.split()[0]
                    rest = t[t.find(first) + len(first):]
                    # "अभी मैं" → "Vapi में", not "Vapi मैं".
                    rest = re.sub(r"^(\s*)मैं", r"\1में", rest)
                    fixed = city + rest
                    logger.info("[STT-CTX] %r → %r (answer to a place question)",
                                t, fixed)
                    return fixed
        except Exception:
            logger.debug("[STT-CTX] city fix failed", exc_info=True)
        return text

    async def process_stream(self, text: str):
        """Every reply to a caller turn, finalized for address and self-gender.

        One choke point instead of forty: the engine below has dozens of
        yield sites (shortcuts, playbook answers, close-confirm, callback
        pin-down, streamed model chunks), and each would otherwise need to
        remember the rule. A chunk the rule empties entirely is dropped rather
        than handed to TTS as blank text, which the pipeline would replace
        with "Sorry, could you repeat that?"."""
        text = self._contextual_city_fix(text)
        agen = self._process_stream_raw(text)
        try:
            async for result in agen:
                t = getattr(result, "text", None)
                if isinstance(t, str) and t:
                    ft = self._finalize_spoken(t)
                    if ft != t:
                        if not ft.strip():
                            continue
                        # copy, not a fresh EngineResult: callers hang extra
                        # attributes on results and those must survive.
                        import copy as _copy
                        result = _copy.copy(result)
                        result.text = ft
                yield result
        finally:
            await agen.aclose()

    async def _process_stream_raw(self, text: str):
        """
        Proactive streaming conversation engine.

        Extracts structured data, advances the booking flow, and falls back
        to LLM for questions and natural phrasing.
        """
        # Turn transaction: snapshot BEFORE any mutation so a continuation of
        # the caller's previous utterance can supersede this whole turn.
        self.begin_turn_transaction(text)

        # B2: refresh capture-mode state FIRST — the language tiering (A3)
        # and the pipeline's endpointing/STT forcing read it this turn.
        self._refresh_capture_slot()

        # Machine speech (RUNBOOK §63) is decided BEFORE anything reads the
        # turn: a screener's words are not an answer, not language evidence,
        # not a reason to advance a stage and not a line for the model's
        # history. It returns the (possibly shortened) text a PERSON said, or
        # None when the whole turn was handled here.
        if not (self.hangup_pending and self._hangup_hard):
            _gated = self._machine_line_gate(text)
            if isinstance(_gated, EngineResult):
                yield _gated
                return
            if _gated is None:
                return
            text = _gated

        # A new caller utterance cancels a pending agent hangup: "wait, one
        # more question" after the goodbye must be answered, not cut off.
        # Every close path below re-arms it if this turn also ends the call.
        # A HARD close (max-duration cap) is the one exception — the platform
        # is ending the call, so further speech is left unanswered.
        if self.hangup_pending:
            if self._hangup_hard:
                logger.info("[HANGUP] hard close pending — caller speech ignored")
                return
            logger.info("[HANGUP] caller spoke again — hangup cancelled")
            self.hangup_pending = False
        # Stale end-call signal from a cancelled/superseded LLM stream must
        # never arm a hangup on a later turn.
        self._llm_end_call_seen = False

        # v3 commitment clarification: the suppression window (armed when we
        # ask "what day works?") decays one notch per user turn.
        if self._suppress_time_match_turns > 0:
            self._suppress_time_match_turns -= 1

        # Save name state before add_user may auto-extract a name (feature 4)
        _prev_booking_name = self.memory.booking.user_name

        # Context-aware name capture: if agent just asked for name, treat this
        # short response as the caller's name (even without "my name is" prefix).
        # Skipped while a name confirmation is pending — the reply to "Your
        # name is X, right?" is a yes/no, and capturing it here used to store
        # garbage like "Nahi Rajesh" as the confirmed name.
        if not self.memory.booking.user_name and not self._name_pending:
            ctx_name = self._extract_name_if_asked(text)
            if ctx_name:
                # Drop confirmation words that leak into the capture window.
                _toks = [t for t in ctx_name.split()
                         if t.lower() not in ("nahi", "no", "haan", "yes", "ji", "han")]
                ctx_name = " ".join(_toks) if _toks else ""
            if ctx_name:
                self.memory.booking.user_name = ctx_name
                self.memory.context.user_name = ctx_name
                self.memory.call_state.customer_name = ctx_name
                _prev_booking_name = ctx_name  # treat as already known
                # Direct answer to an explicit name question = high confidence:
                # confirm immediately and write through to the state manager.
                # (Previously _name_confirmed stayed False on this path, the
                # sync gate never fired, and the state block demanded the name
                # be re-asked forever — the critical review finding.)
                self._name_confirmed = True
                self.state_mgr.set_externally("name", ctx_name, source="system")
                logger.info("[NAME] Context-aware capture (confirmed): %r", ctx_name)

        self.memory.add_user(text, language=self.state.language)

        # QA: record this user turn against any pending asked-fields.
        # A bare "hello?" is a can-you-hear-me repair signal, not an answer —
        # marking the asked field answered with it makes the agent never
        # re-ask (live incident: name recorded as 'hello hello hello').
        # Nor is a question ("calling? from where you are calling?") or, for
        # anything but a yes/no, a bare "okay okay" (§63): each was recorded
        # as an answer on call 919426600929, and the QUESTIONS ALREADY
        # ANSWERED band then told the model to move on to the next one.
        if not self._is_greeting_only_text(text) \
                and not self._is_question_only(text):
            _only = None
            if self._is_bare_ack(text):
                _only = {n for n, sp in self.state_mgr.slots.items()
                         if getattr(sp, "type", "") == "boolean"}
            self.memory.qa_tracker.observe_user_turn(
                text, self.memory.turn_index, only_fields=_only)

        # ── Conversation State layer: extract + merge EVERY user turn ──
        # Runs BEFORE shortcut routing so slot values embedded in shortcut
        # turns ("thank you, 2BHK works") are never dropped. Pure regex,
        # <1 ms — adds nothing to perceived latency.
        try:
            # `asked`: what the agent's LAST line actually asked about (read
            # from the utterance, not the directive) — a yes/no or an
            # everyday word this turn answers that and nothing else.
            self._turn_non_answer = self._non_answer_kind(text)
            self._turn_caller_text = text
            _state_changes = self.state_mgr.observe_user(
                # The precise reading (the slot's own question text) first;
                # the label/slot-name scan only when it found nothing (§75).
                # A question that LISTS a slot's options asked that slot,
                # whatever the wording around it (calls 1080/1081).
                text, asked=(self.state_mgr.options_asked(
                                 self.last_agent_response or "")
                             or getattr(self.memory.qa_tracker,
                                        "last_asked_field", None)
                             or self._last_asked_slot),
                # A question back, confusion or a hello is not an answer:
                # the question's ask budget is not spent on it (§69).
                non_answer=bool(self._turn_non_answer) or "?" in text)
            if _state_changes:
                logger.info("[STATE] Turn extracted %d field(s): %s",
                            len(_state_changes),
                            ", ".join(f"{c.field}={c.new!r}" for c in _state_changes))
            if not _state_changes or not self._record_on_main_call():
                # LLM gap-filler when the regex pass found nothing new:
                # structured utterances are already covered, and this halves
                # extractor request volume against the shared quota. An agent
                # with no record tool on its spoken path has no OTHER writer,
                # so one regex hit must not cost the rest of the sentence —
                # sim 2026-09-28: "own plant" matched, and name, company,
                # product and turnover in the same breath were never read.
                # The extractor asks only for still-missing slots.
                self._spawn_slot_extractor(text)
        except Exception:
            logger.exception("[STATE] observe_user failed (continuing)")

        if self._pitch is not None:
            self._pitch_observe_caller(text)
        if self._share is not None:
            self._share_observe_caller(text)
        # Opt-in ━━ PRICE ━━ band (pricing_policy.second_ask_approx): the
        # first price question gets the policy line, a second one the
        # approximate figure — the prompt rule alone did neither (call 1074).
        self._price_ask_pending = False
        if self._price_second_ask_enabled() and _PRICE_ASK_RE.search(text or ""):
            self._price_asks += 1
            self._price_ask_pending = True
            logger.info("[PRICE] caller asked about price (ask #%d)", self._price_asks)

        # Name-capture: if a new name was just extracted this turn, hold for confirmation
        if (not self._name_confirmed
                and not self._name_pending
                and self.memory.booking.user_name
                and self.memory.booking.user_name != _prev_booking_name):
            self._name_pending = self.memory.booking.user_name
            self._name_pending_turn = self.state.turn_count
            self.memory.booking.user_name = None
            self.memory.context.user_name = None
            self.state_mgr.mark_pending("name")
            logger.info("[NAME] Name pending confirmation: %r", self._name_pending)

        # --- Language detection + stabilization ---
        detected_lang, conf, src = await self.detect_language_v3(text)
        self.state.turn_count += 1
        logger.info('[LANG] "%s" -> %s (src=%s, conf=%.2f, locked=%s)',
                     text, detected_lang, src, conf, self.state.language_locked)

        self.state.language_history.append(detected_lang)
        history = self.state.language_history[-5:]
        self.state.language_history = history

        prev_lang = self.state.language

        # Decay the lock after 10 turns — allows the call to re-evaluate language
        # within a reasonable window when the caller switches naturally.
        # An explicit language_switch intent resets lock_turns to -10 so it
        # persists for 20 turns before decaying, giving strong persistence after
        # a deliberate switch request.
        if self.state.language_locked:
            self.state.language_lock_turns += 1
            if self.state.language_lock_turns > 10:
                self.state.language_locked = False
                logger.info("[LANG] Lock expired after %d turns", self.state.language_lock_turns)
        _lock_before = (self.state.language_locked, self.state.language_lock_turns)

        if self._evidence_switch_enabled():
            # ── A2 (2026-07-30): evidence-density switching ──────────────
            # One uniform scorer for every pair, both directions. Replaces
            # the single-strong-marker fast path and the 2/4-ASCII-word
            # English switch — both switched whole calls on a single signal
            # (live incident: one buggy marker hit on "अच्छे" turned a Hindi
            # call Gujarati; the name "alvin anthony" turned it English).
            # Explicit requests (intent=language_switch, handled below) stay
            # instant for all pairs and are the only path that sets a lock.
            if (self._turn_stt_source in ("fallback", "primary-script-retry")
                    and not _INDIC_EVIDENCE_RE.search(text)):
                # Tier 0: recovered LATIN transcripts are mishear-prone
                # (', angie?' from "हाँजी") — they never vote on language.
                # But Indic script inside a recovered transcript is unfakeable
                # evidence (see _INDIC_EVIDENCE_RE) and votes normally,
                # keeping the switcher alive through hedge-heavy stretches.
                logger.info("[LANG] Turn doesn't vote (stt_source=%s)",
                            self._turn_stt_source)
            elif self._capture_slot is not None or self._name_pending:
                # Tier 0: names/emails being captured are language-neutral.
                logger.info("[LANG] Turn doesn't vote (capture/name pending)")
            elif self.state.language_locked:
                # An explicit caller choice is honored until its lock decays.
                logger.info("[LANG] Evidence switching paused (explicit lock on %s)",
                            prev_lang)
            else:
                _target, _reason = self._evidence_switch_decision(text)
                if _target is not None:
                    logger.info("[LANG] Evidence switch %s → %s (%s)",
                                prev_lang, _target, _reason)
                    self.state.language = _target
                    self._last_evidence_switch_turn = self.state.turn_count
                elif _reason not in ("current",):
                    logger.info("[LANG] No switch (%s, staying %s)",
                                _reason, prev_lang)
        else:
            # ── Legacy single-signal switching (LANG_EVIDENCE_SWITCH=false) ──
            is_gujarati_detected = (
                detected_lang == "gu"
                or src == "script_gu_markers"
            )

            # Override an incorrect Hindi/English lock to Gujarati when the evidence is clear.
            if is_gujarati_detected and self.state.language_locked and prev_lang != "gu":
                recent_gu = sum(1 for l in history[-2:] if l == "gu")
                single_strong = (recent_gu >= 1 and src == "script_gu_markers")
                if recent_gu >= 2 or single_strong:
                    logger.info(
                        "[LANG] Overriding lock to Gujarati (src=%s, recent_gu=%d, "
                        "single_strong=%s)",
                        src, recent_gu, single_strong,
                    )
                    self.state.language = "gu"
                    self.state.language_locked = False
                    self.state.language_lock_turns = 0

            # Override a Gujarati lock to Hindi/English when the caller clearly switches.
            if (
                not is_gujarati_detected
                and self.state.language_locked
                and prev_lang == "gu"
                and detected_lang in ("hi", "en")
                and conf >= 0.7
            ):
                recent_non_gu = sum(1 for l in history[-2:] if l != "gu")
                if recent_non_gu >= 2:
                    logger.info(
                        "[LANG] Overriding Gujarati lock to %s "
                        "(recent_non_gu=%d, src=%s, conf=%.2f)",
                        detected_lang, recent_non_gu, src, conf,
                    )
                    self.state.language = detected_lang
                    self.state.language_locked = False
                    self.state.language_lock_turns = 0

            # Gujarati script or markers — switch ONLY if not locked to a different
            # language.
            if is_gujarati_detected and (
                not self.state.language_locked or prev_lang == "gu"
            ):
                if self.state.language != "gu":
                    logger.info("[LANG] Switching to Gujarati (src=%s)", src)
                self.state.language = "gu"
            elif is_gujarati_detected:
                logger.info(
                    "[LANG] Ignoring Gujarati markers (locked to %s, src=%s)",
                    prev_lang, src,
                )
            elif src == "pattern" and conf >= 0.7:
                # Pattern-based detection: Hindi/Gujarati romanized words
                self.state.language = detected_lang
            elif src == "script" and detected_lang == "hi":
                # Devanagari text — could be Hindi OR Gujarati via STT.
                if prev_lang != "gu" or not self.state.language_locked:
                    self.state.language = "hi"
            elif detected_lang == "en" and src in ("default", "pattern"):
                # Pure-Latin / non-Indic text: multi-word threshold.
                # Politeness and bare yes/no are not English evidence
                # (call 934 — see _LANG_NEUTRAL_TOKENS).
                word_count = _language_content_words(text)
                min_words_for_switch = 4 if self.state.language_locked and prev_lang in ("hi", "gu") else 2
                if word_count >= min_words_for_switch and prev_lang != "en":
                    logger.info(
                        "[LANG] Switching to English (ascii multi-word, src=%s, "
                        "words=%d, min=%d, locked=%s)",
                        src, word_count, min_words_for_switch, self.state.language_locked,
                    )
                    self.state.language = "en"
                    self.state.language_locked = False
                    self.state.language_lock_turns = 0
                elif word_count < min_words_for_switch and prev_lang in ("hi", "gu"):
                    logger.info(
                        "[LANG] Suppressing English switch (too few words=%d, min=%d, "
                        "maintaining %s)",
                        word_count, min_words_for_switch, prev_lang,
                    )
            # Ambiguous/default: preserve current language

        # ── Every automatic switch is bounded by the agent's build ─────────
        # The A2 switcher above checks languages.auto_switch and
        # _switchable_languages(); the legacy branch never did. An agent built
        # Hindi-only (RUNBOOK §70: supported [hi], evidence_switch false)
        # therefore ran the ONE switcher with no bound on it — call 1059
        # (2026-10-11, RUNBOOK §73): "3 bhk" → "Switching to English (ascii
        # multi-word)" and a Hindi caller was answered in English. The bound
        # is enforced here, after both branches, so no path can carry a call
        # into a language the agent has no greeting, no responses and no TTS
        # voice for. Explicit caller requests (intent=language_switch, below)
        # keep their own bounded path.
        _allowed = self._switchable_languages()
        if self.state.language != prev_lang:
            if not self._auto_switch_enabled():
                logger.info("[LANG] Switch %s → %s refused — languages."
                            "auto_switch is off", prev_lang, self.state.language)
                self.state.language = prev_lang
                self.state.language_locked, self.state.language_lock_turns = _lock_before
            elif self.state.language not in _allowed:
                logger.info("[LANG] Switch %s → %s refused — agent speaks %s",
                            prev_lang, self.state.language, sorted(_allowed))
                self.state.language = prev_lang
                self.state.language_locked, self.state.language_lock_turns = _lock_before
        if self.state.language not in _allowed:
            # The call was already outside the build (a seed or mirror from
            # before this guard existed): pin it to the agent's default.
            _default = str(self._def.languages.default or "en").strip()[:2].lower()
            _pin = _default if _default in _allowed else sorted(_allowed)[0]
            logger.info("[LANG] Language %r is not one this agent speaks — "
                        "pinned to %s", self.state.language, _pin)
            self.state.language = _pin

        self.state.lang_confidence = conf
        self.state.lang_source = src
        logger.info('[LANG] Response language: %s (detected=%s, prev=%s)',
                     self.state.language, detected_lang, prev_lang)

        # Reset per-turn metrics
        self.state.filler_used = False
        self.state.llm_cancelled = False
        self.state.parallel_execution = True
        self.state.objection_handled = False
        self._turn_lang_directive = None

        # --- Intent detection ---
        _kb = self._def.knowledge_base
        # objection_patterns_mode 'extend' merges the universal deferral set
        # with the agent's custom patterns; 'replace' (default) keeps today's
        # semantics where any custom list disables the ~28 universal phrases
        # (load-bearing for both Pramukh agents' custom lists).
        _obj_patterns = _kb.objection_patterns
        if (_obj_patterns and str(getattr(
                _kb, "objection_patterns_mode", "replace")) == "extend"):
            _obj_patterns = list(_DEFAULT_OBJECTION_PATTERNS) + list(_obj_patterns)
        intent = detect_intent(
            text,
            self.state.stage,
            location_aliases=frozenset(_kb.location_aliases),
            objection_patterns=_obj_patterns,
            time_patterns=_kb.time_patterns,
        )

        # --- Fire LLM in background (parallel with deterministic routing) ---
        llm_queue: asyncio.Queue[str | None] = asyncio.Queue()
        llm_done = asyncio.Event()
        llm_task = asyncio.create_task(
            self._run_llm_to_queue(text, llm_queue, llm_done),
            name="parallel_llm",
        )

        def _cancel_llm():
            if not llm_task.done():
                llm_task.cancel()
            self.state.llm_cancelled = True

        self.state.last_intent = intent
        logger.info('[INTENT] "%s" -> %s (stage=%s)', text, intent, self.state.stage)

        # ── DND: "stop calling me" is a compliance event, not an objection ──
        # Detected deterministically (platform-wide, all agents) and STORED:
        # the flag rides collected_data into the intel pipeline, which sets
        # the contact's disposition to 'rejected' (never auto-dialled again),
        # cancels open follow-ups, and forces the follow-up decision to NO.
        # The reply itself stays with the LLM — the prompt already handles
        # polite disengagement; what must never depend on the model is the
        # RECORD of the request.
        if _detect_dnd_request(text):
            if not self.state_mgr.extra_flags.get("dnd_requested"):
                self.state_mgr.extra_flags["dnd_requested"] = True
                self.state_mgr.extra_flags["dnd_quote"] = text[:200]
                logger.info("[DND] caller asked not to be contacted: %r",
                            text[:80])
            # An explicit opt-out ends the call HERE, on the turn it is
            # said. Left to the paths below it was answered by the
            # wrong-number re-confirmation ("is this the wrong number, or
            # the right one?") or by the model — the caller who said "don't
            # call me again" was asked again and again (Aristo test calls,
            # 2026-10-11). A never-confirmed identity gets the apology
            # line; anyone else the configured decline farewell.
            _cancel_llm()
            self.state.is_closing = True
            self.commit_pending_turn()  # closing turn — never rolled back
            self.state.stage = STAGE_CLOSING
            self._wrong_number_pending = 0
            self.state.decline_count += 1   # still counted, as on the objection path
            self.call_outcome = self.call_outcome or "dnd"
            self.hangup_pending = True
            _close_lang = self._closing_language()
            if (self.call_direction == "outbound"
                    and (self._identity_other
                         or self.state_mgr.extra_flags.get("wrong_person")
                         or self.state_mgr.extra_flags.get(
                             "wrong_number_suspected")
                         or not self._good_time_spoken)):
                resp = self._wrong_number_close_line()
            else:
                resp = self._lang_response(
                    self._def.response_bank.decline_farewell,
                    lang=_close_lang) \
                    or "Thank you for your time. Have a great day!"
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            yield EngineResult(text=resp, is_shortcut=True, intent="farewell")
            return

        # Reset decline counter when the user re-engages with a non-objection
        # intent — we don't want a stale "1 prior decline" to trip the 2-strike
        # farewell after the user has clearly come back into the conversation.
        if intent not in ("objection", "farewell"):
            self.state.decline_count = 0

        # =============================================================
        # STAGE-AWARE ROUTING
        # =============================================================

        # --- Already answered on speech onset (2026-08-17) ---------------
        # The pipeline spoke the connect intro the moment the caller started
        # talking. This transcript is that same "hello" arriving ~700ms later
        # through endpoint+STT; replying again would be two agent lines for
        # one caller utterance. Absorb it — and absorb the repeat hellos that
        # ride in behind it, which is what the caller does while the intro is
        # still playing. Anything with content falls through and is answered
        # normally.
        if (self._connect_onset_absorb_until > time.monotonic()
                and self._is_greeting_only_text(text)):
            _cancel_llm()
            logger.info("[CONNECT] Absorbing bare hello %r — already answered "
                        "on onset", text[:40])
            # Leave no trace (RUNBOOK §65): kept as a provisional turn, this
            # hello was glued onto the caller's next words by the continuation
            # merge ("हेलो।" + "हां जी हां जी" — call 1008), and counted as a
            # turn that had been answered.
            self.rollback_pending_turn()
            return

        # --- "Are you a real person or an AI?" — always the truth (§66) ---
        # Platform rule, every agent, only when explicitly asked. Ahead of the
        # identity shortcut ("who is this?" answers with a name) and the FAQ
        # playbook, so no canned line can answer this question with anything
        # but yes. An owner's own playbook answer that admits it is kept.
        if _ai_disclosure.asks_if_ai(text):
            _cancel_llm()
            resp = self._ai_disclosure_reply(text)
            logger.info("[AI-DISCLOSURE] caller asked %r → %r",
                        text[:60], resp[:80])
            self._refund_turn_ask()
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            yield EngineResult(text=resp, is_shortcut=True,
                               intent="ai_disclosure")
            return

        # --- Early-call "hello?" — caller didn't hear us; say it again ---
        # (platform rule, all agents). Capped at 2 replays so a caller who
        # actually wants to open with "hello" still gets a conversation.
        # Never replay a greeting that is ITSELF just "hello?" — the rule
        # assumes the caller missed something worth hearing again, and a bare
        # hello carries nothing. Replaying it produces the exact parrot loop
        # users report: agent "Hello?" → caller "hello" → agent "Hello?".
        # A hello answering a hello IS contact established; fall through to the
        # normal stage routing so the agent gets on with its script.
        # Once the second beat has been spoken the caller has heard who we
        # are; a further hello is a presence check (§63) — "yes, I can hear
        # you" + the question — not the same 10-second line again.
        if (self.last_agent_response and self._hello_replays < 2
                and not self._intro_delivered
                and not self._is_greeting_only_text(self.last_agent_response)
                and self._is_early_hello(text)):
            _cancel_llm()
            self._hello_replays += 1
            resp = self.last_agent_response
            # The owner wrote a second beat: say THAT, once, instead of the
            # opening again. A replay is the right repair when nothing else
            # exists, but word-for-word it is the most tape-like thing a
            # caller can hear — live 2026-09-18 (call 887) the same 10-second
            # opening twice was 21 of the call's 35 seconds. Until now
            # intro_line was consulted only when the opening was a bare
            # "Hello", so an agent with a real opening sentence could not
            # have one.
            _second = "" if self._intro_delivered else self._connect_intro_configured()
            if _second:
                self._intro_delivered = True
                resp = _second
            logger.info("[REPAIR] Early 'hello' #%d — %s: %r",
                        self._hello_replays,
                        "owner's second beat" if _second else "replaying last response",
                        resp[:60])
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            yield EngineResult(text=resp, is_shortcut=True, intent="repeat")
            return

        # --- Connect hello fast path (platform rule, all agents) ---
        # The complement of the replay above: when the last utterance was
        # ITSELF just "hello" (bare opening) there is nothing worth
        # replaying, and before this branch existed the caller's first
        # "hello?" paid a full 1.2-1.5s LLM turn — dead air that keeps the
        # hello loop alive. Answer instantly with a full identity intro.
        if (CONNECT_HELLO_FASTPATH
                and self._connect_cfg().get("hello_fastpath") is not False
                and not self._intro_delivered
                and self._is_early_greeting(text)
                and (not self.last_agent_response
                     or self._is_greeting_only_text(self.last_agent_response))):
            resp = self.connect_intro_line()
            if resp:
                _cancel_llm()
                self._intro_delivered = True
                logger.info("[REPAIR] Connect hello — instant intro: %r",
                            resp[:60])
                self.memory.add_assistant(resp, language=self.state.language)
                self._store_last_response(resp)
                yield EngineResult(text=resp, is_shortcut=True,
                                   intent="connect_intro")
                return

        # --- Repeat request (feature 3) — replay or re-render last agent response ---
        if self._is_repeat_request(text) and self.last_agent_response:
            # Check if caller also requested a language change (e.g. "repeat in Gujarati")
            _repeat_has_lang = any(
                p in text.lower()
                for p in [
                    "gujarati", "hindi", "english",
                    "ગુજરાતી", "gu ma", "gu mein",
                    "हिंदी", "हिन्दी", "hindi mein", "hindi mai",
                    "english mein", "english mai",
                ]
            )
            if _repeat_has_lang:
                # Extract desired language and re-render via LLM in the new language
                new_lang = self._extract_requested_language(text)
                if new_lang != self.state.language and \
                        new_lang not in self._switchable_languages():
                    # Bounded like every other switch (RUNBOOK §73): a
                    # Hindi-only agent re-renders in Hindi, never in a
                    # language it has no voice for.
                    logger.info("[REPEAT] Language-change repeat to %s refused "
                                "— agent speaks %s", new_lang,
                                sorted(self._switchable_languages()))
                    new_lang = self.state.language
                if new_lang != self.state.language:
                    logger.info("[REPEAT] Language-change repeat: %s -> %s", self.state.language, new_lang)
                    self.state.language = new_lang
                    self.state.language_locked = True
                    self.state.language_lock_turns = -7
                    self.state.language_history = [new_lang] * 5
                logger.info("[REPEAT+LANG] Re-rendering last response in %s", self.state.language)
                # LLM sees the updated language in the system prompt; the
                # directive makes the re-render explicit so it never answers
                # with a bare "sure, let's speak X" acknowledgment.
                self._arm_lang_redeliver_directive(self.state.language)
            else:
                _cancel_llm()
                resp = self.last_agent_response
                logger.info("[REPEAT] Replaying last response (lang=%s): %r",
                            self.last_agent_language, resp[:60])
                self.memory.add_assistant(resp, language=self.state.language)
                self._store_last_response(resp)
                yield EngineResult(text=resp, is_shortcut=True, intent="repeat")
                return

        # --- Close-confirmation answer (platform rule, all agents) ---
        # Our previous turn asked "should I end the call here, or did I just
        # mishear that?". A clear yes closes; ANYTHING else drops the gate and
        # falls through, so the turn is answered as normal conversation.
        if self._close_confirm_pending:
            self._close_confirm_pending = False
            self._close_confirm_answered = True
            if self._is_close_confirm_yes(text):
                _cancel_llm()
                logger.info("[HANGUP] close confirmed by caller — closing")
                self.state.is_closing = True
                self.commit_pending_turn()  # closing turn — never rolled back
                # Short platform thank-you, NOT the agent's closing speech —
                # the goodbye (LLM or canned) may already have been spoken on
                # the turn that asked for this confirmation.
                resp = self._close_confirmed_line()
                self.memory.add_assistant(resp, language=self.state.language)
                self._store_last_response(resp)
                self.state.stage = STAGE_CLOSING
                self.hangup_pending = True
                yield EngineResult(text=resp, is_shortcut=True, intent="farewell")
                return
            logger.info("[HANGUP] close-confirm answered %r — not a goodbye, "
                        "conversation continues", text[:40])

        # --- Mid-call "hello?" — a presence check, never an answer (§63) ---
        # Outside the connect window a bare hello went to the model, which
        # read it as "move on" and asked the NEXT question of its script —
        # live 2026-09-28 the caller said "hello" twice and heard two new
        # questions (call 919426600929). A hello answers nothing: say we are
        # here and repeat what we asked; a second one in a row means they
        # cannot hear us or someone new is on the line — say who we are.
        _presence = self._presence_check_reply(text)
        if _presence:
            _cancel_llm()
            self._refund_turn_ask()
            self.memory.add_assistant(_presence, language=self.state.language)
            self._store_last_response(_presence)
            yield EngineResult(text=_presence, is_shortcut=True, intent="repeat")
            return

        # --- A bare filler on a scripted stage: the question again, in the
        # owner's words, never the model's own answer to it (RUNBOOK §78) ---
        _reask = self._scripted_filler_reask(text)
        if _reask:
            _cancel_llm()
            self.memory.add_assistant(_reask, language=self.state.language)
            self._store_last_response(_reask)
            yield EngineResult(text=_reask, is_shortcut=True, intent="repeat")
            return

        # --- Farewell at any stage ---
        # Platform rule (all agents): the close-permission confirm counts as a
        # farewell — our previous turn asked "anything else I can help with?"
        # and the caller confirmed they're done. The caller's own goodbye and
        # this confirmed wrap-up are the ONLY paths that arm hangup_pending
        # from normal conversation: the agent never cuts a call the caller
        # hasn't agreed to end.
        # The word check backs up the classifier: "okay bye" classifies as
        # affirmation (the leading okay wins), which used to skip this branch
        # entirely and hand the goodbye to the LLM.
        # --- "No, nothing else" inside a stage that ASKED for questions
        # (RUNBOOK §75): the owner's flow moves on — to the invitation —
        # instead of closing the call. Only a stage that names a
        # `when_intent: no_more_questions` branch does this; everywhere
        # else a done-reply to "anything else?" closes exactly as before. ---
        _nmq_goto = self._no_more_questions_goto(text)
        if (intent == "farewell" or self._contains_farewell(text)
                or (self._is_close_confirmation(text) and not _nmq_goto)):
            _cancel_llm()
            # v3 last-chance commitment checkpoint: if the caller is leaving
            # with a vague promise on the table ("ok bye, call me next week"),
            # ask the ONE clarification instead of the closing line — once.
            _cc_ask = self._commitment_checkpoint(text)
            if _cc_ask is not None:
                self.memory.add_assistant(_cc_ask, language=self.state.language)
                self._store_last_response(_cc_ask)
                yield EngineResult(text=_cc_ask, is_shortcut=True, intent="objection")
                return
            # Confirm before ending: a goodbye we may have misheard costs one
            # question to check and a whole call to get wrong.
            _confirm = self._maybe_close_confirm("caller farewell")
            if _confirm is not None:
                self.memory.add_assistant(_confirm, language=self.state.language)
                self._store_last_response(_confirm)
                yield EngineResult(text=_confirm, is_shortcut=True, intent="farewell")
                return
            self.state.is_closing = True
            # Closing turns are final: the bridge may hang up right after the
            # farewell line, so never leave this turn open to rollback.
            self.commit_pending_turn()
            # D9: response_bank.closing_responses is the real farewell line
            # when present ({} = today's localized/hardcoded chain).
            _close_lang = self._closing_language()
            _closing = self._lang_response(
                self._def.response_bank.closing_responses, lang=_close_lang)
            resp = _closing or self._localized(
                "closing", "Thank you for your time! We'll be in touch. Have a great day!",
                lang=_close_lang)
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            self.state.stage = STAGE_CLOSING
            self.hangup_pending = True
            yield EngineResult(text=resp, is_shortcut=True, intent="farewell")
            return

        # --- Wrong number / "that's not me" on an outbound call (RUNBOOK
        # §73). Checked BEFORE objections: "wrong number" is also a default
        # objection pattern and must not burn a decline strike instead.
        # The first signal is RE-CONFIRMED once ("is this the wrong number,
        # or the right one?"); the answer to that closes the call with an
        # apology, resumes the script, or holds the call to a callback for
        # the real contact — and nothing in between reaches the pitch. ---
        if self.call_direction == "outbound":
            _wn_hit = self._wrong_number_pattern_hit(text)
            if self._wrong_number_pending:
                _verdict = self._reconfirm_verdict(text)
                logger.info("[IDENTITY] re-confirmation answer %r → %s (ask %d)",
                            text[:60], _verdict, self._wrong_number_pending)
                if _verdict == "unclear" and intent == "identity_question":
                    # "who is this?" — say who we are, then ask again. The
                    # identity answer hands the turn back to our last
                    # question, which IS the re-confirmation line.
                    _cancel_llm()
                    resp = self._identity_answer(text)
                    self.memory.add_assistant(resp, language=self.state.language)
                    self._store_last_response(resp)
                    yield EngineResult(text=resp, is_shortcut=True,
                                       intent="identity_question")
                    return
                if _verdict == "wrong" or (
                        _verdict == "unclear" and self._wrong_number_pending >= 2):
                    _cancel_llm()
                    self._wrong_number_pending = 0
                    self.state.is_closing = True
                    self.commit_pending_turn()  # closing turn — never rolled back
                    self.state.stage = STAGE_CLOSING
                    self.call_outcome = self.call_outcome or "wrong_number"
                    self.state_mgr.extra_flags["wrong_number"] = True
                    self.state_mgr.extra_flags.pop("wrong_number_suspected", None)
                    self.hangup_pending = True
                    resp = self._wrong_number_close_line()
                    logger.info("[IDENTITY] wrong number confirmed — closing")
                    self.memory.add_assistant(resp, language=self.state.language)
                    self._store_last_response(resp)
                    yield EngineResult(text=resp, is_shortcut=True, intent="farewell")
                    return
                if _verdict == "me":
                    # It is them after all: resume exactly where a plain yes
                    # to the identity question would have — the authored
                    # good-time line, no model turn.
                    self._wrong_number_pending = 0
                    self.state_mgr.extra_flags.pop("wrong_number_suspected", None)
                    logger.info("[IDENTITY] confirmed after re-check — resuming")
                    _gt = "" if self._good_time_spoken else self._good_time_line()
                    if _gt:
                        _cancel_llm()
                        self._good_time_spoken = True
                        self.commit_pending_turn()  # §77: never rolled back
                        self.memory.add_assistant(_gt, language=self.state.language)
                        self._store_last_response(_gt)
                        yield EngineResult(text=_gt, is_shortcut=True,
                                           intent="good_time_check")
                        return
                    # No authored line: the model continues, un-cancelled.
                elif _verdict == "other":
                    # Right number, someone else answered: the call stays
                    # open ONLY to find when the contact is reachable
                    # (per-turn ━━ IDENTITY NOT CONFIRMED ━━ band).
                    self._wrong_number_pending = 0
                    self._identity_other = True
                    self.state_mgr.extra_flags.pop("wrong_number_suspected", None)
                    self.state_mgr.extra_flags["wrong_person"] = True
                    logger.info("[IDENTITY] right number, different person — "
                                "no pitch; arranging a callback")
                    _wp = ""
                    try:
                        _wp = self._personalize(
                            str(self._lang_response(
                                self._def.outbound.wrong_person_response) or ""),
                            "outbound.wrong_person_response").strip()
                    except Exception:
                        logger.exception("[IDENTITY] wrong-person line failed")
                    if _wp:
                        _cancel_llm()
                        self.memory.add_assistant(_wp, language=self.state.language)
                        self._store_last_response(_wp)
                        yield EngineResult(text=_wp, is_shortcut=True,
                                           intent="wrong_person")
                        return
                    # No authored line: the model continues under the band.
                else:
                    # Unclear once: ask the same question one more time.
                    _cancel_llm()
                    self._wrong_number_pending += 1
                    resp = self._wrong_number_reconfirm_line()
                    self.memory.add_assistant(resp, language=self.state.language)
                    self._store_last_response(resp)
                    yield EngineResult(text=resp, is_shortcut=True,
                                       intent="wrong_number_check")
                    return
            elif _wn_hit or (self._identity_question_just_asked()
                             and _is_identity_no(text)):
                _line = self._wrong_number_reconfirm_line()
                if _line:
                    _cancel_llm()
                    self._wrong_number_pending = 1
                    self.state_mgr.extra_flags["wrong_number_suspected"] = True
                    logger.info("[IDENTITY] %s %r — re-confirming before "
                                "closing", "wrong-number phrase" if _wn_hit
                                else "no to the identity question", text[:60])
                    self.memory.add_assistant(_line, language=self.state.language)
                    self._store_last_response(_line)
                    yield EngineResult(text=_line, is_shortcut=True,
                                       intent="wrong_number_check")
                    return
                # Re-confirmation disabled (WRONG_NUMBER_RECONFIRM=false):
                # the pre-§73 immediate close on a wrong-number phrase.
                if _wn_hit:
                    _cancel_llm()
                    self.state.is_closing = True
                    self.commit_pending_turn()  # closing turn — never rolled back
                    self.state.stage = STAGE_CLOSING
                    self.call_outcome = self.call_outcome or "wrong_number"
                    self.state_mgr.extra_flags["wrong_number"] = True
                    self.hangup_pending = True
                    resp = self._wrong_number_close_line()
                    self.memory.add_assistant(resp, language=self.state.language)
                    self._store_last_response(resp)
                    yield EngineResult(text=resp, is_shortcut=True, intent="farewell")
                    return

        # --- A declined site visit (RUNBOOK §73) — recorded before the
        # objection branch, because "अभी नहीं" / "not now" is also a
        # universal deferral pattern and the canned "I won't take much time,
        # when should I call?" is the wrong reply to "I can't come now". A
        # decline aimed at the visit, with no decline of the CALL in the
        # same breath, is routed to the model under the ━━ VISIT DECLINED ━━
        # band (send the details on WhatsApp, never invite again). ---
        _vd = self._detect_visit_decline(text)
        if _vd:
            self._note_visit_declined(_vd, text)
            if (intent == "objection"
                    and not _CALL_LEVEL_DECLINE_RE.search(text)
                    and not self.state_mgr.extra_flags.get("dnd_requested")):
                logger.info("[VISIT] decline is about the visit, not the "
                            "call — not an objection strike")
                intent = "general"
                self.state.last_intent = intent
                self.state.decline_count = 0

        # --- A visit DAY/TIME negotiation is not an objection (RUNBOOK §79,
        # call 64629614): "कल की मत करो, उसके बाद का कुछ करो" and "कल का मत
        # करो, कोई और date fix कर लो" matched the objection word "मत करो"
        # twice, the second strike closed the call with the escalation
        # handoff line, and the caller was hung up on mid-sentence. ---
        if intent == "objection" and self._is_visit_negotiation(text):
            logger.info("[VISIT] negotiating the day/time, not declining — "
                        "not an objection strike: %r", text[:60])
            self._visit_negotiation_turn = self.state.turn_count
            intent = "general"
            self.state.last_intent = intent

        # --- Objection at any stage (except GREETING, which has its own handler) ---
        # --- v3 commitment clarification (flag-gated; runs on ANY intent so
        # "call me next week" — classified time_preference, not objection —
        # is captured too). Resolution of a pending clarification answer wins
        # over everything below; fresh detection asks at most one question
        # per call and otherwise records + acknowledges like the callback
        # policy always did.
        if self._clarify_cfg().get("enabled"):
            _cc_resp = None
            if self._pending_clarify is not None:
                _cc_resp = self._resolve_clarification(text)
            elif self._detect_callback_request(text):
                _cc_resp = self._maybe_ask_clarification(text)
            if _cc_resp is not None:
                _cancel_llm()
                self.memory.add_assistant(_cc_resp, language=self.state.language)
                self._store_last_response(_cc_resp)
                yield EngineResult(text=_cc_resp, is_shortcut=True, intent="objection")
                return

        # --- v2 objection-playbook FAQ shortcut, independent of intent ------
        # A playbook entry like "what do you do" / "are you an AI" / "how did
        # you get my number" is a neutral QUESTION, not a decline — detect_intent
        # correctly classifies it as "question"/"identity_question"/"general",
        # never "objection". _pick_objection_response() (below) only runs
        # inside the intent=="objection" branch, so these carefully scripted
        # answers could never fire deterministically UNLESS the caller's own
        # phrasing happened to also read as a decline — on a real Jai Mistry
        # call (2026-09-08, RUNBOOK §36) that meant "tell me about your
        # agency" got a fully improvised pitch instead of the owner's exact
        # line, despite it being the intended per-agent trigger. Checked ONLY
        # when intent != "objection" so a genuine decline (NOT_INTERESTED,
        # AGENCY_NO_PROBLEM, …) still goes through the branch below unchanged
        # — decline counting, DND and callback policy keep exactly their
        # current behavior; this only fills the gap for everything else.
        if intent != "objection" and self.state.stage != STAGE_GREETING:
            _faq_hit = self._match_objection_playbook(text)
            if _faq_hit is not None:
                _cancel_llm()
                self.memory.add_assistant(_faq_hit, language=self.state.language)
                self._store_last_response(_faq_hit)
                yield EngineResult(text=_faq_hit, is_shortcut=True, intent="faq")
                return

        if intent == "objection" and self.state.stage != STAGE_GREETING:
            _cancel_llm()
            self.state.objection_handled = True

            # v2 callback policy (D10): "call me tomorrow"-style deferrals
            # capture a callback time WITHOUT booking a calendar slot.
            cb_policy = self._def.call_flow.callback_policy or {}
            if cb_policy.get("enabled") and self._detect_callback_request(text):
                cb_resp = self._handle_callback_request(text, cb_policy)
                if cb_resp is not None:
                    if cb_policy.get("counts_as_decline", True):
                        self.state.decline_count += 1
                    self.memory.add_assistant(cb_resp, language=self.state.language)
                    self._store_last_response(cb_resp)
                    yield EngineResult(text=cb_resp, is_shortcut=True, intent="objection")
                    return

            self.state.decline_count += 1

            # v2 DND (D10): immediate polite end when the utterance matches a
            # configured dnd_phrase (default [] = no behaviour change).
            _dnd = [str(p).lower() for p in
                    ((self._def.guardrails.compliance or {}).get("dnd_phrases") or [])]
            _is_dnd = any(p and p in text.lower() for p in _dnd)
            # The per-agent dnd_phrases list above defaults to [] on EVERY
            # agent, so before this line the configured close could never
            # fire. Meanwhile _detect_dnd_request() (platform-wide, matches
            # "stop calling", "remove my number", hi/gu forms) only recorded a
            # CRM flag. The two were never connected, so on 2026-08-04 a
            # caller who said "stop calling me" was answered with the
            # objection playbook — "it's always worth a quick comparison" —
            # instead of being let go. An explicit opt-out now takes the same
            # graceful close as a configured dnd_phrase.
            _is_dnd = _is_dnd or bool(
                self.state_mgr.extra_flags.get("dnd_requested"))

            # Nth decline (per call_flow.decline_threshold; all live agents
            # carry 2 — wiring changes nothing): stop pushing, end gracefully.
            _threshold = self._def.call_flow.decline_threshold or 2
            if _is_dnd or self.state.decline_count >= _threshold:
                self.state.is_closing = True
                self.commit_pending_turn()  # closing turn — never rolled back
                self.state.stage = STAGE_CLOSING
                # Escalation handoff replaces the farewell only when enabled.
                esc = self._def.guardrails.escalation or {}
                resp = ""
                _close_lang = self._closing_language()
                if esc.get("enabled"):
                    resp = self._lang_response(esc.get("handoff_message") or {},
                                               lang=_close_lang)
                if not resp:
                    resp = self._lang_response(
                        self._def.response_bank.decline_farewell,
                        lang=_close_lang) \
                        or "Thank you for your time. Have a great day!"
                if _is_dnd:
                    self.state_mgr.extra_flags["dnd_requested"] = True
                    self.call_outcome = self.call_outcome or "dnd"
                    logger.info("[DND] caller requested do-not-disturb — ending call")
                self.hangup_pending = True
                self.memory.add_assistant(resp, language=self.state.language)
                self._store_last_response(resp)
                yield EngineResult(text=resp, is_shortcut=True, intent="farewell")
                return

            resp = self._pick_objection_response(text)
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            yield EngineResult(text=resp, is_shortcut=True, intent="objection")
            return

        # --- v2 canned phrases (deterministic, gated on configuration) ---
        for _cp in (self._def.response_bank.canned_phrases or []):
            _pats = [str(p).lower() for p in (_cp.get("patterns") or [])]
            if _pats and any(p and p in text.lower() for p in _pats):
                resp = self._lang_response(_cp.get("responses") or {})
                if resp:
                    _cancel_llm()
                    self.memory.add_assistant(resp, language=self.state.language)
                    self._store_last_response(resp)
                    yield EngineResult(text=resp, is_shortcut=True, intent="canned")
                    return

        # --- v2 greeting shortcut (opt-in: enable_greeting_shortcut) ---
        # Default false = today's behaviour (Pramukh's localized hi/gu
        # "hello" turns keep going through the LLM).
        if intent == "greeting" and self._def.response_bank.enable_greeting_shortcut \
                and self.state.stage != STAGE_GREETING:
            _greet = self._localized("greeting", "")
            if _greet:
                _cancel_llm()
                self.memory.add_assistant(_greet, language=self.state.language)
                self._store_last_response(_greet)
                yield EngineResult(text=_greet, is_shortcut=True, intent="greeting")
                return

        # --- Gratitude at any stage ---
        # Only a turn that is NOTHING but thanks. "thank you, 2BHK works" and
        # a screener's "thanks, please…" carry more than gratitude, and a
        # canned "You're welcome!" drops the rest on the floor.
        if intent == "gratitude" and self._is_gratitude_only(text):
            _cancel_llm()
            # In the call's language: the English pair was the only built-in,
            # so a Hindi-only agent closed call 1064 with "You're welcome!"
            # (RUNBOOK §73). response_bank.localized_responses still wins.
            resp = self._localized_list(
                "gratitude",
                _GRATITUDE_DEFAULTS.get((self.state.language or "en")[:2],
                                        _GRATITUDE_DEFAULTS["en"]))
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            yield EngineResult(text=resp, is_shortcut=True, intent="gratitude")
            return

        # --- Identity question at any stage — fast deterministic response ---
        if intent == "identity_question":
            _cancel_llm()
            lang = self.state.language
            # Chain + generated fallback: copilot-built agents ship {} here —
            # the old ["en"] access crashed "who is calling?" on those agents.
            resp = (self._lang_response(self._def.response_bank.identity_responses)
                    or self._identity_answer(text))
            self.memory.add_assistant(resp, language=lang)
            self._store_last_response(resp)
            if self.state.stage == STAGE_GREETING:
                self.state.stage = self._post_greeting_stage()
            yield EngineResult(text=resp, is_shortcut=True, intent="identity_question")
            return

        # --- Language switch at any stage ---
        if intent == "language_switch":
            old_lang = self.state.language
            new_lang = self._extract_requested_language(text)
            # An explicit request still cannot take the call outside the
            # languages this agent was built for — it has no greeting, no
            # canned responses and no TTS voice there. Say so instead of
            # switching into a language the agent cannot actually speak.
            if new_lang not in self._switchable_languages():
                logger.info("[LANG] Explicit switch to %s refused — agent "
                            "speaks %s", new_lang,
                            sorted(self._switchable_languages()))
                _unsupported = self._lang_response(
                    self._def.response_bank.lang_unsupported_responses)
                if _unsupported:
                    _cancel_llm()
                    self.memory.add_assistant(_unsupported,
                                              language=self.state.language)
                    yield EngineResult(text=_unsupported, is_shortcut=True,
                                       intent="language_switch")
                    return
                # No configured line — let the LLM answer in the language it
                # already speaks rather than say nothing.
                new_lang = old_lang
            logger.info(
                "[LANG] Explicit switch request -> %s (was %s, locked=%s)",
                new_lang, old_lang, self.state.language_locked,
            )
            self.state.language = new_lang
            self.state.language_locked = True
            # Start at -7 so the lock persists for 17 turns before decaying.
            # Explicit caller requests should persist but allow natural switching.
            self.state.language_lock_turns = -7
            self.state.language_history = [new_lang] * 5
            logger.info("[LANG-LOCK] explicit_switch lang=%s lock_turns=%d (persists ~17 turns)",
                        new_lang, self.state.language_lock_turns)
            # The canned ack is ONLY for a bare "change language" ask that
            # actually changes the language. When the caller names the language
            # they're already speaking ("हिंदी में बताओ" mid-Hindi-call = "say
            # THAT in Hindi"), or the request carries a real ask ("हिंदी में
            # बताओ लेस कॉल के बारे में"), the ack drops their actual question —
            # a live call answered "जी बिल्कुल, हम हिंदी में बात करते हैं।"
            # three turns straight. Those turns go to the LLM with a one-turn
            # re-deliver directive; the language state above is already set, and
            # the parallel LLM task builds its prompt only after this handler
            # returns, so the new language is what it sees.
            if new_lang == old_lang or self._lang_switch_has_content(text):
                self._arm_lang_redeliver_directive(new_lang)
                logger.info(
                    "[LANG] switch is content-bearing or already-%s — routing "
                    "to LLM for re-delivery instead of canned ack", new_lang)
            else:
                _cancel_llm()
                _ls_bank = self._def.response_bank.lang_switch_responses
                resp = (_ls_bank.get(new_lang) or _ls_bank.get("hi") or _ls_bank.get("en")
                        or next(iter(_ls_bank.values()), "") or "Sure!")
                self.memory.add_assistant(resp, language=new_lang)
                # Deliberately NOT _store_last_response: the ack must never
                # become the repeat/replay target — "repeat that" after a
                # switch should replay the last substantive utterance.
                self.memory.qa_tracker.observe_agent_turn(resp, self.memory.turn_index)
                yield EngineResult(text=resp, is_shortcut=True, intent="language_switch")
                return

        # --- B2/B3: deterministic contact capture (all agents) ---
        # Runs after the explicit language-switch branch (a caller may still
        # switch languages mid-capture) and before normal routing. Yields
        # nothing when the turn is unrelated — normal routing continues.
        if self._capture_slot is not None:
            _capture_handled = False
            async for _cr in self._handle_capture_turn(text, intent,
                                                       _cancel_llm):
                _capture_handled = True
                yield _cr
            if _capture_handled:
                return
            # A silently-buffered fragment also ends the turn (agent stays
            # quiet while the caller finishes spelling).
            if self._capture_buf and self._capture_buf[-1] == text:
                _cancel_llm()
                return

        # =============================================================
        # NON-SHORTCUT ROUTING — extract data, advance stage, let LLM respond
        # =============================================================

        # Extract any structured info as a side effect (non-blocking)
        self._extract_data_from_input(text, intent)

        # Write-through: sync legacy extractor results into the state manager
        # so the CONVERSATION STATE block reflects everything, whichever
        # extractor caught it first.
        try:
            self._sync_state_manager()
        except Exception:
            logger.exception("[STATE] sync failed (continuing)")

        # Name confirmation resolution (feature 4)
        if self._name_pending:
            if intent == "affirmation":
                self.memory.booking.user_name = self._name_pending
                self.memory.context.user_name = self._name_pending
                self._name_confirmed = True
                self.state_mgr.set_externally("name", self._name_pending, source="system")
                logger.info("[NAME] Confirmed name: %r", self._name_pending)
                self._name_pending = None
            elif intent == "negation":
                logger.info("[NAME] Caller rejected name %r — discarding pending", self._name_pending)
                # discard (not clear): also nulls any value that leaked into
                # the slot, so a rejected name can never become ground truth.
                self.state_mgr.discard_pending("name")
                self._name_pending = None
            elif self.state.turn_count - self._name_pending_turn >= 2:
                # Caller neither confirmed nor denied after 2 turns — auto-accept.
                # They likely just said their name and kept talking naturally.
                self.memory.booking.user_name = self._name_pending
                self.memory.context.user_name = self._name_pending
                self.memory.call_state.customer_name = self._name_pending
                self._name_confirmed = True
                self.state_mgr.set_externally("name", self._name_pending, source="system")
                logger.info("[NAME] Auto-accepted name after 2 turns: %r", self._name_pending)
                self._name_pending = None

        # v2 generic confirmation loop for merge_policy: confirm slots (the
        # literal 'name' slot is hard-excluded — its flow lives above).
        _pending_confirm = [p for p in self.state_mgr.detect_missing_slots()["pending"]
                            if p != "name"]
        for _slot in _pending_confirm:
            _since = self._slot_confirm_turn.setdefault(_slot, self.state.turn_count)
            _val = self.state_mgr.state.get(_slot)
            # B3: a contact slot only confirms on a BARE yes — "okay tell
            # me more" opens with an affirmation but is not an email
            # confirmation (the capture branch above handles these turns;
            # this guard covers the fall-through path).
            _is_contact = (getattr(self.state_mgr.slots.get(_slot), "type", "")
                           in ("email", "phone"))
            # C3 (2026-07-31): a contact slot resolves on a bare yes/no ONLY
            # when the agent's last utterance actually read the value back —
            # a "नहीं नहीं" answering "any more questions?" once discarded a
            # good email here.
            _contact_ctx_ok = (not _is_contact
                               or self._capture_awaiting_confirm)
            if (intent == "affirmation" and _val is not None
                    and _contact_ctx_ok
                    and (not _is_contact
                         or _all_tokens_in(text, _AFFIRM_TOKENS)
                         or (len(text.split()) <= 3 and "?" not in text))):
                self.state_mgr.set_externally(_slot, _val, source="system")
                self._slot_confirm_turn.pop(_slot, None)
                logger.info("[CONFIRM] Slot %s confirmed: %r", _slot, _val)
            elif intent == "negation" and _contact_ctx_ok:
                self.state_mgr.discard_pending(_slot)
                self._slot_confirm_turn.pop(_slot, None)
                logger.info("[CONFIRM] Slot %s rejected — discarded", _slot)
            elif (self.state.turn_count - _since >= 2 and _val is not None
                    and getattr(self.state_mgr.slots.get(_slot), "type", "")
                    not in ("email", "phone")):
                # B3: contact slots are NEVER auto-accepted — silently
                # promoting an unconfirmed email is exactly how a wrong
                # value becomes permanent ground truth.
                self.state_mgr.set_externally(_slot, _val, source="system")
                self._slot_confirm_turn.pop(_slot, None)
                logger.info("[CONFIRM] Slot %s auto-accepted after 2 turns: %r", _slot, _val)

        # Advance from GREETING on first user response
        if self.state.stage == STAGE_GREETING:
            self.state.stage = self._post_greeting_stage()
            logger.info("[STAGE] GREETING → %s", self.state.stage)

        # =============================================================
        # SOFT STAGE ADVANCEMENT based on collected data
        # =============================================================
        # v2: declarative branches first (empty = no-op), then advancement.
        _stage_before = self.state.stage
        if not self._maybe_branch(intent):
            self._maybe_advance_stage()
        # Cascade through stages the caller has ALREADY answered. Advancement
        # is one stage per turn, so a caller who volunteers everything in two
        # sentences left the stage goal several stages stale: sim 2026-09-28
        # (Nisha) reached the callback question with the flow still in
        # WHAT_THEY_WANT, never arrived at the close that names the next step,
        # and a branch on an answered slot (a sub-₹5 Cr turnover) would only
        # fire once the lagging flow finally reached its stage. Only a stage
        # whose sole gate is "everything it collects is in" may be skipped —
        # intent / min_turns / appointment gates keep their pacing.
        for _hop in range(len(self._def.call_flow.stages)):
            if self.state.stage == _stage_before or not self._stage_already_answered():
                break
            _stage_before = self.state.stage
            if not self._maybe_branch(intent):
                self._maybe_advance_stage()
            logger.info("[STAGE] cascade %s → %s (already answered)",
                        _stage_before, self.state.stage)

        # --- A stage a branch entered on THIS turn speaks its authored
        # `script.entry_line` verbatim, with no model turn (call 1092: the
        # visit decline jumped to the close stage and the model said only
        # the pre-close line — the "details on WhatsApp" the owner wants
        # first was a secondary clause of the goal and was dropped). ---
        _el = self._stage_entry_line(text, intent, _stage_before)
        if _el:
            _cancel_llm()
            logger.info("[SCRIPT] %s entered — authored entry line, no LLM: "
                        "%r", self.state.stage, _el[:60])
            self.memory.add_assistant(_el, language=self.state.language)
            self._store_last_response(_el)
            yield EngineResult(text=_el, is_shortcut=True, intent="general")
            return

        # --- Outbound: plain "yes, it's me" → the authored good-time beat ---
        _gt = self._good_time_fastpath_line(text, intent)
        if _gt:
            _cancel_llm()
            self._good_time_spoken = True
            # RUNBOOK §77: a beat the caller is already hearing is never
            # rolled back. A fragment that landed while this line played
            # ("ठीक है?", call 1061) superseded the turn — the line left
            # history, the flag reset with it, and the model re-introduced
            # itself from the stage goal. Committed, the fragment is simply
            # the next turn, exactly as closing turns already do.
            self.commit_pending_turn()
            logger.info("[CONNECT] Identity confirmed — authored good-time "
                        "line, no LLM: %r", _gt[:60])
            self.memory.add_assistant(_gt, language=self.state.language)
            self._store_last_response(_gt)
            yield EngineResult(text=_gt, is_shortcut=True,
                               intent="good_time_check")
            return

        # =============================================================
        # LLM handles all non-shortcut turns with full inbound script context
        # The system prompt guides the LLM through the 9-stage flow naturally.
        # =============================================================
        self.state.llm_cancelled = False
        async for result in self._consume_llm_with_filler(
            llm_queue, llm_done, intent, nudge_back=False, user_text=text,
        ):
            yield result


    # ------------------------------------------------------------------
    # Inbound flow helpers
    # ------------------------------------------------------------------

    def _extract_data_from_input(self, text: str, intent: str) -> None:
        """Extract structured data as a side effect — never blocks the flow."""
        lower = text.lower()
        cs = self.memory.call_state

        if "location" not in cs.domain_fields:
            loc = self._extract_location(text)
            if loc:
                cs.domain_fields["location"] = loc
                self.memory.qa_tracker.mark_extracted("location", loc)
                logger.info("[DISCOVERY] Location extracted: %s", loc)

        if "property_type" not in cs.domain_fields:
            bhk = self._extract_bhk(text)
            if bhk:
                cs.domain_fields["property_type"] = bhk.upper()
                self.memory.qa_tracker.mark_extracted("bhk", bhk.upper())
                logger.info("[DISCOVERY] Config extracted: %s", bhk)

        if not self.memory.booking.appointment_time and self._books_appointments():
            # v3: while a commitment clarification is in flight, day/time words
            # refine the commitment — they must never book a calendar slot.
            if self._suppress_time_match_turns > 0:
                logger.info("[COMMITMENT] time-slot matching suppressed this turn")
            else:
                t = self._extract_time_preference(text, stage=self.state.stage)
                if t:
                    self.memory.booking.appointment_time = t
                    self._record_calendar_booking(t)
                    logger.info("[DISCOVERY] Time extracted: %s", t)

        if not self.state.purpose and "purpose" not in cs.domain_fields:
            # Multi-word / script phrases: safe to use substring matching (rare false positives)
            invest_kw_exact = [
                "rental income", "rental return", "return on investment",
                "kiraya", "kiraye",
                "रिटर्न", "निवेश", "ભાડું", "રોકાણ",
            ]
            self_use_kw_exact = [
                "self use", "self-use", "own use",
                "apne liye", "rehna",
                "ઘર", "રહેવા", "ખુદ", "ફેમિલી",
            ]
            # Single English words that need word-boundary matching to avoid
            # false positives ("returns my call", "staying on topic", etc.)
            _invest_bound_re = re.compile(
                r'\b(?:investment|investing|investor|invest|appreciation|'
                r'rental income|rental return|profit|yield)\b'
                r'|\breturn on investment\b',
                re.IGNORECASE
            )
            _self_use_bound_re = re.compile(
                r'\b(?:family|families|stay|staying|live|living|reside|residing|'
                r'ghar|makan|rahega|settle|settling)\b', re.IGNORECASE
            )
            if (any(kw in lower or kw in text for kw in invest_kw_exact)
                    or _invest_bound_re.search(lower)):
                self.state.purpose = "investment"
                self.memory.qa_tracker.mark_extracted("purpose", "investment")
            elif (any(kw in lower or kw in text for kw in self_use_kw_exact)
                    or _self_use_bound_re.search(lower)):
                self.state.purpose = "self_use"
                self.memory.qa_tracker.mark_extracted("purpose", "self_use")

        # ── Budget extraction ─────────────────────────────────────────────
        if "budget" not in cs.domain_fields:
            budget = self._extract_budget(text)
            if budget:
                cs.domain_fields["budget"] = budget
                self.memory.qa_tracker.mark_extracted("budget", budget)
                logger.info("[DISCOVERY] Budget extracted: %s", budget)

        # ── Timeline extraction ───────────────────────────────────────────
        if "timeline" not in cs.domain_fields:
            timeline = self._extract_timeline(text)
            if timeline:
                cs.domain_fields["timeline"] = timeline
                self.memory.qa_tracker.mark_extracted("timeline", timeline)
                logger.info("[DISCOVERY] Timeline extracted: %s", timeline)

        # ── Entity detection (persistent context lock) ───────────────────
        # Match against known_projects/entities in the agent definition.
        # First match is locked for the session (stored in domain_fields).
        if "matched_entity" not in cs.domain_fields:
            entity_result = self._extract_project_from_text(text)
            if entity_result:
                entity_name, entity_loc = entity_result
                cs.domain_fields["matched_entity"] = entity_name
                if entity_loc:
                    cs.domain_fields["entity_location"] = entity_loc
                self.state.project_recommended = entity_name
                logger.info(
                    "[STATE_UPDATE] matched_entity=%r location=%r",
                    entity_name, entity_loc,
                )

        # ── Sync all layers into CallState ───────────────────────────────
        self._update_call_state()
        # A bare early "hello?" is dead-air repair, not conversation — it
        # must not burn the stage budget (four connect hellos once pushed a
        # call past identity into BUSINESS_DISCOVERY via max_turns).
        if not self._is_early_hello(text):
            self.state.discovery_turns += 1

    def _post_greeting_stage(self) -> str:
        """Stage to enter after the greeting turn. Legacy flows land on
        DISCOVERY; custom-id flows (builder/copilot agents whose stages don't
        include DISCOVERY) start at their OWN first stage. Without this, the
        unknown 'DISCOVERY' id fell through _maybe_advance_stage's index-0
        fallback with the turn budget already spent, so the first custom
        stage's goal never governed a single LLM turn (e.g. an outbound
        CONFIRM_PERSON stage was skipped and the pitch fired on turn 1 even
        when a stranger answered the phone).

        The flow's OWN first stage wins (2026-09-08, RUNBOOK §37). The
        previous rule — "a stage named DISCOVERY exists ANYWHERE ⇒ go to
        DISCOVERY" — only asked whether the id appeared somewhere in the list,
        so a flow that opens with an identity stage and merely happens to call
        its SECOND stage DISCOVERY jumped straight past stage 0. Live:
        jai_mistry-outbound's INTRO_QUALIFY (confirm_person, and with it the
        entire wrong-number script the owner dictated) never governed a single
        turn on any call — the agent never introduced itself, and the caller
        had to ask twice who was calling. jethalal and lescall_com had the
        same shape.

        A flow whose FIRST stage is literally named GREETING keeps today's
        behaviour EXACTLY, quirks included — DISCOVERY when the flow has such
        a stage (pramukh), the GREETING stage itself when it does not
        (fitcoach, kaapd_pooja, serene_health, whose stage-0 goals are written
        for the beat right after the caller answers). Nothing in this incident
        is evidence about that shape, so this fix does not touch it. The only
        behaviour that changes is the broken case above: three live agents
        (jai_mistry, jethalal, lescall_com) whose real opening stage was
        being skipped."""
        stages = self._def.call_flow.stages
        if not stages:
            return STAGE_DISCOVERY
        try:
            first = min(stages, key=lambda s: int(getattr(s, "order", 0) or 0))
        except Exception:
            first = stages[0]
        if first.id == STAGE_GREETING:
            return (STAGE_DISCOVERY
                    if any(s.id == STAGE_DISCOVERY for s in stages)
                    else first.id)
        return first.id

    def _completed_when_met(self) -> bool:
        """call_flow.completed_when — is the call's completion condition met?"""
        cw = self._def.call_flow.completed_when or {}
        required = [str(x) for x in (cw.get("required_slots") or [])]
        if required and not self.state_mgr.collect_satisfied(required):
            return False
        if cw.get("require_appointment") and not self.memory.booking.appointment_time:
            return False
        return True

    # Recognized AND-side condition keys of advance_when. max_turns is NOT
    # one of them — it is an OR-escape evaluated in _maybe_advance_stage, so
    # {intent: affirmation, max_turns: 3} advances on the affirmation OR on
    # turn 3, and a bare {max_turns: 3} waits for turn 3 (never turn 1 via
    # the vacuous-True path).
    _AW_AND_KEYS = ("min_turns", "all_collect_filled", "slots", "intent",
                    "appointment_captured")

    def _advance_when_met(self, stage_def, aw: dict) -> bool:
        """Declarative advance_when gate (all present conditions AND-ed)."""
        if aw.get("min_turns"):
            try:
                if self.state.discovery_turns < int(aw["min_turns"]):
                    return False
            except (TypeError, ValueError):
                pass
        if aw.get("all_collect_filled"):
            if not self.state_mgr.collect_satisfied(
                    list(getattr(stage_def, "collect", None) or [])):
                return False
        if aw.get("slots"):
            if not self.state_mgr.collect_satisfied([str(s) for s in aw["slots"]]):
                return False
        if aw.get("intent"):
            if self.state.last_intent != str(aw["intent"]):
                return False
        if aw.get("appointment_captured"):
            if not self.memory.booking.appointment_time:
                return False
        return True

    def _exec_branch_action(self, action: str) -> None:
        """Deterministic side effects for branch actions (plan §4.3 item 4).
        Promises are STORED (→ calls.collected_data), not just spoken."""
        sm = self.state_mgr
        if action == "capture_phone":
            phone_slot = next(
                (n for n, s in sm.slots.items() if getattr(s, "type", "") == "phone"),
                "phone" if "phone" in sm.slots else None)
            if phone_slot:
                sm.require_now(phone_slot)
            logger.info("[BRANCH-ACTION] capture_phone (slot=%s)", phone_slot)
        elif action == "promise_whatsapp_media":
            self._whatsapp_promised = True
            sm.extra_flags["whatsapp_promised"] = True
            wp = self._def.follow_up.whatsapp_promise or {}
            if wp.get("assets"):
                sm.extra_flags["whatsapp_assets"] = list(wp["assets"])
            logger.info("[BRANCH-ACTION] promise_whatsapp_media recorded")
        elif action == "book_visit":
            # The goto stage carries the scheduling role; its machinery
            # (calendar block, bare-affirmation slot matching) takes over.
            logger.info("[BRANCH-ACTION] book_visit — scheduling machinery engaged")
        elif action == "schedule_callback":
            self._callback_requested = "requested"
            sm.extra_flags["callback_requested"] = True
            slot_name = str((self._def.call_flow.callback_policy or {})
                            .get("capture_slot") or "callback_time")
            if slot_name in sm.slots:
                sm.require_now(slot_name)
            logger.info("[BRANCH-ACTION] schedule_callback (slot=%s)", slot_name)
        elif action.startswith("retire_slots:"):
            # `retire_slots:a,b,c` — the branch's VALUE decided these slots
            # do not apply to this caller (RUNBOOK §56). Runs before the
            # stage moves, so the reason names the deciding stage.
            names = [x.strip() for x in action.split(":", 1)[1].split(",")
                     if x.strip()]
            done = sm.retire_slots(names, reason=f"branch in {self.state.stage}")
            logger.info("[BRANCH-ACTION] retire_slots %s → retired %s", names, done)
        else:
            logger.warning("[BRANCH-ACTION] unknown action %r ignored", action)

    def _maybe_branch(self, intent: str) -> bool:
        """v2 declarative branching — evaluated just before turn-count
        advancement. Empty branch lists = no-op (today's behaviour)."""
        sd = self._stage_def()
        branches = (getattr(sd, "branches", None) or []) if sd else []
        for i, br in enumerate(branches):
            if not isinstance(br, dict):
                continue
            matched = False
            if br.get("when_intent"):
                matched = intent == str(br["when_intent"])
            elif isinstance(br.get("when_slot"), dict):
                ws = br["when_slot"]
                actual = self.state_mgr.state.get(str(ws.get("name") or ""))
                if "equals" in ws:
                    matched = (actual is not None
                               and str(actual).strip().lower()
                               == str(ws.get("equals") or "").strip().lower())
                else:
                    # No `equals` — a pure "has this slot been given ANY
                    # answer" gate. This is what a free-text reason needs:
                    # there is no fixed vocabulary to match a value against,
                    # only "the caller has now answered" as the trigger to
                    # converge on wherever the flow goes next (RUNBOOK §35).
                    matched = actual is not None and str(actual).strip() != ""
            if not matched:
                continue
            goto = str(br.get("goto") or "")
            _actions = [str(a) for a in (br.get("actions") or []) if str(a)]
            if goto and self._stage_def(goto) is None:
                logger.warning("[STAGE] branch goto %r invalid — skipped", goto)
                continue
            if not goto and not _actions:
                logger.warning("[STAGE] branch %d in %s has neither goto nor "
                               "actions — skipped", i, self.state.stage)
                continue
            key = (self.state.stage, i)
            try:
                max_takes = max(1, int(br.get("max_takes", 1) or 1))
            except (TypeError, ValueError):
                max_takes = 1
            if self._branch_takes.get(key, 0) >= max_takes:
                continue
            self._branch_takes[key] = self._branch_takes.get(key, 0) + 1
            for action in _actions:
                try:
                    self._exec_branch_action(action)
                except Exception:
                    logger.exception("[BRANCH-ACTION] %r failed (continuing)", action)
            if not goto:
                # An ACTION-ONLY branch (RUNBOOK §73): the value decides
                # something about this caller — "an investor is never asked
                # who will live there" (retire_slots:family_size) — without
                # moving the flow. The stage keeps its own pacing; the next
                # branch may still fire.
                logger.info("[STAGE] branch %d in %s ran %s (no goto)",
                            i, self.state.stage, _actions)
                continue
            self.state.stage = goto
            self.state.discovery_turns = 0
            logger.info("[STAGE] → %s (branch)", goto)
            return True
        return False

    # Function words of a Hindi / Hinglish / English line — two unrelated
    # questions share "क्या मैं आपकी … मदद कर सकता हूँ", so only CONTENT words
    # decide whether a reply carries an authored line (§75).
    _LINE_STOP = frozenset({
        "क्या", "मैं", "आप", "आपका", "आपकी", "आपके", "आपको", "में", "के", "की",
        "का", "को", "से", "पर", "है", "हैं", "हूँ", "हूं", "हो", "था", "थी",
        "कर", "करें", "सकता", "सकती", "सकते", "और", "या", "तो", "भी", "यह",
        "ये", "इस", "उस", "वो", "जी", "कोई", "कुछ", "पास", "लिए", "रहे", "रही",
        "रहा", "रहेगा", "रहेगी", "ही", "ना", "नहीं", "हाँ", "sir", "ma'am",
        "the", "you", "your", "are", "for", "and", "with", "that", "this",
        "can", "any", "else", "about", "have", "there", "which", "what",
        "would", "like", "please", "okay", "yes",
    })

    @classmethod
    def _line_tokens(cls, s: str) -> set[str]:
        out = set()
        for w in str(s or "").split():
            w = w.strip("?,.!।॥\"'()<>-—:;").lower()
            if len(w) >= 3 and w not in cls._LINE_STOP:
                out.add(w)
        return out

    def _line_asks(self, said: str, line: str, slot: str = "") -> bool:
        """Does the reply already carry `line` — word for word or as a
        paraphrase? Half of the line's words is a paraphrase (RUNBOOK §75:
        "आप specifically 3 BHK देख रहे हैं या 4 BHK?" for the authored
        "…consider कर रहे हैं या 4 BHK?"); for a slot, one of its own
        question signals inside a question also counts."""
        want = self._line_tokens(line)
        if not want:
            return False
        have = self._line_tokens(said)
        if len(want & have) / len(want) >= 0.5:
            return True
        if slot and "?" in (said or ""):
            low = (said or "").lower()
            sigs = ((self._derive_qa_field_signals() or {}).get(slot) or [])
            if any(len(s) >= 6 and s in low for s in sigs):
                return True
        return False

    def _script_followup(self, said: str) -> str:
        """The scripted stage's floor (RUNBOOK §75): the text to speak after
        the model's reply — '' on every stage without a `script` block."""
        sd = self._stage_def()
        script = getattr(sd, "script", None) if sd else None
        if not isinstance(script, dict) or not script:
            return ""
        sm = self.state_mgr
        lang = self.state.language
        extra: list[str] = []
        if script.get("ask_verbatim"):
            noted = sm.last_noted_ask
            spec = sm.slots.get(noted) if noted else None
            max_asks = int(getattr(spec, "max_asks", 0) or 0) if spec else 0
            if (spec is not None
                    and sm._asked_turn.get(noted) == sm.turn_index
                    and sm.state.get(noted) is None
                    # The model asked something else ("क्या आप दूसरे projects
                    # देखना चाहेंगे?") — stacking the slot question behind it
                    # asked two things at once; the directive keeps the slot
                    # for the next turn.
                    and not (said or "").rstrip().endswith("?")
                    # The floor spends the slot's own ask budget, never more.
                    and (max_asks <= 0
                         or sm.asked_count.get(noted, 0) <= max_asks)):
                q = self._lang_response(
                    dict(getattr(sm.slots[noted], "question", None) or {}),
                    lang=lang)
                # A second ask speaks the slot's `reask_question` when it has
                # one (§81: "sorry, didn't catch that — location, size…?"),
                # and a reply carrying EITHER line already asked it.
                rq = self._lang_response(
                    dict(getattr(sm.slots[noted], "reask_question", None)
                         or {}), lang=lang)
                if rq and sm.asked_count.get(noted, 0) >= 2:
                    q, rq = rq, q
                if (q and not self._line_asks(said, q, slot=noted)
                        and not (rq and self._line_asks(said, rq))):
                    extra.append(q)
                    logger.info("[SCRIPT] %s: the reply did not ask %s — its "
                                "question is spoken after it",
                                self.state.stage, noted)
        aa = script.get("after_answer")
        if isinstance(aa, dict) and aa:
            line = self._lang_response(aa, lang=lang)
            unless = [str(u) for u in (script.get("unless") or []) if str(u)]
            if (line and not self._line_asks(said, line)
                    and not self._CLOSE_PERMISSION_RE.search(said or "")
                    # A reply that ends on a question of its own ("आप अपना
                    # नया budget क्या रखना चाहते हैं?") is not an answer —
                    # stacking the doubts question behind it asked two
                    # things at once (call b5452b23, 03:22 and 03:41).
                    and not (said or "").rstrip().endswith("?")
                    and not any(self._line_asks(said, u) for u in unless)
                    and not any(self._line_asks(x, line) for x in extra)):
                extra.append(line)
                logger.info("[SCRIPT] %s: after-answer line spoken",
                            self.state.stage)
        return " ".join(self._personalize(x, "script") for x in extra).strip()

    def _scripted_filler_reask(self, text: str) -> str:
        """A bare "हम्म" / "अच्छा" / "okay" to a scripted question is not an
        answer — on a stage with `script.ask_verbatim`, the question is put
        again in the owner's words, without the model (RUNBOOK §78: on
        "हम्म" the model answered its own factor question — "तो location और
        connectivity आपके लिए सबसे ज़्यादा मायने रखते हैं" — on call
        b5452b23 and again on the revision-10 sim). '' everywhere else."""
        sd = self._stage_def()
        script = getattr(sd, "script", None) if sd else None
        if not isinstance(script, dict) or not script.get("ask_verbatim"):
            return ""
        if self.state.is_closing or not self._is_bare_ack_text(text):
            return ""
        last = (self.last_agent_response or "").rstrip()
        if not last.endswith("?"):
            return ""
        sm = self.state_mgr
        noted = sm.last_noted_ask
        spec = sm.slots.get(noted) if noted else None
        if spec is None or sm.state.get(noted) is not None:
            return ""
        q = self._lang_response(dict(getattr(spec, "question", None) or {}),
                                lang=self.state.language)
        if not q or not self._line_asks(last, q, slot=noted):
            return ""
        # Voiced again: it spends the slot's second ask, so a third never
        # follows (note_asked is idempotent within the turn).
        sm.note_asked(noted)
        logger.info("[SCRIPT] %s: bare filler %r — %s asked again, verbatim",
                    self.state.stage, text[:20], noted)
        return self._personalize(q, "script")

    def _no_more_questions_goto(self, text: str) -> str:
        """RUNBOOK §75: the stage asked "any other question?" and names a
        `when_intent: no_more_questions` branch — the caller's done-reply
        ("नहीं, बस", "no that's all"; the same test as a close permission)
        takes that branch instead of closing the call. Returns the stage
        moved to, or '' (no such branch, not a done-reply, takes spent)."""
        sd = self._stage_def()
        branches = (getattr(sd, "branches", None) or []) if sd else []
        for i, br in enumerate(branches):
            if (not isinstance(br, dict)
                    or str(br.get("when_intent") or "") != "no_more_questions"):
                continue
            goto = str(br.get("goto") or "")
            if not goto or self._stage_def(goto) is None:
                logger.warning("[STAGE] no_more_questions branch %d in %s "
                               "names no stage — skipped", i, self.state.stage)
                continue
            key = (self.state.stage, i)
            try:
                max_takes = max(1, int(br.get("max_takes", 1) or 1))
            except (TypeError, ValueError):
                max_takes = 1
            if self._branch_takes.get(key, 0) >= max_takes:
                continue
            # The stage's question may sit one line back: an OBJECTION
            # HANDLING answer the model closed with its own question ("…cost
            # sheet देखना चाहेंगे?") came between the doubts question and the
            # caller's "नहीं, बस इतना ही" (sim 2026-10-12) — that "no" is
            # still the stage's done-reply, not the call's.
            _recent = [l for l in self.memory.recent_assistant_texts(2) if l]
            if not any(self._is_done_reply(text, l)
                       for l in (_recent or [self.last_agent_response or ""])):
                return ""
            self._branch_takes[key] = self._branch_takes.get(key, 0) + 1
            for action in [str(a) for a in (br.get("actions") or []) if str(a)]:
                try:
                    self._exec_branch_action(action)
                except Exception:
                    logger.exception("[BRANCH-ACTION] %r failed (continuing)",
                                     action)
            self.state.stage = goto
            self.state.discovery_turns = 0
            logger.info("[STAGE] → %s (no more questions: %r)", goto, text[:40])
            return goto
        return ""

    _CASCADE_AW_KEYS = frozenset({"all_collect_filled", "max_turns", "goto"})

    def _stage_already_answered(self) -> bool:
        """True when the CURRENT stage exists only to collect slots and every
        one of them is already in — the stage-advance cascade may pass it.
        Any other gate (intent, min_turns, appointment, explicit slot list)
        is pacing the business asked for and is never skipped."""
        sd = self._stage_def()
        if sd is None or not (getattr(sd, "collect", None) or []):
            return False
        aw = getattr(sd, "advance_when", None) or {}
        if not aw.get("all_collect_filled") or not set(aw) <= self._CASCADE_AW_KEYS:
            return False
        try:
            return bool(self._advance_when_met(sd, aw))
        except Exception:
            logger.exception("[STAGE] cascade check failed (not cascading)")
            return False

    def _maybe_advance_stage(self) -> None:
        """Domain-agnostic stage advancement driven by turn count and booking events."""
        b = self.memory.booking
        cs = self.memory.call_state
        # RUNBOOK §75: a book_appointment tool call writes CallState AFTER
        # the reply, and the mirror into memory.booking happened only at
        # the next prompt build (_update_call_state) — so the stage waiting
        # on the booking advanced one caller turn late and that turn's
        # reply still came from the old goal (the invitation instead of the
        # read-back). Mirror first; the check below then sees the booking.
        if cs.appointment_time and not b.appointment_time:
            b.appointment_time = cs.appointment_time
        if cs.appointment_date and not b.appointment_date:
            b.appointment_date = cs.appointment_date
        stage_list = self._def.call_flow.stages
        current_idx = next((i for i, s in enumerate(stage_list) if s.id == self.state.stage), 0)
        if current_idx >= len(stage_list) - 1:
            return  # already at last stage

        next_stage_id = stage_list[current_idx + 1].id
        last_stage_id = stage_list[-1].id
        current_def = stage_list[current_idx]
        dt = self.state.discovery_turns

        # v2 completed-call gate: advancement INTO the final stage is gated
        # only when completed_when.gate_closing is true (default off = today).
        _cw = self._def.call_flow.completed_when or {}
        _gate_closing = bool(_cw.get("gate_closing"))

        def _blocked_into_closing(target: str) -> bool:
            return _gate_closing and target == last_stage_id and not self._completed_when_met()

        # v2: completion condition met → mark the call complete (gives the
        # previously-dead booking.confirmed path its first intentional writer).
        if _cw and not b.confirmed and (
                _cw.get("required_slots") or _cw.get("require_appointment")) \
                and self._completed_when_met():
            b.confirmed = True
            self.call_outcome = self.call_outcome or "completed"
            logger.info("[STAGE] completed_when met — call marked complete")

        # Booking confirmed → jump to last stage (CLOSING equivalent)
        if b.confirmed and self.state.stage != last_stage_id:
            self.state.stage = last_stage_id
            self.state.is_closing = True
            self.state.discovery_turns = 0
            logger.info("[STAGE] → %s (booking confirmed)", last_stage_id)
            return

        # Appointment time captured → advance once, then stop reacting to this
        # event. getattr guard: appointment_advanced was never initialized in
        # ConversationState — the bare attribute access raised AttributeError
        # the first time an appointment was ever captured (latent prod bug).
        if b.appointment_time and not getattr(self.state, "appointment_advanced", False):
            self.state.appointment_advanced = True
            if not _blocked_into_closing(next_stage_id):
                self.state.stage = next_stage_id
                self.state.discovery_turns = 0
                logger.info("[STAGE] → %s (appointment_time set)", next_stage_id)
                return

        # v2 declarative gate: a non-empty advance_when REPLACES the legacy
        # pacing for this stage (empty = today's exact behaviour).
        aw = getattr(current_def, "advance_when", None) or {}
        if aw:
            try:
                _mx = int(aw.get("max_turns") or 0)
            except (TypeError, ValueError):
                _mx = 0
            # AND-side conditions met, OR the max_turns time-escape reached.
            # Without the escape, a never-satisfied condition strands the
            # stage forever (the Kaapd failure shape); without the
            # _AW_AND_KEYS guard, a bare {max_turns: N} would advance on
            # turn 1 via _advance_when_met's vacuous True.
            _met = (any(k in aw for k in self._AW_AND_KEYS)
                    and self._advance_when_met(current_def, aw))
            # An outbound call's FIRST stage has not spoken yet when the
            # caller's first words arrive — the opening was "हेलो?" and the
            # identity beat is this stage's whole job. A first utterance that
            # happens to be an affirmation ("जी नमस्ते", "हाँ बोलिए") answers
            # the hello, not a question nobody asked; advancing on it skips
            # the stage entirely. Third sighting (calls 791, 934, 971). The
            # connect intro counts as the stage having spoken.
            if (_met and aw.get("intent") and current_idx == 0 and dt == 0
                    and not self._intro_delivered
                    and getattr(self._def, "call_direction", "") == "outbound"):
                logger.info("[STAGE] %s: caller's first words match intent "
                            "%r but the stage has not spoken yet — holding "
                            "one turn", current_def.id, aw.get("intent"))
                _met = False
            # advance_when.goto — where this stage's escape LANDS (§37).
            # Without it, advancement is list-adjacency only, so a stage that
            # exists to catch one early-exit path escapes into whatever
            # happens to sit next in the YAML. Live twice: NO_FOCUS_REASON's
            # when_slot branch to WRAP_UP needs an async slot extractor to
            # land inside the turn, and when it does not (a deterministic
            # objection shortcut answers the turn and the extractor never
            # runs) the max_turns escape dropped the caller into
            # AGENCY_CHALLENGES — a stage with no script for them — and the
            # model improvised an English "anything else I can help with?".
            # Both convergent stages can now name WRAP_UP; adjacency cannot
            # give it to more than one of them.
            _target = str(aw.get("goto") or "").strip()
            if _target and any(s.id == _target for s in stage_list):
                next_stage_id = _target
            elif _target:
                logger.warning("[STAGE] advance_when.goto=%r on %s names no "
                               "stage — falling back to the next one",
                               _target, current_def.id)
            if (_met or (_mx and dt >= _mx)) and \
                    not _blocked_into_closing(next_stage_id):
                self.state.stage = next_stage_id
                self.state.discovery_turns = 0
                logger.info("[STAGE] → %s (advance_when %s)", next_stage_id,
                            "met" if _met else f"max_turns {_mx} reached")
            return

        # Goal-met advance for yes/no confirmation stages — EXPLICIT role
        # only: the derived shim maps legacy OPENING → confirm_person and
        # would silently change existing outbound-skeleton agents. No agent
        # on disk carries an explicit confirm_person role today. Reuses the
        # deterministic [INTENT] classification set earlier this turn; three
        # of the Kaapd call's six turns were affirmations wasted against an
        # already-satisfied confirm goal.
        if (getattr(current_def, "role", "") == "confirm_person"
                and self.state.last_intent == "affirmation"
                and dt >= 1
                and not _blocked_into_closing(next_stage_id)):
            self.state.stage = next_stage_id
            self.state.discovery_turns = 0
            logger.info("[STAGE] → %s (confirm_person affirmation)",
                        next_stage_id)
            return

        # Slot-aware early advancement: when the caller front-loads their
        # requirements ("3BHK in Vapi under 50 lakhs"), don't hold them in
        # DISCOVERY for the scripted turn count — advance as soon as every
        # required discovery slot is filled. Role check (v2 shim), with the
        # legacy id literal kept as fallback.
        if self._stage_role() == "discovery" or self.state.stage == STAGE_DISCOVERY:
            gaps = self.state_mgr.detect_missing_slots()
            _required = sum(1 for s in self.state_mgr.slots.values() if s.required)
            # Guards: pending counts as incomplete, and name-only agents
            # (pitch flows) never early-advance — a captured name is not
            # "discovery complete"; their pacing stays turn-count driven.
            if _required >= 2 and not gaps["missing"] and not gaps.get("pending") \
                    and not gaps.get("blocked"):
                if not _blocked_into_closing(next_stage_id):
                    self.state.stage = next_stage_id
                    self.state.discovery_turns = 0
                    logger.info("[STAGE] → %s (all required slots filled — early advance)",
                                next_stage_id)
                    return

        # Turn-count based advancement
        threshold = current_def.turns_until_advance
        if dt >= threshold and not _blocked_into_closing(next_stage_id):
            self.state.stage = next_stage_id
            self.state.discovery_turns = 0
            logger.info("[STAGE] → %s (turn threshold %d reached)", next_stage_id, threshold)

        logger.debug(
            "[STAGE-STATE] stage=%s turns=%d/%d appt=%s confirmed=%s domain=%s",
            self.state.stage, dt, current_def.turns_until_advance,
            b.appointment_time or "—", b.confirmed, cs.domain_fields,
        )

    # ------------------------------------------------------------------
    # Call-state management helpers
    # ------------------------------------------------------------------

    def _sync_state_manager(self) -> None:
        """Write-through legacy extraction results into the state manager.

        The legacy extractors (_extract_data_from_input, memory name/phone
        capture) write into cs.domain_fields / booking; the state manager is
        the single source of truth for the prompt, so mirror everything in.
        """
        cs = self.memory.call_state
        b = self.memory.booking
        sm = self.state_mgr
        mapping = {
            "location": "location",
            "budget": "budget",
            "timeline": "timeline",
            "purpose": "purpose",
        }
        for src_key, slot in mapping.items():
            val = cs.domain_fields.get(src_key)
            if val is not None:
                sm.merge_state({slot: val}, source="legacy", fill_only=True)
        ptype = cs.domain_fields.get("property_type")
        if ptype:
            # Legacy stores "3 BHK"-style values under property_type.
            target = "bhk" if "bhk" in str(ptype).lower() else "property_type"
            sm.merge_state({target: str(ptype).replace(" ", "").upper()
                            if target == "bhk" else ptype},
                           source="legacy", fill_only=True)
        if b.user_name and self._name_confirmed:
            sm.set_externally("name", b.user_name, source="legacy")
        # v2 phone write-through: CallState.customer_phone and a phone-type
        # slot must never diverge (D5). Sync both directions.
        _phone_slot = next(
            (n for n, s in sm.slots.items() if getattr(s, "type", "") == "phone"),
            None)
        if _phone_slot:
            _slot_val = sm.state.get(_phone_slot)
            if cs.customer_phone and _slot_val is None:
                sm.merge_state({_phone_slot: cs.customer_phone},
                               source="legacy", fill_only=True)
            elif _slot_val and not cs.customer_phone:
                cs.customer_phone = str(_slot_val)
        # Meeting-booking agents: a booked appointment IS the meeting time —
        # write it through so the state block never re-asks after booking.
        if b.appointment_time and "meeting_time" in sm.state:
            when = f"{b.appointment_date or ''} {b.appointment_time}".strip()
            sm.set_externally("meeting_time", when, source="legacy")

    # ── Async LLM slot extraction (gap-filler; never on the hot path) ───

    def _execute_tool_proposals(self, had_text: bool) -> list[str]:
        """Execute ("on" mode) the tool proposals accumulated by
        _call_llm_stream, through the deterministic gates the plan requires:
        fill-only slot merges, stage-legality checks, the turn-transaction
        deferral already inside _record_calendar_booking, and the existing
        llm_end_call gate. Returns spoken confirmation lines for TOOL-ONLY
        turns; when the model also produced text, its own words are the
        confirmation and nothing extra is spoken.
        """
        proposals, self._pending_tool_proposals = (
            self._pending_tool_proposals, [])
        if not proposals:
            return []
        if self._tools_mode != "on":
            for p in proposals:
                logger.info("[TOOLS] dropped proposal outside 'on' mode: "
                            "%s %r", p.name, p.args)
            return []
        from .llm_tools import clean_slot_args, confirm_line
        lang = (self.state.language or "en")[:2]
        lines: list[str] = []
        _refused: str | None = None   # why a book_appointment was refused
        for p in proposals:
            if not p.valid:
                continue  # malformed args — already logged loudly
            try:
                if p.name == "record_customer_details":
                    data = _drop_non_values(clean_slot_args(p.args), "tool")
                    data = self._drop_yes_no_from_question_turn(
                        data, self._last_caller_text(), "tool")
                    changes = (self.state_mgr.merge_state(
                        data, source="tool", fill_only=True) if data else [])
                    logger.info("[TOOLS] record_customer_details %r → "
                                "%d filled", data, len(changes))
                    if changes and not had_text:
                        lines.append(confirm_line(p.name, lang, {}))

                elif p.name == "book_appointment":
                    date = _roll_forward_wrong_year(
                        str(p.args.get("date") or "").strip())
                    tm = str(p.args.get("time") or "").strip()
                    cs = self.memory.call_state
                    if not self._books_appointments():
                        logger.info("[TOOLS] book_appointment rejected — "
                                    "this agent never books (calendar.type "
                                    "none)")
                        _refused = "refused"
                    elif not date or not tm:
                        logger.info("[TOOLS] book_appointment rejected — "
                                    "missing args %r", p.args)
                        _refused = "unusable"
                    elif not _is_bookable_when(date, tm):
                        # The model passed placeholders instead of a real
                        # slot (observed live 08-03: date='today',
                        # time='now'), and the caller heard the canned
                        # confirmation read it back verbatim — "Done, I have
                        # noted today at now." An unusable slot is not a
                        # booking; let the conversation ask properly.
                        logger.info("[TOOLS] book_appointment rejected — "
                                    "unusable date/time %r / %r", date, tm)
                        _refused = "unusable"
                    elif _is_past_iso_date(date):
                        # The model resolves "kal" itself and sometimes lands
                        # in the wrong YEAR — 2025-09-19 for a call made on
                        # 2026-09-18, measured the day this gate was added.
                        # Stored, that is a diary entry nobody will ever see
                        # and a caller who was told it is noted.
                        logger.info("[TOOLS] book_appointment rejected — "
                                    "%r is already past", date)
                        _refused = "unusable"
                    elif self.state.is_closing or self.state.turn_count <= 1:
                        # Stage-legality gate (phantom-booking protection).
                        logger.info("[TOOLS] book_appointment rejected — not "
                                    "legal now (closing=%s turn=%d)",
                                    self.state.is_closing,
                                    self.state.turn_count)
                        _refused = "refused"
                    elif cs.appointment_date and cs.appointment_time:
                        logger.info("[TOOLS] book_appointment rejected — "
                                    "appointment already fixed (fill-only)")
                    elif not self._booking_hour_was_said(tm):
                        # The model fixed an hour nobody said. Call 1072
                        # (2026-10-12): "संडे को आ जाऊँगी" became a tool call
                        # for 2026-10-18 at 11:00; the caller was told "any
                        # time 10 to 7" and then read back "11 बजे". A day
                        # with no clock time is a question still to ask —
                        # the NOT BOOKED band asks it.
                        logger.info("[TOOLS] book_appointment rejected — "
                                    "the caller never said %r", tm)
                        _refused = "unusable"
                    else:
                        cs.appointment_date = cs.appointment_date or date
                        cs.appointment_time = cs.appointment_time or tm
                        self._record_calendar_booking(f"{date} at {tm}")
                        # The booking is also the answer to the agent's own
                        # day-and-time slot, when it has exactly one. Without
                        # this an agent that keeps record_customer_details
                        # off the spoken path books the call correctly and
                        # still shows "time: unknown" in collected_data.
                        _time_slots = [
                            n for n, sp in self.state_mgr.slots.items()
                            if getattr(sp, "type", "") == "time"
                            and self.state_mgr.state.get(n) is None]
                        if len(_time_slots) == 1:
                            self.state_mgr.merge_state(
                                {_time_slots[0]: f"{date} at {tm}"},
                                source="tool", fill_only=True)
                        logger.info("[TOOLS] book_appointment executed "
                                    "%s at %s", date, tm)
                        if not had_text:
                            lines.append(confirm_line(
                                p.name, lang, {"date": date, "time": tm}))

                elif p.name == "request_callback":
                    when = str(p.args.get("when") or "").strip() or "later"
                    self._capture_callback_flags(when, "")
                    logger.info("[TOOLS] request_callback executed when=%r",
                                when)
                    if not had_text:
                        lines.append(confirm_line(p.name, lang,
                                                  {"when": when}))

                elif p.name == "flag_do_not_call":
                    self.state_mgr.extra_flags["dnd_requested"] = True
                    logger.info("[TOOLS] flag_do_not_call executed")
                    if not had_text:
                        lines.append(confirm_line(p.name, lang, {}))

                elif p.name == "end_call":
                    if bool(self._termination_cfg().get("llm_end_call", True)):
                        self._llm_end_call_seen = True
                        logger.info("[TOOLS] end_call → llm_end_call signal "
                                    "(reason=%r)", p.args.get("reason"))
                    else:
                        logger.info("[TOOLS] end_call ignored — "
                                    "llm_end_call disabled")
                else:
                    logger.warning("[TOOLS] unknown tool proposed: %s", p.name)
            except Exception:
                logger.exception("[TOOLS] executing %s failed", p.name)
        cs = self.memory.call_state
        if _refused and not (cs.appointment_date and cs.appointment_time):
            self._booking_refused = (self.state.turn_count, _refused)
        return lines

    def _spawn_slot_extractor(self, text: str) -> None:
        """Fire a background LLM extraction for slots the regexes missed.

        Latency-safe: runs concurrently with response generation; result
        merges with fill_only=True so a slow/stale completion can only fill
        still-empty slots, never overwrite anything newer. At most one task
        in flight — a new turn cancels the previous extractor.
        """
        if not self._slot_extract_enabled or not self._api_key:
            return
        if len(text.split()) < 3 and not (
                not self._record_on_main_call() and self._last_asked_slot):
            # Too short to carry a fact — unless this agent relies on THIS
            # extractor alone (no record tool on the spoken path) and the
            # caller is answering a question it has just been asked:
            # "Conversions." and "फर्नीचर का" are complete answers.
            return
        # C5 (2026-07-31): spelling turns are noise to the extractor — a
        # live caller spelling "angelo" got their name overwritten to
        # 'Panj Loa'. While the engine expects spelled input, extract nothing.
        if self._capture_slot is not None and self.capture_expecting_input():
            return
        _gaps = self.state_mgr.detect_missing_slots()
        # A slot whose last permitted ask was just spoken is not askable, but
        # it is exactly what the caller is answering now — RUNBOOK §61.
        missing = list(dict.fromkeys(
            list(_gaps["missing"]) + list(_gaps.get("awaiting", []))))
        # B4 (2026-07-30): contact-typed slots are deterministic-only — the
        # LLM once assembled 'bell@gmail.com' from garbled ASR and the
        # fill-only merge made the guess immortal. merge_state also rejects
        # source="llm" for these as a second wall.
        missing = [
            m for m in missing
            if getattr(self.state_mgr.slots.get(m), "type", "")
            not in ("email", "phone")
        ]
        # C5: and while a capture is merely ARMED, the name slot stays
        # off-limits too — email digressions are full of name-shaped noise.
        if self._capture_slot is not None:
            missing = [m for m in missing if m != "name"]
        # A locked identity is no longer "missing", so the extractor would
        # never see the one utterance that IS allowed to change it. Re-offer
        # those slots exactly when the caller explicitly asks for a different
        # name — merge_state applies the same test before storing anything.
        if self._capture_slot is None:
            from .state_manager import wants_name_change
            if wants_name_change(text):
                missing = list(dict.fromkeys(
                    list(missing) + sorted(self.state_mgr.identity_locked)))
        # An OPTIONAL slot the agent has JUST asked about is worth reading too
        # — but only for an agent with no record tool on its spoken path,
        # where this extractor is the one writer left. Optional slots are
        # otherwise never offered here (that is what keeps a late-call field
        # like a meeting time from being guessed out of an early sentence);
        # asked-for is the one moment the answer is actually on the table.
        # Simulated 2026-09-18: "सोमवार ग्यारह बजे" was confirmed aloud and
        # stored nowhere, because the model had not called the booking tool.
        _asked = self._last_asked_slot
        if (_asked and not self._record_on_main_call()
                and _asked in self.state_mgr.slots and _asked not in missing
                and self.state_mgr.state.get(_asked) is None
                and getattr(self.state_mgr.slots[_asked], "type", "")
                not in ("email", "phone")):
            missing = list(missing) + [_asked]
        if not missing:
            return
        if self._slot_extract_task and not self._slot_extract_task.done():
            self._slot_extract_task.cancel()
        self._slot_extract_task = asyncio.create_task(
            self._llm_extract_slots(text, missing), name="slot_extractor",
        )

    def _slot_hint(self, name: str) -> str:
        """' (what the slot means)' for the extractor, or '' if nothing useful.

        Prefers the business's own question wording over the label — it is
        the most precise statement of what the field is, in the language the
        caller was asked in.
        """
        spec = self.state_mgr.slots.get(name)
        if spec is None:
            return ""
        q = ""
        src = getattr(spec, "question", None)
        if isinstance(src, dict):
            q = str(src.get((self.state.language or "en")[:2])
                    or src.get("en") or next(iter(src.values()), "")).strip()
        hint = q or str(getattr(spec, "label", "") or "").strip()
        hint = hint.rstrip("?.! ").replace('"', "'")
        # A closed vocabulary must be NAMED to the writer. The state manager
        # stores the canonical value or nothing, but the question alone does
        # not say what the values are: live-sim 2026-09-28 (Nisha), "around
        # 40 crore" was written as '40 crore' to a slot whose values are
        # 'Under 5 Cr' / '5-25 Cr' / … and REJECTED — so the turnover branch
        # that disqualifies a sub-₹5 Cr caller could never fire. Vocabulary
        # first: the tool description is capped (_SLOT_HINT_MAX_CHARS) and a
        # truncated question costs less than a truncated value list. Long
        # lists (a location roster) are left to the synonym matcher.
        vocab = [str(v) for v in (getattr(spec, "enum_values", None) or [])
                 if str(v).strip()]
        vocab_txt = " | ".join(vocab).replace('"', "'")
        if 2 <= len(vocab) and len(vocab_txt) <= 100:
            tail = f" — the caller's answer to: {hint}" if len(hint) >= 4 else ""
            return f" (record exactly one of: {vocab_txt}{tail})"
        return f" (the caller's answer to: {hint})" if len(hint) >= 4 else ""

    def _record_on_main_call(self) -> bool:
        """llm_config.record_details_on_main_call (default True = today)."""
        return bool(getattr(self._def.llm_config,
                            "record_details_on_main_call", True))

    def _tool_slot_hints(self) -> dict[str, str]:
        """{slot: what it means} for the tool roster — the same wording
        `_slot_hint` gives the json extractor, as a parameter description.
        See build_tool_declarations for why a bare name is not enough."""
        hints: dict[str, str] = {}
        for name in self.state_mgr.slots:
            hint = self._slot_hint(name).strip().strip("()").strip()
            if hint:
                hints[name] = hint[0].upper() + hint[1:]
        return hints

    async def _llm_extract_slots(self, text: str, missing: list[str]) -> None:
        # B4: a caller explicitly correcting an earlier value ("actually…",
        # "nahi, …") may overwrite FILLED slots — the fill-only default made
        # the first (possibly wrong) value immortal. Contact-typed slots stay
        # excluded; validators and merge policies still gate every write.
        from .state_manager import _CORRECTION_MARKER_RE
        _correction = bool(_CORRECTION_MARKER_RE.search(text or ""))
        if _correction:
            _filled = [
                n for n, v in self.state_mgr.state.items()
                if v is not None and n in self.state_mgr.slots
                and n != "name"
                and getattr(self.state_mgr.slots[n], "type", "")
                not in ("email", "phone")
            ]
            missing = list(dict.fromkeys(list(missing) + _filled))
        try:
            if self._tools_mode == "shadow":
                # P1 shadow: same trigger, same fill-only trust level, but
                # the extraction runs through the live tool roster so real
                # calls generate proposal data without touching the spoken
                # path. Action proposals are logged, never executed.
                await self._shadow_tool_extract(text, missing)
                return
            client = self._get_client()
            # Each key carries WHAT IT MEANS, in the business's own words —
            # the slot's question, else its label. A bare key list makes the
            # model guess from the name: live 2026-09-16, a caller answering
            # "yes, I'm available" to "is this a good time to talk?" had
            # joining_timeline set to "Immediate", and the agent later told
            # them they were "available to join immediately" — a fact nobody
            # had said. The question text is what disambiguates
            # joining_timeline (when can you START) from availability (can
            # you talk NOW).
            keys = ", ".join(
                f'"{m}"{self._slot_hint(m)}' for m in missing)
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self._model,
                    messages=[{
                        "role": "system",
                        "content": (
                            "Extract structured fields from one caller utterance "
                            "(may be Hinglish/Hindi/Gujarati/English, possibly noisy "
                            "phone-call ASR). Return ONLY a compact JSON object "
                            f"using these keys: {{{keys}}}. Each key is followed by "
                            "what it means — a value only counts if the caller "
                            "answered THAT. Include ONLY the keys the caller "
                            "clearly answered and omit every other key ({} when "
                            "nothing was stated). Values must be short canonical strings "
                            "(e.g. \"3BHK\", \"50 Lakhs\", \"Vapi\", \"Next month\", "
                            "\"Yes\", \"No\")."
                        ),
                    }, {"role": "user", "content": text}],
                    # Sized to the roster. A flat 120 cut the JSON off before
                    # its closing brace once an agent had ~10 slots (every key
                    # echoed as null, pretty-printed) and the parse below then
                    # stored NOTHING — sim 2026-09-28, Nisha, 11 slots: name,
                    # company, product and turnover extracted correctly and
                    # all discarded, turn after turn.
                    max_tokens=min(600, max(120, 40 + 28 * len(missing))),
                    temperature=0,
                    response_format={"type": "json_object"},
                    **self._llm_extra,
                ),
                # Background task — never on the caller's clock. 2.5 s is
                # plenty while the spoken path also records details; for an
                # agent where THIS is the only writer it cancelled exactly the
                # turn that mattered — the caller's first, densest sentence
                # (sim 2026-09-28: name, company, product and turnover in one
                # breath, nothing stored, every one of them asked again).
                timeout=2.5 if self._record_on_main_call() else 4.5,
            )
            raw = (resp.choices[0].message.content or "").strip()
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if m:
                parsed = json.loads(m.group(0))
            else:
                # A reply cut off mid-object still carries every COMPLETE
                # "key": "value" pair before the cut — keep those rather
                # than dropping the whole turn's facts.
                parsed = {k: v for k, v in re.findall(
                    r'"([A-Za-z0-9_]+)"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)}
                if parsed:
                    logger.info("[SLOTS] truncated extractor reply — "
                                "salvaged %d complete field(s)", len(parsed))
            data = {k: v for k, v in parsed.items()
                    if k in missing
                    and isinstance(v, (str, int, float)) and str(v).strip()}
            data = _drop_non_values(self._scrub_hello_values(data), "llm")
            data = self._drop_yes_no_from_question_turn(data, text, "llm")
            # A content-free acknowledgement answers the question that was
            # JUST asked and nothing else. Live 2026-09-16 (call 883): the
            # caller said ", yeah yeah, we can, we can." to "is now a good
            # time?" and the extractor recorded
            # relocation_willingness='Yes' — a commitment about moving city
            # that nobody had been asked for, which then suppressed the real
            # relocation question.
            if _is_ack_only(text):
                _keep = self._last_asked_slot
                _dropped = [k for k in data if k != _keep]
                if _dropped:
                    logger.info(
                        "[STATE:LLM] bare acknowledgement — ignoring %s "
                        "(only %s could be answered by %r)",
                        ", ".join(_dropped), _keep or "nothing", text[:40])
                data = {k: v for k, v in data.items() if k == _keep}
            # B4 echo guard: a value that appears in the agent's own last
            # utterance but NOT in the caller's is the LLM echoing the
            # agent's read-back into state, not information the caller gave.
            # C5 (2026-07-31): Latin-script caller turns only — the guard is
            # script-blind, and a caller answering "ज़ूम चलेगा" to "Zoom या
            # Google Meet?" had their answer dropped as an "echo" (the value
            # is Latin in the agent's turn, Devanagari in the caller's).
            _agent_last = (self.last_agent_response or "").lower()
            _caller_has_indic = bool(
                re.search(r"[ऀ-ॿ઀-૿]", text))
            if _agent_last and not _caller_has_indic:
                _caller = text.lower()
                data = {
                    k: v for k, v in data.items()
                    if not (str(v).lower() in _agent_last
                            and str(v).lower() not in _caller)
                }
            if data:
                # C6: a value answering the slot the agent JUST asked about
                # may overwrite (the agent asked "आज कितने बजे?" precisely
                # because the filled 'Now' needed refining) — everything
                # else stays fill-only.
                _answer_slot = self._last_asked_slot
                _answer = {}
                if (_answer_slot and _answer_slot in data
                        and not _correction):
                    _answer = {_answer_slot: data.pop(_answer_slot)}
                changes = []
                if data:
                    changes += self.state_mgr.merge_state(
                        data, source="llm", utterance=text,
                        fill_only=not _correction)
                if _answer:
                    changes += self.state_mgr.merge_state(
                        _answer, source="answer", utterance=text,
                        fill_only=False)
                if changes:
                    logger.info("[STATE:LLM] Gap-filled %s",
                                ", ".join(f"{c.field}={c.new!r}" for c in changes))
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as exc:
            logger.debug("[STATE:LLM] extractor skipped: %s", exc)

    async def _shadow_tool_extract(self, text: str, missing: list[str]) -> None:
        """Shadow-mode background extraction via the tool roster.

        record_customer_details fills the still-missing slots exactly like
        the legacy json extractor (fill_only merge); every other proposal is
        `[TOOLS] shadow`-logged for the P1 agreement metric. Grep
        `[TOOLS] shadow proposal` vs the deterministic paths to decide when
        "on" is safe per agent.
        """
        from .llm_tools import (ToolCallAccumulator, build_tool_declarations,
                                clean_slot_args)
        client = self._get_client()
        context = (f"Agent said: {self.last_agent_response or '(call start)'}\n"
                   f"Caller replied: {text}")
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=self._model,
                messages=[{
                    "role": "system",
                    "content": (
                        "You observe one exchange from a phone call (may be "
                        "Hinglish/Hindi/Gujarati/English, noisy ASR). Call "
                        "tools to record any details or actions the CALLER "
                        "clearly stated. Call nothing otherwise."),
                }, {"role": "user", "content": context}],
                max_tokens=200,
                temperature=0,
                tools=build_tool_declarations(
                    list(self.state_mgr.slots.keys()),
                    self._tool_slot_hints(),
                    include_book=self._books_appointments()),
                **self._llm_extra,
            ),
            timeout=2.5,
        )
        msg = resp.choices[0].message
        acc = ToolCallAccumulator()
        acc.add_delta(getattr(msg, "tool_calls", None))
        for p in acc.finish():
            if not p.valid:
                continue
            if p.name == "record_customer_details":
                data = _drop_non_values(
                    self._scrub_hello_values(clean_slot_args(p.args)),
                    "shadow")
                wanted = {k: v for k, v in data.items() if k in missing}
                if wanted:
                    changes = self.state_mgr.merge_state(
                        wanted, source="llm", utterance=text, fill_only=True)
                    if changes:
                        logger.info(
                            "[STATE:TOOLS] Gap-filled %s",
                            ", ".join(f"{c.field}={c.new!r}"
                                      for c in changes))
                extra = {k: v for k, v in data.items() if k not in missing}
                if extra:
                    logger.info("[TOOLS] shadow record extras (not merged): "
                                "%r", extra)
            else:
                logger.info("[TOOLS] shadow proposal: %s %r (caller=%r)",
                            p.name, p.args, text[:80])

    def _extract_project_from_text(self, text: str) -> tuple[str, str] | None:
        """Return (canonical_name, location) if a known project is mentioned.

        Project keys are sorted longest-first so multi-word names
        like 'aranya ii' are matched before shorter substrings like 'aranya'.
        Word-boundary matching prevents short names (aura, orbit, satva) from
        spuriously matching inside unrelated words.
        """
        lower = text.lower()
        _projects = self._def.knowledge_base.known_projects
        for key in sorted(_projects, key=len, reverse=True):
            if _CONFIG_TOKEN_RE.match(key):
                # A size/configuration is not a product. Owners (and the
                # Copilot doc-miner) routinely park "3 BHK" / "2 BHK" in
                # known_projects; matching one locked matched_entity to
                # "3 BHK" and told the model "DO NOT ask which one they want
                # — it is: 3 BHK", so no real project was ever matched.
                # _extract_bhk already owns these tokens.
                continue
            if re.search(r'\b' + re.escape(key) + r'\b', lower):
                return _projects[key]
        return None

    # ── RUNBOOK §68: budget-fit suggestion + caller-confirmed pitch switch ──
    def _pitch_budget_value(self):
        """The caller's budget as the state holds it ('' when unknown)."""
        sm = self.state_mgr
        for n, spec in sm.slots.items():
            if getattr(spec, "type", "") == "money" and sm.state.get(n):
                return sm.state.get(n)
        for n in sm.slots:
            if "budget" in str(n).lower() and sm.state.get(n):
                return sm.state.get(n)
        return self.memory.call_state.domain_fields.get("budget") or ""

    def _pitch_view(self):
        """The agent definition the PER-TURN bands should read: the agent
        itself, or a view whose primary project is the one the caller
        switched to. The cached agent band always reads self._def."""
        if self._pitch is None or self._pitch.active_id == self._pitch.base_id:
            return self._def
        try:
            from . import pitch_switch as _ps
            return _ps.switched_view(self._def, self._pitch.active_id)
        except Exception:
            logger.exception("[PITCH] view failed — using the agent as is")
            return self._def

    def _pitch_goal(self, goal: str) -> str:
        p = self._pitch
        if p is None or p.active_id == p.base_id or not goal:
            return goal
        try:
            from . import pitch_switch as _ps
            kb = self._def.knowledge_base
            old = _ps.item_by_id(kb, p.base_id) or {}
            new = _ps.item_by_id(kb, p.active_id) or {}
            new_name = str(new.get("name") or "")
            if not new_name:
                return goal
            for key in sorted({str(old.get("name") or "")}
                              | {str(a) for a in (old.get("aliases") or [])},
                              key=len, reverse=True):
                if len(key) >= 3:
                    goal = re.sub(r"(?<![\w])" + re.escape(key) + r"(?![\w])",
                                  new_name, goal)
            # "Pramukh Pramukh X" when an alias sat after the brand word.
            return goal.replace("Pramukh " + new_name, new_name)
        except Exception:
            logger.exception("[PITCH] goal re-point failed (continuing)")
            return goal

    def _pitch_config_value(self) -> str:
        """The configuration the caller asked for (RUNBOOK §75): a
        budget-fit alternative that does not offer their BHK is not a fit."""
        sm = self.state_mgr
        for n in ("configuration_required", "configuration", "bhk"):
            if n in sm.slots and sm.state.get(n):
                return str(sm.state.get(n))
        return ""

    def _pitch_band_lines(self) -> list[str]:
        if self._pitch is None:
            return []
        # A fixed visit is not re-litigated on budget: call 1074 told the
        # caller "it doesn't fit your budget" two turns after booking her.
        cs = self.memory.call_state
        if ((cs.appointment_date and cs.appointment_time)
                or self.state.is_closing
                or self._stage_role() == "confirmation"):
            return []
        try:
            from . import pitch_switch as _ps
            return _ps.band(self._def.knowledge_base, self._pitch,
                            self._pitch_budget_value(),
                            self._pitch_config_value())
        except Exception:
            logger.exception("[PITCH] band failed (continuing)")
            return []

    def _pitch_observe_agent(self, text: str) -> None:
        if self._pitch is None or not text:
            return
        try:
            from . import pitch_switch as _ps
            _ps.observe_agent(self._def.knowledge_base, self._pitch, text)
        except Exception:
            logger.exception("[PITCH] observe_agent failed (continuing)")

    def _pitch_observe_caller(self, text: str) -> None:
        try:
            from . import pitch_switch as _ps
            kb = self._def.knowledge_base
            target = _ps.observe_caller(kb, self._pitch, text,
                                        self._pitch_budget_value())
            if not target:
                return
            item = _ps.item_by_id(kb, target) or {}
            name = str(item.get("name") or target)
            # The post-call record and the recommend gate read this; the
            # project the caller is now hearing about is the one discussed.
            self.state.project_recommended = name
            sm = self.state_mgr
            for n in sm.slots:
                if n in ("finalized_property", "selected_project") \
                        and sm.state.get(n):
                    sm.merge_state({n: name}, source="system", utterance=text)
        except Exception:
            logger.exception("[PITCH] observe_caller failed (continuing)")

    # ── RUNBOOK §72: WhatsApp sharing tracker ────────────────────────────
    def _share_pitch_id(self) -> str | None:
        """The project the agent pitches NOW — the switched one after a
        budget switch, else the primary project."""
        if self._pitch is not None:
            return self._pitch.active_id
        try:
            from ..agent_definition.catalog import primary_project
            pp = primary_project(self._def.knowledge_base)
            return pp["id"] if pp else None
        except Exception:
            return None

    def _price_second_ask_enabled(self) -> bool:
        pp = getattr(self._def.knowledge_base, "pricing_policy", None) or {}
        return bool(isinstance(pp, dict) and pp.get("second_ask_approx"))

    def _price_band_lines(self) -> list[str]:
        """━━ PRICE ━━ — only on the turn the caller asked about price, and
        only for an agent with `pricing_policy.second_ask_approx`."""
        if not self._price_ask_pending or not self._price_second_ask_enabled():
            return []
        pp = self._def.knowledge_base.pricing_policy or {}
        approx = str(pp.get("approx_line") or "").strip()
        n = self._price_asks
        lines = ["", "━━ PRICE ━━"]
        if n <= 1 or not approx:
            lines.append(
                "▸ This is their FIRST price question. Reply with the pricing "
                "line only: exact pricing depends on the configuration, floor, "
                "area and availability, and you don't want to confuse them "
                "with a rough figure — you'll share the exact unit price, "
                "carpet area, floor and complete cost sheet (ask 3 or 4 BHK "
                "only if the size is not yet known). NO figure, and no visit "
                "question in this reply.")
        else:
            lines.append(
                f"▸ They have asked about the price AGAIN (ask #{n}). Now give "
                f"the approximate figure, once, calling it approximate: {approx}"
                " — and that the exact unit price and cost sheet still come "
                "from the team. No visit question in this reply.")
        return lines

    def _visit_time_needed_band(self) -> list[str]:
        """━━ TIME NEEDED ━━ — the caller named a day or a part of the day but
        no clock hour, nothing is booked, and the stage is scheduling. Call
        1074: "हाँ, सुबह आ सकती हूँ, संडे को" → "रविवार की सुबह … तय रहा" with
        no time and no calendar row; three tool bookings of 'morning' were
        then refused in silence."""
        try:
            if not self._books_appointments():
                return []
            cs = self.memory.call_state
            if cs.appointment_date and cs.appointment_time:
                return []
            if self._stage_role() not in ("scheduling", "confirmation"):
                return []
            last = self._last_caller_text()
            if not last or not _DAY_OR_PERIOD_RE.search(last) \
                    or _has_clock_hour(last):
                return []
            return [
                "",
                "━━ TIME NEEDED ━━",
                f"▸ They said \"{last[:80]}\" — a day or a part of the day, no "
                "clock time. Nothing is booked. This reply: ask what time "
                "suits them, between 10 AM and 7 PM, in one short sentence — "
                "and nothing else: do not say the visit is fixed, do not read "
                "a day back, no WhatsApp, no new topic.",
            ]
        except Exception:
            logger.exception("[VISIT] time-needed band failed")
            return []

    def _share_band_lines(self) -> list[str]:
        if self._share is None:
            return []
        try:
            from . import share_tracker as _st
            lines = _st.band(self._def.knowledge_base, self._share,
                             self._share_pitch_id())
            # Opt-in per agent (follow_up.whatsapp_promise.offer_from_roles):
            # until the call reaches one of those stage roles, details are
            # not volunteered. The Aristo test calls (2026-10-11) had the
            # agent announce "I was going to send details on WhatsApp"
            # straight after the hello, before a word of requirement.
            _wp = getattr(self._def.follow_up, "whatsapp_promise", None) or {}
            _roles = _wp.get("offer_from_roles") if isinstance(_wp, dict) else None
            if (_roles and not self._share.agreed
                    and self._stage_role() not in set(_roles)
                    and not self.state_mgr.extra_flags.get("dnd_requested")):
                lines.append(
                    "▸ HOLD: it is too early on this call to offer anything "
                    "on WhatsApp. Do not mention WhatsApp, a brochure or "
                    "'sending details', and do not ask whether this number is "
                    "on WhatsApp, until you have understood what they are "
                    "looking for. Only if the caller themselves asks you to "
                    "send something do you say yes (and then ask about the "
                    "number once).")
            return lines
        except Exception:
            logger.exception("[SHARE] band failed (continuing)")
            return []

    def _share_observe_agent(self, text: str) -> None:
        if self._share is None or not text:
            return
        try:
            from . import share_tracker as _st
            _st.observe_agent(self._def.knowledge_base, self._share, text)
        except Exception:
            logger.exception("[SHARE] observe_agent failed (continuing)")

    def _share_observe_caller(self, text: str) -> None:
        """Record the caller's side and persist what changed. The agreed
        list and the number status ride extra_flags → collected_data (the
        WhatsApp follow-up reads them), and the agent's own
        whatsapp_same_number slot is written so CALL STATE and the record
        agree with the band."""
        try:
            from . import share_tracker as _st
            kb = self._def.knowledge_base
            st = self._share
            changes = _st.observe_caller(kb, st, text)
            sm = self.state_mgr
            if st.agreed:
                sm.extra_flags["whatsapp_share_projects"] = [
                    _st.name_of(kb, i) for i in st.agreed]
                sm.extra_flags["whatsapp_promised"] = True
                self._whatsapp_promised = True
            if st.topic_id:
                sm.extra_flags["whatsapp_topic"] = _st.name_of(kb, st.topic_id)
            if st.number != _st.NUMBER_UNKNOWN:
                sm.extra_flags["whatsapp_number_status"] = st.number
            if st.alt_number:
                sm.extra_flags["whatsapp_alt_number"] = st.alt_number
            status = changes.get("number")
            if status and "whatsapp_same_number" in sm.slots:
                sm.merge_state(
                    {"whatsapp_same_number":
                     "Yes" if status == _st.NUMBER_SAME else "No"},
                    source="system", utterance=text)
        except Exception:
            logger.exception("[SHARE] observe_caller failed (continuing)")

    def _note_recommendation(self, text: str) -> None:
        """Record that the AGENT put a named option in front of the caller.

        `project_recommended` is documented as "project name mentioned to
        caller" but the only writer was the extractor for names the CALLER
        said — the agent naming three projects left it None. Every consumer
        (the post-call 'Project discussed' line, and now the
        recommend-before-schedule gate) was reading a field the agent's own
        speech never filled.
        """
        if self.state.project_recommended or not text:
            return
        try:
            from ..agent_definition.catalog import derive_known_projects
            names = derive_known_projects(self._def.knowledge_base)
        except Exception:
            names = self._def.knowledge_base.known_projects or {}
        lower = text.lower()
        for key in sorted(names, key=len, reverse=True):
            if _CONFIG_TOKEN_RE.match(key) or len(key) < 4:
                continue
            if re.search(r'\b' + re.escape(key) + r'\b', lower):
                canonical = names[key]
                self.state.project_recommended = (
                    canonical[0] if isinstance(canonical, (list, tuple)) else str(canonical))
                logger.info("[RECOMMEND] agent named %r",
                            self.state.project_recommended)
                return

    def _qualifying_facts_known(self) -> bool:
        """Has the caller said anything a recommendation could be built on?

        Any filled required slot that is not their identity, plus the legacy
        discovery fields. One is enough — a caller who has said "Surat" has
        told us more than enough to be given a name.
        """
        cs = self.memory.call_state
        if any(k in cs.domain_fields
               for k in ("location", "property_type", "budget")):
            return True
        sm = self.state_mgr
        return any(
            sm.state.get(n) is not None
            for n, spec in sm.slots.items()
            if spec.required and n not in sm.identity_locked
        )

    def _recommendation_gate_open(self) -> bool:
        """True when a recommendation is DUE and none has been made yet.

        Three conditions, all cheap and all per-turn:
          1. the flow declares a recommendation stage (explicit role
             'recommend', or the legacy PROJECT_MATCH id) — a support or
             survey agent has nothing to recommend and is never gated;
          2. there is something to recommend from (non-empty catalog);
          3. the caller has already given at least one qualifying fact
             beyond their name, and no option has been named to them yet.

        call_flow.recommend_before_schedule = on|off overrides (1); 'auto'
        (the default) derives it from the flow as above.
        """
        mode = str(getattr(self._def.call_flow, "recommend_before_schedule",
                           "auto") or "auto").strip().lower()
        if mode == "off":
            return False
        if not (getattr(self._def.knowledge_base, "catalog", None) or []):
            return False
        if mode != "on":
            has_reco_stage = any(
                self._stage_role(s.id) == "recommend" or s.id == STAGE_PROJECT_MATCH
                for s in self._def.call_flow.stages)
            if not has_reco_stage:
                return False
        if self.state.project_recommended:
            return False
        return self._qualifying_facts_known()

    def _update_call_state(self) -> None:
        """Sync CallState from ConversationState + BookingData.

        Called after every data-extraction pass so the call_state is always
        up to date before being injected into the system prompt.
        """
        cs = self.memory.call_state
        b = self.memory.booking

        # Customer name — prefer the confirmed booking name
        if b.user_name and not cs.customer_name:
            cs.customer_name = b.user_name
            logger.info("[STATE_UPDATE] customer_name=%r (from booking)", cs.customer_name)
        elif cs.customer_name and not b.user_name:
            b.user_name = cs.customer_name

        # Appointment — sync both directions
        if b.appointment_time and not cs.appointment_time:
            cs.appointment_time = b.appointment_time
        elif cs.appointment_time and not b.appointment_time:
            b.appointment_time = cs.appointment_time
            if not self._calendar_booked:
                when = cs.appointment_time
                if cs.appointment_date and " at " not in when:
                    when = f"{cs.appointment_date} at {when}"
                self._record_calendar_booking(when)
        if b.appointment_date and not cs.appointment_date:
            cs.appointment_date = b.appointment_date

        # Language + Stage always mirror live state
        cs.language = self.state.language
        cs.conversation_stage = self.state.stage

        # Sync name into QA tracker when confirmed
        if cs.customer_name and not self.memory.qa_tracker.is_answered("name"):
            self.memory.qa_tracker.mark_extracted("name", cs.customer_name)

        logger.info(
            "[STATE] name=%s phone=%s appt=%s stage=%s lang=%s domain=%s",
            cs.customer_name or "—",
            ("***" + cs.customer_phone[-4:]) if cs.customer_phone else "—",
            cs.appointment_time or "—",
            cs.conversation_stage,
            cs.language,
            cs.domain_fields,
        )

    def _compute_missing_booking_fields(self) -> list[str]:
        """Return list of still-missing mandatory booking fields.

        No side effects — safe to call from _build_system_prompt.
        """
        cs = self.memory.call_state
        b = self.memory.booking
        missing: list[str] = []
        if b.appointment_time or cs.appointment_time:
            if not (b.user_name or cs.customer_name):
                missing.append("customer name")
            if not cs.customer_phone:
                missing.append("phone number")
        return missing

    def _validate_booking_for_closing(self) -> tuple[bool, list[str]]:
        """Validate mandatory booking fields; log results.

        Returns (is_valid, missing_field_names).
        Called by _maybe_advance_stage before allowing CLOSING.
        """
        cs = self.memory.call_state
        missing = self._compute_missing_booking_fields()
        valid = len(missing) == 0
        logger.info(
            "[BOOKING_VALIDATION] valid=%s missing=%s",
            valid,
            missing if missing else "none",
        )
        if missing:
            logger.info("[MISSING_REQUIRED_FIELDS] %s", ", ".join(missing))
        cs.booking_validated = valid
        return valid, missing

    # ------------------------------------------------------------------
    # Data extraction helpers
    # ------------------------------------------------------------------

    def _extract_budget(self, text: str) -> str | None:
        """Extract budget range from user input (lakh/crore amounts)."""
        lower = text.lower()
        # Crore patterns: "1 crore", "1.5 crore", "डेढ़ करोड़", etc.
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:crore|cr\.?|करोड़|karod|cr\b)", lower)
        if m:
            return f"{m.group(1)} crore"
        # Lakh patterns: "70 lakh", "50-60 lakh", "70L", etc.
        m = re.search(r"(\d+(?:\.\d+)?)\s*(?:lakh|lac|l\b|लाख|lakh)", lower)
        if m:
            return f"{m.group(1)} lakh"
        # Range: "50 to 70 lakh"
        m = re.search(r"(\d+)\s*(?:to|-)\s*(\d+)\s*(?:lakh|lac|l\b|लाख)", lower)
        if m:
            return f"{m.group(1)}-{m.group(2)} lakh"
        # Gujarati/Hindi script numbers with lakh
        if "લાખ" in text or "करोड़" in text:
            m = re.search(r"(\d+(?:\.\d+)?)\s*(?:લાખ|करोड़)", text)
            if m:
                unit = "lakh" if "લાખ" in text else "crore"
                return f"{m.group(1)} {unit}"
        return None

    def _extract_location(self, text: str) -> str | None:
        loc = match_location(text)
        if loc:
            return loc.capitalize()
        lower = text.lower()
        for city in ["surat", "vapi", "silvassa"]:
            if city in lower:
                return city.capitalize()
        for hi, en in [("सूरत", "Surat"), ("वापी", "Vapi"), ("सिलवासा", "Silvassa")]:
            if hi in text:
                return en
        for gu, en in [("સુરત", "Surat"), ("વાપી", "Vapi"), ("સિલવાસા", "Silvassa")]:
            if gu in text:
                return en
        return None

    def _extract_bhk(self, text: str) -> str | None:
        bhk = match_bhk(text)
        if bhk:
            return bhk
        m = re.search(r"(\d)\s*(?:bhk|bed)", text, re.IGNORECASE)
        if m and 1 <= int(m.group(1)) <= 5:
            return f"{m.group(1)} bhk"
        return None

    def _extract_timeline(self, text: str) -> str | None:
        """Extract caller's stated purchase/move-in timeline."""
        lower = text.lower()
        # Immediate / ready now
        if re.search(r"\b(immediate|right away|asap|ready now|abhi|abhi chahiye|turant)\b", lower):
            return "immediate"
        if any(w in lower for w in ["अभी", "तुरंत", "હમણાં"]):
            return "immediate"
        # Within N months
        m = re.search(r"(\d+)\s*(?:month|mahine|महीने|maheena|महीनों|mashina|mashin)", lower)
        if m:
            return f"{m.group(1)} months"
        # Within N years
        m = re.search(r"(\d+)\s*(?:year|saal|साल|varsh|वर्ष|varsha)", lower)
        if m:
            return f"{m.group(1)} year(s)"
        # End of year / this year
        if re.search(r"\b(end of year|this year|is saal|is varsh|2025|2026)\b", lower):
            return "this year"
        # 6 months / half year
        if re.search(r"\b(6 months?|six months?|chhe mahine|6 mahine)\b", lower):
            return "6 months"
        # 3 months / quarter
        if re.search(r"\b(3 months?|three months?|teen mahine|quarter)\b", lower):
            return "3 months"
        return None

    # Name extraction signals — checked against the last agent response
    _NAME_ASK_SIGNALS = [
        "your name", "naam", "aapka naam", "naam kya", "naam batao",
        "naam bataiye", "naam bata", "apna naam", "apna name",
        "what should i call", "may i know your name", "introduce yourself",
        "aapka naam kya hai", "naam bataiye", "naam kya hai",
        "आपका नाम", "नाम बताइए", "नाम क्या", "अपना नाम",
        "tamaru naam", "tamaro naam", "tamaru", "aapnu naam",
        "tamaru naam shu che", "tamaro naam shu che",
        "તમારું નામ", "નામ", "શું નામ",
    ]

    _NAME_BLOCKLIST = {
        "a", "the", "just", "not", "is", "interested", "busy",
        "calling", "looking", "available", "free", "here", "there",
        "good", "fine", "okay", "sure", "ready", "happy", "sorry",
        "glad", "trying", "going", "coming", "waiting", "talking",
        "asking", "wondering", "thinking", "planning", "searching",
        # Feeling/state words — "i'm even better", "i'm doing well" etc. are
        # small-talk answers to "how are you?", never a name introduction.
        "even", "better", "best", "well", "doing", "great", "alright",
        "blessed", "grateful", "thankful", "tired", "bored", "stressed",
        "exhausted", "sleepy", "hungry", "sick", "ill", "unwell",
        "nervous", "anxious", "worried", "scared", "afraid", "confused",
        "fantastic", "wonderful", "excellent", "perfect", "amazing",
        "awesome", "fabulous", "cool", "nice", "bit",
        # Intensifiers / adverbs that precede a state word
        "very", "really", "so", "too", "quite", "pretty", "more", "much",
        "still", "all", "right", "kind", "sort", "of", "feeling",
        "absolutely", "totally", "super", "completely", "actually",
        "honestly", "literally", "ok",
        # Courtesy / address words that trail a state reply
        "thanks", "thank", "thankyou", "thanku", "thx", "welcome",
        "please", "yaar", "bhai", "sir", "madam", "you", "yourself",
        "today", "now", "also", "anyway", "though",
        "surat", "vapi", "silvassa", "pramukh", "vishal", "patel",
        "yes", "no", "haan", "nahi", "ji", "ha", "na",
        # Greetings — never a name
        "hello", "helo", "hellow", "hi", "hey", "namaste", "namaskar",
        "kem", "kemcho", "hallo", "halo", "bolo", "suno", "suniye",
        "boliye",
        # possessive/pronoun words that precede a name — not a name themselves
        "mera", "meri", "mere", "naam", "my", "name",
        "maro", "mari", "maru", "apnu", "apna", "aapka",
        "tamaru", "tamaro", "tamari", "ame", "tame",
    }

    # Romanized "my name is/was" phrases — user is stating their name will follow,
    # not that the phrase itself IS the name.
    _MY_NAME_PHRASES = frozenset({
        "mera naam", "meri naam", "maro naam", "maru naam",
        "my name", "my name is", "i am", "i'm",
        "tamaro naam", "tamaru naam", "apna naam",
    })

    def _extract_name_if_asked(self, user_text: str) -> str | None:
        """Return a name candidate when the agent's last response asked for the name
        and the user replied with a short phrase that looks like a name.

        This covers the common case where the caller just says their name
        (e.g. "Rahul") without the "my name is" prefix.
        """
        if not self.last_agent_response:
            return None

        agent_lower = self.last_agent_response.lower()
        if not any(s in agent_lower for s in self._NAME_ASK_SIGNALS):
            return None

        user_text = user_text.strip()
        # Reject pure "my name" phrases — the actual name wasn't captured yet
        lower_text = user_text.lower()
        if lower_text in self._MY_NAME_PHRASES or any(
            lower_text == phrase for phrase in self._MY_NAME_PHRASES
        ):
            return None
        # Must be short — names are 1-3 words
        words = user_text.split()
        if len(words) == 0 or len(words) > 4:
            return None
        # Must not contain question markers
        if "?" in user_text:
            return None
        if any(w in user_text.lower() for w in ["what", "kya", "kon", "kaun", "kahan", "kyun"]):
            return None
        # Must not be a location or BHK
        if match_location(user_text) or match_bhk(user_text):
            return None
        # Must not be entirely lowercase short common words
        if all(w.lower() in self._NAME_BLOCKLIST for w in words):
            return None
        # Take consecutive title-cased or long-enough words as the name
        name_parts = []
        for w in words:
            clean = re.sub(r"[^A-Za-z]", "", w)
            if len(clean) < 2:
                break
            if clean[0].isupper() or len(clean) >= 3:
                name_parts.append(clean.capitalize())
        if not name_parts:
            return None
        candidate = " ".join(name_parts)
        if candidate.lower() in self._NAME_BLOCKLIST:
            return None
        return candidate

    def _extract_time_preference(self, text: str, stage: str | None = None) -> str | None:
        """
        Match user-stated time/day to an available slot.

        Returns None when the user gave a SPECIFIC time that isn't on the
        slot list — never silently fudges to a different slot. The engine's
        scheduling reprompt path then offers the real available slots back.

        The ``stage`` parameter gates the bare-affirmation heuristic (Step 5):
        mapping a lone "yes/ok/haan" to the first slot only makes sense when
        the agent just offered slots (INVITE_VISIT/CONFIRMATION).  In earlier
        stages the same words are general affirmations and must NOT book time.
        """
        lower = text.lower()

        # --- Step 1: extract specific hour if present ---
        hour_24: int | None = None
        # English "11 am", "6 pm", "5:00 pm"
        m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b", lower)
        if m:
            h = int(m.group(1))
            is_pm = "p" in m.group(3)
            hour_24 = (h % 12) + (12 if is_pm else 0)
        else:
            # Hindi/Gujarati/transliterated: "11 बजे", "11 વાગે", "11 baje"
            m = re.search(r"(\d{1,2})(?::(\d{2}))?\s*(?:बजे|वजे|વાગે|baje|baaje)", text)
            if m:
                h = int(m.group(1))
                is_evening = bool(re.search(
                    r"(शाम|સાંજે|saanje|shaam|evening|night|रात|રાત)", text + lower))
                is_afternoon = bool(re.search(
                    r"(दोपहर|બપોરે|bapore|dopahar|afternoon)", text + lower))
                is_morning = bool(re.search(
                    r"(सुबह|સવારે|savare|subah|morning)", text + lower))
                if is_evening or is_afternoon:
                    hour_24 = (h % 12) + 12
                elif is_morning:
                    hour_24 = h % 12 if h != 12 else 12
                else:
                    # Default heuristic: 1-7 → PM (afternoon/evening),
                    # 8-12 → AM (morning) — fits typical site-visit phrasing
                    hour_24 = (h % 12) + (12 if 1 <= h <= 7 else 0)

        # --- Step 2: extract day ---
        day: str | None = None
        # v2: calendar.day_synonyms consulted BEFORE the hardcoded map so
        # 'Monday 4 PM'-style slot labels finally match ({} = today).
        for canon_day, phrases in (self._def.calendar.day_synonyms or {}).items():
            for phrase in phrases:
                p = str(phrase).lower()
                if p and (p in lower or str(phrase) in text):
                    day = canon_day
                    break
            if day:
                break
        if day:
            pass
        elif re.search(r"\b(today|aaj|aaje)\b", lower) or "आज" in text or "આજે" in text:
            day = "Today"
        elif (re.search(r"\b(tomorrow|kal|kaale)\b", lower)
              or "कल" in text or "કાલે" in text):
            day = "Tomorrow"
        elif (re.search(r"\b(parso|parson|day after tomorrow)\b", lower)
              or "परसों" in text or "परसो" in text
              or "પરમ" in text or "પરસું" in text or "પરસો" in text):
            day = "Day after tomorrow"
        elif re.search(r"\b(saturday|this saturday|weekend)\b", lower):
            day = "This Saturday"

        # --- Step 3: if specific time given, match strictly against slots ---
        # Only offer/accept slots that are still free (bookings.json aware).
        _slots = self._available_calendar_slots()
        if not _slots:
            return None
        if hour_24 is not None:
            for slot in _slots:
                if day and slot.day != day:
                    continue
                if _slot_hour_24(slot.time) == hour_24:
                    # (was slot['day']/slot['time'] — TypeError on dataclass slots)
                    return f"{slot.day} at {slot.time}"
            # User gave a specific hour with no matching slot — caller will
            # reprompt with available options instead of locking a wrong time.
            return None

        # --- Step 4: no specific hour. Match by part-of-day + day. ---
        period: str | None = None
        if re.search(r"(सुबह|સવારે|savare|subah|morning)", text + lower):
            period = "morning"
        elif re.search(r"(दोपहर|બપોરે|bapore|dopahar|afternoon)", text + lower):
            period = "afternoon"
        elif re.search(r"(शाम|સાંજે|saanje|shaam|evening|night)", text + lower):
            period = "evening"

        if day or period:
            for slot in _slots:
                if day and slot.day != day:
                    continue
                if period and _slot_period(slot.time) != period:
                    continue
                return f"{slot.day} at {slot.time}"
            # Day matched but period didn't (e.g. user wants "Tomorrow evening"
            # — no evening slots tomorrow). Don't fudge: caller reprompts.
            if period:
                return None
            # Day-only match: take the first slot of that day.
            for slot in _slots:
                if slot.day == day:
                    return f"{slot.day} at {slot.time}"

        # --- Step 5: bare affirmation → first available slot ---
        # ONLY apply when the agent is actively offering time slots (INVITE_VISIT or
        # CONFIRMATION stage).  In DISCOVERY/GREETING/PROJECT_MATCH stages the same
        # words ("yes", "haan", "ji") are general affirmations — mapping them to a
        # slot would silently pre-book a visit the caller never agreed to schedule.
        _scheduling_stages = (STAGE_INVITE_VISIT, STAGE_CONFIRMATION)
        # v2 role shim: custom-id stages whose role is scheduling/confirmation
        # also arm the bare-affirmation matcher (id literals stay as fallback).
        _role = self._stage_role(stage) if stage is not None else ""
        if stage is None or stage in _scheduling_stages or \
                _role in ("scheduling", "confirmation"):
            if re.search(r"\b(yes|yeah|ok|okay|sure|haan|ha|ji|theek|first|pehla)\b", lower):
                return f"{_slots[0].day} at {_slots[0].time}"
            if any(w in text for w in ("ठीक", "हां", "हाँ", "જી", "હા", "બરાબર", "ઠીક")):
                return f"{_slots[0].day} at {_slots[0].time}"

        return None

    # ------------------------------------------------------------------
    # Acknowledgment builders
    # ------------------------------------------------------------------

    def _location_ack(self, location: str) -> str:
        lang = self.state.language
        if lang == "hi":
            return f"बढ़िया, {location} एक बहुत अच्छी लोकेशन है!"
        elif lang == "gu":
            return f"સરસ, {location} ખૂબ સારી લોકેશન છે!"
        return f"Excellent choice! {location} is a wonderful location."

    def _bhk_ack(self, bhk: str, location: str) -> str:
        lang = self.state.language
        bhk_upper = bhk.upper()
        if lang == "hi":
            return f"बढ़िया! {location} में {bhk_upper} के लिए हमारे पास शानदार विकल्प हैं।"
        elif lang == "gu":
            return f"સરસ! {location} માં {bhk_upper} માટે અમારી પાસે ઉત્તમ વિકલ્પો છે."
        return f"Great! We have some fantastic {bhk_upper} options in {location}."

    def _build_confirmation_prompt(self) -> str:
        b = self.memory.booking
        cs = self.memory.call_state
        lang = self.state.language
        entity = cs.domain_fields.get("matched_entity") or ""
        appt = b.appointment_time or ""
        if lang == "hi":
            ctx = f"{entity} के लिए " if entity else ""
            return f"तो मैं नोट कर लेता हूँ — {ctx}{appt or 'कल'} का अपॉइंटमेंट। क्या ये सही है?"
        elif lang == "gu":
            ctx = f"{entity} માટે " if entity else ""
            return f"તો હું નોંધ કરી લઉં — {ctx}{appt or 'કાલ'} ની મુલાકાત. આ બરાબર છે?"
        ctx = f"for {entity} " if entity else ""
        return f"Let me confirm — your appointment {ctx}is scheduled for {appt or 'tomorrow'}. Does that work?"

    def _build_closing_message(self) -> str:
        b = self.memory.booking
        cs = self.memory.call_state
        lang = self.state.language
        name = cs.customer_name or ""
        appt = b.appointment_time or ""
        company = self._def.identity.company_name
        if lang == "hi":
            return (
                f"बहुत अच्छा{', ' + name if name else ''}! "
                f"आपका अपॉइंटमेंट {appt} के लिए कन्फर्म हो गया है। "
                f"हम आपको एक कन्फर्मेशन मैसेज भेजेंगे। {company} के लिए धन्यवाद!"
            )
        elif lang == "gu":
            return (
                f"ખૂબ સરસ{', ' + name if name else ''}! "
                f"તમારી {appt} ની મુલાકાત કન્ફર્મ થઈ ગઈ છે. "
                f"અમે તમને કન્ફર્મેશન મેસેજ મોકલીશું. {company} માટે આભાર!"
            )
        return (
            f"Wonderful{', ' + name if name else ''}! "
            f"Your appointment is confirmed for {appt}. "
            f"We'll send you a confirmation message. Thank you for choosing {company}!"
        )

    def _get_proactive_prompt(self, stage: str) -> str:
        lang = self.state.language
        prompts = self._def.response_bank.proactive_prompts.get(stage, {})
        return prompts.get(lang, prompts.get("en", ""))

    # ------------------------------------------------------------------
    # LLM consumption with filler support
    # ------------------------------------------------------------------

    def _filler_allowed(self) -> bool:
        """Filler frequency governors (llm_config.filler_max_per_call /
        filler_min_gap_turns; 0 = ungoverned = today's behaviour: a filler on
        every turn whose first LLM chunk beats filler_timeout_s — which on
        the Kaapd call was every turn). Suppressed filler = wait silently."""
        # Stage suppression (2026-07-23 filler audit): a filler before the
        # agent has even introduced itself ("One moment please... Hi, I'm
        # Prakhar from...") or glued onto the goodbye ("एक मिनट... [farewell]")
        # is pure awkwardness with no latency worth masking — both were
        # observed on real calls. First LLM turn and closing turns never
        # play a filler.
        if self.state.turn_count <= 1 or self.state.is_closing:
            return False
        cfg = self._def.llm_config
        max_per_call = int(getattr(cfg, "filler_max_per_call", 0) or 0)
        if max_per_call and self._fillers_played >= max_per_call:
            return False
        min_gap = int(getattr(cfg, "filler_min_gap_turns", 0) or 0)
        if min_gap and (self.state.turn_count - self._last_filler_turn) < min_gap:
            return False
        return True

    def _effective_filler_timeout(self) -> float:
        """Per-agent filler_timeout_s, floored platform-wide.

        Production measurement (2026-07-23 latency audit, n=12 turns): LLM
        first-chunk spans 730-1228ms, yet 8 agents configure 0.6s — so their
        fillers fired on essentially EVERY turn and, because the caller must
        sit through ~1.1-1.3s of filler audio before the (usually already
        finished) answer, each one ADDED ~a second of delay instead of
        masking any. At 1.5s, zero of the sampled turns would have played a
        filler; it fires only on genuine outliers, which is the whole point.
        The floor is env-tunable (FILLER_TIMEOUT_FLOOR_S) and never LOWERS an
        agent's own configured value.
        """
        try:
            floor = float(os.environ.get("FILLER_TIMEOUT_FLOOR_S", "1.5"))
        except ValueError:
            floor = 1.5
        return max(float(self._def.llm_config.filler_timeout_s), floor)

    async def _consume_llm_with_filler(
        self,
        llm_queue: asyncio.Queue,
        llm_done: asyncio.Event,
        intent: str,
        nudge_back: bool = False,
        user_text: str = "",
    ):
        """Consume LLM queue with filler timeout.

        nudge_back is accepted for API compatibility but no longer appends
        a second utterance — the flow re-prompts naturally on the next turn.
        This prevents the agent from talking non-stop (LLM answer + nudge
        back-to-back without letting the user speak).

        user_text is the caller utterance this turn — needed only to re-ask
        the model for spoken words when it answered with a tool call and no
        sentence (see the text-less-tool-turn repair below).
        """
        full_parts: list[str] = []

        first_chunk = None
        try:
            first_chunk = await asyncio.wait_for(
                llm_queue.get(), timeout=self._effective_filler_timeout(),
            )
        except asyncio.TimeoutError:
            if self._filler_allowed():
                filler = self._pick_filler()
                self.state.filler_used = True
                self._fillers_played += 1
                self._last_filler_turn = self.state.turn_count
                yield EngineResult(text=filler, is_shortcut=True, intent="filler")
            # Post-filler wait is BOUNDED (it used to be a bare get()): a
            # stalled-but-open LLM stream previously meant "one moment
            # please…" followed by up to ~7s of open-ended silence until the
            # httpx timeout. Cap it at stream_timeout_s and fall back to the
            # language-appropriate canned line instead. The fallback replaces
            # the stream entirely — we must not fall through into the
            # chunk loop, which would block on the same stalled queue.
            try:
                first_chunk = await asyncio.wait_for(
                    llm_queue.get(),
                    timeout=float(self._def.llm_config.stream_timeout_s),
                )
            except asyncio.TimeoutError:
                logger.warning("[LLM] Post-filler wait exhausted "
                               "stream_timeout_s — canned fallback")
                fallback = self._def.response_bank.lang_fallbacks.get(
                    self.state.language,
                    self._def.response_bank.response_fallback)
                full_parts.append(fallback)
                yield EngineResult(text=fallback, is_shortcut=False,
                                   intent=intent)
                first_chunk = None
                llm_queue = None  # sentinel: skip the streaming loop

        if first_chunk is not None and llm_queue is not None:
            full_parts.append(first_chunk)
            yield EngineResult(text=first_chunk, is_shortcut=False, intent=intent)

            while True:
                chunk = await llm_queue.get()
                if chunk is None:
                    break
                full_parts.append(chunk)
                yield EngineResult(text=chunk, is_shortcut=False, intent=intent)

        # Native tool proposals ("on" mode): execute through the
        # deterministic gates. Runs BEFORE the end-call check because the
        # end_call tool arms the same _llm_end_call_seen flag the [END_CALL]
        # marker uses. Confirmation lines are yielded only on TOOL-ONLY
        # turns (the compat shim returns tool calls INSTEAD of text) so an
        # executed action is never followed by dead air.
        _had_text = bool(full_parts)
        _had_proposals = bool(self._pending_tool_proposals)
        _tool_lines = self._execute_tool_proposals(had_text=_had_text)

        # ── Text-less tool turn: ask again, with tools switched off ──────
        # The trailer at _call_llm_stream says "ALWAYS also reply with your
        # normal 1-2 spoken sentences alongside any tool call". When the
        # model ignores that, everything the caller hears comes from the
        # canned CONFIRM_LINES — and when the tool filled nothing (a
        # record_customer_details naming a property the agent does not have)
        # there is no canned line either, so the caller hears NOTHING.
        # Measured on this fleet: 0 canned lines in 1,206 agent turns
        # 07-20..07-30, then 7.9% (08-01) / 10.3% (08-03) / 13.6% (08-04) —
        # and 0/517 → 16.8% within maya_lescall_sdr alone, an agent whose
        # slot roster and tool wiring never changed. The action is already
        # committed at this point; re-asking only buys back the sentence.
        if not _had_text and _had_proposals and _TOOL_TEXT_REISSUE:
            _spoken = await self._reissue_text_only(user_text, intent)
            if _spoken:
                _tool_lines = []  # the model's own words ARE the confirmation
                full_parts.append(_spoken)
                yield EngineResult(text=_spoken, is_shortcut=False,
                                   intent=intent)

        for _line in _tool_lines:
            full_parts.append(_line)
            yield EngineResult(text=_line, is_shortcut=True, intent=intent)

        # Last resort: a tool ran, nothing was spoken, and the re-ask did not
        # land. Never end a turn on silence — the caller is waiting.
        if not full_parts and _had_proposals:
            _ack = (self._lang_response(self._def.response_bank.tool_ack_fallbacks)
                    or _TOOL_ACK_FALLBACK.get(
                        (self.state.language or "en")[:2],
                        _TOOL_ACK_FALLBACK["en"]))
            logger.warning("[TOOLS] tool-only turn produced no speech — "
                           "acknowledging instead of dead air")
            full_parts.append(_ack)
            yield EngineResult(text=_ack, is_shortcut=True, intent=intent)

        # LLM end-call signal (termination.llm_end_call): the stream carried
        # an [END_CALL] token — the goodbye chunks above are the farewell, so
        # close exactly like the deterministic farewell paths. Caller speech
        # after this still cancels at the top of process_stream.
        if self._llm_end_call_seen:
            self._llm_end_call_seen = False
            _said = " ".join(full_parts).strip()
            # The model sometimes staples the token onto an OPEN QUESTION
            # ("...Is there anything else I can help you with today?
            # [END_CALL]") — observed live 2026-07-31, where it cut the caller
            # off as they answered. A turn that asks the caller something is
            # by definition not a goodbye, so the marker is a mistake: drop it
            # and let the conversation continue.
            if _said.endswith("?"):
                logger.info("[HANGUP] [END_CALL] ignored — turn ends in a "
                            "question, not a goodbye: %r", _said[-60:])
            elif (_confirm := self._maybe_close_confirm("llm end-call signal")):
                full_parts.append(_confirm)
                yield EngineResult(text=_confirm, is_shortcut=True,
                                   intent="farewell")
            else:
                self.state.is_closing = True
                self.commit_pending_turn()  # closing turn — never rolled back
                self.state.stage = STAGE_CLOSING
                self.hangup_pending = True
                logger.info("[HANGUP] LLM end-call signal — closing after goodbye")
                if not full_parts:
                    # Marker arrived with no speakable goodbye — never end on
                    # dead silence; play the configured closing line.
                    resp = self._lang_response(
                        self._def.response_bank.closing_responses,
                        lang=self._closing_language()) or self._localized(
                        "closing",
                        "Thank you for your time! Have a great day!",
                        lang=self._closing_language())
                    full_parts.append(resp)
                    yield EngineResult(text=resp, is_shortcut=False, intent="farewell")

        # RUNBOOK §75: a scripted stage holds the reply to its lines — the
        # slot question the directive asked, or the stage's after-answer
        # line — spoken after the model's text when the model left it out.
        if full_parts and not self.state.is_closing and not self.hangup_pending:
            try:
                _extra = self._script_followup(" ".join(full_parts))
            except Exception:
                logger.exception("[SCRIPT] follow-up failed (continuing)")
                _extra = ""
            if _extra:
                full_parts.append(_extra)
                yield EngineResult(text=_extra, is_shortcut=True, intent=intent)

        if full_parts:
            full_text = " ".join(full_parts)
            self.memory.add_assistant(full_text, language=self.state.language)
            # Last-response memory (feature 3)
            self.last_agent_response = full_text
            self.last_agent_language = self.state.language
            # QA: detect which field this agent response is asking about
            self.memory.qa_tracker.observe_agent_turn(full_text, self.memory.turn_index)
            self._note_recommendation(full_text)
            self._pitch_observe_agent(full_text)
            self._share_observe_agent(full_text)
            try:
                self._reconcile_ask_budget()
            except Exception:
                logger.exception("[STATE] ask-budget reconcile failed (continuing)")
            # C1/C4/C6: LLM utterances reset + re-derive the capture
            # expectation exactly like shortcut ones — the live email ask
            # came from the LLM and capture stayed dark for five turns.
            self._capture_awaiting_payload = False
            self._capture_awaiting_confirm = False
            try:
                self._note_ask_signals(full_text)
            except Exception:
                logger.exception("[CAPTURE] ask-signal scan failed (continuing)")
            logger.info("[LAST-RESPONSE] Stored LLM response (lang=%s, len=%d)",
                        self.last_agent_language, len(full_text))

    def _build_flow_nudge(self) -> str | None:
        """Build a gentle nudge to return to the booking flow after a detour. Won't repeat the same nudge."""
        missing = self.memory.booking.next_missing_field()
        if not missing:
            return None

        stage_map = {
            "location": STAGE_DISCOVERY,
            "property_type": STAGE_DISCOVERY,
            "appointment": STAGE_INVITE_VISIT,
        }
        target_stage = stage_map.get(missing)
        if not target_stage:
            return None

        if self._last_nudge_stage == target_stage:
            return None

        self._last_nudge_stage = target_stage
        return self._get_proactive_prompt(target_stage)

    # ------------------------------------------------------------------
    # Parallel LLM helpers
    # ------------------------------------------------------------------

    async def _run_llm_to_queue(
        self, text: str, queue: asyncio.Queue, done: asyncio.Event,
    ) -> None:
        try:
            async for chunk in self._call_llm_stream(text):
                await queue.put(chunk)
            self._llm_failure_streak = 0
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[LLM] Background stream failed")
            # Incident B (2026-07-15): a deterministic prompt-build crash is
            # indistinguishable from a network blip here — three live calls
            # answered every turn with the canned fallback. Streak tracking
            # lets the platform end the loop honestly instead of repeating.
            self._llm_failure_streak += 1
            fallback = self._def.response_bank.lang_fallbacks.get(self.state.language, self._def.response_bank.response_fallback)
            _limit = 0
            try:
                _limit = int(os.environ.get("LLM_FAILURE_STREAK_LIMIT", "0"))
            except ValueError:
                _limit = 0
            if _limit > 0 and self._llm_failure_streak >= _limit:
                logger.error(
                    "[LLM] %d consecutive failures — closing gracefully "
                    "instead of looping the canned fallback",
                    self._llm_failure_streak)
                self.call_outcome = self.call_outcome or "system_error"
                self.state.is_closing = True
                self.hangup_pending = True
                fallback = (self._lang_response(
                    self._def.response_bank.graceful_close)
                    or self._lang_response(
                        self._def.response_bank.decline_farewell)
                    or fallback)
            try:
                queue.put_nowait(fallback)
            except asyncio.QueueFull:
                pass
        finally:
            await queue.put(None)
            done.set()

    async def _reissue_text_only(self, user_text: str, intent: str) -> str:
        """Re-ask the model for the sentence it owed this turn.

        Runs only after a text-less tool turn, with the tool declarations
        withdrawn (`force_text`), so the model has nothing to answer with
        except words. Bounded by TOOL_TEXT_REISSUE_TIMEOUT_S — on timeout or
        error the caller falls back to the canned confirmation, exactly
        today's behaviour, so this can only add speech, never remove it.
        """
        _t0 = time.monotonic()
        # What the FIRST pass decided about ending the call. The re-ask runs
        # its own LLM stream, which may raise this flag again on its own
        # text; restoring this snapshot afterwards keeps the retry from
        # arming a close while preserving one the first pass already made.
        _end_call_armed = self._llm_end_call_seen
        parts: list[str] = []
        try:
            async def _collect() -> None:
                async for chunk in self._call_llm_stream(user_text,
                                                         force_text=True):
                    if chunk:
                        parts.append(chunk)

            await asyncio.wait_for(_collect(),
                                   timeout=_TOOL_TEXT_REISSUE_TIMEOUT_S)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            logger.warning("[TOOLS] text re-ask timed out after %.1fs — "
                           "falling back to the canned confirmation",
                           _TOOL_TEXT_REISSUE_TIMEOUT_S)
        except Exception:
            logger.exception("[TOOLS] text re-ask failed")
        # A marker the RETRY invented must not arm termination — it would
        # hang up on a turn the caller has not finished. But clearing this
        # unconditionally also threw away a close the FIRST pass had already
        # decided on, and the re-ask runs ONLY on a text-less tool turn —
        # exactly the shape `end_call` arrives in (tool call, no speech). So
        # every tool-issued close was silently dropped: live call 6ad891a4
        # (2026-08-25, shubhangi-outbound) reached the caller's "yes, yes —
        # let's close the call here", emitted end_call, spoke the farewell,
        # and never hung up. It re-asked instead, four times, then dead-air
        # repair asked a fifth. Restore what the first pass decided, no more.
        if self._llm_end_call_seen and not _end_call_armed:
            if self.state.is_closing:
                # The call is already in its close (booking confirmed /
                # completed_when met) and the re-ask's text IS this turn's
                # only speech: a goodbye here is the goodbye the stage asked
                # for, not an invention. Call 909 (§55.5): three re-ask
                # goodbyes ignored, the same confirmation spoken five times,
                # the caller hung up on a loop. Close-confirm still gates it.
                logger.info("[HANGUP] [END_CALL] in the text re-ask honoured "
                            "— closing stage, first pass was tool-only")
                _end_call_armed = True
            else:
                logger.info("[HANGUP] [END_CALL] in the text re-ask ignored — "
                            "the first pass did not ask to close")
        self._llm_end_call_seen = _end_call_armed
        self._pending_tool_proposals = []
        spoken = " ".join(p.strip() for p in parts if p and p.strip()).strip()
        if not spoken:
            return ""
        logger.info("[TOOLS] text re-ask recovered %d chars in %.0fms",
                    len(spoken), (time.monotonic() - _t0) * 1000)
        return spoken

    async def _call_llm_stream(self, text: str, force_text: bool = False):
        client = self._get_client()

        # RAG: retrieve evidence for this turn.
        #
        # `retrieve_async` runs the whole retrieval — including the embedding
        # HTTP request — in a worker thread with a deadline. The previous
        # version called the blocking client directly from this coroutine, so
        # every turn that reached RAG froze the event loop for the duration of
        # that request (measured p50 320ms / p95 622ms against the live
        # provider) and took VAD, STT and TTS down with it.
        #
        # It never raises: a failure, a timeout or an empty corpus all come
        # back as a result with `abstained` set, and the block below simply has
        # nothing to add to the prompt.
        self._rag_context = ""
        self._rag_citations = []
        self._rag_trace_id = ""
        try:
            from ..documents.store import get_folder_by_agent_id, has_documents
            _agent_folder = get_folder_by_agent_id(self._def.agent_id)
            if _agent_folder and has_documents(_agent_folder):
                from ..documents.retrieval import retrieve_async
                _rag = await retrieve_async(
                    _agent_folder, text,
                    call_id=(self._call_id_provider() if self._call_id_provider
                             else None),
                    turn_index=self.memory.turn_index)
                self._rag_context = _rag.context_block
                # Citations are structured data on the turn record, never
                # dialogue — a phone agent does not say "per source two".
                self._rag_citations = _rag.citations
                self._rag_trace_id = _rag.trace_id
        except Exception:
            logger.debug("[RAG] retrieval skipped for this turn", exc_info=True)

        system = self._build_system_prompt()

        # Final language enforcement — closest to generation for maximum effect
        lang_names = {"hi": "Hindi", "gu": "Gujarati", "en": "English"}
        lang_name = lang_names.get(self.state.language, "Hindi")
        _lang_script_note = {
            "hi": "Use Hindi (Devanagari script preferred, or romanized Hindi). NOT Gujarati, NOT English.",
            "gu": "Use Gujarati (Gujarati script preferred, or romanized Gujarati). NOT Hindi, NOT English.",
            "en": "Use English only. NOT Hindi, NOT Gujarati.",
        }
        if getattr(self._def.languages, "allow_code_mixing", False) and self.state.language != "en":
            # Hinglish mode — the opposite of pure-language enforcement. Encourage
            # natural code-mixing so the agent sounds like a real person, not a
            # textbook. Keep the base script (e.g. Devanagari for Hindi) so TTS
            # renders it correctly, but let common English words flow through.
            _final = (
                f"FINAL INSTRUCTION: Reply in natural HINGLISH. Write the {lang_name} words in "
                f"their native script (Devanagari for Hindi) and keep everyday English words in "
                f"English/Latin letters — e.g. 'नमस्ते! मैं Ananya बात कर रही हूँ, Mr. Ben आपसे "
                f"एक quick meeting करना चाहते हैं।'. Do NOT romanize the Hindi (never write 'main "
                f"aapse baat'), and do NOT translate common English words into pure {lang_name}. "
                f"Mix in the English words Indians actually say (meeting, time, business, company, "
                f"help, sure, ok, actually, AI, schedule). Keep it to 1 short, warm spoken sentence."
            )
        else:
            _final = (
                f"FINAL INSTRUCTION: Your ENTIRE response must be in {lang_name}. "
                f"{_lang_script_note.get(self.state.language, '')} "
                f"Do NOT mix languages. Do NOT switch mid-sentence."
            )
        # One-turn language-switch re-delivery directive (armed by the
        # language_switch / repeat+lang handlers; also cleared at turn start).
        if self._turn_lang_directive:
            _final = self._turn_lang_directive + "\n" + _final
            self._turn_lang_directive = None
        # Tools directive ("on" mode): declaring tools alone is not enough —
        # with the full agent prompt the model simply answers in text and
        # never calls them (observed on the first live E2E). Lives in this
        # trailer, not _build_system_prompt, so the golden fixtures stay
        # env-independent.
        if self._tools_mode == "on" and not force_text \
                and not self._record_on_main_call():
            # No record tool on this agent's spoken path, so nothing here
            # should invite a tool call on an ordinary turn.
            _final += (
                "\nTOOLS: call a tool ONLY for an action the customer has "
                "agreed to — booking a day and time, a callback request, "
                "do-not-call, or ending the call. Their details are noted "
                "for you automatically; never call a tool for that. Whatever "
                "you do, ALWAYS reply with your spoken sentences.")
        elif self._tools_mode == "on" and not force_text:
            _final += (
                "\nTOOLS: use the provided tools to record details the "
                "customer states and actions they agree to — booking a "
                "visit, a callback request, do-not-call, or ending the "
                "call. ALWAYS also reply with your normal 1-2 spoken "
                "sentences alongside any tool call.")
        elif force_text and self._booking_unconfirmed_reason():
            # The tool was REFUSED, not recorded — never tell the model
            # otherwise, or it narrates a booking that does not exist.
            _final += (
                "\nNo booking was recorded. Reply now with ONLY your normal "
                "1-2 spoken sentences, continuing the conversation "
                "naturally; do not say a visit or appointment is booked. "
                "Do not mention systems, notes or tools.")
        elif force_text:
            # Text-only re-ask: the action is already recorded, so the ONLY
            # thing missing is what the caller hears. Say so explicitly —
            # left implicit, the model tends to re-narrate the tool call.
            _final += (
                "\nThe customer's details have already been recorded — you "
                "do not need to record anything. Reply now with ONLY your "
                "normal 1-2 spoken sentences to the customer, continuing "
                "the conversation naturally. Do not mention recording, "
                "systems, notes or tools.")
        # ONE system message, always. Gemini's OpenAI-compat endpoint keeps
        # only a single systemInstruction — with the language trailer sent as
        # a SECOND system message it silently REPLACED the entire agent
        # prompt, so since the Gemini switch every live turn ran prompt-less:
        # no persona, no knowledge, no rules (calls cf6b0e23 / 25dbbabb —
        # the model even parroted the trailer's own 'Ananya/Mr. Ben' example).
        # Merging is provider-agnostic: OpenAI-style backends honor it too.
        messages: list[dict[str, str]] = [
            {"role": "system", "content": system + "\n\n" + _final}]
        messages.extend(self.memory.get_history_for_llm())

        # Native tool calling — "on" mode only. Shadow mode deliberately does
        # NOT declare tools here: the P0 probe showed the Gemini compat shim
        # returns tool calls INSTEAD of text, so declaring them changes what
        # the caller hears; shadow runs on the background extractor instead.
        self._pending_tool_proposals = []
        _tool_kwargs: dict = {}
        _tool_acc = None
        if self._tools_mode == "on" and not force_text:
            from .llm_tools import ToolCallAccumulator
            _tool_kwargs["tools"] = self._turn_tool_declarations()
            _tool_acc = ToolCallAccumulator()

        # Agent-scoped provider context cache (RUNBOOK §62): None unless this
        # agent's cache is READY — the turn then goes out exactly as before.
        _cache_plan = self._context_cache_plan(
            system, _final, messages[1:], _tool_kwargs.get("tools"), force_text)

        _SENTENCE_END = re.compile(r'[.!?।]\s')

        def _next_break(pat, buf: str):
            """Next boundary in `buf` that is not an abbreviation's full stop.

            The bare pattern breaks after ANY period, so "Thank you for
            confirming Mr. Piyush." shipped as two chunks — "…confirming Mr."
            and "Piyush." — each its own TTS request and its own playback WAV,
            separated by the bridge's ~100ms inter-file gap. Measured live
            2026-09-16 (call 882): one four-sentence opening went out as FOUR
            audio files and the 0.07s one was rejected outright by the sink's
            validator, so part of the line was never heard. With {title}
            resolving to "Mr."/"Mrs." on every addressed turn, this fires
            constantly — it is a large part of why the delivery sounds
            chopped up and robotic.
            """
            pos = 0
            while True:
                m = pat.search(buf, pos)
                if m is None:
                    return None
                if buf[m.start()] != "." or not _ABBREV_DOT_RE.search(
                        buf[:m.start() + 1]):
                    return m
                pos = m.end()
        # Clause boundary (comma/colon/semicolon too) — used ONLY for the first
        # emission so audio starts at the model's first natural pause instead of
        # waiting for a whole sentence. Cuts first-audio latency noticeably.
        _CLAUSE_END = re.compile(r'[,;:।.!?]\s')
        _first_emitted = False
        deadline = time.monotonic() + self._def.llm_config.stream_timeout_s

        _spoke_model = self._model
        _usage = None
        try:
            # Primary LLM, with automatic fallback: Groq's free tier has a
            # shared tokens-per-day cap — when the primary fails (429/quota/
            # auth/network), retry ONCE on the fallback provider instead of
            # dropping the caller to a canned "please repeat" line.
            _usage_kwargs = ({"stream_options": {"include_usage": True}}
                             if self._llm_usage_supported else {})
            try:
                stream = await self._primary_stream_create(
                    client, messages, _tool_kwargs, _usage_kwargs, _cache_plan)
            except asyncio.CancelledError:
                raise
            except Exception as _primary_exc:
                # Telemetry must never cost a turn. If the provider rejected
                # the usage ask ITSELF, retry the primary without it before
                # writing the model off as failed — dropping to the fallback
                # model over an unsupported optional parameter would silently
                # change which model speaks to every caller. Any other failure
                # (429, timeout, 5xx) is a real failure: fall through to the
                # normal fallback and LEAVE TELEMETRY ON, because a quota blip
                # says nothing about whether the parameter is supported.
                if _usage_kwargs and _is_param_rejection(_primary_exc):
                    self._llm_usage_supported = False
                    logger.warning(
                        "[LLM] stream_options rejected by %s (%s) — usage "
                        "telemetry disabled for this process, retrying",
                        self._model, _primary_exc)
                    try:
                        stream = await client.chat.completions.create(
                            model=self._model,
                            messages=messages,
                            max_tokens=self._def.llm_config.max_tokens,
                            temperature=0.7,
                            stream=True,
                            **_tool_kwargs,
                            **self._llm_extra,
                        )
                        _primary_exc = None
                    except asyncio.CancelledError:
                        raise
                    except Exception as _retry_exc:
                        _primary_exc = _retry_exc
                if _primary_exc is not None:
                    fb = self._get_fallback_client()
                    if fb is None:
                        raise _primary_exc
                    logger.warning(
                        "[LLM] Primary (%s) failed: %s — falling back to %s",
                        self._model, _primary_exc, self._fallback_model,
                    )
                    # Fidelity record: a different model spoke this turn.
                    if self._fallback_model not in self.llm_models_used:
                        self.llm_models_used.append(self._fallback_model)
                    self.llm_fallback_turns += 1
                    _spoke_model = self._fallback_model
                    # The fallback is not a rare path: with the primary's
                    # free-tier day quota spent, it serves EVERY turn, so
                    # leaving it unmetered would leave the real bill unmeasured
                    # exactly when it matters. Same retry-without guard as the
                    # primary — the fallback has nowhere further to fall, so a
                    # rejected optional parameter must not be fatal here.
                    try:
                        stream = await fb.chat.completions.create(
                            model=self._fallback_model,
                            messages=messages,
                            max_tokens=self._def.llm_config.max_tokens,
                            temperature=0.7,
                            stream=True,
                            **_usage_kwargs,
                            **_tool_kwargs,
                        )
                    except asyncio.CancelledError:
                        raise
                    except Exception as _fb_exc:
                        if not _usage_kwargs or not _is_param_rejection(_fb_exc):
                            raise
                        self._llm_usage_supported = False
                        logger.warning(
                            "[LLM] stream_options rejected by fallback %s — "
                            "usage telemetry off for this process",
                            self._fallback_model)
                        stream = await fb.chat.completions.create(
                            model=self._fallback_model,
                            messages=messages,
                            max_tokens=self._def.llm_config.max_tokens,
                            temperature=0.7,
                            stream=True,
                            **_tool_kwargs,
                        )

            buffer = ""
            timed_out = False
            # Raw model TTFT, distinct from the engine-level first_chunk metric
            # (which also includes the clause-gating below). The latency audit
            # had to reconstruct this split from a single httpx log line —
            # never again. Anchored to the pre-create timestamp (deadline was
            # computed from it) so connection+request time is included.
            _ttft_t0 = deadline - self._def.llm_config.stream_timeout_s
            _ttft_logged = False
            async for chunk in stream:
                if time.monotonic() > deadline:
                    logger.warning("[LLM-TIMEOUT] fallback triggered after %.0fs",
                                   self._def.llm_config.stream_timeout_s)
                    timed_out = True
                    break
                # Token counts, wherever the provider chose to put them. The
                # two backends disagree and BOTH shapes have to work: OpenAI
                # appends a final chunk with an EMPTY choices list carrying
                # usage, while Gemini's compat layer hangs usage off a chunk
                # that also carries content (measured 2026-08-11 — reading it
                # only from empty-choice chunks recorded zero requests against
                # a live Gemini call). Last writer wins; usage is cumulative
                # for the request, not per-chunk.
                if getattr(chunk, "usage", None):
                    _usage = chunk.usage
                # An empty choices list is not an error — it is OpenAI's usage
                # chunk, and some providers send interim keepalives the same
                # way. Unguarded, chunk.choices[0] below raises IndexError,
                # the outer handler swallows it, and the caller hears the
                # generic fallback line INSTEAD of the reply the model just
                # streamed, on every single turn.
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if _tool_acc is not None and getattr(delta, "tool_calls", None):
                    _tool_acc.add_delta(delta.tool_calls)
                if delta.content:
                    if not _ttft_logged:
                        _ttft_logged = True
                        logger.info("[LLM] TTFT %.0fms",
                                    (time.monotonic() - _ttft_t0) * 1000)
                    buffer += delta.content
                    while True:
                        # First chunk: break at the earliest clause pause (>=4
                        # words) to start speaking fast. After that: full
                        # sentences, for natural delivery.
                        pat = _SENTENCE_END if _first_emitted else _CLAUSE_END
                        match = _next_break(pat, buffer)
                        if not match:
                            break
                        sentence = buffer[:match.end()].strip()
                        if not _first_emitted and len(sentence.split()) < 4:
                            break  # too short to ship alone — wait for more
                        buffer = buffer[match.end():]
                        sentence = self._strip_end_call_marker(sentence)
                        if sentence:
                            yield self._scrub_llm_email(
                                _validate_response(sentence, self.state.language,
                                                   self._def.llm_config.max_response_length))
                            _first_emitted = True

            self._record_llm_usage(_usage, _spoke_model, system)

            if _tool_acc is not None and _tool_acc.has_calls:
                self._pending_tool_proposals = _tool_acc.finish()
                logger.info("[TOOLS] stream produced %d proposal(s): %s",
                            len(self._pending_tool_proposals),
                            [p.name for p in self._pending_tool_proposals])

            tail = self._strip_end_call_marker(buffer.strip())
            if timed_out:
                if tail:
                    yield self._scrub_llm_email(
                        _validate_response(tail, self.state.language,
                                           self._def.llm_config.max_response_length))
                else:
                    # Never hang up on the heels of a timeout fallback — the
                    # line promises a follow-up, not a goodbye.
                    self._llm_end_call_seen = False
                    yield (self._lang_response(self._def.response_bank.timeout_fallbacks)
                           or "Let me get back to you on that.")
            elif tail:
                yield self._scrub_llm_email(
                    _validate_response(tail, self.state.language,
                                       self._def.llm_config.max_response_length))

        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.error("[LLM] Stream failed: %s", exc)
            yield self._def.response_bank.lang_fallbacks.get(self.state.language, self._def.response_bank.response_fallback)

    # ------------------------------------------------------------------
    # Cost telemetry
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Agent-scoped provider context cache (RUNBOOK §62)
    # ------------------------------------------------------------------

    def _turn_tool_declarations(self) -> list[dict]:
        """The main call's tool roster for THIS turn (tools mode "on")."""
        from .llm_tools import build_tool_declarations
        _cs = self.memory.call_state
        _booked = bool(_cs.appointment_date and _cs.appointment_time)
        return build_tool_declarations(
            list(self.state_mgr.slots.keys()),
            self._tool_slot_hints(),
            include_record=self._record_on_main_call(),
            # Withdrawn once it can only be refused (§55.5).
            include_book=(self._books_appointments()
                          and not (_booked or self.state.is_closing)))

    def _context_cache_namespace(self) -> str:
        """The agent this cache belongs to — its slug when the bridge set one,
        else its agent_id. Never shared between agents."""
        return (getattr(self, "agent_slug", "") or self._def.agent_id
                or "agent").strip()

    def _context_cache_allowed(self) -> bool:
        from . import context_cache as cc
        if not cc.ENABLED or not cc.is_supported(self._base_url):
            return False
        off = {x.strip() for x in os.environ.get(
            "LLM_CONTEXT_CACHE_DISABLED_AGENTS", "").split(",") if x.strip()}
        return self._context_cache_namespace() not in off \
            and (self._def.agent_id or "") not in off

    def _context_cache_ensure(self, agent_band: str,
                              tools: list[dict] | None):
        """Kick (never await) creation/refresh of this agent's cache for the
        given band + roster. Returns (key, namespace)."""
        from . import context_cache as cc
        ns = self._context_cache_namespace()
        key = cc.make_key(ns, self._model, agent_band, tools)
        tag = ("+".join(sorted(t["function"]["name"] for t in tools))
               if tools else "notools")
        cc.REGISTRY.ensure(
            key=key, namespace=ns, model=self._model, agent_band=agent_band,
            tools=tools, api_key=self._api_key, base_url=self._base_url,
            label=f"{self.state.language}:{tag}")
        return key, ns

    def _context_cache_plan(self, system: str, final: str,
                            history: list[dict], tools: list[dict] | None,
                            force_text: bool) -> dict | None:
        """Cached request body for this turn, or None to send it uncached.
        Never raises — a cache is an optimisation, never a dependency."""
        try:
            if force_text or not self._context_cache_allowed():
                return None
            bands = getattr(self, "_prompt_bands", None)
            if not bands or bands[0] != system or not bands[1]:
                return None
            from . import context_cache as cc
            _, agent_band, call_ctx, turn_ctx = bands
            key, ns = self._context_cache_ensure(agent_band, tools)
            entry = cc.REGISTRY.lookup(key)
            if entry is None:
                cc.REGISTRY.note_miss(ns)
                return None
            msgs = cc.build_cached_messages(
                call_ctx, turn_ctx + "\n\n" + final, history)
            if msgs is None:
                return None
            return {"key": key, "entry": entry, "messages": msgs}
        except Exception:
            logger.debug("[CTX-CACHE] plan failed", exc_info=True)
            return None

    def prewarm_context_cache(self) -> None:
        """Create this agent's cache at call start so the FIRST LLM turn is
        already served from it. Background only; safe to call repeatedly."""
        try:
            if not self._context_cache_allowed():
                return
            # The agent band ONLY — a full prompt build at call start would
            # run the slot directive and spend a question's ask budget
            # before the caller has said a word.
            band = "\n".join(self._agent_band_parts(self.state.language))
            if not band:
                return
            tools = (self._turn_tool_declarations()
                     if self._tools_mode == "on" else None)
            self._context_cache_ensure(band, tools)
        except Exception:
            logger.debug("[CTX-CACHE] prewarm failed", exc_info=True)

    async def _primary_stream_create(self, client, messages, tool_kwargs,
                                     usage_kwargs, plan):
        """The primary model's streaming request — from the agent's context
        cache when a plan exists, else exactly the uncached request. A request
        refused BECAUSE of the cache kills that entry and the same turn is
        re-sent uncached; any other failure propagates to the fallback logic."""
        base = dict(model=self._model,
                    max_tokens=self._def.llm_config.max_tokens,
                    temperature=0.7, stream=True,
                    **usage_kwargs, **self._llm_extra)
        self._turn_cache_tokens = 0
        if plan is not None:
            from . import context_cache as cc
            entry = plan["entry"]
            try:
                stream = await client.chat.completions.create(
                    messages=plan["messages"],
                    extra_body={"extra_body": {"google": {
                        "cached_content": entry.name}}},
                    **base)
                self._turn_cache_tokens = entry.tokens
                cc.REGISTRY.note_hit(entry)
                return stream
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not cc.is_cache_error(exc):
                    raise
                cc.REGISTRY.invalidate(plan["key"], str(exc)[:160])
                logger.warning("[CTX-CACHE] cached request refused — this "
                               "turn re-sent uncached")
        return await client.chat.completions.create(
            messages=messages, **base, **tool_kwargs)

    def _record_llm_usage(self, usage, model: str, system_prompt: str,
                          request_type: str = "turn") -> None:
        """Hand one request's provider-reported token counts to the ledger.

        Silent no-op when `usage` is None — which is the case for the fallback
        provider and for any process where stream_options was rejected. A
        missing request is visibly missing (llm_requests lags the turn count);
        substituting a local tokenizer estimate would put a guess in a cost
        report where it reads as measurement.

        Fire-and-forget: the ledger append is file I/O and the caller is a
        streaming generator with a live caller on the line. Nothing here may
        raise into the turn, so the whole body is defensive.
        """
        if usage is None:
            return
        try:
            prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
            # `prompt_tokens_details` is where OpenAI reports the cached
            # prefix. Gemini's OpenAI-compat endpoint returns it as null
            # (measured 2026-08-11), so this stays 0 there — the compat layer
            # gives us no way to see a cache hit even once the quota exists.
            details = getattr(usage, "prompt_tokens_details", None)
            cached = int(getattr(details, "cached_tokens", 0) or 0) if details else 0
            # The compat route never reports cache hits; a turn served from
            # this agent's context cache carries the cache's own size (the
            # provider's count at creation). Only for the model that used it.
            if model == self._model:
                cached = max(cached, int(getattr(self, "_turn_cache_tokens", 0) or 0))
            if not prompt_tokens and not completion_tokens:
                return
            fingerprint = _prompt_cache.prefix_fingerprint(system_prompt)
            tracker = _credit_tracker()
            if tracker is None:
                return
            call_id = (self._call_id_provider() if self._call_id_provider
                       else None)
            asyncio.ensure_future(tracker.record_llm(
                call_id, model, prompt_tokens, completion_tokens,
                cached_input_tokens=cached, request_type=request_type,
                prefix_fingerprint=fingerprint,
            ))
            # One line per turn carrying the cacheable-prefix hash. Two turns
            # of one call showing different hashes means the static band is no
            # longer static and prompt caching cannot work — the failure is
            # otherwise invisible until a bill arrives.
            logger.info(
                "[LLM-USAGE] model=%s in=%d cached=%d out=%d prefix=%s",
                model, prompt_tokens, cached, completion_tokens, fingerprint)
        except Exception:
            logger.debug("[LLM-USAGE] record failed", exc_info=True)

    # ------------------------------------------------------------------
    # System prompt — Pramukh Group domain
    # ------------------------------------------------------------------

    def _lang_header_line(self, lang: str) -> str:
        """⚡ LANGUAGE = … — the language lock that opens the prompt."""
        _register_notes = self._def.languages.register_notes
        _supported_langs = set(self._def.languages.supported)

        # Build lang_header only for languages this agent supports.
        # Using .get() so missing entries get a plain lock line instead of KeyError.
        def _make_lang_header(code: str, name: str) -> str:
            note = _register_notes.get(code, "")
            # sorted(): set order varies per process (hash seed) — pin it so
            # the prompt is deterministic (required by the golden harness).
            other_langs = ", ".join(sorted(c for c in _supported_langs if c != code))
            note_line = f"\n{note}" if note else ""
            return (
                f"⚡ LANGUAGE = {name.upper()}. You MUST write ONLY in {name}.\n"
                f"Every single word must be {name}."
                + (f" Not {other_langs}." if other_langs else "")
                + f" No full sentences in any other language.\n"
                f"Even if previous responses were in another language — respond in {name.upper()} NOW."
                + note_line
            )

        lang_header = {code: _make_lang_header(code, {"hi": "Hindi", "en": "English", "gu": "Gujarati",
                       "mr": "Marathi", "ta": "Tamil", "te": "Telugu"}.get(code, code.upper()))
                       for code in _supported_langs}
        # Ensure the 'en' fallback always exists for .get() below
        if "en" not in lang_header:
            lang_header["en"] = _make_lang_header("en", "English")
        return lang_header.get(lang, lang_header["en"])

    def _agent_band_parts(self, lang: str, pb=None) -> list[str]:
        """The AGENT band of the system prompt — everything that depends only
        on the agent definition and the language. Order is load-bearing prose
        ("the section above…"); see the band comment in _build_system_prompt.
        Memoised per (definition, language) for the life of the router."""
        key = (id(self._def), lang)
        memo = getattr(self, "_agent_band_memo", None)
        if memo is None:
            memo = self._agent_band_memo = {}
        hit = memo.get(key)
        if hit is not None:
            return hit
        if pb is None:
            from . import prompt_blocks as pb
        parts = [
            # Language lock FIRST — highest priority
            self._lang_header_line(lang),
            *pb.identity_block(self._def, lang),
            *pb.mission_block(self._def),
            *pb.brand_block(self._def),
            *pb.portfolio_block(self._def, lang),
            # Stays directly after the portfolio: its own text says "the
            # section above is your ONLY source of truth".
            *pb.knowledge_grounding_block(self._def),
            *pb.guidelines_block(self._def),
            *pb.personality_block(self._def),
            *pb.style_block(self._def),
            *pb.anti_repetition_block(self._def),
            *pb.objection_block(self._def, lang),
            # Its fabrication rule says "not listed above" — must stay after
            # the portfolio/knowledge sections.
            *pb.firm_rules_block(self._def, lang),
            *pb.call_closure_block(self._def, lang),
        ]
        memo[key] = parts
        return parts

    def _build_system_prompt(self) -> str:  # noqa: C901
        b = self.memory.booking
        cs = self.memory.call_state
        lang = self.state.language
        lang_name = {"hi": "Hindi", "gu": "Gujarati", "en": "English"}.get(lang, "English")
        # Evaluated once per turn. _reco_due drives the hard gate; the softer
        # _match_now drives the deterministic candidate list, which stays
        # visible for as long as the caller's facts could produce a BETTER
        # match than whatever has already been named. Closing it at the first
        # named project is how a ₹4-5 Cr caller kept being shown ₹1.66 Cr
        # stock: the rules that reach the top of the portfolio never rendered.
        try:
            _reco_due = self._recommendation_gate_open()
            _match_now = _reco_due or self._qualifying_facts_known()
        except Exception:
            logger.exception("[RECOMMEND] gate check failed — treating as closed")
            _reco_due = _match_now = False


        # ── Stage-specific goal ──────────────────────────────────────────────
        _stage_goals = self._def.call_flow.stage_goals
        stage_goal = _stage_goals.get(self.state.stage, next(iter(_stage_goals.values()), ""))
        # The goal QUOTES the script the model must say word for word. Show it
        # the way this caller will hear it — {honorific}/{contact_name}
        # resolved, a literal "Sir" held to the contact's gender, "chahta tha"
        # in the voice's gender — or it copies braces and the wrong gender
        # straight into the call (RUNBOOK §42).
        stage_goal = self._render_author_line(stage_goal)
        # RUNBOOK §68: after a caller-confirmed switch the goal names the
        # project being pitched NOW. Only for budget_switch agents, whose
        # goals are written fact-free (deploy/build_pramukh_projects.py).
        stage_goal = self._pitch_goal(stage_goal)

        # ── Collected info ───────────────────────────────────────────────────
        # Use confirmed name only; pending name gets its own confirmation block above
        caller_name = b.user_name or (self.memory.context.user_name if not self._name_pending else None)
        collected_lines = []
        if caller_name:
            collected_lines.append(f"Caller name: {caller_name}")
        if self._name_pending:
            collected_lines.append(f"Name just given (UNCONFIRMED): {self._name_pending!r}")
        if self.state.purpose:
            collected_lines.append(f"Purpose: {self.state.purpose}")

        if self.state.project_recommended:
            collected_lines.append(f"Project discussed: {self.state.project_recommended}")
        if b.appointment_time:
            collected_lines.append(f"Visit scheduled: {b.appointment_time}")
        collected_str = "\n".join(f"  · {c}" for c in collected_lines) or "  · Nothing collected yet"

        # ── Qualification status (for DISCOVERY stage guidance) ───────────────
        _qname = b.user_name or cs.customer_name


        # Name-capture confirmation instruction (feature 4)
        name_confirm_block: list[str] = []
        if self._name_pending:
            name_confirm_block = [
                "",
                f"⚡ NAME CHECK: Caller just gave their name as {self._name_pending!r}.",
                # Not a quotable template: the old "MUST confirm it: 'Your
                # name is X, right?' — nothing else first" was said verbatim,
                # as its own robotic turn, and swallowed whatever else the
                # caller said with the name (sim 2026-09-28: "I'm Rakesh Shah,
                # from Shah Polymers" → "Your name is Rakesh Shah, right?").
                # A caller who lets it pass is auto-accepted after 2 turns;
                # "no, it's …" still discards it (see the resolution above).
                "In your next response, say the name back once, naturally, so they can "
                "correct you if you misheard it — woven into your reply, not a standalone "
                "'your name is …, right?' question — and still respond to everything "
                "else they just said.",
            ]

        # ── Persistent CallState block ────────────────────────────────────────
        # Build "already collected — do not re-ask" list
        # Issues 7 & 8: include ALL collected fields so the LLM never re-asks.
        dont_reask: list[str] = []
        if cs.customer_name:
            dont_reask.append(f"caller's name ({cs.customer_name})")
        if cs.customer_phone:
            dont_reask.append("caller's phone number (stored; do not repeat back)")

        # ── Doctrine bands — ALL owned by prompt_blocks (D13) ────────────────
        from . import prompt_blocks as pb
        _direction_lines = pb.direction_block(
            self._def, self.call_direction,
            self.outbound_contact_name, self.outbound_contact_phone,
            lang=lang, callback_context=self.callback_context,
            honorific=_honorific_word(self.outbound_contact_gender, lang=lang),
            name_title=_title_word(self.outbound_contact_gender, lang=lang),
            render=self._render_author_line)
        _free_slots = [] if b.appointment_time else self._available_calendar_slots()
        _cal_lines = pb.calendar_block(self._def, b.appointment_time, _free_slots)

        # Layout is CACHE-ORDERED (2026-08-10). Every turn of a call re-sends
        # this entire prompt, so what it costs is decided by how much of it
        # the provider can serve from its implicit prefix cache — cached input
        # is 10x cheaper than fresh input. A prefix caches only up to the
        # first byte that differs, so blocks are banded by how often they
        # change:
        #
        #   STATIC   — identical for (agent, language) on every turn of every
        #              call. This is the cached prefix; it must come FIRST.
        #   PER-CALL — fixed once the call starts (direction, who the caller
        #              is). Caches across the turns of a single call.
        #   PER-TURN — stage, slots, goal, calendar. Nothing at or after the
        #              first of these can ever cache, so they come LAST.
        #
        # Before this, ━━ CALL STATE ━━ sat at character 2,221 of a 12,724-
        # character prompt: the cacheable prefix was ~550 tokens, under the
        # provider's minimum, so ~3,200 tokens were billed fresh every turn
        # even though 92% of consecutive prompts are byte-identical.
        #
        # Promoting a block to an earlier band is a real saving; demoting one
        # costs tokens on every turn of every call. Relative order WITHIN a
        # band is load-bearing prose ("the section above", "the END-CALL
        # RULES below") — do not reshuffle it. Recency agrees with the split:
        # the live state and the goal now sit closest to the caller's words.
        # AGENT band — identical for (agent, language) on every turn of every
        # call. Served from the agent's provider context cache (context_cache,
        # RUNBOOK §62). Memoised per language: it depends on nothing but the
        # agent definition, and rebuilding it every turn was pure CPU.
        _agent_parts = self._agent_band_parts(lang, pb)
        _call_parts = [
            # ── PER-CALL — stable from the first turn to the last ───────────
            *_direction_lines,
            *pb.customer_context_block(self.customer_context, self._def),
            *self._call_style_lines(),
        ]
        parts = [
            *_agent_parts,
            *_call_parts,

            # ── PER-TURN — changes every turn; nothing below here caches ────
            # The ━━ CALL STATE ━━ header is the cache boundary itself
            # (prompt_cache.STATIC_PREFIX_MARKER), so it has to be the FIRST
            # per-turn thing emitted. name_confirm_block used to sit above it:
            # empty in every golden fixture, so nothing caught it, but on a
            # live call the turn after a caller gives their name it appears
            # INSIDE the supposedly-static prefix and breaks the cache for
            # that turn. Anything conditional on this turn belongs below.
            "",
            "━━ CALL STATE ━━",
            *name_confirm_block,
            f"Stage: {self.state.stage}  |  Language: {lang_name}  |  Turn: {self.state.turn_count}",
            *([f"  · Visit scheduled: {b.appointment_time}"] if b.appointment_time else []),
            *(["  · Phone: captured (stored internally — never repeat it back)"]
              if cs.customer_phone else []),
            "",
            # THE single authoritative state block: filled/missing slots,
            # audit-backed values, rolling summary, and the one next question.
            # Replaces the old CONFIRMED FIELDS / ALREADY COLLECTED / Known-so-far
            # trio that used to overlap and occasionally contradict each other.
            self.state_mgr.build_prompt_context(
                self.state.language,
                # Discovery-style prompting for the legacy ids AND for the
                # first two stages of custom-id flows (position-based —
                # custom ids like CONFIRM_PERSON/OPENING are never in the
                # legacy tuple, which silently disabled the strong ask-next-
                # missing-slot directive for builder-created agents).
                stage_discovery=(
                    self.state.stage in (STAGE_GREETING, STAGE_DISCOVERY)
                    or next((i for i, s in enumerate(self._def.call_flow.stages)
                             if s.id == self.state.stage), 0) <= 1
                    # A stage that DECLARES slots to collect gets the strong
                    # ask-next-missing-slot directive at any index. Gate is
                    # collect-only (never role: the live pramukh_agent has
                    # explicit 'discovery' roles at index 2-3 whose prompts
                    # must stay byte-identical). Empty on every stage of both
                    # Pramukh agents and every pre-existing agent.
                    or bool(getattr(self._stage_def(), "collect", None))
                ),
                # This stage's OWN questions. Empty on legacy flows, which
                # keep global priority order unchanged.
                stage_slots=list(
                    getattr(self._stage_def(), "collect", None) or []),
                # A greeting / identity-confirm / close that collects nothing
                # asks nothing. Roles are the signal because they are exactly
                # the vocabulary for "this beat is not discovery"; a legacy
                # stage with no role set is untouched.
                stage_asks=self._stage_asks_questions(),
            ),
            *([
                "",
                "━━ ACTIVE CONTEXT ━━",
                f"The caller is interested in: {cs.domain_fields['matched_entity']}.",
                f"Answer specific questions in the context of: {cs.domain_fields['matched_entity']}.",
                f"DO NOT ask which one they want — it is: {cs.domain_fields['matched_entity']}.",
            ] if "matched_entity" in cs.domain_fields else []),
            # The wall clock. Sits in the PER-TURN band because it changes
            # every turn, and immediately before the goal because resolving
            # "tomorrow" into a date is part of pursuing it.
            *pb.now_block(self._def, _now_ist()),
            # A weekday/date pair or "today is …" from the caller that the
            # calendar contradicts (call 1043) — [] on every other turn.
            *pb.date_claim_block(getattr(self, "_turn_caller_text", ""),
                                 _now_ist()),
            *pb.script_fidelity_block(),
            "",
            "━━ CURRENT GOAL ━━",
            stage_goal,
            # Deliberately AFTER the goal: a stage script that quotes a
            # question the agent already asked must not win. On call 851 the
            # ROLE_AND_TIMELINE script said Say: "…also could you let us know
            # how fast you could join us?" — the previous turn had asked
            # exactly that and the caller had answered "immediately", and the
            # caller heard it again word for word.
            *pb.last_question_block(
                self._previous_turn_question(),
                non_answer=getattr(self, "_turn_non_answer", "")),
            # RUNBOOK §73: the person on the line is not the contact — no
            # pitch, only when the contact is reachable. [] otherwise.
            *self._identity_unconfirmed_band(),
            # RUNBOOK §73: a declined site visit stands — details on
            # WhatsApp, never a second invitation. [] until a decline.
            *self._visit_declined_band(),
            # RUNBOOK §79: a visit day/time being negotiated — offer
            # another day, never a callback or a goodbye. [] otherwise.
            *self._visit_negotiation_band(),
            # RUNBOOK §77: a callback was asked for and the call is not
            # closing yet — settle the time, ask nothing else. [] otherwise.
            *self._callback_pending_band(),
            # A book_appointment the gates refused (F4): the caller may have
            # heard "booked" already. [] on every other turn.
            *self._booking_unconfirmed_band(),
            # A day or a part of the day with no clock hour is a question
            # still to ask, not a visit to declare fixed (call 1074).
            *self._visit_time_needed_band(),
            # Repetition guards (§55.5): a visit read back once is not read
            # back again; a name said in the last replies is not said now.
            *pb.visit_confirmed_block(
                cs.appointment_date, cs.appointment_time,
                self.memory.recent_assistant_texts(3),
                llm_end=bool(self._termination_cfg().get("llm_end_call", True)),
                close_question=self._pre_close_question(),
                close_asked=self._close_question_asked()),
            *pb.name_fatigue_block(cs.customer_name,
                                   self.memory.recent_assistant_texts(3)),
            *pb.match_rules_block(self._pitch_view(),
                                  dict(self.state_mgr.state),
                                  self._stage_role(), force=_match_now),
            # RUNBOOK §68: budget-fit suggestion / caller-confirmed switch.
            # [] for every agent without primary_project.budget_switch.
            *self._pitch_band_lines(),
            # RUNBOOK §79: the project this turn is about — named by the
            # caller, or the one(s) the agent's own last line named.
            *self._project_focus_band(),
            # RUNBOOK §72: WhatsApp sharing — topic / agreed list / number.
            # [] for every agent without follow_up.whatsapp_promise.track_projects.
            *self._share_band_lines(),
            # Opt-in: first price ask → policy line; second → the approx figure.
            *self._price_band_lines(),
            # Calendar stays after ━━ CALL STATE ━━: it returns [] once a
            # visit is booked, on the grounds that CALL STATE already said so.
            *_cal_lines,
            # Last block before the caller's words, and deliberately after
            # CURRENT GOAL: while it is open it OUTRANKS the stage goal — a
            # greeting-stage goal must not be able to talk the model into
            # booking a visit before anything has been recommended.
            *(pb.recommend_first_block(self._pitch_view()) if _reco_due else []),
        ]

        # C2 (2026-07-31): while an email capture is armed, the engine owns
        # the value end-to-end — the LLM once read back four assemblies it
        # could not write and "confirmed" one that was never in state.
        # Runtime injection so EXISTING agents get the doctrine too (the
        # Copilot enrichment only reaches newly built ones).
        if self._capture_slot is not None:
            parts.append("")
            parts.append(
                "━━ EMAIL CAPTURE (ENGINE-OWNED) ━━\n"
                "The system is capturing the caller's email deterministically. "
                "NEVER state, spell, read back, or confirm an email address "
                "yourself — the system does that. NEVER claim the email has "
                "been noted. NEVER ask the caller to speak slowly; if needed, "
                "ask them to say the full email in one go.")

        # QA tracker block — second layer of anti-repetition enforcement
        qa_block = self.memory.qa_tracker.to_prompt_block()
        if qa_block:
            parts.append("")
            parts.append(qa_block)

        context_str = self.memory.context.to_prompt_string()
        if context_str:
            parts.append(f"\nKnown context:\n{context_str}")

        if self._rag_context:
            parts.append("")
            parts.append(self._rag_context)

        full = "\n".join(parts)
        # The two halves the context cache needs: the AGENT band (cached,
        # shared by every call of this agent) and everything after it (sent
        # with the turn). full == agent_band + "\n" + turn_context, byte for byte.
        _agent_band = "\n".join(_agent_parts)
        _rest = full[len(_agent_band) + 1:]
        _call_band = "\n".join(_call_parts)
        _turn_band = (_rest[len(_call_band) + 1:] if _call_band
                      and _rest.startswith(_call_band) else _rest)
        if not _rest.startswith(_call_band):
            _call_band = ""
        self._prompt_bands = (full, _agent_band, _call_band, _turn_band)
        return full


    # ------------------------------------------------------------------
    # Language detection
    # ------------------------------------------------------------------

    def _evidence_switch_enabled(self) -> bool:
        """A2 master gate: env knob AND per-agent languages.evidence_switch."""
        if not LANG_EVIDENCE_SWITCH:
            return False
        return getattr(self._def.languages, "evidence_switch", True) is not False

    def _language_votes(self, text: str) -> tuple[dict[str, int], int]:
        """A2 per-word vote count for (en, hi, gu) plus the en function-word
        count. Neutral (no vote): digits/emails/spelled letters, names and
        unknown Latin tokens, bare acks, ambiguous romanized words, weak
        Gujarati-in-Devanagari markers, unsupported scripts."""
        votes = {"en": 0, "hi": 0, "gu": 0}
        en_function = 0
        for raw in text.lower().split():
            w = raw.strip(_SCORER_STRIP_CHARS)
            if not w or "@" in w or any(ch.isdigit() for ch in w):
                continue
            if any("઀" <= c <= "૿" for c in w):
                votes["gu"] += 1
                continue
            if any("ऀ" <= c <= "ॿ" for c in w):
                if _STRONG_GU_DEVA_RE.search(w):
                    votes["gu"] += 1
                elif _WEAK_GU_DEVA_RE.search(w):
                    pass    # ambiguous marker — neutral
                else:
                    votes["hi"] += 1
                continue
            if not w.isascii():
                continue    # unsupported script — validation's problem
            if w in _SCORER_ACK_NEUTRAL:
                continue
            in_hi = w in _HI_ROMAN_VOTE_WORDS
            in_gu = w in _GU_ROMAN_VOTE_WORDS
            if in_hi and in_gu:
                continue
            if in_hi:
                votes["hi"] += 1
            elif in_gu:
                votes["gu"] += 1
            elif w in _EN_FUNCTION_WORDS:
                votes["en"] += 1
                en_function += 1
        return votes, en_function

    def _switchable_languages(self) -> frozenset[str]:
        """The languages this agent may be carried into mid-call.

        Exactly `languages.supported` (plus the default, which is always
        speakable). An agent built for Hindi/English/Gujarati follows the
        caller across all three; an English-only agent stays English no
        matter what it hears — that is a build decision, not a runtime one.
        """
        langs = {str(x).strip()[:2].lower()
                 for x in (self._def.languages.supported or [])}
        langs.add(str(self._def.languages.default or "en").strip()[:2].lower())
        return frozenset(x for x in langs if x in ("en", "hi", "gu"))

    def _auto_switch_enabled(self) -> bool:
        """languages.auto_switch — ON by default, platform-wide."""
        return bool(getattr(self._def.languages, "auto_switch", True))

    def _evidence_switch_decision(self, text: str) -> tuple[str | None, str]:
        """A2 (2026-07-30): decide a mid-call language switch from word
        evidence inside THIS utterance. Returns (target | None, reason).

        Tiers: zero votes → the turn doesn't vote at all; density ≥ 0.6 with
        ≥ 2 votes → instant switch (en additionally needs a function word —
        structurally guaranteed since only function words vote en); one vote
        or density in [0.3, 0.6) → confirmation band: hold, arm
        _pending_lang_switch, complete on any supporting vote next voting
        turn; a turn voting the current language disarms. After any
        evidence switch, non-explicit switching pauses for 1 turn.
        """
        cur = self.state.language
        if not self._auto_switch_enabled():
            return None, "auto_switch_off"
        votes, en_function = self._language_votes(text)
        total = sum(votes.values())
        if total == 0:
            return None, "no_votes"
        best = max(votes, key=lambda k: votes[k])
        if votes[best] == 0 or best == cur:
            self._pending_lang_switch = None
            return None, "current"
        # Switching is bounded by the languages this agent was BUILT for.
        # Without this the switcher could carry a call into a language the
        # agent has no greeting, no responses and no TTS voice for.
        if best not in self._switchable_languages():
            self._pending_lang_switch = None
            return None, f"unsupported({best})"
        density = votes[best] / total
        if density < 0.3:
            self._pending_lang_switch = None
            return None, f"weak(density={density:.2f})"
        if self.state.turn_count - self._last_evidence_switch_turn <= 1:
            return None, "cooldown"
        if best == "en" and en_function < 1:
            return None, "en_no_function_word"
        detail = f"votes={votes[best]}/{total},density={density:.2f}"
        if votes[best] >= 2 and density >= 0.6:
            self._pending_lang_switch = None
            return best, f"evidence({detail})"
        # UNANIMOUS evidence on a real sentence switches immediately. The
        # two-vote bar cost the caller a whole turn: live 2026-08-04,
        # "haan, baat kar sakte hain." scored votes=1/1 density=1.00 and was
        # held as `pending`, so a Hindi speaker was answered in English and
        # had to say it again before the agent followed. Nothing in that
        # utterance voted for another language — one vote out of one, in a
        # multi-word sentence, is not a coin flip.
        if (density >= 1.0
                and len(text.split()) >= LANG_UNANIMOUS_MIN_WORDS
                and not (best == "en" and en_function < 1)):
            self._pending_lang_switch = None
            return best, f"evidence_unanimous({detail})"
        if self._pending_lang_switch == best:
            self._pending_lang_switch = None
            return best, f"evidence_confirmed({detail})"
        self._pending_lang_switch = best
        return None, f"pending({detail})"

    async def detect_language_v3(self, text: str) -> tuple[str, float, str]:
        text_lower = text.lower().strip()
        if not text_lower:
            return "en", 1.0, "default"

        if text_lower in self._lang_cache:
            return self._lang_cache[text_lower]

        if any("઀" <= c <= "૿" for c in text_lower):
            result = ("gu", 1.0, "script")
        elif any("ऀ" <= c <= "ॿ" for c in text_lower):
            # Devanagari detected — but STT often writes Gujarati in
            # Devanagari. Check for Gujarati marker words in Devanagari.
            if self._has_gujarati_markers_devanagari(text_lower):
                result = ("gu", 0.9, "script_gu_markers")
            else:
                result = ("hi", 1.0, "script")
        else:
            hi_score = _pattern_score(text_lower, _HINDI_PATTERNS)
            gu_score = _pattern_score(text_lower, _GUJARATI_PATTERNS)
            total = hi_score + gu_score

            if total > 0:
                confidence = abs(hi_score - gu_score) / total
                lang = "hi" if hi_score >= gu_score else "gu"
                result = (lang, max(confidence, 0.5), "pattern")
            elif text_lower.isascii():
                # ASCII text with zero Hindi/Gujarati pattern hits is overwhelmingly
                # English. Defaulting to "hi" here forced English speakers into
                # Devanagari STT on the next turn.
                result = ("en", 0.5, "default")
            elif self.state.language_history or LANG_LLM_CLASSIFY_FIRST_TURN:
                lang = await self._classify_language_llm(text_lower)
                result = (lang, 0.9, "llm")
            else:
                # First turn: never block the caller's first reply on an
                # extra LLM round-trip. STT's own detection has already been
                # mirrored into state.language by the pipeline; fall back to
                # the agent default. The en/hi/gu hard guard below still runs.
                lang = self.state.language or "hi"
                result = (lang, 0.6, "stt_or_default_first_turn")

        # Hard guard: only the three supported languages may be returned.
        # If the LLM fallback returned something unexpected (or a new script
        # leaked through), default to Hindi rather than propagating a
        # language code the agent has no responses for.
        if result[0] not in ("en", "hi", "gu"):
            logger.warning(
                "[LANG] detect_language_v3 returned unsupported lang=%r for %r — "
                "overriding to 'hi'",
                result[0], text[:60],
            )
            result = ("hi", 0.3, "guard")

        self._lang_cache[text_lower] = result
        return result

    @staticmethod
    def _has_gujarati_markers_devanagari(text: str) -> bool:
        """Detect Gujarati words written in Devanagari by STT.

        Two-tier detection:
        - Strong copulas (छे/छो/छु/केम/तमे/नथी/हसे/जोइए): exclusively Gujarati,
          never appear as word-stems in Hindi. ONE hit is sufficient.
          Word-boundary matching prevents false matches inside Hindi words like छोटा.
        - Weak markers: ambiguous — require 2+ hits for confidence.

        This fixes the regression where short Gujarati greetings like "kem cho"
        get transcribed as "गेम छो" (Devanagari via hi-IN STT), producing only
        one marker hit ("छो") that the old >= 2 threshold would reject.
        """
        # Strong tier: one boundary-clean match is enough
        if _STRONG_GU_DEVA_RE.search(text):
            return True

        # Weak tier: ambiguous markers, need 2+ boundary-clean hits (the old
        # substring scan matched inside Hindi words — see _WEAK_GU_DEVA_RE)
        return len(_WEAK_GU_DEVA_RE.findall(text)) >= 2

    async def _classify_language_llm(self, text: str) -> str:
        try:
            client = self._get_client()
            response = await client.chat.completions.create(
                model=self._model,
                messages=[{
                    "role": "user",
                    "content": _LANG_CLASSIFY_PROMPT.format(text=text),
                }],
                max_tokens=5,
                temperature=0.0,
                **self._llm_extra,
            )
            raw = response.choices[0].message.content.strip().lower()
            return _LANG_NAME_TO_CODE.get(raw, "en")
        except Exception as exc:
            logger.warning("[LANG] LLM classification failed: %s", exc)
            return "en"

    # ------------------------------------------------------------------
    # Objection + filler helpers
    # ------------------------------------------------------------------

    def _extract_requested_language(self, text: str) -> str:
        lower = text.lower()

        # Identify negated languages up front so they don't leak into
        # instead/directive/last-position resolution. "I can't speak Hindi"
        # contains "speak hindi" which would otherwise match the directive
        # regex and route to Hindi — the opposite of what the user wants.
        _lang_alt = (
            r"english|hindi|gujarati|inglish|angrezi|"
            r"इंग्लिश|अंग्रेज़ी|अंग्रेजी|हिंदी|हिन्दी|गुजराती|"
            r"ગુજરાતી|અંગ્રેજી|હિન્દી|ઇંગ્લિશ"
        )
        _neg_after = r"(?:नहीं|नहि|नही|न्ही|નથી|નહિ|નહી|નહીં|nahi|nahin|nai)"
        _neg_before_en = r"(?:don'?t|do not|can'?t|cannot)\s+(?:know|speak|talk|understand)?"
        _name_to_code = {
            "english": "en", "angrezi": "en", "inglish": "en",
            "hindi": "hi", "gujarati": "gu",
            "इंग्लिश": "en", "अंग्रेज़ी": "en", "अंग्रेजी": "en",
            "ઇંગ્લિશ": "en", "અંગ્રેજી": "en",
            "हिंदी": "hi", "हिन्दी": "hi", "હિન્દી": "hi",
            "ગુજરાતી": "gu", "गुजराती": "gu",
        }
        negated: set[str] = set()
        for pat in (
            rf"({_lang_alt})\s{{0,3}}{_neg_after}",
            rf"{_neg_before_en}\s+({_lang_alt})",
        ):
            for m in re.finditer(pat, text, re.IGNORECASE):
                key = m.group(1)
                code = _name_to_code.get(key) or _name_to_code.get(key.lower())
                if code:
                    negated.add(code)

        # "X instead of Y" / "X not Y" / "X rather than Y" — the language
        # BEFORE the marker is the preferred one. Check this first because the
        # generic directive regex below would otherwise match "of Y" / "to Y".
        instead_match = re.search(
            r"(english|hindi|gujarati|angrezi|inglish)\s+(?:instead\s+of|not|rather\s+than|over|than)\s+(english|hindi|gujarati|angrezi|inglish)?",
            lower,
        )
        if instead_match:
            preferred = instead_match.group(1)
            code = {
                "english": "en", "angrezi": "en", "inglish": "en",
                "hindi": "hi", "gujarati": "gu",
            }[preferred]
            if code not in negated:
                return code

        # Prefer language names that follow a directive word — the destination
        # of "speak in X" / "switch to X" / "talk X" is what the user wants.
        directive_match = re.search(
            r"(?:speak|talk|switch|continue|reply|use|change|in|to|me|mai|mein|ma|mei)\s+(?:in\s+|to\s+)?(english|hindi|gujarati|angrezi|inglish)",
            lower,
        )
        if directive_match:
            target = directive_match.group(1)
            code = {
                "english": "en", "angrezi": "en", "inglish": "en",
                "hindi": "hi", "gujarati": "gu",
            }[target]
            if code not in negated:
                return code

        # Fallback: pick the LAST language name mentioned. Handles cases like
        # "switch from hindi to english" → returns en correctly.
        positions: list[tuple[int, str]] = []
        for word, code in [
            ("english", "en"), ("angrezi", "en"), ("inglish", "en"),
            ("hindi", "hi"), ("gujarati", "gu"),
        ]:
            idx = lower.rfind(word)
            if idx >= 0:
                positions.append((idx, code))
        for word, code in [
            ("इंग्लिश", "en"), ("अंग्रेज़ी", "en"), ("अंग्रेजी", "en"),
            ("અંગ્રેજી", "en"), ("ઇંગ્લિશ", "en"),
            ("हिंदी", "hi"), ("हिन्दी", "hi"), ("હિન્દી", "hi"),
            ("ગુજરાતી", "gu"), ("गुजराती", "gu"),
        ]:
            idx = text.rfind(word)
            if idx >= 0:
                positions.append((idx, code))
        if positions:
            positions.sort(reverse=True)
            for _, code in positions:
                if code not in negated:
                    return code
            # All mentioned languages were negated — keep current language
            # if it's not the negated one; otherwise fall back to script.
            if self.state.language not in negated:
                return self.state.language
            if any("઀" <= ch <= "૿" for ch in text):
                return "gu"
            if any("ऀ" <= ch <= "ॿ" for ch in text):
                return "hi"
            return "en"
        return self.state.language

    def _closing_language(self) -> str:
        """Language for closing-flavoured lines (closing_responses,
        decline_farewell, wrong_number_close). Policy '' = today: the
        last-turn state.language — the single most misdetection-prone turn
        ('bye bye thank you' flipped the all-Hindi Kaapd call to English one
        turn before the close). 'dominant' = majority language over the
        caller's turns this call, restricted to languages.supported;
        tie-break = most recent, fallback = state.language."""
        policy = str(getattr(self._def.languages, "closing_language_policy",
                             "") or "")
        if policy != "dominant":
            return self.state.language
        return self.dominant_language()

    def dominant_language(self) -> str:
        """Majority language over the caller's turns this call, restricted
        to languages.supported; tie-break = most recent; fallback =
        state.language. Policy-independent on purpose: the call ROW's
        language is written from this, not from whatever the last turn
        flipped to — call 934 (2026-09-24) was 20 Hindi turns and one
        "ji ji ji okay sir thank you", and its row says `en`."""
        supported = set(self._def.languages.supported or [])
        turns = [t for t in (getattr(self.memory, "_turns", None) or [])
                 if getattr(t, "role", "") == "user"]
        counts: dict[str, int] = {}
        for t in turns:
            tl = getattr(t, "language", "") or ""
            if tl and (not supported or tl in supported):
                counts[tl] = counts.get(tl, 0) + 1
        if not counts:
            return self.state.language
        best = max(counts.values())
        winners = {l for l, c in counts.items() if c == best}
        if len(winners) > 1:
            for t in reversed(turns):
                tl = getattr(t, "language", "") or ""
                if tl in winners:
                    return tl
        return next(iter(winners))

    def _lang_response(self, bank: dict, lang: str | None = None) -> str:
        """Per-language response chain: lang → en → first value → ''.
        Fixes the ['en'] KeyError traps for Copilot-built agents.

        THE funnel for every authored spoken line in the agent — closings,
        canned phrases, objection answers, identity responses, graceful
        close, wrong-number/wrong-person lines, handoff, timeout fallbacks.
        Placeholders are resolved here so gendered address is not a greeting
        feature: an owner who writes "Thank you {honorific}" into their
        closing line gets it, the same way they get it in the opening.

        Substitution only, never the strip backstop — `{trust_proofs}` is
        resolved by `_match_objection_playbook` AFTER this returns, and the
        surfaces that finish a line call `_personalize` themselves.
        """
        if lang is None:
            lang = self.state.language
        if not isinstance(bank, dict) or not bank:
            return ""
        val = bank.get(lang) or bank.get("en") or next(iter(bank.values()), "")
        if isinstance(val, list):
            if not val:
                return ""
            resp = val[self._response_counter % len(val)]
            self._response_counter += 1
            return self._substitute_tokens(str(resp))
        return self._substitute_tokens(str(val or ""))

    def _match_objection_playbook(self, text: str) -> str | None:
        """D3: deterministic objection playbook match — a configured pattern
        hit returns the playbook answer for the caller's language."""
        playbook = getattr(self._def.response_bank, "objection_playbook", None) or []
        lower = text.lower()
        proofs = "; ".join(getattr(self._def.brand, "trust_proofs", []) or [])
        for item in playbook:
            patterns = [str(p).lower() for p in (item.get("patterns") or [])]
            if any(p and p in lower for p in patterns):
                answer = self._lang_response(item.get("answer") or {})
                if answer:
                    return answer.replace("{trust_proofs}", proofs)
        return None

    def _pick_objection_response(self, text: str = "") -> str:
        # v2: playbook first (empty playbook = today's rotation).
        if text:
            pb = self._match_objection_playbook(text)
            if pb:
                return pb
        resp = self._lang_response(self._def.response_bank.objection_responses)
        resp = strip_unresolved_tokens(resp, "objection_response")
        return resp or "I understand. Could I share one quick point before you go?"

    # ── v2 callback policy (D10) ────────────────────────────────────────

    # `am`/`pm` are clock words ONLY next to a number. As bare tokens they
    # matched "ma'am", "I am", "am I speaking…" — and on 2026-09-15 that is
    # how "yes ma'am, so I just got a call from your number" was read as a
    # time preference and answered with "which day and what time would work
    # best for us to call you?" on the caller's FIRST sentence.
    _CALLBACK_TIME_RE = re.compile(
        r"\b(kal|tomorrow|parso|evening|morning|afternoon|shaam|subah|dopahar|"
        r"baje|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
        r"next week|baad|later|minutes?|mins?|minit|hours?|ghante|ghanta)\b"
        r"|\d\s*(?:a\.?m\.?|p\.?m\.?)\b"
        r"|मिनट|घंटे|घंटा|મિનિટ|કલાક", re.IGNORECASE)
    # The caller REPORTING a call they received is not the caller ASKING to
    # be called. Inbound lines attached to an outbound campaign open with
    # this sentence more often than with anything else — "I got a call from
    # this number 20 minutes back, what is this about?" — and it carries both
    # a duration and the word "call", which is all the weak branch below
    # needs. Live 2026-09-15 (calls 856, 863): two callers asking what the
    # role was got "Perfect — I'll call you back in about 20 minutes. Thank
    # you, and have a great day!" and the platform hung up on them.
    _RECEIVED_CALL_RE = re.compile(
        r"\b(?:i|we|just)?\s*(?:got|get|received|had|have|missed|saw|seen)\s+"
        r"(?:a|an|one|your|this|the)?\s*(?:miss(?:ed)?\s*)?(?:call|phone)\b"
        r"|\b(?:you|u|someone|somebody|somone|anyone)\s+(?:just\s+|had\s+)?"
        r"call(?:ed|ing)\b"
        r"|\bmiss(?:ed)?\s*call\b"
        r"|\bcall\s+(?:came|aaya|aya|aayi|ayi)\b"
        r"|\b(?:aapka|apka|tumhara|tamaro)\s+(?:call|phone)\s+(?:aaya|aya|hato)\b"
        r"|\b(?:call|phone)\s+(?:aaya|aya|aayo)\b"
        r"|कॉल\s*(?:आया|आयी|आई)|फ़?ोन\s*(?:आया|आयी)|मिस\s*कॉल"
        r"|કૉલ\s*આવ્યો|ફોન\s*આવ્યો|મિસ\s*કૉલ",
        re.IGNORECASE)
    _CALLBACK_ASK_RE = re.compile(
        r"\b(call (?:me )?(?:back|later|tomorrow|after|in)|callback|call karna|"
        r"phone karna|baad mein (?:call|baat)|baad me (?:call|baat)|"
        r"phir (?:call|baat)|dobara call)\b", re.IGNORECASE)
    # Devanagari / Gujarati request shapes (RUNBOOK §77). Every form above
    # is Latin, so "पांच मिनट में कॉल बैक कर सकते हैं", "बाद में कॉल कर सकते
    # हो?" and "कल ग्यारह बजे कॉल कर दीजिए" (calls 1061, 1073) were never a
    # callback: the first reached the model as `general` and the next
    # qualification question followed, the others classified `objection`
    # and got the canned deferral. Every alternative needs a REQUEST verb
    # form next to the call word, so a caller REPORTING a call ("कॉल आया
    # था", "क्यों कॉल किया आपने?"), describing one ("हमारी टीम कॉल करती
    # है") or refusing one ("मत करना कॉल") does not match. Script guards,
    # never `\b`, next to Indic text. Swept 2026-10-12 over 11,621 corpus
    # caller lines: 36 hits before, all Latin; 63 after — the 27 new ones
    # all Devanagari, 25 requests or deferrals, one "I will call you" and
    # one narration of a request, both tolerated; nothing lost.
    # tests/test_callback_indic.py pins both lists.
    _CB_NA = r"(?![\u0900-\u097F])"
    _CB_VERB = (r"(?:कर(?:ना|ो|ें|िए|िये|\s*दो|\s*दीजिए|\s*दीजिये|\s*दें|"
                r"\s*देना|\s*सकते|\s*सकती|\s*सकता|\s*लेना|\s*लीजिए|\s*लो)"
                r"|कीजिए|कीजिये|करिए|करिये|करें)")
    _CB_NOT_PAST = (r"(?!\s*(?:की\s*थी|किया|करा|आया|आयी|आई|हुई|हुआ|"
                    r"करती\s*(?:है|थी)|करता\s*(?:है|था)|करते\s*थे|करके|है"
                    + _CB_NA + r"))")
    # "कॉल करना है" is a statement, "मत कॉल करो" a refusal: not requests.
    _CB_NOT_STMT = (r"(?!\s*(?:है" + _CB_NA + r"|था|थी|पड़|मत|नहीं|नही|ना"
                    + _CB_NA + r"))")
    _CB_NOT_NEG_BEFORE = r"(?<!मत )(?<!नहीं )(?<!नही )(?<!ना )"
    _CB_WIN = (r"(?:(?!मत" + _CB_NA + r"|नहीं|नही|ना" + _CB_NA +
               r"|किया|करा|आया|आई|आयी|करती|करता|करते)\S+\s*){0,3}?")
    _CB_KAL = r"(?<![\u0900-\u097F])कल" + _CB_NA
    _CB_TIME = r"(?:मिनट|घंटे|घंटा|बजे|परसों|सुबह|शाम|दोपहर|" + _CB_KAL + r")"
    _CB_DAYPART = r"(?:परसों|सुबह|शाम|दोपहर|" + _CB_KAL + r")"
    _CALLBACK_ASK_INDIC_RE = re.compile("|".join([
        # "call back", any spelling
        r"कॉल\s*बैक|कॉलबैक|कॉल\s*बेक|फोन\s*बैक|કૉલ\s*બેક|કોલ\s*બેક",
        # later / again + call | talk — not a past "बाद में कॉल की थी"
        r"(?:बाद\s*में|बाद\s*मे|थोड़ी\s*देर\s*(?:बाद|में)|कुछ\s*देर\s*(?:बाद|में)|"
        r"दोबारा|दुबारा|फिर\s*से)\s*(?:मुझे\s*|हमें\s*|आप\s*)?(?:कॉल|फोन|फ़ोन|बात)"
        + _CB_NOT_PAST,
        # "(call) me": मुझे / हमें … कॉल + a request verb
        r"(?:मुझे|हमें|मेरे\s*को)\s*" + _CB_WIN + _CB_NOT_NEG_BEFORE
        + r"(?:कॉल|फोन|फ़ोन)\s*" + _CB_VERB + _CB_NOT_STMT,
        # a bare imperative — "कॉल कर दो", "कॉल कीजिए", "कॉल कर सकते हो" —
        # but not the statement "कॉल करना है"
        _CB_NOT_NEG_BEFORE + r"(?:कॉल|फोन|फ़ोन)\s*" + _CB_VERB + _CB_NOT_STMT,
        # a time + call: "कल ग्यारह बजे कॉल कर दीजिए", "10 मिनट बाद कॉल करना"
        _CB_TIME + r"\s*(?:के\s*बाद\s*|बाद\s*|में\s*|को\s*|तक\s*)?(?:ही\s*)?"
        r"(?:मुझे\s*|आप\s*)?" + _CB_NOT_NEG_BEFORE + r"(?:कॉल|फोन|फ़ोन)\s*"
        + _CB_VERB + _CB_NOT_STMT,
        # a time + talk: "कल सुबह बात करते हैं" — not "बात करते थे"
        _CB_DAYPART + r"\s*(?:को\s*|में\s*|बाद\s*|तक\s*)?(?:ही\s*)?बात\s*कर"
        r"(?:ते|ेंगे|ें|ना|ो|िए|िये|\s*लेंगे|\s*लेते)(?!\s*(?:थे|थी|था))",
        # Gujarati
        r"(?:મને|અમને)\s*(?:\S+\s*){0,3}?(?:કૉલ|કોલ|ફોન)\s*કર",
        r"પછી\s*(?:મને\s*)?(?:કૉલ|કોલ|ફોન|વાત)\s*કર",
    ]), re.IGNORECASE)

    # "I can't talk NOW" without any callback wording — a brush-off, not an
    # objection. Routed into the callback pin-down flow (ask when to call
    # back, confirm, close) instead of the decline strike counter, which
    # would keep pitching a caller who already said they're busy.
    # Devanagari/Gujarati alternatives sit outside \b groups (matras break
    # word boundaries — the Indic regex gotcha).
    _BUSY_NOW_RE = re.compile(
        r"\b(not free|not right now|busy right now|bit busy|i'?m busy|"
        r"can'?t talk|cannot talk|in a meeting|driving right now|"
        r"abhi busy|abhi free nahi|abhi time nahi|abhi fursat nahi|"
        r"fursat nahi|abhi baat nahi|abhi nahi kar|hamna nahi|"
        r"atyare nahi|atyare busy|atyare time nathi)\b"
        r"|अभी\s*(?:busy|व्यस्त|समय\s*नहीं|बात\s*नहीं)|फ़?ुरसत\s*नहीं"
        r"|અત્યારે\s*(?:નહીં|વ્યસ્ત|સમય\s*નથી)|હમણાં\s*નહીં",
        re.IGNORECASE)

    def _detect_callback_request(self, text: str) -> bool:
        """Deferral WITH a callback flavour (time preference or 'call me
        back'), or a busy-now brush-off — both mean 'reach me later'.

        The explicit forms are read first and unconditionally: "I missed your
        call, ring me back at five" is a callback request even though it also
        reports a received call. Only the WEAK branch — a stray time word
        near the word "call" — is withheld from a caller who is merely
        telling us they were called.
        """
        if self._CALLBACK_ASK_RE.search(text) \
                or self._CALLBACK_ASK_INDIC_RE.search(text):
            return True
        if self._BUSY_NOW_RE.search(text):
            return True
        if self._RECEIVED_CALL_RE.search(text):
            logger.info("[COMMITMENT] time word near 'call' ignored — the "
                        "caller is reporting a call they received, not "
                        "asking for one: %r", text[:60])
            return False
        return (bool(self._CALLBACK_TIME_RE.search(text))
                and bool(re.search(r"\b(call|baat|phone)\b", text,
                                   re.IGNORECASE)))

    # ── Close-permission confirm (platform rule, all agents) ────────────
    # The prompt doctrine makes every agent ask permission before wrapping
    # up ("Is there anything else I can help you with?"). This pair detects
    # that exchange deterministically: our last response asked the question,
    # the caller's short reply says they're done → close like a farewell.
    _CLOSE_PERMISSION_RE = re.compile(
        r"anything else|any other (?:question|help)|anything more|"
        r"aur (?:kuch|koi)|kuch aur|koi aur|और कुछ|कुछ और|और कोई|कोई और|"
        # "और किसी चीज़ में मदद" — the Pramukh owner's pre-close line (§75).
        # Bound to चीज़/बात so "और किसी दिन आ सकते हैं?" is not a close.
        r"और किसी\s*(?:चीज़|चीज|बात)|aur kisi\s*(?:cheez|chiz|cheej|baat)|"
        r"किसी और\s*(?:चीज़|चीज|बात)|kisi aur\s*(?:cheez|chiz|cheej|baat)|"
        r"biju kai|bija koi|kai biju|બીજું કંઈ|કંઈ બીજું|બીજો કોઈ",
        re.IGNORECASE)
    _DONE_REPLY_RE = re.compile(
        r"^(?:no+|nope|nahi+n?|nhi|na|bas|bs|नहीं|ना|बस|ના|બસ|"
        r"that'?s all|nothing(?: else| more)?|no,? than(?:ks|k you)|"
        r"than(?:ks|k you|k u|ku)|dhanyavaa?d|shukriya|aabhar|"
        r"धन्यवाद|शुक्रिया|आभार|આભાર|"
        r"i'?m (?:good|okay|fine)|all good|done|it'?s (?:okay|fine))"
        # Script-aware edge, not \b: "नहीं" ends in a vowel sign + anusvara
        # (not \w), so `नहीं\b` never matched and a Devanagari "no" to "और
        # कुछ?" was never a deterministic close (RUNBOOK §75).
        r"(?![\w\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])",
        re.IGNORECASE)

    # Unambiguous "I'm hanging up now" phrasings beyond the classic goodbye
    # words. Kept deliberately narrow — a phrase that can appear mid-pitch
    # ("take care of maintenance", "I have to go to Surat next week") must
    # NEVER be here; per-agent additions go in termination.farewell_patterns.
    _FAREWELL_EXTRA = (
        "gotta go", "got to go", "good night", "रखता हूँ", "रखता हूं",
        "रखती हूँ", "रखती हूं", "फोन रखो", "फोन रखिए", "rakhta hu",
        "rakhta hoon", "rakhti hu", "rakhti hoon", "phone rakh",
        "call rakh", "rakh raha hu", "rakh rahi hu", "muku chu",
        "phone muku", "મૂકું છું", "ફોન મૂક",
    )

    def _contains_farewell(self, text: str) -> bool:
        """Same farewell vocabulary as detect_intent — applied directly so a
        goodbye inside a longer utterance ('okay thank you, bye') closes the
        call even when the classifier scored the turn as something else.
        call_flow.termination.farewell_patterns adds per-agent phrases (any
        language/script, matched as lowercase substrings).

        The single-word vocabulary is additionally held to
        `_farewell_is_terminal`: a goodbye that ENDS the turn closes the call,
        a "bye" with an answer after it does not. Speech recognition turns
        ordinary words into "bye" (Signify → "bye", "by the time" → "bye at
        the time") and this used to interrupt live callers mid-sentence with
        "shall I end the call here?"."""
        lower = (text or "").lower()
        if (re.search(r"\b(bye|goodbye|alvida|aavjo)\b", lower)
                and _farewell_is_terminal(lower)):
            return True
        if any(w in lower for w in ("अलविदा", "फिर मिलते", "આવજો", "ફરી મળીશું")):
            return True
        if any(w in lower for w in self._FAREWELL_EXTRA):
            return True
        extra = [str(p).lower().strip() for p in
                 (self._termination_cfg().get("farewell_patterns") or [])]
        return any(p and p in lower for p in extra)

    def _termination_cfg(self) -> dict:
        """call_flow.termination with a safe empty default."""
        cfg = getattr(self._def.call_flow, "termination", None)
        return cfg if isinstance(cfg, dict) else {}

    def _pre_close_question(self) -> str:
        """`call_flow.termination.pre_close_question` — the owner's own
        "anything else before I go?" line, asked before every goodbye
        (RUNBOOK §75); '' when the agent has none (every other agent)."""
        q = self._termination_cfg().get("pre_close_question")
        if not isinstance(q, dict) or not q:
            return ""
        try:
            return self._lang_response(q, lang=self.state.language)
        except Exception:
            logger.exception("[CLOSE] pre_close_question failed (ignored)")
            return ""

    def _close_question_asked(self) -> bool:
        """Did one of the agent's last two lines already ask it?"""
        return any(self._CLOSE_PERMISSION_RE.search(l or "")
                   for l in self.memory.recent_assistant_texts(2))

    # ── Close-confirmation gate (platform rule, all agents) ─────────────
    # Speech is the least reliable input we have: on 2026-07-31 a live caller
    # saying "okay, by the way..." was transcribed "okay bye", the engine
    # closed instantly and the channel dropped while they were still talking.
    # Every speech-triggered close now asks ONE short question first and only
    # ends the call after the caller says yes. Deliberately NOT applied to
    # closes that are not a hearing judgement: DND / wrong-number (compliance
    # — re-asking someone who said "stop calling me" is worse than the risk),
    # silence hangup (nobody is there to answer) and the max-duration hard cap
    # (the platform is ending the call regardless).
    _CLOSE_CONFIRM_MAX = 2
    # Owner-specified copy (2026-08-01): short and casual — "okay then, shall
    # I cut the call?" — not a wordy "did I mishear?" apology. First-person
    # subjunctive (कर दूँ / કરી દઉં) on purpose: it is gender-neutral, agent
    # voices vary per language (same doctrine as _INTRO_LINES).
    _CLOSE_CONFIRM_Q = {
        "en": "Alright then, shall I end the call here?",
        "hi": "ठीक है, तो फिर मैं कॉल कट कर दूँ?",
        "gu": "સારું, તો પછી હું કૉલ કટ કરી દઉં?",
    }
    # Spoken after the caller confirms, right before the platform hangs up.
    _CLOSE_CONFIRMED = {
        "en": "Thank you for talking with me — have a wonderful day!",
        "hi": "बात करने के लिए धन्यवाद, आपका दिन शुभ हो।",
        "gu": "વાત કરવા બદલ આભાર, તમારો દિવસ શુભ રહે.",
    }

    def _close_confirm_question(self) -> str:
        """The one confirmation line, per language. response_bank
        localized_responses['close_confirm'] overrides it per agent."""
        lang = self._closing_language()
        return self._localized(
            "close_confirm",
            self._CLOSE_CONFIRM_Q.get((lang or "en")[:2],
                                      self._CLOSE_CONFIRM_Q["en"]),
            lang=lang)

    def _close_confirmed_line(self) -> str:
        """The thank-you spoken after a confirmed close, per language.
        Platform copy for every agent (owner-specified); response_bank
        localized_responses['close_confirmed'] overrides it per agent. The
        agent's closing_responses are deliberately NOT used here — they are
        full farewell speeches, and one may already have been spoken on the
        turn that triggered the confirmation."""
        lang = self._closing_language()
        return self._localized(
            "close_confirmed",
            self._CLOSE_CONFIRMED.get((lang or "en")[:2],
                                      self._CLOSE_CONFIRMED["en"]),
            lang=lang)

    def _maybe_close_confirm(self, reason: str) -> str | None:
        """Return the confirmation question when a close should be checked
        with the caller first, or None when it may proceed immediately."""
        if not bool(self._termination_cfg().get("confirm_before_end", True)):
            return None
        # Asked once and the caller answered: whatever they said, they have
        # now heard the question and the agent has already said its goodbye.
        # Asking again re-speaks that goodbye — the caller's own "कर दो" ("do
        # it") got the farewell repeated twice more on 2026-09-08. One check
        # protects against a mishearing; a second is a loop.
        if self._close_confirm_answered:
            logger.info("[HANGUP] close-confirm already answered once — "
                        "closing directly (%s)", reason)
            return None
        if self._close_confirm_count >= self._CLOSE_CONFIRM_MAX:
            logger.info("[HANGUP] close-confirm cap spent — closing directly "
                        "(%s)", reason)
            return None
        self._close_confirm_pending = True
        self._close_confirm_count += 1
        logger.info("[HANGUP] close-confirm asked (%s) — hangup deferred "
                    "until the caller confirms", reason)
        return self._close_confirm_question()

    def _is_close_confirm_yes(self, text: str) -> bool:
        """True only when the caller clearly agreed to end the call.

        Biased hard toward "keep the call alive": anything with content in it
        ("can you take my email address?") answers the question with a no by
        simply not being a yes, which is the safe direction to be wrong in."""
        t = (text or "").strip()
        if not t:
            return False
        _negated = re.search(
            r"\b(no|not|don'?t|do not|nahi+n?|nhi|na|mat|ના|નહીં|नहीं|ना|मत)\b",
            t, re.IGNORECASE)
        if _all_tokens_in(t, _NEG_TOKENS):
            return False
        # "yes, end it" / "haan rakh do" / "you can cut the call"
        if re.search(r"\b(end|cut|hang ?up|disconnect|khatam|khatm|band|"
                     r"rakh (?:do|de|dijiye)|puru kar|खत्म|बंद|रख|પૂરું|ખતમ)\b",
                     t, re.IGNORECASE) and not _negated:
            return True
        # A bare imperative "go ahead / do it" IS the answer to "shall I cut
        # the call?" — the question already named the action, so the caller
        # does not repeat it. Measured live 2026-09-08 (RUNBOOK §37): the
        # caller answered "कर दो, कर दो, कर दो, कर दो।" ("do it") four times
        # and the gate scored every one of them as "not a goodbye", so the
        # agent re-spoke its farewell twice more until the caller hung up.
        # Safe here and nowhere else: this test only ever runs on the turn
        # immediately after we asked the close question.
        if re.search(r"(?:^|[\s,])(?:कर\s*(?:दो|दीजिए|दीजिये|दे|दें)|"
                     r"કરી\s*(?:દો|દયો)|कट\s*कर|કટ\s*કર|"
                     r"kar\s*(?:do|dijiye|de)|kardo|kardijiye|"
                     r"go ahead|please do|do it|carry on)"
                     r"(?:[\s,.।॥!]|$)", t, re.IGNORECASE) and not _negated:
            return True
        if self._contains_farewell(t):
            return True
        # A bare all-affirmation turn ("yes", "haan ji", "ठीक है") is a yes.
        if _all_tokens_in(t, _AFFIRM_TOKENS):
            return True
        # ...as is a short turn that OPENS with one and adds nothing but
        # politeness ("yes please", "haan ji bilkul"). The length bar is what
        # keeps "yes but can you tell me the price first" out — those words
        # inside a real sentence are not an answer to this question.
        if "?" in t or _negated:
            return False
        toks = [w.strip(".,!;:।॥\"'") for w in t.lower().split()]
        toks = [w for w in toks if w]
        # An affirmation wrapped in politeness is still the affirmation:
        # "ji ji ji okay sir thank you" (call 934). Strip the politeness;
        # if what is left is non-empty and all yes-tokens, it is a yes.
        # "thank you" alone leaves nothing and stays a no; "okay thank
        # you but one question" leaves content words and stays a no.
        core = [w for w in toks if w not in _POLITE_TOKENS]
        if core and all(w in _AFFIRM_TOKENS for w in core):
            return True
        return bool(toks) and toks[0] in _AFFIRM_TOKENS and len(toks) <= 4

    def _stage_entry_line(self, text: str, intent: str,
                          stage_before: str) -> str:
        """`stages[].script.entry_line` of the stage a branch moved the flow
        to on this turn — "" when the stage did not change, has no entry
        line, or the caller asked something (a question goes to the model,
        whose goal carries the same line)."""
        if self.state.stage == stage_before or self.state.is_closing:
            return ""
        sd = self._stage_def()
        script = getattr(sd, "script", None) if sd else None
        el = script.get("entry_line") if isinstance(script, dict) else None
        if not isinstance(el, dict) or not el:
            return ""
        if "?" in (text or "") or intent in ("question", "farewell",
                                             "language_switch"):
            return ""
        try:
            line = self._personalize(
                str(self._lang_response(dict(el)) or "").strip(),
                f"stages.{self.state.stage}.script.entry_line")
        except Exception:
            logger.exception("[SCRIPT] entry line build failed")
            return ""
        return line

    def stt_recheck_slot(self, text: str) -> str:
        """RUNBOOK §81: the enum slot this caller turn answers when the
        agent opted in (`languages.stt_enum_recheck`), STT is locked to one
        language, and the transcript matches none of the slot's values —
        the turn worth one re-transcription without the language lock
        (call 1092: "Amenities" under hi-IN came back "हमें देखिए" and
        "नि टी"). "" otherwise."""
        langs = getattr(self._def, "languages", None)
        if (not getattr(langs, "stt_enum_recheck", False)
                or not getattr(langs, "stt_forced_language", "")):
            return ""
        t = (text or "").strip()
        if not t or "?" in t or len(t.split()) > 6:
            return ""
        sm = self.state_mgr
        noted = sm.last_noted_ask
        spec = sm.slots.get(noted) if noted else None
        if (spec is None or getattr(spec, "type", "") != "enum"
                or sm.state.get(noted) is not None
                or noted in getattr(sm, "retired", set())):
            return ""
        q = self._lang_response(dict(getattr(spec, "question", None) or {}),
                                lang=self.state.language)
        if not q or not self._line_asks(self.last_agent_response or "", q,
                                        slot=noted):
            return ""
        if self.stt_recheck_accepts(noted, t):
            return ""
        return noted

    def stt_recheck_accepts(self, slot: str, text: str) -> bool:
        """Does `text` name one of `slot`'s values?"""
        sm = self.state_mgr
        spec = sm.slots.get(slot)
        if spec is None:
            return False
        try:
            hit, _alias = sm._latest_alias(spec, (text or "").lower())
        except Exception:
            return False
        return hit is not None

    def _good_time_fastpath_line(self, text: str, intent: str) -> str:
        """The authored good-time line when this turn is a plain yes to the
        identity question the agent just asked; "" otherwise.

        Deliberately narrow: every token must be a yes / go-ahead / "I am
        speaking" word, and the agent's last line must contain the identity
        question (the onset intro, a dead-air re-ask, a presence re-ask). A
        question, a "no", a name, "who is this?" — anything with content —
        goes to the model as before."""
        if not GOOD_TIME_FASTPATH or self._good_time_spoken:
            return ""
        if self._connect_cfg().get("good_time_fastpath") is False:
            return ""
        if intent in ("negation", "objection", "farewell", "question",
                      "identity_question", "language_switch"):
            return ""
        ob = getattr(self._def, "outbound", None)
        if ob is None or getattr(self._def, "call_direction", "") != "outbound":
            return ""
        if not _is_identity_yes(text):
            return ""
        if not self._identity_question_just_asked():
            return ""
        return self._good_time_line()

    def _good_time_line(self) -> str:
        """The authored, personalised outbound.good_time_check_line; "" when
        the agent has none worth speaking."""
        ob = getattr(self._def, "outbound", None)
        if ob is None:
            return ""
        try:
            line = self._personalize(
                str(self._lang_response(ob.good_time_check_line) or "").strip(),
                "outbound.good_time_check_line")
        except Exception:
            logger.exception("[CONNECT] good-time line build failed")
            return ""
        return line if len(line) >= 10 else ""

    def _identity_question_just_asked(self) -> bool:
        """Was the agent's last line its own "am I speaking with X?" beat —
        the onset intro, a dead-air re-ask, a presence re-ask?

        Any language variant: the caller's reply may have flipped the
        response language away from the one the question was asked in. The
        re-asks drop the greeting ("माफ़ कीजिए… क्या मेरी बात…"), so the
        line's longest sentence — its question — is what is matched."""
        ob = getattr(self._def, "outbound", None)
        if ob is None:
            return False
        last = _norm_letters(self.last_agent_response or "")
        if not last:
            return False
        _variants = getattr(ob, "identity_confirm_line", None) or {}
        for _raw in (_variants.values() if isinstance(_variants, dict)
                     else [_variants]):
            try:
                icl = self._personalize(
                    str(_raw or ""), "outbound.identity_confirm_line")
            except Exception:
                continue
            _segs = [_norm_letters(x) for x in re.split(r"[!?।.]+", str(icl))]
            _core = max(_segs, key=len, default="")
            if len(_core) >= 10 and _core in last:
                return True
        return False

    # ── RUNBOOK §73: wrong-number re-confirmation ───────────────────────

    def _wrong_number_reconfirm_line(self) -> str:
        """The re-confirmation question, per agent (outbound.
        wrong_number_reconfirm_line) else the built-in, in the call's
        language. "" when the feature is off."""
        if not WRONG_NUMBER_RECONFIRM:
            return ""
        ob = getattr(self._def, "outbound", None)
        authored = getattr(ob, "wrong_number_reconfirm_line", None) if ob else None
        line = ""
        if isinstance(authored, dict) and authored:
            line = str(self._lang_response(authored) or "").strip()
        if not line:
            lang = (self.state.language or "en")[:2]
            line = _WRONG_NUMBER_RECONFIRM_DEFAULT.get(
                lang, _WRONG_NUMBER_RECONFIRM_DEFAULT["en"])
        return line

    def _wrong_number_close_line(self) -> str:
        ob = getattr(self._def, "outbound", None)
        _close_lang = self._closing_language()
        line = ""
        if ob is not None:
            line = str(self._lang_response(ob.wrong_number_close,
                                           lang=_close_lang) or "").strip()
        if not line:
            line = _WRONG_NUMBER_CLOSE_DEFAULT.get(
                (_close_lang or "en")[:2], _WRONG_NUMBER_CLOSE_DEFAULT["en"])
        return line

    def _wrong_number_pattern_hit(self, text: str) -> bool:
        """The agent's own wrong_number_patterns, plus the platform set."""
        lower = (text or "").lower()
        ob = getattr(self._def, "outbound", None)
        pats = [str(p).lower() for p in
                ((getattr(ob, "wrong_number_patterns", None) or []) if ob else [])]
        if any(p and p in lower for p in pats):
            return True
        return bool(_WRONG_NUMBER_RE.search(lower))

    def _reconfirm_verdict(self, text: str) -> str:
        """How the caller answered "is this the wrong number, or the right
        one?": 'me' (it is them / the number is right), 'other' (right
        number, the contact is someone else or not reachable now), 'wrong'
        (an acknowledgement, a yes, or a repeated no — the owner's rule:
        a positive or acknowledging answer confirms the wrong number),
        'unclear' (anything else — ask once more)."""
        t = (text or "").strip()
        lower = t.lower()
        if not lower:
            return "unclear"
        # The contact's own name in the reply — "haan, Vishal bol raha hoon".
        _name = (self.outbound_contact_name or "").strip().lower().split()
        if _name and len(_name[0]) >= 3 and _name[0] in lower:
            return "me"
        if _RIGHT_NUMBER_ME_RE.search(lower):
            return "me"
        if self._wrong_number_pattern_hit(lower):
            return "wrong"
        if _RIGHT_NUMBER_OTHER_RE.search(lower):
            return "other"
        if _is_identity_no(t):
            return "wrong"
        if _is_identity_yes(t) or _all_tokens_in(
                t, _AFFIRM_TOKENS | _POLITE_TOKENS | _NEG_TOKENS):
            return "wrong"
        return "unclear"

    # ── RUNBOOK §73: a declined site visit ──────────────────────────────

    def _last_line_invites_visit(self) -> bool:
        """Did the agent's previous line invite the caller to visit — in its
        own words, or with one of its visit-slot questions?"""
        last = (self.last_agent_response or "").strip()
        if not last:
            return False
        if _VISIT_INVITE_CUE_RE.search(last):
            return True
        norm_last = _norm_letters(last)
        for sname in ("site_visit_interest", "finalized_property"):
            spec = self.state_mgr.slots.get(sname)
            q = getattr(spec, "question", None) if spec else None
            qs = list(q.values()) if isinstance(q, dict) else ([q] if q else [])
            for raw in qs:
                core = _norm_letters(str(raw or ""))
                if len(core) >= 10 and core in norm_last:
                    return True
        return False

    def _detect_visit_decline(self, text: str) -> str:
        """'Maybe later' / 'No' when this turn declines a site visit, else ''.
        Strong forms stand alone; weak ones need the previous agent line to
        have invited a visit. A caller who ASKS something is not declining."""
        t = (text or "").strip()
        if not t:
            return ""
        if "?" in t and not _VISIT_DECLINE_STRONG_RE.search(t.lower()):
            return ""
        lower = t.lower()
        strong = bool(_VISIT_DECLINE_STRONG_RE.search(lower))
        if _VISIT_ALT_TIME_RE.search(lower):
            # "not Saturday, only after 5" — they are picking a time.
            return ""
        if not strong:
            if not self._last_line_invites_visit():
                return ""
            if _VISIT_ANSWER_FIRST_RE.search(lower):
                return "answer_first"
            toks = re.sub(r"[^0-9a-z'\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+",
                          " ", lower).split()
            bare_no = (0 < len(toks) <= 5
                       and all(w in _VISIT_BARE_NO_TOKENS for w in toks)
                       and any(w in _NEG_TOKENS for w in toks))
            if not bare_no and not _VISIT_WEAK_DEFER_RE.search(lower):
                return ""
            # A plain yes is the caller accepting the visit; a yes that
            # carries a "later" is still a later.
            if _has_strong_yes(t) and not _VISIT_LATER_CUE_RE.search(lower):
                return ""
        if _VISIT_LATER_CUE_RE.search(lower) or not strong:
            return "Maybe later"
        return "No"

    def _note_visit_declined(self, value: str, text: str) -> None:
        """Record the decline: the slot (when the agent collects one), the
        call record flag, and the turn the per-turn band dates from.
        'answer_first' records only the turn — one reply without an
        invitation, nothing on the record."""
        if value == "answer_first":
            self._visit_answer_first_turn = self.state.turn_count
            logger.info("[VISIT] caller wants the answer before any visit "
                        "talk: %r", (text or "")[:80])
            return
        first = self._visit_declined_turn < 0
        self._visit_declined_turn = self.state.turn_count
        sm = self.state_mgr
        sm.extra_flags["visit_declined"] = True
        sm.extra_flags["visit_declined_quote"] = (text or "")[:160]
        if "site_visit_interest" in sm.slots:
            cur = sm.state.get("site_visit_interest")
            # A hard no never softens back to "maybe later" by itself.
            if cur != "No" or value == "No":
                sm.set_externally("site_visit_interest", value, source="system")
        logger.info("[VISIT] caller declined a site visit (%s%s): %r",
                    value, "" if first else ", again", (text or "")[:80])

    def _booking_unconfirmed_reason(self) -> str:
        """"unusable" | "refused" on the turn a book_appointment was refused
        and on the turn after it; "" once a visit is fixed or it is stale."""
        br = self._booking_refused
        if not br:
            return ""
        cs = self.memory.call_state
        if cs.appointment_date and cs.appointment_time:
            return ""
        if self.state.turn_count - br[0] not in (0, 1):
            return ""
        return br[1]

    def _booking_unconfirmed_band(self) -> list[str]:
        from . import prompt_blocks as pb
        reason = self._booking_unconfirmed_reason()
        return pb.booking_unconfirmed_block(reason) if reason else []

    def _callback_pending_band(self) -> list[str]:
        """━━ CALLBACK REQUESTED ━━ (RUNBOOK §77): [] unless the caller has
        asked to be called back and the call is not closing yet — the turn
        after the one clarification question, or a branch-scheduled callback."""
        flags = self.state_mgr.extra_flags
        if not flags.get("callback_requested") or self.state.is_closing:
            return []
        from . import prompt_blocks as pb
        return pb.callback_pending_block(str(flags.get("callback_time") or ""))

    def _is_visit_negotiation(self, text: str) -> bool:
        """The caller is moving the visit to another day/time — a proposed
        day refused, another asked for — while a visit is on the table.
        Never a decline of the call, never a callback request."""
        t = (text or "").strip()
        if not t:
            return False
        low = t.lower()
        if (_CALL_LEVEL_DECLINE_RE.search(low)
                or _VISIT_DECLINE_STRONG_RE.search(low)
                or self._detect_callback_request(t)):
            return False
        last = (self.last_agent_response or "").lower()
        on_table = (self._stage_role() in ("scheduling", "confirmation")
                    or self._last_line_invites_visit()
                    or bool(_VISIT_ALT_TIME_RE.search(last)))
        if not on_table:
            return False
        return bool(_VISIT_ALT_TIME_RE.search(low)
                    or _VISIT_NEGOTIATION_CUE_RE.search(low))

    def _visit_negotiation_band(self) -> list[str]:
        if self._visit_negotiation_turn < 0 or \
                self.state.turn_count - self._visit_negotiation_turn > 1:
            return []
        from . import prompt_blocks as pb
        return pb.visit_negotiation_block(
            getattr(self, "_turn_caller_text", "") or "")

    def _project_focus_band(self) -> list[str]:
        """RUNBOOK §79: which project this turn is about — the one the caller
        just NAMED, else the ones the agent's previous line named. Call
        64629614: \"3 bhk, 4 bhk प्रमुख Vedanta में क्या-क्या मिलता है?\" was
        answered with Green County's amenities, and \"दोनों का carpet
        area\" (the two projects just offered) with Vedanta's."""
        kb = self._def.knowledge_base
        items = [i for i in (getattr(kb, "catalog", None) or [])
                 if isinstance(i, dict) and i.get("name")]
        if not items:
            return []
        try:
            from . import pitch_switch as _ps
            text = getattr(self, "_turn_caller_text", "") or ""
            named = [i["name"] for i in items if _ps.mentions(text, i)]
            last = self.last_agent_response or ""
            discussed = [i["name"] for i in items if _ps.mentions(last, i)]
        except Exception:
            return []
        from . import prompt_blocks as pb
        return pb.project_focus_block(named, discussed)

    def _visit_declined_band(self) -> list[str]:
        if self._visit_declined_turn < 0:
            if self._visit_answer_first_turn == self.state.turn_count:
                try:
                    from . import prompt_blocks as pb
                    return pb.visit_declined_block("", 0, answer_first=True)
                except Exception:
                    logger.exception("[VISIT] band build failed")
            return []
        try:
            from . import prompt_blocks as pb
            project = ""
            if self._share is not None:
                from . import share_tracker as _st
                _pid = self._share.topic_id or self._share_pitch_id()
                project = str(_st.name_of(self._def.knowledge_base, _pid) or "") \
                    if _pid else ""
            if not project:
                cs = self.memory.call_state
                project = str(getattr(cs, "selected_project", None)
                              or getattr(cs, "project", None) or "")
            return pb.visit_declined_block(
                project, self.state.turn_count - self._visit_declined_turn,
                whatsapp_tracked=self._share is not None)
        except Exception:
            logger.exception("[VISIT] band build failed")
            return []

    def _identity_unconfirmed_band(self) -> list[str]:
        if not self._identity_other:
            return []
        try:
            from . import prompt_blocks as pb
            return pb.identity_unconfirmed_block(
                self.outbound_contact_name or "")
        except Exception:
            logger.exception("[IDENTITY] band build failed")
            return []

    def language_allowed(self, code: str) -> bool:
        """May this call be carried into `code`? Exactly the agent's own
        languages — what the pipeline's STT mirror must respect too."""
        return str(code or "")[:2].lower() in self._switchable_languages()

    def _connect_cfg(self) -> dict:
        """call_flow.connect with a safe empty default."""
        cfg = getattr(self._def.call_flow, "connect", None)
        return cfg if isinstance(cfg, dict) else {}

    def _turn_taking_cfg(self) -> dict:
        """call_flow.turn_taking with a safe empty default (RUNBOOK §78)."""
        cfg = getattr(self._def.call_flow, "turn_taking", None)
        return cfg if isinstance(cfg, dict) else {}

    @staticmethod
    def _is_bare_ack_text(text: str) -> bool:
        """Nothing but an acknowledgement — "हाँ", "जी हाँ", "हम्म", "okay
        sir", "ठीक है": what a listener says WHILE the other person talks.
        Never a negative, never more than four tokens."""
        toks = re.sub(r"[^0-9a-zऀ-ॣ०-ॿ઀-૿]+", " ",
                      (text or "").lower()).split()
        if not toks or len(toks) > 4:
            return False
        if any(t in _NEG_TOKENS for t in toks):
            return False
        if not any(t in _ACK_CORE for t in toks):
            return False
        return all(t in _ACK_CORE or t in _ACK_FILLER for t in toks)

    # Identity-composed connect intro, per language. First-person forms are
    # gender-neutral on purpose — agent voices vary per language. Separate
    # with/without-agent shapes: "This is  from X" / "ये X से का कॉल है"
    # read broken when the agent name is empty or IS the company name.
    _INTRO_LINES = {
        ("en", True): "Hello! This is {agent} from {company}.",
        ("en", False): "Hello! This is {company}.",
        ("hi", True): "नमस्ते! मैं {agent}, {company} से।",
        ("hi", False): "नमस्ते! ये {company} की ओर से कॉल है।",
        ("gu", True): "નમસ્તે! હું {agent}, {company} તરફથી.",
        ("gu", False): "નમસ્તે! આ {company} તરફથી કૉલ છે.",
    }
    _INTRO_TAIL_INBOUND = {
        "en": "How may I help you today?",
        "hi": "बताइए, मैं आपकी क्या मदद करूँ?",
        "gu": "બોલો, હું તમારી શું મદદ કરું?",
    }
    _INTRO_TAIL_OUTBOUND = {
        "en": "Can you hear me clearly?",
        "hi": "क्या मेरी आवाज़ साफ़ आ रही है?",
        "gu": "શું મારો અવાજ બરાબર આવે છે?",
    }

    def _identity_answer(self, user_text: str = "") -> str:
        """Deterministic answer to "who is this / which company is calling?".

        Built from the SAME localized table as the connect intro, in the
        caller's language and shaped by direction. The old fallback was a
        hardcoded English sentence — "This is {agent} from {company}. How can
        I help you?" — which on a Hindi OUTBOUND call was wrong three ways at
        once (live 2026-09-08, RUNBOOK §37): English mid-Hindi-call, an
        inbound "how can I help you?" offered by the side that placed the
        call, and no return to the question the agent had just asked, so the
        script stalled and the model improvised the next turn.
        """
        lang = (self.state.language or "en")[:2]
        agent = (self._def.identity.agent_name or "").strip()
        company = (self._def.identity.company_name or "").strip()
        tmpl = self._INTRO_LINES.get(
            (lang, bool(agent)), self._INTRO_LINES[("en", bool(agent))])
        # The intro table opens with a greeting; answering "who is this?" with
        # "Hello!" is a beat out of place mid-call.
        line = tmpl.format(agent=agent, company=company)
        line = re.sub(r"^(?:Hello!|नमस्ते!|નમસ્તે!)\s*", "", line).strip()
        if not line:
            line = f"This is {agent or company}."
        if self.call_direction == "outbound":
            # We called them: hand the turn back to our own last question
            # rather than asking what they need — unless the caller has
            # ALREADY answered it. "hello, this is Piyush Gupta. Who is
            # this?" got "This is Shubhangi from Dhron AI. Am I speaking to
            # Piyush Gupta?" on three live calls today (874, 875, 869): the
            # caller had just said so, in the same breath as the question.
            _q = self._last_question_asked()
            if _q and self._is_identity_question(_q) and \
                    self._identity_already_given(user_text):
                logger.info("[IDENTITY] caller already identified themselves "
                            "— not re-asking %r", _q[:50])
                _q = ""
            return f"{line} {_q}".strip() if _q else line
        return (f"{line} " + self._INTRO_TAIL_INBOUND.get(
            lang, self._INTRO_TAIL_INBOUND["en"])).strip()

    # "this is Piyush", "yes, speaking", "मैं ही बोल रहा हूँ" — the caller
    # naming or confirming themselves, in the same turn as "who is this?".
    _SELF_ID_RE = re.compile(
        r"\b(?:this is|it'?s me|speaking|yes,?\s*(?:i am|i'?m|you are|"
        r"that'?s me|this is))\b"
        r"|बोल\s*रह[ाी]|मैं\s*ही|હું\s*જ|બોલું\s*છું",
        re.IGNORECASE)

    def _identity_already_given(self, text: str) -> bool:
        """Did the caller just tell us who they are?"""
        t = (text or "").strip()
        if not t:
            return False
        if self._SELF_ID_RE.search(t):
            return True
        name = (self.outbound_contact_name or "").strip().lower()
        first = name.split()[0] if name else ""
        # Three characters is the bar a real given name clears; shorter
        # fragments match inside ordinary words.
        return len(first) >= 3 and first in t.lower()

    def _is_identity_question(self, question: str) -> bool:
        """Is `question` this agent's own 'am I speaking to X?' beat?"""
        q = re.sub(r"\W+", " ", (question or "").lower()).strip()
        if not q:
            return False
        for raw in (self._def.outbound.identity_confirm_line,
                    (self._connect_cfg() or {}).get("intro_line")):
            authored = self._personalize(
                str(self._lang_response(raw) or ""), "identity_confirm_line")
            authored = re.sub(r"\W+", " ", authored.lower()).strip()
            if authored and (authored in q or q in authored):
                return True
        return False

    def tts_language_for(self, text: str) -> str:
        """Language code the TTS should speak `text` in — decided by the text
        itself, not the conversation state. Mislabeling English as hi-IN
        pushes it through Sarvam's Hindi pronunciation frontend at the Hindi
        pace factor — the measured "rural accent" incident
        (deploy/PLAN-tts-accent-consistency.md). Script wins outright; Latin
        text follows the conversation language only when it carries romanized
        Hindi/Gujarati markers (fillers, Hinglish openers)."""
        t = (text or "").strip().lower()
        if not t:
            return self.state.language or "en"
        if any("઀" <= c <= "૿" for c in t):
            return "gu"
        if any("ऀ" <= c <= "ॿ" for c in t):
            # Code-mixed Devanagari+Latin stays hi-IN: Sarvam speaks embedded
            # English words natively; Devanagari under en-IN does not work.
            return "gu" if self._has_gujarati_markers_devanagari(t) else "hi"
        if self.state.language in ("hi", "gu") and (
                _pattern_score(t, _HINDI_PATTERNS) > 0
                or _pattern_score(t, _GUJARATI_PATTERNS) > 0
                # Hinglish function words — see _ROMAN_INDIC_TTS_MARKERS.
                or _roman_indic_marker_score(t) > 0):
            return self.state.language
        return "en"

    def stt_mirror_consistent(self, short: str, text: str) -> bool:
        """Whether Sarvam's per-segment language claim is plausible for the
        transcript it arrived with. Gates the pipeline's state mirror:
        acoustic misdetection (Indian-accented English → hi-IN, bare "hello"
        → anything) must not flip the agent's language, and a lone "okay"
        from a Hindi speaker is not an English switch — same ≥2-word bar as
        the conversation-side switch threshold."""
        t = (text or "").strip().lower()
        if not t:
            return False
        has_guj = any("઀" <= c <= "૿" for c in t)
        has_dev = any("ऀ" <= c <= "ॿ" for c in t)
        if short == "gu":
            return (has_guj
                    or (has_dev and self._has_gujarati_markers_devanagari(t))
                    or _pattern_score(t, _GUJARATI_PATTERNS) > 0)
        if short == "hi":
            return has_dev or _pattern_score(t, _HINDI_PATTERNS) > 0
        if short == "en":
            # A3 hardening (2026-07-30): pure-Latin + multi-word is not
            # enough — the name "alvin anthony" passed and flipped a Hindi
            # call to English mid-conversation. An en claim must carry at
            # least one English FUNCTION word; names/brands carry none.
            words = [w.strip(_SCORER_STRIP_CHARS) for w in t.split()]
            return (not has_dev and not has_guj
                    and len(words) >= 2
                    and any(w in _EN_FUNCTION_WORDS for w in words))
        return False

    # ------------------------------------------------------------------
    # B2/B3: deterministic contact capture (2026-07-30)
    # ------------------------------------------------------------------

    def _refresh_capture_slot(self) -> None:
        """Arm capture mode when the engine is collecting a contact-typed
        slot: either its confirmation is pending, or it is the FIRST missing
        slot and the ask instruction has already been emitted. Agents with
        no contact-typed slots never arm — zero behavior change for them."""
        slot: str | None = None
        if CAPTURE_MODE_ENV:
            try:
                # C1: an ask the ENGINE detected in the agent's own utterance
                # arms capture even when the scheduler never emitted the ask
                # instruction (LLM/stage-goal asks — the live norm). Cleared
                # once the slot is resolved (filled-and-confirmed or skipped).
                asked = self._capture_asked_slot
                if asked is not None:
                    spec_a = self.state_mgr.slots.get(asked)
                    resolved = (
                        spec_a is None
                        or asked in self.state_mgr.skipped
                        or (self.state_mgr.state.get(asked) is not None
                            and asked not in self.state_mgr._pending_fields))
                    if resolved:
                        self._capture_asked_slot = asked = None
                gaps = self.state_mgr.detect_missing_slots()
                for n in gaps["pending"]:
                    spec = self.state_mgr.slots.get(n)
                    if spec is not None and spec.type in ("email", "phone"):
                        slot = n
                        break
                if slot is None and asked is not None:
                    slot = asked
                if slot is None and gaps["missing"]:
                    n = gaps["missing"][0]
                    spec = self.state_mgr.slots.get(n)
                    if (spec is not None and spec.type in ("email", "phone")
                            and self.state_mgr.asked_count.get(n, 0) >= 1):
                        slot = n
            except Exception:
                slot = None
        if slot != self._capture_slot:
            logger.info("[CAPTURE] %s",
                        f"armed for slot {slot!r}" if slot else "released")
            self._capture_buf.clear()
        self._capture_slot = slot

    def capture_expecting_input(self) -> bool:
        """C4: the pipeline's capture affordances (2s merge window, follow-on
        loop, en-IN STT, raised barge threshold) apply only while the
        engine's LAST utterance actually solicited spelled input or a yes/no
        — an armed-but-digressing turn (Zoom, booking) pays normal latency."""
        return bool(self._capture_slot) and (
            self._capture_awaiting_payload or self._capture_awaiting_confirm
            or bool(self._capture_buf))

    def _capture_line(self, bank: dict[str, str]) -> str:
        lang = self.state.language if self.state.language in ("en", "hi", "gu") \
            else "en"
        return bank.get(lang) or bank["en"]

    async def _handle_capture_turn(self, text: str, intent: str,
                                   cancel_llm) -> "AsyncIterator[EngineResult]":
        """Deterministic handling of one turn while an email capture is
        armed. Yields nothing at all when the turn is unrelated to the
        capture — the caller then falls through to normal routing. Fully
        handles: payload assembly, silent buffering of partial spellings,
        read-back, yes/no confirmation, corrections, and the re-ask cap
        (→ `<slot>_unresolved` flag, never a stored guess).

        Phone-typed slots arm capture (endpointing/STT effects) but their
        turns route normally — the existing phone regex + digit read-back
        machinery already handles digits well.
        """
        from .contact_capture import (looks_like_partial_email,
                                      normalize_spoken_email,
                                      spelled_readback)
        slot = self._capture_slot
        spec = self.state_mgr.slots.get(slot)
        if spec is None or spec.type != "email":
            return
        pending = slot in self.state_mgr._pending_fields
        now = time.time()

        # Stale buffer: fragments older than 30s belong to an abandoned
        # attempt, not this turn.
        if self._capture_buf and now - self._capture_buf_ts > 30.0:
            self._capture_buf.clear()

        joined = " ".join(self._capture_buf + [text])
        candidate = (normalize_spoken_email(joined)
                     or normalize_spoken_email(text))

        # (a) Confirmation resolution for a pending value. A turn carrying a
        # parseable email (new OR restated — the typed extractor may have
        # pre-filled the slot from this same utterance) goes to read-back.
        # A bare yes/no resolves — but ONLY when the engine's own last
        # utterance was the read-back (C3): a "नहीं नहीं" answering "any
        # more questions?" six turns later once DISCARDED a good email.
        # "Bare" is token-based (C3): every word an affirmation/negation
        # word — an emphatic "हाँ सही है सही है।" (5 tokens) must confirm,
        # while "okay tell me more" must not.
        _bare_yes = ((intent == "affirmation" and _short_turn(text))
                     or _all_tokens_in(text, _AFFIRM_TOKENS))
        _bare_no = ((intent == "negation" and _short_turn(text))
                    or _all_tokens_in(text, _NEG_TOKENS))
        _confirm_ctx = self._capture_awaiting_confirm
        if pending and candidate:
            pass          # fall through to (b) — read back the value
        elif pending and _confirm_ctx and _bare_yes and not _bare_no:
            cancel_llm()
            val = self.state_mgr.state.get(slot)
            self.state_mgr.set_externally(slot, val, source="system")
            self._capture_buf.clear()
            logger.info("[CAPTURE] %s confirmed: %r", slot, val)
            resp = self._capture_line(_CAPTURE_CONFIRMED)
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            yield EngineResult(text=resp, is_shortcut=True,
                               intent="capture_confirmed")
            return
        elif pending and _confirm_ctx and _bare_no:
            cancel_llm()
            self.state_mgr.discard_pending(slot)
            logger.info("[CAPTURE] %s rejected by caller — re-asking", slot)
            async for r in self._capture_reask_or_giveup(slot):
                yield r
            return
        elif pending and self._is_capture_progress_query(text):
            # "नोट कर लिया आपने?" — answer from state, never the LLM.
            cancel_llm()
            resp = self._capture_line(_CAPTURE_PROGRESS_PENDING).format(
                spelled=spelled_readback(self.state_mgr.state.get(slot) or ""))
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            self._capture_awaiting_confirm = True
            yield EngineResult(text=resp, is_shortcut=True,
                               intent="capture_readback")
            return
        elif pending:
            # Neither yes/no in read-back context nor a new value — let the
            # LLM handle the digression; the pending confirm instruction
            # stays in the prompt context.
            return

        # (b) Payload turn.
        if candidate:
            cancel_llm()
            self._capture_buf.clear()
            old = self.state_mgr.state.get(slot)
            self.state_mgr.state[slot] = candidate
            self.state_mgr.mark_pending(slot)
            self.state_mgr.update_audit_log(
                slot, old, candidate, "capture", text)
            logger.info("[CAPTURE] %s candidate %r → read-back", slot,
                        candidate)
            resp = self._capture_line(_CAPTURE_READBACK).format(
                spelled=spelled_readback(candidate))
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            self._capture_awaiting_confirm = True   # C3: bare yes/no is live
            yield EngineResult(text=resp, is_shortcut=True,
                               intent="capture_readback")
            return

        # (c) Partial spelling — buffer silently and let the caller finish.
        # The pipeline's stretched merge window catches most fragments; this
        # catches the ones that still arrive as separate turns.
        if looks_like_partial_email(text):
            cancel_llm()
            self._capture_buf.append(text)
            self._capture_buf_ts = now
            self._capture_awaiting_payload = True   # C4: still spelling
            if len(self._capture_buf) >= 4:
                # Enough fragments that assembly should have worked — the
                # attempt is garbled beyond recovery. Re-ask or give up.
                self._capture_buf.clear()
                async for r in self._capture_reask_or_giveup(slot):
                    yield r
                return
            logger.info("[CAPTURE] buffering fragment %r (%d buffered)",
                        text[:40], len(self._capture_buf))
            # Yield nothing: the agent stays silent while the caller spells.
            return

        # (c2) Progress query with only a partial buffer ("नोट कर लिया?")
        # — acknowledge deterministically and ask them to continue.
        if self._capture_buf and self._is_capture_progress_query(text):
            cancel_llm()
            resp = self._capture_line(_CAPTURE_PROGRESS_PARTIAL)
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            self._capture_awaiting_payload = True
            yield EngineResult(text=resp, is_shortcut=True,
                               intent="capture_reask")
            return

        # (d) Unrelated turn — normal routing continues, capture stays armed.
        return

    _PROGRESS_QUERY_RE = re.compile(
        r"(?:नोट|note|लिख|likha?|नोंध|written|got)", re.IGNORECASE)

    def _is_capture_progress_query(self, text: str) -> bool:
        """'नोट कर लिया आपने?' / 'कितना नोट किया?' — the caller is asking
        what the agent has captured so far."""
        return (bool(self._PROGRESS_QUERY_RE.search(text))
                and ("?" in text or len(text.split()) <= 6))

    def _scrub_llm_email(self, text: str) -> str:
        """C2: while an email capture is armed the LLM may not SPEAK an
        address — on the live call it read back four different assemblies
        it could not write, and the caller confirmed a value state never
        held. Any email in LLM output is replaced with the spelled read-back
        of the engine's pending value (making the LLM's own confirm question
        point at state truth), or a generic reference when nothing is
        captured yet."""
        if not text or self._capture_slot is None:
            return text
        if not _EMAIL_ANY_RE.search(text):
            return text
        from .contact_capture import spelled_readback
        slot = self._capture_slot
        val = self.state_mgr.state.get(slot)
        if val:
            repl = spelled_readback(val)
        else:
            repl = _EMAIL_GENERIC.get(self.state.language,
                                      _EMAIL_GENERIC["en"])
        scrubbed = _EMAIL_ANY_RE.sub(repl, text)
        logger.info("[CAPTURE] LLM spoke an email while capture armed — "
                    "replaced with %s", "state read-back" if val else
                    "generic reference")
        return scrubbed

    async def _capture_reask_or_giveup(self, slot: str):
        """One more deterministic ask, or — past the cap — mark the slot
        unresolved (skipped + flagged for follow-up) and move on. Never
        stores a low-confidence guess."""
        spec = self.state_mgr.slots.get(slot)
        cap = getattr(spec, "max_asks", 0) or 3
        self._capture_reasks = getattr(self, "_capture_reasks", {})
        self._capture_reasks[slot] = self._capture_reasks.get(slot, 0) + 1
        if self._capture_reasks[slot] >= cap and slot not in self._capture_fallback_said:
            self._capture_fallback_said.add(slot)
            self.state_mgr.discard_pending(slot)
            self.state_mgr.skipped.add(slot)
            self.state_mgr.extra_flags[f"{slot}_unresolved"] = True
            self._capture_slot = None
            self._capture_asked_slot = None
            logger.info("[CAPTURE] %s unresolved after %d attempts — "
                        "flagged for follow-up", slot,
                        self._capture_reasks[slot])
            resp = self._capture_line(_CAPTURE_UNRESOLVED)
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
        elif slot in self._capture_fallback_said:
            return
        else:
            resp = self._capture_line(_CAPTURE_REASK)
            self.memory.add_assistant(resp, language=self.state.language)
            self._store_last_response(resp)
            self._capture_awaiting_payload = True   # C4: spelling expected
        yield EngineResult(text=resp, is_shortcut=True,
                           intent="capture_reask")

    def _connect_intro_configured(self) -> str:
        """The business's own word-for-word second line, if it set one.

        `call_flow.connect.intro_line` is the answer to the request the
        platform could not honour before 2026-08-04: "say Hello, wait for
        them to answer, THEN say <exact sentence>". Everything the caller
        hears at the start of a call is now settable — the first line via
        response_bank/outbound.opening_greeting, this one for the second
        beat. Accepts a per-language map or a bare string.
        """
        raw = self._connect_cfg().get("intro_line")
        if isinstance(raw, str):
            raw = {"en": raw}
        if not isinstance(raw, dict) or not raw:
            return ""
        line = self._personalize(
            str(self._lang_response(raw) or "").strip(),
            "call_flow.connect.intro_line")
        # A configured intro that is itself a bare hello would re-open the
        # exact "Hello" ↔ "Hello" parrot loop the fast path exists to kill.
        if len(line) < 2 or self._is_greeting_only_text(line):
            return ""
        return line

    def connect_intro_line(self) -> str:
        """Full identity intro for the connect hello fast path (and for the
        dead-air reprompt when the last utterance was a bare hello). Prefers
        the business's configured intro_line, then the configured opening
        when it is a real line; otherwise composes one from identity so the
        protection never depends on clean agent config."""
        # Tier 0: the owner dictated this line. It outranks everything —
        # including response_bank.opening_greeting, which is LIVE on agents
        # whose opening is long (that tier would otherwise shadow it exactly
        # the way stage goals shadowed dictated speech before).
        configured = self._connect_intro_configured()
        if configured:
            logger.info("[CONNECT] intro_line from config (lang=%s): %r",
                        self.state.language, configured[:60])
            return configured
        last = (self.last_agent_response or "").strip()
        if last and not self._is_greeting_only_text(last):
            return last
        # Tier 1.5: on OUTBOUND calls the business has usually already
        # written this exact beat — `outbound.identity_confirm_line` ("Hope
        # you're having a very good {current_time}, just wanted to make sure
        # if I am speaking to {contact_name}"). Until 2026-08-04 the engine
        # only ever fed it to the LLM as prompt context
        # (prompt_blocks.py:335), so the caller heard the generic identity
        # template here and the owner's line arrived a turn late, if at all
        # — precisely the live 2026-08-04 report. Speak the words they
        # actually wrote.
        if getattr(self._def, "call_direction", "") == "outbound":
            _icl = self._personalize(
                str(self._lang_response(
                    self._def.outbound.identity_confirm_line) or "").strip(),
                "outbound.identity_confirm_line")
            if len(_icl) >= 10 and not self._is_greeting_only_text(_icl):
                logger.info("[CONNECT] identity_confirm_line (lang=%s): %r",
                            self.state.language, _icl[:60])
                return _icl
        # Tier 2: the agent's OWN authored opening, from the field this
        # DIRECTION actually reads. Two bugs lived here:
        #   • it read response_bank.opening_greeting even on outbound agents,
        #     so an outbound business's own words were skipped;
        #   • a line containing {contact_name} was REJECTED outright ("{" not
        #     in opening) instead of being personalized — so exactly the
        #     businesses that wrote the most tailored opening got the generic
        #     platform template.
        opening = ""
        if getattr(self._def, "call_direction", "") == "outbound":
            opening = str(self._lang_response(
                self._def.outbound.opening_greeting) or "").strip()
        if not opening:
            opening = (self._def.response_bank.opening_greeting or "").strip()
        opening = self._personalize(opening, "opening_greeting")
        if len(opening) >= 15 and not self._is_greeting_only_text(opening):
            return opening
        # Nothing authored for this beat — the platform composes one from
        # identity. This is the "every agent says the same thing" line; the
        # cure is call_flow.connect.intro_line, and the Copilot lint tells
        # the business so before a caller ever hears it.
        logger.info("[CONNECT] no authored second line — composing from "
                    "identity (set call_flow.connect.intro_line to control "
                    "this wording)")
        return self._composed_intro_line()

    def _composed_intro_line(self) -> str:
        """The platform's own identity intro, built from identity.agent_name +
        identity.company_name in the call's language. The floor under every
        opening: a greeting that asked only "am I speaking with
        {contact_name}?" has nothing left to say for a contact with no name."""
        ident = self._def.identity
        agent = (ident.agent_name or "").strip()
        company = (ident.company_name or "").strip() or agent or "our team"
        lang = self.state.language if self.state.language in ("en", "hi", "gu") \
            else "en"
        has_agent = bool(agent) and agent.lower() not in company.lower()
        line = self._INTRO_LINES[(lang, has_agent)].format(
            agent=agent, company=company)
        tail = (self._INTRO_TAIL_OUTBOUND
                if getattr(self._def, "call_direction", "") == "outbound"
                else self._INTRO_TAIL_INBOUND)[lang]
        return f"{line} {tail}"

    def _recent_caller_texts(self, n: int = 4) -> list[str]:
        """The caller's last `n` utterances, newest first."""
        out: list[str] = []
        try:
            for turn in reversed(self.memory.turns):
                if getattr(turn, "role", "") == "user":
                    out.append(getattr(turn, "text", "") or "")
                    if len(out) >= n:
                        break
        except Exception:
            pass
        return out

    def _booking_hour_was_said(self, tm: str) -> bool:
        """Did the CALLER give this clock hour — in their own last few turns,
        or by agreeing to an hour the agent had put to them? A tool time with
        no hour at all is left to _is_bookable_when."""
        hours = _hours_of(tm)
        if not hours:
            return True
        callers = self._recent_caller_texts(4)
        if not callers:
            return True          # nothing said yet — nothing to check against
        if any(_hour_said_in(t, hours) for t in callers):
            return True
        last = (callers[0] if callers else "").lower()
        toks = set(re.sub(r"[^0-9a-z'\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF]+",
                          " ", last).split())
        if toks & _YES_TOKENS_FOR_HOUR:
            try:
                agent_lines = self.memory.recent_assistant_texts(2)
            except Exception:
                agent_lines = []
            if any(_hour_said_in(t, hours) for t in agent_lines):
                return True
        return False

    def _last_caller_text(self) -> str:
        """The caller's most recent utterance, from the turn history."""
        try:
            for turn in reversed(self.memory.turns):
                if getattr(turn, "role", "") == "user":
                    return getattr(turn, "text", "") or ""
        except Exception:
            pass
        return ""

    def _drop_yes_no_from_question_turn(self, data: dict, text: str,
                                        where: str) -> dict:
        """A caller's QUESTION never answers a yes/no or closed-choice slot.

        Live 2026-09-26 (call 971): asked whether he would move out of
        Lucknow, a Delhi resident said "बट मैं रहता तो दिल्ली में हूँ, लखनऊ
        कहाँ से आ गया?" — and relocate_outside_lucknow was stored as 'No'.
        He had asked what we meant; he had not declined. Free-text slots keep
        whatever the question carries ("Lucknow? I live in Delhi" is still a
        city). A yes/no that LEADS the sentence stays ("हाँ, क्यों नहीं?")."""
        if not data or not text:
            return data
        asks = ("?" in text or "？" in text
                or getattr(self.state, "last_intent", "") == "question")
        if not asks:
            return data
        toks = _ack_tokens(text)
        if toks and (toks[0] in _AFFIRM_TOKENS or toks[0] in _NEG_TOKENS):
            return data
        kept: dict = {}
        dropped: list[str] = []
        for k, v in data.items():
            spec = self.state_mgr.slots.get(k)
            if spec is not None and getattr(spec, "type", "") in ("boolean", "enum"):
                dropped.append(f"{k}={v!r}")
                continue
            kept[k] = v
        if dropped:
            logger.info("[STATE] %s: the caller asked a question, they did not "
                        "answer one — ignoring %s from %r",
                        where, ", ".join(dropped), text[:60])
        return kept

    def _reconcile_ask_budget(self) -> None:
        """The prompt directive spends a slot's ask budget when it SUGGESTS
        the question; the agent's spoken line is what actually asked. When
        the line demonstrably asked about a different slot, give the
        suggested one its ask back (call 971: issues_faced retired unvoiced
        while the model asked about the Mahotsav experience instead)."""
        sm = self.state_mgr
        noted = sm.last_noted_ask
        if not noted or sm._asked_turn.get(noted) != sm.turn_index:
            return
        # The QA tracker matches the slot's OWN question text (RUNBOOK §75):
        # it is the precise reading. The keyword matcher (_note_ask_signals)
        # is a label/slot-name scan — on an authored Hindi line it credited
        # "…3 और 4 BHK homes… investment के लिए?" to investment_horizon (the
        # word "investment" in that slot's label) and un-noted the purpose
        # ask the line had just voiced, so the caller's answer was refused
        # as "not asked". The precise reading wins; a line the tracker says
        # asked the noted slot never loses its ask.
        qa = getattr(self.memory.qa_tracker, "last_asked_field", None)
        if qa and qa == noted:
            return
        # Two slots can share a signal ("सबसे ज़रूरी": decision_factor and
        # the legacy amenity_priorities). When the line carries the NOTED
        # slot's signals too, the directive's reading wins the tie: the ask
        # stands and the answer is taken (call 1074: "कंस्ट्रक्शन क्वालिटी"
        # was refused as "not asked" one second after the line asked it).
        try:
            line = getattr(self, "last_agent_response", "") or ""
            if self.memory.qa_tracker.asks_field(noted, line):
                self.memory.qa_tracker.last_asked_field = noted
                return
        except Exception:
            pass
        actually = qa or self._last_asked_slot
        if actually and actually != noted and actually in sm.slots:
            sm.unnote_asked(noted)

    def _scrub_hello_values(self, data: dict) -> dict:
        """Drop extracted slot values that are just hello noise — a caller's
        repeated 'hello hello' must never become their recorded name."""
        clean = {k: v for k, v in data.items()
                 if not self._is_bare_hello_text(str(v))}
        dropped = set(data) - set(clean)
        if dropped:
            logger.info("[STATE] Dropped hello-noise value(s) for %s",
                        ", ".join(sorted(dropped)))
        return clean

    def _strip_end_call_marker(self, text: str) -> str:
        """Remove the [END_CALL] token before validation — _validate_response
        strips brackets/underscores and would leave spoken 'ENDCALL' litter.
        Seeing the token arms the end-call signal when the agent allows it
        (termination.llm_end_call, default True); when disallowed the token
        is still stripped so it can never be spoken."""
        if not text or ("[" not in text and "END" not in text):
            return text
        new = _END_CALL_RE.sub("", text)
        if new != text:
            if bool(self._termination_cfg().get("llm_end_call", True)):
                self._llm_end_call_seen = True
                logger.info("[HANGUP] LLM emitted [END_CALL] with its goodbye")
            else:
                logger.info("[HANGUP] [END_CALL] stripped — llm_end_call disabled")
            new = new.strip()
        return new

    def _is_close_confirmation(self, text: str) -> bool:
        """True when the caller just confirmed they're done, in reply to our
        permission-to-close question — and, for an agent with an authored
        `termination.pre_close_question` (RUNBOOK §75), only when THAT line
        is what was asked: its other "any other question?" lines are
        conversation (the doubts loop), not a goodbye — with
        `confirm_before_end: false` a "नहीं बस" to one of them hung up the
        call mid-requirement (sim 2026-10-12)."""
        last = self.last_agent_response or ""
        if not self._is_done_reply(text, last):
            return False
        pcq = self._pre_close_question()
        if pcq and not self._line_asks(last, pcq):
            return False
        return True

    def _is_done_reply(self, text: str, last: str) -> bool:
        """The caller's short "no / nothing else / thanks" in reply to an
        agent line that asked whether there is anything else. Kept
        deliberately strict: the reply must be short, open with a done-word,
        and ask nothing new — 'no, but actually...' keeps the conversation
        alive."""
        if not last or not self._CLOSE_PERMISSION_RE.search(last):
            return False
        t = (text or "").strip()
        if not t or len(t) > 45 or "?" in t:
            return False
        if re.search(r"\b(what|how|when|where|why|which|kya|kaise|kab|kaha|"
                     r"kaun|kitna|kitne|su|kem|ketla|but|lekin|par)\b",
                     t, re.IGNORECASE):
            return False
        return bool(self._DONE_REPLY_RE.search(t))

    @staticmethod
    def _parse_day_time(text: str) -> tuple[str, str]:
        """Deterministic (day, time) extraction from a callback phrase —
        shared by the callback policy and the v3 commitment clarifier."""
        lower = text.lower()
        day = ""
        if re.search(r"\b(today|aaj)\b", lower) or "आज" in text:
            day = "Today"
        elif re.search(r"\b(tomorrow|kal)\b", lower) or "कल" in text:
            day = "Tomorrow"
        elif re.search(r"\b(parso|day after)\b", lower):
            day = "Day after tomorrow"
        else:
            m = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", lower)
            if m:
                day = m.group(1).capitalize()
        tm = ""
        m = re.search(r"\b(\d{1,2})(?::\d{2})?\s*(a\.?m\.?|p\.?m\.?|baje)\b", lower)
        if m:
            tm = m.group(0)
        elif re.search(r"(morning|subah|सुबह)", lower):
            tm = "morning"
        elif re.search(r"(afternoon|dopahar|दोपहर)", lower):
            tm = "afternoon"
        elif re.search(r"(evening|shaam|शाम)", lower):
            tm = "evening"
        return day, tm

    # ── Relative callback offsets ("call me in/after 10 minutes") ──────
    # Word-numbers cover the spoken forms STT actually produces in en/hi/gu
    # code-mix; Devanagari/Gujarati numerals arrive as digits from Sarvam.
    _REL_NUM_WORDS = {
        "one": 1, "two": 2, "five": 5, "ten": 10, "fifteen": 15,
        "twenty": 20, "thirty": 30, "forty five": 45, "sixty": 60,
        "ek": 1, "do": 2, "paanch": 5, "panch": 5, "das": 10, "dus": 10,
        "pandrah": 15, "bees": 20, "tees": 30,
        "दस": 10, "पांच": 5, "पाँच": 5, "पंद्रह": 15, "बीस": 20, "तीस": 30,
        "દસ": 10, "પાંચ": 5, "પંદર": 15, "વીસ": 20, "ત્રીસ": 30,
    }
    _REL_MIN_RE = re.compile(
        r"\b(\d{1,3}|" + "|".join(_REL_NUM_WORDS) + r")\s*"
        r"(?:-|se)?\s*(?:\d{1,3}\s*)?"          # "10-15 minute" ranges → lower bound
        r"(minutes?|mins?|minit|min\b|मिनट|મિનિટ)", re.IGNORECASE)
    _REL_HOUR_RE = re.compile(
        r"\b(\d{1,2}|" + "|".join(_REL_NUM_WORDS) + r")\s*"
        r"(hours?|hrs?|ghante|ghanta|घंटे|घंटा|કલાક)", re.IGNORECASE)
    _REL_HALF_HOUR_RE = re.compile(
        r"\b(half\s+(?:an\s+)?hour|aa?dha\s+ghant|आधा\s*घंट|आधे\s*घंट|"
        r"અડધો\s*કલાક)", re.IGNORECASE)
    _REL_ONE_HOUR_RE = re.compile(
        r"\b(an\s+hour|one\s+hour|ek\s+ghanta|एक\s*घंट)", re.IGNORECASE)
    _REL_VAGUE_RE = re.compile(
        r"\b(baad|later|thodi\s+der|thoda\s+der|abhi\s+nahi|"
        r"in\s+a\s+(?:bit|while)|after\s+some\s?time|kuch\s+der)\b|"
        r"बाद|थोड़ी\s*देर|પછી|થોડી\s*વાર", re.IGNORECASE)

    @classmethod
    def _parse_relative_offset(cls, text: str) -> int | None:
        """Minutes until the requested callback, or None when the phrase has
        no usable relative offset ('call me in 10 minutes' → 10)."""
        def _num(tok: str) -> int:
            tok = tok.strip().lower()
            return int(tok) if tok.isdigit() else cls._REL_NUM_WORDS.get(tok, 0)
        if cls._REL_HALF_HOUR_RE.search(text):
            return 30
        m = cls._REL_MIN_RE.search(text)
        if m and _num(m.group(1)):
            return max(1, min(_num(m.group(1)), 24 * 60))
        m = cls._REL_HOUR_RE.search(text)
        if m and _num(m.group(1)):
            return max(1, min(_num(m.group(1)), 24)) * 60
        if cls._REL_ONE_HOUR_RE.search(text):
            return 60
        return None

    @staticmethod
    def _offset_phrase(offset_min: int, lang: str = "en") -> str:
        """'in about 10 minutes' / '10 मिनट में' — spoken confirmation form,
        in the caller's script (RUNBOOK §77: the hi/gu forms were romanized,
        which Sarvam renders poorly and no authored Pramukh line uses)."""
        if offset_min % 60 == 0:
            n = offset_min // 60
            unit = {"en": ("hour", "hours"), "hi": ("घंटे", "घंटे"),
                    "gu": ("કલાક", "કલાક")}.get(lang, ("hour", "hours"))
            amount = f"{n} {unit[0] if n == 1 else unit[1]}"
        else:
            unit_m = {"hi": "मिनट", "gu": "મિનિટ"}.get(lang)
            amount = (f"{offset_min} {unit_m}" if unit_m else
                      f"{offset_min} minute" + ("s" if offset_min != 1 else ""))
        return {"en": f"in about {amount}",
                "hi": f"{amount} में",
                "gu": f"{amount}માં"}.get(lang, f"in about {amount}")

    def _handle_callback_request(self, text: str, policy: dict) -> str | None:
        """Capture a callback time preference WITHOUT booking a calendar slot
        (never _record_calendar_booking) and reply from the policy bank."""
        day, tm = self._parse_day_time(text)
        offset_min = self._parse_relative_offset(text)
        if offset_min:
            preference = self._offset_phrase(offset_min,
                                             (self.state.language or "en")[:2])
        else:
            preference = " ".join(x for x in (day, tm) if x) or "later"
        slot_name = str(policy.get("capture_slot") or "callback_time")
        if slot_name in self.state_mgr.slots:
            self.state_mgr.set_externally(slot_name, preference, source="system")
        self.state_mgr.extra_flags["callback_requested"] = True
        self.state_mgr.extra_flags["callback_time"] = preference
        if offset_min:
            self.state_mgr.extra_flags["callback_offset_min"] = offset_min
        self.call_outcome = self.call_outcome or "callback_requested"
        logger.info("[CALLBACK] captured preference %r (no calendar booking)", preference)
        self._arm_callback_close()
        resp = self._lang_response(policy.get("responses") or {})
        if resp:
            return resp
        if (self.state.language or "en")[:2] in ("hi", "gu"):
            # RUNBOOK §77: the built-in below was English on every Hindi call.
            return self._clarify_confirmation(day, tm, offset_min)
        return (f"No problem at all — we'll call you back {preference}. "
                "Thank you, and have a great day!")

    # ── v3 in-call commitment clarification (PLAN-intelligence-layer §2.8) ──

    def _clarify_cfg(self) -> dict:
        try:
            return self._def.follow_up.clarify_commitments or {}
        except Exception:
            return {}

    def _sync_commitments(self) -> None:
        """extra_flags["commitments"] → collected_data → post-call pipeline."""
        self.state_mgr.extra_flags["commitments"] = list(self._commitments)

    def _record_commitment(self, quote: str, day: str, tm: str,
                           clarified: bool, offset_min: int | None = None) -> dict:
        if offset_min:
            described = self._offset_phrase(offset_min, "en")
        else:
            described = " ".join(x for x in (day, tm) if x) or "later"
        c = {
            "kind": "callback", "made_by": "customer",
            "description": f"Customer asked to be called {described}",
            "quote": quote[:300], "day": day, "time": tm,
            "offset_min": offset_min or 0,
            "vague": not (offset_min or (day and tm)), "clarified": clarified,
            "turn": self.state.turn_count,
        }
        self._commitments.append(c)
        self._sync_commitments()
        return c

    def _capture_callback_flags(self, day: str, tm: str,
                                offset_min: int | None = None) -> None:
        """The same side effects _handle_callback_request produces, so the
        outcome taxonomy and dashboard snapshots keep working unchanged."""
        if offset_min:
            preference = self._offset_phrase(offset_min,
                                             (self.state.language or "en")[:2])
        else:
            preference = " ".join(x for x in (day, tm) if x) or "later"
        cb_policy = self._def.call_flow.callback_policy or {}
        slot_name = str(cb_policy.get("capture_slot") or "callback_time")
        if slot_name in self.state_mgr.slots:
            self.state_mgr.set_externally(slot_name, preference, source="system")
        self.state_mgr.extra_flags["callback_requested"] = True
        self.state_mgr.extra_flags["callback_time"] = preference
        if offset_min:
            self.state_mgr.extra_flags["callback_offset_min"] = offset_min
        self.call_outcome = self.call_outcome or "callback_requested"

    def _arm_callback_close(self) -> None:
        """A confirmed callback IS the caller's request to end the call now —
        honouring it (confirm + goodbye, then platform hangup) is the
        consented close the supreme rule requires. The caller speaking again
        still cancels the hangup at the top of process_stream."""
        self.state.is_closing = True
        self.commit_pending_turn()  # closing turn — never rolled back
        self.state.stage = STAGE_CLOSING
        self.hangup_pending = True
        logger.info("[HANGUP] callback confirmed — closing with caller consent")

    # RUNBOOK §77: the parser's English labels ("Tomorrow", "evening",
    # "11 baje") in the caller's script for a spoken line. English unchanged.
    _DAY_WORDS = {
        "hi": {"today": "आज", "tomorrow": "कल", "day after tomorrow": "परसों",
               "monday": "सोमवार", "tuesday": "मंगलवार", "wednesday": "बुधवार",
               "thursday": "गुरुवार", "friday": "शुक्रवार", "saturday": "शनिवार",
               "sunday": "रविवार"},
        "gu": {"today": "આજે", "tomorrow": "કાલે", "day after tomorrow": "પરમ દિવસે",
               "monday": "સોમવારે", "tuesday": "મંગળવારે", "wednesday": "બુધવારે",
               "thursday": "ગુરુવારે", "friday": "શુક્રવારે", "saturday": "શનિવારે",
               "sunday": "રવિવારે"},
    }
    _TIME_WORDS = {"hi": {"morning": "सुबह", "afternoon": "दोपहर", "evening": "शाम"},
                   "gu": {"morning": "સવારે", "afternoon": "બપોરે", "evening": "સાંજે"}}

    @classmethod
    def _spoken_day_time(cls, day: str, tm: str, lang: str) -> tuple[str, str]:
        if lang not in cls._DAY_WORDS:
            return day, tm
        d = cls._DAY_WORDS[lang].get((day or "").strip().lower(), day)
        t = cls._TIME_WORDS[lang].get((tm or "").strip().lower())
        if t is None:
            t = re.sub(r"\s*\bbaje\b", " बजे" if lang == "hi" else " વાગે",
                       tm or "", flags=re.IGNORECASE).strip()
        return d, t

    def _authored_busy_line(self) -> str:
        """The agent's own `outbound.busy_response`, personalised — "" when
        there is none or this is not an outbound agent. The Pramukh builders
        author it in Devanagari; it is the owner's wording for exactly the
        moment the busy-flavoured clarification covers."""
        ob = getattr(self._def, "outbound", None)
        if ob is None or getattr(self._def, "call_direction", "") != "outbound":
            return ""
        try:
            line = self._personalize(
                str(self._lang_response(getattr(ob, "busy_response", {}) or {})
                    or "").strip(), "outbound.busy_response")
        except Exception:
            logger.exception("[CALLBACK] authored busy line failed")
            return ""
        return line if len(line) >= 10 else ""

    def _clarify_question(self, day: str, tm: str, relative: bool = False,
                          busy: bool = False) -> str:
        # RUNBOOK §77: hi/gu lines in native script (they were romanized,
        # which Sarvam renders poorly and the Devanagari always-rule forbids).
        lang = (self.state.language or "en")[:2]
        d, _t = self._spoken_day_time(day, tm, lang)
        if busy:
            # "sorry, not free right now" — apologise for the timing and pin
            # down when to call back; never keep pitching a busy caller. The
            # agent's authored busy line first, when it has one.
            authored = self._authored_busy_line()
            if authored:
                return authored
            bank = {"en": "No problem at all — about how many minutes later "
                          "should I call you back, or is there a time that suits you?",
                    "hi": "कोई बात नहीं — मैं कितने मिनट बाद कॉल करूँ, या कोई "
                          "समय बताइए जो आपको ठीक लगे?",
                    "gu": "કોઈ વાંધો નહીં — કેટલી મિનિટ પછી કૉલ કરું, કે કોઈ "
                          "સમય કહો જે તમને ઠીક લાગે?"}
        elif relative:
            # "call me in a bit / thodi der baad" — pin down the delay, not a day.
            bank = {"en": "Of course — about how many minutes should I wait "
                          "before calling you back, or is there a time that suits you?",
                    "hi": "ज़रूर — मैं कितने मिनट बाद कॉल करूँ, या कोई समय "
                          "बताइए जो आपको ठीक लगे?",
                    "gu": "ચોક્કસ — કેટલી મિનિટ પછી કૉલ કરું, કે કોઈ સમય કહો "
                          "જે તમને ઠીક લાગે?"}
        elif day and not tm:
            bank = {"en": f"Sure — what time on {day.lower()} works best for you?",
                    "hi": f"ज़रूर — {d} कितने बजे कॉल करना ठीक रहेगा?",
                    "gu": f"ચોક્કસ — {d} કેટલા વાગે કૉલ કરવો ઠીક રહેશે?"}
        elif tm and not day:
            bank = {"en": "Sure — which day works best for you?",
                    "hi": "ज़रूर — किस दिन कॉल करना ठीक रहेगा?",
                    "gu": "ચોક્કસ — કયા દિવસે કૉલ કરવો ઠીક રહેશે?"}
        else:
            bank = {"en": "Sure — which day and what time would work best for us to call you?",
                    "hi": "ज़रूर — किस दिन और कितने बजे कॉल करना ठीक रहेगा?",
                    "gu": "ચોક્કસ — કયા દિવસે અને કેટલા વાગે કૉલ કરવો ઠીક રહેશે?"}
        return bank.get(lang, bank["en"])

    def _clarify_confirmation(self, day: str, tm: str,
                              offset_min: int | None = None) -> str:
        lang = (self.state.language or "en")[:2]
        if offset_min:
            preference = self._offset_phrase(offset_min, lang)
        else:
            d, t = self._spoken_day_time(day, tm, lang)
            preference = (" ".join(x for x in (d, t) if x)
                          or {"hi": "जल्द ही", "gu": "જલ્દી"}.get(lang, "soon"))
        # Goodbye tail included: this confirmation is a closing line — the
        # platform hangs up after it plays (_arm_callback_close).
        bank = {"en": f"Perfect — I'll call you back {preference}. "
                      "Thank you, and have a great day!",
                "hi": f"बिल्कुल — हम आपको {preference} कॉल करेंगे। "
                      "धन्यवाद, आपका दिन शुभ रहे!",
                "gu": f"ચોક્કસ — અમે તમને {preference} કૉલ કરીશું. "
                      "આભાર, તમારો દિવસ સરસ રહે!"}
        return bank.get(lang, bank["en"])

    def _maybe_ask_clarification(self, text: str) -> str | None:
        """Fresh commitment detected. Vague + not yet asked → ask the ONE
        clarification; otherwise record it and acknowledge politely."""
        day, tm = self._parse_day_time(text)
        offset_min = self._parse_relative_offset(text)
        self._capture_callback_flags(day, tm, offset_min)
        if offset_min:
            self._record_commitment(text, day, tm, clarified=True,
                                    offset_min=offset_min)
            logger.info("[COMMITMENT] relative commitment captured: +%d min",
                        offset_min)
            self._arm_callback_close()
            return self._clarify_confirmation(day, tm, offset_min)
        if day and tm:
            self._record_commitment(text, day, tm, clarified=True)
            logger.info("[COMMITMENT] precise commitment captured: %s %s", day, tm)
            self._arm_callback_close()
            return self._clarify_confirmation(day, tm)
        commitment = self._record_commitment(text, day, tm, clarified=False)
        if self._clarify_asked:
            # One question per call, ever — never interrogate the caller.
            self._arm_callback_close()
            return self._clarify_confirmation(day, tm)
        self._clarify_asked = True
        self._pending_clarify = commitment
        self._suppress_time_match_turns = 2  # this turn + the answer turn
        # Busy flavour only when the brush-off carries NO time hint at all —
        # "busy right now, call me next week" still gets the day question.
        _busy = (not day and not tm
                 and bool(self._BUSY_NOW_RE.search(text))
                 and not self._CALLBACK_TIME_RE.search(text))
        _relative = not day and bool(self._REL_VAGUE_RE.search(text))
        logger.info("[COMMITMENT] vague commitment (%r) — asking clarification",
                    " ".join(x for x in (day, tm) if x) or "later")
        return self._clarify_question(day, tm, relative=_relative, busy=_busy)

    def _resolve_clarification(self, text: str) -> str | None:
        """The turn after our clarification question: fold the answer into the
        pending commitment. Unparseable answers give up gracefully — the
        commitment stays vague and the post-call engine schedules a default."""
        pending = self._pending_clarify
        self._pending_clarify = None
        if pending is None:
            return None
        offset_min = self._parse_relative_offset(text)
        if offset_min:
            pending["offset_min"] = offset_min
            pending["vague"] = False
            pending["clarified"] = True
            pending["description"] = ("Customer asked to be called "
                                      + self._offset_phrase(offset_min, "en"))
            self._sync_commitments()
            self._capture_callback_flags("", "", offset_min)
            self._suppress_time_match_turns = 1
            logger.info("[COMMITMENT] clarified: +%d min", offset_min)
            self._arm_callback_close()
            return self._clarify_confirmation("", "", offset_min)
        day, tm = self._parse_day_time(text)
        day = day or pending.get("day", "")
        tm = tm or pending.get("time", "")
        if not day and not tm:
            logger.info("[COMMITMENT] clarification answer unparseable — keeping vague")
            return None  # let the normal engine handle whatever this turn was
        pending["day"], pending["time"] = day, tm
        pending["vague"] = not (day and tm)
        pending["clarified"] = True
        pending["description"] = ("Customer asked to be called "
                                  + " ".join(x for x in (day, tm) if x))
        self._sync_commitments()
        self._capture_callback_flags(day, tm)
        self._suppress_time_match_turns = 1  # cover this turn's extraction too
        logger.info("[COMMITMENT] clarified: %s %s", day, tm)
        self._arm_callback_close()
        return self._clarify_confirmation(day, tm)

    def _commitment_checkpoint(self, farewell_text: str) -> str | None:
        """Farewell-time last chance: a vague unclarified commitment (either
        detected earlier or embedded in the farewell itself) gets the one
        clarification question instead of the closing line."""
        if not self._clarify_cfg().get("enabled") or self._clarify_asked:
            return None
        if self._detect_callback_request(farewell_text):
            return self._maybe_ask_clarification(farewell_text)
        vague = [c for c in self._commitments
                 if c.get("vague") and not c.get("clarified")]
        if not vague:
            return None
        self._clarify_asked = True
        self._pending_clarify = vague[-1]
        self._suppress_time_match_turns = 2
        logger.info("[COMMITMENT] farewell checkpoint — asking clarification")
        return self._clarify_question(vague[-1].get("day", ""),
                                      vague[-1].get("time", ""))

    def _pick_filler(self) -> str:
        """Return one filler phrase, chosen by input type.

        Two tiers:
        - APPROVAL filler  — for simple affirmations ("yes", "ok", "haan").
          Signals agreement only, no "I'm looking something up" implication.
          Examples: "Sure!", "Haanji!", "Of course!"
        - THINKING filler  — for information requests / complex questions.
          Signals the agent is retrieving or reasoning.
          Examples: "One moment please...", "Ek minute...", "Batati hoon..."

        The YAML bank (response_bank.filler_responses) provides thinking fillers
        for agents that want branded phrasing. The approval pool is always
        code-driven so it stays short and varied across all agents.
        """
        lang = self.state.language

        # ── Classify the caller's last turn ──────────────────────────────────
        last_user = ""
        for t in reversed(self.memory.turns):
            if t.role == "user":
                last_user = t.text.lower().strip()
                break

        _ACK_TOKENS = {
            # English affirmations
            "yes", "yeah", "yep", "yup", "sure", "ok", "okay", "fine",
            "good", "alright", "right", "agreed", "please", "go ahead",
            "of course", "sounds good", "great", "perfect", "wonderful",
            # Hindi affirmations
            "haan", "ha", "haa", "ji", "haanji", "bilkul", "zaroor",
            "theek", "sahi", "accha", "thik", "wah", "haan ji",
            # Gujarati affirmations
            "ha", "haa", "ji", "bilkul", "zaroor", "saru", "thik chhe",
        }
        # filler_ack_extended: recognise Devanagari/Gujarati-script
        # affirmations too — otherwise an all-Devanagari approval always
        # lands in the 'thinking' pool ('एक मिनट…' before every turn of the
        # Kaapd call). Per-call conditional union — never mutate the set
        # shared shape across agents in one process.
        if getattr(self._def.llm_config, "filler_ack_extended", False):
            _ACK_TOKENS = _ACK_TOKENS | {
                "हाँ", "हां", "जी", "हाँजी", "जीहाँ", "बिल्कुल", "बिलकुल",
                "ठीक", "अच्छा", "ज़रूर", "जरूर", "सही",
                "હા", "જી", "બરાબર", "ચોક્કસ", "જરૂર", "સારું",
            }
            # \w drops Devanagari/Gujarati combining marks ('हाँ' → 'ह'),
            # so script text must be tokenized with the marks preserved or
            # the extended tokens can never match.
            clean_words = set(re.findall(
                r"[\wऀ-ॿ઀-૿]+", last_user))
        else:
            clean_words = set(re.sub(r"[^\w\s]", "", last_user).split())
        is_simple_ack = (
            len(clean_words) <= 6
            and bool(clean_words & _ACK_TOKENS)
            and "?" not in last_user
        )

        # ── Approval pool — short, instant, no checking implication ──────────
        _APPROVAL: dict[str, list[str]] = {
            # Reactions to the CALLER's "okay", not answers to a request:
            # "Of course!" / "Absolutely!" to "okay okay" (call 919426600929)
            # agreed to something nobody asked.
            "en": ["Okay.", "Alright.", "Great.", "Okay, great."],
            "hi": ["Haanji!", "Zaroor!", "Bilkul!", "Ji haan!", "Haan, bilkul!"],
            "gu": ["Haanji!", "Zaroor!", "Bilkul!", "Ji!", "Haan!"],
        }

        # ── Thinking pool — implies retrieving / reasoning ───────────────────
        # Use the YAML bank when the agent has branded thinking phrases IN THE
        # CALLER'S LANGUAGE; otherwise prefer the built-in pool for that
        # language over another language's YAML bank. The old lookup was
        # bank.get(lang, bank.get("en")) — so a Gujarati caller of an agent
        # whose YAML had only en/hi banks got an ENGLISH filler mid-Gujarati
        # conversation, and the built-in Gujarati pool below was unreachable
        # (observed live: "हाँ, एक सेकंड... ચોક્કસ, 12:00 વાગ્યેનો...").
        bank = self._def.response_bank.filler_responses
        yaml_thinking = bank.get(lang, [])
        _THINKING_BUILTIN: dict[str, list[str]] = {
            "en": [
                "One moment please...",
                "Let me check...",
                "Sure, one second...",
                "Let me look into that...",
            ],
            "hi": [
                "Ek minute...",
                "Batati hoon...",
                "Ek second, dekh leti hoon...",
                "Haan, check karti hoon...",
            ],
            "gu": [
                "Ek minute...",
                "Joi leti hoon...",
                "Ek second...",
                "Jova dyo...",
            ],
        }

        if is_simple_ack:
            pool = _APPROVAL.get(lang, _APPROVAL["en"])
        elif yaml_thinking:
            pool = yaml_thinking
        else:
            pool = _THINKING_BUILTIN.get(lang, _THINKING_BUILTIN["en"])

        resp = pool[self._filler_counter % len(pool)]
        self._filler_counter += 1
        return resp

    # ------------------------------------------------------------------
    # Last-response memory + repeat detection (feature 3)
    # ------------------------------------------------------------------

    def _stage_asks_questions(self) -> bool:
        """Does the CURRENT stage collect information at all?

        False for a beat that collects nothing in a flow whose author wired
        `collect` lists — a greeting, an identity confirm, an availability
        check, the caller's own introduction, a close. Such a stage used to be
        handed CONVERSATION STATE's "Ask ONLY ONE question next, about: X",
        which outranks a stage goal: measured live 2026-09-16, the agent
        confirmed "am I speaking to Piyush?" and went straight to "what are
        your core technical skills?" — never introducing itself, never asking
        whether it was a good time, never asking for the introduction.

        The signal is whether the flow wires `collect` ANYWHERE. If it does,
        an empty list is a deliberate "nothing here". If it never does (every
        pre-existing agent — pramukh, pramukh_agent, demo_realty, …), the
        lists carry no information and global slot priority is left exactly as
        it was.
        """
        if not any(getattr(s, "collect", None)
                   for s in (self._def.call_flow.stages or [])):
            return True
        return bool(getattr(self._stage_def(), "collect", None))

    def _derive_qa_field_signals(self) -> dict[str, list[str]] | None:
        """qa_field_signals, widened with each slot's OWN question text.

        QATracker matches the agent's spoken line against these signals to
        learn which field it just asked about. Builder-created agents get
        qa_field_signals auto-filled with the slot LABEL ("joining timeline")
        — a phrase the agent never says — so the tracker matched nothing and
        the whole anti-repetition layer was inert on every Copilot agent.
        Live 2026-09-15 (call 851): the agent asked "Could you let us know
        how fast you could join us?", the caller answered "immediately", and
        the next stage's script asked it again word for word.

        The slot's configured question IS what the agent says when it follows
        the script, in every language it is authored in — so it is the signal
        that was missing. Configured signals are kept: this only ever widens.
        """
        try:
            signals: dict[str, list[str]] = {
                str(k): [str(s).lower() for s in (v or [])]
                for k, v in (self._def.knowledge_base.qa_field_signals
                             or {}).items()
            }
            for spec in (self._def.slots or []):
                name = str(getattr(spec, "name", "") or "")
                if not name:
                    continue
                bucket = signals.setdefault(name, [])
                for src in (getattr(spec, "question", None),
                            getattr(spec, "reask_question", None)):
                    for phrase in (src or {}).values():
                        phrase = str(phrase or "").strip().lower().rstrip("?.!")
                        # Long enough to be distinctive: a two-word question
                        # ("kitna?") would match half the call.
                        if len(phrase) >= 12 and phrase not in bucket:
                            bucket.append(phrase)
                if not bucket:
                    signals.pop(name, None)
            return signals or None
        except Exception:
            logger.exception("[QA] signal derivation failed — using config")
            return self._def.knowledge_base.qa_field_signals or None

    # Sentence enders, script-aware (Devanagari danda included).
    _QUESTION_SENT_RE = re.compile(r"[^.!?।॥]*\?")

    def _previous_turn_question(self) -> str:
        """The question the agent asked on its IMMEDIATELY PREVIOUS turn.

        Distinct from `_last_question_asked`, which scans back through the
        whole window for anything that invited a reply — for "you already
        asked this" only the last turn counts, because that is the question
        the caller's current words are answering.
        """
        last = (self.last_agent_response or "").strip()
        if not last or "?" not in last:
            return ""
        matches = self._QUESTION_SENT_RE.findall(last)
        if not matches:
            return ""
        return matches[-1].strip(" ,;:—-")

    def _store_last_response(self, text: str) -> None:
        """Cache the most recent agent utterance for repeat/translate requests."""
        self.last_agent_response = text
        self.last_agent_language = self.state.language
        self.memory.qa_tracker.observe_agent_turn(text, self.memory.turn_index)
        self._note_recommendation(text)
        self._pitch_observe_agent(text)
        self._share_observe_agent(text)
        # C1/C3/C4/C6: every agent utterance resets the capture expectation
        # and re-derives it from what was actually said; the capture yield
        # sites re-set their flag right after calling this.
        self._capture_awaiting_payload = False
        self._capture_awaiting_confirm = False
        try:
            self._note_ask_signals(text)
        except Exception:
            logger.exception("[CAPTURE] ask-signal scan failed (continuing)")
        logger.debug("[LAST-RESPONSE] Stored shortcut response (lang=%s): %r",
                     self.last_agent_language, text[:60])

    _EMAIL_ASK_RE = re.compile(
        r"(?:e-?mail|ईमेल|इमेल|ई-मेल|મેલ|ઈમેલ|મેઈલ)", re.IGNORECASE)
    _ASK_VERB_RE = re.compile(
        r"(?:\?|बता|बोलिए|भेज|दीजिए|लिख|spell|share|give|provide|send|"
        r"કહો|જણાવ|લખાવ)", re.IGNORECASE)

    def _note_ask_signals(self, response: str) -> None:
        """C1 + C6: derive, from the agent's own utterance, (a) whether an
        email capture should arm and (b) which non-contact slot a direct
        answer may overwrite next turn."""
        self._last_asked_slot = None
        if not response:
            return
        low = response.lower()
        # C3: whenever the utterance that just played reads back the pending
        # value (engine template OR a scrubbed LLM readback), a bare yes/no
        # next turn resolves it. " — at — " is the spelled-readback marker.
        _cap = self._capture_slot
        if _cap and _cap in self.state_mgr._pending_fields:
            _val = str(self.state_mgr.state.get(_cap) or "")
            if _val and (" — at — " in response or _val.lower() in low):
                self._capture_awaiting_confirm = True
        # (a) email-ask → arm capture for the first unresolved email slot.
        if self._EMAIL_ASK_RE.search(response) and self._ASK_VERB_RE.search(response):
            for n, spec in self.state_mgr.slots.items():
                if spec.type != "email" or n in self.state_mgr.skipped:
                    continue
                if (self.state_mgr.state.get(n) is None
                        or n in self.state_mgr._pending_fields):
                    if self._capture_asked_slot != n:
                        logger.info("[CAPTURE] ask detected for slot %r", n)
                    self._capture_asked_slot = n
                    self._capture_awaiting_payload = (
                        n not in self.state_mgr._pending_fields)
                    break
        # (b) which slot does this utterance ask about? (question turns only)
        if "?" not in response:
            return
        for n, spec in self.state_mgr.slots.items():
            if spec.type in ("email", "phone") or n == "name":
                continue
            kws = {n.lower()} | {w for w in spec.display().lower().split()
                                 if len(w) >= 3}
            # Clock/calendar words mark a slot that HOLDS a time or a date —
            # never an enum that merely has "time" in its name. The enum
            # purchase_timeline was credited by "आपने कुछ समय पहले…" in the
            # good-time line, and the caller's "हाँ, अभी बात कर सकते हैं"
            # then wrote it as Immediate through the ask gate (call
            # b5452b23) — the timeline question was never asked.
            _enum = spec.type == "enum"
            if spec.type == "time" or ("time" in n and not _enum):
                kws |= {"बजे", "समय", "टाइम", "time", "વાગ્યે", "સમય"}
            elif spec.type == "date" or (
                    ("day" in n or "date" in n) and not _enum):
                kws |= {"day", "दिन", "तारीख", "date", "દિવસ", "તારીખ"}
            if any(k in low for k in kws):
                self._last_asked_slot = n
                break

    def _is_repeat_request(self, text: str) -> bool:
        """Return True if the caller is asking to repeat the last agent response."""
        lower = text.lower().strip()
        return any(p in lower for p in _REPEAT_DETECT_PATTERNS)

    def _lang_switch_has_content(self, text: str) -> bool:
        """True when a language-switch utterance carries a substantive ask
        beyond the switch itself ("हिंदी में बताओ लेस कॉल के बारे में").

        Errs toward True: the cost of a wrong True is one LLM turn instead of
        the instant canned ack; the cost of a wrong False is the ack swallowing
        the caller's actual question.
        """
        # \w alone shatters Indic tokens: Python's \w excludes matras and
        # anusvara (Mn/Mc), so "हिंदी" tokenizes as ['ह','द']. Include the
        # full Devanagari + Gujarati blocks explicitly.
        tokens = re.findall(r"[\w'’ऀ-ॿ઀-૿]+", text.lower())
        residue = [t for t in tokens if t not in _LANG_SWITCH_STOP_TOKENS]
        return sum(len(t) for t in residue) >= 4

    def _arm_lang_redeliver_directive(self, lang: str) -> None:
        """One-turn system-prompt trailer: answer the caller's actual request
        in the requested language — never a bare language acknowledgment."""
        name = {"hi": "Hindi", "gu": "Gujarati", "en": "English"}.get(
            lang, "the requested language")
        self._turn_lang_directive = (
            f"LANGUAGE SWITCH THIS TURN: the caller just asked for {name}. Do NOT "
            f"answer with a bare acknowledgment like 'sure, let's speak {name}'. "
            f"Answer their actual request fully in {name}; if they asked you to "
            f"repeat or re-explain something you already said, restate that "
            f"content in {name}."
        )

    # ------------------------------------------------------------------
    # LLM warmup (feature 6)
    # ------------------------------------------------------------------

    @staticmethod
    def _warm_rag_index_impl(agent_id: str) -> None:
        """Load the document index and open the embedding connection.

        Runs in a worker thread off the warmup path. Everything it touches is
        cached per process afterwards: the parsed chunks, the normalised vector
        matrix, the BM25 index and the HTTPS connection.
        """
        try:
            from ..documents.store import get_folder_by_agent_id, has_documents
            folder = get_folder_by_agent_id(agent_id)
            if not folder or not has_documents(folder):
                return
            from ..documents import embeddings, retrieval
            retrieval.load_cached_index(folder)
            provider = embeddings.get_provider()
            if embeddings.provider_available(provider):
                embeddings.embed_query("warmup", provider)
            logger.info("[RAG] index + embedding connection warm for %s", folder)
        except Exception as exc:
            logger.warning("[RAG] warmup failed (first turn will be slower): %r", exc)

    async def warmup(self) -> None:
        """Warm up LLM API connection to reduce first-call latency.

        Runs at most once per process (class-level flag) — Issue 6.
        """
        # The document-RAG warmup is deliberately ABOVE the once-per-process
        # gate below, and guarded per AGENT instead. The LLM connection is a
        # process-wide resource; a document index is not — it belongs to one
        # agent. Gating it per process meant it fired for whichever agent
        # happened to start first, which on this runtime is the document-less
        # neutral fallback, so it did nothing at all in production and every
        # agent with documents still paid the cold-start on its first RAG turn.
        #
        # Cost of getting this wrong: the embedding endpoint is a different
        # host, and its first request in a fresh process pays DNS + TLS —
        # measured at 1.7s against 232ms warm, i.e. most of the retrieval
        # deadline spent on setup. Detached and best-effort: a failed warmup
        # costs the first RAG turn its context, never the call.
        agent_id = self._def.agent_id
        if agent_id and agent_id not in ConversationRouter._rag_warmed_agents:
            ConversationRouter._rag_warmed_agents.add(agent_id)
            asyncio.ensure_future(asyncio.to_thread(
                ConversationRouter._warm_rag_index_impl, agent_id))

        if ConversationRouter._warmup_done_flag:
            logger.debug("[CONV] LLM warmup skipped — already done this process")
            return
        # Same once-per-process moment, and equally off the call path: ask the
        # provider whether it will actually cache a repeated prompt prefix.
        # The answer decides whether ~86% of every turn's input tokens are
        # billed fresh, and before this it was not visible anywhere. Detached
        # task — a hung probe must never delay the warmup or a call.
        _prompt_cache.spawn_capability_probe(
            self._model, self._api_key, self._base_url)
        try:
            client = self._get_client()
            await client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
                temperature=0.0,
                **self._llm_extra,
            )
            ConversationRouter._warmup_done_flag = True
            logger.info("[CONV] LLM warmup complete (first time this process)")
        except Exception as exc:
            logger.warning("[CONV] LLM warmup failed: %s", exc)

    # ------------------------------------------------------------------
    # Localization helpers
    # ------------------------------------------------------------------

    def _localized(self, key: str, fallback: str,
                   lang: str | None = None) -> str:
        loc = self._def.response_bank.localized_responses.get(
            lang if lang is not None else self.state.language, {})
        val = loc.get(key)
        if val is None:
            return fallback
        if isinstance(val, list):
            return val[self._response_counter % len(val)]
        return val

    def _localized_list(self, key: str, en_list: list[str]) -> str:
        loc = self._def.response_bank.localized_responses.get(self.state.language, {})
        val = loc.get(key)
        if isinstance(val, list):
            resp = val[self._response_counter % len(val)]
            self._response_counter += 1
            return resp
        resp = en_list[self._response_counter % len(en_list)]
        self._response_counter += 1
        return resp
