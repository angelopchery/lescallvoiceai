"""Who is being addressed, and who is speaking (RUNBOOK §42).

Every spoken line carries two genders that have nothing to do with each other:

  * the CALLER's, from ``contacts.gender`` — it decides Sir/Ma'am
    (``{honorific}``) and Mr./Mrs. (``{title}``). It is optional data: most
    contact lists carry no gender column, and a contact without one is never
    addressed by a gendered word AT ALL — not through a placeholder, not
    through a script that hardcoded "Sir", not by the model improvising one.
    Guessing from a name or a voice is how a woman gets called Sir on a live
    sales call (jai_mistry-2, 2026-09-10: "kya main SHAHANAZ Sir se baat kar
    raha hoon?" to a contact whose record said female).
  * the AGENT's, from the voice the business selected — it decides
    first-person agreement ("main kar raha hoon" / "kar rahi hoon"). A shubh
    voice saying "विशाल पटेल बात कर रही हूँ" (pramukh_agent, 2026-07-21) and a
    simran voice saying "samajh gaya" (jai_mistry-2, 2026-09-10) are the same
    defect from opposite directions: the voice changed, the words did not.

Pure functions only. The engine's output, the prompt builder, the dashboard's
spoken-surface preview and the Copilot's write path all call these, so none
of them can disagree about what a caller will actually hear.
"""

from __future__ import annotations

import re
from typing import Iterable

from ..agent_definition.voice_catalog import DEFAULT_TTS_SPEAKER, speaker_gender

# ── script-aware word boundaries ────────────────────────────────────────────
#
# `\b` cannot be trusted around Devanagari or Gujarati: Python's `\w` does not
# cover the combining vowel signs, so `\bसर\b` happily matches inside
# सर्विसेज ("services"), अक्सर ("often") and फ्रीलांसर — and `\bસર\b` inside
# ખૂબ સરસ ("very nice") and સરખામણી ("comparison"). All five were in live
# agent files on 2026-09-11. A letter here is any Latin, Devanagari or
# Gujarati code point, combining marks included — but NOT the danda (।, ॥),
# the abbreviation signs or the native digits, which sit inside the same
# Unicode blocks: counting "।" as a letter made every Hindi sentence ending
# "…रही हूँ।" invisible to the rules below.
_LETTERS = ("A-Za-zऀ-ॣॱ-ॿ"
            "઀-૥૲-૿")
_DEV_LETTERS = "ऀ-ॣॱ-ॿ"
_GU_LETTERS = "઀-૥૲-૿"
_NB = rf"(?<![{_LETTERS}])"          # not preceded by a letter
_NA = rf"(?![{_LETTERS}])"           # not followed by a letter
_DEV = re.compile(r"[ऀ-ॿ]")
_GUJ = re.compile(r"[઀-૿]")


def _script(text: str) -> str:
    """'hi' for Devanagari, 'gu' for Gujarati, 'en' for everything else."""
    if _DEV.search(text or ""):
        return "hi"
    if _GUJ.search(text or ""):
        return "gu"
    return "en"


script_of = _script


# ── the placeholders (single source for engine, prompt and preview) ─────────
#
# The engine resolves these in every authored spoken line; the prompt builder
# resolves them in the author text it QUOTES to the model (stage goals, the
# outbound.* lines), because SCRIPT FIDELITY tells the model to reproduce a
# quoted line word for word — braces included, if we leave them in.
CONTACT_PH_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:contact[ _\-]?name|customer[ _\-]?name|person[ _\-]?name|callee[ _\-]?name|contact|name)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$contact[ _\-]?name\b",
    re.IGNORECASE,
)
TIME_PH_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:current[ _\-]?time|time[ _\-]?of[ _\-]?day|greeting[ _\-]?time|day[ _\-]?part)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$current[ _\-]?time\b",
    re.IGNORECASE,
)
# Deliberately does NOT match a bare {gender} or {sir}: those read as "insert
# the raw column value", which would speak the word "male" to the caller. Nor
# bare {title}'s cousin {job_title}.
HONORIFIC_PH_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:honorific|sir[ _\-/]?(?:or[ _\-/]?)?ma['’ ]?am|"
    r"gender[ _\-]?(?:honorific|salutation)|salutation)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$honorific\b",
    re.IGNORECASE,
)
# Anything naming a "title" is the NAME PREFIX (Mr./Mrs.); anything naming an
# honorific/salutation/sir/ma'am is the standalone word. The two are never
# ambiguous because an author needs both in the same sentence.
TITLE_PH_RE = re.compile(
    r"(?:\$\{|\{\{|\{|\[|<)\s*"
    r"(?:title|mr\.?[ _\-/]*(?:or[ _\-/]+)?mr?s\.?|"
    r"name[ _\-]?(?:title|prefix)|gender[ _\-]?title|prefix)"
    r"\s*(?:\}\}|\}|\]|>)"
    r"|\$title\b",
    re.IGNORECASE,
)


# ── the words ───────────────────────────────────────────────────────────────
#
# Devanagari and Gujarati carry their own everyday business honorifics, and
# the script is chosen from the surrounding line: an English "Sir" dropped into
# a Hindi greeting makes the TTS code-switch mid-sentence.
HONORIFIC_WORDS = {
    "en": {"male": "Sir", "female": "Ma'am"},
    "hi": {"male": "सर", "female": "मैडम"},
    "gu": {"male": "સર", "female": "મેડમ"},
}

# ENGLISH FEMALE PREFIX — the one judgement call in these tables. "Mrs."
# asserts a marital status a phone number on a spreadsheet does not tell us;
# "Ms." carries the same respect and asserts nothing. The owner asked for Mrs.
# and it is the form Indian business calling expects, so that is what ships —
# as ONE named constant, so switching is a one-word change.
FEMALE_NAME_PREFIX_EN = "Mrs."

