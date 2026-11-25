"""Budget-fit suggestion + caller-confirmed pitch switch (RUNBOOK §68).

An agent with `knowledge_base.primary_project` pitches ONE project. Opt-in
with `primary_project.budget_switch: true` and, when the caller's budget
does not fit that project's price, the agent may make ONE short suggestion
of the catalogue projects whose price does fit — and only when the caller
explicitly says yes to one of them does that project become the pitch for
the rest of the call.

Built for the Pramukh per-project agents (2026-10-07): seven agents, one per
project, each pitching its own project like Ravi pitches Swarit, all sharing
one catalogue. The owner's test: "give a budget that does not fit; it should
recommend what does, and switch only after I confirm".

Pure functions + one small per-call state object; the router owns the wiring
(conversation.py) and the bands live in prompt_blocks.py. Nothing here runs
for an agent without the flag — every other agent is byte-identical.

Decisions worth keeping:
  * The switch is DETERMINISTIC. The agent's spoken line is what decides
    which projects were offered (a suggestion the band computed but the model
    never said cannot be accepted), and the caller's next turns decide yes /
    no. A model-side "switch" tool would be one more write the free-tier key
    has to carry, and the owner asked for an explicit confirmation — a yes or
    the project's name, not an inference.
  * A question about another project is NOT a switch ("Vivan kitne ka hai?"
    is the owner testing shared knowledge). Only a project that was OFFERED
    on budget grounds can be switched to.
  * The static (cached) knowledge band is never rebuilt for a switch: the
    per-turn bands take a switched VIEW of the agent definition, and a
    ━━ PITCH SWITCHED ━━ band next to CURRENT GOAL says which name now means
    "the project you pitch". Rebuilding the agent band would break the
    agent-scoped context cache (§62) for the rest of the call.
"""
from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# How far outside a price band a budget may sit and still "fit". Callers
# round ("around 1.5 crore" for a ₹1.75 Cr home is a negotiation, not a
# mismatch), so a small stretch is allowed in both directions.
FIT_TOLERANCE = 0.15
# A suggestion the caller neither accepted nor declined expires after this
# many caller turns and is treated as declined — it is never repeated.
OFFER_TTL_TURNS = 2
MAX_ALTERNATIVES = 2

_INDIC = "ऀ-ॣ०-ॿ઀-૿"
_NUM = r"(?<![\d.,])(\d+(?:[.,]\d+)*)(?![\d]|[.,]\d)"
# A number followed (optionally) by its unit. Units: crore / cr / करोड़ /
# કરોડ ; lakh / lac / l / लाख / લાખ.
_AMOUNT_RE = re.compile(
    _NUM + r"\s*(crores?|cr\.?|करोड़|करोड|કરોડ|lakhs?|lacs?|lac|l\b|लाख|લાખ)?"
    # A number that counts something else is not money: "3 BHK", "2,320
    # sq ft", "8.2 km", "46 units", "10th floor".
    r"(?!\s*(?:\.5\s*)?(?:bhk|sq|square|ft|feet|km|kms|acre|floor|th\b|st\b|"
    r"nd\b|rd\b|units?|bungalows?|buildings?|towers?|wings?|minutes?|mins?|"
    r"years?|%|बीएचके|એકર))",
    re.IGNORECASE)
_DEVANAGARI_DIGITS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯",
                                   "01234567890123456789")


# "डेढ़ करोड़" / "dhai crore" — how a budget is actually said on the phone.
_FRACTION_WORDS = (
    (r"(?:डेढ़|डेढ|dedh|dedh|દોઢ)", "1.5"),
    (r"(?:ढाई|dhai|adhai|અઢી)", "2.5"),
)


def _unit_factor(unit: str) -> Optional[float]:
    u = (unit or "").lower().strip(". ")
    if not u:
        return None
    if u.startswith("cr") or u in ("करोड़", "करोड", "કરોડ"):
        return 100.0
    return 1.0  # lakh / lac / l / लाख / લાખ


