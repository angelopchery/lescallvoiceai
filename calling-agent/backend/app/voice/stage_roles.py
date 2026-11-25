"""
stage_roles — the legacy stage-id → semantic-role shim (v2, plan §4.3).

Shared by conversation.py and state_manager.py (which must not import each
other). Explicit StageDefinition.role always wins; these are the derivations
for legacy ids. Custom ids derive 'custom' and keep turn-count-only pacing.
"""

from __future__ import annotations

LEGACY_STAGE_ROLES = {
    "GREETING": "greeting",
    "DISCOVERY": "discovery",
    "PROJECT_MATCH": "pitch",
    "INVITE_VISIT": "scheduling",
    "CONFIRMATION": "confirmation",
    "CLOSING": "closing",
    "OPENING": "confirm_person",
    "PITCH": "pitch",
    "HANDLE_QUESTIONS": "pitch",
    "ASK": "scheduling",
    "SLOT_BOOKING": "scheduling",
}


def derive_stage_role(stage_id: str) -> str:
    return LEGACY_STAGE_ROLES.get((stage_id or "").upper(), "custom")


def stage_role(stage_def, stage_id: str) -> str:
    """Explicit role from a StageDefinition, else derived from the id."""
    explicit = (getattr(stage_def, "role", "") or "") if stage_def is not None else ""
    return explicit or derive_stage_role(stage_id)
