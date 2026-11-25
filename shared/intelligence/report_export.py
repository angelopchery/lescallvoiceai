"""Report exports: a branded .docx document and an analysis-ready .xlsx workbook.

Both are built from a saved report row (`reports.call_ids` selects the calls)
joined to the intelligence layer, so they always agree with what the Reports
page shows.

The .docx carries the LesCall logo as a true page watermark (a floating VML
shape in the header, sitting behind the body text on every page) plus a small
letterhead logo — a header-only image would be a letterhead, not a watermark.

The .xlsx is deliberately boring where it matters: one header row, no merged
cells in data sheets, real numbers and real dates (never strings that look like
them), stable column order, autofilter + frozen panes. That is what "ready for
analysis" means — you can pivot it without cleaning it first.
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
from datetime import date, datetime
from typing import Any

log = logging.getLogger("report_export")

LOGO_PATH = os.environ.get(
    "LESCALL_LOGO_PATH", "/home/angelo/lescall/frontend/public/logo.png")

# --- lead banding ---------------------------------------------------------
# Thresholds are env-tunable but the defaults are read off the live corpus:
# across 290 analysed calls lead_score has p50=0, p75=10, p90=65, max=90, so
# 70 isolates a genuine shortlist (~10%) rather than "everything above zero".
HOT_LEAD_MIN = int(os.environ.get("REPORT_HOT_LEAD_MIN", "70"))
WARM_LEAD_MIN = int(os.environ.get("REPORT_WARM_LEAD_MIN", "40"))
# Human-intervention triggers.
RISK_MIN = int(os.environ.get("REPORT_RISK_MIN", "60"))

BAND_HOT, BAND_WARM, BAND_COLD = "Hot", "Warm", "Cold"


def lead_band(score: Any) -> str:
    s = _int(score)
    if s is None:
        return "Unscored"
    if s >= HOT_LEAD_MIN:
        return BAND_HOT
    if s >= WARM_LEAD_MIN:
        return BAND_WARM
    return BAND_COLD


def _int(v) -> int | None:
    try:
        return int(v) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        return None


def _float(v) -> float | None:
    try:
        return float(v) if v is not None and str(v) != "" else None
    except (TypeError, ValueError):
        return None


def _jlist(raw) -> list:
    """Parse one of the JSON list columns; never raise on bad data."""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _jdict(raw) -> dict:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _texts(raw, limit: int = 6) -> str:
    """JSON list of {text}|str -> "a; b; c" for a spreadsheet cell."""
    out = []
    for item in _jlist(raw)[:limit]:
        if isinstance(item, dict):
            t = item.get("text") or item.get("label") or item.get("name")
        else:
            t = item
        if t:
            out.append(str(t).replace("\n", " ").strip())
    return "; ".join(out)


def _dt(v):
    """Return a real datetime for Excel, or None. Postgres hands timestamps
    back as strings through this driver, so parse defensively."""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.replace(tzinfo=None)
    if isinstance(v, date):
        return v
    s = str(v).strip().replace("Z", "").replace("T", " ")
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- data load

_CALL_COLUMNS = """
    c.id, c.conn_id, c.call_key, c.direction, c.phone, c.started_at, c.ended_at,
    c.duration_s, c.language, c.agent_slug, c.outcome, c.outcome_kind,
    c.answer_state, c.answer_state_reason, c.recording_url, c.contact_id,
    ct.name AS contact_name,
    i.summary, i.outcome_summary, i.customer_intent, i.conversation_quality,
    i.conversion_probability, i.satisfaction, i.urgency, i.confidence,
    i.low_confidence, i.lead_score, i.opportunity_score, i.risk_score,
    i.compliance_score, i.sentiment, i.topics, i.pain_points, i.objections,
    i.buying_signals, i.competitor_mentions, i.product_interest,
    i.budget_discussion, i.timeline_discussion, i.decision_makers,
    i.compliance_issues, i.escalations, i.risk_indicators, i.recommendations,
    i.entities, i.tags, i.processed_at