def amounts_lakhs(text: str) -> list[float]:
    """Every money amount in `text`, in lakhs, in order.

    A bare number takes the unit of the NEXT number that has one ("50 – 55
    lakh", "2.35 to 3.70 Cr"), else the previous one. With no unit anywhere,
    a number under 10 is crore, 10 or more is lakhs, and 100,000 or more is
    a rupee figure. Numbers that count something else (BHK, sq ft, km…) are
    skipped.
    """
    s = str(text or "").translate(_DEVANAGARI_DIGITS)
    for word, num in _FRACTION_WORDS:
        s = re.sub(word, num, s, flags=re.IGNORECASE)
    raw: list[tuple[float, Optional[float]]] = []
    for m in _AMOUNT_RE.finditer(s):
        try:
            n = float(m.group(1).replace(",", ""))
        except ValueError:
            continue
        raw.append((n, _unit_factor(m.group(2) or "")))
    out: list[float] = []
    for i, (n, f) in enumerate(raw):
        if f is None:
            nxt = next((g for _, g in raw[i + 1:] if g is not None), None)
            prv = next((g for _, g in reversed(raw[:i]) if g is not None), None)
            f = nxt or prv
        if f is None:
            if n >= 100000:          # rupees
                n, f = n / 100000.0, 1.0
            else:
                f = 100.0 if n < 10 else 1.0
        out.append(n * f)
    return [x for x in out if x > 0]


def price_bounds(item: dict) -> Optional[tuple[float, float]]:
    """(low, high) in lakhs for a catalogue item, or None if unpriced.

    `budget_lakhs: [lo, hi]` on the item wins (data-only, never rendered);
    otherwise `price_range` is parsed.
    """
    explicit = (item or {}).get("budget_lakhs")
    if isinstance(explicit, (list, tuple)) and explicit:
        try:
            vals = [float(v) for v in explicit if v is not None]
        except (TypeError, ValueError):
            vals = []
        if vals:
            return (min(vals), max(vals))
    vals = amounts_lakhs(str((item or {}).get("price_range") or ""))
    if not vals:
        return None
    return (min(vals), max(vals))


def budget_bounds(value: Any) -> Optional[tuple[float, float]]:
    """The caller's budget as (low, high) lakhs; a single figure is (x, x)."""
    vals = amounts_lakhs(str(value or ""))
    if not vals:
        return None
    return (min(vals), max(vals))


def fits(budget: tuple[float, float], price: tuple[float, float],
         tol: float = FIT_TOLERANCE) -> bool:
    """Do the two ranges meet, allowing `tol` stretch on the price band?"""
    b_lo, b_hi = budget
    p_lo, p_hi = price
    return b_hi >= p_lo * (1 - tol) and b_lo <= p_hi * (1 + tol)


def direction(budget: tuple[float, float], price: tuple[float, float]) -> str:
    """'below' when the budget sits under the price band, else 'above'."""
    return "below" if budget[1] < price[0] else "above"


def _config_key(text: str) -> str:
    """'Ultra 4 BHK' / '4bhk' / '4 BHK' → '4 bhk': the bedroom count is what
    a caller's requirement names; 'ultra' is a variant of it."""
    t = " ".join(str(text or "").lower().replace("bhk", " bhk").split())
    return t.replace("ultra ", "")


def offers_config(item: dict, config: str) -> bool:
    """Does the item list the caller's configuration? True when either side
    is unknown — a missing list is never a reason to hide a project."""
    want = _config_key(config)
    cfgs = (item or {}).get("configurations")
    if not want or not isinstance(cfgs, list) or not cfgs:
        return True
    return any(_config_key(c) == want for c in cfgs)


def alternatives(kb, budget: tuple[float, float], exclude: set[str],
                 limit: int = MAX_ALTERNATIVES, config: str = "") -> list[dict]:
    """Catalogue items whose price fits `budget` — and, when the caller has
    said which configuration they want, that offer it (RUNBOOK §75: a
    3 BHK buyer is not sent to a 1.5/2 BHK project) — closest first."""
    mid = (budget[0] + budget[1]) / 2.0
    scored: list[tuple[float, dict]] = []
    for item in (getattr(kb, "catalog", None) or []):
        if not isinstance(item, dict):
            continue
        iid = str(item.get("id") or "").strip().lower()
        if not iid or iid in exclude:
            continue
        pb = price_bounds(item)
        if not pb or not fits(budget, pb):
            continue
        if not offers_config(item, config):
            continue
        pmid = (pb[0] + pb[1]) / 2.0
        scored.append((abs(pmid - mid) / max(mid, 1.0), item))
    scored.sort(key=lambda x: x[0])
    return [it for _, it in scored[:limit]]


