"""Deterministic spoken-contact capture: normalization + validation (B1).

Phone-call ASR mangles spoken emails in predictable ways (live call
2026-07-30, deploy/PLAN-language-fidelity-and-contact-capture.md):

- ITN glues "gmail dot com" into "www.gmail.com"       → "bell www.gmail.com"
- spelled letters come back as separated capitals       → "C O N Y BN @"
- the Whisper fallback transliterates letter NAMES into
  Devanagari                                            → "एल दब्यू वाय एल"
- operator words are spoken, not symbols                → "ben at gmail dot com"

The pipeline buffers capture-mode fragments; this module turns the joined
spoken form into a canonical candidate and validates it. The LLM never
writes these values — merge_state rejects source="llm" for contact-typed
slots — so the only path into state is this normalizer + the caller's
explicit confirmation.
"""

from __future__ import annotations

import re

# Local-part @ domain.tld — deliberately simple: the goal is "plausible
# email", not RFC 5321. Underscores/dots/dashes/plus in the local part.
EMAIL_RE = re.compile(
    r"^[a-z0-9][a-z0-9._+\-]*@[a-z0-9][a-z0-9\-]*(\.[a-z0-9\-]+)+$")

# Spoken operator words → symbols. Multi-word forms before single words.
_OPERATOR_SUBS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bat\s+the\s+rate\s+of\b"), " @ "),
    (re.compile(r"\bat\s+the\s+rate\b"), " @ "),
    (re.compile(r"\bat\s+(?:d|da|de)\s+rate\b"), " @ "),
    (re.compile(r"\bएट\s+(?:द|दी|डी|दि)\s+रेट\b"), " @ "),
    (re.compile(r"\bएट\s+रेट\b"), " @ "),
    (re.compile(r"\b(?:at|एट|एट्)\b"), " @ "),
    (re.compile(r"\b(?:dot|डॉट|डाट|dott)\b"), " . "),
    (re.compile(r"\b(?:underscore|अंडरस्कोर)\b"), " _ "),
    (re.compile(r"\b(?:dash|hyphen|डैश|हाइफन)\b"), " - "),
    (re.compile(r"\b(?:plus|प्लस)\b"), " + "),
]

# English letter NAMES that ASR writes as words ("double u" → w, "why" → y).
_LETTER_NAME_WORDS = {
    "double": None,          # handled as "double u/you" bigram below
    "you": "u", "u": "u",
    "why": "y", "y": "y",
    "are": "r", "r": "r",
    "bee": "b", "be": "b", "b": "b",
    "see": "c", "sea": "c", "c": "c",
    "dee": "d", "d": "d",
    "ee": "e", "e": "e",
    "ef": "f", "eff": "f", "f": "f",
    "gee": "g", "g": "g",
    "aitch": "h", "h": "h",
    "eye": "i", "i": "i",
    "jay": "j", "j": "j",
    "kay": "k", "k": "k",
    "el": "l", "ell": "l", "l": "l",
    "em": "m", "m": "m",
    "en": "n", "n": "n",
    "oh": "o", "o": "o",
    "pee": "p", "p": "p",
    "cue": "q", "queue": "q", "q": "q",
    "es": "s", "ess": "s", "s": "s",
    "tee": "t", "tea": "t", "t": "t",
    "vee": "v", "v": "v",
    "ex": "x", "x": "x",
    "zed": "z", "zee": "z", "z": "z",
    "a": "a", "j": "j", "k": "k", "w": "w",
}

# Devanagari letter-name transliterations (the Whisper fallback path:
# "एल दब्यू वाय एल" was the caller spelling L W Y L).
_DEVA_LETTER_NAMES = {
    "ए": "a", "एह": "a",
    "बी": "b", "सी": "c", "डी": "d", "ई": "e", "इ": "e",
    "एफ": "f", "एफ़": "f", "जी": "g", "एच": "h",
    "आई": "i", "आय": "i", "जे": "j", "के": "k",
    "एल": "l", "एम": "m", "एन": "n", "ओ": "o",
    "पी": "p", "क्यू": "q", "आर": "r", "एस": "s",
    "टी": "t", "यू": "u", "वी": "v",
    "डब्ल्यू": "w", "डब्लू": "w", "दब्यू": "w", "डबल्यू": "w",
    "एक्स": "x", "वाय": "y", "वाई": "y", "जेड": "z", "ज़ेड": "z",
}

