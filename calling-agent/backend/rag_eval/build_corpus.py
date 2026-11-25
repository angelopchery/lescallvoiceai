"""Materialise the evaluation corpus from REAL platform content.

The corpus is not written by hand and is not synthetic. Every file under
`corpus/` is a verbatim copy of content that exists in this repository and is
served to real callers today:

  * `dhronai_profile.txt` — the extracted text of the only document any tenant
    has actually uploaded through the Agent Builder
    (agents/dhron_calling/documents/eb0bfae02d/aboutdhron.docx).
  * every other file — the `knowledge_base.portfolio_prompt_text` of a live
    agent definition, i.e. the paragraph that agent reads from on every call.

Why the copy instead of reading the live files at eval time: agent.yaml is
hot-reloaded and edited by tenants through the Copilot, so an evaluation that
read it live would silently change its own corpus between two runs and make
every before/after comparison meaningless. `manifest.json` records the source
path, a sha256 and the extraction date, so a drifted copy is detectable
(`--check`) and a deliberate refresh is one command.

    python -m rag_eval.build_corpus            # rewrite corpus/ + manifest
    python -m rag_eval.build_corpus --check    # non-zero exit if drifted
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

import yaml

_HERE = Path(__file__).resolve().parent
CORPUS_DIR = _HERE / "corpus"
MANIFEST = CORPUS_DIR / "manifest.json"
_AGENTS = _HERE.parents[1] / "agents"

# (corpus name, kind, source path relative to the repo's agents/ dir, title)
#
# A 5th element, when present, is a literal marker: everything from that marker
# onward is dropped before the copy is written. It exists for exactly one
# reason — one live agent's knowledge paragraph carries a named employee's
# current and expected salary, and an evaluation corpus is a file that gets
# copied into reports, CI logs and shared artifacts. The retrieval value of
# that document is its company name colliding with the target document's; the
# compensation sentences add nothing to it. The redaction is recorded in the
# manifest so the copy is never mistaken for verbatim.
#
# The distractor set is chosen for retrieval difficulty, not for volume:
# `dhron_ai_angelobot` is a DIFFERENT document about the SAME company (the
# near-duplicate case that breaks naive top-1 retrieval), and the remaining
# eight are other tenants' catalogues — the realistic "semantically similar
# B2B sales copy that is nonetheless the wrong document" population.
SOURCES: list[tuple] = [
    ("dhronai_profile", "document",
     "dhron_calling/documents/eb0bfae02d/text.txt",
     "DhronAI Company Knowledge Base — Sales Enablement Guide"),
    ("dhron_ai_angelobot", "knowledge_base", "angelobot/agent.yaml",
     "Dhron AI — engineer introduction (near-duplicate distractor)",
     "Salary facts"),
    ("gamezone_india", "knowledge_base", "nintendo_gamer/agent.yaml",
     "GameZone India — Nintendo Switch console catalogue"),
    ("nike_india", "knowledge_base", "nike_shoe_specialist/agent.yaml",
     "Nike — footwear catalogue"),
    ("kaapadh_fabrics", "knowledge_base", "kaapdfabrics/agent.yaml",
     "Kaapadh Fabrics — fabric knowledge base"),
    ("b2_market", "knowledge_base", "hardik-outbound/agent.yaml",
     "B2 Market — product knowledge base"),
    ("serene_health", "knowledge_base", "serene_health/agent.yaml",
     "Serene Health Clinics — specialist directory"),
    ("horizon_homes", "knowledge_base", "demo_realty/agent.yaml",
     "Horizon Homes — project portfolio"),
    ("pramukh_group", "knowledge_base", "pramukh/agent.yaml",
     "Pramukh Group — project portfolio"),
    ("dhron_eye", "knowledge_base", "shubhanga/agent.yaml",
     "Dhron Eye — product portfolio"),
]


def _extract(kind: str, path: Path) -> str:
    if kind == "document":
        return path.read_text(encoding="utf-8")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    kb = data.get("knowledge_base") or {}
    label = kb.get("portfolio_section_label") or "KNOWLEDGE BASE"
    body = (kb.get("portfolio_prompt_text") or "").strip()
    company = ((data.get("identity") or {}).get("company_name") or "").strip()
    header = f"{company} — {label}".strip(" —")
    catalog = kb.get("catalog") or []
    extra = ""
    if isinstance(catalog, list) and catalog:
        # Catalogue entries are structured; render them the way the agent's own
        # prompt renders them so the eval corpus matches what a caller hears.
        lines = []
        for item in catalog:
            if isinstance(item, dict):
                lines.append("  ".join(f"{k}: {v}" for k, v in item.items() if v))
            else:
                lines.append(str(item))
        extra = "\n\nCATALOGUE\n" + "\n".join(lines)
    return f"{header}\n\n{body}{extra}\n"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(check_only: bool = False) -> int:
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    entries, drift = [], []
    for name, kind, rel, title, *rest in SOURCES:
        src = _AGENTS / rel
        if not src.exists():
            print(f"  MISSING source for {name}: {src}", file=sys.stderr)
            return 2
        text = _extract(kind, src)
        redact_from = rest[0] if rest else None
        if redact_from and redact_from in text:
            text = text[:text.index(redact_from)].rstrip() + "\n"
        dest = CORPUS_DIR / f"{name}.txt"
        if check_only:
            have = dest.read_text(encoding="utf-8") if dest.exists() else ""
            if have != text:
                drift.append(name)
        else:
            dest.write_text(text, encoding="utf-8")
        entry = {
            "name": name, "title": title, "kind": kind,
            "source": f"calling-agent/agents/{rel}",
            "sha256": _sha(text), "chars": len(text),
        }
        if redact_from:
            entry["redacted"] = f"truncated at {redact_from!r} (personal data)"
        entries.append(entry)

    if check_only:
        if drift:
            print("corpus DRIFTED from source: " + ", ".join(drift), file=sys.stderr)
            print("re-run `python -m rag_eval.build_corpus` and re-baseline.",
                  file=sys.stderr)
            return 1
        print(f"corpus in sync with source ({len(entries)} documents)")
        return 0

    MANIFEST.write_text(json.dumps({
        "generated_at": datetime.date.today().isoformat(),
        "note": "Verbatim copies of live platform content — see build_corpus.py.",
        "documents": entries,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    total = sum(e["chars"] for e in entries)
    print(f"wrote {len(entries)} documents ({total:,} chars) to {CORPUS_DIR}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="verify corpus/ still matches its sources; do not write")
    raise SystemExit(build(check_only=ap.parse_args().check))
