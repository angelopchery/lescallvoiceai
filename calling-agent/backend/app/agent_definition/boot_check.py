"""
boot_check — create/update-time engine validation for agent definitions.

Incident B (2026-07-15): a Copilot-written `forbidden_fabrications: true`
passed AgentDefinition.from_dict cleanly and crashed the prompt build on
every live turn — three dead calls answering only "Sorry, could you repeat
that?". Nothing had ever executed the ENGINE's prompt assembly against the
definition before a real phone call did.

engine_boot_check() renders every prompt surface exactly the way a live call
would — every stage × every supported language × both directions, plus the
opening greeting — on throwaway routers. Errors mean the agent WOULD crash
or misrender live and the write must be refused; warnings are semantic and
non-blocking (they must NEVER graduate into automatic YAML repairs — repairs
belong to the Copilot LLM regenerating the spec with the user).

Read-only, offline, deterministic; ~5-15 ms per agent after module import.
Callers in async contexts must run it via `asyncio.to_thread(...)` — the
greeting leg uses `asyncio.run` internally and needs a loop-free thread.
"""

from __future__ import annotations

import asyncio
import re

from .schema import AgentDefinition

# Unresolved {token} in a rendered greeting would be SPOKEN to the caller.
_TEMPLATE_TOKEN = re.compile(r"\{[a-z_]+\}")

_ERROR_CAP = 8


def _innermost_frame(exc: BaseException) -> str:
    """Deepest engine frame of the traceback — tells the Copilot LLM WHICH
    field family broke instead of a bare exception type."""
    tb = exc.__traceback__
    best = ""
    while tb is not None:
        fname = tb.tb_frame.f_code.co_filename
        if any(m in fname for m in ("prompt_blocks", "conversation",
                                    "state_manager", "catalog")):
            short = fname.rsplit("/", 1)[-1]
            best = f"{short}:{tb.tb_lineno} in {tb.tb_frame.f_code.co_name}"
        tb = tb.tb_next
    return best or "unknown frame"


def engine_boot_check(agent: AgentDefinition) -> dict:
    """Returns {"errors": [...], "warnings": [...]}.

    errors   → the definition crashes or misrenders on a live call; refuse
               the write and surface the messages to the Copilot LLM.
    warnings → semantic observations (non-blocking, advisory only).
    """
    from ..voice.conversation import ConversationRouter

    errors: list[str] = []
    warnings: list[str] = []

    langs = list(agent.languages.supported or []) \
        or [agent.languages.default or "en"]
    stage_ids = [s.id for s in agent.call_flow.stages] or ["GREETING"]

    for direction in ("inbound", "outbound"):
        for lang in langs:
            if len(errors) >= _ERROR_CAP:
                break
            try:
                router = ConversationRouter(agent_def=agent,
                                            default_language=lang)
                # Deterministic calendar — live bookings.json must never
                # influence validation (golden-harness recipe).
                router._available_calendar_slots = (
                    lambda: list(agent.calendar.mock_slots or []))
                router.call_direction = direction
                if direction == "outbound":
                    router.outbound_contact_name = "Boot Check"
                    router.outbound_contact_phone = "9999999999"
            except Exception as exc:  # noqa: BLE001 — every failure is data
                errors.append(
                    f"router lang={lang} dir={direction}: "
                    f"{type(exc).__name__}: {exc} (at {_innermost_frame(exc)})")
                continue

            try:
                opening = asyncio.run(router.generate_opening())
                text = getattr(opening, "text", "") or ""
                leftover = _TEMPLATE_TOKEN.findall(text)
                if leftover:
                    errors.append(
                        f"greeting lang={lang} dir={direction}: unresolved "
                        f"template token(s) {leftover} would be spoken to "
                        f"the caller verbatim")
            except Exception as exc:  # noqa: BLE001
                errors.append(
                    f"greeting lang={lang} dir={direction}: "
                    f"{type(exc).__name__}: {exc} (at {_innermost_frame(exc)})")

            for sid in stage_ids:
                if len(errors) >= _ERROR_CAP:
                    break
                try:
                    router.state.stage = sid
                    router.state.language = lang
                    router._build_system_prompt()
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"stage={sid} lang={lang} dir={direction}: "
                        f"{type(exc).__name__}: {exc} "
                        f"(at {_innermost_frame(exc)})")

    # ── Non-blocking semantic warnings ────────────────────────────────────
    # Whole-flow-dead stage machine: EVERY non-final stage is un-advanceable
    # by any turn-driven path (99-threshold, no advance_when, no branches).
    # Deliberately narrow — legacy flows advance via booking events and must
    # not be nagged (an LLM 'fixing' production pacing from a warning is a
    # real failure mode).
    stages = agent.call_flow.stages
    if len(stages) > 1:
        non_final = stages[:-1]
        def _dead(s) -> bool:
            return (int(getattr(s, "turns_until_advance", 8) or 8) >= 99
                    and not (getattr(s, "advance_when", None) or {})
                    and not (getattr(s, "branches", None) or []))
        if all(_dead(s) for s in non_final):
            warnings.append(
                "stage machine is inert: every non-final stage has "
                "turns_until_advance >= 99 with no advance_when and no "
                "branches — the call can never progress past stage 0")
    for o in (getattr(agent.response_bank, "objection_playbook", None) or []):
        if not isinstance(o, dict):
            continue
        has_guidance = bool(str(o.get("prompt_guidance") or "").strip())
        answers = o.get("answer") or {}
        has_answer = bool(isinstance(answers, dict)
                          and any(str(v).strip() for v in answers.values()))
        if not has_guidance and not has_answer:
            warnings.append(
                f"objection_playbook entry '{o.get('id') or '?'}' has neither "
                "prompt_guidance nor any answer — it can never influence a call")

    return {"errors": errors[:_ERROR_CAP], "warnings": warnings}