"""


def load_report_rows(db, report: dict, owner: str) -> list[dict]:
    """Every call in the report, joined to its intelligence record.

    Falls back to the report's own date window when call_ids is empty, so a
    report saved before call_ids was populated still exports something useful
    rather than an empty workbook.
    """
    call_ids = _jlist(report.get("call_ids"))
    ids = [i for i in (_int(x) for x in call_ids) if i is not None]
    if ids:
        placeholders = ",".join("?" for _ in ids)
        sql = (f"SELECT {_CALL_COLUMNS} FROM calls c "
               " LEFT JOIN call_intelligence i ON i.call_id = c.id "
               " LEFT JOIN contacts ct ON ct.id = c.contact_id "
               f" WHERE c.owner = ? AND c.id IN ({placeholders}) "
               " ORDER BY c.started_at DESC")
        rows = db.execute(sql, (owner, *ids)).fetchall()
    else:
        sql = (f"SELECT {_CALL_COLUMNS} FROM calls c "
               " LEFT JOIN call_intelligence i ON i.call_id = c.id "
               " LEFT JOIN contacts ct ON ct.id = c.contact_id "
               " WHERE c.owner = ? ORDER BY c.started_at DESC LIMIT 1000")
        rows = db.execute(sql, (owner,)).fetchall()
    return [dict(r) for r in rows]


def load_commitments(db, owner: str, call_ids: list[int]) -> dict[int, list[dict]]:
    if not call_ids:
        return {}
    placeholders = ",".join("?" for _ in call_ids)
    rows = db.execute(
        "SELECT call_id, made_by, kind, description, deadline_text, deadline_at, "
        "       is_vague, clarified, status "
        f"  FROM commitments WHERE owner = ? AND call_id IN ({placeholders}) "
        "  ORDER BY call_id", (owner, *call_ids)).fetchall()
    out: dict[int, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d["call_id"], []).append(d)
    return out


# ------------------------------------------------------- intervention rules

def intervention_reasons(row: dict, commitments: list[dict]) -> list[str]:
    """Why a human should look at this call. Empty list = leave it to the agent.

    Every rule is evidence already produced by the intelligence layer — nothing
    here re-interprets the transcript.
    """
    reasons: list[str] = []
    risk = _int(row.get("risk_score"))
    if risk is not None and risk >= RISK_MIN:
        reasons.append(f"risk {risk}")
    if (row.get("sentiment") or "").lower() == "negative":
        reasons.append("negative sentiment")
    if (row.get("urgency") or "").lower() in ("high", "critical"):
        reasons.append(f"{row.get('urgency')} urgency")
    if _jlist(row.get("escalations")):
        reasons.append("escalation raised")
    if _jlist(row.get("compliance_issues")):
        reasons.append("compliance issue")
    # A promise WE made that is still open — the customer is waiting on us.
    open_agent = [c for c in commitments
                  if c.get("status") == "open" and c.get("made_by") == "agent"]
    if open_agent:
        vague = [c for c in open_agent if c.get("is_vague") and not c.get("clarified")]
        reasons.append(f"{len(open_agent)} open promise(s)"
                       + (f", {len(vague)} vague" if vague else ""))
    # A hot lead nobody has followed up is exactly what gets lost.
    if lead_band(row.get("lead_score")) == BAND_HOT and not commitments:
        reasons.append("hot lead, no next step recorded")
    # Deliberately NOT a trigger: low_confidence says our extraction was unsure,
    # not that the customer needs attention. It fired on 55% of the live corpus
    # and drowned the real signals, so it ships as its own column instead.
    return reasons


# ------------------------------------------------------------------- .docx

def _watermark_xml(rel_id: str, width_pt: float, height_pt: float) -> str:
    """VML watermark: floating, centred, behind the text, on every page.

    python-docx has no watermark API — an inline header image would sit at the
    top of the page and push the body down. This is the shape Word itself
    writes for Insert > Watermark.
    """
    return (
        '<w:p xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:pPr><w:jc w:val="center"/></w:pPr>'
        '<w:r>'
        '<w:pict xmlns:v="urn:schemas-microsoft-com:vml" '
        'xmlns:o="urn:schemas-microsoft-com:office:office" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<v:shape id="lescall_watermark" o:spid="_x0000_s2049" type="#_x0000_t75" '
        f'style="position:absolute;margin-left:0;margin-top:0;width:{width_pt}pt;'
        f'height:{height_pt}pt;z-index:-251658752;mso-position-horizontal:center;'
        'mso-position-horizontal-relative:margin;mso-position-vertical:center;'
        'mso-position-vertical-relative:margin" o:allowincell="f">'
        f'<v:imagedata r:id="{rel_id}" o:title="LesCall" gain="19661f" blacklevel="22938f"/>'
        '</v:shape>'
        '</w:pict>'
        '</w:r>'
        '</w:p>'
    )


def _faded_logo(alpha: float = 0.16) -> io.BytesIO | None:
    """Pre-fade the logo so the watermark is legible under body text even in
    viewers that ignore VML's gain/blacklevel washout."""
    try:
        from PIL import Image
        im = Image.open(LOGO_PATH).convert("RGBA")
        r, g, b, a = im.split()
        a = a.point(lambda v: int(v * alpha))
        im.putalpha(a)
        # Flatten onto white: Word renders RGBA PNGs inconsistently.
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        flat = Image.alpha_composite(bg, im).convert("RGB")
        buf = io.BytesIO()
        flat.save(buf, format="PNG")
        buf.seek(0)
        return buf
    except Exception as exc:
        log.warning("watermark logo unavailable: %r", exc)
        return None


