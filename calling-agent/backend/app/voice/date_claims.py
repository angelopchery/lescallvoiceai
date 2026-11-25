"""Does what the caller just said about the CALENDAR hold up?

Call 1043 (2026-10-09, a Friday): the agent offered "संडे यानी 11 अक्टूबर" —
correct — and the caller answered "संडे को तो बारह अक्टूबर है". The model
gave way, told the caller "11 अक्टूबर आज है", and booked Monday the 12th as
"Sunday". The prompt's ━━ RIGHT NOW ━━ band listed "Sun 11 Oct" the whole
time; a confident caller simply outranks a static fact.

So the check is done in code, per turn: a weekday and a date said together
that do not match, or a "today is …" that is not today, puts the real answer
in the turn band (prompt_blocks.date_claim_block) — next to the goal, where a
per-turn fact is obeyed (RUNBOOK §55.5). Pure; never raises into a call.

Only explicit dates count: a day number must sit next to a month word or
"तारीख/date", or carry an ordinal ("12th") — "दो बजे" is a time, not a date.
Tokens are split on script-aware boundaries; `\\b` and `\\w` both break on
Indic vowel signs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

_WEEKDAYS_EN = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                "Saturday", "Sunday"]
_MONTHS_EN = ["January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December"]

_WEEKDAY_WORDS: dict[str, int] = {}
for _i, _words in enumerate([
    ("monday", "mon", "somvar", "somwar", "सोमवार", "मंडे", "मन्डे", "સોમવાર", "મન્ડે"),
    ("tuesday", "tue", "tues", "mangalvar", "mangalwar", "मंगलवार", "ट्यूज़डे",
     "ट्यूजडे", "ट्यूसडे", "મંગળવાર"),
    ("wednesday", "wed", "budhvar", "budhwar", "बुधवार", "वेडनेसडे", "वेनसडे",
     "બુધવાર"),
    ("thursday", "thu", "thurs", "guruvar", "guruwar", "गुरुवार", "गुरूवार",
     "बृहस्पतिवार", "थर्सडे", "ગુરુવાર"),
    ("friday", "fri", "shukravar", "shukrawar", "शुक्रवार", "फ्राइडे", "फ्राईडे",
     "શુક્રવાર"),
    ("saturday", "sat", "shanivar", "shaniwar", "शनिवार", "सैटरडे", "सेटरडे",
     "શનિવાર"),
    ("sunday", "sun", "ravivar", "raviwar", "रविवार", "संडे", "सन्डे", "રવિવાર",
     "સન્ડે"),
]):
    for _w in _words:
        _WEEKDAY_WORDS[_w] = _i
# "sun"/"sat"/"wed"/"mon" are also English words; only full forms count when
# the turn is Latin text — the abbreviations stay out.
for _w in ("mon", "tue", "tues", "wed", "thu", "thurs", "fri", "sat", "sun"):
    _WEEKDAY_WORDS.pop(_w, None)

_MONTH_WORDS: dict[str, int] = {}
for _i, _words in enumerate([
    ("january", "jan", "जनवरी", "જાન્યુઆરી"),
    ("february", "feb", "फरवरी", "फ़रवरी", "ફેબ્રુઆરી"),
    ("march", "मार्च", "માર્ચ"),
    ("april", "अप्रैल", "એપ્રિલ"),
    ("may", "मई", "મે"),
    ("june", "जून", "જૂન"),
    ("july", "जुलाई", "જુલાઈ"),
    ("august", "aug", "अगस्त", "ઓગસ્ટ"),
    ("september", "sept", "sep", "सितंबर", "सितम्बर", "सेप्टेंबर", "સપ્ટેમ્બર"),
    ("october", "oct", "अक्टूबर", "अक्तूबर", "ऑक्टोबर", "ओक्टोबर", "अक्टोबर",
     "ઓક્ટોબર", "ઑક્ટોબર"),
    ("november", "nov", "नवंबर", "नवम्बर", "નવેમ્બર"),
    ("december", "dec", "दिसंबर", "दिसम्बर", "ડિસેમ્બર"),
], start=1):
    for _w in _words:
        _MONTH_WORDS[_w] = _i

_DATE_WORDS = frozenset({"तारीख", "तारीख़", "tarikh", "tareekh", "tarik",
                         "date", "डेट", "તારીખ"})
_TODAY_WORDS = frozenset({"today", "aaj", "आज", "આજે", "આજ"})
# Between "today" and the day it is said to be, any of these means the
# sentence is NOT "today is X": "आज नहीं, संडे को" / "aaj nahi kal".
_BREAK_WORDS = frozenset({
    "नहीं", "नही", "ना", "न", "no", "not", "nahi", "nahin", "nai", "નહીં",
    "નહિ", "ના", "कल", "kal", "tomorrow", "परसों", "parso", "parson", "કાલે",
    "but", "लेकिन", "पर", "बल्कि", "or", "या", "या", "after", "बाद"})
_TODAY_WINDOW = 4

_HINDI_NUMBERS = {
    "एक": 1, "दो": 2, "तीन": 3, "चार": 4, "पांच": 5, "पाँच": 5, "छह": 6,
    "छः": 6, "छे": 6, "सात": 7, "आठ": 8, "नौ": 9, "दस": 10, "ग्यारह": 11,
    "बारह": 12, "तेरह": 13, "चौदह": 14, "पंद्रह": 15, "पन्द्रह": 15,
    "सोलह": 16, "सत्रह": 17, "अठारह": 18, "उन्नीस": 19, "बीस": 20,
    "इक्कीस": 21, "बाईस": 22, "बाइस": 22, "तेईस": 23, "तेइस": 23,
    "चौबीस": 24, "पच्चीस": 25, "छब्बीस": 26, "सत्ताईस": 27, "सत्ताइस": 27,
    "अट्ठाईस": 28, "अट्ठाइस": 28, "उनतीस": 29, "तीस": 30, "इकतीस": 31,
}

_TOKEN_RE = re.compile(r"[0-9A-Za-zऀ-ॿ઀-૿'’]+")
_SENTENCE_SPLIT_RE = re.compile(r"[.?!।\n]+")
_ORDINAL_RE = re.compile(r"^(\d{1,2})(st|nd|rd|th|वीं|वी|वां|मी)?$", re.I)


def _digits(tok: str) -> str:
    """Devanagari/Gujarati digits → ASCII."""
    out = []
    for ch in tok:
        if "०" <= ch <= "९":
            out.append(chr(ord(ch) - 0x0966 + 48))
        elif "૦" <= ch <= "૯":
            out.append(chr(ord(ch) - 0x0AE6 + 48))
        else:
            out.append(ch)
    return "".join(out)


def _day_number(tok: str) -> tuple[int | None, bool]:
    """(day, had_ordinal) for a token that names a day-of-month number."""
    t = _digits(tok).lower()
    m = _ORDINAL_RE.match(t)
    if m:
        n = int(m.group(1))
        return (n if 1 <= n <= 31 else None), bool(m.group(2))
    return _HINDI_NUMBERS.get(tok), False


def _resolve(day: int, month: int | None, today: date) -> date | None:
    """The calendar date a caller most plausibly means: this month/year if it
    has not passed by more than a week, else the next one."""
    try:
        if month is None:
            cand = date(today.year, today.month, day)
            if cand < today - timedelta(days=7):
                ny, nm = (today.year + (today.month == 12),
                          today.month % 12 + 1)
                cand = date(ny, nm, day)
            return cand
        cand = date(today.year, month, day)
        if cand < today - timedelta(days=31):
            cand = date(today.year + 1, month, day)
        return cand
    except ValueError:
        return None          # "31 November" — not a date; nothing to check


@dataclass(frozen=True)
class DateIssue:
    kind: str                # "weekday_mismatch" | "today_mismatch"
    said: str                # what the caller paired, in plain English
    truth: str               # the fact that contradicts it


def _fmt(d: date) -> str:
    return f"{_WEEKDAYS_EN[d.weekday()]} {d.day} {_MONTHS_EN[d.month - 1]}"


def _next_weekday(today: date, wd: int) -> date:
    return today + timedelta(days=(wd - today.weekday()) % 7)


def check_date_claims(text: str, now: datetime | date) -> list[DateIssue]:
    """Calendar claims in one caller turn that contradict the real calendar."""
    if not text:
        return []
    today = now.date() if isinstance(now, datetime) else now
    issues: list[DateIssue] = []
    try:
        for sent in _SENTENCE_SPLIT_RE.split(text):
            toks = _TOKEN_RE.findall(sent)
            low = [t.lower() for t in toks]
            weekdays = sorted({_WEEKDAY_WORDS[t] for t in low
                               if t in _WEEKDAY_WORDS})
            dates: list[tuple[int, date]] = []
            for i, tok in enumerate(toks):
                day, ordinal = _day_number(tok)
                if day is None:
                    continue
                nxt = low[i + 1:i + 3]
                prev = low[i - 1] if i else ""
                month = next((_MONTH_WORDS[w] for w in nxt
                              if w in _MONTH_WORDS), None)
                if month is None and prev in _MONTH_WORDS:
                    month = _MONTH_WORDS[prev]          # "October 12"
                explicit = (month is not None
                            or any(w in _DATE_WORDS for w in nxt)
                            or (ordinal and tok.isascii()))
                if not explicit:
                    continue
                d = _resolve(day, month, today)
                if d:
                    dates.append((i, d))

            # "today is X": X follows the today-word closely, unbroken.
            claim_dates: list[date] = []
            claim_wds: list[int] = []
            for ti, t in enumerate(low):
                if t not in _TODAY_WORDS:
                    continue
                window = range(ti + 1, min(len(low), ti + 1 + _TODAY_WINDOW))
                for j in window:
                    if low[j] in _BREAK_WORDS:
                        break
                    if low[j] in _WEEKDAY_WORDS:
                        claim_wds.append(_WEEKDAY_WORDS[low[j]])
                    claim_dates += [d for (k, d) in dates if k == j]
            wrong_date = [d for d in claim_dates if d != today]
            wrong_wd = [w for w in claim_wds if w != today.weekday()]
            if wrong_date or (wrong_wd and not claim_dates):
                said = (f"{wrong_date[0].day} {_MONTHS_EN[wrong_date[0].month - 1]}"
                        if wrong_date else _WEEKDAYS_EN[wrong_wd[0]])
                issues.append(DateIssue(
                    "today_mismatch", f"today is {said}",
                    f"today is {_fmt(today)}"))
                continue
            if weekdays and dates:
                for _, d in dates:
                    if d.weekday() in weekdays:
                        continue
                    wd = weekdays[0]
                    issues.append(DateIssue(
                        "weekday_mismatch",
                        f"{_WEEKDAYS_EN[wd]} is {d.day} {_MONTHS_EN[d.month - 1]}",
                        f"{d.day} {_MONTHS_EN[d.month - 1]} is a "
                        f"{_WEEKDAYS_EN[d.weekday()]}; the coming "
                        f"{_WEEKDAYS_EN[wd]} is {_fmt(_next_weekday(today, wd))}"))
    except Exception:
        return []
    # One line per distinct claim.
    seen, out = set(), []
    for i in issues:
        if (i.kind, i.said) not in seen:
            seen.add((i.kind, i.said))
            out.append(i)
    return out
