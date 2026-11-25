"""
STT post-processing for domain-specific vocabulary correction.

Fixes common misrecognitions of city names, BHK types, real estate terms,
and Hindi/Gujarati transliterated words. Applied after STT transcription
and before intent detection.
"""

from __future__ import annotations

import re
import logging
from rapidfuzz import fuzz

from ...agent_definition.schema import STTCorrections

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# City name corrections (Surat, Vapi, Silvassa and common misrecognitions)
# ---------------------------------------------------------------------------

_CITY_CORRECTIONS: dict[str, str] = {
    "super": "surat",
    "soorat": "surat",
    "surate": "surat",
    "sorat": "surat",
    "surt": "surat",
    "suraat": "surat",
    "suraht": "surat",
    "surth": "surat",
    "wapi": "vapi",
    "bapi": "vapi",
    "vaapi": "vapi",
    "wappi": "vapi",
    "vapee": "vapi",
    "waapi": "vapi",
    # NOT "silver" — it is an ordinary English word (a colour, and the name
    # of half the insurance plans in India). Rewriting it to a Gujarati city
    # inside a live call is a much bigger error than missing one mishear.
    "silvasa": "silvassa",
    "silver sa": "silvassa",
    "silversa": "silvassa",
    "selvassa": "silvassa",
    "silvasa": "silvassa",
    "silwasa": "silvassa",
    "silvassa": "silvassa",
    "dadra": "silvassa",
    # --- Devanagari / Gujarati script mishearings of city names ---
    # Vapi
    "वापी": "vapi", "वापि": "vapi", "बाबी": "vapi", "वाबी": "vapi",
    "वआपी": "vapi", "वापीे": "vapi", "वापिए": "vapi",
    "વાપી": "vapi", "વાપિ": "vapi", "બાપી": "vapi", "વાબી": "vapi",
    # Surat
    "सूरत": "surat", "सुरत": "surat", "सूरत्": "surat",
    "સુરત": "surat", "સૂરત": "surat",
    # Silvassa
    "सिलवासा": "silvassa", "सिलवासआ": "silvassa", "सिल्वासा": "silvassa",
    "સિલવાસા": "silvassa", "સિલ્વાસા": "silvassa",
    # Note: deliberately NOT mapping ambiguous Devanagari words like "अभी"
    # (= "now") or "आप ही" (= "yourself/you") to vapi — they're real
    # words and would over-trigger outside location context.
}

_CANONICAL_CITIES = {"surat", "vapi", "silvassa"}

# ---------------------------------------------------------------------------
# BHK normalization
# ---------------------------------------------------------------------------

_WORD_TO_NUM: dict[str, str] = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
    "do": "2", "teen": "3", "char": "4", "paanch": "5", "panch": "5",
    "be": "2", "tran": "3", "chaar": "4",
    "ek": "1",
    # Hindi script numerals
    "एक": "1", "दो": "2", "तीन": "3", "चार": "4", "पांच": "5", "पाँच": "5",
    "फोर": "4", "थ्री": "3", "टू": "2", "फाइव": "5",
    # Gujarati script numerals
    "એક": "1", "બે": "2", "ત્રણ": "3", "ચાર": "4", "પાંચ": "5",
}

# Hindi/Gujarati script BHK variants as spoken by Sarvam STT
_DEVANAGARI_BHK_PATTERNS: dict[str, str] = {
    "बीएचके": "bhk",
    "बी एच के": "bhk",
    "जीएचके": "bhk",   # common Sarvam misrecognition of BHK
    "जी एच के": "bhk",
    "बिएचके": "bhk",
    "बीएचकी": "bhk",
    "બીએચકે": "bhk",   # Gujarati
    "બી એચ કે": "bhk",
    "बेडरूम": "bhk",
    "બેડરૂમ": "bhk",
}

_BHK_RE = re.compile(
    r"(\d)\s*b\s*h\s*k",
    re.IGNORECASE,
)

_WORD_BHK_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _WORD_TO_NUM) + r")\s+b\s*h\s*k\b",
    re.IGNORECASE,
)

_BEDROOM_RE = re.compile(
    r"(\d)\s*(?:bed\s*room|bedroom)s?",
    re.IGNORECASE,
)

_WORD_BEDROOM_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in _WORD_TO_NUM) + r")\s+(?:bed\s*room|bedroom)s?\b",
    re.IGNORECASE,
)