TITLE_WORDS = {
    "en": {"male": "Mr.", "female": FEMALE_NAME_PREFIX_EN},
    "hi": {"male": "श्री", "female": "श्रीमती"},
    "gu": {"male": "શ્રી", "female": "શ્રીમતી"},
}
# A romanized Shri/Smt stays romanized: an author who wrote "Shri Rajesh" in
# a Hinglish line did not ask for "Mr. Rajesh".
_SHRI_WORDS = {"male": "Shri", "female": "Shrimati"}

# SPOKEN Hindi/Gujarati never puts श्री/श्रीमती in front of the CALLER's
# name: on a phone call "श्री Vijay" reads like a notice board, and the owner
# asked for "Vijay जी" instead (call 1048 — "श्री Vijay" on 8 of 13 lines).
# `TITLE_WORDS` keeps the prefix because {title} still resolves through it and
# the WhatsApp renderer mirrors the table; `shri_to_suffix` then moves it
# behind the name on every spoken line (enforce_address runs on all of them).
# "जी" carries no gender, so it is used whether or not one is on file.
NAME_SUFFIX_WORDS = {"hi": "जी", "gu": "જી", "en": "ji"}
_SHRI_FAMILY_LAT = ("shri", "shree", "sri", "smt", "shrimati", "shreemati")
_SHRI_FAMILY_RE = re.compile(
    rf"{_NB}(?P<t>(?i:shrimati|shreemati|shri|shree|sri|smt)\.?"
    rf"|श्रीमती|श्रीमान|सुश्री|श्री|શ્રીમતી|શ્રીમાન|શ્રી)(?P<sp>[ \t]+)")
_SUFFIX_ALREADY_RE = re.compile(
    rf"^[ \t]*(?:जी|જી|(?i:ji|jee)){_NA}")


def name_suffix_for(title: str) -> str:
    """The suffix that replaces a श्री-family title ("" for Mr./Mrs.)."""
    t = (title or "").strip()
    if not t:
        return ""
    if t.lower().rstrip(".") in _SHRI_FAMILY_LAT:
        return NAME_SUFFIX_WORDS["en"]
    if _SHRI_FAMILY_RE.match(t + " "):
        return NAME_SUFFIX_WORDS[_script(t)]
    return ""

# A preview has no contact, so it has no gender — but a dashboard that
# silently DROPPED {honorific} from the resolved opening would tell an author
# their placeholder does not work. spoken_surfaces sets this as the gender and
# swaps the word back for the token, exactly as it does for the contact name.
HONORIFIC_PREVIEW_GENDER = "__preview__"
HONORIFIC_PREVIEW_WORD = "ZZHONORIFICZZ"
TITLE_PREVIEW_WORD = "ZZTITLEZZ"


def known_gender(gender: str | None) -> str:
    """'male' / 'female' for a gender we can act on, '' for everything else.

    `contacts.gender` is normalized at ingest (shared.contacts.normalize_gender)
    but stores unrecognised values verbatim ("other", "prefer not to say"), and
    none of those is a basis for calling someone Sir or Ma'am."""
    g = (gender or "").strip().lower()
    return g if g in ("male", "female") else ""


def gendered_word(table: dict, gender: str, surrounding: str = "",
                  lang: str = "") -> str:
    """Shared lookup behind {honorific} and {title}: one place decides what
    counts as a known gender and which script the word comes out in."""
    g = (gender or "").strip().lower()
    if g == HONORIFIC_PREVIEW_GENDER:
        return (HONORIFIC_PREVIEW_WORD if table is HONORIFIC_WORDS
                else TITLE_PREVIEW_WORD)
    g = known_gender(g)
    if not g:
        return ""
    # An explicit language wins: the prompt blocks know the call's language
    # outright, while a spoken line only has its own characters to go on.
    key = (lang or "").strip().lower()[:2]
    if key not in table:
        key = _script(surrounding)
    return table[key][g]


def honorific_word(gender: str, surrounding: str = "", lang: str = "") -> str:
    """"Sir" / "Ma'am" for a KNOWN gender, and "" for everything else — the
    empty string is the important half: an unknown gender is never guessed."""
    return gendered_word(HONORIFIC_WORDS, gender, surrounding, lang)


def title_word(gender: str, surrounding: str = "", lang: str = "") -> str:
    """"Mr." / "Mrs." for a KNOWN gender, "" for everything else."""
    return gendered_word(TITLE_WORDS, gender, surrounding, lang)


# ── closing the seam a removed word leaves ──────────────────────────────────

def tidy_seams(text: str) -> str:
    """Close the gap a removed placeholder or address word leaves.

    Without this, "Good morning {honorific}, am I speaking with X?" is spoken
    as "Good morning , am I speaking with X?" — the comma audibly detached —
    "the guardian of {student_name}?" as "the guardian of?", and "Sir, kya ek
    minute" with the Sir removed starts on a stray comma.
    """
    # Order matters: dropping a now-dangling preposition leaves its OWN
    # leading space behind, so the whitespace rules run after it.
    text = re.sub(r"\b(of|for|to|with|from)\s*([?.!])", r"\2", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([?.!,;:।])", r"\1", text)
    # A removed word between two commas, or a comma left in front of the end
    # of a sentence: "Thank you, , bye" / "Achha ,." never reach TTS.
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",\s*([?.!।])", r"\1", text)
    # A SENTENCE that now opens on punctuation: "Craft Idea. {honorific}, kya
    # ek minute" must not become "Craft Idea., kya".
    text = re.sub(r"([.!?।])[ \t]*[,;:]+[ \t]*", r"\1 ", text)
    # A line that now OPENS on punctuation ("Sir, kya ek minute" → ", kya").
    text = re.sub(r"^[\s,;:—–\-]+", "", text)
    # …and the same just inside an OPENING quote in a stage goal ("'Sir, …'"
    # → "'…"). Opening only — a closing quote's comma ("'yes', then") stays.
    text = re.sub(r"(^|[\s(:])(['\"‘“])[ \t]*[,;:][ \t]*", r"\1\2", text)
    return text.rstrip(" \t")