_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")


def _add_markdown_runs(par, text: str) -> None:
    """Render **bold** spans; everything else is literal. The report documents
    are LLM-written markdown, and bold is the only inline mark they use."""
    pos = 0
    for m in _MD_BOLD.finditer(text):
        if m.start() > pos:
            par.add_run(text[pos:m.start()])
        par.add_run(m.group(1)).bold = True
        pos = m.end()
    if pos < len(text):
        par.add_run(text[pos:])


def build_docx(report: dict, rows: list[dict], owner: str) -> bytes:
    """The report's markdown as a branded Word document."""
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()

    # Watermark + letterhead in the header (repeats on every page).
    section = doc.sections[0]
    header = section.header
    logo_buf = _faded_logo()
    if logo_buf is not None:
        try:
            wm_par = header.paragraphs[0]
            run = wm_par.add_run()
            run.add_picture(logo_buf, width=Inches(5.0))
            # Convert the inline picture into the floating VML watermark.
            drawing = run._r.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}drawing")
            if drawing is not None:
                blip = drawing.find(
                    ".//{http://schemas.openxmlformats.org/drawingml/2006/main}blip")
                rel_id = blip.get(
                    "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
                ) if blip is not None else None
                run._r.remove(drawing)
                if rel_id:
                    from docx.oxml import parse_xml
                    wm_par._p.addnext(parse_xml(_watermark_xml(rel_id, 360.0, 132.0)))
        except Exception as exc:
            log.warning("watermark embed failed, continuing without: %r", exc)

    title = doc.add_heading(str(report.get("title") or "Call report"), level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.LEFT

    meta = doc.add_paragraph()
    meta_run = meta.add_run(
        f"{len(rows)} call(s)  ·  generated {datetime.utcnow():%Y-%m-%d %H:%M} UTC  ·  {owner}")
    meta_run.font.size = Pt(9)
    meta_run.font.color.rgb = RGBColor(0x6F, 0x71, 0x80)
    if report.get("query_prompt"):
        q = doc.add_paragraph()
        qr = q.add_run(f"Query: {report['query_prompt']}")
        qr.italic = True
        qr.font.size = Pt(9)
        qr.font.color.rgb = RGBColor(0x6F, 0x71, 0x80)

    # Headline numbers, so the document stands alone without the workbook.
    hot = [r for r in rows if lead_band(r.get("lead_score")) == BAND_HOT]
    scored = [s for s in (_int(r.get("lead_score")) for r in rows) if s is not None]
    doc.add_heading("At a glance", level=1)
    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    for label, value in (
        ("Calls in report", str(len(rows))),
        ("Hot leads (score ≥ %d)" % HOT_LEAD_MIN, str(len(hot))),
        ("Average lead score", str(round(sum(scored) / len(scored), 1)) if scored else "—"),
        ("Calls needing a human", str(sum(1 for r in rows if r.get("_intervention")))),
    ):
        cells = tbl.add_row().cells
        cells[0].text = label
        cells[1].text = value

    # The saved markdown body.
    doc.add_heading("Report", level=1)
    for raw_line in str(report.get("document") or "").splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 4)
            doc.add_heading(line.lstrip("#").strip(), level=max(level, 1))
        elif line.lstrip().startswith(("- ", "* ")):
            p = doc.add_paragraph(style="List Bullet")
            _add_markdown_runs(p, line.lstrip()[2:])
        elif re.match(r"^\s*\d+\.\s", line):
            p = doc.add_paragraph(style="List Number")
            _add_markdown_runs(p, re.sub(r"^\s*\d+\.\s", "", line))
        elif set(line.strip()) <= {"-", "|", ":"} and "|" in line:
            continue  # markdown table rule
        else:
            _add_markdown_runs(doc.add_paragraph(), line)

    if hot:
        doc.add_heading("Hot leads", level=1)
        t = doc.add_table(rows=1, cols=4)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(("Contact", "Lead", "Intent", "When")):
            t.rows[0].cells[i].text = h
        for r in sorted(hot, key=lambda x: _int(x.get("lead_score")) or 0, reverse=True)[:25]:
            c = t.add_row().cells
            c[0].text = f"{r.get('contact_name') or r.get('phone') or '—'}"
            c[1].text = str(_int(r.get("lead_score")) or "—")
            c[2].text = (r.get("customer_intent") or "")[:120]
            d = _dt(r.get("started_at"))
            c[3].text = d.strftime("%Y-%m-%d %H:%M") if d else "—"

    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ------------------------------------------------------------------- .xlsx

