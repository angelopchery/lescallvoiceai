"""
agent_definition — platform-level agent configuration layer.

Public API for the rest of the codebase:

    from app.agent_definition import AgentDefinition, get_pramukh_agent
"""

from .schema import (
    AgentDefinition,
    AgentIdentity,
    AppointmentSlot,
    BrandConfig,
    CalendarConfig,
    CallFlowConfig,
    FollowUpConfig,
    GuardrailsConfig,
    KnowledgeBase,
    LanguageConfig,
    LLMConfig,
    OutboundConfig,
    PersonalityConfig,
    PostCallConfig,
    ResponseBank,
    STTCorrections,
    SlotDefinition,
    StageDefinition,
    TTSVoiceConfig,
)
from .pramukh_agent import get_pramukh_agent
from .fallback_agent import get_neutral_fallback_agent

__all__ = [
    "AgentDefinition",
    "AgentIdentity",
    "AppointmentSlot",
    "BrandConfig",
    "CalendarConfig",
    "CallFlowConfig",
    "FollowUpConfig",
    "GuardrailsConfig",
    "KnowledgeBase",
    "LanguageConfig",
    "LLMConfig",
    "OutboundConfig",
    "PersonalityConfig",
    "PostCallConfig",
    "ResponseBank",
    "STTCorrections",
    "SlotDefinition",
    "StageDefinition",
    "TTSVoiceConfig",
    "get_pramukh_agent",
    "get_neutral_fallback_agent",
]
