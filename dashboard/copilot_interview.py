"""
copilot_interview — the data-driven interview blueprint (plan §6.1, D12).

INTERVIEW_LAYERS is the machine that replaced the prompt-prose checklist:
each layer has compulsory questions (question_hint), the AgentSpec v2 paths
its answers land in, and a pure `done(draft)` check. Completeness is
SERVER-computed (interview_progress) — never LLM-trusted.

The draft is a partial AgentSpec v2 dict accumulated by the update_draft tool;
`_ingested_docs` (list of doc ids) is the only non-spec key it may carry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


def _get(draft: dict, *path, default=None):
    node = draft or {}
    for p in path:
        if not isinstance(node, dict):
            return default
        node = node.get(p)
        if node is None:
            return default
    return node


def _nonempty_str(v, min_len: int = 1) -> bool:
    return isinstance(v, str) and len(v.strip()) >= min_len


def _supported_langs(draft: dict) -> list[str]:
    sup = _get(draft, "languages", "supported", default=[])
    return [x for x in sup if isinstance(x, str) and x.strip()] if isinstance(sup, list) else []


def _lang_cover_ok(draft: dict, d: dict) -> bool:
    """Per-language coverage: an entry for EVERY supported code (en-only
    content on a hi/gu agent is rejected)."""
    langs = _supported_langs(draft)
    if not langs:
        return False
    return isinstance(d, dict) and all(_nonempty_str(d.get(l)) for l in langs)


# ─────────────────────────────────────────────────────────────────────────────
# done() checks — each returns (bool, [missing descriptions])
# ─────────────────────────────────────────────────────────────────────────────

def _done_business(draft):
    missing = []
    if not _nonempty_str(_get(draft, "identity", "company_name")):
        missing.append("what the business is (company name)")
    if not _nonempty_str(_get(draft, "identity", "role_description"), 8):
        missing.append("what customers call about / the agent's role")
    return not missing, missing


def _done_objective(draft):
    missing = []
    if _get(draft, "call_direction") not in ("inbound", "outbound"):
        missing.append("inbound or outbound?")
    if not _nonempty_str(_get(draft, "call_flow", "call_goal"), 15):
        missing.append("the ONE thing a successful call accomplishes")
    return not missing, missing


def _done_persona(draft):
    missing = []
    if not _nonempty_str(_get(draft, "identity", "agent_name")):
        missing.append("the agent's spoken name")
    pers = _get(draft, "personality", default={}) or {}
    if not isinstance(pers, dict) or len([k for k in
            ("energy", "tone", "style", "pronouns", "ai_disclosure",
             "custom_instructions", "traits") if pers.get(k)]) < 2:
        missing.append("speaking style (warm/formal, energetic/calm, admit-AI policy)")
    return not missing, missing


def _done_languages(draft):
    missing = []
    langs = _supported_langs(draft)
    if not langs:
        missing.append("which languages (hi/en/gu)")
    if not _nonempty_str(_get(draft, "languages", "default")):
        missing.append("the opening language")
    tv = _get(draft, "languages", "tts_voices", default={}) or {}
    if not (isinstance(tv, dict) and tv):
        missing.append("the voice (male/female + speaker)")
    return not missing, missing


def _done_brand(draft):
    br = _get(draft, "brand", default={}) or {}
    ok = isinstance(br, dict) and (
        (isinstance(br.get("trust_proofs"), list) and br["trust_proofs"])
        or (isinstance(br.get("credibility_markers"), list) and br["credibility_markers"]))
    return ok, ([] if ok else ["2-3 credibility markers / trust proofs"])


def _done_call_flow(draft):
    stages = _get(draft, "call_flow", "stages", default=[]) or []
    stages = [s for s in stages if isinstance(s, dict)]
    missing = []
    if len(stages) < 2:
        missing.append("the ideal call walked step by step (≥2 stages)")
    elif any(not _nonempty_str(s.get("goal"), 25) for s in stages):
        missing.append("a substantive goal (2+ sentences) for every stage")
    return not missing, missing


def _done_data_capture(draft):
    slots = [s for s in (_get(draft, "slots", default=[]) or []) if isinstance(s, dict)]
    missing = []
    if not slots:
        missing.append("what must be collected from every caller (the slots)")
        return False, missing
    langs = _supported_langs(draft)
    if not langs:
        missing.append("languages must be settled before slot questions")
        return False, missing
    bad = [s.get("name") for s in slots
           if not all(_nonempty_str((s.get("question") or {}).get(l)) for l in langs)]
    if bad:
        missing.append("question wording in EVERY language ("
                       + "/".join(langs) + ") for: " + ", ".join(str(b) for b in bad))
    return not missing, missing


def _is_sales_draft(draft) -> bool:
    """Outbound agents and flows with pitch/recommend stages SELL — they must
    enumerate what they are allowed to claim, or the live model improvises a
    catalog (Kaapd: 'party-wear sarees' invented into an empty catalog).
    Limitation: inbound flows whose stages carry no roles fall back to the
    prose gate."""
    if str(draft.get("call_direction") or "").strip().lower() == "outbound":
        return True
    stages = _get(draft, "call_flow", "stages", default=[]) or []
    return any(isinstance(s, dict) and s.get("role") in ("pitch", "recommend")
               for s in stages)


def _done_knowledge(draft):
    text = _get(draft, "knowledge_base", "portfolio_prompt_text", default="")
    catalog = _get(draft, "knowledge_base", "catalog", default=[]) or []
    catalog = [c for c in catalog
               if isinstance(c, dict) and str(c.get("name") or "").strip()]
    docs = draft.get("_ingested_docs") or []
    if _is_sales_draft(draft):
        # A prose paragraph alone lets the agent improvise products — a sales
        # agent needs at least one named catalog item (skip_layer remains the
        # user's escape hatch; strict grounding covers the skipped case).
        ok = bool(catalog)
        return ok, ([] if ok else
                    ["each product or product family the agent may claim, as "
                     "catalog items (name + 1-2 pitch points) — a text "
                     "description alone lets the agent improvise products; "
                     "mine the uploaded document/session before asking again"])
    ok = _nonempty_str(text, 80) or bool(catalog) or bool(docs)
    return ok, ([] if ok else
                ["the products/services knowledge (≥80 chars, a catalog, or an uploaded document)"])


def _done_objections(draft):
    playbook = _get(draft, "response_bank", "objection_playbook", default=[]) or []
    playbook = [o for o in playbook if isinstance(o, dict)]
    # objection_responses ({lang: [str]}) is the other real home for pushback
    # answers — recording there must satisfy this layer too, or the interview
    # re-asks a question the user already answered (incident 2026-07-15).
    responses = _get(draft, "response_bank", "objection_responses", default={}) or {}
    has_responses = isinstance(responses, dict) and any(
        isinstance(v, list) and any(_nonempty_str(x) for x in v)
        for v in responses.values())
    missing = []
    if not playbook:
        if has_responses:
            return True, []
        missing.append("the top pushbacks and the best answer to each")
        return False, missing
    # An answer in ANY language closes the topic — full language coverage is
    # a quality concern (the copilot can translate), never a reason to re-ask
    # a user who already gave the answers (Kaapd doc test: en-only answers
    # with hi/en supported left the layer open on some extraction runs).
    bad = [o.get("id") or "?" for o in playbook
           if not (isinstance(o.get("answer"), dict)
                   and any(_nonempty_str(v) for v in o["answer"].values()))]
    if bad and not has_responses:
        missing.append("the best answer to: " + ", ".join(map(str, bad)))
    return not missing, missing


def _done_scheduling(draft):
    # "We don't book appointments" is a VALID answer — an explicit no-booking
    # marker closes the layer (previously mock_slots=[] could never satisfy
    # it, which is how no-booking agents shipped phantom calendars).
    _sched = _get(draft, "call_flow", "scheduling", default={}) or {}
    no_booking = (
        str(_get(draft, "calendar", "type", default="")).lower() == "none"
        or (isinstance(_sched, dict) and _sched.get("enabled") is False))
    ok = no_booking or bool(
        _get(draft, "calendar", "mock_slots", default=[])
        or _get(draft, "call_flow", "scheduling", default={})
        or _get(draft, "call_flow", "callback_policy", default={})
        or _get(draft, "follow_up", "whatsapp_promise", default={}))
    return ok, ([] if ok else
                ["bookable slots / scheduling style / callback or WhatsApp "
                 "follow-up policy — or say the business doesn't book "
                 "appointments (calendar.type: none)"])


def _done_guardrails(draft):
    gr = _get(draft, "guardrails", default={}) or {}
    ok = isinstance(gr, dict) and bool(
        gr.get("never_rules") or gr.get("always_rules")
        or (gr.get("honesty") or {}).get("unknown_answer_policy")
        or (gr.get("compliance") or {}).get("disclaimers"))
    return ok, ([] if ok else
                ["what the agent must NEVER/ALWAYS do (and what to say when it doesn't know)"])


def _done_greeting_close(draft):
    missing = []
    if not _nonempty_str(_get(draft, "response_bank", "opening_greeting"), 10):
        missing.append("the exact opening line")
    closing = _get(draft, "response_bank", "closing_responses", default={}) or {}
    if not _lang_cover_ok(draft, closing):
        langs = "/".join(_supported_langs(draft)) or "each language"
        missing.append(f"the closing line in {langs}")
    return not missing, missing


def _done_post_call(draft):
    pc = _get(draft, "post_call", default={}) or {}
    ok = isinstance(pc, dict) and bool(pc.get("outcomes") or pc.get("success_definition"))
    return ok, ([] if ok else ["what makes a call successful (outcome taxonomy)"])


# ─────────────────────────────────────────────────────────────────────────────
# The blueprint
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InterviewLayer:
    id: str
    title: str
    compulsory: bool
    spec_paths: list[str]
    question_hint: str
    followup_hint: str = ""
    allows_upload: bool = False
    done: Callable[[dict], tuple[bool, list[str]]] = field(default=lambda d: (False, []))


INTERVIEW_LAYERS: list[InterviewLayer] = [
    InterviewLayer(
        "business_identity", "Business", True,
        ["identity.company_name", "identity.role_description"],
        "What does the business do, and what do customers usually call about?",
        done=_done_business),
    InterviewLayer(
        "call_objective", "Objective", True,
        ["call_flow.call_goal", "call_direction"],
        "What is the ONE thing a successful call accomplishes? And does the agent "
        "ANSWER calls (inbound) or PLACE them (outbound)?",
        done=_done_objective),
    InterviewLayer(
        "persona_voice", "Persona", True,
        ["identity.agent_name", "personality"],
        "What's the agent's name, and how should it come across — warm or formal, "
        "energetic or calm? If someone asks whether it's an AI, admit or deflect?",
        done=_done_persona),
    InterviewLayer(
        "languages", "Languages & voice", True,
        ["languages.supported", "languages.default", "languages.tts_voices"],
        "Which of Hindi / English / Gujarati should it speak (adding another "
        "language is an engineering task)? Which language does it open in, and "
        "male or female voice?",
        done=_done_languages),
    InterviewLayer(
        "brand_trust", "Brand & trust", True,
        ["brand"],
        "What makes the business credible — years in business, customer counts, "
        "certifications? The top 2-3 proofs are the agent's ammo against doubt.",
        done=_done_brand),
    InterviewLayer(
        "call_flow", "Call flow", True,
        ["call_flow.stages"],
        "Let's map the ideal call in 3-5 steps — I'll turn each into a stage. "
        "Three specific things: (1) right after the hello, what should the "
        "agent ask or say first? (2) what comes in the middle — pitch first or "
        "questions first, and in what order? (3) how should the call end when "
        "the person is interested, and when they're not?",
        followup_hint="For each step: what must be TRUE before moving to the "
                      "next one (e.g. 'budget confirmed', 'interest shown')? "
                      "Propose defaults from the business type instead of "
                      "making the user invent them.",
        done=_done_call_flow),
    InterviewLayer(
        "data_capture", "Data capture", True,
        ["slots"],
        "What must be collected from every caller (name, requirement, budget…)? "
        "In what order, and what's the exact question wording in each language?",
        followup_hint="Which values should be read back to confirm (phone in 2-digit "
                      "groups)? How many times to ask before moving on?",
        done=_done_data_capture),
    InterviewLayer(
        "knowledge_catalog", "Knowledge", True,
        ["knowledge_base.portfolio_prompt_text", "knowledge_base.catalog"],
        "Tell me everything the agent must know — the full product/service list "
        "with names, prices, and facts. You can also upload a brochure or price "
        "sheet and I'll extract it.",
        allows_upload=True,
        done=_done_knowledge),
    InterviewLayer(
        "objections_branching", "Objections", True,
        ["response_bank.objection_playbook", "response_bank.objection_responses"],
        "What are the top pushbacks callers give ('too expensive', 'not now'), "
        "and the best answer to each? After the pitch: what should happen on "
        "interest, on rejection, and on 'what else do you have'? (Interest/"
        "rejection handling belongs in the wording of the EXISTING stage goals "
        "— never replace call_flow.stages to record objections.)",
        done=_done_objections),
    InterviewLayer(
        "scheduling_followup", "Scheduling", True,
        ["calendar", "call_flow.scheduling", "call_flow.callback_policy",
         "follow_up.whatsapp_promise"],
        "Which time slots can the agent book? Offer exactly two options or stay "
        "flexible? If they say 'call me tomorrow' — capture the time? Promise "
        "WhatsApp photos/details?",
        done=_done_scheduling),
    InterviewLayer(
        "guardrails_compliance", "Guardrails", True,
        ["guardrails"],
        "What must the agent NEVER do or say, and what must it ALWAYS do? Any "
        "sector disclaimers? What should it say when it doesn't know something?",
        done=_done_guardrails),
    InterviewLayer(
        "greeting_close", "Greeting & close", True,
        ["response_bank.opening_greeting", "response_bank.closing_responses"],
        "What's the exact opening line when the call connects (I can draft one), "
        "and the closing line in each language?",
        done=_done_greeting_close),
    InterviewLayer(
        "post_call", "Outcomes", False,
        ["post_call"],
        "What counts as a successful call, and how should calls be scored?",
        done=_done_post_call),
]

_LAYERS_BY_ID = {l.id: l for l in INTERVIEW_LAYERS}


def interview_progress(draft: dict, skipped: list[str]) -> dict:
    """Pure, server-computed completeness — the create gate's ground truth."""
    layers = []
    next_layer = None
    for layer in INTERVIEW_LAYERS:
        is_skipped = layer.id in (skipped or [])
        try:
            ok, missing = layer.done(draft or {})
        except Exception:
            ok, missing = False, ["(check failed)"]
        layers.append({
            "id": layer.id, "title": layer.title,
            "compulsory": layer.compulsory,
            "done": bool(ok), "skipped": is_skipped,
            "missing": missing,
            "allows_upload": layer.allows_upload,
        })
        if next_layer is None and layer.compulsory and not ok and not is_skipped:
            next_layer = layer
    open_compulsory = [l["id"] for l in layers
                       if l["compulsory"] and not l["done"] and not l["skipped"]]
    return {
        "layers": layers,
        "complete": not open_compulsory,
        "open_layers": open_compulsory,
        "next_layer_id": next_layer.id if next_layer else None,
        "next_question": next_layer.question_hint if next_layer else None,
        "next_followup": next_layer.followup_hint if next_layer else None,
    }