# Common mail providers for edit-distance snapping ("jimail" → gmail) and
# the domains ITN likes to prefix with "www.".
_KNOWN_DOMAINS = [
    "gmail", "yahoo", "outlook", "hotmail", "rediffmail", "icloud",
    "protonmail", "proton", "live", "aol", "zoho",
]
_TLD_FIXES = {
    "con": "com", "comm": "com", "vom": "com", "cim": "com",
    "male": "mail", "mali": "mail",
    "inn": "in", "co,": "co",
}


def _edit1(a: str, b: str) -> bool:
    """Damerau distance ≤ 1: one substitution, insertion, deletion, or
    adjacent transposition ("gmial" → "gmail")."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if la == lb:
        diffs = [i for i in range(la) if a[i] != b[i]]
        if (len(diffs) == 2 and diffs[1] == diffs[0] + 1
                and a[diffs[0]] == b[diffs[1]]
                and a[diffs[1]] == b[diffs[0]]):
            return True
    if abs(la - lb) > 1:
        return False
    if la > lb:
        a, b, la, lb = b, a, lb, la
    i = j = diff = 0
    while i < la and j < lb:
        if a[i] == b[j]:
            i += 1
            j += 1
            continue
        diff += 1
        if diff > 1:
            return False
        if la == lb:
            i += 1
        j += 1
    return True


def _snap_domain(domain: str) -> str:
    """'jimail.con' → 'gmail.com': strip www., snap the provider by edit
    distance 1, fix common TLD mishears."""
    domain = domain.lstrip(".")
    while domain.startswith("www."):
        domain = domain[4:]
    parts = [p for p in domain.split(".") if p]
    if not parts:
        return domain
    # "email"/"mail" are ordinary words within edit-1 of gmail — never snap.
    if parts[0] not in ("email", "mail"):
        for known in _KNOWN_DOMAINS:
            if _edit1(parts[0], known):
                parts[0] = known
                break
    parts = [_TLD_FIXES.get(p, p) for p in parts]
    if len(parts) == 1:
        # Bare provider ("gmail") — the caller said "at gmail" and stopped.
        parts.append("com")
    return ".".join(parts)


def normalize_spoken_email(raw: str) -> str | None:
    """Turn a joined spoken-form buffer into a canonical email candidate,
    or None when nothing email-shaped can be assembled.

    Handles: operator words (at/dot, Hindi एट/डॉट), spelled letters (Latin
    single letters, English letter names, Devanagari letter names), the ITN
    "www." artifact, provider/TLD snapping, embedded surrounding speech
    ("ben @gmail.com मैंने बोला।" → ben@gmail.com).
    """
    if not raw or not raw.strip():
        return None
    t = " " + raw.lower().strip() + " "

    # Devanagari letter names → Latin letters (before dropping Devanagari).
    for name, letter in sorted(_DEVA_LETTER_NAMES.items(),
                               key=lambda kv: -len(kv[0])):
        t = t.replace(f" {name} ", f" {letter} ").replace(
            f" {name}।", f" {letter} ")

    # Operator words → symbols.
    for pat, sub in _OPERATOR_SUBS:
        t = pat.sub(sub, t)

    # "double u/you" bigram → w.
    t = re.sub(r"\bdouble\s+(?:u|you)\b", " w ", t)

    # Drop any remaining non-ASCII (surrounding Hindi speech: "मैंने बोला").
    t = "".join(ch if ch.isascii() else " " for ch in t)

    # English letter-name words become letters ONLY in runs of ≥2 short
    # tokens (a lone "you"/"are" in normal speech must not turn into u/r).
    tokens = t.split()
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        mapped = _LETTER_NAME_WORDS.get(tok)
        if mapped and (
            (i + 1 < len(tokens) and _LETTER_NAME_WORDS.get(tokens[i + 1]))
            or (out and len(out[-1]) == 1 and out[-1].isalpha())
            or len(tok) == 1
        ):
            out.append(mapped)
        else:
            out.append(tok)
        i += 1

    # Join spelled runs: ≥2 consecutive short tokens, mostly single letters
    # ("c o n y bn" → "conybn"; "is my" stays two words).
    joined: list[str] = []
    run: list[str] = []

    def _flush_run() -> None:
        if not run:
            return
        singles = sum(1 for r in run if len(r) == 1)
        if len(run) >= 2 and singles / len(run) >= 0.6:
            joined.append("".join(run))
        else:
            joined.extend(run)
        run.clear()

    for tok in out:
        if len(tok) <= 2 and tok.replace("-", "").isalnum():
            run.append(tok)
        else:
            _flush_run()
            joined.append(tok)
    _flush_run()
    t = " ".join(joined)

    # Fuse symbols: "@"/"." absorb surrounding spaces; leftover single
    # letters glue to their neighbours.
    t = re.sub(r"\s*@\s*", "@", t)
    t = re.sub(r"\s*\.\s*", ".", t)
    t = re.sub(r"(?<=\b[a-z0-9])\s+(?=[a-z0-9]\b)", "", t)   # "b en" → "ben"
    t = re.sub(r"(?<=[a-z0-9])\s+(?=[@._+\-])", "", t)
    t = re.sub(r"(?<=[@._+\-])\s+(?=[a-z0-9])", "", t)

    # Pick the best email-shaped token out of the remaining speech.
    # RC9 (2026-07-31): spelled local-part material immediately BEFORE the
    # @-token belongs to the address — 'angelo.pa knaan@gmail.com' (from
    # 'ए एन जी ई एल ओ। डॉट। पी ए। knaan@…') must assemble as
    # 'angelo.pa.knaan@gmail.com', not drop the spelled prefix. Only tokens
    # that are clearly spelled material qualify (contain a ./_ separator or
    # are a single letter) — ordinary words ("is", "email") never join.
    _words = t.split()
    scored: list[tuple[int, int, str]] = []          # (len, -idx, candidate)
    for _idx, _w in enumerate(_words):
        if "@" not in _w:
            continue
        cand = _w.strip(".,;:!?\"'()")
        local, _, domain = cand.partition("@")
        prefix: list[str] = []
        j = _idx - 1
        while j >= 0 and len(prefix) < 3:
            p = _words[j].strip(".,;:!?\"'()")
            if not (re.fullmatch(r"[a-z0-9]+(?:[._][a-z0-9]+)+", p)
                    or (len(p) == 1 and p.isalnum())):
                break
            prefix.insert(0, p)
            j -= 1
        if prefix and local:
            local = ".".join(prefix + [local])
        elif prefix:
            local = ".".join(prefix)
        scored.append((len(local) + len(domain), -_idx, f"{local}@{domain}"))
    for _, _, cand in sorted(scored, reverse=True):
        local, _, domain = cand.partition("@")
        local = local.strip("._+-")
        domain = _snap_domain(domain)
        candidate = f"{local}@{domain}".strip()
        if EMAIL_RE.match(candidate):
            return candidate

    # No "@" anywhere (ITN artifact: "bell www.gmail.com") — a bare
    # "<word> <provider-domain>" adjacency is the caller's local part
    # followed by their provider.
    words = [w.strip(".,;:!?\"'()") for w in t.split()]
    for i, w in enumerate(words):
        if "." not in w or i == 0:
            continue
        domain = _snap_domain(w)
        head = domain.split(".")[0]
        local = words[i - 1].strip("._+-")
        if (head in _KNOWN_DOMAINS and local
                and local.isalnum() and not local.isdigit()):
            candidate = f"{local}@{domain}"
            if EMAIL_RE.match(candidate):
                return candidate
    return None


def validate_email(value: str) -> bool:
    """Slot-type validator: canonical form only."""
    return bool(value) and bool(EMAIL_RE.match(str(value).strip().lower()))


def looks_like_partial_email(raw: str) -> bool:
    """Does this fragment look like the caller is mid-way through giving an
    email (worth buffering silently instead of answering)? True on: an @ or
    operator word, a spelled-letter run, or a known provider name."""
    if not raw or not raw.strip():
        return False
    t = " " + raw.lower() + " "
    if "@" in t or re.search(r"\b(?:at|dot|एट|डॉट)\b", t):
        return True
    if re.search(r"\b(?:mail|gmail|yahoo|outlook|hotmail|rediffmail)\w*\b", t):
        return True
    tokens = [w.strip(".,;:!?") for w in t.split()]
    letterish = sum(
        1 for w in tokens
        if (len(w) == 1 and w.isalpha() and w.isascii())
        or w in _DEVA_LETTER_NAMES
    )
    return letterish >= 2


def spelled_readback(value: str) -> str:
    """B3 'spelled' read-back: local part letter-by-letter (dots induce TTS
    pauses — bulbul v3 has no SSML), domain spoken naturally.
    'ben@gmail.com' → 'b. e. n. — at — gmail dot com'."""
    s = str(value or "").strip()
    if "@" not in s:
        return s
    local, _, domain = s.partition("@")
    spelt = ". ".join(local) + "."
    domain_spoken = domain.replace(".", " dot ")
    return f"{spelt} — at — {domain_spoken}"