def item_keys(item: dict) -> list[str]:
    """Lower-cased spoken names for an item: name, aliases, and the name
    without the 'Pramukh ' prefix. Longest first."""
    keys: set[str] = set()
    name = str(item.get("name") or "").strip()
    if name:
        keys.add(name.lower())
        if name.lower().startswith("pramukh "):
            keys.add(name.lower()[len("pramukh "):])
    for a in (item.get("aliases") or []):
        a = str(a).strip().lower()
        if len(a) >= 3:
            keys.add(a)
    return sorted(keys, key=len, reverse=True)


def mentions(text: str, item: dict) -> bool:
    """Does `text` name `item`? Script-aware word edges, never `\\b`."""
    low = str(text or "").lower()
    for k in item_keys(item):
        for m in re.finditer(re.escape(k), low):
            before = low[m.start() - 1] if m.start() > 0 else " "
            after = low[m.end()] if m.end() < len(low) else " "
            if (not (before.isalnum() or re.match(f"[{_INDIC}]", before))
                    and not (after.isalnum() or re.match(f"[{_INDIC}]", after))):
                return True
    return False


# ── Caller answer classification ────────────────────────────────────────────
# A reply to "would you like to hear about X?" in any of the ways a caller in
# Vapi actually says yes or no — English, Hinglish, Hindi and Gujarati, in
# Latin or native script. Owner (2026-10-07): the confirmation must accept
# EVERY positive response, not one phrase. Order of the checks is the design:
#   1. hedges ("let me think", "dekhenge", "baad mein") are not a yes;
#   2. positives that CONTAIN a negation word ("why not", "no problem",
#      "koi baat nahi", "I don't mind") are a yes, and are removed before…
#   3. …any remaining negation, which wins over a yes ("haan, but no");
#   4. then any positive word or phrase.
# A wrong switch is worse than none, so ambiguity resolves to "other".

def _alt(words) -> str:
    return "|".join(sorted((re.escape(w) for w in words), key=len, reverse=True))


def _edge_re(words) -> re.Pattern:
    """Whole-phrase match with script-aware edges (never \\b near Indic)."""
    return re.compile(
        rf"(?<![a-z0-9{_INDIC}])(?:{_alt(words)})(?![a-z0-9{_INDIC}])",
        re.IGNORECASE)


