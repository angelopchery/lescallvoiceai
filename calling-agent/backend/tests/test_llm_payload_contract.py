"""
LLM payload contract — the transport-level twin of the golden prompt harness.

Root cause of BOTH Kaapd incident calls (cf6b0e23 13:26, 25dbbabb 22:03):
the conversation turn sent TWO system messages (the 11k-char agent prompt
first, a ~500-char language trailer last). Gemini's OpenAI-compat endpoint
keeps a single systemInstruction, so the trailer silently REPLACED the whole
agent prompt — every live turn ran with no persona, no knowledge, no rules
(the model even parroted the trailer's own 'Ananya/Mr. Ben' example
sentence). The golden harness could never catch this: it pins what
_build_system_prompt RETURNS, not what the API request CARRIES.

Contract pinned here: exactly ONE system message, at index 0, carrying both
the full agent prompt AND the final language instruction.
"""
from __future__ import annotations

import asyncio

from conftest import AGENTS_DIR

from app.agent_definition.schema import AgentDefinition
from app.voice.conversation import ConversationRouter


def _capture_payload(yaml_path, lang: str, user_text: str) -> dict:
    agent = AgentDefinition.from_yaml(yaml_path)
    router = ConversationRouter(agent_def=agent, default_language=lang)
    router._available_calendar_slots = (
        lambda: list(agent.calendar.mock_slots or []))
    router.call_direction = "outbound"
    router.outbound_contact_name = "Contract Test"
    router.state.language = lang
    router.memory.add_user("हाँ जी बोलिए", language=lang)
    router.memory.add_assistant("जी, बताता हूँ।", language=lang)

    captured: dict = {}

    class _Stream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

    class _Client:
        class chat:
            class completions:
                @staticmethod
                async def create(**kw):
                    captured.update(kw)
                    return _Stream()

    router._get_client = lambda: _Client()
    router._get_fallback_client = lambda: None

    async def _run():
        async for _ in router._call_llm_stream(user_text):
            pass

    asyncio.run(_run())
    return captured


def _assert_contract(captured: dict, expect_probe: str) -> None:
    msgs = captured.get("messages") or []
    assert msgs, "no request captured"
    system_msgs = [m for m in msgs if m["role"] == "system"]
    assert len(system_msgs) == 1, (
        f"{len(system_msgs)} system messages in the payload — providers that "
        "keep only one systemInstruction (Gemini) would drop the agent "
        "prompt entirely (the 2026-07-15 incident class)")
    assert msgs[0]["role"] == "system"
    content = msgs[0]["content"]
    assert expect_probe in content, "agent prompt missing from the payload"
    assert "FINAL INSTRUCTION" in content, (
        "language enforcement missing from the single system message")
    # History must follow as plain user/assistant turns.
    assert all(m["role"] in ("user", "assistant") for m in msgs[1:])
    assert captured.get("max_tokens"), "max_tokens missing from the request"


def test_single_system_message_kaapd_hindi():
    # Was shubh_assistant until it was archived; kaapd_pooja is the live agent
    # covering the same Hindi fabrics prompt surface.
    _assert_contract(
        _capture_payload(AGENTS_DIR / "kaapd_pooja" / "agent.yaml",
                         "hi", "क्या स्टॉक है आपके पास?"),
        expect_probe="Polyester Knitted Fabrics")


def test_single_system_message_pramukh_english():
    _assert_contract(
        _capture_payload(AGENTS_DIR / "pramukh" / "agent.yaml",
                         "en", "which projects do you have?"),
        expect_probe="Pramukh")
