"""
Conversation memory system — short-term + context + booking data.

Short-term: sliding window of recent turns.
Context: extracted structured info (user name, intent, topic).
Booking: Pramukh Group appointment booking state.
All are injected into LLM prompts for context-aware responses.
"""

from __future__ import annotations

import re
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

MAX_SHORT_TERM_TURNS = 12  # covers a full 20-turn call without dropping early answers

# ---------------------------------------------------------------------------
# Phone number extraction — Indian mobile numbers (10 digits, starts 6-9)
# ---------------------------------------------------------------------------
_PHONE_RE = re.compile(
    r'(?<!\d)'
    r'(?:(?:\+91|91)[-.\s]?)?'      # optional country code
    r'([6-9]\d{4}[-.\s]?\d{5})'     # 10-digit Indian mobile
    r'(?!\d)',
)

# Words that trail a spoken self-introduction without being part of the name
# ("this is sunita speaking", "main amit bol raha hu", "priya here").
_NAME_TRAILING = {
    "speaking", "calling", "here", "side", "this",
    "bol", "bolta", "bolti", "raha", "rahi", "rahe",
    "hu", "hoon", "hun", "hai", "hain", "ho",
    "che", "chhe", "cho", "chu",
}

# Words that END a name — what follows is the company, the place or the
# rest of the sentence. The capture takes up to two words, so "i'm rakesh
# from shah polymers" became the name "Rakesh From" and the agent greeted
# "Amit From" by it (Dechcept sims 2026-09-25 and 2026-09-28).
_NAME_STOP = {"from", "at", "with", "and", "of", "in", "for", "se", "ka", "ki", "ke"}


@dataclass
class Turn:
    role: str       # "user" | "assistant"
    text: str
    language: str = ""  # "en", "hi", "gu" — language at time of turn


@dataclass
class BookingData:
    """
    Universal booking state — valid for any calling agent domain.
    Domain-specific collected fields (location, BHK, department, concern, etc.)
    live in domain_fields so this class never needs editing per-agent.
    """
    user_name: str | None = None
    user_phone: str | None = None
    appointment_date: str | None = None
    appointment_time: str | None = None
    confirmed: bool = False
    # Agent-specific key-value pairs: "location", "property_type", "department", etc.
    domain_fields: dict = field(default_factory=dict)

    def summary(self) -> str:
        parts = []
        if self.user_name:
            parts.append(f"Name: {self.user_name}")
        for k, v in self.domain_fields.items():
            parts.append(f"{k.replace('_', ' ').title()}: {v}")
        if self.appointment_date:
            parts.append(f"Date: {self.appointment_date}")
        if self.appointment_time:
            parts.append(f"Time: {self.appointment_time}")
        return ", ".join(parts) if parts else "No details yet"

    def next_missing_field(self) -> str | None:
        if not self.appointment_date and not self.appointment_time:
            return "appointment"
        return None


@dataclass
class CallState:
    """
    Universal per-call state — valid for any calling agent domain.
    Domain-specific fields (project, location, BHK, department, etc.)
    live in domain_fields so this class never needs editing per-agent.
    Injected into every LLM prompt so the agent never re-asks confirmed info.
    """
    customer_name: str | None = None
    customer_phone: str | None = None   # stored internally; never read back verbatim
    appointment_date: str | None = None
    appointment_time: str | None = None
    language: str = "hi"
    conversation_stage: str = "GREETING"
    booking_validated: bool = False
    # Agent-specific key-value pairs: "selected_project", "location", "department", etc.
    domain_fields: dict = field(default_factory=dict)


@dataclass
class ContextMemory:
    """Structured info extracted from conversation."""
    user_name: str | None = None
    intent: str | None = None
    topic: str | None = None
    sentiment: str = "neutral"
    extra: dict = field(default_factory=dict)

    def to_prompt_string(self) -> str:
        parts = []
        if self.user_name:
            parts.append(f"User's name: {self.user_name}")
        if self.intent:
            parts.append(f"Current intent: {self.intent}")
        if self.topic:
            parts.append(f"Topic: {self.topic}")
        if self.sentiment != "neutral":
            parts.append(f"User sentiment: {self.sentiment}")
        return "\n".join(parts)