def _recapitalize(original: str, new: str) -> str:
    """If removing a leading word left a lowercase Latin letter where the
    line used to start with a capital, restore the capital."""
    o = original.lstrip()
    n = new.lstrip()
    if o[:1].isupper() and n[:1].islower() and n[:1].isascii():
        return new[: len(new) - len(n)] + n[:1].upper() + n[1:]
    return new


# ── the CALLER: Sir / Ma'am and Mr. / Mrs. ──────────────────────────────────

_HON_LAT = r"sir|ma['’]?am|madame|madam|maam|mam"
_HON_DEV = r"सर|मैडम|मैम|मेम|मॅडम|मॅम"
_HON_GU = r"સર|મેડમ|મૅડમ|મેમ"
_HONORIFIC_RE = re.compile(
    rf"{_NB}(?P<w>(?i:{_HON_LAT})|{_HON_DEV}|{_HON_GU}){_NA}")
# "sir/ma'am", "Sir / Madam", "sir or ma'am" — ONE address, and the engine's
# own old fallback for a contact with no name (spoken on 29+ live lines:
# "Am I speaking to sir/ma'am?"). Handled before single words so it resolves
# to one word, not two.
_COMPOUND_RE = re.compile(
    rf"{_NB}(?i:(?:sir|ma['’]?am|madam|mam)\s*(?:/|\\|&|\bor\b|\band\b)\s*"
    rf"(?:sir|ma['’]?am|madam|mam)){_NA}")

_TITLE_LAT_MR = ("mr", "mrs", "ms", "miss", "mister")
_TITLE_LAT_SHRI = ("shri", "shree", "sri", "smt", "shrimati", "shreemati")
_TITLE_RE = re.compile(
    rf"{_NB}(?P<t>(?i:mrs|mr|ms|miss|mister|shrimati|shreemati|shri|shree|sri|smt)\.?"
    rf"|श्रीमती|श्रीमान|सुश्री|श्री|मिसेज़|मिसेज|मिस्टर|શ્રીમતી|શ્રીમાન|શ્રી)"
    rf"(?P<sp>[ \t]+)")

_NAME_PREFIX_RE = re.compile(
    r"(?<![A-Za-z])(?:Mr|Mrs|Ms|Dr|Shri|Smt|Mister|Miss)\.?\s+([A-Z][a-z]+)")


def protected_names(texts: Iterable[str]) -> frozenset[str]:
    """Third parties the agent's own script names with a title.

    dhronai_ea calls on behalf of "Mr. Umang, the CEO" — Umang is not the
    caller, so "Umang Sir" / "Mr. Umang" must never be re-gendered to the
    caller's gender or stripped for an unknown caller. Measured across every
    agent line on record (2026-09-11), a third-party "<Name> Sir" essentially
    never occurs; this set is the guard for the agents that do name one."""
    out: set[str] = set()
    for t in texts:
        for m in _NAME_PREFIX_RE.finditer(t or ""):
            out.add(m.group(1).lower())
    return frozenset(out)


def _name_tokens(contact_name: str) -> set[str]:
    return {w.lower() for w in re.findall(rf"[{_LETTERS}]+", contact_name or "")
            if len(w) > 1}


def shri_to_suffix(text: str, contact_name: str) -> str:
    """"श्री Vijay, …" → "Vijay जी, …" — only in front of the CONTACT's name.

    A third party keeps the title the script gave them ("Shri Natubhai R.
    Bhatu", the founder), and a contact whose own name is Shree is not
    titled at all. Idempotent: a name already followed by जी/ji gets none."""
    names = _name_tokens(contact_name)
    if not text or not names:
        return text
    out: list[str] = []
    pos = 0
    for m in _SHRI_FAMILY_RE.finditer(text):
        t = m.group("t")
        if m.start() < pos or t.lower().rstrip(".") in names:
            continue
        # The contact's own name words right after the title, as one unit
        # ("श्री Vijay Patel" → "Vijay Patel जी").
        end = cur = m.end()
        while True:
            w = re.compile(rf"([{_LETTERS}]+)([ \t]*)").match(text, cur)
            if not w or w.group(1).lower() not in names:
                break
            end = w.start() + len(w.group(1))
            cur = w.end()
            if not w.group(2):
                break
        if end == m.end():
            continue
        name_part = text[m.end():end]
        suffix = "" if _SUFFIX_ALREADY_RE.match(text[end:]) \
            else " " + name_suffix_for(t)
        out.append(text[pos:m.start()] + name_part + suffix)
        pos = end
    out.append(text[pos:])
    return "".join(out)


def _prev_word(text: str, idx: int) -> str:
    m = re.search(rf"([{_LETTERS}]+)[ \t]*$", text[:idx])
    return m.group(1).lower() if m else ""