# (header, key) in a fixed order — analysis tools care about stable columns.
CALL_COLUMNS: list[tuple[str, str]] = [
    ("Call ID", "conn_id"),
    ("Date", "_started"),
    ("Contact", "contact_name"),
    ("Phone", "phone"),
    ("Direction", "direction"),
    ("Duration (s)", "_duration"),
    ("Answer state", "_answer_state"),
    ("Lead band", "_band"),
    ("Lead score", "lead_score"),
    ("Opportunity score", "opportunity_score"),
    ("Risk score", "risk_score"),
    ("Compliance score", "compliance_score"),
    ("Conversion probability", "_conversion"),
    ("Conversation quality", "_quality"),
    ("Satisfaction", "_satisfaction"),
    ("Sentiment", "sentiment"),
    ("Urgency", "urgency"),
    ("Needs human", "_needs_human"),
    ("Why (human)", "_intervention"),
    ("Customer intent", "customer_intent"),
    ("Summary", "summary"),
    ("Outcome", "outcome"),
    ("Outcome kind", "outcome_kind"),
    ("Product interest", "_product"),
    ("Buying signals", "_buying"),
    ("Objections", "_objections"),
    ("Pain points", "_pain"),
    ("Competitors", "_competitors"),
    ("Budget", "_budget"),
    ("Timeline", "_timeline"),
    ("Topics", "_topics"),
    ("Tags", "_tags"),
    ("Open promises", "_open_promises"),
    ("Next actions", "_next_actions"),
    ("Language", "language"),
    ("Agent", "agent_slug"),
    ("Analysis confidence", "_confidence"),
    ("Low confidence", "_low_confidence"),
    ("Recording", "recording_url"),
]