def _normalize_bhk(text: str) -> str:
    # First: convert Devanagari/Gujarati BHK variants to ASCII "bhk"
    for pattern, replacement in _DEVANAGARI_BHK_PATTERNS.items():
        if pattern in text:
            text = text.replace(pattern, replacement)

    # Convert Devanagari/Gujarati number words to digits when near "bhk"
    for word, num in _WORD_TO_NUM.items():
        if word in text and not word.isascii():
            bhk_nearby = re.search(
                re.escape(word) + r"\s*(?:bhk|b\s*h\s*k|बीएचके|जीएचके|બીએચકે|बेडरूम|બેડરૂમ)",
                text
            )
            if bhk_nearby:
                text = text.replace(word, num, 1)

    text = _BHK_RE.sub(r"\1 bhk", text)

    def _word_bhk_replace(m: re.Match) -> str:
        word = m.group(1).lower()
        num = _WORD_TO_NUM.get(word, word)
        return f"{num} bhk"

    text = _WORD_BHK_RE.sub(_word_bhk_replace, text)
    text = _BEDROOM_RE.sub(r"\1 bhk", text)

    def _word_bedroom_replace(m: re.Match) -> str:
        word = m.group(1).lower()
        num = _WORD_TO_NUM.get(word, word)
        return f"{num} bhk"

    text = _WORD_BEDROOM_RE.sub(_word_bedroom_replace, text)
    return text


# ---------------------------------------------------------------------------
# Hindi / Gujarati common word corrections
# ---------------------------------------------------------------------------

_HINDI_CORRECTIONS: dict[str, str] = {
    "namasthe": "namaste",
    "namashte": "namaste",
    "namasthey": "namaste",
    "ji haan": "ji haan",
    "jee": "ji",
    "bilkool": "bilkul",
    "makan": "makan",
    "makaan": "makan",
    "kiraya": "kiraya",
    "dekhna": "dekhna",
    "dikhao": "dikhao",
    "ghar": "ghar",
    "flat": "flat",
    "bhada": "bhada",
    "khareedna": "kharidna",
    "interested": "interested",
    "samay": "samay",
    "subah": "subah",
    "dopahar": "dopahar",
    "shaam": "shaam",
    "kal": "kal",
    "parso": "parso",
    "aaj": "aaj",
}

_GUJARATI_CORRECTIONS: dict[str, str] = {
    "kem cho": "kem cho",
    "kemcho": "kem cho",
    "majama": "maja ma",
    "maja maa": "maja ma",
    "bhai": "bhai",
    "joie": "joie",
    "joiye": "joiye",
    "gher": "ghar",
    "makan": "makan",
    "aavjo": "aavjo",
    "saru": "saru",
    "barobar": "barobar",
    "savare": "savare",
    "bapore": "bapore",
    "saanje": "saanje",
    "aaje": "aaje",
    "kaale": "kaale",
}

# ---------------------------------------------------------------------------
# Phonetic cross-language corrections
# STT often mistranscribes Hindi/Gujarati phrases as similar-sounding
# English words. These corrections map common misrecognitions back.
# ---------------------------------------------------------------------------

