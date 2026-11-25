"""
catalog — pure functions over knowledge_base.catalog / match_rules (D16).

An empty catalog means everything falls back to the legacy
portfolio_prompt_text verbatim — byte-identical to pre-v2 behaviour.
"""

from __future__ import annotations

import re
from typing import Any


# Catalogue-item DETAIL keys rendered into the knowledge band, in this order,
# when an item carries them (RUNBOOK §55). A catalogue item is an open dict
# on purpose — a project scraped from a website has a RERA number, an
# amenities list and an address that the five core fields cannot hold, and
# the model must read "RERA: …", not a YAML key. Keys not listed here (media,
# source, faq, contact_email) stay DATA for the dashboard and the Copilot and
# never reach the spoken prompt.
_DETAIL_KEYS: tuple[tuple[str, str], ...] = (
    ("project_type", "Type"),
    ("tagline", "Tagline"),
    ("ideal_for", "Ideal for"),
    ("address", "Address"),
    ("rera", "RERA"),
    ("status", "Status"),
    ("amenities", "Amenities"),
    ("layout", "Layout"),
    ("home_features", "Each home"),
    ("specifications", "Specifications"),
    ("connectivity", "Connectivity"),
    ("landmarks", "Nearby"),
    ("lifestyle", "Lifestyle"),
    ("highlights", "Highlights"),
    ("brochure", "Brochure"),
    ("contact", "Contact"),
    ("not_published", "NOT published — never guess"),
)


# Owner review of the Green County agent (2026-10-12, F1 — RUNBOOK §76): a
# project that is still being built lists its amenities as a flat "Amenities:"
# line, and the model reads a list as an inventory — "you have an indoor pool
# and a Dolby theatre", to a caller, as if both were open today. The clause is
# emitted ONLY for an item that carries `amenities` AND whose `availability`
# or `status` says under construction; every other item renders byte-identically
# to before. Engine-level on purpose: the shared Pramukh catalogue is data the
# owner edits, and a rule in one agent's prompt would not follow the project
# into the 27 other agents that carry the same entry.
_UNDER_CONSTRUCTION_RE = re.compile(r"under[\s-]*construction", re.IGNORECASE)
_AMENITY_STATUS_CLAUSE = (
    "Amenities are what the project offers or plans — do not say any of them "
    "is built, open or in use unless Status says so; say it is part of the "
    "project.")


def amenity_status_clause(item: dict) -> str:
    """The amenity-status clause for an under-construction item with
    amenities; '' for every other item (and for every pre-§55 catalogue)."""
    item = item or {}
    if not item.get("amenities"):
        return ""
    stated = " ".join(_detail_value(item.get(k)) for k in ("availability", "status")
                      if item.get(k))
    return _AMENITY_STATUS_CLAUSE if _UNDER_CONSTRUCTION_RE.search(stated) else ""


def _detail_value(value: Any) -> str:
    if isinstance(value, dict):
        return "; ".join(f"{k} {v}" for k, v in value.items() if str(v).strip())
    if isinstance(value, (list, tuple)):
        items = [str(x).strip() for x in value if str(x).strip()]
        # An address-shaped item carries its own commas; separate those with
        # semicolons so "opposite X, Y, the office, on Z" reads as two places.
        sep = "; " if any("," in x for x in items) else ", "
        return sep.join(items)
    return str(value).strip()


def render_item_details(item: dict) -> str:
    """'Type: …. RERA: …. Amenities: ….' for the detail keys an item carries;
    '' for an item that carries none (every pre-§55 catalogue)."""
    parts: list[str] = []
    for key, label in _DETAIL_KEYS:
        raw = (item or {}).get(key)
        text = _detail_value(raw) if raw else ""
        if text:
            parts.append(f"{label}: {text.rstrip('.')}.")
    clause = amenity_status_clause(item)
    if clause:
        parts.append(clause)
    return " ".join(parts)


