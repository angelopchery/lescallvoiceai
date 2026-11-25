"""Boundary scrubbing for text that arrives from the network.

One specific hazard drives this module: **Postgres text columns cannot hold a
NUL byte**. A `?q=%00` in a query string, or a stray `\\u0000` inside a JSON
name field, reaches psycopg and raises `DataError` — which is a 500 for what is
plainly a bad request, and a 500 that a caller can trigger at will is a denial
of service with extra steps.

Stripping rather than rejecting is deliberate. A control character in a
customer name is never meaningful data; it is a copy-paste artefact or an
export bug in whatever system is pushing to us. Refusing the request would fail
a 400-row import over one invisible byte in row 212, which helps nobody. What
IS kept: tab, newline and carriage return, because a notes field legitimately
contains them.

Applied at exactly two places — `_json_body` for bodies and `_q` for string
query parameters — so there is one answer to "where does untrusted text get
cleaned", not a scattering of per-field `.replace()` calls that the next
endpoint forgets to copy.
"""

from __future__ import annotations

# Everything below 0x20 except tab (09), newline (0A) and carriage return (0D),
# plus DEL. NUL is the one that actually breaks Postgres; the rest are stripped
# with it because they are equally meaningless in a name, and leaving them in
# means they turn up later in a CSV export or a log line.
_STRIP = {c: None for c in range(0x20) if c not in (0x09, 0x0A, 0x0D)}
_STRIP[0x7F] = None

MAX_TEXT = 20_000
MAX_KEYS = 200
MAX_DEPTH = 20


def text(value, limit: int = MAX_TEXT) -> str:
    """One string, scrubbed and length-capped."""
    if value is None:
        return ""
    return str(value).translate(_STRIP)[:limit]


def deep(value, _depth: int = 0):
    """Recursively scrub a decoded JSON value.

    Also bounds the structure: a body nested 500 deep, or a dict with 50 000
    keys, costs real CPU to walk and then to serialise back out. The caps are
    far above any legitimate CRM payload.
    """
    if _depth > MAX_DEPTH:
        return None
    if isinstance(value, str):
        return text(value)
    if isinstance(value, dict):
        out = {}
        for k, v in list(value.items())[:MAX_KEYS]:
            out[text(k, 200)] = deep(v, _depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [deep(v, _depth + 1) for v in value[:1000]]
    return value