_PHONETIC_CORRECTIONS: dict[str, str] = {
    # Gujarati "theek che" (it's fine) misheard as English
    "shoe care": "theek che",
    "she care": "theek che",
    "tea care": "theek che",
    "tick che": "theek che",
    "thick che": "theek che",
    "take care": "theek che",
    # "kem cho" (how are you) — Sarvam frequently transcribes this as "gameshow"
    # because the phoneme pattern /k-ɛ-m/ /tʃ-oʊ/ sounds like "game" + "show"
    "game show": "kem cho",
    "gameshow": "kem cho",
    "game show game show": "kem cho",
    "came show": "kem cho",
    "come show": "kem cho",
    "cam show": "kem cho",
    "kim cho": "kem cho",
    "game cho": "kem cho",
    "gem show": "kem cho",
    # "kem cho" transcribed in Devanagari by Sarvam hi-IN model
    # Sarvam renders /k-ɛ-m/ as गेम (game) and /tʃ-oʊ/ as छो
    "गेम छो": "kem cho",
    "गेम शो": "kem cho",
    "गेम चो": "kem cho",
    "केम छो": "kem cho",
    "केम चो": "kem cho",
    # "kem cho" with various Sarvam noise prefixes in Devanagari
    "गे छो": "kem cho",
    # "hu chu" / "hu chhu" (I am, Gujarati) in Devanagari via hi-IN model
    "हु छू": "hu chu",
    "हू छू": "hu chu",
    "हु चू": "hu chu",
    "तुम छो": "kem cho",   # hi-IN misread of "kem cho"
    "और शायद": "kem cho", # hi-IN random output for short Gujarati greeting
    # "shu che" / "shu chhe" (what is it, Gujarati) in Devanagari
    "शु छे": "shu che",
    "शु चे": "shu che",
    "शू छे": "shu che",
    "शू चे": "shu che",
    # "maja ma" / "maja maa" (I'm fine, Gujarati) in Devanagari
    "माजा मा": "maja ma",
    "माजा मां": "maja ma",
    # "mane property joiye che" variants from hi-IN model
    "मने प्रॉपर्टी जोईए छे": "mane property joiye che",
    "मने प्रोपर्टी जोईए छे": "mane property joiye che",
    # "vapi ma 2 bhk joiye che" variants
    "वापी मा 2 बीएचके जोईए छे": "vapi ma 2 bhk joiye che",
    "सूरत मा 2 बीएचके जोईए छे": "surat ma 2 bhk joiye che",
    # "kaise ho" (how are you, Hindi) misheard
    "kaisa ho": "kaise ho",
    "kaisey ho": "kaise ho",
    "kese ho": "kaise ho",
    # "haan" / "ha" (yes) misheard
    "han": "haan",
    "hahn": "haan",
    "huh": "haan",
    # "nahi" / "nai" (no) misheard
    "nah he": "nahi",
    "na he": "nahi",
    "na hi": "nahi",
    "nye": "nahi",
    # "chahiye" (want/need) misheard
    "chai": "chahiye",
    "chai yeah": "chahiye",
    "cha he a": "chahiye",
    # "dekhna" (to see/look) misheard
    "they can": "dekhna",
    "deck na": "dekhna",
    # "abhi" (now) misheard
    "abbey": "abhi",
    "a be": "abhi",
    # "accha" / "acha" (good/okay) misheard
    "at cha": "accha",
    "attach a": "accha",
    "each a": "accha",
    # "bilkul" (absolutely) misheard
    "bill cool": "bilkul",
    "bill call": "bilkul",
    # "zaroor" (definitely) misheard
    "the rude": "zaroor",
    "jar or": "zaroor",
    # "samajh" (understand) misheard
    "some much": "samajh",
    "some age": "samajh",
    # "joiye" (want, Gujarati) misheard
    "joey": "joiye",
    "joy": "joiye",
    # "batao" (tell me) misheard
    "but how": "batao",
    "but ao": "batao",
    # Common Hinglish
    "money dubhe che joiye che": "mane dubhe che joiye che",
}

_REAL_ESTATE_CORRECTIONS: dict[str, str] = {
    "pramuk": "pramukh",
    "pramuk group": "pramukh group",
    "pramuk groups": "pramukh group",
    "praamukh": "pramukh",
    # NOT "premium" — same reason as "silver" above. An insurance agent asks
    # about the premium on every single call.
    "site visit": "site visit",
    "sight visit": "site visit",
    "booking": "booking",
    "book": "book",
    "property": "property",
    "apartment": "apartment",
    "flat": "flat",
}

# Project name corrections — common STT misrecognitions of Pramukh Group project names.
# Only high-confidence, unambiguous corrections included.
_PROJECT_NAME_CORRECTIONS: dict[str, str] = {
    # Aranya variants
    "aranya 2": "aranya ii",
    "aranya two": "aranya ii",
    "aranya to": "aranya ii",
    # Yogi Woods variants
    "yogi wood": "yogi woods",
    "yoga woods": "yogi woods",
    "yogi weds": "yogi woods",
    "yogiwoods": "yogi woods",
    "yogi ward": "yogi woods",
    # Satva variants (satwa is an alternate transliteration)
    "satwa": "satva",
    "saatva": "satva",
    # Sahaj variants
    "sahaj": "sahaj",  # pass-through — already correct, included for docstring clarity
    # Sangath variants (sangath is not a common Hindi/Gujarati word in this context)
    "sangat ": "sangath ",  # trailing space ensures we don't mangle "sangat" mid-word
    # Vivanta variants
    "vivant ": "vivanta ",
    # Central Park variants
    "central park": "central park",  # pass-through
    # Orbit variants
    "orbit series": "orbit series",  # pass-through
    # Agastya variants
    "agasthya": "agastya",
    "agastya": "agastya",  # pass-through
}