def _prepare(rows: list[dict], commitments: dict[int, list[dict]]) -> list[dict]:
    """Derive every computed cell once, so the sheets agree with each other."""
    for r in rows:
        cs = commitments.get(r.get("id"), [])
        reasons = intervention_reasons(r, cs)
        r["_intervention"] = "; ".join(reasons)
        r["_needs_human"] = "YES" if reasons else "no"
        r["_band"] = lead_band(r.get("lead_score"))
        r["_started"] = _dt(r.get("started_at"))
        r["_duration"] = _float(r.get("duration_s"))
        r["_conversion"] = _float(r.get("conversion_probability"))
        r["_quality"] = _float(r.get("conversation_quality"))
        r["_satisfaction"] = _float(r.get("satisfaction"))
        r["_confidence"] = _float(r.get("confidence"))
        r["_low_confidence"] = "yes" if _int(r.get("low_confidence")) else "no"
        state = (r.get("answer_state") or "")
        r["_answer_state"] = ("Empty (%s)" % (r.get("answer_state_reason") or "?")
                              if state == "empty" else ("Answered" if state else "—"))
        r["_product"] = _texts(r.get("product_interest"))
        r["_buying"] = _texts(r.get("buying_signals"))
        r["_objections"] = _texts(r.get("objections"))
        r["_pain"] = _texts(r.get("pain_points"))
        r["_competitors"] = _texts(r.get("competitor_mentions"))
        r["_budget"] = _texts(r.get("budget_discussion"), 3)
        r["_timeline"] = _texts(r.get("timeline_discussion"), 3)
        r["_topics"] = _texts(r.get("topics"))
        r["_tags"] = _texts(r.get("tags"), 10)
        rec = _jdict(r.get("recommendations"))
        r["_next_actions"] = "; ".join(
            str(a.get("text") if isinstance(a, dict) else a)
            for a in (rec.get("next_actions") or [])[:5])
        r["_open_promises"] = "; ".join(
            f"{c.get('kind')}: {c.get('description')}"
            for c in cs if c.get("status") == "open")[:900]
        r["_commitments"] = cs
    return rows