def _render_item(item: dict) -> str:
    """One knowledge-band line for a catalogue item ('' when it has no name)."""
    name = str(item.get("name") or "").strip()
    if not name:
        return ""
    bits: list[str] = []
    if item.get("location"):
        bits.append(f"in {item['location']}")
    if item.get("configurations"):
        cfgs = item["configurations"]
        bits.append("/".join(str(c) for c in cfgs) if isinstance(cfgs, list) else str(cfgs))
    if item.get("price_range"):
        bits.append(str(item["price_range"]))
    if item.get("availability"):
        bits.append(str(item["availability"]))
    if item.get("positioning_tag"):
        bits.append(f"({item['positioning_tag']})")
    line = f"▸ {name}" + (f" — {', '.join(bits)}" if bits else "")
    pitch = item.get("pitch_points")
    if isinstance(pitch, list) and pitch:
        line += ". " + " ".join(str(p).rstrip(".") + "." for p in pitch)
    facts = item.get("facts")
    if isinstance(facts, dict) and facts:
        line += " " + " ".join(f"{k}: {v}." for k, v in facts.items())
    details = render_item_details(item)
    if details:
        line += " " + details
    return line


def primary_project(kb) -> dict | None:
    """{id, mode, reason, name, item} for knowledge_base.primary_project.

    None when no primary is set — and None when the id names nothing in the
    catalogue: a primary that cannot be rendered must not silently turn every
    other item informational. `mode` is 'exclusive' unless 'lead' is spelled
    out.
    """
    pp = getattr(kb, "primary_project", None) or {}
    if not isinstance(pp, dict):
        return None
    pid = str(pp.get("id") or "").strip().lower()
    if not pid:
        return None
    item = next((i for i in (getattr(kb, "catalog", None) or [])
                 if isinstance(i, dict)
                 and str(i.get("id") or "").strip().lower() == pid), None)
    if not item:
        return None
    mode = str(pp.get("mode") or "exclusive").strip().lower()
    if mode not in ("exclusive", "lead"):
        mode = "exclusive"
    return {"id": pid, "mode": mode,
            "reason": str(pp.get("reason") or "").strip(),
            "name": str(item.get("name") or pid).strip(), "item": item,
            # RUNBOOK §68: the one sanctioned exception to "never volunteer
            # another project" — a budget that does not fit the primary.
            "budget_switch": bool(pp.get("budget_switch"))}


def primary_city(kb) -> str | None:
    """The catalogue city the primary project sits in, or None."""
    pp = primary_project(kb)
    if not pp:
        return None
    loc = str(pp["item"].get("location") or "").lower()
    for city in (getattr(kb, "locations", None) or []):
        c = str(city).strip()
        if c and _word_in(c.lower(), loc):
            return c
    return None


def render_catalog_prompt(kb) -> str:
    """Knowledge paragraph for the system prompt.

    catalog empty → portfolio_prompt_text VERBATIM (today's behaviour).
    catalog present → one structured line per item + the portfolio text
    appended (extra colour the owner wrote is never lost).
    primary_project set → that item renders FIRST under a ★ PRIMARY PROJECT
    heading, and the rest are labelled information-only (exclusive) or
    "after the primary" (lead). No primary → byte-identical to before.
    """
    catalog = getattr(kb, "catalog", None) or []
    if not catalog:
        return kb.portfolio_prompt_text
    lines: list[str] = []
    pp = primary_project(kb)
    if pp:
        head = f"★ PRIMARY PROJECT — the one you pitch: {pp['name']}"
        if pp["reason"]:
            head += f" ({pp['reason']})"
        lines.append(head)
        lines.append(_render_item(pp["item"]))
        if pp["mode"] == "exclusive":
            lines.append(
                "▸ Every project below is INFORMATION ONLY: when the caller "
                "asks about one, answer plainly and exactly as written — "
                f"then bring the conversation back to {pp['name']} when it "
                "fits. Never volunteer, recommend or pitch one of them, and "
                f"never offer one in {pp['name']}'s place unless the caller "
                "asks what else there is"
                + (" — or a ━━ BUDGET FIT ━━ note below tells you their "
                   "budget fits one of them better: then you may SUGGEST it "
                   "in one sentence, and it becomes the project you pitch "
                   "only after the caller clearly says yes."
                   if pp.get("budget_switch") else "."))
        else:
            lines.append(
                f"▸ The projects below may follow {pp['name']} when they fit "
                "what the caller said better — but it is always named first.")
    for item in catalog:
        if pp and item is pp["item"]:
            continue
        line = _render_item(item)
        if line:
            lines.append(line)
    if kb.portfolio_prompt_text:
        lines.append("")
        lines.append(kb.portfolio_prompt_text)
    return "\n".join(lines)