def enforce_address(text: str, gender: str, contact_name: str = "",
                    protected: Iterable[str] = ()) -> str:
    """Make every gendered address in a finished line match the contact.

    Known gender: a wrong word is corrected in its own script (Sir → Ma'am,
    सर → मैडम, Mr. → Mrs. in front of the contact's name). Unknown gender: the
    word is REMOVED and the seam closed — "Thank you Sir." → "Thank you.",
    "Achha Sir, samajh gaya" → "Achha, samajh gaya". This runs on EVERY line
    the caller hears — authored, canned or model-written — so an author who
    typed a literal "Sir" and a model that improvised one are held to the same
    rule as the {honorific} placeholder.

    Titles are only touched directly in front of the contact's own name;
    anything else ("Mr. Umang, our CEO") is a third party. Names in
    `protected` are third parties too, and their "Sir" is left alone.
    """
    if not text:
        return text
    g_raw = (gender or "").strip().lower()
    if g_raw == HONORIFIC_PREVIEW_GENDER:
        return text                      # a preview stands for every contact
    g = known_gender(g_raw)
    protect = {p.lower() for p in protected} - _name_tokens(contact_name)
    names = _name_tokens(contact_name)
    original = text
    removed = False

    def _hon(script_of: str) -> str:
        # A removed word leaves a marker, so a clause whose only object it
        # was can be recognised and dropped below.
        return HONORIFIC_WORDS[_script(script_of)][g] if g else _GONE

    def _compound(m: re.Match) -> str:
        nonlocal removed
        if not g:
            removed = True
        return _hon("x")                 # the compound is always Latin

    text = _COMPOUND_RE.sub(_compound, text)
    # श्री/Shri in front of the caller's name moves BEHIND it as जी/ji —
    # first, so the Mr./Mrs. pass below never sees a श्री to regender.
    text = shri_to_suffix(text, contact_name)

    if names:
        def _title(m: re.Match) -> str:
            nonlocal removed
            t = m.group("t")
            rest = text_ref[m.end():]
            nxt = re.match(rf"([{_LETTERS}]+)", rest)
            # Only a title sitting in front of the CONTACT'S name is theirs;
            # and a "title" that is itself part of their name (a contact
            # called Shree) is not a title at all.
            if (not nxt or nxt.group(1).lower() not in names
                    or t.lower().rstrip(".") in names):
                return m.group(0)
            if not g:
                removed = True
                return ""
            base = t.lower().rstrip(".")
            if base in _TITLE_LAT_SHRI:
                word = _SHRI_WORDS[g]
            elif base in _TITLE_LAT_MR:
                word = TITLE_WORDS["en"][g]
            else:
                word = TITLE_WORDS[_script(t)][g]
            return word + m.group("sp")

        text_ref = text
        text = _TITLE_RE.sub(_title, text)

    def _single(m: re.Match) -> str:
        nonlocal removed
        if _prev_word(text_ref2, m.start()) in protect:
            return m.group(0)
        if not g:
            removed = True
        return _hon(m.group("w"))

    text_ref2 = text
    text = _HONORIFIC_RE.sub(_single, text)
    if removed:
        text = _drop_objectless_identity_clauses(text).replace(_GONE, "")
        text = _recapitalize(original, tidy_seams(text).strip())
        # "Sir." on its own leaves only punctuation — nothing to say.
        if not re.search(rf"[{_LETTERS}0-9]", text):
            return ""
    return text


_GONE = "\x00"
# "am I speaking to Sir?" with the Sir removed is not a question any more —
# the word WAS its object. Same for "kya main Sir se baat kar raha hoon?".
_OBJECTLESS_ID = re.compile(
    r"(?i)\b(?:speaking|talking)\s+(?:to|with)\s*\x00\s*$"
    r"|\bmain\s*\x00\s*se\s+baat"
    r"|मैं\s*\x00\s*से\s+बात"
    r"|હું\s*\x00\s*સાથે\s+વાત")


def _drop_objectless_identity_clauses(text: str) -> str:
    if _GONE not in text:
        return text
    out: list[str] = []
    for sent in _SENTENCE_RE.findall(text):
        body = sent.rstrip()
        m = re.search(r"[.!?।]+$", body)
        end = m.group(0) if m else ""
        body = body[:m.start()] if m else body
        clauses = body.split(",")
        kept = [c for c in clauses if not _OBJECTLESS_ID.search(c)]
        if len(kept) == len(clauses):
            out.append(sent)
            continue
        if not any(c.replace(_GONE, "").strip() for c in kept):
            continue
        out.append(",".join(kept) + ("." if "?" in end else end))
    return " ".join(s.strip() for s in out if s.strip())


def tokenize_address(text: str, protected: Iterable[str] = ()) -> str:
    """Rewrite an AUTHORED line's literal address as placeholders.

    "kya main {contact_name} Sir se baat kar raha hoon?" →
    "kya main {contact_name} {honorific} se baat kar raha hoon?", and
    "Mr. {contact_name}" → "{title} {contact_name}". Used by the one-time
    fleet migration and on every Copilot write, so the script the business
    reads says what the call will do: address by the contact's gender, or not
    at all. Third parties named with a title are left exactly as written.
    """
    if not text:
        return text
    protect = {p.lower() for p in protected}
    text = _COMPOUND_RE.sub("{honorific}", text)
    text = re.sub(
        rf"{_NB}(?:(?i:mrs|mr|ms|miss|mister|shrimati|shreemati|shri|shree|sri|smt)\.?"
        rf"|श्रीमती|श्रीमान|श्री|मिसेज़|मिसेज|मिस्टर|શ્રીમતી|શ્રીમાન|શ્રી)"
        r"([ \t]+)(\{contact_name\})",
        r"{title}\1\2", text)

    def _single(m: re.Match) -> str:
        if _prev_word(ref, m.start()) in protect:
            return m.group(0)
        return "{honorific}"

    ref = text
    return _HONORIFIC_RE.sub(_single, text)