def build_xlsx(report: dict, rows: list[dict], owner: str) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    HEAD_FILL = PatternFill("solid", fgColor="0F172A")
    HEAD_FONT = Font(color="FFFFFF", bold=True, size=10)
    BAND_FILL = {
        BAND_HOT: PatternFill("solid", fgColor="FFE1E1"),
        BAND_WARM: PatternFill("solid", fgColor="FFF3D6"),
    }
    HUMAN_FILL = PatternFill("solid", fgColor="FDE2E2")

    wb = Workbook()

    def write_sheet(ws, data: list[dict], columns: list[tuple[str, str]],
                    band_colour: bool = True):
        ws.append([h for h, _ in columns])
        for i, (_h, _k) in enumerate(columns, start=1):
            c = ws.cell(row=1, column=i)
            c.fill, c.font = HEAD_FILL, HEAD_FONT
            c.alignment = Alignment(vertical="center")
        for r in data:
            ws.append([r.get(k) for _h, k in columns])
            row_i = ws.max_row
            if band_colour:
                fill = BAND_FILL.get(r.get("_band"))
                if fill:
                    ws.cell(row=row_i, column=1).fill = fill
                if r.get("_needs_human") == "YES":
                    idx = next((i for i, (_h, k) in enumerate(columns, start=1)
                                if k == "_needs_human"), None)
                    if idx:
                        ws.cell(row=row_i, column=idx).fill = HUMAN_FILL
            for i, (_h, k) in enumerate(columns, start=1):
                if k == "_started":
                    ws.cell(row=row_i, column=i).number_format = "yyyy-mm-dd hh:mm"
                elif k in ("_conversion", "_quality", "_satisfaction", "_confidence"):
                    ws.cell(row=row_i, column=i).number_format = "0.00"
        # One header row, filterable, frozen — pivot-ready as-is.
        ws.freeze_panes = "A2"
        if data:
            ws.auto_filter.ref = f"A1:{get_column_letter(len(columns))}{ws.max_row}"
        for i, (h, k) in enumerate(columns, start=1):
            width = 34 if k in ("summary", "_intervention", "_next_actions",
                                "_open_promises", "customer_intent") else max(11, min(len(h) + 4, 24))
            ws.column_dimensions[get_column_letter(i)].width = width

    # 1. Summary
    ws = wb.active
    ws.title = "Summary"
    hot = [r for r in rows if r["_band"] == BAND_HOT]
    warm = [r for r in rows if r["_band"] == BAND_WARM]
    needs = [r for r in rows if r["_needs_human"] == "YES"]
    scored = [s for s in (_int(r.get("lead_score")) for r in rows) if s is not None]
    empties = [r for r in rows if (r.get("answer_state") or "") == "empty"]
    facts = [
        ("Report", report.get("title") or ""),
        ("Owner", owner),
        ("Generated (UTC)", datetime.utcnow().strftime("%Y-%m-%d %H:%M")),
        ("Query", report.get("query_prompt") or ""),
        ("", ""),
        ("Calls in report", len(rows)),
        ("Analysed calls", sum(1 for r in rows if r.get("processed_at"))),
        ("Empty calls (no real conversation)", len(empties)),
        ("", ""),
        (f"Hot leads (lead ≥ {HOT_LEAD_MIN})", len(hot)),
        (f"Warm leads ({WARM_LEAD_MIN}–{HOT_LEAD_MIN - 1})", len(warm)),
        ("Cold / unscored", len(rows) - len(hot) - len(warm)),
        ("Average lead score", round(sum(scored) / len(scored), 1) if scored else 0),
        ("", ""),
        ("Calls needing a human", len(needs)),
        (f"  • risk ≥ {RISK_MIN}", sum(1 for r in needs if "risk" in r["_intervention"])),
        ("  • negative sentiment", sum(1 for r in needs if "negative" in r["_intervention"])),
        ("  • open promises", sum(1 for r in needs if "promise" in r["_intervention"])),
        ("  • hot lead, no next step", sum(1 for r in needs if "no next step" in r["_intervention"])),
    ]
    ws.append(["Metric", "Value"])
    for i in (1, 2):
        ws.cell(row=1, column=i).fill = HEAD_FILL
        ws.cell(row=1, column=i).font = HEAD_FONT
    for k, v in facts:
        ws.append([k, v])
    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 46
    ws.freeze_panes = "A2"

    # 2. All calls
    write_sheet(wb.create_sheet("Calls"), rows, CALL_COLUMNS)

    # 3. Hot leads, best first
    write_sheet(wb.create_sheet("Hot Leads"),
                sorted(hot, key=lambda r: _int(r.get("lead_score")) or 0, reverse=True),
                CALL_COLUMNS)

    # 4. Needs a human, most at-risk first
    write_sheet(wb.create_sheet("Needs Human"),
                sorted(needs, key=lambda r: _int(r.get("risk_score")) or 0, reverse=True),
                CALL_COLUMNS)

    # 5. Every promise, so nothing silently expires
    cw = wb.create_sheet("Commitments")
    cols = ["Call ID", "Contact", "Phone", "Made by", "Kind", "Description",
            "Deadline (text)", "Deadline", "Vague", "Clarified", "Status"]
    cw.append(cols)
    for i in range(1, len(cols) + 1):
        cw.cell(row=1, column=i).fill = HEAD_FILL
        cw.cell(row=1, column=i).font = HEAD_FONT
    for r in rows:
        for c in r.get("_commitments") or []:
            cw.append([
                r.get("conn_id"), r.get("contact_name"), r.get("phone"),
                c.get("made_by"), c.get("kind"), c.get("description"),
                c.get("deadline_text"), _dt(c.get("deadline_at")),
                "yes" if c.get("is_vague") else "no",
                "yes" if c.get("clarified") else "no", c.get("status"),
            ])
    cw.freeze_panes = "A2"
    if cw.max_row > 1:
        cw.auto_filter.ref = f"A1:{get_column_letter(len(cols))}{cw.max_row}"
    for i, h in enumerate(cols, start=1):
        cw.column_dimensions[get_column_letter(i)].width = 40 if h == "Description" else max(12, len(h) + 4)

    # 6. What the bands mean — a workbook that explains itself.
    lw = wb.create_sheet("Legend")
    lw.append(["Field", "Meaning"])
    for i in (1, 2):
        lw.cell(row=1, column=i).fill = HEAD_FILL
        lw.cell(row=1, column=i).font = HEAD_FONT
    for k, v in [
        ("Lead band", f"Hot ≥ {HOT_LEAD_MIN}, Warm {WARM_LEAD_MIN}–{HOT_LEAD_MIN - 1}, "
                      f"Cold < {WARM_LEAD_MIN}, Unscored = call not analysed"),
        ("Lead score", "0-100, how likely this contact is to buy (intelligence layer)"),
        ("Opportunity score", "0-100, size/quality of the opportunity"),
        ("Risk score", "0-100, risk of losing them or a problem on the account"),
        ("Answer state", "Empty = connected but no real conversation (silence, "
                         "noise, voicemail, or under 10s)"),
        ("Needs human", f"YES when any of: risk ≥ {RISK_MIN}; negative sentiment; "
                        "high/critical urgency; escalation; compliance issue; an open "
                        "promise we made; a hot lead with no next step"),
        ("Low confidence", "The analysis itself was uncertain. Advisory only — it does "
                           "NOT set 'Needs human', because it describes our extraction "
                           "rather than the customer"),
        ("Why (human)", "The specific rules that fired, semicolon separated"),
        ("Conversion probability", "0-1 from the intelligence layer"),
        ("Analysis confidence", "0-1; low values mean treat the row's analysis with care"),
    ]:
        lw.append([k, v])
    lw.column_dimensions["A"].width = 24
    lw.column_dimensions["B"].width = 96
    for row in lw.iter_rows(min_row=2, max_col=2):
        row[1].alignment = Alignment(wrap_text=True, vertical="top")

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def safe_filename(title: str, ext: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", str(title or "report")).strip("-").lower()
    return f"{slug or 'report'}.{ext}"


# --- plain-text family ----------------------------------------------------
# The synthesized document is already markdown, so .md is the source itself
# with a provenance header — no re-rendering, nothing to drift. .txt is the
# same content with the markup taken off, for pasting into a mail client or an
# CRM note field that would otherwise show literal asterisks.

def _front_matter(report: dict, rows: list[dict], owner: str) -> list[str]:
    head = [str(report.get("title") or "Call report"),
            f"{len(rows)} call(s) · generated "
            f"{datetime.utcnow():%Y-%m-%d %H:%M} UTC · {owner}"]
    if report.get("query_prompt"):
        head.append(f"Query: {report['query_prompt']}")
    return head


def build_md(report: dict, rows: list[dict], owner: str) -> bytes:
    title, meta, *rest = _front_matter(report, rows, owner)
    parts = [f"# {title}", "", f"_{meta}_"]
    if rest:
        parts += ["", f"_{rest[0]}_"]
    parts += ["", "---", "", str(report.get("document") or "").strip(), ""]
    return "\n".join(parts).encode("utf-8")


def _strip_markdown(md: str) -> str:
    """Markdown → readable plain text.

    Headings keep their text and gain an underline, tables become aligned
    columns, list bullets normalise to '- '. Deliberately small: the input is
    our own generator's output, not arbitrary markdown.
    """
    out: list[str] = []
    table: list[list[str]] = []

    def flush_table() -> None:
        if not table:
            return
        widths = [max(len(r[i]) if i < len(r) else 0 for r in table)
                  for i in range(max(len(r) for r in table))]
        for ri, row in enumerate(table):
            out.append("  ".join(
                (row[i] if i < len(row) else "").ljust(widths[i])
                for i in range(len(widths))).rstrip())
            if ri == 0:
                out.append("  ".join("-" * w for w in widths).rstrip())
        out.append("")
        table.clear()

    for raw in str(md or "").splitlines():
        line = raw.rstrip()
        if re.match(r"^\s*\|.*\|\s*$", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not all(re.fullmatch(r":?-{2,}:?", c or "-") for c in cells):
                table.append([_inline_plain(c) for c in cells])
            continue
        flush_table()
        h = re.match(r"^(#{1,6})\s+(.*)$", line)
        if h:
            text = _inline_plain(h[2])
            out += ["", text, ("=" if len(h[1]) == 1 else "-") * len(text)]
            continue
        if re.fullmatch(r"\s*-{3,}\s*", line):
            out.append("-" * 60)
            continue
        li = re.match(r"^(\s*)[-*]\s+(.*)$", line)
        if li:
            out.append(f"{li[1]}- {_inline_plain(li[2])}")
            continue
        out.append(_inline_plain(line))
    flush_table()
    # Collapse the runs of blank lines the heading rule introduces.
    text, blank = [], False
    for line in out:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        text.append(line)
    return "\n".join(text).strip() + "\n"


def _inline_plain(s: str) -> str:
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    return re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)


def build_txt(report: dict, rows: list[dict], owner: str) -> bytes:
    head = _front_matter(report, rows, owner)
    banner = [head[0], "=" * len(head[0])] + head[1:] + ["", "-" * 60, ""]
    body = _strip_markdown(report.get("document") or "")
    return ("\n".join(banner) + body).encode("utf-8")