# ---------------------------------------------------------------------------
# Fuzzy location matching
# ---------------------------------------------------------------------------


def match_location(text: str) -> str | None:
    """Return canonical city name if any word closely matches a known location."""
    words = text.lower().split()
    for word in words:
        if word in _CANONICAL_CITIES:
            return word
        if word in _CITY_CORRECTIONS:
            return _CITY_CORRECTIONS[word]
        for city in _CANONICAL_CITIES:
            if fuzz.ratio(word, city) >= 75:
                return city
    return None


def match_bhk(text: str) -> str | None:
    """Extract BHK type from text. Returns e.g. '2 bhk' or None."""
    normalized = _normalize_bhk(text.lower())
    m = re.search(r"(\d)\s*bhk", normalized, re.IGNORECASE)
    if m:
        num = int(m.group(1))
        if 1 <= num <= 5:
            return f"{num} bhk"
    return None


# ---------------------------------------------------------------------------
# Main post-processing pipeline
# ---------------------------------------------------------------------------


def _apply_corrections(text: str, corrections: dict[str, str]) -> str:
    """Substring corrections, anchored at word boundaries for ASCII keys.

    Unanchored, these tables rewrite the INSIDE of unrelated words: an
    insurance agent saying "premium" got "pramukh", "silver" got "silvassa",
    and "thank you" became "thaank you". Indic scripts have no \\b semantics
    in Python's `re` (Devanagari matras are non-word characters), so those
    keys keep the plain substring behaviour they were written for.
    """
    lower = text.lower()
    for wrong, right in corrections.items():
        if wrong and wrong in lower:
            esc = re.escape(wrong)
            if wrong.isascii():
                # Only anchor the ends that are actually word characters —
                # a key like "3 bhk" or "-ji" must still match.
                pre = r"\b" if wrong[0].isalnum() else ""
                post = r"\b" if wrong[-1].isalnum() else ""
                esc = f"{pre}{esc}{post}"
            pattern = re.compile(esc, re.IGNORECASE)
            text = pattern.sub(lambda _m: right, text)  # literal replacement
            lower = text.lower()
    return text


def post_process_transcript(
    text: str,
    language: str = "en",
    corrections: STTCorrections | None = None,
) -> str:
    """
    Apply domain-specific corrections to STT output.

    Order: phonetic → BHK normalisation → city → real-estate → project names.
    If `corrections` is provided, its tables are applied as an additional pass
    after the built-in tables, letting agent-specific overrides take priority.
    """
    if not text or not text.strip():
        return text

    result = _apply_corrections(text, _PHONETIC_CORRECTIONS)
    result = _normalize_bhk(result)
    # The Pramukh-domain tables (cities/real-estate/project names) are gated
    # behind stt_corrections.use_builtin_tables (default true = today's
    # behaviour for every existing agent; the Copilot writes false for new
    # non-real-estate agents so 'bhk'-style corrections never fire on them).
    _use_builtin = corrections is None or getattr(corrections, "use_builtin_tables", True)
    if _use_builtin:
        result = _apply_corrections(result, _CITY_CORRECTIONS)
        result = _apply_corrections(result, _REAL_ESTATE_CORRECTIONS)
        result = _apply_corrections(result, _PROJECT_NAME_CORRECTIONS)

    # "unknown" = first turn, language not yet locked — apply all Indic corrections
    # so Gujarati/Hindi content is normalised regardless of detection order.
    if language in ("hi", "en", "unknown"):
        result = _apply_corrections(result, _HINDI_CORRECTIONS)
    if language in ("gu", "en", "unknown"):
        result = _apply_corrections(result, _GUJARATI_CORRECTIONS)

    # Agent-specific corrections overlay — applied last so they win on conflict.
    if corrections is not None:
        result = _apply_corrections(result, corrections.phonetic_corrections)
        result = _apply_corrections(result, corrections.city_corrections)
        result = _apply_corrections(result, corrections.real_estate_corrections)
        result = _apply_corrections(result, corrections.project_name_corrections)

    if result != text:
        logger.info("[STT-PP] %r -> %r", text, result)

    return result.strip()
