"""
Extract plain text from uploaded documents.

Supports: PDF (pdfplumber text layer, with Gemini-vision OCR fallback for
scanned pages), DOCX (paragraphs AND tables, in document order), XLSX
(every sheet, row by row), images (Gemini-vision OCR), and plain-text
formats (TXT / MD / CSV / TSV / JSON / YAML).

`SUPPORTED` is the one list the upload endpoints, the Copilot attach button
and the Agent Builder's file pickers all read, so what the UI offers and what
the server can read never disagree again — the Copilot used to accept `.doc`,
which python-docx cannot open, and the Builder refused `.md` and `.csv`, which
the server read fine. `unsupported_reason()` gives the sentence the user sees
for the rest: what is wrong and what to do instead.

OCR strategy (production): the embedded text layer is always tried first —
it is free, instant and exact. A PDF is treated as scanned when it yields
almost no text per page; only then is the raw file sent to Gemini
(multimodal) for a verbatim transcription. No system OCR deps (tesseract,
poppler) are required, which keeps the container image unchanged.
"""
from __future__ import annotations

import base64
import io
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_IMAGE_EXTS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".webp": "image/webp", ".gif": "image/gif"}
_TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml"}
_SHEET_EXTS = {".xlsx", ".xlsm"}

# extension -> human label. Order is what the UI lists.
SUPPORTED: dict[str, str] = {
    ".pdf": "PDF",
    ".docx": "Word (.docx)",
    ".xlsx": "Excel (.xlsx)",
    ".xlsm": "Excel (.xlsm)",
    ".txt": "plain text",
    ".md": "Markdown",
    ".markdown": "Markdown",
    ".csv": "CSV",
    ".tsv": "TSV",
    ".json": "JSON",
    ".yaml": "YAML",
    ".yml": "YAML",
    ".png": "PNG image",
    ".jpg": "JPEG image",
    ".jpeg": "JPEG image",
    ".webp": "WebP image",
    ".gif": "GIF image",
}
# The `accept` attribute for a file input; the human list for a sentence.
ACCEPT = ",".join(SUPPORTED)
SUPPORTED_HUMAN = "PDF, Word (.docx), Excel (.xlsx), TXT, Markdown, CSV or an image (PNG/JPG/WebP)"

# Types people upload and we cannot read — each with the way out.
_REJECTED: dict[str, str] = {
    ".doc": ("Legacy Word (.doc) files can't be read — open it in Word and "
             "save as .docx or PDF, then upload again."),
    ".xls": "Legacy Excel (.xls) isn't supported — save it as .xlsx or CSV and upload again.",
    ".pptx": "PowerPoint isn't supported yet — export the deck as PDF and upload that.",
    ".ppt": "PowerPoint isn't supported yet — export the deck as PDF and upload that.",
    ".pages": "Apple Pages files can't be read — export as PDF or .docx and upload again.",
    ".numbers": "Apple Numbers files can't be read — export as .xlsx or CSV and upload again.",
    ".zip": "Archives aren't supported — upload the documents inside it one at a time.",
    ".rar": "Archives aren't supported — upload the documents inside it one at a time.",
    ".7z": "Archives aren't supported — upload the documents inside it one at a time.",
}

# Below this many chars per page on average, a PDF is considered scanned.
_SCANNED_PDF_CHARS_PER_PAGE = 200
# Gemini inline_data caps the whole request at ~20 MB; leave headroom.
_OCR_MAX_BYTES = 14 * 1024 * 1024


def is_supported(filename: str) -> bool:
    return Path(filename or "").suffix.lower() in SUPPORTED


def unsupported_reason(filename: str) -> str:
    """"" when the file can be read; otherwise the sentence to show the user."""
    ext = Path(filename or "").suffix.lower()
    if ext in SUPPORTED:
        return ""
    if ext in _REJECTED:
        return _REJECTED[ext]
    shown = f"'{ext}' files aren't" if ext else "Files without an extension aren't"
    return f"{shown} supported — upload {SUPPORTED_HUMAN}."


def extract_text(content: bytes, filename: str) -> str:
    """Return plain text from raw file bytes. filename used only for extension detection."""
    reason = unsupported_reason(filename)
    if reason:
        raise ValueError(reason)
    ext = Path(filename).suffix.lower()
    try:
        if ext == ".pdf":
            return _extract_pdf(content)
        elif ext == ".docx":
            return _extract_docx(content)
        elif ext in _SHEET_EXTS:
            return _extract_xlsx(content)
        elif ext in _IMAGE_EXTS:
            return _ocr_via_gemini(content, _IMAGE_EXTS[ext])
        else:
            return _decode_text(content, filename)
    except ValueError:
        raise
    except Exception as exc:
        logger.error("[docs/extract] Failed to extract %s: %s", filename, exc)
        raise ValueError(f"Could not extract text from {filename}: {exc}") from exc