_HEDGE_WORDS = (
    # English
    "maybe", "may be", "perhaps", "not sure", "let me think", "i'll think",
    "i will think", "will think", "think about it", "let me see", "we'll see",
    "will see", "let's see", "later", "some other time", "not now",
    "not right now", "i don't know", "dont know", "don't know", "no idea",
    # Hinglish
    "sochta hoon", "sochti hoon", "sochenge", "soch ke", "soch kar",
    "soch ke batata", "soch ke bataunga", "dekhenge", "dekhte hain",
    "dekhta hoon", "baad mein", "baad me", "abhi nahi", "pata nahi",
    "malum nahi", "shayad", "ho sakta hai",
    # Hindi
    "सोचता हूँ", "सोचता हूं", "सोचती हूँ", "सोचेंगे", "सोच के", "सोचकर",
    "देखेंगे", "देखते हैं", "देखता हूँ", "बाद में", "अभी नहीं", "पता नहीं",
    "मालूम नहीं", "शायद", "हो सकता है",
    # Gujarati
    "વિચારીશ", "વિચારીને", "જોઈશું", "જોઈએ છીએ", "પછી", "હમણાં નહીં",
    "ખબર નથી", "કદાચ",
)
_POSITIVE_WITH_NEG = (
    # English
    "why not", "no problem", "no issues", "no issue", "not a problem",
    "i don't mind", "i dont mind", "don't mind", "dont mind", "not bad",
    "no doubt", "can't say no", "of course",
    # Hinglish
    "kyu nahi", "kyun nahi", "kyon nahi", "kyun nahin", "kyu nai", "kyon nahin",
    "koi baat nahi", "koi baat nahin", "koi problem nahi", "dikkat nahi",
    "kyu nahi bataiye", "bura nahi",
    # Hindi
    "क्यों नहीं", "क्यूँ नहीं", "क्यूं नहीं", "कोई बात नहीं", "कोई problem नहीं",
    "कोई दिक्कत नहीं", "दिक्कत नहीं", "बुरा नहीं",
    # Gujarati
    "કેમ નહીં", "કેમ નહિ", "વાંધો નહીં", "કોઈ વાંધો નહીં", "વાંધો નથી",
)
_NEG_WORDS = (
    # English
    "no", "nope", "nah", "not", "never", "don't", "dont", "do not",
    "not interested", "no thanks", "no thank you", "rather not", "skip it",
    "not for me", "pass",
    # Hinglish
    "nahi", "nahin", "nai", "na", "naa", "mat", "rehne do", "rehne dijiye",
    "rahne do", "nahi chahiye", "zarurat nahi", "jarurat nahi", "interest nahi",
    # Hindi
    "नहीं", "नही", "नहि", "ना", "मत", "रहने दो", "रहने दीजिए", "रहने दीजिये",
    "नहीं चाहिए", "ज़रूरत नहीं", "जरूरत नहीं",
    # Gujarati
    "ના", "નહીં", "નહિ", "નથી", "રહેવા દો", "નથી જોઈતું", "જરૂર નથી",
)
_YES_WORDS = (
    # English
    "yes", "yeah", "yea", "yep", "yup", "ya", "yah", "yess", "ok", "okay",
    "okk", "okey", "k", "kk", "sure", "surely", "fine", "alright", "all right",
    "right", "correct", "great", "good", "nice", "cool", "perfect", "awesome",
    "absolutely", "definitely", "certainly", "of course", "please",
    "go ahead", "go on", "carry on", "continue", "tell me", "tell me more",
    "tell me about it", "let's", "lets", "let us", "sounds good",
    "sounds great", "sounds nice", "that works", "works for me", "done",
    "interested", "i'm interested", "im interested", "i am interested",
    "i'd like that", "i would like", "i'd like to", "would love", "love to",
    "happy to", "why not", "go for it", "let's do it", "lets do it",
    "explain", "share", "send", "show me", "i want",
    # Hinglish
    "haan", "han", "haa", "ha", "haanji", "hanji", "haan ji", "han ji", "ji",
    "jee", "ji haan", "ji han", "theek", "thik", "theek hai", "thik hai",
    "bilkul", "chalega", "chalo", "chaliye", "zaroor", "jaroor", "jarur",
    "zarur", "batao", "bataiye", "bataye", "bata do", "bata dijiye",
    "bolo", "boliye", "suniye", "sunao", "sunaiye", "samjhao", "samjhaiye",
    "dikhao", "dikhaiye", "dekh lete", "dekhna hai", "dekhna chahunga",
    "sahi", "sahi hai", "accha", "achha", "acha", "achcha", "badhiya",
    "badiya", "mast", "pakka", "haan bolo", "aage bolo", "aage bataiye",
    "interest hai", "pasand", "chahiye", "jaanna hai", "janna hai",
    "kyu nahi", "kyun nahi", "kyon nahi",
    # Hindi
    "हाँ", "हां", "हा", "हाँजी", "हांजी", "जी", "जी हाँ", "जी हां", "ठीक",
    "ठीक है", "बिल्कुल", "बिलकुल", "चलेगा", "चलो", "चलिए", "ज़रूर", "जरूर",
    "बताओ", "बताइए", "बताइये", "बताएं", "बताएँ", "बता दो", "बता दीजिए",
    "बोलो", "बोलिए", "बोलिये", "सुनाइए", "सुनाओ", "समझाइए", "दिखाइए",
    "दिखाओ", "देख लेते", "देखना है", "सही", "सही है", "अच्छा", "अच्छी बात",
    "बढ़िया", "बढिया", "पक्का", "ओके", "ओके जी", "हाँ बोलिए", "आगे बताइए",
    "इंटरेस्टेड", "पसंद", "चाहिए", "जानना है", "क्यों नहीं",
    # Gujarati
    "હા", "હાં", "હાજી", "હા જી", "જી", "બરાબર", "ચોક્કસ", "ચાલશે", "ચાલો",
    "કહો", "કહોને", "બતાવો", "જણાવો", "સારું", "સરસ", "ઠીક", "ઠીક છે",
    "જરૂર", "હા કહો", "ઓકે", "પાક્કું", "કેમ નહીં",
)
_HEDGE_RE = _edge_re(_HEDGE_WORDS)
_POS_NEG_RE = _edge_re(_POSITIVE_WITH_NEG)
_NEG_RE = _edge_re(_NEG_WORDS)
_YES_RE = _edge_re(_YES_WORDS)
_QUESTION_RE = re.compile(
    r"\?|(?<![a-z])(?:kya|kitna|kitne|kitni|kaha|kahan|kab|kaise|kaun|kaunsa|"
    r"what|how|where|when|which|price|rate)(?![a-z])|"
    r"क्या|कितना|कितने|कितनी|कहाँ|कहां|कब|कैसे|कौन|શું|કેટલા|કેટલું|ક્યાં|ક્યારે|કેવી",
    re.IGNORECASE)


