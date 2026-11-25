"""
NEUTRAL_FALLBACK_AGENT — what answers a call when the tenant's agent fails to load.

Before this existed, a failed agent load fell back to get_pramukh_agent(): a
tenant's caller was answered as Vishal Patel from Pramukh Group. That must
never happen again. This agent has a generic assistant persona, no brand, and
an apologetic close; its only job is to fail politely while the ERROR-level
load-failure log gets someone's attention.

It is built entirely from platform dataclasses (no YAML, no I/O) so it can
never itself fail to load.
"""

from __future__ import annotations

from .schema import (
    AgentDefinition,
    AgentIdentity,
    AppointmentSlot,  # noqa: F401  (re-export convenience for tests)
    CalendarConfig,
    CallFlowConfig,
    KnowledgeBase,
    LanguageConfig,
    LLMConfig,
    PersonalityConfig,
    ResponseBank,
    StageDefinition,
    STTCorrections,
    TTSVoiceConfig,
)

_APOLOGY_EN = ("I'm sorry, we're having a technical issue right now — "
               "please call back shortly.")
_APOLOGY_HI = ("माफ़ कीजिए, अभी हमारे सिस्टम में कुछ technical दिक्कत है — "
               "कृपया थोड़ी देर बाद फिर call कीजिए।")
_APOLOGY_GU = ("માફ કરશો, અત્યારે અમારી સિસ્ટમમાં ટેક્નિકલ સમસ્યા છે — "
               "કૃપા કરીને થોડી વાર પછી ફરી કૉલ કરો.")


def get_neutral_fallback_agent() -> AgentDefinition:
    """A brand-less, apologetic assistant used ONLY when an agent load fails."""
    return AgentDefinition(
        agent_id="neutral-fallback-v1",
        version="1.0.0",
        identity=AgentIdentity(
            agent_name="Assistant",
            company_name="",
            company_tagline="",
            role_description="phone assistant",
            contact="",
            pronouns="she",
        ),
        personality=PersonalityConfig(),
        languages=LanguageConfig(
            supported=["en", "hi", "gu"],
            default="hi",
            tts_voices={
                "en": TTSVoiceConfig(),
                "hi": TTSVoiceConfig(),
                "gu": TTSVoiceConfig(),
            },
            register_notes={},
        ),
        call_flow=CallFlowConfig(
            stages=[
                StageDefinition(
                    id="CLOSING",
                    order=0,
                    goal=(
                        "There is a temporary technical issue with this line. "
                        "Apologize briefly, ask the caller to call back shortly, "
                        "and end the call politely. Do not pitch anything, do not "
                        "collect information, do not make promises."
                    ),
                    turns_until_advance=99,
                ),
            ],
            decline_threshold=2,
        ),
        knowledge_base=KnowledgeBase(
            known_projects={},
            locations=[],
            portfolio_prompt_text=(
                "No business knowledge is available on this call due to a "
                "technical issue. Apologize and ask the caller to call back."
            ),
            portfolio_section_label="KNOWLEDGE BASE",
            stt_corrections=STTCorrections(),
        ),
        response_bank=ResponseBank(
            opening_greeting=("Hello, Namaste! " + _APOLOGY_EN),
            identity_responses={
                "en": "This is an automated assistant. " + _APOLOGY_EN,
                "hi": "मैं एक automated सहायक हूँ। " + _APOLOGY_HI,
                "gu": "હું એક automated સહાયક છું. " + _APOLOGY_GU,
            },
            objection_responses={
                "en": [_APOLOGY_EN],
                "hi": [_APOLOGY_HI],
                "gu": [_APOLOGY_GU],
            },
            decline_farewell={
                "en": _APOLOGY_EN + " Thank you, goodbye.",
                "hi": _APOLOGY_HI + " धन्यवाद, नमस्ते।",
                "gu": _APOLOGY_GU + " આભાર, આવજો.",
            },
            lang_switch_responses={
                "en": _APOLOGY_EN,
                "hi": _APOLOGY_HI,
                "gu": _APOLOGY_GU,
            },
            lang_fallbacks={
                "en": _APOLOGY_EN,
                "hi": _APOLOGY_HI,
                "gu": _APOLOGY_GU,
            },
            timeout_fallbacks={
                "en": _APOLOGY_EN,
                "hi": _APOLOGY_HI,
                "gu": _APOLOGY_GU,
            },
            response_fallback=_APOLOGY_EN,
            graceful_close={
                "en": _APOLOGY_EN + " Thank you, goodbye.",
                "hi": _APOLOGY_HI + " धन्यवाद, नमस्ते।",
                "gu": _APOLOGY_GU + " આભાર, આવજો.",
            },
        ),
        calendar=CalendarConfig(type="mock", mock_slots=[], slots_text={}),
        llm_config=LLMConfig(
            model="gpt-4o-mini",
            max_tokens=120,
            timeout_s=8.0,
            stream_timeout_s=10.0,
            filler_timeout_s=1.5,
            intent_classify_timeout_s=1.0,
            max_response_length=400,
        ),
        slots=[],
        call_direction="inbound",
    )