def derive_known_projects(kb) -> dict[str, tuple[str, str]]:
    """known_projects extended with catalog names/aliases (extractor map).

    Existing explicit known_projects entries always win.
    """
    out: dict[str, tuple[str, str]] = {}
    for item in (getattr(kb, "catalog", None) or []):
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        location = str(item.get("location") or "").strip()
        keys = [name.lower()]
        aliases = item.get("aliases")
        if isinstance(aliases, list):
            keys += [str(a).strip().lower() for a in aliases if str(a).strip()]
        for k in keys:
            out.setdefault(k, (name, location))
    out.update(kb.known_projects or {})
    return out


_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")


def _budget_to_lakhs(value: str) -> float | None:
    """'50 Lakhs' / '1.2 Crore' / '₹22-29L' → representative number in lakhs."""
    s = str(value or "").lower()
    nums = [float(n) for n in _NUM_RE.findall(s)]
    if not nums:
        return None
    n = max(nums)  # for ranges use the upper bound (most permissive match)
    if "cr" in s or "करोड" in s or "crore" in s:
        n *= 100
    return n


def _cond_matches(cond: str, actual: Any) -> bool:
    """Evaluate one match_rules condition against a slot value.

    Supports '<=40L' / '>=1Cr' style budget comparisons and
    case-insensitive equality for everything else.
    """
    if actual is None:
        return False
    cond_s = str(cond).strip()
    m = re.match(r"^(<=|>=|<|>)\s*(.+)$", cond_s)
    if m:
        op, rhs = m.group(1), m.group(2)
        left = _budget_to_lakhs(str(actual))
        right = _budget_to_lakhs(rhs)
        if left is None or right is None:
            return False
        return {"<=": left <= right, ">=": left >= right,
                "<": left < right, ">": left > right}[op]
    return str(actual).strip().lower() == cond_s.lower()


def _resolve_slot(key: str, slots: dict[str, Any]) -> Any:
    """Slot value for a match-rule `when` key, tolerating the natural name gap.

    Owners write `when: {budget: '<=40L'}` while the slot is `budget_range`,
    and `{location: Vapi}` while the slot is `preferred_location` — the exact
    lookup then returns None, `_cond_matches` returns False, and the whole
    rule set is silently dead (every rule of the live Pramukh agent was, for
    exactly this reason). Exact key wins; otherwise a slot whose underscore
    components contain the key, but only when that match is unambiguous.
    """
    if key in slots:
        return slots[key]
    k = str(key).strip().lower()
    cands = [n for n in slots if k in str(n).lower().split("_")]
    return slots[cands[0]] if len(cands) == 1 else None


def _word_in(needle: str, hay: str) -> bool:
    """`needle` occurs in `hay` as a whole word. Boundaries are "not
    alphanumeric" rather than \b so Devanagari / Gujarati text works too."""
    if not needle or not hay:
        return False
    start = 0
    while True:
        idx = hay.find(needle, start)
        if idx < 0:
            return False
        before_ok = idx == 0 or not hay[idx - 1].isalnum()
        after = idx + len(needle)
        after_ok = after >= len(hay) or not hay[after].isalnum()
        if before_ok and after_ok:
            return True
        start = idx + 1


# Slot-name components that say "this slot holds a PLACE". A match rule
# engine that does not know which slot is the caller's city cannot keep a
# recommendation inside it.
_PLACE_KEY_PARTS = frozenset({"location", "city", "locality", "area", "town",
                              "region"})


def caller_city(kb, slots: dict[str, Any]) -> str | None:
    """The catalogue city the caller has named, or None.

    Reads every filled slot whose NAME says it holds a place and resolves the
    value against `kb.locations` — directly ("Vapi", "Vesu, Surat"), or via a
    catalogue item's own `location` string, so a caller who said "Chala"
    resolves to Vapi through "Chala, Vapi" without anyone having to list
    localities in config. None when no place slot is filled, or the place is
    nowhere the catalogue reaches.
    """
    locations = [str(l).strip() for l in (getattr(kb, "locations", None) or [])
                 if str(l).strip()]
    if not locations:
        return None
    catalog = getattr(kb, "catalog", None) or []
    for key, val in (slots or {}).items():
        if not (set(str(key).lower().split("_")) & _PLACE_KEY_PARTS):
            continue
        v = " ".join(str(val or "").lower().split())
        if not v:
            continue
        for city in locations:
            if _word_in(city.lower(), v):
                return city
        for item in catalog:
            loc = str(item.get("location") or "").lower()
            if loc and _word_in(v, loc):
                for city in locations:
                    if _word_in(city.lower(), loc):
                        return city
    return None