# Fields the engine SPEAKS (or quotes per call to the model) — the only
# places a literal address is rewritten as a placeholder. Caller-side
# patterns, rules, knowledge prose and identity text are never touched:
# "Never append 'Thank you Sir'" is a rule ABOUT the words.
_SPOKEN_RB = ("identity_responses", "closing_responses", "graceful_close",
              "objection_responses", "timeout_fallbacks", "tool_ack_fallbacks",
              "localized_responses")
_SPOKEN_OB = ("opening_greeting", "identity_confirm_line",
              "good_time_check_line", "pitch", "wrong_person_response",
              "busy_response", "wrong_number_close")


def _strings_in(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in _strings_in(v)]
    if isinstance(node, list):
        return [s for v in node for s in _strings_in(v)]
    return []


def tokenize_spoken_fields(d: dict, protected: Iterable[str] | None = None
                           ) -> list[str]:
    """Rewrite literal address as placeholders in every SPOKEN field of an
    agent definition dict (whole or partial), in place. Returns the paths it
    changed. Shared by the Copilot write path and the fleet migration so the
    two can never disagree about which fields are speech."""
    if protected is None:
        protected = protected_names(_strings_in(d))
    changed: list[str] = []

    def _fix(container, key, path: str) -> None:
        v = container.get(key) if isinstance(container, dict) else None
        if isinstance(v, str):
            nv = tokenize_address(v, protected)
            if nv != v:
                container[key] = nv
                changed.append(path)
        elif isinstance(v, dict):
            for k in list(v):
                _fix(v, k, f"{path}.{k}")
        elif isinstance(v, list):
            for i, item in enumerate(v):
                if isinstance(item, str):
                    nv = tokenize_address(item, protected)
                    if nv != item:
                        v[i] = nv
                        changed.append(f"{path}[{i}]")

    rb = d.get("response_bank")
    if isinstance(rb, dict):
        _fix(rb, "opening_greeting", "response_bank.opening_greeting")
        for k in _SPOKEN_RB:
            _fix(rb, k, f"response_bank.{k}")
        for i, cp in enumerate(rb.get("canned_phrases") or []):
            if isinstance(cp, dict):
                _fix(cp, "responses", f"response_bank.canned_phrases[{i}].responses")
        for i, op in enumerate(rb.get("objection_playbook") or []):
            if isinstance(op, dict):
                _fix(op, "answer", f"response_bank.objection_playbook[{i}].answer")
    ob = d.get("outbound")
    if isinstance(ob, dict):
        for k in _SPOKEN_OB:
            _fix(ob, k, f"outbound.{k}")
    cf = d.get("call_flow")
    if isinstance(cf, dict):
        for i, st in enumerate(cf.get("stages") or []):
            if isinstance(st, dict):
                _fix(st, "goal", f"call_flow.stages[{i}].goal")
        if isinstance(cf.get("connect"), dict):
            _fix(cf["connect"], "intro_line", "call_flow.connect.intro_line")
    return changed


# An identity question with its object removed: "am I speaking with ?",
# "kya main  se baat kar raha hoon?". With no name to ask about there is no
# question left to ask.
_IDENTITY_Q = re.compile(
    r"(?i)\b(?:speaking|talking)\s+(?:to|with)\b|\bam\s+i\s+speaking\b|"
    r"\bis\s+this\b|\bbaat\s+kar\s+rah|\bse\s+baat\b|\bbol\s+rahe\b|"
    r"बात\s+कर\s+रह|से\s+बात|बोल\s+रहे|વાત\s+કર|સાથે\s+વાત|બોલો\s+છો")
_SENTENCE_RE = re.compile(r"[^.!?।]*[.!?।]+|[^.!?।]+$")


def resolve_contact_name(text: str, name: str) -> str:
    """Fill {contact_name}, or — with no name on file — take it out properly.

    The old fallback spoke the literal word "sir/ma'am" in the name's place:
    "Am I speaking to sir/ma'am?" reached real callers on 29+ lines
    (kaapd_pooja, vishal_patel, hardik, shubhangi) and is exactly the
    gendered address this module exists to withhold from a contact with no
    gender. Now a clause that only asked WHO they are is dropped ("Hello, am I
    speaking with {contact_name}?" → "Hello."), and anywhere else the token
    simply goes ("Thank you {contact_name}!" → "Thank you!")."""
    if not text or not CONTACT_PH_RE.search(text):
        return text
    if name:
        return CONTACT_PH_RE.sub(lambda _m: name, text)
    out: list[str] = []
    for sent in _SENTENCE_RE.findall(text):
        if not CONTACT_PH_RE.search(sent):
            out.append(sent)
            continue
        body = sent.rstrip()
        end = ""
        m = re.search(r"[.!?।]+$", body)
        if m:
            body, end = body[:m.start()], m.group(0)
        clauses = body.split(",")
        kept = [c for c in clauses
                if not (CONTACT_PH_RE.search(c)
                        and _IDENTITY_Q.search(CONTACT_PH_RE.sub("", c)))]
        if len(kept) != len(clauses):
            if not any(c.strip() for c in kept):
                continue
            if "?" in end:
                end = "."
        out.append(CONTACT_PH_RE.sub("", ",".join(kept)) + end)
    return tidy_seams(" ".join(s.strip() for s in out if s.strip())).strip()


def neutral_address(text: str) -> str:
    """Author text for the STATIC (cached) prompt band.

    That band is identical for every call of an agent, so it cannot know who
    is on the line: gendered tokens are removed there and the per-call
    ━━ CALL TYPE ━━ band tells the model how — or whether — to address this
    caller. Leaving the braces in would have the model reproduce them."""
    if not text or "{" not in text and "[" not in text and "<" not in text \
            and "$" not in text:
        return text
    new = TITLE_PH_RE.sub("", HONORIFIC_PH_RE.sub("", text))
    return tidy_seams(new) if new != text else text


