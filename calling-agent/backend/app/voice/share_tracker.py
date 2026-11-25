"""WhatsApp sharing tracker: which project is under discussion, which ones the
caller agreed to receive on WhatsApp, and which number to send to (RUNBOOK §72).

Opt-in: `follow_up.whatsapp_promise.track_projects: true` on an agent whose
knowledge_base has a catalogue. Nothing here runs for any other agent.

Built from call 1052 (2026-10-10, "Ravi test" / Pramukh Vedanta). The budget
check switched the pitch to Silver Stone; the caller then asked about Green
County, Vivan, Blue Stone, Aristo and Swarit in turn, said yes to receiving
each one's details, and said three times that his WhatsApp is on a different
number. The agent promised "Silver Stone's location, floor plan and cost sheet
to this number" eight times, answered "its address" with Silver Stone's while
the caller meant Green County, asked "is this number on WhatsApp?" on eleven
consecutive turns, and booked the visit for Silver Stone.

Deterministic, like pitch_switch: the catalogue names in each side's line
decide the TOPIC; the agent's own offer plus the caller's yes decide what is
AGREED; explicit "different number" phrases decide the NUMBER. The model only
ever sees the result, in one per-turn band. Decisions worth keeping:
  * A question about a project is a topic change, never a pitch change —
    that stays pitch_switch's job. The band says both names so "इसका" means
    the topic and the invitation still means the pitch.
  * A correction ("Silver Stone नहीं, Blue Stone") contains a negation and a
    send verb; the LAST project named wins and the send verb is a request —
    pitch_switch.classify_answer alone would read it as a refusal.
  * The agent's "is this number on WhatsApp?" is counted from its OWN line
    (not the directive), so the band can say "you already asked" whatever
    the slot machinery stored — call 1052 stored nothing, because every
    "हाँ जी" came in a turn that also asked a question (§61 rule).
  * Another number is only ever captured AFTER the caller says WhatsApp is on
    a different number (or the agent asked for it): a number read out for a
    callback is not a WhatsApp number.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from . import pitch_switch as _ps

logger = logging.getLogger(__name__)

NUMBER_UNKNOWN = "unknown"
NUMBER_SAME = "same"
NUMBER_DIFFERENT = "different"
NUMBER_CAPTURED = "captured"

# After this many unanswered "is this number on WhatsApp?" asks the band
# tells the agent to stop asking; after this many unanswered asks for the
# other number it tells the agent to leave it to the team.
MAX_NUMBER_ASKS = 2
MAX_ALT_ASKS = 2

_INDIC = "ऀ-ॣ०-ॿ઀-૿"
_EDGE_L = rf"(?<![a-z0-9{_INDIC}])"
_EDGE_R = rf"(?![a-z0-9{_INDIC}])"

_WA_RE = re.compile(
    r"whats\s*app|व्हाट्सएप|व्हाट्सऐप|व्हाट्सप|वॉट्सऐप|वॉट्सएप|वाट्सअप|वाट्सएप|"
    r"વોટ્સએપ|વોટ્સઍપ|વ્હોટ્સએપ|વ્હોટ્સઍપ", re.IGNORECASE)
_NUMBER_RE = re.compile(r"number|नंबर|नम्बर|नंबर|નંબર", re.IGNORECASE)
_SEND_RE = re.compile(
    _EDGE_L + r"(?:भेज\S*|bhej\S*|send\S*|share\S*|forward\S*|मोकल\S*|મોકલ\S*)",
    re.IGNORECASE)
_NO_SEND_RE = re.compile(
    r"(?:मत|नहीं|नही|na|mat|nahi|nahin|don'?t|do not|never)\s+(?:भेज|bhej|send|share)|"
    r"(?:भेज\S*|bhej\S*|send\S*)\s+(?:मत|नहीं|नही|na|mat|nahi|nahin)|"
    r"नहीं चाहिए|nahi chahiye|not needed|no need|नथी જોઈતું", re.IGNORECASE)
# "my WhatsApp is on another number" / "not this number" / "this number
# has no WhatsApp". Only honoured when the sentence also talks about a
# NUMBER (or WhatsApp itself) — "doosre project ka bhejo" is not a number.
_DIFF_RE = re.compile(
    _EDGE_L + r"(?:alag|doosra|dusra|doosre|dusre|another|other|different|"
    r"अलग|दूसरा|दुसरा|दूसरे|दुसरे|અલગ|બીજો|બીજા|બીજું)" + _EDGE_R
    + r"|not (?:on )?this|ye(?:h)?\s*wala\s*nah?in?|is\s*p(?:ar|e)\s*nah?in?|"
    r"यह वाला नहीं|ये वाला नहीं|इस ?(?:पर|पे) नहीं|इसी ?(?:पर|पे) नहीं|"
    r"(?:whats\s*app|व्हाट्सएप|व्हाट्सऐप)\s*(?:nahi|nahin|नहीं|नही)\s*(?:hai|है)|"
    r"આ નંબર પર નથી|આ નથી", re.IGNORECASE)
_SAME_RE = re.compile(
    r"same number|this number|yahi number|yehi number|isi number|"
    r"यही नंबर|इसी नंबर|इसी पर|यही है|આ જ નંબર|આ નંબર પર", re.IGNORECASE)
# An agent line asking the caller to SAY a number (vs. a yes/no about it).
_ASK_DIGITS_RE = re.compile(
    r"कौन[\s-]*सा|कौनसा|konsa|kaunsa|which number|what number|what'?s the number|"
    r"बता\S*|बताइ\S*|बोल\S*|लिखवा\S*|likhwa\S*|bata\S*|tell me|give me|"
    r"share (?:the|your)|दीजिए|dijiye|कह\S*|જણાવ\S*|કહો", re.IGNORECASE)
# A reply that OPENS with a yes answers the offer even when a new question
# follows ("हाँ जी हाँ जी, और nearby क्या-क्या है?").
_OPENS_YES_RE = re.compile(
    r"^(?:haan|han|haa|ha|ji|jee|yes|yeah|yep|ok|okay|sure|bilkul|zaroor|"
    r"theek|thik|हाँ|हां|हा|जी|ठीक|बिल्कुल|बिलकुल|ज़रूर|जरूर|ओके|હા|હાં|જી|ઠીક|"
    r"ચોક્કસ)(?![a-z])", re.IGNORECASE)
_QUESTION_RE = re.compile(
    r"\?|" + _EDGE_L + r"(?:क्या|kya|shall i|should i|can i|may i|दूँ|du|doon|"
    r"करूँ|karu|karoon|चाहेंगे|chahenge|ठीक रहेगा|theek rahega)" + _EDGE_R,
    re.IGNORECASE)

_DIGITS_TRANS = str.maketrans("०१२३४५६७८९૦૧૨૩૪૫૬૭૮૯", "01234567890123456789")
_DIGIT_WORDS = {
    "0": ("zero", "शून्य", "सिफ़र", "सिफर", "शुन्य", "શૂન્ય", "oh"),
    "1": ("one", "ek", "एक", "એક"),
    "2": ("two", "do", "दो", "બે", "be"),
    "3": ("three", "teen", "tin", "तीन", "ત્રણ", "tran"),
    "4": ("four", "char", "chaar", "चार", "ચાર"),
    "5": ("five", "paanch", "panch", "pach", "पांच", "पाँच", "પાંચ"),
    "6": ("six", "chhe", "che", "chha", "छह", "छः", "छे", "છ"),
    "7": ("seven", "saat", "sat", "सात", "સાત"),
    "8": ("eight", "aath", "ath", "आठ", "આઠ"),
    "9": ("nine", "nau", "no", "नौ", "નવ", "nav"),
}
_WORD_TO_DIGIT = {w: d for d, ws in _DIGIT_WORDS.items() for w in ws}
_DOUBLE_RE = re.compile(_EDGE_L + r"(?:double|डबल|ડબલ)\s+(\d)" + _EDGE_R, re.IGNORECASE)
_TRIPLE_RE = re.compile(_EDGE_L + r"(?:triple|ट्रिपल|ટ્રિપલ)\s+(\d)" + _EDGE_R, re.IGNORECASE)
_PHONE_RE = re.compile(
    r"(?<!\d)(?:(?:\+?91|0)[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")


def _spoken_digits(text: str) -> str:
    """Devanagari/Gujarati numerals and spoken digit words → ASCII digits
    (sequences only; a lone "do" stays a word)."""
    s = str(text or "").translate(_DIGITS_TRANS)
    tokens = re.split(r"(\s+|[,.\-])", s)
    out: list[str] = []
    run: list[str] = []
    for tok in tokens:
        key = tok.lower().strip("'’")
        if key in _WORD_TO_DIGIT:
            run.append(_WORD_TO_DIGIT[key])
            continue
        if tok.strip() == "" or tok in (",", ".", "-"):
            if run:
                out.append(" ".join(run)); run = []
            out.append(tok)
            continue
        if run:
            if len(run) >= 4:
                out.append(" ".join(run))
            else:
                out.append(" ".join(w for w in run))
            run = []
        out.append(tok)
    if run:
        out.append(" ".join(run))
    s = "".join(out)
    s = _DOUBLE_RE.sub(lambda m: f"{m.group(1)} {m.group(1)}", s)
    s = _TRIPLE_RE.sub(lambda m: f"{m.group(1)} {m.group(1)} {m.group(1)}", s)
    return s


def phone_in(text: str) -> str:
    """A ten-digit Indian mobile number in `text` (digits, numerals or spoken
    digit words), or ''."""
    s = _spoken_digits(text)
    for m in _PHONE_RE.finditer(s):
        raw = re.sub(r"[\s.-]", "", m.group(1))
        if len(raw) == 10 and raw[0] in "6789":
            return raw
    return ""


@dataclass
class ShareState:
    topic_id: Optional[str] = None
    agreed: list[str] = field(default_factory=list)
    declined: set[str] = field(default_factory=set)
    offered_id: Optional[str] = None        # the agent's LAST line offered to send this
    number: str = NUMBER_UNKNOWN
    number_asked: int = 0                   # "is this number on WhatsApp?" count
    number_question_open: bool = False      # the agent's LAST line asked it
    alt_number: str = ""                    # digits of the other number, when given
    alt_number_asked: int = 0
    alt_question_open: bool = False
    caller_turns: int = 0
    last_agent_text: str = ""
    promised_lines: int = 0                 # "I'll send it" statements so far
    just_agreed: bool = False               # the caller's LAST turn said yes to it


def enabled(agent_def) -> bool:
    wp = getattr(getattr(agent_def, "follow_up", None), "whatsapp_promise", None) or {}
    kb = getattr(agent_def, "knowledge_base", None)
    has_catalog = any(isinstance(i, dict) and i.get("name")
                      for i in (getattr(kb, "catalog", None) or []))
    return bool(isinstance(wp, dict) and wp.get("track_projects") and has_catalog)


def _items(kb) -> list[dict]:
    return [i for i in (getattr(kb, "catalog", None) or [])
            if isinstance(i, dict) and i.get("id") and i.get("name")]


def _iid(item: dict) -> str:
    return str(item.get("id") or "").strip().lower()


def _spellings(kb, item: dict) -> list[str]:
    """pitch_switch.item_keys plus every STT spelling the agent's own
    correction table maps onto this item ("ग्रीन काउंटी" → Green County):
    the post-processor rewrites most of them before the router sees the
    text, but a merged fragment or a line from the audit transcript may
    still carry the native-script form."""
    keys = set(_ps.item_keys(item))
    try:
        corr = getattr(getattr(kb, "stt_corrections", None),
                       "project_name_corrections", None) or {}
    except Exception:
        corr = {}
    for wrong, right in (corr.items() if isinstance(corr, dict) else []):
        r = str(right or "").strip().lower()
        w = str(wrong or "").strip().lower()
        if len(w) >= 3 and (r in keys or f"pramukh {r}" in keys):
            keys.add(w)
    return sorted(keys, key=len, reverse=True)


def named_in_order(kb, text: str) -> list[str]:
    """Catalogue ids named in `text`, in order of their LAST mention, so a
    correction ("Silver Stone नहीं, Blue Stone") ends with the corrected one."""
    low = str(text or "").lower()
    found: list[tuple[int, str]] = []
    for item in _items(kb):
        last = -1
        for k in _spellings(kb, item):
            for m in re.finditer(re.escape(k), low):
                before = low[m.start() - 1] if m.start() > 0 else " "
                after = low[m.end()] if m.end() < len(low) else " "
                if (not (before.isalnum() or re.match(f"[{_INDIC}]", before))
                        and not (after.isalnum() or re.match(f"[{_INDIC}]", after))):
                    last = max(last, m.start())
        if last >= 0:
            found.append((last, _iid(item)))
    found.sort()
    return [i for _, i in found]


def name_of(kb, iid: Optional[str]) -> str:
    item = _ps.item_by_id(kb, iid or "")
    return str((item or {}).get("name") or iid or "")


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.?!।])\s+|\n", str(text or "")) if s.strip()]


def observe_agent(kb, st: ShareState, text: str) -> None:
    """The agent spoke. Record its topic, its share offer and its number
    question — from the line itself, never from the directive."""
    if not text or text == st.last_agent_text:
        return
    st.last_agent_text = text
    names = named_in_order(kb, text)
    if len(names) == 1:
        st.topic_id = names[0]
    st.offered_id = None
    st.number_question_open = False
    st.alt_question_open = False
    for sent in _sentences(text):
        wa, nums = bool(_WA_RE.search(sent)), bool(_NUMBER_RE.search(sent))
        send = bool(_SEND_RE.search(sent))
        if not (wa or send):
            continue
        if not _QUESTION_RE.search(sent):
            if send:
                st.promised_lines += 1   # a statement: "मैं … भेज देता हूँ"
            continue
        if wa and nums and _ASK_DIGITS_RE.search(sent) and st.number in (
                NUMBER_DIFFERENT, NUMBER_CAPTURED):
            st.alt_question_open = True
            st.alt_number_asked += 1
            continue
        if wa and nums and not send:
            # "क्या यह नंबर WhatsApp पर है?"
            st.number_question_open = True
            st.number_asked += 1
            continue
        if wa and nums and send and not _ASK_DIGITS_RE.search(sent):
            # "क्या यह नंबर WhatsApp पर है जिस पर मैं … भेज सकूँ?" — both.
            st.number_question_open = True
            st.number_asked += 1
        in_sent = named_in_order(kb, sent)
        st.offered_id = in_sent[-1] if in_sent else st.topic_id
    if st.offered_id or st.number_question_open or st.alt_question_open:
        logger.info("[SHARE] agent offered=%s number_q=%s alt_q=%s topic=%s",
                    st.offered_id, st.number_question_open, st.alt_question_open,
                    st.topic_id)


def observe_caller(kb, st: ShareState, text: str) -> dict[str, Any]:
    """The caller spoke. Returns the state changes worth recording:
    {'agreed': [id…] (new), 'declined': id, 'number': status, 'alt_number': digits}."""
    st.caller_turns += 1
    st.just_agreed = False
    out: dict[str, Any] = {}
    if not text:
        st.offered_id = None
        st.number_question_open = False
        st.alt_question_open = False
        return out
    names = named_in_order(kb, text)
    prior_topic = st.topic_id      # "वो भी भेज देना. और Aristo की…" means the
    #                                project we were on, not the new question

    # ── the number ──
    low = text.lower()
    talks_number = bool(_NUMBER_RE.search(low) or _WA_RE.search(low))
    verdict = _ps.classify_answer(text)
    if _DIFF_RE.search(low) and (talks_number or st.number_question_open):
        if st.number != NUMBER_CAPTURED:
            st.number = NUMBER_DIFFERENT
            out["number"] = NUMBER_DIFFERENT
    elif st.number_question_open:
        if verdict == "no" and not _SAME_RE.search(low):
            st.number = NUMBER_DIFFERENT
            out["number"] = NUMBER_DIFFERENT
        elif _SAME_RE.search(low) or (verdict == "yes" and (
                _OPENS_YES_RE.match(_ps._norm(low))
                or len(_ps._norm(low).split()) <= 3)):
            # "Vivan के बारे में बता दो" contains the yes-word "बता दो" but
            # answers nothing — only a reply that OPENS with a yes (or is a
            # bare one) is the answer to "is this number on WhatsApp?".
            if st.number == NUMBER_UNKNOWN:
                st.number = NUMBER_SAME
                out["number"] = NUMBER_SAME
            st.just_agreed = True
    if st.number in (NUMBER_DIFFERENT, NUMBER_CAPTURED) or st.alt_question_open:
        digits = phone_in(text)
        if digits and digits != st.alt_number:
            st.alt_number = digits
            st.number = NUMBER_CAPTURED
            out["number"] = NUMBER_CAPTURED
            out["alt_number"] = digits

    # ── what they agreed to receive ──
    send_m = _SEND_RE.search(low)
    asks_send = bool(send_m) and not _NO_SEND_RE.search(low)
    target: Optional[str] = None
    if asks_send:
        # "मुझे Green County का भेज दो" names the project BEFORE the verb;
        # "हाँ भेज देना, और Aristo की location बता दो" names one AFTER it —
        # that one is a new question, not what they want sent.
        before = named_in_order(kb, low[:send_m.start()])
        target = before[-1] if before else (st.offered_id or prior_topic)
    elif st.offered_id and verdict == "yes" and not _NO_SEND_RE.search(low) \
            and ("?" not in low or _OPENS_YES_RE.match(_ps._norm(low))):
        target = st.offered_id
    if target:
        st.just_agreed = True
        if target not in st.agreed:
            st.agreed.append(target)
            out["agreed"] = [target]
            logger.info("[SHARE] caller agreed to receive %s (all: %s)",
                        target, st.agreed)
        st.declined.discard(target)
    elif st.offered_id and (verdict == "no" or _NO_SEND_RE.search(low)) \
            and st.offered_id not in st.agreed:
        st.declined.add(st.offered_id)
        out["declined"] = st.offered_id
        logger.info("[SHARE] caller declined %s", st.offered_id)

    if names:
        st.topic_id = names[-1]
    if out.get("number"):
        logger.info("[SHARE] number status → %s%s", st.number,
                    f" (ends {st.alt_number[-4:]})" if st.alt_number else "")
    st.offered_id = None
    st.number_question_open = False
    st.alt_question_open = False
    return out


def band(kb, st: ShareState, pitch_id: Optional[str]) -> list[str]:
    """The per-turn ━━ WHATSAPP ━━ lines (never empty for an enabled agent —
    the number rule applies from the first turn)."""
    pitch = (pitch_id or "").strip().lower() or None
    topic = st.topic_id or pitch
    pn, tn = name_of(kb, pitch), name_of(kb, topic)
    lines = ["", "━━ WHATSAPP — what you share, and where ━━"]
    if topic and pitch and topic != pitch:
        lines.append(
            f"▸ Project under discussion RIGHT NOW: {tn} (the caller brought it "
            f"up last). \"इसका\", \"its\", \"this one\" in your reply means {tn} "
            f"— answer about {tn}, with {tn}'s own facts. {pn} is still the "
            f"project you pitch and invite them to see; do not swap the two.")
    elif tn:
        lines.append(f"▸ Project under discussion right now: {tn} — the project "
                     "you pitch.")
    if st.agreed:
        agreed = ", ".join(name_of(kb, i) for i in st.agreed)
        lines.append(
            f"▸ They have said YES to receiving on WhatsApp: {agreed}. When you "
            "say what you will send, name exactly these — never a project they "
            "did not ask for, and never only the one you pitch. Do not offer "
            "again what is already on this list.")
    else:
        lines.append(
            "▸ They have not agreed to receive anything on WhatsApp yet. Offer "
            "a project's details at most ONCE, for the project under "
            "discussion, when it fits the moment — never as a line tacked on "
            "to every reply.")
    if st.just_agreed:
        # The owner's ask (2026-10-12): "yes, send it" gets "ठीक है", not the
        # promise read back. Call 1074 restated what it would send, and to
        # which number, after every yes and every "ok".
        lines.append(
            "▸ They just said YES to it. Reply with a one- or two-word "
            "acknowledgement and go straight to the next step — do not "
            "restate what you will send or to which number.")
    if st.promised_lines >= 2:
        # Call 1074: "मैं details WhatsApp पर भेज देता हूँ" five times, once
        # per "ठीक है", because nothing told the model the promise was made.
        lines.append(
            f"▸ You have already said {st.promised_lines} times that you will "
            "send it. Never say it again on this call — an 'ok' from them "
            "gets a short reply and the next step (the closing question), "
            "not the promise.")
    if st.declined - set(st.agreed):
        dec = ", ".join(name_of(kb, i) for i in sorted(st.declined - set(st.agreed)))
        lines.append(f"▸ They declined {dec}'s details — do not offer them again.")

    n = st.number
    if n == NUMBER_SAME:
        lines.append(
            "▸ WhatsApp number: THIS number — confirmed by the caller. Never ask "
            "again; say \"इसी नंबर पर\" and move on.")
    elif n == NUMBER_CAPTURED:
        last4 = st.alt_number[-4:]
        lines.append(
            f"▸ WhatsApp number: the caller gave a DIFFERENT number, ending in "
            f"{last4} — recorded. Confirm only the last four digits, once "
            f"(\"…जो {last4} पर ख़त्म होता है\"), then promise to send there. "
            "Never read the whole number back, never say \"this number\".")
    elif n == NUMBER_DIFFERENT:
        if st.alt_number_asked >= MAX_ALT_ASKS:
            lines.append(
                "▸ WhatsApp number: the caller's WhatsApp is on a DIFFERENT "
                "number and you could not catch it. Say the team will confirm "
                "the WhatsApp number when they call, and move on — never "
                "promise to send to \"this number\".")
        else:
            lines.append(
                "▸ WhatsApp number: the caller said their WhatsApp is on a "
                "DIFFERENT number. \"This number\" is WRONG for WhatsApp — never "
                "promise to send to it. Ask them, once, to tell you the WhatsApp "
                "number, then wait for it (the engine records it).")
    elif st.number_asked >= MAX_NUMBER_ASKS:
        lines.append(
            "▸ WhatsApp number: you have asked twice whether this number is on "
            "WhatsApp without a clear answer. Do NOT ask again — send to this "
            "number and let the team confirm.")
    elif st.number_asked == 1:
        lines.append(
            "▸ WhatsApp number: you already asked whether this number is on "
            "WhatsApp and got no clear answer. Do not ask it in this reply; ask "
            "once more, briefly, only when you next confirm what you will send.")
    else:
        lines.append(
            "▸ WhatsApp number: not confirmed yet. The FIRST time you promise to "
            "send something, ask in the same breath whether this number is on "
            "WhatsApp — once. Never ask them to dictate a number unless they say "
            "WhatsApp is on another number.")
    if topic and pitch and topic != pitch:
        lines.append(
            f"▸ Site visit: a visit is booked for the project THEY name. If "
            f"they fix a day or time without naming one, ask in a few words "
            f"whether they mean {tn} or {pn} — once — before booking.")
    return lines
