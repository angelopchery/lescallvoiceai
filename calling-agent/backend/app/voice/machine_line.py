"""
Machine speech on the caller's side of the line (RUNBOOK §63, §83).

An outbound call is not always answered by a person. On 2026-09-28 (call
919426600929_20260928_094925, Craft Idea / Nisha) an iPhone's Call Screening
answered: "Hi, if you record your name and reason for calling, I'll see if
this person is available." The engine treated that as the customer: it stored
the sentence as the answer to the first question, pinned the call to English
on the screener's voice, replied "You're welcome!" to the screener's "Thanks,
please stay on the line", and when the real person picked up and said
"hello" — twice — it carried on with the NEXT question of its script to
someone who had never heard who was calling.

The corpus (1,189 calls, 11,850 caller lines) holds four kinds of these
lines, all recognisable by wording no human uses on a sales call:

  SCREENER     asks the caller to state name and reason — iOS Call
               Screening, Google Call Screen, Truecaller / Samsung assistants.
               Answer ONCE with identity + purpose, then wait silently for a
               person to pick up.
  HOLD         "the person you are speaking with has put your call on hold,
               please stay on the line" (and the screener's own "please stay
               on the line"). Say nothing; the call is not ours to fill. The
               announcement REPEATS every ~45 s for as long as the hold lasts
               (call 917265832771, 2026-10-02: 8.5 minutes of it, in three
               languages, with the agent pitching into every garbled
               fragment) — so the engine bounds the whole episode, not the
               gap between two announcements.
  VOICEMAIL    "your call has been forwarded to voicemail… at the tone,
               please record your message"; Apple Live Voicemail (which reads
               the same carrier wording); a personal greeting ("I can't take
               your call right now, leave your name and number"). Close the
               call — never talk to a mailbox.
  UNREACHABLE  a network announcement: the number is switched off / not
               reachable / not in service. Close the call.

Pure text functions, no state. The ENGINE decides what a kind means at a
given moment (a VOICEMAIL phrase from someone already in conversation is a
person talking about voicemail, not a mailbox) — see
ConversationRouter._machine_line_gate.

Precision over recall: every pattern is anchored on machine phrasing
("the person you're trying to reach", "reason for calling", "record your
message"), never on words a person says ("not available", "switched off",
"leave a message" alone, "you've reached" — a person DID say "you have
reached the wrong number"). A missed machine line costs today's behaviour; a
false one silences or hangs up on a customer. Re-run the corpus sweep over
every caller line in recordings/ before widening any pattern (the §83 sweep
script lives in the RUNBOOK).
"""

from __future__ import annotations

import re

SCREENER = "screener"
HOLD = "hold"
VOICEMAIL = "voicemail"
UNREACHABLE = "unreachable"

# Kinds that end the call, and kinds that make the agent wait silently.
CLOSING_KINDS = frozenset({VOICEMAIL, UNREACHABLE})
WAITING_KINDS = frozenset({SCREENER, HOLD})

_I = re.IGNORECASE

# Every pattern below is attested in recordings/ or is the published wording
# of the vendor's screener / mailbox. STT variants seen live are folded in
# ("haang up", "forward it to voicemail", "at the tone, please.", "add the
# tone", Devanagari and Gujarati transliterations of the English line).
_SCREENER_RE = re.compile(
    r"reason for (?:your |the )?call"
    r"|record your name"
    r"|see if (?:this|the) person is (?:available|free|around)"
    r"|screening service"
    r"|(?:get|receive) a copy of this conversation"
    r"|go ahead and (?:say|state|tell)"
    r"|(?:say|state|tell (?:me|us)) your name,? and"
    r"|your name,? and (?:the reason|why you(?:'re| are)? calling)"
    r"|truecaller assistant|digital assistant|call assist(?:ant)?\b"
    r"|अपना नाम.{0,40}(?:कारण|वजह)"
    r"|कॉल करने (?:का|की) (?:कारण|वजह)",
    _I)

# The hold announcement, as STT renders it in English, Hindi, Gujarati — and
# as Gujarati-script English ("ધ પર્સન યુ આર સ્પીકિંગ વિથ…"). "the person you
# are speaking with" is the anchor; a person never refers to themselves that
# way. Declarative forms only ("has put your call on hold"): the imperative
# "एक मिनट होल्ड करो" is a person asking us to wait, and stays a person.
_HOLD_RE = re.compile(
    r"(?:put|placed|kept) your call on hold"
    r"|your call (?:is|has been) (?:put |placed )?on hold"
    r"|has your call on hold"
    r"|person you(?:'re| are) speaking with"
    r"|(?:please )?stay on the line"
    r"|(?:is|are) (?:busy )?on another call"
    r"|होल्ड पर (?:रख|है)"
    r"|ला(?:इ|ई)न पर बने रह"
    r"|आप जिस व्यक्ति से बात कर रहे"
    r"|दूसरी कॉल पर व्यस्त"
    r"|કૉલ(?:ને)? હોલ્ડ|કોલ(?:ને)? હોલ્ડ"
    r"|હોલ્ડ પર રાખ|હોલ્ડ કરી (?:છે|દેવા)"
    r"|લા(?:ઈ|ઇ)ન પર (?:રહો|બને રહ)"
    r"|કૃપા કરીને લા(?:ઈ|ઇ)ન"
    r"|વ્યક્તિ સાથે વાત કરી રહ્યા છો|વ્યક્તિ સે વાત કર રહે|વ્યક્તિને કોલ કર્યો છે"
    r"|સ્પીકિંગ વિથ|ઓન હોલ્ડ|સ્ટે ઓન ધ લા(?:ઈ|ઇ)ન",
    _I)