# ── the AGENT: first-person agreement follows the selected voice ────────────

def voice_gender(agent_def, lang: str = "") -> str:
    """Gender of the voice the caller HEARS for `lang` — the same chain the
    runtime uses to pick it: that language's voice, else the default
    language's voice, else the platform default speaker."""
    langs = getattr(agent_def, "languages", None)
    tv = dict(getattr(langs, "tts_voices", None) or {})
    default = getattr(langs, "default", "") or ""
    for code in ((lang or "")[:2], default):
        v = tv.get(code) if code else None
        if v is None:
            continue
        g = speaker_gender(getattr(v, "speaker", "") or "") \
            or known_gender(getattr(v, "gender", ""))
        if g:
            return g
        return ""                        # a voice we cannot classify
    return speaker_gender(DEFAULT_TTS_SPEAKER)


def agent_gender(agent_def, lang: str = "") -> str:
    """'male' / 'female' for the agent's own speech in `lang`.

    The voice decides. Stored pronouns are only consulted for a voice the
    catalog cannot classify (an unknown new Sarvam speaker)."""
    g = voice_gender(agent_def, lang)
    if g:
        return g
    pers = getattr(agent_def, "personality", None)
    ident = getattr(agent_def, "identity", None)
    p = ((getattr(pers, "pronouns", "") or getattr(ident, "pronouns", "")
          or "")).strip().lower()
    return {"he": "male", "she": "female"}.get(p, "")


def pronouns_for(gender: str) -> str:
    return {"male": "he", "female": "she"}.get(gender, "")


# Hindi first-person agreement. Every rule is anchored on a form ONLY THE
# SPEAKER can be the subject of — "… hoon" (I am), the -ūṅgā future, or a
# clause that opens with "main" — because the same participles agree with
# anyone else too: "number lag gaya hoga", "koi wajah rahi", "kya hua" and a
# caller named Kavita ("main Kavita hoon") must all come out untouched.
# Whole words from an explicit list, never a suffix rule: female names end in
# -a and -ta constantly.
_HI_LAT = [
    ("raha", "rahi"), ("rha", "rhi"), ("reha", "rehi"),
    ("gaya", "gayi"), ("gya", "gyi"), ("aaya", "aayi"), ("aya", "ayi"),
    ("chuka", "chuki"), ("samjha", "samjhi"), ("samajha", "samajhi"),
    ("baitha", "baithi"), ("pahuncha", "pahunchi"),
    ("karta", "karti"), ("sakta", "sakti"), ("chahta", "chahti"),
    ("chahata", "chahati"), ("leta", "leti"), ("deta", "deti"),
    ("rehta", "rehti"), ("rahta", "rahti"), ("jaanta", "jaanti"),
    ("janta", "janti"), ("sochta", "sochti"), ("dekhta", "dekhti"),
    ("bolta", "bolti"), ("puchta", "puchti"), ("poochta", "poochti"),
    ("puchhta", "puchhti"), ("batata", "batati"), ("bataata", "bataati"),
    ("milta", "milti"), ("chalta", "chalti"), ("samajhta", "samajhti"),
    ("samjhta", "samjhti"), ("sunta", "sunti"), ("likhta", "likhti"),
    ("maanta", "maanti"), ("manta", "manti"), ("bhejta", "bhejti"),
    ("rakhta", "rakhti"), ("aata", "aati"), ("ata", "ati"),
    ("jaata", "jaati"), ("jata", "jati"), ("dikhata", "dikhati"),
    ("samjhata", "samjhati"), ("karwata", "karwati"), ("karvata", "karvati"),
    ("padhta", "padhti"), ("sunata", "sunati"), ("bechta", "bechti"),
    ("dhoondhta", "dhoondhti"), ("dhundhta", "dhundhti"),
]
_HI_DEV = [
    ("रहा", "रही"), ("गया", "गई"), ("आया", "आई"), ("चुका", "चुकी"),
    ("समझा", "समझी"), ("बैठा", "बैठी"), ("पहुँचा", "पहुँची"),
    ("पहुंचा", "पहुंची"), ("करता", "करती"), ("सकता", "सकती"),
    ("चाहता", "चाहती"), ("लेता", "लेती"), ("देता", "देती"), ("रहता", "रहती"),
    ("जानता", "जानती"), ("सोचता", "सोचती"), ("देखता", "देखती"),
    ("बोलता", "बोलती"), ("पूछता", "पूछती"), ("बताता", "बताती"),
    ("मिलता", "मिलती"), ("चलता", "चलती"), ("समझता", "समझती"),
    ("सुनता", "सुनती"), ("लिखता", "लिखती"), ("मानता", "मानती"),
    ("भेजता", "भेजती"), ("रखता", "रखती"), ("आता", "आती"), ("जाता", "जाती"),
    ("दिखाता", "दिखाती"), ("समझाता", "समझाती"), ("करवाता", "करवाती"),
    ("पढ़ता", "पढ़ती"), ("सुनाता", "सुनाती"), ("बेचता", "बेचती"),
]
# Spelling variants that only ever appear on the FEMININE side.
_HI_DEV_FEM_ALIASES = {"गयी": "गया", "आयी": "आया"}
# Future stems: {stem}unga / {stem}ungi. "de"/"le" are irregular (dunga,
# lunga). "lungi" is ALSO a garment, so it never becomes "lunga" (a textile
# agent sells lungis) — only the masculine → feminine direction uses it.
_HI_FUT_STEMS = (
    "kar", "bata", "aa", "ja", "mil", "bhej", "rakh", "dekh", "puch", "pooch",
    "sun", "bol", "samjha", "karwa", "karva", "dikha", "chal", "soch", "kah",
    "likh", "padh", "bech", "bula", "bulwa", "ruk", "reh", "rah", "bhijwa",
    "dhoondh", "bana", "sambhal", "pahunch", "nikal",
)
# Gujarati: anchored on છું / chhu (I am). "આવી" is also "such" and "રહી"
# agrees with feminine nouns ("ચાલી રહી છે"), so neither may change without
# the first-person copula right after it.
_GU_SCRIPT = [
    ("રહ્યો", "રહી"), ("ગયો", "ગઈ"), ("આવ્યો", "આવી"), ("બોલ્યો", "બોલી"),
    ("કરતો", "કરતી"), ("શકતો", "શકતી"), ("જતો", "જતી"), ("આવતો", "આવતી"),
    ("બેઠો", "બેઠી"), ("પહોંચ્યો", "પહોંચી"), ("જાણતો", "જાણતી"),
    ("કહેતો", "કહેતી"), ("લખતો", "લખતી"), ("મોકલતો", "મોકલતી"),
    ("ઇચ્છતો", "ઇચ્છતી"), ("માંગતો", "માંગતી"), ("વિચારતો", "વિચારતી"),
]
_GU_LAT = [
    ("rahyo", "rahi"), ("gayo", "gayi"), ("aavyo", "aavi"), ("bolyo", "boli"),
    ("karto", "karti"), ("shakto", "shakti"), ("jato", "jati"),
    ("aavto", "aavti"), ("samjyo", "samji"),
]


