"""Numbers spoken in ENGLISH inside a Hindi / Gujarati line (RUNBOOK §82).

Bulbul reads the digits of a Hindi line in Hindi — and not reliably:
"₹50-55 लाख" came out as "paanch paanch lakh" (digit by digit), the
decimal point of "₹2.15 Cr" got a Hindi word, and the hyphen of the range
was voiced. The Pramukh owner wants every figure said the way an Indian
sales adviser says it on the phone — "fifty to fifty five lakh", "two point
one five crore" — with the rest of the line untouched (2026-10-15).

`english_numbers()` rewrites ONLY what is written in digits: integers,
decimals, ranges, percentages, ordinals, clock times and phone-shaped digit
runs. Number WORDS already in the line ("दो मिनट", "डेढ़ करोड़") are left
alone, so the language of the line never changes. It runs on the text handed
to TTS, never on the transcript — the record keeps the figures as written.

Opt-in per agent: `languages.tts_numbers_english: true`. Every other agent
is byte-identical (the pipeline skips the call).

Decisions worth keeping:
  * Indian grouping: 3,50,000 → "three lakh fifty thousand", never "three
    hundred fifty thousand" — that is how the figure is said on this phone.
  * A fraction is spoken digit by digit ("two point one five"), trailing
    zeros dropped ("3.70" → "three point seven"): the way a price is read.
  * "₹" is dropped when a lakh/crore unit follows the figure (the unit says
    it is money); a bare "₹35,000" becomes "thirty five thousand rupees".
    "Cr" after a figure is "crore" — the abbreviation is a spelling, not a
    word anyone says.
  * A run of seven or more digits (phone numbers) is spelled digit by digit.
    Phone numbers are never read back by design, but a contact number in an
    identity line may be — "eighty seven thousand…" is not a phone number.
  * Never raises: any failure returns the text unchanged.
"""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven",
         "eight", "nine", "ten", "eleven", "twelve", "thirteen", "fourteen",
         "fifteen", "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy",
         "eighty", "ninety"]
_ORDINAL_IRREGULAR = {
    "one": "first", "two": "second", "three": "third", "five": "fifth",
    "eight": "eighth", "nine": "ninth", "twelve": "twelfth",
}

# Digits in Indic scripts → ASCII, so one set of rules reads every line.
_INDIC_DIGITS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯", "01234567890123456789")

_NUM = r"\d+(?:\.\d+)?"
# Thousands separators in either grouping: 10,000 / 3,890 / 5,00,000.
_GROUPED_RE = re.compile(r"(?<![\d.])\d{1,3}(?:,\d{2,3})+(?![\d,])")
# Seven or more digits, optionally space-grouped, optionally "+" — a phone.
_PHONE_RE = re.compile(r"(?<![\d.,₹])\+?\d(?: ?\d){6,}(?![\d.,])")
# "50-55", "2.15–2.35", "50 to 55" is already words.
_RANGE_RE = re.compile(rf"(?<![\d.])({_NUM})\s*[-–—]\s*({_NUM})(?![\d.])")
_PERCENT_RE = re.compile(rf"(?<![\d.])({_NUM})\s*%")
_ORDINAL_RE = re.compile(r"(?<![\d.])(\d+)(?:st|nd|rd|th)(?![0-9a-z])",
                         re.IGNORECASE)
_TIME_RE = re.compile(r"(?<![\d.:])(\d{1,2}):(\d{2})(?![\d:])")
# "₹2.15 crore", "₹ 50 लाख", "₹50-55 लाख", "₹35,000" (commas already
# removed, "Cr" already "crore"): the sign, a figure or a range, a unit.
_MONEY_UNIT = r"lakhs?|lacs?|crores?|लाख|करोड़|करोड|લાખ|કરોડ"
_RUPEE_RE = re.compile(
    rf"(?:₹|Rs\.?|रु\.?)\s*({_NUM}(?:\s*[-–—]\s*{_NUM})?)"
    rf"(\s*(?:{_MONEY_UNIT})(?![0-9a-zऀ-ॣ઀-૿]))?", re.IGNORECASE)
# "2.15 Cr" / "2 cr." — the abbreviation after a figure (while the figure
# is still digits: it runs before any figure becomes words).
_CR_RE = re.compile(r"(?<=\d)\s*cr\.?(?![0-9a-z])", re.IGNORECASE)
# An ISO date is never spoken as written; leave it to Bulbul as before.
_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
# "4-BHK", "3-bedroom": a hyphen gluing a figure to a word is a space.
_NUM_HYPHEN_WORD_RE = re.compile(r"(?<=\d)-(?=[A-Za-zऀ-ॣ઀-૿])")
_PLAIN_RE = re.compile(rf"(?<![\d.])({_NUM})(?![\d.])")
# A year is read in pairs ("twenty twenty six") only when it stands alone:
# not after a currency sign, not before a unit or a money word.
_YEAR_UNIT_AFTER_RE = re.compile(
    r"^\s*(?:sq|square|ft|feet|rupees?|lakhs?|lacs?|crores?|cr\b|लाख|करोड़|"
    r"करोड|રૂ|લાખ|કરોડ|%|bhk|units?|km|kg|rs\b|₹)", re.IGNORECASE)