# "the person / number / subscriber you are (trying to) reach / call(ing)"
_TARGET = (r"(?:person|number|subscriber|customer|party|user)"
           r" (?:you|you're|you are|you have|you've)"
           r"(?: (?:trying|want|wish|wanting))?(?: to)?"
           r" (?:reach|reached|call|called|calling|dial(?:l)?ed|dialling|dialing)")

# A personal mailbox greeting speaks in the FIRST person about not taking the
# call ("Hi, you've reached Rahul. I can't take your call right now…"). The
# first person is the anchor: "he can't come to the phone right now" is a
# family member who answered, and stays a person.
_FIRST_PERSON_AWAY = (
    r"\b(?:i|we)(?:'m| am|'re| are)? ?(?:can(?:'t|not)|cannot|unable to"
    r"|not able to|am unable to) (?:take your call|come to the phone"
    r"|get to the phone)")

_VOICEMAIL_RE = re.compile(
    r"voice ?mail"
    r"|record your message"
    r"|(?:at|add|after) the (?:tone|beep)"
    r"|when you (?:have |'ve )?finished recording"
    r"|leave (?:a|your) (?:voice )?message after"
    r"|please leave (?:a|your) (?:voice )?message"
    r"|leave your name(?:,| and)(?: your)?(?: phone| contact)? number"
    r"|" + _FIRST_PERSON_AWAY +
    r"|for more options"
    r"|press (?:one|1|two|2|zero|0|star|\*|hash|#|pound) (?:to|for)\b"
    r"|automated voice messaging"
    r"|mailbox"
    r"|" + _TARGET + r"[^.?!]{0,30}\b(?:is |are )?(?:not available|unavailable)"
    r"|trying to reach,? (?:is )?(?:not available|unavailable)"
    r"|बीप के बाद"
    r"|(?:संदेश|मैसेज) रिकॉर्ड"
    r"|वॉ(?:इ|य)स ?मेल|ट्राइंग टू रीच|फिनिश्ड रिकॉर्डिंग|यू मे हैंग अप"
    r"|रिकॉर्ड योर मैसेज"
    r"|બીપ (?:પછી|બાદ)"
    r"|વૉ(?:ઇ|ઈ|ય)સ ?મેલ|વો(?:ઇ|ઈ|ય)સ ?મેલ",
    _I)

_UNREACHABLE_RE = re.compile(
    _TARGET + r"[^.?!]{0,40}\b(?:switched off|not reachable|unreachable"
    r"|out of (?:the )?(?:coverage|network|service) area|busy|not answering"
    r"|not in service|does not exist|is not valid|is invalid)"
    r"|(?:number|subscriber) (?:is )?(?:temporarily )?(?:switched off|not reachable)"
    r"|(?:number|subscriber) (?:does not exist|is not in service)"
    r"|(?:call )?cannot be completed as dial(?:l)?ed"
    r"|आप जिस (?:नंबर|व्यक्ति|ग्राहक).{0,50}(?:स्विच(?:्ड)? ऑफ|पहुंच से बाहर|पहुँच से बाहर"
    r"|कवरेज क्षेत्र से बाहर|व्यस्त|मौजूद नहीं|उपलब्ध नहीं)"
    r"|તમે જે (?:નંબર|વ્યક્તિ|ગ્રાહક).{0,50}(?:સ્વિચ ઓફ|પહોંચની બહાર|વ્યસ્ત|ઉપલબ્ધ નથી)",
    _I)

# Order matters: a voicemail preamble may say "is not available", which the
# screener also implies; the more final interpretation wins.
_ORDER = ((VOICEMAIL, _VOICEMAIL_RE), (UNREACHABLE, _UNREACHABLE_RE),
          (SCREENER, _SCREENER_RE), (HOLD, _HOLD_RE))

# A machine line tells the caller WHEN to speak. For a voicemail that the
# agent answers with a message, this is the cue that recording has started.
_RECORD_CUE_RE = re.compile(
    r"record your message|(?:at|add|after) the (?:tone|beep)"
    r"|when you (?:have |'ve )?finished recording|leave (?:a|your) (?:voice )?message"
    r"|बीप के बाद|(?:संदेश|मैसेज) रिकॉर्ड|બીપ (?:પછી|બાદ)",
    _I)