class QATracker:
    """
    Tracks which discovery questions have been asked and whether the caller
    answered them — even when data extraction failed to parse the value.

    This prevents the LLM from re-asking questions the caller already responded
    to in natural speech, such as a mangled budget number or an informal name.
    """

    # Signals that indicate the agent is asking about a specific field.
    _FIELD_SIGNALS: dict[str, list[str]] = {
        "name": [
            "your name", "naam", "aapka naam", "naam kya", "naam batao",
            "naam bataiye", "आपका नाम", "नाम बताइए", "नाम क्या",
            "tamaru naam", "tamaro naam", "tamaru", "aapnu naam",
            "what should i call", "may i know your name", "introduce yourself",
            "kon che aap", "tame kon", "tamaru naam shu che",
        ],
        "location": [
            "which city", "kahan", "kahan se", "location", "kis jagah",
            "which location", "city", "कहाँ", "किस शहर", "क्या लोकेशन",
            "ક્યાં", "ક્યા શહેर",
        ],
        "bhk": [
            "bhk", "how many bedroom", "configuration", "config",
            "kitne bhk", "kitne bedroom", "कितने bhk", "BHK chahiye",
            "property size", "flat size", "किस साइज",
        ],
        "budget": [
            "budget", "price range", "kitna budget", "how much",
            "bajat", "बजट", "कितना बजट", "budget kya", "comfortable range",
            "range", "how much are you looking", "investment budget",
            " બજેટ", "ketlu budget",
        ],
        "purpose": [
            "purpose", "self use", "investment", "apne liye",
            "kiraye ke liye", "किस लिए", "invest ke liye",
            "for yourself", "for living", "for renting",
        ],
        "timeline": [
            "when", "how soon", "timeline", "kab", "कब", "ક્યારे",
            "planning to buy", "when are you looking",
        ],
    }

    def __init__(self, field_signals: dict[str, list[str]] | None = None) -> None:
        self._asked: dict[str, int] = {}    # field -> turn_index when asked
        self._answered: dict[str, str] = {} # field -> user answer text (may be raw STT)
        # Per-agent field signals; fall back to the class-level built-in default.
        self._active_signals = field_signals if field_signals else self._FIELD_SIGNALS
        # The field the agent's MOST RECENT line asked about (None when it
        # asked nothing recognisable) — read by the engine to reconcile the
        # slot ask budget with what was actually voiced.
        self.last_asked_field: str | None = None

    def observe_agent_turn(self, agent_text: str, turn_index: int) -> None:
        """Detect which field the agent just asked about."""
        self.last_asked_field = None
        lower = agent_text.lower()
        has_question = "?" in agent_text or any(
            w in lower for w in [
                "what", "which", "how", "when", "where",
                "kya", "kab", "kahan", "kaun", "kitna",
                "क्या", "कब", "कहाँ", "कौन", "कितना",
                "shu", "kem", "kyare", "kon", "keta",
            ]
        )
        if not has_question:
            return
        best: tuple[int, int, str] | None = None   # (position, length, field)
        for field, signals in self._active_signals.items():
            if field in self._answered:
                continue
            hits = [(lower.rfind(s) + len(s), len(s)) for s in signals
                    if s and s in lower]
            if not hits:
                continue
            # The question sits at the END of the line: the field whose
            # signal ENDS furthest right is the one asked; at the same end
            # the longer signal (a slot's own question text beats a shared
            # keyword). Call 1074 (2026-10-12): "₹1.20 करोड़ के बजट में … 4 BHK
            # … — सबसे ज़रूरी क्या है…?" was credited to the first dict entry
            # that matched anywhere, and the factor's ask was un-noted.
            pos_len = max(hits)
            if best is None or pos_len > (best[0], best[1]):
                best = (pos_len[0], pos_len[1], field)
            if field not in self._asked:
                self._asked[field] = turn_index
                logger.info("[QA] Agent asked about %r at turn %d", field, turn_index)
        if best is not None:
            self.last_asked_field = best[2]

    def observe_user_turn(self, user_text: str, turn_index: int,
                          only_fields: set[str] | None = None) -> None:
        """Mark pending asked-fields as answered by this user turn.
        `only_fields` limits which fields this turn CAN answer (a bare
        "okay" answers a yes/no, not "what kind of business do you run?")."""
        for field, asked_at in list(self._asked.items()):
            if only_fields is not None and field not in only_fields:
                continue
            if field not in self._answered and turn_index > asked_at:
                self._answered[field] = user_text[:100]
                logger.info("[QA] Field %r answered: %r", field, user_text[:50])

    def asks_field(self, field: str, agent_text: str) -> bool:
        """Does `agent_text` carry one of `field`'s signals?"""
        lower = (agent_text or "").lower()
        return any(s and s in lower
                   for s in self._active_signals.get(field, []) or [])

    def mark_extracted(self, field: str, value: str) -> None:
        """Override answer with the successfully extracted canonical value."""
        self._answered[field] = value
        if field not in self._asked:
            self._asked[field] = 0

    def is_answered(self, field: str) -> bool:
        return field in self._answered

    def to_prompt_block(self) -> str:
        if not self._answered:
            return ""
        lines = ["━━ QUESTIONS ALREADY ANSWERED — NEVER RE-ASK ━━"]
        for field, answer in self._answered.items():
            lines.append(f"  · {field}: caller already responded ({answer!r})")
        lines.append(
            "Even if extraction was imperfect, the caller HAS answered these. "
            "Move on — do NOT ask again."
        )
        return "\n".join(lines)