def _below_hundred(n: int) -> str:
    if n < 20:
        return _ONES[n]
    tens, ones = divmod(n, 10)
    return _TENS[tens] + (f" {_ONES[ones]}" if ones else "")


def _below_thousand(n: int) -> str:
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(f"{_ONES[hundreds]} hundred")
    if rest:
        parts.append(_below_hundred(rest))
    return " ".join(parts)


def int_words(n: int) -> str:
    """0 ≤ n < 10^16 in Indian grouping (crore / lakh / thousand)."""
    if n < 0:
        return "minus " + int_words(-n)
    if n == 0:
        return "zero"
    parts: list[str] = []
    crore, n = divmod(n, 10_000_000)
    if crore:
        parts.append(f"{int_words(crore)} crore")
    lakh, n = divmod(n, 100_000)
    if lakh:
        parts.append(f"{_below_hundred(lakh)} lakh")
    thousand, n = divmod(n, 1_000)
    if thousand:
        parts.append(f"{_below_hundred(thousand)} thousand")
    if n:
        parts.append(_below_thousand(n))
    return " ".join(parts)


def year_words(n: int) -> str:
    """1100–2099 the way a year is said: "nineteen ninety five", "two
    thousand nine", "twenty twenty six"."""
    if 2000 <= n <= 2009:
        return int_words(n)
    hi, lo = divmod(n, 100)
    if lo == 0:
        return f"{_below_hundred(hi)} hundred"
    if lo < 10:
        return f"{_below_hundred(hi)} oh {_ONES[lo]}"
    return f"{_below_hundred(hi)} {_below_hundred(lo)}"


def ordinal_words(n: int) -> str:
    words = int_words(n)
    head, _, last = words.rpartition(" ")
    if last in _ORDINAL_IRREGULAR:
        last = _ORDINAL_IRREGULAR[last]
    elif last.endswith("y"):
        last = last[:-1] + "ieth"
    else:
        last = last + "th"
    return f"{head} {last}".strip()


def number_words(token: str, *, year_ok: bool = False) -> str:
    """One figure as written ("55", "2.15", "3.70") → English words."""
    whole, _, frac = token.partition(".")
    n = int(whole)
    if year_ok and not frac and len(whole) == 4 and 1100 <= n <= 2099:
        out = year_words(n)
    else:
        out = int_words(n)
    frac = frac.rstrip("0")
    if frac:
        out += " point " + " ".join(_ONES[int(ch)] for ch in frac)
    return out


def _digits_spoken(s: str) -> str:
    return " ".join(_ONES[int(ch)] for ch in s if ch.isdigit())


def _figure_words(s: str) -> str:
    """A figure or a figure range as written ("55", "50-55", "2.15–2.35")."""
    m = _RANGE_RE.fullmatch(s.strip())
    if m:
        return f"{number_words(m.group(1))} to {number_words(m.group(2))}"
    return number_words(s.strip())


def _convert(text: str) -> str:
    if _ISO_DATE_RE.search(text):
        return text
    t = text.translate(_INDIC_DIGITS)
    t = _GROUPED_RE.sub(lambda m: m.group(0).replace(",", ""), t)
    t = _PHONE_RE.sub(lambda m: _digits_spoken(m.group(0)), t)
    t = _CR_RE.sub(" crore", t)
    t = _TIME_RE.sub(_time, t)
    t = _PERCENT_RE.sub(lambda m: f"{number_words(m.group(1))} percent", t)
    t = _ORDINAL_RE.sub(lambda m: ordinal_words(int(m.group(1))), t)
    t = _RUPEE_RE.sub(_rupee, t)
    t = _RANGE_RE.sub(lambda m: _figure_words(m.group(0)), t)
    t = _NUM_HYPHEN_WORD_RE.sub(" ", t)
    t = _PLAIN_RE.sub(_plain, t)
    return re.sub(r"[ \t]{2,}", " ", t)


def _time(m: re.Match) -> str:
    hour, minute = int(m.group(1)), int(m.group(2))
    if hour > 24 or minute > 59:
        return m.group(0)
    h = _below_hundred(hour)
    if minute == 0:
        return h
    if minute < 10:
        return f"{h} oh {_ONES[minute]}"
    return f"{h} {_below_hundred(minute)}"


def _rupee(m: re.Match) -> str:
    figure = _figure_words(m.group(1))
    unit = m.group(2)
    if unit:
        # The unit says it is money; the sign would only be voiced oddly.
        return f"{figure}{unit}"
    return f"{figure} rupees"


def _plain(m: re.Match) -> str:
    tok = m.group(1)
    tail = m.string[m.end():]
    year_ok = (not _YEAR_UNIT_AFTER_RE.match(tail)
               and not m.string[max(0, m.start() - 2):m.start()].strip()
               .endswith(("₹", "Rs", "rs")))
    return number_words(tok, year_ok=year_ok)


def english_numbers(text: str) -> str:
    """The line with every figure written in digits spoken in English.
    Never raises — a failure returns the text unchanged."""
    if not text or not any(ch.isdigit() for ch in text.translate(_INDIC_DIGITS)):
        return text
    try:
        return _convert(text)
    except Exception:
        logger.exception("[TTS:NUMBERS] conversion failed — speaking the "
                         "line as written: %r", text[:80])
        return text
