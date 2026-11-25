"""Stage — customer long-term memory (governed fact lifecycle).

Merges each analyzed call's facts into a per-contact memory document. Every
entry is a governed fact:

    {text, call_id, date, confidence, status, updated_at}

with status one of active | superseded | expired. The lifecycle rules:

* Dedup by meaning, not string: a new statement similar enough to an active
  fact UPDATES it in place (newest phrasing wins, confidence rises) instead
  of appending a near-duplicate.
* Conflict resolution: a new statement about the same slot (same leading
  topic word) that is NOT a near-duplicate supersedes the old fact — a
  status flip, never a delete, so the timeline keeps the history.
* Expiry: time-sensitive categories decay (an open promise from two months
  ago is stale); stable facts (decision makers, purchases) never expire.
* Caps: active facts per category keep the original caps; inactive
  (superseded/expired) facts are trimmed to a small tail.

Scalars maintained alongside the lists: risk_level, last_intent,
last_sentiment, preferred_language (from calls.language), preferred_call_time
(rolling histogram of call start hours), communication_style (when the
extractor surfaces it in entities).

Read side:
* digest_for_contact() — render the ≤600-char prompt digest from the stored
  memory (used by the extraction stage and the hub API).
* stage_digest() — write-time precompute: renders the digest after every
  merge into customer_memory.context_digest so the live-call bridge pays one
  indexed read, zero assembly (the "hot cache" layer of pre-call context).
"""

from __future__ import annotations

import datetime as dt
import difflib
import json
import logging
import re
from typing import Any

from shared.db.database import get_db

log = logging.getLogger("intel.memory")

_LIST_CAPS = {"preferences": 12, "promises": 12, "objections": 10,
              "followup_history": 10, "relationship_notes": 10,
              "decision_makers": 6, "purchases": 8}
# Superseded/expired tail kept per category (flip, not delete — but bounded).
_INACTIVE_CAP = 6
# Days after which an ACTIVE fact in these categories expires (measured
# against the merged call's date). Absent categories never expire.
_TTL_DAYS = {"promises": 45, "followup_history": 90, "objections": 240,
             "relationship_notes": 240, "preferences": 365}
# Similarity thresholds (SequenceMatcher over normalized text).
_SIM_UPDATE = 0.60     # ≥: same fact — update in place
_SIM_CONFLICT = 0.30   # ≥ (with same leading topic word): supersede


def _norm_words(text: str) -> list[str]:
    return [w for w in re.sub(r"[\W_]+", " ", (text or "").lower()).split() if w]


def _norm(text: str) -> str:
    return "".join(_norm_words(text))


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, " ".join(_norm_words(a)),
                                   " ".join(_norm_words(b))).ratio()