# What a screener says AFTER we have answered it, while it rings the owner:
# "Thanks, please stay on the line", "one moment", "okay". STT splits these
# into bare fragments ("thanks" / "please"), so a fragment made ONLY of these
# words is the screener still talking, not a person.
_SCREENER_RESIDUE = frozenset({
    "thanks", "thank", "you", "please", "stay", "on", "the", "line", "hold",
    "okay", "ok", "one", "moment", "wait", "a", "while", "sure", "alright",
    "all", "right", "connecting", "i'll", "ill", "i", "will", "see", "if",
    "this", "person", "is", "available", "got", "it", "noted", "and",
})

# The words of the hold announcement in all three scripts (and Gujarati-
# script English). STT splits the repeating announcement into pieces that
# fail every pattern above ("the person you are speaking with has put",
# "પર રાખ્યો છે."); a fragment made only of these words is the announcement,
# not a person coming back.
_HOLD_RESIDUE = frozenset({
    # English
    "the", "person", "you", "you're", "youre", "are", "speaking", "with",
    "has", "have", "put", "your", "call", "on", "hold", "please", "stay",
    "line", "is", "busy", "another", "moment", "one", "wait", "kindly",
    "holding", "placed", "been", "a", "to", "spot",
    # Hindi
    "आप", "जिस", "व्यक्ति", "से", "बात", "कर", "रहे", "हैं", "उसने", "आपकी",
    "कॉल", "को", "होल्ड", "पर", "रखा", "है", "कृपया", "लाइन", "लाईन", "बने",
    "रहें", "हॉल", "ने", "रखयो", "छे", "करणे", "एमे", "कृपा",
    # Gujarati
    "તમે", "જે", "વ્યક્તિ", "સાથે", "વાત", "કરી", "રહ્યા", "છો", "એ", "મેં",
    "તમારા", "કોલ", "કૉલ", "કોલને", "કૉલને", "હોલ્ડ", "પર", "રાખ્યો", "છે",
    "કૃપા", "કરીને", "કૃપયા", "લાઈન", "લાઇન", "રહો", "બને", "રહે", "રહેં",
    "આપ", "સે", "કર", "હૈ", "ઉસ", "કો", "રખા", "ધ", "પર્સન", "યુ", "આર",
    "સ્પીકિંગ", "વિથ", "હેસ", "પુટ", "યોર", "ઓન", "પ્લીઝ", "સ્ટે", "એમને",
    "દેવાનું", "તે", "વ્યક્તિને", "કર્યો", "તેને", "તમારો",
})

_WORD_RE = re.compile(r"[0-9a-z'ऀ-ॣ०-ॿ઀-૿]+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.?!।])\s+")


def classify(text: str) -> str | None:
    """The machine kind this utterance is, or None for a person."""
    t = (text or "").strip()
    if len(t) < 6:
        return None
    for kind, rx in _ORDER:
        if rx.search(t):
            return kind
    return None


def split(text: str) -> tuple[str | None, str]:
    """(machine kind, what a PERSON said in the same transcript).

    The pipeline merges fragments that arrive close together, so a hold
    announcement and the person's "hello" can reach the engine as one turn.
    Sentences are classified one by one; the human remainder is returned so
    the engine can answer the person instead of the machine."""
    t = (text or "").strip()
    kind = classify(t)
    if kind is None:
        return None, t
    human: list[str] = []
    for sent in _SENTENCE_SPLIT_RE.split(t):
        s = sent.strip()
        if s and classify(s) is None and not is_residue(kind, s):
            human.append(s)
    return kind, " ".join(human).strip()


def has_record_cue(text: str) -> bool:
    """Is the mailbox now recording (the moment to leave a message)?"""
    return bool(_RECORD_CUE_RE.search(text or ""))


def _only_these_words(text: str, vocab: frozenset, max_tokens: int) -> bool:
    toks = _WORD_RE.findall((text or "").lower())
    return bool(toks) and len(toks) <= max_tokens and all(t in vocab for t in toks)


def is_screener_residue(text: str) -> bool:
    """A fragment made only of a screener's courtesy words."""
    return _only_these_words(text, _SCREENER_RESIDUE, 8)


def is_hold_residue(text: str) -> bool:
    """A fragment made only of the hold announcement's own words."""
    return _only_these_words(text, _HOLD_RESIDUE, 12)


def is_residue(kind: str | None, text: str) -> bool:
    """While waiting through `kind`, is this fragment the machine still
    talking rather than a person coming through?"""
    if kind == SCREENER:
        return is_screener_residue(text)
    if kind == HOLD:
        return is_hold_residue(text)
    return False