def _decode_text(content: bytes, filename: str) -> str:
    """Plain-text formats. A BOM decides the encoding when there is one; a
    file full of NUL bytes is a binary somebody renamed, and says so."""
    if content.startswith((b"\xff\xfe", b"\xfe\xff")):
        return content.decode("utf-16", errors="replace").strip()
    if b"\x00" in content[:4096]:
        raise ValueError(
            f"{filename} doesn't look like a text file — if it is a Word or "
            "PDF document, upload it with its real extension.")
    return content.decode("utf-8-sig", errors="replace").strip()


def _extract_pdf(content: bytes) -> str:
    import pdfplumber
    parts: list[str] = []
    n_pages = 0
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            n_pages += 1
            text = page.extract_text()
            if text and text.strip():
                parts.append(text.strip())
    text = "\n\n".join(parts)
    if n_pages and len(text) / n_pages >= _SCANNED_PDF_CHARS_PER_PAGE:
        return text
    # Sparse or empty text layer — likely a scanned/image PDF. OCR it and
    # keep whichever result carries more content.
    try:
        ocr = _ocr_via_gemini(content, "application/pdf")
    except Exception as exc:
        logger.warning("[docs/extract] OCR fallback failed (%s) — using text layer "
                       "(%d chars)", exc, len(text))
        return text
    return ocr if len(ocr) > len(text) else text


def _extract_docx(content: bytes) -> str:
    """Paragraphs and tables in the order they appear.

    `Document.paragraphs` alone skips every table cell — and a client's call
    script, price list or objection/answer grid is very often a table. Walking
    the body keeps a table where the author put it, so a heading still sits
    above the rows it introduces.
    """
    import docx as _docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = _docx.Document(io.BytesIO(content))
    parts: list[str] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            text = Paragraph(child, doc).text.strip()
            if text:
                parts.append(text)
        elif child.tag == qn("w:tbl"):
            rows: list[str] = []
            for row in Table(child, doc).rows:
                cells: list[str] = []
                for cell in row.cells:
                    value = " ".join(cell.text.split())
                    # Merged cells repeat across the row; keep one copy.
                    if value and (not cells or cells[-1] != value):
                        cells.append(value)
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append("\n".join(rows))
    return "\n\n".join(parts)


def _extract_xlsx(content: bytes) -> str:
    """Every sheet, one line per non-empty row, cells joined with ' | '.

    Values, not formulas (`data_only`), and read-only mode so a large price
    book does not build a full object tree in memory.
    """
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for ws in wb.worksheets:
            rows: list[str] = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c).strip() for c in row
                         if c is not None and str(c).strip() != ""]
                if cells:
                    rows.append(" | ".join(cells))
            if rows:
                parts.append(f"## {ws.title}\n" + "\n".join(rows))
    finally:
        wb.close()
    return "\n\n".join(parts)


def _ocr_via_gemini(content: bytes, mime_type: str) -> str:
    """Verbatim transcription of a scanned PDF / image via Gemini multimodal.

    Uses the native generateContent API (the OpenAI-compat endpoint does not
    take PDF inline data). Key comes from GEMINI_API_KEY / LLM_API_KEY; model
    is overridable via DOC_OCR_MODEL.
    """
    if len(content) > _OCR_MAX_BYTES:
        raise ValueError(
            f"file too large for OCR ({len(content) // (1024 * 1024)} MB > "
            f"{_OCR_MAX_BYTES // (1024 * 1024)} MB) and it has no text layer")
    api_key = (os.environ.get("GEMINI_API_KEY", "").strip()
               or os.environ.get("LLM_API_KEY", "").strip())
    if not api_key:
        raise ValueError("no GEMINI_API_KEY/LLM_API_KEY configured for OCR")
    model = os.environ.get("DOC_OCR_MODEL", "").strip() or "gemini-3.1-flash-lite"
    import httpx
    resp = httpx.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        headers={"x-goog-api-key": api_key},
        json={
            "contents": [{"parts": [
                {"inline_data": {
                    "mime_type": mime_type,
                    "data": base64.b64encode(content).decode("ascii"),
                }},
                {"text": (
                    "Transcribe ALL text content of this document verbatim, in "
                    "reading order, as plain markdown. Preserve headings, lists "
                    "and tables. Do not summarize, translate, or add commentary."
                )},
            ]}],
            "generationConfig": {"temperature": 0, "maxOutputTokens": 65536},
        },
        timeout=120.0,
    )
    resp.raise_for_status()
    data = resp.json()
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("text")).strip()
    logger.info("[docs/extract] Gemini OCR (%s, %d bytes) -> %d chars",
                mime_type, len(content), len(text))
    return text