def _alt(words: Iterable[str]) -> str:
    return "|".join(re.escape(w) for w in sorted(set(words), key=len, reverse=True))


def _match_case(src: str, dst: str) -> str:
    if src.isupper() and len(src) > 1:
        return dst.upper()
    if src[:1].isupper():
        return dst[:1].upper() + dst[1:]
    return dst


class _Rules:
    """Compiled regenderers for one direction (to 'male' or to 'female')."""

    def __init__(self, to: str) -> None:
        fwd = to == "female"
        lat = {m: f for m, f in _HI_LAT} if fwd else {f: m for m, f in _HI_LAT}
        dev = {m: f for m, f in _HI_DEV} if fwd else {
            **{f: m for m, f in _HI_DEV}, **_HI_DEV_FEM_ALIASES}
        gus = {m: f for m, f in _GU_SCRIPT} if fwd else {f: m for m, f in _GU_SCRIPT}
        gul = {m: f for m, f in _GU_LAT} if fwd else {f: m for m, f in _GU_LAT}
        self.lat, self.dev, self.gus, self.gul = lat, dev, gus, gul
        self.fwd = fwd
        L = r"(?<![A-Za-z])"
        R = r"(?![A-Za-z])"
        # A. "<participle> hoon" — only the speaker is "hoon".
        self.lat_hoon = re.compile(
            rf"{L}({_alt(lat)})(\s+)(hoon|hun|hu|hoo){R}", re.IGNORECASE)
        self.dev_hoon = re.compile(
            rf"{_NB}({_alt(dev)})(\s+)(हूँ|हूं|हू){_NA}")
        # B. "samajh gaya" said on its own is always "I understood".
        src_g = "gaya|gya" if fwd else "gayi|gyi|gai"
        self.lat_samajh = re.compile(
            rf"{L}(samajh|samaj|samjh)(\s+)({src_g}){R}", re.IGNORECASE)
        self.dev_samajh = re.compile(
            rf"{_NB}(समझ)(\s+)({'गया' if fwd else 'गई|गयी'}){_NA}")
        # C. the -ūṅgā future is first person singular and nothing else.
        stems = _alt(_HI_FUT_STEMS)
        if fwd:
            self.lat_fut = re.compile(
                rf"{L}((?:{stems})(?:u|oo)|du|lu|doo|loo)(nga){R}", re.IGNORECASE)
            self.dev_fut = re.compile(
                rf"{_NB}([{_DEV_LETTERS}]*?(?:ूँ|ूं|ुं|ुँ|ऊँ|ऊं|ू|ऊ))गा{_NA}")
        else:
            self.lat_fut = re.compile(
                rf"{L}((?:{stems})(?:u|oo)|du|doo)(ngi){R}", re.IGNORECASE)
            self.dev_fut = re.compile(
                rf"{_NB}([{_DEV_LETTERS}]*?(?:ूँ|ूं|ुं|ुँ|ऊँ|ऊं|ू|ऊ))गी{_NA}")
        # D. a clause that opens with "main" owns its first "<p> tha".
        self.lat_tha = re.compile(
            rf"{L}({_alt(lat)})(\s+)({'tha' if fwd else 'thi'}){R}", re.IGNORECASE)
        self.dev_tha = re.compile(
            rf"{_NB}({_alt(dev)})(\s+)({'था' if fwd else 'थी'}){_NA}")
        # Gujarati.
        self.gu_script = re.compile(
            rf"{_NB}({_alt(gus)})(\s+)(છું|છુ){_NA}")
        self.gu_pachho = re.compile(
            rf"{_NB}({'પાછો' if fwd else 'પાછી'})(\s+[{_GU_LETTERS}]+\s+)(છું|છુ){_NA}")
        self.gu_samji = re.compile(
            rf"{_NB}(સમજી)(\s+)({'ગયો' if fwd else 'ગઈ'}){_NA}")
        self.gu_lat = re.compile(
            rf"{L}({_alt(gul)})(\s+)(chhu|chu|chhun){R}", re.IGNORECASE)

    def apply(self, text: str) -> str:
        fwd = self.fwd
        text = self.lat_hoon.sub(
            lambda m: _match_case(m.group(1), self.lat[m.group(1).lower()])
            + m.group(2) + m.group(3), text)
        text = self.dev_hoon.sub(
            lambda m: self.dev[m.group(1)] + m.group(2) + m.group(3), text)
        text = self.lat_samajh.sub(
            lambda m: m.group(1) + m.group(2)
            + _match_case(m.group(3), "gayi" if fwd else "gaya"), text)
        text = self.dev_samajh.sub(
            lambda m: m.group(1) + m.group(2) + ("गई" if fwd else "गया"), text)
        text = self.lat_fut.sub(
            lambda m: m.group(1) + _match_case(m.group(2), "ngi" if fwd else "nga"),
            text)
        text = self.dev_fut.sub(
            lambda m: m.group(1) + ("गी" if fwd else "गा"), text)
        text = self._clause_tha(text)
        text = self.gu_script.sub(
            lambda m: self.gus[m.group(1)] + m.group(2) + m.group(3), text)
        text = self.gu_pachho.sub(
            lambda m: ("પાછી" if fwd else "પાછો") + m.group(2) + m.group(3), text)
        text = self.gu_samji.sub(
            lambda m: m.group(1) + m.group(2) + ("ગઈ" if fwd else "ગયો"), text)
        text = self.gu_lat.sub(
            lambda m: _match_case(m.group(1), self.gul[m.group(1).lower()])
            + m.group(2) + m.group(3), text)
        return text

    _CLAUSE = re.compile(r"[^.!?,;:।\n—–]+")
    _OPENS_WITH_MAIN = re.compile(
        rf"^\s*(?:['\"‘“(]\s*)?(?:main|mai|मैं)(?![{_LETTERS}])",
        re.IGNORECASE)

    def _clause_tha(self, text: str) -> str:
        fwd = self.fwd

        def _one(cm: re.Match) -> str:
            clause = cm.group(0)
            if not self._OPENS_WITH_MAIN.match(clause):
                return clause
            clause = self.lat_tha.sub(
                lambda m: _match_case(m.group(1), self.lat[m.group(1).lower()])
                + m.group(2) + _match_case(m.group(3), "thi" if fwd else "tha"),
                clause, count=1)
            return self.dev_tha.sub(
                lambda m: self.dev[m.group(1)] + m.group(2)
                + ("थी" if fwd else "था"), clause, count=1)

        return self._CLAUSE.sub(_one, text)


    def count(self, text: str) -> int:
        """How many first-person forms of the SOURCE gender `text` carries."""
        n = 0
        for rx in (self.lat_hoon, self.dev_hoon, self.lat_samajh,
                   self.dev_samajh, self.lat_fut, self.dev_fut,
                   self.gu_script, self.gu_pachho, self.gu_samji, self.gu_lat):
            n += len(rx.findall(text))
        return n


