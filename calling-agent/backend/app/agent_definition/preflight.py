"""
Call-start prompt preflight (F6).

Incident B (2026-07-15): a prompt-build TypeError was swallowed per-turn by
the blanket except around the LLM call and every caller turn became
"Sorry, could you repeat that?" — three dead calls, zero alerts. The prompt
build is fully deterministic, so a broken agent can be detected in ~5-15ms
BEFORE the greeting by rendering every stage × language prompt once on
throwaway routers.

    from app.agent_definition.preflight import preflight_agent
    health = preflight_agent(agent_def)      # 'ok' | 'preflight_failed:<stage>'

The telephony bridge (conversation_call_bridge._build_agent_stack) calls this
right after the agent loads (gated by PROMPT_PREFLIGHT, default ON) and swaps
in the neutral fallback agent when it fails — the caller hears an honest
apology instead of an endless repeat-that loop.

Throwaway-only: this module NEVER touches the live router or session state.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def preflight_agent(agent_def, languages: Optional[list[str]] = None,
                    slug: str = "") -> str:
    """Render every stage × language system prompt for *agent_def*.

    Builds a THROWAWAY ConversationRouter per language (calendar mocked from
    the YAML's mock_slots, exactly like the golden harness) and calls
    _build_system_prompt() for each call-flow stage. Output is discarded.

    Returns 'ok' when every prompt renders, else 'preflight_failed:<stage>'
    naming the first stage whose prompt build raised. Never raises.
    """
    # Lazy relative import: works under both top-level package spellings
    # ('app.*' in tests/run.py, 'backend.app.*' in the telephony bridge) and
    # avoids an agent_definition <-> voice import cycle at module load.
    from ..voice.conversation import ConversationRouter

    name = slug or str(getattr(agent_def, "agent_id", "") or "?")
    try:
        langs = list(languages
                     or getattr(agent_def.languages, "supported", None)
                     or [getattr(agent_def.languages, "default", "en") or "en"])
        stages = list(getattr(agent_def.call_flow, "stages", None) or [])
    except Exception as exc:  # malformed definition — fail closed, loudly
        logger.error("[PREFLIGHT] prompt build FAILED for %s stage=%s lang=%s: %r",
                     name, "?", "?", exc, exc_info=True)
        return "preflight_failed:?"

    for lang in langs:
        stage_id = "?"
        try:
            router = ConversationRouter(agent_def=agent_def,
                                        default_language=lang)
            # Deterministic calendar — same mock as the golden harness, so
            # live bookings.json state can never flip a preflight verdict.
            router._available_calendar_slots = (
                lambda: list(agent_def.calendar.mock_slots or []))
            for stage in stages:
                stage_id = getattr(stage, "id", str(stage))
                router.state.stage = stage_id
                router._build_system_prompt()  # output discarded
        except Exception as exc:
            logger.error(
                "[PREFLIGHT] prompt build FAILED for %s stage=%s lang=%s: %r",
                name, stage_id, lang, exc, exc_info=True)
            return f"preflight_failed:{stage_id}"
    return "ok"