# "बताइए ना", "bolo na", "કહોને" — a trailing na/ne after another word is a
# softener ("do tell me"), not a no. Only a na that OPENS the reply negates.
_SOFTENER_RE = re.compile(
    rf"(?<=[a-z{_INDIC}])\s+(?:na|naa|ना|ने|ને)(?![a-z0-9{_INDIC}])",
    re.IGNORECASE)


def _norm(text: str) -> str:
    t = str(text or "").lower().replace("’", "'")
    t = re.sub(r"[.,!;:।॥\"]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return _SOFTENER_RE.sub("", t).strip()


def classify_answer(text: str) -> str:
    """'yes' | 'no' | 'other' for a reply to "would you like to hear about it?"."""
    t = _norm(text)
    if not t:
        return "other"
    pos_neg = bool(_POS_NEG_RE.search(t))
    rest = _POS_NEG_RE.sub(" ", t)
    if _HEDGE_RE.search(rest) and not pos_neg:
        return "other"
    if _NEG_RE.search(rest):
        return "no"
    if pos_neg or _YES_RE.search(rest):
        return "yes"
    return "other"


def is_question(text: str) -> bool:
    return bool(_QUESTION_RE.search(str(text or "")))

# A caller asking to STAY with the original ("no, X is fine", "X hi theek").
_STAY_RE = re.compile(
    r"(?:\bhi\b|\bhee\b|ही|જ\b|only|itself)", re.IGNORECASE)


# ── Per-call state ──────────────────────────────────────────────────────────
@dataclass
class PitchSwitchState:
    base_id: str                       # the agent's own primary project
    active_id: str                     # what the agent pitches NOW
    offered: list[str] = field(default_factory=list)   # ids said to the caller
    offered_turn: int = -1             # caller-turn index when offered
    candidates: list[str] = field(default_factory=list)  # band's current list
    declined: set[str] = field(default_factory=set)
    switched_from: Optional[str] = None
    switch_budget: str = ""
    ambiguous_yes: bool = False
    caller_turns: int = 0
    # The budget the caller said no to (or let an offer lapse on). While the
    # budget is unchanged nothing else is suggested — "no" means "stay".
    quiet_for_budget: Optional[str] = None
    budget_now: str = ""
    # Owner (2026-10-12): a below-budget caller is first ASKED whether they
    # would like to see our other properties in that budget; the names come
    # only after a yes. `generic_asked` = the budget that question was asked
    # for; `generic_open` = it was the agent's last line and awaits an
    # answer; `generic_yes` = they said yes, name the candidates now.
    generic_asked: Optional[str] = None
    generic_open: bool = False
    generic_turn: int = -1
    generic_yes: bool = False


def enabled(agent_def) -> bool:
    pp = getattr(getattr(agent_def, "knowledge_base", None),
                 "primary_project", None) or {}
    return bool(isinstance(pp, dict) and pp.get("budget_switch") and pp.get("id"))


def switched_view(agent_def, active_id: str):
    """A shallow copy of `agent_def` whose primary project is `active_id`.

    Only the per-turn bands read it; the cached agent band keeps the
    original. Shallow copies — nothing in the original is mutated."""
    kb = agent_def.knowledge_base
    pp = dict(getattr(kb, "primary_project", None) or {})
    if str(pp.get("id") or "").strip().lower() == active_id:
        return agent_def
    item = next((i for i in (kb.catalog or [])
                 if str(i.get("id") or "").strip().lower() == active_id), None)
    pp["id"] = active_id
    if item is not None:
        pp["reason"] = str(item.get("positioning_tag") or "").strip() or \
            "the project the caller chose for their budget"
    kb2 = copy.copy(kb)
    kb2.primary_project = pp
    d2 = copy.copy(agent_def)
    d2.knowledge_base = kb2
    return d2


def item_by_id(kb, iid: str) -> Optional[dict]:
    iid = str(iid or "").strip().lower()
    return next((i for i in (getattr(kb, "catalog", None) or [])
                 if isinstance(i, dict)
                 and str(i.get("id") or "").strip().lower() == iid), None)


def compute_candidates(kb, st: PitchSwitchState, budget_value: Any,
                       config_value: Any = "") -> tuple[
        list[dict], Optional[tuple[float, float]], Optional[tuple[float, float]]]:
    """Alternatives to suggest right now ([] when the active project fits,
    there is no budget, or nothing else fits). Also returns the parsed
    budget and the active project's price band for the band text."""
    st.budget_now = str(budget_value or "")
    if st.quiet_for_budget is not None and st.quiet_for_budget == st.budget_now:
        return [], None, None
    budget = budget_bounds(budget_value)
    active = item_by_id(kb, st.active_id)
    price = price_bounds(active) if active else None
    if not budget or not price or fits(budget, price):
        return [], budget, price
    if direction(budget, price) == "above":
        # They can afford it. Call 1077 (2026-10-12): ₹1.2 Cr against a
        # ₹90 lakh 3 BHK was read as "does not fit" and the agent offered
        # Silver Stone and Green County instead of its own project.
        return [], budget, price
    exclude = {st.active_id, st.base_id} | set(st.declined)
    return alternatives(kb, budget, exclude,
                        config=str(config_value or "")), budget, price


_GENERIC_OFFER_RE = re.compile(
    r"(?:और|अन्य|दूसर\S*|बाक़ी|बाकी|other|another|more)\s*(?:\S+\s+){0,3}?"
    r"(?:properties|property|projects|project|options|option|प्रॉपर्टी\S*|"
    r"प्रोपर्टी\S*|प्रोजेक्ट\S*|ऑप्शन\S*|विकल्प)", re.IGNORECASE)


def observe_agent(kb, st: PitchSwitchState, text: str) -> None:
    """The agent spoke: which of the current candidates did it name — or
    did it ask the generic "our other properties in that budget?" question
    without naming any?"""
    if not st.candidates or not text:
        return
    if (not any(mentions(text, item_by_id(kb, c) or {}) for c in st.candidates)
            and _GENERIC_OFFER_RE.search(text) and is_question(text)):
        st.generic_asked = st.budget_now
        st.generic_open = True
        st.generic_turn = st.caller_turns
        st.generic_yes = False
        logger.info("[PITCH] generic other-properties question asked")
        return
    named = [cid for cid in st.candidates
             if (it := item_by_id(kb, cid)) is not None and mentions(text, it)]
    if named:
        st.offered = named
        st.offered_turn = st.caller_turns
        st.ambiguous_yes = False
        logger.info("[PITCH] offered %s (active=%s)", named, st.active_id)


def observe_caller(kb, st: PitchSwitchState, text: str,
                   budget_value: Any = "") -> Optional[str]:
    """The caller spoke. Returns the id switched to, or None."""
    st.caller_turns += 1
    if not st.offered:
        if st.generic_open:
            verdict = classify_answer(text)
            if verdict == "yes":
                st.generic_open = False
                st.generic_yes = True
                logger.info("[PITCH] yes to other properties — name them")
            elif verdict == "no":
                st.generic_open = False
                st.declined.update(st.candidates)
                st.quiet_for_budget = str(budget_value or "")
                logger.info("[PITCH] no to other properties — staying put")
            elif not is_question(text) and \
                    st.caller_turns - st.generic_turn >= OFFER_TTL_TURNS:
                st.generic_open = False
                st.declined.update(st.candidates)
                st.quiet_for_budget = str(budget_value or "")
                logger.info("[PITCH] other-properties question lapsed")
        return None
    offered_items = [(cid, item_by_id(kb, cid)) for cid in st.offered]
    named = [cid for cid, it in offered_items if it and mentions(text, it)]
    verdict = classify_answer(text)
    # Naming the ORIGINAL project while declining = staying put.
    base_item = item_by_id(kb, st.active_id)
    stays = bool(base_item and mentions(text, base_item))
    target: Optional[str] = None
    if verdict == "no" or (stays and not named):
        st.declined.update(st.offered)
        st.quiet_for_budget = str(budget_value or "")
        logger.info("[PITCH] caller declined %s — staying with %s",
                    st.offered, st.active_id)
        st.offered = []
        return None
    asking = is_question(text)
    if len(named) == 1 and (verdict == "yes" or (
            verdict == "other" and not asking
            and len(_norm(text).split()) <= 4)):
        # "Green County chalega" / "haan Green County" / just "Green County"
        # (the answer to "which one?"). A QUESTION about the offered project
        # ("Green County kitne ka hai?") is interest, not a confirmation.
        target = named[0]
    elif verdict == "yes" and len(st.offered) == 1 and not stays:
        target = st.offered[0]
    elif verdict == "yes" and len(st.offered) > 1 and not named:
        st.ambiguous_yes = True
        return None
    if target:
        st.switched_from = st.active_id
        st.active_id = target
        st.switch_budget = str(budget_value or "")
        st.offered = []
        st.candidates = []
        st.ambiguous_yes = False
        logger.info("[PITCH] caller chose %s — pitch switched from %s",
                    target, st.switched_from)
        return target
    if asking:
        # The caller is asking about the offer (or anything else) — the
        # agent answers, and the offer stays open for their real answer.
        st.offered_turn = st.caller_turns
        return None
    if st.caller_turns - st.offered_turn >= OFFER_TTL_TURNS:
        st.declined.update(st.offered)
        st.quiet_for_budget = str(budget_value or "")
        logger.info("[PITCH] offer %s expired unanswered", st.offered)
        st.offered = []
    return None


def _money(lakhs: float) -> str:
    if lakhs >= 100:
        v = lakhs / 100.0
        s = f"{v:.2f}".rstrip("0").rstrip(".")
        return f"₹{s} Cr"
    return f"₹{lakhs:g} lakh"


def _band_str(b: tuple[float, float]) -> str:
    return _money(b[0]) if abs(b[0] - b[1]) < 1e-6 else \
        f"{_money(b[0])}–{_money(b[1])}"


def _item_brief(item: dict) -> str:
    bits = []
    cfg = item.get("configurations")
    if isinstance(cfg, list) and cfg:
        bits.append("/".join(str(c) for c in cfg))
    if item.get("price_range"):
        bits.append(str(item["price_range"]))
    if item.get("location"):
        bits.append(str(item["location"]))
    return f"{item.get('name')} ({', '.join(bits)})" if bits else str(item.get("name"))


def band(kb, st: PitchSwitchState, budget_value: Any,
         config_value: Any = "") -> list[str]:
    """The per-turn ━━ BUDGET FIT ━━ / ━━ PITCH SWITCHED ━━ lines, and the
    side effect of recording this turn's candidates on `st`. `config_value`
    (the caller's BHK, when known) narrows the alternatives (§75)."""
    lines: list[str] = []
    if st.switched_from:
        new = item_by_id(kb, st.active_id) or {}
        old = item_by_id(kb, st.switched_from) or {}
        nn, on = new.get("name", st.active_id), old.get("name", st.switched_from)
        lines += [
            "",
            "━━ PITCH SWITCHED (the caller chose this) ━━",
            f"▸ The caller explicitly chose {nn} over {on}"
            + (f" for their budget ({st.switch_budget})" if st.switch_budget else "")
            + f". From now on {nn} is the project you pitch and the one you "
            f"invite them to see. Wherever the KNOWLEDGE BASE, the rules or "
            f"CURRENT GOAL above say {on} is the project you pitch, read {nn}.",
            f"▸ Use {nn}'s OWN facts from the KNOWLEDGE BASE — never carry "
            f"{on}'s sizes, price, amenities or status over to it. Say in one "
            f"warm clause that you'll gladly tell them about {nn}, then carry on.",
            f"▸ Do not bring {on} back up unless the caller asks about it.",
        ]
    cands, budget, price = compute_candidates(kb, st, budget_value,
                                              config_value)
    st.candidates = [str(c.get("id")).strip().lower() for c in cands]
    active = item_by_id(kb, st.active_id) or {}
    an = active.get("name", st.active_id)
    if st.offered:
        names = [(item_by_id(kb, c) or {}).get("name", c) for c in st.offered]
        lines += [
            "",
            "━━ BUDGET FIT — waiting for their answer ━━",
            f"▸ You suggested {' or '.join(names)} because their range does "
            f"not fit {an}. {an} stays the project you pitch until they "
            "clearly say yes to one; do NOT start pitching the suggestion yet "
            "and do not repeat the suggestion.",
        ]
        if st.ambiguous_yes:
            lines.append(f"▸ They said yes but you named {len(names)} projects "
                         "— ask, in a few words, which one they'd like to hear "
                         "about.")
        return lines
    if not cands or not budget or not price:
        # A budget above the price band with nothing pricier to suggest is
        # not a mismatch — they can afford it. Call 1074: ₹1.2 Cr against a
        # ₹90 lakh 3 BHK read "does not fit", and the model said so.
        if budget and price and not fits(budget, price) and not cands \
                and not st.declined and direction(budget, price) == "below":
            lines += [
                "",
                "━━ BUDGET FIT ━━",
                f"▸ Their range ({_band_str(budget)}) does not fit {an} "
                f"({active.get('price_range') or _band_str(price)}), and no "
                "other project in the KNOWLEDGE BASE fits it either. Say so "
                f"honestly in one sentence, note their range for the team, "
                f"and carry on with {an} — never bend a price.",
            ]
        return lines
    where = direction(budget, price)
    names = [str(c.get("name")) for c in cands]
    lines += ["", "━━ BUDGET FIT — suggest, don't pitch ━━"]
    if st.generic_yes:
        lines += [
            "▸ They said YES to hearing about our other properties in their "
            f"range ({_band_str(budget)}). Now name " + (" and ".join(names))
            + " — one line each (name, size, rough price), nothing more — "
            + ("and ask whether they'd like to hear more about it."
               if len(names) == 1 else "and ask which one they'd like to hear about."),
            "▸ Facts for those lines: " + "; ".join(_item_brief(c) for c in cands) + ".",
        ]
    elif st.generic_open and st.generic_asked == st.budget_now:
        lines += [
            "▸ You asked whether they'd like to see our other properties in "
            "their range and they have not answered that yet. Answer what "
            "they just said; do not repeat the question and do not name a "
            "project.",
        ]
    else:
        lines += [
            f"▸ Their range ({_band_str(budget)}) is below {an} "
            f"({active.get('price_range') or _band_str(price)}). This turn: "
            "answer what they just said, then in ONE short sentence say "
            f"honestly that {an} is above that range and ask whether they "
            "would like to see our other properties in that budget — a "
            "question, no project names yet.",
        ]
    lines.append(
        f"▸ It is a suggestion, not a switch: until they clearly say yes to a "
        f"named project, {an} stays the project you pitch. If they'd rather "
        f"stay with {an}, accept it warmly and never suggest it again.")
    return lines