_RULES = {"female": _Rules("female"), "male": _Rules("male")}


def authored_self_gender(texts: Iterable[str]) -> str:
    """The first person the owner actually WROTE — 'male' when the script
    says "main bol raha hoon", 'female' for "bol rahi hoon", '' when it
    commits to neither.

    Used only to pick a DEFAULT voice for an agent built without one: the
    Copilot used to default every unvoiced agent to a female speaker, so the
    Kaapd brief's "मैं कापड़, सूरत से करण बोल रहा हूँ" was given priya — the
    jai_mistry-2 defect, created by the platform instead of by a user. This is
    evidence from the owner's own words, never a guess from the agent's name."""
    masc = fem = 0
    for t in texts:
        if t:
            masc += _RULES["female"].count(t)    # masculine forms present
            fem += _RULES["male"].count(t)       # feminine forms present
    if masc > fem:
        return "male"
    if fem > masc:
        return "female"
    return ""


def regender_self(text: str, gender: str) -> str:
    """Put the agent's own first-person agreement in `gender`.

    "main kar raha hoon" → "main kar rahi hoon" for a female voice and back
    for a male one; "समझ गया" → "समझ गई"; "karunga" → "karungi"; "હું નોંધી
    રહ્યો છું" → "રહી છું". Anything not unambiguously FIRST person is left
    alone. An unknown gender changes nothing."""
    rules = _RULES.get(known_gender(gender))
    if not text or rules is None:
        return text
    return rules.apply(text)


def render_author_line(text: str, *, contact_name: str = "",
                       contact_gender: str = "", agent_gender: str = "",
                       protected: Iterable[str] = ()) -> str:
    """An authored line the PROMPT quotes to the model (a stage goal's
    verbatim script, an outbound.* line), rendered the way the caller will
    hear it: placeholders resolved, literal address held to the contact's
    gender, the agent's own agreement to its voice.

    The model is told to reproduce quoted lines word for word; handing it
    "Thank you {honorific}" or "Main … chahta tha" for a simran voice is
    handing it braces or the wrong gender to copy."""
    if not text:
        return text
    t = resolve_contact_name(text, contact_name)
    had_token = False
    # The LINE's own script picks the word, not the call's language: a
    # romanized Hinglish script line gets "Ma'am", never a Devanagari मैडम
    # dropped into the middle of Latin text.
    if HONORIFIC_PH_RE.search(t):
        w = honorific_word(contact_gender, t)
        t = HONORIFIC_PH_RE.sub(lambda _m: w, t)
        had_token = had_token or not w
    if TITLE_PH_RE.search(t):
        w = title_word(contact_gender, t)
        t = TITLE_PH_RE.sub(lambda _m: w, t)
        had_token = had_token or not w
    if had_token:
        t = tidy_seams(t)
    t = enforce_address(t, contact_gender, contact_name, protected)
    return regender_self(t, agent_gender)