def _date_of(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _coerce_fact(entry: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a legacy {text, call_id, date} entry to the governed shape."""
    fact = dict(entry or {})
    fact.setdefault("text", "")
    fact.setdefault("confidence", 0.6)
    fact.setdefault("status", "active")
    fact.setdefault("updated_at", fact.get("date"))
    return fact


def _merge_facts(existing: list[dict], new_items: list[dict], cap: int) -> list[dict]:
    """Lifecycle merge for one category. Pure; returns the new list."""
    facts = [_coerce_fact(e) for e in existing]
    for item in new_items:
        text = (item.get("text") or "").strip()
        if not _norm(text):
            continue
        active = [f for f in facts if f.get("status") == "active"]
        best, best_sim = None, 0.0
        for f in active:
            sim = _similarity(f["text"], text)
            if sim > best_sim:
                best, best_sim = f, sim
        if best is not None and best_sim >= _SIM_UPDATE:
            # Same fact, possibly rephrased — newest phrasing wins.
            best.update(
                text=text, call_id=item.get("call_id"), date=item.get("date"),
                updated_at=item.get("date"),
                confidence=min(0.95, float(best.get("confidence") or 0.6) + 0.15),
                status="active",
            )
            continue
        words = _norm_words(text)
        conflict = None
        if words:
            for f in active:
                fw = _norm_words(f["text"])
                if fw and fw[0] == words[0] and _similarity(f["text"], text) >= _SIM_CONFLICT:
                    conflict = f
                    break
        if conflict is not None:
            # New statement about the same slot — flip, don't delete.
            conflict["status"] = "superseded"
            conflict["updated_at"] = item.get("date")
        facts.append({
            "text": text, "call_id": item.get("call_id"),
            "date": item.get("date"), "updated_at": item.get("date"),
            "confidence": float(item.get("confidence") or 0.6),
            "status": "active",
        })
    # Caps: keep the newest `cap` active facts and a short inactive tail,
    # preserving original order.
    active = [f for f in facts if f.get("status") == "active"]
    inactive = [f for f in facts if f.get("status") != "active"]
    keep = set(map(id, active[-cap:])) | set(map(id, inactive[-_INACTIVE_CAP:]))
    return [f for f in facts if id(f) in keep]


def _apply_expiry(memory: dict[str, Any], call_date: dt.date | None) -> None:
    if call_date is None:
        return
    for cat, ttl in _TTL_DAYS.items():
        for f in memory.get(cat) or []:
            if f.get("status") != "active":
                continue
            d = _date_of(f.get("date"))
            if d is not None and (call_date - d).days > ttl:
                f["status"] = "expired"
                f["updated_at"] = call_date.isoformat()


_LANG_NAMES = {"en": "English", "hi": "Hindi", "gu": "Gujarati",
               "mr": "Marathi", "ta": "Tamil", "te": "Telugu"}
_IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def _time_bucket(started_at: str | None) -> str | None:
    try:
        t = dt.datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    hour = t.astimezone(_IST).hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "late"


def merge_call(memory: dict[str, Any], call: dict[str, Any],
               ci: dict[str, Any], commitments: list[dict[str, Any]],
               followup_decision: str | None) -> dict[str, Any]:
    """Pure merge (unit-testable)."""
    call_id = call["id"]
    date = (call.get("started_at") or "")[:10]

    def prov(text: str) -> dict[str, Any]:
        return {"text": text, "call_id": call_id, "date": date}

    def jlist(field: str) -> list[str]:
        try:
            return json.loads(ci.get(field) or "[]")
        except Exception:
            return []

    memory = dict(memory or {})
    for cat in _LIST_CAPS:
        memory.setdefault(cat, [])

    memory["preferences"] = _merge_facts(
        memory["preferences"], [prov(t) for t in jlist("product_interest")],
        _LIST_CAPS["preferences"])
    memory["objections"] = _merge_facts(
        memory["objections"], [prov(t) for t in jlist("objections")],
        _LIST_CAPS["objections"])
    memory["decision_makers"] = _merge_facts(
        memory["decision_makers"], [prov(t) for t in jlist("decision_makers")],
        _LIST_CAPS["decision_makers"])
    memory["promises"] = _merge_facts(
        memory["promises"],
        [prov(f"{c.get('made_by')}: {c.get('description')}") for c in commitments],
        _LIST_CAPS["promises"])
    if ci.get("summary"):
        memory["relationship_notes"] = _merge_facts(
            memory["relationship_notes"], [prov(ci["summary"])],
            _LIST_CAPS["relationship_notes"])
    if followup_decision:
        memory["followup_history"] = _merge_facts(
            memory["followup_history"],
            [prov(f"follow-up decision: {followup_decision}")],
            _LIST_CAPS["followup_history"])

    # Entities are optional extractor extras; purchases / communication_style
    # ride there when the model surfaces them (the DDL comment's promise).
    try:
        entities = json.loads(ci.get("entities") or "{}") or {}
    except Exception:
        entities = {}
    purchases = entities.get("purchases")
    if isinstance(purchases, list):
        memory["purchases"] = _merge_facts(
            memory["purchases"],
            [prov(str(p)) for p in purchases if isinstance(p, (str, int, float))],
            _LIST_CAPS["purchases"])
    style = entities.get("communication_style")
    if isinstance(style, str) and style.strip():
        memory["communication_style"] = style.strip()[:120]

    _apply_expiry(memory, _date_of(date))

    risk = ci.get("risk_score")
    if isinstance(risk, int):
        memory["risk_level"] = ("high" if risk >= 70 else
                                "medium" if risk >= 40 else "low")
    if ci.get("customer_intent"):
        memory["last_intent"] = ci["customer_intent"]
    if ci.get("sentiment"):
        memory["last_sentiment"] = ci["sentiment"]

    lang = (call.get("language") or "").strip().lower()[:2]
    if lang in _LANG_NAMES:
        memory["preferred_language"] = lang

    bucket = _time_bucket(call.get("started_at"))
    if bucket:
        hist = dict(memory.get("call_time_histogram") or {})
        hist[bucket] = int(hist.get(bucket) or 0) + 1
        memory["call_time_histogram"] = hist
        top = max(hist.items(), key=lambda kv: kv[1])
        if top[1] >= 2:
            memory["preferred_call_time"] = top[0]
    return memory


async def stage_memory(call: dict[str, Any]) -> None:
    contact_id = call.get("contact_id")
    if not contact_id:
        return
    call_id = call["id"]
    with get_db() as db:
        ci_row = db.execute(
            "SELECT * FROM call_intelligence WHERE call_id=?", (call_id,)
        ).fetchone()
        if not ci_row:
            return
        commitments = [dict(r) for r in db.execute(
            "SELECT made_by, description FROM commitments WHERE call_id=?", (call_id,)
        ).fetchall()]
        fu = db.execute(
            "SELECT decision FROM followups WHERE call_id=? ORDER BY id DESC LIMIT 1",
            (call_id,),
        ).fetchone()
        mem_row = db.execute(
            "SELECT memory FROM customer_memory WHERE contact_id=?", (contact_id,)
        ).fetchone()

    existing = {}
    if mem_row:
        try:
            existing = json.loads(mem_row["memory"] or "{}")
        except Exception:
            existing = {}
    merged = merge_call(existing, call, dict(ci_row), commitments,
                        fu["decision"] if fu else None)

    with get_db() as db:
        db.execute(
            """INSERT INTO customer_memory
                   (contact_id, owner, memory, calls_count, last_call_at, updated_at)
               VALUES (?, ?, ?, 1, ?, now())
               ON CONFLICT (contact_id) DO UPDATE SET
                   memory=EXCLUDED.memory,
                   calls_count=customer_memory.calls_count + 1,
                   last_call_at=EXCLUDED.last_call_at,
                   version=customer_memory.version + 1,
                   updated_at=now()""",
            (contact_id, call.get("owner") or "", json.dumps(merged, ensure_ascii=False),
             call.get("started_at")),
        )


# ---------------------------------------------------------------------------
# read side: digest render + write-time precompute
# ---------------------------------------------------------------------------

def _active_texts(mem: dict[str, Any], cat: str, n: int) -> list[str]:
    facts = [_coerce_fact(f) for f in mem.get(cat) or []]
    return [f["text"] for f in facts if f.get("status") == "active"][-n:]


def render_digest(mem: dict[str, Any], calls_count: int,
                  limit_chars: int = 600) -> str:
    """Pure render of the prompt digest from a memory document."""
    parts: list[str] = [f"{calls_count} previous call(s)."]
    if mem.get("last_intent"):
        parts.append(f"Last intent: {mem['last_intent']}.")
    prefer_bits: list[str] = []
    if mem.get("preferred_language"):
        prefer_bits.append(_LANG_NAMES.get(mem["preferred_language"],
                                           mem["preferred_language"]))
    if mem.get("preferred_call_time"):
        prefer_bits.append(f"{mem['preferred_call_time']} calls")
    if prefer_bits:
        parts.append("Prefers " + ", ".join(prefer_bits) + ".")
    if mem.get("communication_style"):
        parts.append(f"Style: {mem['communication_style']}.")
    prefs = _active_texts(mem, "preferences", 3)
    if prefs:
        parts.append("Interested in: " + "; ".join(prefs) + ".")
    purchases = _active_texts(mem, "purchases", 2)
    if purchases:
        parts.append("Purchased: " + "; ".join(purchases) + ".")
    promises = []
    for f in [_coerce_fact(x) for x in mem.get("promises") or []]:
        if f.get("status") == "active":
            promises.append(f"{f['text']} ({f['date']})" if f.get("date") else f["text"])
    if promises:
        parts.append("Open promises: " + "; ".join(promises[-2:]) + ".")
    objections = _active_texts(mem, "objections", 2)
    if objections:
        parts.append("Past objections: " + "; ".join(objections) + ".")
    makers = _active_texts(mem, "decision_makers", 2)
    if makers:
        parts.append("Decision makers: " + "; ".join(makers) + ".")
    if mem.get("risk_level") == "high":
        parts.append("HIGH churn risk — handle with care.")
    return " ".join(parts)[:limit_chars]


def digest_for_contact(contact_id: int, limit_chars: int = 600) -> str:
    """Compact history digest for prompt injection."""
    with get_db() as db:
        row = db.execute(
            "SELECT memory, calls_count FROM customer_memory WHERE contact_id=?",
            (contact_id,),
        ).fetchone()
    if not row:
        return ""
    try:
        mem = json.loads(row["memory"] or "{}")
    except Exception:
        return ""
    return render_digest(mem, row["calls_count"], limit_chars)


async def stage_digest(call: dict[str, Any]) -> None:
    """Precompute the pre-call context digest at write time (runs after the
    memory merge, so the digest always reflects this call)."""
    contact_id = call.get("contact_id")
    if not contact_id:
        return
    with get_db() as db:
        row = db.execute(
            "SELECT memory, calls_count FROM customer_memory WHERE contact_id=?",
            (contact_id,),
        ).fetchone()
    if not row:
        return
    try:
        mem = json.loads(row["memory"] or "{}")
    except Exception:
        mem = {}
    digest = render_digest(mem, row["calls_count"])
    with get_db() as db:
        db.execute(
            "UPDATE customer_memory SET context_digest=?, digest_updated_at=now()"
            " WHERE contact_id=?",
            (digest, contact_id),
        )
