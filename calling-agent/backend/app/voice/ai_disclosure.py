"""
"Are you a real person or an AI?" — answered honestly by every agent
(RUNBOOK §66).

Every agent on the platform is a voice AI. None of them volunteers it, and
none of them may deny it: the moment a caller asks explicitly — "are you a
robot?", "क्या आप AI हो?", "इंसान हो या मशीन?", "is this a recording?" — the
answer is yes, said plainly, and the conversation carries on.

Until this module the rule lived only in the prompt (`_ai_disclosure_line`),
which the model mostly obeys, and two deterministic paths could beat it: the
identity-question shortcut answered "who am I talking to — a bot?" with the
agent's name alone, and an owner's playbook answer could say anything.
`personality.ai_disclosure` (deflect / admit / admit_if_pressed) still shapes
the prompt's tone; it no longer decides whether an explicit question gets a
true answer.

Explicit means explicit. The patterns below need a question SHAPE about the
agent itself (you / this call + robot / AI / machine / human / recording);
the bare word "machine" in "आपकी मशीन है क्या?" (a caller asking about a
product) must not trigger it, which is why मशीन only counts after आप/तुम
and right before the copula. Pure text functions, no state.
"""

from __future__ import annotations

import re

from .address import _NA, _NB

# What the caller might be asking whether we are.
_EN_WHAT = (r"(?:a |an )?(?:(?:real |actual |live )?"
            r"(?:robot|bot|ai|a\.i\.?|artificial intelligence|machine|computer|"
            r"human(?: being)?|recording|recorded (?:call|message|voice)|"
            r"automated (?:call|voice|system|message)|virtual (?:assistant|agent))"
            r"|(?:real|actual|live) person)")
_EN_RE = re.compile(
    rf"\b(?:are|r)\s+(?:you|u)\s+{_EN_WHAT}\b"
    rf"|\b(?:is|was)\s+(?:this|it|that)\s+(?:a |an )?"
    r"(?:robot|bot|ai|a\.i\.?|machine|computer|recording|recorded|automated|"
    r"real (?:person|human)|human)\b"
    rf"|\bam\s+i\s+(?:talking|speaking)\s+(?:to|with)\s+{_EN_WHAT}\b"
    r"|\b(?:human|person|real)\s+or\s+(?:a |an )?(?:robot|bot|ai|machine|computer|recording)\b"
    r"|\b(?:robot|bot|ai|machine|computer)\s+or\s+(?:a |an )?(?:real )?(?:human|person)\b",
    re.IGNORECASE)

# Romanized Hindi / Gujarati.
_ROMAN_NOUN = (r"(?:robot|bot|ai|a\.i|machine|computer|insaan|insan|insaan|"
               r"human|aadmi|admi|recording|manas|maanas)")
_ROMAN_RE = re.compile(
    rf"\b(?:aap|ap|tum|tu|tame|tamey)\s+(?:\w+\s+)?{_ROMAN_NOUN}\s+"
    r"(?:ho|hain|hai|hu|cho|chho)\b"
    rf"|\b{_ROMAN_NOUN}\s+(?:ho|hai|hain|cho|chho)\s+(?:kya|ke|na|ne)\b"
    r"|\b(?:ye|yeh|ya|aa)\s+(?:robot|ai|bot|recording|computer)\s+(?:hai|che|chhe)\b"
    r"|\b(?:insaan|insan|human|aadmi)\s+(?:ho|hai|hain)\s+(?:ya|ke)\s+(?:robot|machine|ai|bot)\b",
    re.IGNORECASE)

# Devanagari. AI arrives from STT as "AI", "ai", "एआई", "ए आई" or "ए.आई.".
_HI_AI = r"(?:रोबोट|रोबॉट|बॉट|बोट|एआई|ए\s?आई|ए\.आई\.?|AI|ai|A\.I\.?|कंप्यूटर|कम्प्यूटर)"
_HI_ANY = rf"(?:{_HI_AI}|मशीन|इंसान|इन्सान|मनुष्य|आदमी|रिकॉर्डिंग|रिकार्डिंग|रेकॉर्डिंग)"
_HI_RE = re.compile(
    # आप/तुम [कोई/एक/असली] X हो/हैं
    rf"{_NB}(?:आप|तुम|तू){_NA}\s+(?:\S+\s+)?{_HI_ANY}\s*(?:हो|हैं|है){_NA}"
    # X हो क्या / X हैं क्या / X है क्या   (robot ho kya)
    rf"|{_NB}{_HI_AI}\s*(?:हो|हैं|है)\s*(?:क्या|ना|न){_NA}"
    rf"|{_NB}(?:क्या\s+)?(?:ये|यह|ये\s+कॉल){_NA}\s+(?:\S+\s+)?"
    rf"(?:{_HI_AI}|रिकॉर्डिंग|रिकार्डिंग|रेकॉर्डिंग)\s*(?:है|हैं){_NA}"
    # इंसान हो या मशीन / मशीन हो या इंसान
    rf"|{_NB}(?:इंसान|इन्सान|आदमी|असली|रियल){_NA}\s*(?:हो|हैं|है)?\s*या\s*"
    rf"(?:{_HI_AI}|मशीन){_NA}"
    rf"|{_NB}(?:{_HI_AI}|मशीन){_NA}\s*(?:हो|हैं|है)?\s*या\s*(?:इंसान|इन्सान|आदमी){_NA}"
    # रिकॉर्डिंग है क्या / मशीन से बात हो रही है क्या
    rf"|{_NB}(?:रिकॉर्डिंग|रिकार्डिंग|रेकॉर्डिंग)\s+(?:है|चल रही है)\s*क्या"
    rf"|{_NB}(?:{_HI_AI}|मशीन)\s+से\s+(?:बात\s+)?(?:हो\s+रही\s+है|कर\s+रहा|कर\s+रही)",
    re.IGNORECASE)

