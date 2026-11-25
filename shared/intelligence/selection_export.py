"""The Excel workbook for a SELECTION of calls (Intelligence → Export).

A saved report exports one document; this exports the thing an operator
actually selects on the Intelligence page — some rows, usually the output of a
filter — as an analysis-ready workbook, and puts it in Google Cloud Storage.

Four questions, four sheets, because they are four different tables and
cramming them into one is what makes an export unusable:

    Numbers      one row per PHONE — the call list, deduplicated
    Contacts     one row per contact record — who they are
    Calls        one row per CALL — what happened, scored
    Insights     one row per call — what was extracted from it
    Transcripts  one row per call — the words
    Commitments  one row per promise — what is outstanding
    Evidence     one row per call — WHY the filter selected it (when the
                 selection came from a verified AI filter)
    Summary      what this file is, which question produced it
    Legend       what every band and score means

The same doctrine as report_export.build_xlsx: one header row, no merged cells
in data sheets, real numbers and real dates, stable column order, autofilter +
frozen panes. `Numbers` deliberately comes first — it is the sheet people
paste into a dialler.
"""

from __future__ import annotations

import io
import logging
from datetime import datetime
from typing import Any

from shared.intelligence.report_export import (
    BAND_HOT, BAND_WARM, CALL_COLUMNS, HOT_LEAD_MIN, RISK_MIN, WARM_LEAD_MIN,
    _CALL_COLUMNS, _dt, _float, _int, _jdict, _prepare, _texts, lead_band,
    load_commitments,
)

log = logging.getLogger("selection_export")

# Excel's hard per-cell ceiling is 32767 characters; a transcript past that is
# truncated with a marker rather than silently losing its tail (or, worse,
# corrupting the file — openpyxl writes it and Excel refuses to open it).
CELL_MAX = 32000

HEAD_BG = "0F172A"


def load_calls(db, owner: str, call_ids: list[int]) -> list[dict]:
    """The selected calls, joined to intelligence and contact, newest first.

    Owner-scoped in SQL: an id that belongs to another tenant simply is not in
    the result, so a hand-edited request exports nothing rather than something.
    """
    ids = [i for i in (_int(x) for x in call_ids) if i is not None]
    if not ids:
        return []
    rows = db.execute(
        f"SELECT {_CALL_COLUMNS}, c.transcript, c.download_token, c.gcs_object, "
        "       c.placed_by, c.created_at "
        "  FROM calls c "
        "  LEFT JOIN call_intelligence i ON i.call_id = c.id "
        "  LEFT JOIN contacts ct ON ct.id = c.contact_id "
        " WHERE c.owner = ? AND c.id = ANY(?) "
        " ORDER BY c.started_at DESC NULLS LAST",
        (owner, ids),
    ).fetchall()
    return [dict(r) for r in rows]


