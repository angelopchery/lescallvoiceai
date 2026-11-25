"""Voice prompting for the Copilot composer — mic in, clean prompt out.

Speech recognition gets ordinary English right and proper nouns wrong. In a
copilot chat the proper nouns ARE the message: "make hardik-2-inbound greet
in Gujarati" is useless as "make hard it to in bound greet in gujrati". So the
pipeline is built around that failure mode rather than around generic
spellchecking.

Four stages, cheapest first, each one able to stand alone if the next is
unavailable:

  1. STT — Sarvam saarika with language_code=unknown (the composer is used in
     English and Hinglish; forcing en-IN mangles the Hindi words people mix
     in). Same provider, key pool and dynamic normalizer the call pipeline
     uses; a fresh SarvamSTT per request so one speaker's AGC state never
     leaks into the next, over the process-shared HTTPS connection so there is
     no TLS handshake to pay for.
  2. Romanize — Hindi and Gujarati come back from the recognizer in their own
     script ("आप कैसे हो"), and this composer is a Latin-script text box that
     people type Hinglish into. Any Indic run is transliterated to the way it
     is actually written down here ("Aap kaise ho"). It costs one request, it
     only fires when Indic script is present — an English prompt never pays
     for it — and it reuses the HTTPS connection the recognizer just warmed,
     to the same host. The original is kept and returned either way.
  3. Repair — deterministic, ~1 ms, no network. Fixes what stage 1 actually
     gets wrong: the caller's OWN vocabulary (their agent slugs, agent names,
     company names, projects, voices) plus this platform's jargon, matched
     fuzzily over 1-3 word windows and rewritten to the canonical spelling.
  4. Tidy — fillers, stutter repeats, sentence casing, terminal punctuation.
  5. Polish — OPTIONAL model pass for grammar and word order. Off by default
     (COPILOT_VOICE_POLISH=1 to enable) and hard-capped by a timeout: stage 2
     already fixes the errors that change meaning, and a second network
     round-trip is the difference between a mic that feels instant and one
     that feels like a form submission.

Deliberately NOT reused: app/voice/stt/post_processor.py. That module is the
CALL pipeline's real-estate vocabulary — it rewrites "super" to "surat" and
"dadra" to "silvassa", which is correct for a Pramukh caller naming a city and
catastrophic for someone typing "make it super short" into a chat box.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import time
import wave

logger = logging.getLogger("copilot_voice")

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MAX_AUDIO_BYTES = 12 * 1024 * 1024      # ~6 min of 16k mono WAV
MAX_AUDIO_SECONDS = 120.0
TARGET_SR = 16_000

# A one-word rewrite is the risky one — "make it super short" must never
# become a proper noun — so it needs near-certainty. Multi-word spans carry
# their own evidence (three tokens agreeing is not a coincidence).
_MIN_SCORE_1 = 84.0
_MIN_SCORE_N = 84.0
_MIN_TOKEN_LEN = 4
# fuzz.ratio is unkind to short words — "shub" vs "shubh" scores 88.9, which
# is plainly the same name misheard. Below the confident-score line a single
# edit is accepted on its own, which is precisely the "spelling mistake"
# case, and nothing looser is.
_CONFIDENT_SCORE = 90.0
_MORPH_SUFFIXES = ("s", "es", "ed", "d", "ing", "ings", "er", "ers")

POLISH_ENABLED = os.environ.get("COPILOT_VOICE_POLISH", "").strip().lower() in ("1", "true", "yes")
POLISH_TIMEOUT_S = float(os.environ.get("COPILOT_VOICE_POLISH_TIMEOUT_S", "1.2"))

# One contiguous block covers Devanagari through Malayalam — every script
# Sarvam's transliterator accepts, and every script this platform's callers
# speak in.
_INDIC_RE = re.compile(r"[ऀ-ൿ]")
TRANSLITERATE_URL = os.environ.get(
    "SARVAM_TRANSLITERATE_URL", "https://api.sarvam.ai/transliterate")
TRANSLITERATE_TIMEOUT_S = float(
    os.environ.get("COPILOT_VOICE_TRANSLIT_TIMEOUT_S", "2.5"))
# Sarvam romanizes from these; anything else (or a mix) goes as "auto".
_TRANSLIT_SOURCES = frozenset((
    "hi-IN", "bn-IN", "gu-IN", "kn-IN", "ml-IN", "mr-IN",
    "od-IN", "pa-IN", "ta-IN", "te-IN"))


# ---------------------------------------------------------------------------
# Audio decoding
# ---------------------------------------------------------------------------

def decode_audio(raw: bytes) -> tuple[bytes, int]:
    """Any recording → (PCM16 mono bytes, sample_rate).

    The browser sends 16 kHz mono WAV it built itself, so the fast path is
    pure stdlib and costs microseconds. Anything else (a webm/ogg upload, a
    phone recording) goes through ffmpeg, which is why that path exists at
    all — it is never taken by our own composer.
    """
    if raw[:4] == b"RIFF":
        try:
            return _decode_wav(raw)
        except Exception as exc:
            logger.warning("[voice] WAV decode failed (%r) — trying ffmpeg", exc)
    return _decode_ffmpeg(raw)


def _decode_wav(raw: bytes) -> tuple[bytes, int]:
    with wave.open(io.BytesIO(raw), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError(f"expected 16-bit PCM, got {wf.getsampwidth() * 8}-bit")
        sr = wf.getframerate()
        pcm = wf.readframes(wf.getnframes())
        if wf.getnchannels() > 1:
            pcm = _to_mono(pcm, wf.getnchannels())
    return pcm, sr


def _to_mono(pcm: bytes, channels: int) -> bytes:
    # array, not audioop: audioop is deprecated and gone in Python 3.13.
    import array
    import sys as _sys
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) - (len(pcm) % (2 * channels))])
    if _sys.byteorder != "little":
        samples.byteswap()
    out = array.array("h", (sum(samples[i:i + channels]) // channels
                            for i in range(0, len(samples), channels)))
    if _sys.byteorder != "little":
        out.byteswap()
    return out.tobytes()


def _decode_ffmpeg(raw: bytes) -> tuple[bytes, int]:
    import subprocess
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
         "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "1",
         "-ar", str(TARGET_SR), "pipe:1"],
        input=raw, capture_output=True, timeout=30,
    )
    if proc.returncode != 0 or not proc.stdout:
        raise ValueError(
            (proc.stderr or b"").decode("utf-8", "replace")[:200]
            or "could not decode the recording")
    return proc.stdout, TARGET_SR


# ---------------------------------------------------------------------------
# Stage 2 — romanize
# ---------------------------------------------------------------------------

def has_indic(text: str) -> bool:
    return bool(_INDIC_RE.search(text or ""))


async def romanize(text: str, language: str = "", client=None) -> str:
    """Indic script → the Hinglish people actually type ("Aap kaise ho").

    Returns `text` unchanged on any failure. Falling back to the original
    script is the honest degradation: it is exactly what this box showed
    before romanization existed, and it is self-evident on screen. A
    hand-rolled transliteration table would instead fail SILENTLY and
    plausibly — "sabaa thheeka" reads like a word someone meant.
    """
    if not has_indic(text):
        return text
    src = language if language in _TRANSLIT_SOURCES else "auto"
    try:
        from backend.app.voice.key_rotator import current_key
    except Exception:                                          # pragma: no cover
        return text
    if client is None:
        import httpx
        client = httpx.AsyncClient(timeout=TRANSLITERATE_TIMEOUT_S)
    try:
        resp = await asyncio.wait_for(
            client.post(
                TRANSLITERATE_URL,
                headers={"api-subscription-key": current_key()},
                json={
                    "input": text,
                    "source_language_code": src,
                    "target_language_code": "en-IN",
                    # Keep "2" as "2", not "do": the composer's whole job is
                    # naming agents and numbers, and a spelled-out digit
                    # breaks the slug repair that runs next.
                    "spoken_form": False,
                    "numerals_format": "international",
                },
            ),
            timeout=TRANSLITERATE_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning("[voice] transliterate exceeded %.1fs — keeping %s",
                       TRANSLITERATE_TIMEOUT_S, src)
        return text
    except Exception as exc:
        logger.warning("[voice] transliterate failed: %r", exc)
        return text
    if resp.status_code != 200:
        logger.warning("[voice] transliterate HTTP %s: %s",
                       resp.status_code, resp.text[:160])
        return text
    try:
        out = (resp.json().get("transliterated_text") or "").strip()
    except Exception:
        return text
    # A transliterator that hands back Indic script did nothing; a blank one
    # would silently delete what the user said.
    return out if out and not has_indic(out) else text


# ---------------------------------------------------------------------------
# Stage 3 — vocabulary repair
# ---------------------------------------------------------------------------

# This platform's own jargon. Every entry here is a word STT has a reason to
# get wrong (a coinage, an acronym, a brand, or a term of art); ordinary
# English is left to the recognizer, which is already good at it.
_PRODUCT_LEXICON: tuple[str, ...] = (
    "Copilot", "inbound", "outbound", "greeting", "opening line", "barge-in",
    "voicemail", "IVR", "DID", "SIP", "trunk", "webhook", "WhatsApp",
    "Sarvam", "FreeSWITCH", "Plivo", "Razorpay", "Postgres", "Gujarati",
    "Hindi", "Hinglish", "Marathi", "Kannada", "Telugu", "Tamil", "Bengali",
    "Punjabi", "transcript", "escalate", "appointment", "follow-up",
    "callback", "do-not-disturb", "DND", "knowledge base", "guardrail",
    "persona", "auto-dialer", "campaign", "qualify", "lead", "CRM",
    "onboarding", "brochure", "catalogue", "pipeline", "latency",
    "dashboard", "analytics", "activate", "deactivate", "agent",
    "conversation stage", "slot", "placeholder", "contact name",
    "current time", "hand off", "handover", "opt-out", "consent",
)

# Words the recognizer is entitled to have meant literally. Without this a
# high fuzzy score quietly turns a normal sentence into jargon — the exact
# failure the call pipeline's post-processor documents ("silver" must not
# become "silvassa").
_PROTECTED = frozenset("""
about after again against agent all also always another answer any anything
are ask asked back because been before being best better between both build
building built call called calls can change changed check come could country
create day days did different does doing done down during each even every
first following for form found from get give given going good great group
had has have help her here him his how into its just keep know last let like
line link list little long look made make making many may mean might more
most move much must name need never new next night now number off often old
once only open other our out over own part people place please point put
right said same say see seem send sent set she short should show side since
small some something sound speak start state still such sure take talk tell
than that the their them then there these they thing think this those
though three through time today together too took top try turn two under
until use used using very want way week well went were what when where
which while who why will with word work would year yes your
""".split())

# Function words carry the shape of the sentence, not its content. A window
# that STARTS or ENDS on one has been cut in the wrong place: "greet in" is
# a verb and a preposition that happen to concatenate into something scoring
# 93 against "greeting", while "hardik to inbound" — content word at both
# ends — really is a slug the recognizer split.
_FUNCTION = frozenset("""
a an and as at be but by do for from go had has have he her him his i if in
is it its me my no not of on or our out so the this that to too up us was we
were will with you your
""".split())


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


class Lexicon:
    """Canonical spellings, indexed by their normalized (space- and
    punctuation-free) form.

    Deliberately NOT bucketed by word count. "hardik-2-inbound" is one word
    written down and three words spoken, "follow-up" is one or two, and a
    recognizer that hears "hardik to inbound" produces a three-token window
    that has to be matched against a one-token entry. Since every comparison
    happens on the normalized form, token count carries no information — and
    bucketing on it silently made every multi-word mishearing unmatchable.
    """

    def __init__(self, phrases) -> None:
        self.by_key: dict[str, str] = {}
        for phrase in phrases:
            phrase = str(phrase or "").strip()
            if not phrase or len(phrase) > 60:
                continue
            key = _norm(phrase)
            if len(key) < 3:
                continue
            # First writer wins: the tenant's own names are added before the
            # generic product words, so "Agent Pooja" stays theirs.
            self.by_key.setdefault(key, phrase)
        self.keys: list[str] = list(self.by_key)

    def __bool__(self) -> bool:
        return bool(self.by_key)


_TRAILING_PUNCT = re.compile(r"[.,!?;:]+$")


def _is_morph_variant(key: str, cand_key: str) -> bool:
    """Are these the same word in different grammatical dress?"""
    a, b = (key, cand_key) if len(key) > len(cand_key) else (cand_key, key)
    return a.startswith(b) and a[len(b):] in _MORPH_SUFFIXES


def _canonicalize(window: str, exact: str) -> str | None:
    """The canonical spelling for a span that already matched exactly, or
    None when the span is fine as written.

    Two different repairs hide here. Re-spacing ("in bound" → "inbound",
    "hardik 2 inbound" → "hardik-2-inbound") is always right: the speaker
    said the word, the recognizer split it. Re-CASING is only right when the
    canonical form actually carries case — otherwise a sentence-initial
    "Agent" gets quietly lowercased on its way to the model.
    """
    if window == exact:
        return None
    if window.lower() == exact.lower():
        return exact if exact.lower() != exact else None
    return exact


def repair_vocabulary(text: str, lex: Lexicon) -> tuple[str, list[dict]]:
    """Rewrite mis-heard spans to their canonical spelling.

    One left-to-right pass, trying the widest span first, appending to a new
    token list — so a replacement that changes the token count ("b2market" →
    "B2 Market") can never corrupt the scan that produced it.
    """
    if not text.strip() or not lex:
        return text, []
    try:
        from rapidfuzz import fuzz, process
    except Exception:                                  # pragma: no cover
        return text, []

    from rapidfuzz.distance import Levenshtein

    tokens = text.split()
    out: list[str] = []
    fixes: list[dict] = []
    i = 0
    while i < len(tokens):
        matched = 0
        for span in (3, 2, 1):
            if i + span > len(tokens):
                continue
            window = " ".join(tokens[i:i + span])
            # Punctuation rides along on the last token and must survive the
            # rewrite — "in gujrati." has to come back with its full stop.
            tail = _TRAILING_PUNCT.search(window)
            suffix = tail.group(0) if tail else ""
            bare = window[:len(window) - len(suffix)] if suffix else window
            key = _norm(bare)
            if len(key) < 3:
                continue
            # Before ANY branch, including the exact one: an ordinary English
            # word stays an ordinary English word. "how many calls did we
            # take" must never become "…DID we take" just because this
            # platform happens to call a phone number a DID.
            if span == 1 and (bare.lower() in _PROTECTED
                              or bare.lower() in _FUNCTION):
                continue
            exact = lex.by_key.get(key)
            if exact is not None:
                repl = _canonicalize(bare, exact)
                if repl is not None:
                    fixes.append({"from": bare, "to": repl, "kind": "spelling"})
                    out.extend((repl + suffix).split())
                else:
                    out.extend(window.split())
                matched = span
                break
            if len(key) < (_MIN_TOKEN_LEN if span == 1 else 5):
                continue
            edge = [t.strip(".,!?;:").lower() for t in (tokens[i], tokens[i + span - 1])]
            if span > 1 and any(t in _FUNCTION for t in edge):
                continue
            hit = process.extractOne(
                key, lex.keys, scorer=fuzz.ratio,
                score_cutoff=_MIN_SCORE_1 if span == 1 else _MIN_SCORE_N)
            # A rewrite that changes the opening sound is a different word,
            # not a mishearing. This is what keeps "super" from becoming a
            # place name it merely scores well against.
            if not hit or key[:1] != hit[0][:1]:
                continue
            cand = lex.by_key[hit[0]]
            # "agents", "slots", "greetings" are the plural the speaker meant,
            # not a mishearing of the singular — but "Poojas" IS the name
            # Pooja with a possessive the recognizer swallowed. Grammar
            # applies to the jargon; proper nouns are just names.
            if cand.islower() and _is_morph_variant(key, hit[0]):
                continue
            # Single words need near-certainty or a single edit — one token is
            # thin evidence and the downside is rewriting plain English. A
            # multi-token window has already cleared the edge guard and is
            # matching on ten-plus characters of content, where the score
            # cutoff is the evidence ("hardik to inbound" → the slug is two
            # edits and unmistakable).
            if span == 1 and hit[1] < _CONFIDENT_SCORE and not (
                    len(key) >= _MIN_TOKEN_LEN
                    and Levenshtein.distance(key, hit[0]) <= 1):
                continue
            fixes.append({"from": bare, "to": cand, "kind": "vocab",
                          "score": round(float(hit[1]), 1)})
            out.extend((cand + suffix).split())
            matched = span
            break
        if matched:
            i += matched
        else:
            out.append(tokens[i])
            i += 1

    return " ".join(out), fixes


# ---------------------------------------------------------------------------
# Stage 4 — tidy
# ---------------------------------------------------------------------------

_FILLERS = ("um", "uh", "umm", "uhh", "uhm", "erm", "hmm", "hmmm", "mmm")
_FILLER_RE = re.compile(r"(?<!\w)(?:%s)(?=[\s,.]|$)" % "|".join(_FILLERS), re.I)
# Only squash a stutter, never a real doubling. "no no" and "very very" are
# things people mean; "greeting greeting" is the recognizer hiccuping.
_STUTTER_OK = frozenset(("no", "yes", "very", "really", "ha", "ho", "so", "bye"))
_STUTTER_RE = re.compile(r"\b([A-Za-z][\w'-]{2,})(\s+\1)+\b", re.I)


def tidy(text: str) -> str:
    text = _FILLER_RE.sub("", text)
    text = _STUTTER_RE.sub(
        lambda m: m.group(1) if m.group(1).lower() not in _STUTTER_OK else m.group(0),
        text)
    text = re.sub(r"\s+([,.!?;:])", r"\1", text)
    text = re.sub(r"\s{2,}", " ", text).strip(" ,")
    if not text:
        return ""
    # Recognizers return lowercase and unpunctuated; a prompt that reads like
    # a sentence is also a prompt the copilot's model reads more reliably.
    text = re.sub(r"(?<![\w'])i(?![\w'])", "I", text)
    # Capitalize the opening word — unless it is an identifier. A sentence
    # that starts with an agent slug must keep it verbatim: "hardik-2-inbound"
    # is a directory name, and "Hardik-2-inbound" is not that directory.
    first = text.split(" ", 1)[0]
    if first.isalpha() and first.islower():
        text = text[0].upper() + text[1:]
    if text[-1] not in ".!?":
        text += "?" if _is_question(text) else "."
    return text


# Hinglish puts the question word in the middle as often as at the front —
# "Aap kaise ho" is a question and "kaise" is its third word — so these are
# matched anywhere, unlike the English set which only counts sentence-initial.
_HINGLISH_Q = frozenset("""
kya kyaa kaise kaisa kaisi kaun kaunsa konsa kab kahan kahaan kidhar kyun kyu
kyon kitna kitne kitni kem shu su
""".split())
_ENGLISH_Q = re.compile(
    r"^(who|what|when|where|why|how|which|can|could|do|does|did|is|are|"
    r"will|would|should|may|might|shall)\b", re.I)


def _is_question(text: str) -> bool:
    if _ENGLISH_Q.match(text):
        return True
    return any(w.strip(".,!?;:").lower() in _HINGLISH_Q for w in text.split())


# ---------------------------------------------------------------------------
# Stage 4 — optional model polish
# ---------------------------------------------------------------------------

_POLISH_SYSTEM = (
    "You clean up speech-to-text output. Fix grammar, word order and obvious "
    "misrecognitions. NEVER answer, explain, translate or add anything — the "
    "text is a prompt someone dictated, not a question for you. Keep every "
    "proper noun, agent name, slug and technical term EXACTLY as written. "
    "Reply with the corrected text and nothing else."
)


async def polish(text: str, get_client, model: str) -> str:
    """Model pass for grammar and word order. Returns `text` unchanged on any
    failure or timeout — a slow model must degrade to a fast mic, never to a
    spinner."""
    if not text or len(text.split()) < 3:
        return text
    try:
        client = get_client()
        resp = await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": _POLISH_SYSTEM},
                          {"role": "user", "content": text}],
                temperature=0,
                max_tokens=256,
            ),
            timeout=POLISH_TIMEOUT_S,
        )
        out = (resp.choices[0].message.content or "").strip()
    except asyncio.TimeoutError:
        logger.info("[voice] polish exceeded %.1fs — keeping the fast text",
                    POLISH_TIMEOUT_S)
        return text
    except Exception as exc:
        logger.warning("[voice] polish failed: %r", exc)
        return text
    # A model that decided to answer instead of correct is discarded: length
    # is the cheap, reliable tell.
    if not out or len(out) > max(80, int(len(text) * 1.6)):
        return text
    return out.strip().strip('"')


# ---------------------------------------------------------------------------
# The pipeline
# ---------------------------------------------------------------------------

async def transcribe(raw_audio: bytes, lex: Lexicon, *, language: str = "unknown",
                     want_polish: bool = False, get_client=None,
                     polish_model: str = "") -> dict:
    """Recording → prompt text, with per-stage timings for the caller to log."""
    t0 = time.monotonic()
    pcm, sr = decode_audio(raw_audio)
    seconds = len(pcm) / 2.0 / max(sr, 1)
    if seconds > MAX_AUDIO_SECONDS:
        raise ValueError(f"recording is {seconds:.0f}s — the limit is "
                         f"{MAX_AUDIO_SECONDS:.0f}s")
    if seconds < 0.2:
        return {"text": "", "raw_text": "", "corrections": [], "language": "",
                "romanized": False, "polished": False,
                "seconds": round(seconds, 2), "empty": True,
                "ms": {"total": round((time.monotonic() - t0) * 1000)}}
    t_decode = time.monotonic()

    from backend.app.voice.stt.sarvam import SarvamSTT
    stt = SarvamSTT()
    result = await stt.transcribe(pcm, sample_rate=sr, language=language)
    raw_text = (result.text or "").strip()
    t_stt = time.monotonic()

    # Reuses the connection the transcription just opened to the same host.
    roman = await romanize(raw_text, result.detected_language or "",
                           client=stt._get_client())
    romanized = roman != raw_text
    t_roman = time.monotonic()

    repaired, fixes = repair_vocabulary(roman, lex)
    text = tidy(repaired)
    t_fix = time.monotonic()

    polished = False
    if want_polish and text and get_client is not None and polish_model:
        after = await polish(text, get_client, polish_model)
        polished = after != text
        text = after
    t_end = time.monotonic()

    return {
        "text": text,
        # What the recognizer actually returned, in its own script. The UI
        # offers it back as a one-click swap, because someone dictating the
        # literal Hindi text of an agent's greeting wants देवनागरी, not
        # Hinglish — and only they know which of the two they are doing.
        "raw_text": raw_text,
        "romanized": romanized,
        "corrections": fixes,
        "language": result.detected_language or "",
        "seconds": round(seconds, 2),
        "polished": polished,
        "empty": not text,
        "ms": {
            "decode": round((t_decode - t0) * 1000),
            "stt": round((t_stt - t_decode) * 1000),
            "romanize": round((t_roman - t_stt) * 1000),
            "repair": round((t_fix - t_roman) * 1000),
            "polish": round((t_end - t_fix) * 1000),
            "total": round((t_end - t0) * 1000),
        },
    }