class ConversationMemory:
    """
    Manages short-term turn history, structured context, booking state,
    and the question-answer tracker.
    """

    def __init__(
        self,
        max_turns: int = MAX_SHORT_TERM_TURNS,
        field_signals: dict[str, list[str]] | None = None,
    ) -> None:
        self._max_turns = max_turns
        self._turns: list[Turn] = []
        self.context = ContextMemory()
        self.booking = BookingData()
        self.call_state = CallState()
        self.qa_tracker = QATracker(field_signals=field_signals or None)
        # Set by the engine: the same rule its spoken output passes through
        # (gendered address, the agent's own agreement — RUNBOOK §42), so the
        # history the model reads back is what the caller actually heard. A
        # model shown its own "Sir" to a woman one turn ago says it again.
        self.spoken_filter = None

    @property
    def turns(self) -> list[Turn]:
        return self._turns

    @property
    def turn_count(self) -> int:
        return len(self._turns) // 2

    def add_user(self, text: str, language: str = "") -> None:
        self._turns.append(Turn(role="user", text=text, language=language))
        self._extract_context(text)
        self._trim()

    def add_assistant(self, text: str, language: str = "") -> None:
        if self.spoken_filter is not None and text:
            try:
                text = self.spoken_filter(text) or text
            except Exception:  # history must never cost the call
                pass
        self._turns.append(Turn(role="assistant", text=text, language=language))
        self._trim()

    def restore_turns(self, turns: list[Turn]) -> None:
        """Replace the turn list wholesale (turn-transaction rollback)."""
        self._turns = list(turns)

    def recent_assistant_texts(self, n: int = 3) -> list[str]:
        """The agent's last `n` spoken lines, oldest first — what the per-turn
        repetition guards (name fatigue, confirmed-visit restating; RUNBOOK
        §55.5) compare the next reply against."""
        out = [t.text for t in self._turns if t.role == "assistant" and t.text]
        return out[-n:] if n > 0 else []

    def get_history_for_llm(self) -> list[dict[str, str]]:
        return [{"role": t.role, "content": t.text} for t in self._turns]

    @property
    def turn_index(self) -> int:
        """Current turn count (number of complete user+assistant pairs)."""
        return len([t for t in self._turns if t.role == "user"])

    def _trim(self) -> None:
        max_messages = self._max_turns * 2
        if len(self._turns) > max_messages:
            self._turns = self._turns[-max_messages:]

    def _extract_context(self, text: str) -> None:
        lower = text.lower()

        # Phone number — capture once; stored internally and never echoed back
        if not self.call_state.customer_phone:
            m = _PHONE_RE.search(text)
            if m:
                raw = re.sub(r'[-.\s]', '', m.group(0))
                # Normalise: strip country code prefix, keep 10-digit number
                if raw.startswith('+91'):
                    raw = raw[3:]
                elif raw.startswith('91') and len(raw) > 10:
                    raw = raw[2:]
                if len(raw) == 10 and raw[0] in '6789':
                    self.call_state.customer_phone = raw
                    logger.info("[MEMORY] Phone captured (stored internally, not echoed)")

        # STT transcripts arrive fully lowercased (validate_transcript lowercases all text).
        # Patterns use case-insensitive capture groups; results are capitalized on store.
        name_patterns = [
            # English explicit: "my name is rahul", "i am priya"
            r"(?:my name is|i'm|i am|this is|call me)\s+([a-z]{2,}(?:\s+[a-z]{2,})?)",
            # Hindi romanized: "mera naam rahul", "naam hai priya"
            r"(?:mera naam|naam hai|naam mera|aapka naam)\s+([a-z]{2,}(?:\s+[a-z]{2,})?)",
            r"\bnaam\s+([a-z]{3,})",
            # Gujarati romanized: "maru naam", "maro naam"
            r"(?:maru naam|maro naam|mari name)\s+([a-z]{2,}(?:\s+[a-z]{2,})?)",
            # Hindi informal: "main rahul bol raha", captures the name between main and bol/hu
            r"(?:main|mai|mein)\s+([a-z]{3,})\s+(?:bol|baat|calling|speaking|hu|hoon)",
        ]
        _name_blocklist = {
            "a", "the", "just", "not", "is", "interested", "busy",
            "calling", "looking", "available", "free", "here", "there",
            "good", "fine", "okay", "sure", "ready", "happy", "sorry",
            "glad", "trying", "going", "coming", "waiting", "talking",
            "asking", "wondering", "thinking", "planning", "searching",
            # Feeling/state/condition words — caller answering "how are you?"
            # with "i'm <state>" must never be read as a name. These are the
            # words that follow "i'm"/"i am" in small talk, not introductions.
            "even", "better", "best", "well", "doing", "great", "alright",
            "blessed", "grateful", "thankful", "tired", "bored", "stressed",
            "exhausted", "sleepy", "hungry", "sick", "ill", "unwell",
            "nervous", "anxious", "worried", "scared", "afraid", "confused",
            "fantastic", "wonderful", "excellent", "perfect", "amazing",
            "awesome", "fabulous", "fabulous", "cool", "nice", "bit",
            # Present-participle verbs that follow "I'm" in problem reports —
            # never a name. "I'm having/facing/experiencing issues" etc.
            "having", "facing", "experiencing", "dealing", "struggling",
            "looking", "trying", "getting", "seeing", "finding", "making",
            "running", "taking", "giving", "putting", "keeping", "using",
            "working", "calling", "saying", "telling", "hoping", "wanting",
            "going", "coming", "waiting", "talking", "asking", "thinking",
            "planning", "searching", "checking", "testing", "building",
            "solving", "fixing", "handling", "managing", "running",
            "fighting", "battling", "suffering", "losing", "missing",
            "unable", "unable", "just", "currently", "already", "also",
            # Intensifiers / adverbs that precede a state word
            "very", "really", "so", "too", "quite", "pretty", "more", "much",
            "still", "all", "right", "kind", "sort", "of", "feeling",
            "absolutely", "totally", "super", "completely", "actually",
            "honestly", "literally", "fine", "good", "okay", "ok",
            # Courtesy / address words that trail a state reply
            "thanks", "thank", "thankyou", "thanku", "thx", "welcome",
            "please", "yaar", "bhai", "sir", "madam", "you", "yourself",
            "today", "now", "also", "anyway", "though",
            # Degree adverbs missed by the list above — observed live on
            # 2026-08-04: "i'm fairly happy" was captured as the name "Fairly".
            "fairly", "somewhat", "mostly", "rather", "slightly", "reasonably",
            "extremely", "incredibly", "kinda", "sorta", "half", "abit",
            # Time words after "call me ..." — "call me later" was captured
            # as the name "Later" on a live maya_lescall_sdr-outbound run.
            "later", "back", "tomorrow", "tonight", "soon", "afterwards",
            "sometime", "anytime", "again", "after", "morning", "evening",
            "afternoon", "monday", "tuesday", "wednesday", "thursday",
            "friday", "saturday", "sunday", "week", "month",
            "surat", "vapi", "silvassa", "pramukh", "vishal", "patel",
            "mera", "meri", "mere", "naam", "my", "name",
            "maro", "mari", "maru", "apnu", "apna", "aapka",
            "tamaru", "tamaro", "tamari", "ame", "tame",
            # Hindi/Gujarati trailing words that follow a name
            "hai", "hain", "ho", "hun", "hoon", "hu", "hoon",
            "che", "chhe", "cho", "chu",
            "bol", "baat", "raha", "rahi", "rahe",
        }
        if not self.booking.user_name:  # only try if not already captured
            for pattern in name_patterns:
                match = re.search(pattern, lower)
                if match:
                    name = match.group(1).strip()
                    raw = [re.sub(r"[^a-z]", "", t) for t in name.split()]
                    raw = [t for t in raw if t]
                    for _i, _t in enumerate(raw):
                        if _i and _t in _NAME_STOP:
                            raw = raw[:_i]
                            break
                    # Trailing self-intro verbs are part of the SENTENCE, not
                    # the name: "this is sunita speaking" -> Sunita. Stripped
                    # before the all-or-nothing check below so they don't
                    # poison an otherwise real introduction.
                    while len(raw) > 1 and raw[-1] in _NAME_TRAILING:
                        raw.pop()
                    # ALL-OR-NOTHING. The old code dropped blocklisted tokens
                    # and kept the survivors, so any state phrase whose adverb
                    # was not listed became a name: "i'm fairly happy" -> Fairly,
                    # "i am really busy" -> Really, "call me later" -> Later.
                    # One blocked token now poisons the whole candidate; a real
                    # introduction ("my name is rahul sharma") carries none.
                    if not raw or any(t in _name_blocklist for t in raw) \
                            or any(len(t) < 2 for t in raw):
                        continue
                    name_tokens = [t.capitalize() for t in raw]
                    if name_tokens:
                        candidate = " ".join(name_tokens)
                        if len(candidate) > 2 and candidate.lower() not in _name_blocklist:
                            self.context.user_name = candidate
                            self.booking.user_name = candidate
                            logger.info("[MEMORY] Extracted name: %s", candidate)
                            break

        topic_keywords = {
            "appointment": "scheduling", "schedule": "scheduling",
            "book": "scheduling", "visit": "scheduling",
            "cancel": "cancellation",
            "price": "pricing", "cost": "pricing",
            "how much": "pricing", "rate": "pricing", "kitna": "pricing",
            "problem": "support", "issue": "support",
            "flat": "property", "apartment": "property",
            "bhk": "property", "property": "property",
            "makan": "property", "ghar": "property",
        }
        for keyword, topic in topic_keywords.items():
            if keyword in lower:
                self.context.topic = topic
                break

        negative_words = {"angry", "frustrated", "annoyed", "terrible", "awful", "worst", "hate"}
        positive_words = {"great", "thanks", "awesome", "love", "perfect", "excellent", "wonderful",
                          "accha", "badhiya", "saru", "maja"}
        words = set(lower.split())
        if words & negative_words:
            self.context.sentiment = "negative"
        elif words & positive_words:
            self.context.sentiment = "positive"

    def reset(self) -> None:
        self._turns.clear()
        self.context = ContextMemory()
        self.booking = BookingData()
        self.call_state = CallState()
        self.qa_tracker = QATracker()
