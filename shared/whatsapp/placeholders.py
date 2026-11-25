"""Resolve playbook placeholders for one call.

The same vocabulary the voice engine speaks (RUNBOOK §41/§42), with the same
rule about gender: {honorific} and {title} come ONLY from `contacts.gender`
and resolve to nothing when it is unknown — ~99% of contacts have no gender on
file and it is never guessed from a name.

That rule collides with one WhatsApp constraint: a template variable can never
be empty (Meta rejects the send). So a gender word must never BE a variable on
its own. `{greeting}` is the composed form for exactly that reason —
"Mr. Rakesh Shah" / "Rakesh Shah" / "Sir" / "" — and every variable carries a
fallback ("there") used when the whole rendered value comes out empty.

Tokens:
    {contact_name} {first_name} {honorific} {title} {greeting}
    {agent_name} {company_name} {project_name}
    {visit_date} {visit_time} {link} {slot:<name>}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote

# Mirrors calling-agent/backend/app/voice/address.py (HONORIFIC_WORDS /
# TITLE_WORDS). Copied, not imported: the intelligence worker that runs the
# stage does not have the engine package on its path. test_whatsapp_*
# asserts the two tables stay identical.
HONORIFIC_WORDS = {
    "en": {"male": "Sir", "female": "Ma'am"},
    "hi": {"male": "सर", "female": "मैडम"},
    "gu": {"male": "સર", "female": "મેડમ"},
}
TITLE_WORDS = {
    "en": {"male": "Mr.", "female": "Mrs."},
    "hi": {"male": "श्री", "female": "श्रीमती"},
    "gu": {"male": "શ્રી", "female": "શ્રીમતી"},
}

TOKENS = (
    "contact_name", "first_name", "honorific", "title", "greeting",
    "agent_name", "company_name", "project_name", "visit_date", "visit_time",
    "link",
)

_TOKEN_RE = re.compile(r"\{\s*(slot:[A-Za-z0-9_]+|[a-z_]+)\s*\}")

# Words a name slot must never be mistaken for: an answer to "is this Rakesh?"
# is not the caller's name.
_NOT_A_NAME = {
    "yes", "no", "haan", "han", "ha", "ji", "nahi", "nahin", "ok", "okay",
    "हाँ", "हां", "नहीं", "जी", "sir", "madam", "caller", "customer", "unknown",
    "none", "null", "n/a",
}


def known_gender(gender: str | None) -> str:
    g = (gender or "").strip().lower()
    return g if g in ("male", "female") else ""


def lang_key(language: str | None) -> str:
    key = (language or "en").strip().lower()[:2]
    return key if key in HONORIFIC_WORDS else "en"


def plausible_name(value) -> str:
    """A slot/contact value usable as a person's name, else ''."""
    if not isinstance(value, str):
        return ""
    v = " ".join(value.split())
    if len(v) < 2 or len(v) > 60 or any(ch.isdigit() for ch in v):
        return ""
    if v.lower() in _NOT_A_NAME:
        return ""
    return v


@dataclass
class Context:
    contact_name: str = ""
    gender: str = ""
    language: str = "en"
    agent_name: str = ""
    company_name: str = ""
    project_name: str = ""
    visit_date: str = ""
    visit_time: str = ""
    link: str = ""
    slots: dict = field(default_factory=dict)

    def honorific(self) -> str:
        g = known_gender(self.gender)
        return HONORIFIC_WORDS[lang_key(self.language)][g] if g else ""

    def title(self) -> str:
        g = known_gender(self.gender)
        return TITLE_WORDS[lang_key(self.language)][g] if g else ""

    def first_name(self) -> str:
        return self.contact_name.split()[0] if self.contact_name else ""

    def greeting(self) -> str:
        name = self.contact_name
        if name:
            t = self.title()
            return f"{t} {name}" if t else name
        return self.honorific()

    def value(self, token: str) -> str:
        if token.startswith("slot:"):
            v = self.slots.get(token[5:])
            if v is None or isinstance(v, (dict, list)):
                return ""
            return str(v)
        getter = {
            "contact_name": lambda: self.contact_name,
            "first_name": self.first_name,
            "honorific": self.honorific,
            "title": self.title,
            "greeting": self.greeting,
            "agent_name": lambda: self.agent_name,
            "company_name": lambda: self.company_name,
            "project_name": lambda: self.project_name,
            "visit_date": lambda: self.visit_date,
            "visit_time": lambda: self.visit_time,
            "link": lambda: self.link,
        }.get(token)
        return (getter() or "") if getter else ""


def tidy(text: str) -> str:
    """Close the seam a token that resolved to nothing leaves behind.

    The subset of address.tidy_seams that applies to a message: doubled
    spaces, a space before punctuation, a doubled comma, a line that now
    opens on punctuation."""
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([?.!,;:।])", r"\1", text)
    text = re.sub(r",\s*,", ",", text)
    text = re.sub(r",\s*([?.!।])", r"\1", text)
    text = re.sub(r"^[\s,;:—–\-]+", "", text)
    return text.strip()


def render(expr: str, ctx: Context) -> str:
    """Resolve every token in `expr`. Unknown tokens resolve to nothing —
    a literal "{budget}" must never reach a customer's phone."""
    if not expr:
        return ""
    out = _TOKEN_RE.sub(lambda m: ctx.value(m.group(1).strip()), expr)
    return tidy(out)


def render_url(url: str, ctx: Context) -> str:
    """Like render(), but each resolved value is URL-encoded — the form link
    "…viewform?entry.123={contact_name}" must survive "Rakesh Shah & Co"."""
    if not url:
        return ""
    return _TOKEN_RE.sub(
        lambda m: quote(ctx.value(m.group(1).strip()), safe=""), url.strip())


def meta_safe(text: str, limit: int = 1024) -> str:
    """Meta refuses a text parameter containing a newline, a tab or more than
    four consecutive spaces. Collapse them rather than fail the send."""
    text = re.sub(r"[\r\n\t]+", " ", text or "")
    text = re.sub(r" {2,}", " ", text)
    return text.strip()[:limit]


def tokens_in(expr: str) -> list[str]:
    return [m.group(1).strip() for m in _TOKEN_RE.finditer(expr or "")]