# Gujarati.
_GU_ANY = r"(?:રોબોટ|રોબૉટ|બોટ|એઆઈ|એ\s?આઈ|AI|ai|મશીન|કમ્પ્યુટર|માણસ|રેકોર્ડિંગ)"
_GU_RE = re.compile(
    rf"{_NB}(?:તમે|તું){_NA}\s+(?:\S+\s+)?{_GU_ANY}\s*(?:છો|છે){_NA}"
    rf"|{_NB}(?:આ){_NA}\s+(?:\S+\s+)?(?:રોબોટ|બોટ|એઆઈ|AI|ai|રેકોર્ડિંગ)\s*છે{_NA}"
    rf"|{_NB}(?:માણસ){_NA}\s*(?:છો|છે)?\s*કે\s*(?:રોબોટ|મશીન|AI|એઆઈ){_NA}",
    re.IGNORECASE)


def asks_if_ai(text: str) -> bool:
    """Is the caller explicitly asking whether they are talking to an AI /
    a machine / a recording / a real person?"""
    t = (text or "").strip()
    if not t:
        return False
    return bool(_EN_RE.search(t) or _ROMAN_RE.search(t)
                or _HI_RE.search(t) or _GU_RE.search(t))


# Does an owner-authored answer already say it is an AI? Then it is the
# owner's words that are spoken (their playbook entry), not ours.
_ADMITS_RE = re.compile(
    r"\b(?:ai|a\.i\.?|artificial intelligence|virtual (?:assistant|agent)|"
    r"voice assistant|bot|automated)\b"
    rf"|{_NB}(?:एआई|ए\s?आई|वर्चुअल|रोबोट|બોટ|એઆઈ|વર્ચ્યુઅલ){_NA}",
    re.IGNORECASE)
_DENIES_RE = re.compile(
    r"\bnot (?:an? )?(?:ai|bot|robot|machine|recording)\b"
    r"|\b(?:i'?m|i am) (?:a )?(?:real|human)\b"
    rf"|{_NB}(?:रोबोट|मशीन|AI|एआई)\s+नहीं{_NA}"
    rf"|{_NB}(?:असली\s+)?इंसान\s+(?:ही\s+)?हूँ",
    re.IGNORECASE)


def admits_ai(answer: str) -> bool:
    """An authored answer that says it is an AI and never denies it."""
    a = answer or ""
    return bool(_ADMITS_RE.search(a)) and not _DENIES_RE.search(a)


# The platform's own answer. First person without gendered verbs, so it is
# correct for every voice before `_finalize_spoken` even looks at it.
_DEFAULT = {
    "en": "Yes, I'm {agent}, an AI voice assistant{company_clause}.",
    "hi": "जी, मैं {agent} हूँ, {company_clause_hi}एक AI voice assistant।",
    "gu": "હા, હું {agent} છું, {company_clause_gu}એક AI voice assistant.",
}


def default_disclosure(lang: str, agent_name: str, company: str) -> str:
    """"Yes, I'm Ravi, an AI voice assistant calling for Pramukh Group." in
    the call's language (en fallback). Name / company may be empty."""
    agent = (agent_name or "").strip()
    comp = (company or "").strip()
    if comp and comp.lower() == agent.lower():
        comp = ""
    lang = lang if lang in _DEFAULT else "en"
    if not agent:
        return {
            "en": "Yes, I'm an AI voice assistant"
                  + (f" calling on behalf of {comp}." if comp else "."),
            "hi": "जी, मैं " + (f"{comp} की तरफ़ से " if comp else "")
                  + "एक AI voice assistant हूँ।",
            "gu": "હા, હું " + (f"{comp} તરફથી " if comp else "")
                  + "એક AI voice assistant છું.",
        }[lang]
    return _DEFAULT[lang].format(
        agent=agent,
        company_clause=(f" calling on behalf of {comp}" if comp else ""),
        company_clause_hi=(f"{comp} की तरफ़ से " if comp else ""),
        company_clause_gu=(f"{comp} તરફથી " if comp else ""),
    )