def load_contacts(db, owner: str, contact_ids: list[int]) -> dict[int, dict]:
    ids = [i for i in (_int(x) for x in contact_ids) if i is not None]
    if not ids:
        return {}
    rows = db.execute(
        """SELECT id, name, phone, sig_digits, notes, details, call_status,
                  disposition, call_count, last_called_at, gender, group_id,
                  primary_agent_slug, added_at, created_at
             FROM contacts WHERE owner = ? AND id = ANY(?)""",
        (owner, ids),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def load_groups(db, owner: str) -> dict[int, str]:
    try:
        rows = db.execute(
            "SELECT id, name FROM contact_groups WHERE owner = ?", (owner,)
        ).fetchall()
    except Exception:      # the table is optional on older deployments
        return {}
    return {r["id"]: r["name"] for r in rows}


# --------------------------------------------------------------- sheet specs

NUMBER_COLUMNS: list[tuple[str, str]] = [
    ("Phone", "phone"),
    ("Name", "name"),
    ("Calls in this export", "calls"),
    ("Total talk time (s)", "talk_s"),
    ("First call (in export)", "first_at"),
    ("Last call (in export)", "last_at"),
    ("Best lead score", "best_lead"),
    ("Lead band", "band"),
    ("Latest sentiment", "sentiment"),
    ("Needs human", "needs_human"),
    ("Follow-up open", "followup"),
    ("Direction", "direction"),
    ("Agent(s)", "agents"),
    ("Contact on file", "known"),
]

CONTACT_COLUMNS: list[tuple[str, str]] = [
    ("Name", "name"),
    ("Phone", "phone"),
    ("Group", "group_name"),
    ("Disposition", "disposition"),
    ("Call status", "call_status"),
    ("Lifetime calls", "call_count"),
    ("Last called", "_last_called"),
    ("Primary agent", "primary_agent_slug"),
    ("Gender", "gender"),
    ("Notes", "notes"),
    ("Extra details", "details"),
    ("Added", "_added"),
    ("Calls in this export", "calls_here"),
]

INSIGHT_COLUMNS: list[tuple[str, str]] = [
    ("Call ID", "conn_id"),
    ("Date", "_started"),
    ("Contact", "contact_name"),
    ("Phone", "phone"),
    ("Customer intent", "customer_intent"),
    ("Topics", "_topics"),
    ("Product interest", "_product"),
    ("Buying signals", "_buying"),
    ("Objections", "_objections"),
    ("Pain points", "_pain"),
    ("Competitors", "_competitors"),
    ("Budget", "_budget"),
    ("Timeline", "_timeline"),
    ("Decision makers", "_deciders"),
    ("Compliance issues", "_compliance"),
    ("Escalations", "_escalations"),
    ("Risk indicators", "_risks"),
    ("Next actions", "_next_actions"),
    ("Tags", "_tags"),
]

TRANSCRIPT_COLUMNS: list[tuple[str, str]] = [
    ("Call ID", "conn_id"),
    ("Date", "_started"),
    ("Contact", "contact_name"),
    ("Phone", "phone"),
    ("Language", "language"),
    ("Duration (s)", "_duration"),
    ("Summary", "summary"),
    ("Transcript", "_transcript"),
]

# The shared call columns plus two links this workbook can provide and a
# report cannot: a row in a spreadsheet is a dead end unless it can get you
# back to the call it describes.
SELECTION_CALL_COLUMNS: list[tuple[str, str]] = (
    CALL_COLUMNS[:2] + [("Open in LesCall", "_call_url")] + CALL_COLUMNS[2:]
)

EVIDENCE_COLUMNS: list[tuple[str, str]] = [
    ("Call ID", "conn_id"),
    ("Date", "_started"),
    ("Contact", "contact_name"),
    ("Phone", "phone"),
    ("Selected by filter", "_match"),
    ("Confidence", "_match_conf"),
    ("Quoted evidence", "_match_evidence"),
    ("Why", "_match_reason"),
]


def _derive(rows: list[dict], evidence: dict[int, dict] | None) -> None:
    """Columns that only this workbook has (report_export._prepare covers the
    shared ones)."""
    from shared import export_store

    base = export_store.public_base_url()
    evidence = evidence or {}
    for r in rows:
        r["_call_url"] = (f"{base}/intelligence/{r['call_key']}"
                          if r.get("call_key") else "")
        if not r.get("recording_url") and r.get("download_token"):
            # The stored column is populated at call teardown; older rows have
            # only the token, and a blank Recording cell in an export reads as
            # "there is no recording" when there is.
            r["recording_url"] = export_store.public_base_url() + \
                f"/api/recordings/dl/{r['download_token']}"
        r["_deciders"] = _texts(r.get("decision_makers"))
        r["_compliance"] = _texts(r.get("compliance_issues"))
        r["_escalations"] = _texts(r.get("escalations"))
        r["_risks"] = _texts(r.get("risk_indicators"))
        tx = (r.get("transcript") or "").strip()
        r["_transcript"] = (tx[:CELL_MAX] + "\n…[truncated for Excel]"
                            if len(tx) > CELL_MAX else tx)
        ev = evidence.get(r.get("id")) or {}
        r["_match"] = "yes" if ev else ""
        r["_match_conf"] = _float(ev.get("confidence"))
        r["_match_evidence"] = (ev.get("evidence") or "")[:CELL_MAX]
        r["_match_reason"] = (ev.get("reason") or "")[:900]


def _numbers(rows: list[dict]) -> list[dict]:
    """One row per phone. A person who called four times is one line on the
    dialler list, not four — which is the whole reason this sheet exists
    separately from Calls."""
    by_phone: dict[str, dict] = {}
    for r in rows:
        phone = (r.get("phone") or "").strip() or "(unknown)"
        agg = by_phone.setdefault(phone, {
            "phone": phone, "name": r.get("contact_name") or "",
            "calls": 0, "talk_s": 0.0, "first_at": None, "last_at": None,
            "best_lead": None, "sentiment": None, "needs_human": "no",
            "followup": "no", "_directions": set(),
            "_agents": set(), "known": "yes" if r.get("contact_id") else "no",
        })
        if r.get("direction"):
            # A number we both called and that called us is "inbound, outbound"
            # — taking whichever row landed first would hide half the story on
            # exactly the contacts that matter most.
            agg["_directions"].add(r["direction"])
        agg["calls"] += 1
        agg["talk_s"] += _float(r.get("duration_s")) or 0.0
        started = _dt(r.get("started_at"))
        if started:
            if agg["first_at"] is None or started < agg["first_at"]:
                agg["first_at"] = started
            if agg["last_at"] is None or started > agg["last_at"]:
                agg["last_at"] = started
                agg["sentiment"] = r.get("sentiment")
        lead = _int(r.get("lead_score"))
        if lead is not None and (agg["best_lead"] is None or lead > agg["best_lead"]):
            agg["best_lead"] = lead
        if r.get("_needs_human") == "YES":
            agg["needs_human"] = "YES"
        if r.get("agent_slug"):
            agg["_agents"].add(r["agent_slug"])
        if not agg["name"] and r.get("contact_name"):
            agg["name"] = r["contact_name"]
    out = []
    for agg in by_phone.values():
        agg["agents"] = ", ".join(sorted(agg.pop("_agents")))
        agg["direction"] = ", ".join(sorted(agg.pop("_directions")))
        agg["band"] = lead_band(agg["best_lead"])
        agg["talk_s"] = round(agg["talk_s"], 1)
        out.append(agg)
    # Hottest first: this sheet is a work queue, not an archive.
    return sorted(out, key=lambda a: (-(a["best_lead"] or -1), -a["calls"]))


def _contact_rows(rows: list[dict], contacts: dict[int, dict],
                  groups: dict[int, str]) -> list[dict]:
    counts: dict[int, int] = {}
    for r in rows:
        cid = r.get("contact_id")
        if cid:
            counts[cid] = counts.get(cid, 0) + 1
    out = []
    for cid, c in contacts.items():
        c = dict(c)
        c["group_name"] = groups.get(c.get("group_id")) or ""
        c["_last_called"] = _dt(c.get("last_called_at"))
        c["_added"] = _dt(c.get("added_at") or c.get("created_at"))
        c["calls_here"] = counts.get(cid, 0)
        out.append(c)
    return sorted(out, key=lambda c: -(c.get("calls_here") or 0))


def build_workbook(meta: dict, rows: list[dict], commitments: dict[int, list[dict]],
                   contacts: dict[int, dict], groups: dict[int, str],
                   evidence: dict[int, dict] | None = None) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    HEAD_FILL = PatternFill("solid", fgColor=HEAD_BG)
    HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
    BAND_FILL = {BAND_HOT: PatternFill("solid", fgColor="FFE1E1"),
                 BAND_WARM: PatternFill("solid", fgColor="FFF3D6")}
    HUMAN_FILL = PatternFill("solid", fgColor="FDE2E2")

    _prepare(rows, commitments)
    _derive(rows, evidence)

    wb = Workbook()

    def sheet(title: str, data: list[dict], columns: list[tuple[str, str]],
              wide: tuple[str, ...] = (), wrap: tuple[str, ...] = ()):
        ws = wb.create_sheet(title)
        ws.append([h for h, _ in columns])
        for i in range(1, len(columns) + 1):
            c = ws.cell(row=1, column=i)
            c.fill, c.font = HEAD_FILL, HEAD_FONT
            c.alignment = Alignment(vertical="center")
        for r in data:
            ws.append([r.get(k) for _h, k in columns])
            row_i = ws.max_row
            fill = BAND_FILL.get(r.get("_band") or r.get("band"))
            if fill:
                ws.cell(row=row_i, column=1).fill = fill
            for i, (_h, k) in enumerate(columns, start=1):
                cell = ws.cell(row=row_i, column=i)
                if k in ("_started", "first_at", "last_at", "_last_called", "_added"):
                    cell.number_format = "yyyy-mm-dd hh:mm"
                elif k in ("_conversion", "_quality", "_satisfaction",
                           "_confidence", "_match_conf"):
                    cell.number_format = "0.00"
                elif k == "phone":
                    # A phone number is an identifier, not a quantity: leave it
                    # a string so Excel never strips a leading + or reformats
                    # +919876543210 into scientific notation.
                    cell.number_format = "@"
                if k in wrap:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                if k == "needs_human" and r.get(k) == "YES":
                    cell.fill = HUMAN_FILL
                if k == "_needs_human" and r.get(k) == "YES":
                    cell.fill = HUMAN_FILL
        ws.freeze_panes = "A2"
        if data:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{ws.max_row}"
        for i, (h, k) in enumerate(columns, start=1):
            ws.column_dimensions[get_column_letter(i)].width = (
                60 if k in wide else max(11, min(len(h) + 4, 26)))
        return ws

    # 1. Summary — what this file is and which question produced it.
    ws = wb.active
    ws.title = "Summary"
    ws.append(["Field", "Value"])
    for i in (1, 2):
        ws.cell(row=1, column=i).fill = HEAD_FILL
        ws.cell(row=1, column=i).font = HEAD_FONT
    numbers = _numbers(rows)
    hot = [r for r in rows if r["_band"] == BAND_HOT]
    needs = [r for r in rows if r["_needs_human"] == "YES"]
    facts: list[tuple[str, Any]] = [
        ("Export", meta.get("title") or "Call selection"),
        ("Account", meta.get("owner") or ""),
        ("Generated (UTC)", datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
        ("Selected by", meta.get("created_by") or ""),
        ("", ""),
        ("Filter used", meta.get("query") or "(rows picked by hand)"),
    ]
    crit = meta.get("criterion") or {}
    if crit.get("restated"):
        facts.append(("Filter, as the AI read it", crit["restated"]))
    for n, req in enumerate(crit.get("requirements") or [], start=1):
        facts.append((f"  Requirement {n}", req))
    if meta.get("verified"):
        facts.append(("Verification",
                      f"every row was read by {meta.get('verify_model') or 'the filter model'} "
                      "and had to quote its own evidence"))
    facts += [
        ("", ""),
        ("Calls in this export", len(rows)),
        ("Unique phone numbers", len(numbers)),
        ("Known contacts", len(contacts)),
        ("Analysed calls", sum(1 for r in rows if r.get("processed_at"))),
        ("Total talk time (min)",
         round(sum(_float(r.get("duration_s")) or 0.0 for r in rows) / 60.0, 1)),
        ("", ""),
        (f"Hot leads (lead ≥ {HOT_LEAD_MIN})", len(hot)),
        ("Calls needing a human", len(needs)),
        ("Open promises",
         sum(1 for cs in commitments.values() for c in cs if c.get("status") == "open")),
    ]
    for k, v in facts:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 90
    ws.freeze_panes = "A2"
    for row in ws.iter_rows(min_row=2, max_col=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    # 2-7. The tables.
    sheet("Numbers", numbers, NUMBER_COLUMNS)
    sheet("Contacts", _contact_rows(rows, contacts, groups), CONTACT_COLUMNS,
          wide=("notes", "details"), wrap=("notes", "details"))
    sheet("Calls", rows, SELECTION_CALL_COLUMNS,
          wide=("summary", "_intervention", "_next_actions", "_open_promises",
                "customer_intent"),
          wrap=("summary", "_intervention"))
    sheet("Insights", rows, INSIGHT_COLUMNS,
          wide=("_topics", "_buying", "_objections", "_next_actions"),
          wrap=("_topics", "_buying", "_objections", "_next_actions"))
    sheet("Transcripts", rows, TRANSCRIPT_COLUMNS,
          wide=("summary", "_transcript"), wrap=("summary",))
    if evidence:
        sheet("Evidence", [r for r in rows if r.get("_match")], EVIDENCE_COLUMNS,
              wide=("_match_evidence", "_match_reason"),
              wrap=("_match_evidence", "_match_reason"))

    cw = wb.create_sheet("Commitments")
    cols = ["Call ID", "Contact", "Phone", "Made by", "Kind", "Description",
            "Deadline (text)", "Deadline", "Vague", "Clarified", "Status"]
    cw.append(cols)
    for i in range(1, len(cols) + 1):
        cw.cell(row=1, column=i).fill = HEAD_FILL
        cw.cell(row=1, column=i).font = HEAD_FONT
    for r in rows:
        for c in r.get("_commitments") or []:
            cw.append([r.get("conn_id"), r.get("contact_name"), r.get("phone"),
                       c.get("made_by"), c.get("kind"), c.get("description"),
                       c.get("deadline_text"), _dt(c.get("deadline_at")),
                       "yes" if c.get("is_vague") else "no",
                       "yes" if c.get("clarified") else "no", c.get("status")])
    cw.freeze_panes = "A2"
    if cw.max_row > 1:
        cw.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{cw.max_row}"
    for i, h in enumerate(cols, start=1):
        cw.column_dimensions[get_column_letter(i)].width = (
            50 if h == "Description" else max(12, len(h) + 4))

    lw = wb.create_sheet("Legend")
    lw.append(["Sheet / field", "What it is"])
    for i in (1, 2):
        lw.cell(row=1, column=i).fill = HEAD_FILL
        lw.cell(row=1, column=i).font = HEAD_FONT
    for k, v in [
        ("Numbers", "One row per phone number — the de-duplicated call list, "
                    "hottest lead first. Paste this into a dialler."),
        ("Contacts", "The contact record behind those numbers, for the ones "
                     "already in the phone book."),
        ("Calls", "One row per call, with every score the intelligence layer "
                  "produced."),
        ("Insights", "What was extracted from each call: topics, objections, "
                     "buying signals, budget, next actions."),
        ("Transcripts", "The words. Truncated at 32,000 characters — Excel's "
                        "per-cell ceiling, not ours."),
        ("Evidence", "Present only when this selection came from an AI filter: "
                     "the verbatim line from each call that made it qualify."),
        ("Commitments", "Every promise made on these calls and whether it is "
                        "still open."),
        ("Open in LesCall", "Link straight to that call's workspace — "
                            "transcript, recording and analysis."),
        ("Lead band", f"Hot ≥ {HOT_LEAD_MIN}, Warm {WARM_LEAD_MIN}–{HOT_LEAD_MIN - 1}, "
                      f"Cold < {WARM_LEAD_MIN}, Unscored = not analysed."),
        ("Needs human", f"YES when any of: risk ≥ {RISK_MIN}; negative sentiment; "
                        "high/critical urgency; escalation; compliance issue; an "
                        "open promise we made; a hot lead with no next step."),
        ("Gender (Contacts sheet)", "Optional, and blank for most contacts. "
                                    "Filled from a Gender or Sex column in the "
                                    "sheet the contacts were uploaded from. An "
                                    "agent whose opening line uses {honorific} "
                                    "addresses these people as Sir or Ma'am; "
                                    "everyone else is greeted without a title."),
        ("Answer state", "Empty = connected but no real conversation (silence, "
                         "noise, voicemail, or under 10s)."),
        ("Confidence (Evidence sheet)", "How sure the filter model was that this "
                                        "call satisfies the filter. Rows below the "
                                        "floor were dropped, not listed."),
    ]:
        lw.append([k, v])
    lw.column_dimensions["A"].width = 28
    lw.column_dimensions["B"].width = 96
    for row in lw.iter_rows(min_row=2, max_col=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def build_for_selection(db, owner: str, call_ids: list[int], meta: dict,
                        evidence: dict[int, dict] | None = None) -> tuple[bytes, dict]:
    """Load everything the workbook needs and build it. Returns (bytes, stats)."""
    rows = load_calls(db, owner, call_ids)
    commitments = load_commitments(db, owner, [r["id"] for r in rows if r.get("id")])
    contact_ids = [r["contact_id"] for r in rows if r.get("contact_id")]
    contacts = load_contacts(db, owner, contact_ids)
    groups = load_groups(db, owner) if contacts else {}
    blob = build_workbook({**meta, "owner": owner}, rows, commitments,
                          contacts, groups, evidence)
    phones = {(r.get("phone") or "").strip() for r in rows}
    return blob, {"call_count": len(rows), "contact_count": len(contacts),
                  "number_count": len(phones - {""}),
                  "call_ids": [r["id"] for r in rows]}