def _item_in_city(item: dict, city: str) -> bool:
    """An item with no `location` is never filtered out — nothing is known
    about where it is, and a silent drop would be a new kind of wrong."""
    loc = str((item or {}).get("location") or "").lower()
    return not loc or _word_in(city.lower(), loc)


def _raw_match_hits(kb, slots: dict[str, Any]) -> list[dict]:
    rules = getattr(kb, "match_rules", None) or []
    slots = slots or {}
    hits: list[dict] = []
    for rule in rules:
        when = rule.get("when")
        if not isinstance(when, dict) or not when:
            continue
        if all(_cond_matches(cond, _resolve_slot(key, slots))
               for key, cond in when.items()):
            hits.append({
                "recommend": [str(r) for r in (rule.get("recommend") or [])],
                "reason": str(rule.get("reason") or ""),
            })
    return hits


def scope_hits_to_city(kb, city: str | None, hits: list[dict]) -> list[dict]:
    """Drop every recommended id that the catalogue places in ANOTHER city.

    Rules are a union, and each fires on its own condition. Live 2026-09-24
    (call 904): the caller had said Chala, in Vapi, and a budget of 50 lakh;
    `budget <= 70L → Aranya II (Surat)` fired on its own and the agent — with a
    visit at Pramukh Vedanta in Chala already agreed — told them Pramukh
    Aranya II in Surat "could be a genuinely good fit". A rule may know one
    fact; the caller's city outranks every one of them. Ids the catalogue does
    not carry, and items without a `location`, are kept untouched.
    """
    if not city or not hits:
        return hits
    by_id = {str(i.get("id") or "").lower(): i
             for i in (getattr(kb, "catalog", None) or [])}
    scoped: list[dict] = []
    for hit in hits:
        keep = [r for r in hit["recommend"]
                if _item_in_city(by_id.get(str(r).lower(), {}), city)]
        if keep:
            scoped.append({**hit, "recommend": keep})
    return scoped


def evaluate_match_rules(kb, slots: dict[str, Any]) -> list[dict]:
    """Deterministic recommendation candidates for the current slot state.

    Returns [{recommend: [ids], reason: str}] for every rule whose `when`
    conditions ALL match a filled slot value — narrowed to the caller's city
    when they have named one (see scope_hits_to_city). Empty rules/slots → [].

    With `knowledge_base.primary_project` set (RUNBOOK §55) the primary is
    ALWAYS the first candidate, before any slot is filled — "recommend it by
    default" — carrying `primary: True`. In `exclusive` mode it is the ONLY
    candidate: the rules still exist (for `lead` mode and for rollback) but
    their hits never reach the band, so nothing else can be pitched. In
    `lead` mode the scoped rule hits follow it, minus the primary itself.
    """
    slots = slots or {}
    hits = scope_hits_to_city(kb, caller_city(kb, slots),
                              _raw_match_hits(kb, slots))
    pp = primary_project(kb)
    if not pp:
        return hits
    lead = {"recommend": [pp["id"]],
            "reason": pp["reason"] or "the primary project — lead with it",
            "primary": True}
    if pp["mode"] == "exclusive":
        return [lead]
    rest: list[dict] = []
    for hit in hits:
        keep = [r for r in hit["recommend"] if str(r).lower() != pp["id"]]
        if keep:
            rest.append({**hit, "recommend": keep})
    return [lead] + rest


def catalog_item_names(kb, ids: list[str]) -> list[str]:
    """Resolve catalog item ids to display names (id itself as fallback)."""
    by_id = {str(i.get("id") or "").lower(): str(i.get("name") or i.get("id"))
             for i in (getattr(kb, "catalog", None) or [])}
    return [by_id.get(str(x).lower(), str(x)) for x in ids]
