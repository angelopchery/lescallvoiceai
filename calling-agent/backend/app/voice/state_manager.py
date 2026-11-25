"""
ConversationStateManager — the conversation state management layer.

Solves the "user gave three answers in one sentence, agent asks all three
again" problem. One caller utterance like

    "I want a 3BHK apartment in Vapi under 50 lakhs"

fills location / configuration / budget in a single pass; the agent then
acknowledges what it knows and asks ONLY the highest-priority missing field.

Design (latency-first, telephony-first)
---------------------------------------
* extract_entities() is pure precompiled-regex + knowledge-base matching —
  well under 1 ms per utterance. It runs synchronously on the hot path so the
  CURRENT turn's system prompt already contains the CURRENT turn's facts.
* An optional async LLM extractor (see conversation.py) back-fills slots the
  regexes miss; it merges ONLY into still-empty slots so a late/stale result
  can never clobber newer information (race-safe by construction).
* merge_state() never overwrites a value with null, and logs every change to
  an audit trail. Latest user information always wins for real values —
  "Actually make it a 2BHK" flips configuration and records old→new.
* running_summary() and build_prompt_context() are template-built strings —
  no LLM calls, no I/O, nothing on the critical path but string formatting.
* Slot vocabulary is per-agent: defined in agent YAML (`slots:`) or derived
  from the agent's qa_field_signals as a fallback, so real-estate agents get
  location/bhk/budget/... while a meeting-booking EA tracks name/company/....

Telephony realities handled: Hinglish + Devanagari + Gujarati phrasing,
noisy ASR fragments (extract what parses, ignore the rest), corrections,
multi-intent utterances, information fragmented across turns (merge), and
barge-in (extraction is synchronous, so an interrupted turn still counts).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class SlotSpec:
    """One trackable piece of information."""
    name: str                                   # canonical key, e.g. "location"
    priority: int = 100                         # lower = asked earlier
    required: bool = True                       # counts toward missing_slots
    label: str = ""                             # human label for prompts ("Configuration")
    question: dict[str, str] = field(default_factory=dict)   # lang -> question to ask
    summary_known: str = ""                     # template, {value} substituted
    summary_unknown: str = ""                   # sentence used while missing
    # v2 (mirrors SlotDefinition — all defaults keep legacy behaviour):
    type: str = ""                              # ''|text|number|money|enum|boolean|phone|date|time|location
    enum_values: list[str] = field(default_factory=list)
    enum_synonyms: dict[str, list[str]] = field(default_factory=dict)
    extract_patterns: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    merge_policy: str = "free"                  # free|correction_gated|confirm
    ask_gated: bool = False                     # RUNBOOK §75 — answer-only writes
    confirm_readback: str = ""                  # ''|verbatim|digit_groups_2
    max_asks: int = 0                           # 0 = unlimited
    reask_question: dict[str, str] = field(default_factory=dict)
    # Precompiled at manager init (hot path stays <1 ms):
    _compiled_patterns: list = field(default_factory=list, repr=False, compare=False)
    _synonym_lookup: dict = field(default_factory=dict, repr=False, compare=False)

    def display(self) -> str:
        return self.label or self.name.replace("_", " ").title()


# type: phone — Indian mobile (mirrors memory.py's extraction pattern)
_SLOT_PHONE_RE = re.compile(
    r'(?<!\d)'
    r'(?:(?:\+91|91)[-.\s]?)?'
    r'([6-9]\d{4}[-.\s]?\d{5})'
    r'(?!\d)',
)

# Generic number for type: number
_SLOT_NUMBER_RE = re.compile(r'(?<![\d.])(\d+(?:\.\d+)?)(?![\d.])')

# Script-aware word boundaries (RUNBOOK §42 rule: never `\b`, never
# str.isalnum() as a boundary test around Indic text — a matra, chandrabindu
# or anusvara is category Mn/Mc, so both treat the middle of "मिली" or the
# end of "हाँ" as a word edge).
_WORD_CHAR_RE = re.compile(r"[0-9a-zऀ-ॣ०-ॿ઀-૿]")


_AMOUNT_WORD_RE = re.compile(
    r"lakh|lac\b|lacs|crore|\bcr\b|\bk\b|thousand|hazaar|hazar|"
    r"लाख|करोड़|करोड|हज़ार|हजार|કરોડ|લાખ|હજાર|"
    r"\b(?:one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|"
    r"forty|fifty|sixty|seventy|eighty|ninety|hundred|ek|do|teen|char|"
    r"paanch|das|bees|tees|chalis|pachas|saath|sattar|assi|nabbe|sau|"
    r"dedh|dhai|sawa)\b|"
    r"एक|दो|तीन|चार|पांच|पाँच|छह|सात|आठ|नौ|दस|बीस|तीस|चालीस|पचास|साठ|"
    r"सत्तर|अस्सी|नब्बे|सौ|डेढ़|ढाई|सवा|साढ़े", re.IGNORECASE)


def _looks_like_amount(text: str) -> bool:
    """Carries a digit, or a money unit / number word — nothing about it
    says it is a figure otherwise ("No", "नहीं देखना", "flexible")."""
    t = str(text or "")
    return any(ch.isdigit() for ch in t) or bool(_AMOUNT_WORD_RE.search(t))


def _word_boundary_ok(lower: str, start: int, end: int) -> bool:
    before_ok = start == 0 or not _WORD_CHAR_RE.match(lower[start - 1])
    after_ok = end >= len(lower) or not _WORD_CHAR_RE.match(lower[end])
    return before_ok and after_ok


# Boolean answers, with the same script-aware edges. The old `\b` form
# never matched "हाँ" / "नहीं" at all.
_NB = r"(?<![0-9a-zऀ-ॣ०-ॿ઀-૿])"
_NA = r"(?![0-9a-zऀ-ॣ०-ॿ઀-૿])"
_BOOL_YES_RE = re.compile(
    _NB + r"(?:yes|yeah|yep|haan|han|ha|ji|bilkul|sure|ok|okay|"
    r"હા|हां|हाँ|जी|बिल्कुल|हाँजी|हांजी)" + _NA)
_BOOL_NO_RE = re.compile(
    _NB + r"(?:no|nahi|nahin|nope|नहीं|नहि|ना|ना|ના|નહીં)" + _NA)

# Everyday words that appear in a closed vocabulary — evaluations,
# agreements, fillers. Matched anywhere in a call they mean nothing about
# the slot ("अच्छा, तो…" is "I see", "हाँ ठीक रहेगा" is "fine, go ahead");
# they are values only as the answer to that slot's own question. A city, a
# project name or "linkedin" is NOT here — those are taken wherever the
# caller volunteers them (the Prakhar/Vapi case, RUNBOOK §54).
_EVERYDAY_ALIASES = frozenset({
    "ok", "okay", "okey", "fine", "good", "great", "nice", "bad", "poor",
    "normal", "average", "right", "correct", "sure", "yes", "yeah", "yep",
    "no", "nope", "so so", "not bad", "very good",
    "theek", "thik", "theek thaak", "theek hai", "thik hai", "accha",
    "achha", "acha", "sahi", "badhiya", "bahut accha", "haan", "han", "ha",
    "ji", "ji haan", "haan ji", "nahi", "nahin", "bilkul", "chalta hai",
    "ठीक", "ठीक-ठाक", "ठीक ठाक", "ठीक है", "ठीक था", "अच्छा", "अच्छा रहा",
    "बहुत अच्छा", "सही", "बढ़िया", "मस्त", "हाँ", "हां", "हाँ जी", "जी हाँ",
    "जी", "नहीं", "बिल्कुल", "चलता है", "सामान्य", "नॉट बैड", "गुड", "ओके",
    "હા", "ના", "જી", "સારું", "બરાબર", "ઠીક", "ચાલે",
})


def _is_everyday_alias(alias: str) -> bool:
    return " ".join((alias or "").lower().split()) in _EVERYDAY_ALIASES

# Correction markers for merge_policy: correction_gated — the caller is
# explicitly revising an earlier value, not just mentioning a new one.
# Script-aware edges, not `\b`: Python's \w excludes the Devanagari anusvara
# / matras, so `\bनहीं\b` never matched "नहीं," or "नहीं " — a correction_gated
# slot could not be corrected in Hindi at all (RUNBOOK §73).
_CORRECTION_MARKER_RE = re.compile(
    r"(?<![\w\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])"
    r"(actually|nahi|नहीं|नही|nahin|galat|गलत|ग़लत|wrong|change|correct|sorry|"
    r"i meant|make it|instead|update|मतलब|असल\s*में|दरअसल|"
    # "change" as Hindi callers say it — "बजट चेंज करके 70 लाख" was refused
    # on the Vedanta test (RUNBOOK §78) because only the Latin spelling and
    # the bare stem "बदल" (not "बदलकर") were known.
    r"चेंज|चेन्ज|चैंज|बदलकर|बदलना|बदल)"
    r"(?![\w\u0900-\u0963\u0966-\u097F\u0A80-\u0AFF])", re.IGNORECASE)

# ---------------------------------------------------------------------------
# Caller identity (2026-08-06)
#
# When the dialled contact's name is already known — an outbound list, or a
# contacts row for an inbound caller — the agent opens with it ("Am I speaking
# with Harshal?"). That name is authoritative. A live call proved what happens
# without this: ASR heard "मैं रवि रेडी हूँ" (all four Sarvam decodes of the
# original audio agree on "रवि"), the background slot-extractor read it as
# "I am Ravi", and because the agent's own `customer_name` collect slot had
# never been seeded from the CRM it counted as MISSING and was filled from that
# one token. The prompt then re-published it as "✓ ground truth — never re-ask",
# and the agent addressed Harshal as Ravi until he objected and hung up.
#
# So a known identity is LOCKED: background extraction (regex/LLM) may not
# overwrite it. Only two things can — an explicit answer to a name question the
# agent actually asked (source "system"/"answer"), or the caller explicitly
# saying to change it. Everything else is a mishearing by definition, because
# the caller already told us who they are.
_IDENTITY_SLOT_NAMES = frozenset({
    "name", "customer_name", "contact_name", "caller_name", "client_name",
    "full_name", "person_name", "lead_name", "customer", "your_name",
    "first_name", "prospect_name", "guest_name", "user_name",
})

# An explicit instruction to change the stored name — NOT a bare assertion.
# "मैं रवि रेडी हूँ" must not unlock; "actually my name is Ravi" must.
_NAME_CHANGE_RE = re.compile(
    r"(?:"
    r"\b(?:actually|sorry|no|not)\b[^.?!]{0,40}\b(?:name|naam)\b"
    r"|\b(?:my|the)\s+name\s+(?:is|are|isn'?t|is\s+not)\b"
    r"|\bname\s+(?:is\s+)?(?:wrong|incorrect|galat)\b"
    r"|\b(?:call|address)\s+me\b"
    r"|\bi'?m\s+not\b[^.?!]{0,30}\bi'?m\b"
    r"|\b(?:mera|मेरा)\s*(?:naam|नाम)\b"
    r"|\b(?:maru|મારું)\s*(?:naam|નામ)\b"
    r"|\b(?:naam|नाम|નામ)\s*(?:badal|बदल|બદલ|change|update)"
    r"|(?:नाम|નામ)\s*(?:है|छे|छ)\b"
    r")", re.IGNORECASE)


def is_identity_slot(slot_name: str) -> bool:
    """True for slots that hold WHO the caller is.

    Matched on the canonical name so agents built later — including anything
    the Copilot writes — are covered without needing a new flag in their YAML.
    """
    n = (slot_name or "").strip().lower()
    if n in _IDENTITY_SLOT_NAMES:
        return True
    # "customer_full_name", "contact person name", … — a name-ish tail with a
    # person-ish head. Deliberately narrow: "company_name"/"project_name"/
    # "policy_name" are NOT identities and must keep behaving exactly as before.
    return bool(re.fullmatch(
        r"(?:customer|client|caller|contact|lead|prospect|guest|person|user|your)"
        r"[_\s-]*(?:full[_\s-]*)?name", n))


def wants_name_change(utterance: str) -> bool:
    """True only when the caller explicitly asks us to use a different name."""
    return bool(_NAME_CHANGE_RE.search(utterance or ""))


def format_readback(value: Any, mode: str) -> str:
    """Format a captured value for spoken read-back confirmation (D5)."""
    s = str(value or "")
    if mode == "digit_groups_2":
        digits = re.sub(r"\D", "", s)
        if digits:
            return " ".join(digits[i:i + 2] for i in range(0, len(digits), 2))
    if mode == "spelled":
        # B3: local part letter-by-letter, domain spoken naturally —
        # 'ben@gmail.com' → 'b. e. n. — at — gmail dot com'.
        from .contact_capture import spelled_readback
        return spelled_readback(s)
    return s


@dataclass
class AuditEntry:
    """One state mutation — the audit trail the spec requires."""
    field: str
    old: Any
    new: Any
    source: str          # "regex" | "llm" | "legacy" | "system"
    turn: int
    timestamp: str       # ISO-8601
    utterance: str = ""  # snippet that caused the change

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Precompiled extractors (module-level: compiled once per process)
# ---------------------------------------------------------------------------

# Word-numbers common in Indian budget/config speech (romanized + Devanagari).
_WORD_NUM = {
    "ek": 1, "एक": 1, "one": 1, "do": 2, "दो": 2, "two": 2,
    "teen": 3, "tin": 3, "तीन": 3, "three": 3,
    "char": 4, "chaar": 4, "चार": 4, "four": 4, "paanch": 5, "panch": 5, "पांच": 5, "five": 5,
    "das": 10, "dus": 10, "दस": 10, "ten": 10, "bees": 20, "बीस": 20, "twenty": 20,
    "tees": 30, "तीस": 30, "thirty": 30, "chalis": 40, "chaalis": 40, "चालीस": 40, "forty": 40,
    "pachas": 50, "पचास": 50, "fifty": 50, "saath": 60, "साठ": 60, "sixty": 60,
    "sattar": 70, "सत्तर": 70, "seventy": 70, "assi": 80, "अस्सी": 80, "eighty": 80,
    "nabbe": 90, "नब्बे": 90, "ninety": 90, "sau": 100, "सौ": 100, "hundred": 100,
    "dedh": 1.5, "डेढ़": 1.5, "dhai": 2.5, "ढाई": 2.5,
}
_NUM_WORDS_ALT = "|".join(sorted(map(re.escape, _WORD_NUM), key=len, reverse=True))

# 1-6 BHK: "3bhk", "3 bhk", "teen bhk", "३ बीएचके", "3 bedroom", "2-3 bhk" (→ last)
_BHK_RE = re.compile(
    rf"(\d|[१-६]|{_NUM_WORDS_ALT})\s*[-–]?\s*(?:bhk|b\.h\.k|बीएचके|bedroom|बेडरूम|bed\b)",
    re.IGNORECASE,
)
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९", "0123456789")

# Budget: "50 lakh(s)", "पचास लाख", "1.2 crore", "50-60 lakh", "under 50L"
_BUDGET_RE = re.compile(
    rf"(\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?|{_NUM_WORDS_ALT})"
    r"\s*(lakh|lac|lakhs|लाख|l\b|crore|crores|cr\b|करोड़|करोड)",
    re.IGNORECASE,
)
_BUDGET_QUALIFIER_RE = re.compile(
    r"\b(under|below|upto|up to|tak|se kam|max|maximum|के अंदर|तक|around|approx|budget)\b",
    re.IGNORECASE,
)

_PROPERTY_TYPES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(apartment|flat|फ्लैट|अपार्टमेंट)\b", re.I), "Apartment"),
    (re.compile(r"\b(villa|bungalow|बंगला|विला|row ?house|kothi|कोठी)\b", re.I), "Villa/Bungalow"),
    (re.compile(r"\b(plot|zameen|jameen|ज़मीन|जमीन|प्लॉट|land)\b", re.I), "Plot/Land"),
    (re.compile(r"\b(shop|dukaan|dukan|दुकान|showroom|office|ऑफिस|commercial)\b", re.I), "Commercial"),
]

# Order matters: "Just exploring" MUST outrank the immediacy patterns so
# "koi jaldi nahi" / "bas dekh raha" never read as "Immediately". Bare
# "abhi"/"jaldi" are NOT immediacy — they need purchase context nearby
# ("abhi chahiye", "turant lena hai"); "abhi nahi" never matches at all.
_TIMELINE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(just (?:looking|exploring|enquiring)|bas dekh|सिर्फ देख|no hurry"
                r"|koi jaldi nahi|कोई जल्दी नहीं)\b", re.I), "Just exploring"),
    (re.compile(r"\b(immediately|asap|ready to (?:buy|book))\b", re.I), "Immediately"),
    (re.compile(r"\b(?:abhi|turant|अभी|तुरंत)(?!\s*(?:nahi|नहीं))\b.{0,16}?"
                r"\b(?:chahiye|chaahiye|lena|le\s*l|book|kharid|buy|चाहिए|लेना|ख़रीद|खरीद)", re.I), "Immediately"),
    (re.compile(r"\b(?:chahiye|lena|book|kharid|buy)\b.{0,12}?\b(?:abhi|turant)(?!\s*nahi)\b", re.I), "Immediately"),
    (re.compile(r"\b(this|is)\s*(month|महीने|mahine)\b", re.I), "This month"),
    (re.compile(r"\b(next|agle|अगले)\s*(month|महीने|mahine)\b", re.I), "Next month"),
    (re.compile(r"\b([1-9]|[१-९]|do|teen|char|छह|che)\s*[-–]?\s*(months?|महीने|mahino)\b", re.I), "{0} months"),
    (re.compile(r"\b(this|is)\s*(year|साल|saal)\b", re.I), "This year"),
    (re.compile(r"\b(next|agle|अगले)\s*(year|साल|saal)\b", re.I), "Next year"),
    (re.compile(r"\b(diwali|दिवाली|dussehra|दशहरा|holi|होली|navratri|नवरात्रि)\b", re.I), "Around {0}"),
]

_LOAN_YES_RE = re.compile(
    # Bare "hai/hoga" removed from the verb list: "loan available hai kya?"
    # is a QUESTION about loans, not a statement of needing one.
    r"\b(loan|लोन|emi|finance|financing|home ?loan|karza|कर्ज)\b.{0,24}?"
    r"\b(chahiye|lena|lunga|lungi|karna|karwana|required|need|want|चाहिए|लेना)\b"
    r"|\bwith\s+(?:a\s+)?loan\b|\bloan\s*(?:pe|par|पे|पर|se|से)\b",
    re.IGNORECASE,
)
# Question markers near a loan mention → the caller is ASKING about loans,
# not answering whether they need one. Don't fill the slot from questions.
_LOAN_QUESTION_RE = re.compile(
    r"\?|\b(kya|kitna|kitne|kaise|kab|milega|milta|interest|byaj|rate|क्या|कितना|कैसे|मिलेगा|ब्याज)\b",
    re.IGNORECASE,
)
_LOAN_NO_RE = re.compile(
    r"\b(cash|कैश|full payment|no loan|without loan|bina loan|बिना लोन|self fund|own fund|loan nahi|लोन नहीं)\b",
    re.IGNORECASE,
)

_PURPOSE_INVEST_RE = re.compile(
    r"\b(invest|investment|rental|kiraya|kiraye|किराया|किराए|rent (?:out|pe)|resale|returns?)\b", re.I)
_PURPOSE_SELF_RE = re.compile(
    r"\b(self ?use|khud|खुद|rehne|रहने|apne (?:liye|rehne)|family ke liye|own use|live|shift)\b", re.I)

_OCCUPATION_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(business|vyapar|व्यापार|businessman|self ?employed|entrepreneur|dukandar)\b", re.I), "Business"),
    (re.compile(r"\b(job|naukri|नौकरी|service|salaried|employee|company me|kaam karta)\b", re.I), "Salaried"),
    (re.compile(r"\b(doctor|डॉक्टर|engineer|इंजीनियर|ca\b|advocate|lawyer|teacher|professor)\b", re.I), "Professional"),
]

# Correction markers — "actually", "nahi X", "change kar do", "make it"
_NEGATED_VALUE_RE = re.compile(rf"(\d|{_NUM_WORDS_ALT})\s*(?:bhk|बीएचके)\s*(?:nahi|नहीं|nahin|not|मत)", re.I)


def _word_or_digit_to_num(tok: str) -> float | None:
    tok = tok.strip().lower().translate(_DEVANAGARI_DIGITS)
    if re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return float(tok)
    return _WORD_NUM.get(tok)


# ---------------------------------------------------------------------------
# The manager
# ---------------------------------------------------------------------------

def _word_in(needle: str, hay: str) -> bool:
    """`needle` occurs in `hay` as a whole word — boundaries are "not
    alphanumeric" rather than \\b so Devanagari / Gujarati text works too."""
    if not needle or not hay:
        return False
    start = 0
    while True:
        idx = hay.find(needle, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not hay[idx - 1].isalnum()
        after = idx + len(needle)
        after_ok = after >= len(hay) or not hay[after].isalnum()
        if before_ok and after_ok:
            return True
        start = idx + 1


class ConversationStateManager:
    """Per-call structured memory: extract → merge → detect gaps → drive prompts."""

    def __init__(
        self,
        slots: list[SlotSpec],
        locations: list[str] | None = None,
        location_aliases: list[str] | None = None,
        default_intent: str = "",
        language: str = "hi",
    ) -> None:
        # Deep-copy: the fallback slot sets are shared module-level objects,
        # and v2 hooks (require_now, precompiled patterns) mutate specs —
        # per-call managers must never leak mutations into each other.
        import copy as _copy
        self.slots: dict[str, SlotSpec] = {s.name: _copy.deepcopy(s) for s in slots}
        self.state: dict[str, Any] = {s.name: None for s in slots}
        if default_intent:
            self.state.setdefault("intent", default_intent)
        self.audit_log: list[AuditEntry] = []
        # Log an unknown collect-name once per manager, not once per turn.
        self._warned_unknown_collect: dict[str, bool] = {}
        self.language = language
        self.turn_index = 0
        self._pending_fields: set[str] = set()   # e.g. name awaiting confirmation
        # v2 bookkeeping — all additive:
        self.asked_count: dict[str, int] = {}    # slot -> times asked
        self._asked_turn: dict[str, int] = {}    # slot -> turn last counted
        self.skipped: set[str] = set()           # max_asks exhausted
        # A slot whose LAST permitted ask has just been chosen: not askable
        # again, but the caller has not answered yet — so it stays an
        # extraction target for the next turn and only becomes `skipped`
        # once that turn has passed unanswered (RUNBOOK §61, call 971).
        self._ask_exhausted: dict[str, int] = {}  # slot -> turn of last ask
        # The slot the engine says its spoken line actually asked about this
        # turn (set by observe_user's `asked=`), so a yes/no or an everyday
        # word can be read as the answer to THAT question and nothing else.
        self._asked_hint: str | None = None
        self.last_noted_ask: str | None = None
        # A branch said these do not apply to THIS caller (retire_slots).
        # Always a subset of `skipped`, so every 'leave missing / counts as
        # satisfied' path already holds; the subset carries the wording
        # ('not applicable', never 'not yet discussed') and the write refusal.
        self.retired: set[str] = set()
        self.readback_done: set[str] = set()     # read-back confirmed slots
        self.extra_flags: dict[str, Any] = {}    # e.g. whatsapp_promised (→ collected_data)
        # Identity slots seeded from a KNOWN contact (see _IDENTITY_SLOT_NAMES).
        # Empty for an unknown caller, so those calls keep today's behaviour
        # exactly — nothing is locked that was never known in the first place.
        self.identity_locked: set[str] = set()
        # Precompile per-slot patterns/synonyms (hot path stays <1 ms).
        for s in self.slots.values():
            s._compiled_patterns = []
            for p in (s.extract_patterns or [])[:8]:
                try:
                    s._compiled_patterns.append(re.compile(str(p)[:200], re.IGNORECASE))
                except re.error:
                    logger.warning("[STATE] slot %s pattern %r does not compile — skipped",
                                   s.name, p)
            lookup: dict[str, str] = {}
            for canon in (s.enum_values or []):
                lookup[str(canon).lower()] = str(canon)
            for canon, alts in (s.enum_synonyms or {}).items():
                canon_val = lookup.get(str(canon).lower(), str(canon))
                for alt in (alts if isinstance(alts, (list, tuple)) else [alts]):
                    lookup[str(alt).lower()] = canon_val
            s._synonym_lookup = lookup
        # Location matching material from the agent's knowledge base.
        self._locations = [l for l in (locations or []) if l]
        self._loc_lookup: dict[str, str] = {}
        for canon in self._locations:
            self._loc_lookup[canon.lower()] = canon
        aliases = [a.strip() for a in (location_aliases or []) if a and a.strip()]
        n_loc = len(self._locations)
        # Agent YAMLs list aliases in script-groups mirroring the locations
        # order (e.g. [surat, vapi, silvassa, સુરત, વાપી, સિલવાસા, सूरत, ...]),
        # so position-in-group canonicalizes non-Latin scripts too.
        grouped = n_loc > 0 and len(aliases) % n_loc == 0
        for i, alias in enumerate(aliases):
            a = alias.lower()
            best = self._loc_lookup.get(a) \
                or next((c for c in self._locations if c.lower().startswith(a[:3])), None) \
                or (self._locations[i % n_loc] if grouped else None)
            self._loc_lookup[a] = best or alias.title()

    # ------------------------------------------------------------------
    # 1. extract_entities — pure sync, <1 ms
    # ------------------------------------------------------------------
    def _untyped(self, name: str) -> bool:
        spec = self.slots.get(name)
        return spec is not None and not getattr(spec, "type", "")

    def _extract_typed(self, spec: SlotSpec, text: str, lower: str) -> Any:
        """v2 type-driven extraction for one slot. None = nothing found."""
        # Custom regexes win (first capture group, else whole match).
        for pat in spec._compiled_patterns:
            m = pat.search(text)
            if m:
                return (m.group(1) if m.groups() else m.group(0)).strip()
        t = spec.type
        if t in ("enum", "location"):
            # Latest mention wins (mirrors _match_location correction handling),
            # the LONGEST alias at a position wins over one it contains ("not
            # bad" is not "bad"), and word boundaries are script-aware —
            # str.isalnum() is False for every matra and anusvara, so "मिल"
            # used to match inside "मिली" (RUNBOOK §61).
            hit, hit_alias = self._latest_alias(spec, lower)
            if hit is None and t == "location":
                return self._match_location(lower)
            # An everyday word ("ठीक", "okay", "अच्छा", "सही") is a slot value
            # ONLY when it answers that slot's own question. Live 2026-09-26
            # (call 971): "हाँ ठीक रहेगा" — consent to a two-minute chat —
            # was stored as mahotsav_experience='Okay' on turn 4, the closed
            # slot then refused the caller's real "it was good" three
            # minutes later, and the agent read the wrong value back to him
            # ("अनुभव ठीक-ठाक रहा"). A distinctive alias (a city, a project,
            # "linkedin") is still taken wherever the caller volunteers it.
            if (hit is not None and t == "enum"
                    and _is_everyday_alias(hit_alias)
                    and not self._is_being_asked(spec.name)):
                logger.debug("[STATE] %s: everyday word %r ignored — the "
                             "slot was not asked", spec.name, hit_alias)
                return None
            return hit
        if t == "money":
            return self._extract_budget(text)
        if t == "number":
            matches = _SLOT_NUMBER_RE.findall(lower)
            return matches[-1] if matches else None
        if t == "phone":
            m = _SLOT_PHONE_RE.search(text)
            if m:
                raw = re.sub(r"[-.\s]", "", m.group(0))
                if raw.startswith("+91"):
                    raw = raw[3:]
                elif raw.startswith("91") and len(raw) > 10:
                    raw = raw[2:]
                if len(raw) == 10 and raw[0] in "6789":
                    return raw
            return None
        if t == "boolean":
            # Only when this slot is the one being asked — a stray "yes"
            # anywhere else must not fill it. Script-aware boundaries: `\b`
            # never matched "हाँ" or "नहीं" (the chandrabindu / anusvara is
            # not a word character), so Hindi yes/no answers were invisible
            # here and only the async LLM extractor could store them.
            if not self._is_being_asked(spec.name):
                return None
            # A yes/no slot with an authored vocabulary ("मिल गई" = Yes,
            # "ढूंढ रहा" = No) reads the answer through it first — the
            # caller rarely says a bare yes or no.
            if getattr(spec, "_synonym_lookup", None):
                canon, _ = self._latest_alias(spec, lower)
                if canon is not None:
                    return canon
            if _BOOL_YES_RE.search(lower):
                return "Yes"
            if _BOOL_NO_RE.search(lower):
                return "No"
            return None
        if t == "email":
            # B1 (2026-07-30): deterministic spoken-form assembly — the ONLY
            # writer for email slots (merge_state rejects source="llm").
            from .contact_capture import normalize_spoken_email
            return normalize_spoken_email(text)
        # text/date/time: deferred to the LLM gap-filler.
        return None

    @staticmethod
    def _latest_alias(spec: SlotSpec, lower: str) -> tuple[str | None, str]:
        """(canonical value, alias) of the LAST vocabulary mention in the
        utterance, or (None, ""). Latest wins so a correction ("Vapi nahi,
        Surat") lands on the corrected value; at one position the longest
        alias wins over any it contains ("not bad" is not "bad"); edges are
        script-aware (see _word_boundary_ok)."""
        hits: list[tuple[int, int, str, str]] = []   # (start, end, alias, canon)
        for alias, canon in (spec._synonym_lookup or {}).items():
            start = 0
            while True:
                idx = lower.find(alias, start)
                if idx < 0:
                    break
                end = idx + len(alias)
                if _word_boundary_ok(lower, idx, end):
                    hits.append((idx, end, alias, canon))
                start = idx + 1
        hits = [h for h in hits
                if not any(o is not h and o[0] <= h[0] and h[1] <= o[1]
                           and (o[1] - o[0]) > (h[1] - h[0]) for o in hits)]
        if not hits:
            return None, ""
        _, _, alias, canon = max(hits, key=lambda h: h[0])
        return canon, alias

    def _is_being_asked(self, name: str) -> bool:
        """Is `name` the question the caller is answering right now?

        Three sources, any one suffices: the engine's own reading of what
        its last spoken line asked (`asked=` on observe_user); the slot the
        prompt directive chose on the previous turn (`note_asked`, so a
        stage-scoped ask counts, not only the global-priority head); or the
        slot at the head of `missing`. An ask noted at turn 0 is a warm-up
        prompt build, not a question anyone heard — it never counts."""
        if self._asked_hint == name:
            return True
        t = self._asked_turn.get(name)
        if t is not None and t >= 1 and t == self.turn_index - 1:
            return True
        gaps = self.detect_missing_slots()
        return bool(gaps["missing"]) and gaps["missing"][0] == name

    def _just_asked(self, name: str) -> bool:
        """Stricter than `_is_being_asked` (RUNBOOK §75): the agent's LAST
        line asked `name` — the utterance hint, or the directive noted on
        the previous turn — never the head-of-missing fallback. The next
        question is not the current one: with the fallback, "हमें 3 BHK
        चाहिए, location बताओ" filled the size AND, in the same breath,
        decision_factor=Location, because the size's arrival made the
        factor the new head of the list."""
        if self._asked_hint == name:
            return True
        t = self._asked_turn.get(name)
        return t is not None and t >= 1 and t == self.turn_index - 1

    def extract_entities(self, text: str) -> dict[str, Any]:
        """Extract every recognizable slot value from one utterance."""
        if not text or not text.strip():
            return {}
        out: dict[str, Any] = {}
        lower = text.lower()

        # v2: type-driven extraction runs FIRST for typed slots; slots with
        # an empty type keep the legacy name-keyed dispatch byte-identically.
        for name, spec in self.slots.items():
            if not getattr(spec, "type", ""):
                continue
            try:
                val = self._extract_typed(spec, text, lower)
            except Exception:
                logger.exception("[STATE] typed extraction failed for %s", name)
                val = None
            if val is not None:
                out[name] = val

        if "location" in self.slots and self._untyped("location"):
            loc = self._match_location(lower)
            if loc:
                out["location"] = loc

        if (("bhk" in self.slots and self._untyped("bhk"))
                or ("configuration" in self.slots and self._untyped("configuration"))):
            bhk = self._extract_bhk(text)
            if bhk:
                out["bhk" if "bhk" in self.slots else "configuration"] = bhk

        if "property_type" in self.slots and self._untyped("property_type"):
            for pat, label in _PROPERTY_TYPES:
                if pat.search(text):
                    out["property_type"] = label
                    break

        if "budget" in self.slots and self._untyped("budget"):
            budget = self._extract_budget(text)
            if budget:
                out["budget"] = budget

        if "timeline" in self.slots and self._untyped("timeline"):
            tl = self._extract_timeline(text)
            if tl:
                out["timeline"] = tl

        if ("loan_required" in self.slots and self._untyped("loan_required")
                and not _LOAN_QUESTION_RE.search(text)):
            if _LOAN_NO_RE.search(text):
                out["loan_required"] = "No"
            elif _LOAN_YES_RE.search(text):
                out["loan_required"] = "Yes"

        if "purpose" in self.slots and self._untyped("purpose"):
            if _PURPOSE_INVEST_RE.search(text):
                out["purpose"] = "Investment"
            elif _PURPOSE_SELF_RE.search(text):
                out["purpose"] = "Self-use"

        if "occupation" in self.slots and self._untyped("occupation"):
            for pat, label in _OCCUPATION_PATTERNS:
                if pat.search(text):
                    out["occupation"] = label
                    break
        return out

    # -- individual extractors ----------------------------------------
    def _match_location(self, lower: str) -> str | None:
        """Latest MENTION in the utterance wins (corrections: 'Vapi nahi, Surat').

        Tracks the highest start index across ALL occurrences of ALL aliases —
        not dict order, which previously made 'Vapi nahi, Surat' resolve to
        the rejected city.
        """
        hit: str | None = None
        best_idx = -1
        for alias, canon in self._loc_lookup.items():
            start = 0
            while True:
                idx = lower.find(alias, start)
                if idx < 0:
                    break
                # word-boundary-ish check that also works for Devanagari (no \b)
                before_ok = idx == 0 or not lower[idx - 1].isalnum()
                after = idx + len(alias)
                after_ok = after >= len(lower) or not lower[after].isalnum()
                if before_ok and after_ok and idx > best_idx:
                    best_idx = idx
                    hit = canon
                start = idx + 1
        return hit

    @staticmethod
    def _extract_bhk(text: str) -> str | None:
        negated: set[float] = set()
        for m in _NEGATED_VALUE_RE.finditer(text):
            n = _word_or_digit_to_num(m.group(1))
            if n:
                negated.add(n)
        value: float | None = None
        for m in _BHK_RE.finditer(text):
            n = _word_or_digit_to_num(m.group(1))
            if n and 1 <= n <= 6 and n not in negated:
                value = n  # last non-negated mention wins
        if value is None:
            return None
        return f"{int(value)}BHK"

    @staticmethod
    def _extract_budget(text: str) -> str | None:
        result: str | None = None
        parts: list[tuple[float, str]] = []    # single amounts, in order
        for m in _BUDGET_RE.finditer(text):
            raw_num, unit = m.group(1), m.group(2).lower()
            unit_label = "Crore" if unit.startswith(("cr", "कर")) or unit == "करोड़" else "Lakhs"
            num_part = raw_num.strip().translate(_DEVANAGARI_DIGITS)
            if re.search(r"[-–]", num_part):
                lo, hi = re.split(r"\s*[-–]\s*", num_part)[:2]
                result = f"{lo.strip()}-{hi.strip()} {unit_label}"
                parts = []
            else:
                n = _word_or_digit_to_num(num_part)
                if n is None:
                    continue
                n_str = str(int(n)) if float(n).is_integer() else str(n)
                result = f"{n_str} {unit_label}"
                parts.append((float(n), unit_label))
        # "एक करोड़ बीस लाख" / "1 crore 20 lakh" is ONE amount — 1.2 crore, not
        # the 20 lakh of the last match alone. Call 1074 (2026-10-12) stored
        # "20 Lakhs" for a ₹1.2 Cr caller and the budget-fit check then told
        # her, after the visit was fixed, that the home did not fit.
        if len(parts) >= 2 and parts[-2][1] == "Crore" and parts[-1][1] == "Lakhs":
            total = parts[-2][0] * 100 + parts[-1][0]
            result = f"{total / 100.0:g} Crore"
        return result

    @staticmethod
    def _extract_timeline(text: str) -> str | None:
        for pat, template in _TIMELINE_PATTERNS:
            m = pat.search(text)
            if m:
                if "{0}" in template:
                    tok = m.group(1)
                    n = _word_or_digit_to_num(tok)
                    return template.format(int(n) if n else tok.title())
                return template
        return None

    # ------------------------------------------------------------------
    # 2. merge_state — null-protected, latest-wins, audited
    # ------------------------------------------------------------------
    def merge_state(
        self,
        extracted: dict[str, Any],
        source: str = "regex",
        utterance: str = "",
        fill_only: bool = False,
    ) -> list[AuditEntry]:
        """Merge extracted values into state.

        * None/empty extracted values are ignored (never overwrite with null).
        * Real values overwrite (latest user info wins) with an audit entry —
          unless fill_only=True (used by the async LLM extractor so a slow,
          possibly stale result can only fill gaps, never clobber).
        """
        changes: list[AuditEntry] = []
        for key, new in (extracted or {}).items():
            if new is None or (isinstance(new, str) and not new.strip()):
                continue
            if key not in self.state:
                continue  # unknown slot for this agent — ignore
            if key in self.retired:
                # A branch retired this slot for THIS caller — a candidate
                # with no job has no company, role, joining date or offer
                # letter. Live 2026-09-24 (call 934): the caller said on
                # turn 1 he was unemployed, and the office's record still
                # got company_name='होना है private limited',
                # designation='डेटा साइंस', joining_status='अभी बाकी है'.
                self.update_audit_log(
                    key, self.state.get(key), self.state.get(key), source,
                    f"REJECTED {source} write {new!r}: slot retired (not "
                    f"applicable to this caller)")
                continue
            if key in self._pending_fields:
                # Field is awaiting the caller's explicit confirmation (e.g.
                # name). No extractor — regex OR async LLM — may write it;
                # only set_externally (the confirmation path) can, which
                # clears the pending flag first. Prevents a rejected value
                # from resurrecting as confirmed ground truth.
                continue
            old = self.state.get(key)
            if old == new:
                continue
            if fill_only and old is not None:
                continue
            # v2 merge policies (default 'free' = current latest-wins). The
            # literal slot 'name' is hard-excluded — its confirmation flow
            # (_name_pending in conversation.py) stays untouched.
            spec = self.slots.get(key)
            policy = getattr(spec, "merge_policy", "free") if spec else "free"
            slot_type = getattr(spec, "type", "") if spec else ""
            # B1/B4 (2026-07-30): contact-typed slots have exactly one
            # writer — the deterministic capture path. A live call stored
            # the LLM's guess 'bell@gmail.com' (assembled from ASR "bell
            # www.gmail.com") as ground truth; fill_only then made it
            # immortal. LLM extraction may not touch these slots at all,
            # and no source may store a value that fails the validator.
            # The caller already told us who they are (CRM name, confirmed on
            # the opening line). Background extraction never gets to revise
            # that from a mis-heard token — only a name question the agent
            # actually asked ("system"/"answer") or an explicit request to
            # change it. See _IDENTITY_SLOT_NAMES for why this exists.
            if (key in self.identity_locked
                    and source not in ("system", "answer")
                    and not wants_name_change(utterance)):
                self.update_audit_log(
                    key, old, old, source,
                    f"REJECTED {source} write {new!r} (identity confirmed as "
                    f"{old!r}; no explicit name change): {(utterance or '')[:80]}")
                logger.info(
                    "[IDENTITY] kept %r, rejected %s guess %r from %r",
                    old, source, new, (utterance or "")[:60])
                continue
            if slot_type == "money" and source != "system":
                # A budget is an AMOUNT. The LLM gap-filler stored
                # budget_range='No' from "मैंने बोला कि मुझे नहीं देखना" (a
                # refusal to visit, call 1059) and the value then fed the
                # budget-fit check as if the caller had named a figure. Both
                # the value and the caller's words must carry a number or a
                # money unit.
                if not _looks_like_amount(str(new)) or (
                        utterance and not _looks_like_amount(utterance)):
                    self.update_audit_log(
                        key, old, old, source,
                        f"REJECTED {source} write {new!r}: not an amount "
                        f"(or not in what the caller said): "
                        f"{(utterance or '')[:80]}")
                    logger.info("[STATE] %s: rejected %s value %r — no "
                                "amount in %r", key, source, new,
                                (utterance or "")[:60])
                    continue
            if slot_type in ("email", "phone") and source in ("llm", "answer"):
                self.update_audit_log(
                    key, old, old, source,
                    f"REJECTED {source} write {new!r} (contact slots are "
                    f"deterministic-only)")
                continue
            # An ask-gated slot (RUNBOOK §75) is written only as the answer
            # to its own question, or on an explicit correction. Its
            # vocabulary is everyday domain words a caller also says in
            # QUESTIONS about the project: call 1052 flipped decision_factor
            # six times ("लोकेशन बता सकते हो" → Location, "एमेनिटीज़ के बारे
            # में बता सकते हो" → Amenities, "कंस्ट्रक्शन में है अभी?" →
            # Construction quality) and the LLM gap-filler wrote it from
            # "निवेश के लिए" two turns before it was asked.
            if (spec is not None and getattr(spec, "ask_gated", False)
                    and source not in ("system", "answer")
                    and not self._just_asked(key)
                    and not _CORRECTION_MARKER_RE.search(utterance or "")):
                self.update_audit_log(
                    key, old, old, source,
                    f"REJECTED {source} write {new!r}: ask-gated slot, not "
                    f"the answer to its question: {(utterance or '')[:80]}")
                logger.info("[STATE] %s: rejected %s value %r — ask-gated "
                            "slot was not being asked", key, source, new)
                continue
            # A slot with an authored vocabulary (enum_values / enum_synonyms)
            # is CLOSED for every writer — regex, LLM extractor, tool: the
            # canonical value is stored, or nothing is. Live 2026-09-24
            # (call 904, Prakhar): the tool wrote preferred_location='Chala'
            # — a locality of Vapi, so the `location: Vapi` match rule never
            # fired and the budget rule pushed a Surat project at a Vapi
            # caller — plus configuration_required='KBS' and
            # finalized_property='Bhagwati', STT noise the agent then treated
            # as facts it "remembered". Type decides whether the
            # deterministic extractor runs; the vocabulary decides what may
            # be stored. A slot with no vocabulary is untouched.
            if spec is not None and getattr(spec, "_synonym_lookup", None):
                canon = self._canonical_enum(spec, str(new))
                if canon is None:
                    self.update_audit_log(
                        key, old, old, source,
                        f"REJECTED {source} write {new!r}: not in the "
                        f"slot's vocabulary")
                    logger.info("[STATE] %s: rejected %s value %r — not one "
                                "of the slot's %d known values",
                                key, source, new, len(spec._synonym_lookup))
                    continue
                if canon == old:
                    continue
                new = canon
            if slot_type == "email":
                from .contact_capture import validate_email
                if not validate_email(str(new)):
                    self.update_audit_log(
                        key, old, old, source,
                        f"REJECTED invalid email {new!r}")
                    continue
                new = str(new).strip().lower()
            elif slot_type in ("date", "time") and source in ("llm", "answer",
                                                              "tool"):
                # A gap-filled date/time must at least parse-ish: carry a
                # digit or a recognizable day/time word ('after office' was
                # stored as preferred_time from a discovery answer).
                # "tool" joined this list 2026-09-18: with LLM_TOOLS_MODE=on
                # the main model's record_customer_details is the writer that
                # actually runs in production, and it stored the caller's
                # "Sure, we can do a call" as consultation_time — fill-only
                # then refused the real "Monday 11 am" one turn later. Native
                # script words too: a Hindi caller's "परसों शाम" carries no
                # Latin token and no digit.
                _sv = str(new).lower()
                if not (any(ch.isdigit() for ch in _sv)
                        or re.search(
                            r"\b(morning|afternoon|evening|noon|night|today|"
                            r"tomorrow|tonight|monday|tuesday|wednesday|"
                            r"thursday|friday|saturday|sunday|subah|dopahar|"
                            r"shaam|raat|aaj|kal|parso|savare|bapore|saanje)\b",
                            _sv)
                        or re.search(
                            r"आज|कल|परसों|परसो|सुबह|दोपहर|शाम|रात|बजे|सोमवार|"
                            r"मंगलवार|बुधवार|गुरुवार|शुक्रवार|शनिवार|रविवार|इतवार|"
                            r"આજે|કાલે|પરમ|સવારે|બપોરે|સાંજે|રાત્રે|વાગ્યે|સોમવાર|"
                            r"મંગળવાર|બુધવાર|ગુરુવાર|શુક્રવાર|શનિવાર|રવિવાર",
                            _sv)):
                    self.update_audit_log(
                        key, old, old, source,
                        f"REJECTED unparseable {slot_type} {new!r}")
                    continue
            if key != "name" and policy == "correction_gated" and old is not None:
                if not (_NEGATED_VALUE_RE.search(utterance or "")
                        or _CORRECTION_MARKER_RE.search(utterance or "")):
                    # Overwrite rejected — keep the confirmed value, audit it.
                    self.update_audit_log(
                        key, old, old, source,
                        f"REJECTED overwrite to {new!r} (no correction marker): "
                        f"{(utterance or '')[:80]}")
                    continue
            if key != "name" and policy == "confirm" and source != "system":
                # Captured but held for the caller's explicit confirmation;
                # conversation.py's generic confirm loop resolves it.
                self.state[key] = new
                self._pending_fields.add(key)
                entry = self.update_audit_log(
                    key, old, new, source, (utterance or "")[:120])
                changes.append(entry)
                continue
            self.state[key] = new
            if spec is not None and getattr(spec, "confirm_readback", ""):
                self.readback_done.discard(key)  # new value → read back again
            entry = self.update_audit_log(key, old, new, source, utterance)
            changes.append(entry)
        return changes

    # ------------------------------------------------------------------
    # 3. missing-slot detection
    # ------------------------------------------------------------------
    def _deps_met(self, name: str) -> bool:
        """All depends_on slots filled (or the slot has no dependencies)."""
        spec = self.slots.get(name)
        deps = getattr(spec, "depends_on", None) or [] if spec else []
        return all(self.state.get(d) is not None for d in deps if d in self.slots)

    def detect_missing_slots(self) -> dict[str, list[str]]:
        filled = [n for n, v in self.state.items()
                  if n in self.slots and v is not None]
        # v2 dependency gates: a required, unfilled slot whose depends_on are
        # not yet met is BLOCKED — never asked early, still filled
        # opportunistically when volunteered (extraction is ungated).
        # v2 ask fatigue: a slot in `skipped` (max_asks hit) leaves `missing`.
        # So does one whose last permitted ask was just spoken (`awaiting`):
        # never asked again, but still listened for — the extractor reads
        # `missing + awaiting` (RUNBOOK §61).
        awaiting = sorted(
            (n for n, t in self._ask_exhausted.items()
             if n in self.slots and self.state.get(n) is None
             and n not in self.skipped),
            key=lambda n: self.slots[n].priority)
        unfilled = [n for n, s in self.slots.items()
                    if s.required and self.state.get(n) is None
                    and n not in self._pending_fields
                    and n not in self.skipped
                    and n not in self._ask_exhausted]
        blocked = sorted((n for n in unfilled if not self._deps_met(n)),
                         key=lambda n: self.slots[n].priority)
        missing = sorted((n for n in unfilled if self._deps_met(n)),
                         key=lambda n: self.slots[n].priority)
        # Pending = captured but unconfirmed. Excluded from `missing` (don't
        # re-ask mid-confirmation) but NOT filled — "all collected" logic and
        # stage advancement must treat pending as incomplete.
        pending = sorted(
            (n for n in self._pending_fields if n in self.slots),
            key=lambda n: self.slots[n].priority,
        )
        return {"filled": filled, "missing": missing, "pending": pending,
                "blocked": blocked, "skipped": sorted(self.skipped),
                "awaiting": awaiting}

    def collect_satisfied(self, names: list[str]) -> bool:
        """Stage-advancement check: every named slot is filled (pending
        excluded) or skipped via max_asks (counts as satisfied).

        FAIL-CLOSED on a name that is not a defined slot. This used to
        `continue` — treating the unknown name as already satisfied — so a
        stage whose `collect` referenced slots that no longer existed
        returned vacuously True and advanced on its FIRST evaluation. That is
        not a hypothetical: a whole-list `slots` replacement orphaned the
        `collect` of hardik's EXISTING_COVERAGE stage and the entire
        existing-coverage qualification silently evaporated from live calls,
        with no error and no lint (2026-08-04 audit).

        A config mistake must degrade VISIBLY, not delete a stage. Holding is
        safe: `advance_when.max_turns` is an OR-fallback (conversation.py),
        so the stage still advances on its own turn budget.
        """
        for n in names or []:
            if n not in self.slots:
                if not self._warned_unknown_collect.get(n):
                    self._warned_unknown_collect[n] = True
                    logger.warning(
                        "[STATE] stage collects '%s' which is not a defined "
                        "slot — gate held (was silently satisfied before). "
                        "Fix the agent's slots or the stage's collect list.", n)
                return False
            if n in self._pending_fields:
                return False
            if (self.state.get(n) is None and n not in self.skipped
                    and n not in self._ask_exhausted):
                return False
        return True

    def require_now(self, name: str) -> None:
        """Branch action hook (capture_phone): make an optional slot required
        so it becomes the next ask."""
        spec = self.slots.get(name)
        if spec is not None:
            spec.required = True
            self.skipped.discard(name)

    def retire_slots(self, names: list[str], reason: str = "") -> list[str]:
        """Branch action hook (`retire_slots:a,b,c`): these slots do not
        apply to THIS caller. They leave `missing` (so the state block stops
        calling them a LATER part of the call), count as satisfied for the
        stage gate, read 'not applicable' in the summary and the record,
        and refuse every later write (merge_state). Idempotent; unknown
        names are ignored with a warning.

        Why a branch action and not `depends_on`: depends_on gates a slot
        on another slot being FILLED, not on its VALUE, so a survey whose
        two halves ('got a job' / 'still looking') never share a question
        could not be expressed — call 934 (2026-09-24, RUNBOOK §56) asked
        an unemployed caller which company he had joined, his designation,
        his joining date and whether the offer letter had come."""
        done: list[str] = []
        for raw in names or []:
            n = str(raw or "").strip()
            if not n:
                continue
            if n not in self.slots:
                logger.warning("[STATE] retire_slots names '%s', not a defined "
                               "slot — ignored", n)
                continue
            if n in self.retired:
                continue
            self.retired.add(n)
            self.skipped.add(n)
            self._pending_fields.discard(n)
            self._ask_exhausted.pop(n, None)
            self.update_audit_log(
                n, self.state.get(n), self.state.get(n), "branch",
                f"RETIRED — {reason or 'not applicable to this caller'}")
            done.append(n)
        if done:
            logger.info("[STATE] retired %s (%s)", done,
                        reason or "not applicable to this caller")
        return done

    def next_missing_slot(self) -> SlotSpec | None:
        missing = self.detect_missing_slots()["missing"]
        return self.slots[missing[0]] if missing else None

    # ------------------------------------------------------------------
    # 4. dynamic question generation
    # ------------------------------------------------------------------
    def generate_next_question(self, language: str | None = None,
                               only: str | None = None) -> str | None:
        """Phrasing for the next slot question.

        `only` names the slot to phrase — passed when the CURRENT stage's
        collect list, not global priority, decided which one to ask. Without
        it the suggested phrasing could belong to a different slot than the
        directive above it named.
        """
        slot = self.slots.get(only) if only else self.next_missing_slot()
        if slot is None:
            return None
        lang = language or self.language
        # v2: second-attempt phrasing after the first ask, when provided.
        asked = self.asked_count.get(slot.name, 0)
        if asked >= 1 and slot.reask_question:
            q = (slot.reask_question.get(lang) or slot.reask_question.get("en")
                 or next(iter(slot.reask_question.values()), ""))
            if q:
                return q
        q = slot.question.get(lang) or slot.question.get("en") or ""
        return q or f"Ask the caller about their {slot.display().lower()}."

    @staticmethod
    def _stage_missing(missing: "list[str]",
                       stage_slots: "list[str] | None") -> "list[str]":
        """`missing`, narrowed to this stage's collect list.

        Returns the full list when the stage declares no slots (legacy flows
        keep global priority order, byte-for-byte), and [] when the stage
        declares slots and every one of them is already answered — the caller
        then falls through to the softer "weave one in" directive instead of
        being marched into the next stage's questions early.
        """
        if not stage_slots:
            return list(missing)
        wanted = {str(s) for s in stage_slots}
        return [m for m in missing if m in wanted]

    def note_asked(self, name: str) -> None:
        """Ask-fatigue bookkeeping (v2): called at question-selection time.
        At max_asks (when >0) the slot moves to `skipped` — excluded from
        `missing`, satisfied for stage advancement.

        Counted at most ONCE per turn_index: this runs on every prompt BUILD
        (retries/rebuilds included), and per-build counting would burn a
        slot's max_asks budget without the agent ever voicing a question."""
        spec = self.slots.get(name)
        if spec is None:
            return
        if self._asked_turn.get(name) == self.turn_index:
            return
        self._asked_turn[name] = self.turn_index
        self.last_noted_ask = name
        self.asked_count[name] = self.asked_count.get(name, 0) + 1
        max_asks = getattr(spec, "max_asks", 0) or 0
        if max_asks > 0 and self.asked_count[name] >= max_asks and \
                self.state.get(name) is None:
            # Not `skipped` yet: the question is being asked THIS turn and
            # the caller has not had a chance to answer. Live 2026-09-26
            # (call 971): every max_asks=1 slot moved to `skipped` here, the
            # background extractor reads only `missing`, so the caller's
            # "नहीं, अभी तक नहीं आई" (company_contacted) and "रोजगार की मदद
            # चाहूंगा" (future_assistance) were heard, answered and never
            # stored. It settles on the caller's NEXT turn (observe_user).
            self._ask_exhausted[name] = self.turn_index
            logger.info("[STATE] slot %s at max_asks=%d — last ask; skipped "
                        "if unanswered", name, max_asks)

    def unnote_asked(self, name: str) -> None:
        """Undo this turn's `note_asked` — the agent's spoken line asked a
        DIFFERENT question than the directive suggested, so the budget for
        this one was never spent. Call 971: the directive said issues_faced,
        the model asked about the Mahotsav experience instead (the stage
        goal's first beat), and issues_faced was retired unvoiced."""
        if self._asked_turn.get(name) != self.turn_index:
            return
        self._asked_turn.pop(name, None)
        self.asked_count[name] = max(0, self.asked_count.get(name, 0) - 1)
        self._ask_exhausted.pop(name, None)
        if self.last_noted_ask == name:
            self.last_noted_ask = None
        logger.info("[STATE] slot %s not actually asked this turn — ask "
                    "budget restored (count=%d)", name, self.asked_count[name])

    # How many times one slot may have its last ask given back because the
    # caller replied with a question / confusion / hello instead of an answer.
    NON_ANSWER_GRACE = 2

    def _keep_ask_budget(self) -> None:
        """The caller's reply to our last question was not an answer (they
        asked something back, were confused, or checked the line). The ask
        just spent did not get a chance — give it back, so the slot is asked
        again after the agent clears up the confusion instead of being
        skipped (RUNBOOK §69, call 1032: three questions skipped this way)."""
        grace = self.__dict__.setdefault("_non_answer_grace", {})
        for name, asked_at in list(self._ask_exhausted.items()):
            if self.state.get(name) is not None or asked_at < self.turn_index - 1:
                continue
            if grace.get(name, 0) >= self.NON_ANSWER_GRACE:
                continue
            grace[name] = grace.get(name, 0) + 1
            self._ask_exhausted.pop(name, None)
            self.asked_count[name] = max(0, self.asked_count.get(name, 0) - 1)
            logger.info("[STATE] slot %s: the caller did not answer (question/"
                        "confusion) — ask budget kept (grace %d/%d)",
                        name, grace[name], self.NON_ANSWER_GRACE)

    def _settle_asks(self) -> None:
        """Promote exhausted-and-still-unanswered slots to `skipped` once
        the caller has had a full turn (and the async extractor a chance)
        to answer; drop the bookkeeping for ones that got filled."""
        for name, asked_at in list(self._ask_exhausted.items()):
            if self.state.get(name) is not None:
                self._ask_exhausted.pop(name, None)
                continue
            if asked_at < self.turn_index - 1:
                self._ask_exhausted.pop(name, None)
                self.skipped.add(name)
                logger.info("[STATE] slot %s asked its %d permitted time(s) "
                            "and went unanswered — skipped",
                            name, self.asked_count.get(name, 0))

    # ------------------------------------------------------------------
    # 5. prompt context — THE single authoritative state block
    # ------------------------------------------------------------------
    def build_prompt_context(self, language: str | None = None,
                             stage_discovery: bool = True,
                             stage_slots: "list[str] | None" = None,
                             stage_asks: bool = True) -> str:
        """The single authoritative state block for the system prompt.

        Ground truth for FACTS always; the next-ACTION directive is
        stage-aware — during discovery it demands the one next question,
        after discovery it defers to the stage goal so booking/closing flows
        are never deadlocked by an unfilled optional-ish slot.

        `stage_slots` is the CURRENT stage's `collect` list. When the author
        declared one, the next question is chosen from it rather than from
        global slot priority. Without that scoping the agent runs ahead of
        its own flow: it asks a LATER stage's question because that slot
        happens to be the next one missing, the caller answers, and then that
        stage arrives and its script asks the same question again. Measured
        live 2026-09-15 (call 851) — "Could you let us know how fast you
        could join us?" / "immediately" / "Okay, so just to confirm, could
        you let me know of the roles that you are interested in, and also how
        fast you could join us?" nine seconds later.
        """
        gaps = self.detect_missing_slots()
        lines = ["━━ CONVERSATION STATE (ground truth for FACTS — never re-ask ✓ fields) ━━"]
        # Compliance outranks the sales goal. _detect_dnd_request already
        # RECORDS the opt-out, but the spoken reply was left entirely to the
        # model on the assumption that "the prompt already handles polite
        # disengagement". Adversarial testing on 2026-08-04 disproved that:
        # after "stop calling me" the agent answered "We work with all major
        # providers... it's always worth a quick comparison." Once the caller
        # has opted out there is no pitch left to make.
        if self.extra_flags.get("dnd_requested"):
            lines.append(
                "⛔ THIS CALLER HAS ASKED NOT TO BE CONTACTED. Acknowledge it, "
                "confirm they will be removed, apologise briefly and close the "
                "call. Do NOT pitch, do NOT ask another qualifying question, do "
                "NOT offer a callback or 'one quick point' — no matter what the "
                "stage goal below says.")
        for name, spec in sorted(self.slots.items(), key=lambda kv: kv[1].priority):
            val = self.state.get(name)
            if name in self._pending_fields:
                lines.append(f"  ? {spec.display()}: awaiting caller confirmation")
            elif val is not None:
                lines.append(f"  ✓ {spec.display()}: {val}")
            elif name in self.retired:
                # Retired by a branch: not a fact to learn, not a gap. Listing
                # it as Unknown is what told the model a jobless caller's
                # "company" was still an open question (call 934).
                continue
            elif spec.required:
                lines.append(f"  ✗ {spec.display()}: Unknown")
        if gaps["missing"]:
            pretty = ", ".join(self.slots[m].display() for m in gaps["missing"])
            lines.append(f"MISSING (priority order): {pretty}")
        lines.append(f"SUMMARY: {self.running_summary()}")
        lines.append("RULES:")
        lines.append("  • NEVER ask about any ✓ field — the caller already told you. "
                      "Acknowledge ✓ facts naturally when relevant.")
        # Without this the model still "corrects" the name mid-call off a
        # mis-heard token, even though merge_state refuses to store it — and a
        # caller who is addressed by the wrong name does not care which layer
        # invented it.
        _locked = [n for n in self.identity_locked if self.state.get(n)]
        if _locked:
            lines.append(
                f"  • The caller's name is already confirmed: "
                f"{self.state[_locked[0]]}. Keep using it. Do NOT infer a "
                f"different name from anything you hear — names inside a "
                f"sentence are usually mis-heard words. Change it ONLY if the "
                f"caller explicitly says it is wrong or asks to be called "
                f"something else.")
        # v2: read-back instruction for filled-but-unconfirmed readback slots.
        readback_due = [
            n for n, spec in self.slots.items()
            if getattr(spec, "confirm_readback", "")
            and self.state.get(n) is not None
            and n not in self.readback_done
            and n not in self._pending_fields
        ]
        for n in sorted(readback_due, key=lambda x: self.slots[x].priority):
            spec = self.slots[n]
            spoken = format_readback(self.state.get(n), spec.confirm_readback)
            lines.append(f"  • READ BACK the {spec.display().lower()} ONCE to confirm it — "
                         f"say it as: \"{spoken}\" — then continue.")
            self.readback_done.add(n)  # instruction is emitted exactly once
        if gaps["pending"]:
            # A confirmation is in flight (e.g. name). That confirmation IS
            # the next action — never stack a second question on top of it.
            pretty_p = ", ".join(self.slots[p].display() for p in gaps["pending"])
            for p in gaps["pending"]:
                spec = self.slots[p]
                if p != "name" and getattr(spec, "merge_policy", "") == "confirm":
                    spoken = format_readback(self.state.get(p),
                                             getattr(spec, "confirm_readback", ""))
                    lines.append(f"  • CONFIRM the {spec.display().lower()} the caller gave "
                                 f"(\"{spoken}\") — ask if it's correct, nothing else first.")
            lines.append(f"  • Awaiting confirmation of: {pretty_p}. Confirm that FIRST — "
                         "do not ask any other question this turn.")
        elif not stage_asks:
            # This step of the call is not for questions — a greeting, an
            # identity confirmation, an availability check, a close. Without
            # this branch the directive below hands it the next missing slot
            # and the model asks THAT instead of doing the step: measured
            # live 2026-09-16, the agent answered "am I speaking to Piyush?"
            # and went straight to "what are your core technical skills?",
            # never introducing itself and never asking for the caller's
            # introduction, because CONVERSATION STATE outranks a stage goal.
            lines.append(
                "  • This step is NOT for collecting information. Do NOT ask "
                "about any of the fields above here — the later steps that "
                "own them ask them themselves. Do only what the CURRENT GOAL "
                "says.")
        elif gaps["missing"] and stage_discovery and self._stage_missing(
                gaps["missing"], stage_slots):
            _scoped = self._stage_missing(gaps["missing"], stage_slots)
            first_name = _scoped[0]
            nq = self.generate_next_question(language, only=first_name)
            first = self.slots[first_name].display()
            self.note_asked(first_name)
            lines.append(f"  • Ask ONLY ONE question next, about: {first}."
                         + (f" Suggested phrasing: {nq}" if nq else ""))
            _later = [m for m in gaps["missing"] if m not in _scoped]
            if _later:
                lines.append(
                    "  • Questions belonging to a LATER part of the call: "
                    + ", ".join(self.slots[m].display() for m in _later)
                    + ". Do NOT ask them yet — that part of the flow asks "
                      "them itself, and asking early means the caller is "
                      "asked twice.")
        elif gaps["missing"]:
            pretty = ", ".join(self.slots[m].display() for m in gaps["missing"])
            lines.append(f"  • Still unknown: {pretty}. Weave in AT MOST one of these "
                         "only if it fits naturally — the CURRENT GOAL comes first.")
        else:
            lines.append("  • All required information is collected — do not re-ask "
                         "anything; progress the conversation per the CURRENT GOAL.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 6. audit log
    # ------------------------------------------------------------------
    def update_audit_log(
        self, field_name: str, old: Any, new: Any, source: str, utterance: str = "",
    ) -> AuditEntry:
        entry = AuditEntry(
            field=field_name, old=old, new=new, source=source,
            turn=self.turn_index,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            utterance=(utterance or "")[:120],
        )
        self.audit_log.append(entry)
        logger.info("[STATE] %s: %r → %r (source=%s turn=%d)",
                    field_name, old, new, source, self.turn_index)
        return entry

    # ------------------------------------------------------------------
    # Convenience / lifecycle
    # ------------------------------------------------------------------
    def observe_user(self, text: str,
                     asked: str | None = None,
                     non_answer: bool = False) -> list[AuditEntry]:
        """The per-turn hot-path entry point: extract + merge in one call.

        `asked` is the slot the engine believes its last spoken line asked
        about (from the utterance itself, not the directive) — a yes/no or
        an everyday word in this turn is read as the answer to THAT."""
        self.turn_index += 1
        self._asked_hint = asked if asked in self.slots else None
        if non_answer:
            self._keep_ask_budget()
        self._settle_asks()
        extracted = self.extract_entities(text)
        return self.merge_state(extracted, source="regex", utterance=text)

    def options_asked(self, agent_text: str) -> str | None:
        """The enum slot whose OPTIONS the agent's question lists — "3 BHK या
        4 BHK?", "लोकेशन, साइज़, amenities या construction quality?". A line
        that offers two or more of a slot's own values as the choice asked
        THAT slot, however the model worded the rest (calls 1080/1081: the
        reworded factor question was not recognised and the answer was
        refused as "not asked"). Only the question sentence is read; filled
        slots are skipped; the slot with the most options listed wins."""
        txt = (agent_text or "").strip()
        if not txt:
            return None
        parts = re.split(r"(?<=[?।.!])\s+", txt)
        qs = [p for p in parts if "?" in p] or ([parts[-1]] if parts else [])
        q = (qs[-1] if qs else "").lower()
        if not q:
            return None
        best, best_n = None, 1
        for name, spec in self.slots.items():
            if self.state.get(name) is not None or name in self.retired:
                continue
            lookup = getattr(spec, "_synonym_lookup", None) or {}
            if len(set(lookup.values())) < 2:
                continue
            hit: set[str] = set()
            for alias, canon in lookup.items():
                if len(alias) < 3:
                    continue
                i = q.find(alias)
                while i >= 0:
                    if _word_boundary_ok(q, i, i + len(alias)):
                        hit.add(canon)
                        break
                    i = q.find(alias, i + 1)
            if len(hit) > best_n:
                best, best_n = name, len(hit)
        return best

    def _canonical_enum(self, spec: SlotSpec, raw: str) -> str | None:
        """The canonical vocabulary value a written string means, or None.

        Exact alias first; else the LONGEST alias that occurs in the value as
        a whole word ("Aranya II" beats "Aranya"; "chala, vapi" → Vapi); a
        location-typed slot finally consults the knowledge base's own
        location aliases. Whole-word so "Pal" never matches inside "Palanpur"
        and "office" never inside "officer"."""
        norm = " ".join(str(raw or "").lower().split()).strip(" .,!?;:")
        if not norm:
            return None
        lookup = spec._synonym_lookup or {}
        if norm in lookup:
            return lookup[norm]
        best, best_len = None, 0
        for alias, canon in lookup.items():
            if len(alias) > best_len and _word_in(alias, norm):
                best, best_len = canon, len(alias)
        if best is not None:
            return best
        if getattr(spec, "type", "") == "location":
            return self._match_location(norm)
        return None

    def set_externally(self, field_name: str, value: Any, source: str = "system") -> None:
        """Write-through for values captured by other subsystems (name flow,
        appointment slot matcher, legacy extractors)."""
        if field_name not in self.state:
            return
        self._pending_fields.discard(field_name)
        if value is None or self.state.get(field_name) == value:
            return
        old = self.state.get(field_name)
        self.state[field_name] = value
        self.update_audit_log(field_name, old, value, source)

    def seed_identity(self, value: str, source: str = "crm") -> list[str]:
        """Seed every identity slot with the KNOWN contact name and lock it.

        Called once per call, before the greeting, whenever the platform
        already knows who it dialled (outbound list) or who is calling
        (contacts row). Two things happen, and both matter:

          * the slot stops being MISSING, so the background extractor — which
            only ever targets missing slots — never goes looking for a name;
          * the slot is locked, so nothing but an explicit correction can
            change it (see merge_state).

        Only fills slots that are still empty: a value already captured this
        call is the caller's own words and outranks the CRM.
        """
        value = (value or "").strip()
        if not value:
            return []
        seeded: list[str] = []
        for name in self.slots:
            if not is_identity_slot(name):
                continue
            self.identity_locked.add(name)
            if self.state.get(name) is None:
                self.state[name] = value
                self.update_audit_log(name, None, value, source,
                                      "known contact — seeded before greeting")
                seeded.append(name)
        if seeded:
            logger.info("[IDENTITY] seeded+locked %s = %r (%s)",
                        ", ".join(seeded), value, source)
        elif self.identity_locked:
            logger.info("[IDENTITY] locked %s (already held a value)",
                        ", ".join(sorted(self.identity_locked)))
        return seeded

    def unlock_identity(self, reason: str = "") -> None:
        """Release the lock — the caller explicitly asked us to use a
        different name, so subsequent turns may refine it normally."""
        if self.identity_locked:
            logger.info("[IDENTITY] unlocked (%s)", reason or "caller request")
        self.identity_locked.clear()

    def mark_pending(self, field_name: str) -> None:
        """Field captured but awaiting caller confirmation (e.g. name)."""
        if field_name in self.state:
            self._pending_fields.add(field_name)

    def clear_pending(self, field_name: str) -> None:
        self._pending_fields.discard(field_name)

    def discard_pending(self, field_name: str) -> None:
        """Caller REJECTED the pending value: clear the flag AND null any
        value that leaked into the slot, with an audit entry."""
        self._pending_fields.discard(field_name)
        old = self.state.get(field_name)
        if field_name in self.state and old is not None:
            self.state[field_name] = None
            self.update_audit_log(field_name, old, None, "system", "caller rejected value")

    def running_summary(self) -> str:
        """Rolling template summary — rebuilt each turn from state, no LLM."""
        parts: list[str] = []
        known: list[str] = []
        cfg = self.state.get("bhk") or self.state.get("configuration")
        ptype = self.state.get("property_type")
        loc = self.state.get("location")
        budget = self.state.get("budget")
        if cfg or ptype or loc or budget:
            frag = "Customer wants"
            frag += f" a {cfg}" if cfg else " a property"
            if ptype:
                frag += f" {ptype.lower()}"
            if loc:
                frag += f" in {loc}"
            if budget:
                frag += f" under {budget}" if "-" not in str(budget) else f" in the {budget} range"
            parts.append(frag + ".")
        for name, spec in sorted(self.slots.items(), key=lambda kv: kv[1].priority):
            if name in ("bhk", "configuration", "property_type", "location", "budget"):
                continue
            if name in self._pending_fields:
                known.append(f"{spec.display()} awaiting caller confirmation.")
                continue
            val = self.state.get(name)
            if val is not None:
                known.append(spec.summary_known.format(value=val) if spec.summary_known
                             else f"{spec.display()}: {val}.")
            elif name in self.retired:
                known.append(f"{spec.display()}: not applicable.")
            elif spec.required:
                parts_unknown = spec.summary_unknown or f"{spec.display()} not yet discussed."
                known.append(parts_unknown)
        parts.extend(known)
        return " ".join(parts) if parts else "No details collected yet."

    def to_dict(self) -> dict:
        gaps = self.detect_missing_slots()
        out = {
            "state": dict(self.state),
            "filled": gaps["filled"],
            "missing": gaps["missing"],
            "summary": self.running_summary(),
            "audit_log": [a.to_dict() for a in self.audit_log],
            "turns_observed": self.turn_index,
            # v2 — additive keys only (snapshot consumers rely on the rest).
            # A slot still awaiting its answer when the call ends was asked
            # and unanswered — the record says `skipped`, as before.
            "skipped": sorted(set(gaps["skipped"]) | set(gaps["awaiting"])),
            "blocked": gaps["blocked"],
            "retired": sorted(self.retired),
        }
        if self.extra_flags:
            out["flags"] = dict(self.extra_flags)
        return out

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Per-agent slot sets
# ---------------------------------------------------------------------------

_Q = dict  # brevity below

_REAL_ESTATE_SLOTS: list[SlotSpec] = [
    SlotSpec("name", 10, True, "Name",
             _Q(hi="क्या मैं आपका नाम जान सकती हूँ?", en="May I know your name?"),
             summary_unknown="Name not yet captured."),
    SlotSpec("location", 20, True, "Location",
             _Q(hi="आप किस city में property देख रहे हैं?", en="Which city are you looking in?")),
    SlotSpec("bhk", 30, True, "Configuration",
             _Q(hi="आपको कितने BHK चाहिए?", en="What configuration are you looking for — 2BHK, 3BHK?")),
    SlotSpec("property_type", 35, False, "Property Type",
             _Q(hi="Flat चाहिए या villa/plot?", en="Apartment, villa or plot?")),
    SlotSpec("budget", 40, True, "Budget",
             _Q(hi="आपका comfortable budget क्या है?", en="What budget range are you comfortable with?")),
    SlotSpec("purpose", 50, False, "Purpose",
             _Q(hi="खुद रहने के लिए या investment के लिए?", en="Is this for self-use or investment?")),
    SlotSpec("timeline", 60, True, "Timeline",
             _Q(hi="आप कब तक purchase करने का सोच रहे हैं?", en="When are you planning to purchase?"),
             summary_unknown="Purchase timeline not yet discussed."),
    SlotSpec("loan_required", 70, True, "Loan Required",
             _Q(hi="क्या आप home loan लेने का सोच रहे हैं?", en="Will you be taking a home loan?"),
             summary_unknown="Loan requirement not yet discussed."),
    SlotSpec("occupation", 80, False, "Occupation",
             _Q(hi="आप क्या काम करते हैं?", en="What do you do professionally?")),
]

_MEETING_SLOTS: list[SlotSpec] = [
    SlotSpec("name", 10, True, "Name",
             _Q(hi="क्या मैं आपका नाम जान सकती हूँ?", en="May I know your name?"),
             summary_unknown="Name not yet captured."),
    # Deliberately NOT named "timeline": that would reuse the purchase-timeline
    # regexes and let small talk ("next month India aa raha hoon") permanently
    # fill the meeting time. This slot is filled from the appointment-booking
    # write-through (_sync_state_manager) or the LLM gap-filler only.
    SlotSpec("meeting_time", 20, True, "Meeting Time",
             _Q(hi="कौन सा दिन और time आपके लिए ठीक रहेगा?", en="What day and time works for you?"),
             summary_unknown="Meeting time not yet agreed."),
]


def build_state_manager(agent_def, language: str = "hi") -> ConversationStateManager:
    """Factory: build a per-call manager from the agent definition.

    Priority: explicit agent YAML `slots:` → derived default by agent type
    (real-estate agents get the full property slot set; others get the
    meeting/name set). qa_field_signals keys decide the agent type.
    """
    slots: list[SlotSpec] = []
    for s in getattr(agent_def, "slots", None) or []:
        # Copy ALL SlotDefinition fields (summary_known/unknown used to be
        # dropped here — the plan's L4 finding).
        _type = getattr(s, "type", "") or ""
        _policy = getattr(s, "merge_policy", "free") or "free"
        _readback = getattr(s, "confirm_readback", "") or ""
        _max_asks = int(getattr(s, "max_asks", 0) or 0)
        # B6 engine-level contact provisioning (2026-07-30, platform-wide —
        # every existing and future agent): a slot NAMED email is email-typed
        # even without YAML migration (no legacy name-keyed email extractor
        # exists, so typing it costs nothing), and contact-typed slots get
        # the confirm/read-back/cap machinery unless the YAML says otherwise.
        # This is what makes captured emails pending-until-confirmed and
        # correctable instead of first-guess-immortal.
        # "text" counts as untyped here: it is the generic default the
        # Copilot used to stamp on every slot (the live LesCall agent's
        # email slot is `type: text`), not a deliberate opt-out.
        if _type in ("", "text") and s.name.strip().lower() in (
                "email", "email_id", "email_address"):
            _type = "email"
        if _type == "email":
            # Email is the incident class: pending-until-confirmed, spelled
            # read-back, capped asks. Phone deliberately keeps its authored
            # policy — live outbound flows capture numbers passively and a
            # forced confirm gate could stall their stage advancement; it
            # only gains the digit read-back hint when none is set.
            if _policy == "free":
                _policy = "confirm"
            if not _readback:
                _readback = "spelled"
            if _max_asks <= 0:
                _max_asks = 2
        elif _type == "phone" and not _readback:
            _readback = "digit_groups_2"
        slots.append(SlotSpec(
            name=s.name, priority=s.priority, required=s.required,
            label=getattr(s, "label", "") or "",
            question=dict(getattr(s, "question", {}) or {}),
            summary_known=getattr(s, "summary_known", "") or "",
            summary_unknown=getattr(s, "summary_unknown", "") or "",
            type=_type,
            enum_values=list(getattr(s, "enum_values", None) or []),
            enum_synonyms=dict(getattr(s, "enum_synonyms", None) or {}),
            extract_patterns=list(getattr(s, "extract_patterns", None) or []),
            depends_on=list(getattr(s, "depends_on", None) or []),
            merge_policy=_policy,
            ask_gated=bool(getattr(s, "ask_gated", False)),
            confirm_readback=_readback,
            max_asks=_max_asks,
            reask_question=dict(getattr(s, "reask_question", None) or {}),
        ))
    if not slots:
        qa_keys = set((agent_def.knowledge_base.qa_field_signals or {}).keys())
        stage_ids = {s.id for s in agent_def.call_flow.stages}
        # v2 role shim: custom-id stages with an explicit scheduling/
        # confirmation role also count as booking flows (the id-literal set
        # stays as the fallback for legacy agents).
        from .stage_roles import stage_role as _stage_role_of
        stage_roles = {_stage_role_of(s, s.id) for s in agent_def.call_flow.stages}
        if "bhk" in qa_keys:
            # BHK is the real-estate discriminator; location/budget alone
            # appear in clinics and product-sales agents too.
            slots = list(_REAL_ESTATE_SLOTS)
        elif (stage_ids & {"INVITE_VISIT", "SLOT_BOOKING", "CONFIRMATION"}
              or stage_roles & {"scheduling", "confirmation"}):
            # Books meetings/appointments → track name + agreed time.
            slots = list(_MEETING_SLOTS)
        else:
            # Pure pitch agents (no booking stage): only track the name —
            # never pressure the LLM to ask for a meeting time that the
            # call flow has no stage to handle.
            slots = [s for s in _MEETING_SLOTS if s.name == "name"]
    kb = agent_def.knowledge_base
    slot_names = {s.name for s in slots}
    default_intent = "buy_property" if {"location", "bhk"} & slot_names else ""
    return ConversationStateManager(
        slots=slots,
        locations=list(kb.locations or []),
        location_aliases=list(kb.location_aliases or []),
        default_intent=default_intent,
        language=language,
    )