def render_interview_state(progress: dict) -> str:
    """Compact INTERVIEW STATE block for the system prompt (~350-token cap)."""
    marks = []
    for l in progress["layers"]:
        mark = "✓" if l["done"] else ("~skip" if l["skipped"] else "✗")
        marks.append(f"{mark} {l['title']}")
    lines = ["INTERVIEW STATE (server-tracked — this list is the truth, not your memory):",
             " | ".join(marks)]
    if progress["next_layer_id"]:
        layer = _LAYERS_BY_ID[progress["next_layer_id"]]
        lines.append(f"NEXT: ask exactly this ONE question ({layer.title}): {progress['next_question']}")
        if layer.spec_paths:
            lines.append("Record answers under: " + ", ".join(layer.spec_paths))
        if progress["next_followup"]:
            lines.append(f"Then, if natural: {progress['next_followup']}")
        missing = next((l["missing"] for l in progress["layers"]
                        if l["id"] == progress["next_layer_id"]), [])
        if missing:
            lines.append("Still missing here: " + "; ".join(missing))
        if layer.allows_upload:
            lines.append("Offer the document upload: they can attach a brochure/price "
                         "sheet and you call ingest_document on it.")
    else:
        lines.append("ALL compulsory layers are complete (or explicitly skipped) — "
                     "summarize the plan in 3-5 short lines and ask 'shall I build it?'.")
    lines.append(
        "RULES: mine EVERY answer (and any info dump) into update_draft FIRST, then ask "
        "the next missing question. ONE question per turn. If the user says to skip a "
        "topic, call skip_layer with their exact words. After business+objective are "
        "known, PROPOSE a tailored slot set, stage skeleton, and likely objections for "
        "this business type and ask them to confirm (pass source='proposal' to "
        "update_draft when they accept).")
    return "\n".join(lines)
